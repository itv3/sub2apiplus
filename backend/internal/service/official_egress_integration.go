package service

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/config"
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
	cfgs ...*config.Config,
) (*http.Request, error) {
	if req == nil {
		return nil, errors.New("official egress requires HTTP request")
	}
	mode := officialClientProfileModeActive
	if len(cfgs) > 0 {
		mode = officialClientProfileModeFromConfig(cfgs[0])
	}
	enabled, version, err := resolveOfficialEgressAccountProfile(account, mode)
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
		ProfileMode:     mode,
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
	requestContext := WithHTTPUpstreamRedirectsDisabled(req.Context())
	requestContext = WithOfficialEgressContext(requestContext, egressContext)
	req = req.WithContext(requestContext)
	if err := validateOfficialEgressHTTPRequest(req, egressContext); err != nil {
		return nil, err
	}
	return req, nil
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
	cfgs ...*config.Config,
) (bool, error) {
	if account == nil ||
		account.Platform != PlatformAnthropic ||
		c == nil ||
		c.Request == nil ||
		!supportsOfficialEgressHTTPProfile(PlatformAnthropic, c.Request.URL.Path) {
		return false, nil
	}
	mode := officialClientProfileModeActive
	if len(cfgs) > 0 {
		mode = officialClientProfileModeFromConfig(cfgs[0])
	}
	enabled, _, err := resolveOfficialEgressAccountProfile(account, mode)
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
	cfgs ...*config.Config,
) (context.Context, error) {
	mode := officialClientProfileModeActive
	if len(cfgs) > 0 {
		mode = officialClientProfileModeFromConfig(cfgs[0])
	}
	enabled, version, err := resolveOfficialEgressAccountProfile(account, mode)
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
		ProfileMode:     mode,
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
	if err := validateOfficialEgressWebSocketTarget(parsedURL, egressContext); err != nil {
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

// validateOfficialEgressHTTPRequest 在实际发送前绑定 Method、Host 和最终上游 Path。
func validateOfficialEgressHTTPRequest(req *http.Request, egressContext *OfficialEgressContext) error {
	if req == nil || req.URL == nil || egressContext == nil {
		return errors.New("official egress final HTTP target is unavailable")
	}
	if req.Method != http.MethodPost {
		return fmt.Errorf("official egress rejected HTTP method: %s", req.Method)
	}
	if !strings.EqualFold(req.URL.Scheme, "https") {
		return fmt.Errorf("official egress rejected HTTP scheme: %s", req.URL.Scheme)
	}
	if normalizeOfficialEgressHost(req.URL.Host) != egressContext.upstreamHost {
		return errors.New("official egress final HTTP host conflicts with resolved context")
	}
	expectedPath := expectedOfficialEgressUpstreamPath(egressContext)
	if normalizeOfficialEgressEndpoint(req.URL.Path) != expectedPath {
		return fmt.Errorf("official egress rejected final HTTP path: %s", req.URL.Path)
	}
	return nil
}

func validateOfficialEgressWebSocketTarget(target *url.URL, egressContext *OfficialEgressContext) error {
	if target == nil || egressContext == nil {
		return errors.New("official egress final WebSocket target is unavailable")
	}
	if !strings.EqualFold(target.Scheme, "wss") {
		return fmt.Errorf("official egress rejected WebSocket scheme: %s", target.Scheme)
	}
	if normalizeOfficialEgressHost(target.Host) != egressContext.upstreamHost {
		return errors.New("official egress final WebSocket host conflicts with resolved context")
	}
	if normalizeOfficialEgressEndpoint(target.Path) != expectedOfficialEgressUpstreamPath(egressContext) {
		return fmt.Errorf("official egress rejected final WebSocket path: %s", target.Path)
	}
	return nil
}

func expectedOfficialEgressUpstreamPath(egressContext *OfficialEgressContext) string {
	if egressContext == nil {
		return ""
	}
	switch egressContext.targetPlatform {
	case PlatformAnthropic:
		return "/v1/messages"
	case PlatformOpenAI:
		if egressContext.transport == OfficialEgressTransportHTTP &&
			egressContext.inboundEndpoint == "/v1/responses/compact" {
			return "/backend-api/codex/responses/compact"
		}
		return "/backend-api/codex/responses"
	default:
		return ""
	}
}
