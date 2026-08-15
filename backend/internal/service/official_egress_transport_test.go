package service

import (
	"context"
	"net/http"
	"sync/atomic"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

func TestOfficialEgressT3_ThreeTransportProvidersAreIndependent(t *testing.T) {
	anthropicAccount := officialEgressTestAccount(50, PlatformAnthropic)
	anthropicContext := resolveOfficialEgressT3HTTPContext(
		t,
		anthropicAccount,
		"/v1/messages",
		"api.anthropic.com",
		"anthropic-ca",
	)
	anthropicSelection, err := resolveOfficialEgressTransportSelection(anthropicContext)
	require.NoError(t, err)
	require.Equal(t, officialEgressTransportProfileAnthropicHTTP, anthropicSelection.ProfileID)
	require.Len(t, anthropicSelection.TLSProfile.CipherSuites, 17)
	require.Equal(t, uint16(0x1301), anthropicSelection.TLSProfile.CipherSuites[0])
	require.Equal(t, []string{"http/1.1"}, anthropicSelection.TLSProfile.ALPNProtocols)

	openAIAccount := officialEgressTestAccount(94, PlatformOpenAI)
	openAIHTTPContext := resolveOfficialEgressT3HTTPContext(
		t,
		openAIAccount,
		"/v1/responses",
		"chatgpt.com",
		"openai-http-ca",
	)
	openAIHTTPSelection, err := resolveOfficialEgressTransportSelection(openAIHTTPContext)
	require.NoError(t, err)
	require.Equal(t, activeOpenAICodexTransportIDForTest(officialCodexEndpointResponsesHTTP), openAIHTTPSelection.ProfileID)
	require.Len(t, openAIHTTPSelection.TLSProfile.CipherSuites, 30)
	require.Equal(t, uint16(0x1302), openAIHTTPSelection.TLSProfile.CipherSuites[0])
	require.Empty(t, openAIHTTPSelection.TLSProfile.ALPNProtocols)
	require.NotContains(t, openAIHTTPSelection.TLSProfile.Extensions, uint16(16))

	openAIWSContext := resolveOfficialEgressT3WSContext(t, openAIAccount, "openai-ws-ca")
	openAIWSSelection, err := resolveOfficialEgressTransportSelection(openAIWSContext)
	require.NoError(t, err)
	require.Equal(t, activeOpenAICodexTransportIDForTest(officialCodexEndpointResponsesWS), openAIWSSelection.ProfileID)
	require.Len(t, openAIWSSelection.TLSProfile.CipherSuites, 10)
	require.Equal(t, uint16(0x1302), openAIWSSelection.TLSProfile.CipherSuites[0])
	require.Empty(t, openAIWSSelection.TLSProfile.ALPNProtocols)
	require.True(t, openAIWSSelection.TLSProfile.RandomizeExtensions)

	require.NotEqual(t, anthropicSelection.ProfileID, openAIHTTPSelection.ProfileID)
	require.NotEqual(t, openAIHTTPSelection.ProfileID, openAIWSSelection.ProfileID)
}

func TestOfficialEgressT3_WHAMUsesExactLongLivedTransport(t *testing.T) {
	account := officialEgressTestAccount(95, PlatformOpenAI)
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  account.Platform,
		InboundEndpoint: "/v1/responses",
		Transport:       OfficialEgressTransportHTTP,
		UpstreamHost:    "chatgpt.com",
		ProfileVersion:  officialEgressActiveVersionForTest(account),
		AccountType:     account.Type,
		CodexEndpointID: officialCodexEndpointWhamUsage,
		InvocationID:    "wham-long-lived-transport",
	})
	_, err := (DefaultOfficialEgressProfileResolver{}).ResolveHTTPProfile(
		egressContext,
		account,
		"/v1/responses",
	)
	require.NoError(t, err)
	wantTransport := activeOpenAICodexTransportIDForTest(officialCodexEndpointWhamUsage)
	require.Equal(t, wantTransport, egressContext.TransportProfileID())

	selection, err := resolveOfficialEgressTransportSelection(egressContext)
	require.NoError(t, err)
	require.Equal(t, wantTransport, selection.ProfileID)
	require.Len(t, selection.TLSProfile.CipherSuites, 30)
}

func TestOfficialEgressT3_HTTPTransportOverridesOnlyEnabledContext(t *testing.T) {
	fallback := &tlsfingerprint.Profile{Name: "fallback"}
	plainRequest, err := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	require.NoError(t, err)
	selected, enabled, err := resolveOfficialEgressHTTPTransportProfile(plainRequest, fallback)
	require.NoError(t, err)
	require.False(t, enabled)
	require.Same(t, fallback, selected)

	account := officialEgressTestAccount(94, PlatformOpenAI)
	egressContext := resolveOfficialEgressT3HTTPContext(
		t,
		account,
		"/v1/responses",
		"chatgpt.com",
		"",
	)
	officialRequest := plainRequest.WithContext(
		WithOfficialEgressContext(plainRequest.Context(), egressContext),
	)
	selected, enabled, err = resolveOfficialEgressHTTPTransportProfile(officialRequest, fallback)
	require.NoError(t, err)
	require.True(t, enabled)
	require.NotSame(t, fallback, selected)
	require.Len(t, selected.CipherSuites, 30)
}

func TestOfficialEgressT3_AnthropicUpstreamUsesResolvedTransport(t *testing.T) {
	account := officialEgressTestAccount(50, PlatformAnthropic)
	egressContext := resolveOfficialEgressT3HTTPContext(
		t,
		account,
		"/v1/responses",
		"api.anthropic.com",
		"",
	)
	request, err := http.NewRequest(
		http.MethodPost,
		"https://api.anthropic.com/v1/messages",
		nil,
	)
	require.NoError(t, err)
	request = request.WithContext(
		WithOfficialEgressContext(request.Context(), egressContext),
	)
	upstream := &officialEgressAnthropicUpstreamRecorder{}

	_, err = doAnthropicHTTPUpstreamWithOfficialEgress(
		upstream,
		request,
		"http://capture.example:18080",
		account,
		&tlsfingerprint.Profile{Name: "legacy"},
	)
	require.NoError(t, err)
	require.NotNil(t, upstream.profile)
	require.Len(t, upstream.profile.CipherSuites, 17)
	require.Equal(t, []string{"http/1.1"}, upstream.profile.ALPNProtocols)
}

func TestOfficialEgressT3_OpenAIHTTPProxyProfileMatchesCapturedClientBoundary(t *testing.T) {
	profile := newOpenAIOfficialEgressHTTPProxyTLSProfile()

	require.Len(t, profile.CipherSuites, 10)
	require.Equal(t, uint16(0x1302), profile.CipherSuites[0])
	require.Equal(t, []string{"h2", "http/1.1"}, profile.ALPNProtocols)
	require.Contains(t, profile.Extensions, uint16(16))
	require.True(t, profile.RandomizeExtensions)
	require.True(t, profile.Transport.DisableCompression)
}

func TestOfficialEgressT3_ConnectionPoolKeyIncludesTransportProfileAndCA(t *testing.T) {
	account := officialEgressTestAccount(94, PlatformOpenAI)
	first := resolveOfficialEgressT3HTTPContext(
		t,
		account,
		"/v1/responses",
		"chatgpt.com",
		"ca-a",
	)
	second := resolveOfficialEgressT3HTTPContext(
		t,
		account,
		"/v1/responses",
		"chatgpt.com",
		"ca-b",
	)
	require.NotEqual(t, first.ConnectionPoolID(), second.ConnectionPoolID())
	require.Contains(t, first.ConnectionPoolID(), "tls_profile="+activeOpenAICodexTransportIDForTest(officialCodexEndpointResponsesHTTP))
	require.Contains(t, first.ConnectionPoolID(), "ca=ca-a")
}

func TestOfficialEgressT3_WebSocketPoolDoesNotReuseAcrossCAProfiles(t *testing.T) {
	cfg := &configForOfficialEgressT3{}
	pool := newOpenAIWSConnPool(cfg.config())
	t.Cleanup(pool.Close)
	dialer := &officialEgressT3WSDialer{}
	pool.setClientDialerForTest(dialer)

	account := officialEgressTestAccount(94, PlatformOpenAI)
	firstContext := resolveOfficialEgressT3WSContext(t, account, "ca-a")
	secondContext := resolveOfficialEgressT3WSContext(t, account, "ca-b")
	request := openAIWSAcquireRequest{
		Account: account,
		WSURL:   "wss://chatgpt.com/backend-api/codex/responses",
		Headers: http.Header{},
	}

	firstLease, err := pool.Acquire(
		WithOfficialEgressContext(context.Background(), firstContext),
		request,
	)
	require.NoError(t, err)
	firstLease.Release()

	secondLease, err := pool.Acquire(
		WithOfficialEgressContext(context.Background(), secondContext),
		request,
	)
	require.NoError(t, err)
	secondLease.Release()
	require.Equal(t, int64(2), dialer.calls.Load())

	reusedLease, err := pool.Acquire(
		WithOfficialEgressContext(context.Background(), secondContext),
		request,
	)
	require.NoError(t, err)
	require.True(t, reusedLease.Reused())
	reusedLease.Release()
	require.Equal(t, int64(2), dialer.calls.Load())
}

func TestOfficialEgressT3_WebSocketTransportSupportsDirectHTTPAndSOCKS5(t *testing.T) {
	profile := newOpenAIOfficialEgressWebSocketTLSProfile()
	for _, rawProxyURL := range []string{
		"",
		"http://proxy.example:8080",
		"https://proxy.example:8443",
		"socks5://proxy.example:1080",
		"socks5h://proxy.example:1080",
	} {
		transport, err := buildOpenAIOfficialEgressWSTransport(profile, rawProxyURL)
		require.NoError(t, err, rawProxyURL)
		require.NotNil(t, transport.DialTLSContext, rawProxyURL)
		require.False(t, transport.ForceAttemptHTTP2, rawProxyURL)
	}

	_, err := buildOpenAIOfficialEgressWSTransport(profile, "ftp://proxy.example")
	require.ErrorContains(t, err, "unsupported")
}

func TestOfficialEgressT3_ProxyStateKeyDoesNotExposeCredentials(t *testing.T) {
	const proxyURL = "http://capture-user:capture-secret@proxy.example:8080"
	key := officialEgressProxyStateKey(proxyURL)
	require.NotEmpty(t, key)
	require.NotContains(t, key, "capture-user")
	require.NotContains(t, key, "capture-secret")
	require.NotEqual(t, proxyURL, key)
	require.Len(t, key, 64)
	require.Equal(
		t,
		officialEgressProxyStateKey("http://capture-user:capture-secret@PROXY.EXAMPLE:8080"),
		key,
		"代理 hostname 大小写不应产生不同资源身份",
	)
	require.NotEqual(
		t,
		officialEgressProxyStateKey("http://capture-user:other-secret@proxy.example:8080"),
		key,
		"代理认证材料变化必须隔离客户端资源",
	)
}

type officialEgressT3WSDialer struct {
	calls atomic.Int64
}

func (d *officialEgressT3WSDialer) Dial(
	context.Context,
	string,
	http.Header,
	string,
) (openAIWSClientConn, int, http.Header, error) {
	d.calls.Add(1)
	return &openAIWSFakeConn{}, 0, nil, nil
}

type officialEgressAnthropicUpstreamRecorder struct {
	profile *tlsfingerprint.Profile
}

func (r *officialEgressAnthropicUpstreamRecorder) Do(
	req *http.Request,
	proxyURL string,
	accountID int64,
	accountConcurrency int,
) (*http.Response, error) {
	return r.DoWithTLS(req, proxyURL, accountID, accountConcurrency, nil)
}

func (r *officialEgressAnthropicUpstreamRecorder) DoWithTLS(
	_ *http.Request,
	_ string,
	_ int64,
	_ int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	r.profile = profile
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       http.NoBody,
	}, nil
}

type configForOfficialEgressT3 struct{}

func (*configForOfficialEgressT3) config() *config.Config {
	cfg := &config.Config{}
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 4
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	return cfg
}

func resolveOfficialEgressT3HTTPContext(
	t *testing.T,
	account *Account,
	endpoint string,
	host string,
	caFingerprint string,
) *OfficialEgressContext {
	t.Helper()
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  account.Platform,
		InboundEndpoint: endpoint,
		Transport:       OfficialEgressTransportHTTP,
		UpstreamHost:    host,
		ProfileVersion:  officialEgressActiveVersionForTest(account),
		AccountType:     account.Type,
		CAFingerprint:   caFingerprint,
	})
	_, err := (DefaultOfficialEgressProfileResolver{}).ResolveHTTPProfile(
		egressContext,
		account,
		endpoint,
	)
	require.NoError(t, err)
	return egressContext
}

func resolveOfficialEgressT3WSContext(
	t *testing.T,
	account *Account,
	caFingerprint string,
) *OfficialEgressContext {
	t.Helper()
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  account.Platform,
		InboundEndpoint: "/v1/responses",
		Transport:       OfficialEgressTransportWebSocket,
		UpstreamHost:    "chatgpt.com",
		ProfileVersion:  officialEgressActiveVersionForTest(account),
		AccountType:     account.Type,
		CAFingerprint:   caFingerprint,
	})
	_, err := (DefaultOfficialEgressProfileResolver{}).ResolveWebSocketProfile(
		egressContext,
		account,
		"/v1/responses",
	)
	require.NoError(t, err)
	frozenContext, err := egressContext.Freeze()
	require.NoError(t, err)
	return frozenContext
}
