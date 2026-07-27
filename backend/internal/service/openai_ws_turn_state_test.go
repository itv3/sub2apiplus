package service

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
)

func newOpenAIWSLeaseWithHandshakeTurnState(value string) *openAIWSConnLease {
	headers := http.Header{}
	if value != "" {
		headers.Set(openAIWSTurnStateHeader, value)
	}
	return &openAIWSConnLease{conn: &openAIWSConn{handshakeHeaders: headers}}
}

// turn-state 是上游按连接下发的粘性路由令牌，官方契约要求只在同一 turn 内保持。
// 新建连接的握手若未下发该头，必须解析为空串，把上一条连接的残留值一并清除；
// 若退回"仅在非空时覆盖"，就会把旧连接的 turn-state 回放到新连接上。
func TestReplaceOpenAIWSTurnStateFromLeaseDoesNotCarryOverAcrossConnections(t *testing.T) {
	connA := newOpenAIWSLeaseWithHandshakeTurnState("  ts-conn-a  ")
	require.Equal(t, "ts-conn-a", replaceOpenAIWSTurnStateFromLease(connA))

	connB := newOpenAIWSLeaseWithHandshakeTurnState("")
	require.Empty(
		t,
		replaceOpenAIWSTurnStateFromLease(connB),
		"新连接未下发 turn-state 时必须解析为空，不得沿用上一条连接的值",
	)

	connC := newOpenAIWSLeaseWithHandshakeTurnState("ts-conn-c")
	require.Equal(t, "ts-conn-c", replaceOpenAIWSTurnStateFromLease(connC))
}

func TestReplaceOpenAIWSTurnStateFromLeaseHandlesMissingConn(t *testing.T) {
	require.Empty(t, replaceOpenAIWSTurnStateFromLease(nil))
	require.Empty(t, replaceOpenAIWSTurnStateFromLease(&openAIWSConnLease{}))
}

// 官方从流内 response.metadata 事件的 headers 对象取 turn-state（大小写不敏感），
// 握手响应头那条路在官方 CLI 里是死代码。此前只认握手头导致长期取到空串。
func TestExtractOpenAIWSTurnStateFromUpstreamEvent(t *testing.T) {
	cases := []struct {
		name    string
		payload string
		want    string
	}{
		{
			name:    "标准事件",
			payload: `{"type":"response.metadata","headers":{"x-codex-turn-state":"ts-from-event"}}`,
			want:    "ts-from-event",
		},
		{
			name:    "header 名大小写不敏感",
			payload: `{"type":"response.metadata","headers":{"X-Codex-Turn-State":"ts-upper"}}`,
			want:    "ts-upper",
		},
		{
			name:    "非 metadata 事件不提取",
			payload: `{"type":"response.completed","headers":{"x-codex-turn-state":"ts-ignored"}}`,
			want:    "",
		},
		{
			name:    "缺少 headers 对象",
			payload: `{"type":"response.metadata"}`,
			want:    "",
		},
		{
			name:    "headers 不是对象",
			payload: `{"type":"response.metadata","headers":"not-an-object"}`,
			want:    "",
		},
		{
			name:    "空载荷",
			payload: ``,
			want:    "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, extractOpenAIWSTurnStateFromUpstreamEvent([]byte(tc.payload)))
		})
	}
}
