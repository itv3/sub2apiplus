package service

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"testing"

	"github.com/stretchr/testify/require"
)

// access_programs 在“画像没有 AccessPrograms 值域节”时的旧形态黄金（HTTP 定型）。
//
// 画像没有该节（Active 槽位，或 previous 槽位装了不声明该节的合成画像）时，access_programs 不在顶层字段闭集内，
// 任何取值都被闭集剔除，出站字节与请求不带该字段时相同。下面的表是在引入该节的代码之前用当时的代码实测记录的：
// 每一步记定型结果的 SHA-256 与是否改写。

// accessProgramsValue 是入站 access_programs 的一种取值（JSON 原文，空串表示请求不带该字段）。
type accessProgramsValue struct {
	name string
	json string
}

// accessProgramsInboundValues 覆盖合法值、画像子集外的值与各种形态非法的值。
func accessProgramsInboundValues() []accessProgramsValue {
	return []accessProgramsValue{
		{"none", ""},
		{"standard", `{"cyber":"standard"}`},
		{"daybreak_blue", `{"cyber":"daybreak_blue"}`},
		{"daybreak_red", `{"cyber":"daybreak_red"}`},
		{"unknown_program", `{"cyber":"zzz"}`},
		{"empty_program", `{"cyber":""}`},
		{"upper_case", `{"cyber":"Standard"}`},
		{"padded", `{"cyber":" standard"}`},
		{"cyber_number", `{"cyber":1}`},
		{"cyber_null", `{"cyber":null}`},
		{"cyber_array", `{"cyber":["standard"]}`},
		{"extra_key", `{"cyber":"standard","extra":true}`},
		{"other_key_only", `{"other":"standard"}`},
		{"empty_object", `{}`},
		{"null", `null`},
		{"string", `"standard"`},
		{"array", `["standard"]`},
		{"number", `1`},
		{"bool", `true`},
	}
}

// buildAccessProgramsHTTPBody 拼出带 access_programs 的 HTTP 请求体；官方身份由 client_metadata 提供。
func buildAccessProgramsHTTPBody(access string) []byte {
	field := ""
	if access != "" {
		field = `,"access_programs":` + access
	}
	return []byte(`{"model":"gpt-5.6-luna","instructions":"You are a helper.","input":[` + baseInstructionsUserItem +
		`],"tool_choice":"auto","reasoning":{"effort":"medium"}` + field +
		`,"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
}

// legacyAccessProgramsHTTPOutcome 是一步 HTTP 定型的结果。
type legacyAccessProgramsHTTPOutcome struct {
	modified bool
	sha256   string
	err      string
}

func collectLegacyAccessProgramsHTTPOutcomes(t *testing.T, mode string) map[string]legacyAccessProgramsHTTPOutcome {
	t.Helper()
	out := map[string]legacyAccessProgramsHTTPOutcome{}
	for _, lite := range []bool{false, true} {
		for _, value := range accessProgramsInboundValues() {
			key := value.name + "|lite=" + strconv.FormatBool(lite)
			final, modified, _, err := finalizeBaseInstructionsHTTP(
				t, mode, lite, "entry", buildAccessProgramsHTTPBody(value.json),
			)
			if err != nil {
				out[key] = legacyAccessProgramsHTTPOutcome{err: err.Error()}
				continue
			}
			sum := sha256.Sum256(final)
			out[key] = legacyAccessProgramsHTTPOutcome{modified: modified, sha256: hex.EncodeToString(sum[:])}
		}
	}
	return out
}

var legacyAccessProgramsHTTPGoldens = map[string]legacyAccessProgramsHTTPOutcome{
	"array|lite=false":           {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"array|lite=true":            {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"bool|lite=false":            {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"bool|lite=true":             {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"cyber_array|lite=false":     {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"cyber_array|lite=true":      {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"cyber_null|lite=false":      {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"cyber_null|lite=true":       {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"cyber_number|lite=false":    {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"cyber_number|lite=true":     {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"daybreak_blue|lite=false":   {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"daybreak_blue|lite=true":    {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"daybreak_red|lite=false":    {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"daybreak_red|lite=true":     {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"empty_object|lite=false":    {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"empty_object|lite=true":     {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"empty_program|lite=false":   {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"empty_program|lite=true":    {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"extra_key|lite=false":       {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"extra_key|lite=true":        {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"none|lite=false":            {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"none|lite=true":             {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"null|lite=false":            {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"null|lite=true":             {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"number|lite=false":          {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"number|lite=true":           {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"other_key_only|lite=false":  {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"other_key_only|lite=true":   {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"padded|lite=false":          {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"padded|lite=true":           {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"standard|lite=false":        {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"standard|lite=true":         {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"string|lite=false":          {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"string|lite=true":           {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"unknown_program|lite=false": {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"unknown_program|lite=true":  {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"upper_case|lite=false":      {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"upper_case|lite=true":       {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
}

func requireLegacyAccessProgramsHTTPOutcomes(t *testing.T, got map[string]legacyAccessProgramsHTTPOutcome) {
	t.Helper()
	require.Len(t, got, len(legacyAccessProgramsHTTPGoldens))
	for key, want := range legacyAccessProgramsHTTPGoldens {
		actual, ok := got[key]
		require.True(t, ok, "缺少 %s", key)
		require.Equal(t, want, actual, "%s：节缺省时 HTTP 出站字节漂移", key)
	}
}

func TestFinalizeOfficialOpenAIHTTPBodyDropsAccessProgramsWhenSectionAbsent(t *testing.T) {
	require.Len(t, legacyAccessProgramsHTTPGoldens, 38)
	// 黄金值固定的是“节缺省时”的出站字节，所以取正式目录里不声明该节的旧画像槽位：晋升前是 active，
	// 晋升后 active 已是声明该节的较新画像、旧画像退居 previous。
	legacyMode := requireOfficialCodexLegacyBaseProfileMode(t)
	require.Nil(t, officialCodexAccessProgramsSection(legacyMode), "底稿槽位必须不声明 AccessPrograms 节")
	requireLegacyAccessProgramsHTTPOutcomes(t, collectLegacyAccessProgramsHTTPOutcomes(t, legacyMode))
}

// previous 槽位装了合成画像，但合成画像不声明 AccessPrograms 节：门控按调用冻结的 mode 解析各自画像的节，
// 没有节就走旧逻辑，结果与旧画像槽位（晋升前是 Active，晋升后夹具让 active 读旧画像）逐项相同。
func TestFinalizeOfficialOpenAIHTTPBodyDropsAccessProgramsOnPreviousSlotWithoutSection(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, nil)
	require.Nil(t, synthetic.Optional().AccessPrograms)
	requireLegacyAccessProgramsHTTPOutcomes(t, collectLegacyAccessProgramsHTTPOutcomes(t, officialClientProfileModePrevious))
}
