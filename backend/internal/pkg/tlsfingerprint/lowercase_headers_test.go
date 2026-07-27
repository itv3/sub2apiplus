package tlsfingerprint

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

type capturingRoundTripper struct {
	captured http.Header
}

func (c *capturingRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	c.captured = req.Header.Clone()
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       http.NoBody,
		Header:     http.Header{},
		Request:    req,
	}, nil
}

// 官方 Rust hyper 在 h1 上发全小写 header 名，Go 默认发 canonical 形态。
// 该 RoundTripper 负责把 wire 形态收口到小写。
func TestLowercaseHeaderRoundTripperLowercasesHeaderNames(t *testing.T) {
	base := &capturingRoundTripper{}
	rt := NewLowercaseHeaderRoundTripper(base)

	req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	req.Header.Set("Session-Id", "s1")
	req.Header.Set("Originator", "codex_exec")
	req.Header.Set("X-Codex-Turn-Metadata", "{}")
	req.Header.Set("Content-Type", "application/json")

	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}

	for _, name := range []string{"session-id", "originator", "x-codex-turn-metadata", "content-type"} {
		if _, ok := base.captured[name]; !ok {
			t.Errorf("出站缺少小写 header %q，实际 keys=%v", name, keysOf(base.captured))
		}
	}
	for _, name := range []string{"Session-Id", "Originator", "X-Codex-Turn-Metadata", "Content-Type"} {
		if _, ok := base.captured[name]; ok {
			t.Errorf("出站仍保留 canonical header %q", name)
		}
	}
}

// User-Agent 必须只出现一次且为小写：canonical key 置空串用于抑制 Go 的硬编码输出，
// 若被误当成普通 header 处理就会同时发出两行。
func TestLowercaseHeaderRoundTripperKeepsSingleUserAgent(t *testing.T) {
	base := &capturingRoundTripper{}
	rt := NewLowercaseHeaderRoundTripper(base)

	req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	req.Header.Set("User-Agent", "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown")

	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}

	// 这里刻意绕过 Header.Get：本用例校验的正是 map key 的原始大小写。
	lower := lookupRawHeader(base.captured, "user-agent")
	if len(lower) != 1 || lower[0] != "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown" {
		t.Errorf("小写 user-agent 取值不正确: %v", lower)
	}
	canonical := lookupRawHeader(base.captured, "User-Agent")
	if len(canonical) != 1 || canonical[0] != "" {
		t.Errorf("canonical User-Agent 必须置为空串以抑制 Go 默认 UA，实际: %v", canonical)
	}
}

// 未携带 User-Agent 时不应凭空造出空值头。
func TestLowercaseHeaderRoundTripperWithoutUserAgent(t *testing.T) {
	base := &capturingRoundTripper{}
	rt := NewLowercaseHeaderRoundTripper(base)

	req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	req.Header.Set("Session-Id", "s1")

	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}
	if lookupRawHeader(base.captured, "User-Agent") != nil {
		t.Error("请求本无 User-Agent，不应生成空值头")
	}
	if lookupRawHeader(base.captured, "user-agent") != nil {
		t.Error("请求本无 User-Agent，不应生成小写头")
	}
}

func keysOf(h http.Header) []string {
	out := make([]string, 0, len(h))
	for k := range h {
		out = append(out, k)
	}
	return out
}

// lookupRawHeader 按字面 key 查表，不做 canonical 归一。
// http.Header 本质是 map[string][]string，这里正是要验证 key 的原始大小写。
func lookupRawHeader(h http.Header, key string) []string {
	for name, values := range h {
		if name == key {
			return values
		}
	}
	return nil
}
