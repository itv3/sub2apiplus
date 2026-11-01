package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestSyncBillingHeaderVersion(t *testing.T) {
	tests := []struct {
		name      string
		body      string
		userAgent string
		wantSub   string // substring expected in result
		unchanged bool   // expect body to remain the same
	}{
		{
			name:      "replaces cc_version and recomputes message-derived suffix",
			body:      `{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.81.df2; cc_entrypoint=cli; cch=00000;"},{"type":"text","text":"You are Claude Code.","cache_control":{"type":"ephemeral"}}],"messages":[]}`,
			userAgent: "claude-cli/2.1.22 (external, cli)",
			wantSub:   "cc_version=2.1.22." + computeClaudeCodeFingerprint([]byte(`{"messages":[]}`), "2.1.22"),
		},
		{
			name:      "no billing header in system",
			body:      `{"system":[{"type":"text","text":"You are Claude Code."}],"messages":[]}`,
			userAgent: "claude-cli/2.1.22",
			unchanged: true,
		},
		{
			name:      "no system field",
			body:      `{"messages":[]}`,
			userAgent: "claude-cli/2.1.22",
			unchanged: true,
		},
		{
			name:      "user-agent without version",
			body:      `{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.81; cc_entrypoint=cli; cch=00000;"}],"messages":[]}`,
			userAgent: "Mozilla/5.0",
			unchanged: true,
		},
		{
			name:      "empty user-agent",
			body:      `{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.81; cc_entrypoint=cli; cch=00000;"}],"messages":[]}`,
			userAgent: "",
			unchanged: true,
		},
		{
			name:      "version already matches",
			body:      `{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.22; cc_entrypoint=cli; cch=00000;"}],"messages":[]}`,
			userAgent: "claude-cli/2.1.22",
			unchanged: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := syncBillingHeaderVersion([]byte(tt.body), tt.userAgent)
			if tt.unchanged {
				assert.Equal(t, tt.body, string(result), "body should remain unchanged")
			} else {
				assert.Contains(t, string(result), tt.wantSub)
				// Ensure old semver is gone
				assert.NotContains(t, string(result), "cc_version=2.1.81")
			}
		})
	}
}

func TestSyncBillingHeaderVersion_RecomputesSuffixAndIsIdempotent(t *testing.T) {
	body := []byte(`{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.81.df2; cc_entrypoint=cli;"}],"messages":[{"role":"user","content":"hello world"}]}`)
	version := "2.1.22"
	result := syncBillingHeaderVersion(body, "claude-cli/"+version)
	require.Contains(t, gjson.GetBytes(result, "system.0.text").String(),
		"cc_version="+version+"."+computeClaudeCodeFingerprint(body, version)+";")
	require.Equal(t, string(result), string(syncBillingHeaderVersion(result, "claude-cli/"+version)))
	require.JSONEq(t, gjson.GetBytes(body, "messages").Raw, gjson.GetBytes(result, "messages").Raw)
}

// Claude OAuth 的旧 Messages/count_tokens 构造入口已退休；账单头不能成为旁路。
func TestBuildOAuthRequest_BillingLegacyBuildersFailClose(t *testing.T) {
	account := &Account{ID: 1, Platform: PlatformAnthropic, Type: AccountTypeOAuth}
	body := []byte(`{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hello world"}]}`)
	svc := &GatewayService{}

	_, wireBody, err := svc.buildUpstreamRequest(
		context.Background(), nil, account, body, "test-token", "oauth", "claude-haiku-4-5", false, true,
	)
	require.Nil(t, wireBody)
	require.ErrorContains(t, err, "旧 Messages 构造链已退休")

	_, wireBody, err = svc.buildCountTokensRequest(
		context.Background(), nil, account, body, "test-token", "oauth", "claude-haiku-4-5", true,
	)
	require.Nil(t, wireBody)
	require.ErrorContains(t, err, "旧 count_tokens 构造链已退休")
}

func TestSyncBillingHeaderVersion_UnicodeSuffix(t *testing.T) {
	for _, initialVersion := range []string{"2.1.81.df2", "2.1.283.000", "2.1.283.c7e"} {
		t.Run(initialVersion, func(t *testing.T) {
			body := []byte(`{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=` + initialVersion + `; cc_entrypoint=cli;"},{"type":"text","text":"Keep this prompt."}],"messages":[{"role":"user","content":"这是一个用于验证中文指纹计算的测试消息，请保持一致。"}],"unknown":{"keep":true}}`)
			result := syncBillingHeaderVersion(body, "claude-cli/2.1.283")
			// 期望值来自独立 Node.js 计算，不能由待测函数反算。
			require.Equal(t, "x-anthropic-billing-header: cc_version=2.1.283.c7e; cc_entrypoint=cli;", gjson.GetBytes(result, "system.0.text").String())
			require.Equal(t, result, syncBillingHeaderVersion(result, "claude-cli/2.1.283"))
			if initialVersion == "2.1.283.c7e" {
				require.Equal(t, body, result)
			}
			for _, path := range []string{"messages", "system.1", "unknown"} {
				require.Equal(t, gjson.GetBytes(body, path).Raw, gjson.GetBytes(result, path).Raw)
			}
		})
	}
}
