package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 官方整段重放不重连（画像 WebSocketContinuation 节 ReplayOnSameConnection）。
//
// 官方客户端每次发 WS 请求前判断能否在上一次请求加其输出之上续链；不能时（换模型、上下文压缩后新窗口、
// input 前缀不可复用、相邻 configuration_update 合并）在原连接上发一帧不带 previous_response_id、不带 generate 的
// 全量 response.create，不重连、不补预热。网关对这类帧的旧做法是关掉当前上游连接、新开一条、先补预热帧、再把业务帧
// 链到预热响应上（为把第三方客户端每轮整段历史归一成官方形态而设）。
//
// 画像声明 ReplayOnSameConnection 后：自己发过 generate=false 预热帧的客户端（自管续链）的全量帧由原上游连接原样承接。
// 本文件用真实 wire 的假上游 WS 终端验证：
//   - 三种触发（换模型、压缩后的输入缩短、configuration_update 合并）都在同一条上游连接上完成；
//   - 重放帧：无 previous_response_id、无 generate、全量 input、顶层字段序与其余帧一致；之后没有网关补出的预热帧；
//     客户端随后的增量帧续接重放帧的响应；
//   - 画像不声明（对照）：行为与改动前逐项相同——新连接、网关预热、业务帧链预热；
//   - 声明了但客户端从未自己发过预热帧（第三方每轮整段历史）：行为同样不变。
//
// 这些用例替换包级画像入口，不得并行。

// replayOfficialUserAgent 是官方 Codex 客户端的 UA；重放判定不依赖 UA（见第三方用例），这里只是让官方场景更像真实入站。
const replayOfficialUserAgent = "codex_cli_rs/test-client (Mac OS; arm64) Apple_Terminal"

// withReplayOnSameConnectionProfile 把 mode 槽位的画像在 service 层读到的版本换成 WebSocketContinuation.ReplayOnSameConnection
// 取值为 enabled 的同源画像（以正式目录里该槽位的画像快照为底稿，只改这一个字段），用例结束自动恢复。
// 不论正式画像是否已声明该字段，用例看到的开关都由 enabled 决定：声明（true）与对照（false）都靠它固定。
// 正式 ReleaseCatalog 与 Executor 不受影响：执行层不读这个字段，只有 service 层的入站转发读它。
func withReplayOnSameConnectionProfile(t *testing.T, mode string, enabled bool) {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	require.NotEmpty(t, doc.WebSocketContinuation, "槽位 %s 的画像必须已有 WebSocketContinuation 节", mode)
	var section profilecontract.WebSocketContinuationSection
	require.NoError(t, json.Unmarshal(doc.WebSocketContinuation, &section))
	section.ReplayOnSameConnection = enabled
	raw, err := json.Marshal(section)
	require.NoError(t, err)
	doc.WebSocketContinuation = raw
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	patched, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	require.Equal(t, enabled, patched.Optional().WebSocketContinuation.ReplayOnSameConnection)

	previous := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(requested string) (profilecontract.ExecutableProfile, error) {
		if normalizeOfficialClientProfileMode(requested) == normalizeOfficialClientProfileMode(mode) {
			return patched, nil
		}
		return previous(requested)
	}
	t.Cleanup(func() { officialCodexExecutableProfileForMode = previous })
	require.Equal(t, enabled, officialCodexWebSocketReplayOnSameConnection(mode))
}

// replayStep 由上一轮完成事件的响应 ID 构造客户端要发的下一帧。
type replayStep func(previousResponseID string) []byte

// replayRunIngress 按给定帧序列驱动一条下游 WS 入站会话，返回终端上的全部握手。
func replayRunIngress(
	t *testing.T,
	mode string,
	userAgent string,
	steps []replayStep,
) []codexGateWireRequest {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{userAgent}}
	var ws *codexGateWebSocketIngress
	last := ""
	for index, step := range steps {
		frame := step(last)
		if index == 0 {
			ws = gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(), frame)
		} else {
			ws.send(t, frame)
		}
		last = ws.readUntilCompleted(t)
	}
	ws.close(t)
	return codexGateWebSocketHandshakes(server)
}

func replayPrewarmFrame(t *testing.T, model string) replayStep {
	return func(string) []byte {
		return codexGateWSIngressFrame(t, model, "", "", []any{}, map[string]any{"generate": false})
	}
}

// replayChainFrame 是续链帧：previous_response_id 取上一轮完成事件的响应 ID，input 只有新增项。
func replayChainFrame(t *testing.T, model string, items ...any) replayStep {
	return func(previous string) []byte {
		return codexGateWSIngressFrame(t, model, previous, "", items, nil)
	}
}

// replayFullFrame 是官方客户端判定不能增量时发的全量帧：不带 previous_response_id，也不带 generate。
func replayFullFrame(t *testing.T, model string, items ...any) replayStep {
	return func(string) []byte {
		return codexGateWSIngressFrame(t, model, "", "", items, nil)
	}
}

// replayBaseItem 是非 Lite 模型里官方客户端带的基础指令消息（官方形态，id 为 UUIDv5，passthrough 只有 content_item_kinds）。
// 官方客户端的每一帧全量历史都自带它；入站没有它时网关会按“新链缺来源”回退到默认指令，那是第三方客户端的路径。
func replayBaseItem() any {
	var item any
	if err := json.Unmarshal([]byte(incLegacyBase), &item); err != nil {
		panic(err)
	}
	return item
}

// replayHistoryItems 是三轮对话到“turn three”为止的全量历史（基础指令、开发者上下文加三条用户消息）。
func replayHistoryItems(extra ...any) []any {
	items := []any{
		replayBaseItem(),
		codexGateMessage("developer", "developer context"),
		codexGateMessage("user", "turn one"),
		codexGateMessage("user", "turn two"),
	}
	items = append(items, extra...)
	return append(items, codexGateMessage("user", "turn three"))
}

type replayScenario struct {
	name string
	// 重放帧使用的模型；换模型场景与原模型不同。
	replayModel string
	// 重放帧的 input（全量）。
	replayInput []any
	// 重放帧里应当逐字出现的断言片段（用来证明全量历史确实原样发往上游）。
	mustContain []string
}

func replayScenarios() []replayScenario {
	return []replayScenario{
		{
			// 换模型：model_changed，输入仍是上一次请求的超集，但官方因 model 变化不续链。
			name:        "model_changed",
			replayModel: "gpt-5.4",
			replayInput: replayHistoryItems(),
			mustContain: []string{"developer context", "turn one", "turn two", "turn three"},
		},
		{
			// 压缩后新窗口：历史被压缩结果替换，input 不再以上次请求加输出为前缀（input_shortened／input_mismatch）。
			name:        "compacted_input_shortened",
			replayModel: "gpt-5.5",
			replayInput: []any{
				replayBaseItem(),
				codexGateMessage("developer", "developer context"),
				codexGateMessage("user", "compacted summary of turn one and turn two"),
				codexGateMessage("user", "turn three"),
			},
			mustContain: []string{"developer context", "compacted summary of turn one and turn two", "turn three"},
		},
		{
			// 0.165 新增：连续 configuration_update 合并后与上次请求的前缀不一致，同样整段重放。
			name:        "configuration_update_coalesced",
			replayModel: "gpt-5.5",
			replayInput: replayHistoryItems(map[string]any{
				"type": "configuration_update", "reasoning": map[string]any{"effort": "high"},
			}),
			mustContain: []string{"developer context", "turn one", "turn two", "turn three", `"configuration_update"`},
		},
	}
}

// replaySteps 是官方客户端的会话：预热、首个业务帧、一个增量帧、整段重放帧、重放之后的增量帧。
func replaySteps(t *testing.T, scenario replayScenario) []replayStep {
	return []replayStep{
		replayPrewarmFrame(t, "gpt-5.5"),
		replayChainFrame(t, "gpt-5.5", replayBaseItem(), codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")),
		replayChainFrame(t, "gpt-5.5", codexGateMessage("user", "turn two")),
		replayFullFrame(t, scenario.replayModel, scenario.replayInput...),
		replayChainFrame(t, scenario.replayModel, codexGateMessage("user", "turn four")),
	}
}

// replayRequireFieldSlots 校验帧的顶层字段是批准字段序的有序子集并含全部必需项，顶层没有 instructions。
func replayRequireFieldSlots(t *testing.T, frames [][]byte) {
	t.Helper()
	for index, raw := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		order := codexGateJSONFieldOrder(t, raw)
		codexGateRequireOrderedSubset(t, order, codexGateApprovedWSFields, codexGateApprovedWSRequired, label+" 的顶层字段")
		require.NotContains(t, order, "instructions", "%s：顶层不得有 instructions", label)
	}
}

func replayCountPrewarmFrames(frames [][]byte) int {
	count := 0
	for _, raw := range frames {
		if gjson.GetBytes(raw, "generate").Type == gjson.False {
			count++
		}
	}
	return count
}

// 声明 ReplayOnSameConnection 的画像：官方客户端的三种整段重放都发生在同一条上游连接上。
func TestOpenAIWSReplayOnSameConnectionKeepsConnectionForOfficialReplay(t *testing.T) {
	mode := officialClientProfileModeActive
	withReplayOnSameConnectionProfile(t, mode, true)

	for _, scenario := range replayScenarios() {
		t.Run(scenario.name, func(t *testing.T) {
			handshakes := replayRunIngress(t, mode, replayOfficialUserAgent, replaySteps(t, scenario))
			require.Len(t, handshakes, 1, "整段重放不得新建上游连接")
			connection := handshakes[0].connection
			frames := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, frames, 5, "预热、首个业务帧、增量帧、重放帧、重放之后的增量帧，网关不补任何帧")
			replayRequireFieldSlots(t, frames)

			// 重放之前：官方客户端自己的预热与续链原样放行。
			require.Equal(t, gjson.False, gjson.GetBytes(frames[0], "generate").Type, "首帧是客户端自己的预热帧")
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", connection), gjson.GetBytes(frames[1], "previous_response_id").String())
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_2", connection), gjson.GetBytes(frames[2], "previous_response_id").String())

			// 重放帧：与官方一致——无 previous_response_id、无 generate、全量 input、同连接。
			replay := frames[3]
			require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists(), "重放帧不得带 previous_response_id：%s", replay)
			require.False(t, gjson.GetBytes(replay, "generate").Exists(), "重放帧不得带 generate：%s", replay)
			require.Equal(t, "response.create", gjson.GetBytes(replay, "type").String())
			require.Equal(t, scenario.replayModel, gjson.GetBytes(replay, "model").String())
			require.Equal(t, len(scenario.replayInput), len(gjson.GetBytes(replay, "input").Array()), "全量 input 的项数原样：%s", replay)
			for _, fragment := range scenario.mustContain {
				require.Contains(t, string(replay), fragment, "全量历史必须原样发往上游")
			}
			// 逐帧 client_metadata 照常重建：重放帧是新的一轮，键集合与增量帧相同。
			require.Equal(t, replayMetadataKeys(frames[2]), replayMetadataKeys(replay), "重放帧 client_metadata 的键集合与其余业务帧一致")

			// 网关没有补预热：整条连接上 generate=false 的帧只有客户端自己的那一帧。
			require.Equal(t, 1, replayCountPrewarmFrames(frames), "不得补出第二个预热帧")

			// 重放之后客户端继续增量：续接的是重放帧自己的响应，仍在同一条连接。
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_4", connection), gjson.GetBytes(frames[4], "previous_response_id").String(),
				"重放之后的增量帧续接重放帧的响应")
		})
	}
}

func replayMetadataKeys(frame []byte) string {
	var keys []string
	gjson.GetBytes(frame, "client_metadata").ForEach(func(key, _ gjson.Result) bool {
		keys = append(keys, key.String())
		return true
	})
	return strings.Join(keys, ",")
}

// 对照：画像不声明该字段（旧画像）——改动前的行为逐项不变：新连接、网关补预热、业务帧链预热。
func TestOpenAIWSReplayWithoutProfileSectionKeepsLegacyReconnectAndPrewarm(t *testing.T) {
	mode := officialClientProfileModeActive
	withReplayOnSameConnectionProfile(t, mode, false)
	for _, scenario := range replayScenarios() {
		t.Run(scenario.name, func(t *testing.T) {
			handshakes := replayRunIngress(t, mode, replayOfficialUserAgent, replaySteps(t, scenario))
			require.Len(t, handshakes, 2, "旧行为：整段重放由新连接承接")
			first := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, first, 3, "首条连接：客户端预热、首个业务帧、增量帧")
			second := codexGateResponseCreateFrames(handshakes[1])
			require.Len(t, second, 3, "新连接：网关补的预热、链上预热响应的业务帧、重放之后的增量帧")
			require.Equal(t, gjson.False, gjson.GetBytes(second[0], "generate").Type, "新连接先发网关补的预热帧")
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[1].connection),
				gjson.GetBytes(second[1], "previous_response_id").String(), "业务帧链上新连接自己的预热响应")
			replayRequireFieldSlots(t, append(append([][]byte{}, first...), second...))
		})
	}
}

// 画像声明了，但下游客户端从未自己发过预热帧（第三方客户端每个用户轮次都发整段历史）：沿用旧的归一化。
func TestOpenAIWSReplayOnSameConnectionIgnoresClientsThatNeverSentOwnPrewarm(t *testing.T) {
	mode := officialClientProfileModeActive
	withReplayOnSameConnectionProfile(t, mode, true)
	steps := []replayStep{
		replayFullFrame(t, "gpt-5.5", replayBaseItem(), codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")),
		replayChainFrame(t, "gpt-5.5", codexGateMessage("user", "turn two")),
		replayFullFrame(t, "gpt-5.4", replayHistoryItems()...),
	}
	// 重放判定不看 UA：官方 UA 但没有自己的预热帧，同样按旧方式承接。
	for _, userAgent := range []string{codexGateThirdPartyUserAgent, replayOfficialUserAgent} {
		handshakes := replayRunIngress(t, mode, userAgent, steps)
		require.Len(t, handshakes, 2, "UA=%s：没有自己的预热帧，全量帧仍由新连接承接", userAgent)
		second := codexGateResponseCreateFrames(handshakes[1])
		require.Len(t, second, 2, "新连接：网关补的预热帧与链上它的业务帧")
		require.Equal(t, gjson.False, gjson.GetBytes(second[0], "generate").Type)
		require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[1].connection),
			gjson.GetBytes(second[1], "previous_response_id").String())
	}
}

// Lite 模型的官方形态：重放帧里的 additional_tools 与基础指令消息随历史原样重放（只出现一次），仍在同一条连接。
func TestOpenAIWSReplayOnSameConnectionKeepsLitePrefixVerbatim(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，Lite 前缀随预热帧发送，本例不适用")
	}
	withReplayOnSameConnectionProfile(t, mode, true)
	liteMetadata := map[string]any{"client_metadata": map[string]any{responsesLiteWSMetadataKey: "true"}}
	rawItem := func(raw string) any {
		var item any
		require.NoError(t, json.Unmarshal([]byte(raw), &item))
		return item
	}
	litePrefix := []any{rawItem(incOfficialTools), rawItem(incOfficialBase)}
	steps := []replayStep{
		func(string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", "", "", []any{}, map[string]any{
				"generate": false, "client_metadata": liteMetadata["client_metadata"],
			})
		},
		func(previous string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", previous, "",
				append(append([]any{}, litePrefix...), rawItem(incOfficialContext), rawItem(incOfficialUser)), liteMetadata)
		},
		func(previous string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", previous, "", []any{codexGateMessage("user", "turn two")}, liteMetadata)
		},
		// 换模型（Lite 到 Lite）：历史里已有前缀，全量重放。
		func(string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-terra", "", "",
				append(append(append([]any{}, litePrefix...), rawItem(incOfficialContext), rawItem(incOfficialUser)),
					codexGateMessage("user", "turn two")), liteMetadata)
		},
	}
	handshakes := replayRunIngress(t, mode, replayOfficialUserAgent, steps)
	require.Len(t, handshakes, 1, "Lite 模型的整段重放同样不重连")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 4, "预热、首个业务帧、增量帧、重放帧，网关不补任何帧")
	replay := frames[3]
	require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists())
	require.False(t, gjson.GetBytes(replay, "generate").Exists())
	require.Equal(t, "gpt-5.6-terra", gjson.GetBytes(replay, "model").String())
	require.Equal(t, 1, strings.Count(string(replay), "model.base_instructions"), "基础指令消息随历史只出现一次")
	require.Equal(t, 1, strings.Count(string(replay), `"type":"additional_tools"`), "additional_tools 随历史只出现一次")
	roles := codexGateInputRoles(replay, section.ContentItemKind)
	require.GreaterOrEqual(t, len(roles), 2)
	require.Equal(t, []string{"tools", "base"}, roles[:2], "重放帧 input 仍以 additional_tools、基础指令消息开头：%v", roles)
	require.Equal(t, 1, replayCountPrewarmFrames(frames), "不得补出第二个预热帧")
	replayRequireFieldSlots(t, frames)
}

// 同一账号下“必须换连接”的情形：上游连接在回合之间被对端关闭（官方里是上游 60 分钟主动断开或网络断开）。
// 官方此时的形态是：新连接上发同一帧全量请求，不补预热——网关与之一致：新连接上的第一帧就是客户端的重放帧，
// 网关不补预热，也不链 previous_response_id；之后客户端的增量帧续接这条新连接上的响应。
func TestOpenAIWSReplayOnSameConnectionClosedUpstreamReplaysOnNewConnectionWithoutPrewarm(t *testing.T) {
	mode := officialClientProfileModeActive
	withReplayOnSameConnectionProfile(t, mode, true)
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{replayOfficialUserAgent}}

	ws := gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(),
		codexGateWSIngressFrame(t, "gpt-5.5", "", "", []any{}, map[string]any{"generate": false}))
	last := ws.readUntilCompleted(t)
	ws.send(t, codexGateWSIngressFrame(t, "gpt-5.5", last, "",
		[]any{replayBaseItem(), codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")}, nil))
	last = ws.readUntilCompleted(t)
	ws.send(t, codexGateWSIngressFrame(t, "gpt-5.5", last, "", []any{codexGateMessage("user", "turn two")}, nil))
	last = ws.readUntilCompleted(t)

	// 对端关闭当前上游连接。
	server.mu.Lock()
	for connection := range server.open {
		_ = connection.Close()
	}
	server.mu.Unlock()

	ws.send(t, codexGateWSIngressFrame(t, "gpt-5.4", "", "", replayHistoryItems(), nil))
	last = ws.readUntilCompleted(t)
	ws.send(t, codexGateWSIngressFrame(t, "gpt-5.4", last, "", []any{codexGateMessage("user", "turn four")}, nil))
	ws.readUntilCompleted(t)
	ws.close(t)

	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, 2, "对端关闭的连接必须换新连接")
	first := codexGateResponseCreateFrames(handshakes[0])
	require.GreaterOrEqual(t, len(first), 3, "旧连接上：预热、首个业务帧、增量帧")
	second := codexGateResponseCreateFrames(handshakes[1])
	require.Len(t, second, 2, "新连接上：客户端的重放帧本身与其后的增量帧，网关不补预热")
	replay := second[0]
	require.False(t, gjson.GetBytes(replay, "generate").Exists(), "新连接的第一帧是全量重放帧，不是预热：%s", replay)
	require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists(), "重放帧不链 previous_response_id：%s", replay)
	require.Equal(t, "gpt-5.4", gjson.GetBytes(replay, "model").String())
	require.Equal(t, len(replayHistoryItems()), len(gjson.GetBytes(replay, "input").Array()), "全量 input 原样")
	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[1].connection), gjson.GetBytes(second[1], "previous_response_id").String(),
		"之后的增量帧续接新连接上重放帧的响应")
	all := append(append([][]byte{}, first...), second...)
	replayRequireFieldSlots(t, all)
	require.Equal(t, 1, replayCountPrewarmFrames(all), "整个会话里只有客户端自己的那个预热帧")
}

func TestShouldReplayOnSameOpenAIWSConnection(t *testing.T) {
	full := []byte(`{"type":"response.create","model":"gpt-5.5","input":[]}`)
	cases := []struct {
		name      string
		enabled   bool
		owns      bool
		payload   []byte
		previous  string
		toolOut   bool
		wantReuse bool
	}{
		{"全部成立", true, true, full, "", false, true},
		{"画像未声明", false, true, full, "", false, false},
		{"客户端没自己发过预热", true, false, full, "", false, false},
		{"带 previous_response_id 是普通续链", true, true, full, "resp_1", false, false},
		{"previous_response_id 只含空白按空处理", true, true, full, "  ", false, true},
		{"工具结果续轮不在此列", true, true, full, "", true, false},
		{"客户端自己的预热帧不算重放", true, true, []byte(`{"type":"response.create","generate":false,"input":[]}`), "", false, false},
		{"带 generate=true 的帧不算", true, true, []byte(`{"type":"response.create","generate":true,"input":[]}`), "", false, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.Equal(t, item.wantReuse,
				shouldReplayOnSameOpenAIWSConnection(item.enabled, item.owns, item.payload, item.previous, item.toolOut))
		})
	}
}

func TestOpenAIWSPayloadIsClientPrewarm(t *testing.T) {
	require.True(t, openAIWSPayloadIsClientPrewarm([]byte(`{"type":"response.create","generate":false}`)))
	require.False(t, openAIWSPayloadIsClientPrewarm([]byte(`{"type":"response.create","generate":true}`)))
	require.False(t, openAIWSPayloadIsClientPrewarm([]byte(`{"type":"response.create"}`)))
	require.False(t, openAIWSPayloadIsClientPrewarm([]byte(`{"type":"response.create","generate":"false"}`)),
		"字符串 false 不是布尔假")
	require.False(t, openAIWSPayloadIsClientPrewarm(nil))
}
