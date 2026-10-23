package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestMappedModelsDiscoveryKeepsAliasesAfterGroupAllowlist(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const (
		groupID int64 = 7981
		alias         = "sonnet"
		target        = "claude-sonnet-4-6"
	)
	for _, accountCase := range []struct {
		name         string
		extraAccount *service.Account
		want         []string
	}{
		{
			name: "同账号原名被隐藏",
			want: []string{alias},
		},
		{
			name: "其他账号独立自映射保留原名",
			extraAccount: &service.Account{
				ID:       2,
				Platform: service.PlatformAnthropic,
				Type:     service.AccountTypeAPIKey,
				Credentials: map[string]any{
					"model_mapping": map[string]any{target: target},
				},
			},
			want: []string{alias, target},
		},
		{
			name: "其他无映射订阅账号保留原名",
			extraAccount: &service.Account{
				ID:       2,
				Platform: service.PlatformAnthropic,
				Type:     service.AccountTypeOAuth,
			},
			want: []string{alias, target},
		},
	} {
		for _, allowlistCase := range []struct {
			name   string
			models []string
		}{
			{name: "精确白名单同时选择别名和原名", models: []string{alias, target}},
			{name: "通配符白名单匹配原名", models: []string{alias, "claude-sonnet-4-6*"}},
		} {
			for _, endpointCase := range []struct {
				name  string
				codex bool
			}{
				{name: "普通模型发现"},
				{name: "客户端模型发现", codex: true},
			} {
				t.Run(accountCase.name+"/"+allowlistCase.name+"/"+endpointCase.name, func(t *testing.T) {
					account := service.Account{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeAPIKey,
						Credentials: map[string]any{
							"model_mapping": map[string]any{alias: target, target: target},
						},
					}
					accounts := []service.Account{account}
					if accountCase.extraAccount != nil {
						accounts = append(accounts, *accountCase.extraAccount)
					}
					handler := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
						byGroup: map[int64][]service.Account{groupID: accounts},
					})
					group := &service.Group{
						ID:       groupID,
						Platform: service.PlatformAnthropic,
						ModelAllowlist: service.GroupModelAllowlist{
							Enabled: true,
							Models:  allowlistCase.models,
						},
					}
					recorder := httptest.NewRecorder()
					ctx, _ := gin.CreateTestContext(recorder)
					path := "/v1/models"
					if endpointCase.codex {
						path += "?client_version=0.144.0"
					}
					ctx.Request = httptest.NewRequest(http.MethodGet, path, nil)
					ctx.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{GroupID: &group.ID, Group: group})
					if endpointCase.codex {
						handler.CodexModels(ctx)
					} else {
						handler.Models(ctx)
					}

					require.Equal(t, http.StatusOK, recorder.Code, recorder.Body.String())
					var models []string
					if endpointCase.codex {
						models = codexHandlerManifestSlugs(t, recorder)
					} else {
						var body gatewayModelsResponseForTest
						require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &body))
						models = modelIDsForTest(body.Data)
					}
					require.ElementsMatch(t, accountCase.want, models, "默认目录与分组白名单不得重新补入已隐藏的原名")
					require.Equal(t, map[string]string{alias: target, target: target}, account.GetModelMapping(), "模型发现不得修改供实际请求使用的账号映射")
					for _, model := range []string{alias, target} {
						require.True(t, account.IsModelSupported(model), "别名和原名仍须可用于实际请求")
						mapped, matched := account.ResolveMappedModel(model)
						require.True(t, matched)
						require.Equal(t, target, mapped)
					}
				})
			}
		}
	}
}

func TestMappedModelsDiscoveryCompositeETagTracksPublicCatalog(t *testing.T) {
	t.Run("组合分组远程发现隐藏原名并按最终目录校验缓存", func(t *testing.T) {
		const (
			alias  = "deepseek-flash"
			target = "cline-pass/deepseek-v4.1-flash"
		)
		mapping := map[string]any{target: target}
		account := newPinnedCodexAccount(1, service.StatusActive, true, false)
		account.Credentials["model_mapping"] = mapping
		upstream := &codexModelsPinnedHTTPUpstream{bodies: map[int64]string{
			1: `{"models":[{"slug":"cline-pass/deepseek-v4.1-flash","context_window":424242}]}`,
		}}
		handler := newPinnedCodexTestHandler([]service.Account{account}, upstream, 3)
		group := &service.Group{
			ID:       7982,
			Platform: service.PlatformComposite,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{alias, target},
			},
		}

		first := performCodexModelsRequestForGroup(t, handler, group, "")
		require.Equal(t, http.StatusOK, first.Code, first.Body.String())
		require.Equal(t, []string{target}, codexHandlerManifestSlugs(t, first))
		oldETag := first.Header().Get("ETag")
		require.NotEmpty(t, oldETag)

		// 添加别名后仍使用同一份上游缓存，客户端必须立即看到新的公开目录。
		mapping[alias] = target
		second := performCodexModelsRequestForGroup(t, handler, group, oldETag)
		require.Equal(t, http.StatusOK, second.Code, second.Body.String())
		require.Equal(t, []string{alias}, codexHandlerManifestSlugs(t, second))
		require.Contains(t, second.Body.String(), `"context_window":424242`, "别名应保留上游模型元数据")
		newETag := second.Header().Get("ETag")
		require.NotEmpty(t, newETag)
		require.NotEqual(t, oldETag, newETag)
		require.Equal(t, service.CodexModelsManifestETag(second.Body.Bytes()), newETag)

		third := performCodexModelsRequestForGroup(t, handler, group, newETag)
		require.Equal(t, http.StatusNotModified, third.Code, third.Body.String())
		require.Empty(t, third.Body.String())
		require.Equal(t, newETag, third.Header().Get("ETag"))
		require.Equal(t, []int64{account.ID}, upstream.accountIDs(), "目录展示变化应复用原始上游缓存")
		require.Equal(t, map[string]string{alias: target, target: target}, account.GetModelMapping())
		for _, model := range []string{alias, target} {
			require.True(t, account.IsModelSupported(model), "目录隐藏原名不得改变模型准入")
			mapped, matched := account.ResolveMappedModel(model)
			require.True(t, matched)
			require.Equal(t, target, mapped)
		}
	})
}
