package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"unsafe"

	"github.com/stretchr/testify/require"
)

func TestOfficialJSONSharedShortStringsKeepDecodeAndOwnership(t *testing.T) {
	item := `{"type":"message","role":"user","plain":"repeated-short-value","escaped":"a\u0062\/c","invalid":"bad` + "\xff" + `","n":1.2300e+04,"duplicate":1,"duplicate":2}`
	body := []byte(`{"input":[` + strings.Repeat(item+",", 127) + item + `]}`)
	index, err := buildOfficialJSONRawIndexForDecode(body)
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(index.nodes), officialJSONSharedDecodeMinNodes)
	got := index.decodeValueSharingBody(index.root)
	want, err := decodeOfficialJSONObjectUseNumberSlow(body)
	require.NoError(t, err)
	require.Equal(t, want, got, "重复键、数字文本、转义和非法 UTF-8 行为必须保持原样")

	object, ok := got.(map[string]any)
	require.True(t, ok)
	items, ok := object["input"].([]any)
	require.True(t, ok)
	first, ok := items[0].(map[string]any)
	require.True(t, ok)
	second, ok := items[1].(map[string]any)
	require.True(t, ok)
	firstText, ok := first["plain"].(string)
	require.True(t, ok)
	secondText, ok := second["plain"].(string)
	require.True(t, ok)
	require.Equal(t, unsafe.StringData(firstText), unsafe.StringData(secondText), "重复短值应只复制一次")
	begin := uintptr(unsafe.Pointer(unsafe.SliceData(body)))
	end := begin + uintptr(len(body))
	for _, name := range []string{"plain", "escaped", "invalid", "type", "role"} {
		value, ok := first[name].(string)
		require.True(t, ok)
		pointer := uintptr(unsafe.Pointer(unsafe.StringData(value)))
		require.False(t, pointer >= begin && pointer < end, "短值 %s 不能引用大正文", name)
	}
	// 字符串不可变，因此可以复用；map、slice 和不同解码调用仍必须互相隔离。
	first["plain"] = "changed"
	require.Equal(t, "repeated-short-value", second["plain"])
	againObject, ok := index.decodeValueSharingBody(index.root).(map[string]any)
	require.True(t, ok)
	again, ok := againObject["input"].([]any)
	require.True(t, ok)
	againFirst, ok := again[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "repeated-short-value", againFirst["plain"])
	require.Equal(t, json.Number("1.2300e+04"), first["n"])
}

func TestOfficialJSONSharedShortStringCacheIsBounded(t *testing.T) {
	cache := &officialJSONSharedDecodeStrings{}
	for i := 0; i < 2000; i++ {
		value := fmt.Sprintf("%05d-%s", i, strings.Repeat("x", 500))
		require.Equal(t, value, cache.copyPlainString([]byte(value)))
	}
	require.LessOrEqual(t, len(cache.values), officialJSONSharedDecodeMaxStrings)
	require.LessOrEqual(t, cache.bytes, officialJSONSharedDecodeMaxBytes)
	require.Positive(t, len(cache.values))
	// 达到上限后，已登记的重复值仍可复用；新值继续正确复制，不会借用调用方缓冲。
	raw := []byte("not-cached-after-limit")
	text, ok := cache.copyPlainString(raw).(string)
	require.True(t, ok)
	raw[0] = 'X'
	require.Equal(t, "not-cached-after-limit", text)
}

func TestOfficialJSONSharedShortStringsReduceRepeatedValueAllocation(t *testing.T) {
	const count = 1536
	text := strings.Repeat("repeated short tool output ", 24)
	item := []byte(`{"type":"message","role":"user","text":"` + text + `"}`)
	body := []byte(`{"input":[`)
	for i := 0; i < count; i++ {
		if i != 0 {
			body = append(body, ',')
		}
		body = append(body, item...)
	}
	body = append(body, ']', '}')
	index, err := buildOfficialJSONRawIndexForDecode(body)
	require.NoError(t, err)
	var uncached, cached any
	previous := testMeasureAllocatedBytes(t, func() {
		uncached = index.decodeValueSharingBodyWithStrings(index.root, nil)
	})
	current := testMeasureAllocatedBytes(t, func() {
		cached = index.decodeValueSharingBody(index.root)
	})
	require.Equal(t, uncached, cached)
	t.Logf("正文 %.2f MiB：未复用短值解码 %.2f MiB，请求内有界复用 %.2f MiB",
		float64(len(body))/(1<<20), float64(previous)/(1<<20), float64(current)/(1<<20))
	require.Less(t, current+uint64(len(text)*count)*8/10, previous,
		"重复短值应至少省去八成重复字符串分配，不能只改变统计位置")
	uncachedObject, ok := uncached.(map[string]any)
	require.True(t, ok)
	before, err := marshalOfficialJSONObjectPreservingOrderAndRaw(uncachedObject, body)
	require.NoError(t, err)
	cachedObject, ok := cached.(map[string]any)
	require.True(t, ok)
	after, err := marshalOfficialJSONObjectPreservingOrderAndRaw(cachedObject, body)
	require.NoError(t, err)
	require.True(t, bytes.Equal(before, after), "复用短值不能改变原文拼接结果")
}
