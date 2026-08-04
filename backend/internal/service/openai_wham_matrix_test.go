package service

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func changeset4WHAMAccount(plan string) *Account {
	return &Account{
		ID:       8401,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-wham-matrix",
			"plan_type":          plan,
		},
	}
}

func changeset4CompleteWHAMUsage(t *testing.T, plan string) *OpenAIQuotaUsage {
	t.Helper()
	body, err := json.Marshal(map[string]any{
		"plan_type": plan,
		"rate_limit": map[string]any{
			"primary_window": map[string]any{
				"used_percent": 10, "limit_window_seconds": 18000, "reset_after_seconds": 100,
			},
			"secondary_window": map[string]any{
				"used_percent": 20, "limit_window_seconds": 604800, "reset_after_seconds": 200,
			},
		},
	})
	require.NoError(t, err)
	var usage OpenAIQuotaUsage
	require.NoError(t, json.Unmarshal(body, &usage))
	return &usage
}

func TestChangeset4WHAMPlanCatalogMatchesOfficial0145ClosedEnum(t *testing.T) {
	expected := []openAIWHAMPlanType{
		openAIWHAMPlanGuest,
		openAIWHAMPlanFree,
		openAIWHAMPlanGo,
		openAIWHAMPlanPlus,
		openAIWHAMPlanPro,
		openAIWHAMPlanProlite,
		openAIWHAMPlanFreeWorkspace,
		openAIWHAMPlanTeam,
		openAIWHAMPlanTeamUsageBased,
		openAIWHAMPlanBusiness,
		openAIWHAMPlanBusinessUsageBased,
		openAIWHAMPlanEducation,
		openAIWHAMPlanQuorum,
		openAIWHAMPlanK12,
		openAIWHAMPlanEnterprise,
		openAIWHAMPlanEdu,
		openAIWHAMPlanUnknown,
	}
	actual := make([]openAIWHAMPlanType, 0, len(openAIWHAMPlanCatalog))
	seen := make(map[openAIWHAMPlanType]struct{}, len(openAIWHAMPlanCatalog))
	for _, definition := range openAIWHAMPlanCatalog {
		actual = append(actual, definition.Plan)
		_, duplicate := seen[definition.Plan]
		require.False(t, duplicate, "计划闭集不得包含重复值: %s", definition.Plan)
		seen[definition.Plan] = struct{}{}
		require.Contains(t, []string{
			openAIWHAMPlanSemanticSupported,
			openAIWHAMPlanSemanticControlledFallback,
		}, definition.SemanticStatus)
	}
	require.Equal(t, expected, actual)
}

func TestChangeset4WHAMEveryKnownPlanPairHasClosedDecision(t *testing.T) {
	for _, credential := range openAIWHAMPlanCatalog {
		for _, response := range openAIWHAMPlanCatalog {
			name := string(credential.Plan) + "_to_" + string(response.Plan)
			t.Run(name, func(t *testing.T) {
				cell := evaluateOpenAIWHAMMatrix(
					changeset4WHAMAccount(string(credential.Plan)),
					changeset4CompleteWHAMUsage(t, string(response.Plan)),
					"",
				)
				expectedSupported := credential.SemanticStatus == openAIWHAMPlanSemanticSupported &&
					response.SemanticStatus == openAIWHAMPlanSemanticSupported &&
					credential.Family == response.Family
				if expectedSupported {
					require.Equal(t, openAIWHAMDecisionSupported, cell.FinalDecision)
					require.Empty(t, cell.FallbackReason)
					require.True(t, cell.ApprovedFamily)
					return
				}
				require.Equal(t, openAIWHAMDecisionFallback, cell.FinalDecision)
				require.True(t, isOpenAIWHAMFallbackReason(cell.FallbackReason),
					"fallback 必须使用闭集 reason code: %+v", cell)
			})
		}
	}
}

func TestChangeset4WHAMMatrixCapturesRequiredSemanticDimensions(t *testing.T) {
	t.Run("Team 与 usage-based Team 同族", func(t *testing.T) {
		cell := evaluateOpenAIWHAMMatrix(
			changeset4WHAMAccount("team"),
			changeset4CompleteWHAMUsage(t, "self_serve_business_usage_based"),
			"",
		)
		require.Equal(t, openAIWHAMDecisionSupported, cell.FinalDecision)
		require.Equal(t, "team", cell.PlanFamily)
	})

	t.Run("Business 与 usage-based Business 同族", func(t *testing.T) {
		cell := evaluateOpenAIWHAMMatrix(
			changeset4WHAMAccount("business"),
			changeset4CompleteWHAMUsage(t, "enterprise_cbp_usage_based"),
			"",
		)
		require.Equal(t, openAIWHAMDecisionSupported, cell.FinalDecision)
		require.Equal(t, "business", cell.PlanFamily)
	})

	testCases := []struct {
		name       string
		mutate     func(*Account, *OpenAIQuotaUsage)
		mapReason  string
		wantReason string
	}{
		{
			name: "Agent Identity 缺少端点语义证据",
			mutate: func(account *Account, _ *OpenAIQuotaUsage) {
				account.Credentials[openAIAuthModeCredentialKey] = OpenAIAuthModeAgentIdentity
			},
			wantReason: openAIWHAMFallbackAgentIdentity,
		},
		{
			name: "PAT 认证类型未批准",
			mutate: func(account *Account, _ *OpenAIQuotaUsage) {
				account.Credentials[openAIAuthModeCredentialKey] = OpenAIAuthModePersonalAccessToken
			},
			wantReason: openAIWHAMFallbackAuthenticationKind,
		},
		{
			name: "FedRAMP 缺少端点语义证据",
			mutate: func(account *Account, _ *OpenAIQuotaUsage) {
				account.Credentials["chatgpt_account_is_fedramp"] = true
			},
			wantReason: openAIWHAMFallbackFedRAMP,
		},
		{
			name: "账号 ID 缺失",
			mutate: func(account *Account, _ *OpenAIQuotaUsage) {
				delete(account.Credentials, "chatgpt_account_id")
			},
			wantReason: openAIWHAMFallbackAccountIDMissing,
		},
		{
			name:       "响应计划漂移",
			mutate:     func(_ *Account, usage *OpenAIQuotaUsage) { usage.PlanType = "plus" },
			wantReason: openAIWHAMFallbackResponsePlan,
		},
		{
			name:       "顶层窗口缺失",
			mutate:     func(_ *Account, usage *OpenAIQuotaUsage) { usage.RateLimit = nil },
			mapReason:  "wham_rate_limit_missing",
			wantReason: "wham_rate_limit_missing",
		},
		{
			name: "5h 窗口不完整",
			mutate: func(_ *Account, usage *OpenAIQuotaUsage) {
				usage.RateLimit.PrimaryWindow.usedPercentPresent = false
			},
			mapReason:  "wham_window_incomplete",
			wantReason: "wham_window_incomplete",
		},
		{
			name: "7d 窗口缺失",
			mutate: func(_ *Account, usage *OpenAIQuotaUsage) {
				usage.RateLimit.SecondaryWindow = nil
			},
			mapReason:  "wham_window_7d_missing",
			wantReason: "wham_window_7d_missing",
		},
		{
			name: "未知窗口时长",
			mutate: func(_ *Account, usage *OpenAIQuotaUsage) {
				usage.RateLimit.SecondaryWindow.LimitWindowSeconds = 2592000
			},
			mapReason:  "wham_window_unknown_duration",
			wantReason: "wham_window_unknown_duration",
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			account := changeset4WHAMAccount("pro")
			usage := changeset4CompleteWHAMUsage(t, "pro")
			testCase.mutate(account, usage)
			cell := evaluateOpenAIWHAMMatrix(account, usage, testCase.mapReason)
			require.Equal(t, openAIWHAMDecisionFallback, cell.FinalDecision)
			require.Equal(t, testCase.wantReason, cell.FallbackReason)
			require.True(t, isOpenAIWHAMFallbackReason(cell.FallbackReason))
		})
	}
}

func TestChangeset4WHAMMatrixSerializationKeepsAllAcceptanceFacts(t *testing.T) {
	cell := evaluateOpenAIWHAMMatrix(
		changeset4WHAMAccount("plus"),
		changeset4CompleteWHAMUsage(t, "plus"),
		"",
	)
	body, err := json.Marshal(cell)
	require.NoError(t, err)
	for _, field := range []string{
		"credential_plan", "response_plan", "approved_family", "authentication_kind",
		"agent_identity", "fedramp", "account_id_present", "top_level_rate_limit_present",
		"five_hour_window_complete", "seven_day_window_complete", "known_durations",
		"final_decision",
	} {
		require.Contains(t, string(body), `"`+field+`"`)
	}
}
