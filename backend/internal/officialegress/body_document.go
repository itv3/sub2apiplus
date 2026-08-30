package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// orderedJSONDocument 只属于单个 Executor attempt。source、fields 与 fieldIndex
// 在顶层解析完成后永久只读；overlay 是 attempt-local 的删除/注入层，clone 时必须
// 独立复制，编译失败不得改变原始 document。
type orderedJSONDocument struct {
	source            []byte
	fields            []orderedJSONField
	fieldIndex        map[string]int
	duplicatesChecked bool
	overlay           map[string]orderedJSONOverlay
	overlayOrder      []string
}

type orderedJSONField struct {
	name  string
	value json.RawMessage
}

type orderedJSONOverlay struct {
	value   json.RawMessage
	omitted bool
}

const unsupportedPromptCacheBreakpointField = "prompt_cache_breakpoint"

// ignoredJSONValue 让单个 Decoder 完成值语法扫描，但不复制可能很大的嵌套值。
// 字段 value 最终直接引用不可变 source 中由 InputOffset 确定的区间。
type ignoredJSONValue struct{}

func (*ignoredJSONValue) UnmarshalJSON([]byte) error { return nil }

func newOrderedJSONDocument(source []byte) (*orderedJSONDocument, error) {
	decoder := json.NewDecoder(bytes.NewReader(source))
	decoder.UseNumber()
	opening, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	if delimiter, ok := opening.(json.Delim); !ok || delimiter != '{' {
		return nil, errors.New("JSON Body 必须是 object")
	}
	document := &orderedJSONDocument{
		source: source, fieldIndex: make(map[string]int),
	}
	for decoder.More() {
		nameToken, tokenErr := decoder.Token()
		if tokenErr != nil {
			return nil, tokenErr
		}
		name, ok := nameToken.(string)
		if !ok {
			return nil, errors.New("JSON Body 字段名非法")
		}
		if _, duplicate := document.fieldIndex[name]; duplicate {
			return nil, fmt.Errorf("JSON Body 字段重复: %s", name)
		}
		valueStart, startErr := orderedJSONValueStart(source, int(decoder.InputOffset()))
		if startErr != nil {
			return nil, startErr
		}
		var ignored ignoredJSONValue
		if err := decoder.Decode(&ignored); err != nil {
			return nil, err
		}
		valueEnd := int(decoder.InputOffset())
		if valueEnd < valueStart || valueEnd > len(source) {
			return nil, errors.New("JSON Body value 边界非法")
		}
		document.fieldIndex[name] = len(document.fields)
		document.fields = append(document.fields, orderedJSONField{
			name: name, value: json.RawMessage(source[valueStart:valueEnd]),
		})
	}
	closing, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	if delimiter, ok := closing.(json.Delim); !ok || delimiter != '}' {
		return nil, errors.New("JSON Body 未正确闭合")
	}
	var trailing ignoredJSONValue
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, errors.New("JSON Body 后存在额外值")
		}
		return nil, err
	}
	document.duplicatesChecked = true
	return document, nil
}

func orderedJSONValueStart(source []byte, offset int) (int, error) {
	for offset < len(source) && isJSONSpace(source[offset]) {
		offset++
	}
	if offset >= len(source) || source[offset] != ':' {
		return 0, errors.New("JSON Body 字段缺少冒号")
	}
	offset++
	for offset < len(source) && isJSONSpace(source[offset]) {
		offset++
	}
	if offset >= len(source) {
		return 0, errors.New("JSON Body 字段缺少值")
	}
	return offset, nil
}

func isJSONSpace(value byte) bool {
	return value == ' ' || value == '\t' || value == '\r' || value == '\n'
}

func (d *orderedJSONDocument) clone() *orderedJSONDocument {
	if d == nil {
		return nil
	}
	out := *d
	if d.overlay != nil {
		out.overlay = make(map[string]orderedJSONOverlay, len(d.overlay))
		for name, field := range d.overlay {
			field.value = append(json.RawMessage(nil), field.value...)
			out.overlay[name] = field
		}
	}
	out.overlayOrder = append([]string(nil), d.overlayOrder...)
	return &out
}

func (d *orderedJSONDocument) value(name string) (json.RawMessage, bool) {
	if d == nil || !d.duplicatesChecked {
		return nil, false
	}
	if field, dirty := d.overlay[name]; dirty {
		if field.omitted {
			return nil, false
		}
		return field.value, true
	}
	index, ok := d.fieldIndex[name]
	if !ok {
		return nil, false
	}
	return d.fields[index].value, true
}

func (d *orderedJSONDocument) set(name string, value json.RawMessage) {
	if d.overlay == nil {
		d.overlay = make(map[string]orderedJSONOverlay)
	}
	if _, dirty := d.overlay[name]; !dirty {
		d.overlayOrder = append(d.overlayOrder, name)
	}
	d.overlay[name] = orderedJSONOverlay{value: append(json.RawMessage(nil), value...)}
}

func (d *orderedJSONDocument) omit(name string) {
	if d.overlay == nil {
		d.overlay = make(map[string]orderedJSONOverlay)
	}
	if _, dirty := d.overlay[name]; !dirty {
		d.overlayOrder = append(d.overlayOrder, name)
	}
	d.overlay[name] = orderedJSONOverlay{omitted: true}
}

func (d *orderedJSONDocument) namesInSourceOrder() []string {
	if d == nil {
		return nil
	}
	names := make([]string, 0, len(d.fields)+len(d.overlayOrder))
	for _, field := range d.fields {
		if _, present := d.value(field.name); present {
			names = append(names, field.name)
		}
	}
	for _, name := range d.overlayOrder {
		if _, existed := d.fieldIndex[name]; existed {
			continue
		}
		if _, present := d.value(name); present {
			names = append(names, name)
		}
	}
	return names
}

func (d *orderedJSONDocument) encodeSourceOrder() []byte {
	return d.encodeNames(d.namesInSourceOrder())
}

func (d *orderedJSONDocument) encodeNames(names []string) []byte {
	capacity := 2
	if d != nil {
		capacity += len(d.source)
	}
	out := bytes.NewBuffer(make([]byte, 0, capacity))
	_ = out.WriteByte('{')
	written := 0
	for _, name := range names {
		value, present := d.value(name)
		if !present {
			continue
		}
		if written > 0 {
			_ = out.WriteByte(',')
		}
		quotedName := strconv.AppendQuote(nil, name)
		_, _ = out.Write(quotedName)
		_ = out.WriteByte(':')
		_, _ = out.Write(value)
		written++
	}
	_ = out.WriteByte('}')
	return out.Bytes()
}

// CompilerOwnedBodyFields 是 prepare 边界从调用方 Body 中抽出的 attempt-local
// 身份材料。Metadata 只交给 service 构造结构化事实，不进入 Plan 的可写状态。
type CompilerOwnedBodyFields struct {
	Metadata                   map[string]string
	RefreshToken               string
	EventType                  string
	PreviousResponseIDReusable bool
	RoutingHint                CodexRoutingHintFacts
	HostedFileUploadPresent    bool
}

// PrepareOfficialCodexAttemptBody 在单个 attempt 起点复制并解析需要抽取
// compiler-owned 字段的 JSON Body。顶层 duplicate 全局校验完成后才允许写入
// 删除 overlay；其余 replayable JSON 延后到 Compiler 内进行同样的一次解析。
func PrepareOfficialCodexAttemptBody(
	endpointID string,
	body []byte,
) (RequestBody, CompilerOwnedBodyFields, error) {
	owned := append([]byte(nil), body...)
	requestBody := newOwnedReplayableRequestBody(owned)
	fields := CompilerOwnedBodyFields{Metadata: make(map[string]string)}
	if len(bytes.TrimSpace(owned)) == 0 || !endpointExtractsCompilerOwnedBody(endpointID) {
		return requestBody, fields, nil
	}
	document, err := newOrderedJSONDocument(owned)
	if err != nil {
		return RequestBody{}, CompilerOwnedBodyFields{}, fmt.Errorf("解析 Codex 语义 Body：%w", err)
	}
	if err := extractCompilerOwnedBodyFields(endpointID, document, &fields); err != nil {
		return RequestBody{}, CompilerOwnedBodyFields{}, err
	}
	requestBody.state.document = document
	return requestBody, fields, nil
}

func endpointExtractsCompilerOwnedBody(endpointID string) bool {
	switch endpointID {
	case "responses_http", "responses_compact", "responses_ws", "oauth_refresh", "files_create":
		return true
	default:
		return false
	}
}

func extractCompilerOwnedBodyFields(
	endpointID string,
	document *orderedJSONDocument,
	output *CompilerOwnedBodyFields,
) error {
	if officialCodexRoutingHintEndpoint(endpointID) {
		routingHint, err := optionalCodexRoutingHintFactsFromDocument(document)
		if err != nil {
			return err
		}
		output.RoutingHint = routingHint
	}
	if endpointID == "files_create" {
		hostedNames := []string{"codex_connector_id", "codex_action_name", "codex_model"}
		presentCount := 0
		for _, name := range hostedNames {
			value, present := document.value(name)
			if !present {
				continue
			}
			presentCount++
			var text string
			if err := json.Unmarshal(value, &text); err != nil || strings.TrimSpace(text) == "" {
				return fmt.Errorf("Codex hosted 文件上传字段 %s 非法", name)
			}
		}
		if presentCount != 0 && presentCount != len(hostedNames) {
			return errors.New("Codex hosted 文件上传三个上下文字段必须全有或全无")
		}
		output.HostedFileUploadPresent = presentCount == len(hostedNames)
	}
	if endpointID == "responses_ws" {
		typeRaw, present := document.value("type")
		if !present || json.Unmarshal(typeRaw, &output.EventType) != nil || strings.TrimSpace(output.EventType) == "" {
			return errors.New("Codex WebSocket 语义帧必须包含唯一 type")
		}
		output.EventType = strings.TrimSpace(output.EventType)
		if previousRaw, previousPresent := document.value("previous_response_id"); previousPresent {
			var previousResponseID string
			if json.Unmarshal(previousRaw, &previousResponseID) == nil && strings.TrimSpace(previousResponseID) != "" {
				output.PreviousResponseIDReusable = true
			}
		}
	}
	if endpointID == "responses_http" || endpointID == "responses_ws" {
		if metadataRaw, present := document.value("client_metadata"); present {
			metadata, err := newOrderedJSONDocument(metadataRaw)
			if err != nil {
				return errors.New("client_metadata 不是 JSON object")
			}
			for _, field := range metadata.fields {
				var text string
				if json.Unmarshal(field.value, &text) == nil {
					output.Metadata[field.name] = strings.TrimSpace(text)
				}
			}
			document.omit("client_metadata")
		}
	}
	if endpointID == "responses_http" || endpointID == "responses_compact" || endpointID == "responses_ws" {
		if inputRaw, present := document.value("input"); present {
			normalizedInput, changed, err := stripUnsupportedPromptCacheBreakpoints(inputRaw)
			if err != nil {
				return fmt.Errorf("清理 Responses prompt_cache_breakpoint：%w", err)
			}
			if changed {
				document.set("input", normalizedInput)
			}
		}
		if _, present := document.value("prompt_cache_key"); present {
			document.omit("prompt_cache_key")
		}
	}
	if endpointID == "responses_compact" {
		for _, name := range []string{
			"tool_choice", "store", "stream", "stream_options", "include",
			"service_tier", "client_metadata",
		} {
			if _, present := document.value(name); present {
				document.omit(name)
			}
		}
	}
	if endpointID == "oauth_refresh" {
		value, present := document.value("refresh_token")
		if !present {
			return nil
		}
		var token string
		if err := json.Unmarshal(value, &token); err != nil || strings.TrimSpace(token) == "" {
			return errors.New("OAuth refresh_token 非法")
		}
		output.RefreshToken = strings.TrimSpace(token)
		document.omit("refresh_token")
	}
	return nil
}

// stripUnsupportedPromptCacheBreakpoints 删除 OpenAI SDK 写入 input 内容块的
// 显式缓存断点提示。ChatGPT/Codex 官方出站目前不接受该字段，但删除它只会关闭
// 一次缓存提示，不会改变消息正文、工具调用或会话续链语义。
func stripUnsupportedPromptCacheBreakpoints(input json.RawMessage) (json.RawMessage, bool, error) {
	trimmed := bytes.TrimSpace(input)
	if len(trimmed) == 0 || !bytes.Contains(trimmed, []byte(`"`+unsupportedPromptCacheBreakpointField+`"`)) {
		return input, false, nil
	}
	return stripUnsupportedPromptCacheBreakpointValue(trimmed)
}

func stripUnsupportedPromptCacheBreakpointValue(raw json.RawMessage) (json.RawMessage, bool, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return raw, false, nil
	}

	switch trimmed[0] {
	case '{':
		document, err := newOrderedJSONDocument(trimmed)
		if err != nil {
			return nil, false, err
		}
		changed := false
		for _, field := range document.fields {
			if field.name == unsupportedPromptCacheBreakpointField {
				document.omit(field.name)
				changed = true
				continue
			}
			normalized, childChanged, childErr := stripUnsupportedPromptCacheBreakpointValue(field.value)
			if childErr != nil {
				return nil, false, childErr
			}
			if childChanged {
				document.set(field.name, normalized)
				changed = true
			}
		}
		if !changed {
			return raw, false, nil
		}
		return document.encodeSourceOrder(), true, nil
	case '[':
		decoder := json.NewDecoder(bytes.NewReader(trimmed))
		decoder.UseNumber()
		opening, err := decoder.Token()
		if err != nil {
			return nil, false, err
		}
		if delimiter, ok := opening.(json.Delim); !ok || delimiter != '[' {
			return nil, false, errors.New("Responses input 数组起始符非法")
		}
		items := make([]json.RawMessage, 0)
		changed := false
		for decoder.More() {
			var item json.RawMessage
			if err := decoder.Decode(&item); err != nil {
				return nil, false, err
			}
			normalized, childChanged, childErr := stripUnsupportedPromptCacheBreakpointValue(item)
			if childErr != nil {
				return nil, false, childErr
			}
			if childChanged {
				item = normalized
				changed = true
			}
			items = append(items, item)
		}
		closing, err := decoder.Token()
		if err != nil {
			return nil, false, err
		}
		if delimiter, ok := closing.(json.Delim); !ok || delimiter != ']' {
			return nil, false, errors.New("Responses input 数组未正确闭合")
		}
		var trailing ignoredJSONValue
		if err := decoder.Decode(&trailing); err != io.EOF {
			if err == nil {
				return nil, false, errors.New("Responses input 数组后存在额外值")
			}
			return nil, false, err
		}
		if !changed {
			return raw, false, nil
		}
		var encoded bytes.Buffer
		_ = encoded.WriteByte('[')
		for i, item := range items {
			if i > 0 {
				_ = encoded.WriteByte(',')
			}
			_, _ = encoded.Write(bytes.TrimSpace(item))
		}
		_ = encoded.WriteByte(']')
		return encoded.Bytes(), true, nil
	default:
		return raw, false, nil
	}
}
