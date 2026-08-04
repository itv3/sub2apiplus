package tlsfingerprint

import (
	"bytes"
	"errors"
	"net"
	"net/http"
	"strconv"
	"strings"
)

// h1WireConn 在 wire 层重写 HTTP/1.1 请求头，使其与官方 hyper 的输出形态一致。
//
// 为什么必须在 conn 层做
// ----------------------
// Go 把请求写入藏在 `http.Transport` 的 persistConn.writeLoop 内部
// （`req.write` → `Header.writeSubset`），**没有任何 hook 可以插入**。而那条路：
//
//   - 对 Host 硬编码 `fmt.Fprintf(w, "Host: %s\r\n", host)` 并置于最前
//   - 把 Host / User-Agent / Content-Length 排除在 writeSubset 之外
//   - 对其余 header 强制字典序
//
// 官方 hyper 则是「用户头按 HeaderMap 插入序 → host → content-length」，且全小写
// （规格表 SPEC-H1-001~004）。两者无法通过任何 Transport 配置调和。
//
// 相比自实现 RoundTripper（须自管连接池、keep-alive、响应读取、超时，且每请求新建
// 连接又会因失去 keep-alive 而制造新偏离），在 conn 层拦截可完整复用 Go 的这些能力。
//
// 完整官方画像开启 strict 后，任何无法确信改写正确的情形都会在写出字节前失败；
// 只有历史非严格画像仍保留 passthrough 兼容行为。
type h1WireConn struct {
	net.Conn

	// rules 按请求路径给出官方 header 次序；官方各端点的插入序不同，故不能共用一份。
	rules []H1HeaderOrderRule
	// preserve 以小写名为键、官方字面量为值，用于 WS 握手那几个大写驼峰的必需头。
	preserve map[string]string
	strict   bool

	pending     bytes.Buffer
	bodyRemain  int64
	inBody      bool
	passthrough bool
}

const h1WireMaxHeadBytes = 512 * 1024

func newH1WireConn(conn net.Conn, rules []H1HeaderOrderRule, preserve map[string]string) net.Conn {
	return newH1WireConnWithMode(conn, rules, preserve, false)
}

func newH1WireConnWithMode(
	conn net.Conn,
	rules []H1HeaderOrderRule,
	preserve map[string]string,
	strict bool,
) net.Conn {
	if conn == nil {
		return nil
	}
	return &h1WireConn{Conn: conn, rules: rules, preserve: preserve, strict: strict}
}

func (c *h1WireConn) Write(p []byte) (int, error) {
	if c.passthrough {
		return c.Conn.Write(p)
	}

	consumed := len(p)
	for len(p) > 0 {
		if c.inBody {
			// body 段原样透传，只做计数以便识别下一个请求的头部起点。
			n := int64(len(p))
			if c.bodyRemain < n {
				n = c.bodyRemain
			}
			if _, err := c.Conn.Write(p[:n]); err != nil {
				return 0, err
			}
			p = p[n:]
			c.bodyRemain -= n
			if c.bodyRemain == 0 {
				c.inBody = false
			}
			continue
		}

		_, _ = c.pending.Write(p)
		p = nil

		idx := bytes.Index(c.pending.Bytes(), []byte("\r\n\r\n"))
		if idx < 0 {
			if c.pending.Len() > h1WireMaxHeadBytes {
				if c.strict {
					return 0, errors.New("official h1 wire header exceeds profile limit")
				}
				return c.giveUp(consumed)
			}
			continue
		}

		head := c.pending.Next(idx + 4)
		rest := append([]byte(nil), c.pending.Bytes()...)
		c.pending.Reset()
		webSocketUpgrade := isH1WebSocketUpgrade(head)

		rewritten, length, chunked, ok := rewriteH1HeadWithMode(
			head,
			c.rules,
			c.preserve,
			c.strict,
		)
		if !ok || chunked {
			if c.strict {
				return 0, errors.New("official h1 wire request does not match immutable profile")
			}
			c.passthrough = true
			if _, err := c.Conn.Write(head); err != nil {
				return 0, err
			}
			if len(rest) > 0 {
				if _, err := c.Conn.Write(rest); err != nil {
					return 0, err
				}
			}
			return consumed, nil
		}

		if _, err := c.Conn.Write(rewritten); err != nil {
			return 0, err
		}
		// HTTP Upgrade 成功写出后，这条连接的后续字节已经是 WebSocket 帧，
		// 不能再按 HTTP 请求头缓冲。否则 Write 会表面返回成功，却把业务帧留在
		// pending 中，直到连接关闭也不会真正送入网络。
		if webSocketUpgrade {
			c.passthrough = true
			if len(rest) > 0 {
				if _, err := c.Conn.Write(rest); err != nil {
					return 0, err
				}
			}
			return consumed, nil
		}
		c.bodyRemain = length
		c.inBody = length > 0
		p = rest
	}
	return consumed, nil
}

// isH1WebSocketUpgrade 只识别完整、合法的 WebSocket Upgrade 请求。
// h1WireConn 只包装客户端请求腿，因此命中后即可永久切换为原始字节透传。
func isH1WebSocketUpgrade(head []byte) bool {
	text := strings.TrimSuffix(string(head), "\r\n\r\n")
	lines := strings.Split(text, "\r\n")
	if len(lines) < 2 || !strings.HasPrefix(lines[0], http.MethodGet+" ") {
		return false
	}
	connectionUpgrade := false
	webSocketUpgrade := false
	for _, line := range lines[1:] {
		idx := strings.IndexByte(line, ':')
		if idx <= 0 {
			return false
		}
		name := strings.ToLower(strings.TrimSpace(line[:idx]))
		value := strings.TrimSpace(line[idx+1:])
		switch name {
		case "connection":
			connectionUpgrade = h1HeaderContainsToken(value, "upgrade")
		case "upgrade":
			webSocketUpgrade = h1HeaderContainsToken(value, "websocket")
		}
	}
	return connectionUpgrade && webSocketUpgrade
}

func h1HeaderContainsToken(value string, target string) bool {
	for _, token := range strings.Split(value, ",") {
		if strings.EqualFold(strings.TrimSpace(token), target) {
			return true
		}
	}
	return false
}

// giveUp 放弃改写并把缓冲内容原样送出，之后本连接一律透传。
func (c *h1WireConn) giveUp(consumed int) (int, error) {
	c.passthrough = true
	buffered := c.pending.Bytes()
	c.pending.Reset()
	if len(buffered) > 0 {
		if _, err := c.Conn.Write(buffered); err != nil {
			return 0, err
		}
	}
	return consumed, nil
}

// orderForPath 是旧画像使用的兼容入口。
func orderForPath(rules []H1HeaderOrderRule, requestLine string) []string {
	rule, ok := matchH1HeaderOrderRule(rules, requestLine, nil)
	if !ok {
		return nil
	}
	return rule.Order
}

// rewriteH1Head 按官方形态重排请求头。
//
// 输出顺序为：匹配到的清单内的头（按清单序）→ 清单外的头（按原出现序）→ host
// → content-length。名称一律小写，preserve 命中项按给定字面量输出。
//
// 返回 length 为 Content-Length 取值，chunked 表示请求使用分块编码（调用方应放弃改写）。
func rewriteH1Head(head []byte, rules []H1HeaderOrderRule, preserve map[string]string) (out []byte, length int64, chunked bool, ok bool) {
	return rewriteH1HeadWithMode(head, rules, preserve, false)
}

func rewriteH1HeadWithMode(
	head []byte,
	rules []H1HeaderOrderRule,
	preserve map[string]string,
	strict bool,
) (out []byte, length int64, chunked bool, ok bool) {
	text := string(head)
	text = strings.TrimSuffix(text, "\r\n\r\n")
	lines := strings.Split(text, "\r\n")
	if len(lines) == 0 || lines[0] == "" {
		return nil, 0, false, false
	}

	type field struct{ name, value string }

	var (
		requestLine   = lines[0]
		fields        []field
		hostValue     string
		contentLength string
	)

	for _, line := range lines[1:] {
		if line == "" {
			continue
		}
		idx := strings.Index(line, ":")
		if idx <= 0 {
			return nil, 0, false, false
		}
		name := strings.ToLower(strings.TrimSpace(line[:idx]))
		value := strings.TrimSpace(line[idx+1:])
		switch name {
		case "host":
			hostValue = value
		case "content-length":
			contentLength = value
		case "transfer-encoding":
			if strings.Contains(strings.ToLower(value), "chunked") {
				return nil, 0, true, true
			}
			fields = append(fields, field{name, value})
		default:
			// Go 在抑制默认 UA 时会写出空值 User-Agent，不能带到 wire 上。
			if value == "" && name == "user-agent" {
				continue
			}
			fields = append(fields, field{name, value})
		}
	}
	if hostValue == "" {
		return nil, 0, false, false
	}
	if contentLength != "" {
		parsed, err := strconv.ParseInt(contentLength, 10, 64)
		if err != nil {
			return nil, 0, false, false
		}
		length = parsed
	}

	headerNames := make(map[string]bool, len(fields))
	for _, current := range fields {
		headerNames[current.name] = true
	}
	rule, matched := matchH1HeaderOrderRule(rules, requestLine, headerNames)
	if !matched {
		if strict {
			return nil, 0, false, false
		}
		rule = H1HeaderOrderRule{}
	}
	hostOrdered := h1HeaderRuleContains(rule, "host")
	contentLengthOrdered := h1HeaderRuleContains(rule, "content-length")
	if hostOrdered {
		fields = append(fields, field{name: "host", value: hostValue})
		headerNames["host"] = true
	}
	if contentLengthOrdered && contentLength != "" {
		fields = append(fields, field{name: "content-length", value: contentLength})
		headerNames["content-length"] = true
	}
	order, ordered := resolveH1HeaderOrder(rule, headerNames)
	if !ordered {
		return nil, 0, false, false
	}
	if rule.RejectUnlisted {
		allowed := make(map[string]bool, len(rule.Order)+len(rule.PrefixHeaders)+len(rule.AppendHeaders))
		for _, name := range rule.Order {
			allowed[strings.ToLower(strings.TrimSpace(name))] = true
		}
		for _, name := range rule.PrefixHeaders {
			allowed[strings.ToLower(strings.TrimSpace(name))] = true
		}
		for _, name := range rule.AppendHeaders {
			allowed[strings.ToLower(strings.TrimSpace(name))] = true
		}
		for name := range headerNames {
			if !allowed[name] {
				return nil, 0, false, false
			}
		}
	}

	emit := func(builder *strings.Builder, name, value string) {
		if literal, hit := preserve[name]; hit {
			name = literal
		}
		_, _ = builder.WriteString(name)
		_, _ = builder.WriteString(": ")
		_, _ = builder.WriteString(value)
		_, _ = builder.WriteString("\r\n")
	}

	var builder strings.Builder
	_, _ = builder.WriteString(requestLine)
	_, _ = builder.WriteString("\r\n")

	written := make(map[string]bool, len(fields))
	for _, name := range order {
		for _, f := range fields {
			if f.name == name && !written[f.name] {
				emit(&builder, f.name, f.value)
				written[f.name] = true
			}
		}
	}
	for _, f := range fields {
		if !written[f.name] {
			emit(&builder, f.name, f.value)
			written[f.name] = true
		}
	}

	// host 与 content-length 收尾：官方由 reqwest 在用户头之后插入 host，
	// 再由 hyper 的 set_length 在 encode 前追加 content-length。
	if !hostOrdered {
		emit(&builder, "host", hostValue)
	}
	if contentLength != "" && !contentLengthOrdered {
		emit(&builder, "content-length", contentLength)
	}
	_, _ = builder.WriteString("\r\n")
	return []byte(builder.String()), length, false, true
}

func h1HeaderRuleContains(rule H1HeaderOrderRule, expected string) bool {
	expected = strings.ToLower(strings.TrimSpace(expected))
	for _, names := range [][]string{rule.Order, rule.PrefixHeaders, rule.AppendHeaders} {
		for _, rawName := range names {
			if strings.ToLower(strings.TrimSpace(rawName)) == expected {
				return true
			}
		}
	}
	return false
}

func matchH1HeaderOrderRule(
	rules []H1HeaderOrderRule,
	requestLine string,
	headers map[string]bool,
) (H1HeaderOrderRule, bool) {
	parts := strings.Fields(requestLine)
	if len(parts) < 2 {
		return H1HeaderOrderRule{}, false
	}
	method := strings.ToUpper(parts[0])
	target := parts[1]
	path, _, _ := strings.Cut(target, "?")
	var fallback *H1HeaderOrderRule
	for index := range rules {
		rule := &rules[index]
		if rule.Method != "" && !strings.EqualFold(rule.Method, method) {
			continue
		}
		if rule.Path != "" && rule.Path != path {
			continue
		}
		if rule.Path == "" && rule.PathContains != "" && !strings.Contains(target, rule.PathContains) {
			continue
		}
		if rule.Path == "" && rule.PathContains == "" {
			if fallback == nil {
				fallback = rule
			}
			continue
		}
		if !matchH1HeaderConditions(*rule, headers) {
			continue
		}
		return *rule, true
	}
	if fallback != nil && matchH1HeaderConditions(*fallback, headers) {
		return *fallback, true
	}
	return H1HeaderOrderRule{}, false
}

func matchH1HeaderConditions(rule H1HeaderOrderRule, headers map[string]bool) bool {
	if headers == nil {
		return len(rule.RequiredHeaders) == 0 && len(rule.ForbiddenHeaders) == 0
	}
	for _, name := range rule.RequiredHeaders {
		if !headers[strings.ToLower(strings.TrimSpace(name))] {
			return false
		}
	}
	for _, name := range rule.ForbiddenHeaders {
		if headers[strings.ToLower(strings.TrimSpace(name))] {
			return false
		}
	}
	return true
}

func resolveH1HeaderOrder(rule H1HeaderOrderRule, headers map[string]bool) ([]string, bool) {
	filtered := make([]string, 0, len(rule.Order))
	seen := make(map[string]bool, len(rule.Order))
	for _, rawName := range rule.Order {
		name := strings.ToLower(strings.TrimSpace(rawName))
		if name == "" || seen[name] {
			return nil, false
		}
		seen[name] = true
		if headers[name] {
			filtered = append(filtered, name)
		}
	}
	if rule.Mode == "" || rule.Mode == H1HeaderOrderModeStatic {
		return filtered, true
	}
	if rule.Mode != H1HeaderOrderModeSwapRemove {
		return nil, false
	}

	remaining := append([]string(nil), filtered...)
	for _, rawName := range rule.RemoveHeaders {
		name := strings.ToLower(strings.TrimSpace(rawName))
		index := -1
		for i, current := range remaining {
			if current == name {
				index = i
				break
			}
		}
		if index < 0 {
			return nil, false
		}
		last := len(remaining) - 1
		remaining[index] = remaining[last]
		remaining = remaining[:last]
	}

	resolved := make([]string, 0, len(rule.PrefixHeaders)+len(remaining)+len(rule.AppendHeaders))
	for _, rawName := range rule.PrefixHeaders {
		name := strings.ToLower(strings.TrimSpace(rawName))
		if name == "" || !headers[name] {
			return nil, false
		}
		resolved = append(resolved, name)
	}
	resolved = append(resolved, remaining...)
	for _, rawName := range rule.AppendHeaders {
		name := strings.ToLower(strings.TrimSpace(rawName))
		if name == "" || seen[name] {
			return nil, false
		}
		seen[name] = true
		if headers[name] {
			resolved = append(resolved, name)
		}
	}
	return resolved, true
}
