package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

// privacyProductionCapture 直接位于 PrivacyClientFactory 的 transport 边界，
// 因而捕获的是三个生产函数完成 method/URL/query/header 构造后的真实请求。
type privacyProductionCapture struct {
	mu       sync.Mutex
	requests []*http.Request
}

func (c *privacyProductionCapture) RoundTrip(request *http.Request) (*http.Response, error) {
	c.mu.Lock()
	c.requests = append(c.requests, request.Clone(request.Context()))
	c.mu.Unlock()

	body := `{}`
	switch request.URL.Path {
	case "/backend-api/accounts/check/v4-2023-04-27":
		body = `{"accounts":{"org-test":{"account":{"plan_type":"plus","is_default":true},"entitlement":{"expires_at":"2099-01-02T03:04:05Z"}}}}`
	case "/backend-api/subscriptions":
		body = `{"plan_type":"plus","active_until":"2099-01-02T03:04:05Z","will_renew":true,"id":"sub-test"}`
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    request,
	}, nil
}

func (c *privacyProductionCapture) factory(proxyURLs, rolloutKeys *[]string) PrivacyClientFactory {
	return func(request PrivacyClientRequest) (*PrivacyHTTPClient, error) {
		*proxyURLs = append(*proxyURLs, request.ProxyURL)
		*rolloutKeys = append(*rolloutKeys, request.RolloutKey)
		client := req.NewClient()
		client.GetClient().Transport = c
		return &PrivacyHTTPClient{
			Client:          client,
			Persona:         "chrome_133_xhr",
			FailureCooldown: time.Hour,
		}, nil
	}
}

func (c *privacyProductionCapture) snapshot() []*http.Request {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]*http.Request, len(c.requests))
	copy(out, c.requests)
	return out
}

// TestPrivacyProductionFunctionsBuildAllThreeBrowserRequests 修复变更集 0 早期测试
// “手工复刻 header、没有调用生产函数”的假覆盖。这里直接执行三个包私有生产函数；
// URL 虽为生产常量，也能通过 PrivacyClientFactory 注入的 transport 安全捕获，无需
// 修改生产代码或访问网络。
func TestPrivacyProductionFunctionsBuildAllThreeBrowserRequests(t *testing.T) {
	capture := &privacyProductionCapture{}
	var proxyURLs []string
	var rolloutKeys []string
	factory := capture.factory(&proxyURLs, &rolloutKeys)
	ctx := context.Background()
	rolloutKey := buildOpenAIPrivacyRolloutKey("account-stable-test")

	require.Equal(t, PrivacyModeTrainingOff,
		ensureOpenAITraining(
			ctx,
			factory,
			"access-test",
			"http://proxy.example:8080",
			openAIPrivacyEnsureInput{RolloutKey: rolloutKey},
		).Mode)
	account := fetchChatGPTAccountInfo(
		ctx, factory, "access-test", "http://proxy.example:8080", "org-test", rolloutKey,
	)
	require.NotNil(t, account)
	require.Equal(t, "plus", account.PlanType)
	require.Equal(t, "2099-01-02T03:04:05Z",
		fetchChatGPTSubscriptionExpiresAt(
			ctx, factory, "access-test", "http://proxy.example:8080", "org-test", rolloutKey,
		))

	require.Equal(t, []string{
		"http://proxy.example:8080",
		"http://proxy.example:8080",
		"http://proxy.example:8080",
	}, proxyURLs, "三个生产函数必须把代理配置原样交给工厂")
	require.Equal(t, []string{rolloutKey, rolloutKey, rolloutKey}, rolloutKeys,
		"同一账号三个 privacy 端点必须共用完全相同的分桶键")

	requests := capture.snapshot()
	require.Len(t, requests, 3)
	byPath := make(map[string]*http.Request, len(requests))
	for _, request := range requests {
		byPath[request.URL.Path] = request
		require.Equal(t, "chatgpt.com", request.URL.Host)
		require.Equal(t, "Bearer access-test", request.Header.Get("Authorization"))
		require.Equal(t, "https://chatgpt.com", request.Header.Get("Origin"))
		require.Equal(t, "https://chatgpt.com/", request.Header.Get("Referer"))
		require.Equal(t, "application/json", request.Header.Get("Accept"))
	}
	expectedSinkByPath := map[string]officialegress.SinkID{
		"/backend-api/settings/account_user_setting": officialegress.SinkWebPrivacyDisableTraining,
		"/backend-api/accounts/check/v4-2023-04-27":  officialegress.SinkWebPrivacyAccountInfo,
		"/backend-api/subscriptions":                 officialegress.SinkWebPrivacySubscription,
	}
	for path, expectedSinkID := range expectedSinkByPath {
		identity, ok := officialegress.AttemptIdentityFromContext(byPath[path].Context())
		require.True(t, ok, "privacy 请求 %s 缺少运行时 SinkID", path)
		require.Equal(t, expectedSinkID, identity.SinkID)
		require.Equal(t, officialegress.PersonaChatGPTWeb, identity.DeclaredPersona)
		require.False(t, identity.HasFinalizationToken, "1A legacy 请求不得伪造 FinalizationToken")
	}

	settings := byPath["/backend-api/settings/account_user_setting"]
	require.NotNil(t, settings)
	require.Equal(t, http.MethodPatch, settings.Method)
	require.Equal(t, "training_allowed", settings.URL.Query().Get("feature"))
	require.Equal(t, "false", settings.URL.Query().Get("value"))
	require.Equal(t, "cors", settings.Header.Get("sec-fetch-mode"))
	require.Equal(t, "same-origin", settings.Header.Get("sec-fetch-site"))
	require.Equal(t, "empty", settings.Header.Get("sec-fetch-dest"))

	accounts := byPath["/backend-api/accounts/check/v4-2023-04-27"]
	require.NotNil(t, accounts)
	require.Equal(t, http.MethodGet, accounts.Method)
	require.Equal(t, "cors", accounts.Header.Get("sec-fetch-mode"))
	require.Equal(t, "same-origin", accounts.Header.Get("sec-fetch-site"))
	require.Equal(t, "empty", accounts.Header.Get("sec-fetch-dest"))

	subscriptions := byPath["/backend-api/subscriptions"]
	require.NotNil(t, subscriptions)
	require.Equal(t, http.MethodGet, subscriptions.Method)
	require.Equal(t, "org-test", subscriptions.URL.Query().Get("account_id"))
	require.Equal(t, "cors", subscriptions.Header.Get("sec-fetch-mode"))
	require.Equal(t, "same-origin", subscriptions.Header.Get("sec-fetch-site"))
	require.Equal(t, "empty", subscriptions.Header.Get("sec-fetch-dest"))
}

func TestPrivacyRolloutKeyStableAcrossTokenRotationAndEndpoints(t *testing.T) {
	capture := &privacyProductionCapture{}
	var proxyURLs []string
	var rolloutKeys []string
	factory := capture.factory(&proxyURLs, &rolloutKeys)
	rolloutKey := buildOpenAIPrivacyRolloutKey("acct-token-rotation")

	for _, accessToken := range []string{"access-token-before", "access-token-after"} {
		result := ensureOpenAITraining(
			context.Background(),
			factory,
			accessToken,
			"",
			openAIPrivacyEnsureInput{RolloutKey: rolloutKey},
		)
		require.Equal(t, PrivacyModeTrainingOff, result.Mode)
		require.NotNil(t, fetchChatGPTAccountInfo(
			context.Background(), factory, accessToken, "", "org-test", rolloutKey,
		))
		require.NotEmpty(t, fetchChatGPTSubscriptionExpiresAt(
			context.Background(), factory, accessToken, "", "org-test", rolloutKey,
		))
	}

	require.Len(t, rolloutKeys, 6)
	for _, actual := range rolloutKeys {
		require.Equal(t, rolloutKey, actual,
			"access token 轮换及端点变化不得改变账号实验分组")
	}
}
