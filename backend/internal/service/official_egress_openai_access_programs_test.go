package service

import (
	"slices"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// access_programs 值域节（AccessPrograms）的行为：
//   - 画像声明该节后，入站取值只有落在节的子集内才允许出站，其余整字段删除并告警；
//   - 第一版从不生成缺席的字段，节里写了默认程序也不使用；
//   - HTTP 定型与 WS 派生首帧共用同一个函数，预热帧、链帧、增量帧与工具续接帧继承归一化后的取值；
//   - 节缺省时的旧形态由 official_egress_openai_access_programs_legacy_test.go 与 WS 旧形态黄金固化。

// accessProgramsExpectation 是一种入站取值在值域 {standard} 下的预期结果。
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
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	section := &profilecontract.AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}}
	expectations := accessProgramsExpectations()
	values := accessProgramsInboundValues()
	require.Len(t, expectations, len(values))
	for _, value := range values {
		expectation, ok := expectations[value.name]
		require.True(t, ok, value.name)
		payload := decodeAccessProgramsPayload(t, value)
		before := cloneOfficialOpenAIMap(payload)
		modified := normalizeOfficialCodexAccessPrograms(payload, section)
		if !expectation.dropped {
			require.False(t, modified, value.name)
			require.Equal(t, before, payload, "合法取值与缺席都不得改动 payload：%s", value.name)
			continue
		}
		require.True(t, modified, value.name)
		_, exists := payload[officialCodexAccessProgramsField]
		require.False(t, exists, "%s：越出值域必须整字段删除", value.name)
		require.Equal(t, "m", payload["model"], "只删除 access_programs，其余字段不动：%s", value.name)
		require.Len(t, payload, 1, value.name)
	}
	require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings())
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
			require.False(t, normalizeOfficialCodexAccessPrograms(payload, section), "%s：%s", tt.name, program)
			require.Contains(t, payload, "access_programs", "%s：%s", tt.name, program)
		}
		for _, program := range tt.dropped {
			payload := map[string]any{"access_programs": map[string]any{"cyber": program}}
			require.True(t, normalizeOfficialCodexAccessPrograms(payload, section), "%s：%s", tt.name, program)
			require.NotContains(t, payload, "access_programs", "%s：%s", tt.name, program)
		}
	}
}

// 第一版只透传、不生成：缺席就保持缺席，节里写了默认程序也不使用；节为空与 payload 为空都是恒等操作。
func TestNormalizeOfficialCodexAccessProgramsNeverGenerates(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	for _, section := range []*profilecontract.AccessProgramsSection{
		{AllowedCyberPrograms: []string{"standard"}},
		{AllowedCyberPrograms: []string{"standard"}, DefaultCyberProgram: "standard"},
		{AllowedCyberPrograms: []string{"daybreak_blue", "standard"}, DefaultCyberProgram: "daybreak_blue"},
	} {
		payload := map[string]any{"model": "m"}
		require.False(t, normalizeOfficialCodexAccessPrograms(payload, section))
		require.Equal(t, map[string]any{"model": "m"}, payload, "缺席的 access_programs 不得被生成：%+v", section)
	}

	// 节缺省：任何取值（含非法值）都不碰，字段是否保留交给顶层字段闭集。
	for _, value := range accessProgramsInboundValues() {
		payload := decodeAccessProgramsPayload(t, value)
		before := cloneOfficialOpenAIMap(payload)
		require.False(t, normalizeOfficialCodexAccessPrograms(payload, nil), value.name)
		require.Equal(t, before, payload, value.name)
	}
	require.False(t, normalizeOfficialCodexAccessPrograms(nil, &profilecontract.AccessProgramsSection{}))
	require.Empty(t, officialCodexAccessProgramsWarnings())
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
	require.Nil(t, officialCodexAccessProgramsSection(officialClientProfileModeActive))
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	require.NotNil(t, synthetic.Optional().AccessPrograms)
	section := officialCodexAccessProgramsSection(officialClientProfileModePrevious)
	require.NotNil(t, section)
	require.Equal(t, []string{"standard"}, section.AllowedCyberPrograms)
	require.Nil(t, officialCodexAccessProgramsSection(officialClientProfileModeActive), "Active 槽位不受 previous 槽位的合成画像影响")
}

// topLevelKeysOf 返回帧或请求体的顶层键（按出现顺序）。
func topLevelKeysOf(t *testing.T, body []byte) []string {
	t.Helper()
	return jsonTopLevelKeys(t, string(body))
}

func TestFinalizeOfficialOpenAIHTTPBodyAppliesAccessProgramsDomainOnTargetSlot(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialOpenAIProjectedFieldsSeenForTest(t)
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	expectations := accessProgramsExpectations()
	for _, lite := range []bool{false, true} {
		none, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(""),
		)
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(none, "access_programs").Exists(), "缺席就保持缺席")
		for _, value := range accessProgramsInboundValues() {
			label := value.name + "/lite=" + strconv.FormatBool(lite)
			expectation := expectations[value.name]
			final, modified, _, err := finalizeBaseInstructionsHTTP(
				t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(value.json),
			)
			require.NoError(t, err, label)
			switch {
			case value.json == "":
				require.Equal(t, none, final, label)
			case expectation.dropped:
				require.True(t, modified, label)
				require.False(t, gjson.GetBytes(final, "access_programs").Exists(), label)
				require.Equal(t, string(none), string(final), "%s：删除后与请求不带该字段时逐字节相同", label)
			default:
				require.Equal(t, value.json, gjson.GetBytes(final, "access_programs").Raw, "%s：合法取值原样保留", label)
				keys := topLevelKeysOf(t, final)
				require.Equal(t, "access_programs", keys[len(keys)-1], "%s：位置跟随画像字段序", label)
			}
		}
	}
	// 画像在 Body.Fields 里声明了该字段，闭集剔除不会碰它；被删除的取值是值域节删的，不是闭集删的。
	require.Empty(t, projectedFieldWarningsMentioning("access_programs"))
	require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings())
}

// 已经是规范形态的请求体（定型对它是幂等的），再追加一个 access_programs：合法取值不改写，越界取值删除并报告
// “正文被改写”，使调用方按改写后的正文编码，而不是把含越界取值的原字节转发出去。
func TestFinalizeOfficialOpenAIHTTPBodyReportsAccessProgramsDeletionAsModification(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	expectations := accessProgramsExpectations()
	for _, lite := range []bool{false, true} {
		canonical, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(""),
		)
		require.NoError(t, err)
		again, modified, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, lite, "entry", canonical)
		require.NoError(t, err)
		require.False(t, modified, "规范形态的请求体再次定型不应被改写（用例前提）")
		require.Equal(t, string(canonical), string(again))

		for _, value := range accessProgramsInboundValues() {
			label := value.name + "/lite=" + strconv.FormatBool(lite)
			body := append([]byte(nil), canonical[:len(canonical)-1]...)
			if value.json != "" {
				body = append(body, []byte(`,"access_programs":`+value.json)...)
			}
			body = append(body, '}')
			final, modified, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, lite, "entry", body)
			require.NoError(t, err, label)
			if expectations[value.name].dropped {
				require.True(t, modified, "%s：删除 access_programs 必须报告正文被改写", label)
				require.Equal(t, string(canonical), string(final), label)
				continue
			}
			require.False(t, modified, label)
			require.Equal(t, string(body), string(final), label)
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
}

// 第一版不默认生成：节里写了默认程序，请求没带 access_programs 时仍然不带。
func TestFinalizeOfficialOpenAIHTTPBodyDoesNotGenerateDefaultAccessPrograms(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, "standard"))
	require.Equal(t, "standard", synthetic.Optional().AccessPrograms.DefaultCyberProgram)
	for _, lite := range []bool{false, true} {
		final, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(""),
		)
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(final, "access_programs").Exists(), "lite=%v", lite)
	}
}

// 值域节与基础指令载体节各管各的门控：同一个画像同时声明两节时，二者同时生效、互不干扰。
func TestFinalizeOfficialOpenAIHTTPBodyAppliesBaseInstructionsAndAccessProgramsOnTheFullTargetProfile(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	require.NotNil(t, synthetic.Optional().BaseInstructions)
	require.NotNil(t, synthetic.Optional().AccessPrograms)
	for _, lite := range []bool{false, true} {
		final, _, _, err := finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(`{"cyber":"standard"}`),
		)
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(final, "instructions").Exists(), "基础指令走 input 载体")
		require.Equal(t, `{"cyber":"standard"}`, gjson.GetBytes(final, "access_programs").Raw)
		keys := topLevelKeysOf(t, final)
		require.Equal(t, "access_programs", keys[len(keys)-1])

		final, _, _, err = finalizeBaseInstructionsHTTP(
			t, officialClientProfileModePrevious, lite, "entry", buildAccessProgramsHTTPBody(`{"cyber":"daybreak_red"}`),
		)
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(final, "access_programs").Exists())
		require.Contains(t, string(final), "model.base_instructions", "值域节删除 access_programs 不影响基础指令载体")
	}
}

// previous 槽位（带值域节）与 Active 槽位（无节）交替构造请求，各自的结果互不影响。
func TestFinalizeOfficialOpenAIHTTPBodyAccessProgramsSlotsDoNotInfluenceEachOther(t *testing.T) {
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	body := buildAccessProgramsHTTPBody(`{"cyber":"standard"}`)
	var previousFirst, activeFirst []byte
	for round := 0; round < 3; round++ {
		previous, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, false, "entry", body)
		require.NoError(t, err)
		active, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModeActive, false, "entry", body)
		require.NoError(t, err)
		if round == 0 {
			previousFirst, activeFirst = previous, active
		}
		require.Equal(t, string(previousFirst), string(previous), "round=%d", round)
		require.Equal(t, string(activeFirst), string(active), "round=%d", round)
	}
	require.True(t, gjson.GetBytes(previousFirst, "access_programs").Exists())
	require.False(t, gjson.GetBytes(activeFirst, "access_programs").Exists())
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

// requireWSAccessPrograms 断言帧里 access_programs 的有无与取值。
func requireWSAccessPrograms(t *testing.T, frame []byte, value accessProgramsValue, expectation accessProgramsExpectation, label string) {
	t.Helper()
	raw, exists := wsFrameAccessPrograms(frame)
	if value.json == "" || expectation.dropped {
		require.False(t, exists, "%s：应当没有 access_programs，实际 %s", label, raw)
		return
	}
	require.True(t, exists, "%s：合法取值必须保留", label)
	require.Equal(t, value.json, raw, label)
}

func TestOpenAIWSFrameAppliesAccessProgramsDomainAndFramesInheritIt(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialOpenAIProjectedFieldsSeenForTest(t)
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	expectations := accessProgramsExpectations()
	for _, lite := range []bool{false, true} {
		for _, value := range accessProgramsInboundValues() {
			label := value.name + "/lite=" + strconv.FormatBool(lite)
			expectation := expectations[value.name]
			payload := wsAccessProgramsFirstFrame(value.json)
			ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, payload)

			first, result, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
			require.NoError(t, err, label)
			requireWSAccessPrograms(t, first, value, expectation, label+"/first")
			recorded := slices.ContainsFunc(result.Modifications, func(m OfficialEgressModification) bool {
				return m.Field == officialCodexAccessProgramsField
			})
			require.Equal(t, expectation.dropped, recorded, "%s：删除才登记改写记录", label)

			prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
			require.NoError(t, err, label)
			require.True(t, inject, label)
			requireWSAccessPrograms(t, prewarm, value, expectation, label+"/prewarm")
			chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, "resp_access_prewarm")
			require.NoError(t, err, label)
			requireWSAccessPrograms(t, chain, value, expectation, label+"/chain")

			// 增量帧自带 access_programs 时同样按值域归一化。
			incremental := wsAccessProgramsIncrementalFrame(value.json)
			incrementalFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, incremental, incremental, "resp_access_1", false)
			require.NoError(t, err, label)
			requireWSAccessPrograms(t, incrementalFinal, value, expectation, label+"/incremental")

			// 工具续接帧由已归一化的帧收敛而来，继承其中的取值。
			toolFrame := wsAccessProgramsToolFrame(value.json)
			toolFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, toolFrame, toolFrame, "resp_access_tool_parent", false)
			require.NoError(t, err, label)
			requireWSAccessPrograms(t, toolFinal, value, expectation, label+"/tool")
			narrowed, changed, err := buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(ctx, toolFinal, "resp_access_tool_parent")
			require.NoError(t, err, label)
			require.True(t, changed, label)
			requireWSAccessPrograms(t, narrowed, value, expectation, label+"/narrowed")
		}
	}
	require.Empty(t, projectedFieldWarningsMentioning("access_programs"), "字段在画像里声明过，闭集不会剔除它")
	require.Equal(t, expectedAccessProgramsWarnings(), officialCodexAccessProgramsWarnings())
}

// 官方客户端形态的入口同样走派生路径，同样受值域约束。
func TestOpenAIWSFrameAppliesAccessProgramsDomainToOfficialIngress(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	expectations := accessProgramsExpectations()
	for _, value := range accessProgramsInboundValues() {
		expectation := expectations[value.name]
		prewarmFrame := withWSLegacyAccessPrograms(t, newOfficialOpenAIWSFirstFrameForTest(t), value.json)
		ctx := newWSLegacyOfficialIngressContext(t, officialClientProfileModePrevious, prewarmFrame)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, prewarmFrame, prewarmFrame, "", false)
		require.NoError(t, err, value.name)
		requireWSAccessPrograms(t, final, value, expectation, value.name+"/prewarm")

		business := withWSLegacyAccessPrograms(t, withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
			"type":"response.create",
			"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}]
		}`)), value.json)
		final, _, err = prepareOpenAIOfficialEgressSemanticWSFrame(ctx, business, business, "", false)
		require.NoError(t, err, value.name)
		requireWSAccessPrograms(t, final, value, expectation, value.name+"/business")
	}
}

func TestOpenAIWSFrameDoesNotGenerateDefaultAccessPrograms(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, "standard"))
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	for _, lite := range []bool{false, true} {
		payload := wsAccessProgramsFirstFrame("")
		ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, payload)
		first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.NoError(t, err)
		prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
		require.NoError(t, err)
		require.True(t, inject)
		chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, "resp_access_prewarm")
		require.NoError(t, err)
		for name, frame := range map[string][]byte{"first": first, "prewarm": prewarm, "chain": chain} {
			_, exists := wsFrameAccessPrograms(frame)
			require.False(t, exists, "lite=%v %s：缺席的 access_programs 不得被生成", lite, name)
		}
	}
}

// previous 槽位（带值域节）与 Active 槽位（无节）的 WS 连接交替构造帧，各自的结果互不影响。
func TestOpenAIWSFrameAccessProgramsSlotsDoNotInfluenceEachOther(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	payload := wsAccessProgramsFirstFrame(`{"cyber":"standard"}`)
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
		require.Equal(t, string(previousFirst), string(normalizeWSLegacyFrame(previous)), "round=%d", round)
		require.Equal(t, string(activeFirst), string(normalizeWSLegacyFrame(active)), "round=%d", round)
	}
	_, previousHas := wsFrameAccessPrograms(previousFirst)
	_, activeHas := wsFrameAccessPrograms(activeFirst)
	require.True(t, previousHas)
	require.False(t, activeHas)
}

// 准备阶段端到端：真实 buildUpstreamRequest 经配置选择 previous 槽位，值域节生效；同一请求走 active 槽位时
// access_programs 被顶层字段闭集剔除。
func TestBuildUpstreamRequestAppliesAccessProgramsDomainOnPreviousSlot(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticAccessProgramsOnlyMutation(t, []string{"standard"}, ""))
	resetOfficialCodexAccessProgramsWarnedForTest(t)
	base := newOfficialOpenAIHTTPTestBody(t, true, true, false)
	for _, tt := range []struct {
		program string
		kept    bool
	}{{"standard", true}, {"daybreak_red", false}} {
		body := mutateOfficialOpenAIHTTPTestBody(t, base, func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["access_programs"] = map[string]any{"cyber": tt.program}
		})
		_, previousFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModePrevious, body)
		require.Equal(t, tt.kept, gjson.GetBytes(previousFinal, "access_programs").Exists(), tt.program)
		if tt.kept {
			require.Equal(t, tt.program, gjson.GetBytes(previousFinal, "access_programs.cyber").String())
		}
		_, activeFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModeActive, body)
		require.False(t, gjson.GetBytes(activeFinal, "access_programs").Exists(), "active 槽位没有值域节：%s", tt.program)
	}
}
