package service

import (
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 官方 Codex 较新版本默认开启 reasoning_effort_override：会话内改 effort 时，客户端在 input 里追加
// {"type":"configuration_update","reasoning":{"effort":"…"}}，请求级 reasoning.effort 在一个上下文窗口内保持基线。
// 本文件锁定网关对这种项的透传：HTTP 与 WS 两条出站路径都必须原样放行（键集合只有 type、reasoning，不补
// internal_chat_message_metadata_passthrough，不改位置，不影响其余各项的定型结果）。
//
// 做法：对真实官方请求（testdata/codex_0164_informal）在 input 里插入该项，与未插入的基线各跑一遍同样的定型流程，
// 要求“带项结果 = 基线结果 + 原位原字节的该项”，其余项一字不差。

const (
	cuUpdateLow    = `{"type":"configuration_update","reasoning":{"effort":"low"}}`
	cuUpdateMedium = `{"type":"configuration_update","reasoning":{"effort":"medium"}}`
	cuUpdateHigh   = `{"type":"configuration_update","reasoning":{"effort":"high"}}`
	cuUpdateXHigh  = `{"type":"configuration_update","reasoning":{"effort":"xhigh"}}`
)

// cuInsertInputItem 把 raw 插到 frame.input 的 index 位置（index==len 即追加），其余各项原文不动。
func cuInsertInputItem(t *testing.T, frame []byte, index int, raw string) []byte {
	t.Helper()
	items := inputRaws(frame)
	require.LessOrEqual(t, index, len(items))
	merged := make([]string, 0, len(items)+1)
	merged = append(merged, items[:index]...)
	merged = append(merged, raw)
	merged = append(merged, items[index:]...)
	updated, err := sjson.SetRawBytes(frame, "input", []byte("["+strings.Join(merged, ",")+"]"))
	require.NoError(t, err)
	return updated
}

// requireCUInputIsBaselinePlusUpdates 要求 got 的 input 等于 baseline 的 input 在 positions 位置依次插入 updates 之后的结果，
// 所有项（含被插入项）逐字节相同。positions 是各项在 got 里的下标，升序。
//
// ignoreTurnIDs 为真时，非 configuration_update 项比较前先去掉 passthrough.turn_id：在历史中间插入一项会让其后各用户消息的
// 下标位移，而网关派生历史回合 turn_id 的种子带用户消息下标，所以后续各段的 turn_id 值随之不同（真实会话里该项出现在每次请求
// 的同一位置，下标稳定，不受影响）。
func requireCUInputIsBaselinePlusUpdates(t *testing.T, baseline, got []byte, positions []int, updates []string, ignoreTurnIDs bool, label string) {
	t.Helper()
	comparable := func(raw string) string {
		if !ignoreTurnIDs {
			return raw
		}
		stripped, err := sjson.Delete(raw, "internal_chat_message_metadata_passthrough.turn_id")
		require.NoError(t, err)
		return stripped
	}
	base := inputRaws(baseline)
	actual := inputRaws(got)
	require.Len(t, actual, len(base)+len(updates), label)
	skip := map[int]string{}
	for i, position := range positions {
		skip[position] = updates[i]
	}
	baseIndex := 0
	for index, raw := range actual {
		if update, isUpdate := skip[index]; isUpdate {
			require.True(t, raw == update, "%s：input.%d 必须是原字节的 configuration_update\nwant %s\n got %s", label, index, update, raw)
			require.Equal(t, []string{"type", "reasoning"}, jsonTopLevelKeys(t, raw), "%s：键集合只有 type、reasoning", label)
			continue
		}
		require.True(t, comparable(raw) == comparable(base[baseIndex]), "%s：input.%d 必须与基线 input.%d 逐字节相同\nwant %.200s\n got %.200s", label, index, baseIndex, base[baseIndex], raw)
		baseIndex++
	}
	require.Equal(t, gjson.GetBytes(baseline, "reasoning").Raw, gjson.GetBytes(got, "reasoning").Raw, "%s：请求级 reasoning 不被 configuration_update 改写", label)
}

func runCUHTTP(t *testing.T, lite bool, inbound []byte) []byte {
	t.Helper()
	normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(inbound)
	require.NoError(t, err)
	final, _ := finalizeIncrementalHTTP(t, lite, inbound, normalized)
	return final
}

// HTTP 出站：真实官方请求（增量窗口、旧窗口多回合、非 Lite）在 input 末尾或历史中间带 configuration_update，
// 定型后该项原样保留且不补 passthrough，其余项与未带该项时完全一致。
func TestFinalizeOfficialOpenAIHTTPBodyKeepsConfigurationUpdateVerbatim(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	cases := []struct {
		name string
		file string
		lite bool
		// 插入位置（相对入站 input 下标）；负数表示追加到末尾。
		positions []int
		// 是否在历史中间插入（其后各段历史 turn_id 的派生种子随下标位移）。
		midHistory bool
	}{
		{"增量窗口 Lite 末尾", "s2_http_lite", true, []int{-1}, false},
		{"旧窗口多回合 Lite 末尾", "r1_http_legacy_window", true, []int{-1}, false},
		{"旧窗口多回合 Lite 历史中间", "r1_http_legacy_window", true, []int{7}, true},
		{"旧窗口多回合 Lite 中间加末尾", "r1_http_legacy_window", true, []int{7, -1}, true},
		{"非 Lite 末尾", "s7_http_nonlite", false, []int{-1}, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			inbound := loadCodex0164Frame(t, item.file)
			baseline := runCUHTTP(t, item.lite, inbound)

			with := inbound
			var updates []string
			var positions []int
			for i, position := range item.positions {
				length := len(inputRaws(with))
				if position < 0 {
					position = length
				}
				update := []string{cuUpdateHigh, cuUpdateMedium}[i%2]
				with = cuInsertInputItem(t, with, position, update)
				updates = append(updates, update)
				positions = append(positions, position)
			}
			final := runCUHTTP(t, item.lite, with)
			requireCUInputIsBaselinePlusUpdates(t, baseline, final, positions, updates, item.midHistory, item.name)
			for _, position := range positions {
				require.False(t, gjson.GetBytes(final, "input."+strconv.Itoa(position)+".internal_chat_message_metadata_passthrough").Exists(),
					"官方 configuration_update 项没有 passthrough 字段，不能被补 turn_id")
			}
		})
	}
}

// runCUWSFrames 在同一条官方 WS 连接上依次定型 frames（每帧给出期望的 previous_response_id），返回各帧定型结果。
func runCUWSFrames(t *testing.T, firstPayload []byte, frames []cuWSFrame) [][]byte {
	t.Helper()
	ctx, _ := incWSOfficialContext(t, true, firstPayload)
	results := make([][]byte, 0, len(frames))
	for _, frame := range frames {
		candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(frame.body)
		require.NoError(t, err)
		final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, frame.body, candidate, frame.expectedPrevious, false)
		require.NoError(t, err)
		results = append(results, final)
	}
	return results
}

type cuWSFrame struct {
	body             []byte
	expectedPrevious string
}

// WS 出站：真实官方首个业务帧（预热之后、previous_response_id 续链）在 input 末尾带 configuration_update，
// 与基线一致地定型，该项原字节保留，不补 passthrough。
func TestWSOfficialClientKeepsConfigurationUpdateVerbatimOnBusinessFrame(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	prewarm := loadCodex0164Frame(t, "s1_ws_prewarm")
	business := loadCodex0164Frame(t, "s1_ws_business")

	baseline := runCUWSFrames(t, prewarm, []cuWSFrame{{prewarm, ""}, {business, "resp_informal_0001"}})[1]
	with := cuInsertInputItem(t, business, len(inputRaws(business)), cuUpdateHigh)
	final := runCUWSFrames(t, prewarm, []cuWSFrame{{prewarm, ""}, {with, "resp_informal_0001"}})[1]

	requireCUInputIsBaselinePlusUpdates(t, baseline, final, []int{len(inputRaws(business))}, []string{cuUpdateHigh}, false, "WS 首个业务帧")
	require.Equal(t, "resp_informal_0001", gjson.GetBytes(final, "previous_response_id").String())
	require.Equal(t, gjson.GetBytes(baseline, "client_metadata.turn_id").Exists(), gjson.GetBytes(final, "client_metadata.turn_id").Exists())
}

// WS 出站：整段重放（无 previous_response_id，多回合历史，换模型后的形态）里，历史中间和末尾各带一条 configuration_update，
// 与基线一致地定型；经网关补预热、链帧后仍原样保留。
func TestWSOfficialClientKeepsConfigurationUpdateVerbatimOnFullHistoryReplay(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	replay := loadCodex0164Frame(t, "m1_ws_model_switch_replay")

	// 历史中间（第一轮用户消息之后、助手回答之前）与末尾（最后一条用户消息之后）。
	with := cuInsertInputItem(t, replay, 7, cuUpdateMedium)
	with = cuInsertInputItem(t, with, len(inputRaws(with)), cuUpdateHigh)

	baselineFrames := runCUWSFrames(t, replay, []cuWSFrame{{replay, ""}})
	frames := runCUWSFrames(t, with, []cuWSFrame{{with, ""}})
	requireCUInputIsBaselinePlusUpdates(t, baselineFrames[0], frames[0],
		[]int{7, len(inputRaws(replay)) + 1}, []string{cuUpdateMedium, cuUpdateHigh}, true, "WS 整段重放")

	// 网关补预热后链帧：业务帧保留整段历史，两条 configuration_update 原位原字节。
	ctx, _ := incWSOfficialContext(t, true, with)
	candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(with)
	require.NoError(t, err)
	first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, with, candidate, "", false)
	require.NoError(t, err)
	_, inject, err := buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, first)
	require.NoError(t, err)
	require.True(t, inject)
	chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, wsBasePrewarmResp)
	require.NoError(t, err)
	chainItems := inputRaws(chain)
	require.Len(t, chainItems, len(inputRaws(with)))
	require.True(t, chainItems[7] == cuUpdateMedium, "链帧历史中间的 configuration_update 原字节：%s", chainItems[7])
	require.True(t, chainItems[len(chainItems)-1] == cuUpdateHigh, "链帧末尾的 configuration_update 原字节：%s", chainItems[len(chainItems)-1])
}

// WS 出站：续链增量帧（previous_response_id 加新增项）的 input 只有“新用户消息 + configuration_update”，
// 不因缺少前缀而被补基础指令，也不给 configuration_update 补 turn_id；连续两帧各带一条也互不影响。
func TestWSOfficialClientKeepsConfigurationUpdateVerbatimOnIncrementalFrames(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	prewarmFrame := []byte(strings.Replace(string(incWSFrame(``, "")), `"input":[]}`, `"input":[],"generate":false}`, 1))
	first := incWSFrame(``, "resp_inc_warm", incOfficialTools, incOfficialBase, incOfficialContext, incOfficialUser)
	second := incWSFrame(``, "resp_inc_turn1", incOfficialUser, cuUpdateHigh)
	third := incWSFrame(``, "resp_inc_turn2", incOfficialUser, cuUpdateLow)

	results := runCUWSFrames(t, prewarmFrame, []cuWSFrame{
		{prewarmFrame, ""}, {first, "resp_inc_warm"}, {second, "resp_inc_turn1"}, {third, "resp_inc_turn2"},
	})
	for index, expected := range []string{cuUpdateHigh, cuUpdateLow} {
		items := inputRaws(results[2+index])
		require.Len(t, items, 2, "增量帧 %d", index)
		require.True(t, items[1] == expected, "增量帧 %d 的 configuration_update 原字节：%s", index, items[1])
		require.Equal(t, []string{"message:user", "configuration_update:"}, describeWSInput(results[2+index]))
		require.NotContains(t, string(results[2+index]), "model.base_instructions")
	}
}

// 目标画像仍是旧版本（候选期、回退）而入站已是较新官方客户端时：configuration_update 同样不能被补 passthrough。
// 旧版本客户端从不发这种项，没有历史字节需要保持，所以不受“旧画像保持原行为”约束。
func TestWSLegacyProfileKeepsConfigurationUpdateWithoutPassthrough(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, nil)
	require.Nil(t, synthetic.Optional().BaseInstructions, "旧画像不声明基础指令节")
	first := []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[` + baseInstructionsUserItem + `,` + cuUpdateHigh + `],` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]}`)
	ctx := newWSLegacyOfficialIngressContext(t, officialClientProfileModePrevious, first)
	candidate, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(first)
	require.NoError(t, err)
	final, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, first, candidate, "", false)
	require.NoError(t, err)

	updates, users := 0, 0
	for _, item := range gjson.GetBytes(final, "input").Array() {
		switch item.Get("type").String() {
		case "configuration_update":
			updates++
			require.True(t, item.Raw == cuUpdateHigh, "configuration_update 原字节：%s", item.Raw)
		case "message":
			if item.Get("role").String() == "user" {
				users++
				require.True(t, item.Get("internal_chat_message_metadata_passthrough.turn_id").Exists(), "普通消息照旧补 turn_id")
			}
		}
	}
	require.Equal(t, 1, updates)
	require.Equal(t, 1, users)
}

// WS 每帧的 turn metadata：reasoning_effort 记录用户当前选择（最后一条 configuration_update），而不是被钉死的请求级基线；
// 增量帧靠连接状态沿用，整段重放帧回到新基线。
func TestOfficialOpenAIWSFrameTurnMetadataFollowsConfigurationUpdateEffort(t *testing.T) {
	withOfficialCodexSyntheticProfile(t, turnMetadataTargetMutation(t))
	egressContext := newTurnMetadataWSTestContext(t)
	user := map[string]any{
		"type": "message", "role": "user",
		"content": []any{map[string]any{"type": "input_text", "text": "hi"}},
	}
	update := func(effort string) map[string]any {
		return map[string]any{"type": "configuration_update", "reasoning": map[string]any{"effort": effort}}
	}
	frame := func(previous string, items ...any) map[string]any {
		payload := map[string]any{
			"type": "response.create", "model": "gpt-5.6-luna",
			"reasoning": map[string]any{"effort": "medium"}, "input": items,
		}
		if previous != "" {
			payload["previous_response_id"] = previous
		}
		return payload
	}
	effortOf := func(payload map[string]any) string {
		t.Helper()
		metadata, _, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(egressContext, payload, false)
		require.NoError(t, err)
		turn, ok := metadata["x-codex-turn-metadata"].(string)
		require.True(t, ok)
		return gjson.Get(turn, "reasoning_effort").String()
	}

	require.Equal(t, "medium", effortOf(frame("", user)), "没改档：请求级基线")
	require.Equal(t, "high", effortOf(frame("resp_1", user, update("high"))), "中途升到 high：turn metadata 记 high，请求级仍是 medium")
	require.Equal(t, "high", effortOf(frame("resp_2", user)), "增量帧不带 update：沿用之前的 high")
	require.Equal(t, "low", effortOf(frame("resp_3", user, update("low"))))
	require.Equal(t, "medium", effortOf(frame("", user)), "整段重放（新窗口）：回到新基线")
	require.Equal(t, "medium", effortOf(frame("resp_4", user)), "清掉之后增量帧取请求级基线")
}

// 端到端：官方 Codex 的 HTTP Responses 请求（请求级 effort 是钉死的基线 high）在历史中带两条 configuration_update（最后一条 low），
// 经完整的 Forward 与官方出站定型发往上游后：两条 update 在上游正文里原字节、原位置保留；转发结果里的 effort 是用户当前选择 low。
func TestOfficialForwardHTTPKeepsConfigurationUpdateAndReportsEffectiveEffort(t *testing.T) {
	tc := officialForwardBodyCase{
		name: "configuration_update_mid_window",
		body: func(t *testing.T) []byte {
			// 用原文插入，保证 update 项的键序是官方序（type、reasoning）；测试夹具自己的重编码会把键排成字典序。
			body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
			body = cuInsertInputItem(t, body, len(inputRaws(body)), cuUpdateMedium)
			body = cuInsertInputItem(t, body, len(inputRaws(body)), `{"type":"message","role":"user","content":"继续"}`)
			return cuInsertInputItem(t, body, len(inputRaws(body)), cuUpdateLow)
		},
		context: func(_ *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		},
	}
	// 正文工作区开启与关闭两条路径的上游 wire 字节必须逐字节一致。
	outcome := officialForwardBodyCompare(t, tc)
	require.NoError(t, outcome.err)
	require.Len(t, outcome.calls, 1)

	sent := gjson.GetBytes(outcome.calls[0].decoded, "input").Array()
	var updates []string
	for _, item := range sent {
		if item.Get("type").String() == "configuration_update" {
			updates = append(updates, item.Raw)
		}
	}
	require.Equal(t, []string{
		`{"type":"configuration_update","reasoning":{"effort":"medium"}}`,
		`{"type":"configuration_update","reasoning":{"effort":"low"}}`,
	}, updates, "上游收到的 configuration_update 原字节、原顺序，不带 passthrough")
	require.Equal(t, "configuration_update", sent[len(sent)-1].Get("type").String(), "最后一项仍是 update")
	require.Equal(t, "high", gjson.GetBytes(outcome.calls[0].decoded, "reasoning.effort").String(), "请求级 effort 不被 update 改写")

	require.NotNil(t, outcome.result)
	require.NotNil(t, outcome.result.ReasoningEffort)
	require.Equal(t, "low", *outcome.result.ReasoningEffort, "用量日志与计费读用户当前选择，而不是钉死的基线")
}

// ---- 第三方入站的 HTTP 出站合成（画像 ReasoningEffort.ConfigurationUpdate=http_first_turn） ----
//
// 官方依据（X 路实采）：有 WS 预热的路径首轮不带 update；名为 OpenAI、不带 WS 预热的 provider 首轮 input 末尾必带
// {"type":"configuration_update","reasoning":{"effort":"<请求 effort>"}}；目录不声明支持的模型、数字档位都不产生。
// 网关 HTTP 出站对应后者：第三方入站合成，位置是首个用户轮次之后、第一个模型输出项之前；WS 出站不合成；官方入站原样。

const (
	cuAssistantItem = `{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}`
	cuCallItem      = `{"type":"function_call","call_id":"call_cu_1","name":"read_file","arguments":"{}"}`
	cuCallOutput    = `{"type":"function_call_output","call_id":"call_cu_1","output":"x"}`
	cuSolManifest   = `{"models":[{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":true,` +
		`"supports_parallel_tool_calls":true,"default_reasoning_level":"low","supports_reasoning_effort_updates":true}]}`
)

// cuSynthMutation 在“增量窗口”合成画像上声明 ReasoningEffort.ConfigurationUpdate=http_first_turn。
func cuSynthMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	base := syntheticIncrementalPrefixMutation(t)
	return func(doc *profilecontract.SnapshotDoc) {
		base(doc)
		doc.ReasoningEffort = syntheticServiceRawSection(t, profilecontract.ReasoningEffortSection{
			CustomNumericSerialization: "u64_integer",
			ConfigurationUpdate:        profilecontract.ReasoningEffortConfigurationUpdateHTTPFirstTurn,
		})
	}
}

func cuThirdPartyBody(effort string, items ...string) []byte {
	return []byte(`{"model":"gpt-6.1-sol","instructions":"第三方指令","input":[` + strings.Join(items, ",") + `],` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],` +
		`"tool_choice":"auto","reasoning":{"effort":` + effort + `},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
}

var cuSynthEnabled = officialOpenAIReasoningDefaults{
	Effort: "low", Known: true, SupportsEffortUpdates: true, ConfigurationUpdateHTTPFirstTurn: true,
}

func cuFinalizeHTTP(t *testing.T, inbound []byte, defaults officialOpenAIReasoningDefaults, allow bool, compact bool) []byte {
	t.Helper()
	normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(inbound)
	require.NoError(t, err)
	contract, err := captureOfficialOpenAIHTTPBodyContract(inbound)
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(
		nil, newOfficialOpenAIHTTPTestAccount(94110), normalized, contract, officialClientProfileModePrevious)
	require.NoError(t, err)
	final, _, err := finalizeOfficialOpenAIHTTPBody(normalized, contract, identity, defaults, officialOpenAIHTTPBodyOptions{
		ProfileMode: officialClientProfileModePrevious, UseResponsesLite: true, SupportsParallelTools: true,
		ThreadID: identity.threadID, TurnID: identity.turnID, AllowConfigurationUpdate: allow, IsCompact: compact,
	})
	require.NoError(t, err)
	return final
}

// requireCUSynthesized 要求 got 的 input 等于 baseline 的 input 在 position 处插入官方形态的 update(effort)，其余项逐字节相同。
func requireCUSynthesized(t *testing.T, baseline, got []byte, position int, effort string, label string) {
	t.Helper()
	update := `{"type":"configuration_update","reasoning":{"effort":"` + effort + `"}}`
	requireCUInputIsBaselinePlusUpdates(t, baseline, got, []int{position}, []string{update}, false, label)
}

func TestHTTPEgressSynthesizesConfigurationUpdateForThirdPartyIngress(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	cases := []struct {
		name     string
		items    []string
		effort   string
		position func(baseline []byte) int // 合成项在最终 input 里的下标
	}{
		{"首轮在 input 末尾", []string{baseInstructionsUserItem}, "medium", func(b []byte) int { return len(inputRaws(b)) }},
		{"首轮 xhigh", []string{baseInstructionsUserItem}, "xhigh", func(b []byte) int { return len(inputRaws(b)) }},
		{"首轮 max", []string{baseInstructionsUserItem}, "max", func(b []byte) int { return len(inputRaws(b)) }},
		// 后续轮次：第三方客户端重放历史，update 留在首个用户轮次之后、第一个助手输出之前（官方历史里的位置）。
		{"第二轮在首轮用户消息之后", []string{baseInstructionsUserItem, cuAssistantItem, baseInstructionsUserItem}, "medium",
			func(b []byte) int { return len(inputRaws(b)) - 2 }},
		{"首轮含工具调用时在调用之前", []string{baseInstructionsUserItem, cuCallItem, cuCallOutput, cuAssistantItem}, "medium",
			func(b []byte) int { return len(inputRaws(b)) - 3 }},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			inbound := cuThirdPartyBody(`"`+item.effort+`"`, item.items...)
			baseline := cuFinalizeHTTP(t, inbound, officialOpenAIReasoningDefaults{Effort: "low", Known: true}, true, false)
			got := cuFinalizeHTTP(t, inbound, cuSynthEnabled, true, false)
			requireCUSynthesized(t, baseline, got, item.position(baseline), item.effort, item.name)
		})
	}
}

// 同一窗口各请求的 input 是前缀关系：首轮请求的整段 input（含末尾的 update）是第二轮请求 input 的前缀，上游前缀缓存不被合成项破坏。
func TestHTTPEgressConfigurationUpdateKeepsRequestsPrefixStableWithinWindow(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	first := cuFinalizeHTTP(t, cuThirdPartyBody(`"medium"`, baseInstructionsUserItem), cuSynthEnabled, true, false)
	second := cuFinalizeHTTP(t, cuThirdPartyBody(`"medium"`, baseInstructionsUserItem, cuAssistantItem, baseInstructionsUserItem), cuSynthEnabled, true, false)
	firstItems, secondItems := inputRaws(first), inputRaws(second)
	require.Greater(t, len(secondItems), len(firstItems))
	for index, raw := range firstItems {
		require.True(t, raw == secondItems[index], "input.%d 在第二轮必须与首轮逐字节相同（前缀稳定）\nfirst  %.160s\nsecond %.160s", index, raw, secondItems[index])
	}
	require.Equal(t, `{"type":"configuration_update","reasoning":{"effort":"medium"}}`, firstItems[len(firstItems)-1])
	require.Equal(t, 1, strings.Count(string(second), `"configuration_update"`), "只有一条")
}

func TestHTTPEgressDoesNotSynthesizeConfigurationUpdateOutsideItsGates(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	plainBody := cuThirdPartyBody(`"medium"`, baseInstructionsUserItem)
	unsupported := cuSynthEnabled
	unsupported.SupportsEffortUpdates = false
	notDeclared := cuSynthEnabled
	notDeclared.ConfigurationUpdateHTTPFirstTurn = false
	numeric := cuSynthEnabled
	numeric.NumericEffortAsInteger = true

	cases := []struct {
		name     string
		body     []byte
		defaults officialOpenAIReasoningDefaults
		allow    bool
		compact  bool
	}{
		{"模型目录不支持（gpt-5.6-* 一类）", plainBody, unsupported, true, false},
		{"画像未声明（旧版本画像）", plainBody, notDeclared, true, false},
		{"调用方不允许（子代理、guardian、记忆合并）", plainBody, cuSynthEnabled, false, false},
		{"数字档位（字符串）", cuThirdPartyBody(`"3"`, baseInstructionsUserItem), cuSynthEnabled, true, false},
		{"数字档位（整数画像）", cuThirdPartyBody(`"3"`, baseInstructionsUserItem), numeric, true, false},
		{"入站已带 update", cuThirdPartyBody(`"medium"`, baseInstructionsUserItem, cuUpdateHigh), cuSynthEnabled, true, false},
		{"压缩后的新窗口", cuThirdPartyBody(`"medium"`, baseInstructionsUserItem, `{"type":"compaction","encrypted_content":"x"}`, baseInstructionsUserItem), cuSynthEnabled, true, false},
		{"压缩请求", cuThirdPartyBody(`"medium"`, baseInstructionsUserItem, `{"type":"compaction_trigger"}`), cuSynthEnabled, true, false},
		{"没有用户消息", cuThirdPartyBody(`"medium"`, cuAssistantItem), cuSynthEnabled, true, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			final := cuFinalizeHTTP(t, item.body, item.defaults, item.allow, item.compact)
			count := strings.Count(string(final), `"configuration_update"`)
			wantCount := strings.Count(string(item.body), `"configuration_update"`)
			require.Equal(t, wantCount, count, "不得合成 configuration_update")
			// 与关掉开关的结果逐字节相同。
			off := cuFinalizeHTTP(t, item.body, officialOpenAIReasoningDefaults{Effort: item.defaults.Effort, Known: true, NumericEffortAsInteger: item.defaults.NumericEffortAsInteger}, true, item.compact)
			require.True(t, string(off) == string(final), "关闭合成时的出站字节必须一致")
		})
	}
}

// 官方入站（带官方形态的基础指令消息）原样：官方客户端自己决定窗口里有没有这一项，网关不替它补。
func TestHTTPEgressDoesNotSynthesizeConfigurationUpdateForOfficialIngress(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	inbound := officialIncrementalHTTPBody(incOfficialTools, incOfficialBase, incOfficialContext, incOfficialUser)
	final := cuFinalizeHTTP(t, inbound, cuSynthEnabled, true, false)
	require.NotContains(t, string(final), "configuration_update")
	items := inputRaws(final)
	require.Len(t, items, 4)
}

// 画像声明字段与目录能力位冻结进请求上下文，再由上下文取出给定型使用。
func TestOfficialOpenAIReasoningDefaultsCarryConfigurationUpdateGates(t *testing.T) {
	newContext := func(supports bool) *OfficialEgressContext {
		return NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID: 94, TargetPlatform: PlatformOpenAI, ProfileMode: officialClientProfileModePrevious,
			Transport: OfficialEgressTransportHTTP, DefaultReasoningLevel: "low", ReasoningDefaultsKnown: true,
			SupportsReasoningEffortUpdates: supports,
		})
	}
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	declared := officialOpenAIReasoningDefaultsFromContext(newContext(true))
	require.True(t, declared.ConfigurationUpdateHTTPFirstTurn)
	require.True(t, declared.SupportsEffortUpdates)
	require.False(t, officialOpenAIReasoningDefaultsFromContext(newContext(false)).SupportsEffortUpdates)

	// 画像不声明该字段（旧版本）：恒为否，与模型能力无关。
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	legacy := officialOpenAIReasoningDefaultsFromContext(newContext(true))
	require.False(t, legacy.ConfigurationUpdateHTTPFirstTurn)
}

// 模型目录能力位：账号清单的 supports_reasoning_effort_updates 优先，缺省为 false；bundled 只含两个已知为真的 slug。
func TestOpenAIModelCapabilitiesReadReasoningEffortUpdates(t *testing.T) {
	require.True(t, bundledOpenAIModelCapabilities["gpt-6.1-sol"].SupportsReasoningEffortUpdates)
	require.True(t, bundledOpenAIModelCapabilities["gpt-6-astra"].SupportsReasoningEffortUpdates)
	for slug, capability := range bundledOpenAIModelCapabilities {
		if slug != "gpt-6.1-sol" && slug != "gpt-6-astra" {
			require.False(t, capability.SupportsReasoningEffortUpdates, slug)
		}
	}

	svc := &OpenAIGatewayService{}
	svc.openaiModelCapabilities.replaceFromManifest(94, []byte(`{"models":[`+
		`{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":true,"supports_reasoning_effort_updates":true},`+
		`{"slug":"gpt-6-astra","visibility":"list","use_responses_lite":true},`+
		`{"slug":"gpt-5.6-luna","visibility":"list","use_responses_lite":true,"supports_reasoning_effort_updates":false}]}`))
	sol, known := svc.openaiModelCapabilities.modelCapabilities(94, "gpt-6.1-sol")
	require.True(t, known)
	require.True(t, sol.SupportsReasoningEffortUpdates)
	astra, _ := svc.openaiModelCapabilities.modelCapabilities(94, "gpt-6-astra")
	require.False(t, astra.SupportsReasoningEffortUpdates, "账号清单没写＝不支持，不沿用 bundled")
	luna, _ := svc.openaiModelCapabilities.modelCapabilities(94, "gpt-5.6-luna")
	require.False(t, luna.SupportsReasoningEffortUpdates)
}

// 端到端：第三方客户端（Kilo 形态）的 Responses 请求经完整 Forward 走官方 OAuth 账号的 HTTP 出站。画像声明 http_first_turn、
// 账号清单声明 gpt-6.1-sol 支持 effort 更新时，上游收到末尾带官方形态 update(请求 effort) 的 input；请求级 effort 不变。
// 同时核对正文工作区开启与关闭两条路径的上游 wire 字节逐字节一致。
func TestOfficialForwardHTTPSynthesizesConfigurationUpdateForThirdPartyIngress(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	body := []byte(`{"model":"gpt-6.1-sol","stream":false,"store":true,"instructions":"Kilo raw responses instruction",` +
		`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"KILO_UPDATE_S1_OK"}]}],` +
		`"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],` +
		`"reasoning":{"effort":"high"},"text":{"verbosity":"low"}}`)
	tc := officialForwardBodyCase{
		name: "configuration_update_third_party_http",
		body: func(*testing.T) []byte { return append([]byte(nil), body...) },
		context: func(_ *testing.T, raw []byte) *gin.Context {
			c := newOfficialOpenAIHTTPKiloContext(raw, "kilo-configuration-update-session")
			c.Request.URL.Path = "/v1/responses"
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			return c
		},
		service: func(upstream HTTPUpstream) *OpenAIGatewayService {
			cfg := &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}}
			cfg.Gateway.OfficialClientProfiles.Mode = officialClientProfileModePrevious
			svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream}
			svc.openaiModelCapabilities.replaceFromManifest(94, []byte(cuSolManifest))
			return svc
		},
	}
	outcome := officialForwardBodyCompare(t, tc)
	require.NoError(t, outcome.err)
	require.Len(t, outcome.calls, 1)
	sent := gjson.GetBytes(outcome.calls[0].decoded, "input").Array()
	require.NotEmpty(t, sent)
	require.Equal(t, `{"type":"configuration_update","reasoning":{"effort":"high"}}`, sent[len(sent)-1].Raw,
		"首轮：官方形态的 update 在 input 末尾，紧跟首个用户消息")
	require.Equal(t, "message", sent[len(sent)-2].Get("type").String())
	require.Equal(t, "user", sent[len(sent)-2].Get("role").String())
	require.Equal(t, "high", gjson.GetBytes(outcome.calls[0].decoded, "reasoning.effort").String())
	require.Equal(t, 1, strings.Count(string(outcome.calls[0].decoded), `"configuration_update"`))
	require.NotNil(t, outcome.result)
	require.NotNil(t, outcome.result.ReasoningEffort)
	require.Equal(t, "high", *outcome.result.ReasoningEffort)

	// 同一请求换成账号清单不支持 effort 更新的模型能力：不合成。
	tc.name = "configuration_update_third_party_http_unsupported"
	tc.service = func(upstream HTTPUpstream) *OpenAIGatewayService {
		cfg := &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}}
		cfg.Gateway.OfficialClientProfiles.Mode = officialClientProfileModePrevious
		svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream}
		svc.openaiModelCapabilities.replaceFromManifest(94, []byte(
			`{"models":[{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true,"default_reasoning_level":"low"}]}`))
		return svc
	}
	unsupported := officialForwardBodyCompare(t, tc)
	require.NoError(t, unsupported.err)
	require.Len(t, unsupported.calls, 1)
	require.NotContains(t, string(unsupported.calls[0].decoded), "configuration_update")
}
