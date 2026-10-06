package service

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

const (
	openAIWSClientReadLimitBytesDefault     int64 = 64 * 1024 * 1024
	openAIWSHTTPBridgeThresholdBytesDefault int64 = 15 * 1024 * 1024
	openAIWSHTTPBridgeErrorBodyLimitBytes         = 64 * 1024
)

const (
	openAIWSHTTPBridgeToolStateContextKey       = "openai_ws_http_bridge_tool_state"
	openAIWSHTTPBridgeSessionAffinityContextKey = "openai_ws_http_bridge_session_affinity"
)

type openAIWSHTTPBridgeToolState struct {
	ClientMapping apicompat.ResponsesClientToolMapping
	LoweredTools  json.RawMessage
}

func openAIWSHTTPBridgeToolStateFromContext(c *gin.Context) (openAIWSHTTPBridgeToolState, bool) {
	if c == nil {
		return openAIWSHTTPBridgeToolState{}, false
	}
	value, ok := c.Get(openAIWSHTTPBridgeToolStateContextKey)
	state, typed := value.(openAIWSHTTPBridgeToolState)
	return state, ok && typed
}

func setOpenAIWSHTTPBridgeToolState(c *gin.Context, state openAIWSHTTPBridgeToolState) {
	if c == nil {
		return
	}
	state.LoweredTools = append(json.RawMessage(nil), state.LoweredTools...)
	c.Set(openAIWSHTTPBridgeToolStateContextKey, state)
}

// freezeOpenAIWSHTTPBridgeSessionAffinity 在客户端 WS 连接首次进入 HTTP bridge 时
// 冻结会话锚点。后续 turn 的正文和 failover 账号都可能变化，但同一连接必须继续
// 使用首次锚点，避免已冻结的官方 Persona invocation 身份发生漂移。
func freezeOpenAIWSHTTPBridgeSessionAffinity(c *gin.Context, sessionHash string) {
	if c == nil || strings.TrimSpace(sessionHash) == "" {
		return
	}
	if current, exists := c.Get(openAIWSHTTPBridgeSessionAffinityContextKey); exists {
		if value, ok := current.(string); ok && strings.TrimSpace(value) != "" {
			return
		}
	}
	c.Set(openAIWSHTTPBridgeSessionAffinityContextKey, strings.TrimSpace(sessionHash))
}

func openAIWSHTTPBridgeSessionAffinity(c *gin.Context) string {
	if c == nil {
		return ""
	}
	value, exists := c.Get(openAIWSHTTPBridgeSessionAffinityContextKey)
	if !exists {
		return ""
	}
	affinity, _ := value.(string)
	return strings.TrimSpace(affinity)
}

func decodeOpenAIWSHTTPBridgeLoweredTools(raw json.RawMessage) []any {
	if len(raw) == 0 {
		return nil
	}
	var tools []any
	if err := json.Unmarshal(raw, &tools); err != nil {
		return nil
	}
	return tools
}

func openAIWSHTTPBridgeRawField(body []byte, name string) (json.RawMessage, bool) {
	if !json.Valid(body) || !openAIBodyRoot(body).IsObject() {
		return nil, false
	}
	// 仅复制需要跨 turn 保留的小字段，避免为了读取 tools 连同整份 input 一起复制。
	// 逐项遍历保留末次同名值，与 map[string]json.RawMessage 的重复键语义相同。
	var raw string
	present := false
	openAIBodyRoot(body).ForEach(func(key, value gjson.Result) bool {
		if key.Str == name {
			raw, present = value.Raw, true
		}
		return true
	})
	return append(json.RawMessage(nil), raw...), present
}

func openAIWSHTTPBridgeToolUpstreamName(account *Account) string {
	if account != nil && account.Platform == PlatformGrok {
		return "Grok WS HTTP bridge"
	}
	return "OpenAI WS HTTP bridge"
}

// ResolveOpenAIWSClientFirstMessageTimeout returns the effective client ingress deadline.
func ResolveOpenAIWSClientFirstMessageTimeout(cfg *config.Config) time.Duration {
	seconds := config.DefaultOpenAIWSClientFirstMessageTimeoutSeconds
	if cfg != nil && cfg.Gateway.OpenAIWS.ClientFirstMessageTimeoutSeconds > 0 {
		seconds = cfg.Gateway.OpenAIWS.ClientFirstMessageTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func ResolveOpenAIWSClientReadLimitBytes(cfg *config.Config) int64 {
	if cfg == nil || cfg.Gateway.OpenAIWS.ClientReadLimitBytes <= 0 {
		return openAIWSClientReadLimitBytesDefault
	}
	return cfg.Gateway.OpenAIWS.ClientReadLimitBytes
}

func (s *OpenAIGatewayService) openAIWSReplayInputLimits() openAIWSReplayInputLimits {
	return defaultOpenAIWSReplayInputLimits()
}

func (s *OpenAIGatewayService) openAIWSHTTPBridgeEnabled() bool {
	return s != nil && s.cfg != nil && s.cfg.Gateway.OpenAIWS.HTTPBridgeEnabled
}

func (s *OpenAIGatewayService) openAIWSHTTPBridgeThresholdBytes() int64 {
	if s == nil || s.cfg == nil || s.cfg.Gateway.OpenAIWS.HTTPBridgeThresholdBytes <= 0 {
		return openAIWSHTTPBridgeThresholdBytesDefault
	}
	return s.cfg.Gateway.OpenAIWS.HTTPBridgeThresholdBytes
}

func (s *OpenAIGatewayService) shouldBridgeOpenAIWSHTTP(account *Account, payloadBytes int, previousResponseID string) bool {
	if account != nil && account.Platform == PlatformGrok {
		return true
	}
	if !s.openAIWSHTTPBridgeEnabled() {
		return false
	}
	if strings.TrimSpace(previousResponseID) != "" {
		return false
	}
	threshold := s.openAIWSHTTPBridgeThresholdBytes()
	return threshold > 0 && int64(payloadBytes) >= threshold
}

func (s *OpenAIGatewayService) shouldBridgeOpenAIWSPassthroughFirstMessage(account *Account, payload []byte) bool {
	if account != nil && account.Platform == PlatformGrok {
		return true
	}
	if !s.openAIWSHTTPBridgeEnabled() || int64(len(payload)) < s.openAIWSHTTPBridgeThresholdBytes() {
		return false
	}
	if !json.Valid(payload) {
		return false
	}

	i := skipOpenAIWSJSONSpace(payload, 0)
	if i >= len(payload) || payload[i] != '{' {
		return false
	}
	i++
	eventType := "response.create"
	previousResponseID := ""
	typeSeen, previousResponseIDSeen := false, false
	for {
		i = skipOpenAIWSJSONSpace(payload, i)
		if payload[i] == '}' {
			break
		}
		keyStart := i
		keyEnd := scanOpenAIWSJSONString(payload, keyStart)
		i = skipOpenAIWSJSONSpace(payload, keyEnd)
		i++ // json.Valid guarantees the colon.
		i = skipOpenAIWSJSONSpace(payload, i)
		valueStart := i
		i = skipOpenAIWSJSONValue(payload, i)

		key := ""
		// A critical key is at most 20 decoded bytes. The generous encoded bound
		// covers escaped spellings without allocating attacker-sized key strings.
		if keyEnd-keyStart <= 128 {
			_ = json.Unmarshal(payload[keyStart:keyEnd], &key)
		}
		switch key {
		case "type":
			if typeSeen {
				return false
			}
			typeSeen = true
			var value *string
			if err := json.Unmarshal(payload[valueStart:i], &value); err != nil {
				return false
			}
			if value == nil || strings.TrimSpace(*value) == "" {
				eventType = "response.create"
			} else {
				eventType = strings.TrimSpace(*value)
			}
		case "previous_response_id":
			if previousResponseIDSeen {
				return false
			}
			previousResponseIDSeen = true
			var value *string
			if err := json.Unmarshal(payload[valueStart:i], &value); err != nil {
				return false
			}
			if value != nil {
				previousResponseID = strings.TrimSpace(*value)
			}
		}
		i = skipOpenAIWSJSONSpace(payload, i)
		if payload[i] == ',' {
			i++
		}
	}
	return eventType == "response.create" && previousResponseID == ""
}

func skipOpenAIWSJSONSpace(payload []byte, i int) int {
	for i < len(payload) {
		switch payload[i] {
		case ' ', '\t', '\r', '\n':
			i++
		default:
			return i
		}
	}
	return i
}

func scanOpenAIWSJSONString(payload []byte, i int) int {
	for i++; i < len(payload); i++ {
		switch payload[i] {
		case '\\':
			i++
		case '"':
			return i + 1
		}
	}
	return len(payload)
}

func skipOpenAIWSJSONValue(payload []byte, i int) int {
	if payload[i] == '"' {
		return scanOpenAIWSJSONString(payload, i)
	}
	if payload[i] != '{' && payload[i] != '[' {
		for i < len(payload) && payload[i] != ',' && payload[i] != '}' {
			i++
		}
		return i
	}
	depth := 0
	for ; i < len(payload); i++ {
		switch payload[i] {
		case '"':
			i = scanOpenAIWSJSONString(payload, i) - 1
		case '{', '[':
			depth++
		case '}', ']':
			depth--
			if depth == 0 {
				return i + 1
			}
		}
	}
	return len(payload)
}

func prepareOpenAIWSHTTPBridgeBody(account *Account, payload []byte) ([]byte, error) {
	return prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account, payload, nil)
}

// prepareOpenAIWSHTTPBridgeBodyWithWorkspace 保留旧 json.Marshal 的字段排序、转义与数字语义。
// 普通官方桥接只准备逻辑对象树，原文仍供 model/会话等不变字段读取；最终规范化由片段编码器完成。
// 复杂的 Lite 转换保留原物化路径。官方 bridge 的长字符串直接引用 WS 原文，对象树只移交一次。
func prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account *Account, payload []byte, workspace *officialForwardHTTPBody) ([]byte, error) {
	var body map[string]any
	index := workspace.indexFor(payload)
	if index != nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		body, _ = index.decodeValueSharingBody(index.root).(map[string]any)
	} else {
		if err := decodeOpenAIJSONUseNumber(payload, &body); err != nil {
			return nil, err
		}
	}
	if body == nil {
		return nil, errors.New("response.create payload must be a JSON object")
	}
	if workspace != nil && workspace.wsIngress != nil {
		if err := workspace.wsIngress.applyHeaders(body); err != nil {
			return nil, err
		}
	}
	delete(body, "type")
	delete(body, "generate")
	delete(body, "previous_response_id")
	deleteOpenAIResponsesNoneReasoningEffortFromObject(account, body)
	body["stream"] = true
	if workspace != nil && account != nil && account.IsOpenAIOAuth() {
		// namespace、内容回放和触发项需要多次变换，先沿用旧基准，避免合并阶段改变键序。
		// 任意层的重复键或非法 UTF-8 也须先物化，保证后续 gjson 身份读取与旧规范化正文一致。
		_, handled, _ := normalizeOpenAIResponsesLiteHeaderFields(payload)
		if handled && openAIWSHTTPBridgeCanKeepRawBody(index) {
			workspace.bridge = &openAIWSHTTPBridgeBody{source: payload, account: account, lite: isOpenAIResponsesLiteWebSocketPayload(payload)}
			if workspace.wsIngress != nil {
				headerIndex, err := workspace.wsIngress.preparedHeaderIndex(account)
				if err != nil {
					return nil, err
				}
				workspace.bridge.headerIndex = headerIndex
			}
			workspace.releaseRequestMap(&body, newOpenAIRequestView(payload), payload)
			return payload, nil
		}
	}
	prepared, err := json.Marshal(body)
	if err == nil && workspace != nil {
		workspace.index, workspace.indexBody = nil, nil
		workspace.releaseRequestMap(&body, newOpenAIRequestView(prepared), prepared)
	}
	return prepared, err
}

func openAIWSHTTPBridgeCanKeepRawBody(index *officialJSONRawIndex) bool {
	if index == nil || !utf8.Valid(index.body) {
		return false
	}
	for node := range index.nodes {
		if index.nodes[node].kind == officialJSONRawKindObject &&
			len(index.uniqueKeys(int32(node))) != len(index.objectMembers(int32(node))) {
			return false
		}
	}
	return true
}

// openAIWSHTTPBridgeBody 保存未物化的 bridge 准备状态。source 在整个 turn 内只读，
// prepared 只在上游错误或复杂工具呈现确实需要连续正文时建立，不进入普通成功路径。
type openAIWSHTTPBridgeBody struct {
	source   []byte
	account  *Account
	lite     bool
	prepared []byte
	// 入口小字段已先完成归一化，prepare 后的独立索引保留它们正确的规范键序。
	headerIndex *officialJSONRawIndex
}

func (b *officialForwardHTTPBody) hasUnmaterializedWSHTTPBridgeBody(body []byte) bool {
	return b != nil && b.bridge != nil && b.bridge.prepared == nil && officialForwardSameBody(b.bridge.source, body)
}

func (b *officialForwardHTTPBody) materializeWSHTTPBridgeBody(body []byte) ([]byte, error) {
	if b == nil || b.bridge == nil {
		return body, nil
	}
	if b.bridge.prepared != nil {
		if officialForwardSameBody(b.bridge.source, body) {
			return b.bridge.prepared, nil
		}
		return body, nil
	}
	if !officialForwardSameBody(b.bridge.source, body) {
		return body, nil
	}
	source := b.bridge.source
	if b.wsIngress != nil {
		source = b.wsIngress.materialize()
	}
	prepared, err := prepareOpenAIWSHTTPBridgeBody(b.bridge.account, source)
	if err != nil {
		return nil, err
	}
	if b.bridge.lite {
		if normalized, changed, normalizeErr := normalizeOpenAIResponsesLitePayloadForAccount(prepared, b.bridge.account); normalizeErr != nil {
			return nil, normalizeErr
		} else if changed {
			prepared = normalized
		}
	}
	b.bridge.prepared = prepared
	b.index, b.indexBody = nil, nil
	return prepared, nil
}

// finalizeHTTPBodyPayloadMembers 仍使用同一个官方 Finalizer 做全部字段修正与契约校验。
// bridge 仅替换编码基准：旧 prepare 会规范化所有原始对象键与字符串，新的片段编码器复现同一结果。
func (b *officialForwardHTTPBody) finalizeHTTPBodyPayloadMembers(
	payload map[string]any,
	body []byte,
	index *officialJSONRawIndex,
	contract *officialOpenAIHTTPBodyContract,
	defaults officialOpenAIReasoningDefaults,
	options officialOpenAIHTTPBodyOptions,
) ([]officialegress.JSONObjectMember, bool, error) {
	if !b.hasUnmaterializedWSHTTPBridgeBody(body) {
		return finalizeOfficialOpenAIHTTPBodyPayloadMembers(payload, body, index, contract, defaults, options)
	}
	if _, err := finalizeOfficialOpenAIHTTPBodyPayloadInPlace(payload, contract, defaults, options); err != nil {
		return nil, false, err
	}
	order, err := officialCodexBodyFieldOrderForMode(options.ProfileMode, officialCodexEndpointResponsesHTTP)
	if err != nil {
		return nil, false, err
	}
	keys := officialJSONOrderedTopLevelKeys(payload, order, nil)
	members := make([]officialegress.JSONObjectMember, 0, len(keys))
	for _, key := range keys {
		quotedKey, err := json.Marshal(key)
		if err != nil {
			return nil, false, err
		}
		writer := openAIWSHTTPBridgeJSONSegments{}
		valueIndex := index
		if key != "input" && b.bridge.headerIndex != nil {
			valueIndex = b.bridge.headerIndex
		}
		if b.bridge.headerIndex != nil {
			writer.inputIndex, writer.headerIndex = index, b.bridge.headerIndex
		}
		if err := writer.appendValue(valueIndex, payload[key], valueIndex.memberNode(valueIndex.root, key)); err != nil {
			return nil, false, err
		}
		writer.flush()
		member := officialegress.JSONObjectMember{Name: key, QuotedName: quotedKey}
		if len(writer.segments) == 1 {
			member.Value = writer.segments[0]
		} else {
			member.ValueSegments = writer.segments
		}
		members = append(members, member)
	}
	// 即使就地修正没有变更，也要写出已删除 WS 字段、强制 stream 和规范化后的逻辑正文。
	return members, true, nil
}

// openAIWSHTTPBridgeJSONSegments 把短键、标点等汇入有界小段，长字符串直接引用只读原文。
// 片段只在完整 JSON token 之间断开；数组项可以跨段，交由编译器的跨段校验与压缩处理。
type openAIWSHTTPBridgeJSONSegments struct {
	segments [][]byte
	buffer   []byte
	// 延迟入口的逻辑原文由两份索引组成。复合值匹配只能搜索真实 input 和
	// 已归一化的小字段，不能引用原正文里已被身份隔离替换的小字段旧值。
	inputIndex  *officialJSONRawIndex
	headerIndex *officialJSONRawIndex
}

func (w *openAIWSHTTPBridgeJSONSegments) flush() {
	if len(w.buffer) > 0 {
		// 长 token 会频繁分隔短标点。已写区间冻结后继续使用同一小块的剩余容量，
		// 避免每个长字符串前的几个字节都独占 4KiB；片段容量收紧，禁止扩展覆盖后续片段。
		used := len(w.buffer)
		w.segments = append(w.segments, w.buffer[:used:used])
		w.buffer = w.buffer[used:used]
	}
}

func (w *openAIWSHTTPBridgeJSONSegments) appendBytes(value []byte) {
	// 中等长度的工具输入/输出也直接引用原 token。只把短键和标点汇入小块，
	// 避免几百字节的历史内容在原文之外又累计成数 MiB 的片段副本。
	if len(value) >= 128 {
		w.flush()
		w.segments = append(w.segments, value)
		return
	}
	if len(w.buffer)+len(value) > cap(w.buffer) {
		w.flush()
	}
	if cap(w.buffer)-len(w.buffer) < len(value) {
		w.buffer = make([]byte, 0, 4096)
	}
	w.buffer = append(w.buffer, value...)
}

func (w *openAIWSHTTPBridgeJSONSegments) appendByte(value byte) {
	if len(w.buffer) == cap(w.buffer) {
		w.flush()
	}
	if cap(w.buffer) == 0 {
		w.buffer = make([]byte, 0, 4096)
	}
	w.buffer = append(w.buffer, value)
}

// appendCanonicalNode 与 json.Marshal(以 UseNumber 解码的原节点) 相同：对象键排序、重复键取末值、
// 原有 HTML 字符和 Unicode 行分隔符按标准库转义；已规范的长字符串与数字直接共享原 token。
func (w *openAIWSHTTPBridgeJSONSegments) appendCanonicalNode(index *officialJSONRawIndex, node int32) error {
	n := &index.nodes[node]
	switch n.kind {
	case officialJSONRawKindObject:
		var local [16]string
		keys := append(local[:0], index.uniqueKeys(node)...)
		sort.Strings(keys)
		w.appendByte('{')
		for i, key := range keys {
			if i > 0 {
				w.appendByte(',')
			}
			quoted, _ := json.Marshal(key)
			w.appendBytes(quoted)
			w.appendByte(':')
			if err := w.appendCanonicalNode(index, index.memberNode(node, key)); err != nil {
				return err
			}
		}
		w.appendByte('}')
	case officialJSONRawKindArray:
		w.appendByte('[')
		for i, child := range index.arrayItems(node) {
			if i > 0 {
				w.appendByte(',')
			}
			if err := w.appendCanonicalNode(index, child); err != nil {
				return err
			}
		}
		w.appendByte(']')
	case officialJSONRawKindString:
		raw := index.raw(node)
		if !n.slowPath && !bytes.ContainsAny(raw, "<>&\u2028\u2029") {
			w.appendBytes(raw)
			return nil
		}
		encoded, err := json.Marshal(index.decodeString(node))
		if err != nil {
			return err
		}
		w.appendBytes(encoded)
	default:
		w.appendBytes(index.raw(node))
	}
	return nil
}

// appendValue 复现原保序拼接器的内容匹配与同位置回退，只把“原始字节”换成 prepare 后的规范化 token。
// 新增/修改的标量沿用 marshalOpenAIUpstreamJSON，保持旧 Finalizer 对新值关闭 HTML 转义的行为。
func (w *openAIWSHTTPBridgeJSONSegments) appendValue(index *officialJSONRawIndex, value any, node int32) error {
	if node >= 0 && index.equals(node, value) {
		return w.appendCanonicalNode(index, node)
	}
	if pooled, found := w.lookupComposite(index, value); found >= 0 {
		return w.appendCanonicalNode(pooled, found)
	}
	switch typed := value.(type) {
	case map[string]any:
		var original []string
		if node >= 0 && index.nodes[node].kind == officialJSONRawKindObject {
			original = append([]string(nil), index.uniqueKeys(node)...)
			sort.Strings(original)
		}
		keys := make([]string, 0, len(typed))
		for _, key := range original {
			if _, exists := typed[key]; exists {
				keys = append(keys, key)
			}
		}
		additional := make([]string, 0)
		for key := range typed {
			if !slices.Contains(original, key) {
				additional = append(additional, key)
			}
		}
		sort.Strings(additional)
		keys = append(keys, additional...)
		w.appendByte('{')
		for i, key := range keys {
			if i > 0 {
				w.appendByte(',')
			}
			quoted, _ := json.Marshal(key)
			w.appendBytes(quoted)
			w.appendByte(':')
			child := int32(-1)
			if node >= 0 {
				child = index.memberNode(node, key)
			}
			if err := w.appendValue(index, typed[key], child); err != nil {
				return err
			}
		}
		w.appendByte('}')
	case []any:
		var original []int32
		if node >= 0 && index.nodes[node].kind == officialJSONRawKindArray {
			original = index.arrayItems(node)
		}
		used := make([]bool, len(original))
		w.appendByte('[')
		for i, item := range typed {
			if i > 0 {
				w.appendByte(',')
			}
			match := index.matchArrayItem(item, original, used)
			if match < 0 && i < len(original) && !used[i] {
				match = i
			}
			child := int32(-1)
			if match >= 0 {
				used[match], child = true, original[match]
			}
			if err := w.appendValue(index, item, child); err != nil {
				return err
			}
		}
		w.appendByte(']')
	case json.RawMessage:
		if json.Valid(typed) {
			w.appendBytes(typed)
			return nil
		}
		return fmt.Errorf("WS HTTP bridge 包含非法原始 JSON 值")
	default:
		encoded, err := marshalOpenAIUpstreamJSON(value)
		if err != nil {
			return err
		}
		w.appendBytes(encoded)
	}
	return nil
}

func (w *openAIWSHTTPBridgeJSONSegments) lookupComposite(index *officialJSONRawIndex, value any) (*officialJSONRawIndex, int32) {
	if w.headerIndex == nil {
		return index, index.lookupComposite(value)
	}
	switch value.(type) {
	case map[string]any, []any:
	default:
		return nil, -1
	}
	hash, ok := index.hashValue(value)
	if !ok {
		return nil, -1
	}
	// 小正文的 input=[] 只是占位符，根对象也并非真实正文，二者都不参与匹配。
	placeholder := w.headerIndex.memberNode(w.headerIndex.root, "input")
	for _, node := range w.headerIndex.byHash[hash] {
		if node != w.headerIndex.root && node != placeholder && w.headerIndex.equals(node, value) {
			return w.headerIndex, node
		}
	}
	input := w.inputIndex.memberNode(w.inputIndex.root, "input")
	if input >= 0 {
		span := w.inputIndex.nodes[input]
		for _, node := range w.inputIndex.byHash[hash] {
			candidate := w.inputIndex.nodes[node]
			if candidate.start >= span.start && candidate.end <= span.end && w.inputIndex.equals(node, value) {
				return w.inputIndex, node
			}
		}
	}
	return nil, -1
}

type openAIWSToolCallReplayCollector struct {
	items    []json.RawMessage
	seen     map[string]struct{}
	allItems []json.RawMessage
	allSeen  map[string]struct{}
}

func (c *openAIWSToolCallReplayCollector) AddEvent(eventType string, message []byte) {
	switch strings.TrimSpace(eventType) {
	case "response.output_item.done":
		item := gjson.GetBytes(message, "item")
		c.addEventItem(item)
	case "response.completed", "response.done":
		output := gjson.GetBytes(message, "response.output")
		if !output.IsArray() {
			return
		}
		for _, item := range output.Array() {
			c.addEventItem(item)
		}
	}
}

// Items/AllItems 返回浅拷贝头数组；正文由 collector 独立分配且此后不可变，
// 调用方按 replay 所有权不变式共享持有。
func (c *openAIWSToolCallReplayCollector) Items() []json.RawMessage {
	return slices.Clone(c.items)
}

func (c *openAIWSToolCallReplayCollector) AllItems() []json.RawMessage {
	return slices.Clone(c.allItems)
}

func (c *openAIWSToolCallReplayCollector) addEventItem(item gjson.Result) {
	if !item.Exists() || item.Type != gjson.JSON {
		return
	}
	raw := strings.TrimSpace(item.Raw)
	if raw == "" || !strings.HasPrefix(raw, "{") || strings.TrimSpace(item.Get("type").String()) == "" {
		return
	}
	key := strings.TrimSpace(item.Get("id").String())
	if key == "" {
		key = strings.TrimSpace(item.Get("call_id").String())
	}
	if key == "" {
		key = raw
	}
	_, allSeen := c.allSeen[key]
	isContext := isCodexToolCallContextItemType(item.Get("type").String())
	_, contextSeen := c.seen[key]
	if allSeen && (!isContext || contextSeen) {
		return
	}
	rawMessage := json.RawMessage([]byte(raw))
	if !allSeen {
		if c.allSeen == nil {
			c.allSeen = make(map[string]struct{})
		}
		c.allSeen[key] = struct{}{}
		c.allItems = append(c.allItems, rawMessage)
	}
	if !isContext || contextSeen {
		return
	}
	if c.seen == nil {
		c.seen = make(map[string]struct{})
	}
	c.seen[key] = struct{}{}
	c.items = append(c.items, rawMessage)
}

func buildOpenAIWSHTTPBridgeErrorEvent(statusCode int, message string) []byte {
	message = strings.TrimSpace(message)
	if message == "" {
		message = http.StatusText(statusCode)
	}
	if message == "" {
		message = "upstream request failed"
	}
	event := map[string]any{
		"type":            "error",
		"sequence_number": 0,
		"status":          statusCode,
		"error": map[string]any{
			"type":    "upstream_error",
			"message": message,
		},
	}
	body, err := json.Marshal(event)
	if err != nil {
		return []byte(`{"type":"error","sequence_number":0,"error":{"type":"upstream_error","message":"upstream request failed"}}`)
	}
	return body
}

func buildOpenAIWSHTTPBridgeFailedEvent(responseID, model string, source []byte, fallbackMessage string) []byte {
	errorType := strings.TrimSpace(gjson.GetBytes(source, "error.type").String())
	if errorType == "" {
		errorType = strings.TrimSpace(gjson.GetBytes(source, "response.error.type").String())
	}
	code := strings.TrimSpace(gjson.GetBytes(source, "error.code").String())
	if code == "" {
		code = strings.TrimSpace(gjson.GetBytes(source, "response.error.code").String())
	}
	if code == "" {
		code = "upstream_error"
	}
	message := extractOpenAISSEErrorMessage(source)
	if message == "" {
		message = strings.TrimSpace(fallbackMessage)
	}
	if message == "" {
		message = "Upstream response failed"
	}
	errorBody := map[string]any{"code": code, "message": message}
	if errorType != "" {
		errorBody["type"] = errorType
	}
	response := map[string]any{
		"id": responseID, "object": "response", "status": "failed",
		"output": []any{}, "error": errorBody,
	}
	if model = strings.TrimSpace(model); model != "" {
		response["model"] = model
	}
	body, err := json.Marshal(map[string]any{"type": "response.failed", "sequence_number": 0, "response": response})
	if err != nil {
		return []byte(`{"type":"response.failed","sequence_number":0,"response":{"status":"failed","output":[],"error":{"code":"upstream_error","message":"Upstream response failed"}}}`)
	}
	return body
}

func (s *OpenAIGatewayService) proxyOpenAIWSHTTPBridgeTurn(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	token string,
	payload []byte,
	payloadBytes int,
	originalModel string,
	imageBillingModel string,
	imageSizeTier string,
	imageInputSize string,
	grokCacheIdentity string,
	turn int,
	writeClientMessage func([]byte) error,
) (result *OpenAIForwardResult, returnErr error) {
	// 所有轮次、所有构建/发送/读取阶段统一保留本地故障原因，交由 WS 入口
	// 发送 1013。不得合成上游 502，也不得让首轮错误进入账号换号链路。
	defer func() {
		if IsRequestBodyStorageError(returnErr) {
			returnErr = NewOpenAIWSClientCloseError(coderws.StatusTryAgainLater,
				"request body storage temporarily unavailable; retry later", returnErr)
		}
	}()
	if s == nil {
		return nil, errors.New("service is nil")
	}
	if s.httpUpstream == nil {
		return nil, errors.New("openai http upstream is nil")
	}
	if account == nil {
		return nil, errors.New("account is nil")
	}
	if writeClientMessage == nil {
		return nil, errors.New("client websocket writer is nil")
	}
	responseModelObserver := &upstreamResponseModelObserver{}
	// HTTP bridge 的 turn-state 属于当前 WebSocket 连接的回合状态；它只能
	// 作为 Finalizer 的显式输入进入官方 wire，不能在 Finalizer 之后补写请求头。
	officialEgressTurnState := ""
	if c != nil {
		officialEgressTurnState = strings.TrimSpace(c.GetHeader(openAIWSTurnStateHeader))
	}

	var profileErr error
	officialEgressEnabled := false
	if account.Platform == PlatformOpenAI {
		officialEgressEnabled, _, profileErr = resolveOfficialEgressAccountProfile(account)
	}
	var officialForwardBody *officialForwardHTTPBody
	if officialEgressEnabled && profileErr == nil {
		ctx, officialForwardBody = newOfficialForwardHTTPBody(ctx, payload)
	}
	defer officialForwardBody.closeStorage()
	if deferred := openAIWSDeferredIngressBodyFromContext(ctx, payload); deferred != nil {
		if officialForwardBody == nil {
			payload = deferred.materialize()
		} else {
			officialForwardBody.wsIngress = deferred
			officialForwardBody.index, officialForwardBody.indexBody = deferred.index, payload
			// 索引所有权移交工作区，attempt 前释放时不再被入口状态额外保活。
			deferred.index = nil
		}
	}
	body, err := prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account, payload, officialForwardBody)
	if err != nil {
		return nil, fmt.Errorf("prepare http bridge body: %w", err)
	}
	if profileErr != nil {
		return nil, fmt.Errorf("resolve official egress config: %w", profileErr)
	}
	var officialEgressBodyContract *officialOpenAIHTTPBodyContract
	upstreamRequestContext := c
	managedOfficialBridge := false
	if account.Platform == PlatformOpenAI {
		if officialEgressEnabled {
			managedOfficialBridge = true
			officialEgressBodyContract, err = officialForwardBody.captureContract(c, body)
			if err != nil {
				return nil, fmt.Errorf("capture OpenAI HTTP bridge body contract: %w", err)
			}
			upstreamRequestContext, err = newOpenAIOfficialEgressHTTPBridgeContext(c, officialEgressBodyContract)
			if err != nil {
				return nil, err
			}
		}
	}
	if managedOfficialBridge {
		ctx, err = bindOfficialEgressSink(ctx, officialEgressSinkResponsesWSHTTPBridge)
		if err != nil {
			return nil, fmt.Errorf("bind WebSocket HTTP bridge official egress sink: %w", err)
		}
	}

	var grokIntentSourceBody []byte
	grokExplicitToolsField, grokExplicitToolIntent := false, false
	if account.Platform == PlatformGrok {
		// 只有 Grok 缓存路由需要保留适配前意图，OpenAI bridge 不持有这份整段副本。
		grokIntentSourceBody = append([]byte(nil), body...)
		_, grokExplicitToolsField = openAIWSHTTPBridgeRawField(grokIntentSourceBody, "tools")
		grokExplicitToolIntent = hasGrokResponsesToolIntent(grokIntentSourceBody)
	}
	var clientToolMapping apicompat.ResponsesClientToolMapping
	functionToolUpstream := (account.Platform == PlatformOpenAI && account.Type == AccountTypeAPIKey) || account.Platform == PlatformGrok
	if functionToolUpstream {
		if account.Platform == PlatformGrok {
			body, err = sanitizeGrokResponsesInput(body)
			if err != nil {
				return nil, fmt.Errorf("sanitize Grok WS HTTP bridge input: %w", err)
			}
		}
		inheritedState, _ := openAIWSHTTPBridgeToolStateFromContext(c)
		inheritedLoweredTools := decodeOpenAIWSHTTPBridgeLoweredTools(inheritedState.LoweredTools)
		body, clientToolMapping, err = adaptResponsesClientToolsForFunctionUpstreamWithMapping(
			body,
			openAIWSHTTPBridgeToolUpstreamName(account),
			inheritedState.ClientMapping,
			inheritedLoweredTools,
		)
		if err != nil {
			return nil, fmt.Errorf("adapt %s client tools: %w", openAIWSHTTPBridgeToolUpstreamName(account), err)
		}
		if account.Platform == PlatformGrok && !grokExplicitToolsField && !grokExplicitToolIntent && len(inheritedLoweredTools) > 0 && hasGrokResponsesToolIntent(body) {
			// This continuation omitted tools, so the pre-adapter source cannot
			// represent the effective inherited declarations. Cache routing must
			// see the rehydrated tool intent or it will replace client functions
			// with the native-search tool-free route. Explicit current-turn tool
			// intent still uses the original pre-sanitization source above.
			grokIntentSourceBody = append(grokIntentSourceBody[:0], body...)
		}
		loweredTools := inheritedState.LoweredTools
		if currentTools, present := openAIWSHTTPBridgeRawField(body, "tools"); present {
			loweredTools = currentTools
		}
		setOpenAIWSHTTPBridgeToolState(c, openAIWSHTTPBridgeToolState{
			ClientMapping: clientToolMapping,
			LoweredTools:  loweredTools,
		})
	}
	if account.Platform != PlatformGrok && isOpenAIResponsesLiteWebSocketPayload(payload) {
		liteBody, liteChanged, liteErr := officialForwardBody.normalizeResponsesLitePayloadForAccount(body, account)
		if liteErr != nil {
			return nil, fmt.Errorf("normalize responses Lite payload: %w", liteErr)
		}
		if liteChanged {
			body = liteBody
		}
	}

	buildUpstreamRequest := func(requestBody []byte) (*http.Request, error) {
		upstreamCtx, releaseUpstreamCtx := detachUpstreamContext(ctx)
		defer releaseUpstreamCtx()
		var upstreamReq *http.Request
		var buildErr error
		if account.Platform == PlatformGrok {
			upstreamReq, buildErr = buildGrokResponsesRequest(upstreamCtx, c, account, requestBody, token, grokCacheIdentity, s.cfg, s.settingService)
		} else {
			promptCacheKey := strings.TrimSpace(gjson.GetBytes(requestBody, "prompt_cache_key").String())
			if officialForwardBody.hasUnmaterializedWSHTTPBridgeBody(requestBody) && officialForwardBody.wsIngress != nil {
				promptCacheKey = strings.TrimSpace(openAIBodyGet(officialForwardBody.wsIngress.headers, "prompt_cache_key").String())
			}
			upstreamReq, buildErr = s.buildUpstreamRequestOpenAIPassthroughWithPlan(
				upstreamCtx,
				upstreamRequestContext,
				account,
				requestBody,
				token,
				openAIUpstreamRequestPlan{
					IsStream:                   true,
					PromptCacheKey:             promptCacheKey,
					OfficialEgressBodyContract: officialEgressBodyContract,
					OfficialEgressTurnState:    officialEgressTurnState,
				},
			)
		}
		if buildErr != nil {
			return nil, buildErr
		}
		if officialForwardBody != nil {
			if bridge := officialForwardBody.bridge; bridge != nil && bridge.prepared != nil && officialForwardSameBody(body, bridge.source) {
				body = bridge.prepared
				officialForwardBody.bridge = nil
			}
			// Finalizer 已接收成员来源，attempt 期间无需继续保留用于对象树解码的索引。
			officialForwardBody.index, officialForwardBody.indexBody = nil, nil
		}
		// OAuth 账号的 Lite 能力只能来自服务端模型 manifest；API Key 和
		// SetupToken 没有同一份能力清单，继续保留既有客户端协商。
		if account.Platform != PlatformGrok && !account.IsOpenAIOAuth() {
			if isOpenAIResponsesLiteWebSocketPayload(payload) {
				upstreamReq.Header.Set(responsesLiteHeader, "true")
			}
			// 映射到 gpt-5.5 的请求不走 Lite（本次上游合并引入）。OAuth 已在构造函数内、官方出站
			// 收口之前处理，这里只覆盖构造后才按客户端协商补 Lite 头的 SetupToken 账号。
			if err := applyMappedGPT55LiteCompatibility(upstreamReq, account, requestBody); err != nil {
				return nil, err
			}
		}
		return upstreamReq, nil
	}
	if account.Platform == PlatformGrok {
		upstreamModel := resolveGrokWSUpstreamModel(account, body, originalModel)
		body, err = patchGrokResponsesBody(body, upstreamModel)
		if err != nil {
			return nil, err
		}
		grokMixedCacheIntentBody := append([]byte(nil), body...)
		body, err = applyGrokResponsesCacheIdentity(body, grokIntentSourceBody, grokCacheIdentity, account.IsGrokOAuth())
		if err != nil {
			return nil, fmt.Errorf("apply grok prompt cache identity: %w", err)
		}
		body, err = applyGrokFreeRequestToolCacheRoute(c, body, grokMixedCacheIntentBody, account, grokCacheIdentity)
		if err != nil {
			return nil, fmt.Errorf("apply grok Free function-tool cache route: %w", err)
		}
	}
	actualModel := strings.TrimSpace(gjson.GetBytes(body, "model").String())
	if actualModel == "" {
		actualModel = canonicalOpenAIAccountSchedulingModel(account, originalModel)
	}
	SetOpsUpstreamModel(c, actualModel)

	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	if c != nil {
		c.Set("openai_passthrough", true)
		c.Set("openai_ws_http_bridge", true)
	}
	var officialForwardPlan *OpenAIForwardInvocationPlan
	var officialFallbackTarget officialegress.FallbackNode
	officialFallbackPending := false
	if managedOfficialBridge {
		holder := officialCodexBundleHolderForGin(c)
		officialForwardPlan = officialCodexResponseForwardPlanFromHolder(
			holder, officialegress.SinkCodexResponsesWS, account.ID,
		)
		if officialForwardPlan != nil {
			if officialForwardPlan.CurrentSinkID() == officialegress.SinkCodexResponsesWS {
				var found bool
				officialFallbackTarget, found = officialForwardPlan.FallbackNode(
					officialegress.SinkCodexResponsesWSHTTPBridge,
				)
				if !found {
					return nil, errors.New("WS HTTP bridge fallback 不在冻结 Bundle 中")
				}
				officialFallbackPending = true
			}
		} else {
			officialForwardPlan, err = s.officialCodexResponseForwardPlan(
				ctx, c, account,
				officialegress.SinkCodexResponsesWSHTTPBridge,
				"changeset3.responses.ws_http_bridge",
				"service.proxyOpenAIWSHTTPBridgeTurn",
				officialCodexIngressForwardAttemptBudget,
			)
			if err != nil {
				return nil, err
			}
		}
	}

	turnStart := time.Now()
	rejectedFieldRetryState := newOpenAIResponsesRejectedFieldRetryState(body)
	var resp *http.Response
	for {
		upstreamReq, buildErr := buildUpstreamRequest(body)
		if buildErr != nil {
			return nil, buildErr
		}
		switch {
		case account.Platform == PlatformGrok:
			resp, err = s.httpUpstream.Do(upstreamReq, proxyURL, account.ID, account.Concurrency)
		case officialForwardPlan != nil:
			if officialFallbackPending {
				resp, err = officialForwardPlan.TransitionHTTPFallback(
					upstreamReq.Context(), upstreamReq, officialFallbackTarget,
				)
				officialFallbackPending = false
			} else {
				resp, err = officialForwardPlan.ExecuteHTTPRequest(
					upstreamReq.Context(), upstreamReq, officialCodexEndpointResponsesHTTP,
				)
			}
		case account.IsOpenAIOAuth() && !officialEgressEnabled:
			resp, err = s.doOpenAIUpstream(upstreamReq, proxyURL, account)
		default:
			resp, err = doOpenAIHTTPUpstreamWithProfile(
				s.httpUpstream,
				upstreamReq,
				proxyURL,
				account,
				s.tlsFPProfileService,
				// WS-HTTP 桥接沿用 passthrough 语义：不做 API Key mimic，不套 mimic TLS 指纹。
				openAIAPIKeyCodexMimicProfile{},
			)
		}
		if err != nil {
			if IsRequestBodyStorageError(err) {
				return nil, err
			}
			if turn == 1 {
				return nil, s.handleOpenAIUpstreamTransportError(ctx, c, account, err, true)
			}
			safeErr := sanitizeUpstreamErrorMessage(err.Error())
			clientError := buildOpenAIWSHTTPBridgeErrorEvent(http.StatusBadGateway, "Upstream request failed")
			if writeErr := writeClientMessage(clientError); writeErr == nil {
				markOpenAIWSClientVisibleFailure(c, "error", clientError)
			}
			return nil, fmt.Errorf("upstream http bridge request failed: %s", safeErr)
		}
		if resp.StatusCode < 400 {
			break
		}
		if officialForwardBody.hasUnmaterializedWSHTTPBridgeBody(body) {
			body, err = officialForwardBody.materializeWSHTTPBridgeBody(body)
			if err != nil {
				_ = resp.Body.Close()
				return nil, fmt.Errorf("restore websocket http bridge retry body: %w", err)
			}
			officialForwardBody.bridge = nil
			rejectedFieldRetryState.remember(body)
		}

		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, openAIWSHTTPBridgeErrorBodyLimitBytes))
		_ = resp.Body.Close()
		markOpenAICyberPolicyEvent(c, respBody, resp.StatusCode, nil)
		if resp.StatusCode == http.StatusBadRequest &&
			extractUpstreamErrorCode(respBody) == openAIWSFallbackReasonInvalidEncryptedContent {
			s.markOpenAIWSInvalidEncryptedContentLineageFromPayload(
				c, body, "ingress_ws_http_bridge_invalid_encrypted_lineage_mark", account.ID, turn,
			)
		}
		retryBody, retryReason, changed, retryErr := normalizeOpenAIResponsesRejectedFieldRetryBody(resp.StatusCode, body, respBody)
		if retryErr != nil {
			return nil, fmt.Errorf("normalize websocket http bridge rejected field retry: %w", retryErr)
		}
		if changed && rejectedFieldRetryState.Allow(retryBody) {
			logOpenAIWSModeInfo(
				"ingress_ws_http_bridge_rejected_field_retry account_id=%d turn=%d reason=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(retryReason, openAIWSLogValueMaxLen),
			)
			body = retryBody
			payloadBytes = len(body)
			continue
		}

		upstreamMsg := sanitizeUpstreamErrorMessage(strings.TrimSpace(extractUpstreamErrorMessage(respBody)))
		if upstreamMsg == "" {
			upstreamMsg = http.StatusText(resp.StatusCode)
		}
		shouldFailover := s.shouldFailoverOpenAIUpstreamResponse(account, resp.StatusCode, upstreamMsg, respBody)
		if account.Platform == PlatformGrok {
			shouldFailover = s.shouldFailoverGrokUpstreamError(resp.StatusCode, respBody)
			s.handleGrokAccountUpstreamError(withGrokTeamRateLimitModel(ctx, resolveGrokWSUpstreamModel(account, body, originalModel)), account, resp.StatusCode, resp.Header, respBody)
			if shouldFailover && (turn == 1 || resp.StatusCode == http.StatusTooManyRequests) {
				return nil, newOpenAIUpstreamFailoverError(resp.StatusCode, resp.Header, respBody, upstreamMsg, false)
			}
		} else if shouldFailover && (turn == 1 || resp.StatusCode == http.StatusTooManyRequests) {
			return nil, s.handleFailoverErrorResponsePassthrough(ctx, resp, c, account, body, respBody)
		}
		if account.Platform != PlatformGrok && (shouldFailover || shouldCooldownOpenAITransientUpstreamError(resp.StatusCode, respBody)) {
			s.handleOpenAIAccountUpstreamError(ctx, account, resp.StatusCode, resp.Header, respBody, actualModel)
		}
		clientError := buildOpenAIWSHTTPBridgeErrorEvent(resp.StatusCode, upstreamMsg)
		if writeErr := writeClientMessage(clientError); writeErr == nil {
			markOpenAIWSClientVisibleFailure(c, "error", clientError)
		}
		return nil, fmt.Errorf("upstream http bridge error: status=%d message=%s", resp.StatusCode, upstreamMsg)
	}
	defer func() { _ = resp.Body.Close() }()
	stopCancelBody := context.AfterFunc(ctx, func() { _ = resp.Body.Close() })
	defer stopCancelBody()
	if account.Platform == PlatformGrok {
		s.updateGrokUsageFromResponse(withGrokTeamRateLimitModel(ctx, resolveGrokWSUpstreamModel(account, body, originalModel)), account, resp.Header, resp.StatusCode)
	}

	responseID := ""
	usage := OpenAIUsage{}
	imageCounter := newOpenAIImageOutputCounter()
	var firstTokenMs *int
	reqStream := openAIWSPayloadBoolFromRaw(body, "stream", true)
	if officialForwardBody.hasUnmaterializedWSHTTPBridgeBody(body) {
		// 原 WS 帧可显式写 stream=false，但 bridge 准备的 HTTP 语义始终是 true。
		reqStream = true
	}
	eventCount := 0
	tokenEventCount := 0
	terminalEventCount := 0
	replayCollector := &openAIWSToolCallReplayCollector{}
	firstEventType := ""
	lastEventType := ""
	upstreamTerminalEvent := ""
	sawDone := false
	wroteDownstream := false
	pendingClientMessages := make([][]byte, 0, 4)
	pendingClientMessageBytes := int64(0)
	capacityFailoverSuppressedLogged := false
	clientDisconnected := false
	officialOpenAIResponses := account != nil && account.Platform == PlatformOpenAI
	bareErrorPending := false
	var bareErrorPayload []byte
	bareErrorMessage := ""
	failureAccountSideEffectsApplied := false
	mappedModel := actualModel
	needModelReplace := false
	var mappedModelBytes []byte
	if originalModel != "" {
		needModelReplace = mappedModel != "" && mappedModel != originalModel
		if needModelReplace {
			mappedModelBytes = []byte(mappedModel)
		}
	}

	resultWithUsage := func() *OpenAIForwardResult {
		imageCount := imageCounter.Count()
		result := &OpenAIForwardResult{
			RequestID:                     responseID,
			Usage:                         usage,
			Model:                         originalModel,
			UpstreamModel:                 mappedModel,
			UpstreamResponseModel:         responseModelObserver.Model(),
			UpstreamResponseModelConflict: responseModelObserver.Conflict(),
			UpstreamResponseServiceTier:   responseModelObserver.ServiceTier(),
			ServiceTier:                   resolvedOpenAIUpstreamServiceTierFromObserver(responseModelObserver, extractOpenAIServiceTierFromBody(body)),
			ReasoningEffort:               ApplyThinkingEnabledFallback(extractOpenAIReasoningEffortFromBody(body, mappedModel, originalModel), body, mappedModel),
			RequestedReasoningEffort:      CanonicalRequestedReasoningEffort(body, originalModel, mappedModel),
			Stream:                        reqStream,
			OpenAIWSMode:                  true,
			UpstreamTerminalEvent:         upstreamTerminalEvent,
			ResponseHeaders:               cloneHeader(resp.Header),
			Duration:                      time.Since(turnStart),
			FirstTokenMs:                  firstTokenMs,
		}
		if replayInput := replayCollector.Items(); len(replayInput) > 0 {
			result.wsReplayInput = replayInput
			result.wsReplayInputExists = true
		}
		result.wsAccountFailoverReplayInput = replayCollector.AllItems()
		if imageCount > 0 {
			result.ImageCount = imageCount
			result.ImageSize = imageSizeTier
			result.ImageInputSize = imageInputSize
			result.ImageOutputSizes = imageCounter.Sizes()
			result.BillingModel = imageBillingModel
		}
		return result
	}

	maxLineSize := defaultMaxLineSize
	if s.cfg != nil && s.cfg.Gateway.MaxLineSize > 0 {
		maxLineSize = s.cfg.Gateway.MaxLineSize
	}
	if hasResponsesClientToolMapping(clientToolMapping) {
		resp.Body = newResponsesClientToolStreamBody(resp.Body, clientToolMapping, maxLineSize)
	}
	scanner := bufio.NewScanner(resp.Body)
	scanBuf := getSSEScannerBuf64K()
	scanner.Buffer(scanBuf[:0], maxLineSize)
	defer putSSEScannerBuf64K(scanBuf)

	pendingSSEEventType := ""
	finalizeBareError := func() error {
		if !bareErrorPending {
			return nil
		}
		if !failureAccountSideEffectsApplied {
			failureAccountSideEffectsApplied = s.handleOpenAIWSFailureAccountSideEffects(ctx, account, mappedModel, resp.Header, bareErrorPayload)
		}
		upstreamTerminalEvent = "response.failed"
		if clientDisconnected {
			return nil
		}
		clientMessage := buildOpenAIWSHTTPBridgeFailedEvent(responseID, originalModel, bareErrorPayload, bareErrorMessage)
		if rewritten, changed := sanitizeOpenAICapacityShedErrorCodeForClient(clientMessage); changed {
			clientMessage = rewritten
		}
		messages := append(pendingClientMessages, clientMessage)
		pendingClientMessages = nil
		pendingClientMessageBytes = 0
		for _, message := range messages {
			if err := writeClientMessage(message); err != nil {
				if isOpenAIWSClientDisconnectError(err) {
					clientDisconnected = true
					return nil
				}
				return fmt.Errorf("write synthesized websocket response.failed: %w", err)
			}
			wroteDownstream = true
		}
		markOpenAIWSClientVisibleFailure(c, "response.failed", clientMessage)
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if eventType, ok := extractOpenAISSEEventLine(line); ok {
			pendingSSEEventType = eventType
			continue
		}
		if strings.TrimSpace(line) == "" {
			pendingSSEEventType = ""
			continue
		}
		data, ok := extractOpenAISSEDataLine(line)
		if !ok {
			continue
		}
		trimmedData := strings.TrimSpace(data)
		if trimmedData == "" {
			continue
		}
		if trimmedData == "[DONE]" {
			sawDone = true
			continue
		}

		upstreamMessage := []byte(openAICompatPayloadWithEventType(trimmedData, pendingSSEEventType))
		if normalized, changed := normalizeCompletedImageGenerationStatus(upstreamMessage); changed {
			upstreamMessage = normalized
		}
		eventType, eventResponseID, _ := parseOpenAIWSEventEnvelope(upstreamMessage)
		responseModelObserver.ObserveOpenAI(upstreamMessage, eventType)
		if responseID == "" && eventResponseID != "" {
			responseID = eventResponseID
		}
		if eventType != "" {
			eventCount++
			if firstEventType == "" {
				firstEventType = eventType
			}
			lastEventType = eventType
		}
		if isOpenAIWSTokenEvent(eventType) {
			tokenEventCount++
			if firstTokenMs == nil {
				ms := int(time.Since(turnStart).Milliseconds())
				firstTokenMs = &ms
			}
		}
		if openAIWSMessageShouldParseUsage(eventType, upstreamMessage) {
			parseOpenAIWSResponseUsageFromCompletedEvent(upstreamMessage, &usage)
		}
		if eventType == "error" || eventType == "response.failed" {
			markOpenAICyberPolicyEvent(c, upstreamMessage, http.StatusOK, &usage)
		}
		imageCounter.AddSSEData(upstreamMessage)

		if needModelReplace && len(mappedModelBytes) > 0 && openAIWSEventMayContainModel(eventType) && strings.Contains(trimmedData, mappedModel) {
			upstreamMessage = replaceOpenAIWSMessageModel(upstreamMessage, mappedModel, originalModel)
		}
		if s.toolCorrector != nil && openAIWSEventMayContainToolCalls(eventType) && openAIWSMessageLikelyContainsToolCalls(upstreamMessage) {
			if corrected, changed := s.toolCorrector.CorrectToolCallsInSSEBytes(upstreamMessage); changed {
				upstreamMessage = corrected
			}
		}
		replayCollector.AddEvent(eventType, upstreamMessage)

		var upstreamEventErr error
		if officialOpenAIResponses && bareErrorPending && (eventType == "response.completed" || eventType == "response.done") {
			// Some upstreams emit a recoverable bare error before the authoritative
			// successful terminal. Do not replace that terminal with a synthetic
			// failure or retain side effects from the superseded error.
			bareErrorPending = false
			bareErrorPayload = nil
			bareErrorMessage = ""
		}
		suppressClientMessage := officialOpenAIResponses && bareErrorPending && eventType != "response.failed"
		if eventType == "error" || eventType == "response.failed" {
			errMessage := extractOpenAISSEErrorMessage(upstreamMessage)
			if errMessage == "" {
				errMessage = "upstream error event"
			}
			statusCode := openAIStreamFailureStatus(upstreamMessage, errMessage)
			shouldFailover := openAIStreamFailedEventShouldFailover(upstreamMessage, errMessage)
			if eventType == "error" {
				errCodeRaw, errTypeRaw, _ := parseOpenAIWSErrorEventFields(upstreamMessage)
				shouldFailover = openAIStreamErrorEventShouldFailover(upstreamMessage, errMessage)
				if account.Platform == PlatformGrok {
					statusCode = openAIWSErrorHTTPStatusFromRaw(errCodeRaw, errTypeRaw)
				}
				if reason, _ := classifyOpenAIWSErrorEventFromRaw(errCodeRaw, errTypeRaw, errMessage); reason == openAIWSFallbackReasonInvalidEncryptedContent {
					s.markOpenAIWSInvalidEncryptedContentLineageFromPayload(
						c, body, "ingress_ws_http_bridge_invalid_encrypted_lineage_mark", account.ID, turn,
					)
				}
			}
			requestScopedCapacity := isOpenAIUpstreamCapacityShedEvent(upstreamMessage)
			if account.Platform == PlatformGrok && eventType == "error" {
				// SSE error events do not carry an HTTP status. The local status
				// mapper therefore defaults unknown xAI codes (for example
				// new_sensitive) to 502; classify the body as a request-scoped
				// 403 before applying status-based failover or account state.
				if isGrokContentPolicyRejection(http.StatusForbidden, upstreamMessage) {
					shouldFailover = false
				} else {
					shouldFailover = s.shouldFailoverGrokUpstreamError(statusCode, upstreamMessage)
					s.handleGrokAccountUpstreamError(ctx, account, statusCode, resp.Header, upstreamMessage)
				}
			}
			// A disconnected client needs this attempt drained for usage, not replayed,
			// even when only non-semantic heartbeats were delivered.
			if !clientDisconnected && !wroteDownstream && shouldFailover && (turn == 1 || statusCode == http.StatusTooManyRequests) {
				if account.Platform == PlatformGrok {
					return nil, newOpenAIUpstreamFailoverError(statusCode, resp.Header, upstreamMessage, errMessage, false)
				}
				return nil, s.newOpenAIStreamFailoverErrorWithModel(c, account, true, resp.Header.Get("x-request-id"), upstreamMessage, errMessage, mappedModel, resp.Header)
			}
			if account.Platform != PlatformGrok && !failureAccountSideEffectsApplied {
				if eventType == "response.failed" || (!officialOpenAIResponses && shouldFailover && !requestScopedCapacity) {
					failureAccountSideEffectsApplied = s.handleOpenAIWSFailureAccountSideEffects(ctx, account, mappedModel, resp.Header, upstreamMessage)
				}
			}
			if wroteDownstream && requestScopedCapacity && !capacityFailoverSuppressedLogged {
				logOpenAICapacityFailoverSuppressed(ctx, account, "ws_http_bridge", resp.Header.Get("x-request-id"), eventType)
				capacityFailoverSuppressedLogged = true
			}
			if eventType == "error" && !officialOpenAIResponses {
				upstreamEventErr = errors.New(errMessage)
			} else if eventType == "error" {
				bareErrorPending = true
				bareErrorPayload = append(bareErrorPayload[:0], upstreamMessage...)
				bareErrorMessage = errMessage
				suppressClientMessage = true
			} else {
				bareErrorPending = false
			}
		}

		// 客户端写出副本改写容量降载码：Codex 对 error/response.failed 中的
		// server_is_overloaded / slow_down 判致命并终止会话，改写后走客户端内置
		// 重试。账号状态与终止事件判定（下方 handleOpenAIWSTerminalTransientFailure）
		// 仍使用未改写的 upstreamMessage。
		clientMessage := upstreamMessage
		if eventType == "error" || eventType == "response.failed" {
			if rewritten, changed := sanitizeOpenAICapacityShedErrorCodeForClient(clientMessage); changed {
				clientMessage = rewritten
			}
		}
		if !clientDisconnected && !suppressClientMessage {
			isKeepalive := eventType == "keepalive"
			stageBeforeSemanticOutput := turn == 1 && account.Platform == PlatformOpenAI && !wroteDownstream
			commitStagedMessages := !stageBeforeSemanticOutput ||
				openAIStreamDataStartsClientOutput(string(clientMessage), eventType) ||
				isOpenAIWSTerminalEvent(eventType)
			if stageBeforeSemanticOutput && !commitStagedMessages && !isKeepalive {
				if pendingClientMessageBytes+int64(len(clientMessage)) > openAIFirstOutputStageMaxBytes {
					return nil, s.newOpenAIStreamFailoverError(
						c,
						account,
						true,
						resp.Header.Get("x-request-id"),
						nil,
						"OpenAI WS HTTP bridge first-output staging limit exceeded",
						resp.Header,
					)
				}
				pendingClientMessages = append(pendingClientMessages, append([]byte(nil), clientMessage...))
				pendingClientMessageBytes += int64(len(clientMessage))
			} else {
				// Keep the client connection alive without committing this attempt
				// or exposing its staged lifecycle metadata.
				var messages [][]byte
				if !isKeepalive {
					messages = pendingClientMessages
					pendingClientMessages = nil
					pendingClientMessageBytes = 0
				}
				messages = append(messages, clientMessage)
				for _, message := range messages {
					if err := writeClientMessage(message); err != nil {
						if isOpenAIWSClientDisconnectError(err) {
							clientDisconnected = true
							closeStatus, closeReason := summarizeOpenAIWSReadCloseError(err)
							logOpenAIWSModeInfo(
								"ingress_ws_http_bridge_client_disconnected_drain account_id=%d turn=%d close_status=%s close_reason=%s",
								account.ID,
								turn,
								closeStatus,
								truncateOpenAIWSLogValue(closeReason, openAIWSHeaderValueMaxLen),
							)
							break
						}
						return nil, wrapOpenAIWSIngressTurnError(
							"write_client",
							fmt.Errorf("write client websocket event: %w", err),
							wroteDownstream,
						)
					}
					if !isKeepalive {
						wroteDownstream = true
					}
				}
			}
		}
		if !clientDisconnected && !suppressClientMessage {
			markOpenAIWSClientVisibleFailure(c, eventType, upstreamMessage)
		}

		if upstreamEventErr != nil {
			return resultWithUsage(), upstreamEventErr
		}
		if isOpenAIWSTerminalEvent(eventType) && !bareErrorPending {
			if eventType == "response.failed" {
				upstreamTerminalEvent = "response.failed"
			} else {
				upstreamTerminalEvent = s.handleOpenAIWSTerminalTransientFailure(ctx, account, mappedModel, resp.Header, upstreamMessage)
			}
			terminalEventCount++
			firstTokenMsValue := -1
			if firstTokenMs != nil {
				firstTokenMsValue = *firstTokenMs
			}
			logOpenAIWSModeInfo(
				"ingress_ws_http_bridge_turn_completed account_id=%d turn=%d response_id=%s payload_bytes=%d duration_ms=%d events=%d token_events=%d terminal_events=%d first_event=%s last_event=%s first_token_ms=%d client_disconnected=%v",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(responseID, openAIWSIDValueMaxLen),
				payloadBytes,
				time.Since(turnStart).Milliseconds(),
				eventCount,
				tokenEventCount,
				terminalEventCount,
				truncateOpenAIWSLogValue(firstEventType, openAIWSLogValueMaxLen),
				truncateOpenAIWSLogValue(lastEventType, openAIWSLogValueMaxLen),
				firstTokenMsValue,
				clientDisconnected,
			)
			return resultWithUsage(), nil
		}
	}
	if bareErrorPending {
		if finalizeErr := finalizeBareError(); finalizeErr != nil {
			return resultWithUsage(), finalizeErr
		}
		if scanErr := scanner.Err(); scanErr != nil {
			return resultWithUsage(), fmt.Errorf("read upstream http bridge stream after error event: %w", scanErr)
		}
		return resultWithUsage(), errors.New(bareErrorMessage)
	}
	if err := scanner.Err(); err != nil {
		streamErr := fmt.Errorf("read upstream http bridge stream: %w", err)
		if turn == 1 && !clientDisconnected && !wroteDownstream {
			return nil, s.handleOpenAIUpstreamTransportError(ctx, c, account, streamErr, true)
		}
		return resultWithUsage(), streamErr
	}
	terminalErr := errors.New("upstream http bridge stream ended before terminal event")
	if sawDone {
		terminalErr = errors.New("upstream http bridge stream sent [DONE] before terminal event")
	}
	if turn == 1 && !clientDisconnected && !wroteDownstream {
		return nil, s.handleOpenAIUpstreamTransportError(ctx, c, account, terminalErr, true)
	}
	return resultWithUsage(), terminalErr
}

// newOpenAIOfficialEgressHTTPBridgeContext 把当前 WS 帧的身份字段映射成等价的
// 官方 HTTP 请求头。WS 握手 Header 是连接级 prewarm 快照，不能覆盖每轮帧内
// 更新的 turn metadata；这里只创建请求副本，不修改原始入站连接状态。
func newOpenAIOfficialEgressHTTPBridgeContext(
	c *gin.Context,
	contract *officialOpenAIHTTPBodyContract,
) (*gin.Context, error) {
	if contract == nil {
		return c, nil
	}
	if c == nil || c.Request == nil {
		return nil, errors.New("OpenAI official egress HTTP bridge requires ingress request")
	}
	metadata := contract.clientMetadata
	bridgeContext := c.Copy()
	bridgeContext.Request = c.Request.Clone(c.Request.Context())
	bridgeContext.Request.Header = c.Request.Header.Clone()
	bridgeContext.Request.Header.Set("session-id", officialOpenAIString(metadata, "session_id"))
	bridgeContext.Request.Header.Set("thread-id", officialOpenAIString(metadata, "thread_id"))
	bridgeContext.Request.Header.Set("x-client-request-id", contract.promptCacheKey)
	bridgeContext.Request.Header.Set("x-codex-window-id", officialOpenAIString(metadata, "x-codex-window-id"))
	bridgeContext.Request.Header.Set("x-codex-turn-metadata", officialOpenAIString(metadata, "x-codex-turn-metadata"))
	if affinity := openAIWSHTTPBridgeSessionAffinity(c); affinity != "" {
		// 该头只存在于内部请求副本，用于 Persona 身份派生。普通透传白名单不放行
		// X-Session-Affinity，因此它不会进入最终官方 wire。
		bridgeContext.Request.Header.Set(openCodeSessionAffinityHeader, affinity)
	}
	return bridgeContext, nil
}

func resolveGrokWSCacheIdentity(c *gin.Context, account *Account, seedPayload, currentPayload []byte, originalModel string) (string, error) {
	body, err := prepareOpenAIWSHTTPBridgeBody(account, seedPayload)
	if err != nil {
		return "", err
	}
	upstreamModel := resolveGrokWSUpstreamModel(account, currentPayload, originalModel)
	body, err = patchGrokResponsesBody(body, upstreamModel)
	if err != nil {
		return "", err
	}
	return resolveGrokCacheIdentity(c, body, "", upstreamModel), nil
}

func resolveGrokWSUpstreamModel(account *Account, body []byte, originalModel string) string {
	upstreamModel := strings.TrimSpace(gjson.GetBytes(body, "model").String())
	originalModel = strings.TrimSpace(originalModel)
	// Shared ingress has already applied channel and account mappings when the
	// body model differs from the client-facing model. Only resolve from the
	// original model when the body still carries that original value.
	if account != nil && originalModel != "" && (upstreamModel == "" || upstreamModel == originalModel) {
		if mappedModel := normalizeOpenAIModelForUpstream(account, account.GetMappedModel(originalModel)); mappedModel != "" {
			upstreamModel = mappedModel
		}
	}
	if upstreamModel == "" {
		upstreamModel = grokDefaultResponsesModel
	}
	return upstreamModel
}
