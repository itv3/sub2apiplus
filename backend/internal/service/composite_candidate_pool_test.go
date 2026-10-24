//go:build unit

package service

// 账号级候选池：声明收集、资格过滤、配额预检与排序写回的单测。
// 粘性、续链与集合谓词的用例见 composite_candidate_pool_sticky_test.go。
//
// 用例编号"场景 N"指方案验收标准中能在单测层验证的场景。多协议自定义平台由另一路实现，
// 这里用现有的 adaptive 国产平台账号模拟：Kimi、DeepSeek 三种协议齐全，
// Zhipu 没有原生 Responses 端点（等价于"清空了 Responses 地址"的自定义账号）。

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

// ---- 入口与交回原流程 ----

func TestCompositeCandidatePoolUnhandledCases(t *testing.T) {
	account := compositePoolAdaptive(1, PlatformKimi, 10)

	t.Run("没有入口标记不进池且不读账号目录", func(t *testing.T) {
		f := newCompositePoolFixture(t, account)
		decision, handled, err := f.pool.ResolveCandidatePool(context.Background(), compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.False(t, handled)
		require.False(t, decision.Matched)
		require.Zero(t, f.lister.calls)
	})

	t.Run("端点不在四种文本入口内", func(t *testing.T) {
		f := newCompositePoolFixture(t, account)
		for _, endpoint := range []string{CompositeRouteEndpointImages, CompositeRouteEndpointEmbeddings, CompositeRouteEndpointGemini, CompositeRouteEndpointAny} {
			ctx := f.ctxFor(endpoint, compositePoolResponsesBody(""), nil)
			outcome, err := f.pool.resolve(ctx, compositePoolGroupID, compositePoolModel, endpoint)
			require.NoError(t, err)
			require.Equal(t, compositeCandidatePathNone, outcome.Path, endpoint)
		}
		require.Zero(t, f.lister.calls)
	})

	t.Run("入口标记的端点与解析端点不一致", func(t *testing.T) {
		f := newCompositePoolFixture(t, account)
		ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
		outcome, err := f.pool.resolve(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, compositeCandidatePathNone, outcome.Path)
	})

	t.Run("零声明交回原流程", func(t *testing.T) {
		f := newCompositePoolFixture(t, account)
		ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil)
		decision, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, "some-other-model", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.False(t, handled)
		require.False(t, decision.Matched)
		require.Equal(t, 1, f.lister.calls, "读过账号目录才知道零声明")
	})

	t.Run("空映射账号不入池", func(t *testing.T) {
		f := newCompositePoolFixture(t,
			compositePoolAccount(1, PlatformDeepseek, 1, nil),
			compositePoolAccount(2, PlatformOpenAI, 1, map[string]any{"api_protocol": APIProtocolAdaptive}),
		)
		ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil)
		_, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.False(t, handled, "DeepSeek 平台账号即使没写映射也不能凭模型名前缀冒领")
	})

	t.Run("账号目录读取失败返回错误并交回调用方", func(t *testing.T) {
		f := newCompositePoolFixture(t, account)
		f.lister.err = errors.New("db down")
		ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil)
		_, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses)
		require.Error(t, err)
		require.False(t, handled)
	})

	t.Run("空池与非法分组", func(t *testing.T) {
		var nilPool *CompositeCandidatePool
		_, handled, err := nilPool.ResolveCandidatePool(context.Background(), 1, "m", CompositeRouteEndpointMessages)
		require.NoError(t, err)
		require.False(t, handled)
		f := newCompositePoolFixture(t, account)
		ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil)
		_, handled, err = f.pool.ResolveCandidatePool(ctx, 0, compositePoolModel, CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.False(t, handled)
	})
}

// ---- 声明收集与强弱分层（场景 6、10、13） ----

func TestCompositeCandidatePoolSingleAccountSetExactAndWildcard(t *testing.T) {
	// 场景 6：只有一个账号声明该模型也生成单账号集合。
	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformDeepseek, 30))
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathRanked, outcome.Path)
	require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	require.Equal(t, CompositeClaimExplicit, outcome.Tier)

	// 场景 13：单账号通配声明（foo-*）请求 foo-x，由集合承接；旧的精确判定认不出它。
	wildcard := compositePoolWithMapping(compositePoolAdaptive(2, PlatformKimi, 10), map[string]any{"foo-*": "foo-upstream"})
	f = newCompositePoolFixture(t, wildcard)
	ctx := f.ctxFor(CompositeRouteEndpointResponses, `{"model":"foo-x","input":"hi"}`, nil)
	got, err := f.pool.resolve(ctx, compositePoolGroupID, "foo-x", CompositeRouteEndpointResponses)
	require.NoError(t, err)
	require.Equal(t, compositeCandidatePathRanked, got.Path)
	require.Equal(t, PlatformKimi, got.Decision.TargetPlatform)
	require.Equal(t, []int64{2}, got.Decision.CandidateAccountIDs)
	require.Equal(t, CompositeClaimWildcard, got.Tier)
	require.False(t, explicitModelMappingClaims(wildcard, "foo-x"), "前提：旧的精确判定认不出通配声明")
	decisionCtx := WithCompositeRouteDecision(context.Background(), got.Decision)
	require.True(t, CompositeAccountClaimAllowed(decisionCtx, &wildcard, "foo-x"))
}

func TestCompositeCandidatePoolStrongClaimSuppressesWildcardClaim(t *testing.T) {
	// 场景 10：有其它精确声明账号时，通配声明账号不入池，哪怕它优先级更高、更适合入站协议。
	strong := compositePoolAdaptive(1, PlatformZhipu, 50)
	weak := compositePoolWithMapping(compositePoolAdaptive(2, PlatformKimi, 1), map[string]any{"deepseek-*": "x"})
	f := newCompositePoolFixture(t, strong, weak)
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, CompositeClaimExplicit, outcome.Tier)
	require.Equal(t, PlatformZhipu, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	decisionCtx := WithCompositeRouteDecision(context.Background(), outcome.Decision)
	require.False(t, CompositeAccountClaimAllowed(decisionCtx, &weak, compositePoolModel), "通配账号不在集合内，调度闸门不放行")

	// 全部只有通配声明时，通配账号入池且能被调度闸门放行（不出现无账号可用）。
	weakA := compositePoolWithMapping(compositePoolAdaptive(3, PlatformKimi, 20), map[string]any{"deepseek-*": "x"})
	weakB := compositePoolWithMapping(compositePoolAdaptive(4, PlatformDeepseek, 10), map[string]any{"deepseek-fl*": "x"})
	f = newCompositePoolFixture(t, weakA, weakB)
	outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, CompositeClaimWildcard, outcome.Tier)
	require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform, "两个都是零转换，Priority 小的先选")
	require.Equal(t, []int64{4}, outcome.Decision.CandidateAccountIDs)
	decisionCtx = WithCompositeRouteDecision(context.Background(), outcome.Decision)
	require.True(t, CompositeAccountClaimAllowed(decisionCtx, &weakB, compositePoolModel))
}

// ---- 排序：零转换优先，再比账号优先级 ----

func TestCompositeCandidatePoolZeroConversionBeforePriority(t *testing.T) {
	kimi := compositePoolAdaptive(1, PlatformKimi, 20)
	zhipu := compositePoolAdaptive(2, PlatformZhipu, 10)
	deepseek := compositePoolAdaptive(3, PlatformDeepseek, 30)

	cases := []struct {
		name         string
		endpoint     string
		body         string
		wantPlatform string
		wantIDs      []int64
	}{
		// Zhipu 没有原生 Responses：Responses 入站要转换，让位给零转换的 Kimi（Priority 优于 DeepSeek）。
		{"responses", CompositeRouteEndpointResponses, compositePoolResponsesBody(""), PlatformKimi, []int64{1}},
		// 其余入站协议三者都零转换，按 Priority 取 Zhipu。
		{"chat_completions", CompositeRouteEndpointChatCompletions, compositePoolChatBody(), PlatformZhipu, []int64{2}},
		{"messages", CompositeRouteEndpointMessages, compositePoolMessagesBody(""), PlatformZhipu, []int64{2}},
		{"count_tokens", CompositeRouteEndpointCountTokens, compositePoolMessagesBody(""), PlatformZhipu, []int64{2}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newCompositePoolFixture(t, kimi, zhipu, deepseek)
			outcome := f.resolveHandled(f.ctxFor(tc.endpoint, tc.body, nil), tc.endpoint)
			require.Equal(t, compositeCandidatePathRanked, outcome.Path)
			require.Equal(t, tc.wantPlatform, outcome.Decision.TargetPlatform)
			require.Equal(t, tc.wantIDs, outcome.Decision.CandidateAccountIDs)
			require.Equal(t, tc.endpoint, outcome.Decision.Endpoint)
			require.Equal(t, int64(compositePoolGroupID), outcome.Decision.GroupID)
		})
	}
}

func TestCompositeCandidatePoolResponsesAddressCleared(t *testing.T) {
	// 场景 1（用现有国产平台模拟）：优先级最高的账号没有 Responses 端点，
	// Responses 入站退到需转换组，改选零转换的次优账号；Chat Completions 入站不受影响。
	noResponses := compositePoolAdaptive(1, PlatformZhipu, 10)
	full := compositePoolAdaptive(2, PlatformDeepseek, 30)
	f := newCompositePoolFixture(t, noResponses, full)

	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)

	outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformZhipu, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolSamePlatformDifferentProtocolCapability(t *testing.T) {
	// 场景 9：同平台里一个账号只会 Chat Completions（优先级更高），另一个三种齐全。
	chatOnly := compositePoolAccount(1, PlatformKimi, 5, map[string]any{
		"api_protocol":  APIProtocolChatCompletions,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	full := compositePoolAdaptive(2, PlatformKimi, 30)
	f := newCompositePoolFixture(t, chatOnly, full)

	// Responses 与 Anthropic 入站：只有齐全账号零转换，集合只放它。
	for _, endpoint := range []string{CompositeRouteEndpointResponses, CompositeRouteEndpointMessages} {
		body := compositePoolResponsesBody("")
		if endpoint == CompositeRouteEndpointMessages {
			body = compositePoolMessagesBody("")
		}
		outcome := f.resolveHandled(f.ctxFor(endpoint, body, nil), endpoint)
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs, endpoint)
	}
	// Chat Completions 入站：两者都零转换，集合是同平台的两个账号，调度器再按优先级选。
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1, 2}, outcome.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolAnthropicPlatformOnlyZeroConversionForAnthropicInbound(t *testing.T) {
	// Anthropic 平台账号只对 Anthropic 入站（含 count_tokens）零转换。
	anthropic := compositePoolAnthropic(1, 50)
	responsesPinned := compositePoolAccount(2, PlatformKimi, 10, map[string]any{
		"api_protocol":  APIProtocolResponses,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	f := newCompositePoolFixture(t, anthropic, responsesPinned)

	for _, endpoint := range []string{CompositeRouteEndpointMessages, CompositeRouteEndpointCountTokens} {
		outcome := f.resolveHandled(f.ctxFor(endpoint, compositePoolMessagesBody(""), nil), endpoint)
		require.Equal(t, PlatformAnthropic, outcome.Decision.TargetPlatform, "%s：Anthropic 零转换，压过优先级更高但固定 Responses 的 Kimi", endpoint)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	}
	// Responses 入站：固定 Responses 的 Kimi 零转换。
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	// Chat Completions 入站：双方都需转换，退到需转换组按 Priority，Kimi 优先。
	outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolTieBreakByAccountID(t *testing.T) {
	f := newCompositePoolFixture(t,
		compositePoolAdaptive(9, PlatformKimi, 10),
		compositePoolAdaptive(4, PlatformKimi, 10),
		compositePoolAdaptive(6, PlatformDeepseek, 10),
	)
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform, "优先级并列时账号 ID 小的账号决定平台")
	require.Equal(t, []int64{4, 9}, outcome.Decision.CandidateAccountIDs, "集合只含与首选同平台的账号，且升序")
}

func TestCompositeCandidatePoolSetIsSinglePlatformAndOnlyFromChosenGroup(t *testing.T) {
	// 集合必须单平台；零转换组非空时需转换账号不进集合（不做请求内换组）。
	zeroKimi := compositePoolAdaptive(1, PlatformKimi, 20)
	convertKimi := compositePoolAccount(2, PlatformKimi, 1, map[string]any{
		"api_protocol":  APIProtocolChatCompletions,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	zeroDeepseek := compositePoolAdaptive(3, PlatformDeepseek, 5)
	f := newCompositePoolFixture(t, zeroKimi, convertKimi, zeroDeepseek)
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{3}, outcome.Decision.CandidateAccountIDs)

	// 零转换组为空时取需转换组。
	f = newCompositePoolFixture(t, convertKimi, compositePoolAdaptive(4, PlatformZhipu, 7))
	outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
}

// ---- 资格过滤（场景 2、3） ----

func TestCompositeCandidatePoolEligibilityDisabledAndCooldownAccounts(t *testing.T) {
	future := time.Now().Add(30 * time.Minute)
	past := time.Now().Add(-time.Minute)
	preferred := compositePoolAdaptive(1, PlatformKimi, 10)
	backup := compositePoolAdaptive(2, PlatformDeepseek, 30)

	t.Run("场景2 停用账号后选次优", func(t *testing.T) {
		disabled := preferred
		disabled.Schedulable = false
		for _, endpoint := range []string{CompositeRouteEndpointResponses, CompositeRouteEndpointChatCompletions, CompositeRouteEndpointMessages} {
			f := newCompositePoolFixture(t, disabled, backup)
			outcome := f.resolveHandled(f.ctxFor(endpoint, compositePoolMessagesBody(""), nil), endpoint)
			require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform, endpoint)
			require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
			require.Equal(t, "not_schedulable", outcome.Rejected[1])
		}
	})

	t.Run("场景3 冷却中的账号下一次请求选次优，冷却到期后回来", func(t *testing.T) {
		cooling := preferred
		cooling.RateLimitResetAt = &future
		f := newCompositePoolFixture(t, cooling, backup)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		require.Contains(t, outcome.Rejected, int64(1))

		recovered := preferred
		recovered.RateLimitResetAt = &past
		f = newCompositePoolFixture(t, recovered, backup)
		outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
		require.Empty(t, outcome.Rejected)
	})

	t.Run("Anthropic 网关族账号同样被过滤", func(t *testing.T) {
		cooling := compositePoolAnthropic(1, 1)
		cooling.OverloadUntil = &future
		f := newCompositePoolFixture(t, cooling, backup)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, compositePoolMessagesBody(""), nil), CompositeRouteEndpointMessages)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
		require.Contains(t, outcome.Rejected, int64(1))
	})

	t.Run("端点能力不满足的账号被过滤（文本入口默认要求 chat_completions）", func(t *testing.T) {
		responsesOnly := compositePoolAdaptive(1, PlatformKimi, 1)
		responsesOnly.Credentials = map[string]any{
			"api_key": "sk", "api_protocol": APIProtocolAdaptive,
			"model_mapping":       compositePoolMapping(compositePoolModel),
			"openai_capabilities": []any{"responses"},
		}
		f := newCompositePoolFixture(t, responsesOnly, backup)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
		require.Equal(t, "capability_mismatch", outcome.Rejected[1])
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
	})
}

func TestCompositeCandidatePoolNoEligibleAccountStillWritesBack(t *testing.T) {
	// 声明账号全部没过资格过滤：不退回原流程，也不在候选池里新造错误；
	// 按声明账号排序后写回，由下游调度器的终检返回它本来就有的错误。
	a := compositePoolAdaptive(1, PlatformKimi, 10)
	a.Schedulable = false
	b := compositePoolAdaptive(2, PlatformZhipu, 5)
	b.Schedulable = false
	f := newCompositePoolFixture(t, a, b)
	ctx := f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil)

	decision, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses)
	require.NoError(t, err)
	require.True(t, handled)
	require.True(t, decision.Matched)
	require.Equal(t, CompositeRouteSourceAccount, decision.Source)
	require.Equal(t, PlatformKimi, decision.TargetPlatform, "Responses 入站 Kimi 零转换、Zhipu 需转换")
	require.Equal(t, []int64{1}, decision.CandidateAccountIDs)

	outcome := f.resolve(ctx, CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathNoEligible, outcome.Path)
	require.Len(t, outcome.Rejected, 2)
}

// ---- 用户 × 平台配额预检（场景 11） ----

func TestCompositeCandidatePoolUserPlatformQuotaPrecheck(t *testing.T) {
	kimiA := compositePoolAdaptive(1, PlatformKimi, 10)
	kimiB := compositePoolAdaptive(2, PlatformKimi, 11)
	deepseek := compositePoolAdaptive(3, PlatformDeepseek, 30)

	t.Run("余额模式：配额用尽的平台整体被剔除，按平台只查一次", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimiA, kimiB, deepseek)
		f.quota.records[PlatformKimi] = compositePoolExhaustedQuota(PlatformKimi)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
		require.Equal(t, []int64{3}, outcome.Decision.CandidateAccountIDs)
		require.Equal(t, "user_platform_quota_exhausted", outcome.Rejected[1])
		require.Equal(t, "user_platform_quota_exhausted", outcome.Rejected[2])
		require.Equal(t, 1, f.quota.reads[PlatformKimi], "同一平台的两个账号只预检一次")
		require.Equal(t, 1, f.quota.reads[PlatformDeepseek])
	})

	t.Run("订阅模式豁免用户 × 平台配额", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimiA, kimiB, deepseek)
		f.quota.records[PlatformKimi] = compositePoolExhaustedQuota(PlatformKimi)
		ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil, func(e *CompositeCandidatePoolEntry) {
			e.Group = &Group{ID: compositePoolGroupID, Platform: PlatformComposite, SubscriptionType: SubscriptionTypeSubscription}
			e.Subscription = &UserSubscription{ID: 1, UserID: compositePoolUserID, GroupID: compositePoolGroupID}
		})
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
		require.Empty(t, f.quota.reads, "订阅模式根本不读配额")
	})

	t.Run("订阅类型分组但用户没有订阅仍按余额模式检查", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimiA, deepseek)
		f.quota.records[PlatformKimi] = compositePoolExhaustedQuota(PlatformKimi)
		ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil, func(e *CompositeCandidatePoolEntry) {
			e.Group = &Group{ID: compositePoolGroupID, Platform: PlatformComposite, SubscriptionType: SubscriptionTypeSubscription}
		})
		outcome := f.resolveHandled(ctx, CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform)
	})

	t.Run("简易模式跳过配额检查", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimiA, deepseek)
		f.billing.cfg = &config.Config{RunMode: config.RunModeSimple}
		f.quota.records[PlatformKimi] = compositePoolExhaustedQuota(PlatformKimi)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
		require.Empty(t, f.quota.reads)
	})

	t.Run("场景11 多候选预检不递增 RPM 计数", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimiA, kimiB, deepseek, compositePoolAnthropic(4, 50))
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, compositePoolMessagesBody(""), nil), CompositeRouteEndpointMessages)
		require.Len(t, outcome.Rejected, 0, "三个平台的候选都合格")
		require.NotEmpty(t, f.quota.reads, "前提：配额预检确实执行过")
		require.Zero(t, atomic.LoadInt32(&f.rpm.userGroupCalls), "分组 RPM 计数不得被预检递增")
		require.Zero(t, atomic.LoadInt32(&f.rpm.userCalls), "用户 RPM 计数不得被预检递增")
	})
}

// ---- 入口标记与决策传递 ----

func TestCompositeCandidatePoolEntryContextRoundTrip(t *testing.T) {
	_, ok := CompositeCandidatePoolEntryFromContext(context.Background())
	require.False(t, ok)

	entry := CompositeCandidatePoolEntry{Endpoint: CompositeRouteEndpointResponses, UserID: 3, PreviousResponseID: "resp_x"}
	ctx := WithCompositeCandidatePoolEntry(context.Background(), entry)
	got, ok := CompositeCandidatePoolEntryFromContext(ctx)
	require.True(t, ok)
	require.Equal(t, entry.UserID, got.UserID)
	require.Equal(t, "resp_x", got.PreviousResponseID)
}

func TestCompositeCandidatePoolDecisionFeedsRouteContext(t *testing.T) {
	f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformKimi, 10), compositePoolAdaptive(2, PlatformKimi, 11))
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil)
	decision, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointChatCompletions)
	require.NoError(t, err)
	require.True(t, handled)

	routed := WithCompositeRouteDecision(context.Background(), decision)
	platform, ok := ResolvedTargetPlatformFromContext(routed)
	require.True(t, ok)
	require.Equal(t, PlatformKimi, platform)
	source, _ := CompositeRouteSourceFromContext(routed)
	require.Equal(t, CompositeRouteSourceAccount, source)
	publicModel, _ := RequestedPublicModelFromContext(routed)
	require.Equal(t, compositePoolModel, publicModel)
	ids, ok := CompositeCandidateAccountIDsFromContext(routed)
	require.True(t, ok)
	require.Equal(t, []int64{1, 2}, ids)

	// 未匹配的决策不写集合；没有集合的决策也不写。
	_, ok = CompositeCandidateAccountIDsFromContext(WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{CandidateAccountIDs: []int64{1}}))
	require.False(t, ok)
	_, ok = CompositeCandidateAccountIDsFromContext(WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{Matched: true, TargetPlatform: PlatformKimi}))
	require.False(t, ok)
}

// ---- 上下文隔离 ----

func TestCompositeCandidatePoolEligibilityIgnoresAccountModelOwnershipMarker(t *testing.T) {
	// 下游调度器里"账号是否声明了该模型"的闸门读取 account_model 来源标记，按精确键判定；
	// 候选池已按声明强度收集账号，资格过滤不能被这个标记误伤通配账号。
	wildcard := compositePoolWithMapping(compositePoolAdaptive(1, PlatformKimi, 10), map[string]any{"foo-*": "foo-upstream"})
	f := newCompositePoolFixture(t, wildcard)
	marked := WithCompositeRouteDecision(
		f.ctxFor(CompositeRouteEndpointChatCompletions, `{"model":"foo-x","messages":[{"role":"user","content":"hi"}]}`, nil),
		CompositeRouteDecision{Matched: true, Source: CompositeRouteSourceAccount, PublicModel: "foo-x", TargetPlatform: PlatformKimi, UpstreamModel: "foo-x"},
	)
	source, ok := CompositeRouteSourceFromContext(marked)
	require.True(t, ok)
	require.Equal(t, CompositeRouteSourceAccount, source, "前提：上下文确实带着 account_model 来源标记")

	outcome, err := f.pool.resolve(marked, compositePoolGroupID, "foo-x", CompositeRouteEndpointChatCompletions)
	require.NoError(t, err)
	require.Equal(t, compositeCandidatePathRanked, outcome.Path)
	require.Empty(t, outcome.Rejected)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
}

// compositePoolRPMCache 返回固定的 RPM 计数，供 Anthropic 网关族的 RPM 资格判断读取。
type compositePoolRPMCache struct {
	RPMCache
	count int
}

func (c compositePoolRPMCache) GetRPM(_ context.Context, _ int64) (int, error) { return c.count, nil }

func TestCompositeCandidatePoolAnthropicStickyOnlyStateOnlyHelpsBoundAccount(t *testing.T) {
	// Anthropic OAuth 账号 RPM 超限且策略为粘性豁免：新会话不能选它，已绑定它的会话仍可继续使用。
	oauth := compositePoolAnthropic(1, 30)
	oauth.Type = AccountTypeOAuth
	oauth.Extra = map[string]any{"base_rpm": 5, "rpm_strategy": "sticky_exempt"}
	kimi := compositePoolAdaptive(2, PlatformKimi, 10)
	f := newCompositePoolFixture(t, oauth, kimi)
	f.gateway.rpmCache = compositePoolRPMCache{count: 50}
	body := compositePoolMessagesBody("rpm-sess")

	unbound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
	require.Equal(t, PlatformKimi, unbound.Decision.TargetPlatform)
	require.Equal(t, "rpm_limited", unbound.Rejected[1])

	f.bindAnthropicSticky("rpm-sess", 1)
	bound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
	require.Equal(t, compositeCandidatePathSticky, bound.Path)
	require.Equal(t, PlatformAnthropic, bound.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, bound.Decision.CandidateAccountIDs, "仅限粘性的账号只服务已绑定它的会话，不会被带进新会话的集合")
}
