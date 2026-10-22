package service

import (
	"crypto/sha256"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

// 本文件锁定 docs/bug.md 问题五的根治约束：官方出站身份缓存只允许固定长度摘要作
// 键，用户正文在任何时刻都不能进入缓存；锚点摘要与旧种子的规范化语义逐字节一致。

func testOfficialContentDigestOf(text string) officialContentDigest {
	return officialContentDigest(sha256.Sum256([]byte(text)))
}

func TestOfficialUUIDV7SeedFieldBoundariesAreUnambiguous(t *testing.T) {
	left := newOfficialUUIDV7Seed("turn").WriteString("a").WriteString("bc").Key()
	right := newOfficialUUIDV7Seed("turn").WriteString("ab").WriteString("c").Key()
	require.NotEqual(t, left, right, "带长度前缀的字段不能因拼接位置不同而碰撞")
	require.Equal(
		t,
		left,
		newOfficialUUIDV7Seed("turn").WriteString("a").WriteString("bc").Key(),
		"相同字段序列必须得到相同的键",
	)
	require.NotEqual(
		t,
		newOfficialUUIDV7Seed("turn").WriteString("").Key(),
		newOfficialUUIDV7Seed("turn").Key(),
		"空字段与没有字段必须区分",
	)
}

func TestOfficialUUIDV7SeedSeparatesDomains(t *testing.T) {
	session := newOfficialUUIDV7Seed(officialUUIDV7DomainSession).WriteString("x").Key()
	child := newOfficialUUIDV7Seed(officialUUIDV7DomainChild).WriteString("x").Key()
	turn := newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).WriteString("x").Key()
	require.NotEqual(t, session, child)
	require.NotEqual(t, session, turn)
	require.NotEqual(t, child, turn)
}

func TestOfficialUUIDV7SeedUserAnchorDistinguishesIndexPresenceAndContent(t *testing.T) {
	hello := testOfficialContentDigestOf("hello")
	world := testOfficialContentDigestOf("world")
	base := func() *officialUUIDV7Seed {
		return newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).WriteString("thread")
	}
	present := base().WriteUserAnchor(officialOpenAIUserAnchor{index: 3, digest: hello}, true).Key()
	require.Equal(
		t,
		present,
		base().WriteUserAnchor(officialOpenAIUserAnchor{index: 3, digest: hello}, true).Key(),
	)
	require.NotEqual(
		t,
		present,
		base().WriteUserAnchor(officialOpenAIUserAnchor{index: 4, digest: hello}, true).Key(),
		"同一文本出现在不同下标必须得到不同的 turn 键",
	)
	require.NotEqual(
		t,
		present,
		base().WriteUserAnchor(officialOpenAIUserAnchor{index: 3, digest: world}, true).Key(),
	)
	absent := base().WriteUserAnchor(officialOpenAIUserAnchor{}, false).Key()
	require.NotEqual(t, present, absent)
	require.Equal(
		t,
		absent,
		base().WriteUserAnchor(officialOpenAIUserAnchor{index: 9, digest: hello}, false).Key(),
		"锚点不存在时不得泄漏任何残留字段",
	)
}

// legacyOfficialOpenAIUserAnchors 复刻改造前 officialOpenAIHTTPUserAnchors 的语义：
// 完整解码正文后返回首条用户消息全文与 “下标:末条用户消息全文”。它只在测试里作为
// 等价性参照存在。
func legacyOfficialOpenAIUserAnchors(t *testing.T, body []byte) (string, string) {
	t.Helper()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	input, ok := payload["input"].([]any)
	if !ok {
		if text, ok := payload["input"].(string); ok {
			text = strings.TrimSpace(text)
			return text, "0:" + text
		}
		return "", ""
	}
	first := ""
	last := ""
	for index, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || officialOpenAIString(item, "type") != "message" ||
			officialOpenAIString(item, "role") != "user" {
			continue
		}
		text := officialOpenAIHTTPMessageContentText(item["content"])
		if text == "" {
			continue
		}
		if first == "" {
			first = text
		}
		last = strconv.Itoa(index) + ":" + text
	}
	return first, last
}

func TestOfficialOpenAIUserAnchorsMatchLegacyNormalization(t *testing.T) {
	fixtures := map[string]string{
		"string_input":       `{"model":"gpt-5.4","input":"  hello world  "}`,
		"empty_string_input": `{"model":"gpt-5.4","input":""}`,
		"blank_string_input": `{"model":"gpt-5.4","input":"   "}`,
		"missing_input":      `{"model":"gpt-5.4"}`,
		"object_input":       `{"model":"gpt-5.4","input":{"text":"not-an-array"}}`,
		"mixed_roles": `{"input":[
			{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
			{"type":"message","role":"user","content":"  第一轮问题 "},
			{"type":"message","role":"assistant","content":[{"type":"output_text","text":"回答"}]},
			{"type":"message","role":"user","content":[
				{"type":"input_text","text":" a "},
				{"type":"input_image","image_url":"data:x"},
				{"type":"input_text","input_text":"b"},
				{"type":"input_text","text":"   "},
				"not-an-object"
			]}
		]}`,
		"skips_empty_and_foreign_items": `{"input":[
			{"type":"message","role":"user","content":[{"type":"input_text","text":""}]},
			{"type":"function_call_output","role":"user","output":"ignored"},
			42,
			{"type":"message","role":"user","content":[{"type":"input_text","text":"   "}]},
			{"type":"message","role":"user","content":"last"}
		]}`,
		"trimmed_type_and_role": `{"input":[
			{"type":" message ","role":"user ","content":"trimmed"}
		]}`,
		"escapes_and_unicode": `{"input":[
			{"type":"message","role":"user","content":"中文\n换行\t制表 \"引号\" \\ 反斜杠 é emoji 😀"},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"line1\nline2"},{"type":"input_text","text":"中"}]}
		]}`,
		"no_user_messages": `{"input":[
			{"type":"message","role":"developer","content":"rules"},
			{"type":"message","role":"assistant","content":"answer"}
		]}`,
		"only_whitespace_user": `{"input":[
			{"type":"message","role":"user","content":"   "}
		]}`,
		"non_string_type": `{"input":[
			{"type":1,"role":"user","content":"x"},
			{"type":"message","role":["user"],"content":"y"},
			{"type":"message","role":"user","content":{"text":"object-content"}},
			{"type":"message","role":"user","content":"z"}
		]}`,
	}
	for name, raw := range fixtures {
		t.Run(name, func(t *testing.T) {
			body := []byte(raw)
			legacyFirst, legacyLast := legacyOfficialOpenAIUserAnchors(t, body)
			anchors := officialOpenAIUserAnchorsFromBody(body)

			if legacyFirst == "" {
				require.False(t, anchors.firstFound, "旧语义没有首条锚点时新实现也不能有")
			} else {
				require.True(t, anchors.firstFound)
				require.Equal(t, testOfficialContentDigestOf(legacyFirst), anchors.first.digest)
			}
			if legacyLast == "" {
				require.False(t, anchors.lastFound)
			} else {
				require.True(t, anchors.lastFound)
				separator := strings.Index(legacyLast, ":")
				require.Greater(t, separator, 0)
				index, err := strconv.Atoi(legacyLast[:separator])
				require.NoError(t, err)
				require.Equal(t, index, anchors.last.index, "末条锚点必须保留 input 下标")
				require.Equal(
					t,
					testOfficialContentDigestOf(legacyLast[separator+1:]),
					anchors.last.digest,
					"末条锚点摘要必须与旧规范化文本逐字节一致",
				)
			}

			var payload map[string]any
			require.NoError(t, json.Unmarshal(body, &payload))
			require.Equal(
				t,
				anchors,
				officialOpenAIUserAnchorsFromInput(payload["input"]),
				"原始 JSON 扫描与结构化遍历必须得到相同锚点",
			)
		})
	}
}

func TestDigestOfficialOpenAIMessageContentMatchesLegacyText(t *testing.T) {
	contents := []any{
		"  plain  ",
		"",
		[]any{
			map[string]any{"type": "input_text", "text": " a "},
			map[string]any{"type": "input_text", "input_text": "b"},
			map[string]any{"type": "input_text", "text": "  "},
			"skip",
		},
		[]any{},
		[]any{map[string]any{"type": "input_text", "text": ""}},
		json.RawMessage(`[{"type":"input_text","text":"raw \n part"},{"type":"input_text","input_text":"x"}]`),
		42,
		nil,
	}
	for index, content := range contents {
		legacy := officialOpenAIHTTPMessageContentText(content)
		if raw, ok := content.(json.RawMessage); ok {
			var decoded any
			require.NoError(t, json.Unmarshal(raw, &decoded))
			legacy = officialOpenAIHTTPMessageContentText(decoded)
		}
		digest, found := digestOfficialOpenAIMessageContent(content)
		require.Equal(t, legacy != "", found, "content[%d]", index)
		if found {
			require.Equal(t, testOfficialContentDigestOf(legacy), digest, "content[%d]", index)
		}
	}
}

func testMeasureAllocatedBytes(t *testing.T, run func()) uint64 {
	t.Helper()
	// 满载机器上，前面用例遗留的后台协程和日志写入会在测量窗口里同时分配，把“只复制几个短字段”这类上限用例的读数
	// 抬高到几十 KB（后台验证 #10：56664 对 4096，取三次最小值也一样）。测量期间把 P 数降到 1，遗留协程在窗口内
	// 无法并行运行；仍取多次中的最小值，并在每次测量前强制 GC，让基线干净。
	previous := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(previous)
	best := ^uint64(0)
	for attempt := 0; attempt < 5; attempt++ {
		var before, after runtime.MemStats
		runtime.GC()
		runtime.ReadMemStats(&before)
		run()
		runtime.ReadMemStats(&after)
		if allocated := after.TotalAlloc - before.TotalAlloc; allocated < best {
			best = allocated
		}
	}
	return best
}

var testAllocNoiseSink atomic.Value

// 回归：满载时其它协程会在测量窗口里同时分配（后台验证 #10 的 TestNewOpenAIRequestViewAllocatesOnlyExtractedFields：
// 读数 56664，上限 4096）。辅助函数在窗口内只留一个 P，后台协程无法并行分配，读数只反映被测函数自己。
func TestMeasureAllocatedBytesIgnoresBackgroundAllocation(t *testing.T) {
	stop := make(chan struct{})
	defer close(stop)
	for i := 0; i < 4; i++ {
		go func() {
			for {
				select {
				case <-stop:
					return
				default:
					testAllocNoiseSink.Store(make([]byte, 2048))
					time.Sleep(20 * time.Microsecond)
				}
			}
		}()
	}
	var kept []byte
	allocated := testMeasureAllocatedBytes(t, func() {
		kept = make([]byte, 1024)
		for index := range kept {
			kept[index] = 1
		}
	})
	require.Len(t, kept, 1024)
	require.GreaterOrEqual(t, allocated, uint64(1024))
	require.Less(t, allocated, uint64(8192),
		"后台协程的分配不得计入被测函数：读数 %d 字节", allocated)
}

func testBuildLargeOfficialOpenAIBody(t *testing.T, userMessages int, textBytes int) []byte {
	t.Helper()
	// 只用 ASCII 填充，按字节截断不会切断多字节字符；引号与换行保证 JSON 中存在
	// 转义，扫描路径必须真正反转义首末两条用户消息。
	unit := "long-context \"quoted\" line\n"
	filler := strings.Repeat(unit, textBytes/len(unit)+1)[:textBytes]
	items := make([]any, 0, userMessages*2)
	for index := 0; index < userMessages; index++ {
		items = append(items, map[string]any{
			"type": "message",
			"role": "user",
			"content": []any{map[string]any{
				"type": "input_text",
				"text": "user-" + strconv.Itoa(index) + "\n" + filler,
			}},
		})
		items = append(items, map[string]any{
			"type": "message",
			"role": "assistant",
			"content": []any{map[string]any{
				"type": "output_text",
				"text": "assistant-" + strconv.Itoa(index) + "\n" + filler,
			}},
		})
	}
	body, err := json.Marshal(map[string]any{"model": "gpt-5.4", "input": items})
	require.NoError(t, err)
	return body
}

func TestOfficialOpenAIUserAnchorsFromBodyAvoidsCopyingLargeBody(t *testing.T) {
	body := testBuildLargeOfficialOpenAIBody(t, 32, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)

	var anchors officialOpenAIUserAnchors
	allocated := testMeasureAllocatedBytes(t, func() {
		anchors = officialOpenAIUserAnchorsFromBody(body)
	})
	require.True(t, anchors.firstFound)
	require.True(t, anchors.lastFound)
	require.Equal(t, 62, anchors.last.index)
	require.Less(
		t,
		allocated,
		uint64(len(body))/4,
		"锚点提取不得完整解码或复制正文（实际分配 %d 字节，正文 %d 字节）",
		allocated,
		len(body),
	)

	legacyAllocated := testMeasureAllocatedBytes(t, func() {
		legacyOfficialOpenAIUserAnchors(t, body)
	})
	require.Greater(
		t,
		legacyAllocated,
		uint64(len(body)),
		"旧实现应当至少分配一整份正文，否则本测试的对照失去意义",
	)
}

func TestResolveOfficialOpenAIWSHistoricalTurnIDDoesNotMaterializeSegment(t *testing.T) {
	filler := strings.Repeat("历史片段 \"quoted\"\n", 8192)
	input := make([]any, 0, 40)
	for index := 0; index < 40; index++ {
		role := "user"
		if index%2 == 1 {
			role = "assistant"
		}
		input = append(input, map[string]any{
			"type": "message",
			"role": role,
			"content": []any{map[string]any{
				"type": "input_text",
				"text": strconv.Itoa(index) + filler,
			}},
		})
	}
	segment := make([]int, 0, len(input))
	for index := range input {
		segment = append(segment, index)
	}
	sessionID := uuid.NewString()

	var turnID string
	allocated := testMeasureAllocatedBytes(t, func() {
		var err error
		turnID, err = resolveOfficialOpenAIWSHistoricalTurnID(input, segment, sessionID, 0)
		require.NoError(t, err)
	})
	require.NoError(t, uuid.Validate(turnID))
	require.Less(
		t,
		allocated,
		uint64(256*1024),
		"有用户文本的历史片段只应做摘要，不应复制任何消息文本（实际分配 %d 字节）",
		allocated,
	)

	// 没有用户文本时以片段 JSON 摘要兜底：编码只在编码器内部短暂存在，不再生成
	// 持久化的正文或十六进制副本。encoding/json 的反射编码器可能短暂保留多份
	// 编码缓冲，因此这里只约束峰值不超过编码体积的三倍。
	assistantOnly := input[1:2]
	encoded, err := json.Marshal(assistantOnly)
	require.NoError(t, err)
	var fallbackTurnID string
	fallbackAllocated := testMeasureAllocatedBytes(t, func() {
		var err error
		fallbackTurnID, err = resolveOfficialOpenAIWSHistoricalTurnID(assistantOnly, []int{0}, sessionID, 0)
		require.NoError(t, err)
	})
	require.NoError(t, uuid.Validate(fallbackTurnID))
	require.Less(
		t,
		fallbackAllocated,
		uint64(len(encoded))*3,
		"兜底路径分配（%d 字节）不得超过编码体积（%d 字节）的三倍",
		fallbackAllocated,
		len(encoded),
	)
}

func TestResolveOfficialOpenAIWSHistoricalTurnIDIsStableAndSharesCurrentTurnSeed(t *testing.T) {
	sessionID := uuid.NewString()
	input := []any{
		map[string]any{"type": "message", "role": "developer", "content": "rules"},
		map[string]any{"type": "message", "role": "user", "content": " 第一轮问题 "},
		map[string]any{"type": "message", "role": "assistant", "content": "回答"},
	}
	segment := []int{0, 1, 2}

	first, err := resolveOfficialOpenAIWSHistoricalTurnID(input, segment, sessionID, 0)
	require.NoError(t, err)
	second, err := resolveOfficialOpenAIWSHistoricalTurnID(input, segment, sessionID, 0)
	require.NoError(t, err)
	require.Equal(t, first, second, "同一历史片段必须复用同一个 turn_id")

	// 该片段在上一轮作为当前轮时使用同一个种子，所以历史项的 turn_id 与当时
	// client_metadata.turn_id 保持连续。
	anchors := officialOpenAIUserAnchorsFromInput(input)
	require.True(t, anchors.lastFound)
	require.Equal(t, 1, anchors.last.index)
	currentTurnID := generateOfficialStableUUIDV7(
		newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
			WriteString(sessionID).
			WriteUserAnchor(anchors.last, true).
			Key(),
	)
	require.Equal(t, first, currentTurnID)

	// 没有用户文本的片段以 JSON 摘要兜底，同样稳定且区分内容。
	assistantOnly := []any{
		map[string]any{"type": "message", "role": "assistant", "content": "a"},
	}
	fallbackFirst, err := resolveOfficialOpenAIWSHistoricalTurnID(assistantOnly, []int{0}, sessionID, 0)
	require.NoError(t, err)
	fallbackSecond, err := resolveOfficialOpenAIWSHistoricalTurnID(assistantOnly, []int{0}, sessionID, 0)
	require.NoError(t, err)
	require.Equal(t, fallbackFirst, fallbackSecond)
	changed := []any{
		map[string]any{"type": "message", "role": "assistant", "content": "b"},
	}
	fallbackChanged, err := resolveOfficialOpenAIWSHistoricalTurnID(changed, []int{0}, sessionID, 0)
	require.NoError(t, err)
	require.NotEqual(t, fallbackFirst, fallbackChanged)
	require.NotEqual(t, first, fallbackFirst)
}

func TestOfficialUUIDV7LRUCacheReusesWithinTTLAndExpires(t *testing.T) {
	clock := time.Unix(1_700_000_000, 0)
	cache := newOfficialUUIDV7LRUCache(time.Hour, 16, func() time.Time { return clock })
	key := newOfficialUUIDV7Seed("test").WriteString("a").Key()

	first := cache.resolve(key)
	require.NoError(t, uuid.Validate(first))
	require.Equal(t, uuid.Version(7), uuid.MustParse(first).Version())
	clock = clock.Add(30 * time.Minute)
	require.Equal(t, first, cache.resolve(key), "TTL 内必须复用")
	stats := cache.stats()
	require.Equal(t, uint64(1), stats.Hits)
	require.Equal(t, uint64(1), stats.Misses)
	require.Equal(t, 1, stats.Entries)
	require.Equal(t, officialUUIDV7CacheEntryBytes, stats.EstimatedBytes)

	// 命中会刷新 lastUsed：距离上次使用 61 分钟才过期。
	clock = clock.Add(31 * time.Minute)
	require.Equal(t, first, cache.resolve(key))
	clock = clock.Add(61 * time.Minute)
	renewed := cache.resolve(key)
	require.NotEqual(t, first, renewed, "过期后必须生成新的 UUID")
	stats = cache.stats()
	require.Equal(t, uint64(1), stats.EvictedExpired)
	require.Equal(t, uint64(2), stats.Misses)
	require.Equal(t, 1, stats.Entries)
}

func TestOfficialUUIDV7LRUCacheEvictsLeastRecentlyUsedAtCapacity(t *testing.T) {
	clock := time.Unix(1_700_000_000, 0)
	cache := newOfficialUUIDV7LRUCache(time.Hour, 3, func() time.Time { return clock })
	keyOf := func(name string) officialUUIDV7CacheKey {
		return newOfficialUUIDV7Seed("test").WriteString(name).Key()
	}
	first := cache.resolve(keyOf("k1"))
	second := cache.resolve(keyOf("k2"))
	third := cache.resolve(keyOf("k3"))
	require.Equal(t, first, cache.resolve(keyOf("k1")), "访问 k1 使它成为最近使用")

	fourth := cache.resolve(keyOf("k4"))
	require.NotEqual(t, fourth, second)
	stats := cache.stats()
	require.Equal(t, 3, stats.Entries)
	require.Equal(t, uint64(1), stats.EvictedCapacity)

	require.Equal(t, first, cache.resolve(keyOf("k1")), "最近使用的 k1 必须幸存")
	require.Equal(t, third, cache.resolve(keyOf("k3")))
	require.Equal(t, fourth, cache.resolve(keyOf("k4")))
	require.NotEqual(t, second, cache.resolve(keyOf("k2")), "最久未使用的 k2 必须已被淘汰")
	stats = cache.stats()
	require.Equal(t, 3, stats.Entries)
	require.Equal(t, uint64(2), stats.EvictedCapacity)
}

func TestOfficialUUIDV7LRUCacheSweepsExpiredEntriesInBoundedSteps(t *testing.T) {
	clock := time.Unix(1_700_000_000, 0)
	cache := newOfficialUUIDV7LRUCache(time.Hour, 1000, func() time.Time { return clock })
	for index := 0; index < 100; index++ {
		cache.resolve(newOfficialUUIDV7Seed("old").WriteInt(index).Key())
	}
	require.Equal(t, 100, cache.stats().Entries)

	clock = clock.Add(2 * time.Hour)
	cache.resolve(newOfficialUUIDV7Seed("new").WriteInt(0).Key())
	stats := cache.stats()
	require.Equal(
		t,
		100-officialUUIDV7CacheSweepPerInsert+1,
		stats.Entries,
		"每次写入只从最久未使用端清理有限个过期条目，锁内工作量必须有界",
	)
	require.Equal(t, uint64(officialUUIDV7CacheSweepPerInsert), stats.EvictedExpired)

	inserts := (100 + officialUUIDV7CacheSweepPerInsert - 1) / officialUUIDV7CacheSweepPerInsert
	for index := 1; index < inserts; index++ {
		cache.resolve(newOfficialUUIDV7Seed("new").WriteInt(index).Key())
	}
	stats = cache.stats()
	require.Equal(t, inserts, stats.Entries, "过期条目最终全部被清理，只剩新条目")
	require.Equal(t, uint64(100), stats.EvictedExpired)
	require.Equal(t, uint64(0), stats.EvictedCapacity)
}

func TestOfficialUUIDV7CacheMemoryDoesNotScaleWithContent(t *testing.T) {
	const requests = 2000
	const textBytes = 32 * 1024
	cache := newOfficialUUIDV7LRUCache(officialUUIDV7CacheTTL, officialUUIDV7CacheMaxEntries, time.Now)

	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	for index := 0; index < requests; index++ {
		text := strings.Repeat("x", textBytes) + strconv.Itoa(index)
		digest, found := digestOfficialOpenAIMessageContent(text)
		require.True(t, found)
		cache.resolve(
			newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
				WriteString("session").
				WriteUserAnchor(officialOpenAIUserAnchor{index: index, digest: digest}, true).
				Key(),
		)
	}
	runtime.GC()
	runtime.ReadMemStats(&after)
	runtime.KeepAlive(cache)

	stats := cache.stats()
	require.Equal(t, requests, stats.Entries)
	require.Equal(t, requests*officialUUIDV7CacheEntryBytes, stats.EstimatedBytes)

	var growth uint64
	if after.HeapAlloc > before.HeapAlloc {
		growth = after.HeapAlloc - before.HeapAlloc
	}
	require.Less(
		t,
		growth,
		uint64(requests*officialUUIDV7CacheEntryBytes*4),
		"GC 后堆增长 %d 字节；若缓存仍保留内容全文，本测试会增长约 %d 字节",
		growth,
		requests*textBytes,
	)
}

func TestDeriveOfficialOpenAIHTTPIdentityDoesNotRetainRequestContent(t *testing.T) {
	const requests = 300
	const textBytes = 64 * 1024
	account := newOfficialOpenAIHTTPTestAccount(94999)
	filler := strings.Repeat("y", textBytes)

	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	for index := 0; index < requests; index++ {
		body, err := json.Marshal(map[string]any{
			"model": "gpt-5.4",
			"input": []any{map[string]any{
				"type":    "message",
				"role":    "user",
				"content": "request-" + strconv.Itoa(index) + " " + filler,
			}},
		})
		require.NoError(t, err)
		contract, err := captureGeneratedOfficialOpenAIHTTPBodyContract(body)
		require.NoError(t, err)
		identity, err := deriveOfficialOpenAIHTTPIdentity(
			nil, account, body, contract, officialClientProfileModeActive,
		)
		require.NoError(t, err)
		require.NoError(t, uuid.Validate(identity.sessionID))
		require.NoError(t, uuid.Validate(identity.turnID))
	}
	runtime.GC()
	runtime.ReadMemStats(&after)

	var growth uint64
	if after.HeapAlloc > before.HeapAlloc {
		growth = after.HeapAlloc - before.HeapAlloc
	}
	require.Less(
		t,
		growth,
		uint64(4*1024*1024),
		"GC 后堆增长 %d 字节；旧实现会把 %d 份约 %d 字节的正文长期留在缓存键中",
		growth,
		requests*2,
		textBytes,
	)
}

// TestOfficialEgressPackageLevelMapsAreAudited 是问题五的静态守卫：官方出站层任何
// 新增的包级 map 缓存都必须在这里登记键的构成，禁止再以请求正文、用户消息、工具
// 结果或完整 JSON 作为键。
func TestOfficialEgressPackageLevelMapsAreAudited(t *testing.T) {
	audited := map[string]string{
		"officialOpenAIHTTPTurnStarts":           "键为 账号ID|session UUID|turn UUID，带 2 小时 TTL 与 8192 条上限",
		"officialOpenAIProjectedFieldsSeen":      "键为被投影字段名列表，256 条封顶",
		"officialCodexAccessProgramsWarned":      "键为 拒绝原因|程序名：原因取自固定的 5 个值，程序名只有不超过 32 字节的小写字母、数字、下划线才回显，其余记为 other，64 条封顶",
		"officialCodexTrustedConditionalHeaders": "静态常量表",
		"officialOpenAICompactionReasons":        "静态常量表",
	}
	files, err := filepath.Glob("official_egress*.go")
	require.NoError(t, err)
	require.NotEmpty(t, files)

	var offenders []string
	fileSet := token.NewFileSet()
	for _, path := range files {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		parsed, err := parser.ParseFile(fileSet, path, nil, parser.SkipObjectResolution)
		require.NoError(t, err)
		for _, decl := range parsed.Decls {
			genDecl, ok := decl.(*ast.GenDecl)
			if !ok || genDecl.Tok != token.VAR {
				continue
			}
			for _, spec := range genDecl.Specs {
				valueSpec, ok := spec.(*ast.ValueSpec)
				if !ok {
					continue
				}
				hasMap := testASTContainsMapType(valueSpec.Type)
				for _, value := range valueSpec.Values {
					if literal, ok := value.(*ast.CompositeLit); ok {
						hasMap = hasMap || testASTContainsMapType(literal.Type)
					}
				}
				if !hasMap {
					continue
				}
				for _, name := range valueSpec.Names {
					if _, ok := audited[name.Name]; ok {
						continue
					}
					offenders = append(offenders, path+": "+name.Name)
				}
			}
		}
	}
	require.Empty(
		t,
		offenders,
		"官方出站层新增了未登记的包级 map：%v。请确认其键不含请求正文、用户消息、"+
			"工具结果或完整 JSON（内容只能以固定长度摘要进入），并在本测试登记键的构成",
		offenders,
	)
}

func testASTContainsMapType(expr ast.Expr) bool {
	if expr == nil {
		return false
	}
	found := false
	ast.Inspect(expr, func(node ast.Node) bool {
		if _, ok := node.(*ast.MapType); ok {
			found = true
			return false
		}
		return !found
	})
	return found
}
