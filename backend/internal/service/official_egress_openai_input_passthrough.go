package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/config"
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
//  2. 本次请求（HTTP）或本条连接（WS）走 Lite；非 Lite 官方客户端自己会清掉 content_item_metadata，网关沿用旧行为整体剥离；
//  3. 画像 BaseInstructions 节声明 IDScheme=uuidv7（即该版本画像按“增量窗口”承载前缀，同一开关管 passthrough 保真）；
//  4. 该 input 项的 passthrough 是官方形态（见 isOfficialCodexInputPassthroughForm）。第三方客户端发来的任意形态
//     不满足第 4 条，仍按旧行为剥离；网关也不会替第三方入站合成逐项 passthrough。
//
// 任何一条不满足，行为与引入本文件之前逐字节相同。画像不声明 uuidv7（含 0.163 画像）时，本文件的判定恒为否。

// officialCodexInputPassthroughPreservedForMode 判断给定发布模式、端点上，画像是否声明了 passthrough 保真所需的
// uuidv7 增量窗口方案。
func officialCodexInputPassthroughPreservedForMode(mode, endpointID string) bool {
	section, ok := officialCodexBaseInstructionsCarrier(normalizeOfficialClientProfileMode(mode), endpointID)
	return ok && section.UsesUUIDv7IDs()
}

// officialCodexWSInputPassthroughPreserved 是 WS 入站（兼容层）的前三条判定：官方出站账号、Lite 连接、画像声明 uuidv7。
// cfg 决定发布模式（空配置按 active，与 attachOfficialEgressWebSocketContext 一致）。
func officialCodexWSInputPassthroughPreserved(cfg *config.Config, account *Account, responsesLite bool) bool {
	if !responsesLite || account == nil || !account.IsOpenAI() || !account.UsesOfficialEgressProfile() {
		return false
	}
	return officialCodexInputPassthroughPreservedForMode(
		officialClientProfileModeFromConfig(cfg), officialCodexEndpointResponsesWS,
	)
}

// officialCodexHTTPInputPassthroughPreserved 是 HTTP 入站（兼容层）的前三条判定。mode 是调用方已冻结的发布模式
// （转发入口里的 codexReleaseMode）。
func officialCodexHTTPInputPassthroughPreserved(mode string, account *Account, useResponsesLite bool) bool {
	if !useResponsesLite || account == nil || !account.IsOpenAI() || !account.UsesOfficialEgressProfile() {
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
func isOfficialCodexInputPassthroughForm(itemType string, passthrough gjson.Result) bool {
	if !officialCodexInputItemTypeCarriesPassthrough(itemType) || !passthrough.IsObject() {
		return false
	}
	hasKey := false
	valid := true
	passthrough.ForEach(func(key, value gjson.Result) bool {
		hasKey = true
		switch officialCodexInputPassthroughKeyKind(key.String()) {
		case "string":
			valid = value.Type == gjson.String
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

// isOfficialCodexInputPassthroughFormMap 是 isOfficialCodexInputPassthroughForm 的 map 版本（兼容层字段路径用）。
// 数字可能是 float64、json.Number 或整数类型，数组可能是 []any 或 []string。
func isOfficialCodexInputPassthroughFormMap(itemType string, passthrough any) bool {
	object, ok := passthrough.(map[string]any)
	if !ok || len(object) == 0 || !officialCodexInputItemTypeCarriesPassthrough(itemType) {
		return false
	}
	for key, value := range object {
		switch officialCodexInputPassthroughKeyKind(key) {
		case "string":
			if _, ok := value.(string); !ok {
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
// 基础指令消息不处理：它的 turn_id 是窗口第一个回合的，定型逻辑本来就跳过它。仅在画像声明 uuidv7 方案时调用。
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

// applyCodexAccountIdentityInputTurnIDsMap 把保留下来的逐项 passthrough 里的客户端 turn_id 按账号命名空间改写，
// 与 applyCodexAccountIdentityClientMetadataMap 对入站 client_metadata.turn_id 的改写用同一个函数（种类 "turn"）：
// 网关对所有入站身份值（安装、会话、线程、轮次）都做账号作用域化，避免同一客户端会话在调度器换号后把同一个原始值
// 送给两个上游账号；逐项 turn_id 不能例外，否则原始客户端 turn_id 会跨账号泄漏。同一原始 turn_id 得到同一结果，
// 所以同一回合的各项仍彼此一致，历史回合各自稳定。
// 基础指令消息的 turn_id 属于窗口第一个回合，与其 id 一样按官方原文放行，不改写。无账号命名空间时恒等。返回是否有改动。
//
// 已知差距：HTTP 官方出站定型会为出站 client_metadata.turn_id 另行重新派生（不使用这个函数），所以 HTTP 上逐项 turn_id 与
// 出站 client_metadata.turn_id 不相等。要让两者对上，需要在 HTTP 定型里按网关的轮次身份重派生（见交回报告）。
//
// WS 不用这个函数：WS 的逐项 turn_id 由定型逻辑按网关自己的线程身份改写（neutralizeOfficialOpenAIWSClientTurnIDs）。
func applyCodexAccountIdentityInputTurnIDsMap(
	requestBody map[string]any,
	account *Account,
	apiKeyID int64,
	isBaseInstructions func(item map[string]any) bool,
) bool {
	if requestBody == nil || codexAccountIdentityNamespace(account) == "" {
		return false
	}
	input, ok := requestBody["input"].([]any)
	if !ok {
		return false
	}
	changed := false
	for _, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid || !officialCodexInputItemTypeCarriesPassthrough(officialOpenAIString(item, "type")) {
			continue
		}
		if isBaseInstructions != nil && isBaseInstructions(item) {
			continue
		}
		metadata, valid := item[officialOpenAIWSItemTurnMetadata].(map[string]any)
		if !valid {
			continue
		}
		raw, valid := metadata["turn_id"].(string)
		if !valid || strings.TrimSpace(raw) == "" {
			continue
		}
		if next := scopeCodexAccountIdentityValue(account, apiKeyID, "turn", raw); next != raw {
			metadata["turn_id"] = next
			changed = true
		}
	}
	return changed
}

// officialCodexStashedInputPassthroughKey 是兼容层转换期间暂存逐项 passthrough 的私有键。它不是任何协议字段：
// 只在 stash／restore 之间短暂存在于内存里的 map，转换（含 input 过滤、内容归一化）会把未知键随项一起复制，
// 但兼容层的剥离只认原键名，所以暂存的值躲过剥离；放回之后这个私有键不再存在。
const officialCodexStashedInputPassthroughKey = "__sub2api_stashed_input_passthrough"

// stashOfficialInputPassthroughForTransform 在 HTTP 字段路径的兼容层转换之前，把官方形态的逐项 passthrough 暂存到私有键
// （改键名而不是复制，值是同一个 map）。这样不需要改兼容层转换函数本身（它被历史 transition 回执钉住）。
// 返回是否暂存了任何项；非官方形态的项不暂存，由转换照旧剥离。
func stashOfficialInputPassthroughForTransform(reqBody map[string]any) bool {
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
		if !exists || !isOfficialCodexInputPassthroughFormMap(officialOpenAIString(item, "type"), passthrough) {
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
