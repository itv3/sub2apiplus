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

// 增量窗口前缀在 WS 帧上的流程（画像 BaseInstructions 节 IDScheme=uuidv7，连接走 Lite）：
//  1. Codex 0.164 客户端：预热帧 input 为空、不被塞进默认指令；首个业务帧带的前缀（additional_tools 与基础指令消息）
//     逐字节原样放行，additional_tools 不补 passthrough；后续增量帧不动；
//  2. 第三方客户端：网关合成稳定 UUIDv7 的前缀，补出的预热帧 input 为空，业务帧保留前缀，同线程同内容的 id 各帧相同；
//  3. 从旧版本续聊的旧窗口（UUIDv5，前缀在预热帧里）：同样原样放行，业务帧不会凭空多出前缀；
//  4. 非 Lite 连接、画像未声明 uuidv7 方案：沿用旧形态（前缀随预热帧、业务帧剔除、UUIDv5）。

const incWSModel = "gpt-5.6-luna"

func incWSContext(t *testing.T, lite bool, firstPayload []byte) (context.Context, *OfficialEgressContext) {
	t.Helper()
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	return wsBaseContext(t, lite, firstPayload)
}

// incWSFrame 构造一帧 response.create；prev 非空时带 previous_response_id。
func incWSFrame(extra string, prev string, items ...string) []byte {
	previous := ""
	if prev != "" {
		previous = `"previous_response_id":"` + prev + `",`
	}
	return []byte(`{"type":"response.create","model":"` + incWSModel + `",` + previous + extra + `"input":[` + strings.Join(items, ",") + `]}`)
}

// incWSPrepare 模拟线上入口：候选帧是兼容层剥掉 passthrough 之后的版本，原始帧保留官方原文。
func incWSPrepare(t *testing.T, ctx context.Context, original []byte, expectedPrev string) []byte {
	t.Helper()
	candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(original)
	require.NoError(t, err)
	final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, candidate, expectedPrev, false)
	require.NoError(t, err)
	return final
}

func TestWSIncrementalPrefixOfficialClientFlowKeepsEmptyPrewarmAndVerbatimPrefix(t *testing.T) {
	prewarmFrame := incWSFrame(``, "")
	prewarmFrame = []byte(strings.Replace(string(prewarmFrame), `"input":[]}`, `"input":[],"generate":false}`, 1))
	require.Contains(t, string(prewarmFrame), `"generate":false`)
	ctx, egressContext := incWSContext(t, true, prewarmFrame)

	// 1. 官方预热帧：input 保持空数组，不回退到默认指令，连接上也没有记下已发送的基础指令。
	prewarm := incWSPrepare(t, ctx, prewarmFrame, "")
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw)
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())
	require.True(t, gjson.GetBytes(prewarm, "generate").Exists())
	require.False(t, gjson.GetBytes(prewarm, "instructions").Exists())
	require.NotContains(t, string(prewarm), "model.base_instructions", "预热帧不能被塞进默认基础指令")
	sent, _ := wsBaseState(egressContext)
	require.False(t, sent)
	// 预热帧自带 generate，网关不再补预热。
	_, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, prewarm)
	require.NoError(t, err)
	require.False(t, inject)

	// 2. 首个业务帧：前缀逐字节原样；其余项按本轮补 turn_id；additional_tools 不带 passthrough。
	original := incWSFrame(``, "resp_inc_warm", incOfficialTools, incOfficialBase, incOfficialContext, incOfficialUser)
	business := incWSPrepare(t, ctx, original, "resp_inc_warm")
	input := gjson.GetBytes(business, "input").Array()
	require.Len(t, input, 4)
	require.Equal(t, incOfficialTools, input[0].Raw, "additional_tools 逐字节原样")
	turnID := gjson.GetBytes(business, "client_metadata.turn_id").String()
	require.NotEmpty(t, turnID)
	requireBaseMessageVerbatimExceptTurnID(t, incOfficialBase, input[1].Raw, turnID,
		"基础指令消息除 turn_id 外逐字节原样（UUIDv7 id、create_time、content_item_metadata 都保留），turn_id 与首轮其余项、client_metadata 相同")
	require.Equal(t, 1, strings.Count(string(business), "model.base_instructions"), "前缀只出现一次，没有重复追加")
	require.Equal(t, "resp_inc_warm", gjson.GetBytes(business, "previous_response_id").String())
	for index := 2; index < 4; index++ {
		require.Equal(t, turnID, input[index].Get("internal_chat_message_metadata_passthrough.turn_id").String(), "input.%d", index)
	}
	sent, digest := wsBaseState(egressContext)
	require.True(t, sent)
	require.Equal(t, sha256.Sum256([]byte(incTestBaseText)), digest)

	// 3. 后续增量帧：只有新增项，前缀不被补回，也没有凭空多出的基础指令。
	increment := incWSPrepare(t, ctx, incWSFrame(``, "resp_inc_turn1", incOfficialUser), "resp_inc_turn1")
	require.Equal(t, []string{"message:user"}, describeWSInput(increment))
	require.NotContains(t, string(increment), "model.base_instructions")
	sent, _ = wsBaseState(egressContext)
	require.True(t, sent, "增量帧不改变连接上已发送的状态")
}

// 重连后官方客户端重发整段历史（无 previous_response_id、无 generate）：前缀原样，网关补的预热帧是空的，业务帧保留前缀。
func TestWSIncrementalPrefixFullHistoryReplayGetsEmptyGatewayPrewarmAndKeepsPrefix(t *testing.T) {
	original := incWSFrame(``, "", incOfficialTools, incOfficialBase, incOfficialContext, incOfficialUser)
	ctx, _ := incWSContext(t, true, original)

	first := incWSPrepare(t, ctx, original, "")
	require.Equal(t, incOfficialTools, gjson.GetBytes(first, "input.0").Raw)
	requireBaseMessageVerbatimExceptTurnID(t, incOfficialBase, gjson.GetBytes(first, "input.1").Raw,
		gjson.GetBytes(first, "client_metadata.turn_id").String(), "整段重放的基础指令消息")

	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw, "增量窗口的预热帧 input 为空")
	require.True(t, gjson.GetBytes(prewarm, "generate").Exists())
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())
	require.False(t, gjson.GetBytes(prewarm, "previous_response_id").Exists())
	turnMetadata := gjson.GetBytes(prewarm, "client_metadata.x-codex-turn-metadata").String()
	require.Equal(t, "prewarm", gjson.Get(turnMetadata, "request_kind").String())

	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, wsBasePrewarmResp, gjson.GetBytes(chain, "previous_response_id").String())
	require.False(t, gjson.GetBytes(chain, "generate").Exists())
	require.Equal(t, incOfficialTools, gjson.GetBytes(chain, "input.0").Raw, "业务帧保留 additional_tools，逐字节不变")
	requireBaseMessageVerbatimExceptTurnID(t, incOfficialBase, gjson.GetBytes(chain, "input.1").Raw,
		gjson.GetBytes(chain, "client_metadata.turn_id").String(), "业务帧保留基础指令消息，除 turn_id 外逐字节不变")
	require.Equal(t, []string{"tools", "BASE", "message:developer", "message:user"}, describeWSInput(chain))
}

func TestWSIncrementalPrefixThirdPartyIngressSynthesizesStableUUIDv7Prefix(t *testing.T) {
	section := incrementalPrefixSection()
	variant := wsLegacyVariant{"plain", incJSONString(t, incTestBaseText), `[` + baseInstructionsUserItem + `]`, ""}
	payload := buildWSLegacyDerivedPayload(variant)
	ctx, egressContext := incWSContext(t, true, payload)

	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, []string{"tools", "BASE", "message:user"}, describeWSInput(first))
	require.False(t, gjson.GetBytes(first, "instructions").Exists())
	tools := gjson.GetBytes(first, "input.0")
	base := gjson.GetBytes(first, "input.1")
	require.Equal(t, []string{"type", "id", "role", "tools"}, jsonTopLevelKeys(t, tools.Raw), "官方键序 type,id,role,tools")
	requireStableUUIDv7ItemID(t, tools.Get("id").String(), "at", "合成 additional_tools")
	require.False(t, tools.Get("internal_chat_message_metadata_passthrough").Exists(), "官方 additional_tools 项没有 passthrough")
	requireStableUUIDv7ItemID(t, base.Get("id").String(), "msg", "合成基础指令消息")
	requireSynthesizedBasePassthrough(t, base, "WS 第三方入站")
	require.True(t, isOfficialCodexOfficialBaseInstructionsForm(section, []byte(base.Raw), incTestBaseText))
	requireTurnIDOnNonPrefixItems(t, first)
	sent, digest := wsBaseState(egressContext)
	require.True(t, sent)
	require.Equal(t, sha256.Sum256([]byte(incTestBaseText)), digest)

	// 网关补的预热帧 input 为空；业务帧保留前缀，二者拼成官方 0.164 的两帧关系。
	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw)
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())
	require.True(t, gjson.GetBytes(prewarm, "generate").Exists())
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, []string{"tools", "BASE", "message:user"}, describeWSInput(chain))
	require.Equal(t, tools.Raw, gjson.GetBytes(chain, "input.0").Raw)
	require.Equal(t, base.Raw, gjson.GetBytes(chain, "input.1").Raw)
	require.Equal(t, wsBasePrewarmResp, gjson.GetBytes(chain, "previous_response_id").String())
	requireTurnIDOnNonPrefixItems(t, chain)

	// 续链帧重复同一段 instructions：丢弃，不再追加前缀。
	repeated := []byte(`{"type":"response.create","model":"` + incWSModel + `","previous_response_id":"resp_inc_tp_1","instructions":` +
		incJSONString(t, incTestBaseText) + `,"input":[` + baseInstructionsUserItem + `]}`)
	increment, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, repeated, repeated, "resp_inc_tp_1", false)
	require.NoError(t, err)
	require.Equal(t, []string{"message:user"}, describeWSInput(increment))

	// 在同一线程重新开链（无 previous_response_id）并带同一段正文：id 与第一次完全相同（上游前缀缓存不被 id 破坏）。
	again, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, base.Raw, gjson.GetBytes(again, "input.1").Raw)
	require.Equal(t, tools.Raw, gjson.GetBytes(again, "input.0").Raw)

	// 换一段正文：新的基础指令消息 id 不同。
	other := buildWSLegacyDerivedPayload(wsLegacyVariant{"plain", incJSONString(t, incTestBaseText+"changed"), `[` + baseInstructionsUserItem + `]`, ""})
	changed, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, other, other, "", false)
	require.NoError(t, err)
	require.NotEqual(t, base.Get("id").String(), gjson.GetBytes(changed, "input.1.id").String())
	require.Equal(t, tools.Get("id").String(), gjson.GetBytes(changed, "input.0.id").String(), "工具定义没变，additional_tools 的 id 不变")
}

// requireTurnIDOnNonPrefixItems 要求除 additional_tools 与基础指令消息之外的每一项都带 client_metadata.turn_id。
func requireTurnIDOnNonPrefixItems(t *testing.T, frame []byte) {
	t.Helper()
	turnID := gjson.GetBytes(frame, "client_metadata.turn_id").String()
	require.NotEmpty(t, turnID)
	for index, item := range gjson.GetBytes(frame, "input").Array() {
		passthrough := item.Get("internal_chat_message_metadata_passthrough")
		switch {
		case item.Get("type").String() == "additional_tools":
			require.False(t, passthrough.Exists(), "input.%d：additional_tools 不带 passthrough", index)
		case passthrough.Get("content_item_kinds").Exists():
			requireSynthesizedBasePassthrough(t, item, "input."+strconv.Itoa(index))
			require.Equal(t, turnID, passthrough.Get("turn_id").String(), "input.%d：单段时基础指令消息的 turn_id 等于本轮 turn_id", index)
		default:
			require.Equal(t, turnID, passthrough.Get("turn_id").String(), "input.%d 应带本轮 turn_id", index)
		}
	}
}

// 从旧版本续聊的旧窗口：前缀（UUIDv5）随预热帧发送，业务帧没有前缀。官方形态的项同样原样放行，业务帧不会凭空多出前缀。
func TestWSIncrementalPrefixLegacyWindowKeepsPrefixInOfficialPrewarmFrame(t *testing.T) {
	prewarmFrame := []byte(`{"type":"response.create","model":"` + incWSModel + `","input":[` + incLegacyTools + `,` + incLegacyBase + `],"generate":false}`)
	ctx, egressContext := incWSContext(t, true, prewarmFrame)

	prewarm := incWSPrepare(t, ctx, prewarmFrame, "")
	require.Equal(t, incLegacyTools, gjson.GetBytes(prewarm, "input.0").Raw)
	require.Equal(t, incLegacyBase, gjson.GetBytes(prewarm, "input.1").Raw, "旧窗口的 UUIDv5 基础指令消息逐字节原样")
	require.Len(t, gjson.GetBytes(prewarm, "input").Array(), 2)
	sent, _ := wsBaseState(egressContext)
	require.True(t, sent)
	_, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, prewarm)
	require.NoError(t, err)
	require.False(t, inject)

	business := incWSPrepare(t, ctx, incWSFrame(``, "resp_legacy_warm", incOfficialContext, incOfficialUser), "resp_legacy_warm")
	require.Equal(t, []string{"message:developer", "message:user"}, describeWSInput(business))
	require.NotContains(t, string(business), "model.base_instructions", "业务帧不能凭空多出前缀")
}

// 入站带了基础指令标记但不是官方形态：不原样放行，按正文合成稳定 UUIDv7 id。
func TestWSIncrementalPrefixRegeneratesNonOfficialLookalike(t *testing.T) {
	lookalike := strings.Replace(incOfficialBase, "msg_01a17935-5c98-7f51-995d-9110469c51d5", "msg_from_client", 1)
	original := incWSFrame(``, "", incOfficialTools, lookalike, incOfficialUser)
	ctx, _ := incWSContext(t, true, original)

	first := incWSPrepare(t, ctx, original, "")
	require.Equal(t, []string{"tools", "BASE", "message:user"}, describeWSInput(first))
	base := gjson.GetBytes(first, "input.1")
	require.NotEqual(t, lookalike, base.Raw)
	requireStableUUIDv7ItemID(t, base.Get("id").String(), "msg", "非官方形态重新合成")
	requireSynthesizedBasePassthrough(t, base, "非官方形态重新合成")
	require.Equal(t, incOfficialTools, gjson.GetBytes(first, "input.0").Raw, "官方形态的 additional_tools 仍原样")
}

// 非 Lite 连接即使画像声明 uuidv7 方案也沿用旧形态：前缀（UUIDv5）随预热帧，业务帧剔除。
func TestWSIncrementalPrefixNonLiteKeepsLegacyShape(t *testing.T) {
	section := incrementalPrefixSection()
	payload := buildWSLegacyDerivedPayload(wsLegacyVariant{"plain", incJSONString(t, incTestBaseText), `[` + baseInstructionsUserItem + `]`, ""})
	ctx, egressContext := incWSContext(t, false, payload)

	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, []string{"BASE", "message:user"}, describeWSInput(first))
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, wsBaseThreadID(t, egressContext), incTestBaseText)
	require.NoError(t, err)
	require.Equal(t, []string{string(rendered)}, wsBaseItems(first), "非 Lite：按网关线程重算 UUIDv5")

	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, []string{"BASE"}, describeWSInput(prewarm), "非 Lite：前缀随预热帧")
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, []string{"message:user"}, describeWSInput(chain), "非 Lite：业务帧剔除前缀")
}

// 画像不声明 uuidv7 方案（旧方案）时 Lite 连接保持旧形态：前缀随预热帧、业务帧剔除、additional_tools 仍补 turn_id。
func TestWSIncrementalPrefixLegacySchemeKeepsWarmupCarriedPrefixAndItemTurnID(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	payload := buildWSLegacyDerivedPayload(wsLegacyVariant{"plain", incJSONString(t, incTestBaseText), `[` + baseInstructionsUserItem + `]`, ""})
	ctx, _ := wsBaseContext(t, true, payload)

	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, []string{"tools", "BASE", "message:user"}, describeWSInput(first))
	require.False(t, gjson.GetBytes(first, "input.0.id").Exists(), "旧方案：additional_tools 不补 id")
	require.True(t, gjson.GetBytes(first, "input.0.internal_chat_message_metadata_passthrough.turn_id").Exists(),
		"旧方案：additional_tools 仍按旧逻辑补 turn_id（0.163 画像行为不变）")
	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, []string{"tools", "BASE"}, describeWSInput(prewarm))
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, []string{"message:user"}, describeWSInput(chain))
}

// finalizeOfficialOpenAIWSInputTurnMetadata 对 additional_tools 的处理按画像方案分叉：uuidv7 方案不补 turn_id，旧方案补。
func TestFinalizeOfficialOpenAIWSInputTurnMetadataSkipsAdditionalToolsOnlyForUUIDv7Scheme(t *testing.T) {
	build := func() map[string]any {
		return map[string]any{
			"generate": true,
			"client_metadata": map[string]any{
				"turn_id":    "7a46fb58-2930-4d6c-9cca-ea1124fcc871",
				"session_id": "019f9577-d69f-7892-809e-8a3a4198c671",
			},
			"input": []any{
				map[string]any{"type": "additional_tools", "id": "at_x", "role": "developer", "tools": []any{map[string]any{"type": "function", "name": "f"}}},
				map[string]any{"type": "message", "role": "user", "content": "hi"},
				map[string]any{"type": "compaction_trigger"},
			},
		}
	}
	itemOf := func(payload map[string]any, index int) map[string]any {
		return typeAs[map[string]any](t, typeAs[[]any](t, payload["input"])[index])
	}

	v7 := build()
	modified, err := finalizeOfficialOpenAIWSInputTurnMetadata(v7, incrementalPrefixSection())
	require.NoError(t, err)
	require.True(t, modified)
	_, toolsHasPassthrough := itemOf(v7, 0)[officialOpenAIWSItemTurnMetadata]
	require.False(t, toolsHasPassthrough, "uuidv7 方案：additional_tools 不补 passthrough")
	_, triggerHasPassthrough := itemOf(v7, 2)[officialOpenAIWSItemTurnMetadata]
	require.False(t, triggerHasPassthrough, "uuidv7 方案：compaction_trigger 不补 passthrough")
	require.Equal(t, "7a46fb58-2930-4d6c-9cca-ea1124fcc871",
		typeAs[map[string]any](t, itemOf(v7, 1)[officialOpenAIWSItemTurnMetadata])["turn_id"], "其余项照常补 turn_id")

	for name, section := range map[string]*profilecontract.BaseInstructionsSection{
		"旧方案":  baseInstructionsTestSection(),
		"无载体节": nil,
	} {
		legacy := build()
		_, err := finalizeOfficialOpenAIWSInputTurnMetadata(legacy, section)
		require.NoError(t, err, name)
		passthrough, ok := itemOf(legacy, 0)[officialOpenAIWSItemTurnMetadata].(map[string]any)
		require.True(t, ok, "%s：additional_tools 保持旧行为（补 turn_id）", name)
		require.Equal(t, "7a46fb58-2930-4d6c-9cca-ea1124fcc871", passthrough["turn_id"], name)
		_, triggerHasPassthrough := itemOf(legacy, 2)[officialOpenAIWSItemTurnMetadata]
		require.True(t, triggerHasPassthrough, "%s：compaction_trigger 保持旧行为（补 turn_id）", name)
	}
}

// 第三方客户端重发多回合历史（用户、助手、用户…）：网关合成的基础指令消息自带 turn_id，链帧在多回合历史上再次定型时
// 它不参与历史回合的一致性判定，不会被误判成与历史项的 turn_id 冲突。
func TestWSIncrementalPrefixThirdPartyMultiTurnHistoryReplay(t *testing.T) {
	assistant := `{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}`
	user2 := `{"type":"message","role":"user","content":[{"type":"input_text","text":"again"}]}`
	payload := []byte(`{"type":"response.create","model":"` + incWSModel + `","instructions":` + incJSONString(t, incTestBaseText+"multi") + `,` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],` +
		`"input":[` + baseInstructionsUserItem + `,` + assistant + `,` + user2 + `,` + assistant + `,` + user2 + `]}`)
	ctx, _ := incWSContext(t, true, payload)

	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, "tools", describeWSInput(first)[0])
	require.Equal(t, "BASE", describeWSInput(first)[1])
	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw)
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	require.Equal(t, gjson.GetBytes(first, "input.1").Raw, gjson.GetBytes(chain, "input.1").Raw, "基础指令消息在链帧里逐字节不变")
	require.Len(t, gjson.GetBytes(chain, "input").Array(), 7)
}
