//go:build unit

package tlsfingerprint

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"net/http/httptest"
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

func TestNewH2TransportUsesProfileCompressionSetting(t *testing.T) {
	rt, err := NewH2Transport(&Profile{
		ALPNProtocols: []string{"h2", "http/1.1"},
		Transport:     TransportOptions{DisableCompression: true},
	}, nil, 0, 0)
	if err != nil {
		t.Fatalf("direct h2 transport 构建失败: %v", err)
	}
	transport, ok := rt.(*http2.Transport)
	if !ok {
		t.Fatalf("期望 *http2.Transport，得到 %T", rt)
	}
	if !transport.DisableCompression {
		t.Fatal("profile 开启禁用压缩后，h2 transport 应关闭自动压缩")
	}
}

func TestNewH2TransportDisableCompressionOmitsAcceptEncodingOnWire(t *testing.T) {
	headerSeen := make(chan bool, 1)
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, exists := r.Header["Accept-Encoding"]
		headerSeen <- exists
		w.WriteHeader(http.StatusNoContent)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()

	rt, err := NewH2Transport(&Profile{
		ALPNProtocols: []string{"h2", "http/1.1"},
		Transport:     TransportOptions{DisableCompression: true},
	}, nil, 0, 0)
	if err != nil {
		t.Fatalf("direct h2 transport 构建失败: %v", err)
	}
	transport := rt.(*http2.Transport)
	transport.DialTLSContext = func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
		dialer := &tls.Dialer{Config: &tls.Config{
			InsecureSkipVerify: true, // 测试服务器使用临时自签名证书。
			NextProtos:         []string{"h2"},
		}}
		return dialer.DialContext(ctx, network, addr)
	}

	req, err := http.NewRequest(http.MethodPost, server.URL, nil)
	if err != nil {
		t.Fatalf("创建请求失败: %v", err)
	}
	resp, err := (&http.Client{Transport: transport}).Do(req)
	if err != nil {
		t.Fatalf("执行 h2 请求失败: %v", err)
	}
	defer resp.Body.Close()

	if <-headerSeen {
		t.Fatal("关闭自动压缩后，wire 上不应出现 accept-encoding")
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
