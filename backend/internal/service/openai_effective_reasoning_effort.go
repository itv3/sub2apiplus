package service

import (
	"bytes"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	"github.com/tidwall/gjson"
)

// 生效 effort（effective reasoning effort）。
//
// 较新版本的官方 Codex 客户端默认开启 reasoning_effort_override：会话内用户改 effort 时，客户端不改请求级
// reasoning.effort（它在一个上下文窗口内保持首次采样时钉下的基线，以保护上游前缀缓存），而是在 input 里追加
//
//	{"type":"configuration_update","reasoning":{"effort":"high"}}
//
// 上游按窗口里最后一条这样的项执行，所以请求体里“用户实际选择的 effort”不再等于请求级 reasoning.effort。
// 网关里凡是要回答“这次请求按什么 effort 跑”的地方（用量日志、渠道 reasoning_effort_multipliers 计费倍率、
// 首包超时、官方 turn metadata、分组 effort 上限策略）都必须读生效 effort：
//
//	生效 effort ＝ input 里最后一条 configuration_update.reasoning.effort；没有则取请求级 reasoning.effort。
//
// WS 增量帧（带 previous_response_id）的 input 只含新增项，之前帧里的 configuration_update 不在本帧里，所以 WS
// 连接上要用 openAIWindowEffortCarrier 记住最近一次的取值（见下）。HTTP 与 WS 整段重放帧的 input 是完整历史，
// 无状态读取即可。

// openAIConfigurationUpdateItemType 是官方客户端在 input 里记录会话内 effort 变化的项类型。
const openAIConfigurationUpdateItemType = "configuration_update"

// lastConfigurationUpdateEffort 返回 body.input 里最后一条 configuration_update 项的 reasoning.effort（已去首尾空白）。
// 没有这样的项、或各项的 effort 都不是非空字符串时 ok 为 false。input 不是数组（字符串简写、缺省）时恒为 false。
//
// 先用一次子串检查挡掉绝大多数请求（官方客户端之外没人会发这种项），避免为每个请求遍历大 input。
func lastConfigurationUpdateEffort(body []byte) (effort string, ok bool) {
	if len(body) == 0 || !bytes.Contains(body, []byte(openAIConfigurationUpdateItemType)) {
		return "", false
	}
	return lastConfigurationUpdateEffortInInput(gjson.GetBytes(body, "input"))
}

// lastConfigurationUpdateEffortInInput 在 input 数组（gjson 结果）里找最后一条 configuration_update 项的 reasoning.effort。
func lastConfigurationUpdateEffortInInput(input gjson.Result) (effort string, ok bool) {
	if !input.IsArray() {
		return "", false
	}
	input.ForEach(func(_, item gjson.Result) bool {
		if !item.IsObject() || strings.TrimSpace(item.Get("type").String()) != openAIConfigurationUpdateItemType {
			return true
		}
		value := item.Get("reasoning.effort")
		if value.Type != gjson.String {
			return true
		}
		if trimmed := strings.TrimSpace(value.String()); trimmed != "" {
			// 不提前返回：要的是最后一条。
			effort, ok = trimmed, true
		}
		return true
	})
	return effort, ok
}

// lastConfigurationUpdateEffortFromInput 是 lastConfigurationUpdateEffort 在已解码 input 上的版本（从后往前找，命中即返回）。
func lastConfigurationUpdateEffortFromInput(input []any) (string, bool) {
	for index := len(input) - 1; index >= 0; index-- {
		// 网关合成的项（官方出站定型里的 configuration_update、基础指令消息）是预编码 JSON。
		if raw, isRaw := input[index].(json.RawMessage); isRaw {
			if !bytes.Contains(raw, []byte(openAIConfigurationUpdateItemType)) {
				continue
			}
			if effort, ok := lastConfigurationUpdateEffortInInput(gjson.Parse("[" + string(raw) + "]")); ok {
				return effort, true
			}
			continue
		}
		item, valid := input[index].(map[string]any)
		if !valid {
			continue
		}
		if itemType, _ := item["type"].(string); strings.TrimSpace(itemType) != openAIConfigurationUpdateItemType {
			continue
		}
		reasoning, valid := item["reasoning"].(map[string]any)
		if !valid {
			continue
		}
		if effort, isString := reasoning["effort"].(string); isString && strings.TrimSpace(effort) != "" {
			return strings.TrimSpace(effort), true
		}
	}
	return "", false
}

// lastConfigurationUpdateEffortFromReqBody 是 lastConfigurationUpdateEffort 在已解码请求体 map 上的版本。
func lastConfigurationUpdateEffortFromReqBody(reqBody map[string]any) (string, bool) {
	if reqBody == nil {
		return "", false
	}
	input, valid := reqBody["input"].([]any)
	if !valid {
		return "", false
	}
	return lastConfigurationUpdateEffortFromInput(input)
}

// requestLevelReasoningEffortRaw 是请求级 effort 原文：reasoning.effort，其次是部分客户端使用的扁平字段 reasoning_effort。
func requestLevelReasoningEffortRaw(body []byte) string {
	raw := strings.TrimSpace(gjson.GetBytes(body, "reasoning.effort").String())
	if raw == "" {
		raw = strings.TrimSpace(gjson.GetBytes(body, "reasoning_effort").String())
	}
	return raw
}

// effectiveReasoningEffortRaw 返回请求体的生效 effort 原文（未做档位归一化）：
// 最后一条 configuration_update 优先，其次请求级 reasoning.effort，再次扁平 reasoning_effort。无则返回空串。
func effectiveReasoningEffortRaw(body []byte) string {
	if effort, ok := lastConfigurationUpdateEffort(body); ok {
		return effort
	}
	return requestLevelReasoningEffortRaw(body)
}

// effectiveReasoningEffortRawWithCarry 同 effectiveReasoningEffortRaw，但在 body 自己没有 configuration_update 时先用
// carried（WS 增量帧沿用的之前帧里的取值，见 openAIWindowEffortCarrier）。carried 为空时与 effectiveReasoningEffortRaw 相同。
func effectiveReasoningEffortRawWithCarry(body []byte, carried string) string {
	if effort, ok := lastConfigurationUpdateEffort(body); ok {
		return effort
	}
	if carried != "" {
		return carried
	}
	return requestLevelReasoningEffortRaw(body)
}

// requestedReasoningEffortRawWithCarry 是“客户端请求的 effort”原文的同类版本：
// update、carried、请求级 reasoning.effort／reasoning_effort／output_config.effort 依次优先。
func requestedReasoningEffortRawWithCarry(body []byte, carried string) string {
	if effort, ok := lastConfigurationUpdateEffort(body); ok {
		return effort
	}
	if carried != "" {
		return carried
	}
	return requestLevelRequestedReasoningEffortRaw(body)
}

// openAIWindowEffortCarrier 记住 WS 连接上“窗口内最近一次 configuration_update 的 effort”。
//
// 官方客户端在 WS 上用 previous_response_id 续链时只发新增项：用户在第 N 帧改了 effort（该帧 input 里带 update），
// 第 N+1、N+2 帧不再带它，但生效 effort 仍是那次改的值。无状态读取会让这些帧回落到被钉死的基线，把计费倍率、用量日志
// 和首包超时都算错（升档后少算）。advance 按“客户端帧”逐帧推进状态（每个会发往上游的客户端帧恰好调用一次）：
//   - 帧的 input 里有 update：记住最后一条，本帧自己带着取值，无需沿用（返回空串）；
//   - 没有 update、帧带 previous_response_id（增量续链）且之前记住过：返回记住的值，调用方用 *WithCarry 读取；
//   - 其余（首帧、预热帧、换模型或压缩后的整段重放）：新的基线，清掉记录，返回空串。
//
// 必须用客户端发来的帧判断“是否增量续链”：网关为整段重放补预热后会给出站帧加上 previous_response_id，
// 那是网关链出来的，不代表窗口延续。
//
// 不是并发安全的：一条连接的帧由同一个 goroutine 顺序处理。同一连接上“策略之前（请求 effort）”与“策略之后（生效 effort）”
// 的取值各用一个实例。
type openAIWindowEffortCarrier struct {
	carried string
}

func (c *openAIWindowEffortCarrier) advance(clientFrame []byte) (carried string) {
	if c == nil {
		return ""
	}
	// 预热帧（generate=false）不生成内容、input 为空，既不带 update 也不代表窗口延续，不动记录。
	if generate := gjson.GetBytes(clientFrame, "generate"); generate.Type == gjson.False {
		return ""
	}
	if effort, ok := lastConfigurationUpdateEffort(clientFrame); ok {
		c.carried = effort
		return ""
	}
	if c.carried != "" && strings.TrimSpace(gjson.GetBytes(clientFrame, "previous_response_id").String()) != "" {
		return c.carried
	}
	c.carried = ""
	return ""
}

// configurationUpdateEffortPaths 返回 body.input 里每条 configuration_update 项的 effort 路径（"input.<下标>.reasoning.effort"），
// 供分组 effort 策略逐项改写。只收 effort 是字符串的项；没有这种项时返回 nil。
func configurationUpdateEffortPaths(body []byte) []string {
	if len(body) == 0 || !bytes.Contains(body, []byte(openAIConfigurationUpdateItemType)) {
		return nil
	}
	input := gjson.GetBytes(body, "input")
	if !input.IsArray() {
		return nil
	}
	var paths []string
	input.ForEach(func(index, item gjson.Result) bool {
		if item.IsObject() &&
			strings.TrimSpace(item.Get("type").String()) == openAIConfigurationUpdateItemType &&
			item.Get("reasoning.effort").Type == gjson.String {
			paths = append(paths, "input."+strconv.FormatInt(index.Int(), 10)+".reasoning.effort")
		}
		return true
	})
	return paths
}

// foldConfigurationUpdateEffortIntoResponsesRequest 把 input 里最后一条 configuration_update 的 effort 折进请求级
// reasoning.effort，返回是否改动。
//
// 用在把 Responses 请求转换成 Chat Completions／Anthropic 等协议的路径上：这些上游不认识 configuration_update，转换器会丢弃
// 这种项，不折叠的话用户在会话内改的档位会被窗口基线盖掉；折叠之后转换器、用量日志与计费读到同一个生效 effort。
// 原生 Responses 上游（含官方 OAuth）不折叠，configuration_update 原样透传，上游自己按它执行。
func foldConfigurationUpdateEffortIntoResponsesRequest(req *apicompat.ResponsesRequest) bool {
	if req == nil || len(req.Input) == 0 || !bytes.Contains(req.Input, []byte(openAIConfigurationUpdateItemType)) {
		return false
	}
	effort, ok := lastConfigurationUpdateEffortInInput(gjson.ParseBytes(req.Input))
	if !ok {
		return false
	}
	if req.Reasoning == nil {
		req.Reasoning = &apicompat.ResponsesReasoning{}
	}
	if req.Reasoning.Effort == effort {
		return false
	}
	req.Reasoning.Effort = effort
	return true
}
