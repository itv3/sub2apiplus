package service

import (
	"context"
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

type httpUpstreamCookieJarContextKey struct{}

// WithHTTPUpstreamCookieJar 将账号级 Cookie jar 绑定到一次上游请求。
// repository 只负责把它安装到实际 http.Client，不需要感知账号凭据。
func WithHTTPUpstreamCookieJar(ctx context.Context, jar http.CookieJar) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if jar == nil {
		return ctx
	}
	return context.WithValue(ctx, httpUpstreamCookieJarContextKey{}, jar)
}

// HTTPUpstreamCookieJarFromContext 返回本次请求绑定的账号级 Cookie jar。
func HTTPUpstreamCookieJarFromContext(ctx context.Context) http.CookieJar {
	if ctx == nil {
		return nil
	}
	jar, _ := ctx.Value(httpUpstreamCookieJarContextKey{}).(http.CookieJar)
	return jar
}

// HTTPUpstream 上游 HTTP 请求接口
// 用于向上游 API（Claude、OpenAI、Gemini 等）发送请求
type HTTPUpstream interface {
	// Do 执行 HTTP 请求（不启用 TLS 指纹）
	Do(req *http.Request, proxyURL string, accountID int64, accountConcurrency int) (*http.Response, error)

	// DoWithTLS 执行带 TLS 指纹伪装的 HTTP 请求
	//
	// profile 参数:
	//   - nil: 不启用 TLS 指纹，行为与 Do 方法相同
	//   - non-nil: 使用指定的 Profile 进行 TLS 指纹伪装
	//
	// Profile 由调用方通过 TLSFingerprintProfileService 解析后传入，
	// 支持按账号绑定的数据库 profile 或内置默认 profile。
	DoWithTLS(req *http.Request, proxyURL string, accountID int64, accountConcurrency int, profile *tlsfingerprint.Profile) (*http.Response, error)
}
