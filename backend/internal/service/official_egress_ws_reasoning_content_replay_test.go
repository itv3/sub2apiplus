package service

import (
	"encoding/json"
	"fmt"
	"runtime"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 本文件锁定跨模型旧会话的 1008 缺陷：先用别家模型（推理项带非空 content 数组）、后切 GPT 并自动压缩时，
// WS 入口兼容层（normalizeOpenAIResponsesReasoningContentReplay）会删掉推理项里的 content 数组，而出站业务历史
// 校验（equalOfficialOpenAIWSBusinessHistory）此前以客户端原帧逐项比较，把这项受控清理判成“input was modified”，
// 转 1008 断流，重试带同一份历史再次失败。
//
// 修法是让校验器直接调用兼容层的同一个清理函数判定（规则只有一份，不另写条件），校验器只额外接受
// “候选项 = 原始推理项删去满足清理条件的 content 数组”这一种差异。以下用例：
//   - 用真实的 WS 兼容处理产出候选帧再进校验（不手写候选帧），避免只分别测两个函数而漏掉组合冲突；
//   - 覆盖正例、边界（空数组／字符串／null／对象不被额外放行）、负例（改任何其他业务字段仍拒绝）；
//   - 以表格同时驱动“兼容层是否清理”与“校验器是否接受清理”，证明两处条件一致。
// 样本均为脱敏的结构样例，不含真实会话内容。

// reasoningReplayOAuthAccount 是 WS 兼容处理使用的 OpenAI OAuth 账号（官方出站只对它生效）。
func reasoningReplayOAuthAccount() *Account {
	return &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}
}

// reasoningReplayReasoningItem 构造跨模型历史里的一条推理项（脱敏样例）：content 为别家模型留下的可见推理块。
func reasoningReplayReasoningItem(contentJSON string) string {
	content := ""
	if contentJSON != "" {
		content = `,"content":` + contentJSON
	}
	return `{"type":"reasoning","id":"rs_sample_0001","summary":[{"type":"summary_text","text":"示例摘要"}]` +
		content + `,"encrypted_content":"示例密文"}`
}

// reasoningReplayFrame 构造一帧 response.create：恢复完整历史（用户消息、助手消息、推理项、配对的工具调用与输出、新问题）。
func reasoningReplayFrame(reasoningItem string, model string) []byte {
	return []byte(`{"type":"response.create","model":"` + model + `","stream":true,"store":false,` +
		`"reasoning":{"effort":"high"},"input":[` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"第一问"}]},` +
		reasoningItem + `,` +
		`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"第一答"}]},` +
		`{"type":"function_call","id":"fc_sample_0001","call_id":"call_sample_0001","name":"lookup","arguments":"{\"q\":\"one\"}"},` +
		`{"type":"function_call_output","call_id":"call_sample_0001","output":"ok"},` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"第二问"}]}` +
		`]}`)
}

// reasoningReplayCandidate 用真实的 WS 兼容处理从客户端原帧产出候选帧。
func reasoningReplayCandidate(t *testing.T, original []byte) ([]byte, bool) {
	t.Helper()
	candidate, changed, err := normalizeOpenAIResponsesWebSocketCompatibilityBody(original, reasoningReplayOAuthAccount(), false)
	require.NoError(t, err)
	return candidate, changed
}

// 正例：非空 content 数组被兼容层清理后，校验放行；summary、encrypted_content、id 原样保留，其他历史项不变。
func TestOfficialWSReasoningContentReplayCompatOutputPassesBusinessContract(t *testing.T) {
	for _, contentJSON := range []string{
		`[{"type":"reasoning_text","text":"示例推理历史"}]`,
		`[{"type":"reasoning_text","text":"一"},{"type":"reasoning_text","text":"二"}]`,
		`[{"type":"reasoning_text","text":""}]`,
		`[null]`,
		`[[]]`,
	} {
		t.Run(contentJSON, func(t *testing.T) {
			original := reasoningReplayFrame(reasoningReplayReasoningItem(contentJSON), "gpt-6.1-sol")
			candidate, changed := reasoningReplayCandidate(t, original)
			require.True(t, changed)
			require.False(t, gjson.GetBytes(candidate, "input.1.content").Exists(), "兼容层应已清理 content")
			require.Equal(t, "示例摘要", gjson.GetBytes(candidate, "input.1.summary.0.text").String())
			require.Equal(t, "示例密文", gjson.GetBytes(candidate, "input.1.encrypted_content").String())
			require.Equal(t, "rs_sample_0001", gjson.GetBytes(candidate, "input.1.id").String())
			require.JSONEq(t, gjson.GetBytes(original, "input.0").Raw, gjson.GetBytes(candidate, "input.0").Raw)
			require.JSONEq(t, gjson.GetBytes(original, "input.3").Raw, gjson.GetBytes(candidate, "input.3").Raw)
			require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false))
		})
	}
}

// 正例：原样历史、无 content 的推理项、合法工具调用与输出配对继续通过；一帧里多条推理项各自清理。
func TestOfficialWSReasoningContentReplayKeepsOtherShapesPassing(t *testing.T) {
	t.Run("无 content 的推理项", func(t *testing.T) {
		original := reasoningReplayFrame(reasoningReplayReasoningItem(""), "gpt-6.1-sol")
		candidate, changed := reasoningReplayCandidate(t, original)
		require.False(t, changed)
		require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false))
	})
	t.Run("候选与原帧逐字相同", func(t *testing.T) {
		original := reasoningReplayFrame(reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"x"}]`), "gpt-6.1-sol")
		require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, original, "", false), "兼容层未介入时原样通过")
	})
	t.Run("多条推理项各自清理", func(t *testing.T) {
		original := []byte(`{"type":"response.create","model":"gpt-6.1-sol","input":[` +
			reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"a"}]`) + `,` +
			`{"type":"message","role":"user","content":"mid"},` +
			reasoningReplayReasoningItem(`[]`) + `,` +
			reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"b"}]`) + `]}`)
		candidate, changed := reasoningReplayCandidate(t, original)
		require.True(t, changed)
		require.False(t, gjson.GetBytes(candidate, "input.0.content").Exists())
		require.True(t, gjson.GetBytes(candidate, "input.2.content").IsArray(), "空数组不是清理对象，保持原样")
		require.False(t, gjson.GetBytes(candidate, "input.3.content").Exists())
		require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false))
	})
	t.Run("推理项缺 type 两端空白仍按兼容层规则清理", func(t *testing.T) {
		original := []byte(`{"type":"response.create","model":"gpt-6.1-sol","input":[` +
			`{"type":" reasoning ","summary":[],"content":[{"type":"reasoning_text","text":"x"}]}]}`)
		candidate, changed := reasoningReplayCandidate(t, original)
		require.True(t, changed)
		require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false))
	})
}

// 边界：空数组、字符串、null、对象等不满足既有清理条件的 content 形态，兼容层不清理，校验器也不因本修复额外放行
// “把它们删掉”的候选帧。
func TestOfficialWSReasoningContentReplayBoundaryShapesAreNotExtraAccepted(t *testing.T) {
	for _, contentJSON := range []string{`[]`, `"visible reasoning"`, `null`, `{"type":"reasoning_text"}`, `7`, `true`} {
		t.Run(contentJSON, func(t *testing.T) {
			original := reasoningReplayFrame(reasoningReplayReasoningItem(contentJSON), "gpt-6.1-sol")
			candidate, changed := reasoningReplayCandidate(t, original)
			require.False(t, changed, "兼容层不清理该形态")
			require.JSONEq(t, string(original), string(candidate))
			require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false))

			// 手工把 content 删掉的候选帧：该形态不满足清理条件，不得放行。
			stripped, err := sjson.DeleteBytes(original, "input.1.content")
			require.NoError(t, err)
			require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, stripped, "", false), "input was modified")
		})
	}
	t.Run("非推理项的 content 数组不放行", func(t *testing.T) {
		original := []byte(`{"type":"response.create","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
		stripped, err := sjson.DeleteBytes(original, "input.0.content")
		require.NoError(t, err)
		require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, stripped, "", false), "input was modified")
	})
	t.Run("其他类型的 content 数组不放行", func(t *testing.T) {
		for _, itemType := range []string{"custom_tool_call", "reasoning_summary", "Reasoning", "reasoningx", ""} {
			original := []byte(`{"type":"response.create","input":[{"type":"` + itemType + `","content":[{"type":"x","text":"y"}]}]}`)
			stripped, err := sjson.DeleteBytes(original, "input.0.content")
			require.NoError(t, err)
			require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, stripped, "", false), "input was modified", itemType)
		}
	})
}

// 负例：在“合法清理”的基础上再动其他任何业务字段，或者对 content 做清理之外的改动，仍然拒绝。
func TestOfficialWSReasoningContentReplayStillRejectsOtherModifications(t *testing.T) {
	original := reasoningReplayFrame(reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"示例推理历史"}]`), "gpt-6.1-sol")
	cleaned, changed := reasoningReplayCandidate(t, original)
	require.True(t, changed)
	require.NoError(t, validateUnifiedOpenAIWSBusinessContract(original, cleaned, "", false), "前提：只清理 content 时放行")

	set := func(t *testing.T, path string, value any) []byte {
		t.Helper()
		out, err := sjson.SetBytes(cleaned, path, value)
		require.NoError(t, err)
		return out
	}
	del := func(t *testing.T, path string) []byte {
		t.Helper()
		out, err := sjson.DeleteBytes(cleaned, path)
		require.NoError(t, err)
		return out
	}
	cases := map[string][]byte{
		"改消息正文":           set(t, "input.0.content.0.text", "被改的第一问"),
		"改消息角色":           set(t, "input.0.role", "developer"),
		"改助手消息正文":         set(t, "input.2.content.0.text", "被改的第一答"),
		"改工具参数":           set(t, "input.3.arguments", `{"q":"two"}`),
		"改工具名":            set(t, "input.3.name", "other"),
		"改配对 call_id":     set(t, "input.4.call_id", "call_other"),
		"改工具输出":           set(t, "input.4.output", "tampered"),
		"改推理摘要":           set(t, "input.1.summary.0.text", "被改的摘要"),
		"改推理密文":           set(t, "input.1.encrypted_content", "被改的密文"),
		"删推理密文":           del(t, "input.1.encrypted_content"),
		"删推理摘要":           del(t, "input.1.summary"),
		"改推理项 id":         set(t, "input.1.id", "rs_other_9999"),
		"清理之外再多一个字段":      set(t, "input.1.extra", "new"),
		"候选新增空 content":   set(t, "input.1.content", []any{}),
		"候选改写 content":    set(t, "input.1.content", []any{map[string]any{"type": "reasoning_text", "text": "改写"}}),
		"候选 content 为字符串": set(t, "input.1.content", "text"),
		"删整条推理记录":         del(t, "input.1"),
		"删后一项":            del(t, "input.5"),
		"多一条消息":           set(t, "input.-1", map[string]any{"type": "message", "role": "user", "content": "extra"}),
	}
	for name, candidate := range cases {
		t.Run(name, func(t *testing.T) {
			require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false), "input was modified")
		})
	}

	t.Run("原帧保留 content 而候选改写其值", func(t *testing.T) {
		candidate, err := sjson.SetBytes(original, "input.1.content.0.text", "改写")
		require.NoError(t, err)
		require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false), "input was modified")
	})
	t.Run("清理只豁免推理项：在别的项上做同样的删除仍拒绝", func(t *testing.T) {
		candidate, err := sjson.DeleteBytes(cleaned, "input.0.content")
		require.NoError(t, err)
		require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false), "input was modified")
	})
	t.Run("续链 ID 变化仍拒绝", func(t *testing.T) {
		withPrev := []byte(strings.Replace(string(original), `"type":"response.create",`, `"type":"response.create","previous_response_id":"resp_one",`, 1))
		candidate := []byte(strings.Replace(string(cleaned), `"type":"response.create",`, `"type":"response.create","previous_response_id":"resp_two",`, 1))
		require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(withPrev, candidate, "", false), "previous_response_id was modified")
	})
}

// 负例：孤立工具输出不得借本修复绕过保护。兼容层的旧版入站改写会清掉孤立的 function_call_output，
// 即使同一帧里也有可清理的推理项，校验仍须因输入被改而拒绝。
func TestOfficialWSReasoningContentReplayDoesNotLaunderOrphanToolOutput(t *testing.T) {
	original := []byte(`{"type":"response.create","model":"gpt-6.1-sol","input":[` +
		reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"x"}]`) + `,` +
		`{"type":"function_call_output","call_id":"call_missing","output":"must not disappear"}]}`)
	candidate, changed := reasoningReplayCandidate(t, original)
	require.True(t, changed)
	require.False(t, gjson.GetBytes(candidate, "input.0.content").Exists())
	require.Len(t, gjson.GetBytes(candidate, "input").Array(), 1, "兼容层确实清掉了孤立工具输出")
	require.ErrorContains(t, validateUnifiedOpenAIWSBusinessContract(original, candidate, "", false), "input was modified")
}

// 兼容层与校验器共用同一条清理规则：对同一批 content 形态与 type 形态，兼容层“是否清理”必须等于
// 校验器“是否接受清理后的候选”。任何一边改了条件而另一边没跟上，这里立刻变红。
func TestOfficialWSReasoningContentReplayRuleSharedBetweenCompatAndValidator(t *testing.T) {
	typeForms := []string{`"reasoning"`, `" reasoning "`, `"Reasoning"`, `"custom_tool_call"`, `"reasoning_summary"`, `null`, `7`, `""`}
	contentForms := []string{
		`[{"type":"reasoning_text","text":"x"}]`, `[1]`, `[null]`, `[[]]`, `[{}]`,
		`[]`, `null`, `""`, `"text"`, `{}`, `{"type":"reasoning_text"}`, `0`, `false`,
	}
	for _, typeForm := range typeForms {
		for _, contentForm := range contentForms {
			name := typeForm + "/" + contentForm
			t.Run(name, func(t *testing.T) {
				item := `{"type":` + typeForm + `,"summary":[],"content":` + contentForm + `,"encrypted_content":"c"}`
				original := []byte(`{"type":"response.create","model":"gpt-6.1-sol","input":[` + item + `]}`)
				stripped, err := sjson.DeleteBytes(original, "input.0.content")
				require.NoError(t, err)

				compatStrips := false
				if candidate, changed, compatErr := normalizeOpenAIResponsesReasoningContentReplay(original); compatErr == nil && changed {
					compatStrips = !gjson.GetBytes(candidate, "input.0.content").Exists()
				}
				validatorAccepts := validateUnifiedOpenAIWSBusinessContract(original, stripped, "", false) == nil

				require.Equal(t, compatStrips, validatorAccepts, "兼容层清理与校验器放行必须一致")
			})
		}
	}
}

// 比较只读：校验前后原帧与候选帧的对象树逐项不变；比较时只克隆单项，不为长历史生成两份完整 JSON。
func TestOfficialWSReasoningContentReplayComparisonIsReadOnlyAndLight(t *testing.T) {
	original := reasoningReplayFrame(reasoningReplayReasoningItem(`[{"type":"reasoning_text","text":"示例推理历史"}]`), "gpt-6.1-sol")
	candidate, changed := reasoningReplayCandidate(t, original)
	require.True(t, changed)
	left, _, err := (*officialWSFrameBody)(nil).decode(original)
	require.NoError(t, err)
	right, _, err := (*officialWSFrameBody)(nil).decode(candidate)
	require.NoError(t, err)
	leftBefore, _ := json.Marshal(left)
	rightBefore, _ := json.Marshal(right)
	equal, err := equalOfficialOpenAIWSBusinessHistory(left, right)
	require.NoError(t, err)
	require.True(t, equal)
	leftAfter, _ := json.Marshal(left)
	rightAfter, _ := json.Marshal(right)
	require.Equal(t, string(leftBefore), string(leftAfter), "不能修改原帧对象树")
	require.Equal(t, string(rightBefore), string(rightAfter), "不能修改候选对象树")
	require.True(t, gjson.GetBytes(leftAfter, "input.1.content").IsArray(), "原帧的 content 仍在")

	// 长历史：每条推理项带 8KiB 密文与非空 content。比较过程只处理单项副本。
	const items = 400
	blob := strings.Repeat("e", 8*1024)
	parts := make([]string, 0, items)
	for i := 0; i < items; i++ {
		parts = append(parts, fmt.Sprintf(`{"type":"reasoning","id":"rs_%04d","summary":[],"content":[{"type":"reasoning_text","text":"t"}],"encrypted_content":"%s"}`, i, blob))
	}
	longText := `{"type":"response.create","model":"gpt-6.1-sol","input":[` + strings.Join(parts, ",") + `]}`
	long := []byte(longText)
	longCandidate, longChanged := reasoningReplayCandidate(t, long)
	require.True(t, longChanged)
	longLeft, _, err := (*officialWSFrameBody)(nil).decode(long)
	require.NoError(t, err)
	longRight, _, err := (*officialWSFrameBody)(nil).decode(longCandidate)
	require.NoError(t, err)
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	longEqual, err := equalOfficialOpenAIWSBusinessHistory(longLeft, longRight)
	runtime.ReadMemStats(&after)
	require.NoError(t, err)
	require.True(t, longEqual)
	allocated := after.TotalAlloc - before.TotalAlloc
	// 逐项只编码单项副本（编码、清理、解码各一次，累计约 4 倍整帧大小），任一时刻只持有一项；
	// 这里取 6 倍整帧为累计分配上限，防止退化成对整段历史整体编码、再逐项复制。
	require.Less(t, allocated, uint64(6*len(long)), "比较累计分配 %d 字节，整帧 %d 字节", allocated, len(long))
}
