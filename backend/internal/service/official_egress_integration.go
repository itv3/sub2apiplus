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
	"github.com/google/uuid"
)

var defaultOfficialEgressProfileResolver OfficialEgressProfileResolver = DefaultOfficialEgressProfileResolver{}

const officialEgressInvocationGinKey = "official_egress_invocation_id"

// officialEgressInvocationIDForRequest 为一次入站上层调用生成稳定 ID。
// Gin Context 会贯穿同一调用的账号切换与 retry，因此所有 rebuild 得到同一值；
// 新入站请求拥有新的 Context，自然不会跨调用复用 HTTP Client/TCP。
func officialEgressInvocationIDForRequest(c *gin.Context) (string, error) {
	if c == nil {
		return "", errors.New("official egress invocation requires ingress context")
	}
	if value, exists := c.Get(officialEgressInvocationGinKey); exists {
		invocationID, _ := value.(string)
		if invocationID = strings.TrimSpace(invocationID); invocationID != "" {
			return invocationID, nil
		}
		return "", errors.New("official egress invocation identity is invalid")
	}
	invocationID := uuid.NewString()
	c.Set(officialEgressInvocationGinKey, invocationID)
	return invocationID, nil
}

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
	inboundEndpoint := canonicalOfficialEgressInboundEndpoint(c.Request.URL.Path)
	if !supportsOfficialEgressHTTPProfile(targetPlatform, inboundEndpoint) {
		return req, nil
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	modelCapabilities := openAIModelCapabilitiesFromContext(req.Context())
	invocationID, err := officialEgressInvocationIDForRequest(c)
	if err != nil {
		return nil, err
	}
	var codexRuntimeState officialCodex0145RuntimeState
	if strings.EqualFold(targetPlatform, PlatformOpenAI) {
		codexRuntimeState, err = resolveBoundOrIngressOfficialCodex0145RuntimeState(
			req.Context(),
			c,
			account,
		)
		if err != nil {
			return nil, err
		}
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:                         account.ID,
		TargetPlatform:                    targetPlatform,
		InboundEndpoint:                   inboundEndpoint,
		Transport:                         OfficialEgressTransportHTTP,
		UpstreamHost:                      req.URL.Host,
		ProfileVersion:                    version,
		ProfileMode:                       mode,
		AccountType:                       account.Type,
		ProxyID:                           proxyID,
		ResponsesLite:                     modelCapabilities.UseResponsesLite,
		ParallelTools:                     modelCapabilities.SupportsParallelToolCalls,
		DefaultReasoningLevel:             modelCapabilities.DefaultReasoningLevel,
		DefaultReasoningSummary:           modelCapabilities.DefaultReasoningSummary,
		SupportsReasoningSummaryParameter: modelCapabilities.SupportsReasoningSummaryParameter,
		ReasoningDefaultsKnown:            modelCapabilities.ReasoningDefaultsKnown,
		InvocationID:                      invocationID,
		CodexRuntimeState:                 codexRuntimeState,
	})
	egressContext.cookieJar = HTTPUpstreamCookieJarFromContext(req.Context())
	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		inboundEndpoint,
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
		normalizedEndpoint := canonicalOfficialEgressInboundEndpoint(endpoint)
		return normalizedEndpoint == "/v1/messages" ||
			normalizedEndpoint == "/v1/responses" ||
			normalizedEndpoint == "/v1/chat/completions"
	case PlatformOpenAI:
		normalizedEndpoint := canonicalOfficialEgressInboundEndpoint(endpoint)
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
	inboundEndpoint := canonicalOfficialEgressInboundEndpoint(c.Request.URL.Path)
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	modelCapabilities := openAIModelCapabilitiesFromContext(ctx)
	invocationID, err := officialEgressInvocationIDForRequest(c)
	if err != nil {
		return nil, err
	}
	var codexRuntimeState officialCodex0145RuntimeState
	if strings.EqualFold(account.Platform, PlatformOpenAI) {
		codexRuntimeState, err = resolveBoundOrIngressOfficialCodex0145RuntimeState(
			ctx,
			c,
			account,
		)
		if err != nil {
			return nil, err
		}
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:                         account.ID,
		TargetPlatform:                    account.Platform,
		InboundEndpoint:                   inboundEndpoint,
		Transport:                         OfficialEgressTransportWebSocket,
		UpstreamHost:                      parsedURL.Host,
		ProfileVersion:                    version,
		ProfileMode:                       mode,
		AccountType:                       account.Type,
		ProxyID:                           proxyID,
		ResponsesLite:                     modelCapabilities.UseResponsesLite,
		ParallelTools:                     modelCapabilities.SupportsParallelToolCalls,
		DefaultReasoningLevel:             modelCapabilities.DefaultReasoningLevel,
		DefaultReasoningSummary:           modelCapabilities.DefaultReasoningSummary,
		SupportsReasoningSummaryParameter: modelCapabilities.SupportsReasoningSummaryParameter,
		ReasoningDefaultsKnown:            modelCapabilities.ReasoningDefaultsKnown,
		InvocationID:                      invocationID,
		CodexRuntimeState:                 codexRuntimeState,
	})
	egressContext.cookieJar = HTTPUpstreamCookieJarFromContext(ctx)
	profile, err := defaultOfficialEgressProfileResolver.ResolveWebSocketProfile(
		egressContext,
		account,
		inboundEndpoint,
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
	if !strings.EqualFold(req.URL.Scheme, "https") {
		return fmt.Errorf("official egress rejected HTTP scheme: %s", req.URL.Scheme)
	}
	if normalizeOfficialEgressHost(req.URL.Host) != egressContext.upstreamHost {
		return errors.New("official egress final HTTP host conflicts with resolved context")
	}
	expectedMethod := http.MethodPost
	expectedPath := expectedOfficialEgressUpstreamPath(egressContext)
	if egressContext.targetPlatform == PlatformOpenAI {
		endpoint, err := resolveCodex0145Endpoint(
			egressContext.profileVersion,
			codex0145EndpointID(egressContext.codexEndpointProfileID),
		)
		if err != nil {
			return err
		}
		expectedMethod = endpoint.Method
		if !officialCodex0145HostMatches(endpoint.Host, egressContext.upstreamHost) {
			return errors.New("Codex 端点画像 host 与上下文冲突")
		}
		if strings.TrimSpace(egressContext.codexRequestedEndpointID) != "" {
			if req.Method != expectedMethod {
				return fmt.Errorf("official egress rejected HTTP method: %s", req.Method)
			}
			if err := officialCodex0145ValidateEndpointTarget(endpoint, req.URL); err != nil {
				return err
			}
			return nil
		}
		if req.URL.RawQuery != "" || len(endpoint.Query) != 0 {
			return errors.New("Codex Responses 端点不允许 provider 级 query")
		}
	}
	if req.Method != expectedMethod {
		return fmt.Errorf("official egress rejected HTTP method: %s", req.Method)
	}
	if normalizeOfficialEgressEndpoint(req.URL.Path) != expectedPath {
		return fmt.Errorf("official egress rejected final HTTP path: %s", req.URL.Path)
	}
	return nil
}

func officialCodex0145ValidateEndpointTarget(endpoint officialCodexEndpointProfile, target *url.URL) error {
	if target == nil {
		return errors.New("Codex 端点目标为空")
	}
	if !officialCodex0145HostMatches(endpoint.Host, target.Hostname()) {
		return fmt.Errorf("Codex 端点 %s 拒绝 host：%s", endpoint.ID, target.Hostname())
	}
	if !endpoint.HostFromResponse && !officialCodex0145PathMatches(endpoint.Path, target.EscapedPath()) {
		return fmt.Errorf("Codex 端点 %s 拒绝 path：%s", endpoint.ID, target.EscapedPath())
	}
	values, err := url.ParseQuery(target.RawQuery)
	if err != nil {
		return fmt.Errorf("Codex 端点 %s query 无效：%w", endpoint.ID, err)
	}
	if endpoint.HostFromResponse {
		if len(endpoint.Query) == 1 && endpoint.Query[0].Name == "*" && endpoint.Query[0].Required && target.RawQuery == "" {
			return fmt.Errorf("Codex 端点 %s 缺少服务端签名 query", endpoint.ID)
		}
		return nil
	}
	allowed := make(map[string]officialCodexQueryField, len(endpoint.Query))
	for _, field := range endpoint.Query {
		allowed[field.Name] = field
		actual := values[field.Name]
		if field.Required && len(actual) != 1 {
			return fmt.Errorf("Codex 端点 %s 缺少或重复 query：%s", endpoint.ID, field.Name)
		}
		if len(actual) > 1 {
			return fmt.Errorf("Codex 端点 %s query 重复：%s", endpoint.ID, field.Name)
		}
		if field.Source == officialCodexSourceConstant && len(actual) == 1 && actual[0] != field.Value {
			return fmt.Errorf("Codex 端点 %s 固定 query 被改写：%s", endpoint.ID, field.Name)
		}
	}
	for name := range values {
		if _, ok := allowed[name]; !ok {
			return fmt.Errorf("Codex 端点 %s 不允许 query：%s", endpoint.ID, name)
		}
	}
	return nil
}

func officialCodex0145PathMatches(template, escapedPath string) bool {
	if template == escapedPath {
		return true
	}
	templateParts := strings.Split(strings.TrimPrefix(template, "/"), "/")
	pathParts := strings.Split(strings.TrimPrefix(escapedPath, "/"), "/")
	if len(templateParts) != len(pathParts) {
		return false
	}
	for index, part := range templateParts {
		if strings.HasPrefix(part, "{") && strings.HasSuffix(part, "}") {
			if pathParts[index] == "" {
				return false
			}
			continue
		}
		if part != pathParts[index] {
			return false
		}
	}
	return true
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
	if egressContext.targetPlatform == PlatformOpenAI {
		endpoint, err := resolveCodex0145Endpoint(
			egressContext.profileVersion,
			codex0145EndpointID(egressContext.codexEndpointProfileID),
		)
		if err != nil {
			return err
		}
		if strings.TrimSpace(egressContext.codexRequestedEndpointID) != "" {
			if endpoint.Upgrade != "websocket" {
				return errors.New("Codex WebSocket 上下文绑定了非 WS 端点")
			}
			return officialCodex0145ValidateEndpointTarget(endpoint, target)
		}
		if endpoint.Upgrade != "websocket" ||
			normalizeOfficialEgressHost(endpoint.Host) != egressContext.upstreamHost ||
			target.RawQuery != "" || len(endpoint.Query) != 0 {
			return errors.New("Codex WebSocket 目标与端点画像冲突")
		}
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
		endpointID := strings.TrimSpace(egressContext.codexEndpointProfileID)
		if endpointID == "" {
			return ""
		}
		endpoint, err := resolveCodex0145Endpoint(
			egressContext.profileVersion,
			codex0145EndpointID(endpointID),
		)
		if err != nil {
			return ""
		}
		return endpoint.Path
	default:
		return ""
	}
}
