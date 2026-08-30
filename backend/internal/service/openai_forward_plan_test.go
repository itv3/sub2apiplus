package service

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"runtime"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

type openAIForwardPlanUpstream struct {
	mu       sync.Mutex
	requests []*http.Request
}

type openAIForwardPlanWebSocketAcquirer struct {
	mu         sync.Mutex
	identities []officialegress.AttemptIdentity
}

type openAIForwardPlanCaptureDialer struct {
	mu      sync.Mutex
	headers http.Header
}

func (d *openAIForwardPlanCaptureDialer) Dial(
	_ context.Context,
	_ string,
	headers http.Header,
	_ string,
) (openAIWSClientConn, int, http.Header, error) {
	d.mu.Lock()
	d.headers = cloneHeader(headers)
	d.mu.Unlock()
	return &openAIWSFakeConn{}, 0, nil, nil
}

func (d *openAIForwardPlanCaptureDialer) Headers() http.Header {
	d.mu.Lock()
	defer d.mu.Unlock()
	return cloneHeader(d.headers)
}

type openAIForwardPlanTerminalGuardUpstream struct {
	guard    *officialegress.Guard
	request  *http.Request
	decision officialegress.GuardDecision
}

func newOpenAIForwardPlanGuard(t *testing.T) *officialegress.Guard {
	t.Helper()
	guard, err := officialegress.NewGuard(
		officialegress.DefaultGuard().Config(),
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		nil,
	)
	require.NoError(t, err)
	return guard
}

func (u *openAIForwardPlanTerminalGuardUpstream) Do(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
) (*http.Response, error) {
	return u.evaluateHTTP(request)
}

func (u *openAIForwardPlanTerminalGuardUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
	_ *tlsfingerprint.Profile,
) (*http.Response, error) {
	return u.evaluateHTTP(request)
}

func (u *openAIForwardPlanTerminalGuardUpstream) evaluateHTTP(
	request *http.Request,
) (*http.Response, error) {
	// 生产 HTTPUpstream 在 terminal Guard 之前执行画像声明的全小写适配。
	// 测试替身必须复刻该顺序，才能验证真正的最终线而非适配前中间态。
	cloned := request.Clone(request.Context())
	lowered := make(http.Header, len(request.Header)+1)
	userAgent := ""
	for name, values := range request.Header {
		if strings.EqualFold(name, "User-Agent") {
			if len(values) > 0 && userAgent == "" {
				userAgent = values[0]
			}
			continue
		}
		lowered[strings.ToLower(name)] = append([]string(nil), values...)
	}
	lowered["User-Agent"] = []string{""}
	if userAgent != "" {
		lowered["user-agent"] = []string{userAgent}
	}
	cloned.Header = lowered
	request = cloned
	u.request = request
	u.decision = u.guard.Evaluate(
		request,
		officialegress.BackendHTTPUpstream,
		officialegress.WireProtocolHTTP,
	)
	if !u.decision.Allow {
		return nil, errors.New("Linux HTTP 最终线 Guard 拒绝请求")
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(bytes.NewReader(nil)),
		Request:    request,
	}, nil
}

func (a *openAIForwardPlanWebSocketAcquirer) AcquireOfficialCodexWebSocket(
	_ context.Context,
	prepared officialegress.PreparedRequest,
) (officialegress.WebSocketConnection, error) {
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
	if !ok || !identity.HasFinalizationToken {
		return nil, errors.New("测试 Acquire 缺少当前 attempt token")
	}
	a.mu.Lock()
	a.identities = append(a.identities, identity)
	a.mu.Unlock()
	return openAIForwardPlanWebSocketConnection{}, nil
}

type openAIForwardPlanWebSocketConnection struct{}

func (openAIForwardPlanWebSocketConnection) ReadMessage(context.Context) ([]byte, error) {
	return nil, errors.New("测试连接没有消息")
}

func (openAIForwardPlanWebSocketConnection) WriteMessage(context.Context, []byte) error {
	return nil
}

func (openAIForwardPlanWebSocketConnection) Close() error { return nil }

func (u *openAIForwardPlanUpstream) Do(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
) (*http.Response, error) {
	return u.respond(request)
}

func (u *openAIForwardPlanUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
	_ *tlsfingerprint.Profile,
) (*http.Response, error) {
	return u.respond(request)
}

func (u *openAIForwardPlanUpstream) respond(request *http.Request) (*http.Response, error) {
	cloned := request.Clone(request.Context())
	cloned.Header = request.Header.Clone()
	u.mu.Lock()
	u.requests = append(u.requests, cloned)
	u.mu.Unlock()
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(bytes.NewReader(nil)),
		Request:    request,
	}, nil
}

func (u *openAIForwardPlanUpstream) snapshot() []*http.Request {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]*http.Request(nil), u.requests...)
}

func TestOpenAIForwardInvocationPlanFreezesIdentityAndResolvesBundleOnce(t *testing.T) {
	upstream := &openAIForwardPlanUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		newOpenAIForwardPlanGuard(t),
		upstream,
		officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)

	account := &Account{
		ID: 711, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 4,
		Credentials: map[string]any{
			openAIAuthModeCredentialKey: "snapshot-mode",
			"chatgpt_account_id":        "synthetic-account",
		},
	}
	behavior := officialegress.BehaviorPolicy{
		ID: "forward-plan.behavior", Source: "test",
		Kind: officialegress.BehaviorUserRequest, AttemptBudget: 3,
	}
	plan, err := newOpenAIForwardInvocationPlan(context.Background(), openAIForwardInvocationPlanInput{
		Runtime: runtimeState, Account: account,
		PrimarySinkID: officialEgressSinkResponsesForward,
		InvocationID:  "forward-plan-invocation",
		IdentityMode:  officialegress.IdentityCodexOAuthStrict,
		HeaderPolicy:  officialegress.HeaderPolicy{ID: "forward-plan.headers", Source: "test"},
		ExecutionPolicy: officialegress.ExecutionPolicy{
			ID: "forward-plan.execution", Source: "test", MaxAttempts: 3,
			Replayable: true, ConcurrencyLimit: 1,
		},
		BehaviorPolicy: behavior,
		DeploymentPolicy: officialegress.DeploymentSupportPolicy{
			ID: "forward-plan.deployment", Source: "test", Platform: runtime.GOOS + "/" + runtime.GOARCH,
			ProxyMode: "direct", SupportedBackends: []officialegress.BackendKind{officialegress.BackendHTTPUpstream},
		},
	})
	require.NoError(t, err)
	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())
	require.Equal(t, "snapshot-mode", plan.AccountIdentity().AuthMode)

	// invocation 创建后修改账号对象，不得污染已经冻结的身份投影。
	account.Credentials[openAIAuthModeCredentialKey] = "mutated-mode"
	require.Equal(t, "snapshot-mode", plan.AccountIdentity().AuthMode)

	authentication := http.Header{"Authorization": []string{"Bearer attempt-one"}}
	first, err := plan.NewAttempt(openAIForwardPlanAttemptInput(
		officialegress.AttemptReasonInitial,
		authentication,
	))
	require.NoError(t, err)
	require.Equal(t, uint32(1), first.Ordinal())
	authentication.Set("Authorization", "Bearer mutated-after-attempt")

	_, err = plan.NewAttempt(openAIForwardPlanAttemptInput(
		officialegress.AttemptReasonRetry,
		http.Header{"Authorization": []string{"Bearer too-early"}},
	))
	require.ErrorContains(t, err, "尚未执行")

	result, err := plan.ExecuteAttempt(context.Background(), first)
	require.NoError(t, err)
	require.NotNil(t, result.HTTPResponse())
	_ = result.HTTPResponse().Body.Close()

	second, err := plan.NewAttempt(openAIForwardPlanAttemptInput(
		officialegress.AttemptReasonRetry,
		http.Header{"Authorization": []string{"Bearer attempt-two"}},
	))
	require.NoError(t, err)
	require.Equal(t, uint32(2), second.Ordinal())
	require.Equal(t, officialegress.AttemptReasonRetry, second.Reason())
	result, err = plan.ExecuteAttempt(context.Background(), second)
	require.NoError(t, err)
	_ = result.HTTPResponse().Body.Close()

	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount(),
		"retry 不得重新解析 ReleaseBundle")
	requests := upstream.snapshot()
	require.Len(t, requests, 2)
	require.Equal(t, "Bearer attempt-one", requests[0].Header.Get("Authorization"))
	require.Equal(t, "Bearer attempt-two", requests[1].Header.Get("Authorization"))

	_, err = plan.ExecuteAttempt(context.Background(), first)
	require.ErrorContains(t, err, "已消费或乱序")
}

func TestOpenAIForwardInvocationPlanRequiresExplicitWebSocketHTTPFallback(t *testing.T) {
	upstream := &openAIForwardPlanUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		newOpenAIForwardPlanGuard(t),
		upstream,
		officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	acquirer := &openAIForwardPlanWebSocketAcquirer{}
	require.NoError(t, runtimeState.BindCodexWebSocketAcquirer(acquirer))

	behavior := officialegress.BehaviorPolicy{
		ID: "forward-fallback.behavior", Source: "test",
		Kind: officialegress.BehaviorUserRequest,
		FallbackSinkIDs: []officialegress.SinkID{
			officialEgressSinkResponsesWSHTTPBridge,
		},
		AttemptBudget: 2,
	}
	plan, err := newOpenAIForwardInvocationPlan(context.Background(), openAIForwardInvocationPlanInput{
		Runtime: runtimeState,
		Account: &Account{
			ID: 712, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
			Credentials: map[string]any{
				openAIAuthModeCredentialKey: "oauth", "chatgpt_account_id": "acct-forward-fallback",
			},
		},
		PrimarySinkID: officialEgressSinkResponsesWS,
		InvocationID:  "forward-fallback-invocation",
		IdentityMode:  officialegress.IdentityCodexOAuthStrict,
		HeaderPolicy: officialegress.HeaderPolicy{
			ID: "forward-fallback.headers", Source: "test",
		},
		ExecutionPolicy: officialegress.ExecutionPolicy{
			ID: "forward-fallback.execution", Source: "test", MaxAttempts: 2,
			Replayable: true, ConcurrencyLimit: 1,
		},
		BehaviorPolicy: behavior,
		DeploymentPolicy: officialegress.DeploymentSupportPolicy{
			ID: "forward-fallback.deployment", Source: "test", Platform: runtime.GOOS + "/" + runtime.GOARCH,
			ProxyMode: "direct", SupportedBackends: []officialegress.BackendKind{
				officialegress.BackendHTTPUpstream,
				officialegress.BackendWebSocket,
			},
		},
	})
	require.NoError(t, err)
	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())

	// 生产 WS 路径在进入 Compiler 前由 buildOpenAIResponsesWSURL 把 https 转成 wss；
	// 实际提交 WS Executor 的 attempt target 必须使用 wss。
	wsTarget, err := http.NewRequest(
		http.MethodGet, strings.Replace(chatgptCodexURL, "https://", "wss://", 1), nil,
	)
	require.NoError(t, err)
	routingHint, err := officialegress.ParseOfficialCodexRoutingHintFacts(
		officialCodexEndpointResponsesWS,
		[]byte(`{"model":"gpt-5.5","service_tier":"priority"}`),
	)
	require.NoError(t, err)
	first, err := plan.NewAttempt(openAIForwardAttemptInput{
		Reason: officialegress.AttemptReasonInitial,
		SinkID: officialEgressSinkResponsesWS, EndpointID: officialCodexEndpointResponsesWS,
		Protocol: officialegress.WireProtocolWebSocket,
		Method:   http.MethodGet, URL: wsTarget.URL,
		Headers:        http.Header{"Upgrade": []string{"websocket"}},
		Authentication: http.Header{"Authorization": []string{"Bearer ws-attempt"}},
		Body:           officialegress.NewReplayableRequestBody(nil),
		RoutingHint:    routingHint,
	})
	require.NoError(t, err)
	result, err := plan.ExecuteAttempt(context.Background(), first)
	require.NoError(t, err)
	require.NotNil(t, result.WebSocket())
	require.NoError(t, result.WebSocket().Close())

	fallbacks := plan.Bundle().FallbackNodes()
	require.Len(t, fallbacks, 1)
	target := fallbacks[0]
	require.Equal(t, officialEgressSinkResponsesWSHTTPBridge, target.SinkID)
	fallbackInput := openAIForwardPlanAttemptInput(
		officialegress.AttemptReasonFallback,
		http.Header{"Authorization": []string{"Bearer http-fallback"}},
	)
	fallbackInput.SinkID = target.SinkID
	fallbackInput.EndpointID = target.EndpointID
	fallbackInput.Protocol = target.Protocol

	_, err = plan.NewAttempt(fallbackInput)
	require.ErrorIs(t, err, officialegress.ErrFallbackTransitionRequired)
	fallback, err := plan.TransitionFallbackAttempt(target, fallbackInput)
	require.NoError(t, err)
	require.Equal(t, uint32(2), fallback.Ordinal())
	require.Equal(t, officialegress.AttemptReasonFallback, fallback.Reason())
	result, err = plan.ExecuteAttempt(context.Background(), fallback)
	require.NoError(t, err)
	require.NotNil(t, result.HTTPResponse())
	_ = result.HTTPResponse().Body.Close()

	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount(),
		"WS 到 HTTP fallback 只能使用原 invocation 的 Bundle")
	acquirer.mu.Lock()
	require.Len(t, acquirer.identities, 1)
	require.Equal(t, uint32(1), acquirer.identities[0].AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonInitial), acquirer.identities[0].AttemptReason)
	acquirer.mu.Unlock()
	requests := upstream.snapshot()
	require.Len(t, requests, 1)
	identity, ok := officialegress.AttemptIdentityFromContext(requests[0].Context())
	require.True(t, ok)
	require.Equal(t, uint32(2), identity.AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonFallback), identity.AttemptReason)
}

func TestOpenAIForwardInvocationPlanAcquireThenHTTPFallbackUsesOneBundle(t *testing.T) {
	upstream := &openAIForwardPlanUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		newOpenAIForwardPlanGuard(t),
		upstream,
		officialCodexExecutorID,
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	account := &Account{
		ID: 713, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"chatgpt_account_id": "acct-forward"},
	}
	plan, err := newOfficialCodexResponseForwardPlan(
		context.Background(),
		officialCodexResponseForwardPlanInput{
			Runtime: runtimeState, Account: account,
			PrimarySinkID: officialegress.SinkCodexResponsesWS,
			InvocationID:  "forward-acquire-fallback-invocation",
			PolicyID:      "forward-acquire-fallback", PolicySource: "test",
			FallbackSinkIDs: []officialegress.SinkID{
				officialegress.SinkCodexResponsesWSHTTPBridge,
			},
			AttemptBudget: 3,
		},
	)
	require.NoError(t, err)

	cfg := &config.Config{}
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	pool := newOpenAIWSConnPool(cfg)
	t.Cleanup(pool.Close)
	dialer := &openAIForwardPlanCaptureDialer{}
	pool.setClientDialerForTest(dialer)
	routingHint, err := officialegress.ParseOfficialCodexRoutingHintFacts(
		officialCodexEndpointResponsesWS,
		[]byte(`{"model":"gpt-5.5","service_tier":"priority"}`),
	)
	require.NoError(t, err)
	lease, err := plan.AcquireWebSocketPool(
		context.Background(), pool,
		openAIWSAcquireRequest{
			Account: account,
			WSURL:   "wss://chatgpt.com/backend-api/codex/responses",
			Headers: http.Header{
				"Authorization":        []string{"Bearer ws-attempt"},
				"Chatgpt-Account-Id":   []string{"acct-forward"},
				"X-Codex-Routing-Hint": []string{"model=caller-spoof;tier=flex"},
			},
			RoutingHint: routingHint,
		},
		officialCodexEndpointResponsesWS,
	)
	require.NoError(t, err)
	lease.Release()
	require.Equal(t, "model=gpt-5.5;tier=priority", dialer.Headers().Get(openAICodexRoutingHintHeader))

	target, found := plan.FallbackNode(officialegress.SinkCodexResponsesWSHTTPBridge)
	require.True(t, found)
	request, err := http.NewRequest(
		http.MethodPost,
		chatgptCodexURL,
		bytes.NewBufferString(`{"model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`),
	)
	require.NoError(t, err)
	request.Header.Set("Authorization", "Bearer http-fallback")
	request.Header.Set("Chatgpt-Account-Id", "acct-forward")
	request.Header.Set("Content-Type", "application/json")
	response, err := plan.TransitionHTTPFallback(context.Background(), request, target)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())

	require.Equal(t, uint64(1), runtimeState.BundleResolver.ResolveCount())
	require.Equal(t, officialegress.SinkCodexResponsesWSHTTPBridge, plan.CurrentSinkID())
	requests := upstream.snapshot()
	require.Len(t, requests, 1)
	identity, ok := officialegress.AttemptIdentityFromContext(requests[0].Context())
	require.True(t, ok)
	require.Equal(t, uint32(2), identity.AttemptOrdinal)
	require.Equal(t, string(officialegress.AttemptReasonFallback), identity.AttemptReason)
}

func TestOpenAIForwardInvocationPlanLinuxLiteHTTPFallbackPassesTerminalGuard(t *testing.T) {
	guard := newOpenAIForwardPlanGuard(t)
	upstream := &openAIForwardPlanTerminalGuardUpstream{guard: guard}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard,
		upstream,
		officialCodexExecutorID,
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	require.NoError(t, runtimeState.BindCodexWebSocketAcquirer(&openAIForwardPlanWebSocketAcquirer{}))

	account := &Account{
		ID: 714, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"chatgpt_account_id": "acct-linux-lite"},
	}
	plan, err := newOpenAIForwardInvocationPlan(context.Background(), openAIForwardInvocationPlanInput{
		Runtime: runtimeState, Account: account,
		PrimarySinkID: officialEgressSinkResponsesWS,
		InvocationID:  "linux-lite-http-fallback",
		IdentityMode:  officialegress.IdentityCodexOAuthStrict,
		HeaderPolicy: officialegress.HeaderPolicy{
			ID: "linux-lite.headers", Source: "test",
		},
		ExecutionPolicy: officialegress.ExecutionPolicy{
			ID: "linux-lite.execution", Source: "test", MaxAttempts: 2,
			Replayable: true, ConcurrencyLimit: 1,
		},
		BehaviorPolicy: officialegress.BehaviorPolicy{
			ID: "linux-lite.behavior", Source: "test",
			Kind: officialegress.BehaviorUserRequest,
			FallbackSinkIDs: []officialegress.SinkID{
				officialEgressSinkResponsesWSHTTPBridge,
			},
			AttemptBudget: 2,
		},
		DeploymentPolicy: officialegress.DeploymentSupportPolicy{
			ID: "linux-lite.deployment", Source: "test", Platform: "linux/amd64",
			ProxyMode: "direct", SupportedBackends: []officialegress.BackendKind{
				officialegress.BackendHTTPUpstream,
				officialegress.BackendReqProfile,
				officialegress.BackendWebSocket,
			},
		},
	})
	require.NoError(t, err)

	wsTarget, err := http.NewRequest(http.MethodGet, "wss://chatgpt.com/backend-api/codex/responses", nil)
	require.NoError(t, err)
	routingHint, err := officialegress.ParseOfficialCodexRoutingHintFacts(
		officialCodexEndpointResponsesWS,
		[]byte(`{"model":"gpt-5.6-luna"}`),
	)
	require.NoError(t, err)
	first, err := plan.NewAttempt(openAIForwardAttemptInput{
		Reason: officialegress.AttemptReasonInitial,
		SinkID: officialEgressSinkResponsesWS, EndpointID: officialCodexEndpointResponsesWS,
		Protocol: officialegress.WireProtocolWebSocket,
		Method:   http.MethodGet, URL: wsTarget.URL,
		Headers:        make(http.Header),
		Authentication: http.Header{"Authorization": []string{"Bearer ws-token"}},
		Body:           officialegress.NewReplayableRequestBody(nil),
		RoutingHint:    routingHint,
	})
	require.NoError(t, err)
	result, err := plan.ExecuteAttempt(context.Background(), first)
	require.NoError(t, err)
	require.NoError(t, result.WebSocket().Close())

	target, found := plan.FallbackNode(officialEgressSinkResponsesWSHTTPBridge)
	require.True(t, found)
	body := `{"model":"gpt-5.6-luna","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`
	request, err := http.NewRequest(
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		bytes.NewBufferString(body),
	)
	require.NoError(t, err)
	request.Header.Set("Authorization", "Bearer http-token")
	request = request.WithContext(WithOfficialEgressContext(
		request.Context(),
		NewOfficialEgressContext(OfficialEgressContextInput{
			TargetPlatform: PlatformOpenAI,
			ResponsesLite:  true,
		}),
	))

	response, err := plan.TransitionHTTPFallback(request.Context(), request, target)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	require.True(t, upstream.decision.Allow, "%+v", upstream.decision)
	require.NotNil(t, upstream.request)
	headerValues := func(name string) []string {
		for rawName, values := range upstream.request.Header {
			if strings.EqualFold(rawName, name) {
				return values
			}
		}
		return nil
	}
	require.Equal(t, []string{"true"}, headerValues(responsesLiteHeaderKey))
	require.Equal(t, []string{"zstd"}, headerValues("content-encoding"))
}

func TestOpenAIForwardInvocationPlanMaterializesCookieBeforeFinalization(t *testing.T) {
	guard, err := officialegress.NewGuard(
		officialegress.DefaultGuard().Config(),
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		nil,
	)
	require.NoError(t, err)
	upstream := &openAIForwardPlanTerminalGuardUpstream{guard: guard}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard,
		upstream,
		officialCodexExecutorID,
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)

	account := &Account{
		ID: 715, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"chatgpt_account_id": "acct-cookie-finalization"},
	}
	jar, err := cookiejar.New(nil)
	require.NoError(t, err)
	target, err := url.Parse(chatgptCodexURL)
	require.NoError(t, err)
	jar.SetCookies(target, []*http.Cookie{{
		Name: "__cf_bm", Value: "cookie-before-finalize", Path: "/", Secure: true,
	}})
	ctx := WithHTTPUpstreamCookieJar(context.Background(), jar)
	plan, err := newOfficialCodexResponseForwardPlan(ctx, officialCodexResponseForwardPlanInput{
		Runtime: runtimeState, Account: account,
		PrimarySinkID: officialEgressSinkResponsesChatCompletions,
		InvocationID:  "cookie-before-finalization",
		PolicyID:      "cookie-finalization", PolicySource: "test",
		AttemptBudget: 1,
	})
	require.NoError(t, err)

	body := `{"model":"gpt-5.6-luna","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		chatgptCodexURL,
		bytes.NewBufferString(body),
	)
	require.NoError(t, err)
	request.Header.Set("Authorization", "Bearer cookie-finalization-token")
	response, err := plan.ExecuteHTTPRequest(ctx, request, officialCodexEndpointResponsesHTTP)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	require.True(t, upstream.decision.Allow, "%+v", upstream.decision)
	require.NotNil(t, upstream.request)

	var cookieValues []string
	for name, values := range upstream.request.Header {
		if strings.EqualFold(name, "cookie") {
			cookieValues = append(cookieValues, values...)
		}
	}
	require.Equal(t, []string{"__cf_bm=cookie-before-finalize"}, cookieValues)
}

func openAIForwardPlanAttemptInput(
	reason officialegress.AttemptReason,
	authentication http.Header,
) openAIForwardAttemptInput {
	target, _ := http.NewRequest(http.MethodPost, chatgptCodexURL, nil)
	return openAIForwardAttemptInput{
		Reason: reason, SinkID: officialEgressSinkResponsesForward,
		EndpointID: officialCodexEndpointResponsesHTTP,
		Protocol:   officialegress.WireProtocolHTTP,
		Method:     http.MethodPost,
		URL:        target.URL,
		Headers: http.Header{
			"Content-Type": []string{"application/json"},
			"Accept":       []string{"text/event-stream"},
		},
		Authentication: authentication,
		Body: officialegress.NewReplayableRequestBody([]byte(
			`{"model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
		)),
	}
}
