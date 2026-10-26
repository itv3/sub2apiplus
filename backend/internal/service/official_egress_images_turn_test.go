//go:build unit

package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 独立 images 端点的回合头事实（x-codex-image-turn-id、x-codex-turn-metadata）：
// 只在当前 release 画像为该端点声明了对应槽位时才生成事实；旧画像一个字节都不变。
// 最终出站头由 officialegress 包的合成画像编译用例覆盖。

// withImagesTurnHeaderSlots 以正式目录 active 画像为底稿，给 images 两个端点按需声明回合头槽位，
// 并临时替换 service 读取画像的包级入口。用例结束自动恢复；使用本夹具的用例不得并行执行。
func withImagesTurnHeaderSlots(t *testing.T, imageTurnID, turnMetadata bool) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		endpoint := syntheticServiceSnapshotEndpoint(t, &doc, endpointID)
		kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers)+2)
		maxSlot := 0
		for _, slot := range endpoint.Headers {
			if slot.Name == officialCodexImageTurnIDHeader || slot.Name == officialCodexTurnMetadataHeader {
				continue
			}
			maxSlot = max(maxSlot, slot.Slot)
			kept = append(kept, slot)
		}
		add := func(name string) {
			maxSlot++
			kept = append(kept, profilecontract.SnapshotHeaderSlot{
				Slot: maxSlot, Name: name, WireName: name,
				Source: string(profilecontract.SourceTurn), Condition: string(profilecontract.ConditionAlways),
			})
		}
		if imageTurnID {
			add(officialCodexImageTurnIDHeader)
		}
		if turnMetadata {
			add(officialCodexTurnMetadataHeader)
		}
		endpoint.Headers = kept
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	executable, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	previous := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(string) (profilecontract.ExecutableProfile, error) {
		return executable, nil
	}
	t.Cleanup(func() { officialCodexExecutableProfileForMode = previous })
	return executable
}

func completeImagesTurnFactsForTest(
	t *testing.T,
	endpointID string,
	profileMode string,
	prefill func(*officialegress.CodexIdentityFacts),
) officialegress.CodexIdentityFacts {
	t.Helper()
	facts := officialegress.CodexIdentityFacts{}
	if prefill != nil {
		prefill(&facts)
	}
	require.NoError(t, completeOfficialCodexGeneratedIdentityFacts(
		&facts, endpointID, "images-seed", officialCodexFallbackTurnMetadataInput{profileMode: profileMode},
	))
	return facts
}

func TestImagesTurnFactsLegacyProfileGeneratesNothing(t *testing.T) {
	// 正式目录里没有为 images 声明回合头槽位的画像（晋升 0.164 之前）：事实保持为空。
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	imageTurnID, turnMetadata, _ := officialCodexImagesHeaderDeclarations(
		officialClientProfileModeActive, officialCodexEndpointImagesGenerations,
	)
	if imageTurnID || turnMetadata {
		t.Skipf("active 画像（%s）已声明 images 回合头槽位，旧画像对照由下方合成画像用例覆盖", release.Version())
	}
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, nil)
		require.Empty(t, facts.TurnID.Value)
		require.Empty(t, facts.TurnMetadata.Value)
	}
}

func TestImagesTurnFactsSyntheticLegacyProfileGeneratesNothing(t *testing.T) {
	withImagesTurnHeaderSlots(t, false, false)
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, nil)
		require.Empty(t, facts.TurnID.Value, "画像未声明槽位时不得多生成身份事实")
		require.Empty(t, facts.TurnMetadata.Value)
		require.Empty(t, facts.SessionID.Value)
		require.Empty(t, facts.ThreadID.Value)
	}
}

func TestImagesTurnFactsFollowDeclaredSlots(t *testing.T) {
	profile := withImagesTurnHeaderSlots(t, true, true)
	turnID := officialCodexGeneratedUUIDLikeValue("images-seed", "turn")
	sessionID := officialCodexGeneratedUUIDLikeValue("images-seed", "session")
	threadID := officialCodexGeneratedUUIDLikeValue("images-seed", "thread")
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, nil)
		require.Equal(t, turnID, facts.TurnID.Value)
		require.Equal(t, officialegress.IdentitySourceInvocation, facts.TurnID.Source)
		require.Equal(t, officialegress.IdentityLifecycleTurn, facts.TurnID.Lifecycle)
		// 键序固定；turn_id 与 x-codex-image-turn-id 同源。
		require.Equal(t,
			`{"session_id":"`+sessionID+`","thread_id":"`+threadID+`","turn_id":"`+turnID+
				`","thread_source":"user","sandbox":"seccomp","codex_version":"`+profile.Version()+`"}`,
			facts.TurnMetadata.Value)
		require.NoError(t, facts.Validate())
		// 生成的会话标识只进入 turn metadata，不写进会话事实，避免影响 Conditions。
		require.Empty(t, facts.SessionID.Value)
		require.Empty(t, facts.ThreadID.Value)
	}
}

func TestImagesTurnFactsSlotsAreIndependent(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, false)
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, officialClientProfileModeActive, nil)
	require.NotEmpty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value, "未声明 x-codex-turn-metadata 槽位时不生成 turn metadata")

	withImagesTurnHeaderSlots(t, false, true)
	facts = completeImagesTurnFactsForTest(t, officialCodexEndpointImagesEdits, officialClientProfileModeActive, nil)
	require.NotEmpty(t, facts.TurnMetadata.Value)
	// turn metadata 里的 turn_id 总要有来源，所以即使只声明 metadata 槽位也会生成 TurnID 事实。
	require.Contains(t, facts.TurnMetadata.Value, `"turn_id":"`+facts.TurnID.Value+`"`)
}

func TestImagesTurnFactsKeepStructuredValues(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, true)
	preset := func(facts *officialegress.CodexIdentityFacts) {
		var err error
		facts.TurnID, err = officialegress.NewCodexIdentityValue(
			"given-turn", officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
		)
		require.NoError(t, err)
		facts.TurnMetadata, err = officialegress.NewCodexIdentityValue(
			`{"turn_id":"given-turn"}`, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
		)
		require.NoError(t, err)
	}
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, officialClientProfileModeActive, preset)
	require.Equal(t, "given-turn", facts.TurnID.Value)
	require.Equal(t, `{"turn_id":"given-turn"}`, facts.TurnMetadata.Value)
}

func TestImagesTurnFactsNeedFrozenProfileMode(t *testing.T) {
	// 调用没有冻结 release mode 时不读画像，保持旧逻辑，不默认退化为 active。
	withImagesTurnHeaderSlots(t, true, true)
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, "", nil)
	require.Empty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value)
}

func TestImagesTurnFactsDoNotTouchOtherEndpoints(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, true)
	// 非 images、非 Responses 端点（例如 models）即使画像里别处声明了槽位也不受影响。
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointModels, officialClientProfileModeActive, nil)
	require.Empty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value)
}
