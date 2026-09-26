package service

import (
	"errors"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// WS 可重试错误与降级预算（VC-4 第 9 项）：
//   - 流内容量降载与预热错误事件保留上游 error.code，错误文本与改动前完全相同；
//     握手被拒时从拒绝体解析 error.code；
//   - 画像声明 WebSocketRetry 节时，节中的错误码计入预算、耗尽后按节降级 HTTP；
//     未声明的码（server_is_overloaded）与旧画像都交还旧逻辑。

func webSocketRetryTargetMutation(t *testing.T, budget int) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.WebSocketRetry = syntheticServiceRawSection(t, profilecontract.WebSocketRetrySection{
			RetryableErrorCodes: []string{"slow_down"}, RetryBudget: budget, FallbackTransport: "http",
		})
	}
}

func TestOpenAIWSUpstreamErrorCodeKeepsErrorTextAndCode(t *testing.T) {
	legacy := wrapOpenAIWSFallback("upstream_capacity_shed", errors.New("OpenAI servers are temporarily overloaded"))
	coded := wrapOpenAIWSFallback("upstream_capacity_shed", withOpenAIWSUpstreamErrorCode(
		"Slow_Down", errors.New("OpenAI servers are temporarily overloaded"),
	))
	require.Equal(t, legacy.Error(), coded.Error(), "附上错误码不得改变错误文本")
	require.Equal(t, "slow_down", openAIWSUpstreamErrorCode(coded))
	reason, retryable := classifyOpenAIWSReconnectReason(coded)
	require.Equal(t, "upstream_capacity_shed", reason)
	require.True(t, retryable, "分类结果不受错误码包装影响")

	require.Empty(t, openAIWSUpstreamErrorCode(legacy))
	require.Equal(t, errors.New("x").Error(), withOpenAIWSUpstreamErrorCode("", errors.New("x")).Error())

	handshake := wrapOpenAIWSFallback("upstream_5xx", &openAIWSDialError{
		StatusCode:   http.StatusServiceUnavailable,
		ResponseBody: []byte(`{"error":{"code":"slow_down","message":"Please slow down"}}`),
		Err:          errors.New("dial rejected"),
	})
	require.Equal(t, "slow_down", openAIWSUpstreamErrorCode(handshake), "握手拒绝体中的 error.code 同样可识别")
}

func TestOfficialCodexWebSocketRetryPolicyFollowsProfile(t *testing.T) {
	oauth := &Account{ID: 301, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	apiKey := &Account{ID: 302, Platform: PlatformOpenAI, Type: AccountTypeAPIKey}
	slowDown := wrapOpenAIWSFallback("upstream_capacity_shed", withOpenAIWSUpstreamErrorCode("slow_down", errors.New("slow down")))
	overloaded := wrapOpenAIWSFallback("upstream_capacity_shed", withOpenAIWSUpstreamErrorCode("server_is_overloaded", errors.New("overloaded")))

	require.Nil(t, newOfficialCodexWebSocketRetryPolicy(oauth, officialClientProfileModeActive), "旧画像没有 WebSocketRetry 节")
	var nilPolicy *officialCodexWebSocketRetryPolicy
	handled, _, _ := nilPolicy.observe(slowDown)
	require.False(t, handled)
	require.Equal(t, openAIWSReconnectRetryLimit+1, nilPolicy.maxAttempts(openAIWSReconnectRetryLimit+1))

	withOfficialCodexSyntheticProfile(t, webSocketRetryTargetMutation(t, 2))
	require.Nil(t, newOfficialCodexWebSocketRetryPolicy(apiKey, officialClientProfileModeActive), "非官方 OAuth 出口不适用")
	require.Nil(t, newOfficialCodexWebSocketRetryPolicy(oauth, ""), "未冻结 release mode 时不读画像")
	policy := newOfficialCodexWebSocketRetryPolicy(oauth, officialClientProfileModeActive)
	require.NotNil(t, policy)
	require.Equal(t, openAIWSReconnectRetryLimit+1, policy.maxAttempts(openAIWSReconnectRetryLimit+1))

	handled, _, _ = policy.observe(overloaded)
	require.False(t, handled, "server_is_overloaded 不在节中，交还旧逻辑")
	var retry, fallback bool
	for index := 0; index < 2; index++ {
		handled, retry, fallback = policy.observe(slowDown)
		require.True(t, handled)
		require.True(t, retry, "预算内继续重试")
		require.False(t, fallback)
	}
	handled, retry, fallback = policy.observe(slowDown)
	require.True(t, handled)
	require.False(t, retry, "预算耗尽")
	require.True(t, fallback, "按节降级 HTTP")
}

func TestOfficialCodexWebSocketRetryPolicyWidensAttemptsForLargerBudget(t *testing.T) {
	withOfficialCodexSyntheticProfile(t, webSocketRetryTargetMutation(t, openAIWSReconnectRetryLimit+4))
	policy := newOfficialCodexWebSocketRetryPolicy(
		&Account{ID: 303, Platform: PlatformOpenAI, Type: AccountTypeOAuth}, officialClientProfileModeActive,
	)
	require.Equal(t, openAIWSReconnectRetryLimit+5, policy.maxAttempts(openAIWSReconnectRetryLimit+1))
}
