package service

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"testing"

	"github.com/stretchr/testify/require"
)

// 画像没有声明 BaseInstructions 节时，HTTP 定型的出站字节必须与引入该节之前逐字节相同。
//
// legacyHTTPInstructionGoldens 不是引入之后现算的：引入之前，用当时的代码对下面的输入矩阵
// （11 种 instructions／input 形态 × 入口契约与兼容层生成契约 × Lite 与非 Lite，共 44 组）实测记录
// 定型结果的 SHA-256、是否改写与错误文本。测试用它断言“节缺省即旧逻辑”，包括 Lite 的旧迁入形态
// （无 id、无 passthrough、键字典序）和非 Lite 保留顶层 instructions；任何让旧画像出站字节漂移的
// 改动都会立刻变红。

type legacyHTTPInstructionGolden struct {
	modified bool
	err      string
	sha256   string
}

var legacyHTTPInstructionGoldens = map[string]legacyHTTPInstructionGolden{
	"plain_eol|entry|false":                {modified: true, sha256: "9e5306a8f0fed0070067a1d1cf2cd8cbd23e9a899126717a92a220b1da659c85"},
	"plain_eol|entry|true":                 {modified: true, sha256: "6a7d0c26c3a89423f1142632ef6373b5a9f1540b58e7df5822578af0d11e8e52"},
	"plain_eol|generated|false":            {modified: true, sha256: "9e5306a8f0fed0070067a1d1cf2cd8cbd23e9a899126717a92a220b1da659c85"},
	"plain_eol|generated|true":             {modified: true, sha256: "6a7d0c26c3a89423f1142632ef6373b5a9f1540b58e7df5822578af0d11e8e52"},
	"plain|entry|false":                    {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"plain|entry|true":                     {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"plain|generated|false":                {modified: true, sha256: "704b2f98f9edeb2b3c9d4b15342b5419bc8aa326bea7de1097141b2da89d7617"},
	"plain|generated|true":                 {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"tools_first|entry|false":              {modified: true, sha256: "ac016039b85dc2990d37c5cc9e739ac92be572eef34f9657412f898022a43825"},
	"tools_first|entry|true":               {modified: true, sha256: "08f5ac93e2edbbd308f8872f21e26e6cf9316a29a1e69f4e81fc2b51e1a1ec75"},
	"tools_first|generated|false":          {modified: true, sha256: "ac016039b85dc2990d37c5cc9e739ac92be572eef34f9657412f898022a43825"},
	"tools_first|generated|true":           {modified: true, sha256: "08f5ac93e2edbbd308f8872f21e26e6cf9316a29a1e69f4e81fc2b51e1a1ec75"},
	"dev_copy|entry|false":                 {modified: true, sha256: "56157b3d8e819efcb3bc64ea30bb29fcad83f57555f5e45c082922f7727c73d3"},
	"dev_copy|entry|true":                  {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"dev_copy|generated|false":             {modified: true, sha256: "56157b3d8e819efcb3bc64ea30bb29fcad83f57555f5e45c082922f7727c73d3"},
	"dev_copy|generated|true":              {modified: true, sha256: "f0297112971d6c28824a5e56c0875fd995bbb46064bccf7229bf8cccc7f0a9b7"},
	"dev_copy_after_tools|entry|false":     {modified: true, sha256: "9921704f76b64c84a298faf703edcce38792b184622a670dfb1d40d3d9173228"},
	"dev_copy_after_tools|entry|true":      {modified: true, sha256: "08f5ac93e2edbbd308f8872f21e26e6cf9316a29a1e69f4e81fc2b51e1a1ec75"},
	"dev_copy_after_tools|generated|false": {modified: true, sha256: "9921704f76b64c84a298faf703edcce38792b184622a670dfb1d40d3d9173228"},
	"dev_copy_after_tools|generated|true":  {modified: true, sha256: "08f5ac93e2edbbd308f8872f21e26e6cf9316a29a1e69f4e81fc2b51e1a1ec75"},
	"dev_copy_not_first|entry|false":       {modified: true, sha256: "04b017fb8a3920ad324c8525285de5bbb5e24dd10f74dc29f86a409bc37709d8"},
	"dev_copy_not_first|entry|true":        {modified: true, sha256: "5c8dadb212f4e99616b7a4ed2988c0c104e608eb90a5ac86098816737f6ab151"},
	"dev_copy_not_first|generated|false":   {modified: true, sha256: "04b017fb8a3920ad324c8525285de5bbb5e24dd10f74dc29f86a409bc37709d8"},
	"dev_copy_not_first|generated|true":    {modified: true, sha256: "5c8dadb212f4e99616b7a4ed2988c0c104e608eb90a5ac86098816737f6ab151"},
	"blank|entry|false":                    {modified: true, sha256: "9ec71200a8556f54c99fe655e678d33bc4cda62369b9db5c2aa9adb3b3f8c85c"},
	"blank|entry|true":                     {modified: true, sha256: "1b1548d30381cb7c3ead69052d31d58d159b2bd6fe758a2788b880f2e117ae43"},
	"blank|generated|false":                {modified: true, sha256: "9ec71200a8556f54c99fe655e678d33bc4cda62369b9db5c2aa9adb3b3f8c85c"},
	"blank|generated|true":                 {modified: true, sha256: "1b1548d30381cb7c3ead69052d31d58d159b2bd6fe758a2788b880f2e117ae43"},
	"absent|entry|false":                   {modified: true, sha256: "9ec71200a8556f54c99fe655e678d33bc4cda62369b9db5c2aa9adb3b3f8c85c"},
	"absent|entry|true":                    {modified: true, sha256: "1b1548d30381cb7c3ead69052d31d58d159b2bd6fe758a2788b880f2e117ae43"},
	"absent|generated|false":               {modified: true, sha256: "9ec71200a8556f54c99fe655e678d33bc4cda62369b9db5c2aa9adb3b3f8c85c"},
	"absent|generated|true":                {modified: true, sha256: "1b1548d30381cb7c3ead69052d31d58d159b2bd6fe758a2788b880f2e117ae43"},
	"input_string|entry|false":             {modified: true, sha256: "d6a90abb8d455058c792bb10a204f237cfe62ad31c4480d459a2c84bb6aea1fb"},
	"input_string|entry|true":              {modified: true, sha256: "5f8cd9e31d01c8c986d59d595c3060d73d76da7c72ef9d2a3f6061d849ddfc6f"},
	"input_string|generated|false":         {modified: true, sha256: "d6a90abb8d455058c792bb10a204f237cfe62ad31c4480d459a2c84bb6aea1fb"},
	"input_string|generated|true":          {modified: true, sha256: "5f8cd9e31d01c8c986d59d595c3060d73d76da7c72ef9d2a3f6061d849ddfc6f"},
	"special_chars|entry|false":            {modified: true, sha256: "7534a461712fe5a4e8fed10fcd6b9a71184068227fde9fffac31f804b923681b"},
	"special_chars|entry|true":             {modified: true, sha256: "616574d7623711f318b0c7a26d3143a5fa572bbbdfa99f2ced9560c5c52e6fca"},
	"special_chars|generated|false":        {modified: true, sha256: "7534a461712fe5a4e8fed10fcd6b9a71184068227fde9fffac31f804b923681b"},
	"special_chars|generated|true":         {modified: true, sha256: "616574d7623711f318b0c7a26d3143a5fa572bbbdfa99f2ced9560c5c52e6fca"},
	"non_string|entry|false":               {modified: true, sha256: "999dc8ae4f7a36c568f1f041f2aa6a9333d858c240cd8e86e21e10dd216cff3c"},
	"non_string|entry|true":                {modified: false, err: "OpenAI official egress third-party instructions must be a string"},
	"non_string|generated|false":           {modified: true, sha256: "9ec71200a8556f54c99fe655e678d33bc4cda62369b9db5c2aa9adb3b3f8c85c"},
	"non_string|generated|true":            {modified: true, sha256: "1b1548d30381cb7c3ead69052d31d58d159b2bd6fe758a2788b880f2e117ae43"},
}

type baseInstructionsHTTPVariant struct {
	name string
	// instructions 是顶层 instructions 的 JSON 原文；空串表示请求不带该字段。
	instructions string
	// input 是 input 的 JSON 原文。
	input string
}

const (
	baseInstructionsUserItem   = `{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}`
	baseInstructionsToolsItem  = `{"type":"additional_tools","tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]}`
	baseInstructionsCopyItem   = `{"type":"message","role":"developer","content":[{"type":"input_text","text":"You are a helper."}]}`
	baseInstructionsTaggedItem = `{"type":"message","id":"msg_stale","role":"developer","content":[{"type":"input_text","text":"You are a helper."}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
)

// baseInstructionsLegacyVariants 是黄金表覆盖的 11 种形态，顺序与引入之前的实测转储一致。
func baseInstructionsLegacyVariants() []baseInstructionsHTTPVariant {
	return []baseInstructionsHTTPVariant{
		{"plain_eol", `"You are a helper.\n"`, `[` + baseInstructionsUserItem + `]`},
		{"plain", `"You are a helper."`, `[` + baseInstructionsUserItem + `]`},
		{"tools_first", `"You are a helper."`, `[` + baseInstructionsToolsItem + `,` + baseInstructionsUserItem + `]`},
		{"dev_copy", `"You are a helper."`, `[` + baseInstructionsCopyItem + `,` + baseInstructionsUserItem + `]`},
		{"dev_copy_after_tools", `"You are a helper."`, `[` + baseInstructionsToolsItem + `,` + baseInstructionsCopyItem + `,` + baseInstructionsUserItem + `]`},
		{"dev_copy_not_first", `"You are a helper."`, `[` + baseInstructionsUserItem + `,` + baseInstructionsCopyItem + `]`},
		{"blank", `"   "`, `[` + baseInstructionsUserItem + `]`},
		{"absent", ``, `[` + baseInstructionsUserItem + `]`},
		{"input_string", `"You are a helper."`, `"hi"`},
		{"special_chars", "\"a<b>&c \u2028 d \\u0001 \U0001F600 \\\"q\\\" \\\\\"", `[` + baseInstructionsUserItem + `]`},
		{"non_string", `123`, `[` + baseInstructionsUserItem + `]`},
	}
}

// buildBaseInstructionsHTTPBody 按固定模板拼出请求体；官方身份由 client_metadata 提供。
func buildBaseInstructionsHTTPBody(variant baseInstructionsHTTPVariant) []byte {
	instructions := ""
	if variant.instructions != "" {
		instructions = `"instructions":` + variant.instructions + `,`
	}
	return []byte(`{"model":"gpt-5.6-luna",` + instructions + `"input":` + variant.input +
		`,"tool_choice":"auto","reasoning":{"effort":"medium"},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
}

// finalizeBaseInstructionsHTTP 用与线上相同的入口完成一次 HTTP 定型。contractKind 取 entry（入口契约，
// 指令由入口显式提供）或 generated（兼容层生成契约）。ThreadID 取身份派生出的线程。
func finalizeBaseInstructionsHTTP(
	t *testing.T,
	mode string,
	lite bool,
	contractKind string,
	body []byte,
) ([]byte, bool, officialOpenAIHTTPIdentity, error) {
	t.Helper()
	var contract *officialOpenAIHTTPBodyContract
	var err error
	if contractKind == "entry" {
		contract, err = captureOfficialOpenAIHTTPBodyContract(body)
	} else {
		contract, err = captureGeneratedOfficialOpenAIHTTPBodyContract(body)
	}
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(nil, newOfficialOpenAIHTTPTestAccount(94100), body, contract, mode)
	require.NoError(t, err)
	final, modified, err := finalizeOfficialOpenAIHTTPBody(body, contract, identity, officialOpenAIReasoningDefaults{},
		officialOpenAIHTTPBodyOptions{
			ProfileMode: mode, UseResponsesLite: lite, SupportsParallelTools: true, ThreadID: identity.threadID,
		})
	return final, modified, identity, err
}

// requireLegacyHTTPBytes 对一个变体在 mode 槽位上的 44 组定型逐项断言旧黄金。
func requireLegacyHTTPBytes(t *testing.T, mode string) int {
	t.Helper()
	compared := 0
	for _, variant := range baseInstructionsLegacyVariants() {
		for _, contractKind := range []string{"entry", "generated"} {
			for _, lite := range []bool{false, true} {
				key := variant.name + "|" + contractKind + "|" + strconv.FormatBool(lite)
				golden, ok := legacyHTTPInstructionGoldens[key]
				require.True(t, ok, "黄金表缺少 %s", key)
				final, modified, _, err := finalizeBaseInstructionsHTTP(t, mode, lite, contractKind, buildBaseInstructionsHTTPBody(variant))
				if golden.err != "" {
					require.EqualError(t, err, golden.err, key)
					compared++
					continue
				}
				require.NoError(t, err, key)
				require.Equal(t, golden.modified, modified, key)
				sum := sha256.Sum256(final)
				require.Equal(t, golden.sha256, hex.EncodeToString(sum[:]),
					"%s：节缺省时出站字节漂移，实际：%s", key, final)
				compared++
			}
		}
	}
	return compared
}

func TestFinalizeOfficialOpenAIHTTPBodyKeepsLegacyBytesWhenBaseInstructionsSectionAbsent(t *testing.T) {
	require.Len(t, legacyHTTPInstructionGoldens, 44)
	// 黄金值固定的是“节缺省时”的出站字节，所以取正式目录里不声明该节的旧画像槽位：晋升前是 active，
	// 晋升后 active 已是声明该节的较新画像、旧画像退居 previous。
	legacyMode := requireOfficialCodexLegacyBaseProfileMode(t)
	require.Nil(t, officialCodexOptionalSectionsForMode(legacyMode).BaseInstructions, "底稿槽位必须不声明 BaseInstructions 节")
	require.Equal(t, 44, requireLegacyHTTPBytes(t, legacyMode))
}

// previous 槽位装了合成画像，但合成画像不声明 BaseInstructions 节：门控按调用冻结的 mode 解析各自画像的节，
// 没有节就走旧逻辑，字节与 Active 相同。
func TestFinalizeOfficialOpenAIHTTPBodyKeepsLegacyBytesOnPreviousSlotWithoutSection(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, nil)
	require.Nil(t, synthetic.Optional().BaseInstructions)
	require.Equal(t, 44, requireLegacyHTTPBytes(t, officialClientProfileModePrevious))
}
