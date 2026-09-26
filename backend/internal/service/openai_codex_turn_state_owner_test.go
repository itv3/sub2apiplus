package service

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// turn-state 按账号 owner 隔离（VC-4 第 8 项）：
//   - 只有画像声明 TurnState 节（ResetOnAccountOwnerChange）时才启用；
//   - 启用后 WS 入口丢弃已知由其他账号铸造的客户端回带 turn-state，同账号或无溯源记录
//     的值原样保留（与透传路径的出站守卫共用溯源表）。

func turnStateTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.TurnState = syntheticServiceRawSection(t, profilecontract.TurnStateSection{ResetOnAccountOwnerChange: true})
	}
}

// turnStateLegacyMutation 去掉画像的 TurnState 节，还原为未声明 owner 隔离的旧画像形态。
func turnStateLegacyMutation(doc *profilecontract.SnapshotDoc) {
	doc.TurnState = nil
}

// TestOfficialCodexTurnStateOwnerIsolationFollowsProfile 改动前直接把 Active 当作旧画像；VC-6 晋升后
// Active 是已声明 TurnState 节的目标画像。现分两部分保持原意：
//   - 正式目录逐槽位：是否启用 owner 隔离恰由该槽位画像的 TurnState 节决定（直接读正式目录
//     推导期望），晋升前后都覆盖两份真实画像；
//   - 旧画像对照组与目标画像：以 Active 为底稿去掉／追加该节合成（候选期去节是空操作）。
func TestOfficialCodexTurnStateOwnerIsolationFollowsProfile(t *testing.T) {
	for _, mode := range officialCodexFormalModes {
		section := officialCodexFormalExecutableProfile(t, mode).Optional().TurnState
		require.Equal(t, section != nil && section.ResetOnAccountOwnerChange,
			officialCodexTurnStateOwnerIsolation(mode), "%s 槽位的 owner 隔离必须由 TurnState 节决定", mode)
	}

	withOfficialCodexLegacySyntheticProfile(t, "TurnState 节",
		func(profile profilecontract.ExecutableProfile) bool { return profile.Optional().TurnState != nil },
		turnStateLegacyMutation)
	require.False(t, officialCodexTurnStateOwnerIsolation(officialClientProfileModeActive),
		"旧画像没有 TurnState 节，不改变 WS 入口 turn-state 行为")
	withOfficialCodexSyntheticProfile(t, turnStateTargetMutation(t))
	require.True(t, officialCodexTurnStateOwnerIsolation(officialClientProfileModeActive))
}

func TestIsolateOfficialCodexIngressTurnStateDropsOtherAccountValue(t *testing.T) {
	gin.SetMode(gin.TestMode)
	service := &OpenAIGatewayService{}
	newContext := func() *gin.Context {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodGet, "/openai/v1/responses", nil)
		c.Request.Header.Set("session-id", "owner-isolation-session")
		c.Set("api_key", &APIKey{ID: 7})
		return c
	}
	accountA := &Account{ID: 501, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	accountB := &Account{ID: 502, Platform: PlatformOpenAI, Type: AccountTypeOAuth}

	unknown := newContext()
	require.Equal(t, "state-a", service.isolateOfficialCodexIngressTurnState(unknown, accountB, "state-a"),
		"没有溯源记录时保持原样")

	c := newContext()
	service.noteOpenAICodexTurnStateProvenance(c, accountA)
	require.Equal(t, "state-a", service.isolateOfficialCodexIngressTurnState(c, accountA, "state-a"),
		"同账号铸造的 turn-state 保留")
	require.Empty(t, service.isolateOfficialCodexIngressTurnState(c, accountB, "state-a"),
		"账号 failover 后旧账号铸造的 turn-state 不再回送")
	require.Empty(t, service.isolateOfficialCodexIngressTurnState(c, accountB, "  "))
}
