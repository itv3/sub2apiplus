package tlsfingerprint

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"

	"golang.org/x/net/http2"
)

// tlsDialFunc 是三种 utls dialer（直连 / HTTP-CONNECT / SOCKS5）统一的握手函数签名：
// 完成 TCP 建链 + utls ClientHello 握手，返回已完成握手的 *utls.UConn（满足 net.Conn）。
type tlsDialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// ProfileNegotiatesH2 判断 profile 的 ALPN 是否声明了 h2（决定是否需要 HTTP/2 wire）。
func ProfileNegotiatesH2(profile *Profile) bool {
	if profile == nil {
		return false
	}
	for _, p := range profile.ALPNProtocols {
		if p == "h2" {
			return true
		}
	}
	return false
}

// resolveTLSDialFunc 按代理类型返回对应的 utls 握手函数。
// 返回值统一为 tlsDialFunc，屏蔽 direct / HTTP / SOCKS5 三种底层 dialer 的差异。
func resolveTLSDialFunc(profile *Profile, proxyURL *url.URL) (tlsDialFunc, error) {
	if proxyURL == nil {
		return NewDialer(profile, nil).DialTLSContext, nil
	}
	switch scheme := proxyURL.Scheme; scheme {
	case "socks5", "socks5h":
		return NewSOCKS5ProxyDialer(profile, proxyURL).DialTLSContext, nil
	case "http", "https":
		return NewHTTPProxyDialer(profile, proxyURL).DialTLSContext, nil
	default:
		return nil, fmt.Errorf("tlsfingerprint: unsupported proxy scheme for h2: %s", scheme)
	}
}

// NewH2Transport 构建一个走真正 HTTP/2 wire 协议的 RoundTripper。
//
// 背景：net/http 的 http.Transport 只有在自己管理 TLS 时才会按 ALPN 自动升级到 HTTP/2；
// 当我们用自定义 DialTLSContext 返回 utls 连接时，net/http 认不出 utls 的 ConnectionState，
// 即便 ALPN 协商成 h2 也仍按 HTTP/1.1 发帧。因此这里改用 golang.org/x/net/http2.Transport，
// 由它在 utls 握手完成后的连接上直接建立 HTTP/2 会话，保证 wire 层与官方 codex 一致。
//
// http2.Transport 的 DialTLSContext 复用上面的 utls 握手函数；传入的 *tls.Config 被忽略
// （握手参数完全由 profile 决定的 ClientHello 掌控）。
func NewH2Transport(profile *Profile, proxyURL *url.URL, idleConnTimeout, responseHeaderTimeout time.Duration) (http.RoundTripper, error) {
	dial, err := resolveTLSDialFunc(profile, proxyURL)
	if err != nil {
		return nil, err
	}
	return &http2.Transport{
		DisableCompression: profile != nil && profile.Transport.DisableCompression,
		// 复用 utls 握手：忽略 cfg，ClientHello 指纹由 profile 决定。
		DialTLSContext: func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
			return dial(ctx, network, addr)
		},
		IdleConnTimeout: idleConnTimeout,
		// ReadIdleTimeout 触发 h2 PING 健康检查，避免复用已被上游/代理静默关闭的连接。
		ReadIdleTimeout: responseHeaderTimeout,
	}, nil
}
