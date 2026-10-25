package handler

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// rejectOpenAIWSPinnedHTTPFallback 在 WebSocket 握手 Accept 之前调用。
// 该会话此前因需要协议转换而被拒绝过时，直接回 426，Codex 收到后立刻改走 HTTPS，
// 不再先建连接、再被断开、再重试。返回 true 表示已写出响应，调用方应直接返回。
// 握手没带会话头时不会命中，行为与改动前一致。
func (h *OpenAIGatewayHandler) rejectOpenAIWSPinnedHTTPFallback(c *gin.Context) bool {
	if !service.OpenAIWSHTTPFallbackPinned(c) {
		return false
	}
	h.errorResponse(c, http.StatusUpgradeRequired, "invalid_request_error", "This session is served over HTTPS because the selected upstream requires protocol conversion")
	return true
}
