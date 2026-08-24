package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

const (
	chatgptCodexAlphaSearchURL   = "https://chatgpt.com/backend-api/codex/alpha/search"
	openAIPlatformAlphaSearchURL = "https://api.openai.com/v1/alpha/search"
)

// ForwardAlphaSearch proxies Codex standalone web search without binding the
// evolving alpha request or response schema.
//
// 返回值约定：仅当上游返回 2xx（一次真实成功的搜索）时返回非 nil 的
// *OpenAIForwardResult（WebSearchCalls=1，供按次计费）；上游错误被原样透传
// 给客户端时返回 (nil, nil)，不产生计费。
func (s *OpenAIGatewayService) ForwardAlphaSearch(ctx context.Context, c *gin.Context, account *Account, body []byte) (*OpenAIForwardResult, error) {
	if s == nil || c == nil || account == nil {
		return nil, fmt.Errorf("service, context, and account are required")
	}
	if _, err := s.prepareCodexAccountIdentitySource(ctx, c, account); err != nil {
		return nil, err
	}
	modelResult := gjson.GetBytes(body, "model")
	requestedModel := strings.TrimSpace(modelResult.String())
	if modelResult.Type != gjson.String || requestedModel == "" {
		return nil, fmt.Errorf("model is required")
	}

	upstreamModel := normalizeOpenAIModelForUpstream(account, account.GetMappedModel(requestedModel))
	if upstreamModel != "" && upstreamModel != requestedModel {
		body = ReplaceModelInBody(body, upstreamModel)
	}
	var sanitizedBody []byte
	var err error
	if account.IsOpenAIOAuth() {
		runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
		if runtimeErr != nil {
			return nil, runtimeErr
		}
		mode := string(runtimeState.CodexReleaseMode)
		ctx, err = bindOfficialCodexRuntimeStateFromIngress(
			ctx,
			c,
			account,
			mode,
			codexEndpointID(officialCodexEndpointAlphaSearch),
		)
		if err != nil {
			return nil, fmt.Errorf("resolve alpha search runtime state: %w", err)
		}
		sanitizedBody, err = sanitizeOpenAIAlphaSearchBody(mode, body)
	} else {
		// API-key 自定义上游不受内置 OAuth 画像的封闭字段集约束，保持原有仅剔除
		// 已知 Responses 专用字段的行为。
		sanitizedBody, err = sanitizeOpenAIAPIKeyAlphaSearchBody(body)
	}
	if err != nil {
		return nil, fmt.Errorf("sanitize alpha search request body: %w", err)
	}
	body = sanitizedBody

	token, _, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return nil, err
	}

	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	if err := s.ensureOpenAIAlphaSearchAuthMetadata(ctx, account, token, proxyURL); err != nil {
		return nil, err
	}
	SetOpsUpstreamModel(c, upstreamModel)

	// Codex Personal Access Token（at-...）目前可访问 ChatGPT Codex
	// /responses，但会被 standalone /alpha/search 的 access enforcement
	// 拒绝为 no_matching_rule。对 PAT 账号使用等价的 hosted web_search
	// Responses 路径兜底，避免把可用账号误判为搜索不可用。
	if account.IsOpenAIPersonalAccessToken() {
		return s.forwardAlphaSearchViaResponsesWebSearch(ctx, c, account, body, token, proxyURL, requestedModel, upstreamModel)
	}
	ctx, err = bindOfficialEgressSink(ctx, officialEgressSinkAlphaSearchDirect)
	if err != nil {
		return nil, fmt.Errorf("bind alpha search official egress sink: %w", err)
	}

	req, err := s.buildOpenAIAlphaSearchRequest(ctx, c, account, body, token)
	if err != nil {
		return nil, err
	}

	upstreamStart := time.Now()
	// alpha/search 与 PAT 旁路都打官方域名，必须与业务请求同一 TLS 画像；
	// 走标准 Transport 会在同账号同 IP 上暴露不同形态的 ClientHello。
	var resp *http.Response
	if account.IsOpenAIOAuth() {
		runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
		if runtimeErr != nil {
			return nil, runtimeErr
		}
		attemptIdentity, _ := officialegress.AttemptIdentityFromContext(req.Context())
		invocation, invocationErr := newOfficialCodexHTTPInvocation(
			req.Context(),
			officialCodexHTTPInvocationInput{
				Runtime: runtimeState, Account: account,
				SinkID:       officialEgressSinkAlphaSearchDirect,
				InvocationID: attemptIdentity.InvocationID, ProxyURL: proxyURL,
				PolicyID:     "changeset3.alpha_search.direct",
				PolicySource: "service.ForwardAlphaSearch", AttemptBudget: 1,
			},
		)
		if invocationErr != nil {
			return nil, invocationErr
		}
		resp, err = invocation.Execute(req.Context(), officialCodexHTTPAttemptInput{
			EndpointID: officialCodexEndpointAlphaSearch, Request: req,
		})
	} else {
		tlsProfile := OpenAIOfficialEgressHTTPTLSProfile(false)
		resp, err = doOpenAIAPIKeyHTTPTransport(
			s.httpUpstream, req, proxyURL, account, tlsProfile,
		)
	}
	SetOpsLatencyMs(c, OpsUpstreamLatencyMsKey, time.Since(upstreamStart).Milliseconds())
	if err != nil {
		return nil, s.handleOpenAIUpstreamTransportError(ctx, c, account, err, true)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, err := ReadUpstreamResponseBody(resp.Body, s.cfg, c, openAITooLargeError)
	if err != nil {
		return nil, fmt.Errorf("read alpha search response: %w", err)
	}

	if resp.StatusCode >= http.StatusBadRequest {
		upstreamMessage := sanitizeUpstreamErrorMessage(strings.TrimSpace(extractUpstreamErrorMessage(respBody)))
		if s.shouldFailoverOpenAIUpstreamResponse(resp.StatusCode, upstreamMessage, respBody) ||
			isOpenAIAlphaSearchEndpointUnsupported(account, resp.StatusCode) {
			resp.Body = io.NopCloser(bytes.NewReader(respBody))
			// alpha/search 是独立的工具端点，单次 401 不能证明账号的模型调用
			// 凭据全局失效。若沿用通用 401 逻辑，PAT 会因没有 refresh_token
			// 被永久标记为 error；历史导入且缺少 auth_mode 标记的 at- token 也会
			// 漏过 PAT 类型判断。这里仍允许本次请求换号，但不修改任何账号状态；
			// 真正的凭据失效由普通 Responses 请求或 whoami 校验判定。
			shouldDisable := false
			if shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(resp.StatusCode) {
				shouldDisable = s.handleFailoverSideEffects(ctx, resp, account, respBody, openAIAlphaSearchSchedulingModel(account, requestedModel))
			}
			retryableOnSameAccount := !shouldDisable && account.IsPoolMode() && account.IsPoolModeRetryableStatus(resp.StatusCode)
			if account.IsOpenAIOAuthLike() && resp.StatusCode == http.StatusTooManyRequests {
				return nil, s.newOpenAIAccountFailoverError(account, resp.StatusCode, resp.Header, respBody, upstreamMessage, shouldDisable, retryableOnSameAccount)
			}
			if isOpenAIHTTPUpstreamAccessStateError(resp.StatusCode, upstreamMessage, respBody) {
				return nil, newOpenAIUpstreamFailoverError(resp.StatusCode, resp.Header, respBody, upstreamMessage, retryableOnSameAccount)
			}
			return nil, &UpstreamFailoverError{StatusCode: resp.StatusCode, ResponseBody: respBody, RetryableOnSameAccount: retryableOnSameAccount}
		}
	}

	if !account.IsShadow() {
		s.UpdateCodexUsageSnapshotFromHeaders(ctx, account.ID, resp.Header)
	}
	writeOpenAIPassthroughResponseHeaders(c.Writer.Header(), resp.Header, s.responseHeaderFilter)
	contentType := resp.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}
	c.Data(resp.StatusCode, contentType, respBody)
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		// 非 2xx（错误/重定向）已原样透传给客户端：不是一次成功的搜索，不计费。
		return nil, nil
	}
	return &OpenAIForwardResult{
		RequestID:      strings.TrimSpace(resp.Header.Get("x-request-id")),
		Model:          requestedModel,
		UpstreamModel:  upstreamModel,
		Duration:       time.Since(upstreamStart),
		WebSearchCalls: 1,
	}, nil
}

func (s *OpenAIGatewayService) forwardAlphaSearchViaResponsesWebSearch(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	alphaBody []byte,
	token string,
	proxyURL string,
	requestedModel string,
	upstreamModel string,
) (*OpenAIForwardResult, error) {
	if upstreamModel == "" {
		upstreamModel = requestedModel
	}
	boundCtx, bindErr := bindOfficialEgressSink(ctx, officialEgressSinkAlphaSearchPATFallback)
	if bindErr != nil {
		return nil, fmt.Errorf("bind alpha search PAT official egress sink: %w", bindErr)
	}
	ctx = boundCtx
	responsesBody, err := buildOpenAIAlphaSearchResponsesWebSearchBody(alphaBody, upstreamModel)
	if err != nil {
		return nil, err
	}
	req, err := s.buildOpenAIAlphaSearchResponsesWebSearchRequest(ctx, c, account, alphaBody, responsesBody, token)
	if err != nil {
		return nil, err
	}
	SetActualOpenAIUpstreamEndpoint(c, "/v1/responses")

	upstreamStart := time.Now()
	officialEgress, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if runtimeErr != nil {
		return nil, fmt.Errorf("Codex Executor is not configured: %w", runtimeErr)
	}
	resp, err := officialEgress.ExecuteCodexHTTP(ctx, OfficialCodexHTTPExecution{
		SinkID: officialEgressSinkAlphaSearchPATFallback, EndpointID: officialCodexEndpointResponsesHTTP,
		// PAT fallback 的 Responses body 是服务端重建产物，不能把原
		// alpha/search 入站误当成该 body 的完整官方 Responses 身份。
		Account: account, ProxyURL: proxyURL, Request: req,
		PolicyID: "changeset1b.alpha_search.pat_fallback.v1", PolicySource: "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md#policy-changeset-1b",
		ConcurrencyLimit: account.EffectiveLoadFactor(), HasBillingSideEffect: true,
	})
	SetOpsLatencyMs(c, OpsUpstreamLatencyMsKey, time.Since(upstreamStart).Milliseconds())
	if err != nil {
		return nil, s.handleOpenAIUpstreamTransportError(ctx, c, account, err, true)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, err := ReadUpstreamResponseBody(resp.Body, s.cfg, c, openAITooLargeError)
	if err != nil {
		return nil, fmt.Errorf("read alpha search responses fallback response: %w", err)
	}

	if resp.StatusCode >= http.StatusBadRequest {
		upstreamMessage := sanitizeUpstreamErrorMessage(strings.TrimSpace(extractUpstreamErrorMessage(respBody)))
		if s.shouldFailoverOpenAIUpstreamResponse(resp.StatusCode, upstreamMessage, respBody) {
			resp.Body = io.NopCloser(bytes.NewReader(respBody))
			// 仍按 alpha/search 工具请求处理：PAT 的工具链路失败不能直接永久置错。
			shouldDisable := false
			if shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(resp.StatusCode) {
				shouldDisable = s.handleFailoverSideEffects(ctx, resp, account, respBody, openAIAlphaSearchSchedulingModel(account, requestedModel))
			}
			retryableOnSameAccount := !shouldDisable && account.IsPoolMode() && account.IsPoolModeRetryableStatus(resp.StatusCode)
			if account.IsOpenAIOAuthLike() && resp.StatusCode == http.StatusTooManyRequests {
				return nil, s.newOpenAIAccountFailoverError(account, resp.StatusCode, resp.Header, respBody, upstreamMessage, shouldDisable, retryableOnSameAccount)
			}
			if isOpenAIHTTPUpstreamAccessStateError(resp.StatusCode, upstreamMessage, respBody) {
				return nil, newOpenAIUpstreamFailoverError(resp.StatusCode, resp.Header, respBody, upstreamMessage, retryableOnSameAccount)
			}
			return nil, &UpstreamFailoverError{StatusCode: resp.StatusCode, ResponseBody: respBody, RetryableOnSameAccount: retryableOnSameAccount}
		}
	}

	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		writeOpenAIPassthroughResponseHeaders(c.Writer.Header(), resp.Header, s.responseHeaderFilter)
		contentType := resp.Header.Get("Content-Type")
		if contentType == "" {
			contentType = "application/json"
		}
		c.Data(resp.StatusCode, contentType, respBody)
		return nil, nil
	}

	if !account.IsShadow() {
		s.UpdateCodexUsageSnapshotFromHeaders(ctx, account.ID, resp.Header)
	}
	alphaRespBody, err := openAIAlphaSearchResponseFromResponsesSSE(respBody)
	if err != nil {
		return nil, err
	}
	c.Data(http.StatusOK, "application/json", alphaRespBody)
	return &OpenAIForwardResult{
		RequestID:        strings.TrimSpace(resp.Header.Get("x-request-id")),
		Model:            requestedModel,
		UpstreamModel:    upstreamModel,
		UpstreamEndpoint: "/v1/responses",
		ResponseHeaders:  resp.Header.Clone(),
		Duration:         time.Since(upstreamStart),
		WebSearchCalls:   1,
	}, nil
}

func openAIAlphaSearchSchedulingModel(account *Account, requestedModel string) string {
	return canonicalOpenAIAccountSchedulingModel(account, requestedModel)
}

func (s *OpenAIGatewayService) buildOpenAIAlphaSearchResponsesWebSearchRequest(ctx context.Context, c *gin.Context, account *Account, alphaBody []byte, body []byte, token string) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, chatgptCodexURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req = req.WithContext(WithHTTPUpstreamProfile(req.Context(), HTTPUpstreamProfileOpenAI))

	authHeaders, err := s.buildOpenAIAuthenticationHeaders(ctx, account, token)
	if err != nil {
		return nil, fmt.Errorf("build openai authentication headers: %w", err)
	}
	for key, values := range authHeaders {
		for _, value := range values {
			req.Header.Add(key, value)
		}
	}
	req.Host = "chatgpt.com"
	if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, req.Header, account); err != nil {
		return nil, fmt.Errorf("resolve chatgpt account headers: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Del("OpenAI-Beta")
	// PAT fallback 是独立的 Responses invocation，不得复用 alpha/search 入站
	// turn metadata；Executor 会从本 invocation 的结构化事实生成新会话与轮次。
	applyOpenAICodexAuxiliaryHeaders(req.Header)
	apiKeyID := getAPIKeyIDFromContext(c)
	if sessionID := strings.TrimSpace(gjson.GetBytes(alphaBody, "id").String()); sessionID != "" {
		isolated := isolateOpenAISessionID(apiKeyID, sessionID)
		// 官方会话头一律连字符小写，且没有 conversation-id——thread-id 才是其对应物
		// （codex-api/src/requests/headers.rs:8,11）。规格表 SPEC-HDR-007。
		setHeaderRaw(req.Header, "session-id", isolated)
		setHeaderRaw(req.Header, "thread-id", isolated)
	}
	applyCodexAccountIdentityHeaders(req.Header, codexAccountIdentitySource(c, account), apiKeyID)
	enforceCodexIdentityHeadersWithUA(req.Header, s.codexIdentityOverrideUA(account))
	account.ApplyHeaderOverrides(req.Header)
	return req, nil
}

func buildOpenAIAlphaSearchResponsesWebSearchBody(alphaBody []byte, model string) ([]byte, error) {
	if strings.TrimSpace(model) == "" {
		return nil, fmt.Errorf("model is required")
	}
	tool := map[string]any{"type": "web_search"}
	if contextSize := strings.TrimSpace(gjson.GetBytes(alphaBody, "settings.search_context_size").String()); contextSize != "" {
		tool["search_context_size"] = contextSize
	}
	if userLocation := gjson.GetBytes(alphaBody, "settings.user_location"); userLocation.IsObject() {
		var loc map[string]any
		if err := json.Unmarshal([]byte(userLocation.Raw), &loc); err == nil && len(loc) > 0 {
			tool["user_location"] = loc
		}
	}
	payload := map[string]any{
		"model":               model,
		"tool_choice":         "auto",
		"parallel_tool_calls": false,
		"reasoning":           map[string]any{},
		"store":               false,
		"stream":              true,
		"include":             []any{"reasoning.encrypted_content"},
		"input": []any{
			map[string]any{
				"role": "user",
				"content": []any{
					map[string]any{
						"type": "input_text",
						"text": openAIAlphaSearchResponsesWebSearchPrompt(alphaBody),
					},
				},
			},
		},
		"tools": []any{tool},
	}
	return json.Marshal(payload)
}

func openAIAlphaSearchResponsesWebSearchPrompt(alphaBody []byte) string {
	var b strings.Builder
	_, _ = b.WriteString("Execute this Codex standalone web.run request for another model.\n")
	_, _ = b.WriteString("Use the hosted web_search tool when web/current information is needed.\n")
	_, _ = b.WriteString("Return concise source-backed results. Include titles, URLs, dates, and direct answers when available.\n")
	if commands := strings.TrimSpace(gjson.GetBytes(alphaBody, "commands").Raw); commands != "" {
		_, _ = b.WriteString("\nCommands JSON:\n")
		_, _ = b.WriteString(truncateOpenAIAlphaSearchPromptJSON(commands, 12000))
	}
	if settings := strings.TrimSpace(gjson.GetBytes(alphaBody, "settings").Raw); settings != "" {
		_, _ = b.WriteString("\n\nSearch settings JSON:\n")
		_, _ = b.WriteString(truncateOpenAIAlphaSearchPromptJSON(settings, 4000))
	}
	if input := strings.TrimSpace(gjson.GetBytes(alphaBody, "input").Raw); input != "" {
		_, _ = b.WriteString("\n\nRecent conversation/input JSON:\n")
		_, _ = b.WriteString(truncateOpenAIAlphaSearchPromptJSON(input, 8000))
	}
	if b.Len() == 0 {
		return "Execute the requested web search and return concise source-backed results."
	}
	return b.String()
}

func truncateOpenAIAlphaSearchPromptJSON(value string, limit int) string {
	value = strings.TrimSpace(value)
	if limit <= 0 || len(value) <= limit {
		return value
	}
	return value[:limit] + "\n...<truncated>"
}

func (s *OpenAIGatewayService) buildOpenAIAlphaSearchRequest(ctx context.Context, c *gin.Context, account *Account, body []byte, token string) (*http.Request, error) {
	targetURL := ""
	method := http.MethodPost
	var endpointProfile officialCodexEndpointProfile
	var err error
	mode := ""
	if account.IsOpenAIOAuth() {
		runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
		if runtimeErr != nil {
			return nil, runtimeErr
		}
		mode = string(runtimeState.CodexReleaseMode)
		endpointProfile, err = resolveCodexEndpointForMode(
			mode,
			codexEndpointID(officialCodexEndpointAlphaSearch),
		)
		if err != nil {
			return nil, fmt.Errorf("resolve alpha search endpoint profile: %w", err)
		}
		profileURL, profileErr := buildCodexEndpointURLForMode(
			mode,
			codexEndpointID(endpointProfile.ID),
			officialCodexEndpointURLInput{},
		)
		if profileErr != nil {
			return nil, fmt.Errorf("resolve alpha search endpoint URL: %w", profileErr)
		}
		targetURL = profileURL.String()
		method = endpointProfile.Method
		ctx = s.bindOpenAICookieJar(ctx, account)
	} else {
		targetURL, err = s.openAIAlphaSearchURL(account)
		if err != nil {
			return nil, err
		}
	}
	parsedURL, err := url.Parse(targetURL)
	if err != nil {
		return nil, fmt.Errorf("parse alpha search URL: %w", err)
	}
	if account.IsOpenAIOAuth() {
		if _, exists := OfficialEgressContextFromContext(ctx); !exists {
			runtimeState, runtimeErr := resolveBoundOrIngressOfficialCodexRuntimeState(
				ctx, c, account, mode, codexEndpointID(officialCodexEndpointAlphaSearch),
			)
			if runtimeErr != nil {
				return nil, fmt.Errorf("resolve alpha search semantic runtime: %w", runtimeErr)
			}
			invocationID, invocationErr := officialEgressInvocationIDForRequest(c)
			if invocationErr != nil {
				return nil, invocationErr
			}
			proxyID := int64(0)
			if account.ProxyID != nil {
				proxyID = *account.ProxyID
			}
			egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
				AccountID: account.ID, TargetPlatform: PlatformOpenAI,
				InboundEndpoint: c.Request.URL.Path, Transport: OfficialEgressTransportHTTP,
				UpstreamHost: parsedURL.Host, ProfileVersion: officialCodexProfileVersion,
				ProfileMode: mode, AccountType: account.Type, ProxyID: proxyID,
				CodexEndpointID: officialCodexEndpointAlphaSearch,
				InvocationID:    invocationID, CodexRuntimeState: runtimeState,
			})
			ctx = WithOfficialEgressContext(ctx, egressContext)
		}
	}
	// OAuth 路径不透传入站 query：官方 provider 的 query_params 在所有构造点均为 None
	// （model-provider-info/src/lib.rs:339,384,534），alpha/search 的 URL 不带任何
	// 参数。原先无条件透传等于让第三方客户端往打给官方的 URL 上注入任意 query，
	// 与 P0-4 的未知顶层字段泄漏同类，只是发生在 URL 层。规格表 SPEC-EP-013。
	//
	// API Key 路径打的是第三方 base URL，不受官方画像约束，保持原有透传行为。
	if c != nil && c.Request != nil && c.Request.URL != nil && account.Type != AccountTypeOAuth {
		query := parsedURL.Query()
		for key, values := range c.Request.URL.Query() {
			for _, value := range values {
				query.Add(key, value)
			}
		}
		parsedURL.RawQuery = query.Encode()
	}

	req, err := http.NewRequestWithContext(ctx, method, parsedURL.String(), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req = req.WithContext(WithHTTPUpstreamProfile(req.Context(), HTTPUpstreamProfileOpenAI))
	if account.IsOpenAIOAuth() {
		req.Host = endpointProfile.Host
	}

	authHeaders, err := s.buildOpenAIAuthenticationHeaders(ctx, account, token)
	if err != nil {
		return nil, fmt.Errorf("build openai authentication headers: %w", err)
	}
	for key, values := range authHeaders {
		for _, value := range values {
			req.Header.Add(key, value)
		}
	}

	req.Header.Set("Content-Type", "application/json")
	// 官方 alpha/search 走 execute，端点层不设 accept，由 reqwest 补默认 */*。
	req.Header.Set("Accept", "*/*")

	if account.Type == AccountTypeOAuth {
		if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, req.Header, account); err != nil {
			return nil, fmt.Errorf("resolve chatgpt account headers: %w", err)
		}

		turnMetadata := openAIAlphaSearchInboundHeader(c, "X-Codex-Turn-Metadata")
		if turnMetadata == "" {
			derived, deriveErr := deriveOfficialOpenAIHTTPIdentity(
				c,
				account,
				body,
				nil,
				mode,
			)
			if deriveErr != nil {
				return nil, fmt.Errorf("derive alpha search turn metadata: %w", deriveErr)
			}
			turnMetadata = derived.turnMetadata
		}
		req.Header.Set("X-Codex-Turn-Metadata", turnMetadata)
		applyCodexAccountIdentityHeaders(req.Header, codexAccountIdentitySource(c, account), getAPIKeyIDFromContext(c))
		canonical := resolveCodexOutboundIdentity("")
		if version := openAIAlphaSearchInboundHeader(c, "Version"); version != "" {
			req.Header.Set("Version", version)
		} else {
			req.Header.Set("Version", canonical.version)
		}
		if originator := openAIAlphaSearchInboundHeader(c, "Originator"); originator != "" {
			req.Header.Set("Originator", originator)
		} else {
			req.Header.Set("Originator", canonical.originator)
		}
		if customUA := account.GetOpenAIUserAgent(); customUA != "" {
			req.Header.Set("User-Agent", customUA)
		} else if userAgent := openAIAlphaSearchInboundHeader(c, "User-Agent"); userAgent != "" {
			req.Header.Set("User-Agent", userAgent)
		} else {
			req.Header.Set("User-Agent", canonical.userAgent)
		}
		if s.cfg != nil && s.cfg.Gateway.ForceCodexCLI {
			req.Header.Set("User-Agent", canonical.userAgent)
		}
		enforceCodexIdentityHeadersWithUA(req.Header, s.codexIdentityOverrideUA(account))
	}

	account.ApplyHeaderOverrides(req.Header)
	if !account.IsOpenAIOAuth() {
		stripOpenAIAlphaSearchResponsesHeaders(req.Header)
	}
	return req, nil
}

// stripOpenAIAlphaSearchResponsesHeaders 让独立搜索请求与官方 Codex
// SearchClient 的线协议保持一致。alpha/search 不是 /responses 的子请求：官方
// 客户端仅在 Provider/Auth 基础头之外附加 x-codex-turn-metadata，不发送
// OpenAI-Beta、会话隔离或 Responses Lite 状态头。originator 与 User-Agent
// 属于官方默认客户端头，必须保留。
//
// alpha/search 使用专用构造器生成官方 SearchClient 的最小线协议形态；
// 该函数作为最后一道防线，避免账号 header 覆写或后续改动重新带入
// Responses 专用头，使 PAT 的 alpha/search 被上游按错误认证路径处理。
func stripOpenAIAlphaSearchResponsesHeaders(headers http.Header) {
	if headers == nil {
		return
	}
	for _, key := range []string{
		"OpenAI-Beta",
		// 会话头两种写法都要删。官方形态是连字符（SPEC-HDR-007 起已统一），
		// 而 account_header_override.go 的覆写白名单里恰好收了 session-id /
		// thread-id——账号一旦配置覆写，就会在 ApplyHeaderOverrides 阶段被设上，
		// 落在本函数之前。只删下划线写法的话 Del 规范化成 "Session_Id"，
		// 与连字符的 "Session-Id" 不是同一个键，删不掉，这道防线等于失效。
		"session-id",
		"thread-id",
		// 下划线写法一并保留：openai_gateway_service.go 的调试头白名单仍列着它们，
		// 历史配置与第三方客户端也可能发。出站侧已无人再用下划线写法
		// （account_test_service.go 的探针已改为连字符，SPEC-HDR-007）。
		"Session_ID",
		"Conversation_ID",
		"X-Codex-Beta-Features",
		"X-Codex-Turn-State",
		responsesLiteHeaderKey,
	} {
		headers.Del(key)
	}
}

func openAIAlphaSearchInboundHeader(c *gin.Context, key string) string {
	if c == nil {
		return ""
	}
	return strings.TrimSpace(c.GetHeader(key))
}

var openAIAlphaSearchUnsupportedBodyFields = map[string]struct{}{
	// Codex alpha/search 是 SearchRequest 独立协议，不是 /responses 子请求。
	// 新版 Codex/第三方代理可能把 Responses 公共字段误带到搜索请求里；ChatGPT
	// alpha/search 会对这些字段返回 Unknown parameter（例如 prompt_cache_key）。
	"prompt_cache_key":       {},
	"prompt_cache_retention": {},
	"store":                  {},
}

// sanitizeOpenAIAlphaSearchBody 按当前 Release 的 alpha-search 画像执行严格闭包。
// 未知字段和 Responses 公共字段全部删除，六个必需字段缺一即失败，最终顺序固定为
// id, model, input, commands, settings, max_output_tokens。
func sanitizeOpenAIAlphaSearchBody(mode string, body []byte) ([]byte, error) {
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return nil, fmt.Errorf("decode alpha search body: %w", err)
	}
	return projectCodexEndpointJSONBodyForMode(
		mode,
		codexEndpointID(officialCodexEndpointAlphaSearch),
		payload,
		body,
		nil,
	)
}

// sanitizeOpenAIAPIKeyAlphaSearchBody 保留自定义 API-key 上游的既有宽松行为：
// 只删除已知会与 Responses 协议串线的字段，不应用内置 OAuth 的六字段闭包。
func sanitizeOpenAIAPIKeyAlphaSearchBody(body []byte) ([]byte, error) {
	if len(body) == 0 {
		return body, nil
	}
	parsed := gjson.ParseBytes(body)
	if !parsed.IsObject() {
		return body, nil
	}
	hasUnsupported := false
	parsed.ForEach(func(key, _ gjson.Result) bool {
		if _, bad := openAIAlphaSearchUnsupportedBodyFields[key.String()]; bad {
			hasUnsupported = true
			return false
		}
		return true
	})
	// 没有待删字段就原样返回原始字节——不重新序列化，键序与空白全部保持不变
	if !hasUnsupported {
		return body, nil
	}
	var buf bytes.Buffer
	_ = buf.WriteByte('{')
	first := true
	parsed.ForEach(func(key, value gjson.Result) bool {
		name := key.String()
		if _, bad := openAIAlphaSearchUnsupportedBodyFields[name]; bad {
			return true
		}
		if !first {
			_ = buf.WriteByte(',')
		}
		first = false
		encodedKey, err := json.Marshal(name)
		if err != nil {
			return true
		}
		_, _ = buf.Write(encodedKey)
		_ = buf.WriteByte(':')
		_, _ = buf.WriteString(value.Raw)
		return true
	})
	_ = buf.WriteByte('}')
	return buf.Bytes(), nil
}

func (s *OpenAIGatewayService) ensureOpenAIAlphaSearchAuthMetadata(ctx context.Context, account *Account, token string, proxyURL string) error {
	if s == nil || account == nil || !account.IsOpenAIPersonalAccessToken() {
		return nil
	}
	if strings.TrimSpace(account.GetChatGPTAccountID()) != "" {
		return nil
	}
	var oauthService *OpenAIOAuthService
	if s.openAITokenProvider != nil {
		oauthService = s.openAITokenProvider.openAIOAuthService
	}
	if oauthService == nil {
		return nil
	}
	tokenInfo, err := oauthService.ValidateCodexPersonalAccessToken(ctx, token, proxyURL)
	if err != nil {
		return fmt.Errorf("validate Codex PAT metadata for alpha/search: %w", err)
	}
	credentials := shallowCopyMap(account.Credentials)
	for key, value := range oauthService.BuildAccountCredentials(tokenInfo) {
		credentials[key] = value
	}
	credentials = NormalizeOpenAIPersonalAccessTokenCredentials(account, tokenInfo, credentials)
	account.Credentials = shallowCopyMap(credentials)
	if s.accountRepo != nil {
		if err := persistAccountCredentials(ctx, s.accountRepo, account, credentials); err != nil {
			return fmt.Errorf("persist Codex PAT metadata for alpha/search: %w", err)
		}
	}
	return nil
}

// isOpenAIAlphaSearchEndpointUnsupported 识别「API key 上游没有实现
// /v1/alpha/search 端点」的响应。404/405 不在通用 failover 状态集里（模型
// 调用中的 404 通常是用户请求问题），但对这个独立工具端点而言，它几乎只
// 意味着所选上游（官方平台或第三方中转）不提供该端点——应换号重试，而
// 不是把 404 透传给客户端，否则混合分组里 OAuth 账号明明可以承接搜索，
// 请求却可能死在先被选中的 API key 账号上。
func isOpenAIAlphaSearchEndpointUnsupported(account *Account, statusCode int) bool {
	if account == nil || account.Type != AccountTypeAPIKey {
		return false
	}
	return statusCode == http.StatusNotFound || statusCode == http.StatusMethodNotAllowed
}

func shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(statusCode int) bool {
	switch statusCode {
	case http.StatusUnauthorized, http.StatusNotFound, http.StatusMethodNotAllowed:
		// 401：工具端点的 access enforcement 不代表凭据全局失效；
		// 404/405：端点不存在只说明该上游不支持独立搜索，账号本身健康。
		// 两类都只换号，不写账号错误状态。
		return false
	default:
		return true
	}
}

func openAIAlphaSearchResponseFromResponsesSSE(body []byte) ([]byte, error) {
	output, results := parseOpenAIResponsesSSEForAlphaSearch(body)
	resp := map[string]any{
		"output": output,
	}
	if len(results) > 0 {
		resp["results"] = results
	}
	return json.Marshal(resp)
}

func parseOpenAIResponsesSSEForAlphaSearch(body []byte) (string, []any) {
	text := strings.ReplaceAll(string(body), "\r\n", "\n")
	var output strings.Builder
	var completedResponse any
	results := make([]any, 0)
	seenURLs := make(map[string]struct{})

	for _, block := range strings.Split(text, "\n\n") {
		data := openAIAlphaSearchSSEData(block)
		if data == "" || data == "[DONE]" {
			continue
		}
		var event map[string]any
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			continue
		}
		if delta, _ := event["delta"].(string); delta != "" && event["type"] == "response.output_text.delta" {
			_, _ = output.WriteString(delta)
		}
		if event["type"] == "response.completed" {
			completedResponse = event["response"]
		}
		collectOpenAIAlphaSearchURLCitations(event, &results, seenURLs)
	}

	out := output.String()
	if strings.TrimSpace(out) == "" && completedResponse != nil {
		out = extractOpenAIResponsesCompletedText(completedResponse)
		collectOpenAIAlphaSearchURLCitations(completedResponse, &results, seenURLs)
	}
	return out, results
}

func openAIAlphaSearchSSEData(block string) string {
	var lines []string
	for _, line := range strings.Split(block, "\n") {
		line = strings.TrimRight(line, "\r")
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		lines = append(lines, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func extractOpenAIResponsesCompletedText(response any) string {
	resp, ok := response.(map[string]any)
	if !ok {
		return ""
	}
	outputItems, _ := resp["output"].([]any)
	var b strings.Builder
	for _, item := range outputItems {
		itemMap, ok := item.(map[string]any)
		if !ok || itemMap["type"] != "message" {
			continue
		}
		contentItems, _ := itemMap["content"].([]any)
		for _, content := range contentItems {
			contentMap, ok := content.(map[string]any)
			if !ok {
				continue
			}
			if contentMap["type"] == "output_text" {
				if text, _ := contentMap["text"].(string); text != "" {
					_, _ = b.WriteString(text)
				}
			}
		}
	}
	return b.String()
}

func collectOpenAIAlphaSearchURLCitations(value any, results *[]any, seen map[string]struct{}) {
	switch typed := value.(type) {
	case map[string]any:
		if typed["type"] == "url_citation" {
			if urlValue, _ := typed["url"].(string); strings.TrimSpace(urlValue) != "" {
				urlValue = strings.TrimSpace(urlValue)
				if _, exists := seen[urlValue]; !exists {
					seen[urlValue] = struct{}{}
					result := map[string]any{
						"type":   "text_result",
						"ref_id": fmt.Sprintf("turn0search%d", len(*results)),
						"url":    urlValue,
					}
					if title, _ := typed["title"].(string); strings.TrimSpace(title) != "" {
						result["title"] = strings.TrimSpace(title)
					}
					*results = append(*results, result)
				}
			}
		}
		for _, child := range typed {
			collectOpenAIAlphaSearchURLCitations(child, results, seen)
		}
	case []any:
		for _, child := range typed {
			collectOpenAIAlphaSearchURLCitations(child, results, seen)
		}
	}
}

func (s *OpenAIGatewayService) openAIAlphaSearchURL(account *Account) (string, error) {
	if account == nil {
		return "", fmt.Errorf("account is required")
	}
	switch account.Type {
	case AccountTypeOAuth, AccountTypeSetupToken:
		return chatgptCodexAlphaSearchURL, nil
	case AccountTypeAPIKey:
		baseURL := account.GetOpenAIBaseURL()
		if baseURL == "" {
			return openAIPlatformAlphaSearchURL, nil
		}
		validatedURL, err := s.validateUpstreamBaseURL(baseURL)
		if err != nil {
			return "", err
		}
		return buildOpenAIEndpointURL(validatedURL, "/v1/alpha/search"), nil
	default:
		return "", fmt.Errorf("unsupported OpenAI account type: %s", account.Type)
	}
}
