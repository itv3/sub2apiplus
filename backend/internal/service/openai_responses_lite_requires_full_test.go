package service

import (
	"math/rand"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 本文件锁定问题四 M2 清单外的一处副本：openAIResponsesLiteRequiresFullResponses 逐项遍历 input，不再为全部
// input 项分配结果切片；判定结果与改造前完全一致。

// legacyOpenAIResponsesLiteRequiresFullResponses 是改造前的实现，原样保留作差分基准。
func legacyOpenAIResponsesLiteRequiresFullResponses(body []byte) bool {
	if len(body) == 0 || !gjson.ValidBytes(body) {
		return false
	}
	var containsHostedType func(gjson.Result) bool
	containsHostedType = func(value gjson.Result) bool {
		if !value.Exists() || !value.IsObject() {
			return false
		}
		if isOpenAIResponsesLiteHostedToolType(value.Get("type").String()) {
			return true
		}
		nestedTools := value.Get("tools")
		if !nestedTools.IsArray() {
			return false
		}
		for _, nested := range nestedTools.Array() {
			if containsHostedType(nested) {
				return true
			}
		}
		return false
	}
	tools := gjson.GetBytes(body, "tools")
	if tools.IsArray() {
		for _, tool := range tools.Array() {
			if containsHostedType(tool) {
				return true
			}
		}
	}
	toolChoice := gjson.GetBytes(body, "tool_choice")
	if containsHostedType(toolChoice) {
		return true
	}
	if toolChoice.Type == gjson.String {
		if isOpenAIResponsesLiteHostedToolType(toolChoice.String()) {
			return true
		}
	}
	input := openAIBodyGet(body, "input")
	if input.IsArray() {
		for _, item := range input.Array() {
			itemType := strings.TrimSpace(item.Get("type").String())
			if isOpenAIResponsesLiteHostedToolCallType(itemType) {
				return true
			}
		}
	}
	return false
}

func liteRequiresFullRandomBody(rng *rand.Rand) []byte {
	types := []string{`"message"`, `"reasoning"`, `"custom_tool_call"`, `"web_search_call"`, `" image_generation_call "`,
		`"computer_call_output"`, `"mcp_list_tools"`, `7`, `null`, `"file_search_call"`, `"tool_search_call"`}
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.6-luna"`)
	if rng.Intn(3) == 0 {
		_, _ = b.WriteString(`,"tools":[{"type":"function","name":"f"},{"type":"namespace","tools":[{"type":` +
			[]string{`"function"`, `"web_search"`}[rng.Intn(2)] + `}]}]`)
	}
	if rng.Intn(4) == 0 {
		_, _ = b.WriteString(`,"tool_choice":` + []string{`"auto"`, `"web_search"`, `{"type":"file_search"}`, `{"type":"function"}`}[rng.Intn(4)])
	}
	switch rng.Intn(5) {
	case 0:
		_, _ = b.WriteString(`,"input":"text"`)
	case 1:
	default:
		_, _ = b.WriteString(`,"input":[`)
		for i, n := 0, rng.Intn(8); i < n; i++ {
			if i > 0 {
				_ = b.WriteByte(',')
			}
			if rng.Intn(6) == 0 {
				_, _ = b.WriteString(`"loose"`)
				continue
			}
			_, _ = b.WriteString(`{"type":` + types[rng.Intn(len(types))] + `,"id":"x"}`)
		}
		_ = b.WriteByte(']')
	}
	_ = b.WriteByte('}')
	return []byte(b.String())
}

func TestOpenAIResponsesLiteRequiresFullResponsesMatchesLegacy(t *testing.T) {
	rng := rand.New(rand.NewSource(20261008))
	fixtures := map[string][]byte{
		"official":       newOfficialOpenAIHTTPTestBody(t, true, true, true),
		"memory_profile": buildOfficialEgressMemoryProfileBody(t, 128<<10),
		"hosted_last":    []byte(`{"input":[{"type":"message"},{"type":"reasoning"},{"type":"web_search_call"}]}`),
		"invalid":        []byte(`{"input":[{"type":"web_search_call"}`),
		"empty":          nil,
	}
	for name, body := range fixtures {
		require.Equal(t, legacyOpenAIResponsesLiteRequiresFullResponses(body), openAIResponsesLiteRequiresFullResponses(body), name)
	}
	hosted := 0
	for round := 0; round < 3000; round++ {
		body := liteRequiresFullRandomBody(rng)
		want := legacyOpenAIResponsesLiteRequiresFullResponses(body)
		require.Equal(t, want, openAIResponsesLiteRequiresFullResponses(body), "random#%d %s", round, body)
		mutated := decodeMutate(rng, body)
		require.Equal(t, legacyOpenAIResponsesLiteRequiresFullResponses(mutated), openAIResponsesLiteRequiresFullResponses(mutated),
			"mutated#%d %s", round, mutated)
		if want {
			hosted++
		}
	}
	require.Greater(t, hosted, 300, "夹具必须覆盖 hosted 工具形态")
}

func TestOpenAIResponsesLiteRequiresFullResponsesDoesNotAllocatePerInputItem(t *testing.T) {
	body := buildOfficialEgressMemoryProfileBody(t, 8<<20)
	allocated := testMeasureAllocatedBytes(t, func() {
		require.False(t, openAIResponsesLiteRequiresFullResponses(body))
	})
	legacy := testMeasureAllocatedBytes(t, func() {
		require.False(t, legacyOpenAIResponsesLiteRequiresFullResponses(body))
	})
	t.Logf("正文 %.1f MiB：Lite 能力判定分配 %d → %d 字节", float64(len(body))/(1<<20), legacy, allocated)
	require.Less(t, allocated, uint64(64<<10), "逐项遍历不得按 input 长度分配")
	require.Greater(t, legacy, uint64(256<<10), "对照：改造前按 input 长度分配结果切片")
}
