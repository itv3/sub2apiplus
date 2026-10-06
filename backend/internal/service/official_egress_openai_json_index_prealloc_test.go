package service

import (
	"math/rand"
	"strconv"
	"strings"
	"testing"
	"unsafe"

	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M2 清单外的一处副本：正文索引的节点表按预计数一次预留，不再从 64 起按约 1.25 倍反复
// 扩容。预计数在合法正文上必须与扫描器登记的节点数完全相同；非法正文只影响预留容量，扫描结果与错误不变。

func indexPreallocCheck(t *testing.T, name string, body []byte) {
	t.Helper()
	count := officialJSONRawCountStructure(body)
	for label, build := range map[string]func([]byte) (*officialJSONRawIndex, error){
		"decode": buildOfficialJSONRawIndexForDecode,
		"full":   buildOfficialJSONRawIndex,
	} {
		index, err := build(body)
		if err != nil {
			continue
		}
		require.Equal(t, len(index.nodes), count.values, "%s（%s）：预计数必须等于登记的节点数", name, label)
		require.Equal(t, cap(index.nodes), len(index.nodes), "%s（%s）：节点表不得扩容", name, label)
		require.Equal(t, len(index.members), count.members, "%s（%s）：对象成员计数必须精确", name, label)
		require.Equal(t, len(index.items), count.items, "%s（%s）：数组元素计数必须精确", name, label)
		require.Equal(t, len(index.objects), count.objects, "%s（%s）：对象缓存计数必须精确", name, label)
		require.Equal(t, len(index.members), cap(index.members), "%s（%s）：成员表不得扩容", name, label)
		require.Equal(t, len(index.items), cap(index.items), "%s（%s）：元素表不得扩容", name, label)
		require.Equal(t, len(index.objects), cap(index.objects), "%s（%s）：对象缓存表不得扩容", name, label)
	}
}

func TestOfficialJSONRawCountValuesMatchesScanner(t *testing.T) {
	fixtures := map[string][]byte{
		"official":       newOfficialOpenAIHTTPTestBody(t, true, true, true),
		"memory_profile": buildOfficialEgressMemoryProfileBody(t, 256<<10),
		"escapes":        []byte(`{"a\"b":"x\\\"y\\\\","c":["\\\\","\"",[],{},[[]],[{}],""],"d":{"e":[1,-0.5e3,true,false,null]}}`),
		"whitespace":     []byte(" \n{ \"a\" : [ 1 , [ ] , { } , \"s\" ] , \"b\" : { } } \t"),
		"scalars":        []byte(`"text"`),
		"empty_array":    []byte(`[]`),
		"nested_arrays":  []byte(`[[1,[2,[3,[]]]],[],[[]]]`),
		"deep":           []byte(`{"a":` + strings.Repeat("[", 9998) + strings.Repeat("]", 9998) + `}`),
	}
	for i, fixture := range officialJSONDecodeFixtures {
		fixtures["decode_valid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for name, body := range fixtures {
		indexPreallocCheck(t, name, body)
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		// 解码意义上非法的夹具：扫描器能接受的（如顶层不是对象）同样要求计数精确，其余只要求不崩溃。
		indexPreallocCheck(t, "invalid#"+strconv.Itoa(i), []byte(fixture))
	}
	rng := rand.New(rand.NewSource(20261007))
	for round := 0; round < 3000; round++ {
		body := []byte(decodeRandomJSON(rng, 0))
		indexPreallocCheck(t, "random#"+strconv.Itoa(round), body)
		indexPreallocCheck(t, "request#"+strconv.Itoa(round), decodeSharingRandomBody(rng))
		mutated := decodeMutate(rng, body)
		_ = officialJSONRawCountValues(mutated)
		indexPreallocCheck(t, "mutated#"+strconv.Itoa(round), mutated)
	}
	for _, broken := range []string{`"unterminated`, `{"a":"\`, `[[[`, `]]]}}`, `{"a":[1,2`, `\"`, `,,,`} {
		require.NotPanics(t, func() { _ = officialJSONRawCountValues([]byte(broken)) }, broken)
	}
}

func TestOfficialJSONRawIndexPreallocatesNodeTable(t *testing.T) {
	body := buildOfficialEgressMemoryProfileBody(t, 8<<20)
	var index *officialJSONRawIndex
	allocated := testMeasureAllocatedBytes(t, func() {
		var err error
		index, err = buildOfficialJSONRawIndexForDecode(body)
		require.NoError(t, err)
	})
	nodeTable := uint64(len(index.nodes)) * uint64(unsafe.Sizeof(officialJSONRawNode{}))
	t.Logf("正文 %.1f MiB：%d 个节点，节点表 %.1f MiB，建索引共分配 %.1f MiB",
		float64(len(body))/(1<<20), len(index.nodes), float64(nodeTable)/(1<<20), float64(allocated)/(1<<20))
	require.Equal(t, cap(index.nodes), len(index.nodes))
	// 节点表一次到位；其余为各对象的成员表、数组元素表等小块，合计不超过节点表本身。
	require.Less(t, allocated, nodeTable*2, "建索引分配 %d 字节，不得再是节点表（%d 字节）的数倍", allocated, nodeTable)
}
