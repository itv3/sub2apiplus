package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// 独立 images 端点（images_generations／images_edits）的回合头。
//
// 官方 ext/image-generation 的 image_request_headers 会写两个头：
//   - x-codex-image-turn-id：生图工具调用所在回合的 sub_id（0.163 起就有，0.163 画像缺失）；
//   - x-codex-turn-metadata：当前回合 turn metadata 的 JSON 字符串（0.164 新增）。
//
// 两个头是否出站只由画像是否为该端点声明了对应槽位决定：旧画像没有槽位，本文件的逻辑一个
// 字节都不改，身份事实也不多生成（避免旧画像的 FinalizationToken 身份摘要漂移）。
// 两个槽位彼此独立，登记时可以只声明其中之一。

const (
	officialCodexImageTurnIDHeader  = "x-codex-image-turn-id"
	officialCodexTurnMetadataHeader = "x-codex-turn-metadata"
)

// officialCodexEndpointIsImages 判断端点是否是独立 images 端点。
func officialCodexEndpointIsImages(endpointID string) bool {
	return endpointID == officialCodexEndpointImagesGenerations ||
		endpointID == officialCodexEndpointImagesEdits
}

// officialCodexImagesHeaderDeclarations 读取 mode 对应画像里 images 端点是否声明了两个回合头槽位，
// 并返回画像版本（供 turn metadata 的 codex_version 使用）。mode 为空或画像不可解析时一律视为
// “未声明”，保持旧逻辑；正式请求在其他环节会因同一解析失败而失败关闭。
func officialCodexImagesHeaderDeclarations(mode, endpointID string) (imageTurnID, turnMetadata bool, version string) {
	if strings.TrimSpace(mode) == "" {
		return false, false, ""
	}
	profile, err := officialCodexExecutableProfileForMode(mode)
	if err != nil {
		return false, false, ""
	}
	endpoint, found := profile.Endpoint(endpointID)
	if !found {
		return false, false, ""
	}
	for _, slot := range endpoint.Headers {
		name := slot.WireName
		if name == "" {
			name = slot.Name
		}
		switch strings.ToLower(strings.TrimSpace(name)) {
		case officialCodexImageTurnIDHeader:
			imageTurnID = true
		case officialCodexTurnMetadataHeader:
			turnMetadata = true
		}
	}
	return imageTurnID, turnMetadata, profile.Version()
}

// ---------------------------------------------------------------------------
// x-codex-turn-metadata（images 工具请求形态）
//
// 官方 images 请求的 turn metadata 来自 current_meta_value_for_extension_request：与同一回合
// Responses 请求共用同一份回合状态，取值逐项相同（20 次实采全部一致），只是装配过程不同——
// 先按 CodexTurnMetadataPayload 声明序排好有值的键，再接 extra（按键名字典序，默认只有 model、
// reasoning_effort），然后依次 swap_remove agent_name、parent_turn_id、root_turn_id（serde_json 启用
// preserve_order 时 Map::remove 是换位删除：把末尾元素搬到被删位置），最后追加 codex_version。
// 所以键序不是声明序，而是这套机械过程的结果；这里按同一过程实现，不写死任何一种键序。
//
// 本网关的做法是“复用 Responses 回合事实，再按官方规则重排”，不另起一套事实：
//   - 入站已带 Responses 形态的 turn metadata（同一官方客户端回合）→ 取其中的共有键与取值；
//   - 否则 → 用与 Responses 兜底相同的基础键值（officialCodexFallbackTurnMetadataBaseValues
//     与画像 TurnMetadata 节），再补官方 CLI 默认路径的常量，以及画像 SessionDefaults 登记的默认会话模型
//     （model、reasoning_effort、node_repl_auto_review_required）。

// officialImagesTurnMetadataPayloadOrder 是 CodexTurnMetadataPayload 的字段声明序。
var officialImagesTurnMetadataPayloadOrder = []string{
	"installation_id", "session_id", "thread_id", "agent_name", "turn_id", "window_id", "window_number",
	"context_window_id", "request_kind", "history_initialization", "forked_from_thread_id",
	"forked_from_ordinal_exclusive", "parent_thread_id", "parent_turn_id", "root_turn_id", "subagent_kind",
	"thread_source", "turn_trigger", "sandbox", "sandbox_mode", "auto_review_enabled",
	"node_repl_auto_review_required", "node_repl_disabled", "workspaces", "tool_namespaces_info",
	"turn_started_at_unix_ms", "history_ingest_requested", "analytics_enabled", "compaction",
}

// officialImagesTurnMetadataResponsesOnlyKey 判断键是否 Responses 请求才有、images 工具请求里恒无值
// （扩展请求模板没有请求种类与窗口身份，也不带 analytics 与压缩信息）。用函数而不是包级 map，
// 避免官方出站层新增需要登记的包级 map。
func officialImagesTurnMetadataResponsesOnlyKey(key string) bool {
	switch key {
	case "installation_id", "window_id", "window_number", "context_window_id",
		"request_kind", "history_initialization", "history_ingest_requested",
		"analytics_enabled", "compaction", "tool_namespaces_info":
		return true
	default:
		return false
	}
}

// officialImagesTurnMetadataRemovedKeys 是官方按顺序 swap_remove 的三个键。
var officialImagesTurnMetadataRemovedKeys = []string{"agent_name", "parent_turn_id", "root_turn_id"}

// officialCodexImagesTurnStartedAtMs 取回合开始毫秒时间；包级变量只为让测试固定时间。
var officialCodexImagesTurnStartedAtMs = func() int64 { return time.Now().UnixMilli() }

// orderOfficialImagesTurnMetadataKeys 按官方装配过程给出 images 请求 turn metadata 的最终键序。
// keys 是除 codex_version 外将出现在最终 JSON 里的键；agent_name 与 root_turn_id 在官方里恒存在、
// 随后被删，所以函数内部把它们当作“删除前存在”处理，它们的存在会影响换位结果。
// userInputRequested 为真时在末尾追加 user_input_requested_during_turn（官方：触发过
// request_user_input 的回合）。
func orderOfficialImagesTurnMetadataKeys(keys []string, userInputRequested bool) []string {
	present := map[string]bool{"agent_name": true, "root_turn_id": true}
	for _, key := range keys {
		present[key] = true
	}
	structured := make(map[string]bool, len(officialImagesTurnMetadataPayloadOrder))
	ordered := make([]string, 0, len(present)+1)
	for _, key := range officialImagesTurnMetadataPayloadOrder {
		structured[key] = true
		if present[key] {
			ordered = append(ordered, key)
		}
	}
	extras := make([]string, 0, len(keys))
	for key := range present {
		if !structured[key] {
			extras = append(extras, key)
		}
	}
	sort.Strings(extras)
	ordered = append(ordered, extras...)
	for _, removed := range officialImagesTurnMetadataRemovedKeys {
		for index, key := range ordered {
			if key == removed {
				ordered[index] = ordered[len(ordered)-1]
				ordered = ordered[:len(ordered)-1]
				break
			}
		}
	}
	ordered = append(ordered, "codex_version")
	if userInputRequested {
		ordered = append(ordered, "user_input_requested_during_turn")
	}
	return ordered
}

// asciiJSON 把一段合法 JSON 压成紧凑形式，并把非 ASCII 字符转成 \uXXXX（官方 to_ascii_json_string）。
// 合法 JSON 里的非 ASCII 字符只会出现在字符串内，所以对整段逐字符转义是安全的。
func asciiJSON(raw []byte) ([]byte, error) {
	var compact bytes.Buffer
	if err := json.Compact(&compact, raw); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	for rest := compact.Bytes(); len(rest) > 0; {
		r, size := utf8.DecodeRune(rest)
		switch {
		case r < 0x80:
			_ = out.WriteByte(rest[0]) // bytes.Buffer 的写入恒返回 nil
		case r >= 0x10000:
			r -= 0x10000
			fmt.Fprintf(&out, "\\u%04x\\u%04x", 0xd800+(r>>10), 0xdc00+(r&0x3ff))
		default:
			fmt.Fprintf(&out, "\\u%04x", r)
		}
		rest = rest[size:]
	}
	return out.Bytes(), nil
}

// marshalOfficialImagesTurnMetadata 把回合事实（顶层键 → JSON 值）装配成官方 images 请求的
// x-codex-turn-metadata。Responses 专有键与 codex_version 会被丢弃，codex_version 恒由参数给出。
// session_id、thread_id、turn_id 必须有值。
func marshalOfficialImagesTurnMetadata(
	values map[string]json.RawMessage,
	codexVersion string,
) (string, error) {
	kept := make(map[string]json.RawMessage, len(values))
	keys := make([]string, 0, len(values))
	userInputRequested := false
	for key, value := range values {
		switch {
		case officialImagesTurnMetadataResponsesOnlyKey(key), key == "codex_version",
			key == "agent_name", key == "root_turn_id":
			continue
		case key == "user_input_requested_during_turn":
			userInputRequested = strings.TrimSpace(string(value)) == "true"
			continue
		}
		kept[key] = value
		keys = append(keys, key)
	}
	for _, required := range []string{"session_id", "thread_id", "turn_id"} {
		if strings.TrimSpace(string(kept[required])) == "" || string(kept[required]) == `""` {
			return "", fmt.Errorf("images turn metadata 缺少 %s", required)
		}
	}
	if strings.TrimSpace(codexVersion) != "" {
		version, err := json.Marshal(codexVersion)
		if err != nil {
			return "", err
		}
		kept["codex_version"] = version
	}
	var out bytes.Buffer
	_ = out.WriteByte('{') // bytes.Buffer 的写入恒返回 nil
	first := true
	for _, key := range orderOfficialImagesTurnMetadataKeys(keys, userInputRequested) {
		value, exists := kept[key]
		switch {
		case key == "codex_version" && !exists:
			continue
		case key == "user_input_requested_during_turn":
			value = json.RawMessage("true")
		case !exists:
			continue
		}
		encodedKey, err := asciiJSON(imagesTurnJSONString(key))
		if err != nil {
			return "", err
		}
		encodedValue, err := asciiJSON(value)
		if err != nil {
			return "", fmt.Errorf("images turn metadata 键 %s 的值不是合法 JSON：%w", key, err)
		}
		if !first {
			_ = out.WriteByte(',')
		}
		first = false
		_, _ = out.Write(encodedKey)
		_ = out.WriteByte(':')
		_, _ = out.Write(encodedValue)
	}
	_ = out.WriteByte('}')
	return out.String(), nil
}

func imagesTurnJSONString(value string) json.RawMessage {
	raw, _ := json.Marshal(value)
	return raw
}

// officialImagesTurnMetadataFromResponses 把同一回合的 Responses 形态 turn metadata 转成 images 请求形态：
// 保留共有键的原始取值字节，丢弃 Responses 专有键，再按官方过程重排。
func officialImagesTurnMetadataFromResponses(responsesMetadata, codexVersion string) (string, error) {
	var values map[string]json.RawMessage
	if err := json.Unmarshal([]byte(responsesMetadata), &values); err != nil {
		return "", fmt.Errorf("解析 Responses turn metadata：%w", err)
	}
	return marshalOfficialImagesTurnMetadata(values, codexVersion)
}

// officialImagesSynthesizedTurnMetadataValues 在入站没有回合事实时（非 Codex 客户端直接调图像接口），
// 从 Responses 兜底的基础键值出发，补官方 CLI 默认路径上的常量与会话模型取值。
//
// model、reasoning_effort、node_repl_auto_review_required 是会话模型的取值，不是 images 请求体里的图像模型
// （gpt-image-2）：取画像 TurnMetadata 节 SessionDefaults 登记的默认会话模型及其目录条目，三项来自同一条
// 目录记录，不在服务层另行硬编码。画像没有声明 SessionDefaults 时没有可信来源，model 与 reasoning_effort
// 不写，node_repl_auto_review_required 取 false（与未声明会话模型时的旧结果一致）。
// 入站带了同回合 Responses 事实时，这些键都从该事实取，不走这里。
func officialImagesSynthesizedTurnMetadataValues(
	facts *officialegress.CodexIdentityFacts,
	sessionID string,
	profileMode string,
	memoryGeneration bool,
) (map[string]json.RawMessage, error) {
	tmp := *facts
	tmp.SessionID, tmp.ThreadID = mustIdentityValue(sessionID), mustIdentityValue(sessionID)
	base := officialCodexFallbackTurnMetadataBaseValues(&tmp)
	nodeReplAutoReviewRequired := false
	if section := officialCodexOptionalSectionsForMode(profileMode).TurnMetadata; section != nil {
		extension := officialCodexTurnMetadataExtension{
			TurnTrigger: officialCodexTurnTrigger(facts.ProcessSurface.Value, facts.Subagent.Value, memoryGeneration),
		}
		if defaults := section.SessionDefaults; defaults != nil {
			extension.Model = defaults.Model
			extension.ReasoningEffort = defaults.ReasoningEffort
			nodeReplAutoReviewRequired = defaults.NodeReplAutoReviewRequired
		}
		if err := applyOfficialCodexTurnMetadataSection(base, section, extension); err != nil {
			return nil, err
		}
	}
	base["sandbox_mode"] = "read-only"
	base["auto_review_enabled"] = false
	base["node_repl_auto_review_required"] = nodeReplAutoReviewRequired
	base["node_repl_disabled"] = false
	// 回合开始时间与 Responses 同源：由 turn_id 内嵌毫秒加抽样间隔推出；turn_id 不是 UUIDv7 时才取当前时钟。
	if startedAt, ok := officialCodexTurnStartedAtMs(facts.TurnID.Value); ok {
		base["turn_started_at_unix_ms"] = startedAt
	} else {
		base["turn_started_at_unix_ms"] = officialCodexImagesTurnStartedAtMs()
	}
	values := make(map[string]json.RawMessage, len(base))
	for key, value := range base {
		raw, err := json.Marshal(value)
		if err != nil {
			return nil, err
		}
		values[key] = raw
	}
	return values, nil
}

func mustIdentityValue(value string) officialegress.CodexIdentityValue {
	return officialegress.CodexIdentityValue{
		Value: value, Source: officialegress.IdentitySourceInvocation, Lifecycle: officialegress.IdentityLifecycleSession,
	}
}

// completeOfficialCodexImagesTurnFacts 为 images 端点补齐画像声明的回合头所需的身份事实。
// 已由结构化身份或调用登记提供的事实原样保留，只补缺失的；已有的 Responses 形态 turn metadata
// 会被转成 images 请求形态。
func completeOfficialCodexImagesTurnFacts(
	facts *officialegress.CodexIdentityFacts,
	endpointID string,
	identitySeed string,
	fallback officialCodexFallbackTurnMetadataInput,
) error {
	if facts == nil {
		return errors.New("Codex 身份事实为空")
	}
	imageTurnID, turnMetadata, version := officialCodexImagesHeaderDeclarations(fallback.profileMode, endpointID)
	if !imageTurnID && !turnMetadata {
		return nil
	}
	if facts.TurnID.Value == "" && facts.TurnMetadata.Value != "" {
		// 入站回合事实已带 turn_id：两个头必须同源，取它。
		var inbound struct {
			TurnID string `json:"turn_id"`
		}
		if json.Unmarshal([]byte(facts.TurnMetadata.Value), &inbound) == nil && strings.TrimSpace(inbound.TurnID) != "" {
			turnID, err := officialegress.NewCodexIdentityValue(
				inbound.TurnID, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
			)
			if err != nil {
				return err
			}
			facts.TurnID = turnID
		}
	}
	if facts.TurnID.Value == "" {
		turnID, err := officialegress.NewCodexIdentityValue(
			officialCodexInvocationTurnID(identitySeed),
			officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleTurn,
		)
		if err != nil {
			return err
		}
		facts.TurnID = turnID
	}
	if !turnMetadata {
		return nil
	}
	var metadata string
	var err error
	if facts.TurnMetadata.Value != "" {
		metadata, err = officialImagesTurnMetadataFromResponses(facts.TurnMetadata.Value, version)
	} else {
		sessionID := facts.SessionID.Value
		if sessionID == "" {
			sessionID = officialCodexInvocationSessionID(identitySeed)
		}
		var values map[string]json.RawMessage
		values, err = officialImagesSynthesizedTurnMetadataValues(
			facts, sessionID, fallback.profileMode, fallback.memoryGeneration,
		)
		if err == nil {
			// turn_id 以事实为准（上面可能取了入站值），与 x-codex-image-turn-id 同源。
			values["turn_id"] = imagesTurnJSONString(facts.TurnID.Value)
			metadata, err = marshalOfficialImagesTurnMetadata(values, version)
		}
	}
	if err != nil {
		return err
	}
	facts.TurnMetadata, err = officialegress.NewCodexIdentityValue(
		metadata, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
	)
	return err
}
