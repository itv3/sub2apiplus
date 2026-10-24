package service

import (
	"context"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

const (
	CompositeModelListProtocolOpenAI    = "openai"
	CompositeModelListProtocolAnthropic = "anthropic"
)

// GetCompositeProtocolModelSources 按账号实际接入协议聚合 Composite 分组的模型源。
// 返回值以账号平台分组；平台存在但模型切片为空时，表示该协议下存在可调度账号，
// 但账号没有显式模型映射，调用方可沿用该平台原有的默认模型回退规则。
func (s *GatewayService) GetCompositeProtocolModelSources(ctx context.Context, groupID *int64, protocol string) map[string][]string {
	sources := make(map[string][]string)
	if s == nil || s.accountRepo == nil {
		return sources
	}

	protocol = normalizeCompositeModelListProtocol(protocol)
	if protocol == "" {
		return sources
	}

	var accounts []Account
	var err error
	if groupID != nil {
		accounts, err = s.accountRepo.ListSchedulableByGroupID(ctx, *groupID)
	} else {
		accounts, err = s.accountRepo.ListSchedulable(ctx)
	}
	if err != nil {
		return sources
	}

	modelSets := make(map[string]map[string]struct{})
	forceDefaultPlatforms := make(map[string]struct{})
	protocolAccounts := make(map[string][]Account)
	for i := range accounts {
		account := &accounts[i]
		if !accountSupportsCompositeModelListProtocol(account, protocol) {
			continue
		}

		platform := strings.TrimSpace(account.Platform)
		if platform == "" {
			continue
		}
		if _, ok := modelSets[platform]; !ok {
			modelSets[platform] = make(map[string]struct{})
		}
		protocolAccounts[platform] = append(protocolAccounts[platform], *account)

		// OpenAI 自动透传不受 model_mapping 白名单约束；与现有
		// GetAvailableModels 行为一致，存在透传账号时由调用方回退默认模型。
		if platform == PlatformOpenAI && account.IsOpenAIPassthroughEnabled() {
			forceDefaultPlatforms[platform] = struct{}{}
			continue
		}

		for model := range AdvertisedModelMappingForAccount(account) {
			model = strings.TrimSpace(model)
			if model != "" {
				modelSets[platform][model] = struct{}{}
			}
		}
	}

	for platform, modelSet := range modelSets {
		if _, forceDefault := forceDefaultPlatforms[platform]; forceDefault {
			sources[platform] = compositeOpenAIDefaultsSource(platform, protocolAccounts[platform])
			continue
		}

		models := make([]string, 0, len(modelSet))
		for model := range modelSet {
			models = append(models, model)
		}
		if platform == PlatformOpenAI {
			// 与普通 OpenAI 模型目录保持一致：同一 Composite 组中只要有
			// 未配置映射的 OpenAI 账号，就不能因另一账号的部分映射而
			// 隐藏默认模型。
			models = supplementUnmappedOpenAIModels(protocolAccounts[platform], models)
			if len(models) == 0 {
				// 没有任何显式映射：沿用默认模型（调用方回落默认清单），但要剔除被 GPT-5.5 退役规则挡掉的模型。
				sources[platform] = compositeOpenAIDefaultsSource(platform, protocolAccounts[platform])
				continue
			}
			// 与普通 OpenAI 目录一致（上游 GPT-5.5 退役）：已对 ChatGPT 订阅账号退役的模型不再列出；
			// 过滤后清单为空时保持非 nil 空列表，表示权威结果，调用方不得回落默认清单把退役模型加回来。
			models = filterRetiredOpenAIModelIDs(protocolAccounts[platform], models)
		}
		sort.Strings(models)
		sources[platform] = models
	}

	return sources
}

// compositeOpenAIDefaultsSource 返回「沿用默认模型」场景下 OpenAI 平台的模型来源：
// 默认清单里没有模型被 GPT-5.5 退役规则挡掉时返回 nil，由调用方回落默认清单；
// 有模型被挡掉时返回已剔除退役模型的显式默认清单，调用方不得再回落。其它平台一律返回 nil。
func compositeOpenAIDefaultsSource(platform string, accounts []Account) []string {
	if platform != PlatformOpenAI {
		return nil
	}
	defaults := openai.DefaultModelIDs()
	filtered := filterRetiredOpenAIModelIDs(accounts, defaults)
	if len(filtered) == len(defaults) {
		return nil
	}
	return filtered
}

func normalizeCompositeModelListProtocol(protocol string) string {
	switch strings.ToLower(strings.TrimSpace(protocol)) {
	case CompositeModelListProtocolOpenAI:
		return CompositeModelListProtocolOpenAI
	case CompositeModelListProtocolAnthropic:
		return CompositeModelListProtocolAnthropic
	default:
		return ""
	}
}

func accountSupportsCompositeModelListProtocol(account *Account, protocol string) bool {
	if account == nil {
		return false
	}

	protocol = normalizeCompositeModelListProtocol(protocol)
	switch strings.TrimSpace(account.Platform) {
	case PlatformAnthropic:
		return protocol == CompositeModelListProtocolAnthropic
	case PlatformOpenAI, PlatformGrok:
		return protocol == CompositeModelListProtocolOpenAI
	case PlatformKimi, PlatformZhipu, PlatformDeepseek, PlatformMiniMax:
		switch account.GetAPIProtocol() {
		case APIProtocolAnthropic:
			return protocol == CompositeModelListProtocolAnthropic
		case APIProtocolAdaptive:
			return protocol == CompositeModelListProtocolOpenAI || protocol == CompositeModelListProtocolAnthropic
		case APIProtocolChatCompletions, APIProtocolResponses:
			return protocol == CompositeModelListProtocolOpenAI
		default:
			return false
		}
	default:
		// profile 没有默认地址的平台（自定义多协议）按 profile 与账号凭据判定。
		return customPlatformSupportsModelListProtocol(account, protocol)
	}
}
