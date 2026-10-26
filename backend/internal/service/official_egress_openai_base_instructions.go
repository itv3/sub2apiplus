package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync/atomic"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
)

// 基础指令载体：画像 BaseInstructions 节。
//
// 较新版本的官方请求不再发送顶层 instructions：基础指令改为 input 最前（additional_tools 之后）
// 的一条 developer 消息，消息带稳定的 id 和 internal_chat_message_metadata_passthrough 标记。
// 本文件只做“读节、渲染官方形态、识别、去重、安装”，由 HTTP 定型阶段按节门控调用：画像没有这个节
// （或节没有列出该端点）时调用方保持旧逻辑，共享代码不比较版本号。
//
// 官方形态，键序固定：
//
//	{"type":"message","id":"<前缀>_<uuid>","role":"developer",
//	 "content":[{"type":"input_text","text":"<正文>"}],
//	 "internal_chat_message_metadata_passthrough":{"content_item_kinds":["<标记>"]}}
//
// id = 前缀 + "_" + UUIDv5(UUIDv5(NAMESPACE_OID, thread_id), 正文的 UTF-8 字节)：同一线程、同一正文
// 得到同一 id，线程变了 id 跟着变。map[string]any 的编码按键字典序，无法表达这个键序，所以消息整体
// 预编码成 json.RawMessage 放进 input；保序编码器对预编码项原样追加，且不让它参与数组配对
// （见 isOfficialJSONPreEncodedItem）。

// officialCodexInboundPrefixScanLimit 是契约捕获阶段在入站 input 里寻找带标记 developer 消息的范围：
// 官方 prefix 的位置固定，最多是 additional_tools 加基础指令消息，多看三项足够，避免为此遍历大 input。
const officialCodexInboundPrefixScanLimit = 3

// officialCodexBaseInstructionsCarrier 返回 mode 对应画像的 BaseInstructions 节，并要求该端点在节的
// EndpointIDs 里；画像没有该节或没有列出该端点时返回 (nil, false)，调用方保持旧逻辑。
// 返回的节是画像内的共享只读数据，调用方不得修改。
func officialCodexBaseInstructionsCarrier(mode, endpointID string) (*profilecontract.BaseInstructionsSection, bool) {
	section := officialCodexOptionalSectionsForMode(mode).BaseInstructions
	if section == nil || !slices.Contains(section.EndpointIDs, endpointID) {
		return nil, false
	}
	return section, true
}

// officialCodexBaseInstructionsID 按官方规则生成消息 id。
func officialCodexBaseInstructionsID(section *profilecontract.BaseInstructionsSection, threadID, text string) string {
	threadNamespace := uuid.NewSHA1(uuid.NameSpaceOID, []byte(threadID))
	return section.IDPrefix + "_" + uuid.NewSHA1(threadNamespace, []byte(text)).String()
}

// appendOfficialCodexJSONString 以官方客户端序列化库的转义规则追加 JSON 字符串：只转义引号、反斜杠和
// 控制字符（\b \f \n \r \t，其余 \u00XX 小写十六进制），其余字符原样输出，包括 < > &、U+2028、U+2029
// 和非 BMP 字符。较早版本的 encoding/json 会把 U+2028、U+2029 转义成六个字符的 u 转义序列，所以不能直接用它。
// 非法 UTF-8 逐字节替换为 U+FFFD，与 encoding/json 一致。
func appendOfficialCodexJSONString(dst []byte, value string) []byte {
	const hexDigits = "0123456789abcdef"
	dst = append(dst, '"')
	for i := 0; i < len(value); {
		c := value[i]
		if c < utf8.RuneSelf {
			switch {
			case c == '"':
				dst = append(dst, '\\', '"')
			case c == '\\':
				dst = append(dst, '\\', '\\')
			case c == '\b':
				dst = append(dst, '\\', 'b')
			case c == '\f':
				dst = append(dst, '\\', 'f')
			case c == '\n':
				dst = append(dst, '\\', 'n')
			case c == '\r':
				dst = append(dst, '\\', 'r')
			case c == '\t':
				dst = append(dst, '\\', 't')
			case c < 0x20:
				dst = append(dst, '\\', 'u', '0', '0', hexDigits[c>>4], hexDigits[c&0xf])
			default:
				dst = append(dst, c)
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(value[i:])
		if r == utf8.RuneError && size == 1 {
			dst = append(dst, "\xef\xbf\xbd"...)
			i++
			continue
		}
		dst = append(dst, value[i:i+size]...)
		i += size
	}
	return append(dst, '"')
}

// renderOfficialCodexBaseInstructionsItem 生成官方形态的基础指令消息（预编码 JSON）。
// 线程 ID 缺失时失败关闭：没有线程就算不出稳定 id，也不应发出缺 id 的官方形态消息。
func renderOfficialCodexBaseInstructionsItem(
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
) (json.RawMessage, error) {
	if strings.TrimSpace(threadID) == "" {
		return nil, errors.New("OpenAI official egress base instructions require a thread id")
	}
	return renderOfficialCodexBaseInstructionsItemWithID(
		section, officialCodexBaseInstructionsID(section, threadID, text), text,
	), nil
}

// renderOfficialCodexBaseInstructionsItemWithID 按给定的 id 生成官方形态的基础指令消息（预编码 JSON）。
// id 的来源由调用方决定：旧方案按线程与正文算 UUIDv5，增量窗口方案用稳定的 UUIDv7（见
// official_egress_openai_incremental_prefix.go）。passthrough 只带 content_item_kinds，这是旧方案的官方形态。
func renderOfficialCodexBaseInstructionsItemWithID(
	section *profilecontract.BaseInstructionsSection,
	id string,
	text string,
) json.RawMessage {
	passthrough := append([]byte(`{"content_item_kinds":[`), appendOfficialCodexJSONString(nil, section.ContentItemKind)...)
	passthrough = append(passthrough, `]}`...)
	return renderOfficialCodexBaseInstructionsItemWithPassthrough(id, text, passthrough)
}

// renderOfficialCodexBaseInstructionsItemWithPassthrough 生成官方形态的基础指令消息，passthrough 是调用方已经编码好的
// internal_chat_message_metadata_passthrough 对象原文。键序固定：type、id、role、content、passthrough。
func renderOfficialCodexBaseInstructionsItemWithPassthrough(id, text string, passthrough []byte) json.RawMessage {
	out := make([]byte, 0, len(text)+len(passthrough)+160)
	out = append(out, `{"type":"message","id":`...)
	out = appendOfficialCodexJSONString(out, id)
	out = append(out, `,"role":"developer","content":[{"type":"input_text","text":`...)
	out = appendOfficialCodexJSONString(out, text)
	out = append(out, `}],"internal_chat_message_metadata_passthrough":`...)
	out = append(out, passthrough...)
	out = append(out, '}')
	return out
}

// officialCodexInputItemString 读取 input 项的字符串成员；项可以是已解码的对象，也可以是预编码字节。
func officialCodexInputItemString(item any, key string) string {
	switch typed := item.(type) {
	case map[string]any:
		return officialOpenAIString(typed, key)
	case json.RawMessage:
		return strings.TrimSpace(gjson.GetBytes(typed, key).String())
	}
	return ""
}

func isOfficialCodexAdditionalToolsItem(item any) bool {
	return officialCodexInputItemString(item, "type") == "additional_tools"
}

// isOfficialCodexDeveloperOrSystemMessage 判断 input 项是否是 developer 或 system 消息。
func isOfficialCodexDeveloperOrSystemMessage(item any) bool {
	switch officialCodexInputItemString(item, "role") {
	case "developer", "system":
		itemType := officialCodexInputItemString(item, "type")
		return itemType == "" || itemType == "message"
	}
	return false
}

// isOfficialCodexBaseInstructionsItem 按 internal_chat_message_metadata_passthrough.content_item_kinds
// 识别带节标记的基础指令消息（已解码对象和预编码字节两种形态）。
func isOfficialCodexBaseInstructionsItem(section *profilecontract.BaseInstructionsSection, item any) bool {
	switch typed := item.(type) {
	case map[string]any:
		if officialOpenAIString(typed, "role") != "developer" {
			return false
		}
		passthrough, ok := typed["internal_chat_message_metadata_passthrough"].(map[string]any)
		if !ok {
			return false
		}
		kinds, ok := passthrough["content_item_kinds"].([]any)
		if !ok {
			return false
		}
		for _, kind := range kinds {
			if value, isString := kind.(string); isString && value == section.ContentItemKind {
				return true
			}
		}
	case json.RawMessage:
		parsed := gjson.ParseBytes(typed)
		if !parsed.IsObject() || strings.TrimSpace(parsed.Get("role").String()) != "developer" {
			return false
		}
		kinds := parsed.Get("internal_chat_message_metadata_passthrough.content_item_kinds")
		if !kinds.IsArray() {
			return false
		}
		found := false
		kinds.ForEach(func(_, kind gjson.Result) bool {
			if kind.Type == gjson.String && kind.String() == section.ContentItemKind {
				found = true
				return false
			}
			return true
		})
		return found
	}
	return false
}

// isOfficialCodexInstructionsCopy 判断 input 项是否是与指令同文本的 developer 或 system 消息：
// 入站兼容层把 system 消息提升成顶层 instructions 时会在 input 里留一份 role 改成 developer 的副本，
// 官方形态里这段文本只出现一次，副本必须去掉。比较时两边都去掉首尾空白。
func isOfficialCodexInstructionsCopy(item any, trimmedText string) bool {
	message, ok := item.(map[string]any)
	if !ok || !isOfficialCodexDeveloperOrSystemMessage(message) {
		return false
	}
	return officialOpenAIHTTPMessageContentText(message["content"]) == trimmedText
}

// officialCodexLeadingSystemPrefixEnd 返回 input 前导“系统区”的结束下标：从头开始连续的
// additional_tools、developer、system 项，遇到其他任何项（用户消息、工具调用等）即结束。
// 基础指令的旧副本只可能出现在这一区段，历史对话部分不会被碰。
func officialCodexLeadingSystemPrefixEnd(input []any) int {
	for index, item := range input {
		if isOfficialCodexAdditionalToolsItem(item) || isOfficialCodexDeveloperOrSystemMessage(item) {
			continue
		}
		return index
	}
	return len(input)
}

// officialCodexBaseInstructionsInputItems 取出 payload 的 input 数组；缺省视为空数组，字符串输入按
// Responses 约定转成单条 user 消息，其他类型失败关闭。
func officialCodexBaseInstructionsInputItems(payload map[string]any) ([]any, error) {
	switch value := payload["input"].(type) {
	case nil:
		return []any{}, nil
	case string:
		return []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": []any{map[string]any{"type": "input_text", "text": value}},
		}}, nil
	case []any:
		return value, nil
	default:
		return nil, errors.New("OpenAI official egress input must be a string or array")
	}
}

// rebuildOfficialCodexBaseInstructionsInput 重建 input：先去掉前导系统区里的旧副本（带节标记的基础指令消息，
// 以及与指令同文本的 developer／system 消息），rendered 非空时再把它插到前导 additional_tools 之后。
// 总是返回新切片，原切片不被改动。rendered 为空时只去重、不插入（WS 增量帧上正文与连接上已发送的相同时使用）。
func rebuildOfficialCodexBaseInstructionsInput(
	input []any,
	section *profilecontract.BaseInstructionsSection,
	trimmedText string,
	rendered json.RawMessage,
) []any {
	prefixEnd := officialCodexLeadingSystemPrefixEnd(input)
	rebuilt := make([]any, 0, len(input)+1)
	inserted := rendered == nil
	for index, item := range input {
		isTools := isOfficialCodexAdditionalToolsItem(item)
		if index < prefixEnd && !isTools &&
			(isOfficialCodexBaseInstructionsItem(section, item) || isOfficialCodexInstructionsCopy(item, trimmedText)) {
			continue
		}
		if !inserted && !isTools {
			rebuilt = append(rebuilt, rendered)
			inserted = true
		}
		rebuilt = append(rebuilt, item)
	}
	if !inserted {
		rebuilt = append(rebuilt, rendered)
	}
	return rebuilt
}

// installOfficialCodexBaseInstructions 把官方形态的基础指令消息安装到 payload["input"]：
// 先去掉前导系统区里的旧副本，再插到前导 additional_tools 之后（见 rebuildOfficialCodexBaseInstructionsInput）。
// 空白指令不生成消息，与官方“文本为空不生成”一致。payload["input"] 总是换成新切片，原切片不被改动。
func installOfficialCodexBaseInstructions(
	payload map[string]any,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	instructions string,
) error {
	trimmed := strings.TrimSpace(instructions)
	if trimmed == "" {
		return nil
	}
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, threadID, instructions)
	if err != nil {
		return err
	}
	input, err := officialCodexBaseInstructionsInputItems(payload)
	if err != nil {
		return err
	}
	payload["input"] = rebuildOfficialCodexBaseInstructionsInput(input, section, trimmed, rendered)
	return nil
}

// officialCodexBaseInstructionsSource 决定基础指令正文的来源，优先级：
//  1. payload 里现存的顶层 instructions，且入口契约拥有它（入口显式提供，或兼容层生成）；
//  2. 契约拥有、但已被中间转换从 payload 消费掉的 instructions；
//  3. 兼容层生成、同样被消费掉的 instructions；
//  4. 入站 input 前缀里带节标记的 developer 消息（官方较新版本客户端的请求没有顶层字段，
//     标记在入站规范化时已被剥掉，契约捕获阶段先记下了正文）。
//
// 都没有时返回 sourced=false：此时 payload 里残留的顶层 instructions 没有来源，由调用方删除。
func officialCodexBaseInstructionsSource(
	payload map[string]any,
	contract *officialOpenAIHTTPBodyContract,
	section *profilecontract.BaseInstructionsSection,
) (text string, sourced bool, err error) {
	errNotString := errors.New("OpenAI official egress third-party instructions must be a string")
	if current, present := payload["instructions"]; present {
		if contract.instructionsPresent || contract.generatedInstructionsPresent {
			value, isString := current.(string)
			if !isString {
				return "", false, errNotString
			}
			return value, true, nil
		}
	} else if contract.instructionsPresent {
		value, isString := contract.instructions.(string)
		if !isString {
			return "", false, errNotString
		}
		return value, true, nil
	} else if contract.generatedInstructionsPresent {
		value, isString := contract.generatedInstructions.(string)
		if !isString {
			return "", false, errNotString
		}
		return value, true, nil
	}
	if inbound, found := contract.inboundTaggedMessageText(section.ContentItemKind); found {
		return inbound, true, nil
	}
	return "", false, nil
}

// applyOfficialCodexBaseInstructionsCarrier 是 HTTP 定型阶段在画像声明 BaseInstructions 节时对
// instructions 的处理：Lite 与非 Lite 同形，顶层 instructions 一律删除，来源明确的正文以官方形态
// 安装进 input，没有来源的残留直接丢弃（避免把网关内部实现泄漏到官方请求）。返回 payload 是否被改动。
func applyOfficialCodexBaseInstructionsCarrier(
	payload map[string]any,
	contract *officialOpenAIHTTPBodyContract,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
) (bool, error) {
	text, sourced, err := officialCodexBaseInstructionsSource(payload, contract, section)
	if err != nil {
		return false, err
	}
	_, hadTopLevel := payload["instructions"]
	delete(payload, "instructions")
	if !sourced || strings.TrimSpace(text) == "" {
		return hadTopLevel, nil
	}
	if err := installOfficialCodexBaseInstructions(payload, section, threadID, text); err != nil {
		return false, err
	}
	return true, nil
}

// officialCodexTaggedDeveloperMessage 从已解码的 input 项里取出带内容标记的 developer 消息：
// 返回标记集合与正文（content 里各文本片段按序以换行连接，不去空白，id 要用原文计算）。
func officialCodexTaggedDeveloperMessage(item map[string]any) (officialOpenAIHTTPTaggedMessage, bool) {
	if officialOpenAIString(item, "role") != "developer" {
		return officialOpenAIHTTPTaggedMessage{}, false
	}
	passthrough, ok := item["internal_chat_message_metadata_passthrough"].(map[string]any)
	if !ok {
		return officialOpenAIHTTPTaggedMessage{}, false
	}
	rawKinds, ok := passthrough["content_item_kinds"].([]any)
	if !ok {
		return officialOpenAIHTTPTaggedMessage{}, false
	}
	kinds := make([]string, 0, len(rawKinds))
	for _, rawKind := range rawKinds {
		if kind, isString := rawKind.(string); isString && kind != "" {
			kinds = append(kinds, kind)
		}
	}
	if len(kinds) == 0 {
		return officialOpenAIHTTPTaggedMessage{}, false
	}
	return officialOpenAIHTTPTaggedMessage{kinds: kinds, text: officialCodexMessageTextVerbatim(item["content"])}, true
}

// officialCodexMessageTextVerbatim 取消息 content 的文本：字符串直接返回，数组里各文本片段按序以换行
// 连接。与 officialOpenAIHTTPMessageContentText 的区别是不去首尾空白。
func officialCodexMessageTextVerbatim(content any) string {
	switch value := content.(type) {
	case string:
		return value
	case []any:
		parts := make([]string, 0, len(value))
		for _, rawPart := range value {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			text, _ := part["text"].(string)
			if text == "" {
				text, _ = part["input_text"].(string)
			}
			if text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	}
	return ""
}

// collectOfficialOpenAIInboundTaggedMessagesFromIndex 只读扫描入站 input 的前几项，记录带内容标记的
// developer 消息。它不改动 input，也不依赖画像：是否使用这些记录由定型阶段按画像节决定。
// 没有 passthrough 成员的项（绝大多数请求）不解码，零分配。
func collectOfficialOpenAIInboundTaggedMessagesFromIndex(
	index *officialJSONRawIndex,
	input int32,
) []officialOpenAIHTTPTaggedMessage {
	if input < 0 || index.nodes[input].kind != officialJSONRawKindArray {
		return nil
	}
	items := index.arrayItems(input)
	if len(items) > officialCodexInboundPrefixScanLimit {
		items = items[:officialCodexInboundPrefixScanLimit]
	}
	var messages []officialOpenAIHTTPTaggedMessage
	for _, item := range items {
		if index.nodes[item].kind != officialJSONRawKindObject ||
			index.memberNode(item, "internal_chat_message_metadata_passthrough") < 0 {
			continue
		}
		decoded, ok := index.decodeValue(item).(map[string]any)
		if !ok {
			continue
		}
		if message, found := officialCodexTaggedDeveloperMessage(decoded); found {
			// 同时保留原文：画像声明增量窗口方案时，官方形态的基础指令消息要原样放行（见
			// officialCodexInboundBaseInstructionsRawLimit）；其他方案不使用。超限的原文不复制。
			if raw := index.raw(item); len(raw) <= officialCodexInboundBaseInstructionsRawLimit {
				message.raw = bytes.Clone(raw)
			}
			messages = append(messages, message)
		}
	}
	return messages
}

// inboundTaggedMessageText 返回入站 input 前缀里带指定内容标记的 developer 消息正文。
func (c *officialOpenAIHTTPBodyContract) inboundTaggedMessageText(kind string) (string, bool) {
	for _, message := range c.inboundTaggedMessages {
		if slices.Contains(message.kinds, kind) {
			return message.text, true
		}
	}
	return "", false
}

// WebSocket 帧上的基础指令。
//
// WS 与 HTTP 共用同一种官方形态消息，差别在于 WS 是连接上的续链协议：官方客户端在一条链的第一帧（预热帧或首个业务帧）
// 带上基础指令消息，之后用 previous_response_id 续链的增量帧不再重复。第三方客户端则习惯在每一帧都带上顶层
// instructions（公开 Responses API 的 instructions 不随续链继承）。所以 WS 载体按“链”决策：
//   - 新链（帧里没有 previous_response_id）：把正文安装成官方形态消息，并在连接状态里记下正文摘要；既没有顶层
//     instructions 也没有可用的带标记消息时，回退到模型默认 Codex 指令（与 HTTP 缺 instructions 时注入的默认指令是同一个
//     来源和函数），按有来源处理；
//   - 续链帧带了正文：与连接上已发送的摘要相同就丢弃（含 input 里的旧副本），避免历史里堆出重复的基础指令；
//     不同则作为新的基础指令消息追加并更新摘要，同时提示一次；
//   - 续链帧没带正文：不动。
//
// 基础指令消息是预编码字节，装进 input 之后帧里就有非对象项，所以安装必须排在逐项 turn_id 补写之后；
// 补写函数也要豁免已经带标记的基础指令消息（见 finalizeOfficialOpenAIWSInputTurnMetadata）。

// officialCodexBaseInstructionsDigest 是基础指令正文的 SHA-256。
type officialCodexBaseInstructionsDigest = [sha256.Size]byte

type officialCodexWSBaseInstructionsAction int

const (
	// officialCodexWSBaseInstructionsNone：本帧没有可用的基础指令正文，input 不动。
	officialCodexWSBaseInstructionsNone officialCodexWSBaseInstructionsAction = iota
	// officialCodexWSBaseInstructionsInstall：把正文安装成官方形态消息（新链、首次、或正文变了）。
	officialCodexWSBaseInstructionsInstall
	// officialCodexWSBaseInstructionsDrop：续链帧重复了连接上已发送的正文，只去掉旧副本、不再安装。
	officialCodexWSBaseInstructionsDrop
)

// planBaseInstructions 按本帧是否续链、是否带正文以及正文摘要，决定怎么处理并更新连接状态。
// replaced 表示这是续链帧上换了一段不同的正文（连接上之前已经发送过另一段）。
func (s *officialOpenAIWSDerivedState) planBaseInstructions(
	chained bool,
	sourced bool,
	digest officialCodexBaseInstructionsDigest,
) (action officialCodexWSBaseInstructionsAction, replaced bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !sourced {
		if !chained {
			// 新链没有基础指令：此后这条链上就没有，之后续链帧再带正文按首次对待。
			s.baseInstructionsSent = false
		}
		return officialCodexWSBaseInstructionsNone, false
	}
	if chained && s.baseInstructionsSent && s.baseInstructionsDigest == digest {
		return officialCodexWSBaseInstructionsDrop, false
	}
	replaced = chained && s.baseInstructionsSent
	s.baseInstructionsSent = true
	s.baseInstructionsDigest = digest
	return officialCodexWSBaseInstructionsInstall, replaced
}

// officialCodexTaggedBaseInstructionsMessage 在 input 前缀里找带节标记的 developer 消息，返回其正文（不去空白）。
// 只看前几项：官方基础指令消息的位置固定在 additional_tools 之后，不必为此遍历整份历史。
func officialCodexTaggedBaseInstructionsMessage(
	rawInput any,
	section *profilecontract.BaseInstructionsSection,
) (string, bool) {
	input, ok := rawInput.([]any)
	if !ok {
		return "", false
	}
	if len(input) > officialCodexInboundPrefixScanLimit {
		input = input[:officialCodexInboundPrefixScanLimit]
	}
	for _, rawItem := range input {
		item, isObject := rawItem.(map[string]any)
		if !isObject {
			continue
		}
		if message, found := officialCodexTaggedDeveloperMessage(item); found &&
			slices.Contains(message.kinds, section.ContentItemKind) {
			return message.text, true
		}
	}
	return "", false
}

// officialCodexLeadingInstructionsCopyExists 判断 input 前导系统区里是否还留着与指令同文本的 developer／system 消息。
func officialCodexLeadingInstructionsCopyExists(rawInput any, trimmedText string) bool {
	input, ok := rawInput.([]any)
	if !ok || trimmedText == "" {
		return false
	}
	for _, item := range input[:officialCodexLeadingSystemPrefixEnd(input)] {
		if isOfficialCodexInstructionsCopy(item, trimmedText) {
			return true
		}
	}
	return false
}

// officialCodexWSBaseInstructionsSource 决定 WS 帧上基础指令的正文，优先级：
//  1. 候选帧里现存的顶层 instructions（第三方客户端的写法）；非字符串失败关闭，空白视同没有正文。
//     顶层字段存在就由它说了算，不再看 input，与 HTTP 载体一致；
//  2. 候选帧 input 前缀里仍带节标记的 developer 消息；
//  3. 原始帧 input 前缀里带节标记的 developer 消息，且候选帧前导系统区里还留着同文本的副本。官方较新版本客户端的帧
//     没有顶层字段；OAuth 兼容层会把 input 项的 internal_chat_message_metadata_passthrough 剥掉，标记随之丢失，
//     只能在原始帧上认。要求候选帧里还有副本，是为了不把兼容层有意删除的消息复活。
//
// 都没有时返回 sourced=false。original 可以为空。
func officialCodexWSBaseInstructionsSource(
	original map[string]any,
	candidate map[string]any,
	section *profilecontract.BaseInstructionsSection,
) (text string, sourced bool, err error) {
	if current, present := candidate["instructions"]; present {
		value, isString := current.(string)
		if !isString {
			return "", false, errors.New("OpenAI official egress third-party instructions must be a string")
		}
		return value, strings.TrimSpace(value) != "", nil
	}
	if message, found := officialCodexTaggedBaseInstructionsMessage(candidate["input"], section); found {
		return message, true, nil
	}
	if original != nil {
		if message, found := officialCodexTaggedBaseInstructionsMessage(original["input"], section); found &&
			officialCodexLeadingInstructionsCopyExists(candidate["input"], strings.TrimSpace(message)) {
			return message, true, nil
		}
	}
	return "", false, nil
}

// applyOfficialCodexWSBaseInstructions 是 WS 派生帧在画像声明 BaseInstructions 节（且列出 responses_ws）时对基础指令
// 的处理：text／sourced 来自 officialCodexWSBaseInstructionsSource，顶层 instructions 已由调用方取走并删除。
// 必须在逐项 turn_id 补写之后调用（安装的是预编码字节）。
//
// 新链帧（此时 previous_response_id 已是全量历史回退之后的最终值）既没有顶层 instructions（空白视同没有）也没有可用的
// 带标记消息时，回退到模型默认 Codex 指令，与 HTTP 路径对缺 instructions 的 OAuth 请求注入默认指令是同一个来源和同一个
// 函数（defaultCodexSynthInstructions），再按有来源的新链安装成官方形态的基础指令消息。网关的目的是让上游始终看到官方
// 形态：官方形态的新链首帧带基础指令，预热帧也只承载这段前缀；不回退的话，第三方客户端只发普通 developer 消息的首帧
// 会因为预热帧没有可承载的前缀而构造失败。
// 有来源时行为不变；续链帧没有来源时也不回退（链上的基础指令已经在这条链的第一帧里了）。画像没有声明载体节时本函数
// 不会被调用，旧画像的帧与旧形态逐字节相同。
func applyOfficialCodexWSBaseInstructions(
	payload map[string]any,
	state *officialOpenAIWSDerivedState,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
	sourced bool,
) error {
	trimmed := strings.TrimSpace(text)
	sourced = sourced && trimmed != ""
	chained := strings.TrimSpace(officialOpenAIString(payload, "previous_response_id")) != ""
	if !sourced && !chained {
		// 模型取帧内最终发往上游的 model，与 HTTP 注入默认指令取最终上游模型一致；空模型和未知模型也交给同一个函数处理，
		// 回退结果与 HTTP 完全相同。
		text = defaultCodexSynthInstructions(officialOpenAIString(payload, "model"))
		trimmed = strings.TrimSpace(text)
		sourced = trimmed != ""
	}
	var digest officialCodexBaseInstructionsDigest
	if sourced {
		digest = sha256.Sum256([]byte(text))
	}
	action, replaced := state.planBaseInstructions(chained, sourced, digest)
	switch action {
	case officialCodexWSBaseInstructionsInstall:
		if replaced {
			warnOfficialCodexBaseInstructionsChanged()
		}
		return installOfficialCodexBaseInstructions(payload, section, threadID, text)
	case officialCodexWSBaseInstructionsDrop:
		input, err := officialCodexBaseInstructionsInputItems(payload)
		if err != nil {
			return err
		}
		payload["input"] = rebuildOfficialCodexBaseInstructionsInput(input, section, trimmed, nil)
	}
	return nil
}

// officialCodexBaseInstructionsChangedWarned 让“连接上基础指令正文变了”的提示每个进程只打一次：
// 正文变化有合法来源（用户切换了模式），不该刷屏，但值得让运维知道网关确实追加了新消息。
var officialCodexBaseInstructionsChangedWarned atomic.Bool

func warnOfficialCodexBaseInstructionsChanged() {
	if !officialCodexBaseInstructionsChangedWarned.CompareAndSwap(false, true) {
		return
	}
	logger.LegacyPrintf(
		"service.official_egress_openai",
		"[Codex] WebSocket 连接上的续链帧带了与已发送基础指令不同的 instructions，已作为新的基础指令消息追加（同一进程只提示一次）",
	)
}

// officialCodexWSBaseInstructionsCarrier 返回 mode 对应画像在 WS 端点上的基础指令载体节；未启用时为 nil。
func officialCodexWSBaseInstructionsCarrier(mode string) *profilecontract.BaseInstructionsSection {
	section, _ := officialCodexBaseInstructionsCarrier(mode, officialCodexEndpointResponsesWS)
	return section
}
