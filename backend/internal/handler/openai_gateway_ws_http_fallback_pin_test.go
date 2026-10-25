package handler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// recordPinForTest 模拟「该会话此前因协议转换被拒绝」，API Key 编号与 newOpenAIWSHandlerTestServer 保持一致。
func recordPinForTest(t *testing.T, sessionID string) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/openai/v1/responses", nil)
	c.Request.Header.Set("session-id", sessionID)
	c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{ID: 101})
	require.True(t, service.RecordOpenAIWSHTTPFallbackPin(c, 9))
}

// dialPinTestServer 带指定请求头发起握手，返回握手响应的状态码；握手成功（101）时立刻关闭连接。
func dialPinTestServer(t *testing.T, serverURL string, header http.Header) int {
	t.Helper()
	dialCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, response, _ := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(serverURL, "http")+"/openai/v1/responses", &coderws.DialOptions{HTTPHeader: header})
	if conn != nil {
		_ = conn.CloseNow()
	}
	require.NotNil(t, response)
	status := response.StatusCode
	_ = response.Body.Close()
	return status
}

// 命中标记的会话：握手回 426，且根本没走到后面的入口并发租约（说明在 Accept 之前就返回了）。
func TestOpenAIResponsesWebSocket_PinnedHTTPFallbackRejectedBeforeAccept(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var leaseCalls atomic.Int32
	cache := &concurrencyCacheMock{
		acquireIngressLeaseFn: func(context.Context, int64, int, string) (bool, error) {
			leaseCalls.Add(1)
			return false, nil // 未命中标记的请求会走到这里并得到 429，用来证明它们没被 426 误伤
		},
	}
	h := newOpenAIHandlerForPreviousResponseIDValidation(t, cache)
	h.cfg = &config.Config{}
	h.cfg.Gateway.OpenAIWS.MaxIngressConnectionsPerAPIKey = 1
	wsServer := newOpenAIWSHandlerTestServer(t, h, middleware.AuthSubject{UserID: 1, Concurrency: 1})
	defer wsServer.Close()

	const pinnedSession = "pin-handler-test-session-a"
	recordPinForTest(t, pinnedSession)

	// 1. 同一会话再次握手：426，租约没有被申请。
	status := dialPinTestServer(t, wsServer.URL, http.Header{"Session-Id": []string{pinnedSession}})
	require.Equal(t, http.StatusUpgradeRequired, status)
	require.Zero(t, leaseCalls.Load())

	// 2. 另一个会话（官方 GPT 之类的新会话）：不被拦截，继续走到租约（这里被桩拒成 429）。
	status = dialPinTestServer(t, wsServer.URL, http.Header{"Session-Id": []string{"pin-handler-test-session-other"}})
	require.Equal(t, http.StatusTooManyRequests, status)
	require.Equal(t, int32(1), leaseCalls.Load())

	// 3. 握手没带会话头：不拦截，行为与改动前一致。
	status = dialPinTestServer(t, wsServer.URL, nil)
	require.Equal(t, http.StatusTooManyRequests, status)
	require.Equal(t, int32(2), leaseCalls.Load())
}
