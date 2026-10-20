package service

import (
	"bytes"
	"encoding/json"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 基础指令载体（画像 BaseInstructions 节）的单元与矩阵测试：
//  1. 官方形态消息的渲染：id 公式与键序用独立实现预算的黄金值固定，字符串转义对照标准编码器逐字符核对；
//  2. 识别、去重、安装位置的表驱动用例；
//  3. HTTP 定型矩阵：11 种 instructions／input 形态 × 入口契约与兼容层生成契约 × Lite 与非 Lite，
//     在 previous 槽位装合成画像时顶层 instructions 消失、官方形态消息出现在 additional_tools 之后；
//  4. 入站官方客户端自带的基础指令：契约捕获阶段只读记录，定型阶段以网关线程重算 id 重建；
//  5. 大 input 上的分配上界：安装前缀不能把整份 input 解码一遍。
// 节缺省的画像（Active）出站字节不变，见 official_egress_openai_base_instructions_legacy_test.go。

const baseInstructionsTestThreadID = "019a0000-0000-7000-8000-000000000001"

func baseInstructionsTestSection() *profilecontract.BaseInstructionsSection {
	return &profilecontract.BaseInstructionsSection{
		Carrier:         "input_developer_message",
		ContentItemKind: "model.base_instructions",
		EndpointIDs:     []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS},
		IDPrefix:        "msg",
	}
}

// baseInstructionsRenderGoldens 的期望值由独立实现（Python uuid.uuid5 与 json.dumps）预算，不依赖被测代码。

var baseInstructionsRenderGoldens = []struct{ name, text, want string }{
	{
		name: "plain",
		text: "You are a helper.",
		want: "{\"type\":\"message\",\"id\":\"msg_f91e0038-7b28-59f1-a5de-6ed5292de87b\",\"role\":\"developer\",\"content\":[{\"type\":\"input_text\",\"text\":\"You are a helper.\"}],\"internal_chat_message_metadata_passthrough\":{\"content_item_kinds\":[\"model.base_instructions\"]}}",
	},
	{
		name: "special",
		text: "Rules:\n\t- a<b> && c>d\n- \"quoted\" \\ backslash\r\n- ctrl:\x01\x1f\x08\x0c\x7f\n- sep:\u2028\u2029\n- 中文😀𝒜\n",
		want: "{\"type\":\"message\",\"id\":\"msg_49a65eac-df24-5df7-a2cb-7836d40a4942\",\"role\":\"developer\",\"content\":[{\"type\":\"input_text\",\"text\":\"Rules:\\n\\t- a<b> && c>d\\n- \\\"quoted\\\" \\\\ backslash\\r\\n- ctrl:\\u0001\\u001f\\b\\f\x7f\\n- sep:\u2028\u2029\\n- 中文😀𝒜\\n\"}],\"internal_chat_message_metadata_passthrough\":{\"content_item_kinds\":[\"model.base_instructions\"]}}",
	},
	{
		name: "trailing_newlines",
		text: "Line one.\nLine two.\n\n",
		want: "{\"type\":\"message\",\"id\":\"msg_361586df-ec00-5d3a-a0eb-46b5cced451c\",\"role\":\"developer\",\"content\":[{\"type\":\"input_text\",\"text\":\"Line one.\\nLine two.\\n\\n\"}],\"internal_chat_message_metadata_passthrough\":{\"content_item_kinds\":[\"model.base_instructions\"]}}",
	},
}

func TestRenderOfficialCodexBaseInstructionsItemMatchesIndependentGoldens(t *testing.T) {
	section := baseInstructionsTestSection()
	for _, golden := range baseInstructionsRenderGoldens {
		t.Run(golden.name, func(t *testing.T) {
			rendered, err := renderOfficialCodexBaseInstructionsItem(section, baseInstructionsTestThreadID, golden.text)
			require.NoError(t, err)
			require.Equal(t, golden.want, string(rendered))
			require.True(t, json.Valid(rendered))
			// 键序固定：type、id、role、content、internal_chat_message_metadata_passthrough。
			require.Equal(t,
				[]string{"type", "id", "role", "content", "internal_chat_message_metadata_passthrough"},
				jsonTopLevelKeys(t, string(rendered)))
		})
	}
	// 线程不同、正文不同，id 都不同；同线程同正文 id 稳定。
	first := officialCodexBaseInstructionsID(section, baseInstructionsTestThreadID, "text")
	require.Equal(t, first, officialCodexBaseInstructionsID(section, baseInstructionsTestThreadID, "text"))
	require.NotEqual(t, first, officialCodexBaseInstructionsID(section, baseInstructionsTestThreadID, "text "))
	require.NotEqual(t, first, officialCodexBaseInstructionsID(section, baseInstructionsTestThreadID+"x", "text"))
	require.True(t, strings.HasPrefix(first, "msg_"))
	// 没有线程就算不出稳定 id：失败关闭。
	_, err := renderOfficialCodexBaseInstructionsItem(section, "  ", "text")
	require.Error(t, err)
}

func TestAppendOfficialCodexJSONStringMatchesStandardEncoder(t *testing.T) {
	reference := func(value string) string {
		var buffer bytes.Buffer
		encoder := json.NewEncoder(&buffer)
		encoder.SetEscapeHTML(false)
		require.NoError(t, encoder.Encode(value))
		encoded := strings.TrimSuffix(buffer.String(), "\n")
		// 官方序列化库原样输出 U+2028、U+2029；标准编码器在某些版本里会把它们转义。
		encoded = strings.ReplaceAll(encoded, `\u2028`, "\u2028")
		return strings.ReplaceAll(encoded, `\u2029`, "\u2029")
	}
	check := func(value string) {
		t.Helper()
		require.Equal(t, reference(value), string(appendOfficialCodexJSONString(nil, value)), "%q", value)
	}
	for r := rune(0); r < 0x3000; r++ {
		check(string(r))
	}
	for r := rune(0xe000); r < 0x10000; r++ {
		check(string(r))
	}
	for r := rune(0x10000); r < 0x11000; r++ {
		check(string(r))
	}
	check("")
	check("混合 \"quoted\" \\ <tag> & \U0001F600 \x00\x1f\x7f\u2028\u2029 end\r\n")
	// 非法 UTF-8 逐字节替换为 U+FFFD，不会产出非法 JSON。
	require.Equal(t, "\"a\uFFFDb\"", string(appendOfficialCodexJSONString(nil, "a\xffb")))
	require.Equal(t, "\"\uFFFD\uFFFD\"", string(appendOfficialCodexJSONString(nil, "\xe2\x80")))
}

func TestIsOfficialCodexBaseInstructionsItemRecognizesTaggedDeveloperMessages(t *testing.T) {
	section := baseInstructionsTestSection()
	tagged := func(role, passthrough string) string {
		return `{"type":"message","role":"` + role + `","content":[{"type":"input_text","text":"x"}]` + passthrough + `}`
	}
	kinds := func(value string) string {
		return `,"internal_chat_message_metadata_passthrough":{"content_item_kinds":` + value + `}`
	}
	cases := []struct {
		name string
		raw  string
		want bool
	}{
		{"带标记的 developer 消息", tagged("developer", kinds(`["model.base_instructions"]`)), true},
		{"标记集合含多个值", tagged("developer", kinds(`["model.other","model.base_instructions"]`)), true},
		{"其他标记", tagged("developer", kinds(`["model.other"]`)), false},
		{"没有 passthrough", tagged("developer", ""), false},
		{"passthrough 没有 content_item_kinds", tagged("developer", `,"internal_chat_message_metadata_passthrough":{"turn_id":"t"}`), false},
		{"content_item_kinds 不是数组", tagged("developer", kinds(`"model.base_instructions"`)), false},
		{"content_item_kinds 里不是字符串", tagged("developer", kinds(`[1,null]`)), false},
		{"role 是 user", tagged("user", kinds(`["model.base_instructions"]`)), false},
		{"role 是 system", tagged("system", kinds(`["model.base_instructions"]`)), false},
		{"不是对象", `"text"`, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.Equal(t, item.want, isOfficialCodexBaseInstructionsItem(section, json.RawMessage(item.raw)), "预编码字节形态")
			var decoded any
			require.NoError(t, json.Unmarshal([]byte(item.raw), &decoded))
			require.Equal(t, item.want, isOfficialCodexBaseInstructionsItem(section, decoded), "已解码对象形态")
		})
	}
}

// summarizeBaseInstructionsInput 把 payload["input"] 概括成序列：预编码字节记为 BASE，其余记 type:role。
func summarizeBaseInstructionsInput(t *testing.T, payload map[string]any) []string {
	t.Helper()
	items, ok := payload["input"].([]any)
	require.True(t, ok, "input 应当是数组")
	out := make([]string, 0, len(items))
	for _, item := range items {
		switch typed := item.(type) {
		case json.RawMessage:
			require.True(t, json.Valid(typed))
			out = append(out, "BASE")
		case map[string]any:
			out = append(out, officialOpenAIString(typed, "type")+":"+officialOpenAIString(typed, "role"))
		default:
			out = append(out, "?")
		}
	}
	return out
}

func TestInstallOfficialCodexBaseInstructionsDedupesAndPlacesAfterLeadingTools(t *testing.T) {
	section := baseInstructionsTestSection()
	const text = "You are a helper."
	user := `{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}`
	tools := `{"type":"additional_tools","tools":[]}`
	devItem := func(role, body string) string {
		return `{"type":"message","role":"` + role + `","content":[{"type":"input_text","text":"` + body + `"}]}`
	}
	tagged := func(body string) string {
		return `{"type":"message","id":"msg_stale","role":"developer","content":[{"type":"input_text","text":"` + body + `"}],` +
			`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	}
	cases := []struct {
		name  string
		input string // JSON 数组；空串表示缺省，另有两个特殊值见下
		want  []string
	}{
		{"input 缺省", "", []string{"BASE"}},
		{"input 为空数组", `[]`, []string{"BASE"}},
		{"input 为字符串", `"hi"`, []string{"BASE", "message:user"}},
		{"只有用户消息", `[` + user + `]`, []string{"BASE", "message:user"}},
		{"前导 additional_tools", `[` + tools + `,` + user + `]`, []string{"additional_tools:", "BASE", "message:user"}},
		{"多个前导 additional_tools", `[` + tools + `,` + tools + `,` + user + `]`, []string{"additional_tools:", "additional_tools:", "BASE", "message:user"}},
		{"前导同文本 developer 副本", `[` + devItem("developer", text) + `,` + user + `]`, []string{"BASE", "message:user"}},
		{"additional_tools 之后的副本", `[` + tools + `,` + devItem("developer", text) + `,` + user + `]`, []string{"additional_tools:", "BASE", "message:user"}},
		{"副本在 additional_tools 之前", `[` + devItem("developer", text) + `,` + tools + `,` + user + `]`, []string{"additional_tools:", "BASE", "message:user"}},
		{"同文本 system 副本", `[` + devItem("system", text) + `,` + user + `]`, []string{"BASE", "message:user"}},
		{"两个副本都去掉", `[` + devItem("developer", text) + `,` + devItem("system", text) + `,` + user + `]`, []string{"BASE", "message:user"}},
		{"带标记的旧基础指令按标记去掉", `[` + tagged(text) + `,` + user + `]`, []string{"BASE", "message:user"}},
		{"带标记但文本不同的旧项也去掉", `[` + tagged("older text") + `,` + user + `]`, []string{"BASE", "message:user"}},
		{"文本不同的 developer 消息保留", `[` + devItem("developer", "Something else.") + `,` + user + `]`, []string{"BASE", "message:developer", "message:user"}},
		{"用户消息之后的同文本副本保留", `[` + user + `,` + devItem("developer", text) + `]`, []string{"BASE", "message:user", "message:developer"}},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			payload := map[string]any{"model": "m"}
			switch {
			case item.input == "":
			case strings.HasPrefix(item.input, `"`):
				var value string
				require.NoError(t, json.Unmarshal([]byte(item.input), &value))
				payload["input"] = value
			default:
				var items []any
				require.NoError(t, json.Unmarshal([]byte(item.input), &items))
				payload["input"] = items
			}
			require.NoError(t, installOfficialCodexBaseInstructions(payload, section, baseInstructionsTestThreadID, text))
			require.Equal(t, item.want, summarizeBaseInstructionsInput(t, payload))
			// 安装的就是官方形态消息，字节与渲染结果逐字节相同。
			rendered, err := renderOfficialCodexBaseInstructionsItem(section, baseInstructionsTestThreadID, text)
			require.NoError(t, err)
			installed, isArray := payload["input"].([]any)
			require.True(t, isArray)
			for _, entry := range installed {
				if raw, ok := entry.(json.RawMessage); ok {
					require.Equal(t, string(rendered), string(raw))
				}
			}
		})
	}

	t.Run("空白指令不生成消息也不动 input", func(t *testing.T) {
		payload := map[string]any{"input": []any{map[string]any{"type": "message", "role": "user"}}}
		require.NoError(t, installOfficialCodexBaseInstructions(payload, section, baseInstructionsTestThreadID, " \n\t "))
		require.Equal(t, []string{"message:user"}, summarizeBaseInstructionsInput(t, payload))
	})
	t.Run("input 类型非法失败关闭", func(t *testing.T) {
		payload := map[string]any{"input": 7}
		require.Error(t, installOfficialCodexBaseInstructions(payload, section, baseInstructionsTestThreadID, text))
	})
	t.Run("线程缺失失败关闭", func(t *testing.T) {
		payload := map[string]any{"input": []any{}}
		require.Error(t, installOfficialCodexBaseInstructions(payload, section, "", text))
	})
	t.Run("安装不改动原切片", func(t *testing.T) {
		original := []any{map[string]any{"type": "message", "role": "developer", "content": text}, map[string]any{"type": "message", "role": "user"}}
		payload := map[string]any{"input": original}
		require.NoError(t, installOfficialCodexBaseInstructions(payload, section, baseInstructionsTestThreadID, text))
		require.Len(t, original, 2)
		first, isMap := original[0].(map[string]any)
		require.True(t, isMap)
		require.Equal(t, "developer", officialOpenAIString(first, "role"))
	})
}

// baseInstructionsExpectation 是 HTTP 矩阵里一种形态的期望：input 序列与基础指令正文。
type baseInstructionsExpectation struct {
	sequence []string
	// text 为空表示不应有基础指令消息。
	text string
	// entryError 为真表示入口契约下应当失败关闭（顶层 instructions 不是字符串）；兼容层生成契约没有来源，
	// 顶层残留直接丢弃，期望序列是 generatedSequence。
	entryError        bool
	generatedSequence []string
}

func baseInstructionsAllVariants() []baseInstructionsHTTPVariant {
	otherDeveloper := `{"type":"message","role":"developer","content":[{"type":"input_text","text":"Something else."}]}`
	return append(baseInstructionsLegacyVariants(),
		baseInstructionsHTTPVariant{"dev_other_text", `"You are a helper."`, `[` + otherDeveloper + `,` + baseInstructionsUserItem + `]`},
		baseInstructionsHTTPVariant{"tagged_old", `"You are a helper."`, `[` + baseInstructionsTaggedItem + `,` + baseInstructionsUserItem + `]`},
	)
}

func baseInstructionsExpectations() map[string]baseInstructionsExpectation {
	base := []string{"BASE", "message:user"}
	toolsThenBase := []string{"additional_tools:", "BASE", "message:user"}
	userOnly := []string{"message:user"}
	const helper = "You are a helper."
	return map[string]baseInstructionsExpectation{
		"plain_eol":            {sequence: base, text: "You are a helper.\n"},
		"plain":                {sequence: base, text: helper},
		"tools_first":          {sequence: toolsThenBase, text: helper},
		"dev_copy":             {sequence: base, text: helper},
		"dev_copy_after_tools": {sequence: toolsThenBase, text: helper},
		"dev_copy_not_first":   {sequence: []string{"BASE", "message:user", "message:developer"}, text: helper},
		"blank":                {sequence: userOnly},
		"absent":               {sequence: userOnly},
		"input_string":         {sequence: base, text: helper},
		"special_chars":        {sequence: base, text: "a<b>&c \u2028 d \x01 \U0001F600 \"q\" \\"},
		"non_string":           {entryError: true, generatedSequence: userOnly},
		"dev_other_text":       {sequence: []string{"BASE", "message:developer", "message:user"}, text: helper},
		"tagged_old":           {sequence: base, text: helper},
	}
}

func describeBaseInstructionsInput(items []gjson.Result) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		if item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() {
			out = append(out, "BASE")
			continue
		}
		out = append(out, item.Get("type").String()+":"+item.Get("role").String())
	}
	return out
}

// resetOfficialOpenAIProjectedFieldsSeenForTest 清空投影告警的去重表并在用例结束时恢复，使用例能断言
// 本次定型是否触发过“画像闭集外的顶层字段”告警。
func resetOfficialOpenAIProjectedFieldsSeenForTest(t *testing.T) {
	t.Helper()
	officialOpenAIProjectedFieldsMu.Lock()
	saved := officialOpenAIProjectedFieldsSeen
	officialOpenAIProjectedFieldsSeen = map[string]struct{}{}
	officialOpenAIProjectedFieldsMu.Unlock()
	t.Cleanup(func() {
		officialOpenAIProjectedFieldsMu.Lock()
		officialOpenAIProjectedFieldsSeen = saved
		officialOpenAIProjectedFieldsMu.Unlock()
	})
}

func projectedFieldWarningsMentioning(name string) []string {
	officialOpenAIProjectedFieldsMu.Lock()
	defer officialOpenAIProjectedFieldsMu.Unlock()
	var matches []string
	for key := range officialOpenAIProjectedFieldsSeen {
		if strings.Contains(key, name) {
			matches = append(matches, key)
		}
	}
	return matches
}

func TestFinalizeOfficialOpenAIHTTPBodyInstallsOfficialBaseInstructionsOnTargetSlot(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	resetOfficialOpenAIProjectedFieldsSeenForTest(t)
	section := synthetic.Optional().BaseInstructions
	require.NotNil(t, section)
	expectations := baseInstructionsExpectations()
	variants := baseInstructionsAllVariants()
	require.Len(t, expectations, len(variants))
	for _, variant := range variants {
		want, ok := expectations[variant.name]
		require.True(t, ok, variant.name)
		for _, contractKind := range []string{"entry", "generated"} {
			for _, lite := range []bool{false, true} {
				label := variant.name + "/" + contractKind + "/lite=" + strconv.FormatBool(lite)
				final, _, identity, err := finalizeBaseInstructionsHTTP(
					t, officialClientProfileModePrevious, lite, contractKind, buildBaseInstructionsHTTPBody(variant))
				wantSequence := want.sequence
				if want.entryError {
					if contractKind == "entry" {
						require.EqualError(t, err, "OpenAI official egress third-party instructions must be a string", label)
						continue
					}
					wantSequence = want.generatedSequence
				}
				require.NoError(t, err, label)
				require.False(t, gjson.GetBytes(final, "instructions").Exists(), "%s：画像声明了载体就不能再发顶层 instructions", label)
				items := gjson.GetBytes(final, "input").Array()
				require.Equal(t, wantSequence, describeBaseInstructionsInput(items), label)
				if want.text == "" {
					continue
				}
				rendered, renderErr := renderOfficialCodexBaseInstructionsItem(section, identity.threadID, want.text)
				require.NoError(t, renderErr)
				found := 0
				for _, item := range items {
					if item.Get("internal_chat_message_metadata_passthrough").Exists() {
						found++
						require.Equal(t, string(rendered), item.Raw, "%s：消息必须是官方形态的预编码字节", label)
					}
				}
				require.Equal(t, 1, found, label)
			}
		}
	}
	// 载体分支自己删除顶层 instructions，不能留给闭集剔除：那会被当成“画像落后于真实客户端”的告警信号。
	require.Empty(t, projectedFieldWarningsMentioning("instructions"))
}

func TestFinalizeOfficialOpenAIHTTPBodyEmitsProfileFieldOrderWithBaseInstructions(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	body := buildBaseInstructionsHTTPBody(baseInstructionsHTTPVariant{
		"plain", `"You are a helper."`, `[` + baseInstructionsUserItem + `]`,
	})
	final, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, false, "entry", body)
	require.NoError(t, err)
	// 合成画像的字段序：stream 前置，没有 instructions；载荷里不存在的字段不输出。
	require.Equal(t,
		[]string{"model", "stream", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "include"},
		jsonTopLevelKeys(t, string(final)))
}

// 候选画像与生产画像交替构造请求互不影响：先后各一次，previous 的载体形态不能泄漏到 active，反之亦然。
func TestFinalizeOfficialOpenAIHTTPBodyAlternatingSlotsDoNotInfluenceEachOther(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	body := buildBaseInstructionsHTTPBody(baseInstructionsHTTPVariant{
		"plain", `"You are a helper."`, `[` + baseInstructionsToolsItem + `,` + baseInstructionsUserItem + `]`,
	})
	activeFirst, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModeActive, true, "entry", body)
	require.NoError(t, err)
	for round := 0; round < 3; round++ {
		previousFinal, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, true, "entry", body)
		require.NoError(t, err)
		require.False(t, gjson.GetBytes(previousFinal, "instructions").Exists())
		require.True(t, gjson.GetBytes(previousFinal, "input.1.internal_chat_message_metadata_passthrough").Exists())

		activeFinal, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModeActive, true, "entry", body)
		require.NoError(t, err)
		require.Equal(t, string(activeFirst), string(activeFinal), "候选请求不能改变随后生产请求的字节")
		require.False(t, gjson.GetBytes(activeFinal, "input.1.internal_chat_message_metadata_passthrough").Exists())
		require.False(t, gjson.GetBytes(activeFinal, "input.1.id").Exists(), "旧画像的 Lite 迁入形态没有 id")
	}
}

// 入站官方客户端的基础指令：标记在入站规范化时被剥掉，契约捕获阶段先记下；定型阶段按画像节以网关线程重算 id 重建。
func TestFinalizeOfficialOpenAIHTTPBodyRebuildsInboundOfficialBaseInstructions(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	section := synthetic.Optional().BaseInstructions
	const text = "Official base instructions.\nSecond line.\n"
	tagged := `{"type":"message","id":"msg_from_client","role":"developer","content":[{"type":"input_text","text":"Official base instructions.\nSecond line.\n"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	buildBody := func(first string) []byte {
		return []byte(`{"model":"gpt-5.6-luna","input":[` + first + `,` + baseInstructionsUserItem + `],"tool_choice":"auto",` +
			`"reasoning":{"effort":"medium"},"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671",` +
			`"turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
	}
	inbound := buildBody(tagged)
	contract, err := captureOfficialOpenAIHTTPBodyContract(inbound)
	require.NoError(t, err)
	recorded, found := contract.inboundTaggedMessageText(section.ContentItemKind)
	require.True(t, found)
	require.Equal(t, text, recorded, "正文要原样记录，不去首尾空白（id 依赖原文）")

	// 入站规范化只剥掉 passthrough，id 仍在。
	normalized := buildBody(strings.Replace(tagged,
		`,"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}`, "", 1))
	require.NotContains(t, string(normalized), "internal_chat_message_metadata_passthrough")

	for _, lite := range []bool{false, true} {
		identity, err := deriveOfficialOpenAIHTTPIdentity(nil, newOfficialOpenAIHTTPTestAccount(94101), normalized, contract, officialClientProfileModePrevious)
		require.NoError(t, err)
		target, _, err := finalizeOfficialOpenAIHTTPBody(normalized, contract, identity, officialOpenAIReasoningDefaults{},
			officialOpenAIHTTPBodyOptions{ProfileMode: officialClientProfileModePrevious, UseResponsesLite: lite, SupportsParallelTools: true, ThreadID: identity.threadID})
		require.NoError(t, err)
		items := gjson.GetBytes(target, "input").Array()
		require.Equal(t, []string{"BASE", "message:user"}, describeBaseInstructionsInput(items))
		rendered, err := renderOfficialCodexBaseInstructionsItem(section, identity.threadID, text)
		require.NoError(t, err)
		require.Equal(t, string(rendered), items[0].Raw)
		require.NotEqual(t, "msg_from_client", items[0].Get("id").String(), "入站 id 来自客户端自己的线程，不能沿用")

		// 节缺省（Active）：入站的这一项原样保留，不重建。
		legacyContract, err := captureOfficialOpenAIHTTPBodyContract(inbound)
		require.NoError(t, err)
		activeIdentity, err := deriveOfficialOpenAIHTTPIdentity(nil, newOfficialOpenAIHTTPTestAccount(94101), normalized, legacyContract, officialClientProfileModeActive)
		require.NoError(t, err)
		legacy, _, err := finalizeOfficialOpenAIHTTPBody(normalized, legacyContract, activeIdentity, officialOpenAIReasoningDefaults{},
			officialOpenAIHTTPBodyOptions{ProfileMode: officialClientProfileModeActive, UseResponsesLite: lite, SupportsParallelTools: true, ThreadID: activeIdentity.threadID})
		require.NoError(t, err)
		legacyItems := gjson.GetBytes(legacy, "input").Array()
		require.Equal(t, "msg_from_client", legacyItems[0].Get("id").String())
		require.False(t, legacyItems[0].Get("internal_chat_message_metadata_passthrough").Exists())
		require.False(t, gjson.GetBytes(legacy, "instructions").Exists())
	}
}

func TestCaptureOfficialOpenAIHTTPBodyContractRecordsInboundTaggedMessages(t *testing.T) {
	tagged := func(role, kinds, text string) string {
		return `{"type":"message","role":"` + role + `","content":[{"type":"input_text","text":"` + text + `"}],` +
			`"internal_chat_message_metadata_passthrough":{"content_item_kinds":` + kinds + `}}`
	}
	plainUser := baseInstructionsUserItem
	bodyOf := func(items ...string) []byte {
		return []byte(`{"model":"m","input":[` + strings.Join(items, ",") + `]}`)
	}
	record := func(items ...string) []officialOpenAIHTTPTaggedMessage {
		contract, err := captureOfficialOpenAIHTTPBodyContract(bodyOf(items...))
		require.NoError(t, err)
		return contract.inboundTaggedMessages
	}

	require.Empty(t, record(plainUser), "没有标记的请求零记录")
	require.Empty(t, record(tagged("user", `["model.base_instructions"]`, "x")), "只记录 developer 消息")
	require.Empty(t, record(tagged("developer", `[]`, "x")), "标记集合为空不记录")
	require.Empty(t, record(tagged("developer", `"model.base_instructions"`, "x")), "标记不是数组不记录")

	messages := record(tagged("developer", `["model.base_instructions","model.other"]`, "first"), plainUser)
	require.Len(t, messages, 1)
	require.Equal(t, []string{"model.base_instructions", "model.other"}, messages[0].kinds)
	require.Equal(t, "first", messages[0].text)

	// 只看 input 的前几项：官方 prefix 的位置固定，更靠后的带标记项不被记录。
	within := record(plainUser, plainUser, tagged("developer", `["model.base_instructions"]`, "third"))
	require.Len(t, within, 1)
	require.Equal(t, "third", within[0].text)
	require.Empty(t, record(plainUser, plainUser, plainUser, tagged("developer", `["model.base_instructions"]`, "fourth")))

	// 多个文本片段按序以换行连接，不去首尾空白。
	multi := `{"type":"message","role":"developer","content":[{"type":"input_text","text":" a "},{"type":"input_text","text":"b"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	multiMessages := record(multi)
	require.Len(t, multiMessages, 1)
	require.Equal(t, " a \nb", multiMessages[0].text)

	// 记录是只读的：契约捕获不改变其他契约字段。
	contract, err := captureOfficialOpenAIHTTPBodyContract(bodyOf(tagged("developer", `["model.base_instructions"]`, "x"), plainUser))
	require.NoError(t, err)
	require.False(t, contract.instructionsPresent)
	text, found := contract.inboundTaggedMessageText("model.base_instructions")
	require.True(t, found)
	require.Equal(t, "x", text)
	_, found = contract.inboundTaggedMessageText("model.unknown")
	require.False(t, found)
}

// baseInstructionsLargeBody 构造带顶层 instructions 的大请求体：每项一条文本消息，填充用不含转义的字符，
// 共享解码才能引用正文而不是复制字符串（与既有大正文夹具同一思路）。
func baseInstructionsLargeBody(t *testing.T, items, textBytes int) []byte {
	t.Helper()
	filler := strings.Repeat("QUJDREVGR0g=", textBytes/12+1)[:textBytes]
	input := make([]any, 0, items)
	for index := 0; index < items; index++ {
		role, partType := "user", "input_text"
		if index%2 == 1 {
			role, partType = "assistant", "output_text"
		}
		input = append(input, map[string]any{
			"type": "message", "role": role,
			"content": []any{map[string]any{"type": partType, "text": filler + strconv.Itoa(index)}},
		})
	}
	body, err := marshalOpenAIUpstreamJSON(map[string]any{
		"model": "gpt-5.4", "instructions": "You are a helper.", "input": input,
	})
	require.NoError(t, err)
	return body
}

// 大 input 上安装基础指令前缀：分配上界按正文体积的一小部分计，不能把整份 input 解码一遍。
func TestInstallOfficialCodexBaseInstructionsDoesNotDecodeLargeInput(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	body := baseInstructionsLargeBody(t, 64, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(nil, newOfficialOpenAIHTTPTestAccount(94100), body, contract, officialClientProfileModePrevious)
	require.NoError(t, err)

	for _, lite := range []bool{false, true} {
		options := officialOpenAIHTTPBodyOptions{
			ProfileMode: officialClientProfileModePrevious, UseResponsesLite: lite, SupportsParallelTools: true, ThreadID: identity.threadID,
		}
		allocated := testMeasureAllocatedBytes(t, func() {
			payload, index, decodeErr := decodeOfficialJSONObjectSharingBody(body)
			require.NoError(t, decodeErr)
			members, modified, finalizeErr := finalizeOfficialOpenAIHTTPBodyPayloadMembers(
				payload, body, index, contract, officialOpenAIReasoningDefaults{}, options)
			require.NoError(t, finalizeErr)
			require.True(t, modified)
			require.NotEmpty(t, members)
		})
		t.Logf("lite=%v：正文 %.1f MiB，共享解码加定型成员总分配 %.1f KiB", lite, float64(len(body))/(1<<20), float64(allocated)/1024)
		require.Less(t, allocated, uint64(len(body))/8,
			"lite=%v：安装基础指令分配 %d 字节，不得再按正文体积（%d 字节）成倍分配", lite, allocated, len(body))

		// 成员依次写出后的正文：基础指令在首位，历史引用原文。
		payload, index, decodeErr := decodeOfficialJSONObjectSharingBody(body)
		require.NoError(t, decodeErr)
		members, _, finalizeErr := finalizeOfficialOpenAIHTTPBodyPayloadMembers(payload, body, index, contract, officialOpenAIReasoningDefaults{}, options)
		require.NoError(t, finalizeErr)
		final := officialegress.AppendJSONObjectMembers(nil, members)
		require.True(t, json.Valid(final))
		require.False(t, gjson.GetBytes(final, "instructions").Exists())
		require.Equal(t, "model.base_instructions", gjson.GetBytes(final, "input.0.internal_chat_message_metadata_passthrough.content_item_kinds.0").String())
		require.Equal(t, "user", gjson.GetBytes(final, "input.1.role").String())
		require.Equal(t, 65, len(gjson.GetBytes(final, "input").Array()))
	}
}

// 节只列 responses_ws 时，HTTP 端点不采用载体：门控按端点解析，HTTP 仍走旧逻辑。
func TestFinalizeOfficialOpenAIHTTPBodyKeepsLegacyShapeWhenSectionDoesNotListHTTPEndpoint(t *testing.T) {
	newer := syntheticNewerProfileMutation(t)
	withOfficialCodexSyntheticPreviousProfile(t, func(doc *profilecontract.SnapshotDoc) {
		newer(doc)
		doc.BaseInstructions = json.RawMessage(`{"Carrier":"input_developer_message","ContentItemKind":"model.base_instructions","EndpointIDs":["responses_ws"],"IDPrefix":"msg"}`)
		endpoint := syntheticServiceSnapshotEndpoint(t, doc, officialCodexEndpointResponsesHTTP)
		endpoint.Body.Fields = append([]profilecontract.SnapshotBodyField{{Name: "instructions", OmitWhen: "empty_string"}}, endpoint.Body.Fields...)
	})
	body := buildBaseInstructionsHTTPBody(baseInstructionsHTTPVariant{
		"plain", `"You are a helper."`, `[` + baseInstructionsUserItem + `]`,
	})

	nonLite, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, false, "entry", body)
	require.NoError(t, err)
	require.Equal(t, "You are a helper.", gjson.GetBytes(nonLite, "instructions").String(), "非 Lite 保留顶层 instructions")
	require.Equal(t, []string{"message:user"}, describeBaseInstructionsInput(gjson.GetBytes(nonLite, "input").Array()))

	lite, _, _, err := finalizeBaseInstructionsHTTP(t, officialClientProfileModePrevious, true, "entry", body)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(lite, "instructions").Exists())
	first := gjson.GetBytes(lite, "input.0")
	require.Equal(t, "developer", first.Get("role").String(), "Lite 仍是旧的迁入形态")
	require.False(t, first.Get("id").Exists(), "旧迁入形态没有 id")
	require.False(t, first.Get("internal_chat_message_metadata_passthrough").Exists(), "旧迁入形态没有 passthrough")
}
