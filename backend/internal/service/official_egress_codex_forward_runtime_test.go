package service

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodexRuntimeSnapshotBindsBeforeRefreshCompilation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	userAgent, err := profile.RenderUserAgentWithTerminal(
		officialCodexSurfaceTUI, "xterm-256color", false,
	)
	require.NoError(t, err)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
	c.Request.Header.Set("user-agent", userAgent)
	c.Request.Header.Set("originator", "codex-tui")
	c.Request = c.Request.WithContext(WithOfficialCodexIngressRuntime(c.Request.Context(), c))
	account := &Account{
		ID: 145, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"access_token": "token"},
	}
	bound, err := bindOfficialCodexRuntimeStateFromIngress(
		c.Request.Context(), c, account, officialClientProfileModeActive,

		codexEndpointID(officialCodexEndpointResponsesHTTP))

	require.NoError(t, err)
	state, ok, err := officialCodexRuntimeStateFromContext(bound)
	require.NoError(t, err)
	require.True(t, ok)
	require.Equal(t, officialCodexSurfaceTUI, state.SurfaceID)
	require.Equal(t, "xterm-256color", state.TerminalToken)
}
