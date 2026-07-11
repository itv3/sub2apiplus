//go:build unit

package tlsfingerprint

import (
	"net/url"
	"testing"

	"golang.org/x/net/http2"
)

func TestProfileNegotiatesH2(t *testing.T) {
	if ProfileNegotiatesH2(nil) {
		t.Fatal("nil profile 不应判定为 h2")
	}
	if ProfileNegotiatesH2(&Profile{ALPNProtocols: []string{"http/1.1"}}) {
		t.Fatal("仅 http/1.1 不应判定为 h2")
	}
	if !ProfileNegotiatesH2(&Profile{ALPNProtocols: []string{"h2", "http/1.1"}}) {
		t.Fatal("含 h2 应判定为 h2")
	}
}

func TestNewH2TransportDirect(t *testing.T) {
	rt, err := NewH2Transport(&Profile{ALPNProtocols: []string{"h2", "http/1.1"}}, nil, 0, 0)
	if err != nil {
		t.Fatalf("direct h2 transport 构建失败: %v", err)
	}
	if _, ok := rt.(*http2.Transport); !ok {
		t.Fatalf("期望 *http2.Transport，得到 %T", rt)
	}
}

func TestNewH2TransportSOCKS5(t *testing.T) {
	u, _ := url.Parse("socks5://127.0.0.1:1080")
	rt, err := NewH2Transport(&Profile{ALPNProtocols: []string{"h2"}}, u, 0, 0)
	if err != nil {
		t.Fatalf("socks5 h2 transport 构建失败: %v", err)
	}
	if _, ok := rt.(*http2.Transport); !ok {
		t.Fatalf("期望 *http2.Transport，得到 %T", rt)
	}
}

func TestNewH2TransportUnknownProxyScheme(t *testing.T) {
	u, _ := url.Parse("ftp://127.0.0.1:21")
	if _, err := NewH2Transport(&Profile{ALPNProtocols: []string{"h2"}}, u, 0, 0); err == nil {
		t.Fatal("未知代理协议应返回错误")
	}
}
