package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// officialWSFrameBody 只服务原生 WS 的当前轮，复用最近一版只读正文的索引。
// 对象树每次独立创建，长字符串借用源正文，业务改写不会污染其他阶段。只保留
// 一版索引，避免缓存所有中间正文；轮末和退出时清空，先于入站系统内存的释放。
type officialWSFrameBody struct {
	index *officialJSONRawIndex
	// invalidEncrypted 是本轮进场清理时取的“会话失效密文摘要”快照（只读）。清理与校验用同一份快照，
	// 避免两者之间缓存变化造成“清理按新集合、校验按旧集合”的错位；每轮进场重新赋值，轮末随 clear 清空。
	// 读写方法见 openai_ws_encrypted_recovery.go。
	invalidEncrypted map[string]struct{}
}

type officialWSFrameBodyContextKey struct{}

func newOfficialWSFrameBody(ctx context.Context) (context.Context, *officialWSFrameBody) {
	body := &officialWSFrameBody{}
	return context.WithValue(ctx, officialWSFrameBodyContextKey{}, body), body
}

func officialWSFrameBodyFromContext(ctx context.Context) *officialWSFrameBody {
	if ctx == nil {
		return nil
	}
	body, _ := ctx.Value(officialWSFrameBodyContextKey{}).(*officialWSFrameBody)
	return body
}

func (b *officialWSFrameBody) clear() {
	if b != nil {
		b.index = nil
		b.invalidEncrypted = nil
	}
}

func (b *officialWSFrameBody) decode(source []byte) (map[string]any, *officialJSONRawIndex, error) {
	var index *officialJSONRawIndex
	if b != nil && b.index != nil && officialForwardSameBody(source, b.index.body) {
		index = b.index
	} else {
		index, _ = buildOfficialJSONRawIndexForDecode(source)
	}
	if index == nil || index.nodes[index.root].kind != officialJSONRawKindObject {
		payload, err := decodeOfficialJSONObjectUseNumberSlow(source)
		return payload, nil, err
	}
	if b != nil {
		b.index = index
	}
	// 根节点已经验证为对象，解码器在此分支只会返回 map。
	payload, _ := index.decodeValueSharingBody(index.root).(map[string]any)
	return payload, index, nil
}

func decodeOfficialWSFrameBody(ctx context.Context, source []byte) (map[string]any, *officialJSONRawIndex, error) {
	return officialWSFrameBodyFromContext(ctx).decode(source)
}

// marshalOfficialWSFrameBody 复用同一版本的扫描结果；只有拼接编码需要时才补摘要。
func marshalOfficialWSFrameBody(mode string, payload map[string]any, source []byte, index *officialJSONRawIndex) ([]byte, error) {
	order, err := officialCodexBodyFieldOrderForMode(mode, officialCodexEndpointResponsesWS)
	if err != nil {
		return nil, err
	}
	index = officialJSONIndexWithAdditionalToolsKeyOrder(
		index, source, officialCodexOrdersAdditionalToolsKeys(mode, officialCodexEndpointResponsesWS),
	)
	if index != nil {
		index.ensureDigests()
	}
	members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, order, source, index)
	if err != nil {
		return nil, err
	}
	// 逐项 metadata 会使输出比原帧更大；按成员精确计算长度，避免先分配整帧后
	// 因少量新增字段再次扩容。片段仍由原有保序编码器产生，线序与字节不变。
	return officialegress.AppendJSONObjectMembers(make([]byte, 0, officialegress.JSONObjectMembersLength(members)), members), nil
}

// equalOfficialOpenAIWSBusinessHistory 逐项比较旧规范化历史的逻辑值。输入来自
// UseNumber 解码，只有 JSON 类型；数字文本、空容器和键值语义与旧 json.Marshal
// 后比较一致。一次仅持有两项的小字段副本，不生成两份完整的历史 JSON。
//
// 不带失效密文恢复依据的比较（只认入站兼容层的推理历史清理）。
func equalOfficialOpenAIWSBusinessHistory(original, candidate map[string]any) (bool, error) {
	return equalOfficialOpenAIWSBusinessHistoryWithRecovery(original, candidate, nil)
}

// equalOfficialOpenAIWSBusinessHistoryWithRecovery 在上面的逐项比较之上，额外认可“服务端已确认的失效密文恢复”。
//
// recovery 是本轮进场清理时取的失效摘要快照（与清理函数用同一份）。差异只允许两类：
//  1. 候选项恰为原始推理项经入站兼容层 content 清理后的结果（第一轮修复）；
//  2. 原始推理项的密文摘要命中 recovery 时，候选项恰为“对原始项副本套用进场清理用的同一个上游剥离函数
//     (stripOpenAIInvalidEncryptedContentItems) 后”的结果，且可与第 1 类清理组合，或该项被整项删除。
//
// 其余任何差异（正文、角色、摘要、工具参数、配对 ID、续链关系、未命中摘要的密文变化，以及 compaction／compaction_summary
// 项被删除）一律判为不等。recovery 为空时与旧行为逐项相同，不产生任何额外开销。
func equalOfficialOpenAIWSBusinessHistoryWithRecovery(original, candidate map[string]any, recovery map[string]struct{}) (bool, error) {
	left, err := officialOpenAIWSHistoryInput(original)
	if err != nil {
		return false, err
	}
	right, err := officialOpenAIWSHistoryInput(candidate)
	if err != nil {
		return false, err
	}
	for li, ri := 0, 0; ; {
		lv, nextLeft, leftOK := nextOfficialOpenAIWSHistoryItem(left, li)
		rv, nextRight, rightOK := nextOfficialOpenAIWSHistoryItem(right, ri)
		if !leftOK && !rightOK {
			return true, nil
		}
		if leftOK && rightOK &&
			(reflect.DeepEqual(lv, rv) ||
				equalOfficialOpenAIWSHistoryItemAfterReasoningReplay(lv, rv) ||
				equalOfficialOpenAIWSHistoryItemAfterRecovery(lv, rv, recovery)) {
			li, ri = nextLeft, nextRight
			continue
		}
		// 原始项在恢复清理后会被整项删除（清掉密文后只剩 type 的空骨架）：候选里已没有它，只推进原始侧游标。
		if leftOK && officialOpenAIWSHistoryItemDroppedByRecovery(lv, recovery) {
			li = nextLeft
			continue
		}
		return false, nil
	}
}

// equalOfficialOpenAIWSHistoryItemAfterReasoningReplay 判断候选项是否恰为“原始项经入站兼容层的推理历史清理后”的结果。
//
// 入站兼容层（normalizeOpenAIResponsesReasoningContentReplay）会删掉 reasoning 项里非空数组形式的 content，
// 这是跨模型旧会话（先用别家模型、后切 GPT）历史必需的受控清理。校验器不另写一套条件，而是把原始项逐项副本包成
// 最小请求体，直接调用同一个清理函数；清理有效时再与候选项比较，其余字段（summary、encrypted_content、id 等）
// 必须与清理结果完全一致，所以除这一种差异外不会额外放行任何改动。清理函数不认的形态（空数组、字符串、null、
// 非 reasoning 项等）原样返回，此处判为不等。
//
// 只在逐项比较已经不等时才调用，相等的项（绝大多数）不产生任何额外开销；只编码单项副本，不为长历史生成整段 JSON，
// 也不改动原始项与候选项。任何编码、清理或解码失败都按“不等”处理（保守拒绝）。
func equalOfficialOpenAIWSHistoryItemAfterReasoningReplay(original, candidate any) bool {
	item, ok := original.(map[string]any)
	if !ok {
		return false
	}
	cleaned, ok := officialOpenAIWSReasoningReplayCleaned(item)
	return ok && reflect.DeepEqual(cleaned, candidate)
}

// officialOpenAIWSReasoningReplayCleaned 把原始项的逐项副本包成最小请求体，交给入站兼容层的同一个清理函数；
// 清理有效（确有 content 数组被删）时返回清理后的项副本，否则返回 false。不改动传入的项。任何编码、清理或解码失败都按“无效”处理。
func officialOpenAIWSReasoningReplayCleaned(item map[string]any) (map[string]any, bool) {
	body, err := json.Marshal(map[string]any{"input": []any{item}})
	if err != nil {
		return nil, false
	}
	cleaned, changed, err := normalizeOpenAIResponsesReasoningContentReplay(body)
	if err != nil || !changed {
		return nil, false
	}
	payload, err := decodeOfficialJSONObjectUseNumber(cleaned)
	if err != nil {
		return nil, false
	}
	items, _ := payload["input"].([]any)
	if len(items) != 1 {
		return nil, false
	}
	next, ok := items[0].(map[string]any)
	return next, ok
}

// officialOpenAIWSRecoveryOutcomes 计算“命中失效摘要的推理项”在服务端恢复清理后可能的形态。
//
// 清理规则一份都不另写：直接对原始项的副本调用上游 lineage 的剥离函数 stripOpenAIInvalidEncryptedContentItems
// （进场清理用的就是它，摘要命中判定与 sanitizeEncryptedReasoningInputItem 的剥离语义都在里面），
// 并与第一轮的 content 清理按两种先后顺序组合，得到候选可以出现的全部形态 kept；某种顺序下该项会被整项删除
// （清掉密文后只剩 type 的空骨架）时 dropped 为 true。
//
// 只认 reasoning 项：compaction／compaction_summary 可能是早期上下文的唯一副本，没有可信的替代历史时不得静默删除，
// 所以即使摘要命中也不在这里放行，候选里它们一旦消失就会被校验拒绝。
// 摘要未命中、密文不是字符串、类型不对，一律返回空，不放行任何差异。不改动传入的项。
func officialOpenAIWSRecoveryOutcomes(original any, recovery map[string]struct{}) (kept []any, dropped bool) {
	if len(recovery) == 0 {
		return nil, false
	}
	item, ok := original.(map[string]any)
	if !ok || strings.TrimSpace(officialOpenAIString(item, "type")) != "reasoning" {
		return nil, false
	}
	if encrypted, ok := item["encrypted_content"].(string); !ok || encrypted == "" {
		return nil, false
	}
	apply := func(source map[string]any) {
		wrapper := map[string]any{"input": []any{cloneOfficialOpenAIMap(source)}}
		if stripOpenAIInvalidEncryptedContentItems(wrapper, recovery) == 0 {
			return
		}
		remaining, _ := wrapper["input"].([]any)
		if len(remaining) == 0 {
			dropped = true
			return
		}
		kept = append(kept, remaining[0])
	}
	// 顺序一：只清密文（入站没有做 content 清理时，进场清理直接面对原始项）。
	apply(item)
	// 顺序二：先经入站兼容层清掉 content 数组，进场清理再清密文（跨模型旧会话的真实顺序）。
	if cleaned, ok := officialOpenAIWSReasoningReplayCleaned(item); ok {
		apply(cleaned)
	}
	return kept, dropped
}

// equalOfficialOpenAIWSHistoryItemAfterRecovery 判断候选项是否恰为原始推理项经失效密文恢复清理后的某一种形态。
func equalOfficialOpenAIWSHistoryItemAfterRecovery(original, candidate any, recovery map[string]struct{}) bool {
	kept, _ := officialOpenAIWSRecoveryOutcomes(original, recovery)
	for _, outcome := range kept {
		if reflect.DeepEqual(outcome, candidate) {
			return true
		}
	}
	return false
}

// officialOpenAIWSHistoryItemDroppedByRecovery 判断原始项在恢复清理后是否会被整项删除（候选里应当不再出现它）。
func officialOpenAIWSHistoryItemDroppedByRecovery(original any, recovery map[string]struct{}) bool {
	_, dropped := officialOpenAIWSRecoveryOutcomes(original, recovery)
	return dropped
}

func officialOpenAIWSHistoryInput(payload map[string]any) ([]any, error) {
	switch input := payload["input"].(type) {
	case nil:
		return nil, nil
	case string:
		return []any{map[string]any{"type": "message", "role": "user", "content": input}}, nil
	case []any:
		return input, nil
	default:
		return nil, fmt.Errorf("normalize OpenAI official egress WebSocket business history: %w",
			errors.New("OpenAI official egress input must be a string or array"))
	}
}

func nextOfficialOpenAIWSHistoryItem(input []any, position int) (any, int, bool) {
	for position < len(input) {
		raw := input[position]
		position++
		item, ok := raw.(map[string]any)
		if !ok {
			return raw, position, true
		}
		if officialOpenAIString(item, "type") == officialOpenAIAdditionalToolsType {
			continue
		}
		cloned := cloneOfficialOpenAIMap(item)
		if officialOpenAIString(cloned, "type") == "" && officialOpenAIString(cloned, "role") != "" {
			cloned["type"] = "message"
		}
		delete(cloned, officialOpenAIWSItemTurnMetadata)
		itemType := officialOpenAIString(cloned, "type")
		if id, ok := cloned["id"].(string); ok && shouldStripOpenAIResponsesInputItemID(itemType, id) {
			delete(cloned, "id")
		}
		if shouldStripOpenAIResponsesNonPairCallID(itemType) {
			delete(cloned, "call_id")
		}
		if strings.TrimSpace(itemType) == "message" {
			cloned["content"] = officialOpenAIHTTPMessageContentText(cloned["content"])
		}
		return cloned, position, true
	}
	return nil, position, false
}
