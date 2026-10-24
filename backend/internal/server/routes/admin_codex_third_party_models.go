package routes

import (
	adminhandler "github.com/Wei-Shaw/sub2api/internal/handler/admin"

	"github.com/gin-gonic/gin"
)

// registerCodexThirdPartyModelsRoutes 注册「Codex 三方模型启用」管理接口。
//
// admin 是已经挂好管理员认证、审计和限流中间件的 /admin 路由组，这里不再重复挂载。
// 接口路径统一以分组 ID 为作用域：
//
//	GET  /codex-third-party-models/:group_id          读取配置、评估结果、源快照状态
//	POST /codex-third-party-models/:group_id/preview  预览变更（不落库）
//	PUT  /codex-third-party-models/:group_id          保存变更（整批原子提交）
//	POST /codex-third-party-models/:group_id/verify   标记已验证
//	POST /codex-third-party-models/:group_id/sync     手动同步账号模型 ID 源（后台执行）
//	POST /codex-third-party-models/:group_id/refresh  刷新页面依赖的上游目录（同步执行，返回逐账号结果与最新状态）
func registerCodexThirdPartyModelsRoutes(admin *gin.RouterGroup, h *adminhandler.CodexThirdPartyModelsHandler) {
	models := admin.Group("/codex-third-party-models")
	{
		models.GET("/:group_id", h.Get)
		models.POST("/:group_id/preview", h.Preview)
		models.PUT("/:group_id", h.Save)
		models.POST("/:group_id/verify", h.Verify)
		models.POST("/:group_id/sync", h.Sync)
		models.POST("/:group_id/refresh", h.Refresh)
	}
}
