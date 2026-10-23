package admin

// codex_third_party_models_handler.go 是「Codex 三方模型启用」的管理接口。
//
// 管理员认证、审计和限流由路由组统一挂载，这里不再重复。所有响应只包含展示配置、
// 目录评估结果和源快照状态，不含任何账号凭据。错误统一走 response.ErrorFrom，
// 由服务层返回带稳定错误码的 ApplicationError（400 校验失败、404 分组不存在、409 版本冲突）。

import (
	"context"
	"strconv"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// codexThirdPartyModelsService 是处理器依赖的服务能力，*service.CodexThirdPartyAdminService 满足该接口。
// 用接口承接是为了让处理器测试不必拼装整套服务依赖。
type codexThirdPartyModelsService interface {
	GetGroupState(ctx context.Context, groupID int64) (*service.CodexThirdPartyGroupState, error)
	PreviewChanges(ctx context.Context, groupID int64, req service.CodexThirdPartyPreviewRequest) (*service.CodexThirdPartyPreviewResult, error)
	SaveChanges(ctx context.Context, groupID int64, req service.CodexThirdPartySaveRequest) (*service.CodexThirdPartySaveResult, error)
	MarkVerified(ctx context.Context, groupID int64, req service.CodexThirdPartyVerifyRequest) (*service.CodexThirdPartyVerifyResult, error)
	SyncSources(ctx context.Context, groupID int64) (*service.CodexThirdPartySyncResult, error)
}

// CodexThirdPartyModelsHandler 处理 Codex 三方模型启用相关的管理接口。
type CodexThirdPartyModelsHandler struct {
	svc codexThirdPartyModelsService
}

// NewCodexThirdPartyModelsHandler 创建处理器。
func NewCodexThirdPartyModelsHandler(svc *service.CodexThirdPartyAdminService) *CodexThirdPartyModelsHandler {
	handler := &CodexThirdPartyModelsHandler{}
	if svc != nil {
		handler.svc = svc
	}
	return handler
}

// codexThirdPartyChangeBody 是请求里的单项开关变更。enabled 用指针，缺失时可以和 false 区分开。
type codexThirdPartyChangeBody struct {
	Slug    string `json:"slug"`
	Enabled *bool  `json:"enabled"`
}

// codexThirdPartyPreviewBody 是预览请求体。expected_revision 必填：0 表示期望配置尚不存在。
type codexThirdPartyPreviewBody struct {
	ExpectedRevision *int64                      `json:"expected_revision" binding:"required"`
	Changes          []codexThirdPartyChangeBody `json:"changes" binding:"required"`
}

// codexThirdPartySaveBody 是保存请求体。含开启的变更必须带预览返回的 input_fingerprint。
type codexThirdPartySaveBody struct {
	ExpectedRevision *int64                      `json:"expected_revision" binding:"required"`
	Changes          []codexThirdPartyChangeBody `json:"changes" binding:"required"`
	InputFingerprint string                      `json:"input_fingerprint"`
}

// codexThirdPartyVerifyBody 是标记已验证请求体。
type codexThirdPartyVerifyBody struct {
	ExpectedRevision *int64 `json:"expected_revision" binding:"required"`
	Slug             string `json:"slug" binding:"required"`
	Fingerprint      string `json:"fingerprint" binding:"required"`
	Note             string `json:"note"`
}

// convertCodexThirdPartyChanges 把请求里的变更转成服务层结构，缺 slug 或缺 enabled 的项视为请求不合法。
func convertCodexThirdPartyChanges(items []codexThirdPartyChangeBody) ([]service.CodexThirdPartyChange, bool) {
	out := make([]service.CodexThirdPartyChange, 0, len(items))
	for _, item := range items {
		if item.Slug == "" || item.Enabled == nil {
			return nil, false
		}
		out = append(out, service.CodexThirdPartyChange{Slug: item.Slug, Enabled: *item.Enabled})
	}
	return out, true
}

func parseCodexThirdPartyGroupID(c *gin.Context) (int64, bool) {
	groupID, err := strconv.ParseInt(c.Param("group_id"), 10, 64)
	if err != nil || groupID <= 0 {
		response.BadRequest(c, "Invalid group ID")
		return 0, false
	}
	return groupID, true
}

// serviceReady 在服务未装配时直接返回 500，避免空指针。
func (h *CodexThirdPartyModelsHandler) serviceReady(c *gin.Context) bool {
	if h == nil || h.svc == nil {
		response.InternalError(c, "Codex third-party models service is not configured")
		return false
	}
	return true
}

// Get 读取分组的展示配置、候选评估结果和源快照状态。
// GET /api/v1/admin/codex-third-party-models/:group_id
func (h *CodexThirdPartyModelsHandler) Get(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	state, err := h.svc.GetGroupState(c.Request.Context(), groupID)
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, state)
}

// Preview 在当前配置的副本上叠加变更并返回评估结果，不落库。
// POST /api/v1/admin/codex-third-party-models/:group_id/preview
func (h *CodexThirdPartyModelsHandler) Preview(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	var body codexThirdPartyPreviewBody
	if err := c.ShouldBindJSON(&body); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	changes, valid := convertCodexThirdPartyChanges(body.Changes)
	if !valid {
		response.BadRequest(c, "Invalid request: every change needs slug and enabled")
		return
	}
	result, err := h.svc.PreviewChanges(c.Request.Context(), groupID, service.CodexThirdPartyPreviewRequest{
		ExpectedRevision: *body.ExpectedRevision,
		Changes:          changes,
	})
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// Save 保存一批开关变更，整批原子提交。
// PUT /api/v1/admin/codex-third-party-models/:group_id
func (h *CodexThirdPartyModelsHandler) Save(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	var body codexThirdPartySaveBody
	if err := c.ShouldBindJSON(&body); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	changes, valid := convertCodexThirdPartyChanges(body.Changes)
	if !valid {
		response.BadRequest(c, "Invalid request: every change needs slug and enabled")
		return
	}
	result, err := h.svc.SaveChanges(c.Request.Context(), groupID, service.CodexThirdPartySaveRequest{
		ExpectedRevision: *body.ExpectedRevision,
		Changes:          changes,
		InputFingerprint: body.InputFingerprint,
	})
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// Verify 把管理员的真实调用验收结果绑定到候选的当前指纹。
// POST /api/v1/admin/codex-third-party-models/:group_id/verify
func (h *CodexThirdPartyModelsHandler) Verify(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	var body codexThirdPartyVerifyBody
	if err := c.ShouldBindJSON(&body); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	result, err := h.svc.MarkVerified(c.Request.Context(), groupID, service.CodexThirdPartyVerifyRequest{
		ExpectedRevision: *body.ExpectedRevision,
		Slug:             body.Slug,
		Fingerprint:      body.Fingerprint,
		Note:             body.Note,
	})
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// Sync 对分组全部账号强制刷新模型 ID 源，后台执行，立即返回各账号状态（HTTP 202）。
// POST /api/v1/admin/codex-third-party-models/:group_id/sync
func (h *CodexThirdPartyModelsHandler) Sync(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	result, err := h.svc.SyncSources(c.Request.Context(), groupID)
	if response.ErrorFrom(c, err) {
		return
	}
	response.Accepted(c, result)
}
