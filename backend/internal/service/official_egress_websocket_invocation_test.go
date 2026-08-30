package service

import (
	"context"
	"net/http"
	"sort"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

type officialCodexAcquireIdentityGuard struct {
	mu         sync.Mutex
	identities []officialegress.AttemptIdentity
}

type changeset5WebSocketHeaderCaptureDialer struct {
	url     string
	headers http.Header
}

func officialCodexWebSocketTestRoutingHint(t *testing.T) officialegress.CodexRoutingHintFacts {
	t.Helper()
	facts, err := officialegress.ParseOfficialCodexRoutingHintFacts(
		officialCodexEndpointResponsesWS,
		[]byte(`{"model":"gpt-5.6-luna"}`),
	)
	require.NoError(t, err)
	return facts
}

func (d *changeset5WebSocketHeaderCaptureDialer) Dial(
	_ context.Context,
	wsURL string,
	headers http.Header,
	_ string,
) (openAIWSClientConn, int, http.Header, error) {
	d.url = wsURL
	d.headers = headers.Clone()
	return &openAIWSFakeConn{}, http.StatusSwitchingProtocols, nil, nil
}

// TestChangeset5WebSocketExecutorOwnsFinalHandshakeHeaders 接替旧兼容 helper 的
// WS Header 证明：闭集、Host/入站宿主头清理、冻结身份和条件槽都在生产
// Executor + WS Acquire 链上验证，不再依赖无人调用的旧 finalizer。
func TestChangeset5WebSocketExecutorOwnsFinalHandshakeHeaders(t *testing.T) {
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(), nil, officialCodexExecutorID,
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	account := changeset3ProductionAccount(91)
	identity := changeset3ProductionInvocationIdentity(91)
	identity.ParentThreadID = "88888888-8888-8888-8888-888888888891"
	identity.Subagent = "guardian"
	invocation, err := newOfficialCodexWebSocketInvocation(
		context.Background(),
		officialCodexWebSocketInvocationInput{
			Runtime: runtimeState, Account: account,
			SinkID:       officialegress.SinkCodexResponsesWS,
			InvocationID: "changeset5-ws-header-authority",
			PolicyID:     "changeset5-ws-header", PolicySource: "changeset5-test",
			AttemptBudget: 1, IdentityFacts: identity,
		},
	)
	require.NoError(t, err)

	// invocation 创建后篡改来源对象，最终握手仍必须使用构造时冻结的账号和身份。
	account.Credentials["chatgpt_account_id"] = "mutated-account"
	identity.SessionID = "99999999-9999-9999-9999-999999999999"
	dialer := &changeset5WebSocketHeaderCaptureDialer{}
	request := openAIWSAcquireRequest{
		Account:     account,
		WSURL:       "wss://chatgpt.com/backend-api/codex/responses",
		RoutingHint: officialCodexWebSocketTestRoutingHint(t),
		Headers: http.Header{
			"Authorization":             []string{"Bearer changeset5-ws-token"},
			"Host":                      []string{"attacker.invalid"},
			"Session_id":                []string{"legacy-session"},
			"Conversation_id":           []string{"legacy-conversation"},
			"Accept-Language":           []string{"zh-CN"},
			"Sec-Fetch-Mode":            []string{"cors"},
			"X-Stainless-Helper-Method": []string{"stream"},
		},
	}
	session, status, _, err := invocation.DialDirect(
		context.Background(), dialer, request, officialCodexEndpointResponsesWS,
	)
	require.NoError(t, err)
	require.NotNil(t, session)
	require.Equal(t, http.StatusSwitchingProtocols, status)
	require.Equal(t, request.WSURL, dialer.url)

	allowed := []string{
		"authorization", "chatgpt-account-id", "openai-beta", "originator", "session-id",
		"thread-id", "user-agent", "version", "x-client-request-id",
		"x-codex-beta-features", "x-codex-parent-thread-id", "x-codex-turn-metadata",
		"x-codex-routing-hint", "x-codex-window-id", "x-openai-subagent",
	}
	var actual []string
	for name := range dialer.headers {
		actual = append(actual, http.CanonicalHeaderKey(name))
	}
	for index := range allowed {
		allowed[index] = http.CanonicalHeaderKey(allowed[index])
	}
	sort.Strings(actual)
	sort.Strings(allowed)
	require.Equal(t, allowed, actual, "Executor 必须拥有 WS Header 精确闭集")
	require.Equal(t, changeset3ProductionInvocationIdentity(91).SessionID, dialer.headers.Get("session-id"))
	require.Equal(t, changeset3ProductionAccount(91).GetChatGPTAccountID(), dialer.headers.Get("chatgpt-account-id"))
	require.Equal(t, "guardian", dialer.headers.Get("x-openai-subagent"))
	require.Equal(t, "88888888-8888-8888-8888-888888888891", dialer.headers.Get("x-codex-parent-thread-id"))
	require.Equal(t, "model=gpt-5.6-luna", dialer.headers.Get("x-codex-routing-hint"))
	require.Empty(t, dialer.headers.Get("Host"))
	require.Empty(t, dialer.headers.Get("session_id"))
	require.Empty(t, dialer.headers.Get("conversation_id"))
	requireNoInboundHostHeaders(t, dialer.headers)
}

func (g *officialCodexAcquireIdentityGuard) EvaluateConnectionAdmission(
	request *http.Request,
	_ officialegress.BackendKind,
	_ officialegress.WireProtocol,
) officialegress.GuardDecision {
	identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
	if ok {
		g.mu.Lock()
		g.identities = append(g.identities, identity)
		g.mu.Unlock()
	}
	return officialegress.GuardDecision{Allow: ok && identity.HasFinalizationToken}
}

func TestOfficialCodexWebSocketInvocationValidatesEveryPoolAcquire(t *testing.T) {
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(), nil, officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	account := &Account{
		ID: 731, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 2,
		Credentials: map[string]any{"chatgpt_account_id": "acct-ws-test"},
	}
	invocation, err := newOfficialCodexWebSocketInvocation(
		context.Background(),
		officialCodexWebSocketInvocationInput{
			Runtime: runtimeState, Account: account,
			SinkID:       officialegress.SinkCodexResponsesWS,
			InvocationID: "ws-acquire-invocation",
			PolicyID:     "ws-acquire", PolicySource: "test",
			FallbackSinkIDs: []officialegress.SinkID{
				officialegress.SinkCodexResponsesWSHTTPBridge,
			},
			AttemptBudget: 2,
		},
	)
	require.NoError(t, err)

	cfg := &config.Config{}
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	pool := newOpenAIWSConnPool(cfg)
	t.Cleanup(pool.Close)
	dialer := &openAIWSCountingDialer{}
	pool.setClientDialerForTest(dialer)
	guard := &officialCodexAcquireIdentityGuard{}
	pool.guard = guard
	request := openAIWSAcquireRequest{
		Account:     account,
		WSURL:       "wss://chatgpt.com/backend-api/codex/responses",
		RoutingHint: officialCodexWebSocketTestRoutingHint(t),
		Headers: http.Header{
			"Upgrade":            []string{"websocket"},
			"Authorization":      []string{"Bearer ws-test"},
			"Chatgpt-Account-Id": []string{"acct-ws-test"},
		},
	}

	first, err := invocation.AcquirePool(
		context.Background(), pool, request, officialCodexEndpointResponsesWS,
	)
	require.NoError(t, err)
	firstID := first.ConnID()
	first.Release()
	second, err := invocation.AcquirePool(
		context.Background(), pool, request, officialCodexEndpointResponsesWS,
	)
	require.NoError(t, err)
	require.True(t, second.Reused())
	require.Equal(t, firstID, second.ConnID())
	second.Release()

	require.Equal(t, 1, dialer.DialCount())
	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())
	guard.mu.Lock()
	require.GreaterOrEqual(t, len(guard.identities), 2)
	firstIdentity := guard.identities[0]
	lastIdentity := guard.identities[len(guard.identities)-1]
	guard.mu.Unlock()
	require.True(t, firstIdentity.HasFinalizationToken)
	require.True(t, lastIdentity.HasFinalizationToken)
	require.Equal(t, uint32(1), firstIdentity.AttemptOrdinal)
	require.Equal(t, uint32(2), lastIdentity.AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonInitial), firstIdentity.AttemptReason)
	require.Equal(t, string(officialegress.AttemptReasonReconnect), lastIdentity.AttemptReason)
}
