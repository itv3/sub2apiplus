package service

import (
	"net/http"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// WS 续接失效条件（VC-4 第 10 项）：画像 WebSocketContinuation 节声明 auth_revision 时，
// 官方出站 WS 连接池键计入握手 Bearer 凭据摘要，token 刷新后不复用旧连接；Agent Identity
// 断言不计入；画像没有该节时连接池键与原组合逐字节相同。

func webSocketContinuationTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.WebSocketContinuation = syntheticServiceRawSection(t, profilecontract.WebSocketContinuationSection{
			ResetOn: []string{"account_owner", "account_routing_override", "auth_revision", "base_url", "late_tool_result_metadata"},
		})
	}
}

func newContinuationTestEgressContext(t *testing.T) *OfficialEgressContext {
	t.Helper()
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID: 401, TargetPlatform: PlatformOpenAI, ProfileMode: officialClientProfileModeActive,
		ProfileVersion: officialCodexVersion0145, Transport: OfficialEgressTransportWebSocket,
		UpstreamHost: "chatgpt.com",
	})
	egressContext.connectionPoolID = "pool-continuation-test"
	require.NoError(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID, testOfficialOpenAISessionID,
		OfficialEgressFieldSourceDerived, OfficialEgressFieldLifecycleSession,
	))
	return egressContext
}

func bearerHeaders(token string) http.Header {
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+token)
	return headers
}

// TestOfficialEgressWebSocketPoolTransportKeyFollowsContinuationSection 改动前直接把 Active 当作
// 旧画像；VC-6 晋升后 Active 是已声明 WebSocketContinuation 节（含 auth_revision）的目标画像。
// 现先在正式目录逐槽位核验“是否把认证代次计入连接池键”恰由该节决定，再以 Active 为底稿
// 去掉／追加该节做旧画像与目标画像对照（候选期去节是空操作，旧画像连接池键仍须逐字节相同）。
func TestOfficialEgressWebSocketPoolTransportKeyFollowsContinuationSection(t *testing.T) {
	for _, mode := range officialCodexFormalModes {
		section := officialCodexFormalExecutableProfile(t, mode).Optional().WebSocketContinuation
		require.Equal(t, section != nil && slices.Contains(section.ResetOn, "auth_revision"),
			officialCodexWebSocketContinuationResetsOn(mode, "auth_revision"),
			"%s 槽位是否按认证代次失效必须由 WebSocketContinuation 节决定", mode)
	}

	withOfficialCodexLegacySyntheticProfile(t, "WebSocketContinuation 节",
		func(profile profilecontract.ExecutableProfile) bool {
			return profile.Optional().WebSocketContinuation != nil
		},
		func(doc *profilecontract.SnapshotDoc) { doc.WebSocketContinuation = nil })
	egressContext := newContinuationTestEgressContext(t)
	legacy := egressContext.connectionPoolID +
		"|proxy_state=" + officialEgressProxyStateKey("") +
		"|identity=" + officialEgressWebSocketIdentityKey(egressContext)
	require.Equal(t, legacy, officialEgressWebSocketPoolTransportKey(egressContext, "", bearerHeaders("token-a")),
		"旧画像连接池键与原组合逐字节相同")
	require.False(t, officialCodexWebSocketContinuationResetsOn(officialClientProfileModeActive, "auth_revision"))

	withOfficialCodexSyntheticProfile(t, webSocketContinuationTargetMutation(t))
	require.True(t, officialCodexWebSocketContinuationResetsOn(officialClientProfileModeActive, "auth_revision"))
	require.False(t, officialCodexWebSocketContinuationResetsOn("", "auth_revision"), "未冻结 release mode 时不读画像")

	keyA := officialEgressWebSocketPoolTransportKey(egressContext, "", bearerHeaders("token-a"))
	keyARepeat := officialEgressWebSocketPoolTransportKey(egressContext, "", bearerHeaders("token-a"))
	keyB := officialEgressWebSocketPoolTransportKey(egressContext, "", bearerHeaders("token-b"))
	require.Equal(t, keyA, keyARepeat, "同一认证代次复用同一连接池键")
	require.NotEqual(t, keyA, keyB, "token 刷新后连接池键变化，不复用旧连接")
	require.Contains(t, keyA, legacy+"|auth_revision=")
	require.NotContains(t, keyA, "token-a", "连接池键只含凭据摘要")

	agentIdentity := http.Header{}
	agentIdentity.Set("Authorization", "agent-assertion-per-attempt")
	require.Equal(t, legacy, officialEgressWebSocketPoolTransportKey(egressContext, "", agentIdentity),
		"Agent Identity 断言不代表认证代次")
}
