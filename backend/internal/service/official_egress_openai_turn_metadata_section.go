package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 本文件按画像 TurnMetadata 可选节扩展 x-codex-turn-metadata。
//
// 画像没有该节时所有函数都是恒等操作，turn metadata 的键集、取值与字节保持旧逻辑；
// 画像声明该节时，只追加节 Keys 中列出、且网关能给出可信取值的目标版本新键，并按节
// 校验压缩元数据的 implementation／phase 闭集。

// turn metadata 目标版本新键。
const (
	officialCodexTurnMetadataKeyAnalyticsEnabled      = "analytics_enabled"
	officialCodexTurnMetadataKeyHistoryInitialization = "history_initialization"
	officialCodexTurnMetadataKeyModel                 = "model"
	officialCodexTurnMetadataKeyReasoningEffort       = "reasoning_effort"
	officialCodexTurnMetadataKeyTurnTrigger           = "turn_trigger"
)

// history_initialization 描述会话历史是怎样建立的，是运行时级属性：同一线程的所有 Responses 请求
// （含 WebSocket 预热握手与预热帧、压缩请求）取同一个值，压缩之后也不变。官方客户端恒带这个键，
// 缺失本身就可分辨，所以画像声明该键时恒写入。网关不跟踪入站会话的历史来源，默认给新会话的取值；
// 扩展里显式给出的值必须落在官方取值闭集内，留给将来受限透传使用。取值与版本无关。
const officialCodexHistoryInitializationNew = "new"

var officialCodexHistoryInitializationValues = []string{
	officialCodexHistoryInitializationNew, "cleared", "cold_resume", "warm_fork", "cold_fork", "supplied_history",
}

// officialCodexHistoryInitialization 返回网关默认给出的 history_initialization：新会话。
func officialCodexHistoryInitialization() string {
	return officialCodexHistoryInitializationNew
}

// validateOfficialCodexHistoryInitialization 要求取值落在官方取值闭集内。
func validateOfficialCodexHistoryInitialization(value string) error {
	if !slices.Contains(officialCodexHistoryInitializationValues, value) {
		return fmt.Errorf("turn metadata history_initialization 不在官方取值闭集：%q", value)
	}
	return nil
}

// turn_trigger 取值取自官方客户端源码的常量：exec 入口为 exec，TUI 用户输入为 user，
// guardian 审阅会话为 guardian_review，内部记忆合并为 memory_consolidation；普通子代理
// 继承父轮的触发来源（即入口取值）。这些取值与版本无关，只在画像声明该键时写入。
const (
	officialCodexTurnTriggerExec                = "exec"
	officialCodexTurnTriggerUser                = "user"
	officialCodexTurnTriggerGuardianReview      = "guardian_review"
	officialCodexTurnTriggerMemoryConsolidation = "memory_consolidation"
)

// officialCodexTurnMetadataExtension 是 turn metadata 新键的取值输入。空串表示该请求
// 没有可信取值，对应键不写入（与官方 Option 为 None 时省略一致）。
type officialCodexTurnMetadataExtension struct {
	// Model 是本次请求体的 model（上游模型名）。
	Model string
	// ReasoningEffort 是本次请求体最终的 reasoning.effort。
	ReasoningEffort string
	// TurnTrigger 是本轮触发来源；prewarm 等不属于某一轮的请求留空。
	TurnTrigger string
	// HistoryInitialization 是会话历史的建立方式。与上面“没有可信取值就省略”的键不同：留空取默认的新会话，
	// 画像声明该键时恒写入；非空时必须落在官方取值闭集内。
	HistoryInitialization string
	// WindowNumber／ContextWindowID 是上下文窗口身份（画像 WindowTracking）；为 nil／空串表示不写对应键。
	WindowNumber    *int
	ContextWindowID string
	// AgentName／RootTurnID 是根线程的代理路径与根回合 id（画像 RootThreadKeys）；空串表示不写对应键。
	AgentName  string
	RootTurnID string
}

// officialCodexTurnTrigger 按入口与会话种类给出 turn_trigger 取值；无法确定时返回空串。
func officialCodexTurnTrigger(surfaceID string, subagent string, memoryGeneration bool) string {
	switch {
	case memoryGeneration:
		return officialCodexTurnTriggerMemoryConsolidation
	case strings.TrimSpace(subagent) == officialCodexGuardianSubagentValue:
		return officialCodexTurnTriggerGuardianReview
	}
	switch strings.TrimSpace(surfaceID) {
	case officialCodexSurfaceExec:
		return officialCodexTurnTriggerExec
	case officialCodexSurfaceTUI:
		return officialCodexTurnTriggerUser
	default:
		return ""
	}
}

// officialOpenAIEffectiveReasoningEffort 返回本次请求的生效 effort，写入 turn metadata 的 reasoning_effort：
// input 里最后一条 configuration_update 优先（较新官方客户端会话内改 effort 的载体；官方 ExecutionMetadata 记的是
// 用户当前选择，而请求级 reasoning.effort 此时被钉在窗口基线上），其次请求体显式值（ultra 映射为 max，与
// normalizeDerivedOfficialOpenAIReasoning 一致），缺席时取模型默认值；都没有时返回空串。
// WS 增量帧只含新增项，之前帧里的 update 由调用方经 officialOpenAIWSDerivedState.windowReasoningEffortForFrame 带入。
func officialOpenAIEffectiveReasoningEffort(
	payload map[string]any,
	defaults officialOpenAIReasoningDefaults,
) string {
	if effort, ok := lastConfigurationUpdateEffortFromReqBody(payload); ok {
		if strings.EqualFold(effort, "ultra") {
			return "max"
		}
		return effort
	}
	if reasoning, ok := payload["reasoning"].(map[string]any); ok {
		if effort, ok := reasoning["effort"].(string); ok && strings.TrimSpace(effort) != "" {
			if strings.EqualFold(strings.TrimSpace(effort), "ultra") {
				return "max"
			}
			return effort
		}
		// 自定义数字档位在请求体里可能是 JSON 整数，turn metadata 仍按档位的字符串形态记录
		// （官方 ExecutionMetadata 写 reasoning_effort.to_string()），与请求体取同一个值。
		if effort, ok := reasoning["effort"].(json.Number); ok {
			if parsed, err := strconv.ParseUint(string(effort), 10, 64); err == nil {
				return strconv.FormatUint(parsed, 10)
			}
		}
	}
	effort := strings.TrimSpace(defaults.Effort)
	if effort == "" {
		return ""
	}
	if strings.EqualFold(effort, "ultra") {
		return "max"
	}
	return strings.ToLower(effort)
}

// applyOfficialCodexTurnMetadataSection 按画像节向 turn metadata 键值表追加新键，并校验
// 压缩元数据闭集。section 为 nil 时不做任何改动。
func applyOfficialCodexTurnMetadataSection(
	values map[string]any,
	section *profilecontract.TurnMetadataSection,
	extension officialCodexTurnMetadataExtension,
) error {
	if section == nil {
		return nil
	}
	if values == nil {
		return errors.New("turn metadata 键值表为空")
	}
	declared := func(key string) bool { return slices.Contains(section.Keys, key) }
	if declared(officialCodexTurnMetadataKeyAnalyticsEnabled) {
		// 按官方取证值固定，不透传下游入站的取值。
		values[officialCodexTurnMetadataKeyAnalyticsEnabled] = section.AnalyticsEnabled
	}
	if declared(officialCodexTurnMetadataKeyHistoryInitialization) {
		value := strings.TrimSpace(extension.HistoryInitialization)
		if value == "" {
			value = officialCodexHistoryInitialization()
		}
		if err := validateOfficialCodexHistoryInitialization(value); err != nil {
			return err
		}
		values[officialCodexTurnMetadataKeyHistoryInitialization] = value
	}
	if model := strings.TrimSpace(extension.Model); model != "" && declared(officialCodexTurnMetadataKeyModel) {
		values[officialCodexTurnMetadataKeyModel] = model
	}
	if effort := strings.TrimSpace(extension.ReasoningEffort); effort != "" &&
		declared(officialCodexTurnMetadataKeyReasoningEffort) {
		values[officialCodexTurnMetadataKeyReasoningEffort] = effort
	}
	if trigger := strings.TrimSpace(extension.TurnTrigger); trigger != "" &&
		declared(officialCodexTurnMetadataKeyTurnTrigger) {
		values[officialCodexTurnMetadataKeyTurnTrigger] = trigger
	}
	if section.RootThreadKeys {
		if name := strings.TrimSpace(extension.AgentName); name != "" && declared("agent_name") {
			values["agent_name"] = name
		}
		if id := strings.TrimSpace(extension.RootTurnID); id != "" && declared("root_turn_id") {
			values["root_turn_id"] = id
		}
	}
	if section.WindowTracking {
		if extension.WindowNumber != nil && declared("window_number") {
			values["window_number"] = *extension.WindowNumber
		}
		if id := strings.TrimSpace(extension.ContextWindowID); id != "" && declared("context_window_id") {
			values["context_window_id"] = id
		}
	}
	return validateOfficialCodexTurnMetadataCompaction(values["compaction"], section)
}

// validateOfficialCodexTurnMetadataCompaction 要求网关生成的压缩元数据落在画像声明的
// implementation／phase 闭集内（例如目标版本已删除 responses_compact）；不在闭集内说明
// 画像与执行层不一致，失败关闭。
func validateOfficialCodexTurnMetadataCompaction(
	value any,
	section *profilecontract.TurnMetadataSection,
) error {
	if value == nil || section == nil {
		return nil
	}
	implementation, phase := "", ""
	switch typed := value.(type) {
	case officialOpenAICompactionMetadata:
		implementation, phase = typed.Implementation, typed.Phase
	case map[string]any:
		implementation, _ = typed["implementation"].(string)
		phase, _ = typed["phase"].(string)
	default:
		return fmt.Errorf("turn metadata compaction 形态非法：%T", value)
	}
	if !slices.Contains(section.CompactionImplementations, implementation) {
		return fmt.Errorf("turn metadata compaction.implementation 不在画像闭集：%q", implementation)
	}
	if !slices.Contains(section.CompactionPhases, phase) {
		return fmt.Errorf("turn metadata compaction.phase 不在画像闭集：%q", phase)
	}
	return nil
}

// extendOfficialOpenAITurnMetadataJSON 在已序列化的 turn metadata 上应用画像节。
// section 为 nil 时原样返回；否则保留原有键的原始字节与相对顺序，新键按官方字段序插入。
func extendOfficialOpenAITurnMetadataJSON(
	raw string,
	section *profilecontract.TurnMetadataSection,
	extension officialCodexTurnMetadataExtension,
) (string, error) {
	if section == nil || strings.TrimSpace(raw) == "" {
		return raw, nil
	}
	values, err := decodeOfficialJSONObjectUseNumber([]byte(raw))
	if err != nil {
		return "", fmt.Errorf("解析 turn metadata：%w", err)
	}
	if err := applyOfficialCodexTurnMetadataSection(values, section, extension); err != nil {
		return "", err
	}
	encoded, err := marshalOfficialOrderedJSONObjectPreservingRaw(
		values, officialOpenAITurnMetadataFieldOrder, []byte(raw),
	)
	if err != nil {
		return "", fmt.Errorf("编码 turn metadata：%w", err)
	}
	return string(encoded), nil
}
