package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestGatewayClaudeStrictOpenAIEntrypointsUsePersonaRuntime(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name       string
		protocol   string
		path       string
		parseKind  string
		body       string
		stream     bool
		wantOutput string
	}{
		{
			name: "chat-buffered", protocol: officialegress.IngressProtocolOpenAIChatCompletions,
			path: "/v1/chat/completions", parseKind: "chat_completions",
			body:       `{"model":"claude-sonnet-5","messages":[{"role":"system","content":"custom rules"},{"role":"user","content":"hello"}],"max_completion_tokens":32000,"reasoning_effort":"low","stream":false,"tools":[]}`,
			wantOutput: `"object":"chat.completion"`,
		},
		{
			name: "chat-stream", protocol: officialegress.IngressProtocolOpenAIChatCompletions,
			path: "/chat/completions", parseKind: "chat_completions",
			body:   `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"stream":true,"stream_options":{"include_usage":true}}`,
			stream: true, wantOutput: "data: [DONE]",
		},
		{
			name: "responses-buffered", protocol: officialegress.IngressProtocolOpenAIResponses,
			path: "/v1/responses", parseKind: "responses",
			body:       `{"model":"claude-sonnet-5","instructions":"custom rules","input":"hello","max_output_tokens":32000,"reasoning":{"effort":"low"},"stream":false,"store":false}`,
			wantOutput: `"object":"response"`,
		},
		{
			name: "responses-stream", protocol: officialegress.IngressProtocolOpenAIResponses,
			path: "/responses", parseKind: "responses",
			body:   `{"model":"claude-sonnet-5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}],"stream":true,"tools":[]}`,
			stream: true, wantOutput: "response.completed",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			upstream := &claudeFWGServiceUpstream{streamAsSSE: true}
			runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
			svc := newClaudeStrictOpenAIService(cfg, runtimeState)
			body := []byte(test.body)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, test.path, bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Set("api_key", &APIKey{ID: 23})
			parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), test.parseKind)
			require.NoError(t, err)
			parsed.SessionContext = &SessionContext{APIKeyID: 23, ClientIP: "127.0.0.1"}

			var result *ForwardResult
			if test.protocol == officialegress.IngressProtocolOpenAIChatCompletions {
				result, err = svc.ForwardAsChatCompletions(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			} else {
				result, err = svc.ForwardAsResponses(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			}
			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, test.stream, result.Stream)
			require.Equal(t, http.StatusOK, recorder.Code)
			require.Contains(t, recorder.Body.String(), test.wantOutput)
			require.Equal(t, "claude-sonnet-5", result.UpstreamResponseModel)
			require.False(t, result.UpstreamResponseModelConflict)

			messages := claudeStrictMessageCaptures(upstream.captures)
			require.Len(t, messages, 1)
			capture := messages[0]
			require.Equal(t, "claude-cli/2.1.226 (external, sdk-cli)", capture.header.Get("User-Agent"))
			require.Equal(t, "Bearer <secret-candidate-access-token>", capture.header.Get("Authorization"))
			require.Contains(t, string(capture.body), `"metadata":{"user_id"`)
			require.Contains(t, string(capture.body), `"messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]`)
			require.NotContains(t, string(capture.body), `"reasoning_effort"`)
			require.NotContains(t, string(capture.body), `"max_completion_tokens"`)
			require.NotContains(t, string(capture.body), `"input":`)
			require.Equal(t, capture.body, parsed.Body.Bytes())
			if !test.stream && test.protocol == officialegress.IngressProtocolOpenAIChatCompletions {
				require.Contains(t, recorder.Body.String(), `"finish_reason":"stop"`)
				require.Contains(t, recorder.Body.String(), `"prompt_tokens":3`)
				require.Contains(t, recorder.Body.String(), `"completion_tokens":2`)
				require.Contains(t, recorder.Body.String(), `"total_tokens":5`)
			}
			if !test.stream && test.protocol == officialegress.IngressProtocolOpenAIResponses {
				require.Contains(t, recorder.Body.String(), `"status":"completed"`)
				require.Contains(t, recorder.Body.String(), `"input_tokens":3`)
				require.Contains(t, recorder.Body.String(), `"output_tokens":2`)
				require.Contains(t, recorder.Body.String(), `"total_tokens":5`)
			}
		})
	}
}

func TestClaudeStrictOpenAIPhysicalAliasMatrix(t *testing.T) {
	tests := []struct {
		name     string
		protocol string
		method   string
		path     string
		allowed  bool
	}{
		{name: "chat-v1", protocol: officialegress.IngressProtocolOpenAIChatCompletions, method: http.MethodPost, path: "/v1/chat/completions", allowed: true},
		{name: "chat-bare", protocol: officialegress.IngressProtocolOpenAIChatCompletions, method: http.MethodPost, path: "/chat/completions", allowed: true},
		{name: "responses-v1", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/v1/responses", allowed: true},
		{name: "responses-bare", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/responses", allowed: true},
		{name: "responses-v1-subpath", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/v1/responses/compact"},
		{name: "responses-bare-subpath", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/responses/compact"},
		{name: "responses-v1-ws", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodGet, path: "/v1/responses"},
		{name: "responses-bare-ws", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodGet, path: "/responses"},
		{name: "codex-direct-root", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/backend-api/codex/responses"},
		{name: "codex-direct-subpath", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/backend-api/codex/responses/compact"},
		{name: "codex-direct-ws", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodGet, path: "/backend-api/codex/responses"},
		{name: "unregistered-openai-prefix", protocol: officialegress.IngressProtocolOpenAIResponses, method: http.MethodPost, path: "/openai/v1/responses"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(test.method, test.path, nil)
			err := validateClaudeStrictOpenAIPath(c, test.protocol)
			if test.allowed {
				require.NoError(t, err)
			} else {
				require.Error(t, err)
			}
		})
	}
}

func TestClaudeStrictResponsesWebSocketFailsClosedBeforeCredential(t *testing.T) {
	upstream := &claudeFWGServiceUpstream{}
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
	svc := newClaudeStrictOpenAIService(cfg, runtimeState)
	require.True(t, svc.ShouldFailCloseClaudeStrictResponsesWebSocket(claudeFWGServiceAccount()))
	openAISvc := &OpenAIGatewayService{cfg: cfg}
	require.True(t, openAISvc.ShouldFailCloseClaudeStrictResponsesWebSocket(claudeFWGServiceAccount()))

	apiKeyAccount := claudeFWGServiceAccount()
	apiKeyAccount.Type = AccountTypeAPIKey
	require.False(t, svc.ShouldFailCloseClaudeStrictResponsesWebSocket(apiKeyAccount))
	require.False(t, openAISvc.ShouldFailCloseClaudeStrictResponsesWebSocket(apiKeyAccount))

	source, err := os.ReadFile("../handler/openai_gateway_handler.go")
	require.NoError(t, err)
	start := bytes.Index(source, []byte("func (h *OpenAIGatewayHandler) ResponsesWebSocket"))
	require.GreaterOrEqual(t, start, 0)
	section := source[start:]
	guardIndex := bytes.Index(section, []byte("ShouldFailCloseClaudeStrictResponsesWebSocket(account)"))
	credentialIndex := bytes.Index(section, []byte("GetRequestCredential(accountCtx, c, account)"))
	require.GreaterOrEqual(t, guardIndex, 0)
	require.Greater(t, credentialIndex, guardIndex,
		"Claude Responses WebSocket 必须在读取凭据或连接上游前 fail-close")
}

func TestGatewayClaudeStrictOpenAIRejectsBeforeAnyUpstreamCall(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name      string
		protocol  string
		path      string
		parseKind string
		body      string
	}{
		{name: "chat-unknown-field", protocol: officialegress.IngressProtocolOpenAIChatCompletions,
			path: "/v1/chat/completions", parseKind: "chat_completions",
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"seed":1}`},
		{name: "chat-third-party-tools", protocol: officialegress.IngressProtocolOpenAIChatCompletions,
			path: "/v1/chat/completions", parseKind: "chat_completions",
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"tools":[{"type":"function","function":{"name":"shell"}}]}`},
		{name: "chat-query", protocol: officialegress.IngressProtocolOpenAIChatCompletions,
			path: "/v1/chat/completions?beta=true", parseKind: "chat_completions",
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}]}`},
		{name: "responses-subpath", protocol: officialegress.IngressProtocolOpenAIResponses,
			path: "/v1/responses/compact", parseKind: "responses",
			body: `{"model":"claude-sonnet-5","input":"hello"}`},
		{name: "responses-stateful", protocol: officialegress.IngressProtocolOpenAIResponses,
			path: "/v1/responses", parseKind: "responses",
			body: `{"model":"claude-sonnet-5","input":"hello","previous_response_id":"resp_1"}`},
		{name: "responses-unknown-model", protocol: officialegress.IngressProtocolOpenAIResponses,
			path: "/v1/responses", parseKind: "responses",
			body: `{"model":"claude-unknown","input":"hello"}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			upstream := &claudeFWGServiceUpstream{}
			runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
			svc := newClaudeStrictOpenAIService(cfg, runtimeState)
			body := []byte(test.body)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, test.path, bytes.NewReader(body))
			parsed := &ParsedRequest{
				Model: "claude-sonnet-5", Body: NewRequestBodyRef(body),
				SessionContext: &SessionContext{APIKeyID: 23, ClientIP: "127.0.0.1"},
			}
			var err error
			if test.protocol == officialegress.IngressProtocolOpenAIChatCompletions {
				_, err = svc.ForwardAsChatCompletions(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			} else {
				_, err = svc.ForwardAsResponses(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			}
			require.Error(t, err)
			require.Equal(t, http.StatusBadRequest, recorder.Code)
			require.Empty(t, upstream.captures, "本地 fail-close 前不得调用 startup、refresh 或 inference")
		})
	}
}

func TestGatewayClaudeStrictOpenAIHandlesHTTPStreamFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, protocol := range []string{
		officialegress.IngressProtocolOpenAIChatCompletions,
		officialegress.IngressProtocolOpenAIResponses,
	} {
		t.Run(protocol, func(t *testing.T) {
			upstream := &claudeFWGServiceUpstream{rejectFirstStream: true}
			runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
			svc := newClaudeStrictOpenAIService(cfg, runtimeState)
			path := "/v1/responses"
			body := []byte(`{"model":"claude-sonnet-5","input":"hello","stream":true}`)
			parseKind := "responses"
			if protocol == officialegress.IngressProtocolOpenAIChatCompletions {
				path = "/v1/chat/completions"
				body = []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"stream":true}`)
				parseKind = "chat_completions"
			}
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
			c.Set("api_key", &APIKey{ID: 23})
			parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), parseKind)
			require.NoError(t, err)
			parsed.SessionContext = &SessionContext{APIKeyID: 23, ClientIP: "127.0.0.1"}
			var result *ForwardResult
			if protocol == officialegress.IngressProtocolOpenAIChatCompletions {
				result, err = svc.ForwardAsChatCompletions(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			} else {
				result, err = svc.ForwardAsResponses(
					context.Background(), c, claudeFWGServiceAccount(), body, parsed,
				)
			}
			require.NoError(t, err)
			require.True(t, result.Stream)
			require.Equal(t, http.StatusOK, recorder.Code)
			require.Len(t, claudeStrictMessageCaptures(upstream.captures), 2)
		})
	}
}

type claudeStrictSSEErrorUpstream struct {
	captures []claudeFWGServiceCapture
	messages int
}

func (u *claudeStrictSSEErrorUpstream) Do(
	request *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
) (*http.Response, error) {
	return u.DoWithTLS(request, proxyURL, accountID, concurrency, nil)
}

func (u *claudeStrictSSEErrorUpstream) DoWithTLS(
	request *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	u.captures = append(u.captures, claudeFWGServiceCapture{
		method: request.Method, url: request.URL.String(), header: request.Header.Clone(),
		body: body, proxyURL: proxyURL, accountID: accountID,
		concurrency: concurrency, tlsProfile: profile,
	})
	contentType := "application/json"
	responseBody := `{}`
	if request.URL.Path == "/v1/messages" {
		u.messages++
		if u.messages == 1 {
			contentType = "text/event-stream"
			responseBody = "event: ping\n" + `data: {"type":"ping"}` + "\n\n" +
				"event: error\n" + `data: {"type":"error","error":{"type":"overloaded_error","message":"retry"}}` + "\n\n"
		} else {
			responseBody = `{"id":"msg_fallback","type":"message","role":"assistant","model":"claude-sonnet-5","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":3,"output_tokens":2}}`
		}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header: http.Header{
			"Content-Type": []string{contentType}, "Request-Id": []string{"req_servicetest"},
		},
		Body: io.NopCloser(strings.NewReader(responseBody)), Request: request,
	}, nil
}

func TestGatewayClaudeStrictOpenAISSEErrorUsesRuntimeFallbackBeforeWriting(t *testing.T) {
	gin.SetMode(gin.TestMode)
	upstream := &claudeStrictSSEErrorUpstream{}
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
	svc := newClaudeStrictOpenAIService(cfg, runtimeState)
	body := []byte(`{"model":"claude-sonnet-5","input":"hello","stream":true}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Set("api_key", &APIKey{ID: 23})
	parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), "responses")
	require.NoError(t, err)
	parsed.SessionContext = &SessionContext{APIKeyID: 23, ClientIP: "127.0.0.1"}
	result, err := svc.ForwardAsResponses(
		context.Background(), c, claudeFWGServiceAccount(), body, parsed,
	)
	require.NoError(t, err)
	require.True(t, result.Stream)
	require.Equal(t, 2, upstream.messages)
	require.NotContains(t, recorder.Body.String(), "overloaded_error")
	require.Contains(t, recorder.Body.String(), "response.completed")
}

func TestClaudeStrictOpenAIBranchesBeforeLegacyConverters(t *testing.T) {
	checks := []struct {
		path       string
		legacyCall string
	}{
		{path: "gateway_forward_as_chat_completions.go", legacyCall: "apicompat.ChatCompletionsToResponses"},
		{path: "gateway_forward_as_responses.go", legacyCall: "adaptResponsesClientToolsForAnthropic"},
	}
	for _, check := range checks {
		source, err := os.ReadFile(check.path)
		require.NoError(t, err)
		strictIndex := bytes.Index(source, []byte("shouldRouteClaudeStrictOpenAI"))
		legacyIndex := bytes.Index(source, []byte(check.legacyCall))
		require.GreaterOrEqual(t, strictIndex, 0)
		require.Greater(t, legacyIndex, strictIndex,
			"Claude OAuth strict 分支必须在旧转换器之前返回")
	}
}

func newClaudeStrictOpenAIService(
	cfg *config.Config,
	runtimeState *OfficialEgressTransitionRuntime,
) *GatewayService {
	return &GatewayService{
		cfg: cfg, rateLimitService: &RateLimitService{},
		responseHeaderFilter: compileResponseHeaderFilter(cfg),
		claudeTokenProvider:  NewClaudeTokenProvider(nil, nil, nil),
		officialEgress:       runtimeState,
	}
}

func claudeStrictMessageCaptures(captures []claudeFWGServiceCapture) []claudeFWGServiceCapture {
	out := make([]claudeFWGServiceCapture, 0, 2)
	for _, capture := range captures {
		if strings.Contains(capture.url, "/v1/messages?beta=true") {
			out = append(out, capture)
		}
	}
	return out
}
