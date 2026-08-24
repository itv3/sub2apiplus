package service

import "net/http"

// openAIPluginTransportAllowed 把上游插件能力限制在 Official Egress 闭集之外。
// 官方 OAuth 即使命中插件绑定，也必须继续由调用方交给 Codex Executor；若旧调用点
// 误入本 facade，后续 HTTPUpstream Guard 仍会 fail-close，插件不能先行接管。
func openAIPluginTransportAllowed(account *Account) (bool, error) {
	officialEgressEnabled, _, err := resolveOfficialEgressAccountProfile(account)
	if err != nil {
		return false, err
	}
	return !officialEgressEnabled, nil
}

func (s *OpenAIGatewayService) SetPluginManager(manager *PluginManager) {
	s.pluginManager = manager
}

// doOpenAIUpstream 只在 OpenAI OAuth 能力绑定已启用时把真实请求交给插件。
// 插件返回标准 http.Response，响应解析、错误映射、SSE 和计费仍由现有核心链处理。
func (s *OpenAIGatewayService) doOpenAIUpstream(request *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	pluginAllowed, err := openAIPluginTransportAllowed(account)
	if err != nil {
		return nil, err
	}
	if pluginAllowed && s.pluginManager != nil {
		response, handled, err := s.pluginManager.RoundTripOpenAIOAuth(request.Context(), request, proxyURL, account)
		if handled {
			return response, err
		}
	}
	return doOpenAIAPIKeyHTTPTransport(s.httpUpstream, request, proxyURL, account, nil)
}

// doOpenAIAccountTestUpstream 让 OpenAI OAuth 账号测试与真实转发使用同一插件路径。
// API Key 和未命中插件的账号保持各自原有的 HTTPUpstream 行为。
func (s *AccountTestService) doOpenAIAccountTestUpstream(
	request *http.Request,
	proxyURL string,
	account *Account,
	useTLSFallback bool,
) (*http.Response, error) {
	pluginAllowed, err := openAIPluginTransportAllowed(account)
	if err != nil {
		return nil, err
	}
	if pluginAllowed && s.pluginManager != nil {
		response, handled, err := s.pluginManager.RoundTripOpenAIOAuth(request.Context(), request, proxyURL, account)
		if handled {
			return response, err
		}
	}
	if useTLSFallback {
		return doOpenAIAPIKeyHTTPTransport(
			s.httpUpstream,
			request,
			proxyURL,
			account,
			s.tlsFPProfileService.ResolveTLSProfile(account),
		)
	}
	return doOpenAIAPIKeyHTTPTransport(s.httpUpstream, request, proxyURL, account, nil)
}
