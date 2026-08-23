package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

type claudeFWGRefreshErrorUpstream struct {
	calls int
}

func (u *claudeFWGRefreshErrorUpstream) Do(
	request *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
) (*http.Response, error) {
	return u.DoWithTLS(request, proxyURL, accountID, concurrency, nil)
}

func (u *claudeFWGRefreshErrorUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
	_ *tlsfingerprint.Profile,
) (*http.Response, error) {
	u.calls++
	return &http.Response{
		StatusCode: http.StatusBadRequest,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body: io.NopCloser(strings.NewReader(
			`{"error":"invalid_grant","error_description":"Refresh token not found or invalid",` +
				`"refresh_token":"<secret-must-not-leak>"}`,
		)),
		Request: request,
	}, nil
}

func TestClaudeFWGServiceRefreshPreservesOAuthErrorCode(t *testing.T) {
	upstream := &claudeFWGRefreshErrorUpstream{}
	runtimeState, _ := newClaudeFWGServiceRuntime(t, upstream)
	refresher, err := newClaudeFWGTokenRefresher(
		&ClaudeTokenRefresher{}, runtimeState.ClaudeCandidate, &claudeFWGProxyRepoStub{},
	)
	require.NoError(t, err)
	account := claudeFWGServiceAccount()
	account.Credentials["refresh_token"] = "<secret-old-refresh-token>"
	account.Credentials["scope"] = "user:inference"

	_, err = refresher.Refresh(context.Background(), account)
	require.Error(t, err)
	require.ErrorContains(t, err, "invalid_grant")
	require.ErrorContains(t, err, "上游状态 400")
	require.NotContains(t, err.Error(), "<secret-must-not-leak>")
	require.NotContains(t, err.Error(), "<secret-old-refresh-token>")
	require.Equal(t, 1, upstream.calls)
}
