package service

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"strings"
)

const (
	maxOpenAIConcatenatedJSONDocuments = 16
	maxOpenAIConcatenatedJSONBytes     = 16 * 1024 * 1024
)

// splitOpenAIConcatenatedJSONDocuments recognizes the narrow corruption shape
// produced when multiple complete Responses events arrive in one transport
// message. Other malformed payloads are left untouched for normal error paths.
func splitOpenAIConcatenatedJSONDocuments(payload []byte) ([][]byte, bool) {
	payload = bytes.TrimSpace(payload)
	if len(payload) == 0 || len(payload) > maxOpenAIConcatenatedJSONBytes || json.Valid(payload) {
		return nil, false
	}

	decoder := json.NewDecoder(bytes.NewReader(payload))
	documents := make([][]byte, 0, 2)
	for {
		var raw json.RawMessage
		err := decoder.Decode(&raw)
		if err != nil {
			if err == io.EOF && len(documents) > 1 {
				return documents, true
			}
			return nil, false
		}
		raw = bytes.TrimSpace(raw)
		var envelope struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &envelope); err != nil {
			return nil, false
		}
		eventType := strings.TrimSpace(envelope.Type)
		if eventType == "" || strings.ContainsAny(eventType, "\r\n") {
			return nil, false
		}
		if len(documents) == maxOpenAIConcatenatedJSONDocuments {
			return nil, false
		}
		documents = append(documents, raw)
	}
}

type openAISSEJSONDocumentScanner struct {
	scanner *bufio.Scanner
	pending []string
	current string
}

func newOpenAISSEJSONDocumentScanner(scanner *bufio.Scanner) *openAISSEJSONDocumentScanner {
	return &openAISSEJSONDocumentScanner{scanner: scanner}
}

func (s *openAISSEJSONDocumentScanner) Scan() bool {
	if len(s.pending) > 0 {
		s.current = s.pending[0]
		s.pending = s.pending[1:]
		return true
	}
	if s.scanner == nil || !s.scanner.Scan() {
		return false
	}

	line := s.scanner.Text()
	data, ok := extractOpenAISSEDataLine(line)
	if !ok {
		s.current = line
		return true
	}
	if len(data) > maxOpenAIConcatenatedJSONBytes {
		s.current = line
		return true
	}
	documents, repaired := splitOpenAIConcatenatedJSONDocuments([]byte(data))
	if repaired {
		expanded := make([]string, 0, len(documents)*3)
		for i, document := range documents {
			if i > 0 {
				var envelope struct {
					Type string `json:"type"`
				}
				_ = json.Unmarshal(document, &envelope)
				expanded = append(expanded, "event: "+strings.TrimSpace(envelope.Type))
			}
			expanded = append(expanded, "data: "+string(document), "")
		}
		s.current = expanded[0]
		s.pending = expanded[1:]
		return true
	}
	if compacted, consumed, prettyRepaired := s.repairPrettyPrintedJSONDataLine(data); prettyRepaired {
		s.current = compacted
		return true
	} else if len(consumed) > 0 {
		s.pending = append(s.pending, consumed...)
	}
	s.current = line
	return true
}

// repairPrettyPrintedJSONDataLine 修复上游把 pretty-printed JSON 错误地拆成
// “首行 data:、后续裸 JSON 行”的事件。合法 SSE 的多条 data: 行也一并支持；
// 只有在拼接结果成为完整 JSON 文档后才压成单行，无法确认时原样回放。
func (s *openAISSEJSONDocumentScanner) repairPrettyPrintedJSONDataLine(firstData string) (string, []string, bool) {
	trimmed := strings.TrimSpace(firstData)
	if len(trimmed) == 0 || json.Valid([]byte(trimmed)) || (trimmed[0] != '{' && trimmed[0] != '[') {
		return "", nil, false
	}

	var payload bytes.Buffer
	_, _ = payload.WriteString(firstData)
	consumed := make([]string, 0, 8)
	for payload.Len() <= maxOpenAIConcatenatedJSONBytes && s.scanner != nil && s.scanner.Scan() {
		line := s.scanner.Text()
		consumed = append(consumed, line)
		if line == "" || openAISSELineStartsNewField(line) {
			return "", consumed, false
		}
		fragment := line
		if data, ok := extractOpenAISSEDataLine(line); ok {
			fragment = data
		}
		_ = payload.WriteByte('\n')
		_, _ = payload.WriteString(fragment)
		if payload.Len() > maxOpenAIConcatenatedJSONBytes {
			return "", consumed, false
		}
		candidate := bytes.TrimSpace(payload.Bytes())
		if !json.Valid(candidate) {
			continue
		}
		var compacted bytes.Buffer
		if err := json.Compact(&compacted, candidate); err != nil {
			return "", consumed, false
		}
		return "data: " + compacted.String(), nil, true
	}
	return "", consumed, false
}

func openAISSELineStartsNewField(line string) bool {
	trimmed := strings.TrimLeft(line, " \t")
	return strings.HasPrefix(trimmed, "event:") ||
		strings.HasPrefix(trimmed, "id:") ||
		strings.HasPrefix(trimmed, "retry:") ||
		strings.HasPrefix(trimmed, ":")
}

func (s *openAISSEJSONDocumentScanner) Text() string {
	return s.current
}

func (s *openAISSEJSONDocumentScanner) Err() error {
	if s.scanner == nil {
		return nil
	}
	return s.scanner.Err()
}
