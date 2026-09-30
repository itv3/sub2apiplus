package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// toolNameRewriteKey 是 gin.Context 上存 ToolNameRewrite 映射的 key。
// 请求阶段写入，响应阶段读取，用于 bytes 级逆向还原假名 → 真名。
const toolNameRewriteKey = "claude_tool_name_rewrite"

// staticToolNameRewrites 是"静态前缀映射"，与 Parrot src/transform/cc_mimicry.py
// TOOL_NAME_REWRITES 完全一致。只有以这些前缀开头的工具会被重写。
var staticToolNameRewrites = map[string]string{
	"sessions_": "cc_sess_",
	"session_":  "cc_ses_",
}

// claudeCodeOAuthToolNameRewrites 对齐 CLIProxyAPI 的 oauthToolRenameMap。
// 只把已知第三方小写工具名改成 Claude Code 风格名称，避免扩大到动态假名混淆。
var claudeCodeOAuthToolNameRewrites = map[string]string{
	"bash":         "Bash",
	"read":         "Read",
	"write":        "Write",
	"edit":         "Edit",
	"glob":         "Glob",
	"grep":         "Grep",
	"task":         "Task",
	"webfetch":     "WebFetch",
	"todowrite":    "TodoWrite",
	"question":     "Question",
	"skill":        "Skill",
	"ls":           "LS",
	"todoread":     "TodoRead",
	"notebookedit": "NotebookEdit",
}

// fakeToolNamePrefixes 是"动态映射"的前缀池，与 Parrot _FAKE_PREFIXES 一致。
// 当 tools 数量 > dynamicToolMapThreshold 时随机选用其中前缀生成可读假名。
var fakeToolNamePrefixes = []string{
	"analyze_", "compute_", "fetch_", "generate_", "lookup_", "modify_",
	"process_", "query_", "render_", "resolve_", "sync_", "update_",
	"validate_", "convert_", "extract_", "manage_", "monitor_", "parse_",
	"review_", "search_", "transform_", "handle_", "invoke_", "notify_",
}

// dynamicToolMapThreshold 与 Parrot 一致：tools 数量超过 5 才启用动态映射。
// 少量工具不需要混淆（一般是 Claude Code 自己的核心工具 bash/edit/read 等）。
const dynamicToolMapThreshold = 5

// ToolNameRewrite 是单次请求内的工具名混淆映射。
//   - Forward: real → fake，请求阶段在 body 上应用。
//   - Reverse: fake → real，响应阶段对每个 chunk 做 bytes.Replace 还原。
//
// ReverseOrdered 是按假名长度倒序的 (fake, real) 列表，用于防止短假名是长假名的
// 子串时 bytes.Replace 先被吃掉（对齐 Parrot _restore_tool_names_in_chunk 的
// `sorted(..., key=lambda x: len(x[1]), reverse=True)`）。
type ToolNameRewrite struct {
	Forward                   map[string]string
	Reverse                   map[string]string
	ReverseOrdered            [][2]string
	StructuredResponseRestore bool
}

// buildDynamicToolMap 构造 tools 的动态假名映射。
//
// 与 Parrot _build_dynamic_tool_map 语义等价：
//   - tools 数量 ≤ dynamicToolMapThreshold 时返回 nil（不做动态映射，走静态 fallback）
//   - 同一组 tool_names 在同进程内映射稳定（保证 cache 命中）
//
// Parrot 用 `random.Random(hash(tuple(tool_names)))` 作 seed + shuffle 前缀池；
// Go 无法字节级复刻 Python hash，但"稳定性"和"前缀池打散"两个不变量都保留：
// 用 fnv64a(strings.Join(names, "\x00")) 作 seed 喂 math/rand.New。
// 字节级不同不影响上游判定（Anthropic 不会验证我们的随机种子算法）。
func buildDynamicToolMap(toolNames []string) map[string]string {
	if len(toolNames) <= dynamicToolMapThreshold {
		return nil
	}
	h := fnv.New64a()
	for i, n := range toolNames {
		if i > 0 {
			_, _ = h.Write([]byte{0})
		}
		_, _ = h.Write([]byte(n))
	}
	rng := rand.New(rand.NewSource(int64(h.Sum64())))

	available := make([]string, len(fakeToolNamePrefixes))
	copy(available, fakeToolNamePrefixes)
	rng.Shuffle(len(available), func(i, j int) { available[i], available[j] = available[j], available[i] })

	mapping := make(map[string]string, len(toolNames))
	for i, name := range toolNames {
		prefix := available[i%len(available)]
		headLen := 3
		if len(name) < 3 {
			headLen = len(name)
		}
		fake := fmt.Sprintf("%s%s%02d", prefix, name[:headLen], i)
		mapping[name] = fake
	}
	return mapping
}

// sanitizeToolName 把真名转成假名。
// 与 Parrot _sanitize_tool_name 语义一致：动态映射优先，再走静态前缀映射。
func sanitizeToolName(name string, dynamic map[string]string) string {
	if dynamic != nil {
		if fake, ok := dynamic[name]; ok {
			return fake
		}
	}
	for prefix, replacement := range staticToolNameRewrites {
		if strings.HasPrefix(name, prefix) {
			return replacement + name[len(prefix):]
		}
	}
	return name
}

// shouldMimicToolName 指示某个 tool 是否需要重命名。
// server tool（type != "" 且不是 "function" / "custom"）是 Anthropic 协议语义的一部分，
// 比如 "web_search_20250305" / "computer_20250124"；误改会导致上游拒绝。
func shouldMimicToolName(toolType string) bool {
	if toolType == "" || toolType == "function" || toolType == "custom" {
		return true
	}
	return false
}

// buildToolNameRewriteFromBody 扫描 body 的 tools[*].name，构造 ToolNameRewrite
// 并返回它。若不需要混淆（tools 数量不足 + 没有匹配静态前缀的工具）返回 nil。
//
// 注意：只扫描，不改 body。真正的 body 改写在 applyToolNameRewriteToBody。
func buildToolNameRewriteFromBody(body []byte) *ToolNameRewrite {
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		return nil
	}

	mimicableNames := make([]string, 0)
	toolsArr := tools.Array()
	for _, t := range toolsArr {
		if !shouldMimicToolName(t.Get("type").String()) {
			continue
		}
		name := t.Get("name").String()
		if name == "" {
			continue
		}
		mimicableNames = append(mimicableNames, name)
	}

	dynamic := buildDynamicToolMap(mimicableNames)

	rw := &ToolNameRewrite{
		Forward: make(map[string]string),
		Reverse: make(map[string]string),
	}
	for _, name := range mimicableNames {
		fake := sanitizeToolName(name, dynamic)
		if fake == name {
			continue
		}
		rw.Forward[name] = fake
		rw.Reverse[fake] = name
	}
	return finalizeToolNameRewrite(rw)
}

// buildClaudeCodeOAuthToolNameRewriteFromBody 按 CLIProxyAPI 的工具名映射表构造
// 单请求映射：请求侧小写第三方名 -> Claude Code 名，响应侧再按本请求 reverse map 回写。
func buildClaudeCodeOAuthToolNameRewriteFromBody(body []byte) *ToolNameRewrite {
	rw := &ToolNameRewrite{
		Forward:                   make(map[string]string),
		Reverse:                   make(map[string]string),
		StructuredResponseRestore: true,
	}
	recordRename := func(original string) {
		renamed, ok := claudeCodeOAuthToolNameRewrites[original]
		if !ok || renamed == "" || renamed == original {
			return
		}
		rw.Forward[original] = renamed
		if _, exists := rw.Reverse[renamed]; !exists {
			rw.Reverse[renamed] = original
		}
	}

	tools := gjson.GetBytes(body, "tools")
	if tools.IsArray() {
		tools.ForEach(func(_, tool gjson.Result) bool {
			if !shouldMimicToolName(tool.Get("type").String()) {
				return true
			}
			recordRename(tool.Get("name").String())
			return true
		})
	}

	if tc := gjson.GetBytes(body, "tool_choice"); tc.Exists() && tc.Get("type").String() == "tool" {
		recordRename(tc.Get("name").String())
	}

	messages := gjson.GetBytes(body, "messages")
	if messages.IsArray() {
		messages.ForEach(func(_, msg gjson.Result) bool {
			content := msg.Get("content")
			if !content.IsArray() {
				return true
			}
			content.ForEach(func(_, part gjson.Result) bool {
				switch part.Get("type").String() {
				case "tool_use":
					recordRename(part.Get("name").String())
				case "tool_reference":
					recordRename(part.Get("tool_name").String())
				case "tool_result":
					nestedContent := part.Get("content")
					if nestedContent.IsArray() {
						nestedContent.ForEach(func(_, nestedPart gjson.Result) bool {
							if nestedPart.Get("type").String() == "tool_reference" {
								recordRename(nestedPart.Get("tool_name").String())
							}
							return true
						})
					}
				}
				return true
			})
			return true
		})
	}

	return finalizeToolNameRewrite(rw)
}

func finalizeToolNameRewrite(rw *ToolNameRewrite) *ToolNameRewrite {
	if rw == nil || len(rw.Forward) == 0 {
		return nil
	}
	rw.ReverseOrdered = make([][2]string, 0, len(rw.Reverse))
	for fake, real := range rw.Reverse {
		rw.ReverseOrdered = append(rw.ReverseOrdered, [2]string{fake, real})
	}
	sort.SliceStable(rw.ReverseOrdered, func(i, j int) bool {
		return len(rw.ReverseOrdered[i][0]) > len(rw.ReverseOrdered[j][0])
	})
	return rw
}

type toolNameSpan struct {
	start, end int
	value      []byte
}

// applyToolNameRewriteNamesToBody 只改写工具名，不注入 tools cache_control。
//
//   - 改写 $.tools[*].name（仅对 shouldMimicToolName 通过的 tool）
//   - 改写 $.tool_choice.name（仅当 $.tool_choice.type == "tool"）
//   - 改写 $.messages[*].content[*].name（仅当 type == "tool_use"）
//   - 改写 $.messages[*].content[*].tool_name（仅当 type == "tool_reference"）
//   - 改写 $.messages[*].content[*].content[*].tool_name（tool_result 内嵌的 tool_reference）
//
// 所有替换先按原始正文中的绝对偏移收集，再一次性拷贝正文（与上游 27a9421e6 一致）。
// 响应侧 bytes.Replace 会连带还原假名 → 真名。
func applyToolNameRewriteNamesToBody(body []byte, rw *ToolNameRewrite) []byte {
	if rw == nil || len(rw.Forward) == 0 {
		return body
	}

	// gjson 子结果的 Index 是相对原始正文的绝对偏移：先收集全部替换位置，
	// 在改变正文长度之前不做修改，最后只拷贝一次正文。
	var edits []toolNameSpan
	addName := func(name gjson.Result) {
		if !name.Exists() {
			return
		}
		fake, ok := rw.Forward[name.String()]
		if !ok {
			return
		}
		encoded, err := json.Marshal(fake)
		if err != nil || name.Index < 0 || name.Index+len(name.Raw) > len(body) ||
			!bytes.Equal(body[name.Index:name.Index+len(name.Raw)], []byte(name.Raw)) {
			return
		}
		edits = append(edits, toolNameSpan{name.Index, name.Index + len(name.Raw), encoded})
	}

	tools := gjson.GetBytes(body, "tools")
	if tools.IsArray() {
		tools.ForEach(func(_, tool gjson.Result) bool {
			if shouldMimicToolName(tool.Get("type").String()) {
				addName(tool.Get("name"))
			}
			return true
		})
	}
	if choice := gjson.GetBytes(body, "tool_choice"); choice.Get("type").String() == "tool" {
		addName(choice.Get("name"))
	}
	if messages := gjson.GetBytes(body, "messages"); messages.IsArray() {
		messages.ForEach(func(_, msg gjson.Result) bool {
			content := msg.Get("content")
			if content.IsArray() {
				content.ForEach(func(_, block gjson.Result) bool {
					switch block.Get("type").String() {
					case "tool_use":
						addName(block.Get("name"))
					case "tool_reference":
						addName(block.Get("tool_name"))
					case "tool_result":
						nested := block.Get("content")
						if nested.IsArray() {
							nested.ForEach(func(_, nestedBlock gjson.Result) bool {
								if nestedBlock.Get("type").String() == "tool_reference" {
									addName(nestedBlock.Get("tool_name"))
								}
								return true
							})
						}
					}
					return true
				})
			}
			return true
		})
	}
	if len(edits) != 0 {
		sort.Slice(edits, func(i, j int) bool { return edits[i].start < edits[j].start })
		var out []byte
		out = make([]byte, 0, len(body))
		pos := 0
		for _, edit := range edits {
			if edit.start < pos { // 畸形 JSON 可能产生重叠区间，直接忽略。
				continue
			}
			out = append(out, body[pos:edit.start]...)
			out = append(out, edit.value...)
			pos = edit.end
		}
		body = append(out, body[pos:]...)
	}

	return body
}

// applyToolNameRewriteToBody 把已构造的 ToolNameRewrite 应用到 body 上，并在
// $.tools[last].cache_control 上打 ephemeral 缓存断点。
func applyToolNameRewriteToBody(body []byte, rw *ToolNameRewrite) []byte {
	body = applyToolNameRewriteNamesToBody(body, rw)
	body = applyToolsLastCacheBreakpoint(body)
	return body
}

// applyToolsLastCacheBreakpoint 在最后一个非延迟加载工具上注入 cache_control
// 断点。Anthropic 不允许 defer_loading=true 的工具携带 cache_control，
// 因此会先清理所有延迟加载工具上的客户端断点。兼容官方顶层字段和
// Claude Code 使用的 custom.defer_loading 字段。其余行为对齐 Parrot
// `tools[-1]["cache_control"] = {"type":"ephemeral","ttl":"1h"}`，
// 但 ttl 按本仓规则：
//   - 客户端已为该 tool 显式设置 cache_control.ttl → 完全透传不覆盖
//   - 否则注入 {"type":"ephemeral","ttl": claude.DefaultCacheControlTTL}
//
// 纯副作用函数，tools 不存在或为空数组时 no-op。
func applyToolsLastCacheBreakpoint(body []byte) []byte {
	body = stripDeferredToolCacheControl(body)
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		return body
	}
	arr := tools.Array()
	if len(arr) == 0 {
		return body
	}
	lastIdx := -1
	for idx, tool := range arr {
		if isDeferredLoadingTool(tool) {
			continue
		}
		lastIdx = idx
	}
	if lastIdx == -1 {
		return body
	}

	existingCC := arr[lastIdx].Get("cache_control")

	if existingCC.Exists() && existingCC.Get("ttl").String() != "" {
		return body
	}

	if existingCC.Exists() {
		if next, err := sjson.SetBytes(body, fmt.Sprintf("tools.%d.cache_control.ttl", lastIdx), claude.DefaultCacheControlTTL); err == nil {
			body = next
		}
		return body
	}

	raw := fmt.Sprintf(`{"type":"ephemeral","ttl":%q}`, claude.DefaultCacheControlTTL)
	if next, err := sjson.SetRawBytes(body, fmt.Sprintf("tools.%d.cache_control", lastIdx), []byte(raw)); err == nil {
		body = next
	}
	return body
}

func isDeferredLoadingTool(tool gjson.Result) bool {
	return tool.Get("defer_loading").Type == gjson.True ||
		tool.Get("custom.defer_loading").Type == gjson.True
}

// stripDeferredToolCacheControl removes the cache marker Anthropic rejects on
// deferred tools. Only the literal JSON boolean true enables deferred loading.
func stripDeferredToolCacheControl(body []byte) []byte {
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		return body
	}
	for idx, tool := range tools.Array() {
		if !isDeferredLoadingTool(tool) || !tool.Get("cache_control").Exists() {
			continue
		}
		if next, err := sjson.DeleteBytes(body, fmt.Sprintf("tools.%d.cache_control", idx)); err == nil {
			body = next
		}
	}
	return body
}

// restoreToolNamesInBytes 对 bytes chunk 做逆向还原：假名 → 真名。
// 按 ReverseOrdered 的假名长度倒序逐个 bytes.Replace，防止子串冲突
// （与 Parrot _restore_tool_names_in_chunk 的 sorted(..., reverse=True) 等价）。
// 再做静态前缀还原（cc_sess_ → sessions_ / cc_ses_ → session_）。
//
// rw 可为 nil；nil 时仍会做静态前缀还原。
func restoreToolNamesInBytes(data []byte, rw *ToolNameRewrite) []byte {
	if rw != nil {
		if rw.StructuredResponseRestore {
			return restoreStructuredToolNamesInBytes(data, rw)
		}
		for _, pair := range rw.ReverseOrdered {
			fake, real := pair[0], pair[1]
			if fake == "" || fake == real {
				continue
			}
			data = replaceAllBytes(data, fake, real)
		}
	}
	for prefix, replacement := range staticToolNameRewrites {
		data = replaceAllBytes(data, replacement, prefix)
	}
	return data
}

func restoreStructuredToolNamesInBytes(data []byte, rw *ToolNameRewrite) []byte {
	if len(data) == 0 || rw == nil || len(rw.Reverse) == 0 {
		return restoreStaticToolNamePrefixes(data)
	}

	if out, ok := restoreStructuredToolNamesInSSELine(data, rw); ok {
		return restoreStaticToolNamePrefixes(out)
	}
	if out, ok := restoreStructuredToolNamesInJSON(data, rw); ok {
		return restoreStaticToolNamePrefixes(out)
	}
	return restoreStaticToolNamePrefixes(data)
}

func restoreStaticToolNamePrefixes(data []byte) []byte {
	for prefix, replacement := range staticToolNameRewrites {
		data = replaceAllBytes(data, replacement, prefix)
	}
	return data
}

func restoreStructuredToolNamesInSSELine(line []byte, rw *ToolNameRewrite) ([]byte, bool) {
	dataLineStart := 0
	lineStr := string(line)
	if !strings.HasPrefix(lineStr, "data:") {
		idx := strings.Index(lineStr, "\ndata:")
		if idx < 0 {
			return nil, false
		}
		dataLineStart = idx + 1
	}
	dataLine := line[dataLineStart:]
	if !strings.HasPrefix(string(dataLine), "data:") {
		return nil, false
	}
	prefixLen := len("data:")
	payloadStart := prefixLen
	for payloadStart < len(dataLine) && (dataLine[payloadStart] == ' ' || dataLine[payloadStart] == '\t') {
		payloadStart++
	}
	payload := dataLine[payloadStart:]
	payloadEnd := len(payload)
	for payloadEnd > 0 && (payload[payloadEnd-1] == '\n' || payload[payloadEnd-1] == '\r' || payload[payloadEnd-1] == ' ' || payload[payloadEnd-1] == '\t') {
		payloadEnd--
	}
	payloadJSON := payload[:payloadEnd]
	payloadSuffix := payload[payloadEnd:]
	if len(payloadJSON) == 0 || string(payloadJSON) == "[DONE]" || !gjson.ValidBytes(payloadJSON) {
		return nil, false
	}

	restored, changed := restoreStructuredToolNamesInJSONObject(payloadJSON, rw)
	if !changed {
		return line, true
	}
	out := make([]byte, 0, dataLineStart+payloadStart+len(restored)+len(payloadSuffix))
	out = append(out, line[:dataLineStart]...)
	out = append(out, dataLine[:payloadStart]...)
	out = append(out, restored...)
	out = append(out, payloadSuffix...)
	return out, true
}

func restoreStructuredToolNamesInJSON(data []byte, rw *ToolNameRewrite) ([]byte, bool) {
	if !gjson.ValidBytes(data) {
		return nil, false
	}
	return restoreStructuredToolNamesInJSONObject(data, rw)
}

func restoreStructuredToolNamesInJSONObject(data []byte, rw *ToolNameRewrite) ([]byte, bool) {
	out := data
	changed := false
	setStringIfMapped := func(path string) {
		current := gjson.GetBytes(out, path)
		if !current.Exists() {
			return
		}
		real, ok := rw.Reverse[current.String()]
		if !ok {
			return
		}
		if next, err := sjson.SetBytes(out, path, real); err == nil {
			out = next
			changed = true
		}
	}

	restoreAnthropicToolFields := func(prefix string) {
		setStringIfMapped(prefix + "name")
		setStringIfMapped(prefix + "tool_name")
	}

	content := gjson.GetBytes(out, "content")
	if content.IsArray() {
		content.ForEach(func(idx, part gjson.Result) bool {
			base := fmt.Sprintf("content.%d.", int(idx.Num))
			switch part.Get("type").String() {
			case "tool_use", "tool_reference":
				restoreAnthropicToolFields(base)
			}
			return true
		})
	}

	restoreAnthropicToolFields("content_block.")
	restoreOpenAIResponseToolFields(out, &setStringIfMapped)
	restoreOpenAIChatToolFields(out, &setStringIfMapped)

	return out, changed
}

func restoreOpenAIResponseToolFields(out []byte, setStringIfMapped *func(string)) {
	output := gjson.GetBytes(out, "output")
	if output.IsArray() {
		output.ForEach(func(idx, item gjson.Result) bool {
			if item.Get("type").String() == "function_call" || item.Get("type").String() == "custom_tool_call" {
				(*setStringIfMapped)(fmt.Sprintf("output.%d.name", int(idx.Num)))
			}
			return true
		})
	}

	item := gjson.GetBytes(out, "item")
	if item.Exists() && (item.Get("type").String() == "function_call" || item.Get("type").String() == "custom_tool_call") {
		(*setStringIfMapped)("item.name")
	}

	typ := gjson.GetBytes(out, "type").String()
	if typ == "response.function_call_arguments.delta" || typ == "response.function_call_arguments.done" {
		(*setStringIfMapped)("name")
	}
}

func restoreOpenAIChatToolFields(out []byte, setStringIfMapped *func(string)) {
	choices := gjson.GetBytes(out, "choices")
	if !choices.IsArray() {
		return
	}
	choices.ForEach(func(choiceIdx, choice gjson.Result) bool {
		toolCalls := choice.Get("message.tool_calls")
		if toolCalls.IsArray() {
			toolCalls.ForEach(func(toolIdx, toolCall gjson.Result) bool {
				if toolCall.Get("type").String() == "" || toolCall.Get("type").String() == "function" {
					(*setStringIfMapped)(fmt.Sprintf("choices.%d.message.tool_calls.%d.function.name", int(choiceIdx.Num), int(toolIdx.Num)))
				}
				return true
			})
		}

		deltaToolCalls := choice.Get("delta.tool_calls")
		if deltaToolCalls.IsArray() {
			deltaToolCalls.ForEach(func(toolIdx, toolCall gjson.Result) bool {
				(*setStringIfMapped)(fmt.Sprintf("choices.%d.delta.tool_calls.%d.function.name", int(choiceIdx.Num), int(toolIdx.Num)))
				return true
			})
		}
		return true
	})
}

// replaceAllBytes 是 bytes.ReplaceAll 的便捷封装，避免每个调用点各自做 []byte 转换。
func replaceAllBytes(data []byte, from, to string) []byte {
	if len(data) == 0 || from == "" || from == to {
		return data
	}
	fromBytes := []byte(from)
	if !bytes.Contains(data, fromBytes) {
		return data
	}
	return bytes.ReplaceAll(data, fromBytes, []byte(to))
}

// toolNameRewriteFromContext 从 gin.Context 取出请求阶段保存的工具名映射。
// 找不到（c==nil 或 key 不存在或类型不对）时返回 nil；调用方必须能处理 nil。
func toolNameRewriteFromContext(c interface {
	Get(string) (any, bool)
}) *ToolNameRewrite {
	if c == nil {
		return nil
	}
	raw, ok := c.Get(toolNameRewriteKey)
	if !ok || raw == nil {
		return nil
	}
	rw, _ := raw.(*ToolNameRewrite)
	return rw
}

// reverseToolNamesIfPresent 是响应侧注入点的统一封装：从 c 取出 mapping
// 并对 chunk 做 bytes 级假名→真名替换。没有请求侧 mapping 时不改响应。
func reverseToolNamesIfPresent(c interface {
	Get(string) (any, bool)
}, chunk []byte) []byte {
	rw := toolNameRewriteFromContext(c)
	if rw == nil {
		return chunk
	}
	return restoreToolNamesInBytes(chunk, rw)
}
