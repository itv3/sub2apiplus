//go:build unit

package service

// Composite 上游失败短冷却的单测：5xx 与明确超时触发 30 秒冷却，到期可再选；
// 429、鉴权、额度、4xx 参数错误、客户端取消、本地满载以及非 Composite 请求不走新冷却；
// 已有更长的冷却不被缩短。

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/stretchr/testify/require"
)

// cooldownRepoStub 记录冷却写入，并可返回库里"最新"的账号（模拟别的请求已写下更长冷却）。
type cooldownRepoStub struct {
	rateLimitAccountRepoStub
	fresh     *Account
	lastUntil time.Time
}

func (r *cooldownRepoStub) SetTempUnschedulable(ctx context.Context, id int64, until time.Time, reason string) error {
	r.lastUntil = until
	return r.rateLimitAccountRepoStub.SetTempUnschedulable(ctx, id, until, reason)
}

func (r *cooldownRepoStub) GetByID(_ context.Context, id int64) (*Account, error) {
	if r.fresh != nil && r.fresh.ID == id {
		copied := *r.fresh
		return &copied, nil
	}
	return nil, ErrAccountNotFound
}

type cooldownBlockerStub struct {
	calls  int
	until  time.Time
	reason string
}

func (b *cooldownBlockerStub) BlockAccountScheduling(_ *Account, until time.Time, reason string) {
	b.calls++
	b.until = until
	b.reason = reason
}
func (b *cooldownBlockerStub) ClearAccountSchedulingBlock(int64) {}

type cooldownFixture struct {
	repo    *cooldownRepoStub
	cache   *tempUnschedCacheStub
	blocker *cooldownBlockerStub
	service *RateLimitService
}

func newCooldownFixture() *cooldownFixture {
	f := &cooldownFixture{repo: &cooldownRepoStub{}, cache: &tempUnschedCacheStub{}, blocker: &cooldownBlockerStub{}}
	f.service = NewRateLimitService(f.repo, nil, &config.Config{}, nil, f.cache)
	f.service.SetAccountRuntimeBlocker(f.blocker)
	return f
}

// compositeAutoCtx 是候选池自动路由的请求上下文（路由来源 account_model）。
func compositeAutoCtx() context.Context {
	return WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
		Matched: true, Source: CompositeRouteSourceAccount, GroupID: 1,
		PublicModel: "m", TargetPlatform: PlatformKimi, UpstreamModel: "m",
	})
}

func cooldownTestAccount() *Account {
	return &Account{ID: 501, Platform: PlatformKimi, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 2}
}

func TestCompositeFailureCooldownTriggersFor5xxAndTimeouts(t *testing.T) {
	for _, tc := range []struct {
		status int
		kind   string
	}{
		{500, "server_error"}, {502, "server_error"}, {503, "server_error"},
		{520, "server_error"}, {521, "server_error"}, {523, "server_error"},
		{504, "timeout"}, {522, "timeout"}, {524, "timeout"},
	} {
		t.Run(http.StatusText(tc.status)+"_"+tc.kind, func(t *testing.T) {
			f := newCooldownFixture()
			before := time.Now()
			shouldDisable := f.service.HandleUpstreamError(compositeAutoCtx(), cooldownTestAccount(), tc.status, http.Header{}, []byte(`{"error":"upstream"}`))
			require.False(t, shouldDisable, "短冷却不改变 HandleUpstreamError 的返回值")
			require.Equal(t, 1, f.repo.tempCalls)
			require.Equal(t, int64(501), f.repo.lastTempID)
			require.WithinDuration(t, before.Add(30*time.Second), f.repo.lastUntil, 2*time.Second, "冷却固定 30 秒")
			require.Equal(t, 1, f.cache.setCalls)
			require.Equal(t, compositeFailureCooldownMarker, f.cache.lastState.MatchedKeyword)
			require.Equal(t, tc.status, f.cache.lastState.StatusCode)
			require.Equal(t, TempUnschedScopeAccount, f.cache.lastState.Scope)
			require.Equal(t, 1, f.blocker.calls, "同步进程内运行时封禁，下一次选号立即避开")
			require.WithinDuration(t, f.repo.lastUntil, f.blocker.until, time.Second)
			require.Equal(t, 0, f.repo.setErrorCalls)
		})
	}
}

func TestCompositeFailureCooldownExpiresAndAccountIsSelectableAgain(t *testing.T) {
	// 到期后账号重新允许选择：冷却只写 TempUnschedulableUntil，没有任何需要清理的持久状态。
	f := newCooldownFixture()
	account := cooldownTestAccount()
	require.True(t, f.service.applyCompositeFailureCooldown(compositeAutoCtx(), account, 502, nil))

	blockedUntil := f.repo.lastUntil
	account.TempUnschedulableUntil = &blockedUntil
	require.False(t, account.IsSchedulable(), "冷却期内不可调度")

	expired := time.Now().Add(-time.Second)
	account.TempUnschedulableUntil = &expired
	require.True(t, account.IsSchedulable(), "到期后重新允许选择")
}

func TestCompositeFailureCooldownIgnoresOtherFailures(t *testing.T) {
	// 429、鉴权、额度、4xx 参数错误不走新冷却（它们走原规则，本函数一律不触发）。
	for _, status := range []int{400, 401, 402, 403, 404, 408, 422, 429, 501, 505} {
		f := newCooldownFixture()
		require.False(t, f.service.applyCompositeFailureCooldown(compositeAutoCtx(), cooldownTestAccount(), status, nil), "状态码 %d 不应触发", status)
		require.Equal(t, 0, f.repo.tempCalls, "状态码 %d 不应写冷却", status)
	}
	// 经过 HandleUpstreamError 整体入口时，429 与 4xx 同样不产生 composite 冷却。
	for _, status := range []int{400, 404, 422} {
		f := newCooldownFixture()
		f.service.HandleUpstreamError(compositeAutoCtx(), cooldownTestAccount(), status, http.Header{}, []byte(`{"error":"bad"}`))
		require.Equal(t, 0, f.repo.tempCalls)
		require.Equal(t, 0, f.cache.setCalls)
	}
}

func TestCompositeFailureCooldownRequiresCompositeAutoRoute(t *testing.T) {
	cases := map[string]context.Context{
		"非 Composite 请求（没有路由来源）": context.Background(),
		"显式路由": WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
			Matched: true, Source: CompositeRouteSourceExplicit, GroupID: 1, PublicModel: "m", TargetPlatform: PlatformKimi, UpstreamModel: "m",
		}),
		"前缀猜测": WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
			Matched: true, Source: CompositeRouteSourceDetector, GroupID: 1, PublicModel: "m", TargetPlatform: PlatformKimi, UpstreamModel: "m",
		}),
		"路由来源为空串": context.WithValue(context.Background(), ctxkey.CompositeRouteSource, ""),
	}
	for name, ctx := range cases {
		t.Run(name, func(t *testing.T) {
			f := newCooldownFixture()
			f.service.HandleUpstreamError(ctx, cooldownTestAccount(), 502, http.Header{}, []byte("bad gateway"))
			require.Equal(t, 0, f.repo.tempCalls)
			require.Equal(t, 0, f.cache.setCalls)
			require.Equal(t, 0, f.blocker.calls)
		})
	}
}

func TestCompositeFailureCooldownDoesNotShortenExistingBlock(t *testing.T) {
	later := time.Now().Add(10 * time.Minute)
	for name, mutate := range map[string]func(*Account){
		"已有更长的临时不可调度": func(a *Account) { a.TempUnschedulableUntil = &later },
		"已有更长的限流窗口":   func(a *Account) { a.RateLimitResetAt = &later },
		"已有更长的过载窗口":   func(a *Account) { a.OverloadUntil = &later },
	} {
		t.Run(name+"（调用方快照）", func(t *testing.T) {
			f := newCooldownFixture()
			account := cooldownTestAccount()
			mutate(account)
			require.False(t, f.service.applyCompositeFailureCooldown(compositeAutoCtx(), account, 503, nil))
			require.Equal(t, 0, f.repo.tempCalls)
			require.Equal(t, 0, f.cache.setCalls)
			require.Equal(t, 0, f.blocker.calls)
		})
	}
	t.Run("库里最新账号已有更长冷却（调用方快照已过时）", func(t *testing.T) {
		f := newCooldownFixture()
		fresh := cooldownTestAccount()
		fresh.TempUnschedulableUntil = &later
		f.repo.fresh = fresh
		require.False(t, f.service.applyCompositeFailureCooldown(compositeAutoCtx(), cooldownTestAccount(), 500, nil))
		require.Equal(t, 0, f.repo.tempCalls)
	})
	t.Run("已有更短的冷却会被延长到 30 秒", func(t *testing.T) {
		f := newCooldownFixture()
		account := cooldownTestAccount()
		shorter := time.Now().Add(5 * time.Second)
		account.TempUnschedulableUntil = &shorter
		require.True(t, f.service.applyCompositeFailureCooldown(compositeAutoCtx(), account, 500, nil))
		require.True(t, f.repo.lastUntil.After(shorter))
	})
}

func TestCompositeFailureCooldownSwitchOff(t *testing.T) {
	// 验收 6：开关关闭时行为与现状一致，5xx 只记日志，不写任何冷却。
	t.Setenv(compositeFailureCooldownEnv, "0")
	f := newCooldownFixture()
	shouldDisable := f.service.HandleUpstreamError(compositeAutoCtx(), cooldownTestAccount(), 502, http.Header{}, []byte("bad gateway"))
	require.False(t, shouldDisable)
	require.Equal(t, 0, f.repo.tempCalls)
	require.Equal(t, 0, f.cache.setCalls)
	require.Equal(t, 0, f.blocker.calls)
}

func TestCompositeFailureCooldownPoolModeAndCustomCodesKeepOldRules(t *testing.T) {
	// 池模式与自定义错误码账号在 HandleUpstreamError 更早的位置分流，不会触发 composite 冷却。
	f := newCooldownFixture()
	pool := cooldownTestAccount()
	pool.Credentials = map[string]any{"pool_mode": true}
	f.service.HandleUpstreamError(compositeAutoCtx(), pool, 502, http.Header{}, nil)
	require.Equal(t, 0, f.repo.tempCalls)

	f = newCooldownFixture()
	custom := cooldownTestAccount()
	custom.Credentials = map[string]any{"custom_error_codes_enabled": true, "custom_error_codes": []any{float64(502)}}
	f.service.HandleUpstreamError(compositeAutoCtx(), custom, 502, http.Header{}, nil)
	require.Equal(t, 0, f.repo.tempCalls, "自定义错误码账号走自己的规则")
}

func TestCompositeFailureCooldownStatusClassification(t *testing.T) {
	require.Equal(t, "timeout", compositeFailureCooldownKind(504))
	require.Equal(t, "timeout", compositeFailureCooldownKind(524))
	require.Equal(t, "server_error", compositeFailureCooldownKind(500))
	for _, status := range []int{200, 400, 401, 403, 404, 429, 501, 505, 529} {
		require.Equal(t, "", compositeFailureCooldownKind(status), "状态码 %d 不属于短冷却范围", status)
	}
}
