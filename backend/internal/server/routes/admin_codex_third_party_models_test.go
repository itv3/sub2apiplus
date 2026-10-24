package routes

import (
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"

	adminhandler "github.com/Wei-Shaw/sub2api/internal/handler/admin"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 路由表必须恰好是约定的五条，方法和路径都不能错：读取用 GET、预览/验收/同步用 POST、保存用 PUT。
func TestRegisterCodexThirdPartyModelsRoutesExposesExactlyTheDocumentedEndpoints(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	admin := router.Group("/api/v1/admin")
	registerCodexThirdPartyModelsRoutes(admin, adminhandler.NewCodexThirdPartyModelsHandler(nil))

	var got []string
	for _, route := range router.Routes() {
		got = append(got, route.Method+" "+route.Path)
	}
	sort.Strings(got)
	require.Equal(t, []string{
		"DELETE /api/v1/admin/codex-third-party-profile-aliases",
		"GET /api/v1/admin/codex-third-party-models/:group_id",
		"GET /api/v1/admin/codex-third-party-profile-aliases",
		"POST /api/v1/admin/codex-third-party-models/:group_id/preview",
		"POST /api/v1/admin/codex-third-party-models/:group_id/refresh",
		"POST /api/v1/admin/codex-third-party-models/:group_id/sync",
		"POST /api/v1/admin/codex-third-party-models/:group_id/verify",
		"PUT /api/v1/admin/codex-third-party-models/:group_id",
		"PUT /api/v1/admin/codex-third-party-profile-aliases",
	}, got)
}

// 路由组上的中间件必须对新接口生效：这里用一个拒绝一切请求的中间件代替管理员认证，
// 确认全部接口都在它后面，而不是绕开了路由组。
func TestRegisterCodexThirdPartyModelsRoutesInheritsGroupMiddleware(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	admin := router.Group("/api/v1/admin")
	admin.Use(func(c *gin.Context) { c.AbortWithStatus(http.StatusUnauthorized) })
	registerCodexThirdPartyModelsRoutes(admin, adminhandler.NewCodexThirdPartyModelsHandler(nil))

	for _, call := range []struct{ method, path string }{
		{http.MethodGet, "/api/v1/admin/codex-third-party-models/1"},
		{http.MethodPost, "/api/v1/admin/codex-third-party-models/1/preview"},
		{http.MethodPut, "/api/v1/admin/codex-third-party-models/1"},
		{http.MethodPost, "/api/v1/admin/codex-third-party-models/1/verify"},
		{http.MethodPost, "/api/v1/admin/codex-third-party-models/1/sync"},
		{http.MethodPost, "/api/v1/admin/codex-third-party-models/1/refresh"},
		{http.MethodGet, "/api/v1/admin/codex-third-party-profile-aliases"},
		{http.MethodPut, "/api/v1/admin/codex-third-party-profile-aliases"},
		{http.MethodDelete, "/api/v1/admin/codex-third-party-profile-aliases"},
	} {
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, httptest.NewRequest(call.method, call.path, nil))
		require.Equal(t, http.StatusUnauthorized, rec.Code, "%s %s", call.method, call.path)
	}
}
