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
	var selected profilecontract.ExecutableEndpointProfile
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == endpointID {
			selected = endpoint
			break
		}
	}
	if selected.ID == "" {
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

var officialOpenAITurnMetadataFieldOrder = []string{
	"installation_id", "session_id", "thread_id", "turn_id", "window_id",
	"request_kind", "thread_source", "sandbox", "turn_started_at_unix_ms",
	"compaction",
}

type officialJSONRawPool map[string]json.RawMessage

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
	return marshalOfficialOrderedJSONObjectPreservingRaw(payload, order, original)
}

func marshalOfficialOpenAIWSJSON(mode string, payload map[string]any) ([]byte, error) {
	return marshalOfficialOpenAIWSJSONPreservingRaw(mode, payload, nil)

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

// marshalOfficialOrderedJSONObjectPreservingRaw 只固定官方结构体可观察的
// 顶层字段顺序。未变化的嵌套值直接复用原始 JSON 字节；需要局部修改的对象和
// 数组也会保留其余成员的原始字节与相对顺序，避免画像修正改写用户数据。
func marshalOfficialOrderedJSONObjectPreservingRaw(
	payload map[string]any,
	order []string,
	original []byte,
) ([]byte, error) {
	originalFields, originalKeys, _ := decodeOrderedRawJSONObject(original)
	originalPool := collectOfficialJSONCompositeRawValues(original)
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
	keys = append(keys, unknown...)

	out := []byte{'{'}
	for index, key := range keys {
		if index > 0 {
			out = append(out, ',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		encodedValue, err := marshalOfficialJSONValuePreservingRaw(
			payload[key],
			originalFields[key],
			originalPool,
		)
		if err != nil {
			return nil, err
		}
		out = append(out, encodedKey...)
		out = append(out, ':')
		out = append(out, encodedValue...)
	}
	out = append(out, '}')
	return out, nil
}

// decodeOfficialJSONObjectUseNumber 在任何可能重新编码正文的官方出站路径中
// 保留 JSON 数字的十进制文本，防止大整数经过 float64 后发生不可逆改写。
func decodeOfficialJSONObjectUseNumber(body []byte) (map[string]any, error) {
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

func decodeOfficialJSONValueUseNumber(body []byte) (any, error) {
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

func marshalOfficialJSONValuePreservingRaw(
	value any,
	original json.RawMessage,
	originalPool officialJSONRawPool,
) ([]byte, error) {
	if len(original) > 0 {
		decoded, err := decodeOfficialJSONValueUseNumber(original)
		if err == nil && reflectOfficialJSONEqual(value, decoded) {
			return append([]byte(nil), original...), nil
		}
	}
	if pooled := matchOfficialJSONCompositeRawValue(value, originalPool); len(pooled) > 0 {
		return append([]byte(nil), pooled...), nil
	}

	switch typed := value.(type) {
	case map[string]any:
		return marshalOfficialNestedJSONObjectPreservingRaw(typed, original, originalPool)
	case []any:
		return marshalOfficialJSONArrayPreservingRaw(typed, original, originalPool)
	case json.RawMessage:
		if json.Valid(typed) {
			return append([]byte(nil), typed...), nil
		}
	}
	return marshalOpenAIUpstreamJSON(value)
}

func marshalOfficialNestedJSONObjectPreservingRaw(
	payload map[string]any,
	original json.RawMessage,
	originalPool officialJSONRawPool,
) ([]byte, error) {
	originalFields, originalKeys, _ := decodeOrderedRawJSONObject(original)
	keys := make([]string, 0, len(payload))
	seen := make(map[string]struct{}, len(payload))
	for _, key := range originalKeys {
		if _, exists := payload[key]; !exists {
			continue
		}
		keys = append(keys, key)
		seen[key] = struct{}{}
	}
	additional := make([]string, 0, len(payload)-len(keys))
	for key := range payload {
		if _, exists := seen[key]; !exists {
			additional = append(additional, key)
		}
	}
	sort.Strings(additional)
	keys = append(keys, additional...)

	out := []byte{'{'}
	for index, key := range keys {
		if index > 0 {
			out = append(out, ',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		encodedValue, err := marshalOfficialJSONValuePreservingRaw(
			payload[key],
			originalFields[key],
			originalPool,
		)
		if err != nil {
			return nil, err
		}
		out = append(out, encodedKey...)
		out = append(out, ':')
		out = append(out, encodedValue...)
	}
	out = append(out, '}')
	return out, nil
}

func marshalOfficialJSONArrayPreservingRaw(
	items []any,
	original json.RawMessage,
	originalPool officialJSONRawPool,
) ([]byte, error) {
	var originalItems []json.RawMessage
	_ = json.Unmarshal(original, &originalItems)
	used := make([]bool, len(originalItems))
	out := []byte{'['}
	for index, item := range items {
		if index > 0 {
			out = append(out, ',')
		}
		originalIndex := matchOfficialJSONOriginalArrayItem(item, originalItems, used)
		if originalIndex < 0 && index < len(originalItems) && !used[index] {
			originalIndex = index
		}
		var originalItem json.RawMessage
		if originalIndex >= 0 {
			used[originalIndex] = true
			originalItem = originalItems[originalIndex]
		}
		encoded, err := marshalOfficialJSONValuePreservingRaw(item, originalItem, originalPool)
		if err != nil {
			return nil, err
		}
		out = append(out, encoded...)
	}
	out = append(out, ']')
	return out, nil
}

// collectOfficialJSONCompositeRawValues 收集原始正文中的对象和数组字节。
// Profile 可能把工具或消息搬到另一个顶层字段；局部父节点因此无法直接命中，
// 全文池仍可复用未变化的用户对象，避免跨字段搬运时重排其内部键。
func collectOfficialJSONCompositeRawValues(body []byte) officialJSONRawPool {
	values := make(officialJSONRawPool, 32)
	var collect func(json.RawMessage)
	collect = func(raw json.RawMessage) {
		trimmed := bytes.TrimSpace(raw)
		if len(trimmed) == 0 || (trimmed[0] != '{' && trimmed[0] != '[') {
			return
		}
		decoded, err := decodeOfficialJSONValueUseNumber(trimmed)
		if err != nil {
			return
		}
		canonical, err := marshalOpenAIUpstreamJSON(decoded)
		if err != nil {
			return
		}
		key := string(canonical)
		if _, exists := values[key]; !exists {
			values[key] = append(json.RawMessage(nil), trimmed...)
		}
		if trimmed[0] == '{' {
			fields, keys, err := decodeOrderedRawJSONObject(trimmed)
			if err != nil {
				return
			}
			for _, key := range keys {
				collect(fields[key])
			}
			return
		}
		var items []json.RawMessage
		if err := json.Unmarshal(trimmed, &items); err != nil {
			return
		}
		for _, item := range items {
			collect(item)
		}
	}
	collect(body)
	return values
}

func matchOfficialJSONCompositeRawValue(
	value any,
	originalPool officialJSONRawPool,
) json.RawMessage {
	switch value.(type) {
	case map[string]any, []any:
	default:
		return nil
	}
	canonical, err := marshalOpenAIUpstreamJSON(value)
	if err != nil {
		return nil
	}
	return originalPool[string(canonical)]
}

func matchOfficialJSONOriginalArrayItem(
	item any,
	originalItems []json.RawMessage,
	used []bool,
) int {
	for index, raw := range originalItems {
		if used[index] {
			continue
		}
		decoded, err := decodeOfficialJSONValueUseNumber(raw)
		if err == nil && reflectOfficialJSONEqual(item, decoded) {
			return index
		}
	}
	return -1
}

func reflectOfficialJSONEqual(left, right any) bool {
	leftJSON, leftErr := marshalOpenAIUpstreamJSON(left)
	rightJSON, rightErr := marshalOpenAIUpstreamJSON(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftJSON, rightJSON)
}
