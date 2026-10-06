package officialegress

// segmentedJSONScanner 沿只读片段检查对象、数组和标点。完整标量直接复用连续字节
// 扫描器，长字符串仍引用原帧；字符串或数字 token 自身被切开时返回 false，交回
// 原字节解析路径复核。成员数组处于第 2 层，深度限制与 skipJSONValue 保持一致。
type segmentedJSONScanner struct {
	parts  [][]byte
	part   int
	offset int
}

func (s *segmentedJSONScanner) peek() byte {
	for s.part < len(s.parts) && s.offset == len(s.parts[s.part]) {
		s.part++
		s.offset = 0
	}
	if s.part == len(s.parts) {
		return 0
	}
	return s.parts[s.part][s.offset]
}

func (s *segmentedJSONScanner) finished() bool {
	s.peek()
	return s.part == len(s.parts)
}

func (s *segmentedJSONScanner) take(value byte) bool {
	if s.peek() != value {
		return false
	}
	s.offset++
	return true
}

func (s *segmentedJSONScanner) skipSpace() {
	for isJSONSpace(s.peek()) {
		s.offset++
	}
}

func (s *segmentedJSONScanner) skipString() bool {
	if s.peek() != '"' {
		return false
	}
	end, _, err := skipJSONString(s.parts[s.part], s.offset)
	if err != nil {
		return false
	}
	s.offset = end
	return true
}

func (s *segmentedJSONScanner) skipValue(depth int) bool {
	first := s.peek()
	if depth > jsonScanMaxDepth || first == 0 {
		return false
	}
	switch first {
	case '{':
		return s.skipObject(depth)
	case '[':
		return s.skipArray(depth)
	case '"':
		return s.skipString()
	default:
		// 数字、布尔值和 null 必须在当前片段完整结束；剩余分隔符由父层验证。
		end, err := skipJSONValue(s.parts[s.part], s.offset, depth)
		if err != nil {
			return false
		}
		s.offset = end
		return true
	}
}

func (s *segmentedJSONScanner) skipObject(depth int) bool {
	s.offset++
	s.skipSpace()
	if s.take('}') {
		return true
	}
	for {
		if !s.skipString() {
			return false
		}
		s.skipSpace()
		if !s.take(':') {
			return false
		}
		s.skipSpace()
		if !s.skipValue(depth + 1) {
			return false
		}
		s.skipSpace()
		if s.take('}') {
			return true
		}
		if !s.take(',') {
			return false
		}
		s.skipSpace()
	}
}

func (s *segmentedJSONScanner) skipArray(depth int) bool {
	s.offset++
	s.skipSpace()
	if s.take(']') {
		return true
	}
	for {
		if !s.skipValue(depth + 1) {
			return false
		}
		s.skipSpace()
		if s.take(']') {
			return true
		}
		if !s.take(',') {
			return false
		}
		s.skipSpace()
	}
}
