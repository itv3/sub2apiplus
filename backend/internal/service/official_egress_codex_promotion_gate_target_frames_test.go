package service

import (
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// codexGateRunWSIngress 按给定客户端帧序列驱动一次官方 WS 入站会话。steps 里每一项由上一轮完成事件的响应 ID 构造本帧；
// 返回终端上的全部握手。
func codexGateRunWSIngress(
	t *testing.T,
	mode string,
	steps []func(previousResponseID string) []byte,
) (*codexGateWireServer, []codexGateWireRequest) {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
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
	return server, codexGateWebSocketHandshakes(server)
}

// SPEC-WS-005（condition_change）：response.create 字段遵循固定槽位和条件省略；本轮改写——字段槽位不再有顶层 instructions，
// 新增条件字段 access_programs。
//
// 检查项与网关观测（官方 WS 入站会话，真实 wire 帧）：
//   - field-slots：连接上每一帧 response.create（预热、首轮、增量、全量重放）的顶层字段都是批准的 WS 字段序的有序子集并含全部
//     必需项，顶层没有 instructions；
//   - 预热帧：generate=false，不带 previous_response_id，input 只有前缀（前导 additional_tools 与官方形态的基础指令消息），
//     没有其他 developer／user 消息，不带 access_programs（官方预热帧不绑定回合程序）；
//   - 增量与业务帧：input 里没有前缀（additional_tools、基础指令消息都只在预热帧登记），不带 generate=false，
//     previous_response_id 续接同一连接上紧邻的上一个响应，不重复基础指令（第三方每帧重发的 instructions 被丢弃）；
//   - 凡非预热帧都带 access_programs，位于末位，默认路径取值 {"cyber":"standard"}；
//   - 一条连接上基础指令消息恰好出现一次，id 由握手的 thread-id 与正文按官方规则算出。
//
// 三种入站：第三方风格（每帧顶层 instructions，含全量重放换新连接）、官方较新版本风格（带标记 developer 消息、增量帧不再带）、
// Lite 模型（前导 additional_tools）。对照：另一槽位每帧保留顶层 instructions、预热帧带 developer 上下文、没有载体消息，
// 任何一帧都不带 access_programs。
func TestCodexWebSocketFrameSlotsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "responses_ws 的闭集字段不含 instructions 且含 access_programs，基础指令走 input 载体",
		func(profile profilecontract.ExecutableProfile) bool {
			return codexGateResponsesClosedFieldsWithoutInstructions(profile) && codexGateBaseInstructionsCarrierDeclared(profile)
		})
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	text := codexGateBaseInstructionsText
	dev := []any{codexGateMessage("developer", "developer context")}
	frame := func(model string, previous string, instructions string, input ...any) []byte {
		return codexGateWSIngressFrame(t, model, previous, instructions, input, nil)
	}

	// 第三方风格：每帧都带顶层 instructions；第三帧是不带 previous_response_id 的全量重放，由新连接承接。
	thirdParty := []func(string) []byte{
		func(string) []byte {
			return frame(codexGateNonLiteModel, "", text, dev[0], codexGateMessage("user", "turn one"))
		},
		func(previous string) []byte {
			return frame(codexGateNonLiteModel, previous, text, codexGateMessage("user", "turn two"))
		},
		func(string) []byte {
			return frame(codexGateNonLiteModel, "", text, dev[0], codexGateMessage("user", "turn one"),
				codexGateMessage("user", "turn two"), codexGateMessage("user", "turn three"))
		},
	}
	// 官方较新版本风格：首帧带带标记的 developer 消息、没有顶层 instructions，增量帧不再带前缀。
	official := []func(string) []byte{
		func(string) []byte {
			return frame(codexGateNonLiteModel, "", "", codexGateTaggedBaseInstructionsMessage(text), dev[0],
				codexGateMessage("user", "turn one"))
		},
		func(previous string) []byte {
			return frame(codexGateNonLiteModel, previous, "", codexGateMessage("user", "turn two"))
		},
	}
	// Lite 模型：入站带函数工具，Lite 归一化把它放进前导 additional_tools，与基础指令一起进入预热帧。
	liteTools := map[string]any{"tools": []any{codexGateFunctionTool}}
	lite := []func(string) []byte{
		func(string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", "", text,
				[]any{dev[0], codexGateMessage("user", "turn one")}, liteTools)
		},
		func(previous string) []byte {
			// 增量帧不再重发工具声明：官方客户端的前缀只在链的第一帧出现。
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", previous, text,
				[]any{codexGateMessage("user", "turn two")}, nil)
		},
	}

	// requireConnection 校验一条连接上的全部帧。wantPrewarm 是预热帧 input 的预期形状。
	requireConnection := func(handshake codexGateWireRequest, wantPrewarm []string, where string) {
		t.Helper()
		frames := codexGateResponseCreateFrames(handshake)
		require.GreaterOrEqual(t, len(frames), 2, "%s：每条连接至少有预热帧与一个业务帧", where)
		threadID := handshake.header.Get("Thread-Id")
		totalBase := 0
		for index, raw := range frames {
			label := fmt.Sprintf("%s 连接 %d 第 %d 帧", where, handshake.connection, index+1)
			order := codexGateJSONFieldOrder(t, raw)
			codexGateRequireOrderedSubset(t, order, codexGateApprovedWSFields, codexGateApprovedWSRequired, label+" 的顶层字段")
			require.NotContains(t, order, "instructions", "%s：顶层不得有 instructions", label)
			bases := codexGateBaseInstructionsItems(raw, section.ContentItemKind)
			totalBase += len(bases)
			if index == 0 {
				require.Equal(t, gjson.False, gjson.GetBytes(raw, "generate").Type, "%s：首帧必须是 generate=false 的预热帧", label)
				require.False(t, gjson.GetBytes(raw, "previous_response_id").Exists(), "%s：预热帧不携带 previous_response_id", label)
				require.NotContains(t, order, "access_programs", "%s：预热帧不绑定回合程序", label)
				require.Equal(t, wantPrewarm, codexGateInputRoles(raw, section.ContentItemKind),
					"%s：预热帧 input 只取前缀（additional_tools 与基础指令消息）：%s", label, raw)
				require.Len(t, bases, 1, label)
				codexGateRequireBaseInstructionsMessage(t, bases[0], section, threadID, text, label)
				continue
			}
			require.NotEqual(t, gjson.False, gjson.GetBytes(raw, "generate").Type, "%s：业务帧不得带 generate=false", label)
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_%d", handshake.connection, index),
				gjson.GetBytes(raw, "previous_response_id").String(), "%s：只续接同一连接上紧邻的上一个响应", label)
			require.Empty(t, bases, "%s：基础指令消息只在预热帧登记，业务帧不得重复", label)
			for _, role := range codexGateInputRoles(raw, section.ContentItemKind) {
				require.NotEqual(t, "tools", role, "%s：additional_tools 只在预热帧登记", label)
			}
			require.Equal(t, "access_programs", order[len(order)-1], "%s：非预热帧都带 access_programs 且位于末位", label)
			require.Equal(t, codexGateDefaultAccessPrograms, gjson.GetBytes(raw, "access_programs").Raw, label)
		}
		require.Equal(t, 1, totalBase, "%s：一条连接上基础指令消息恰好出现一次", where)
	}

	server, handshakes := codexGateRunWSIngress(t, mode, thirdParty)
	require.Len(t, handshakes, 2, "第三方风格：全量重放不复用旧连接，由新连接承接")
	requireConnection(handshakes[0], []string{"base"}, "第三方风格")
	requireConnection(handshakes[1], []string{"base"}, "第三方风格重放")
	firstConnection := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, firstConnection, 3, "首条连接：预热、首轮、增量")
	require.Equal(t, []string{"message:developer", "message:user"}, codexGateInputRoles(firstConnection[1], section.ContentItemKind),
		"首轮业务帧只带入站的业务消息")
	require.Equal(t, []string{"message:user"}, codexGateInputRoles(firstConnection[2], section.ContentItemKind), "增量帧只带新一轮的用户消息")
	require.Empty(t, server.httpRequestsForPath(codexGateResponsesPath), "官方 WS 入站不得回落 HTTP")

	_, handshakes = codexGateRunWSIngress(t, mode, official)
	require.Len(t, handshakes, 1)
	requireConnection(handshakes[0], []string{"base"}, "官方风格")
	preserved := codexGateBaseInstructionsItems(codexGateResponseCreateFrames(handshakes[0])[0], section.ContentItemKind)
	require.NotEqual(t, "msg_stale_inbound", preserved[0].Get("id").String(), "入站的旧 id 必须按网关线程重算")

	_, handshakes = codexGateRunWSIngress(t, mode, lite)
	require.Len(t, handshakes, 1)
	requireConnection(handshakes[0], []string{"tools", "base"}, "Lite")

	// ---- 对照：另一槽位每帧保留顶层 instructions，没有载体消息，也没有 access_programs ----
	control, ok := codexGateControlReleaseMode(t, mode)
	require.True(t, ok, "判别式门禁必有一个不具备该结构事实的对照槽位")
	_, controlHandshakes := codexGateRunWSIngress(t, control, thirdParty)
	require.NotEmpty(t, controlHandshakes)
	for _, handshake := range controlHandshakes {
		for index, raw := range codexGateResponseCreateFrames(handshake) {
			label := fmt.Sprintf("对照连接 %d 第 %d 帧", handshake.connection, index+1)
			require.Equal(t, text, gjson.GetBytes(raw, "instructions").String(), "%s：另一槽位保留顶层 instructions", label)
			require.Empty(t, codexGateBaseInstructionsItems(raw, section.ContentItemKind), "%s：另一槽位没有基础指令载体消息", label)
			require.False(t, gjson.GetBytes(raw, "access_programs").Exists(), "%s：另一槽位不带 access_programs", label)
			require.False(t, strings.Contains(string(raw), "model.base_instructions"), label)
		}
	}
}
