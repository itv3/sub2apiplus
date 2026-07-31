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
	execUserAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)

	for _, testCase := range []struct {
		name           string
		passthrough    bool
		userAgent      string
		originator     string
		wantUserAgent  string
		wantOriginator string
		wantSurface    string
		wantTerminal   string
		wantSuffix     bool
	}{
		{
			name:           "精确 TUI 普通转换",
			userAgent:      tuiUserAgent,
			originator:     "codex-tui",
			wantUserAgent:  tuiUserAgent,
			wantOriginator: "codex-tui",
			wantSurface:    officialCodexSurfaceTUI,
			wantTerminal:   "xterm-256color",
		},
		{
			name:           "精确 TUI passthrough 早退",
			passthrough:    true,
			userAgent:      tuiUserAgent,
			originator:     "codex-tui",
			wantUserAgent:  tuiUserAgent,
			wantOriginator: "codex-tui",
			wantSurface:    officialCodexSurfaceTUI,
			wantTerminal:   "xterm-256color",
		},
		{
			name:           "Desktop 后续版本投影默认画像",
			userAgent:      "Codex Desktop/0.146.0-alpha.3.1 (Mac OS 26.5.2; arm64) unknown (Codex Desktop; 26.721.81911)",
			originator:     "Codex Desktop",
			wantUserAgent:  execUserAgent,
			wantOriginator: "codex_exec",
			wantSurface:    officialCodexSurfaceExec,
			wantTerminal:   "unknown",
			wantSuffix:     true,
		},
		{
			name:           "VS Code 后续版本投影默认画像",
			passthrough:    true,
			userAgent:      "codex_vscode/0.146.0-alpha.3.1 (Ubuntu 24.4.0; aarch64) unknown (VS Code; 26.721.41059)",
			originator:     "codex_vscode",
			wantUserAgent:  execUserAgent,
			wantOriginator: "codex_exec",
			wantSurface:    officialCodexSurfaceExec,
			wantTerminal:   "unknown",
			wantSuffix:     true,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.4","instructions":"保持原样","stream":true,"input":[{"type":"message","role":"user","content":"hello"}]}`)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("content-type", "application/json")
			body = codexOfficialIngressIdentityForTest(t, c, body)
			c.Request.Header.Set("user-agent", testCase.userAgent)
			c.Request.Header.Set("originator", testCase.originator)

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
			require.Equal(t, testCase.wantUserAgent, upstream.lastReq.Header.Get("user-agent"))
			require.Equal(t, testCase.wantOriginator, upstream.lastReq.Header.Get("originator"))
			require.NoError(t, oauthClient.err)
			require.Equal(t, testCase.wantSurface, oauthClient.state.SurfaceID)
			require.Equal(t, testCase.wantOriginator, oauthClient.state.Originator)
			require.Equal(t, testCase.wantTerminal, oauthClient.state.TerminalToken)
			require.Equal(t, testCase.wantSuffix, oauthClient.state.UserAgentSuffixEnabled)
		})
	}
}
