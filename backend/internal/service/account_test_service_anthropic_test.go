//go:build unit

package service

import (
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/stretchr/testify/require"
)

func TestAccountTestService_AnthropicAPIKeyBetaHeaderFollowsMimicSetting(t *testing.T) {
	tests := []struct {
		name        string
		mimic       bool
		wantHeader  string
		wantContext bool
	}{
		{
			name:        "开启兼容时使用官方客户端完整 beta 列表",
			mimic:       true,
			wantHeader:  strings.Join(claude.APIKeyMimicBetas(), ","),
			wantContext: true,
		},
		{
			name:        "关闭兼容时保持普通 API Key beta 列表",
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
			svc := &AccountTestService{
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

			err := svc.testClaudeAccountConnection(ctx, account, "claude-sonnet-4-6")
			require.NoError(t, err)
			require.Len(t, upstream.requests, 1)

			betaHeader := upstream.requests[0].Header.Get("anthropic-beta")
			require.Equal(t, tt.wantHeader, betaHeader)
			if tt.wantContext {
				require.Contains(t, betaHeader, claude.BetaContext1M)
			} else {
				require.NotContains(t, betaHeader, claude.BetaContext1M)
			}
		})
	}
}
