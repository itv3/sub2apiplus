package officialegress

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
)

type invocationTestWebSocketPort struct{}

func (invocationTestWebSocketPort) AcquireWebSocket(
	context.Context,
	PreparedRequest,
) (WebSocketConnection, error) {
	return invocationTestWebSocketConnection{}, nil
}

type invocationTestWebSocketConnection struct{}

func (invocationTestWebSocketConnection) ReadMessage(context.Context) ([]byte, error) {
	return nil, errors.New("测试连接没有消息")
}

func (invocationTestWebSocketConnection) WriteMessage(context.Context, []byte) error { return nil }
func (invocationTestWebSocketConnection) Close() error                               { return nil }

func TestExecutorInvocationEnforcesBehaviorAndTransportBudgets(t *testing.T) {
	t.Run("行为预算", func(t *testing.T) {
		executor, bundle, request := newExecutorInvocationTestFixture(t, 3, 2)
		invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
		if err != nil {
			t.Fatal(err)
		}
		first, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, request))
		if err != nil {
			t.Fatal(err)
		}
		assertInvocationAttemptIdentity(t, first, bundle, 1, AttemptReasonInitial)

		request.AttemptReason = AttemptReasonReconnect
		second, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, request))
		if err != nil {
			t.Fatal(err)
		}
		assertInvocationAttemptIdentity(t, second, bundle, 2, AttemptReasonReconnect)

		request.AttemptReason = AttemptReasonRetry
		if _, err := invocation.PrepareAttempt(context.Background(), request); !errors.Is(err, ErrBehaviorAttemptBudgetExceeded) {
			t.Fatalf("第三次 attempt 未被行为预算拒绝：%v", err)
		}
	})

	t.Run("传输预算", func(t *testing.T) {
		executor, bundle, request := newExecutorInvocationTestFixture(t, 1, 3)
		invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, request)); err != nil {
			t.Fatal(err)
		}
		request.AttemptReason = AttemptReasonRetry
		if _, err := invocation.PrepareAttempt(context.Background(), request); !errors.Is(err, ErrTransportAttemptBudgetExceeded) {
			t.Fatalf("第二次 attempt 未被传输预算拒绝：%v", err)
		}
	})
}

func TestExecutorInvocationRequiresFallbackTransitionCapability(t *testing.T) {
	executor, bundle, primary := newExecutorInvocationTestFixture(t, 3, 3)
	invocation, err := executor.BeginInvocation(context.Background(), bundle, primary.Plan.InvocationID)
	if err != nil {
		t.Fatal(err)
	}
	fallbacks := bundle.FallbackNodes()
	var fallbackTarget FallbackNode
	for _, candidate := range fallbacks {
		if candidate.SinkID == SinkCodexResponsesForward &&
			candidate.EndpointID == "responses_http" && candidate.Protocol == WireProtocolHTTP {
			fallbackTarget = candidate
			break
		}
	}
	if fallbackTarget.EndpointID == "" {
		t.Fatalf("测试 Bundle 缺少 Responses HTTP fallback：%v", fallbacks)
	}
	if err := invocation.TransitionFallback(fallbackTarget); err == nil {
		t.Fatal("首次 attempt 前错误接受了 fallback transition")
	}
	first, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, primary))
	if err != nil {
		t.Fatal(err)
	}
	assertInvocationAttemptIdentity(t, first, bundle, 1, AttemptReasonInitial)

	fallback := executorRequestForFallback(t, bundle, primary, fallbackTarget)
	if _, err := invocation.PrepareAttempt(context.Background(), fallback); !errors.Is(err, ErrFallbackTransitionRequired) {
		t.Fatalf("没有 transition capability 的 fallback 未被拒绝：%v", err)
	}
	invalid := fallbackTarget
	invalid.EndpointID = "not-in-bundle"
	if err := invocation.TransitionFallback(invalid); err == nil {
		t.Fatal("Bundle 外 fallback target 未被拒绝")
	}
	if err := invocation.TransitionFallback(fallbackTarget); err != nil {
		t.Fatal(err)
	}
	second, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, fallback))
	if err != nil {
		t.Fatal(err)
	}
	assertInvocationAttemptIdentity(t, second, bundle, 2, AttemptReasonFallback)

	fallback.AttemptReason = AttemptReasonRetry
	third, err := invocation.PrepareAttempt(context.Background(), freshExecutorInvocationRequest(t, fallback))
	if err != nil {
		t.Fatal(err)
	}
	assertInvocationAttemptIdentity(t, third, bundle, 3, AttemptReasonRetry)
}

func TestExecutorInvocationAssignsConcurrentOrdinalsOnce(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 3, 3)
	invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	ordinals := map[uint32]bool{}
	successes := 0
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			prepared, prepareErr := invocation.PrepareAttempt(
				context.Background(), freshExecutorInvocationRequest(t, request),
			)
			if prepareErr != nil {
				if !errors.Is(prepareErr, ErrBehaviorAttemptBudgetExceeded) &&
					!errors.Is(prepareErr, ErrTransportAttemptBudgetExceeded) {
					t.Errorf("并发 Prepare 返回意外错误：%v", prepareErr)
				}
				return
			}
			httpRequest, takeErr := prepared.TakeHTTPRequest()
			if takeErr != nil {
				t.Errorf("取得并发 PreparedRequest：%v", takeErr)
				return
			}
			identity, ok := AttemptIdentityFromContext(httpRequest.Context())
			if !ok {
				t.Error("并发 attempt 缺少身份")
				return
			}
			mu.Lock()
			successes++
			if ordinals[identity.AttemptOrdinal] {
				t.Errorf("AttemptOrdinal 被重复签发：%d", identity.AttemptOrdinal)
			}
			ordinals[identity.AttemptOrdinal] = true
			mu.Unlock()
		}()
	}
	wg.Wait()
	if successes != 3 || len(ordinals) != 3 || !ordinals[1] || !ordinals[2] || !ordinals[3] {
		t.Fatalf("并发 attempt 预算或序号非法：successes=%d ordinals=%v", successes, ordinals)
	}
}

func TestExecutorInvocationBindsIdentityPoliciesAndConsumesAttemptAuthenticationOnce(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 2, 2)
	authentication, err := NewAttemptAuthentication(AttemptAuthenticationInput{
		BearerToken: "synthetic-attempt-token",
	})
	if err != nil {
		t.Fatal(err)
	}
	request.Plan.Headers.Del("Authorization")
	request.Plan.Authentication = authentication
	request.Plan.BodyPolicy = BodyPolicy{ID: "identity-test-body", Source: "test"}
	invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
	if err != nil {
		t.Fatal(err)
	}
	prepared, err := invocation.PrepareAttempt(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	payload := prepared.Token().payload
	if payload.IdentityMode != IdentityCodexOAuthStrict ||
		payload.IdentityFactsDigest != request.Plan.IdentityFacts.Digest() ||
		payload.HeaderPolicyDigest != request.Plan.HeaderPolicy.Digest() ||
		payload.BodyPolicyDigest != request.Plan.BodyPolicy.Digest() ||
		payload.AttemptOrdinal != 1 || payload.AttemptReason != string(AttemptReasonInitial) {
		t.Fatalf("FinalizationToken 未绑定身份/策略/attempt 投影：%+v", payload)
	}
	request.AttemptReason = AttemptReasonRetry
	if _, err := invocation.PrepareAttempt(context.Background(), request); err == nil ||
		!strings.Contains(err.Error(), "AttemptAuthentication 已消费") {
		t.Fatalf("跨 attempt 复用认证材料未被拒绝：%v", err)
	}
}

func TestCodexIdentityFactsRejectConditionDrift(t *testing.T) {
	sessionID, err := NewCodexIdentityValue(
		"synthetic-session", IdentitySourceInvocation, IdentityLifecycleSession,
	)
	if err != nil {
		t.Fatal(err)
	}
	facts := CodexIdentityFacts{SessionID: sessionID}
	if err := facts.Validate(); err == nil {
		t.Fatal("SessionID 与 session_id_present 条件漂移未被拒绝")
	}
	facts.Conditions.SessionIDPresent = true
	if err := facts.Validate(); err != nil {
		t.Fatalf("一致的结构化身份事实被拒绝：%v", err)
	}
}

func TestCodexOAuthStrictRejectsCallerOwnedProtectedHeaders(t *testing.T) {
	protected := map[string]string{
		"Authorization":       "Bearer synthetic-attempt-token",
		"Host":                "chatgpt.com",
		"Content-Length":      "0",
		"Transfer-Encoding":   "chunked",
		"Connection":          "Upgrade",
		"Upgrade":             "websocket",
		"Content-Type":        "application/json",
		"Accept":              "text/event-stream",
		"Content-Encoding":    "zstd",
		"User-Agent":          "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)",
		"originator":          "codex_exec",
		"version":             "0.145.0",
		"OpenAI-Beta":         "responses_websockets=2026-02-06",
		"chatgpt-account-id":  "synthetic-account",
		"session-id":          "synthetic-session",
		"conversation-id":     "synthetic-conversation",
		"thread-id":           "synthetic-thread",
		"x-client-request-id": "synthetic-request",
		"x-codex-test":        "synthetic-value",
		"Sec-WebSocket-Key":   "synthetic-key",
	}
	for name, value := range protected {
		name, value := name, value
		t.Run("generic_"+strings.ToLower(name), func(t *testing.T) {
			executor, _, request := newExecutorInvocationTestFixture(t, 1, 1)
			request = freshExecutorInvocationRequest(t, request)
			request.Plan.Headers = make(http.Header)
			request.Plan.Headers.Set(name, value)
			if _, err := executor.Prepare(context.Background(), request); err == nil ||
				!strings.Contains(err.Error(), "普通 Headers 禁止保护头") {
				t.Fatalf("generic 保护头 %s 取得了 FinalizationToken：%v", name, err)
			}
		})
		t.Run("override_"+strings.ToLower(name), func(t *testing.T) {
			executor, _, request := newExecutorInvocationTestFixture(t, 1, 1)
			request = freshExecutorInvocationRequest(t, request)
			request.Plan.ResolvedHeaderOverrides = http.Header{name: []string{value}}
			if _, err := executor.Prepare(context.Background(), request); err == nil ||
				!strings.Contains(err.Error(), "Header Override 禁止保护头") {
				t.Fatalf("override 保护头 %s 取得了 FinalizationToken：%v", name, err)
			}
		})
	}
}

func TestCodexOAuthStrictRejectsWrongStructuredProcessIdentity(t *testing.T) {
	for name, mutate := range map[string]func(*CodexIdentityFacts){
		"surface": func(facts *CodexIdentityFacts) {
			facts.ProcessSurface, _ = NewCodexIdentityValue(
				"browser", IdentitySourceProcess, IdentityLifecycleProcess,
			)
		},
		"phase": func(facts *CodexIdentityFacts) {
			facts.ProcessPhase, _ = NewCodexIdentityValue(
				"caller_selected", IdentitySourceProcess, IdentityLifecycleProcess,
			)
		},
		"terminal": func(facts *CodexIdentityFacts) {
			facts.TerminalToken, _ = NewCodexIdentityValue(
				"invalid terminal", IdentitySourceProcess, IdentityLifecycleProcess,
			)
		},
	} {
		name, mutate := name, mutate
		t.Run(name, func(t *testing.T) {
			executor, _, request := newExecutorInvocationTestFixture(t, 1, 1)
			request = freshExecutorInvocationRequest(t, request)
			mutate(&request.Plan.IdentityFacts)
			if _, err := executor.Prepare(context.Background(), request); err == nil {
				t.Fatal("错误结构化身份取得了 FinalizationToken")
			}
		})
	}
}

func TestExecutorRejectsActivePreviousBundlePlanMix(t *testing.T) {
	executor, _, request := newExecutorInvocationTestFixture(t, 1, 1)
	request = freshExecutorInvocationRequest(t, request)
	request.Plan.Mode = ReleaseModePrevious
	if _, err := executor.Prepare(context.Background(), request); err == nil ||
		!strings.Contains(err.Error(), "一致的 ReleaseBundle") {
		t.Fatalf("active Bundle 与 previous Plan 混搭未被拒绝：%v", err)
	}
}

func TestExecutorWebSocketSessionRequiresPreparedOneShotFrame(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 2, 2)
	request = freshExecutorInvocationRequest(t, request)
	invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := invocation.ExecuteAttempt(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	session := result.WebSocket()
	if session == nil {
		t.Fatal("Executor 未返回受控 WebSocket session")
	}
	payload := []byte(`{"type":"response.create","model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`)
	frame, err := session.PrepareFrame(WebSocketFramePlan{
		Type: WebSocketFrameText, EventType: "response.create", Payload: payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if frame.Ordinal() != 1 || frame.EndpointID() != "responses_ws" ||
		frame.InvocationID() != request.Plan.InvocationID || frame.BodyDigest() == "" {
		t.Fatalf("PreparedWebSocketFrame 绑定投影不完整：%+v", frame)
	}
	if err := session.WritePreparedFrame(context.Background(), frame); err != nil {
		t.Fatal(err)
	}
	if err := session.WritePreparedFrame(context.Background(), frame); err == nil ||
		!strings.Contains(err.Error(), "禁止重放") {
		t.Fatalf("PreparedWebSocketFrame 重放未被拒绝：%v", err)
	}
}

func TestExecutorWebSocketSessionRejectsUncompiledOrMixedFrame(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 2, 2)
	request = freshExecutorInvocationRequest(t, request)
	invocation, err := executor.BeginInvocation(context.Background(), bundle, request.Plan.InvocationID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := invocation.ExecuteAttempt(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	session := result.WebSocket()
	protectedBody := []byte(`{"type":"response.create","model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[],"client_metadata":{"session_id":"caller-owned"}}`)
	if _, err := session.PrepareFrame(WebSocketFramePlan{
		Type: WebSocketFrameText, EventType: "response.create", Payload: protectedBody,
	}); err == nil || !strings.Contains(err.Error(), "compiler-owned client_metadata") {
		t.Fatalf("未经 Executor 所有权收敛的 WS 身份字段未被拒绝：%v", err)
	}
	if _, err := session.PrepareFrame(WebSocketFramePlan{
		Type: WebSocketFrameText, EventType: "session.update",
		Payload: []byte(`{"type":"session.update"}`),
	}); err == nil || !strings.Contains(err.Error(), "discriminator") {
		t.Fatalf("Bundle 外 WS event 未被拒绝：%v", err)
	}
	if _, err := session.PrepareFrame(WebSocketFramePlan{
		Type: WebSocketFrameBinary, EventType: "response.create", Payload: []byte("binary"),
	}); err == nil || !strings.Contains(err.Error(), "二进制") {
		t.Fatalf("未声明的透明二进制帧未被拒绝：%v", err)
	}
}

func newExecutorInvocationTestFixture(
	t *testing.T,
	maxAttempts int,
	attemptBudget int,
) (*Executor, ReleaseBundle, ExecutorRequest) {
	t.Helper()
	execution := ExecutionPolicy{
		ID: "invocation-test-execution", Source: "test",
		MaxAttempts: maxAttempts, Replayable: true, ConcurrencyLimit: 1,
	}
	deployment := DeploymentSupportPolicy{
		ID: "invocation-test-deployment", Source: "test", Platform: "test", ProxyMode: "direct",
		SupportedBackends: []BackendKind{BackendHTTPUpstream, BackendWebSocket},
	}
	behavior := BehaviorPolicy{
		ID: "invocation-test-behavior", Source: "test", Kind: BehaviorUserRequest,
		FallbackSinkIDs: []SinkID{SinkCodexResponsesForward}, AttemptBudget: attemptBudget,
	}
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := resolver.Resolve(BundleResolveRequest{
		SinkID: SinkCodexResponsesWS, Mode: ReleaseModeActive,
		Execution: execution, Deployment: deployment, Behavior: behavior,
	})
	if err != nil {
		t.Fatal(err)
	}
	httpAdapter, err := NewHTTPUpstreamTransportAdapter(&fakeHTTPUpstreamPort{})
	if err != nil {
		t.Fatal(err)
	}
	wsAdapter, err := NewWebSocketTransportAdapter(invocationTestWebSocketPort{})
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{
			{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream},
			{Backend: BackendWebSocket, Protocol: WireProtocolWebSocket, AdapterID: AdapterWebSocket},
		},
		[]TransportAdapter{httpAdapter, wsAdapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	executor, err := NewExecutor(ExecutorID(t.Name()), NewCompiler(), registry, DefaultGuard())
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := DefaultSinkCatalog().Resolve(SinkCodexResponsesWS)
	if !ok {
		t.Fatal("缺少 Responses WS binding")
	}
	target, err := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	if err != nil {
		t.Fatal(err)
	}
	request := ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: SinkCodexResponsesWS, Purpose: binding.Purpose(), EndpointID: "responses_ws",
			Mode: ReleaseModeActive, Protocol: WireProtocolWebSocket,
			Method: http.MethodGet, URL: target,
			IdentityMode:   IdentityCodexOAuthStrict,
			IdentityFacts:  executorInvocationIdentityFacts(t),
			HeaderPolicy:   HeaderPolicy{ID: "invocation-test-headers", Source: "test"},
			BodyPolicy:     BodyPolicy{ID: "invocation-test-body", Source: "test"},
			BehaviorPolicy: behavior, Body: NewReplayableRequestBody(nil),
			InvocationID: "invocation-test-id", DeclaredPersona: PersonaCodexCLI,
		},
		ExecutionScopeKey: "account:test",
	}
	return executor, bundle, request
}

func executorRequestForFallback(
	t *testing.T,
	bundle ReleaseBundle,
	primary ExecutorRequest,
	target FallbackNode,
) ExecutorRequest {
	t.Helper()
	binding, ok := DefaultSinkCatalog().Resolve(target.SinkID)
	if !ok {
		t.Fatalf("缺少 fallback binding：%s", target.SinkID)
	}
	request := primary
	request.Plan = primary.Plan.clone()
	request.Plan.SinkID = target.SinkID
	request.Plan.Purpose = binding.Purpose()
	request.Plan.EndpointID = target.EndpointID
	request.Plan.Protocol = target.Protocol
	request.Plan.Method = http.MethodPost
	request.Plan.Headers = nil
	request.Plan.Body = NewReplayableRequestBody([]byte(
		`{"model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
	))
	request.AttemptReason = AttemptReasonFallback
	request = freshExecutorInvocationRequest(t, request)
	return request
}

func freshExecutorInvocationRequest(t *testing.T, request ExecutorRequest) ExecutorRequest {
	t.Helper()
	authentication, err := NewAttemptAuthentication(AttemptAuthenticationInput{
		BearerToken: "synthetic-invocation-attempt-token",
	})
	if err != nil {
		t.Fatal(err)
	}
	request.Plan = request.Plan.clone()
	request.Plan.Authentication = authentication
	return request
}

func executorInvocationIdentityFacts(t *testing.T) CodexIdentityFacts {
	t.Helper()
	value := func(raw string, source IdentityFactSource, lifecycle IdentityFactLifecycle) CodexIdentityValue {
		fact, err := NewCodexIdentityValue(raw, source, lifecycle)
		if err != nil {
			t.Fatal(err)
		}
		return fact
	}
	return CodexIdentityFacts{
		AccountIdentityProjection: value("synthetic-local-account", IdentitySourceAccount, IdentityLifecycleAccount),
		ChatGPTAccountID:          value("synthetic-account", IdentitySourceAccount, IdentityLifecycleAccount),
		ProcessSurface:            value("exec", IdentitySourceProcess, IdentityLifecycleProcess),
		ProcessPhase:              value("initialized", IdentitySourceProcess, IdentityLifecycleProcess),
		TerminalToken:             value("unknown", IdentitySourceProcess, IdentityLifecycleProcess),
		UserAgentSuffixEnabled:    true,
		InstallationID:            value("synthetic-installation", IdentitySourceInvocation, IdentityLifecycleInvocation),
		SessionID:                 value("synthetic-session", IdentitySourceInvocation, IdentityLifecycleSession),
		ThreadID:                  value("synthetic-thread", IdentitySourceInvocation, IdentityLifecycleSession),
		WindowID:                  value("synthetic-window", IdentitySourceInvocation, IdentityLifecycleSession),
		ClientRequestID:           value("synthetic-request", IdentitySourceInvocation, IdentityLifecycleInvocation),
		TurnID:                    value("synthetic-turn", IdentitySourceTurn, IdentityLifecycleTurn),
		TurnMetadata:              value("synthetic-turn-metadata", IdentitySourceTurn, IdentityLifecycleTurn),
		Conditions:                CodexRequestConditions{SessionIDPresent: true},
	}
}

func assertInvocationAttemptIdentity(
	t *testing.T,
	prepared PreparedRequest,
	bundle ReleaseBundle,
	ordinal uint32,
	reason AttemptReason,
) {
	t.Helper()
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		t.Fatal(err)
	}
	identity, ok := AttemptIdentityFromContext(request.Context())
	if !ok || identity.BundleDigest != bundle.BundleDigest() ||
		identity.AttemptOrdinal != ordinal || identity.AttemptReason != string(reason) ||
		!identity.HasFinalizationToken {
		t.Fatalf("attempt 身份非法：%+v", identity)
	}
}
