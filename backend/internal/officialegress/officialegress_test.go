package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

func TestEmbeddedSinkCatalogCurrentStateIsMachineProvable(t *testing.T) {
	catalog, err := LoadEmbeddedSinkCatalog()
	if err != nil {
		t.Fatalf("加载 Catalog 失败: %v", err)
	}
	bindings := catalog.Bindings()
	if len(bindings) != 34 {
		t.Fatalf("SinkID 数量=%d，期望 34", len(bindings))
	}

	var runtimeBindable, legacyReachable, canaryReachable, enforcedReachable, facades, pending, scopeExcluded int
	for _, binding := range bindings {
		if binding.RuntimeBindable() {
			runtimeBindable++
		} else if binding.Purpose() != Purpose("facade") &&
			binding.EnforcementState() != SinkStatePendingRemoval {
			scopeExcluded++
		}
		if binding.Purpose() == Purpose("facade") {
			facades++
			if binding.RuntimeBindable() {
				t.Fatalf("facade %s 不得成为运行时 key", binding.ID())
			}
		}
		if binding.EnforcementState() == SinkStatePendingRemoval {
			pending++
			if binding.Persona() != PersonaDeadCode {
				t.Fatalf("pending_removal %s 不是 dead-code", binding.ID())
			}
			continue
		}
		switch binding.EnforcementState() {
		case SinkStateLegacyObserve:
			legacyReachable++
			if binding.MigrationReceiptDigest() != "" {
				t.Fatalf("legacy Sink %s 不应已有定型证据", binding.ID())
			}
		case SinkStateCanaryEnforce:
			canaryReachable++
			if binding.MigrationReceiptDigest() == "" {
				t.Fatalf("canary Sink %s 缺少 MigrationReceipt", binding.ID())
			}
		case SinkStateEnforced:
			enforcedReachable++
			if binding.MigrationReceiptDigest() == "" {
				t.Fatalf("enforced Sink %s 缺少 MigrationReceipt", binding.ID())
			}
		default:
			t.Fatalf("可达 Sink %s 的状态=%s 不符合 1C 收据闭环", binding.ID(), binding.EnforcementState())
		}
	}
	if runtimeBindable != 27 || legacyReachable != 11 || canaryReachable != 0 || enforcedReachable != 21 ||
		facades != 3 || pending != 2 || scopeExcluded != 2 {
		t.Fatalf(
			"分类数量异常：runtime=%d legacy=%d canary=%d enforced=%d facade=%d pending=%d scope_excluded=%d",
			runtimeBindable, legacyReachable, canaryReachable, enforcedReachable, facades, pending, scopeExcluded,
		)
	}
}

func TestSinkCatalogRejectsUnsafeEnforcementAndFacadeBinding(t *testing.T) {
	base := testSinkBindingInput(SinkStateCanaryEnforce)
	base.migrationReceipt = nil
	if _, err := NewSinkCatalog([]SinkBindingInput{base}); err == nil {
		t.Fatal("无 MigrationReceipt 的 canary Sink 未被拒绝")
	}

	base = testSinkBindingInput(SinkStateCanaryEnforce)
	base.Persona = PersonaUnclassified
	if _, err := NewSinkCatalog([]SinkBindingInput{base}); err == nil {
		t.Fatal("unclassified persona 的 canary Sink 未被拒绝")
	}

	if _, err := DefaultSinkCatalog().BindContext(context.Background(), "codex.facade.upstream"); err == nil {
		t.Fatal("facade SinkID 被当作运行时 binding key")
	}
	for _, id := range []SinkID{"codex.admin_test.chat_completions", "codex.admin_test.keeper"} {
		if _, err := DefaultSinkCatalog().BindContext(context.Background(), id); err == nil {
			t.Fatalf("受审 scope-exclusion SinkID %s 被当作运行时 binding key", id)
		}
	}
	business, err := BindDefaultSink(context.Background(), "codex.responses.forward")
	if err != nil {
		t.Fatal(err)
	}
	_, err = WithAttemptMetadata(business, AttemptMetadataInput{
		SinkID: "codex.facade.upstream", Purpose: "facade", DeclaredPersona: PersonaCodexCLI,
	})
	if err == nil {
		t.Fatal("共享 facade 覆盖业务 SinkID 未被拒绝")
	}
	nested, err := StartDefaultSinkAttempt(business, SinkUnclassifiedAgentTaskRegister)
	if err != nil {
		t.Fatalf("业务调用边界无法开启嵌套 attempt: %v", err)
	}
	identity, ok := AttemptIdentityFromContext(nested)
	if !ok || identity.SinkID != SinkUnclassifiedAgentTaskRegister {
		t.Fatalf("嵌套 attempt binding 错误: %+v", identity)
	}
}

func TestCodexRoutesMapToTwoReleaseFamiliesWithoutInventingPurposes(t *testing.T) {
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	release, err := DefaultReleaseCatalog().Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	bindings := resolver.bindingsByProfile[release.ProfileDigest()].Bindings()
	purposes := map[string]struct{}{}
	endpoints := map[string]struct{}{}
	for _, binding := range bindings {
		purposes[binding.ReleasePurpose()] = struct{}{}
		endpoints[binding.EndpointID()] = struct{}{}
	}
	if len(purposes) != 2 {
		t.Fatalf("ReleaseGraph purpose 被扩成 %d 个: %#v", len(purposes), purposes)
	}
	if _, ok := purposes[RegistryPurposeOpenAIOAuthHTTP]; !ok {
		t.Fatal("缺少 HTTP registry purpose")
	}
	if _, ok := purposes[RegistryPurposeOpenAIOAuthWS]; !ok {
		t.Fatal("缺少 WS registry purpose")
	}
	if len(endpoints) != 16 {
		t.Fatalf("画像 endpoint 映射不唯一：%d", len(endpoints))
	}
}

func TestTransportOnlyOAuthExchangeCannotResolveEndpointOrRelease(t *testing.T) {
	binding, ok := DefaultSinkCatalog().Resolve(SinkCodexOAuthExchange)
	if !ok {
		t.Fatal("缺少 OAuth exchange SinkBinding")
	}
	target, err := url.Parse("https://auth.openai.com/oauth/token")
	if err != nil {
		t.Fatal(err)
	}
	resolved, ok := DefaultOfficialRouteCatalog().ResolveBinding(
		http.MethodPost, target, WireProtocolHTTP, binding,
	)
	if !ok {
		t.Fatal("OAuth exchange 的物理 route 未登记")
	}
	if resolved.EndpointID != "" || resolved.FamilyID != "" || resolved.RegistryPurpose != "" {
		t.Fatalf("transport_only exchange 获得了发布端点：%+v", resolved)
	}
	if _, err := DefaultOfficialRouteCatalog().ReleaseSelection(resolved); err == nil {
		t.Fatal("transport_only exchange 获得了 ReleaseSelection")
	}
}

func TestGuardUsesPhysicalRouteBeforeUntrustedPurpose(t *testing.T) {
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyObserve),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
	}, DefaultSinkCatalog(), DefaultOfficialRouteCatalog(), &captureGuardRecorder{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		SinkID: SinkCodexResponsesForward, Purpose: Purpose("oauth_refresh"),
		DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	request := testResponsesRequest(t, ctx)
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if decision.Allow || decision.RejectionReason != ReasonSinkBindingMismatch {
		t.Fatalf("错误 purpose 未由 UnregisteredSinkPolicy 拒绝：%+v", decision)
	}
	if slices.Contains(decision.Reasons, ReasonUnknownRoute) ||
		slices.Contains(decision.Reasons, ReasonUnknownRouteObserved) {
		t.Fatalf("已知物理 route 被错误降级为 unknown_route：%+v", decision)
	}
}

func TestConnectionAdmissionValidatesCurrentInvocationAndReceiptIdentity(t *testing.T) {
	input := SinkBindingInput{
		ID: SinkCodexResponsesWS, Purpose: "user_request.responses_ws",
		Persona: PersonaCodexCLI, EndpointEvidence: EndpointEvidenceCodexProfile,
		Routes: []CatalogRoute{{
			Key: RouteKey{Method: http.MethodGet, Host: "chatgpt.com",
				Path: "/backend-api/codex/responses", Purpose: "user_request.responses_ws"},
			Protocol: WireProtocolWebSocket,
		}},
		TargetBackend: BackendWebSocket, LegacyBackends: []BackendKind{BackendWebSocket},
		EnforcementState: SinkStateEnforced, Owner: "test", MigrationChangeset: "test",
		ExpiryCondition: "test", RuntimeBindable: true,
	}
	digest, err := sinkBindingIdentityDigest(input)
	if err != nil {
		t.Fatal(err)
	}
	input.migrationReceipt = &MigrationReceipt{
		digest: strings.Repeat("e", sha256.Size*2), sinkID: input.ID,
		approvedState: SinkStateEnforced, bindingDigest: digest,
		authorityKind: receiptcontract.AuthorityCodexExecutor,
		authorityID:   "ws-executor", tokenIssuerID: "ws-token-issuer",
		routeClaims: []migrationRouteClaim{{
			route: input.Routes[0], evidenceKind: "codex_endpoint", evidenceID: "responses_ws",
			backend: BackendWebSocket, adapterID: AdapterWebSocket, transportID: "ws-transport",
		}},
	}
	sinks, err := NewSinkCatalog([]SinkBindingInput{input})
	if err != nil {
		t.Fatal(err)
	}
	routes, err := NewOfficialRouteCatalog(sinks)
	if err != nil {
		t.Fatal(err)
	}
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce), CanaryPercent: 100,
	}, sinks, routes, &captureGuardRecorder{})
	if err != nil {
		t.Fatal(err)
	}
	issuer, err := newTokenIssuer("ws-token-issuer")
	if err != nil {
		t.Fatal(err)
	}
	if err := guard.registerIssuer(issuer); err != nil {
		t.Fatal(err)
	}

	buildRequest := func(withToken bool, metadataInvocation, tokenInvocation, transportID string) *http.Request {
		t.Helper()
		ctx, metadataErr := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
			SinkID: input.ID, Purpose: input.Purpose, DeclaredPersona: input.Persona,
			InvocationID: metadataInvocation, ExecutorID: "ws-executor",
			AttemptOrdinal: 1, AttemptReason: string(AttemptReasonAcquire),
			ProfileDigest: "ws-profile", ConnectionPoolDigest: "ws-pool",
		})
		if metadataErr != nil {
			t.Fatal(metadataErr)
		}
		if withToken {
			token := issuer.sign(tokenPayload{
				AuthorityID: "ws-executor", ReleaseDigest: "ws-profile", SinkID: input.ID, Route: input.Routes[0].Key,
				Persona: input.Persona, EndpointID: "responses_ws", TransportID: transportID,
				AdapterID: AdapterWebSocket, Backend: BackendWebSocket,
				Protocol: WireProtocolWebSocket, ConnectionPoolDigest: "ws-pool",
				InvocationID: tokenInvocation, AttemptOrdinal: 1,
				AttemptReason: string(AttemptReasonAcquire),
				Normalization: WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve},
			})
			ctx = withFinalizationToken(ctx, token)
			if metadataInvocation != tokenInvocation {
				metadata, _ := attemptMetadataFromContext(ctx)
				metadata.InvocationID = metadataInvocation
				ctx = context.WithValue(ctx, attemptMetadataContextKey{}, metadata)
			}
		}
		request, requestErr := http.NewRequestWithContext(
			ctx, http.MethodGet, "https://chatgpt.com/backend-api/codex/responses", nil,
		)
		if requestErr != nil {
			t.Fatal(requestErr)
		}
		return request
	}

	valid := guard.EvaluateConnectionAdmission(
		buildRequest(true, "invocation-1", "invocation-1", "ws-transport"),
		BackendWebSocket, WireProtocolWebSocket,
	)
	if !valid.Allow {
		t.Fatalf("有效当前 invocation admission 被拒绝：%+v", valid)
	}
	for name, request := range map[string]*http.Request{
		"missing token":    buildRequest(false, "invocation-2", "", "ws-transport"),
		"wrong invocation": buildRequest(true, "invocation-2", "invocation-old", "ws-transport"),
		"wrong transport":  buildRequest(true, "invocation-3", "invocation-3", "other-transport"),
	} {
		decision := guard.EvaluateConnectionAdmission(request, BackendWebSocket, WireProtocolWebSocket)
		if decision.Allow || decision.RejectionReason == "" {
			t.Fatalf("%s 未被 admission 拒绝：%+v", name, decision)
		}
	}

	preserved, err := sinks.PreserveAttemptContext(
		buildRequest(true, "invocation-4", "invocation-4", "ws-transport").Context(), input.ID,
	)
	if err != nil {
		t.Fatal(err)
	}
	identity, ok := AttemptIdentityFromContext(preserved)
	if !ok || !identity.HasFinalizationToken || identity.InvocationID != "invocation-4" {
		t.Fatalf("连接池保留 attempt 时清除了 Token：%+v", identity)
	}
}

func TestResponsesCatalogOnlyContainsReviewedExactSubpaths(t *testing.T) {
	binding, ok := DefaultSinkCatalog().Resolve(SinkCodexResponsesForward)
	if !ok {
		t.Fatal("缺少 Responses forward SinkBinding")
	}
	for _, path := range []string{
		"/backend-api/codex/responses",
		"/backend-api/codex/responses/compact",
	} {
		target := &url.URL{Scheme: "https", Host: "chatgpt.com", Path: path}
		resolved, ok := DefaultOfficialRouteCatalog().ResolveBinding(
			http.MethodPost, target, WireProtocolHTTP, binding,
		)
		if !ok || resolved.EndpointID == "" {
			t.Fatalf("受审 Responses route 未解析：path=%s route=%+v", path, resolved)
		}
	}
	unsupported := &url.URL{
		Scheme: "https", Host: "chatgpt.com", Path: "/backend-api/codex/responses/resp_1/cancel",
	}
	if _, ok := DefaultOfficialRouteCatalog().MatchPhysical(
		http.MethodPost, unsupported, WireProtocolHTTP,
	); ok {
		t.Fatal("未举证 Responses 资源子路径进入了受控 route 闭集")
	}
}

func TestEgressScopeStageZeroSeparatesThirdPartyTraffic(t *testing.T) {
	recorder := &captureGuardRecorder{}
	guard := newTestDefaultGuard(t, recorder)
	tests := []string{
		"https://api.anthropic.com/v1/messages?token=secret",
		"https://generativelanguage.googleapis.com/v1beta/models/gemini",
		"https://api.stripe.com/v1/payment_intents",
		"https://www.google.com/search?q=private",
		"https://api.openai.com/v1/responses",
	}
	for _, target := range tests {
		req, err := http.NewRequest(http.MethodPost, target, strings.NewReader("private-body"))
		if err != nil {
			t.Fatal(err)
		}
		decision := guard.Evaluate(req, BackendHTTPUpstream, WireProtocolHTTP)
		if !decision.Allow || decision.Scope != EgressScopeOutOfScope ||
			!slices.Contains(decision.Reasons, ReasonOutOfScopePassthrough) ||
			slices.Contains(decision.Reasons, ReasonUnknownRoute) {
			t.Fatalf("第三方请求被误判：target=%s decision=%+v", target, decision)
		}
	}

	managedContext, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		SinkID: "new.codex.sink", Purpose: "user_request.responses", DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	managed, _ := http.NewRequestWithContext(
		managedContext,
		http.MethodPost,
		"https://api.openai.com/v1/responses",
		nil,
	)
	decision := guard.Evaluate(managed, BackendHTTPUpstream, WireProtocolHTTP)
	if decision.Scope != EgressScopeManaged || !slices.Contains(decision.Reasons, ReasonUnknownRoute) {
		t.Fatalf("带受控 purpose 的 api.openai.com 请求未进入 managed unknown_route: %+v", decision)
	}
}

func TestGuardedTransportPassesThirdPartyProvidersByteForByteWithoutUnknownRoute(t *testing.T) {
	tests := []struct {
		name   string
		target string
	}{
		{"Anthropic", "https://api.anthropic.com/v1/messages?trace=private"},
		{"Gemini", "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent"},
		{"支付", "https://api.stripe.com/v1/payment_intents"},
		{"websearch", "https://api.tavily.com/search?q=private"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recorder := &captureGuardRecorder{}
			guard := newTestDefaultGuard(t, recorder)
			base := guardRoundTripFunc(func(request *http.Request) (*http.Response, error) {
				body, err := io.ReadAll(request.Body)
				if err != nil {
					t.Fatal(err)
				}
				if request.Method != http.MethodPost || request.URL.String() != test.target ||
					string(body) != "exact-body" || request.Header.Get("X-Exact") != "exact-header" {
					t.Fatalf("第三方请求被 Guard 改写：%s %s %q %#v",
						request.Method, request.URL, body, request.Header)
				}
				return &http.Response{
					StatusCode: http.StatusCreated,
					Header:     http.Header{"X-Exact-Result": []string{"same"}},
					Body:       io.NopCloser(strings.NewReader("exact-response")),
					Request:    request,
				}, nil
			})
			transport := NewGuardedRoundTripper(base, guard, BackendHTTPUpstream, WireProtocolHTTP)
			request, err := http.NewRequest(http.MethodPost, test.target, strings.NewReader("exact-body"))
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("X-Exact", "exact-header")
			response, err := transport.RoundTrip(request)
			if err != nil {
				t.Fatal(err)
			}
			responseBody, err := io.ReadAll(response.Body)
			if err != nil {
				t.Fatal(err)
			}
			_ = response.Body.Close()
			if response.StatusCode != http.StatusCreated || response.Header.Get("X-Exact-Result") != "same" ||
				string(responseBody) != "exact-response" {
				t.Fatalf("第三方响应结果被 Guard 改写：%+v %q", response, responseBody)
			}
			if len(recorder.events) != 1 || recorder.events[0].Reason != ReasonOutOfScopePassthrough {
				t.Fatalf("第三方发送产生非 scope-0 事件：%+v", recorder.events)
			}
		})
	}
}

func TestObservePoliciesAndEnforcedSinkRuntimeRejection(t *testing.T) {
	recorder := &captureGuardRecorder{}
	guard := newTestDefaultGuard(t, recorder)

	unknown, _ := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api/codex/new-endpoint", nil)
	assertGuardReasons(t, guard.Evaluate(unknown, BackendHTTPUpstream, WireProtocolHTTP),
		ReasonUnknownRoute, ReasonUnknownRouteObserved)

	missingContext, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		Purpose: "user_request.responses", DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	missing := testResponsesRequest(t, missingContext)
	assertGuardReasons(t, guard.Evaluate(missing, BackendHTTPUpstream, WireProtocolHTTP),
		ReasonMissingSinkID, ReasonUnregisteredSinkObserved)

	unregisteredContext, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		SinkID: "codex.not.registered", Purpose: "user_request.responses", DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	unregistered := testResponsesRequest(t, unregisteredContext)
	assertGuardReasons(t, guard.Evaluate(unregistered, BackendHTTPUpstream, WireProtocolHTTP),
		ReasonUnregisteredSink, ReasonUnregisteredSinkObserved)

	boundContext, err := BindDefaultSink(context.Background(), "codex.responses.forward")
	if err != nil {
		t.Fatal(err)
	}
	wrongBackend := testResponsesRequest(t, boundContext)
	assertGuardReasons(t, guard.Evaluate(wrongBackend, BackendReqProfile, WireProtocolHTTP),
		ReasonWrongBackend, ReasonSinkBindingMismatch, ReasonUnregisteredSinkObserved)

	enforced := testResponsesRequest(t, boundContext)
	enforcedDecision := guard.Evaluate(enforced, BackendHTTPUpstream, WireProtocolHTTP)
	if enforcedDecision.Allow || enforcedDecision.SinkState != SinkStateEnforced ||
		enforcedDecision.RejectionReason != ReasonMissingFinalizationToken ||
		!slices.Contains(enforcedDecision.Reasons, ReasonWrongExecutor) {
		t.Fatalf("变更集 3 enforced Sink 接受了无 Token 旧链：%+v", enforcedDecision)
	}

	facadeContext, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		SinkID: "codex.facade.upstream", Purpose: "facade", DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	facade := testResponsesRequest(t, facadeContext)
	assertGuardReasons(t, guard.Evaluate(facade, BackendHTTPUpstream, WireProtocolHTTP),
		ReasonUnregisteredSink, ReasonUnregisteredSinkObserved)
}

func TestChangeset1CPoliciesRemainEnforcedAfterChangeset3Promotion(t *testing.T) {
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		CanaryPercent:          100,
	}, DefaultSinkCatalog(), DefaultOfficialRouteCatalog(), &captureGuardRecorder{})
	if err != nil {
		t.Fatal(err)
	}

	unknown, err := http.NewRequest(
		http.MethodGet,
		"https://chatgpt.com/backend-api/codex/changeset-1c-unknown",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	unknownDecision := guard.Evaluate(unknown, BackendHTTPUpstream, WireProtocolHTTP)
	if unknownDecision.Allow || unknownDecision.RejectionReason != ReasonUnknownRoute {
		t.Fatalf("UnknownRoutePolicy=enforce 未拒绝未知 route：%+v", unknownDecision)
	}

	for name, input := range map[string]AttemptMetadataInput{
		"缺失 SinkID": {
			Purpose: "user_request.responses", DeclaredPersona: PersonaCodexCLI,
		},
		"未登记 SinkID": {
			SinkID: "codex.changeset1c.unregistered", Purpose: "user_request.responses",
			DeclaredPersona: PersonaCodexCLI,
		},
		"绑定不符": {
			SinkID: SinkCodexResponsesForward, Purpose: "oauth_refresh",
			DeclaredPersona: PersonaCodexCLI,
		},
	} {
		t.Run(name, func(t *testing.T) {
			requestContext, contextErr := WithAttemptMetadata(context.Background(), input)
			if contextErr != nil {
				t.Fatal(contextErr)
			}
			decision := guard.Evaluate(
				testResponsesRequest(t, requestContext),
				BackendHTTPUpstream,
				WireProtocolHTTP,
			)
			if decision.Allow || decision.RejectionReason == "" {
				t.Fatalf("UnregisteredSinkPolicy=enforce 未拒绝无效 binding：%+v", decision)
			}
		})
	}

	enforcedContext, err := BindDefaultSink(context.Background(), SinkCodexResponsesForward)
	if err != nil {
		t.Fatal(err)
	}
	enforcedDecision := guard.Evaluate(
		testResponsesRequest(t, enforcedContext),
		BackendHTTPUpstream,
		WireProtocolHTTP,
	)
	if enforcedDecision.Allow || enforcedDecision.SinkState != SinkStateEnforced ||
		enforcedDecision.RejectionReason != ReasonMissingFinalizationToken {
		t.Fatalf("变更集 3 状态提升后主链仍接受无 Token 发送：%+v", enforcedDecision)
	}
}

func TestGuardTelemetryNeverContainsSecretsOrRawPaths(t *testing.T) {
	var output bytes.Buffer
	recorder := NewBoundedGuardRecorder(8, slog.New(slog.NewTextHandler(&output, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	})))
	guard := newTestDefaultGuard(t, recorder)
	req, err := http.NewRequest(
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/unknown/user-secret?access_token=query-secret",
		strings.NewReader("body-secret"),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer header-secret")
	req.Header.Set("Cookie", "session=cookie-secret")
	_ = guard.Evaluate(req, BackendHTTPUpstream, WireProtocolHTTP)
	logged := output.String()
	for _, secret := range []string{
		"user-secret", "query-secret", "body-secret", "header-secret", "cookie-secret", "access_token",
	} {
		if strings.Contains(logged, secret) {
			t.Fatalf("Guard 日志泄漏 %q：%s", secret, logged)
		}
	}
	if !strings.Contains(logged, "/backend-api/codex/{managed_path}") {
		t.Fatalf("Guard 日志未使用规范化 path 模板：%s", logged)
	}
}

func TestGuardRollbackTelemetryIsVisibleAtProductionInfoLevel(t *testing.T) {
	var output bytes.Buffer
	recorder := NewBoundedGuardRecorder(8, slog.New(slog.NewTextHandler(&output, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))
	for _, reason := range []GuardReason{
		ReasonCanaryObservePassthrough,
		ReasonSinkOverrideObserved,
		ReasonUnknownRouteOverrideObserved,
		ReasonUnregisteredSinkOverrideObserved,
	} {
		if err := recorder.RecordOfficialEgressEvent(GuardEvent{Reason: reason}); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(output.String(), "reason_code="+string(reason)) {
			t.Fatalf("生产 Info 级别未记录回滚原因 %s：%s", reason, output.String())
		}
	}
}

func TestGuardRecorderFailureNeverChangesDecision(t *testing.T) {
	for _, testCase := range []struct {
		name     string
		recorder GuardRecorder
	}{
		{name: "返回错误", recorder: failingGuardRecorder{}},
		{name: "触发 panic", recorder: failingGuardRecorder{panicOnRecord: true}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			guard := newTestDefaultGuard(t, testCase.recorder)
			req, err := http.NewRequest(http.MethodGet, "https://example.com/health", nil)
			if err != nil {
				t.Fatal(err)
			}
			decision := guard.Evaluate(req, BackendPlainNetHTTP, WireProtocolHTTP)
			if !decision.Allow || decision.Scope != EgressScopeOutOfScope {
				t.Fatalf("记录器故障改变了 Guard 决策：%+v", decision)
			}
			if guard.RecorderFailureCount() != 1 {
				t.Fatalf("记录器故障计数=%d，期望 1", guard.RecorderFailureCount())
			}
		})
	}
}

func TestExecutorSignsFinalRequestAndGuardDetectsMutation(t *testing.T) {
	const digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const poolDigest = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	selection := testReleaseSelection()
	execution := testExecutionPolicy()
	deployment := testDeploymentPolicy(BackendHTTPUpstream)
	transport := TransportSpec{
		ID: "test-http", Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP,
		Adapter: AdapterHTTPUpstream, ProfileDigest: digest,
		ConnectionGroup: "responses", ConnectionPoolDigest: poolDigest,
		ResourceLifecycle: profilecontract.ResourceLifecyclePolicy{
			Lifecycle:         profilecontract.LifecyclePerUpperApiCall,
			Scope:             profilecontract.ResourceScopeInvocation,
			RetryReusesClient: true,
		},
		Normalization: WireNormalizationPlan{
			HeaderMode: HeaderNormalizationLowercase, SuppressDefaultUserAgent: true,
		},
	}
	release, err := NewResolvedRelease(ResolvedReleaseInput{
		ID: "test-release", Purpose: RegistryPurposeOpenAIOAuthHTTP,
		Mode: ReleaseModeActive, Version: "0.145.0", Digest: digest,
		Persona: PersonaCodexCLI, Execution: execution, Deployment: deployment,
		Selection: selection,
		Endpoints: []ResolvedEndpoint{{
			ID: "responses_http", Protocol: WireProtocolHTTP, Transport: transport,
			Route: RouteKey{Method: http.MethodPost, Host: "chatgpt.com",
				Path: "/backend-api/codex/responses", Purpose: "user_request.responses"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	sinkCatalog, err := NewSinkCatalog([]SinkBindingInput{
		testSinkBindingInputForAuthority(SinkStateEnforced, "test-executor", "test-http"),
	})
	if err != nil {
		t.Fatal(err)
	}
	routeCatalog, err := NewOfficialRouteCatalog(sinkCatalog)
	if err != nil {
		t.Fatal(err)
	}
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		CanaryPercent:          100,
	}, sinkCatalog, routeCatalog, &captureGuardRecorder{})
	if err != nil {
		t.Fatal(err)
	}
	adapter, err := NewHTTPUpstreamTransportAdapter(&fakeHTTPUpstreamPort{})
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream}},
		[]TransportAdapter{adapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	executor, err := NewExecutor(
		"test-executor",
		passthroughRequestCompiler{},
		registry,
		guard,
	)
	if err != nil {
		t.Fatal(err)
	}
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	input := ExecutorRequest{
		Bundle: testBundleFromResolvedRelease(t, release),
		Plan: CodexEgressPlan{
			SinkID: "codex.responses.forward", Purpose: "user_request.responses",
			EndpointID: "responses_http", Mode: ReleaseModeActive,
			Method: http.MethodPost, URL: target,
			Headers: http.Header{
				"X-Codex-Test": []string{"signed-before-normalization"},
				"User-Agent":   []string{""},
			},
			Body:            NewReplayableRequestBody([]byte(`{"model":"gpt-test"}`)),
			DeclaredPersona: PersonaCodexCLI, InvocationID: "stable-invocation",
		},
	}
	preboundContext, err := sinkCatalog.StartAttemptContext(
		context.Background(),
		"codex.responses.forward",
	)
	if err != nil {
		t.Fatal(err)
	}
	prepared, err := executor.Prepare(preboundContext, input)
	if err != nil {
		t.Fatalf("Executor Prepare 失败: %v", err)
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		t.Fatal(err)
	}
	identity, ok := AttemptIdentityFromContext(request.Context())
	if !ok || identity.InvocationID != "stable-invocation" ||
		identity.ExecutorID != "test-executor" || !identity.HasFinalizationToken {
		t.Fatalf("Executor 未完整升级业务 binding：%+v", identity)
	}
	// 模拟 HTTPUpstream terminal 前的合法 lowercase wire adapter。语义摘要必须
	// 接受该计划内变换，同时仍保留 Go 默认 User-Agent 抑制哨兵。
	finalHeaders := make(http.Header, len(request.Header))
	for name, values := range request.Header {
		if name == "User-Agent" {
			finalHeaders[name] = append([]string(nil), values...)
			continue
		}
		finalHeaders[strings.ToLower(name)] = append([]string(nil), values...)
	}
	request.Header = finalHeaders
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if !decision.Allow || decision.RejectionReason != "" {
		t.Fatalf("有效 FinalizationToken 被拒绝：%+v", decision)
	}

	request.Header.Set("x-after-finalize", "mutated")
	decision = guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if decision.Allow || decision.RejectionReason != ReasonRequestModifiedAfterFinalize {
		t.Fatalf("定型后修改未被拒绝：%+v", decision)
	}
}

func TestWebSocketCompressionNormalizationHasStableSemanticDigest(t *testing.T) {
	plan := WireNormalizationPlan{
		HeaderMode:                  HeaderNormalizationPreserve,
		WebSocketCompressionOffer:   "permessage-deflate; client_max_window_bits",
		WebSocketContextTakeover:    true,
		WebSocketCompressedTextRSV1: true,
		WebSocketRawDeflatePayload:  true,
	}
	before, err := http.NewRequest(http.MethodGet, "wss://chatgpt.com/backend-api/codex/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	before.Header.Set("Upgrade", "websocket")
	after := before.Clone(before.Context())
	// coder/websocket 在进入用户提供的 HTTP RoundTripper 前会把 wss 转为 https。
	// 这项协议别名转换必须与 transport 自动握手头一样保持同一语义摘要。
	after.URL.Scheme = "https"
	after.Header = before.Header.Clone()
	after.Header.Set("Connection", "Upgrade")
	after.Header.Set("Upgrade", "websocket")
	after.Header.Set("Sec-WebSocket-Version", "13")
	after.Header.Set("Sec-WebSocket-Key", "synthetic-transport-key")
	after.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate; client_max_window_bits")
	beforeDigest, err := requestDigest(before, plan, WireProtocolWebSocket)
	if err != nil {
		t.Fatal(err)
	}
	afterDigest, err := requestDigest(after, plan, WireProtocolWebSocket)
	if err != nil {
		t.Fatal(err)
	}
	if beforeDigest != afterDigest {
		t.Fatalf("计划内 WS 协议别名或 compression 扩展改变了语义摘要：before=%s after=%s", beforeDigest, afterDigest)
	}
	if err := validateFinalWireNormalization(after, plan, WireProtocolWebSocket); err != nil {
		t.Fatalf("合法最终 WS offer 被拒绝：%v", err)
	}
	after.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate; server_no_context_takeover")
	if err := validateFinalWireNormalization(after, plan, WireProtocolWebSocket); err == nil {
		t.Fatal("未授权的 WS compression 变换未被拒绝")
	}

	insecure := after.Clone(after.Context())
	insecure.URL.Scheme = "http"
	insecure.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate; client_max_window_bits")
	insecureDigest, err := requestDigest(insecure, plan, WireProtocolWebSocket)
	if err != nil {
		t.Fatal(err)
	}
	if insecureDigest == beforeDigest {
		t.Fatal("wss 与 ws 被错误归并为同一个请求摘要")
	}
}

func TestExecutorSingleUseBodyIsNotPreReadAndCannotReplay(t *testing.T) {
	execution := testExecutionPolicy()
	execution.Replayable = false
	executor, sinkCatalog, guard, bundle := newExecutorForBodyTest(t, execution)
	stream := &countingReadCloser{reader: strings.NewReader("single-use-payload")}
	body, err := NewSingleUseRequestBody(stream, int64(len("single-use-payload")))
	if err != nil {
		t.Fatal(err)
	}
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	input := ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: SinkCodexResponsesForward, Purpose: "user_request.responses",
			EndpointID: "responses_http", Mode: ReleaseModeActive,
			Method: http.MethodPost, URL: target, Body: body,
			DeclaredPersona: PersonaCodexCLI, InvocationID: "single-use-invocation",
		},
	}
	ctx, err := sinkCatalog.StartAttemptContext(context.Background(), SinkCodexResponsesForward)
	if err != nil {
		t.Fatal(err)
	}
	prepared, err := executor.Prepare(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if stream.readCalls != 0 {
		t.Fatalf("Executor Prepare 预读了 single-use Body：reads=%d", stream.readCalls)
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		t.Fatal(err)
	}
	if stream.readCalls != 0 {
		t.Fatalf("取得请求时预读了 single-use Body：reads=%d", stream.readCalls)
	}
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if !decision.Allow {
		t.Fatalf("single-use capability 未通过 Guard：%+v", decision)
	}
	if stream.readCalls != 0 {
		t.Fatalf("Guard 摘要读取了 single-use Body：reads=%d", stream.readCalls)
	}
	if _, err := prepared.TakeHTTPRequest(); err == nil {
		t.Fatal("single-use PreparedRequest 被第二次取得")
	}
	raw, err := io.ReadAll(request.Body)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "single-use-payload" || stream.readCalls == 0 {
		t.Fatalf("single-use Body 发送内容异常：body=%q reads=%d", raw, stream.readCalls)
	}
}

func TestRequestCompilerCannotReplaceSingleUseCapabilityWithSameLengthStream(t *testing.T) {
	originalStream := &countingReadCloser{reader: strings.NewReader("original")}
	original, err := NewSingleUseRequestBody(originalStream, int64(len("original")))
	if err != nil {
		t.Fatal(err)
	}
	replacementStream := &countingReadCloser{reader: strings.NewReader("replaced")}
	replacement, err := NewSingleUseRequestBody(replacementStream, int64(len("replaced")))
	if err != nil {
		t.Fatal(err)
	}
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	plan := CodexEgressPlan{
		SinkID: SinkCodexResponsesForward, Purpose: "user_request.responses",
		EndpointID: "responses_http", Mode: ReleaseModeActive,
		Method: http.MethodPost, URL: target, Body: original,
		DeclaredPersona: PersonaCodexCLI,
	}
	compiled, err := NewCompiledRequest(plan.Method, target, nil, replacement)
	if err != nil {
		t.Fatal(err)
	}
	endpoint := ResolvedEndpoint{
		ID: "responses_http", Protocol: WireProtocolHTTP,
		Route: RouteKey{Method: http.MethodPost, Host: "chatgpt.com",
			Path: "/backend-api/codex/responses", Purpose: plan.Purpose},
	}
	if err := validateCompiledRequestForEndpoint(compiled, endpoint, plan); err == nil ||
		!strings.Contains(err.Error(), "capability") {
		t.Fatalf("同长度替换 single-use stream 未被拒绝：%v", err)
	}
	if originalStream.readCalls != 0 || replacementStream.readCalls != 0 {
		t.Fatal("capability 比较不应读取任一 stream")
	}
}

func TestExecutorRejectsReplayPolicyForSingleUseBody(t *testing.T) {
	execution := testExecutionPolicy()
	executor, sinkCatalog, _, bundle := newExecutorForBodyTest(t, execution)
	body, err := NewSingleUseRequestBody(
		&countingReadCloser{reader: strings.NewReader("payload")},
		int64(len("payload")),
	)
	if err != nil {
		t.Fatal(err)
	}
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	ctx, err := sinkCatalog.StartAttemptContext(context.Background(), SinkCodexResponsesForward)
	if err != nil {
		t.Fatal(err)
	}
	_, err = executor.Prepare(ctx, ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: SinkCodexResponsesForward, Purpose: "user_request.responses",
			EndpointID: "responses_http", Mode: ReleaseModeActive,
			Method: http.MethodPost, URL: target, Body: body, DeclaredPersona: PersonaCodexCLI,
		},
	})
	if err == nil || !strings.Contains(err.Error(), "single-use stream") {
		t.Fatalf("可重放策略未拒绝 single-use Body：%v", err)
	}
}

func TestExecutorRejectsUnknownPurposeBackendAndProtocolMismatch(t *testing.T) {
	adapter, err := NewHTTPUpstreamTransportAdapter(&fakeHTTPUpstreamPort{})
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream}},
		[]TransportAdapter{adapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.resolve(TransportSpec{Backend: BackendWebSocket}); err == nil {
		t.Fatal("未登记 backend 发生默认回落")
	}
	if _, err := registry.resolve(TransportSpec{
		Backend: BackendHTTPUpstream, Protocol: WireProtocolWebSocket, Adapter: AdapterHTTPUpstream,
	}); err == nil {
		t.Fatal("协议不匹配发生默认回落")
	}
	if _, err := NewAdapterRegistry(
		[]BackendDescriptor{{Backend: BackendPlainNetHTTP, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream}},
		[]TransportAdapter{adapter},
	); err == nil {
		t.Fatal("plain_net_http 被登记为 Executor adapter")
	}

	guard := newTestDefaultGuard(t, &captureGuardRecorder{})
	executor, err := NewExecutor("unknown-purpose-executor", NewCompiler(), registry, guard)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := resolver.Resolve(BundleResolveRequest{
		SinkID: SinkCodexResponsesForward, Mode: ReleaseModeActive,
		Execution:  testExecutionPolicy(),
		Deployment: testDeploymentPolicy(BackendHTTPUpstream),
		Behavior:   BehaviorPolicy{ID: "unknown-purpose", Source: "test", Kind: BehaviorUserRequest, AttemptBudget: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	_, err = executor.Prepare(context.Background(), ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: "codex.responses.forward", Purpose: "unknown.purpose",
			EndpointID: "responses_http", Mode: ReleaseModeActive,
			Protocol: WireProtocolHTTP, Method: http.MethodPost, URL: target,
			IdentityMode:    IdentityCodexOAuthStrict,
			HeaderPolicy:    HeaderPolicy{ID: "unknown-purpose-headers", Source: "test"},
			BodyPolicy:      BodyPolicy{ID: "unknown-purpose-body", Source: "test"},
			DeclaredPersona: PersonaCodexCLI,
		},
	})
	if err == nil || !strings.Contains(err.Error(), "Purpose") {
		t.Fatalf("未知 purpose 未被 Bundle/compiler 显式拒绝：%v", err)
	}
}

func TestOfficialEgressCoreHasNoReverseDependency(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		parsed, err := parser.ParseFile(token.NewFileSet(), file, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatal(err)
		}
		for _, declaration := range parsed.Decls {
			importDecl, ok := declaration.(*ast.GenDecl)
			if !ok || importDecl.Tok != token.IMPORT {
				continue
			}
			for _, spec := range importDecl.Specs {
				importSpec, ok := spec.(*ast.ImportSpec)
				if !ok {
					t.Fatalf("核心文件 %s 包含非 import spec 节点：%T", file, spec)
				}
				path := strings.Trim(importSpec.Path.Value, `"`)
				for _, forbidden := range []string{
					"/internal/service", "/internal/repository", "github.com/gin-gonic/gin", "github.com/imroc/req",
				} {
					if strings.Contains(path, forbidden) {
						t.Fatalf("核心文件 %s 反向依赖 %s", file, path)
					}
				}
			}
		}
	}
}

type captureGuardRecorder struct {
	events []GuardEvent
}

type failingGuardRecorder struct {
	panicOnRecord bool
}

type countingReadCloser struct {
	reader    io.Reader
	readCalls int
	closed    bool
}

func (r *countingReadCloser) Read(buffer []byte) (int, error) {
	r.readCalls++
	return r.reader.Read(buffer)
}

func (r *countingReadCloser) Close() error {
	r.closed = true
	return nil
}

func (r failingGuardRecorder) RecordOfficialEgressEvent(GuardEvent) error {
	if r.panicOnRecord {
		panic("合成记录器故障")
	}
	return errors.New("合成记录器故障")
}

type guardRoundTripFunc func(*http.Request) (*http.Response, error)

func (f guardRoundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func (r *captureGuardRecorder) RecordOfficialEgressEvent(event GuardEvent) error {
	r.events = append(r.events, event)
	return nil
}

func newTestDefaultGuard(t *testing.T, recorder GuardRecorder) *Guard {
	t.Helper()
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyObserve),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyObserve),
	}, DefaultSinkCatalog(), DefaultOfficialRouteCatalog(), recorder)
	if err != nil {
		t.Fatal(err)
	}
	return guard
}

func testResponsesRequest(t *testing.T, ctx context.Context) *http.Request {
	t.Helper()
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		strings.NewReader(`{"model":"gpt-test"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	return req
}

func assertGuardReasons(t *testing.T, decision GuardDecision, expected ...GuardReason) {
	t.Helper()
	if !decision.Allow {
		t.Fatalf("observe 决策意外拒绝：%+v", decision)
	}
	for _, reason := range expected {
		if !slices.Contains(decision.Reasons, reason) {
			t.Fatalf("决策缺少 reason=%s：%+v", reason, decision)
		}
	}
}

func testSinkBindingInput(state SinkEnforcementState) SinkBindingInput {
	return testSinkBindingInputForAuthority(state, "body-test-executor", "body-test-http")
}

func TestSinkCatalogTokenlessBackgroundPrewarmOnlyAllowsLegacy(t *testing.T) {
	tests := []struct {
		name    string
		state   SinkEnforcementState
		allowed bool
	}{
		{name: "legacy", state: SinkStateLegacyObserve, allowed: true},
		{name: "canary", state: SinkStateCanaryEnforce, allowed: false},
		{name: "enforced", state: SinkStateEnforced, allowed: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			catalog, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(test.state)})
			if err != nil {
				t.Fatal(err)
			}
			allowed, err := catalog.AllowsTokenlessBackgroundPrewarm("codex.responses.forward")
			if err != nil {
				t.Fatal(err)
			}
			if allowed != test.allowed {
				t.Fatalf("state=%s allowed=%v want=%v", test.state, allowed, test.allowed)
			}
		})
	}
	if _, err := DefaultSinkCatalog().AllowsTokenlessBackgroundPrewarm("missing.sink"); err == nil {
		t.Fatal("未登记 Sink 的后台预热判定未 fail-close")
	}
}

func testSinkBindingInputForAuthority(
	state SinkEnforcementState,
	authorityID ExecutorID,
	transportID string,
) SinkBindingInput {
	input := SinkBindingInput{
		ID: "codex.responses.forward", Purpose: "user_request.responses",
		Persona: PersonaCodexCLI, EndpointEvidence: EndpointEvidenceCodexProfile,
		Routes: []CatalogRoute{{
			Key: RouteKey{Method: http.MethodPost, Host: "chatgpt.com",
				Path: "/backend-api/codex/responses", Purpose: "user_request.responses"},
			Protocol: WireProtocolHTTP,
		}},
		TargetBackend: BackendHTTPUpstream, LegacyBackends: []BackendKind{BackendHTTPUpstream},
		EnforcementState: state, Owner: "test", MigrationChangeset: "test",
		ExpiryCondition: "test", RuntimeBindable: true,
	}
	if state == SinkStateCanaryEnforce || state == SinkStateEnforced {
		digest, err := sinkBindingIdentityDigest(input)
		if err != nil {
			panic(err)
		}
		input.migrationReceipt = &MigrationReceipt{
			digest: strings.Repeat("f", sha256.Size*2), sinkID: input.ID,
			approvedState: state, bindingDigest: digest,
			authorityKind: receiptcontract.AuthorityCodexExecutor,
			authorityID:   authorityID, tokenIssuerID: authorityID,
			routeClaims: []migrationRouteClaim{{
				route: input.Routes[0], evidenceKind: "codex_endpoint", evidenceID: "responses_http",
				backend: BackendHTTPUpstream, adapterID: AdapterHTTPUpstream,
				transportID: transportID,
			}},
		}
	}
	return input
}

func testReleaseSelection() ReleaseSelectionPolicy {
	return ReleaseSelectionPolicy{
		ID: "responses-selection", Source: "test", BusinessPurpose: "user_request.responses",
		FamilyID:        ReleaseFamilyOpenAIOAuthHTTP,
		RegistryPurpose: RegistryPurposeOpenAIOAuthHTTP, EndpointID: "responses_http",
	}
}

func testExecutionPolicy() ExecutionPolicy {
	return ExecutionPolicy{
		ID: "test-execution", Source: "test", MaxAttempts: 1,
		Replayable: true, ConcurrencyLimit: 1,
	}
}

func newExecutorForBodyTest(
	t *testing.T,
	execution ExecutionPolicy,
) (*Executor, SinkCatalog, *Guard, ReleaseBundle) {
	t.Helper()
	const digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const poolDigest = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	selection := testReleaseSelection()
	deployment := testDeploymentPolicy(BackendHTTPUpstream)
	release, err := NewResolvedRelease(ResolvedReleaseInput{
		ID: "body-test-release", Purpose: RegistryPurposeOpenAIOAuthHTTP,
		Mode: ReleaseModeActive, Version: "0.145.0", Digest: digest,
		Persona: PersonaCodexCLI, Execution: execution, Deployment: deployment,
		Selection: selection,
		Endpoints: []ResolvedEndpoint{{
			ID: "responses_http", Protocol: WireProtocolHTTP,
			Route: RouteKey{Method: http.MethodPost, Host: "chatgpt.com",
				Path: "/backend-api/codex/responses", Purpose: "user_request.responses"},
			Transport: TransportSpec{
				ID: "body-test-http", Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP,
				Adapter: AdapterHTTPUpstream, ProfileDigest: digest,
				ConnectionGroup: "responses", ConnectionPoolDigest: poolDigest,
				ResourceLifecycle: profilecontract.ResourceLifecyclePolicy{
					Lifecycle:         profilecontract.LifecyclePerUpperApiCall,
					Scope:             profilecontract.ResourceScopeInvocation,
					RetryReusesClient: true,
				},
				Normalization: WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve},
			},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	sinkCatalog, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(SinkStateEnforced)})
	if err != nil {
		t.Fatal(err)
	}
	routeCatalog, err := NewOfficialRouteCatalog(sinkCatalog)
	if err != nil {
		t.Fatal(err)
	}
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce), CanaryPercent: 100,
	}, sinkCatalog, routeCatalog, &captureGuardRecorder{})
	if err != nil {
		t.Fatal(err)
	}
	adapter, err := NewHTTPUpstreamTransportAdapter(&fakeHTTPUpstreamPort{})
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream}},
		[]TransportAdapter{adapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	executor, err := NewExecutor("body-test-executor", passthroughRequestCompiler{}, registry, guard)
	if err != nil {
		t.Fatal(err)
	}
	return executor, sinkCatalog, guard, testBundleFromResolvedRelease(t, release)
}

func testDeploymentPolicy(backends ...BackendKind) DeploymentSupportPolicy {
	return DeploymentSupportPolicy{
		ID: "test-deployment", Source: "test", Platform: "test", ProxyMode: "direct",
		ProxyIdentityDigest: "direct",
		SupportedBackends:   backends,
	}
}

type passthroughRequestCompiler struct{}

func (passthroughRequestCompiler) Compile(
	_ context.Context,
	bundle ReleaseBundle,
	plan CodexEgressPlan,
	_ EndpointDynamicInputs,
) (CompiledExecution, error) {
	protocol := plan.Protocol
	if !protocol.Valid() {
		protocol = WireProtocolHTTP
	}
	endpoint, err := bundle.ResolveEndpointPlan(plan.SinkID, plan.Method, plan.URL, protocol)
	if err != nil {
		return CompiledExecution{}, err
	}
	request, err := NewCompiledRequest(plan.Method, plan.URL, plan.Headers, plan.Body)
	if err != nil {
		return CompiledExecution{}, err
	}
	transport := TransportSpec{
		ID:      endpoint.template.transport.ID,
		Backend: endpoint.template.backend, Protocol: endpoint.Protocol(),
		Adapter: endpoint.template.adapter, ProfileDigest: bundle.ProfileDigest(),
		ConnectionGroup:      endpoint.template.connectionGroup,
		ConnectionPoolDigest: strings.Repeat("b", sha256.Size*2),
		ResourceLifecycle:    endpoint.template.endpoint.ResourceLifecycle,
		Normalization: WireNormalizationPlan{
			HeaderMode: HeaderNormalizationPreserve,
		},
	}
	if endpoint.template.transport.LowercaseHTTPHeaders {
		transport.Normalization = WireNormalizationPlan{
			HeaderMode: HeaderNormalizationLowercase, SuppressDefaultUserAgent: true,
		}
	}
	return CompiledExecution{
		request: request, endpointPlan: endpoint, transport: transport,
		releaseDigest: bundle.ReleaseDigest(), bundleDigest: bundle.BundleDigest(),
		poolDigest: transport.ConnectionPoolDigest, compiledDigest: strings.Repeat("c", sha256.Size*2),
		connection: ConnectionIdentity{digest: strings.Repeat("d", sha256.Size*2)},
	}, nil
}

func testBundleFromResolvedRelease(t *testing.T, release ResolvedRelease) ReleaseBundle {
	t.Helper()
	plans := make(map[string]ResolvedEndpointPlan)
	for _, endpoint := range release.Endpoints() {
		physical := physicalRouteFromCatalogRoute(CatalogRoute{Key: endpoint.Route, Protocol: endpoint.Protocol})
		binding := EndpointBinding{
			key: EndpointBindingKey{
				SinkID: SinkCodexResponsesForward, Purpose: endpoint.Route.Purpose,
				PhysicalRouteID: physicalRouteID(physical), Protocol: endpoint.Protocol,
			},
			endpointID: endpoint.ID, releasePurpose: release.Purpose(),
			evidenceDigest: strings.Repeat("e", sha256.Size*2),
		}
		transport := profilecontract.ExecutableTransportProfile{
			ID:                   endpoint.Transport.ID,
			LowercaseHTTPHeaders: endpoint.Transport.Normalization.HeaderMode == HeaderNormalizationLowercase,
		}
		plan := ResolvedEndpointPlan{template: EndpointPlanTemplate{
			binding: binding,
			route:   CatalogRoute{Key: endpoint.Route, Protocol: endpoint.Protocol},
			endpoint: profilecontract.ExecutableEndpointProfile{
				ID: endpoint.ID, Method: endpoint.Route.Method,
				Host: endpoint.Route.Host, Path: endpoint.Route.Path,
				TransportID:       endpoint.Transport.ID,
				ResourceLifecycle: endpoint.Transport.ResourceLifecycle,
			},
			transport: transport, backend: endpoint.Transport.Backend,
			adapter: endpoint.Transport.Adapter, connectionGroup: endpoint.Transport.ConnectionGroup,
		}}
		plans[binding.Key().identity()] = plan
	}
	return ReleaseBundle{
		release: ResolvedCodexRelease{
			mode: release.Mode(), releaseDigest: release.Digest(),
			profileDigest: release.Digest(),
		},
		primarySink: SinkCodexResponsesForward,
		plans:       plans, execution: release.Execution(), deployment: release.Deployment(),
		behavior:     BehaviorPolicy{ID: "test", Source: "test", Kind: BehaviorUserRequest, AttemptBudget: 1},
		bundleDigest: release.Digest(),
	}
}

type fakeHTTPUpstreamPort struct{}

func (*fakeHTTPUpstreamPort) SendHTTPUpstream(
	_ context.Context,
	request PreparedRequest,
) (*http.Response, error) {
	httpRequest, err := request.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader("ok")),
		Request:    httpRequest,
	}, nil
}
