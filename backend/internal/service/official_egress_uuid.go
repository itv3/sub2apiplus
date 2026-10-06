package service

import (
	"container/list"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"hash"
	"io"
	"strconv"
	"sync"
	"time"
	"unsafe"

	"github.com/google/uuid"
)

const (
	// officialUUIDV7CacheTTL 是稳定 UUID 的复用窗口。官方 Codex 客户端在整段对话内
	// 保持同一个 session_id，与账号粘连 TTL（默认 1 小时）无关；把它缩短到粘连 TTL
	// 会让空闲超过 1 小时的对话换一套身份，因此保持 24 小时。键固定为 32 字节后，
	// 该 TTL 不再决定内存上限，上限只由 officialUUIDV7CacheMaxEntries 决定。
	officialUUIDV7CacheTTL = 24 * time.Hour
	// officialUUIDV7CacheMaxEntries 是条目硬上限。每条目常驻约
	// officialUUIDV7CacheEntryBytes，满表约 12 MiB，与请求内容体积无关。
	officialUUIDV7CacheMaxEntries = 65536
	// officialUUIDV7CacheSweepPerInsert 限制每次写入顺带清理的过期条目数，保证业务
	// 请求在全局锁内的工作量恒为 O(1)，不再像旧实现那样满表时扫描全部条目。
	officialUUIDV7CacheSweepPerInsert = 8
	// officialUUIDV7CacheEntryBytes 是单条目常驻内存估算：32 字节键、36 字节 UUID
	// 文本、时间戳、链表节点与 map 桶开销。用于指标，不参与淘汰判断。
	officialUUIDV7CacheEntryBytes = 192
)

// 官方出站身份种子的用途域。域名作为种子的第一个字段写入，保证 session、child、
// turn 三类键即使其余字段相同也互不碰撞。
const (
	officialUUIDV7DomainSession = "openai-official-egress-session"
	officialUUIDV7DomainChild   = "openai-official-egress-child"
	officialUUIDV7DomainTurn    = "openai-official-egress-turn"
)

// officialUUIDV7CacheKey 是稳定 UUID 缓存唯一接受的键类型：固定 32 字节的 SHA-256
// 摘要。用户消息全文、历史片段 JSON、整帧十六进制等请求内容曾经直接作为 map 键
// 驻留 24 小时，使进程内存随转发内容量近似 1:1 增长（docs/bug.md 问题五）。改为
// 固定长度键后，调用方在类型层面就无法再把正文塞进缓存。
type officialUUIDV7CacheKey [sha256.Size]byte

// officialContentDigest 是一段请求内容（用户消息文本、历史片段或整帧 JSON）的固定
// 长度摘要。它只是种子的一个字段，不能直接当缓存键使用。
type officialContentDigest [sha256.Size]byte

// officialUUIDV7Seed 以“用途域 + 带长度前缀的字段序列”构造缓存键。字段边界由 8 字节
// 长度前缀表达，不再用分隔符拼接字符串，因此 "a"+"bc" 与 "ab"+"c" 不会得到同一个键。
// 摘要在调用方线程内完成，缓存锁内只做 O(1) 查找与写入。
type officialUUIDV7Seed struct {
	hash hash.Hash
}

func newOfficialUUIDV7Seed(domain string) *officialUUIDV7Seed {
	seed := &officialUUIDV7Seed{hash: sha256.New()}
	seed.WriteString(domain)
	return seed
}

func (s *officialUUIDV7Seed) writeLength(length int) {
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], uint64(length))
	_, _ = s.hash.Write(buf[:])
}

// WriteString 写入一个有界的小字段：账号范围、显式会话锚点、身份种类、UUID 等。
// 它不限制长度，调用方必须保证不传入请求正文；正文只能经 WriteDigest 进入种子。
func (s *officialUUIDV7Seed) WriteString(value string) *officialUUIDV7Seed {
	s.writeLength(len(value))
	_, _ = io.WriteString(s.hash, value)
	return s
}

// WriteInt 以十进制文本写入整数字段（例如 input 下标）。
func (s *officialUUIDV7Seed) WriteInt(value int) *officialUUIDV7Seed {
	return s.WriteString(strconv.Itoa(value))
}

// WriteDigest 写入一段内容的固定长度摘要。
func (s *officialUUIDV7Seed) WriteDigest(digest officialContentDigest) *officialUUIDV7Seed {
	s.writeLength(len(digest))
	_, _ = s.hash.Write(digest[:])
	return s
}

// Key 结束种子构造并返回缓存键。
func (s *officialUUIDV7Seed) Key() officialUUIDV7CacheKey {
	var key officialUUIDV7CacheKey
	s.hash.Sum(key[:0])
	return key
}

// officialContentDigester 把内容分块写入 SHA-256。它实现 io.Writer，既能接收字符串
// 片段，也能直接作为 json.Encoder 的输出端，从而不必先把内容拼成完整字符串。
type officialContentDigester struct {
	hash hash.Hash
}

func newOfficialContentDigester() officialContentDigester {
	return officialContentDigester{hash: sha256.New()}
}

func (d *officialContentDigester) ensure() {
	if d.hash == nil {
		d.hash = sha256.New()
	}
}

func (d *officialContentDigester) Write(p []byte) (int, error) {
	d.ensure()
	return d.hash.Write(p)
}

// WriteString 以零拷贝只读视图把字符串写入哈希。io.WriteString 对 sha256 摘要器会退化
// 成 []byte(s) 复制，长上下文的每条用户文本都会被再复制一份；hash.Hash.Write 从不修改
// 输入，因此这里直接借用字符串底层字节。
func (d *officialContentDigester) WriteString(value string) {
	d.ensure()
	if value == "" {
		return
	}
	_, _ = d.hash.Write(unsafe.Slice(unsafe.StringData(value), len(value)))
}

func (d *officialContentDigester) Sum() officialContentDigest {
	d.ensure()
	var digest officialContentDigest
	d.hash.Sum(digest[:0])
	return digest
}

// digestOfficialJSONValue 把确定性 JSON 编码直接流入 SHA-256。常见 JSON 树逐项
// 写入，不在编码器内部积累整份正文；其他类型保留原编码器兼容行为。
func digestOfficialJSONValue(value any) (officialContentDigest, error) {
	digester := newOfficialContentDigester()
	if writeOfficialJSONDigestValue(&digester, value, 0) {
		digester.WriteString("\n")
		return digester.Sum(), nil
	}
	digester = newOfficialContentDigester()
	encoder := json.NewEncoder(&digester)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return officialContentDigest{}, err
	}
	return digester.Sum(), nil
}

// officialUUIDV7CacheStats 是缓存的运行时指标快照。
type officialUUIDV7CacheStats struct {
	Entries         int
	Hits            uint64
	Misses          uint64
	EvictedExpired  uint64
	EvictedCapacity uint64
	EstimatedBytes  int
}

type officialUUIDV7CacheEntry struct {
	key      officialUUIDV7CacheKey
	value    string
	lastUsed time.Time
}

// officialUUIDV7LRUCache 是键固定为 32 字节的有界 TTL/LRU 缓存。链表 Front 为最近
// 使用、Back 为最久未使用；每次命中都会把条目移到 Front 并刷新 lastUsed，所以过期
// 条目总是连续位于 Back 端，插入时从 Back 端有界清理即可，不需要扫描整个 map。
type officialUUIDV7LRUCache struct {
	mu         sync.Mutex
	ttl        time.Duration
	maxEntries int
	now        func() time.Time
	entries    map[officialUUIDV7CacheKey]*list.Element
	order      *list.List

	hits            uint64
	misses          uint64
	evictedExpired  uint64
	evictedCapacity uint64
}

func newOfficialUUIDV7LRUCache(
	ttl time.Duration,
	maxEntries int,
	now func() time.Time,
) *officialUUIDV7LRUCache {
	if maxEntries < 1 {
		maxEntries = 1
	}
	if now == nil {
		now = time.Now
	}
	return &officialUUIDV7LRUCache{
		ttl:        ttl,
		maxEntries: maxEntries,
		now:        now,
		entries:    make(map[officialUUIDV7CacheKey]*list.Element),
		order:      list.New(),
	}
}

// resolve 返回键对应的稳定 UUIDv7；未命中或已过期时生成新值并登记。
func (c *officialUUIDV7LRUCache) resolve(key officialUUIDV7CacheKey) string {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	if element, exists := c.entries[key]; exists {
		entry, valid := element.Value.(*officialUUIDV7CacheEntry)
		if !valid {
			// map 与链表理论上由同一把锁维护；若测试或未来改动破坏了
			// 这个不变量，先移除损坏节点，再按未命中路径恢复，不能让
			// 诊断缓存把整个请求 goroutine 置于 panic 状态。
			c.removeLocked(element)
		} else if now.Sub(entry.lastUsed) <= c.ttl {
			entry.lastUsed = now
			c.order.MoveToFront(element)
			c.hits++
			return entry.value
		}
		c.removeLocked(element)
		c.evictedExpired++
	}
	c.misses++
	c.sweepExpiredLocked(now, officialUUIDV7CacheSweepPerInsert)
	for c.order.Len() >= c.maxEntries {
		c.removeLocked(c.order.Back())
		c.evictedCapacity++
	}
	value := newOfficialUUIDV7()
	entry := &officialUUIDV7CacheEntry{key: key, value: value, lastUsed: now}
	c.entries[key] = c.order.PushFront(entry)
	return value
}

func (c *officialUUIDV7LRUCache) removeLocked(element *list.Element) {
	if element == nil {
		return
	}
	entry, valid := element.Value.(*officialUUIDV7CacheEntry)
	if valid {
		delete(c.entries, entry.key)
	}
	c.order.Remove(element)
}

// sweepExpiredLocked 从最久未使用端清理最多 limit 个过期条目。
func (c *officialUUIDV7LRUCache) sweepExpiredLocked(now time.Time, limit int) {
	for removed := 0; removed < limit; removed++ {
		element := c.order.Back()
		if element == nil {
			return
		}
		entry, valid := element.Value.(*officialUUIDV7CacheEntry)
		if !valid {
			c.removeLocked(element)
			continue
		}
		if now.Sub(entry.lastUsed) <= c.ttl {
			return
		}
		c.removeLocked(element)
		c.evictedExpired++
	}
}

func (c *officialUUIDV7LRUCache) stats() officialUUIDV7CacheStats {
	c.mu.Lock()
	defer c.mu.Unlock()
	return officialUUIDV7CacheStats{
		Entries:         c.order.Len(),
		Hits:            c.hits,
		Misses:          c.misses,
		EvictedExpired:  c.evictedExpired,
		EvictedCapacity: c.evictedCapacity,
		EstimatedBytes:  c.order.Len() * officialUUIDV7CacheEntryBytes,
	}
}

func newOfficialUUIDV7() string {
	value, err := uuid.NewV7()
	if err != nil {
		value = uuid.New()
	}
	return value.String()
}

var officialUUIDV7Cache = newOfficialUUIDV7LRUCache(
	officialUUIDV7CacheTTL,
	officialUUIDV7CacheMaxEntries,
	time.Now,
)

// generateOfficialStableUUIDV7 为官方 Codex 的 session/turn 字段生成真实时间有序
// UUIDv7，并在进程内按种子稳定复用。它只接受固定长度的摘要键：调用方必须先用
// officialUUIDV7Seed 把锚点摘要化，用户正文在任何时刻都不能成为缓存键。仍使用真实
// UUIDv7 而不是把哈希位伪装成 v7 时间戳，避免出站身份与官方客户端形态不一致。
func generateOfficialStableUUIDV7(key officialUUIDV7CacheKey) string {
	return officialUUIDV7Cache.resolve(key)
}
