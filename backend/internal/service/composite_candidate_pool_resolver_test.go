//go:build unit

package service

// 候选池接入 CompositeRouteResolver 的单测：挂钩位置（显式路由之后、账号归属判定之前）、
// 未处理与错误时回退原流程的口径，以及网关服务就地接线。

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"
)

// compositePoolStub 是可编排返回值的候选池桩，记录调用次数。
type compositePoolStub struct {
	decision CompositeRouteDecision
	handled  bool
	err      error
	calls    int
}

func (s *compositePoolStub) ResolveCandidatePool(_ context.Context, _ int64, _, _ string) (CompositeRouteDecision, bool, error) {
	s.calls++
	return s.decision, s.handled, s.err
}

func TestCompositeRouteResolverCandidatePoolHook(t *testing.T) {
	const groupID = int64(77)
	poolDecision := CompositeRouteDecision{
		Matched: true, Source: CompositeRouteSourceAccount, GroupID: groupID, PublicModel: "alias",
		TargetPlatform: PlatformKimi, UpstreamModel: "alias", Endpoint: CompositeRouteEndpointResponses,
		CandidateAccountIDs: []int64{3, 4},
	}
	ownershipCalls := 0
	ownership := func(context.Context, int64, string) (CompositeModelOwnership, error) {
		ownershipCalls++
		return CompositeModelOwnership{TargetPlatform: PlatformZhipu, Matched: true}, nil
	}
	newResolver := func(pool *compositePoolStub, routes ...CompositeModelRoute) *CompositeRouteResolver {
		resolver := NewCompositeRouteResolver(compositeRouteRepoStub{routes: routes})
		resolver.SetModelOwnershipResolver(ownership)
		resolver.SetCandidatePool(pool)
		return resolver
	}

	t.Run("候选池已处理：直接返回，不再走账号归属判定", func(t *testing.T) {
		ownershipCalls = 0
		pool := &compositePoolStub{decision: poolDecision, handled: true}
		decision, err := newResolver(pool).Resolve(context.Background(), groupID, "alias", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, poolDecision, decision)
		require.Equal(t, 1, pool.calls)
		require.Zero(t, ownershipCalls)
	})

	t.Run("显式路由优先于候选池", func(t *testing.T) {
		ownershipCalls = 0
		pool := &compositePoolStub{decision: poolDecision, handled: true}
		resolver := newResolver(pool, CompositeModelRoute{
			ID: 1, GroupID: groupID, PublicModel: "alias", MatchType: CompositeRouteMatchExact,
			TargetPlatform: PlatformOpenAI, UpstreamModel: "gpt-5", Endpoint: CompositeRouteEndpointAny, Enabled: true,
		})
		decision, err := resolver.Resolve(context.Background(), groupID, "alias", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, CompositeRouteSourceExplicit, decision.Source)
		require.Equal(t, PlatformOpenAI, decision.TargetPlatform)
		require.Zero(t, pool.calls, "显式路由命中后不进候选池")
	})

	t.Run("候选池未处理：交回原有账号归属判定", func(t *testing.T) {
		ownershipCalls = 0
		pool := &compositePoolStub{}
		decision, err := newResolver(pool).Resolve(context.Background(), groupID, "alias", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, 1, pool.calls)
		require.Equal(t, 1, ownershipCalls)
		require.Equal(t, CompositeRouteSourceAccount, decision.Source)
		require.Equal(t, PlatformZhipu, decision.TargetPlatform)
		require.Empty(t, decision.CandidateAccountIDs, "原流程的决策不带候选集合")
	})

	t.Run("候选池出错：可被前缀识别的模型名退回原流程", func(t *testing.T) {
		ownershipCalls = 0
		pool := &compositePoolStub{err: errors.New("db down")}
		decision, err := newResolver(pool).Resolve(context.Background(), groupID, "gpt-5", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, 1, ownershipCalls)
		require.True(t, decision.Matched)
	})

	t.Run("候选池出错：识别不了的别名无法猜平台，返回错误", func(t *testing.T) {
		ownershipCalls = 0
		pool := &compositePoolStub{err: errors.New("db down")}
		decision, err := newResolver(pool).Resolve(context.Background(), groupID, "alias", CompositeRouteEndpointResponses)
		require.Error(t, err)
		require.False(t, decision.Matched)
		require.Zero(t, ownershipCalls)
	})

	t.Run("没有设置候选池时行为不变", func(t *testing.T) {
		ownershipCalls = 0
		resolver := NewCompositeRouteResolver(compositeRouteRepoStub{})
		resolver.SetModelOwnershipResolver(ownership)
		decision, err := resolver.Resolve(context.Background(), groupID, "alias", CompositeRouteEndpointResponses)
		require.NoError(t, err)
		require.Equal(t, PlatformZhipu, decision.TargetPlatform)
		var nilResolver *CompositeRouteResolver
		nilResolver.SetCandidatePool(&compositePoolStub{})
	})
}

func TestGatewayServiceAttachCompositeCandidatePool(t *testing.T) {
	// 网关服务用自己持有的账号仓储与解析器就地创建候选池并接入；缺依赖时不启用。
	resolver := NewCompositeRouteResolver(compositeRouteRepoStub{})
	svc := &GatewayService{accountRepo: schedulerTestOpenAIAccountRepo{}, compositeResolver: resolver}
	svc.AttachCompositeCandidatePool(&OpenAIGatewayService{}, &BillingCacheService{})
	pool, ok := resolver.candidatePool.(*CompositeCandidatePool)
	require.True(t, ok, "解析器应已持有候选池")
	require.Same(t, svc, pool.gateway)
	require.NotNil(t, pool.accounts)

	noRepo := NewCompositeRouteResolver(compositeRouteRepoStub{})
	(&GatewayService{compositeResolver: noRepo}).AttachCompositeCandidatePool(nil, nil)
	require.Nil(t, noRepo.candidatePool, "没有账号仓储时不启用候选池")
	(&GatewayService{accountRepo: schedulerTestOpenAIAccountRepo{}}).AttachCompositeCandidatePool(nil, nil)
	var nilSvc *GatewayService
	nilSvc.AttachCompositeCandidatePool(nil, nil)
}
