package service

import (
	"fmt"
	"reflect"
	"strings"

	"github.com/tidwall/gjson"
)

var openAIResponsesLiteHostedToolTypes = map[string]struct{}{
	"image_generation":     {},
	"web_search":           {},
	"web_search_preview":   {},
	"x_search":             {},
	"file_search":          {},
	"code_interpreter":     {},
	"computer_use_preview": {},
}

// Responses 的 hosted 工具在 input 历史中使用独立的 call 类型。这里必须
// 使用显式集合，不能用“所有 *_call”这种宽匹配：Codex 的 custom_tool_call、
// tool_search_call、local_shell_call 和 mcp_tool_call 都是 Lite 可以承载的
// 客户端工具续接，误判会把正常请求切到另一条能力链路并破坏续接画像。
var openAIResponsesLiteHostedToolCallTypes = map[string]struct{}{
	"image_generation_call": {},
	"web_search_call":       {},
	"x_search_call":         {},
	"file_search_call":      {},
	"code_interpreter_call": {},
	"computer_call":         {},
	"computer_call_output":  {},
	"mcp_call":              {},
	"mcp_approval_request":  {},
	"mcp_approval_response": {},
	"mcp_list_tools":        {},
	"mcp_list_tools_output": {},
}

func isOpenAIResponsesLiteHostedToolType(toolType string) bool {
	_, hosted := openAIResponsesLiteHostedToolTypes[strings.TrimSpace(toolType)]
	return hosted
}

func isOpenAIResponsesLiteHostedToolCallType(itemType string) bool {
	_, hosted := openAIResponsesLiteHostedToolCallTypes[strings.TrimSpace(itemType)]
	return hosted
}

// openAIResponsesLiteRequiresFullResponses 判断请求是否声明了 Lite 无法承载的
// hosted tool。仅按顶层 tool type 和明确的 tool_choice/历史调用项判断；名为
// web_search 的普通 function 仍是客户端函数，不应被误当成 hosted tool。
func openAIResponsesLiteRequiresFullResponses(body []byte) bool {
	if len(body) == 0 || !gjson.ValidBytes(body) {
		return false
	}
	var containsHostedType func(gjson.Result) bool
	containsHostedType = func(value gjson.Result) bool {
		if !value.Exists() || !value.IsObject() {
			return false
		}
		if isOpenAIResponsesLiteHostedToolType(value.Get("type").String()) {
			return true
		}
		// namespace/additional_tools 可能携带嵌套工具定义；只沿 tools
		// 字段递归，避免把函数参数 schema 中同名字符串误判为 hosted。
		nestedTools := value.Get("tools")
		if !nestedTools.IsArray() {
			return false
		}
		for _, nested := range nestedTools.Array() {
			if containsHostedType(nested) {
				return true
			}
		}
		return false
	}
	tools := gjson.GetBytes(body, "tools")
	if tools.IsArray() {
		for _, tool := range tools.Array() {
			if containsHostedType(tool) {
				return true
			}
		}
	}
	toolChoice := gjson.GetBytes(body, "tool_choice")
	if containsHostedType(toolChoice) {
		return true
	}
	if toolChoice.Type == gjson.String {
		if isOpenAIResponsesLiteHostedToolType(toolChoice.String()) {
			return true
		}
	}
	input := openAIBodyGet(body, "input")
	if input.IsArray() {
		for _, item := range input.Array() {
			itemType := strings.TrimSpace(item.Get("type").String())
			if isOpenAIResponsesLiteHostedToolCallType(itemType) {
				return true
			}
		}
	}
	return false
}

type openAIResponsesLiteValidationError struct {
	param   string
	message string
}

func (e *openAIResponsesLiteValidationError) Error() string { return e.message }

func newOpenAIResponsesLiteValidationError(param, format string, args ...any) error {
	return &openAIResponsesLiteValidationError{param: param, message: fmt.Sprintf(format, args...)}
}

// normalizeOpenAIResponsesLiteTools applies the Responses Lite request
// contract: reasoning must cover all turns, and private namespace declarations
// use the input.additional_tools carrier. Other top-level tools must belong to
// the small set accepted by the Lite endpoint; rejecting unsupported hosted
// tools is intentional because silently dropping them would change behavior.
func normalizeOpenAIResponsesLiteTools(reqBody map[string]any) (bool, error) {
	if reqBody == nil {
		return false, nil
	}
	if parallel, exists := reqBody["parallel_tool_calls"]; exists {
		if _, ok := parallel.(bool); !ok {
			return false, newOpenAIResponsesLiteValidationError("parallel_tool_calls", "responses Lite requires parallel_tool_calls to be a boolean")
		}
	}
	if rawReasoning, exists := reqBody["reasoning"]; exists && rawReasoning != nil {
		if _, ok := rawReasoning.(map[string]any); !ok {
			return false, newOpenAIResponsesLiteValidationError("reasoning", "responses Lite requires reasoning to be an object")
		}
	}
	rawTools, exists := reqBody["tools"]
	if !exists || rawTools == nil {
		changed, err := ensureOpenAIResponsesLiteReasoningContext(reqBody)
		if err != nil {
			return false, err
		}
		return ensureOpenAIResponsesLiteParallelToolCalls(reqBody, changed)
	}
	tools, ok := rawTools.([]any)
	if !ok {
		return false, newOpenAIResponsesLiteValidationError("tools", "responses Lite requires tools to be an array")
	}

	topLevelTools := make([]any, 0, len(tools))
	namespaceTools := make([]any, 0, len(tools))
	for index, rawTool := range tools {
		if customTool, ok := rawTool.(string); ok {
			if strings.TrimSpace(customTool) == "" {
				return false, fmt.Errorf("responses Lite custom tool at index %d must not be empty", index)
			}
			topLevelTools = append(topLevelTools, rawTool)
			continue
		}
		tool, ok := rawTool.(map[string]any)
		if !ok {
			return false, fmt.Errorf("responses Lite tool at index %d must be an object", index)
		}
		toolType := strings.TrimSpace(firstNonEmptyString(tool["type"]))
		switch toolType {
		case "function", "custom", "tool_search":
			topLevelTools = append(topLevelTools, rawTool)
		case "namespace":
			namespaceTools = append(namespaceTools, rawTool)
		case "":
			return false, fmt.Errorf("responses Lite tool at index %d is missing type", index)
		default:
			return false, fmt.Errorf("responses Lite does not support top-level tool type %q at index %d", toolType, index)
		}
	}
	if len(namespaceTools) == 0 {
		changed, err := ensureOpenAIResponsesLiteReasoningContext(reqBody)
		if err != nil {
			return false, err
		}
		return ensureOpenAIResponsesLiteParallelToolCalls(reqBody, changed)
	}

	input, err := appendOpenAIResponsesLiteAdditionalTools(reqBody["input"], namespaceTools)
	if err != nil {
		return false, err
	}
	if _, err := ensureOpenAIResponsesLiteReasoningContext(reqBody); err != nil {
		return false, err
	}
	reqBody["input"] = input
	if len(topLevelTools) == 0 {
		delete(reqBody, "tools")
	} else {
		reqBody["tools"] = topLevelTools
	}
	return ensureOpenAIResponsesLiteParallelToolCalls(reqBody, true)
}

func ensureOpenAIResponsesLiteParallelToolCalls(reqBody map[string]any, changed bool) (bool, error) {
	parallel, exists := reqBody["parallel_tool_calls"]
	if exists {
		if _, ok := parallel.(bool); !ok {
			return false, newOpenAIResponsesLiteValidationError("parallel_tool_calls", "responses Lite requires parallel_tool_calls to be a boolean")
		}
	}
	if parallel == false {
		return changed, nil
	}
	reqBody["parallel_tool_calls"] = false
	return true, nil
}

func ensureOpenAIResponsesLiteReasoningContext(reqBody map[string]any) (bool, error) {
	rawReasoning, exists := reqBody["reasoning"]
	if !exists || rawReasoning == nil {
		reqBody["reasoning"] = map[string]any{"context": "all_turns"}
		return true, nil
	}
	reasoning, ok := rawReasoning.(map[string]any)
	if !ok {
		return false, newOpenAIResponsesLiteValidationError("reasoning", "responses Lite requires reasoning to be an object")
	}
	if context, ok := reasoning["context"].(string); ok && context == "all_turns" {
		return false, nil
	}
	reasoning["context"] = "all_turns"
	return true, nil
}

func appendOpenAIResponsesLiteAdditionalTools(input any, namespaceTools []any) ([]any, error) {
	var items []any
	switch typed := input.(type) {
	case nil:
		items = make([]any, 0, 1)
	case string:
		items = []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": typed,
		}}
	case []any:
		items = typed
	default:
		return nil, fmt.Errorf("responses Lite namespace tools require input to be a string or array")
	}

	var target map[string]any
	var targetTools []any
	var allAdditionalTools []any
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok || strings.TrimSpace(firstNonEmptyString(item["type"])) != "additional_tools" {
			continue
		}
		rawAdditionalTools, exists := item["tools"]
		additionalTools := []any(nil)
		toolsOK := true
		if exists && rawAdditionalTools != nil {
			additionalTools, toolsOK = rawAdditionalTools.([]any)
		}
		if !toolsOK {
			return nil, fmt.Errorf("responses Lite input.additional_tools tools must be an array")
		}
		if target == nil {
			target = item
			targetTools = additionalTools
		}
		allAdditionalTools = append(allAdditionalTools, additionalTools...)
	}

	merged, err := mergeOpenAIResponsesLiteAdditionalTools(allAdditionalTools, namespaceTools)
	if err != nil {
		return nil, err
	}
	newTools := merged[len(allAdditionalTools):]
	if target != nil {
		if len(newTools) > 0 {
			target["tools"] = append(append([]any(nil), targetTools...), newTools...)
		}
		return items, nil
	}

	items = append(items, map[string]any{
		"type":  "additional_tools",
		"role":  "developer",
		"tools": newTools,
	})
	return items, nil
}

func mergeOpenAIResponsesLiteAdditionalTools(existing []any, moved []any) ([]any, error) {
	merged := append([]any(nil), existing...)
	seen := make(map[string]any, len(existing)+len(moved))
	for _, rawTool := range existing {
		if identity := openAIResponsesLiteToolIdentity(rawTool); identity != "" {
			if previous, exists := seen[identity]; exists && !reflect.DeepEqual(previous, rawTool) {
				return nil, fmt.Errorf("responses Lite additional_tools contains conflicting definitions for %s", openAIResponsesLiteToolIdentityForError(rawTool))
			}
			seen[identity] = rawTool
		}
	}
	for _, rawTool := range moved {
		identity := openAIResponsesLiteToolIdentity(rawTool)
		if identity != "" {
			if previous, exists := seen[identity]; exists {
				if reflect.DeepEqual(previous, rawTool) {
					continue
				}
				return nil, fmt.Errorf("responses Lite additional_tools conflicts with migrated %s", openAIResponsesLiteToolIdentityForError(rawTool))
			}
			seen[identity] = rawTool
		}
		merged = append(merged, rawTool)
	}
	return merged, nil
}

func openAIResponsesLiteToolIdentity(rawTool any) string {
	tool, ok := rawTool.(map[string]any)
	if !ok {
		return ""
	}
	toolType := strings.TrimSpace(firstNonEmptyString(tool["type"]))
	name := strings.TrimSpace(firstNonEmptyString(tool["name"]))
	if toolType == "" || name == "" {
		return ""
	}
	return toolType + "\x00" + name
}

func openAIResponsesLiteToolIdentityForError(rawTool any) string {
	tool, _ := rawTool.(map[string]any)
	return fmt.Sprintf("tool type %q name %q", strings.TrimSpace(firstNonEmptyString(tool["type"])), strings.TrimSpace(firstNonEmptyString(tool["name"])))
}

func normalizeOpenAIResponsesLiteToolsPayload(body []byte) ([]byte, bool, error) {
	// 只读预检（docs/bug.md 6.4 第 3 点）：官方客户端发往 Lite 端点的请求本身已满足
	// 契约（reasoning.context=all_turns、parallel_tool_calls=false、namespace 工具已在
	// additional_tools），索引扫描能证明 normalizeOpenAIResponsesLiteTools 既不会改写也
	// 不会报错时，不构建对象树；否则在同一索引上建树，不再第二次扫描。非法正文或顶层
	// 不是对象时走解码路径，错误值与原实现一致。
	var requestBody map[string]any
	index, indexErr := buildOfficialJSONRawIndexForDecode(body)
	if indexErr == nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		if openAIResponsesLiteAlreadyNormalized(index) {
			return body, false, nil
		}
		// 对象树只在本函数内按 Lite 契约改写并随即保序拼接编码，其中的值只会被比较或编码进新正文、
		// 不会保存到函数之外；无转义的长字符串因此以只读视图引用 body，不再把整段正文复制进树
		// （问题四 M2 清单外副本：官方出站 Lite 改写路径上约 1.2 倍正文的瞬时峰值）。
		requestBody, _ = index.decodeValueSharingBody(index.root).(map[string]any)
	} else {
		index = nil
		var err error
		if requestBody, err = decodeOfficialJSONObjectUseNumber(body); err != nil {
			return body, false, fmt.Errorf("decode responses Lite request body: %w", err)
		}
	}
	changed, err := normalizeOpenAIResponsesLiteTools(requestBody)
	if err != nil || !changed {
		return body, false, err
	}
	// 拼接编码复用同一份索引（问题四 M2-c）：补齐结构摘要后与按原文现场建立的完整索引逐项相同，
	// 不再为同一正文扫描第二遍。index 为 nil（扫描未通过而 encoding/json 解码成功）时，拼接编码
	// 与过去一样按原文现场建索引。
	index.ensureDigests()
	rebuilt, err := marshalOfficialJSONObjectPreservingOrderAndRawWithIndex(requestBody, body, index)
	if err != nil {
		return body, false, fmt.Errorf("encode responses Lite request body: %w", err)
	}
	return rebuilt, true, nil
}

// openAIResponsesLiteAlreadyNormalized 只在能证明 normalizeOpenAIResponsesLiteTools 会返回
// (false, nil) 时返回 true：任何会报错或会改写的形态都返回 false，交给解码路径处理。判断
// 逻辑与 normalizeOpenAIResponsesLiteTools 及其辅助函数逐条对应，同名键取最后一次出现。
func openAIResponsesLiteAlreadyNormalized(index *officialJSONRawIndex) bool {
	root := index.root
	kind := func(node int32) officialJSONRawKind { return index.nodes[node].kind }
	// parallel_tool_calls 存在时必须是布尔，否则旧路径报错。
	parallel := index.memberNode(root, "parallel_tool_calls")
	if parallel >= 0 && kind(parallel) != officialJSONRawKindTrue && kind(parallel) != officialJSONRawKindFalse {
		return false
	}
	// reasoning 存在且非 null 时必须是对象，否则旧路径报错。
	reasoning := index.memberNode(root, "reasoning")
	reasoningIsObject := reasoning >= 0 && kind(reasoning) == officialJSONRawKindObject
	if reasoning >= 0 && kind(reasoning) != officialJSONRawKindNull && !reasoningIsObject {
		return false
	}
	tools := index.memberNode(root, "tools")
	toolsPresent := tools >= 0 && kind(tools) != officialJSONRawKindNull
	if toolsPresent {
		if kind(tools) != officialJSONRawKindArray {
			return false
		}
		for _, tool := range index.nodes[tools].items {
			switch kind(tool) {
			case officialJSONRawKindString:
				if strings.TrimSpace(index.decodeString(tool)) == "" {
					return false
				}
			case officialJSONRawKindObject:
				toolType := ""
				if typeNode := index.memberNode(tool, "type"); typeNode >= 0 && kind(typeNode) == officialJSONRawKindString {
					toolType = strings.TrimSpace(index.decodeString(typeNode))
				}
				switch toolType {
				case "function", "custom", "tool_search":
				default:
					// namespace 需要迁移到 additional_tools；空或其他类型旧路径报错。
					return false
				}
			default:
				return false
			}
		}
	}
	// ensureOpenAIResponsesLiteReasoningContext：缺失、null 或 context 不是 "all_turns" 都会改写。
	if !reasoningIsObject {
		return false
	}
	contextNode := index.memberNode(reasoning, "context")
	if contextNode < 0 || kind(contextNode) != officialJSONRawKindString ||
		!index.stringEquals(&index.nodes[contextNode], "all_turns") {
		return false
	}
	// ensureOpenAIResponsesLiteParallelToolCalls：无论是否存在工具，归一化路径
	// 都会补齐 parallel_tool_calls=false；预检必须覆盖这一事实，避免“无工具
	// 且字段缺失”的请求被错误地当成已归一化而跳过补字段。
	if parallel < 0 || kind(parallel) != officialJSONRawKindFalse {
		return false
	}
	return true
}

func normalizeOpenAIResponsesLiteParallelToolCallsPayload(body []byte) ([]byte, bool, error) {
	var requestBody map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &requestBody); err != nil {
		return body, false, fmt.Errorf("decode responses Lite request body: %w", err)
	}
	changed, err := ensureOpenAIResponsesLiteParallelToolCalls(requestBody, false)
	if err != nil || !changed {
		return body, false, err
	}
	rebuilt, err := marshalOpenAIUpstreamJSON(requestBody)
	if err != nil {
		return body, false, fmt.Errorf("encode responses Lite request body: %w", err)
	}
	return rebuilt, true, nil
}

func normalizeOpenAIResponsesLitePayloadForAccount(body []byte, account *Account) ([]byte, bool, error) {
	if account == nil || !account.IsOpenAI() {
		return body, false, nil
	}
	if account.IsOpenAIOAuthLike() {
		return normalizeOpenAIResponsesLiteToolsPayload(body)
	}
	return normalizeOpenAIResponsesLiteParallelToolCallsPayload(body)
}
