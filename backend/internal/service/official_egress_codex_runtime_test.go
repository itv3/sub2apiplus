package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodex0145RuntimeStateRejectsForgedThirdPartyConditions(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "third-party/1.0")
	c.Request.Header.Set("x-openai-subagent", "guardian")
	c.Request.Header.Set("x-openai-memgen-request", "true")
	c.Request.Header.Set("x-codex-parent-thread-id", "11111111-1111-4111-8111-111111111111")
	c.Request.Header.Set("x-responsesapi-include-timing-metrics", "true")
	c.Request.Header.Set("x-openai-internal-codex-residency", "us")

	state, err := resolveOfficialCodex0145RuntimeState(c, officialEgressTestAccount(145, PlatformOpenAI))
	require.NoError(t, err)
	require.Equal(t, officialCodexSurfaceExec, state.SurfaceID)
	require.True(t, state.UserAgentSuffixEnabled)
	require.Empty(t, state.ConditionalHeaders)
}

func TestOfficialCodex0145RuntimeStateUsesManagedAccountConditions(t *testing.T) {
	account := officialEgressTestAccount(145, PlatformOpenAI)
	account.Extra[officialCodexResidencyAccountExtraKey] = "us"
	account.Extra[officialCodexRuntimeMetricsAccountExtra] = true

	state, err := resolveOfficialCodex0145RuntimeState(nil, account)
	require.NoError(t, err)
	require.Equal(t, "us", state.ConditionalHeaders["x-openai-internal-codex-residency"])
	require.Equal(t, "true", state.ConditionalHeaders["x-responsesapi-include-timing-metrics"])

	account.Extra[officialCodexResidencyAccountExtraKey] = "eu"
	_, err = resolveOfficialCodex0145RuntimeState(nil, account)
	require.ErrorContains(t, err, "只允许 us")
}

func TestOfficialCodex0145RuntimeStateAcceptsOnlyExactPairedSurfaceIdentity(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)

	for _, testCase := range []struct {
		name          string
		surfaceID     string
		includeSuffix bool
		originator    string
	}{
		{name: "exec 有 suffix", surfaceID: officialCodexSurfaceExec, includeSuffix: true, originator: "codex_exec"},
		{name: "exec 无 suffix", surfaceID: officialCodexSurfaceExec, includeSuffix: false, originator: "codex_exec"},
		{name: "TUI 有 suffix", surfaceID: officialCodexSurfaceTUI, includeSuffix: true, originator: "codex-tui"},
		{name: "TUI 无 suffix", surfaceID: officialCodexSurfaceTUI, includeSuffix: false, originator: "codex-tui"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			userAgent, renderErr := profile.RenderUserAgent(testCase.surfaceID, testCase.includeSuffix)
			require.NoError(t, renderErr)
			c := officialCodex0145RuntimeIngress(userAgent, testCase.originator)
			state, resolveErr := resolveOfficialCodex0145RuntimeState(c, officialEgressTestAccount(145, PlatformOpenAI))
			require.NoError(t, resolveErr)
			require.Equal(t, testCase.surfaceID, state.SurfaceID)
			require.Equal(t, testCase.includeSuffix, state.UserAgentSuffixEnabled)
		})
	}
	xtermUserAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI,
		"xterm-256color",
		true,
	)
	require.NoError(t, err)
	xtermState, err := resolveOfficialCodex0145RuntimeState(
		officialCodex0145RuntimeIngress(xtermUserAgent, "codex-tui"),
		officialEgressTestAccount(145, PlatformOpenAI),
	)
	require.NoError(t, err)
	require.Equal(t, "xterm-256color", xtermState.TerminalToken)

	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	_, err = resolveOfficialCodex0145RuntimeState(
		officialCodex0145RuntimeIngress(userAgent, "codex-tui"),
		officialEgressTestAccount(145, PlatformOpenAI),
	)
	require.ErrorContains(t, err, "画像允许的进程阶段")

	_, err = resolveOfficialCodex0145RuntimeState(
		officialCodex0145RuntimeIngress(userAgent+" forged", "codex_exec"),
		officialEgressTestAccount(145, PlatformOpenAI),
	)
	require.ErrorContains(t, err, "配对")
}

func TestOfficialCodex0145RuntimeStateModelsBootstrapIsProfilePhase(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	for _, surfaceID := range []string{officialCodexSurfaceExec, officialCodexSurfaceTUI} {
		userAgent, renderErr := profile.RenderUserAgent(surfaceID, false)
		require.NoError(t, renderErr)
		ingress := officialCodex0145RuntimeIngress(userAgent, "codex_cli_rs")
		state, resolveErr := resolveOfficialCodex0145RuntimeState(
			ingress,
			officialEgressTestAccount(145, PlatformOpenAI),
			codex0145EndpointID(officialCodexEndpointModels),
		)
		require.NoError(t, resolveErr)
		require.Equal(t, officialCodexProcessPhaseInitialModels, state.ProcessPhase)
		require.Equal(t, "codex_cli_rs", state.Originator)
		require.False(t, state.UserAgentSuffixEnabled)

		_, resolveErr = resolveOfficialCodex0145RuntimeState(
			ingress,
			officialEgressTestAccount(145, PlatformOpenAI),
			codex0145EndpointID(officialCodexEndpointResponsesHTTP),
		)
		require.ErrorContains(t, resolveErr, "画像允许的进程阶段")
	}
}

func TestOfficialCodex0145RuntimeStateCrossChecksSubagentMetadata(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	parentID := "11111111-1111-4111-8111-111111111111"

	guardian := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	guardian.Request.Header.Set("x-openai-subagent", "Other(custom-review)")
	guardian.Request.Header.Set("x-codex-parent-thread-id", parentID)
	guardian.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"Other(custom-review)","parent_thread_id":"`+parentID+`"}`)
	state, err := resolveOfficialCodex0145RuntimeState(guardian, officialEgressTestAccount(145, PlatformOpenAI))
	require.NoError(t, err)
	require.Equal(t, "Other(custom-review)", state.ConditionalHeaders["x-openai-subagent"])
	require.Equal(t, parentID, state.ConditionalHeaders["x-codex-parent-thread-id"])

	mismatch := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	mismatch.Request.Header.Set("x-openai-subagent", "guardian")
	mismatch.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"review"}`)
	_, err = resolveOfficialCodex0145RuntimeState(mismatch, officialEgressTestAccount(145, PlatformOpenAI))
	require.ErrorContains(t, err, "subagent 条件")

	memgen := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	memgen.Request.Header.Set("x-openai-subagent", "memory_consolidation")
	memgen.Request.Header.Set("x-openai-memgen-request", "true")
	memgen.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"memory_consolidation"}`)
	state, err = resolveOfficialCodex0145RuntimeState(memgen, officialEgressTestAccount(145, PlatformOpenAI))
	require.NoError(t, err)
	require.Equal(t, "true", state.ConditionalHeaders["x-openai-memgen-request"])

	threadSpawn := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	threadSpawn.Request.Header.Set("x-openai-subagent", "collab_spawn")
	threadSpawn.Request.Header.Set("x-codex-parent-thread-id", parentID)
	threadSpawn.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"thread_spawn","parent_thread_id":"`+parentID+`"}`)
	state, err = resolveOfficialCodex0145RuntimeState(threadSpawn, officialEgressTestAccount(145, PlatformOpenAI))
	require.NoError(t, err)
	require.Equal(t, "collab_spawn", state.ConditionalHeaders["x-openai-subagent"])

	memorySubagent := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	memorySubagent.Request.Header.Set("x-openai-subagent", "memory_consolidation")
	memorySubagent.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"memory_consolidation"}`)
	state, err = resolveOfficialCodex0145RuntimeState(memorySubagent, officialEgressTestAccount(145, PlatformOpenAI))
	require.NoError(t, err)
	require.Empty(t, state.ConditionalHeaders["x-openai-memgen-request"])
}

func TestOfficialCodex0145RuntimeStatePropagatesAndProjectsByEndpoint(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceTUI, false)
	require.NoError(t, err)
	account := officialEgressTestAccount(145, PlatformOpenAI)
	account.Extra[officialCodexResidencyAccountExtraKey] = "us"
	ingress := officialCodex0145RuntimeIngress(userAgent, "codex-tui")

	ctx, err := bindOfficialCodex0145RuntimeStateFromIngress(context.Background(), ingress, account)
	require.NoError(t, err)
	req := httptest.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api/wham/usage", nil).WithContext(ctx)
	req, egressContext, err := attachOfficialCodex0145EndpointRequest(
		req,
		account,
		codex0145EndpointID(officialCodexEndpointWhamUsage),
		"runtime-propagation-test",
	)
	require.NoError(t, err)
	actualUserAgent, actualOriginator, err := officialCodex0145ProcessIdentity(egressContext)
	require.NoError(t, err)
	require.Equal(t, userAgent, actualUserAgent)
	require.Equal(t, "codex-tui", actualOriginator)

	req.Header.Set("user-agent", actualUserAgent)
	req.Header.Set("authorization", "Bearer test")
	req.Header.Set("chatgpt-account-id", "acct_test")
	req.Header.Set("accept", "*/*")
	req.Header.Set("x-openai-internal-codex-residency", "forged")
	req.Header.Set("x-openai-subagent", "forged")
	_, err = officialCodex0145FinalizeEndpointHeaders(egressContext, req.Header, nil)
	require.NoError(t, err)
	require.Empty(t, req.Header.Get("x-openai-internal-codex-residency"))
	require.Empty(t, req.Header.Get("x-openai-subagent"))
}

func TestOfficialCodex0145HTTPAttachUsesRuntimeFrozenBeforeTokenRefresh(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	tuiUserAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI,
		"xterm-256color",
		false,
	)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(tuiUserAgent, "codex-tui")
	account := officialEgressTestAccount(145, PlatformOpenAI)
	ctx, err := bindOfficialCodex0145RuntimeStateFromIngress(
		context.Background(),
		c,
		account,
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)

	// 模拟刷新完成后兼容层改写入站 header；最终 HTTP attach 只能使用 context
	// 中的首次快照，不能产生第二套进程身份。
	execUserAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c.Request.Header.Set("user-agent", execUserAgent)
	c.Request.Header.Set("originator", "codex_exec")
	req := httptest.NewRequest(
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		nil,
	).WithContext(ctx)
	req, err = attachOfficialEgressHTTPContext(req, c, account, PlatformOpenAI)
	require.NoError(t, err)
	egressContext, exists := OfficialEgressContextFromContext(req.Context())
	require.True(t, exists)
	require.Equal(t, officialCodexSurfaceTUI, egressContext.codexRuntimeState.SurfaceID)
	require.Equal(t, "codex-tui", egressContext.codexRuntimeState.Originator)
	require.Equal(t, "xterm-256color", egressContext.codexRuntimeState.TerminalToken)
	require.False(t, egressContext.codexRuntimeState.UserAgentSuffixEnabled)
}

func TestOfficialCodex0145HTTPCompressionFeatureSurvivesIngressBodyDecode(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	body := []byte(`{"model":"gpt-5.5","input":[]}`)
	require.NoError(t, compressOfficialOpenAIHTTPRequest(c.Request, body, 3))
	c.Request = c.Request.WithContext(
		WithOfficialCodex0145IngressRuntime(c.Request.Context(), c),
	)

	// 真实调用通用 body reader，证明冻结发生在它移除线上的编码头之前。
	decoded, err := pkghttputil.ReadRequestBodyWithPrealloc(c.Request)
	require.NoError(t, err)
	require.JSONEq(t, string(body), string(decoded))
	require.Empty(t, c.Request.Header.Get("Content-Encoding"))
	ctx, err := bindOfficialCodex0145RuntimeStateFromIngress(
		c.Request.Context(),
		c,
		officialEgressTestAccount(145, PlatformOpenAI),
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)
	state, found, err := officialCodex0145RuntimeStateFromContext(ctx)
	require.NoError(t, err)
	require.True(t, found)
	require.True(t, state.RequestCompressionEnabled)
}

func TestOfficialCodex0145HTTPCompressionFeatureIgnoresLateHeaderMutation(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	c.Request = c.Request.WithContext(
		WithOfficialCodex0145IngressRuntime(c.Request.Context(), c),
	)

	// 冻结后再伪造 zstd 不能改变该调用的 feature，证明 Finalizer 不回读当前 header。
	c.Request.Header.Set("Content-Encoding", "zstd")
	ctx, err := bindOfficialCodex0145RuntimeStateFromIngress(
		c.Request.Context(),
		c,
		officialEgressTestAccount(145, PlatformOpenAI),
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)
	state, found, err := officialCodex0145RuntimeStateFromContext(ctx)
	require.NoError(t, err)
	require.True(t, found)
	require.False(t, state.RequestCompressionEnabled)
}

func TestWithOfficialCodex0145IngressRuntimeKeepsFirstWireSnapshot(t *testing.T) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	c.Request.Header.Set("Content-Encoding", "zstd")

	first := WithOfficialCodex0145IngressRuntime(c.Request.Context(), c)
	// 模拟 Composite 路由完成解压、后续兼容层又改写进程头；Handler 的重复捕获
	// 必须返回同一上下文，不能用标准化后的请求覆盖首次 wire 快照。
	c.Request.Header.Del("Content-Encoding")
	c.Request.Header.Set("User-Agent", "third-party/1.0")
	c.Request.Header.Del("originator")
	second := WithOfficialCodex0145IngressRuntime(first, c)

	require.True(t, first == second)
	snapshot, captured := officialCodex0145IngressRuntimeSnapshotFromContext(second)
	require.True(t, captured)
	require.True(t, snapshot.OfficialClient)
	require.Equal(t, userAgent, snapshot.UserAgent)
	require.Equal(t, "codex_exec", snapshot.Originator)
	require.True(t, snapshot.RequestCompressionEnabled)
}

func TestOfficialCodex0145RuntimeStateIsDeepCopiedAcrossContextFreeze(t *testing.T) {
	state := defaultOfficialCodex0145RuntimeState()
	state.ConditionalHeaders["x-openai-internal-codex-residency"] = "us"
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:         145,
		TargetPlatform:    PlatformOpenAI,
		InboundEndpoint:   "/v1/responses",
		Transport:         OfficialEgressTransportWebSocket,
		UpstreamHost:      "chatgpt.com",
		ProfileVersion:    officialCodexVersion0145,
		ProfileMode:       officialClientProfileModeActive,
		AccountType:       AccountTypeOAuth,
		CodexRuntimeState: state,
	})
	state.ConditionalHeaders["x-openai-internal-codex-residency"] = "mutated"
	require.Equal(t, "us", egressContext.codexRuntimeState.ConditionalHeaders["x-openai-internal-codex-residency"])

	frozen, err := egressContext.Freeze()
	require.NoError(t, err)
	egressContext.codexRuntimeState.ConditionalHeaders["x-openai-internal-codex-residency"] = "changed"
	require.Equal(t, "us", frozen.codexRuntimeState.ConditionalHeaders["x-openai-internal-codex-residency"])
}

func officialCodex0145RuntimeIngress(userAgent string, originator string) *gin.Context {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", userAgent)
	c.Request.Header.Set("originator", originator)
	return c
}
