package service

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 较新版本画像的合成夹具。
//
// 候选期的真实形态是“目标画像装在 previous 槽位、Active 仍是旧画像”。真实目标画像要到候选目录生成
// 之后才存在，所以用合成画像驱动 service 层：以正式目录 Active 画像为底稿，应用快照改写后重新编译，
// 并同时替换 service 读取画像的两个包级入口（可选节入口与字段序入口），使 previous 槽位读到合成画像、
// active 槽位仍读正式 Active。这样同一个用例里先后用 previous 与 active 构造请求，就能证明候选画像
// 与生产画像互不影响。正式目录不被修改，用例结束自动恢复。
//
// 替换的是包级入口，使用本夹具的用例不得并行执行。

// withOfficialCodexSyntheticPreviousProfile 把 mutate 改写后的合成画像装在 previous 槽位。
func withOfficialCodexSyntheticPreviousProfile(
	t *testing.T,
	mutate func(*profilecontract.SnapshotDoc),
) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	if mutate != nil {
		mutate(&doc)
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	synthetic, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)

	previousOptional := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(mode string) (profilecontract.ExecutableProfile, error) {
		if normalizeOfficialClientProfileMode(mode) == officialClientProfileModePrevious {
			return synthetic, nil
		}
		return previousOptional(mode)
	}
	previousBodyOrder := officialCodexReleaseProfileForBodyOrder
	officialCodexReleaseProfileForBodyOrder = func(mode string) (profilecontract.ExecutableProfile, error) {
		if mode == officialClientProfileModePrevious {
			return synthetic, nil
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
