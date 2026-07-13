package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func newBetaPolicySettingServiceForTest(t *testing.T, token string) *SettingService {
	return newBetaPolicySettingServiceWithActionForTest(t, token, BetaPolicyActionBlock)
}

func newBetaPolicySettingServiceWithActionForTest(t *testing.T, token, action string) *SettingService {
	t.Helper()
	settings := &BetaPolicySettings{
		Rules: []BetaPolicyRule{
			{
				BetaToken: token,
				Action:    action,
				Scope:     BetaPolicyScopeAll,
			},
		},
	}
	raw, err := json.Marshal(settings)
	require.NoError(t, err)
	return NewSettingService(&gatewayTTLSettingRepo{
		data: map[string]string{
			SettingKeyBetaPolicySettings: string(raw),
		},
	}, &config.Config{})
}

func TestDefaultAPIKeyMimicBetaHeaderSelectsTerminalBetaFromFinalBody(t *testing.T) {
	t.Run("普通请求使用 fallback-credit", func(t *testing.T) {
		header := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-sonnet-4-5","messages":[]}`))
		require.True(t, anthropicBetaTokensContains(header, claude.BetaFallbackCredit))
		require.False(t, anthropicBetaTokensContains(header, claude.BetaStructuredOutputs))
	})

	t.Run("不相关的 output_config 不触发 structured-outputs", func(t *testing.T) {
		header := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-sonnet-4-5","output_config":{"effort":"high"},"messages":[]}`))
		require.True(t, anthropicBetaTokensContains(header, claude.BetaFallbackCredit))
		require.False(t, anthropicBetaTokensContains(header, claude.BetaStructuredOutputs))
	})

	t.Run("大小写错误或非法类型不触发 structured-outputs", func(t *testing.T) {
		upperCase := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-sonnet-4-5","output_config":{"format":{"type":"JSON_SCHEMA"}},"messages":[]}`))
		illegalType := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-sonnet-4-5","output_config":{"format":{"type":123}},"messages":[]}`))
		require.True(t, anthropicBetaTokensContains(upperCase, claude.BetaFallbackCredit))
		require.False(t, anthropicBetaTokensContains(upperCase, claude.BetaStructuredOutputs))
		require.True(t, anthropicBetaTokensContains(illegalType, claude.BetaFallbackCredit))
		require.False(t, anthropicBetaTokensContains(illegalType, claude.BetaStructuredOutputs))
	})

	t.Run("结构化输出使用 structured-outputs 并保持 context-1m 位置", func(t *testing.T) {
		header := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-fable-5","output_config":{"format":{"type":"json_schema"}},"messages":[]}`))
		tokens := parseAnthropicBetaHeader(header)
		require.True(t, anthropicBetaTokensContains(header, claude.BetaStructuredOutputs))
		require.False(t, anthropicBetaTokensContains(header, claude.BetaFallbackCredit))
		require.Equal(t, claude.BetaContext1M, tokens[1])
		require.Equal(t, claude.BetaStructuredOutputs, tokens[len(tokens)-1])
		seen := make(map[string]struct{}, len(tokens))
		for _, token := range tokens {
			_, duplicated := seen[token]
			require.False(t, duplicated, "beta token 不应重复: %s", token)
			seen[token] = struct{}{}
		}
	})

	t.Run("Haiku 保持精简 beta 特殊路径", func(t *testing.T) {
		header := defaultAPIKeyMimicBetaHeader([]byte(`{"model":"claude-haiku-4-5","output_config":{"format":{"type":"json_schema"}},"messages":[]}`))
		require.Equal(t, claude.APIKeyHaikuBetaHeader, header)
		require.False(t, anthropicBetaTokensContains(header, claude.BetaStructuredOutputs))
		require.False(t, anthropicBetaTokensContains(header, claude.BetaFallbackCredit))
	})

	t.Run("count_tokens 暂不扩展结构化输出条件规则", func(t *testing.T) {
		header := defaultAPIKeyCountTokensMimicBetaHeader([]byte(`{"model":"claude-sonnet-4-5","output_config":{"format":{"type":"json_schema"}},"messages":[]}`))
		require.True(t, anthropicBetaTokensContains(header, claude.BetaFallbackCredit))
		require.True(t, anthropicBetaTokensContains(header, claude.BetaTokenCounting))
		require.False(t, anthropicBetaTokensContains(header, claude.BetaStructuredOutputs))
	})
}

func TestGatewayServiceAnthropicAPIKeyMimicRejectsStructuredOutputsWhenRequiredBetaFiltered(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")

	svc := &GatewayService{
		cfg:            &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}},
		settingService: newBetaPolicySettingServiceWithActionForTest(t, claude.BetaStructuredOutputs, BetaPolicyActionFilter),
	}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
	}
	body := []byte(`{"model":"claude-sonnet-4-5","output_config":{"format":{"type":"json_schema"}},"messages":[{"role":"user","content":"hello"}]}`)

	_, _, err := svc.buildUpstreamRequest(
		context.Background(), c, account, body, "anthropic-key", "apikey", "claude-sonnet-4-5", true, false,
	)
	require.Error(t, err)
	var blocked *BetaBlockedError
	require.ErrorAs(t, err, &blocked)
	require.Contains(t, blocked.Message, claude.BetaStructuredOutputs)
	require.Contains(t, blocked.Message, "过滤策略")
}

func TestGatewayServiceAnthropicAPIKeyMimicBlocksAutoInjectedFinalBeta(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "same-client/1.0")
	c.Request.RemoteAddr = "1.2.3.4:1234"

	svc := &GatewayService{
		cfg: &config.Config{
			Gateway: config.GatewayConfig{
				InjectBetaForAPIKey: true,
				MaxLineSize:         defaultMaxLineSize,
			},
		},
		settingService: newBetaPolicySettingServiceForTest(t, claude.BetaContext1M),
	}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
		Credentials: map[string]any{
			"api_key": "anthropic-key",
		},
	}

	_, _, err := svc.buildUpstreamRequest(
		context.Background(),
		c,
		account,
		[]byte(`{"model":"claude-opus-4-8","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`),
		"anthropic-key",
		"apikey",
		"claude-opus-4-8",
		false,
		false,
	)
	require.Error(t, err)
	var blocked *BetaBlockedError
	require.ErrorAs(t, err, &blocked)
	require.Contains(t, blocked.Error(), claude.BetaContext1M)
}

func TestGatewayServiceAnthropicAPIKeyMimicCountTokensBlocksAutoInjectedFinalBeta(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages/count_tokens", nil)

	svc := &GatewayService{
		cfg: &config.Config{
			Gateway: config.GatewayConfig{
				InjectBetaForAPIKey: true,
			},
		},
		settingService: newBetaPolicySettingServiceForTest(t, claude.BetaTokenCounting),
	}
	account := &Account{
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
		Credentials: map[string]any{
			"api_key": "anthropic-key",
		},
	}

	_, _, err := svc.buildCountTokensRequest(
		context.Background(),
		c,
		account,
		[]byte(`{"model":"claude-3-5-sonnet-latest","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`),
		"anthropic-key",
		"apikey",
		"claude-3-5-sonnet-latest",
		false,
	)
	require.Error(t, err)
	var blocked *BetaBlockedError
	require.ErrorAs(t, err, &blocked)
	require.Contains(t, blocked.Error(), claude.BetaTokenCounting)
}

func TestBuildAPIKeyMimicMetadataUserIDUsesAPIKeySessionContext(t *testing.T) {
	account := &Account{
		ID: 12,
		Extra: map[string]any{
			"account_uuid": "11111111-1111-1111-1111-111111111111",
		},
	}
	body := []byte(`{"messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]}`)
	headers := http.Header{}
	headers.Set("User-Agent", "same-client/1.0")

	first := buildAPIKeyMimicMetadataUserID(account, body, headers, "1.1.1.1", 101)
	second := buildAPIKeyMimicMetadataUserID(account, body, headers, "1.1.1.1", 202)

	require.NotEmpty(t, first)
	require.NotEmpty(t, second)
	require.NotEqual(t, first, second)

	firstParsed := ParseMetadataUserID(first)
	secondParsed := ParseMetadataUserID(second)
	require.NotNil(t, firstParsed)
	require.NotNil(t, secondParsed)
	require.NotEqual(t, firstParsed.SessionID, secondParsed.SessionID)
}

func TestGatewayServiceAnthropicAPIKeyMimicKeepsValidMetadataSession(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")

	svc := &GatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
	}
	const metadataUserID = `{"device_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","account_uuid":"","session_id":"11111111-2222-4333-8444-555555555555"}`
	body := []byte(`{"model":"claude-fable-5","metadata":{"user_id":` + strconv.Quote(metadataUserID) + `},"messages":[{"role":"user","content":"hello"}]}`)

	req, wireBody, err := svc.buildUpstreamRequest(
		context.Background(), c, account, body, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)
	require.Equal(t, metadataUserID, gjson.GetBytes(wireBody, "metadata.user_id").String())
	require.Equal(t, "11111111-2222-4333-8444-555555555555", getHeaderRaw(req.Header, "X-Claude-Code-Session-Id"))
}

func TestGatewayServiceAnthropicAPIKeyMimicReplacesInvalidMetadataSession(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")
	c.Request.RemoteAddr = "1.2.3.4:1234"

	svc := &GatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
	}
	body := []byte(`{"model":"claude-fable-5","metadata":{"user_id":"third-party-session"},"messages":[{"role":"user","content":"hello"}]}`)

	req, wireBody, err := svc.buildUpstreamRequest(
		context.Background(), c, account, body, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)
	metadataUserID := gjson.GetBytes(wireBody, "metadata.user_id").String()
	require.NotEqual(t, "third-party-session", metadataUserID)
	parsed := ParseMetadataUserID(metadataUserID)
	require.NotNil(t, parsed)
	require.Equal(t, parsed.SessionID, getHeaderRaw(req.Header, "X-Claude-Code-Session-Id"))
}

func TestGatewayServiceAnthropicAPIKeyMimicSessionStableAcrossAppendedMessages(t *testing.T) {
	gin.SetMode(gin.TestMode)
	newContext := func() *gin.Context {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
		c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")
		c.Request.RemoteAddr = "1.2.3.4:1234"
		return c
	}

	svc := &GatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
	}
	firstBody := []byte(`{"model":"claude-fable-5","messages":[{"role":"user","content":"hello"}]}`)
	appendedBody := []byte(`{"model":"claude-fable-5","messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"},{"role":"user","content":"continue"}]}`)

	firstReq, _, err := svc.buildUpstreamRequest(
		context.Background(), newContext(), account, firstBody, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)
	secondReq, _, err := svc.buildUpstreamRequest(
		context.Background(), newContext(), account, appendedBody, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)

	firstSessionID := getHeaderRaw(firstReq.Header, "X-Claude-Code-Session-Id")
	require.NotEmpty(t, firstSessionID)
	require.Equal(t, firstSessionID, getHeaderRaw(secondReq.Header, "X-Claude-Code-Session-Id"))
}

func TestApplyClaudeCodeMimicHeadersSharedPathStillAddsLegacyHeaders(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages", nil)
	applyClaudeCodeMimicHeaders(req, true)

	require.Equal(t, "stream", getHeaderRaw(req.Header, "x-stainless-helper-method"))
	require.NotEmpty(t, getHeaderRaw(req.Header, "x-client-request-id"))
}

func TestAnthropicAPIKeySDKCLIIdentityOnlyRewritesGeneratedBlocks(t *testing.T) {
	body := []byte(`{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.207.abc; cc_entrypoint=cli;"},{"type":"text","text":"You are Claude Code, Anthropic's official CLI for Claude."},{"type":"text","text":"用户自定义：cc_entrypoint=cli;"}]}`)
	out := applyAnthropicAPIKeySDKCLIIdentity(body)

	require.Contains(t, gjson.GetBytes(out, "system.0.text").String(), "cc_entrypoint=claude-desktop-3p;")
	require.Equal(t, claudeSDKCLIIdentityPrompt, gjson.GetBytes(out, "system.1.text").String())
	require.Equal(t, "用户自定义：cc_entrypoint=cli;", gjson.GetBytes(out, "system.2.text").String())
}

func TestAnthropicAPIKeySDKCLINormalizePreservesMissingTemperatureAndPlainAutoOnly(t *testing.T) {
	body := []byte(`{"model":"claude-fable-5","messages":[],"tools":[{"name":"Read","input_schema":{"type":"object"}}],"tool_choice":{"type":"auto"}}`)
	out, _ := normalizeClaudeOAuthRequestBody(body, "claude-fable-5", claudeOAuthNormalizeOptions{
		preserveMissingTemperature: true,
		dropPlainAutoToolChoice:    true,
	})
	require.False(t, gjson.GetBytes(out, "temperature").Exists())
	require.False(t, gjson.GetBytes(out, "tool_choice").Exists())

	bodyWithSemantics := []byte(`{"model":"claude-fable-5","messages":[],"tools":[{"name":"Read","input_schema":{"type":"object"}}],"tool_choice":{"type":"auto","disable_parallel_tool_use":true},"temperature":0.4}`)
	out, _ = normalizeClaudeOAuthRequestBody(bodyWithSemantics, "claude-fable-5", claudeOAuthNormalizeOptions{
		preserveMissingTemperature: true,
		dropPlainAutoToolChoice:    true,
	})
	require.Equal(t, 0.4, gjson.GetBytes(out, "temperature").Float())
	require.True(t, gjson.GetBytes(out, "tool_choice.disable_parallel_tool_use").Bool())
}
