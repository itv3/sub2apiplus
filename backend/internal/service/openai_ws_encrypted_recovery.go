package service

import (
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

// 原生 WS 入口（ProxyResponsesWebSocketFromClient）的“失效密文受控恢复”。
//
// 背景：先用别家模型、后切 GPT 的旧会话，历史里混有 GPT 上游无法解密的 encrypted_content。上游以
// invalid_encrypted_content 拒绝整包，错误里只带被拒密文的“首 4 位...末 4 位”片段。
// 上游文件 openai_encrypted_content_lineage.go 的做法是把整包所有密文摘要记入会话，下次进场统一剥离；
// 这带来两个问题：一是把请求里合法的 GPT 密文也一并标坏、剥掉；二是进场清理只改“待发送帧”，出站业务历史校验
// 仍拿客户端原帧比对，把清理判成 input was modified，转 1008 断流，客户端反复重连反复失败。
//
// 本文件只放 fork 自己的逻辑，对上游文件只通过三个钩子接入（各一两行）：
//  1. recordOpenAIWSRejectedEncryptedContent：上游拒绝后，按错误里的密文片段精确定位被拒的那一项，只记它；
//     记录挂在“账号 + 协议 + 会话”作用域键下，换号、换协议、换会话不会复用；
//  2. openAIWSIngressRecoverySnapshot：下次请求进场时，对同一作用域键取一次摘要快照，同一份快照既交给上游
//     剥离函数做清理，也交给出站业务历史校验（见 official_egress_ws_body.go），清理与校验不会错位；
//  3. emitOpenAIWSRecoveryValidationFailure：恢复清理之后校验仍失败时，先给客户端一个明确的错误事件再关连接。
//
// 恢复边界（下列情形一律不记录，客户端照常收到上游的原始错误，历史不会被静默改写）：
//   - 被拒项是 compaction／compaction_summary：它可能是早期上下文的唯一副本，请求里没有可信的替代历史，不得静默删除；
//   - 本次请求已经向客户端输出过文字或工具调用（见 outputCommitted）：不是纯输入校验失败，不做推断；
//   - 无法唯一定位且请求里还有压缩项：被拒的可能正是压缩项，不做整包恢复；
//   - 同一作用域键在窗口内已记录 openAIWSEncryptedRecoveryMaxMarks 次：视为不可恢复，不再记录。
//
// 无法唯一定位、但请求里只有推理项时，允许一次（同一作用域键窗口内仅一次）有边界的“整包推理项”恢复，
// 见 decideOpenAIWSEncryptedRecovery；这是上游原有行为的收紧版：不含压缩项、限一次、挂在账号作用域下。
//
// 日志只记摘要个数、结果与原因，不记密文、片段、认证信息或会话正文。

const (
	// 同一“账号 + 协议 + 会话”在窗口内最多记录的恢复次数（精确定位与整包恢复合计）。
	openAIWSEncryptedRecoveryMaxMarks = 8
	// 恢复次数计数的窗口：每次记录都会顺延；记满后不再记录，窗口自然过期后才重新计数。
	openAIWSEncryptedRecoveryWindow = 10 * time.Minute
	// 恢复次数计数用的伪会话键后缀与整包恢复标记（复用 OpenAIWSStateStore 的“会话 → 字符串集合 + TTL + 容量自保护”）。
	openAIWSEncryptedRecoveryBudgetSuffix = "|recovery-budget"
	openAIWSEncryptedRecoveryBroadToken   = "broad"
	// 错误片段里“首尾可见证据”的最小总长度：太短的片段区分度不足，不作为定位依据。
	openAIWSEncryptedHintMinEvidence = 6
)

// 上游文案形如：The encrypted content gAAA...Q== could not be verified. Reason: ...
var (
	openAIWSRejectedCiphertextPattern = regexp.MustCompile(`(?is)the encrypted content\s+(\S+)\s+could not be verified`)
	openAIWSCiphertextEllipsisPattern = regexp.MustCompile(`\.{3,}|…|\*{3,}`)
)

const (
	openAIWSHintNone     = "none"
	openAIWSHintExact    = "exact"
	openAIWSHintEllipsis = "ellipsis"
)

// openAIWSCiphertextHint 是从上游错误文案里取出的被拒密文片段。exact 表示文案给出了完整值，
// ellipsis 表示只给了“首...尾”。片段本身不写日志。
type openAIWSCiphertextHint struct {
	form string
	head string
	tail string
}

// parseOpenAIWSRejectedCiphertextHint 解析上游文案里的被拒密文片段；没有或证据不足时返回 none。
func parseOpenAIWSRejectedCiphertextHint(message string) openAIWSCiphertextHint {
	match := openAIWSRejectedCiphertextPattern.FindStringSubmatch(message)
	if match == nil {
		return openAIWSCiphertextHint{form: openAIWSHintNone}
	}
	fragment := strings.Trim(match[1], "\"'`[]()<>")
	if fragment == "" {
		return openAIWSCiphertextHint{form: openAIWSHintNone}
	}
	loc := openAIWSCiphertextEllipsisPattern.FindStringIndex(fragment)
	if loc == nil {
		return openAIWSCiphertextHint{form: openAIWSHintExact, head: fragment}
	}
	head, tail := fragment[:loc[0]], fragment[loc[1]:]
	if len(head)+len(tail) < openAIWSEncryptedHintMinEvidence {
		return openAIWSCiphertextHint{form: openAIWSHintNone}
	}
	return openAIWSCiphertextHint{form: openAIWSHintEllipsis, head: head, tail: tail}
}

// matches 判断某条密文是否与片段一致。完整值必须全等；“首...尾”要求前缀与后缀同时吻合且长度足够。
func (h openAIWSCiphertextHint) matches(value string) bool {
	switch h.form {
	case openAIWSHintExact:
		return value == h.head
	case openAIWSHintEllipsis:
		return len(value) >= len(h.head)+len(h.tail) &&
			strings.HasPrefix(value, h.head) && strings.HasSuffix(value, h.tail)
	default:
		return false
	}
}

const (
	openAIWSCandidateReasoning  = "reasoning"
	openAIWSCandidateCompaction = "compaction"
)

// openAIWSEncryptedCandidate 是请求里一条带密文的历史项（按摘要去重）。
type openAIWSEncryptedCandidate struct {
	digest string
	// kind 是 reasoning 或 compaction（compaction／compaction_summary 都归 compaction；同一摘要出现在两种项里时按 compaction 保守处理）。
	kind    string
	matched bool
}

// collectOpenAIWSEncryptedCandidates 扫描 input，收集 lineage 覆盖类型（reasoning、compaction、compaction_summary）里
// 带非空字符串密文的项，按摘要去重；同时标出哪些与错误片段吻合。payload 须遵守 replay 所有权不变式（零拷贝视图只读解析）。
func collectOpenAIWSEncryptedCandidates(payload []byte, hint openAIWSCiphertextHint) []openAIWSEncryptedCandidate {
	if len(payload) == 0 {
		return nil
	}
	input := gjson.Get(openAIWSPayloadStringView(payload), "input")
	if !input.Exists() {
		return nil
	}
	var candidates []openAIWSEncryptedCandidate
	indexByDigest := map[string]int{}
	appendItem := func(item gjson.Result) {
		itemType := strings.TrimSpace(item.Get("type").String())
		if !openAIEncryptedLineageItemType(itemType) {
			return
		}
		encrypted := item.Get("encrypted_content")
		if encrypted.Type != gjson.String || encrypted.String() == "" {
			return
		}
		value := encrypted.String()
		kind := openAIWSCandidateCompaction
		if itemType == "reasoning" {
			kind = openAIWSCandidateReasoning
		}
		digest := openAIEncryptedContentDigest(value)
		matched := hint.matches(value)
		if at, seen := indexByDigest[digest]; seen {
			if kind == openAIWSCandidateCompaction {
				candidates[at].kind = openAIWSCandidateCompaction
			}
			candidates[at].matched = candidates[at].matched || matched
			return
		}
		indexByDigest[digest] = len(candidates)
		candidates = append(candidates, openAIWSEncryptedCandidate{digest: digest, kind: kind, matched: matched})
	}
	switch {
	case input.IsArray():
		input.ForEach(func(_, item gjson.Result) bool {
			appendItem(item)
			return true
		})
	case input.IsObject():
		appendItem(input)
	}
	return candidates
}

// 恢复决策的动作与结果取值（也是日志里的 located / result 字段）。
const (
	openAIWSRecoveryActionPrecise = "precise"
	openAIWSRecoveryActionBroad   = "broad"
	openAIWSRecoveryActionNone    = "none"

	openAIWSRecoveryResultRecorded      = "recorded"
	openAIWSRecoveryResultUnrecoverable = "unrecoverable"
	openAIWSRecoveryResultSkipped       = "skipped"
)

// openAIWSEncryptedRecoveryDecision 是不依赖任何状态的纯决策结果。
type openAIWSEncryptedRecoveryDecision struct {
	action  string
	digests []string
	result  string
	reason  string
	matched int
}

// decideOpenAIWSEncryptedRecovery 根据请求里的密文候选和错误片段决定记录什么。规则：
//  1. 已输出过内容：不推断，不记录；
//  2. 没有任何候选：无事可记；
//  3. 候选只有一个（按摘要去重）：它就是被拒项；多个时用错误片段唯一定位，片段必须恰好吻合一个候选；
//  4. 定位到的是压缩项：不记录（不得静默删除可能唯一的早期上下文）；
//  5. 无法唯一定位：请求里没有压缩项时，才允许一次有边界的整包推理项恢复（只含推理项），有压缩项则不做。
//
// 不依据密文长度、外观或模型名做任何判断。
func decideOpenAIWSEncryptedRecovery(candidates []openAIWSEncryptedCandidate, hint openAIWSCiphertextHint, outputCommitted bool) openAIWSEncryptedRecoveryDecision {
	matchedCount := 0
	for _, candidate := range candidates {
		if candidate.matched {
			matchedCount++
		}
	}
	decision := openAIWSEncryptedRecoveryDecision{action: openAIWSRecoveryActionNone, matched: matchedCount}
	if outputCommitted {
		decision.result, decision.reason = openAIWSRecoveryResultUnrecoverable, "output_committed"
		return decision
	}
	if len(candidates) == 0 {
		decision.result, decision.reason = openAIWSRecoveryResultSkipped, "no_candidates"
		return decision
	}

	located := -1
	reason := ""
	switch {
	case len(candidates) == 1:
		located, reason = 0, "only_candidate"
	case hint.form == openAIWSHintNone:
		reason = "no_fragment"
	case matchedCount == 1:
		for index, candidate := range candidates {
			if candidate.matched {
				located, reason = index, "fragment"
			}
		}
	case matchedCount == 0:
		reason = "fragment_no_match"
	default:
		reason = "fragment_ambiguous"
	}

	if located >= 0 {
		if candidates[located].kind != openAIWSCandidateReasoning {
			decision.result, decision.reason = openAIWSRecoveryResultUnrecoverable, "compaction_item"
			return decision
		}
		decision.action = openAIWSRecoveryActionPrecise
		decision.digests = []string{candidates[located].digest}
		decision.result, decision.reason = openAIWSRecoveryResultRecorded, reason
		return decision
	}

	// 无法唯一定位。请求里有压缩项时，被拒的可能正是它，清掉推理项既无益又会损失上下文，直接判不可恢复。
	for _, candidate := range candidates {
		if candidate.kind != openAIWSCandidateReasoning {
			decision.result, decision.reason = openAIWSRecoveryResultUnrecoverable, reason+"_with_compaction"
			return decision
		}
	}
	decision.action = openAIWSRecoveryActionBroad
	decision.digests = make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		decision.digests = append(decision.digests, candidate.digest)
	}
	decision.result, decision.reason = openAIWSRecoveryResultRecorded, reason
	return decision
}

// openAIWSEncryptedRecoveryScopeKey 返回恢复记录的作用域键：会话哈希 + 账号 + 协议 + 入口。
// 上游 lineage 默认只按“分组 + 会话”记账，换号后会把 A 号的判定套到 B 号上；这里把账号与协议编进键里，
// 换号、换协议、换会话都读不到别处的记录，也不会读到 HTTP 桥接与 Forward 路径写的记录。
// 账号或会话缺失时返回空串（无法确定作用域，调用方不记录也不读取）。
func openAIWSEncryptedRecoveryScopeKey(sessionHash string, account *Account) string {
	sessionHash = strings.TrimSpace(sessionHash)
	if sessionHash == "" || account == nil || account.ID <= 0 {
		return ""
	}
	return sessionHash + "|ws-ingress-recovery|account=" + strconv.FormatInt(account.ID, 10) +
		"|" + strings.TrimSpace(account.Platform) + "/" + strings.TrimSpace(account.Type) + "/" + account.GetAPIProtocol()
}

// openAIWSRecoveryRequestID 取客户端请求编号用于日志；没有时返回 "-"。
func openAIWSRecoveryRequestID(c *gin.Context) string {
	if c == nil || c.Request == nil {
		return "-"
	}
	requestID, _ := c.Request.Context().Value(ctxkey.ClientRequestID).(string)
	return truncateOpenAIWSLogValue(requestID, openAIWSIDValueMaxLen)
}

// recordOpenAIWSRejectedEncryptedContent 是上游文件里“收到 invalid_encrypted_content 错误事件”处的钩子。
//
// 规则见 decideOpenAIWSEncryptedRecovery；这里补上状态相关的边界（作用域、恢复次数）并写日志。
// payload 是本次发往上游被拒的帧，rejectMessage 是上游错误文案，outputCommitted 表示本请求已向客户端输出过内容。
// 上游错误事件照旧透传给客户端，本函数只决定“下次请求进场时要不要、清哪几项”。
func (s *OpenAIGatewayService) recordOpenAIWSRejectedEncryptedContent(
	c *gin.Context,
	account *Account,
	groupID int64,
	sessionHash string,
	turn int,
	payload []byte,
	rejectMessage string,
	outputCommitted bool,
) {
	hint := parseOpenAIWSRejectedCiphertextHint(rejectMessage)
	candidates := collectOpenAIWSEncryptedCandidates(payload, hint)
	decision := decideOpenAIWSEncryptedRecovery(candidates, hint, outputCommitted)

	attempts := 0
	scopeKey := openAIWSEncryptedRecoveryScopeKey(sessionHash, account)
	stateStore := s.getOpenAIWSStateStore()
	if decision.action != openAIWSRecoveryActionNone {
		switch {
		case scopeKey == "" || stateStore == nil:
			decision.action, decision.digests = openAIWSRecoveryActionNone, nil
			decision.result, decision.reason = openAIWSRecoveryResultSkipped, "no_scope"
		default:
			budgetKey := scopeKey + openAIWSEncryptedRecoveryBudgetSuffix
			used := stateStore.GetSessionInvalidEncryptedContentDigests(groupID, budgetKey)
			attempts = len(used)
			_, broadUsed := used[openAIWSEncryptedRecoveryBroadToken]
			switch {
			case attempts >= openAIWSEncryptedRecoveryMaxMarks:
				decision.action, decision.digests = openAIWSRecoveryActionNone, nil
				decision.result, decision.reason = openAIWSRecoveryResultUnrecoverable, "budget_exhausted"
			case decision.action == openAIWSRecoveryActionBroad && broadUsed:
				decision.action, decision.digests = openAIWSRecoveryActionNone, nil
				decision.result, decision.reason = openAIWSRecoveryResultUnrecoverable, "broad_exhausted"
			default:
				stateStore.MarkSessionInvalidEncryptedContent(groupID, scopeKey, decision.digests, s.openAIWSSessionStickyTTL())
				tokens := []string{"mark-" + strconv.Itoa(attempts+1)}
				if decision.action == openAIWSRecoveryActionBroad {
					tokens = append(tokens, openAIWSEncryptedRecoveryBroadToken)
				}
				stateStore.MarkSessionInvalidEncryptedContent(groupID, budgetKey, tokens, openAIWSEncryptedRecoveryWindow)
				attempts++
			}
		}
	}

	model := ""
	if len(payload) > 0 {
		model = gjson.GetBytes(payload, "model").String()
	}
	accountID := int64(0)
	if account != nil {
		accountID = account.ID
	}
	logOpenAIWSModeInfo(
		"ingress_ws_encrypted_recovery stage=upstream_reject request_id=%s account_id=%d turn=%d model=%s candidates=%d hint=%s matched=%d marked=%d located=%s result=%s reason=%s attempts=%d output_committed=%v",
		openAIWSRecoveryRequestID(c),
		accountID,
		turn,
		truncateOpenAIWSLogValue(model, openAIWSLogValueMaxLen),
		len(candidates),
		hint.form,
		decision.matched,
		len(decision.digests),
		decision.action,
		decision.result,
		decision.reason,
		attempts,
		outputCommitted,
	)
	if len(decision.digests) > 0 {
		// 保留上游原有的观测日志键，已有的检索与告警不受影响。
		logOpenAIWSModeInfo("ingress_ws_invalid_encrypted_lineage_mark account_id=%d turn=%d digests=%d", accountID, turn, len(decision.digests))
	}
}

// openAIWSIngressRecoverySnapshot 是上游文件里“进场剥离失效密文”处的钩子：对本会话的恢复作用域键取一次摘要快照。
// 返回的快照交给上游的剥离函数做清理，同时写入 frameBody，供随后的出站业务历史校验使用同一份——清理与校验之间
// 缓存即使变化，两者看到的也是同一个集合。没有记录（常态）或作用域缺失时返回 nil，并清空 frameBody 里的旧快照。
func (s *OpenAIGatewayService) openAIWSIngressRecoverySnapshot(
	c *gin.Context,
	frameBody *officialWSFrameBody,
	groupID int64,
	sessionHash string,
	account *Account,
) map[string]struct{} {
	var digests map[string]struct{}
	if scopeKey := openAIWSEncryptedRecoveryScopeKey(sessionHash, account); scopeKey != "" {
		digests = s.sessionInvalidEncryptedContentDigests(groupID, scopeKey)
	}
	frameBody.setInvalidEncryptedRecovery(digests)
	if len(digests) > 0 && account != nil {
		logOpenAIWSModeInfo(
			"ingress_ws_encrypted_recovery stage=entry request_id=%s account_id=%d known_digests=%d",
			openAIWSRecoveryRequestID(c),
			account.ID,
			len(digests),
		)
	}
	return digests
}

// setInvalidEncryptedRecovery 保存本轮进场清理取的失效摘要快照（只读，调用方不得再修改）。nil 接收者安全。
func (b *officialWSFrameBody) setInvalidEncryptedRecovery(digests map[string]struct{}) {
	if b != nil {
		b.invalidEncrypted = digests
	}
}

// invalidEncryptedRecovery 返回本轮的失效摘要快照；没有时返回 nil。nil 接收者安全。
func (b *officialWSFrameBody) invalidEncryptedRecovery() map[string]struct{} {
	if b == nil {
		return nil
	}
	return b.invalidEncrypted
}

// emitOpenAIWSRecoveryValidationFailure 是上游文件里“出站帧校验失败、即将 1008 关连接”处的钩子。
// 仅当本轮做过失效密文恢复清理、且失败原因是“历史输入被修改”时生效：先给客户端写一个明确的错误事件
// （错误码 invalid_encrypted_content 与中文提示，不伪造 response.completed），再由调用方照旧关闭连接，
// 避免客户端只看到“连接被服务端关闭”。其他校验失败（没有恢复清理参与，或与历史比较无关）保持原行为，不写任何事件，
// 免得把无关的失败说成“加密状态无法验证”。
func emitOpenAIWSRecoveryValidationFailure(
	frameBody *officialWSFrameBody,
	writeClientMessage func([]byte) error,
	accountID int64,
	turn int,
	cause error,
) {
	known := len(frameBody.invalidEncryptedRecovery())
	if known == 0 || writeClientMessage == nil || cause == nil ||
		!strings.Contains(cause.Error(), "input was modified") {
		return
	}
	writeErr := writeClientMessage([]byte(`{"type":"error","status":400,"error":` + openAIEncryptedContentErrorJSON + `}`))
	logOpenAIWSModeInfo(
		"ingress_ws_encrypted_recovery stage=validate account_id=%d turn=%d known_digests=%d result=%s reason=validation_failed notice_sent=%v cause=%s",
		accountID,
		turn,
		known,
		openAIWSRecoveryResultUnrecoverable,
		writeErr == nil,
		truncateOpenAIWSLogValue(cause.Error(), openAIWSLogValueMaxLen),
	)
}
