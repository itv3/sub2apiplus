package service

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 本文件锁定官方出站"终态定型之前"的正文改写环节同样保持数据保真。
// 这些环节位于 Finalizer 之前，一旦退回 encoding/json 的默认行为，
// Finalizer 保住的只是一份已经被改坏的字节，任何终态断言都发现不了。

const fidelityBigInteger = "12345678901234567890"

// TestOpenAIResponsesLiteNormalizationPreservesNestedRawData 覆盖 Lite 工具归一化。
// 该函数在 HTTP 与 WS 共 4 个入口被调用，且只要请求缺 reasoning.context=all_turns
// 就必定触发——任何第三方客户端都缺这个字段，因此触发率是 100%。
func TestOpenAIResponsesLiteNormalizationPreservesNestedRawData(t *testing.T) {
	body := []byte(`{"model":"gpt-5.6-luna","input":"hi","payload":{"big":` +
		fidelityBigInteger + `,"z":1,"a":2},"tools":[]}`)

	out, changed, err := normalizeOpenAIResponsesLiteToolsPayload(body)
	require.NoError(t, err)
	require.True(t, changed, "缺少 reasoning.context 时必须发生归一化，否则用例没有覆盖到改写路径")

	rebuilt := string(out)
	require.Contains(t, rebuilt, fidelityBigInteger,
		"大整数必须原样保留；经 float64 往返会被改写为 ...000")
	require.NotContains(t, rebuilt, "12345678901234567000",
		"出现该值说明数字经过了 float64")
	require.Contains(t, rebuilt, `"payload":{"big":`+fidelityBigInteger+`,"z":1,"a":2}`,
		"嵌套对象必须保留原始键序，不得被 Go map 编码重排为字典序")
}

// TestOpenAIResponsesLiteNormalizationKeepsModifiedObjectKeyOrder 覆盖"被改写的
// 嵌套对象"这一类：reasoning 因为要补 context=all_turns 而必然被修改，无法直接
// 复用原始字节，因此最容易在重新编码时被 Go map 按字典序重排。真实抓包中该对象
// 入站为 effort/summary/context，一旦退化就会变成 context/effort/summary。
func TestOpenAIResponsesLiteNormalizationKeepsModifiedObjectKeyOrder(t *testing.T) {
	body := []byte(`{"model":"gpt-5.6-luna","input":"hi",` +
		`"reasoning":{"effort":"low","summary":"auto","context":"none"}}`)

	out, changed, err := normalizeOpenAIResponsesLiteToolsPayload(body)
	require.NoError(t, err)
	require.True(t, changed)

	rebuilt := string(out)
	require.Contains(t, rebuilt, `"reasoning":{"effort":"low","summary":"auto","context":"all_turns"}`,
		"仅 context 的值应被改写，其余成员的原始顺序必须保留")
	require.NotContains(t, rebuilt, `"reasoning":{"context":`,
		"出现 context 打头说明对象被按字典序重排了")
}

// TestFlattenOpenAIResponsesNamespacesPreservesNestedRawData 覆盖非 Lite 的
// namespace 摊平路径，与上面属同一类改写环节。
func TestFlattenOpenAIResponsesNamespacesPreservesNestedRawData(t *testing.T) {
	body := []byte(`{"model":"gpt-5.4","payload":{"big":` + fidelityBigInteger +
		`,"z":1,"a":2},"tools":[{"type":"namespace","name":"ns","tools":[{"type":"function","name":"f"}]}]}`)

	out, err := flattenOpenAIResponsesNamespaces(nil, body)
	require.NoError(t, err)

	rebuilt := string(out)
	require.Contains(t, rebuilt, fidelityBigInteger, "大整数必须原样保留")
	require.Contains(t, rebuilt, `"payload":{"big":`+fidelityBigInteger+`,"z":1,"a":2}`,
		"未参与摊平的嵌套用户数据必须保持原始字节与键序")
}

// TestMarshalOfficialOrderedJSONObjectHandlesDuplicateTopLevelKeys 锁定重复顶层键
// 不再触发 makeslice panic。JSON 规范允许重复键，encoding/json 静默接受，
// 中间代理产出重复键并不罕见。
func TestMarshalOfficialOrderedJSONObjectHandlesDuplicateTopLevelKeys(t *testing.T) {
	// top_p 不在官方字段序里，会走"未知键按原始顺序追加"的分支——
	// 正是该分支按 len(payload)-len(keys) 计算容量。
	original := []byte(`{"model":"claude-fable-5","top_p":0.5,"top_p":0.6,"messages":[]}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)

	require.NotPanics(t, func() {
		out, marshalErr := marshalOfficialJSONObjectPreservingOrderAndRaw(payload, original)
		require.NoError(t, marshalErr)
		// 与 encoding/json 解码到 map 的语义一致：保留最后一次出现的值。
		require.Contains(t, string(out), `"top_p":0.6`)
		require.Equal(t, 1, strings.Count(string(out), `"top_p"`),
			"重复键在输出中只能出现一次")
	})
}

// TestDecodeOrderedRawJSONObjectDeduplicatesKeys 直接锁定根因：keys 不得含重复项。
func TestDecodeOrderedRawJSONObjectDeduplicatesKeys(t *testing.T) {
	fields, keys, err := decodeOrderedRawJSONObject(
		[]byte(`{"a":1,"b":2,"a":3,"b":4,"a":5}`),
	)
	require.NoError(t, err)
	require.Equal(t, []string{"a", "b"}, keys, "keys 只登记首次出现的位置且不得重复")
	require.JSONEq(t, `5`, string(fields["a"]), "值保留最后一次出现的结果")
	require.JSONEq(t, `4`, string(fields["b"]))
}

// TestResolveOfficialOpenAIHTTPTurnStartReturnsEffectiveValue 锁定同一个 turn 的
// 起始时间以实际登记值为准：并发首发请求各自算出的毫秒值不同，若调用方返回自己
// 那一份，x-codex-turn-metadata 就会在同一个 turn 内漂移。
func TestResolveOfficialOpenAIHTTPTurnStartReturnsEffectiveValue(t *testing.T) {
	const turnKey = "fidelity-regression-turn"
	officialOpenAIHTTPTurnStarts.Lock()
	delete(officialOpenAIHTTPTurnStarts.entries, turnKey)
	officialOpenAIHTTPTurnStarts.Unlock()
	t.Cleanup(func() {
		officialOpenAIHTTPTurnStarts.Lock()
		delete(officialOpenAIHTTPTurnStarts.entries, turnKey)
		officialOpenAIHTTPTurnStarts.Unlock()
	})

	first := resolveOfficialOpenAIHTTPTurnStart(nil, turnKey)
	require.Positive(t, first)

	// 后到者即便算出不同的时间戳，也必须拿到首次登记的值。
	second := resolveOfficialOpenAIHTTPTurnStart(nil, turnKey)
	require.Equal(t, first, second, "同一个 turn 必须始终返回首次登记的起始时间")
}
