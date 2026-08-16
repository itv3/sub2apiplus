package service

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/stretchr/testify/require"
)

type openaiOAuthClientRefreshStub struct {
	refreshCalls    int32
	refreshResponse *openai.TokenResponse
	refreshErr      error
}

func (s *openaiOAuthClientRefreshStub) ExchangeCode(ctx context.Context, code, codeVerifier, redirectURI, proxyURL, clientID string) (*openai.TokenResponse, error) {
	return nil, errors.New("not implemented")
}

func (s *openaiOAuthClientRefreshStub) DecodeRefreshResponse(
	_ context.Context,
	response *http.Response,
	transportErr error,
	_ string,
) (*openai.TokenResponse, error) {
	atomic.AddInt32(&s.refreshCalls, 1)
	if response != nil && response.Body != nil {
		_ = response.Body.Close()
	}
	if transportErr != nil {
		return nil, transportErr
	}
	if s.refreshResponse != nil || s.refreshErr != nil {
		return s.refreshResponse, s.refreshErr
	}
	return nil, errors.New("not implemented")
}

type openAIOAuthClientWithoutRefreshDecoder struct{}

func (openAIOAuthClientWithoutRefreshDecoder) ExchangeCode(
	context.Context,
	string,
	string,
	string,
	string,
	string,
) (*openai.TokenResponse, error) {
	return nil, errors.New("not implemented")
}

func TestOpenAIOAuthService_RefreshAccountTokenDoesNotBypassPrivacyCooldown(t *testing.T) {
	client := &openaiOAuthClientRefreshStub{refreshResponse: &openai.TokenResponse{
		AccessToken: "rotated-access-token", RefreshToken: "rotated-refresh-token", ExpiresIn: 3600,
	}}
	svc := NewOpenAIOAuthService(nil, client)
	defer svc.Stop()
	resource := &oauthRefreshCaptureResource{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(), nil, officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive, resource,
	)
	require.NoError(t, err)
	svc.SetOfficialEgressRuntime(runtimeState)
	capture := &privacyProductionCapture{}
	var proxyURLs []string
	var rolloutKeys []string
	svc.SetPrivacyClientFactory(capture.factory(&proxyURLs, &rolloutKeys))

	stableKey := buildOpenAIPrivacyRolloutKey("acct-cooldown")
	account := &Account{
		ID:       78,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":       "old-access-token",
			"refresh_token":      "old-refresh-token",
			"chatgpt_account_id": "acct-cooldown",
		},
		Extra: map[string]any{
			"privacy_mode":            PrivacyModeCFBlocked,
			privacyRetryAfterExtraKey: time.Now().UTC().Add(time.Hour).Format(time.RFC3339),
			privacyRolloutKeyExtraKey: stableKey,
		},
	}

	info, err := svc.RefreshAccountToken(context.Background(), account)
	require.NoError(t, err)
	require.Equal(t, "rotated-access-token", info.AccessToken)
	require.Equal(t, int32(1), atomic.LoadInt32(&client.refreshCalls))
	require.NotNil(t, resource.request)
	for _, request := range capture.snapshot() {
		require.NotEqual(t, "/backend-api/settings/account_user_setting", request.URL.Path,
			"账号刷新不得在读取冷却状态前调用 settings")
	}
	for _, rolloutKey := range rolloutKeys {
		require.Equal(t, stableKey, rolloutKey)
	}

	requestCount := len(capture.snapshot())
	result := ensureOpenAIPrivacyForAccount(
		context.Background(), svc.privacyClientFactory, account, "", false,
	)
	require.Empty(t, result.Mode, "冷却期内统一入口必须直接跳过")
	require.Len(t, capture.snapshot(), requestCount, "冷却检查不得产生 settings 请求")
}

func TestOpenAIOAuthService_RefreshMissingDecoderFailsClosed(t *testing.T) {
	svc := NewOpenAIOAuthService(nil, openAIOAuthClientWithoutRefreshDecoder{})
	defer svc.Stop()

	_, err := svc.RefreshTokenWithClientID(
		context.Background(), "refresh-token", "", "client-id",
	)
	require.ErrorContains(t, err, "缺少受管响应解码端口")
}

func TestOpenAIOAuthService_RefreshMissingRuntimeFailsClosed(t *testing.T) {
	client := &openaiOAuthClientRefreshStub{}
	svc := NewOpenAIOAuthService(nil, client)
	defer svc.Stop()

	_, err := svc.RefreshTokenWithClientID(
		context.Background(), "refresh-token", "", "client-id",
	)
	require.ErrorContains(t, err, "缺少正式 Executor runtime")
	require.Zero(t, atomic.LoadInt32(&client.refreshCalls), "缺少 runtime 时不得进入响应解码端口")
}

func TestOpenAIOAuthService_RefreshAccountToken_NoRefreshTokenUsesExistingAccessToken(t *testing.T) {
	client := &openaiOAuthClientRefreshStub{}
	svc := NewOpenAIOAuthService(nil, client)
	var privacyClientCalls int32
	svc.SetPrivacyClientFactory(func(request PrivacyClientRequest) (*PrivacyHTTPClient, error) {
		atomic.AddInt32(&privacyClientCalls, 1)
		return nil, errors.New("stop before request")
	})

	expiresAt := time.Now().Add(30 * time.Minute).UTC().Format(time.RFC3339)
	account := &Account{
		ID:       77,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token": "existing-access-token",
			"expires_at":   expiresAt,
			"client_id":    "client-id-1",
		},
	}

	info, err := svc.RefreshAccountToken(context.Background(), account)
	require.NoError(t, err)
	require.NotNil(t, info)
	require.Equal(t, "existing-access-token", info.AccessToken)
	require.Equal(t, "client-id-1", info.ClientID)
	require.Zero(t, atomic.LoadInt32(&client.refreshCalls), "existing access token should be reused without calling refresh")
	require.Positive(t, atomic.LoadInt32(&privacyClientCalls), "existing access token should still run enrichment")
}

func TestOpenAIOAuthService_RefreshAccountToken_PATIgnoresStaleRefreshToken(t *testing.T) {
	configureObserveGuardForLocalHTTPTest(t)
	client := &openaiOAuthClientRefreshStub{}
	var whoamiCalls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&whoamiCalls, 1)
		w.Header().Set("content-type", "application/json")
		_, _ = w.Write([]byte(`{
			"email":"user@example.com",
			"chatgpt_user_id":"user-123",
			"chatgpt_account_id":"acct-123",
			"chatgpt_plan_type":"plus",
			"chatgpt_account_is_fedramp":false
		}`))
	}))
	defer server.Close()

	originalURL := openAICodexPATWhoamiURL
	openAICodexPATWhoamiURL = server.URL
	defer func() { openAICodexPATWhoamiURL = originalURL }()

	svc := NewOpenAIOAuthService(nil, client)
	defer svc.Stop()

	account := &Account{
		ID:       77,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":  "at-test-token",
			"refresh_token": "stale-refresh-token",
			"expires_at":    time.Now().Add(-time.Hour).UTC().Format(time.RFC3339),
			"auth_mode":     "personal_access_token",
		},
	}

	info, err := svc.RefreshAccountToken(context.Background(), account)
	require.NoError(t, err)
	require.Equal(t, OpenAIAuthModePersonalAccessToken, info.AuthMode)
	require.Equal(t, "at-test-token", info.AccessToken)
	require.Empty(t, info.RefreshToken)
	require.Equal(t, int32(1), atomic.LoadInt32(&whoamiCalls))
	require.Zero(t, atomic.LoadInt32(&client.refreshCalls), "PAT accounts must not call OAuth refresh even if stale refresh_token remains")
}

func TestOpenAITokenRefresher_NeedsRefresh_SkipsAccountWithoutRefreshToken(t *testing.T) {
	refresher := NewOpenAITokenRefresher(nil, nil)
	expiresAt := time.Now().Add(time.Minute).UTC().Format(time.RFC3339)

	withoutRT := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token": "access-token",
			"expires_at":   expiresAt,
		},
	}
	require.False(t, refresher.NeedsRefresh(withoutRT, 5*time.Minute))

	withRT := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":  "access-token",
			"refresh_token": "refresh-token",
			"expires_at":    expiresAt,
		},
	}
	require.True(t, refresher.NeedsRefresh(withRT, 5*time.Minute))

	patWithStaleRT := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":  "at-test-token",
			"refresh_token": "stale-refresh-token",
			"expires_at":    expiresAt,
			"auth_mode":     OpenAIAuthModePersonalAccessToken,
		},
	}
	require.False(t, refresher.NeedsRefresh(patWithStaleRT, 5*time.Minute))
}

func TestOpenAITokenRefresher_Refresh_PATRemovesStaleOAuthFields(t *testing.T) {
	configureObserveGuardForLocalHTTPTest(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		_, _ = w.Write([]byte(`{
			"email":"user@example.com",
			"chatgpt_user_id":"user-123",
			"chatgpt_account_id":"acct-123",
			"chatgpt_plan_type":"plus",
			"chatgpt_account_is_fedramp":true
		}`))
	}))
	defer server.Close()

	originalURL := openAICodexPATWhoamiURL
	openAICodexPATWhoamiURL = server.URL
	defer func() { openAICodexPATWhoamiURL = originalURL }()

	svc := NewOpenAIOAuthService(nil, nil)
	defer svc.Stop()
	refresher := NewOpenAITokenRefresher(svc, nil)

	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":  "at-test-token",
			"refresh_token": "stale-refresh-token",
			"id_token":      "stale-id-token",
			"expires_at":    time.Now().Add(-time.Hour).UTC().Format(time.RFC3339),
			"expires_in":    3600,
			"client_id":     "stale-client",
			"auth_mode":     OpenAIAuthModePersonalAccessToken,
			"model_mapping": map[string]any{"gpt-5": "gpt-5-codex"},
		},
	}

	credentials, err := refresher.Refresh(context.Background(), account)
	require.NoError(t, err)
	require.Equal(t, "at-test-token", credentials["access_token"])
	require.Equal(t, OpenAIAuthModePersonalAccessToken, credentials["auth_mode"])
	require.Equal(t, "personal_access_token", credentials["openai_auth_mode"])
	require.NotContains(t, credentials, "refresh_token")
	require.NotContains(t, credentials, "id_token")
	require.NotContains(t, credentials, "expires_at")
	require.NotContains(t, credentials, "expires_in")
	require.NotContains(t, credentials, "client_id")
	require.Equal(t, map[string]any{"gpt-5": "gpt-5-codex"}, credentials["model_mapping"])
}

func TestOpenAITokenProvider_NoRefreshTokenExpiredAccessTokenReturnsError(t *testing.T) {
	provider := NewOpenAITokenProvider(nil, nil, nil)
	expiresAt := time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token": "expired-access-token",
			"expires_at":   expiresAt,
		},
	}

	token, err := provider.GetAccessToken(context.Background(), account)
	require.Error(t, err)
	require.Empty(t, token)
	require.Contains(t, err.Error(), "refresh_token is missing")
}
