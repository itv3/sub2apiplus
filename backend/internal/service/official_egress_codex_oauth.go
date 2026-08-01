package service

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// CodexOAuthExchangeProfile 保存授权码换码阶段所需的最小官方客户端身份。
//
// 授权码换码属于账号接入流程，目前不在严格端点画像的证据闭集内，因此这里只复用
// active Codex 版本的进程身份和 TLS ClientHello，不启用端点级 H1 线形校验。
// 调用方只消费这个稳定接口，不持有版本号或具体 TLS 参数。
type CodexOAuthExchangeProfile struct {
	UserAgent  string
	Originator string
	TLSProfile *tlsfingerprint.Profile
}

// ResolveActiveCodexOAuthExchangeProfile 按 release registry 的 active 版本解析授权码
// 换码身份，并派生一份独立的 TLS-only 画像。
func ResolveActiveCodexOAuthExchangeProfile() (CodexOAuthExchangeProfile, error) {
	resolved, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil {
		return CodexOAuthExchangeProfile{}, err
	}
	version := strings.TrimSpace(resolved.Build.Version)
	userAgent := strings.TrimSpace(resolved.Build.UserAgent)
	originator := strings.TrimSpace(resolved.Build.Originator)
	if version == "" || userAgent == "" || originator == "" {
		return CodexOAuthExchangeProfile{}, fmt.Errorf("active Codex OAuth 换码身份不完整")
	}

	tlsProfile, err := resolveOfficialCodexDefaultTLSProfile(
		version,
		officialCodexTransportProtocolHTTP1,
	)
	if err != nil {
		return CodexOAuthExchangeProfile{}, fmt.Errorf("解析 active Codex OAuth 换码 TLS 画像：%w", err)
	}
	// 必须使用独立名称隔离 req.Client 缓存；缓存键会消费画像名称，同名会导致
	// TLS-only 客户端与 refresh 的 strict H1 客户端互相复用。
	tlsProfile.Name = strings.TrimSpace(tlsProfile.Name) + " | oauth-authorization-code-tls-only"
	tlsProfile.Transport.H1HeaderOrders = nil
	tlsProfile.Transport.StrictH1Wire = false

	return CodexOAuthExchangeProfile{
		UserAgent:  userAgent,
		Originator: originator,
		TLSProfile: tlsProfile,
	}, nil
}

// BuildActiveCodexOAuthRefreshRequest 为尚未绑定账号的 OAuth 刷新阶段提供稳定入口。
// 该阶段没有 Account，不能构造账号级 OfficialEgressContext；但 URL、表单闭集和顺序、
// header 闭集和线序以及 TLS/H1 仍全部由 active 版本画像编译。
func BuildActiveCodexOAuthRefreshRequest(
	ctx context.Context,
	clientID string,
	refreshToken string,
	scope string,
) (*http.Request, *tlsfingerprint.Profile, error) {
	values := make(url.Values, 4)
	values.Set("client_id", strings.TrimSpace(clientID))
	values.Set("grant_type", "refresh_token")
	values.Set("refresh_token", strings.TrimSpace(refreshToken))
	values.Set("scope", strings.TrimSpace(scope))
	body, err := validateAndOrderActiveCodexFormBody(
		officialCodexEndpointOAuthRefresh,
		values,
	)
	if err != nil {
		return nil, nil, err
	}
	target, err := buildActiveCodexEndpointURL(
		officialCodexEndpointOAuthRefresh,
		officialCodex0145EndpointURLInput{},
	)
	if err != nil {
		return nil, nil, err
	}
	request, err := http.NewRequestWithContext(
		WithHTTPUpstreamRedirectsDisabled(ctx),
		http.MethodPost,
		target.String(),
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("构造 Codex OAuth refresh 请求：%w", err)
	}
	request.Host = target.Host
	profile, err := resolveActiveCodexVersionProfile()
	if err != nil {
		return nil, nil, err
	}
	runtimeState, bound, err := officialCodex0145RuntimeStateFromContext(ctx)
	if err != nil {
		return nil, nil, err
	}
	if !bound {
		runtimeState = defaultOfficialCodex0145RuntimeState()
	}
	userAgent, err := profile.RenderUserAgentWithTerminal(
		runtimeState.SurfaceID,
		runtimeState.TerminalToken,
		runtimeState.UserAgentSuffixEnabled,
	)
	if err != nil {
		return nil, nil, err
	}
	request.Header.Set("user-agent", userAgent)
	if _, err := applyActiveCodexHeaderContract(
		officialCodexEndpointOAuthRefresh,
		request.Header,
		nil,
	); err != nil {
		return nil, nil, err
	}
	tlsProfile, err := resolveActiveCodexEndpointTLSProfileForURL(
		officialCodexEndpointOAuthRefresh,
		request.URL,
	)
	if err != nil {
		return nil, nil, err
	}
	return request, tlsProfile, nil
}

// BuildOfficialCodex0145OAuthRefreshRequest 保留旧调用方兼容，实际始终解析 active 版本。
// 新接入点应使用 BuildActiveCodexOAuthRefreshRequest，避免业务代码持有版本标识。
func BuildOfficialCodex0145OAuthRefreshRequest(
	ctx context.Context,
	clientID string,
	refreshToken string,
	scope string,
) (*http.Request, *tlsfingerprint.Profile, error) {
	return BuildActiveCodexOAuthRefreshRequest(ctx, clientID, refreshToken, scope)
}
