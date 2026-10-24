//go:build unit

package service

// 账号级候选池：粘性、Responses 续链归属与集合谓词的单测（场景 5、15、18 等）。
// 候选池全程只读：每个用例都断言它没有写粘性、没有删粘性。

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// bindOpenAISticky 按 OpenAI 网关族的契约（openai: 前缀加会话哈希）绑定会话与账号。
func (f *compositePoolFixture) bindOpenAISticky(seed string, accountID int64) {
	f.cache.sessionBindings = ensureBindings(f.cache.sessionBindings)
	f.cache.sessionBindings["openai:"+DeriveSessionHashFromSeed(seed)] = accountID
}

// bindAnthropicSticky 按 Anthropic 网关族的契约（裸哈希，Gemini 可带 gemini: 前缀）绑定会话与账号。
func (f *compositePoolFixture) bindAnthropicSticky(key string, accountID int64) {
	f.cache.sessionBindings = ensureBindings(f.cache.sessionBindings)
	f.cache.sessionBindings[key] = accountID
}

func ensureBindings(m map[string]int64) map[string]int64 {
	if m == nil {
		return map[string]int64{}
	}
	return m
}

// requireNoStickyWrites 断言候选池没有写入或删除任何粘性绑定。
func (f *compositePoolFixture) requireNoStickyWrites(before map[string]int64) {
	f.t.Helper()
	require.Equal(f.t, before, f.cache.sessionBindings, "候选池不得写粘性")
	require.Empty(f.t, f.cache.deletedSessions, "候选池不得删粘性")
}

func (f *compositePoolFixture) snapshotBindings() map[string]int64 {
	out := map[string]int64{}
	for key, id := range f.cache.sessionBindings {
		out[key] = id
	}
	return out
}

// ---- OpenAI 网关族粘性（场景 5） ----

func TestCompositeCandidatePoolOpenAIFamilyStickyOverridesRanking(t *testing.T) {
	// Chat Completions 入站三者都零转换，无绑定时取 Priority 最小的 Zhipu。
	worseKimi := compositePoolAdaptive(1, PlatformKimi, 30)
	betterKimi := compositePoolAdaptive(2, PlatformKimi, 10)
	zhipu := compositePoolAdaptive(3, PlatformZhipu, 5)
	header := compositePoolSessionHeader("sess-1")

	f := newCompositePoolFixture(t, worseKimi, betterKimi, zhipu)
	unbound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), header), CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathRanked, unbound.Path)
	require.Equal(t, PlatformZhipu, unbound.Decision.TargetPlatform)

	// 同一会话已绑到 Priority 较差的 Kimi 账号：目标平台跟绑定账号走，
	// 集合为绑定账号加同平台其它合格候选，Zhipu 不在集合里。
	f.bindOpenAISticky("sess-1", 1)
	before := f.snapshotBindings()
	bound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), header), CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathSticky, bound.Path)
	require.Equal(t, PlatformKimi, bound.Decision.TargetPlatform)
	require.Equal(t, []int64{1, 2}, bound.Decision.CandidateAccountIDs)
	f.requireNoStickyWrites(before)

	// 换一个新会话号则重新排序，不受旧绑定影响。
	fresh := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), compositePoolSessionHeader("sess-2")), CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathRanked, fresh.Path)
	require.Equal(t, PlatformZhipu, fresh.Decision.TargetPlatform)
}

func TestCompositeCandidatePoolStickyAccountNeedingConversionIsStillUsed(t *testing.T) {
	// 场景 15：粘性绑定账号没有 Responses 端点（需要转换），同一会话的 Responses 请求仍落在它身上，
	// 不按零转换过滤，也不报无账号可用。
	noResponses := compositePoolAdaptive(1, PlatformZhipu, 30)
	full := compositePoolAdaptive(2, PlatformKimi, 10)
	f := newCompositePoolFixture(t, noResponses, full)
	header := compositePoolSessionHeader("sess-15")

	unbound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), header), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformKimi, unbound.Decision.TargetPlatform)

	f.bindOpenAISticky("sess-15", 1)
	bound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), header), CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathSticky, bound.Path)
	require.Equal(t, PlatformZhipu, bound.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, bound.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolStickyBindingIgnoredWhenBoundAccountNotUsable(t *testing.T) {
	future := time.Now().Add(time.Hour)
	header := compositePoolSessionHeader("sess-x")
	good := compositePoolAdaptive(2, PlatformKimi, 10)

	cases := []struct {
		name  string
		bound Account
	}{
		{"绑定账号被停用", func() Account { a := compositePoolAdaptive(1, PlatformDeepseek, 50); a.Schedulable = false; return a }()},
		{"绑定账号冷却中", func() Account {
			a := compositePoolAdaptive(1, PlatformDeepseek, 50)
			a.RateLimitResetAt = &future
			return a
		}()},
		{"绑定账号不再声明该模型", compositePoolWithMapping(compositePoolAdaptive(1, PlatformDeepseek, 50), map[string]any{"other-model": "x"})},
		{"绑定账号被强声明压制的通配账号", compositePoolWithMapping(compositePoolAdaptive(1, PlatformDeepseek, 50), map[string]any{"deepseek-*": "x"})},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newCompositePoolFixture(t, tc.bound, good)
			f.bindOpenAISticky("sess-x", 1)
			before := f.snapshotBindings()
			outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), header), CompositeRouteEndpointChatCompletions)
			require.Equal(t, compositeCandidatePathRanked, outcome.Path, "绑定账号不合格时不使用粘性")
			require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
			f.requireNoStickyWrites(before) // 失效绑定的清理仍由各网关调度器负责
		})
	}

	t.Run("绑定账号所在平台的用户配额用尽", func(t *testing.T) {
		f := newCompositePoolFixture(t, compositePoolAdaptive(1, PlatformDeepseek, 50), good)
		f.quota.records[PlatformDeepseek] = compositePoolExhaustedQuota(PlatformDeepseek)
		f.bindOpenAISticky("sess-x", 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, compositePoolChatBody(), header), CompositeRouteEndpointChatCompletions)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	})
}

// ---- Anthropic 网关族粘性（场景 5） ----

func TestCompositeCandidatePoolAnthropicFamilyStickyUsesBareKey(t *testing.T) {
	anthropic := compositePoolAnthropic(1, 30)
	kimi := compositePoolAdaptive(2, PlatformKimi, 10)
	f := newCompositePoolFixture(t, anthropic, kimi)
	body := compositePoolMessagesBody("anth-sess-1")

	unbound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
	require.Equal(t, compositeCandidatePathRanked, unbound.Path)
	require.Equal(t, PlatformKimi, unbound.Decision.TargetPlatform, "两者都零转换，Priority 小的 Kimi 先")

	f.bindAnthropicSticky("anth-sess-1", 1)
	before := f.snapshotBindings()
	bound := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
	require.Equal(t, compositeCandidatePathSticky, bound.Path)
	require.Equal(t, PlatformAnthropic, bound.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, bound.Decision.CandidateAccountIDs)
	f.requireNoStickyWrites(before)

	// count_tokens 走同一套 Anthropic 网关族契约。
	counted := f.resolveHandled(f.ctxFor(CompositeRouteEndpointCountTokens, body, nil), CompositeRouteEndpointCountTokens)
	require.Equal(t, compositeCandidatePathSticky, counted.Path)
	require.Equal(t, PlatformAnthropic, counted.Decision.TargetPlatform)
}

func TestCompositeCandidatePoolGeminiStickyPrefixByEndpoint(t *testing.T) {
	gemini := compositePoolGemini(1, 30)
	kimi := compositePoolAdaptive(2, PlatformKimi, 10)

	t.Run("Messages 入口 Gemini 账号的粘性键带 gemini: 前缀", func(t *testing.T) {
		f := newCompositePoolFixture(t, gemini, kimi)
		body := compositePoolMessagesBody("g-sess-1")
		f.bindAnthropicSticky("g-sess-1", 1) // 裸键不是 Gemini 在该入口的契约
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path, "裸键对 Gemini 账号不算绑定")

		f.bindAnthropicSticky("gemini:g-sess-1", 1)
		outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, nil), CompositeRouteEndpointMessages)
		require.Equal(t, compositeCandidatePathSticky, outcome.Path)
		require.Equal(t, PlatformGemini, outcome.Decision.TargetPlatform)
	})

	t.Run("Chat Completions 入口同样带前缀", func(t *testing.T) {
		f := newCompositePoolFixture(t, gemini, kimi)
		body := `{"model":"` + compositePoolModel + `","messages":[{"role":"user","content":"hi"}]` + compositePoolMetadataSnippet("g-sess-2") + `}`
		f.bindAnthropicSticky("gemini:g-sess-2", 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, body, nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, compositeCandidatePathSticky, outcome.Path)
		require.Equal(t, PlatformGemini, outcome.Decision.TargetPlatform)
	})

	t.Run("Responses 入口不带前缀（与 Anthropic 网关 Responses 处理器一致）", func(t *testing.T) {
		f := newCompositePoolFixture(t, gemini, kimi)
		body := compositePoolResponsesBody(compositePoolMetadataSnippet("g-sess-3"))
		f.bindAnthropicSticky("gemini:g-sess-3", 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, body, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path, "带前缀的键在 Responses 入口不是契约")

		f.bindAnthropicSticky("g-sess-3", 1)
		outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, body, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathSticky, outcome.Path)
		require.Equal(t, PlatformGemini, outcome.Decision.TargetPlatform)
	})
}

func TestCompositeCandidatePoolBothGatewayFamiliesBound(t *testing.T) {
	// 两套网关族可能各自绑定了一个账号：按候选排序取优（零转换优先，再比 Priority）。
	anthropic := compositePoolAnthropic(1, 5)
	kimi := compositePoolAdaptive(2, PlatformKimi, 30)
	body := compositePoolMessagesBody("both-sess")
	header := compositePoolSessionHeader("both-sess")

	f := newCompositePoolFixture(t, anthropic, kimi)
	f.bindAnthropicSticky("both-sess", 1)
	f.bindOpenAISticky("both-sess", 2)

	// Messages 入站两者都零转换，Priority 小的 Anthropic 账号胜出。
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, body, header), CompositeRouteEndpointMessages)
	require.Equal(t, compositeCandidatePathSticky, outcome.Path)
	require.Equal(t, PlatformAnthropic, outcome.Decision.TargetPlatform)

	// Chat Completions 入站 Anthropic 账号需转换，Kimi 零转换胜出。
	chatBody := `{"model":"` + compositePoolModel + `","messages":[{"role":"user","content":"hi"}]` + compositePoolMetadataSnippet("both-sess") + `}`
	outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, chatBody, header), CompositeRouteEndpointChatCompletions)
	require.Equal(t, compositeCandidatePathSticky, outcome.Path)
	require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
}

func TestCompositeCandidatePoolOpenAIMessagesInboundStickyDerivation(t *testing.T) {
	// OpenAI 网关族的 Messages 入口在通用哈希之上还有 Claude Code 会话头与 metadata.user_id 派生。
	kimi := compositePoolAdaptive(1, PlatformKimi, 30)
	zhipu := compositePoolAdaptive(2, PlatformZhipu, 10)

	t.Run("Claude Code 会话头", func(t *testing.T) {
		f := newCompositePoolFixture(t, kimi, zhipu)
		header := http.Header{"X-Claude-Code-Session-Id": []string{"cc-session-1"}}
		f.bindOpenAISticky("cc-session-1", 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointMessages, compositePoolMessagesBody(""), header), CompositeRouteEndpointMessages)
		require.Equal(t, compositeCandidatePathSticky, outcome.Path)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	})

	t.Run("metadata.user_id 派生（种子含模型名，仅在通用哈希为空时启用）", func(t *testing.T) {
		// 请求体带 model 字段时通用哈希按内容派生、恒不为空；这条派生只在通用哈希为空时才会用到，
		// 处理器同样如此，所以用不带 model 字段的请求体触发它。
		f := newCompositePoolFixture(t, kimi, zhipu)
		body := `{"max_tokens":8,"metadata":{"user_id":"plain-user-1"}}`
		f.bindOpenAISticky(compositePoolModel+"-plain-user-1", 1)
		outcome, err := f.pool.resolve(f.ctxFor(CompositeRouteEndpointMessages, body, nil), compositePoolGroupID, compositePoolModel, CompositeRouteEndpointMessages)
		require.NoError(t, err)
		require.Equal(t, compositeCandidatePathSticky, outcome.Path)
		require.Equal(t, PlatformKimi, outcome.Decision.TargetPlatform)
	})
}

// ---- Responses 续链归属（场景 18） ----

func TestCompositeCandidatePoolContinuationBeatsSticky(t *testing.T) {
	// 场景 18：响应 ID 归属账号 A（优先级较差），会话粘性被人为绑到同平台账号 B，
	// 带该 ID 的请求回到 A；集合只放 A。
	ownerA := compositePoolOpenAI(1, 50)
	otherB := compositePoolOpenAI(2, 10)
	f := newCompositePoolFixture(t, ownerA, otherB)
	require.NoError(t, f.store.BindResponseAccount(context.Background(), compositePoolGroupID, "resp_abc123", 1, time.Hour))
	f.bindOpenAISticky("sess-c", 2)
	header := compositePoolSessionHeader("sess-c")
	before := f.snapshotBindings()

	withPrev := compositePoolResponsesBody(`,"previous_response_id":"resp_abc123"`)
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, withPrev, header), CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathContinuation, outcome.Path)
	require.Equal(t, PlatformOpenAI, outcome.Decision.TargetPlatform)
	require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	f.requireNoStickyWrites(before)

	// 对照：没有 previous_response_id 时走普通粘性，集合包含绑定账号 B 与同平台的 A。
	plain := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), header), CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathSticky, plain.Path)
	require.Equal(t, []int64{1, 2}, plain.Decision.CandidateAccountIDs)

	// 入口标记里显式给出的 previous_response_id 同样生效（请求体缺省时）。
	viaEntry := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), header, func(e *CompositeCandidatePoolEntry) {
		e.PreviousResponseID = "resp_abc123"
	}), CompositeRouteEndpointResponses)
	require.Equal(t, compositeCandidatePathContinuation, viaEntry.Path)
	require.Equal(t, []int64{1}, viaEntry.Decision.CandidateAccountIDs)
}

func TestCompositeCandidatePoolContinuationConditions(t *testing.T) {
	future := time.Now().Add(time.Hour)
	prev := compositePoolResponsesBody(`,"previous_response_id":"resp_owner"`)
	bind := func(f *compositePoolFixture, accountID int64) {
		require.NoError(t, f.store.BindResponseAccount(context.Background(), compositePoolGroupID, "resp_owner", accountID, time.Hour))
	}

	t.Run("归属账号不是 OpenAI 平台：无状态续链，按普通请求处理", func(t *testing.T) {
		kimi := compositePoolAdaptive(1, PlatformKimi, 50)
		deepseek := compositePoolAdaptive(2, PlatformDeepseek, 10)
		f := newCompositePoolFixture(t, kimi, deepseek)
		bind(f, 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, prev, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path)
		require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform, "不被归属锁定，按优先级选")
	})

	t.Run("归属账号当前不合格：落回普通调度，不删绑定", func(t *testing.T) {
		owner := compositePoolOpenAI(1, 50)
		owner.RateLimitResetAt = &future
		other := compositePoolOpenAI(2, 10)
		f := newCompositePoolFixture(t, owner, other)
		bind(f, 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, prev, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path)
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
		bound, err := f.store.GetResponseAccount(context.Background(), compositePoolGroupID, "resp_owner")
		require.NoError(t, err)
		require.Equal(t, int64(1), bound, "冷却是暂时状态，续链绑定保留")
	})

	t.Run("响应 ID 查不到归属或不是 Responses 入站：按普通请求处理", func(t *testing.T) {
		owner := compositePoolOpenAI(1, 50)
		other := compositePoolOpenAI(2, 10)
		f := newCompositePoolFixture(t, owner, other)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, prev, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path, "未知的响应 ID")

		bind(f, 1)
		chatBody := `{"model":"` + compositePoolModel + `","messages":[{"role":"user","content":"hi"}],"previous_response_id":"resp_owner"}`
		outcome = f.resolveHandled(f.ctxFor(CompositeRouteEndpointChatCompletions, chatBody, nil), CompositeRouteEndpointChatCompletions)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path, "Chat Completions 入站不使用续链归属")
	})

	t.Run("归属账号没有声明该模型：不使用", func(t *testing.T) {
		owner := compositePoolWithMapping(compositePoolOpenAI(1, 50), map[string]any{"other-model": "gpt-5.1"})
		other := compositePoolOpenAI(2, 10)
		f := newCompositePoolFixture(t, owner, other)
		bind(f, 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, prev, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathRanked, outcome.Path)
		require.Equal(t, []int64{2}, outcome.Decision.CandidateAccountIDs)
	})

	t.Run("归属账号是被强声明压制的通配账号：续链已落在它身上，仍然锁定", func(t *testing.T) {
		owner := compositePoolWithMapping(compositePoolOpenAI(1, 50), map[string]any{"deepseek-*": "gpt-5.1"})
		strong := compositePoolOpenAI(2, 10)
		f := newCompositePoolFixture(t, owner, strong)
		bind(f, 1)
		outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, prev, nil), CompositeRouteEndpointResponses)
		require.Equal(t, compositeCandidatePathContinuation, outcome.Path)
		require.Equal(t, []int64{1}, outcome.Decision.CandidateAccountIDs)
	})
}

// ---- 集合谓词与决策传递 ----

func TestCompositeAccountClaimAllowedPredicate(t *testing.T) {
	exact := Account{ID: 1, Platform: PlatformKimi, Credentials: map[string]any{"model_mapping": map[string]any{compositePoolModel: "x"}}}
	wildcard := Account{ID: 2, Platform: PlatformKimi, Credentials: map[string]any{"model_mapping": map[string]any{"deepseek-*": "x"}}}
	stranger := Account{ID: 3, Platform: PlatformKimi, Credentials: map[string]any{"model_mapping": map[string]any{compositePoolModel: "x"}}}

	// 没有集合：沿用精确判定，行为与引入候选池之前完全一致。
	plain := context.Background()
	require.True(t, CompositeAccountClaimAllowed(plain, &exact, compositePoolModel))
	require.False(t, CompositeAccountClaimAllowed(plain, &wildcard, compositePoolModel))
	require.False(t, CompositeAccountClaimAllowed(plain, nil, compositePoolModel))

	// 有集合：按账号 ID 是否属于集合判定，与映射是否精确无关。
	withSet := WithCompositeRouteDecision(plain, CompositeRouteDecision{
		Matched: true, Source: CompositeRouteSourceAccount, PublicModel: compositePoolModel,
		TargetPlatform: PlatformKimi, UpstreamModel: compositePoolModel,
		CandidateAccountIDs: []int64{2, 1},
	})
	require.True(t, CompositeAccountClaimAllowed(withSet, &exact, compositePoolModel))
	require.True(t, CompositeAccountClaimAllowed(withSet, &wildcard, compositePoolModel), "通配声明账号在集合内即放行")
	require.False(t, CompositeAccountClaimAllowed(withSet, &stranger, compositePoolModel), "即使精确声明，不在集合内也不放行")
	ids, ok := CompositeCandidateAccountIDsFromContext(withSet)
	require.True(t, ok)
	require.Equal(t, []int64{1, 2}, ids, "返回升序副本")
}
