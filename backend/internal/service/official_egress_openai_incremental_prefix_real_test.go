package service

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 用 codex-cli 0.164.0 官方客户端的真实请求做输入（非正式实采：对本地假上游发的真实请求，原始字节，gzip 压缩存放）：
//   - s1_ws_prewarm／s1_ws_business：Lite 模型 WS 的预热帧（input 为空）与首个业务帧（前缀 additional_tools＋基础指令消息）；
//   - m1_ws_model_switch_replay：换模型后的整段历史重放（无 previous_response_id，多回合，含 model.instructions 等）；
//   - c1_ws_compaction_trigger／c1_ws_new_window_replay：压缩帧（previous_response_id 加单项 compaction_trigger）与压缩后新窗口的整段重放；
//   - s2_http_lite：同模型 HTTP 请求；
//   - r1_http_legacy_window：0.163 建的会话由 0.164 续聊（前缀 id 仍是 UUIDv5，基础指令消息的 passthrough 多了 content_item_metadata）。
// 两条核心断言：空 input 的预热帧不能被塞进默认指令；业务帧的前缀逐字节保留。

func loadCodex0164Frame(t *testing.T, name string) []byte {
	t.Helper()
	file, err := os.Open(filepath.Join("testdata", "codex_0164_informal", name+".json.gz"))
	require.NoError(t, err)
	defer func() { _ = file.Close() }()
	reader, err := gzip.NewReader(file)
	require.NoError(t, err)
	raw, err := io.ReadAll(reader)
	require.NoError(t, err)
	require.True(t, gjson.ValidBytes(raw))
	return raw
}

// incWSOfficialContext 构造官方客户端入口的 WS 上下文（入站头由首帧的 client_metadata 生成），冻结在 previous 槽位。
func incWSOfficialContext(t *testing.T, lite bool, firstPayload []byte) (context.Context, *OfficialEgressContext) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	applyOfficialOpenAIWSIngressHeadersForTest(c, firstPayload)
	ctx, err := attachOfficialEgressWebSocketContext(
		withOpenAIResponsesLiteCapability(context.Background(), lite),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
		wsLegacyConfigForMode(officialClientProfileModePrevious),
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	require.True(t, ok)
	require.NotNil(t, egressContext.openAIWSDerived)
	return ctx, egressContext
}

func inputRaws(frame []byte) []string {
	items := gjson.GetBytes(frame, "input").Array()
	raws := make([]string, 0, len(items))
	for _, item := range items {
		raws = append(raws, item.Raw)
	}
	return raws
}

// requireBaseMessageVerbatimExceptTurnID 要求 got 的基础指令消息与 want（入站官方原文）除 passthrough.turn_id 外逐字节相同：
// id、键序、其余各键、空白都不变。入站带 turn_id 键时，got 的 turn_id 必须等于 wantTurnID（网关的"窗口第一段"turn_id），
// 且不能还是客户端的值（不得把客户端命名空间的 id 漏到上游）；入站没有 turn_id 键（旧窗口的官方形态）时整项逐字节相同，不补键。
func requireBaseMessageVerbatimExceptTurnID(t *testing.T, want, got, wantTurnID, label string) {
	t.Helper()
	clientTurn := gjson.Get(want, "internal_chat_message_metadata_passthrough.turn_id")
	if !clientTurn.Exists() {
		require.True(t, want == got, "%s：入站基础指令消息没有 turn_id 键，必须整项逐字节原样且不补键\nwant %.300s\n got %.300s", label, want, got)
		return
	}
	gotTurn := gjson.Get(got, "internal_chat_message_metadata_passthrough.turn_id")
	require.True(t, gotTurn.Exists(), label)
	require.Equal(t, wantTurnID, gotTurn.String(), "%s：基础指令消息的 turn_id 是网关的窗口第一段 turn_id", label)
	require.NotEqual(t, clientTurn.String(), gotTurn.String(), "%s：客户端的 turn_id 不得漏到上游", label)
	expected := string(rewriteOfficialCodexBaseInstructionsRawTurnID(json.RawMessage(want), wantTurnID))
	require.True(t, expected == got, "%s：除 turn_id 外必须逐字节原样\nwant %.300s\n got %.300s", label, expected, got)
}

// requireBasePrefixVerbatim 要求 got 的 input 前两项（additional_tools 与基础指令消息）与 want 帧里的相同：
// additional_tools 逐字节，基础指令消息除 turn_id 外逐字节；turn_id 必须与 got 里第一段其余项的 turn_id 一致
// （官方里它就是窗口第一个回合的 id，与第一段其余项相同）。
func requireBasePrefixVerbatim(t *testing.T, want, got []byte, label string) {
	t.Helper()
	wantItems, gotItems := inputRaws(want), inputRaws(got)
	require.GreaterOrEqual(t, len(wantItems), 2, label)
	require.GreaterOrEqual(t, len(gotItems), 2, label)
	require.Equal(t, "additional_tools", gjson.Get(wantItems[0], "type").String(), label)
	require.Equal(t, "model.base_instructions", gjson.Get(wantItems[1], "internal_chat_message_metadata_passthrough.content_item_kinds.0").String(), label)
	require.True(t, wantItems[0] == gotItems[0], "%s：additional_tools 必须逐字节原样（%d 字节）", label, len(wantItems[0]))
	wantTurnID := gjson.Get(gotItems[1], "internal_chat_message_metadata_passthrough.turn_id").String()
	if len(gotItems) > 2 {
		if firstSegment := gjson.Get(gotItems[2], "internal_chat_message_metadata_passthrough.turn_id"); firstSegment.Exists() {
			wantTurnID = firstSegment.String()
		}
	}
	requireBaseMessageVerbatimExceptTurnID(t, wantItems[1], gotItems[1], wantTurnID, label)
}

func TestWSIncrementalPrefixRealCodex0164LiteFlow(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	require.Equal(t, "[]", gjson.GetBytes(prewarmFrame, "input").Raw, "真实预热帧的 input 就是空数组")
	require.False(t, gjson.GetBytes(businessFrame, "generate").Exists())

	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, egressContext := incWSOfficialContext(t, true, prewarmFrame)

	// 1. 真实预热帧：input 仍是空数组，没有被塞进默认基础指令，连接上也没有记下已发送的基础指令。
	candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(prewarmFrame)
	require.NoError(t, err)
	prewarm, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, prewarmFrame, candidate, "", false)
	require.NoError(t, err)
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw)
	require.True(t, gjson.GetBytes(prewarm, "generate").Exists())
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())
	require.NotContains(t, string(prewarm), "model.base_instructions")
	require.NotContains(t, string(prewarm), defaultCodexSynthInstructions(gjson.GetBytes(prewarm, "model").String())[:40])
	sent, _ := wsBaseState(egressContext)
	require.False(t, sent)
	_, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, prewarm)
	require.NoError(t, err)
	require.False(t, inject, "客户端自己发了预热帧，网关不再补")

	// 2. 真实首个业务帧：前缀（38 KB 的工具目录与 21 KB 的基础指令正文）逐字节原样，其余项补本轮 turn_id。
	candidate, _, err = normalizeOpenAIOAuthResponsesCompatibilityBody(businessFrame)
	require.NoError(t, err)
	business, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, businessFrame, candidate, "resp_informal_0001", false)
	require.NoError(t, err)
	requireBasePrefixVerbatim(t, businessFrame, business, "首个业务帧")
	require.Equal(t, 1, strings.Count(string(business), "model.base_instructions"), "前缀只出现一次")
	require.Len(t, gjson.GetBytes(business, "input").Array(), len(gjson.GetBytes(businessFrame, "input").Array()))
	require.Equal(t, "resp_informal_0001", gjson.GetBytes(business, "previous_response_id").String())
	turnID := gjson.GetBytes(business, "client_metadata.turn_id").String()
	require.NotEmpty(t, turnID)
	for index, item := range gjson.GetBytes(business, "input").Array() {
		if index < 2 {
			continue
		}
		require.Equal(t, turnID, item.Get("internal_chat_message_metadata_passthrough.turn_id").String(), "input.%d", index)
	}
	sent, _ = wsBaseState(egressContext)
	require.True(t, sent)
}

// 换模型：帧里不带 previous_response_id，整段多回合历史重放（追加 model.instructions、collaboration_mode.instructions 与新的 user.text）。
// 前缀原样；网关补的预热帧 input 为空；链帧在多回合历史上再次定型不能因基础指令消息自带的 turn_id 报冲突。
func TestWSIncrementalPrefixRealCodex0164ModelSwitchReplay(t *testing.T) {
	replay := loadCodex0164Frame(t, "m1_ws_model_switch_replay")
	require.False(t, gjson.GetBytes(replay, "previous_response_id").Exists())
	require.False(t, gjson.GetBytes(replay, "generate").Exists())
	require.Equal(t, 15, len(gjson.GetBytes(replay, "input").Array()))

	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, egressContext := incWSOfficialContext(t, true, replay)
	candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(replay)
	require.NoError(t, err)
	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, replay, candidate, "", false)
	require.NoError(t, err)
	requireBasePrefixVerbatim(t, replay, first, "换模型整段重放")
	require.Equal(t, 15, len(gjson.GetBytes(first, "input").Array()))
	sent, _ := wsBaseState(egressContext)
	require.True(t, sent)

	prewarm, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject, "没有 generate 也没有 previous_response_id 的帧由网关补预热")
	require.Equal(t, "[]", gjson.GetBytes(prewarm, "input").Raw)
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())

	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	requireBasePrefixVerbatim(t, replay, chain, "链帧")
	require.Equal(t, 15, len(gjson.GetBytes(chain, "input").Array()), "业务帧保留整段历史，不剔除前缀")
	require.Equal(t, wsBasePrewarmResp, gjson.GetBytes(chain, "previous_response_id").String())
	require.Equal(t, 1, strings.Count(string(chain), `"model.base_instructions"`))
}

// 压缩：压缩帧是 previous_response_id 加单项 compaction_trigger（不带前缀，不能被补上基础指令）；压缩后换成新窗口，
// 前缀 id 重新随机生成，客户端用整段历史（无 previous_response_id）重放，新窗口的前缀原样放行。
func TestWSIncrementalPrefixRealCodex0164CompactionStartsNewWindow(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	compaction := loadCodex0164Frame(t, "c1_ws_compaction_trigger")
	newWindow := loadCodex0164Frame(t, "c1_ws_new_window_replay")
	require.Equal(t, "compaction_trigger", gjson.GetBytes(compaction, "input.0.type").String())
	require.Len(t, gjson.GetBytes(compaction, "input").Array(), 1)

	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, egressContext := incWSOfficialContext(t, true, prewarmFrame)
	prepare := func(original []byte, expectedPrev string) []byte {
		t.Helper()
		candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(original)
		require.NoError(t, err)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, candidate, expectedPrev, false)
		require.NoError(t, err)
		return final
	}
	prepare(prewarmFrame, "")
	oldWindow := prepare(businessFrame, "resp_informal_0001")

	compactionFinal := prepare(compaction, "resp_informal_0002")
	require.Equal(t, []string{"compaction_trigger"}, func() []string {
		var types []string
		for _, item := range gjson.GetBytes(compactionFinal, "input").Array() {
			types = append(types, item.Get("type").String())
		}
		return types
	}())
	require.Equal(t, `{"type":"compaction_trigger"}`, gjson.GetBytes(compactionFinal, "input.0").Raw,
		"官方 compaction_trigger 项没有 passthrough 字段，不能被补 turn_id")
	require.NotContains(t, string(compactionFinal), "model.base_instructions", "压缩帧不能被补上基础指令")
	require.Equal(t, "resp_informal_0002", gjson.GetBytes(compactionFinal, "previous_response_id").String())

	replay := prepare(newWindow, "")
	requireBasePrefixVerbatim(t, newWindow, replay, "压缩后的新窗口")
	require.NotEqual(t, gjson.GetBytes(oldWindow, "input.1.id").String(), gjson.GetBytes(replay, "input.1.id").String(), "新窗口的前缀 id 重新生成")
	require.Equal(t, 9, len(gjson.GetBytes(replay, "input").Array()))
	require.Equal(t, 1, strings.Count(string(replay), `"model.base_instructions"`))
	sent, _ := wsBaseState(egressContext)
	require.True(t, sent)
}

func TestFinalizeOfficialOpenAIHTTPBodyRealCodex0164Requests(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	cases := []struct {
		name       string
		file       string
		toolsID    string // 前缀 additional_tools 项的 id 前缀与版本
		baseIDHead string
		metadata   bool // 基础指令消息的 passthrough 是否带 content_item_metadata
	}{
		{"增量窗口 UUIDv7", "s2_http_lite", "at_01a1", "msg_01a1", true},
		{"旧窗口续聊 UUIDv5", "r1_http_legacy_window", "at_2b62", "msg_e938", true},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			inbound := loadCodex0164Frame(t, item.file)
			itemsBefore := inputRaws(inbound)
			require.True(t, strings.HasPrefix(gjson.Get(itemsBefore[0], "id").String(), item.toolsID), itemsBefore[0][:80])
			require.True(t, strings.HasPrefix(gjson.Get(itemsBefore[1], "id").String(), item.baseIDHead))
			require.Equal(t, item.metadata, gjson.Get(itemsBefore[1], "internal_chat_message_metadata_passthrough.content_item_metadata").Exists())

			normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(inbound)
			require.NoError(t, err)
			final, _ := finalizeIncrementalHTTP(t, true, inbound, normalized)
			requireBasePrefixVerbatim(t, inbound, final, item.name)
			require.Equal(t, len(itemsBefore), len(gjson.GetBytes(final, "input").Array()))
			require.False(t, gjson.GetBytes(final, "instructions").Exists())
			require.False(t, gjson.GetBytes(final, "tools").Exists())
		})
	}
}

// 第三方入站用真实的官方基础指令正文：合成项的形态与真实的 0.164 基础指令消息逐项同形——键序、passthrough 键序、
// content_item_metadata 字节都与真实帧一致，只是 id、turn_id、create_time 的值不同。
func TestSynthesizedBaseInstructionsMatchRealCodex0164Shape(t *testing.T) {
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	real := gjson.GetBytes(businessFrame, "input.1")
	text := real.Get("content.0.text").String()
	require.Greater(t, len(text), 20000)

	section := incrementalPrefixSection()
	rendered, err := renderOfficialCodexIncrementalBaseInstructionsItem(section, incTestThreadID, text)
	require.NoError(t, err)
	synthetic := gjson.ParseBytes(rendered)
	require.Equal(t, jsonTopLevelKeys(t, real.Raw), jsonTopLevelKeys(t, synthetic.Raw), "消息键序")
	require.Equal(t,
		jsonTopLevelKeys(t, real.Get("internal_chat_message_metadata_passthrough").Raw),
		jsonTopLevelKeys(t, synthetic.Get("internal_chat_message_metadata_passthrough").Raw), "passthrough 键序")
	require.Equal(t, real.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Raw,
		synthetic.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Raw)
	require.Equal(t, real.Get("internal_chat_message_metadata_passthrough.content_item_metadata").Raw,
		synthetic.Get("internal_chat_message_metadata_passthrough.content_item_metadata").Raw, "content_item_metadata 字节与真实一致")
	require.Equal(t, real.Get("content").Raw, synthetic.Get("content").Raw, "content 字节与真实一致（转义规则相同）")
	requireSynthesizedBasePassthrough(t, synthetic, "合成")
	// 真实消息本身满足官方形态判定（旧窗口的 UUIDv5 形态也满足）。
	require.True(t, isOfficialCodexOfficialBaseInstructionsForm(section, []byte(real.Raw), text))
	legacy := inputRaws(loadCodex0164Frame(t, "r1_http_legacy_window"))[1]
	require.True(t, isOfficialCodexOfficialBaseInstructionsForm(section, []byte(legacy), gjson.Get(legacy, "content.0.text").String()))
	require.False(t, bytes.Equal([]byte(real.Raw), rendered))
}
