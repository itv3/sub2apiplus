package service

import (
	"sort"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

// 回合 id 与回合开始时间的官方形态测试。
//
// 官方 turn_id 来自 Uuid::now_v7()（Rust uuid 库，官方 Cargo.lock 锁定的版本）；turn_started_at_unix_ms 比 turn_id 内嵌毫秒晚 1～4 毫秒。
// 位布局的依据是 Rust uuid 库源码（v7.rs 的 new_v7、timestamp.rs 的 encode_unix_timestamp_millis 与 ContextV7），
// 并用本地官方实采里的 287 个 UUIDv7 交叉验证了“rand_a 第 2、3 位恒为 0”这一特征。

// officialCodexTurnClockForTest 固定回合 id 的时钟与随机源，并在测试结束后还原。
func officialCodexTurnClockForTest(t *testing.T, now time.Time) {
	t.Helper()
	originalClock, originalRandom := officialCodexTurnClock, officialCodexTurnRandom
	originalContext := officialCodexTurnUUIDContext
	officialCodexTurnClock = func() time.Time { return now }
	var counter byte
	officialCodexTurnRandom = func(p []byte) error {
		for i := range p {
			counter++
			p[i] = counter * 37
		}
		return nil
	}
	officialCodexTurnUUIDContext = &officialCodexUUIDV7Context{}
	t.Cleanup(func() {
		officialCodexTurnClock, officialCodexTurnRandom = originalClock, originalRandom
		officialCodexTurnUUIDContext = originalContext
	})
}

func officialCodexEmbeddedMs(id uuid.UUID) int64 {
	return int64(id[0])<<40 | int64(id[1])<<32 | int64(id[2])<<24 | int64(id[3])<<16 | int64(id[4])<<8 | int64(id[5])
}

func officialCodexRandA(id uuid.UUID) uint16 { return (uint16(id[6])<<8 | uint16(id[7])) & 0x0fff }

func TestOfficialCodexTurnUUIDV7MatchesOfficialLayout(t *testing.T) {
	now := time.UnixMilli(1793036419100)
	officialCodexTurnClockForTest(t, now)

	for i := 0; i < 5000; i++ {
		text := newOfficialCodexTurnUUIDV7()
		parsed, err := uuid.Parse(text)
		require.NoError(t, err)
		require.Equal(t, text, parsed.String(), "必须是小写规范文本")
		require.EqualValues(t, 7, parsed.Version())
		require.Equal(t, uuid.RFC4122, parsed.Variant())
		require.Equal(t, now.UnixMilli()-officialCodexTurnSubmitLeadMs, officialCodexEmbeddedMs(parsed))
		// 官方 id 的 rand_a 第 2、3 位恒为 0。
		require.Zero(t, (officialCodexRandA(parsed)>>2)&3, "id=%s", text)
	}
}

func TestOfficialCodexTurnUUIDV7SamplesFromOfficialCaptureSatisfyTheLayout(t *testing.T) {
	// 官方客户端实采的回合 id（本地非正式实采，exec 与 app-server 场景）。
	for _, text := range []string{
		"01a1794c-8017-7560-8816-ae77f473a5ee",
		"01a1793d-088b-7da3-a56f-d5aad9041eb0",
		"01a17935-da56-7f41-a0c4-b36390804a73",
		"01a17937-48cd-75a0-904d-d4b0a61e412b",
		"01a159e0-3d30-79b2-8f87-e817382e3cfb", // 真实上游实采
	} {
		parsed, err := uuid.Parse(text)
		require.NoError(t, err)
		require.EqualValues(t, 7, parsed.Version())
		require.Equal(t, uuid.RFC4122, parsed.Variant())
		require.Zero(t, (officialCodexRandA(parsed)>>2)&3, "官方样本 %s 也满足布局特征", text)
	}
}

func TestOfficialCodexTurnUUIDV7IsMonotonicAndHandlesClockMovement(t *testing.T) {
	now := time.UnixMilli(1793036419100)
	officialCodexTurnClockForTest(t, now)

	var ids []string
	for i := 0; i < 2000; i++ {
		ids = append(ids, newOfficialCodexTurnUUIDV7())
	}
	require.True(t, sort.StringsAreSorted(ids), "同一毫秒内按计数器单调递增")
	seen := map[string]bool{}
	for _, id := range ids {
		require.False(t, seen[id], "id 不重复")
		seen[id] = true
	}

	// 时钟前进：内嵌毫秒随之前进；时钟回退：沿用上次的毫秒，仍然单调。
	officialCodexTurnClock = func() time.Time { return now.Add(10 * time.Millisecond) }
	forward, err := uuid.Parse(newOfficialCodexTurnUUIDV7())
	require.NoError(t, err)
	require.Equal(t, now.UnixMilli()+10-officialCodexTurnSubmitLeadMs, officialCodexEmbeddedMs(forward))
	officialCodexTurnClock = func() time.Time { return now }
	back, err := uuid.Parse(newOfficialCodexTurnUUIDV7())
	require.NoError(t, err)
	require.Equal(t, officialCodexEmbeddedMs(forward), officialCodexEmbeddedMs(back), "时钟回退时沿用上次的毫秒")
}

func TestOfficialCodexTurnStartedAtIsDerivedFromTurnIDAndNeverLaterThanTheClock(t *testing.T) {
	now := time.UnixMilli(1793036419100)
	officialCodexTurnClockForTest(t, now)

	for i := 0; i < 2000; i++ {
		// 每次推进时钟，避免全部落在同一毫秒。
		current := now.Add(time.Duration(i) * 3 * time.Millisecond)
		officialCodexTurnClock = func() time.Time { return current }
		turnID := newOfficialCodexTurnUUIDV7()
		startedAt, ok := officialCodexTurnStartedAtMs(turnID)
		require.True(t, ok)
		parsed, err := uuid.Parse(turnID)
		require.NoError(t, err)
		gap := startedAt - officialCodexEmbeddedMs(parsed)
		require.GreaterOrEqual(t, gap, int64(1))
		require.LessOrEqual(t, gap, int64(4))
		require.LessOrEqual(t, startedAt, current.UnixMilli(), "开始时间不晚于 id 生成时刻")
		again, _ := officialCodexTurnStartedAtMs(turnID)
		require.Equal(t, startedAt, again, "同一 turn_id 的开始时间稳定")
	}
}

func TestOfficialCodexTurnStartGapFollowsTheOfficialSampleDistribution(t *testing.T) {
	const total = 78000
	counts := map[int64]int{}
	for i := 0; i < total; i++ {
		id, err := uuid.NewV7()
		require.NoError(t, err)
		counts[officialCodexTurnStartGapMs(id.String())]++
	}
	var sampleTotal float64
	for _, sample := range officialCodexTurnStartGapSamples {
		sampleTotal += float64(sample.count)
	}
	for _, sample := range officialCodexTurnStartGapSamples {
		want := float64(sample.count) / sampleTotal
		got := float64(counts[sample.gapMs]) / total
		require.InDelta(t, want, got, 0.015, "gap=%dms", sample.gapMs)
	}
	require.Len(t, counts, len(officialCodexTurnStartGapSamples), "不应出现分布之外的间隔")
	require.Equal(t, uint64(39), func() uint64 {
		var sum uint64
		for _, sample := range officialCodexTurnStartGapSamples {
			sum += sample.count
		}
		return sum
	}(), "样本总数与实采一致（37 + 2）")
	// 平均间隔应接近样本均值 74/39 毫秒（约 1.9）。
	var mean float64
	for gap, count := range counts {
		mean += float64(gap) * float64(count)
	}
	require.InDelta(t, 74.0/39.0, mean/total, 0.05)
}

func TestOfficialCodexTurnStartedAtMsRejectsNonV7(t *testing.T) {
	for _, value := range []string{"", "not-a-uuid", uuid.NewString(), "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"} {
		_, ok := officialCodexTurnStartedAtMs(value)
		require.False(t, ok, "value=%q", value)
	}
	before := time.Now().UnixMilli()
	got := officialCodexTurnStartedAtMsOrNow("not-a-uuid")
	require.GreaterOrEqual(t, got, before)
}

func TestOfficialCodexInvocationTurnIDIsStablePerInvocationAndOfficialShaped(t *testing.T) {
	first := officialCodexInvocationTurnID("invocation-a")
	second := officialCodexInvocationTurnID("invocation-a")
	other := officialCodexInvocationTurnID("invocation-b")
	require.Equal(t, first, second, "同一次调用的多次 attempt 回合 id 必须一致")
	require.NotEqual(t, first, other)
	parsed, err := uuid.Parse(first)
	require.NoError(t, err)
	require.EqualValues(t, 7, parsed.Version())
	require.Equal(t, uuid.RFC4122, parsed.Variant())
	require.Zero(t, (officialCodexRandA(parsed)>>2)&3)
}

func TestGenerateOfficialStableTurnUUIDV7IsStableAndUsesOfficialLayout(t *testing.T) {
	key := newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).WriteString("thread-x").WriteString("anchor-1").Key()
	first := generateOfficialStableTurnUUIDV7(key)
	require.Equal(t, first, generateOfficialStableTurnUUIDV7(key))
	parsed, err := uuid.Parse(first)
	require.NoError(t, err)
	require.EqualValues(t, 7, parsed.Version())
	require.Zero(t, (officialCodexRandA(parsed)>>2)&3)
}

// session、thread、window、at_／msg_ item id 等非回合 id 也必须是官方位布局（rand_a 第 2、3 位恒为 0）。
func TestOfficialStableUUIDV7ForSessionAndItemIDsUsesOfficialLayout(t *testing.T) {
	// 官方实采样本（各版一致）：线程／会话 id，以及 at_／msg_ item id 的 UUID 部分。
	for _, text := range []string{
		"01a17935-c8ae-7882-9b88-60f5c3ad6e5b",
		"01a17935-5c98-7f51-995d-9105098f672a",
		"01a17935-5c98-7f51-995d-9110469c51d5",
		"01a1794c-7ff8-77a1-a7b0-c82436e27910",
		"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95",
	} {
		parsed, err := uuid.Parse(text)
		require.NoError(t, err)
		require.Zero(t, (officialCodexRandA(parsed)>>2)&3, text)
	}
	seen := map[string]bool{}
	for i := 0; i < 3000; i++ {
		key := newOfficialUUIDV7Seed(officialUUIDV7DomainSession).WriteInt(i).Key()
		text := generateOfficialStableUUIDV7(key)
		require.Equal(t, text, generateOfficialStableUUIDV7(key), "同一个键必须稳定")
		parsed, err := uuid.Parse(text)
		require.NoError(t, err)
		require.EqualValues(t, 7, parsed.Version())
		require.Equal(t, uuid.RFC4122, parsed.Variant())
		require.Zero(t, (officialCodexRandA(parsed)>>2)&3, "id=%s", text)
		require.False(t, seen[text])
		seen[text] = true
	}
}

// 派生出的 HTTP 身份里 session／thread／turn 都满足官方布局，且共用同一个单调上下文：先生成的 id 字典序不大于后生成的。
func TestDerivedHTTPIdentityIDsUseOfficialLayout(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94777)
	body := []byte(`{"model":"gpt-5.4","input":"official-layout-94777"}`)
	contract, err := captureGeneratedOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(nil, account, body, contract, officialClientProfileModeActive)
	require.NoError(t, err)
	for _, text := range []string{identity.sessionID, identity.turnID} {
		parsed, parseErr := uuid.Parse(text)
		require.NoError(t, parseErr)
		require.EqualValues(t, 7, parsed.Version())
		require.Zero(t, (officialCodexRandA(parsed)>>2)&3, text)
	}
	sessionID, err := uuid.Parse(identity.sessionID)
	require.NoError(t, err)
	turnID, err := uuid.Parse(identity.turnID)
	require.NoError(t, err)
	require.LessOrEqual(t, officialCodexEmbeddedMs(sessionID), officialCodexEmbeddedMs(turnID), "会话 id 不晚于它的第一个回合 id")
}
