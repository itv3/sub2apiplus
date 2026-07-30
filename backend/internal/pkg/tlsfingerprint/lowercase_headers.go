package tlsfingerprint

import (
	"net/http"
	"strings"
)

// lowercaseHeaderRoundTripper 在请求真正写上线前把 header 名整体转为小写。
//
// 官方 Rust 客户端（hyper）在 HTTP/1.1 上直接用 HeaderName::as_str() 输出，即全小写；
// Go 的 Header.Set 经 CanonicalMIMEHeaderKey 改写成 Session-Id / Originator 这类形态，
// 在 h1 线上逐字节可区分。HTTP/2 的 HPACK 本就强制小写，这一层对 h2 无副作用。
//
// 放在传输层而不是各 Finalizer 里，是为了让语义定型与 wire 形态分层：上层继续用
// canonical 写法读写 header，wire 形态由画像统一收口。
//
// 但"全小写"并非官方的统一形态：WS 握手由 tungstenite 生成，它把
// Host/Connection/Upgrade/Sec-WebSocket-Version/Sec-WebSocket-Key 五项按硬编码字面量
// 写出（大写驼峰），只有其余 header 才走 HeaderName::as_str() 的小写路径。因此需要
// preserve 例外清单，否则握手的这几项会被这一层改成小写而偏离官方。
type lowercaseHeaderRoundTripper struct {
	base http.RoundTripper
	// preserve 以小写名为键、官方字面量为值。命中项直接用该字面量作 map key，
	// writeSubset 原样输出，从而绕开 CanonicalMIMEHeaderKey 的改写。
	preserve map[string]string
}

// NewLowercaseHeaderRoundTripper 包装 base，使其发出的 header 名全部为小写；
// preserveCase 中的名字按给定字面量原样输出，用于官方并非小写的少数 header。
func NewLowercaseHeaderRoundTripper(base http.RoundTripper, preserveCase []string) http.RoundTripper {
	if base == nil {
		return nil
	}
	var preserve map[string]string
	if len(preserveCase) > 0 {
		preserve = make(map[string]string, len(preserveCase))
		for _, name := range preserveCase {
			if trimmed := strings.TrimSpace(name); trimmed != "" {
				preserve[strings.ToLower(trimmed)] = trimmed
			}
		}
	}
	return &lowercaseHeaderRoundTripper{base: base, preserve: preserve}
}

func (r *lowercaseHeaderRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if r == nil || r.base == nil {
		return nil, http.ErrUseLastResponse
	}
	if req == nil {
		return r.base.RoundTrip(req)
	}

	cloned := req.Clone(req.Context())
	lowered := make(http.Header, len(cloned.Header))
	userAgent := ""
	for name, values := range cloned.Header {
		lower := strings.ToLower(name)
		// User-Agent 单独处理：Go 的 Request.write 硬编码输出 "User-Agent: " 前缀，
		// 并在 canonical key 缺失时填入 Go 默认 UA。直接改成小写会同时发出
		// "User-Agent: Go-http-client/1.1" 和小写两行。
		if lower == "user-agent" {
			if userAgent == "" && len(values) > 0 {
				userAgent = values[0]
			}
			continue
		}
		if literal, ok := r.preserve[lower]; ok {
			lowered[literal] = values
			continue
		}
		lowered[lower] = values
	}
	// canonical key 必须始终置空：h1 路径下 Request.write 会在请求没有
	// User-Agent 时自动补 Go-http-client/1.1，而官方画像中的“缺少该槽位”表示
	// wire 上确实没有该头。h2 路径同样以这个空值标记阻止默认值注入。
	lowered["User-Agent"] = []string{""}
	if userAgent != "" {
		// 画像显式声明 User-Agent 时，空 canonical 值只负责抑制 Go 默认值，
		// 真正的官方值仍由小写 key 写出，因此 wire 上恰好只有一行。
		lowered["user-agent"] = []string{userAgent}
	}
	cloned.Header = lowered
	return r.base.RoundTrip(cloned)
}

// CloseIdleConnections 透传给底层 Transport，避免包装后连接无法回收。
func (r *lowercaseHeaderRoundTripper) CloseIdleConnections() {
	if r == nil || r.base == nil {
		return
	}
	if closer, ok := r.base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}
