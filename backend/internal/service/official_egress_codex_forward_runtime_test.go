package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodex0145ForwardBindsRuntimeBeforeOAuthRefresh(t *testing.T) {
	gin.SetMode(gin.TestMode)
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	tuiUserAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI,
		"xterm-256color",
		false,
	)
	require.NoError(t, err)

	for _, testCase := range []struct {
		name        string
		passthrough bool
	}{
		{name: "普通转换", passthrough: false},
		{name: "passthrough 早退", passthrough: true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.4","instructions":"保持原样","stream":true,"input":[{"type":"message","role":"user","content":"hello"}]}`)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("content-type", "application/json")
			body = codexOfficialIngressIdentityForTest(t, c, body)
			c.Request.Header.Set("user-agent", tuiUserAgent)
			c.Request.Header.Set("originator", "codex-tui")

			account := &Account{
				ID:          145,
				Name:        "codex-runtime-refresh",
				Platform:    PlatformOpenAI,
				Type:        AccountTypeOAuth,
				Concurrency: 1,
				Status:      StatusActive,
				Schedulable: true,
				Extra:       map[string]any{"openai_passthrough": testCase.passthrough},
				Credentials: map[string]any{
					"access_token":       "expired-access-token",
					"refresh_token":      "refresh-token",
					"expires_at":         time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
					"chatgpt_account_id": "chatgpt-account",
				},
			}
			repo := &officialCodexOAuthRuntimeRepo{account: account}
			oauthClient := &officialCodexOAuthRuntimeClient{}
			oauthService := NewOpenAIOAuthService(nil, oauthClient)
			defer oauthService.Stop()
			tokenProvider := NewOpenAITokenProvider(repo, nil, oauthService)
			tokenProvider.SetRefreshAPI(
				NewOAuthRefreshAPI(repo, nil),
				NewOpenAITokenRefresher(oauthService, repo),
			)
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK,
				Header: http.Header{
					"content-type": []string{"text/event-stream"},
					"x-request-id": []string{"runtime-refresh-request"},
				},
				Body: io.NopCloser(bytes.NewBufferString("data: [DONE]\n\n")),
			}}
			gateway := &OpenAIGatewayService{
				cfg:                 &config.Config{},
				accountRepo:         repo,
				httpUpstream:        upstream,
				openAITokenProvider: tokenProvider,
			}
			gateway.openaiModelCapabilities.replaceFromManifest(
				account.ID,
				[]byte(`{"models":[{"slug":"gpt-5.4","use_responses_lite":false,"supports_parallel_tool_calls":true}]}`),
			)

			result, forwardErr := gateway.Forward(context.Background(), c, account, body)
			require.NoError(t, forwardErr)
			require.NotNil(t, result)
			require.NotNil(t, upstream.lastReq)
			require.Equal(t, tuiUserAgent, upstream.lastReq.Header.Get("user-agent"))
			require.Equal(t, "codex-tui", upstream.lastReq.Header.Get("originator"))
			require.NoError(t, oauthClient.err)
			require.Equal(t, officialCodexSurfaceTUI, oauthClient.state.SurfaceID)
			require.Equal(t, "codex-tui", oauthClient.state.Originator)
			require.Equal(t, "xterm-256color", oauthClient.state.TerminalToken)
			require.False(t, oauthClient.state.UserAgentSuffixEnabled)
		})
	}
}
