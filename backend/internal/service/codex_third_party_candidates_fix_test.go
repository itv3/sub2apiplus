package service

// 针对 ARM64 联调反馈的修复用例：
//   - 能力档案匹配去厂商前缀、忽略大小写，deepseek-flash 档案登记上游别名；
//   - 多个账号映射到不同原始上游名、规范化后命中同一档案时不算冲突；
//   - 体积预算与运行时一致：个别账号基础目录拉取失败不挡住整个分组；
//   - 候选与目录只认分组默认启用的模型名单；
//   - 按生产形态（官方 OAuth 空映射 + 显式映射的中转账号 + 白名单）构造 codex-auto-review 的原生判定。

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"
)

// cxtpProdShapeAccounts 返回 ARM64 实测的账号形态：官方 ChatGPT OAuth 账号（空映射）加三个显式映射的中转账号。
func cxtpProdShapeAccounts() []Account {
	return []Account{
		cxtpOAuthAccount(90),
		// HUB_OpenAI：deepseek-flash 同名映射。
		cxtpOpenAIKeyAccount(105, "https://hub.example.com/v1", map[string]any{"deepseek-flash": "deepseek-flash"}),
		// commandcode：全部映射到带厂商前缀的上游型号。
		cxtpOpenAIKeyAccount(109, "https://commandcode.example.com/v1", map[string]any{
			"Kimi-K3":        "moonshotai/Kimi-K3",
			"GLM-5.3":        "zai-org/GLM-5.3",
			"deepseek-flash": "deepseek/deepseek-v4.1-flash",
		}),
		// ClinPass：映射到 cline-pass 前缀的上游型号。
		cxtpOpenAIKeyAccount(110, "https://cline.example.com/v1", map[string]any{"deepseek-flash": "cline-pass/deepseek-v4.1-flash"}),
	}
}

// cxtpProdShapeService 构造生产形态的 Composite 评估服务。Composite 解析器装上与生产一致的
// 「按账号显式映射判定归属」逻辑（生产里由网关服务注入）。
func cxtpProdShapeService(t *testing.T, cfg map[int64]CodexModelVisibilityConfig, fetcher *cxtpFetcher) (*CodexThirdPartyCatalogService, []Account) {
	t.Helper()
	accounts := cxtpProdShapeAccounts()
	svc := newCXTPService(t, &cxtpSettings{cfg: cfg}, accounts, fetcher)
	svc.resolver.SetModelOwnershipResolver(func(_ context.Context, _ int64, model string) (CompositeModelOwnership, error) {
		platforms := map[string]struct{}{}
		for _, account := range accounts {
			if explicitModelMappingClaims(account, model) {
				platforms[account.Platform] = struct{}{}
			}
		}
		if len(platforms) == 1 {
			return CompositeModelOwnership{Matched: true, TargetPlatform: PlatformOpenAI}, nil
		}
		return CompositeModelOwnership{Ambiguous: len(platforms) > 1}, nil
	})
	return svc, accounts
}

func TestCodexThirdPartyProfileMatchStripsVendorPrefixAndIgnoresCase(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	for model, wantID := range map[string]string{
		"moonshotai/Kimi-K3":                  "kimi-k3",
		"Kimi-K3":                             "kimi-k3",
		"zai-org/GLM-5.3":                     "glm-5.3",
		"GLM-5.3":                             "glm-5.3",
		"deepseek-flash":                      "deepseek-flash",
		"DeepSeek-Flash":                      "deepseek-flash",
		"deepseek/deepseek-v4.1-flash":        "deepseek-flash",
		"cline-pass/deepseek-v4.1-flash":      "deepseek-flash",
		"deepseek-v4-flash":                   "deepseek-flash",
		"deepseek-v4-flash-vision-exp":        "deepseek-flash",
		"vendor/deepseek-v4-flash-vision-exp": "deepseek-flash",
		"deepseek-v4-pro":                     "deepseek-v4-pro",
		" models/deepseek-v4-pro ":            "deepseek-v4-pro",
	} {
		profile := set.match(model)
		require.NotNil(t, profile, model)
		require.Equal(t, wantID, profile.ID, model)
	}
	for _, model := range []string{"", "vendor/", "vendor/unknown-model", "kimi-k2", "deepseek-v4"} {
		require.Nil(t, set.match(model), model)
	}
}

func TestCodexThirdPartyDeepSeekFlashProfileRecordsAliasEvidence(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	flash := set.match("deepseek-flash")
	require.NotNil(t, flash)
	require.Equal(t, "deepseek-flash", flash.UpstreamModels[0], "指令模板按第一个型号取，别名只能追加在后面")
	require.Subset(t, flash.UpstreamModels, []string{"deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"})
	require.Contains(t, flash.Sources, "https://api-docs.deepseek.com/quick_start/pricing")
	require.Equal(t, "2026-10-23", flash.CheckedAt)
	// 别名不改变模板校验：运行时模板仍与档案固定的摘要一致。
	_, digest := codexThirdPartyRuntimeTemplate(flash)
	require.Equal(t, flash.InstructionsTemplateSHA256, digest)
	// 同一份档案不会被两个不同 ID 争抢别名。
	require.Equal(t, "deepseek-v4-pro", set.match("deepseek-v4-pro").ID)
}

func TestCodexThirdPartyProdShapeCandidatesAllEnableWithListNames(t *testing.T) {
	svc, _ := cxtpProdShapeService(t, nil, &cxtpFetcher{bodies: map[int64][]byte{
		90: cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 0)),
	}})
	// 分组默认名单：账号映射的公开名（别名生效、没映射的显示原名），上游原名已被隐藏。
	svc.groupModels = func(context.Context, *Group) ([]string, error) {
		return []string{"gpt-6.1-sol", "Kimi-K3", "GLM-5.3", "deepseek-flash"}, nil
	}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	bySlug := map[string]CodexThirdPartyCandidate{}
	for _, c := range eval.Candidates {
		bySlug[c.Slug] = c
	}
	require.Len(t, bySlug, 4, "只有名单里的模型，加上基础目录里的原生行")
	require.True(t, bySlug["gpt-6.1-sol"].Native)

	for slug, want := range map[string]struct{ profile, upstream string }{
		"Kimi-K3":        {"kimi-k3", "moonshotai/Kimi-K3"},
		"GLM-5.3":        {"glm-5.3", "zai-org/GLM-5.3"},
		"deepseek-flash": {"deepseek-flash", "cline-pass/deepseek-v4.1-flash, deepseek-flash, deepseek/deepseek-v4.1-flash"},
	} {
		c := bySlug[slug]
		require.Equal(t, slug, c.DisplayName, "显示名取名单里的名字")
		require.True(t, c.CanEnable, "%s：%v", slug, c.Reasons)
		require.Equal(t, CodexModelStatusConfigurable, c.Status, slug)
		require.Equal(t, want.profile, c.ProfileID, slug)
		require.Equal(t, want.upstream, c.UpstreamModel, slug)
		require.NotEmpty(t, c.Fingerprint, slug)
	}
}

func TestCodexThirdPartyProdShapeRuntimePublishesListNamesAsSlugs(t *testing.T) {
	cfg := cxtpConfig(1, "Kimi-K3", "GLM-5.3", "deepseek-flash")
	svc, _ := cxtpProdShapeService(t, cfg, &cxtpFetcher{})
	svc.groupModels = func(context.Context, *Group) ([]string, error) {
		return []string{"gpt-6.1-sol", "Kimi-K3", "GLM-5.3", "deepseek-flash"}, nil
	}
	base := cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 0))
	got, err := svc.ApplyToBody(context.Background(), cxtpCompositeGroup(), base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-6.1-sol", "GLM-5.3", "Kimi-K3", "deepseek-flash"}, cxtpSlugs(t, got))
	for _, slug := range []string{"Kimi-K3", "GLM-5.3", "deepseek-flash"} {
		fields := cxtpEntry(t, got, slug)
		require.JSONEq(t, `"`+slug+`"`, string(fields["slug"]))
		require.JSONEq(t, `"`+slug+`"`, string(fields["display_name"]))
	}
}

func TestCodexThirdPartyDifferentRawUpstreamNamesOfSameProfileAreNotConflict(t *testing.T) {
	// 两个账号把同一公开名映射到不同的原始上游名，规范化后命中同一档案。
	a := cxtpOpenAIKeyAccount(1, "https://a.example", map[string]any{"flash": "deepseek/deepseek-v4.1-flash"})
	b := cxtpOpenAIKeyAccount(2, "https://b.example", map[string]any{"flash": "DeepSeek-V4-Flash"})
	svc := newCXTPService(t, &cxtpSettings{}, []Account{a, b}, nil)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.Len(t, eval.Candidates, 1)
	require.True(t, eval.Candidates[0].CanEnable, "%v", eval.Candidates[0].Reasons)
	require.Equal(t, "deepseek-flash", eval.Candidates[0].ProfileID)

	// 映射到不同档案的型号仍然拒绝套用单一档案。
	c := cxtpOpenAIKeyAccount(3, "https://c.example", map[string]any{"flash": "zai-org/GLM-5.3"})
	svc = newCXTPService(t, &cxtpSettings{}, []Account{a, c}, nil)
	eval, err = svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.False(t, eval.Candidates[0].CanEnable)
	require.Equal(t, CodexModelStatusNeedsCapability, eval.Candidates[0].Status)
	require.Contains(t, eval.Candidates[0].Reasons[0], "无法套用单一能力档案")

	// 其中一个型号没有档案：指出具体是哪个。
	d := cxtpOpenAIKeyAccount(4, "https://d.example", map[string]any{"flash": "vendor/unknown-model"})
	svc = newCXTPService(t, &cxtpSettings{}, []Account{a, d}, nil)
	eval, err = svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.False(t, eval.Candidates[0].CanEnable)
	require.Contains(t, eval.Candidates[0].Reasons[0], "vendor/unknown-model")
}

// ---------------------------------------------------------------------------
// 体积预算：个别账号失败即换号
// ---------------------------------------------------------------------------

func TestCodexThirdPartyBudgetSkipsFailedBaseAccountsAndTakesWorstOfSuccesses(t *testing.T) {
	small, large := cxtpOAuthAccount(1), cxtpOAuthAccount(2)
	relayA := cxtpOpenAIKeyAccount(105, "https://hub.example.com/v1", map[string]any{"deepseek-flash": "deepseek-flash"})
	relayB := cxtpOpenAIKeyAccount(110, "https://cline.example.com/v1", map[string]any{"deepseek-flash": "cline-pass/deepseek-v4.1-flash"})
	fetcher := &cxtpFetcher{
		bodies: map[int64][]byte{
			1: cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 100)),
			2: cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 5000)),
		},
		errs: map[int64]error{105: errors.New("hub unreachable"), 110: errors.New("cline unreachable")},
	}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "deepseek-flash")}, []Account{small, large, relayA, relayB}, fetcher)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "deepseek-flash")[8])
	require.NoError(t, err)
	require.True(t, eval.Budget.Complete, "至少一个账号成功即完成：%s", eval.Budget.Reason)
	require.Empty(t, eval.Budget.Reason)
	require.Equal(t, []int64{1, 2, 105, 110}, eval.Budget.BaseAccountIDs)

	// 最坏体积取成功者的最大值：等于以大目录账号为基础、开启 deepseek-flash 后的实际正文长度。
	runtime, err := svc.ApplyToBody(context.Background(), group, fetcher.bodies[2])
	require.NoError(t, err)
	require.Equal(t, len(runtime), eval.Budget.WorstCaseBytes)
	for _, c := range eval.Candidates {
		if c.Slug == "deepseek-flash" {
			require.True(t, c.CanEnable, "%v", c.Reasons)
		}
	}
}

func TestCodexThirdPartyBudgetIncompleteOnlyWhenEveryBaseAccountFails(t *testing.T) {
	relayA := cxtpOpenAIKeyAccount(105, "https://hub.example.com/v1", map[string]any{"deepseek-flash": "deepseek-flash"})
	relayB := cxtpOpenAIKeyAccount(110, "https://cline.example.com/v1", map[string]any{"deepseek-flash": "cline-pass/deepseek-v4.1-flash"})
	fetcher := &cxtpFetcher{errs: map[int64]error{105: errors.New("down"), 110: errors.New("down")}}
	svc := newCXTPService(t, &cxtpSettings{}, []Account{relayA, relayB}, fetcher)
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), &Group{ID: 8, Platform: PlatformOpenAI}, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.False(t, eval.Budget.Complete)
	require.Contains(t, eval.Budget.Reason, "账号 105")
	require.Contains(t, eval.Budget.Reason, "账号 110")

	// 分组里没有 OpenAI 平台账号：按空基础估算，视为完成。
	zhipu := cxtpCNAccount(5, PlatformZhipu, "https://zhipu.example", "")
	zhipu.Credentials["model_mapping"] = map[string]any{"glm-5.3": "glm-5.3"}
	svc = newCXTPService(t, &cxtpSettings{}, []Account{zhipu}, nil)
	eval, err = svc.EvaluateCodexThirdPartyCatalog(context.Background(), &Group{ID: 8, Platform: PlatformZhipu}, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.True(t, eval.Budget.Complete)
}

func TestCodexThirdPartyBudgetPinnedGroupSkipsFailedPinnedAccount(t *testing.T) {
	good, bad := cxtpOAuthAccount(1), cxtpOAuthAccount(2)
	relay := cxtpOpenAIKeyAccount(4, "https://site.example", map[string]any{"deepseek-flash": "deepseek-flash"})
	fetcher := &cxtpFetcher{
		bodies: map[int64][]byte{1: cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 0))},
		errs:   map[int64]error{2: errors.New("pinned account down")},
	}
	svc := newCXTPService(t, &cxtpSettings{}, []Account{good, bad, relay}, fetcher)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	group.CodexModelsManifestConfig.Enabled = true
	group.CodexModelsManifestConfig.AccountIDs = []int64{2, 1}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "deepseek-flash")[8])
	require.NoError(t, err)
	require.True(t, eval.Budget.Complete, eval.Budget.Reason)
}

// ---------------------------------------------------------------------------
// codex-auto-review 的原生判定
// ---------------------------------------------------------------------------

func TestCodexThirdPartyProdShapeKeepsCodexAutoReviewNative(t *testing.T) {
	// 生产形态：官方 OAuth 账号可调度（空映射），中转账号都是显式映射，分组白名单含 codex-auto-review，
	// 基础目录（官方上游）列出它。运行时发 /v1/responses 能由官方账号承接，目录里必须原样保留。
	base := cxtpBase(
		cxtpGPTEntry("gpt-6.1-sol", 20), cxtpGPTEntry("gpt-6-astra", 20), cxtpGPTEntry("gpt-6-luna", 20), cxtpGPTEntry("codex-auto-review", 20))
	group := cxtpCompositeGroup()
	group.ModelAllowlist = GroupModelAllowlist{Enabled: true, Models: []string{"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "codex-auto-review", "Kimi-K3", "GLM-5.3", "deepseek-flash"}}
	svc, _ := cxtpProdShapeService(t, cxtpConfig(1, "Kimi-K3"), &cxtpFetcher{bodies: map[int64][]byte{90: base}})
	svc.groupModels = func(context.Context, *Group) ([]string, error) {
		return []string{"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "codex-auto-review", "Kimi-K3", "GLM-5.3", "deepseek-flash"}, nil
	}

	// 运行时：四个原生条目逐字节保留，开启的 Kimi-K3 追加在后。
	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "codex-auto-review", "Kimi-K3"}, cxtpSlugs(t, got))
	_, want, err := parseCodexCatalogBody(base)
	require.NoError(t, err)
	_, have, err := parseCodexCatalogBody(got)
	require.NoError(t, err)
	for i := range want {
		require.Equal(t, string(want[i].raw), string(have[i].raw), want[i].slug)
	}

	// 评估器：codex-auto-review 是原生只读行，不会变成第三方候选。
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "Kimi-K3")[8])
	require.NoError(t, err)
	var native []string
	for _, c := range eval.Candidates {
		if c.Native {
			native = append(native, c.Slug)
		} else {
			require.NotEqual(t, "codex-auto-review", c.Slug)
		}
	}
	require.Equal(t, []string{"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "codex-auto-review"}, native)

	// 路由层：承接账号只有官方 OAuth 账号 90，判定为原生。
	sess, err := svc.newSession(context.Background(), group)
	require.NoError(t, err)
	route, err := sess.route(context.Background(), "codex-auto-review")
	require.NoError(t, err)
	require.True(t, route.Native, "%+v", route.Reasons)
	require.Equal(t, []int64{90}, route.AccountIDs)
}
