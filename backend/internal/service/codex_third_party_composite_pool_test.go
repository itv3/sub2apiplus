//go:build unit

package service

// Codex 三方模型目录接入账号级跨平台候选池的单测。
//
// 场景：Composite 分组里同一个公开模型名被多个平台的账号同时声明。旧流程按"声明它的平台有几个"
// 判歧义、开关打不开；现在目录页复用候选池的同一套规则（强弱声明分层、零转换优先、Priority 与
// 账号 ID 排序），对"Responses 请求可能落到的全部账号"求能力档案与协议覆盖。
// 夹具复用候选池单测的账号构造（composite_candidate_pool_fixture_test.go），
// 这样同一份账号配置可以同时喂给候选池与目录页，断言两边结论一致。

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// cxtpPoolNamed 返回改了名称的账号副本，便于断言备选账号的展示信息。
func cxtpPoolNamed(account Account, name string) Account {
	account.Name = name
	return account
}

// cxtpPoolChatOnly 构造固定 Chat Completions 协议的账号（对应"只有 CC"的 Cline）。
func cxtpPoolChatOnly(id int64, platform string, priority int, mapping map[string]any) Account {
	return compositePoolAccount(id, platform, priority, map[string]any{
		"api_protocol":  APIProtocolChatCompletions,
		"model_mapping": mapping,
	})
}

// cxtpPoolCrossPlatformAccounts 是 ARM64 实测形态的缩影：DeepSeek、Custom 对 Responses 零转换，
// Cline 只有 Chat Completions（需转换，且 Priority 最高），CommandCode 按模型分流。
func cxtpPoolCrossPlatformAccounts() []Account {
	return []Account{
		cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 30), "DeepSeek"),
		cxtpPoolNamed(compositePoolCustom(12, 10, customFullURLs()), "Custom"),
		cxtpPoolNamed(cxtpPoolChatOnly(13, PlatformCline, 1, compositePoolMapping(compositePoolModel)), "Cline"),
	}
}

// cxtpPoolService 构造 Composite 分组的目录服务：账号同时来自目录页的仓储，Composite 解析器装上与生产一致的
// "按账号显式映射判定归属"逻辑（持久停用的账号不声明），不带候选池入口标记，与生产的目录页路径一致。
func cxtpPoolService(t *testing.T, accounts []Account) *CodexThirdPartyCatalogService {
	t.Helper()
	svc := newCXTPService(t, &cxtpSettings{}, accounts, nil)
	svc.resolver.SetModelOwnershipResolver(func(_ context.Context, _ int64, model string) (CompositeModelOwnership, error) {
		platforms := map[string]struct{}{}
		for i := range accounts {
			if compositeAccountPersistentlyEnabled(&accounts[i]) && explicitModelMappingClaims(accounts[i], model) {
				platforms[accounts[i].Platform] = struct{}{}
			}
		}
		switch len(platforms) {
		case 0:
			return CompositeModelOwnership{}, nil
		case 1:
			for platform := range platforms {
				return CompositeModelOwnership{Matched: true, TargetPlatform: platform}, nil
			}
		}
		return CompositeModelOwnership{Ambiguous: true}, nil
	})
	svc.groupModels = func(context.Context, *Group) ([]string, error) { return []string{compositePoolModel}, nil }
	return svc
}

func cxtpPoolEvaluate(t *testing.T, svc *CodexThirdPartyCatalogService) CodexThirdPartyCandidate {
	t.Helper()
	eval, err := svc.EvaluateCodexThirdPartyCatalog(WithCodexThirdPartyOfflineEvaluation(context.Background()), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	for _, candidate := range eval.Candidates {
		if candidate.Slug == compositePoolModel {
			return candidate
		}
	}
	t.Fatalf("candidates 里没有 %s：%+v", compositePoolModel, eval.Candidates)
	return CodexThirdPartyCandidate{}
}

func cxtpPoolReceivers(t *testing.T, svc *CodexThirdPartyCatalogService) []*Account {
	t.Helper()
	sess, err := svc.newSession(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	receivers, err := sess.compositeReceivers(context.Background(), compositePoolModel)
	require.NoError(t, err)
	return receivers
}

func cxtpPoolIDs(accounts []*Account) []int64 {
	ids := make([]int64, 0, len(accounts))
	for _, account := range accounts {
		ids = append(ids, account.ID)
	}
	return ids
}

func TestCodexTPCompositePoolCrossPlatformClaimsNoLongerConflict(t *testing.T) {
	// ① 多个平台的账号同时精确声明同一模型：不再判歧义；承接集合是 Responses 零转换的账号，
	// 只有 Chat Completions 的 Cline 不在其中；首选按 Priority（Custom 优先于 DeepSeek）。
	svc := cxtpPoolService(t, cxtpPoolCrossPlatformAccounts())
	candidate := cxtpPoolEvaluate(t, svc)

	require.NotEqual(t, CodexModelStatusConflict, candidate.Status, "原因：%v", candidate.Reasons)
	require.True(t, candidate.CanEnable, "状态 %s，原因：%v", candidate.Status, candidate.Reasons)
	require.Equal(t, []int64{10, 12}, candidate.AccountIDs, "Cline 只有 Chat Completions，需转换，不在零转换组里")
	require.Equal(t, PlatformCustom, candidate.TargetPlatform)
	require.NotNil(t, candidate.PrimaryAccount)
	require.Equal(t, CodexThirdPartyRouteAccount{ID: 12, Name: "Custom", Platform: PlatformCustom}, *candidate.PrimaryAccount)
	require.Equal(t, []CodexThirdPartyRouteAccount{{ID: 10, Name: "DeepSeek", Platform: PlatformDeepseek}}, candidate.AlternateAccounts)
	require.Equal(t, "deepseek-flash", candidate.ProfileID)
}

func TestCodexTPCompositePoolMatchesCandidatePoolOnSameFixture(t *testing.T) {
	// ② 同一份账号配置、同一个 Responses 入站：目录页的承接集合与候选池算出的首选平台、零转换组一致。
	cases := []struct {
		name     string
		accounts []Account
	}{
		{"三平台混合", cxtpPoolCrossPlatformAccounts()},
		{"需转换组兜底", []Account{
			cxtpPoolChatOnly(1, PlatformCline, 5, compositePoolMapping(compositePoolModel)),
			cxtpPoolChatOnly(2, PlatformKimi, 9, compositePoolMapping(compositePoolModel)),
			compositePoolAnthropic(3, 1),
		}},
		{"同优先级按账号 ID", []Account{
			compositePoolAdaptive(9, PlatformKimi, 10),
			compositePoolAdaptive(4, PlatformDeepseek, 10),
			compositePoolAdaptive(6, PlatformMiniMax, 10),
		}},
		{"强声明压过弱声明", []Account{
			compositePoolAdaptive(1, PlatformKimi, 50),
			compositePoolWithMapping(compositePoolAdaptive(2, PlatformDeepseek, 1), map[string]any{"deepseek-*": "deepseek-chat"}),
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newCompositePoolFixture(t, tc.accounts...)
			outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)

			svc := cxtpPoolService(t, tc.accounts)
			sess, err := svc.newSession(context.Background(), cxtpCompositeGroup())
			require.NoError(t, err)
			pointers, err := sess.allPlatformAccounts(context.Background())
			require.NoError(t, err)
			set := compositeConfigReceivers(pointers, compositePoolModel, func(account *Account) bool {
				return svc.responsesZeroConversion(account, compositePoolModel)
			})
			primary := set.Primary()
			require.NotNil(t, primary)
			require.Equal(t, outcome.Decision.TargetPlatform, primary.Platform, "首选平台与候选池一致")
			require.Equal(t, outcome.Tier, set.Tier, "声明强度层级一致")

			// 候选池只截取与首选同平台的账号交给下游调度器：承接分组按首选平台截取后必须相等。
			var sameWithPrimary []int64
			for _, account := range set.Accounts {
				if account.Platform == primary.Platform {
					sameWithPrimary = append(sameWithPrimary, account.ID)
				}
			}
			sort.Slice(sameWithPrimary, func(i, j int) bool { return sameWithPrimary[i] < sameWithPrimary[j] })
			require.Equal(t, outcome.Decision.CandidateAccountIDs, sameWithPrimary, "与首选同平台的账号集合与候选池一致")

			// 零转换组：首选账号的零转换结论与候选池的判定一致。
			run := f.pool.newRun(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeCandidatePoolEntry{
				Endpoint: CompositeRouteEndpointResponses, Body: []byte(compositePoolResponsesBody("")),
			}, compositePoolGroupID, compositePoolModel, CompositeRouteEndpointResponses, APIProtocolResponses)
			require.Equal(t, run.isZeroConversion(primary), set.ZeroConversion, "承接分组是否零转换组与候选池判定一致")
		})
	}
}

func TestCodexTPCompositePoolExplicitRouteWins(t *testing.T) {
	// ③ 显式 Composite 路由命中时行为不变：仍按显式路由的单一平台，不走跨平台承接。
	accounts := cxtpPoolCrossPlatformAccounts()
	svc := cxtpPoolService(t, accounts)
	svc.resolver = NewCompositeRouteResolver(cxtpRouteRepo{routes: []CompositeModelRoute{{
		ID: 1, GroupID: 8, PublicModel: compositePoolModel, MatchType: CompositeRouteMatchExact,
		TargetPlatform: PlatformDeepseek, UpstreamModel: "deepseek-flash", Endpoint: CompositeRouteEndpointAny, Enabled: true,
	}}})
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, PlatformDeepseek, candidate.TargetPlatform)
	require.Equal(t, []int64{10}, candidate.AccountIDs, "只有目标平台下的账号")
	require.Nil(t, candidate.PrimaryAccount)
	require.Empty(t, candidate.AlternateAccounts)
}

func TestCodexTPCompositePoolFallsBackToConvertGroup(t *testing.T) {
	// ④ 零转换组为空时用需转换组：全部是 Chat Completions，按 Priority 取首选，需转换组的账号全部入集合。
	accounts := []Account{
		cxtpPoolNamed(cxtpPoolChatOnly(1, PlatformCline, 5, compositePoolMapping(compositePoolModel)), "Cline"),
		cxtpPoolNamed(cxtpPoolChatOnly(2, PlatformKimi, 9, compositePoolMapping(compositePoolModel)), "Kimi"),
	}
	svc := cxtpPoolService(t, accounts)
	receivers := cxtpPoolReceivers(t, svc)
	require.Equal(t, []int64{1, 2}, cxtpPoolIDs(receivers))

	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, PlatformCline, candidate.TargetPlatform)
	require.Equal(t, int64(1), candidate.PrimaryAccount.ID)
	require.Equal(t, []int64{2}, []int64{candidate.AlternateAccounts[0].ID})
}

func TestCodexTPCompositePoolPersistentDisableDropsButCooldownKeeps(t *testing.T) {
	// ⑤ 持久停用（可调度开关关、状态非 active）的账号视同无声明，不计入；冷却中的账号仍计入。
	accounts := cxtpPoolCrossPlatformAccounts()
	soon := time.Now().Add(time.Hour)
	accounts[1].RateLimitResetAt = &soon // Custom 冷却中：静态发布不随冷却抖动，仍计入
	accounts[0].Schedulable = false      // DeepSeek 持久停用：不计入
	svc := cxtpPoolService(t, accounts)
	receivers := cxtpPoolReceivers(t, svc)
	require.Equal(t, []int64{12}, cxtpPoolIDs(receivers), "DeepSeek 被停用，Cline 需转换；只剩冷却中的 Custom")

	accounts[0].Schedulable = true
	accounts[1].Status = StatusDisabled
	svc = cxtpPoolService(t, accounts)
	require.Equal(t, []int64{10}, cxtpPoolIDs(cxtpPoolReceivers(t, svc)), "Custom 状态非 active 不计入")
}

func TestCodexTPCompositePoolCannotUnifyProfileNeedsCapability(t *testing.T) {
	// ⑥ 承接集合里的账号把模型映射到无法套用同一能力档案的上游型号：状态为能力依据不足，原因文案沿用现有说法。
	deepseek := compositePoolWithMapping(compositePoolAdaptive(10, PlatformDeepseek, 30), map[string]any{compositePoolModel: "deepseek-flash"})
	custom := compositePoolWithMapping(compositePoolCustom(12, 10, customFullURLs()), map[string]any{compositePoolModel: "kimi-k3"})
	svc := cxtpPoolService(t, []Account{deepseek, custom})
	candidate := cxtpPoolEvaluate(t, svc)

	require.Equal(t, CodexModelStatusNeedsCapability, candidate.Status)
	require.False(t, candidate.CanEnable)
	require.NotEmpty(t, candidate.Reasons)
	require.Contains(t, candidate.Reasons[0], "多个账号把该模型映射到不同的上游型号")
	require.Contains(t, candidate.Reasons[0], "无法套用单一能力档案")
	require.Equal(t, "deepseek-flash, kimi-k3", candidate.UpstreamModel)
}

func TestCodexTPCompositePoolNativeAndThirdPartyMixConflicts(t *testing.T) {
	// ⑦ 承接集合里既有原生 GPT 又有第三方：沿用原有的归属冲突规则。
	native := compositePoolWithMapping(compositePoolOpenAI(10, 10), map[string]any{compositePoolModel: "gpt-5.1"})
	thirdParty := compositePoolAdaptive(12, PlatformDeepseek, 20)
	// 原生账号需要走 OpenAI 官方地址：不设 base_url 即官方 API 地址。
	svc := cxtpPoolService(t, []Account{native, thirdParty})
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, CodexModelStatusConflict, candidate.Status)
	require.Contains(t, candidate.Reasons, "同名模型同时被原生 OpenAI GPT 与第三方账号承接，归属冲突")
}

func TestCodexTPCompositePoolNonGatewayPlatformMarksUnavailable(t *testing.T) {
	// 承接集合里有不经 OpenAI 网关的平台账号（这里零转换组为空，退到需转换组含 Anthropic 账号）：沿用原文案标不可用。
	accounts := []Account{
		compositePoolAnthropic(3, 1),
		cxtpPoolChatOnly(2, PlatformKimi, 9, compositePoolMapping(compositePoolModel)),
	}
	svc := cxtpPoolService(t, accounts)
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, CodexModelStatusUnavailable, candidate.Status)
	require.Contains(t, candidate.Reasons, "目标平台 anthropic 不经 Responses 网关，无法提供 Codex 目录")
}

func TestCodexTPCompositePoolSinglePlatformUnchanged(t *testing.T) {
	// ⑧ 单平台声明（旧流程可唯一归属）不走跨平台承接：没有首选/备选字段，行为与原来一致。
	accounts := []Account{
		cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 30), "DeepSeek"),
		cxtpPoolNamed(compositePoolAdaptive(11, PlatformDeepseek, 20), "DeepSeek-2"),
	}
	svc := cxtpPoolService(t, accounts)
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, PlatformDeepseek, candidate.TargetPlatform)
	require.Equal(t, []int64{10, 11}, candidate.AccountIDs)
	require.Nil(t, candidate.PrimaryAccount)
	require.Empty(t, candidate.AlternateAccounts)
}

func cxtpPoolCommandCode(id int64, priority int, baseURL string) Account {
	account := compositePoolAccount(id, PlatformCommandCode, priority, map[string]any{
		"api_protocol":  APIProtocolAdaptive,
		"api_base_urls": map[string]any{APIProtocolChatCompletions: baseURL, APIProtocolResponses: baseURL},
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	account.Name = "CommandCode"
	return account
}

func TestCodexTPCompositePoolARM64ShapeWithoutUpstreamCatalog(t *testing.T) {
	// ARM64 实测形态：DeepSeek、CommandCode（按模型分流）、Custom 都精确声明同一个模型。
	// 上游模型目录没有缓存时按内置规则判定：该模型在 CommandCode 上走 Chat Completions，需转换，不在零转换组里。
	baseURL := fmt.Sprintf("http://cc-pool-cold-%d.example/provider/v1", time.Now().UnixNano())
	accounts := []Account{
		cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 30), "DeepSeek"),
		cxtpPoolCommandCode(11, 20, baseURL),
		cxtpPoolNamed(compositePoolCustom(12, 10, customFullURLs()), "Custom"),
	}
	candidate := cxtpPoolEvaluate(t, cxtpPoolService(t, accounts))
	require.Equal(t, CodexModelStatusConfigurable, candidate.Status, "原因：%v", candidate.Reasons)
	require.Equal(t, PlatformCustom, candidate.TargetPlatform)
	require.Equal(t, []int64{10, 12}, candidate.AccountIDs)
	require.Equal(t, []CodexThirdPartyRouteAccount{{ID: 10, Name: "DeepSeek", Platform: PlatformDeepseek}}, candidate.AlternateAccounts)
}

// cxtpPoolWarmCatalog 把 CommandCode 账号的上游模型目录写进进程内缓存（模拟运行时请求已预热），
// 目录里的键就是该账号对公开模型名的路由模型名；返回清理函数。
func cxtpPoolWarmCatalog(t *testing.T, account *Account, baseURL string, protocols []string) {
	t.Helper()
	routingModel := upstreamRoutingModel(account, []byte(`{"model":"`+compositePoolModel+`"}`), "")
	key := modelProtocolCatalogKey(account, buildOpenAIModelsURL(baseURL))
	upstreamModelProtocols.store(key, map[string][]string{strings.ToLower(routingModel): protocols}, nil, time.Now())
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, key)
	})
}

// cxtpPoolARM64Accounts 是 ARM64 分组 8 的形态：DeepSeek（优先级 1）、CommandCode（2，映射到带厂商前缀的上游型号）、
// HUB_OpenAI（openai 平台中转，10）、Cline（只有 Chat Completions）。
func cxtpPoolARM64Accounts(baseURL string) []Account {
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	hub := compositePoolAccount(13, PlatformOpenAI, 10, map[string]any{
		"base_url":      "https://hub.example.com/v1",
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	hub.Name = "HUB_OpenAI"
	return []Account{
		cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 1), "DeepSeek"),
		commandCode,
		hub,
		cxtpPoolNamed(cxtpPoolChatOnly(14, PlatformCline, 5, compositePoolMapping(compositePoolModel)), "Cline"),
	}
}

func TestCodexTPCompositePoolModelRoutedAccountUsesCachedUpstreamCatalog(t *testing.T) {
	// 运行时请求已经预热过 CommandCode 的上游模型目录（该模型支持 Responses）：目录页读同一份缓存，
	// CommandCode 进零转换组，协议记为原生 Responses，与 DeepSeek、HUB 同属一个能力档案，整组可发布；
	// 首选按 Priority 是 DeepSeek，CommandCode 与 HUB 是备选；读页面不向上游发请求。
	baseURL := fmt.Sprintf("http://cc-pool-warm-%d.example/provider/v1", time.Now().UnixNano())
	accounts := cxtpPoolARM64Accounts(baseURL)
	cxtpPoolWarmCatalog(t, &accounts[1], baseURL, []string{APIProtocolChatCompletions, APIProtocolResponses})

	f := newCompositePoolFixture(t, accounts...)
	upstream := &recordingHTTPUpstream{}
	f.openAI.httpUpstream = upstream
	outcome := f.resolveHandled(f.ctxFor(CompositeRouteEndpointResponses, compositePoolResponsesBody(""), nil), CompositeRouteEndpointResponses)
	require.Equal(t, PlatformDeepseek, outcome.Decision.TargetPlatform, "候选池：DeepSeek 零转换且 Priority 最高")

	svc := cxtpPoolService(t, accounts)
	svc.fetcher = openAICodexBaseFetcher{svc: f.openAI}
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, PlatformDeepseek, candidate.TargetPlatform, "目录页与候选池首选平台一致")
	require.Equal(t, []int64{10, 11, 13}, candidate.AccountIDs, "Cline 只有 Chat Completions，不在零转换组里")
	require.Equal(t, CodexModelStatusConfigurable, candidate.Status, "原因：%v", candidate.Reasons)
	require.True(t, candidate.CanEnable)
	require.Equal(t, CodexThirdPartyProtocolNative, candidate.Protocol)
	require.Equal(t, "deepseek-flash", candidate.ProfileID)
	require.Equal(t, int64(10), candidate.PrimaryAccount.ID)
	require.Equal(t, []CodexThirdPartyRouteAccount{
		{ID: 11, Name: "CommandCode", Platform: PlatformCommandCode},
		{ID: 13, Name: "HUB_OpenAI", Platform: PlatformOpenAI},
	}, candidate.AlternateAccounts)
	require.Contains(t, candidate.UpstreamModel, "deepseek/deepseek-v4.1-flash", "CommandCode 的最终上游型号带厂商前缀")
	require.Zero(t, upstream.calls, "读目录页不得向上游发请求")
}

func TestCodexTPCompositePoolModelRoutedProtocolFollowsCachedCatalog(t *testing.T) {
	// 按模型分流账号的协议跟随已缓存的上游模型目录：支持 Responses 为原生通路，只支持 Chat Completions
	// 为转换通路；缓存缺失、该模型不在目录里、目录给出其它协议时维持"无法静态确认"（空串）。
	cases := []struct {
		name      string
		warm      bool
		protocols []string
		want      string
	}{
		{"目录支持 Responses", true, []string{APIProtocolChatCompletions, APIProtocolResponses}, CodexThirdPartyProtocolNative},
		{"目录只有 Chat Completions", true, []string{APIProtocolChatCompletions}, CodexThirdPartyProtocolViaChat},
		{"目录没有缓存", false, nil, ""},
		{"目录给出其它协议", true, []string{APIProtocolAnthropic}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			baseURL := fmt.Sprintf("http://cc-proto-%d.example/provider/v1", time.Now().UnixNano())
			commandCode := cxtpPoolCommandCode(11, 2, baseURL)
			if tc.warm {
				cxtpPoolWarmCatalog(t, &commandCode, baseURL, tc.protocols)
			}
			svc := cxtpPoolService(t, []Account{commandCode})
			svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{httpUpstream: &recordingHTTPUpstream{}}}
			require.Equal(t, tc.want, svc.accountProtocol(&commandCode, compositePoolModel))
		})
	}
	// 普通账号不受影响：沿用账号自身配置。
	svc := cxtpPoolService(t, nil)
	deepseek := compositePoolAdaptive(10, PlatformDeepseek, 1)
	require.Equal(t, codexTPAccountProtocol(&deepseek), svc.accountProtocol(&deepseek, compositePoolModel))
}

func TestCodexTPCompositePoolModelRoutedCacheMissingKeepsOriginalReason(t *testing.T) {
	// 缓存缺失：CommandCode 按内置规则走 Chat Completions，需转换，不在零转换组，整组仍可发布。
	// 只有当它被迫进入承接集合（零转换组为空）且目录没有缓存时，才停在"无法静态确认 Responses 通路"。
	baseURL := fmt.Sprintf("http://cc-pool-cold2-%d.example/provider/v1", time.Now().UnixNano())
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	cline := cxtpPoolChatOnly(14, PlatformCline, 5, compositePoolMapping(compositePoolModel))
	svc := cxtpPoolService(t, []Account{commandCode, cline})
	svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{httpUpstream: &recordingHTTPUpstream{}}}
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, []int64{11, 14}, candidate.AccountIDs, "两个账号都需转换，退到需转换组")
	require.Equal(t, CodexModelStatusNeedsCapability, candidate.Status)
	require.Contains(t, candidate.Reasons, "部分承接账号按模型分流上游协议，无法静态确认 Responses 通路")
}

func TestCodexTPCompositePoolConvertGroupWithCachedChatCatalogIsViaChat(t *testing.T) {
	// 两个账号都需转换（CommandCode 缓存目录只有 Chat Completions，Cline 固定 Chat Completions）：
	// 协议确定为转换通路，整组按"Responses 经 Chat 转换"发布。
	baseURL := fmt.Sprintf("http://cc-pool-chat-%d.example/provider/v1", time.Now().UnixNano())
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	cxtpPoolWarmCatalog(t, &commandCode, baseURL, []string{APIProtocolChatCompletions})
	cline := cxtpPoolChatOnly(14, PlatformCline, 5, compositePoolMapping(compositePoolModel))
	svc := cxtpPoolService(t, []Account{commandCode, cline})
	svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{httpUpstream: &recordingHTTPUpstream{}}}
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, []int64{11, 14}, candidate.AccountIDs)
	require.Equal(t, CodexThirdPartyProtocolViaChat, candidate.Protocol, "原因：%v", candidate.Reasons)
	require.NotEqual(t, CodexModelStatusNeedsCapability, candidate.Status, "原因：%v", candidate.Reasons)
}

func TestModelProtocolCatalogPeekNeverRefreshes(t *testing.T) {
	// 只读缓存查询：目录没缓存时返回 nil，且不触发后台刷新、不发上游请求。
	baseURL := fmt.Sprintf("http://cc-peek-%d.example/provider/v1", time.Now().UnixNano())
	account := cxtpPoolCommandCode(21, 5, baseURL)
	upstream := &recordingHTTPUpstream{}
	svc := &OpenAIGatewayService{httpUpstream: upstream}
	require.Nil(t, svc.modelCatalogProtocolsCached(&account, compositePoolModel))
	_, key, ok := modelCatalogEndpoint(&account, compositePoolModel)
	require.True(t, ok)
	upstreamModelProtocols.mu.Lock()
	_, created := upstreamModelProtocols.entries[key]
	upstreamModelProtocols.mu.Unlock()
	require.False(t, created, "只读查询不创建缓存条目、不安排刷新")
	require.Zero(t, upstream.calls)

	upstreamModelProtocols.store(key, map[string][]string{compositePoolModel: {APIProtocolResponses}}, nil, time.Now())
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, key)
	})
	require.Equal(t, []string{APIProtocolResponses}, svc.modelCatalogProtocolsCached(&account, "DeepSeek-Flash"), "模型名大小写不敏感")
}
