package admin

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

type openAIOAuthCreatePrivacyClient struct {
	idToken       string
	exchangeCalls *atomic.Int32
}

func (s openAIOAuthCreatePrivacyClient) ExchangeCode(
	_ context.Context,
	_, _, _, _, _ string,
) (*openai.TokenResponse, error) {
	if s.exchangeCalls != nil {
		s.exchangeCalls.Add(1)
	}
	return &openai.TokenResponse{
		AccessToken: "initial-access-token", RefreshToken: "initial-refresh-token",
		IDToken: s.idToken, ExpiresIn: 3600,
	}, nil
}

func TestCreateAccountFromOAuthValidatesExtraBeforeExchange(t *testing.T) {
	gin.SetMode(gin.TestMode)
	exchangeCalls := &atomic.Int32{}
	oauthService := service.NewOpenAIOAuthService(nil, openAIOAuthCreatePrivacyClient{
		exchangeCalls: exchangeCalls,
	})
	defer oauthService.Stop()

	auth, err := oauthService.GenerateAuthURL(context.Background(), nil, "", service.PlatformOpenAI)
	require.NoError(t, err)
	parsedAuthURL, err := url.Parse(auth.AuthURL)
	require.NoError(t, err)
	payload, err := json.Marshal(map[string]any{
		"session_id": auth.SessionID,
		"code":       "authorization-code",
		"state":      parsedAuthURL.Query().Get("state"),
		"extra": map[string]any{
			"openai_long_context_billing_enabled": "invalid",
		},
	})
	require.NoError(t, err)

	handler := NewOpenAIOAuthHandler(oauthService, &stubAdminService{}, nil, nil)
	router := gin.New()
	router.POST("/api/v1/admin/openai/create-from-oauth", handler.CreateAccountFromOAuth)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/openai/create-from-oauth", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)

	require.Equal(t, http.StatusBadRequest, recorder.Code)
	require.Zero(t, exchangeCalls.Load(), "非法账号配置必须在兑换授权码前拒绝")
}

type openAIOAuthCreatePrivacyTransport struct {
	settingsStatus int
	settingsBody   string
	settingsCalls  *atomic.Int32
}

func (t openAIOAuthCreatePrivacyTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	status := http.StatusOK
	body := `{}`
	if strings.Contains(request.URL.Path, "/accounts/check/") {
		body = `{"accounts":{}}`
	} else if strings.Contains(request.URL.Path, "/settings/") && t.settingsStatus != 0 {
		if t.settingsCalls != nil {
			t.settingsCalls.Add(1)
		}
		status = t.settingsStatus
		body = t.settingsBody
	} else if strings.Contains(request.URL.Path, "/settings/") && t.settingsCalls != nil {
		t.settingsCalls.Add(1)
	}
	return &http.Response{
		StatusCode: status,
		Status:     http.StatusText(status),
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    request,
	}, nil
}

func createOAuthAccountWithInitialPrivacy(
	t *testing.T,
	transport openAIOAuthCreatePrivacyTransport,
) (*service.CreateAccountInput, int32) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	settingsCalls := &atomic.Int32{}
	transport.settingsCalls = settingsCalls
	oauthService := service.NewOpenAIOAuthService(nil, openAIOAuthCreatePrivacyClient{})
	defer oauthService.Stop()
	oauthService.SetPrivacyClientFactory(func(service.PrivacyClientRequest) (*service.PrivacyHTTPClient, error) {
		client := req.NewClient()
		client.GetClient().Transport = transport
		return &service.PrivacyHTTPClient{
			Client: client, Persona: "chrome_133_xhr", FailureCooldown: time.Hour,
		}, nil
	})

	auth, err := oauthService.GenerateAuthURL(context.Background(), nil, "", service.PlatformOpenAI)
	require.NoError(t, err)
	parsedAuthURL, err := url.Parse(auth.AuthURL)
	require.NoError(t, err)
	state := parsedAuthURL.Query().Get("state")
	require.NotEmpty(t, state)

	payload, err := json.Marshal(map[string]any{
		"session_id":  auth.SessionID,
		"code":        "authorization-code",
		"state":       state,
		"name":        "privacy-create-test",
		"concurrency": 1,
		"priority":    1,
		"credential_extras": map[string]any{
			"access_token":  "client-must-not-override",
			"model_mapping": map[string]any{"gpt-5": "gpt-5"},
		},
		"extra": map[string]any{
			"privacy_rollout_key": "client-must-not-control",
			"custom_setting":      true,
		},
	})
	require.NoError(t, err)
	recorder := httptest.NewRecorder()

	adminService := &stubAdminService{}
	handler := NewOpenAIOAuthHandler(oauthService, adminService, nil, nil)
	router := gin.New()
	router.POST("/api/v1/admin/openai/create-from-oauth", handler.CreateAccountFromOAuth)
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/openai/create-from-oauth", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)

	require.Equal(t, http.StatusOK, recorder.Code)
	require.Len(t, adminService.createdAccounts, 1)
	return adminService.createdAccounts[0], settingsCalls.Load()
}

func TestCreateAccountFromOAuthPersistsInitialPrivacyResultAtomically(t *testing.T) {
	created, settingsCalls := createOAuthAccountWithInitialPrivacy(t, openAIOAuthCreatePrivacyTransport{})
	require.EqualValues(t, 1, settingsCalls)
	require.Equal(t, "initial-access-token", created.Credentials["access_token"])
	require.NotEqual(t, "client-must-not-override", created.Credentials["access_token"])
	require.Equal(t, map[string]any{"gpt-5": "gpt-5"}, created.Credentials["model_mapping"])
	require.Equal(t, true, created.Extra["custom_setting"])
	require.NotContains(t, created.Extra, "privacy_rollout_key")
	require.Equal(t, service.PrivacyModeTrainingOff, created.ManagedPrivacyExtra["privacy_mode"])
	require.Equal(t, "chrome_133_xhr", created.ManagedPrivacyExtra["privacy_browser_persona"])
	rolloutKey, _ := created.ManagedPrivacyExtra["privacy_rollout_key"].(string)
	require.Len(t, rolloutKey, 32,
		"首次授权的 privacy 结果与稳定分桶键必须随账号创建一次写入")
}

func TestCreateAccountFromOAuthPersistsInitialPrivacyFailureCooldownAtomically(t *testing.T) {
	created, settingsCalls := createOAuthAccountWithInitialPrivacy(t, openAIOAuthCreatePrivacyTransport{
		settingsStatus: http.StatusServiceUnavailable,
		settingsBody:   "Cloudflare: Just a moment",
	})
	require.EqualValues(t, 1, settingsCalls,
		"专用创建入口在 Cloudflare 失败时只能发送一次 settings")
	require.Equal(t, service.PrivacyModeCFBlocked, created.ManagedPrivacyExtra["privacy_mode"])
	retryAfter, _ := created.ManagedPrivacyExtra["privacy_retry_after"].(string)
	parsedRetryAfter, err := time.Parse(time.RFC3339, retryAfter)
	require.NoError(t, err)
	require.True(t, parsedRetryAfter.After(time.Now().UTC()),
		"首次授权失败的 retry-after 必须与账号创建原子持久化")
	require.Len(t, created.ManagedPrivacyExtra["privacy_rollout_key"], 32)
}

func TestExchangeCodeDoesNotSendUnpersistedPrivacySettings(t *testing.T) {
	gin.SetMode(gin.TestMode)
	settingsCalls := &atomic.Int32{}
	oauthService := service.NewOpenAIOAuthService(nil, openAIOAuthCreatePrivacyClient{})
	defer oauthService.Stop()
	oauthService.SetPrivacyClientFactory(func(service.PrivacyClientRequest) (*service.PrivacyHTTPClient, error) {
		client := req.NewClient()
		client.GetClient().Transport = openAIOAuthCreatePrivacyTransport{settingsCalls: settingsCalls}
		return &service.PrivacyHTTPClient{
			Client: client, Persona: "chrome_133_xhr", FailureCooldown: time.Hour,
		}, nil
	})

	auth, err := oauthService.GenerateAuthURL(context.Background(), nil, "", service.PlatformOpenAI)
	require.NoError(t, err)
	parsedAuthURL, err := url.Parse(auth.AuthURL)
	require.NoError(t, err)
	payload, err := json.Marshal(map[string]any{
		"session_id": auth.SessionID,
		"code":       "authorization-code",
		"state":      parsedAuthURL.Query().Get("state"),
	})
	require.NoError(t, err)

	handler := NewOpenAIOAuthHandler(oauthService, &stubAdminService{}, nil, nil)
	router := gin.New()
	router.POST("/api/v1/admin/openai/exchange-code", handler.ExchangeCode)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/openai/exchange-code", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)

	require.Equal(t, http.StatusOK, recorder.Code)
	require.Zero(t, settingsCalls.Load(),
		"普通 exchange-code 不得产生无法与账号原子持久化的 privacy settings 副作用")
}

func openAIOAuthTestIDToken(accountID, userID string) string {
	payload, _ := json.Marshal(map[string]any{
		"exp": time.Now().Add(time.Hour).Unix(),
		"https://api.openai.com/auth": map[string]any{
			"chatgpt_account_id": accountID,
			"chatgpt_user_id":    userID,
			"chatgpt_plan_type":  "pro",
		},
	})
	return "e30." + base64.RawURLEncoding.EncodeToString(payload) + ".signature"
}

func TestReauthorizeOpenAIFreezesExistingRolloutKeyAcrossAllEndpoints(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const stableKey = "11111111111111111111111111111111"
	settingsCalls := &atomic.Int32{}
	rolloutKeys := make([]string, 0, 3)
	oauthService := service.NewOpenAIOAuthService(nil, openAIOAuthCreatePrivacyClient{
		idToken: openAIOAuthTestIDToken("remote-account-after-reauth", "remote-user"),
	})
	defer oauthService.Stop()
	oauthService.SetPrivacyClientFactory(func(request service.PrivacyClientRequest) (*service.PrivacyHTTPClient, error) {
		rolloutKeys = append(rolloutKeys, request.RolloutKey)
		client := req.NewClient()
		client.GetClient().Transport = openAIOAuthCreatePrivacyTransport{settingsCalls: settingsCalls}
		return &service.PrivacyHTTPClient{
			Client: client, Persona: "chrome_133_xhr", FailureCooldown: time.Hour,
		}, nil
	})

	adminService := &stubAdminService{getAccountResult: &service.Account{
		ID:       42,
		Platform: service.PlatformOpenAI,
		Type:     service.AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":  "old-access-token",
			"refresh_token": "old-refresh-token",
		},
		Extra: map[string]any{
			"privacy_rollout_key": stableKey,
		},
	}}
	auth, err := oauthService.GenerateAuthURL(context.Background(), nil, "", service.PlatformOpenAI)
	require.NoError(t, err)
	parsedAuthURL, err := url.Parse(auth.AuthURL)
	require.NoError(t, err)
	payload, err := json.Marshal(map[string]any{
		"session_id": auth.SessionID,
		"code":       "reauthorization-code",
		"state":      parsedAuthURL.Query().Get("state"),
	})
	require.NoError(t, err)

	handler := NewAccountHandler(adminService, nil, oauthService, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	router := gin.New()
	router.POST("/api/v1/admin/openai/accounts/:id/reauthorize", handler.ReauthorizeOpenAI)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/openai/accounts/42/reauthorize", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)

	require.Equal(t, http.StatusOK, recorder.Code)
	require.EqualValues(t, 1, settingsCalls.Load())
	require.Len(t, rolloutKeys, 3, "accounts/check、subscriptions 与 settings 都必须创建同一画像客户端")
	for _, rolloutKey := range rolloutKeys {
		require.Equal(t, stableKey, rolloutKey)
	}
	require.NotNil(t, adminService.lastUpdateAccountInput)
	require.Equal(t, "remote-account-after-reauth",
		adminService.lastUpdateAccountInput.Credentials["chatgpt_account_id"])
	require.Equal(t, stableKey,
		adminService.lastUpdateAccountInput.ManagedPrivacyExtra["privacy_rollout_key"])
	require.Equal(t, "chrome_133_xhr",
		adminService.lastUpdateAccountInput.ManagedPrivacyExtra["privacy_browser_persona"])
}

func TestReauthorizeOpenAIHonorsPersistedFailureCooldown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const stableKey = "22222222222222222222222222222222"
	settingsCalls := &atomic.Int32{}
	oauthService := service.NewOpenAIOAuthService(nil, openAIOAuthCreatePrivacyClient{
		idToken: openAIOAuthTestIDToken("remote-account-after-cooldown", "remote-user"),
	})
	defer oauthService.Stop()
	oauthService.SetPrivacyClientFactory(func(service.PrivacyClientRequest) (*service.PrivacyHTTPClient, error) {
		client := req.NewClient()
		client.GetClient().Transport = openAIOAuthCreatePrivacyTransport{settingsCalls: settingsCalls}
		return &service.PrivacyHTTPClient{
			Client: client, Persona: "chrome_133_xhr", FailureCooldown: time.Hour,
		}, nil
	})

	adminService := &stubAdminService{getAccountResult: &service.Account{
		ID:       43,
		Platform: service.PlatformOpenAI,
		Type:     service.AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token": "old-access-token",
		},
		Extra: map[string]any{
			"privacy_mode":            service.PrivacyModeCFBlocked,
			"privacy_retry_after":     time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			"privacy_browser_persona": "chrome_133_xhr",
			"privacy_rollout_key":     stableKey,
		},
	}}
	auth, err := oauthService.GenerateAuthURL(context.Background(), nil, "", service.PlatformOpenAI)
	require.NoError(t, err)
	parsedAuthURL, err := url.Parse(auth.AuthURL)
	require.NoError(t, err)
	payload, err := json.Marshal(map[string]any{
		"session_id": auth.SessionID,
		"code":       "reauthorization-code",
		"state":      parsedAuthURL.Query().Get("state"),
	})
	require.NoError(t, err)

	handler := NewAccountHandler(adminService, nil, oauthService, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	router := gin.New()
	router.POST("/api/v1/admin/openai/accounts/:id/reauthorize", handler.ReauthorizeOpenAI)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/openai/accounts/43/reauthorize", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)

	require.Equal(t, http.StatusOK, recorder.Code)
	require.Zero(t, settingsCalls.Load(), "冷却期内重授权不得再次请求 settings")
	require.NotNil(t, adminService.lastUpdateAccountInput)
	require.Empty(t, adminService.lastUpdateAccountInput.ManagedPrivacyExtra,
		"跳过 settings 时不得用空结果覆盖已有受管状态")
	require.Equal(t, "remote-account-after-cooldown",
		adminService.lastUpdateAccountInput.Credentials["chatgpt_account_id"])
}
