package service

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 跨模型旧会话（先用别家模型、后切 GPT 并自动压缩）的完整 WS 链路回归：
// 下游 WS 入站 → 兼容处理（清理推理项的非空 content 数组）→ 出站业务历史校验 → 真实 Executor 定型 → 假上游终端。
// 修复前这条链路在校验处返回“input was modified”，转 1008 断流，重试同样失败。
//
// 每个场景都在两套画像下各跑一遍：上一版 Active（withoutTargetProfileForWire，Active 已是目标版本时先摘掉补丁字段）与
// 目标版本画像（withTargetProfileForWire，上一版画像加补丁清单合成）。这些用例替换包级画像入口，不得并行。
// 样本是脱敏的结构样例，不含真实会话内容。

type reasoningReplayProfileCase struct {
	name  string
	apply func(t *testing.T, mode string)
}

func reasoningReplayProfiles() []reasoningReplayProfileCase {
	return []reasoningReplayProfileCase{
		{"上一版 Active 画像", func(t *testing.T, mode string) { withoutTargetProfileForWire(t, mode) }},
		{"目标版本画像", func(t *testing.T, mode string) { withTargetProfileForWire(t, mode) }},
	}
}

// reasoningReplayHistoryItems 是旧会话恢复出的完整历史：推理项带别家模型留下的可见推理块（非空 content 数组）。
func reasoningReplayHistoryItems(extra ...any) []any {
	items := []any{
		replayBaseItem(),
		codexGateMessage("developer", "developer context"),
		codexGateMessage("user", "第一问"),
		map[string]any{
			"type": "reasoning", "id": "rs_sample_0001",
			"summary":           []any{map[string]any{"type": "summary_text", "text": "示例摘要"}},
			"content":           []any{map[string]any{"type": "reasoning_text", "text": "示例推理历史"}},
			"encrypted_content": "示例密文",
		},
		map[string]any{"type": "message", "role": "assistant", "content": []any{map[string]any{"type": "output_text", "text": "第一答"}}},
		map[string]any{"type": "function_call", "id": "fc_sample_0001", "call_id": "call_sample_0001", "name": "lookup", "arguments": `{"q":"one"}`},
		map[string]any{"type": "function_call_output", "call_id": "call_sample_0001", "output": "ok"},
	}
	items = append(items, extra...)
	return append(items, codexGateMessage("user", "第二问"))
}

// reasoningReplayRequireUpstreamCleaned 断言假上游收到的每个业务帧：推理项已无 content，summary 与 encrypted_content 原样，
// 业务消息仍在。
func reasoningReplayRequireUpstreamCleaned(t *testing.T, handshakes []codexGateWireRequest, wantReasoningFrames int) {
	t.Helper()
	seen := 0
	for _, handshake := range handshakes {
		for _, frame := range codexGateResponseCreateFrames(handshake) {
			reasoning := 0
			gjson.GetBytes(frame, "input").ForEach(func(_, item gjson.Result) bool {
				if item.Get("type").String() != "reasoning" {
					return true
				}
				reasoning++
				require.False(t, item.Get("content").Exists(), "推理项的 content 数组应已清理：%s", item.Raw)
				require.Equal(t, "示例摘要", item.Get("summary.0.text").String())
				require.Equal(t, "示例密文", item.Get("encrypted_content").String())
				return true
			})
			if reasoning > 0 {
				seen++
				require.Contains(t, string(frame), "第二问", "业务消息原样随历史发往上游")
			}
		}
	}
	require.GreaterOrEqual(t, seen, wantReasoningFrames, "上游应收到带推理项的业务帧")
}

// 恢复完整历史、切换模型、自动压缩、压缩后的新窗口：第三方客户端每轮整段历史，全部放行。
func TestOpenAIWSCrossModelReasoningContentHistoryPassesEgressValidation(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			steps := []replayStep{
				// 恢复旧会话：整段历史首帧（reason=restored_history）。
				replayFullFrame(t, "gpt-5.5", reasoningReplayHistoryItems()...),
				// 切换模型：同一历史、换模型再发一轮。
				replayFullFrame(t, "gpt-5.4", reasoningReplayHistoryItems(codexGateMessage("user", "追问"))...),
				// 自动压缩：整段历史加末项 compaction_trigger（官方压缩请求的 trigger 恒为 input 末项）。
				replayFullFrame(t, "gpt-5.4", append(reasoningReplayHistoryItems(), map[string]any{"type": "compaction_trigger"})...),
				// 压缩之后的新窗口：历史被缩短，仍带推理项。
				replayFullFrame(t, "gpt-5.4", append([]any{replayBaseItem(), codexGateMessage("user", "压缩摘要")}, reasoningReplayHistoryItems()[3], codexGateMessage("user", "第二问"))...),
			}
			handshakes := replayRunIngress(t, mode, codexGateThirdPartyUserAgent, steps)
			require.NotEmpty(t, handshakes)
			reasoningReplayRequireUpstreamCleaned(t, handshakes, 4)
		})
	}
}

// 官方形态客户端自管续链（先发预热帧、再续链、再整段重放）：整段重放帧与首个业务帧里的推理项同样被清理并放行。
func TestOpenAIWSCrossModelReasoningContentHistoryPassesForOfficialShapedClient(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			steps := []replayStep{
				replayPrewarmFrame(t, "gpt-5.5"),
				// 首个业务帧：链到预热响应上，input 是恢复出的完整历史。
				replayChainFrame(t, "gpt-5.5", reasoningReplayHistoryItems()...),
				// 换模型，官方不续链，整段重放（不带 previous_response_id，不带 generate）。
				replayFullFrame(t, "gpt-5.4", reasoningReplayHistoryItems(codexGateMessage("user", "追问"))...),
				// 重放之后恢复增量：新增项里不再带推理项也照常。
				replayChainFrame(t, "gpt-5.4", codexGateMessage("user", "再问")),
			}
			handshakes := replayRunIngress(t, mode, replayOfficialUserAgent, steps)
			require.NotEmpty(t, handshakes)
			reasoningReplayRequireUpstreamCleaned(t, handshakes, 2)
		})
	}
}

// 断线重试：上游在业务帧上断开，换新连接重试同一帧。重试用定型之前的帧（已清理 content）重新定型，
// 与客户端原帧（仍带 content）比对依然放行；新连接上的帧与没有断开时一致。
func TestOpenAIWSCrossModelReasoningContentHistoryRetryAfterUpstreamClosed(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			steps := []replayStep{replayFullFrame(t, "gpt-5.5", reasoningReplayHistoryItems()...)}
			baseline := replayRunWithUpstreamClose(t, mode, codexGateThirdPartyUserAgent, 0, 0, steps)
			retried := replayRunWithUpstreamClose(t, mode, codexGateThirdPartyUserAgent, 1, 2, steps)
			require.Equal(t, len(baseline)+1, len(retried), "断开后恰多一条新连接，会话照常完成")
			reasoningReplayRequireUpstreamCleaned(t, retried[len(retried)-1:], 1)
			last := codexGateResponseCreateFrames(retried[len(retried)-1])
			baseLast := codexGateResponseCreateFrames(baseline[len(baseline)-1])
			require.Equal(t, len(baseLast), len(last))
			for index := range last {
				require.Equal(t,
					replayFrameFieldsExcept(baseLast[index], "previous_response_id", "client_metadata"),
					replayFrameFieldsExcept(last[index], "previous_response_id", "client_metadata"),
					"第 %d 帧：重试后的帧与没有断开时的帧内容一致", index+1)
			}
		})
	}
}

// 同一 API 的普通新会话不受影响：没有推理项 content 的历史照常放行，上游帧里的推理项与消息逐项不变（不被本修复触碰）。
func TestOpenAIWSPlainSessionUnaffectedByReasoningContentReplayFix(t *testing.T) {
	for _, profile := range reasoningReplayProfiles() {
		t.Run(profile.name, func(t *testing.T) {
			mode := officialClientProfileModeActive
			profile.apply(t, mode)
			plain := []any{
				replayBaseItem(),
				codexGateMessage("developer", "developer context"),
				codexGateMessage("user", "第一问"),
				map[string]any{
					"type": "reasoning", "id": "rs_sample_0002", "summary": []any{map[string]any{"type": "summary_text", "text": "示例摘要"}},
					"content": []any{}, "encrypted_content": "示例密文",
				},
				codexGateMessage("user", "第二问"),
			}
			steps := []replayStep{
				replayFullFrame(t, "gpt-5.5", plain...),
				replayFullFrame(t, "gpt-5.5", append(append([]any{}, plain...), codexGateMessage("user", "第三问"))...),
			}
			handshakes := replayRunIngress(t, mode, codexGateThirdPartyUserAgent, steps)
			require.NotEmpty(t, handshakes)
			checked := 0
			for _, handshake := range handshakes {
				for _, frame := range codexGateResponseCreateFrames(handshake) {
					gjson.GetBytes(frame, "input").ForEach(func(_, item gjson.Result) bool {
						if item.Get("type").String() == "reasoning" {
							checked++
							require.True(t, item.Get("content").IsArray(), "空数组不是清理对象，保持原样：%s", item.Raw)
							require.Equal(t, "示例密文", item.Get("encrypted_content").String())
						}
						return true
					})
				}
			}
			require.Positive(t, checked)
		})
	}
}

// 防御：本文件的用例都用第三方或官方形态 UA 走真实 WS 入站；确保 UA 常量没有被改成空导致落入别的分支。
func TestOpenAIWSReasoningContentReplayUsesDistinctClientShapes(t *testing.T) {
	require.NotEqual(t, codexGateThirdPartyUserAgent, replayOfficialUserAgent)
	require.NotEmpty(t, http.Header{"User-Agent": []string{replayOfficialUserAgent}}.Get("User-Agent"))
}
