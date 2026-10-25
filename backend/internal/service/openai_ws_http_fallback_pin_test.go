package service

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// fakePinClock 是可手动拨动的时钟，让过期测试不依赖真实等待。
type fakePinClock struct {
	mu  sync.Mutex
	cur time.Time
}

func (f *fakePinClock) now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cur
}

func (f *fakePinClock) advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cur = f.cur.Add(d)
}

func newPinTestStore(ttl time.Duration, maxEntries int) (*openAIWSHTTPFallbackPinStore, *fakePinClock) {
	clock := &fakePinClock{cur: time.Unix(1_700_000_000, 0)}
	return newOpenAIWSHTTPFallbackPinStore(ttl, maxEntries, clock.now), clock
}

func newPinTestContext(apiKeyID int64, headers map[string]string) *gin.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/openai/v1/responses", nil)
	for k, v := range headers {
		c.Request.Header.Set(k, v)
	}
	if apiKeyID > 0 {
		c.Set("api_key", &APIKey{ID: apiKeyID})
	}
	return c
}

func TestOpenAIWSHTTPFallbackPinStore_RecordAndQuery(t *testing.T) {
	store, _ := newPinTestStore(time.Hour, 10)
	key := openAIWSHTTPFallbackPinKeyOf(1, "sess-a")

	require.False(t, store.pinned(key), "未记录前不应命中")
	store.record(key)
	require.True(t, store.pinned(key))
	require.False(t, store.pinned(openAIWSHTTPFallbackPinKeyOf(1, "sess-b")), "不同会话互不影响")
	require.False(t, store.pinned(openAIWSHTTPFallbackPinKeyOf(2, "sess-a")), "不同 API Key 互不影响")
}

func TestOpenAIWSHTTPFallbackPinStore_ExpiresAndSlidingRenewal(t *testing.T) {
	store, clock := newPinTestStore(time.Hour, 10)
	key := openAIWSHTTPFallbackPinKeyOf(1, "sess-a")
	store.record(key)

	// 命中会续期：每 40 分钟查一次，累计超过 1 小时仍然有效。
	for i := 0; i < 4; i++ {
		clock.advance(40 * time.Minute)
		require.True(t, store.pinned(key), "第 %d 次查询应命中并续期", i+1)
	}

	// 一小时内没人再查，则过期并被删除。
	clock.advance(time.Hour)
	require.False(t, store.pinned(key))
	require.Zero(t, store.size())
}

func TestOpenAIWSHTTPFallbackPinStore_RecordRenewsAndPurgesExpired(t *testing.T) {
	store, clock := newPinTestStore(time.Hour, 10)
	old := openAIWSHTTPFallbackPinKeyOf(1, "old")
	store.record(old)
	clock.advance(30 * time.Minute)
	fresh := openAIWSHTTPFallbackPinKeyOf(1, "fresh")
	store.record(fresh)

	clock.advance(31 * time.Minute) // old 已过期，fresh 还剩 29 分钟
	store.record(openAIWSHTTPFallbackPinKeyOf(1, "third"))
	require.Equal(t, 2, store.size(), "写入时应顺带清掉过期的 old")
	require.False(t, store.pinned(old))
	require.True(t, store.pinned(fresh))
}

func TestOpenAIWSHTTPFallbackPinStore_EvictsLeastRecentlyUsedAtCapacity(t *testing.T) {
	store, clock := newPinTestStore(time.Hour, 3)
	keys := make([]openAIWSHTTPFallbackPinKey, 0, 4)
	for i := 0; i < 3; i++ {
		keys = append(keys, openAIWSHTTPFallbackPinKeyOf(1, "s"+strconv.Itoa(i)))
		store.record(keys[i])
		clock.advance(time.Second)
	}
	require.True(t, store.pinned(keys[0]), "s0 被查询过，成为最近使用")

	keys = append(keys, openAIWSHTTPFallbackPinKeyOf(1, "s3"))
	store.record(keys[3]) // 已满，应淘汰最久未使用的 s1
	require.Equal(t, 3, store.size())
	require.False(t, store.pinned(keys[1]), "s1 应被淘汰")
	require.True(t, store.pinned(keys[0]))
	require.True(t, store.pinned(keys[2]))
	require.True(t, store.pinned(keys[3]))
}

func TestOpenAIWSHTTPFallbackPinStore_DisabledWhenLimitsInvalid(t *testing.T) {
	key := openAIWSHTTPFallbackPinKeyOf(1, "s")
	for _, store := range []*openAIWSHTTPFallbackPinStore{
		newOpenAIWSHTTPFallbackPinStore(time.Hour, 0, nil),
		newOpenAIWSHTTPFallbackPinStore(0, 10, nil),
	} {
		store.record(key)
		require.False(t, store.pinned(key))
	}
	var nilStore *openAIWSHTTPFallbackPinStore
	nilStore.record(key)
	require.False(t, nilStore.pinned(key))
}

func TestOpenAIWSHTTPFallbackPinStore_ConcurrentAccess(t *testing.T) {
	store, _ := newPinTestStore(time.Hour, 50)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 500; i++ {
				key := openAIWSHTTPFallbackPinKeyOf(int64(g), "s"+strconv.Itoa(i%80))
				store.record(key)
				store.pinned(key)
			}
		}(g)
	}
	wg.Wait()
	require.LessOrEqual(t, store.size(), 50)
}

func TestOpenAIWSHTTPFallbackPin_RecordAndQueryByContext(t *testing.T) {
	original := openAIWSHTTPFallbackPins
	store, _ := newPinTestStore(time.Hour, 10)
	openAIWSHTTPFallbackPins = store
	t.Cleanup(func() { openAIWSHTTPFallbackPins = original })

	handshake := map[string]string{"session-id": "codex-thread-1"}

	require.False(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(7, handshake)), "未记录前不拦截")
	require.True(t, RecordOpenAIWSHTTPFallbackPin(newPinTestContext(7, handshake), 42))
	require.True(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(7, handshake)), "同 Key 同会话应命中")

	require.False(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(8, handshake)), "换 API Key 不命中")
	require.False(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(7, map[string]string{"session-id": "codex-thread-2"})), "换会话不命中")
	// 兼容 explicitOpenAIHeaderSessionID 名单里的其他会话头。
	require.True(t, RecordOpenAIWSHTTPFallbackPin(newPinTestContext(7, map[string]string{"conversation_id": "conv-1"}), 42))
	require.True(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(7, map[string]string{"conversation_id": "conv-1"})))
}

func TestOpenAIWSHTTPFallbackPin_NoSessionHeaderNeverRecordsOrBlocks(t *testing.T) {
	original := openAIWSHTTPFallbackPins
	store, _ := newPinTestStore(time.Hour, 10)
	openAIWSHTTPFallbackPins = store
	t.Cleanup(func() { openAIWSHTTPFallbackPins = original })

	noHeader := newPinTestContext(7, nil)
	require.False(t, RecordOpenAIWSHTTPFallbackPin(noHeader, 42), "没有会话头不记录")
	require.Zero(t, store.size())
	require.False(t, OpenAIWSHTTPFallbackPinned(noHeader), "没有会话头不拦截")

	blank := newPinTestContext(7, map[string]string{"session-id": "   "})
	require.False(t, RecordOpenAIWSHTTPFallbackPin(blank, 42), "空白会话头视为没有")
	require.Zero(t, store.size())

	// 即便表里有别的会话的标记，没带会话头的握手也不会被拦。
	require.True(t, RecordOpenAIWSHTTPFallbackPin(newPinTestContext(7, map[string]string{"session-id": "x"}), 42))
	require.False(t, OpenAIWSHTTPFallbackPinned(newPinTestContext(7, nil)))
	require.False(t, RecordOpenAIWSHTTPFallbackPin(nil, 42))
	require.False(t, OpenAIWSHTTPFallbackPinned(nil))
}
