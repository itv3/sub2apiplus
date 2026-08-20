package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

const claudeFWGDesktopUserAgent = "claude-cli/2.1.234 (external, claude-desktop-3p, agent-sdk/0.3.234)"

func TestClaudeFWGIngressQueryIsClosed(t *testing.T) {
	for _, test := range []struct {
		rawQuery   string
		forceQuery bool
	}{
		{},
		{rawQuery: "beta=true"},
	} {
		require.True(t, claudeFWGIngressQueryAllowed(test.rawQuery, test.forceQuery), test)
	}

	for _, test := range []struct {
		rawQuery   string
		forceQuery bool
	}{
		{forceQuery: true},
		{rawQuery: "unknown=true"},
		{rawQuery: "beta=false"},
		{rawQuery: "beta=true&extra=1"},
		{rawQuery: "beta=true&beta=true"},
		{rawQuery: "beta=%74rue"},
	} {
		require.False(t, claudeFWGIngressQueryAllowed(test.rawQuery, test.forceQuery), test)
	}
}

func TestClaudeFWGOfficialDesktopBetaQueryUsesStrictPersona(t *testing.T) {
	gin.SetMode(gin.TestMode)
	upstream := &claudeFWGServiceUpstream{rejectFirstStream: true}
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
	svc := &GatewayService{
		cfg: cfg, rateLimitService: &RateLimitService{},
		responseHeaderFilter: compileResponseHeaderFilter(cfg),
		claudeTokenProvider:  NewClaudeTokenProvider(nil, nil, nil),
		officialEgress:       runtimeState,
	}
	account := claudeFWGServiceAccount()

	messagesBody := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"desktop query"}],"tools":[],"stream":true}`)
	messagesRecorder := httptest.NewRecorder()
	messagesContext, _ := gin.CreateTestContext(messagesRecorder)
	messagesContext.Request = httptest.NewRequest(
		http.MethodPost, "/v1/messages?beta=true", bytes.NewReader(messagesBody),
	)
	messagesContext.Request.Header.Set("Content-Type", "application/json")
	messagesContext.Request.Header.Set("User-Agent", claudeFWGDesktopUserAgent)
	messagesContext.Set("api_key", &APIKey{ID: 23})
	messagesContext.Request = messagesContext.Request.WithContext(
		WithOfficialClaudeIngressRuntime(messagesContext.Request.Context(), messagesContext),
	)
	messagesParsed, err := ParseGatewayRequest(NewRequestBodyRef(messagesBody), PlatformAnthropic)
	require.NoError(t, err)
	messagesResult, err := svc.Forward(
		context.Background(), messagesContext, account, messagesParsed,
	)
	require.NoError(t, err)
	require.NotNil(t, messagesResult)
	require.Equal(t, http.StatusOK, messagesRecorder.Code)

	messageCaptureCount := 0
	for _, capture := range upstream.captures {
		if capture.url == "https://api.anthropic.com/v1/messages?beta=true" {
			messageCaptureCount++
			require.Equal(t, "claude-cli/2.1.226 (external, sdk-cli)", capture.header.Get("User-Agent"))
		}
	}
	require.GreaterOrEqual(t, messageCaptureCount, 1)

	countBody := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"desktop count"}],"tools":[]}`)
	countRecorder := httptest.NewRecorder()
	countContext, _ := gin.CreateTestContext(countRecorder)
	countContext.Request = httptest.NewRequest(
		http.MethodPost, "/v1/messages/count_tokens?beta=true", bytes.NewReader(countBody),
	)
	countContext.Request.Header.Set("Content-Type", "application/json")
	countContext.Request.Header.Set("User-Agent", claudeFWGDesktopUserAgent)
	countContext.Set("api_key", &APIKey{ID: 23})
	countContext.Request = countContext.Request.WithContext(
		WithOfficialClaudeIngressRuntime(countContext.Request.Context(), countContext),
	)
	countParsed, err := ParseGatewayRequest(NewRequestBodyRef(countBody), PlatformAnthropic)
	require.NoError(t, err)
	require.NoError(
		t, svc.ForwardCountTokens(context.Background(), countContext, account, countParsed),
	)
	require.Equal(t, http.StatusOK, countRecorder.Code)
	require.JSONEq(t, `{"input_tokens":42}`, countRecorder.Body.String())

	countCapture := upstream.captures[len(upstream.captures)-1]
	require.Equal(t, "https://api.anthropic.com/v1/messages/count_tokens?beta=true", countCapture.url)
	require.Equal(t, "claude-cli/2.1.226 (external, cli)", countCapture.header.Get("User-Agent"))
}
