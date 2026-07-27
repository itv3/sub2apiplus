package tlsfingerprint

import (
	"bufio"
	"net"
	"net/http"
	"strings"
	"testing"
)

// 官方 models 请求的实测形态（official-h1-full-20260727T124125Z）：
//
//	version, authorization, chatgpt-account-id, accept, originator, user-agent, host
//
// 即：用户头按插入序 → host 在最后，全小写。Go 默认输出 Host 在最前 + 其余字典序。
var officialModelsOrder = []H1HeaderOrderRule{{Order: []string{
	"version", "authorization", "chatgpt-account-id", "accept", "originator", "user-agent",
}}}

// rules 把裸清单包成兜底规则，便于用例直接给顺序。
func rules(order ...string) []H1HeaderOrderRule {
	return []H1HeaderOrderRule{{Order: order}}
}

func TestRewriteH1HeadMatchesOfficialOrder(t *testing.T) {
	// Go 的 Request.write 会输出这种形态：Host 在最前，其余字典序。
	head := "GET /backend-api/codex/models?client_version=0.145.0 HTTP/1.1\r\n" +
		"Host: chatgpt.com\r\n" +
		"accept: */*\r\n" +
		"authorization: Bearer token\r\n" +
		"chatgpt-account-id: acct\r\n" +
		"originator: codex_exec\r\n" +
		"user-agent: codex_exec/0.145.0\r\n" +
		"version: 0.145.0\r\n\r\n"

	out, length, chunked, ok := rewriteH1Head([]byte(head), officialModelsOrder, nil)
	if !ok || chunked {
		t.Fatalf("重写失败: ok=%v chunked=%v", ok, chunked)
	}
	if length != 0 {
		t.Errorf("无 body 时 length 应为 0，实际 %d", length)
	}

	got := headerNamesOf(string(out))
	want := []string{"version", "authorization", "chatgpt-account-id", "accept", "originator", "user-agent", "host"}
	assertOrder(t, got, want)
}

func TestRewriteH1HeadPutsContentLengthLast(t *testing.T) {
	// 官方 POST /backend-api/ps/mcp 实测：..., content-type, host, content-length
	head := "POST /backend-api/codex/responses HTTP/1.1\r\n" +
		"Host: chatgpt.com\r\n" +
		"Content-Length: 42\r\n" +
		"accept: text/event-stream\r\n" +
		"content-type: application/json\r\n" +
		"originator: codex_exec\r\n\r\n"

	out, length, _, ok := rewriteH1Head([]byte(head), rules("accept", "content-type", "originator"), nil)
	if !ok {
		t.Fatal("重写失败")
	}
	if length != 42 {
		t.Errorf("Content-Length 应解析为 42，实际 %d", length)
	}
	got := headerNamesOf(string(out))
	assertOrder(t, got, []string{"accept", "content-type", "originator", "host", "content-length"})
}

func TestRewriteH1HeadPreservesWebSocketCase(t *testing.T) {
	// tungstenite 把这五项按硬编码字面量写出（大写驼峰），其余头才小写。
	preserve := map[string]string{
		"connection": "Connection", "upgrade": "Upgrade",
		"sec-websocket-version": "Sec-WebSocket-Version", "sec-websocket-key": "Sec-WebSocket-Key",
	}
	head := "GET /backend-api/codex/responses HTTP/1.1\r\n" +
		"Host: chatgpt.com\r\n" +
		"Connection: Upgrade\r\n" +
		"Upgrade: websocket\r\n" +
		"Sec-Websocket-Key: abc==\r\n" +
		"Sec-Websocket-Version: 13\r\n" +
		"chatgpt-account-id: acct\r\n\r\n"

	out, _, _, ok := rewriteH1Head([]byte(head),
		rules("connection", "upgrade", "sec-websocket-version", "sec-websocket-key", "chatgpt-account-id"), preserve)
	if !ok {
		t.Fatal("重写失败")
	}
	text := string(out)
	for _, want := range []string{"Connection: Upgrade", "Upgrade: websocket",
		"Sec-WebSocket-Key: abc==", "Sec-WebSocket-Version: 13"} {
		if !strings.Contains(text, want) {
			t.Errorf("缺少官方形态的 %q，实际输出:\n%s", want, text)
		}
	}
	if strings.Contains(text, "sec-websocket-key:") {
		t.Error("握手头被压成小写，偏离 tungstenite 形态")
	}
}

// chunked 无法安全改写（body 长度不可预知），必须原样透传。
func TestRewriteH1HeadBailsOnChunked(t *testing.T) {
	head := "POST /x HTTP/1.1\r\nHost: chatgpt.com\r\nTransfer-Encoding: chunked\r\n\r\n"
	_, _, chunked, _ := rewriteH1Head([]byte(head), nil, nil)
	if !chunked {
		t.Error("chunked 请求应被识别并放弃改写")
	}
}

// Go 抑制默认 UA 时写出的空值 User-Agent 不能出现在 wire 上。
func TestRewriteH1HeadDropsEmptyUserAgent(t *testing.T) {
	head := "GET /x HTTP/1.1\r\nHost: chatgpt.com\r\nUser-Agent: \r\nuser-agent: codex_exec/0.145.0\r\n\r\n"
	out, _, _, ok := rewriteH1Head([]byte(head), rules("user-agent"), nil)
	if !ok {
		t.Fatal("重写失败")
	}
	if strings.Count(string(out), "user-agent:") != 1 {
		t.Errorf("user-agent 应恰好出现一次，实际输出:\n%s", string(out))
	}
}

// 端到端：经真实 http.Transport 发请求，验证 wire 上的字节确实被改写。
func TestH1WireConnEndToEnd(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	names := make(chan []string, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
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

	raw, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	wrapped := newH1WireConn(raw, officialModelsOrder, nil)
	req, _ := http.NewRequest(http.MethodGet, "http://chatgpt.com/backend-api/codex/models", nil)
	req.Header.Set("version", "0.145.0")
	req.Header.Set("authorization", "Bearer t")
	req.Header.Set("chatgpt-account-id", "acct")
	req.Header.Set("accept", "*/*")
	req.Header.Set("originator", "codex_exec")
	if err := req.Write(wrapped); err != nil {
		t.Fatal(err)
	}

	got := <-names
	if len(got) == 0 || got[len(got)-1] != "host" {
		t.Errorf("host 应为最后一项且小写，实际=%v", got)
	}
	for _, n := range got {
		if n == "Host" {
			t.Errorf("wire 上仍出现大写 Host，实际=%v", got)
		}
	}
}

func headerNamesOf(head string) []string {
	var names []string
	for _, line := range strings.Split(strings.TrimSuffix(head, "\r\n\r\n"), "\r\n")[1:] {
		if idx := strings.Index(line, ":"); idx > 0 {
			names = append(names, line[:idx])
		}
	}
	return names
}

func assertOrder(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("header 数量不符\n实际=%v\n期望=%v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("第 %d 项不符\n实际=%v\n期望=%v", i, got, want)
		}
	}
}

// 官方各端点的插入序不同，规则集必须按路径分派——首版用并集清单时 models 顺序当场就错。
func TestOrderForPathSelectsByEndpoint(t *testing.T) {
	ruleset := []H1HeaderOrderRule{
		{PathContains: "/codex/models", Order: []string{"version", "authorization"}},
		{Order: []string{"chatgpt-account-id", "authorization"}},
	}
	if got := orderForPath(ruleset, "GET /backend-api/codex/models?x=1 HTTP/1.1"); got[0] != "version" {
		t.Errorf("models 应命中专属清单，实际首项=%q", got[0])
	}
	if got := orderForPath(ruleset, "POST /backend-api/codex/responses HTTP/1.1"); got[0] != "chatgpt-account-id" {
		t.Errorf("responses 应落到兜底清单，实际首项=%q", got[0])
	}
}
