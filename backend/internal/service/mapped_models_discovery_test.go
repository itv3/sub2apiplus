package service

import (
	"context"
	"encoding/json"
	"maps"
	"net/http"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestMappedModelsDiscoveryCatalogs(t *testing.T) {
	const target = "cline-pass/deepseek-v4.1-flash"
	for _, tc := range []struct {
		name        string
		mappings    []map[string]any
		passthrough bool
		want        []string
		wantCodex   []string
	}{
		{
			name:     "单个别名隐藏同账号原名",
			mappings: []map[string]any{{target: target, "deepseek-flash": target}},
			want:     []string{"deepseek-flash"},
		},
		{
			name:     "多个别名全部保留",
			mappings: []map[string]any{{target: target, "deepseek-flash": target, "deepseek-fast": target}},
			want:     []string{"deepseek-fast", "deepseek-flash"},
		},
		{
			name:     "其他账号独立提供原名仍然展示",
			mappings: []map[string]any{{target: target, "deepseek-flash": target}, {target: target}},
			want:     []string{target, "deepseek-flash"},
		},
		{
			name:     "原名自身又是别名时继续展示",
			mappings: []map[string]any{{target: "provider-next", "deepseek-flash": target, "provider-next": "provider-next"}},
			want:     []string{target, "deepseek-flash"},
		},
		{
			name:     "仅自映射保持展示",
			mappings: []map[string]any{{target: target}},
			want:     []string{target},
		},
		{
			name:     "未配置映射沿用默认回退",
			mappings: []map[string]any{nil},
		},
		{
			name:      "通配符映射不隐藏原名",
			mappings:  []map[string]any{{target: target, "deepseek-*": target}},
			want:      []string{target, "deepseek-*"},
			wantCodex: []string{target},
		},
		{
			name:        "透传模式沿用原有列表规则",
			mappings:    []map[string]any{{target: target, "deepseek-flash": target}},
			passthrough: true,
			wantCodex:   []string{target, "deepseek-flash"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			accounts := make([]Account, 0, len(tc.mappings))
			originalMappings := make([]map[string]string, 0, len(tc.mappings))
			for i, mapping := range tc.mappings {
				account := Account{
					ID:          int64(i + 1),
					Platform:    PlatformOpenAI,
					Type:        AccountTypeAPIKey,
					Credentials: map[string]any{"model_mapping": mapping},
					Extra:       map[string]any{"openai_passthrough": tc.passthrough},
				}
				originalMappings = append(originalMappings, maps.Clone(account.GetModelMapping()))
				accounts = append(accounts, account)
			}
			originalCredentials, err := json.Marshal(accounts[0].Credentials)
			require.NoError(t, err)
			groupID := int64(91)
			repo := &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: accounts}}
			service := &GatewayService{accountRepo: repo}

			require.Equal(t, tc.want, service.GetAvailableModels(context.Background(), &groupID, PlatformOpenAI))
			sources := service.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolOpenAI)
			require.Contains(t, sources, PlatformOpenAI)
			require.Equal(t, tc.want, sources[PlatformOpenAI])
			wantCodex := tc.wantCodex
			if wantCodex == nil {
				wantCodex = tc.want
			}
			require.ElementsMatch(t, wantCodex, openAIConfiguredCodexModelIDs(accounts))

			for i := range accounts {
				require.Equal(t, originalMappings[i], accounts[i].GetModelMapping(), "模型发现不得修改供路由复用的映射缓存")
			}
			credentials, err := json.Marshal(accounts[0].Credentials)
			require.NoError(t, err)
			require.Equal(t, string(originalCredentials), string(credentials), "模型发现不得修改账号凭据")
		})
	}
}

func TestMappedModelsDiscoveryPreservesRouting(t *testing.T) {
	t.Run("原名直调与别名调用均保留原有映射", func(t *testing.T) {
		const target = "cline-pass/deepseek-v4.1-flash"
		account := Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
			Credentials: map[string]any{"model_mapping": map[string]any{target: target, "deepseek-flash": target}}}
		originalMapping := maps.Clone(account.GetModelMapping())
		groupID := int64(92)
		service := &GatewayService{accountRepo: &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: {account}}}}
		require.Equal(t, []string{"deepseek-flash"}, service.GetAvailableModels(context.Background(), &groupID, PlatformOpenAI))

		for _, model := range []string{target, "deepseek-flash"} {
			require.True(t, account.IsModelSupported(model), "发现页隐藏原名不得影响模型准入")
			mapped, matched := account.ResolveMappedModel(model)
			require.True(t, matched)
			require.Equal(t, target, mapped)
		}
		require.Equal(t, originalMapping, account.GetModelMapping())
	})
}

func TestMappedModelsDiscoveryAntigravityWhitelist(t *testing.T) {
	t.Run("反重力平台恢复上游映射与默认透传目录", func(t *testing.T) {
		account := Account{ID: 1, Platform: PlatformAntigravity,
			Credentials: map[string]any{"model_mapping": map[string]any{
				"custom-upstream": "custom-upstream",
				"custom-alias":    "custom-upstream",
			}}}
		groupID := int64(93)
		service := &GatewayService{accountRepo: &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: {account}}}}
		models := service.GetAvailableModels(context.Background(), &groupID, PlatformAntigravity)
		require.Contains(t, models, "custom-upstream")
		require.Contains(t, models, "custom-alias")
		require.Contains(t, models, "gemini-3.8-flash-high")
	})
}

func TestMappedModelsDiscoveryGrokDefaults(t *testing.T) {
	for _, tc := range []struct {
		name        string
		credentials map[string]any
		explicit    bool
	}{
		{name: "无凭据继续展示平台内置模型"},
		{name: "未配置映射继续展示平台内置模型", credentials: map[string]any{}},
		{name: "空映射继续展示平台内置模型", credentials: map[string]any{"model_mapping": map[string]any{}}},
		{name: "非字符串映射继续展示平台内置模型", credentials: map[string]any{"model_mapping": map[string]any{"invalid": 42}}},
		{
			name: "显式配置别名后隐藏原名",
			credentials: map[string]any{"model_mapping": map[string]any{
				"grok-4.3":  "grok-4.3",
				"grok-fast": "grok-4.3",
			}},
			explicit: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			account := Account{ID: 1, Platform: PlatformGrok, Type: AccountTypeAPIKey, Credentials: tc.credentials}
			originalMapping := maps.Clone(account.GetModelMapping())
			groupID := int64(94)
			service := &GatewayService{accountRepo: &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: {account}}}}
			models := service.GetAvailableModels(context.Background(), &groupID, PlatformGrok)
			sources := service.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolOpenAI)
			if tc.explicit {
				require.Equal(t, []string{"grok-fast"}, models)
				require.Equal(t, []string{"grok-fast"}, sources[PlatformGrok])
			} else {
				require.Contains(t, models, "grok-4.3", "内置兼容别名不得隐藏默认模型")
				require.Contains(t, sources[PlatformGrok], "grok-4.3")
				require.Len(t, models, len(originalMapping), "未配置有效映射时完整保留平台默认目录")
			}
			require.Equal(t, originalMapping, account.GetModelMapping(), "发现列表不得修改平台默认映射")
		})
	}
}

func TestMappedModelsDiscoveryCodexGroupAllowlist(t *testing.T) {
	const target = "cline-pass/deepseek-v4.1-flash"
	for _, tc := range []struct {
		name        string
		selected    []string
		independent bool
		want        []string
	}{
		{name: "白名单同时选择别名与原名时仅保留别名", selected: []string{"deepseek-flash", target}, want: []string{"deepseek-flash"}},
		{name: "白名单仅选择原名也不能将其补回", selected: []string{target}, want: []string{"deepseek-flash"}},
		{name: "其他账号独立提供原名时允许同时选择", selected: []string{"deepseek-flash", target}, independent: true, want: []string{target, "deepseek-flash"}},
		{name: "仅选择原名时保留其他账号的独立原名", selected: []string{target}, independent: true, want: []string{target, "deepseek-flash"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			account := Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
				Credentials: map[string]any{"model_mapping": map[string]any{target: target, "deepseek-flash": target}}}
			originalMapping := maps.Clone(account.GetModelMapping())
			accounts := []Account{account}
			if tc.independent {
				accounts = append(accounts, Account{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
					Credentials: map[string]any{"model_mapping": map[string]any{target: target}}})
			}
			group := &Group{Platform: PlatformOpenAI, ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: tc.selected}}
			require.Equal(t, tc.want, openAIConfiguredCodexModelIDsForGroup(accounts, group))
			require.Equal(t, originalMapping, account.GetModelMapping(), "分组白名单不得改动账号路由映射")
			require.True(t, account.IsModelSupported(target), "目录隐藏原名后仍允许原名直接调用")
		})
	}
}

func TestMappedModelsDiscoveryApplyPinnedCodexMapping(t *testing.T) {
	for _, tc := range []struct {
		name     string
		platform string
	}{
		{name: "普通分组远程目录更新别名与校验标记", platform: PlatformOpenAI},
		{name: "组合分组远程目录更新别名与校验标记", platform: PlatformComposite},
	} {
		t.Run(tc.name, func(t *testing.T) {
			const target = "cline-pass/deepseek-v4.1-flash"
			account := &Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
				Credentials: map[string]any{"model_mapping": map[string]any{target: target, "deepseek-flash": target}}}
			body := []byte(`{"models":[{"slug":"cline-pass/deepseek-v4.1-flash","source":"原始元数据","unknown":{"keep":true}}],"unknown_top":true}`)
			original := string(body)
			originalETag := CodexModelsManifestETag(body)
			response := &OpenAIModelsResponse{Body: body, ETag: originalETag}
			require.NoError(t, ApplyPinnedCodexModelsMapping(response, account, &Group{Platform: tc.platform}))
			require.JSONEq(t, `{"models":[{"slug":"deepseek-flash","display_name":"deepseek-flash","source":"原始元数据","unknown":{"keep":true}}],"unknown_top":true}`, string(response.Body))
			require.NotEqual(t, originalETag, response.ETag, "模型目录发生变化后不得沿用原始校验标记")
			require.Equal(t, codexModelsManifestBodyETag(response.Body), response.ETag)
			require.Equal(t, original, string(body), "应用别名不得修改共享的远程目录正文")
		})
	}
}

func TestMappedModelsDiscoveryRemoteProjection(t *testing.T) {
	const target = "cline-pass/deepseek-v4.1-flash"
	for _, format := range []struct {
		name    string
		field   string
		idField string
		codex   bool
	}{
		{name: "普通远程目录", field: "data", idField: "id"},
		{name: "远程编码目录", field: "models", idField: "slug", codex: true},
	} {
		t.Run(format.name, func(t *testing.T) {
			for _, tc := range []struct {
				name        string
				mapping     map[string]any
				passthrough bool
				want        map[string]string
			}{
				{
					name:    "隐藏原名并为多个别名保留元数据",
					mapping: map[string]any{target: target, "deepseek-flash": target, "deepseek-fast": target},
					want:    map[string]string{"deepseek-flash": "原始模型元数据", "deepseek-fast": "原始模型元数据"},
				},
				{
					name:    "原名自身也是别名时读取对应的一跳元数据",
					mapping: map[string]any{target: "provider-next", "deepseek-flash": target, "provider-next": "provider-next"},
					want:    map[string]string{target: "后续模型元数据", "deepseek-flash": "原始模型元数据"},
				},
				{
					name: "无映射保持原目录",
					want: map[string]string{target: "原始模型元数据", "provider-next": "后续模型元数据"},
				},
				{
					name:    "自映射保持原名",
					mapping: map[string]any{target: target},
					want:    map[string]string{target: "原始模型元数据"},
				},
				{
					name:    "通配符不删除原名也不生成虚构模型",
					mapping: map[string]any{target: target, "deepseek-*": target},
					want:    map[string]string{target: "原始模型元数据"},
				},
				{
					name:        "透传忽略残留映射并保留原目录",
					mapping:     map[string]any{target: target, "deepseek-flash": target},
					passthrough: true,
					want:        map[string]string{target: "原始模型元数据", "provider-next": "后续模型元数据"},
				},
			} {
				t.Run(tc.name, func(t *testing.T) {
					account := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
						Credentials: map[string]any{"model_mapping": tc.mapping}, Extra: map[string]any{"openai_passthrough": tc.passthrough}}
					originalMapping := maps.Clone(account.GetModelMapping())
					body, err := json.Marshal(map[string]any{
						"保留顶层字段": true,
						format.field: []map[string]any{
							{format.idField: target, "source": "原始模型元数据", "unknown": map[string]any{"keep": true}},
							{format.idField: "provider-next", "source": "后续模型元数据", "unknown": map[string]any{"keep": true}},
						},
					})
					require.NoError(t, err)
					original := string(body)
					projected, err := projectAccountModelsBody(body, account, nil, format.codex)
					require.NoError(t, err)
					require.Equal(t, original, string(body), "远程投影不得修改共享发现正文")
					require.Equal(t, originalMapping, account.GetModelMapping())
					envelope, entries, err := modelCatalogEntries(projected, format.field)
					require.NoError(t, err)
					require.JSONEq(t, "true", string(envelope["保留顶层字段"]))
					require.Len(t, entries, len(tc.want))
					for _, raw := range entries {
						var entry map[string]json.RawMessage
						require.NoError(t, json.Unmarshal(raw, &entry))
						var id, source string
						require.NoError(t, json.Unmarshal(entry[format.idField], &id))
						require.NoError(t, json.Unmarshal(entry["source"], &source))
						require.Contains(t, tc.want, id)
						require.Equal(t, tc.want[id], source)
						require.JSONEq(t, `{"keep":true}`, string(entry["unknown"]))
					}
				})
			}
		})
	}
}

func TestMappedModelsDiscoveryPreservesRawCache(t *testing.T) {
	for _, tc := range []struct {
		name  string
		body  string
		codex bool
	}{
		{name: "普通目录缓存仍保留原名", body: `{"object":"list","data":[{"id":"cline-pass/deepseek-v4.1-flash","source":"原始元数据"}]}`},
		{name: "编码目录缓存仍保留原名", body: `{"models":[{"slug":"cline-pass/deepseek-v4.1-flash","source":"原始元数据"}]}`, codex: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			service := newCodexModelsAPIKeyTestService(&codexModelsHTTPUpstreamStub{do: func(_ *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
				calls.Add(1)
				return ordinaryModelsUpstreamResponse(tc.body), nil
			}})
			account := newCodexModelsAPIKeyTestAccount("https://models.example/v1")
			account.Credentials["model_mapping"] = map[string]any{
				"cline-pass/deepseek-v4.1-flash": "cline-pass/deepseek-v4.1-flash",
				"deepseek-flash":                 "cline-pass/deepseek-v4.1-flash",
			}
			fetch := func() (*OpenAIModelsResponse, error) {
				if tc.codex {
					return service.FetchCodexModelsManifest(context.Background(), account, CodexCanonicalClientVersion(), "")
				}
				return service.FetchOpenAIModelsList(context.Background(), account)
			}
			response, err := fetch()
			require.NoError(t, err)
			original := string(response.Body)
			response.Body, err = projectAccountModelsBody(response.Body, account, nil, tc.codex)
			require.NoError(t, err)
			require.NotContains(t, string(response.Body), "cline-pass/deepseek-v4.1-flash")
			require.Contains(t, string(response.Body), "deepseek-flash")
			cached, err := fetch()
			require.NoError(t, err)
			require.Equal(t, original, string(cached.Body), "别名展示不得污染供其他请求复用的原始发现缓存")
			require.EqualValues(t, 1, calls.Load(), "第二次发现应直接命中原始缓存")
		})
	}
}
