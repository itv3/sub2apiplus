package service

import (
	"encoding/json"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/tidwall/gjson"
)

// HTTP 出站对第三方入站合成 configuration_update（画像 ReasoningEffort 节声明 ConfigurationUpdate=http_first_turn）。
//
// 官方依据（0.165 非正式实采，X 路）：reasoning_effort_override 默认开启后，窗口内第一次采样时若还没有钉子，客户端在
// input 里记一条 {"type":"configuration_update","reasoning":{"effort":"<请求 effort>"}}；有 WS 预热的路径（含 WS 握手被拒后
// 回退 HTTP）预热帧已把 effort 钉死，首轮不带；只有名为 OpenAI、不带 WS 预热的 provider（正式 HTTP 采集用的那个）首轮
// input 末尾必带这一项。目录不声明 supports_reasoning_effort_updates 的模型、自定义（数字）档位都不产生。
//
// 网关的 HTTP 出站画像对应后一种 provider，WS 出站对应前一种。所以：
//   - HTTP 出站：第三方入站、模型目录声明支持、请求 effort 是已知档位时合成；位置是“首个用户轮次之后、第一个模型输出项之前”，
//     首轮就是 input 末尾（与官方一致），之后每个请求随第三方客户端重放的历史在同一位置带同一条（官方历史里这条一直留着）。
//     位置只看当前请求的 input，不需要任何会话状态，所以多实例、重启、重试都稳定；
//   - WS 出站：不合成（WS 帧定型是另一条路径，本文件不被调用）；
//   - 官方入站（入站带官方形态的基础指令消息）：原样透传，不合成——官方客户端自己知道窗口基线是否已钉；
//   - 取值：请求级 reasoning.effort 定型后的值（已知档位）。第三方入站每个请求的 effort 就是用户当前选择，没有“钉死的基线”，
//     所以 update 与请求级取值相同，不改变上游执行的 effort。
//
// 画像不声明该字段时本文件的判定恒为否，出站字节与引入本文件之前逐字节相同。

// officialCodexConfigurationUpdateKnownEffort 判断 effort 是否官方能写进 configuration_update 的档位
// （自定义档位、disabled 都不产生）。用 switch 而不是包级 map，避免多一个需要审计的包级可变状态。
func officialCodexConfigurationUpdateKnownEffort(effort string) bool {
	switch effort {
	case "none", "minimal", "low", "medium", "high", "xhigh", "max":
		return true
	}
	return false
}

// hasInboundTaggedKind 判断入站 input 前缀里是否有带指定内容标记的 developer 消息（官方客户端自带的基础指令消息等）。
func (c *officialOpenAIHTTPBodyContract) hasInboundTaggedKind(kind string) bool {
	if c == nil || kind == "" {
		return false
	}
	for _, message := range c.inboundTaggedMessages {
		for _, candidate := range message.kinds {
			if candidate == kind {
				return true
			}
		}
	}
	return false
}

// officialCodexInputItemTypeAndRole 取 input 项的 type 与 role；项可能是已解码的对象，也可能是预编码 JSON（网关合成的前缀项）。
func officialCodexInputItemTypeAndRole(item any) (itemType string, role string) {
	switch typed := item.(type) {
	case map[string]any:
		itemType, _ = typed["type"].(string)
		role, _ = typed["role"].(string)
	case json.RawMessage:
		itemType = gjson.GetBytes(typed, "type").String()
		role = gjson.GetBytes(typed, "role").String()
	}
	return strings.TrimSpace(itemType), strings.TrimSpace(role)
}

// officialCodexConfigurationUpdateInsertIndex 返回合成项应插入的下标，ok 为假表示不合成：
//   - input 里已有 configuration_update（客户端自己带了）、或有压缩项（压缩后的新窗口官方以当前选择重建基线、不追加 update）→ 否；
//   - 插入点是第一个“模型输出项”（助手消息、reasoning、各类工具调用……，即不属于用户侧输入的项）之前，没有输出项时是 input 末尾；
//   - 插入点之前至少要有一条用户消息，否则不合成。
func officialCodexConfigurationUpdateInsertIndex(input []any) (int, bool) {
	insertAt := len(input)
	foundOutput := false
	sawUser := false
	userBeforeOutput := false
	for index, item := range input {
		itemType, role := officialCodexInputItemTypeAndRole(item)
		switch itemType {
		case openAIConfigurationUpdateItemType, "compaction", "compaction_summary", "compaction_trigger":
			return 0, false
		}
		if foundOutput {
			continue
		}
		switch itemType {
		case "additional_tools", "function_call_output", "custom_tool_call_output", "tool_search_output":
			continue
		case "message":
			if role == "user" {
				sawUser = true
				continue
			}
			if role == "developer" || role == "system" {
				continue
			}
		}
		foundOutput = true
		insertAt = index
		userBeforeOutput = sawUser
	}
	if !foundOutput {
		userBeforeOutput = sawUser
	}
	if !userBeforeOutput {
		return 0, false
	}
	return insertAt, true
}

// synthesizeOfficialCodexConfigurationUpdate 在 HTTP 定型的最后一步为第三方入站合成 configuration_update，返回 payload 是否被改动。
// 必须在其余定型（基础指令安装、逐项 turn_id 改写、reasoning 默认值补齐）之后调用：回合与前缀 id 的派生都用不含该项的结构，
// 保证同一窗口各请求的派生值稳定；合成项用预编码 JSON，键序固定为官方序（type、reasoning）。
func synthesizeOfficialCodexConfigurationUpdate(
	payload map[string]any,
	contract *officialOpenAIHTTPBodyContract,
	carrier *profilecontract.BaseInstructionsSection,
	defaults officialOpenAIReasoningDefaults,
) bool {
	if !defaults.ConfigurationUpdateHTTPFirstTurn || !defaults.SupportsEffortUpdates || carrier == nil {
		return false
	}
	// 官方入站：入站带官方形态的基础指令消息。官方客户端自己决定窗口里有没有这一项，网关不替它补。
	if contract.hasInboundTaggedKind(carrier.ContentItemKind) {
		return false
	}
	reasoning, ok := payload["reasoning"].(map[string]any)
	if !ok {
		return false
	}
	effort, ok := reasoning["effort"].(string)
	if !ok {
		return false
	}
	effort = strings.TrimSpace(effort)
	if !officialCodexConfigurationUpdateKnownEffort(effort) {
		return false
	}
	input, ok := payload["input"].([]any)
	if !ok {
		return false
	}
	insertAt, ok := officialCodexConfigurationUpdateInsertIndex(input)
	if !ok {
		return false
	}
	rendered := json.RawMessage(`{"type":"` + openAIConfigurationUpdateItemType + `","reasoning":{"effort":"` + effort + `"}}`)
	rebuilt := make([]any, 0, len(input)+1)
	rebuilt = append(rebuilt, input[:insertAt]...)
	rebuilt = append(rebuilt, rendered)
	rebuilt = append(rebuilt, input[insertAt:]...)
	payload["input"] = rebuilt
	return true
}
