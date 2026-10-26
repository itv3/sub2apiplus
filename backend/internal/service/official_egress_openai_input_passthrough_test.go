package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 逐项 internal_chat_message_metadata_passthrough 保真（P2）的测试。
//
// 输入是 0.164 官方客户端对假上游发的真实帧（见 testdata/codex_0164_informal）。四个条件同时满足时逐项保留，
// 缺任一条件时照旧剥离；WS 上客户端自带的 turn_id 被改写成网关派生的 turn_id，其余键逐字节保留。

const p2PassthroughKey = "internal_chat_message_metadata_passthrough"

func p2Config() *config.Config {
	return wsLegacyConfigForMode(officialClientProfileModePrevious)
}

// p2Normalize 走与线上相同的 WS 兼容层入口（带配置与账号）。
func p2Normalize(t *testing.T, frame []byte, account *Account, lite bool) []byte {
	t.Helper()
	candidate, _, err := normalizeOpenAIResponsesWebSocketCompatibilityBodyWithConfig(p2Config(), frame, account, lite)
	require.NoError(t, err)
	return candidate
}

func p2Passthroughs(frame []byte) []gjson.Result {
	items := gjson.GetBytes(frame, "input").Array()
	out := make([]gjson.Result, len(items))
	for index, item := range items {
		out[index] = item.Get(p2PassthroughKey)
	}
	return out
}

// p2RequirePassthroughExceptTurnID 要求 got 帧第 index 项的 passthrough 与 want 帧同位置项：键序相同，除 turn_id 外
// 每个键的原文字节完全相同。
func p2RequirePassthroughExceptTurnID(t *testing.T, want, got gjson.Result, label string) {
	t.Helper()
	require.True(t, want.IsObject(), label)
	require.True(t, got.IsObject(), label)
	require.Equal(t, jsonTopLevelKeys(t, want.Raw), jsonTopLevelKeys(t, got.Raw), "%s：键序", label)
	want.ForEach(func(key, value gjson.Result) bool {
		if key.String() == "turn_id" {
			return true
		}
		require.Equal(t, value.Raw, got.Get(key.String()).Raw, "%s：%s 必须逐字节保留", label, key.String())
		return true
	})
}

func TestOfficialCodexInputPassthroughFormMatchesRealFrames(t *testing.T) {
	for _, name := range []string{"s1_ws_business", "m1_ws_model_switch_replay", "c1_ws_new_window_replay", "s2_http_lite", "r1_http_legacy_window"} {
		frame := loadCodex0164Frame(t, name)
		seen := 0
		for index, item := range gjson.GetBytes(frame, "input").Array() {
			passthrough := item.Get(p2PassthroughKey)
			itemType := item.Get("type").String()
			if !passthrough.Exists() {
				require.False(t, officialCodexInputItemTypeCarriesPassthrough(itemType) && itemType == "message", "%s input.%d 消息项应带 passthrough", name, index)
				continue
			}
			seen++
			require.True(t, isOfficialCodexInputPassthroughForm(itemType, passthrough), "%s input.%d", name, index)
			var decoded map[string]any
			decoder := json.NewDecoder(strings.NewReader(passthrough.Raw))
			decoder.UseNumber()
			require.NoError(t, decoder.Decode(&decoded))
			require.True(t, isOfficialCodexInputPassthroughFormMap(itemType, decoded), "%s input.%d（map）", name, index)
		}
		require.Greater(t, seen, 0, name)
	}
}

func TestOfficialCodexInputPassthroughFormRejectsNonOfficialShapes(t *testing.T) {
	cases := []struct {
		name     string
		itemType string
		raw      string
	}{
		{"未知键", "message", `{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be","foo":1}`},
		{"turn_id 不是字符串", "message", `{"turn_id":7}`},
		{"create_time 不是数字", "message", `{"create_time":"1"}`},
		{"content_item_kinds 不是字符串数组", "message", `{"content_item_kinds":[1]}`},
		{"content_item_metadata 不是数组", "message", `{"content_item_metadata":{"a":1}}`},
		{"tool_calls_complete 不是布尔", "function_call_output", `{"tool_calls_complete":"yes"}`},
		{"空对象", "message", `{}`},
		{"不是对象", "message", `["turn_id"]`},
		{"additional_tools 官方没有该字段", "additional_tools", `{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be"}`},
		{"compaction_trigger 官方没有该字段", "compaction_trigger", `{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be"}`},
		{"configuration_update 官方没有该字段", "configuration_update", `{"content_item_kinds":["x"]}`},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.False(t, isOfficialCodexInputPassthroughForm(item.itemType, gjson.Parse(item.raw)))
			var decoded any
			require.NoError(t, json.Unmarshal([]byte(item.raw), &decoded))
			require.False(t, isOfficialCodexInputPassthroughFormMap(item.itemType, decoded))
		})
	}
	require.True(t, isOfficialCodexInputPassthroughForm("message", gjson.Parse(
		`{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be","create_time":1793034932.5,"content_item_kinds":["a"],"content_item_metadata":[{}]}`)))
	require.True(t, isOfficialCodexInputPassthroughForm("function_call_output", gjson.Parse(
		`{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be","cell_id":"c","executed_tool_calls":[],"tool_calls_complete":true}`)))
}

// 四个条件逐条检查（兼容层判定部分：官方出站、Lite、画像 uuidv7）。
func TestOfficialCodexInputPassthroughPreservedGate(t *testing.T) {
	official := newOfficialOpenAIHTTPTestAccount(9401)
	setupToken := newOfficialOpenAIHTTPTestAccount(9402)
	setupToken.Type = AccountTypeSetupToken
	apiKey := newOfficialOpenAIHTTPTestAccount(9403)
	apiKey.Type = AccountTypeAPIKey

	t.Run("画像声明 uuidv7", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		require.True(t, officialCodexWSInputPassthroughPreserved(p2Config(), official, true), "四条件满足")
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), official, false), "非 Lite")
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), setupToken, true), "SetupToken 不走官方出站")
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), apiKey, true), "API Key 不走官方出站")
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), nil, true))
		require.True(t, officialCodexHTTPInputPassthroughPreserved(officialClientProfileModePrevious, official, true))
		require.False(t, officialCodexHTTPInputPassthroughPreserved(officialClientProfileModePrevious, official, false), "非 Lite")
		require.False(t, officialCodexHTTPInputPassthroughPreserved(officialClientProfileModePrevious, setupToken, true))
	})
	t.Run("画像不声明 uuidv7（0.163 一类的旧画像）", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, nil)
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), official, true))
		require.False(t, officialCodexHTTPInputPassthroughPreserved(officialClientProfileModePrevious, official, true))
	})
	t.Run("画像声明基础指令节但没有 uuidv7", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, func(doc *profilecontract.SnapshotDoc) {
			syntheticNewerProfileMutation(t)(doc)
			section := incrementalPrefixSection()
			section.IDScheme = ""
			doc.BaseInstructions = syntheticServiceRawSection(t, *section)
		})
		require.False(t, officialCodexWSInputPassthroughPreserved(p2Config(), official, true))
	})
}

// 兼容层按项判定：官方形态的项保留，其余项（含 additional_tools、第三方形态）仍剥离；缺条件时整帧照旧全部剥离。
func TestWSCompatibilityLayerPreservesOfficialItemPassthroughOnlyWhenAllConditionsHold(t *testing.T) {
	frame := loadCodex0164Frame(t, "s1_ws_business")
	original := p2Passthroughs(frame)
	require.Greater(t, len(original), 2)
	official := newOfficialOpenAIHTTPTestAccount(9410)
	setupToken := newOfficialOpenAIHTTPTestAccount(9411)
	setupToken.Type = AccountTypeSetupToken

	t.Run("四条件满足：逐项逐字节保留", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		candidate := p2Normalize(t, frame, official, true)
		for index, want := range original {
			got := p2Passthroughs(candidate)[index]
			if !want.Exists() {
				require.False(t, got.Exists(), "input.%d 入站没有 passthrough（additional_tools）", index)
				continue
			}
			require.Equal(t, want.Raw, got.Raw, "input.%d 的 passthrough 必须逐字节原样（含 content_item_metadata）", index)
		}
	})
	t.Run("第三方形态的项仍剥离，其余官方项保留", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		tampered := frame
		var err error
		tampered, err = setRawJSON(tampered, "input.3."+p2PassthroughKey+".foo", `1`)
		require.NoError(t, err)
		tampered, err = setRawJSON(tampered, "input.0."+p2PassthroughKey, `{"turn_id":"01a17935-d08d-77c1-a5d1-3302aa7d65be"}`)
		require.NoError(t, err)
		candidate := p2Normalize(t, tampered, official, true)
		got := p2Passthroughs(candidate)
		require.False(t, got[3].Exists(), "带未知键的项被剥离")
		require.False(t, got[0].Exists(), "additional_tools 官方没有该字段，被剥离")
		for _, index := range []int{1, 2, 4, 5} {
			require.Equal(t, original[index].Raw, got[index].Raw, "input.%d", index)
		}
	})
	t.Run("非 Lite：整体剥离，不留 content_item_metadata", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		candidate := p2Normalize(t, frame, official, false)
		require.NotContains(t, string(candidate), p2PassthroughKey)
		require.NotContains(t, string(candidate), "content_item_metadata")
	})
	t.Run("SetupToken 账号：整体剥离", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		candidate := p2Normalize(t, frame, setupToken, true)
		require.NotContains(t, string(candidate), p2PassthroughKey)
	})
	t.Run("画像不声明 uuidv7：整体剥离（0.163 行为不变）", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, nil)
		candidate := p2Normalize(t, frame, official, true)
		require.NotContains(t, string(candidate), p2PassthroughKey)
	})
	t.Run("透传等不带标志的入口：照旧剥离", func(t *testing.T) {
		withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		stripped, _, err := normalizeOpenAIResponsesCompatibilityBodyWithOptions(frame, official, openAIResponsesCompatibilityOptions{
			Config: p2Config(), ResponsesLite: true,
		})
		require.NoError(t, err)
		require.NotContains(t, string(stripped), p2PassthroughKey)
	})
}

func setRawJSON(body []byte, path, raw string) ([]byte, error) {
	return sjson.SetRawBytes(body, path, []byte(raw))
}

// 首个业务帧：逐项 passthrough 除 turn_id 外逐字节保留，turn_id 被改写成网关本轮的 turn_id（不再是客户端的）。
func TestWSRealCodex0164BusinessFramePreservesItemPassthroughAndRewritesTurnID(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, _ := incWSOfficialContext(t, true, prewarmFrame)
	account := newOfficialOpenAIHTTPTestAccount(94)

	prepare := func(original []byte, previous string) []byte {
		t.Helper()
		candidate := p2Normalize(t, original, account, true)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, candidate, previous, false)
		require.NoError(t, err)
		return final
	}
	prepare(prewarmFrame, "")
	final := prepare(businessFrame, "resp_informal_0001")
	requireBasePrefixVerbatim(t, businessFrame, final, "首个业务帧")

	currentTurn := gjson.GetBytes(final, "client_metadata.turn_id").String()
	_, err := uuid.Parse(currentTurn)
	require.NoError(t, err)
	want := p2Passthroughs(businessFrame)
	got := p2Passthroughs(final)
	require.Len(t, got, len(want))
	for index := 2; index < len(want); index++ {
		p2RequirePassthroughExceptTurnID(t, want[index], got[index], fmt.Sprintf("input.%d", index))
		clientTurn := want[index].Get("turn_id").String()
		require.NotEmpty(t, clientTurn)
		require.Equal(t, currentTurn, got[index].Get("turn_id").String(), "客户端 turn_id 被改写成网关本轮的 turn_id")
		require.NotEqual(t, clientTurn, got[index].Get("turn_id").String())
		// turn_id 仍排在第一位（官方键序），create_time 的小数位原样。
		require.Equal(t, "turn_id", jsonTopLevelKeys(t, got[index].Raw)[0])
	}
}

// 换模型整段重放：多回合历史，客户端 turn_id 与网关对回合的切分不一致；定型不得报冲突，历史各段改写成网关派生的 turn_id，
// 同一段内一致、不同段互异、没有任何一项还留着客户端的 turn_id，其余键逐字节保留；链帧再次定型结果不变。
func TestWSRealCodex0164ModelSwitchReplayRewritesClientTurnIDsToGatewayTurnIDs(t *testing.T) {
	replay := loadCodex0164Frame(t, "m1_ws_model_switch_replay")
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	ctx, _ := incWSOfficialContext(t, true, replay)
	account := newOfficialOpenAIHTTPTestAccount(94)

	candidate := p2Normalize(t, replay, account, true)
	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, replay, candidate, "", false)
	require.NoError(t, err, "客户端自带的 turn_id 与网关的回合切分不同，也不能报 turn_id conflicts")
	requireBasePrefixVerbatim(t, replay, first, "整段重放")

	want := p2Passthroughs(replay)
	got := p2Passthroughs(first)
	require.Len(t, got, len(want))
	clientTurns := map[string]bool{}
	for _, item := range want {
		if item.Exists() {
			clientTurns[item.Get("turn_id").String()] = true
		}
	}
	require.GreaterOrEqual(t, len(clientTurns), 2, "样本是多回合历史")
	currentTurn := gjson.GetBytes(first, "client_metadata.turn_id").String()
	gatewayTurns := map[string]bool{}
	for index := 2; index < len(want); index++ {
		if !want[index].Exists() {
			continue
		}
		p2RequirePassthroughExceptTurnID(t, want[index], got[index], "input")
		turn := got[index].Get("turn_id").String()
		_, parseErr := uuid.Parse(turn)
		require.NoError(t, parseErr, "input.%d", index)
		require.False(t, clientTurns[turn], "input.%d 不应再留着客户端的 turn_id", index)
		gatewayTurns[turn] = true
	}
	require.GreaterOrEqual(t, len(gatewayTurns), 2, "历史各段有各自的网关 turn_id")
	require.True(t, gatewayTurns[currentTurn], "最后一段就是本轮 turn_id")
	last := len(want) - 1
	require.Equal(t, currentTurn, got[last].Get("turn_id").String())

	// 链帧在已定型的多回合历史上再次定型：不报冲突、不再改动逐项 passthrough。
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	chained := p2Passthroughs(chain)
	require.Len(t, chained, len(got))
	for index := range got {
		require.Equal(t, got[index].Raw, chained[index].Raw, "input.%d", index)
	}
}

// 缺条件时 WS 整体行为不变：兼容层剥掉 passthrough，定型补 {"turn_id"}，不出现 create_time／content_item_metadata。
func TestWSRealCodex0164WithoutAllConditionsKeepsLegacyStripping(t *testing.T) {
	prewarmFrame := loadCodex0164Frame(t, "s1_ws_prewarm")
	businessFrame := loadCodex0164Frame(t, "s1_ws_business")
	cases := []struct {
		name    string
		mutate  func(t *testing.T)
		account func() *Account
		lite    bool
	}{
		{"画像不声明 uuidv7", func(t *testing.T) { withOfficialCodexSyntheticPreviousProfile(t, nil) }, func() *Account { return newOfficialOpenAIHTTPTestAccount(9420) }, true},
		{"非 Lite", func(t *testing.T) {
			withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
		}, func() *Account { return newOfficialOpenAIHTTPTestAccount(9421) }, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			item.mutate(t)
			ctx, _ := incWSOfficialContext(t, item.lite, prewarmFrame)
			account := item.account()
			prepare := func(original []byte, previous string) []byte {
				candidate := p2Normalize(t, original, account, item.lite)
				final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, original, candidate, previous, false)
				require.NoError(t, err)
				return final
			}
			prepare(prewarmFrame, "")
			final := prepare(businessFrame, "resp_informal_0001")
			require.NotContains(t, string(final), "content_item_metadata\":[{\"provenance\":{\"type\":\"harness\",\"hook\":false}}],\"turn_id", "没有逐项 passthrough 保留")
			currentTurn := gjson.GetBytes(final, "client_metadata.turn_id").String()
			clientTurn := gjson.GetBytes(businessFrame, "input.2."+p2PassthroughKey+".turn_id").String()
			require.NotEmpty(t, clientTurn)
			for index, passthrough := range p2Passthroughs(final) {
				if !passthrough.Exists() {
					continue
				}
				if strings.Contains(passthrough.Raw, "model.base_instructions") {
					continue // 基础指令消息（画像声明时）按基础指令逻辑处理，不属于逐项 passthrough
				}
				require.Equal(t, `{"turn_id":"`+currentTurn+`"}`, passthrough.Raw, "input.%d：照旧只补网关的 turn_id", index)
			}
		})
	}
}

// 工具输出的当前轮判定不受客户端 turn_id 影响（客户端 turn_id 清空后由网关身份回填，再比较）。
func TestNeutralizeOfficialOpenAIWSClientTurnIDsValidatesThenBlanks(t *testing.T) {
	section := incrementalPrefixSection()
	isBase := func(item map[string]any) bool { return isOfficialCodexBaseInstructionsItem(section, item) }
	build := func(turn any) map[string]any {
		return map[string]any{"input": []any{
			map[string]any{"type": "message", "role": "user", p2PassthroughKey: map[string]any{"turn_id": turn, "create_time": json.Number("1.5")}},
			map[string]any{"type": "additional_tools", "id": "at_x", "role": "developer", "tools": []any{}},
			map[string]any{"type": "message", "role": "user"},
		}}
	}
	good := build("01a17935-d08d-77c1-a5d1-3302aa7d65be")
	modified, err := neutralizeOfficialOpenAIWSClientTurnIDs(good, isBase)
	require.NoError(t, err)
	require.True(t, modified)
	passthrough := good["input"].([]any)[0].(map[string]any)[p2PassthroughKey].(map[string]any)
	require.Equal(t, "", passthrough["turn_id"])
	require.Equal(t, json.Number("1.5"), passthrough["create_time"], "其余键不动")

	_, err = neutralizeOfficialOpenAIWSClientTurnIDs(build("not-a-uuid"), isBase)
	require.ErrorContains(t, err, "turn_id must be UUID", "校验不放宽")
	_, err = neutralizeOfficialOpenAIWSClientTurnIDs(build(7), isBase)
	require.ErrorContains(t, err, "turn_id must be a string")

	empty := build("")
	modified, err = neutralizeOfficialOpenAIWSClientTurnIDs(empty, isBase)
	require.NoError(t, err)
	require.False(t, modified)
}

// HTTP：字段路径与字节路径都按项保留官方形态，缺条件照旧剥离；对真实 0.164 HTTP 请求，定型后逐项 passthrough 与入站逐字节相同。
func TestHTTPRealCodex0164PreservesItemPassthrough(t *testing.T) {
	for _, name := range []string{"s2_http_lite", "r1_http_legacy_window"} {
		t.Run(name, func(t *testing.T) {
			withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
			inbound := loadCodex0164Frame(t, name)
			want := p2Passthroughs(inbound)

			// 字节路径
			kept, _, err := normalizeOpenAIOAuthResponsesCompatibilityBodyWithOptions(inbound, true)
			require.NoError(t, err)
			require.Equal(t, passthroughRaws(want), passthroughRaws(p2Passthroughs(kept)), "字节路径逐项保留")
			stripped, _, err := normalizeOpenAIOAuthResponsesCompatibilityBodyWithOptions(inbound, false)
			require.NoError(t, err)
			require.NotContains(t, string(stripped), p2PassthroughKey)

			// 字段路径（线上 HTTP 转发走这里）
			decode := func() map[string]any {
				var decoded map[string]any
				decoder := json.NewDecoder(bytes.NewReader(inbound))
				decoder.UseNumber()
				require.NoError(t, decoder.Decode(&decoded))
				return decoded
			}
			wantMaps := map[int]any{}
			for index, item := range decode()["input"].([]any) {
				if value, ok := item.(map[string]any)[p2PassthroughKey]; ok {
					wantMaps[index] = value
				}
			}
			require.NotEmpty(t, wantMaps)
			transformed := decode()
			require.True(t, stashOfficialInputPassthroughForTransform(transformed))
			result := applyCodexOAuthTransformWithOptions(transformed, codexOAuthTransformOptions{
				SkipDefaultInstructions: true, PreserveToolCallIDs: true,
			})
			require.NoError(t, result.Error)
			restoreOfficialInputPassthroughAfterTransform(transformed)
			for index, value := range wantMaps {
				items := transformed["input"].([]any)
				require.Equal(t, value, items[index].(map[string]any)[p2PassthroughKey], "字段路径 input.%d 保留", index)
			}
			for _, item := range transformed["input"].([]any) {
				require.NotContains(t, item.(map[string]any), officialCodexStashedInputPassthroughKey, "私有暂存键不残留")
			}
			transformedOff := decode()
			result = applyCodexOAuthTransformWithOptions(transformedOff, codexOAuthTransformOptions{
				SkipDefaultInstructions: true, PreserveToolCallIDs: true,
			})
			require.NoError(t, result.Error)
			for _, item := range transformedOff["input"].([]any) {
				require.NotContains(t, item.(map[string]any), p2PassthroughKey)
			}

			// 定型：官方形态入站经 HTTP 定型后，逐项 passthrough 仍与入站逐字节相同。
			final, _ := finalizeIncrementalHTTP(t, true, inbound, kept)
			require.Equal(t, passthroughRaws(want), passthroughRaws(p2Passthroughs(final)), "HTTP 定型后逐项 passthrough 逐字节相同")
		})
	}
}

func passthroughRaws(values []gjson.Result) []string {
	out := make([]string, len(values))
	for index, value := range values {
		out[index] = value.Raw
	}
	return out
}

// HTTP 官方出站：保留下来的逐项 turn_id 与 client_metadata.turn_id 用同一个函数作用域化，当前回合各项仍等于 client_metadata.turn_id。
func TestApplyCodexAccountIdentityInputTurnIDsMapMatchesClientMetadataScoping(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	section := incrementalPrefixSection()
	isBase := func(item map[string]any) bool { return isOfficialCodexBaseInstructionsItem(section, item) }
	account := newOfficialOpenAIHTTPTestAccount(9430)
	const apiKeyID = int64(77)
	const currentTurn = "01a17935-d08d-77c1-a5d1-3302aa7d65be"
	const oldTurn = "01a17935-d1b8-7dc0-8cbd-0c67d81f4828"
	build := func() map[string]any {
		return map[string]any{
			"client_metadata": map[string]any{"turn_id": currentTurn},
			"input": []any{
				map[string]any{"type": "additional_tools", "id": "at_x", "role": "developer", "tools": []any{}},
				map[string]any{"type": "message", "role": "developer", p2PassthroughKey: map[string]any{
					"turn_id": oldTurn, "content_item_kinds": []any{"model.base_instructions"}}},
				map[string]any{"type": "message", "role": "user", p2PassthroughKey: map[string]any{"turn_id": oldTurn, "create_time": json.Number("1.5")}},
				map[string]any{"type": "message", "role": "user", p2PassthroughKey: map[string]any{"turn_id": currentTurn}},
				map[string]any{"type": "message", "role": "user"},
			},
		}
	}
	body := build()
	require.True(t, applyCodexAccountIdentityClientMetadataMap(body, account, apiKeyID))
	require.True(t, applyCodexAccountIdentityInputTurnIDsMap(body, account, apiKeyID, isBase))
	scopedCurrent := body["client_metadata"].(map[string]any)["turn_id"].(string)
	require.NotEqual(t, currentTurn, scopedCurrent)
	items := body["input"].([]any)
	turnOf := func(index int) string {
		return items[index].(map[string]any)[p2PassthroughKey].(map[string]any)["turn_id"].(string)
	}
	require.Equal(t, oldTurn, turnOf(1), "基础指令消息不改写")
	require.Equal(t, scopedCurrent, turnOf(3), "当前回合各项等于 client_metadata.turn_id")
	require.Equal(t, scopeCodexAccountIdentityValue(account, apiKeyID, "turn", oldTurn), turnOf(2), "历史回合同函数改写")
	require.NotEqual(t, oldTurn, turnOf(2))
	require.Equal(t, json.Number("1.5"), items[2].(map[string]any)[p2PassthroughKey].(map[string]any)["create_time"], "其余键不动")

	// 无账号命名空间（没有 chatgpt_account_id 等）时恒等。
	bare := newOfficialOpenAIHTTPTestAccount(9431)
	bare.Credentials = map[string]any{"access_token": "t"}
	untouched := build()
	require.False(t, applyCodexAccountIdentityInputTurnIDsMap(untouched, bare, apiKeyID, isBase))
	require.Equal(t, build(), untouched)
}

// HTTP 整条转发链路：官方出站 Lite、画像声明 uuidv7 时，上游收到的逐项 passthrough 保留（除 turn_id 外逐字节相同），
// turn_id 经账号作用域改写（与入站身份值同一函数）；缺条件（画像不声明 uuidv7）时照旧全部剥离。
func TestOpenAIGatewayForwardOfficialEgressHTTPPreservesItemPassthrough(t *testing.T) {
	const passthroughRaw = `{"turn_id":"` + testOfficialOpenAITurnID + `","create_time":1793034932.5448666,` +
		`"content_item_kinds":["model.contextual"],"content_item_metadata":[{"provenance":{"type":"harness","hook":false}}]}`
	build := func(t *testing.T) []byte {
		body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
		var err error
		body, err = sjson.SetRawBytes(body, "input.1."+p2PassthroughKey, []byte(passthroughRaw))
		require.NoError(t, err)
		body, err = sjson.SetRawBytes(body, "input.2."+p2PassthroughKey, []byte(passthroughRaw))
		require.NoError(t, err)
		return body
	}
	forward := func(t *testing.T) []byte {
		body := build(t)
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		upstream := &httpUpstreamRecorder{responses: []*http.Response{
			newOfficialOpenAIHTTPJSONResponse(http.StatusOK,
				`{"id":"resp_p2","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`),
		}}
		result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(context.Background(), c, newOfficialOpenAIHTTPTestAccount(94), body)
		require.NoError(t, err)
		require.NotNil(t, result)
		require.Len(t, upstream.bodies, 1)
		return upstream.bodies[0]
	}

	t.Run("四条件满足", func(t *testing.T) {
		withOfficialCodexSyntheticProfile(t, syntheticIncrementalPrefixMutation(t))
		sent := forward(t)
		// 逐项 turn_id 按账号命名空间作用域化（与入站身份值的处理同一函数）；网关对出站 client_metadata.turn_id 另行重新派生，
		// 两者目前并不相等，这是 HTTP 侧已知差距（见交回报告），这里只锁定当前行为。
		wantTurn := scopeCodexAccountIdentityValue(newOfficialOpenAIHTTPTestAccount(94), 0, "turn", testOfficialOpenAITurnID)
		require.NotEqual(t, testOfficialOpenAITurnID, wantTurn)
		for _, index := range []string{"input.1", "input.2"} {
			passthrough := gjson.GetBytes(sent, index+"."+p2PassthroughKey)
			require.True(t, passthrough.Exists(), index)
			p2RequirePassthroughExceptTurnID(t, gjson.Parse(passthroughRaw), passthrough, index)
			require.Equal(t, wantTurn, passthrough.Get("turn_id").String(), index)
			require.NotContains(t, passthrough.Raw, officialCodexStashedInputPassthroughKey)
		}
		require.NotContains(t, string(sent), officialCodexStashedInputPassthroughKey)
	})
	t.Run("画像不声明 uuidv7：照旧剥离", func(t *testing.T) {
		withOfficialCodexSyntheticProfile(t, nil)
		sent := forward(t)
		require.NotContains(t, string(sent), p2PassthroughKey)
	})
}
