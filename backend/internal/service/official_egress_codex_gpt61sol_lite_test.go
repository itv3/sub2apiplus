package service

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 0.163 主线模型 gpt-6.1-sol 是 Lite 模型。第三方客户端从不请求 /backend-api/codex/models，入站是非 Lite 形态
// （没有 Lite 头、工具在顶层 tools、基础指令在顶层 instructions）；网关必须按模型能力把它整成 Lite 出站：
// 顶层 tools 进入 input 最前的 additional_tools，出站带 reasoning.context=all_turns 与 text.verbosity=low，
// 顶层没有 instructions，Lite 头为 true。冷启动（没有上游元数据快照，只能靠内置能力表）与有快照两种情形都要成立。
//
// 三种出站形态都要验：
//   - HTTP 出站：第三方 HTTP 入站带强制 HTTP 降级标记（官方客户端耗尽 WS 预算后的形态），出站直接走 HTTP；
//   - WS 出站、HTTP 入站：普通第三方 HTTP 入站网关默认先走上游 WS（扮演 Codex 客户端），这是它的默认形态；
//   - WS 出站、WS 入站：第三方 WS 客户端首帧是非 Lite 形态，网关派生出 Lite 的预热帧与业务帧。
//
// 公共约定：
//   - “冷启动”指账号没有模型清单快照，且 /models 拉取失败（终端对该路径返回 500），请求只能用 bundled；
//   - “有快照”指账号清单把 gpt-6.1-sol 标成 Lite（visibility=list，清单接管）；
//   - 目标槽位按画像结构事实定位（候选态在 previous，晋升后在 active），另一槽位（旧画像）作对照。

const gpt61SolInstructions = "gpt-6.1-sol lite instructions"

// gpt61SolSnapshotManifest 是账号清单快照：列出 gpt-6.1-sol 与已有 Lite 模型 gpt-5.6-luna（WS 线上形态的对照模型），
// 接管后其余模型都是“已知不支持 Lite”。
const gpt61SolSnapshotManifest = `{"models":[` +
	`{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,` +
	`"default_reasoning_level":"low","default_reasoning_summary":"none","supports_reasoning_summary_parameter":true},` +
	`{"slug":"gpt-5.6-luna","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,` +
	`"default_reasoning_level":"medium","default_reasoning_summary":"none","supports_reasoning_summary_parameter":true}]}`

// gpt61SolRespond 是终端的应答：/models 拉取失败（冷启动不得靠拉取得到能力），其余用默认应答。
func gpt61SolRespond(request codexGateWireRequest) codexGateWireResponse {
	if request.path == "/backend-api/codex/models" {
		return codexGateWireResponse{
			status: http.StatusInternalServerError,
			header: http.Header{"Content-Type": []string{"application/json"}},
			body:   []byte(`{"error":"models unavailable"}`),
		}
	}
	return codexGateDefaultResponse(request)
}

// gpt61SolConfigure 决定账号的模型清单快照：冷启动清掉测试服务工厂预置的快照；有快照时写入上面的清单。
func gpt61SolConfigure(snapshot bool) func(service *OpenAIGatewayService, account *Account) {
	return func(service *OpenAIGatewayService, account *Account) {
		service.openaiModelCapabilities.mu.Lock()
		delete(service.openaiModelCapabilities.accounts, account.ID)
		service.openaiModelCapabilities.mu.Unlock()
		if snapshot {
			service.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(gpt61SolSnapshotManifest))
		}
	}
}

func gpt61SolCaseLabel(slot string, snapshot bool) string {
	state := "冷启动"
	if snapshot {
		state = "有快照"
	}
	return fmt.Sprintf("槽位 %s／%s", slot, state)
}

// gpt61SolNonLiteBody 是第三方客户端的非 Lite 形态入站：顶层 tools、顶层 instructions，没有 additional_tools。
func gpt61SolNonLiteBody(model string) []byte {
	return []byte(`{"model":"` + model + `","stream":true,"store":false,"instructions":"` + gpt61SolInstructions + `",` +
		`"reasoning":{"effort":"high"},"parallel_tool_calls":true,"tool_choice":"auto",` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],` +
		`"input":[` +
		`{"type":"message","role":"developer","content":[{"type":"input_text","text":"developer context"}]},` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"turn one"}]}]}`)
}

// gpt61SolThirdPartyIngress 构造第三方客户端的 Responses HTTP 入站：非官方 UA，没有 Lite 头。网关对普通第三方 HTTP
// 默认先走上游 WS；forceHTTP 为 true 时带上强制 HTTP 降级标记，出站直接走 HTTP（WS 预算耗尽后的形态）。
func gpt61SolThirdPartyIngress(body []byte, forceHTTP bool) *gin.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", codexGateThirdPartyUserAgent)
	c.Set("api_key", &APIKey{ID: 42})
	if forceHTTP {
		c.Set(officialCodexForceHTTPFallbackKey, true)
	}
	return c
}

// requireGPT61SolLiteShape 校验出站正文（HTTP 请求体或 WS 的预热帧）是 Lite 形态：工具进了前导 additional_tools，
// 顶层没有 tools 与 instructions，基础指令正文在 input 里恰出现一次。carrier 为 true 时它是官方形态的基础指令消息，
// 否则（旧画像）是一条普通 developer 消息。
func requireGPT61SolLiteShape(t *testing.T, body []byte, model string, carrier bool, label string) {
	t.Helper()
	require.Equal(t, model, gjson.GetBytes(body, "model").String(), label)
	require.False(t, gjson.GetBytes(body, "tools").Exists(), "%s：Lite 顶层不发 tools：%s", label, body)
	require.False(t, gjson.GetBytes(body, "instructions").Exists(), "%s：顶层不得有 instructions：%s", label, body)
	first := gjson.GetBytes(body, "input.0")
	require.Equal(t, "additional_tools", first.Get("type").String(), "%s：input 最前是 additional_tools：%s", label, body)
	require.Equal(t, "read_file", first.Get(`tools.#(type=="function").name`).String(),
		"%s：入站顶层 tools 进入 additional_tools：%s", label, first.Raw)
	require.Equal(t, 1, bytes.Count(body, []byte(gpt61SolInstructions)), "%s：基础指令正文只出现一次", label)
	carrierMessages := 0
	for _, item := range gjson.GetBytes(body, "input").Array() {
		if item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() {
			carrierMessages++
		}
	}
	if carrier {
		require.Equal(t, 1, carrierMessages, "%s：载体画像用官方形态的基础指令消息承载：%s", label, body)
	} else {
		require.Zero(t, carrierMessages, "%s：旧画像没有基础指令载体消息：%s", label, body)
	}
}

// HTTP 出站：非 Lite 形态入站，已知 Lite 模型 gpt-6.1-sol。
func TestGPT61SolNonLiteInboundIsShapedAsLiteOnOAuthHTTPEgress(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	control, hasControl := codexGateControlReleaseMode(t, mode)
	slots := []string{mode}
	if hasControl {
		slots = append(slots, control)
	}
	t.Logf("目标槽位=%s 对照槽位=%s（有对照=%v）", mode, control, hasControl)
	for _, slot := range slots {
		for _, snapshot := range []bool{false, true} {
			label := "HTTP " + gpt61SolCaseLabel(slot, snapshot)
			server := startCodexGateWireServer(t, gpt61SolRespond)
			body := gpt61SolNonLiteBody("gpt-6.1-sol")
			wire := codexGateForwardResponsesWire(t, slot, server, gpt61SolThirdPartyIngress(body, true), body, gpt61SolConfigure(snapshot))

			requireGPT61SolLiteShape(t, wire.body, "gpt-6.1-sol", slot == mode, label)
			require.Equal(t, "all_turns", gjson.GetBytes(wire.body, "reasoning.context").String(), label+"：reasoning.context")
			require.Equal(t, "high", gjson.GetBytes(wire.body, "reasoning.effort").String(), label+"：客户端的 effort 原样保留")
			require.Equal(t, "low", gjson.GetBytes(wire.body, "text.verbosity").String(), label+"：text.verbosity")
			require.Equal(t, gjson.False, gjson.GetBytes(wire.body, "parallel_tool_calls").Type, label+"：Lite 固定 parallel_tool_calls=false")
			require.Equal(t, "true", wire.header.Get("x-openai-internal-codex-responses-lite"), label+"：Lite 头")
		}
	}
}

// requireGPT61SolLiteFields 校验出站正文里 Lite 画像固定的字段：reasoning.context、text.verbosity、parallel_tool_calls。
func requireGPT61SolLiteFields(t *testing.T, body []byte, label string) {
	t.Helper()
	require.Equal(t, "all_turns", gjson.GetBytes(body, "reasoning.context").String(), label+"：reasoning.context")
	require.Equal(t, "high", gjson.GetBytes(body, "reasoning.effort").String(), label+"：客户端的 effort 原样保留")
	require.Equal(t, "low", gjson.GetBytes(body, "text.verbosity").String(), label+"：text.verbosity")
	require.Equal(t, gjson.False, gjson.GetBytes(body, "parallel_tool_calls").Type, label+"：Lite 固定 parallel_tool_calls=false")
	require.False(t, gjson.GetBytes(body, "tools").Exists(), label+"：Lite 顶层不发 tools")
	require.False(t, gjson.GetBytes(body, "instructions").Exists(), label+"：顶层不得有 instructions")
}

// gpt61SolWSGate 构造 WS 上游的网关服务与终端：冷启动清掉预置快照，有快照时写入清单。
func gpt61SolWSGate(t *testing.T, slot string, snapshot bool) (*codexGateWireServer, *codexGateService) {
	t.Helper()
	server := startCodexGateWireServer(t, gpt61SolRespond)
	gate := newCodexGateService(t, slot, server)
	gpt61SolConfigure(snapshot)(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	return server, gate
}

// WS 上游、HTTP 入站：普通第三方 HTTP 请求网关默认先走上游 WS（扮演 Codex 客户端）。
func TestGPT61SolNonLiteInboundIsShapedAsLiteOnOAuthWSEgressFromHTTPIngress(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	control, hasControl := codexGateControlReleaseMode(t, mode)
	slots := []string{mode}
	if hasControl {
		slots = append(slots, control)
	}
	for _, slot := range slots {
		for _, snapshot := range []bool{false, true} {
			label := "WS 上游（HTTP 入站） " + gpt61SolCaseLabel(slot, snapshot)
			server, gate := gpt61SolWSGate(t, slot, snapshot)
			handshake, err := gate.forwardViaWebSocket(t, server, gpt61SolNonLiteBody("gpt-6.1-sol"), nil)
			require.NoError(t, err, label)
			require.Empty(t, server.httpRequestsForPath(codexGateResponsesPath), label+"：不得回落 HTTP")
			frames := codexGateResponseCreateFrames(handshake)
			require.NotEmpty(t, frames, label)
			for index, raw := range frames {
				frameLabel := fmt.Sprintf("%s 第 %d 帧", label, index+1)
				requireGPT61SolLiteShape(t, raw, "gpt-6.1-sol", slot == mode, frameLabel)
				requireGPT61SolLiteFields(t, raw, frameLabel)
			}

			// WS 上游的握手与帧不带单独的 Lite 头或标记（画像里 Lite 在 WS 上只体现为正文结构）；与已有 Lite 模型
			// gpt-5.6-luna 同一条件下的握手头顺序、首帧顶层字段序必须逐项相同，说明 gpt-6.1-sol 没有自己的特殊形态。
			lunaServer, lunaGate := gpt61SolWSGate(t, slot, snapshot)
			lunaHandshake, lunaErr := lunaGate.forwardViaWebSocket(t, lunaServer, gpt61SolNonLiteBody("gpt-5.6-luna"), nil)
			require.NoError(t, lunaErr, label+"（对照 gpt-5.6-luna）")
			lunaFrames := codexGateResponseCreateFrames(lunaHandshake)
			require.NotEmpty(t, lunaFrames, label)
			require.Equal(t, lunaHandshake.headerNames, handshake.headerNames, label+"：握手头与 gpt-5.6-luna 同形")
			require.Equal(t, codexGateJSONFieldOrder(t, lunaFrames[0]), codexGateJSONFieldOrder(t, frames[0]),
				label+"：首帧顶层字段序与 gpt-5.6-luna 同形")
			require.Equal(t, gjson.GetBytes(lunaFrames[0], "client_metadata.@keys").Raw, gjson.GetBytes(frames[0], "client_metadata.@keys").Raw,
				label+"：首帧 client_metadata 的键与 gpt-5.6-luna 同形")
		}
	}
}

// WS 上游、WS 入站：第三方 WS 客户端首帧是非 Lite 形态（顶层 tools 与 instructions），网关派生出 Lite 的预热帧与业务帧。
func TestGPT61SolNonLiteInboundIsShapedAsLiteOnOAuthWSEgressFromWSIngress(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	control, hasControl := codexGateControlReleaseMode(t, mode)
	slots := []string{mode}
	if hasControl {
		slots = append(slots, control)
	}
	tools := map[string]any{"tools": []any{map[string]any{
		"type": "function", "name": "read_file", "parameters": map[string]any{"type": "object"},
	}}}
	for _, slot := range slots {
		for _, snapshot := range []bool{false, true} {
			label := "WS 上游（WS 入站） " + gpt61SolCaseLabel(slot, snapshot)
			server, gate := gpt61SolWSGate(t, slot, snapshot)
			header := http.Header{"User-Agent": []string{codexGateThirdPartyUserAgent}}
			first := codexGateWSIngressFrame(t, "gpt-6.1-sol", "", gpt61SolInstructions,
				[]any{codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")}, tools)
			ws := gate.startWebSocketIngress(t, header, codexGateIngressAPIKey(), first)
			last := ws.readUntilCompleted(t)
			ws.send(t, codexGateWSIngressFrame(t, "gpt-6.1-sol", last, "", []any{codexGateMessage("user", "turn two")}, nil))
			ws.readUntilCompleted(t)
			ws.close(t)

			handshakes := codexGateWebSocketHandshakes(server)
			require.Len(t, handshakes, 1, label)
			frames := codexGateResponseCreateFrames(handshakes[0])
			require.Len(t, frames, 3, label+"：预热、首轮业务帧、增量帧")
			prewarm := frames[0]
			require.Equal(t, gjson.False, gjson.GetBytes(prewarm, "generate").Type, label+"：首帧是 generate=false 的预热帧")
			requireGPT61SolLiteShape(t, prewarm, "gpt-6.1-sol", slot == mode, label+" 预热帧")
			require.False(t, gjson.GetBytes(prewarm, "tools").Exists(), label)
			for index, raw := range frames {
				frameLabel := fmt.Sprintf("%s 第 %d 帧", label, index+1)
				require.Equal(t, "gpt-6.1-sol", gjson.GetBytes(raw, "model").String(), frameLabel)
				require.False(t, gjson.GetBytes(raw, "tools").Exists(), frameLabel+"：Lite 顶层不发 tools")
				require.False(t, gjson.GetBytes(raw, "instructions").Exists(), frameLabel+"：顶层不得有 instructions")
				require.Equal(t, "all_turns", gjson.GetBytes(raw, "reasoning.context").String(), frameLabel+"：reasoning.context")
				require.Equal(t, "low", gjson.GetBytes(raw, "text.verbosity").String(), frameLabel+"：text.verbosity")
			}
			// 前缀（additional_tools 与基础指令）只在预热帧登记，业务帧续接预热响应、不重复前缀。
			require.False(t, gjson.GetBytes(frames[1], `input.#(type=="additional_tools")`).Exists(), label+"：业务帧不重复 additional_tools")
			require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[0].connection),
				gjson.GetBytes(frames[1], "previous_response_id").String(), label+"：业务帧续接预热响应")
			require.Equal(t, 1, bytes.Count(frames[1], []byte("turn one")), label+"：首轮用户消息在业务帧")
		}
	}
}

// 入站没有 reasoning 时，出站的默认 effort 跟能力表的默认值走：gpt-6.1-sol 取官方内置目录值 low（冷启动取内置能力表，
// 有快照取账号清单里的值，这里清单与内置目录一致）。真实账号清单的取值要等 VC-1 目录证据复核；复核若不是 low，
// 应同步修改内置能力表与 fixture，而不是放宽这里的断言。默认摘要是 none，出站不带 summary。
func TestGPT61SolDefaultReasoningEffortFollowsTheCapabilityDefault(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "基础指令走 input 载体且列出 responses_ws", codexGateBaseInstructionsCarrierDeclared)
	control, hasControl := codexGateControlReleaseMode(t, mode)
	slots := []string{mode}
	if hasControl {
		slots = append(slots, control)
	}
	for _, slot := range slots {
		for _, snapshot := range []bool{false, true} {
			label := "HTTP " + gpt61SolCaseLabel(slot, snapshot)
			server := startCodexGateWireServer(t, gpt61SolRespond)
			body, err := sjson.DeleteBytes(gpt61SolNonLiteBody("gpt-6.1-sol"), "reasoning")
			require.NoError(t, err)
			wire := codexGateForwardResponsesWire(t, slot, server, gpt61SolThirdPartyIngress(body, true), body, gpt61SolConfigure(snapshot))

			require.Equal(t, "low", gjson.GetBytes(wire.body, "reasoning.effort").String(), label+"：默认 effort 取能力表默认值")
			require.Equal(t, "all_turns", gjson.GetBytes(wire.body, "reasoning.context").String(), label)
			require.False(t, gjson.GetBytes(wire.body, "reasoning.summary").Exists(), label+"：默认摘要是 none，出站不带 summary")
			require.Equal(t, "low", gjson.GetBytes(wire.body, "text.verbosity").String(), label)
		}
	}
}
