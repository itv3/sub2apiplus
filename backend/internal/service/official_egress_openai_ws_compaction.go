package service

import (
	"fmt"
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

// officialCodexWSCompactionHasNoStampedItems 判断压缩帧 input 里是否只有“官方不带 passthrough”的项（compaction_trigger、
// configuration_update、additional_tools）。官方压缩请求（增量帧）正是这种形态；带历史消息的完整历史压缩帧，其项里已有
// 原回合的 turn_id（见 finalizeOfficialOpenAIWSInputTurnMetadata 的一致性校验），不能再换成压缩自己的回合 id，仍沿用原回合。
func officialCodexWSCompactionHasNoStampedItems(payload map[string]any) bool {
	input, _ := payload["input"].([]any)
	if len(input) == 0 {
		return false
	}
	for _, raw := range input {
		item, _ := raw.(map[string]any)
		itemType := strings.TrimSpace(officialOpenAIString(item, "type"))
		if itemType != openAIConfigurationUpdateItemType && !officialCodexItemTypeHasNoTurnMetadata(itemType) {
			return false
		}
	}
	return true
}

// officialCodexWSCompactionTurnID 为 WS 压缩请求（画像 TurnMetadata.WSCompactionRequests）生成它自己的回合 id 与开始时间。
//
// 官方 0.165 的压缩请求是独立的一次提交：turn_id 是新的 UUIDv7（根线程时 root_turn_id 等于它），既不同于压缩前最后一个
// 业务回合，也不同于压缩之后下一个业务回合（实采 c1：三帧三个不同 id）。没有这个处理时压缩帧因没有用户锚点而沿用前一回合的 id。
//
// 稳定性：回合 id 由进程内稳定缓存按 seed 复用——seed 取会话、previous_response_id 与 input 摘要。同一帧被重试（恢复入站原帧后
// 再定型）seed 不变，拿到同一个 id；下一次压缩的 previous_response_id 不同，拿到新的 id。本函数不写连接状态：压缩帧之后的帧仍按原规则
// （用户锚点或沿用 lastTurnID）取回合，因此轮内压缩后继续同一回合的帧不受影响。
//
// 不是压缩请求、或压缩帧里带有已盖原回合 turn_id 的历史项时返回 ok=false，调用方沿用原回合 id。
func officialCodexWSCompactionTurnID(
	egressContext *OfficialEgressContext,
	sessionID string,
	payload map[string]any,
) (turnID string, startedAtMS int64, ok bool, err error) {
	if !officialCodexWSIsCompactionRequest(egressContext, payload) || !officialCodexWSCompactionHasNoStampedItems(payload) {
		return "", 0, false, nil
	}
	digest, digestErr := digestOfficialJSONValue(payload["input"])
	if digestErr != nil {
		return "", 0, false, fmt.Errorf("构造 WebSocket 压缩请求 Turn ID seed：%w", digestErr)
	}
	turnID = generateOfficialStableTurnUUIDV7(
		newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
			WriteString(sessionID).
			WriteString("compaction_request").
			WriteString(officialOpenAIString(payload, "previous_response_id")).
			WriteDigest(digest).
			Key(),
	)
	return turnID, officialCodexTurnStartedAtMsOrNow(turnID), true, nil
}
