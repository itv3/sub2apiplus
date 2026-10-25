package service

import (
	"container/list"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

// 本文件是本仓库独有的逻辑，不属于上游。
//
// 背景：Codex 的 WebSocket 握手发生在网关还不知道模型的时候，所以网关只能先接受（101）。
// 收到第一条消息、选中需要协议转换的账号（上游是 Chat Completions 或 Anthropic）后，
// WebSocket 无状态桥接无法服务，只能用关闭码 1008 断开。Codex 对「握手之后才被关闭」一律按
// 断线重试，重试满次数才改走 HTTPS，于是新会话第一轮要多等十几秒、界面出现多次「重新连接」。
// Codex 只有在握手阶段直接收到 426 时才会立刻改走 HTTPS。
//
// 做法：第一次因协议转换拒绝时，把「这个会话要走 HTTPS」记在内存里；同一会话再来握手，
// 在 Accept 之前直接回 426，让客户端立刻回落。
//
// 约束：
//   - 只用握手请求自带的会话头做键（复用 explicitOpenAIHeaderSessionID），并混入 API Key，
//     不同 Key 之间互不影响；握手没带会话头时既不记录也不拦截，行为与改动前一致。
//   - 只存在内存里，进程重启即清空，最坏结果是多一轮重连，不影响正确性。
//   - 不改变任何出站请求的 Header、Body、身份或 transport。

const (
	// openAIWSHTTPFallbackPinTTL 是标记的有效期；每次命中都会续期（滑动过期）。
	openAIWSHTTPFallbackPinTTL = 2 * time.Hour
	// openAIWSHTTPFallbackPinMaxEntries 是内存表的条数上限，超出时淘汰最久未被使用的条目。
	openAIWSHTTPFallbackPinMaxEntries = 20000
)

// openAIWSHTTPFallbackPins 是进程内共享的标记表。
var openAIWSHTTPFallbackPins = newOpenAIWSHTTPFallbackPinStore(
	openAIWSHTTPFallbackPinTTL,
	openAIWSHTTPFallbackPinMaxEntries,
	time.Now,
)

type openAIWSHTTPFallbackPinKey [sha256.Size]byte

type openAIWSHTTPFallbackPinEntry struct {
	key      openAIWSHTTPFallbackPinKey
	expireAt time.Time
}

// openAIWSHTTPFallbackPinStore 是带过期与条数上限的 LRU 内存表。
// 过期时间等于「最后一次使用时间 + ttl」，ttl 固定，因此链表尾部的条目一定最早过期，
// 清理与淘汰都只需要从尾部取，复杂度 O(1)。
type openAIWSHTTPFallbackPinStore struct {
	mu         sync.Mutex
	ttl        time.Duration
	maxEntries int
	now        func() time.Time
	items      map[openAIWSHTTPFallbackPinKey]*list.Element
	order      *list.List // 头部是最近使用的条目
}

func newOpenAIWSHTTPFallbackPinStore(ttl time.Duration, maxEntries int, now func() time.Time) *openAIWSHTTPFallbackPinStore {
	if now == nil {
		now = time.Now
	}
	return &openAIWSHTTPFallbackPinStore{
		ttl:        ttl,
		maxEntries: maxEntries,
		now:        now,
		items:      make(map[openAIWSHTTPFallbackPinKey]*list.Element),
		order:      list.New(),
	}
}

// openAIWSHTTPFallbackPinKeyOf 把「API Key + 会话标识」哈希成定长键，避免超长请求头撑大内存。
func openAIWSHTTPFallbackPinKeyOf(apiKeyID int64, sessionID string) openAIWSHTTPFallbackPinKey {
	h := sha256.New()
	var idBytes [8]byte
	binary.BigEndian.PutUint64(idBytes[:], uint64(apiKeyID))
	_, _ = h.Write(idBytes[:])
	_, _ = h.Write([]byte(sessionID))
	var key openAIWSHTTPFallbackPinKey
	copy(key[:], h.Sum(nil))
	return key
}

// purgeExpiredLocked 从尾部清掉已过期的条目，调用方必须持有 mu。
func (s *openAIWSHTTPFallbackPinStore) purgeExpiredLocked(now time.Time) {
	for {
		tail := s.order.Back()
		if tail == nil {
			return
		}
		entry := tail.Value.(*openAIWSHTTPFallbackPinEntry)
		if now.Before(entry.expireAt) {
			return
		}
		s.order.Remove(tail)
		delete(s.items, entry.key)
	}
}

// record 记录（或续期）一个标记。
func (s *openAIWSHTTPFallbackPinStore) record(key openAIWSHTTPFallbackPinKey) {
	if s == nil || s.maxEntries <= 0 || s.ttl <= 0 {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	s.purgeExpiredLocked(now)
	expireAt := now.Add(s.ttl)
	if element, ok := s.items[key]; ok {
		element.Value.(*openAIWSHTTPFallbackPinEntry).expireAt = expireAt
		s.order.MoveToFront(element)
		return
	}
	// 淘汰最久未被使用的条目，给新条目腾位置。
	for len(s.items) >= s.maxEntries {
		tail := s.order.Back()
		if tail == nil {
			break
		}
		s.order.Remove(tail)
		delete(s.items, tail.Value.(*openAIWSHTTPFallbackPinEntry).key)
	}
	s.items[key] = s.order.PushFront(&openAIWSHTTPFallbackPinEntry{key: key, expireAt: expireAt})
}

// pinned 查询标记是否有效；命中时顺带续期。
func (s *openAIWSHTTPFallbackPinStore) pinned(key openAIWSHTTPFallbackPinKey) bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	element, ok := s.items[key]
	if !ok {
		return false
	}
	entry := element.Value.(*openAIWSHTTPFallbackPinEntry)
	if !now.Before(entry.expireAt) {
		s.order.Remove(element)
		delete(s.items, key)
		return false
	}
	entry.expireAt = now.Add(s.ttl)
	s.order.MoveToFront(element)
	return true
}

// size 返回当前条数（含尚未被清理的过期条目），仅供测试使用。
func (s *openAIWSHTTPFallbackPinStore) size() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.items)
}

// openAIWSHTTPFallbackPinLogID 返回键的短指纹，日志里用它代替原始会话标识。
func openAIWSHTTPFallbackPinLogID(key openAIWSHTTPFallbackPinKey) string {
	return hex.EncodeToString(key[:6])
}

// openAIWSHTTPFallbackPinKeyFromContext 从握手请求取会话头与 API Key 生成键；
// 没有会话头时返回 ok=false。
func openAIWSHTTPFallbackPinKeyFromContext(c *gin.Context) (key openAIWSHTTPFallbackPinKey, apiKeyID int64, ok bool) {
	sessionID := strings.TrimSpace(explicitOpenAIHeaderSessionID(c))
	if sessionID == "" {
		return key, 0, false
	}
	apiKeyID = getAPIKeyIDFromContext(c)
	return openAIWSHTTPFallbackPinKeyOf(apiKeyID, sessionID), apiKeyID, true
}

// RecordOpenAIWSHTTPFallbackPin 在「因需要协议转换而拒绝 WebSocket」时调用，
// 记下握手请求对应的会话要走 HTTPS。握手没带会话头时不记录，返回 false。
func RecordOpenAIWSHTTPFallbackPin(c *gin.Context, accountID int64) bool {
	key, apiKeyID, ok := openAIWSHTTPFallbackPinKeyFromContext(c)
	if !ok {
		return false
	}
	openAIWSHTTPFallbackPins.record(key)
	logOpenAIWSModeInfo(
		"ingress_ws_http_fallback_pin_recorded api_key_id=%d account_id=%d session_fp=%s ttl=%s",
		apiKeyID,
		accountID,
		openAIWSHTTPFallbackPinLogID(key),
		openAIWSHTTPFallbackPinTTL,
	)
	return true
}

// OpenAIWSHTTPFallbackPinned 在 WebSocket 握手 Accept 之前调用：
// 该会话此前因协议转换被拒绝过时返回 true，调用方应直接回 426。
// 握手没带会话头时始终返回 false，保持改动前的行为。
func OpenAIWSHTTPFallbackPinned(c *gin.Context) bool {
	key, apiKeyID, ok := openAIWSHTTPFallbackPinKeyFromContext(c)
	if !ok || !openAIWSHTTPFallbackPins.pinned(key) {
		return false
	}
	logOpenAIWSModeInfo(
		"ingress_ws_http_fallback_pin_rejected api_key_id=%d session_fp=%s status=426",
		apiKeyID,
		openAIWSHTTPFallbackPinLogID(key),
	)
	return true
}
