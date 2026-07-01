package service

import (
	"strings"
	"testing"
)

func TestCollectKeeperOpenAIResponsesStream(t *testing.T) {
	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"response.output_text.delta","delta":"可以"}`,
		`data: {"type":"response.output_text.delta","delta":"优化"}`,
		`data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":8,"total_tokens":20,"input_tokens_details":{"cached_tokens":3}}}}`,
		``,
	}, "\n"))

	reply, usage, err := collectKeeperOpenAIResponsesStream(body)
	if err != nil {
		t.Fatalf("collectKeeperOpenAIResponsesStream returned error: %v", err)
	}
	if reply != "可以优化" {
		t.Fatalf("reply = %q, want %q", reply, "可以优化")
	}
	if usage.InputTokens != 12 || usage.OutputTokens != 8 || usage.TotalTokens != 20 || usage.CacheReadTokens != 3 {
		t.Fatalf("usage = %+v", usage)
	}
}

func TestCollectKeeperClaudeStream(t *testing.T) {
	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"message_start","message":{"usage":{"input_tokens":15,"cache_creation_input_tokens":2,"cache_read_input_tokens":4}}}`,
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"继续"}}`,
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"分析"}}`,
		`data: {"type":"message_delta","usage":{"output_tokens":9}}`,
		`data: {"type":"message_stop"}`,
		``,
	}, "\n"))

	reply, usage, err := collectKeeperClaudeStream(body)
	if err != nil {
		t.Fatalf("collectKeeperClaudeStream returned error: %v", err)
	}
	if reply != "继续分析" {
		t.Fatalf("reply = %q, want %q", reply, "继续分析")
	}
	if usage.InputTokens != 15 || usage.OutputTokens != 9 || usage.CacheCreationTokens != 2 || usage.CacheReadTokens != 4 || usage.TotalTokens != 30 {
		t.Fatalf("usage = %+v", usage)
	}
}
