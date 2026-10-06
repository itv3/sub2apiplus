package service

import (
	"strings"
)

const openAIResponsesInputTextMaxChars = 10000000

// sanitizeOpenAIResponsesOrphanToolOutputs removes tool-output items that have
// no matching call or item reference anywhere in the current input. Named
// function outputs without a call ID are standalone inputs, not orphan results.
func sanitizeOpenAIResponsesOrphanToolOutputs(reqBody map[string]any, input []any, hasPreviousResponseID bool) bool {
	if len(input) == 0 || hasPreviousResponseID {
		return false
	}

	toolCallIDs := make(map[string]struct{}, len(input))
	referenceIDs := make(map[string]struct{}, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		itemType := strings.TrimSpace(firstNonEmptyString(item["type"]))
		if itemType == "item_reference" {
			if id := strings.TrimSpace(firstNonEmptyString(item["id"])); id != "" {
				referenceIDs[id] = struct{}{}
			}
			continue
		}
		if !isCodexToolCallContextItemType(itemType) {
			continue
		}
		if id := strings.TrimSpace(firstNonEmptyString(item["call_id"], item["id"])); id != "" {
			toolCallIDs[id] = struct{}{}
		}
	}

	modified := false
	normalized := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || !isCodexToolCallOutputItemType(strings.TrimSpace(firstNonEmptyString(item["type"]))) {
			normalized = append(normalized, rawItem)
			continue
		}

		callID := strings.TrimSpace(firstNonEmptyString(item["call_id"]))
		// Codex sends externally supplied inputs (such as task delegation) as
		// named function outputs without a preceding function call. Preserve
		// their native type, namespace and output instead of silently dropping
		// the request that started the turn.
		if callID == "" && strings.TrimSpace(firstNonEmptyString(item["type"])) == "function_call_output" &&
			strings.TrimSpace(firstNonEmptyString(item["name"])) != "" {
			normalized = append(normalized, rawItem)
			continue
		}
		_, hasToolCall := toolCallIDs[callID]
		_, hasReference := referenceIDs[callID]
		if callID != "" && (hasToolCall || hasReference) {
			normalized = append(normalized, rawItem)
			continue
		}

		modified = true
	}
	if !modified {
		return false
	}
	reqBody["input"] = normalized
	return true
}

// openAIResponsesHasOrphanToolOutputsFromIndex 只检查上述清理是否会改写 input。
// 读取规则保持一致：字符串才参与匹配，同名键取末值，调用项允许以 id 补 call_id，
// 独立的具名 function_call_output 保留；消息正文和工具输出全文无需解码。
func openAIResponsesHasOrphanToolOutputsFromIndex(index *officialJSONRawIndex) bool {
	readString := func(node int32, keys ...string) string {
		for _, key := range keys {
			child := index.memberNode(node, key)
			if child >= 0 && index.nodes[child].kind == officialJSONRawKindString {
				if value := strings.TrimSpace(index.decodeString(child)); value != "" {
					return value
				}
			}
		}
		return ""
	}
	if readString(index.root, "previous_response_id") != "" {
		return false
	}
	input := index.memberNode(index.root, "input")
	if input < 0 || index.nodes[input].kind != officialJSONRawKindArray {
		return false
	}
	knownIDs := make(map[string]struct{})
	for _, item := range index.nodes[input].items {
		if index.nodes[item].kind != officialJSONRawKindObject {
			continue
		}
		typeName := readString(item, "type")
		id := ""
		if typeName == "item_reference" {
			id = readString(item, "id")
		} else if isCodexToolCallContextItemType(typeName) {
			id = readString(item, "call_id", "id")
		}
		if id != "" {
			knownIDs[id] = struct{}{}
		}
	}
	for _, item := range index.nodes[input].items {
		if index.nodes[item].kind != officialJSONRawKindObject {
			continue
		}
		typeName := readString(item, "type")
		if !isCodexToolCallOutputItemType(typeName) {
			continue
		}
		callID := readString(item, "call_id")
		if callID == "" && typeName == "function_call_output" && readString(item, "name") != "" {
			continue
		}
		if _, exists := knownIDs[callID]; callID == "" || !exists {
			return true
		}
	}
	return false
}

func truncateOpenAIResponsesInputText(_ map[string]any) bool {
	// Do not silently rewrite client or tool output. If an upstream enforces a
	// text limit, forwarding the original value preserves its explicit error for
	// the client and the normal Ops error pipeline. This compatibility shim is
	// retained until the two callers can remove the old mutation hook together.
	return false
}

func openAIResponsesInputMayNeedTruncation(_ []byte) bool {
	// See truncateOpenAIResponsesInputText. Returning false also avoids decoding
	// very large bodies solely for a mutation that must not happen.
	return false
}
