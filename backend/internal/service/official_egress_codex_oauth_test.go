package service

import (
	"context"
	"errors"
	"io"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

type officialCodexOAuthRuntimeClient struct {
	state officialCodex0145RuntimeState
	err   error
}

func (c *officialCodexOAuthRuntimeClient) ExchangeCode(
	context.Context,
	string,
	string,
	string,
	string,
	string,
) (*openai.TokenResponse, error) {
	return nil, errors.New("本测试不执行授权码交换")
}

func (c *officialCodexOAuthRuntimeClient) RefreshToken(
	ctx context.Context,
	refreshToken string,
	proxyURL string,
) (*openai.TokenResponse, error) {
	return c.RefreshTokenWithClientID(ctx, refreshToken, proxyURL, "")
}

func (c *officialCodexOAuthRuntimeClient) RefreshTokenWithClientID(
	ctx context.Context,
	_ string,
	_ string,
	_ string,
) (*openai.TokenResponse, error) {
	c.state, _, c.err = officialCodex0145RuntimeStateFromContext(ctx)
	if c.err != nil {
		return nil, c.err
	}
	return &openai.TokenResponse{
		AccessToken:  "refreshed-access-token",
		RefreshToken: "rotated-refresh-token",
		ExpiresIn:    3600,
	}, nil
}

type officialCodexOAuthRuntimeRepo struct {
	AccountRepository
	account *Account
}

func (r *officialCodexOAuthRuntimeRepo) GetByID(context.Context, int64) (*Account, error) {
	if r.account == nil {
		return nil, errors.New("测试账号不存在")
	}
	return r.account, nil
}

func (r *officialCodexOAuthRuntimeRepo) Update(_ context.Context, account *Account) error {
	r.account = account
	return nil
}

func TestResolveActiveCodexOAuthExchangeProfileUsesTLSOnlyIdentity(t *testing.T) {
	profile, err := ResolveActiveCodexOAuthExchangeProfile()
	if err != nil {
		t.Fatal(err)
	}
	active, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil {
		t.Fatal(err)
	}
	if profile.UserAgent != active.Build.UserAgent {
		t.Fatalf("OAuth 换码未使用 active Codex UA：%q", profile.UserAgent)
	}
	if profile.Originator != active.Build.Originator {
		t.Fatalf("OAuth 换码未使用 active Codex originator：%q", profile.Originator)
	}
	if profile.TLSProfile == nil {
		t.Fatal("OAuth 换码缺少 TLS 画像")
	}
	if profile.TLSProfile.Transport.StrictH1Wire || len(profile.TLSProfile.Transport.H1HeaderOrders) != 0 {
		t.Fatalf("OAuth 换码错误继承 strict H1 规则：%+v", profile.TLSProfile.Transport)
	}

	refreshProfile, err := resolveActiveCodexEndpointTLSProfile(officialCodexEndpointOAuthRefresh)
	if err != nil {
		t.Fatal(err)
	}
	if !refreshProfile.Transport.StrictH1Wire || len(refreshProfile.Transport.H1HeaderOrders) == 0 {
		t.Fatal("OAuth refresh 严格画像被意外降级")
	}
	if profile.TLSProfile.Name == refreshProfile.Name {
		t.Fatalf("OAuth 换码与 refresh 使用相同缓存身份：%q", profile.TLSProfile.Name)
	}

	second, err := ResolveActiveCodexOAuthExchangeProfile()
	if err != nil {
		t.Fatal(err)
	}
	if second.TLSProfile == profile.TLSProfile {
		t.Fatal("OAuth 换码画像未按调用隔离")
	}
}

func TestOfficialCodex0145OAuthRefreshUsesVersionProfile(t *testing.T) {
	request, tlsProfile, err := BuildOfficialCodex0145OAuthRefreshRequest(
		context.Background(),
		"client-id",
		"refresh +/token",
		"openid profile email",
	)
	if err != nil {
		t.Fatal(err)
	}
	if request.URL.String() != "https://auth.openai.com/oauth/token" || request.Host != "auth.openai.com" {
		t.Fatalf("OAuth refresh 目标不一致：url=%s host=%s", request.URL, request.Host)
	}
	body, err := io.ReadAll(request.Body)
	if err != nil {
		t.Fatal(err)
	}
	wantBody := "client_id=client-id&grant_type=refresh_token&refresh_token=refresh+%2B%2Ftoken&scope=openid+profile+email"
	if string(body) != wantBody {
		t.Fatalf("OAuth refresh 表单或字段顺序不一致：\n实际：%s\n期望：%s", body, wantBody)
	}
	if request.ContentLength != int64(len(wantBody)) {
		t.Fatalf("OAuth refresh content-length 不一致：%d", request.ContentLength)
	}
	if request.Header.Get("content-type") != "application/x-www-form-urlencoded" ||
		request.Header.Get("accept") != "application/json" ||
		!strings.HasPrefix(request.Header.Get("user-agent"), "codex_exec/0.145.0 ") {
		t.Fatalf("OAuth refresh header 不一致：%v", request.Header)
	}
	if request.Header.Get("originator") != "" || len(request.Header) != 3 {
		t.Fatalf("OAuth refresh 泄漏画像外 header：%v", request.Header)
	}
	if tlsProfile == nil || !tlsProfile.Transport.StrictH1Wire {
		t.Fatal("OAuth refresh 未绑定 strict H1 TLS 画像")
	}
	foundRule := false
	for _, rule := range tlsProfile.Transport.H1HeaderOrders {
		if rule.Method == "POST" && rule.Path == "/oauth/token" {
			foundRule = true
			break
		}
	}
	if !foundRule {
		t.Fatal("OAuth refresh TLS 画像缺少精确 method/path 规则")
	}
}

func TestOfficialCodex0145OAuthRefreshFormRejectsOpenOrDuplicateFields(t *testing.T) {
	testCases := []url.Values{
		{
			"client_id":     {"client-id"},
			"grant_type":    {"refresh_token"},
			"refresh_token": {"refresh-token"},
			"scope":         {"openid profile email"},
			"unexpected":    {"value"},
		},
		{
			"client_id":     {"client-id", "duplicate"},
			"grant_type":    {"refresh_token"},
			"refresh_token": {"refresh-token"},
			"scope":         {"openid profile email"},
		},
	}
	for _, values := range testCases {
		if _, err := officialCodex0145ValidateAndOrderFormBody(
			officialCodexVersion0145,
			officialCodexEndpointOAuthRefresh,
			values,
		); err == nil {
			t.Fatalf("开放或重复表单字段未被拒绝：%v", values)
		}
	}
}

func TestOfficialCodex0145OAuthRefreshPreservesTUITerminalRuntime(t *testing.T) {
	state := defaultOfficialCodex0145RuntimeState()
	state.SurfaceID = officialCodexSurfaceTUI
	state.Originator = "codex-tui"
	state.TerminalToken = "xterm-256color"
	state.UserAgentSuffixEnabled = false
	ctx, err := withOfficialCodex0145RuntimeState(context.Background(), state)
	if err != nil {
		t.Fatal(err)
	}
	request, _, err := BuildOfficialCodex0145OAuthRefreshRequest(
		ctx,
		"client-id",
		"refresh-token",
		"openid",
	)
	if err != nil {
		t.Fatal(err)
	}
	want := "codex-tui/0.145.0 (Ubuntu 24.4.0; x86_64) xterm-256color"
	if got := request.Header.Get("user-agent"); got != want {
		t.Fatalf("OAuth refresh 未保留 TUI runtime：实际 %q，期望 %q", got, want)
	}
}

func TestOfficialCodex0145TokenProviderPreservesRuntimeAcrossRefreshLayers(t *testing.T) {
	account := &Account{
		ID:          145,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Status:      StatusActive,
		Schedulable: true,
		Credentials: map[string]any{
			"access_token":       "expired-access-token",
			"refresh_token":      "refresh-token",
			"expires_at":         time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			"chatgpt_account_id": "chatgpt-account",
		},
	}
	repo := &officialCodexOAuthRuntimeRepo{account: account}
	client := &officialCodexOAuthRuntimeClient{}
	oauthService := NewOpenAIOAuthService(nil, client)
	defer oauthService.Stop()
	provider := NewOpenAITokenProvider(repo, nil, oauthService)
	provider.SetRefreshAPI(
		NewOAuthRefreshAPI(repo, nil),
		NewOpenAITokenRefresher(oauthService, repo),
	)

	state := defaultOfficialCodex0145RuntimeState()
	state.SurfaceID = officialCodexSurfaceTUI
	state.Originator = "codex-tui"
	state.TerminalToken = "xterm-256color"
	state.UserAgentSuffixEnabled = false
	ctx, err := withOfficialCodex0145RuntimeState(context.Background(), state)
	if err != nil {
		t.Fatal(err)
	}

	token, err := provider.GetAccessToken(ctx, account)
	if err != nil {
		t.Fatal(err)
	}
	if token != "refreshed-access-token" {
		t.Fatalf("TokenProvider 未返回刷新 token：%q", token)
	}
	if client.err != nil {
		t.Fatal(client.err)
	}
	if client.state.SurfaceID != officialCodexSurfaceTUI ||
		client.state.Originator != "codex-tui" ||
		client.state.TerminalToken != "xterm-256color" ||
		client.state.UserAgentSuffixEnabled {
		t.Fatalf("OAuth 刷新中间层丢失运行态：%+v", client.state)
	}
}
