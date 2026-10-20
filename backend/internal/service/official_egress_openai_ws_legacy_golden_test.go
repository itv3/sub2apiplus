package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// WS 帧定型的旧形态黄金。
//
// 画像没有基础指令载体节与 access_programs 值域节时（Active 槽位，或 previous 槽位装了不声明这两节的合成画像），
// WS 首帧、预热帧、链帧、增量帧与工具续接帧的出站字节必须与引入这两节之前完全相同。下面的表是在改动之前用
// 当时的代码实测记录的：每一步记归一化后帧字节的 SHA-256，出错则记错误文本。
//
// 归一化只抹掉每次运行都会变的三类内容：turn_started_at_unix_ms 与 x-codex-ws-stream-request-start-ms 取当前时间；
// UUIDv7 的 turn_id 带真实时间戳，同一进程内按种子缓存、跨进程每次不同。归一化把每个 UUIDv7 按首次出现的次序
// 换成固定占位，既抹掉时间戳，又保留“哪些位置是同一个 turn_id”的关系。

// wsLegacyOutcome 是一步 WS 定型的结果。
type wsLegacyOutcome struct {
	sha256 string
	err    string
}

var (
	wsLegacyStartedAtPattern = regexp.MustCompile(`(turn_started_at_unix_ms\\?":)\d+`)
	wsLegacyStreamStartedAt  = regexp.MustCompile(`("x-codex-ws-stream-request-start-ms":")\d+(")`)
	wsLegacyUUIDV7Pattern    = regexp.MustCompile(`[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}`)
)

// normalizeWSLegacyFrame 抹掉帧里每次运行都会变的内容，见本文件开头的说明。
func normalizeWSLegacyFrame(frame []byte) []byte {
	frame = wsLegacyStartedAtPattern.ReplaceAll(frame, []byte("${1}0"))
	frame = wsLegacyStreamStartedAt.ReplaceAll(frame, []byte("${1}0${2}"))
	seen := map[string]string{}
	return wsLegacyUUIDV7Pattern.ReplaceAllFunc(frame, func(match []byte) []byte {
		key := string(match)
		replaced, ok := seen[key]
		if !ok {
			replaced = fmt.Sprintf("00000000-0000-7000-8000-%012d", len(seen)+1)
			seen[key] = replaced
		}
		return []byte(replaced)
	})
}

func recordWSLegacy(out map[string]wsLegacyOutcome, key string, frame []byte, err error) {
	if err != nil {
		out[key] = wsLegacyOutcome{err: err.Error()}
		return
	}
	sum := sha256.Sum256(normalizeWSLegacyFrame(frame))
	out[key] = wsLegacyOutcome{sha256: hex.EncodeToString(sum[:])}
}

// wsLegacyVariant 是一种第三方首帧形态。instructions、input、access 都是 JSON 原文，空串表示不带该字段。
type wsLegacyVariant struct {
	name         string
	instructions string
	input        string
	access       string
}

func wsLegacyDerivedVariants() []wsLegacyVariant {
	user := `[` + baseInstructionsUserItem + `]`
	const helper = `"You are a helper."`
	return []wsLegacyVariant{
		{"plain", helper, user, ""},
		{"no_instructions", "", user, ""},
		{"dev_copy", helper, `[` + baseInstructionsCopyItem + `,` + baseInstructionsUserItem + `]`, ""},
		{"tagged", helper, `[` + baseInstructionsTaggedItem + `,` + baseInstructionsUserItem + `]`, ""},
		{"tools_first", helper, `[` + baseInstructionsToolsItem + `,` + baseInstructionsUserItem + `]`, ""},
		{"blank", `"   "`, user, ""},
		{"special", `"指令 a<b>&c \"q\"\n"`, user, ""},
		{"access_std", helper, user, `{"cyber":"standard"}`},
		{"access_red", helper, user, `{"cyber":"daybreak_red"}`},
		{"access_bogus", helper, user, `"oops"`},
	}
}

func buildWSLegacyDerivedPayload(variant wsLegacyVariant) []byte {
	instructions := ""
	if variant.instructions != "" {
		instructions = `"instructions":` + variant.instructions + `,`
	}
	access := ""
	if variant.access != "" {
		access = `,"access_programs":` + variant.access
	}
	return []byte(`{"type":"response.create","model":"gpt-5.6-luna",` + instructions +
		`"input":` + variant.input +
		`,"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]` + access + `}`)
}

func wsLegacyConfigForMode(mode string) *config.Config {
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = mode
	return cfg
}

// newWSLegacyDerivedContext 构造第三方入口的 WS 派生上下文，冻结在 mode 槽位。
func newWSLegacyDerivedContext(t *testing.T, mode string, lite bool, firstPayload []byte) context.Context {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.4.0")
	c.Request.Header.Set("X-Session-Affinity", "ws-legacy-golden-session")
	ctx, err := attachOfficialEgressWebSocketContext(
		withOpenAIResponsesLiteCapability(context.Background(), lite),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
		wsLegacyConfigForMode(mode),
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	require.NotNil(t, egressContext.openAIWSDerived, "第三方入口必须走派生路径")
	require.Equal(t, mode, egressContext.ProfileMode())
	return ctx
}

// newWSLegacyOfficialIngressContext 构造官方客户端形态入口的 WS 上下文，冻结在 mode 槽位。当前所有 WS 入口
// （含官方客户端直连）都登记派生身份，走同一条派生定型路径，所以这里同样要求派生状态存在。
func newWSLegacyOfficialIngressContext(t *testing.T, mode string, firstPayload []byte) context.Context {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	applyOfficialOpenAIWSIngressHeadersForTest(c, firstPayload)
	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
		wsLegacyConfigForMode(mode),
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	require.NotNil(t, egressContext.openAIWSDerived, "官方客户端形态入口同样走派生路径")
	require.Equal(t, mode, egressContext.ProfileMode())
	return ctx
}

// collectWSLegacyDerivedFlows 对一种首帧形态依次跑首帧、预热帧、链帧。
func collectWSLegacyDerivedFlows(
	t *testing.T, mode string, lite bool, variant wsLegacyVariant, out map[string]wsLegacyOutcome,
) {
	t.Helper()
	prefix := "derived|" + variant.name + "|lite=" + strconv.FormatBool(lite) + "|"
	payload := buildWSLegacyDerivedPayload(variant)
	ctx := newWSLegacyDerivedContext(t, mode, lite, payload)
	prepare := prepareOpenAIOfficialEgressSemanticWSFrame
	first, _, err := prepare(ctx, payload, payload, "", false)
	recordWSLegacy(out, prefix+"first", first, err)
	if err != nil {
		return
	}
	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	recordWSLegacy(out, prefix+"prewarm(inject="+strconv.FormatBool(inject)+")", prewarm, err)
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, "resp_golden_prewarm")
	recordWSLegacy(out, prefix+"chain", chain, err)
}

// collectWSLegacyDerivedIncrementalFlows 在同一连接上先发首帧，再发三种增量帧：重复同一段 instructions、
// 换一段 instructions、不带 instructions。
func collectWSLegacyDerivedIncrementalFlows(
	t *testing.T, mode string, lite bool, out map[string]wsLegacyOutcome,
) {
	t.Helper()
	prefix := "derived|plain|lite=" + strconv.FormatBool(lite) + "|"
	variant := wsLegacyDerivedVariants()[0]
	payload := buildWSLegacyDerivedPayload(variant)
	ctx := newWSLegacyDerivedContext(t, mode, lite, payload)
	prepare := prepareOpenAIOfficialEgressSemanticWSFrame
	_, _, err := prepare(ctx, payload, payload, "", false)
	require.NoError(t, err)
	incremental := func(instructions string) []byte {
		field := ""
		if instructions != "" {
			field = `"instructions":` + instructions + `,`
		}
		return []byte(`{"type":"response.create","model":"gpt-5.6-luna","previous_response_id":"resp_golden_1",` +
			field + `"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"again"}]}]}`)
	}
	for _, step := range []struct{ name, instructions string }{
		{"inc_same", `"You are a helper."`},
		{"inc_diff", `"Different instructions."`},
		{"inc_none", ""},
	} {
		frame := incremental(step.instructions)
		final, _, err := prepare(ctx, frame, frame, "resp_golden_1", false)
		recordWSLegacy(out, prefix+step.name, final, err)
	}
}

// collectWSLegacyDerivedToolFlows 复现完整历史的工具续接：整段历史的请求、含工具调用与输出的续接请求，
// 再把续接请求收敛为只含当前轮工具输出的最小帧。
func collectWSLegacyDerivedToolFlows(
	t *testing.T, mode string, lite bool, out map[string]wsLegacyOutcome,
) {
	t.Helper()
	prefix := "derived|tool|lite=" + strconv.FormatBool(lite) + "|"
	variant := wsLegacyDerivedVariants()[0]
	payload := buildWSLegacyDerivedPayload(variant)
	ctx := newWSLegacyDerivedContext(t, mode, lite, payload)
	prepare := prepareOpenAIOfficialEgressSemanticWSFrame
	_, _, err := prepare(ctx, payload, payload, "", false)
	require.NoError(t, err)

	toolRequest := []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[
		{"type":"additional_tools","tools":[]},
		{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件"}]}]}`)
	requestFinal, _, err := prepare(ctx, toolRequest, toolRequest, "resp_golden_before_tool", false)
	recordWSLegacy(out, prefix+"request", requestFinal, err)

	toolContinuation := []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[
		{"type":"additional_tools","tools":[]},
		{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件"}]},
		{"type":"reasoning","encrypted_content":"encrypted"},
		{"type":"function_call","call_id":"call_golden_1","name":"read","arguments":"{}"},
		{"type":"function_call_output","call_id":"call_golden_1","output":"ok"}]}`)
	continuationFinal, _, err := prepare(ctx, toolContinuation, toolContinuation, "resp_golden_tool_parent", false)
	recordWSLegacy(out, prefix+"continuation", continuationFinal, err)
	if err != nil {
		return
	}
	narrowed, changed, err := buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(
		ctx, continuationFinal, "resp_golden_tool_parent",
	)
	recordWSLegacy(out, prefix+"narrowed(changed="+strconv.FormatBool(changed)+")", narrowed, err)
}

// withWSLegacyAccessPrograms 给已编码的帧补上 access_programs（JSON 原文）；access 为空时原样返回。
func withWSLegacyAccessPrograms(t *testing.T, body []byte, access string) []byte {
	t.Helper()
	if access == "" {
		return body
	}
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	payload["access_programs"] = json.RawMessage(access)
	encoded, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return encoded
}

// collectWSLegacyOfficialIngressFlows 覆盖官方客户端形态的入口：预热帧与业务帧，各带几种 access_programs。
func collectWSLegacyOfficialIngressFlows(t *testing.T, mode string, out map[string]wsLegacyOutcome) {
	t.Helper()
	for _, access := range []struct{ name, value string }{
		{"none", ""},
		{"std", `{"cyber":"standard"}`},
		{"red", `{"cyber":"daybreak_red"}`},
		{"bogus", `"oops"`},
	} {
		prewarmFrame := withWSLegacyAccessPrograms(t, newOfficialOpenAIWSFirstFrameForTest(t), access.value)
		ctx := newWSLegacyOfficialIngressContext(t, mode, prewarmFrame)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, prewarmFrame, prewarmFrame, "", false)
		recordWSLegacy(out, "official|prewarm|access="+access.name, final, err)

		business := withWSLegacyAccessPrograms(t, withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
			"type":"response.create",
			"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}]
		}`)), access.value)
		final, _, err = prepareOpenAIOfficialEgressSemanticWSFrame(ctx, business, business, "", false)
		recordWSLegacy(out, "official|business|access="+access.name, final, err)
	}
}

// collectWSLegacyOutcomes 跑完整个矩阵，返回每一步的结果。
func collectWSLegacyOutcomes(t *testing.T, mode string) map[string]wsLegacyOutcome {
	t.Helper()
	out := map[string]wsLegacyOutcome{}
	for _, lite := range []bool{false, true} {
		for _, variant := range wsLegacyDerivedVariants() {
			collectWSLegacyDerivedFlows(t, mode, lite, variant, out)
		}
		collectWSLegacyDerivedIncrementalFlows(t, mode, lite, out)
		collectWSLegacyDerivedToolFlows(t, mode, lite, out)
	}
	collectWSLegacyOfficialIngressFlows(t, mode, out)
	return out
}

// wsLegacyGoldens 是改动之前用旧代码实测记录的黄金表（80 步）。键为“入口形态|变体|是否 Lite|步骤”。
// 非 Lite 的预热步骤对没有 developer 消息的首帧返回错误，那是旧逻辑本来的行为，同样固化在表里。
var wsLegacyGoldens = map[string]wsLegacyOutcome{
	"derived|access_bogus|lite=false|chain":                    {sha256: "d07211f5a0e5c2e4bd779abfdd23a896e419f10dcaea62c74aa3085e5ef6b6c4"},
	"derived|access_bogus|lite=false|first":                    {sha256: "e5c3a8f19b4582ffdbdff6057bbe3bdb2071503cf1af2c3b8cc290d32924b390"},
	"derived|access_bogus|lite=false|prewarm(inject=false)":    {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|access_bogus|lite=true|chain":                     {sha256: "4a624c0769191367d9e535282f0349c861c442736ff87398723ad4ef3650a67b"},
	"derived|access_bogus|lite=true|first":                     {sha256: "700091628284eda24c7d791cb3c783e1ac8896f22a5e03e32b24ef3cd867de6e"},
	"derived|access_bogus|lite=true|prewarm(inject=true)":      {sha256: "9b03206b29dad09ada223d38ddc36c78aa930c03c8f2482bb96d6a75488c538c"},
	"derived|access_red|lite=false|chain":                      {sha256: "d07211f5a0e5c2e4bd779abfdd23a896e419f10dcaea62c74aa3085e5ef6b6c4"},
	"derived|access_red|lite=false|first":                      {sha256: "e5c3a8f19b4582ffdbdff6057bbe3bdb2071503cf1af2c3b8cc290d32924b390"},
	"derived|access_red|lite=false|prewarm(inject=false)":      {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|access_red|lite=true|chain":                       {sha256: "4a624c0769191367d9e535282f0349c861c442736ff87398723ad4ef3650a67b"},
	"derived|access_red|lite=true|first":                       {sha256: "700091628284eda24c7d791cb3c783e1ac8896f22a5e03e32b24ef3cd867de6e"},
	"derived|access_red|lite=true|prewarm(inject=true)":        {sha256: "9b03206b29dad09ada223d38ddc36c78aa930c03c8f2482bb96d6a75488c538c"},
	"derived|access_std|lite=false|chain":                      {sha256: "d07211f5a0e5c2e4bd779abfdd23a896e419f10dcaea62c74aa3085e5ef6b6c4"},
	"derived|access_std|lite=false|first":                      {sha256: "e5c3a8f19b4582ffdbdff6057bbe3bdb2071503cf1af2c3b8cc290d32924b390"},
	"derived|access_std|lite=false|prewarm(inject=false)":      {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|access_std|lite=true|chain":                       {sha256: "4a624c0769191367d9e535282f0349c861c442736ff87398723ad4ef3650a67b"},
	"derived|access_std|lite=true|first":                       {sha256: "700091628284eda24c7d791cb3c783e1ac8896f22a5e03e32b24ef3cd867de6e"},
	"derived|access_std|lite=true|prewarm(inject=true)":        {sha256: "9b03206b29dad09ada223d38ddc36c78aa930c03c8f2482bb96d6a75488c538c"},
	"derived|blank|lite=false|chain":                           {sha256: "0760397cc6325f8be6e810ef472c38597f71cb6a9dba8986ce4727fd5b6ccef5"},
	"derived|blank|lite=false|first":                           {sha256: "b16193224e5464f0d6aacb915019cace87e53c99650d36452ba48575d9a0509d"},
	"derived|blank|lite=false|prewarm(inject=false)":           {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|blank|lite=true|chain":                            {sha256: "b7993c23ffa9318c87813458452067ac7fa8fcb03c126bf17a18152b8ff783cf"},
	"derived|blank|lite=true|first":                            {sha256: "5542af28c4bd203392ea40c9b85cd70937f1f7f293637ea103991b823f3ddf49"},
	"derived|blank|lite=true|prewarm(inject=true)":             {sha256: "455d33e470eae2d07634634d9cfa303e8c58ebc95a1c4358326cef66b3aa64fb"},
	"derived|dev_copy|lite=false|chain":                        {sha256: "17dd487addf1847884377529787013da62ef89965bbf79db72e024ad380c713b"},
	"derived|dev_copy|lite=false|first":                        {sha256: "7446b7b840a5d2ce7c7f1f9be4f024fb84ee513e3550405a19b61fa42c623320"},
	"derived|dev_copy|lite=false|prewarm(inject=true)":         {sha256: "79d5f80ac1758976e40aedaabd112aad6af3d76cbe37edc3ea5d303daf72c4f3"},
	"derived|dev_copy|lite=true|chain":                         {sha256: "9a943a37220b8d6a1eb7da65e887691e01b250a85975fce3b14ed6f8f99cdec1"},
	"derived|dev_copy|lite=true|first":                         {sha256: "4bac6d234ca4581f777899a255d220283acf7cd5c9cfbd6a1ab162d79633b841"},
	"derived|dev_copy|lite=true|prewarm(inject=true)":          {sha256: "b4b254c440e1c4bcccfa579303c85d34a23619ab0069b9a9a324ccf10296e66b"},
	"derived|no_instructions|lite=false|chain":                 {sha256: "0760397cc6325f8be6e810ef472c38597f71cb6a9dba8986ce4727fd5b6ccef5"},
	"derived|no_instructions|lite=false|first":                 {sha256: "b16193224e5464f0d6aacb915019cace87e53c99650d36452ba48575d9a0509d"},
	"derived|no_instructions|lite=false|prewarm(inject=false)": {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|no_instructions|lite=true|chain":                  {sha256: "b7993c23ffa9318c87813458452067ac7fa8fcb03c126bf17a18152b8ff783cf"},
	"derived|no_instructions|lite=true|first":                  {sha256: "5542af28c4bd203392ea40c9b85cd70937f1f7f293637ea103991b823f3ddf49"},
	"derived|no_instructions|lite=true|prewarm(inject=true)":   {sha256: "455d33e470eae2d07634634d9cfa303e8c58ebc95a1c4358326cef66b3aa64fb"},
	"derived|plain|lite=false|chain":                           {sha256: "d07211f5a0e5c2e4bd779abfdd23a896e419f10dcaea62c74aa3085e5ef6b6c4"},
	"derived|plain|lite=false|first":                           {sha256: "e5c3a8f19b4582ffdbdff6057bbe3bdb2071503cf1af2c3b8cc290d32924b390"},
	"derived|plain|lite=false|inc_diff":                        {sha256: "4aa9262c555f3336c038959f8f04d31e9cd3910855766e6529a3d080c605f14e"},
	"derived|plain|lite=false|inc_none":                        {sha256: "23180a764754d3ed616845cbc9c0951f0597e9f3b88c84e10434dd6b3c503fbb"},
	"derived|plain|lite=false|inc_same":                        {sha256: "cb01d3cc02f2a897f9548360303df7ad1d47827bb203328283aec684af9f6bd1"},
	"derived|plain|lite=false|prewarm(inject=false)":           {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|plain|lite=true|chain":                            {sha256: "4a624c0769191367d9e535282f0349c861c442736ff87398723ad4ef3650a67b"},
	"derived|plain|lite=true|first":                            {sha256: "700091628284eda24c7d791cb3c783e1ac8896f22a5e03e32b24ef3cd867de6e"},
	"derived|plain|lite=true|inc_diff":                         {sha256: "af18ddadd9da5d2a5c209e08d89e2730b7358adbc731066482d60ecd0739f965"},
	"derived|plain|lite=true|inc_none":                         {sha256: "4be41e1f38574c8c9f62691d3fed235241f0f1492579243f3c9b60515a16de03"},
	"derived|plain|lite=true|inc_same":                         {sha256: "c86306c2fb77dd14215142329359cb44ed608c98ba81f8877857e77ed6b7d80a"},
	"derived|plain|lite=true|prewarm(inject=true)":             {sha256: "9b03206b29dad09ada223d38ddc36c78aa930c03c8f2482bb96d6a75488c538c"},
	"derived|special|lite=false|chain":                         {sha256: "c843ec3ebc22e0d8a7230879ce95b4d20cb541dd501a4f4b9763a09673f901ef"},
	"derived|special|lite=false|first":                         {sha256: "58202d423639bdd41571bd83b9792f828a2d8b7a4c1d6b728e34021f8b467668"},
	"derived|special|lite=false|prewarm(inject=false)":         {err: "OpenAI official egress WebSocket prewarm requires developer context"},
	"derived|special|lite=true|chain":                          {sha256: "0b7cb78fc07303c479c402ff3a5b4b7d28ec16fd6acce415312c403cb29b0d75"},
	"derived|special|lite=true|first":                          {sha256: "0709cebcb65e91a09716c320be216ea409e8de93c43e88768c832acb41f8f48b"},
	"derived|special|lite=true|prewarm(inject=true)":           {sha256: "e2a665bb1770dc06f36bbd3fb378ed6efaad668776636b06b67de17d87efff6a"},
	"derived|tagged|lite=false|chain":                          {sha256: "0e76b203032f4ac5c773480e2ce1f1fe6275ba82b2ac3c627be736410490639c"},
	"derived|tagged|lite=false|first":                          {sha256: "e9997da29d96abff48f29b7a2ff3f6bd489a8698d0ee97372790950f2a5cd308"},
	"derived|tagged|lite=false|prewarm(inject=true)":           {sha256: "e44fe65dd652524352ef2313dec7ee0a4152ef892d92e4f5ace567ef32e8fc10"},
	"derived|tagged|lite=true|chain":                           {sha256: "7f46045834eef933e35a11006ff8b3b72c20e65270df38865cb7502080637b6b"},
	"derived|tagged|lite=true|first":                           {sha256: "10478dc2aa34b804b79d8c1cd3990ef88a02131e253996b7b0f2293d4403d598"},
	"derived|tagged|lite=true|prewarm(inject=true)":            {sha256: "454454269d7bed41f9a0a11fce5acb2849e67b0b3a0e4d2ccd6509d4c0062bbc"},
	"derived|tools_first|lite=false|chain":                     {sha256: "d07211f5a0e5c2e4bd779abfdd23a896e419f10dcaea62c74aa3085e5ef6b6c4"},
	"derived|tools_first|lite=false|first":                     {sha256: "4ae02ea53a60ad67026e21b5249c9cbdfc2d41850d50de2c3d17054a1ab54910"},
	"derived|tools_first|lite=false|prewarm(inject=true)":      {sha256: "451c86724123a512d880b07ae28222300704e4901c7a6d67c52424b6ad65e8f3"},
	"derived|tools_first|lite=true|chain":                      {sha256: "9a943a37220b8d6a1eb7da65e887691e01b250a85975fce3b14ed6f8f99cdec1"},
	"derived|tools_first|lite=true|first":                      {sha256: "4720401524ff1183b0bc7f3c1546135741bb2320caac57586ebeb0638e3c8426"},
	"derived|tools_first|lite=true|prewarm(inject=true)":       {sha256: "34629a5a640b229584c972528e81dc71e08bb12e3f98fb1c81f8a499501d4b66"},
	"derived|tool|lite=false|continuation":                     {sha256: "da6c8682b432a7aae4991a6f2b131ce0290d661d430675294fafd35f53fc0a44"},
	"derived|tool|lite=false|narrowed(changed=true)":           {sha256: "647d526d987113d765c9a22a9cb7bb5adbfb3feed021796186bf5dc67c2c5ceb"},
	"derived|tool|lite=false|request":                          {sha256: "8100c1b867622ad13a901e619678ca0bf582603562b04ee890da08c059c9fe30"},
	"derived|tool|lite=true|continuation":                      {sha256: "4cb3355dd9a22c6ccdf70fb85bfdd6f8d5b5ddd304ee1f56b5e747052a9437b3"},
	"derived|tool|lite=true|narrowed(changed=true)":            {sha256: "3276c6e709d9c01a3310f6a73961aaabab83506f45e66d3662d081249bdbf8b2"},
	"derived|tool|lite=true|request":                           {sha256: "6d7556db176be2214ef4a75331f55fb4ff9745302c39abd478d1c141a172c8e4"},
	"official|business|access=bogus":                           {sha256: "2f74dd466832d70540d36d11aa92ae34362b483bf0891ed3d516ed14373caa6e"},
	"official|business|access=none":                            {sha256: "2f74dd466832d70540d36d11aa92ae34362b483bf0891ed3d516ed14373caa6e"},
	"official|business|access=red":                             {sha256: "2f74dd466832d70540d36d11aa92ae34362b483bf0891ed3d516ed14373caa6e"},
	"official|business|access=std":                             {sha256: "2f74dd466832d70540d36d11aa92ae34362b483bf0891ed3d516ed14373caa6e"},
	"official|prewarm|access=bogus":                            {sha256: "5c25a9ab7bcb22596059c8fb13aa27977f5d1cd575ab95221f7f24dec70de047"},
	"official|prewarm|access=none":                             {sha256: "5c25a9ab7bcb22596059c8fb13aa27977f5d1cd575ab95221f7f24dec70de047"},
	"official|prewarm|access=red":                              {sha256: "5c25a9ab7bcb22596059c8fb13aa27977f5d1cd575ab95221f7f24dec70de047"},
	"official|prewarm|access=std":                              {sha256: "5c25a9ab7bcb22596059c8fb13aa27977f5d1cd575ab95221f7f24dec70de047"},
}

func requireWSLegacyOutcomes(t *testing.T, got map[string]wsLegacyOutcome) {
	t.Helper()
	require.Len(t, got, len(wsLegacyGoldens))
	for key, want := range wsLegacyGoldens {
		actual, ok := got[key]
		require.True(t, ok, "缺少 %s", key)
		require.Equal(t, want, actual, "%s：节缺省时 WS 出站字节漂移", key)
	}
}

func TestOpenAIWSFramesKeepLegacyOutputWhenSectionsAbsent(t *testing.T) {
	require.Len(t, wsLegacyGoldens, 80)
	// 黄金值固定的是“节缺省时”的出站字节，所以取正式目录里不声明这两个节的旧画像槽位：晋升前是 active，
	// 晋升后 active 已是声明它们的较新画像、旧画像退居 previous。
	legacyMode := requireOfficialCodexLegacyBaseProfileMode(t)
	sections := officialCodexOptionalSectionsForMode(legacyMode)
	require.Nil(t, sections.BaseInstructions, "底稿槽位必须不声明 BaseInstructions 节")
	require.Nil(t, sections.AccessPrograms, "底稿槽位必须不声明 AccessPrograms 节")
	requireWSLegacyOutcomes(t, collectWSLegacyOutcomes(t, legacyMode))
}

// previous 槽位装了合成画像，但合成画像不声明基础指令载体节与 access_programs 值域节：门控按调用冻结的 mode
// 解析各自画像的节，没有节就走旧逻辑，结果与 Active 槽位逐项相同。
func TestOpenAIWSFramesKeepLegacyOutputOnPreviousSlotWithoutSections(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, nil)
	require.Nil(t, synthetic.Optional().BaseInstructions)
	require.Nil(t, synthetic.Optional().AccessPrograms)
	requireWSLegacyOutcomes(t, collectWSLegacyOutcomes(t, officialClientProfileModePrevious))
}
