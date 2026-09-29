package service

import (
	"encoding/json"
	"math/rand"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"unsafe"

	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M1 第二项：Finalizer 在正文索引上解码时，无转义的长字符串以只读视图
// 引用正文而不复制。解码结果必须与 decodeOfficialJSONObjectUseNumber 逐项相等（含错误文本），
// 只有满足条件的长字符串引用正文，定型输出与改造前逐字节一致，且不再把整段正文复制第二遍。

func decodeSharingBodyCompare(t *testing.T, name string, body []byte) {
	t.Helper()
	expected, expectedErr := decodeOfficialJSONObjectUseNumber(body)
	payload, index, err := decodeOfficialJSONObjectSharingBody(body)
	if decodeSharingErrorParity(t, name, expectedErr, err) {
		require.Equal(t, expectedErr.Error(), err.Error(), "%s：错误文本必须一致", name)
		require.Nil(t, index, name)
		return
	}
	require.True(t, reflect.DeepEqual(expected, payload),
		"%s：引用正文的解码结果必须与普通解码逐项相等\nexpected=%#v\nactual=%#v", name, expected, payload)
	if index != nil {
		require.Equal(t, unsafe.Pointer(unsafe.SliceData(body)), unsafe.Pointer(unsafe.SliceData(index.body)),
			"%s：返回的索引必须属于同一正文", name)
	}
}

// decodeSharingBodyLongStringDocument 生成含长字符串的随机顶层对象：无转义、含转义、
// 非法 UTF-8、恰在阈值边界、重复键遮蔽等形态混合出现。
func decodeSharingBodyLongStringDocument(rng *rand.Rand) []byte {
	lengths := []int{
		officialJSONSharedStringMinBytes - 1, officialJSONSharedStringMinBytes,
		officialJSONSharedStringMinBytes + 1, 4096,
	}
	values := func() string {
		length := lengths[rng.Intn(len(lengths))]
		switch rng.Intn(5) {
		case 0:
			return `"` + strings.Repeat("a", length) + `"`
		case 1:
			return `"` + strings.Repeat("中", length/3+1) + `"`
		case 2:
			return `"` + strings.Repeat("x", length) + `\n"`
		case 3:
			return "\"" + strings.Repeat("y", length) + "\xff\""
		default:
			return `"` + strings.Repeat("QUJD", length/4+1) + `"`
		}
	}
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.6-luna","input":[`)
	for n := rng.Intn(5); n > 0; n-- {
		_, _ = b.WriteString(`{"type":"reasoning","encrypted_content":` + values() + `},`)
	}
	_, _ = b.WriteString(`{"type":"message","content":[{"type":"input_text","text":` + values() + `}]}]`)
	if rng.Intn(2) == 0 {
		_, _ = b.WriteString(`,"instructions":` + values() + `,"instructions":` + values())
	}
	_, _ = b.WriteString(`}`)
	return []byte(b.String())
}

func TestDecodeOfficialJSONObjectSharingBodyMatchesDecode(t *testing.T) {
	for i, fixture := range officialJSONDecodeFixtures {
		decodeSharingBodyCompare(t, "valid#"+strconv.Itoa(i), []byte(fixture))
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		decodeSharingBodyCompare(t, "invalid#"+strconv.Itoa(i), []byte(fixture))
	}
	for _, fixture := range []string{`[1,{"a":"b"},[]]`, `"text"`, `12.5`, `null`, ` [] `} {
		decodeSharingBodyCompare(t, "scalar "+fixture, []byte(fixture))
	}
	for _, depth := range []int{9999, 10000, 10001} {
		body := []byte(`{"a":` + strings.Repeat("[", depth-1) + strings.Repeat("]", depth-1) + `}`)
		decodeSharingBodyCompare(t, "depth "+strconv.Itoa(depth), body)
	}
	rng := rand.New(rand.NewSource(20260929))
	for round := 0; round < 1500; round++ {
		body := []byte(decodeRandomJSON(rng, 0))
		decodeSharingBodyCompare(t, "random#"+strconv.Itoa(round), body)
		decodeSharingBodyCompare(t, "mutated#"+strconv.Itoa(round), decodeMutate(rng, body))
		decodeSharingBodyCompare(t, "request#"+strconv.Itoa(round), decodeSharingRandomBody(rng))
		long := decodeSharingBodyLongStringDocument(rng)
		decodeSharingBodyCompare(t, "long#"+strconv.Itoa(round), long)
		decodeSharingBodyCompare(t, "long-mutated#"+strconv.Itoa(round), decodeMutate(rng, long))
	}
}

func TestDecodeOfficialJSONObjectSharingBodyReferencesOnlyLongPlainStrings(t *testing.T) {
	plain := strings.Repeat("A", officialJSONSharedStringMinBytes)
	short := strings.Repeat("B", officialJSONSharedStringMinBytes-1)
	escaped := strings.Repeat("C", officialJSONSharedStringMinBytes) + `\n`
	invalid := strings.Repeat("D", officialJSONSharedStringMinBytes) + "\xff"
	body := []byte(`{"plain":"` + plain + `","short":"` + short + `","escaped":"` + escaped +
		`","invalid":"` + invalid + `","nested":{"items":["` + plain + `"]}}`)
	payload, index, err := decodeOfficialJSONObjectSharingBody(body)
	require.NoError(t, err)
	require.NotNil(t, index)
	begin := uintptr(unsafe.Pointer(unsafe.SliceData(body)))
	end := begin + uintptr(len(body))
	inBody := func(value any) bool {
		text, ok := value.(string)
		require.True(t, ok)
		pointer := uintptr(unsafe.Pointer(unsafe.StringData(text)))
		return pointer >= begin && pointer < end
	}
	require.True(t, inBody(payload["plain"]), "无转义长字符串必须直接引用正文")
	nested, ok := payload["nested"].(map[string]any)
	require.True(t, ok)
	items, ok := nested["items"].([]any)
	require.True(t, ok)
	require.True(t, inBody(items[0]), "嵌套的无转义长字符串必须直接引用正文")
	require.False(t, inBody(payload["short"]), "短字符串必须复制，避免小值把整段正文钉在内存里")
	require.False(t, inBody(payload["escaped"]), "含转义的字符串必须按反转义结果复制")
	require.False(t, inBody(payload["invalid"]), "非法 UTF-8 字符串必须按 encoding/json 规则替换后复制")
	require.Equal(t, strings.Repeat("C", officialJSONSharedStringMinBytes)+"\n", payload["escaped"])
	require.Equal(t, strings.Repeat("D", officialJSONSharedStringMinBytes)+"�", payload["invalid"])
}

// decodeSharingBodyFinalizeInputs 生成 Finalizer 差分用的请求正文：官方夹具、派生入口形态、
// 带长字符串的形态与随机请求。
func decodeSharingBodyFinalizeInputs(t *testing.T, rng *rand.Rand) map[string][]byte {
	t.Helper()
	longText := strings.Repeat("长指令 long instructions ", 128)
	longTool := `{"type":"function","name":"f","description":"` + strings.Repeat("d", 2048) + `","parameters":{"type":"object"}}`
	bodies := map[string][]byte{
		"large":    officialBodySharingLargeRequestBody(t, 8, 8*1024),
		"guardian": newOfficialOpenAIGuardianHTTPBody(t),
		"derived": []byte(`{"model":"gpt-5.6-luna","input":"hi","instructions":"系统指令","tools":[{"type":"function","name":"f","parameters":{"type":"object"}}],` +
			`"max_output_tokens":5,"reasoning":{"effort":"ultra","summary":"Detailed"},"text":{"verbosity":"high"},"parallel_tool_calls":true,"truncation":"auto"}`),
		"derived_long": []byte(`{"model":"gpt-5.6-luna","input":[{"role":"user","content":"` + strings.Repeat("u", 4096) + `"}],"instructions":"` + longText + `",` +
			`"tools":[` + longTool + `,{"type":"namespace","name":"ns","tools":[` + longTool + `]}],"parallel_tool_calls":false}`),
		"derived_messages": []byte(`{"model":"gpt-5.5","input":[{"role":"user","content":"a"},{"type":"function_call","call_id":"c1","name":"f","arguments":"{}"},` +
			`{"type":"function_call_output","call_id":"c1","output":"o"}],"include":[],"store":true,"stream":false,"tool_choice":"required","text":null}`),
		"namespace": []byte(`{"model":"gpt-5.6-luna","input":[{"type":"message","role":"user","content":"x"}],"tools":[{"type":"namespace","name":"ns","tools":[{"type":"function","name":"g"}]}],` +
			`"reasoning":null,"parallel_tool_calls":false}`),
	}
	for _, stream := range []bool{false, true} {
		for _, explicit := range []bool{false, true} {
			for _, tool := range []bool{false, true} {
				name := "official stream=" + strconv.FormatBool(stream) + " explicit=" + strconv.FormatBool(explicit) +
					" tool=" + strconv.FormatBool(tool)
				bodies[name] = newOfficialOpenAIHTTPTestBody(t, stream, explicit, tool)
			}
		}
	}
	for round := 0; round < 150; round++ {
		bodies["random#"+strconv.Itoa(round)] = decodeSharingRandomBody(rng)
	}
	for round := 0; round < 30; round++ {
		bodies["long#"+strconv.Itoa(round)] = decodeSharingBodyLongStringDocument(rng)
	}
	return bodies
}

func TestFinalizeOfficialOpenAIHTTPBodySharingDecodeMatchesLegacy(t *testing.T) {
	rng := rand.New(rand.NewSource(20260930))
	defaultsVariants := []officialOpenAIReasoningDefaults{
		{},
		{Effort: "high", Summary: "auto", SupportsSummary: true, Known: true},
		{Effort: "Ultra", Summary: "none", SupportsSummary: false, Known: true},
	}
	compared := 0
	for name, body := range decodeSharingBodyFinalizeInputs(t, rng) {
		if _, err := captureOfficialOpenAIHTTPBodyContract(body); err != nil {
			continue
		}
		for _, compact := range []bool{false, true} {
			for _, lite := range []bool{false, true} {
				for _, parallel := range []bool{false, true} {
					for defaultsIndex, defaults := range defaultsVariants {
						options := officialOpenAIHTTPBodyOptions{
							IsCompact: compact, UseResponsesLite: lite, SupportsParallelTools: parallel,
							ProfileMode: officialClientProfileModeActive,
						}
						label := name + " compact=" + strconv.FormatBool(compact) +
							" lite=" + strconv.FormatBool(lite) + " parallel=" + strconv.FormatBool(parallel) +
							" defaults=" + strconv.Itoa(defaultsIndex)
						legacyContract, err := captureOfficialOpenAIHTTPBodyContract(body)
						require.NoError(t, err)
						legacy, legacyModified, legacyErr := finalizeOfficialOpenAIHTTPBody(
							body, legacyContract, officialOpenAIHTTPIdentity{}, defaults, options,
						)
						currentContract, err := captureOfficialOpenAIHTTPBodyContract(body)
						require.NoError(t, err)
						payload, index, decodeErr := decodeOfficialJSONObjectSharingBody(body)
						require.NoError(t, decodeErr, label)
						current, currentModified, currentErr := finalizeOfficialOpenAIHTTPBodyPayloadWithIndex(
							payload, body, index, currentContract, officialOpenAIHTTPIdentity{}, defaults, options,
						)
						if decodeSharingErrorParity(t, label, legacyErr, currentErr) {
							require.Equal(t, legacyErr.Error(), currentErr.Error(), label)
							continue
						}
						require.Equal(t, legacyModified, currentModified, label)
						require.Equal(t, string(legacy), string(current), "%s：定型正文必须逐字节一致", label)
						require.True(t, json.Valid(current), label)
						compared++
					}
				}
			}
		}
	}
	require.Greater(t, compared, 1000, "差分样本过少，夹具可能失效")
}

func TestFinalizerDecodeSharingBodyDoesNotCopyLargeBody(t *testing.T) {
	body := officialBodySharingLargeRequestBody(t, 64, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)
	shared := testMeasureAllocatedBytes(t, func() {
		_, _, err := decodeOfficialJSONObjectSharingBody(body)
		require.NoError(t, err)
	})
	copied := testMeasureAllocatedBytes(t, func() {
		_, err := decodeOfficialJSONObjectUseNumber(body)
		require.NoError(t, err)
	})
	t.Logf("正文 %.1f MiB：引用正文的解码 %.1f KiB（含索引），普通解码 %.1f MiB",
		float64(len(body))/(1<<20), float64(shared)/1024, float64(copied)/(1<<20))
	require.Less(t, shared, uint64(len(body))/16,
		"引用正文的解码分配 %d 字节，不得再按正文体积（%d 字节）复制字符串", shared, len(body))
	require.GreaterOrEqual(t, copied, uint64(len(body)), "对照组的普通解码应至少分配一份正文")
}

// TestOfficialEgressHTTPFinalizerDoesNotDecodeLargeBodyTwice 锁定终态修正整体只剩一次拼接
// 编码的正文分配：解码不再复制字符串，索引也只建一次。
func TestOfficialEgressHTTPFinalizerDoesNotDecodeLargeBodyTwice(t *testing.T) {
	body := officialBodySharingLargeRequestBody(t, 64, 128*1024)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	allocated := testMeasureAllocatedBytes(t, func() {
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		_, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
			c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
			openAIUpstreamRequestPlan{
				IsStream: true, PromptCacheKey: testOfficialOpenAISessionID,
				IsCodexCLI: true, OfficialEgressBodyContract: contract,
			},
		)
		require.NoError(t, err)
	})
	t.Logf("正文 %.1f MiB：终态修正整体分配 %.1f MiB（%.2f 倍正文）",
		float64(len(body))/(1<<20), float64(allocated)/(1<<20), float64(allocated)/float64(len(body)))
	require.Less(t, allocated, uint64(len(body))*13/10,
		"终态修正分配 %d 字节，应只剩一次拼接编码的正文（%d 字节）", allocated, len(body))
}
