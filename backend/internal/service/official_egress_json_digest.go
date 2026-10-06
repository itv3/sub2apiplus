package service

import (
	"encoding/json"
	"sort"
	"unicode/utf8"
)

// writeOfficialJSONDigestValue 将已解码的 JSON 树逐项写入摘要，避免 json.Encoder
// 为一个长字符串或完整历史分配连续输出缓冲。键序、数字文本和转义与
// Encoder.SetEscapeHTML(false) 一致。非 JSON 类型及过深结构交还原编码器，保留
// 自定义 Marshaler、非法数值、循环引用等兼容与报错行为；失败的部分摘要会被丢弃。
func writeOfficialJSONDigestValue(digester *officialContentDigester, value any, depth int) bool {
	if depth > 256 {
		return false
	}
	switch value := value.(type) {
	case nil:
		digester.WriteString("null")
	case bool:
		if value {
			digester.WriteString("true")
		} else {
			digester.WriteString("false")
		}
	case string:
		return writeOfficialJSONDigestString(digester, value)
	case json.Number:
		encoded, err := json.Marshal(value)
		if err != nil {
			return false
		}
		_, _ = digester.Write(encoded)
	case []any:
		if value == nil {
			digester.WriteString("null")
			return true
		}
		digester.WriteString("[")
		for i, element := range value {
			if i > 0 {
				digester.WriteString(",")
			}
			if !writeOfficialJSONDigestValue(digester, element, depth+1) {
				return false
			}
		}
		digester.WriteString("]")
	case map[string]any:
		if value == nil {
			digester.WriteString("null")
			return true
		}
		keys := make([]string, 0, len(value))
		for key := range value {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		digester.WriteString("{")
		for i, key := range keys {
			if i > 0 {
				digester.WriteString(",")
			}
			if !writeOfficialJSONDigestString(digester, key) {
				return false
			}
			digester.WriteString(":")
			if !writeOfficialJSONDigestValue(digester, value[key], depth+1) {
				return false
			}
		}
		digester.WriteString("}")
	default:
		return false
	}
	return true
}

// writeOfficialJSONDigestString 直接引用连续文本片段；仅转义处写入固定的小片段。
// 不进行 HTML 转义，但与 encoding/json 一样转义 U+2028/U+2029；非法 UTF-8 的
// 替代编码随标准库实现而异，遇到时让整个值回到原编码器。
func writeOfficialJSONDigestString(digester *officialContentDigester, value string) bool {
	const hex = "0123456789abcdef"
	digester.WriteString("\"")
	start := 0
	for i := 0; i < len(value); {
		c := value[i]
		if c >= 0x20 && c < utf8.RuneSelf && c != '\\' && c != '"' {
			i++
			continue
		}
		escaped, size := "", 1
		switch c {
		case '\\':
			escaped = `\\`
		case '"':
			escaped = `\"`
		case '\n':
			escaped = `\n`
		case '\r':
			escaped = `\r`
		case '\t':
			escaped = `\t`
		case '\b':
			escaped = `\b`
		case '\f':
			escaped = `\f`
		default:
			if c < 0x20 {
				var sequence = [6]byte{'\\', 'u', '0', '0', hex[c>>4], hex[c&15]}
				digester.WriteString(value[start:i])
				_, _ = digester.Write(sequence[:])
				i++
				start = i
				continue
			}
			r, width := utf8.DecodeRuneInString(value[i:])
			size = width
			switch {
			case r == utf8.RuneError && width == 1:
				return false
			case r == '\u2028':
				escaped = `\u2028`
			case r == '\u2029':
				escaped = `\u2029`
			default:
				i += size
				continue
			}
		}
		digester.WriteString(value[start:i])
		digester.WriteString(escaped)
		i += size
		start = i
	}
	digester.WriteString(value[start:])
	digester.WriteString("\"")
	return true
}
