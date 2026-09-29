package service

import (
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// ============================================================================
// legacy compact 端点删除（SPEC-EP-007 / SPEC-EP-014 / SPEC-EP-020）的晋升后门禁。
//
// 三条规则在目标画像中的批准语义相同：legacy compact 端点（POST
// /backend-api/codex/responses/compact）随官方客户端删除，目标制品不得再声明它；
// 显式的 legacy compact 请求必须在端点解析处失败关闭，不得回退到普通 Responses
// 或其他任何端点。三个门禁各自侧重被删除规则原本约束的维度：
//   - SPEC-EP-007：URL 与方法（Bundle 的 route/target 解析）；
//   - SPEC-EP-014：header 集合（header 契约与 H1 线序规则）；
//   - SPEC-EP-020：body（顶层字段闭集与 body 投影）。
// 每个门禁都另外在“另一槽位”（旧画像）上做对照：同一请求在旧画像下确实会出站，
// 证明目标槽位的拒绝来自画像删除，而不是测试构造不合法。
// ============================================================================

// codexGateLegacyCompactFeature 是 delete 规则的目标制品判别事实。
const codexGateLegacyCompactFeature = "画像不含 legacy compact 端点（responses_compact）"

// codexGateCompactResponse 为旧画像对照请求提供合法的 compact 应答。
func codexGateCompactResponse(request codexGateWireRequest) codexGateWireResponse {
	if request.path == codexGateLegacyCompactPath {
		return codexGateJSONResponse(
			`{"id":"resp_gate_compact","object":"response.compaction","output":[],` +
				`"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}`,
		)
	}
	return codexGateDefaultResponse(request)
}

// codexGateRequireLegacyCompactForwardFailsClosed 经生产 Forward 发出显式 legacy compact
// 请求：目标槽位必须失败且本地终端收不到任何请求（没有回退到 /responses）；另一槽位
// 同一请求必须真实到达 compact 路径。
func codexGateRequireLegacyCompactForwardFailsClosed(t *testing.T, mode string) {
	t.Helper()
	targetServer := startCodexGateWireServer(t, codexGateCompactResponse)
	body := newOfficialOpenAIHTTPTestBody(t, false, true, false)
	_, _, err := codexGateForwardResponses(
		t, mode, targetServer,
		newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact"), body, nil,
	)
	require.Error(t, err, "目标槽位下显式 legacy compact 请求必须失败关闭")
	require.Empty(t, targetServer.wireRequests(),
		"目标槽位下显式 legacy compact 请求不得产生任何出站（不得回退到 /responses 或其他端点）")
	require.Empty(t, targetServer.clientHellos(), "失败关闭发生在建立上游连接之前")

	other := codexGateOtherReleaseMode(mode)
	controlServer := startCodexGateWireServer(t, codexGateCompactResponse)
	_, _, _ = codexGateForwardResponses(
		t, other, controlServer,
		newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact"), body, nil,
	)
	compact := controlServer.requestsForPath(codexGateLegacyCompactPath)
	require.Len(t, compact, 1,
		"对照：%s 槽位（旧画像）同一 legacy compact 请求应真实出站到 compact 路径，否则目标槽位的拒绝不具判别力", other)
	require.Equal(t, http.MethodPost, compact[0].method)
}

// codexGateResolveResponsesBundle 以生产 Forward 同一组冻结策略解析 Responses 主链的
// Bundle（Sink codex.responses.forward）。
func codexGateResolveResponsesBundle(
	t *testing.T,
	mode string,
	sinkID officialegress.SinkID,
) (officialegress.ReleaseBundle, error) {
	t.Helper()
	resolver, err := officialegress.NewBundleResolver(
		officialegress.DefaultReleaseCatalog(), officialegress.DefaultSinkCatalog(),
	)
	require.NoError(t, err)
	return resolver.Resolve(officialegress.BundleResolveRequest{
		SinkID: sinkID, Mode: officialegress.ReleaseMode(mode),
		Execution: officialegress.ExecutionPolicy{
			ID: "promotion-gate.compact.execution", Source: "promotion-gate",
			MaxAttempts: 1, Replayable: true, ConcurrencyLimit: 1,
		},
		Deployment: officialegress.DeploymentSupportPolicy{
			ID: "promotion-gate.compact.deployment", Source: "promotion-gate",
			Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: "direct",
			SupportedBackends: []officialegress.BackendKind{officialegress.BackendHTTPUpstream},
		},
		Behavior: officialegress.BehaviorPolicy{
			ID: "promotion-gate.compact.behavior", Source: "promotion-gate",
			Kind: officialegress.BehaviorUserRequest, AttemptBudget: 1,
		},
	})
}

// SPEC-EP-007（delete）：legacy compact 的 URL 与方法。
//
// 重放的批准语义：目标制品不含 legacy compact 端点；显式 POST /responses/compact
// 在端点解析处失败关闭、不回退。对应的网关真实路径：
//   - service 端点解析 resolveCodexEndpointForMode / buildCodexEndpointURLForMode；
//   - 正式 BundleResolver 解析出的 Responses 主链 Bundle：ResolveEndpointPlan 与
//     ValidatedRequestTargetAuthority 都拒绝 compact target，且不会被 /responses
//     的 route 吞掉；只承载 compact 的 admin_test.compact sink 在目标槽位整体无法成包；
//   - 生产 Forward：入站 /v1/responses/compact 失败，上游零请求。
func TestCodexLegacyCompactRouteRemovalReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)

	_, err := resolveCodexEndpointForMode(mode, officialCodexEndpointResponsesCompact)
	require.Error(t, err, "目标画像不得再解析出 responses_compact 端点")
	_, err = buildCodexEndpointURLForMode(mode, officialCodexEndpointResponsesCompact, officialCodexEndpointURLInput{})
	require.Error(t, err, "目标画像不得再为 responses_compact 生成 URL")

	bundle, err := codexGateResolveResponsesBundle(t, mode, officialegress.SinkCodexResponsesForward)
	require.NoError(t, err, "目标槽位的 Responses 主链 Bundle 必须可解析")
	compactTarget, err := url.Parse(codexGateLegacyCompactHTTPS)
	require.NoError(t, err)
	_, err = bundle.ResolveEndpointPlan(
		officialegress.SinkCodexResponsesForward, http.MethodPost, compactTarget, officialegress.WireProtocolHTTP,
	)
	require.Error(t, err, "Bundle 不得为 POST /responses/compact 解析出任何 endpoint plan")
	_, err = bundle.ValidatedRequestTargetAuthority(http.MethodPost, compactTarget, officialegress.WireProtocolHTTP)
	require.Error(t, err, "compact target 不得通过 Bundle 的请求作用域校验")
	responsesTarget, err := url.Parse(codexGateResponsesHTTPS)
	require.NoError(t, err)
	responsesPlan, err := bundle.ResolveEndpointPlan(
		officialegress.SinkCodexResponsesForward, http.MethodPost, responsesTarget, officialegress.WireProtocolHTTP,
	)
	require.NoError(t, err)
	require.Equal(t, officialCodexEndpointResponsesHTTP, responsesPlan.EndpointID(),
		"/responses 仍只映射到 responses_http，compact 删除不影响普通 Responses")
	for _, plan := range bundle.EndpointPlans() {
		require.NotEqual(t, officialCodexEndpointResponsesCompact, plan.EndpointID(),
			"目标槽位的 Bundle 不得携带 compact endpoint plan")
	}
	_, err = codexGateResolveResponsesBundle(t, mode, officialegress.SinkCodexAdminTestCompact)
	require.Error(t, err, "只承载 compact 的 admin_test.compact sink 在目标槽位必须无法成包")

	codexGateRequireLegacyCompactForwardFailsClosed(t, mode)
}

// SPEC-EP-014（delete）：legacy compact 的 header 集合。
//
// 重放的批准语义：目标制品不再声明 compact 的 header 集合（x-codex-installation-id
// 头槽位、beta/turn-state 共用第三槽的 compact-third-slot 替代组都只属于 compact）；
// header 契约无法为 compact 生成任何头；传输画像的 strict H1 线序规则里没有 compact
// 路径，任何该路径的请求都不可能以 compact 头集合写上 wire；生产 Forward 失败且零出站。
func TestCodexLegacyCompactHeaderRemovalReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)
	profile := codexGateExecutableProfile(t, mode)
	for _, endpoint := range profile.Endpoints() {
		for _, slot := range endpoint.Headers {
			require.NotEqual(t, "compact-third-slot", slot.AlternateGroup,
				"%s 仍声明 compact 专属的第三槽替代组 %s", endpoint.ID, slot.Name)
			require.False(t, strings.EqualFold(slot.Name, "x-codex-installation-id"),
				"%s 仍声明 compact 专属的 x-codex-installation-id 头槽位", endpoint.ID)
		}
	}

	_, err := applyCodexHeaderContractForMode(
		mode, officialCodexEndpointResponsesCompact, http.Header{}, map[string]bool{},
	)
	require.Error(t, err, "header 契约不得再为 responses_compact 生成头集合")

	tlsProfile, err := resolveCodexEndpointTLSProfileForMode(mode, officialCodexEndpointResponsesHTTP)
	require.NoError(t, err)
	require.True(t, tlsProfile.Transport.StrictH1Wire, "Responses 传输画像必须 strict 定型 H1 wire")
	sawResponses := false
	for _, rule := range tlsProfile.Transport.H1HeaderOrders {
		require.NotEqual(t, codexGateLegacyCompactPath, rule.Path,
			"传输画像不得保留 compact 路径的 H1 线序规则（strict 模式下该路径无法写上 wire）")
		if rule.Method == http.MethodPost && rule.Path == codexGateResponsesPath {
			sawResponses = true
			require.NotContains(t, rule.Order, "x-codex-installation-id",
				"普通 Responses 的线序不得吸收 compact 的头集合")
		}
	}
	require.True(t, sawResponses, "传输画像必须保留普通 Responses 的 H1 线序规则")

	codexGateRequireLegacyCompactForwardFailsClosed(t, mode)
}

// SPEC-EP-020（delete）：legacy compact 的 body。
//
// 重放的批准语义：目标制品不再声明 compact 的 body 契约；service 的顶层字段闭集与
// body 投影都无法为 compact 定型请求体；生产 Forward 失败且零出站。
func TestCodexLegacyCompactBodyRemovalReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)
	profile := codexGateExecutableProfile(t, mode)
	responses := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesHTTP)
	require.Equal(t, profilecontract.BodyJson, responses.Body.Encoding)
	require.True(t, responses.Body.Closed, "普通 Responses body 契约保持闭集")

	_, err := officialOpenAITopLevelAllowSetForMode(mode, officialCodexEndpointResponsesCompact)
	require.Error(t, err, "目标画像不得再提供 compact 的顶层字段闭集")
	_, err = projectCodexEndpointJSONBodyForMode(
		mode, officialCodexEndpointResponsesCompact,
		map[string]any{"model": "gpt-5.6-luna", "input": []any{}, "parallel_tool_calls": false},
		[]byte(`{"model":"gpt-5.6-luna","input":[],"parallel_tool_calls":false}`),
		map[string]bool{},
	)
	require.Error(t, err, "body 投影不得再为 responses_compact 定型请求体")

	codexGateRequireLegacyCompactForwardFailsClosed(t, mode)
}
