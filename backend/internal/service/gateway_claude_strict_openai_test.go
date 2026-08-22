package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestGatewayClaudeStrictOpenAIEntrypointsFailClosed(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, test := range []struct {
		protocol  string
		path      string
		parseKind string
		body      string
	}{
		{officialegress.IngressProtocolOpenAIChatCompletions, "/v1/chat/completions", "chat_completions", `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}]}`},
		{officialegress.IngressProtocolOpenAIResponses, "/v1/responses", "responses", `{"model":"claude-sonnet-5","input":"hello"}`},
	} {
		upstream := &claudeFWGServiceUpstream{}
		runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
		svc := newClaudeStrictOpenAIService(cfg, runtimeState)
		body := []byte(test.body)
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest(http.MethodPost, test.path, bytes.NewReader(body))
		parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), test.parseKind)
		require.NoError(t, err)
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
		require.Error(t, err)
		require.Nil(t, result)
		require.Equal(t, http.StatusBadRequest, recorder.Code)
		require.Empty(t, upstream.captures, "第三方入口必须在凭据和上游调用前拒绝")
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
