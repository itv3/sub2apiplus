package service

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

// 预编码的 json.RawMessage 数组项：字节形态（例如官方键序）由调用方负责，保序编码器原样追加，
// 不让它与原始数组元素做内容或位置配对。RawMessage 不可哈希，若参与配对，相等判断会逐个候选元素
// 解码，把一次只读的前缀插入放大成对整段数组的解码；位置配对还会占用原元素。

func TestOfficialJSONPreEncodedArrayItemIsAppendedVerbatimAndKeepsOriginalRanges(t *testing.T) {
	// 原始元素内部的空白特意保留：被改写会丢失，引用原区间则逐字节保留。
	originals := []string{`{"a" : 1}`, `{"b" : 2}`, `{"c" : 3}`}
	body := []byte(`{"input":[` + strings.Join(originals, ",") + `],"tail":true}`)
	const preEncoded = `{"z":"pre","y":[1, 2]}`
	for position := 0; position <= len(originals); position++ {
		t.Run("插入位置"+strconv.Itoa(position), func(t *testing.T) {
			expectedItems := append(append(append([]string{}, originals[:position]...), preEncoded), originals[position:]...)
			expected := `{"input":[` + strings.Join(expectedItems, ",") + `],"tail":true}`
			order := []string{"input", "tail"}

			build := func() (map[string]any, *officialJSONRawIndex) {
				payload, index, err := decodeOfficialJSONObjectSharingBody(body)
				require.NoError(t, err)
				items, ok := payload["input"].([]any)
				require.True(t, ok)
				rebuilt := append(append(append([]any{}, items[:position]...), json.RawMessage(preEncoded)), items[position:]...)
				payload["input"] = rebuilt
				return payload, index
			}

			payload, index := build()
			encoded, err := marshalOfficialOrderedJSONObjectPreservingRawWithIndex(payload, order, body, index)
			require.NoError(t, err)
			require.Equal(t, expected, string(encoded), "整段编码")

			payload, index = build()
			members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, order, body, index)
			require.NoError(t, err)
			require.Equal(t, expected, string(officialegress.AppendJSONObjectMembers(nil, members)), "按成员编码")
		})
	}
}

func TestOfficialJSONPreEncodedArrayItemDoesNotDecodeOriginalItems(t *testing.T) {
	body := baseInstructionsLargeBody(t, 64, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)
	order := []string{"instructions", "input", "model"}
	run := func(members bool) uint64 {
		return testMeasureAllocatedBytes(t, func() {
			payload, index, err := decodeOfficialJSONObjectSharingBody(body)
			require.NoError(t, err)
			items, ok := payload["input"].([]any)
			require.True(t, ok)
			payload["input"] = append([]any{json.RawMessage(`{"pre":true}`)}, items...)
			if members {
				encoded, encodeErr := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, order, body, index)
				require.NoError(t, encodeErr)
				require.NotEmpty(t, encoded)
				return
			}
			encoded, encodeErr := marshalOfficialOrderedJSONObjectPreservingRawWithIndex(payload, order, body, index)
			require.NoError(t, encodeErr)
			require.Greater(t, len(encoded), len(body))
		})
	}
	// 成员路径只产出片段引用，整段路径要拼出一份完整正文（约一倍正文）；两者都不能额外解码历史。
	memberAllocated := run(true)
	require.Less(t, memberAllocated, uint64(len(body))/8,
		"按成员编码分配 %d 字节，不得再按正文体积（%d 字节）解码历史", memberAllocated, len(body))
	wholeAllocated := run(false)
	require.Less(t, wholeAllocated, uint64(len(body))*13/10,
		"整段编码分配 %d 字节，应只剩一次拼接编码的正文（%d 字节）", wholeAllocated, len(body))
}
