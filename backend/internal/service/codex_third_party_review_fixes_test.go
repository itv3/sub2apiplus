package service

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// 验收修复用例：D1 非 OpenAI 平台空账号分组不得走原样返回捷径、
// D4 评估基础与运行时基础一致（含分组合并）、D7 官方账号按上游目录快照保留名称范围外的原生模型。

// ---------------------------------------------------------------------------
// D1
// ---------------------------------------------------------------------------

func TestCodexThirdPartyNonOpenAIGroupWithoutAccountsStillFiltersFallbackCatalog(t *testing.T) {
	// 通用目录 handler 在没有持久启用账号时用平台回落清单生成目录；默认关闭必须仍然生效。
	for _, platform := range []string{PlatformDeepseek, PlatformMiniMax, PlatformKimi, PlatformZhipu} {
		t.Run(platform, func(t *testing.T) {
			fallback, err := BuildCodexModelsManifest([]string{"deepseek-flash", "deepseek-v4-pro", "kimi-k3", "glm-5.3"})
			require.NoError(t, err)
			svc := newCXTPService(t, &cxtpSettings{}, nil, nil)
			got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: platform}, fallback)
			require.NoError(t, err)
			require.Empty(t, cxtpSlugs(t, got), "未配置时输出不得含任何第三方条目")

			// 开启也不能发布：没有承接账号。
			svc = newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "deepseek-flash")}, nil, nil)
			got, err = svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: platform}, fallback)
			require.NoError(t, err)
			require.Empty(t, cxtpSlugs(t, got))
		})
	}
}

func TestCodexThirdPartyOpenAIGroupWithoutAccountsKeepsBaseBytes(t *testing.T) {
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("kimi-k3", 0))
	svc := newCXTPService(t, &cxtpSettings{}, nil, nil)
	for _, platform := range []string{PlatformOpenAI, PlatformComposite} {
		got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: platform}, base)
		require.NoError(t, err)
		require.True(t, bytes.Equal(base, got), platform)
	}
}

// ---------------------------------------------------------------------------
// D4
// ---------------------------------------------------------------------------

// cxtpAppendEntry 模拟运行时分组合并追加了一个账号别名条目。
func cxtpAppendEntry(entry string) func([]byte) []byte {
	return func(body []byte) []byte {
		text := string(body)
		idx := strings.LastIndex(text, "]")
		sep := ","
		if strings.HasSuffix(strings.TrimSpace(text[:idx]), "[") {
			sep = ""
		}
		return []byte(text[:idx] + sep + entry + text[idx:])
	}
}

func TestCodexThirdPartyEvaluationBaseMatchesRuntimeBaseAfterGroupMerge(t *testing.T) {
	extra := cxtpGPTEntry("gpt-5.4", 5000)
	oauth := cxtpOAuthAccount(1)
	kimi := cxtpOpenAIKeyAccount(4, "https://site.example", map[string]any{"kimi-k3": "kimi-k3"})
	fetcher := &cxtpFetcher{
		bodies: map[int64][]byte{
			1: cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0)),
			4: cxtpBase(cxtpGPTEntry("kimi-k3", 0)),
		},
		merge: cxtpAppendEntry(extra),
	}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, []Account{oauth, kimi}, fetcher)
	group := &Group{ID: 8, Platform: PlatformOpenAI}

	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "kimi-k3")[8])
	require.NoError(t, err)

	// 运行时对账号 1 的基础：映射之后合并，再应用策略。评估的最坏体积必须不小于该结果，
	// 并等于各基础运行时结果的最大值。
	runtimeSize := 0
	for id, body := range fetcher.bodies {
		merged, _ := fetcher.MergeGroupModels(context.Background(), group, body)
		out, err := svc.ApplyToBody(context.Background(), group, merged)
		require.NoError(t, err, id)
		if len(out) > runtimeSize {
			runtimeSize = len(out)
		}
	}
	require.Equal(t, runtimeSize, eval.Budget.WorstCaseBytes)

	// 合并追加的原生条目出现在原生只读行里，原生数量按合并后计算。
	var natives []string
	for _, c := range eval.Candidates {
		if c.Native {
			natives = append(natives, c.Slug)
		}
	}
	require.Contains(t, natives, "gpt-5.4")
	require.Equal(t, 2, eval.Budget.NativeCount)
}

func TestCodexThirdPartyEvaluationPinnedGroupFoldsAccountsLikeRuntime(t *testing.T) {
	a, b := cxtpOAuthAccount(1), cxtpOAuthAccount(2)
	third := cxtpOpenAIKeyAccount(4, "https://site.example", map[string]any{"kimi-k3": "kimi-k3"})
	fetcher := &cxtpFetcher{
		bodies: map[int64][]byte{
			1: cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0)),
			2: cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("gpt-5.4", 3000)),
		},
		merge: cxtpAppendEntry(cxtpGPTEntry("gpt-5.4-mini", 2000)),
	}
	svc := newCXTPService(t, &cxtpSettings{}, []Account{a, b, third}, fetcher)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	group.CodexModelsManifestConfig.Enabled = true
	group.CodexModelsManifestConfig.AccountIDs = []int64{2, 1}

	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.True(t, eval.Budget.Complete)
	require.Equal(t, []int64{1, 2}, eval.Budget.BaseAccountIDs)

	// 运行时：固定账号目录按配置顺序按 slug 合并，再整体执行一次分组合并，然后应用策略。
	combined, err := mergeCodexModelsManifestBodies([][]byte{fetcher.bodies[2], fetcher.bodies[1]})
	require.NoError(t, err)
	merged, _ := fetcher.MergeGroupModels(context.Background(), group, combined)
	out, err := svc.ApplyToBody(context.Background(), group, merged)
	require.NoError(t, err)
	require.Equal(t, len(out), eval.Budget.WorstCaseBytes)
	var natives []string
	for _, c := range eval.Candidates {
		if c.Native {
			natives = append(natives, c.Slug)
		}
	}
	require.ElementsMatch(t, []string{"gpt-5.6-sol", "gpt-5.4", "gpt-5.4-mini"}, natives)
}

// ---------------------------------------------------------------------------
// D7
// ---------------------------------------------------------------------------

func TestCodexThirdPartyMixedGroupKeepsNativeModelListedByUpstream(t *testing.T) {
	oauth := cxtpOAuthAccount(1)
	kimi := cxtpOpenAIKeyAccount(4, "https://site.example", map[string]any{"kimi-k3": "kimi-k3"})
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("o5-preview", 0), cxtpGPTEntry("chatgpt-next", 0), cxtpGPTEntry("kimi-k3", 0))

	// 快照缺失：名称前缀规则兜底，名称范围外的型号与现在一样被隐藏。
	svc := newCXTPService(t, &cxtpSettings{}, []Account{oauth, kimi}, nil)
	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, got))

	// 快照含这两个型号：保留，kimi-k3 仍按第三方关闭而移除。
	fetcher := &cxtpFetcher{listed: map[int64]map[string]bool{1: {"o5-preview": true, "chatgpt-next": true}}}
	svc = newCXTPService(t, &cxtpSettings{}, []Account{oauth, kimi}, fetcher)
	got, err = svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol", "o5-preview", "chatgpt-next"}, cxtpSlugs(t, got))
}

func TestCodexThirdPartyUpstreamListedSnapshotSemantics(t *testing.T) {
	svc := &OpenAIGatewayService{}
	fetcher := openAICodexBaseFetcher{svc: svc}
	account := &Account{ID: 7, Platform: PlatformOpenAI, Type: AccountTypeOAuth}

	require.False(t, fetcher.UpstreamListsModel(account, "o5-preview"), "快照缺失")

	svc.openaiModelCapabilities.replaceFromManifest(7, []byte(`{"models":[{"slug":"O5-Preview","visibility":"list"},{"slug":"gpt-5.6-sol","visibility":"list"}]}`))
	require.True(t, fetcher.UpstreamListsModel(account, "o5-preview"), "不区分大小写")
	require.False(t, fetcher.UpstreamListsModel(account, "chatgpt-next"), "未列出")
	require.False(t, fetcher.UpstreamListsModel(&Account{ID: 8}, "o5-preview"), "按账号保存")

	// 过期后不再作为正向证据。
	svc.openaiModelCapabilities.mu.Lock()
	snapshot := svc.openaiModelCapabilities.accounts[7]
	snapshot.expiresAt = time.Now().Add(-time.Second)
	svc.openaiModelCapabilities.accounts[7] = snapshot
	svc.openaiModelCapabilities.mu.Unlock()
	require.False(t, fetcher.UpstreamListsModel(account, "o5-preview"))
}
