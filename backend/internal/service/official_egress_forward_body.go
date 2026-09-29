package service

import (
	"context"
	"unsafe"

	"github.com/gin-gonic/gin"
)

// 本文件实现官方出站 HTTP 转发主干上的“正文工作区”（问题四 M2）。
//
// Forward 以 []byte 表示请求正文：契约捕获、对象树解码、保序重编码、compaction 规整与
// Finalizer 各自拿到的是同一版本（或下一版本）的正文，却各自扫描一遍建立正文索引。16.8 MiB
// 官方 Lite 形态实测，一次转发要建 7 次索引，每次累计分配约 1.3 倍正文。工作区为当前正文
// 版本缓存一份索引，同一版本的所有使用方共享（M2-c）。
//
// 工作区只在官方出站 HTTP 路径上创建（Forward 中 officialOpenAIHTTPEnabled 为真时），经 ctx 传给
// 同一次调用的 Finalizer；其余路径拿到的是 nil。所有方法对 nil 接收者原样调用改造前的函数，
// 非官方出站路径（API Key、透传、兼容转换、WS 及 WS→HTTP 回落等）代码路径与行为不变。
//
// 正文按只读约束使用（与 openai_json_rawview.go 的零拷贝视图相同）：缓存以“同一底层数组起点、
// 同一长度”识别正文版本，正文一经产出不再原地改写，因此同一切片必然是同一内容。
type officialForwardHTTPBody struct {
	// indexBody 与 index：当前正文版本的索引缓存。只保留一份，正文换版本即替换，旧正文随之
	// 不再被工作区引用。
	indexBody []byte
	index     *officialJSONRawIndex
}

type officialForwardHTTPBodyContextKey struct{}

// officialForwardHTTPBodyDisabledContextKey 只供差分测试关闭工作区，得到改造前的代码路径。
type officialForwardHTTPBodyDisabledContextKey struct{}

// newOfficialForwardHTTPBody 为一次官方出站 HTTP 转发创建工作区，并挂到返回的 ctx 上，供同一次
// 调用的 Finalizer 取用。ctx 标记了关闭（仅测试）时返回 nil 工作区与原 ctx。
func newOfficialForwardHTTPBody(ctx context.Context) (context.Context, *officialForwardHTTPBody) {
	if ctx == nil {
		ctx = context.Background()
	}
	if disabled, _ := ctx.Value(officialForwardHTTPBodyDisabledContextKey{}).(bool); disabled {
		return ctx, nil
	}
	body := &officialForwardHTTPBody{}
	return context.WithValue(ctx, officialForwardHTTPBodyContextKey{}, body), body
}

// withOfficialForwardHTTPBodyDisabled 只供差分测试使用：返回的 ctx 让 Forward 不创建工作区。
func withOfficialForwardHTTPBodyDisabled(ctx context.Context) context.Context {
	return context.WithValue(ctx, officialForwardHTTPBodyDisabledContextKey{}, true)
}

// officialForwardHTTPBodyFromContext 取回本次调用的工作区；不在官方出站 HTTP 路径上时返回 nil。
func officialForwardHTTPBodyFromContext(ctx context.Context) *officialForwardHTTPBody {
	if ctx == nil {
		return nil
	}
	body, _ := ctx.Value(officialForwardHTTPBodyContextKey{}).(*officialForwardHTTPBody)
	return body
}

// officialForwardSameBody 判断两段正文是否为同一版本：同一底层数组起点且长度相同。
func officialForwardSameBody(left, right []byte) bool {
	return len(left) == len(right) &&
		unsafe.Pointer(unsafe.SliceData(left)) == unsafe.Pointer(unsafe.SliceData(right))
}

// indexFor 返回 body 的正文索引（只登记节点；需要拼接编码时由使用方调用 ensureDigests 补齐）。
// 与缓存的是同一版本时直接复用，否则扫描一遍并替换缓存。正文为空或非法时返回 nil 且清空缓存，
// 使用方随即按改造前的逻辑处理（得到与过去相同的结果或错误）。nil 工作区始终返回 nil。
func (b *officialForwardHTTPBody) indexFor(body []byte) *officialJSONRawIndex {
	if b == nil {
		return nil
	}
	if b.index != nil && officialForwardSameBody(b.indexBody, body) {
		return b.index
	}
	b.index, b.indexBody = nil, nil
	index, err := buildOfficialJSONRawIndexForDecode(body)
	if err != nil {
		return nil
	}
	b.index, b.indexBody = index, body
	return index
}

// captureContract 等价于 captureOfficialOpenAIHTTPBodyContractForRequest，复用 body 的索引。
func (b *officialForwardHTTPBody) captureContract(c *gin.Context, body []byte) (*officialOpenAIHTTPBodyContract, error) {
	return captureOfficialOpenAIHTTPBodyContractForRequestWithIndex(c, body, b.indexFor(body))
}

// decodeRequestView 等价于 view.Decode(c)（getOpenAIRequestBodyMap），复用视图正文的索引。
func (b *officialForwardHTTPBody) decodeRequestView(c *gin.Context, view openAIRequestView) (map[string]any, error) {
	if b == nil {
		return view.Decode(c)
	}
	if index := b.indexFor(view.body); index != nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		return index.decodeObject(index.root), nil
	}
	return getOpenAIRequestBodyMap(c, view.body)
}

// reencode 等价于 marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)，复用 body 的索引。
func (b *officialForwardHTTPBody) reencode(payload map[string]any, body []byte) ([]byte, error) {
	if b == nil {
		return marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)
	}
	index := b.indexFor(body)
	index.ensureDigests()
	return marshalOfficialJSONObjectPreservingOrderAndRawWithIndex(payload, body, index)
}

// normalizeCompactionTriggerInputOrder 等价于 NormalizeCompactionTriggerInputOrder，复用 body 的索引。
func (b *officialForwardHTTPBody) normalizeCompactionTriggerInputOrder(body []byte) ([]byte, bool, error) {
	if b == nil {
		return NormalizeCompactionTriggerInputOrder(body)
	}
	return normalizeCompactionTriggerInputOrderWithIndex(body, b.indexFor(body))
}
