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

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
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

type openAIOAuthReqProfileTransport struct{}

func (s *openaiOAuthService) ExchangeCode(ctx context.Context, code, codeVerifier, redirectURI, proxyURL, clientID string) (*openai.TokenResponse, error) {
	boundCtx, bindErr := officialegress.StartDefaultSinkAttempt(ctx, officialegress.SinkCodexOAuthExchange)
	if bindErr != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_INVALID", "bind OAuth exchange official egress sink: %v", bindErr)
	}
	ctx = boundCtx
	releaseMode, ok := officialegress.ReleaseModeFromContext(ctx)
	if !ok {
		return nil, infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_OAUTH_RELEASE_MODE_MISSING",
			"OAuth exchange 缺少进程级冻结 release mode",
		)
	}
	exchangeProfile, err := resolveOpenAIExchangeTLSProfile(releaseMode)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_EXCHANGE_PROFILE_FAILED", "resolve OAuth exchange profile: %v", err)
	}
	client, err := createOpenAIExchangeReqClient(proxyURL, exchangeProfile)
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

	// 官方画像的 exchange 使用 raw auth client。这里显式写入抓包确认的
	// form 字段顺序，并清空 req/v3 默认 User-Agent，避免伪装成 refresh client。
	formBody := strings.Join([]string{
		"grant_type=" + url.QueryEscape("authorization_code"),
		"code=" + url.QueryEscape(code),
		"redirect_uri=" + url.QueryEscape(redirectURI),
		"client_id=" + url.QueryEscape(clientID),
		"code_verifier=" + url.QueryEscape(codeVerifier),
	}, "&")

	var tokenResp openai.TokenResponse

	resp, err := client.R().
		SetContext(ctx).
		SetHeader("Content-Type", "application/x-www-form-urlencoded").
		SetHeader("Accept", "*/*").
		SetHeader("User-Agent", "").
		SetBodyString(formBody).
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

func (s *openaiOAuthService) DecodeRefreshResponse(
	ctx context.Context,
	resp *http.Response,
	transportErr error,
	proxyURL string,
) (*openai.TokenResponse, error) {
	if transportErr != nil {
		if shouldReturnOpenAIDirectConnectionHint(ctx, proxyURL, transportErr) {
			return nil, newOpenAIDirectConnectionError(transportErr)
		}
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_OAUTH_REQUEST_FAILED", "request failed: %v", transportErr)
	}
	if resp == nil {
		return nil, infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_OAUTH_REQUEST_FAILED",
			"req-profile adapter 返回空响应",
		)
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
