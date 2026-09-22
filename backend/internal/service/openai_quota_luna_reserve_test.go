package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

// GET /wham/usage 的 Luna Reserve 条件只由网关自身的配额请求声明：非 FedRAMP 账号
// 携带，FedRAMP 账号不带；其余 WHAM 请求头不受影响。
func TestCodexQuotaUsageHeadersDeclareLunaReserveForNonFedRAMP(t *testing.T) {
	base := http.Header{"Authorization": []string{"Bearer token"}}

	withReserve := codexQuotaUsageHeaders(base, false)
	require.Equal(t, "1", withReserve.Get("x-openai-codex-luna-reserve"))
	require.Equal(t, "Bearer token", withReserve.Get("Authorization"))
	require.Empty(t, base.Get("x-openai-codex-luna-reserve"), "不得修改传入的公共头")

	fedRAMP := codexQuotaUsageHeaders(base, true)
	require.Empty(t, fedRAMP.Get("x-openai-codex-luna-reserve"))

	require.NotNil(t, codexQuotaUsageHeaders(nil, false))
}

// 画像没有 wham_usage 的 Luna Reserve 槽位时，该条件头只作为事实进入 compiler，不得以
// 普通 Header 身份泄漏到 wire；周期入口 QueryUsageOnly 与管理端 QueryUsage 都遵守画像闭集。
// 0.154.0 起 active 画像已带该槽位（SPEC-EP-019 的 change），因此这条负例固定跑 previous
// 0.151.0——回滚到 previous 时同样不得泄漏。正例见
// TestCodexWhamRequestsUseClosedBackendClientProfile。
func TestCodexWhamUsageLunaReserveDoesNotLeakWithoutProfileSlot(t *testing.T) {
	account := &Account{
		ID:       711,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-luna-reserve",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-luna-reserve",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer server.Close()

	upstream := newQuotaRedirectingUpstream(server)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	// 配额链路的画像由发布指针（runtime.CodexReleaseMode）解析，不读入站 ProfileMode；
	// 负例必须真正跑在没有 Luna Reserve 槽位的 previous 0.151.0 画像上。
	guard, guardErr := officialegress.NewGuard(
		officialegress.DefaultGuard().Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), officialegress.DefaultGuard().Recorder(),
	)
	require.NoError(t, guardErr)
	previousRuntime, runtimeErr := newOfficialEgressTransitionRuntimeWithExecutor(
		guard, upstream, officialCodexExecutorID, officialegress.ReleaseModePrevious,
	)
	require.NoError(t, runtimeErr)
	service.officialEgress = previousRuntime
	runtimeState := defaultOfficialCodexRuntimeState()
	runtimeState.ProfileMode = officialClientProfileModePrevious
	runtimeState.SurfaceID = officialCodexSurfaceTUI
	runtimeState.Originator = "codex-tui"
	runtimeState.TerminalToken = "xterm-256color"
	runtimeState.UserAgentSuffixEnabled = false
	runtimeContext, err := withOfficialCodexRuntimeState(context.Background(), runtimeState)
	require.NoError(t, err)

	_, err = service.QueryUsageOnly(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.QueryUsage(runtimeContext, account.ID)
	require.NoError(t, err)

	usageSeen := 0
	for _, request := range upstream.requests {
		if request.URL.Path == "/backend-api/wham/usage" {
			usageSeen++
		}
		require.Empty(t, request.Header.Get("x-openai-codex-luna-reserve"),
			"%s：previous 0.151.0 画像没有 Luna Reserve 槽位，条件头不得进入 wire", request.URL.Path)
	}
	require.Equal(t, 2, usageSeen, "QueryUsageOnly 与 QueryUsage 各发一次 /wham/usage")
}

// codexWhamGateReleaseMode 找出装载了 wham_usage Luna Reserve 槽位的发布槽位。晋升前
// 目标画像在 previous，晋升后在 active；门禁按结构事实而不是按槽位名选择目标制品，
// 这样同一条测试在候选期与晋升后都在重放同一语义。
func codexWhamGateReleaseMode(t *testing.T) string {
	t.Helper()
	for _, mode := range []string{officialClientProfileModePrevious, officialClientProfileModeActive} {
		endpoint, err := resolveCodexEndpointForMode(mode, officialCodexEndpointWhamUsage)
		if err != nil {
			continue
		}
		for _, slot := range endpoint.OrderedHeaders() {
			if strings.EqualFold(slot.Name, "x-openai-codex-luna-reserve") {
				require.Equal(t, officialCodexConditionLunaReserve, slot.Condition)
				require.Equal(t, "1", slot.Value)
				return mode
			}
		}
	}
	t.Fatal("ReleaseCatalog 的 active/previous 都没有 wham_usage 的 Luna Reserve 槽位：目标制品未入库")
	return ""
}

// codexWhamWireHeaderOrder 按 TLS 画像里该端点的静态 H1 线序规则，推导 wire 层实际
// 写出的 header 名序列：规则闭集（RejectUnlisted）内、请求上确实存在的头按规则顺序
// 输出，host 由传输层生成。这正是采集侧 header_names_in_order 的构造方式。
func codexWhamWireHeaderOrder(
	t *testing.T,
	profile *tlsfingerprint.Profile,
	request *http.Request,
) []string {
	t.Helper()
	require.NotNil(t, profile, "%s 缺少 TLS 画像", request.URL.Path)
	var rule *tlsfingerprint.H1HeaderOrderRule
	for index := range profile.Transport.H1HeaderOrders {
		candidate := &profile.Transport.H1HeaderOrders[index]
		if candidate.Method == request.Method && candidate.Path == request.URL.Path {
			rule = candidate
			break
		}
	}
	require.NotNil(t, rule, "TLS 画像缺少 %s %s 的 H1 规则", request.Method, request.URL.Path)
	require.Equal(t, tlsfingerprint.H1HeaderOrderModeStatic, rule.Mode)
	require.True(t, rule.RejectUnlisted)
	present := make(map[string]struct{}, len(request.Header))
	for name := range request.Header {
		present[strings.ToLower(name)] = struct{}{}
	}
	order := make([]string, 0, len(rule.Order))
	for _, name := range rule.Order {
		if _, ok := present[name]; ok || name == "host" {
			order = append(order, name)
		}
	}
	return order
}

// SPEC-EP-019 的晋升后门禁：在装载了目标画像的发布槽位上重放批准断言语义。
//   - GET /wham/usage 使用 backend-client 精确线序，并在 chatgpt-account-id 之后携带
//     x-openai-codex-luna-reserve，值固定为 1；
//   - GET /wham/rate-limit-reset-credits 保持精确线序且不携带该头；
//   - FedRAMP 账号的 usage 不携带该头。
//
// 与 TestCodexWhamUsageLunaReserveDoesNotLeakWithoutProfileSlot 互为正反面：条件事实
// 相同，是否落到 wire 只由画像槽位决定。
func TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexWhamGateReleaseMode(t)

	account := &Account{
		ID:       712,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-luna-reserve-target",
		},
	}
	fedRAMPAccount := &Account{
		ID:       713,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id":         "acct-luna-reserve-fedramp",
			"chatgpt_account_is_fedramp": true,
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{
		account.ID:        account,
		fedRAMPAccount.ID: fedRAMPAccount,
	}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account):        "token-luna-reserve-target",
		OpenAITokenCacheKey(fedRAMPAccount): "token-luna-reserve-fedramp",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer server.Close()

	upstream := newQuotaRedirectingUpstream(server)
	base := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		base.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), base.Recorder(),
	)
	require.NoError(t, err)
	egressRuntime, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard, upstream, officialCodexExecutorID, officialegress.ReleaseMode(mode),
	)
	require.NoError(t, err)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	service.officialEgress = egressRuntime

	runtimeState := defaultOfficialCodexRuntimeState()
	runtimeState.ProfileMode = mode
	runtimeState.SurfaceID = officialCodexSurfaceTUI
	runtimeState.Originator = "codex-tui"
	runtimeState.TerminalToken = "xterm-256color"
	runtimeState.UserAgentSuffixEnabled = false
	runtimeContext, err := withOfficialCodexRuntimeState(context.Background(), runtimeState)
	require.NoError(t, err)

	_, err = service.QueryUsageOnly(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.QueryUsage(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.QueryUsageOnly(runtimeContext, fedRAMPAccount.ID)
	require.NoError(t, err)

	usageSeen, creditsSeen, fedRAMPUsageSeen := 0, 0, 0
	for index, request := range upstream.requests {
		order := codexWhamWireHeaderOrder(t, upstream.tlsProfiles[index], request)
		fedRAMP := request.Header.Get("chatgpt-account-id") == "acct-luna-reserve-fedramp"
		switch request.URL.Path {
		case "/backend-api/wham/usage":
			if fedRAMP {
				fedRAMPUsageSeen++
				require.Empty(t, request.Header.Get("x-openai-codex-luna-reserve"),
					"FedRAMP 账号的 usage 不得携带 Luna Reserve 头")
				require.Equal(t, "true", request.Header.Get("x-openai-fedramp"))
				continue
			}
			usageSeen++
			require.Equal(t, "1", request.Header.Get("x-openai-codex-luna-reserve"),
				"usage GET 的 x-openai-codex-luna-reserve 值固定为 1")
			require.Equal(t, []string{
				"user-agent", "authorization", "chatgpt-account-id",
				"x-openai-codex-luna-reserve", "accept", "host",
			}, order, "usage GET 必须在 chatgpt-account-id 之后携带 Luna Reserve 头")
		case "/backend-api/wham/rate-limit-reset-credits":
			creditsSeen++
			require.Empty(t, request.Header.Get("x-openai-codex-luna-reserve"),
				"rate-limit-reset-credits GET 不得携带 Luna Reserve 头")
			require.Equal(t, []string{
				"user-agent", "authorization", "chatgpt-account-id", "accept", "host",
			}, order, "rate-limit-reset-credits GET 使用 backend-client 精确线序")
		default:
			require.Empty(t, request.Header.Get("x-openai-codex-luna-reserve"),
				"%s：Luna Reserve 头只属于 usage GET", request.URL.Path)
		}
	}
	require.Equal(t, 2, usageSeen, "QueryUsageOnly 与 QueryUsage 各发一次 /wham/usage")
	require.Equal(t, 1, creditsSeen, "QueryUsage 补查一次 rate-limit-reset-credits")
	require.Equal(t, 1, fedRAMPUsageSeen)
}
