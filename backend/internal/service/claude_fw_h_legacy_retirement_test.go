package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestClaudeOAuthStrictRoutingDoesNotDependOnReleaseFlags(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{ID: 100, Platform: PlatformAnthropic, Type: AccountTypeOAuth}
	svc := &GatewayService{cfg: &config.Config{}}

	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens", "/messages/count_tokens"} {
		ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ctx.Request = httptest.NewRequest(http.MethodPost, path, nil)
		if path == "/v1/messages" {
			require.True(t, svc.shouldRouteClaudeStrictMessages(ctx, account))
		} else {
			require.True(t, svc.shouldRouteClaudeStrictCountTokens(ctx, account))
		}
	}
	require.True(t, svc.shouldRouteClaudeStrictOpenAI(account))
	require.True(t, svc.ShouldFailCloseClaudeStrictResponsesWebSocket(account))
	require.True(t, (&OpenAIGatewayService{}).ShouldFailCloseClaudeStrictResponsesWebSocket(account))

	setupToken := &Account{ID: 101, Platform: PlatformAnthropic, Type: AccountTypeSetupToken}
	require.False(t, svc.shouldRouteClaudeStrictOpenAI(setupToken))
}

func TestClaudeOAuthRetiredBuildersFailCloseLocally(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{ID: 100, Platform: PlatformAnthropic, Type: AccountTypeOAuth}
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"test"}]}`)
	ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
	ctx.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	svc := &GatewayService{}

	_, _, err := svc.buildUpstreamRequest(
		context.Background(), ctx, account, body, "token", "oauth",
		"claude-sonnet-5", false, false,
	)
	require.ErrorContains(t, err, "旧 Messages 构造链已退休")
	_, _, err = svc.buildCountTokensRequest(
		context.Background(), ctx, account, body, "token", "oauth",
		"claude-sonnet-5", false,
	)
	require.ErrorContains(t, err, "旧 count_tokens 构造链已退休")

	next, model := svc.applyClaudeSetupTokenThirdPartyCompatibilityToBody(
		context.Background(), ctx, account, body, nil, "claude-sonnet-5", false,
	)
	require.True(t, bytes.Equal(body, next))
	require.Equal(t, "claude-sonnet-5", model)
	_, _, err = resolveOfficialEgressAccountProfile(account)
	require.ErrorContains(t, err, "旧画像解析器已退休")
}

func TestClaudeOAuthRetiredSymbolsDoNotReturn(t *testing.T) {
	paths := []string{
		"gateway_claude_fw_g.go",
		"gateway_claude_strict_openai.go",
		"gateway_forward.go",
		"gateway_forward_as_chat_completions.go",
		"gateway_forward_as_responses.go",
		"gateway_count_tokens.go",
		"gateway_upstream_request.go",
		"gateway_claude_oauth_body.go",
		"official_egress_anthropic.go",
	}
	var productionSource []byte
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		require.NoError(t, err)
		productionSource = append(productionSource, raw...)
	}
	for _, retired := range []string{
		"claudeStrictRoutingEnabled",
		"applyClaudeOAuthThirdPartyCompatibilityToBody",
		"buildOAuthMetadataUserID(",
		"buildOAuthMetadataUserIDFromBody",
		"finalizeAnthropicOfficialEgressRequest",
		"officialEgressSinkClaudeLegacyMessages",
		"officialEgressSinkClaudeLegacyTokenCount",
	} {
		require.NotContains(t, string(productionSource), retired)
	}
	require.Contains(t, string(productionSource), "AccountTypeSetupToken")
}
