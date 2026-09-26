package service

import (
	"context"
	"net/http"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// ============================================================================
// SPEC-EP-019 的晋升后门禁：WHAM backend-client 请求形态。
//
// 驱动的是生产 OpenAIQuotaService：管理端完整配额查询 QueryUsage（官方启动窗口：
// accounts/check 是 backend client 首个请求，随后 usage 与 reset-credits）、周期入口
// QueryUsageOnly（FedRAMP 账号）与安全 consume（ResetCredit）。请求经 WHAM 统一出口
// doCodexQuotaRequest 进入正式 Compiler/Executor，本地 TLS 终端观测真实 wire。
// 先例 TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease 只覆盖
// Luna Reserve；本门禁按目标画像重放本轮 EP-019 的全部检查项。
// ============================================================================

const codexGateWhamChatGPTAccountID = "acct-wham-promotion-gate"

// codexGateWhamResponse 为 WHAM 端点给出最小合法应答；accounts/check 返回当前工作区的
// 默认路由（NO_CONSTRAINT）。
func codexGateWhamResponse(request codexGateWireRequest) codexGateWireResponse {
	switch request.path {
	case "/backend-api/wham/accounts/check":
		return codexGateJSONResponse(`{"accounts":[{"id":"` + codexGateWhamChatGPTAccountID +
			`","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"}]}`)
	case "/backend-api/wham/rate-limit-reset-credits/consume":
		return codexGateJSONResponse(`{"code":"reset","windows_reset":1}`)
	default:
		return codexGateDefaultResponse(request)
	}
}

// SPEC-EP-019（change）：WHAM accounts/check、usage、reset credits 与安全 consume 使用独立
// backend-client 形态；accounts/check 是 backend client 首个请求，usage 在 ChatGPT 认证且非
// FedRAMP 时携带 Luna Reserve 头。
//
// 检查项与网关观测（真实 wire）：
//   - wham-get-paths：管理端完整配额查询发出的 WHAM GET 路径全集恰为 accounts/check、usage、
//     rate-limit-reset-credits，且 accounts/check 是第一个；
//   - wham-accounts-check-headers：accounts/check 使用 backend-client 线序，不带 Luna Reserve；
//   - wham-usage-headers / wham-usage-luna-reserve-value：usage 在 chatgpt-account-id 之后携带
//     x-openai-codex-luna-reserve: 1（FedRAMP 账号不带）；
//   - wham-credits-headers：rate-limit-reset-credits 使用 backend-client 线序，不带 Luna Reserve；
//   - wham-consume / wham-consume-body：consume 为 POST、线序精确，请求体顶层只有 redeem_request_id。
//
// cookie 在各线序中是可选项（jar 建立后才出现），这里按允许全集的有序子集断言。
func TestCodexWhamBackendClientReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "画像声明 wham_accounts_check 发现端点",
		func(profile profilecontract.ExecutableProfile) bool {
			endpoint, ok := codexGateEndpoint(profile, "wham_accounts_check")
			section := profile.Optional().WorkspaceRouting
			return ok && endpoint.Path == "/backend-api/wham/accounts/check" &&
				section != nil && section.DiscoveryEndpointID == endpoint.ID
		})
	profile := codexGateExecutableProfile(t, mode)
	credits := codexGateMustEndpoint(t, profile, officialCodexEndpointWhamResetCredits)
	cookieSlot, ok := codexGateHeaderSlot(credits, "cookie")
	require.True(t, ok, "目标画像的 reset-credits 声明 cookie 条件槽位")
	require.Equal(t, profilecontract.ConditionCookiePresent, cookieSlot.Condition)
	resetOfficialCodexWorkspaceRoutingResults(t)

	account := &Account{
		ID: 7191, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{"chatgpt_account_id": codexGateWhamChatGPTAccountID},
	}
	fedRAMP := &Account{
		ID: 7192, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id":         "acct-wham-promotion-gate-fedramp",
			"chatgpt_account_is_fedramp": true,
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account, fedRAMP.ID: fedRAMP}}
	tokens := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-wham-gate",
		OpenAITokenCacheKey(fedRAMP): "token-wham-gate-fedramp",
	}}, nil)
	server := startCodexGateWireServer(t, codexGateWhamResponse)
	upstream := newCodexGateWireUpstream(t, server)
	runtimeState := newCodexGateRuntime(t, upstream, mode)
	upstream.guard = runtimeState.Guard
	quota := NewOpenAIQuotaService(repo, nil, tokens, upstream)
	quota.officialEgress = runtimeState

	state := defaultOfficialCodexRuntimeState()
	state.ProfileMode = mode
	state.SurfaceID = officialCodexSurfaceTUI
	state.Originator = "codex-tui"
	state.TerminalToken = "xterm-256color"
	state.UserAgentSuffixEnabled = false
	ctx, err := withOfficialCodexRuntimeState(context.Background(), state)
	require.NoError(t, err)

	_, err = quota.QueryUsage(ctx, account.ID)
	require.NoError(t, err, "管理端完整配额查询失败")
	_, err = quota.ResetCredit(ctx, account.ID)
	require.NoError(t, err, "consume 失败")
	_, err = quota.QueryUsageOnly(ctx, fedRAMP.ID)
	require.NoError(t, err, "FedRAMP 账号的周期 usage 失败")

	var whamGets []codexGateWireRequest
	for _, request := range server.wireRequests() {
		if request.method == http.MethodGet && len(request.path) > len("/backend-api/wham/") &&
			request.path[:len("/backend-api/wham/")] == "/backend-api/wham/" &&
			request.header.Get("Chatgpt-Account-Id") == codexGateWhamChatGPTAccountID {
			whamGets = append(whamGets, request)
		}
	}
	var paths []string
	for _, request := range whamGets {
		if !slices.Contains(paths, request.path) {
			paths = append(paths, request.path)
		}
	}
	require.ElementsMatch(t, []string{
		"/backend-api/wham/accounts/check", "/backend-api/wham/usage", "/backend-api/wham/rate-limit-reset-credits",
	}, paths, "完整配额查询的 WHAM GET 路径全集必须与官方一致且无额外 WHAM GET")
	require.Equal(t, "/backend-api/wham/accounts/check", whamGets[0].path, "accounts/check 必须是 backend client 首个请求")

	backendClient := []string{"user-agent", "authorization", "chatgpt-account-id", "accept", "cookie", "host"}
	backendRequired := []string{"user-agent", "authorization", "chatgpt-account-id", "accept", "host"}
	withLuna := []string{"user-agent", "authorization", "chatgpt-account-id", "x-openai-codex-luna-reserve", "accept", "cookie", "host"}
	withLunaRequired := []string{"user-agent", "authorization", "chatgpt-account-id", "x-openai-codex-luna-reserve", "accept", "host"}
	for _, request := range whamGets {
		switch request.path {
		case "/backend-api/wham/accounts/check":
			codexGateRequireOrderedSubset(t, request.lowerHeaderNames(), backendClient, backendRequired, "accounts/check 线序")
			require.False(t, request.has("x-openai-codex-luna-reserve"), "accounts/check 不得携带 Luna Reserve")
		case "/backend-api/wham/usage":
			codexGateRequireOrderedSubset(t, request.lowerHeaderNames(), withLuna, withLunaRequired, "usage 线序")
			require.Equal(t, []string{"1"}, request.values("x-openai-codex-luna-reserve"), "usage 的 Luna Reserve 固定为 1")
		case "/backend-api/wham/rate-limit-reset-credits":
			codexGateRequireOrderedSubset(t, request.lowerHeaderNames(), backendClient, backendRequired, "reset-credits 线序")
			require.False(t, request.has("x-openai-codex-luna-reserve"), "reset-credits 不得携带 Luna Reserve")
		}
	}
	for _, request := range server.requestsForPath("/backend-api/wham/usage") {
		if request.header.Get("Chatgpt-Account-Id") != codexGateWhamChatGPTAccountID {
			require.False(t, request.has("x-openai-codex-luna-reserve"), "FedRAMP 账号的 usage 不得携带 Luna Reserve")
		}
	}

	consume := server.requestsForPath("/backend-api/wham/rate-limit-reset-credits/consume")
	require.Len(t, consume, 1)
	require.Equal(t, http.MethodPost, consume[0].method)
	require.Equal(t,
		[]string{"user-agent", "authorization", "chatgpt-account-id", "content-type", "accept", "host", "content-length"},
		consume[0].lowerHeaderNames(), "consume 线序必须精确")
	require.Equal(t, []string{"redeem_request_id"}, codexGateJSONFieldOrder(t, consume[0].body),
		"consume 请求体顶层只有 redeem_request_id")

	decision, found := lookupOfficialCodexWorkspaceRouting(codexGateWhamChatGPTAccountID)
	require.True(t, found, "accounts/check 的判定必须被记录")
	require.True(t, decision.Default)
}
