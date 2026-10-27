package service

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 较新版本画像的合成夹具。
//
// 候选期的真实形态是“目标画像装在 previous 槽位、Active 仍是旧画像”。用例需要两张画像做对照：
// 声明了新特征的“目标”（合成画像，装在 previous 槽位，便于控制每一项特征与取值）和不声明它们的
// “旧画像对照”（读 active 槽位）。合成画像以正式目录里的旧画像（见 officialCodexLegacyBaseProfileMode）为底稿，
// 应用快照改写后重新编译，并同时替换 service 读取画像的两个包级入口（可选节入口与字段序入口），
// 使 previous 槽位读到合成画像、active 槽位读到旧画像。这样同一个用例里先后用 previous 与 active
// 构造请求，就能证明目标画像与旧画像互不影响。正式目录不被修改，用例结束自动恢复。
//
// 晋升后正式 Active 已是较新画像，旧画像退居正式 previous 槽位：此时合成画像仍装在 previous（遮住正式的
// previous），active 槽位同样被换成那张旧画像，用例看到的“目标在 previous、旧画像在 active”与晋升前逐项相同。
//
// 替换的是包级入口，使用本夹具的用例不得并行执行。

// withOfficialCodexSyntheticPreviousProfile 把 mutate 改写后的合成画像装在 previous 槽位。
func withOfficialCodexSyntheticPreviousProfile(
	t *testing.T,
	mutate func(*profilecontract.SnapshotDoc),
) profilecontract.ExecutableProfile {
	t.Helper()
	baseMode, hasLegacySlot := officialCodexLegacyBaseProfileMode()
	doc, legacy := officialCodexLegacyBase(t)
	if mutate != nil {
		mutate(&doc)
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	synthetic, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)

	// 旧画像不在 active 槽位（晋升后）时，active 槽位也要读旧画像，才能继续充当“旧画像对照”。
	activeIsLegacy := hasLegacySlot && baseMode == officialClientProfileModeActive
	previousOptional := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(mode string) (profilecontract.ExecutableProfile, error) {
		switch normalizeOfficialClientProfileMode(mode) {
		case officialClientProfileModePrevious:
			return synthetic, nil
		case officialClientProfileModeActive:
			if !activeIsLegacy {
				return legacy, nil
			}
		}
		return previousOptional(mode)
	}
	previousBodyOrder := officialCodexReleaseProfileForBodyOrder
	officialCodexReleaseProfileForBodyOrder = func(mode string) (profilecontract.ExecutableProfile, error) {
		switch mode {
		case officialClientProfileModePrevious:
			return synthetic, nil
		case officialClientProfileModeActive:
			if !activeIsLegacy {
				return legacy, nil
			}
		}
		return previousBodyOrder(mode)
	}
	t.Cleanup(func() {
		officialCodexExecutableProfileForMode = previousOptional
		officialCodexReleaseProfileForBodyOrder = previousBodyOrder
	})
	return synthetic
}

// syntheticNewerProfileMutation 是较新版本合成画像的完整改写：顶层字段重排与删增、基础指令载体节、
// access_programs 值域节、turn metadata 的 history_initialization 键。后续阶段按需在这里追加其他节与键。
func syntheticNewerProfileMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	bodyContract := syntheticNewerBodyContractMutation(t)
	return func(doc *profilecontract.SnapshotDoc) {
		bodyContract(doc)
		syntheticDeclareTurnMetadataKey(t, doc, "history_initialization")
	}
}

// syntheticDeclareTurnMetadataKey 在合成画像的 TurnMetadata 节里声明一个键（保持排序去重）。
func syntheticDeclareTurnMetadataKey(t *testing.T, doc *profilecontract.SnapshotDoc, key string) {
	t.Helper()
	var section profilecontract.TurnMetadataSection
	require.NoError(t, json.Unmarshal(doc.TurnMetadata, &section))
	if !slices.Contains(section.Keys, key) {
		section.Keys = append(section.Keys, key)
		slices.Sort(section.Keys)
	}
	doc.TurnMetadata = syntheticServiceRawSection(t, section)
}
