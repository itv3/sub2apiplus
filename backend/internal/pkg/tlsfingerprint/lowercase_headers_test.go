package tlsfingerprint

import (
	"bufio"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
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
	rt := NewLowercaseHeaderRoundTripper(base, nil)

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
	rt := NewLowercaseHeaderRoundTripper(base, nil)

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

// 未携带 User-Agent 时必须用 canonical 空值抑制 Go 默认 UA；该空值不会写到
// wire，只是 net/http 的传输层哨兵。
func TestLowercaseHeaderRoundTripperWithoutUserAgentSuppressesGoDefault(t *testing.T) {
	base := &capturingRoundTripper{}
	rt := NewLowercaseHeaderRoundTripper(base, nil)

	req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	req.Header.Set("Session-Id", "s1")

	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}
	canonical := lookupRawHeader(base.captured, "User-Agent")
	if len(canonical) != 1 || canonical[0] != "" {
		t.Errorf("缺少抑制 Go 默认 UA 的 canonical 空值哨兵: %v", canonical)
	}
	if lookupRawHeader(base.captured, "user-agent") != nil {
		t.Error("请求本无 User-Agent，不应生成小写头")
	}
}

// 直接读取 HTTP/1.1 原始头，证明内部空值哨兵既不会出线，也不会让 Go 补入
// Go-http-client/1.1。Files 控制面正依赖这个“画像没有 UA 即 wire 没有 UA”的语义。
func TestLowercaseHeaderRoundTripperWithoutUserAgentOmitsItOnWire(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听失败: %v", err)
	}
	defer func() { _ = listener.Close() }()

	head := make(chan []string, 1)
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		var lines []string
		scanner := bufio.NewScanner(conn)
		for scanner.Scan() {
			line := scanner.Text()
			if line == "" {
				break
			}
			lines = append(lines, line)
		}
		head <- lines
		_, _ = conn.Write([]byte("HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"))
	}()

	rt := NewLowercaseHeaderRoundTripper(&http.Transport{}, nil)
	req, err := http.NewRequest(http.MethodPost, "http://"+listener.Addr().String()+"/backend-api/files", nil)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	req.Header.Set("Authorization", "Bearer redacted")
	resp, err := rt.RoundTrip(req)
	if err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}
	if err := resp.Body.Close(); err != nil {
		t.Fatal(err)
	}

	for _, line := range <-head {
		if strings.HasPrefix(strings.ToLower(line), "user-agent:") {
			t.Fatalf("wire 不应出现 User-Agent，实际行=%q", line)
		}
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

// WS 握手不是全小写：tungstenite 把 Host/Connection/Upgrade/Sec-WebSocket-Version/
// Sec-WebSocket-Key 五项按硬编码字面量写出（大写驼峰），只有其余 header 走小写路径。
//
// 这里直接读 wire 字节而不看 Request.Header：Go 的 http server 会把收到的 header 名
// canonical 化后入 map，map 级断言看不出真实形态——正是该差异此前未被发现的原因。
func TestLowercaseHeaderRoundTripperPreservesWebSocketHandshakeCase(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听失败: %v", err)
	}
	defer func() { _ = listener.Close() }()

	names := make(chan []string, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		var got []string
		scanner := bufio.NewScanner(conn)
		for scanner.Scan() {
			line := scanner.Text()
			if line == "" {
				break
			}
			if idx := strings.Index(line, ":"); idx > 0 {
				got = append(got, line[:idx])
			}
		}
		names <- got
		_, _ = conn.Write([]byte("HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"))
	}()

	rt := NewLowercaseHeaderRoundTripper(&http.Transport{},
		[]string{"Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key"})
	req, err := http.NewRequest(http.MethodGet, "http://"+listener.Addr().String(), nil)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	// coder/websocket 用 Go canonical 形式设置这些头。
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Sec-Websocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
	req.Header.Set("Sec-Websocket-Version", "13")
	req.Header.Set("Chatgpt-Account-Id", "acct")
	req.Header.Set("Originator", "codex_cli_rs")

	resp, err := rt.RoundTrip(req)
	if err != nil {
		t.Fatalf("RoundTrip 失败: %v", err)
	}
	if err := resp.Body.Close(); err != nil {
		t.Fatal(err)
	}

	onWire := <-names
	present := make(map[string]bool, len(onWire))
	for _, name := range onWire {
		present[name] = true
	}
	// 官方大写驼峰的五项（Host 由 Go 的 Request.write 硬编码输出，本就一致）。
	for _, name := range []string{"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key"} {
		if !present[name] {
			t.Errorf("wire 上缺少官方形态的 %q，实际=%v", name, onWire)
		}
	}
	// 业务头仍须小写，且不得出现被压小写的握手头。
	for _, name := range []string{"chatgpt-account-id", "originator"} {
		if !present[name] {
			t.Errorf("wire 上缺少小写业务头 %q，实际=%v", name, onWire)
		}
	}
	for _, name := range []string{"connection", "upgrade", "sec-websocket-key", "sec-websocket-version"} {
		if present[name] {
			t.Errorf("握手头 %q 被压成小写，偏离 tungstenite 形态，实际=%v", name, onWire)
		}
	}
}
