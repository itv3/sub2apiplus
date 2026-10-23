package service

import (
	"context"
	"strings"
)

// mappedModelTargetsForListing 收集仅需在模型目录中隐藏的上游原名。
// 只有具体的非自映射别名才触发隐藏，通配符无法直接作为可展示的替代名称。
// 若目标本身也映射到其它模型，则保留其别名含义，避免映射链或环被误删。
// 返回值只用于展示，不得用于修改账号映射、模型准入或实际请求转发。
func mappedModelTargetsForListing(mapping map[string]string) map[string]struct{} {
	var targets map[string]struct{}
	aliases := make(map[string]struct{})
	for source, target := range mapping {
		source = strings.TrimSpace(source)
		target = strings.TrimSpace(target)
		if source == "" || target == "" || source == target || strings.Contains(source, "*") || strings.Contains(target, "*") {
			continue
		}
		if targets == nil {
			targets = make(map[string]struct{})
		}
		targets[target] = struct{}{}
		aliases[source] = struct{}{}
	}
	for alias := range aliases {
		delete(targets, alias)
	}
	return targets
}

// modelMappingForListing 生成账号映射的展示视图，保留原始映射供原名直接调用。
// 没有需要隐藏的目标时复用只读映射；发生过滤时创建副本，避免污染账号缓存。
func modelMappingForListing(mapping map[string]string) map[string]string {
	targets := mappedModelTargetsForListing(mapping)
	if len(targets) == 0 {
		return mapping
	}
	visible := make(map[string]string, len(mapping))
	for model, target := range mapping {
		if _, hidden := targets[strings.TrimSpace(model)]; !hidden {
			visible[model] = target
		}
	}
	return visible
}

// FilterMappedModelTargetsForListing 在 Anthropic 默认目录补齐后继续隐藏已映射原名。
// 默认目录不携带账号来源，需检查同平台账号：若其它账号仍独立提供该请求名，
// 则保留该模型。这里只裁剪展示副本，账号的模型准入和转发规则保持不变。
func (s *GatewayService) FilterMappedModelTargetsForListing(ctx context.Context, groupID *int64, platform string, models []string) []string {
	if platform != PlatformAnthropic || len(models) == 0 || s == nil || s.accountRepo == nil {
		return models
	}
	var accounts []Account
	var err error
	if groupID != nil {
		accounts, err = s.accountRepo.ListSchedulableByGroupID(ctx, *groupID)
	} else {
		accounts, err = s.accountRepo.ListSchedulable(ctx)
	}
	if err != nil {
		return models
	}
	hiddenTargets := make(map[string]struct{})
	accountTargets := make([]map[string]struct{}, len(accounts))
	for i := range accounts {
		account := &accounts[i]
		if account.Platform != platform {
			continue
		}
		targets := mappedModelTargetsForListing(stringMappingFromRaw(account.Credentials["model_mapping"]))
		accountTargets[i] = targets
		for target := range targets {
			hiddenTargets[target] = struct{}{}
		}
	}
	if len(hiddenTargets) == 0 {
		return models
	}
	for target := range hiddenTargets {
		for i := range accounts {
			account := &accounts[i]
			if _, hidden := accountTargets[i][target]; account.Platform == platform && !hidden && account.IsModelSupported(target) {
				delete(hiddenTargets, target)
				break
			}
		}
	}
	visible := make([]string, 0, len(models))
	for _, model := range models {
		if _, hidden := hiddenTargets[strings.TrimSpace(model)]; !hidden {
			visible = append(visible, model)
		}
	}
	return visible
}
