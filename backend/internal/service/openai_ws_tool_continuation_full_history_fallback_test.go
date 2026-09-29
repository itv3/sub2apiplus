package service

import (
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestApplyOfficialOpenAIWSFullHistoryFallback(t *testing.T) {
	active := &officialOpenAIWSDerivedState{}
	active.setFullHistoryFallback(true)
	inactive := &officialOpenAIWSDerivedState{}
	noPrevious := map[string]any{"type": "response.create"}
	withPrevious := map[string]any{"type": "response.create", "previous_response_id": "resp_old"}

	cases := []struct {
		name         string
		payload      map[string]any
		state        *officialOpenAIWSDerivedState
		hasCurrent   bool
		reliable     bool
		wantCurrent  bool
		wantReliable bool
	}{
		{name: "未打标记时可靠判定保持原样", payload: noPrevious, state: inactive, hasCurrent: true, reliable: true, wantCurrent: true, wantReliable: true},
		{name: "未打标记保持失败关闭", payload: noPrevious, state: inactive, hasCurrent: true, reliable: false, wantCurrent: true, wantReliable: false},
		{name: "打了标记时帧整理后的可靠判定也按历史处理", payload: noPrevious, state: active, hasCurrent: true, reliable: true, wantCurrent: false, wantReliable: true},
		{name: "没有会话状态保持失败关闭", payload: noPrevious, state: nil, hasCurrent: true, reliable: false, wantCurrent: true, wantReliable: false},
		{name: "打了标记且没有续链锚点时按历史处理", payload: noPrevious, state: active, hasCurrent: true, reliable: false, wantCurrent: false, wantReliable: true},
		{name: "带续链锚点时不兜底", payload: withPrevious, state: active, hasCurrent: true, reliable: false, wantCurrent: true, wantReliable: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotCurrent, gotReliable := applyOfficialOpenAIWSFullHistoryFallback(tc.payload, tc.state, tc.hasCurrent, tc.reliable)
			require.Equal(t, tc.wantCurrent, gotCurrent)
			require.Equal(t, tc.wantReliable, gotReliable)
		})
	}

	active.setFullHistoryFallback(false)
	require.False(t, active.fullHistoryFallbackActive(), "每轮判定都会重写标记，清除后不得残留")
}

// openAIWSFullHistoryFallbackFrame 模拟断线重连后 Codex 重发的完整历史：同一用户轮内有两次工具往返，
// 开头带开发者上下文（真实 Codex 帧都有，新连接的预热帧只承载这部分），没有逐项 turn_id，
// 也没有 previous_response_id。第一条工具输出之后还有工具调用、之前也有未标注轮次
// 的工具调用，逐项判定无法确定哪些输出属于本轮。referenceOnly 为 true 时第二次工具调用只以
// item_reference 出现：入站兼容处理会保留它，但帧内没有实际的工具调用项，历史不算完整。
// （孤立输出会被入站兼容处理直接删掉，到不了轮次判定，所以不用它构造不完整历史。）
func openAIWSFullHistoryFallbackFrame(referenceOnly bool) []byte {
	secondCall := `{"type":"function_call","call_id":"call_fh_2","name":"shell","arguments":"{\"command\":[\"pwd\"]}"},`
	if referenceOnly {
		secondCall = `{"type":"item_reference","id":"call_fh_2"},`
	}
	return []byte(`{"type":"response.create","model":"gpt-5.5","stream":true,"store":false,"input":[` +
		`{"type":"message","role":"developer","content":[{"type":"input_text","text":"developer context"}]},` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"run tools"}]},` +
		`{"type":"function_call","call_id":"call_fh_1","name":"shell","arguments":"{\"command\":[\"ls\"]}"},` +
		`{"type":"function_call_output","call_id":"call_fh_1","output":"a.txt"},` +
		secondCall +
		`{"type":"function_call_output","call_id":"call_fh_2","output":"/tmp"}` +
		`]}`)
}

// startOpenAIWSFullHistoryFallbackSession 在真实 wire 的派生官方出口链路上，以断线重连的新会话发出首帧。
func startOpenAIWSFullHistoryFallbackSession(
	t *testing.T,
	fallbackEnabled bool,
	first []byte,
) (*codexGateWireServer, *codexGateWebSocketIngress) {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, officialClientProfileModeActive, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	gate.service.cfg.Gateway.OpenAIWS.ToolContinuationFullHistoryFallbackEnabled = fallbackEnabled
	header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
	return server, gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(), first)
}

// requireOpenAIWSAmbiguousClose 断言入站会话以 1008 “续接不明确”结束，且没有任何业务帧发往上游。
func requireOpenAIWSAmbiguousClose(t *testing.T, server *codexGateWireServer, ws *codexGateWebSocketIngress) {
	t.Helper()
	select {
	case serverErr := <-ws.done:
		var closeErr *OpenAIWSClientCloseError
		require.True(t, errors.As(serverErr, &closeErr), "会话应以客户端关闭错误结束，实际 %v", serverErr)
		require.Equal(t, coderws.StatusPolicyViolation, closeErr.StatusCode())
		require.Contains(t, closeErr.Reason(), "tool continuation turn is ambiguous")
	case <-time.After(5 * time.Second):
		t.Fatal("等待入站会话以 1008 结束超时")
	}
	for _, handshake := range codexGateWebSocketHandshakes(server) {
		require.Empty(t, codexGateResponseCreateFrames(handshake), "续接不明确时不得向上游发出任何 response.create")
	}
}

func TestOpenAIWSToolContinuationFullHistoryFallbackOpensNewChainOnReconnect(t *testing.T) {
	server, ws := startOpenAIWSFullHistoryFallbackSession(t, true, openAIWSFullHistoryFallbackFrame(false))
	responseID := ws.readUntilCompleted(t)
	ws.close(t)

	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, 1, "重连后的完整历史由一条新连接承接")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 2, "新连接上先发预热帧，再发业务帧")

	prewarm, business := frames[0], frames[1]
	require.Equal(t, gjson.False, gjson.GetBytes(prewarm, "generate").Type, "首帧必须是 generate=false 的预热帧")
	require.False(t, gjson.GetBytes(prewarm, "previous_response_id").Exists(), "预热帧不携带 previous_response_id")

	prewarmResponseID := fmt.Sprintf("resp_gate_ws_%d_1", handshakes[0].connection)
	require.Equal(t, prewarmResponseID, gjson.GetBytes(business, "previous_response_id").String(),
		"业务帧只续接新连接自己的预热响应，不续接任何旧连接上的响应")
	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_2", handshakes[0].connection), responseID)

	// 完整历史原样转发：工具调用与工具输出一条不少，不按“本轮新增输出”裁剪。
	callIDsByType := map[string][]string{}
	for _, item := range gjson.GetBytes(business, "input").Array() {
		itemType := item.Get("type").String()
		if callID := item.Get("call_id").String(); callID != "" {
			callIDsByType[itemType] = append(callIDsByType[itemType], callID)
		}
	}
	require.Equal(t, []string{"call_fh_1", "call_fh_2"}, callIDsByType["function_call"])
	require.Equal(t, []string{"call_fh_1", "call_fh_2"}, callIDsByType["function_call_output"])
	require.Equal(t, "call_fh_2", gjson.GetBytes(business, "input.@reverse.0.call_id").String(),
		"最后一项仍是待回传的工具结果")
}

func TestOpenAIWSToolContinuationFullHistoryFallbackRejectsIncompleteHistory(t *testing.T) {
	server, ws := startOpenAIWSFullHistoryFallbackSession(t, true, openAIWSFullHistoryFallbackFrame(true))
	requireOpenAIWSAmbiguousClose(t, server, ws)
}

func TestOpenAIWSToolContinuationFullHistoryFallbackDisabledKeepsFailClose(t *testing.T) {
	server, ws := startOpenAIWSFullHistoryFallbackSession(t, false, openAIWSFullHistoryFallbackFrame(false))
	requireOpenAIWSAmbiguousClose(t, server, ws)
}
