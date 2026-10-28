package service

import (
	"bufio"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"

	coderws "github.com/coder/websocket"
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

// replayTestProfileDocs 记录本测试里各槽位当前叠加了哪些改写（累计），使多个开关可以先后叠加在同一槽位上。
var replayTestProfileDocs = map[string]profilecontract.SnapshotDoc{}

// withMutatedOfficialProfile 把 mode 槽位的画像在 service 层读到的版本换成改写后的同源画像（以正式目录里该槽位的画像快照为底稿，
// 或在已叠加的改写上继续改写），用例结束自动恢复。正式 ReleaseCatalog 与 Executor 不受影响。
// 同包其他夹具也替换这个包级入口，所以使用本夹具的用例不得并行。
func withMutatedOfficialProfile(t *testing.T, mode string, mutate func(doc *profilecontract.SnapshotDoc)) {
	t.Helper()
	key := normalizeOfficialClientProfileMode(mode)
	doc, stacked := replayTestProfileDocs[key]
	if !stacked {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
		require.NoError(t, err)
		doc = release.Profile().ToSnapshot()
	}
	mutate(&doc)
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	patched, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)

	previous := officialCodexExecutableProfileForMode
	previousDoc, hadDoc := replayTestProfileDocs[key]
	replayTestProfileDocs[key] = doc
	officialCodexExecutableProfileForMode = func(requested string) (profilecontract.ExecutableProfile, error) {
		if normalizeOfficialClientProfileMode(requested) == key {
			return patched, nil
		}
		return previous(requested)
	}
	t.Cleanup(func() {
		officialCodexExecutableProfileForMode = previous
		if hadDoc {
			replayTestProfileDocs[key] = previousDoc
		} else {
			delete(replayTestProfileDocs, key)
		}
	})
}

// withReplayOnSameConnectionProfile 把 mode 槽位 WebSocketContinuation.ReplayOnSameConnection 取值固定为 enabled。
// 不论正式画像是否已声明该字段，用例看到的开关都由 enabled 决定：声明（true）与对照（false）都靠它固定。
func withReplayOnSameConnectionProfile(t *testing.T, mode string, enabled bool) {
	t.Helper()
	withMutatedOfficialProfile(t, mode, func(doc *profilecontract.SnapshotDoc) {
		require.NotEmpty(t, doc.WebSocketContinuation, "槽位 %s 的画像必须已有 WebSocketContinuation 节", mode)
		var section profilecontract.WebSocketContinuationSection
		require.NoError(t, json.Unmarshal(doc.WebSocketContinuation, &section))
		section.ReplayOnSameConnection = enabled
		raw, err := json.Marshal(section)
		require.NoError(t, err)
		doc.WebSocketContinuation = raw
	})
	require.Equal(t, enabled, officialCodexWebSocketReplayOnSameConnection(mode))
}

// withTurnStateResetOnNewTurnProfile 把 mode 槽位 TurnState.ResetOnNewTurn 取值固定为 enabled。
func withTurnStateResetOnNewTurnProfile(t *testing.T, mode string, enabled bool) {
	t.Helper()
	withMutatedOfficialProfile(t, mode, func(doc *profilecontract.SnapshotDoc) {
		require.NotEmpty(t, doc.TurnState, "槽位 %s 的画像必须已有 TurnState 节", mode)
		var section profilecontract.TurnStateSection
		require.NoError(t, json.Unmarshal(doc.TurnState, &section))
		section.ResetOnNewTurn = enabled
		raw, err := json.Marshal(section)
		require.NoError(t, err)
		doc.TurnState = raw
	})
	require.Equal(t, enabled, officialCodexTurnStateResetOnNewTurn(mode))
}

// withTurnMetadataSectionProfile 对 mode 槽位 TurnMetadata 节做改写（叠加在已有改写之上）。
func withTurnMetadataSectionProfile(t *testing.T, mode string, mutate func(section *profilecontract.TurnMetadataSection)) {
	t.Helper()
	withMutatedOfficialProfile(t, mode, func(doc *profilecontract.SnapshotDoc) {
		require.NotEmpty(t, doc.TurnMetadata, "槽位 %s 的画像必须已有 TurnMetadata 节", mode)
		var section profilecontract.TurnMetadataSection
		require.NoError(t, json.Unmarshal(doc.TurnMetadata, &section))
		mutate(&section)
		raw, err := json.Marshal(section)
		require.NoError(t, err)
		doc.TurnMetadata = raw
	})
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

// replayStartLargeFrameIngress 与夹具的 startWebSocketIngress 相同，只多放宽下游读上限：真实官方帧带整段历史，
// 比 coder/websocket 默认的 32 KiB 读上限大（生产入口自己设置读上限）。
func replayStartLargeFrameIngress(
	t *testing.T,
	g *codexGateService,
	header http.Header,
	apiKey *APIKey,
	first []byte,
) *codexGateWebSocketIngress {
	t.Helper()
	ingress := &codexGateWebSocketIngress{done: make(chan error, 1)}
	wsServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		conn, err := coderws.Accept(writer, request, &coderws.AcceptOptions{
			CompressionMode: coderws.CompressionContextTakeover,
		})
		if err != nil {
			ingress.done <- err
			return
		}
		conn.SetReadLimit(16 << 20)
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
		ctx := g.ingress(ginContext)
		readCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			ingress.done <- readErr
			return
		}
		ingress.done <- g.service.ProxyResponsesWebSocketFromClient(
			ctx, ginContext, conn, g.account, "oauth-test-token", firstMessage, nil,
		)
	}))
	t.Cleanup(wsServer.Close)
	dialCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), nil)
	require.NoError(t, err)
	client.SetReadLimit(16 << 20)
	t.Cleanup(func() { _ = client.CloseNow() })
	ingress.client = client
	ingress.send(t, first)
	return ingress
}

// ---- 官方 0.165 真实帧（ARM64 非正式实采，见 testdata/codex_0165_informal/README.md） ----

// replay0165Manifest 是账号模型清单快照：0.165 实采会话用到的三个模型都是 Lite 模型。
const replay0165Manifest = `{"models":[` +
	`{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,` +
	`"default_reasoning_level":"low","default_reasoning_summary":"none","supports_reasoning_summary_parameter":true},` +
	`{"slug":"gpt-6-astra","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,` +
	`"default_reasoning_level":"medium","default_reasoning_summary":"none","supports_reasoning_summary_parameter":true},` +
	`{"slug":"gpt-5.6-sol","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,` +
	`"default_reasoning_level":"low","default_reasoning_summary":"none","supports_reasoning_summary_parameter":true}]}`

// replay0165Session 是一次官方 0.165 实采会话的主线程：握手头（原样名称与取值，授权类已打码）与按帧号索引的客户端文本帧。
type replay0165Session struct {
	handshake [][2]string
	frames    map[int][]byte
}

func loadCodex0165Session(t *testing.T, name string) replay0165Session {
	t.Helper()
	file, err := os.Open(filepath.Join("testdata", "codex_0165_informal", name+".jsonl.gz"))
	require.NoError(t, err)
	defer func() { _ = file.Close() }()
	reader, err := gzip.NewReader(file)
	require.NoError(t, err)
	session := replay0165Session{frames: map[int][]byte{}}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 1<<20), 16<<20)
	for scanner.Scan() {
		line := scanner.Bytes()
		switch gjson.GetBytes(line, "kind").String() {
		case "handshake":
			gjson.GetBytes(line, "headers").ForEach(func(_, pair gjson.Result) bool {
				session.handshake = append(session.handshake, [2]string{pair.Get("0").String(), pair.Get("1").String()})
				return true
			})
		case "frame":
			text := gjson.GetBytes(line, "text").String()
			require.True(t, gjson.Valid(text))
			session.frames[int(gjson.GetBytes(line, "frame_no").Int())] = []byte(text)
		}
	}
	require.NoError(t, scanner.Err())
	require.NotEmpty(t, session.handshake)
	return session
}

func (s replay0165Session) header(name string) string {
	for _, pair := range s.handshake {
		if strings.EqualFold(pair[0], name) {
			return pair[1]
		}
	}
	return ""
}

// step 返回第 frameNo 帧的构造步骤：带 previous_response_id 的帧换成假上游本轮实际返回的响应 ID。
func (s replay0165Session) step(t *testing.T, frameNo int) replayStep {
	return func(previous string) []byte {
		raw, ok := s.frames[frameNo]
		require.True(t, ok, "会话里没有第 %d 帧", frameNo)
		if gjson.GetBytes(raw, "previous_response_id").Exists() {
			require.NotEmpty(t, previous)
			out, err := sjson.SetBytes(raw, "previous_response_id", previous)
			require.NoError(t, err)
			return out
		}
		return raw
	}
}

// replayRunStepsHooked 用给定账号清单驱动一条下游 WS 入站会话（支持大帧）；before 若非空，在发出第 index 帧之前调用，
// 可用来关闭上游连接等。返回终端握手、下游实际发出的各帧与终端。
func replayRunStepsHooked(
	t *testing.T,
	mode string,
	manifest string,
	steps []replayStep,
	before func(index int, server *codexGateWireServer),
) ([]codexGateWireRequest, [][]byte) {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	gate.service.openaiModelCapabilities.replaceFromManifest(gate.account.ID, []byte(manifest))
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{replayOfficialUserAgent}}
	var ws *codexGateWebSocketIngress
	last := ""
	var sent [][]byte
	for index, step := range steps {
		if before != nil {
			before(index, server)
		}
		frame := step(last)
		sent = append(sent, frame)
		if index == 0 {
			ws = replayStartLargeFrameIngress(t, gate, header, codexGateIngressAPIKey(), frame)
		} else {
			ws.send(t, frame)
		}
		last = ws.readUntilCompleted(t)
	}
	ws.close(t)
	return codexGateWebSocketHandshakes(server), sent
}

// replayRunSteps 用给定账号清单驱动一条下游 WS 入站会话（支持大帧），返回终端握手与下游实际发出的各帧。
func replayRunSteps(t *testing.T, mode string, manifest string, steps []replayStep) ([]codexGateWireRequest, [][]byte) {
	t.Helper()
	return replayRunStepsHooked(t, mode, manifest, steps, nil)
}

// replayCloseUpstreamConnections 由终端关闭所有上游连接。
func replayCloseUpstreamConnections(server *codexGateWireServer) {
	server.mu.Lock()
	defer server.mu.Unlock()
	for open := range server.open {
		_ = open.Close()
	}
}

// replayFrameFieldsExcept 返回帧的顶层字段原文（按字段名），去掉 skip 里的字段。
func replayFrameFieldsExcept(raw []byte, skip ...string) map[string]string {
	fields := map[string]string{}
	gjson.ParseBytes(raw).ForEach(func(key, value gjson.Result) bool {
		for _, name := range skip {
			if key.String() == name {
				return true
			}
		}
		fields[key.String()] = value.Raw
		return true
	})
	return fields
}

// requireReplayEqualsLegacyChainedFrame 证明“同连接原样转发”与旧做法的唯一差别在连接与预热：
// 同一份真实重放帧，经旧做法（新连接＋网关预热＋链预热响应）发出的业务帧，与新做法发出的重放帧相比，除
// previous_response_id 与逐帧重建的 client_metadata 之外，顶层字段逐字节相同（input 全量历史、reasoning、tools 等）。
func requireReplayEqualsLegacyChainedFrame(t *testing.T, replay, legacyChained []byte) {
	t.Helper()
	require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists())
	require.True(t, gjson.GetBytes(legacyChained, "previous_response_id").Exists(), "对照：旧做法的业务帧链着预热响应")
	require.Equal(t,
		replayFrameFieldsExcept(legacyChained, "previous_response_id", "client_metadata"),
		replayFrameFieldsExcept(replay, "client_metadata"),
		"除 previous_response_id 与 client_metadata 外，两种做法发出的业务帧逐字段相同")
	require.Equal(t, replayMetadataKeys(legacyChained), replayMetadataKeys(replay), "client_metadata 的键集合与旧做法一致")
}

type replay0165Case struct {
	fixture string
	// 会话里的帧数；replayAt 是整段重放帧的序号（1 起）；followUpAt 是重放之后紧随的增量帧（0 表示没有）。
	frames, replayAt, followUpAt int
	// 官方样本里重放帧发生的原因（只用于失败信息）。
	reason string
}

func replay0165Cases() []replay0165Case {
	return []replay0165Case{
		// 帧 1 预热、2 首个业务帧、3 换模型（gpt-6.1-sol 到 gpt-6-astra，model_changed）。
		{fixture: "m1_ws_model_switch", frames: 3, replayAt: 3, reason: "换模型"},
		// 帧 1 预热、2 首个业务帧、3 增量、4 压缩（compaction_trigger 增量帧）、5 压缩后新窗口（窗口号 :0 变 :1）、6 恢复增量。
		{fixture: "c1_ws_compact_continue", frames: 6, replayAt: 5, followUpAt: 6, reason: "压缩后新窗口"},
		// 帧 1 预热、2 首个业务帧、3 目录里不支持 effort update 的模型改 effort（reasoning_changed，没有 configuration_update）。
		{fixture: "e6_ws_effort_unsupported_model", frames: 3, replayAt: 3, reason: "不支持 update 的模型改推理强度"},
	}
}

func replayInputTypes(frame []byte) []string {
	var types []string
	for _, item := range gjson.GetBytes(frame, "input").Array() {
		types = append(types, item.Get("type").String()+"/"+item.Get("role").String())
	}
	return types
}

// 官方 0.165 客户端的真实帧（ARM64 非正式实采：换模型、压缩后续聊、不支持 update 的模型改 effort）逐帧过网关：
//   - 官方样本事实：三种整段重放都在原连接上，握手只有一次，握手头里的 routing hint 与 window id 保持旧值，
//     新值只在每帧 client_metadata 里；压缩后窗口号由 :0 变 :1；
//   - 网关：整段重放帧同样在原上游连接上发出，握手只有一次（routing hint 仍是首个模型），不补预热；
//     每一帧的 previous_response_id／generate 的有无、模型、顶层字段序、reasoning、input 项类型序列与官方帧逐帧一致，
//     重放之后的增量帧续接重放帧的响应；
//   - 与旧做法（新连接＋网关预热＋链预热响应）相比，重放帧的内容除 previous_response_id 与逐帧重建的 client_metadata
//     之外逐字段相同，差别只在连接与预热。
func TestOpenAIWSReplayOnSameConnectionMatchesOfficial0165Sessions(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	for _, item := range replay0165Cases() {
		t.Run(item.fixture, func(t *testing.T) {
			session := loadCodex0165Session(t, item.fixture)
			var steps []replayStep
			for index := 1; index <= item.frames; index++ {
				steps = append(steps, session.step(t, index))
			}

			// ---- 官方样本自身的事实（夹具自检）----
			require.Equal(t, gjson.False, gjson.GetBytes(session.frames[1], "generate").Type, "官方首帧是客户端自己的预热帧")
			official := session.frames[item.replayAt]
			require.False(t, gjson.GetBytes(official, "previous_response_id").Exists(), "官方重放帧无 previous_response_id")
			require.False(t, gjson.GetBytes(official, "generate").Exists(), "官方重放帧无 generate")
			require.True(t, strings.HasPrefix(session.header("x-codex-routing-hint"), "model="))
			require.True(t, strings.HasSuffix(session.header("x-codex-window-id"), ":0"), "官方握手里的窗口号是 :0")
			if item.fixture == "c1_ws_compact_continue" {
				require.True(t, strings.HasSuffix(gjson.GetBytes(official, `client_metadata.x-codex-window-id`).String(), ":1"),
					"官方压缩后新窗口的帧里窗口号是 :1，握手里仍是 :0")
			}

			// ---- 网关：声明 ReplayOnSameConnection ----
			withReplayOnSameConnectionProfile(t, mode, true)
			handshakes, sent := replayRunSteps(t, mode, replay0165Manifest, steps)
			require.Len(t, handshakes, 1, "%s的整段重放不得新建上游连接", item.reason)
			require.Equal(t, session.header("x-codex-routing-hint"), handshakes[0].header.Get("x-codex-routing-hint"),
				"握手里的 routing hint 保持首个模型，换模型后不重新握手")
			require.True(t, strings.HasSuffix(handshakes[0].header.Get("x-codex-window-id"), ":0"))
			frames := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, frames, item.frames, "网关不补任何帧")
			require.Equal(t, 1, replayCountPrewarmFrames(frames), "整个会话里只有客户端自己的那个预热帧")
			for index := range frames {
				label := fmt.Sprintf("第 %d 帧", index+1)
				officialFrame, gateFrame := sent[index], frames[index]
				require.Equal(t, gjson.GetBytes(officialFrame, "previous_response_id").Exists(),
					gjson.GetBytes(gateFrame, "previous_response_id").Exists(), label+"：previous_response_id 的有无与官方一致")
				require.Equal(t, gjson.GetBytes(officialFrame, "generate").Raw, gjson.GetBytes(gateFrame, "generate").Raw, label+"：generate 与官方一致")
				require.Equal(t, gjson.GetBytes(officialFrame, "model").String(), gjson.GetBytes(gateFrame, "model").String(), label+"：模型")
				if gjson.GetBytes(officialFrame, "input.0.type").String() == "compaction_trigger" {
					// 压缩帧（本例之外的既有差异）：官方不带 access_programs、turn metadata 的 request_kind 是 compaction，
					// 网关按普通业务帧补 access_programs 并写 request_kind=turn；整段重放不涉及，见 W3 报告第 7 节。
					t.Logf("%s：压缩帧 官方字段序=%v 网关字段序=%v", label, codexGateJSONFieldOrder(t, officialFrame), codexGateJSONFieldOrder(t, gateFrame))
				} else {
					require.Equal(t, codexGateJSONFieldOrder(t, officialFrame), codexGateJSONFieldOrder(t, gateFrame), label+"：顶层字段序与官方一致")
				}
				require.Equal(t, gjson.GetBytes(officialFrame, "reasoning").Raw, gjson.GetBytes(gateFrame, "reasoning").Raw, label+"：reasoning")
				require.Equal(t, replayInputTypes(officialFrame), replayInputTypes(gateFrame), label+"：input 项类型序列与官方一致")
			}
			replay := frames[item.replayAt-1]
			require.Equal(t, len(gjson.GetBytes(official, "input").Array()), len(gjson.GetBytes(replay, "input").Array()), "整段历史的项数原样")
			require.Equal(t, 1, strings.Count(string(replay), `"model.base_instructions"`), "基础指令消息随历史只出现一次")
			// 重放帧是新的一轮：client_metadata 里的标识由网关重建（不把客户端实采时的 turn_id 发往上游），模型与推理强度取本帧。
			require.NotEqual(t, gjson.GetBytes(official, "client_metadata.turn_id").String(), gjson.GetBytes(replay, "client_metadata.turn_id").String())
			officialMeta := gjson.Parse(gjson.GetBytes(official, `client_metadata.x-codex-turn-metadata`).String())
			gateMeta := gjson.Parse(gjson.GetBytes(replay, `client_metadata.x-codex-turn-metadata`).String())
			require.Equal(t, officialMeta.Get("model").String(), gateMeta.Get("model").String(), "turn metadata 的 model 取本帧模型")
			require.Equal(t, officialMeta.Get("reasoning_effort").String(), gateMeta.Get("reasoning_effort").String(), "turn metadata 的 reasoning_effort 取本帧")
			require.Equal(t, "turn", gateMeta.Get("request_kind").String())
			// 窗口号：官方压缩后推进到 :1；网关的窗口号是连接级身份，目前不随压缩推进（已知缺口，见 W3 报告第 7 节），这里只记录。
			t.Logf("%s：官方帧窗口号=%s 网关帧窗口号=%s", item.fixture,
				gjson.GetBytes(official, `client_metadata.x-codex-window-id`).String(), gjson.GetBytes(replay, `client_metadata.x-codex-window-id`).String())
			if item.followUpAt > 0 {
				require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_%d", handshakes[0].connection, item.replayAt),
					gjson.GetBytes(frames[item.followUpAt-1], "previous_response_id").String(), "重放之后的增量帧续接重放帧的响应")
			}
			if item.fixture == "c1_ws_compact_continue" {
				require.Equal(t, `{"type":"compaction_trigger"}`, gjson.GetBytes(frames[3], "input.0").Raw, "压缩帧是增量帧，原样")
				require.Equal(t, 1, strings.Count(string(replay), `"type":"compaction"`), "压缩后的重放帧恰有一个 compaction 项")
			}
			if item.fixture == "e6_ws_effort_unsupported_model" {
				require.NotContains(t, string(replay), "configuration_update", "目录不支持 update 的模型改 effort 不产生 configuration_update")
				require.Equal(t, "high", gjson.GetBytes(replay, "reasoning.effort").String())
			}

			// ---- 对照：旧做法（画像不声明）----
			withReplayOnSameConnectionProfile(t, mode, false)
			legacyHandshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
			require.Len(t, legacyHandshakes, 2, "旧做法：整段重放由新连接承接")
			legacyFrames := codexGateResponseCreateFrames(legacyHandshakes[1])
			require.GreaterOrEqual(t, len(legacyFrames), 2)
			require.Equal(t, gjson.False, gjson.GetBytes(legacyFrames[0], "generate").Type, "新连接先发网关补的预热帧")
			requireReplayEqualsLegacyChainedFrame(t, replay, legacyFrames[1])
		})
	}
}

// 官方 0.165 TUI 每个会话启动后另开一条标题线程（独立 WS）：同一连接上先发预热帧，再发一帧全量业务帧（不带 previous_response_id、
// 不带 generate，因为工具目录与预热不同）。这是官方自己的“预热后全量帧”，旧做法会把它拆成两条上游连接并补预热；
// 声明 ReplayOnSameConnection 后与官方一致：一条连接、两帧。
func TestOpenAIWSReplayOnSameConnectionOfficialTitleThreadStaysOnOneConnection(t *testing.T) {
	mode := officialClientProfileModeActive
	session := loadCodex0165Session(t, "t1_ws_title_thread")
	steps := []replayStep{session.step(t, 1), session.step(t, 2)}
	require.False(t, gjson.GetBytes(session.frames[2], "previous_response_id").Exists())
	require.False(t, gjson.GetBytes(session.frames[2], "generate").Exists())

	withReplayOnSameConnectionProfile(t, mode, true)
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1, "预热后的全量业务帧在同一条上游连接上发出")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 2, "预热帧与业务帧，网关不补任何帧")
	require.Equal(t, gjson.False, gjson.GetBytes(frames[0], "generate").Type)
	require.False(t, gjson.GetBytes(frames[1], "previous_response_id").Exists())
	require.False(t, gjson.GetBytes(frames[1], "generate").Exists())
	require.Equal(t, 5, len(gjson.GetBytes(frames[1], "input").Array()))
	require.Equal(t, session.header("x-codex-routing-hint"), handshakes[0].header.Get("x-codex-routing-hint"))

	withReplayOnSameConnectionProfile(t, mode, false)
	legacy, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, legacy, 2, "旧做法：预热之后的全量帧被拆到第二条连接并补预热")
}

// replayRunWithUpstreamClose 驱动一条下游 WS 入站会话；终端在第 connection 条连接收到第 frameNo 个 response.create 时
// 把所有上游连接关掉（只触发一次，该帧不应答），模拟对端在写入前后断开。返回终端上的握手。
func replayRunWithUpstreamClose(
	t *testing.T,
	mode string,
	userAgent string,
	connection int,
	frameNo int,
	steps []replayStep,
) []codexGateWireRequest {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	var fired atomic.Bool
	server.setWebSocketResponder(func(request codexGateWireRequest, frame codexGateWireFrame) [][]byte {
		if codexGateWebSocketFrameType(frame) != "response.create" {
			return nil
		}
		if connection > 0 && request.connection == connection && len(request.frames) == frameNo && fired.CompareAndSwap(false, true) {
			server.mu.Lock()
			for open := range server.open {
				_ = open.Close()
			}
			server.mu.Unlock()
			return nil
		}
		return codexGateDefaultWebSocketResponse(request, frame)
	})
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
	if connection > 0 {
		require.True(t, fired.Load(), "终端没有触发断开")
	}
	return codexGateWebSocketHandshakes(server)
}

// 派生帧被可重试的失败（上游在写入前后断开）打断后，换新连接重试同一帧：重试用定型之前的帧重新定型，
// 不会因为拿定型后的帧与客户端原帧比对而被判成“被篡改”（1008）。此前凡是走 retryIngressTurn 的派生帧
// （没有 previous_response_id 的全量帧：第三方客户端的首帧与每轮整段历史、预热帧与网关补出的业务帧）重试一次就会收到 1008。
// 这里用旧画像（网关补预热那条路径）与第三方客户端覆盖：断在预热帧、断在业务帧、第二轮整段历史断在业务帧。
func TestOpenAIWSRetryAfterUpstreamClosedKeepsDerivedFrameValid(t *testing.T) {
	mode := officialClientProfileModeActive
	withReplayOnSameConnectionProfile(t, mode, false)
	turnOne := replayFullFrame(t, "gpt-5.5", replayBaseItem(), codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one"))
	turnTwo := replayFullFrame(t, "gpt-5.5", replayBaseItem(), codexGateMessage("developer", "developer context"),
		codexGateMessage("user", "turn one"), codexGateMessage("user", "turn two"))
	cases := []struct {
		name       string
		connection int
		frameNo    int
		steps      []replayStep
	}{
		{"首帧断在网关补的预热帧", 1, 1, []replayStep{turnOne}},
		{"首帧断在业务帧", 1, 2, []replayStep{turnOne}},
		{"第二轮整段历史断在业务帧", 2, 2, []replayStep{turnOne, turnTwo}},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			baseline := replayRunWithUpstreamClose(t, mode, codexGateThirdPartyUserAgent, 0, 0, item.steps)
			handshakes := replayRunWithUpstreamClose(t, mode, codexGateThirdPartyUserAgent, item.connection, item.frameNo, item.steps)
			require.Equal(t, len(baseline)+1, len(handshakes), "断开后恰多一条新连接，会话照常完成")
			last := codexGateResponseCreateFrames(handshakes[len(handshakes)-1])
			baseLast := codexGateResponseCreateFrames(baseline[len(baseline)-1])
			require.Equal(t, len(baseLast), len(last), "新连接上的帧序列与没有断开时最后一条连接相同：预热帧与链上它的业务帧")
			for index := range last {
				require.Equal(t,
					replayFrameFieldsExcept(baseLast[index], "previous_response_id", "client_metadata"),
					replayFrameFieldsExcept(last[index], "previous_response_id", "client_metadata"),
					"第 %d 帧：重试后的帧与没有断开时的帧内容一致", index+1)
			}
		})
	}
}

// replayTurnStateValue 是假上游在第 frameNo 帧响应里下发的 turn-state。
func replayTurnStateValue(connection int, frameNo int) string {
	return fmt.Sprintf("ts-%d-%d", connection, frameNo)
}

// replayRunTurnStateSession 驱动官方客户端会话：预热、回合一首帧（响应带工具调用）、同一回合内的工具结果续轮、回合二首帧。
// 假上游对每个响应（含预热帧的响应）下发 turn-state，返回各业务帧 client_metadata 里回送的 turn-state（空串表示没有）。
func replayRunTurnStateSession(t *testing.T, mode string) []string {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	server.setWebSocketResponder(func(request codexGateWireRequest, frame codexGateWireFrame) [][]byte {
		if codexGateWebSocketFrameType(frame) != "response.create" {
			return nil
		}
		responseID := fmt.Sprintf("resp_gate_ws_%d_%d", request.connection, len(request.frames))
		events := [][]byte{[]byte(`{"type":"response.metadata","headers":{"x-codex-turn-state":"` +
			replayTurnStateValue(request.connection, len(request.frames)) + `"}}`)}
		output := `[]`
		if strings.Contains(string(frame.payload), "turn one") {
			// 回合一的响应带一个工具调用，客户端随后发工具结果续轮。
			output = `[{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{}","status":"completed"}]`
		}
		events = append(events,
			[]byte(`{"type":"response.created","response":{"id":"`+responseID+`","object":"response","status":"in_progress"}}`),
			[]byte(`{"type":"response.completed","response":{"id":"`+responseID+`","object":"response","status":"completed","output":`+output+
				`,"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}`))
		return events
	})
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{replayOfficialUserAgent}}
	steps := []replayStep{
		replayPrewarmFrame(t, "gpt-5.5"),
		replayChainFrame(t, "gpt-5.5", replayBaseItem(), codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")),
		replayChainFrame(t, "gpt-5.5", map[string]any{"type": "function_call_output", "call_id": "call_1", "output": "ok"}),
		replayChainFrame(t, "gpt-5.5", codexGateMessage("user", "turn two")),
	}
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
	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 4)
	var echoed []string
	for _, frame := range frames {
		echoed = append(echoed, gjson.GetBytes(frame, `client_metadata.x-codex-turn-state`).String())
	}
	return echoed
}

// turn-state 的作用域：官方每个回合新建模型客户端会话，turn-state 只在产生它的回合内回送。
//   - 紧跟客户端预热帧的第一个业务帧回送预热响应里收到的值（预热过的会话交给第一个回合）；
//   - 同一回合内的工具结果续轮回送本回合已收到的值；
//   - 新用户轮次的第一个请求不回送上一回合的值。
//
// 画像声明 TurnState.ResetOnNewTurn 时网关与此一致；不声明（旧画像）时保持旧行为：保存的值回送给之后的每个请求。
func TestOpenAIWSTurnStateScopedToTurnWhenProfileDeclares(t *testing.T) {
	mode := officialClientProfileModeActive

	withTurnStateResetOnNewTurnProfile(t, mode, true)
	scoped := replayRunTurnStateSession(t, mode)
	require.Equal(t, []string{
		"",                         // 预热帧：还没有任何值
		replayTurnStateValue(1, 1), // 回合一首帧：预热响应里收到的值随预热过的会话交给第一个回合
		replayTurnStateValue(1, 2), // 同一回合内的工具结果续轮：回送本回合已收到的值
		"",                         // 回合二首帧：新用户轮次，不回送上一回合的值
	}, scoped, "声明 ResetOnNewTurn：turn-state 只在产生它的回合内回送")

	withTurnStateResetOnNewTurnProfile(t, mode, false)
	legacy := replayRunTurnStateSession(t, mode)
	require.Equal(t, []string{
		"",
		replayTurnStateValue(1, 1),
		replayTurnStateValue(1, 2),
		replayTurnStateValue(1, 3), // 旧行为：上一回合收到的值跨回合回送
	}, legacy, "对照：不声明时保持旧行为")
}

func TestShouldResetOpenAIWSTurnStateOnNewTurn(t *testing.T) {
	cases := []struct {
		name                          string
		enabled                       bool
		turn                          int
		hasFunctionCallOutput         bool
		previousFrameWasClientPrewarm bool
		want                          bool
	}{
		{"新用户轮次", true, 3, false, false, true},
		{"画像未声明", false, 3, false, false, false},
		{"会话第一帧没有可清的值", true, 1, false, false, false},
		{"工具结果续轮属于同一回合", true, 3, true, false, false},
		{"紧跟预热帧的业务帧保留预热响应的值", true, 2, false, true, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.Equal(t, item.want, shouldResetOpenAIWSTurnStateOnNewTurn(
				item.enabled, item.turn, item.hasFunctionCallOutput, item.previousFrameWasClientPrewarm))
		})
	}
}

func replayFrameTurnMetadata(frame []byte) gjson.Result {
	return gjson.Parse(gjson.GetBytes(frame, `client_metadata.x-codex-turn-metadata`).String())
}

func replayHandshakeTurnMetadata(handshake codexGateWireRequest) gjson.Result {
	return gjson.Parse(handshake.header.Get("x-codex-turn-metadata"))
}

var replayUUIDPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// 上下文窗口跟踪（画像 TurnMetadata.WindowTracking）。官方 0.165 压缩续聊会话（c1）的事实：
//   - 握手里窗口头是 `<线程>:0`，握手 turn metadata 带 window_id、window_number=0、context_window_id；
//   - 压缩请求仍在窗口 0；压缩后新窗口的第一帧起 client_metadata 与 turn metadata 里窗口号变 `:1`、window_number=1、
//     context_window_id 换新，之后各帧同窗口同值；握手头不重发（复用连接上仍是旧窗口号）；
//   - 推进之后新建连接（上游断开后重放）的握手窗口头与 turn metadata 用推进后的窗口号。
//
// 画像不声明（对照）时保持旧行为：窗口号恒为 `:0`，不写 window_number 与 context_window_id。
func TestOpenAIWSWindowTrackingAdvancesWindowAfterCompaction(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	var steps []replayStep
	for index := 1; index <= 6; index++ {
		steps = append(steps, session.step(t, index))
	}
	// 官方样本事实（夹具自检）。
	officialHandshakeTM := gjson.Parse(session.header("x-codex-turn-metadata"))
	require.EqualValues(t, 0, officialHandshakeTM.Get("window_number").Int())
	require.True(t, replayUUIDPattern.MatchString(officialHandshakeTM.Get("context_window_id").String()))
	officialReplayTM := replayFrameTurnMetadata(session.frames[5])
	require.EqualValues(t, 1, officialReplayTM.Get("window_number").Int())
	require.NotEqual(t, officialHandshakeTM.Get("context_window_id").String(), officialReplayTM.Get("context_window_id").String())

	withReplayOnSameConnectionProfile(t, mode, true)
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WindowTracking = true })
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 6)

	thread := strings.TrimSuffix(handshakes[0].header.Get("x-codex-window-id"), ":0")
	require.NotEqual(t, handshakes[0].header.Get("x-codex-window-id"), thread, "握手窗口头是 <线程>:0")
	handshakeTM := replayHandshakeTurnMetadata(handshakes[0])
	require.Equal(t, thread+":0", handshakeTM.Get("window_id").String())
	require.EqualValues(t, 0, handshakeTM.Get("window_number").Int(), "握手 turn metadata 带 window_number")
	windowZeroContext := handshakeTM.Get("context_window_id").String()
	require.True(t, replayUUIDPattern.MatchString(windowZeroContext), "握手 turn metadata 带 context_window_id：%s", handshakeTM.Raw)

	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		tm := replayFrameTurnMetadata(frame)
		window := gjson.GetBytes(frame, `client_metadata.x-codex-window-id`).String()
		require.Equal(t, window, tm.Get("window_id").String(), label+"：client_metadata 与 turn metadata 的窗口号一致")
		if index < 4 {
			// 帧 1～4（预热、首个业务帧、增量、压缩请求）仍在窗口 0，与握手一致。
			require.Equal(t, thread+":0", window, label)
			require.EqualValues(t, 0, tm.Get("window_number").Int(), label)
			require.Equal(t, windowZeroContext, tm.Get("context_window_id").String(), label+"：窗口 0 的上下文窗口 id 与握手相同")
			continue
		}
		// 帧 5、6：压缩后的新窗口。
		require.Equal(t, thread+":1", window, label+"：压缩后窗口号推进到 :1，线程部分仍是网关派生的")
		require.EqualValues(t, 1, tm.Get("window_number").Int(), label)
		require.True(t, replayUUIDPattern.MatchString(tm.Get("context_window_id").String()), label)
		require.NotEqual(t, windowZeroContext, tm.Get("context_window_id").String(), label+"：上下文窗口 id 换新")
	}
	require.Equal(t, replayFrameTurnMetadata(frames[4]).Get("context_window_id").String(),
		replayFrameTurnMetadata(frames[5]).Get("context_window_id").String(), "同一窗口内上下文窗口 id 不变")
	require.Equal(t, thread+":0", handshakes[0].header.Get("x-codex-window-id"), "复用的连接不重发握手，握手里仍是旧窗口号")

	// 推进之后新建连接：上游断开后客户端重放（不带 previous_response_id），新连接的握手用推进后的窗口号。
	reconnectSteps := append(append([]replayStep{}, steps[:5]...), func(string) []byte {
		raw, err := sjson.DeleteBytes(session.frames[6], "previous_response_id")
		require.NoError(t, err)
		return raw
	})
	reconnected, _ := replayRunStepsHooked(t, mode, replay0165Manifest, reconnectSteps, func(index int, server *codexGateWireServer) {
		if index == 5 {
			replayCloseUpstreamConnections(server)
		}
	})
	require.Len(t, reconnected, 2, "上游断开后重放换了新连接")
	secondThread := strings.TrimSuffix(reconnected[1].header.Get("x-codex-window-id"), ":1")
	require.NotEqual(t, reconnected[1].header.Get("x-codex-window-id"), secondThread, "新连接握手的窗口头是推进后的 :1")
	require.Equal(t, strings.TrimSuffix(reconnected[0].header.Get("x-codex-window-id"), ":0"), secondThread, "线程部分不变")
	secondTM := replayHandshakeTurnMetadata(reconnected[1])
	require.Equal(t, reconnected[1].header.Get("x-codex-window-id"), secondTM.Get("window_id").String())
	require.EqualValues(t, 1, secondTM.Get("window_number").Int())
	require.Equal(t, replayFrameTurnMetadata(frames[4]).Get("context_window_id").String(), secondTM.Get("context_window_id").String(),
		"新连接握手的上下文窗口 id 与窗口 1 的帧相同")

	// 对照：不声明。
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WindowTracking = false })
	legacyHandshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, legacyHandshakes, 1)
	for index, frame := range codexGateResponseCreateFrames(legacyHandshakes[0]) {
		label := fmt.Sprintf("对照第 %d 帧", index+1)
		tm := replayFrameTurnMetadata(frame)
		require.True(t, strings.HasSuffix(gjson.GetBytes(frame, `client_metadata.x-codex-window-id`).String(), ":0"), label+"：旧行为窗口号恒为 :0")
		require.False(t, tm.Get("window_number").Exists(), label)
		require.False(t, tm.Get("context_window_id").Exists(), label)
	}
}

// WS 压缩请求（画像 TurnMetadata.WSCompactionRequests）。官方 0.165 压缩续聊会话（c1）的压缩帧：previous_response_id 加单项
// compaction_trigger；请求体不带 access_programs；turn metadata 的 request_kind 是 compaction，带 compaction 对象，
// 不带 model、reasoning_effort、turn_trigger；压缩前后的普通业务帧不受影响。画像不声明（对照）时按普通业务帧处理。
func TestOpenAIWSCompactionRequestFrameMatchesOfficial(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	var steps []replayStep
	for index := 1; index <= 6; index++ {
		steps = append(steps, session.step(t, index))
	}
	official := session.frames[4]
	officialTM := replayFrameTurnMetadata(official)
	require.Equal(t, "compaction", officialTM.Get("request_kind").String())
	require.False(t, officialTM.Get("model").Exists())
	require.False(t, gjson.GetBytes(official, "access_programs").Exists())

	withReplayOnSameConnectionProfile(t, mode, true)
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WSCompactionRequests = true })
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 6)
	compaction := frames[3]
	require.Equal(t, `{"type":"compaction_trigger"}`, gjson.GetBytes(compaction, "input.0").Raw)
	require.Equal(t, codexGateJSONFieldOrder(t, official), codexGateJSONFieldOrder(t, compaction), "压缩帧的顶层字段序与官方一致（没有 access_programs）")
	tm := replayFrameTurnMetadata(compaction)
	require.Equal(t, "compaction", tm.Get("request_kind").String())
	require.Equal(t, officialTM.Get("compaction").Raw, tm.Get("compaction").Raw, "compaction 对象的键序与取值与官方一致")
	for _, key := range []string{"model", "reasoning_effort", "turn_trigger"} {
		require.False(t, tm.Get(key).Exists(), "压缩请求的 turn metadata 不带 %s：%s", key, tm.Raw)
	}
	require.True(t, tm.Get("turn_started_at_unix_ms").Exists())
	// 压缩请求有自己的新回合 id：官方实采里压缩帧与它前一个、后一个业务帧是三个不同的 UUIDv7；
	// client_metadata 的 turn_id 与 turn metadata 的 turn_id 一致；开始时间由该 id 推出（不早于 id 内嵌毫秒）。
	officialIDs := []string{
		replayFrameTurnMetadata(session.frames[3]).Get("turn_id").String(),
		officialTM.Get("turn_id").String(),
		replayFrameTurnMetadata(session.frames[5]).Get("turn_id").String(),
	}
	require.Len(t, map[string]struct{}{officialIDs[0]: {}, officialIDs[1]: {}, officialIDs[2]: {}}, 3, "官方压缩帧的回合 id 与前后业务帧都不同")
	beforeID := replayFrameTurnMetadata(frames[2]).Get("turn_id").String()
	afterID := replayFrameTurnMetadata(frames[4]).Get("turn_id").String()
	compactionID := tm.Get("turn_id").String()
	require.Regexp(t, replayUUIDPattern, compactionID)
	require.Equal(t, byte('7'), compactionID[14], "压缩回合 id 是 UUIDv7")
	require.NotEqual(t, beforeID, compactionID, "压缩帧不再沿用前一个业务回合的 id")
	require.NotEqual(t, afterID, compactionID, "压缩帧与压缩之后的业务回合 id 不同")
	require.NotEqual(t, beforeID, afterID)
	require.Equal(t, compactionID, gjson.GetBytes(compaction, "client_metadata.turn_id").String(), "client_metadata 与 turn metadata 的 turn_id 一致")
	startedAt, ok := officialCodexTurnStartedAtMs(compactionID)
	require.True(t, ok)
	require.Equal(t, startedAt, tm.Get("turn_started_at_unix_ms").Int(), "开始时间由压缩回合 id 推出")
	// 压缩帧之后的业务帧仍按各自的用户锚点取回合（官方第 5、6 帧也是两个不同的回合），压缩回合 id 不污染后续帧。
	require.NotEqual(t, compactionID, replayFrameTurnMetadata(frames[5]).Get("turn_id").String())
	require.NotEqual(t, replayFrameTurnMetadata(frames[4]).Get("turn_id").String(), replayFrameTurnMetadata(frames[5]).Get("turn_id").String())
	// 压缩之前与之后的普通业务帧不受影响：照常带 access_programs、model、reasoning_effort、turn_trigger。
	for _, index := range []int{1, 2, 4, 5} {
		label := fmt.Sprintf("第 %d 帧", index+1)
		require.True(t, gjson.GetBytes(frames[index], "access_programs").Exists(), label)
		other := replayFrameTurnMetadata(frames[index])
		require.Equal(t, "turn", other.Get("request_kind").String(), label)
		require.True(t, other.Get("model").Exists() && other.Get("reasoning_effort").Exists() && other.Get("turn_trigger").Exists(), label)
		require.False(t, other.Get("compaction").Exists(), label)
	}

	// 对照：不声明。
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WSCompactionRequests = false })
	legacyHandshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	legacy := codexGateResponseCreateFrames(legacyHandshakes[0])[3]
	require.True(t, gjson.GetBytes(legacy, "access_programs").Exists(), "旧行为：压缩帧按普通业务帧补 access_programs")
	require.Equal(t, "turn", replayFrameTurnMetadata(legacy).Get("request_kind").String())
	require.Equal(t, replayFrameTurnMetadata(codexGateResponseCreateFrames(legacyHandshakes[0])[2]).Get("turn_id").String(),
		replayFrameTurnMetadata(legacy).Get("turn_id").String(), "旧行为：压缩帧没有用户锚点，沿用前一个业务回合的 id")
}

// 压缩回合 id 的稳定性：同一帧重试（seed 不变）得到同一个 id，下一次压缩（previous_response_id 不同）得到新 id；
// 画像不声明、或压缩帧里带有已盖原回合 turn_id 的历史项时不生成，沿用原回合 id。
func TestOfficialCodexWSCompactionTurnIDIsStablePerRequest(t *testing.T) {
	mode := officialClientProfileModeActive
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WSCompactionRequests = true })
	prewarmFrame := session.frames[1]
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	applyOfficialOpenAIWSIngressHeadersForTest(c, prewarmFrame)
	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(), c, newOfficialOpenAIHTTPTestAccount(95),
		"wss://chatgpt.com/backend-api/codex/responses", prewarmFrame, wsLegacyConfigForMode(mode),
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	require.NotNil(t, egressContext.openAIWSDerived)
	sessionID := "0198a7b0-0000-7000-8000-000000000001"

	frame := func(previous string, input ...any) map[string]any {
		return map[string]any{"previous_response_id": previous, "input": input}
	}
	trigger := map[string]any{"type": "compaction_trigger"}
	first, startedAt, ok, err := officialCodexWSCompactionTurnID(egressContext, sessionID, frame("resp_a", trigger))
	require.NoError(t, err)
	require.True(t, ok)
	require.Regexp(t, replayUUIDPattern, first)
	require.Equal(t, officialCodexTurnStartedAtMsOrNow(first), startedAt)
	again, _, ok, err := officialCodexWSCompactionTurnID(egressContext, sessionID, frame("resp_a", trigger))
	require.NoError(t, err)
	require.True(t, ok)
	require.Equal(t, first, again, "同一帧重试得到同一个压缩回合 id")
	next, _, ok, err := officialCodexWSCompactionTurnID(egressContext, sessionID, frame("resp_b", trigger))
	require.NoError(t, err)
	require.True(t, ok)
	require.NotEqual(t, first, next, "下一次压缩（previous_response_id 不同）是新回合")
	other, _, _, err := officialCodexWSCompactionTurnID(egressContext, "0198a7b0-0000-7000-8000-000000000002", frame("resp_a", trigger))
	require.NoError(t, err)
	require.NotEqual(t, first, other, "不同会话互不影响")

	// 带历史项（已盖原回合 turn_id）的压缩帧、没有 compaction_trigger 的帧：不生成。
	history := map[string]any{"type": "message", "role": "user", "content": "x"}
	_, _, ok, err = officialCodexWSCompactionTurnID(egressContext, sessionID, frame("", history, trigger))
	require.NoError(t, err)
	require.False(t, ok, "完整历史压缩帧沿用原回合 id")
	_, _, ok, err = officialCodexWSCompactionTurnID(egressContext, sessionID, frame("resp_a", history))
	require.NoError(t, err)
	require.False(t, ok)

	// 画像不声明：不生成。
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.WSCompactionRequests = false })
	_, _, ok, err = officialCodexWSCompactionTurnID(egressContext, sessionID, frame("resp_a", trigger))
	require.NoError(t, err)
	require.False(t, ok)
}

// WS 压缩请求的 compaction 对象与 HTTP 压缩（resolveDerivedOfficialOpenAICompactionMetadata）按同一规则取值。
func TestOfficialCodexCompactionMetadataForReasonMatchesHTTPRule(t *testing.T) {
	mode := officialClientProfileModeActive
	body := []byte(`{"model":"gpt-5.5","input":[{"type":"message","role":"user","content":"x"},{"type":"compaction_trigger"}]}`)
	for _, reason := range []string{"user_requested", "context_limit", "model_downshift", "comp_hash_changed", "", "bogus"} {
		t.Run("原因="+reason, func(t *testing.T) {
			ginContext, _ := gin.CreateTestContext(httptest.NewRecorder())
			ginContext.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			ginContext.Request.Header.Set("x-codex-turn-metadata", `{"compaction":{"reason":"`+reason+`"}}`)
			want, ok := resolveDerivedOfficialOpenAICompactionMetadata(ginContext, body, mode)
			require.True(t, ok)
			require.Equal(t, want, officialCodexCompactionMetadataForReason(reason))
		})
	}
}

// withClientMetadataSectionProfile 对 mode 槽位 ClientMetadata 节做改写（叠加在已有改写之上）。
func withClientMetadataSectionProfile(t *testing.T, mode string, mutate func(section *profilecontract.ClientMetadataSection)) {
	t.Helper()
	withMutatedOfficialProfile(t, mode, func(doc *profilecontract.SnapshotDoc) {
		require.NotEmpty(t, doc.ClientMetadata, "槽位 %s 的画像必须已有 ClientMetadata 节", mode)
		var section profilecontract.ClientMetadataSection
		require.NoError(t, json.Unmarshal(doc.ClientMetadata, &section))
		mutate(&section)
		raw, err := json.Marshal(section)
		require.NoError(t, err)
		doc.ClientMetadata = raw
	})
}

func replayObjectKeys(object gjson.Result) []string {
	var keys []string
	object.ForEach(func(key, _ gjson.Result) bool {
		keys = append(keys, key.String())
		return true
	})
	return keys
}

func replaySubtract(keys []string, remove ...string) []string {
	var out []string
	for _, key := range keys {
		if !slices.Contains(remove, key) {
			out = append(out, key)
		}
	}
	return out
}

// replayConfigDependentTurnMetadataKeys 是官方 turn metadata 里取值取决于用户本地配置或模型目录、网关没有可信来源的四个键
// （SPEC-BODY-009：网关只生成有可信取值的键）：沙箱模式、自动审阅开关、node REPL 的两个开关。网关不写，是刻意的。
var replayConfigDependentTurnMetadataKeys = []string{
	"sandbox_mode", "auto_review_enabled", "node_repl_auto_review_required", "node_repl_disabled",
}

// 根线程键（画像 TurnMetadata.RootThreadKeys）。官方 0.165 会话（c1）的 turn metadata：根线程带 agent_name（/root），回合帧带
// root_turn_id（等于 turn_id），预热帧没有 root_turn_id；与官方相比只差四个取决于本地配置的键（刻意不写，见
// replayConfigDependentTurnMetadataKeys），其余键的集合与相对顺序一致。画像不声明（对照）时保持旧行为：不写这两个键。
//
// client_metadata 的三个动态键由 Executor 编译器按画像 ClientMetadata.DynamicKeys 写入（官方出站字节层面），
// 见 officialegress 包 TestWebSocketFrameClientMetadataDynamicKeysFollowProfile 与本文件的语义帧用例。
func TestOpenAIWSRootThreadKeysMatchOfficialTurnMetadata(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	var steps []replayStep
	for index := 1; index <= 6; index++ {
		steps = append(steps, session.step(t, index))
	}

	withReplayOnSameConnectionProfile(t, mode, true)
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) {
		section.WindowTracking = true
		section.WSCompactionRequests = true
		section.RootThreadKeys = true
	})
	handshakes, sent := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 6)

	handshakeTM := replayHandshakeTurnMetadata(handshakes[0])
	require.Equal(t, "/root", handshakeTM.Get("agent_name").String(), "握手 turn metadata 带根线程的 agent_name")
	require.False(t, handshakeTM.Get("root_turn_id").Exists(), "握手是预热形态，没有 root_turn_id")

	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		prewarm := gjson.GetBytes(frame, "generate").Type == gjson.False
		tm := replayFrameTurnMetadata(frame)
		officialTM := replayFrameTurnMetadata(sent[index])
		require.Equal(t, "/root", tm.Get("agent_name").String(), label)
		if prewarm {
			require.False(t, tm.Get("root_turn_id").Exists(), label)
		} else {
			require.NotEmpty(t, tm.Get("turn_id").String(), label)
			require.Equal(t, tm.Get("turn_id").String(), tm.Get("root_turn_id").String(), label+"：根线程的 root_turn_id 等于 turn_id")
		}
		// turn metadata：键集合与官方只差四个取决于本地配置的键；键序一致。
		wantKeys := replaySubtract(replayObjectKeys(officialTM), replayConfigDependentTurnMetadataKeys...)
		require.Equal(t, wantKeys, replayObjectKeys(tm), label+"：turn metadata 的键集合与键序与官方一致（除四个配置相关键）")
	}

	// 对照：不声明。
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) {
		section.WindowTracking = false
		section.WSCompactionRequests = false
		section.RootThreadKeys = false
	})
	legacyHandshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	for index, frame := range codexGateResponseCreateFrames(legacyHandshakes[0]) {
		label := fmt.Sprintf("对照第 %d 帧", index+1)
		require.False(t, replayFrameTurnMetadata(frame).Get("agent_name").Exists(), label)
		require.False(t, replayFrameTurnMetadata(frame).Get("root_turn_id").Exists(), label)
	}
}

// 语义帧里的动态 client_metadata 事实：根线程的回合帧带 root_turn_id（预热帧与子代理线程不带），Lite 连接带 Lite 标记，
// 每帧带发送时间戳；这些事实交给 Executor 编译器，按画像 ClientMetadata.DynamicKeys 决定是否出站。
func TestOpenAIWSSemanticFrameCarriesDynamicClientMetadataFacts(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	withTurnMetadataSectionProfile(t, mode, func(section *profilecontract.TurnMetadataSection) { section.RootThreadKeys = true })

	prepare := func(subagent bool) (prewarm, business []byte) {
		prewarmFrame := session.frames[1]
		gin.SetMode(gin.TestMode)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
		applyOfficialOpenAIWSIngressHeadersForTest(c, prewarmFrame)
		ctx, err := attachOfficialEgressWebSocketContext(
			withOpenAIResponsesLiteCapability(context.Background(), true), c, newOfficialOpenAIHTTPTestAccount(94),
			"wss://chatgpt.com/backend-api/codex/responses", prewarmFrame, wsLegacyConfigForMode(mode),
		)
		require.NoError(t, err)
		egressContext, ok := OfficialEgressContextFromContext(ctx)
		require.True(t, ok)
		require.NotNil(t, egressContext.openAIWSDerived)
		if subagent {
			egressContext.codexRuntimeState.ConditionalHeaders["x-openai-subagent"] = "review"
		}
		run := func(raw []byte, expectedPrev string) []byte {
			t.Helper()
			candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(raw)
			require.NoError(t, err)
			final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, raw, candidate, expectedPrev, false)
			require.NoError(t, err)
			return final
		}
		prewarm = run(prewarmFrame, "")
		business = run(session.frames[2], "resp_informal_0001")
		return prewarm, business
	}

	prewarm, business := prepare(false)
	require.False(t, gjson.GetBytes(prewarm, "client_metadata.root_turn_id").Exists(), "预热帧没有 root_turn_id")
	require.Equal(t, "true", gjson.GetBytes(prewarm, "client_metadata."+responsesLiteWSMetadataKey).String(), "Lite 连接的帧带 Lite 标记")
	require.NotEmpty(t, gjson.GetBytes(prewarm, `client_metadata.x-codex-ws-stream-request-start-ms`).String())
	turnID := gjson.GetBytes(business, "client_metadata.turn_id").String()
	require.NotEmpty(t, turnID)
	require.Equal(t, turnID, gjson.GetBytes(business, "client_metadata.root_turn_id").String(), "根线程回合帧的 root_turn_id 等于 turn_id")

	_, subagentBusiness := prepare(true)
	require.False(t, gjson.GetBytes(subagentBusiness, "client_metadata.root_turn_id").Exists(), "子代理线程没有官方样本，不写 root_turn_id")
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
