package repository

// chatgpt-web persona 的 HTTP 层 wire 证据。
//
// 背景：privacy service 用 CreatePrivacyReqClient（ImpersonateChrome）直打
// chatgpt.com 的 settings / accounts / subscriptions 三个浏览器端点。变更集 0 的审核
// 指出：仅凭工厂层配置 Impersonate=true **不能**认定最终 Header 已经正确——真正决定
// 形态的是 req/v3 默认导航 Header 与请求级 Fetch Metadata 合并之后的结果。
//
// 本地 TLS server 可以提取 ClientHello 的部分语义字段（当前固定 supported_groups），
// 也可以记录最终 HTTP Header；但完整 ClientHello 字节与扩展顺序、GREASE，及真实 Chrome
// 的 HTTP/2 SETTINGS、pseudo-header/HPACK 顺序仍需外部抓包对照。本测试把可本地复验的
// 部分固化成 fixture，不能把这些断言扩张为完整浏览器 wire 已获证明。

import (
	"bufio"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// capturedRequest 记录服务端实际收到的形态。
type capturedRequest struct {
	proto       string
	headerNames []string // HTTP/2 解码后的键集合，不保留 wire 顺序
	headers     http.Header
}

// startCapturingTLSServer 起一个本地 TLS server，记录首个请求的 wire 形态。
//
// 用 TLS 而非明文：ImpersonateChrome 的 ALPN 协商结果会影响最终协议与 header 形态，
// 明文连接测不出真实分支。
func startCapturingTLSServer(t *testing.T, captured *capturedRequest) *httptest.Server {
	t.Helper()
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured.proto = r.Proto
		captured.headers = r.Header.Clone()
		// http.Header 是 map，丢失了 wire 顺序；HTTP/1.1 下可从 Host 之外的原始
		// 顺序近似恢复，这里记录键集合供断言使用。
		keys := make([]string, 0, len(r.Header))
		for k := range r.Header {
			keys = append(keys, strings.ToLower(k))
		}
		captured.headerNames = keys
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	srv.EnableHTTP2 = true
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
}

// TestChromePersonaWireHeaders 固化 chatgpt-web persona 的 HTTP 层出站形态。
//
// 断言的是**语义约束**而非逐字节快照：req/v3 版本升级会带来 Chrome 版本变化，
// 逐字节固定会让升级必然失败，而这些约束是 persona 成立的必要条件。
func TestChromePersonaWireHeaders(t *testing.T) {
	var captured capturedRequest
	srv := startCapturingTLSServer(t, &captured)

	client, err := CreatePrivacyReqClient("")
	require.NoError(t, err, "构造 Chrome impersonate 客户端")

	// httptest 自签证书：仅测试内跳过校验，不影响生产配置。
	client.SetTLSClientConfig(&tls.Config{InsecureSkipVerify: true}) //nolint:gosec // 本地自签

	// 复刻 openai_privacy_service.go disableOpenAITraining 的请求级 header。
	resp, err := client.R().
		SetHeader("Authorization", "Bearer test-token").
		SetHeader("Origin", "https://chatgpt.com").
		SetHeader("Referer", "https://chatgpt.com/").
		SetHeader("Accept", "application/json").
		SetHeader("sec-fetch-mode", "cors").
		SetHeader("sec-fetch-site", "same-origin").
		SetHeader("sec-fetch-dest", "empty").
		SetQueryParam("feature", "training_allowed").
		SetQueryParam("value", "false").
		Patch(srv.URL + "/backend-api/settings/account_user_setting")
	require.NoError(t, err, "发送 privacy 形态请求")
	require.Equal(t, http.StatusOK, resp.StatusCode)

	got := func(name string) string { return captured.headers.Get(name) }

	// --- 1. 请求级 Fetch Metadata 必须原样到达，不被 impersonate 默认值覆盖 ---
	//
	// 这是本测试最核心的断言。ImpersonateChrome 会注入一整套导航 header，其中
	// 包含 sec-fetch-*；若它们覆盖了 privacy service 显式设置的 cors/same-origin/empty，
	// 出站形态就会变成"浏览器导航"而不是"页面内 XHR"——两者在上游侧可区分。
	require.Equal(t, "cors", got("sec-fetch-mode"),
		"sec-fetch-mode 必须是 cors；被覆盖成 navigate 意味着形态退化为导航请求")
	require.Equal(t, "same-origin", got("sec-fetch-site"))
	require.Equal(t, "empty", got("sec-fetch-dest"))

	// --- 2. 浏览器身份标记必须存在 ---
	require.Contains(t, strings.ToLower(got("User-Agent")), "chrome",
		"User-Agent 必须是 Chrome；缺失说明 impersonate 未生效")
	require.Equal(t, "https://chatgpt.com", got("Origin"))
	require.Equal(t, "https://chatgpt.com/", got("Referer"))

	// --- 3. Chrome 特有 header 应由 impersonate 提供 ---
	//
	// sec-ch-ua 系列是 Chrome 的 Client Hints，Go 标准库不会产生。它们的存在是
	// impersonate 真正接管了 header 生成的证据。
	require.NotEmpty(t, got("sec-ch-ua"),
		"sec-ch-ua 缺失说明 ImpersonateChrome 未注入 Client Hints")
	require.NotEmpty(t, got("Accept-Language"))

	// --- 4. 不得泄漏 Codex 身份 ---
	//
	// browser persona 与 codex-cli persona 共用 chatgpt.com。若 Codex 头出现在
	// 浏览器端点上，等于在同一 host 上暴露两个身份使用同一套 header 生成逻辑。
	for _, leaked := range []string{"originator", "version", "session-id", "thread-id"} {
		require.Empty(t, got(leaked),
			"browser persona 不得携带 Codex 身份头 %q", leaked)
	}

	// --- 5. 协议应协商到 HTTP/2 ---
	//
	// Chrome 对 chatgpt.com 走 h2。若这里退化为 HTTP/1.1，说明 ALPN 或 HTTP/2
	// 配置有问题，wire 形态与真实浏览器不符。
	require.Equal(t, "HTTP/2.0", captured.proto,
		"Chrome persona 应协商到 HTTP/2；退化为 %s 说明 ALPN 配置异常", captured.proto)
}

// TestChrome133PersonaHasXHRWireWithoutNavigationHeaders 正向验证修复后的 XHR 语义。
func TestChrome133PersonaHasXHRWireWithoutNavigationHeaders(t *testing.T) {
	var captured capturedRequest
	srv := startCapturingTLSServer(t, &captured)

	client, err := CreatePrivacyReqClientWithProfile("", PrivacyReqClientProfile{
		Persona:        PrivacyBrowserPersonaChrome133,
		Platform:       "linux",
		AcceptLanguage: "en-GB,en;q=0.9",
	})
	require.NoError(t, err)
	client.SetTLSClientConfig(&tls.Config{InsecureSkipVerify: true}) //nolint:gosec // 本地自签

	resp, err := client.R().
		SetHeader("Accept", "application/json").
		SetHeader("sec-fetch-mode", "cors").
		SetHeader("sec-fetch-site", "same-origin").
		SetHeader("sec-fetch-dest", "empty").
		Patch(srv.URL + "/backend-api/settings/account_user_setting")
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)

	got := func(name string) string { return captured.headers.Get(name) }
	for _, header := range []string{"Sec-Fetch-User", "Upgrade-Insecure-Requests", "Cache-Control", "Pragma"} {
		require.Empty(t, got(header), "XHR wire 不得携带导航专属头 %s", header)
	}
	require.Equal(t, "application/json", got("Accept"))
	require.Equal(t, "cors", got("Sec-Fetch-Mode"))
	require.Equal(t, "same-origin", got("Sec-Fetch-Site"))
	require.Equal(t, "empty", got("Sec-Fetch-Dest"))
	require.Equal(t, `"Linux"`, got("Sec-Ch-Ua-Platform"))
	require.Equal(t, "en-GB,en;q=0.9", got("Accept-Language"))
	require.Contains(t, got("User-Agent"), "Chrome/133.")
	require.Contains(t, got("Sec-Ch-Ua"), `v="133"`)
}

// TestChrome133PersonaTLSAndHTTPVersionsMatch 验证 TLS PQ group 和 HTTP 版本声明同步。
func TestChrome133PersonaTLSAndHTTPVersionsMatch(t *testing.T) {
	var hello *tls.ClientHelloInfo
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.EnableHTTP2 = true
	srv.TLS = &tls.Config{
		GetConfigForClient: func(chi *tls.ClientHelloInfo) (*tls.Config, error) {
			hello = chi
			return nil, nil
		},
	}
	srv.StartTLS()
	t.Cleanup(srv.Close)

	client, err := CreatePrivacyReqClient("")
	require.NoError(t, err)
	client.SetTLSClientConfig(&tls.Config{InsecureSkipVerify: true}) //nolint:gosec // 本地自签

	_, err = client.R().Get(srv.URL)
	require.NoError(t, err)
	require.NotNil(t, hello, "未捕获到 ClientHello")

	hasPQ := false
	for _, curve := range hello.SupportedCurves {
		// X25519MLKEM768 = 0x11ec；旧草案 X25519Kyber768 = 0x6399。
		if curve == 0x11ec || curve == 0x6399 {
			hasPQ = true
		}
	}
	require.True(t, hasPQ, "Chrome 133 ClientHello 必须包含 X25519MLKEM768")
}

// TestChromePersonaIsolatedFromCodexProfile 验证两个 persona 的客户端池条目不共享。
//
// 连接池 key 若不区分 persona，Chrome 请求可能复用 Codex 画像建立的连接，
// 在同一 TLS 连接上先后出现两种身份的请求——这是最难排查的一类身份泄漏。
func TestChromePersonaIsolatedFromCodexProfile(t *testing.T) {
	chrome, err := CreatePrivacyReqClient("")
	require.NoError(t, err)
	legacy, err := CreatePrivacyReqClientWithProfile("", PrivacyReqClientProfile{
		Persona: PrivacyBrowserPersonaLegacyChrome120,
	})
	require.NoError(t, err)

	plain, err := getSharedReqClient(reqClientOptions{Timeout: 30 * time.Second})
	require.NoError(t, err)

	require.NotSame(t, chrome, plain,
		"Impersonate 与非 Impersonate 必须是不同的池条目，否则 Chrome 与其他 persona 会共享连接")
	require.NotSame(t, chrome, legacy,
		"Chrome 133 灰度和 Chrome 120 回滚画像不得共享连接池")
}

func TestPrivacyPersonaRolloutCanRollbackIndependently(t *testing.T) {
	const rolloutKey = "stable-account-bucket"
	require.Equal(t, PrivacyBrowserPersonaLegacyChrome120,
		SelectPrivacyBrowserPersona(false, 100, rolloutKey),
		"总开关关闭时必须独立回滚 browser persona")
	require.Equal(t, PrivacyBrowserPersonaLegacyChrome120,
		SelectPrivacyBrowserPersona(true, 0, rolloutKey))
	require.Equal(t, PrivacyBrowserPersonaChrome133,
		SelectPrivacyBrowserPersona(true, 100, rolloutKey))

	first := SelectPrivacyBrowserPersona(true, 37, rolloutKey)
	for range 20 {
		require.Equal(t, first, SelectPrivacyBrowserPersona(true, 37, rolloutKey),
			"同一账号的灰度分桶必须稳定")
	}
}

// startRawTLSCapture 起一个只 offer http/1.1 的 TLS listener，读取原始请求字节。
//
// 为什么必须降级到 HTTP/1.1：HTTP/2 下服务端拿到的 http.Header 是 map，顺序与 casing
// 已经丢失，而这两者都是指纹的组成部分。HTTP/1.1 的报文保留原始字节，是当前能在
// 本地拿到 header 顺序与 casing 的唯一方式。
//
// 局限必须说明：这条路径验证不了 HTTP/2 的 pseudo-header 顺序与 HPACK 编码，
// 那部分仍需真实抓包比对。
func startRawTLSCapture(t *testing.T) (addr string, lines <-chan []string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		DNSNames:     []string{"localhost"},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	require.NoError(t, err)
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	kb, err := x509.MarshalECPrivateKey(key)
	require.NoError(t, err)
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kb})
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	require.NoError(t, err)

	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{
		Certificates: []tls.Certificate{cert},
		NextProtos:   []string{"http/1.1"},
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = ln.Close() })

	out := make(chan []string, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			out <- nil
			return
		}
		defer func() { _ = conn.Close() }()
		reader := bufio.NewReader(conn)
		var collected []string
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				break
			}
			line = strings.TrimRight(line, "\r\n")
			if line == "" {
				break
			}
			collected = append(collected, line)
		}
		_, _ = conn.Write([]byte("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"))
		out <- collected
	}()
	return ln.Addr().String(), out
}

// TestChromePersonaRawWireOrder 验证 HTTP/1.1 wire 上的 header casing 与压缩能力。
func TestChromePersonaRawWireOrder(t *testing.T) {
	addr, lines := startRawTLSCapture(t)

	client, err := CreatePrivacyReqClient("")
	require.NoError(t, err)
	client.SetTLSClientConfig(&tls.Config{
		InsecureSkipVerify: true, //nolint:gosec // 本地自签
		NextProtos:         []string{"http/1.1"},
	})
	_, _ = client.R().
		SetHeader("sec-fetch-mode", "cors").
		Get("https://" + addr + "/backend-api/settings/account_user_setting")

	wire := <-lines
	require.NotEmpty(t, wire, "未捕获到原始请求")

	var names []string
	byName := map[string]string{}
	for _, line := range wire[1:] {
		idx := strings.Index(line, ":")
		if idx <= 0 {
			continue
		}
		name := line[:idx]
		names = append(names, name)
		byName[strings.ToLower(name)] = strings.TrimSpace(line[idx+1:])
	}

	// --- HTTP/1.1 下 Chrome 使用 Title-Case，全小写会立刻暴露非浏览器实现 ---
	require.Contains(t, names, "User-Agent", "HTTP/1.1 应为 Title-Case")
	require.Contains(t, names, "Sec-Fetch-Mode")

	// req/v3 自动解压模式会声明并正确处理 Chrome 常用的四种编码。
	require.Equal(t, "gzip, deflate, br, zstd", byName["accept-encoding"])

	t.Logf("HTTP/1.1 wire header 顺序（供 fixture 对照）：")
	for i, n := range names {
		t.Logf("  [%02d] %s: %s", i, n, byName[strings.ToLower(n)])
	}
}

// TestChromePersonaCoversAllThreeEndpoints 验证三个 privacy 端点共用同一 XHR 画像。
func TestChromePersonaCoversAllThreeEndpoints(t *testing.T) {
	for _, tc := range []struct {
		name   string
		method string
		path   string
	}{
		{
			name: "settings_PATCH", method: http.MethodPatch,
			path: "/backend-api/settings/account_user_setting",
		},
		{
			name: "accounts_check_GET", method: http.MethodGet,
			path: "/backend-api/accounts/check/v4-2023-04-27",
		},
		{
			name: "subscriptions_GET", method: http.MethodGet,
			path: "/backend-api/subscriptions",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var captured capturedRequest
			srv := startCapturingTLSServer(t, &captured)

			client, err := CreatePrivacyReqClient("")
			require.NoError(t, err)
			client.SetTLSClientConfig(&tls.Config{InsecureSkipVerify: true}) //nolint:gosec // 本地自签

			req := client.R().
				SetHeader("Authorization", "Bearer test").
				SetHeader("Origin", "https://chatgpt.com").
				SetHeader("Referer", "https://chatgpt.com/").
				SetHeader("Accept", "application/json")

			var sendErr error
			if tc.method == http.MethodPatch {
				_, sendErr = req.Patch(srv.URL + tc.path)
			} else {
				_, sendErr = req.Get(srv.URL + tc.path)
			}
			require.NoError(t, sendErr)

			// 三端点共有的 persona 属性。
			require.Contains(t, strings.ToLower(captured.headers.Get("User-Agent")), "chrome/133")
			require.Equal(t, "HTTP/2.0", captured.proto)
			require.Contains(t, captured.headers.Get("sec-ch-ua"), `v="133"`)
			require.Equal(t, "cors", captured.headers.Get("sec-fetch-mode"))
			require.Equal(t, "same-origin", captured.headers.Get("sec-fetch-site"))
			require.Equal(t, "empty", captured.headers.Get("sec-fetch-dest"))
			for _, header := range []string{"Sec-Fetch-User", "Upgrade-Insecure-Requests", "Cache-Control", "Pragma"} {
				require.Empty(t, captured.headers.Get(header))
			}
		})
	}
}
