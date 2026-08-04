package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

type changeset1BUsageRepo struct {
	AccountRepository
	accounts map[int64]*Account
	updates  chan map[string]any
}

type changeset1BConcurrentUsageRepo struct {
	AccountRepository
	account *Account
}

func (r *changeset1BConcurrentUsageRepo) GetByID(_ context.Context, _ int64) (*Account, error) {
	copyAccount := *r.account
	copyAccount.Credentials = shallowCopyMap(r.account.Credentials)
	copyAccount.Extra = shallowCopyMap(r.account.Extra)
	return &copyAccount, nil
}

func (r *changeset1BConcurrentUsageRepo) UpdateExtra(_ context.Context, _ int64, _ map[string]any) error {
	return nil
}

func (r *changeset1BUsageRepo) GetByID(_ context.Context, id int64) (*Account, error) {
	return r.accounts[id], nil
}

func (r *changeset1BUsageRepo) UpdateExtra(_ context.Context, _ int64, updates map[string]any) error {
	if r.updates != nil {
		r.updates <- updates
	}
	return nil
}

func decodeStrictWHAMFixture(t *testing.T, body string) *OpenAIQuotaUsage {
	t.Helper()
	var usage OpenAIQuotaUsage
	require.NoError(t, json.Unmarshal([]byte(body), &usage))
	return &usage
}

func TestBuildCodexGlobalWindowExtraUpdatesMapsByExactDuration(t *testing.T) {
	now := time.Date(2026, 8, 2, 9, 0, 0, 0, time.UTC)
	usage := decodeStrictWHAMFixture(t, `{
		"plan_type":"pro",
		"rate_limit":{
			"primary_window":{"used_percent":70,"limit_window_seconds":604800,"reset_after_seconds":7200},
			"secondary_window":{"used_percent":0,"limit_window_seconds":18000,"reset_after_seconds":900}
		}
	}`)

	updates, err := buildCodexGlobalWindowExtraUpdates(usage, now)
	require.NoError(t, err)
	require.Equal(t, float64(0), updates["codex_5h_used_percent"])
	require.Equal(t, float64(70), updates["codex_7d_used_percent"])
	require.Equal(t, 300, updates["codex_5h_window_minutes"])
	require.Equal(t, 10080, updates["codex_7d_window_minutes"])
}

func TestBuildCodexGlobalWindowExtraUpdatesRejectsUnknownOrIncompleteWindows(t *testing.T) {
	now := time.Date(2026, 8, 2, 9, 0, 0, 0, time.UTC)
	for _, testCase := range []struct {
		name       string
		body       string
		wantReason string
	}{
		{
			name: "30d 不得写成 7d",
			body: `{"rate_limit":{
				"primary_window":{"used_percent":10,"limit_window_seconds":18000,"reset_after_seconds":10},
				"secondary_window":{"used_percent":20,"limit_window_seconds":2592000,"reset_after_seconds":20}
			}}`,
			wantReason: "wham_window_unknown_2592000",
		},
		{
			name: "used_percent 缺失",
			body: `{"rate_limit":{
				"primary_window":{"limit_window_seconds":18000,"reset_after_seconds":10},
				"secondary_window":{"used_percent":20,"limit_window_seconds":604800,"reset_after_seconds":20}
			}}`,
			wantReason: "wham_window_incomplete",
		},
		{
			name: "7d 窗口缺失",
			body: `{"rate_limit":{
				"primary_window":{"used_percent":10,"limit_window_seconds":18000,"reset_after_seconds":10}
			}}`,
			wantReason: "wham_window_7d_missing",
		},
		{
			name: "reset_at 非法",
			body: `{"rate_limit":{
				"primary_window":{"used_percent":10,"limit_window_seconds":18000,"reset_at":0},
				"secondary_window":{"used_percent":20,"limit_window_seconds":604800,"reset_after_seconds":20}
			}}`,
			wantReason: "wham_window_reset_invalid",
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			updates, err := buildCodexGlobalWindowExtraUpdates(
				decodeStrictWHAMFixture(t, testCase.body),
				now,
			)
			require.Error(t, err)
			require.Nil(t, updates)
			require.Contains(t, err.Error(), testCase.wantReason)
		})
	}
}

func TestOpenAIWHAMMappingFallbackReasonUsesClosedReasonCodes(t *testing.T) {
	require.Equal(t, "wham_window_unknown_duration",
		openAIWHAMMappingFallbackReason(errors.New("wham_window_unknown_2592000")))
	require.Equal(t, "wham_window_duplicate_duration",
		openAIWHAMMappingFallbackReason(errors.New("wham_window_duplicate_18000")))
	require.Equal(t, "wham_window_incomplete",
		openAIWHAMMappingFallbackReason(errors.New("wham_window_incomplete")))
	require.Equal(t, "wham_response_invalid",
		openAIWHAMMappingFallbackReason(errors.New("unbounded upstream detail")))
}

func TestOpenAIQuotaQueryUsageOnlyMakesExactlyOneRequest(t *testing.T) {
	account := &Account{
		ID:       8101,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-usage-only",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "access-usage-only",
	}}, nil)

	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		calls.Add(1)
		require.True(t, strings.HasSuffix(request.URL.Path, "/backend-api/wham/usage"))
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{"plan_type":"pro","rate_limit":{
			"primary_window":{"used_percent":10,"limit_window_seconds":18000,"reset_after_seconds":10},
			"secondary_window":{"used_percent":20,"limit_window_seconds":604800,"reset_after_seconds":20}
		}}`))
	}))
	defer server.Close()

	service := NewOpenAIQuotaService(repo, nil, tokenProvider, newQuotaRedirectingUpstream(server))
	usage, err := service.QueryUsageOnly(context.Background(), account.ID)
	require.NoError(t, err)
	require.Equal(t, "pro", usage.PlanType)
	require.Equal(t, int32(1), calls.Load(), "usage-only 刷新不得附加查询 reset-credit details")
}

func TestRefreshOpenAICodexSnapshotUsesWHAMFirstForVerifiedPro(t *testing.T) {
	account := &Account{
		ID:       8201,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":       "access-pro",
			"chatgpt_account_id": "acct-pro",
			"plan_type":          "pro",
		},
	}
	repo := &changeset1BUsageRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "access-pro",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		require.True(t, strings.HasSuffix(request.URL.Path, "/backend-api/wham/usage"))
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{"plan_type":"pro","rate_limit":{
			"primary_window":{"used_percent":11,"limit_window_seconds":18000,"reset_after_seconds":100},
			"secondary_window":{"used_percent":22,"limit_window_seconds":604800,"reset_after_seconds":200}
		}}`))
	}))
	defer server.Close()
	upstream := newQuotaRedirectingUpstream(server)
	quota := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	service := &AccountUsageService{accountRepo: repo, openAIQuotaService: quota}

	updates, err := service.refreshOpenAICodexSnapshotWHAMFirst(context.Background(), account, time.Now())
	require.NoError(t, err)
	require.Equal(t, float64(11), updates["codex_5h_used_percent"])
	require.Equal(t, float64(22), updates["codex_7d_used_percent"])
	require.Len(t, upstream.requests, 1)
	require.Equal(t, "/backend-api/wham/usage", upstream.requests[0].URL.Path)
}

func TestRefreshOpenAICodexSnapshotFallsBackInSameCallThroughExecutor(t *testing.T) {
	account := &Account{
		ID:          8202,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "access-pro-fallback",
			"chatgpt_account_id": "acct-pro-fallback",
			"plan_type":          "pro",
		},
	}
	repo := &changeset1BUsageRepo{
		accounts: map[int64]*Account{account.ID: account},
		updates:  make(chan map[string]any, 2),
	}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "access-pro-fallback",
	}}, nil)
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		calls.Add(1)
		switch request.URL.Path {
		case "/backend-api/wham/usage":
			writer.WriteHeader(http.StatusNotFound)
			_, _ = writer.Write([]byte(`{"error":"unsupported"}`))
		case "/backend-api/codex/responses":
			writer.Header().Set("content-type", "text/event-stream")
			writer.Header().Set("x-codex-primary-used-percent", "44")
			writer.Header().Set("x-codex-primary-window-minutes", "300")
			writer.Header().Set("x-codex-primary-reset-after-seconds", "100")
			writer.Header().Set("x-codex-secondary-used-percent", "55")
			writer.Header().Set("x-codex-secondary-window-minutes", "10080")
			writer.Header().Set("x-codex-secondary-reset-after-seconds", "200")
			_, _ = io.WriteString(writer, "event: response.completed\ndata: {}\n\n")
		default:
			t.Fatalf("未预期的上游路径: %s", request.URL.Path)
		}
	}))
	defer server.Close()
	upstream := newQuotaRedirectingUpstream(server)
	quota := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	service := &AccountUsageService{accountRepo: repo, openAIQuotaService: quota}

	updates, err := service.refreshOpenAICodexSnapshotWHAMFirst(context.Background(), account, time.Now())
	require.NoError(t, err)
	require.Equal(t, float64(44), updates["codex_5h_used_percent"])
	require.Equal(t, float64(55), updates["codex_7d_used_percent"])
	require.Equal(t, int32(2), calls.Load(), "WHAM 不兼容时必须在同一次刷新内 fallback")
	require.Len(t, upstream.requests, 2)
	responsesRequest := upstream.requests[1]
	require.Equal(t, "/backend-api/codex/responses", responsesRequest.URL.Path)
	identity, ok := officialegress.AttemptIdentityFromContext(responsesRequest.Context())
	require.True(t, ok)
	require.Equal(t, officialEgressSinkUsageProbe, identity.SinkID)
	require.True(t, identity.HasFinalizationToken,
		"Responses fallback 必须通过 Codex Executor，不得使用普通 Go TLS")
	require.NotNil(t, upstream.tlsProfiles[1])
}

func TestOpenAIUsageForceCoalescesAndCannotBypassExecutorLease(t *testing.T) {
	account := &Account{
		ID:          8203,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 8,
		Credentials: map[string]any{
			"access_token":       "access-force-fallback",
			"chatgpt_account_id": "acct-force-fallback",
			"plan_type":          "go",
		},
		Extra: map[string]any{},
	}
	repo := &changeset1BConcurrentUsageRepo{account: account}
	var calls atomic.Int32
	started := make(chan struct{})
	releaseRequest := make(chan struct{})
	var startOnce sync.Once
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/backend-api/codex/responses" {
			writer.WriteHeader(http.StatusNotFound)
			return
		}
		calls.Add(1)
		startOnce.Do(func() { close(started) })
		<-releaseRequest
		writer.Header().Set("content-type", "text/event-stream")
		writer.Header().Set("x-codex-primary-used-percent", "12")
		writer.Header().Set("x-codex-primary-window-minutes", "300")
		writer.Header().Set("x-codex-primary-reset-after-seconds", "100")
		writer.Header().Set("x-codex-secondary-used-percent", "34")
		writer.Header().Set("x-codex-secondary-window-minutes", "10080")
		writer.Header().Set("x-codex-secondary-reset-after-seconds", "200")
		_, _ = io.WriteString(writer, "event: response.completed\ndata: {}\n\n")
	}))
	defer server.Close()
	upstream := newQuotaRedirectingUpstream(server)
	service := &AccountUsageService{
		accountRepo:        repo,
		openAIQuotaService: &OpenAIQuotaService{httpUpstream: upstream},
		cache:              NewUsageCache(),
	}

	errorsCh := make(chan error, 2)
	for range 2 {
		go func() {
			_, err := service.GetUsage(context.Background(), account.ID, true)
			errorsCh <- err
		}()
	}
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("等待首个计费 fallback 请求超时")
	}
	time.Sleep(50 * time.Millisecond)
	close(releaseRequest)
	for range 2 {
		require.NoError(t, <-errorsCh)
	}
	require.Equal(t, int32(1), calls.Load(),
		"并发 force 必须由账号级 singleflight 合并")

	_, err := service.GetUsage(context.Background(), account.ID, true)
	require.NoError(t, err)
	require.Equal(t, int32(1), calls.Load(),
		"顺序 force 可以绕过快照缓存，但不得绕过 Executor 最短间隔 lease")
}
