package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

type oauthRefreshCaptureResource struct {
	request   *http.Request
	transport officialegress.TransportSpec
}

func (r *oauthRefreshCaptureResource) SendOfficialCodexReqProfile(
	_ context.Context,
	request *http.Request,
	transport officialegress.TransportSpec,
	_ string,
) (*http.Response, error) {
	r.request = request
	r.transport = transport
	return &http.Response{
		StatusCode: http.StatusOK,
		Body: io.NopCloser(strings.NewReader(
			`{"access_token":"access-test","refresh_token":"refresh-test","expires_in":3600}`,
		)),
	}, nil
}

func TestOAuthRefreshCompilesFromSingleReleaseBundle(t *testing.T) {
	resource := &oauthRefreshCaptureResource{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(), nil, officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive, resource,
	)
	require.NoError(t, err)
	svc := NewOpenAIOAuthService(nil, nil)
	svc.SetOfficialEgressRuntime(runtimeState)

	response, err := svc.executeOAuthRefresh(
		context.Background(), "refresh-secret", "client-id", "",
	)
	require.NoError(t, err)
	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())
	require.NotNil(t, response)
	_ = response.Body.Close()
	require.Equal(t, officialegress.BackendReqProfile, resource.transport.Backend)
	body, err := io.ReadAll(resource.request.Body)
	require.NoError(t, err)
	require.Equal(t, `{"client_id":"client-id","grant_type":"refresh_token","refresh_token":"refresh-secret"}`, string(body))
	require.NotEmpty(t, resource.request.Header.Get("User-Agent"))
	require.Equal(t, resource.request.Header.Get("Content-Type"), "application/json")
	identity, ok := officialegress.AttemptIdentityFromContext(resource.request.Context())
	require.True(t, ok)
	require.True(t, identity.HasFinalizationToken)
	require.Equal(t, uint32(1), identity.AttemptOrdinal)
}
