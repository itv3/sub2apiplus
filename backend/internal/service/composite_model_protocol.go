package service

import (
	"context"
	"sort"
	"strings"
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
			sources[platform] = nil
			continue
		}

		models := make([]string, 0, len(modelSet))
		for model := range modelSet {
			models = append(models, model)
		}
		sort.Strings(models)
		sources[platform] = models
	}

	return sources
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
	case PlatformKimi, PlatformZhipu, PlatformDeepseek:
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
		return false
	}
}
