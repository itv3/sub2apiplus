package service

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// RA 独立验收（Codex 0.164 仿真后端）查出问题的修复用例：
//   - F1：基础指令消息自己的 passthrough.turn_id 统一改写成网关的"窗口第一段"turn_id（HTTP、WS，官方形态与第三方合成）；
//   - F2：moveOfficialOpenAIAdditionalToolsFirst、additional_tools 键序两处共享逻辑只在画像声明 uuidv7 方案时生效，
//     0.163 画像的输出与引入这两处之前（main）逐字节相同；
//   - F3：官方形态 passthrough 里的 turn_id 不是 UUID 时，按非官方形态剥离该项 passthrough、请求照常转发，不整帧拒绝。

const raUserItem = `{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}`
const raAssistantItem = `{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hi"}]}`
const raUser2Item = `{"type":"message","role":"user","content":[{"type":"input_text","text":"again"}]}`

// raThirdPartyHTTPBody 构造第三方入站的 Lite 请求（顶层 instructions 与 tools，没有任何 passthrough）。
func raThirdPartyHTTPBody(t *testing.T, items ...string) []byte {
	t.Helper()
	return []byte(`{"model":"gpt-5.6-luna","instructions":` + incJSONString(t, "RA 第三方入站基础指令专用 "+t.Name()) + `,` +
		`"input":[` + strings.Join(items, ",") + `],` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],` +
		`"tool_choice":"auto","reasoning":{"effort":"medium"},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
}

func raBaseItem(t *testing.T, frame []byte) gjson.Result {
	t.Helper()
	for _, item := range gjson.GetBytes(frame, "input").Array() {
		if item.Get("internal_chat_message_metadata_passthrough.content_item_kinds.0").String() == "model.base_instructions" {
			return item
		}
	}
	require.Fail(t, "input 里没有基础指令消息")
	return gjson.Result{}
}

// F1（HTTP，第三方入站）：合成的基础指令消息 turn_id 等于网关本轮 turn_id；同一窗口里下一个请求（多了助手回复和新用户消息，
// 本轮 turn_id 换了）基础指令消息仍是同一个 turn_id、整项逐字节不变——它是窗口第一段的 id，不随本轮变化，
// 上游前缀缓存不因它击穿。
func TestRAHTTPThirdPartyBaseInstructionsTurnIDIsFirstSegmentAndStable(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))

	body1 := raThirdPartyHTTPBody(t, raUserItem)
	first, identity1 := finalizeIncrementalHTTP(t, true, body1, body1)
	base1 := raBaseItem(t, first)
	require.Equal(t, identity1.turnID, base1.Get("internal_chat_message_metadata_passthrough.turn_id").String(),
		"首轮：基础指令消息的 turn_id 等于网关本轮 turn_id（官方首轮也是这样）")
	requireSynthesizedBasePassthrough(t, base1, "首轮合成")

	body2 := raThirdPartyHTTPBody(t, raUserItem, raAssistantItem, raUser2Item)
	second, identity2 := finalizeIncrementalHTTP(t, true, body2, body2)
	require.NotEqual(t, identity1.turnID, identity2.turnID, "第二个请求是新的回合")
	base2 := raBaseItem(t, second)
	require.Equal(t, base1.Raw, base2.Raw, "同一窗口：基础指令消息整项逐字节相同，turn_id 不随本轮变化")
	require.Equal(t, identity1.turnID, base2.Get("internal_chat_message_metadata_passthrough.turn_id").String(),
		"多轮时等于第一段（历史）的 turn_id，也就是它当年作为当前回合时的值")
}

// F1（HTTP，官方形态入站，多段历史）：基础指令消息的 turn_id 与第一段其余项一致、不是当前回合的、不是客户端的；
// 整项除 turn_id 外逐字节保留；只有第一段（首轮）的请求得到同一个 turn_id（窗口内稳定）。
func TestRAHTTPOfficialBaseInstructionsTurnIDEqualsFirstSegmentTurnID(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	replay := loadCodex0164Frame(t, "m1_ws_model_switch_replay")
	inputs := gjson.GetBytes(replay, "input").Array()
	require.Equal(t, 15, len(inputs))
	clientBaseTurn := inputs[1].Get(p2PassthroughKey + ".turn_id").String()
	require.NotEmpty(t, clientBaseTurn)

	build := func(count int) []byte {
		raws := make([]string, 0, count)
		for _, item := range inputs[:count] {
			raws = append(raws, item.Raw)
		}
		return officialIncrementalHTTPBody(raws...)
	}
	finalize := func(count int) ([]byte, officialOpenAIHTTPIdentity) {
		full := build(count)
		normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBodyWithOptions(full, true, true)
		require.NoError(t, err)
		return finalizeIncrementalHTTP(t, true, full, normalized)
	}

	// 首轮（只有第一段）：基础指令消息的 turn_id 等于本轮 turn_id。
	firstFinal, firstIdentity := finalize(7)
	firstBase := raBaseItem(t, firstFinal)
	requireBaseMessageVerbatimExceptTurnID(t, inputs[1].Raw, firstBase.Raw, firstIdentity.turnID, "首轮")

	// 多段历史：第一段是 input.0～input.9；基础指令消息等于第一段其余项的 turn_id，也等于首轮的值。
	final, identity := finalize(15)
	require.NotEqual(t, firstIdentity.turnID, identity.turnID)
	base := raBaseItem(t, final)
	segmentTurn := gjson.GetBytes(final, "input.2."+p2PassthroughKey+".turn_id").String()
	require.NotEmpty(t, segmentTurn)
	require.Equal(t, segmentTurn, base.Get("internal_chat_message_metadata_passthrough.turn_id").String(), "与第一段其余项一致")
	require.Equal(t, firstIdentity.turnID, segmentTurn, "第一段历史的 turn_id 就是它当年作为当前回合时的值")
	require.NotEqual(t, identity.turnID, segmentTurn)
	require.NotEqual(t, clientBaseTurn, segmentTurn)
	requireBaseMessageVerbatimExceptTurnID(t, inputs[1].Raw, base.Raw, segmentTurn, "多段历史")
}

// F1（旧窗口，v5 前缀）：官方实采里旧窗口的基础指令消息 passthrough 本来就没有 turn_id 键，所以整项逐字节不变、不补键。
func TestRAHTTPLegacyWindowBaseInstructionsHasNoTurnIDAndStaysVerbatim(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	inbound := loadCodex0164Frame(t, "r1_http_legacy_window")
	items := inputRaws(inbound)
	require.False(t, gjson.Get(items[1], "internal_chat_message_metadata_passthrough.turn_id").Exists(), "官方实采：旧窗口基础指令消息没有 turn_id")
	normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBodyWithOptions(inbound, true, true)
	require.NoError(t, err)
	final, _ := finalizeIncrementalHTTP(t, true, inbound, normalized)
	require.Equal(t, items[1], inputRaws(final)[1], "无 turn_id 键：整项逐字节原样，不补键")
}

// F1（改写函数）：只换 turn_id 这一个字符串值的区间，其余字节（含空白、键序）逐字节不动；没有该键、值不是字符串、
// turnID 为空时原样返回。
func TestRewriteOfficialCodexBaseInstructionsRawTurnIDSplicesOnlyTheValue(t *testing.T) {
	raw := []byte(`{"type":"message","id":"msg_x","role":"developer","content":[{"type":"input_text","text":"turn_id"}],` +
		`"internal_chat_message_metadata_passthrough": {"turn_id" : "old-id" ,"create_time":1.5,"content_item_kinds":["model.base_instructions"]}}`)
	got := rewriteOfficialCodexBaseInstructionsRawTurnID(raw, "01a17935-c8dc-7cb2-8b5b-c9b98611bb03")
	require.Equal(t, strings.Replace(string(raw), `"old-id"`, `"01a17935-c8dc-7cb2-8b5b-c9b98611bb03"`, 1), string(got))
	require.Equal(t, `{"type":"message","id":"msg_x","role":"developer","content":[{"type":"input_text","text":"turn_id"}],`+
		`"internal_chat_message_metadata_passthrough": {"turn_id" : "old-id" ,"create_time":1.5,"content_item_kinds":["model.base_instructions"]}}`,
		string(raw), "入参不被改动")

	withoutKey := []byte(`{"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`)
	require.Equal(t, string(withoutKey), string(rewriteOfficialCodexBaseInstructionsRawTurnID(withoutKey, "01a17935-c8dc-7cb2-8b5b-c9b98611bb03")))
	notString := []byte(`{"internal_chat_message_metadata_passthrough":{"turn_id":7}}`)
	require.Equal(t, string(notString), string(rewriteOfficialCodexBaseInstructionsRawTurnID(notString, "01a17935-c8dc-7cb2-8b5b-c9b98611bb03")))
	require.Equal(t, string(raw), string(rewriteOfficialCodexBaseInstructionsRawTurnID(raw, " ")))
}

// F1（残留的对象形态基础指令消息）：没被安装步骤重建的残留项，turn_id 同样改写，不留客户端值。
func TestRewriteOfficialCodexBaseInstructionsMapTurnIDsRewritesLeftovers(t *testing.T) {
	section := incrementalPrefixSection()
	leftover := map[string]any{
		"type": "message", "role": "developer",
		p2PassthroughKey: map[string]any{"turn_id": "client-side", "content_item_kinds": []any{section.ContentItemKind}},
	}
	other := map[string]any{"type": "message", "role": "user", p2PassthroughKey: map[string]any{"turn_id": "keep-me"}}
	noKey := map[string]any{
		"type": "message", "role": "developer",
		p2PassthroughKey: map[string]any{"content_item_kinds": []any{section.ContentItemKind}},
	}
	payload := map[string]any{"input": []any{leftover, other, noKey}}
	require.True(t, rewriteOfficialCodexBaseInstructionsMapTurnIDs(payload, section, "gateway-turn"))
	require.Equal(t, "gateway-turn", leftover[p2PassthroughKey].(map[string]any)["turn_id"])
	require.Equal(t, "keep-me", other[p2PassthroughKey].(map[string]any)["turn_id"], "非基础指令消息不动")
	require.NotContains(t, noKey[p2PassthroughKey].(map[string]any), "turn_id", "没有 turn_id 键的不补键")
	require.False(t, rewriteOfficialCodexBaseInstructionsMapTurnIDs(payload, section, "gateway-turn"), "再次调用无改动")
	require.False(t, rewriteOfficialCodexBaseInstructionsMapTurnIDs(payload, section, ""), "turnID 为空不改")
}

// F1（WS，第三方入站，多回合）：基础指令消息的 turn_id 等于第一段其余项的 turn_id（网关派生的第一段值），
// 不再是独立的稳定值。
func TestRAWSThirdPartyBaseInstructionsTurnIDEqualsFirstSegment(t *testing.T) {
	payload := []byte(`{"type":"response.create","model":"` + incWSModel + `","instructions":` + incJSONString(t, incTestBaseText+"ra-ws") + `,` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],` +
		`"input":[` + raUserItem + `,` + raAssistantItem + `,` + raUser2Item + `]}`)
	ctx, _ := incWSContext(t, true, payload)
	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, []string{"tools", "BASE", "message:user", "message:assistant", "message:user"}, describeWSInput(first))
	baseTurn := gjson.GetBytes(first, "input.1.internal_chat_message_metadata_passthrough.turn_id").String()
	firstSegmentTurn := gjson.GetBytes(first, "input.2.internal_chat_message_metadata_passthrough.turn_id").String()
	currentTurn := gjson.GetBytes(first, "client_metadata.turn_id").String()
	require.NotEmpty(t, baseTurn)
	require.Equal(t, firstSegmentTurn, baseTurn, "基础指令消息 turn_id 等于第一段其余项")
	require.NotEqual(t, currentTurn, baseTurn, "多段历史：第一段不是当前回合")
	// 链帧再次定型不改变它。
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, gjson.GetBytes(first, "input.1").Raw, gjson.GetBytes(chain, "input.1").Raw)
}

// F1（WS 官方形态，首轮）：基础指令消息 turn_id、其余项 turn_id、client_metadata.turn_id 四处相同（官方首轮如此），
// 客户端的 turn_id 不再出现在任何位置。
func TestRAWSOfficialFirstTurnAllTurnIDsAreEqual(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	clientTurn := gjson.GetBytes(businessFrame, "client_metadata.turn_id").String()
	require.NotEmpty(t, clientTurn)
	require.Equal(t, clientTurn, gjson.GetBytes(businessFrame, "input.1."+p2PassthroughKey+".turn_id").String(), "官方首轮：基础指令消息 turn_id 就是 client_metadata.turn_id")

	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, _ := incWSOfficialContext(t, true, prewarmFrame)
	prepare := func(original []byte, previous string) []byte {
		candidate := p2Normalize(t, original, newOfficialOpenAIHTTPTestAccount(94), true)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, candidate, previous, false)
		require.NoError(t, err)
		return final
	}
	prepare(prewarmFrame, "")
	final := prepare(businessFrame, "resp_informal_0001")
	gatewayTurn := gjson.GetBytes(final, "client_metadata.turn_id").String()
	require.NotEqual(t, clientTurn, gatewayTurn)
	for index, item := range gjson.GetBytes(final, "input").Array() {
		if item.Get("type").String() == "additional_tools" {
			continue
		}
		require.Equal(t, gatewayTurn, item.Get(p2PassthroughKey+".turn_id").String(), "input.%d", index)
	}
	require.NotContains(t, string(final), clientTurn, "客户端的 turn_id 不得出现在出站帧的任何位置")
}

// ---- F2 ----

// 下面两个金样是在 main（f9fd48826，没有任何 0.164 改动）上对同样的输入跑同一条 HTTP 定型链路得到的 input 原文，
// 0.163 画像下本分支必须与之逐字节相同（取样方法：在 main 的独立工作树里放一个临时探针测试，调用
// captureOfficialOpenAIHTTPBodyContract、deriveOfficialOpenAIHTTPIdentity、finalizeOfficialOpenAIHTTPBody，打印 input）。
const (
	raMainGoldenToolsOrder = `[{"type":"additional_tools","role":"developer","tools":[{"type":"function","name":"f1","parameters":{"type":"object"}}]},` +
		`{"type":"additional_tools","id":"at_01a17935-5c98-7f51-995d-9105098f672a","role":"developer","tools":[{"type":"function","name":"f2","parameters":{"type":"object"}}]},` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}]`
	raMainGoldenToolsKeyOrder = `[{"tools":[{"type":"function","name":"f1","parameters":{"type":"object"}},{"type":"function","name":"f2","parameters":{"type":"object"}}],` +
		`"id":"at_01a17935-5c98-7f51-995d-9105098f672a","role":"developer","type":"additional_tools"},` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}]`
)

// 旧画像（keep=false）：所有 additional_tools（含带 id 的增量项）都提到首位，行为与 main 逐字相同。
func TestMoveOfficialOpenAIAdditionalToolsFirstLegacyProfileMovesEveryAdditionalTools(t *testing.T) {
	user := map[string]any{"type": "message", "role": "user", "content": "hi"}
	hint := map[string]any{"type": "message", "role": "developer", "content": "hint"}
	increment := map[string]any{"type": "additional_tools", "id": "at_01a17935-5c98-7f51-995d-9105098f672a", "role": "developer", "tools": []any{}}
	leading := map[string]any{"type": "additional_tools", "id": "at_01a17935-5c98-7f51-995d-9105098f6000", "role": "developer", "tools": []any{}}
	declared := map[string]any{"type": "additional_tools", "role": "developer", "tools": []any{}}
	cases := []struct {
		name         string
		input        []any
		wantModified bool
		want         []any
	}{
		{"带 id 的增量项在提示消息之后：旧画像照旧提到首位", []any{hint, increment, user}, true, []any{increment, hint, user}},
		{"RA 的窄条件：第一个工具项不在开头、后面还有带 id 的项", []any{user, declared, increment}, true, []any{declared, increment, user}},
		{"第一个工具项就在开头：不动（与 main 的早返回一致，即使后面还有带 id 的项）", []any{leading, user, increment}, false, []any{leading, user, increment}},
		{"第一个工具项就在开头且没有别的工具项：不动", []any{leading, user}, false, []any{leading, user}},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			payload := map[string]any{"input": append([]any(nil), item.input...)}
			require.Equal(t, item.wantModified, moveOfficialOpenAIAdditionalToolsFirst(payload, false))
			require.Equal(t, item.want, payload["input"])
		})
	}
}

// 旧画像（0.163）端到端：窄条件输入（第一个 additional_tools 不在 input 开头，后面还有带 id 的 additional_tools）
// 出站的 input 顺序与 main 一致（所有 additional_tools 提到首位）；声明 uuidv7 方案的画像则让带 id 的增量项留在原位。
func TestRAAdditionalToolsOrderingIsGatedByProfile(t *testing.T) {
	const declared = `{"type":"additional_tools","role":"developer","tools":[{"type":"function","name":"f1","parameters":{"type":"object"}}]}`
	const increment = `{"type":"additional_tools","id":"at_01a17935-5c98-7f51-995d-9105098f672a","role":"developer","tools":[{"type":"function","name":"f2","parameters":{"type":"object"}}]}`
	body := officialIncrementalHTTPBody(raUserItem, declared, increment)
	// describe 用"类型:工具名"描述 input 顺序（工具名区分 declared 的 f1 与增量项的 f2）。
	describe := func(frame []byte) []string {
		var out []string
		for _, item := range gjson.GetBytes(frame, "input").Array() {
			label := item.Get("type").String()
			if name := item.Get("tools.0.name"); name.Exists() {
				label += ":" + name.String()
			}
			out = append(out, label)
		}
		return out
	}

	t.Run("0.163 画像：与 main 相同，所有 additional_tools 在首位", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, nil)
		final, _ := finalizeIncrementalHTTP(t, true, body, body)
		require.Equal(t, []string{"additional_tools:f1", "additional_tools:f2", "message"}, describe(final))
		require.Equal(t, raMainGoldenToolsOrder, gjson.GetBytes(final, "input").Raw, "input 与 main 逐字节相同")
	})
	t.Run("uuidv7 画像：带 id 的增量项留在原位", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		final, _ := finalizeIncrementalHTTP(t, true, body, body)
		require.Equal(t, []string{"additional_tools:f1", "message", "additional_tools:f2"}, describe(final))
	})
}

// 旧画像（0.163）端到端：带 id 的 additional_tools 键序不是官方序，且被网关改写（顶层 tools 并入它）时，
// 键序保持原样（与 main 一致）；声明 uuidv7 方案的画像才固定成 type,id,role,tools。
func TestRAAdditionalToolsKeyOrderIsGatedByProfile(t *testing.T) {
	const existing = `{"tools":[{"type":"function","name":"f1","parameters":{"type":"object"}}],"id":"at_01a17935-5c98-7f51-995d-9105098f672a","role":"developer","type":"additional_tools"}`
	body := []byte(`{"model":"gpt-5.6-luna","input":[` + existing + `,` + raUserItem + `],` +
		`"tools":[{"type":"function","name":"f2","parameters":{"type":"object"}}],` +
		`"tool_choice":"auto","reasoning":{"effort":"medium"},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)

	t.Run("0.163 画像：保持原键序", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, nil)
		final, _ := finalizeIncrementalHTTP(t, true, body, body)
		item := gjson.GetBytes(final, "input.0")
		require.Equal(t, "additional_tools", item.Get("type").String())
		require.Len(t, item.Get("tools").Array(), 2, "顶层工具并入了该项，它被网关重编码")
		require.Equal(t, []string{"tools", "id", "role", "type"}, jsonTopLevelKeys(t, item.Raw))
		require.Equal(t, raMainGoldenToolsKeyOrder, gjson.GetBytes(final, "input").Raw, "input 与 main 逐字节相同")
	})
	t.Run("uuidv7 画像：固定官方键序", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		final, _ := finalizeIncrementalHTTP(t, true, body, body)
		item := gjson.GetBytes(final, "input.0")
		require.Len(t, item.Get("tools").Array(), 2)
		require.Equal(t, []string{"type", "id", "role", "tools"}, jsonTopLevelKeys(t, item.Raw))
	})
}

// 键序开关只对声明 uuidv7 方案的画像置位；占位索引不改变其余编码行为。
func TestOfficialJSONIndexWithAdditionalToolsKeyOrder(t *testing.T) {
	require.Nil(t, officialJSONIndexWithAdditionalToolsKeyOrder(nil, nil, false), "关闭时原样返回 nil")
	existing, err := buildOfficialJSONRawIndex([]byte(`{"a":1}`))
	require.NoError(t, err)
	require.Same(t, existing, officialJSONIndexWithAdditionalToolsKeyOrder(existing, nil, false))
	require.False(t, existing.orderAdditionalToolsKeys, "关闭时不置位")
	require.Same(t, existing, officialJSONIndexWithAdditionalToolsKeyOrder(existing, nil, true))
	require.True(t, existing.orderAdditionalToolsKeys)
	built := officialJSONIndexWithAdditionalToolsKeyOrder(nil, []byte(`{"input":[]}`), true)
	require.NotNil(t, built)
	require.True(t, built.orderAdditionalToolsKeys)
	placeholder := officialJSONIndexWithAdditionalToolsKeyOrder(nil, nil, true)
	require.NotNil(t, placeholder, "原文不可索引时用占位索引承载开关")
	require.True(t, placeholder.orderAdditionalToolsKeys)

	// 未置位时（旧画像）带 id 的 additional_tools 重编码不排序；置位后才排序。
	value := map[string]any{"tools": []any{}, "role": "developer", "id": "at_x", "type": "additional_tools"}
	plainIndex, err := buildOfficialJSONRawIndex([]byte(`{}`))
	require.NoError(t, err)
	plain, err := officialJSONAppendValue(plainIndex, nil, value, -1)
	require.NoError(t, err)
	require.Equal(t, `{"id":"at_x","role":"developer","tools":[],"type":"additional_tools"}`, string(plain), "旧画像：字典序，与 main 相同")
	nilIndex, err := officialJSONAppendValue(nil, nil, value, -1)
	require.NoError(t, err)
	require.Equal(t, string(plain), string(nilIndex))
	ordered, err := officialJSONAppendValue(placeholder, nil, value, -1)
	require.NoError(t, err)
	require.Equal(t, `{"type":"additional_tools","id":"at_x","role":"developer","tools":[]}`, string(ordered))
}

// ---- F3 ----

// 官方形态判定：passthrough 里的 turn_id 非空且不是 UUID 时不是官方形态（gjson 与 map 两个版本一致）；空白、UUID 仍是。
func TestOfficialCodexInputPassthroughFormRejectsNonUUIDTurnID(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want bool
	}{
		{"UUID", `{"turn_id":"01a17935-c8dc-7cb2-8b5b-c9b98611bb03","content_item_kinds":["user.text"]}`, true},
		{"空串（后续定型按没有处理）", `{"turn_id":"","content_item_kinds":["user.text"]}`, true},
		{"非 UUID", `{"turn_id":"turn-1","content_item_kinds":["user.text"]}`, false},
		{"UUID 前后带空白也算 UUID（与后续校验口径一致）", `{"turn_id":" 01a17935-c8dc-7cb2-8b5b-c9b98611bb03 ","content_item_kinds":["user.text"]}`, true},
		{"cell_id 不受影响", `{"cell_id":"anything","content_item_kinds":["user.text"]}`, true},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.Equal(t, item.want, isOfficialCodexInputPassthroughForm("message", gjson.Parse(item.raw), true))
			var decoded map[string]any
			require.NoError(t, json.Unmarshal([]byte(item.raw), &decoded))
			require.Equal(t, item.want, isOfficialCodexInputPassthroughFormMap("message", decoded, true))
		})
	}
}

// F3（WS 端到端）：官方形态帧里某项的 turn_id 不是 UUID——兼容层按非官方形态剥离该项的 passthrough，帧照常转发，
// 该项由定型补上网关 turn_id；其余项的 passthrough 仍除 turn_id 外逐字节保留。旧画像下行为本来就是剥离，结果一致。
func TestRAWSNonUUIDItemTurnIDIsStrippedNotRejected(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	tampered, err := sjson.SetBytes(businessFrame, "input.5."+p2PassthroughKey+".turn_id", "turn-1")
	require.NoError(t, err)

	run := func(t *testing.T, mutation bool) []byte {
		if mutation {
			withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		} else {
			withOfficialCodexSyntheticPreviousProfile(t, nil)
		}
		ctx, _ := incWSOfficialContext(t, true, prewarmFrame)
		account := newOfficialOpenAIHTTPTestAccount(94)
		candidate := p2Normalize(t, tampered, account, true)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, tampered, candidate, "resp_informal_0001", false)
		require.NoError(t, err, "非 UUID 的 turn_id 不再整帧拒绝")
		return final
	}

	t.Run("uuidv7 画像", func(t *testing.T) {
		final := run(t, true)
		turn := gjson.GetBytes(final, "client_metadata.turn_id").String()
		item := gjson.GetBytes(final, "input.5")
		require.Equal(t, `{"turn_id":"`+turn+`"}`, item.Get(p2PassthroughKey).Raw, "该项 passthrough 被剥离后只剩定型补的网关 turn_id")
		for _, index := range []int{2, 3, 4, 6} {
			path := "input." + strconv.Itoa(index) + "." + p2PassthroughKey
			want := gjson.GetBytes(businessFrame, path)
			got := gjson.GetBytes(final, path)
			p2RequirePassthroughExceptTurnID(t, want, got, path)
			require.Equal(t, turn, got.Get("turn_id").String())
		}
	})
	t.Run("0.163 画像", func(t *testing.T) {
		final := run(t, false)
		require.NotContains(t, string(final), "content_item_kinds", "旧画像：逐项 passthrough 一律剥离")
	})
}

// F3（HTTP 兼容层）：官方形态里某项 turn_id 非 UUID 时该项不暂存（照旧被兼容层剥离），其余官方形态项仍暂存保留。
func TestRAHTTPNonUUIDItemTurnIDIsNotStashed(t *testing.T) {
	inbound := loadCodex0164Frame(t, "s2_http_lite")
	tampered, err := sjson.SetBytes(inbound, "input.4."+p2PassthroughKey+".turn_id", "turn-1")
	require.NoError(t, err)
	decoded := map[string]any{}
	require.NoError(t, json.Unmarshal(tampered, &decoded))
	require.True(t, stashOfficialInputPassthroughForTransform(decoded, true), "其余项仍是官方形态")
	input := decoded["input"].([]any)
	_, stashedBad := input[4].(map[string]any)[officialCodexStashedInputPassthroughKey]
	require.False(t, stashedBad, "非 UUID turn_id 的项不暂存，交给兼容层剥离")
	_, stashedGood := input[5].(map[string]any)[officialCodexStashedInputPassthroughKey]
	require.True(t, stashedGood)
	restoreOfficialInputPassthroughAfterTransform(decoded)
}
