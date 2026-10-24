package admin

import (
	"bytes"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// ollamaCloudUsageSessionRequest 是保存会话 / 更新到期时间的统一请求体：
//   - session 非空：保存新 Cookie，session_expires_at 若给出则一并保存；
//   - session 为空且给出 session_expires_at：只更新到期时间（空串或 null 表示清除）；
//   - 两者都没有：400。
//
// session_expires_at 用 RawMessage 接收，以区分「未提供」和「显式 null」。
type ollamaCloudUsageSessionRequest struct {
	Session          string          `json:"session"`
	SessionExpiresAt json.RawMessage `json:"session_expires_at"`
}

// parseSessionExpiry 解析 session_expires_at：返回时间、是否提供了该字段、错误。
// 空串、null 视为「提供了但要清除」，此时时间为 nil。
func (r ollamaCloudUsageSessionRequest) parseSessionExpiry() (*time.Time, bool, error) {
	raw := bytes.TrimSpace(r.SessionExpiresAt)
	if len(raw) == 0 {
		return nil, false, nil
	}
	if bytes.Equal(raw, []byte("null")) {
		return nil, true, nil
	}
	var text string
	if err := json.Unmarshal(raw, &text); err != nil {
		return nil, true, err
	}
	text = strings.TrimSpace(text)
	if text == "" {
		return nil, true, nil
	}
	parsed, err := time.Parse(time.RFC3339, text)
	if err != nil {
		return nil, true, err
	}
	return &parsed, true, nil
}

type ollamaCloudUsageAutoRefreshRequest struct {
	Enabled *bool `json:"enabled" binding:"required"`
}

func (h *AccountHandler) GetOllamaCloudUsageSettings(c *gin.Context) {
	if h.ollamaCloudUsage == nil {
		response.ErrorFrom(c, service.ErrOllamaCloudUsageUnavailable)
		return
	}
	settings, err := h.ollamaCloudUsage.GetSettings(c.Request.Context())
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, settings)
}

func (h *AccountHandler) UpdateOllamaCloudUsageSettings(c *gin.Context) {
	if h.ollamaCloudUsage == nil {
		response.ErrorFrom(c, service.ErrOllamaCloudUsageUnavailable)
		return
	}
	var req service.OllamaCloudUsageSettings
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	if err := h.ollamaCloudUsage.UpdateSettings(c.Request.Context(), &req); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	settings, err := h.ollamaCloudUsage.GetSettings(c.Request.Context())
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, settings)
}

func (h *AccountHandler) GetOllamaCloudUsage(c *gin.Context) {
	if !h.requireOllamaCloudUsage(c) {
		return
	}
	accountID, ok := ollamaCloudUsageAccountID(c)
	if !ok {
		return
	}
	state, err := h.ollamaCloudUsage.GetState(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, state)
}

func (h *AccountHandler) SaveOllamaCloudUsageSession(c *gin.Context) {
	if !h.requireOllamaCloudUsage(c) {
		return
	}
	accountID, ok := ollamaCloudUsageAccountID(c)
	if !ok {
		return
	}
	var req ollamaCloudUsageSessionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	expiresAt, expiryProvided, err := req.parseSessionExpiry()
	if err != nil {
		response.BadRequest(c, "Invalid session_expires_at: must be an RFC3339 timestamp")
		return
	}
	var state *service.OllamaCloudUsageState
	switch {
	case strings.TrimSpace(req.Session) != "":
		state, err = h.ollamaCloudUsage.SaveSession(c.Request.Context(), accountID, req.Session, expiresAt)
	case expiryProvided:
		state, err = h.ollamaCloudUsage.SetSessionExpiry(c.Request.Context(), accountID, expiresAt)
	default:
		response.BadRequest(c, "Invalid request: session or session_expires_at is required")
		return
	}
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, state)
}

func (h *AccountHandler) DeleteOllamaCloudUsageSession(c *gin.Context) {
	if !h.requireOllamaCloudUsage(c) {
		return
	}
	accountID, ok := ollamaCloudUsageAccountID(c)
	if !ok {
		return
	}
	state, err := h.ollamaCloudUsage.DeleteSession(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, state)
}

func (h *AccountHandler) SetOllamaCloudUsageAutoRefresh(c *gin.Context) {
	if !h.requireOllamaCloudUsage(c) {
		return
	}
	accountID, ok := ollamaCloudUsageAccountID(c)
	if !ok {
		return
	}
	var req ollamaCloudUsageAutoRefreshRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	state, err := h.ollamaCloudUsage.SetAutoRefresh(c.Request.Context(), accountID, *req.Enabled)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, state)
}

func (h *AccountHandler) RefreshOllamaCloudUsage(c *gin.Context) {
	if !h.requireOllamaCloudUsage(c) {
		return
	}
	accountID, ok := ollamaCloudUsageAccountID(c)
	if !ok {
		return
	}
	state, err := h.ollamaCloudUsage.Refresh(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, state)
}

func (h *AccountHandler) requireOllamaCloudUsage(c *gin.Context) bool {
	if h != nil && h.ollamaCloudUsage != nil {
		return true
	}
	response.ErrorFrom(c, service.ErrOllamaCloudUsageUnavailable)
	return false
}

func ollamaCloudUsageAccountID(c *gin.Context) (int64, bool) {
	if c == nil {
		return 0, false
	}
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || accountID <= 0 {
		response.BadRequest(c, "Invalid account ID")
		return 0, false
	}
	return accountID, true
}
