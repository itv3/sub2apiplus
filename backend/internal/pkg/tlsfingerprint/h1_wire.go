package tlsfingerprint

import (
	"bytes"
	"net"
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
// 安全兜底
// --------
// 任何无法确信改写正确的情形（chunked、超长头、解析失败）一律切换为 passthrough，
// 原样透传后续所有字节。宁可保留形态偏离，也不能破坏请求。
type h1WireConn struct {
	net.Conn

	// rules 按请求路径给出官方 header 次序；官方各端点的插入序不同，故不能共用一份。
	rules []H1HeaderOrderRule
	// preserve 以小写名为键、官方字面量为值，用于 WS 握手那几个大写驼峰的必需头。
	preserve map[string]string

	pending     bytes.Buffer
	bodyRemain  int64
	inBody      bool
	passthrough bool
}

const h1WireMaxHeadBytes = 512 * 1024

func newH1WireConn(conn net.Conn, rules []H1HeaderOrderRule, preserve map[string]string) net.Conn {
	if conn == nil {
		return nil
	}
	return &h1WireConn{Conn: conn, rules: rules, preserve: preserve}
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

		c.pending.Write(p)
		p = nil

		idx := bytes.Index(c.pending.Bytes(), []byte("\r\n\r\n"))
		if idx < 0 {
			if c.pending.Len() > h1WireMaxHeadBytes {
				// 头部异常大，放弃改写，把已缓冲的内容原样吐出去。
				return c.giveUp(consumed)
			}
			continue
		}

		head := c.pending.Next(idx + 4)
		rest := append([]byte(nil), c.pending.Bytes()...)
		c.pending.Reset()

		rewritten, length, chunked, ok := rewriteH1Head(head, c.rules, c.preserve)
		if !ok || chunked {
			// 解析失败或 chunked：不改写，原样发出并从此放弃干预本连接。
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
		c.bodyRemain = length
		c.inBody = length > 0
		p = rest
	}
	return consumed, nil
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

// orderForPath 取第一个匹配的规则；PathContains 为空的作为兜底。
func orderForPath(rules []H1HeaderOrderRule, requestLine string) []string {
	var fallback []string
	for _, rule := range rules {
		if rule.PathContains == "" {
			if fallback == nil {
				fallback = rule.Order
			}
			continue
		}
		if strings.Contains(requestLine, rule.PathContains) {
			return rule.Order
		}
	}
	return fallback
}

// rewriteH1Head 按官方形态重排请求头。
//
// 输出顺序为：匹配到的清单内的头（按清单序）→ 清单外的头（按原出现序）→ host
// → content-length。名称一律小写，preserve 命中项按给定字面量输出。
//
// 返回 length 为 Content-Length 取值，chunked 表示请求使用分块编码（调用方应放弃改写）。
func rewriteH1Head(head []byte, rules []H1HeaderOrderRule, preserve map[string]string) (out []byte, length int64, chunked bool, ok bool) {
	text := string(head)
	text = strings.TrimSuffix(text, "\r\n\r\n")
	lines := strings.Split(text, "\r\n")
	if len(lines) == 0 || lines[0] == "" {
		return nil, 0, false, false
	}

	type field struct{ name, value string }
	order := orderForPath(rules, lines[0])

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

	emit := func(builder *strings.Builder, name, value string) {
		if literal, hit := preserve[name]; hit {
			name = literal
		}
		builder.WriteString(name)
		builder.WriteString(": ")
		builder.WriteString(value)
		builder.WriteString("\r\n")
	}

	var builder strings.Builder
	builder.WriteString(requestLine)
	builder.WriteString("\r\n")

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
	emit(&builder, "host", hostValue)
	if contentLength != "" {
		emit(&builder, "content-length", contentLength)
	}
	builder.WriteString("\r\n")
	return []byte(builder.String()), length, false, true
}
