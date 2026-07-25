package routes

import (
	"net/http"
	"net/http/httptest"
	"testing"

	servermiddleware "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestGatewayRoutesCodexModelsManifestPathIsRegistered(t *testing.T) {
	router := newGatewayRoutesTestRouter()

	registered := make(map[string]string)
	for _, route := range router.Routes() {
		if route.Method == http.MethodGet {
			registered[route.Path] = route.Handler
		}
	}

	require.NotEmpty(t, registered["/backend-api/codex/models"], "GET /backend-api/codex/models should be registered")
	require.NotEmpty(t, registered["/v1/models"], "GET /v1/models should be registered")
	require.NotEmpty(t, registered["/models"], "GET /models should be registered")
	require.Equal(t, registered["/v1/models"], registered["/models"], "root alias should use the same platform-aware handler")
}

func TestCodexModelsManifestRequestSupportsCompositeGroup(t *testing.T) {
	gin.SetMode(gin.TestMode)

	testCases := []struct {
		name          string
		platform      string
		clientVersion string
		want          bool
	}{
		{name: "OpenAI Codex 请求", platform: service.PlatformOpenAI, clientVersion: "0.145.0", want: true},
		{name: "组合分组 Codex 请求", platform: service.PlatformComposite, clientVersion: "0.145.0", want: true},
		{name: "组合分组普通模型列表", platform: service.PlatformComposite, want: false},
		{name: "Anthropic 分组不越权", platform: service.PlatformAnthropic, clientVersion: "0.145.0", want: false},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			target := "/v1/models"
			if testCase.clientVersion != "" {
				target += "?client_version=" + testCase.clientVersion
			}
			c.Request = httptest.NewRequest(http.MethodGet, target, nil)
			groupID := int64(8)
			c.Set(string(servermiddleware.ContextKeyAPIKey), &service.APIKey{
				GroupID: &groupID,
				Group:   &service.Group{ID: groupID, Platform: testCase.platform},
			})

			require.Equal(t, testCase.want, isCodexModelsManifestRequest(c))
		})
	}
}
