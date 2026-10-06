package service

import (
	"encoding/json"
	"strings"
	"unsafe"

	"github.com/tidwall/gjson"
)

// officialOpenAIUserAnchor 是一条用户消息的身份锚点：它在 input 中的下标与规范化
// 文本的摘要。规范化规则与 officialOpenAIHTTPMessageContentText 完全一致（字符串
// 内容去首尾空白；数组内容取每个 part 去空白后的 text，缺失时取 input_text，非空
// part 用换行连接），差别只在于文本分块写入 SHA-256，从不拼成完整字符串，也不会
// 以全文形态进入任何缓存键。
type officialOpenAIUserAnchor struct {
	index  int
	digest officialContentDigest
}

// officialOpenAIUserAnchors 汇总首条与末条有效用户消息。firstFound/lastFound 为
// false 表示不存在对应锚点。对字符串形态的 input 保留旧种子语义：末条锚点始终存在
// （下标 0），首条锚点只在文本非空时存在。
type officialOpenAIUserAnchors struct {
	first      officialOpenAIUserAnchor
	firstFound bool
	last       officialOpenAIUserAnchor
	lastFound  bool
}

func (a *officialOpenAIUserAnchors) observe(index int, digest officialContentDigest) {
	anchor := officialOpenAIUserAnchor{index: index, digest: digest}
	if !a.firstFound {
		a.first = anchor
		a.firstFound = true
	}
	a.last = anchor
	a.lastFound = true
}

// observeStringInput 处理 input 为纯字符串的请求：旧种子把它视为下标 0 的用户消息，
// 末条锚点即使文本为空也存在，首条锚点只在文本非空时存在。
func (a *officialOpenAIUserAnchors) observeStringInput(text string) {
	var digester officialOpenAIMessageTextDigester
	digester.addPart(text)
	digest, found := digester.result()
	a.last = officialOpenAIUserAnchor{index: 0, digest: digest}
	a.lastFound = true
	if found {
		a.first = a.last
		a.firstFound = true
	}
}

// WriteUserAnchor 把用户锚点写入种子：存在时写入下标与文本摘要，不存在时写入空
// 字段，分别对应旧种子中的 "下标:文本" 与空串两种形态。
func (s *officialUUIDV7Seed) WriteUserAnchor(
	anchor officialOpenAIUserAnchor,
	found bool,
) *officialUUIDV7Seed {
	if !found {
		return s.WriteString("")
	}
	return s.WriteString("user").WriteInt(anchor.index).WriteDigest(anchor.digest)
}

// WriteFirstUserDigest 写入首条用户消息摘要作为会话兜底锚点。
func (s *officialUUIDV7Seed) WriteFirstUserDigest(
	anchor officialOpenAIUserAnchor,
	found bool,
) *officialUUIDV7Seed {
	if !found {
		return s.WriteString("")
	}
	return s.WriteString("first_user").WriteDigest(anchor.digest)
}

// officialOpenAIMessageTextDigester 按 officialOpenAIHTTPMessageContentText 的规则
// 规范化消息文本并分块写入 SHA-256：每个 part 去首尾空白，空 part 跳过，非空 part
// 之间以换行连接。由于每个 part 已去空白，连接结果的首尾也不会是空白，因此与旧实现
// “连接后再整体 TrimSpace”的结果逐字节一致。
type officialOpenAIMessageTextDigester struct {
	digester officialContentDigester
	parts    int
}

func (d *officialOpenAIMessageTextDigester) addPart(text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	if d.parts > 0 {
		d.digester.WriteString("\n")
	}
	d.digester.WriteString(text)
	d.parts++
}

// result 返回摘要与“是否存在非空文本”。空文本仍返回确定的摘要，供字符串 input
// 的末条锚点使用。
func (d *officialOpenAIMessageTextDigester) result() (officialContentDigest, bool) {
	return d.digester.Sum(), d.parts > 0
}

// digestOfficialOpenAIMessageContent 对结构化 content 计算规范化文本摘要。
func digestOfficialOpenAIMessageContent(content any) (officialContentDigest, bool) {
	var digester officialOpenAIMessageTextDigester
	switch value := content.(type) {
	case string:
		digester.addPart(value)
	case []any:
		for _, rawPart := range value {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			text := officialOpenAIString(part, "text")
			if text == "" {
				text = officialOpenAIString(part, "input_text")
			}
			digester.addPart(text)
		}
	case json.RawMessage:
		return digestOfficialOpenAIMessageContentJSON(gjson.ParseBytes(value))
	}
	return digester.result()
}

// digestOfficialOpenAIMessageContentJSON 对原始 JSON 形态的 content 计算与
// digestOfficialOpenAIMessageContent 完全相同的摘要，只做只读扫描。
func digestOfficialOpenAIMessageContentJSON(content gjson.Result) (officialContentDigest, bool) {
	var digester officialOpenAIMessageTextDigester
	switch {
	case content.Type == gjson.String:
		digester.addPart(content.Str)
	case content.IsArray():
		content.ForEach(func(_, part gjson.Result) bool {
			if !part.IsObject() {
				return true
			}
			text := officialOpenAIJSONString(part, "text")
			if text == "" {
				text = officialOpenAIJSONString(part, "input_text")
			}
			digester.addPart(text)
			return true
		})
	}
	return digester.result()
}

// officialOpenAIJSONString 与 officialOpenAIString 语义一致：只接受字符串值并去
// 首尾空白，其他类型视为空。
func officialOpenAIJSONString(value gjson.Result, key string) string {
	field := value.Get(key)
	if field.Type != gjson.String {
		return ""
	}
	return strings.TrimSpace(field.Str)
}

func isOfficialOpenAIJSONUserMessage(item gjson.Result) bool {
	return item.IsObject() &&
		officialOpenAIJSONString(item, "type") == "message" &&
		officialOpenAIJSONString(item, "role") == "user"
}

// officialOpenAIUserAnchorsFromBody 只读扫描原始 JSON 正文提取用户锚点。它不解码
// 整个 body，也不复制正文：先记录每条候选用户消息的零拷贝视图，再只对首条与末条
// 有效消息做文本反转义与摘要，因此对 18 MB 级长上下文的额外分配只与首末两条用户
// 消息的长度相关。
func officialOpenAIUserAnchorsFromBody(body []byte) officialOpenAIUserAnchors {
	var anchors officialOpenAIUserAnchors
	if len(body) == 0 {
		return anchors
	}
	// body 在本函数内只读，零拷贝转换避免为大正文再复制一份。
	text := unsafe.String(unsafe.SliceData(body), len(body))
	input := gjson.Get(text, "input")
	switch {
	case input.Type == gjson.String:
		anchors.observeStringInput(input.Str)
	case input.IsArray():
		type candidate struct {
			index int
			raw   string
		}
		var candidates []candidate
		index := 0
		// 候选只保存原始只读区间与序号，避免为每条历史消息保存完整 Result。
		// 仍只解析首末有效文本；逐条计算会复制所有带转义的历史消息。
		input.ForEach(func(_, item gjson.Result) bool {
			if isOfficialOpenAIJSONUserMessage(item) {
				candidates = append(candidates, candidate{index: index, raw: item.Raw})
			}
			index++
			return true
		})
		for _, item := range candidates {
			digest, found := digestOfficialOpenAIMessageContentJSON(gjson.Get(item.raw, "content"))
			if found {
				anchors.first = officialOpenAIUserAnchor{index: item.index, digest: digest}
				anchors.firstFound = true
				break
			}
		}
		if !anchors.firstFound {
			return anchors
		}
		for position := len(candidates) - 1; position >= 0; position-- {
			item := candidates[position]
			digest, found := digestOfficialOpenAIMessageContentJSON(gjson.Get(item.raw, "content"))
			if found {
				anchors.last = officialOpenAIUserAnchor{index: item.index, digest: digest}
				anchors.lastFound = true
				break
			}
		}
	}
	return anchors
}

// officialOpenAIUserAnchorsFromInput 直接遍历已解码的 input 提取用户锚点，与
// officialOpenAIUserAnchorsFromBody 对同一内容得到相同结果。已有结构化 payload 时
// 必须走这里，不得再先编码整帧、随后又解码来提取锚点。
func officialOpenAIUserAnchorsFromInput(input any) officialOpenAIUserAnchors {
	var anchors officialOpenAIUserAnchors
	switch value := input.(type) {
	case string:
		anchors.observeStringInput(value)
	case []any:
		for index, rawItem := range value {
			switch item := rawItem.(type) {
			case map[string]any:
				if officialOpenAIString(item, "type") != "message" ||
					officialOpenAIString(item, "role") != "user" {
					continue
				}
				if digest, found := digestOfficialOpenAIMessageContent(item["content"]); found {
					anchors.observe(index, digest)
				}
			case json.RawMessage:
				parsed := gjson.ParseBytes(item)
				if !isOfficialOpenAIJSONUserMessage(parsed) {
					continue
				}
				if digest, found := digestOfficialOpenAIMessageContentJSON(parsed.Get("content")); found {
					anchors.observe(index, digest)
				}
			}
		}
	case json.RawMessage:
		return officialOpenAIUserAnchorsFromBody([]byte(`{"input":` + string(value) + `}`))
	}
	return anchors
}
