package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"

	coderws "github.com/coder/websocket"
)

// 跨模型旧会话“上游拒绝密文 → 客户端重连并重发原历史”的完整失败链回归（真实 WS 入站，假上游终端）：
//
//	下游 WS 入站 → 兼容处理（清 reasoning.content）→ 上游 invalid_encrypted_content 拒绝 → 网关按错误片段精确记录被拒项
//	→ 客户端重连、重发同一份原历史 → 进场清理（只清被拒项）→ 出站业务历史校验（认可同一份快照下的清理）→ 假上游。
//
// 修复前，进场清理只改待发送帧，校验仍拿客户端原帧比对，判成 input was modified 并以 1008 关连接，客户端反复重连反复失败。
// 每个场景都在两套画像下各跑一遍（上一版 Active 与目标版本画像）。这些用例替换包级画像入口，不得并行。
// 样本是脱敏的结构样例，不含真实会话内容。

// recoveryWireHarness 是带假上游的真实 WS 入站环境。假上游对携带“被标记密文”的业务帧回 invalid_encrypted_content，
// 其余帧正常完成；所有上游帧都留在终端的握手记录里，供断言。
type recoveryWireHarness struct {
	t      *testing.T
	server *codexGateWireServer
	gate   *codexGateService

	mu sync.Mutex
	// reject 是“出现即被上游拒绝”的密文 → 上游错误文案。
	reject map[string]string
	// lifecycleFirst 为 true 时拒绝前先回 response.created（线上日志里错误事件前确有生命周期事件）。
	lifecycleFirst bool
	// outputFirst 为 true 时拒绝前先输出文字增量（模拟已经向客户端输出过内容）。
	outputFirst bool
	// dropOnce 为 true 时，下一个带历史的业务帧到达时关掉所有上游连接且不应答（模拟断线，触发网关换连接重试）。
	dropOnce bool
}

func newRecoveryWireHarness(t *testing.T, mode string) *recoveryWireHarness {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newTargetAwareGate(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	h := &recoveryWireHarness{t: t, server: server, gate: gate, reject: map[string]string{}, lifecycleFirst: true}
	server.setWebSocketResponder(h.respond)
	return h
}

func (h *recoveryWireHarness) rejectCipher(value string, message string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.reject[value] = message
}

func (h *recoveryWireHarness) clearRejects() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.reject = map[string]string{}
}

func (h *recoveryWireHarness) setOutputFirst(enabled bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.outputFirst = enabled
}

func (h *recoveryWireHarness) armDropOnce() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.dropOnce = true
}

// respond 是假上游的帧应答：先看是否需要模拟断线，再看业务帧里有没有被标记的密文，否则按默认完成。
func (h *recoveryWireHarness) respond(request codexGateWireRequest, frame codexGateWireFrame) [][]byte {
	if codexGateWebSocketFrameType(frame) != "response.create" {
		return nil
	}
	input := gjson.GetBytes(frame.payload, "input")
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.dropOnce && gjson.GetBytes(frame.payload, "generate").Type != gjson.False && len(input.Array()) > 0 {
		h.dropOnce = false
		h.server.mu.Lock()
		for open := range h.server.open {
			_ = open.Close()
		}
		h.server.mu.Unlock()
		return nil
	}
	message := ""
	input.ForEach(func(_, item gjson.Result) bool {
		if value := item.Get("encrypted_content"); value.Type == gjson.String {
			if rejected, hit := h.reject[value.String()]; hit {
				message = rejected
				return false
			}
		}
		return true
	})
	if message == "" {
		return codexGateDefaultWebSocketResponse(request, frame)
	}
	quoted, _ := json.Marshal(message)
	responseID := "resp_rejected_" + strconv.Itoa(request.connection) + "_" + strconv.Itoa(len(request.frames))
	var events [][]byte
	if h.lifecycleFirst || h.outputFirst {
		events = append(events, []byte(`{"type":"response.created","response":{"id":"`+responseID+`","object":"response","status":"in_progress"}}`))
	}
	if h.outputFirst {
		events = append(events, []byte(`{"type":"response.output_text.delta","delta":"已经输出的文字"}`))
	}
	events = append(events,
		[]byte(`{"type":"error","status":400,"error":{"type":"invalid_request_error","code":"invalid_encrypted_content","message":`+string(quoted)+`}}`),
		[]byte(`{"type":"response.failed","response":{"id":"`+responseID+`","status":"failed","error":{"code":"invalid_encrypted_content","message":`+string(quoted)+`}}}`),
	)
	return events
}

// start 建立一条下游 WS 入站会话（等价 startWebSocketIngress，但可指定账号，供跨账号隔离用例）。
func (h *recoveryWireHarness) start(t *testing.T, account *Account, header http.Header, first []byte) *codexGateWebSocketIngress {
	t.Helper()
	ingress := &codexGateWebSocketIngress{done: make(chan error, 1)}
	apiKey := codexGateIngressAPIKey()
	wsServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		conn, err := coderws.Accept(writer, request, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			ingress.done <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		ginContext, _ := gin.CreateTestContext(httptest.NewRecorder())
		cloned := request.Clone(request.Context())
		cloned.Header = request.Header.Clone()
		for name, values := range header {
			cloned.Header.Del(name)
			for _, value := range values {
				cloned.Header.Add(name, value)
			}
		}
		cloned.URL.Path = "/v1/responses"
		ginContext.Request = cloned
		ginContext.Set("api_key", apiKey)
		ctx := h.gate.ingress(ginContext)
		readCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			ingress.done <- readErr
			return
		}
		ingress.done <- h.gate.service.ProxyResponsesWebSocketFromClient(ctx, ginContext, conn, account, "oauth-test-token", firstMessage, nil)
	}))
	t.Cleanup(wsServer.Close)
	dialCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), nil)
	require.NoError(t, err)
	t.Cleanup(func() { _ = client.CloseNow() })
	ingress.client = client
	ingress.send(t, first)
	return ingress
}

// otherAccount 返回与网关账号同源但编号不同的账号（模拟调度把同一会话交给另一个账号）。
func (h *recoveryWireHarness) otherAccount(delta int64) *Account {
	return newOfficialOpenAIHTTPTestAccount(h.gate.account.ID + delta)
}

// recoveryClientTurn 是客户端在一轮里看到的结果。
type recoveryClientTurn struct {
	eventTypes  []string
	errorEvent  []byte
	completedID string
	failed      bool
	// closed 表示读到了连接被服务端关闭；closeStatus 是服务端以 OpenAIWSClientCloseError 结束时的关闭码（否则为 0）。
	closed      bool
	closeStatus coderws.StatusCode
}

// recoveryReadTurn 读取下游事件，直到 response.completed、response.failed 或连接关闭。
func recoveryReadTurn(t *testing.T, ws *codexGateWebSocketIngress) recoveryClientTurn {
	t.Helper()
	var turn recoveryClientTurn
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, message, err := ws.client.Read(ctx)
		cancel()
		if err != nil {
			turn.closed = true
			select {
			case serverErr := <-ws.done:
				var closeErr *OpenAIWSClientCloseError
				if errors.As(serverErr, &closeErr) {
					turn.closeStatus = closeErr.StatusCode()
				}
				ws.done <- serverErr // 放回，留给 close 等待
			case <-time.After(3 * time.Second):
				t.Fatalf("读下游事件失败（%v）且服务端会话未结束", err)
			}
			return turn
		}
		eventType := gjson.GetBytes(message, "type").String()
		turn.eventTypes = append(turn.eventTypes, eventType)
		switch eventType {
		case "error":
			turn.errorEvent = append([]byte(nil), message...)
		case "response.completed":
			turn.completedID = gjson.GetBytes(message, "response.id").String()
			return turn
		case "response.failed":
			turn.failed = true
			return turn
		}
	}
}

// recoveryBusinessFrame 是上游终端收到的一个带历史的业务帧（预热帧 input 为空，不在此列）。
type recoveryBusinessFrame struct {
	connection int
	raw        []byte
}

func (f recoveryBusinessFrame) has(cipher string) bool {
	return strings.Contains(string(f.raw), cipher)
}

// text 返回帧里全部 JSON 字符串值（已反转义）拼成的文本，用来断言中文正文等是否仍在帧里。
func (f recoveryBusinessFrame) text() string {
	var decoded any
	if err := json.Unmarshal(f.raw, &decoded); err != nil {
		return string(f.raw)
	}
	var out strings.Builder
	var walk func(value any)
	walk = func(value any) {
		switch typed := value.(type) {
		case string:
			_, _ = out.WriteString(typed)
			_ = out.WriteByte('\n')
		case []any:
			for _, item := range typed {
				walk(item)
			}
		case map[string]any:
			for _, item := range typed {
				walk(item)
			}
		}
	}
	walk(decoded)
	return out.String()
}

func (h *recoveryWireHarness) businessFrames() []recoveryBusinessFrame {
	var out []recoveryBusinessFrame
	for index, handshake := range codexGateWebSocketHandshakes(h.server) {
		for _, frame := range codexGateResponseCreateFrames(handshake) {
			// 预热帧（generate=false）的 input 里只有基础指令，不带历史，不算业务帧。
			if gjson.GetBytes(frame, "generate").Type == gjson.False {
				continue
			}
			if len(gjson.GetBytes(frame, "input").Array()) > 0 {
				out = append(out, recoveryBusinessFrame{connection: index + 1, raw: frame})
			}
		}
	}
	return out
}

// recordedDigests 返回某账号名下全部恢复记录（不含恢复次数计数用的伪键）的摘要并集。
func (h *recoveryWireHarness) recordedDigests(accountID int64) map[string]struct{} {
	store, ok := h.gate.service.getOpenAIWSStateStore().(*defaultOpenAIWSStateStore)
	require.True(h.t, ok)
	store.sessionInvalidEncryptedMu.RLock()
	defer store.sessionInvalidEncryptedMu.RUnlock()
	out := map[string]struct{}{}
	infix := "|ws-ingress-recovery|account=" + strconv.FormatInt(accountID, 10) + "|"
	for key, binding := range store.sessionInvalidEncrypted {
		if !strings.Contains(key, infix) || strings.HasSuffix(key, openAIWSEncryptedRecoveryBudgetSuffix) {
			continue
		}
		for digest := range binding.digests {
			out[digest] = struct{}{}
		}
	}
	return out
}

// scopedKey 返回某账号的恢复记录键（要求恰有一个）。
func (h *recoveryWireHarness) scopedKey(accountID int64) string {
	store, ok := h.gate.service.getOpenAIWSStateStore().(*defaultOpenAIWSStateStore)
	require.True(h.t, ok, "状态存储类型应为 defaultOpenAIWSStateStore")
	store.sessionInvalidEncryptedMu.RLock()
	defer store.sessionInvalidEncryptedMu.RUnlock()
	infix := "|ws-ingress-recovery|account=" + strconv.FormatInt(accountID, 10) + "|"
	var found []string
	for key := range store.sessionInvalidEncrypted {
		if strings.Contains(key, infix) && !strings.HasSuffix(key, openAIWSEncryptedRecoveryBudgetSuffix) {
			found = append(found, key)
		}
	}
	require.Len(h.t, found, 1)
	// 存储里的键是“分组编号:会话键”，这里返回会话键部分（即 openAIWSEncryptedRecoveryScopeKey 的值）。
	_, sessionKey, ok := strings.Cut(found[0], ":")
	require.True(h.t, ok)
	return sessionKey
}

// scopedKeyCount 统计全部恢复作用域键（不含恢复次数计数用的伪键）。
func (h *recoveryWireHarness) scopedKeyCount() int {
	store, ok := h.gate.service.getOpenAIWSStateStore().(*defaultOpenAIWSStateStore)
	require.True(h.t, ok, "状态存储类型应为 defaultOpenAIWSStateStore")
	store.sessionInvalidEncryptedMu.RLock()
	defer store.sessionInvalidEncryptedMu.RUnlock()
	count := 0
	for key := range store.sessionInvalidEncrypted {
		if strings.Contains(key, "|ws-ingress-recovery|") && !strings.HasSuffix(key, openAIWSEncryptedRecoveryBudgetSuffix) {
			count++
		}
	}
	return count
}

// rawKeyCount 统计不带恢复作用域的记录键数（上游原有“分组 + 会话”键）。
func (h *recoveryWireHarness) rawKeyCount() int {
	store, ok := h.gate.service.getOpenAIWSStateStore().(*defaultOpenAIWSStateStore)
	require.True(h.t, ok, "状态存储类型应为 defaultOpenAIWSStateStore")
	store.sessionInvalidEncryptedMu.RLock()
	defer store.sessionInvalidEncryptedMu.RUnlock()
	count := 0
	for key := range store.sessionInvalidEncrypted {
		if !strings.Contains(key, "|ws-ingress-recovery|") {
			count++
		}
	}
	return count
}

func recoveryRaw(item string) json.RawMessage { return json.RawMessage(item) }

// recoveryFrame 构造客户端的整段历史帧：同一个 promptCacheKey 即同一个会话。
func recoveryFrame(t *testing.T, promptCacheKey string, items ...any) []byte {
	t.Helper()
	return codexGateWSIngressFrame(t, "gpt-6.1-sol", "", "", items, map[string]any{"prompt_cache_key": promptCacheKey})
}

// recoveryColdHistory 是旧会话恢复出的完整历史：A 是别家模型留下的推理项（带 content 数组，密文 GPT 无法验证），
// B 是有效的 GPT 推理项（也带 content），另有配对的工具调用与输出、助手消息。
func recoveryColdHistory(extra ...any) []any {
	items := []any{
		replayBaseItem(),
		codexGateMessage("developer", "developer context"),
		codexGateMessage("user", "第一问"),
		recoveryRaw(recoveryReasoningItemWithContent("A")),
		map[string]any{"type": "message", "role": "assistant", "content": []any{map[string]any{"type": "output_text", "text": "第一答"}}},
		map[string]any{"type": "function_call", "id": "fc_sample_0001", "call_id": "call_sample_0001", "name": "lookup", "arguments": `{"q":"one"}`},
		map[string]any{"type": "function_call_output", "call_id": "call_sample_0001", "output": "ok"},
		recoveryRaw(recoveryReasoningItemWithContent("B")),
	}
	items = append(items, extra...)
	return append(items, codexGateMessage("user", "第二问"))
}

func recoveryHeader() http.Header {
	return http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
}

// recoveryRequireRecoveredFrame 断言成功的那一帧：被拒项 A 的密文与 content 都没了、骨架（摘要）保留；
// 有效项 B 的密文原样、只做第一轮的 content 清理；消息、工具调用与输出原样。
func recoveryRequireRecoveredFrame(t *testing.T, frame recoveryBusinessFrame) {
	t.Helper()
	require.False(t, frame.has(recoveryCipher("A")), "被拒项的密文已被清理")
	require.True(t, frame.has(recoveryCipher("B")), "有效项的密文原样保留")
	require.Contains(t, frame.text(), "摘要-A", "被拒项只清密文，推理骨架（摘要）保留")
	require.NotContains(t, frame.text(), "别家模型的可见推理", "第一轮：content 数组已清理")
	for _, want := range []string{"第一问", "第二问", "第一答", "call_sample_0001", "developer context"} {
		require.Contains(t, frame.text(), want)
	}
	gjson.GetBytes(frame.raw, "input").ForEach(func(_, item gjson.Result) bool {
		require.False(t, item.Get("content.0.type").String() == "reasoning_text", "不应残留 reasoning_text：%s", item.Raw)
		return true
	})
}

// 1) 完整失败链：恢复完整历史 → 上游拒绝被拒项 → 客户端重连并重发原历史 → 进场清理与校验按同一规则完成 → 成功。
// 同时覆盖：只记被拒的一项（有效项不被整包标坏）、组合清理（content + 密文）、不再 1008、不伪造 response.completed。
func TestOpenAIWSInvalidEncryptedContentFullFailureChainRecoversOnReconnect(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			h := newRecoveryWireHarness(t, mode)
			bad := recoveryCipher("A")
			h.rejectCipher(bad, recoveryHintMessage(bad))
			frame := recoveryFrame(t, "pck-chain", recoveryColdHistory()...)

			logs := captureOpenAIWSRecoveryLogs(t, func() {
				// 第一次：整段历史被上游拒绝，客户端收到明确的错误事件，没有 response.completed，连接没有被 1008 关闭。
				first := h.start(t, h.gate.account, recoveryHeader(), frame)
				turn := recoveryReadTurn(t, first)
				require.NotEmpty(t, turn.errorEvent, "客户端收到上游的明确错误事件")
				require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(turn.errorEvent, "error.code").String())
				require.Empty(t, turn.completedID, "不伪造 response.completed")
				require.False(t, turn.closed, "错误事件之后连接不是被裸关闭")
				require.Equal(t, coderws.StatusCode(0), turn.closeStatus)
				first.close(t)
				require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID), "只记被拒的那一项，有效项与其余密文不被标坏")
				require.Zero(t, h.rawKeyCount(), "不写上游原有的“分组 + 会话”键")

				// 重连并重发同一份原历史：进场只清被拒项，校验按同一份快照放行，成功完成。
				second := h.start(t, h.gate.account, recoveryHeader(), frame)
				retry := recoveryReadTurn(t, second)
				require.Empty(t, retry.errorEvent, "重连后不再被拒")
				require.NotEmpty(t, retry.completedID, "重连后完成压缩前的这一轮")
				require.False(t, retry.closed)
				second.close(t)
			})

			frames := h.businessFrames()
			require.GreaterOrEqual(t, len(frames), 2)
			require.True(t, frames[0].has(bad), "首次发往上游的帧原样带着被拒密文")
			recoveryRequireRecoveredFrame(t, frames[len(frames)-1])
			// 定位日志：请求编号、账号、模型、阶段、命中摘要数、结果都在，且没有密文、片段、会话正文。
			for _, want := range []string{"stage=upstream_reject", "account_id=" + strconv.FormatInt(h.gate.account.ID, 10),
				"model=gpt-6.1-sol", "matched=1", "marked=1", "result=recorded", "reason=fragment", "stage=entry", "known_digests=1", "stripped_items="} {
				require.Contains(t, logs, want)
			}
			// 全部日志里不得出现完整密文或会话正文；本次新增的恢复日志里也不得出现错误文案回显的“首...尾”片段
			// （上游原有的 ingress_ws_error_event 日志会回显该片段，不在本次改动范围内）。
			for _, secret := range []string{bad, recoveryCipher("B"), "第一问", "第二问", "摘要-A"} {
				require.NotContains(t, logs, secret)
			}
			for _, line := range strings.Split(logs, "\n") {
				if strings.Contains(line, "ingress_ws_encrypted_recovery") {
					require.NotContains(t, line, bad[:4]+"..."+bad[len(bad)-4:])
				}
			}
		})
	}
}

// 2) 有效项与失效项混在一起：24 条有效 GPT 密文加 1 条失效项，只记失效项，重连后 24 条有效密文原样发往上游。
func TestOpenAIWSInvalidEncryptedContentMixedValidAndInvalidKeepsValidItems(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	extra := make([]any, 0, 24)
	for index := 0; index < 24; index++ {
		extra = append(extra, recoveryRaw(recoveryReasoningItem("G"+strconv.Itoa(index))))
	}
	frame := recoveryFrame(t, "pck-mixed", recoveryColdHistory(extra...)...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
	first.close(t)
	require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID))

	second := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, second).completedID)
	second.close(t)

	frames := h.businessFrames()
	last := frames[len(frames)-1]
	recoveryRequireRecoveredFrame(t, last)
	for index := 0; index < 24; index++ {
		require.True(t, last.has(recoveryCipher("G"+strconv.Itoa(index))), "有效推理项 G%d 的密文不被误清", index)
	}
}

// 3) 自动压缩：整段历史末项是 compaction_trigger（官方压缩请求形态），被拒后重连重发仍按同一规则恢复。
func TestOpenAIWSInvalidEncryptedContentRecoversAutoCompactionRequest(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			h := newRecoveryWireHarness(t, mode)
			bad := recoveryCipher("A")
			h.rejectCipher(bad, recoveryHintMessage(bad))
			history := append(recoveryColdHistory(), map[string]any{"type": "compaction_trigger"})
			frame := recoveryFrame(t, "pck-compact", history...)

			first := h.start(t, h.gate.account, recoveryHeader(), frame)
			require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
			first.close(t)
			require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID))

			second := h.start(t, h.gate.account, recoveryHeader(), frame)
			turn := recoveryReadTurn(t, second)
			require.NotEmpty(t, turn.completedID, "自动压缩请求恢复后完成：%v", turn.eventTypes)
			second.close(t)
			frames := h.businessFrames()
			last := frames[len(frames)-1]
			require.False(t, last.has(bad))
			require.True(t, last.has(recoveryCipher("B")))
			require.Contains(t, last.text(), "compaction_trigger")
		})
	}
}

// 4) 断线重试：恢复后的帧在新连接上被上游断开，网关换连接重试同一帧；重试用的是清理后的帧，校验仍以客户端原帧为基准放行。
func TestOpenAIWSInvalidEncryptedContentRecoveryThenUpstreamDisconnectRetry(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			h := newRecoveryWireHarness(t, mode)
			bad := recoveryCipher("A")
			h.rejectCipher(bad, recoveryHintMessage(bad))
			frame := recoveryFrame(t, "pck-retry", recoveryColdHistory()...)

			first := h.start(t, h.gate.account, recoveryHeader(), frame)
			require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
			first.close(t)

			h.armDropOnce()
			second := h.start(t, h.gate.account, recoveryHeader(), frame)
			turn := recoveryReadTurn(t, second)
			require.NotEmpty(t, turn.completedID, "断线重试后仍完成：%v closeStatus=%d", turn.eventTypes, turn.closeStatus)
			require.Equal(t, coderws.StatusCode(0), turn.closeStatus, "没有 1008")
			second.close(t)

			frames := h.businessFrames()
			require.GreaterOrEqual(t, len(frames), 3, "拒绝帧、被断开的帧、重试帧")
			for _, retried := range frames[1:] {
				require.False(t, retried.has(bad), "恢复后的帧（含被断开的与重试的）都不再带被拒密文")
			}
			recoveryRequireRecoveredFrame(t, frames[len(frames)-1])
		})
	}
}

// 5) 跨账号与跨会话隔离：A 号上记录的被拒项，换账号或换会话后不会被套用（原样发往上游，由该账号/会话自己的拒绝来定位）。
func TestOpenAIWSInvalidEncryptedContentRecoveryIsolatedByAccountAndSession(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	frame := recoveryFrame(t, "pck-iso-1", recoveryColdHistory()...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
	first.close(t)
	require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID))
	before := len(h.businessFrames())

	// 换账号：同一会话、同一份历史，B 号读不到 A 号的记录，被拒密文原样发往上游，再次被拒（而不是被悄悄清掉）。
	other := h.otherAccount(100)
	viaOther := h.start(t, other, recoveryHeader(), frame)
	turn := recoveryReadTurn(t, viaOther)
	require.NotEmpty(t, turn.errorEvent, "换号后不复用 A 号的清理记录")
	require.Equal(t, coderws.StatusCode(0), turn.closeStatus)
	viaOther.close(t)
	frames := h.businessFrames()
	require.True(t, frames[before].has(bad), "换号后被拒密文原样发往上游")
	require.Equal(t, recoveryDigests(bad), h.recordedDigests(other.ID), "B 号按自己的拒绝记录自己的作用域")
	require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID), "A 号的记录没有变化")

	// 换会话：同一账号、另一个会话，同样读不到。
	before = len(h.businessFrames())
	otherSession := recoveryFrame(t, "pck-iso-2", recoveryColdHistory()...)
	viaSession := h.start(t, h.gate.account, recoveryHeader(), otherSession)
	require.NotEmpty(t, recoveryReadTurn(t, viaSession).errorEvent, "换会话后不复用清理记录")
	viaSession.close(t)
	require.True(t, h.businessFrames()[before].has(bad))
	// 三条记录各在自己的作用域键下：A 号会话一、B 号会话一、A 号会话二；彼此不串。
	require.Equal(t, 3, h.scopedKeyCount())
}

// 6) 压缩项：被拒的就是加密压缩项时，不得静默删除——不记录、重发时原样带着，客户端每次都收到明确错误，从不 1008、从不 completed。
func TestOpenAIWSInvalidEncryptedContentCompactionItemIsNeverSilentlyDropped(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	badCompaction := recoveryCipher("C")
	h.rejectCipher(badCompaction, recoveryHintMessage(badCompaction))
	frame := recoveryFrame(t, "pck-cmp-bad", recoveryColdHistory(recoveryRaw(recoveryCompactionItem("C")))...)

	for attempt := 1; attempt <= 3; attempt++ {
		ws := h.start(t, h.gate.account, recoveryHeader(), frame)
		turn := recoveryReadTurn(t, ws)
		require.NotEmpty(t, turn.errorEvent, "第 %d 次：客户端收到明确错误", attempt)
		require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(turn.errorEvent, "error.code").String())
		require.Empty(t, turn.completedID)
		require.Equal(t, coderws.StatusCode(0), turn.closeStatus, "不是裸关连接")
		ws.close(t)
	}
	require.Empty(t, h.recordedDigests(h.gate.account.ID), "压缩项被拒：不记录任何清理")
	frames := h.businessFrames()
	require.Len(t, frames, 3)
	for _, sent := range frames {
		require.True(t, sent.has(badCompaction), "压缩项每次都原样发往上游，从未被静默丢弃")
		require.True(t, sent.has(recoveryCipher("A")), "没有任何整包清理")
	}
}

// 7) 压缩项与恢复并存：历史里有一条有效的加密压缩项，被拒的是推理项——推理项被清，压缩项完整保留。
func TestOpenAIWSInvalidEncryptedContentRecoveryKeepsValidCompactionItem(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	frame := recoveryFrame(t, "pck-cmp-ok", recoveryColdHistory(recoveryRaw(recoveryCompactionItem("C")))...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
	first.close(t)
	require.Equal(t, recoveryDigests(bad), h.recordedDigests(h.gate.account.ID))

	second := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, second).completedID)
	second.close(t)
	frames := h.businessFrames()
	last := frames[len(frames)-1]
	recoveryRequireRecoveredFrame(t, last)
	require.True(t, last.has(recoveryCipher("C")), "有效的加密压缩项完整保留")
	require.Contains(t, last.text(), "compaction")
}

// 8) 已输出后的边界：上游在已经向客户端输出文字之后才返回 invalid_encrypted_content，不是纯输入校验失败——不记录，重发原样发往上游。
func TestOpenAIWSInvalidEncryptedContentAfterOutputIsNotRecorded(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	h.setOutputFirst(true)
	frame := recoveryFrame(t, "pck-output", recoveryColdHistory()...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	turn := recoveryReadTurn(t, first)
	require.Contains(t, turn.eventTypes, "response.output_text.delta", "前提：客户端已经收到输出")
	require.NotEmpty(t, turn.errorEvent)
	first.close(t)
	require.Empty(t, h.recordedDigests(h.gate.account.ID), "已输出过内容：不推断，不记录")

	second := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, second).errorEvent)
	second.close(t)
	frames := h.businessFrames()
	require.Len(t, frames, 2)
	require.True(t, frames[1].has(bad), "没有记录就没有清理：重发的帧原样带着密文")
}

// 9) 无法唯一定位（上游文案不带密文片段）：允许一次有边界的整包推理项恢复；用掉之后同一会话不再整包恢复，
// 客户端收到明确错误，不陷入“清理 → 本地校验失败”的循环。
func TestOpenAIWSInvalidEncryptedContentUnlocatedRecoveryIsBoundedToOnce(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, "The encrypted content could not be verified. Reason: Encrypted content could not be decrypted or parsed.")
	frame := recoveryFrame(t, "pck-unlocated", recoveryColdHistory()...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
	first.close(t)
	require.Equal(t, recoveryDigests(bad, recoveryCipher("B")), h.recordedDigests(h.gate.account.ID), "无法定位：只允许整包推理项恢复（这里是两条推理项）")

	// 整包推理项被清理，校验按同一份快照放行，成功。
	second := h.start(t, h.gate.account, recoveryHeader(), frame)
	turn := recoveryReadTurn(t, second)
	require.NotEmpty(t, turn.completedID, "%v", turn.eventTypes)
	second.close(t)
	last := h.businessFrames()
	require.False(t, last[len(last)-1].has(bad))
	require.False(t, last[len(last)-1].has(recoveryCipher("B")), "整包恢复清掉了请求里全部推理项的密文（有边界的代价）")

	// 同一会话再次遇到无法定位的拒绝：整包恢复已用掉，不再记录；客户端收到明确错误，不是 1008。
	fresh := recoveryCipher("Z")
	h.clearRejects()
	h.rejectCipher(fresh, "The encrypted content could not be verified.")
	again := recoveryFrame(t, "pck-unlocated",
		replayBaseItem(), codexGateMessage("user", "新问"),
		recoveryRaw(recoveryReasoningItem("Z")), recoveryRaw(recoveryReasoningItem("Y")), codexGateMessage("user", "再问"))
	for attempt := 1; attempt <= 2; attempt++ {
		ws := h.start(t, h.gate.account, recoveryHeader(), again)
		retry := recoveryReadTurn(t, ws)
		require.NotEmpty(t, retry.errorEvent, "第 %d 次：明确错误", attempt)
		require.Empty(t, retry.completedID)
		require.Equal(t, coderws.StatusCode(0), retry.closeStatus)
		ws.close(t)
	}
	require.Equal(t, recoveryDigests(bad, recoveryCipher("B")), h.recordedDigests(h.gate.account.ID), "整包恢复已用掉：不再扩大记录")
}

// 10) 保护：进场清理要删压缩项（记录里混进了压缩项摘要）时，出站校验拒绝——不静默丢掉压缩项；
// 客户端先收到明确的错误事件（含中文提示），再被关闭连接；上游从未收到缺了压缩项的帧。
func TestOpenAIWSInvalidEncryptedContentValidationFailureNotifiesClientBeforeClose(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad := recoveryCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	frame := recoveryFrame(t, "pck-validate", recoveryColdHistory(recoveryRaw(recoveryCompactionItem("C")))...)

	first := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, first).errorEvent)
	first.close(t)
	key := h.scopedKey(h.gate.account.ID)
	// 模拟记录被污染：压缩项 C 的摘要混进了本会话的恢复记录（正常路径不会写入压缩项摘要）。
	h.gate.service.getOpenAIWSStateStore().MarkSessionInvalidEncryptedContent(3, key, []string{openAIEncryptedContentDigest(recoveryCipher("C"))}, time.Minute)
	before := len(h.businessFrames())

	logs := captureOpenAIWSRecoveryLogs(t, func() {
		second := h.start(t, h.gate.account, recoveryHeader(), frame)
		turn := recoveryReadTurn(t, second)
		require.NotEmpty(t, turn.errorEvent, "关闭前先给客户端一个明确的错误事件")
		require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(turn.errorEvent, "error.code").String())
		require.Contains(t, gjson.GetBytes(turn.errorEvent, "error.message").String(), "无法安全恢复")
		require.Empty(t, turn.completedID, "不伪造 response.completed")
		require.True(t, turn.closed)
		require.Equal(t, coderws.StatusPolicyViolation, turn.closeStatus, "校验拒绝仍以 1008 关闭连接")
		second.close(t)
	})
	require.Len(t, h.businessFrames(), before, "缺了压缩项的帧从未发往上游")
	require.Contains(t, logs, "stage=validate")
	require.Contains(t, logs, "result=unrecoverable")
}

// 11) 普通新会话与没有被拒项的会话不受影响：没有恢复记录时一切照旧，帧原样（只做第一轮的 content 清理）发往上游。
func TestOpenAIWSInvalidEncryptedContentPlainSessionUnaffected(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	frame := recoveryFrame(t, "pck-plain", recoveryColdHistory()...)
	ws := h.start(t, h.gate.account, recoveryHeader(), frame)
	require.NotEmpty(t, recoveryReadTurn(t, ws).completedID)
	ws.close(t)
	frames := h.businessFrames()
	require.Len(t, frames, 1)
	require.True(t, frames[0].has(recoveryCipher("A")))
	require.True(t, frames[0].has(recoveryCipher("B")))
	require.Empty(t, h.recordedDigests(h.gate.account.ID))
}

// 12) 片段同时吻合多项（历史里 3 个推理候选，错误片段吻合其中 2 个）：只记吻合的 2 个；重连重发后这 2 项的密文被清，
// 未吻合的有效推理项 B 与不吻合的加密压缩项 C 原样发往上游，进场清理与出站校验用同一份快照，不 1008、不伪造 completed。
func TestOpenAIWSInvalidEncryptedContentAmbiguousFragmentOnlyRecoversMatchedItems(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			h := newRecoveryWireHarness(t, mode)
			bad, twin := recoveryCipher("A"), recoverySiblingCipher("A")
			h.rejectCipher(bad, recoveryHintMessage(bad))
			// 历史：A（被拒）、B（有效）、A2（与 A 首尾相同的另一条推理密文，同样吻合片段）、C（有效的加密压缩项，不吻合）。
			frame := recoveryFrame(t, "pck-ambiguous", recoveryColdHistory(
				recoveryRaw(recoveryReasoningItemWithCipher("A2", twin)),
				recoveryRaw(recoveryCompactionItem("C")),
			)...)

			first := h.start(t, h.gate.account, recoveryHeader(), frame)
			turn := recoveryReadTurn(t, first)
			require.NotEmpty(t, turn.errorEvent, "客户端收到上游的明确错误事件")
			require.Empty(t, turn.completedID)
			require.Equal(t, coderws.StatusCode(0), turn.closeStatus)
			first.close(t)
			require.Equal(t, recoveryDigests(bad, twin), h.recordedDigests(h.gate.account.ID), "只记片段吻合的两项，有效项 B 与压缩项 C 不被标坏")

			second := h.start(t, h.gate.account, recoveryHeader(), frame)
			retry := recoveryReadTurn(t, second)
			require.Empty(t, retry.errorEvent, "重连后不再被拒")
			require.NotEmpty(t, retry.completedID, "校验按同一份快照放行：%v closeStatus=%d", retry.eventTypes, retry.closeStatus)
			require.Equal(t, coderws.StatusCode(0), retry.closeStatus, "没有 1008")
			second.close(t)

			frames := h.businessFrames()
			require.GreaterOrEqual(t, len(frames), 2)
			require.True(t, frames[0].has(bad) && frames[0].has(twin), "首次发往上游的帧原样带着两条吻合的密文")
			last := frames[len(frames)-1]
			recoveryRequireRecoveredFrame(t, last) // A 的密文已清、B 的密文原样、中文正文与工具调用原样
			require.False(t, last.has(twin), "吻合的另一项 A2 的密文也已清理")
			require.Contains(t, last.text(), "摘要-A2", "A2 只清密文，推理骨架保留")
			require.True(t, last.has(recoveryCipher("C")), "不吻合的加密压缩项原样保留")
			require.Contains(t, last.text(), "compaction")
		})
	}
}

// 13) 吻合的几项里有压缩项：不可恢复，不记录；重发时两条吻合的密文（含压缩项）原样发往上游，客户端每次都收到明确错误，从不 1008、从不 completed。
func TestOpenAIWSInvalidEncryptedContentAmbiguousFragmentWithMatchedCompactionIsNeverRecovered(t *testing.T) {
	mode := officialClientProfileModeActive
	withoutTargetProfileForWire(t, mode)
	h := newRecoveryWireHarness(t, mode)
	bad, twin := recoveryCipher("A"), recoverySiblingCipher("A")
	h.rejectCipher(bad, recoveryHintMessage(bad))
	frame := recoveryFrame(t, "pck-ambiguous-cmp", recoveryColdHistory(
		recoveryRaw(recoveryCompactionItemWithCipher("A2", twin)),
	)...)

	for attempt := 1; attempt <= 3; attempt++ {
		ws := h.start(t, h.gate.account, recoveryHeader(), frame)
		turn := recoveryReadTurn(t, ws)
		require.NotEmpty(t, turn.errorEvent, "第 %d 次：客户端收到明确错误", attempt)
		require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(turn.errorEvent, "error.code").String())
		require.Empty(t, turn.completedID)
		require.Equal(t, coderws.StatusCode(0), turn.closeStatus, "不是裸关连接")
		ws.close(t)
	}
	require.Empty(t, h.recordedDigests(h.gate.account.ID), "吻合项里有压缩项：不记录任何清理")
	frames := h.businessFrames()
	require.Len(t, frames, 3)
	for _, sent := range frames {
		require.True(t, sent.has(bad) && sent.has(twin), "两条吻合的密文每次都原样发往上游，未被静默丢弃")
		require.True(t, sent.has(recoveryCipher("B")), "没有任何整包清理")
	}
}
