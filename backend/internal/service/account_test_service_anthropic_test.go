//go:build unit

package service

import (
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestAccountTestService_AnthropicAPIKeyMimicUsesFullGatewayRequest(t *testing.T) {
	tests := []struct {
		name        string
		mimic       bool
		wantHeader  string
		wantContext bool
	}{
		{
			name:        "开启兼容时复用完整 mimic 构造链",
			mimic:       true,
			wantHeader:  strings.Join(claude.APIKeyMimicBetas(), ","),
			wantContext: true,
		},
		{
			name:        "关闭兼容时保持普通 API Key 测试形态",
			mimic:       false,
			wantHeader:  claude.APIKeyBetaHeader,
			wantContext: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ctx, _ := newTestContext()
			upstream := &queuedHTTPUpstream{
				responses: []*http.Response{newJSONResponse(http.StatusOK, "")},
			}
			// 挂载默认 BetaPolicy，确保 Fable 5 下 mimic 仍保留 context-1m。
			settingSvc := NewSettingService(
				&betaPolicySettingRepoStub{values: map[string]string{}},
				&config.Config{},
			)
			svc := &AccountTestService{
				gatewayService: &GatewayService{
					cfg: &config.Config{
						Gateway: config.GatewayConfig{
							MaxLineSize: defaultMaxLineSize,
						},
						Security: config.SecurityConfig{
							URLAllowlist: config.URLAllowlistConfig{Enabled: false},
						},
					},
					settingService: settingSvc,
				},
				httpUpstream: upstream,
				cfg: &config.Config{Security: config.SecurityConfig{
					URLAllowlist: config.URLAllowlistConfig{Enabled: false},
				}},
			}
			account := &Account{
				ID:          101,
				Platform:    PlatformAnthropic,
				Type:        AccountTypeAPIKey,
				Concurrency: 1,
				Credentials: map[string]any{"api_key": "test-key"},
				Extra: map[string]any{
					"anthropic_apikey_mimic_claude_code": tt.mimic,
				},
			}

			err := svc.testClaudeAccountConnection(ctx, account, "claude-fable-5")
			require.NoError(t, err)
			require.Len(t, upstream.requests, 1)
			require.Len(t, upstream.bodies, 1)

			req := upstream.requests[0]
			body := upstream.bodies[0]
			require.Equal(t, "claude-fable-5", gjson.GetBytes(body, "model").String())
			betaHeader := getHeaderRaw(req.Header, "anthropic-beta")
			require.Equal(t, tt.wantHeader, betaHeader)
			if tt.wantContext {
				require.Contains(t, betaHeader, claude.BetaContext1M)
				require.Equal(t, "application/json", getHeaderRaw(req.Header, "Accept"))
				require.Equal(t, "claude-cli/2.1.209 (external, claude-desktop-3p, agent-sdk/0.3.209)", getHeaderRaw(req.Header, "User-Agent"))
				require.Equal(t, "test-key", getHeaderRaw(req.Header, "x-api-key"))
				require.Equal(t, "Bearer test-key", getHeaderRaw(req.Header, "Authorization"))
				require.Equal(t, int64(512), gjson.GetBytes(body, "max_tokens").Int())
				require.Len(t, gjson.GetBytes(body, "tools").Array(), len(anthropicAPIKeyMimicTestToolNames))
				require.Equal(t, "Agent", gjson.GetBytes(body, "tools.0.name").String())
				require.Equal(t, "Write", gjson.GetBytes(body, "tools.26.name").String())

				system := gjson.GetBytes(body, "system")
				require.True(t, system.IsArray())
				require.Len(t, system.Array(), 3)
				require.Contains(t, system.Get("0.text").String(), "x-anthropic-billing-header")
				require.Contains(t, system.Get("0.text").String(), "cc_entrypoint=claude-desktop-3p")
				require.Equal(t, claudeSDKCLIIdentityPrompt, system.Get("1.text").String())
				require.Equal(t, claudeCodeSystemPromptExpansion, system.Get("2.text").String())

				metadataUserID := gjson.GetBytes(body, "metadata.user_id").String()
				require.NotEmpty(t, metadataUserID)
				parsed := ParseMetadataUserID(metadataUserID)
				require.NotNil(t, parsed)
				require.Equal(t, parsed.SessionID, getHeaderRaw(req.Header, "X-Claude-Code-Session-Id"))
			} else {
				require.NotContains(t, betaHeader, claude.BetaContext1M)
				// 普通 API Key 测试仍走旧 Claude Code 风格 payload，system 不是三段 mimic 结构。
				system := gjson.GetBytes(body, "system")
				require.True(t, system.IsArray())
				require.NotEqual(t, 3, len(system.Array()))
			}
		})
	}
}

func TestAccountTestService_ProcessClaudeStreamRejectsUnavailableText(t *testing.T) {
	svc := &AccountTestService{}
	ctx, rec := newTestContext()

	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Service temporarily unavailable. Please retry later."}}`,
		``,
		`data: {"type":"message_stop"}`,
		``,
	}, "\n"))

	err := svc.processClaudeStream(ctx, body)
	require.Error(t, err)
	output := rec.Body.String()
	require.Contains(t, output, `"type":"error"`)
	require.Contains(t, output, claudeTestUnavailableMessage)
	require.NotContains(t, output, `"type":"test_complete"`)
}

func TestAccountTestService_ProcessClaudeStreamSuccessStillCompletes(t *testing.T) {
	svc := &AccountTestService{}
	ctx, rec := newTestContext()

	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello from claude"}}`,
		``,
		`data: {"type":"message_stop"}`,
		``,
	}, "\n"))

	err := svc.processClaudeStream(ctx, body)
	require.NoError(t, err)
	output := rec.Body.String()
	require.Contains(t, output, `"type":"content"`)
	require.Contains(t, output, `"type":"test_complete"`)
	require.Contains(t, output, `"success":true`)
	require.NotContains(t, output, `"type":"error"`)
}

func TestIsClaudeTestUnavailableResponse(t *testing.T) {
	require.True(t, isClaudeTestUnavailableResponse("Service temporarily unavailable. Please retry later."))
	require.True(t, isClaudeTestUnavailableResponse("  service temporarily unavailable. please retry later.  "))
	require.False(t, isClaudeTestUnavailableResponse("hello"))
	require.False(t, isClaudeTestUnavailableResponse("Service temporarily unavailable"))
}
