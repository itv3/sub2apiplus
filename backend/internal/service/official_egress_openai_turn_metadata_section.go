package service

import (
	"errors"
	"fmt"
	"slices"
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
	officialCodexTurnMetadataKeyAnalyticsEnabled = "analytics_enabled"
	officialCodexTurnMetadataKeyModel            = "model"
	officialCodexTurnMetadataKeyReasoningEffort  = "reasoning_effort"
	officialCodexTurnMetadataKeyTurnTrigger      = "turn_trigger"
)

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

// officialOpenAIEffectiveReasoningEffort 返回请求体最终会携带的 reasoning.effort：显式值
// 优先（ultra 映射为 max，与 normalizeDerivedOfficialOpenAIReasoning 一致），缺席时取
// 模型默认值；两者都没有时返回空串。
func officialOpenAIEffectiveReasoningEffort(
	payload map[string]any,
	defaults officialOpenAIReasoningDefaults,
) string {
	if reasoning, ok := payload["reasoning"].(map[string]any); ok {
		if effort, ok := reasoning["effort"].(string); ok && strings.TrimSpace(effort) != "" {
			if strings.EqualFold(strings.TrimSpace(effort), "ultra") {
				return "max"
			}
			return effort
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
