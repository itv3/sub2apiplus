package service

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

// ── stub helpers ─────────────────────────────────────────────────────────────

// stubQuotaAccountRepo 是多账号 AccountRepository stub，仅实现 GetByID。
type stubQuotaAccountRepo struct {
	AccountRepository
	accounts       map[int64]*Account
	extraUpdates   map[int64]map[string]any
	extraUpdateErr error
}

func (r *stubQuotaAccountRepo) GetByID(_ context.Context, id int64) (*Account, error) {
	acc, ok := r.accounts[id]
	if !ok {
		return nil, fmt.Errorf("account %d not found", id)
	}
	return acc, nil
}

func (r *stubQuotaAccountRepo) UpdateCredentials(_ context.Context, id int64, credentials map[string]any) error {
	acc, ok := r.accounts[id]
	if !ok {
		return fmt.Errorf("account %d not found", id)
	}
	acc.Credentials = credentials
	return nil
}

func (r *stubQuotaAccountRepo) UpdateExtra(_ context.Context, id int64, updates map[string]any) error {
	if r.extraUpdateErr != nil {
		return r.extraUpdateErr
	}
	if r.extraUpdates == nil {
		r.extraUpdates = make(map[int64]map[string]any)
	}
	r.extraUpdates[id] = updates
	return nil
}

// stubQuotaTokenCache 实现 OpenAITokenCache，返回预设静态 token。
type stubQuotaTokenCache struct {
	tokens map[string]string
}

func (c *stubQuotaTokenCache) GetAccessToken(_ context.Context, key string) (string, error) {
	if t, ok := c.tokens[key]; ok {
		return t, nil
	}
	return "", errors.New("token not found")
}

func (c *stubQuotaTokenCache) SetAccessToken(_ context.Context, _ string, _ string, _ time.Duration) error {
	return nil
}

func (c *stubQuotaTokenCache) DeleteAccessToken(_ context.Context, _ string) error { return nil }

func (c *stubQuotaTokenCache) AcquireRefreshLock(_ context.Context, _ string, _ time.Duration) (bool, error) {
	return true, nil
}

func (c *stubQuotaTokenCache) ReleaseRefreshLock(_ context.Context, _ string) error { return nil }

// quotaRedirectingUpstream 将保留原始路径和 header 的请求转发到测试服务器。
type quotaRedirectingUpstream struct {
	target      *url.URL
	mu          sync.Mutex
	requests    []*http.Request
	bodies      [][]byte
	tlsProfiles []*tlsfingerprint.Profile
}

func (u *quotaRedirectingUpstream) Do(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
) (*http.Response, error) {
	cloned := request.Clone(request.Context())
	cloned.URL.Scheme = u.target.Scheme
	cloned.URL.Host = u.target.Host
	return http.DefaultClient.Do(cloned)
}

func (u *quotaRedirectingUpstream) DoWithTLS(
	request *http.Request,
	proxyURL string,
	accountID int64,
	accountConcurrency int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	var body []byte
	if request.Body != nil {
		body, _ = io.ReadAll(request.Body)
		request.Body = io.NopCloser(strings.NewReader(string(body)))
	}
	snapshot := request.Clone(request.Context())
	snapshot.Header = request.Header.Clone()
	u.mu.Lock()
	u.requests = append(u.requests, snapshot)
	u.bodies = append(u.bodies, append([]byte(nil), body...))
	u.tlsProfiles = append(u.tlsProfiles, profile)
	u.mu.Unlock()
	return u.Do(request, proxyURL, accountID, accountConcurrency)
}

// newQuotaRedirectingUpstream 构造不添加任何浏览器头的测试 HTTPUpstream。
func newQuotaRedirectingUpstream(srv *httptest.Server) *quotaRedirectingUpstream {
	targetURL, _ := url.Parse(srv.URL)
	return &quotaRedirectingUpstream{target: targetURL}
}

// ── Part A: buildCodexSparkWindowExtraUpdates ─────────────────────────────────

// TestBuildCodexSparkWindowExtraUpdates_ContainsCodexKeys 验证:
//   - 产出包含 codex_5h_used_percent / codex_7d_used_percent
//   - 不含任何 codex_spark_ 前缀的 key（Method Z 前缀已禁止）
//   - 数值正确映射（primary 较短→5h，secondary 较长→7d）
func TestBuildCodexSparkWindowExtraUpdates_ContainsCodexKeys(t *testing.T) {
	now := time.Now().UTC()
	usage := &OpenAIQuotaUsage{
		AdditionalRateLimits: []OpenAIAdditionalRateLimit{
			{
				MeteredFeature: "codex_bengalfox",
				RateLimit: &OpenAIRateLimit{
					PrimaryWindow: &OpenAIRateLimitWindow{
						UsedPercent:        0.42,
						LimitWindowSeconds: 18000, // 300 min = 5 h
						ResetAfterSeconds:  3600,
						usedPercentPresent: true, limitWindowPresent: true, resetAfterPresent: true,
					},
					SecondaryWindow: &OpenAIRateLimitWindow{
						UsedPercent:        0.15,
						LimitWindowSeconds: 604800, // 7 d
						ResetAfterSeconds:  86400,
						usedPercentPresent: true, limitWindowPresent: true, resetAfterPresent: true,
					},
				},
			},
		},
	}

	updates := buildCodexSparkWindowExtraUpdates(usage, now)
	require.NotNil(t, updates, "expected non-nil updates for valid codex_bengalfox entry")

	// 必须含有 codex_5h_* 和 codex_7d_* 键
	require.Contains(t, updates, "codex_5h_used_percent")
	require.Contains(t, updates, "codex_7d_used_percent")

	// 任何键不得含有 codex_spark_ 前缀（Method Z 已禁止）
	for k := range updates {
		require.False(t, strings.Contains(k, "codex_spark_"),
			"unexpected Method-Z prefix in key: %s", k)
	}

	// 数值验证（primary=5h, secondary=7d）
	require.InDelta(t, 0.42, updates["codex_5h_used_percent"], 1e-9)
	require.InDelta(t, 0.15, updates["codex_7d_used_percent"], 1e-9)
}

// TestBuildCodexSparkWindowExtraUpdates_NilUsage 验证 nil usage 返回 nil。
func TestBuildCodexSparkWindowExtraUpdates_NilUsage(t *testing.T) {
	require.Nil(t, buildCodexSparkWindowExtraUpdates(nil, time.Now()))
}

// TestBuildCodexSparkWindowExtraUpdates_NoBengalfox 验证无 codex_bengalfox 条目时返回 nil。
func TestBuildCodexSparkWindowExtraUpdates_NoBengalfox(t *testing.T) {
	usage := &OpenAIQuotaUsage{
		AdditionalRateLimits: []OpenAIAdditionalRateLimit{
			{MeteredFeature: "other_feature", RateLimit: &OpenAIRateLimit{}},
		},
	}
	require.Nil(t, buildCodexSparkWindowExtraUpdates(usage, time.Now()))
}

// ── Part C: ResetCredit 影子拒绝 ───────────────────────────────────────────

// TestResetCreditShadowRejected 验证:
//   - ResetCredit(ctx, shadowID) 返回 ErrSparkShadowResetNotSupported
//   - 不触达上游（privacyClientFactory 为 nil，若调用则 panic）
func TestResetCreditShadowRejected(t *testing.T) {
	pid := int64(100)
	shadow := &Account{
		ID:              200,
		ParentAccountID: &pid,
		Platform:        PlatformOpenAI,
		Type:            AccountTypeOAuth,
		QuotaDimension:  QuotaDimensionSpark,
	}
	repo := &stubQuotaAccountRepo{
		accounts: map[int64]*Account{200: shadow},
	}
	// privacyClientFactory 故意为 nil —— 若流程误到上游则 prepareUpstreamCall 会先在
	// 配置检查处报错，但我们在此之前就应该拦截并返回 ErrSparkShadowResetNotSupported。
	svc := &OpenAIQuotaService{accountRepo: repo}

	_, err := svc.ResetCredit(context.Background(), 200)
	require.ErrorIs(t, err, ErrSparkShadowResetNotSupported,
		"shadow ResetCredit should return ErrSparkShadowResetNotSupported, got: %v", err)
	// 外审 F6:必须是结构化 409(而非裸 error→500)。
	require.Equal(t, http.StatusConflict, infraerrors.Code(err),
		"shadow ResetCredit 应映射为 409 Conflict 而非 500")
}

func TestResetCreditTargetedSendsStableCreditAndRedeemIDs(t *testing.T) {
	account := &Account{
		ID: 203, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"chatgpt_account_id": "account-targeted"},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenCache := &stubQuotaTokenCache{tokens: map[string]string{OpenAITokenCacheKey(account): "fake-token"}}
	tokenProvider := NewOpenAITokenProvider(repo, tokenCache, nil)
	var body map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/backend-api/wham/rate-limit-reset-credits/consume", r.URL.Path)
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		w.Header().Set("content-type", "application/json")
		_, _ = w.Write([]byte(`{"code":"ok","windows_reset":1}`))
	}))
	defer srv.Close()

	svc := NewOpenAIQuotaService(repo, nil, tokenProvider, newQuotaRedirectingFactory(srv))
	result, err := svc.ResetCreditTargeted(context.Background(), account.ID, "credit-123", "redeem-456")
	require.NoError(t, err)
	require.Equal(t, "ok", result.Code)
	require.Equal(t, map[string]string{
		"credit_id": "credit-123", "redeem_request_id": "redeem-456",
	}, body)
}

func TestResetCreditAgentIdentityUsesAssertionAndRecoversInvalidTaskOnce(t *testing.T) {
	configureObserveGuardForLocalHTTPTest(t)
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	der, err := x509.MarshalPKCS8PrivateKey(privateKey)
	require.NoError(t, err)
	account := &Account{
		ID:       201,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"auth_mode":          OpenAIAuthModeAgentIdentity,
			"agent_runtime_id":   "runtime-reset-recovery",
			"agent_private_key":  base64.StdEncoding.EncodeToString(der),
			"task_id":            "task-reset-old",
			"chatgpt_account_id": "account-reset-recovery",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	resetCalls := 0
	registerCalls := 0
	var assertions []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		if strings.Contains(r.URL.Path, "/task/register") {
			registerCalls++
			_, _ = w.Write([]byte(`{"task_id":"task-reset-new"}`))
			return
		}
		resetCalls++
		assertions = append(assertions, r.Header.Get("authorization"))
		require.Equal(t, "account-reset-recovery", r.Header.Get("chatgpt-account-id"))
		if resetCalls == 1 {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":{"code":"invalid_task_id"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"code":"ok","windows_reset":2}`))
	}))
	defer srv.Close()
	oldBase := openAIAgentIdentityAuthAPIBaseURL
	openAIAgentIdentityAuthAPIBaseURL = srv.URL
	t.Cleanup(func() { openAIAgentIdentityAuthAPIBaseURL = oldBase })

	invalidator := &agentIdentityWSInvalidationRecorder{}
	svc := NewOpenAIQuotaService(repo, nil, nil, newQuotaRedirectingUpstream(srv))
	svc.agentIdentityWS = invalidator

	result, err := svc.ResetCredit(context.Background(), account.ID)
	require.NoError(t, err)
	require.Equal(t, "ok", result.Code)
	require.Equal(t, 2, result.WindowsReset)
	require.Equal(t, 2, resetCalls)
	require.Equal(t, 1, registerCalls)
	require.Len(t, assertions, 2)
	require.True(t, strings.HasPrefix(assertions[0], "AgentAssertion "))
	require.True(t, strings.HasPrefix(assertions[1], "AgentAssertion "))
	require.NotEqual(t, assertions[0], assertions[1])
	require.Equal(t, "task-reset-new", account.GetCredential("task_id"))
	require.Equal(t, []int64{account.ID}, invalidator.accountIDs)
}

func TestResetCreditAgentIdentityReusesConcurrentlyRecoveredTask(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	der, err := x509.MarshalPKCS8PrivateKey(privateKey)
	require.NoError(t, err)
	account := &Account{
		ID:       202,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"auth_mode":          OpenAIAuthModeAgentIdentity,
			"agent_runtime_id":   "runtime-reset-concurrent",
			"agent_private_key":  base64.StdEncoding.EncodeToString(der),
			"task_id":            "task-reset-old",
			"chatgpt_account_id": "account-reset-concurrent",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	resetCalls := 0
	registerCalls := 0
	var assertions []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		if strings.Contains(r.URL.Path, "/task/register") {
			registerCalls++
			_, _ = w.Write([]byte(`{"task_id":"task-reset-unexpected"}`))
			return
		}
		resetCalls++
		assertions = append(assertions, r.Header.Get("authorization"))
		if resetCalls == 1 {
			credentials := shallowCopyMap(account.Credentials)
			credentials["task_id"] = "task-reset-concurrent"
			account.Credentials = credentials
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":{"code":"invalid_task_id"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"code":"ok","windows_reset":1}`))
	}))
	defer srv.Close()
	oldBase := openAIAgentIdentityAuthAPIBaseURL
	openAIAgentIdentityAuthAPIBaseURL = srv.URL
	t.Cleanup(func() { openAIAgentIdentityAuthAPIBaseURL = oldBase })

	svc := NewOpenAIQuotaService(repo, nil, nil, newQuotaRedirectingUpstream(srv))
	result, err := svc.ResetCredit(context.Background(), account.ID)
	require.NoError(t, err)
	require.Equal(t, "ok", result.Code)
	require.Equal(t, 2, resetCalls)
	require.Zero(t, registerCalls)
	require.Equal(t, "task-reset-old", decodeAgentAssertionTask(t, assertions[0]))
	require.Equal(t, "task-reset-concurrent", decodeAgentAssertionTask(t, assertions[1]))
}

// ── Part B: prepareUpstreamCall 影子 resolve ──────────────────────────────

// TestPrepareUpstreamCallShadowResolve 验证影子账号（200）QueryUsage 时:
//   - 不因 chatgpt_account_id 为空而报错
//   - 使用母账号（100）的 chatgpt_account_id("org-parent123")
//
// 测试策略: 直接调用包内可见的 prepareUpstreamCall，注入 stubTokenCache（命中路径）
// 和 stubQuotaAccountRepo（同时持有影子+母账号），绕开 /wham/usage HTTP 往返。
// 这比 httptest 端到端 mock 更轻量且对实现细节的耦合更低。
func TestPrepareUpstreamCallShadowResolve(t *testing.T) {
	ctx := context.Background()
	pid := int64(100)

	// 影子账号：无 chatgpt_account_id credentials
	shadow := &Account{
		ID:              200,
		ParentAccountID: &pid,
		Platform:        PlatformOpenAI,
		Type:            AccountTypeOAuth,
		Status:          StatusActive,
		QuotaDimension:  QuotaDimensionSpark,
	}
	// 母账号：有完整 credentials
	parent := &Account{
		ID:       100,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "org-parent123",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{200: shadow, 100: parent}}

	// stubTokenCache 为母账号 cache key 提供 fake token（走缓存命中路径，无需真实刷新）
	tokenCache := &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(parent): "fake-access-token",
	}}
	tokenProvider := NewOpenAITokenProvider(repo, tokenCache, nil)

	// HTTPUpstream 只用于配置完整性检查；prepareUpstreamCall 不会真正发起请求。
	svc := NewOpenAIQuotaService(repo, nil, tokenProvider, &quotaRedirectingUpstream{
		target: &url.URL{Scheme: "http", Host: "127.0.0.1:1"},
	})

	_, chatGPTAccountID, _, _, err := svc.prepareUpstreamCall(ctx, 200)
	require.NoError(t, err, "shadow resolve should succeed; got error: %v", err)
	require.Equal(t, "org-parent123", chatGPTAccountID,
		"prepareUpstreamCall should use parent's chatgpt_account_id after shadow resolve")
}

func TestQueryUsageAgentIdentityUsesAssertionWithoutOAuthToken(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	der, err := x509.MarshalPKCS8PrivateKey(privateKey)
	require.NoError(t, err)
	account := &Account{
		ID:       300,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"auth_mode":                  OpenAIAuthModeAgentIdentity,
			"agent_runtime_id":           "runtime-quota",
			"agent_private_key":          base64.StdEncoding.EncodeToString(der),
			"task_id":                    "task-quota",
			"chatgpt_account_id":         "account-quota",
			"chatgpt_account_is_fedramp": true,
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	var authorization string
	var accountHeader string
	var fedrampHeader string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization = r.Header.Get("authorization")
		accountHeader = r.Header.Get("chatgpt-account-id")
		fedrampHeader = r.Header.Get("x-openai-fedramp")
		w.Header().Set("content-type", "application/json")
		_, _ = w.Write([]byte(`{"plan_type":"pro","rate_limit":{"allowed":true}}`))
	}))
	defer srv.Close()
	svc := NewOpenAIQuotaService(repo, nil, nil, newQuotaRedirectingUpstream(srv))
	usage, err := svc.QueryUsage(context.Background(), account.ID)
	require.NoError(t, err)
	require.NotNil(t, usage)
	require.True(t, strings.HasPrefix(authorization, "AgentAssertion "))
	require.Equal(t, "account-quota", accountHeader)
	require.Equal(t, "true", fedrampHeader)
}

func TestQueryUsageAgentIdentityRecoversInvalidTaskOnce(t *testing.T) {
	configureObserveGuardForLocalHTTPTest(t)
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	der, err := x509.MarshalPKCS8PrivateKey(privateKey)
	require.NoError(t, err)
	account := &Account{
		ID:       301,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"auth_mode":          OpenAIAuthModeAgentIdentity,
			"agent_runtime_id":   "runtime-quota-recovery",
			"agent_private_key":  base64.StdEncoding.EncodeToString(der),
			"task_id":            "task-quota-old",
			"chatgpt_account_id": "account-quota-recovery",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	usageCalls := 0
	registerCalls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		if strings.Contains(r.URL.Path, "/task/register") {
			registerCalls++
			_, _ = w.Write([]byte(`{"task_id":"task-quota-new"}`))
			return
		}
		if strings.Contains(r.URL.Path, "rate-limit-reset-credits") {
			_, _ = w.Write([]byte(`{}`))
			return
		}
		if strings.Contains(r.URL.Path, "/settings/user") {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{}`))
			return
		}
		usageCalls++
		if usageCalls == 1 {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":{"code":"invalid_task_id"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"plan_type":"pro","rate_limit":{"allowed":true}}`))
	}))
	defer srv.Close()
	oldBase := openAIAgentIdentityAuthAPIBaseURL
	openAIAgentIdentityAuthAPIBaseURL = srv.URL
	t.Cleanup(func() { openAIAgentIdentityAuthAPIBaseURL = oldBase })

	invalidator := &agentIdentityWSInvalidationRecorder{}
	svc := NewOpenAIQuotaService(repo, nil, nil, newQuotaRedirectingUpstream(srv))
	svc.agentIdentityWS = invalidator
	usage, err := svc.QueryUsage(context.Background(), account.ID)
	require.NoError(t, err)
	require.NotNil(t, usage)
	require.Equal(t, 2, usageCalls)
	require.Equal(t, 1, registerCalls)
	require.Equal(t, "task-quota-new", account.GetCredential("task_id"))
	require.Equal(t, []int64{account.ID}, invalidator.accountIDs)
}

func TestParseOpenAIRateLimitResetCreditDetails_CompatibleContainers(t *testing.T) {
	tests := []struct {
		name string
		body string
		want []string
	}{
		{
			name: "credits",
			body: `{"credits":[{"id":"secret-id","expires_at":"2026-07-03T04:05:06Z"}]}`,
			want: []string{"2026-07-03T04:05:06Z"},
		},
		{
			name: "rate limit reset credits",
			body: `{"rate_limit_reset_credits":[{"expiresAt":"2026-07-04T04:05:06Z"}]}`,
			want: []string{"2026-07-04T04:05:06Z"},
		},
		{
			name: "items",
			body: `{"items":[{"expires_at":"2026-07-05T04:05:06Z"}]}`,
			want: []string{"2026-07-05T04:05:06Z"},
		},
		{
			name: "data",
			body: `{"data":[{"expires_at":"2026-07-06T04:05:06Z"}]}`,
			want: []string{"2026-07-06T04:05:06Z"},
		},
		{
			name: "array",
			body: `[{"expires_at":"2026-07-07T04:05:06Z"}]`,
			want: []string{"2026-07-07T04:05:06Z"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseOpenAIRateLimitResetCreditDetails([]byte(tt.body))
			require.NoError(t, err)
			require.Len(t, got.Credits, len(tt.want))
			for i := range tt.want {
				require.Equal(t, tt.want[i], got.Credits[i].ExpiresAt)
			}
			encoded, err := json.Marshal(got.Credits)
			require.NoError(t, err)
			require.NotContains(t, string(encoded), "secret-id")
		})
	}
}

func TestQueryUsageIncludesResetCreditExpirations_EndToEnd(t *testing.T) {
	ctx := context.Background()
	account := &Account{
		ID:       100,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "org-parent123",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{100: account}}
	tokenCache := &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "fake-token",
	}}
	tokenProvider := NewOpenAITokenProvider(repo, tokenCache, nil)

	var capturedHeaders http.Header
	var detailCalls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		switch r.URL.Path {
		case "/backend-api/wham/usage":
			_ = json.NewEncoder(w).Encode(OpenAIQuotaUsage{
				RateLimitResetCredits: &OpenAIRateLimitResetCredits{AvailableCount: 2},
			})
		case "/backend-api/wham/rate-limit-reset-credits":
			detailCalls++
			capturedHeaders = r.Header.Clone()
			require.Equal(t, "org-parent123", r.Header.Get("ChatGPT-Account-ID"))
			_, _ = w.Write([]byte(`{"credits":[{"id":"secret-credit-id","expires_at":"2026-07-03T04:05:06Z"},{"expiresAt":"2026-07-04T04:05:06Z"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	svc := NewOpenAIQuotaService(repo, nil, tokenProvider, newQuotaRedirectingUpstream(srv))
	usage, err := svc.QueryUsage(ctx, 100)
	require.NoError(t, err)
	require.NotNil(t, usage)
	require.NotNil(t, usage.RateLimitResetCredits)
	require.Equal(t, 2, usage.RateLimitResetCredits.AvailableCount)
	require.Equal(t, 1, detailCalls)
	require.Empty(t, capturedHeaders.Get("OpenAI-Beta"))
	require.Equal(t, activeOpenAICodexUserAgentForTest(), capturedHeaders.Get("User-Agent"))
	require.Equal(t, "*/*", capturedHeaders.Get("Accept"))
	require.Empty(t, capturedHeaders.Get("Version"))
	require.Empty(t, capturedHeaders.Get("Originator"))
	require.Empty(t, capturedHeaders.Get("Sec-Fetch-Site"))
	require.Equal(t, []OpenAIRateLimitResetCreditDetail{
		{ExpiresAt: "2026-07-03T04:05:06Z"},
		{ExpiresAt: "2026-07-04T04:05:06Z"},
	}, usage.RateLimitResetCredits.Credits)
	require.NoError(t, svc.CacheResetCreditsSnapshot(ctx, 100, usage.RateLimitResetCredits))
	require.Equal(t, &OpenAIRateLimitResetCredits{
		AvailableCount: 2,
		Credits: []OpenAIRateLimitResetCreditDetail{
			{ExpiresAt: "2026-07-03T04:05:06Z"},
			{ExpiresAt: "2026-07-04T04:05:06Z"},
		},
	}, repo.extraUpdates[100][openaiQuotaResetCreditsKey])

	encoded, err := json.Marshal(usage)
	require.NoError(t, err)
	require.NotContains(t, string(encoded), "secret-credit-id")
}

func TestQueryUsageResetCreditDetails401NonFatal(t *testing.T) {
	ctx := context.Background()
	account := &Account{
		ID:       100,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "org-parent123",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{100: account}}
	tokenCache := &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "fake-token",
	}}
	tokenProvider := NewOpenAITokenProvider(repo, tokenCache, nil)

	var detailCalls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		switch r.URL.Path {
		case "/backend-api/wham/usage":
			_ = json.NewEncoder(w).Encode(OpenAIQuotaUsage{
				RateLimitResetCredits: &OpenAIRateLimitResetCredits{AvailableCount: 1},
			})
		case "/backend-api/wham/rate-limit-reset-credits":
			detailCalls++
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"unauthorized","id":"secret-error-id"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	svc := NewOpenAIQuotaService(repo, nil, tokenProvider, newQuotaRedirectingUpstream(srv))
	usage, err := svc.QueryUsage(ctx, 100)
	require.NoError(t, err)
	require.NotNil(t, usage)
	require.NotNil(t, usage.RateLimitResetCredits)
	require.Equal(t, 1, usage.RateLimitResetCredits.AvailableCount)
	require.Equal(t, 1, detailCalls)
	require.Empty(t, usage.RateLimitResetCredits.Credits)

	// A count without expiration details must not be persisted (the reader could
	// never age it out), and the previous snapshot must survive untouched.
	require.Error(t, svc.CacheResetCreditsSnapshot(ctx, 100, usage.RateLimitResetCredits))
	require.Empty(t, repo.extraUpdates)
}

func TestCacheResetCreditsSnapshot(t *testing.T) {
	ctx := context.Background()

	t.Run("zero count allows an empty expiration list", func(t *testing.T) {
		repo := &stubQuotaAccountRepo{}
		svc := &OpenAIQuotaService{accountRepo: repo}
		credits := &OpenAIRateLimitResetCredits{AvailableCount: 0}

		require.NoError(t, svc.CacheResetCreditsSnapshot(ctx, 100, credits))
		require.Equal(t, credits, repo.extraUpdates[100][openaiQuotaResetCreditsKey])
	})

	t.Run("missing expiration list preserves the cache", func(t *testing.T) {
		repo := &stubQuotaAccountRepo{}
		svc := &OpenAIQuotaService{accountRepo: repo}

		err := svc.CacheResetCreditsSnapshot(ctx, 100, &OpenAIRateLimitResetCredits{AvailableCount: 1})

		require.Error(t, err)
		require.Empty(t, repo.extraUpdates)
	})

	t.Run("empty expiration list with a positive count preserves the cache", func(t *testing.T) {
		repo := &stubQuotaAccountRepo{}
		svc := &OpenAIQuotaService{accountRepo: repo}

		err := svc.CacheResetCreditsSnapshot(ctx, 100, &OpenAIRateLimitResetCredits{
			AvailableCount: 2,
			Credits:        []OpenAIRateLimitResetCreditDetail{},
		})

		require.Error(t, err)
		require.Empty(t, repo.extraUpdates)
	})

	t.Run("nil snapshot preserves the cache", func(t *testing.T) {
		repo := &stubQuotaAccountRepo{}
		svc := &OpenAIQuotaService{accountRepo: repo}

		require.Error(t, svc.CacheResetCreditsSnapshot(ctx, 100, nil))
		require.Empty(t, repo.extraUpdates)
	})

	t.Run("repository errors are returned", func(t *testing.T) {
		repo := &stubQuotaAccountRepo{extraUpdateErr: errors.New("database unavailable")}
		svc := &OpenAIQuotaService{accountRepo: repo}

		err := svc.CacheResetCreditsSnapshot(ctx, 100, &OpenAIRateLimitResetCredits{
			AvailableCount: 1,
			Credits:        []OpenAIRateLimitResetCreditDetail{{ExpiresAt: "2026-07-03T04:05:06Z"}},
		})

		require.ErrorContains(t, err, "database unavailable")
	})
}

func TestCodexWhamRequestsUseClosedBackendClientProfile(t *testing.T) {
	account := &Account{
		ID:       710,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-wham-profile",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-wham-profile",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		switch request.URL.Path {
		case "/backend-api/wham/settings/user":
			_, _ = writer.Write([]byte(`{}`))
		case "/backend-api/wham/usage":
			_, _ = writer.Write([]byte(`{}`))
		case "/backend-api/wham/rate-limit-reset-credits":
			_, _ = writer.Write([]byte(`{}`))
		case "/backend-api/wham/rate-limit-reset-credits/consume":
			_, _ = writer.Write([]byte(`{"code":"ok","windows_reset":1}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	upstream := newQuotaRedirectingUpstream(server)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	runtimeState := defaultOfficialCodexRuntimeState()
	runtimeState.ProfileMode = officialClientProfileModeActive
	runtimeState.SurfaceID = officialCodexSurfaceTUI
	runtimeState.Originator = "codex-tui"
	runtimeState.TerminalToken = "xterm-256color"
	runtimeState.UserAgentSuffixEnabled = false
	runtimeContext, err := withOfficialCodexRuntimeState(context.Background(), runtimeState)
	require.NoError(t, err)
	_, err = service.QueryUsage(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.ResetCredit(runtimeContext, account.ID)
	require.NoError(t, err)

	require.Len(t, upstream.requests, 4)
	require.Len(t, upstream.tlsProfiles, 4)
	expectedTargets := []string{
		"https://chatgpt.com/backend-api/wham/settings/user",
		"https://chatgpt.com/backend-api/wham/usage",
		"https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
		"https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
	}
	expectedEndpointIDs := []string{
		officialCodexEndpointWhamSettingsUser,
		officialCodexEndpointWhamUsage,
		officialCodexEndpointWhamResetCredits,
		officialCodexEndpointWhamConsumeResetCredit,
	}
	poolIDs := make([]string, 0, len(upstream.requests))
	for index, request := range upstream.requests {
		require.Equal(t, expectedTargets[index], request.URL.String())
		require.Equal(t, "chatgpt.com", request.Host)
		require.Equal(t, "Bearer token-wham-profile", request.Header.Get("Authorization"))
		require.Equal(t, "acct-wham-profile", request.Header.Get("Chatgpt-Account-Id"))
		require.Equal(t, "*/*", request.Header.Get("Accept"))
		require.Equal(
			t,
			"codex-tui/"+activeOpenAICodexVersionForTest()+" (Ubuntu 24.4.0; x86_64) xterm-256color",
			request.Header.Get("User-Agent"),
		)
		require.True(t, HTTPUpstreamRedirectsDisabled(request.Context()))
		require.Equal(t, HTTPUpstreamProfileOpenAI, HTTPUpstreamProfileFromContext(request.Context()))
		require.NotNil(t, upstream.tlsProfiles[index])
		require.True(t, upstream.tlsProfiles[index].Transport.StrictH1Wire)
		attempt, ok := officialegress.AttemptIdentityFromContext(request.Context())
		require.True(t, ok)
		require.Equal(t, expectedEndpointIDs[index], attempt.EndpointID)
		require.Equal(t, officialegress.SinkCodexQuotaWHAM, attempt.SinkID)
		require.NotEmpty(t, attempt.BundleDigest)
		poolIDs = append(poolIDs, attempt.ConnectionPoolDigest)
		matchedWireRule := false
		for _, rule := range upstream.tlsProfiles[index].Transport.H1HeaderOrders {
			if rule.Method == request.Method && rule.Path == request.URL.EscapedPath() {
				matchedWireRule = true
				break
			}
		}
		require.True(t, matchedWireRule, "WHAM 请求缺少 method/path 精确 H1 规则")

		headerNames := make([]string, 0, len(request.Header))
		for name := range request.Header {
			headerNames = append(headerNames, strings.ToLower(name))
		}
		expectedHeaders := []string{"user-agent", "authorization", "chatgpt-account-id", "accept"}
		if index == 0 {
			expectedHeaders = append(expectedHeaders, "cache-control")
		}
		if index == 3 {
			expectedHeaders = append(expectedHeaders, "content-type")
			require.Equal(t, "application/json", request.Header.Get("Content-Type"))
		} else {
			require.Empty(t, request.Header.Get("Content-Type"))
		}
		require.ElementsMatch(t, expectedHeaders, headerNames)
	}
	require.Equal(t, poolIDs[0], poolIDs[1])
	require.Equal(t, poolIDs[1], poolIDs[2])
	require.Equal(t, poolIDs[2], poolIDs[3])
	require.NotEmpty(t, poolIDs[0])
	processRuntime, err := resolveOfficialEgressRuntime(nil, upstream)
	require.NoError(t, err)
	require.Equal(
		t,
		uint64(2),
		processRuntime.BundleResolver.ResolveCount(),
		"QueryUsage 的两个 WHAM endpoint 必须共用一次解析；独立 ResetCredit 再解析一次",
	)

	require.Empty(t, upstream.bodies[0])
	require.Empty(t, upstream.bodies[1])
	require.Empty(t, upstream.bodies[2])
	var consumeBody map[string]string
	require.NoError(t, json.Unmarshal(upstream.bodies[3], &consumeBody))
	require.Len(t, consumeBody, 1)
	require.NotEmpty(t, consumeBody["redeem_request_id"])
}

// TestResetCreditGetByIDError_FailsClosed 验证守卫「失败关闭」语义：
// 当守卫的 GetByID 发生瞬时错误时，ResetCredit 必须立即返回该错误，
// 不得旁路进入 prepareUpstreamCall（否则影子账号会借 resolve 路径操作母账号）。
//
// 区分方法：privacyClientFactory/tokenProvider 留 nil；
//   - 旁路路径：prepareUpstreamCall 配置检查先命中，报 "not configured"
//   - 守卫正确关闭：报 "account not found"（来自守卫的 infraerrors）
func TestResetCreditGetByIDError_FailsClosed(t *testing.T) {
	// 空 map：GetByID(200) 返回 "account 200 not found"
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{}}
	// tokenProvider / privacyClientFactory 故意为 nil：
	// 若代码泄漏到 prepareUpstreamCall，会因配置检查而报 "not configured" 而非 "account not found"。
	svc := &OpenAIQuotaService{accountRepo: repo}

	_, err := svc.ResetCredit(context.Background(), 200)
	require.Error(t, err, "GetByID error must propagate; got nil")
	require.NotContains(t, err.Error(), "not configured",
		"error reached prepareUpstreamCall config-check — guard did not fail-closed; got: %v", err)
}

// TestQueryUsageShadowResolve_EndToEnd 是端到端补充：通过 httptest 服务真实 /wham/usage
// 路径，验证影子账号的 QueryUsage 能成功拿到服务器响应（header 由母账号注入）。
func TestQueryUsageShadowResolve_EndToEnd(t *testing.T) {
	ctx := context.Background()
	pid := int64(100)

	shadow := &Account{
		ID: 200, ParentAccountID: &pid,
		Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Status: StatusActive, QuotaDimension: QuotaDimensionSpark,
	}
	parent := &Account{
		ID: 100, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{"chatgpt_account_id": "org-e2e-parent"},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{200: shadow, 100: parent}}

	tokenCache := &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(parent): "fake-token-e2e",
	}}
	tokenProvider := NewOpenAITokenProvider(repo, tokenCache, nil)

	// httptest server 记录收到的 chatgpt-account-id header，返回空 usage JSON
	var capturedAccountID string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedAccountID = r.Header.Get("chatgpt-account-id")
		w.Header().Set("content-type", "application/json")
		_ = json.NewEncoder(w).Encode(OpenAIQuotaUsage{})
	}))
	defer srv.Close()

	svc := NewOpenAIQuotaService(repo, nil, tokenProvider, newQuotaRedirectingUpstream(srv))
	usage, err := svc.QueryUsage(ctx, 200)
	require.NoError(t, err)
	require.NotNil(t, usage)
	require.Equal(t, "org-e2e-parent", capturedAccountID,
		"upstream should receive parent's chatgpt-account-id; got: %s", capturedAccountID)
}
