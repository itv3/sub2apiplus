package handler

import (
	"bytes"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexBootstrapPreflightOrdinaryHistoryDoesNotAllocate(t *testing.T) {
	body := []byte(`{"model":"gpt-5","input":[{"type":"function_call_output","name":"exec","call_id":"call-1","output":"` + strings.Repeat("x", 2<<20) + `"}]}`)
	// 正文在测量前构造；无注入候选的长历史不应复制 input 或解码大段 output。
	allocations := testing.AllocsPerRun(5, func() {
		if codexBootstrapMayNeedNormalization(body) {
			panic("普通工具输出被误判为注入候选")
		}
	})
	require.Zero(t, allocations)
	got, changed := normalizeCodexDelegationBootstrap(body)
	require.False(t, changed)
	require.Equal(t, &body[0], &got[0])
}

func TestCodexBootstrapPreflightPreservesEscapedCandidates(t *testing.T) {
	tests := []struct {
		name      string
		body      []byte
		normalize func([]byte) ([]byte, bool)
	}{
		{
			name:      "delegation",
			body:      []byte(`{"model":"gpt-5","input":[{"type":"function_call_output","namespace":"codex_app","name":"create_thread","output":"` + delegationEnvelope + `"}]}`),
			normalize: normalizeCodexDelegationBootstrap,
		},
		{
			name:      "automation",
			body:      codexAutomationBootstrapBody(t, `<heartbeat><automation_id>wiki</automation_id></heartbeat>`, ""),
			normalize: normalizeCodexAutomationBootstrap,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// 键名和判别值均可合法转义，预检不得因此跳过原本支持的候选。
			body := tt.body
			for _, pair := range [][2]string{
				{`"input"`, `"\u0069nput"`},
				{`"type"`, `"\u0074ype"`},
				{`"namespace"`, `"\u006eamespace"`},
				{`"name"`, `"\u006eame"`},
				{`function_call_output`, `function_call_\u006futput`},
				{`codex_app`, `codex_\u0061pp`},
				{`create_thread`, `create_\u0074hread`},
				{`automation_update`, `automation_\u0075pdate`},
			} {
				body = bytes.ReplaceAll(body, []byte(pair[0]), []byte(pair[1]))
			}
			require.True(t, codexBootstrapMayNeedNormalization(body))
			got, changed := tt.normalize(body)
			require.True(t, changed)
			require.Equal(t, "message", gjson.GetBytes(got, "input.0.type").String())
			require.Equal(t, "user", gjson.GetBytes(got, "input.0.role").String())
		})
	}
}
