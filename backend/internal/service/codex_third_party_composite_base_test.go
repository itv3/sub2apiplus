package service

// Composite 分组在线目录的基础目录：只从可调度的官方账号拉取，拉不到就按账号映射本地生成。
// 夹具是 ARM64 分组 8 的形态：官方 OAuth 账号 #90 被手动暂停调度，另有三个显式映射的中转账号。

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// cxtpGroup8Accounts 返回分组 8 的账号：#90 官方 OAuth（空映射），#105、#109、#110 中转账号。
func cxtpGroup8Accounts() []Account {
	self := func(models ...string) map[string]any {
		mapping := map[string]any{}
		for _, model := range models {
			mapping[model] = model
		}
		return mapping
	}
	hub := cxtpOpenAIKeyAccount(105, "https://hub.example.com/v1", self("gpt-6-astra", "gpt-6.1-sol", "deepseek-flash", "codex-auto-review", "deepseek-v4.1-flash"))
	commandcode := cxtpOpenAIKeyAccount(109, "https://commandcode.example.com/v1", map[string]any{
		"GLM-5.3": "zai-org/GLM-5.3", "Kimi-K3": "moonshotai/Kimi-K3", "deepseek-flash": "deepseek/deepseek-v4.1-flash",
	})
	clinpass := cxtpOpenAIKeyAccount(110, "https://cline.example.com/v1", map[string]any{"deepseek-flash": "cline-pass/deepseek-v4.1-flash"})
	return []Account{cxtpOAuthAccount(90), hub, commandcode, clinpass}
}

func cxtpGroup8() *Group {
	group := cxtpCompositeGroup()
	group.ModelAllowlist = GroupModelAllowlist{Enabled: true, Models: []string{
		"gpt-6.1-sol", "gpt-6-astra", "codex-auto-review", "deepseek-flash", "Kimi-K3", "GLM-5.3",
	}}
	return group
}

// cxtpGroup8Service 构造分组 8 的服务。paused 里的账号暂停调度（官方账号 #90 默认暂停）。
func cxtpGroup8Service(t *testing.T, enabled []string, paused ...int64) (*CodexThirdPartyCatalogService, *cxtpRepo, *cxtpFetcher) {
	t.Helper()
	accounts := cxtpGroup8Accounts()
	fetcher := &cxtpFetcher{bodies: map[int64][]byte{}}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, enabled...)}, accounts, fetcher)
	repo, ok := svc.accountRepo.(*cxtpRepo)
	require.True(t, ok)
	repo.paused = map[int64]bool{}
	for _, id := range paused {
		repo.paused[id] = true
	}
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
	// 名单替身：账号映射的公开名（普通模型列表的来源）。
	svc.groupModels = func(context.Context, *Group) ([]string, error) {
		return []string{"gpt-6-astra", "gpt-6.1-sol", "codex-auto-review", "deepseek-flash", "deepseek-v4.1-flash", "GLM-5.3", "Kimi-K3"}, nil
	}
	return svc, repo, fetcher
}

// cxtpCompositeFetch 记录被拉取的官方账号，并按预设返回目录或错误。
type cxtpCompositeFetch struct {
	mu     sync.Mutex
	calls  []int64
	bodies map[int64][]byte
	errs   map[int64]error
	block  bool
}

func (f *cxtpCompositeFetch) fetch(ctx context.Context, account *Account) (*OpenAIModelsResponse, error) {
	f.mu.Lock()
	f.calls = append(f.calls, account.ID)
	f.mu.Unlock()
	if f.block {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if err := f.errs[account.ID]; err != nil {
		return nil, err
	}
	return &OpenAIModelsResponse{Body: f.bodies[account.ID]}, nil
}

func (f *cxtpCompositeFetch) called() []int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]int64(nil), f.calls...)
}

func TestCodexCompositeOnlyRelayAccountsBuildsLocalBaseWithoutFetching(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, []string{"deepseek-flash"}, 90)
	group := cxtpGroup8()
	fetch := &cxtpCompositeFetch{}

	manifest, account, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, fetch.fetch)
	require.NoError(t, err)
	require.Nil(t, account, "本地生成不对应任何官方账号")
	require.Empty(t, fetch.called(), "分组里只有中转账号和暂停的官方账号：不拉任何目录")
	require.NotEmpty(t, manifest.ETag)

	// 本地基础目录：账号映射出来的公开名，按白名单过滤；codex-auto-review 因白名单显式列出而可见。
	require.ElementsMatch(t, []string{"GLM-5.3", "Kimi-K3", "codex-auto-review", "deepseek-flash", "gpt-6-astra", "gpt-6.1-sol"}, cxtpSlugs(t, manifest.Body))

	// 再经第三方策略：GPT 条目按路由判定为原生保留，未开启的三方条目移除，已开启的 deepseek-flash 换成档案条目。
	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	slugs := cxtpSlugs(t, manifest.Body)
	require.Equal(t, []string{"codex-auto-review", "deepseek-flash", "gpt-6-astra", "gpt-6.1-sol"}, slugs)
	flash := cxtpEntry(t, manifest.Body, "deepseek-flash")
	require.JSONEq(t, `"list"`, string(flash["visibility"]))
	require.Contains(t, string(flash["input_modalities"]), "image")
	require.Contains(t, string(flash["model_messages"]), "instructions_template")
	for _, slug := range []string{"gpt-6-astra", "gpt-6.1-sol", "codex-auto-review"} {
		require.JSONEq(t, `"list"`, string(cxtpEntry(t, manifest.Body, slug)["visibility"]), slug)
	}
}

func TestCodexCompositeDisabledThirdPartyModelsStayHidden(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, nil, 90)
	group := cxtpGroup8()
	manifest, _, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, nil)
	require.NoError(t, err)
	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	require.Equal(t, []string{"codex-auto-review", "gpt-6-astra", "gpt-6.1-sol"}, cxtpSlugs(t, manifest.Body))
}

func TestCodexCompositeSchedulableOfficialAccountSuppliesTheBase(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, []string{"deepseek-flash"}) // #90 可调度
	group := cxtpGroup8()
	official := cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 10), cxtpGPTEntry("gpt-6-luna", 10))
	fetch := &cxtpCompositeFetch{bodies: map[int64][]byte{90: official}}

	manifest, account, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, fetch.fetch)
	require.NoError(t, err)
	require.NotNil(t, account)
	require.Equal(t, int64(90), account.ID)
	require.Equal(t, []int64{90}, fetch.called(), "只拉官方账号，中转账号不拉")
	require.Equal(t, string(official), string(manifest.Body), "用官方目录，不混入本地生成的条目")

	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	require.Equal(t, []string{"gpt-6.1-sol", "gpt-6-luna", "deepseek-flash"}, cxtpSlugs(t, manifest.Body))
}

func TestCodexCompositeOfficialFailureFallsBackToLocalBase(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, []string{"deepseek-flash"})
	group := cxtpGroup8()

	// 拉取报错。
	failing := &cxtpCompositeFetch{errs: map[int64]error{90: errors.New("upstream 502")}}
	manifest, account, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, failing.fetch)
	require.NoError(t, err)
	require.Nil(t, account)
	require.Equal(t, []int64{90}, failing.called())
	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	require.Equal(t, []string{"codex-auto-review", "deepseek-flash", "gpt-6-astra", "gpt-6.1-sol"}, cxtpSlugs(t, manifest.Body))

	// 拉取超过基础目录预算：请求上下文仍然有效，改为本地生成而不是 500。
	slow := &cxtpCompositeFetch{block: true}
	fetchCtx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond) // time-threshold: allow 被测对象就是超时或取消语义，上下文截止时间本身是输入，机器变慢只会让超时更早触发
	defer cancel()
	manifest, account, err = svc.BuildCompositeBaseManifest(context.Background(), fetchCtx, group, slow.fetch)
	require.NoError(t, err)
	require.Nil(t, account)
	require.Equal(t, []int64{90}, slow.called())
	require.NotEmpty(t, cxtpSlugs(t, manifest.Body))

	// 返回空目录同样视为失败。
	empty := &cxtpCompositeFetch{bodies: map[int64][]byte{}}
	_, account, err = svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, empty.fetch)
	require.NoError(t, err)
	require.Nil(t, account)
}

func TestCodexCompositePausedOfficialAccountIsNeverUsed(t *testing.T) {
	svc, repo, _ := cxtpGroup8Service(t, []string{"deepseek-flash"}, 90)
	group := cxtpGroup8()
	fetch := &cxtpCompositeFetch{bodies: map[int64][]byte{90: cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 10))}}

	manifest, account, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, fetch.fetch)
	require.NoError(t, err)
	require.Nil(t, account)
	require.Empty(t, fetch.called(), "暂停调度的官方账号不得发起任何请求")
	require.Positive(t, repo.listCalls, "可调度判断来自账号仓库的调度查询")
	require.NotContains(t, string(manifest.Body), `"x_unknown_field"`, "没有用官方账号的目录")

	// 官方账号恢复可调度后才会被使用（夹具里只是取消暂停标记，不涉及任何真实账号开关）。
	delete(repo.paused, 90)
	_, account, err = svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, fetch.fetch)
	require.NoError(t, err)
	require.NotNil(t, account)
	require.Equal(t, int64(90), account.ID)
}

func TestCodexCompositeRelayAccountsAreNeverFetchedEvenWhenOfficialFails(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, nil)
	group := cxtpGroup8()
	fetch := &cxtpCompositeFetch{errs: map[int64]error{90: errors.New("down")}}
	_, _, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, fetch.fetch)
	require.NoError(t, err)
	for _, id := range fetch.called() {
		require.Equal(t, int64(90), id, "中转账号 %d 不得拿来拉目录", id)
	}
}

func TestCodexCompositeLocalBaseWithoutOpenAIModelsStillPublishesEnabledThirdParty(t *testing.T) {
	// 分组里只有 DeepSeek 平台账号：本地生成得到空目录，再叠加已开启的第三方条目。
	deepseek := cxtpCNAccount(3, PlatformDeepseek, "https://ds.example", "responses")
	deepseek.Credentials["model_mapping"] = map[string]any{"deepseek-flash": "deepseek-flash"}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "deepseek-flash")}, []Account{deepseek}, nil)
	group := cxtpCompositeGroup()
	manifest, account, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, nil)
	require.NoError(t, err)
	require.Nil(t, account)
	require.JSONEq(t, `{"models":[]}`, string(manifest.Body))
	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	require.Equal(t, []string{"deepseek-flash"}, cxtpSlugs(t, manifest.Body))
}

func TestCodexCompositeBuildReportsDatabaseFailure(t *testing.T) {
	svc, repo, _ := cxtpGroup8Service(t, nil, 90)
	repo.err = errors.New("db down")
	_, _, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), cxtpGroup8(), nil)
	require.Error(t, err)
}

// ---------------------------------------------------------------------------
// 管理页预算：没有可用官方账号时取本地生成的基础目录
// ---------------------------------------------------------------------------

func TestCodexCompositeBudgetWithoutOfficialAccountUsesLocalBase(t *testing.T) {
	svc, _, fetcher := cxtpGroup8Service(t, []string{"deepseek-flash"}, 90)
	group := cxtpGroup8()
	cfg := cxtpConfig(1, "deepseek-flash")[8]

	for name, ctx := range map[string]context.Context{
		"离线（页面读取）": WithCodexThirdPartyOfflineEvaluation(context.Background()),
		"预览与保存":    context.Background(),
	} {
		eval, err := svc.EvaluateCodexThirdPartyCatalog(ctx, group, cfg)
		require.NoError(t, err, name)
		require.True(t, eval.Budget.Complete, "%s：%s", name, eval.Budget.Reason)
		require.False(t, eval.Budget.Deferred, name)
		require.Empty(t, eval.Budget.BaseAccountIDs, name)
		require.Empty(t, fetcher.fetchedIDs(), "%s：不发起任何基础目录获取", name)

		// 预算等于运行时（本地生成再叠加策略）的最终目录体积。
		manifest, _, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, nil)
		require.NoError(t, err)
		require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
		require.Equal(t, len(manifest.Body), eval.Budget.WorstCaseBytes, name)
		require.Equal(t, 3, eval.Budget.NativeCount, name)
		require.Equal(t, 1, eval.Budget.EnabledThirdPartyCount, name)

		// 原生行来自本地基础目录，三方候选照常给出。
		var natives, thirdParty []string
		for _, c := range eval.Candidates {
			if c.Native {
				natives = append(natives, c.Slug)
			} else {
				thirdParty = append(thirdParty, c.Slug)
			}
		}
		require.ElementsMatch(t, []string{"codex-auto-review", "gpt-6-astra", "gpt-6.1-sol"}, natives, name)
		require.Contains(t, thirdParty, "deepseek-flash", name)
	}
}

func TestCodexCompositeBudgetWithSchedulableOfficialKeepsDeferredRule(t *testing.T) {
	svc, _, fetcher := cxtpGroup8Service(t, nil) // #90 可调度
	group := cxtpGroup8()
	fetcher.bodies[90] = cxtpBase(cxtpGPTEntry("gpt-6.1-sol", 10))
	cfg := cxtpConfig(1, "deepseek-flash")[8]

	offline, err := svc.EvaluateCodexThirdPartyCatalog(WithCodexThirdPartyOfflineEvaluation(context.Background()), group, cfg)
	require.NoError(t, err)
	require.True(t, offline.Budget.Deferred)
	require.False(t, offline.Budget.Complete)
	require.Empty(t, fetcher.fetchedIDs())

	preview, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cfg)
	require.NoError(t, err)
	require.True(t, preview.Budget.Complete)
	require.Equal(t, []int64{90}, preview.Budget.BaseAccountIDs)
	require.Equal(t, []int64{90}, fetcher.fetchedIDs())

	// 官方账号全部失败：未完成并拦开启，不退回本地基础目录。
	fetcher.errs = map[int64]error{90: errors.New("down")}
	fetcher.bodies = map[int64][]byte{}
	svc2, _, fetcher2 := cxtpGroup8Service(t, nil)
	fetcher2.errs = map[int64]error{90: errors.New("down")}
	failed, err := svc2.EvaluateCodexThirdPartyCatalog(context.Background(), group, cfg)
	require.NoError(t, err)
	require.False(t, failed.Budget.Complete)
	require.False(t, failed.Budget.Deferred)
}

func TestCodexCompositeLocalBaseWithoutAllowlistListsMappedNamesAndHidesCodexAuto(t *testing.T) {
	svc, _, _ := cxtpGroup8Service(t, []string{"deepseek-flash", "Kimi-K3"}, 90)
	group := cxtpCompositeGroup() // 没有白名单
	manifest, _, err := svc.BuildCompositeBaseManifest(context.Background(), context.Background(), group, nil)
	require.NoError(t, err)
	// 未显式列入白名单的 codex-auto-* 不进目录；其余映射名都在本地基础目录里。
	require.ElementsMatch(t, []string{"GLM-5.3", "Kimi-K3", "deepseek-flash", "deepseek-v4.1-flash", "gpt-6-astra", "gpt-6.1-sol"}, cxtpSlugs(t, manifest.Body))
	require.NoError(t, svc.ApplyToManifest(context.Background(), group, manifest))
	// 策略之后：GPT 条目保留，已开启的 deepseek-flash、Kimi-K3 换成档案条目，未开启的 GLM-5.3、deepseek-v4.1-flash 移除。
	require.ElementsMatch(t, []string{"Kimi-K3", "deepseek-flash", "gpt-6-astra", "gpt-6.1-sol"}, cxtpSlugs(t, manifest.Body))
	for _, slug := range []string{"Kimi-K3", "deepseek-flash"} {
		fields := cxtpEntry(t, manifest.Body, slug)
		require.JSONEq(t, `"list"`, string(fields["visibility"]), slug)
		require.Contains(t, string(fields["model_messages"]), "instructions_template", slug)
	}
}
