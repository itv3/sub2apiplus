package repository

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/imroc/req/v3"
)

// NewOpenAIOAuthClient creates a new OpenAI OAuth client
func NewOpenAIOAuthClient() service.OpenAIOAuthClient {
	return &openaiOAuthService{tokenURL: openai.TokenURL}
}

type openaiOAuthService struct {
	tokenURL string
}

func (s *openaiOAuthService) ExchangeCode(ctx context.Context, code, codeVerifier, redirectURI, proxyURL, clientID string) (*openai.TokenResponse, error) {
	exchangeProfile, err := service.ResolveActiveCodexOAuthExchangeProfile()
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_EXCHANGE_PROFILE_FAILED", "resolve OAuth exchange profile: %v", err)
	}
	client, err := createOpenAIExchangeReqClient(proxyURL, exchangeProfile.TLSProfile)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_CLIENT_INIT_FAILED", "create HTTP client: %v", err)
	}

	if redirectURI == "" {
		redirectURI = openai.DefaultRedirectURI
	}
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		clientID = openai.ClientID
	}

	formData := url.Values{}
	formData.Set("grant_type", "authorization_code")
	formData.Set("client_id", clientID)
	formData.Set("code", code)
	formData.Set("redirect_uri", redirectURI)
	formData.Set("code_verifier", codeVerifier)

	var tokenResp openai.TokenResponse

	resp, err := client.R().
		SetContext(ctx).
		SetHeader("User-Agent", exchangeProfile.UserAgent).
		SetHeader("originator", exchangeProfile.Originator).
		SetFormDataFromValues(formData).
		SetSuccessResult(&tokenResp).
		Post(s.tokenURL)

	if err != nil {
		if shouldReturnOpenAIDirectConnectionHint(ctx, proxyURL, err) {
			return nil, newOpenAIDirectConnectionError(err)
		}
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_FAILED", "request failed: %v", err)
	}

	if !resp.IsSuccessState() {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_TOKEN_EXCHANGE_FAILED", "token exchange failed: status %d, body: %s", resp.StatusCode, resp.String())
	}

	return &tokenResp, nil
}

func (s *openaiOAuthService) RefreshToken(ctx context.Context, refreshToken, proxyURL string) (*openai.TokenResponse, error) {
	return s.RefreshTokenWithClientID(ctx, refreshToken, proxyURL, "")
}

func (s *openaiOAuthService) RefreshTokenWithClientID(ctx context.Context, refreshToken, proxyURL string, clientID string) (*openai.TokenResponse, error) {
	// 调用方应始终传入正确的 client_id；为兼容旧数据，未指定时默认使用 OpenAI ClientID
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		clientID = openai.ClientID
	}
	return s.refreshTokenWithClientID(ctx, refreshToken, proxyURL, clientID)
}

func (s *openaiOAuthService) refreshTokenWithClientID(ctx context.Context, refreshToken, proxyURL, clientID string) (*openai.TokenResponse, error) {
	request, tlsProfile, err := service.BuildActiveCodexOAuthRefreshRequest(
		ctx,
		clientID,
		refreshToken,
		openai.RefreshScopes,
	)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_INVALID", "build refresh request: %v", err)
	}
	// tokenURL 仅为仓库测试保留本地 server 注入点；生产构造始终来自 active Codex
	// 端点画像，不能由配置改写 auth.openai.com。
	if override := strings.TrimSpace(s.tokenURL); override != "" && override != openai.TokenURL {
		overrideURL, parseErr := url.Parse(override)
		if parseErr != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_INVALID", "parse test token URL: %v", parseErr)
		}
		request.URL = overrideURL
		request.Host = overrideURL.Host
	}
	client, err := createOpenAIReqClientWithProfile(proxyURL, tlsProfile)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_CLIENT_INIT_FAILED", "create HTTP client: %v", err)
	}
	resp, err := client.GetClient().Do(request)

	if err != nil {
		if shouldReturnOpenAIDirectConnectionHint(ctx, proxyURL, err) {
			return nil, newOpenAIDirectConnectionError(err)
		}
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_FAILED", "request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_FAILED", "read response: %v", err)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_TOKEN_REFRESH_FAILED", "token refresh failed: status %d, body: %s", resp.StatusCode, string(responseBody))
	}
	var tokenResp openai.TokenResponse
	if err := json.Unmarshal(responseBody, &tokenResp); err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_TOKEN_REFRESH_FAILED", "decode token response: %v", err)
	}
	return &tokenResp, nil
}

func createOpenAIExchangeReqClient(proxyURL string, profile *tlsfingerprint.Profile) (*req.Client, error) {
	return createOpenAIReqClientWithProfile(proxyURL, profile)
}

func createOpenAIReqClientWithProfile(proxyURL string, profile *tlsfingerprint.Profile) (*req.Client, error) {
	return getSharedReqClient(reqClientOptions{
		ProxyURL:   proxyURL,
		Timeout:    120 * time.Second,
		ForceHTTP2: false,
		TLSProfile: profile,
	})
}

func shouldReturnOpenAIDirectConnectionHint(ctx context.Context, proxyURL string, err error) bool {
	if strings.TrimSpace(proxyURL) != "" || err == nil {
		return false
	}
	if ctx != nil && ctx.Err() != nil {
		return false
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	return isOpenAIConnectivityError(err)
}

// isOpenAIConnectivityError 只识别 HTTP 传输阶段包装的底层网络错误。
// 响应 JSON 解码、严格画像拒绝等应用层错误不能被误报为直连失败。
func isOpenAIConnectivityError(err error) bool {
	var requestErr *url.Error
	if !errors.As(err, &requestErr) || requestErr.Err == nil {
		return false
	}
	var networkErr net.Error
	return errors.As(requestErr.Err, &networkErr)
}

func newOpenAIDirectConnectionError(cause error) error {
	return infraerrors.New(
		http.StatusBadGateway,
		"OPENAI_OAUTH_DIRECT_CONNECTION_FAILED",
		"This server could not reach OpenAI directly. Check the server network and retry; if a proxy is needed, select one that can access OpenAI. If the authorization code has expired, regenerate the authorization URL.",
	).WithCause(cause)
}
