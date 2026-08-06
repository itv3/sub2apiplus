package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

type liveHTTPUpstreamStub struct {
	request    *http.Request
	body       []byte
	tlsProfile *tlsfingerprint.Profile
}

type liveAttestationStub struct {
	header string
	err    error
}

func (s liveAttestationStub) Check(context.Context) error {
	return s.err
}

func (s liveAttestationStub) Generate(context.Context) (string, error) {
	return s.header, s.err
}

func (s *liveHTTPUpstreamStub) Do(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
) (*http.Response, error) {
	s.request = request
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	s.body = body
	return &http.Response{
		StatusCode: http.StatusOK,
		Header: http.Header{
			"Location": {"/backend-api/codex/call_test"},
		},
		Body: io.NopCloser(strings.NewReader("v=0\r\n")),
	}, nil
}

func (s *liveHTTPUpstreamStub) DoWithTLS(
	request *http.Request,
	proxyURL string,
	accountID int64,
	accountConcurrency int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	s.tlsProfile = profile
	return s.Do(request, proxyURL, accountID, accountConcurrency)
}

func TestLiveCapabilityOnlyAllowsOpenAIOAuth(t *testing.T) {
	require.True(t, (&Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}).SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive))
	require.False(t, (&Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey}).SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive))
	require.False(t, (&Account{Platform: PlatformGrok, Type: AccountTypeOAuth}).SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive))
	require.False(t, (&Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			openAIAuthModeCredentialKey: OpenAIAuthModePersonalAccessToken,
		},
	}).SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive))
	require.False(t, (&Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			openAIAuthModeCredentialKey: OpenAIAuthModeAgentIdentity,
		},
	}).SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive))
}

func TestValidateLiveCallRequestDoesNotRequireDelegation(t *testing.T) {
	request := &LiveCallRequest{
		SDP:     "v=0\r\n",
		Session: json.RawMessage(`{"model":"gpt-live-test","instructions":"hello"}`),
	}
	require.NoError(t, ValidateLiveCallRequest(request))
	require.NotContains(t, string(request.Session), "delegation")
}

func TestCreateUpstreamLiveCallPreservesSession(t *testing.T) {
	upstream := &liveHTTPUpstreamStub{}
	service := &OpenAIGatewayService{
		cfg:          &config.Config{},
		httpUpstream: upstream,
	}
	account := &Account{
		ID:          7,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 2,
		Credentials: map[string]any{
			"access_token":               "test-access-token",
			"chatgpt_account_id":         "acct_test",
			"chatgpt_account_is_fedramp": true,
		},
	}
	session := json.RawMessage(`{
		"model":"gpt-live-test",
		"delegation":{"type":"client"},
		"custom":{"keep":true}
	}`)

	realtimeSessionID := "019fb33c-a2b1-75c8-9782-d341262446cf"
	created, err := service.createUpstreamLiveCall(context.Background(), account, &LiveCallRequest{
		SDP:     "v=offer\r\n",
		Session: session,
	}, `{"v":1,"s":0,"t":"v1.test"}`, realtimeSessionID)
	require.NoError(t, err)
	require.Equal(t, "call_test", created.CallID)
	require.Equal(t, []byte("v=0\r\n"), created.SDP)

	var forwarded struct {
		SDP     string          `json:"sdp"`
		Session json.RawMessage `json:"session"`
	}
	require.NoError(t, json.Unmarshal(upstream.body, &forwarded))
	require.Equal(t, "v=offer\r\n", forwarded.SDP)
	require.JSONEq(t, string(session), string(forwarded.Session))
	require.Equal(t, "Bearer test-access-token", upstream.request.Header.Get("Authorization"))
	require.Equal(t, "acct_test", upstream.request.Header.Get("Chatgpt-Account-Id"))
	require.Equal(t, "true", upstream.request.Header.Get("X-OpenAI-FedRAMP"))
	require.Equal(t, http.MethodPost, upstream.request.Method)
	require.Equal(t, "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas", upstream.request.URL.String())
	require.Equal(t, "chatgpt.com", upstream.request.URL.Host)
	require.Equal(t, "*/*", upstream.request.Header.Get("Accept"))
	require.Equal(t, "quicksilver=v1", upstream.request.Header.Get("OpenAI-Alpha"))
	require.Equal(t, `{"v":1,"s":0,"t":"v1.test"}`, upstream.request.Header.Get(liveAttestationHeader))
	require.Equal(t, realtimeSessionID, upstream.request.Header.Get("X-Session-Id"))
	require.Empty(t, upstream.request.Header.Get("Session-Id"))
	require.Empty(t, upstream.request.Header.Get("Thread-Id"))
	require.Empty(t, upstream.request.Header.Get("OpenAI-Beta"))
	require.Empty(t, upstream.request.Header.Get("Host"))
	require.Empty(t, upstream.request.Header.Get("Content-Length"))
	require.ElementsMatch(t, []string{
		"version",
		"openai-alpha",
		"x-session-id",
		"x-oai-attestation",
		"authorization",
		"chatgpt-account-id",
		"x-openai-fedramp",
		"content-type",
		"accept",
		"originator",
		"user-agent",
	}, lowerLiveHeaderNames(upstream.request.Header))
	require.NotNil(t, upstream.tlsProfile)
	require.True(t, upstream.tlsProfile.Transport.StrictH1Wire)
	require.Contains(t, upstream.tlsProfile.Name, "Official Codex compiled")
	require.Equal(t, HTTPUpstreamProfileOpenAI, HTTPUpstreamProfileFromContext(upstream.request.Context()))
	require.True(t, HTTPUpstreamRedirectsDisabled(upstream.request.Context()))
	attempt, ok := officialegress.AttemptIdentityFromContext(upstream.request.Context())
	require.True(t, ok)
	require.Equal(t, officialegress.SinkCodexRealtimeCalls, attempt.SinkID)
	require.Equal(t, realtimeSessionID, attempt.InvocationID)
	require.Equal(t, officialegress.ReleaseModeActive, attempt.ReleaseMode)
	require.NotEmpty(t, attempt.ReleaseDigest)
	require.NotEmpty(t, attempt.BundleDigest)
	require.NotEmpty(t, attempt.ConnectionPoolDigest)
}

func TestLiveAttestationCipherRoundTripAndRejectsOtherInstanceKey(t *testing.T) {
	first := newLiveAttestationCipher(&config.Config{
		JWT: config.JWTConfig{Secret: "first-live-secret"},
	})
	second := newLiveAttestationCipher(&config.Config{
		JWT: config.JWTConfig{Secret: "second-live-secret"},
	})
	require.NotNil(t, first)
	require.NotNil(t, second)

	ciphertext, err := first.Encrypt(`{"v":1,"s":0,"t":"v1.opaque"}`)
	require.NoError(t, err)
	require.NotContains(t, ciphertext, "opaque")

	plaintext, err := first.Decrypt(ciphertext)
	require.NoError(t, err)
	require.Equal(t, `{"v":1,"s":0,"t":"v1.opaque"}`, plaintext)

	_, err = second.Decrypt(ciphertext)
	require.Error(t, err)
}

func TestPrepareLiveAttestationEncryptsHeaderAndReturnsExplicitProviderError(t *testing.T) {
	cipher := newLiveAttestationCipher(&config.Config{
		JWT: config.JWTConfig{Secret: "live-attestation-test-secret"},
	})
	service := &OpenAIGatewayService{
		liveAttestation:       liveAttestationStub{header: `{"v":1,"s":0,"t":"v1.test"}`},
		liveAttestationCipher: cipher,
	}
	header, ciphertext, err := service.prepareLiveAttestation(context.Background())
	require.NoError(t, err)
	require.Equal(t, `{"v":1,"s":0,"t":"v1.test"}`, header)
	require.NotContains(t, ciphertext, "v1.test")
	decrypted, err := cipher.Decrypt(ciphertext)
	require.NoError(t, err)
	require.Equal(t, header, decrypted)

	service.liveAttestation = liveAttestationStub{err: errors.New("macOS app missing")}
	_, _, err = service.prepareLiveAttestation(context.Background())
	var unavailable *LiveAttestationUnavailableError
	require.ErrorAs(t, err, &unavailable)
	require.Contains(t, unavailable.Error(), "macOS app missing")
}

func TestLiveMaxSessionDurationDefaultsAndOverrides(t *testing.T) {
	require.Equal(t, defaultLiveMaxSessionDuration, (&OpenAIGatewayService{}).liveMaxSessionDuration())
	require.Equal(
		t,
		90*time.Second,
		(&OpenAIGatewayService{cfg: &config.Config{
			Gateway: config.GatewayConfig{
				Live: config.GatewayLiveConfig{MaxSessionDurationSeconds: 90},
			},
		}}).liveMaxSessionDuration(),
	)
}

func TestLiveSidebandNormalCloseEndsCall(t *testing.T) {
	normalClose := coderws.CloseError{Code: coderws.StatusNormalClosure}
	require.ErrorIs(t, liveSidebandReadError(normalClose), ErrLiveCallNotFound)

	abnormalClose := coderws.CloseError{Code: coderws.StatusInternalError}
	require.Equal(t, abnormalClose, liveSidebandReadError(abnormalClose))
}

func TestLiveCreateFailoverUsesExistingOpenAIPolicy(t *testing.T) {
	service := &OpenAIGatewayService{}
	require.False(t, service.shouldFailoverLiveCreateError(&UpstreamFailoverError{
		StatusCode:   http.StatusBadRequest,
		ResponseBody: []byte(`{"error":{"message":"invalid session"}}`),
	}))
	require.True(t, service.shouldFailoverLiveCreateError(&UpstreamFailoverError{
		StatusCode: http.StatusForbidden,
	}))
	require.True(t, service.shouldFailoverLiveCreateError(&UpstreamFailoverError{
		StatusCode: http.StatusBadGateway,
	}))
	require.True(t, service.shouldFailoverLiveCreateError(errors.New("transport failed")))
}

func TestLiveCallIDFromLocation(t *testing.T) {
	callID, err := liveCallIDFromLocation("https://chatgpt.com/backend-api/codex/call_123?intent=quicksilver")
	require.NoError(t, err)
	require.Equal(t, "call_123", callID)

	callID, err = liveCallIDFromLocation("/backend-api/codex/call_456")
	require.NoError(t, err)
	require.Equal(t, "call_456", callID)
}

func TestLiveRealtimeSessionIDIsStableForLease(t *testing.T) {
	first := liveRealtimeSessionID("lease-stable")
	second := liveRealtimeSessionID("lease-stable")
	require.Equal(t, first, second)
	require.NotEqual(t, first, liveRealtimeSessionID("lease-other"))
}

func TestLiveSidebandUsesCodex0145EndpointAndClosedHeaders(t *testing.T) {
	target, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		officialCodexEndpointRealtimeSideband,
		officialCodexEndpointURLInput{
			QueryValues: map[string]string{"call_id": "call/value"},
		},
	)
	require.NoError(t, err)
	require.Equal(t, "wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=call%2Fvalue", target.String())

	account := &Account{
		ID:       27,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":               "sideband-token",
			"chatgpt_account_id":         "acct-sideband",
			"chatgpt_account_is_fedramp": true,
		},
	}
	cipher := newLiveAttestationCipher(&config.Config{
		JWT: config.JWTConfig{Secret: "live-sideband-headers-secret"},
	})
	record := &LiveCallRecord{CallID: "call/value", LeaseID: "lease-sideband"}
	record.AttestationCiphertext, err = cipher.Encrypt(`{"v":1,"s":0,"t":"v1.sideband"}`)
	require.NoError(t, err)
	service := &OpenAIGatewayService{
		accountRepo:           &liveTestAccountRepo{account: account},
		liveAttestationCipher: cipher,
	}
	realtimeSessionID := liveRealtimeSessionID("lease-sideband")
	headers, err := service.liveSidebandHeaders(context.Background(), account, nil, record)
	require.NoError(t, err)
	require.Empty(t, headers.Get("OpenAI-Alpha"), "画像常量只能由 Executor compiler 生成")
	require.Equal(t, realtimeSessionID, headers.Get("X-Session-Id"))
	require.Empty(t, headers.Get("Originator"), "进程身份只能由 Executor compiler 生成")
	require.Empty(t, headers.Get("User-Agent"), "进程身份只能由 Executor compiler 生成")
	require.Equal(t, "Bearer sideband-token", headers.Get("Authorization"))
	require.Equal(t, "acct-sideband", headers.Get("Chatgpt-Account-Id"))
	require.Equal(t, "true", headers.Get("X-OpenAI-FedRAMP"))
	require.Equal(t, `{"v":1,"s":0,"t":"v1.sideband"}`, headers.Get(liveAttestationHeader))
	require.Empty(t, headers.Get("Version"), "版本身份只能由 Executor compiler 生成")

	require.ElementsMatch(t, []string{
		"chatgpt-account-id",
		"x-openai-fedramp",
		"authorization",
		"x-session-id",
		"x-oai-attestation",
	}, lowerLiveHeaderNames(headers))
	require.Empty(t, headers.Get("Host"))
	require.Empty(t, headers.Get("Connection"))
	require.Empty(t, headers.Get("Upgrade"))
	require.Empty(t, headers.Get("Sec-WebSocket-Version"))
	require.Empty(t, headers.Get("Sec-WebSocket-Key"))
	require.Empty(t, headers.Get("Sec-WebSocket-Extensions"))
}

func TestLiveSidebandRestoresFrozenTUIRuntimeState(t *testing.T) {
	account := &Account{
		ID:       28,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":       "sideband-token",
			"chatgpt_account_id": "acct-sideband",
		},
		Extra: map[string]any{},
	}
	cipher := newLiveAttestationCipher(&config.Config{
		JWT: config.JWTConfig{Secret: "live-sideband-runtime-secret"},
	})
	record := &LiveCallRecord{
		CallID:  "call/runtime",
		LeaseID: "lease-runtime",
		CodexRuntimeState: LiveCodexRuntimeState{
			SurfaceID:              officialCodexSurfaceTUI,
			ProcessPhase:           officialCodexProcessPhaseInitialized,
			Originator:             "codex-tui",
			TerminalToken:          "xterm-256color",
			UserAgentSuffixEnabled: false,
			ConditionalHeaders: map[string]string{
				"x-openai-internal-codex-residency": "us",
			},
		},
	}
	var err error
	record.AttestationCiphertext, err = cipher.Encrypt(`{"v":1,"s":0,"t":"v1.sideband"}`)
	require.NoError(t, err)

	ctx, restored, err := restoreLiveCodexRuntimeState(
		context.Background(), account, record, officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.Equal(t, "us", restored.ConditionalHeaders["x-openai-internal-codex-residency"])
	service := &OpenAIGatewayService{
		accountRepo:           &liveTestAccountRepo{account: account},
		liveAttestationCipher: cipher,
	}
	headers, err := service.liveSidebandHeaders(ctx, account, nil, record)
	require.NoError(t, err)
	require.Empty(t, headers.Get("originator"), "TUI 身份只登记为结构化运行态")
	require.Empty(t, headers.Get("user-agent"), "TUI 身份只登记为结构化运行态")
	require.Empty(t, headers.Get("x-openai-internal-codex-residency"), "managed identity 只登记为结构化事实")
}

func lowerLiveHeaderNames(headers http.Header) []string {
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, strings.ToLower(name))
	}
	return names
}

func TestLiveSidebandDialDisablesWebSocketCompression(t *testing.T) {
	runtimeState, err := newOfficialEgressTestRuntime(&liveHTTPUpstreamStub{})
	require.NoError(t, err)
	account := &Account{
		ID: 891, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"chatgpt_account_id": "acct-live-compression"},
	}
	invocation, err := newOfficialCodexWebSocketInvocation(
		context.Background(),
		officialCodexWebSocketInvocationInput{
			Runtime: runtimeState, Account: account,
			SinkID:       officialegress.SinkCodexRealtimeSideband,
			InvocationID: "test-live-sideband-compression",
			PolicyID:     "test.realtime.sideband", PolicySource: "openai_live_test",
			AttemptBudget: 1,
		},
	)
	require.NoError(t, err)
	dialer := &liveCompiledTransportCaptureDialer{}
	connection, _, _, err := invocation.DialDirect(
		context.Background(), dialer,
		openAIWSAcquireRequest{
			Account: account,
			WSURL:   "wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=call_123",
			Headers: http.Header{"Authorization": {"Bearer token"}},
			// 与生产 dialLiveSideband 一致：call_id 的可信值来自 realtime call
			// 建立响应，作为受信动态事实随请求提交。
			ServerResponseQuery: map[string]string{"call_id": "call_123"},
		},
		officialCodexEndpointRealtimeSideband,
	)
	require.NoError(t, err)
	require.NoError(t, connection.Close())
	require.Empty(t, dialer.transport.Normalization.WebSocketCompressionOffer)
}

type liveCompiledTransportCaptureDialer struct {
	transport officialegress.TransportSpec
}

func (d *liveCompiledTransportCaptureDialer) Dial(
	ctx context.Context,
	_ string,
	_ http.Header,
	_ string,
) (openAIWSClientConn, int, http.Header, error) {
	compiled, ok := ctx.Value(officialCompiledWSTransportContextKey{}).(officialCompiledWSTransport)
	if !ok {
		return nil, 0, nil, errors.New("测试未收到终端 WebSocket transport")
	}
	d.transport = compiled.transport.Clone()
	return candidateTraceFailingWSConn{}, http.StatusSwitchingProtocols, nil, nil
}

func TestRequestTypeLive(t *testing.T) {
	require.True(t, RequestTypeLive.IsValid())
	require.Equal(t, "live", RequestTypeLive.String())
	parsed, err := ParseUsageRequestType("live")
	require.NoError(t, err)
	require.Equal(t, RequestTypeLive, parsed)
}
