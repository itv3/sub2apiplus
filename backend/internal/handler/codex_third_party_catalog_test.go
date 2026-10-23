package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

type cxtpSettingRepo struct {
	service.SettingRepository
	values map[string]string
}

func (r cxtpSettingRepo) GetValue(_ context.Context, key string) (string, error) {
	if value, ok := r.values[key]; ok {
		return value, nil
	}
	return "", service.ErrSettingNotFound
}

type cxtpAccountRepo struct {
	service.AccountRepository
	accounts []service.Account
}

func (r cxtpAccountRepo) ListModelAvailabilityCandidates(context.Context, *int64, []string, bool) ([]service.Account, error) {
	return append([]service.Account(nil), r.accounts...), nil
}

func cxtpService(t *testing.T, enabled []string, accounts ...service.Account) *service.CodexThirdPartyCatalogService {
	t.Helper()
	cfg := service.NewCodexModelVisibilityConfig()
	cfg.Revision = 1
	for _, slug := range enabled {
		cfg.Models[slug] = service.CodexModelVisibilityEntry{Enabled: true}
	}
	raw, err := cfg.Marshal()
	require.NoError(t, err)
	settings := service.NewSettingService(cxtpSettingRepo{values: map[string]string{
		service.CodexModelVisibilitySettingKey(8): raw,
	}}, nil)
	return service.NewCodexThirdPartyCatalogService(settings, cxtpAccountRepo{accounts: accounts}, nil, nil, nil)
}

func cxtpKimiAccount() service.Account {
	return service.Account{
		ID: 4, Platform: service.PlatformKimi, Type: service.AccountTypeAPIKey,
		Credentials: map[string]any{
			"api_key": "sk-test-secret", "base_url": "https://kimi.example", "api_protocol": "responses",
			"model_mapping": map[string]any{"kimi-k3": "kimi-k3"},
		},
	}
}

func cxtpContext(target string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, target, nil)
	return c, rec
}

func cxtpSlugs(t *testing.T, body []byte) []string {
	t.Helper()
	var parsed struct {
		Models []struct {
			Slug string `json:"slug"`
		} `json:"models"`
	}
	require.NoError(t, json.Unmarshal(body, &parsed))
	slugs := make([]string, 0, len(parsed.Models))
	for _, model := range parsed.Models {
		slugs = append(slugs, model.Slug)
	}
	return slugs
}

const cxtpBaseBody = `{"extra_top":1,"models":[{"slug":"gpt-5.6-sol","display_name":"GPT","x_keep":true},{"slug":"kimi-k3","display_name":"old"}]}`

func TestCodexThirdPartyHandlerWithoutServiceKeepsOriginalBehavior(t *testing.T) {
	h := &OpenAIGatewayHandler{}
	c, rec := cxtpContext("/v1/models?client_version=1.0.0")
	h.writeCodexModelsResponseWithPolicy(c, &service.Group{ID: 8, Platform: service.PlatformOpenAI}, &service.OpenAIModelsResponse{Body: []byte(cxtpBaseBody)}, "")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, cxtpBaseBody, rec.Body.String())
	require.NotEmpty(t, rec.Header().Get("ETag"))

	body, ok := (&GatewayHandler{}).applyCodexThirdParty(c, &service.Group{ID: 8}, []byte(cxtpBaseBody))
	require.True(t, ok)
	require.Equal(t, cxtpBaseBody, string(body))
	require.False(t, h.writeCodexThirdPartyOnlyCatalog(c, &service.Group{ID: 8, Platform: service.PlatformComposite}, ""))
}

func TestCodexThirdPartyHandlerFiltersAndPublishes(t *testing.T) {
	oauth := service.Account{ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Credentials: map[string]any{}}
	group := &service.Group{ID: 8, Platform: service.PlatformComposite}
	routeHint := cxtpKimiAccount()

	// 关闭：基础目录里的 kimi-k3 被移除，原生条目保持不变。
	h := &OpenAIGatewayHandler{}
	h.SetCodexThirdPartyCatalog(cxtpService(t, nil, oauth, routeHint))
	c, rec := cxtpContext("/v1/models?client_version=1.0.0")
	h.writeCodexModelsResponseWithPolicy(c, group, &service.OpenAIModelsResponse{Body: []byte(cxtpBaseBody)}, "")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, rec.Body.Bytes()))
	require.Contains(t, rec.Body.String(), `"x_keep":true`)
	require.Contains(t, rec.Body.String(), `"extra_top":1`)
	closedETag := rec.Header().Get("ETag")

	// 开启：kimi-k3 被档案条目替换；ETag 随最终正文变化，旧 ETag 不再返回 304。
	h.SetCodexThirdPartyCatalog(cxtpService(t, []string{"kimi-k3"}, oauth, routeHint))
	c, rec = cxtpContext("/v1/models?client_version=1.0.0")
	h.writeCodexModelsResponseWithPolicy(c, group, &service.OpenAIModelsResponse{Body: []byte(cxtpBaseBody)}, closedETag)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, []string{"gpt-5.6-sol", "kimi-k3"}, cxtpSlugs(t, rec.Body.Bytes()))
	require.NotEqual(t, closedETag, rec.Header().Get("ETag"))

	// 相同输入重复请求得到相同 ETag，If-None-Match 命中返回 304。
	etag := rec.Header().Get("ETag")
	c, rec = cxtpContext("/v1/models?client_version=1.0.0")
	h.writeCodexModelsResponseWithPolicy(c, group, &service.OpenAIModelsResponse{Body: []byte(cxtpBaseBody)}, etag)
	require.Equal(t, http.StatusNotModified, rec.Code)
}

func TestCodexThirdPartyGatewayHandlerAppliesPolicy(t *testing.T) {
	h := &GatewayHandler{}
	h.SetCodexThirdPartyCatalog(cxtpService(t, []string{"kimi-k3"}, cxtpKimiAccount()))
	group := &service.Group{ID: 8, Platform: service.PlatformKimi}
	generated := `{"models":[{"slug":"kimi-k3","display_name":"generated"},{"slug":"kimi-other","display_name":"x"}]}`
	c, _ := cxtpContext("/backend-api/codex/models")
	body, ok := h.applyCodexThirdParty(c, group, []byte(generated))
	require.True(t, ok)
	require.Equal(t, []string{"kimi-k3"}, cxtpSlugs(t, body))
	require.NotContains(t, string(body), `"display_name":"generated"`)
}

func TestCodexThirdPartyCompositeWithoutOpenAIAccountsServesThirdPartyOnly(t *testing.T) {
	group := &service.Group{ID: 8, Platform: service.PlatformComposite}
	h := &OpenAIGatewayHandler{}
	h.SetCodexThirdPartyCatalog(cxtpService(t, []string{"kimi-k3"}, cxtpKimiAccount()))
	c, rec := cxtpContext("/v1/models?client_version=1.0.0")
	require.True(t, h.writeCodexThirdPartyOnlyCatalog(c, group, ""))
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, []string{"kimi-k3"}, cxtpSlugs(t, rec.Body.Bytes()))

	// 分组里仍有 OpenAI 账号（只是暂不可调度）时不改写，沿用原有的「暂无可用账号」错误。
	oauth := service.Account{ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Credentials: map[string]any{}}
	h.SetCodexThirdPartyCatalog(cxtpService(t, []string{"kimi-k3"}, oauth, cxtpKimiAccount()))
	c, rec = cxtpContext("/v1/models?client_version=1.0.0")
	require.False(t, h.writeCodexThirdPartyOnlyCatalog(c, group, ""))
	require.Empty(t, strings.TrimSpace(rec.Body.String()))

	// 非 Composite 分组不走该分支。
	require.False(t, h.writeCodexThirdPartyOnlyCatalog(c, &service.Group{ID: 8, Platform: service.PlatformOpenAI}, ""))
}
