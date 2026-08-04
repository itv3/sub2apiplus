package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

type changeset1BExecutorUpstream struct {
	request     *http.Request
	concurrency int
}

func (u *changeset1BExecutorUpstream) Do(
	request *http.Request,
	_ string,
	_ int64,
	concurrency int,
) (*http.Response, error) {
	return u.respond(request, concurrency)
}

func (u *changeset1BExecutorUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	_ int64,
	concurrency int,
	_ *tlsfingerprint.Profile,
) (*http.Response, error) {
	return u.respond(request, concurrency)
}

func (u *changeset1BExecutorUpstream) respond(
	request *http.Request,
	concurrency int,
) (*http.Response, error) {
	u.request = request
	u.concurrency = concurrency
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(bytes.NewReader(nil)),
		Request:    request,
	}, nil
}

func TestOfficialCodexExecutorUsesFrozenPreviousModeAndPolicyConcurrency(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = officialClientProfileModePrevious
	mode := officialEgressReleaseModeFromConfig(cfg)
	require.Equal(t, officialegress.ReleaseModePrevious, mode)

	upstream := &changeset1BExecutorUpstream{}
	guard, err := officialegress.NewGuard(
		officialegress.DefaultGuard().Config(),
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		nil,
	)
	require.NoError(t, err)
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard,
		upstream,
		officialCodexExecutorID,
		mode,
	)
	require.NoError(t, err)

	request, err := http.NewRequestWithContext(
		context.Background(),
		http.MethodPost,
		chatgptCodexURL,
		bytes.NewBufferString(`{"model":"gpt-5","input":"hi","tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`),
	)
	require.NoError(t, err)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "text/event-stream")
	request.Header.Set("Authorization", "Bearer access-previous-test")
	account := &Account{
		ID:          991,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 9,
		Credentials: map[string]any{
			"access_token":       "access-previous-test",
			"chatgpt_account_id": "acct-previous-test",
		},
	}
	setOpenAIChatGPTAccountHeaders(request.Header, account)

	response, err := runtimeState.ExecuteCodexHTTP(context.Background(), OfficialCodexHTTPExecution{
		SinkID: officialEgressSinkUsageProbe, EndpointID: officialCodexEndpointResponsesHTTP,
		Account: account, Request: request,
		PolicyID: "changeset1b.previous-test.v1", PolicySource: "test",
		ConcurrencyLimit: 1,
	})
	require.NoError(t, err)
	require.NotNil(t, response)
	_ = response.Body.Close()

	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount(),
		"一次账号尝试只能解析一次 ReleaseBundle")
	require.NotNil(t, upstream.request)
	previous, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModePrevious,
	)
	require.NoError(t, err)
	require.Equal(t, previous.Build.UserAgent, upstream.request.Header.Get("User-Agent"))
	require.Equal(t, previous.Build.Originator, upstream.request.Header.Get("originator"))
	require.Equal(t, 1, upstream.concurrency,
		"transport 必须使用 ExecutionPolicy.ConcurrencyLimit，而不是账号并发值")
}
