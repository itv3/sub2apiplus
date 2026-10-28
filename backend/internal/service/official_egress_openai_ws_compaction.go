package service

import (
	"strings"

	"github.com/tidwall/gjson"
)

// WS 压缩请求的元数据与请求体（画像 TurnMetadata 节 WSCompactionRequests）。
//
// 官方压缩请求（远程压缩 v2）是一帧 previous_response_id 加单项 `{"type":"compaction_trigger"}` 的增量帧。与普通业务帧相比
// （官方 0.165 实采）：请求体不带 access_programs；turn metadata 的 request_kind 是 compaction，带
// compaction{trigger,reason,implementation,phase,strategy}，不带 model、reasoning_effort、turn_trigger。
// 画像没有该字段时本文件的入口都返回“不是压缩请求”，压缩帧按普通业务帧处理（旧行为）。

// officialCodexTurnMetadataWSCompactionRequests 判断 mode 对应画像是否声明 WS 压缩请求按官方写元数据与请求体。
func officialCodexTurnMetadataWSCompactionRequests(mode string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).TurnMetadata
	return section != nil && section.WSCompactionRequests
}

// officialCodexWSIsCompactionTriggerFrame 判断语义帧的 input 末项是不是 compaction_trigger。
func officialCodexWSIsCompactionTriggerFrame(payload map[string]any) bool {
	input, _ := payload["input"].([]any)
	if len(input) == 0 {
		return false
	}
	last, _ := input[len(input)-1].(map[string]any)
	return officialOpenAIString(last, "type") == "compaction_trigger"
}

// officialCodexWSIsCompactionRequest 判断本帧是否按官方压缩请求处理：画像声明、会话走派生出站、末项是 compaction_trigger。
func officialCodexWSIsCompactionRequest(egressContext *OfficialEgressContext, payload map[string]any) bool {
	return egressContext != nil && egressContext.openAIWSDerived != nil &&
		officialCodexTurnMetadataWSCompactionRequests(egressContext.ProfileMode()) &&
		officialCodexWSIsCompactionTriggerFrame(payload)
}

// officialCodexCompactionMetadataForReason 按压缩原因生成官方 compaction 对象。取值规则与 HTTP 压缩一致
// （resolveDerivedOfficialOpenAICompactionMetadata）：用户主动压缩是 manual／standalone_turn；三种自动原因是 auto，
// context_limit 为 mid_turn，model_downshift 与 comp_hash_changed 为 pre_turn；未知原因回落 user_requested。
func officialCodexCompactionMetadataForReason(reason string) officialOpenAICompactionMetadata {
	reason = strings.TrimSpace(reason)
	if _, ok := officialOpenAICompactionReasons[reason]; !ok {
		reason = "user_requested"
	}
	metadata := officialOpenAICompactionMetadata{
		Trigger:        "manual",
		Reason:         reason,
		Implementation: "responses_compaction_v2",
		Phase:          "standalone_turn",
		Strategy:       "memento",
	}
	if reason != "user_requested" {
		metadata.Trigger = "auto"
		metadata.Phase = "mid_turn"
		if reason == "model_downshift" || reason == "comp_hash_changed" {
			metadata.Phase = "pre_turn"
		}
	}
	return metadata
}

// officialCodexWSCompactionMetadata 为 WS 压缩请求生成 compaction 对象：原因取自入站帧自带的 turn metadata
// （只认官方四种取值，非法值不会伪造第五种）。不是压缩请求时第二个返回值为 false。
func officialCodexWSCompactionMetadata(
	egressContext *OfficialEgressContext,
	payload map[string]any,
) (officialOpenAICompactionMetadata, bool) {
	if !officialCodexWSIsCompactionRequest(egressContext, payload) {
		return officialOpenAICompactionMetadata{}, false
	}
	clientMetadata, _ := payload["client_metadata"].(map[string]any)
	reason := gjson.Get(officialOpenAIString(clientMetadata, "x-codex-turn-metadata"), "compaction.reason").String()
	return officialCodexCompactionMetadataForReason(reason), true
}
