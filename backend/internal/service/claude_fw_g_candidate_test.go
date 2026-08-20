package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

const claudeFWGServiceAccountUUID = "22222222-2222-4222-8222-222222222222"

type claudeFWGServiceCapture struct {
	method      string
	url         string
	header      http.Header
	body        []byte
	proxyURL    string
	accountID   int64
	concurrency int
	tlsProfile  *tlsfingerprint.Profile
}

type claudeFWGServiceUpstream struct {
	captures          []claudeFWGServiceCapture
	rejectFirstStream bool
	streamRejected    bool
}

func (u *claudeFWGServiceUpstream) Do(
	request *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
) (*http.Response, error) {
	return u.DoWithTLS(request, proxyURL, accountID, concurrency, nil)
}

func (u *claudeFWGServiceUpstream) DoWithTLS(
	request *http.Request,
	proxyURL string,
	accountID int64,
	concurrency int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	u.captures = append(u.captures, claudeFWGServiceCapture{
		method: request.Method, url: request.URL.String(), header: request.Header.Clone(),
		body: body, proxyURL: proxyURL, accountID: accountID,
		concurrency: concurrency, tlsProfile: profile,
	})
	status := http.StatusOK
	responseBody := `{}`
	switch request.URL.Path {
	case "/v1/messages":
		if u.rejectFirstStream && !u.streamRejected && bytes.Contains(body, []byte(`"stream":true`)) {
			u.streamRejected = true
			status = http.StatusNotFound
		} else {
			responseBody = `{"id":"msg_fwg","type":"message","role":"assistant","model":"claude-sonnet-5","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":3,"output_tokens":2}}`
		}
	case "/v1/messages/count_tokens":
		responseBody = `{"input_tokens":42}`
	case "/v1/oauth/token":
		responseBody = `{"access_token":"<secret-fresh-access-token>","token_type":"bearer","expires_in":3600,"refresh_token":"<secret-rotated-refresh-token>","scope":"user:inference"}`
	}
	return &http.Response{
		StatusCode: status,
		Header: http.Header{
			"Content-Type": []string{"application/json"},
			"Request-Id":   []string{"req_servicetest"},
		},
		Body:    io.NopCloser(strings.NewReader(responseBody)),
		Request: request,
	}, nil
}

type claudeFWGProxyRepoStub struct {
	ProxyRepository
	proxy *Proxy
	err   error
}

func (r *claudeFWGProxyRepoStub) GetByID(context.Context, int64) (*Proxy, error) {
	return r.proxy, r.err
}

func newClaudeFWGServiceRuntime(
	t *testing.T,
	upstream HTTPUpstream,
) (*OfficialEgressTransitionRuntime, *config.Config) {
	t.Helper()
	catalog, err := officialegress.ClaudeFWGCandidateSinkCatalog(
		officialegress.DefaultSinkCatalog(),
	)
	require.NoError(t, err)
	routes, err := officialegress.NewOfficialRouteCatalog(catalog)
	require.NoError(t, err)
	guard, err := officialegress.NewGuard(officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyEnforce),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyEnforce),
		CanaryPercent:          100,
	}, catalog, routes, nil)
	require.NoError(t, err)
	cfg := &config.Config{}
	cfg.Gateway.ClaudeFWGCandidateEnabled = true
	runtimeState, err := BuildOfficialEgressTransitionRuntime(guard, upstream, cfg, nil)
	require.NoError(t, err)
	require.NotNil(t, runtimeState.ClaudeCandidate)
	return runtimeState, cfg
}

func claudeFWGServiceAccount() *Account {
	return &Account{
		ID: 91, Platform: PlatformAnthropic, Type: AccountTypeOAuth, Concurrency: 4,
		Credentials: map[string]any{
			"access_token": "<secret-candidate-access-token>",
			"expires_at":   time.Now().Add(time.Hour).Unix(),
		},
		Extra: map[string]any{"account_uuid": claudeFWGServiceAccountUUID},
	}
}

func TestClaudeFWGServiceCandidateUsesStrictTransportContext(t *testing.T) {
	upstream := &claudeFWGServiceUpstream{}
	runtimeState, _ := newClaudeFWGServiceRuntime(t, upstream)
	require.Len(t, runtimeState.ClaudeCandidate.RuleIDs(), 40)

	trusted := officialegress.ClaudeTrustedFacts{
		Account: officialegress.ClaudeTrustedAccountFacts{
			AccountScope: "anthropic-oauth-account:91", AccountUUID: claudeFWGServiceAccountUUID,
		},
		Session: officialegress.ClaudeTrustedSessionFacts{
			SessionID: "33333333-3333-4333-8333-333333333333",
			Source:    officialegress.ClaudeSessionSourcePlannerDerived,
		},
		Entrypoint: officialegress.ClaudeTrustedEntrypointFacts{
			Entrypoint:       officialegress.ClaudeEntrypointSDKCLI,
			IngressProtocol:  "managed-internal",
			IngressBindingID: "test:91",
		},
		Features: officialegress.ClaudeTrustedFeatureFacts{
			SystemMode: officialegress.ClaudeSystemDefault,
		},
	}
	_, err := runtimeState.ClaudeCandidate.ExecuteEndpoint(
		context.Background(),
		officialegress.ClaudeEndpointExecution{
			EndpointKind: "lifecycle-hello", TrustedFacts: trusted,
			InvocationID: "44444444-4444-4444-8444-444444444444",
		},
	)
	require.ErrorContains(t, err, "缺少账号传输上下文")

	ctx := withClaudeCandidateHTTPTransport(context.Background(), "http://127.0.0.1:18080", 91, 4)
	response, err := runtimeState.ClaudeCandidate.ExecuteEndpoint(
		ctx,
		officialegress.ClaudeEndpointExecution{
			EndpointKind: "lifecycle-hello", TrustedFacts: trusted,
			InvocationID: "55555555-5555-4555-8555-555555555555",
		},
	)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	require.Len(t, upstream.captures, 1)
	capture := upstream.captures[0]
	require.Equal(t, http.MethodHead, capture.method)
	require.Equal(t, "http://127.0.0.1:18080", capture.proxyURL)
	require.Equal(t, int64(91), capture.accountID)
	require.Equal(t, 4, capture.concurrency)
	require.NotNil(t, capture.tlsProfile)
	require.Equal(t, []string{"http/1.1"}, capture.tlsProfile.ALPNProtocols)
}

func TestGatewayClaudeFWGStreamFallbackBridgesJSONToSSE(t *testing.T) {
	gin.SetMode(gin.TestMode)
	upstream := &claudeFWGServiceUpstream{rejectFirstStream: true}
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
	svc := &GatewayService{
		cfg: cfg, rateLimitService: &RateLimitService{},
		responseHeaderFilter: compileResponseHeaderFilter(cfg),
		claudeTokenProvider:  NewClaudeTokenProvider(nil, nil, nil),
		officialEgress:       runtimeState,
	}
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"tools":[],"stream":true}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("api_key", &APIKey{ID: 23})
	c.Request = c.Request.WithContext(
		WithOfficialClaudeIngressRuntime(c.Request.Context(), c),
	)
	parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), PlatformAnthropic)
	require.NoError(t, err)
	result, err := svc.Forward(context.Background(), c, claudeFWGServiceAccount(), parsed)
	require.NoError(t, err)
	require.True(t, result.Stream)
	require.Equal(t, 3, result.Usage.InputTokens)
	require.Equal(t, 2, result.Usage.OutputTokens)
	require.Equal(t, "text/event-stream", recorder.Header().Get("Content-Type"))
	require.Contains(t, recorder.Body.String(), "event: message_start")
	require.Contains(t, recorder.Body.String(), "event: content_block_delta")
	require.Contains(t, recorder.Body.String(), "event: message_stop")
	require.Len(t, upstream.captures, 5)
	last := upstream.captures[len(upstream.captures)-1]
	require.Contains(t, last.url, "/v1/messages?beta=true")
	require.NotContains(t, string(last.body), `"stream":true`)
	require.Equal(t, "Bearer <secret-candidate-access-token>", last.header.Get("Authorization"))
	require.Equal(t, int64(91), last.accountID)
	require.Equal(t, 4, last.concurrency)
}

func TestClaudeFWGServiceIngressSnapshotAndRouteAreClosed(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("Content-Encoding", "gzip")
	c.Request.Header.Set("X-Frozen", "before")
	ctx := WithOfficialClaudeIngressRuntime(context.Background(), c)
	c.Request.Header.Set("X-Frozen", "after")
	snapshot, ok := claudeFWGIngressSnapshotFromContext(ctx)
	require.True(t, ok)
	require.True(t, snapshot.Captured)
	require.True(t, snapshot.RequestGzip)
	require.Equal(t, "before", snapshot.Headers.Get("X-Frozen"))

	cfg := &config.Config{}
	cfg.Gateway.ClaudeFWGCandidateEnabled = true
	svc := &GatewayService{cfg: cfg}
	account := claudeFWGServiceAccount()
	require.True(t, svc.shouldRouteClaudeFWGCandidate(c, account))
	c.Request.URL.RawQuery = "beta=true"
	require.False(t, svc.shouldRouteClaudeFWGCandidate(c, account))
	c.Request.URL.RawQuery = ""
	account.Type = AccountTypeAPIKey
	require.False(t, svc.shouldRouteClaudeFWGCandidate(c, account))

	account = claudeFWGServiceAccount()
	c.Set("api_key", &APIKey{ID: 23})
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"tools":[],"stream":true}`)
	parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), PlatformAnthropic)
	require.NoError(t, err)
	parsed.SessionContext = &SessionContext{
		ClientIP: "192.0.2.1", UserAgent: "test-client/1.0", APIKeyID: 23,
	}
	trusted, err := buildClaudeFWGTrustedFacts(c, account, parsed)
	require.NoError(t, err)
	require.Equal(t, "api-key:23", trusted.Entrypoint.IngressBindingID)
	require.Equal(t, claudeFWGServiceAccountUUID, trusted.Account.AccountUUID)
	require.Equal(t, officialegress.ClaudeSessionSourcePlannerDerived, trusted.Session.Source)
	second, err := buildClaudeFWGTrustedFacts(c, account, parsed)
	require.NoError(t, err)
	require.Equal(t, trusted.Session.SessionID, second.Session.SessionID)
	differentBody := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"different first turn"}],"tools":[],"stream":true}`)
	differentParsed, err := ParseGatewayRequest(NewRequestBodyRef(differentBody), PlatformAnthropic)
	require.NoError(t, err)
	differentParsed.SessionContext = parsed.SessionContext
	different, err := buildClaudeFWGTrustedFacts(c, account, differentParsed)
	require.NoError(t, err)
	require.NotEqual(t, trusted.Session.SessionID, different.Session.SessionID)
}

func TestClaudeFWGServiceCountTokensUsesStrictCandidateRoute(t *testing.T) {
	gin.SetMode(gin.TestMode)
	upstream := &claudeFWGServiceUpstream{}
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, upstream)
	svc := &GatewayService{
		cfg: cfg, rateLimitService: &RateLimitService{},
		responseHeaderFilter: compileResponseHeaderFilter(cfg),
		claudeTokenProvider:  NewClaudeTokenProvider(nil, nil, nil),
		officialEgress:       runtimeState,
	}
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"count this"}],"tools":[]}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages/count_tokens", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("api_key", &APIKey{ID: 23})
	c.Request = c.Request.WithContext(
		WithOfficialClaudeIngressRuntime(c.Request.Context(), c),
	)
	parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), PlatformAnthropic)
	require.NoError(t, err)
	parsed.SessionContext = &SessionContext{
		ClientIP: "192.0.2.1", UserAgent: "test-client/1.0", APIKeyID: 23,
	}
	account := claudeFWGServiceAccount()
	require.True(t, svc.shouldRouteClaudeFWGCountTokens(c, account))
	require.NoError(t, svc.ForwardCountTokens(context.Background(), c, account, parsed))
	require.Equal(t, http.StatusOK, recorder.Code)
	require.JSONEq(t, `{"input_tokens":42}`, recorder.Body.String())
	require.Len(t, upstream.captures, 1)
	capture := upstream.captures[0]
	require.Contains(t, capture.url, "/v1/messages/count_tokens?beta=true")
	require.Equal(t, "Bearer <secret-candidate-access-token>", capture.header.Get("Authorization"))
	require.Equal(t, "claude-cli/2.1.226 (external, cli)", capture.header.Get("User-Agent"))
	require.Equal(t, string(body), string(capture.body))
	require.Equal(t, string(capture.body), string(parsed.Body.Bytes()))
	require.NotNil(t, capture.tlsProfile)
	require.Equal(t, []string{"http/1.1"}, capture.tlsProfile.ALPNProtocols)

	rejectedRecorder := httptest.NewRecorder()
	rejectedContext, _ := gin.CreateTestContext(rejectedRecorder)
	rejectedBody := []byte(`{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"count"}],"tools":[]}`)
	rejectedContext.Request = httptest.NewRequest(
		http.MethodPost, "/v1/messages/count_tokens", bytes.NewReader(rejectedBody),
	)
	rejectedContext.Set("api_key", &APIKey{ID: 23})
	rejectedParsed, err := ParseGatewayRequest(
		NewRequestBodyRef(rejectedBody), PlatformAnthropic,
	)
	require.NoError(t, err)
	err = svc.ForwardCountTokens(context.Background(), rejectedContext, account, rejectedParsed)
	require.ErrorContains(t, err, "model 不在 SupportEnvelope")
	require.Equal(t, http.StatusBadRequest, rejectedRecorder.Code)
	require.Len(t, upstream.captures, 1)

	c.Request.URL.RawQuery = "beta=true"
	require.False(t, svc.shouldRouteClaudeFWGCountTokens(c, account))
	c.Request.URL.RawQuery = ""
	account.Type = AccountTypeAPIKey
	require.False(t, svc.shouldRouteClaudeFWGCountTokens(c, account))
}

func TestClaudeFWGServicePlannerSessionCarriesPreviousResponse(t *testing.T) {
	gin.SetMode(gin.TestMode)
	upstream := &claudeFWGServiceUpstream{}
	runtimeState, _ := newClaudeFWGServiceRuntime(t, upstream)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"stable first turn"}],"tools":[],"stream":true}`)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
	c.Request.Header.Set("User-Agent", "test-client/1.0")
	c.Set("api_key", &APIKey{ID: 23})
	parsed, err := ParseGatewayRequest(NewRequestBodyRef(body), PlatformAnthropic)
	require.NoError(t, err)
	parsed.SessionContext = &SessionContext{
		ClientIP: "192.0.2.1", UserAgent: "test-client/1.0", APIKeyID: 23,
	}
	account := claudeFWGServiceAccount()
	trusted, err := buildClaudeFWGTrustedFacts(c, account, parsed)
	require.NoError(t, err)
	transportCtx := withClaudeCandidateHTTPTransport(
		context.Background(), "", account.ID, account.Concurrency,
	)

	first, err := runtimeState.ClaudeCandidate.ExecuteMessages(
		transportCtx,
		officialegress.ClaudeMessagesExecution{
			Body: body, AccessToken: "candidate-access-token", TrustedFacts: trusted,
			InvocationID: "44444444-4444-4444-8444-444444444444",
		},
	)
	require.NoError(t, err)
	require.NoError(t, first.Response.Body.Close())
	require.NoError(t, first.FinalizeSession(true))

	second, err := runtimeState.ClaudeCandidate.ExecuteMessages(
		transportCtx,
		officialegress.ClaudeMessagesExecution{
			Body: body, AccessToken: "candidate-access-token", TrustedFacts: trusted,
			InvocationID: "55555555-5555-4555-8555-555555555555",
		},
	)
	require.NoError(t, err)
	require.NoError(t, second.Response.Body.Close())
	require.NoError(t, second.FinalizeSession(true))

	messageBodies := make([][]byte, 0, 2)
	for _, capture := range upstream.captures {
		if strings.Contains(capture.url, "/v1/messages?beta=true") {
			messageBodies = append(messageBodies, capture.body)
		}
	}
	require.Len(t, messageBodies, 2)
	require.NotContains(t, string(messageBodies[0]), "cc_prev_req=")
	require.Contains(t, string(messageBodies[1]), "cc_prev_req=req_servicetest;")
}

func TestClaudeFWGServiceRefreshUsesStrictEndpoint(t *testing.T) {
	upstream := &claudeFWGServiceUpstream{}
	runtimeState, _ := newClaudeFWGServiceRuntime(t, upstream)
	refresher, err := newClaudeFWGTokenRefresher(
		&ClaudeTokenRefresher{}, runtimeState.ClaudeCandidate, &claudeFWGProxyRepoStub{},
	)
	require.NoError(t, err)
	account := claudeFWGServiceAccount()
	account.Credentials["refresh_token"] = "<secret-old-refresh-token>"
	account.Credentials["scope"] = "user:inference"
	credentials, err := refresher.Refresh(context.Background(), account)
	require.NoError(t, err)
	require.Equal(t, "<secret-fresh-access-token>", credentials["access_token"])
	require.Equal(t, "<secret-rotated-refresh-token>", credentials["refresh_token"])
	require.Len(t, upstream.captures, 1)
	capture := upstream.captures[0]
	require.Contains(t, capture.url, "platform.claude.com/v1/oauth/token")
	require.Empty(t, capture.header.Get("Authorization"))
	require.Equal(t, "axios/1.15.2", capture.header.Get("User-Agent"))
	require.Equal(t, `{"grant_type":"refresh_token","refresh_token":"<secret-old-refresh-token>","client_id":"9d1c250a-e61b-44d9-88ed-5944d1962f5e","scope":"user:inference"}`, string(capture.body))
	require.Equal(t, int64(91), capture.accountID)
	require.Equal(t, 4, capture.concurrency)

	proxyID := int64(7)
	account.ProxyID = &proxyID
	_, err = refresher.Refresh(context.Background(), account)
	require.ErrorContains(t, err, "配置的代理不存在")
}
