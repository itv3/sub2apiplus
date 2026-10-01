package service

import (
	"crypto/sha256"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// Responses WebSocket 受影响规则的晋升后门禁。
//
// 两条生产入口：第三方客户端的 HTTP 入站经 Forward 走上游 WS（forwardViaWebSocket），
// 官方客户端的 WS 入站经 ProxyResponsesWebSocketFromClient 转发（startWebSocketIngress）。
// 上游握手由生产 coder/websocket 发起、tlsfingerprint 按画像 swap_remove 重写，帧与
// ClientHello 都在本地终端的真实字节上观测。
// ============================================================================

// codexGateWebSocketHandshakes 返回终端上全部 WS 握手（按到达顺序）。
func codexGateWebSocketHandshakes(server *codexGateWireServer) []codexGateWireRequest {
	var out []codexGateWireRequest
	for _, request := range server.wireRequests() {
		if request.webSocket {
			out = append(out, request)
		}
	}
	return out
}

// codexGateWebSocketFixedPrefix 是 tungstenite 硬编码的五个握手头（大小写即 wire 字面量）。
var codexGateWebSocketFixedPrefix = []string{"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key"}

// codexGateSwapRemoveRemaining 按画像声明的线序模式（ws_fixed_prefix_then_header_map_swap_remove）
// 从 HeaderMap 插入序推导前缀之后的线序：先按插入序过滤出本次出现的 header，再依次对
// 固定前缀做 swap_remove（被删位置由末尾元素补位），最后追加删除之后才生成的 header。
// 它只复述画像声明的算法，用来对照真实 wire，不参与生产出站。
func codexGateSwapRemoveRemaining(
	insertion []string,
	present map[string]bool,
	prefix []string,
	appendHeaders []string,
) []string {
	var filtered []string
	for _, name := range insertion {
		if present[strings.ToLower(name)] {
			filtered = append(filtered, strings.ToLower(name))
		}
	}
	for _, name := range prefix {
		index := slices.Index(filtered, strings.ToLower(name))
		if index < 0 {
			continue
		}
		last := len(filtered) - 1
		filtered[index] = filtered[last]
		filtered = filtered[:last]
	}
	for _, name := range appendHeaders {
		if present[strings.ToLower(name)] {
			filtered = append(filtered, strings.ToLower(name))
		}
	}
	return filtered
}

// SPEC-WS-002（change）：WS 前五项之后均为小写且按 swap_remove 结果输出。
//
// 检查项与网关观测：
//   - remaining-lowercase：真实握手的前五项保持 tungstenite 字面量，其后 header 名全部小写；
//   - default-swap-remove-order：默认完整握手的剩余线序是批准允许全集的有序子集并含全部
//     必需项，openai-beta 位于 x-codex-routing-hint 之后；Cookie jar 非空时 cookie 紧跟
//     前五项（目标画像把 cookie 追加在插入序末尾，swap_remove 把它换到最前）；
//   - optional-missing-covered：保留可选条件缺席的独立握手样本——jar 为空时没有 cookie，
//     下游从不发送 beta 头时 x-codex-beta-features 依然存在。
//
// 本例中 jar 由上一次握手 101 应答的 Set-Cookie 写回（画像 CookieJar 节声明握手写回），
// 下一次握手即携带该 Cookie，覆盖“jar 非空”的真实形成过程。
func TestCodexWebSocketHandshakeOrderReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "responses_ws 声明 cookie 条件槽位并排在插入序末尾",
		func(profile profilecontract.ExecutableProfile) bool {
			endpoint, ok := codexGateEndpoint(profile, officialCodexEndpointResponsesWS)
			slot, has := codexGateHeaderSlot(endpoint, "cookie")
			insertion := endpoint.HeaderMapInsertionOrder
			return ok && has && slot.Condition == profilecontract.ConditionCookiePresent &&
				len(insertion) > 0 && insertion[len(insertion)-1] == "cookie"
		})
	profile := codexGateExecutableProfile(t, mode)
	section := profile.Optional().CookieJar
	require.NotNil(t, section)
	require.True(t, section.WebSocketHandshakeWriteBack, "目标画像声明 WS 握手 Set-Cookie 写回同一 jar")
	responsesWS := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS)
	var fixedPrefix []string
	for _, transport := range profile.Transports() {
		if transport.ID == responsesWS.TransportID && transport.WebSocket != nil {
			fixedPrefix = transport.WebSocket.FixedHandshakePrefix
		}
	}
	require.Len(t, fixedPrefix, 5)
	allowed := []string{
		"cookie", "chatgpt-account-id", "authorization", "user-agent", "originator", "version",
		"x-codex-beta-features", "x-client-request-id", "session-id", "thread-id", "x-codex-window-id",
		"x-codex-turn-metadata", "x-codex-routing-hint", "openai-beta", "sec-websocket-extensions",
	}
	required := allowed[1:]

	server := startCodexGateWireServer(t, nil)
	server.setUpgradeHeader(http.Header{"Set-Cookie": []string{"__cf_bm=promotion-gate-ws; Path=/; Secure; HttpOnly"}})
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)

	requireHandshake := func(handshake codexGateWireRequest, where string) []string {
		t.Helper()
		require.Equal(t, codexGateWebSocketFixedPrefix, handshake.headerNames[:5], "%s 的前五项必须保持官方字面量", where)
		remaining := codexGateRemainingHandshakeNames(handshake)
		present := map[string]bool{}
		for _, name := range handshake.lowerHeaderNames() {
			present[name] = true
		}
		for _, name := range remaining {
			require.Equal(t, strings.ToLower(name), name, "%s 前五项之后的 header 名必须小写：%s", where, name)
		}
		require.Equal(t,
			codexGateSwapRemoveRemaining(responsesWS.HeaderMapInsertionOrder, present, fixedPrefix, responsesWS.PostRemoveHeaders),
			remaining, "%s 的剩余线序必须等于目标画像插入序经 swap_remove 的结果", where)
		return remaining
	}

	// 可选条件缺席：jar 为空、下游不发 beta 头。
	first, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err)
	firstRemaining := requireHandshake(first, "jar 为空的握手")
	require.NotContains(t, firstRemaining, "cookie", "jar 为空时不得发送 cookie")
	require.Equal(t, []string{"remote_compaction_v2"}, first.values("x-codex-beta-features"),
		"可选条件缺席时 x-codex-beta-features 依然存在")

	// 默认完整握手：jar 已由上一次 101 的 Set-Cookie 写回。
	second, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err)
	secondRemaining := requireHandshake(second, "jar 非空的握手")
	codexGateRequireOrderedSubset(t, secondRemaining, allowed, required, "默认完整握手的剩余线序")
	require.Equal(t, "cookie", secondRemaining[0], "jar 非空时 cookie 必须紧跟前五项")
	require.Greater(t, slices.Index(secondRemaining, "openai-beta"), slices.Index(secondRemaining, "x-codex-routing-hint"),
		"默认完整握手的 openai-beta 必须位于 x-codex-routing-hint 之后")
	require.Equal(t, []string{"__cf_bm=promotion-gate-ws"}, second.values("cookie"),
		"握手 Cookie 来自上一次握手写回 jar 的值")
}

// SPEC-TLS-003（change）：WS ClientHello 扩展集合相同但顺序不是固定常量；signature_algorithms
// 在原 10 项后追加 ML-DSA-44/65/87（2308、2309、2310）。
//
// 检查项与网关观测：extension-order-diversity——8 次相互独立的上层调用各自建立新的 WS
// 连接，本地终端记录的 ClientHello 扩展集合全部相同，且至少出现两种不同排列（画像声明
// 扩展随机化）；每次 ClientHello 的 signature_algorithms 恰为画像 WS 传输的 13 项（原 10 项
// 加末尾三项 ML-DSA），SNI 为 chatgpt.com。
func TestCodexWebSocketClientHelloReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mldsa := []uint16{2308, 2309, 2310}
	mode := codexGateTargetReleaseMode(t, "WS 传输的 signature_algorithms 末尾追加 ML-DSA-44/65/87",
		func(profile profilecontract.ExecutableProfile) bool {
			endpoint, ok := codexGateEndpoint(profile, officialCodexEndpointResponsesWS)
			if !ok {
				return false
			}
			for _, transport := range profile.Transports() {
				if transport.ID == endpoint.TransportID {
					algorithms := transport.SignatureAlgorithms
					return len(algorithms) == 13 && slices.Equal(algorithms[10:], mldsa)
				}
			}
			return false
		})
	profile := codexGateExecutableProfile(t, mode)
	var wsTransport profilecontract.ExecutableTransportProfile
	for _, transport := range profile.Transports() {
		if transport.ID == codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS).TransportID {
			wsTransport = transport
		}
	}
	require.Len(t, wsTransport.SignatureAlgorithms, 13)
	require.Equal(t, []uint16{1283, 1027, 1539, 2055, 2054, 2053, 2052, 1537, 1281, 1025}, wsTransport.SignatureAlgorithms[:10],
		"原 10 项签名算法保持不变")
	require.Equal(t, mldsa, wsTransport.SignatureAlgorithms[10:], "ML-DSA-44/65/87 追加在原 10 项之后")
	require.True(t, wsTransport.RandomizeExtensions, "WS 传输声明扩展顺序随机化")

	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	const captures = 8
	for index := 0; index < captures; index++ {
		_, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
		require.NoError(t, err)
	}
	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, captures, "每次独立调用都建立新的 WS 连接")
	var extensionSet []uint16
	orders := map[string]bool{}
	for index, handshake := range handshakes {
		hello := codexGateHelloFor(t, server, handshake)
		require.Equal(t, "chatgpt.com", hello.serverName)
		require.Equal(t, wsTransport.SignatureAlgorithms, hello.signatureSchemes,
			"第 %d 次 WS ClientHello 的 signature_algorithms 必须逐项等于画像（含末尾 ML-DSA）", index+1)
		require.Equal(t, mldsa, hello.signatureSchemes[len(hello.signatureSchemes)-3:],
			"第 %d 次 WS ClientHello 必须在末尾携带 ML-DSA-44/65/87", index+1)
		sorted := slices.Clone(hello.extensions)
		slices.Sort(sorted)
		if extensionSet == nil {
			extensionSet = sorted
		}
		require.Equal(t, extensionSet, sorted, "第 %d 次 WS ClientHello 的扩展集合必须一致", index+1)
		orders[fmt.Sprint(hello.extensions)] = true
	}
	require.GreaterOrEqual(t, len(orders), 2, "%d 次独立握手的扩展顺序不得是固定常量", captures)
}

// codexGateSlowDownEvent 是上游 WS 流内的 slow_down 容量降载错误事件。
var codexGateSlowDownEvent = []byte(`{"type":"error","error":{"type":"server_error","code":"slow_down","message":"Slow down"}}`)

// SPEC-PROTO-002（condition_change）：Responses 默认 WS，预算耗尽后同一调用降级 HTTP。
//
// 检查项与网关观测：
//   - default-websocket：第三方 HTTP 入站的 Responses 按画像默认走上游 WS，终端只收到 WS
//     握手与帧、没有 HTTP Responses 请求，传输序列以 websocket 开头；
//   - fallback-after-exhaustion：上游每次都以 slow_down（画像 WebSocketRetry 节的可重试码）
//     拒绝时，网关按节中预算重连 WS，预算耗尽后以 HTTP 结束——WS 尝试恰为 RetryBudget+1 次，
//     最后一条是 HTTP /responses，重试预算耗尽计数加一；
//   - same-invocation：全部 WS 尝试与 HTTP 降级属于同一上层调用——入口层调用 ID 与 Executor
//     层调用 ID 分别一致，attempt 原因依次为 initial、reconnect…、fallback。
//
// 对照：另一槽位没有 WebSocketRetry 节，同样的 slow_down 不按画像预算降级 HTTP。
func TestCodexWebSocketFallbackReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "WebSocketRetry 节把 slow_down 计入预算并降级 HTTP",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().WebSocketRetry
			return section != nil && slices.Contains(section.RetryableErrorCodes, "slow_down") &&
				section.FallbackTransport == "http"
		})
	section := codexGateExecutableProfile(t, mode).Optional().WebSocketRetry
	require.NotNil(t, section, "目标画像必须声明 WebSocketRetry 节")
	require.True(t, codexGateExecutableProfile(t, mode).Features().SupportsWebSockets)

	// default-websocket。
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	normal, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err)
	require.NotEmpty(t, codexGateResponseCreateFrames(normal))
	require.Empty(t, server.httpRequestsForPath(codexGateResponsesPath), "正常场景不得出现 HTTP Responses")

	// fallback-after-exhaustion 与 same-invocation。
	runSlowDown := func(mode string) (*codexGateService, *codexGateWebSocketDialer, *codexGateWireServer, error) {
		slowServer := startCodexGateWireServer(t, nil)
		slowServer.setWebSocketResponder(func(_ codexGateWireRequest, frame codexGateWireFrame) [][]byte {
			if codexGateWebSocketFrameType(frame) == "response.create" {
				return [][]byte{codexGateSlowDownEvent}
			}
			return nil
		})
		slowGate := newCodexGateService(t, mode, slowServer)
		codexGateUseRecorderManifest(slowGate.service, slowGate.account)
		dialer := slowGate.enableWebSocket(t, slowServer)
		_, forwardErr := slowGate.forwardViaWebSocket(t, slowServer, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
		return slowGate, dialer, slowServer, forwardErr
	}
	slowGate, dialer, slowServer, err := runSlowDown(mode)
	require.NoError(t, err, "预算耗尽后必须以 HTTP 完成本次调用")
	dials := dialer.snapshot()
	require.Len(t, dials, section.RetryBudget+1, "WS 尝试次数必须等于画像预算 + 1")
	fallback := slowServer.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, fallback, 1, "预算耗尽后必须以一条 HTTP Responses 结束")
	require.EqualValues(t, 1, slowGate.service.SnapshotOpenAIWSRetryMetrics().RetryExhaustedTotal)
	httpAttempts := slowGate.upstream.attemptIdentities()
	httpEgress := slowGate.upstream.egressInvocationIDs()
	require.Len(t, httpAttempts, 1)
	for index, dial := range dials {
		require.NoError(t, dial.err, "WS 第 %d 次握手本身必须成功（slow_down 发生在流内）", index+1)
		require.Equal(t, dials[0].egressInvocationID, dial.egressInvocationID, "WS 第 %d 次尝试的入口调用 ID 必须一致", index+1)
		require.Equal(t, httpAttempts[0].InvocationID, dial.attempt.InvocationID,
			"WS 第 %d 次尝试与 HTTP 降级必须属于同一 Executor 调用", index+1)
		wantReason := officialegress.AttemptReasonReconnect
		if index == 0 {
			wantReason = officialegress.AttemptReasonInitial
		}
		require.Equal(t, string(wantReason), dial.attempt.AttemptReason)
	}
	require.NotEmpty(t, dials[0].egressInvocationID)
	require.Equal(t, dials[0].egressInvocationID, httpEgress[0], "HTTP 降级与 WS 尝试属于同一入口调用")
	require.Equal(t, string(officialegress.AttemptReasonFallback), httpAttempts[0].AttemptReason)

	// 对照：另一槽位没有该节，slow_down 不按画像预算降级。
	if other, ok := codexGateControlReleaseMode(t, mode); ok {
		_, controlDialer, controlServer, _ := runSlowDown(other)
		require.Less(t, len(controlDialer.snapshot()), section.RetryBudget+1, "对照槽位不按画像预算重连")
		require.Empty(t, controlServer.httpRequestsForPath(codexGateResponsesPath), "对照槽位的 slow_down 不降级 HTTP")
	}
}

// codexGateIngressFrame 构造官方客户端 WS 模式的一帧 response.create。
func codexGateIngressFrame(previousResponseID string, texts ...string) []byte {
	var input []string
	for index, text := range texts {
		role := "user"
		if index == 0 && strings.HasPrefix(text, "developer:") {
			role, text = "developer", strings.TrimPrefix(text, "developer:")
		}
		input = append(input, `{"type":"message","role":"`+role+`","content":[{"type":"input_text","text":"`+text+`"}]}`)
	}
	previous := ""
	if previousResponseID != "" {
		previous = `"previous_response_id":"` + previousResponseID + `",`
	}
	return []byte(`{"type":"response.create","model":"gpt-5.5","stream":true,"store":false,` + previous +
		`"input":[` + strings.Join(input, ",") + `]}`)
}

// codexGateIngressAPIKey 是 WS 入站会话的 API Key（分组大于零，turn-state 存储键依赖它）。
func codexGateIngressAPIKey() *APIKey {
	groupID := int64(3)
	return &APIKey{ID: 1, UserID: 1, GroupID: &groupID, Group: &Group{ID: groupID}}
}

// SPEC-WS-005（condition_change）：response.create 字段遵循固定槽位和条件省略。
//
// 检查项与网关观测（官方客户端 WS 入站，真实 wire 帧）：
//   - field-slots：连接上的每一帧 response.create（预热、首轮、增量、全量重放）顶层字段都
//     只来自画像冻结槽位且按序出现，并含全部必需字段；
//   - warmup-generate-false：连接建立后的预热帧 generate 为 false，业务帧不带 generate=false；
//   - incremental-prefix-reuse：wire 上每个携带 previous_response_id 的帧，续接的都是同一条
//     连接上紧邻的上一个响应（预热响应或上一轮响应）；客户端以上一轮响应续接时 wire 保留它，
//     客户端全量重放时由新连接承接、只续接新连接自己的预热响应，绝不续接旧连接上的响应；
//     previous_response_id 与连接上最近的响应不一致（前缀不可复用）时失败关闭，不把它发往上游。
//
// 条件变化来自目标画像新增的 WebSocketContinuation 节：ResetOn 含 auth_revision 等条件，
// 认证代次变化必须让连接池键变化（新连接、旧续接失效），账号 owner 变化同理；另一槽位
// 没有该节，认证代次不进入连接池键。
func TestCodexResponseCreateSlotsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "WebSocketContinuation 节声明 auth_revision 等续接失效条件",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().WebSocketContinuation
			return section != nil && slices.Contains(section.ResetOn, "auth_revision")
		})
	profile := codexGateExecutableProfile(t, mode)
	require.NotNil(t, profile.Optional().WebSocketContinuation, "目标画像必须声明 WebSocketContinuation 节")
	require.Equal(t, []string{"account_owner", "account_routing_override", "auth_revision", "base_url", "late_tool_result_metadata"},
		profile.Optional().WebSocketContinuation.ResetOn)
	allowed := []string{
		"type", "model", "instructions", "previous_response_id", "input", "tools", "tool_choice",
		"parallel_tool_calls", "reasoning", "store", "stream", "stream_options", "include", "service_tier",
		"prompt_cache_key", "text", "generate", "client_metadata",
	}
	required := []string{"type", "model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include"}
	responsesWS := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS)
	var slotNames, requiredNames []string
	for _, field := range responsesWS.Body.Fields {
		slotNames = append(slotNames, field.Name)
		if field.Required {
			requiredNames = append(requiredNames, field.Name)
		}
	}
	require.Equal(t, allowed, slotNames, "responses_ws 的冻结槽位必须与批准的字段序一致")
	require.ElementsMatch(t, required, requiredNames)

	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
	ws := gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(),
		codexGateIngressFrame("", "developer:developer context", "turn one"))
	turnOne := ws.readUntilCompleted(t)
	ws.send(t, codexGateIngressFrame(turnOne, "turn two"))
	ws.readUntilCompleted(t)
	ws.send(t, codexGateIngressFrame("", "developer:developer context", "turn one", "turn two", "turn three"))
	ws.readUntilCompleted(t)
	ws.close(t)

	// 终端对第 k 帧的响应 ID 为 resp_gate_ws_<连接号>_<k>。每条连接先发预热帧，其后每一帧
	// 的 previous_response_id 都必须是同一连接上紧邻的上一个响应——前缀只在同一连接内可复用。
	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, 2, "全量重放不复用旧连接上的续接，由新连接承接")
	var all [][]byte
	for _, handshake := range handshakes {
		frames := codexGateResponseCreateFrames(handshake)
		require.GreaterOrEqual(t, len(frames), 2, "每条连接至少有预热帧与一个业务帧")
		require.Equal(t, gjson.False, gjson.GetBytes(frames[0], "generate").Type, "连接 %d 的预热帧 generate 必须为 false", handshake.connection)
		require.False(t, gjson.GetBytes(frames[0], "previous_response_id").Exists(), "预热帧不携带 previous_response_id")
		for index, frame := range frames[1:] {
			require.NotEqual(t, gjson.False, gjson.GetBytes(frame, "generate").Type, "业务帧不得带 generate=false")
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_%d", handshake.connection, index+1),
				gjson.GetBytes(frame, "previous_response_id").String(),
				"连接 %d 第 %d 帧只能续接同一连接上紧邻的上一个响应", handshake.connection, index+2)
		}
		all = append(all, frames...)
	}
	for index, frame := range all {
		codexGateRequireOrderedSubset(t, codexGateJSONFieldOrder(t, frame), allowed, required,
			fmt.Sprintf("第 %d 帧 response.create 的字段槽位", index+1))
	}
	firstConnection := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, firstConnection, 3, "首条连接：预热、首轮、增量")
	require.Equal(t, turnOne, gjson.GetBytes(firstConnection[2], "previous_response_id").String(),
		"前缀可复用的增量帧必须携带客户端给出的上一轮响应 ID")
	for _, frame := range codexGateResponseCreateFrames(handshakes[1]) {
		require.False(t, strings.HasPrefix(gjson.GetBytes(frame, "previous_response_id").String(),
			fmt.Sprintf("resp_gate_ws_%d_", handshakes[0].connection)), "全量重放不得续接旧连接上的响应")
	}

	// 前缀不可复用：客户端给出与连接上最近响应不一致的 previous_response_id。
	staleServer := startCodexGateWireServer(t, nil)
	staleGate := newCodexGateService(t, mode, staleServer)
	codexGateUseRecorderManifest(staleGate.service, staleGate.account)
	staleGate.enableWebSocket(t, staleServer)
	stale := staleGate.startWebSocketIngress(t, header, codexGateIngressAPIKey(),
		codexGateIngressFrame("", "developer:developer context", "turn one"))
	staleTurn := stale.readUntilCompleted(t)
	stale.send(t, codexGateIngressFrame(staleTurn, "turn two"))
	latest := stale.readUntilCompleted(t)
	stale.send(t, codexGateIngressFrame(staleTurn, "turn three"))
	stale.close(t)
	for _, frame := range codexGateResponseCreateFrames(codexGateWebSocketHandshakes(staleServer)[0]) {
		if gjson.GetBytes(frame, "input.0.content.0.text").String() == "turn three" {
			t.Fatalf("前缀不可复用的续接帧不得发往上游：%s", frame)
		}
	}
	require.NotEqual(t, staleTurn, latest)

	// ResetOn：认证代次与账号 owner 进入连接池键。
	attach := func(mode string, account *Account) *OfficialEgressContext {
		t.Helper()
		body, ingress := codexGateRootIngress(t, nil)
		service := newOfficialOpenAIHTTPTestService(nil)
		service.cfg.Gateway.OfficialClientProfiles.Mode = mode
		ctx, err := attachOfficialEgressWebSocketContext(
			WithOfficialCodexIngressRuntime(ingress.Request.Context(), ingress), ingress, account,
			codexGateResponsesWSURL, codexGateResponseCreateFrame(t, body), service.cfg,
		)
		require.NoError(t, err)
		egressContext, ok := OfficialEgressContextFromContext(ctx)
		require.True(t, ok)
		return egressContext
	}
	bearer := func(token string) http.Header { return http.Header{"Authorization": []string{"Bearer " + token}} }
	target := attach(mode, newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID))
	require.Equal(t,
		officialEgressWebSocketPoolTransportKey(target, "", bearer("token-a")),
		officialEgressWebSocketPoolTransportKey(target, "", bearer("token-a")))
	require.NotEqual(t,
		officialEgressWebSocketPoolTransportKey(target, "", bearer("token-a")),
		officialEgressWebSocketPoolTransportKey(target, "", bearer("token-b")),
		"认证代次变化必须让连接池键变化（旧续接失效）")
	otherAccount := attach(mode, newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID+1))
	require.NotEqual(t,
		officialEgressWebSocketPoolTransportKey(target, "", bearer("token-a")),
		officialEgressWebSocketPoolTransportKey(otherAccount, "", bearer("token-a")),
		"账号 owner 变化必须让连接池键变化")
	if other, ok := codexGateControlReleaseMode(t, mode); ok {
		control := attach(other, newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID))
		require.Equal(t,
			officialEgressWebSocketPoolTransportKey(control, "", bearer("token-a")),
			officialEgressWebSocketPoolTransportKey(control, "", bearer("token-b")),
			"对照：另一槽位没有该节，认证代次不进入连接池键")
	}
}

// SPEC-BODY-004（condition_change）：turn-state 从响应读取、保存并按下一请求传输形态回送；
// 账号 owner 变化时清空。
//
// 检查项与网关观测：
//   - turn-state-value-preserved：上游响应下发的 turn-state 与下一请求回送的值摘要相同；
//   - turn-state-channels：HTTP 通道回送在 x-codex-turn-state 头（同一 turn 的下一次请求），
//     WS 通道回送在下一帧 response.create 的 client_metadata["x-codex-turn-state"]，WS 握手
//     本身从不携带；legacy compact 通道随端点删除；
//   - turn-state-owner-reset：同一会话换到另一账号后不回送已保存的 turn-state——HTTP 由另一
//     账号承接时 wire 上没有该头；WS 即使下游在升级请求里回带旧值，新账号的帧也不携带。
//
// 画像开关：目标画像的 TurnState 节让 WS 入口按溯源丢弃他账号铸造的回带值（生产隔离函数
// 直接重放），另一槽位没有该节。
func TestCodexTurnStateRoundTripReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "TurnState 节声明账号 owner 变化时清空",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().TurnState
			return section != nil && section.ResetOnAccountOwnerChange
		})
	profile := codexGateExecutableProfile(t, mode)
	require.True(t, codexGateLegacyCompactRemoved(profile), "legacy compact 通道随端点删除")
	turnStateSlot, ok := codexGateHeaderSlot(codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesHTTP), "x-codex-turn-state")
	require.True(t, ok)
	require.Equal(t, profilecontract.ConditionTurnStatePresent, turnStateSlot.Condition)
	_, wsHeader := codexGateHeaderSlot(codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS), "x-codex-turn-state")
	require.False(t, wsHeader, "WS 握手不声明 turn-state 头")
	require.True(t, officialCodexTurnStateOwnerIsolation(mode))
	if other, ok := codexGateControlReleaseMode(t, mode); ok {
		require.False(t, officialCodexTurnStateOwnerIsolation(other))
	}
	digest := func(value string) [32]byte { return sha256.Sum256([]byte(value)) }

	// HTTP 通道。
	const httpTurnState = "promotion-gate-http-turn-state"
	var responsesCalls atomic.Int32
	server := startCodexGateWireServer(t, func(request codexGateWireRequest) codexGateWireResponse {
		if request.path == codexGateResponsesPath && responsesCalls.Add(1) == 1 {
			return codexGateSSECompletedResponse("resp_gate_turn_state", http.Header{"X-Codex-Turn-State": []string{httpTurnState}})
		}
		return codexGateDefaultResponse(request)
	})
	gate := newCodexGateService(t, mode, server)
	apiKey := codexGateIngressAPIKey()
	send := func(account *Account, withToolContinuation bool) codexGateWireRequest {
		t.Helper()
		body := newOfficialOpenAIHTTPTestBody(t, true, false, withToolContinuation)
		ingress := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		ingress.Set("api_key", apiKey)
		_, err := gate.forwardAs(ingress, body, account)
		require.NoError(t, err)
		requests := server.httpRequestsForPath(codexGateResponsesPath)
		return requests[len(requests)-1]
	}
	first := send(gate.account, false)
	require.False(t, first.has("x-codex-turn-state"), "首个请求没有可回送的 turn-state")
	returned := send(gate.account, true)
	require.Equal(t, []string{httpTurnState}, returned.values("x-codex-turn-state"), "同一 turn 的下一请求必须回送 turn-state")
	require.Equal(t, digest(httpTurnState), digest(returned.header.Get("X-Codex-Turn-State")), "回送值与读取值摘要一致")
	otherAccount := newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID + 1)
	otherAccount.Credentials["chatgpt_account_id"] = "chatgpt-test-account-other"
	codexGateUseRecorderManifest(gate.service, otherAccount)
	switched := send(otherAccount, true)
	require.False(t, switched.has("x-codex-turn-state"), "账号 owner 变化后不得回送旧 turn-state")

	// WS 通道：上游 response.metadata 下发 turn-state，下一帧回送到 client_metadata。
	const wsTurnState = "promotion-gate-ws-turn-state"
	wsServer := startCodexGateWireServer(t, nil)
	wsServer.setWebSocketResponder(func(request codexGateWireRequest, frame codexGateWireFrame) [][]byte {
		if codexGateWebSocketFrameType(frame) != "response.create" {
			return nil
		}
		events := codexGateDefaultWebSocketResponse(request, frame)
		if !gjson.GetBytes(frame.payload, "generate").Exists() {
			events = append([][]byte{[]byte(`{"type":"response.metadata","headers":{"x-codex-turn-state":"` + wsTurnState + `"}}`)}, events...)
		}
		return events
	})
	wsGate := newCodexGateService(t, mode, wsServer)
	codexGateUseRecorderManifest(wsGate.service, wsGate.account)
	wsGate.enableWebSocket(t, wsServer)
	header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
	ws := wsGate.startWebSocketIngress(t, header, apiKey, codexGateIngressFrame("", "developer:developer context", "turn one"))
	turnOne := ws.readUntilCompleted(t)
	ws.send(t, codexGateIngressFrame(turnOne, "turn two"))
	ws.readUntilCompleted(t)
	ws.close(t)
	handshakes := codexGateWebSocketHandshakes(wsServer)
	require.Len(t, handshakes, 1)
	require.False(t, handshakes[0].has("x-codex-turn-state"), "WS 握手不得携带 turn-state")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 3)
	for index, frame := range frames[:2] {
		require.False(t, gjson.GetBytes(frame, `client_metadata.x-codex-turn-state`).Exists(),
			"第 %d 帧发出时尚未收到 turn-state", index+1)
	}
	echoed := gjson.GetBytes(frames[2], `client_metadata.x-codex-turn-state`).String()
	require.Equal(t, wsTurnState, echoed, "下一帧必须在 client_metadata 回送 turn-state")
	require.Equal(t, digest(wsTurnState), digest(echoed))

	// WS 通道的 owner 变化：新账号的会话即使收到下游回带的旧值也不携带。
	wsGate.account = otherAccount
	header.Set("x-codex-turn-state", wsTurnState)
	header.Set("session-id", "promotion-gate-ws-session")
	switchedWS := wsGate.startWebSocketIngress(t, header, apiKey, codexGateIngressFrame("", "developer:developer context", "turn one"))
	switchedTurn := switchedWS.readUntilCompleted(t)
	switchedWS.send(t, codexGateIngressFrame(switchedTurn, "turn two"))
	switchedWS.readUntilCompleted(t)
	switchedWS.close(t)
	switchedHandshakes := codexGateWebSocketHandshakes(wsServer)
	require.Len(t, switchedHandshakes, 2)
	for index, frame := range codexGateResponseCreateFrames(switchedHandshakes[1])[:2] {
		require.NotEqual(t, wsTurnState, gjson.GetBytes(frame, `client_metadata.x-codex-turn-state`).String(),
			"账号 owner 变化后第 %d 帧不得回送旧 turn-state", index+1)
	}

	// 画像开关的生产隔离函数：溯源记录为账号 A 铸造的值，交给账号 B 时丢弃。
	provenance := newOfficialOpenAIHTTPTestContext(nil, "/v1/responses")
	provenance.Request.Header.Set("session-id", "promotion-gate-provenance")
	provenance.Set("api_key", apiKey)
	gate.service.noteOpenAICodexTurnStateProvenance(provenance, gate.account)
	require.Equal(t, wsTurnState, gate.service.isolateOfficialCodexIngressTurnState(provenance, gate.account, wsTurnState))
	require.Empty(t, gate.service.isolateOfficialCodexIngressTurnState(provenance, otherAccount, wsTurnState),
		"他账号铸造的 turn-state 必须被丢弃")
}
