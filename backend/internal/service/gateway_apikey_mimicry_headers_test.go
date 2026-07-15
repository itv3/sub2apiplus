package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
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

func TestAPIKeyMimicKeepsContext1MDespiteDefaultBetaPolicy(t *testing.T) {
	// 默认 BetaPolicy 会对 claude-sonnet-4-6 过滤 context-1m；
	// API Key Desktop mimic 基线必须保留该 token。
	settingSvc := NewSettingService(
		&betaPolicySettingRepoStub{values: map[string]string{}},
		&config.Config{},
	)
	svc := &GatewayService{
		cfg: &config.Config{
			Gateway:  config.GatewayConfig{MaxLineSize: defaultMaxLineSize},
			Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}},
		},
		settingService: settingSvc,
	}
	account := &Account{
		ID:       903,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
		Credentials: map[string]any{"api_key": "anthropic-key"},
	}
	body := []byte(`{"model":"claude-sonnet-4-6","stream":true,"messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`)
	req, _, err := svc.buildUpstreamRequest(
		context.Background(), nil, account, body, "anthropic-key", "apikey", "claude-sonnet-4-6", true, true,
	)
	require.NoError(t, err)
	beta := getHeaderRaw(req.Header, "anthropic-beta")
	require.Contains(t, beta, claude.BetaContext1M)
	require.Equal(t, strings.Join(claude.APIKeyMimicBetas(), ","), beta)
}
