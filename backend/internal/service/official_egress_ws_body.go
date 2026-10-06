package service

import (
	"context"
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
func equalOfficialOpenAIWSBusinessHistory(original, candidate map[string]any) (bool, error) {
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
		if leftOK != rightOK {
			return false, nil
		}
		if !leftOK {
			return true, nil
		}
		if !reflect.DeepEqual(lv, rv) {
			return false, nil
		}
		li, ri = nextLeft, nextRight
	}
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
