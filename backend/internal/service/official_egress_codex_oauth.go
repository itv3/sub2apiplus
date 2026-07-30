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

// BuildOfficialCodex0145OAuthRefreshRequest 为尚未绑定账号的 OAuth 刷新阶段
// 提供唯一薄层入口。该阶段没有 Account，不能构造账号级 OfficialEgressContext；
// 但 URL、表单闭集和顺序、header 闭集和线序以及 TLS/H1 仍全部由同一版本画像编译。
func BuildOfficialCodex0145OAuthRefreshRequest(
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
	body, err := officialCodex0145ValidateAndOrderFormBody(
		officialCodexVersion0145,
		officialCodexEndpointOAuthRefresh,
		values,
	)
	if err != nil {
		return nil, nil, err
	}
	target, err := officialCodex0145BuildEndpointURL(
		officialCodexVersion0145,
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
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
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
	if _, err := officialCodex0145ApplyHeaderContract(
		officialCodexVersion0145,
		officialCodexEndpointOAuthRefresh,
		request.Header,
		nil,
	); err != nil {
		return nil, nil, err
	}
	tlsProfile, err := officialCodex0145ResolveEndpointTLSProfileForURL(
		officialCodexVersion0145,
		officialCodexEndpointOAuthRefresh,
		request.URL,
	)
	if err != nil {
		return nil, nil, err
	}
	return request, tlsProfile, nil
}
