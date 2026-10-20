package service

import (
	"crypto/sha256"
	"fmt"
	"net/http"
	"strconv"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// WS 首帧没有基础指令来源时的默认指令回退。
//
// 画像声明基础指令载体（BaseInstructions 节列出 responses_ws）后，WS 新链首帧的预热帧 input 只取前缀（前导
// additional_tools 与基础指令消息）。第三方客户端的首帧既没有顶层 instructions、也没有带标记的消息、只有普通 developer
// 消息时，原先没有任何前缀可承载，预热帧构造失败。设计决定：WS 与 HTTP 同源——没有来源的新链帧回退到模型默认 Codex
// 指令，也就是 HTTP 路径对缺 instructions 的 OAuth 请求注入默认指令的同一个来源和函数（defaultCodexSynthInstructions），
// 再按有来源的新链安装成官方形态的基础指令消息。
//
// 约定：
//   - 有来源（顶层 instructions、带标记的消息）时行为不变；
//   - 续链帧没有来源时不回退（基础指令已在这条链的第一帧里）；
//   - 画像没有声明载体节（旧画像）时整段不进入，帧与旧形态逐字节相同（旧形态黄金值由
//     official_egress_openai_ws_legacy_golden_test.go 固化）；
//   - 帧内 model 为空或未知时，回退结果与 HTTP 对同样模型字符串注入的默认指令完全一致。

// wsDefaultInstructionsHTTPOracle 返回 HTTP 路径对缺 instructions 的请求体注入的默认指令：OAuth 转换里的
// applyInstructions 按 reqBody["model"] 调用 defaultCodexSynthInstructions，与 Forward 的注入点是同一个函数。
func wsDefaultInstructionsHTTPOracle(t *testing.T, model string) string {
	t.Helper()
	body := map[string]any{}
	if model != "" {
		body["model"] = model
	}
	require.True(t, applyInstructions(body, false), "前提：缺 instructions 的请求体会被 HTTP 路径补上默认指令")
	text, ok := body["instructions"].(string)
	require.True(t, ok)
	require.NotEmpty(t, text)
	return text
}

// wsDefaultInstructionsModels 是回退用例覆盖的帧内模型：非 Codex 系的最新版本、Codex 系、其他版本、空模型、未知模型、
// 带多余空白的模型。表内必须有不同的默认指令，否则“取错来源”的反例无从区分。
var wsDefaultInstructionsModels = []string{
	"gpt-5.5", "gpt-5.3-codex", "gpt-5.2", "gpt-5.1", "gpt-6-astra", "gpt-6-luna", "gpt-unknown-9", "  gpt-5.5  ", "",
}

func TestWSDefaultInstructionsModelTableIsDiscriminating(t *testing.T) {
	distinct := map[string]struct{}{}
	for _, model := range wsDefaultInstructionsModels {
		distinct[wsDefaultInstructionsHTTPOracle(t, model)] = struct{}{}
	}
	require.GreaterOrEqual(t, len(distinct), 3, "模型表必须覆盖至少三种不同的默认指令，否则取错来源的反例无从区分")
	require.Equal(t, wsDefaultInstructionsHTTPOracle(t, "gpt-5.5"), wsDefaultInstructionsHTTPOracle(t, "  gpt-5.5  "),
		"多余空白不改变选择")
	require.Equal(t, wsDefaultInstructionsHTTPOracle(t, "gpt-unknown-9"), wsDefaultInstructionsHTTPOracle(t, ""),
		"空模型与未知模型都落到最新版本的默认指令")
	require.NotEqual(t, wsDefaultInstructionsHTTPOracle(t, "gpt-5.5"), wsDefaultInstructionsHTTPOracle(t, "gpt-5.3-codex"))
}

// wsDefaultInstructionsPayload 构造第三方帧：默认是只有普通 developer 消息和用户消息的新链首帧（带一个函数工具，Lite 会把它归入前导
// additional_tools），不带带标记的消息；model 为空时不写该字段；previousResponseID 非空时是续链增量帧。
func wsDefaultInstructionsPayload(model string, instructionsMember string, previousResponseID string, items ...string) []byte {
	member := ""
	if model != "" {
		member += `"model":` + strconv.Quote(model) + `,`
	}
	if instructionsMember != "" {
		member += `"instructions":` + instructionsMember + `,`
	}
	if previousResponseID != "" {
		member += `"previous_response_id":` + strconv.Quote(previousResponseID) + `,`
	}
	if len(items) == 0 {
		items = []string{wsDefaultInstructionsDeveloperItem, baseInstructionsUserItem}
	}
	input := ""
	for index, item := range items {
		if index > 0 {
			input += ","
		}
		input += item
	}
	// 工具声明只在链的第一帧出现（官方客户端的前缀只在新链首帧），续链增量帧不再重发。
	tools := ""
	if previousResponseID == "" {
		tools = `,"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]`
	}
	return []byte(`{"type":"response.create",` + member + `"input":[` + input + `]` + tools + `}`)
}

const wsDefaultInstructionsDeveloperItem = `{"type":"message","role":"developer","content":[{"type":"input_text","text":"developer context"}]}`

// 没有来源的新链首帧：预热帧、业务帧与连接状态都按“装入了默认指令”处理，默认指令随帧内模型走，与 HTTP 完全一致。
func TestOpenAIWSFirstFrameWithoutBaseInstructionsFallsBackToTheHTTPDefault(t *testing.T) {
	section := wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		for _, model := range wsDefaultInstructionsModels {
			for _, instructions := range []string{"", `"   "`} {
				label := fmt.Sprintf("model=%q/instructions=%s/lite=%v", model, instructions, lite)
				want := wsDefaultInstructionsHTTPOracle(t, model)
				payload := wsDefaultInstructionsPayload(model, instructions, "")
				ctx, egressContext := wsBaseContext(t, lite, payload)

				first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
				require.NoError(t, err, label)
				wantFirst := []string{"BASE", "message:developer", "message:user"}
				wantPrefix := []string{"BASE"}
				if lite {
					wantFirst = append([]string{"tools"}, wantFirst...)
					wantPrefix = append([]string{"tools"}, wantPrefix...)
				}
				require.Equal(t, wantFirst, describeWSInput(first), label)
				require.False(t, gjson.GetBytes(first, "instructions").Exists(), label+"：画像声明了载体就不能再发顶层 instructions")
				require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, want)}, wsBaseItems(first),
					label+"：基础指令消息的正文就是 HTTP 对同一模型注入的默认指令")
				sent, digest := wsBaseState(egressContext)
				require.True(t, sent, label)
				require.Equal(t, sha256.Sum256([]byte(want)), digest, label)

				prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
				require.NoError(t, err, label+"：有了基础指令消息，预热帧就有可承载的前缀")
				require.True(t, inject, label)
				require.Equal(t, wantPrefix, describeWSInput(prewarm), label+"/prewarm：input 只取前缀")
				require.False(t, gjson.GetBytes(prewarm, "generate").Bool(), label)
				require.Equal(t, wsBaseItems(first), wsBaseItems(prewarm), label+"/prewarm：基础指令消息逐字节不变")

				chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
				require.NoError(t, err, label)
				require.Equal(t, []string{"message:developer", "message:user"}, describeWSInput(chain), label+"/chain：剔除前缀，普通 developer 消息留在业务帧")
				require.Empty(t, wsBaseItems(chain), label)
			}
		}
	}
}

// 有来源时行为不变：顶层 instructions、带标记的消息都原样胜出，不会被默认指令覆盖。
func TestOpenAIWSFirstFrameWithABaseInstructionsSourceIgnoresTheDefault(t *testing.T) {
	section := wsBaseSection(t)
	require.NotEqual(t, wsBaseHelperText, wsDefaultInstructionsHTTPOracle(t, "gpt-5.5"), "前提：来源正文与默认指令不同")
	tagged := `{"type":"message","id":"msg_inbound","role":"developer","content":[{"type":"input_text","text":"` + wsBaseHelperText + `"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	cases := []struct {
		name         string
		instructions string
		items        []string
	}{
		{"顶层 instructions", strconv.Quote(wsBaseHelperText), nil},
		{"带标记的消息", "", []string{tagged, wsDefaultInstructionsDeveloperItem, baseInstructionsUserItem}},
	}
	for _, lite := range []bool{false, true} {
		for _, tc := range cases {
			label := tc.name + "/lite=" + strconv.FormatBool(lite)
			payload := wsDefaultInstructionsPayload("gpt-5.5", tc.instructions, "", tc.items...)
			ctx, egressContext := wsBaseContext(t, lite, payload)
			first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
			require.NoError(t, err, label)
			require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseHelperText)}, wsBaseItems(first),
				label+"：来源正文原样装入，不是默认指令")
			_, digest := wsBaseState(egressContext)
			require.Equal(t, sha256.Sum256([]byte(wsBaseHelperText)), digest, label)
		}
	}
}

// 回退只属于新链：续链帧没有来源时不回退，不动连接状态；回退装入的默认指令摘要和有来源的新链一样参与续链去重。
func TestOpenAIWSDefaultInstructionsFallbackStaysOnTheFirstFrameOfTheChain(t *testing.T) {
	section := wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		label := "lite=" + strconv.FormatBool(lite)
		officialCodexBaseInstructionsChangedWarned.Store(false)
		t.Cleanup(func() { officialCodexBaseInstructionsChangedWarned.Store(false) })
		defaultText := wsDefaultInstructionsHTTPOracle(t, "gpt-5.5")
		first := wsDefaultInstructionsPayload("gpt-5.5", "", "")
		ctx, egressContext := wsBaseContext(t, lite, first)
		prepare := func(frame []byte, expectedPrev string) []byte {
			t.Helper()
			final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, frame, frame, expectedPrev, false)
			require.NoError(t, err, label)
			return final
		}
		sentWith := func(text string) {
			t.Helper()
			sent, digest := wsBaseState(egressContext)
			require.True(t, sent, label)
			require.Equal(t, sha256.Sum256([]byte(text)), digest, label)
		}

		frame1 := prepare(first, "")
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, defaultText)}, wsBaseItems(frame1), label)
		sentWith(defaultText)

		// 续链帧没有来源：不回退、不追加，连接状态不变。
		frame2 := prepare(wsDefaultInstructionsPayload("gpt-5.5", "", wsBasePrevID, baseInstructionsUserItem), wsBasePrevID)
		require.Equal(t, []string{"message:user"}, describeWSInput(frame2), label+"：续链帧不回退")
		sentWith(defaultText)

		// 续链帧重复了默认指令的正文（第三方客户端每帧重发 instructions 的习惯）：与已发送的摘要相同，丢弃，不追加也不提示。
		frame3 := prepare(wsDefaultInstructionsPayload("gpt-5.5", strconv.Quote(defaultText), wsBasePrevID, baseInstructionsUserItem), wsBasePrevID)
		require.Equal(t, []string{"message:user"}, describeWSInput(frame3), label+"：重复的默认指令被丢弃")
		require.False(t, officialCodexBaseInstructionsChangedWarned.Load(), label)
		sentWith(defaultText)

		// 另起一条新链仍然回退并安装（新链上的上下文里还没有它）。
		frame4 := prepare(first, "")
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, defaultText)}, wsBaseItems(frame4), label)
	}
}

// 非字符串的 instructions 照旧失败关闭：回退只处理“没有来源”，不吞掉写错的来源。
func TestOpenAIWSDefaultInstructionsFallbackDoesNotSwallowMalformedInstructions(t *testing.T) {
	wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		payload := wsDefaultInstructionsPayload("gpt-5.5", `123`, "")
		ctx, _ := wsBaseContext(t, lite, payload)
		_, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.ErrorContains(t, err, "must be a string", "lite=%v", lite)
	}
}

// ---- 真实 wire：第三方 WS 入站会话 ----

func TestCodexWebSocketFirstFrameWithoutBaseInstructionsFallsBackToDefaultInstructions(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	control, hasControl := codexGateControlReleaseMode(t, mode)

	cases := []struct {
		name         string
		model        string
		instructions string
	}{
		{"没有 instructions", codexGateNonLiteModel, ""},
		{"空白 instructions", codexGateNonLiteModel, "   "},
		{"Codex 系模型", "gpt-5.3-codex", ""},
		{"未知模型", "gpt-unknown-9", ""},
	}
	for _, tc := range cases {
		steps := []func(string) []byte{
			func(string) []byte {
				return codexGateWSIngressFrame(t, tc.model, "", tc.instructions,
					[]any{codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")}, nil)
			},
			func(previous string) []byte {
				return codexGateWSIngressFrame(t, tc.model, previous, "", []any{codexGateMessage("user", "turn two")}, nil)
			},
		}
		want := wsDefaultInstructionsHTTPOracle(t, tc.model)

		server, handshakes := codexGateRunWSIngress(t, mode, steps)
		label := tc.name + "/目标槽位 " + mode
		require.Len(t, handshakes, 1, label)
		require.Empty(t, server.httpRequestsForPath(codexGateResponsesPath), label+"：官方 WS 入站不得回落 HTTP")
		frames := codexGateResponseCreateFrames(handshakes[0])
		require.Len(t, frames, 3, label+"：预热、首轮业务帧、增量帧；原先预热帧构造失败，连接在首帧就被断开")
		prewarm, business, incremental := frames[0], frames[1], frames[2]
		for index, raw := range frames {
			require.False(t, gjson.GetBytes(raw, "instructions").Exists(), "%s 第 %d 帧：顶层不得有 instructions", label, index+1)
		}

		require.Equal(t, gjson.False, gjson.GetBytes(prewarm, "generate").Type, label+"：首帧是 generate=false 的预热帧")
		require.False(t, gjson.GetBytes(prewarm, "previous_response_id").Exists(), label)
		require.Equal(t, []string{"base"}, codexGateInputRoles(prewarm, section.ContentItemKind),
			label+"：预热帧 input 只有前缀，普通 developer 消息留给业务帧")
		bases := codexGateBaseInstructionsItems(prewarm, section.ContentItemKind)
		require.Len(t, bases, 1, label)
		codexGateRequireBaseInstructionsMessage(t, bases[0], section, handshakes[0].header.Get("Thread-Id"), want, label)

		require.Equal(t, []string{"message:developer", "message:user"}, codexGateInputRoles(business, section.ContentItemKind), label)
		require.Empty(t, codexGateBaseInstructionsItems(business, section.ContentItemKind), label+"：业务帧不重复基础指令")
		require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[0].connection), gjson.GetBytes(business, "previous_response_id").String(), label)
		require.Equal(t, []string{"message:user"}, codexGateInputRoles(incremental, section.ContentItemKind), label)
		require.Empty(t, codexGateBaseInstructionsItems(incremental, section.ContentItemKind), label)
		require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_2", handshakes[0].connection), gjson.GetBytes(incremental, "previous_response_id").String(), label)
		for _, raw := range [][]byte{business, incremental} {
			require.Equal(t, codexGateDefaultAccessPrograms, gjson.GetBytes(raw, "access_programs").Raw, label)
		}
		require.NotContains(t, string(prewarm), "access_programs", label+"：预热帧不绑定回合程序")

		// ---- 对照：旧画像槽位的行为不变（没有载体消息，也不凭空补默认指令）----
		if !hasControl {
			continue
		}
		_, controlHandshakes := codexGateRunWSIngress(t, control, steps)
		controlLabel := tc.name + "/对照槽位 " + control
		require.Len(t, controlHandshakes, 1, controlLabel)
		controlFrames := codexGateResponseCreateFrames(controlHandshakes[0])
		require.Len(t, controlFrames, 3, controlLabel)
		for index, raw := range controlFrames {
			require.Empty(t, codexGateBaseInstructionsItems(raw, section.ContentItemKind), "%s 第 %d 帧：旧画像没有基础指令载体消息", controlLabel, index+1)
			require.NotContains(t, string(raw), "model.base_instructions", controlLabel)
			require.False(t, gjson.GetBytes(raw, "access_programs").Exists(), controlLabel)
		}
		require.NotEqual(t, want, gjson.GetBytes(controlFrames[0], "instructions").String(),
			controlLabel+"：旧画像槽位不凭空补默认指令")
		require.Equal(t, []string{"message:developer"}, codexGateInputRoles(controlFrames[0], section.ContentItemKind),
			controlLabel+"：旧画像的预热帧承载入站的 developer 上下文")
	}
}

// ---- 全量历史回退：断线重连后的新链首帧（原形态，没有顶层 instructions）----

// startOpenAIWSFullHistoryFallbackSessionOnMode 与 startOpenAIWSFullHistoryFallbackSession 相同，但发布槽位由调用方指定。
func startOpenAIWSFullHistoryFallbackSessionOnMode(
	t *testing.T,
	mode string,
	first []byte,
) (*codexGateWireServer, *codexGateWebSocketIngress) {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	gate.service.cfg.Gateway.OpenAIWS.ToolContinuationFullHistoryFallbackEnabled = true
	header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
	return server, gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(), first)
}

// 全量历史回退的原形态用例：首帧是断线重连后重发的完整历史，没有顶层 instructions、没有带标记的消息、也没有
// previous_response_id。回退发生在帧整理之后（previous_response_id 已不存在），所以新链首帧要按“新链没有来源”处理：
// 装入默认基础指令，预热帧才有可承载的前缀。
func TestOpenAIWSToolContinuationFullHistoryFallbackWithoutInstructionsOpensNewChainOnCarrierSlot(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	control, hasControl := codexGateControlReleaseMode(t, mode)

	original := openAIWSFullHistoryFallbackFrame(false)
	frame, err := sjson.DeleteBytes(original, "instructions")
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(frame, "instructions").Exists(), "前提：原形态首帧没有顶层 instructions")
	require.NotEqual(t, string(original), string(frame))
	want := wsDefaultInstructionsHTTPOracle(t, "gpt-5.5")

	server, ws := startOpenAIWSFullHistoryFallbackSessionOnMode(t, mode, frame)
	responseID := ws.readUntilCompleted(t)
	ws.close(t)

	handshakes := codexGateWebSocketHandshakes(server)
	require.Len(t, handshakes, 1, "重连后的完整历史由一条新连接承接")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 2, "新连接上先发预热帧，再发业务帧")
	prewarm, business := frames[0], frames[1]
	require.Equal(t, gjson.False, gjson.GetBytes(prewarm, "generate").Type, "首帧必须是 generate=false 的预热帧")
	require.False(t, gjson.GetBytes(prewarm, "previous_response_id").Exists())
	require.Equal(t, []string{"base"}, codexGateInputRoles(prewarm, section.ContentItemKind), "预热帧 input 只有默认基础指令消息")
	bases := codexGateBaseInstructionsItems(prewarm, section.ContentItemKind)
	require.Len(t, bases, 1)
	codexGateRequireBaseInstructionsMessage(t, bases[0], section, handshakes[0].header.Get("Thread-Id"), want, "全量历史回退")

	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[0].connection), gjson.GetBytes(business, "previous_response_id").String(),
		"业务帧只续接新连接自己的预热响应")
	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_2", handshakes[0].connection), responseID)
	require.Empty(t, codexGateBaseInstructionsItems(business, section.ContentItemKind), "业务帧不重复基础指令")
	callIDs := map[string][]string{}
	for _, item := range gjson.GetBytes(business, "input").Array() {
		if callID := item.Get("call_id").String(); callID != "" {
			callIDs[item.Get("type").String()] = append(callIDs[item.Get("type").String()], callID)
		}
	}
	require.Equal(t, []string{"call_fh_1", "call_fh_2"}, callIDs["function_call"], "完整历史原样转发")
	require.Equal(t, []string{"call_fh_1", "call_fh_2"}, callIDs["function_call_output"])
	for index, raw := range frames {
		require.False(t, gjson.GetBytes(raw, "instructions").Exists(), "第 %d 帧：顶层不得有 instructions", index+1)
	}

	if !hasControl {
		return
	}
	controlServer, controlWS := startOpenAIWSFullHistoryFallbackSessionOnMode(t, control, frame)
	controlWS.readUntilCompleted(t)
	controlWS.close(t)
	controlHandshakes := codexGateWebSocketHandshakes(controlServer)
	require.Len(t, controlHandshakes, 1)
	for index, raw := range codexGateResponseCreateFrames(controlHandshakes[0]) {
		require.Empty(t, codexGateBaseInstructionsItems(raw, section.ContentItemKind), "对照槽位第 %d 帧：旧画像没有基础指令载体消息", index+1)
	}
}

// ---- 固定到载体画像槽位（与 useOfficialCodexLegacyProfile 对称）----

// useOfficialCodexBaseInstructionsCarrierProfile 把整个用例固定在声明了 WS 基础指令载体的画像槽位：候选态在 previous，
// 晋升后在 active。槽位按画像结构事实定位，不写死槽位名；服务的发布指针配置与它使用的 Codex runtime 同时指向该槽位。
func useOfficialCodexBaseInstructionsCarrierProfile(t *testing.T, svc *OpenAIGatewayService) {
	t.Helper()
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	if svc.cfg == nil {
		svc.cfg = &config.Config{}
	}
	svc.cfg.Gateway.OfficialClientProfiles.Mode = mode
	require.NotNil(t, svc.httpUpstream, "固定画像 runtime 需要服务已配置 HTTPUpstream")
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, svc.httpUpstream, mode)
}
