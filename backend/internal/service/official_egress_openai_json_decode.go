package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"unsafe"
)

// 本文件把 UseNumber 语义的 JSON 解码改为“索引直建对象树”（docs/bug.md 6.4 第 3 点）。
//
// Go 1.27 的 encoding/json Decoder 从 64 字节起倍增内部读取缓冲，解码一个 17 MB 正文要
// 额外分配约 4 倍正文的缓冲；官方出站链上一个请求至少解码三次，这部分曾是放大倍数的
// 大头。索引扫描器一次只读扫描就能拿到全部值区间与结构，在其上直接构建
// map[string]any / []any / string / json.Number / bool / nil 的对象树，与
// Decoder.UseNumber().Decode 的结果逐字段一致：同名键取最后一次、数字保留十进制文本、
// 字符串按 encoding/json 规则反转义并把非法 UTF-8 逐字节替换为 U+FFFD、空容器为非 nil。
// 扫描失败或顶层不是对象时退回 encoding/json 路径，保证错误值与过去完全一致；扫描器
// 的语法只会比 encoding/json 更严，不会更宽（差分测试锁定）。

// buildOfficialJSONRawIndexForDecode 只登记节点、不计算结构摘要，也不建立复合值索引。
func buildOfficialJSONRawIndexForDecode(body []byte) (*officialJSONRawIndex, error) {
	if len(bytes.TrimSpace(body)) == 0 {
		return nil, errors.New("JSON 正文为空")
	}
	index := &officialJSONRawIndex{
		body:       body,
		nodes:      make([]officialJSONRawNode, 0, 64),
		skipDigest: true,
	}
	scanner := officialJSONRawScanner{index: index}
	root, err := scanner.parseValue(0)
	if err != nil {
		return nil, err
	}
	scanner.skipSpace()
	if scanner.pos != len(body) {
		return nil, errors.New("JSON 正文包含多个顶层值")
	}
	index.root = root
	return index, nil
}

// ensureDigests 把只登记节点的索引（buildOfficialJSONRawIndexForDecode）就地补齐为完整索引，
// 结果与对同一正文调用 buildOfficialJSONRawIndex 逐项相同（问题四 M2-c）。
//
// 同一版本的正文先后要被解码与拼接编码使用：解码只需要节点，拼接编码还需要结构摘要与复合值
// 索引。过去两处各扫描一遍正文；这里不再重新扫描，只沿节点表补算摘要。节点在扫描时总是先登记
// 子节点、后登记父节点，因此按下标顺序计算即可保证子节点摘要先于父节点就绪；字符串、数字与
// 字面量的摘要规则与扫描器逐条相同，复合值随后按前序登记，顺序与扫描器一致。已是完整索引时直接返回。
func (index *officialJSONRawIndex) ensureDigests() {
	if index == nil || !index.skipDigest {
		return
	}
	index.skipDigest = false
	index.seed = officialJSONRawHashSeed
	for i := range index.nodes {
		n := &index.nodes[i]
		switch n.kind {
		case officialJSONRawKindString:
			segment := index.body[n.start+1 : n.end-1]
			if !n.slowPath {
				n.hash = index.hashBytes('s', segment)
				continue
			}
			// 扫描阶段已按同一规则反转义校验过这段字节，这里不会出错。
			unescaped, _ := officialJSONUnescape(index.scratch[:0], segment)
			index.scratch = unescaped[:0]
			n.hash = index.hashBytes('s', unescaped)
		case officialJSONRawKindNumber:
			n.hash = index.hashBytes('n', index.body[n.start:n.end])
		case officialJSONRawKindTrue, officialJSONRawKindFalse, officialJSONRawKindNull:
			n.hash = index.hashBytes(officialJSONRawHashTag(n.kind), nil)
		case officialJSONRawKindObject:
			n.hash = index.hashObjectMembers(n.members)
		case officialJSONRawKindArray:
			n.hash = index.hashArrayItems(n.items)
		}
	}
	index.byHash = make(map[uint64][]int32)
	index.registerComposites(index.root)
}

// decodeValue 把索引节点还原为 encoding/json 解码到 any 时的 Go 值。
func (index *officialJSONRawIndex) decodeValue(node int32) any {
	n := &index.nodes[node]
	switch n.kind {
	case officialJSONRawKindObject:
		object := make(map[string]any, len(n.members))
		for _, member := range n.members {
			// 成员按原始顺序写入：同名键自然取最后一次出现，与 encoding/json 一致。
			object[member.key] = index.decodeValue(member.node)
		}
		return object
	case officialJSONRawKindArray:
		items := make([]any, len(n.items))
		for i, item := range n.items {
			items[i] = index.decodeValue(item)
		}
		return items
	case officialJSONRawKindString:
		return index.decodeString(node)
	case officialJSONRawKindNumber:
		return json.Number(index.body[n.start:n.end])
	case officialJSONRawKindTrue:
		return true
	case officialJSONRawKindFalse:
		return false
	default:
		return nil
	}
}

// decodeString 返回字符串节点反转义后的 Go 字符串；无转义且为合法 UTF-8 时直接复制原文。
func (index *officialJSONRawIndex) decodeString(node int32) string {
	n := &index.nodes[node]
	segment := index.body[n.start+1 : n.end-1]
	if !n.slowPath {
		return string(segment)
	}
	// 扫描阶段已经反转义校验过同一段字节，这里不会再出错。
	unescaped, err := officialJSONUnescape(index.scratch[:0], segment)
	index.scratch = unescaped[:0]
	if err != nil {
		return string(segment)
	}
	return string(unescaped)
}

// decodeOfficialJSONObjectUseNumber 在任何可能重新编码正文的官方出站路径中
// 保留 JSON 数字的十进制文本，防止大整数经过 float64 后发生不可逆改写。
func decodeOfficialJSONObjectUseNumber(body []byte) (map[string]any, error) {
	index, err := buildOfficialJSONRawIndexForDecode(body)
	if err != nil || index.nodes[index.root].kind != officialJSONRawKindObject {
		return decodeOfficialJSONObjectUseNumberSlow(body)
	}
	return index.decodeObject(index.root), nil
}

// decodeObject 把已确认为对象的节点还原为 map；调用方必须先检查节点类型。
func (index *officialJSONRawIndex) decodeObject(node int32) map[string]any {
	object, ok := index.decodeValue(node).(map[string]any)
	if !ok {
		return map[string]any{}
	}
	return object
}

// decodeOfficialJSONValueUseNumber 以 UseNumber 语义解码任意 JSON 值。
func decodeOfficialJSONValueUseNumber(body []byte) (any, error) {
	index, err := buildOfficialJSONRawIndexForDecode(body)
	if err != nil {
		return decodeOfficialJSONValueUseNumberSlow(body)
	}
	return index.decodeValue(index.root), nil
}

// ---------------------------------------------------------------------------
// 引用正文字节的解码（问题四 M1 第二项）
// ---------------------------------------------------------------------------
//
// 官方出站 Finalizer 每个 attempt 都要把终态修正前的正文整段解码成对象树，再按画像改写
// 顶层字段并拼接编码。树里的长字符串（加密推理内容、工具输出、消息文本）绝大多数原样
// 写回，却在解码时被从正文完整复制一遍，约等于再占一份正文。
//
// 这里仍在正文索引上按原结构新建对象与数组（Finalizer 会就地改写它们），但无转义且足够
// 长的字符串不再复制，直接以只读视图引用正文字节。前提与 openai_json_rawview.go 的零拷贝
// 视图相同：官方出站链上的请求正文一经产出即只读，视图只能在正文存活期间使用，不得保存到
// 请求之外。短字符串（类型、角色、ID、模型名等可能被登记或缓存的小值）与含转义的字符串
// 照常复制，因此不会出现一个小字符串把整段正文钉在内存里的情况。

// officialJSONSharedStringMinBytes 是按视图引用正文的字符串最小字节数。
const officialJSONSharedStringMinBytes = 1024

// decodeOfficialJSONObjectSharingBody 与 decodeOfficialJSONObjectUseNumber 的结果逐项相等，
// 并返回建好的完整正文索引供拼接编码复用；长字符串以只读视图引用 body，调用方只能在
// body 存活且不被改写的期间使用返回的对象树，且不得把其中的值保存到本次请求之外。索引
// 构建失败或顶层不是对象时走 encoding/json 路径，错误值与原实现一致，此时不返回索引。
func decodeOfficialJSONObjectSharingBody(body []byte) (map[string]any, *officialJSONRawIndex, error) {
	return decodeOfficialJSONObjectSharingBodyWithIndex(body, nil)
}

// decodeOfficialJSONObjectSharingBodyWithIndex 同 decodeOfficialJSONObjectSharingBody；index 若非 nil 必须是
// body 扫描成功得到的索引（只登记节点或完整均可），补齐结构摘要后直接复用，不再为同一正文扫描
// （问题四 M2-c）。返回的索引与现场建立的完整索引逐项相同。
func decodeOfficialJSONObjectSharingBodyWithIndex(body []byte, index *officialJSONRawIndex) (map[string]any, *officialJSONRawIndex, error) {
	var err error
	if index == nil {
		index, err = buildOfficialJSONRawIndex(body)
	} else {
		index.ensureDigests()
	}
	if err != nil || index.nodes[index.root].kind != officialJSONRawKindObject {
		payload, slowErr := decodeOfficialJSONObjectUseNumberSlow(body)
		return payload, nil, slowErr
	}
	object, ok := index.decodeValueSharingBody(index.root).(map[string]any)
	if !ok {
		object = map[string]any{}
	}
	return object, index, nil
}

// decodeValueSharingBody 构建与 decodeValue(node) 逐项相等的值，只把无转义的长字符串换成
// 引用正文的只读视图。
func (index *officialJSONRawIndex) decodeValueSharingBody(node int32) any {
	n := &index.nodes[node]
	switch n.kind {
	case officialJSONRawKindObject:
		object := make(map[string]any, len(n.members))
		for _, member := range n.members {
			// 成员按原始顺序写入：同名键自然取最后一次出现，与 decodeValue 一致。
			object[member.key] = index.decodeValueSharingBody(member.node)
		}
		return object
	case officialJSONRawKindArray:
		items := make([]any, len(n.items))
		for i, item := range n.items {
			items[i] = index.decodeValueSharingBody(item)
		}
		return items
	case officialJSONRawKindString:
		// 快速路径的字符串无转义且是合法 UTF-8，decodeString 返回的正是这段原文的副本。
		if length := n.end - n.start - 2; !n.slowPath && length >= officialJSONSharedStringMinBytes {
			return unsafe.String(&index.body[n.start+1], length)
		}
		return index.decodeString(node)
	default:
		return index.decodeValue(node)
	}
}
