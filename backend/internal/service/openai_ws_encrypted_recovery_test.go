package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 本文件锁定“跨模型旧会话的失效密文恢复”中不依赖真实 WS 链路的部分：错误片段解析、候选收集、恢复决策、
// 状态记录（作用域隔离、恢复次数、边界）以及出站业务历史校验对恢复清理的认可范围（正例、组合、保护负例）。
// 走真实 WS 入站的完整失败链见 openai_ws_encrypted_recovery_ingress_test.go。样本均为脱敏的结构样例。

// recoveryCipher 生成形如真实密文的脱敏样本：统一前缀 + 随标签变化的主体 + 随标签变化的 4 位尾。
// 所有样本前 4 位相同，只有尾部不同，用来验证“首...尾”片段必须首尾同时吻合才算定位。
func recoveryCipher(label string) string {
	sum := sha256.Sum256([]byte(label))
	body := hex.EncodeToString(sum[:])
	return "gAAAAB" + body + body[:24] + strings.ToUpper(body[10:14])
}

// recoveryHintMessage 构造与线上一致的上游错误文案：只带被拒密文的首 4 位与末 4 位。
func recoveryHintMessage(value string) string {
	return "The encrypted content " + value[:4] + "..." + value[len(value)-4:] +
		" could not be verified. Reason: Encrypted content could not be decrypted or parsed."
}

func recoveryReasoningItem(label string) string {
	return `{"type":"reasoning","id":"rs_` + label + `","summary":[{"type":"summary_text","text":"摘要-` + label +
		`"}],"encrypted_content":"` + recoveryCipher(label) + `"}`
}

// recoveryReasoningItemWithContent 是跨模型留下的推理项：带非空 content 数组。
func recoveryReasoningItemWithContent(label string) string {
	return `{"type":"reasoning","id":"rs_` + label + `","summary":[{"type":"summary_text","text":"摘要-` + label +
		`"}],"content":[{"type":"reasoning_text","text":"别家模型的可见推理"}],"encrypted_content":"` + recoveryCipher(label) + `"}`
}

func recoveryCompactionItem(label string) string {
	return `{"type":"compaction","id":"cmp_` + label + `","encrypted_content":"` + recoveryCipher(label) + `"}`
}

// recoverySiblingCipher 构造与 recoveryCipher(label) 首 4 位、末 4 位都相同、中间不同的另一条密文：
// 错误片段“首 4 位...末 4 位”会同时吻合两者，用来造“片段同时吻合多项”的场景。
func recoverySiblingCipher(label string) string {
	value := recoveryCipher(label)
	return value[:10] + "diff" + value[14:]
}

// recoveryReasoningItemWithCipher 是带指定密文的推理项（带非空 content 数组，贴近跨模型旧会话的真实形态）。
func recoveryReasoningItemWithCipher(id, cipher string) string {
	return `{"type":"reasoning","id":"rs_` + id + `","summary":[{"type":"summary_text","text":"摘要-` + id +
		`"}],"content":[{"type":"reasoning_text","text":"别家模型的可见推理"}],"encrypted_content":"` + cipher + `"}`
}

// recoveryCompactionItemWithCipher 是带指定密文的压缩项。
func recoveryCompactionItemWithCipher(id, cipher string) string {
	return `{"type":"compaction","id":"cmp_` + id + `","encrypted_content":"` + cipher + `"}`
}

func recoveryUserMessage(text string) string {
	return `{"type":"message","role":"user","content":[{"type":"input_text","text":"` + text + `"}]}`
}

func recoveryPayload(items ...string) []byte {
	return []byte(`{"type":"response.create","model":"gpt-6.1-sol","stream":true,"store":false,"input":[` +
		strings.Join(items, ",") + `]}`)
}

func recoveryTestService() *OpenAIGatewayService {
	return &OpenAIGatewayService{cfg: &config.Config{}, cache: &stubGatewayCache{}}
}

func recoveryTestAccount(id int64) *Account {
	return &Account{ID: id, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
}

func recoveryTestContext() *gin.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	request := httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	c.Request = request.WithContext(context.WithValue(request.Context(), ctxkey.ClientRequestID, "req-recovery-test"))
	return c
}

func recoveryDigests(values ...string) map[string]struct{} {
	out := make(map[string]struct{}, len(values))
	for _, value := range values {
		out[openAIEncryptedContentDigest(value)] = struct{}{}
	}
	return out
}

// captureOpenAIWSRecoveryLogs 执行 run 并返回期间写出的全部结构化日志文本（消息 + 字段值）。
func captureOpenAIWSRecoveryLogs(t *testing.T, run func()) string {
	t.Helper()
	sink, restore := captureStructuredLog(t)
	defer restore()
	run()
	sink.mu.Lock()
	defer sink.mu.Unlock()
	var out strings.Builder
	for _, event := range sink.events {
		if event == nil {
			continue
		}
		_, _ = out.WriteString(event.Message)
		for key, value := range event.Fields {
			_, _ = out.WriteString(" " + key + "=" + fmt.Sprint(value))
		}
		_, _ = out.WriteString("\n")
	}
	return out.String()
}

// recoveryScopedDigests 读取某账号在某会话下的恢复记录（经作用域键）。
func recoveryScopedDigests(svc *OpenAIGatewayService, groupID int64, sessionHash string, account *Account) map[string]struct{} {
	return svc.getOpenAIWSStateStore().GetSessionInvalidEncryptedContentDigests(groupID, openAIWSEncryptedRecoveryScopeKey(sessionHash, account))
}

// 上游文案的真实形态：首 4 位 + "..." + 末 4 位；也要认完整值、全角省略号、嵌在 JSON 文本里的写法，
// 证据太短或没有片段时判为 none（不作为定位依据）。
func TestParseOpenAIWSRejectedCiphertextHint(t *testing.T) {
	tests := []struct {
		name    string
		message string
		form    string
		head    string
		tail    string
	}{
		{"线上形态", "The encrypted content gAAA...Zx9Q could not be verified. Reason: Encrypted content could not be decrypted or parsed.", openAIWSHintEllipsis, "gAAA", "Zx9Q"},
		{"全角省略号", "The encrypted content gAAA…Zx9Q could not be verified.", openAIWSHintEllipsis, "gAAA", "Zx9Q"},
		{"星号遮盖", "The encrypted content gAAA****Zx9Q could not be verified.", openAIWSHintEllipsis, "gAAA", "Zx9Q"},
		{"完整值", "The encrypted content stale-cipher could not be verified", openAIWSHintExact, "stale-cipher", ""},
		{"嵌在 JSON 文本里", `{"error":{"message":"The encrypted content gAAA...Zx9Q could not be verified."}}`, openAIWSHintEllipsis, "gAAA", "Zx9Q"},
		{"首尾证据太短", "The encrypted content g...Q could not be verified.", openAIWSHintNone, "", ""},
		{"没有片段", "The encrypted content could not be verified", openAIWSHintNone, "", ""},
		{"无关文案", "something else entirely", openAIWSHintNone, "", ""},
		{"空文案", "", openAIWSHintNone, "", ""},
	}
	for _, item := range tests {
		t.Run(item.name, func(t *testing.T) {
			hint := parseOpenAIWSRejectedCiphertextHint(item.message)
			require.Equal(t, item.form, hint.form)
			require.Equal(t, item.head, hint.head)
			require.Equal(t, item.tail, hint.tail)
		})
	}
}

func TestOpenAIWSCiphertextHintMatches(t *testing.T) {
	value := recoveryCipher("a")
	hint := parseOpenAIWSRejectedCiphertextHint(recoveryHintMessage(value))
	require.True(t, hint.matches(value))
	// 首同尾不同、尾同首不同都不算吻合。
	require.False(t, hint.matches(value[:len(value)-4]+"ZZZZ"))
	require.False(t, hint.matches("xxxx"+value[4:]))
	// 长度比首尾之和还短的值不可能是它。
	require.False(t, hint.matches(value[:4]+value[len(value)-3:]))
	exact := parseOpenAIWSRejectedCiphertextHint("The encrypted content stale-cipher could not be verified")
	require.True(t, exact.matches("stale-cipher"))
	require.False(t, exact.matches("stale-cipher2"))
	require.False(t, openAIWSCiphertextHint{form: openAIWSHintNone}.matches(value))
}

func TestCollectOpenAIWSEncryptedCandidates(t *testing.T) {
	bad := recoveryCipher("bad")
	payload := recoveryPayload(
		recoveryUserMessage("你好"),
		recoveryReasoningItem("good1"),
		recoveryReasoningItem("bad"),
		recoveryReasoningItem("bad"), // 同一密文重复出现：按摘要去重
		recoveryCompactionItem("cmp1"),
		`{"type":"message","role":"assistant","encrypted_content":"`+recoveryCipher("alien")+`"}`, // 非 lineage 类型不收
		`{"type":"reasoning","id":"rs_empty","summary":[]}`,                                       // 没有密文不收
		`{"type":"reasoning","id":"rs_blank","summary":[],"encrypted_content":""}`,                // 空密文不收
	)
	hint := parseOpenAIWSRejectedCiphertextHint(recoveryHintMessage(bad))
	candidates := collectOpenAIWSEncryptedCandidates(payload, hint)
	require.Len(t, candidates, 3)
	require.Equal(t, openAIEncryptedContentDigest(recoveryCipher("good1")), candidates[0].digest)
	require.Equal(t, openAIWSCandidateReasoning, candidates[0].kind)
	require.False(t, candidates[0].matched)
	require.Equal(t, openAIEncryptedContentDigest(bad), candidates[1].digest)
	require.True(t, candidates[1].matched)
	require.Equal(t, openAIWSCandidateCompaction, candidates[2].kind)

	// 同一密文既出现在推理项又出现在压缩项里：保守按压缩项处理。
	mixed := recoveryPayload(
		recoveryReasoningItem("same"),
		`{"type":"compaction","encrypted_content":"`+recoveryCipher("same")+`"}`,
	)
	mixedCandidates := collectOpenAIWSEncryptedCandidates(mixed, openAIWSCiphertextHint{form: openAIWSHintNone})
	require.Len(t, mixedCandidates, 1)
	require.Equal(t, openAIWSCandidateCompaction, mixedCandidates[0].kind)

	// input 为单个对象也能收；没有 input 返回空。
	single := collectOpenAIWSEncryptedCandidates([]byte(`{"input":`+recoveryReasoningItem("solo")+`}`), openAIWSCiphertextHint{form: openAIWSHintNone})
	require.Len(t, single, 1)
	require.Nil(t, collectOpenAIWSEncryptedCandidates([]byte(`{"model":"gpt-5"}`), openAIWSCiphertextHint{form: openAIWSHintNone}))
	require.Nil(t, collectOpenAIWSEncryptedCandidates(nil, openAIWSCiphertextHint{form: openAIWSHintNone}))
}

// 恢复决策表：只能由“唯一候选”或“恰好吻合一个候选的错误片段”定位；定位不到时才允许一次整包类恢复
// （没有片段／片段对不上：全部推理候选；片段同时吻合多项：只记吻合的那几项），
// 压缩项、已输出内容、没有候选一律不记录。
func TestDecideOpenAIWSEncryptedRecovery(t *testing.T) {
	good1, good2, bad, cmp := recoveryCipher("good1"), recoveryCipher("good2"), recoveryCipher("bad"), recoveryCipher("cmp1")
	// twin 与 twinSibling 首 4 位、末 4 位相同、中间不同：错误片段“首...尾”会同时吻合两者。
	// 片段同时吻合多项时，嫌疑范围只取吻合的那几项（被拒的一定在其中），不吻合的有效项不记、不动。
	twin, twinSibling := recoveryCipher("twin"), recoverySiblingCipher("twin")
	reasoningPayload := recoveryPayload(recoveryReasoningItem("good1"), recoveryReasoningItem("bad"), recoveryReasoningItem("good2"))
	withCompaction := recoveryPayload(recoveryReasoningItem("good1"), recoveryReasoningItem("bad"), recoveryCompactionItem("cmp1"))
	decide := func(payload []byte, message string, outputCommitted bool) openAIWSEncryptedRecoveryDecision {
		hint := parseOpenAIWSRejectedCiphertextHint(message)
		return decideOpenAIWSEncryptedRecovery(collectOpenAIWSEncryptedCandidates(payload, hint), hint, outputCommitted)
	}

	t.Run("片段唯一吻合只记那一项，有效项不被标坏", func(t *testing.T) {
		decision := decide(reasoningPayload, recoveryHintMessage(bad), false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, []string{openAIEncryptedContentDigest(bad)}, decision.digests)
		require.Equal(t, "fragment", decision.reason)
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(good1))
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(good2))
	})
	t.Run("只有一个候选不需要片段", func(t *testing.T) {
		decision := decide(recoveryPayload(recoveryReasoningItem("bad")), "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, "only_candidate", decision.reason)
	})
	t.Run("片段无吻合项：整包推理项恢复（有边界）", func(t *testing.T) {
		decision := decide(reasoningPayload, recoveryHintMessage(recoveryCipher("other")), false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action)
		require.Equal(t, "fragment_no_match", decision.reason)
		require.ElementsMatch(t, []string{openAIEncryptedContentDigest(good1), openAIEncryptedContentDigest(bad), openAIEncryptedContentDigest(good2)}, decision.digests)
	})
	t.Run("没有片段：整包推理项恢复", func(t *testing.T) {
		decision := decide(reasoningPayload, "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action)
		require.Equal(t, "no_fragment", decision.reason)
	})
	t.Run("片段吻合多个候选（首尾相同）：不挑一个猜，吻合的几项都记", func(t *testing.T) {
		payload := recoveryPayload(
			`{"type":"reasoning","id":"rs_t1","summary":[],"encrypted_content":"`+twin+`"}`,
			`{"type":"reasoning","id":"rs_t2","summary":[],"encrypted_content":"`+twinSibling+`"}`,
		)
		decision := decide(payload, recoveryHintMessage(twin), false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action)
		require.Equal(t, "fragment_ambiguous", decision.reason)
		require.Len(t, decision.digests, 2)
	})
	t.Run("片段同时吻合 3 个推理候选里的 2 个：只记那 2 个，仍是整包类动作", func(t *testing.T) {
		payload := recoveryPayload(
			recoveryReasoningItemWithCipher("t1", twin),
			recoveryReasoningItem("good1"),
			recoveryReasoningItemWithCipher("t2", twinSibling),
		)
		decision := decide(payload, recoveryHintMessage(twin), false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action, "仍记为整包类，受每窗口一次的整包预算约束")
		require.Equal(t, openAIWSRecoveryResultRecorded, decision.result)
		require.Equal(t, "fragment_ambiguous", decision.reason)
		require.Equal(t, 2, decision.matched)
		require.ElementsMatch(t, []string{openAIEncryptedContentDigest(twin), openAIEncryptedContentDigest(twinSibling)}, decision.digests)
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(good1), "片段对不上的有效推理项不记")
	})
	t.Run("吻合的几项里有压缩项：不可恢复，不记录", func(t *testing.T) {
		payload := recoveryPayload(
			recoveryReasoningItemWithCipher("t1", twin),
			recoveryReasoningItem("good1"),
			recoveryCompactionItemWithCipher("t2", twinSibling),
		)
		decision := decide(payload, recoveryHintMessage(twin), false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, openAIWSRecoveryResultUnrecoverable, decision.result)
		require.Equal(t, "fragment_ambiguous_with_compaction", decision.reason)
		require.Equal(t, 2, decision.matched)
		require.Empty(t, decision.digests)
	})
	t.Run("吻合的几项都是推理项、另有不吻合的压缩项：记吻合的推理项，压缩项不在嫌疑内", func(t *testing.T) {
		payload := recoveryPayload(
			recoveryReasoningItemWithCipher("t1", twin),
			recoveryCompactionItem("cmp1"),
			recoveryReasoningItemWithCipher("t2", twinSibling),
			recoveryReasoningItem("good1"),
		)
		decision := decide(payload, recoveryHintMessage(twin), false)
		require.Equal(t, openAIWSRecoveryActionBroad, decision.action)
		require.Equal(t, openAIWSRecoveryResultRecorded, decision.result)
		require.Equal(t, "fragment_ambiguous", decision.reason)
		require.ElementsMatch(t, []string{openAIEncryptedContentDigest(twin), openAIEncryptedContentDigest(twinSibling)}, decision.digests)
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(cmp), "不吻合的压缩项不记，也不影响判断")
		require.NotContains(t, decision.digests, openAIEncryptedContentDigest(good1))
	})
	t.Run("只有一个候选：不看片段直接定位，片段对不上也照记（only_candidate）", func(t *testing.T) {
		// 候选集合含请求里所有带非空密文的历史项，只有一个候选时上游拒的只能是它；片段对不上只说明文案格式与解析不一致。
		decision := decide(recoveryPayload(recoveryReasoningItem("bad")), recoveryHintMessage(recoveryCipher("other")), false)
		require.Equal(t, openAIWSRecoveryActionPrecise, decision.action)
		require.Equal(t, openAIWSRecoveryResultRecorded, decision.result)
		require.Equal(t, "only_candidate", decision.reason)
		require.Equal(t, 0, decision.matched, "片段确实一个都没对上")
		require.Equal(t, []string{openAIEncryptedContentDigest(bad)}, decision.digests)
	})
	t.Run("定位到压缩项：不可恢复，不记录", func(t *testing.T) {
		decision := decide(withCompaction, recoveryHintMessage(cmp), false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, openAIWSRecoveryResultUnrecoverable, decision.result)
		require.Equal(t, "compaction_item", decision.reason)
		require.Empty(t, decision.digests)
	})
	t.Run("只有一个候选且是压缩项：不可恢复", func(t *testing.T) {
		decision := decide(recoveryPayload(recoveryCompactionItem("cmp1")), "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "compaction_item", decision.reason)
	})
	t.Run("无法定位且请求里有压缩项：不做整包恢复", func(t *testing.T) {
		decision := decide(withCompaction, "The encrypted content could not be verified", false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, openAIWSRecoveryResultUnrecoverable, decision.result)
		require.Equal(t, "no_fragment_with_compaction", decision.reason)
		require.Empty(t, decision.digests)
	})
	t.Run("已输出过内容：不推断，不记录", func(t *testing.T) {
		decision := decide(reasoningPayload, recoveryHintMessage(bad), true)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, "output_committed", decision.reason)
		require.Empty(t, decision.digests)
	})
	t.Run("没有任何候选", func(t *testing.T) {
		decision := decide(recoveryPayload(recoveryUserMessage("hi")), recoveryHintMessage(bad), false)
		require.Equal(t, openAIWSRecoveryActionNone, decision.action)
		require.Equal(t, openAIWSRecoveryResultSkipped, decision.result)
	})
}

// 记录：只记定位到的那一项，挂在“账号 + 协议 + 会话”作用域键下；换账号、换会话、换分组、原始会话键都读不到。
func TestRecordOpenAIWSRejectedEncryptedContentScopeIsolation(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	accountA, accountB := recoveryTestAccount(90), recoveryTestAccount(91)
	bad := recoveryCipher("bad")
	payload := recoveryPayload(recoveryReasoningItem("good1"), recoveryReasoningItem("bad"), recoveryReasoningItem("good2"), recoveryReasoningItem("good3"))

	svc.recordOpenAIWSRejectedEncryptedContent(c, accountA, 8, "session-1", 1, payload, recoveryHintMessage(bad), false)

	require.Equal(t, recoveryDigests(bad), recoveryScopedDigests(svc, 8, "session-1", accountA), "只记被拒的那一项")
	require.Nil(t, recoveryScopedDigests(svc, 8, "session-1", accountB), "换账号读不到")
	require.Nil(t, recoveryScopedDigests(svc, 8, "session-2", accountA), "换会话读不到")
	require.Nil(t, recoveryScopedDigests(svc, 9, "session-1", accountA), "换分组读不到")
	require.Nil(t, svc.getOpenAIWSStateStore().GetSessionInvalidEncryptedContentDigests(8, "session-1"), "不写上游原有的“分组 + 会话”键")
	// 协议维度：同一账号编号但平台/类型不同，作用域键不同。
	other := &Account{ID: 90, Platform: PlatformOpenAI, Type: AccountTypeAPIKey}
	require.NotEqual(t, openAIWSEncryptedRecoveryScopeKey("session-1", accountA), openAIWSEncryptedRecoveryScopeKey("session-1", other))
	// 作用域缺失：无会话、无账号、账号编号为零都不返回键。
	require.Empty(t, openAIWSEncryptedRecoveryScopeKey("", accountA))
	require.Empty(t, openAIWSEncryptedRecoveryScopeKey("session-1", nil))
	require.Empty(t, openAIWSEncryptedRecoveryScopeKey("session-1", &Account{Platform: PlatformOpenAI}))

	// 进场快照同样只读自己的作用域键。
	body := &officialWSFrameBody{}
	require.Equal(t, recoveryDigests(bad), svc.openAIWSIngressRecoverySnapshot(c, body, 8, "session-1", accountA, nil))
	require.Equal(t, recoveryDigests(bad), body.invalidEncryptedRecovery())
	require.Nil(t, svc.openAIWSIngressRecoverySnapshot(c, body, 8, "session-1", accountB, nil))
	require.Nil(t, body.invalidEncryptedRecovery(), "取不到记录时清空旧快照")
}

// 快照语义：取一次后，之后缓存再变化，已取的快照不变（清理与校验用同一份）；frameBody 清空时快照一并清空。
func TestOpenAIWSIngressRecoverySnapshotIsStableAcrossStoreChanges(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	first, second := recoveryCipher("first"), recoveryCipher("second")
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, recoveryPayload(recoveryReasoningItem("first")), "", false)

	body := &officialWSFrameBody{}
	snapshot := svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, nil)
	require.Equal(t, recoveryDigests(first), snapshot)

	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 2, recoveryPayload(recoveryReasoningItem("second")), "", false)
	require.Equal(t, recoveryDigests(first, second), recoveryScopedDigests(svc, 3, "s", account), "缓存已变化")
	require.Equal(t, recoveryDigests(first), body.invalidEncryptedRecovery(), "已取的快照不随缓存变化")

	body.clear()
	require.Nil(t, body.invalidEncryptedRecovery())
	// nil 接收者安全。
	var nilBody *officialWSFrameBody
	nilBody.setInvalidEncryptedRecovery(snapshot)
	require.Nil(t, nilBody.invalidEncryptedRecovery())
}

// 恢复次数限制：同一作用域键窗口内最多记录 openAIWSEncryptedRecoveryMaxMarks 次，之后不再记录；整包恢复只允许一次。
func TestRecordOpenAIWSRejectedEncryptedContentBudget(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)

	// 精确定位：逐个坏项各记一次，到上限后不再记录。
	for index := 0; index < openAIWSEncryptedRecoveryMaxMarks+2; index++ {
		label := "bad" + strconv.Itoa(index)
		payload := recoveryPayload(recoveryReasoningItem("keep"), recoveryReasoningItem(label))
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", index+1, payload, recoveryHintMessage(recoveryCipher(label)), false)
	}
	recorded := recoveryScopedDigests(svc, 3, "s", account)
	require.Len(t, recorded, openAIWSEncryptedRecoveryMaxMarks, "超过次数限制后不再记录")
	require.Contains(t, recorded, openAIEncryptedContentDigest(recoveryCipher("bad0")))
	require.NotContains(t, recorded, openAIEncryptedContentDigest(recoveryCipher("bad"+strconv.Itoa(openAIWSEncryptedRecoveryMaxMarks))))
	require.NotContains(t, recorded, openAIEncryptedContentDigest(recoveryCipher("keep")), "有效项从未被标坏")

	// 另一个账号的预算互不影响。
	other := recoveryTestAccount(91)
	svc.recordOpenAIWSRejectedEncryptedContent(c, other, 3, "s", 1, recoveryPayload(recoveryReasoningItem("x1"), recoveryReasoningItem("x2")), recoveryHintMessage(recoveryCipher("x2")), false)
	require.Equal(t, recoveryDigests(recoveryCipher("x2")), recoveryScopedDigests(svc, 3, "s", other))

	// 整包恢复只允许一次：第一次记录全部推理项，第二次不再记录。
	broadAccount := recoveryTestAccount(92)
	payload := recoveryPayload(recoveryReasoningItem("p1"), recoveryReasoningItem("p2"), recoveryReasoningItem("p3"))
	svc.recordOpenAIWSRejectedEncryptedContent(c, broadAccount, 3, "s", 1, payload, "The encrypted content could not be verified", false)
	require.Len(t, recoveryScopedDigests(svc, 3, "s", broadAccount), 3)
	svc.recordOpenAIWSRejectedEncryptedContent(c, broadAccount, 3, "s", 2, recoveryPayload(recoveryReasoningItem("q1"), recoveryReasoningItem("q2")), "The encrypted content could not be verified", false)
	require.Len(t, recoveryScopedDigests(svc, 3, "s", broadAccount), 3, "第二次整包恢复被拒绝")
	// 整包恢复用掉后，仍可对唯一定位的新坏项做精确记录（受总次数限制）。
	svc.recordOpenAIWSRejectedEncryptedContent(c, broadAccount, 3, "s", 3, recoveryPayload(recoveryReasoningItem("r1"), recoveryReasoningItem("r2")), recoveryHintMessage(recoveryCipher("r2")), false)
	require.Contains(t, recoveryScopedDigests(svc, 3, "s", broadAccount), openAIEncryptedContentDigest(recoveryCipher("r2")))
}

// 边界：压缩项、已输出内容、作用域缺失，一律不记录（也不动任何已有记录）。
func TestRecordOpenAIWSRejectedEncryptedContentBoundaries(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	bad, cmp := recoveryCipher("bad"), recoveryCipher("cmp1")
	payload := recoveryPayload(recoveryReasoningItem("good"), recoveryReasoningItem("bad"), recoveryCompactionItem("cmp1"))

	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s-compaction", 1, payload, recoveryHintMessage(cmp), false)
	require.Nil(t, recoveryScopedDigests(svc, 3, "s-compaction", account), "被拒的是压缩项：不记录，不静默删除")

	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s-no-fragment", 1, payload, "The encrypted content could not be verified", false)
	require.Nil(t, recoveryScopedDigests(svc, 3, "s-no-fragment", account), "无法定位且有压缩项：不做整包恢复")

	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s-output", 1, payload, recoveryHintMessage(bad), true)
	require.Nil(t, recoveryScopedDigests(svc, 3, "s-output", account), "已向客户端输出过内容：不记录")

	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "", 1, payload, recoveryHintMessage(bad), false)
	svc.recordOpenAIWSRejectedEncryptedContent(c, nil, 3, "s-nil-account", 1, payload, recoveryHintMessage(bad), false)
	svc.recordOpenAIWSRejectedEncryptedContent(c, &Account{Platform: PlatformOpenAI}, 3, "s-zero-account", 1, payload, recoveryHintMessage(bad), false)
	require.False(t, svc.getOpenAIWSStateStore().HasAnySessionInvalidEncryptedContent(), "作用域缺失时没有任何记录写入")

	// 边界之后，正常的精确定位依然可以记录。
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s-ok", 1, payload, recoveryHintMessage(bad), false)
	require.Equal(t, recoveryDigests(bad), recoveryScopedDigests(svc, 3, "s-ok", account))
}

// 片段同时吻合多项：记录、进场清理、出站校验三者用同一份摘要快照，只动吻合的那几项。
//   - (a) 3 个推理候选里 2 个吻合：只记 2 个，未吻合项的密文在进场清理后原样保留，出站校验通过；
//   - (c) 吻合项全是推理项、另有一个不吻合的压缩项：记吻合的推理项，压缩项原样保留，出站校验通过。
//
// 两种情形都占用整包预算：同一作用域键再来一次无法定位的拒绝，不再记录。
func TestRecordOpenAIWSRejectedEncryptedContentAmbiguousFragmentOnlyMarksMatched(t *testing.T) {
	twin, twinSibling := recoveryCipher("twin"), recoverySiblingCipher("twin")
	cases := []struct {
		name string
		// kept 是不吻合、必须原样保留的项的密文。
		kept     []string
		keptItem []string
	}{
		{
			name:     "(a) 3 个推理候选里 2 个吻合",
			kept:     []string{recoveryCipher("good1")},
			keptItem: []string{recoveryReasoningItemWithContent("good1")},
		},
		{
			name:     "(c) 吻合项全是推理项、另有不吻合的压缩项",
			kept:     []string{recoveryCipher("good1"), recoveryCipher("cmp1")},
			keptItem: []string{recoveryReasoningItemWithContent("good1"), recoveryCompactionItem("cmp1")},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc := recoveryTestService()
			c := recoveryTestContext()
			account := recoveryTestAccount(90)
			items := []string{
				recoveryUserMessage("第一问"),
				recoveryReasoningItemWithCipher("t1", twin),
			}
			items = append(items, tc.keptItem...)
			items = append(items, recoveryReasoningItemWithCipher("t2", twinSibling), recoveryUserMessage("第二问"))
			original := recoveryPayload(items...)

			svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, original, recoveryHintMessage(twin), false)
			recorded := recoveryScopedDigests(svc, 3, "s", account)
			require.Equal(t, recoveryDigests(twin, twinSibling), recorded, "只记片段吻合的两项")
			for _, cipher := range tc.kept {
				require.NotContains(t, recorded, openAIEncryptedContentDigest(cipher), "不吻合的项不被标坏")
			}

			// 下一次进场：清理与校验用同一份快照。吻合的两项密文被清掉，不吻合的项原样保留，校验放行。
			body := &officialWSFrameBody{}
			snapshot := svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, nil)
			require.Equal(t, recoveryDigests(twin, twinSibling), snapshot)
			candidate := recoveryPipeline(t, original, snapshot)
			require.NotContains(t, string(candidate), twin)
			require.NotContains(t, string(candidate), twinSibling)
			for _, cipher := range tc.kept {
				require.Contains(t, string(candidate), cipher, "不吻合项的密文在进场清理后原样保留")
			}
			require.True(t, recoveryEqual(t, original, candidate, body.invalidEncryptedRecovery()), "出站校验通过")
			require.False(t, recoveryEqual(t, original, candidate, nil), "没有恢复依据时同一候选仍判被修改，说明放行确实来自快照")

			// 占用了整包预算：同一作用域键再来一次没有片段的拒绝，不再记录，也不扩大已有记录。
			svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 2,
				recoveryPayload(recoveryReasoningItem("q1"), recoveryReasoningItem("q2")), "The encrypted content could not be verified", false)
			require.Equal(t, recoveryDigests(twin, twinSibling), recoveryScopedDigests(svc, 3, "s", account), "不另开预算")
		})
	}
}

// (b) 吻合项中有压缩项：判不可恢复，什么都不记；之后重发同一份历史，帧原样（不做任何清理）通过校验。
func TestRecordOpenAIWSRejectedEncryptedContentAmbiguousFragmentWithMatchedCompactionIsUnrecoverable(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	twin, twinSibling := recoveryCipher("twin"), recoverySiblingCipher("twin")
	original := recoveryPayload(
		recoveryUserMessage("第一问"),
		recoveryReasoningItemWithCipher("t1", twin),
		recoveryReasoningItem("good1"),
		recoveryCompactionItemWithCipher("t2", twinSibling),
		recoveryUserMessage("第二问"),
	)

	logs := captureOpenAIWSRecoveryLogs(t, func() {
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, original, recoveryHintMessage(twin), false)
	})
	require.False(t, svc.getOpenAIWSStateStore().HasAnySessionInvalidEncryptedContent(), "吻合项里有压缩项：不记录任何摘要，也不动预算键")
	require.Nil(t, recoveryScopedDigests(svc, 3, "s", account))
	require.Contains(t, logs, "result=unrecoverable")
	require.Contains(t, logs, "reason=fragment_ambiguous_with_compaction")
	require.Contains(t, logs, "marked=0")
	require.Contains(t, logs, "located=none")

	// 没有记录就没有清理：进场快照为空，帧原样，校验照旧。
	body := &officialWSFrameBody{}
	require.Nil(t, svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, nil))
	candidate := recoveryPipeline(t, original, body.invalidEncryptedRecovery())
	for _, cipher := range []string{twin, twinSibling, recoveryCipher("good1")} {
		require.Contains(t, string(candidate), cipher)
	}

	// 不可恢复不占用整包预算：同一作用域键之后遇到“没有片段、只有推理项”的拒绝，仍可做一次整包恢复。
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 2,
		recoveryPayload(recoveryReasoningItem("q1"), recoveryReasoningItem("q2")), "The encrypted content could not be verified", false)
	require.Len(t, recoveryScopedDigests(svc, 3, "s", account), 2)
}

// 只有一个候选且片段对不上：仍按 only_candidate 精确记录。候选集合含请求里所有带非空密文的历史项，
// 只有一个候选时上游拒的只能是它；片段对不上只说明错误文案格式与解析不一致，不能据此否定定位。
func TestRecordOpenAIWSRejectedEncryptedContentSingleCandidateIgnoresMismatchedFragment(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	bad := recoveryCipher("bad")
	// 历史里除了这一条推理项，没有任何其它带密文的项（消息、工具调用不算候选）。
	original := recoveryPayload(recoveryUserMessage("第一问"), recoveryReasoningItemWithContent("bad"), recoveryUserMessage("第二问"))

	logs := captureOpenAIWSRecoveryLogs(t, func() {
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, original, recoveryHintMessage(recoveryCipher("other")), false)
	})
	require.Equal(t, recoveryDigests(bad), recoveryScopedDigests(svc, 3, "s", account), "片段对不上，仍精确记录唯一候选")
	for _, want := range []string{"hint=ellipsis", "candidates=1", "matched=0", "marked=1", "located=precise", "result=recorded", "reason=only_candidate"} {
		require.Contains(t, logs, want)
	}

	// 记录之后进场清理与校验照常一致。
	body := &officialWSFrameBody{}
	snapshot := svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, nil)
	candidate := recoveryPipeline(t, original, snapshot)
	require.NotContains(t, string(candidate), bad)
	require.True(t, recoveryEqual(t, original, candidate, body.invalidEncryptedRecovery()))

	// 唯一候选是压缩项时，即便片段对不上也不记录（压缩项不得静默删除）。
	svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s-cmp", 1,
		recoveryPayload(recoveryCompactionItem("cmp1")), recoveryHintMessage(recoveryCipher("other")), false)
	require.Nil(t, recoveryScopedDigests(svc, 3, "s-cmp", account))
}

// 日志不得出现密文、片段或会话正文。
func TestRecordOpenAIWSRejectedEncryptedContentLogsNoSecrets(t *testing.T) {
	svc := recoveryTestService()
	c := recoveryTestContext()
	account := recoveryTestAccount(90)
	bad := recoveryCipher("bad")
	payload := recoveryPayload(recoveryUserMessage("绝密会话正文"), recoveryReasoningItem("good"), recoveryReasoningItem("bad"))

	logs := captureOpenAIWSRecoveryLogs(t, func() {
		svc.recordOpenAIWSRejectedEncryptedContent(c, account, 3, "s", 1, payload, recoveryHintMessage(bad), false)
		body := &officialWSFrameBody{}
		svc.openAIWSIngressRecoverySnapshot(c, body, 3, "s", account, nil)
		emitOpenAIWSRecoveryValidationFailure(body, func([]byte) error { return nil }, account.ID, 2, errors.New("OpenAI official egress WebSocket input was modified"))
	})
	require.Contains(t, logs, "stage=upstream_reject")
	// 取值来自 ctx 里的 ClientRequestID，日志键必须叫 client_request_id；上游文件的日志键叫 request_id，两者不是一回事。
	require.Contains(t, logs, "client_request_id=req-recovery-test")
	require.Equal(t, 2, strings.Count(logs, "client_request_id=req-recovery-test"), "upstream_reject 与 entry 两条日志都带")
	require.NotContains(t, strings.ReplaceAll(logs, "client_request_id=", ""), "request_id=", "不再出现裸的 request_id 键")
	require.Contains(t, logs, "account_id=90")
	require.Contains(t, logs, "model=gpt-6.1-sol")
	require.Contains(t, logs, "matched=1")
	require.Contains(t, logs, "marked=1")
	require.Contains(t, logs, "result=recorded")
	require.Contains(t, logs, "stage=entry")
	require.Contains(t, logs, "stage=validate")
	for _, secret := range []string{bad, bad[:4] + "..." + bad[len(bad)-4:], "绝密会话正文", "摘要-bad", recoveryCipher("good")} {
		require.NotContains(t, logs, secret)
	}
}

// 校验失败钩子：恢复清理参与时给客户端写明确的错误事件（不是 response.completed）；没有恢复参与时不写。
func TestEmitOpenAIWSRecoveryValidationFailure(t *testing.T) {
	var written [][]byte
	write := func(message []byte) error {
		written = append(written, append([]byte(nil), message...))
		return nil
	}
	modified := errors.New("OpenAI official egress WebSocket input was modified")
	emitOpenAIWSRecoveryValidationFailure(&officialWSFrameBody{}, write, 90, 1, modified)
	emitOpenAIWSRecoveryValidationFailure(nil, write, 90, 1, modified)
	require.Empty(t, written, "没有恢复清理参与：保持原行为，不写事件")

	body := &officialWSFrameBody{}
	body.setInvalidEncryptedRecovery(recoveryDigests("a"))
	emitOpenAIWSRecoveryValidationFailure(body, write, 90, 1, errors.New("OpenAI official egress WebSocket previous_response_id was modified"))
	emitOpenAIWSRecoveryValidationFailure(body, write, 90, 1, nil)
	require.Empty(t, written, "与历史比较无关的失败：不说成加密状态无法验证")
	emitOpenAIWSRecoveryValidationFailure(body, write, 90, 1, modified)
	require.Len(t, written, 1)
	require.True(t, json.Valid(written[0]))
	require.Equal(t, "error", gjson.GetBytes(written[0], "type").String())
	require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(written[0], "error.code").String())
	require.Contains(t, gjson.GetBytes(written[0], "error.message").String(), "无法安全恢复")
	require.NotContains(t, string(written[0]), "response.completed")
	// 写失败不影响调用方继续关闭连接。
	emitOpenAIWSRecoveryValidationFailure(body, func([]byte) error { return errors.New("closed") }, 90, 1, modified)
}

// ---------------------------------------------------------------------------
// 出站业务历史校验对恢复清理的认可范围
// ---------------------------------------------------------------------------

// recoveryHistory 是旧会话恢复出的完整历史：A 是别家模型留下的失效推理项（带 content 数组），B 是有效的 GPT 推理项（也带 content），
// C 是加密压缩项，另有消息与配对的工具调用。
func recoveryHistory(extra ...string) []byte {
	items := []string{
		recoveryUserMessage("第一问"),
		recoveryReasoningItemWithContent("A"),
		`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"第一答"}]}`,
		`{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup","arguments":"{\"q\":\"one\"}"}`,
		`{"type":"function_call_output","call_id":"call_1","output":"ok"}`,
		recoveryReasoningItemWithContent("B"),
		recoveryCompactionItem("C"),
	}
	items = append(items, extra...)
	items = append(items, recoveryUserMessage("第二问"))
	return recoveryPayload(items...)
}

// recoveryPipeline 对客户端原帧走与线上相同的顺序：先入站兼容层，再进场清理（剥离失效密文）。
func recoveryPipeline(t *testing.T, original []byte, invalid map[string]struct{}) []byte {
	t.Helper()
	compat, _, err := normalizeOpenAIResponsesWebSocketCompatibilityBody(original, reasoningReplayOAuthAccount(), false)
	require.NoError(t, err)
	stripped, _, err := stripOpenAIInvalidEncryptedContentRaw(compat, invalid)
	require.NoError(t, err)
	return stripped
}

func recoveryDecode(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	decoded, err := decodeOfficialJSONObjectUseNumber(raw)
	require.NoError(t, err)
	return decoded
}

func recoveryEqual(t *testing.T, original, candidate []byte, recovery map[string]struct{}) bool {
	t.Helper()
	equal, err := equalOfficialOpenAIWSBusinessHistoryWithRecovery(recoveryDecode(t, original), recoveryDecode(t, candidate), recovery)
	require.NoError(t, err)
	return equal
}

// 正例与组合：同时存在 reasoning.content 清理和失效密文清理时，按线上顺序产出的候选放行；
// 没有恢复依据（空快照）时同一个候选仍被拒绝——这正是线上 1008 的成因。
func TestOfficialWSBusinessHistoryAcceptsRecoveryCleanup(t *testing.T) {
	original := recoveryHistory()
	invalid := recoveryDigests(recoveryCipher("A"))
	candidate := recoveryPipeline(t, original, invalid)

	// 管线确实做了两件事：A 的 content 与密文都没了，其余项（含有效项 B 的密文与 content 之外的字段、压缩项 C）原样保留。
	require.False(t, gjson.GetBytes(candidate, "input.1.content").Exists())
	require.False(t, gjson.GetBytes(candidate, "input.1.encrypted_content").Exists())
	require.Equal(t, "摘要-A", gjson.GetBytes(candidate, "input.1.summary.0.text").String())
	require.Equal(t, recoveryCipher("B"), gjson.GetBytes(candidate, "input.5.encrypted_content").String(), "有效项不被误清")
	require.False(t, gjson.GetBytes(candidate, "input.5.content").Exists(), "有效项只做第一轮的 content 清理")
	require.Equal(t, recoveryCipher("C"), gjson.GetBytes(candidate, "input.6.encrypted_content").String(), "压缩项原样保留")

	require.True(t, recoveryEqual(t, original, candidate, invalid), "组合清理放行")
	require.False(t, recoveryEqual(t, original, candidate, nil), "没有恢复依据：仍判被修改")
	require.False(t, recoveryEqual(t, original, candidate, recoveryDigests(recoveryCipher("B"))), "快照没有命中 A：A 的密文被删仍拒绝")

	// 旧入口（不带快照）与带空快照的行为一致。
	equal, err := equalOfficialOpenAIWSBusinessHistory(recoveryDecode(t, original), recoveryDecode(t, candidate))
	require.NoError(t, err)
	require.False(t, equal)
}

// 候选还没来得及清理（清理失败或未命中）时也放行：差异只允许“可能发生的清理”，不要求一定发生。
func TestOfficialWSBusinessHistoryAcceptsUncleanedCandidateWithRecovery(t *testing.T) {
	original := recoveryHistory()
	invalid := recoveryDigests(recoveryCipher("A"))
	compatOnly, _, err := normalizeOpenAIResponsesWebSocketCompatibilityBody(original, reasoningReplayOAuthAccount(), false)
	require.NoError(t, err)
	require.True(t, recoveryEqual(t, original, compatOnly, invalid), "只做了第一轮 content 清理")
	require.True(t, recoveryEqual(t, original, original, invalid), "原样")
	// 只清密文、未做 content 清理（入站没有做兼容清理时进场清理直接面对原始项）。
	stripOnly, _, err := stripOpenAIInvalidEncryptedContentRaw(original, invalid)
	require.NoError(t, err)
	require.True(t, recoveryEqual(t, original, stripOnly, invalid))
}

// 保护负例：恢复清理之外的任何变化都拒绝——消息正文、角色、摘要、工具参数、配对 ID、续链关系、未命中摘要的密文、
// 压缩项、项的增删与顺序。
func TestOfficialWSBusinessHistoryRejectsChangesBeyondRecovery(t *testing.T) {
	original := recoveryHistory()
	invalid := recoveryDigests(recoveryCipher("A"))
	good := recoveryPipeline(t, original, invalid)
	require.True(t, recoveryEqual(t, original, good, invalid), "前提：未改动的候选放行")

	mutations := []struct {
		name   string
		mutate func(t *testing.T, candidate []byte) []byte
	}{
		{"消息正文", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.0.content.0.text", "被改写的第一问")
		}},
		{"消息角色", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.0.role", "assistant") }},
		{"助手消息正文", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.2.content.0.text", "被改写的第一答")
		}},
		{"命中项的推理摘要", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.1.summary.0.text", "被改写的摘要")
		}},
		{"命中项的 id", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.1.id", "rs_other") }},
		{"命中项被写回别的密文", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.1.encrypted_content", recoveryCipher("forged"))
		}},
		{"命中项被新增 content", func(t *testing.T, c []byte) []byte {
			return mustSetRaw(t, c, "input.1.content", `[{"type":"reasoning_text","text":"新增"}]`)
		}},
		{"工具参数", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.3.arguments", `{"q":"two"}`) }},
		{"工具调用 call_id", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.3.call_id", "call_2") }},
		{"工具输出 call_id（孤立输出）", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.4.call_id", "call_9") }},
		{"工具输出内容", func(t *testing.T, c []byte) []byte { return mustSet(t, c, "input.4.output", "tampered") }},
		{"未命中摘要的有效项被清掉密文", func(t *testing.T, c []byte) []byte { return mustDelete(t, c, "input.5.encrypted_content") }},
		{"未命中摘要的有效项被改写密文", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.5.encrypted_content", recoveryCipher("forged"))
		}},
		{"压缩项被改写", func(t *testing.T, c []byte) []byte {
			return mustSet(t, c, "input.6.encrypted_content", recoveryCipher("forged"))
		}},
		{"压缩项被删除", func(t *testing.T, c []byte) []byte { return mustDelete(t, c, "input.6") }},
		{"删除一条助手消息", func(t *testing.T, c []byte) []byte { return mustDelete(t, c, "input.2") }},
		{"多出一项", func(t *testing.T, c []byte) []byte {
			return mustSetRaw(t, c, "input.-1", recoveryUserMessage("多出来的"))
		}},
		{"交换两项顺序", func(t *testing.T, c []byte) []byte {
			first, second := gjson.GetBytes(c, "input.0").Raw, gjson.GetBytes(c, "input.2").Raw
			c = mustSetRaw(t, c, "input.0", second)
			return mustSetRaw(t, c, "input.2", first)
		}},
	}
	for _, item := range mutations {
		t.Run(item.name, func(t *testing.T) {
			candidate := item.mutate(t, append([]byte(nil), good...))
			require.False(t, recoveryEqual(t, original, candidate, invalid))
		})
	}

	// 续链关系：previous_response_id 不属于历史比较，由外层校验拒绝；此处确认带恢复快照时外层仍拒绝被改写的续链。
	withPrevious := mustSet(t, original, "previous_response_id", "resp_prev")
	candidatePrevious := mustSet(t, good, "previous_response_id", "resp_other")
	_, _, _, err := decodeAndValidateUnifiedOpenAIWSBusinessContract(withPrevious, candidatePrevious, "", false, func() *officialWSFrameBody {
		body := &officialWSFrameBody{}
		body.setInvalidEncryptedRecovery(invalid)
		return body
	}())
	require.ErrorContains(t, err, "previous_response_id was modified")
}

// 快照里即使带了压缩项的摘要，候选删掉压缩项也不放行：压缩项不在校验认可范围内。
func TestOfficialWSBusinessHistoryNeverAcceptsCompactionRemoval(t *testing.T) {
	original := recoveryHistory()
	invalid := recoveryDigests(recoveryCipher("A"), recoveryCipher("C"))
	// 上游剥离函数会整项删除命中摘要的压缩项；校验必须把这视为被修改。
	candidate := recoveryPipeline(t, original, invalid)
	require.Len(t, gjson.GetBytes(candidate, "input").Array(), len(gjson.GetBytes(original, "input").Array())-1, "前提：剥离函数确实删了压缩项")
	require.False(t, recoveryEqual(t, original, candidate, invalid))
}

// 整项删除：清掉密文后只剩 type 的空骨架会被剥离函数整项删除，校验认可“原项消失”，但只认命中的那一项。
func TestOfficialWSBusinessHistoryAcceptsDroppedSkeleton(t *testing.T) {
	skeleton := func(label string) string {
		return `{"type":"reasoning","encrypted_content":"` + recoveryCipher(label) + `"}`
	}
	original := recoveryPayload(recoveryUserMessage("问"), skeleton("S1"), recoveryUserMessage("答"), skeleton("S2"))
	invalid := recoveryDigests(recoveryCipher("S1"))
	candidate := recoveryPipeline(t, original, invalid)
	require.Len(t, gjson.GetBytes(candidate, "input").Array(), 3, "前提：命中的空骨架被整项删除")
	require.True(t, recoveryEqual(t, original, candidate, invalid))

	// 删掉没命中的那个空骨架 S2：拒绝。
	wrong := mustDelete(t, candidate, "input.2")
	require.False(t, recoveryEqual(t, original, wrong, invalid))
	// 命中的空骨架末尾被删（候选先于原始耗尽）也要认可；末尾多出一项则拒绝。
	tail := recoveryPayload(recoveryUserMessage("问"), skeleton("S1"))
	require.True(t, recoveryEqual(t, tail, recoveryPayload(recoveryUserMessage("问")), invalid))
	require.False(t, recoveryEqual(t, recoveryPayload(recoveryUserMessage("问")), tail, invalid))
}

// 校验比较只读：不改动原始与候选的对象树。
func TestOfficialWSBusinessHistoryWithRecoveryDoesNotMutateInputs(t *testing.T) {
	original := recoveryDecode(t, recoveryHistory())
	invalid := recoveryDigests(recoveryCipher("A"))
	candidate := recoveryDecode(t, recoveryPipeline(t, recoveryHistory(), invalid))
	originalBefore, _ := json.Marshal(original)
	candidateBefore, _ := json.Marshal(candidate)
	invalidBefore := len(invalid)
	equal, err := equalOfficialOpenAIWSBusinessHistoryWithRecovery(original, candidate, invalid)
	require.NoError(t, err)
	require.True(t, equal)
	originalAfter, _ := json.Marshal(original)
	candidateAfter, _ := json.Marshal(candidate)
	require.Equal(t, originalBefore, originalAfter)
	require.Equal(t, candidateBefore, candidateAfter)
	require.Len(t, invalid, invalidBefore)
}

func mustSet(t *testing.T, body []byte, path string, value any) []byte {
	t.Helper()
	next, err := sjson.SetBytes(body, path, value)
	require.NoError(t, err)
	return next
}

func mustSetRaw(t *testing.T, body []byte, path string, raw string) []byte {
	t.Helper()
	next, err := sjson.SetRawBytes(body, path, []byte(raw))
	require.NoError(t, err)
	return next
}

func mustDelete(t *testing.T, body []byte, path string) []byte {
	t.Helper()
	next, err := sjson.DeleteBytes(body, path)
	require.NoError(t, err)
	return next
}
