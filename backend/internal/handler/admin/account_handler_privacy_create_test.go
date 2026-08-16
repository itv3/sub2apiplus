package admin

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

type handlerPrivacyCreateAccountRepo struct {
	service.AdminAccountRepository

	mu               sync.RWMutex
	extra            map[string]any
	updateExtraCalls int
}

func (r *handlerPrivacyCreateAccountRepo) Create(_ context.Context, account *service.Account) error {
	account.ID = 9101
	r.mu.Lock()
	r.extra = maps.Clone(account.Extra)
	r.mu.Unlock()
	return nil
}

func (r *handlerPrivacyCreateAccountRepo) UpdateExtra(_ context.Context, _ int64, updates map[string]any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.extra == nil {
		r.extra = make(map[string]any, len(updates))
	}
	for key, value := range updates {
		r.extra[key] = value
	}
	r.updateExtraCalls++
	return nil
}

func (r *handlerPrivacyCreateAccountRepo) privacyState() (map[string]any, int) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return maps.Clone(r.extra), r.updateExtraCalls
}

type handlerPrivacyCreateGroupRepo struct {
	service.AdminGroupRepository
}

func (handlerPrivacyCreateGroupRepo) ListActiveByPlatform(context.Context, string) ([]service.Group, error) {
	return nil, nil
}

type handlerPrivacyCreateCFTransport struct {
	settingsCalls *atomic.Int32
}

func (t handlerPrivacyCreateCFTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if strings.Contains(request.URL.Path, "/settings/") {
		t.settingsCalls.Add(1)
	}
	return &http.Response{
		StatusCode: http.StatusServiceUnavailable,
		Status:     http.StatusText(http.StatusServiceUnavailable),
		Header:     http.Header{"Content-Type": []string{"text/html"}},
		Body:       io.NopCloser(strings.NewReader("Cloudflare: Just a moment")),
		Request:    request,
	}, nil
}

func TestAccountHandlerCreateUsesSinglePrivacyEnsureAndPersistsCooldown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	settingsCalls := &atomic.Int32{}
	accountRepo := &handlerPrivacyCreateAccountRepo{}
	groupRepo := handlerPrivacyCreateGroupRepo{}
	privacyFactory := func(service.PrivacyClientRequest) (*service.PrivacyHTTPClient, error) {
		client := req.NewClient()
		client.GetClient().Transport = handlerPrivacyCreateCFTransport{settingsCalls: settingsCalls}
		return &service.PrivacyHTTPClient{
			Client:          client,
			Persona:         "chrome_133_xhr",
			FailureCooldown: time.Hour,
		}, nil
	}

	// 使用生产构造器创建真实 adminServiceImpl，避免只验证 Handler stub 而产生假绿。
	adminService := service.NewAdminService(
		nil, groupRepo, accountRepo, nil, nil, nil, nil, nil, nil,
		nil, nil, nil, nil, nil, nil, nil, privacyFactory, nil, nil, nil, nil, nil,
	)
	handler := NewAccountHandler(
		adminService, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil,
	)
	router := gin.New()
	router.POST("/api/v1/admin/accounts", handler.Create)

	payload, err := json.Marshal(map[string]any{
		"name":     "handler-generic-openai-oauth",
		"platform": service.PlatformOpenAI,
		"type":     service.AccountTypeOAuth,
		"credentials": map[string]any{
			"access_token": "handler-access-token",
		},
		"concurrency": 1,
		"priority":    1,
	})
	require.NoError(t, err)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "handler-create-single-privacy")
	router.ServeHTTP(recorder, request)
	require.Equal(t, http.StatusOK, recorder.Code, recorder.Body.String())

	require.Eventually(t, func() bool {
		extra, updateCalls := accountRepo.privacyState()
		return settingsCalls.Load() == 1 &&
			updateCalls == 1 &&
			extra["privacy_mode"] == service.PrivacyModeCFBlocked &&
			extra["privacy_retry_after"] != ""
	}, time.Second, 10*time.Millisecond, "Cloudflare 失败必须由唯一 Ensure 原子持久化")

	extra, updateCalls := accountRepo.privacyState()
	retryAfter, ok := extra["privacy_retry_after"].(string)
	require.True(t, ok)
	parsedRetryAfter, err := time.Parse(time.RFC3339, retryAfter)
	require.NoError(t, err)
	require.True(t, parsedRetryAfter.After(time.Now()))
	require.Equal(t, "chrome_133_xhr", extra["privacy_browser_persona"])
	require.NotEmpty(t, extra["privacy_rollout_key"])
	require.Equal(t, 1, updateCalls)

	require.Never(t, func() bool {
		_, calls := accountRepo.privacyState()
		return settingsCalls.Load() > 1 || calls > 1
	}, 200*time.Millisecond, 10*time.Millisecond, "通用创建不得在 Ensure 后再次 Force")
}

type handlerPrivacyForceCountingService struct {
	*stubAdminService
	openAIForceCalls      atomic.Int32
	antigravityForceCalls atomic.Int32
}

func (s *handlerPrivacyForceCountingService) CreateAccount(
	ctx context.Context,
	input *service.CreateAccountInput,
) (*service.Account, error) {
	account, err := s.stubAdminService.CreateAccount(ctx, input)
	if account != nil {
		account.Platform = input.Platform
		account.Type = input.Type
		account.Credentials = maps.Clone(input.Credentials)
		account.Extra = maps.Clone(input.Extra)
	}
	return account, err
}

func (s *handlerPrivacyForceCountingService) ForceOpenAIPrivacy(context.Context, *service.Account) string {
	s.openAIForceCalls.Add(1)
	return ""
}

func (s *handlerPrivacyForceCountingService) ForceAntigravityPrivacy(context.Context, *service.Account) string {
	s.antigravityForceCalls.Add(1)
	return ""
}

func (s *handlerPrivacyForceCountingService) forceCalls() int32 {
	return s.openAIForceCalls.Load() + s.antigravityForceCalls.Load()
}

func TestBatchCreateDoesNotForcePrivacyAfterServiceCreate(t *testing.T) {
	gin.SetMode(gin.TestMode)
	adminService := &handlerPrivacyForceCountingService{stubAdminService: newStubAdminService()}
	handler := NewAccountHandler(
		adminService, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil,
	)
	router := gin.New()
	router.POST("/api/v1/admin/accounts/batch", handler.BatchCreate)

	payload := `{"accounts":[` +
		`{"name":"openai","platform":"openai","type":"oauth","credentials":{"access_token":"token-openai"}},` +
		`{"name":"antigravity","platform":"antigravity","type":"oauth","credentials":{"access_token":"token-antigravity"}}` +
		`]}`
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/batch", strings.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "batch-create-no-force")
	router.ServeHTTP(recorder, request)
	require.Equal(t, http.StatusOK, recorder.Code, recorder.Body.String())
	require.Never(t, func() bool { return adminService.forceCalls() > 0 }, 150*time.Millisecond, 10*time.Millisecond)
}

func TestImportDataDoesNotForcePrivacyAfterServiceCreate(t *testing.T) {
	gin.SetMode(gin.TestMode)
	adminService := &handlerPrivacyForceCountingService{stubAdminService: newStubAdminService()}
	handler := NewAccountHandler(
		adminService, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil,
	)
	router := gin.New()
	router.POST("/api/v1/admin/accounts/data", handler.ImportData)

	payload := `{"data":{"type":"sub2api-data","version":1,"proxies":[],"accounts":[` +
		`{"name":"antigravity","platform":"antigravity","type":"oauth","credentials":{"access_token":"token-antigravity"}}` +
		`]},"skip_default_group_bind":true}`
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/data", strings.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "import-data-no-force")
	router.ServeHTTP(recorder, request)
	require.Equal(t, http.StatusOK, recorder.Code, recorder.Body.String())
	require.Never(t, func() bool { return adminService.forceCalls() > 0 }, 150*time.Millisecond, 10*time.Millisecond)
}
