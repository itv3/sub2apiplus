package service

import (
	"errors"
	"fmt"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 字段序入口可替换（officialCodexReleaseProfileForBodyOrder）：
//  1. 默认实现与抽取之前的原入口逐字等价——对 active／previous／空串／非法值及各端点，字段序与错误都相同；
//  2. 默认实现保持严格：空串与大小写不符的 mode 失败关闭，不做 normalizeOfficialClientProfileMode 的宽松化；
//  3. 替换后字段序入口、HTTP／WS 编码与顶层白名单都读到合成画像的 Body.Fields，用例结束入口自动恢复。
//
// 替换的是包级入口，使用 withOfficialCodexBodyOrderProfile 的用例不得并行执行。

// legacyOfficialCodexBodyFieldOrderForMode 是抽出可替换取画像入口之前的原实现（逐字保留）。
// 差分测试用它锁定新入口对任何 mode 与端点都得到相同的字段序和相同的错误。
func legacyOfficialCodexBodyFieldOrderForMode(mode, endpointID string) ([]string, error) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseMode(mode),
	)
	if err != nil {
		return nil, err
	}
	return officialCodexBodyFieldOrderFromProfile(release.ExecutableProfile(), endpointID)
}

// requireSameBodyOrderError 要求两个错误同时为空，或文本、类型与运行时错误码都相同。
func requireSameBodyOrderError(t *testing.T, want, got error, label string) {
	t.Helper()
	if want == nil {
		require.NoError(t, got, label)
		return
	}
	require.Error(t, got, label)
	require.Equal(t, want.Error(), got.Error(), label)
	require.Equal(t, fmt.Sprintf("%T", want), fmt.Sprintf("%T", got), label)
	wantCode, wantOK := officialegress.RuntimeErrorCodeOf(want)
	gotCode, gotOK := officialegress.RuntimeErrorCodeOf(got)
	require.Equal(t, wantOK, gotOK, label)
	require.Equal(t, wantCode, gotCode, label)
}

// 合成画像的目标字段序：stream、service_tier 前置，删去顶层 instructions，末位新增 access_programs。
var (
	syntheticNewerHTTPBodyOrder = []string{
		"model", "stream", "service_tier", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning",
		"store", "stream_options", "include", "prompt_cache_key", "text", "client_metadata", "access_programs",
	}
	syntheticNewerWSBodyOrder = []string{
		"type", "model", "stream", "service_tier", "previous_response_id", "input", "tools", "tool_choice",
		"parallel_tool_calls", "reasoning", "store", "stream_options", "include", "prompt_cache_key", "text",
		"generate", "client_metadata", "access_programs",
	}
)

// syntheticReorderBodyFields 按 order 重建端点的 Body.Fields：沿用旧字段的 Required、OmitWhen 与 Condition。
// order 里没有的旧字段只允许是 instructions（被删），旧画像没有的字段只允许是 access_programs（新增）。
func syntheticReorderBodyFields(t *testing.T, doc *profilecontract.SnapshotDoc, endpointID string, order []string) {
	t.Helper()
	endpoint := syntheticServiceSnapshotEndpoint(t, doc, endpointID)
	existing := make(map[string]profilecontract.SnapshotBodyField, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		existing[field.Name] = field
	}
	rebuilt := make([]profilecontract.SnapshotBodyField, 0, len(order))
	used := make(map[string]bool, len(order))
	for _, name := range order {
		field, found := existing[name]
		if !found {
			require.Equal(t, "access_programs", name, "端点 %s 的旧字段表里没有 %s", endpointID, name)
			field = profilecontract.SnapshotBodyField{Name: name, OmitWhen: "none"}
		}
		rebuilt = append(rebuilt, field)
		used[name] = true
	}
	for name := range existing {
		if !used[name] {
			require.Equal(t, "instructions", name, "端点 %s 的旧字段 %s 在新顺序里丢失", endpointID, name)
		}
	}
	endpoint.Body.Fields = rebuilt
}

// syntheticNewerBodyContractMutation 把两个 Responses 端点改成较新版本的顶层字段形态，并同时声明
// BaseInstructions 与 AccessPrograms 两节，使端点字段与画像节自洽（契约交叉校验才会通过）。
func syntheticNewerBodyContractMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		syntheticReorderBodyFields(t, doc, officialCodexEndpointResponsesHTTP, syntheticNewerHTTPBodyOrder)
		syntheticReorderBodyFields(t, doc, officialCodexEndpointResponsesWS, syntheticNewerWSBodyOrder)
		doc.BaseInstructions = syntheticServiceRawSection(t, profilecontract.BaseInstructionsSection{
			Carrier:         "input_developer_message",
			ContentItemKind: "model.base_instructions",
			EndpointIDs:     []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS},
			IDPrefix:        "msg",
		})
		doc.AccessPrograms = syntheticServiceRawSection(t, profilecontract.AccessProgramsSection{
			AllowedCyberPrograms: []string{"standard"},
			DefaultCyberProgram:  "standard",
		})
	}
}

// withOfficialCodexBodyOrderProfile 以正式目录里的旧画像（见 officialCodexLegacyBaseProfileMode）为底稿，应用快照
// 改写后重新编译，并临时替换 officialCodexReleaseProfileForBodyOrder，使字段序入口（以及经它的 HTTP／WS 编码、
// 顶层白名单与 WS HTTP 桥）读到合成画像的 Body.Fields。正式目录不被修改，用例结束自动恢复。
//
// 它与 withOfficialCodexSyntheticProfile 各管一个入口、互不影响：后者替换可选节等入口，够不到字段序；
// 需要同时覆盖两者的用例叠加使用即可（两个夹具的 Cleanup 各自恢复各自的入口）。
func withOfficialCodexBodyOrderProfile(t *testing.T, mutate func(*profilecontract.SnapshotDoc)) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseMode(requireOfficialCodexLegacyBaseProfileMode(t)),
	)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	if mutate != nil {
		mutate(&doc)
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	executable, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	previous := officialCodexReleaseProfileForBodyOrder
	officialCodexReleaseProfileForBodyOrder = func(string) (profilecontract.ExecutableProfile, error) {
		return executable, nil
	}
	t.Cleanup(func() { officialCodexReleaseProfileForBodyOrder = previous })
	return executable
}

// syntheticBodyPayload 构造一个包含 order 里全部键的最小载荷，值只用于让编码器产出各键。
func syntheticBodyPayload(order []string) map[string]any {
	payload := make(map[string]any, len(order))
	for _, name := range order {
		payload[name] = "value-of-" + name
	}
	return payload
}

func TestOfficialCodexBodyFieldOrderEntryMatchesTheOriginalEntry(t *testing.T) {
	modes := []string{
		officialClientProfileModeActive, officialClientProfileModePrevious,
		"", "bogus", "ACTIVE", " active ", "Previous", "next",
	}
	endpoints := []string{
		officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS, officialCodexEndpointResponsesCompact,
		"no_such_endpoint", "",
	}
	for _, mode := range modes {
		for _, endpointID := range endpoints {
			label := fmt.Sprintf("mode=%q endpoint=%q", mode, endpointID)
			gotFields, gotErr := officialCodexBodyFieldOrderForMode(mode, endpointID)
			wantFields, wantErr := legacyOfficialCodexBodyFieldOrderForMode(mode, endpointID)
			// require.Equal 区分 nil 切片与空切片，字段序与失败时的 nil 返回值都逐一相同。
			require.Equal(t, wantFields, gotFields, label)
			requireSameBodyOrderError(t, wantErr, gotErr, label)
		}
	}
}

// 差分测试的参照是本文件里的原实现副本；这里再独立锁定两个槽位之外的失败关闭形态，
// 避免有人把副本与默认实现同步改成宽松化后差分测试依旧全绿。
func TestOfficialCodexBodyFieldOrderEntryFailsClosedOutsideTheTwoSlots(t *testing.T) {
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
			fields, err := officialCodexBodyFieldOrderForMode(mode, endpointID)
			require.NoError(t, err, "mode=%s endpoint=%s", mode, endpointID)
			require.NotEmpty(t, fields)
			require.Contains(t, fields, "input")
		}
		for _, endpointID := range []string{"no_such_endpoint", ""} {
			fields, err := officialCodexBodyFieldOrderForMode(mode, endpointID)
			require.Nil(t, fields)
			require.EqualError(t, err, "ExecutableProfile 缺少 WS frame endpoint", "mode=%s endpoint=%q", mode, endpointID)
		}
	}
	for _, mode := range []string{"", "bogus", "ACTIVE", " active ", "Previous", "next"} {
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
			fields, err := officialCodexBodyFieldOrderForMode(mode, endpointID)
			require.Error(t, err, "mode=%q 必须失败关闭", mode)
			require.Nil(t, fields)
			code, ok := officialegress.RuntimeErrorCodeOf(err)
			require.True(t, ok, "mode=%q 的错误应带稳定的运行时错误码：%v", mode, err)
			require.Equal(t, officialegress.RuntimeErrorCodeCatalogResolveFailed, code)
		}
	}
}

func TestOfficialCodexReleaseProfileForBodyOrderDefaultResolvesTheFormalCatalogStrictly(t *testing.T) {
	require.NotNil(t, officialCodexReleaseProfileForBodyOrder)
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		profile, err := officialCodexReleaseProfileForBodyOrder(mode)
		require.NoError(t, err)
		release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
		require.NoError(t, err)
		require.Equal(t, release.ExecutableProfile().Digest(), profile.Digest(), "mode=%s 应取到该槽位的正式画像", mode)
		require.Equal(t, release.ExecutableProfile().Version(), profile.Version())
	}
	for _, mode := range []string{"", "bogus", "ACTIVE", " active ", "Previous", "next"} {
		_, err := officialCodexReleaseProfileForBodyOrder(mode)
		require.Error(t, err, "mode=%q 不得被宽松化为 active", mode)
	}
	// 对照：可选节读取入口会把这些 mode 宽松化成 active；字段序入口必须区别于它。
	for _, mode := range []string{"", "ACTIVE", " active ", "next"} {
		_, err := officialCodexExecutableProfileForMode(mode)
		require.NoError(t, err, "对照：officialCodexExecutableProfileForMode 对 %q 宽松", mode)
	}
}

func TestOfficialCodexBodyFieldOrderEntryPassesModeVerbatimAndPropagatesErrors(t *testing.T) {
	previous := officialCodexReleaseProfileForBodyOrder
	t.Cleanup(func() { officialCodexReleaseProfileForBodyOrder = previous })

	var seen []string
	officialCodexReleaseProfileForBodyOrder = func(mode string) (profilecontract.ExecutableProfile, error) {
		seen = append(seen, mode)
		return previous(mode)
	}
	_, _ = officialCodexBodyFieldOrderForMode(" Previous ", officialCodexEndpointResponsesHTTP)
	_, _ = officialCodexBodyFieldOrderForMode(officialClientProfileModeActive, officialCodexEndpointResponsesWS)
	require.Equal(t, []string{" Previous ", officialClientProfileModeActive}, seen, "mode 必须原样传给取画像步骤")

	boom := errors.New("取画像失败")
	officialCodexReleaseProfileForBodyOrder = func(string) (profilecontract.ExecutableProfile, error) {
		return profilecontract.ExecutableProfile{}, boom
	}
	fields, err := officialCodexBodyFieldOrderForMode(officialClientProfileModeActive, officialCodexEndpointResponsesHTTP)
	require.Nil(t, fields)
	require.ErrorIs(t, err, boom, "取画像步骤的错误必须原样透传")
}

func TestOfficialCodexBodyFieldOrderEntryCanBeReplacedBySyntheticProfile(t *testing.T) {
	// 对照取正式目录里的旧画像槽位：晋升前是 active，晋升后 active 已是较新画像、旧画像在 previous。
	legacyMode := requireOfficialCodexLegacyBaseProfileMode(t)
	formalHTTP, err := officialCodexBodyFieldOrderForMode(legacyMode, officialCodexEndpointResponsesHTTP)
	require.NoError(t, err)
	formalWS, err := officialCodexBodyFieldOrderForMode(legacyMode, officialCodexEndpointResponsesWS)
	require.NoError(t, err)
	// 前提：合成画像的字段序必须区别于正式旧画像，否则替换不可观察。
	require.NotEqual(t, syntheticNewerHTTPBodyOrder, formalHTTP)
	require.NotEqual(t, syntheticNewerWSBodyOrder, formalWS)

	synthetic := withOfficialCodexBodyOrderProfile(t, syntheticNewerBodyContractMutation(t))
	require.NotNil(t, synthetic.Optional().BaseInstructions)
	require.NotNil(t, synthetic.Optional().AccessPrograms)
	// 两个入口各管各的：字段序夹具不改可选节读取入口。
	require.Nil(t, officialCodexOptionalSectionsForMode(legacyMode).BaseInstructions)

	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		gotHTTP, err := officialCodexBodyFieldOrderForMode(mode, officialCodexEndpointResponsesHTTP)
		require.NoError(t, err)
		require.Equal(t, syntheticNewerHTTPBodyOrder, gotHTTP, "mode=%s", mode)
		gotWS, err := officialCodexBodyFieldOrderForMode(mode, officialCodexEndpointResponsesWS)
		require.NoError(t, err)
		require.Equal(t, syntheticNewerWSBodyOrder, gotWS, "mode=%s", mode)
	}

	// 顶层白名单随之改变：instructions 不再属于闭集，access_programs 属于。
	for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
		allow, err := officialOpenAITopLevelAllowSetForMode(officialClientProfileModeActive, endpointID)
		require.NoError(t, err)
		require.NotContains(t, allow, "instructions", "端点 %s", endpointID)
		require.Contains(t, allow, "access_programs", "端点 %s", endpointID)
	}

	// HTTP 与 WS 编码都按合成画像的字段序输出顶层键。
	rawHTTP, err := marshalOfficialOpenAIHTTPJSON(officialClientProfileModeActive, syntheticBodyPayload(syntheticNewerHTTPBodyOrder), false)
	require.NoError(t, err)
	require.Equal(t, syntheticNewerHTTPBodyOrder, jsonTopLevelKeys(t, string(rawHTTP)))
	rawWS, err := marshalOfficialOpenAIWSJSONPreservingRaw(officialClientProfileModeActive, syntheticBodyPayload(syntheticNewerWSBodyOrder), nil)
	require.NoError(t, err)
	require.Equal(t, syntheticNewerWSBodyOrder, jsonTopLevelKeys(t, string(rawWS)))
}

func TestOfficialCodexBodyFieldOrderEntryIsRestoredAfterSyntheticProfile(t *testing.T) {
	// 观察对象取正式目录里的旧画像槽位：合成画像的字段序与较新的正式画像相同，只有旧画像能看出替换。
	legacyMode := requireOfficialCodexLegacyBaseProfileMode(t)
	before, err := officialCodexBodyFieldOrderForMode(legacyMode, officialCodexEndpointResponsesWS)
	require.NoError(t, err)

	t.Run("替换期间", func(t *testing.T) {
		withOfficialCodexBodyOrderProfile(t, syntheticNewerBodyContractMutation(t))
		during, err := officialCodexBodyFieldOrderForMode(legacyMode, officialCodexEndpointResponsesWS)
		require.NoError(t, err)
		require.NotEqual(t, before, during, "替换期间入口应读到合成画像")
	})

	after, err := officialCodexBodyFieldOrderForMode(legacyMode, officialCodexEndpointResponsesWS)
	require.NoError(t, err)
	require.Equal(t, before, after, "夹具的 Cleanup 必须把入口恢复为默认实现")
	// 恢复的仍是严格的默认实现。
	_, err = officialCodexReleaseProfileForBodyOrder("")
	require.Error(t, err)
}
