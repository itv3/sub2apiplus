package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 画像声明 TurnMetadata.WindowTracking 后，WS 重连预算耗尽降级到 HTTP 的 attempt 必须与前面的 WS attempt 同口径：
// 窗口号随压缩推进，不进入 invocation 冻结摘要（CodexIdentityFacts.WindowAdvances）。
//
// 缺陷背景：WindowAdvances 原先只在“带 WS 派生状态”时为真，同一次 invocation 里降级而来的 HTTP attempt 没有 WS 派生状态，
// 摘要里仍绑定窗口标识，Executor 以“attempt 不能更换 invocation 冻结的 Persona attestation”拒绝，Forward 把它当传输错误
// 转成 502 failover——任何第三方入站的 OAuth 请求只要 WS 失败就整单失败并换号，而不是降级到 HTTP 继续服务。
// 修复在计划层：首个 attempt 的取值冻结进计划，之后的 attempt 沿用（OpenAIForwardInvocationPlan.alignWindowAdvances）。
// 本文件的用例不依赖正式目录里 Active 画像是否已声明这些开关：先用 withTargetProfileForWire 把 Active 合成为声明了 WindowTracking
// 的目标画像（service 与 Executor 同时生效），候选态、晋升态与集成态下断言一致。

// newWindowTrackedFallbackService 构造一套“WS 拨号恒失败、HTTP 回落可用”的网关服务，Executor 的 Bundle 取自已合成的目标画像目录。
func newWindowTrackedFallbackService(t *testing.T, mode string) (*OpenAIGatewayService, *httpUpstreamRecorder, *candidateTraceFailingWSDialer, *Account) {
	t.Helper()
	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 1
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 1
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 1
	// service 的发布指针配置与 Executor runtime 的 release mode 必须指向同一槽位。
	cfg.Gateway.OfficialClientProfiles.Mode = mode

	dialer := &candidateTraceFailingWSDialer{}
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(dialer)
	t.Cleanup(pool.Close)
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"application/json"}},
		Body: io.NopCloser(strings.NewReader(
			`{"id":"resp_window_tracked_fallback","output":[],"usage":{"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}`,
		)),
	}}
	service := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
		openaiWSPool:     pool,
	}
	// Executor 的 Bundle 改按合成的目标画像目录解析（同 newTargetAwareGate 的做法）。
	require.NotNil(t, targetProfileWireCatalog, "必须先 withTargetProfileForWire")
	runtime := newOfficialEgressTestRuntimeForMode(t, upstream, mode)
	resolver, err := officialegress.NewBundleResolver(*targetProfileWireCatalog, runtime.Guard.ProcessSinkCatalog())
	require.NoError(t, err)
	runtime.BundleResolver = resolver
	service.officialEgress = runtime

	account := &Account{
		ID: 14508, Name: "window-tracked-fallback", Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Status: StatusActive, Schedulable: true, Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "window-tracked-test-token",
			"chatgpt_account_id": "window-tracked-test-account",
		},
	}
	service.openaiModelCapabilities.replaceFromManifest(
		account.ID,
		[]byte(`{"models":[{"slug":"gpt-5.4","use_responses_lite":false}]}`),
	)
	return service, upstream, dialer, account
}

func TestOpenAIForwardWSToHTTPFallbackKeepsInvocationAttestationWhenWindowTracked(t *testing.T) {
	gin.SetMode(gin.TestMode)
	mode := officialClientProfileModeActive
	withTargetProfileForWire(t, mode)
	require.True(t, officialCodexTurnMetadataWindowTracking(mode), "合成的目标画像必须声明 WindowTracking")

	service, upstream, dialer, account := newWindowTrackedFallbackService(t, mode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/openai/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "window-tracked-third-party/1.0")
	body := []byte(`{"model":"gpt-5.4","stream":false,"input":[{"type":"message","role":"user","content":"hello"}]}`)

	result, err := service.Forward(context.Background(), c, account, body)

	require.NoError(t, err, "WS 失败降级到 HTTP 不应被 invocation 摘要校验拒绝（否则对外是 502 failover）")
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq, "降级后的 HTTP 请求必须真正到达上游")
	wsInvocationIDs := dialer.snapshot()
	require.Len(t, wsInvocationIDs, openAIWSReconnectRetryLimit+1, "WS 重连预算应当用尽后才降级")
	httpEgressContext, exists := OfficialEgressContextFromContext(upstream.lastReq.Context())
	require.True(t, exists)
	for _, invocationID := range wsInvocationIDs {
		require.Equal(t, httpEgressContext.InvocationID(), invocationID, "WS 与降级后的 HTTP 属于同一次 invocation")
	}
}

// 对齐函数本身：首个 attempt 的取值冻结进计划，之后的 attempt 一律沿用，与它们自己的取值、Sink 无关。
func TestOpenAIForwardInvocationPlanAlignsWindowAdvancesToFirstAttempt(t *testing.T) {
	// 首个 attempt（带 WS 派生状态的 WS）为真：降级而来的 HTTP attempt 自己取值为假，也要被对齐成真。
	plan := &OpenAIForwardInvocationPlan{}
	require.True(t, plan.alignWindowAdvances(1, true))
	require.True(t, plan.alignWindowAdvances(2, false), "后续 attempt 沿用首个 attempt 的取值")
	require.True(t, plan.alignWindowAdvances(3, false))

	// 首个 attempt 为假（旧画像、首发 HTTP、无 WS 派生状态的 WS）：后续 attempt 即使自己取真也要对齐成假。
	plan = &OpenAIForwardInvocationPlan{}
	require.False(t, plan.alignWindowAdvances(1, false))
	require.False(t, plan.alignWindowAdvances(2, true))

	// 取值一致时是恒等操作。
	plan = &OpenAIForwardInvocationPlan{}
	require.True(t, plan.alignWindowAdvances(1, true))
	require.True(t, plan.alignWindowAdvances(2, true))
}
