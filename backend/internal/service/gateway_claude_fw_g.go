package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type claudeFWGIngressSnapshotContextKey struct{}

// WithOfficialClaudeIngressRuntime 在任何 Body 解压前冻结 Claude 入站 wire。
// Runtime 仍会在账号选定后验证完整官方身份；这里只保存事实，不作信任判断。
func WithOfficialClaudeIngressRuntime(ctx context.Context, c *gin.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if _, captured := claudeFWGIngressSnapshotFromContext(ctx); captured {
		return ctx
	}
	snapshot := officialegress.ClaudeIngressSnapshot{}
	if c != nil && c.Request != nil {
		snapshot = officialegress.ClaudeIngressSnapshot{
			Captured:    true,
			RequestGzip: headerContainsToken(c.Request.Header, "content-encoding", "gzip"),
			Headers:     c.Request.Header.Clone(),
		}
	}
	return context.WithValue(ctx, claudeFWGIngressSnapshotContextKey{}, snapshot)
}

func claudeFWGIngressSnapshotFromContext(
	ctx context.Context,
) (officialegress.ClaudeIngressSnapshot, bool) {
	if ctx == nil {
		return officialegress.ClaudeIngressSnapshot{}, false
	}
	snapshot, ok := ctx.Value(claudeFWGIngressSnapshotContextKey{}).(officialegress.ClaudeIngressSnapshot)
	if !ok {
		return officialegress.ClaudeIngressSnapshot{}, false
	}
	snapshot.Headers = snapshot.Headers.Clone()
	return snapshot, true
}

func (s *GatewayService) shouldRouteClaudeFWGCandidate(c *gin.Context, account *Account) bool {
	if s == nil || s.cfg == nil || !s.cfg.Gateway.ClaudeFWGCandidateEnabled ||
		c == nil || c.Request == nil || c.Request.URL == nil || account == nil {
		return false
	}
	return account.Platform == PlatformAnthropic && account.Type == AccountTypeOAuth &&
		c.Request.Method == http.MethodPost && c.Request.URL.Path == "/v1/messages"
}

func (s *GatewayService) shouldRouteClaudeFWGCountTokens(c *gin.Context, account *Account) bool {
	if s == nil || s.cfg == nil || !s.cfg.Gateway.ClaudeFWGCandidateEnabled ||
		c == nil || c.Request == nil || c.Request.URL == nil || account == nil {
		return false
	}
	path := c.Request.URL.Path
	return account.Platform == PlatformAnthropic && account.Type == AccountTypeOAuth &&
		c.Request.Method == http.MethodPost &&
		(path == "/v1/messages/count_tokens" || path == "/messages/count_tokens")
}

func (s *GatewayService) forwardClaudeFWGCountTokens(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
) error {
	if c == nil || c.Request == nil || c.Request.URL == nil {
		return errors.New("Claude FW-G count_tokens 缺少有效入口")
	}
	if c.Request.URL.RawQuery != "" {
		s.countTokensError(
			c,
			http.StatusBadRequest,
			"invalid_request_error",
			"Request query is outside the Claude Code SupportEnvelope",
		)
		return errors.New("Claude FW-G count_tokens query 不在 SupportEnvelope")
	}
	if s.officialEgress == nil || s.officialEgress.ClaudeCandidate == nil {
		return errors.New("Claude FW-G count_tokens route 已开启但 runtime 未注入")
	}
	if account == nil || parsed == nil || parsed.Body == nil {
		return errors.New("Claude FW-G count_tokens 缺少入口、账号或请求体")
	}
	if account.IsCustomBaseURLEnabled() && strings.TrimSpace(account.GetCustomBaseURL()) != "" {
		return errors.New("Claude FW-G count_tokens 不允许自定义上游地址")
	}
	trusted, err := buildClaudeFWGTrustedFacts(c, account, parsed)
	if err != nil {
		return err
	}
	trusted.Entrypoint.IngressProtocol = "managed-internal"
	accessToken, tokenType, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return err
	}
	if tokenType != "oauth" || strings.TrimSpace(accessToken) == "" {
		return errors.New("Claude FW-G count_tokens 只接受 OAuth access token")
	}
	proxyURL := ""
	if account.ProxyID != nil {
		if account.Proxy == nil {
			return errors.New("Claude FW-G count_tokens 账号代理未解析，禁止静默直连")
		}
		proxyURL = account.Proxy.URL()
	}
	executionContext := withClaudeCandidateHTTPTransport(
		ctx, proxyURL, account.ID, claudeFWGConcurrencyLimit(account),
	)
	ingress, _ := claudeFWGIngressSnapshotFromContext(c.Request.Context())
	result, err := s.officialEgress.ClaudeCandidate.ExecuteCountTokens(
		executionContext,
		officialegress.ClaudeEndpointExecution{
			Body: parsed.Body.Bytes(), AccessToken: accessToken,
			TrustedFacts: trusted, Ingress: ingress, InvocationID: uuid.NewString(),
		},
	)
	if err != nil {
		return fmt.Errorf("Claude FW-G count_tokens 执行失败：%w", err)
	}
	if result.Response == nil || result.Response.Body == nil {
		return errors.New("Claude FW-G count_tokens 返回空响应")
	}
	defer func() { _ = result.Response.Body.Close() }()
	if err := parsed.ReplaceBody(result.WireBody); err != nil {
		return fmt.Errorf("记录 Claude FW-G count_tokens final wire：%w", err)
	}
	responseBody, err := ReadUpstreamResponseBody(
		result.Response.Body,
		s.cfg,
		c,
		func(c *gin.Context) {
			s.countTokensError(c, http.StatusBadGateway, "upstream_error", "Upstream response too large")
		},
	)
	if err != nil {
		if !errors.Is(err, ErrUpstreamResponseBodyTooLarge) {
			s.countTokensError(c, http.StatusBadGateway, "upstream_error", "Failed to read response")
		}
		return err
	}
	if result.Response.StatusCode >= http.StatusBadRequest {
		if s.rateLimitService != nil {
			s.rateLimitService.HandleUpstreamError(
				ctx, account, result.Response.StatusCode, result.Response.Header, responseBody,
			)
		}
		message := sanitizeUpstreamErrorMessage(extractUpstreamErrorMessage(responseBody))
		setOpsUpstreamError(c, result.Response.StatusCode, message, "")
		s.countTokensError(
			c, result.Response.StatusCode, "upstream_error", "Upstream request failed",
		)
		return fmt.Errorf("Claude FW-G count_tokens 上游状态 %d", result.Response.StatusCode)
	}
	writeAnthropicPassthroughResponseHeaders(
		c.Writer.Header(), result.Response.Header, s.responseHeaderFilter,
	)
	contentType := strings.TrimSpace(result.Response.Header.Get("Content-Type"))
	if contentType == "" {
		contentType = "application/json"
	}
	c.Data(result.Response.StatusCode, contentType, responseBody)
	return nil
}

func (s *GatewayService) forwardClaudeFWGCandidate(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
	startTime time.Time,
) (*ForwardResult, error) {
	if c == nil || c.Request == nil || c.Request.URL == nil {
		return nil, errors.New("Claude FW-G candidate 缺少有效入口")
	}
	if c.Request.URL.RawQuery != "" {
		writeAnthropicError(
			c,
			http.StatusBadRequest,
			"invalid_request_error",
			"Request query is outside the Claude Code SupportEnvelope",
		)
		return nil, errors.New("Claude FW-G messages query 不在 SupportEnvelope")
	}
	if s.officialEgress == nil || s.officialEgress.ClaudeCandidate == nil {
		return nil, errors.New("Claude FW-G candidate route 已开启但 runtime 未注入")
	}
	if account.IsCustomBaseURLEnabled() && strings.TrimSpace(account.GetCustomBaseURL()) != "" {
		return nil, errors.New("Claude FW-G candidate 不允许自定义上游地址")
	}
	trusted, err := buildClaudeFWGTrustedFacts(c, account, parsed)
	if err != nil {
		return nil, err
	}
	accessToken, tokenType, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return nil, err
	}
	if tokenType != "oauth" || strings.TrimSpace(accessToken) == "" {
		return nil, errors.New("Claude FW-G candidate 只接受 OAuth access token")
	}
	proxyURL := ""
	if account.ProxyID != nil {
		if account.Proxy == nil {
			return nil, errors.New("Claude FW-G candidate 账号代理未解析，禁止静默直连")
		}
		proxyURL = account.Proxy.URL()
	}
	executionContext := withClaudeCandidateHTTPTransport(
		ctx,
		proxyURL,
		account.ID,
		claudeFWGConcurrencyLimit(account),
	)
	ingress, _ := claudeFWGIngressSnapshotFromContext(c.Request.Context())
	result, err := s.officialEgress.ClaudeCandidate.ExecuteMessages(
		executionContext,
		officialegress.ClaudeMessagesExecution{
			Body:         parsed.Body.Bytes(),
			AccessToken:  accessToken,
			TrustedFacts: trusted,
			Ingress:      ingress,
			InvocationID: uuid.NewString(),
		},
	)
	if err != nil {
		if officialegress.IsClaudeSupportEnvelopeRejection(err) && !c.Writer.Written() {
			writeAnthropicError(
				c,
				http.StatusBadRequest,
				"invalid_request_error",
				"Request is outside the Claude Code SupportEnvelope",
			)
		}
		return nil, fmt.Errorf("Claude FW-G candidate 执行失败：%w", err)
	}
	return s.finishClaudeFWGCandidateResponse(ctx, c, account, parsed, result, startTime)
}

func buildClaudeFWGTrustedFacts(
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
) (officialegress.ClaudeTrustedFacts, error) {
	if c == nil || c.Request == nil || account == nil || parsed == nil || parsed.Body == nil {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude FW-G Planner 缺少入口或账号")
	}
	apiKeyID := getAPIKeyIDFromContext(c)
	if apiKeyID <= 0 {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude FW-G Planner 缺少已认证 API Key binding")
	}
	accountUUID := strings.TrimSpace(account.GetExtraString("account_uuid"))
	if _, err := uuid.Parse(accountUUID); err != nil {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude FW-G Planner 缺少有效 account_uuid")
	}
	firstUserText := claudeFWGFirstUserText(parsed.Body.Bytes())
	if firstUserText == "" {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude FW-G Planner 缺少可信会话锚点")
	}
	sessionContext := parsed.SessionContext
	if sessionContext == nil {
		sessionContext = &SessionContext{
			ClientIP: c.ClientIP(), UserAgent: c.GetHeader("User-Agent"), APIKeyID: apiKeyID,
		}
	}
	if sessionContext.APIKeyID != 0 && sessionContext.APIKeyID != apiKeyID {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude FW-G Planner 的 API Key 会话绑定冲突")
	}
	sessionContext.APIKeyID = apiKeyID
	sessionSeed := buildStableSessionSeed(
		account.ID,
		"claude-fw-g:"+sessionContextDiscriminator(sessionContext),
		firstUserText,
	)

	trusted := officialegress.ClaudeTrustedFacts{
		Account: officialegress.ClaudeTrustedAccountFacts{
			AccountScope: "anthropic-oauth-account:" + strconv.FormatInt(account.ID, 10),
			AccountUUID:  accountUUID,
		},
		Session: officialegress.ClaudeTrustedSessionFacts{
			SessionID: generateSessionUUID(sessionSeed),
			Source:    officialegress.ClaudeSessionSourcePlannerDerived,
		},
		Entrypoint: officialegress.ClaudeTrustedEntrypointFacts{
			Entrypoint:       officialegress.ClaudeEntrypointSDKCLI,
			IngressProtocol:  "anthropic-messages",
			IngressBindingID: "api-key:" + strconv.FormatInt(apiKeyID, 10),
		},
		Features: officialegress.ClaudeTrustedFeatureFacts{
			SystemMode: officialegress.ClaudeSystemDefault,
		},
	}
	return trusted, nil
}

func claudeFWGFirstUserText(body []byte) string {
	var document struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(body, &document) != nil {
		return ""
	}
	for _, message := range document.Messages {
		if message.Role != "user" {
			continue
		}
		var text string
		if json.Unmarshal(message.Content, &text) == nil {
			if claudeFWGRealUserText(text) {
				return text
			}
			continue
		}
		var blocks []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if json.Unmarshal(message.Content, &blocks) != nil {
			return ""
		}
		for _, block := range blocks {
			if block.Type == "text" && claudeFWGRealUserText(block.Text) {
				return block.Text
			}
		}
	}
	return ""
}

func claudeFWGRealUserText(value string) bool {
	trimmed := strings.TrimSpace(value)
	return trimmed != "" && !strings.HasPrefix(trimmed, "<system-reminder>")
}

func claudeFWGConcurrencyLimit(account *Account) int {
	if account == nil || account.Concurrency <= 0 {
		return 1
	}
	return account.Concurrency
}

func (s *GatewayService) finishClaudeFWGCandidateResponse(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
	result officialegress.ClaudeCandidateResult,
	startTime time.Time,
) (*ForwardResult, error) {
	sessionResult := &result
	sessionFinalized := false
	defer func() {
		if !sessionFinalized {
			_ = sessionResult.FinalizeSession(false)
		}
	}()
	resp := result.Response
	if resp == nil || resp.Body == nil {
		return nil, errors.New("Claude FW-G candidate 返回空响应")
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode >= http.StatusBadRequest {
		if s.shouldFailoverUpstreamError(resp.StatusCode) {
			respBody, _ := s.readUpstreamErrorBody(resp)
			resp.Body = io.NopCloser(bytes.NewReader(respBody))
			s.handleRetryExhaustedSideEffects(ctx, resp, account)
			return nil, &UpstreamFailoverError{
				StatusCode:             resp.StatusCode,
				ResponseBody:           respBody,
				RetryableOnSameAccount: account.IsPoolMode() && account.IsPoolModeRetryableStatus(resp.StatusCode),
			}
		}
		return s.handleErrorResponse(ctx, resp, c, account, result.Model)
	}
	originalModel := parsed.Model
	clientStream := parsed.Stream
	if err := parsed.ReplaceBody(result.WireBody); err != nil {
		return nil, fmt.Errorf("记录 Claude FW-G final wire：%w", err)
	}
	if parsed.OnUpstreamAccepted != nil {
		parsed.OnUpstreamAccepted()
	}

	var usage *ClaudeUsage
	var firstTokenMS *int
	var clientDisconnect bool
	var err error
	if result.Stream {
		streamResult, streamErr := s.handleStreamingResponse(
			ctx, resp, c, account, startTime, originalModel, result.Model, false,
		)
		if streamErr != nil {
			var failoverErr *UpstreamFailoverError
			if result.StreamFallback != nil && errors.As(streamErr, &failoverErr) && !c.Writer.Written() {
				fallback, fallbackErr := result.StreamFallback(ctx)
				if fallbackErr != nil {
					return nil, fallbackErr
				}
				if fallback.Response == nil || fallback.Response.Body == nil {
					return nil, errors.New("Claude stream fallback 返回空响应")
				}
				defer func() { _ = fallback.Response.Body.Close() }()
				if fallback.Response.StatusCode < http.StatusOK ||
					fallback.Response.StatusCode >= http.StatusMultipleChoices {
					responseBody, _ := s.readUpstreamErrorBody(fallback.Response)
					return nil, &UpstreamFailoverError{
						StatusCode: fallback.Response.StatusCode, ResponseBody: responseBody,
						RetryableOnSameAccount: false,
					}
				}
				if err := parsed.ReplaceBody(fallback.WireBody); err != nil {
					return nil, fmt.Errorf("记录 Claude FW-G stream fallback wire：%w", err)
				}
				usage, firstTokenMS, clientDisconnect, err = s.bridgeClaudeFWGJSONToSSE(
					ctx, fallback.Response, c, account, startTime, originalModel, fallback.Model,
				)
				if err != nil {
					return nil, err
				}
				result.Model = fallback.Model
				result.Attempts = fallback.Attempts
				sessionResult = &fallback
				streamErr = nil
			}
		}
		if streamErr != nil {
			if partial := partialStreamUsageResult(
				c, resp, streamResult, originalModel, result.Model, startTime, streamErr,
			); partial != nil {
				return partial, streamErr
			}
			return nil, streamErr
		}
		if usage == nil {
			usage = streamResult.usage
			firstTokenMS = streamResult.firstTokenMs
			clientDisconnect = streamResult.clientDisconnect
		}
	} else if clientStream {
		usage, firstTokenMS, clientDisconnect, err = s.bridgeClaudeFWGJSONToSSE(
			ctx, resp, c, account, startTime, originalModel, result.Model,
		)
		if err != nil {
			return nil, err
		}
	} else {
		usage, err = s.handleNonStreamingResponse(ctx, resp, c, account, originalModel, result.Model)
		if err != nil {
			return nil, err
		}
	}
	if err := sessionResult.FinalizeSession(true); err != nil {
		return nil, fmt.Errorf("提交 Claude FW-G 会话状态：%w", err)
	}
	sessionFinalized = true
	return &ForwardResult{
		RequestID:                     resp.Header.Get("x-request-id"),
		Usage:                         *usage,
		Model:                         originalModel,
		UpstreamModel:                 result.Model,
		UpstreamResponseModel:         observedUpstreamResponseModel(c),
		UpstreamResponseModelConflict: observedUpstreamResponseModelConflict(c),
		Stream:                        clientStream,
		Duration:                      time.Since(startTime),
		FirstTokenMs:                  firstTokenMS,
		ClientDisconnect:              clientDisconnect,
	}, nil
}

func (s *GatewayService) bridgeClaudeFWGJSONToSSE(
	ctx context.Context,
	resp *http.Response,
	c *gin.Context,
	account *Account,
	startTime time.Time,
	originalModel string,
	mappedModel string,
) (*ClaudeUsage, *int, bool, error) {
	s.rateLimitService.UpdateSessionWindow(ctx, account, resp.Header)
	body, err := ReadUpstreamResponseBody(resp.Body, s.cfg, c, anthropicTooLargeError)
	if err != nil {
		return nil, nil, false, err
	}
	var message struct {
		ID           string            `json:"id"`
		Type         string            `json:"type"`
		Role         string            `json:"role"`
		Model        string            `json:"model"`
		Content      []json.RawMessage `json:"content"`
		StopReason   string            `json:"stop_reason"`
		StopSequence *string           `json:"stop_sequence"`
		Usage        json.RawMessage   `json:"usage"`
	}
	if err := json.Unmarshal(body, &message); err != nil || message.ID == "" || message.Model == "" {
		return nil, nil, false, errors.New("Claude stream fallback 返回的 JSON message 非法")
	}
	if originalModel != mappedModel {
		message.Model = originalModel
	}
	var usage ClaudeUsage
	if json.Unmarshal(message.Usage, &usage) != nil {
		return nil, nil, false, errors.New("Claude stream fallback usage 非法")
	}
	var startUsage map[string]any
	if json.Unmarshal(message.Usage, &startUsage) != nil {
		return nil, nil, false, errors.New("Claude stream fallback usage 不是对象")
	}
	startUsage["output_tokens"] = 0
	observer := upstreamResponseModelObserverFromContext(c)
	if observer == nil {
		observer = beginUpstreamResponseModelObservation(c)
	}
	observer.ObserveAnthropic(body)

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	if requestID := resp.Header.Get("x-request-id"); requestID != "" {
		c.Header("x-request-id", requestID)
	}
	flusher, ok := c.Writer.(http.Flusher)
	if !ok {
		return nil, nil, false, errors.New("Claude stream fallback 的客户端不支持 flush")
	}
	writeEvent := func(name string, value any) error {
		payload, marshalErr := json.Marshal(value)
		if marshalErr != nil {
			return marshalErr
		}
		payload = reverseToolNamesIfPresent(c, payload)
		if _, writeErr := fmt.Fprintf(c.Writer, "event: %s\ndata: %s\n\n", name, payload); writeErr != nil {
			return writeErr
		}
		flusher.Flush()
		return nil
	}
	if err := writeEvent("message_start", map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id": message.ID, "type": "message", "role": message.Role,
			"content": []any{}, "model": message.Model, "stop_reason": nil,
			"stop_sequence": nil, "usage": startUsage,
		},
	}); err != nil {
		return &usage, nil, true, err
	}
	firstTokenMS := int(time.Since(startTime).Milliseconds())
	for index, raw := range message.Content {
		if err := bridgeClaudeFWGContentBlock(writeEvent, index, raw); err != nil {
			return &usage, &firstTokenMS, true, err
		}
	}
	if err := writeEvent("message_delta", map[string]any{
		"type": "message_delta",
		"delta": map[string]any{
			"stop_reason": message.StopReason, "stop_sequence": message.StopSequence,
		},
		"usage": map[string]int{"output_tokens": usage.OutputTokens},
	}); err != nil {
		return &usage, &firstTokenMS, true, err
	}
	if err := writeEvent("message_stop", map[string]string{"type": "message_stop"}); err != nil {
		return &usage, &firstTokenMS, true, err
	}
	return &usage, &firstTokenMS, false, nil
}

func bridgeClaudeFWGContentBlock(
	writeEvent func(string, any) error,
	index int,
	raw json.RawMessage,
) error {
	var block map[string]any
	if json.Unmarshal(raw, &block) != nil {
		return errors.New("Claude stream fallback content block 非法")
	}
	kind, _ := block["type"].(string)
	start := make(map[string]any, len(block))
	for key, value := range block {
		start[key] = value
	}
	var delta map[string]any
	switch kind {
	case "text":
		text, _ := block["text"].(string)
		start["text"] = ""
		delta = map[string]any{"type": "text_delta", "text": text}
	case "thinking":
		thinking, _ := block["thinking"].(string)
		start["thinking"] = ""
		delete(start, "signature")
		delta = map[string]any{"type": "thinking_delta", "thinking": thinking}
	case "tool_use", "server_tool_use":
		input := block["input"]
		start["input"] = map[string]any{}
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		delta = map[string]any{"type": "input_json_delta", "partial_json": string(encoded)}
	default:
		delta = nil
	}
	if err := writeEvent("content_block_start", map[string]any{
		"type": "content_block_start", "index": index, "content_block": start,
	}); err != nil {
		return err
	}
	if delta != nil {
		if err := writeEvent("content_block_delta", map[string]any{
			"type": "content_block_delta", "index": index, "delta": delta,
		}); err != nil {
			return err
		}
	}
	return writeEvent("content_block_stop", map[string]any{
		"type": "content_block_stop", "index": index,
	})
}
