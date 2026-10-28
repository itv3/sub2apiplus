package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
)

// input 项逐项 internal_chat_message_metadata_passthrough 的保真（较新版本官方客户端的 Lite 请求）。
//
// 背景：OAuth 兼容层历史上会把每个 input 项的 internal_chat_message_metadata_passthrough 整个剥掉，理由是上游社区
// 记录的 #7066（名叫 OpenAI 的自定义 provider 经网关转发时，ChatGPT 对该字段报错）。这个理由在官方出站画像下已经过时：
//   - 官方客户端本身就带着这些字段发往 chatgpt.com（较新版本的 Lite 请求里每条消息都带 turn_id、create_time、
//     content_item_kinds、content_item_metadata 四个键）；
//   - 真实的 0.163 官方客户端带着这些键（turn_id、create_time、content_item_kinds）请求真实 chatgpt.com，
//     返回过 200（取证记录 probe-s4-20261020）。
//
// 所以同时满足下面四条时，兼容层对该 input 项的 passthrough 原样保留，不再剥离：
//  1. 账号走官方出站（OpenAI OAuth 内置画像；SetupToken 等不经官方出站定型的账号不满足，仍按旧行为剥离）；
//  2. Lite 与非 Lite 都适用，区别只在“官方形态”：Lite 的 passthrough 可带 content_item_metadata；非 Lite 官方客户端
//     自己会清掉 content_item_metadata（turn_id、create_time、content_item_kinds 三键），非 Lite 入站带了它就不是官方形态，
//     该项整个 passthrough 照旧被剥；
//  3. 画像 BaseInstructions 节声明 IDScheme=uuidv7（即该版本画像按“增量窗口”承载前缀，同一开关管 passthrough 保真）；
//  4. 该 input 项的 passthrough 是官方形态（见 isOfficialCodexInputPassthroughForm，按 Lite／非 Lite 判）。第三方客户端
//     发来的任意形态不满足第 4 条，仍按旧行为剥离；网关也不会替第三方入站合成逐项 passthrough。
//
// 保留下来的逐项 turn_id 属于客户端自己的轮次命名空间：WS 在定型前校验后清空、由定型逻辑回填网关派生值；HTTP 在定型里
// 校验后改写成网关本轮的 turn_id（当前回合）和网关按线程与该段末条用户消息派生的稳定值（历史各段）。
//
// 任何一条不满足，行为与引入本文件之前逐字节相同。画像不声明 uuidv7（含 0.163 画像）时，本文件的判定恒为否。

// officialCodexInputPassthroughPreservedForMode 判断给定发布模式、端点上，画像是否声明了 passthrough 保真所需的
// uuidv7 增量窗口方案。
func officialCodexInputPassthroughPreservedForMode(mode, endpointID string) bool {
	section, ok := officialCodexBaseInstructionsCarrier(normalizeOfficialClientProfileMode(mode), endpointID)
	return ok && section.UsesUUIDv7IDs()
}

// officialCodexWSInputPassthroughPreserved 是 WS 入站（兼容层）的前两条判定：官方出站账号、画像声明 uuidv7（Lite 与否
// 只影响逐项的“官方形态”判定，不影响这里）。cfg 决定发布模式（空配置按 active，与 attachOfficialEgressWebSocketContext 一致）。
func officialCodexWSInputPassthroughPreserved(cfg *config.Config, account *Account) bool {
	if account == nil || !account.IsOpenAI() || !account.UsesOfficialEgressProfile() {
		return false
	}
	return officialCodexInputPassthroughPreservedForMode(
		officialClientProfileModeFromConfig(cfg), officialCodexEndpointResponsesWS,
	)
}

// officialCodexHTTPInputPassthroughPreserved 是 HTTP 入站（兼容层）的前两条判定。mode 是调用方已冻结的发布模式
// （转发入口里的 codexReleaseMode）。
func officialCodexHTTPInputPassthroughPreserved(mode string, account *Account) bool {
	if account == nil || !account.IsOpenAI() || !account.UsesOfficialEgressProfile() {
		return false
	}
	return officialCodexInputPassthroughPreservedForMode(mode, officialCodexEndpointResponsesHTTP)
}

// officialCodexInputItemTypeCarriesPassthrough 判断官方 ResponseItem 的这种变体是否带 passthrough 字段。
// additional_tools、configuration_update、compaction_trigger 三种在官方客户端里没有该字段，出现在入站里说明不是官方形态。
func officialCodexInputItemTypeCarriesPassthrough(itemType string) bool {
	return !officialCodexItemTypeHasNoTurnMetadata(strings.TrimSpace(itemType))
}

// officialCodexInputPassthroughKeyKind 返回 passthrough 里官方键的值类型约束；未知键返回 ""。
// 键集合取自官方 InternalChatMessageMetadataPassthrough：turn_id、create_time、content_item_kinds、content_item_metadata，
// 另有 cell_id、executed_tool_calls、tool_calls_complete（工具调用项由客户端自己写入）。
func officialCodexInputPassthroughKeyKind(key string) string {
	switch key {
	case "turn_id", "cell_id":
		return "string"
	case "create_time":
		return "number"
	case "content_item_kinds":
		return "string_array"
	case "content_item_metadata", "executed_tool_calls":
		return "array"
	case "tool_calls_complete":
		return "bool"
	}
	return ""
}

// isOfficialCodexInputPassthroughForm 判断入站 input 项（原文）上的 passthrough 是不是官方形态：项类型允许带该字段，
// passthrough 是对象，键全部属于官方键集合且值类型符合。空对象不算官方形态（官方在无内容时直接省略整个字段）。
// lite 为假时（非 Lite 请求）官方客户端会清掉 content_item_metadata，带了它就不是官方形态。
// turn_id 非空时必须是 UUID，否则同样不是官方形态（该项 passthrough 照旧被剥离，而不是留到定型阶段整帧被拒绝）。
func isOfficialCodexInputPassthroughForm(itemType string, passthrough gjson.Result, lite bool) bool {
	if !officialCodexInputItemTypeCarriesPassthrough(itemType) || !passthrough.IsObject() {
		return false
	}
	if !lite && passthrough.Get("content_item_metadata").Exists() {
		return false
	}
	hasKey := false
	valid := true
	passthrough.ForEach(func(key, value gjson.Result) bool {
		hasKey = true
		switch officialCodexInputPassthroughKeyKind(key.String()) {
		case "string":
			valid = value.Type == gjson.String
			// turn_id 非空时必须是 UUID：官方客户端的 turn_id 一直是 UUID，形状不对就不是官方形态，整项 passthrough
			// 照旧被剥离（与旧画像一致），而不是留到后面被整帧拒绝。
			if valid && key.String() == "turn_id" {
				valid = officialCodexPassthroughTurnIDWellFormed(value.String())
			}
		case "number":
			valid = value.Type == gjson.Number
		case "bool":
			valid = value.Type == gjson.True || value.Type == gjson.False
		case "array":
			valid = value.IsArray()
		case "string_array":
			valid = value.IsArray()
			if valid {
				value.ForEach(func(_, element gjson.Result) bool {
					valid = element.Type == gjson.String
					return valid
				})
			}
		default:
			valid = false
		}
		return valid
	})
	return hasKey && valid
}

// officialCodexPassthroughTurnIDWellFormed 判断 passthrough 里的 turn_id 字符串是否形状合规：空白（后续定型逻辑按"没有"
// 处理）或规范的 UUID。与 neutralizeOfficialOpenAIWSClientTurnIDs、rewriteOfficialOpenAIHTTPInputTurnIDs 的校验口径一致。
func officialCodexPassthroughTurnIDWellFormed(value string) bool {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return true
	}
	_, err := uuid.Parse(trimmed)
	return err == nil
}

// isOfficialCodexInputPassthroughFormMap 是 isOfficialCodexInputPassthroughForm 的 map 版本（兼容层字段路径用）。
// 数字可能是 float64、json.Number 或整数类型，数组可能是 []any 或 []string。
func isOfficialCodexInputPassthroughFormMap(itemType string, passthrough any, lite bool) bool {
	object, ok := passthrough.(map[string]any)
	if !ok || len(object) == 0 || !officialCodexInputItemTypeCarriesPassthrough(itemType) {
		return false
	}
	if _, has := object["content_item_metadata"]; has && !lite {
		return false
	}
	for key, value := range object {
		switch officialCodexInputPassthroughKeyKind(key) {
		case "string":
			text, ok := value.(string)
			if !ok {
				return false
			}
			if key == "turn_id" && !officialCodexPassthroughTurnIDWellFormed(text) {
				return false
			}
		case "number":
			switch value.(type) {
			case float64, float32, json.Number, int, int32, int64, uint, uint32, uint64:
			default:
				return false
			}
		case "bool":
			if _, ok := value.(bool); !ok {
				return false
			}
		case "array":
			if !officialCodexIsJSONArray(value) {
				return false
			}
		case "string_array":
			switch typed := value.(type) {
			case []string:
			case []any:
				for _, element := range typed {
					if _, ok := element.(string); !ok {
						return false
					}
				}
			default:
				return false
			}
		default:
			return false
		}
	}
	return true
}

func officialCodexIsJSONArray(value any) bool {
	switch value.(type) {
	case []any, []string, []map[string]any:
		return true
	}
	return false
}

// neutralizeOfficialOpenAIWSClientTurnIDs 把 WS 业务帧里入站 passthrough 的 turn_id 清为空串，交给逐项 turn_id 定型逻辑
// （finalizeOfficialOpenAIWSInputTurnMetadata）按网关自己的线程与轮次身份回填。
//
// 官方客户端自带的 turn_id 属于客户端自己的轮次命名空间，与网关按会话派生的 turn_id 不是同一套，也不保证两边对“回合”的
// 切分一致（例如换模型时追加的开发者提示消息，客户端归给新回合，网关按“助手输出之后的下一条用户消息”切分会把它们划进上一段）。
// 如果原样带进定型逻辑，会得到“turn_id conflicts with its turn”，工具输出的当前轮判定也会拿客户端 id 去比网关 id 而误判。
// 所以与网关重派生 msg_ 的思路一致：先校验（非空必须是 UUID，不放宽），再清空，由定型逻辑改写成网关的 turn_id；
// create_time、content_item_kinds、content_item_metadata 等其余键、键序都原样保留。
// 清为空串而不是删键，是为了让回填发生在键的原位置（官方键序 turn_id 在第一位）。
//
// 基础指令消息不在这里处理：它的 turn_id 由安装步骤改写成窗口第一段的网关 turn_id（见
// officialOpenAIWSFirstSegmentTurnID）。仅在画像声明 uuidv7 方案时调用。
// 返回是否有改动。
func neutralizeOfficialOpenAIWSClientTurnIDs(
	payload map[string]any,
	isBaseInstructions func(item map[string]any) bool,
) (bool, error) {
	input, ok := payload["input"].([]any)
	if !ok {
		return false, nil
	}
	modified := false
	for index, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid {
			continue
		}
		if isBaseInstructions != nil && isBaseInstructions(item) {
			continue
		}
		if !officialCodexInputItemTypeCarriesPassthrough(officialOpenAIString(item, "type")) {
			continue
		}
		metadata, valid := item[officialOpenAIWSItemTurnMetadata].(map[string]any)
		if !valid {
			continue
		}
		raw, exists := metadata["turn_id"]
		if !exists {
			continue
		}
		clientTurnID, isString := raw.(string)
		if !isString {
			return false, officialOpenAIWSInputError(index, "turn_id must be a string")
		}
		if trimmed := strings.TrimSpace(clientTurnID); trimmed != "" {
			if _, err := uuid.Parse(trimmed); err != nil {
				return false, officialOpenAIWSInputError(index, "turn_id must be UUID")
			}
		}
		if clientTurnID != "" {
			metadata["turn_id"] = ""
			modified = true
		}
	}
	return modified, nil
}

// officialOpenAIWSInputError 构造与定型逻辑同口径的逐项错误（"input item N …"）。
func officialOpenAIWSInputError(index int, detail string) error {
	return fmt.Errorf("OpenAI official egress WebSocket input item %d %s", index, detail)
}

// rewriteOfficialOpenAIHTTPInputTurnIDs 是 HTTP 定型对保留下来的逐项 passthrough.turn_id 的处理，与 WS 同思路：
// 客户端自带的 turn_id 属于客户端自己的轮次命名空间，先校验（非空必须是 UUID，不放宽），再改写成网关的值——
// 官方请求里逐项 turn_id 与该回合的 turn_id 从不不一致，不一致本身就是可识别的特征。
//   - 当前回合（按 splitOfficialOpenAIWSInputTurnSegments 的切分取最后一段）：改写成 currentTurnID，即本次请求网关派生的
//     身份 turn_id（也是 x-codex-turn-metadata 里的 turn_id）；
//   - 历史各段：网关按“线程＋该段末条用户消息锚点”派生的稳定值，公式与当前回合 turn_id 的派生相同
//     （generateOfficialStableUUIDV7(domainTurn, threadID, 锚点)），所以一段历史得到的值就是它当年作为当前回合时的值。
//
// 基础指令消息不在这里改（它的 turn_id 由安装步骤改写成窗口第一段的网关 turn_id，见 officialOpenAIHTTPFirstSegmentTurnID）；
// 官方没有 passthrough 的项类型跳过；
// 没有 turn_id 键的 passthrough 不补（HTTP 官方请求本就不是每项都带）。create_time、content_item_kinds、
// content_item_metadata 等其余键与键序不动。返回是否有改动。
func rewriteOfficialOpenAIHTTPInputTurnIDs(
	payload map[string]any,
	section *profilecontract.BaseInstructionsSection,
	currentTurnID string,
	threadID string,
) (bool, error) {
	input, ok := payload["input"].([]any)
	if !ok || strings.TrimSpace(currentTurnID) == "" {
		return false, nil
	}
	segments := splitOfficialOpenAIWSInputTurnSegments(input)
	modified := false
	for segmentIndex, segment := range segments {
		turnID := currentTurnID
		if segmentIndex < len(segments)-1 {
			turnID = deriveOfficialOpenAIHTTPHistoricalTurnID(input, segment, threadID)
		}
		for _, itemIndex := range segment {
			item, valid := input[itemIndex].(map[string]any)
			if !valid || !officialCodexInputItemTypeCarriesPassthrough(officialOpenAIString(item, "type")) {
				continue
			}
			if section != nil && isOfficialCodexBaseInstructionsItem(section, item) {
				continue
			}
			metadata, valid := item[officialOpenAIWSItemTurnMetadata].(map[string]any)
			if !valid {
				continue
			}
			rawTurnID, exists := metadata["turn_id"]
			if !exists {
				continue
			}
			clientTurnID, isString := rawTurnID.(string)
			if !isString {
				return false, fmt.Errorf("OpenAI official egress HTTP input item %d turn_id must be a string", itemIndex)
			}
			if trimmed := strings.TrimSpace(clientTurnID); trimmed != "" {
				if _, err := uuid.Parse(trimmed); err != nil {
					return false, fmt.Errorf("OpenAI official egress HTTP input item %d turn_id must be UUID", itemIndex)
				}
			}
			if clientTurnID != turnID {
				metadata["turn_id"] = turnID
				modified = true
			}
		}
	}
	return modified, nil
}

// officialOpenAIHTTPFirstSegmentTurnID 返回 HTTP 请求里"窗口第一段"的网关 turn_id，供基础指令消息的 passthrough.turn_id 使用。
//
// 官方客户端里基础指令消息的 turn_id 是窗口第一个回合的 id：首轮请求中它与其余项、client_metadata.turn_id 完全相同；
// 多轮时等于第一段历史的 id。网关的对应关系：
//   - input 只有一段（首轮、或第三方单轮）：就是 currentTurnID；
//   - 有多段：第一段历史的网关派生值（deriveOfficialOpenAIHTTPHistoricalTurnID），公式与它当年作为当前回合时的
//     turn_id 相同，所以同一窗口各请求上这个值稳定。
//
// 必须在基础指令消息安装之前、用入站 input 计算：派生种子里带用户消息在 input 中的下标，安装会在前面插入基础指令与工具项，
// 下标随之位移，安装之后再算会与它当年作为当前回合时的值不同，让这一项逐请求变化而击穿前缀缓存。
// 入站 input 不是数组（字符串、缺省）时按单段处理。currentTurnID 为空时返回空串（调用方据此不改写）。
func officialOpenAIHTTPFirstSegmentTurnID(payload map[string]any, currentTurnID string, threadID string) string {
	if strings.TrimSpace(currentTurnID) == "" {
		return ""
	}
	input, ok := payload["input"].([]any)
	if !ok {
		return currentTurnID
	}
	segments := splitOfficialOpenAIWSInputTurnSegments(input)
	if len(segments) <= 1 {
		return currentTurnID
	}
	return deriveOfficialOpenAIHTTPHistoricalTurnID(input, segments[0], threadID)
}

// officialOpenAIWSFirstSegmentTurnID 返回 WS 帧里"窗口第一段"的网关 turn_id，含义同 officialOpenAIHTTPFirstSegmentTurnID。
// 必须在 finalizeOfficialOpenAIWSInputTurnMetadata 之后、基础指令消息安装之前调用：此时各项 passthrough 已是网关值，
// 第一段的 turn_id 与该段其余项的 turn_id 一致。client_metadata 里没有可用的 turn_id（预热帧）、或第一段无法解出时返回空串，
// 调用方据此退回旧行为（合成走稳定值、官方原文不改写）。
func officialOpenAIWSFirstSegmentTurnID(
	payload map[string]any,
	baseInstructions *profilecontract.BaseInstructionsSection,
) string {
	metadata, ok := payload["client_metadata"].(map[string]any)
	if !ok {
		return ""
	}
	currentTurnID := strings.TrimSpace(officialOpenAIString(metadata, "turn_id"))
	if _, err := uuid.Parse(currentTurnID); err != nil {
		return ""
	}
	input, ok := payload["input"].([]any)
	if !ok {
		return currentTurnID
	}
	segments := splitOfficialOpenAIWSInputTurnSegments(input)
	if len(segments) <= 1 {
		return currentTurnID
	}
	sessionID := strings.TrimSpace(officialOpenAIString(metadata, "session_id"))
	if _, err := uuid.Parse(sessionID); err != nil {
		return ""
	}
	turnID, err := resolveOfficialOpenAIWSHistoricalTurnIDSkippingBase(input, segments[0], sessionID, 0, baseInstructions)
	if err != nil {
		return ""
	}
	return turnID
}

// deriveOfficialOpenAIHTTPHistoricalTurnID 派生历史回合的网关 turn_id：公式与 deriveOfficialOpenAIHTTPIdentity 里当前回合
// turn_id 的派生相同，锚点取该段里最后一条有文本的用户消息（下标是它在整个 input 里的位置）；没有用户文本的段用空锚点，
// 与当前回合“没有用户文本”时一致。
func deriveOfficialOpenAIHTTPHistoricalTurnID(input []any, segment []int, threadID string) string {
	var anchor officialOpenAIUserAnchor
	found := false
	for position := len(segment) - 1; position >= 0 && !found; position-- {
		itemIndex := segment[position]
		item, valid := input[itemIndex].(map[string]any)
		if !valid || officialOpenAIString(item, "type") != "message" || officialOpenAIString(item, "role") != "user" {
			continue
		}
		if digest, ok := digestOfficialOpenAIMessageContent(item["content"]); ok {
			anchor = officialOpenAIUserAnchor{index: itemIndex, digest: digest}
			found = true
		}
	}
	return generateOfficialStableTurnUUIDV7(
		newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).WriteString(threadID).WriteUserAnchor(anchor, found).Key(),
	)
}

// officialCodexStashedInputPassthroughKey 是兼容层转换期间暂存逐项 passthrough 的私有键。它不是任何协议字段：
// 只在 stash／restore 之间短暂存在于内存里的 map，转换（含 input 过滤、内容归一化）会把未知键随项一起复制，
// 但兼容层的剥离只认原键名，所以暂存的值躲过剥离；放回之后这个私有键不再存在。
const officialCodexStashedInputPassthroughKey = "__sub2api_stashed_input_passthrough"

// stashOfficialInputPassthroughForTransform 在 HTTP 字段路径的兼容层转换之前，把官方形态的逐项 passthrough 暂存到私有键
// （改键名而不是复制，值是同一个 map）。这样不需要改兼容层转换函数本身（它被历史 transition 回执钉住）。
// 返回是否暂存了任何项；非官方形态的项不暂存，由转换照旧剥离。
func stashOfficialInputPassthroughForTransform(reqBody map[string]any, lite bool) bool {
	input, ok := reqBody["input"].([]any)
	if !ok {
		return false
	}
	stashed := false
	for _, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid {
			continue
		}
		passthrough, exists := item[officialOpenAIWSItemTurnMetadata]
		if !exists || !isOfficialCodexInputPassthroughFormMap(officialOpenAIString(item, "type"), passthrough, lite) {
			continue
		}
		item[officialCodexStashedInputPassthroughKey] = passthrough
		delete(item, officialOpenAIWSItemTurnMetadata)
		stashed = true
	}
	return stashed
}

// restoreOfficialInputPassthroughAfterTransform 把暂存的 passthrough 放回原键名。必须与 stash 成对调用；
// 即使转换丢掉了某些项，被丢掉的项连同暂存值一起消失，不会残留私有键。
func restoreOfficialInputPassthroughAfterTransform(reqBody map[string]any) {
	input, ok := reqBody["input"].([]any)
	if !ok {
		return
	}
	for _, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid {
			continue
		}
		if passthrough, exists := item[officialCodexStashedInputPassthroughKey]; exists {
			delete(item, officialCodexStashedInputPassthroughKey)
			item[officialOpenAIWSItemTurnMetadata] = passthrough
		}
	}
}
