package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestAnthropicAPIKeyMimicMessagesUsesOfficialHTTP1Headers(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")

	service := &GatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}}
	account := &Account{
		ID:       901,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
	}
	body := []byte(`{"model":"claude-fable-5","messages":[{"role":"user","content":"hello"}]}`)

	req, _, err := service.buildUpstreamRequest(
		context.Background(), c, account, body, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)
	require.Equal(t, "application/json", req.Header.Get("Content-Type"))
	require.Contains(t, req.Header, "Content-Type")
	require.NotContains(t, req.Header, "content-type")
	require.Equal(t, "gzip, deflate, br, zstd", req.Header.Get("Accept-Encoding"))
	require.Equal(t, "keep-alive", req.Header.Get("Connection"))

	var wire bytes.Buffer
	require.NoError(t, req.Write(&wire))
	wireText := wire.String()
	require.True(t, strings.Contains(wireText, "\r\nContent-Type: application/json\r\n"))
	require.True(t, strings.Contains(wireText, "\r\nAccept-Encoding: gzip, deflate, br, zstd\r\n"))
	require.True(t, strings.Contains(wireText, "\r\nConnection: keep-alive\r\n"))
}

func TestAnthropicAPIKeyWithoutMimicDoesNotUseOfficialHTTP1Headers(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.3.50")

	service := &GatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}}
	account := &Account{ID: 902, Platform: PlatformAnthropic, Type: AccountTypeAPIKey}
	body := []byte(`{"model":"claude-fable-5","messages":[{"role":"user","content":"hello"}]}`)

	req, _, err := service.buildUpstreamRequest(
		context.Background(), c, account, body, "anthropic-key", "apikey", "claude-fable-5", true, false,
	)
	require.NoError(t, err)
	require.Empty(t, req.Header.Get("Accept-Encoding"))
	require.Empty(t, req.Header.Get("Connection"))
}
