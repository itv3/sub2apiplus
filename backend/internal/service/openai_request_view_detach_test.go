package service

import (
	"math/rand"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unsafe"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 本文件锁定问题四 M2 发现的一处隐性副本：newOpenAIRequestView 以零拷贝视图遍历正文时，提取出的
// Model、PromptCacheKey 等短字符串曾直接引用正文内存。Forward 在正文被改写（Lite 归一化、字段
// 补丁、重编码）后仍一路使用这些字段，几十字节的模型名就把改写前的整段旧正文钉到请求结束。
// 改造后这些字段复制保存：提取结果必须与改造前的零拷贝实现逐字段相同，且不再引用正文。

// legacyNewOpenAIRequestViewZeroCopy 是改造前的 newOpenAIRequestView，原样保留作差分基准。
func legacyNewOpenAIRequestViewZeroCopy(body []byte) openAIRequestView {
	if len(body) == 0 {
		return openAIRequestView{}
	}

	const (
		modelField uint8 = 1 << iota
		streamField
		promptCacheKeyField
		previousResponseIDField
		serviceTierField
		reasoningField
		allRequestViewFields = modelField | streamField | promptCacheKeyField |
			previousResponseIDField | serviceTierField | reasoningField
	)

	view := openAIRequestView{body: body}
	var seen uint8
	parseRawJSONView(body).ForEach(func(key, value gjson.Result) bool {
		switch key.Str {
		case "model":
			if seen&modelField == 0 {
				view.Model = strings.TrimSpace(value.String())
				seen |= modelField
			}
		case "stream":
			if seen&streamField == 0 {
				view.Stream = value.Bool()
				seen |= streamField
			}
		case "prompt_cache_key":
			if seen&promptCacheKeyField == 0 {
				view.PromptCacheKey = strings.TrimSpace(value.String())
				seen |= promptCacheKeyField
			}
		case "previous_response_id":
			if seen&previousResponseIDField == 0 {
				view.PreviousResponseID = strings.TrimSpace(value.String())
				seen |= previousResponseIDField
			}
		case "service_tier":
			if seen&serviceTierField == 0 {
				view.ServiceTier = strings.TrimSpace(value.String())
				seen |= serviceTierField
			}
		case "reasoning":
			if seen&reasoningField == 0 {
				view.ReasoningEffort = strings.TrimSpace(value.Get("effort").String())
				seen |= reasoningField
			}
		}
		return seen != allRequestViewFields
	})
	return view
}

func requestViewStringInBody(body []byte, value string) bool {
	if value == "" || len(body) == 0 {
		return false
	}
	begin := uintptr(unsafe.Pointer(unsafe.SliceData(body)))
	end := begin + uintptr(len(body))
	pointer := uintptr(unsafe.Pointer(unsafe.StringData(value)))
	return pointer >= begin && pointer < end
}

func requestViewDetachCompare(t *testing.T, name string, body []byte) {
	t.Helper()
	legacy := legacyNewOpenAIRequestViewZeroCopy(body)
	current := newOpenAIRequestView(body)
	require.Equal(t, legacy.Model, current.Model, "%s：Model 必须一致", name)
	require.Equal(t, legacy.Stream, current.Stream, "%s：Stream 必须一致", name)
	require.Equal(t, legacy.PromptCacheKey, current.PromptCacheKey, "%s：PromptCacheKey 必须一致", name)
	require.Equal(t, legacy.PreviousResponseID, current.PreviousResponseID, "%s：PreviousResponseID 必须一致", name)
	require.Equal(t, legacy.ServiceTier, current.ServiceTier, "%s：ServiceTier 必须一致", name)
	require.Equal(t, legacy.ReasoningEffort, current.ReasoningEffort, "%s：ReasoningEffort 必须一致", name)
	require.Equal(t, len(legacy.body), len(current.body), "%s：视图仍持有同一正文", name)
	require.Equal(t, unsafe.SliceData(legacy.body), unsafe.SliceData(current.body), "%s：视图仍持有同一正文", name)
	require.Nil(t, current.patches, name)
	require.False(t, current.patchesDisabled, name)
	for field, value := range map[string]string{
		"Model": current.Model, "PromptCacheKey": current.PromptCacheKey,
		"PreviousResponseID": current.PreviousResponseID, "ServiceTier": current.ServiceTier,
		"ReasoningEffort": current.ReasoningEffort,
	} {
		require.False(t, requestViewStringInBody(body, value), "%s：%s 不得再引用正文内存", name, field)
	}
}

// requestViewDetachRandomBody 生成覆盖视图全部字段的随机顶层对象：字段可能缺失、重复、类型不符、
// 带首尾空白或转义，reasoning 可能不是对象，混入 input 等大字段。
func requestViewDetachRandomBody(rng *rand.Rand) []byte {
	values := []string{
		`"gpt-5.6-luna"`, `"  gpt-5.4-high  "`, `"a\"b"`, `"中文"`, `""`, `"   "`, `null`, `true`, `false`,
		`12`, `-0.5e3`, `[1,"x"]`, `{"effort":"high"}`, `{"effort":"  xhigh "}`, `{"effort":7}`, `{}`,
		`{"effort":"low","effort":"high"}`, `"priority"`, `"resp_` + strings.Repeat("z", 40) + `"`,
	}
	keys := []string{"model", "stream", "prompt_cache_key", "previous_response_id", "service_tier", "reasoning", "input", "tools", "model"}
	var b strings.Builder
	_ = b.WriteByte('{')
	for i, n := 0, rng.Intn(12); i < n; i++ {
		if i > 0 {
			_ = b.WriteByte(',')
		}
		_, _ = b.WriteString([]string{"", " ", "\n"}[rng.Intn(3)])
		key := keys[rng.Intn(len(keys))]
		_, _ = b.WriteString(`"` + key + `":`)
		if key == "input" && rng.Intn(2) == 0 {
			_, _ = b.WriteString(`[{"type":"message","content":"` + strings.Repeat("长", 50+rng.Intn(200)) + `"}]`)
			continue
		}
		_, _ = b.WriteString(values[rng.Intn(len(values))])
	}
	_ = b.WriteByte('}')
	return []byte(b.String())
}

func TestNewOpenAIRequestViewMatchesLegacyAndDoesNotReferenceBody(t *testing.T) {
	fixtures := map[string][]byte{
		"official_lite":      newOfficialOpenAIHTTPTestBody(t, true, false, true),
		"official_non_lite":  newOfficialOpenAIHTTPTestBody(t, false, true, false),
		"all_fields":         []byte(`{"model":"  gpt-5.6-luna ","stream":true,"prompt_cache_key":" k ","previous_response_id":"resp_1","service_tier":" priority ","reasoning":{"effort":" high ","summary":"auto"},"input":[{"type":"message","content":"x"}]}`),
		"duplicates":         []byte(`{"model":"a","model":"b","reasoning":{"effort":"low"},"reasoning":{"effort":"high"},"service_tier":"flex","service_tier":"auto"}`),
		"escaped":            []byte(`{"model":"gpt-5","prompt_cache_key":"a\"b","service_tier":"pri\nority"}`),
		"non_string_values":  []byte(`{"model":12,"stream":"true","prompt_cache_key":null,"previous_response_id":["x"],"service_tier":{"a":1},"reasoning":"high"}`),
		"empty_object":       []byte(`{}`),
		"empty_body":         nil,
		"top_level_array":    []byte(`[{"model":"x"}]`),
		"invalid_truncated":  []byte(`{"model":"x","reasoning":{"effort":`),
		"whitespace_wrapped": []byte("\n  {\"model\" : \"m\" , \"stream\" : false }  \n"),
	}
	for i, fixture := range officialJSONDecodeFixtures {
		fixtures["decode_valid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		fixtures["decode_invalid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for name, body := range fixtures {
		requestViewDetachCompare(t, name, body)
	}
	rng := rand.New(rand.NewSource(20260929))
	for round := 0; round < 1500; round++ {
		body := requestViewDetachRandomBody(rng)
		requestViewDetachCompare(t, "view#"+strconv.Itoa(round), body)
		requestViewDetachCompare(t, "view-mutated#"+strconv.Itoa(round), decodeMutate(rng, body))
		requestViewDetachCompare(t, "random#"+strconv.Itoa(round), []byte(decodeRandomJSON(rng, 0)))
		requestViewDetachCompare(t, "request#"+strconv.Itoa(round), decodeSharingRandomBody(rng))
	}
}

// requestViewDetachTrackedFields 模拟 Forward：用一段大正文建立视图，只保留提取出的字段（Forward
// 在正文被改写后会以新正文重建视图，但 reqModel、promptCacheKey 等字段一路沿用），并在正文
// 底层数组被回收时置位 collected。
//
//go:noinline
func requestViewDetachTrackedFields(build func([]byte) openAIRequestView, collected *atomic.Bool) openAIRequestView {
	body := []byte(`{"model":"gpt-5.6-luna","stream":true,"prompt_cache_key":"session-1",` +
		`"previous_response_id":"resp_1","service_tier":"priority","reasoning":{"effort":"high"},` +
		`"input":"` + strings.Repeat("x", 4<<20) + `"}`)
	runtime.AddCleanup(&body[0], func(flag *atomic.Bool) { flag.Store(true) }, collected)
	view := build(body)
	view.body = nil
	return view
}

func requestViewDetachWaitCollected(collected *atomic.Bool, attempts int) bool {
	for i := 0; i < attempts && !collected.Load(); i++ {
		runtime.GC()
		time.Sleep(5 * time.Millisecond)
	}
	return collected.Load()
}

func TestNewOpenAIRequestViewDoesNotPinRewrittenBody(t *testing.T) {
	var collected atomic.Bool
	view := requestViewDetachTrackedFields(newOpenAIRequestView, &collected)
	require.True(t, requestViewDetachWaitCollected(&collected, 200),
		"视图只剩提取出的字段后，正文必须可以回收")
	require.Equal(t, "gpt-5.6-luna", view.Model)
	require.Equal(t, "session-1", view.PromptCacheKey)
	require.Equal(t, "high", view.ReasoningEffort)
	runtime.KeepAlive(view)

	// 对照：改造前的零拷贝实现里，同样只剩提取出的字段时正文仍然可达，永远不会被回收。
	var legacyCollected atomic.Bool
	legacyView := requestViewDetachTrackedFields(legacyNewOpenAIRequestViewZeroCopy, &legacyCollected)
	require.False(t, requestViewDetachWaitCollected(&legacyCollected, 20),
		"对照组：零拷贝子串会把整段正文钉住")
	runtime.KeepAlive(legacyView)
}

func TestNewOpenAIRequestViewAllocatesOnlyExtractedFields(t *testing.T) {
	body := testBuildLargeOfficialOpenAIBody(t, 32, 128*1024)
	var view openAIRequestView
	allocated := testMeasureAllocatedBytes(t, func() {
		view = newOpenAIRequestView(body)
	})
	require.NotEmpty(t, view.Model)
	require.Less(t, allocated, uint64(4096),
		"视图只复制几个短字段，分配 %d 字节，不得随正文体积（%d 字节）增长", allocated, len(body))
}
