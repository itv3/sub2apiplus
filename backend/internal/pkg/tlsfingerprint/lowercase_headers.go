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
type lowercaseHeaderRoundTripper struct {
	base http.RoundTripper
}

// NewLowercaseHeaderRoundTripper 包装 base，使其发出的 header 名全部为小写。
func NewLowercaseHeaderRoundTripper(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		return nil
	}
	return &lowercaseHeaderRoundTripper{base: base}
}

func (r *lowercaseHeaderRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if r == nil || r.base == nil {
		return nil, http.ErrUseLastResponse
	}
	if req == nil || len(req.Header) == 0 {
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
		lowered[lower] = values
	}
	if userAgent != "" {
		// canonical key 置空串：h1 路径下 Request.write 跳过硬编码输出；
		// h2 路径下 x/net/http2 以 asciiEqualFold 匹配 user-agent，空值同样跳过
		// 并标记 didUA，不会再补默认 UA。两条路径都只发出一个小写 user-agent。
		lowered["User-Agent"] = []string{""}
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
