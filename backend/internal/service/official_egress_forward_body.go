package service

import (
	"context"
	"sort"
	"unsafe"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
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
// 工作区只在官方出站 HTTP 路径上创建（Forward 中 officialOpenAIHTTPEnabled 为真时），经 ctx 传给
// 同一次调用的 Finalizer；其余路径拿到的是 nil。所有方法对 nil 接收者原样调用改造前的函数或什么都
// 不做，非官方出站路径（API Key、透传、兼容转换、WS 及 WS→HTTP 回落等）代码路径与行为不变。
//
// 正文按只读约束使用（与 openai_json_rawview.go 的零拷贝视图相同）：以“同一底层数组起点、同一
// 长度”识别正文版本，正文一经产出不再原地改写，因此同一切片必然是同一内容；对象树与成员里引用
// 正文的值只会被比较或重新编码进新正文，不会保存到请求之外。
type officialForwardHTTPBody struct {
	// ingress 是调用方传入 Forward 的原始正文，整个请求期间由调用方持有。
	ingress []byte

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
	body := &officialForwardHTTPBody{ingress: ingress}
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
			return object, nil
		}
	}
	return getOpenAIRequestBodyMap(c, view.body)
}

// reencodeRequestBody 用对象树 payload 整体重编码 *body，返回新正文并同时写回 *body，结果与
// `marshalOfficialJSONObjectPreservingOrderAndRaw(payload, *body)` 逐字节相同（出错时返回 nil，*body 也置为 nil）。
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
	b.members, b.rebuilt, b.spans = nil, nil, nil
	if err != nil {
		*body = nil
		return nil, err
	}
	b.detachMembersFrom(members, base)
	*reqBody = nil
	*body = nil
	view.body = nil
	b.index, b.indexBody = nil, nil
	b.members = members
	b.rebuilt, b.spans = officialForwardMaterializeMembers(members)
	*body = b.rebuilt
	return b.rebuilt, nil
}

// releaseRequestMap 在官方出站 HTTP 路径进入定型与上游 attempt 之前放下对象树（问题四 M2-a）。只在请求
// 视图与当前正文是同一版本时放下：此时之后需要对象树的地方（上游返回 invalid_encrypted_content 后的
// 重试）会由 ensureReqBody 从同一正文重新解码，内容与放下的树相同；二者不一致（失效密文重试已改写
// 正文而视图仍是旧版本）时保留，行为与过去完全相同。nil 工作区不做任何事。
func (b *officialForwardHTTPBody) releaseRequestMap(reqBody *map[string]any, view openAIRequestView, body []byte) {
	if b == nil || !officialForwardSameBody(view.body, body) {
		return
	}
	*reqBody = nil
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
		start := len(out)
		out = append(out, member.Value...)
		spans = append(spans, officialForwardBodySpan{start: start, end: len(out), source: member.Value})
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
		members[i].Value = b.sourceOf(members[i].Value)
	}
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
	if b == nil || b.parked || b.rebuilt == nil || !officialForwardSameBody(*body, b.rebuilt) {
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
	return normalizeCompactionTriggerInputOrderWithIndex(body, b.indexFor(body))
}
