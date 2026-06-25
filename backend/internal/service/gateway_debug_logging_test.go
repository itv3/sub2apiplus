package service

import (
	"strings"
	"testing"
)

func TestRedactGatewayDebugBodyForLog(t *testing.T) {
	body := []byte(`{
		"prompt_cache_key":"pcache-123",
		"session_id":"sess-123",
		"conversation_id":"conv-123",
		"instructions":"You are Kilo",
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from workspace"}]}],
		"delta":"stream secret",
		"tools":[{"type":"function","name":"shell","description":"run shell command","parameters":{"type":"object","properties":{"cmd":{"type":"string","description":"pwd"}}}}]
	}`)

	out := redactGatewayDebugBodyForLog(body)

	if strings.Contains(out, "pcache-123") || strings.Contains(out, "sess-123") || strings.Contains(out, "hello from workspace") || strings.Contains(out, "stream secret") {
		t.Fatalf("expected gateway debug body to redact sensitive payloads, got %s", out)
	}
	if !strings.Contains(out, `"prompt_cache_key":"***"`) {
		t.Fatalf("expected prompt_cache_key to be redacted, got %s", out)
	}
	if !strings.Contains(out, `"input":"***"`) || !strings.Contains(out, `"delta":"***"`) {
		t.Fatalf("expected input and delta to be redacted, got %s", out)
	}
}
