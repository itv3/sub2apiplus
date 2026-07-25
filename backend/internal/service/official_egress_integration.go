package service

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/gin-gonic/gin"
)

var defaultOfficialEgressProfileResolver OfficialEgressProfileResolver = DefaultOfficialEgressProfileResolver{}

// resetOfficialEgressRequestBody 在最终修正器改写 Body 后同步请求长度与重放函数。
// HTTP 重试会重新构建请求，但重定向和诊断代码仍可能使用 GetBody，因此三者必须一致。
func resetOfficialEgressRequestBody(req *http.Request, body []byte) {
	req.Body = io.NopCloser(bytes.NewReader(body))
	req.ContentLength = int64(len(body))
	req.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

// attachOfficialEgressHTTPContext 在每次 HTTP 请求重建后解析一次上下文。
//
// 该函数故意位于 buildUpstreamRequest 的末端：重试或账号切换会创建新的
// http.Request，因此不会继承前一账号的身份状态。不适用该画像的账号原样返回。
func attachOfficialEgressHTTPContext(
	req *http.Request,
	c *gin.Context,
	account *Account,
	targetPlatform string,
) (*http.Request, error) {
	if req == nil {
		return nil, errors.New("official egress requires HTTP request")
	}
	enabled, version, err := resolveOfficialEgressAccountProfile(account)
	if err != nil {
		return nil, err
	}
	if !enabled {
		return req, nil
	}
	if c == nil || c.Request == nil {
		return nil, errors.New("official egress is enabled but ingress request is unavailable")
	}
	if !supportsOfficialEgressHTTPProfile(targetPlatform, c.Request.URL.Path) {
		return req, nil
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  targetPlatform,
		InboundEndpoint: c.Request.URL.Path,
		Transport:       OfficialEgressTransportHTTP,
		UpstreamHost:    req.URL.Host,
		ProfileVersion:  version,
		AccountType:     account.Type,
		ProxyID:         proxyID,
	})
	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		c.Request.URL.Path,
	)
	if err != nil {
		return nil, err
	}
	if err := ValidateOfficialEgressFinalState(egressContext, profile); err != nil {
		return nil, err
	}
	return req.WithContext(WithOfficialEgressContext(req.Context(), egressContext)), nil
}

// supportsOfficialEgressHTTPProfile 限定当前版本真正覆盖的 HTTP 入口。
//
// OpenAI 的 Chat Completions 与 Messages 入口会在业务层转换为 Responses
// 请求后再出站；Anthropic 的 Responses 与 Chat Completions 入口也会在业务层
// 转换为 Messages 请求后再出站。因此，这些跨协议入口必须应用目标平台对应的
// 官方画像。模型清单等不会产生模型请求的共用入口仍保持原有行为。
func supportsOfficialEgressHTTPProfile(targetPlatform, endpoint string) bool {
	switch strings.ToLower(strings.TrimSpace(targetPlatform)) {
	case PlatformAnthropic:
		normalizedEndpoint := normalizeOfficialEgressEndpoint(endpoint)
		return normalizedEndpoint == "/v1/messages" ||
			normalizedEndpoint == "/v1/responses" ||
			normalizedEndpoint == "/v1/chat/completions"
	case PlatformOpenAI:
		normalizedEndpoint := normalizeOfficialEgressEndpoint(endpoint)
		return normalizedEndpoint == "/v1/responses" ||
			normalizedEndpoint == "/v1/responses/compact" ||
			normalizedEndpoint == "/v1/chat/completions" ||
			normalizedEndpoint == "/v1/messages"
	default:
		return false
	}
}

// resolveAnthropicOfficialEgressOwnership 判断后置 Finalizer 是否独占最终画像字段。
// 兼容层仍处理协议与请求语义，但在返回 true 时不得写入 system/cache/metadata。
func resolveAnthropicOfficialEgressOwnership(
	account *Account,
	c *gin.Context,
) (bool, error) {
	if account == nil ||
		account.Platform != PlatformAnthropic ||
		c == nil ||
		c.Request == nil ||
		!supportsOfficialEgressHTTPProfile(PlatformAnthropic, c.Request.URL.Path) {
		return false, nil
	}
	enabled, _, err := resolveOfficialEgressAccountProfile(account)
	return enabled, err
}

// attachOfficialEgressWebSocketContext 在握手前冻结账号、画像、代理和 Host。
//
// 冻结后的上下文绑定到整个 WS 会话；任何需要切换账号或画像的流程都必须重新拨号。
func attachOfficialEgressWebSocketContext(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	targetURL string,
	firstPayload []byte,
) (context.Context, error) {
	enabled, version, err := resolveOfficialEgressAccountProfile(account)
	if err != nil {
		return nil, err
	}
	if !enabled {
		return ctx, nil
	}
	if c == nil || c.Request == nil {
		return nil, errors.New("official egress is enabled but ingress request is unavailable")
	}
	parsedURL, err := url.Parse(targetURL)
	if err != nil {
		return nil, err
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  account.Platform,
		InboundEndpoint: c.Request.URL.Path,
		Transport:       OfficialEgressTransportWebSocket,
		UpstreamHost:    parsedURL.Host,
		ProfileVersion:  version,
		AccountType:     account.Type,
		ProxyID:         proxyID,
	})
	profile, err := defaultOfficialEgressProfileResolver.ResolveWebSocketProfile(
		egressContext,
		account,
		c.Request.URL.Path,
	)
	if err != nil {
		return nil, err
	}
	if err := prepareOpenAIOfficialEgressWSContext(
		egressContext,
		c,
		account,
		firstPayload,
	); err != nil {
		return nil, err
	}
	frozenContext, err := egressContext.Freeze()
	if err != nil {
		return nil, err
	}
	if err := ValidateOfficialEgressFinalState(frozenContext, profile); err != nil {
		return nil, err
	}
	logOfficialEgressProfileResolved(frozenContext, profile)
	return WithOfficialEgressContext(ctx, frozenContext), nil
}
