package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"sort"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// officialCodexBodyFieldOrderForMode 每次从正式 ReleaseCatalog 投影字段槽位。
// 字段序不再保存在包级 active 切片中；最终 compiler 仍会按调用级 Bundle 再校验。
func officialCodexBodyFieldOrderForMode(mode, endpointID string) ([]string, error) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseMode(mode),
	)
	if err != nil {
		return nil, err
	}
	return officialCodexBodyFieldOrderFromProfile(release.ExecutableProfile(), endpointID)
}

func officialCodexBodyFieldOrderFromProfile(
	profile profilecontract.ExecutableProfile,
	endpointID string,
) ([]string, error) {
	selected, found := profile.Endpoint(endpointID)
	if !found {
		return nil, errors.New("ExecutableProfile 缺少 WS frame endpoint")
	}
	fields := make([]string, 0, len(selected.Body.Fields))
	for _, field := range selected.Body.Fields {
		fields = append(fields, field.Name)
	}
	return fields, nil
}

func marshalOfficialOpenAIWSJSONFromProfile(
	profile profilecontract.ExecutableProfile,
	payload map[string]any,
) ([]byte, error) {
	order, err := officialCodexBodyFieldOrderFromProfile(
		profile,
		officialCodexEndpointResponsesWS,
	)
	if err != nil {
		return nil, err
	}
	return marshalOfficialOrderedJSONObjectPreservingRaw(payload, order, nil)
}

// officialOpenAITurnMetadataFieldOrder 对齐官方 turn metadata 结构体的声明序：
// turn_trigger 位于 thread_source 与 sandbox 之间，analytics_enabled 位于
// turn_started_at_unix_ms 与 compaction 之间，model、reasoning_effort 属于结构体末尾
// 按键名排序展开的附加表。新键只在画像 TurnMetadata 节声明时写入，旧画像的键集中
// 不含它们，插入这些位置不改变旧画像的输出字节。
var officialOpenAITurnMetadataFieldOrder = []string{
	"installation_id", "session_id", "thread_id", "turn_id", "window_id",
	"request_kind", "thread_source", "turn_trigger", "sandbox", "turn_started_at_unix_ms",
	"analytics_enabled", "compaction", "model", "reasoning_effort",
}

func marshalOfficialOpenAITurnMetadata(payload map[string]any) ([]byte, error) {
	return marshalOfficialOrderedJSONObject(payload, officialOpenAITurnMetadataFieldOrder)
}

func marshalOfficialOpenAIHTTPJSON(mode string, payload map[string]any, compact bool) ([]byte, error) {
	return marshalOfficialOpenAIHTTPJSONPreservingRaw(mode, payload, compact, nil)
}

func marshalOfficialOpenAIHTTPJSONPreservingRaw(
	mode string,
	payload map[string]any,
	compact bool,
	original []byte,
) ([]byte, error) {
	return marshalOfficialOpenAIHTTPJSONPreservingRawWithIndex(mode, payload, compact, original, nil)
}

// marshalOfficialOpenAIHTTPJSONPreservingRawWithIndex 与 marshalOfficialOpenAIHTTPJSONPreservingRaw
// 相同；index 若非 nil 必须是 original 的完整索引（buildOfficialJSONRawIndex），调用方已为解码
// 建好时直接复用，不再为同一正文扫描第二遍。
func marshalOfficialOpenAIHTTPJSONPreservingRawWithIndex(
	mode string,
	payload map[string]any,
	compact bool,
	original []byte,
	index *officialJSONRawIndex,
) ([]byte, error) {
	endpointID := officialCodexEndpointResponsesHTTP
	if compact {
		endpointID = officialCodexEndpointResponsesCompact
	}
	order, err := officialCodexBodyFieldOrderForMode(
		mode, endpointID,
	)
	if err != nil {
		return nil, err
	}
	return marshalOfficialOrderedJSONObjectPreservingRawWithIndex(payload, order, original, index)
}

func marshalOfficialOpenAIWSJSONPreservingRaw(
	mode string,
	payload map[string]any,
	original []byte,
) ([]byte, error) {
	order, err := officialCodexBodyFieldOrderForMode(
		mode, officialCodexEndpointResponsesWS,
	)
	if err != nil {
		return nil, err
	}
	return marshalOfficialOrderedJSONObjectPreservingRaw(payload, order, original)
}

// marshalOfficialOrderedJSONObject 只固定官方结构体可观察的顶层字段顺序；
// 没有原始正文的调用用于构造全新的官方结构体。
func marshalOfficialOrderedJSONObject(payload map[string]any, order []string) ([]byte, error) {
	return marshalOfficialOrderedJSONObjectPreservingRaw(payload, order, nil)
}

// marshalOfficialJSONObjectPreservingOrderAndRaw 供官方出站终态定型之前的中间
// 改写使用：不重排顶层字段顺序，未被改动的值直接复用原始 JSON 字节。终态定型
// 之前的任何一次 map 往返都会丢失大整数精度并按字典序重排嵌套对象，因此这些
// 环节同样不能退回 encoding/json 的默认行为。
func marshalOfficialJSONObjectPreservingOrderAndRaw(
	payload map[string]any,
	original []byte,
) ([]byte, error) {
	return marshalOfficialOrderedJSONObjectPreservingRaw(payload, nil, original)
}

// marshalOfficialJSONObjectPreservingOrderAndRawWithIndex 同上，复用调用方已建好的 original 索引。
func marshalOfficialJSONObjectPreservingOrderAndRawWithIndex(
	payload map[string]any,
	original []byte,
	index *officialJSONRawIndex,
) ([]byte, error) {
	return marshalOfficialOrderedJSONObjectPreservingRawWithIndex(payload, nil, original, index)
}

// marshalOfficialOrderedJSONObjectPreservingRaw 只固定官方结构体可观察的
// 顶层字段顺序。未变化的嵌套值直接复用原始 JSON 字节；需要局部修改的对象和
// 数组也会保留其余成员的原始字节与相对顺序，避免画像修正改写用户数据。
// 实现见 official_egress_openai_json_index.go：对原始正文做一次只读扫描建立字节
// 区间索引，未改动的值零分配比对后直接拼接，不再解码整段正文，也不再建立
// 全文查找池；输出字节与旧实现逐字一致，由差分测试锁定。
func marshalOfficialOrderedJSONObjectPreservingRaw(
	payload map[string]any,
	order []string,
	original []byte,
) ([]byte, error) {
	return marshalOfficialOrderedJSONObjectPreservingRawWithIndex(payload, order, original, nil)
}

// marshalOfficialOrderedJSONObjectPreservingRawWithIndex 是带预建索引的入口：index 为 nil 时按
// original 现场构建（非法或空正文得到 nil，即没有可复用的原始字节），否则必须是 original 的
// 完整索引。两种入口输出逐字节相同。
func marshalOfficialOrderedJSONObjectPreservingRawWithIndex(
	payload map[string]any,
	order []string,
	original []byte,
	index *officialJSONRawIndex,
) ([]byte, error) {
	if index == nil {
		index = officialJSONRawIndexForOriginal(original)
	}
	root := int32(-1)
	if index != nil {
		root = index.root
	}
	keys := officialJSONOrderedTopLevelKeys(payload, order, index)

	out := make([]byte, 0, len(original)+256)
	out = append(out, '{')
	for index2, key := range keys {
		if index2 > 0 {
			out = append(out, ',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		out = append(out, encodedKey...)
		out = append(out, ':')
		child := int32(-1)
		if index != nil {
			child = index.memberNode(root, key)
		}
		out, err = officialJSONAppendValue(index, out, payload[key], child)
		if err != nil {
			return nil, err
		}
	}
	out = append(out, '}')
	return out, nil
}

// marshalOfficialOpenAIHTTPJSONMembersPreservingRawWithIndex 与 marshalOfficialOpenAIHTTPJSONPreservingRawWithIndex
// 采用同一画像字段序，但产出顶层成员而不拼成整段字节，见
// marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex。
func marshalOfficialOpenAIHTTPJSONMembersPreservingRawWithIndex(
	mode string,
	payload map[string]any,
	compact bool,
	original []byte,
	index *officialJSONRawIndex,
) ([]officialegress.JSONObjectMember, error) {
	endpointID := officialCodexEndpointResponsesHTTP
	if compact {
		endpointID = officialCodexEndpointResponsesCompact
	}
	order, err := officialCodexBodyFieldOrderForMode(
		mode, endpointID,
	)
	if err != nil {
		return nil, err
	}
	return marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, order, original, index)
}

// marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex 与
// marshalOfficialOrderedJSONObjectPreservingRawWithIndex 采用同一键序与取值规则，但逐个产出顶层
// 成员（问题四 M1 第三项）：值与原始区间相等时直接引用 original 的该区间（不复制），改动过的值
// 才新编码。成员依次写出（officialegress.AppendJSONObjectMembers）与整段编码逐字节相同，由差分
// 测试锁定；键与值的出错顺序也与整段编码一致。
func marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(
	payload map[string]any,
	order []string,
	original []byte,
	index *officialJSONRawIndex,
) ([]officialegress.JSONObjectMember, error) {
	if index == nil {
		index = officialJSONRawIndexForOriginal(original)
	}
	root := int32(-1)
	if index != nil {
		root = index.root
	}
	keys := officialJSONOrderedTopLevelKeys(payload, order, index)
	members := make([]officialegress.JSONObjectMember, 0, len(keys))
	for _, key := range keys {
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		child := int32(-1)
		if index != nil {
			child = index.memberNode(root, key)
		}
		value, segments, err := officialJSONMemberValue(index, payload[key], child)
		if err != nil {
			return nil, err
		}
		members = append(members, officialegress.JSONObjectMember{
			Name: key, QuotedName: encodedKey, Value: value, ValueSegments: segments,
		})
	}
	return members, nil
}

// officialJSONMemberValue 对修改后的顶层数组保留元素片段。只插入一条 developer 指令时，
// input 的其余历史依旧直接引用原文，不再为数组追加、扩容一份连续大切片。
// 各片段要么是完整 JSON 元素，要么是数组标点，与旧拼接编码器的输出逐字节相同。
func officialJSONMemberValue(index *officialJSONRawIndex, value any, node int32) ([]byte, [][]byte, error) {
	if index != nil {
		if node >= 0 && index.equals(node, value) {
			return index.raw(node), nil, nil
		}
		if pooled := index.lookupComposite(value); pooled >= 0 {
			return index.raw(pooled), nil, nil
		}
	}
	items, isArray := value.([]any)
	if !isArray {
		encoded, err := officialJSONAppendValue(index, nil, value, node)
		return encoded, nil, err
	}
	var originalItems []int32
	if index != nil && node >= 0 && index.nodes[node].kind == officialJSONRawKindArray {
		originalItems = index.arrayItems(node)
	}
	used := make([]bool, len(originalItems))
	segments := make([][]byte, 0, len(items)*2+2)
	segments = append(segments, []byte{'['})
	for i, item := range items {
		if i > 0 {
			segments = append(segments, []byte{','})
		}
		// 与 officialJSONAppendArray 完全相同：先按内容匹配，再尝试未占用的同位置节点。
		match := -1
		if index != nil {
			match = index.matchArrayItem(item, originalItems, used)
		}
		if match < 0 && i < len(originalItems) && !used[i] {
			match = i
		}
		child := int32(-1)
		if match >= 0 {
			used[match] = true
			child = originalItems[match]
		}
		encoded, err := officialJSONValueBytes(index, item, child)
		if err != nil {
			return nil, nil, err
		}
		segments = append(segments, encoded)
	}
	segments = append(segments, []byte{']'})
	return nil, segments, nil
}

// officialJSONValueBytes 返回 officialJSONAppendValue 对同一参数会追加的字节：同位置或按内容命中
// 原始区间时直接返回该区间（引用原文，不复制），否则新编码一份。
func officialJSONValueBytes(index *officialJSONRawIndex, value any, node int32) ([]byte, error) {
	if index != nil {
		if node >= 0 && index.equals(node, value) {
			return index.raw(node), nil
		}
		if pooled := index.lookupComposite(value); pooled >= 0 {
			return index.raw(pooled), nil
		}
	}
	return officialJSONAppendValue(index, nil, value, node)
}

// officialJSONOrderedTopLevelKeys 给出拼接编码的顶层键序：先按 order 中出现且 payload 存在的键，
// 再按原文首次出现顺序补上 order 之外仍存在的键，最后按字典序追加其余新键。
func officialJSONOrderedTopLevelKeys(
	payload map[string]any,
	order []string,
	index *officialJSONRawIndex,
) []string {
	var originalKeys []string
	if index != nil {
		originalKeys = index.uniqueKeys(index.root)
	}
	known := make(map[string]struct{}, len(order))
	keys := make([]string, 0, len(payload))
	for _, key := range order {
		known[key] = struct{}{}
		if _, exists := payload[key]; exists {
			keys = append(keys, key)
		}
	}
	unknownSeen := make(map[string]struct{}, len(payload)-len(keys))
	for _, key := range originalKeys {
		if _, exists := known[key]; exists {
			continue
		}
		if _, exists := payload[key]; !exists {
			continue
		}
		keys = append(keys, key)
		unknownSeen[key] = struct{}{}
	}
	unknown := make([]string, 0, len(payload)-len(keys))
	for key := range payload {
		if _, exists := known[key]; !exists {
			if _, exists := unknownSeen[key]; exists {
				continue
			}
			unknown = append(unknown, key)
		}
	}
	sort.Strings(unknown)
	return append(keys, unknown...)
}

// decodeOfficialJSONObjectUseNumberSlow 是 encoding/json 路径的对象解码：保留 JSON 数字
// 的十进制文本，防止大整数经过 float64 后发生不可逆改写。热路径已改为索引直建对象树
// （见 official_egress_openai_json_decode.go），本函数只在扫描失败或顶层不是对象时被
// 调用，用于给出与过去完全一致的错误值。
func decodeOfficialJSONObjectUseNumberSlow(body []byte) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		return nil, err
	}
	if payload == nil {
		return nil, errors.New("JSON 顶层必须是对象")
	}
	if err := ensureOfficialJSONDecoderEOF(decoder); err != nil {
		return nil, err
	}
	return payload, nil
}

func ensureOfficialJSONDecoderEOF(decoder *json.Decoder) error {
	var trailing any
	err := decoder.Decode(&trailing)
	if errors.Is(err, io.EOF) {
		return nil
	}
	if err == nil {
		return errors.New("JSON 正文包含多个顶层值")
	}
	return err
}

// decodeOfficialJSONValueUseNumberSlow 是 encoding/json 路径的任意值解码，仅作错误回退。
func decodeOfficialJSONValueUseNumberSlow(body []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := ensureOfficialJSONDecoderEOF(decoder); err != nil {
		return nil, err
	}
	return value, nil
}

func decodeOrderedRawJSONObject(body []byte) (
	map[string]json.RawMessage,
	[]string,
	error,
) {
	fields := make(map[string]json.RawMessage)
	if len(bytes.TrimSpace(body)) == 0 {
		return fields, nil, nil
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	start, err := decoder.Token()
	if err != nil {
		return nil, nil, err
	}
	if delimiter, ok := start.(json.Delim); !ok || delimiter != '{' {
		return nil, nil, errors.New("JSON 顶层必须是对象")
	}
	keys := make([]string, 0)
	for decoder.More() {
		token, tokenErr := decoder.Token()
		if tokenErr != nil {
			return nil, nil, tokenErr
		}
		key, ok := token.(string)
		if !ok {
			return nil, nil, errors.New("JSON 对象键必须是字符串")
		}
		var raw json.RawMessage
		if decodeErr := decoder.Decode(&raw); decodeErr != nil {
			return nil, nil, decodeErr
		}
		// JSON 允许同名顶层键重复出现。值按 json.Unmarshal 到 map 的语义保留最后
		// 一次，但 keys 只登记首次出现的位置：下游会用 len(payload)-len(keys) 计算
		// 切片容量，keys 一旦含重复项就会得到负数并触发 makeslice panic。
		if _, exists := fields[key]; !exists {
			keys = append(keys, key)
		}
		fields[key] = append(json.RawMessage(nil), raw...)
	}
	if _, err = decoder.Token(); err != nil {
		return nil, nil, err
	}
	if err = ensureOfficialJSONDecoderEOF(decoder); err != nil {
		return nil, nil, err
	}
	return fields, keys, nil
}

func reflectOfficialJSONEqual(left, right any) bool {
	leftJSON, leftErr := marshalOpenAIUpstreamJSON(left)
	rightJSON, rightErr := marshalOpenAIUpstreamJSON(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftJSON, rightJSON)
}
