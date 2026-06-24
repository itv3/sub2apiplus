package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

type apiKeyMimicIdentityProfile struct {
	OfficialPrompt string
	NeutralClient  string
}

var (
	anthropicAPIKeyClaudeCodeIdentityProfile = apiKeyMimicIdentityProfile{
		OfficialPrompt: "You are Claude Code, Anthropic's official CLI for Claude.",
		NeutralClient:  "Claude Code",
	}
	openAIAPIKeyCodexIdentityProfile = apiKeyMimicIdentityProfile{
		OfficialPrompt: "You are Codex, running in the official Codex client environment.",
		NeutralClient:  "Codex",
	}
)

var apiKeyMimicThirdPartyIdentityMarkers = []string{
	"kilo-code",
	"kilo code",
	"kilo",
	"cline",
	"roo code",
	"roo-code",
	"roocode",
	"cursor",
	"opencode",
	"open code",
}

func applyAnthropicAPIKeyOfficialIdentityMimicryToBody(body []byte) []byte {
	if len(body) == 0 || !gjson.ValidBytes(body) {
		return body
	}
	profile := anthropicAPIKeyClaudeCodeIdentityProfile
	body = sanitizeJSONTextValue(body, "system", profile, true)
	body = sanitizeAnthropicSystemBlocks(body, profile)
	body = sanitizeAnthropicMessagesIdentity(body, profile)
	return body
}

func applyOpenAIAPIKeyCodexMimicryToBody(body []byte) []byte {
	if len(body) == 0 || !gjson.ValidBytes(body) {
		return body
	}
	body = normalizeOpenAIAPIKeyCodexBareRoleContentMessagesBody(body)
	body = normalizeOpenAIAPIKeyCodexSystemRoleBody(body)
	body = ensureOpenAIAPIKeyCodexInstructionsBody(body)
	body = ensureOpenAIAPIKeyCodexStreamBody(body)
	body = ensureOpenAIAPIKeyCodexStoreBody(body)
	body = ensureOpenAIAPIKeyCodexIncludeBody(body)
	body = ensureOpenAIAPIKeyCodexPromptCacheKeyBody(body)
	return body
}

func ensureOpenAIAPIKeyCodexStoreBody(body []byte) []byte {
	store := gjson.GetBytes(body, "store")
	if store.Exists() && !store.Bool() {
		return body
	}
	out, err := sjson.SetBytes(body, "store", false)
	if err != nil {
		return body
	}
	return out
}

func ensureOpenAIAPIKeyCodexIncludeBody(body []byte) []byte {
	include := gjson.GetBytes(body, "include")
	if include.IsArray() {
		found := false
		include.ForEach(func(_, item gjson.Result) bool {
			if strings.TrimSpace(item.String()) == "reasoning.encrypted_content" {
				found = true
				return false
			}
			return true
		})
		if found {
			return body
		}
	}
	out, err := sjson.SetBytes(body, "include", []string{"reasoning.encrypted_content"})
	if err != nil {
		return body
	}
	return out
}

func ensureOpenAIAPIKeyCodexStreamBody(body []byte) []byte {
	stream := gjson.GetBytes(body, "stream")
	if stream.Exists() && stream.Bool() {
		return body
	}
	out, err := sjson.SetBytes(body, "stream", true)
	if err != nil {
		return body
	}
	return out
}

func ensureOpenAIAPIKeyCodexInstructionsBody(body []byte) []byte {
	instructions := gjson.GetBytes(body, "instructions")
	if instructions.Exists() && instructions.Type == gjson.String && strings.TrimSpace(instructions.String()) != "" {
		return body
	}
	model := strings.TrimSpace(gjson.GetBytes(body, "model").String())
	out, err := sjson.SetBytes(body, "instructions", defaultCodexSynthInstructions(model))
	if err != nil {
		return body
	}
	return out
}

func ensureOpenAIAPIKeyCodexPromptCacheKeyBody(body []byte) []byte {
	existing := gjson.GetBytes(body, "prompt_cache_key")
	if existing.Exists() && strings.TrimSpace(existing.String()) != "" {
		return body
	}
	seed := buildOpenAIAPIKeyCodexPromptCacheKeySeed(body)
	if strings.TrimSpace(seed) == "" {
		seed = fmt.Sprintf("body_bytes=%d", len(body))
	}
	out, err := sjson.SetBytes(body, "prompt_cache_key", "codex-mimic-"+hashSensitiveValueForLog(seed))
	if err != nil {
		return body
	}
	return out
}

func buildOpenAIAPIKeyCodexPromptCacheKeySeed(body []byte) string {
	parts := make([]string, 0, 16)
	if model := strings.TrimSpace(gjson.GetBytes(body, "model").String()); model != "" {
		parts = append(parts, "model="+model)
	}
	if previousResponseID := strings.TrimSpace(gjson.GetBytes(body, "previous_response_id").String()); previousResponseID != "" {
		parts = append(parts, "previous_response_id="+previousResponseID)
	}
	if stream := gjson.GetBytes(body, "stream"); stream.Exists() {
		parts = append(parts, "stream="+stream.String())
	}
	if effort := strings.TrimSpace(gjson.GetBytes(body, "reasoning.effort").String()); effort != "" {
		parts = append(parts, "reasoning_effort="+effort)
	}
	if verbosity := strings.TrimSpace(gjson.GetBytes(body, "text.verbosity").String()); verbosity != "" {
		parts = append(parts, "text_verbosity="+verbosity)
	}
	if inputSignature := openAIAPIKeyCodexInputSignature(gjson.GetBytes(body, "input")); inputSignature != "" {
		parts = append(parts, "input="+inputSignature)
	}
	if toolsSignature := openAIAPIKeyCodexToolsSignature(gjson.GetBytes(body, "tools")); toolsSignature != "" {
		parts = append(parts, "tools="+toolsSignature)
	}
	return strings.Join(parts, "|")
}

func openAIAPIKeyCodexInputSignature(input gjson.Result) string {
	if input.Type == gjson.String {
		return "string"
	}
	if !input.IsArray() {
		return ""
	}
	items := make([]string, 0, 8)
	idx := 0
	input.ForEach(func(_, item gjson.Result) bool {
		if idx >= 16 {
			items = append(items, "more")
			return false
		}
		itemParts := make([]string, 0, 4)
		if typ := strings.TrimSpace(item.Get("type").String()); typ != "" {
			itemParts = append(itemParts, "type="+typ)
		}
		if role := strings.TrimSpace(item.Get("role").String()); role != "" {
			itemParts = append(itemParts, "role="+role)
		}
		content := item.Get("content")
		switch {
		case content.Type == gjson.String:
			itemParts = append(itemParts, "content=string")
		case content.IsArray():
			partTypes := make([]string, 0, 4)
			partIdx := 0
			content.ForEach(func(_, part gjson.Result) bool {
				if partIdx >= 8 {
					partTypes = append(partTypes, "more")
					return false
				}
				if typ := strings.TrimSpace(part.Get("type").String()); typ != "" {
					partTypes = append(partTypes, typ)
				}
				partIdx++
				return true
			})
			if len(partTypes) > 0 {
				itemParts = append(itemParts, "content_types="+strings.Join(partTypes, ","))
			}
		}
		if len(itemParts) > 0 {
			items = append(items, strings.Join(itemParts, ","))
		}
		idx++
		return true
	})
	return strings.Join(items, ";")
}

func openAIAPIKeyCodexToolsSignature(tools gjson.Result) string {
	if !tools.IsArray() {
		return ""
	}
	items := make([]string, 0, 8)
	idx := 0
	tools.ForEach(func(_, tool gjson.Result) bool {
		if idx >= 32 {
			items = append(items, "more")
			return false
		}
		itemParts := make([]string, 0, 3)
		if typ := strings.TrimSpace(tool.Get("type").String()); typ != "" {
			itemParts = append(itemParts, "type="+typ)
		}
		if name := strings.TrimSpace(tool.Get("name").String()); name != "" {
			itemParts = append(itemParts, "name="+name)
		} else if name := strings.TrimSpace(tool.Get("function.name").String()); name != "" {
			itemParts = append(itemParts, "function="+name)
		}
		if len(itemParts) > 0 {
			items = append(items, strings.Join(itemParts, ","))
		}
		idx++
		return true
	})
	return strings.Join(items, ";")
}

func normalizeOpenAIAPIKeyCodexBareRoleContentMessagesBody(body []byte) []byte {
	if len(body) == 0 || !gjson.ValidBytes(body) {
		return body
	}
	if !gjson.GetBytes(body, "input").IsArray() {
		return body
	}

	var reqBody map[string]any
	if err := json.Unmarshal(body, &reqBody); err != nil {
		return body
	}
	input, ok := reqBody["input"].([]any)
	if !ok {
		return body
	}
	normalized, changed := normalizeCodexBareRoleContentMessages(input)
	if !changed {
		return body
	}
	reqBody["input"] = normalized
	out, err := marshalOpenAIUpstreamJSON(reqBody)
	if err != nil {
		return body
	}
	return out
}

func normalizeOpenAIAPIKeyCodexSystemRoleBody(body []byte) []byte {
	if len(body) == 0 || !gjson.ValidBytes(body) || !gjson.GetBytes(body, "input").IsArray() {
		return body
	}
	var reqBody map[string]any
	if err := json.Unmarshal(body, &reqBody); err != nil {
		return body
	}
	input, ok := reqBody["input"].([]any)
	if !ok {
		return body
	}
	changed := false
	for _, item := range input {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		role, ok := m["role"].(string)
		if !ok || !strings.EqualFold(strings.TrimSpace(role), "system") {
			continue
		}
		m["role"] = "developer"
		changed = true
	}
	if !changed {
		return body
	}
	reqBody["input"] = input
	out, err := marshalOpenAIUpstreamJSON(reqBody)
	if err != nil {
		return body
	}
	return out
}

func sanitizeAnthropicSystemBlocks(body []byte, profile apiKeyMimicIdentityProfile) []byte {
	system := gjson.GetBytes(body, "system")
	if !system.IsArray() {
		return body
	}
	idx := -1
	system.ForEach(func(_, item gjson.Result) bool {
		idx++
		if item.Get("type").String() != "text" && item.Get("text").Type != gjson.String {
			return true
		}
		path := fmt.Sprintf("system.%d.text", idx)
		body = sanitizeJSONTextValue(body, path, profile, idx == 0)
		return true
	})
	return body
}

func sanitizeAnthropicMessagesIdentity(body []byte, profile apiKeyMimicIdentityProfile) []byte {
	messages := gjson.GetBytes(body, "messages")
	if !messages.IsArray() {
		return body
	}
	msgIdx := -1
	messages.ForEach(func(_, msg gjson.Result) bool {
		msgIdx++
		content := msg.Get("content")
		if content.Type == gjson.String {
			body = sanitizeJSONTextValue(body, fmt.Sprintf("messages.%d.content", msgIdx), profile, false)
			return true
		}
		if !content.IsArray() {
			return true
		}
		partIdx := -1
		content.ForEach(func(_, part gjson.Result) bool {
			partIdx++
			if part.Get("text").Type == gjson.String {
				body = sanitizeJSONTextValue(body, fmt.Sprintf("messages.%d.content.%d.text", msgIdx, partIdx), profile, false)
			}
			return true
		})
		return true
	})
	return body
}

func sanitizeOpenAIInputIdentity(body []byte, profile apiKeyMimicIdentityProfile) []byte {
	input := gjson.GetBytes(body, "input")
	if input.Type == gjson.String {
		return sanitizeJSONTextValue(body, "input", profile, false)
	}
	if !input.IsArray() {
		return body
	}
	idx := -1
	input.ForEach(func(_, item gjson.Result) bool {
		idx++
		if item.Get("content").Type == gjson.String {
			body = sanitizeJSONTextValue(body, fmt.Sprintf("input.%d.content", idx), profile, false)
			return true
		}
		content := item.Get("content")
		if !content.IsArray() {
			return true
		}
		partIdx := -1
		content.ForEach(func(_, part gjson.Result) bool {
			partIdx++
			if part.Get("text").Type == gjson.String {
				body = sanitizeJSONTextValue(body, fmt.Sprintf("input.%d.content.%d.text", idx, partIdx), profile, false)
			}
			return true
		})
		return true
	})
	return body
}

func sanitizeOpenAIToolsIdentity(body []byte, profile apiKeyMimicIdentityProfile) []byte {
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		return body
	}
	idx := -1
	tools.ForEach(func(_, tool gjson.Result) bool {
		idx++
		body = sanitizeJSONTextValue(body, fmt.Sprintf("tools.%d.description", idx), profile, false)
		body = sanitizeJSONTextValue(body, fmt.Sprintf("tools.%d.function.description", idx), profile, false)
		body = sanitizeToolSchemaDescriptions(body, fmt.Sprintf("tools.%d.parameters", idx), profile)
		body = sanitizeToolSchemaDescriptions(body, fmt.Sprintf("tools.%d.function.parameters", idx), profile)
		return true
	})
	return body
}

func sanitizeToolSchemaDescriptions(body []byte, basePath string, profile apiKeyMimicIdentityProfile) []byte {
	schema := gjson.GetBytes(body, basePath)
	if !schema.Exists() {
		return body
	}
	var walk func(path string, value gjson.Result)
	walk = func(path string, value gjson.Result) {
		if value.Get("description").Type == gjson.String {
			body = sanitizeJSONTextValue(body, path+".description", profile, false)
		}
		if props := value.Get("properties"); props.IsObject() {
			props.ForEach(func(key, child gjson.Result) bool {
				walk(path+".properties."+escapeSJSONPathPart(key.String()), child)
				return true
			})
		}
		if items := value.Get("items"); items.Exists() {
			walk(path+".items", items)
		}
	}
	walk(basePath, schema)
	return body
}

func sanitizeJSONTextValue(body []byte, path string, profile apiKeyMimicIdentityProfile, ensureOfficial bool) []byte {
	result := gjson.GetBytes(body, path)
	if result.Type != gjson.String {
		return body
	}
	next, changed := sanitizeAPIKeyMimicIdentityText(result.String(), profile, ensureOfficial)
	if !changed {
		return body
	}
	if out, err := sjson.SetBytes(body, path, next); err == nil {
		return out
	}
	return body
}

func sanitizeAPIKeyMimicIdentityText(text string, profile apiKeyMimicIdentityProfile, ensureOfficial bool) (string, bool) {
	original := text
	if containsAPIKeyMimicThirdPartyMarker(text) {
		text = rewriteThirdPartyIdentityLines(text, profile)
	}
	if ensureOfficial && !strings.Contains(strings.ToLower(text), strings.ToLower(profile.NeutralClient)) {
		text = strings.TrimSpace(profile.OfficialPrompt + "\n\n" + strings.TrimSpace(text))
	}
	return text, text != original
}

func rewriteThirdPartyIdentityLines(text string, profile apiKeyMimicIdentityProfile) string {
	lines := strings.Split(text, "\n")
	insertedOfficial := false
	for i, line := range lines {
		if !containsAPIKeyMimicThirdPartyMarker(line) {
			continue
		}
		if looksLikeStandaloneIdentityInstructionLine(line) {
			if !insertedOfficial {
				lines[i] = profile.OfficialPrompt
				insertedOfficial = true
			} else {
				lines[i] = ""
			}
			continue
		}
		lines[i] = replaceThirdPartyIdentityMarkers(line, profile.NeutralClient)
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func containsAPIKeyMimicThirdPartyMarker(text string) bool {
	lower := strings.ToLower(text)
	for _, marker := range apiKeyMimicThirdPartyIdentityMarkers {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func looksLikeStandaloneIdentityInstructionLine(line string) bool {
	lower := strings.ToLower(line)
	for _, marker := range []string{"keep ", "rules", "instructions", "project", "repository"} {
		if strings.Contains(lower, marker) {
			return false
		}
	}
	if len(strings.Fields(line)) > 12 {
		return false
	}
	for _, marker := range []string{
		"you are",
		"you operate as",
		"your name",
		"你是",
		"你叫",
		"身份",
		"client",
		"客户端",
		"coding agent",
		"code assistant",
		"interactive cli",
	} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func replaceThirdPartyIdentityMarkers(text string, replacement string) string {
	replacer := strings.NewReplacer(
		"Kilo-Code", replacement,
		"kilo-code", replacement,
		"Kilo Code", replacement,
		"kilo code", replacement,
		"Kilo", replacement,
		"kilo", replacement,
		"Cline", replacement,
		"cline", replacement,
		"Roo Code", replacement,
		"roo code", replacement,
		"Roo-Code", replacement,
		"roo-code", replacement,
		"RooCode", replacement,
		"roocode", replacement,
		"Cursor", replacement,
		"cursor", replacement,
		"OpenCode", replacement,
		"opencode", replacement,
		"Open Code", replacement,
		"open code", replacement,
	)
	return replacer.Replace(text)
}

func escapeSJSONPathPart(part string) string {
	if strings.IndexAny(part, `.\:`) == -1 {
		return part
	}
	encoded, err := json.Marshal(part)
	if err != nil {
		return part
	}
	return string(encoded)
}
