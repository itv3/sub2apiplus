package repository

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/proxyurl"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"

	"github.com/imroc/req/v3"
	utls "github.com/refraction-networking/utls"
)

// reqClientOptions 定义 req 客户端的构建参数
type reqClientOptions struct {
	ProxyURL              string                  // 代理 URL（支持 http/https/socks5）
	Timeout               time.Duration           // 请求超时时间
	Impersonate           bool                    // 是否模拟 Chrome 浏览器指纹
	ForceHTTP2            bool                    // 是否强制使用 HTTP/2
	TLSProfile            *tlsfingerprint.Profile // 可选的受控 TLS ClientHello 画像
	PrivacyBrowserPersona PrivacyBrowserPersona   // privacy 专用的浏览器画像
	PrivacyPlatform       string                  // privacy 画像声明的运行平台
	PrivacyAcceptLanguage string                  // privacy 画像声明的语言偏好
}

// PrivacyBrowserPersona 是 privacy 端点可独立灰度和回滚的浏览器画像。
type PrivacyBrowserPersona string

const (
	PrivacyBrowserPersonaChrome133       PrivacyBrowserPersona = "chrome_133_xhr"
	PrivacyBrowserPersonaLegacyChrome120 PrivacyBrowserPersona = "legacy_chrome_120"
)

// PrivacyReqClientProfile 冻结一次 privacy 客户端构造所需的画像字段。
type PrivacyReqClientProfile struct {
	Persona        PrivacyBrowserPersona
	Platform       string
	AcceptLanguage string
}

// sharedReqClients 存储按配置参数缓存的 req 客户端实例
//
// 性能优化说明：
// 原实现在每次 OAuth 刷新时都创建新的 req.Client：
// 1. claude_oauth_service.go: 每次刷新创建新客户端
// 2. openai_oauth_service.go: 每次刷新创建新客户端
// 3. gemini_oauth_client.go: 每次刷新创建新客户端
//
// 新实现使用 sync.Map 缓存客户端：
// 1. 相同配置（代理+超时+模拟设置）复用同一客户端
// 2. 复用底层连接池，减少 TLS 握手开销
// 3. LoadOrStore 保证并发安全，避免重复创建
var sharedReqClients sync.Map

// getSharedReqClient 获取共享的 req 客户端实例
// 性能优化：相同配置复用同一客户端，避免重复创建
func getSharedReqClient(opts reqClientOptions) (*req.Client, error) {
	key := buildReqClientKey(opts)
	if cached, ok := sharedReqClients.Load(key); ok {
		if c, ok := cached.(*req.Client); ok {
			return c, nil
		}
	}

	client := req.C().SetTimeout(opts.Timeout)
	if opts.TLSProfile != nil && opts.TLSProfile.Transport.DisableCompression {
		// TLS/H1 画像声明的是完整线形；若仍让 req 自动追加 Accept-Encoding，
		// strict H1 会在写出前拒绝画像外字段。该 transport 选项必须与拨号画像同源。
		client = client.DisableCompression()
	}
	if opts.ForceHTTP2 {
		client = client.EnableForceHTTP2()
	}
	if opts.Impersonate {
		client = client.ImpersonateChrome()
	}
	if opts.PrivacyBrowserPersona != "" {
		client = applyPrivacyBrowserPersona(client, PrivacyReqClientProfile{
			Persona:        opts.PrivacyBrowserPersona,
			Platform:       opts.PrivacyPlatform,
			AcceptLanguage: opts.PrivacyAcceptLanguage,
		})
	}
	trimmed, parsedProxy, err := proxyurl.Parse(opts.ProxyURL)
	if err != nil {
		return nil, err
	}
	proxyHandledByTLSDialer := false
	if opts.TLSProfile != nil {
		switch {
		case parsedProxy == nil:
			client.SetDialTLS(tlsfingerprint.NewDialer(opts.TLSProfile, nil).DialTLSContext)
		case parsedProxy.Scheme == "socks5" || parsedProxy.Scheme == "socks5h":
			client.SetDialTLS(tlsfingerprint.NewSOCKS5ProxyDialer(opts.TLSProfile, parsedProxy).DialTLSContext)
			proxyHandledByTLSDialer = true
		case parsedProxy.Scheme == "http" || parsedProxy.Scheme == "https":
			client.SetDialTLS(tlsfingerprint.NewHTTPProxyDialer(opts.TLSProfile, parsedProxy).DialTLSContext)
			proxyHandledByTLSDialer = true
		default:
			return nil, fmt.Errorf("unsupported TLS fingerprint proxy scheme: %s", parsedProxy.Scheme)
		}
	}
	if trimmed != "" && !proxyHandledByTLSDialer {
		client.SetProxyURL(trimmed)
	}
	client = instrumentReqClientWithProfile(client, opts.TLSProfile)

	actual, _ := sharedReqClients.LoadOrStore(key, client)
	if c, ok := actual.(*req.Client); ok {
		return c, nil
	}
	return client, nil
}

func instrumentReqClient(client *req.Client) *req.Client {
	return instrumentReqClientWithGuard(client, nil, nil)
}

// instrumentReqClientWithProfile 让 Guard 与画像声明的 wire 形态在同一条链上生效。
func instrumentReqClientWithProfile(client *req.Client, profile *tlsfingerprint.Profile) *req.Client {
	return instrumentReqClientWithGuard(client, nil, profile)
}

func buildReqClientKey(opts reqClientOptions) string {
	baseKey := fmt.Sprintf("%s|%s|%t|%t",
		strings.TrimSpace(opts.ProxyURL),
		opts.Timeout.String(),
		opts.Impersonate,
		opts.ForceHTTP2,
	)
	if opts.PrivacyBrowserPersona != "" {
		baseKey = fmt.Sprintf("%s|privacy=%s|platform=%s|language=%s",
			baseKey,
			opts.PrivacyBrowserPersona,
			opts.PrivacyPlatform,
			opts.PrivacyAcceptLanguage,
		)
	}
	if opts.TLSProfile == nil {
		return baseKey
	}
	return fmt.Sprintf(
		"%s|tls=%s|disable_compression=%t",
		baseKey,
		opts.TLSProfile.Name,
		opts.TLSProfile.Transport.DisableCompression,
	)
}

// CreatePrivacyReqClient 按已冻结的画像创建或复用 privacy 客户端。
// 可选参数用于生产灰度与 legacy 回滚；不传时保持 Chrome 133/XHR 默认值。
// 发送工厂仍保持在 bootstrap 已审核的函数边界，避免灰度参数把同一
// 物理发送栈误报为新增 Sink。
func CreatePrivacyReqClient(proxyURL string, profiles ...PrivacyReqClientProfile) (*req.Client, error) {
	profile := PrivacyReqClientProfile{Persona: PrivacyBrowserPersonaChrome133}
	if len(profiles) > 0 {
		profile = profiles[0]
	}
	if profile.Persona == "" {
		profile.Persona = PrivacyBrowserPersonaChrome133
	}
	profile.Platform = ResolvePrivacyBrowserPlatform(profile.Platform)
	profile.AcceptLanguage = ResolvePrivacyAcceptLanguage(profile.AcceptLanguage)
	return getSharedReqClient(reqClientOptions{
		ProxyURL:              proxyURL,
		Timeout:               30 * time.Second,
		PrivacyBrowserPersona: profile.Persona,
		PrivacyPlatform:       profile.Platform,
		PrivacyAcceptLanguage: profile.AcceptLanguage,
	})
}

// privacyReqClientWithProfileFactory 仅是对已审核工厂的类型安全别名。
// 显式画像入口通过它复用同一工厂，不产生第二个物理发送候选。
var privacyReqClientWithProfileFactory = CreatePrivacyReqClient

// CreatePrivacyReqClientWithProfile 保留旧调用方的显式画像入口。
func CreatePrivacyReqClientWithProfile(proxyURL string, profile PrivacyReqClientProfile) (*req.Client, error) {
	return privacyReqClientWithProfileFactory(proxyURL, profile)
}

// SelectPrivacyBrowserPersona 使用稳定分桶选择 privacy 画像。
// 空分桶键始终回落 legacy，避免无身份请求被随机扩大灰度。
func SelectPrivacyBrowserPersona(enabled bool, canaryPercent int, rolloutKey string) PrivacyBrowserPersona {
	if !enabled || canaryPercent <= 0 || strings.TrimSpace(rolloutKey) == "" {
		return PrivacyBrowserPersonaLegacyChrome120
	}
	if canaryPercent >= 100 {
		return PrivacyBrowserPersonaChrome133
	}
	digest := sha256.Sum256([]byte(strings.TrimSpace(rolloutKey)))
	bucket := int(binary.BigEndian.Uint64(digest[:8]) % 100)
	if bucket < canaryPercent {
		return PrivacyBrowserPersonaChrome133
	}
	return PrivacyBrowserPersonaLegacyChrome120
}

// ResolvePrivacyBrowserPlatform 将配置或当前部署环境解析为 Client Hints 平台。
func ResolvePrivacyBrowserPlatform(configured string) string {
	platform := strings.ToLower(strings.TrimSpace(configured))
	if platform == "" || platform == "auto" {
		switch runtime.GOOS {
		case "darwin":
			platform = "macos"
		case "windows":
			platform = "windows"
		default:
			platform = "linux"
		}
	}
	switch platform {
	case "macos", "windows", "linux":
		return platform
	default:
		return "linux"
	}
}

// ResolvePrivacyAcceptLanguage 优先使用部署配置，否则从进程 locale 推导。
func ResolvePrivacyAcceptLanguage(configured string) string {
	if value := sanitizePrivacyLocaleHeader(configured); value != "" {
		return value
	}
	for _, key := range []string{"LC_ALL", "LC_MESSAGES", "LANG"} {
		if value := privacyLocaleToAcceptLanguage(os.Getenv(key)); value != "" {
			return value
		}
	}
	return "en-US,en;q=0.9"
}

func privacyLocaleToAcceptLanguage(locale string) string {
	locale = strings.TrimSpace(locale)
	if locale == "" || strings.EqualFold(locale, "C") || strings.EqualFold(locale, "POSIX") {
		return ""
	}
	if index := strings.IndexAny(locale, ".@"); index >= 0 {
		locale = locale[:index]
	}
	locale = strings.ReplaceAll(locale, "_", "-")
	parts := strings.Split(locale, "-")
	if len(parts) == 0 || len(parts[0]) < 2 || len(parts[0]) > 3 {
		return ""
	}
	language := strings.ToLower(parts[0])
	for _, char := range language {
		if char < 'a' || char > 'z' {
			return ""
		}
	}
	if len(parts) > 1 && len(parts[1]) == 2 {
		region := strings.ToUpper(parts[1])
		for _, char := range region {
			if char < 'A' || char > 'Z' {
				return ""
			}
		}
		return fmt.Sprintf("%s-%s,%s;q=0.9,en;q=0.8", language, region, language)
	}
	return fmt.Sprintf("%s,en;q=0.8", language)
}

func sanitizePrivacyLocaleHeader(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > 128 || strings.ContainsAny(value, "\r\n") {
		return ""
	}
	return value
}

func applyPrivacyBrowserPersona(client *req.Client, profile PrivacyReqClientProfile) *req.Client {
	client = client.ImpersonateChrome()
	if profile.Persona == PrivacyBrowserPersonaLegacyChrome120 {
		return client
	}

	platform := ResolvePrivacyBrowserPlatform(profile.Platform)
	acceptLanguage := ResolvePrivacyAcceptLanguage(profile.AcceptLanguage)
	client = client.
		SetTLSFingerprint(utls.HelloChrome_133).
		EnableAutoDecompress()
	for _, header := range []string{
		"Pragma",
		"Cache-Control",
		"Upgrade-Insecure-Requests",
		"Sec-Fetch-User",
	} {
		client.Headers.Del(header)
	}
	client.SetCommonHeaderOrder(
		"sec-ch-ua",
		"sec-ch-ua-mobile",
		"sec-ch-ua-platform",
		"user-agent",
		"accept",
		"sec-fetch-site",
		"sec-fetch-mode",
		"sec-fetch-dest",
		"referer",
		"accept-encoding",
		"accept-language",
		"cookie",
	)
	client.SetCommonHeaders(map[string]string{
		"sec-ch-ua":          `"Not_A Brand";v="8", "Chromium";v="133", "Google Chrome";v="133"`,
		"sec-ch-ua-mobile":   "?0",
		"sec-ch-ua-platform": fmt.Sprintf("%q", privacyPlatformClientHint(platform)),
		"user-agent":         privacyChrome133UserAgent(platform),
		"accept":             "application/json",
		"sec-fetch-site":     "same-origin",
		"sec-fetch-mode":     "cors",
		"sec-fetch-dest":     "empty",
		"accept-encoding":    "gzip, deflate, br, zstd",
		"accept-language":    acceptLanguage,
	})
	return client
}

func privacyPlatformClientHint(platform string) string {
	switch platform {
	case "macos":
		return "macOS"
	case "windows":
		return "Windows"
	default:
		return "Linux"
	}
}

func privacyChrome133UserAgent(platform string) string {
	var system string
	switch platform {
	case "macos":
		system = "Macintosh; Intel Mac OS X 10_15_7"
	case "windows":
		system = "Windows NT 10.0; Win64; x64"
	default:
		system = "X11; Linux x86_64"
	}
	return fmt.Sprintf("Mozilla/5.0 (%s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36", system)
}
