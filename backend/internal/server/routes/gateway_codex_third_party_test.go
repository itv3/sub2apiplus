package routes

import (
	"net/http"
	"net/http/httptest"
	"testing"

	servermiddleware "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 「Codex 三方模型启用」的路由分派：Composite 与 OpenAI 走 OpenAI 目录 handler，
// 国产兼容平台与其它 OpenAI 协议族平台走通用目录 handler，Anthropic 等不提供在线目录。

func codexThirdPartyTestContext(platform, target string) *gin.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, target, nil)
	c.Set(string(servermiddleware.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: 8, Platform: platform},
	})
	return c
}

func TestCodexModelsManifestRequestCoversCompatiblePlatforms(t *testing.T) {
	cases := []struct {
		platform string
		query    string
		want     bool
	}{
		{service.PlatformOpenAI, "?client_version=1.0.0", true},
		{service.PlatformComposite, "?client_version=1.0.0", true},
		{service.PlatformKimi, "?client_version=1.0.0", true},
		{service.PlatformZhipu, "?client_version=1.0.0", true},
		{service.PlatformDeepseek, "?client_version=1.0.0", true},
		{service.PlatformMiniMax, "?client_version=1.0.0", true},
		{service.PlatformKimi, "", false},
		{service.PlatformKimi, "?client_version=%20", false},
		{service.PlatformAnthropic, "?client_version=1.0.0", false},
		{service.PlatformGemini, "?client_version=1.0.0", false},
		{service.PlatformAntigravity, "?client_version=1.0.0", false},
	}
	for _, tc := range cases {
		t.Run(tc.platform+tc.query, func(t *testing.T) {
			c := codexThirdPartyTestContext(tc.platform, "/v1/models"+tc.query)
			require.Equal(t, tc.want, isCodexModelsManifestRequest(c))
		})
	}
}

func TestDispatchCodexModelsGatewayRoutesByPlatform(t *testing.T) {
	cases := map[string]string{
		service.PlatformOpenAI:    "openai",
		service.PlatformComposite: "openai",
		service.PlatformKimi:      "generated",
		service.PlatformZhipu:     "generated",
		service.PlatformDeepseek:  "generated",
	}
	for platform, want := range cases {
		t.Run(platform, func(t *testing.T) {
			c := codexThirdPartyTestContext(platform, "/backend-api/codex/models")
			called := ""
			dispatchCodexModelsGateway(c,
				func(*gin.Context) { called = "openai" },
				func(*gin.Context) { called = "generated" },
			)
			require.Equal(t, want, called)
		})
	}
}

// 纯国产平台分组的 /v1/models?client_version=... 与 /backend-api/codex/models 都能拿到 Codex 目录形态，
// 普通 /v1/models（无 client_version）不进入目录分派。
func TestPureCNGroupModelsRequestReturnsCodexCatalog(t *testing.T) {
	router := newGatewayRoutesTestRouterWithGroup(allowlistGroup(service.PlatformKimi, false))
	for _, path := range []string{"/v1/models?client_version=1.0.0", "/models?client_version=1.0.0", "/backend-api/codex/models"} {
		t.Run(path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
			require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
			require.JSONEq(t, `{"models":[]}`, rec.Body.String())
			require.NotEmpty(t, rec.Header().Get("ETag"))
		})
	}
}
