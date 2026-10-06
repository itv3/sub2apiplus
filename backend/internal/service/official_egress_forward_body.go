package service

import (
	"context"
	"encoding/json"
	"sort"
	"strings"
	"unsafe"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

// 本文件实现官方出站 HTTP 转发主干上的“正文工作区”（问题四 M2）。
//
// Forward 以 []byte 表示请求正文：契约捕获、对象树解码、保序重编码、compaction 规整与
// Finalizer 各自拿到的是同一版本（或下一版本）的正文，却各自扫描一遍建立正文索引。16.8 MiB
// 官方 Lite 形态实测，一次转发要建 7 次索引，每次累计分配约 1.3 倍正文。工作区为当前正文
// 版本缓存一份索引，同一版本的所有使用方共享（M2-c）。
//
// 转发主干还曾整程持有两份与正文等大的对象（M2-a）：Codex 转换等业务改写用的对象树（整段解码、
// 复制全部字符串，约 1.2 倍正文），以及改写后整体重编码出的新正文（1 倍）。二者一直存活到上游
// 请求编译、压缩、发送完毕，正好叠在内存峰值上。工作区为此做四件事：
//   - 对象树改在正文索引上直建，无转义的长字符串以只读视图引用正文，只剩结构与短字符串；
//   - 重编码按顶层成员产出：未改动的值引用原正文区间，其中与调用方原始正文逐字节相同的大成员
//     （input 等）改为直接引用原始正文（调用方在整个请求期间本就持有它），其余引用中间版本正文的
//     成员值复制成独立小段，从而不再让 Lite 归一化、字段补丁等产生的中间正文存活；整段新正文照常
//     物化给 Forward 的其余读取方，并记下每个成员值在其中的区间与来源；
//   - Finalizer 按成员定型时，把引用这份新正文的成员值换成对应来源，请求体因此不再引用它；
//   - 上游 attempt（编译、签名、zstd 压缩与发送）期间，Forward 暂时放下这份新正文、请求视图、lineage
//     基准与对象树，attempt 返回后再按成员物化出逐字节相同的正文放回原处，供错误处理与重试使用。
//
// 工作区在官方出站 HTTP Forward 和 WS→HTTP 单轮桥接路径上创建，经 ctx 传给同一次调用的
// Finalizer。桥接先保留准备好的逻辑树，再按规范化片段输出，避免 prepare 对整段正文重编码。
// 其余路径拿到 nil；所有方法对 nil 接收者原样调用改造前的函数或不做任何事。
//
// 正文按只读约束使用（与 openai_json_rawview.go 的零拷贝视图相同）：以“同一底层数组起点、同一
// 长度”识别正文版本，正文一经产出不再原地改写，因此同一切片必然是同一内容；对象树与成员里引用
// 正文的值只会被比较或重新编码进新正文，不会保存到请求之外。
type officialForwardHTTPBody struct {
	// releaseStorage 只关闭本次完整转发的出站正文文件，不改变业务或上游 context。
	releaseStorage func()
	// bridge 仅用于官方 WS→HTTP 单轮桥接：准备阶段保留逻辑正文，Finalizer 才按规范化片段写出。
	bridge *openAIWSHTTPBridgeBody
	// wsIngress 是 WS 入口已校验但尚未拼接的大 input 与小字段正文。
	wsIngress *openAIWSDeferredIngressBody
	// ingress 是调用方传入 Forward 的原始正文，整个请求期间由调用方持有。
	ingress []byte

	// liteDefaults 是已在入口完成验证的小字段。普通 Lite 请求的 reasoning/parallel 默认值
	// 延迟到对象树与 Finalizer 应用，避免为了补一个字段先重建整份历史正文。
	liteDefaults bool
	// deferredFields 只包含允许延迟的少数顶层小字段；input 或其他业务字段改变时不使用此路径。
	// value 为 nil 表示删除，非 nil 是已经按旧规则编码的完整 JSON 值。
	deferredBody   []byte
	deferredFields map[string]json.RawMessage
	// finalizerPayload 只在 Forward 与 Finalizer 之间移交一次；定型取得后立即清空，重试仍从正文恢复。
	finalizerBody    []byte
	finalizerPayload map[string]any

	// indexBody 与 index：当前正文版本的索引缓存。只保留一份，正文换版本即替换。scans 记录实际
	// 扫描正文建立索引的次数，只用于观测（测试据此验证同一版本正文只扫描一次）。
	indexBody []byte
	index     *officialJSONRawIndex
	scans     int

	// 最近一次 reencode 的结果：顶层成员（值引用原始正文、小段副本或新编码字节）、由成员物化出
	// 的整段正文，以及各成员值在整段正文中的区间与来源。
	members []officialegress.JSONObjectMember
	rebuilt []byte
	spans   []officialForwardBodySpan

	// park 到 restore 之间的暂存状态：Forward 局部变量的地址、请求视图的其余字段，以及哪些变量被放下。
	parked        bool
	parkedBody    *[]byte
	parkedView    *openAIRequestView
	viewState     openAIRequestView
	viewParked    bool
	parkedLineage *[]byte
	lineageParked bool
}

// officialForwardBodySpan 记录物化正文中一个成员值的区间 [start, end) 及其来源切片；来源与该区间
// 逐字节相同。
type officialForwardBodySpan struct {
	start  int
	end    int
	source []byte
}

// officialForwardDetachLocateMinBytes 以上的成员值才去调用方原始正文里查找逐字节相同的同名成员；
// 更小的成员值直接复制成独立小段。查找需要遍历一次原始正文的顶层成员，只对大成员值得。
const officialForwardDetachLocateMinBytes = 64 << 10

type officialForwardHTTPBodyContextKey struct{}

// officialForwardHTTPBodyDisabledContextKey 只供差分测试关闭工作区，得到改造前的代码路径。
type officialForwardHTTPBodyDisabledContextKey struct{}

// newOfficialForwardHTTPBody 为一次官方出站 HTTP 转发创建工作区，并挂到返回的 ctx 上，供同一次
// 调用的 Finalizer 取用。ingress 必须是调用方传入 Forward 的原始正文。ctx 标记了关闭（仅测试）时
// 返回 nil 工作区与原 ctx。
func newOfficialForwardHTTPBody(ctx context.Context, ingress []byte) (context.Context, *officialForwardHTTPBody) {
	if ctx == nil {
		ctx = context.Background()
	}
	if disabled, _ := ctx.Value(officialForwardHTTPBodyDisabledContextKey{}).(bool); disabled {
		return ctx, nil
	}
	// 压缩输出最多占入口原文的四分之三，另受进程总额度限制；按实际分配的完整
	// 系统内存块计量。超额时保留普通磁盘回退，不扩大线上准入范围。
	ctx, release := officialegress.WithRequestBodyStorageMemoryLimit(ctx, int64(len(ingress))*3/4, allocateOfficialRequestBodyMemory)
	body := &officialForwardHTTPBody{ingress: ingress, releaseStorage: release}
	return context.WithValue(ctx, officialForwardHTTPBodyContextKey{}, body), body
}

// 无闭包的组合层适配器只连接内存接口，不让存储资源反向保活业务工作区。
func allocateOfficialRequestBodyMemory(size int) (officialegress.RequestBodyMemoryBuffer, error) {
	return pkghttputil.NewRequestBodyMemoryBuffer(size)
}

// closeStorage 在 HTTP Forward 整体结束或 WS bridge 单轮结束时调用，保留响应头之后
// 的上传、SSE 消费和本轮重试所需的 GetBody 生命周期；nil 工作区不需要清理。
func (b *officialForwardHTTPBody) closeStorage() {
	if b == nil {
		return
	}
	if b.releaseStorage != nil {
		b.releaseStorage()
	}
	// 完整转发已经结束；即使诊断信息或 HTTP transport 暂时仍持有 ctx，也不应
	// 再通过工作区保活入口原文、索引和对象树。出站重放已独立持有定型正文。
	*b = officialForwardHTTPBody{scans: b.scans}
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

// officialForwardOffsetWithin 返回 part 在 whole 内存区间中的起始偏移；part 不完整落在 whole 内时返回 -1。
func officialForwardOffsetWithin(part, whole []byte) int {
	if len(part) == 0 || len(whole) == 0 {
		return -1
	}
	begin := uintptr(unsafe.Pointer(unsafe.SliceData(whole)))
	pointer := uintptr(unsafe.Pointer(unsafe.SliceData(part)))
	if pointer < begin || pointer+uintptr(len(part)) > begin+uintptr(len(whole)) {
		return -1
	}
	return int(pointer - begin)
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
	b.scans++
	index, err := buildOfficialJSONRawIndexForDecode(body)
	if err != nil {
		return nil
	}
	b.index, b.indexBody = index, body
	return index
}

// decodeRequestView 与 view.Decode(c)（getOpenAIRequestBodyMap）结果逐项相等（含全部错误），复用视图
// 正文的索引。对象树在索引上直建，无转义的长字符串以只读视图引用视图正文（decodeValueSharingBody），
// 不再复制整段正文；树只在本次 Forward 内被业务改写与重编码使用，上游 attempt 前由 park 放下。
func (b *officialForwardHTTPBody) decodeRequestView(c *gin.Context, view openAIRequestView) (map[string]any, error) {
	if b == nil {
		return view.Decode(c)
	}
	if index := b.indexFor(view.body); index != nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		if object, ok := index.decodeValueSharingBody(index.root).(map[string]any); ok {
			b.applyLiteDefaults(object)
			b.applyDeferredFields(object, view.body)
			return object, nil
		}
	}
	object, err := getOpenAIRequestBodyMap(c, view.body)
	if err == nil {
		b.applyLiteDefaults(object)
		b.applyDeferredFields(object, view.body)
	}
	return object, err
}

// normalizeResponsesLitePayloadForAccount 的复杂路径保持原逻辑；可独立校验的小字段延迟到本次
// HTTP 正文定型时写出。没有工作区的 API Key、透传和 WS 路径仍立即归一化。
func (b *officialForwardHTTPBody) normalizeResponsesLitePayloadForAccount(body []byte, account *Account) ([]byte, bool, error) {
	if b != nil && account != nil && account.IsOpenAIOAuthLike() {
		_, handled, err := normalizeOpenAIResponsesLiteHeaderFields(body)
		if handled {
			if err == nil {
				b.liteDefaults = true
			}
			return body, false, err
		}
	}
	return normalizeOpenAIResponsesLitePayloadForAccount(body, account)
}

func (b *officialForwardHTTPBody) applyLiteDefaults(payload map[string]any) {
	if b == nil || !b.liteDefaults || payload == nil {
		return
	}
	// 每次重新解码后独立补齐，避免共享的小 map 被后续模型归一化改写。
	_, _ = ensureOpenAIResponsesLiteReasoningContext(payload)
	payload["parallel_tool_calls"] = false
}

func (b *officialForwardHTTPBody) applyDeferredFields(payload map[string]any, body []byte) bool {
	if b == nil || payload == nil || !officialForwardSameBody(body, b.deferredBody) {
		return false
	}
	for name, raw := range b.deferredFields {
		if raw == nil {
			delete(payload, name)
			continue
		}
		// 小字段已经由拼接编码器验证成功；重新解码给本次调用，防止 map 改写污染重试基准。
		value, err := decodeOfficialJSONValueUseNumber(raw)
		if err == nil {
			payload[name] = value
		}
	}
	return len(b.deferredFields) > 0
}

// applyFinalizerFields 同时恢复 Lite 默认值与小字段覆盖。移交对象树可能早已带有默认值，
// 因此须与原文字段比较；不能只依赖 Finalizer 就地修改的返回值来决定是否写出新正文。
func (b *officialForwardHTTPBody) applyFinalizerFields(payload map[string]any, body []byte) bool {
	if b == nil || payload == nil {
		return false
	}
	modified := false
	if b.liteDefaults {
		b.applyLiteDefaults(payload)
		reasoningContext := openAIBodyGet(body, "reasoning.context")
		parallel := openAIBodyGet(body, "parallel_tool_calls")
		modified = reasoningContext.Type != gjson.String || reasoningContext.String() != "all_turns" || parallel.Type != gjson.False
	}
	return b.applyDeferredFields(payload, body) || modified
}

// deferSmallFields 仅在全部大字段逐值相等时延迟少量顶层差异。数组过滤器即使没有改变 input
// 也会标记 Modified；过去这会重建整份正文。小字段留到 Finalizer 一并写出，正文相关读取仍
// 从原文取得 input/model 等不变字段，对象树读取则通过 applyDeferredFields 恢复同一语义。
func (b *officialForwardHTTPBody) deferSmallFields(payload map[string]any, base []byte, index *officialJSONRawIndex, members []officialegress.JSONObjectMember) bool {
	if b == nil || !b.liteDefaults || index == nil || index.nodes[index.root].kind != officialJSONRawKindObject {
		return false
	}
	allowed := func(name string) bool {
		switch name {
		case "include", "reasoning", "parallel_tool_calls", "prompt_cache_key", "client_metadata":
			return true
		default:
			return false
		}
	}
	changed := make(map[string]bool)
	for _, name := range index.uniqueKeys(index.root) {
		value, exists := payload[name]
		if exists && index.equals(index.memberNode(index.root, name), value) {
			continue
		}
		if !allowed(name) {
			return false
		}
		if name == "prompt_cache_key" {
			key, ok := value.(string)
			if !exists || !ok || strings.TrimSpace(key) == "" {
				// 空键或删除会启用完整正文内容回退，必须立即物化，不能沿用原键。
				return false
			}
		}
		if name == "reasoning" && !officialForwardOnlyReasoningContextChanged(index, index.memberNode(index.root, name), value) {
			return false
		}
		changed[name] = exists
	}
	for name := range payload {
		if index.memberNode(index.root, name) >= 0 {
			continue
		}
		if !allowed(name) {
			return false
		}
		if name == "prompt_cache_key" {
			key, ok := payload[name].(string)
			if !ok || strings.TrimSpace(key) == "" {
				return false
			}
		}
		if name == "reasoning" && !officialForwardOnlyReasoningContextChanged(index, -1, payload[name]) {
			return false
		}
		changed[name] = true
	}
	fields := make(map[string]json.RawMessage, len(changed))
	for name, present := range changed {
		if !present {
			fields[name] = nil
		}
	}
	totalBytes := 0
	for _, member := range members {
		if !changed[member.Name] {
			continue
		}
		size := len(member.Value)
		if member.ValueSegments != nil {
			size = 0
			for _, segment := range member.ValueSegments {
				size += len(segment)
			}
		}
		totalBytes += size
		if totalBytes > officialForwardDetachLocateMinBytes {
			return false
		}
		raw := make([]byte, 0, size)
		if member.ValueSegments == nil {
			raw = append(raw, member.Value...)
		} else {
			for _, segment := range member.ValueSegments {
				raw = append(raw, segment...)
			}
		}
		fields[member.Name] = raw
	}
	b.deferredBody, b.deferredFields = base, fields
	return true
}

// reasoning.effort 等值会被后续原文读取方使用，仅 context 默认值允许延迟。
func officialForwardOnlyReasoningContextChanged(index *officialJSONRawIndex, node int32, value any) bool {
	object, ok := value.(map[string]any)
	if !ok {
		return false
	}
	if node < 0 || index.nodes[node].kind == officialJSONRawKindNull {
		return len(object) == 1 && object["context"] == "all_turns"
	}
	if index.nodes[node].kind != officialJSONRawKindObject {
		return false
	}
	for _, name := range index.uniqueKeys(node) {
		if name == "context" {
			continue
		}
		child, exists := object[name]
		if !exists || !index.equals(index.memberNode(node, name), child) {
			return false
		}
	}
	for name := range object {
		if name != "context" && index.memberNode(node, name) < 0 {
			return false
		}
	}
	return true
}

// carryDeferredFields 只用于已知保留覆盖层字段的局部 input/拒绝字段转换；变换后的正文仍需
// 在对象树和 Finalizer 中应用同一覆盖层，不应因底层切片换版而丢失已经完成的业务修改。
func (b *officialForwardHTTPBody) carryDeferredFields(before, after []byte) {
	if b != nil && officialForwardSameBody(before, b.deferredBody) {
		b.deferredBody = after
	}
}

// sessionHashBody 供正文仍未物化时读取已作用域化的 prompt_cache_key；头部优先级由原会话
// 哈希函数保持。这里只在非空字符串存在时用小对象投影，其他情况继续使用完整原文做内容回退。
func (b *officialForwardHTTPBody) sessionHashBody(body []byte) []byte {
	if b == nil || !officialForwardSameBody(body, b.deferredBody) {
		return body
	}
	raw, changed := b.deferredFields["prompt_cache_key"]
	if !changed || strings.TrimSpace(gjson.ParseBytes(raw).String()) == "" {
		return body
	}
	out := make([]byte, 0, len(raw)+32)
	out = append(out, `{"prompt_cache_key":`...)
	out = append(out, raw...)
	return append(out, '}')
}

// reencodeRequestBody 用对象树 payload 更新 *body。一般路径返回并写回与旧保序编码器逐字节相同的正文；
// 经 Lite 校验且只改变受控小字段时保留原切片，将差异存为覆盖层，交给对象树读取方和 Finalizer 应用。
// 覆盖路径中的原文读取方只读取没有变化的业务字段，会话哈希单独通过 sessionHashBody 取得新缓存键。
// 出错时返回 nil，*body 也置为 nil。
// 调用方沿用改造前的写法 `body, err = w.reencodeRequestBody(payload, &body, ...)` 接收；传入正文的地址
// 只是为了在物化新正文之前放下旧正文。
//
// 官方出站 HTTP 路径上按顶层成员重编码（复用旧正文的索引），成员值与调用方原始正文逐字节相同的大
// 成员改为引用原始正文、其余引用旧正文的成员值复制成小段；随后在物化新正文之前先放下旧正文、请求
// 视图里的旧正文、对象树与旧正文的索引（问题四 M2-a），旧正文因此不会与新正文同时存活。调用方紧接着
// 会按新正文重建请求视图；对象树之后需要时 ensureReqBody 从新正文重新解码，内容与放下的树相同。
// 工作区记下产出新正文的成员及各成员值的来源，供 Finalizer 与上游 attempt 期间的暂存使用。
func (b *officialForwardHTTPBody) reencodeRequestBody(
	payload map[string]any,
	body *[]byte,
	view *openAIRequestView,
	reqBody *map[string]any,
) ([]byte, error) {
	if b == nil {
		rebuilt, err := marshalOfficialJSONObjectPreservingOrderAndRaw(payload, *body)
		*body = rebuilt
		return rebuilt, err
	}
	base := *body
	index := b.indexFor(base)
	index.ensureDigests()
	members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, nil, base, index)
	if err != nil {
		*body = nil
		return nil, err
	}
	if b.deferSmallFields(payload, base, index, members) {
		// 正文底层数组没有改变，现有对象树也已是覆盖后的语义；继续交给 Finalizer，避免再解码。
		*reqBody = payload
		return base, nil
	}
	b.deferredBody, b.deferredFields = nil, nil
	// 当前成员可能来自上一次物化结果，先沿已有区间回指，再清空旧物化状态。
	b.rebaseFinalMembers(base, members)
	b.detachMembersFrom(members, base)
	b.members, b.rebuilt, b.spans = nil, nil, nil
	*reqBody = nil
	*body = nil
	view.body = nil
	b.index, b.indexBody = nil, nil
	b.members = members
	b.rebuilt, b.spans = officialForwardMaterializeMembers(members)
	*body = b.rebuilt
	return b.rebuilt, nil
}

// releaseRequestMap 在官方出站 HTTP 路径进入定型之前，把对象树移交给 Finalizer，避免重复解码。只在请求
// 视图与当前正文是同一版本时移交：此时之后需要对象树的地方（上游返回 invalid_encrypted_content 后的
// 重试）会由 ensureReqBody 从同一正文与覆盖层重新解码；二者不一致（失效密文重试已改写
// 正文而视图仍是旧版本）时保留，行为与过去完全相同。nil 工作区不做任何事。
func (b *officialForwardHTTPBody) releaseRequestMap(reqBody *map[string]any, view openAIRequestView, body []byte) {
	if b == nil || !officialForwardSameBody(view.body, body) {
		return
	}
	b.finalizerBody, b.finalizerPayload = body, *reqBody
	*reqBody = nil
}

// decodeFinalizerPayload 接收 Forward 移交的最后一棵对象树。map 不复制，所有权只属于本次
// Finalizer；后续重试无法取得它，从未变的正文及小字段覆盖层重新解码。
func (b *officialForwardHTTPBody) decodeFinalizerPayload(body []byte) (map[string]any, *officialJSONRawIndex, error) {
	if b != nil && b.finalizerPayload != nil && officialForwardSameBody(body, b.finalizerBody) {
		payload := b.finalizerPayload
		b.finalizerBody, b.finalizerPayload = nil, nil
		index := b.indexFor(body)
		index.ensureDigests()
		return payload, index, nil
	}
	if b != nil {
		b.finalizerBody, b.finalizerPayload = nil, nil
	}
	return decodeOfficialJSONObjectSharingBodyWithIndex(body, b.indexFor(body))
}

// detachMembersFrom 让成员值不再引用 base（改写前的中间版本正文）：与调用方原始正文同名成员逐字节
// 相同的大成员值改为引用原始正文；其余引用 base 的成员值复制成独立小段。大成员值在原始正文里找不到
// 逐字节相同的对应时保留引用（这部分内容本就必须存在，由 base 承载）。base 就是原始正文时无需处理。
func (b *officialForwardHTTPBody) detachMembersFrom(members []officialegress.JSONObjectMember, base []byte) {
	if officialForwardSameBody(base, b.ingress) {
		return
	}
	large := make(map[string]int)
	for i := range members {
		if members[i].ValueSegments != nil {
			b.detachMemberSegmentsFrom(&members[i], base)
			continue
		}
		value := members[i].Value
		if officialForwardOffsetWithin(value, base) < 0 {
			continue
		}
		if len(value) >= officialForwardDetachLocateMinBytes {
			if len(b.ingress) > 0 {
				large[members[i].Name] = i
			}
			continue
		}
		members[i].Value = append([]byte(nil), value...)
	}
	if len(large) == 0 {
		return
	}
	// 零拷贝遍历原始正文的顶层成员（重复键逐个出现），按同名且逐字节相同找对应区间。
	ingressText := openAIBodyString(b.ingress)
	openAIBodyRoot(b.ingress).ForEach(func(key, candidate gjson.Result) bool {
		i, pending := large[key.Str]
		if !pending {
			return true
		}
		value := members[i].Value
		if len(candidate.Raw) == len(value) && candidate.Raw == unsafe.String(unsafe.SliceData(value), len(value)) {
			offset := int(uintptr(unsafe.Pointer(unsafe.StringData(candidate.Raw))) -
				uintptr(unsafe.Pointer(unsafe.StringData(ingressText))))
			members[i].Value = b.ingress[offset : offset+len(value) : offset+len(value)]
			delete(large, key.Str)
		}
		return len(large) > 0
	})
}

// detachMemberSegmentsFrom 把改动数组中未变的历史元素回指到入口原文。数组整体因插入指令而变化，
// 但当前 base 中同名数组通常仍与入口完全相同；比较一次大数组后，各元素只按偏移换片段。
func (b *officialForwardHTTPBody) detachMemberSegmentsFrom(member *officialegress.JSONObjectMember, base []byte) {
	baseValue := openAIBodyGet(base, member.Name)
	ingressValue := openAIBodyGet(b.ingress, member.Name)
	var baseBytes, ingressBytes []byte
	if baseValue.Raw != "" && baseValue.Raw == ingressValue.Raw &&
		baseValue.Index >= 0 && baseValue.Index+len(baseValue.Raw) <= len(base) &&
		ingressValue.Index >= 0 && ingressValue.Index+len(ingressValue.Raw) <= len(b.ingress) {
		baseBytes = base[baseValue.Index : baseValue.Index+len(baseValue.Raw)]
		ingressBytes = b.ingress[ingressValue.Index : ingressValue.Index+len(ingressValue.Raw)]
	}
	for i, segment := range member.ValueSegments {
		if offset := officialForwardOffsetWithin(segment, baseBytes); offset >= 0 {
			member.ValueSegments[i] = ingressBytes[offset : offset+len(segment) : offset+len(segment)]
		} else if len(segment) < officialForwardDetachLocateMinBytes && officialForwardOffsetWithin(segment, base) >= 0 {
			member.ValueSegments[i] = append([]byte(nil), segment...)
		}
	}
}

// officialForwardMaterializeMembers 把成员依次写成顶层对象（与 officialegress.AppendJSONObjectMembers
// 逐字节相同），并记下每个成员值在结果中的区间与来源。
func officialForwardMaterializeMembers(members []officialegress.JSONObjectMember) ([]byte, []officialForwardBodySpan) {
	out := make([]byte, 0, officialegress.JSONObjectMembersLength(members))
	spans := make([]officialForwardBodySpan, 0, len(members))
	out = append(out, '{')
	for i, member := range members {
		if i > 0 {
			out = append(out, ',')
		}
		out = append(out, member.QuotedName...)
		out = append(out, ':')
		values := member.ValueSegments
		if values == nil {
			values = [][]byte{member.Value}
		}
		for _, value := range values {
			start := len(out)
			out = append(out, value...)
			spans = append(spans, officialForwardBodySpan{start: start, end: len(out), source: value})
		}
	}
	return append(out, '}'), spans
}

// sourceOf 把落在最近一次物化正文内的切片换成来源里逐字节相同的切片；不在其中时原样返回。
func (b *officialForwardHTTPBody) sourceOf(value []byte) []byte {
	offset := officialForwardOffsetWithin(value, b.rebuilt)
	if offset < 0 {
		return value
	}
	end := offset + len(value)
	i := sort.Search(len(b.spans), func(k int) bool { return b.spans[k].end > offset })
	if i >= len(b.spans) || b.spans[i].start > offset || end > b.spans[i].end {
		return value
	}
	from := offset - b.spans[i].start
	return b.spans[i].source[from : from+len(value) : from+len(value)]
}

// rebaseFinalMembers 在 body 正是最近一次 reencode 物化出的正文时，把 Finalizer 成员中引用它的值换成
// 来源切片（逐字节相同），请求体因此不再引用这份整段正文。其余情况什么都不做。
func (b *officialForwardHTTPBody) rebaseFinalMembers(body []byte, members []officialegress.JSONObjectMember) {
	if b == nil || b.rebuilt == nil || !officialForwardSameBody(body, b.rebuilt) {
		return
	}
	for i := range members {
		if members[i].ValueSegments != nil {
			for j := range members[i].ValueSegments {
				members[i].ValueSegments[j] = b.sourceOf(members[i].ValueSegments[j])
			}
			continue
		}
		value := members[i].Value
		if source := b.sourceOf(value); !officialForwardSameBody(source, value) {
			members[i].Value = source
		} else if segments := b.sourceSegmentsOf(value); len(segments) > 1 {
			members[i].Value = nil
			members[i].ValueSegments = segments
		}
	}
}

// sourceSegmentsOf 处理一个数组值横跨多个原始片段的情况；中间只要出现未登记区间就原样保留，
// 不凭内容猜测来源。由成员物化的数组片段连同标点均有 span，因此可完整恢复原分段表示。
func (b *officialForwardHTTPBody) sourceSegmentsOf(value []byte) [][]byte {
	offset := officialForwardOffsetWithin(value, b.rebuilt)
	if offset < 0 {
		return nil
	}
	end := offset + len(value)
	i := sort.Search(len(b.spans), func(k int) bool { return b.spans[k].end > offset })
	var segments [][]byte
	for offset < end {
		if i >= len(b.spans) || b.spans[i].start > offset || b.spans[i].end <= offset {
			return nil
		}
		span := b.spans[i]
		to := min(end, span.end)
		segments = append(segments, span.source[offset-span.start:to-span.start:to-span.start])
		offset = to
		i++
	}
	return segments
}

// membersFor 在 body 正是最近一次 reencode 物化出的正文时返回产出它的顶层成员（依次写出与 body
// 逐字节相同），否则返回 nil。Finalizer 不改写正文时据此按成员装配请求体。
func (b *officialForwardHTTPBody) membersFor(body []byte) []officialegress.JSONObjectMember {
	if b == nil || b.rebuilt == nil || !officialForwardSameBody(body, b.rebuilt) {
		return nil
	}
	return b.members
}

// park 在上游 attempt（编译、签名、zstd 压缩与发送）前放下 Forward 对整段正文的引用（问题四 M2-a）。只有
// 当前正文正是最近一次重编码物化出的那份时才放下：此时请求体已按成员装配、成员值引用调用方原始正文或
// 小段副本，不再引用它；正文、请求视图、lineage 基准与该正文的索引一并放下。nil 工作区、已暂存或其余
// 情况什么都不做。
//
// attempt 返回后不再无条件恢复（问题四 M3-a）：成功路径只需要 service_tier（serviceTier 从成员读出），
// 响应流式期间也不再保活整段正文；错误处理、compact 回退与重试这些需要正文的分支入口调用 restore。
func (b *officialForwardHTTPBody) park(body *[]byte, view *openAIRequestView, lineage *[]byte) {
	if b == nil || b.parked {
		return
	}
	if b.rebuilt == nil {
		// 只有小字段覆盖层时没有中间正文可释放，但 Finalizer 完成后仍应放下对象索引。
		b.index, b.indexBody = nil, nil
		return
	}
	if !officialForwardSameBody(*body, b.rebuilt) {
		return
	}
	b.parked = true
	b.parkedBody, b.parkedView, b.parkedLineage = body, view, lineage
	b.viewState = *view
	b.viewParked = officialForwardSameBody(b.viewState.body, b.rebuilt)
	b.lineageParked = officialForwardSameBody(*lineage, b.rebuilt)
	b.viewState.body = nil
	*body = nil
	if b.viewParked {
		*view = openAIRequestView{}
	}
	if b.lineageParked {
		*lineage = nil
	}
	b.rebuilt, b.spans = nil, nil
	b.index, b.indexBody = nil, nil
}

// restore 按成员重新物化出与暂存前逐字节相同的正文，放回 park 时放下的正文、请求视图与 lineage 基准，
// 并恢复按来源回指的能力。可重复调用；nil 工作区或未暂存时什么都不做。
func (b *officialForwardHTTPBody) restore() {
	if b == nil || !b.parked {
		return
	}
	b.parked = false
	b.rebuilt, b.spans = officialForwardMaterializeMembers(b.members)
	*b.parkedBody = b.rebuilt
	if b.viewParked {
		view := b.viewState
		view.body = b.rebuilt
		*b.parkedView = view
	}
	if b.lineageParked {
		*b.parkedLineage = b.rebuilt
	}
	b.parkedBody, b.parkedView, b.parkedLineage = nil, nil, nil
	b.viewState = openAIRequestView{}
}

// serviceTier 与 extractOpenAIServiceTierFromBody(body) 结果相同。正文已暂存时从产出它的顶层成员读出：
// 成员名唯一，gjson 取顶层 service_tier 的第一次出现即该成员的值；读出的小值先复制，不引用正文。
func (b *officialForwardHTTPBody) serviceTier(body []byte) *string {
	if b == nil || !b.parked {
		return extractOpenAIServiceTierFromBody(body)
	}
	for _, member := range b.members {
		if member.Name == "service_tier" {
			return normalizeOpenAIServiceTier(gjson.Parse(string(member.Value)).String())
		}
	}
	return nil
}

// normalizeCompactionTriggerInputOrder 等价于 NormalizeCompactionTriggerInputOrder，复用 body 的索引。
func (b *officialForwardHTTPBody) normalizeCompactionTriggerInputOrder(body []byte) ([]byte, bool, error) {
	if b == nil {
		return NormalizeCompactionTriggerInputOrder(body)
	}
	normalized, changed, err := normalizeCompactionTriggerInputOrderWithIndex(body, b.indexFor(body))
	if err == nil && changed && officialForwardSameBody(body, b.deferredBody) {
		// 此转换只调整 input 项的位置，其余小字段覆盖继续绑定到新的正文版本。
		b.deferredBody = normalized
	}
	return normalized, changed, err
}
