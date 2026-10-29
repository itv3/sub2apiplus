package service

import (
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// ============================================================================
// Codex 0.165.0 的晋升后判别式门禁（门禁映射为本轮每条受影响规则各绑一个用例）。
//
// 本轮受影响规则：SPEC-WS-005、SPEC-BODY-004、SPEC-BODY-006、SPEC-BODY-008、SPEC-BODY-009、SPEC-EP-021、SPEC-HDR-007。
// 七类判别式结构事实（都是画像可选节里的开关，由本轮的画像补丁清单新增）：
//   - WebSocketContinuation.ReplayOnSameConnection（WS-005：官方客户端的整段重放留在原上游连接上，网关不重连、不补预热）；
//   - TurnState.ResetOnNewTurn（BODY-004：turn-state 只在产生它的回合内回送）；
//   - ReasoningEffort.ConfigurationUpdate=http_first_turn（BODY-006：HTTP 首轮为第三方入站合成官方形态的 configuration_update）；
//   - TurnMetadata.WindowTracking（HDR-007：压缩后窗口号推进，窗口头与 turn metadata 的窗口键随之变化）；
//   - TurnMetadata.WSCompactionRequests 与 HTTPCompactionRequests（EP-021：压缩请求的 turn metadata 与请求体形态）；
//   - TurnMetadata.RootThreadKeys（BODY-009：根线程的 agent_name 与 root_turn_id）；
//   - ClientMetadata.DynamicKeys（BODY-008：client_metadata 的三个动态键由 Executor 编译器写入）。
//
// 与「行为用例」（openai_ws_same_connection_replay_test.go 等）的区别：行为用例用夹具把槽位画像的开关强行置位后再断言，
// 证明的是网关代码能做这些事；本文件的用例用 codexGateDiscriminatingReleaseMode 按画像结构事实定位目标槽位——候选态目标装在
// previous，晋升后在 active，两个阶段都成立，不写死槽位名——然后**不置位任何开关**，直接在该槽位的正式画像上重放官方 0.165
// 实采会话，证明被晋升的画像确实声明了这些事实、并且行为与官方一致；另一槽位（上一版）作对照，证明行为由画像驱动。
//
// 这些用例只在目录里存在 0.165 画像的形态（候选态与晋升后）成立；没有目标制品的集成态会按「目标制品未入库」失败，
// 与上一轮的判别式门禁同样，随候选链进入收尾分支。下一轮升级之后两个槽位都具备这些事实，用例按继承特征改走
// codexGateTargetReleaseMode（做法见 official_egress_codex_promotion_gate_0164_test.go）。
// ============================================================================

// codexGateRequireControlReleaseMode 返回另一个槽位作对照。本文件的用例用 codexGateDiscriminatingReleaseMode 定位，
// 能走到这里说明另一槽位不具备该事实，对照必然成立；这里显式要求，避免对照段被悄悄省略。
func codexGateRequireControlReleaseMode(t *testing.T, mode string) string {
	t.Helper()
	control, ok := codexGateControlReleaseMode(t, mode)
	require.True(t, ok, "判别式门禁的另一槽位必须不具备该结构事实")
	return control
}

func codexGateDeclaresReplayOnSameConnection(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().WebSocketContinuation
	return section != nil && section.ReplayOnSameConnection
}

func codexGateDeclaresTurnStateReset(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().TurnState
	return section != nil && section.ResetOnNewTurn
}

func codexGateDeclaresHTTPFirstTurnConfigurationUpdate(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().ReasoningEffort
	return section != nil && section.ConfigurationUpdate == profilecontract.ReasoningEffortConfigurationUpdateHTTPFirstTurn
}

func codexGateDeclaresWindowTracking(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().TurnMetadata
	return section != nil && section.WindowTracking
}

func codexGateDeclaresCompactionRequests(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().TurnMetadata
	return section != nil && section.WSCompactionRequests && section.HTTPCompactionRequests
}

func codexGateDeclaresRootThreadKeys(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().TurnMetadata
	return section != nil && section.RootThreadKeys
}

var codexGateApprovedDynamicClientMetadataKeys = []string{
	"root_turn_id", "ws_request_header_x_openai_internal_codex_responses_lite", "x-codex-ws-stream-request-start-ms",
}

func codexGateDeclaresDynamicClientMetadataKeys(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().ClientMetadata
	if section == nil || len(section.DynamicKeys) != len(codexGateApprovedDynamicClientMetadataKeys) {
		return false
	}
	for index, key := range codexGateApprovedDynamicClientMetadataKeys {
		if section.DynamicKeys[index] != key {
			return false
		}
	}
	return true
}

// codexGateC1Steps 返回官方 0.165 压缩续聊会话（c1）的六个客户端帧：预热、首个业务帧、增量、压缩请求、压缩后新窗口、恢复增量。
func codexGateC1Steps(t *testing.T) (replay0165Session, []replayStep) {
	t.Helper()
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	var steps []replayStep
	for index := 1; index <= 6; index++ {
		steps = append(steps, session.step(t, index))
	}
	return session, steps
}

// codexGateRunC1 在 mode 槽位上不置位任何开关地重放 c1 会话，返回第一条上游握手与六个出站帧。
// 目标槽位声明了 ReplayOnSameConnection，整个会话（含压缩后的整段重放）在同一条上游连接上完成，六帧都在这条连接上。
func codexGateRunC1(t *testing.T, mode string) (codexGateWireRequest, [][]byte) {
	t.Helper()
	_, steps := codexGateC1Steps(t)
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1, "%s 槽位：c1 会话在同一条上游连接上完成", mode)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 6, "%s 槽位：c1 会话出站六帧", mode)
	return handshakes[0], frames
}

// codexGateRunC1Control 在对照槽位上重放 c1 会话。对照槽位不声明 ReplayOnSameConnection，压缩后的整段重放由新连接承接（先补预热帧），
// 所以返回所有握手上的出站帧（按连接顺序拼接）：前四帧（预热、首个业务帧、增量、压缩请求）仍是客户端原帧，下标与目标槽位一致。
func codexGateRunC1Control(t *testing.T, mode string) [][]byte {
	t.Helper()
	_, steps := codexGateC1Steps(t)
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.NotEmpty(t, handshakes)
	var frames [][]byte
	for _, handshake := range handshakes {
		frames = append(frames, codexGateResponseCreateFrames(handshake)...)
	}
	require.GreaterOrEqual(t, len(frames), 6, "%s 槽位：对照会话至少出站六帧", mode)
	return frames
}

// SPEC-WS-005（change）：官方客户端的三种整段重放（换模型、压缩后新窗口、不支持 update 的模型改推理强度）都发生在原上游连接上。
//
// 检查项与网关观测（真实 wire，官方 0.165 实采会话 m1／c1／e6 与三种合成场景）：
//   - 重放帧：无 previous_response_id、无 generate，全量 input 原样，同一条连接，不新建握手、不补预热帧；
//   - 实采会话逐帧：previous_response_id／generate 的有无、模型、顶层字段序、input 项类型序列与官方帧一致；
//   - 重放之后客户端的增量帧续接重放帧的响应。
//
// 对照：另一槽位的画像没有 ReplayOnSameConnection，整段重放由新连接承接，先发网关补的预热帧，业务帧链在预热响应上。
func TestCodexReplayOnSameConnectionReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "WebSocketContinuation 声明 ReplayOnSameConnection", codexGateDeclaresReplayOnSameConnection)

	for _, scenario := range replayScenarios() {
		t.Run(scenario.name, func(t *testing.T) {
			handshakes := replayRunIngress(t, mode, replayOfficialUserAgent, replaySteps(t, scenario))
			require.Len(t, handshakes, 1, "整段重放不得新建上游连接")
			frames := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, frames, 5, "预热、首个业务帧、增量帧、重放帧、重放之后的增量帧，网关不补任何帧")
			replay := frames[3]
			require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists(), "重放帧不得带 previous_response_id：%s", replay)
			require.False(t, gjson.GetBytes(replay, "generate").Exists(), "重放帧不得带 generate：%s", replay)
			require.Equal(t, scenario.replayModel, gjson.GetBytes(replay, "model").String())
			require.Equal(t, len(scenario.replayInput), len(gjson.GetBytes(replay, "input").Array()), "全量 input 的项数原样")
			for _, fragment := range scenario.mustContain {
				require.Contains(t, string(replay), fragment, "全量历史必须原样发往上游")
			}
			require.Equal(t, 1, replayCountPrewarmFrames(frames), "不得补出第二个预热帧")
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_4", handshakes[0].connection),
				gjson.GetBytes(frames[4], "previous_response_id").String(), "重放之后的增量帧续接重放帧的响应")
		})
	}

	for _, item := range replay0165Cases() {
		t.Run("official/"+item.fixture, func(t *testing.T) {
			session := loadCodex0165Session(t, item.fixture)
			var steps []replayStep
			for index := 1; index <= item.frames; index++ {
				steps = append(steps, session.step(t, index))
			}
			handshakes, sent := replayRunSteps(t, mode, replay0165Manifest, steps)
			require.Len(t, handshakes, 1, "%s的整段重放不得新建上游连接", item.reason)
			require.Equal(t, session.header("x-codex-routing-hint"), handshakes[0].header.Get("x-codex-routing-hint"),
				"握手里的 routing hint 保持首个模型，换模型后不重新握手")
			frames := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, frames, item.frames, "网关不补任何帧")
			require.Equal(t, 1, replayCountPrewarmFrames(frames), "整个会话里只有客户端自己的那个预热帧")
			for index := range frames {
				label := fmt.Sprintf("第 %d 帧", index+1)
				require.Equal(t, gjson.GetBytes(sent[index], "previous_response_id").Exists(), gjson.GetBytes(frames[index], "previous_response_id").Exists(),
					label+"：previous_response_id 的有无与官方一致")
				require.Equal(t, gjson.GetBytes(sent[index], "generate").Raw, gjson.GetBytes(frames[index], "generate").Raw, label+"：generate 与官方一致")
				require.Equal(t, gjson.GetBytes(sent[index], "model").String(), gjson.GetBytes(frames[index], "model").String(), label+"：模型")
				require.Equal(t, replayInputTypes(sent[index]), replayInputTypes(frames[index]), label+"：input 项类型序列与官方一致")
				if gjson.GetBytes(sent[index], "input.0.type").String() != "compaction_trigger" {
					require.Equal(t, codexGateJSONFieldOrder(t, sent[index]), codexGateJSONFieldOrder(t, frames[index]), label+"：顶层字段序与官方一致")
				}
			}
			if item.followUpAt > 0 {
				require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_%d", handshakes[0].connection, item.replayAt),
					gjson.GetBytes(frames[item.followUpAt-1], "previous_response_id").String(), "重放之后的增量帧续接重放帧的响应")
			}
		})
	}

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		for _, scenario := range replayScenarios() {
			t.Run("control/"+scenario.name, func(t *testing.T) {
				handshakes := replayRunIngress(t, control, replayOfficialUserAgent, replaySteps(t, scenario))
				require.Len(t, handshakes, 2, "对照：另一槽位整段重放由新连接承接")
				second := codexGateResponseCreateFrames(handshakes[1])
				require.Len(t, second, 3, "对照新连接：网关补的预热、链上预热响应的业务帧、重放之后的增量帧")
				require.Equal(t, gjson.False, gjson.GetBytes(second[0], "generate").Type, "对照：新连接先发网关补的预热帧")
				require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[1].connection),
					gjson.GetBytes(second[1], "previous_response_id").String(), "对照：业务帧链上新连接自己的预热响应")
			})
		}
	}
}

// SPEC-BODY-004（change）：turn-state 的作用域是回合。官方每个回合新建模型客户端会话，turn-state 只在产生它的回合内回送：
// 紧跟客户端预热帧的第一个业务帧回送预热响应里收到的值；同一回合内的工具结果续轮回送本回合已收到的值；新用户轮次的第一个请求不回送上一回合的值。
//
// 对照：另一槽位的画像没有 ResetOnNewTurn，保存的值回送给之后的每个请求，跨回合也回送。
func TestCodexTurnStateResetReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "TurnState 声明 ResetOnNewTurn", codexGateDeclaresTurnStateReset)
	require.Equal(t, []string{
		"",                         // 预热帧：还没有任何值
		replayTurnStateValue(1, 1), // 回合一首帧：预热响应里收到的值随预热过的会话交给第一个回合
		replayTurnStateValue(1, 2), // 同一回合内的工具结果续轮：回送本回合已收到的值
		"",                         // 回合二首帧：新用户轮次，不回送上一回合的值
	}, replayRunTurnStateSession(t, mode), "目标槽位：turn-state 只在产生它的回合内回送")

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		require.Equal(t, []string{
			"", replayTurnStateValue(1, 1), replayTurnStateValue(1, 2), replayTurnStateValue(1, 3),
		}, replayRunTurnStateSession(t, control), "对照：另一槽位保持旧行为，上一回合收到的值跨回合回送")
	}
}

// SPEC-BODY-006（change）：画像声明 ReasoningEffort.ConfigurationUpdate=http_first_turn 时，第三方客户端的 HTTP 首轮请求（账号模型清单声明
// 该模型支持推理强度更新）在 input 末尾带官方形态的 configuration_update(请求 effort)，紧跟首个用户消息；请求级 reasoning.effort 不变。
//
// 检查项与网关观测（真实 wire 字节，第三方 Kilo 形态入站）：
//   - 画像事实经出站上下文冻结后给定型使用（ConfigurationUpdateHTTPFirstTurn）；
//   - 末项恰为 {"type":"configuration_update","reasoning":{"effort":"high"}}，整个请求里只有这一条，其前一项是用户消息。
//
// 对照：另一槽位的画像不声明该字段，同一入站不合成任何 configuration_update。
func TestCodexConfigurationUpdateReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "ReasoningEffort 声明 ConfigurationUpdate=http_first_turn", codexGateDeclaresHTTPFirstTurnConfigurationUpdate)

	defaultsFor := func(slot string) officialOpenAIReasoningDefaults {
		return officialOpenAIReasoningDefaultsFromContext(NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID: 94, TargetPlatform: PlatformOpenAI, ProfileMode: slot, Transport: OfficialEgressTransportHTTP,
			DefaultReasoningLevel: "low", ReasoningDefaultsKnown: true, SupportsReasoningEffortUpdates: true,
		}))
	}
	require.True(t, defaultsFor(mode).ConfigurationUpdateHTTPFirstTurn, "目标槽位的画像声明经出站上下文冻结为 http_first_turn")

	body := []byte(`{"model":"gpt-6.1-sol","stream":false,"store":true,"instructions":"Kilo raw responses instruction",` +
		`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"KILO_UPDATE_S1_OK"}]}],` +
		`"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],` +
		`"reasoning":{"effort":"high"},"text":{"verbosity":"low"}}`)
	forward := func(slot string) []gjson.Result {
		server := startCodexGateWireServer(t, nil)
		ingress := newOfficialOpenAIHTTPKiloContext(body, "kilo-configuration-update-session")
		ingress.Request.URL.Path = "/v1/responses"
		SetOpenAIClientTransport(ingress, OpenAIClientTransportHTTP)
		wire := codexGateForwardResponsesWire(t, slot, server, ingress, append([]byte(nil), body...),
			func(service *OpenAIGatewayService, account *Account) {
				service.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(cuSolManifest))
			})
		require.Equal(t, "high", gjson.GetBytes(wire.body, "reasoning.effort").String(), "%s 槽位：请求级 effort 不变", slot)
		return gjson.GetBytes(wire.body, "input").Array()
	}

	sent := forward(mode)
	require.NotEmpty(t, sent)
	require.Equal(t, `{"type":"configuration_update","reasoning":{"effort":"high"}}`, sent[len(sent)-1].Raw,
		"目标槽位：官方形态的 update 在 input 末尾，紧跟首个用户消息")
	require.Equal(t, "user", sent[len(sent)-2].Get("role").String())
	count := 0
	for _, item := range sent {
		if item.Get("type").String() == "configuration_update" {
			count++
		}
	}
	require.Equal(t, 1, count, "整个请求里只有一条 configuration_update")

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		require.False(t, defaultsFor(control).ConfigurationUpdateHTTPFirstTurn, "对照：另一槽位的画像不声明该字段")
		for _, item := range forward(control) {
			require.NotEqual(t, "configuration_update", item.Get("type").String(), "对照：另一槽位不合成 configuration_update")
		}
	}
}

// SPEC-HDR-007（change）：上下文窗口跟踪。官方 0.165 压缩续聊会话（c1）的事实：握手里窗口头是 `<线程>:0`，握手 turn metadata 带
// window_id、window_number=0、context_window_id；压缩请求仍在窗口 0；压缩后新窗口的第一帧起 client_metadata 与 turn metadata 里
// 窗口号变 `:1`、window_number=1、context_window_id 换新，同窗口各帧同值；握手头不重发（复用连接上仍是旧窗口号）；
// 推进之后新建连接（上游断开后重放）的握手窗口头与 turn metadata 用推进后的窗口号。
//
// 对照：另一槽位的画像不声明 WindowTracking，窗口号恒为 `:0`，不写 window_number 与 context_window_id。
func TestCodexWindowTrackingReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "TurnMetadata 声明 WindowTracking", codexGateDeclaresWindowTracking)
	_, steps := codexGateC1Steps(t)
	session, _ := codexGateC1Steps(t)

	handshake, frames := codexGateRunC1(t, mode)
	thread := strings.TrimSuffix(handshake.header.Get("x-codex-window-id"), ":0")
	require.NotEqual(t, handshake.header.Get("x-codex-window-id"), thread, "握手窗口头是 <线程>:0")
	handshakeTM := replayHandshakeTurnMetadata(handshake)
	require.Equal(t, thread+":0", handshakeTM.Get("window_id").String())
	require.EqualValues(t, 0, handshakeTM.Get("window_number").Int(), "握手 turn metadata 带 window_number")
	windowZeroContext := handshakeTM.Get("context_window_id").String()
	require.Regexp(t, replayUUIDPattern, windowZeroContext, "握手 turn metadata 带 context_window_id：%s", handshakeTM.Raw)
	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		tm := replayFrameTurnMetadata(frame)
		window := gjson.GetBytes(frame, `client_metadata.x-codex-window-id`).String()
		require.Equal(t, window, tm.Get("window_id").String(), label+"：client_metadata 与 turn metadata 的窗口号一致")
		if index < 4 {
			require.Equal(t, thread+":0", window, label+"：预热、首个业务帧、增量、压缩请求仍在窗口 0")
			require.EqualValues(t, 0, tm.Get("window_number").Int(), label)
			require.Equal(t, windowZeroContext, tm.Get("context_window_id").String(), label+"：窗口 0 的上下文窗口 id 与握手相同")
			continue
		}
		require.Equal(t, thread+":1", window, label+"：压缩后窗口号推进到 :1")
		require.EqualValues(t, 1, tm.Get("window_number").Int(), label)
		require.Regexp(t, replayUUIDPattern, tm.Get("context_window_id").String(), label)
		require.NotEqual(t, windowZeroContext, tm.Get("context_window_id").String(), label+"：上下文窗口 id 换新")
	}
	require.Equal(t, replayFrameTurnMetadata(frames[4]).Get("context_window_id").String(),
		replayFrameTurnMetadata(frames[5]).Get("context_window_id").String(), "同一窗口内上下文窗口 id 不变")
	require.Equal(t, thread+":0", handshake.header.Get("x-codex-window-id"), "复用的连接不重发握手，握手里仍是旧窗口号")
	// 官方样本自身的窗口号事实（夹具自检，保证上面的期望来自官方而不是网关）。
	require.True(t, strings.HasSuffix(gjson.GetBytes(session.frames[5], `client_metadata.x-codex-window-id`).String(), ":1"))

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
	require.Equal(t, strings.TrimSuffix(reconnected[0].header.Get("x-codex-window-id"), ":0")+":1", reconnected[1].header.Get("x-codex-window-id"),
		"新连接握手的窗口头是推进后的 :1，线程部分不变")
	secondTM := replayHandshakeTurnMetadata(reconnected[1])
	require.EqualValues(t, 1, secondTM.Get("window_number").Int())
	require.Equal(t, replayFrameTurnMetadata(frames[4]).Get("context_window_id").String(), secondTM.Get("context_window_id").String(),
		"新连接握手的上下文窗口 id 与窗口 1 的帧相同")

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		legacyFrames := codexGateRunC1Control(t, control)
		for index, frame := range legacyFrames {
			label := fmt.Sprintf("对照第 %d 帧", index+1)
			tm := replayFrameTurnMetadata(frame)
			require.True(t, strings.HasSuffix(gjson.GetBytes(frame, `client_metadata.x-codex-window-id`).String(), ":0"), label+"：旧行为窗口号恒为 :0")
			require.False(t, tm.Get("window_number").Exists(), label)
			require.False(t, tm.Get("context_window_id").Exists(), label)
		}
	}
}

// codexGateCompactionFrameMutation 把官方根会话的 HTTP 请求改写成 HTTP 压缩请求（input 末尾 compaction_trigger，turn metadata 记 compaction）。
func codexGateCompactionFrameMutation(payload map[string]any, _ map[string]any, turnMetadata map[string]any) {
	input, _ := payload["input"].([]any)
	payload["input"] = append(input, map[string]any{"type": "compaction_trigger"})
	turnMetadata["request_kind"] = "compaction"
	turnMetadata["compaction"] = map[string]any{
		"trigger": "manual", "reason": "user_requested", "implementation": "responses_compaction_v2",
		"phase": "standalone_turn", "strategy": "memento",
	}
}

// SPEC-EP-021（change）：压缩请求的官方形态，WS 与 HTTP 两条线。官方 0.165 的压缩请求：WS 压缩帧是 previous_response_id 加单项
// compaction_trigger，HTTP 压缩请求在 input 末尾带 compaction_trigger；请求体不带 access_programs；turn metadata 的 request_kind 是
// compaction，带 compaction 对象，不带 model、reasoning_effort、turn_trigger；压缩前后的普通业务帧不受影响；压缩请求有自己的新回合 id。
//
// 对照：另一槽位的画像不声明两个压缩开关，压缩请求按普通业务请求处理（补 access_programs，WS 的 request_kind 写 turn，HTTP 仍写 model 等键）。
func TestCodexCompactionRequestsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "TurnMetadata 声明 WSCompactionRequests 与 HTTPCompactionRequests", codexGateDeclaresCompactionRequests)
	session, _ := codexGateC1Steps(t)
	_, frames := codexGateRunC1(t, mode)

	official := session.frames[4]
	officialTM := replayFrameTurnMetadata(official)
	compaction := frames[3]
	require.Equal(t, `{"type":"compaction_trigger"}`, gjson.GetBytes(compaction, "input.0").Raw)
	require.Equal(t, codexGateJSONFieldOrder(t, official), codexGateJSONFieldOrder(t, compaction), "WS 压缩帧的顶层字段序与官方一致（没有 access_programs）")
	require.False(t, gjson.GetBytes(compaction, "access_programs").Exists())
	tm := replayFrameTurnMetadata(compaction)
	require.Equal(t, "compaction", tm.Get("request_kind").String())
	require.Equal(t, officialTM.Get("compaction").Raw, tm.Get("compaction").Raw, "compaction 对象的键序与取值与官方一致")
	for _, key := range []string{"model", "reasoning_effort", "turn_trigger"} {
		require.False(t, tm.Get(key).Exists(), "WS 压缩请求的 turn metadata 不带 %s：%s", key, tm.Raw)
	}
	compactionID := tm.Get("turn_id").String()
	require.Regexp(t, replayUUIDPattern, compactionID)
	require.Equal(t, byte('7'), compactionID[14], "压缩回合 id 是 UUIDv7")
	require.NotEqual(t, replayFrameTurnMetadata(frames[2]).Get("turn_id").String(), compactionID, "压缩帧不再沿用前一个业务回合的 id")
	require.NotEqual(t, replayFrameTurnMetadata(frames[4]).Get("turn_id").String(), compactionID, "压缩帧与压缩之后的业务回合 id 不同")
	for _, index := range []int{1, 2, 4, 5} {
		label := fmt.Sprintf("第 %d 帧", index+1)
		require.True(t, gjson.GetBytes(frames[index], "access_programs").Exists(), label+"：普通业务帧照常带 access_programs")
		other := replayFrameTurnMetadata(frames[index])
		require.Equal(t, "turn", other.Get("request_kind").String(), label)
		require.True(t, other.Get("model").Exists() && other.Get("reasoning_effort").Exists() && other.Get("turn_trigger").Exists(), label)
		require.False(t, other.Get("compaction").Exists(), label)
	}

	// HTTP 压缩请求：官方实采（c3）的形态。
	c3 := loadCodex0165HTTPRequests(t)
	officialHTTP := c3[4]
	require.Equal(t, "compaction", officialHTTP.RequestKind)
	require.NotContains(t, officialHTTP.TopLevelKeys, "access_programs")
	officialHTTPKeys := replaySubtract(replayObjectKeys(replayHTTPSampleTurnMetadata(officialHTTP)), replayConfigDependentTurnMetadataKeys...)
	forwardHTTP := func(slot string, mutate func(map[string]any, map[string]any, map[string]any)) (gjson.Result, codexGateWireRequest) {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, mutate)
		wire := codexGateForwardResponsesWire(t, slot, server, ingress, body, nil)
		return gjson.Parse(gjson.GetBytes(wire.body, `client_metadata.x-codex-turn-metadata`).String()), wire
	}
	httpTM, httpWire := forwardHTTP(mode, codexGateCompactionFrameMutation)
	require.Equal(t, "compaction", httpTM.Get("request_kind").String())
	require.Equal(t, officialHTTPKeys, replayObjectKeys(httpTM), "HTTP 压缩请求 turn metadata 的键集合与键序等于官方实采（除四个配置相关键）")
	require.False(t, gjson.GetBytes(httpWire.body, "access_programs").Exists(), "HTTP 压缩请求体不带 access_programs")
	require.Equal(t, []string{"trigger", "reason", "implementation", "phase", "strategy"}, replayObjectKeys(httpTM.Get("compaction")))
	turnTM, _ := forwardHTTP(mode, nil)
	require.Equal(t, "turn", turnTM.Get("request_kind").String())
	for _, key := range []string{"model", "reasoning_effort", "turn_trigger"} {
		require.True(t, turnTM.Get(key).Exists(), "HTTP 普通回合请求仍写 %s", key)
	}

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		legacyFrames := codexGateRunC1Control(t, control)
		legacy := legacyFrames[3]
		require.True(t, gjson.GetBytes(legacy, "access_programs").Exists(), "对照：WS 压缩帧按普通业务帧补 access_programs")
		require.Equal(t, "turn", replayFrameTurnMetadata(legacy).Get("request_kind").String(), "对照：WS 压缩帧的 request_kind 写 turn")
		legacyHTTP, _ := forwardHTTP(control, codexGateCompactionFrameMutation)
		for _, key := range []string{"model", "reasoning_effort", "turn_trigger"} {
			require.True(t, legacyHTTP.Get(key).Exists(), "对照：另一槽位的 HTTP 压缩请求仍写 %s", key)
		}
	}
}

// SPEC-BODY-009（change）：根线程键。官方 0.165 会话（c1）的 turn metadata：根线程带 agent_name（/root），回合帧带 root_turn_id（等于 turn_id），
// 预热帧没有 root_turn_id；握手 turn metadata 带 agent_name、没有 root_turn_id；与官方相比只差四个取决于本地配置的键（刻意不写），
// 其余键的集合与相对顺序一致。HTTP 回合请求同样（官方 c3：agent_name、root_turn_id 在固定相对位置）。
//
// 对照：另一槽位的画像不声明 RootThreadKeys，不写这两个键。
func TestCodexRootThreadKeysReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "TurnMetadata 声明 RootThreadKeys", codexGateDeclaresRootThreadKeys)
	session, _ := codexGateC1Steps(t)
	handshake, frames := codexGateRunC1(t, mode)

	handshakeTM := replayHandshakeTurnMetadata(handshake)
	require.Equal(t, "/root", handshakeTM.Get("agent_name").String(), "握手 turn metadata 带根线程的 agent_name")
	require.False(t, handshakeTM.Get("root_turn_id").Exists(), "握手是预热形态，没有 root_turn_id")
	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		prewarm := gjson.GetBytes(frame, "generate").Type == gjson.False
		tm := replayFrameTurnMetadata(frame)
		officialTM := replayFrameTurnMetadata(session.frames[index+1])
		require.Equal(t, "/root", tm.Get("agent_name").String(), label)
		if prewarm {
			require.False(t, tm.Get("root_turn_id").Exists(), label)
		} else {
			require.NotEmpty(t, tm.Get("turn_id").String(), label)
			require.Equal(t, tm.Get("turn_id").String(), tm.Get("root_turn_id").String(), label+"：根线程的 root_turn_id 等于 turn_id")
		}
		require.Equal(t, replaySubtract(replayObjectKeys(officialTM), replayConfigDependentTurnMetadataKeys...), replayObjectKeys(tm),
			label+"：turn metadata 的键集合与键序与官方一致（除四个配置相关键）")
	}

	// HTTP 回合请求：agent_name 与 root_turn_id 同样出现，root_turn_id 等于 turn_id。
	server := startCodexGateWireServer(t, nil)
	body, ingress := codexGateRootIngress(t, nil)
	wire := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	httpTM := gjson.Parse(gjson.GetBytes(wire.body, `client_metadata.x-codex-turn-metadata`).String())
	require.Equal(t, "/root", httpTM.Get("agent_name").String())
	require.Equal(t, httpTM.Get("turn_id").String(), httpTM.Get("root_turn_id").String(), "HTTP 根线程上 root_turn_id 等于 turn_id")

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		legacyFrames := codexGateRunC1Control(t, control)
		for index, frame := range legacyFrames {
			label := fmt.Sprintf("对照第 %d 帧", index+1)
			require.False(t, replayFrameTurnMetadata(frame).Get("agent_name").Exists(), label)
			require.False(t, replayFrameTurnMetadata(frame).Get("root_turn_id").Exists(), label)
		}
	}
}

// SPEC-BODY-008（change）：client_metadata 的三个动态键（root_turn_id、Lite 连接标记 ws_request_header_x_openai_internal_codex_responses_lite、
// 发送时间戳 x-codex-ws-stream-request-start-ms）由 Executor 编译器按画像 ClientMetadata.DynamicKeys 写入。
//
// 检查项与网关观测（真实 wire，官方 0.165 c1 会话与 c3 HTTP 实采）：
//   - WS：每帧 client_metadata 的键集合等于官方实采（预热帧 10 键、回合帧 11 键）；root_turn_id 只在回合帧、等于该帧 turn_id；
//     Lite 标记取值 "true"；发送时间戳是 13 位 Unix 毫秒；
//   - HTTP：回合请求与压缩请求的 client_metadata 都是官方同一组 9 个键，含 root_turn_id（等于 turn_id），不带 WS 专属的时间戳与 Lite 标记。
//
// 对照：另一槽位的画像没有 DynamicKeys，WS 帧的 client_metadata 里没有 root_turn_id。
func TestCodexClientMetadataDynamicKeysReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "ClientMetadata 声明三个动态键", codexGateDeclaresDynamicClientMetadataKeys)
	session, _ := codexGateC1Steps(t)
	_, frames := codexGateRunC1(t, mode)
	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		wantKeys := replayObjectKeys(gjsonGet(session.frames[index+1], "client_metadata"))
		gotKeys := replayObjectKeys(gjsonGet(frame, "client_metadata"))
		require.ElementsMatch(t, wantKeys, gotKeys, label+"：出站 client_metadata 键集合等于官方实采")
		if gjsonResultIsFalse(frame, "generate") {
			require.Len(t, gotKeys, 10, label+"：预热帧 10 键")
			require.False(t, gjsonGet(frame, "client_metadata.root_turn_id").Exists(), label)
		} else {
			require.Len(t, gotKeys, 11, label+"：回合帧 11 键")
			require.Equal(t, gjsonGet(frame, "client_metadata.turn_id").String(), gjsonGet(frame, "client_metadata.root_turn_id").String(),
				label+"：根线程上 root_turn_id 等于 turn_id")
		}
		require.Equal(t, "true", gjsonGet(frame, "client_metadata.ws_request_header_x_openai_internal_codex_responses_lite").String(), label)
		require.Regexp(t, `^[0-9]{13}$`, gjsonGet(frame, "client_metadata.x-codex-ws-stream-request-start-ms").String(), label)
	}

	officialHTTPKeys := map[string]bool{}
	for _, sample := range loadCodex0165HTTPRequests(t) {
		keys := make([]string, 0, len(sample.ClientMetadata))
		for key := range sample.ClientMetadata {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		officialHTTPKeys[strings.Join(keys, ",")] = true
	}
	require.Len(t, officialHTTPKeys, 1, "官方实采的 7 个 HTTP 请求 client_metadata 键集合一致")
	var wantHTTPKeys []string
	for joined := range officialHTTPKeys {
		wantHTTPKeys = strings.Split(joined, ",")
	}
	for name, mutate := range map[string]func(map[string]any, map[string]any, map[string]any){
		"普通回合请求": nil, "压缩请求": codexGateCompactionFrameMutation,
	} {
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, mutate)
		wire := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
		metadata := gjson.GetBytes(wire.body, "client_metadata")
		require.ElementsMatch(t, wantHTTPKeys, replayObjectKeys(metadata), name+"：HTTP client_metadata 键集合等于官方实采")
		require.Equal(t, metadata.Get("turn_id").String(), metadata.Get("root_turn_id").String(), name+"：根线程上 root_turn_id 等于 turn_id")
		require.False(t, metadata.Get("x-codex-ws-stream-request-start-ms").Exists(), name+"：发送时间戳是 WS 专属")
		require.False(t, metadata.Get("ws_request_header_x_openai_internal_codex_responses_lite").Exists(), name+"：Lite 在 HTTP 上是请求头")
	}

	{
		control := codexGateRequireControlReleaseMode(t, mode)
		legacyFrames := codexGateRunC1Control(t, control)
		for index, frame := range legacyFrames {
			require.False(t, gjsonGet(frame, "client_metadata.root_turn_id").Exists(), fmt.Sprintf("对照第 %d 帧：另一槽位不写 root_turn_id", index+1))
		}
	}
}
