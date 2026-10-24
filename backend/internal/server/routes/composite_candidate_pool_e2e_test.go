//go:build unit

package routes

// 账号级候选池端到端用例：gin 路由 + compositeTargetPlatformMiddleware + CompositeRouteResolver +
// 候选池 + 真实的 OpenAI 网关处理器与调度器声明闸门 + 出站桩。
// 出站桩只记录请求命中的账号与上游地址，并按地址返回对应协议的合法响应体。

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/handler"
	servermiddleware "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

const (
	poolE2EGroupID = int64(9101)
	poolE2EModel   = "deepseek-flash"
)

// poolE2EAccounts 按平台过滤的账号仓储桩（调度器按平台取账号）。
type poolE2EAccounts struct {
	service.AccountRepository
	accounts []service.Account
}

func (r poolE2EAccounts) GetByID(_ context.Context, id int64) (*service.Account, error) {
	for i := range r.accounts {
		if r.accounts[i].ID == id {
			account := r.accounts[i]
			return &account, nil
		}
	}
	return nil, service.ErrNoAvailableAccounts
}

func (r poolE2EAccounts) byPlatform(platform string) []service.Account {
	var out []service.Account
	for _, account := range r.accounts {
		if account.Platform == platform {
			out = append(out, account)
		}
	}
	return out
}

func (r poolE2EAccounts) ListSchedulableByPlatform(_ context.Context, platform string) ([]service.Account, error) {
	return r.byPlatform(platform), nil
}

func (r poolE2EAccounts) ListSchedulableUngroupedByPlatform(_ context.Context, platform string) ([]service.Account, error) {
	return r.byPlatform(platform), nil
}

func (r poolE2EAccounts) ListSchedulableByGroupIDAndPlatform(_ context.Context, _ int64, platform string) ([]service.Account, error) {
	return r.byPlatform(platform), nil
}

func (r poolE2EAccounts) ListSchedulableByGroupID(context.Context, int64) ([]service.Account, error) {
	return r.accounts, nil
}

func (r poolE2EAccounts) ListAllWithFilters(context.Context, string, string, string, string, int64, string) ([]service.Account, error) {
	return r.accounts, nil
}

// poolE2EUpstream 记录出站请求，并按目标地址返回该协议的合法响应。
type poolE2EUpstream struct {
	service.HTTPUpstream
	mu       sync.Mutex
	accounts []int64
	urls     []string
}

func (u *poolE2EUpstream) Do(req *http.Request, _ string, accountID int64, _ int) (*http.Response, error) {
	_, _ = io.Copy(io.Discard, req.Body)
	u.mu.Lock()
	u.accounts = append(u.accounts, accountID)
	u.urls = append(u.urls, req.URL.String())
	u.mu.Unlock()
	body := `{"id":"chatcmpl-1","object":"chat.completion","model":"deepseek-flash","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`
	switch {
	case strings.HasSuffix(req.URL.Path, "/messages"):
		body = `{"id":"msg_1","type":"message","role":"assistant","model":"deepseek-flash","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}`
	case strings.HasSuffix(req.URL.Path, "/responses"):
		body = `{"id":"resp_1","object":"response","model":"deepseek-flash","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}`
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}, nil
}

func (u *poolE2EUpstream) reset() {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.accounts, u.urls = nil, nil
}

// poolE2ESpy 包住真实候选池，记录解析时上下文里的入口标记。
type poolE2ESpy struct {
	inner   service.CompositeCandidatePoolResolver
	mu      sync.Mutex
	calls   int
	entries []service.CompositeCandidatePoolEntry
}

func (s *poolE2ESpy) ResolveCandidatePool(ctx context.Context, groupID int64, model, endpoint string) (service.CompositeRouteDecision, bool, error) {
	s.mu.Lock()
	s.calls++
	if entry, ok := service.CompositeCandidatePoolEntryFromContext(ctx); ok {
		s.entries = append(s.entries, entry)
	}
	s.mu.Unlock()
	return s.inner.ResolveCandidatePool(ctx, groupID, model, endpoint)
}

func (s *poolE2ESpy) reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls, s.entries = 0, nil
}

type poolE2EEnv struct {
	router   *gin.Engine
	upstream *poolE2EUpstream
	spy      *poolE2ESpy
	probes   map[string]string // 探针路由命中时记录的已解析目标平台
	probeMu  *sync.Mutex
}

func poolE2EAccount(id int64, platform string, priority int, credentials map[string]any) service.Account {
	creds := map[string]any{"api_key": "sk-test"}
	for key, value := range credentials {
		creds[key] = value
	}
	return service.Account{
		ID: id, Name: platform, Platform: platform, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Priority: priority, Concurrency: 5,
		GroupIDs: []int64{poolE2EGroupID}, Credentials: creds,
	}
}

func poolE2ECustom(id int64, priority int, urls map[string]any, mapping map[string]any) service.Account {
	return poolE2EAccount(id, service.PlatformCustom, priority, map[string]any{
		"api_protocol":  service.APIProtocolAdaptive,
		"api_base_urls": urls,
		"model_mapping": mapping,
	})
}

func poolE2EDeepseek(id int64, priority int) service.Account {
	return poolE2EAccount(id, service.PlatformDeepseek, priority, map[string]any{
		"api_protocol":  service.APIProtocolAdaptive,
		"model_mapping": map[string]any{poolE2EModel: poolE2EModel},
	})
}

func poolE2EFullURLs() map[string]any {
	return map[string]any{
		service.APIProtocolChatCompletions: "http://chat.example/v1",
		service.APIProtocolResponses:       "http://responses.example/v1",
		service.APIProtocolAnthropic:       "http://anthropic.example",
	}
}

// newPoolE2EEnv 装配路由。withPool 为 false 时不接候选池，用于对照。
func newPoolE2EEnv(t *testing.T, withPool bool, accounts ...service.Account) *poolE2EEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{RunMode: config.RunModeSimple}
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	repo := poolE2EAccounts{accounts: accounts}
	upstream := &poolE2EUpstream{}
	billingCache := service.NewBillingCacheService(nil, nil, nil, nil, nil, nil, cfg, nil)
	t.Cleanup(billingCache.Stop)
	openAI := service.NewOpenAIGatewayService(repo, nil, nil, nil, nil, nil, nil, cfg, nil, nil, service.NewBillingService(cfg, nil), nil, billingCache, upstream, &service.DeferredService{}, nil, nil, nil, nil, nil, nil, nil, nil)
	anthropic := service.NewGatewayService(
		nil, nil, nil, nil, nil, nil, nil, nil, cfg, nil, nil, nil, nil, nil,
		nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil,
	)
	openAIHandler := handler.NewOpenAIGatewayHandler(openAI, service.NewConcurrencyService(nil), billingCache, service.NewAPIKeyService(nil, nil, nil, nil, nil, nil, cfg), nil, nil, nil, nil, cfg)

	resolver := service.NewCompositeRouteResolver(compositeRouteRepoStub{})
	spy := &poolE2ESpy{inner: service.NewCompositeCandidatePool(repo, openAI, anthropic, billingCache)}
	if withPool {
		resolver.SetCandidatePool(spy)
	}

	groupID := poolE2EGroupID
	group := &service.Group{ID: groupID, Platform: service.PlatformComposite, RateMultiplier: 1}
	env := &poolE2EEnv{upstream: upstream, spy: spy, probes: map[string]string{}, probeMu: &sync.Mutex{}}
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Set(string(servermiddleware.ContextKeyAPIKey), &service.APIKey{ID: 202, UserID: 303, GroupID: &groupID, Group: group, User: &service.User{ID: 303}})
		c.Set(string(servermiddleware.ContextKeyUser), servermiddleware.AuthSubject{UserID: 303})
		c.Set(string(servermiddleware.ContextKeySubscription), &service.UserSubscription{ID: 5, UserID: 303, GroupID: groupID})
		c.Next()
	})
	router.Use(compositeTargetPlatformMiddleware(resolver))
	for _, prefix := range []string{"/v1", ""} {
		router.POST(prefix+"/messages", openAIHandler.Messages)
		router.POST(prefix+"/responses", openAIHandler.Responses)
		router.POST(prefix+"/chat/completions", openAIHandler.ChatCompletions)
	}
	// 非文本入口和文本入口的子路径：只记录中间件已解析出的目标平台，不接真实处理器。
	for _, path := range []string{"/v1/embeddings", "/v1/images/generations", "/v1/responses/compact", "/v1/alpha/search", "/v1/live", "/backend-api/codex/realtime/calls"} {
		path := path
		router.POST(path, func(c *gin.Context) {
			platform, _ := service.ResolvedTargetPlatformFromContext(c.Request.Context())
			env.probeMu.Lock()
			env.probes[path] = platform
			env.probeMu.Unlock()
			c.Status(http.StatusNoContent)
		})
	}
	router.GET("/v1/responses", func(c *gin.Context) { c.Status(http.StatusNoContent) })
	env.router = router
	return env
}

func (e *poolE2EEnv) post(path, body string, headers ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	e.router.ServeHTTP(rec, req)
	return rec
}

func poolE2EBody(kind, model string) string {
	switch kind {
	case "messages":
		return `{"model":"` + model + `","max_tokens":16,"stream":false,"messages":[{"role":"user","content":"hi"}]}`
	case "responses":
		return `{"model":"` + model + `","stream":false,"input":"hi"}`
	default:
		return `{"model":"` + model + `","stream":false,"messages":[{"role":"user","content":"hi"}]}`
	}
}

// 第 1 节主表：三种入站格式都选中零转换的自定义账号，并命中该协议的原生端点。
func TestCompositeCandidatePoolEndToEndZeroConversionByInbound(t *testing.T) {
	custom := poolE2ECustom(1, 10, poolE2EFullURLs(), map[string]any{poolE2EModel: poolE2EModel})
	deepseek := poolE2EDeepseek(2, 30)
	env := newPoolE2EEnv(t, true, custom, deepseek)

	cases := []struct {
		kind, path, endpoint, wantURL string
	}{
		{"messages", "/v1/messages", service.CompositeRouteEndpointMessages, "http://anthropic.example/v1/messages"},
		{"responses", "/v1/responses", service.CompositeRouteEndpointResponses, "http://responses.example/v1/responses"},
		{"chat", "/v1/chat/completions", service.CompositeRouteEndpointChatCompletions, "http://chat.example/v1/chat/completions"},
		// 根路径别名同样进池。
		{"messages", "/messages", service.CompositeRouteEndpointMessages, "http://anthropic.example/v1/messages"},
		{"responses", "/responses", service.CompositeRouteEndpointResponses, "http://responses.example/v1/responses"},
		{"chat", "/chat/completions", service.CompositeRouteEndpointChatCompletions, "http://chat.example/v1/chat/completions"},
	}
	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			env.upstream.reset()
			env.spy.reset()
			rec := env.post(tc.path, poolE2EBody(tc.kind, poolE2EModel), "X-Probe", "pool-e2e")
			require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
			require.Equal(t, []int64{1}, env.upstream.accounts, "优先级更高且零转换的自定义账号")
			require.Equal(t, []string{tc.wantURL}, env.upstream.urls)

			// 入口标记装配：端点、请求体、请求头、用户、API Key、分组与订阅。
			require.Len(t, env.spy.entries, 1)
			entry := env.spy.entries[0]
			require.Equal(t, tc.endpoint, entry.Endpoint)
			require.JSONEq(t, poolE2EBody(tc.kind, poolE2EModel), string(entry.Body))
			require.Equal(t, "pool-e2e", entry.Header.Get("X-Probe"))
			require.Equal(t, int64(303), entry.UserID)
			require.Equal(t, int64(202), entry.APIKeyID)
			require.NotNil(t, entry.Group)
			require.Equal(t, poolE2EGroupID, entry.Group.ID)
			require.NotNil(t, entry.Subscription)
		})
	}
}

// 场景 1：清空自定义账号的 Responses 地址后，Responses 入站退到需转换组，改选零转换的 DeepSeek。
func TestCompositeCandidatePoolEndToEndResponsesAddressCleared(t *testing.T) {
	urls := poolE2EFullURLs()
	delete(urls, service.APIProtocolResponses)
	custom := poolE2ECustom(1, 10, urls, map[string]any{poolE2EModel: poolE2EModel})
	env := newPoolE2EEnv(t, true, custom, poolE2EDeepseek(2, 30))

	rec := env.post("/v1/responses", poolE2EBody("responses", poolE2EModel))
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	require.Equal(t, []int64{2}, env.upstream.accounts)
	require.Len(t, env.upstream.urls, 1)
	require.Contains(t, env.upstream.urls[0], "api.deepseek.com")

	env.upstream.reset()
	rec = env.post("/v1/chat/completions", poolE2EBody("chat", poolE2EModel))
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	require.Equal(t, []int64{1}, env.upstream.accounts, "Chat Completions 入站仍选自定义账号")
}

// 场景 13：分组里只有一个通配声明账号，请求 foo-x 能归属到它并被调度器的声明闸门放行，转发成功。
func TestCompositeCandidatePoolEndToEndSingleWildcardAccount(t *testing.T) {
	wildcard := poolE2ECustom(1, 10, poolE2EFullURLs(), map[string]any{"foo-*": "foo-upstream"})

	env := newPoolE2EEnv(t, true, wildcard)
	rec := env.post("/v1/chat/completions", poolE2EBody("chat", "foo-x"))
	require.Equal(t, http.StatusOK, rec.Code, "不应出现无账号可用: %s", rec.Body.String())
	require.Equal(t, []int64{1}, env.upstream.accounts)
	require.Equal(t, []string{"http://chat.example/v1/chat/completions"}, env.upstream.urls)

	// 对照：不接候选池时，通配声明认不出来，请求得不到路由。
	plain := newPoolE2EEnv(t, false, wildcard)
	rec = plain.post("/v1/chat/completions", poolE2EBody("chat", "foo-x"))
	require.NotEqual(t, http.StatusOK, rec.Code)
	require.Empty(t, plain.upstream.accounts)
}

// 非文本入口（以及文本入口的子路径、Live、alpha/search、realtime、Responses WebSocket）不进池。
func TestCompositeCandidatePoolEndToEndNonTextEntriesStayOutOfPool(t *testing.T) {
	custom := poolE2ECustom(1, 10, poolE2EFullURLs(), map[string]any{poolE2EModel: poolE2EModel})
	env := newPoolE2EEnv(t, true, custom, poolE2EDeepseek(2, 30))

	for _, path := range []string{"/v1/embeddings", "/v1/images/generations", "/v1/responses/compact", "/v1/alpha/search", "/v1/live", "/backend-api/codex/realtime/calls"} {
		t.Run(path, func(t *testing.T) {
			env.spy.reset()
			rec := env.post(path, `{"model":"`+poolE2EModel+`","input":"hi","prompt":"x"}`)
			require.Equal(t, http.StatusNoContent, rec.Code)
			require.Empty(t, env.spy.entries, "这些入口没有入口标记，候选池不处理")
			env.probeMu.Lock()
			defer env.probeMu.Unlock()
			// 候选池没有写回集合，目标平台只可能来自旧流程（前缀识别为 DeepSeek），不会是自定义账号所在平台。
			require.NotEqual(t, service.PlatformCustom, env.probes[path])
		})
	}

	// Responses WebSocket 是 GET，中间件直接放行，根本不解析。
	env.spy.reset()
	req := httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	rec := httptest.NewRecorder()
	env.router.ServeHTTP(rec, req)
	require.Equal(t, http.StatusNoContent, rec.Code)
	require.Zero(t, env.spy.calls)
}
