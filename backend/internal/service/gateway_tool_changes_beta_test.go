//go:build unit

package service

import (
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

// 本分支已退休 Claude OAuth 旧 Messages 构造链（buildUpstreamRequest 对 OAuth 直接报错，官方出站走 strict
// ReleaseBundle），上游用例通过 buildUpstreamRequest 间接验证 mimic 路径的 beta 保留规则；这里改为直接验证
// 承载该规则的 computeFinalAnthropicBeta（上游新增：mimic 路径显式保留 mid-conversation-tool-changes 与
// inline-tools 两个兼容 token，未知 beta 仍被排除，策略过滤优先）。
func TestComputeFinalAnthropicBetaToolChangeBetas(t *testing.T) {
	const mid = "mid-conversation-tool-changes-2026-07-01"
	const inline = "inline-tools-2026-09-15"
	for _, tc := range []struct {
		name, header        string
		drop                map[string]struct{}
		wantMid, wantInline bool
	}{
		{"both", mid + "," + inline, nil, true, true},
		{"mid only", mid, nil, true, false},
		{"inline only", inline, nil, false, true},
		{"absent", "", nil, false, false},
		{"unknown", inline + "-other", nil, false, false},
		{"duplicate", inline + ", " + inline + "," + mid, nil, true, true},
		{"filter inline", mid + "," + inline, map[string]struct{}{inline: {}}, true, false},
		{"filter mid", mid + "," + inline, map[string]struct{}{mid: {}}, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clientHeaders := http.Header{}
			clientHeaders.Set("anthropic-beta", tc.header+",unrelated-beta")
			body := []byte(`{"model":"claude-opus-5-5","max_tokens":1024,"messages":[{"role":"user","content":[{"type":"tool_addition","tool":{"type":"tool_reference","tool_name":"lookup"}},{"type":"text","text":"Use lookup"}]}]}`)
			svc := &GatewayService{cfg: &config.Config{}}
			header, shouldSet := svc.computeFinalAnthropicBeta("oauth", true, "claude-opus-5-5", clientHeaders, body, tc.drop)
			require.True(t, shouldSet)
			require.Equal(t, tc.wantMid, containsBetaToken(header, mid))
			require.Equal(t, tc.wantInline, containsBetaToken(header, inline))
			require.False(t, containsBetaToken(header, "unrelated-beta"))
			require.False(t, containsBetaToken(header, inline+"-other"))
			require.True(t, containsBetaToken(header, "oauth-2025-04-20"))
			if tc.wantInline {
				require.Equal(t, 1, strings.Count(header, inline))
			}
		})
	}
}
