package service

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// officialEgressT1IdentityCache 固化阶段 0 中账号级缓存指纹和会话掩码的组合。
// 这里全部使用脱敏测试值，不复制 Vircs 抓包中的真实动态标识。
type officialEgressT1IdentityCache struct {
	fingerprint     *Fingerprint
	maskedSessionID string
}

func (c *officialEgressT1IdentityCache) GetFingerprint(context.Context, int64) (*Fingerprint, error) {
	return c.fingerprint, nil
}

func (c *officialEgressT1IdentityCache) SetFingerprint(context.Context, int64, *Fingerprint) error {
	return nil
}

func (c *officialEgressT1IdentityCache) GetMaskedSessionID(context.Context, int64) (string, error) {
	return c.maskedSessionID, nil
}

func (c *officialEgressT1IdentityCache) SetMaskedSessionID(context.Context, int64, string) error {
	return nil
}

func TestOfficialEgressT1_AnthropicHTTPBuiltInProfileIgnoresLegacyIdentityOverride(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		accountUUID       = "11111111-1111-4111-8111-111111111111"
		deviceID          = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		maskedSession     = "99999999-9999-4999-8999-999999999999"
		officialUserAgent = "claude-cli/2.1.220 (external, sdk-cli)"
		cachedUserAgent   = "Mozilla/5.0 Chrome/148.0.7778.271 Electron/42.5.1 Claude/1.22209.3"
	)

	identityCache := &officialEgressT1IdentityCache{
		fingerprint: &Fingerprint{
			ClientID:                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			UserAgent:               cachedUserAgent,
			StainlessLang:           "js",
			StainlessPackageVersion: "0.94.0",
			StainlessOS:             "MacOS",
			StainlessArch:           "arm64",
			StainlessRuntime:        "node",
			StainlessRuntimeVersion: "v26.3.0",
		},
		maskedSessionID: maskedSession,
	}
	svc := &GatewayService{
		identityService: NewIdentityService(identityCache),
	}
	account := &Account{
		ID:       50,
		Platform: PlatformAnthropic,
		Type:     AccountTypeSetupToken,
		Extra: map[string]any{
			"account_uuid":               accountUUID,
			"session_id_masking_enabled": true,
			"enable_tls_fingerprint":     false,
		},
	}

	var egressMetadataIDs []string
	for index, inboundSession := range []string{
		"22222222-2222-4222-8222-222222222222",
		"33333333-3333-4333-8333-333333333333",
	} {
		t.Run(fmt.Sprintf("session_%d", index+1), func(t *testing.T) {
			inboundMetadataID := FormatMetadataUserID(deviceID, accountUUID, inboundSession, "2.1.220")
			body := []byte(fmt.Sprintf(`{
				"model":"claude-sonnet-5",
				"system":[
					{"type":"text","text":"身份块"},
					{"type":"text","text":"简短说明","cache_control":{"type":"ephemeral"}},
					{"type":"text","text":"合并的长系统提示","cache_control":{"type":"ephemeral"}}
				],
				"metadata":{"user_id":%q},
				"messages":[{"role":"user","content":[{"type":"text","text":"执行命令"}]}],
				"tools":[{"name":"Bash","description":"执行命令","input_schema":{"type":"object"}}],
				"stream":true
			}`, inboundMetadataID))

			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
			c.Request.Header.Set("User-Agent", officialUserAgent)
			c.Request.Header.Set("X-Claude-Code-Session-Id", inboundSession)

			req, wireBody, err := svc.buildUpstreamRequest(
				context.Background(),
				c,
				account,
				body,
				"oauth-token",
				"oauth",
				"claude-sonnet-5",
				true,
				false,
			)
			require.NoError(t, err)
			require.NotNil(t, req)

			// 内置画像独占最终 UA 和会话身份，账号缓存的旧 Desktop 指纹不得覆盖。
			require.Equal(t, officialUserAgent, getHeaderRaw(req.Header, "User-Agent"))
			require.NotEqual(t, cachedUserAgent, getHeaderRaw(req.Header, "User-Agent"))

			// 工具名和 temperature 等请求语义不得因画像定型退化。
			require.Equal(t, "Bash", gjson.GetBytes(wireBody, "tools.0.name").String())
			require.False(t, gjson.GetBytes(wireBody, "temperature").Exists())

			egressMetadataID := gjson.GetBytes(wireBody, "metadata.user_id").String()
			egressMetadataIDs = append(egressMetadataIDs, egressMetadataID)
			parsed := ParseMetadataUserID(egressMetadataID)
			require.NotNil(t, parsed)
			require.Equal(t, inboundSession, parsed.SessionID)
			require.Equal(t, inboundSession, getHeaderRaw(req.Header, "X-Claude-Code-Session-Id"))
		})
	}

	require.Len(t, egressMetadataIDs, 2)
	require.NotEqual(t, egressMetadataIDs[0], egressMetadataIDs[1], "不同客户端会话必须保持独立身份")
}

func TestOfficialEgressT1_OpenAIHTTPBuiltInProfileOwnsBodyAndIdentity(t *testing.T) {
	gin.SetMode(gin.TestMode)

	body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	c.Set("api_key", &APIKey{ID: 1})
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

	upstream := &httpUpstreamRecorder{
		resp: &http.Response{
			StatusCode: http.StatusBadRequest,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"error":{"type":"invalid_request_error","message":"阶段 0 特征测试停止响应"}}`)),
		},
	}
	svc := &OpenAIGatewayService{
		cfg: &config.Config{Security: config.SecurityConfig{
			URLAllowlist: config.URLAllowlistConfig{Enabled: false},
		}},
		httpUpstream: upstream,
	}
	svc.openaiModelCapabilities.replaceFromManifest(
		94,
		[]byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`),
	)
	account := newOfficialOpenAIHTTPTestAccount(94)

	result, err := svc.Forward(context.Background(), c, account, body)
	require.Error(t, err)
	require.Nil(t, result)
	require.NotNil(t, upstream.lastReq)

	// 内置画像不得无依据合成 instructions。
	require.Empty(t, strings.TrimSpace(gjson.GetBytes(upstream.lastBody, "instructions").String()))

	// 已经正确的字段必须保持，避免后续 Profile 全量重写。
	require.Equal(t, "additional_tools", gjson.GetBytes(upstream.lastBody, "input.0.type").String())
	sessionID := requireUnifiedOfficialOpenAIWireSession(t, upstream.lastReq)
	require.Equal(t, sessionID, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(upstream.lastBody, "include.0").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())

	// 入口 call_id 和官方连字符身份头必须保持同一生命周期。
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.3.call_id").String())
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.4.call_id").String())
	require.Empty(t, upstream.lastReq.Header.Get("session_id"))
	require.Empty(t, upstream.lastReq.Header.Get("conversation_id"))
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("session-id"))
}

func TestOfficialEgressT1_OpenAIWSCharacterizesHandshakeAndContinuationRewrite(t *testing.T) {
	gin.SetMode(gin.TestMode)

	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "codex_exec/0.145.0")
	c.Request.Header.Set("originator", "codex_exec")
	c.Request.Header.Set("session-id", "77777777-7777-4777-8777-777777777777")
	c.Request.Header.Set("thread-id", "88888888-8888-4888-8888-888888888888")
	c.Request.Header.Set("x-client-request-id", "99999999-9999-4999-8999-999999999999")
	c.Request.Header.Set("version", "0.145.0")
	c.Request.Header.Set("x-codex-window-id", "window-ws:0")
	c.Set("api_key", &APIKey{ID: 1})

	svc := &OpenAIGatewayService{}
	account := &Account{
		ID:       94,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"chatgpt_account_id": "chatgpt-account-test",
		},
	}
	headers, _, err := svc.buildOpenAIWSHeaders(
		context.Background(),
		c,
		account,
		"oauth-token",
		OpenAIWSProtocolDecision{Transport: OpenAIUpstreamTransportResponsesWebsocketV2},
		true,
		"",
		"",
		"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"",
		"",
	)
	require.NoError(t, err)

	require.Len(t, headers.Get("session_id"), 16)
	require.Equal(t, scopeCodexAccountIdentityValue(account, 1, "session", "77777777-7777-4777-8777-777777777777"), headers.Get("session-id"))
	require.Equal(t, scopeCodexAccountIdentityValue(account, 1, "thread", "88888888-8888-4888-8888-888888888888"), headers.Get("thread-id"))
	require.Equal(t, scopeCodexAccountIdentityValue(account, 1, "request", "99999999-9999-4999-8999-999999999999"), headers.Get("x-client-request-id"))
	require.Empty(t, headers.Get("version"))
	require.Equal(t, scopeCodexAccountIdentityValue(account, 1, "window", "window-ws:0"), headers.Get("x-codex-window-id"))

	// 夹具只保留决定续轮策略的结构，不包含抓包中的提示词、工具参数或真实动态标识。
	initialPayload := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"generate":false,
		"store":false,
		"stream":true,
		"prompt_cache_key":"session-test",
		"include":["reasoning.encrypted_content"],
		"parallel_tool_calls":false,
		"input":[
			{"type":"additional_tools","tools":[{"type":"exec"},{"type":"wait"},{"type":"request_user_input"}]},
			{"type":"message","role":"user","content":"第一轮"}
		]
	}`)
	continuationPayload := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"store":false,
		"stream":true,
		"prompt_cache_key":"session-test",
		"include":["reasoning.encrypted_content"],
		"parallel_tool_calls":false,
		"previous_response_id":"resp_phase0_turn_1",
		"input":[
			{"type":"message","role":"user","content":"第一轮"},
			{"type":"message","role":"assistant","content":"第一轮回答"},
			{"type":"message","role":"user","content":"第二轮"}
		]
	}`)

	keepPreviousResponseID, reason, err := shouldKeepIngressPreviousResponseID(
		initialPayload,
		continuationPayload,
		"resp_phase0_turn_1",
		false,
	)
	require.NoError(t, err)
	require.False(t, keepPreviousResponseID)
	require.Equal(t, "non_input_changed", reason)

	replayInput, replayInputExists, err := buildOpenAIWSReplayInputSequence(
		nil,
		false,
		initialPayload,
		false,
	)
	require.NoError(t, err)
	require.True(t, replayInputExists)
	require.Len(t, replayInput, 2)

	replayInput, replayInputExists, err = buildOpenAIWSReplayInputSequence(
		replayInput,
		replayInputExists,
		continuationPayload,
		true,
	)
	require.NoError(t, err)
	require.True(t, replayInputExists)
	require.Len(t, replayInput, 5, "当前策略会把首轮 additional_tools 和消息合并进续轮历史")

	withoutPreviousResponseID, removed, err := dropPreviousResponseIDFromRawPayload(continuationPayload)
	require.NoError(t, err)
	require.True(t, removed)
	fullCreatePayload, err := setOpenAIWSPayloadInputSequence(
		withoutPreviousResponseID,
		replayInput,
		replayInputExists,
	)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(fullCreatePayload, "previous_response_id").Exists())
	require.Equal(t, 5, len(gjson.GetBytes(fullCreatePayload, "input").Array()))
	require.Equal(t, "additional_tools", gjson.GetBytes(fullCreatePayload, "input.0.type").String())
}
