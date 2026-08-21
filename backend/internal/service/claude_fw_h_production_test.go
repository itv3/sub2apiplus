package service

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func newClaudeFWHServiceRuntime(
	t *testing.T,
) (*OfficialEgressTransitionRuntime, *config.Config) {
	t.Helper()
	catalog, err := officialegress.ClaudeProductionSinkCatalog(
		officialegress.DefaultSinkCatalog(),
	)
	require.NoError(t, err)
	routes, err := officialegress.NewOfficialRouteCatalog(catalog)
	require.NoError(t, err)
	guard, err := officialegress.NewGuard(officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyEnforce),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyEnforce),
		CanaryPercent:          100,
	}, catalog, routes, nil)
	require.NoError(t, err)
	cfg := &config.Config{}
	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "active"
	runtimeState, err := BuildOfficialEgressTransitionRuntime(
		guard, &claudeFWGServiceUpstream{}, cfg, nil,
	)
	require.NoError(t, err)
	return runtimeState, cfg
}

func TestClaudeFWHProductionSelectorBuildsOnlyProductionRuntime(t *testing.T) {
	runtimeState, cfg := newClaudeFWHServiceRuntime(t)
	require.NotNil(t, runtimeState.Claude)
	require.Nil(t, runtimeState.ClaudeCandidate)
	require.True(t, runtimeState.Claude.IsProductionActive())
	require.Equal(
		t, officialegress.ClaudeFWHProductionApprovalDigest,
		runtimeState.Claude.ProductionApprovalDigest(),
	)

	gin.SetMode(gin.TestMode)
	svc := &GatewayService{cfg: cfg, officialEgress: runtimeState}
	account := claudeFWGServiceAccount()
	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens", "/messages/count_tokens"} {
		recorder := httptest.NewRecorder()
		ctx, _ := gin.CreateTestContext(recorder)
		ctx.Request = httptest.NewRequest(http.MethodPost, path, nil)
		if path == "/v1/messages" {
			require.True(t, svc.shouldRouteClaudeStrictMessages(ctx, account))
		} else {
			require.True(t, svc.shouldRouteClaudeStrictCountTokens(ctx, account))
		}
	}
	for _, path := range []string{"/v1/chat/completions", "/v1/responses"} {
		recorder := httptest.NewRecorder()
		ctx, _ := gin.CreateTestContext(recorder)
		ctx.Request = httptest.NewRequest(http.MethodPost, path, nil)
		require.False(t, svc.shouldRouteClaudeStrictMessages(ctx, account))
		require.False(t, svc.shouldRouteClaudeStrictCountTokens(ctx, account))
	}
}

func TestClaudeFWHActivationFactIsIndependentFromCodex(t *testing.T) {
	runtimeState, cfg := newClaudeFWHServiceRuntime(t)
	previous := processOfficialEgressRuntime.Load()
	processOfficialEgressRuntime.Store(runtimeState)
	t.Cleanup(func() { processOfficialEgressRuntime.Store(previous) })

	fact, err := resolveClaudeOfficialEgressActivationFact(
		cfg,
		time.Unix(1787320443, 0),
		activationEnv(map[string]string{
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST":  officialegress.ClaudeFWGProfileDigest,
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST":  officialegress.ClaudeFWGReleaseDigest,
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_APPROVAL_DIGEST": officialegress.ClaudeFWHProductionApprovalDigest,
		}),
	)
	require.NoError(t, err)
	require.Equal(t, "claude-egress-activation-fact/v1", fact.SchemaVersion)
	require.Equal(t, "active", fact.ProfileMode)
	require.Equal(t, officialegress.ClaudeFWGReleaseDigest, fact.ReleaseDigest)
	require.Equal(t, officialegress.ClaudeFWHProductionApprovalDigest, fact.ApprovalDigest)
	require.Len(t, fact.EventID, 64)

	codexFact, err := resolveOfficialEgressActivationFact(
		activationConfig("active"), time.Unix(1787320443, 0), activationEnv(nil),
	)
	require.NoError(t, err)
	require.Equal(t, "codex-egress-activation-fact/v1", codexFact.SchemaVersion)
	require.NotEqual(t, fact.EventID, codexFact.EventID)
}

func TestClaudeFWHActivationFactRejectsDeclaredDigestDrift(t *testing.T) {
	runtimeState, cfg := newClaudeFWHServiceRuntime(t)
	previous := processOfficialEgressRuntime.Load()
	processOfficialEgressRuntime.Store(runtimeState)
	t.Cleanup(func() { processOfficialEgressRuntime.Store(previous) })
	_, err := resolveClaudeOfficialEgressActivationFact(
		cfg, time.Unix(1787320443, 0), activationEnv(map[string]string{
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_APPROVAL_DIGEST": "0000000000000000000000000000000000000000000000000000000000000000",
		}),
	)
	require.ErrorContains(t, err, "不一致")
}
