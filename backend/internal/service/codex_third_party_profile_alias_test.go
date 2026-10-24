//go:build unit

package service

// 型号别名（“关联到已有档案”）的单测：存取与校验、匹配优先级、失效别名、关联后条目恢复发布。

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/stretchr/testify/require"
)

const aliasTestUnknownModel = "acme-ds:prod"

// newAliasHarness 把真实的目录服务接成管理服务的评估器，二者共用同一份 settings：
// Composite 分组里只有一个 custom 账号，把公开名 deepseek-flash 映射到内置档案认不出的 acme-ds:prod。
func newAliasHarness(t *testing.T) (*codexAdminHarness, *CodexThirdPartyCatalogService) {
	t.Helper()
	h := newCodexAdminHarness(t)
	custom := compositePoolWithMapping(compositePoolCustom(12, 3, customFullURLs()), map[string]any{compositePoolModel: aliasTestUnknownModel})
	custom.Name = "Ollama"
	svc := cxtpPoolService(t, []Account{custom})
	svc.settings = h.settings
	svc.now = h.now
	h.svc.evaluator = svc
	return h, svc
}

func aliasCandidate(t *testing.T, svc *CodexThirdPartyCatalogService, enabled bool) CodexThirdPartyCandidate {
	t.Helper()
	cfg := NewCodexModelVisibilityConfig()
	if enabled {
		cfg.Models[compositePoolModel] = CodexModelVisibilityEntry{Enabled: true}
	}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(WithCodexThirdPartyOfflineEvaluation(t.Context()), cxtpCompositeGroup(), cfg)
	require.NoError(t, err)
	candidate, ok := findCodexCandidate(eval.Candidates, compositePoolModel)
	require.True(t, ok)
	return candidate
}

func TestCodexProfileAliasStoreRoundTripAndKeyShape(t *testing.T) {
	h, _ := newAliasHarness(t)

	list, err := h.svc.ListProfileAliases(t.Context())
	require.NoError(t, err)
	require.Empty(t, list.Aliases)
	require.NotEmpty(t, list.Profiles, "返回各内置档案，供页面下拉列出")
	var deepseek *CodexThirdPartyProfileSummary
	for i := range list.Profiles {
		if list.Profiles[i].ID == "deepseek-flash" {
			deepseek = &list.Profiles[i]
		}
	}
	require.NotNil(t, deepseek)
	require.Contains(t, deepseek.UpstreamModels, "deepseek-v4.1-flash", "档案已认得的型号")

	list, err = h.svc.SetProfileAlias(t.Context(), "  "+aliasTestUnknownModel+"  ", "deepseek-flash", "user:7")
	require.NoError(t, err)
	require.Len(t, list.Aliases, 1)
	require.Equal(t, int64(1), list.Revision)
	alias := list.Aliases[0]
	require.Equal(t, aliasTestUnknownModel, alias.UpstreamModel)
	require.Equal(t, "acme-ds", alias.Key, "键是规范化后的上游型号")
	require.Equal(t, "deepseek-flash", alias.ProfileID)
	require.True(t, alias.Valid)
	require.Equal(t, "user:7", alias.UpdatedBy)
	require.True(t, alias.UpdatedAt.Equal(h.now().UTC()))

	// settings 表里的 JSON 键结构。
	raw := h.repo.raw(CodexProfileAliasSettingKey)
	require.Equal(t, "codex_third_party_profile_aliases", CodexProfileAliasSettingKey)
	var stored map[string]any
	require.NoError(t, json.Unmarshal([]byte(raw), &stored))
	require.EqualValues(t, 1, stored["schema_version"])
	require.EqualValues(t, 1, stored["revision"])
	entries, ok := stored["aliases"].(map[string]any)
	require.True(t, ok)
	entry, ok := entries["acme-ds"].(map[string]any)
	require.True(t, ok)
	require.Equal(t, aliasTestUnknownModel, entry["upstream_model"])
	require.Equal(t, "deepseek-flash", entry["profile_id"])
	require.Equal(t, "user:7", entry["updated_by"])
	require.NotEmpty(t, entry["updated_at"])

	// 重复提交相同内容是幂等的：仍只有一条。
	again, err := h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "deepseek-flash", "user:7")
	require.NoError(t, err)
	require.Len(t, again.Aliases, 1)

	// 另一种写法（大小写、厂商前缀、日期后缀）规范化后是同一个键，更新同一条。
	updated, err := h.svc.SetProfileAlias(t.Context(), "Vendor/ACME-DS-0925:Prod", "kimi-k3", "user:8")
	require.NoError(t, err)
	require.Len(t, updated.Aliases, 1)
	require.Equal(t, "kimi-k3", updated.Aliases[0].ProfileID)
	require.Equal(t, "user:8", updated.Aliases[0].UpdatedBy)
}

func TestCodexProfileAliasValidation(t *testing.T) {
	h, _ := newAliasHarness(t)
	reasonOf := func(err error) string {
		var appErr *infraerrors.ApplicationError
		require.ErrorAs(t, err, &appErr)
		return appErr.Reason
	}

	_, err := h.svc.SetProfileAlias(t.Context(), "   ", "deepseek-flash", "u")
	require.Equal(t, "CODEX_PROFILE_ALIAS_INVALID", reasonOf(err), "上游型号不能为空")
	_, err = h.svc.SetProfileAlias(t.Context(), "vendor/", "deepseek-flash", "u")
	require.Equal(t, "CODEX_PROFILE_ALIAS_INVALID", reasonOf(err), "去掉厂商前缀后为空")
	_, err = h.svc.SetProfileAlias(t.Context(), "bad*model", "deepseek-flash", "u")
	require.Equal(t, "CODEX_PROFILE_ALIAS_INVALID", reasonOf(err))
	_, err = h.svc.SetProfileAlias(t.Context(), strings.Repeat("a", 201), "deepseek-flash", "u")
	require.Equal(t, "CODEX_PROFILE_ALIAS_INVALID", reasonOf(err))
	_, err = h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "", "u")
	require.Equal(t, "CODEX_PROFILE_ALIAS_INVALID", reasonOf(err), "档案 ID 不能为空")

	_, err = h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "no-such-profile", "u")
	require.Equal(t, "CODEX_PROFILE_NOT_FOUND", reasonOf(err), "只允许关联到当前存在的档案")

	// 已被内置档案直接认出的型号（含规范化后认出的写法）不允许再关联到另一个档案。
	for _, model := range []string{"deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash:1017", "DeepSeek_V4.1_Flash-20250925"} {
		_, err = h.svc.SetProfileAlias(t.Context(), model, "kimi-k3", "u")
		require.Equal(t, "CODEX_PROFILE_ALIAS_BUILTIN", reasonOf(err), model)
		var appErr *infraerrors.ApplicationError
		require.ErrorAs(t, err, &appErr)
		require.Equal(t, "deepseek-flash", appErr.Metadata["profile_id"], "错误里明确指出内置档案")
	}
	require.False(t, h.repo.has(CodexProfileAliasSettingKey), "校验失败不写库")
}

func TestCodexProfileAliasDeleteIsIdempotent(t *testing.T) {
	h, svc := newAliasHarness(t)
	_, err := h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "deepseek-flash", "u")
	require.NoError(t, err)
	require.Equal(t, "deepseek-flash", aliasCandidate(t, svc, false).ProfileID)

	list, err := h.svc.DeleteProfileAlias(t.Context(), "ACME-DS-0925")
	require.NoError(t, err, "规范化后同一个键")
	require.Empty(t, list.Aliases)
	require.Equal(t, int64(2), list.Revision)
	require.Empty(t, aliasCandidate(t, svc, false).ProfileID, "解除关联后立即不再匹配")

	again, err := h.svc.DeleteProfileAlias(t.Context(), aliasTestUnknownModel)
	require.NoError(t, err, "本来没有关联也成功")
	require.Equal(t, int64(2), again.Revision, "没有变化不写库")
	_, err = h.svc.DeleteProfileAlias(t.Context(), "  ")
	require.Error(t, err)
}

// DMIT 形态：custom 账号映射到内置档案认不出的型号，整组「暂无能力档案（待补能力）」；
// 已开启的模型显示「已开启，但当前未发布」。关联到已有档案后立即变为可配置并恢复发布，无需重新开关。
func TestCodexProfileAliasLinkRestoresConfigurableAndPublished(t *testing.T) {
	h, svc := newAliasHarness(t)

	before := aliasCandidate(t, svc, true)
	require.Equal(t, CodexModelStatusNeedsCapability, before.Status)
	require.False(t, before.CanEnable)
	require.True(t, before.Enabled)
	require.False(t, before.Published, "已开启但未发布")
	require.Equal(t, aliasTestUnknownModel, before.ProfileMissingModel, "页面据此提供“关联到已有档案”")
	require.Contains(t, before.Reasons, "上游型号 "+aliasTestUnknownModel+" 暂无能力档案（待补能力）")

	_, err := h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "deepseek-flash", "user:1")
	require.NoError(t, err)

	after := aliasCandidate(t, svc, true)
	require.Equal(t, CodexModelStatusConfigurable, after.Status, "原因：%v", after.Reasons)
	require.True(t, after.CanEnable)
	require.True(t, after.Published, "按新判定自动恢复发布")
	require.Equal(t, "deepseek-flash", after.ProfileID)
	require.Empty(t, after.ProfileMissingModel)
	require.NotEqual(t, before.Fingerprint, after.Fingerprint)
}

func TestCodexProfileAliasInvalidProfileDoesNotParticipate(t *testing.T) {
	h, svc := newAliasHarness(t)
	// 档案被删或改名后留下的别名：列表里标出失效，不参与匹配。
	cfg := NewCodexProfileAliasConfig()
	cfg.Revision = 3
	cfg.Aliases["acme-ds"] = CodexProfileAliasEntry{UpstreamModel: aliasTestUnknownModel, ProfileID: "renamed-profile", UpdatedBy: "user:1", UpdatedAt: h.now()}
	require.NoError(t, h.settings.SaveCodexProfileAliases(t.Context(), cfg))
	svc.InvalidateCodexProfileAliases()

	list, err := h.svc.ListProfileAliases(t.Context())
	require.NoError(t, err)
	require.Len(t, list.Aliases, 1)
	require.False(t, list.Aliases[0].Valid)
	candidate := aliasCandidate(t, svc, false)
	require.Equal(t, CodexModelStatusNeedsCapability, candidate.Status)
	require.Empty(t, candidate.ProfileID)
}

func TestCodexProfileAliasMatchPriorityAndNormalizedKey(t *testing.T) {
	set := loadTestProfileSet(t)
	// 别名在所有内置规则之前：即使内置规则能认出，别名表里的关联也优先。
	index := codexProfileAliasIndex{codexProfileAliasKey("deepseek-v4.1-flash"): "kimi-k3"}
	require.Equal(t, "kimi-k3", set.matchWithAliases("deepseek-v4.1-flash", index).ID)
	require.Equal(t, "deepseek-flash", set.matchWithAliases("deepseek-v4.1-flash", nil).ID)

	// 按同一套规范化后的名字精确比较。
	index = codexProfileAliasIndex{codexProfileAliasKey("acme_ds-0925:cloud"): "deepseek-flash"}
	for _, model := range []string{"acme-ds", "ACME_DS", "vendor/acme-ds-20250925", "acme-ds@2025-09-25", "acme-ds:latest"} {
		profile := set.matchWithAliases(model, index)
		require.NotNil(t, profile, model)
		require.Equal(t, "deepseek-flash", profile.ID, model)
	}
	require.Nil(t, set.matchWithAliases("acme-ds-8b", index), "参数规模不剥离")
	require.Nil(t, set.matchWithAliases("acme-ds-preview", index))

	// 指向不存在档案的索引项不命中，回落内置规则。
	index = codexProfileAliasIndex{codexProfileAliasKey("deepseek-v4.1-flash"): "gone"}
	require.Equal(t, "deepseek-flash", set.matchWithAliases("deepseek-v4.1-flash", index).ID)

	// matchAll 里任一型号靠别名命中同一档案即可。
	profile, reason, unmatched := set.matchAllWithAliases([]string{"deepseek-flash", "acme-ds:prod"},
		codexProfileAliasIndex{codexProfileAliasKey("acme-ds:prod"): "deepseek-flash"})
	require.NotNil(t, profile, reason)
	require.Empty(t, unmatched)
	profile, _, unmatched = set.matchAllWithAliases([]string{"deepseek-flash", "acme-ds:prod"}, nil)
	require.Nil(t, profile)
	require.Equal(t, "acme-ds:prod", unmatched)
}

func TestCodexProfileAliasIndexCacheAndInvalidation(t *testing.T) {
	h, svc := newAliasHarness(t)
	// 直接写库（模拟别的节点写入）：缓存有效期内评估侧看不到，失效后读到。
	require.Empty(t, aliasCandidate(t, svc, false).ProfileID)
	cfg := NewCodexProfileAliasConfig()
	cfg.Aliases["acme-ds"] = CodexProfileAliasEntry{UpstreamModel: aliasTestUnknownModel, ProfileID: "deepseek-flash", UpdatedAt: h.now()}
	require.NoError(t, h.settings.SaveCodexProfileAliases(t.Context(), cfg))
	require.Empty(t, aliasCandidate(t, svc, false).ProfileID, "缓存期内仍用旧索引，避免每次目录请求读库")

	h.advance(codexProfileAliasCacheTTL + time.Second)
	require.Equal(t, "deepseek-flash", aliasCandidate(t, svc, false).ProfileID, "缓存过期后读到新别名")
}

func TestCodexProfileAliasRequiresProfileDirectory(t *testing.T) {
	h := newCodexAdminHarness(t) // 评估器是不带档案目录能力的假实现
	_, err := h.svc.ListProfileAliases(t.Context())
	require.ErrorIs(t, err, ErrCodexCatalogEvaluationFailed)
	_, err = h.svc.SetProfileAlias(t.Context(), "x", "deepseek-flash", "u")
	require.ErrorIs(t, err, ErrCodexCatalogEvaluationFailed)
}

// DMIT 形态整组：DeepSeek、CommandCode（目录声明支持 Responses）、custom 账号（映射到内置档案认不出的型号）
// 同时承接一个公开名。关联前整组「暂无能力档案」；关联到 deepseek-flash 后整组变为可配置。
func TestCodexProfileAliasDMITShapeBecomesConfigurableAfterLink(t *testing.T) {
	h := newCodexAdminHarness(t)
	baseURL := "http://cc-alias-dmit.example/provider/v1"
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	cxtpPoolWarmCatalog(t, &commandCode, baseURL, []string{APIProtocolChatCompletions, APIProtocolResponses})
	custom := compositePoolWithMapping(compositePoolCustom(12, 3, customFullURLs()), map[string]any{compositePoolModel: aliasTestUnknownModel})
	svc := cxtpPoolService(t, []Account{cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 1), "DeepSeek"), commandCode, custom})
	svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{httpUpstream: &recordingHTTPUpstream{}}}
	svc.settings = h.settings
	svc.now = h.now
	h.svc.evaluator = svc

	before := aliasCandidate(t, svc, false)
	require.Equal(t, CodexModelStatusNeedsCapability, before.Status)
	require.Equal(t, []int64{10, 11, 12}, before.AccountIDs)
	require.Equal(t, aliasTestUnknownModel, before.ProfileMissingModel)

	_, err := h.svc.SetProfileAlias(t.Context(), aliasTestUnknownModel, "deepseek-flash", "user:1")
	require.NoError(t, err)

	after := aliasCandidate(t, svc, false)
	require.Equal(t, CodexModelStatusConfigurable, after.Status, "原因：%v", after.Reasons)
	require.True(t, after.CanEnable)
	require.Equal(t, "deepseek-flash", after.ProfileID)
}
