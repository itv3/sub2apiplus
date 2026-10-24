package admin

import (
	"net/http"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 保存会话接口的请求体校验：到期时间格式、两个字段都缺失。
func TestOllamaCloudUsageSessionRequestValidatesExpiry(t *testing.T) {
	gin.SetMode(gin.TestMode)
	svc := newOllamaCloudUsageHandlerTestService(t)

	for name, tc := range map[string]struct {
		body string
		want int
	}{
		"neither session nor expiry":   {`{}`, http.StatusBadRequest},
		"invalid expiry format":        {`{"session_expires_at":"2026-13-45"}`, http.StatusBadRequest},
		"expiry not a string":          {`{"session_expires_at":12345}`, http.StatusBadRequest},
		"invalid expiry with session":  {`{"session":"wos-session=a","session_expires_at":"tomorrow"}`, http.StatusBadRequest},
		"expiry only reaches service":  {`{"session_expires_at":"2026-12-01T00:00:00Z"}`, http.StatusServiceUnavailable},
		"explicit null clears expiry":  {`{"session_expires_at":null}`, http.StatusServiceUnavailable},
		"empty string clears expiry":   {`{"session_expires_at":""}`, http.StatusServiceUnavailable},
		"session with expiry to store": {`{"session":"wos-session=a","session_expires_at":"2026-12-01T08:00:00+08:00"}`, http.StatusServiceUnavailable},
	} {
		t.Run(name, func(t *testing.T) {
			ctx, recorder := newOllamaCloudUsageHandlerContext(http.MethodPut, "/admin/accounts/7/ollama-cloud-usage/session", tc.body, "7")
			(&AccountHandler{ollamaCloudUsage: svc}).SaveOllamaCloudUsageSession(ctx)
			require.Equal(t, tc.want, recorder.Code, recorder.Body.String())
		})
	}
}
