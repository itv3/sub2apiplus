//go:build unit

package service

// Composite 同级按剩余容量随机选择、满载换号与失败冷却的单测。
// 随机源全部使用固定值，断言是确定性的，不依赖统计阈值。

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// capacityTestCache 是带真实槽位语义的并发缓存桩：按账号记录占用的请求 ID，
// 上限由抢槽时传入的 maxConcurrency 决定；forceAcquireFail 可让"快照有空位但实际抢槽失败"。
type capacityTestCache struct {
	ConcurrencyCache
	mu               sync.Mutex
	used             map[int64]map[string]struct{}
	forceAcquireFail map[int64]bool
	acquireAttempts  map[int64]int
}

func newCapacityTestCache() *capacityTestCache {
	return &capacityTestCache{
		used:             map[int64]map[string]struct{}{},
		forceAcquireFail: map[int64]bool{},
		acquireAttempts:  map[int64]int{},
	}
}

func (c *capacityTestCache) AcquireAccountSlot(_ context.Context, accountID int64, maxConcurrency int, requestID string) (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.acquireAttempts[accountID]++
	if c.forceAcquireFail[accountID] {
		return false, nil
	}
	if c.used[accountID] == nil {
		c.used[accountID] = map[string]struct{}{}
	}
	if maxConcurrency > 0 && len(c.used[accountID]) >= maxConcurrency {
		return false, nil
	}
	c.used[accountID][requestID] = struct{}{}
	return true, nil
}

func (c *capacityTestCache) ReleaseAccountSlot(_ context.Context, accountID int64, requestID string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.used[accountID], requestID)
	return nil
}

func (c *capacityTestCache) GetAccountsLoadBatch(_ context.Context, accounts []AccountWithConcurrency) (map[int64]*AccountLoadInfo, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[int64]*AccountLoadInfo, len(accounts))
	for _, account := range accounts {
		current := len(c.used[account.ID])
		rate := 0
		if account.MaxConcurrency > 0 {
			rate = current * 100 / account.MaxConcurrency
		}
		out[account.ID] = &AccountLoadInfo{AccountID: account.ID, CurrentConcurrency: current, LoadRate: rate}
	}
	return out, nil
}

func (c *capacityTestCache) GetAccountWaitingCount(_ context.Context, _ int64) (int, error) {
	return 0, nil
}

func (c *capacityTestCache) inUse(accountID int64) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.used[accountID])
}

// compositeCapacityRepo 让候选池夹具的账号目录同时能被平台内调度器列出（按平台过滤）。
type compositeCapacityRepo struct {
	*compositePoolLister
}

func (r compositeCapacityRepo) snapshot() []Account {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Account(nil), r.accounts...)
}

func (r compositeCapacityRepo) ListSchedulableByGroupIDAndPlatform(_ context.Context, _ int64, platform string) ([]Account, error) {
	var out []Account
	for _, account := range r.snapshot() {
		if account.Platform == platform && account.IsSchedulable() {
			out = append(out, account)
		}
	}
	return out, nil
}

func (r compositeCapacityRepo) ListSchedulableByPlatform(ctx context.Context, platform string) ([]Account, error) {
	return r.ListSchedulableByGroupIDAndPlatform(ctx, 0, platform)
}

func (r compositeCapacityRepo) ListSchedulableUngroupedByPlatform(_ context.Context, _ string) ([]Account, error) {
	return nil, nil
}

func (r compositeCapacityRepo) ListSchedulableByGroupID(_ context.Context, _ int64) ([]Account, error) {
	var out []Account
	for _, account := range r.snapshot() {
		if account.IsSchedulable() {
			out = append(out, account)
		}
	}
	return out, nil
}

// compositeCapacityFixture 在候选池夹具上接入有状态的并发缓存与可被调度器列出的账号仓储。
type compositeCapacityFixture struct {
	*compositePoolFixture
	slots *capacityTestCache
	repo  compositeCapacityRepo
	// randValue 是候选池的固定随机源返回值（按 n 取模，保证落在 [0,n)）。
	randValue int64
}

func newCompositeCapacityFixture(t *testing.T, accounts ...Account) *compositeCapacityFixture {
	t.Helper()
	f := &compositeCapacityFixture{compositePoolFixture: newCompositePoolFixture(t, accounts...)}
	f.slots = newCapacityTestCache()
	f.repo = compositeCapacityRepo{f.lister}
	service := NewConcurrencyService(f.slots)
	// 关闭负载快照缓存：测试里槽位一变，下一次读取必须立刻看到。
	service.SetAccountLoadBatchCacheTTL(0)
	f.openAI.concurrencyService = service
	f.gateway.concurrencyService = service
	f.openAI.accountRepo = f.repo
	f.gateway.accountRepo = f.repo
	f.pool.capacityRand = func(n int64) int64 { return f.randValue % n }
	return f
}

// withCapacityAccount 给账号设置并发上限。
func withCapacityAccount(account Account, concurrency int) Account {
	account.Concurrency = concurrency
	return account
}

// ---- 纯排序：同级随机、满载下沉 ----

func capacityCandidates(accounts ...Account) []*compositeCandidate {
	out := make([]*compositeCandidate, 0, len(accounts))
	for i := range accounts {
		account := accounts[i]
		out = append(out, &compositeCandidate{account: &account, zeroConversion: true})
	}
	return out
}

func capacityOrderIDs(ordered []*compositeCandidate) []int64 {
	ids := make([]int64, 0, len(ordered))
	for _, candidate := range ordered {
		ids = append(ids, candidate.account.ID)
	}
	return ids
}

func TestCompositeCapacityOrderEqualCapacityIsEquallyLikely(t *testing.T) {
	// 两个同级账号剩余容量都是 10：总权重 20，落点 [0,10) 选前者、[10,20) 选后者，
	// 把全部 20 个落点逐一代入，两边各得 10 次，即等概率（不依赖随机数统计）。
	group := capacityCandidates(
		compositePoolAdaptive(1, PlatformKimi, 5),
		compositePoolAdaptive(2, PlatformDeepseek, 5),
	)
	capacities := map[int64]compositeAccountCapacity{
		1: {Remaining: 10},
		2: {Remaining: 10},
	}
	firstCount := map[int64]int{}
	for point := int64(0); point < 20; point++ {
		draws := 0
		ordered := compositeCapacityOrder(group, capacities, func(n int64) int64 {
			draws++
			require.Equal(t, int64(20), n, "抽取的区间应为两账号剩余容量之和")
			return point
		})
		require.Equal(t, 1, draws, "两个账号只需抽取一次，最后一个不再抽")
		require.Len(t, ordered, 2)
		firstCount[ordered[0].account.ID]++
	}
	require.Equal(t, map[int64]int{1: 10, 2: 10}, firstCount)
}

func TestCompositeCapacityOrderWeightsByRemainingCapacity(t *testing.T) {
	// 剩余容量 2 与 8：落点 [0,2) 选 1 号，[2,10) 选 2 号。
	group := capacityCandidates(
		compositePoolAdaptive(1, PlatformKimi, 5),
		compositePoolAdaptive(2, PlatformDeepseek, 5),
	)
	capacities := map[int64]compositeAccountCapacity{1: {Remaining: 2}, 2: {Remaining: 8}}
	firstCount := map[int64]int{}
	for point := int64(0); point < 10; point++ {
		ordered := compositeCapacityOrder(group, capacities, func(int64) int64 { return point })
		firstCount[ordered[0].account.ID]++
	}
	require.Equal(t, map[int64]int{1: 2, 2: 8}, firstCount)
}

func TestCompositeCapacityOrderFullAccountsSinkAndNextPriorityFollows(t *testing.T) {
	group := capacityCandidates(
		compositePoolAdaptive(1, PlatformKimi, 1),     // 一级，满载
		compositePoolAdaptive(2, PlatformDeepseek, 1), // 一级，满载
		compositePoolAdaptive(3, PlatformZhipu, 2),    // 二级，有空位
		compositePoolAdaptive(4, PlatformMiniMax, 2),  // 二级，有空位
	)
	capacities := map[int64]compositeAccountCapacity{
		1: {Remaining: 0}, 2: {Remaining: 0}, 3: {Remaining: 3}, 4: {Remaining: 3},
	}
	ordered := compositeCapacityOrder(group, capacities, func(int64) int64 { return 0 })
	// 一级全满：先二级的空位账号，满载账号垫底并保持原顺序。
	require.Equal(t, []int64{3, 4, 1, 2}, capacityOrderIDs(ordered))
}

func TestCompositeCapacityOrderSameLevelBeforeNextPriority(t *testing.T) {
	// 一级里 1 号满载、2 号有空位，二级的 3 号也有空位：同级的 2 号必须排在 3 号之前。
	group := capacityCandidates(
		compositePoolAdaptive(1, PlatformKimi, 1),
		compositePoolAdaptive(2, PlatformDeepseek, 1),
		compositePoolAdaptive(3, PlatformZhipu, 2),
	)
	capacities := map[int64]compositeAccountCapacity{
		1: {Remaining: 0}, 2: {Remaining: 1}, 3: {Remaining: 100},
	}
	for point := int64(0); point < 5; point++ {
		ordered := compositeCapacityOrder(group, capacities, func(n int64) int64 { return point % n })
		require.Equal(t, []int64{2, 3, 1}, capacityOrderIDs(ordered))
	}
}

func TestCompositeCapacityOrderAllFullKeepsOriginalOrder(t *testing.T) {
	group := capacityCandidates(
		compositePoolAdaptive(1, PlatformKimi, 1),
		compositePoolAdaptive(2, PlatformDeepseek, 1),
		compositePoolAdaptive(3, PlatformZhipu, 2),
	)
	capacities := map[int64]compositeAccountCapacity{1: {}, 2: {}, 3: {}}
	ordered := compositeCapacityOrder(group, capacities, func(int64) int64 {
		t.Fatal("全部满载时不应抽取随机数")
		return 0
	})
	require.Equal(t, []int64{1, 2, 3}, capacityOrderIDs(ordered), "全部满载时顺序与原排序完全一致，请求照旧在首选账号上排队")
}

func TestCompositeCapacityOfUnlimitedAndLimited(t *testing.T) {
	limited := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 1), 10)
	require.Equal(t, 6, compositeCapacityOf(&limited, &AccountLoadInfo{CurrentConcurrency: 3, WaitingCount: 1}).Remaining)
	require.Equal(t, 0, compositeCapacityOf(&limited, &AccountLoadInfo{CurrentConcurrency: 12}).Remaining, "超载不出现负数")
	require.Equal(t, 10, compositeCapacityOf(&limited, nil).Remaining, "没有负载信息按空闲处理，与调度器口径一致")

	unlimited := withCapacityAccount(compositePoolAdaptive(2, PlatformKimi, 1), 0)
	capacity := compositeCapacityOf(&unlimited, &AccountLoadInfo{CurrentConcurrency: 99})
	require.True(t, capacity.Unlimited)
	require.Greater(t, capacity.weight(), int64(0), "无上限账号永远不会满载")
}

// ---- 候选池解析：容量感知的首选平台 ----

func TestCompositeCapacityPoolPicksIdleSideWhenOtherIsFull(t *testing.T) {
	// 验收 2：同级两个账号，一边满载、另一边空闲，新请求走空闲账号，不在原账号上等。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 2)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 2)
	f := newCompositeCapacityFixture(t, a, b)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)

	// 1 号占满：无论随机落点在哪，首选都是 2 号所在的 DeepSeek。
	for i := 0; i < 2; i++ {
		ok, err := f.slots.AcquireAccountSlot(context.Background(), 1, 2, fmt.Sprintf("held-%d", i))
		require.NoError(t, err)
		require.True(t, ok)
	}
	for point := int64(0); point < 4; point++ {
		f.randValue = point
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Nil(t, outcome.Decision.CapacityPlan, "另一边满载，没有可换的备选平台")
	}
}

func TestCompositeCapacityPoolSkipsCooledDownSide(t *testing.T) {
	// 验收 2（冷却版）：一边进入临时不可调度冷却，另一边空闲，新请求走空闲账号。
	until := time.Now().Add(30 * time.Second)
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 2)
	a.TempUnschedulableUntil = &until
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 2)
	f := newCompositeCapacityFixture(t, a, b)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	for point := int64(0); point < 4; point++ {
		f.randValue = point
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
	}

	// 冷却到期后重新允许选择：两边都有空位，落点决定首选。
	past := time.Now().Add(-time.Second)
	f.lister.update(1, func(account *Account) { account.TempUnschedulableUntil = &past })
	f.randValue = 0
	require.Equal(t, PlatformKimi, f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions).Decision.TargetPlatform)
	f.randValue = 3
	require.Equal(t, PlatformDeepseek, f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions).Decision.TargetPlatform)
}

func TestCompositeCapacityPoolSameCapacityFixedRandomSource(t *testing.T) {
	// 验收 4：容量相同，固定随机源下首选是确定的，并且换号计划指向另一平台。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 10)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 10)
	f := newCompositeCapacityFixture(t, a, b)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)

	counts := map[string]int{}
	for point := int64(0); point < 20; point++ {
		f.randValue = point
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		counts[outcome.Decision.TargetPlatform]++
		require.NotNil(t, outcome.Decision.CapacityPlan)
		require.Len(t, outcome.Decision.CapacityPlan.Alternates, 1)
		other := PlatformDeepseek
		if outcome.Decision.TargetPlatform == PlatformDeepseek {
			other = PlatformKimi
		}
		require.Equal(t, other, outcome.Decision.CapacityPlan.Alternates[0].Platform)
		require.False(t, outcome.Decision.CapacityPlan.Temporary)
	}
	require.Equal(t, map[string]int{PlatformKimi: 10, PlatformDeepseek: 10}, counts)
}

func TestCompositeCapacityPoolAllFullKeepsLegacyPrimary(t *testing.T) {
	// 全部满载：沿用原排序（账号 ID 最小的 Kimi），请求在它身上排队，不下发换号计划。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 1)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 1)
	f := newCompositeCapacityFixture(t, a, b)
	for id := int64(1); id <= 2; id++ {
		ok, err := f.slots.AcquireAccountSlot(context.Background(), id, 1, fmt.Sprintf("held-%d", id))
		require.NoError(t, err)
		require.True(t, ok)
	}
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	for point := int64(0); point < 4; point++ {
		f.randValue = point
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
		require.Nil(t, outcome.Decision.CapacityPlan)
	}
}

func TestCompositeCapacityPoolSwitchOffKeepsLegacyBehavior(t *testing.T) {
	// 验收 6：开关关闭时行为与现状一致——无论随机落点如何，首选都是账号 ID 最小的平台，也没有换号计划。
	t.Setenv(compositeCapacitySelectionEnv, "off")
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 10)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 10)
	f := newCompositeCapacityFixture(t, a, b)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	for point := int64(0); point < 20; point++ {
		f.randValue = point
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
		require.Nil(t, outcome.Decision.CapacityPlan)
	}
}

func TestCompositeCapacityPoolWithoutConcurrencyServiceKeepsLegacyBehavior(t *testing.T) {
	// 没有并发服务（读不到负载）时不能拿"全部空闲"的假数据去随机，退回确定性排序。
	f := newCompositePoolFixture(t,
		withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 10),
		withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 10),
	)
	f.pool.capacityRand = func(n int64) int64 { return n - 1 }
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.Nil(t, outcome.Decision.CapacityPlan)
}

func TestCompositeCapacityPoolMembersCoverWholePlatformGroup(t *testing.T) {
	// 首选平台的候选集合仍是该平台在承接分组内的全部账号（平台内调度器继续按原策略选号）。
	k1 := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 4)
	k2 := withCapacityAccount(compositePoolAdaptive(2, PlatformKimi, 9), 4)
	d1 := withCapacityAccount(compositePoolAdaptive(3, PlatformDeepseek, 5), 4)
	f := newCompositeCapacityFixture(t, k1, k2, d1)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	f.randValue = 0
	outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1, 2}, outcome.Decision.CandidateAccountIDs)
	require.Equal(t, []int64{3}, outcome.Decision.CapacityPlan.Alternates[0].AccountIDs)
}

func TestCompositeCapacityPoolStickyRequestGetsTemporaryPlanOnly(t *testing.T) {
	// 普通会话命中粘性：首选平台仍是绑定账号所在平台，只多一份"临时借用"的换号计划。
	k := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 30), 4)
	d := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 4)
	f := newCompositeCapacityFixture(t, k, d)
	f.bindOpenAISticky("sess-cap", 1)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), compositePoolSessionHeader("sess-cap"))
	outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathSticky, outcome.Path)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.NotNil(t, outcome.Decision.CapacityPlan)
	require.True(t, outcome.Decision.CapacityPlan.Temporary)
	require.Equal(t, PlatformDeepseek, outcome.Decision.CapacityPlan.Alternates[0].Platform)
}

func TestCompositeCapacityPoolContinuationNeverGetsPlan(t *testing.T) {
	// 续链请求锁定归属账号，不下发换号计划，也不参与容量随机。
	owner := withCapacityAccount(compositePoolOpenAI(1, 30), 4)
	other := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 4)
	f := newCompositeCapacityFixture(t, owner, other)
	require.NoError(t, f.store.BindResponseAccount(context.Background(), compositePoolGroupID, "resp_cap_1", 1, time.Hour))
	ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(`,"previous_response_id":"resp_cap_1"`), nil)
	outcome := f.resolveHandled(ctx, CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathContinuation, outcome.Path)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	require.Nil(t, outcome.Decision.CapacityPlan)
}

// ---- 整链：候选池 → 平台内调度器 → 满载换号 ----

// chainResult 是一次整链选号的结果。
type chainResult struct {
	selection *AccountSelectionResult
	// platform 是最终服务该请求的平台；switched 表示经过了满载换号。
	platform string
	switched bool
	decision CompositeRouteDecision
}

// chainSelect 模拟处理器的一次选号：候选池解析 → 平台内调度器选号 → 需要排队时按计划换号。
// 抢到槽位的选号结果由调用方负责 ReleaseFunc。
func (f *compositeCapacityFixture) chainSelect(header map[string][]string, hash string) chainResult {
	f.t.Helper()
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), header), CompositeRouteEndpointChatCompletions)
	groupID := compositePoolGroupID
	ctx := WithCompositeRouteDecision(context.Background(), outcome.Decision)
	platform := outcome.Decision.TargetPlatform
	selection, _, err := f.openAI.SelectAccountWithSchedulerForCapability(
		ctx, &groupID, "", hash, compositePoolModel, nil,
		OpenAIUpstreamTransportAny, OpenAIEndpointCapabilityChatCompletions, false, false, true, platform,
	)
	require.NoError(f.t, err)
	require.NotNil(f.t, selection)
	require.NotNil(f.t, selection.Account)
	result := chainResult{selection: selection, platform: platform, decision: outcome.Decision}
	if !selection.Acquired {
		sw, ok := f.openAI.CompositeCapacityFallback(ctx, selection, CompositeCapacityFallbackInput{
			GroupID: &groupID, SessionHash: hash, Model: compositePoolModel,
			CapabilityFor: func(string) OpenAIEndpointCapability { return OpenAIEndpointCapabilityChatCompletions },
		})
		if ok {
			result.selection, result.platform, result.switched = sw.Selection, sw.Platform, true
		}
	}
	return result
}

func TestCompositeCapacityChainTwentyRequestsFillBothSidesThenTwentyFirstWaits(t *testing.T) {
	// 验收 1：两个同级账号各允许 10 并发。20 个受控请求分别占满两边（无论随机落点怎样偏），
	// 第 21 个才需要等待（只拿到排队计划，没有抢到槽位）。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 10)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 10)
	f := newCompositeCapacityFixture(t, a, b)

	// 随机落点故意恒为 0（总是偏向 1 号 Kimi）：早期的请求会塞满 Kimi，之后 Kimi 满载，
	// 候选池按容量改选 DeepSeek；整个过程不依赖随机数的好坏。
	f.randValue = 0
	var releases []func()
	for i := 1; i <= 20; i++ {
		result := f.chainSelect(nil, fmt.Sprintf("req-%d", i))
		require.True(t, result.selection.Acquired, "第 %d 个请求应当当场抢到槽位", i)
		releases = append(releases, result.selection.ReleaseFunc)
	}
	require.Equal(t, 10, f.slots.inUse(1), "Kimi 账号恰好占满 10 个槽位")
	require.Equal(t, 10, f.slots.inUse(2), "DeepSeek 账号恰好占满 10 个槽位")

	twentyFirst := f.chainSelect(nil, "req-21")
	require.False(t, twentyFirst.selection.Acquired, "第 21 个请求才需要等待")
	require.NotNil(t, twentyFirst.selection.WaitPlan)
	require.False(t, twentyFirst.switched)
	require.Equal(t, 10, f.slots.inUse(1))
	require.Equal(t, 10, f.slots.inUse(2), "排队请求不占用槽位，不能突破并发上限")

	// 释放一个槽位后，下一个请求当场拿到。
	releases[0]()
	again := f.chainSelect(nil, "req-22")
	require.True(t, again.selection.Acquired)
	again.selection.ReleaseFunc()
	for _, release := range releases[1:] {
		release()
	}
	require.Equal(t, 0, f.slots.inUse(1)+f.slots.inUse(2), "槽位全部释放")
}

func TestCompositeCapacityChainFullSideDoesNotWait(t *testing.T) {
	// 验收 2：一边满载，另一边空闲，新请求走空闲账号。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 1)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 1)
	f := newCompositeCapacityFixture(t, a, b)
	ok, err := f.slots.AcquireAccountSlot(context.Background(), 1, 1, "held")
	require.NoError(t, err)
	require.True(t, ok)

	f.randValue = 0
	result := f.chainSelect(nil, "req-1")
	require.True(t, result.selection.Acquired)
	require.Equal(t, int64(2), result.selection.Account.ID)
	require.False(t, result.switched, "候选池已经直接选了空闲平台，不需要换号")
	result.selection.ReleaseFunc()
}

func TestCompositeCapacityChainActualAcquireFailureSwitchesPlatform(t *testing.T) {
	// 验收 3：负载快照显示 Kimi 账号有空位，但实际抢槽失败（别的请求在快照之后抢走了），
	// 同级的 DeepSeek 账号空闲：换过去，而不是在 Kimi 上排队。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 2)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 2)
	f := newCompositeCapacityFixture(t, a, b)
	f.slots.forceAcquireFail[1] = true // 快照里 1 号是空的，真正抢槽却失败

	f.randValue = 0 // 落点偏向 Kimi：首选必为 Kimi
	result := f.chainSelect(nil, "req-actual-fail")
	require.Equal(t, PlatformKimi, result.decision.TargetPlatform, "快照有空位，候选池选了 Kimi")
	require.True(t, result.switched, "实际抢槽失败后应换到另一平台")
	require.Equal(t, PlatformDeepseek, result.platform)
	require.True(t, result.selection.Acquired)
	require.Equal(t, int64(2), result.selection.Account.ID)
	require.Equal(t, 1, f.slots.inUse(2))
	require.Equal(t, 0, f.slots.inUse(1))
	result.selection.ReleaseFunc()
	require.Equal(t, 0, f.slots.inUse(2), "释放后槽位归零，没有重复占用")
}

func TestCompositeCapacityChainNextPriorityBeforeQueueing(t *testing.T) {
	// 一级两个账号都满载、二级账号空闲：先用二级，全部满载才排队。
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 1), 1)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 1), 1)
	c := withCapacityAccount(compositePoolAdaptive(3, PlatformZhipu, 9), 1)
	f := newCompositeCapacityFixture(t, a, b, c)
	for id := int64(1); id <= 2; id++ {
		ok, err := f.slots.AcquireAccountSlot(context.Background(), id, 1, fmt.Sprintf("held-%d", id))
		require.NoError(t, err)
		require.True(t, ok)
	}
	result := f.chainSelect(nil, "req-prio")
	require.True(t, result.selection.Acquired)
	require.Equal(t, int64(3), result.selection.Account.ID)

	// 三个账号都满载：排队，且仍在首选（账号 ID 最小）的 Kimi 上。
	waiting := f.chainSelect(nil, "req-wait")
	require.False(t, waiting.selection.Acquired)
	require.Equal(t, int64(1), waiting.selection.Account.ID)
	require.False(t, waiting.switched)
	result.selection.ReleaseFunc()
}

func TestCompositeCapacityChainSwitchOffQueuesOnOriginalPlatform(t *testing.T) {
	// 验收 6：开关关闭时行为与现状一致。首选平台抢槽失败就在原账号排队，不换号。
	t.Setenv(compositeCapacitySelectionEnv, "false")
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 2)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 2)
	f := newCompositeCapacityFixture(t, a, b)
	f.slots.forceAcquireFail[1] = true
	f.randValue = 1
	result := f.chainSelect(nil, "req-off")
	require.Equal(t, PlatformKimi, result.decision.TargetPlatform)
	require.False(t, result.switched)
	require.False(t, result.selection.Acquired)
	require.NotNil(t, result.selection.WaitPlan)
	require.Equal(t, 0, f.slots.inUse(2))
}

func TestCompositeCapacityFallbackGates(t *testing.T) {
	a := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 5), 2)
	b := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 2)
	f := newCompositeCapacityFixture(t, a, b)
	f.slots.forceAcquireFail[1] = true
	f.randValue = 0
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	groupID := compositePoolGroupID
	ctx := WithCompositeRouteDecision(context.Background(), outcome.Decision)
	selection, _, err := f.openAI.SelectAccountWithSchedulerForCapability(
		ctx, &groupID, "", "gate", compositePoolModel, nil,
		OpenAIUpstreamTransportAny, OpenAIEndpointCapabilityChatCompletions, false, false, true, outcome.Decision.TargetPlatform,
	)
	require.NoError(t, err)
	require.False(t, selection.Acquired)
	in := CompositeCapacityFallbackInput{
		GroupID: &groupID, SessionHash: "gate", Model: compositePoolModel,
		CapabilityFor: func(string) OpenAIEndpointCapability { return OpenAIEndpointCapabilityChatCompletions },
	}

	t.Run("来源不是候选池时不换号", func(t *testing.T) {
		// 显式路由、前缀猜测的请求（来源不是 account_model）即使带着计划也不换号。
		explicit := WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
			Matched: true, Source: CompositeRouteSourceExplicit, GroupID: groupID,
			PublicModel: compositePoolModel, TargetPlatform: PlatformKimi, UpstreamModel: compositePoolModel,
			CandidateAccountIDs: []int64{1}, CapacityPlan: outcome.Decision.CapacityPlan,
		})
		_, ok := f.openAI.CompositeCapacityFallback(explicit, selection, in)
		require.False(t, ok)
	})
	t.Run("没有计划时不换号", func(t *testing.T) {
		noPlan := outcome.Decision
		noPlan.CapacityPlan = nil
		_, ok := f.openAI.CompositeCapacityFallback(WithCompositeRouteDecision(context.Background(), noPlan), selection, in)
		require.False(t, ok)
	})
	t.Run("已抢到槽位的选号结果不换号", func(t *testing.T) {
		acquired := &AccountSelectionResult{Account: selection.Account, Acquired: true}
		_, ok := f.openAI.CompositeCapacityFallback(ctx, acquired, in)
		require.False(t, ok)
	})
	t.Run("备选平台的配额预检失败则跳过该平台", func(t *testing.T) {
		quotaIn := in
		quotaIn.QuotaCheck = func(_ context.Context, platform string) error {
			require.Equal(t, PlatformDeepseek, platform)
			return ErrUserPlatformDailyQuotaExhausted
		}
		_, ok := f.openAI.CompositeCapacityFallback(ctx, selection, quotaIn)
		require.False(t, ok)
		require.Equal(t, 0, f.slots.inUse(2), "配额预检不通过时不能占用备选账号的槽位")
	})
	t.Run("换号成功时写回目标平台与候选集合并清除计划", func(t *testing.T) {
		sw, ok := f.openAI.CompositeCapacityFallback(ctx, selection, in)
		require.True(t, ok)
		defer sw.Selection.ReleaseFunc()
		platform, resolved := ResolvedTargetPlatformFromContext(sw.Ctx)
		require.True(t, resolved)
		require.Equal(t, PlatformDeepseek, platform)
		ids, hasSet := CompositeCandidateAccountIDsFromContext(sw.Ctx)
		require.True(t, hasSet)
		require.Equal(t, []int64{2}, ids)
		_, hasPlan := CompositeCapacityPlanFromContext(sw.Ctx)
		require.False(t, hasPlan, "换号只发生一次，新上下文不再带计划")
		require.False(t, sw.Temporary)
	})
}

func TestCompositeCapacityStickyTemporaryBorrowDoesNotRebindSession(t *testing.T) {
	// 普通粘性会话：绑定账号所在平台抢槽失败时临时借用其它平台，不改写粘性绑定。
	k := withCapacityAccount(compositePoolAdaptive(1, PlatformKimi, 30), 1)
	d := withCapacityAccount(compositePoolAdaptive(2, PlatformDeepseek, 5), 1)
	f := newCompositeCapacityFixture(t, k, d)
	f.bindOpenAISticky("sess-borrow", 1)
	f.slots.forceAcquireFail[1] = true // 绑定账号当前抢不到槽位
	before := f.snapshotBindings()

	hash := DeriveSessionHashFromSeed("sess-borrow")
	result := f.chainSelect(compositePoolSessionHeader("sess-borrow"), hash)
	require.Equal(t, compositeCandidatePathSticky, f.resolve(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), compositePoolSessionHeader("sess-borrow")), CompositeRouteEndpointChatCompletions).Path)
	require.True(t, result.switched, "绑定账号抢槽失败，临时借用 DeepSeek")
	require.Equal(t, int64(2), result.selection.Account.ID)
	require.Equal(t, before, f.snapshotBindings(), "临时借用不得改写会话与账号的粘性绑定")
	result.selection.ReleaseFunc()
}
