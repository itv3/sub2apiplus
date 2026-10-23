package service

// codex_third_party_profiles.go 负责「Codex 三方模型启用」的共享能力档案：
//
//   - 档案数据放在 go:embed 的 codex_third_party_profiles.json，按最终上游型号与协议契约匹配，
//     不按站点域名匹配；相同型号与协议的不同站点复用同一份档案。
//   - 运行时按公开名生成完整 ModelInfo 条目（整对象生成，不经有限字段结构体往返）。
//   - 每份档案固定指令模板的 sha256，运行时模板与档案不一致就停发该条目并给出原因。
//   - 只输出客户端 ModelInfo 实际存在的字段；已知不属于 ModelInfo 的旧键在加载时直接拒绝。

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

//go:embed codex_third_party_profiles.json
var codexThirdPartyProfilesJSON []byte

// codexThirdPartyDeepSeekOfficialModelsJSON 是 DeepSeek 官方 Codex 教程里的 models.json 原文（逐字保存）。
// 档案用 official_entry 指向其中的条目：网关下发的条目就是官方条目本身，只替换 slug 与 display_name
// （对外名以上游叫法为准），其余逐键逐值保持官方写法，官方没有的键也不会被补出来。
//
//go:embed codex_third_party_deepseek_official_models.json
var codexThirdPartyDeepSeekOfficialModelsJSON []byte

const (
	// CodexThirdPartyProtocolNative 表示上游原生 Responses 通路。
	CodexThirdPartyProtocolNative = "responses_native"
	// CodexThirdPartyProtocolViaChat 表示经网关 Responses 到 Chat Completions 转换的通路。
	CodexThirdPartyProtocolViaChat = "responses_via_chat"
)

// codexThirdPartyForbiddenModelInfoKeys 是旧版内置目录里出现、但当前客户端 ModelInfo 并不存在的键。
// 写入这些键没有效果，还会让人误以为已经生效，所以档案里一律不允许。
var codexThirdPartyForbiddenModelInfoKeys = []string{
	"prefer_websockets",
	"supports_parallel_tool_calls",
	"instructions_variables",
	"supports_reasoning_summaries",
	"base_instructions",
	"multi_agent_reasoning_effort",
}

// codexThirdPartyRequiredModelInfoKeys 是客户端解码必需的字段（缺任一项整份目录解码失败）。
// slug、display_name、model_messages 由运行时生成，不在档案里。
var codexThirdPartyRequiredModelInfoKeys = []string{
	"supported_reasoning_levels",
	"shell_type",
	"visibility",
	"supported_in_api",
	"priority",
	"support_verbosity",
	"truncation_policy",
	"experimental_supported_tools",
}

type codexThirdPartyProfileFile struct {
	SchemaVersion  int                         `json:"schema_version"`
	ClientContract string                      `json:"client_contract"`
	CheckedAt      string                      `json:"checked_at"`
	Notes          string                      `json:"notes"`
	Profiles       []codexThirdPartyProfileRaw `json:"profiles"`
}

type codexThirdPartyProfileRaw struct {
	ID                         string   `json:"id"`
	UpstreamModels             []string `json:"upstream_models"`
	Protocols                  []string `json:"protocols"`
	Sources                    []string `json:"sources"`
	CheckedAt                  string   `json:"checked_at"`
	Notes                      string   `json:"notes"`
	InstructionsTemplateSHA256 string   `json:"instructions_template_sha256"`
	// OfficialEntry 非空表示条目取自嵌入的官方 models.json 中该 slug 的条目（整体照搬），此时不得再写 model_info。
	OfficialEntry string                     `json:"official_entry,omitempty"`
	ModelInfo     map[string]json.RawMessage `json:"model_info"`
}

// codexThirdPartyProfile 是加载校验后的能力档案。
type codexThirdPartyProfile struct {
	ID                         string
	UpstreamModels             []string
	Protocols                  []string
	Sources                    []string
	CheckedAt                  string
	InstructionsTemplateSHA256 string
	ModelInfo                  map[string]json.RawMessage
	// Official 为 true 表示 ModelInfo 是官方条目的完整内容（含官方自带的 model_messages 与指令文本）。
	Official bool
	// Digest 是整份档案内容的 sha256，参与条目指纹。
	Digest string
}

// codexThirdPartyProfileSet 是已加载的全部档案。
type codexThirdPartyProfileSet struct {
	ClientContract string
	profiles       []*codexThirdPartyProfile
	// Digest 是全部档案的摘要，参与输入指纹。
	Digest string
}

// loadCodexThirdPartyProfiles 解析并校验嵌入的档案文件。
func loadCodexThirdPartyProfiles(raw []byte) (*codexThirdPartyProfileSet, error) {
	var file codexThirdPartyProfileFile
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&file); err != nil {
		return nil, fmt.Errorf("codex third party profiles: decode: %w", err)
	}
	if file.SchemaVersion != 1 {
		return nil, fmt.Errorf("codex third party profiles: unsupported schema_version %d", file.SchemaVersion)
	}
	if strings.TrimSpace(file.ClientContract) == "" {
		return nil, fmt.Errorf("codex third party profiles: client_contract is required")
	}
	officialEntries, err := loadCodexThirdPartyOfficialEntries(codexThirdPartyDeepSeekOfficialModelsJSON)
	if err != nil {
		return nil, err
	}
	set := &codexThirdPartyProfileSet{ClientContract: file.ClientContract}
	seenID := map[string]struct{}{}
	seenModel := map[string]string{}
	for _, item := range file.Profiles {
		if strings.TrimSpace(item.ID) == "" {
			return nil, fmt.Errorf("codex third party profiles: empty profile id")
		}
		if _, dup := seenID[item.ID]; dup {
			return nil, fmt.Errorf("codex third party profiles: duplicate profile id %q", item.ID)
		}
		seenID[item.ID] = struct{}{}
		if len(item.UpstreamModels) == 0 {
			return nil, fmt.Errorf("codex third party profiles: %s has no upstream_models", item.ID)
		}
		for _, model := range item.UpstreamModels {
			key := codexThirdPartyProfileKey(model)
			if key == "" {
				return nil, fmt.Errorf("codex third party profiles: %s has empty upstream model", item.ID)
			}
			if owner, dup := seenModel[key]; dup {
				return nil, fmt.Errorf("codex third party profiles: model %q claimed by %s and %s", model, owner, item.ID)
			}
			seenModel[key] = item.ID
		}
		if len(item.Protocols) == 0 {
			return nil, fmt.Errorf("codex third party profiles: %s has no protocols", item.ID)
		}
		for _, protocol := range item.Protocols {
			if protocol != CodexThirdPartyProtocolNative && protocol != CodexThirdPartyProtocolViaChat {
				return nil, fmt.Errorf("codex third party profiles: %s has unknown protocol %q", item.ID, protocol)
			}
		}
		if len(item.Sources) == 0 || strings.TrimSpace(item.CheckedAt) == "" {
			return nil, fmt.Errorf("codex third party profiles: %s must record sources and checked_at", item.ID)
		}
		if len(item.InstructionsTemplateSHA256) != sha256.Size*2 {
			return nil, fmt.Errorf("codex third party profiles: %s has no pinned instructions template digest", item.ID)
		}
		modelInfo := item.ModelInfo
		official := strings.TrimSpace(item.OfficialEntry) != ""
		var officialRaw []byte
		if official {
			// 官方条目整体照搬：官方写了的键（包括客户端不读的旧键）都保持原样，所以不做旧键检查，
			// 但档案里不得再写 model_info，避免和官方条目混在一起。
			if len(item.ModelInfo) > 0 {
				return nil, fmt.Errorf("codex third party profiles: %s uses official_entry and must not write model_info", item.ID)
			}
			entry, ok := officialEntries[item.OfficialEntry]
			if !ok {
				return nil, fmt.Errorf("codex third party profiles: %s official entry %q not found", item.ID, item.OfficialEntry)
			}
			modelInfo = entry
			officialRaw, err = json.Marshal(entry)
			if err != nil {
				return nil, err
			}
			template, templateErr := codexThirdPartyOfficialTemplate(entry)
			if templateErr != nil {
				return nil, fmt.Errorf("codex third party profiles: %s: %w", item.ID, templateErr)
			}
			sum := sha256.Sum256([]byte(template))
			if hex.EncodeToString(sum[:]) != item.InstructionsTemplateSHA256 {
				return nil, fmt.Errorf("codex third party profiles: %s official instructions template does not match the pinned digest", item.ID)
			}
		} else {
			for _, key := range codexThirdPartyForbiddenModelInfoKeys {
				if _, exists := modelInfo[key]; exists {
					return nil, fmt.Errorf("codex third party profiles: %s must not contain %q", item.ID, key)
				}
			}
			for _, key := range []string{"slug", "display_name", "model_messages"} {
				if _, exists := modelInfo[key]; exists {
					return nil, fmt.Errorf("codex third party profiles: %s must not fix %q, it is generated at runtime", item.ID, key)
				}
			}
		}
		for _, key := range codexThirdPartyRequiredModelInfoKeys {
			if _, exists := modelInfo[key]; !exists {
				return nil, fmt.Errorf("codex third party profiles: %s misses required field %q", item.ID, key)
			}
		}
		canonical, err := json.Marshal(item)
		if err != nil {
			return nil, err
		}
		canonical = append(canonical, officialRaw...)
		sum := sha256.Sum256(canonical)
		set.profiles = append(set.profiles, &codexThirdPartyProfile{
			ID:                         item.ID,
			UpstreamModels:             append([]string(nil), item.UpstreamModels...),
			Protocols:                  append([]string(nil), item.Protocols...),
			Sources:                    append([]string(nil), item.Sources...),
			CheckedAt:                  item.CheckedAt,
			InstructionsTemplateSHA256: item.InstructionsTemplateSHA256,
			ModelInfo:                  modelInfo,
			Official:                   official,
			Digest:                     hex.EncodeToString(sum[:]),
		})
	}
	sort.Slice(set.profiles, func(i, j int) bool { return set.profiles[i].ID < set.profiles[j].ID })
	all := sha256.New()
	_, _ = all.Write([]byte(set.ClientContract))
	for _, p := range set.profiles {
		_, _ = all.Write([]byte{0})
		_, _ = all.Write([]byte(p.Digest))
	}
	set.Digest = hex.EncodeToString(all.Sum(nil))
	return set, nil
}

// codexThirdPartyProfileKey 是能力档案的匹配键：取最后一个 `/` 之后的部分并转小写，
// 与仓库「带前缀模型定价回退」同一规则，例如 `moonshotai/Kimi-K3` 与 `kimi-k3` 等价。
func codexThirdPartyProfileKey(model string) string {
	return strings.ToLower(codexProviderQualifiedModelID(model))
}

// match 按最终上游型号查找档案。比较前去掉厂商前缀并忽略大小写与首尾空白。
func (s *codexThirdPartyProfileSet) match(upstreamModel string) *codexThirdPartyProfile {
	if s == nil {
		return nil
	}
	key := codexThirdPartyProfileKey(upstreamModel)
	if key == "" {
		return nil
	}
	for _, p := range s.profiles {
		for _, model := range p.UpstreamModels {
			if codexThirdPartyProfileKey(model) == key {
				return p
			}
		}
	}
	return nil
}

// matchAll 为一组最终上游型号（来自不同承接账号）找出同一份档案。
// 每个型号规范化后都必须命中档案，且命中的是同一份；多个账号映射到不同的原始名、
// 规范化后落在同一档案上不算冲突。找不到时返回 nil 与面向管理员的原因。
func (s *codexThirdPartyProfileSet) matchAll(upstreamModels []string) (*codexThirdPartyProfile, string) {
	var found *codexThirdPartyProfile
	for _, model := range upstreamModels {
		profile := s.match(model)
		if profile == nil {
			return nil, fmt.Sprintf("上游型号 %s 暂无能力档案（待补能力）", model)
		}
		if found != nil && found != profile {
			return nil, "多个账号把该模型映射到不同的上游型号（" + strings.Join(upstreamModels, "、") + "），无法套用单一能力档案"
		}
		found = profile
	}
	if found == nil {
		return nil, "没有可用的上游型号"
	}
	return found, ""
}

// supportsProtocols 判断档案是否覆盖全部给定协议。
func (p *codexThirdPartyProfile) supportsProtocols(protocols []string) bool {
	allowed := map[string]struct{}{}
	for _, item := range p.Protocols {
		allowed[item] = struct{}{}
	}
	for _, protocol := range protocols {
		if _, ok := allowed[protocol]; !ok {
			return false
		}
	}
	return len(protocols) > 0
}

// codexThirdPartyTemplateDigest 返回档案固定的指令模板在当前运行时的 sha256 与正文。
func codexThirdPartyRuntimeTemplate(p *codexThirdPartyProfile) (template string, digest string) {
	if p.Official {
		// 官方条目自带指令文本，直接取条目里的 instructions_template。
		template, _ = codexThirdPartyOfficialTemplate(p.ModelInfo)
	} else {
		template = codexInstructionsTemplateForModel(p.UpstreamModels[0])
	}
	sum := sha256.Sum256([]byte(template))
	return template, hex.EncodeToString(sum[:])
}

// buildEntry 生成完整的 ModelInfo 条目。slug 与显示名取公开名；
// removeImage 为 true 时（承接账号快照明确不含图片）去掉图片输入与 original 图片细节。
func (p *codexThirdPartyProfile) buildEntry(slug string, removeImage bool) (entry []byte, template string, err error) {
	template, digest := codexThirdPartyRuntimeTemplate(p)
	if digest != p.InstructionsTemplateSHA256 {
		return nil, template, fmt.Errorf("指令模板校验值与能力档案不一致（档案 %s）", p.ID)
	}
	fields := make(map[string]json.RawMessage, len(p.ModelInfo)+3)
	for key, value := range p.ModelInfo {
		fields[key] = value
	}
	var encodeErr error
	set := func(key string, value any) {
		raw, marshalErr := json.Marshal(value)
		if marshalErr != nil {
			encodeErr = marshalErr
			return
		}
		fields[key] = raw
	}
	set("slug", slug)
	set("display_name", slug)
	if !p.Official {
		// 官方条目的 model_messages 保持官方原样（含 instructions_variables 和 approvals），不重写。
		set("model_messages", map[string]string{"instructions_template": template})
	}
	if removeImage {
		var modalities []string
		if raw, ok := fields["input_modalities"]; ok {
			_ = json.Unmarshal(raw, &modalities)
		}
		kept := make([]string, 0, len(modalities))
		for _, item := range modalities {
			if item != "image" {
				kept = append(kept, item)
			}
		}
		set("input_modalities", kept)
		set("supports_image_detail_original", false)
	}
	if encodeErr != nil {
		return nil, template, encodeErr
	}
	entry, err = json.Marshal(fields)
	return entry, template, err
}

// summarizeCodexThirdPartyEntry 从生成的条目提取页面展示用的能力摘要。
func summarizeCodexThirdPartyEntry(entry []byte) CodexThirdPartyCapabilitySummary {
	var parsed struct {
		InputModalities          []string `json:"input_modalities"`
		DefaultReasoningLevel    string   `json:"default_reasoning_level"`
		ContextWindow            int64    `json:"context_window"`
		ShellType                string   `json:"shell_type"`
		ApplyPatchToolType       string   `json:"apply_patch_tool_type"`
		SupportsSearchTool       bool     `json:"supports_search_tool"`
		WebSearchToolType        string   `json:"web_search_tool_type"`
		SupportedReasoningLevels []struct {
			Effort string `json:"effort"`
		} `json:"supported_reasoning_levels"`
	}
	if err := json.Unmarshal(entry, &parsed); err != nil {
		return CodexThirdPartyCapabilitySummary{}
	}
	summary := CodexThirdPartyCapabilitySummary{
		InputModalities:       parsed.InputModalities,
		DefaultReasoningLevel: parsed.DefaultReasoningLevel,
		ContextWindow:         parsed.ContextWindow,
		ShellType:             parsed.ShellType,
		ApplyPatchToolType:    parsed.ApplyPatchToolType,
		SupportsSearchTool:    parsed.SupportsSearchTool,
		WebSearchToolType:     parsed.WebSearchToolType,
	}
	for _, level := range parsed.SupportedReasoningLevels {
		summary.ReasoningLevels = append(summary.ReasoningLevels, level.Effort)
	}
	return summary
}

// loadCodexThirdPartyOfficialEntries 解析嵌入的官方 models.json，返回 slug 到完整条目（逐键原始值）的映射。
func loadCodexThirdPartyOfficialEntries(raw []byte) (map[string]map[string]json.RawMessage, error) {
	var file struct {
		Models []map[string]json.RawMessage `json:"models"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		return nil, fmt.Errorf("codex third party official models: decode: %w", err)
	}
	entries := make(map[string]map[string]json.RawMessage, len(file.Models))
	for _, entry := range file.Models {
		var slug string
		if err := json.Unmarshal(entry["slug"], &slug); err != nil || strings.TrimSpace(slug) == "" {
			return nil, fmt.Errorf("codex third party official models: entry without slug")
		}
		if _, dup := entries[slug]; dup {
			return nil, fmt.Errorf("codex third party official models: duplicate slug %q", slug)
		}
		entries[slug] = entry
	}
	return entries, nil
}

// codexThirdPartyOfficialTemplate 取官方条目 model_messages 里的 instructions_template。
func codexThirdPartyOfficialTemplate(entry map[string]json.RawMessage) (string, error) {
	var messages struct {
		InstructionsTemplate string `json:"instructions_template"`
	}
	if err := json.Unmarshal(entry["model_messages"], &messages); err != nil || messages.InstructionsTemplate == "" {
		return "", fmt.Errorf("official entry has no instructions_template")
	}
	return messages.InstructionsTemplate, nil
}
