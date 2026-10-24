package admin

// codex_third_party_models_handler.go 是「Codex 三方模型启用」的管理接口。
//
// 管理员认证、审计和限流由路由组统一挂载，这里不再重复。所有响应只包含展示配置、
// 目录评估结果和源快照状态，不含任何账号凭据。错误统一走 response.ErrorFrom，
// 由服务层返回带稳定错误码的 ApplicationError（400 校验失败、404 分组不存在、409 版本冲突）。

import (
	"context"
	"fmt"
	"strconv"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
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
	RefreshCatalogs(ctx context.Context, groupID int64) (*service.CodexThirdPartyRefreshResult, error)
	ListProfileAliases(ctx context.Context) (*service.CodexProfileAliasList, error)
	SetProfileAlias(ctx context.Context, upstreamModel, profileID, operator string) (*service.CodexProfileAliasList, error)
	DeleteProfileAlias(ctx context.Context, upstreamModel string) (*service.CodexProfileAliasList, error)
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

// Refresh 同步刷新分组页面判定依赖的上游目录（按模型分流账号的协议目录、官方账号基础目录），
// 返回逐账号刷新结果和刷新后的分组状态（与读取接口同结构）。同一分组短时间内重复调用会复用上次结果。
// POST /api/v1/admin/codex-third-party-models/:group_id/refresh
func (h *CodexThirdPartyModelsHandler) Refresh(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	groupID, ok := parseCodexThirdPartyGroupID(c)
	if !ok {
		return
	}
	result, err := h.svc.RefreshCatalogs(c.Request.Context(), groupID)
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// codexProfileAliasBody 是新增或更新型号别名的请求体。
type codexProfileAliasBody struct {
	UpstreamModel string `json:"upstream_model" binding:"required"`
	ProfileID     string `json:"profile_id" binding:"required"`
}

// ListProfileAliases 返回全部型号别名（含失效项）和可供关联的各内置能力档案。
// GET /api/v1/admin/codex-third-party-profile-aliases
func (h *CodexThirdPartyModelsHandler) ListProfileAliases(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	result, err := h.svc.ListProfileAliases(c.Request.Context())
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// SetProfileAlias 新增或更新一条关联：把上游型号关联到已有能力档案（幂等）。
// PUT /api/v1/admin/codex-third-party-profile-aliases
func (h *CodexThirdPartyModelsHandler) SetProfileAlias(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	var body codexProfileAliasBody
	if err := c.ShouldBindJSON(&body); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	operator := ""
	if subject, ok := middleware2.GetAuthSubjectFromContext(c); ok {
		operator = fmt.Sprintf("user:%d", subject.UserID)
	}
	result, err := h.svc.SetProfileAlias(c.Request.Context(), body.UpstreamModel, body.ProfileID, operator)
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}

// DeleteProfileAlias 解除上游型号与能力档案的关联（幂等，本来没有关联也成功）。
// DELETE /api/v1/admin/codex-third-party-profile-aliases?upstream_model=...
func (h *CodexThirdPartyModelsHandler) DeleteProfileAlias(c *gin.Context) {
	if !h.serviceReady(c) {
		return
	}
	model := c.Query("upstream_model")
	if model == "" {
		response.BadRequest(c, "Invalid request: upstream_model is required")
		return
	}
	result, err := h.svc.DeleteProfileAlias(c.Request.Context(), model)
	if response.ErrorFrom(c, err) {
		return
	}
	response.Success(c, result)
}
