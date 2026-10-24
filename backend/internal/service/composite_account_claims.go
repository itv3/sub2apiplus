package service

// 本文件部分代码改编自 Wei-Shaw/sub2api 的 PR #7114（来源提交
// bcb44606150881a8ce781ea2138d695b6e0d5c3e）中的 backend/internal/service/composite_account_claims.go：
//   - CompositeClaimStrength 及三个强度常量；
//   - CompositeAccountClaimStrength、compositeMappingClaimStrength、
//     compositeClaimStrengthForTarget 的分级与匹配顺序；
//   - CompositeAccountClaimsModel 的"任意强度命中即声明"语义。
// 本仓库与上游同为 LGPL-3.0 许可，改编部分沿用该许可，原作者的版权归属保持不变。
//
// 与 PR 原文的本地差异（对应账号级候选池方案："空映射不入池"）：
//  1. 删除空映射账号走 DetectModelPlatform 加 IsModelSupported 的"受控原生"强声明分支，
//     空映射账号对任何模型一律无声明；
//  2. 删除 ollama_cloud 空映射账号按 extra.allowed_models 白名单声明的分支（同样属于空映射）；
//  3. 声明只读账号凭据里管理员显式写下的 model_mapping，不读 GetModelMapping 展开后的
//     平台默认映射（Grok、Antigravity、Gemini GoogleOne 的内置映射都不构成声明），
//     与现有 explicitModelMappingClaims 读取同一字段保持一致；
//  4. PR 里的 CompositeAccountMeetsClaimTier 与 ctxkey.CompositeRequiredClaimStrength
//     本地不引入：声明强度只在候选池收集账号时判一次，下游调度器只认候选集合；
//  5. compositeMappingClaimStrength 去掉对 explicitModelMappingClaims 的快路径
//     （精确键命中的结果与后面的精确分支相同，保留只会多一份重复逻辑）。

import (
	"strings"
)

// CompositeClaimStrength 表达账号对某模型的声明强度。候选池据此分层：
// 存在强声明账号时只有强声明账号入池，完全没有强声明才使用通配命中的弱声明账号，
// 避免 catch-all 通配账号（如映射 "*"）把显式声明的模型冒领过去。
type CompositeClaimStrength int

const (
	// CompositeClaimNone 无声明。
	CompositeClaimNone CompositeClaimStrength = iota
	// CompositeClaimWildcard 弱声明：账号 model_mapping 的通配键命中。
	// 仅在分组内没有任何强声明账号时才参与候选。
	CompositeClaimWildcard
	// CompositeClaimExplicit 强声明：账号 model_mapping 存在与公开模型名完全相同的
	// 键（含平台既有归一化之后的精确键），且映射目标非空。
	CompositeClaimExplicit
)

// CompositeAccountClaimStrength 返回 account 在"配置态"对 model 的声明强度。
// 只读取账号持久化配置里管理员写下的 model_mapping（精确键强、通配键弱），
// 不读取限流等瞬态运行状态，也不展开平台默认映射。映射为空的账号一律无声明。
func CompositeAccountClaimStrength(account *Account, model string) CompositeClaimStrength {
	if account == nil || account.Credentials == nil {
		return CompositeClaimNone
	}
	model = strings.TrimSpace(model)
	if model == "" {
		return CompositeClaimNone
	}
	mapping := stringMappingFromRaw(account.Credentials["model_mapping"])
	if len(mapping) == 0 {
		// 空映射账号不入池：本方案不再允许账号凭"没写映射"去冒领任何模型。
		return CompositeClaimNone
	}
	return compositeMappingClaimStrength(account.Platform, mapping, model)
}

// compositeMappingClaimStrength 按账号映射的实际命中路径分级，匹配顺序与转发阶段
// 解析映射一致：原始模型精确 → 原始通配 → 归一化精确 → 归一化通配，命中即终止。
// 映射目标为空的条目不构成声明，也不回落到通配（显式条目对通配具有遮蔽性，
// 转发阶段同样走不到通配）。
func compositeMappingClaimStrength(platform string, mapping map[string]string, model string) CompositeClaimStrength {
	if mapped, exists := mapping[model]; exists {
		return compositeClaimStrengthForTarget(mapped, true)
	}
	if mapped, matched := matchWildcardMappingResult(mapping, model); matched {
		return compositeClaimStrengthForTarget(mapped, false)
	}
	normalized := normalizeRequestedModelForLookup(platform, model)
	if normalized != model {
		if mapped, exists := mapping[normalized]; exists {
			return compositeClaimStrengthForTarget(mapped, true)
		}
		if mapped, matched := matchWildcardMappingResult(mapping, normalized); matched {
			return compositeClaimStrengthForTarget(mapped, false)
		}
	}
	return CompositeClaimNone
}

// compositeClaimStrengthForTarget 把"命中的映射条目"换算成声明强度：目标为空不构成声明。
func compositeClaimStrengthForTarget(mapped string, exact bool) CompositeClaimStrength {
	if strings.TrimSpace(mapped) == "" {
		return CompositeClaimNone
	}
	if exact {
		return CompositeClaimExplicit
	}
	return CompositeClaimWildcard
}

// CompositeAccountClaimsModel 报告 account 在"配置态"是否声明了 model（任意强度命中均为 true）。
func CompositeAccountClaimsModel(account *Account, model string) bool {
	return CompositeAccountClaimStrength(account, model) != CompositeClaimNone
}

// compositeClaimTier 按声明强度分层挑出可入候选池的条目：
// 存在强声明条目时只返回强声明条目；完全没有强声明条目才返回弱声明条目；
// 都没有则返回 nil。返回值保持输入顺序，tier 是实际采用的强度层级。
// 泛型参数 T 可以是账号，也可以是候选池缓存的配置态快照，强度由调用方给出。
func compositeClaimTier[T any](items []T, strength func(T) CompositeClaimStrength) (tier CompositeClaimStrength, picked []T) {
	var strong, weak []T
	for _, item := range items {
		switch strength(item) {
		case CompositeClaimExplicit:
			strong = append(strong, item)
		case CompositeClaimWildcard:
			weak = append(weak, item)
		}
	}
	if len(strong) > 0 {
		return CompositeClaimExplicit, strong
	}
	if len(weak) > 0 {
		return CompositeClaimWildcard, weak
	}
	return CompositeClaimNone, nil
}
