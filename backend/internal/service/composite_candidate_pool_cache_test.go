//go:build unit

package service

// 候选池配置态缓存与运行态实时读取的单测。
//
// 约定：配置态（分组里有哪些账号、映射、凭据、优先级）按分组缓存，复用网关服务的
// modelsListCache（同 TTL、同失效通道）；运行态由声明命中的候选账号逐个实时读取，
// 读取入口与调度器终检相同（getSchedulableAccount）。

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func compositePoolFutureTime() *time.Time {
	t := time.Now().Add(30 * time.Minute)
	return &t
}

func compositePoolPastTime() *time.Time {
	t := time.Now().Add(-time.Minute)
	return &t
}

func (f *compositePoolFixture) resolveChat() compositeCandidatePoolOutcome {
	f.t.Helper()
	return f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
}

// 缓存命中时，首选账号在调度快照里已经冷却：本次不选它，选次优；冷却结束后立刻回来。
// 对应方案验收标准：429 之后的下一次请求就选次优，不能因为缓存而延迟。
func TestCompositeCandidatePoolConfigCacheDoesNotDelayCooldown(t *testing.T) {
	f := newCompositePoolFixture(t,
		compositePoolAdaptive(1, PlatformKimi, 10),
		compositePoolAdaptive(2, PlatformDeepseek, 30),
	)
	f.enableConfigCache(time.Minute)

	first := f.resolveChat()
	require.Equal(t, []int64{1}, first.Decision.CandidateAccountIDs)
	require.Equal(t, 1, f.lister.loadCalls())
	require.Equal(t, 2, f.lister.readCalls(), "两个声明命中的候选账号各实时读取一次")

	f.lister.update(1, func(a *Account) { a.RateLimitResetAt = compositePoolFutureTime() })
	cooled := f.resolveChat()
	require.Equal(t, []int64{2}, cooled.Decision.CandidateAccountIDs, "缓存命中，首选账号已冷却：本次不选它")
	require.Equal(t, "not_schedulable", cooled.Rejected[1])
	require.Equal(t, 1, f.lister.loadCalls(), "第二次请求没有重新加载账号目录")

	f.lister.update(1, func(a *Account) { a.RateLimitResetAt = compositePoolPastTime() })
	recovered := f.resolveChat()
	require.Equal(t, []int64{1}, recovered.Decision.CandidateAccountIDs, "冷却结束，下一次请求立即回来")
	require.Equal(t, 1, f.lister.loadCalls())
}

// 零声明请求（例如单平台模型 gpt-6-luna）命中缓存，不触发账号加载，也不读任何账号。
func TestCompositeCandidatePoolConfigCacheZeroClaimLoadsNoAccounts(t *testing.T) {
	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformKimi, 10))
	f.enableConfigCache(time.Minute)

	for i := 0; i < 3; i++ {
		ctx := f.ctxFor(CompositeRouteEndpointResponses, `{"model":"gpt-6-luna","input":"hi"}`, nil)
		decision, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, "gpt-6-luna", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.False(t, handled)
		require.False(t, decision.Matched)
	}
	require.Equal(t, 1, f.lister.loadCalls(), "只有第一次未命中缓存时加载一次配置态")
	require.Zero(t, f.lister.readCalls(), "零声明请求不读取任何账号")

	// 配置态按分组缓存，换成有声明的模型也不再加载。
	f.resolveChat()
	require.Equal(t, 1, f.lister.loadCalls())
	require.Positive(t, f.lister.readCalls())
}

// 配置变更的生效时延：
//   - 被改动的账号（映射、优先级、协议地址、解绑分组、删除、读取失败）因为按最新账号复核，立即生效；
//   - 新增声明账号要等缓存失效：失效通道（InvalidateAvailableModelsCache）调用后立即生效，
//     否则不超过缓存 TTL。
func TestCompositeCandidatePoolConfigChangeTakesEffectOnNextRequest(t *testing.T) {
	newCached := func(t *testing.T, accounts ...Account) *compositePoolFixture {
		t.Helper()
		f := newCompositePoolFixture(t, accounts...)
		f.enableConfigCache(time.Minute)
		f.resolveChat() // 预热缓存
		require.Equal(t, 1, f.lister.loadCalls())
		return f
	}
	kimi := compositePoolAdaptive(1, PlatformKimi, 10)
	deepseek := compositePoolAdaptive(2, PlatformDeepseek, 30)

	t.Run("改优先级立即生效", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.update(1, func(a *Account) { a.Priority = 90 })
		require.Equal(t, []int64{2}, f.resolveChat().Decision.CandidateAccountIDs)
		require.Equal(t, 1, f.lister.loadCalls())
	})

	t.Run("撤销声明立即生效", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.update(1, func(a *Account) {
			a.Credentials = map[string]any{"api_key": "sk", "api_protocol": APIProtocolAdaptive, "model_mapping": map[string]any{"other-model": "x"}}
		})
		outcome := f.resolveChat()
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Equal(t, "claim_removed", outcome.Rejected[1])
	})

	t.Run("清掉 Responses 地址让零转换判定立即变化", func(t *testing.T) {
		full := compositePoolCustom(1, 10, customFullURLs())
		f := newCached(t, full, deepseek)
		responses := func() compositeCandidatePoolOutcome {
			return f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
		}
		require.Equal(t, []int64{1}, responses().Decision.CandidateAccountIDs)
		f.lister.update(1, func(a *Account) {
			a.Credentials = map[string]any{
				"api_key": "sk", "api_protocol": APIProtocolAdaptive,
				"api_base_urls": map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"},
				"model_mapping": compositePoolMapping(compositePoolModel),
			}
		})
		require.Equal(t, []int64{2}, responses().Decision.CandidateAccountIDs, "Responses 入站退到需转换组")
		require.Equal(t, 1, f.lister.loadCalls())
	})

	t.Run("账号被解绑分组立即生效", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.update(1, func(a *Account) { a.GroupIDs, a.AccountGroups = []int64{999}, nil })
		outcome := f.resolveChat()
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Equal(t, "not_in_group", outcome.Rejected[1])
	})

	t.Run("Anthropic 网关族账号解绑分组同样立即生效", func(t *testing.T) {
		f := newCached(t, compositePoolAnthropic(1, 10), deepseek)
		f.lister.update(1, func(a *Account) { a.GroupIDs, a.AccountGroups = nil, nil })
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, compositePoolMessagesBody(""), nil), CompositeRouteEndpointMessages)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
		require.Equal(t, "not_in_group", outcome.Rejected[1])
	})

	t.Run("账号被删除或调度器不可见：不合格并记原因", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.mu.Lock()
		f.lister.byIDMissing = map[int64]bool{1: true}
		f.lister.mu.Unlock()
		outcome := f.resolveChat()
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Equal(t, "account_unavailable_for_scheduling", outcome.Rejected[1])
	})

	t.Run("读取失败的账号按不合格处理并记原因", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.mu.Lock()
		f.lister.byIDErr = map[int64]error{1: errors.New("redis timeout")}
		f.lister.mu.Unlock()
		outcome := f.resolveChat()
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Equal(t, "account_refresh_failed", outcome.Rejected[1])
	})

	t.Run("全部读取失败：仍按配置态排序写回，由下游调度器报它自己的错误", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.mu.Lock()
		f.lister.byIDErr = map[int64]error{1: errors.New("redis timeout"), 2: errors.New("redis timeout")}
		f.lister.mu.Unlock()
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, compositeCandidatePathNoEligible, outcome.Path)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	})

	t.Run("新增声明账号：失效通道调用后立即生效，调用前仍看不到", func(t *testing.T) {
		f := newCached(t, kimi, deepseek)
		f.lister.add(compositePoolAdaptive(3, PlatformZhipu, 1))
		require.Equal(t, []int64{1}, f.resolveChat().Decision.CandidateAccountIDs, "缓存未失效：新账号暂不可见")
		require.Equal(t, 1, f.lister.loadCalls())

		groupID := compositePoolGroupID
		f.gateway.InvalidateAvailableModelsCache(&groupID, "")
		require.Equal(t, []int64{3}, f.resolveChat().Decision.CandidateAccountIDs, "失效后立即看到新账号")
		require.Equal(t, 2, f.lister.loadCalls())
	})

	t.Run("新增声明账号：不调用失效通道时，缓存过期后生效（过期时间即缓存 TTL）", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimi, deepseek)
		const ttl = 60 * time.Millisecond
		f.enableConfigCache(ttl)
		f.resolveChat()
		f.lister.add(compositePoolAdaptive(3, PlatformZhipu, 1))
		time.Sleep(4 * ttl)
		require.Equal(t, []int64{3}, f.resolveChat().Decision.CandidateAccountIDs)
		require.Equal(t, 2, f.lister.loadCalls())
	})
}

// 网关服务没有模型列表缓存时（例如仅构造了最小服务），候选池退化为每个请求都加载配置态。
func TestCompositeCandidatePoolConfigCacheDisabledWithoutModelsListCache(t *testing.T) {
	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformKimi, 10))
	f.resolveChat()
	f.resolveChat()
	require.Equal(t, 2, f.lister.loadCalls())
}

// 并发请求的首次加载合并成一次，且共享同一份只读快照不互相影响。
func TestCompositeCandidatePoolConfigLoadIsSharedByConcurrentRequests(t *testing.T) {
	f := newCompositePoolFixture(t,
		compositePoolAdaptive(1, PlatformKimi, 10),
		compositePoolAdaptive(2, PlatformDeepseek, 30),
	)
	f.enableConfigCache(time.Minute)
	f.lister.listDelay = 80 * time.Millisecond

	const workers = 8
	ctxs := make([]context.Context, workers)
	for i := range ctxs {
		ctxs[i] = f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	}
	results := make([]compositeCandidatePoolOutcome, workers)
	errs := make([]error, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = f.pool.resolve(ctxs[i], compositePoolGroupID, compositePoolModel, CompositeRouteEndpointChatCompletions)
		}(i)
	}
	wg.Wait()
	for i := 0; i < workers; i++ {
		require.NoError(t, errs[i])
		require.Equal(t, []int64{1}, results[i].Decision.CandidateAccountIDs)
	}
	require.Equal(t, 1, f.lister.loadCalls(), "并发的首次请求只加载一次账号目录")
}

// 加载失败不写缓存：下一次请求重新尝试。
func TestCompositeCandidatePoolConfigLoadFailureIsNotCached(t *testing.T) {
	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformKimi, 10))
	f.enableConfigCache(time.Minute)
	f.lister.err = errors.New("db down")
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	_, err := f.pool.resolve(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointChatCompletions)
	require.Error(t, err)

	f.lister.mu.Lock()
	f.lister.err = nil
	f.lister.mu.Unlock()
	require.Equal(t, []int64{1}, f.resolveChat().Decision.CandidateAccountIDs)
	require.Equal(t, 2, f.lister.loadCalls())
}

// 用户平台配额预检出错与配额用尽用不同的原因标签；两种情况都剔除该平台。
func TestCompositeCandidatePoolQuotaReasonLabels(t *testing.T) {
	require.Equal(t, "user_platform_quota_exhausted", compositeUserPlatformQuotaReason(ErrUserPlatformDailyQuotaExhausted))
	require.Equal(t, "user_platform_quota_exhausted", compositeUserPlatformQuotaReason(
		withWindowResetsMetadata(ErrUserPlatformWeeklyQuotaExhausted, time.Now())))
	require.Equal(t, "user_platform_quota_exhausted", compositeUserPlatformQuotaReason(ErrUserPlatformMonthlyQuotaExhausted))
	require.Equal(t, "user_platform_quota_check_error", compositeUserPlatformQuotaReason(errors.New("redis: connection refused")))

	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformKimi, 10))
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	entry, ok := CompositeCandidatePoolEntryFromContext(ctx)
	require.True(t, ok)
	account := compositePoolAdaptive(1, PlatformKimi, 10)

	for name, tc := range map[string]struct {
		err  error
		want string
	}{
		"配额用尽":   {withWindowResetsMetadata(ErrUserPlatformDailyQuotaExhausted, time.Now()), "user_platform_quota_exhausted"},
		"预检本身出错": {errors.New("redis: connection refused"), "user_platform_quota_check_error"},
	} {
		t.Run(name, func(t *testing.T) {
			run := f.pool.newRun(ctx, entry, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointChatCompletions, APIProtocolChatCompletions)
			run.quota[PlatformKimi] = tc.err // 模拟该平台的预检结果
			verdict := run.evaluate(&account, false)
			require.False(t, verdict.eligible, "两种情况都剔除该平台")
			require.Equal(t, tc.want, verdict.reason)
		})
	}
}
