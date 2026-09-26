package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// legacy compact 入站在“冻结发布画像已删除 responses_compact”时的失败关闭
//
// VC-6 生产切换前必须钉住的四项行为（晋升后 Active 的目标画像删除了 legacy compact）：
//   ① 下游收到明确的客户端错误：404 not_found_error，并给出可读原因（改用 remote
//      compaction v2 或升级客户端）；
//   ② 失败关闭发生在任何出站之前（含模型能力清单刷新），不会把请求打到任何上游；
//   ③ 不计入账号错误：ops 错误日志标为本地特性门控（业务受限），调度结果上报不计入
//      错误率（见 TestReportOpenAIAccountScheduleResultIgnoresLegacyCompactReleaseRejection）；
//   ④ Forward 不返回结果，用量与计费不产生记录。
//
// 同一组用例在两种目录状态下都成立：按结构事实选出“删除了该端点的槽位”与“仍声明它的
// 槽位”（候选期分别是 previous 与 active，晋升后对调）。前者断言失败关闭，后者作为对照
// 断言 legacy compact 仍正常出站，证明失败关闭来自画像删除，而不是测试构造不合法。
// handler 层（换号、账号状态写入、ops 分类、用量任务）的端到端证据见 handler 包同名用例。
// ============================================================================

const (
	// legacyCompactGateOfficialAccountID 与官方 HTTP 夹具的账号一致：官方 Codex 入站用例按它
	// 预置模型能力清单，避免已知模型的异步刷新改变 Lite 判定。
	legacyCompactGateOfficialAccountID = 94
	// legacyCompactGateProbeAccountID 不预置模型能力清单：第三方入站携带内置能力表未知的
	// 探针模型，Forward 一旦走到模型能力检查就会同步拉取清单（一次真实出站）。“零出站”
	// 断言因此能检出“失败关闭晚于模型能力刷新”的实现。
	legacyCompactGateProbeAccountID = 7160
	// legacyCompactGateProbeModel 是内置能力表未收录的探针模型，只出现在测试上游的清单里。
	legacyCompactGateProbeModel = "gpt-legacy-compact-gate"
)

// legacyCompactGateModelsManifest 在官方 fixture 清单之外加入探针模型，供对照组通过能力检查。
var legacyCompactGateModelsManifest = `{"models":[{"slug":"` + legacyCompactGateProbeModel +
	`","visibility":"list","use_responses_lite":false,"supports_parallel_tool_calls":true},` +
	strings.TrimPrefix(codexModelsRecorderManifest, `{"models":[`)

// legacyCompactGateReleaseSlots 按结构事实返回正式目录中删除了与仍声明 legacy compact
// 端点的两个发布槽位。任一侧缺失时失败：两槽位都声明说明目标画像未入库；两槽位都不
// 声明说明对照组已随旧画像退休，本组用例应与 legacy compact 代码路径一并改写或退役。
func legacyCompactGateReleaseSlots(t *testing.T) (removed string, declared string) {
	t.Helper()
	for _, mode := range officialCodexFormalModes {
		declares := false
		for _, endpoint := range officialCodexFormalExecutableProfile(t, mode).Endpoints() {
			if endpoint.ID == officialCodexEndpointResponsesCompact {
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
	if removed == "" || declared == "" {
		t.Fatalf("正式目录需要同时具备删除与声明 legacy compact 的发布槽位：removed=%q declared=%q", removed, declared)
	}
	return removed, declared
}

// legacyCompactGateUpstream 记录网关发出的全部请求（含模型能力清单刷新）。
type legacyCompactGateUpstream struct {
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
			Body:       io.NopCloser(strings.NewReader(legacyCompactGateModelsManifest)),
		}, nil
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body: io.NopCloser(strings.NewReader(
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

// newLegacyCompactGateService 构造 service 画像配置与 Executor runtime 同指 mode 槽位的网关
// 服务（生产上两者由同一配置派生）。
func newLegacyCompactGateService(t *testing.T, mode string, upstream *legacyCompactGateUpstream) *OpenAIGatewayService {
	t.Helper()
	svc := &OpenAIGatewayService{
		cfg: &config.Config{Security: config.SecurityConfig{
			URLAllowlist: config.URLAllowlistConfig{Enabled: false},
		}},
		httpUpstream: upstream,
	}
	svc.cfg.Gateway.OfficialClientProfiles.Mode = mode
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, upstream, mode)
	svc.openaiModelCapabilities.replaceFromManifest(legacyCompactGateOfficialAccountID, []byte(codexModelsRecorderManifest))
	return svc
}

// newLegacyCompactGateThirdPartyIngress 构造第三方客户端的 legacy compact 入站（探针模型）。
func newLegacyCompactGateThirdPartyIngress(path string) (*gin.Context, *httptest.ResponseRecorder, []byte) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"model":"` + legacyCompactGateProbeModel + `","stream":false,"instructions":"compact-test",` +
		`"input":[{"type":"message","role":"user","content":"hello"}]}`)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", "third-party-client/1.0")
	return c, rec, body
}

// newLegacyCompactGateOfficialIngress 构造官方 Codex 客户端的 legacy compact 入站（与
// TestOpenAIGatewayForwardOfficialEgressHTTPCompactNormalizesExplicitContract 同形态）。
func newLegacyCompactGateOfficialIngress(t *testing.T, path string) (*gin.Context, *httptest.ResponseRecorder, []byte) {
	t.Helper()
	bodyWithMetadata := newOfficialOpenAIHTTPTestBody(t, false, true, true)
	turnMetadata := gjson.GetBytes(bodyWithMetadata, "client_metadata.x-codex-turn-metadata").String()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(bodyWithMetadata, &payload))
	delete(payload, "client_metadata")
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = newOfficialOpenAIHTTPTestContext(body, path).Request
	c.Request.Header.Set("Accept", "application/json")
	c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
	c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
	return c, rec, body
}

func newLegacyCompactGateOAuthAccount(accountID int64, extra map[string]any) *Account {
	account := newOfficialOpenAIHTTPTestAccount(accountID)
	for key, value := range extra {
		account.Extra[key] = value
	}
	return account
}

type legacyCompactGateCase struct {
	name    string
	account func() *Account
	ingress func(t *testing.T) (*gin.Context, *httptest.ResponseRecorder, []byte)
}

// legacyCompactGateCases 覆盖 legacy compact 入站的各条路径：第三方客户端直连普通 OAuth、
// OAuth passthrough、WS ctx_pool 账号（compact 恒走 HTTP），官方 Codex 客户端入站，以及
// Codex 直连与 OpenAI 前缀两个路径别名。
func legacyCompactGateCases() []legacyCompactGateCase {
	thirdParty := func(path string) func(*testing.T) (*gin.Context, *httptest.ResponseRecorder, []byte) {
		return func(*testing.T) (*gin.Context, *httptest.ResponseRecorder, []byte) {
			return newLegacyCompactGateThirdPartyIngress(path)
		}
	}
	plain := func() *Account { return newLegacyCompactGateOAuthAccount(legacyCompactGateProbeAccountID, nil) }
	return []legacyCompactGateCase{
		{name: "第三方客户端/普通OAuth", account: plain, ingress: thirdParty("/v1/responses/compact")},
		{
			name: "第三方客户端/OAuth_passthrough",
			account: func() *Account {
				return newLegacyCompactGateOAuthAccount(legacyCompactGateProbeAccountID, map[string]any{"openai_passthrough": true})
			},
			ingress: thirdParty("/v1/responses/compact"),
		},
		{
			name: "第三方客户端/WS_ctx_pool账号",
			account: func() *Account {
				return newLegacyCompactGateOAuthAccount(legacyCompactGateProbeAccountID, map[string]any{
					"openai_oauth_responses_websockets_v2_mode": OpenAIWSIngressModeCtxPool,
				})
			},
			ingress: thirdParty("/v1/responses/compact"),
		},
		{
			name:    "官方Codex客户端/普通OAuth",
			account: func() *Account { return newLegacyCompactGateOAuthAccount(legacyCompactGateOfficialAccountID, nil) },
			ingress: func(t *testing.T) (*gin.Context, *httptest.ResponseRecorder, []byte) {
				return newLegacyCompactGateOfficialIngress(t, "/v1/responses/compact")
			},
		},
		{name: "Codex直连别名", account: plain, ingress: thirdParty("/backend-api/codex/responses/compact")},
		{name: "OpenAI前缀别名", account: plain, ingress: thirdParty("/openai/v1/responses/compact")},
	}
}

// requireLegacyCompactRemovedResponse 断言下游收到的是明确的客户端错误与可读原因。
func requireLegacyCompactRemovedResponse(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	require.Equal(t, http.StatusNotFound, rec.Code, "① 画像删除的端点必须以客户端错误失败关闭，不得是 5xx")
	require.Equal(t, "not_found_error", gjson.GetBytes(rec.Body.Bytes(), "error.type").String(), rec.Body.String())
	message := gjson.GetBytes(rec.Body.Bytes(), "error.message").String()
	require.Contains(t, message, "/responses/compact", "原因必须指明被拒绝的端点")
	require.Contains(t, message, "remote compaction v2", "原因必须提示改用 remote compaction v2")
	require.Contains(t, message, "compaction_trigger", "原因必须给出 v2 的具体用法")
	require.Contains(t, message, "upgrade", "原因必须提示升级客户端")
}

func TestOpenAILegacyCompactIngressFailsClosedWhenReleaseRemovesEndpoint(t *testing.T) {
	removed, declared := legacyCompactGateReleaseSlots(t)
	for _, tc := range legacyCompactGateCases() {
		t.Run(tc.name+"/删除槽位失败关闭", func(t *testing.T) {
			upstream := &legacyCompactGateUpstream{}
			svc := newLegacyCompactGateService(t, removed, upstream)
			c, rec, body := tc.ingress(t)

			result, err := svc.Forward(context.Background(), c, tc.account(), body)

			require.Nil(t, result, "④ 失败关闭不返回结果，handler 不会提交用量与计费")
			require.ErrorIs(t, err, ErrOpenAILegacyCompactRemovedByRelease)
			requireLegacyCompactRemovedResponse(t, rec)
			require.True(t, IsResponseCommitted(c), "本地错误已完整写出，handler 不得再补写兜底错误")
			require.Empty(t, upstream.snapshot(), "② 失败关闭必须发生在任何出站（含模型能力清单刷新）之前")
			require.True(t, HasOpsClientBusinessLimited(c), "③ ops 错误日志必须标为本地拒绝，不计入上游错误")
			require.Equal(t, OpsClientBusinessLimitedReasonLocalFeatureGate, OpsClientBusinessLimitedReason(c))
		})
		t.Run(tc.name+"/声明槽位对照", func(t *testing.T) {
			upstream := &legacyCompactGateUpstream{}
			svc := newLegacyCompactGateService(t, declared, upstream)
			c, rec, body := tc.ingress(t)

			result, err := svc.Forward(context.Background(), c, tc.account(), body)

			require.NoError(t, err, "对照：仍声明 legacy compact 的槽位必须照常出站")
			require.NotNil(t, result)
			require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
			require.Contains(t, upstream.snapshot(), "POST chatgpt.com/backend-api/codex/responses/compact")
			require.False(t, HasOpsClientBusinessLimited(c))
		})
	}
}

// API Key 账号的 legacy compact 不经过 Codex 发布画像：即使 Codex 槽位删除了该端点，
// API Key 账号仍照常转发到其上游的 /v1/responses/compact。本用例锁定修复只作用于走官方
// Codex 画像的 OAuth 账号，不误伤混合账号池中的 API Key 账号。
func TestOpenAILegacyCompactReleaseGateLeavesAPIKeyAccountsUntouched(t *testing.T) {
	removed, _ := legacyCompactGateReleaseSlots(t)
	upstream := &legacyCompactGateUpstream{}
	svc := newLegacyCompactGateService(t, removed, upstream)
	c, rec, body := newLegacyCompactGateThirdPartyIngress("/v1/responses/compact")
	account := &Account{
		ID: 7157, Name: "openai-apikey", Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Credentials: map[string]any{"api_key": "test-key"}, Status: StatusActive, Schedulable: true,
	}

	result, err := svc.Forward(context.Background(), c, account, body)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	require.Equal(t, []string{"POST api.openai.com/v1/responses/compact"}, upstream.snapshot())
	require.False(t, HasOpsClientBusinessLimited(c))
}

// 画像删除 legacy compact 的本地拒绝发生在任何出站之前，与账号健康无关：调度结果上报不得
// 把它计入账号错误率（高级调度器按错误率 EWMA 做粘性逃逸与负载评分）。对照：同样以失败
// 上报的普通 Forward 错误照常计入，证明观测手段有效。
func TestReportOpenAIAccountScheduleResultIgnoresLegacyCompactReleaseRejection(t *testing.T) {
	rateLimitService := newOpenAIAdvancedSchedulerRateLimitService("true")
	t.Cleanup(resetOpenAIAdvancedSchedulerSettingCacheForTest)
	svc := &OpenAIGatewayService{cfg: &config.Config{}, rateLimitService: rateLimitService}
	require.NotNil(t, svc.getOpenAIAccountScheduler(context.Background()), "高级调度器必须启用，错误率观测才有意义")
	rejected := &Account{ID: 7158, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	failed := &Account{ID: 7159, Platform: PlatformOpenAI, Type: AccountTypeOAuth}

	tripped := svc.ReportOpenAIAccountScheduleResult(
		rejected, "gpt-5.4", false, nil, fmt.Errorf("forward: %w", ErrOpenAILegacyCompactRemovedByRelease),
	)
	require.False(t, tripped)
	errorRate, _, _ := svc.openaiAccountStats.snapshot(rejected.ID)
	require.Zero(t, errorRate, "③ 画像删除 legacy compact 的本地拒绝不得计入账号错误率")

	svc.ReportOpenAIAccountScheduleResult(
		failed, "gpt-5.4", false, nil, errors.New("resolve official egress profile: 不支持端点画像"),
	)
	errorRate, _, _ = svc.openaiAccountStats.snapshot(failed.ID)
	require.Greater(t, errorRate, 0.0, "对照：普通 Forward 失败照常计入错误率")
}
