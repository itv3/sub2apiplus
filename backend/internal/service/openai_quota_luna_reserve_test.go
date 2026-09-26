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

// codexEndpointLunaReserveSlot 按结构事实读取 mode 对应画像中该端点的 Luna Reserve 槽位：
// 声明时返回画像常量值与 true，未声明时返回 false。
func codexEndpointLunaReserveSlot(t *testing.T, mode string, endpointID string) (string, bool) {
	t.Helper()
	endpoint, err := resolveCodexEndpointForMode(mode, codexEndpointID(endpointID))
	require.NoError(t, err, "%s 画像缺少端点 %s", mode, endpointID)
	for _, slot := range endpoint.OrderedHeaders() {
		if strings.EqualFold(slot.Name, "x-openai-codex-luna-reserve") {
			require.Equal(t, officialCodexConditionLunaReserve, slot.Condition)
			return slot.Value, true
		}
	}
	return "", false
}

// 画像没有 wham_usage 的 Luna Reserve 槽位时，该条件头只作为事实进入 compiler，不得以
// 普通 Header 身份泄漏到 wire；周期入口 QueryUsageOnly 与管理端 QueryUsage 都遵守画像闭集。
//
// 改动前本用例把负例固定在 previous 槽位，前提是该槽位恰好装着没有该槽位的旧版本画像；
// RuntimeCatalog 切换后两个槽位的画像都带该槽位，目录里不再有无槽位的发布。service 包又无法
// 向配额链路注入合成 Bundle（BundleResolver 只能由正式 ReleaseCatalog 构造），因此：
//   - “去掉该槽位”的合成发布负例在 officialegress 包
//     TestCompilerLunaReserveReachesWireOnlyThroughProfileSlot 中证明（Compiler 层，条件事实
//     成立也不出站，且出站 Header 与条件不成立时逐项一致）；
//   - 本用例在 service 真实链路上逐槽位核验同一闭集语义，判定只依据该槽位画像的结构事实，
//     不依赖目录里有哪个版本：
//     1. 两个入口发出的每个请求，Luna Reserve 头出现当且仅当请求声明了该条件事实（只有
//     usage 声明）且该槽位画像的对应端点声明了该槽位，出现时取画像常量值；若某个槽位
//     的画像没有该槽位，这一条就是 usage 端点的端到端负例；
//     2. 在同一链路上为该槽位画像未声明该槽位的 WHAM 端点显式声明条件事实，wire 上不得
//     出现该头——这是任何目录状态下都成立的真实链路负例。
//
// 正例见 TestCodexWhamRequestsUseClosedBackendClientProfile，目标发布槽位上的批准断言语义与
// 精确线序见 TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease。
func TestCodexWhamUsageLunaReserveDoesNotLeakWithoutProfileSlot(t *testing.T) {
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		t.Run(mode, func(t *testing.T) {
			// 目标画像声明 WorkspaceRouting 节时 QueryUsage 会先做工作区路由发现并缓存结果。
			resetOfficialCodexWorkspaceRoutingResults(t)
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
			// 两个槽位各自构造 runtime，保证请求真正跑在该槽位的画像上。
			guard, guardErr := officialegress.NewGuard(
				officialegress.DefaultGuard().Config(), officialegress.DefaultSinkCatalog(),
				officialegress.DefaultOfficialRouteCatalog(), officialegress.DefaultGuard().Recorder(),
			)
			require.NoError(t, guardErr)
			egressRuntime, runtimeErr := newOfficialEgressTransitionRuntimeWithExecutor(
				guard, upstream, officialCodexExecutorID, officialegress.ReleaseMode(mode),
			)
			require.NoError(t, runtimeErr)
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

			usageSeen := 0
			for _, request := range upstream.requests {
				identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
				require.True(t, ok, "%s 缺少 attempt 身份", request.URL.Path)
				want := ""
				if identity.EndpointID == officialCodexEndpointWhamUsage {
					usageSeen++
					if value, declared := codexEndpointLunaReserveSlot(t, mode, identity.EndpointID); declared {
						want = value
					}
				}
				require.Equal(t, want, request.Header.Get("x-openai-codex-luna-reserve"),
					"%s %s：Luna Reserve 头只能经该槽位画像声明的槽位出站", mode, request.URL.Path)
			}
			require.Equal(t, 2, usageSeen, "QueryUsageOnly 与 QueryUsage 各发一次 /wham/usage")

			// 真实链路负例：rate-limit-reset-credits 与 usage 同属 WHAM 配额出口，但画像不为它
			// 声明 Luna Reserve 槽位；在同一链路上为它显式声明条件事实，wire 上不得出现该头。
			_, declared := codexEndpointLunaReserveSlot(t, mode, officialCodexEndpointWhamResetCredits)
			require.False(t, declared, "负例前提：%s 画像的 rate-limit-reset-credits 不声明 Luna Reserve 槽位", mode)
			accessToken, chatGPTAccountID, proxyURL, fedRAMP, err := service.prepareUpstreamCall(runtimeContext, account.ID)
			require.NoError(t, err)
			require.False(t, fedRAMP)
			quotaHeaders, _, err := service.buildCodexQuotaHeaders(
				runtimeContext, mode, account.ID, accessToken, chatGPTAccountID,
			)
			require.NoError(t, err)
			factHeaders := codexQuotaUsageHeaders(quotaHeaders, false)
			require.Equal(t, "1", factHeaders.Get("x-openai-codex-luna-reserve"), "负例请求必须真实声明条件事实")
			before := len(upstream.requests)
			status, _, err := service.doCodexQuotaRequest(
				runtimeContext, account.ID, proxyURL, officialCodexEndpointWhamResetCredits, factHeaders, nil,
			)
			require.NoError(t, err)
			require.Equal(t, http.StatusOK, status)
			require.Len(t, upstream.requests, before+1)
			require.Empty(t, upstream.requests[before].Header.Get("x-openai-codex-luna-reserve"),
				"%s：画像未声明 Luna Reserve 槽位的端点，条件事实不得进入 wire", mode)
		})
	}
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
