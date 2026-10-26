package service

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 增量窗口前缀（画像 BaseInstructions 节 IDScheme=uuidv7）的单元与 HTTP 定型测试：
//  1. 画像字段的读取、官方形态判定（形状、键集合、正文逐字相同）；
//  2. 合成项：id 是前缀加 UUIDv7、按 (线程, 内容) 稳定、键序固定；
//  3. HTTP 定型：入站官方形态原样放行（UUIDv7 与旧窗口 UUIDv5 两种 id），第三方入站才合成；非 Lite 仍走旧方案；
//  4. additional_tools 项的补 id、空壳不合成、编码器键序。
// WS 帧的流程用例见 official_egress_openai_incremental_prefix_ws_test.go。

const (
	incTestThreadID = "019a0000-0000-7000-8000-0000000000aa"
	incTestBaseText = "You are Codex, an agent based on GPT-6.\nSecond line.\n"

	// 官方 0.164 Lite 窗口的前缀项（取自真实抓包的形态：键序、id 版本与 passthrough 键序）。
	incOfficialTools = `{"type":"additional_tools","id":"at_01a17935-5c98-7f51-995d-9105098f672a","role":"developer","tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]}`
	incOfficialBase  = `{"type":"message","id":"msg_01a17935-5c98-7f51-995d-9110469c51d5","role":"developer","content":[{"type":"input_text","text":"You are Codex, an agent based on GPT-6.\nSecond line.\n"}],` +
		`"internal_chat_message_metadata_passthrough":{"turn_id":"01a17935-5b84-7f90-a36d-b3d01dca05bd","create_time":1793034902.6807268,"content_item_kinds":["model.base_instructions"],"content_item_metadata":[{"provenance":{"type":"harness","hook":false}}]}}`
	incOfficialContext = `{"type":"message","id":"msg_01a17935-5c98-7f51-995d-912c7d052cf3","role":"developer","content":[{"type":"input_text","text":"permissions"}],` +
		`"internal_chat_message_metadata_passthrough":{"turn_id":"01a17935-5b84-7f90-a36d-b3d01dca05bd","create_time":1793034902.69,"content_item_kinds":["permissions.instructions"],"content_item_metadata":[{"provenance":{"type":"harness","hook":false}}]}}`
	incOfficialUser = `{"type":"message","id":"msg_01a17935-5c9f-7630-aea9-27fc3c3ff5e8","role":"user","content":[{"type":"input_text","text":"hi"}],` +
		`"internal_chat_message_metadata_passthrough":{"turn_id":"01a17935-5b84-7f90-a36d-b3d01dca05bd","create_time":1793034902.7,"content_item_kinds":["user.text"],"content_item_metadata":[{"provenance":{"type":"user"}}]}}`

	// 旧窗口（或非 Lite 模型）里官方客户端发的 UUIDv5 形态：passthrough 只有 content_item_kinds。
	incLegacyTools = `{"type":"additional_tools","id":"at_754bc5ae-7713-52a1-a7c4-342a8e15098a","role":"developer","tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}]}`
	incLegacyBase  = `{"type":"message","id":"msg_5b11e845-898c-5236-899f-ad030f1d4973","role":"developer","content":[{"type":"input_text","text":"You are Codex, an agent based on GPT-6.\nSecond line.\n"}],` +
		`"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}}`
)

func incrementalPrefixSection() *profilecontract.BaseInstructionsSection {
	return &profilecontract.BaseInstructionsSection{
		Carrier:         "input_developer_message",
		ContentItemKind: "model.base_instructions",
		EndpointIDs:     []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS},
		IDPrefix:        "msg",
		IDScheme:        profilecontract.BaseInstructionsIDSchemeUUIDv7,
	}
}

// syntheticIncrementalPrefixMutation 在“较新版本合成画像”之上把基础指令节改为声明 uuidv7 方案。
func syntheticIncrementalPrefixMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	newer := syntheticNewerProfileMutation(t)
	return func(doc *profilecontract.SnapshotDoc) {
		newer(doc)
		doc.BaseInstructions = syntheticServiceRawSection(t, *incrementalPrefixSection())
	}
}

// requireStableUUIDv7ItemID 要求 id 是 前缀_UUIDv7（规范写法、RFC 4122 变体）。
func requireStableUUIDv7ItemID(t *testing.T, id, prefix, label string) {
	t.Helper()
	require.True(t, strings.HasPrefix(id, prefix+"_"), "%s：id 前缀应为 %s_：%s", label, prefix, id)
	suffix := strings.TrimPrefix(id, prefix+"_")
	require.Len(t, suffix, 36, label)
	parsed, err := uuid.Parse(suffix)
	require.NoError(t, err, label)
	require.Equal(t, uuid.Version(7), parsed.Version(), "%s：应为 UUIDv7：%s", label, id)
	require.Equal(t, uuid.RFC4122, parsed.Variant(), label)
	require.Equal(t, strings.ToLower(suffix), suffix, label)
}

// requireSynthesizedBasePassthrough 要求网关合成的基础指令消息带官方 0.164 增量窗口形态的 passthrough：键序
// turn_id,create_time,content_item_kinds,content_item_metadata；turn_id 是 UUIDv7；create_time 是 Unix 秒，
// 毫秒部分与消息 id 的 UUIDv7 时间戳一致（官方的 id 与 create_time 在同一时刻生成）。
func requireSynthesizedBasePassthrough(t *testing.T, base gjson.Result, label string) {
	t.Helper()
	passthrough := base.Get("internal_chat_message_metadata_passthrough")
	require.Equal(t, []string{"turn_id", "create_time", "content_item_kinds", "content_item_metadata"},
		jsonTopLevelKeys(t, passthrough.Raw), label)
	turnID := passthrough.Get("turn_id").String()
	parsedTurn, err := uuid.Parse(turnID)
	require.NoError(t, err, label)
	require.Equal(t, uuid.Version(7), parsedTurn.Version(), label)
	require.Equal(t, `["model.base_instructions"]`, passthrough.Get("content_item_kinds").Raw, label)
	require.Equal(t, `[{"provenance":{"type":"harness","hook":false}}]`, passthrough.Get("content_item_metadata").Raw, label)
	createTime := passthrough.Get("create_time")
	require.Equal(t, gjson.Number, createTime.Type, label)
	parsedID, err := uuid.Parse(strings.TrimPrefix(base.Get("id").String(), "msg_"))
	require.NoError(t, err, label)
	var idMilliseconds uint64
	for _, b := range parsedID[:6] {
		idMilliseconds = idMilliseconds<<8 | uint64(b)
	}
	require.InDelta(t, float64(idMilliseconds), createTime.Float()*1000, 1.0, "%s：create_time 与 id 的时间戳同一毫秒", label)
	require.NotContains(t, createTime.Raw, "e", "%s：不带指数", label)
}

func TestOfficialCodexIncrementalPrefixRequiresUUIDv7SchemeAndLite(t *testing.T) {
	section := incrementalPrefixSection()
	legacy := baseInstructionsTestSection()
	require.True(t, officialCodexIncrementalPrefix(section, true))
	require.False(t, officialCodexIncrementalPrefix(section, false), "非 Lite 不走增量窗口")
	require.False(t, officialCodexIncrementalPrefix(legacy, true), "旧方案画像不走增量窗口")
	require.False(t, officialCodexIncrementalPrefix(nil, true), "画像没有该节时不走增量窗口")
}

func TestIsOfficialCodexOfficialBaseInstructionsFormTable(t *testing.T) {
	section := incrementalPrefixSection()
	const text = incTestBaseText
	build := func(mutate func(item map[string]any)) []byte {
		var item map[string]any
		require.NoError(t, json.Unmarshal([]byte(incOfficialBase), &item))
		if mutate != nil {
			mutate(item)
		}
		raw, err := json.Marshal(item)
		require.NoError(t, err)
		return raw
	}
	passthrough := func(item map[string]any) map[string]any {
		return item["internal_chat_message_metadata_passthrough"].(map[string]any)
	}
	cases := []struct {
		name string
		raw  []byte
		text string
		want bool
	}{
		{"UUIDv7 的官方形态", []byte(incOfficialBase), text, true},
		{"旧窗口 UUIDv5 的官方形态", []byte(incLegacyBase), text, true},
		{"正文不同", []byte(incOfficialBase), text + "x", false},
		{"id 不是 UUID", build(func(i map[string]any) { i["id"] = "msg_from_client" }), text, false},
		{"id 前缀不符", build(func(i map[string]any) { i["id"] = "at_01a17935-5c98-7f51-995d-9110469c51d5" }), text, false},
		{"id 缺失", build(func(i map[string]any) { delete(i, "id") }), text, false},
		{"id 不是字符串", build(func(i map[string]any) { i["id"] = 7 }), text, false},
		{"id 带大括号的 UUID 写法", build(func(i map[string]any) { i["id"] = "msg_{01a17935-5c98-7f51-995d-9110469c51d}" }), text, false},
		{"role 不是 developer", build(func(i map[string]any) { i["role"] = "user" }), text, false},
		{"type 不是 message", build(func(i map[string]any) { i["type"] = "reasoning" }), text, false},
		{"多出 status", build(func(i map[string]any) { i["status"] = "completed" }), text, false},
		{"多出 phase", build(func(i map[string]any) { i["phase"] = "commentary" }), text, false},
		{"passthrough 多出未知键", build(func(i map[string]any) { passthrough(i)["foo"] = 1 }), text, false},
		{"passthrough 带工具调用才有的键", build(func(i map[string]any) { passthrough(i)["cell_id"] = "c" }), text, false},
		{"标记集合含多个值", build(func(i map[string]any) {
			passthrough(i)["content_item_kinds"] = []any{"model.base_instructions", "model.other"}
		}), text, false},
		{"标记不符", build(func(i map[string]any) { passthrough(i)["content_item_kinds"] = []any{"model.other"} }), text, false},
		{"没有 passthrough", build(func(i map[string]any) { delete(i, "internal_chat_message_metadata_passthrough") }), text, false},
		{"content 是字符串", build(func(i map[string]any) { i["content"] = text }), text, false},
		{"content 有两个片段", build(func(i map[string]any) {
			i["content"] = []any{
				map[string]any{"type": "input_text", "text": text},
				map[string]any{"type": "input_text", "text": text},
			}
		}), text, false},
		{"content 片段类型不符", build(func(i map[string]any) {
			i["content"] = []any{map[string]any{"type": "output_text", "text": text}}
		}), text, false},
		{"content 片段多出成员", build(func(i map[string]any) {
			i["content"] = []any{map[string]any{"type": "input_text", "text": text, "annotations": []any{}}}
		}), text, false},
		{"不是对象", []byte(`"text"`), text, false},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			require.Equal(t, item.want, isOfficialCodexOfficialBaseInstructionsForm(section, item.raw, item.text), string(item.raw))
		})
	}
}

func TestRenderOfficialCodexIncrementalBaseInstructionsItemIsStableAndOfficialShaped(t *testing.T) {
	section := incrementalPrefixSection()
	text := "稳定性用例专用正文 a<b>&c \"q\"  "
	rendered, err := renderOfficialCodexIncrementalBaseInstructionsItem(section, incTestThreadID, text)
	require.NoError(t, err)
	require.True(t, json.Valid(rendered))
	// 键序与旧方案相同：type、id、role、content、internal_chat_message_metadata_passthrough。
	require.Equal(t,
		[]string{"type", "id", "role", "content", "internal_chat_message_metadata_passthrough"},
		jsonTopLevelKeys(t, string(rendered)))
	requireStableUUIDv7ItemID(t, gjson.GetBytes(rendered, "id").String(), "msg", "合成基础指令消息")
	requireSynthesizedBasePassthrough(t, gjson.ParseBytes(rendered), "合成基础指令消息")
	require.Equal(t, text, gjson.GetBytes(rendered, "content.0.text").String())
	// 合成项自己也满足官方形态判定。
	require.True(t, isOfficialCodexOfficialBaseInstructionsForm(section, rendered, text))

	// 同线程同正文：id 稳定（滑动窗口内复用同一个 UUIDv7）。
	again, err := renderOfficialCodexIncrementalBaseInstructionsItem(section, incTestThreadID, text)
	require.NoError(t, err)
	require.Equal(t, string(rendered), string(again))
	// 正文或线程不同：id 不同。
	other, err := renderOfficialCodexIncrementalBaseInstructionsItem(section, incTestThreadID, text+" ")
	require.NoError(t, err)
	require.NotEqual(t, gjson.GetBytes(rendered, "id").String(), gjson.GetBytes(other, "id").String())
	otherThread, err := renderOfficialCodexIncrementalBaseInstructionsItem(section, incTestThreadID+"x", text)
	require.NoError(t, err)
	require.NotEqual(t, gjson.GetBytes(rendered, "id").String(), gjson.GetBytes(otherThread, "id").String())
	// 没有线程：失败关闭。
	_, err = renderOfficialCodexIncrementalBaseInstructionsItem(section, "  ", text)
	require.Error(t, err)
}

func TestOfficialCodexInboundOfficialBaseInstructionsFromFrame(t *testing.T) {
	section := incrementalPrefixSection()
	frame := func(items ...string) []byte {
		return []byte(`{"type":"response.create","previous_response_id":"resp_x","input":[` + strings.Join(items, ",") + `]}`)
	}
	raw := officialCodexInboundOfficialBaseInstructionsFromFrame(frame(incOfficialTools, incOfficialBase, incOfficialUser), section, incTestBaseText)
	require.Equal(t, incOfficialBase, string(raw), "原文逐字节返回")
	// 返回的是复制，不引用入参。
	source := frame(incOfficialTools, incOfficialBase)
	copied := officialCodexInboundOfficialBaseInstructionsFromFrame(source, section, incTestBaseText)
	for index := range source {
		source[index] = 'x'
	}
	require.Equal(t, incOfficialBase, string(copied))

	require.Nil(t, officialCodexInboundOfficialBaseInstructionsFromFrame(frame(incOfficialUser), section, incTestBaseText), "没有基础指令消息")
	require.Nil(t, officialCodexInboundOfficialBaseInstructionsFromFrame(frame(incOfficialTools, incOfficialBase), section, "另一段正文"), "正文不同不采用")
	require.Nil(t, officialCodexInboundOfficialBaseInstructionsFromFrame(frame(incOfficialTools, incOfficialBase), section, ""), "空正文不采用")
	require.Nil(t, officialCodexInboundOfficialBaseInstructionsFromFrame(nil, section, incTestBaseText))
	// 只看前几项：第四项之后的不采用。
	require.Nil(t, officialCodexInboundOfficialBaseInstructionsFromFrame(
		frame(incOfficialUser, incOfficialUser, incOfficialUser, incOfficialBase), section, incTestBaseText))
	// 前几项里先出现的非官方形态项不挡住后面的官方项。
	fake := strings.Replace(incOfficialBase, "msg_01a17935-5c98-7f51-995d-9110469c51d5", "msg_fake", 1)
	require.Equal(t, incOfficialBase, string(officialCodexInboundOfficialBaseInstructionsFromFrame(
		frame(fake, incOfficialBase), section, incTestBaseText)))
}

func TestCaptureOfficialOpenAIHTTPBodyContractKeepsInboundTaggedMessageRaw(t *testing.T) {
	body := []byte(`{"model":"m","input":[` + incOfficialTools + `,` + incOfficialBase + `,` + incOfficialUser + `]}`)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	require.NotEmpty(t, contract.inboundTaggedMessages)
	section := incrementalPrefixSection()
	require.Equal(t, incOfficialBase, string(contract.inboundOfficialBaseInstructionsItem(section, incTestBaseText)))
	require.Nil(t, contract.inboundOfficialBaseInstructionsItem(section, incTestBaseText+"x"), "正文不同不采用")
	// 复制：改动请求体缓冲不影响契约里的记录。
	for index := range body {
		body[index] = 'x'
	}
	require.Equal(t, incOfficialBase, string(contract.inboundOfficialBaseInstructionsItem(section, incTestBaseText)))
}

func TestEnsureOfficialCodexAdditionalToolsIDs(t *testing.T) {
	tools := func() []any {
		return []any{map[string]any{"type": "function", "name": "read_file", "parameters": map[string]any{"type": "object"}}}
	}
	t.Run("没有 id 的项补 at_ 加稳定 UUIDv7", func(t *testing.T) {
		build := func() map[string]any {
			return map[string]any{"input": []any{
				map[string]any{"type": "additional_tools", "role": "developer", "tools": tools()},
				map[string]any{"type": "message", "role": "user"},
			}}
		}
		first, second := build(), build()
		modified, err := ensureOfficialCodexAdditionalToolsIDs(first, incTestThreadID)
		require.NoError(t, err)
		require.True(t, modified)
		id := first["input"].([]any)[0].(map[string]any)["id"].(string)
		requireStableUUIDv7ItemID(t, id, "at", "合成 additional_tools")
		// 同线程同工具定义：id 相同；用户消息不被改动。
		_, err = ensureOfficialCodexAdditionalToolsIDs(second, incTestThreadID)
		require.NoError(t, err)
		require.Equal(t, id, second["input"].([]any)[0].(map[string]any)["id"])
		_, hasID := first["input"].([]any)[1].(map[string]any)["id"]
		require.False(t, hasID)
		// 工具定义不同或线程不同：id 不同。
		changed := build()
		changed["input"].([]any)[0].(map[string]any)["tools"] = append(tools(), map[string]any{"type": "function", "name": "other"})
		_, err = ensureOfficialCodexAdditionalToolsIDs(changed, incTestThreadID)
		require.NoError(t, err)
		require.NotEqual(t, id, changed["input"].([]any)[0].(map[string]any)["id"])
		otherThread := build()
		_, err = ensureOfficialCodexAdditionalToolsIDs(otherThread, incTestThreadID+"y")
		require.NoError(t, err)
		require.NotEqual(t, id, otherThread["input"].([]any)[0].(map[string]any)["id"])
	})
	t.Run("入站自带 id 的项原样保留", func(t *testing.T) {
		payload := map[string]any{"input": []any{
			map[string]any{"type": "additional_tools", "id": "at_01a17935-5c98-7f51-995d-9105098f672a", "role": "developer", "tools": tools()},
		}}
		modified, err := ensureOfficialCodexAdditionalToolsIDs(payload, incTestThreadID)
		require.NoError(t, err)
		require.False(t, modified)
		require.Equal(t, "at_01a17935-5c98-7f51-995d-9105098f672a", payload["input"].([]any)[0].(map[string]any)["id"])
	})
	t.Run("没有工具的空壳不补 id 也不删除", func(t *testing.T) {
		for _, emptyTools := range []any{nil, []any{}} {
			shell := map[string]any{"type": "additional_tools", "role": "developer"}
			if emptyTools != nil {
				shell["tools"] = emptyTools
			}
			payload := map[string]any{"input": []any{shell}}
			modified, err := ensureOfficialCodexAdditionalToolsIDs(payload, incTestThreadID)
			require.NoError(t, err)
			require.False(t, modified)
			require.Len(t, payload["input"], 1)
			_, hasID := shell["id"]
			require.False(t, hasID)
		}
	})
	t.Run("线程缺失失败关闭", func(t *testing.T) {
		payload := map[string]any{"input": []any{map[string]any{"type": "additional_tools", "tools": tools()}}}
		_, err := ensureOfficialCodexAdditionalToolsIDs(payload, " ")
		require.Error(t, err)
	})
	t.Run("input 不是数组时不动", func(t *testing.T) {
		modified, err := ensureOfficialCodexAdditionalToolsIDs(map[string]any{"input": "hi"}, incTestThreadID)
		require.NoError(t, err)
		require.False(t, modified)
	})
}

// 带 id 的 additional_tools 项被重编码时键序固定为官方序 type,id,role,tools；没有 id 的旧形态编码行为不变。
func TestOfficialJSONEncoderOrdersAdditionalToolsKeysWithID(t *testing.T) {
	// 开关由画像声明 uuidv7 方案的编码入口置位；这里直接构造带开关的索引。
	index := officialJSONIndexWithAdditionalToolsKeyOrder(nil, nil, true)
	require.NotNil(t, index)
	encode := func(value map[string]any) string {
		out, err := officialJSONAppendValue(index, nil, value, -1)
		require.NoError(t, err)
		return string(out)
	}
	withID := encode(map[string]any{
		"tools": []any{}, "role": "developer", "id": "at_x", "type": "additional_tools",
	})
	require.Equal(t, `{"type":"additional_tools","id":"at_x","role":"developer","tools":[]}`, withID)
	// 多余的键排在官方四键之后。
	extra := encode(map[string]any{
		"zzz": 1, "tools": []any{}, "role": "developer", "id": "at_x", "type": "additional_tools",
	})
	require.Equal(t, `{"type":"additional_tools","id":"at_x","role":"developer","tools":[],"zzz":1}`, extra)
	// 没有 id 的旧形态：字典序，与引入键序钩子之前相同。
	legacy := encode(map[string]any{"tools": []any{}, "role": "developer", "type": "additional_tools"})
	require.Equal(t, `{"role":"developer","tools":[],"type":"additional_tools"}`, legacy)
	// 其他类型的对象不受影响。
	other := encode(map[string]any{"type": "message", "id": "msg_x", "role": "user"})
	require.Equal(t, `{"id":"msg_x","role":"user","type":"message"}`, other)
}

// ---- HTTP 定型 ----

// finalizeIncrementalHTTP 用与线上相同的入口完成一次 HTTP 定型。normalizedBody 是兼容层剥掉 passthrough 之后的正文，
// contractBody 是入站原文（契约在入站规范化之前捕获）。
func finalizeIncrementalHTTP(
	t *testing.T,
	lite bool,
	contractBody []byte,
	normalizedBody []byte,
) ([]byte, officialOpenAIHTTPIdentity) {
	t.Helper()
	contract, err := captureOfficialOpenAIHTTPBodyContract(contractBody)
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(
		nil, newOfficialOpenAIHTTPTestAccount(94110), normalizedBody, contract, officialClientProfileModePrevious)
	require.NoError(t, err)
	final, _, err := finalizeOfficialOpenAIHTTPBody(normalizedBody, contract, identity, officialOpenAIReasoningDefaults{},
		officialOpenAIHTTPBodyOptions{
			ProfileMode: officialClientProfileModePrevious, UseResponsesLite: lite, SupportsParallelTools: true, ThreadID: identity.threadID,
			TurnID: identity.turnID,
		})
	require.NoError(t, err)
	return final, identity
}

func stripInboundPassthrough(t *testing.T, body []byte) []byte {
	t.Helper()
	normalized, _, err := normalizeOpenAIOAuthResponsesCompatibilityBody(body)
	require.NoError(t, err)
	require.NotContains(t, string(normalized), "internal_chat_message_metadata_passthrough")
	return normalized
}

func officialIncrementalHTTPBody(items ...string) []byte {
	return []byte(`{"model":"gpt-5.6-luna","input":[` + strings.Join(items, ",") + `],"tool_choice":"auto",` +
		`"reasoning":{"effort":"medium"},"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671",` +
		`"turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
}

// 入站是 Codex 客户端的 Lite 请求：官方形态的 additional_tools 与基础指令消息原样放行（id、键序、passthrough 都不变），
// 不论 id 是增量窗口的 UUIDv7 还是旧窗口的 UUIDv5（基础指令消息除 passthrough.turn_id 外逐字节，turn_id 改写成网关的窗口第一段 turn_id）。
func TestFinalizeOfficialOpenAIHTTPBodyKeepsInboundOfficialPrefixVerbatimInIncrementalWindow(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	cases := []struct {
		name        string
		tools, base string
	}{
		{"增量窗口 UUIDv7", incOfficialTools, incOfficialBase},
		{"旧窗口 UUIDv5", incLegacyTools, incLegacyBase},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			inbound := officialIncrementalHTTPBody(item.tools, item.base, incOfficialContext, incOfficialUser)
			final, identity := finalizeIncrementalHTTP(t, true, inbound, stripInboundPassthrough(t, inbound))
			input := gjson.GetBytes(final, "input").Array()
			require.Equal(t, 4, len(input))
			require.Equal(t, item.tools, input[0].Raw, "additional_tools 逐字节原样")
			// 基础指令消息除 passthrough.turn_id 外逐字节原样（id、键序、其余各键全部保留）；turn_id 改写成网关本轮（单段，
			// 即窗口第一段）的 turn_id。旧窗口的官方形态本来就没有 turn_id 键，整项原样。
			requireBaseMessageVerbatimExceptTurnID(t, item.base, input[1].Raw, identity.turnID, "基础指令消息")
			require.Equal(t, "permissions", input[2].Get("content.0.text").String())
			require.Equal(t, "hi", input[3].Get("content.0.text").String())
			require.False(t, gjson.GetBytes(final, "instructions").Exists())
		})
	}
}

// 第三方入站的 Lite 请求：基础指令消息和 additional_tools 由网关合成，id 是前缀加稳定 UUIDv7，同线程同内容各请求相同。
func TestFinalizeOfficialOpenAIHTTPBodySynthesizesStableUUIDv7PrefixForThirdPartyIngress(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	section := incrementalPrefixSection()
	text := "第三方入站合成用例专用指令 " + t.Name()
	body := []byte(`{"model":"gpt-5.6-luna","instructions":` + incJSONString(t, text) + `,` +
		`"input":[` + baseInstructionsUserItem + `],` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}},` +
		`{"type":"namespace","name":"mcp__demo","description":"d","tools":[{"type":"function","name":"ping","parameters":{"type":"object"}}]}],` +
		`"tool_choice":"auto","reasoning":{"effort":"medium"},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)

	first, identity := finalizeIncrementalHTTP(t, true, body, body)
	input := gjson.GetBytes(first, "input").Array()
	require.Equal(t, []string{"additional_tools:developer", "BASE", "message:user"}, describeBaseInstructionsInput(input))
	require.False(t, gjson.GetBytes(first, "tools").Exists(), "工具迁入 additional_tools")
	require.False(t, gjson.GetBytes(first, "instructions").Exists())

	tools := input[0]
	require.Equal(t, []string{"type", "id", "role", "tools"}, jsonTopLevelKeys(t, tools.Raw), "官方键序 type,id,role,tools")
	requireStableUUIDv7ItemID(t, tools.Get("id").String(), "at", "additional_tools")
	require.Equal(t, 2, len(tools.Get("tools").Array()))

	base := input[1]
	require.Equal(t, []string{"type", "id", "role", "content", "internal_chat_message_metadata_passthrough"}, jsonTopLevelKeys(t, base.Raw))
	requireStableUUIDv7ItemID(t, base.Get("id").String(), "msg", "基础指令消息")
	require.Equal(t, text, base.Get("content.0.text").String())
	requireSynthesizedBasePassthrough(t, base, "HTTP 第三方入站")
	require.Equal(t, identity.turnID, base.Get("internal_chat_message_metadata_passthrough.turn_id").String(),
		"基础指令消息的 turn_id 是窗口第一段（首轮即本轮）的网关 turn_id")
	require.True(t, isOfficialCodexOfficialBaseInstructionsForm(section, []byte(base.Raw), text))

	// 同一线程、同内容的下一个请求：id 与整段前缀逐字节相同（上游前缀缓存不被 id 破坏）。
	second, secondIdentity := finalizeIncrementalHTTP(t, true, body, body)
	require.Equal(t, identity.threadID, secondIdentity.threadID)
	secondInput := gjson.GetBytes(second, "input").Array()
	require.Equal(t, tools.Raw, secondInput[0].Raw)
	require.Equal(t, base.Raw, secondInput[1].Raw)
	require.Equal(t, string(first), string(second))
}

// 入站带了基础指令标记，但不是官方形态（id 不是 UUID、passthrough 多出未知键等）：不原样放行，按正文合成。
func TestFinalizeOfficialOpenAIHTTPBodyRegeneratesNonOfficialLookalikeBaseInstructions(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	lookalikes := map[string]string{
		"id 不是 UUID":        strings.Replace(incOfficialBase, "msg_01a17935-5c98-7f51-995d-9110469c51d5", "msg_from_client", 1),
		"passthrough 多出未知键": strings.Replace(incOfficialBase, `"content_item_kinds"`, `"foo":1,"content_item_kinds"`, 1),
	}
	for name, base := range lookalikes {
		t.Run(name, func(t *testing.T) {
			inbound := officialIncrementalHTTPBody(incOfficialTools, base, incOfficialUser)
			final, _ := finalizeIncrementalHTTP(t, true, inbound, stripInboundPassthrough(t, inbound))
			input := gjson.GetBytes(final, "input").Array()
			require.Equal(t, []string{"additional_tools:developer", "BASE", "message:user"}, describeBaseInstructionsInput(input))
			require.NotEqual(t, base, input[1].Raw)
			requireStableUUIDv7ItemID(t, input[1].Get("id").String(), "msg", name)
			requireSynthesizedBasePassthrough(t, input[1], name)
			require.Equal(t, incOfficialTools, input[0].Raw, "官方形态的 additional_tools 仍原样")
		})
	}
}

// 非 Lite 模型不走增量窗口：官方客户端对它们仍每请求拼装 UUIDv5 的基础指令消息，网关沿用旧方案（线程加正文算 id）。
func TestFinalizeOfficialOpenAIHTTPBodyKeepsLegacyIDsForNonLiteUnderUUIDv7Scheme(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticIncrementalPrefixMutation(t))
	section := synthetic.Optional().BaseInstructions
	require.True(t, section.UsesUUIDv7IDs())
	inbound := officialIncrementalHTTPBody(incLegacyBase, incOfficialUser)
	final, identity := finalizeIncrementalHTTP(t, false, inbound, stripInboundPassthrough(t, inbound))
	input := gjson.GetBytes(final, "input").Array()
	require.Equal(t, []string{"BASE", "message:user"}, describeBaseInstructionsInput(input))
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, identity.threadID, incTestBaseText)
	require.NoError(t, err)
	require.Equal(t, string(rendered), input[0].Raw, "非 Lite：按网关线程重算 UUIDv5，入站 id 不沿用")
	require.NotEqual(t, "msg_5b11e845-898c-5236-899f-ad030f1d4973", input[0].Get("id").String())
	// 非 Lite 没有 additional_tools 载体，合成路径同样不出现。
	require.False(t, strings.Contains(string(final), `"additional_tools"`))
}

// 画像不声明 uuidv7 方案（旧方案）：行为与引入增量窗口方案之前相同——入站官方消息按网关线程重建，additional_tools 不补 id。
func TestFinalizeOfficialOpenAIHTTPBodyLegacySchemeIsUnchanged(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	section := synthetic.Optional().BaseInstructions
	require.False(t, section.UsesUUIDv7IDs())
	body := []byte(`{"model":"gpt-5.6-luna","instructions":"Legacy scheme helper.","input":[` + baseInstructionsUserItem + `],` +
		`"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],"tool_choice":"auto","reasoning":{"effort":"medium"},` +
		`"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"}}`)
	final, identity := finalizeIncrementalHTTP(t, true, body, body)
	input := gjson.GetBytes(final, "input").Array()
	require.Equal(t, []string{"additional_tools:developer", "BASE", "message:user"}, describeBaseInstructionsInput(input))
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, identity.threadID, "Legacy scheme helper.")
	require.NoError(t, err)
	require.Equal(t, string(rendered), input[1].Raw, "旧方案：UUIDv5")
	require.False(t, input[0].Get("id").Exists(), "旧方案：additional_tools 不补 id")
}

func incJSONString(t *testing.T, value string) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	require.NoError(t, err)
	return string(encoded)
}

// 官方增量窗口里工具集中途变化时追加的 additional_tools 增量项（带 id）位置就是语义，不能被提到 input 首位；
// 第三方入站没有 id 的工具声明仍然移到首位。
func TestMoveOfficialOpenAIAdditionalToolsFirstKeepsIDBearingIncrementsInPlace(t *testing.T) {
	user := map[string]any{"type": "message", "role": "user", "content": "hi"}
	hint := map[string]any{"type": "message", "role": "developer", "content": "This is an incremental tools update."}
	increment := map[string]any{"type": "additional_tools", "id": "at_01a17935-5c98-7f51-995d-9105098f672a", "role": "developer", "tools": []any{}}
	leading := map[string]any{"type": "additional_tools", "id": "at_01a17935-5c98-7f51-995d-9105098f6000", "role": "developer", "tools": []any{}}
	declared := map[string]any{"type": "additional_tools", "role": "developer", "tools": []any{map[string]any{"type": "function", "name": "f"}}}
	cases := []struct {
		name         string
		input        []any
		wantModified bool
		want         []any
	}{
		{"带 id 的增量项在提示消息之后：不动", []any{hint, increment, user}, false, []any{hint, increment, user}},
		{"开头的官方工具项加后面的增量项：不动", []any{leading, user, increment}, false, []any{leading, user, increment}},
		{"没有 id 的工具声明仍移到首位", []any{user, declared}, true, []any{declared, user}},
		{"没有 id 的声明被提前，带 id 的增量项留在原位", []any{user, declared, increment}, true, []any{declared, user, increment}},
		{"没有任何工具项", []any{hint, user}, false, []any{hint, user}},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			payload := map[string]any{"input": append([]any(nil), item.input...)}
			require.Equal(t, item.wantModified, moveOfficialOpenAIAdditionalToolsFirst(payload, true))
			require.Equal(t, item.want, payload["input"])
		})
	}
}
