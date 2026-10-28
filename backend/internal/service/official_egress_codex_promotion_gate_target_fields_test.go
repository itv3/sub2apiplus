package service

import (
	"fmt"
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// codexGateFunctionTool 是门禁入站里的一个普通函数工具，用来观察顶层 tools 在非 Lite 保留、在 Lite 移入 additional_tools。
var codexGateFunctionTool = map[string]any{
	"type": "function", "name": "lookup", "description": "d",
	"parameters": map[string]any{"type": "object"},
}

// codexGateTaggedBaseInstructionsMessage 是官方较新版本客户端的入站形态：input 里带内容标记的 developer 消息，没有顶层 instructions。
func codexGateTaggedBaseInstructionsMessage(text string) map[string]any {
	return map[string]any{
		"type": "message", "id": "msg_stale_inbound", "role": "developer",
		"content": []any{map[string]any{"type": "input_text", "text": text}},
		"internal_chat_message_metadata_passthrough": map[string]any{
			"content_item_kinds": []any{"model.base_instructions"},
		},
	}
}

// SPEC-BODY-001（change）：HTTP 与 WS Responses 使用各自封闭结构体字段。
//
// 检查项与网关观测（真实 wire 字节，生产 Forward／ProxyResponsesWebSocketFromClient 经正式 Compiler／Executor 发出）：
//   - http-closed-fields：HTTP Responses 的顶层字段是批准字段序的有序子集并含全部必需项——官方入站的 Lite 与非 Lite、
//     顶层 instructions 与带标记 developer 消息两种基础指令来源都覆盖；顶层没有 instructions，默认路径（入站没带
//     access_programs）上末位成员是 {"cyber":"standard"}；
//   - ws-closed-fields：WS response.create 每一帧（HTTP 入站派生的上游 WS、官方 WS 入站的预热帧、首轮、增量）的顶层字段
//     都是批准字段序（HTTP 序加 type、previous_response_id、generate）的有序子集并含全部必需项；非预热帧末位是
//     {"cyber":"standard"}，预热帧不带 access_programs；
//   - unknown-sentinel-absent：入站顶层注入的未知字段 candidate_unknown_sentinel 不出现在任何出站字节里。
//
// 画像数据层同时锁定：两个端点的闭集字段序与必需项恰为批准值，闭集不含 instructions、含 access_programs。
// 对照：另一槽位的闭集仍含顶层 instructions、没有 access_programs，同一入站在 wire 上保留顶层 instructions、不带 access_programs。
func TestCodexResponsesBodyClosedFieldsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	// 画像侧该结构事实自 0.163 起存在：候选 0.164 进入 previous 之后两个槽位都具备（继承特征），本门禁不再承担判别职责，
	// 改在较旧槽位（批准这些语义的 0.163 发布）上重放，作为既有批准语义的回归；0.164 对同一规则的新语义由本轮的
	// 判别式门禁（IDScheme／refresh_reason／图像头等，见 official_egress_codex_promotion_gate_0164_test.go）覆盖。
	mode := codexGateTargetReleaseMode(t, "Responses 的 HTTP 与 WS 闭集字段不含 instructions 且含 access_programs",
		codexGateResponsesClosedFieldsWithoutInstructions)
	profile := codexGateExecutableProfile(t, mode)
	httpEndpoint := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesHTTP)
	wsEndpoint := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS)
	require.Equal(t, codexGateApprovedHTTPFields, codexGateFieldNames(httpEndpoint), "responses_http 的冻结槽位必须与批准的字段序一致")
	require.Equal(t, codexGateApprovedWSFields, codexGateFieldNames(wsEndpoint), "responses_ws 的冻结槽位必须与批准的字段序一致")
	require.ElementsMatch(t, codexGateApprovedHTTPRequired, codexGateRequiredFieldNames(httpEndpoint))
	require.ElementsMatch(t, codexGateApprovedWSRequired, codexGateRequiredFieldNames(wsEndpoint))

	// ---- http-closed-fields 与 unknown-sentinel-absent ----
	forwardHTTP := func(mode string, model string, instructions string, tagged bool) codexGateWireRequest {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["model"] = model
			payload["tools"] = []any{codexGateFunctionTool}
			payload[codexGateUnknownSentinel] = codexGateUnknownSentinel
			if instructions != "" {
				payload["instructions"] = instructions
			}
			if tagged {
				input, _ := payload["input"].([]any)
				payload["input"] = append([]any{input[0], codexGateTaggedBaseInstructionsMessage(codexGateBaseInstructionsText)}, input[1:]...)
			}
		})
		if model == codexGateNonLiteModel {
			ingress.Request.Header.Del("x-openai-internal-codex-responses-lite")
		}
		return codexGateForwardResponsesWire(t, mode, server, ingress, body, codexGateUseRecorderManifest)
	}
	for _, tc := range []struct {
		name         string
		model        string
		instructions string
		tagged       bool
	}{
		{"lite/instructions", "gpt-5.6-luna", codexGateBaseInstructionsText, false},
		{"non-lite/instructions", codexGateNonLiteModel, codexGateBaseInstructionsText, false},
		{"non-lite/tagged", codexGateNonLiteModel, "", true},
		{"lite/no-source", "gpt-5.6-luna", "", false},
	} {
		request := forwardHTTP(mode, tc.model, tc.instructions, tc.tagged)
		order := codexGateJSONFieldOrder(t, request.body)
		codexGateRequireOrderedSubset(t, order, codexGateApprovedHTTPFields, codexGateApprovedHTTPRequired, "HTTP "+tc.name+" 的顶层字段")
		require.NotContains(t, order, "instructions", "HTTP %s：闭集没有 instructions，wire 上不得出现", tc.name)
		require.Equal(t, "access_programs", order[len(order)-1], "HTTP %s：默认路径上 access_programs 位于末位", tc.name)
		require.Equal(t, codexGateDefaultAccessPrograms, gjson.GetBytes(request.body, "access_programs").Raw, "HTTP %s", tc.name)
		require.NotContains(t, string(request.body), codexGateUnknownSentinel, "HTTP %s：未知顶层字段不得出现在出站字节里", tc.name)
	}

	// ---- ws-closed-fields 与 unknown-sentinel-absent ----
	requireWSFrames := func(frames [][]byte, where string) {
		t.Helper()
		require.NotEmpty(t, frames, where)
		for index, frame := range frames {
			label := fmt.Sprintf("%s 第 %d 帧", where, index+1)
			order := codexGateJSONFieldOrder(t, frame)
			codexGateRequireOrderedSubset(t, order, codexGateApprovedWSFields, codexGateApprovedWSRequired, label+" response.create 的顶层字段")
			require.NotContains(t, order, "instructions", "%s：闭集没有 instructions，wire 上不得出现", label)
			require.NotContains(t, string(frame), codexGateUnknownSentinel, "%s：未知顶层字段不得出现在出站字节里", label)
			if gjson.GetBytes(frame, "generate").Type == gjson.False {
				require.NotContains(t, order, "access_programs", "%s：预热帧不绑定回合程序", label)
				continue
			}
			require.Equal(t, "access_programs", order[len(order)-1], "%s：默认路径上 access_programs 位于末位", label)
			require.Equal(t, codexGateDefaultAccessPrograms, gjson.GetBytes(frame, "access_programs").Raw, label)
		}
	}
	derivedFrames := func(mode string) [][]byte {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		codexGateUseRecorderManifest(gate.service, gate.account)
		gate.enableWebSocket(t, server)
		handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody(codexGateNonLiteModel, map[string]any{
			"instructions": codexGateBaseInstructionsText, "tools": []any{codexGateFunctionTool},
			codexGateUnknownSentinel: codexGateUnknownSentinel,
		}), nil)
		require.NoError(t, err)
		return codexGateResponseCreateFrames(handshake)
	}
	ingressFrames := func(mode string) [][]byte {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		codexGateUseRecorderManifest(gate.service, gate.account)
		gate.enableWebSocket(t, server)
		header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
		first := codexGateWSIngressFrame(t, codexGateNonLiteModel, "", codexGateBaseInstructionsText,
			[]any{codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")},
			map[string]any{codexGateUnknownSentinel: codexGateUnknownSentinel})
		ws := gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(), first)
		turnOne := ws.readUntilCompleted(t)
		ws.send(t, codexGateWSIngressFrame(t, codexGateNonLiteModel, turnOne, codexGateBaseInstructionsText,
			[]any{codexGateMessage("user", "turn two")}, nil))
		ws.readUntilCompleted(t)
		ws.close(t)
		var all [][]byte
		for _, handshake := range codexGateWebSocketHandshakes(server) {
			all = append(all, codexGateResponseCreateFrames(handshake)...)
		}
		return all
	}
	requireWSFrames(derivedFrames(mode), "HTTP 入站派生的上游 WS")
	ingressTarget := ingressFrames(mode)
	require.Len(t, ingressTarget, 3, "官方 WS 入站：预热帧、首轮、增量各一帧")
	requireWSFrames(ingressTarget, "官方 WS 入站")

	// ---- 对照：另一槽位保留顶层 instructions、没有 access_programs ----
	control, ok := codexGateControlReleaseMode(t, mode)
	if !ok {
		// 继承特征：两个槽位都具备该结构事实，没有“不具备”的对照槽位；上面已在较旧槽位完成重放，对照段随之省略。
		return
	}
	controlProfile := codexGateExecutableProfile(t, control)
	controlHTTP := codexGateMustEndpoint(t, controlProfile, officialCodexEndpointResponsesHTTP)
	controlWS := codexGateMustEndpoint(t, controlProfile, officialCodexEndpointResponsesWS)
	controlRequest := forwardHTTP(control, codexGateNonLiteModel, codexGateBaseInstructionsText, false)
	controlOrder := codexGateJSONFieldOrder(t, controlRequest.body)
	codexGateRequireOrderedSubset(t, controlOrder, codexGateFieldNames(controlHTTP), codexGateRequiredFieldNames(controlHTTP),
		"对照槽位 HTTP 的顶层字段")
	require.Equal(t, codexGateBaseInstructionsText, gjson.GetBytes(controlRequest.body, "instructions").String(),
		"对照：另一槽位的非 Lite 请求保留顶层 instructions")
	require.NotContains(t, controlOrder, "access_programs", "对照：另一槽位不带 access_programs")
	for index, frame := range ingressFrames(control) {
		order := codexGateJSONFieldOrder(t, frame)
		codexGateRequireOrderedSubset(t, order, codexGateFieldNames(controlWS), codexGateRequiredFieldNames(controlWS),
			"对照槽位 WS 的顶层字段")
		require.Contains(t, order, "instructions", "对照：另一槽位的第 %d 帧保留顶层 instructions", index+1)
		require.NotContains(t, order, "access_programs", "对照：另一槽位的第 %d 帧不带 access_programs", index+1)
	}
}
