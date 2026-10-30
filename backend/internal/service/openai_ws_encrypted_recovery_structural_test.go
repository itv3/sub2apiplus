package service

import (
	"errors"
	"strconv"
	"testing"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

// 本文件锁定两件事（走真实 WS 入站的完整链路见 openai_ws_encrypted_recovery_ingress_test.go）：
//
//  1. 结构性扩展（一次清完）：被唯一定位的推理项带非空 content 数组时，同请求里全部“带非空 content 的推理项”一并记为失效；
//     判断只看结构，不看密文长度、前缀、结尾或模型名；
//  2. 结束本轮：invalid_encrypted_content 的 error 写出后返回 1000 的客户端关闭错误，其余事件与情形一律不动。
//
// 样本均为脱敏的结构样例。

// recoveryStructuralItems 构造一份典型历史：三个别家模型留下的推理项（带 content）、一个 OpenAI 格式的有效推理项（无 content）、
// 一个加密压缩项。
func recoveryStructuralItems() []string {
	return []string{
		recoveryUserMessage("第一问"),
		recoveryReasoningItemWithContent("A1"),
		recoveryReasoningItem("good"),
		recoveryReasoningItemWithContent("A2"),
		recoveryCompactionItem("cmp"),
		recoveryReasoningItemWithContent("A3"),
		recoveryUserMessage("第二问"),
	}
}

func TestCollectOpenAIWSEncryptedCandidatesMarksContentBearingReasoning(t *testing.T) {
	payload := recoveryPayload(
		recoveryReasoningItemWithContent("withContent"),
		recoveryReasoningItem("plain"),
		// 空数组、字符串、null 都不算“非空 content 数组”。
		`{"type":"reasoning","id":"rs_e","summary":[],"content":[],"encrypted_content":"`+recoveryCipher("emptyArr")+`"}`,
		`{"type":"reasoning","id":"rs_s","summary":[],"content":"text","encrypted_content":"`+recoveryCipher("strContent")+`"}`,
		`{"type":"reasoning","id":"rs_n","summary":[],"content":null,"encrypted_content":"`+recoveryCipher("nullContent")+`"}`,
		// 压缩项即使带 content 也不算（只有推理项参与扩展）。
		`{"type":"compaction","id":"cmp_c","content":[{"type":"x"}],"encrypted_content":"`+recoveryCipher("cmpContent")+`"}`,
	)
	candidates := collectOpenAIWSEncryptedCandidates(payload, openAIWSCiphertextHint{form: openAIWSHintNone})
	require.Len(t, candidates, 6)
	byDigest := map[string]openAIWSEncryptedCandidate{}
	for _, candidate := range candidates {
		byDigest[candidate.digest] = candidate
	}
	hasContent := func(label string) bool {
		return byDigest[openAIEncryptedContentDigest(recoveryCipher(label))].hasContent
	}
	require.True(t, hasContent("withContent"))
	for _, label := range []string{"plain", "emptyArr", "strContent", "nullContent", "cmpContent"} {
		require.False(t, hasContent(label), label)
	}

	// 同一摘要出现在多个项里：任一项带 content 即为真。
	dup := recoveryPayload(recoveryReasoningItem("dup"), recoveryReasoningItemWithContent("dup"))
	dupCandidates := collectOpenAIWSEncryptedCandidates(dup, openAIWSCiphertextHint{form: openAIWSHintNone})
	require.Len(t, dupCandidates, 1)
	require.True(t, dupCandidates[0].hasContent)
}

// 入站兼容层会在转发上游前删掉 content，所以要回看客户端原帧补记 hasContent；候选范围仍以发往上游的帧为准。
func TestMarkOpenAIWSClientContentBearingCandidates(t *testing.T) {
	sent := recoveryPayload(recoveryReasoningItem("A1"), recoveryReasoningItem("good"), recoveryReasoningItem("A2"))
	original := recoveryPayload(
		recoveryReasoningItemWithContent("A1"),
		recoveryReasoningItem("good"),
		recoveryReasoningItemWithContent("A2"),
		recoveryReasoningItemWithContent("onlyInOriginal"), // 不在发往上游的帧里：不新增候选
	)
	candidates := collectOpenAIWSEncryptedCandidates(sent, openAIWSCiphertextHint{form: openAIWSHintNone})
	require.Len(t, candidates, 3)
	for _, candidate := range candidates {
		require.False(t, candidate.hasContent, "发往上游的帧里 content 已被入站兼容层清掉")
	}
	markOpenAIWSClientContentBearingCandidates(candidates, original)
	require.Len(t, candidates, 3, "原帧独有的项不新增候选")
	require.True(t, candidates[0].hasContent)
	require.False(t, candidates[1].hasContent)
	require.True(t, candidates[2].hasContent)

	// 空原帧、没有 input、压缩项带 content：都不改动。
	untouched := collectOpenAIWSEncryptedCandidates(sent, openAIWSCiphertextHint{form: openAIWSHintNone})
	markOpenAIWSClientContentBearingCandidates(untouched, nil)
	markOpenAIWSClientContentBearingCandidates(untouched, []byte(`{"model":"gpt-5"}`))
	markOpenAIWSClientContentBearingCandidates(nil, original)
	asCompaction := recoveryPayload(`{"type":"compaction","id":"c","content":[{"type":"x"}],"encrypted_content":"` + recoveryCipher("A1") + `"}`)
	markOpenAIWSClientContentBearingCandidates(untouched, asCompaction)
	for _, candidate := range untouched {
		require.False(t, candidate.hasContent)
	}
}

// 决策表：被唯一定位的推理项带 content → 扩展为全部带 content 的推理项（且只含推理项）；
// 不带 content → 只记它自己；压缩项不扩展；无法唯一定位等现有路径不变。
func TestDecideOpenAIWSEncryptedRecoveryStructuralExpansion(t *testing.T) {
	a1, a2, a3 := recoveryCipher("A1"), recoveryCipher("A2"), recoveryCipher("A3")
	good, cmp := recoveryCipher("good"), recoveryCipher("cmp")
	decide := func(items []string, message string, outputCommitted bool) openAIWSEncryptedRecoveryDecision {
		hint := parseOpenAIWSRejectedCiphertextHint(message)
		return decideOpenAIWSEncryptedRecovery(collectOpenAIWSEncryptedCandidates(recoveryPayload(items...), hint), hint, outputCommitted)
	}

	t.Run("片段唯一吻合且带 content：扩展为全部带 content 的推理项，只含推理项", func(t *testing.T) {
		decision := decide(recoveryStructuralItems(), recoveryHintMessage(a2), false)
		require.Equal(t, openAIWSRecoveryActionStructural, decision.action)
		require.Equal(t, openAIWSRecoveryResultRecorded, decision.result)
		require.Equal(t, "fragment", decision.reason)
		require.Equal(t, 1, decision.matched)
		require.ElementsMatch(t, []string{openAIEncryptedContentDigest(a1), openAIEncryptedContentDigest(a2), openAIEncryptedContentDigest(a3)}, decision.digests)
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(good), "OpenAI 格式（无 content）的有效推理项不被误清")
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(cmp), "压缩项永不入列")
	})
	t.Run("定位到的项不带 content：只记它自己，行为不变", func(t *testing.T) {
		decision := decide(recoveryStructuralItems(), recoveryHintMessage(good), false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, []string{openAIEncryptedContentDigest(good)}, decision.digests)
		require.Equal(t, "fragment", decision.reason)
	})
	t.Run("定位到压缩项：不记录，不因旁边有带 content 的推理项而扩展", func(t *testing.T) {
		decision := decide(recoveryStructuralItems(), recoveryHintMessage(cmp), false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "compaction_item", decision.reason)
		require.Empty(t, decision.digests)
	})
	t.Run("只有一个候选且带 content：仍是 precise（无可扩展）", func(t *testing.T) {
		decision := decide([]string{recoveryReasoningItemWithContent("A1")}, "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, "only_candidate", decision.reason)
		require.Equal(t, []string{openAIEncryptedContentDigest(a1)}, decision.digests)
	})
	t.Run("被定位项带 content，但请求里只有它一个带 content：precise", func(t *testing.T) {
		decision := decide([]string{recoveryReasoningItemWithContent("A1"), recoveryReasoningItem("good"), recoveryCompactionItem("cmp")}, recoveryHintMessage(a1), false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, []string{openAIEncryptedContentDigest(a1)}, decision.digests)
	})
	t.Run("同一密文既是推理项又是压缩项：按压缩项保守处理，不记录", func(t *testing.T) {
		items := []string{recoveryReasoningItemWithContent("A1"), `{"type":"compaction","encrypted_content":"` + a1 + `"}`, recoveryReasoningItemWithContent("A2")}
		decision := decide(items, recoveryHintMessage(a1), false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "compaction_item", decision.reason)
	})
	t.Run("已输出过内容：不推断，不扩展", func(t *testing.T) {
		decision := decide(recoveryStructuralItems(), recoveryHintMessage(a2), true)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "output_committed", decision.reason)
		require.Empty(t, decision.digests)
	})
	t.Run("无法唯一定位的现有路径不变：没有片段仍是整包类，且请求里有压缩项就不恢复", func(t *testing.T) {
		decision := decide(recoveryStructuralItems(), "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "no_fragment_with_compaction", decision.reason)

		noCompaction := []string{recoveryReasoningItemWithContent("A1"), recoveryReasoningItem("good"), recoveryReasoningItemWithContent("A2")}
		decision = decide(noCompaction, "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action, "整包类仍是 broad，不是 structural")
		require.Len(t, decision.digests, 3, "整包类记全部推理项（含无 content 的）")
	})
	t.Run("不凭密文外观：密文长度、前缀、结尾不影响扩展，只看 content 结构", func(t *testing.T) {
		short := `{"type":"reasoning","id":"rs_x","summary":[],"content":[{"type":"reasoning_text","text":"t"}],"encrypted_content":"gAAAshort-0"}`
		long := `{"type":"reasoning","id":"rs_y","summary":[],"content":[{"type":"reasoning_text","text":"t"}],"encrypted_content":"` + recoveryCipher("LONG") + recoveryCipher("LONG2") + `"}`
		// 外观上与别家密文一模一样（短、gAAA 前缀、-0 结尾），但没有 content：不扩展，也不被误清。
		lookalike := `{"type":"reasoning","id":"rs_z","summary":[],"encrypted_content":"gAAAlookalike-0"}`
		decision := decide([]string{short, long, lookalike}, recoveryHintMessage("gAAAshort-0"), false)
		require.Equal(t, openAIWSRecoveryActionStructural, decision.action)
		require.ElementsMatch(t, []string{openAIEncryptedContentDigest("gAAAshort-0"), openAIEncryptedContentDigest(recoveryCipher("LONG") + recoveryCipher("LONG2"))}, decision.digests)
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest("gAAAlookalike-0"))
	})
}

// 记录：结构性扩展一次记完，只算一次恢复计次、不占整包预算；日志 located=structural、marked=N；
// 发往上游的帧里 content 已被清掉，必须靠登记的客户端原帧判断。
func TestRecordOpenAIWSRejectedEncryptedContentStructuralUsesClientFrameAndKeepsBudget(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	a1, a2, a3 := recoveryCipher("A1"), recoveryCipher("A2"), recoveryCipher("A3")

	// 客户端原帧带 content；发往上游的帧（入站兼容层之后）不带。
	original := recoveryPayload(
		recoveryUserMessage("第一问"),
		recoveryReasoningItemWithContent("A1"), recoveryReasoningItem("good"),
		recoveryReasoningItemWithContent("A2"), recoveryCompactionItem("cmp"), recoveryReasoningItemWithContent("A3"),
	)
	sent := recoveryPayload(
		recoveryUserMessage("第一问"),
		recoveryReasoningItem("A1"), recoveryReasoningItem("good"),
		recoveryReasoningItem("A2"), recoveryCompactionItem("cmp"), recoveryReasoningItem("A3"),
	)

	// 进场钩子登记原帧（这里只为登记，快照本身为空）。
	body := &officialWSFrameBody{}
	require.Nil(t, svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, original))
	require.Equal(t, original, openAIWSRecoveryClientFrame(c))

	logs := captureOpenAIWSRecoveryLogs(t, func() {
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, sent, recoveryHintMessage(a2), false)
	})
	require.Equal(t, recoveryDigests(a1, a2, a3), recoveryScopedDigests(svc, 3, "s", account), "一次记完全部带 content 的推理项；有效项与压缩项不记")
	for _, want := range []string{"located=structural", "marked=3", "matched=1", "result=recorded", "reason=fragment", "attempts=1", "candidates=5"} {
		require.Contains(t, logs, want)
	}
	for _, secret := range []string{a1, a2, a3, recoveryCipher("good"), recoveryCipher("cmp")} {
		require.NotContains(t, logs, secret, "日志不含密文")
	}

	// 只消耗一次恢复计次（mark-1），不占整包预算（没有 broad 标记）。
	budgetKey := openAIWSEncryptedRecoveryScopeKey("s", account) + openAIWSEncryptedRecoveryBudgetSuffix
	used := svc.getOpenAIWSStateStore().GetSessionInvalidEncryptedContentDigests(3, budgetKey)
	require.Len(t, used, 1)
	require.Contains(t, used, "mark-1")
	require.NotContains(t, used, openAIWSEncryptedRecoveryBroadToken)

	// 整包预算仍然完整：之后同一会话仍可做一次整包类恢复。
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 2,
		recoveryPayload(recoveryReasoningItem("p1"), recoveryReasoningItem("p2")), "The encrypted content could not be verified", false)
	require.Contains(t, recoveryScopedDigests(svc, 3, "s", account), openAIEncryptedContentDigest(recoveryCipher("p1")), "整包预算未被结构性扩展消耗")
}

// 没有登记原帧（或原帧不带 content）时只看发往上游的帧：带 content 的帧（兼容层未清理的账号类型）照样可扩展；
// 登记的原帧不带 content 则不扩展。
func TestRecordOpenAIWSRejectedEncryptedContentStructuralFallbacks(t *testing.T) {
	a1, a2 := recoveryCipher("A1"), recoveryCipher("A2")
	account := recoveryTestAccount(90)

	t.Run("没登记原帧：发往上游的帧自己带 content 即可扩展", func(t *testing.T) {
		svc := recoveryTestService()
		c := recoveryTestContext()
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1,
			recoveryPayload(recoveryReasoningItemWithContent("A1"), recoveryReasoningItemWithContent("A2"), recoveryReasoningItem("good")),
			recoveryHintMessage(a1), false)
		require.Equal(t, recoveryDigests(a1, a2), recoveryScopedDigests(svc, 3, "s", account))
	})
	t.Run("原帧也没有 content（全是 OpenAI 格式）：只记被拒项", func(t *testing.T) {
		svc := recoveryTestService()
		c := recoveryTestContext()
		payload := recoveryPayload(recoveryReasoningItem("A1"), recoveryReasoningItem("A2"), recoveryReasoningItem("good"))
		require.Nil(t, svc.openAIWSIngressRecoverySnapshot(c, &officialWSFrameBody{}, 3, "s", account, payload))
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, payload, recoveryHintMessage(a1), false)
		require.Equal(t, recoveryDigests(a1), recoveryScopedDigests(svc, 3, "s", account))
	})
	t.Run("nil 上下文安全", func(t *testing.T) {
		svc := recoveryTestService()
		require.Nil(t, openAIWSRecoveryClientFrame(nil))
		require.Nil(t, svc.openAIWSIngressRecoverySnapshot(nil, &officialWSFrameBody{}, 3, "s", account, []byte(`{}`)))
	})
}

// 清理与校验用同一份摘要集合，两侧都不看动作类别：结构性扩展记下的集合，进场清理全部剥掉，出站校验放行。
func TestStructuralRecoveryCleanupAndValidationShareOneDigestSet(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	a1, a2, a3 := recoveryCipher("A1"), recoveryCipher("A2"), recoveryCipher("A3")
	original := recoveryPayload(recoveryStructuralItems()...)
	body := &officialWSFrameBody{}
	require.Nil(t, svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, original))
	// 线上记录时用的是兼容层之后的帧。
	sent, _, err := normalizeOpenAIResponsesWebSocketCompatibilityBody(original, reasoningReplayOAuthAccount(), false)
	require.NoError(t, err)
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, sent, recoveryHintMessage(a2), false)
	require.Equal(t, recoveryDigests(a1, a2, a3), recoveryScopedDigests(svc, 3, "s", account))

	snapshot := svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, original)
	require.Equal(t, recoveryDigests(a1, a2, a3), snapshot)
	candidate := recoveryPipeline(t, original, snapshot)
	for _, cipher := range []string{a1, a2, a3} {
		require.NotContains(t, string(candidate), cipher, "进场清理剥掉全部记录的密文")
	}
	require.Contains(t, string(candidate), recoveryCipher("good"), "有效推理项密文原样保留")
	require.Contains(t, string(candidate), recoveryCipher("cmp"), "压缩项原样保留")
	require.True(t, recoveryEqual(t, original, candidate, body.invalidEncryptedRecovery()), "出站校验按同一份快照放行")
	require.False(t, recoveryEqual(t, original, candidate, nil), "没有恢复依据时同一候选仍判被修改")
	for _, text := range []string{"摘要-A1", "摘要-A2", "摘要-A3"} {
		require.Contains(t, string(candidate), text, "推理骨架（摘要）保留")
	}
}

type recoveryLeaseSpy struct{ broken int }

func (l *recoveryLeaseSpy) MarkBroken() { l.broken++ }

// 结束本轮：只对“尚无 token 输出的 invalid_encrypted_content error 事件”返回 1000 的客户端关闭错误并标记连接损坏；
// 其余事件、其余错误、已有输出，一律返回 nil（保持原行为）。
func TestEndOpenAIWSTurnOnInvalidEncryptedContent(t *testing.T) {
	invalid := []byte(`{"type":"error","status":400,"error":{"type":"invalid_request_error","code":"invalid_encrypted_content","message":"The encrypted content gAAA...64D5 could not be verified."}}`)

	t.Run("invalid_encrypted_content 的 error：1000 关闭，连接标记损坏", func(t *testing.T) {
		lease := &recoveryLeaseSpy{}
		err := endOpenAIWSTurnOnInvalidEncryptedContent(lease, "error", invalid, 0, 9, 1)
		var closeErr *OpenAIWSClientCloseError
		require.ErrorAs(t, err, &closeErr)
		require.Equal(t, coderws.StatusNormalClosure, closeErr.StatusCode())
		require.Equal(t, openAIWSEndTurnCloseReason, closeErr.Reason())
		require.Equal(t, 1, lease.broken)
		// 不是 turn 错误：不会被当作可换连接重试的读写错误，也不会补发 upstream_websocket_error。
		require.False(t, isOpenAIWSIngressTurnRetryable(err))
		require.False(t, shouldEmitOpenAIWSUpstreamFailureEvent(openAIWSIngressTurnRetryReason(err), err))
		require.False(t, isOpenAIWSIngressPreviousResponseNotFound(err))
		// finalErr 的取法（errors.Unwrap 后为 nil 则保持原值）不会丢掉关闭码。
		require.NoError(t, errors.Unwrap(err))
	})
	t.Run("只靠文案也认（无 code 字段）", func(t *testing.T) {
		message := []byte(`{"type":"error","error":{"message":"The encrypted content gAAA...64D5 could not be verified. Reason: x"}}`)
		require.Error(t, endOpenAIWSTurnOnInvalidEncryptedContent(&recoveryLeaseSpy{}, "error", message, 0, 9, 1))
	})
	t.Run("已有 token 输出：保持原行为", func(t *testing.T) {
		lease := &recoveryLeaseSpy{}
		require.NoError(t, endOpenAIWSTurnOnInvalidEncryptedContent(lease, "error", invalid, 1, 9, 1))
		require.Zero(t, lease.broken)
	})
	t.Run("别的事件与别的错误不受影响", func(t *testing.T) {
		for name, event := range map[string]struct {
			eventType string
			message   string
		}{
			"response.failed 带同一错误码":      {"response.failed", `{"type":"response.failed","response":{"error":{"code":"invalid_encrypted_content","message":"x"}}}`},
			"response.completed":          {"response.completed", `{"type":"response.completed"}`},
			"速率限制 error":                  {"error", `{"type":"error","error":{"type":"rate_limit_error","code":"rate_limit_exceeded","message":"Rate limit reached"}}`},
			"previous_response_not_found": {"error", `{"type":"error","error":{"code":"previous_response_not_found","message":"x"}}`},
			"普通 400 error":                {"error", `{"type":"error","status":400,"error":{"type":"invalid_request_error","code":"invalid_value","message":"bad"}}`},
		} {
			lease := &recoveryLeaseSpy{}
			require.NoError(t, endOpenAIWSTurnOnInvalidEncryptedContent(lease, event.eventType, []byte(event.message), 0, 9, 1), name)
			require.Zero(t, lease.broken, name)
		}
	})
	t.Run("lease 为空也不 panic", func(t *testing.T) {
		require.Error(t, endOpenAIWSTurnOnInvalidEncryptedContent(nil, "error", invalid, 0, 9, 1))
	})
}

// 账号状态不受影响：这类 error 事件经过的账号侧入口——瞬时故障冷却、限流信号、错误分类——对它都是空操作；
// handler 侧对 1000 的客户端关闭错误不上报调度失败（见 handler 包的对应用例）。
func TestInvalidEncryptedContentErrorEventDoesNotTouchAccountState(t *testing.T) {
	for _, status := range []string{`"status":400,`, ``} {
		event := []byte(`{"type":"error",` + status + `"error":{"type":"invalid_request_error","code":"invalid_encrypted_content","message":"The encrypted content gAAA...64D5 could not be verified. Reason: Encrypted content could not be decrypted or parsed."}}`)
		t.Run("status="+strconv.Quote(status), func(t *testing.T) {
			require.Zero(t, openAIWSPayloadTransientStatus(event), "不触发瞬时故障冷却（handleOpenAIWSErrorEventTransientFailure 据此判定）")
			code, errType, message := parseOpenAIWSErrorEventFields(event)
			require.False(t, isOpenAIWSRateLimitError(code, errType, message), "不触发限流信号（persistOpenAIWSRateLimitSignal 据此判定）与限流 failover")
			reason, _ := classifyOpenAIWSErrorEventFromRaw(code, errType, message)
			require.Equal(t, openAIWSFallbackReasonInvalidEncryptedContent, reason)
		})
	}
}
