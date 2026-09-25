package service

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// guardian 审阅请求的入站标记（VC-4 第 1 项）：
//   - x-codex-guardian 只接受 reviewer，且必须与受信的 guardian 子代理身份同时成立；
//   - 其他取值或身份不一致按“条件不成立”出站，不报错、不带入 wire；
//   - 运行态校验拒绝非 reviewer 取值与孤立的审阅标记；
//   - 身份归一化把请求降级为非 guardian 时，审阅标记同步失效；
//   - 身份事实只在标记与 guardian 子代理同时存在时置位 GuardianReviewRequest。

const guardianRuntimeTestParentID = "11111111-1111-4111-8111-111111111111"

func guardianRuntimeTestIngress(t *testing.T, subagent string, guardian string) *gin.Context {
	t.Helper()
	gin.SetMode(gin.TestMode)
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	ingress := officialCodex0145RuntimeIngress(userAgent, "codex_exec")
	if subagent != "" {
		ingress.Request.Header.Set("x-openai-subagent", subagent)
		ingress.Request.Header.Set("x-codex-parent-thread-id", guardianRuntimeTestParentID)
		ingress.Request.Header.Set("x-codex-turn-metadata",
			`{"thread_source":"subagent","subagent_kind":"`+subagent+`","parent_thread_id":"`+guardianRuntimeTestParentID+`"}`)
	}
	if guardian != "" {
		ingress.Request.Header.Set("x-codex-guardian", guardian)
	}
	return ingress
}

func TestOfficialCodexRuntimeStateTrustsGuardianReviewerOnlyWithGuardianSubagent(t *testing.T) {
	account := officialEgressTestAccount(156, PlatformOpenAI)
	cases := []struct {
		name     string
		subagent string
		guardian string
		want     string
	}{
		{name: "guardian 子代理 + reviewer", subagent: "guardian", guardian: "reviewer", want: "reviewer"},
		{name: "异步评分器 classifier 不仿真", subagent: "guardian", guardian: "classifier"},
		{name: "任意取值不受信任", subagent: "guardian", guardian: "Reviewer"},
		{name: "缺少子代理身份", guardian: "reviewer"},
		{name: "其他子代理身份", subagent: "review", guardian: "reviewer"},
		{name: "无标记", subagent: "guardian"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ingress := guardianRuntimeTestIngress(t, tc.subagent, tc.guardian)
			state, err := resolveOfficialCodexRuntimeState(
				ingress, account, officialClientProfileModeActive, officialClientProfileModeActive,
			)
			require.NoError(t, err, "不受信任的审阅标记只能按条件不成立处理，不能拒绝服务")
			if tc.want == "" {
				require.NotContains(t, state.ConditionalHeaders, officialCodexGuardianHeader)
			} else {
				require.Equal(t, tc.want, state.ConditionalHeaders[officialCodexGuardianHeader])
				require.Equal(t, "guardian", state.ConditionalHeaders["x-openai-subagent"])
			}
			require.NoError(t, validateOfficialCodexRuntimeState(state))
		})
	}
}

func TestOfficialCodexRuntimeStateValidationRejectsUntrustedGuardianMarker(t *testing.T) {
	base := defaultOfficialCodexRuntimeState()
	base.ProfileMode = officialClientProfileModeActive

	classifier := cloneOfficialCodexRuntimeState(base)
	classifier.ConditionalHeaders["x-openai-subagent"] = "guardian"
	classifier.ConditionalHeaders[officialCodexGuardianHeader] = "classifier"
	require.ErrorContains(t, validateOfficialCodexRuntimeState(classifier), "只允许 reviewer")

	orphan := cloneOfficialCodexRuntimeState(base)
	orphan.ConditionalHeaders[officialCodexGuardianHeader] = "reviewer"
	require.ErrorContains(t, validateOfficialCodexRuntimeState(orphan), "guardian 子代理")

	otherSubagent := cloneOfficialCodexRuntimeState(base)
	otherSubagent.ConditionalHeaders["x-openai-subagent"] = "review"
	otherSubagent.ConditionalHeaders[officialCodexGuardianHeader] = "reviewer"
	require.ErrorContains(t, validateOfficialCodexRuntimeState(otherSubagent), "guardian 子代理")
}

func TestNormalizeOfficialCodexConditionalIdentityDropsGuardianMarkerWithIdentity(t *testing.T) {
	newContext := func() *OfficialEgressContext {
		state := defaultOfficialCodexRuntimeState()
		state.ProfileMode = officialClientProfileModeActive
		state.ConditionalHeaders["x-openai-subagent"] = "guardian"
		state.ConditionalHeaders["x-codex-parent-thread-id"] = guardianRuntimeTestParentID
		state.ConditionalHeaders[officialCodexGuardianHeader] = "reviewer"
		return NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID: 156, TargetPlatform: PlatformOpenAI,
			ProfileVersion: officialCodexVersion0145, ProfileMode: officialClientProfileModeActive,
			Transport: OfficialEgressTransportHTTP, UpstreamHost: "chatgpt.com",
			CodexRuntimeState: state,
		})
	}

	kept := newContext()
	normalizeOfficialCodexConditionalIdentity(kept, "guardian", guardianRuntimeTestParentID, false)
	require.Equal(t, "reviewer", kept.codexRuntimeState.ConditionalHeaders[officialCodexGuardianHeader])

	downgraded := newContext()
	normalizeOfficialCodexConditionalIdentity(downgraded, "", "", false)
	require.NotContains(t, downgraded.codexRuntimeState.ConditionalHeaders, officialCodexGuardianHeader)
	require.NoError(t, validateOfficialCodexRuntimeState(downgraded.codexRuntimeState))
}

func TestOfficialCodexIdentityFactsGuardianReviewCondition(t *testing.T) {
	for _, tc := range []struct {
		name     string
		marker   bool
		subagent string
		want     bool
	}{
		{name: "审阅标记 + guardian 子代理", marker: true, subagent: "guardian", want: true},
		{name: "只有 guardian 子代理", subagent: "guardian"},
		{name: "普通请求"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := defaultOfficialCodexRuntimeState()
			state.ProfileMode = officialClientProfileModeActive
			body := `{"model":"gpt-5.6-luna","input":[]}`
			if tc.subagent != "" {
				state.ConditionalHeaders["x-openai-subagent"] = tc.subagent
				state.ConditionalHeaders["x-codex-parent-thread-id"] = guardianRuntimeTestParentID
			}
			if tc.marker {
				state.ConditionalHeaders[officialCodexGuardianHeader] = "reviewer"
			}
			req := httptest.NewRequest(
				http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", strings.NewReader(body),
			)
			egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
				AccountID: 156, TargetPlatform: PlatformOpenAI,
				ProfileVersion: officialCodexVersion0145, ProfileMode: officialClientProfileModeActive,
				Transport: OfficialEgressTransportHTTP, UpstreamHost: "chatgpt.com",
				CodexRuntimeState: state,
			})
			req = req.WithContext(WithOfficialEgressContext(req.Context(), egressContext))
			attempt, err := prepareOfficialCodexSemanticAttempt(
				req, []byte(body), string(officialCodexEndpointResponsesHTTP),
				"guardian-condition-test", projectOfficialCodexIdentityAccount(officialEgressTestAccount(156, PlatformOpenAI)),
			)
			require.NoError(t, err)
			require.Equal(t, tc.want, attempt.IdentityFacts.Conditions.GuardianReviewRequest)
			require.False(t, attempt.IdentityFacts.Conditions.AccountRoutingOverridePresent,
				"工作区路由 override 首期失败关闭，身份事实永不置位")
		})
	}
}

func TestOfficialCodexConditionsFromHeadersGuardianComplement(t *testing.T) {
	headers := http.Header{}
	conditions := officialCodexConditionsFromHeaders(headers)
	require.False(t, conditions[officialCodexConditionGuardianReview])
	require.True(t, conditions[officialCodexConditionNotGuardianReview])
	require.False(t, conditions[officialCodexConditionRoutingOverride])

	headers.Set(officialCodexGuardianHeader, "reviewer")
	headers.Set("x-openai-account-routing-override", "us")
	conditions = officialCodexConditionsFromHeaders(headers)
	require.True(t, conditions[officialCodexConditionGuardianReview])
	require.False(t, conditions[officialCodexConditionNotGuardianReview])
	require.False(t, conditions[officialCodexConditionRoutingOverride], "override 首期恒不成立")
}
