package service

import (
	"container/list"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"hash"
	"io"
	"strconv"
	"strings"
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
	return c.resolveWith(key, newOfficialUUIDV7)
}

// resolveWith 同 resolve，只是未命中时用 generate 生成新值。回合 id 需要官方客户端同款的位布局，
// 与 session、thread 等 id 的生成方式不同，所以由调用方指定。
func (c *officialUUIDV7LRUCache) resolveWith(key officialUUIDV7CacheKey, generate func() string) string {
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
	value := generate()
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

// newOfficialUUIDV7 生成 session、thread、item id 等非回合 id。位布局与官方 Uuid::now_v7() 逐位一致
// （见文件末尾“官方 Codex 回合 id”一节），与回合 id 共用同一个单调上下文——官方进程内所有 UUIDv7 也共用一个。
// 内嵌毫秒回拨 officialCodexIDLeadMs：官方的会话（线程）id 总比它的第一个回合 id 早几十毫秒生成
// （实采差 26～46 毫秒），回拨后新会话里“会话 id 不晚于回合 id”的先后关系与官方一致，也避免回合 id
// 因共用单调上下文被拉到与会话 id 同一毫秒、使回合开始时间晚于生成时刻。
// 以前用 google/uuid 的 NewV7，rand_a 是亚毫秒时间，第 2、3 位为 0 的只有 25%，官方恒为 100%。
// 各版本画像无需门控：本地各版官方实采（含当前与上一版）里的 UUIDv7 全部满足同一布局，且这些 id 本来就是
// 记录时随机的值，不存在按固定字节重放的封存证据。
func newOfficialUUIDV7() string {
	return officialCodexTurnUUIDContext.generate(officialCodexTurnClock().UnixMilli() - officialCodexIDLeadMs)
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

// ---------------------------------------------------------------------------
// 官方 Codex 回合 id（turn_id）与回合开始时间（turn_started_at_unix_ms）
//
// 官方客户端的 turn_id 是提交号 sub_id，由 Uuid::now_v7() 生成（Rust uuid 库，官方 Cargo.lock 锁定的版本）。该函数用进程内共享的
// 单调上下文：每个新毫秒用随机的 41 位数重置 42 位计数器，同毫秒内计数器加一，毫秒回退时沿用上次的
// 毫秒。位布局按 RFC 9562 的版本 7 排布，并带有该库编码函数的一个实现细节，我们逐位照做：
//
//	bit   0..47   毫秒时间戳（大端）
//	bit  48..51   版本号 7
//	bit  52..63   rand_a（12 位）= 计数器第 37..30 位，其后两位恒为 0，再接计数器第 29、28 位
//	                （库把计数器最高 4 位丢进了被覆盖的字节，且在第 2、3 位处插入了变体占位的 0）
//	bit  64..65   变体 0b10
//	bit  66..127  rand_b（62 位）= 计数器第 25..0 位（26 位）+ 36 位随机数（计数器第 27、26 位被变体覆盖）
//
// 因此官方 id 的 rand_a 的第 2、3 位恒为 0（本地实采的 287 个 UUIDv7 全部满足）。
// 网关其他 id（session、thread 等）仍用 google/uuid 的 NewV7（rand_a 是亚毫秒时间），那是另一回事。
//
// turn_started_at_unix_ms 是任务真正开始时读系统时钟，晚于提交时刻（id 内嵌毫秒）一个很短的间隔。
// 本地升级证据库里的官方实采（39 个不同回合，其中 2 个来自真实上游）里，这个差值是 1～4 毫秒，
// 分布见 officialCodexTurnStartGapSamples。网关只做到“差值服从同一分布”：turn_id 在网关判定出新回合
// 的时刻生成，内嵌毫秒回拨 officialCodexTurnSubmitLeadMs 毫秒，开始时间由 turn_id 无状态推出
// （内嵌毫秒 + 以 turn_id 为种子的确定性抽样差值）。这样同一回合无论重试、续轮还是换路径，
// 开始时间都一致，且推出的时间不会晚于生成时刻（官方的开始时间也恒早于该回合的请求发出时刻）。
// ---------------------------------------------------------------------------

const (
	// officialCodexTurnSubmitLeadMs 是 turn_id 内嵌毫秒相对生成时刻的回拨量，等于实采差值的最大值，
	// 保证“内嵌毫秒 + 抽样差值”不晚于生成时刻。
	officialCodexTurnSubmitLeadMs = 4

	// officialCodexIDLeadMs 是会话、线程、item id 内嵌毫秒相对生成时刻的回拨量，须大于回合 id 的回拨量。
	officialCodexIDLeadMs = 50

	// Rust uuid 库的计数器常量：42 位计数器，重置时取 41 位随机数（最高位留空，防止同毫秒内溢出）。
	officialCodexUUIDV7ReseedMask  = uint64(1)<<41 - 1
	officialCodexUUIDV7MaxCounter  = uint64(1)<<42 - 1
	officialCodexUUIDV7CounterLow  = uint64(1)<<30 - 1
	officialCodexInvocationTurnTTL = 2 * time.Hour
	// officialCodexInvocationTurnMax 是按调用（invocation）兜底生成的回合 id 缓存条目上限。
	officialCodexInvocationTurnMax = 4096
)

// officialCodexTurnStartGapSamples 是官方实采中 turn_started_at_unix_ms 减 turn_id 内嵌毫秒的差值分布
// （差值毫秒 -> 回合个数）。数据来源：非正式实采 37 个回合（ARM64 上官方客户端对本地假上游）加真实上游
// 实采 2 个回合，共 39 个；其中 1 毫秒 13 个、2 毫秒 18 个、3 毫秒 7 个、4 毫秒 1 个。
// 样本只覆盖开发机上的 exec 与 app-server 场景，没有覆盖“首轮要刷新模型目录”等可能拉长差值的场景，
// 待 ARM64 实采更多样本后再校准。
var officialCodexTurnStartGapSamples = [...]struct {
	gapMs int64
	count uint64
}{{1, 13}, {2, 18}, {3, 7}, {4, 1}}

// officialCodexTurnClock 与 officialCodexTurnRandom 只为让测试固定时间与随机数。
var (
	officialCodexTurnClock  = time.Now
	officialCodexTurnRandom = func(p []byte) error { _, err := rand.Read(p); return err }
)

// officialCodexUUIDV7Context 复刻 Rust uuid 库的共享单调上下文（ContextV7，不带额外时间精度）。
type officialCodexUUIDV7Context struct {
	mu      sync.Mutex
	lastMs  int64
	counter uint64
}

var officialCodexTurnUUIDContext = &officialCodexUUIDV7Context{}

// officialCodexInvocationTurnCache 缓存“按调用兜底生成”的回合 id：同一次调用的多次 attempt 要拿到同一个
// turn_id。条目少、存活短，单独成表，避免挤占 session、turn 等长期身份的缓存。
var officialCodexInvocationTurnCache = newOfficialUUIDV7LRUCache(
	officialCodexInvocationTurnTTL,
	officialCodexInvocationTurnMax,
	time.Now,
)

// newOfficialCodexTurnUUIDV7 生成一个官方形态的回合 id。
func newOfficialCodexTurnUUIDV7() string {
	nowMs := officialCodexTurnClock().UnixMilli()
	return officialCodexTurnUUIDContext.generate(nowMs - officialCodexTurnSubmitLeadMs)
}

// generate 按 Rust uuid 库的上下文语义取下一个（毫秒，计数器）并编码成 UUID 文本。
func (c *officialCodexUUIDV7Context) generate(ms int64) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if ms > c.lastMs {
		c.lastMs = ms
		c.counter = officialCodexUUIDV7Reseed()
	} else {
		// 同一毫秒或时钟回退：沿用上次的毫秒，计数器加一。
		ms = c.lastMs
		c.counter++
		if c.counter > officialCodexUUIDV7MaxCounter {
			c.lastMs++
			ms = c.lastMs
			c.counter = officialCodexUUIDV7Reseed()
		}
	}
	return encodeOfficialCodexUUIDV7(ms, c.counter)
}

func officialCodexUUIDV7Reseed() uint64 {
	var raw [8]byte
	if err := officialCodexTurnRandom(raw[:]); err != nil {
		// 随机源失败时退回时间扰动，不能 panic：回合 id 只要求形态与单调，不要求密码学强度。
		binary.BigEndian.PutUint64(raw[:], uint64(time.Now().UnixNano()))
	}
	return binary.BigEndian.Uint64(raw[:]) & officialCodexUUIDV7ReseedMask
}

// encodeOfficialCodexUUIDV7 按上面注释的位布局编码。counter 为 42 位计数器。
func encodeOfficialCodexUUIDV7(ms int64, counter uint64) string {
	// 先放 16 字节随机数；计数器占其最高 44 位（42 位计数器 + 在变体占位处插入的 2 位 0）。
	var b [16]byte
	if err := officialCodexTurnRandom(b[:]); err != nil {
		binary.BigEndian.PutUint64(b[:8], uint64(time.Now().UnixNano()))
		binary.BigEndian.PutUint64(b[8:], uint64(ms))
	}
	shifted := (counter>>30)<<32 | (counter & officialCodexUUIDV7CounterLow)
	b[0] = byte(shifted >> 36)
	b[1] = byte(shifted >> 28)
	b[2] = byte(shifted >> 20)
	b[3] = byte(shifted >> 12)
	b[4] = byte(shifted >> 4)
	b[5] = byte(shifted&0x0f)<<4 | b[5]&0x0f

	var out uuid.UUID
	// 毫秒时间戳：48 位大端。
	out[0] = byte(ms >> 40)
	out[1] = byte(ms >> 32)
	out[2] = byte(ms >> 24)
	out[3] = byte(ms >> 16)
	out[4] = byte(ms >> 8)
	out[5] = byte(ms)
	// 版本 7 加 rand_a：库的编码函数取 b[1] 与 b[0] 的低 4 位，b[0] 的高 4 位被丢弃。
	randA := uint16(b[1]) | (uint16(b[0])<<8)&0x0fff | 0x7000
	binary.BigEndian.PutUint16(out[6:8], randA)
	// 变体 0b10 加 rand_b。
	out[8] = b[2]&0x3f | 0x80
	copy(out[9:], b[3:10])
	return out.String()
}

// generateOfficialStableTurnUUIDV7 是 generateOfficialStableUUIDV7 的回合 id 版本：键语义与缓存策略相同，
// 只是未命中时按官方 Uuid::now_v7() 的位布局与内嵌毫秒语义生成。
func generateOfficialStableTurnUUIDV7(key officialUUIDV7CacheKey) string {
	return officialUUIDV7Cache.resolveWith(key, newOfficialCodexTurnUUIDV7)
}

// officialCodexInvocationTurnID 为没有任何回合来源的调用兜底生成回合 id：同一 invocationSeed 的多次调用得到
// 同一个值。取代以前由 invocation seed 做 SHA-256 再按 8-4-4-4-12 排版的写法（那不是 UUID，也没有时间位）。
func officialCodexInvocationTurnID(invocationSeed string) string {
	key := newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
		WriteString("invocation").
		WriteString(invocationSeed).
		Key()
	return officialCodexInvocationTurnCache.resolveWith(key, newOfficialCodexTurnUUIDV7)
}

// officialCodexTurnStartGapMs 以 turnID 为种子确定性地从实采差值分布里抽一个毫秒数。
func officialCodexTurnStartGapMs(turnID string) int64 {
	var total uint64
	for _, sample := range officialCodexTurnStartGapSamples {
		total += sample.count
	}
	sum := sha256.Sum256([]byte("openai-official-egress-turn-start-gap\x00" + turnID))
	pick := binary.BigEndian.Uint64(sum[:8]) % total
	for _, sample := range officialCodexTurnStartGapSamples {
		if pick < sample.count {
			return sample.gapMs
		}
		pick -= sample.count
	}
	return officialCodexTurnStartGapSamples[len(officialCodexTurnStartGapSamples)-1].gapMs
}

// officialCodexTurnStartedAtMs 由回合 id 推出回合开始时间（毫秒）：内嵌毫秒加抽样差值。
// turnID 不是 UUIDv7 时（例如客户端自带的非官方形态 id）没有内嵌时间可用，返回 false，由调用方沿用原来的取法。
func officialCodexTurnStartedAtMs(turnID string) (int64, bool) {
	parsed, err := uuid.Parse(strings.TrimSpace(turnID))
	if err != nil || parsed.Version() != 7 {
		return 0, false
	}
	embedded := int64(parsed[0])<<40 | int64(parsed[1])<<32 | int64(parsed[2])<<24 |
		int64(parsed[3])<<16 | int64(parsed[4])<<8 | int64(parsed[5])
	return embedded + officialCodexTurnStartGapMs(parsed.String()), true
}

// officialCodexTurnStartedAtMsOrNow 同 officialCodexTurnStartedAtMs，推不出时取当前时钟。
func officialCodexTurnStartedAtMsOrNow(turnID string) int64 {
	if startedAt, ok := officialCodexTurnStartedAtMs(turnID); ok {
		return startedAt
	}
	return time.Now().UnixMilli()
}
