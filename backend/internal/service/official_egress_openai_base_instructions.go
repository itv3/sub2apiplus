package service

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
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
	out := make([]byte, 0, len(text)+256)
	out = append(out, `{"type":"message","id":`...)
	out = appendOfficialCodexJSONString(out, officialCodexBaseInstructionsID(section, threadID, text))
	out = append(out, `,"role":"developer","content":[{"type":"input_text","text":`...)
	out = appendOfficialCodexJSONString(out, text)
	out = append(out, `}],"internal_chat_message_metadata_passthrough":{"content_item_kinds":[`...)
	out = appendOfficialCodexJSONString(out, section.ContentItemKind)
	out = append(out, `]}}`...)
	return out, nil
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

// installOfficialCodexBaseInstructions 把官方形态的基础指令消息安装到 payload["input"]：
// 先去掉前导系统区里的旧副本（带节标记的基础指令消息，以及与指令同文本的 developer／system 消息），
// 再插到前导 additional_tools 之后。空白指令不生成消息，与官方“文本为空不生成”一致。
// payload["input"] 总是换成新切片，原切片不被改动。
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
	prefixEnd := officialCodexLeadingSystemPrefixEnd(input)
	rebuilt := make([]any, 0, len(input)+1)
	inserted := false
	for index, item := range input {
		isTools := isOfficialCodexAdditionalToolsItem(item)
		if index < prefixEnd && !isTools &&
			(isOfficialCodexBaseInstructionsItem(section, item) || isOfficialCodexInstructionsCopy(item, trimmed)) {
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
	payload["input"] = rebuilt
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
