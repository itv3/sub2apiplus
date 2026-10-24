//go:build unit

package service

// 账号级候选池与自定义多协议平台（custom）的场景单测：方案验收标准的场景 1（原版）、9、13。
// custom 账号的各协议是否可用完全由凭据 api_base_urls 里填写的地址决定。

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

// compositePoolCustom 构造声明 compositePoolModel 的 custom 账号；urls 为 api_base_urls（协议 → 地址）。
func compositePoolCustom(id int64, priority int, urls map[string]any) Account {
	return compositePoolAccount(id, PlatformCustom, priority, map[string]any{
		"api_protocol":  APIProtocolAdaptive,
		"api_base_urls": urls,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
}

func compositePoolCustomChatOnly() map[string]any {
	return map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"}
}

func TestCompositeCandidatePoolCustomResponsesAddressCleared(t *testing.T) {
	// 场景 1（原版）：custom 账号三地址齐全、优先级最高，三种入站都选它；
	// 清空 Responses 地址后，Responses 入站退到需转换组，改选零转换的次优账号，
	// Chat Completions 与 Anthropic 入站不受影响。
	inbound := []struct {
		endpoint string
		body     string
	}{
		{CompositeRouteEndpointMessages, compositePoolMessagesBody("")},
		{CompositeRouteEndpointResponses, compositePoolResponsesBody("")},
		{CompositeRouteEndpointChatCompletions, compositePoolChatBody()},
	}
	deepseek := compositePoolAdaptive(2, PlatformDeepseek, 30)

	full := compositePoolCustom(1, 10, customFullURLs())
	f := newCompositePoolFixture(t, full, deepseek)
	for _, in := range inbound {
		outcome := f.resolveHandled(f.ctxFor(in.endpoint, in.body, nil), in.endpoint)
		require.Equal(t, PlatformCustom, outcome.Decision.TargetPlatform, in.endpoint)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs, in.endpoint)
	}

	noResponses := compositePoolCustom(1, 10, map[string]any{
		APIProtocolChatCompletions: "http://chat.example/v1",
		APIProtocolAnthropic:       "http://anthropic.example",
	})
	f = newCompositePoolFixture(t, noResponses, deepseek)
	for _, in := range inbound {
		outcome := f.resolveHandled(f.ctxFor(in.endpoint, in.body, nil), in.endpoint)
		if in.endpoint == CompositeRouteEndpointResponses {
			require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform, "custom 退到需转换组，DeepSeek 零转换")
			require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
			continue
		}
		require.Equal(t, PlatformCustom, outcome.Decision.TargetPlatform, in.endpoint)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs, in.endpoint)
	}

	// 没有别的零转换账号时，仍选 custom（走协议转换），不会无账号可用。
	f = newCompositePoolFixture(t, noResponses)
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathRanked, outcome.Path)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolCustomSamePlatformDifferentProtocolCapability(t *testing.T) {
	// 场景 9：同一 custom 平台里，一个只填 Chat Completions 地址、优先级更高，一个三地址齐全。
	chatOnly := compositePoolCustom(1, 5, compositePoolCustomChatOnly())
	full := compositePoolCustom(2, 30, customFullURLs())
	f := newCompositePoolFixture(t, chatOnly, full)

	// Responses 与 Anthropic 入站：只有三地址齐全的账号零转换，集合只放它。
	responses := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, []int64{2}, responses.Decision.CandidateAccountIDs)
	messages := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, compositePoolMessagesBody(""), nil), CompositeRouteEndpointMessages)
	require.Equal(t, []int64{2}, messages.Decision.CandidateAccountIDs)

	// Chat Completions 入站：两者都零转换，集合是同平台两个账号，调度器再按优先级选更高的那个。
	chat := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), nil), CompositeRouteEndpointChatCompletions)
	require.Equal(t, PlatformCustom, chat.Decision.TargetPlatform)
	require.Equal(t, []int64{1, 2}, chat.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolCustomSingleWildcardAccountIsSelectedByScheduler(t *testing.T) {
	// 场景 13：分组里只有一个 custom 账号，映射配 foo-*，请求 foo-x：
	// 候选池生成单账号集合，调度器的声明闸门按集合放行，能选中它，不出现无账号可用。
	const groupID = int64(1301)
	account := compositePoolWithMapping(compositePoolCustom(1301, 10, customFullURLs()), map[string]any{"foo-*": "foo-upstream"})
	account.GroupIDs = []int64{groupID}
	f := newCompositePoolFixture(t, account)
	ctx := f.ctxFor(CompositeRouteEndpointChatCompletions, `{"model":"foo-x","messages":[{"role":"user","content":"hi"}]}`, nil)
	decision, handled, err := f.pool.ResolveCandidatePool(ctx, compositePoolGroupID, "foo-x", CompositeRouteEndpointChatCompletions)
	require.NoError(t, err)
	require.True(t, handled)
	require.Equal(t, PlatformCustom, decision.TargetPlatform)
	require.Equal(t, []int64{account.ID}, decision.CandidateAccountIDs)

	newScheduler := func() *OpenAIGatewayService {
		cfg := &config.Config{}
		return &OpenAIGatewayService{
			accountRepo:        schedulerTestOpenAIAccountRepo{accounts: []Account{account}},
			cache:              &schedulerTestGatewayCache{},
			cfg:                cfg,
			rateLimitService:   newOpenAIAdvancedSchedulerRateLimitService("false"),
			concurrencyService: NewConcurrencyService(schedulerTestConcurrencyCache{}),
		}
	}
	selectWith := func(ctx context.Context) error {
		gid := groupID
		selection, _, selectErr := newScheduler().SelectAccountWithSchedulerForCapability(
			ctx, &gid, "", "", "foo-x", nil, OpenAIUpstreamTransportAny,
			OpenAIEndpointCapabilityChatCompletions, false, false, true, PlatformCustom,
		)
		if selectErr != nil {
			return selectErr
		}
		require.NotNil(t, selection)
		require.NotNil(t, selection.Account)
		require.Equal(t, account.ID, selection.Account.ID)
		if selection.ReleaseFunc != nil {
			selection.ReleaseFunc()
		}
		return nil
	}

	// 带候选集合的上下文：闸门放行通配声明账号。
	require.NoError(t, selectWith(WithCompositeRouteDecision(context.Background(), decision)))

	// 对照：同样的决策不带集合时沿用精确判定，通配声明账号被闸门挡掉，调度器报无可用账号。
	withoutSet := decision
	withoutSet.CandidateAccountIDs = nil
	require.ErrorIs(t, selectWith(WithCompositeRouteDecision(context.Background(), withoutSet)), ErrNoAvailableAccounts)
}
