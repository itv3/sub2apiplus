package service

import (
	"bytes"
	"fmt"
	"math/rand"
	"strconv"
	"strings"
	"testing"
	"unsafe"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 本文件锁定问题四 M2-b：normalizeOpenAIResponsesReasoningContentReplay 不再为判定复制整段 input，
// 改写路径也不再把整段正文复制进对象树。输出字节、是否改写、错误文本都必须与改造前完全一致。

// legacyNormalizeOpenAIResponsesReasoningContentReplay 是改造前的实现，原样保留作差分基准。
func legacyNormalizeOpenAIResponsesReasoningContentReplay(body []byte) ([]byte, bool, error) {
	input := gjson.GetBytes(body, "input")
	if !input.IsArray() {
		return body, false, nil
	}

	needsNormalization := false
	input.ForEach(func(_, item gjson.Result) bool {
		if strings.TrimSpace(item.Get("type").String()) != "reasoning" {
			return true
		}
		content := item.Get("content")
		if content.IsArray() && len(content.Array()) > 0 {
			needsNormalization = true
			return false
		}
		return true
	})
	if !needsNormalization {
		return body, false, nil
	}

	var reqBody map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &reqBody); err != nil {
		return body, false, fmt.Errorf("normalize OpenAI reasoning content replay: %w", err)
	}
	items, ok := reqBody["input"].([]any)
	if !ok {
		return body, false, nil
	}
	changed := false
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok || strings.TrimSpace(firstNonEmptyString(item["type"])) != "reasoning" {
			continue
		}
		content, ok := item["content"].([]any)
		if !ok || len(content) == 0 {
			continue
		}
		delete(item, "content")
		changed = true
	}
	if !changed {
		return body, false, nil
	}
	normalized, err := marshalOpenAIUpstreamJSON(reqBody)
	if err != nil {
		return body, false, fmt.Errorf("serialize normalized OpenAI reasoning content replay: %w", err)
	}
	return normalized, true, nil
}

func reasoningReplayCompare(t *testing.T, name string, body []byte) {
	t.Helper()
	legacyOut, legacyChanged, legacyErr := legacyNormalizeOpenAIResponsesReasoningContentReplay(body)
	out, changed, err := normalizeOpenAIResponsesReasoningContentReplay(body)
	if legacyErr != nil || err != nil {
		require.Error(t, legacyErr, "%s：新实现报错但旧实现没有：%v", name, err)
		require.Error(t, err, "%s：旧实现报错但新实现没有：%v", name, legacyErr)
		require.Equal(t, legacyErr.Error(), err.Error(), "%s：错误文本必须一致", name)
		return
	}
	require.Equal(t, legacyChanged, changed, "%s：是否改写必须一致", name)
	require.True(t, bytes.Equal(legacyOut, out), "%s：输出必须逐字节一致\n旧=%s\n新=%s", name, legacyOut, out)
	if !changed {
		require.Equal(t, unsafe.SliceData(body), unsafe.SliceData(out), "%s：未改写时必须原样返回同一正文", name)
		return
	}
	require.False(t, len(out) > 0 && len(body) > 0 && requestViewStringInBody(body, unsafe.String(&out[0], 1)),
		"%s：改写结果必须是独立的新缓冲", name)
}

// reasoningReplayRandomBody 生成围绕 reasoning 项的随机请求：content 缺失、为空数组、非空数组、
// 非数组、null、重复键；type 带空白、转义或不是字符串；input 缺失、不是数组或重复出现。
func reasoningReplayRandomBody(rng *rand.Rand) []byte {
	contents := []string{
		``, `,"content":[]`, `,"content":[{"type":"reasoning_text","text":"visible"}]`,
		`,"content":"text"`, `,"content":null`, `,"content":{"a":1}`,
		`,"content":[],"content":[{"type":"x"}]`, `,"content":[{"t":1}],"content":[]`,
		`,"content":[1,2,3]`, `,"content":[ ]`,
	}
	types := []string{`"reasoning"`, `" reasoning "`, `"reasoning"`, `"Reasoning"`, `"message"`, `7`, `null`, `["reasoning"]`}
	item := func() string {
		switch rng.Intn(8) {
		case 0:
			return `"loose"`
		case 1:
			return `12`
		case 2:
			return `{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}`
		default:
			encrypted := ""
			if rng.Intn(2) == 0 {
				encrypted = `,"encrypted_content":"gAAAAAB` + strings.Repeat("QUJD", rng.Intn(400)) + `"`
			}
			summary := ""
			if rng.Intn(2) == 0 {
				summary = `,"summary":[{"type":"summary_text","text":"s"}]`
			}
			return `{"type":` + types[rng.Intn(len(types))] + `,"id":"rs_1"` + summary + encrypted + contents[rng.Intn(len(contents))] + `}`
		}
	}
	input := func() string {
		switch rng.Intn(6) {
		case 0:
			return `"plain"`
		case 1:
			return `null`
		default:
			parts := make([]string, 0, 6)
			for n := rng.Intn(6); n > 0; n-- {
				parts = append(parts, item())
			}
			return `[` + strings.Join(parts, ",") + `]`
		}
	}
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.4","stream":true`)
	switch rng.Intn(5) {
	case 0:
	case 1:
		_, _ = b.WriteString(`,"input":` + input() + `,"input":` + input())
	default:
		_, _ = b.WriteString(`,"input":` + input())
	}
	if rng.Intn(3) == 0 {
		_, _ = b.WriteString(`,"big":9007199254740993,"tools":[{"type":"function","name":"f","parameters":{"type":"object"}}],"html":"<a&b>"`)
	}
	_, _ = b.WriteString(`}`)
	return []byte(b.String())
}

func TestNormalizeOpenAIResponsesReasoningContentReplayMatchesLegacy(t *testing.T) {
	fixtures := map[string][]byte{
		"no_input":           []byte(`{"model":"gpt-5.4"}`),
		"input_string":       []byte(`{"input":"hello"}`),
		"no_reasoning":       []byte(`{"input":[{"type":"message","content":"x"}]}`),
		"empty_content":      []byte(`{"input":[{"type":"reasoning","content":[],"summary":[]}]}`),
		"visible_content":    []byte(`{"z":1,"a":2,"input":[{"type":"reasoning","id":"rs_1","encrypted_content":"e","content":[{"type":"reasoning_text","text":"t"}],"summary":[]}],"html":"<&>","n":1.50}`),
		"duplicate_input":    []byte(`{"input":[{"type":"reasoning","content":[1]}],"input":[{"type":"message"}]}`),
		"duplicate_input_2":  []byte(`{"input":[{"type":"message"}],"input":[{"type":"reasoning","content":[1]}]}`),
		"duplicate_content":  []byte(`{"input":[{"type":"reasoning","content":[1],"content":[]}]}`),
		"escaped_type":       []byte(`{"input":[{"type":"reasoning","content":["a"]}]}`),
		"padded_type":        []byte(`{"input":[{"type":"  reasoning\t","content":["a"]}]}`),
		"invalid_after_hit":  []byte(`{"input":[{"type":"reasoning","content":[1]}],"x":}`),
		"trailing_value":     []byte(`{"input":[{"type":"reasoning","content":[1]}]} {}`),
		"top_level_array":    []byte(`[{"input":[{"type":"reasoning","content":[1]}]}]`),
		"top_level_null":     []byte(`null`),
		"empty":              nil,
		"official_lite":      newOfficialOpenAIHTTPTestBody(t, true, false, true),
		"official_non_lite":  newOfficialOpenAIHTTPTestBody(t, false, true, false),
		"memory_profile_256": buildOfficialEgressMemoryProfileBody(t, 256<<10),
	}
	for i, fixture := range officialJSONDecodeFixtures {
		fixtures["decode_valid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		fixtures["decode_invalid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for name, body := range fixtures {
		reasoningReplayCompare(t, name, body)
	}
	rng := rand.New(rand.NewSource(20260929))
	for round := 0; round < 2000; round++ {
		body := reasoningReplayRandomBody(rng)
		reasoningReplayCompare(t, "reasoning#"+strconv.Itoa(round), body)
		reasoningReplayCompare(t, "reasoning-mutated#"+strconv.Itoa(round), decodeMutate(rng, body))
		reasoningReplayCompare(t, "random#"+strconv.Itoa(round), []byte(decodeRandomJSON(rng, 0)))
		reasoningReplayCompare(t, "request#"+strconv.Itoa(round), decodeSharingRandomBody(rng))
	}
}

// reasoningReplayLargeBody 生成含大量加密推理内容的大正文；withVisibleContent 为 true 时混入一条带
// 可见 content 的 reasoning 项，触发改写路径。
func reasoningReplayLargeBody(t *testing.T, withVisibleContent bool) []byte {
	t.Helper()
	encrypted := `"gAAAAAB` + strings.Repeat("QUJDREVGR0hJSktMTU5PUA", 1000) + `"`
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.4","input":[`)
	for i := 0; i < 400; i++ {
		if i > 0 {
			_ = b.WriteByte(',')
		}
		_, _ = b.WriteString(`{"type":"reasoning","summary":[],"encrypted_content":` + encrypted + `}`)
		_, _ = b.WriteString(`,{"type":"message","role":"user","content":[{"type":"input_text","text":"turn ` + strconv.Itoa(i) + `"}]}`)
	}
	if withVisibleContent {
		_, _ = b.WriteString(`,{"type":"reasoning","summary":[],"content":[{"type":"reasoning_text","text":"visible"}]}`)
	}
	_, _ = b.WriteString(`]}`)
	body := []byte(b.String())
	require.Greater(t, len(body), 8<<20)
	return body
}

func TestNormalizeOpenAIResponsesReasoningContentReplayJudgesWithoutCopyingInput(t *testing.T) {
	body := reasoningReplayLargeBody(t, false)
	var changed bool
	allocated := testMeasureAllocatedBytes(t, func() {
		var err error
		_, changed, err = normalizeOpenAIResponsesReasoningContentReplay(body)
		require.NoError(t, err)
	})
	require.False(t, changed)
	require.Less(t, allocated, uint64(len(body))/64,
		"无需改写时只做只读判定，分配 %d 字节，不得再复制整段 input（正文 %d 字节）", allocated, len(body))

	legacyAllocated := testMeasureAllocatedBytes(t, func() {
		_, _, err := legacyNormalizeOpenAIResponsesReasoningContentReplay(body)
		require.NoError(t, err)
	})
	require.Greater(t, legacyAllocated, uint64(len(body))*9/10, "对照：改造前的判定复制整段 input")
}

func TestNormalizeOpenAIResponsesReasoningContentReplayRewritesWithoutCopyingBodyIntoTree(t *testing.T) {
	body := reasoningReplayLargeBody(t, true)
	var out []byte
	allocated := testMeasureAllocatedBytes(t, func() {
		var changed bool
		var err error
		out, changed, err = normalizeOpenAIResponsesReasoningContentReplay(body)
		require.NoError(t, err)
		require.True(t, changed)
	})
	legacyOut, _, err := legacyNormalizeOpenAIResponsesReasoningContentReplay(body)
	require.NoError(t, err)
	require.True(t, bytes.Equal(legacyOut, out))
	// 对象树只含结构与短字符串，长字符串引用正文；输出按规范化编码重写，由共享的
	// marshalOpenAIUpstreamJSON 完成，其编码缓冲的分配不在本项范围内。
	treeAllocated := testMeasureAllocatedBytes(t, func() {
		payload, err := decodeOpenAIReasoningContentReplayBody(body)
		require.NoError(t, err)
		require.NotEmpty(t, payload)
	})
	require.Less(t, treeAllocated, uint64(len(body))/2,
		"改写用的对象树分配 %d 字节，不得再把正文（%d 字节）复制进树", treeAllocated, len(body))
	legacyAllocated := testMeasureAllocatedBytes(t, func() {
		_, _, err := legacyNormalizeOpenAIResponsesReasoningContentReplay(body)
		require.NoError(t, err)
	})
	require.Greater(t, legacyAllocated, allocated+uint64(len(body))*2,
		"对照：改造前的改写路径多出判定副本与 encoding/json 解码（含字符串复制与读取缓冲倍增）")
}
