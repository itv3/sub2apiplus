package service

import (
	"context"
	"slices"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// access_programs（画像 AccessPrograms 节）的行为：
//   - 校验：入站取值只有落在节的子集内才允许出站，其余整字段删除并告警；
//   - 生成：节里写了默认程序时，非预热请求在校验之后仍没有该字段（入站没带，或带的被删除了）就补出默认程序，
//     位置跟随画像字段序（末位）；节里没有默认程序时只校验、从不生成；
//   - 预热帧（generate=false）不绑定回合程序：入站预热帧不生成，网关补出的预热帧去掉继承来的取值；
//   - HTTP 定型与 WS 派生帧共用同一个函数；链帧、增量帧与工具续接帧都带最终取值；
//   - 画像没有该节时的旧形态由 official_egress_openai_access_programs_legacy_test.go 与 WS 旧形态黄金固化。
//
// 矩阵里的“无默认”画像是有节无默认的对照组，“默认=standard”是官方默认路径。

// accessProgramsDefaults 是测试矩阵里画像节的两种默认程序取值：空串＝有节无默认（对照组），standard＝官方默认路径。
var accessProgramsDefaults = []string{"", "standard"}

func accessProgramsDefaultLabel(defaultProgram string) string {
	if defaultProgram == "" {
		return "无默认"
	}
	return "默认=" + defaultProgram
}

// accessProgramsExpectation 是一种入站取值在值域 {standard} 下的校验结果。
type accessProgramsExpectation struct {
	dropped bool
	// reason 是被删除的原因；shown 是告警去重键里回显的程序名（非小写 token 统一记为 other）。
	reason string
	shown  string
}

func accessProgramsExpectations() map[string]accessProgramsExpectation {
	notAllowed := func(shown string) accessProgramsExpectation {
		return accessProgramsExpectation{true, officialCodexAccessProgramsRejectNotAllowed, shown}
	}
	return map[string]accessProgramsExpectation{
		"none":            {},
		"standard":        {},
		"daybreak_blue":   notAllowed("daybreak_blue"),
		"daybreak_red":    notAllowed("daybreak_red"),
		"unknown_program": notAllowed("zzz"),
		"empty_program":   notAllowed("other"),
		"upper_case":      notAllowed("other"),
		"padded":          notAllowed("other"),
		"cyber_number":    {true, officialCodexAccessProgramsRejectCyberNotString, "other"},
		"cyber_null":      {true, officialCodexAccessProgramsRejectCyberNotString, "other"},
		"cyber_array":     {true, officialCodexAccessProgramsRejectCyberNotString, "other"},
		"extra_key":       {true, officialCodexAccessProgramsRejectUnexpectedKeys, "other"},
		"other_key_only":  {true, officialCodexAccessProgramsRejectUnexpectedKeys, "other"},
		"empty_object":    {true, officialCodexAccessProgramsRejectMissingCyber, "other"},
		"null":            {true, officialCodexAccessProgramsRejectNotObject, "other"},
		"string":          {true, officialCodexAccessProgramsRejectNotObject, "other"},
		"array":           {true, officialCodexAccessProgramsRejectNotObject, "other"},
		"number":          {true, officialCodexAccessProgramsRejectNotObject, "other"},
		"bool":            {true, officialCodexAccessProgramsRejectNotObject, "other"},
	}
}

// accessProgramsFinalValue 是入站取值经校验（和默认生成）之后应有的 access_programs（JSON 原文）；
// present=false 表示最终没有该字段。合法取值原样保留；缺席或被删除时有默认程序就补出默认程序。
func accessProgramsFinalValue(
	value accessProgramsValue, expectation accessProgramsExpectation, defaultProgram string,
) (finalJSON string, present bool) {
	if value.json != "" && !expectation.dropped {
		return value.json, true
	}
	if defaultProgram != "" {
		return `{"cyber":"` + defaultProgram + `"}`, true
	}
	return "", false
}

// resetOfficialCodexAccessProgramsWarnedForTest 清空告警去重表并在用例结束时恢复。
func resetOfficialCodexAccessProgramsWarnedForTest(t *testing.T) {
	t.Helper()
	officialCodexAccessProgramsWarnMu.Lock()
	saved := officialCodexAccessProgramsWarned
	officialCodexAccessProgramsWarned = map[string]struct{}{}
	officialCodexAccessProgramsWarnMu.Unlock()
	t.Cleanup(func() {
		officialCodexAccessProgramsWarnMu.Lock()
		officialCodexAccessProgramsWarned = saved
		officialCodexAccessProgramsWarnMu.Unlock()
	})
}

// officialCodexAccessProgramsWarnings 返回当前去重表里的键（已排序）。
func officialCodexAccessProgramsWarnings() []string {
	officialCodexAccessProgramsWarnMu.Lock()
	defer officialCodexAccessProgramsWarnMu.Unlock()
	keys := make([]string, 0, len(officialCodexAccessProgramsWarned))
	for key := range officialCodexAccessProgramsWarned {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// expectedAccessProgramsWarnings 是把全部入站取值各跑一遍之后，去重表里应有的键。
func expectedAccessProgramsWarnings() []string {
	set := map[string]struct{}{}
	for _, expectation := range accessProgramsExpectations() {
		if expectation.dropped {
			set[expectation.reason+"|"+expectation.shown] = struct{}{}
		}
	}
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// syntheticAccessProgramsOnlyMutation 在 Active 画像上只增加 access_programs：两个 Responses 端点的 Body.Fields
// 末位追加该字段，并声明 AccessPrograms 节；其余节与字段序保持 Active 原样。用它证明值域节与基础指令载体节
// 各管各的门控，互不依赖。
func syntheticAccessProgramsOnlyMutation(
	t *testing.T, allowed []string, defaultProgram string,
) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
			endpoint := syntheticServiceSnapshotEndpoint(t, doc, endpointID)
			endpoint.Body.Fields = append(endpoint.Body.Fields, profilecontract.SnapshotBodyField{
				Name: officialCodexAccessProgramsField, OmitWhen: "none",
			})
		}
		doc.AccessPrograms = syntheticServiceRawSection(t, profilecontract.AccessProgramsSection{
			AllowedCyberPrograms: allowed, DefaultCyberProgram: defaultProgram,
		})
	}
}

// accessProgramsProfile 把“只增加 access_programs”的合成画像装在 previous 槽位，节的默认程序由参数给出。
func accessProgramsProfile(t *testing.T, defaultProgram string) profilecontract.ExecutableProfile {
	t.Helper()
	return withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, defaultProgram))
}

func decodeAccessProgramsPayload(t *testing.T, value accessProgramsValue) map[string]any {
	t.Helper()
	body := `{"model":"m"}`
	if value.json != "" {
		body = `{"model":"m","access_programs":` + value.json + `}`
	}
	payload, err := decodeOfficialJSONObjectUseNumber([]byte(body))
	require.NoError(t, err, value.name)
	return payload
}

func TestNormalizeOfficialCodexAccessProgramsAppliesTheSectionValueDomain(t *testing.T) {
	expectations := accessProgramsExpectations()
	values := accessProgramsInboundValues()
	require.Len(t, expectations, len(values))
	for _, defaultProgram := range accessProgramsDefaults {
		resetOfficialCodexAccessProgramsWarnedForTest(t)
		section := &profilecontract.AccessProgramsSection{
			AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: defaultProgram,
		}
		for _, value := range values {
			label := value.name + "/" + accessProgramsDefaultLabel(defaultProgram)
			expectation, ok := expectations[value.name]
			require.True(t, ok, label)
			payload := decodeAccessProgramsPayload(t, value)
			before := cloneOfficialOpenAIMap(payload)
			modified := normalizeOfficialCodexAccessPrograms(payload, section, true)

			wantJSON, wantPresent := accessProgramsFinalValue(value, expectation, defaultProgram)
			wantModified := expectation.dropped || (value.json == "" && wantPresent)
			require.Equal(t, wantModified, modified, label)
			if !wantModified {
				require.Equal(t, before, payload, "合法取值与缺席（且无默认）都不得改动 payload：%s", label)
			}
			require.Equal(t, "m", payload["model"], "只动 access_programs，其余字段不动：%s", label)
			current, present := payload[officialCodexAccessProgramsField]
			require.Equal(t, wantPresent, present, label)
			if wantPresent {
				wantPayload, err := decodeOfficialJSONObjectUseNumber([]byte(`{"access_programs":` + wantJSON + `}`))
				require.NoError(t, err, label)
				require.Equal(t, wantPayload[officialCodexAccessProgramsField], current, label)
			}
			require.Len(t, payload, 1+boolToInt(wantPresent), label)
		}
		require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings(), accessProgramsDefaultLabel(defaultProgram))
	}
}

func boolToInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

// allowGenerate=false（预热帧，或端点没有声明该字段）时只校验、不生成，哪怕节里写了默认程序。
func TestNormalizeOfficialCodexAccessProgramsValidatesWithoutGeneratingWhenGenerationIsNotAllowed(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	section := &profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "standard"}
	expectations := accessProgramsExpectations()
	for _, value := range accessProgramsInboundValues() {
		expectation := expectations[value.name]
		payload := decodeAccessProgramsPayload(t, value)
		modified := normalizeOfficialCodexAccessPrograms(payload, section, false)
		require.Equal(t, expectation.dropped, modified, value.name)
		_, present := payload[officialCodexAccessProgramsField]
		require.Equal(t, value.json != "" && !expectation.dropped, present, "%s：只保留合法取值，绝不补默认值", value.name)
	}
}

// 值域来自画像节的数据而不是写死在代码里：节允许什么就放行什么，不在节里的一律删除。
func TestNormalizeOfficialCodexAccessProgramsFollowsTheSectionData(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	tests := []struct {
		name    string
		allowed []string
		kept    []string
		dropped []string
	}{
		{"仅标准", []string{"standard"}, []string{"standard"}, []string{"daybreak_blue", "daybreak_red"}},
		{"蓝色与标准", []string{"daybreak_blue", "standard"}, []string{"daybreak_blue", "standard"}, []string{"daybreak_red"}},
		{"仅红色", []string{"daybreak_red"}, []string{"daybreak_red"}, []string{"standard", "daybreak_blue"}},
		{"空值域", nil, nil, []string{"standard", "daybreak_blue", "daybreak_red"}},
	}
	for _, tt := range tests {
		section := &profilecontract.AccessProgramsSection{AllowedCyberPrograms: tt.allowed}
		for _, program := range tt.kept {
			payload := map[string]any{"access_programs": map[string]any{"cyber": program}}
			require.False(t, normalizeOfficialCodexAccessPrograms(payload, section, false), "%s：%s", tt.name, program)
			require.Contains(t, payload, "access_programs", "%s：%s", tt.name, program)
		}
		for _, program := range tt.dropped {
			payload := map[string]any{"access_programs": map[string]any{"cyber": program}}
			require.True(t, normalizeOfficialCodexAccessPrograms(payload, section, false), "%s：%s", tt.name, program)
			require.NotContains(t, payload, "access_programs", "%s：%s", tt.name, program)
		}
	}
}

// 默认值取自画像节的 DefaultCyberProgram，不在代码里写死；没有默认程序、不允许生成、默认程序不在值域时都不生成。
func TestNormalizeOfficialCodexAccessProgramsGeneratesOnlyTheProfileDefault(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	generated := func(section *profilecontract.AccessProgramsSection, allowGenerate bool) (map[string]any, bool) {
		payload := map[string]any{"model": "m"}
		modified := normalizeOfficialCodexAccessPrograms(payload, section, allowGenerate)
		return payload, modified
	}

	payload, modified := generated(&profilecontract.AccessProgramsSection{
		AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "standard",
	}, true)
	require.True(t, modified)
	require.Equal(t, map[string]any{"model": "m", "access_programs": map[string]any{"cyber": "standard"}}, payload)

	// 默认程序是数据：画像换成别的默认程序，生成的就是别的程序。
	payload, modified = generated(&profilecontract.AccessProgramsSection{
		AllowedCyberPrograms: []string{"daybreak_blue", "standard"}, DefaultCyberProgram: "daybreak_blue",
	}, true)
	require.True(t, modified)
	require.Equal(t, map[string]any{"cyber": "daybreak_blue"}, payload["access_programs"])

	for name, tt := range map[string]struct {
		section       *profilecontract.AccessProgramsSection
		allowGenerate bool
	}{
		"节里没有默认程序":      {&profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}}, true},
		"不允许生成":         {&profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "standard"}, false},
		"默认程序不在值域里（防御）": {&profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "daybreak_red"}, true},
		"节缺省":           {nil, true},
	} {
		payload, modified := generated(tt.section, tt.allowGenerate)
		require.False(t, modified, name)
		require.Equal(t, map[string]any{"model": "m"}, payload, "%s：不得生成", name)
	}

	// 幂等：已经带着默认程序时再跑一遍不再改动。
	section := &profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "standard"}
	payload, _ = generated(section, true)
	require.False(t, normalizeOfficialCodexAccessPrograms(payload, section, true))

	// 节缺省：任何取值（含非法值）都不碰，字段是否保留交给顶层字段闭集。
	for _, value := range accessProgramsInboundValues() {
		payload := decodeAccessProgramsPayload(t, value)
		before := cloneOfficialOpenAIMap(payload)
		require.False(t, normalizeOfficialCodexAccessPrograms(payload, nil, true), value.name)
		require.Equal(t, before, payload, value.name)
	}
	require.False(t, normalizeOfficialCodexAccessPrograms(nil, section, true))
	require.Empty(t, officialCodexAccessProgramsWarnings())
}

func TestRemoveOfficialCodexAccessProgramsForPrewarmOnlyTouchesTheSectionSlots(t *testing.T) {
	section := &profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}}
	payload := map[string]any{"model": "m", "access_programs": map[string]any{"cyber": "standard"}}
	require.True(t, removeOfficialCodexAccessProgramsForPrewarm(payload, section))
	require.Equal(t, map[string]any{"model": "m"}, payload)
	require.False(t, removeOfficialCodexAccessProgramsForPrewarm(payload, section), "已经没有时不报告改动")

	payload = map[string]any{"model": "m", "access_programs": map[string]any{"cyber": "standard"}}
	require.False(t, removeOfficialCodexAccessProgramsForPrewarm(payload, nil), "节缺省时不碰")
	require.Contains(t, payload, "access_programs")
	require.False(t, removeOfficialCodexAccessProgramsForPrewarm(nil, section))
}

func TestOfficialCodexAccessProgramsGenerationAllowedFollowsTheEndpointFields(t *testing.T) {
	require.True(t, officialCodexAccessProgramsGenerationAllowed(map[string]struct{}{"model": {}, "access_programs": {}}))
	require.False(t, officialCodexAccessProgramsGenerationAllowed(map[string]struct{}{"model": {}}))
	require.False(t, officialCodexAccessProgramsGenerationAllowed(nil))
}

// 告警按（原因，程序名）去重，程序名只有小写 token 才回显，去重表封顶，构造流量不能让它无限增长。
func TestWarnOfficialCodexAccessProgramsDroppedDedupesAndCaps(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	allowed := []string{"standard"}
	notAllowed := officialCodexAccessProgramsRejectNotAllowed

	warnOfficialCodexAccessProgramsDropped(notAllowed, "daybreak_red", allowed)
	warnOfficialCodexAccessProgramsDropped(notAllowed, "daybreak_red", allowed)
	require.Equal(t, []string{"program_not_allowed|daybreak_red"}, officialCodexAccessProgramsWarnings())

	maxToken := strings.Repeat("a", officialCodexAccessProgramsLogValueLimit)
	for _, program := range []string{
		"", "Daybreak", "a b", "line\nbreak", "tab\t", "中文", "a-b",
		strings.Repeat("a", officialCodexAccessProgramsLogValueLimit+1),
	} {
		warnOfficialCodexAccessProgramsDropped(notAllowed, program, allowed)
	}
	warnOfficialCodexAccessProgramsDropped(notAllowed, maxToken, allowed)
	require.Equal(t, []string{
		"program_not_allowed|" + maxToken,
		"program_not_allowed|daybreak_red",
		"program_not_allowed|other",
	}, officialCodexAccessProgramsWarnings(), "不超过上限的小写 token 回显，其余统一记为 other")

	for index := 0; index < 3*officialCodexAccessProgramsWarnLimit; index++ {
		warnOfficialCodexAccessProgramsDropped(notAllowed, "p"+strconv.Itoa(index), allowed)
	}
	require.Len(t, officialCodexAccessProgramsWarnings(), officialCodexAccessProgramsWarnLimit, "去重表必须封顶")
}

func TestOfficialCodexAccessProgramsSectionFollowsTheFrozenMode(t *testing.T) {
	// 夹具之前先确认旧画像槽位本来就不声明该节：晋升前是 active，晋升后 active 已是较新画像、旧画像在 previous。
	require.Nil(t, officialCodexAccessProgramsSection(requireOfficialCodexLegacyBaseProfileMode(t)))
	synthetic := accessProgramsProfile(t, "standard")
	require.NotNil(t, synthetic.Optional().AccessPrograms)
	section := officialCodexAccessProgramsSection(officialClientProfileModePrevious)
	require.NotNil(t, section)
	require.Equal(t, []string{"standard"}, section.AllowedCyberPrograms)
	require.Equal(t, "standard", section.DefaultCyberProgram)
	require.Nil(t, officialCodexAccessProgramsSection(officialClientProfileModeActive), "Active 槽位不受 previous 槽位的合成画像影响")
}

// topLevelKeysOf 返回帧或请求体的顶层键（按出现顺序）。
func topLevelKeysOf(t *testing.T, body []byte) []string {
	t.Helper()
	return jsonTopLevelKeys(t, string(body))
}

// requireAccessProgramsLast 要求 access_programs 是顶层最后一个键（画像 Body.Fields 末位）。
func requireAccessProgramsLast(t *testing.T, body []byte, label string) {
	t.Helper()
	keys := topLevelKeysOf(t, body)
	require.Equal(t, officialCodexAccessProgramsField, keys[len(keys)-1], "%s：位置跟随画像字段序（末位）：%v", label, keys)
}

func TestFinalizeOfficialOpenAIHTTPBodyAppliesAccessProgramsDomainOnTargetSlot(t *testing.T) {
	expectations := accessProgramsExpectations()
	for _, defaultProgram := range accessProgramsDefaults {
		accessProgramsProfile(t, defaultProgram)
		resetOfficialOpenAIProjectedFieldsSeenForTest(t)
		resetOfficialCodexAccessProgramsWarnedForTest(t)
		for _, lite := range []bool{false, true} {
			none, _, _, err := finalizeBaseInstructionsHTTP(
				t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(""),
			)
			require.NoError(t, err)
			noneJSON, nonePresent := accessProgramsFinalValue(accessProgramsValue{"none", ""}, expectations["none"], defaultProgram)
			require.Equal(t, nonePresent, gjson.GetBytes(none, "access_programs").Exists(),
				"%s：只有节里有默认程序才补出，没有就保持缺席", accessProgramsDefaultLabel(defaultProgram))
			if nonePresent {
				require.Equal(t, noneJSON, gjson.GetBytes(none, "access_programs").Raw)
				requireAccessProgramsLast(t, none, "缺席补默认")
			}
			for _, value := range accessProgramsInboundValues() {
				label := value.name + "/lite=" + strconv.FormatBool(lite) + "/" + accessProgramsDefaultLabel(defaultProgram)
				expectation := expectations[value.name]
				final, modified, _, err := finalizeBaseInstructionsHTTP(
					t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(value.json),
				)
				require.NoError(t, err, label)
				wantJSON, wantPresent := accessProgramsFinalValue(value, expectation, defaultProgram)
				require.Equal(t, wantPresent, gjson.GetBytes(final, "access_programs").Exists(), label)
				if wantPresent {
					require.Equal(t, wantJSON, gjson.GetBytes(final, "access_programs").Raw, "%s：合法取值原样保留，缺席或被删除时是默认程序", label)
					requireAccessProgramsLast(t, final, label)
				}
				if expectation.dropped {
					require.True(t, modified, label)
					require.Equal(t, string(none), string(final), "%s：删除（以及补默认值）之后与请求不带该字段时逐字节相同", label)
				}
				if value.json == "" {
					require.Equal(t, string(none), string(final), label)
				}
			}
		}
		// 画像在 Body.Fields 里声明了该字段，闭集剔除不会碰它；被删除的取值是值域节删的，不是闭集删的。
		require.Empty(t, projectedFieldWarningsMentioning("access_programs"), accessProgramsDefaultLabel(defaultProgram))
		require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings(), accessProgramsDefaultLabel(defaultProgram))
	}
}

// 已经是规范形态的请求体（定型对它是幂等的），再追加一个 access_programs：合法取值不改写；越界取值删除并报告
// “正文被改写”，使调用方按改写后的正文编码，而不是把含越界取值的原字节转发出去；缺席且节里有默认程序时补出并报告改写。
func TestFinalizeOfficialOpenAIHTTPBodyReportsAccessProgramsChangesAsModification(t *testing.T) {
	expectations := accessProgramsExpectations()
	for _, lite := range []bool{false, true} {
		// 先在无默认的画像上取规范形态（不含 access_programs）。
		accessProgramsProfile(t, "")
		canonical, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(""),
		)
		require.NoError(t, err)
		again, modified, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, lite, "entry", canonical)
		require.NoError(t, err)
		require.False(t, modified, "规范形态的请求体再次定型不应被改写（用例前提）")
		require.Equal(t, string(canonical), string(again))

		withValue := func(access string) []byte {
			body := append([]byte(nil), canonical[:len(canonical)-1]...)
			if access != "" {
				body = append(body, []byte(`,"access_programs":`+access)...)
			}
			return append(body, '}')
		}
		withDefault := string(withValue(`{"cyber":"standard"}`))

		for _, defaultProgram := range accessProgramsDefaults {
			accessProgramsProfile(t, defaultProgram)
			for _, value := range accessProgramsInboundValues() {
				label := value.name + "/lite=" + strconv.FormatBool(lite) + "/" + accessProgramsDefaultLabel(defaultProgram)
				body := withValue(value.json)
				final, modified, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, lite, "entry", body)
				require.NoError(t, err, label)
				expectation := expectations[value.name]
				_, wantPresent := accessProgramsFinalValue(value, expectation, defaultProgram)
				wantModified := expectation.dropped || (value.json == "" && wantPresent)
				require.Equal(t, wantModified, modified, "%s：删除或补出 access_programs 必须报告正文被改写", label)
				switch {
				case !wantModified:
					require.Equal(t, string(body), string(final), label)
				case defaultProgram == "":
					require.Equal(t, string(canonical), string(final), label)
				default:
					require.Equal(t, withDefault, string(final), label)
				}
			}
		}
	}
}

// 值域来自画像节的数据，HTTP 定型读到的是请求冻结的 mode 对应画像的节。
func TestFinalizeOfficialOpenAIHTTPBodyAccessProgramsFollowTheSectionDataOnTargetSlot(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"daybreak_blue", "standard"}, ""))
	for _, tt := range []struct {
		program string
		kept    bool
	}{{"daybreak_blue", true}, {"standard", true}, {"daybreak_red", false}} {
		final, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, true, "entry",
			buildAccessProgramsHTTPBody(`{"cyber":"`+tt.program+`"}`),
		)
		require.NoError(t, err)
		require.Equal(t, tt.kept, gjson.GetBytes(final, "access_programs").Exists(), tt.program)
	}

	// 默认程序也是数据：画像把默认程序换成 daybreak_blue，缺席时补出的就是它。
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"daybreak_blue", "standard"}, "daybreak_blue"))
	final, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, true, "entry", buildAccessProgramsHTTPBody(""))
	require.NoError(t, err)
	require.Equal(t, `{"cyber":"daybreak_blue"}`, gjson.GetBytes(final, "access_programs").Raw)
	final, _, _, err = finalizeBaseInstructionsHTTP(
		t, officialClientProfileModePrevious, true, "entry", buildAccessProgramsHTTPBody(`{"cyber":"daybreak_red"}`),
	)
	require.NoError(t, err)
	require.Equal(t, `{"cyber":"daybreak_blue"}`, gjson.GetBytes(final, "access_programs").Raw, "越界取值删除后按默认程序补出")
	final, _, _, err = finalizeBaseInstructionsHTTP(
		t, officialClientProfileModePrevious, true, "entry", buildAccessProgramsHTTPBody(`{"cyber":"standard"}`),
	)
	require.NoError(t, err)
	require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(final, "access_programs").Raw, "值域内的合法取值不被默认程序覆盖")
}

// withAccessProgramsSectionButFieldsWithoutIt 模拟“节存在、端点的字段表没有声明 access_programs”：可选节入口读带默认程序的合成画像，
// 字段序入口读不含该字段的旧画像（晋升前是 Active，晋升后是 previous）。合法的画像契约不允许这种组合（节存在则两个 Responses 端点都必须声明字段），
// 这里用两个可替换入口拆开构造，验证调用点对“端点没有声明字段”的防御：不能补出字段表之外的字段。
func withAccessProgramsSectionButFieldsWithoutIt(t *testing.T) {
	t.Helper()
	accessProgramsProfile(t, "standard")
	// 字段表取正式目录里不声明 access_programs 的旧画像：晋升前是 active，晋升后旧画像在 previous。
	_, legacyProfile := officialCodexLegacyBase(t)
	previous := officialCodexReleaseProfileForBodyOrder
	officialCodexReleaseProfileForBodyOrder = func(string) (profilecontract.ExecutableProfile, error) {
		return legacyProfile, nil
	}
	t.Cleanup(func() { officialCodexReleaseProfileForBodyOrder = previous })
}

func TestFinalizeOfficialOpenAIHTTPBodyDoesNotGenerateAccessProgramsWhenTheEndpointDoesNotDeclareIt(t *testing.T) {
	withAccessProgramsSectionButFieldsWithoutIt(t)
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	require.NotNil(t, officialCodexAccessProgramsSection(officialClientProfileModePrevious), "前提：节存在")
	for _, lite := range []bool{false, true} {
		for _, access := range []string{"", `{"cyber":"standard"}`, `{"cyber":"daybreak_red"}`} {
			final, _, _, err := finalizeBaseInstructionsHTTP(
				t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(access),
			)
			require.NoError(t, err)
			require.NotContains(t, string(final), "access_programs", "lite=%v access=%q：字段表没有声明就不能补出，入站的也被闭集剔除", lite, access)
		}
	}
}

func TestOpenAIWSFrameDoesNotGenerateAccessProgramsWhenTheEndpointDoesNotDeclareIt(t *testing.T) {
	withAccessProgramsSectionButFieldsWithoutIt(t)
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	for _, lite := range []bool{false, true} {
		for _, access := range []string{"", `{"cyber":"standard"}`} {
			payload := wsAccessProgramsFirstFrame(access)
			ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, payload)
			first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
			require.NoError(t, err)
			require.NotContains(t, string(first), "access_programs", "lite=%v access=%q", lite, access)
		}
	}
}

// 值域节与基础指令载体节各管各的门控：同一个画像同时声明两节时，二者同时生效、互不干扰。
func TestFinalizeOfficialOpenAIHTTPBodyAppliesBaseInstructionsAndAccessProgramsOnTheFullTargetProfile(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	require.NotNil(t, synthetic.Optional().BaseInstructions)
	require.NotNil(t, synthetic.Optional().AccessPrograms)
	require.Equal(t, "standard", synthetic.Optional().AccessPrograms.DefaultCyberProgram, "合成目标画像与真实画像一致：有默认程序")
	for _, lite := range []bool{false, true} {
		for _, access := range []string{`{"cyber":"standard"}`, `{"cyber":"daybreak_red"}`, ""} {
			final, _, _, err := finalizeBaseInstructionsHTTP(
				t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(access),
			)
			require.NoError(t, err)
			require.False(t, gjson.GetBytes(final, "instructions").Exists(), "基础指令走 input 载体")
			require.Contains(t, string(final), "model.base_instructions", "值域节的删除与生成不影响基础指令载体")
			require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(final, "access_programs").Raw, "access=%q：最终恒为默认程序", access)
			requireAccessProgramsLast(t, final, "完整目标画像")
		}
	}
}

// previous 槽位（带值域节）与 Active 槽位（无节）交替构造请求，各自的结果互不影响。
func TestFinalizeOfficialOpenAIHTTPBodyAccessProgramsSlotsDoNotInfluenceEachOther(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	accessProgramsProfile(t, "standard")
	for _, access := range []string{"", `{"cyber":"standard"}`, `{"cyber":"daybreak_red"}`} {
		body := buildAccessProgramsHTTPBody(access)
		var previousFirst, activeFirst []byte
		for round := 0; round < 3; round++ {
			previous, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, false, "entry", body)
			require.NoError(t, err)
			active, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModeActive, false, "entry", body)
			require.NoError(t, err)
			if round == 0 {
				previousFirst, activeFirst = previous, active
			}
			require.Equal(t, string(previousFirst), string(previous), "access=%q round=%d", access, round)
			require.Equal(t, string(activeFirst), string(active), "access=%q round=%d", access, round)
		}
		require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(previousFirst, "access_programs").Raw, "access=%q", access)
		require.False(t, gjson.GetBytes(activeFirst, "access_programs").Exists(), "Active 槽位没有值域节，不生成也不放行：access=%q", access)
	}
}

// ---- HTTP：压缩请求与 guardian 请求 ----

// buildOfficialOpenAIHTTPRequestForSlotWithContext 与 buildOfficialOpenAIHTTPRequestForSlot 相同，只是入口上下文由调用方给出
// （例如 guardian 请求要带 guardian 子代理头）。
func buildOfficialOpenAIHTTPRequestForSlotWithContext(
	t *testing.T, mode string, body []byte, c *gin.Context, promptCacheKey string,
) (*OfficialEgressContext, []byte) {
	t.Helper()
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = mode
	req, err := (&OpenAIGatewayService{cfg: cfg}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
		openAIUpstreamRequestPlan{
			IsStream: true, PromptCacheKey: promptCacheKey, IsCodexCLI: true, OfficialEgressBodyContract: contract,
		},
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(req.Context())
	require.True(t, ok)
	final, err := readOfficialEgressRequestBodyBytes(req)
	require.NoError(t, err)
	return egressContext, final
}

// 压缩请求（输入末尾带 compaction_trigger）与 guardian 请求沿用回合上下文的程序：同样是非预热的 Responses 请求，
// 同样补出默认程序；入站带越界取值时先删除再补出。
func TestBuildUpstreamRequestGeneratesAccessProgramsForCompactionAndGuardianRequests(t *testing.T) {
	accessProgramsProfile(t, "standard")
	resetOfficialCodexAccessProgramsWarnedForTest(t)

	t.Run("压缩请求", func(t *testing.T) {
		for _, access := range []map[string]any{nil, {"cyber": "standard"}, {"cyber": "daybreak_red"}} {
			body := withCompactionTrigger(t, newOfficialOpenAIHTTPTestBody(t, false, false, false))
			if access != nil {
				body = mutateOfficialOpenAIHTTPTestBody(t, body, func(payload map[string]any, _ map[string]any, _ map[string]any) {
					payload["access_programs"] = access
				})
			}
			egressContext, final := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModePrevious, body)
			turnMetadata := mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value()
			require.Equal(t, "compaction", gjson.Get(turnMetadata, "request_kind").String(), "前提：这是一次压缩请求")
			require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(final, "access_programs").Raw, "access=%v", access)
			requireAccessProgramsLast(t, final, "压缩请求")
			_, activeFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModeActive, body)
			require.False(t, gjson.GetBytes(activeFinal, "access_programs").Exists(), "active 槽位没有值域节：access=%v", access)
		}
	})

	t.Run("guardian 请求", func(t *testing.T) {
		for _, access := range []map[string]any{nil, {"cyber": "standard"}, {"cyber": "daybreak_blue"}} {
			body := newOfficialOpenAIGuardianHTTPBody(t)
			if access != nil {
				body = mutateOfficialOpenAIHTTPTestBody(t, body, func(payload map[string]any, _ map[string]any, _ map[string]any) {
					payload["access_programs"] = access
				})
			}
			c := newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")
			egressContext, final := buildOfficialOpenAIHTTPRequestForSlotWithContext(
				t, officialClientProfileModePrevious, body, c, "guardian:"+testOfficialOpenAISessionID,
			)
			require.Equal(t, "guardian", egressContext.codexRuntimeState.ConditionalHeaders["x-openai-subagent"], "前提：这是 guardian 请求")
			require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(final, "access_programs").Raw, "access=%v", access)
			requireAccessProgramsLast(t, final, "guardian 请求")
		}
	})
}

// 准备阶段端到端：真实 buildUpstreamRequest 经配置选择 previous 槽位，值域节生效；同一请求走 active 槽位时
// access_programs 被顶层字段闭集剔除。
func TestBuildUpstreamRequestAppliesAccessProgramsDomainOnPreviousSlot(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	base := newOfficialOpenAIHTTPTestBody(t, true, true, false)
	for _, defaultProgram := range accessProgramsDefaults {
		accessProgramsProfile(t, defaultProgram)
		for _, tt := range []struct {
			name   string
			access map[string]any
			want   string // 期望的最终取值（JSON 原文），空串表示没有该字段
		}{
			{"合法", map[string]any{"cyber": "standard"}, `{"cyber":"standard"}`},
			{"越界", map[string]any{"cyber": "daybreak_red"}, map[bool]string{true: `{"cyber":"standard"}`, false: ""}[defaultProgram != ""]},
			{"缺席", nil, map[bool]string{true: `{"cyber":"standard"}`, false: ""}[defaultProgram != ""]},
		} {
			label := tt.name + "/" + accessProgramsDefaultLabel(defaultProgram)
			body := base
			if tt.access != nil {
				body = mutateOfficialOpenAIHTTPTestBody(t, base, func(payload map[string]any, _ map[string]any, _ map[string]any) {
					payload["access_programs"] = tt.access
				})
			}
			_, previousFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModePrevious, body)
			require.Equal(t, tt.want, gjson.GetBytes(previousFinal, "access_programs").Raw, label)
			_, activeFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModeActive, body)
			require.False(t, gjson.GetBytes(activeFinal, "access_programs").Exists(), "active 槽位没有值域节：%s", label)
		}
	}
}

// ---- WebSocket ----

// wsAccessProgramsFirstFrame 是带 developer 消息的第三方首帧（非 Lite 的预热要求首帧里有 developer 消息）。
func wsAccessProgramsFirstFrame(access string) []byte {
	return buildWSLegacyDerivedPayload(wsLegacyVariant{
		name:         "access",
		instructions: `"You are a helper."`,
		input:        `[` + baseInstructionsCopyItem + `,` + baseInstructionsUserItem + `]`,
		access:       access,
	})
}

func wsAccessProgramsIncrementalFrame(access string) []byte {
	field := ""
	if access != "" {
		field = `"access_programs":` + access + `,`
	}
	return []byte(`{"type":"response.create","model":"gpt-5.6-luna","previous_response_id":"resp_access_1",` + field +
		`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"again"}]}]}`)
}

func wsAccessProgramsToolFrame(access string) []byte {
	field := ""
	if access != "" {
		field = `"access_programs":` + access + `,`
	}
	return []byte(`{"type":"response.create","model":"gpt-5.6-luna",` + field + `"input":[
		{"type":"additional_tools","tools":[]},
		{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件"}]},
		{"type":"reasoning","encrypted_content":"encrypted"},
		{"type":"function_call","call_id":"call_access_1","name":"read","arguments":"{}"},
		{"type":"function_call_output","call_id":"call_access_1","output":"ok"}]}`)
}

func wsFrameAccessPrograms(frame []byte) (string, bool) {
	value := gjson.GetBytes(frame, "access_programs")
	return value.Raw, value.Exists()
}

// requireWSAccessPrograms 断言帧里 access_programs 的有无与取值：合法取值原样保留，缺席或被删除时是默认程序（若有）。
func requireWSAccessPrograms(
	t *testing.T, frame []byte, value accessProgramsValue, expectation accessProgramsExpectation, defaultProgram string, label string,
) {
	t.Helper()
	wantJSON, wantPresent := accessProgramsFinalValue(value, expectation, defaultProgram)
	raw, exists := wsFrameAccessPrograms(frame)
	if !wantPresent {
		require.False(t, exists, "%s：应当没有 access_programs，实际 %s", label, raw)
		return
	}
	require.True(t, exists, "%s：应当有 access_programs", label)
	require.Equal(t, wantJSON, raw, label)
	requireAccessProgramsLast(t, frame, label)
}

func TestOpenAIWSFrameAppliesAccessProgramsDomainAndFramesInheritIt(t *testing.T) {
	expectations := accessProgramsExpectations()
	for _, defaultProgram := range accessProgramsDefaults {
		accessProgramsProfile(t, defaultProgram)
		resetOfficialOpenAIProjectedFieldsSeenForTest(t)
		resetOfficialCodexAccessProgramsWarnedForTest(t)
		for _, lite := range []bool{false, true} {
			for _, value := range accessProgramsInboundValues() {
				label := value.name + "/lite=" + strconv.FormatBool(lite) + "/" + accessProgramsDefaultLabel(defaultProgram)
				expectation := expectations[value.name]
				payload := wsAccessProgramsFirstFrame(value.json)
				ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, payload)

				first, result, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
				require.NoError(t, err, label)
				requireWSAccessPrograms(t, first, value, expectation, defaultProgram, label+"/first")
				recorded := slices.ContainsFunc(result.Modifications, func(m OfficialEgressModification) bool {
					return m.Field == officialCodexAccessProgramsField
				})
				_, wantPresent := accessProgramsFinalValue(value, expectation, defaultProgram)
				require.Equal(t, expectation.dropped || (value.json == "" && wantPresent), recorded, "%s：删除或补出才登记改写记录", label)

				// 网关补出的预热帧不绑定回合程序：无论业务帧上有什么，预热帧都不带 access_programs。
				prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
				require.NoError(t, err, label)
				require.True(t, inject, label)
				_, prewarmHas := wsFrameAccessPrograms(prewarm)
				require.False(t, prewarmHas, "%s/prewarm：预热帧不带 access_programs", label)
				chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, "resp_access_prewarm")
				require.NoError(t, err, label)
				requireWSAccessPrograms(t, chain, value, expectation, defaultProgram, label+"/chain")

				// 增量帧自带 access_programs 时同样按值域归一化，缺席时补默认程序。
				incremental := wsAccessProgramsIncrementalFrame(value.json)
				incrementalFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, incremental, incremental, "resp_access_1", false)
				require.NoError(t, err, label)
				requireWSAccessPrograms(t, incrementalFinal, value, expectation, defaultProgram, label+"/incremental")

				// 工具续接帧由已归一化的帧收敛而来，继承其中的取值。
				toolFrame := wsAccessProgramsToolFrame(value.json)
				toolFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, toolFrame, toolFrame, "resp_access_tool_parent", false)
				require.NoError(t, err, label)
				requireWSAccessPrograms(t, toolFinal, value, expectation, defaultProgram, label+"/tool")
				narrowed, changed, err := buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(ctx, toolFinal, "resp_access_tool_parent")
				require.NoError(t, err, label)
				require.True(t, changed, label)
				requireWSAccessPrograms(t, narrowed, value, expectation, defaultProgram, label+"/narrowed")
			}
		}
		require.Empty(t, projectedFieldWarningsMentioning("access_programs"), "字段在画像里声明过，闭集不会剔除它")
		require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings(), accessProgramsDefaultLabel(defaultProgram))
	}
}

// 官方形态的入口同样走派生路径：预热帧（generate=false）不生成，业务帧缺席时补默认程序；两种帧都受值域约束。
func TestOpenAIWSFrameAppliesAccessProgramsDomainToOfficialIngress(t *testing.T) {
	expectations := accessProgramsExpectations()
	for _, defaultProgram := range accessProgramsDefaults {
		accessProgramsProfile(t, defaultProgram)
		resetOfficialCodexAccessProgramsWarnedForTest(t)
		for _, value := range accessProgramsInboundValues() {
			label := value.name + "/" + accessProgramsDefaultLabel(defaultProgram)
			expectation := expectations[value.name]
			prewarmFrame := withWSLegacyAccessPrograms(t, newOfficialOpenAIWSFirstFrameForTest(t), value.json)
			ctx := newWSLegacyOfficialIngressContext(t, officialClientProfileModePrevious, prewarmFrame)
			final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, prewarmFrame, prewarmFrame, "", false)
			require.NoError(t, err, label)
			require.False(t, gjson.GetBytes(final, "generate").Bool(), "前提：这是预热帧")
			raw, exists := wsFrameAccessPrograms(final)
			if value.json != "" && !expectation.dropped {
				require.True(t, exists, "%s/prewarm：预热帧上合法取值不动", label)
				require.Equal(t, value.json, raw, label)
			} else {
				require.False(t, exists, "%s/prewarm：预热帧不生成默认程序，越界取值删除后也不补", label)
			}

			business := withWSLegacyAccessPrograms(t, withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
				"type":"response.create",
				"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}]
			}`)), value.json)
			final, _, err = prepareOpenAIOfficialEgressSemanticWSFrame(ctx, business, business, "", false)
			require.NoError(t, err, label)
			requireWSAccessPrograms(t, final, value, expectation, defaultProgram, label+"/business")
		}
	}
}

// 压缩请求（输入末尾带 compaction_trigger）是非预热帧，同样补默认程序。
func TestOpenAIWSFrameGeneratesAccessProgramsForCompactionRequests(t *testing.T) {
	accessProgramsProfile(t, "standard")
	for _, lite := range []bool{false, true} {
		payload := withCompactionTrigger(t, wsAccessProgramsFirstFrame(""))
		ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, payload)
		first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.NoError(t, err)
		require.Equal(t, "compaction_trigger", gjson.GetBytes(first, "input.#(type==\"compaction_trigger\").type").String(), "前提：帧里带压缩触发项")
		raw, exists := wsFrameAccessPrograms(first)
		require.True(t, exists, "lite=%v", lite)
		require.Equal(t, `{"cyber":"standard"}`, raw)
	}
}

// guardian 请求沿用回合上下文的程序：预热帧（generate=false）不生成，业务帧补默认程序。
func TestOpenAIWSFrameGeneratesAccessProgramsForGuardianRequests(t *testing.T) {
	accessProgramsProfile(t, "standard")
	firstPayload := newOfficialOpenAIGuardianWSFirstFrameForTest(t)
	c := newOfficialOpenAIWSIngressContextForTest(t, firstPayload)
	applyOfficialOpenAIGuardianWSHeadersForTest(c, firstPayload)
	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(), c, newOfficialOpenAIHTTPTestAccount(94),
		"wss://chatgpt.com/backend-api/codex/responses", firstPayload, wsLegacyConfigForMode(officialClientProfileModePrevious),
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	require.Equal(t, "guardian", egressContext.codexRuntimeState.ConditionalHeaders["x-openai-subagent"], "前提：这是 guardian 连接")

	prewarmFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, firstPayload, firstPayload, "", false)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(prewarmFinal, "generate").Bool(), "前提：guardian 首帧是预热帧")
	_, prewarmHas := wsFrameAccessPrograms(prewarmFinal)
	require.False(t, prewarmHas, "预热帧不生成")

	business := []byte(`{"type":"response.create","model":"gpt-5.6-luna",` +
		`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"审阅"}]}]}`)
	final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, business, business, "", false)
	require.NoError(t, err)
	raw, exists := wsFrameAccessPrograms(final)
	require.True(t, exists)
	require.Equal(t, `{"cyber":"standard"}`, raw)
	requireAccessProgramsLast(t, final, "guardian 业务帧")
}

// previous 槽位（带值域节与默认程序）与 Active 槽位（无节）的 WS 连接交替构造帧，各自的结果互不影响。
func TestOpenAIWSFrameAccessProgramsSlotsDoNotInfluenceEachOther(t *testing.T) {
	accessProgramsProfile(t, "standard")
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	for _, access := range []string{"", `{"cyber":"standard"}`, `{"cyber":"daybreak_red"}`} {
		payload := wsAccessProgramsFirstFrame(access)
		var previousFirst, activeFirst []byte
		for round := 0; round < 3; round++ {
			previousCtx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, true, payload)
			previous, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(previousCtx, payload, payload, "", false)
			require.NoError(t, err)
			activeCtx := newWSLegacyDerivedContext(t, officialClientProfileModeActive, true, payload)
			active, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(activeCtx, payload, payload, "", false)
			require.NoError(t, err)
			if round == 0 {
				previousFirst, activeFirst = normalizeWSLegacyFrame(previous), normalizeWSLegacyFrame(active)
			}
			require.Equal(t, string(previousFirst), string(normalizeWSLegacyFrame(previous)), "access=%q round=%d", access, round)
			require.Equal(t, string(activeFirst), string(normalizeWSLegacyFrame(active)), "access=%q round=%d", access, round)
		}
		previousRaw, previousHas := wsFrameAccessPrograms(previousFirst)
		_, activeHas := wsFrameAccessPrograms(activeFirst)
		require.True(t, previousHas, "access=%q", access)
		require.Equal(t, `{"cyber":"standard"}`, previousRaw, "access=%q", access)
		require.False(t, activeHas, "access=%q", access)
	}
}
