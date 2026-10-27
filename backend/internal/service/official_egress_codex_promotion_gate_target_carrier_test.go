package service

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// SPEC-BODY-006（change）：HTTP Responses 的 Lite／非 Lite 字段与 Option 省略规则；本轮改写——非 Lite 的请求体顶层不再有
// instructions，基础指令改由 input 最前（additional_tools 之后）的 developer 消息承载，Lite 与非 Lite 同形。
//
// 检查项与网关观测（真实 wire 字节）：
//   - nonlite-instructions-absent 与 lite-top-level-omission：Lite、非 Lite 的 Responses 请求体顶层都没有 instructions；
//   - 基础指令消息的官方形态：input 里恰有一条带内容标记的 developer 消息，键序为 type、id、role、content、
//     internal_chat_message_metadata_passthrough，id 是 msg_ + UUIDv5(UUIDv5(NAMESPACE_OID, thread_id), 正文)，
//     位置紧跟前导 additional_tools（没有 additional_tools 时在最前），其后的入站项原样保留，正文只出现一次（入站里与
//     instructions 同文本的 developer 副本被去掉）；
//     入站是官方形态的带标记消息时，按网关线程重算 id；
//   - nonlite-tools-present 与 lite-tools-omission：非 Lite 顶层保留 tools，Lite 顶层不发 tools（移入 additional_tools）；
//   - numeric-effort-http-integer 与 numeric-effort-ws-integer：可按 u64 解析的自定义 reasoning.effort 在 HTTP 与上游 WS 的
//     每一帧上都是 JSON 整数 3；
//   - WS：HTTP 入站派生的上游 WS 帧同样以官方形态承载基础指令，没有顶层 instructions。
//
// 对照：另一槽位的非 Lite 请求保留顶层 instructions、没有载体消息；Lite 把指令以不带 id 与标记的普通 developer 消息放进 input。
func TestCodexBaseInstructionsCarrierReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	// 画像侧该结构事实自 0.163 起存在：候选 0.164 进入 previous 之后两个槽位都具备（继承特征），本门禁不再承担判别职责，
	// 改在较旧槽位（批准这些语义的 0.163 发布）上重放，作为既有批准语义的回归；0.164 对同一规则的新语义由本轮的
	// 判别式门禁（IDScheme／refresh_reason／图像头等，见 official_egress_codex_promotion_gate_0164_test.go）覆盖。
	mode := codexGateTargetReleaseMode(t, "BaseInstructions 节声明 input 里 developer 消息形态的基础指令载体（HTTP 与 WS）",
		codexGateBaseInstructionsCarrierDeclared)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	require.Equal(t, "input_developer_message", section.Carrier)
	require.Equal(t, "model.base_instructions", section.ContentItemKind)
	require.Equal(t, "msg", section.IDPrefix)
	require.Equal(t, []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS}, section.EndpointIDs)

	encodedText, err := json.Marshal(codexGateBaseInstructionsText)
	require.NoError(t, err)

	type httpCase struct {
		name            string
		model           string
		tagged          bool // 入站是官方形态的带标记消息，没有顶层 instructions
		noAdditional    bool // 入站 input 没有前导 additional_tools
		devCopy         bool // 入站 input 里留着与 instructions 同文本的 developer 副本（兼容层提升 system 消息的遗留形态）
		wantInputShapes []string
	}
	cases := []httpCase{
		{"non-lite/instructions", codexGateNonLiteModel, false, false, false, []string{"tools", "base", "message:developer", "message:user"}},
		{"lite/instructions", "gpt-5.6-luna", false, false, false, []string{"tools", "base", "message:developer", "message:user"}},
		{"non-lite/no-additional-tools", codexGateNonLiteModel, false, true, false, []string{"base", "message:developer", "message:user"}},
		{"non-lite/tagged", codexGateNonLiteModel, true, false, false, []string{"tools", "base", "message:developer", "message:user"}},
		{"lite/tagged", "gpt-5.6-luna", true, false, false, []string{"tools", "base", "message:developer", "message:user"}},
		{"non-lite/developer-copy", codexGateNonLiteModel, false, false, true, []string{"tools", "base", "message:developer", "message:user"}},
	}
	forwardHTTP := func(mode string, tc httpCase) codexGateWireRequest {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["model"] = tc.model
			payload["tools"] = []any{codexGateFunctionTool}
			input, _ := payload["input"].([]any)
			if tc.noAdditional {
				input = input[1:]
			}
			if tc.tagged {
				tagged := codexGateTaggedBaseInstructionsMessage(codexGateBaseInstructionsText)
				if tc.noAdditional {
					input = append([]any{tagged}, input...)
				} else {
					input = append([]any{input[0], tagged}, input[1:]...)
				}
			} else {
				payload["instructions"] = codexGateBaseInstructionsText
			}
			if tc.devCopy {
				// 同文本的 developer 副本紧跟前导 additional_tools：官方形态里这段文本只出现一次，副本必须去掉。
				copyMessage := codexGateMessage("developer", codexGateBaseInstructionsText)
				if tc.noAdditional {
					input = append([]any{copyMessage}, input...)
				} else {
					input = append([]any{input[0], copyMessage}, input[1:]...)
				}
			}
			payload["input"] = input
		})
		if tc.model == codexGateNonLiteModel {
			ingress.Request.Header.Del("x-openai-internal-codex-responses-lite")
		}
		return codexGateForwardResponsesWire(t, mode, server, ingress, body, codexGateUseRecorderManifest)
	}
	for _, tc := range cases {
		request := forwardHTTP(mode, tc)
		body := request.body
		lite := tc.model != codexGateNonLiteModel
		require.False(t, gjson.GetBytes(body, "instructions").Exists(), "%s：顶层不得有 instructions", tc.name)
		items := codexGateBaseInstructionsItems(body, section.ContentItemKind)
		require.Len(t, items, 1, "%s：input 里恰有一条基础指令消息：%s", tc.name, body)
		codexGateRequireBaseInstructionsMessage(t, items[0], section, request.header.Get("Thread-Id"),
			codexGateBaseInstructionsText, tc.name)
		require.Equal(t, tc.wantInputShapes, codexGateInputRoles(body, section.ContentItemKind),
			"%s：基础指令消息紧跟前导 additional_tools，其后入站项原样保留", tc.name)
		require.Equal(t, 1, strings.Count(string(body), string(encodedText)), "%s：正文只出现一次（旧副本已去掉）", tc.name)
		if lite {
			require.False(t, gjson.GetBytes(body, "tools").Exists(), "%s：Lite 顶层不发 tools", tc.name)
			require.Equal(t, "all_turns", gjson.GetBytes(body, "reasoning.context").String(), tc.name)
			require.Equal(t, "low", gjson.GetBytes(body, "text.verbosity").String(), tc.name)
		} else {
			require.Equal(t, "lookup", gjson.GetBytes(body, "tools.0.name").String(), "%s：非 Lite 顶层保留 tools", tc.name)
			require.False(t, gjson.GetBytes(body, "reasoning.context").Exists(), tc.name)
		}
	}

	// ---- numeric-effort-*：自定义数字档位在 HTTP 与上游 WS 的每一帧上都是 JSON 整数 ----
	for _, effort := range []any{3, "3"} {
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["reasoning"] = map[string]any{"effort": effort, "summary": "auto"}
		})
		wire := gjson.GetBytes(codexGateForwardResponsesWire(t, mode, server, ingress, body, nil).body, "reasoning.effort")
		require.Equal(t, gjson.Number, wire.Type, "入站 effort=%#v 时 HTTP wire 必须是 JSON 整数：%s", effort, wire.Raw)
		require.Equal(t, "3", wire.Raw)
	}
	wsServer := startCodexGateWireServer(t, nil)
	wsGate := newCodexGateService(t, mode, wsServer)
	codexGateUseRecorderManifest(wsGate.service, wsGate.account)
	wsGate.enableWebSocket(t, wsServer)
	handshake, err := wsGate.forwardViaWebSocket(t, wsServer, codexGateWebSocketBody(codexGateNonLiteModel, map[string]any{
		"instructions": codexGateBaseInstructionsText, "reasoning": map[string]any{"effort": 3},
	}), nil)
	require.NoError(t, err)
	frames := codexGateResponseCreateFrames(handshake)
	require.NotEmpty(t, frames)
	for index, frame := range frames {
		require.Equal(t, gjson.Number, gjson.GetBytes(frame, "reasoning.effort").Type,
			"第 %d 帧 response.create 的 reasoning.effort 必须是 JSON 整数", index+1)
		require.False(t, gjson.GetBytes(frame, "instructions").Exists(), "第 %d 帧：WS 顶层不得有 instructions", index+1)
		wsItems := codexGateBaseInstructionsItems(frame, section.ContentItemKind)
		require.Len(t, wsItems, 1, "第 %d 帧：基础指令以官方形态承载：%s", index+1, frame)
		codexGateRequireBaseInstructionsMessage(t, wsItems[0], section, handshake.header.Get("Thread-Id"),
			codexGateBaseInstructionsText, "WS 派生帧")
		require.Equal(t, "base", codexGateInputRoles(frame, section.ContentItemKind)[0], "没有前导 additional_tools 时基础指令消息在最前")
	}

	// ---- 对照：另一槽位的最终形态不变 ----
	control, ok := codexGateControlReleaseMode(t, mode)
	if !ok {
		// 继承特征：两个槽位都具备该结构事实，没有“不具备”的对照槽位；上面已在较旧槽位完成重放，对照段随之省略。
		return
	}
	nonLite := forwardHTTP(control, cases[0])
	require.Equal(t, codexGateBaseInstructionsText, gjson.GetBytes(nonLite.body, "instructions").String(),
		"对照：另一槽位的非 Lite 请求保留顶层 instructions")
	require.Empty(t, codexGateBaseInstructionsItems(nonLite.body, section.ContentItemKind), "对照：另一槽位没有基础指令载体消息")
	lite := forwardHTTP(control, cases[1])
	require.False(t, gjson.GetBytes(lite.body, "instructions").Exists(), "对照：另一槽位的 Lite 请求顶层没有 instructions")
	require.Empty(t, codexGateBaseInstructionsItems(lite.body, section.ContentItemKind), "对照：另一槽位的 Lite 没有基础指令载体消息")
	var plainCopies int
	for _, item := range gjson.GetBytes(lite.body, "input").Array() {
		if item.Get("role").String() == "developer" && strings.Contains(item.Raw, string(encodedText)) {
			plainCopies++
			require.False(t, item.Get("id").Exists(), "对照：另一槽位的 Lite 指令消息不带 id")
			require.False(t, item.Get("internal_chat_message_metadata_passthrough").Exists(), "对照：另一槽位的 Lite 指令消息不带 passthrough 标记")
		}
	}
	require.Equal(t, 1, plainCopies, "对照：另一槽位的 Lite 把指令放成一条普通 developer 消息")
}
