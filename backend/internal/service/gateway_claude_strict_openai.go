package service

import (
	"bufio"
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
	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const claudeStrictSSEPreludeLimit = 1 << 20
const claudeStrictJSONResponseLimit = 64 << 20

func (s *GatewayService) shouldRouteClaudeStrictOpenAI(account *Account) bool {
	return account != nil &&
		account.Platform == PlatformAnthropic && account.Type == AccountTypeOAuth
}

// ShouldFailCloseClaudeStrictResponsesWebSocket 表示已选中的 Claude OAuth
// Persona 不接受有状态 Responses WebSocket。调用方必须在读取凭据或连接上游前关闭该入口。
func (s *GatewayService) ShouldFailCloseClaudeStrictResponsesWebSocket(account *Account) bool {
	return s.shouldRouteClaudeStrictOpenAI(account)
}

// ShouldFailCloseClaudeStrictResponsesWebSocket 为 OpenAI WebSocket 调度链提供同一门禁。
// WebSocket 不在 Claude lossless 适配闭集内，因此只能在凭据和上游调用前拒绝。
func (s *OpenAIGatewayService) ShouldFailCloseClaudeStrictResponsesWebSocket(account *Account) bool {
	return account != nil && account.Platform == PlatformAnthropic &&
		account.Type == AccountTypeOAuth
}

func (s *GatewayService) forwardClaudeStrictOpenAI(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	body []byte,
	parsed *ParsedRequest,
	protocol string,
	startTime time.Time,
) (*ForwardResult, error) {
	if err := validateClaudeStrictOpenAIPath(c, protocol); err != nil {
		writeClaudeStrictOpenAIRejection(c, protocol)
		return nil, err
	}
	canonical, report, err := officialegress.AdaptIngressProtocol(protocol, body)
	if err != nil {
		writeClaudeStrictOpenAIRejection(c, protocol)
		return nil, fmt.Errorf("Claude strict 入站翻译失败：%w", err)
	}
	runtime := s.claudeStrictRuntime()
	if runtime == nil {
		return nil, errors.New("Claude strict OpenAI route 已开启但 runtime 未注入")
	}
	if err := runtime.ValidateCanonical(canonical, report); err != nil {
		writeClaudeStrictOpenAIRejection(c, protocol)
		return nil, fmt.Errorf("Claude strict 入站不在 SupportEnvelope：%w", err)
	}
	if account.IsCustomBaseURLEnabled() && strings.TrimSpace(account.GetCustomBaseURL()) != "" {
		return nil, errors.New("Claude strict OpenAI route 不允许自定义上游地址")
	}
	if account.ProxyID != nil && account.Proxy == nil {
		return nil, errors.New("Claude strict OpenAI route 账号代理未解析，禁止静默直连")
	}
	trusted, err := buildClaudeStrictCanonicalTrustedFacts(
		c, account, parsed, protocol, canonical.FirstUserText,
	)
	if err != nil {
		return nil, err
	}
	accessToken, tokenType, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return nil, err
	}
	if tokenType != "oauth" || strings.TrimSpace(accessToken) == "" {
		return nil, errors.New("Claude strict OpenAI route 只接受 OAuth access token")
	}
	proxyURL := ""
	if account.ProxyID != nil {
		proxyURL = account.Proxy.URL()
	}
	executionContext := withClaudeHTTPTransport(
		ctx, proxyURL, account.ID, claudeFWGConcurrencyLimit(account),
	)
	result, err := runtime.ExecuteCanonical(
		executionContext,
		officialegress.ClaudeCanonicalExecution{
			Canonical: canonical, Translation: report, AccessToken: accessToken,
			TrustedFacts: trusted, InvocationID: uuid.NewString(),
		},
	)
	if err != nil {
		if officialegress.IsClaudeSupportEnvelopeRejection(err) && !c.Writer.Written() {
			writeClaudeStrictOpenAIRejection(c, protocol)
		}
		return nil, fmt.Errorf("Claude strict OpenAI 执行失败：%w", err)
	}
	return s.finishClaudeStrictOpenAIResponse(
		executionContext, c, account, parsed, canonical, result, startTime,
	)
}

func validateClaudeStrictOpenAIPath(c *gin.Context, protocol string) error {
	if c == nil || c.Request == nil || c.Request.URL == nil || c.Request.Method != http.MethodPost {
		return errors.New("Claude strict OpenAI 入口缺少有效 POST 路径")
	}
	if c.Request.URL.RawQuery != "" || c.Request.URL.ForceQuery {
		return errors.New("Claude strict OpenAI 入口不接受 query")
	}
	path := c.Request.URL.Path
	switch protocol {
	case officialegress.IngressProtocolOpenAIChatCompletions:
		if path != "/v1/chat/completions" && path != "/chat/completions" {
			return fmt.Errorf("Chat Completions 物理入口未登记：%s", path)
		}
	case officialegress.IngressProtocolOpenAIResponses:
		if path != "/v1/responses" && path != "/responses" {
			return fmt.Errorf("Responses 根入口或子路径未登记：%s", path)
		}
	default:
		return fmt.Errorf("Claude strict OpenAI 协议未登记：%s", protocol)
	}
	return nil
}

func writeClaudeStrictOpenAIRejection(c *gin.Context, protocol string) {
	if c == nil || c.Writer.Written() {
		return
	}
	if protocol == officialegress.IngressProtocolOpenAIChatCompletions {
		writeGatewayCCError(c, http.StatusBadRequest, "invalid_request_error",
			"Request is outside the Claude Code SupportEnvelope")
		return
	}
	writeResponsesError(c, http.StatusBadRequest, "invalid_request_error",
		"Request is outside the Claude Code SupportEnvelope")
}

func buildClaudeStrictCanonicalTrustedFacts(
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
	protocol string,
	firstUserText string,
) (officialegress.ClaudeTrustedFacts, error) {
	if c == nil || c.Request == nil || account == nil || parsed == nil || parsed.Body == nil {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude strict Planner 缺少入口或账号")
	}
	apiKeyID := getAPIKeyIDFromContext(c)
	if apiKeyID <= 0 {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude strict Planner 缺少已认证 API Key binding")
	}
	accountUUID := strings.TrimSpace(account.GetExtraString("account_uuid"))
	if _, err := uuid.Parse(accountUUID); err != nil {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude strict Planner 缺少有效 account_uuid")
	}
	if strings.TrimSpace(firstUserText) == "" {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude strict Planner 缺少可信会话锚点")
	}
	sessionContext := parsed.SessionContext
	if sessionContext == nil {
		sessionContext = &SessionContext{
			ClientIP: c.ClientIP(), UserAgent: c.GetHeader("User-Agent"), APIKeyID: apiKeyID,
		}
	}
	if sessionContext.APIKeyID != 0 && sessionContext.APIKeyID != apiKeyID {
		return officialegress.ClaudeTrustedFacts{}, errors.New("Claude strict Planner 的 API Key 会话绑定冲突")
	}
	sessionContext.APIKeyID = apiKeyID
	sessionSeed := buildStableSessionSeed(
		account.ID,
		"claude-fw-g:"+sessionContextDiscriminator(sessionContext),
		firstUserText,
	)
	return officialegress.ClaudeTrustedFacts{
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
			IngressProtocol:  protocol,
			IngressBindingID: "api-key:" + strconv.FormatInt(apiKeyID, 10),
		},
		Features: officialegress.ClaudeTrustedFeatureFacts{
			SystemMode: officialegress.ClaudeSystemDefault,
		},
	}, nil
}

func (s *GatewayService) finishClaudeStrictOpenAIResponse(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	parsed *ParsedRequest,
	canonical officialegress.CanonicalRequest,
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
	beginUpstreamResponseModelObservation(c)
	upstreamAcceptedNotified := false

	for {
		resp := result.Response
		if resp == nil || resp.Body == nil {
			return nil, errors.New("Claude strict OpenAI 返回空响应")
		}
		if resp.StatusCode >= http.StatusBadRequest {
			return nil, s.handleClaudeStrictOpenAIError(ctx, c, account, &result, canonical.IngressProtocol)
		}
		prepared, prepareErr := s.prepareClaudeStrictOpenAIResponse(ctx, result)
		if prepareErr != nil {
			_ = resp.Body.Close()
			return nil, prepareErr
		}
		result = prepared
		sessionResult = &result
		resp = result.Response
		if resp == nil || resp.Body == nil {
			return nil, errors.New("Claude strict fallback 返回空响应")
		}
		if resp.StatusCode >= http.StatusBadRequest {
			return nil, s.handleClaudeStrictOpenAIError(ctx, c, account, &result, canonical.IngressProtocol)
		}
		if err := parsed.ReplaceBody(result.WireBody); err != nil {
			_ = resp.Body.Close()
			return nil, fmt.Errorf("记录 Claude strict final wire：%w", err)
		}
		if parsed.OnUpstreamAccepted != nil && !upstreamAcceptedNotified {
			parsed.OnUpstreamAccepted()
			upstreamAcceptedNotified = true
		}
		reasoningEffort := canonical.ReasoningEffort
		var reasoningEffortPtr *string
		if reasoningEffort != "" {
			reasoningEffortPtr = &reasoningEffort
		}
		var forwardResult *ForwardResult
		var handleErr error
		switch canonical.IngressProtocol {
		case officialegress.IngressProtocolOpenAIChatCompletions:
			if canonical.Stream {
				forwardResult, handleErr = s.handleCCStreamingFromAnthropic(
					resp, c, canonical.Model, result.Model, reasoningEffortPtr,
					startTime, canonical.IncludeUsage,
				)
			} else {
				forwardResult, handleErr = s.handleCCBufferedFromAnthropic(
					resp, c, canonical.Model, result.Model, reasoningEffortPtr, startTime,
				)
			}
		case officialegress.IngressProtocolOpenAIResponses:
			mapping := apicompat.ResponsesClientToolMapping{}
			if canonical.Stream {
				forwardResult, handleErr = s.handleResponsesStreamingResponse(
					resp, c, canonical.Model, result.Model, reasoningEffortPtr,
					startTime, mapping,
				)
			} else {
				forwardResult, handleErr = s.handleResponsesBufferedStreamingResponse(
					resp, c, canonical.Model, result.Model, reasoningEffortPtr,
					startTime, mapping,
				)
			}
		default:
			handleErr = errors.New("Claude strict 响应协议未登记")
		}
		_ = resp.Body.Close()
		if handleErr != nil && result.StreamFallback != nil && !c.Writer.Written() {
			var streamErr *sseStreamErrorEventError
			if errors.As(handleErr, &streamErr) {
				fallback, fallbackErr := result.StreamFallback(ctx)
				if fallbackErr != nil {
					return nil, fallbackErr
				}
				result = fallback
				sessionResult = &result
				beginUpstreamResponseModelObservation(c)
				continue
			}
		}
		if handleErr != nil {
			return forwardResult, handleErr
		}
		if forwardResult == nil {
			return nil, errors.New("Claude strict OpenAI 响应转换返回空结果")
		}
		if err := sessionResult.FinalizeSessionWithResponseModel(
			true, observedUpstreamResponseModel(c),
		); err != nil {
			return nil, fmt.Errorf("提交 Claude strict 会话状态：%w", err)
		}
		sessionFinalized = true
		forwardResult.UpstreamResponseModel = observedUpstreamResponseModel(c)
		forwardResult.UpstreamResponseModelConflict = observedUpstreamResponseModelConflict(c)
		return forwardResult, nil
	}
}

func (s *GatewayService) handleClaudeStrictOpenAIError(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	result *officialegress.ClaudeCandidateResult,
	protocol string,
) error {
	resp := result.Response
	respBody, _ := s.readUpstreamErrorBody(resp)
	_ = resp.Body.Close()
	resp.Body = io.NopCloser(bytes.NewReader(respBody))
	message := sanitizeUpstreamErrorMessage(extractUpstreamErrorMessage(respBody))
	if s.shouldFailoverUpstreamError(resp.StatusCode) {
		if s.rateLimitService != nil {
			s.rateLimitService.HandleUpstreamError(
				ctx, account, resp.StatusCode, resp.Header, respBody, result.Model,
			)
		}
		return &UpstreamFailoverError{
			StatusCode: resp.StatusCode, ResponseBody: respBody,
			RetryableOnSameAccount: account.IsPoolMode() && account.IsPoolModeRetryableStatus(resp.StatusCode),
		}
	}
	status := mapUpstreamStatusCode(resp.StatusCode)
	if protocol == officialegress.IngressProtocolOpenAIChatCompletions {
		writeGatewayCCError(c, status, "server_error", message)
	} else {
		writeResponsesError(c, status, "server_error", message)
	}
	return fmt.Errorf("Claude strict 上游状态 %d：%s", resp.StatusCode, message)
}

func (s *GatewayService) prepareClaudeStrictOpenAIResponse(
	ctx context.Context,
	result officialegress.ClaudeCandidateResult,
) (officialegress.ClaudeCandidateResult, error) {
	for {
		if result.Response == nil || result.Response.Body == nil {
			return officialegress.ClaudeCandidateResult{}, errors.New("Claude strict 响应为空")
		}
		if result.Response.StatusCode >= http.StatusBadRequest {
			return result, nil
		}
		if !result.Stream {
			if err := convertClaudeStrictJSONResponseToSSE(result.Response); err != nil {
				return officialegress.ClaudeCandidateResult{}, err
			}
			result.Stream = true
			return result, nil
		}
		errorPrelude, err := preflightClaudeStrictSSE(result.Response)
		if err != nil {
			return officialegress.ClaudeCandidateResult{}, err
		}
		if !errorPrelude {
			return result, nil
		}
		_ = result.Response.Body.Close()
		if result.StreamFallback == nil {
			return officialegress.ClaudeCandidateResult{}, &sseStreamErrorEventError{}
		}
		fallback, err := result.StreamFallback(ctx)
		if err != nil {
			return officialegress.ClaudeCandidateResult{}, err
		}
		result = fallback
	}
}

type claudeStrictBufferedBody struct {
	io.Reader
	io.Closer
}

func preflightClaudeStrictSSE(resp *http.Response) (bool, error) {
	reader := bufio.NewReader(resp.Body)
	var prefix bytes.Buffer
	for {
		eventName, eventType, raw, err := readClaudeStrictSSEEvent(reader)
		if len(raw) > 0 {
			if prefix.Len()+len(raw) > claudeStrictSSEPreludeLimit {
				return false, errors.New("Claude strict SSE 首帧超过上限")
			}
			prefix.Write(raw)
		}
		if err != nil {
			return false, err
		}
		if eventName == "error" || eventType == "error" {
			return true, nil
		}
		if eventName == "ping" || eventType == "ping" {
			continue
		}
		resp.Body = &claudeStrictBufferedBody{
			Reader: io.MultiReader(bytes.NewReader(prefix.Bytes()), reader),
			Closer: resp.Body,
		}
		return false, nil
	}
}

func readClaudeStrictSSEEvent(reader *bufio.Reader) (string, string, []byte, error) {
	var raw bytes.Buffer
	eventName := ""
	dataLines := make([]string, 0, 1)
	for {
		line, err := reader.ReadString('\n')
		if line != "" {
			raw.WriteString(line)
			trimmed := strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
			if value, ok := parseAnthropicSSEField(trimmed, "event"); ok {
				eventName = value
			}
			if value, ok := parseAnthropicSSEField(trimmed, "data"); ok {
				dataLines = append(dataLines, value)
			}
			if trimmed == "" {
				break
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) && raw.Len() > 0 {
				break
			}
			return eventName, "", raw.Bytes(), err
		}
	}
	if raw.Len() == 0 {
		return "", "", nil, io.EOF
	}
	eventType := ""
	if data := strings.Join(dataLines, "\n"); data != "" {
		var envelope struct {
			Type string `json:"type"`
		}
		if json.Unmarshal([]byte(data), &envelope) == nil {
			eventType = envelope.Type
		}
	}
	return eventName, eventType, raw.Bytes(), nil
}

func convertClaudeStrictJSONResponseToSSE(resp *http.Response) error {
	body, err := io.ReadAll(io.LimitReader(resp.Body, claudeStrictJSONResponseLimit+1))
	if err != nil {
		return err
	}
	_ = resp.Body.Close()
	if len(body) > claudeStrictJSONResponseLimit {
		return errors.New("Claude strict JSON fallback 响应超过上限")
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
	if json.Unmarshal(body, &message) != nil || message.ID == "" || message.Model == "" ||
		message.Type != "message" || message.Role != "assistant" || len(message.Usage) == 0 {
		return errors.New("Claude strict JSON fallback message 非法")
	}
	var startUsage map[string]any
	if json.Unmarshal(message.Usage, &startUsage) != nil {
		return errors.New("Claude strict JSON fallback usage 非法")
	}
	startUsage["output_tokens"] = 0
	var finalUsage struct {
		OutputTokens int `json:"output_tokens"`
	}
	if json.Unmarshal(message.Usage, &finalUsage) != nil {
		return errors.New("Claude strict JSON fallback usage 非法")
	}
	var sse bytes.Buffer
	writeEvent := func(name string, value any) error {
		payload, marshalErr := json.Marshal(value)
		if marshalErr != nil {
			return marshalErr
		}
		_, marshalErr = fmt.Fprintf(&sse, "event: %s\ndata: %s\n\n", name, payload)
		return marshalErr
	}
	if err := writeEvent("message_start", map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id": message.ID, "type": message.Type, "role": message.Role,
			"content": []any{}, "model": message.Model, "stop_reason": nil,
			"stop_sequence": nil, "usage": startUsage,
		},
	}); err != nil {
		return err
	}
	for index, raw := range message.Content {
		if err := bridgeClaudeFWGContentBlock(writeEvent, index, raw); err != nil {
			return err
		}
	}
	if err := writeEvent("message_delta", map[string]any{
		"type": "message_delta",
		"delta": map[string]any{
			"stop_reason": message.StopReason, "stop_sequence": message.StopSequence,
		},
		"usage": map[string]int{"output_tokens": finalUsage.OutputTokens},
	}); err != nil {
		return err
	}
	if err := writeEvent("message_stop", map[string]string{"type": "message_stop"}); err != nil {
		return err
	}
	resp.Body = io.NopCloser(bytes.NewReader(sse.Bytes()))
	resp.ContentLength = int64(sse.Len())
	resp.Header.Set("Content-Type", "text/event-stream")
	resp.Header.Set("Content-Length", strconv.Itoa(sse.Len()))
	return nil
}
