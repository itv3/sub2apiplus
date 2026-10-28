package service

import (
	"strings"

	"github.com/tidwall/gjson"
)

// HTTP 压缩请求的元数据与请求体（画像 TurnMetadata 节 HTTPCompactionRequests）。
//
// 官方 0.165 的 HTTP `/responses` 压缩请求（input 里带 `compaction_trigger`）与 WS 压缩帧一样（见 official_egress_openai_ws_compaction.go）：
// 请求体不带 access_programs；turn metadata 的 request_kind 是 compaction，带 compaction{…}（排在最后一个键），不带 model、
// reasoning_effort、turn_trigger（官方实采 HTTP 压缩续聊会话）。画像没有该字段时本文件的入口都返回“不是压缩请求”，
// HTTP 压缩请求按普通回合请求处理（旧行为）。

// officialCodexTurnMetadataHTTPCompactionRequests 判断 mode 对应画像是否声明 HTTP 压缩请求按官方写元数据与请求体。
func officialCodexTurnMetadataHTTPCompactionRequests(mode string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).TurnMetadata
	return section != nil && section.HTTPCompactionRequests
}

// officialCodexPayloadHasCompactionTrigger 判断请求体 input 里是否带 compaction_trigger 项（与 HasCompactionTriggerInInput 同口径）。
func officialCodexPayloadHasCompactionTrigger(payload map[string]any) bool {
	input, _ := payload["input"].([]any)
	for _, raw := range input {
		item, _ := raw.(map[string]any)
		if officialOpenAIString(item, "type") == "compaction_trigger" {
			return true
		}
	}
	return false
}

// officialCodexHTTPCompactionRequestBody 判断 HTTP 请求体是否按官方压缩请求处理：画像声明且 input 带 compaction_trigger。
// 用于请求体层面的取舍（不生成 access_programs）。
func officialCodexHTTPCompactionRequestBody(mode string, payload map[string]any) bool {
	return officialCodexTurnMetadataHTTPCompactionRequests(mode) && officialCodexPayloadHasCompactionTrigger(payload)
}

// officialCodexHTTPCompactionTurnMetadata 判断已定型的 turn metadata 是否是压缩请求：画像声明且 request_kind 是 compaction。
// 用于 turn metadata 层面的取舍（不写 model、reasoning_effort、turn_trigger）。
func officialCodexHTTPCompactionTurnMetadata(mode, turnMetadata string) bool {
	return officialCodexTurnMetadataHTTPCompactionRequests(mode) &&
		gjson.Get(turnMetadata, "request_kind").String() == "compaction"
}
