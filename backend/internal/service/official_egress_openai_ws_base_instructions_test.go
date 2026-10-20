package service

import (
	"context"
	"crypto/sha256"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// WS 帧上的基础指令载体（画像 BaseInstructions 节列出 responses_ws 时）：
//  1. 首帧：顶层 instructions 取走，官方形态消息装在 additional_tools 之后，旧副本与带标记的旧消息去掉，
//     基础指令消息不补 turn_id，其余项照常补写；
//  2. 预热帧 input 只取前缀（additional_tools 与基础指令消息），其他 developer 消息留给正式业务帧；
//  3. 链帧剔除基础指令消息；
//  4. 续链帧按连接上已发送的摘要决策：相同丢弃、不同追加并提示、没带不动；
//  5. 官方形态入口：原始帧上的带标记消息在兼容层剥掉标记后仍能识别，并以网关线程重算 id；
//  6. 节缺省时的旧形态由 official_egress_openai_ws_legacy_golden_test.go 固化。

const (
	wsBaseHelperText  = "You are a helper."
	wsBaseOtherText   = "Different instructions."
	wsBaseAgentsText  = "AGENTS rules."
	wsBasePrevID      = "resp_ws_base_prev"
	wsBasePrewarmResp = "resp_ws_base_prewarm"
)

func wsBaseSection(t *testing.T) *profilecontract.BaseInstructionsSection {
	t.Helper()
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	section := synthetic.Optional().BaseInstructions
	require.NotNil(t, section)
	return section
}

func wsBaseContext(t *testing.T, lite bool, firstPayload []byte) (context.Context, *OfficialEgressContext) {
	t.Helper()
	ctx := newWSLegacyDerivedContext(t, officialClientProfileModePrevious, lite, firstPayload)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	return ctx, egressContext
}

func wsBaseThreadID(t *testing.T, egressContext *OfficialEgressContext) string {
	t.Helper()
	return mustOfficialEgressField(t, egressContext, OfficialEgressFieldThreadID).Value()
}

// wsBaseExpectedItem 按网关线程算出期望的官方形态消息。
func wsBaseExpectedItem(t *testing.T, section *profilecontract.BaseInstructionsSection, egressContext *OfficialEgressContext, text string) string {
	t.Helper()
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, wsBaseThreadID(t, egressContext), text)
	require.NoError(t, err)
	return string(rendered)
}

// describeWSInput 把帧里的 input 描述成序列：基础指令消息记 BASE，additional_tools 记 tools，其余记 type:role。
func describeWSInput(frame []byte) []string {
	items := gjson.GetBytes(frame, "input").Array()
	out := make([]string, 0, len(items))
	for _, item := range items {
		switch {
		case item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists():
			out = append(out, "BASE")
		case item.Get("type").String() == "additional_tools":
			out = append(out, "tools")
		default:
			out = append(out, item.Get("type").String()+":"+item.Get("role").String())
		}
	}
	return out
}

// wsBaseItems 返回帧里的基础指令消息原文（按出现顺序）。
func wsBaseItems(frame []byte) []string {
	var raws []string
	for _, item := range gjson.GetBytes(frame, "input").Array() {
		if item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() {
			raws = append(raws, item.Raw)
		}
	}
	return raws
}

func wsBaseState(egressContext *OfficialEgressContext) (bool, officialCodexBaseInstructionsDigest) {
	state := egressContext.openAIWSDerived
	state.mu.Lock()
	defer state.mu.Unlock()
	return state.baseInstructionsSent, state.baseInstructionsDigest
}

const wsBaseTaggedPassthrough = `{"content_item_kinds":["model.base_instructions"]}`

// requireWSItemTurnIDs 要求除基础指令消息外的每一项都带指定的 turn_id，基础指令消息的 passthrough 保持原样。
func requireWSItemTurnIDs(t *testing.T, frame []byte, turnID string, label string) {
	t.Helper()
	for index, item := range gjson.GetBytes(frame, "input").Array() {
		passthrough := item.Get("internal_chat_message_metadata_passthrough")
		if passthrough.Get("content_item_kinds").Exists() {
			require.Equal(t, wsBaseTaggedPassthrough, passthrough.Raw, "%s：基础指令消息不补 turn_id", label)
			continue
		}
		require.Equal(t, turnID, passthrough.Get("turn_id").String(), "%s：input.%d 应带本轮 turn_id", label, index)
	}
}

// wsBaseFrameModel 是 buildWSLegacyDerivedPayload 写死的帧内模型；没有来源时的默认指令按它选择。
const wsBaseFrameModel = "gpt-5.6-luna"

// wsBaseVariant 是一种第三方首帧形态及其在非 Lite 与 Lite 下的预期。
type wsBaseVariant struct {
	wsLegacyVariant
	// text 是期望装进基础指令消息的正文（有来源时是来源正文，没有来源时是模型默认指令）。
	text string
	// first 是首帧 input 的预期序列，lite 时用 firstLite。
	first, firstLite []string
}

func wsBaseVariants() []wsBaseVariant {
	user := baseInstructionsUserItem
	const helper = `"You are a helper."`
	agents := `{"type":"message","role":"developer","content":[{"type":"input_text","text":"AGENTS rules."}]}`
	staleTagged := baseInstructionsTaggedItem
	fallback := defaultCodexSynthInstructions(wsBaseFrameModel)
	return []wsBaseVariant{
		{wsLegacyVariant{"plain", helper, `[` + user + `]`, ""}, wsBaseHelperText,
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"dev_copy", helper, `[` + baseInstructionsCopyItem + `,` + user + `]`, ""}, wsBaseHelperText,
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"tagged_stale", helper, `[` + staleTagged + `,` + user + `]`, ""}, wsBaseHelperText,
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"tools_first", helper, `[` + baseInstructionsToolsItem + `,` + user + `]`, ""}, wsBaseHelperText,
			[]string{"tools", "BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"tools_dev_copy", helper, `[` + baseInstructionsToolsItem + `,` + baseInstructionsCopyItem + `,` + user + `]`, ""}, wsBaseHelperText,
			[]string{"tools", "BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"other_developer", helper, `[` + agents + `,` + user + `]`, ""}, wsBaseHelperText,
			[]string{"BASE", "message:developer", "message:user"}, []string{"tools", "BASE", "message:developer", "message:user"}},
		{wsLegacyVariant{"special", `"指令 a<b>&c \"q\"\n"`, `[` + user + `]`, ""}, "指令 a<b>&c \"q\"\n",
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		// 没有来源（没有顶层 instructions，或只有空白）的新链首帧回退到模型默认 Codex 指令，
		// 与 HTTP 对缺 instructions 的 OAuth 请求注入默认指令是同一个来源和函数。
		{wsLegacyVariant{"no_instructions", "", `[` + user + `]`, ""}, fallback,
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
		{wsLegacyVariant{"blank", `"   "`, `[` + user + `]`, ""}, fallback,
			[]string{"BASE", "message:user"}, []string{"tools", "BASE", "message:user"}},
	}
}

func TestOpenAIWSFramesInstallOfficialBaseInstructionsOnTargetSlot(t *testing.T) {
	section := wsBaseSection(t)
	resetOfficialOpenAIProjectedFieldsSeenForTest(t)
	for _, lite := range []bool{false, true} {
		for _, variant := range wsBaseVariants() {
			label := variant.name + "/lite=" + strconv.FormatBool(lite)
			payload := buildWSLegacyDerivedPayload(variant.wsLegacyVariant)
			require.Equal(t, wsBaseFrameModel, gjson.GetBytes(payload, "model").String(), label+"：默认指令按帧内模型选择")
			ctx, egressContext := wsBaseContext(t, lite, payload)

			first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
			require.NoError(t, err, label)
			want := variant.first
			if lite {
				want = variant.firstLite
			}
			require.Equal(t, want, describeWSInput(first), label+"/first")
			require.False(t, gjson.GetBytes(first, "instructions").Exists(), "%s：画像声明了载体就不能再发顶层 instructions", label)
			requireWSItemTurnIDs(t, first, gjson.GetBytes(first, "client_metadata.turn_id").String(), label+"/first")
			firstBase := wsBaseItems(first)
			require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, variant.text)}, firstBase, label+"：官方形态消息原样出现")
			sent, digest := wsBaseState(egressContext)
			require.True(t, sent, label)
			require.Equal(t, sha256.Sum256([]byte(variant.text)), digest, label)
			keys := jsonTopLevelKeys(t, string(first))
			require.True(t, isSubsequenceOf(keys, syntheticNewerWSBodyOrder), "%s：顶层键序必须跟随画像字段序：%v", label, keys)

			prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
			require.NoError(t, err, label)
			require.True(t, inject, label)
			var wantPrewarm []string
			for _, item := range want {
				if item == "tools" || item == "BASE" {
					wantPrewarm = append(wantPrewarm, item)
				}
			}
			require.Equal(t, wantPrewarm, describeWSInput(prewarm), label+"/prewarm：input 只取前缀（additional_tools 与基础指令消息）")
			require.False(t, gjson.GetBytes(prewarm, "generate").Bool(), label)
			require.Equal(t, firstBase, wsBaseItems(prewarm), label+"/prewarm：基础指令消息逐字节不变")
			for index, item := range gjson.GetBytes(prewarm, "input").Array() {
				if !item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() {
					require.False(t, item.Get("internal_chat_message_metadata_passthrough").Exists(),
						"%s/prewarm：input.%d 不带逐项 turn_id", label, index)
				}
			}

			chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
			require.NoError(t, err, label)
			var wantChain []string
			for _, item := range want {
				if item != "tools" && item != "BASE" {
					wantChain = append(wantChain, item)
				}
			}
			require.Equal(t, wantChain, describeWSInput(chain), label+"/chain：剔除前缀")
			require.Empty(t, wsBaseItems(chain), label)
			require.Equal(t, wsBasePrewarmResp, gjson.GetBytes(chain, "previous_response_id").String(), label)
			requireWSItemTurnIDs(t, chain, gjson.GetBytes(chain, "client_metadata.turn_id").String(), label+"/chain")
		}
	}
	require.Empty(t, projectedFieldWarningsMentioning("instructions"), "取走 instructions 后不应再触发画像闭集外字段告警")
}

// isSubsequenceOf 判断 keys 是否是 order 的子序列（键都在表内且相对次序一致）。
func isSubsequenceOf(keys []string, order []string) bool {
	position := 0
	for _, key := range keys {
		for position < len(order) && order[position] != key {
			position++
		}
		if position == len(order) {
			return false
		}
		position++
	}
	return true
}

// ---- 连接状态与来源决策 ----

func TestPlanBaseInstructionsFollowsTheChainState(t *testing.T) {
	digestOf := func(b byte) officialCodexBaseInstructionsDigest {
		var digest officialCodexBaseInstructionsDigest
		digest[0] = b
		return digest
	}
	state := &officialOpenAIWSDerivedState{}
	steps := []struct {
		name             string
		chained, sourced bool
		digest           byte
		action           officialCodexWSBaseInstructionsAction
		replaced         bool
		wantSent         bool
		wantDigest       byte
	}{
		{"新链首次带正文", false, true, 1, officialCodexWSBaseInstructionsInstall, false, true, 1},
		{"续链重复同一段正文", true, true, 1, officialCodexWSBaseInstructionsDrop, false, true, 1},
		{"续链换了一段正文", true, true, 2, officialCodexWSBaseInstructionsInstall, true, true, 2},
		{"续链重复新的正文", true, true, 2, officialCodexWSBaseInstructionsDrop, false, true, 2},
		{"续链没带正文", true, false, 0, officialCodexWSBaseInstructionsNone, false, true, 2},
		{"新链没带正文", false, false, 0, officialCodexWSBaseInstructionsNone, false, false, 2},
		{"清除之后续链带正文按首次对待", true, true, 2, officialCodexWSBaseInstructionsInstall, false, true, 2},
		{"新链同一段正文仍然安装", false, true, 2, officialCodexWSBaseInstructionsInstall, false, true, 2},
	}
	for _, step := range steps {
		action, replaced := state.planBaseInstructions(step.chained, step.sourced, digestOf(step.digest))
		require.Equal(t, step.action, action, step.name)
		require.Equal(t, step.replaced, replaced, step.name)
		state.mu.Lock()
		sent, digest := state.baseInstructionsSent, state.baseInstructionsDigest
		state.mu.Unlock()
		require.Equal(t, step.wantSent, sent, step.name)
		require.Equal(t, digestOf(step.wantDigest), digest, step.name)
	}
}

func TestOfficialCodexWSBaseInstructionsSourceTable(t *testing.T) {
	section := baseInstructionsTestSection()
	decode := func(raw string) map[string]any {
		payload, err := decodeOfficialJSONObjectUseNumber([]byte(raw))
		require.NoError(t, err, raw)
		return payload
	}
	tagged := func(text string) string {
		return `{"type":"message","id":"msg_inbound","role":"developer","content":[{"type":"input_text","text":"` + text + `"}],` +
			`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	}
	untagged := func(text string) string {
		return `{"type":"message","role":"developer","content":[{"type":"input_text","text":"` + text + `"}]}`
	}
	otherKind := `{"type":"message","role":"developer","content":[{"type":"input_text","text":"K"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.other"]}}`
	user := baseInstructionsUserItem
	frame := func(instructions string, items ...string) string {
		field := ""
		if instructions != "" {
			field = `"instructions":` + instructions + `,`
		}
		return `{"type":"response.create",` + field + `"input":[` + strings.Join(items, ",") + `]}`
	}

	tests := []struct {
		name      string
		original  string // 空串表示没有原始帧树
		candidate string
		wantText  string
		wantOK    bool
		wantErr   bool
	}{
		{"候选帧顶层 instructions", "", frame(`"A"`, user), "A", true, false},
		{"顶层 instructions 空白视同没有", "", frame(`"   "`, user), "   ", false, false},
		{"顶层 instructions 非字符串失败关闭", "", frame(`123`, user), "", false, true},
		{"顶层 instructions 存在就由它说了算（空白压过 input 里的标记）", "", frame(`"  "`, tagged("B"), user), "  ", false, false},
		{"顶层 instructions 压过 input 里不同文本的标记", "", frame(`"A"`, tagged("B"), user), "A", true, false},
		{"候选帧 input 前缀里仍带标记", "", frame("", baseInstructionsToolsItem, tagged("B"), user), "B", true, false},
		{"标记在扫描范围之外（第四项）", "", frame("", user, user, user, tagged("B")), "", false, false},
		{"其他内容标记不认", "", frame("", otherKind, user), "", false, false},
		{"原始帧带标记且候选帧留着同文本副本", frame("", tagged("C"), user), frame("", untagged("C"), user), "C", true, false},
		{"原始帧带标记但候选帧没有副本（不复活被删除的消息）", frame("", tagged("C"), user), frame("", user), "", false, false},
		{"原始帧带标记但候选帧的副本文本不同", frame("", tagged("C"), user), frame("", untagged("D"), user), "", false, false},
		{"没有原始帧树", "", frame("", untagged("C"), user), "", false, false},
		{"input 是字符串", "", `{"type":"response.create","input":"hi"}`, "", false, false},
	}
	for _, tt := range tests {
		var original map[string]any
		if tt.original != "" {
			original = decode(tt.original)
		}
		text, sourced, err := officialCodexWSBaseInstructionsSource(original, decode(tt.candidate), section)
		if tt.wantErr {
			require.ErrorContains(t, err, "must be a string", tt.name)
			continue
		}
		require.NoError(t, err, tt.name)
		require.Equal(t, tt.wantOK, sourced, tt.name)
		require.Equal(t, tt.wantText, text, tt.name)
	}
}

// ---- 续链决策（D5） ----

func TestOpenAIWSBaseInstructionsFollowTheChainOnTargetSlot(t *testing.T) {
	section := wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		label := "lite=" + strconv.FormatBool(lite)
		officialCodexBaseInstructionsChangedWarned.Store(false)
		t.Cleanup(func() { officialCodexBaseInstructionsChangedWarned.Store(false) })

		first := buildWSLegacyDerivedPayload(wsLegacyVariant{
			name: "chain", instructions: `"` + wsBaseHelperText + `"`, input: `[` + baseInstructionsUserItem + `]`,
		})
		ctx, egressContext := wsBaseContext(t, lite, first)
		prepare := func(frame []byte, expectedPrev string) []byte {
			t.Helper()
			final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, frame, frame, expectedPrev, false)
			require.NoError(t, err, label)
			return final
		}
		incremental := func(instructions string, extra ...string) []byte {
			field := ""
			if instructions != "" {
				field = `"instructions":"` + instructions + `",`
			}
			items := append(extra, `{"type":"message","role":"user","content":[{"type":"input_text","text":"again"}]}`)
			return []byte(`{"type":"response.create","model":"gpt-5.6-luna","previous_response_id":"` + wsBasePrevID + `",` +
				field + `"input":[` + strings.Join(items, ",") + `]}`)
		}
		sentWith := func(text string) {
			t.Helper()
			sent, digest := wsBaseState(egressContext)
			require.True(t, sent, label)
			require.Equal(t, sha256.Sum256([]byte(text)), digest, label)
		}

		// 1. 新链：安装，记下摘要。
		frame1 := prepare(first, "")
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseHelperText)}, wsBaseItems(frame1), label)
		sentWith(wsBaseHelperText)

		// 2. 续链重复同一段正文：丢弃，连 input 里的旧副本一起去掉。
		copyOfHelper := `{"type":"message","role":"developer","content":[{"type":"input_text","text":"` + wsBaseHelperText + `"}]}`
		frame2 := prepare(incremental(wsBaseHelperText, copyOfHelper), wsBasePrevID)
		require.Equal(t, []string{"message:user"}, describeWSInput(frame2), label+"：重复正文被丢弃")
		require.False(t, gjson.GetBytes(frame2, "instructions").Exists(), label)
		require.False(t, officialCodexBaseInstructionsChangedWarned.Load(), label+"：重复不提示")

		// 3. 续链换了一段正文：作为新的基础指令消息追加，更新摘要，提示一次。
		frame3 := prepare(incremental(wsBaseOtherText), wsBasePrevID)
		require.Equal(t, []string{"BASE", "message:user"}, describeWSInput(frame3), label)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseOtherText)}, wsBaseItems(frame3), label)
		require.True(t, officialCodexBaseInstructionsChangedWarned.Load(), label+"：换正文要提示")
		sentWith(wsBaseOtherText)

		// 4. 续链重复新的正文：再次丢弃。
		frame4 := prepare(incremental(wsBaseOtherText), wsBasePrevID)
		require.Equal(t, []string{"message:user"}, describeWSInput(frame4), label)

		// 4b. 正文只差末尾换行：官方消息 id 按原文计算，这是另一条消息，摘要也按原文计算，不能当作相同。
		const withNewline = wsBaseOtherText + "\\n"
		frame4b := prepare(incremental(withNewline), wsBasePrevID)
		require.Equal(t, []string{"BASE", "message:user"}, describeWSInput(frame4b), label)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseOtherText+"\n")}, wsBaseItems(frame4b), label)
		frame4c := prepare(incremental(wsBaseOtherText), wsBasePrevID)
		require.Equal(t, []string{"BASE", "message:user"}, describeWSInput(frame4c), label+"：换回去又是一次变化")
		sentWith(wsBaseOtherText)

		// 5. 续链没带正文：不动，连接状态不变。
		frame5 := prepare(incremental(""), wsBasePrevID)
		require.Equal(t, []string{"message:user"}, describeWSInput(frame5), label)
		sentWith(wsBaseOtherText)

		// 6. 新链没带任何基础指令来源：回退到模型默认 Codex 指令（与 HTTP 注入默认指令同一个来源和函数），
		// 连接上记下默认正文的摘要。
		noInstructions := buildWSLegacyDerivedPayload(wsLegacyVariant{name: "bare", input: `[` + baseInstructionsUserItem + `]`})
		frame6 := prepare(noInstructions, "")
		defaultText := defaultCodexSynthInstructions(wsBaseFrameModel)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, defaultText)}, wsBaseItems(frame6), label)
		sentWith(defaultText)

		// 7. 回退装入默认指令之后，续链帧带了客户端自己的正文：与已发送的默认正文不同，
		// 作为新的基础指令消息追加，并提示一次。
		officialCodexBaseInstructionsChangedWarned.Store(false)
		frame7 := prepare(incremental(wsBaseHelperText), wsBasePrevID)
		require.Equal(t, []string{"BASE", "message:user"}, describeWSInput(frame7), label)
		require.True(t, officialCodexBaseInstructionsChangedWarned.Load(), label+"：换了一段正文要提示")
		sentWith(wsBaseHelperText)

		// 8. 新链同一段正文：仍然安装（新链上的上下文里还没有它）。
		frame8 := prepare(first, "")
		wantFrame8 := []string{"BASE", "message:user"}
		if lite {
			wantFrame8 = []string{"tools", "BASE", "message:user"}
		}
		require.Equal(t, wantFrame8, describeWSInput(frame8), label)
	}
}

// 官方形态入口：原始帧上带标记的基础指令消息，在兼容层剥掉标记之后仍能识别，并以网关线程重算 id；
// 候选帧仍带标记时同样重建。网关的输出里只有一条基础指令消息。
func TestOpenAIWSFrameRebuildsOfficialBaseInstructionsFromTaggedInboundMessage(t *testing.T) {
	section := wsBaseSection(t)
	const officialText = "Official base instructions.\n"
	taggedItem := `{"type":"message","id":"msg_inbound_other_thread","role":"developer","content":[{"type":"input_text","text":"Official base instructions.\n"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	// OAuth 兼容层只剥掉 input 项的 internal_chat_message_metadata_passthrough；合法前缀的消息 id 保留。
	strippedItem := `{"type":"message","id":"msg_inbound_other_thread","role":"developer","content":[{"type":"input_text","text":"Official base instructions.\n"}]}`
	frame := func(item string) []byte {
		return []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[` + baseInstructionsToolsItem + `,` + item + `,` +
			baseInstructionsUserItem + `]}`)
	}
	for _, lite := range []bool{false, true} {
		for _, tt := range []struct {
			name                string
			original, candidate []byte
		}{
			{"候选帧与原始帧相同", frame(taggedItem), frame(taggedItem)},
			{"兼容层剥掉了标记", frame(taggedItem), frame(strippedItem)},
		} {
			label := tt.name + "/lite=" + strconv.FormatBool(lite)
			ctx, egressContext := wsBaseContext(t, lite, tt.original)
			final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, tt.original, tt.candidate, "", false)
			require.NoError(t, err, label)
			require.Equal(t, []string{"tools", "BASE", "message:user"}, describeWSInput(final), label)
			want := wsBaseExpectedItem(t, section, egressContext, officialText)
			require.Equal(t, []string{want}, wsBaseItems(final), label+"：官方形态消息逐字节原样，id 由网关线程重算")
			require.NotContains(t, string(final), "msg_inbound_other_thread", label)
			requireWSItemTurnIDs(t, final, gjson.GetBytes(final, "client_metadata.turn_id").String(), label)
		}
	}
}

// 官方客户端自己发的预热帧（generate=false）里带基础指令消息：重建后 passthrough 保持 content_item_kinds，
// 其他项的逐项 turn_id 按预热规则删除。
func TestOpenAIWSOfficialPrewarmFrameKeepsTheBaseInstructionsPassthrough(t *testing.T) {
	section := wsBaseSection(t)
	const text = "Official base instructions.\n"
	original := []byte(`{"type":"response.create","model":"gpt-5.6-luna","generate":false,"input":[` + baseInstructionsToolsItem + `,` +
		`{"type":"message","id":"msg_inbound","role":"developer","content":[{"type":"input_text","text":"Official base instructions.\n"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}},` +
		`{"type":"message","role":"developer","content":[{"type":"input_text","text":"other rules"}],` +
		`"internal_chat_message_metadata_passthrough":{"turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}]}`)
	ctx, egressContext := wsBaseContext(t, true, original)
	final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, original, "", false)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(final, "generate").Bool(), "前提：预热帧")
	require.Equal(t, []string{"tools", "BASE", "message:developer"}, describeWSInput(final))
	require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, text)}, wsBaseItems(final))
	for _, item := range gjson.GetBytes(final, "input").Array() {
		if !item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() {
			require.False(t, item.Get("internal_chat_message_metadata_passthrough").Exists(), "预热帧其余项不带逐项 turn_id：%s", item.Raw)
		}
	}
}

// 带标记但正文空白的消息没有可用的基础指令正文，等同没有来源：新链回退到模型默认 Codex 指令，空白的旧标记消息被默认
// 基础指令消息取代（不留两条）；安装的消息 passthrough 只带内容标记，仍然豁免逐项 turn_id 补写。
func TestOpenAIWSTaggedBaseInstructionsWithBlankTextKeepsItsPassthrough(t *testing.T) {
	section := wsBaseSection(t)
	blankTagged := `{"type":"message","id":"msg_blank","role":"developer","content":[{"type":"input_text","text":"   "}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	payload := buildWSLegacyDerivedPayload(wsLegacyVariant{
		name: "blank_tagged", input: `[` + blankTagged + `,` + baseInstructionsUserItem + `]`,
	})
	for _, lite := range []bool{false, true} {
		ctx, egressContext := wsBaseContext(t, lite, payload)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.NoError(t, err)
		defaultText := defaultCodexSynthInstructions(wsBaseFrameModel)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, defaultText)}, wsBaseItems(final),
			"lite=%v：空白正文的标记消息被默认基础指令取代，只剩一条", lite)
		item := gjson.GetBytes(final, "input.#(internal_chat_message_metadata_passthrough.content_item_kinds)")
		require.Equal(t, wsBaseTaggedPassthrough, item.Get("internal_chat_message_metadata_passthrough").Raw,
			"lite=%v：passthrough 只带内容标记，不补 turn_id", lite)
		sent, digest := wsBaseState(egressContext)
		require.True(t, sent, "lite=%v：回退装入了默认正文，要记录", lite)
		require.Equal(t, sha256.Sum256([]byte(defaultText)), digest, "lite=%v", lite)
	}
}

func TestOpenAIWSBaseInstructionsTopLevelWinsOverTaggedMessage(t *testing.T) {
	section := wsBaseSection(t)
	tagged := `{"type":"message","id":"msg_stale","role":"developer","content":[{"type":"input_text","text":"Stale text."}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
	payload := buildWSLegacyDerivedPayload(wsLegacyVariant{
		name: "both", instructions: `"` + wsBaseHelperText + `"`, input: `[` + tagged + `,` + baseInstructionsUserItem + `]`,
	})
	for _, lite := range []bool{false, true} {
		ctx, egressContext := wsBaseContext(t, lite, payload)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.NoError(t, err)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseHelperText)}, wsBaseItems(final),
			"lite=%v：顶层正文胜出，带标记的旧消息（无论文本）被去掉", lite)
	}
}

// 官方形态消息的 id 由网关线程算出：guardian 子会话的线程与会话不同，算出的 id 必须跟线程走。
func TestOpenAIWSBaseInstructionsUseTheGatewayThreadForGuardianRequests(t *testing.T) {
	section := wsBaseSection(t)
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
	sessionID := mustOfficialEgressField(t, egressContext, OfficialEgressFieldSessionID).Value()
	threadID := wsBaseThreadID(t, egressContext)
	require.NotEqual(t, sessionID, threadID, "前提：guardian 子会话的线程与会话不同")

	business := []byte(`{"type":"response.create","model":"gpt-5.6-luna","instructions":"` + wsBaseHelperText + `",` +
		`"input":[` + baseInstructionsUserItem + `]}`)
	final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, business, business, "", false)
	require.NoError(t, err)
	require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseHelperText)}, wsBaseItems(final))
	bySession, err := renderOfficialCodexBaseInstructionsItem(section, sessionID, wsBaseHelperText)
	require.NoError(t, err)
	require.NotEqual(t, string(bySession), wsBaseItems(final)[0], "用会话 ID 算出的 id 是另一条消息")
}

func TestOpenAIWSBaseInstructionsRejectNonStringInstructions(t *testing.T) {
	wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		payload := buildWSLegacyDerivedPayload(wsLegacyVariant{
			name: "number", instructions: `123`, input: `[` + baseInstructionsUserItem + `]`,
		})
		ctx, _ := wsBaseContext(t, lite, payload)
		_, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
		require.ErrorContains(t, err, "must be a string", "lite=%v：载体分支对非字符串 instructions 失败关闭", lite)
	}
}

// ---- 工具续接 ----

// 第三方客户端的完整历史工具帧没有 previous_response_id：定型按新链处理并安装基础指令，随后的收敛帧只剩当前轮工具输出，
// 基础指令消息不会出现在收敛帧里（上游链上已经有它）。
func TestOpenAIWSToolContinuationFrameDoesNotCarryTheBaseInstructions(t *testing.T) {
	section := wsBaseSection(t)
	for _, lite := range []bool{false, true} {
		label := "lite=" + strconv.FormatBool(lite)
		first := buildWSLegacyDerivedPayload(wsLegacyVariant{
			name: "tool", instructions: `"` + wsBaseHelperText + `"`, input: `[` + baseInstructionsUserItem + `]`,
		})
		ctx, egressContext := wsBaseContext(t, lite, first)
		_, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, first, first, "", false)
		require.NoError(t, err, label)

		toolFrame := []byte(`{"type":"response.create","model":"gpt-5.6-luna","instructions":"` + wsBaseHelperText + `","input":[
			{"type":"additional_tools","tools":[]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件"}]},
			{"type":"reasoning","encrypted_content":"encrypted"},
			{"type":"function_call","call_id":"call_ws_base_1","name":"read","arguments":"{}"},
			{"type":"function_call_output","call_id":"call_ws_base_1","output":"ok"}]}`)
		toolFinal, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, toolFrame, toolFrame, "resp_ws_base_tool", false)
		require.NoError(t, err, label)
		require.Equal(t, []string{wsBaseExpectedItem(t, section, egressContext, wsBaseHelperText)}, wsBaseItems(toolFinal), label+"：完整历史帧是新链，安装基础指令")

		narrowed, changed, err := buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(ctx, toolFinal, "resp_ws_base_tool")
		require.NoError(t, err, label)
		require.True(t, changed, label)
		require.Equal(t, []string{"function_call_output:"}, describeWSInput(narrowed), label)
		require.Empty(t, wsBaseItems(narrowed), label)
		require.Equal(t, "resp_ws_base_tool", gjson.GetBytes(narrowed, "previous_response_id").String(), label)
	}
}

// ---- 门控与对照组 ----

// previous 槽位（带基础指令载体）与 Active 槽位（无节）的 WS 连接交替构造帧，各自的结果互不影响。
func TestOpenAIWSBaseInstructionsSlotsDoNotInfluenceEachOther(t *testing.T) {
	section := wsBaseSection(t)
	payload := wsAccessProgramsFirstFrame("")
	for _, lite := range []bool{false, true} {
		var previousFirst, activeFirst, previousPrewarm, activePrewarm []byte
		for round := 0; round < 3; round++ {
			build := func(mode string) ([]byte, []byte, *OfficialEgressContext) {
				ctx := newWSLegacyDerivedContext(t, mode, lite, payload)
				egressContext, _ := OfficialEgressContextFromContext(ctx)
				first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
				require.NoError(t, err)
				prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
				require.NoError(t, err)
				require.True(t, inject)
				return normalizeWSLegacyFrame(first), normalizeWSLegacyFrame(prewarm), egressContext
			}
			previous, previousW, previousContext := build(officialClientProfileModePrevious)
			active, activeW, _ := build(officialClientProfileModeActive)
			if round == 0 {
				previousFirst, previousPrewarm, activeFirst, activePrewarm = previous, previousW, active, activeW
				require.Equal(t, []string{wsBaseExpectedItem(t, section, previousContext, wsBaseHelperText)}, wsBaseItems(previous))
				require.Empty(t, wsBaseItems(active), "Active 槽位没有基础指令载体：不装官方形态消息")
				require.Contains(t, string(active), wsBaseHelperText, "Active 槽位仍保留指令文本")
			}
			require.Equal(t, string(previousFirst), string(previous), "lite=%v round=%d", lite, round)
			require.Equal(t, string(previousPrewarm), string(previousW), "lite=%v round=%d", lite, round)
			require.Equal(t, string(activeFirst), string(active), "lite=%v round=%d", lite, round)
			require.Equal(t, string(activePrewarm), string(activeW), "lite=%v round=%d", lite, round)
		}
	}
}

// 节只列 responses_http 时，WS 端点不采用载体：门控按端点解析，WS 仍走旧逻辑（顶层 instructions 或 Lite 迁入形态）。
func TestOpenAIWSFrameKeepsLegacyShapeWhenSectionDoesNotListTheWSEndpoint(t *testing.T) {
	newer := syntheticNewerProfileMutation(t)
	withOfficialCodexSyntheticPreviousProfile(t, func(doc *profilecontract.SnapshotDoc) {
		newer(doc)
		doc.BaseInstructions = syntheticServiceRawSection(t, profilecontract.BaseInstructionsSection{
			Carrier: "input_developer_message", ContentItemKind: "model.base_instructions",
			EndpointIDs: []string{officialCodexEndpointResponsesHTTP}, IDPrefix: "msg",
		})
		endpoint := syntheticServiceSnapshotEndpoint(t, doc, officialCodexEndpointResponsesWS)
		endpoint.Body.Fields = append([]profilecontract.SnapshotBodyField{{Name: "instructions", OmitWhen: "empty_string"}}, endpoint.Body.Fields...)
	})
	payload := wsAccessProgramsFirstFrame("")

	nonLiteCtx, _ := wsBaseContext(t, false, payload)
	nonLite, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(nonLiteCtx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, wsBaseHelperText, gjson.GetBytes(nonLite, "instructions").String(), "非 Lite 保留顶层 instructions")
	require.Empty(t, wsBaseItems(nonLite))
	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(nonLiteCtx, nonLite)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, []string{"message:developer"}, describeWSInput(prewarm), "旧逻辑：预热帧取全部 developer 消息")

	liteCtx, _ := wsBaseContext(t, true, payload)
	lite, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(liteCtx, payload, payload, "", false)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(lite, "instructions").Exists())
	require.Empty(t, wsBaseItems(lite))
	for _, item := range gjson.GetBytes(lite, "input").Array() {
		if item.Get("role").String() == "developer" && item.Get("type").String() == "message" {
			require.False(t, item.Get("id").Exists(), "旧迁入形态没有 id：%s", item.Raw)
		}
	}
}

// 载体不让 WS 首帧的分配随正文体积成倍增长：同一份大帧在载体启用与节缺省两个槽位上，分配量只差一个小常数。
func TestOpenAIWSBaseInstructionsDoNotAddProportionalAllocations(t *testing.T) {
	wsBaseSection(t)
	body := baseInstructionsLargeBody(t, 64, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)
	frame := append([]byte(`{"type":"response.create",`), body[1:]...)
	require.Equal(t, "response.create", gjson.GetBytes(frame, "type").String(), "前提：这是一个 response.create 帧")
	for _, lite := range []bool{false, true} {
		measure := func(mode string) uint64 {
			ctx := newWSLegacyDerivedContext(t, mode, lite, frame)
			return testMeasureAllocatedBytes(t, func() {
				final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, frame, frame, "", false)
				require.NoError(t, err)
				require.True(t, gjson.GetBytes(final, "client_metadata.turn_id").Exists(), "前提：帧确实被定型了")
				require.Equal(t, mode == officialClientProfileModePrevious, len(wsBaseItems(final)) == 1,
					"载体只在启用该节的槽位上装基础指令消息")
			})
		}
		legacy := measure(officialClientProfileModeActive)
		carrier := measure(officialClientProfileModePrevious)
		t.Logf("lite=%v：正文 %.1f MiB，节缺省分配 %.1f KiB，载体分配 %.1f KiB", lite,
			float64(len(frame))/(1<<20), float64(legacy)/1024, float64(carrier)/1024)
		extra := uint64(0)
		if carrier > legacy {
			extra = carrier - legacy
		}
		require.Less(t, extra, uint64(len(frame))/32+64*1024,
			"lite=%v：载体比节缺省多分配 %d 字节，不得随正文体积（%d 字节）增长", lite, extra, len(frame))
	}
}
