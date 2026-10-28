package service

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// 本轮目标画像的晋升后判别式门禁：公共夹具。
//
// 门禁映射为本轮每条受影响规则（SPEC-BODY-001、SPEC-BODY-006、SPEC-BODY-009、SPEC-WS-005）各绑一个用例，
// 一律用 codexGateDiscriminatingReleaseMode 按画像结构事实定位目标槽位——候选态目标装在 previous，晋升后在 active，
// 用例在两个阶段都成立，不写死槽位名。目标画像必须是两个槽位里唯一具备该事实的一个；另一个槽位作对照，
// 证明行为由画像驱动，回退旧画像不会带出新行为。
//
// 断言判据取自批准的候选规则期望（tools/official_client_capture/candidate_rule_expectations_*.json 中改写后的
// 五条判据：SPEC-BODY-001 的 http-closed-fields 与 ws-closed-fields、SPEC-WS-005 的 field-slots、SPEC-BODY-006 的
// nonlite-instructions-absent、SPEC-BODY-009 的 turn-metadata-keys），在此逐字复述，不由被测画像推导：
// 画像一旦被改坏，数据层断言与线上断言都会失败。
// ============================================================================

const (
	// codexGateUnknownSentinel 是判据 unknown-sentinel-absent 注入入站顶层的未知字段，不得出现在出站字节里。
	codexGateUnknownSentinel = "candidate_unknown_sentinel"
	// codexGateDefaultAccessPrograms 是默认路径（入站没带 access_programs）上出站末位成员的取值。
	codexGateDefaultAccessPrograms = `{"cyber":"standard"}`
	// codexGateBaseInstructionsText 是门禁用的基础指令正文：含换行、引号与非 ASCII，不含 < > & 与 U+2028／U+2029，
	// 使标准 JSON 编码与官方编码逐字节相同。
	codexGateBaseInstructionsText = "Gate base instructions.\n第二行：包含 \"引号\"。"
)

var (
	// codexGateApprovedHTTPFields 是批准的 HTTP Responses 顶层字段序（SPEC-BODY-001 http-closed-fields 的 allowed）。
	codexGateApprovedHTTPFields = []string{
		"model", "stream", "service_tier", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning",
		"store", "stream_options", "include", "prompt_cache_key", "text", "client_metadata", "access_programs",
	}
	// codexGateApprovedHTTPRequired 是 HTTP 顶层必需字段（http-closed-fields 的 required）。
	codexGateApprovedHTTPRequired = []string{
		"model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include",
	}
	// codexGateApprovedWSFields 是批准的 WS response.create 顶层字段序（SPEC-BODY-001 ws-closed-fields 与
	// SPEC-WS-005 field-slots 的 allowed）。
	codexGateApprovedWSFields = []string{
		"type", "model", "stream", "service_tier", "previous_response_id", "input", "tools", "tool_choice",
		"parallel_tool_calls", "reasoning", "store", "stream_options", "include", "prompt_cache_key", "text",
		"generate", "client_metadata", "access_programs",
	}
	// codexGateApprovedWSRequired 是 WS 顶层必需字段（ws-closed-fields 与 field-slots 的 required）。
	codexGateApprovedWSRequired = []string{
		"type", "model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include",
	}
)

// codexGateFieldNames 返回端点 Body.Fields 的字段名（按画像槽位序）。
func codexGateFieldNames(endpoint profilecontract.ExecutableEndpointProfile) []string {
	names := make([]string, 0, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		names = append(names, field.Name)
	}
	return names
}

// codexGateRequiredFieldNames 返回端点 Body.Fields 里标记为必需的字段名。
func codexGateRequiredFieldNames(endpoint profilecontract.ExecutableEndpointProfile) []string {
	var names []string
	for _, field := range endpoint.Body.Fields {
		if field.Required {
			names = append(names, field.Name)
		}
	}
	return names
}

// codexGateResponsesClosedFieldsWithoutInstructions 是 SPEC-BODY-001 与 SPEC-WS-005 的判别式结构事实：
// HTTP 与 WS 两个 Responses 端点的闭集字段都不含顶层 instructions、都含 access_programs。
func codexGateResponsesClosedFieldsWithoutInstructions(profile profilecontract.ExecutableProfile) bool {
	for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
		endpoint, ok := codexGateEndpoint(profile, endpointID)
		if !ok {
			return false
		}
		names := codexGateFieldNames(endpoint)
		if slices.Contains(names, "instructions") || !slices.Contains(names, "access_programs") {
			return false
		}
	}
	return true
}

// codexGateBaseInstructionsCarrierDeclared 是 SPEC-BODY-006 的判别式结构事实：画像声明了 input 里 developer 消息
// 形态的基础指令载体，且 HTTP 与 WS 两个 Responses 端点都在节的端点清单里。
func codexGateBaseInstructionsCarrierDeclared(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().BaseInstructions
	return section != nil && section.Carrier == "input_developer_message" &&
		slices.Contains(section.EndpointIDs, officialCodexEndpointResponsesHTTP) &&
		slices.Contains(section.EndpointIDs, officialCodexEndpointResponsesWS)
}

// codexGateBaseInstructionsID 复述官方的基础指令消息 id 规则：前缀 + "_" + UUIDv5(UUIDv5(NAMESPACE_OID, 线程 ID), 正文)。
// 同一线程、同一正文得到同一 id，线程变了 id 跟着变。独立于生产实现，用来对照真实 wire。
func codexGateBaseInstructionsID(prefix string, threadID string, text string) string {
	threadNamespace := uuid.NewSHA1(uuid.NameSpaceOID, []byte(threadID))
	return prefix + "_" + uuid.NewSHA1(threadNamespace, []byte(text)).String()
}

// codexGateBaseInstructionsItems 返回 body 的 input 里带指定内容标记的 developer 消息（按出现顺序）。
func codexGateBaseInstructionsItems(body []byte, kind string) []gjson.Result {
	var items []gjson.Result
	for _, item := range gjson.GetBytes(body, "input").Array() {
		for _, marker := range item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Array() {
			if marker.String() == kind {
				items = append(items, item)
				break
			}
		}
	}
	return items
}

// codexGateRequireBaseInstructionsMessage 逐项校验一条官方形态的基础指令消息：键序、角色、id 公式、正文与标记。
func codexGateRequireBaseInstructionsMessage(
	t *testing.T,
	item gjson.Result,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
	where string,
) {
	t.Helper()
	require.Equal(t,
		[]string{"type", "id", "role", "content", "internal_chat_message_metadata_passthrough"},
		codexGateJSONFieldOrder(t, []byte(item.Raw)), "%s：基础指令消息的键序必须是官方形态：%s", where, item.Raw)
	require.Equal(t, "message", item.Get("type").String(), where)
	require.Equal(t, "developer", item.Get("role").String(), where)
	require.NotEmpty(t, threadID, "%s：线程 ID 缺失无法核对消息 id", where)
	require.Equal(t, codexGateBaseInstructionsID(section.IDPrefix, threadID, text), item.Get("id").String(),
		"%s：id 必须是前缀 + UUIDv5(UUIDv5(NAMESPACE_OID, thread_id), 正文)", where)
	// 官方 serde_json 不做 HTML 转义，帧里的 `<`、`>`、`&` 是字面量，期望值也不能转义。
	encodedText, err := marshalNoHTMLEscape(text)
	require.NoError(t, err)
	require.Equal(t, `[{"type":"input_text","text":`+string(encodedText)+`}]`, item.Get("content").Raw,
		"%s：正文必须原样出现在 content 的 input_text 里", where)
	encodedKind, err := json.Marshal(section.ContentItemKind)
	require.NoError(t, err)
	require.Equal(t, `{"content_item_kinds":[`+string(encodedKind)+`]}`,
		item.Get("internal_chat_message_metadata_passthrough").Raw, "%s：passthrough 只带内容标记", where)
}

// codexGateInputRoles 把 input 项描述成 “type:role” 序列（additional_tools 记 tools，基础指令消息记 base），便于整体比对。
func codexGateInputRoles(body []byte, kind string) []string {
	var out []string
	for _, item := range gjson.GetBytes(body, "input").Array() {
		switch {
		case item.Get("type").String() == "additional_tools":
			out = append(out, "tools")
		case len(codexGateBaseInstructionsItems([]byte(`{"input":[`+item.Raw+`]}`), kind)) > 0:
			out = append(out, "base")
		default:
			out = append(out, item.Get("type").String()+":"+item.Get("role").String())
		}
	}
	return out
}

// codexGateMessage 构造一条入站 message 项。
func codexGateMessage(role string, text string) map[string]any {
	return map[string]any{
		"type": "message", "role": role,
		"content": []any{map[string]any{"type": "input_text", "text": text}},
	}
}

// codexGateWSIngressFrame 构造官方客户端 WS 模式的一帧 response.create。previousResponseID 非空时是增量帧；
// instructions 非空时按第三方客户端的写法带顶层 instructions；extra 里的成员原样并入。
func codexGateWSIngressFrame(
	t *testing.T,
	model string,
	previousResponseID string,
	instructions string,
	input []any,
	extra map[string]any,
) []byte {
	t.Helper()
	payload := map[string]any{
		"type": "response.create", "model": model, "stream": true, "store": false,
		"reasoning": map[string]any{"effort": "high"}, "input": input,
	}
	if previousResponseID != "" {
		payload["previous_response_id"] = previousResponseID
	}
	if instructions != "" {
		payload["instructions"] = instructions
	}
	for name, value := range extra {
		payload[name] = value
	}
	raw, err := json.Marshal(payload)
	require.NoError(t, err)
	return raw
}
