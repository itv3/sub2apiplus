package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestOfficialAnthropicBetaHeaderPreservesDynamicCapabilities(t *testing.T) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicSetupTokenMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)

	body := []byte(`{
		"model":"claude-fable-5",
		"output_config":{"format":{"type":"json_schema"}},
		"tools":[{"type":"tool_search_tool_regex_20251119","name":"search"}]
	}`)
	betas := splitAnthropicBetaTokens(buildOfficialAnthropicBetaHeader(profile, body))

	// claude-fable-5 命中 API Key mimic 的 1M 清单，但 OAuth 官方出站与 mimic
	// 是两条独立链路：官方 CLI 默认流量不带 context-1m，OAuth 侧不得自动补全。
	require.NotContains(t, betas, claude.BetaContext1M)
	require.Contains(t, betas, AnthropicAPIKeyBetaStructuredOutputs)
	require.Contains(t, betas, "advanced-tool-use-2025-11-20")
	require.Equal(t, 1, countString(betas, AnthropicAPIKeyBetaStructuredOutputs))
	require.Equal(t, 1, countString(betas, "advanced-tool-use-2025-11-20"))
}

func TestOfficialAnthropicAdvancedToolUseDetectsDeferredLoading(t *testing.T) {
	t.Parallel()
	body := []byte(`{"tools":[{"name":"mcp__docs","custom":{"defer_loading":true},"input_schema":{"type":"object"}}]}`)
	require.True(t, officialAnthropicBodyRequiresAdvancedToolUse(body))
}

func TestOfficialAnthropicAdvancedToolUseDetectsTopLevelDeferredLoading(t *testing.T) {
	t.Parallel()
	body := []byte(`{"tools":[{"name":"mcp__docs","defer_loading":true,"input_schema":{"type":"object"}}]}`)
	require.True(t, officialAnthropicBodyRequiresAdvancedToolUse(body))
}

func TestOfficialAnthropicAdvancedToolUseRejectsFalseDeferredLoading(t *testing.T) {
	t.Parallel()
	body := []byte(`{"tools":[{"name":"mcp__docs","custom":{"defer_loading":false},"input_schema":{"type":"object"}}]}`)
	require.False(t, officialAnthropicBodyRequiresAdvancedToolUse(body))
}

func TestOfficialAnthropicDynamicBetasHonorFilterPolicy(t *testing.T) {
	gin.SetMode(gin.TestMode)
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicSetupTokenMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	account := officialEgressTestAccount(94, PlatformAnthropic)

	tests := []struct {
		name string
		beta string
		body string
	}{
		{
			name: "结构化输出",
			beta: AnthropicAPIKeyBetaStructuredOutputs,
			body: `{"model":"claude-fable-5","output_config":{"format":{"type":"json_schema"}}}`,
		},
		{
			name: "延迟工具加载",
			beta: "advanced-tool-use-2025-11-20",
			body: `{"model":"claude-fable-5","tools":[{"name":"mcp__docs","custom":{"defer_loading":true},"input_schema":{"type":"object"}}]}`,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			svc := &GatewayService{
				settingService: newBetaPolicySettingServiceWithActionForTest(t, tt.beta, BetaPolicyActionFilter),
			}
			req := httptest.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages?beta=true", strings.NewReader(tt.body))
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = req.Clone(req.Context())

			_, err := svc.resolveOfficialAnthropicBetaHeader(req, c, account, profile, []byte(tt.body))
			var blocked *BetaBlockedError
			require.ErrorAs(t, err, &blocked)
		})
	}
}

func TestOfficialAnthropicTopLevelJSONUsesClaudeFieldOrder(t *testing.T) {
	body, err := marshalOfficialOrderedJSONObject(map[string]any{
		"stream":     true,
		"max_tokens": 4096,
		"system":     []any{},
		"messages":   []any{},
		"model":      "claude-fable-5",
		"tools":      []any{},
	}, []string{
		"model", "messages", "system", "tools", "tool_choice", "metadata",
		"max_tokens", "thinking", "temperature", "context_management",
		"stream", "output_config", "speed",
	})
	require.NoError(t, err)
	require.Equal(t, `{"model":"claude-fable-5","messages":[],"system":[],"tools":[],"max_tokens":4096,"stream":true}`, string(body))
}

func TestOfficialAnthropicSamplingFieldsFollowThinkingContract(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("thinking 删除 temperature、top_p 和 top_k", func(t *testing.T) {
		body := []byte(`{"thinking":{"type":"enabled","budget_tokens":1024},"temperature":0.7,"top_p":0.9,"top_k":40}`)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		captureOfficialAnthropicIngressContract(c, body)

		got, err := finalizeOfficialAnthropicIngressDefaults(c, body)
		require.NoError(t, err)
		for _, field := range []string{"temperature", "top_p", "top_k"} {
			require.False(t, gjson.GetBytes(got, field).Exists(), field)
		}
	})

	t.Run("非 thinking 保留显式 temperature 但删除官方不发送的参数", func(t *testing.T) {
		body := []byte(`{"temperature":0.7,"top_p":0.9,"top_k":40}`)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		captureOfficialAnthropicIngressContract(c, body)

		got, err := finalizeOfficialAnthropicIngressDefaults(c, body)
		require.NoError(t, err)
		require.Equal(t, 0.7, gjson.GetBytes(got, "temperature").Float())
		require.False(t, gjson.GetBytes(got, "top_p").Exists())
		require.False(t, gjson.GetBytes(got, "top_k").Exists())
	})

	t.Run("转换链生成的 temperature 继续删除", func(t *testing.T) {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		captureOfficialAnthropicIngressContract(c, []byte(`{}`))

		got, err := finalizeOfficialAnthropicIngressDefaults(c, []byte(`{"temperature":1}`))
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(got, "temperature").Exists())
	})
}

func TestOfficialEgressEndpointAliasesCannotBypassProfiles(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name             string
		platform         string
		ingressPath      string
		upstreamURL      string
		canonicalInbound string
	}{
		{
			name:             "Anthropic Responses 前缀别名",
			platform:         PlatformAnthropic,
			ingressPath:      "/openai/v1/responses",
			upstreamURL:      "https://api.anthropic.com/v1/messages",
			canonicalInbound: "/v1/responses",
		},
		{
			name:             "OpenAI Codex 直连别名",
			platform:         PlatformOpenAI,
			ingressPath:      "/backend-api/codex/responses",
			upstreamURL:      "https://chatgpt.com/backend-api/codex/responses",
			canonicalInbound: "/v1/responses",
		},
		{
			name:             "OpenAI compact 前缀别名",
			platform:         PlatformOpenAI,
			ingressPath:      "/openai/v1/responses/compact",
			upstreamURL:      "https://chatgpt.com/backend-api/codex/responses/compact",
			canonicalInbound: "/v1/responses/compact",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			account := officialEgressTestAccount(94, tt.platform)
			upstreamRequest := httptest.NewRequest(http.MethodPost, tt.upstreamURL, nil)
			ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
			ingressContext.Request = httptest.NewRequest(http.MethodPost, tt.ingressPath, nil)

			got, err := attachOfficialEgressHTTPContext(upstreamRequest, ingressContext, account, tt.platform)
			require.NoError(t, err)
			egressContext, exists := OfficialEgressContextFromContext(got.Context())
			require.True(t, exists)
			require.Equal(t, tt.canonicalInbound, egressContext.InboundEndpoint())
		})
	}
}

func TestOfficialEgressWebSocketAliasUsesCanonicalEndpoint(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := officialEgressTestAccount(94, PlatformOpenAI)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodGet, "/openai/v1/responses", nil)
	firstPayload := newOfficialOpenAIWSFirstFrameForTest(t)
	applyOfficialOpenAIWSIngressHeadersForTest(ingressContext, firstPayload)

	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(),
		ingressContext,
		account,
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
	)
	require.NoError(t, err)
	egressContext, exists := OfficialEgressContextFromContext(ctx)
	require.True(t, exists)
	require.Equal(t, "/v1/responses", egressContext.InboundEndpoint())
}

func countString(values []string, target string) int {
	count := 0
	for _, value := range values {
		if strings.EqualFold(value, target) {
			count++
		}
	}
	return count
}
