package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// TestOfficialCodexVersionSnapshotsDriveVersionResolution 锁定多版本机制：版本
// 解析只认注册表登记的快照，未登记版本明确失败而不回退到既有画像；端点入口的版本
// 来自 registry 的 release 指针而非写死常量。升级 Codex 版本时应当只需登记新快照
// 并调整 release 指针，不改动 §3.5.2 的共享接入点。
func TestOfficialCodexVersionSnapshotsDriveVersionResolution(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	require.Equal(t, officialCodexVersion0145, profile.Version)

	// 未登记版本必须明确失败，不得静默回退到任何既有快照。
	_, err = resolveCodexVersionProfile("0.146.0")
	require.ErrorContains(t, err, "未知 Codex 官方出站版本画像")

	active, err := officialCodexVersionForMode(officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, officialCodexVersion0145, active)
}

// TestOfficialCodex0145ProjectEndpointJSONBodyDropsFieldsOutsideClosedSet 锁定
// body 投影对画像闭集外字段的处理。新版本客户端的新增字段正是在这里被抹掉的，
// 请求仍然成功，因此该行为必须有测试和运行时告警共同看住。
func TestOfficialCodex0145ProjectEndpointJSONBodyDropsFieldsOutsideClosedSet(t *testing.T) {
	original := []byte(`{"id":"s1","model":"gpt-5.5","input":"q","commands":[],` +
		`"settings":{},"max_output_tokens":256,"future_field":"新版本新增","another":1}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)

	encoded, err := officialCodexProjectEndpointJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointAlphaSearch),
		payload,
		original,
		nil,
	)
	require.NoError(t, err)

	var projected map[string]any
	require.NoError(t, json.Unmarshal(encoded, &projected))
	require.Contains(t, projected, "id")
	require.Contains(t, projected, "max_output_tokens")
	require.NotContains(t, projected, "future_field")
	require.NotContains(t, projected, "another")
}

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

	state, err := resolveOfficialCodexRuntimeState(c, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, officialCodexSurfaceExec, state.SurfaceID)
	require.True(t, state.UserAgentSuffixEnabled)
	require.Empty(t, state.ConditionalHeaders)
}

func TestOfficialCodex0145RuntimeStateUsesManagedAccountConditions(t *testing.T) {
	account := officialEgressTestAccount(145, PlatformOpenAI)
	account.Extra[officialCodexResidencyAccountExtraKey] = "us"
	account.Extra[officialCodexRuntimeMetricsAccountExtra] = true

	state, err := resolveOfficialCodexRuntimeState(nil, account, officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "us", state.ConditionalHeaders["x-openai-internal-codex-residency"])
	require.Equal(t, "true", state.ConditionalHeaders["x-responsesapi-include-timing-metrics"])

	account.Extra[officialCodexResidencyAccountExtraKey] = "eu"
	_, err = resolveOfficialCodexRuntimeState(nil, account, officialClientProfileModeActive, officialClientProfileModeActive)
	require.ErrorContains(t, err, "只允许 us")
}

func TestOfficialCodex0145RuntimeStateIgnoresIngressWireIdentity(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
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
			state, resolveErr := resolveOfficialCodexRuntimeState(c, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
			require.NoError(t, resolveErr)
			require.Equal(t, officialCodexSurfaceExec, state.SurfaceID)
			require.True(t, state.UserAgentSuffixEnabled)
			require.Equal(t, "unknown", state.TerminalToken)
			require.Equal(t, "codex_exec", state.Originator)
		})
	}
	xtermUserAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI,
		"xterm-256color",
		true,
	)
	require.NoError(t, err)
	xtermState, err := resolveOfficialCodexRuntimeState(
		officialCodex0145RuntimeIngress(xtermUserAgent, "codex-tui"),
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		officialClientProfileModeActive)

	require.NoError(t, err)
	require.Equal(t, "unknown", xtermState.TerminalToken)

	// 出站一律按 active 画像定型：入站 originator 与 surface 不一致时不再拒绝，
	// 改用画像 originator 出站。
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	mismatchedOriginator, err := resolveOfficialCodexRuntimeState(
		officialCodex0145RuntimeIngress(userAgent, "codex-tui"),
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		officialClientProfileModeActive)

	require.NoError(t, err)
	require.Equal(t, officialCodexSurfaceExec, mismatchedOriginator.SurfaceID)
	require.Equal(t, "codex_exec", mismatchedOriginator.Originator)

	// UA 无法匹配任何 surface 时投影到默认画像状态，同样不拒绝服务。
	forged, err := resolveOfficialCodexRuntimeState(
		officialCodex0145RuntimeIngress(userAgent+" forged", "codex_exec"),
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		officialClientProfileModeActive)

	require.NoError(t, err)
	expected := defaultOfficialCodexRuntimeState()
	expected.ProfileMode = officialClientProfileModeActive
	require.Equal(t, expected, forged)
}

func TestOfficialCodex0145RuntimeStateProjectsOtherOfficialClientsToDefaultSurface(t *testing.T) {
	defaultState := defaultOfficialCodexRuntimeState()
	defaultState.ProfileMode = officialClientProfileModeActive
	account := officialEgressTestAccount(145, PlatformOpenAI)
	clients := []struct {
		name       string
		userAgent  string
		originator string
	}{
		{
			name:       "Codex Desktop 0.146",
			userAgent:  "Codex Desktop/0.146.0-alpha.3.1 (Mac OS 26.5.2; arm64) unknown (Codex Desktop; 26.721.81911)",
			originator: "Codex Desktop",
		},
		{
			name:       "Codex VS Code 0.146",
			userAgent:  "codex_vscode/0.146.0-alpha.3.1 (Ubuntu 24.4.0; aarch64) unknown (VS Code; 26.721.41059)",
			originator: "codex_vscode",
		},
		{
			name:       "Codex exec 后续版本",
			userAgent:  "codex_exec/0.146.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.146.0)",
			originator: "codex_exec",
		},
		{
			// 画像平台串固定为 Ubuntu，官方同版本客户端在 macOS/Windows 上同样
			// 无法通过完整线形校验；这类入口必须投影出站，不得拒绝服务。
			name:       "官方 0.145.0 非 Ubuntu 平台",
			userAgent:  "codex_exec/0.145.0 (Mac OS X 15.5.0; arm64) unknown (codex_exec; 0.145.0)",
			originator: "codex_exec",
		},
	}
	endpoints := []codexEndpointID{
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		codexEndpointID(officialCodexEndpointResponsesWS),
	}

	for _, client := range clients {
		for _, endpointID := range endpoints {
			t.Run(client.name+"/"+string(endpointID), func(t *testing.T) {
				c := officialCodex0145RuntimeIngress(client.userAgent, client.originator)
				// 非目标版本入口不能借官方客户端身份激活 0.145.0 条件分支。
				c.Request.Header.Set("x-openai-subagent", "review")
				c.Request.Header.Set("x-openai-memgen-request", "true")
				c.Request.Header.Set("x-codex-parent-thread-id", "forged-parent")
				state, err := resolveOfficialCodexRuntimeState(c, account, officialClientProfileModeActive, endpointID)
				require.NoError(t, err)
				require.Equal(t, defaultState, state)
			})
		}
	}
}

func TestOfficialCodex0145RuntimeStateModelsBootstrapIsProfilePhase(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	for _, surfaceID := range []string{officialCodexSurfaceExec, officialCodexSurfaceTUI} {
		userAgent, renderErr := profile.RenderUserAgent(surfaceID, false)
		require.NoError(t, renderErr)
		ingress := officialCodex0145RuntimeIngress(userAgent, "codex_cli_rs")
		state, resolveErr := resolveOfficialCodexRuntimeState(
			ingress,
			officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

			codexEndpointID(officialCodexEndpointModels))

		require.NoError(t, resolveErr)
		require.Equal(t, officialCodexProcessPhaseInitialized, state.ProcessPhase)
		require.Equal(t, officialCodexSurfaceExec, state.SurfaceID)
		require.Equal(t, "codex_exec", state.Originator)
		require.True(t, state.UserAgentSuffixEnabled)

		// codex_cli_rs 只在 models 首跳成立；用在其他端点时不再拒绝，改按画像
		// originator 与 initialized 阶段出站。
		nonModels, resolveErr := resolveOfficialCodexRuntimeState(
			ingress,
			officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

			codexEndpointID(officialCodexEndpointResponsesHTTP))

		require.NoError(t, resolveErr)
		require.Equal(t, officialCodexProcessPhaseInitialized, nonModels.ProcessPhase)
		require.NotEqual(t, "codex_cli_rs", nonModels.Originator)
	}
}

func TestOfficialCodex0145RuntimeStateCrossChecksSubagentMetadata(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	parentID := "11111111-1111-4111-8111-111111111111"

	guardian := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	guardian.Request.Header.Set("x-openai-subagent", "Other(custom-review)")
	guardian.Request.Header.Set("x-codex-parent-thread-id", parentID)
	guardian.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"Other(custom-review)","parent_thread_id":"`+parentID+`"}`)
	state, err := resolveOfficialCodexRuntimeState(guardian, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "Other(custom-review)", state.ConditionalHeaders["x-openai-subagent"])
	require.Equal(t, parentID, state.ConditionalHeaders["x-codex-parent-thread-id"])

	// 条件头与 turn metadata 冲突时按“条件不成立”处理：不发条件头，请求照常出站。
	mismatch := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	mismatch.Request.Header.Set("x-openai-subagent", "guardian")
	mismatch.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"review"}`)
	mismatchState, err := resolveOfficialCodexRuntimeState(mismatch, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.NotContains(t, mismatchState.ConditionalHeaders, "x-openai-subagent")
	require.NotContains(t, mismatchState.ConditionalHeaders, "x-codex-parent-thread-id")

	memgen := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	memgen.Request.Header.Set("x-openai-subagent", "memory_consolidation")
	memgen.Request.Header.Set("x-openai-memgen-request", "true")
	memgen.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"memory_consolidation"}`)
	state, err = resolveOfficialCodexRuntimeState(memgen, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "true", state.ConditionalHeaders["x-openai-memgen-request"])

	threadSpawn := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	threadSpawn.Request.Header.Set("x-openai-subagent", "collab_spawn")
	threadSpawn.Request.Header.Set("x-codex-parent-thread-id", parentID)
	threadSpawn.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"thread_spawn","parent_thread_id":"`+parentID+`"}`)
	state, err = resolveOfficialCodexRuntimeState(threadSpawn, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "collab_spawn", state.ConditionalHeaders["x-openai-subagent"])

	memorySubagent := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	memorySubagent.Request.Header.Set("x-openai-subagent", "memory_consolidation")
	memorySubagent.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"subagent","subagent_kind":"memory_consolidation"}`)
	state, err = resolveOfficialCodexRuntimeState(memorySubagent, officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Empty(t, state.ConditionalHeaders["x-openai-memgen-request"])
}

func TestOfficialCodex0145HTTPAttachUsesRuntimeFrozenBeforeTokenRefresh(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	tuiUserAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI,
		"xterm-256color",
		false,
	)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(tuiUserAgent, "codex-tui")
	account := officialEgressTestAccount(145, PlatformOpenAI)
	ctx, err := bindOfficialCodexRuntimeStateFromIngress(
		context.Background(),
		c,
		account, officialClientProfileModeActive,

		codexEndpointID(officialCodexEndpointResponsesHTTP))

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
	require.Equal(t, officialCodexSurfaceExec, egressContext.codexRuntimeState.SurfaceID)
	require.Equal(t, "codex_exec", egressContext.codexRuntimeState.Originator)
	require.Equal(t, "unknown", egressContext.codexRuntimeState.TerminalToken)
	require.True(t, egressContext.codexRuntimeState.UserAgentSuffixEnabled)
}

func TestOfficialCodex0145HTTPCompressionFeatureSurvivesIngressBodyDecode(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	body := []byte(`{"model":"gpt-5.5","input":[]}`)
	require.NoError(t, compressOfficialOpenAIHTTPRequest(c.Request, body, 3))
	c.Request = c.Request.WithContext(
		WithOfficialCodexIngressRuntime(c.Request.Context(), c),
	)

	// 真实调用通用 body reader，证明冻结发生在它移除线上的编码头之前。
	decoded, err := pkghttputil.ReadRequestBodyWithPrealloc(c.Request)
	require.NoError(t, err)
	require.JSONEq(t, string(body), string(decoded))
	require.Empty(t, c.Request.Header.Get("Content-Encoding"))
	ctx, err := bindOfficialCodexRuntimeStateFromIngress(
		c.Request.Context(),
		c,
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		codexEndpointID(officialCodexEndpointResponsesHTTP))

	require.NoError(t, err)
	state, found, err := officialCodexRuntimeStateFromContext(ctx)
	require.NoError(t, err)
	require.True(t, found)
	require.True(t, state.RequestCompressionEnabled)
}

func TestOfficialCodex0145HTTPCompressionIgnoresLateHeaderMutationAfterSnapshot(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	c.Request = c.Request.WithContext(
		WithOfficialCodexIngressRuntime(c.Request.Context(), c),
	)

	// 冻结后再伪造 zstd 不能改变该调用的 feature，证明 Finalizer 不回读当前 header。
	c.Request.Header.Set("Content-Encoding", "zstd")
	ctx, err := bindOfficialCodexRuntimeStateFromIngress(
		c.Request.Context(),
		c,
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		codexEndpointID(officialCodexEndpointResponsesHTTP))

	require.NoError(t, err)
	state, found, err := officialCodexRuntimeStateFromContext(ctx)
	require.NoError(t, err)
	require.True(t, found)
	require.False(t, state.RequestCompressionEnabled)
}

func TestOfficialCodexCompressionRequiresResponsesLiteModelCapability(t *testing.T) {
	for _, testCase := range []struct {
		name          string
		responsesLite bool
		wantEnabled   bool
	}{
		{name: "非 Lite 请求不压缩", responsesLite: false, wantEnabled: false},
		{name: "Lite 请求允许压缩", responsesLite: true, wantEnabled: true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			req := httptest.NewRequest(
				http.MethodPost,
				"https://chatgpt.com/backend-api/codex/responses",
				strings.NewReader(`{"model":"gpt-5.6-luna","input":[]}`),
			)
			runtimeState := defaultOfficialCodexRuntimeState()
			runtimeState.RequestCompressionEnabled = false
			egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
				AccountID:         145,
				TargetPlatform:    PlatformOpenAI,
				ResponsesLite:     testCase.responsesLite,
				ProfileVersion:    officialCodexVersion0145,
				ProfileMode:       officialClientProfileModeActive,
				Transport:         OfficialEgressTransportHTTP,
				UpstreamHost:      "chatgpt.com",
				CodexRuntimeState: runtimeState,
			})
			req = req.WithContext(WithOfficialEgressContext(req.Context(), egressContext))

			attempt, err := prepareOfficialCodexSemanticAttempt(
				req,
				[]byte(`{"model":"gpt-5.6-luna","input":[]}`),
				string(officialCodexEndpointResponsesHTTP),
				"compression-capability-test",
				projectOfficialCodexIdentityAccount(officialEgressTestAccount(145, PlatformOpenAI)),
			)
			require.NoError(t, err)
			require.Equal(t, testCase.wantEnabled, attempt.IdentityFacts.Conditions.CompressionEligible)
		})
	}
}

func TestWithOfficialCodex0145IngressRuntimeKeepsFirstWireSnapshot(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	c := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	c.Request.Header.Set("Content-Encoding", "zstd")

	first := WithOfficialCodexIngressRuntime(c.Request.Context(), c)
	// 模拟 Composite 路由完成解压、后续兼容层又改写进程头；Handler 的重复捕获
	// 必须返回同一上下文，不能用标准化后的请求覆盖首次 wire 快照。
	c.Request.Header.Del("Content-Encoding")
	c.Request.Header.Set("User-Agent", "third-party/1.0")
	c.Request.Header.Del("originator")
	second := WithOfficialCodexIngressRuntime(first, c)

	require.True(t, first == second)
	snapshot, captured := officialCodexIngressRuntimeSnapshotFromContext(second)
	require.True(t, captured)
	require.True(t, snapshot.OfficialClient)
	require.Equal(t, userAgent, snapshot.UserAgent)
	require.Equal(t, "codex_exec", snapshot.Originator)
	require.True(t, snapshot.RequestCompressionEnabled)
}

func TestOfficialCodex0145RuntimeStateIsDeepCopiedAcrossContextFreeze(t *testing.T) {
	state := defaultOfficialCodexRuntimeState()
	state.ProfileMode = officialClientProfileModeActive
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
