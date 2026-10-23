package handler

// 分组默认模型名单（Codex 三方模型候选的唯一来源）必须与该分组 Key 调用普通模型列表接口的结果一致。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// cxtpProdShapeGroupAccounts 是 ARM64 实测的账号形态：官方 OAuth（空映射）加显式映射的中转账号。
func cxtpProdShapeGroupAccounts() []service.Account {
	apiKey := func(id int64, mapping map[string]any) service.Account {
		return service.Account{
			ID: id, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
			Credentials: map[string]any{"api_key": "sk-test-secret", "base_url": "https://relay.example/v1", "model_mapping": mapping},
		}
	}
	return []service.Account{
		{ID: 90, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Credentials: map[string]any{}},
		apiKey(105, map[string]any{"deepseek-flash": "deepseek-flash"}),
		// 别名映射：只显示别名，不显示带厂商前缀的上游原名（即使原名也有自映射）。
		apiKey(109, map[string]any{
			"Kimi-K3": "moonshotai/Kimi-K3", "moonshotai/Kimi-K3": "moonshotai/Kimi-K3",
			"GLM-5.3": "zai-org/GLM-5.3", "deepseek-flash": "deepseek/deepseek-v4.1-flash",
		}),
		apiKey(110, map[string]any{"deepseek-flash": "cline-pass/deepseek-v4.1-flash"}),
		// 没有被映射的名字显示原名。
		apiKey(111, map[string]any{"vendor/Plain-Model": "vendor/Plain-Model"}),
	}
}

func cxtpPlainModelIDs(t *testing.T, h *GatewayHandler, group *service.Group) []string {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{GroupID: &group.ID, Group: group})
	h.Models(c)
	require.Equal(t, http.StatusOK, rec.Code)
	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	return modelIDsForTest(got.Data)
}

func TestCodexThirdPartyGroupModelListEqualsPlainModelsEndpoint(t *testing.T) {
	const groupID int64 = 8
	repo := &gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{groupID: cxtpProdShapeGroupAccounts()}}
	h := newGatewayModelsHandlerForTest(repo)

	for _, tc := range []struct {
		name      string
		platform  string
		allowlist []string
	}{
		{"composite", service.PlatformComposite, nil},
		{"composite with allowlist", service.PlatformComposite, []string{"codex-auto-review", "Kimi-K3", "GLM-5.3", "deepseek-flash", "not-served"}},
		{"openai", service.PlatformOpenAI, nil},
		{"openai with allowlist", service.PlatformOpenAI, []string{"Kimi-K3", "deepseek-flash", "codex-auto-review"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			group := &service.Group{ID: groupID, Platform: tc.platform}
			if tc.allowlist != nil {
				group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: tc.allowlist}
			}
			want := cxtpPlainModelIDs(t, h, group)
			got, err := h.codexThirdPartyGroupModelIDs(context.Background(), group)
			require.NoError(t, err)
			require.Equal(t, want, got, "候选名单必须与普通模型列表逐项一致")
			require.NotEmpty(t, got)
		})
	}
}

func TestCodexThirdPartyGroupModelListShowsAliasesAndHidesMappedTargets(t *testing.T) {
	const groupID int64 = 8
	repo := &gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{groupID: cxtpProdShapeGroupAccounts()}}
	h := newGatewayModelsHandlerForTest(repo)
	group := &service.Group{ID: groupID, Platform: service.PlatformComposite}

	got, err := h.codexThirdPartyGroupModelIDs(context.Background(), group)
	require.NoError(t, err)
	require.Subset(t, got, []string{"Kimi-K3", "GLM-5.3", "deepseek-flash", "vendor/Plain-Model"})
	// 被别名映射掉的上游原名不显示（即使原名有自映射），也没有任何中转站点同步来的海量名字。
	require.NotContains(t, got, "zai-org/GLM-5.3")
	require.NotContains(t, got, "deepseek/deepseek-v4.1-flash")
	require.NotContains(t, got, "cline-pass/deepseek-v4.1-flash")
	require.NotContains(t, got, "moonshotai/Kimi-K3")
}

func TestCodexThirdPartyGroupModelListWithAllowlistKeepsCodexAutoReview(t *testing.T) {
	// 生产形态：分组白名单含 codex-auto-review，普通模型列表列出它，名单必须同样列出。
	const groupID int64 = 8
	repo := &gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{groupID: cxtpProdShapeGroupAccounts()}}
	h := newGatewayModelsHandlerForTest(repo)
	group := &service.Group{ID: groupID, Platform: service.PlatformComposite,
		ModelAllowlist: service.GroupModelAllowlist{Enabled: true, Models: []string{"codex-auto-review", "Kimi-K3", "not-served"}}}
	got, err := h.codexThirdPartyGroupModelIDs(context.Background(), group)
	require.NoError(t, err)
	require.Contains(t, got, "codex-auto-review")
	require.Contains(t, got, "Kimi-K3")
	require.NotContains(t, got, "not-served")
	require.NotContains(t, got, "GLM-5.3", "白名单之外的模型不在名单里")
}

func TestCodexThirdPartyGroupModelListNonOpenAIPlatformUsesCodexList(t *testing.T) {
	// DeepSeek 等兼容平台分组：沿用 Codex 目录自己的名单，不回落到面向 Claude 的静态清单。
	const groupID int64 = 9
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{
		groupID: {{ID: 1, Platform: service.PlatformDeepseek, Type: service.AccountTypeAPIKey, Credentials: map[string]any{
			"api_key": "sk-test-secret", "base_url": "https://ds.example", "api_protocol": "responses",
		}}},
	}})
	got, err := h.codexThirdPartyGroupModelIDs(context.Background(), &service.Group{ID: groupID, Platform: service.PlatformDeepseek})
	require.NoError(t, err)
	require.Equal(t, []string{"deepseek-v4-pro", "deepseek-v4-flash", "deepseek-flash"}, got)
}

func TestCodexThirdPartyGroupModelListReportsFailure(t *testing.T) {
	_, err := (&GatewayHandler{}).codexThirdPartyGroupModelIDs(context.Background(), &service.Group{ID: 1, Platform: service.PlatformComposite})
	require.Error(t, err)
	_, err = newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{}).codexThirdPartyGroupModelIDs(context.Background(), nil)
	require.Error(t, err)
}

func TestCodexThirdPartyGatewayHandlerInjectsGroupModelLister(t *testing.T) {
	// 装配时由通用网关 handler 把名单函数交给目录服务：目录服务不再自行对账号和快照求并集。
	const groupID int64 = 8
	accounts := cxtpProdShapeGroupAccounts()
	repo := &gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{groupID: accounts}}
	h := newGatewayModelsHandlerForTest(repo)
	settings := service.NewSettingService(cxtpSettingRepo{values: map[string]string{}}, nil)
	svc := service.NewCodexThirdPartyCatalogService(settings, cxtpAccountRepo{accounts: accounts}, nil, nil, nil)
	group := &service.Group{ID: groupID, Platform: service.PlatformComposite}

	// 未注入名单：评估失败，不会悄悄退回旧的求并集做法。
	_, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, service.NewCodexModelVisibilityConfig())
	require.ErrorIs(t, err, service.ErrCodexThirdPartyGroupModelsUnavailable)

	h.SetCodexThirdPartyCatalog(svc)
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, service.NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	var slugs []string
	for _, c := range eval.Candidates {
		slugs = append(slugs, c.Slug)
	}
	require.Subset(t, slugs, []string{"Kimi-K3", "GLM-5.3", "deepseek-flash"})
	require.NotContains(t, slugs, "moonshotai/Kimi-K3")
}
