package service

import (
	"strings"
)

// 自定义多协议平台（custom）：管理员自填 Chat Completions / Responses / Anthropic
// 三种协议的上游地址（credentials.api_base_urls），账号固定为 API Key 类型与
// adaptive 协议。profile 不带任何默认地址，各协议是否可用完全由凭据决定
// （见 provider_profile.go 的 CredentialEndpoints 与 providerSupportsProtocol）。
//
// 本文件集中放该平台的账号判定、凭据校验与归一，以及被既有文件调用的小钩子，
// 避免把新逻辑散落进旧文件。

const (
	// credentialKeyAPIBaseURLs 是分协议上游地址在 credentials 里的键。
	credentialKeyAPIBaseURLs = "api_base_urls"
	// credentialKeyAPIProtocol 是账号上游协议在 credentials 里的键。
	credentialKeyAPIProtocol = "api_protocol"
	// credentialKeyBaseURL 是旧版单地址字段；custom 账号保存时与
	// api_base_urls.chat_completions 保持一致。
	credentialKeyBaseURL = "base_url"
)

// IsCustom 报告账号是否为自定义多协议平台账号。
func (a *Account) IsCustom() bool {
	return a != nil && a.Platform == PlatformCustom
}

// credentialAPIBaseURL 读取凭据 api_base_urls 里指定协议的地址（去首尾空白）；
// 未填写、类型不符一律返回空串。同时兼容 map[string]any（JSON 解码结果）与
// map[string]string（程序内构造）两种形态。
func credentialAPIBaseURL(credentials map[string]any, protocol string) string {
	if credentials == nil {
		return ""
	}
	switch urls := credentials[credentialKeyAPIBaseURLs].(type) {
	case map[string]any:
		value, _ := urls[protocol].(string)
		return strings.TrimSpace(value)
	case map[string]string:
		return strings.TrimSpace(urls[protocol])
	default:
		return ""
	}
}

// credentialEndpointFor 返回账号凭据里该协议填写的上游地址，供 profile 无默认地址的
// 供应商判定协议支持。
func (a *Account) credentialEndpointFor(protocol string) string {
	if a == nil {
		return ""
	}
	return credentialAPIBaseURL(a.Credentials, protocol)
}

// isOllamaCloudCustomOutboundAccount 判定 custom 账号的 Chat Completions 出站地址是否
// Ollama Cloud 官方主机：Ollama 的 reasoning 字段归一化与输出 token 上限夹紧都按实际
// 出站地址生效，不再要求账号挂在 OpenAI 平台下。
func isOllamaCloudCustomOutboundAccount(account *Account) bool {
	if account == nil || account.Platform != PlatformCustom || account.Type != AccountTypeAPIKey {
		return false
	}
	return isOllamaCloudBaseURL(account.GetOpenAIBaseURL())
}

// customPlatformSupportsModelListProtocol 判定 profile 无默认地址的供应商账号能否出现在
// 指定协议的 Composite 模型列表里：openai 列表看 Chat Completions / Responses 地址，
// anthropic 列表看 Anthropic 地址；非 adaptive（理论上校验已拦下）只按固定协议归入
// openai 列表。其余平台一律返回 false，保持既有平台行为不变。
func customPlatformSupportsModelListProtocol(account *Account, protocol string) bool {
	profile := account.providerProfile()
	if profile == nil || !profile.CredentialEndpoints {
		return false
	}
	if !account.IsAdaptiveAPIProtocol() {
		return protocol == CompositeModelListProtocolOpenAI
	}
	switch protocol {
	case CompositeModelListProtocolOpenAI:
		return account.providerSupportsProtocol(APIProtocolChatCompletions) ||
			account.providerSupportsProtocol(APIProtocolResponses)
	case CompositeModelListProtocolAnthropic:
		return account.providerSupportsProtocol(APIProtocolAnthropic)
	default:
		return false
	}
}
