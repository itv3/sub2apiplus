package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 工作区路由发现（VC-4 第 6 项）：
//   - accounts/check 判定与官方一致：条目缺失／重复、缺少 origin 或 override 均为非默认；
//     origin 与 override 都落在画像接受取值内才是默认；
//   - 发现请求走 WHAM 配额同一出口，是管理端完整配额查询的首个 backend client 请求；
//     周期入口 QueryUsageOnly 不发出；画像没有 WorkspaceRouting 节时不发出；
//   - 判定为非默认后，画像 RoutedEndpointIDs 中的端点失败关闭，其余端点不受影响。

func workspaceRoutingTargetSection() profilecontract.WorkspaceRoutingSection {
	return profilecontract.WorkspaceRoutingSection{
		DiscoveryEndpointID:    "wham_accounts_check",
		DefaultOrigin:          "https://chatgpt.com",
		AcceptedOriginValues:   []string{"NO_CONSTRAINT", "https://chatgpt.com"},
		AcceptedOverrideValues: []string{"NO_CONSTRAINT"},
		OverrideHeader:         "x-openai-account-routing-override",
		NonDefaultAction:       "fail_closed",
		RoutedEndpointIDs:      []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS},
	}
}

// officialCodexProfileDeclaresWorkspaceRouting 判断画像是否声明工作区路由相关结构：
// WorkspaceRouting 节，或目标画像形态的发现端点（workspaceRoutingTargetMutation 追加的正是这两项）。
func officialCodexProfileDeclaresWorkspaceRouting(profile profilecontract.ExecutableProfile) bool {
	if profile.Optional().WorkspaceRouting != nil {
		return true
	}
	discoveryID := workspaceRoutingTargetSection().DiscoveryEndpointID
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == discoveryID {
			return true
		}
	}
	return false
}

// workspaceRoutingLegacyMutation 去掉 WorkspaceRouting 节与它声明的发现端点（节缺席时按目标
// 画像形态的发现端点 ID 删除），还原为未声明工作区路由的旧画像形态；底稿本就没有时为空操作。
func workspaceRoutingLegacyMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		discoveryID := workspaceRoutingTargetSection().DiscoveryEndpointID
		if len(doc.WorkspaceRouting) > 0 {
			var section profilecontract.WorkspaceRoutingSection
			require.NoError(t, json.Unmarshal(doc.WorkspaceRouting, &section))
			if section.DiscoveryEndpointID != "" {
				discoveryID = section.DiscoveryEndpointID
			}
		}
		doc.WorkspaceRouting = nil
		kept := make([]profilecontract.SnapshotEndpoint, 0, len(doc.Endpoints))
		for _, endpoint := range doc.Endpoints {
			if endpoint.ID != discoveryID {
				kept = append(kept, endpoint)
			}
		}
		doc.Endpoints = kept
	}
}

// workspaceRoutingTargetMutation 追加目标画像的发现端点（按 wham_settings_user 的 backend
// client 画像复制，路径改为 accounts/check）与 WorkspaceRouting 节。
func workspaceRoutingTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		// 先还原为旧形态再追加：VC-6 晋升后合成底稿（Active）就是已声明发现端点与该节的目标
		// 画像，直接追加会出现重复端点。候选期底稿是旧画像，还原是空操作。
		workspaceRoutingLegacyMutation(t)(doc)
		discovery := *syntheticServiceSnapshotEndpoint(t, doc, officialCodexEndpointWhamSettingsUser)
		discovery.ID = "wham_accounts_check"
		discovery.Path = "/backend-api/wham/accounts/check"
		doc.Endpoints = append(doc.Endpoints, discovery)
		doc.WorkspaceRouting = syntheticServiceRawSection(t, workspaceRoutingTargetSection())
	}
}

func resetOfficialCodexWorkspaceRoutingResults(t *testing.T) {
	t.Helper()
	clear := func() {
		officialCodexWorkspaceRoutingResults.Range(func(key, _ any) bool {
			officialCodexWorkspaceRoutingResults.Delete(key)
			return true
		})
	}
	clear()
	t.Cleanup(clear)
}

func TestDecideOfficialCodexWorkspaceRouting(t *testing.T) {
	section := workspaceRoutingTargetSection()
	cases := []struct {
		name        string
		body        string
		wantDefault bool
		wantReason  string
	}{
		{name: "两个字段均为 NO_CONSTRAINT", wantDefault: true,
			body: `{"accounts":[{"id":"acct","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"}],"account_ordering":["acct"],"default_account_id":"acct"}`},
		{name: "显式默认 origin", wantDefault: true,
			body: `{"accounts":[{"id":"other","workspace_backend_origin":"https://eu.chatgpt.com","account_routing_override":"us"},{"id":"acct","workspace_backend_origin":"https://chatgpt.com","account_routing_override":"NO_CONSTRAINT"}]}`},
		{name: "非默认 origin", wantReason: "非默认工作区路由",
			body: `{"accounts":[{"id":"acct","workspace_backend_origin":"https://us.chatgpt.com","account_routing_override":"NO_CONSTRAINT"}]}`},
		{name: "需要 override", wantReason: "非默认工作区路由",
			body: `{"accounts":[{"id":"acct","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"us_cr"}]}`},
		{name: "缺少当前工作区", wantReason: "不含当前工作区",
			body: `{"accounts":[{"id":"other","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"}]}`},
		{name: "重复出现当前工作区", wantReason: "重复出现当前工作区",
			body: `{"accounts":[{"id":"acct","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"},{"id":"acct","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"}]}`},
		{name: "缺少 origin", wantReason: "缺少 workspace_backend_origin",
			body: `{"accounts":[{"id":"acct","account_routing_override":"NO_CONSTRAINT"}]}`},
		{name: "缺少 override", wantReason: "缺少 account_routing_override",
			body: `{"accounts":[{"id":"acct","workspace_backend_origin":"NO_CONSTRAINT"}]}`},
		{name: "ChatGPT 映射形态不带路由字段", wantReason: "缺少 workspace_backend_origin",
			body: `{"accounts":{"acct":{"account":{"account_id":"acct"}}},"account_ordering":["acct"]}`},
		{name: "非法 JSON", wantReason: "不是合法 JSON", body: `not-json`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			result := decideOfficialCodexWorkspaceRouting(&section, "acct", []byte(tc.body))
			require.Equal(t, tc.wantDefault, result.Default, result.Reason)
			if tc.wantReason != "" {
				require.Contains(t, result.Reason, tc.wantReason)
			}
		})
	}
	require.True(t, decideOfficialCodexWorkspaceRouting(nil, "acct", []byte(`{}`)).Default, "画像没有该节时恒为默认")
}

// TestOfficialCodexWorkspaceRoutingGateFollowsProfileAndDiscovery 改动前直接把 Active 当作旧画像；
// VC-6 晋升后 Active 是已声明 WorkspaceRouting 节的目标画像。现先在正式目录逐槽位核验“缓存了
// 非默认判定时，受路由端点失败关闭当且仅当该槽位画像声明该节且端点在 RoutedEndpointIDs 中”，
// 再以 Active 为底稿去掉／追加该节做旧画像与目标画像对照（候选期去节是空操作）。
func TestOfficialCodexWorkspaceRoutingGateFollowsProfileAndDiscovery(t *testing.T) {
	resetOfficialCodexWorkspaceRoutingResults(t)
	account := &Account{ID: 190, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"chatgpt_account_id": "acct-gate"}}
	recordOfficialCodexWorkspaceRouting(officialCodexWorkspaceRoutingResult{
		ChatGPTAccountID: "acct-gate", Reason: "非默认工作区路由",
	})

	for _, mode := range officialCodexFormalModes {
		section := officialCodexFormalExecutableProfile(t, mode).Optional().WorkspaceRouting
		err := officialCodexWorkspaceRoutingGate(mode, account, officialCodexEndpointResponsesHTTP)
		if section != nil && slices.Contains(section.RoutedEndpointIDs, officialCodexEndpointResponsesHTTP) {
			require.True(t, errors.Is(err, ErrOfficialCodexWorkspaceRoutingNonDefault),
				"%s 槽位声明了路由节，受路由端点必须失败关闭：%v", mode, err)
		} else {
			require.NoError(t, err, "%s 槽位未声明路由节，缓存的非默认判定不得影响出站", mode)
		}
	}

	// 旧画像没有 WorkspaceRouting 节：即使缓存了非默认判定也放行。
	withOfficialCodexLegacySyntheticProfile(t, "WorkspaceRouting 节与发现端点",
		officialCodexProfileDeclaresWorkspaceRouting, workspaceRoutingLegacyMutation(t))
	require.NoError(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointResponsesHTTP))

	withOfficialCodexSyntheticProfile(t, workspaceRoutingTargetMutation(t))
	err := officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointResponsesHTTP)
	require.True(t, errors.Is(err, ErrOfficialCodexWorkspaceRoutingNonDefault), "受路由端点必须失败关闭：%v", err)
	require.Error(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointResponsesWS))
	require.NoError(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointWhamUsage),
		"不在 RoutedEndpointIDs 的端点不受影响")

	recordOfficialCodexWorkspaceRouting(officialCodexWorkspaceRoutingResult{ChatGPTAccountID: "acct-gate", Default: true})
	require.NoError(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointResponsesHTTP))

	unknown := &Account{ID: 191, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"chatgpt_account_id": "acct-unknown"}}
	require.NoError(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, unknown, officialCodexEndpointResponsesHTTP),
		"尚未发现的工作区首期放行")
}

type workspaceRoutingDiscoveryCall struct {
	endpointID       codexEndpointID
	authorization    string
	chatGPTAccountID string
	upstreamRequests int
}

func stubOfficialCodexWorkspaceRoutingDiscovery(
	t *testing.T,
	upstream *quotaRedirectingUpstream,
	body string,
	status int,
) *[]workspaceRoutingDiscoveryCall {
	t.Helper()
	calls := &[]workspaceRoutingDiscoveryCall{}
	previous := doOfficialCodexWorkspaceRoutingDiscovery
	doOfficialCodexWorkspaceRoutingDiscovery = func(
		_ *OpenAIQuotaService, _ context.Context, _ int64, _ string, endpointID codexEndpointID, headers http.Header,
	) (int, []byte, error) {
		*calls = append(*calls, workspaceRoutingDiscoveryCall{
			endpointID: endpointID, authorization: headers.Get("authorization"),
			chatGPTAccountID: headers.Get("chatgpt-account-id"), upstreamRequests: len(upstream.requests),
		})
		return status, []byte(body), nil
	}
	t.Cleanup(func() { doOfficialCodexWorkspaceRoutingDiscovery = previous })
	return calls
}

func TestOpenAIQuotaQueryUsageDiscoversWorkspaceRoutingFirst(t *testing.T) {
	resetOfficialCodexWorkspaceRoutingResults(t)
	account := &Account{
		ID: 192, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{"chatgpt_account_id": "acct-routing"},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-routing",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer server.Close()
	upstream := newQuotaRedirectingUpstream(server)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	calls := stubOfficialCodexWorkspaceRoutingDiscovery(t, upstream,
		`{"accounts":[{"id":"acct-routing","workspace_backend_origin":"https://us.chatgpt.com","account_routing_override":"us"}]}`,
		http.StatusOK)

	// 旧画像：不发出发现请求。改动前直接用 Active 充当旧画像；VC-6 晋升后 Active 是已声明
	// WorkspaceRouting 节的目标画像，改为以 Active 为底稿去掉该节与发现端点（候选期是空操作）。
	withOfficialCodexLegacySyntheticProfile(t, "WorkspaceRouting 节与发现端点",
		officialCodexProfileDeclaresWorkspaceRouting, workspaceRoutingLegacyMutation(t))
	_, err := service.QueryUsage(context.Background(), account.ID)
	require.NoError(t, err)
	require.Empty(t, *calls, "画像没有 WorkspaceRouting 节时不得发出发现请求")

	withOfficialCodexSyntheticProfile(t, workspaceRoutingTargetMutation(t))
	_, err = service.QueryUsageOnly(context.Background(), account.ID)
	require.NoError(t, err)
	require.Empty(t, *calls, "周期入口只产生一次官方请求，不发出发现请求")

	before := len(upstream.requests)
	_, err = service.QueryUsage(context.Background(), account.ID)
	require.NoError(t, err, "发现判定只影响受路由端点，不影响配额查询本身")
	require.Len(t, *calls, 1)
	call := (*calls)[0]
	require.Equal(t, codexEndpointID("wham_accounts_check"), call.endpointID, "发现端点取自画像节")
	require.Equal(t, before, call.upstreamRequests, "发现请求必须是本次 backend client 的首个请求")
	require.Equal(t, "Bearer token-routing", call.authorization)
	require.Equal(t, "acct-routing", call.chatGPTAccountID)

	result, found := lookupOfficialCodexWorkspaceRouting("acct-routing")
	require.True(t, found)
	require.False(t, result.Default)
	require.Equal(t, "https://us.chatgpt.com", result.BackendOrigin)
	require.Equal(t, "us", result.RoutingOverride)
	require.Error(t, officialCodexWorkspaceRoutingGate(officialClientProfileModeActive, account, officialCodexEndpointResponsesHTTP))
}

// 发现请求走真实 Executor 时的尝试预算：Executor 在编译之前预留尝试序号，发现请求因此占用
// WHAM 配额 invocation 的一次尝试。画像声明 WorkspaceRouting 节时，完整配额查询必须在同一
// invocation 内依次发出 accounts/check、settings/user（画像含该端点时）、usage 与
// rate-limit-reset-credits，不得因预算耗尽丢掉末尾补查；周期入口与重置入口取同一预算，
// 三个入口解析出同一 Bundle，账号级 backend client 长连接池不因预算不同而分裂。
func TestOpenAIQuotaAttemptBudgetCoversWorkspaceRoutingDiscovery(t *testing.T) {
	resetOfficialCodexWorkspaceRoutingResults(t)
	discoveryMode := ""
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		want := officialCodexQuotaBaseAttemptBudget
		if officialCodexOptionalSectionsForMode(mode).WorkspaceRouting != nil {
			want++
			if discoveryMode == "" {
				discoveryMode = mode
			}
		}
		require.Equal(t, want, officialCodexQuotaAttemptBudget(mode), "mode=%s", mode)
	}
	if discoveryMode == "" {
		t.Skip("Active/Previous 画像都未声明 WorkspaceRouting 节，预算保持基础值")
	}
	section := officialCodexOptionalSectionsForMode(discoveryMode).WorkspaceRouting

	account := &Account{
		ID: 195, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{"chatgpt_account_id": "acct-routing-budget"},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-routing-budget",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
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
		guard, upstream, officialCodexExecutorID, officialegress.ReleaseMode(discoveryMode),
	)
	require.NoError(t, err)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	service.officialEgress = egressRuntime
	runtimeState := defaultOfficialCodexRuntimeState()
	runtimeState.ProfileMode = discoveryMode
	runtimeContext, err := withOfficialCodexRuntimeState(context.Background(), runtimeState)
	require.NoError(t, err)

	_, err = service.QueryUsage(runtimeContext, account.ID)
	require.NoError(t, err)
	want := []string{section.DiscoveryEndpointID}
	if _, settingsErr := resolveCodexEndpointForMode(
		discoveryMode, codexEndpointID(officialCodexEndpointWhamSettingsUser),
	); settingsErr == nil {
		want = append(want, officialCodexEndpointWhamSettingsUser)
	}
	want = append(want, officialCodexEndpointWhamUsage, officialCodexEndpointWhamResetCredits)
	identities := quotaAttemptIdentities(t, upstream.requests)
	require.Equal(t, want, quotaAttemptEndpointIDs(identities),
		"发现请求与配额查询共用同一 invocation，末尾仍须补查 rate-limit-reset-credits")
	for index, identity := range identities {
		require.Equal(t, uint32(index+1), identity.AttemptOrdinal)
		require.Equal(t, identities[0].BundleDigest, identity.BundleDigest)
	}
	usage := identities[len(identities)-2]

	before := len(upstream.requests)
	_, err = service.QueryUsageOnly(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.ResetCredit(runtimeContext, account.ID)
	require.NoError(t, err)
	later := quotaAttemptIdentities(t, upstream.requests[before:])
	require.Equal(t, []string{
		officialCodexEndpointWhamUsage, officialCodexEndpointWhamConsumeResetCredit,
	}, quotaAttemptEndpointIDs(later))
	for _, identity := range later {
		require.Equal(t, usage.BundleDigest, identity.BundleDigest, "三个 WHAM 入口必须解析出同一 Bundle")
	}
	require.Equal(t, usage.ConnectionPoolDigest, later[0].ConnectionPoolDigest,
		"周期入口与完整配额查询复用同一账号级长连接池")
}

func quotaAttemptIdentities(t *testing.T, requests []*http.Request) []officialegress.AttemptIdentity {
	t.Helper()
	identities := make([]officialegress.AttemptIdentity, 0, len(requests))
	for _, request := range requests {
		identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
		require.True(t, ok, "%s 缺少 attempt 身份", request.URL.Path)
		identities = append(identities, identity)
	}
	return identities
}

func quotaAttemptEndpointIDs(identities []officialegress.AttemptIdentity) []string {
	endpointIDs := make([]string, 0, len(identities))
	for _, identity := range identities {
		endpointIDs = append(endpointIDs, identity.EndpointID)
	}
	return endpointIDs
}

func TestOpenAIQuotaWorkspaceRoutingDiscoveryFailureIsNotCached(t *testing.T) {
	resetOfficialCodexWorkspaceRoutingResults(t)
	withOfficialCodexSyntheticProfile(t, workspaceRoutingTargetMutation(t))
	upstream := &quotaRedirectingUpstream{}
	stubOfficialCodexWorkspaceRoutingDiscovery(t, upstream, `{"error":"unavailable"}`, http.StatusServiceUnavailable)
	service := &OpenAIQuotaService{}
	_, performed, err := service.discoverOfficialCodexWorkspaceRouting(
		context.Background(), officialClientProfileModeActive, 193, "acct-failure", "", http.Header{},
	)
	require.True(t, performed)
	require.Error(t, err)
	_, found := lookupOfficialCodexWorkspaceRouting("acct-failure")
	require.False(t, found, "发现失败不缓存判定，下次发现重试")
}

// HTTP 出站上下文挂载处的闸门：非默认判定使 /v1/responses 失败关闭，旧画像不受影响。
func TestAttachOfficialEgressHTTPContextAppliesWorkspaceRoutingGate(t *testing.T) {
	resetOfficialCodexWorkspaceRoutingResults(t)
	account := newOfficialOpenAIHTTPTestAccount(194)
	recordOfficialCodexWorkspaceRouting(officialCodexWorkspaceRoutingResult{
		ChatGPTAccountID: "chatgpt-test-account", Reason: "非默认工作区路由",
	})
	attach := func(t *testing.T, mode string) error {
		t.Helper()
		body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
		_, err := attachOfficialEgressHTTPContextWithMode(req, c, account, PlatformOpenAI, mode)
		return err
	}
	// 正式目录逐槽位：挂载处失败关闭当且仅当该槽位画像声明 WorkspaceRouting 节且 /responses
	// 在受路由端点中。改动前只断言 Active 是旧画像，VC-6 晋升后 Active 换成目标画像。
	for _, mode := range officialCodexFormalModes {
		section := officialCodexFormalExecutableProfile(t, mode).Optional().WorkspaceRouting
		err := attach(t, mode)
		if section != nil && slices.Contains(section.RoutedEndpointIDs, officialCodexEndpointResponsesHTTP) {
			require.True(t, errors.Is(err, ErrOfficialCodexWorkspaceRoutingNonDefault),
				"%s 槽位声明了路由节，非默认路由必须失败关闭：%v", mode, err)
		} else {
			require.NoError(t, err, "%s 槽位未声明路由节，挂载不受缓存判定影响", mode)
		}
	}

	withOfficialCodexLegacySyntheticProfile(t, "WorkspaceRouting 节与发现端点",
		officialCodexProfileDeclaresWorkspaceRouting, workspaceRoutingLegacyMutation(t))
	require.NoError(t, attach(t, officialClientProfileModeActive), "旧画像没有 WorkspaceRouting 节，挂载不受缓存判定影响")

	withOfficialCodexSyntheticProfile(t, workspaceRoutingTargetMutation(t))
	err := attach(t, officialClientProfileModeActive)
	require.True(t, errors.Is(err, ErrOfficialCodexWorkspaceRoutingNonDefault), "非默认路由必须失败关闭：%v", err)
}
