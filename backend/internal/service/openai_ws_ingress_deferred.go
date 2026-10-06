package service

import "context"

// openAIWSDeferredIngressBody 只用于普通官方 OAuth 的 WS→HTTP bridge。
// headers 保留旧正文的全部顶层字段与格式，但将大 input 值暂时换为 []；身份隔离、
// Lite 默认值和 fast policy 仍调用原函数，只处理这份小正文。原 input 始终只读。
// 需要完整正文的 native、历史重写和错误重试路径按原位置拼回 input，恢复旧字节。
type openAIWSDeferredIngressBody struct {
	source  []byte
	input   []byte
	headers []byte
	index   *officialJSONRawIndex
}

type openAIWSDeferredIngressBodyContextKey struct{}

func newOpenAIWSDeferredIngressBody(body []byte, lite bool) *openAIWSDeferredIngressBody {
	return newOpenAIWSDeferredIngressBodyWithIndex(body, lite, nil)
}

func newOpenAIWSDeferredIngressBodyWithIndex(body []byte, lite bool, index *officialJSONRawIndex) *openAIWSDeferredIngressBody {
	input := openAIBodyGet(body, "input")
	if len(input.Raw) < officialForwardDetachLocateMinBytes ||
		len(body)-len(input.Raw) > officialForwardDetachLocateMinBytes {
		return nil
	}
	fields, handled, _ := normalizeOpenAIResponsesLiteHeaderFields(body)
	if !handled {
		return nil
	}
	var err error
	if index == nil || !officialForwardSameBody(body, index.body) {
		index, err = buildOfficialJSONRawIndexForDecode(body)
	}
	if err != nil || !openAIWSHTTPBridgeCanKeepRawBody(index) {
		return nil
	}
	if lite {
		// 旧 Lite 拼接器会在整棵树中寻找相等对象。若新 reasoning 能命中 input
		// 内的对象，其原始空白也会被保留；小正文无法复现这种布局，故完整回退。
		reasoning := index.memberNode(index.root, "reasoning")
		if value, present := fields["reasoning"]; present && (reasoning < 0 || !index.equals(reasoning, value)) {
			index.ensureDigests()
			if index.lookupComposite(value) >= 0 {
				return nil
			}
		}
	}
	inputNode := index.memberNode(index.root, "input")
	if inputNode < 0 {
		return nil
	}
	node := index.nodes[inputNode]
	headers := make([]byte, 0, len(body)-(node.end-node.start)+2)
	headers = append(headers, body[:node.start]...)
	headers = append(headers, '[', ']')
	headers = append(headers, body[node.end:]...)
	return &openAIWSDeferredIngressBody{source: body, input: body[node.start:node.end], headers: headers, index: index}
}

func (b *openAIWSDeferredIngressBody) payloadBytes() int {
	if b == nil {
		return 0
	}
	return len(b.headers) - 2 + len(b.input)
}

func (b *openAIWSDeferredIngressBody) materialize() []byte {
	input := openAIBodyGet(b.headers, "input")
	body := make([]byte, 0, b.payloadBytes())
	body = append(body, b.headers[:input.Index]...)
	body = append(body, b.input...)
	body = append(body, b.headers[input.Index+len(input.Raw):]...)
	return body
}

// applyHeaders 在 bridge 已有对象树中恢复小字段。删除同样要传播，不能让原文中
// 被 fast policy 等移除的字段在 Finalizer 前重新出现。
func (b *openAIWSDeferredIngressBody) applyHeaders(payload map[string]any) error {
	headers, err := decodeOfficialJSONObjectUseNumber(b.headers)
	if err != nil {
		return err
	}
	for key := range payload {
		if key != "input" {
			delete(payload, key)
		}
	}
	for key, value := range headers {
		if key != "input" {
			payload[key] = value
		}
	}
	return nil
}

func openAIWSDeferredIngressBodyFromContext(ctx context.Context, body []byte) *openAIWSDeferredIngressBody {
	if ctx == nil {
		return nil
	}
	deferred, _ := ctx.Value(openAIWSDeferredIngressBodyContextKey{}).(*openAIWSDeferredIngressBody)
	if deferred == nil || !officialForwardSameBody(deferred.source, body) {
		return nil
	}
	return deferred
}

// preparedHeaderIndex 使用旧 prepare 的规范化结果作小字段编码基准。
// reasoning.context 在入口补齐后应与原有键一起排序，不能在 Finalizer 才追加到末尾。
func (b *openAIWSDeferredIngressBody) preparedHeaderIndex(account *Account) (*officialJSONRawIndex, error) {
	body, err := prepareOpenAIWSHTTPBridgeBody(account, b.headers)
	if err != nil {
		return nil, err
	}
	index, err := buildOfficialJSONRawIndex(body)
	return index, err
}
