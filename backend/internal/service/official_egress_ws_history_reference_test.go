package service

import (
	"encoding/json"
	"fmt"
	"strings"
)

// 本文件保留改造前的完整历史编码，仅用于差分验证。
// legacyOfficialOpenAIWSBusinessHistory 把允许变化的表示层字段剥离后再比较。
// additional_tools、逐项 turn metadata，以及兼容层按既有规则删除的非法 item.id
// 与非配对 call_id 都不承载业务语义；消息、工具调用、工具输出及其业务参数仍必须
// 保持一致。
func legacyOfficialOpenAIWSBusinessHistory(payload map[string]any) ([]byte, error) {
	cloned := cloneOfficialOpenAIMap(payload)
	if _, err := normalizeDerivedOfficialOpenAIInput(cloned); err != nil {
		return nil, fmt.Errorf("normalize OpenAI official egress WebSocket business history: %w", err)
	}
	input, _ := cloned["input"].([]any)
	filtered := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			filtered = append(filtered, rawItem)
			continue
		}
		if officialOpenAIString(item, "type") == officialOpenAIAdditionalToolsType {
			continue
		}
		itemClone := cloneOfficialOpenAIMap(item)
		delete(itemClone, officialOpenAIWSItemTurnMetadata)
		itemType := strings.TrimSpace(officialOpenAIString(itemClone, "type"))
		if itemID, ok := itemClone["id"].(string); ok &&
			shouldStripOpenAIResponsesInputItemID(itemType, itemID) {
			delete(itemClone, "id")
		}
		if _, exists := itemClone["call_id"]; exists &&
			shouldStripOpenAIResponsesNonPairCallID(itemType) {
			delete(itemClone, "call_id")
		}
		if itemType == "message" {
			itemClone["content"] = officialOpenAIHTTPMessageContentText(itemClone["content"])
		}
		filtered = append(filtered, itemClone)
	}
	encoded, err := json.Marshal(filtered)
	if err != nil {
		return nil, fmt.Errorf("encode OpenAI official egress WebSocket business history: %w", err)
	}
	return encoded, nil
}
