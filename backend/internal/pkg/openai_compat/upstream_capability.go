// Package openai_compat 提供 OpenAI 协议族在不同上游间的能力差异判定工具。
//
// 背景：sub2api 的 OpenAI APIKey 账号通过 base_url 接入多种第三方 OpenAI 兼容上游
// （DeepSeek、Kimi、GLM、Qwen 等）。这些上游普遍只支持 /v1/chat/completions，
// 不存在 /v1/responses 端点。但网关历史代码无差别走 CC→Responses 转换并打到
// /v1/responses，导致兼容上游 404。
//
// 本包提供基于"账号探测标记"的能力判定，配合
// internal/service/openai_apikey_responses_probe.go 在创建/修改账号时一次性
// 探测并落标。
//
// 设计取舍：
//   - 不维护静态 host 白名单——避免新增厂商时必须改代码（讨论沉淀于
//     pensieve/short-term/knowledge/upstream-capability-detection-design-tradeoffs）
//   - 标记缺失时默认 true（即"走 Responses"），保持与重构前老代码完全一致的存量
//     账号行为（"现状即证据"原则；详见
//     pensieve/short-term/maxims/preserve-existing-runtime-behavior-when-replacing-logic-in-stateful-systems）
package openai_compat

// AccountResponsesSupport 描述账号上游对 OpenAI Responses API 的有效支持状态。
//
// 仅用于 platform=openai + type=apikey 的账号；其他账号类型不应调用本包判定。
type AccountResponsesSupport int

const (
	// ResponsesSupportUnknown 表示账号尚未完成能力探测（extra 字段缺失）。
	// 上游路由层应按"现状即证据"原则默认走 Responses，保持与重构前一致。
	ResponsesSupportUnknown AccountResponsesSupport = iota

	// ResponsesSupportYes 探测确认上游支持 /v1/responses。
	ResponsesSupportYes

	// ResponsesSupportNo 探测确认上游不支持 /v1/responses，应走
	// /v1/chat/completions 直转路径。
	ResponsesSupportNo
)

// ResponsesSupportMode 描述账号级 Responses API 路由覆盖模式。
type ResponsesSupportMode string

const (
	// ResponsesSupportModeAuto 表示跟随自动探测结果。
	ResponsesSupportModeAuto ResponsesSupportMode = "auto"

	// ResponsesSupportModeForceResponses 强制使用 /v1/responses。
	ResponsesSupportModeForceResponses ResponsesSupportMode = "force_responses"

	// ResponsesSupportModeForceChatCompletions 强制使用 /v1/chat/completions。
	ResponsesSupportModeForceChatCompletions ResponsesSupportMode = "force_chat_completions"
)

// ExtraKeyResponsesMode 是 accounts.extra JSON 中存储手动覆盖模式的键名。
// 值类型为 string：auto=跟随探测，force_responses=强制 Responses，
// force_chat_completions=强制 Chat Completions。
const ExtraKeyResponsesMode = "openai_responses_mode"

// ExtraKeyResponsesSupported 是 accounts.extra JSON 中存储自动探测结果的键名。
// 值类型为 bool：true=支持、false=不支持、键缺失=未探测。
const ExtraKeyResponsesSupported = "openai_responses_supported"

// ExtraKeyResponsesSupportedMimicCodexCLI 是 accounts.extra JSON 中存储
// OpenAI APIKey Codex mimic 形态探测结果的键名。值类型与
// ExtraKeyResponsesSupported 相同。阶段一的 mimic profile 固定走 Responses
// 伪装链路，所以该键当前只作为观测/诊断数据，避免污染普通 APIKey 探测结果。
const ExtraKeyResponsesSupportedMimicCodexCLI = "openai_responses_supported_mimic_codex_cli"

// NormalizeResponsesSupportMode 归一化账号级 Responses API 路由覆盖模式。
// 缺失或非法值按 auto 处理，以保持存量行为。
func NormalizeResponsesSupportMode(mode string) ResponsesSupportMode {
	switch ResponsesSupportMode(mode) {
	case ResponsesSupportModeForceResponses:
		return ResponsesSupportModeForceResponses
	case ResponsesSupportModeForceChatCompletions:
		return ResponsesSupportModeForceChatCompletions
	default:
		return ResponsesSupportModeAuto
	}
}

// ResolveResponsesSupport 从账号的 extra map 中读取手动覆盖模式与探测标记。
//
// 标记缺失或类型不匹配时返回 ResponsesSupportUnknown——调用方应按
// "未探测=保留旧行为=走 Responses" 处理（参见 ShouldUseResponsesAPI）。
func ResolveResponsesSupport(extra map[string]any) AccountResponsesSupport {
	return ResolveResponsesSupportForProfile(extra, false)
}

// ResolveResponsesSupportForProfile 从账号 extra 中读取指定 profile 的探测结果。
// mimicCodexCLI=true 时直接返回 ResponsesSupportYes，保证 mimic 账号固定走
// Responses 伪装链路，不受 force_chat_completions 或探测结果影响；
// false 时读取普通探测键与手动覆盖模式。
func ResolveResponsesSupportForProfile(extra map[string]any, mimicCodexCLI bool) AccountResponsesSupport {
	if mimicCodexCLI {
		// mimic 账号的阶段一核心语义是：所有入口统一收敛到 Responses 伪装链路。
		// 因此这里必须忽略 force_chat_completions / 探测 false，避免再掉回 raw Chat Completions。
		return ResponsesSupportYes
	}
	if extra == nil {
		return ResponsesSupportUnknown
	}
	if mode, ok := extra[ExtraKeyResponsesMode].(string); ok {
		switch NormalizeResponsesSupportMode(mode) {
		case ResponsesSupportModeForceResponses:
			return ResponsesSupportYes
		case ResponsesSupportModeForceChatCompletions:
			return ResponsesSupportNo
		}
	}
	key := ExtraKeyResponsesSupported
	if mimicCodexCLI {
		key = ExtraKeyResponsesSupportedMimicCodexCLI
	}
	v, ok := extra[key]
	if !ok {
		return ResponsesSupportUnknown
	}
	supported, ok := v.(bool)
	if !ok {
		return ResponsesSupportUnknown
	}
	if supported {
		return ResponsesSupportYes
	}
	return ResponsesSupportNo
}

// ShouldUseResponsesAPI 判断 OpenAI APIKey 账号的入站 /v1/chat/completions 请求
// 是否应走"CC→Responses 转换 + 上游 /v1/responses"路径。
//
// 返回 true 的两种情况：
//  1. 账号已探测确认支持 Responses
//  2. 账号未探测（标记缺失）——按"现状即证据"原则保留旧行为
//
// 仅当账号已探测且确认不支持时返回 false，此时调用方应走 CC 直转路径
// （详见 internal/service/openai_gateway_chat_completions_raw.go）。
func ShouldUseResponsesAPI(extra map[string]any) bool {
	return ShouldUseResponsesAPIForProfile(extra, false)
}

// ShouldUseResponsesAPIForProfile 判断指定 profile 下是否应走 Responses。
// mimicCodexCLI=true 时应始终返回 true。
func ShouldUseResponsesAPIForProfile(extra map[string]any, mimicCodexCLI bool) bool {
	return ResolveResponsesSupportForProfile(extra, mimicCodexCLI) != ResponsesSupportNo
}
