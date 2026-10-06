package service

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"hash/maphash"
	"slices"
	"sort"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// 本文件实现官方出站定型编码器的“字节区间拼接”后端（docs/bug.md 6.4 第 3 点，
// CODEX_CLI_CLIENT_EMULATION_GUIDE.md §3.3「URL、header 与 body」小节）。
//
// 旧实现为了复用未改动值的原始字节，把整段正文的每个嵌套值解码、规范化编码后建成
// 全文查找池，再对每个位置解码比对；12 MB 正文单次编码累计分配约 885 MiB。这里改为
// 一次只读扫描建立索引：每个 JSON 值只记录字节区间、结构摘要和成员/元素关系，不解码
// 成 Go 值，也不复制字节。编码时对候选值做零分配的精确比对，命中即直接拼接原始区间。
//
// 输出语义与旧实现逐字节一致（由 official_egress_openai_json_splice_test.go 差分锁定）：
//   - 同位置值相等 → 复用原始区间；
//   - 否则复合值按“内容相等”在全文中查找首个前序出现（画像可能把工具或消息搬到另一个
//     顶层字段）；
//   - 否则对象保留原始成员顺序、新增键按字典序追加，数组项先按内容匹配原始项再按位置
//     对应；
//   - 兜底才走 encoding/json 规范化编码。
// “相等”沿用旧实现的规范化 JSON 相等：对象键序无关且同名键取最后一次，数组有序，
// 数字按十进制文本，字符串按反转义并按 encoding/json 规则把非法 UTF-8 替换为 U+FFFD。

type officialJSONRawKind uint8

const (
	officialJSONRawKindInvalid officialJSONRawKind = iota
	officialJSONRawKindObject
	officialJSONRawKindArray
	officialJSONRawKindString
	officialJSONRawKindNumber
	officialJSONRawKindTrue
	officialJSONRawKindFalse
	officialJSONRawKindNull
)

// officialJSONRawMaxDepth 与 encoding/json 的嵌套深度上限一致。
const officialJSONRawMaxDepth = 10000

// officialJSONRawLookupThreshold 超过该成员数的对象用 map 做键查找，否则线性扫描。
const officialJSONRawLookupThreshold = 16

type officialJSONRawMember struct {
	key  string
	node int32
}

// officialJSONRawNode 是原始正文中一个 JSON 值的只读描述。start/end 是去掉外围空白
// 后的字节区间；对象的 members 按原始顺序保留全部成员（含重复键），数组的 items 按顺序
// 保留元素节点。
type officialJSONRawNode struct {
	kind     officialJSONRawKind
	slowPath bool // 字符串含转义或非法 UTF-8：比对必须走反转义路径
	start    int
	end      int
	hash     uint64
	members  []officialJSONRawMember
	items    []int32
	keys     []string         // 对象：去重后按首次出现排序的键，惰性计算
	lookup   map[string]int32 // 对象：键 → 最后一次出现的成员节点，成员较多时惰性建立
}

type officialJSONRawIndex struct {
	body         []byte
	nodes        []officialJSONRawNode
	root         int32
	byHash       map[uint64][]int32 // 复合值按结构摘要索引，保持前序顺序；被同名键遮蔽的子树不登记
	scratch      []byte
	seed         maphash.Seed
	validateOnly bool // 只做语法校验：不登记节点、不计算摘要
	skipDigest   bool // 只登记节点、不计算摘要：供索引直建对象树的解码器使用
}

var officialJSONRawHashSeed = maphash.MakeSeed()

// buildOfficialJSONRawIndex 对原始正文做一次严格的只读扫描。任何语法错误都返回 error，
// 调用方随即按“没有原始正文”处理，与旧实现在解码失败时的行为一致。
func buildOfficialJSONRawIndex(body []byte) (*officialJSONRawIndex, error) {
	if len(bytes.TrimSpace(body)) == 0 {
		return nil, errors.New("JSON 正文为空")
	}
	index := &officialJSONRawIndex{
		body:   body,
		nodes:  make([]officialJSONRawNode, 0, officialJSONRawCountValues(body)),
		byHash: make(map[uint64][]int32),
		seed:   officialJSONRawHashSeed,
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
	index.registerComposites(root)
	return index, nil
}

// officialJSONRawCountValues 预先数出正文中 JSON 值的个数，即扫描器将要登记的节点数（根值、每个对象成员的
// 值、每个数组元素；对象键不登记），供节点表一次预留（问题四 M2 清单外副本）。节点表从 64 起按 Go 切片约
// 1.25 倍反复扩容时，累计分配约为终值的五倍，16.8 MiB 测量形态每建一次索引多分配约 18 MiB。
//
// 只在引号外逐字节识别结构字符：对象成员按冒号计，数组元素按数组内逗号加首个元素计；字符串用
// bytes.IndexByte 跳到结束引号（按前导反斜杠个数识别转义），不逐字节处理。正文合法时结果精确；非法正文
// 的计数可能不准，只影响预留容量，扫描器照常按需扩容并报告同样的错误。
func officialJSONRawCountValues(body []byte) int {
	count := 1
	inArray := make([]bool, 0, 64)
	expectFirstItem := false
	for i := 0; i < len(body); i++ {
		c := body[i]
		if c == ' ' || c == '\t' || c == '\n' || c == '\r' {
			continue
		}
		if expectFirstItem {
			expectFirstItem = false
			if c != ']' {
				count++
			}
		}
		switch c {
		case '"':
			end := i + 1
			for {
				closing := bytes.IndexByte(body[end:], '"')
				if closing < 0 {
					return count
				}
				end += closing
				backslashes := 0
				for j := end - 1; j > i && body[j] == '\\'; j-- {
					backslashes++
				}
				if backslashes%2 == 0 {
					break
				}
				end++
			}
			i = end
		case '{':
			inArray = append(inArray, false)
		case '[':
			inArray = append(inArray, true)
			expectFirstItem = true
		case '}', ']':
			if len(inArray) > 0 {
				inArray = inArray[:len(inArray)-1]
			}
		case ':':
			count++
		case ',':
			if len(inArray) > 0 && inArray[len(inArray)-1] {
				count++
			}
		}
	}
	return count
}

// officialJSONValidateObject 用与索引扫描器同一套语法（与 encoding/json 对齐）严格校验
// 正文：必须是顶层对象、无尾随内容、转义/数字/嵌套合法。它不分配节点也不计算摘要，
// 供各阶段在“不解码就能确定无需改写”之前确认正文合法，从而与原先“解码即校验”的错误
// 行为保持一致。
func officialJSONValidateObject(body []byte) error {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 {
		return errors.New("JSON 正文为空")
	}
	if trimmed[0] != '{' {
		return errors.New("JSON 顶层必须是对象")
	}
	index := &officialJSONRawIndex{body: body, validateOnly: true}
	scanner := officialJSONRawScanner{index: index}
	if _, err := scanner.parseValue(0); err != nil {
		return err
	}
	scanner.skipSpace()
	if scanner.pos != len(body) {
		return errors.New("JSON 正文包含多个顶层值")
	}
	return nil
}

// officialJSONRawIndexForOriginal 是编码器入口使用的宽松构造：正文为空或非法时返回 nil。
func officialJSONRawIndexForOriginal(original []byte) *officialJSONRawIndex {
	index, err := buildOfficialJSONRawIndex(original)
	if err != nil {
		return nil
	}
	return index
}

// ---------------------------------------------------------------------------
// 扫描
// ---------------------------------------------------------------------------

type officialJSONRawScanner struct {
	index *officialJSONRawIndex
	pos   int
	// 字段名只在本次扫描内复用，保留独立副本，避免短键把大正文长期保活。
	keys map[string]string
}

func (s *officialJSONRawScanner) skipSpace() {
	body := s.index.body
	for s.pos < len(body) {
		switch body[s.pos] {
		case ' ', '\t', '\n', '\r':
			s.pos++
		default:
			return
		}
	}
}

func (s *officialJSONRawScanner) addNode(node officialJSONRawNode) int32 {
	if s.index.validateOnly {
		return -1
	}
	s.index.nodes = append(s.index.nodes, node)
	return int32(len(s.index.nodes) - 1)
}

func (s *officialJSONRawScanner) parseValue(depth int) (int32, error) {
	// depth 从 0 起计，总层数为 depth+1；与 encoding/json 一样在超过 10000 层时拒绝。
	if depth >= officialJSONRawMaxDepth {
		return -1, errors.New("JSON 嵌套过深")
	}
	s.skipSpace()
	body := s.index.body
	if s.pos >= len(body) {
		return -1, errors.New("JSON 意外结束")
	}
	switch body[s.pos] {
	case '{':
		return s.parseObject(depth)
	case '[':
		return s.parseArray(depth)
	case '"':
		return s.parseString()
	case 't':
		return s.parseLiteral("true", officialJSONRawKindTrue)
	case 'f':
		return s.parseLiteral("false", officialJSONRawKindFalse)
	case 'n':
		return s.parseLiteral("null", officialJSONRawKindNull)
	default:
		return s.parseNumber()
	}
}

func (s *officialJSONRawScanner) parseLiteral(literal string, kind officialJSONRawKind) (int32, error) {
	body := s.index.body
	if !bytes.HasPrefix(body[s.pos:], []byte(literal)) {
		return -1, errors.New("JSON 字面量非法")
	}
	start := s.pos
	s.pos += len(literal)
	if s.index.validateOnly {
		return -1, nil
	}
	return s.addNode(officialJSONRawNode{
		kind:  kind,
		start: start,
		end:   s.pos,
		hash:  s.index.hashBytes(officialJSONRawHashTag(kind), nil),
	}), nil
}

func (s *officialJSONRawScanner) parseNumber() (int32, error) {
	body := s.index.body
	start := s.pos
	pos := s.pos
	if pos < len(body) && body[pos] == '-' {
		pos++
	}
	if pos >= len(body) {
		return -1, errors.New("JSON 数字非法")
	}
	switch {
	case body[pos] == '0':
		pos++
	case body[pos] >= '1' && body[pos] <= '9':
		for pos < len(body) && body[pos] >= '0' && body[pos] <= '9' {
			pos++
		}
	default:
		return -1, errors.New("JSON 数字非法")
	}
	if pos < len(body) && body[pos] == '.' {
		pos++
		digits := 0
		for pos < len(body) && body[pos] >= '0' && body[pos] <= '9' {
			pos++
			digits++
		}
		if digits == 0 {
			return -1, errors.New("JSON 数字小数部分非法")
		}
	}
	if pos < len(body) && (body[pos] == 'e' || body[pos] == 'E') {
		pos++
		if pos < len(body) && (body[pos] == '+' || body[pos] == '-') {
			pos++
		}
		digits := 0
		for pos < len(body) && body[pos] >= '0' && body[pos] <= '9' {
			pos++
			digits++
		}
		if digits == 0 {
			return -1, errors.New("JSON 数字指数部分非法")
		}
	}
	s.pos = pos
	if s.index.validateOnly {
		return -1, nil
	}
	return s.addNode(officialJSONRawNode{
		kind:  officialJSONRawKindNumber,
		start: start,
		end:   pos,
		hash:  s.index.hashBytes('n', body[start:pos]),
	}), nil
}

// scanStringSegment 从 s.pos 处的引号开始定位字符串结束位置，返回引号内的字节段和
// 是否含转义；控制字符按 encoding/json 规则拒绝。
func (s *officialJSONRawScanner) scanStringSegment() (segment []byte, escaped bool, end int, err error) {
	body := s.index.body
	if s.pos >= len(body) || body[s.pos] != '"' {
		return nil, false, 0, errors.New("JSON 字符串必须以引号开始")
	}
	i := s.pos + 1
	for {
		if i >= len(body) {
			return nil, false, 0, errors.New("JSON 字符串未闭合")
		}
		c := body[i]
		if c == '"' {
			break
		}
		if c == '\\' {
			escaped = true
			i += 2
			continue
		}
		if c < 0x20 {
			return nil, false, 0, errors.New("JSON 字符串包含控制字符")
		}
		i++
	}
	return body[s.pos+1 : i], escaped, i + 1, nil
}

func (s *officialJSONRawScanner) parseString() (int32, error) {
	start := s.pos
	segment, escaped, end, err := s.scanStringSegment()
	if err != nil {
		return -1, err
	}
	slow := escaped || !utf8.Valid(segment)
	var hash uint64
	if !slow {
		if !s.index.validateOnly {
			hash = s.index.hashBytes('s', segment)
		}
	} else {
		unescaped, unescapeErr := officialJSONUnescape(s.index.scratch[:0], segment)
		s.index.scratch = unescaped[:0]
		if unescapeErr != nil {
			return -1, unescapeErr
		}
		if !s.index.validateOnly {
			hash = s.index.hashBytes('s', unescaped)
		}
	}
	s.pos = end
	if s.index.validateOnly {
		return -1, nil
	}
	return s.addNode(officialJSONRawNode{
		kind:     officialJSONRawKindString,
		slowPath: slow,
		start:    start,
		end:      end,
		hash:     hash,
	}), nil
}

// parseKey 解析对象键并返回反转义后的 Go 字符串；键不登记为节点。
func (s *officialJSONRawScanner) parseKey() (string, error) {
	segment, escaped, end, err := s.scanStringSegment()
	if err != nil {
		return "", err
	}
	s.pos = end
	if !escaped && utf8.Valid(segment) {
		return s.copyKey(segment), nil
	}
	unescaped, unescapeErr := officialJSONUnescape(s.index.scratch[:0], segment)
	s.index.scratch = unescaped[:0]
	if unescapeErr != nil {
		return "", unescapeErr
	}
	return s.copyKey(unescaped), nil
}

// copyKey 对重复短键只分配一次；任意用户键达到上限后仍照常复制，不建立无界缓存。
func (s *officialJSONRawScanner) copyKey(value []byte) string {
	if s.index.validateOnly {
		return ""
	}
	if len(value) > 128 || len(s.index.nodes) < officialJSONSharedDecodeMinNodes {
		return string(value)
	}
	if key, found := s.keys[string(value)]; found {
		return key
	}
	key := string(value)
	if len(s.keys) < 128 {
		if s.keys == nil {
			s.keys = make(map[string]string)
		}
		s.keys[key] = key
	}
	return key
}

func (s *officialJSONRawScanner) parseObject(depth int) (int32, error) {
	body := s.index.body
	start := s.pos
	s.pos++ // '{'
	// 小对象先用栈上空间收集成员，最终仅复制一次；长历史中的消息项不再经历多轮扩容。
	var localMembers [8]officialJSONRawMember
	var members []officialJSONRawMember
	memberCount := 0
	s.skipSpace()
	if s.pos < len(body) && body[s.pos] == '}' {
		s.pos++
		if s.index.validateOnly {
			return -1, nil
		}
		return s.addNode(officialJSONRawNode{
			kind:  officialJSONRawKindObject,
			start: start,
			end:   s.pos,
			hash:  s.index.hashObjectMembers(nil),
		}), nil
	}
	for {
		s.skipSpace()
		key, err := s.parseKey()
		if err != nil {
			return -1, err
		}
		s.skipSpace()
		if s.pos >= len(body) || body[s.pos] != ':' {
			return -1, errors.New("JSON 对象缺少冒号")
		}
		s.pos++
		child, err := s.parseValue(depth + 1)
		if err != nil {
			return -1, err
		}
		if !s.index.validateOnly {
			member := officialJSONRawMember{key: key, node: child}
			if memberCount < len(localMembers) {
				localMembers[memberCount] = member
			} else {
				if members == nil {
					members = make([]officialJSONRawMember, len(localMembers), 2*len(localMembers))
					copy(members, localMembers[:])
				}
				members = append(members, member)
			}
			memberCount++
		}
		s.skipSpace()
		if s.pos >= len(body) {
			return -1, errors.New("JSON 对象未闭合")
		}
		switch body[s.pos] {
		case ',':
			s.pos++
		case '}':
			s.pos++
			if s.index.validateOnly {
				return -1, nil
			}
			if members == nil {
				members = append([]officialJSONRawMember(nil), localMembers[:memberCount]...)
			}
			return s.addNode(officialJSONRawNode{
				kind:    officialJSONRawKindObject,
				start:   start,
				end:     s.pos,
				hash:    s.index.hashObjectMembers(members),
				members: members,
			}), nil
		default:
			return -1, errors.New("JSON 对象成员分隔符非法")
		}
	}
}

func (s *officialJSONRawScanner) parseArray(depth int) (int32, error) {
	body := s.index.body
	start := s.pos
	s.pos++ // '['
	var items []int32
	s.skipSpace()
	if s.pos < len(body) && body[s.pos] == ']' {
		s.pos++
		if s.index.validateOnly {
			return -1, nil
		}
		return s.addNode(officialJSONRawNode{
			kind:  officialJSONRawKindArray,
			start: start,
			end:   s.pos,
			hash:  s.index.hashArrayItems(nil),
		}), nil
	}
	for {
		child, err := s.parseValue(depth + 1)
		if err != nil {
			return -1, err
		}
		if !s.index.validateOnly {
			items = append(items, child)
		}
		s.skipSpace()
		if s.pos >= len(body) {
			return -1, errors.New("JSON 数组未闭合")
		}
		switch body[s.pos] {
		case ',':
			s.pos++
		case ']':
			s.pos++
			if s.index.validateOnly {
				return -1, nil
			}
			return s.addNode(officialJSONRawNode{
				kind:  officialJSONRawKindArray,
				start: start,
				end:   s.pos,
				hash:  s.index.hashArrayItems(items),
				items: items,
			}), nil
		default:
			return -1, errors.New("JSON 数组元素分隔符非法")
		}
	}
}

// registerComposites 按前序把复合值登记到摘要索引。对象只沿去重后的键递归，且同名键
// 取最后一次出现的值，与旧实现的查找池遍历方式一致：被遮蔽的重复成员及其子树不登记。
func (index *officialJSONRawIndex) registerComposites(node int32) {
	switch index.nodes[node].kind {
	case officialJSONRawKindObject:
		hash := index.nodes[node].hash
		index.byHash[hash] = append(index.byHash[hash], node)
		for _, key := range index.uniqueKeys(node) {
			index.registerComposites(index.memberNode(node, key))
		}
	case officialJSONRawKindArray:
		hash := index.nodes[node].hash
		index.byHash[hash] = append(index.byHash[hash], node)
		for _, item := range index.nodes[node].items {
			index.registerComposites(item)
		}
	}
}

// ---------------------------------------------------------------------------
// 对象成员访问
// ---------------------------------------------------------------------------

// uniqueKeys 返回对象去重后的键，顺序为首次出现顺序；与 decodeOrderedRawJSONObject
// 的 keys 语义一致。
func (index *officialJSONRawIndex) uniqueKeys(node int32) []string {
	n := &index.nodes[node]
	if n.kind != officialJSONRawKindObject {
		return nil
	}
	if n.keys != nil || len(n.members) == 0 {
		return n.keys
	}
	keys := make([]string, 0, len(n.members))
	if len(n.members) <= officialJSONRawLookupThreshold {
		for i, member := range n.members {
			duplicate := false
			for j := 0; j < i; j++ {
				if n.members[j].key == member.key {
					duplicate = true
					break
				}
			}
			if !duplicate {
				keys = append(keys, member.key)
			}
		}
	} else {
		seen := make(map[string]struct{}, len(n.members))
		for _, member := range n.members {
			if _, exists := seen[member.key]; exists {
				continue
			}
			seen[member.key] = struct{}{}
			keys = append(keys, member.key)
		}
	}
	n.keys = keys
	return keys
}

// memberNode 返回对象中某键最后一次出现的成员节点；不存在返回 -1。同名键取最后一次
// 与 encoding/json 解码到 map 的语义一致。
func (index *officialJSONRawIndex) memberNode(node int32, key string) int32 {
	n := &index.nodes[node]
	if n.kind != officialJSONRawKindObject {
		return -1
	}
	if len(n.members) > officialJSONRawLookupThreshold {
		if n.lookup == nil {
			lookup := make(map[string]int32, len(n.members))
			for _, member := range n.members {
				lookup[member.key] = member.node
			}
			n.lookup = lookup
		}
		if child, exists := n.lookup[key]; exists {
			return child
		}
		return -1
	}
	for i := len(n.members) - 1; i >= 0; i-- {
		if n.members[i].key == key {
			return n.members[i].node
		}
	}
	return -1
}

func (index *officialJSONRawIndex) raw(node int32) []byte {
	n := &index.nodes[node]
	return index.body[n.start:n.end]
}

// ---------------------------------------------------------------------------
// 结构摘要
// ---------------------------------------------------------------------------

func officialJSONRawHashTag(kind officialJSONRawKind) byte {
	switch kind {
	case officialJSONRawKindTrue:
		return 't'
	case officialJSONRawKindFalse:
		return 'f'
	default:
		return 'z'
	}
}

func (index *officialJSONRawIndex) hashBytes(tag byte, data []byte) uint64 {
	if index.skipDigest {
		return 0
	}
	var h maphash.Hash
	h.SetSeed(index.seed)
	_ = h.WriteByte(tag)
	_, _ = h.Write(data)
	return h.Sum64()
}

func (index *officialJSONRawIndex) hashString(tag byte, data string) uint64 {
	if index.skipDigest {
		return 0
	}
	var h maphash.Hash
	h.SetSeed(index.seed)
	_ = h.WriteByte(tag)
	_, _ = h.WriteString(data)
	return h.Sum64()
}

type officialJSONHashPair struct {
	key  string
	hash uint64
}

// hashObjectMembers 对成员做“同名键取最后一次、按键排序”的摘要，使摘要与键序无关。
func (index *officialJSONRawIndex) hashObjectMembers(members []officialJSONRawMember) uint64 {
	if index.skipDigest {
		return 0
	}
	// 绝大多数消息项只有几个字段，栈上缓冲避免每个对象分配排序切片。
	var local [16]officialJSONHashPair
	pairs := local[:0]
	if len(members) > len(local) {
		pairs = make([]officialJSONHashPair, 0, len(members))
	}
	for _, member := range members {
		pairs = append(pairs, officialJSONHashPair{key: member.key, hash: index.nodes[member.node].hash})
	}
	return index.hashPairs(pairs)
}

// hashPairs 要求调用方传入可能含重复键的键值摘要对；稳定排序后同键取最后一项。
func (index *officialJSONRawIndex) hashPairs(pairs []officialJSONHashPair) uint64 {
	slices.SortStableFunc(pairs, func(a, b officialJSONHashPair) int { return strings.Compare(a.key, b.key) })
	var h maphash.Hash
	h.SetSeed(index.seed)
	_ = h.WriteByte('{')
	var buf [8]byte
	for i, pair := range pairs {
		if i+1 < len(pairs) && pairs[i+1].key == pair.key {
			continue
		}
		binary.LittleEndian.PutUint64(buf[:], index.hashString('k', pair.key))
		_, _ = h.Write(buf[:])
		binary.LittleEndian.PutUint64(buf[:], pair.hash)
		_, _ = h.Write(buf[:])
	}
	return h.Sum64()
}

func (index *officialJSONRawIndex) hashArrayItems(items []int32) uint64 {
	if index.skipDigest {
		return 0
	}
	var h maphash.Hash
	h.SetSeed(index.seed)
	_ = h.WriteByte('[')
	var buf [8]byte
	for _, item := range items {
		binary.LittleEndian.PutUint64(buf[:], index.nodes[item].hash)
		_, _ = h.Write(buf[:])
	}
	return h.Sum64()
}

func (index *officialJSONRawIndex) hashUint64s(tag byte, values []uint64) uint64 {
	var h maphash.Hash
	h.SetSeed(index.seed)
	_ = h.WriteByte(tag)
	var buf [8]byte
	for _, value := range values {
		binary.LittleEndian.PutUint64(buf[:], value)
		_, _ = h.Write(buf[:])
	}
	return h.Sum64()
}

// hashValue 对 Go 值计算与原始正文同一方案的结构摘要。不可规范化编码的类型返回 false，
// 调用方随即退回旧实现的解码比对路径。
func (index *officialJSONRawIndex) hashValue(value any) (uint64, bool) {
	switch typed := value.(type) {
	case nil:
		return index.hashBytes('z', nil), true
	case bool:
		if typed {
			return index.hashBytes('t', nil), true
		}
		return index.hashBytes('f', nil), true
	case string:
		if utf8.ValidString(typed) {
			return index.hashString('s', typed), true
		}
		return index.hashBytes('s', officialJSONCoerceUTF8(nil, typed)), true
	case json.Number:
		return index.hashString('n', string(typed)), true
	case map[string]any:
		var local [16]officialJSONHashPair
		pairs := local[:0]
		if len(typed) > len(local) {
			pairs = make([]officialJSONHashPair, 0, len(typed))
		}
		for key, child := range typed {
			childHash, ok := index.hashValue(child)
			if !ok {
				return 0, false
			}
			pairs = append(pairs, officialJSONHashPair{key: key, hash: childHash})
		}
		return index.hashPairs(pairs), true
	case []any:
		var h maphash.Hash
		h.SetSeed(index.seed)
		_ = h.WriteByte('[')
		var buf [8]byte
		for _, child := range typed {
			childHash, ok := index.hashValue(child)
			if !ok {
				return 0, false
			}
			binary.LittleEndian.PutUint64(buf[:], childHash)
			_, _ = h.Write(buf[:])
		}
		return h.Sum64(), true
	case int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64, float32, float64:
		encoded, err := json.Marshal(typed)
		if err != nil {
			return 0, false
		}
		return index.hashBytes('n', encoded), true
	default:
		return 0, false
	}
}

// ---------------------------------------------------------------------------
// 精确相等（规范化 JSON 相等语义，零分配）
// ---------------------------------------------------------------------------

// equals 判断 Go 值与原始节点是否规范化相等。摘要只用于剪枝，这里始终做精确比对。
func (index *officialJSONRawIndex) equals(node int32, value any) bool {
	n := &index.nodes[node]
	switch typed := value.(type) {
	case nil:
		return n.kind == officialJSONRawKindNull
	case bool:
		if typed {
			return n.kind == officialJSONRawKindTrue
		}
		return n.kind == officialJSONRawKindFalse
	case string:
		return n.kind == officialJSONRawKindString && index.stringEquals(n, typed)
	case json.Number:
		return n.kind == officialJSONRawKindNumber && string(index.body[n.start:n.end]) == string(typed)
	case map[string]any:
		if n.kind != officialJSONRawKindObject || len(index.uniqueKeys(node)) != len(typed) {
			return false
		}
		for key, child := range typed {
			member := index.memberNode(node, key)
			if member < 0 || !index.equals(member, child) {
				return false
			}
		}
		return true
	case []any:
		if n.kind != officialJSONRawKindArray || len(n.items) != len(typed) {
			return false
		}
		for i, child := range typed {
			if !index.equals(n.items[i], child) {
				return false
			}
		}
		return true
	case int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64, float32, float64:
		if n.kind != officialJSONRawKindNumber {
			return false
		}
		encoded, err := json.Marshal(typed)
		return err == nil && bytes.Equal(encoded, index.body[n.start:n.end])
	default:
		// json.RawMessage、结构体等罕见类型沿用旧实现的解码比对，保证语义逐字一致。
		decoded, err := decodeOfficialJSONValueUseNumber(index.body[n.start:n.end])
		return err == nil && reflectOfficialJSONEqual(value, decoded)
	}
}

func (index *officialJSONRawIndex) stringEquals(n *officialJSONRawNode, value string) bool {
	segment := index.body[n.start+1 : n.end-1]
	if !n.slowPath {
		if utf8.ValidString(value) {
			return string(segment) == value
		}
		coerced := officialJSONCoerceUTF8(index.scratch[:0], value)
		index.scratch = coerced[:0]
		return string(coerced) == string(segment)
	}
	unescaped, err := officialJSONUnescape(index.scratch[:0], segment)
	if err != nil {
		index.scratch = unescaped[:0]
		return false
	}
	equal := false
	if utf8.ValidString(value) {
		equal = string(unescaped) == value
	} else {
		equal = string(unescaped) == string(officialJSONCoerceUTF8(nil, value))
	}
	index.scratch = unescaped[:0]
	return equal
}

// ---------------------------------------------------------------------------
// 反转义与 UTF-8 规范化（与 encoding/json 逐条对齐）
// ---------------------------------------------------------------------------

func officialJSONHexValue(c byte) int {
	switch {
	case c >= '0' && c <= '9':
		return int(c - '0')
	case c >= 'a' && c <= 'f':
		return int(c-'a') + 10
	case c >= 'A' && c <= 'F':
		return int(c-'A') + 10
	default:
		return -1
	}
}

// officialJSONGetU4 解析形如 \uXXXX 的转义，与 encoding/json 的 getu4 一致；非法返回 -1。
func officialJSONGetU4(s []byte) rune {
	if len(s) < 6 || s[0] != '\\' || s[1] != 'u' {
		return -1
	}
	var r rune
	for _, c := range s[2:6] {
		v := officialJSONHexValue(c)
		if v < 0 {
			return -1
		}
		r = r*16 + rune(v)
	}
	return r
}

// officialJSONUnescape 把引号内的字节段反转义到 dst（复用其容量），规则与 encoding/json
// 的 unquoteBytes 逐条一致：非法转义报错；非法 UTF-8 逐字节替换为 U+FFFD；孤立代理项
// 替换为 U+FFFD。
func officialJSONUnescape(dst []byte, segment []byte) ([]byte, error) {
	dst = dst[:0]
	if cap(dst) < len(segment) {
		dst = make([]byte, 0, len(segment)+16)
	}
	for r := 0; r < len(segment); {
		c := segment[r]
		switch {
		case c == '\\':
			r++
			if r >= len(segment) {
				return dst, errors.New("JSON 字符串转义不完整")
			}
			switch segment[r] {
			case '"', '\\', '/':
				dst = append(dst, segment[r])
				r++
			case 'b':
				dst = append(dst, '\b')
				r++
			case 'f':
				dst = append(dst, '\f')
				r++
			case 'n':
				dst = append(dst, '\n')
				r++
			case 'r':
				dst = append(dst, '\r')
				r++
			case 't':
				dst = append(dst, '\t')
				r++
			case 'u':
				rr := officialJSONGetU4(segment[r-1:])
				if rr < 0 {
					return dst, errors.New("JSON \\u 转义非法")
				}
				r += 5
				if utf16.IsSurrogate(rr) {
					rr1 := officialJSONGetU4(segment[r:])
					if dec := utf16.DecodeRune(rr, rr1); dec != utf8.RuneError {
						r += 6
						dst = utf8.AppendRune(dst, dec)
						break
					}
					rr = utf8.RuneError
				}
				dst = utf8.AppendRune(dst, rr)
			default:
				return dst, errors.New("JSON 转义字符非法")
			}
		case c == '"', c < ' ':
			return dst, errors.New("JSON 字符串包含非法字符")
		case c < utf8.RuneSelf:
			dst = append(dst, c)
			r++
		default:
			rr, size := utf8.DecodeRune(segment[r:])
			r += size
			dst = utf8.AppendRune(dst, rr)
		}
	}
	return dst, nil
}

// officialJSONCoerceUTF8 按 encoding/json 编码字符串时的规则，把非法 UTF-8 逐字节替换为
// U+FFFD，用于让 Go 侧字符串与解码后的原文按同一规范比较。
func officialJSONCoerceUTF8(dst []byte, value string) []byte {
	dst = dst[:0]
	for i := 0; i < len(value); {
		c := value[i]
		if c < utf8.RuneSelf {
			dst = append(dst, c)
			i++
			continue
		}
		rr, size := utf8.DecodeRuneInString(value[i:])
		i += size
		dst = utf8.AppendRune(dst, rr)
	}
	return dst
}

// ---------------------------------------------------------------------------
// 拼接编码
// ---------------------------------------------------------------------------

// lookupComposite 在全文中按内容查找与 value 相等的复合值，返回首个前序出现的节点。
func (index *officialJSONRawIndex) lookupComposite(value any) int32 {
	switch value.(type) {
	case map[string]any, []any:
	default:
		return -1
	}
	hash, ok := index.hashValue(value)
	if !ok {
		return -1
	}
	for _, candidate := range index.byHash[hash] {
		if index.equals(candidate, value) {
			return candidate
		}
	}
	return -1
}

// matchArrayItem 在原始数组元素中按内容找出首个未被占用且相等的元素下标；找不到返回 -1。
func (index *officialJSONRawIndex) matchArrayItem(item any, originalItems []int32, used []bool) int {
	hash, hashable := index.hashValue(item)
	for i, candidate := range originalItems {
		if used[i] {
			continue
		}
		if hashable && index.nodes[candidate].hash != hash {
			continue
		}
		if index.equals(candidate, item) {
			return i
		}
	}
	return -1
}

// officialJSONAppendValue 把 value 编码到 out：同位置原始节点相等则拼接原始区间，否则按
// 内容在全文查找，再退到对象/数组的局部重编码，最后才规范化编码。index 为 nil 表示没有
// 可复用的原始正文；node 为 -1 表示当前位置没有原始节点。
func officialJSONAppendValue(index *officialJSONRawIndex, out []byte, value any, node int32) ([]byte, error) {
	if index != nil {
		if node >= 0 && index.equals(node, value) {
			return append(out, index.raw(node)...), nil
		}
		if pooled := index.lookupComposite(value); pooled >= 0 {
			return append(out, index.raw(pooled)...), nil
		}
	}
	switch typed := value.(type) {
	case map[string]any:
		return officialJSONAppendObject(index, out, typed, node)
	case []any:
		return officialJSONAppendArray(index, out, typed, node)
	case json.RawMessage:
		if json.Valid(typed) {
			return append(out, typed...), nil
		}
	}
	encoded, err := marshalOpenAIUpstreamJSON(value)
	if err != nil {
		return nil, err
	}
	return append(out, encoded...), nil
}

// officialJSONAppendObject 重编码被改动的对象：保留原始成员顺序，新增键按字典序追加，
// 每个成员值继续尝试拼接原始区间。
func officialJSONAppendObject(index *officialJSONRawIndex, out []byte, payload map[string]any, node int32) ([]byte, error) {
	var originalKeys []string
	if index != nil && node >= 0 {
		originalKeys = index.uniqueKeys(node)
	}
	keys := make([]string, 0, len(payload))
	seen := make(map[string]struct{}, len(payload))
	for _, key := range originalKeys {
		if _, exists := payload[key]; !exists {
			continue
		}
		keys = append(keys, key)
		seen[key] = struct{}{}
	}
	additional := make([]string, 0, len(payload)-len(keys))
	for key := range payload {
		if _, exists := seen[key]; !exists {
			additional = append(additional, key)
		}
	}
	sort.Strings(additional)
	keys = append(keys, additional...)

	out = append(out, '{')
	for i, key := range keys {
		if i > 0 {
			out = append(out, ',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		out = append(out, encodedKey...)
		out = append(out, ':')
		child := int32(-1)
		if index != nil && node >= 0 {
			child = index.memberNode(node, key)
		}
		out, err = officialJSONAppendValue(index, out, payload[key], child)
		if err != nil {
			return nil, err
		}
	}
	return append(out, '}'), nil
}

// officialJSONAppendArray 重编码被改动的数组：每个元素先按内容匹配原始元素，匹配不到
// 才按位置对应，随后继续尝试拼接。
func officialJSONAppendArray(index *officialJSONRawIndex, out []byte, items []any, node int32) ([]byte, error) {
	var originalItems []int32
	if index != nil && node >= 0 && index.nodes[node].kind == officialJSONRawKindArray {
		originalItems = index.nodes[node].items
	}
	used := make([]bool, len(originalItems))
	out = append(out, '[')
	for i, item := range items {
		if i > 0 {
			out = append(out, ',')
		}
		match := -1
		if index != nil {
			match = index.matchArrayItem(item, originalItems, used)
		}
		if match < 0 && i < len(originalItems) && !used[i] {
			match = i
		}
		child := int32(-1)
		if match >= 0 {
			used[match] = true
			child = originalItems[match]
		}
		var err error
		out, err = officialJSONAppendValue(index, out, item, child)
		if err != nil {
			return nil, err
		}
	}
	return append(out, ']'), nil
}
