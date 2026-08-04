package service

import (
	"context"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodexHTTPInvocationSharesBundleAndMonotonicAttempts(t *testing.T) {
	upstream := &openAIForwardPlanUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(),
		upstream,
		officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	account := &Account{
		ID: 721, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 2,
		Credentials: map[string]any{"chatgpt_account_id": "acct-quota-test"},
	}
	invocation, err := newOfficialCodexHTTPInvocation(
		context.Background(),
		officialCodexHTTPInvocationInput{
			Runtime: runtimeState, Account: account,
			SinkID: officialEgressSinkQuotaWHAM, InvocationID: "quota-shared-invocation",
			PolicyID: "quota-shared", PolicySource: "test",
			BehaviorKind: officialegress.BehaviorQuotaQuery, AttemptBudget: 2,
		},
	)
	require.NoError(t, err)
	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())

	request := newOfficialCodexHTTPInvocationTestRequest(
		t,
		"https://chatgpt.com/backend-api/wham/usage",
	)
	response, err := invocation.Execute(context.Background(), officialCodexHTTPAttemptInput{
		EndpointID: officialCodexEndpointWhamUsage,
		Request:    request,
	})
	require.NoError(t, err)
	_ = response.Body.Close()

	request = newOfficialCodexHTTPInvocationTestRequest(
		t,
		"https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
	)
	response, err = invocation.Execute(context.Background(), officialCodexHTTPAttemptInput{
		EndpointID: officialCodexEndpointWhamResetCredits,
		Request:    request,
	})
	require.NoError(t, err)
	_ = response.Body.Close()

	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())
	requests := upstream.snapshot()
	require.Len(t, requests, 2)
	first, ok := officialegress.AttemptIdentityFromContext(requests[0].Context())
	require.True(t, ok)
	second, ok := officialegress.AttemptIdentityFromContext(requests[1].Context())
	require.True(t, ok)
	require.Equal(t, uint32(1), first.AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonInitial), first.AttemptReason)
	require.Equal(t, uint32(2), second.AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonIndependent), second.AttemptReason)
	require.Equal(t, first.BundleDigest, second.BundleDigest)
	require.Equal(t, "quota-shared-invocation", first.InvocationID)
	require.Equal(t, first.InvocationID, second.InvocationID)
}

func TestOfficialCodexStructuredIdentityRejectsConflictingTrustedSource(t *testing.T) {
	value, err := resolveOfficialCodexStructuredIdentity(
		"session_id", "structured-session", "registered-session",
	)
	require.ErrorContains(t, err, "与受信来源冲突")
	require.Empty(t, value)

	value, err = resolveOfficialCodexStructuredIdentity(
		"session_id", "structured-session", "structured-session",
	)
	require.NoError(t, err)
	require.Equal(t, "structured-session", value)
}

func newOfficialCodexHTTPInvocationTestRequest(t *testing.T, target string) *http.Request {
	t.Helper()
	request, err := http.NewRequest(http.MethodGet, target, nil)
	require.NoError(t, err)
	request.Header.Set("Authorization", "Bearer quota-test-token")
	request.Header.Set("Chatgpt-Account-Id", "acct-quota-test")
	request.Header.Set("Accept", "*/*")
	return request
}
