package service

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 2026-08-06 Vircs 生产事故回归：service 层任何一处向客户端完整写出错误响应
// 后，必须调用 MarkResponseCommitted。否则 handler 的 ensureForwardErrorResponse
// 会把"已写字节"误判为流式已开始，在 JSON 错误体尾部追加 response.failed SSE，
// 客户端（openai-python）解析拼接体直接 JSONDecodeError，真实上游错误被掩盖。

func TestWriteOpenAIWSFallbackErrorResponseMarksCommitted(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)

	s := &OpenAIGatewayService{}
	wsErr := wrapOpenAIWSFallback("auth_failed", errors.New("upstream authentication failed"))
	wrote := s.writeOpenAIWSFallbackErrorResponse(c, nil, wsErr)

	require.True(t, wrote)
	require.True(t, IsResponseCommitted(c))
	require.Equal(t, http.StatusUnauthorized, w.Code)
	require.True(t, json.Valid(w.Body.Bytes()), w.Body.String())
	require.NotContains(t, w.Body.String(), "event:")
}

func TestWriteOpenAIResponsesFallbackErrorMarksCommitted(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)

	writeOpenAIResponsesFallbackError(c, http.StatusBadRequest, "invalid_request_error", "model is required")

	require.True(t, IsResponseCommitted(c))
	require.Equal(t, http.StatusBadRequest, w.Code)
	require.True(t, json.Valid(w.Body.Bytes()), w.Body.String())
	require.NotContains(t, w.Body.String(), "event:")
}

func TestWriteOpenAIFastPolicyBlockedResponseMarksCommitted(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)

	writeOpenAIFastPolicyBlockedResponse(c, &OpenAIFastBlockedError{Message: "service_tier=priority is not allowed"})

	require.True(t, IsResponseCommitted(c))
	require.Equal(t, http.StatusForbidden, w.Code)
	require.True(t, json.Valid(w.Body.Bytes()), w.Body.String())
	require.NotContains(t, w.Body.String(), "event:")
}

// compact SSE 终止事件同样是完整错误传达：标记 committed 后 handler 不会
// 再补第二个 response.failed，避免严格 SDK 收到重复终止事件。
func TestWriteOpenAICompactSSEFailureMessageMarksCommitted(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses/compact", nil)

	writeOpenAICompactSSEFailureMessage(c, http.StatusBadGateway, "upstream_error", "Upstream request failed")

	require.True(t, IsResponseCommitted(c))
	body := w.Body.String()
	require.Equal(t, 1, strings.Count(body, "event: response.failed"))
	require.Contains(t, body, `"status":"failed"`)
}
