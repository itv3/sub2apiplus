package officialegress

import (
	"bytes"
	"encoding/json"
	"io"
	"sync"
)

// orderedJSONArraySegments 只保存经扫描验证的完整数组片段。片段可以位于完整元素之间，
// 也可以位于对象键、标点及完整标量之间；不会在字符串、转义或数值中间断开。
// source 描述和字节均只读，按需生成的连续视图只供真正需要完整字段的兼容路径使用。
type orderedJSONArraySegments struct {
	parts  [][]byte
	length int
	once   sync.Once
	bytes  json.RawMessage
}

func newOrderedJSONArraySegments(parts [][]byte) (*orderedJSONArraySegments, bool) {
	if length, ok := validateWholeJSONArrayElementSegments(parts); ok {
		return &orderedJSONArraySegments{parts: parts, length: length}, true
	}
	// WS→HTTP 的规范化输出可能把对象结构与原帧中的长字符串分开交接；只沿片段
	// 校验语法，不合并大 input。不能证明 token 完整性的片段仍交回既有字节解析路径。
	scanner := segmentedJSONScanner{parts: parts}
	if scanner.peek() != '[' || !scanner.skipValue(2) || !scanner.finished() {
		return nil, false
	}
	length := 0
	for _, part := range parts {
		length += len(part)
	}
	return &orderedJSONArraySegments{parts: parts, length: length}, true
}

// validateWholeJSONArrayElementSegments 保留原有完整数组项快路径。
func validateWholeJSONArrayElementSegments(parts [][]byte) (int, bool) {
	if len(parts) < 2 || !bytes.Equal(parts[0], []byte{'['}) || !bytes.Equal(parts[len(parts)-1], []byte{']'}) {
		return 0, false
	}
	if len(parts) != 2 && len(parts)%2 != 1 {
		return 0, false
	}
	length := 2
	for i := 1; i < len(parts)-1; i++ {
		part := parts[i]
		if i%2 == 0 {
			if !bytes.Equal(part, []byte{','}) {
				return 0, false
			}
		} else {
			if len(part) == 0 || isJSONSpace(part[0]) {
				return 0, false
			}
			// 顶层对象为第 1 层，成员数组为第 2 层，数组元素从第 3 层开始。
			end, err := skipJSONValue(part, 0, 3)
			if err != nil || end != len(part) {
				return 0, false
			}
		}
		length += len(part)
	}
	return length, true
}

func (s *orderedJSONArraySegments) materialize() json.RawMessage {
	s.once.Do(func() {
		s.bytes = make(json.RawMessage, 0, s.length)
		for _, part := range s.parts {
			s.bytes = append(s.bytes, part...)
		}
	})
	return s.bytes
}

func (f orderedJSONField) rawBytes() json.RawMessage {
	if f.segments != nil {
		return f.segments.materialize()
	}
	return f.value
}

func (f orderedJSONField) encodedLength() int {
	if f.segments != nil {
		return f.segments.length
	}
	return len(f.value)
}

func (f orderedJSONField) appendTo(dst []byte) []byte {
	if f.segments != nil {
		for _, part := range f.segments.parts {
			dst = append(dst, part...)
		}
		return dst
	}
	return append(dst, f.value...)
}

func (f orderedJSONField) writeTo(writer io.Writer) error {
	if f.segments != nil {
		for _, part := range f.segments.parts {
			if err := writeJSONDocumentPart(writer, part); err != nil {
				return err
			}
		}
		return nil
	}
	return writeJSONDocumentPart(writer, f.value)
}

// contains 仅用于寻找完整 JSON 字符串标记。两种分段扫描路径都要求字符串 token
// 位于单个片段，因此完整标记不会跨越边界；需要清理时才物化完整字段。
func (f orderedJSONField) contains(marker []byte) bool {
	if f.segments != nil {
		for _, part := range f.segments.parts {
			if bytes.Contains(part, marker) {
				return true
			}
		}
		return false
	}
	return bytes.Contains(f.value, marker)
}

// policyValue 只供现有 OmitWhen 规则检查空字符串/null。已验证的分段数组必然不是二者，
// 用同为数组的最短值即可运行相同规则，避免为判断 required 而复制大字段。
func (f orderedJSONField) policyValue() json.RawMessage {
	if f.segments != nil {
		return json.RawMessage("[]")
	}
	return f.value
}
