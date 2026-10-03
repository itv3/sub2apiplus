//go:build unit

package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// legacy compact 入站在“冻结发布画像已删除 responses_compact”时的端到端行为
// （VC-6 晋升前置确认；service 层用例见 service 包 openai_legacy_compact_release_gate_test.go）。
//
// 经真实 handler.Responses（选号、换号循环、错误写出、用量提交）、生产 OpenAIGatewayService
// 与正式 Executor runtime 驱动，钉住四项口径：
//   ① 下游收到 404 not_found_error 与可读原因（改用 remote compaction v2 或升级客户端）；
//   ② 不换号、不出站：只选中首个账号，任何上游（模型能力清单、另一 OAuth 账号、池中的
//      API Key 账号）都收不到请求；
//   ③ 无账号副作用：注入真实 RateLimitService 后账号仓库零写入（错误、冷却、临时不可调度、
//      限流），ops 错误日志经中间件同一分类函数归为业务受限、归属客户端、P3；
//   ④ 用量任务零提交。
// 对照：同一请求在仍声明 legacy compact 的槽位上照常出站、200、提交一次用量。各目录状态
// 下都按结构事实选槽位（候选期删除槽位是 previous，晋升后是 active）；legacy compact 按
// 发布退役覆盖层正式退役后两个槽位都删除该端点，对照组改为另一槽位同样失败关闭。
// ============================================================================

// legacyCompactGateHandlerProbeModel 是内置能力表未收录的探针模型：账号未加载模型能力清单时，
// Forward 一旦走到模型能力检查就会同步拉取清单（一次真实出站），“零出站”断言因此能检出
// “失败关闭晚于模型能力刷新”的实现。测试上游的清单收录它，供对照组通过能力检查。
const legacyCompactGateHandlerProbeModel = "gpt-legacy-compact-gate"

// legacyCompactGateHandlerModes 是正式目录的两个发布槽位。
var legacyCompactGateHandlerModes = []string{"active", "previous"}

// legacyCompactGateHandlerSlots 按结构事实返回删除与仍声明 legacy compact 端点的发布槽位。
// legacy compact 已按发布退役覆盖层正式退役时两个槽位都删除该端点，declared 为空，由调用方改做
// 两槽位失败关闭；未退役却缺任一侧时失败（两槽位都声明说明目标画像未入库）。
func legacyCompactGateHandlerSlots(t *testing.T) (removed string, declared string) {
	t.Helper()
	for _, mode := range legacyCompactGateHandlerModes {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
		require.NoError(t, err)
		declares := false
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if endpoint.ID == "responses_compact" {
				declares = true
			}
		}
		switch {
		case declares && declared == "":
			declared = mode
		case !declares && removed == "":
			removed = mode
		}
	}
	if removed == "" || (declared == "" && !legacyCompactGateHandlerReleaseRetired(t)) {
		t.Fatalf("正式目录需要同时具备删除与声明 legacy compact 的发布槽位：removed=%q declared=%q", removed, declared)
	}
	return removed, declared
}

// legacyCompactGateHandlerReleaseRetired 判定发布退役覆盖层是否登记了 legacy compact 的 route
// （与 service 包同名判据一致）；只在两个槽位都已删除该端点时调用。
func legacyCompactGateHandlerReleaseRetired(t *testing.T) bool {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "officialegress", "catalogdata", "release-route-retirements.json"))
	require.NoError(t, err)
	var manifest struct {
		Routes []struct {
			Method     string `json:"method"`
			Host       string `json:"host"`
			Path       string `json:"path"`
			Protocol   string `json:"protocol"`
			EndpointID string `json:"endpoint_id"`
		} `json:"routes"`
	}
	require.NoError(t, json.Unmarshal(raw, &manifest))
	for _, route := range manifest.Routes {
		if route.Method == http.MethodPost && route.Host == "chatgpt.com" &&
			route.Path == "/backend-api/codex/responses/compact" && route.Protocol == "http" &&
			route.EndpointID == "responses_compact" {
			return true
		}
	}
	return false
}

// legacyCompactGateRepo 在只读夹具之上记录全部账号状态写入。
type legacyCompactGateRepo struct {
	openAIImagesFailoverAccountRepo
	mu     sync.Mutex
	writes []string
}

func (r *legacyCompactGateRepo) note(name string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.writes = append(r.writes, name)
}

func (r *legacyCompactGateRepo) snapshot() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.writes...)
}

func (r *legacyCompactGateRepo) SetError(context.Context, int64, string) error {
	r.note("SetError")
	return nil
}

func (r *legacyCompactGateRepo) ClearError(context.Context, int64) error {
	r.note("ClearError")
	return nil
}

func (r *legacyCompactGateRepo) SetSchedulable(context.Context, int64, bool) error {
	r.note("SetSchedulable")
	return nil
}

func (r *legacyCompactGateRepo) SetRateLimited(context.Context, int64, time.Time) error {
	r.note("SetRateLimited")
	return nil
}

func (r *legacyCompactGateRepo) SetModelRateLimit(context.Context, int64, string, time.Time, ...string) error {
	r.note("SetModelRateLimit")
	return nil
}

func (r *legacyCompactGateRepo) SetOverloaded(context.Context, int64, time.Time) error {
	r.note("SetOverloaded")
	return nil
}

func (r *legacyCompactGateRepo) SetTempUnschedulable(context.Context, int64, time.Time, string) error {
	r.note("SetTempUnschedulable")
	return nil
}

func (r *legacyCompactGateRepo) UpdateExtra(context.Context, int64, map[string]any) error {
	r.note("UpdateExtra")
	return nil
}

func (r *legacyCompactGateRepo) UpdateLastUsed(context.Context, int64) error {
	r.note("UpdateLastUsed")
	return nil
}

// legacyCompactGateUpstream 记录网关发出的全部请求（方法、主机、路径与账号）。
type legacyCompactGateUpstream struct {
	service.HTTPUpstream
	mu    sync.Mutex
	calls []string
}

func (u *legacyCompactGateUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	u.mu.Lock()
	u.calls = append(u.calls, req.Method+" "+req.URL.Host+req.URL.Path)
	u.mu.Unlock()
	if strings.HasSuffix(req.URL.Path, "/codex/models") {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body: io.NopCloser(bytes.NewBufferString(
				`{"models":[{"slug":"` + legacyCompactGateHandlerProbeModel +
					`","visibility":"list","use_responses_lite":false,"supports_parallel_tool_calls":true}]}`,
			)),
		}, nil
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body: io.NopCloser(bytes.NewBufferString(
			`{"id":"resp_gate_compact","object":"response.compaction","output":[],` +
				`"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}`,
		)),
	}, nil
}

func (u *legacyCompactGateUpstream) DoWithTLS(
	req *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
	_ *tlsfingerprint.Profile,
) (*http.Response, error) {
	return u.Do(req, proxyURL, accountID, concurrency)
}

func (u *legacyCompactGateUpstream) snapshot() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]string(nil), u.calls...)
}

// legacyCompactGateFixture 是指向某个发布槽位的真实 handler 及其观测点。
type legacyCompactGateFixture struct {
	handler  *OpenAIGatewayHandler
	repo     *legacyCompactGateRepo
	upstream *legacyCompactGateUpstream
	usage    *service.UsageRecordWorkerPool
}

// newLegacyCompactGateFixture 构造 service 画像配置与 Executor runtime 同指 mode 槽位的
// 真实 handler。账号池：两个 OAuth 账号（oauthExtra 决定普通、passthrough 或 ctx_pool）
// 与一个优先级最低的 API Key 账号——若 handler 换号，请求会落到另一账号并真实出站。
func newLegacyCompactGateFixture(t *testing.T, mode string, oauthExtra map[string]any) *legacyCompactGateFixture {
	t.Helper()
	oauth := func(id int64, priority int) service.Account {
		extra := map[string]any{}
		for key, value := range oauthExtra {
			extra[key] = value
		}
		return service.Account{
			ID: id, Name: "legacy-compact-oauth", Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth,
			Status: service.StatusActive, Schedulable: true, Priority: priority,
			Credentials: map[string]any{"access_token": "token", "chatgpt_account_id": "chatgpt-account"},
			Extra:       extra,
		}
	}
	accounts := []service.Account{
		oauth(1, 0),
		oauth(2, 1),
		{
			ID: 3, Name: "legacy-compact-apikey", Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
			Status: service.StatusActive, Schedulable: true, Priority: 9,
			Credentials: map[string]any{"api_key": "test-key"},
		},
	}
	repo := &legacyCompactGateRepo{openAIImagesFailoverAccountRepo: openAIImagesFailoverAccountRepo{accounts: accounts}}
	upstream := &legacyCompactGateUpstream{}
	cfg := &config.Config{RunMode: config.RunModeSimple}
	cfg.Gateway.OfficialClientProfiles.Mode = mode
	baseGuard := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		baseGuard.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), baseGuard.Recorder(),
	)
	require.NoError(t, err)
	runtimeState, err := service.BuildOfficialEgressTransitionRuntime(guard, upstream, cfg, nil)
	require.NoError(t, err)
	rateLimitService := service.NewRateLimitService(repo, nil, cfg, nil, nil)
	gatewayService := service.ProvideOpenAIGatewayService(
		repo, nil, nil, nil, nil, nil, nil, cfg,
		nil, nil, nil, rateLimitService, nil, upstream,
		nil, nil, nil, nil, nil, nil, nil, nil, nil, runtimeState,
	)
	billingService := service.NewBillingCacheService(nil, nil, nil, nil, nil, nil, cfg, nil)
	t.Cleanup(billingService.Stop)
	usage := service.NewUsageRecordWorkerPoolWithOptions(service.UsageRecordWorkerPoolOptions{
		WorkerCount: 1, QueueSize: 8, TaskTimeout: time.Second,
	})
	t.Cleanup(usage.Stop)
	handler := NewOpenAIGatewayHandler(
		gatewayService, service.NewConcurrencyService(nil), billingService,
		service.NewAPIKeyService(nil, nil, nil, nil, nil, nil, cfg), usage, nil, nil, nil, cfg,
	)
	handler.maxAccountSwitches = 10
	return &legacyCompactGateFixture{handler: handler, repo: repo, upstream: upstream, usage: usage}
}

// newLegacyCompactGateContext 构造第三方客户端的 legacy compact 入站（鉴权中间件已放行）。
func newLegacyCompactGateContext(path string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	groupID := int64(4157)
	body := []byte(`{"model":"` + legacyCompactGateHandlerProbeModel + `","stream":false,"instructions":"compact-test",` +
		`"input":[{"type":"message","role":"user","content":"hello"}]}`)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "third-party-client/1.0")
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = req
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		ID: 99, GroupID: &groupID,
		Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI},
		User:  &service.User{ID: 100},
	})
	c.Set(string(middleware2.ContextKeyUser), middleware2.AuthSubject{UserID: 100, Concurrency: 0})
	return c, rec
}

// waitLegacyCompactGateUsage 等待用量池处理完已提交任务，避免异步提交造成误判。
func waitLegacyCompactGateUsage(t *testing.T, usage *service.UsageRecordWorkerPool) uint64 {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		stats := usage.Stats()
		if stats.CompletedTasks+stats.DroppedTasks >= stats.SubmittedTasks || time.Now().After(deadline) {
			return stats.SubmittedTasks
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestOpenAIGatewayHandlerResponsesLegacyCompactRemovedByReleaseFailsClosedWithoutAccountSideEffects(t *testing.T) {
	removed, declared := legacyCompactGateHandlerSlots(t)
	accountModes := []struct {
		name  string
		extra map[string]any
	}{
		{name: "普通OAuth"},
		{name: "OAuth_passthrough", extra: map[string]any{"openai_passthrough": true}},
		{name: "WS_ctx_pool账号", extra: map[string]any{"openai_oauth_responses_websockets_v2_mode": service.OpenAIWSIngressModeCtxPool}},
	}
	paths := []string{
		"/v1/responses/compact",
		"/responses/compact",
		"/backend-api/codex/responses/compact",
		"/openai/v1/responses/compact",
	}
	// requireFailsClosed 在 mode 槽位上断言四项失败关闭口径。
	requireFailsClosed := func(t *testing.T, mode string, extra map[string]any, path string) {
		fixture := newLegacyCompactGateFixture(t, mode, extra)
		c, rec := newLegacyCompactGateContext(path)

		fixture.handler.Responses(c)

		// ② 不出站、不换号：只选中首个账号，任何上游都收不到请求（先于状态码断言，
		// 使“轮询账号池”类回归直接以出站／换号的形式报出）。
		require.Empty(t, fixture.upstream.snapshot(), "失败关闭前不得有任何出站，也不得换号到池中其他账号")
		selected, ok := c.Get(opsAccountIDKey)
		require.True(t, ok)
		require.Equal(t, int64(1), selected, "只尝试首个账号，不轮询账号池")
		// ① 明确的客户端错误与可读原因。
		require.Equal(t, http.StatusNotFound, rec.Code, rec.Body.String())
		errType := gjson.GetBytes(rec.Body.Bytes(), "error.type").String()
		message := gjson.GetBytes(rec.Body.Bytes(), "error.message").String()
		require.Equal(t, "not_found_error", errType, rec.Body.String())
		require.Contains(t, message, "remote compaction v2")
		require.Contains(t, message, "upgrade")
		// ③ 无账号副作用，ops 错误日志归为本地业务受限。
		require.Empty(t, fixture.repo.snapshot(), "不得写入账号错误、冷却、临时不可调度或限流状态")
		phase, businessLimited, owner, _ := classifyOpsErrorLog(c, errType, message, "", rec.Code)
		require.True(t, businessLimited, "ops 错误日志必须归为业务受限，不计入 SLA 与错误率")
		require.NotEqual(t, "upstream", phase)
		require.Equal(t, "client", owner)
		require.Equal(t, "P3", classifyOpsSeverity(errType, rec.Code))
		// ④ 不提交用量任务。
		require.Zero(t, waitLegacyCompactGateUsage(t, fixture.usage), "失败关闭不得产生用量与计费记录")
	}
	for _, accountMode := range accountModes {
		for _, path := range paths {
			t.Run(accountMode.name+path+"/删除槽位失败关闭", func(t *testing.T) {
				requireFailsClosed(t, removed, accountMode.extra, path)
			})
			if declared == "" {
				// legacy compact 已正式发布退役：没有仍声明它的槽位，对照组改为另一槽位同样失败关闭。
				for _, mode := range legacyCompactGateHandlerModes {
					if mode == removed {
						continue
					}
					t.Run(accountMode.name+path+"/退役后"+mode+"槽位同样失败关闭", func(t *testing.T) {
						requireFailsClosed(t, mode, accountMode.extra, path)
					})
				}
				continue
			}
			t.Run(accountMode.name+path+"/声明槽位对照", func(t *testing.T) {
				fixture := newLegacyCompactGateFixture(t, declared, accountMode.extra)
				c, rec := newLegacyCompactGateContext(path)

				fixture.handler.Responses(c)

				require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
				require.Contains(t, fixture.upstream.snapshot(), "POST chatgpt.com/backend-api/codex/responses/compact")
				require.False(t, service.HasOpsClientBusinessLimited(c))
				require.Equal(t, uint64(1), waitLegacyCompactGateUsage(t, fixture.usage), "对照：成功的 compact 照常提交一次用量")
			})
		}
	}
}
