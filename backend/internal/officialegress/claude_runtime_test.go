package officialegress

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

const (
	claudeTestAccountUUID = "22222222-2222-4222-8222-222222222222"
	claudeTestSessionID   = "33333333-3333-4333-8333-333333333333"
	claudeTestRequestID   = "44444444-4444-4444-8444-444444444444"
)

type claudeCapturedRequest struct {
	Method string
	URL    string
	Header http.Header
	Body   []byte
}

type claudeCapturePort struct {
	mu         sync.Mutex
	requests   []claudeCapturedRequest
	transports []TransportSpec
	respond    func(claudeCapturedRequest, int) (*http.Response, error)
}

func (p *claudeCapturePort) SendHTTPUpstream(
	_ context.Context,
	prepared PreparedRequest,
) (*http.Response, error) {
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	captured := claudeCapturedRequest{
		Method: request.Method, URL: request.URL.String(), Header: request.Header.Clone(), Body: body,
	}
	p.mu.Lock()
	p.requests = append(p.requests, captured)
	p.transports = append(p.transports, prepared.Transport())
	ordinal := len(p.requests)
	responder := p.respond
	p.mu.Unlock()
	if responder != nil {
		return responder(captured, ordinal)
	}
	return claudeTestResponse(http.StatusOK, "{}"), nil
}

func (p *claudeCapturePort) transportSnapshot() []TransportSpec {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]TransportSpec, len(p.transports))
	for index := range p.transports {
		out[index] = p.transports[index].Clone()
	}
	return out
}

func (p *claudeCapturePort) snapshot() []claudeCapturedRequest {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]claudeCapturedRequest, len(p.requests))
	copy(out, p.requests)
	return out
}

func claudeTestResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Request-Id": []string{"req_test"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func finalizeClaudeTestResult(t *testing.T, result *ClaudeCandidateResult) {
	t.Helper()
	if err := result.FinalizeSession(true); err != nil {
		t.Fatal(err)
	}
}

func newClaudeTestRuntime(
	t *testing.T,
	port HTTPUpstreamTransportPort,
) (*ClaudeCandidateRuntime, SinkCatalog, *Guard) {
	t.Helper()
	catalog, err := ClaudeFWGCandidateSinkCatalog(DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	routes, err := NewOfficialRouteCatalog(catalog)
	if err != nil {
		t.Fatal(err)
	}
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		CanaryPercent:          100,
	}, catalog, routes, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime, err := NewClaudeCandidateRuntime(catalog, guard, port)
	if err != nil {
		t.Fatal(err)
	}
	runtime.newRequestID = func() string { return claudeTestRequestID }
	runtime.newCCH = func() (string, error) { return "abcde", nil }
	runtime.retryJitter = func(int) (int, error) { return 0, nil }
	runtime.sleep = func(context.Context, time.Duration) error { return nil }
	compiler := runtime.executor.dialects.compilers[PersonaClaudeCode].(claudeDialectCompiler)
	compiler.newRequestID = runtime.newRequestID
	runtime.executor.dialects.compilers[PersonaClaudeCode] = compiler
	return runtime, catalog, guard
}

func claudeTestTrustedFacts() ClaudeTrustedFacts {
	return ClaudeTrustedFacts{
		Account: ClaudeTrustedAccountFacts{
			AccountScope: "anthropic-oauth-account:17", AccountUUID: claudeTestAccountUUID,
		},
		Session: ClaudeTrustedSessionFacts{
			SessionID: claudeTestSessionID, Source: ClaudeSessionSourcePlannerDerived,
		},
		Entrypoint: ClaudeTrustedEntrypointFacts{
			Entrypoint: ClaudeEntrypointSDKCLI, IngressProtocol: "anthropic-messages",
			IngressBindingID: "api-key:23",
		},
		Features: ClaudeTrustedFeatureFacts{SystemMode: ClaudeSystemDefault},
	}
}

func claudeTestMessagesBody() []byte {
	return []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello from client"}],"tools":[],"stream":true}`)
}

func findClaudeCapturedRequest(
	t *testing.T,
	requests []claudeCapturedRequest,
	method string,
	urlSuffix string,
) claudeCapturedRequest {
	t.Helper()
	for _, request := range requests {
		if request.Method == method && strings.HasSuffix(request.URL, urlSuffix) {
			return request
		}
	}
	t.Fatalf("未捕获 Claude 请求：%s %s", method, urlSuffix)
	return claudeCapturedRequest{}
}

func decodeClaudeTestBody(t *testing.T, request claudeCapturedRequest) []byte {
	t.Helper()
	if !strings.EqualFold(request.Header.Get("Content-Encoding"), "gzip") {
		return request.Body
	}
	reader, err := gzip.NewReader(bytes.NewReader(request.Body))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = reader.Close() }()
	body, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func claudeTestHeaderValue(headers http.Header, name string) string {
	for current, values := range headers {
		if strings.EqualFold(current, name) && len(values) > 0 {
			return values[0]
		}
	}
	return ""
}

func claudeTestCanonicalHeaders(headers http.Header) http.Header {
	out := make(http.Header, len(headers))
	for name, values := range headers {
		for _, value := range values {
			out.Add(name, value)
		}
	}
	return out
}

func TestClaudeFWGCandidateCatalogSeparatesStrictManagedAndDenied(t *testing.T) {
	_, catalog, guard := newClaudeTestRuntime(t, &claudeCapturePort{})

	strictCount := 0
	managedCount := 0
	for _, binding := range catalog.Bindings() {
		if binding.Persona() == PersonaClaudeCode {
			strictCount++
			if binding.EnforcementState() != SinkStateEnforced ||
				binding.EndpointEvidence() != EndpointEvidenceClaudeProfile ||
				binding.MigrationReceiptDigest() == "" {
				t.Fatalf("Claude strict Sink 未完整定型：%s", binding.ID())
			}
		}
		if _, ok := binding.ManagedPolicy(); ok {
			managedCount++
			if binding.Persona() != PersonaUnclassified ||
				binding.EnforcementState() != SinkStateEnforced {
				t.Fatalf("Claude managed Sink 越权：%s", binding.ID())
			}
		}
	}
	if strictCount != 8 || managedCount != 9 {
		t.Fatalf("Claude 三态数量不一致：strict=%d managed=%d", strictCount, managedCount)
	}

	managed, ok := catalog.Resolve(SinkClaudeLegacyUsage)
	if !ok {
		t.Fatal("缺少 Claude usage managed Sink")
	}
	ctx, err := catalog.StartAttemptContext(context.Background(), managed.ID())
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodGet, "https://api.anthropic.com/api/oauth/usage", nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if !decision.Allow || decision.Scope != EgressScopeManaged ||
		decision.SinkState != SinkStateEnforced ||
		!slices.Contains(decision.Reasons, ReasonUnclassifiedPersona) ||
		slices.Contains(decision.Reasons, ReasonLegacyObservePassthrough) {
		t.Fatalf("Claude managed policy 未被独立执行：%+v", decision)
	}

	unbound, _ := http.NewRequest(http.MethodGet, "https://api.anthropic.com/api/oauth/usage", nil)
	unboundDecision := guard.Evaluate(unbound, BackendHTTPUpstream, WireProtocolHTTP)
	if unboundDecision.Allow || unboundDecision.RejectionReason != ReasonMissingSinkID {
		t.Fatalf("Claude managed route 未绑定时没有 fail-close：%+v", unboundDecision)
	}
	unknown, _ := http.NewRequestWithContext(
		ctx, http.MethodGet, "https://api.anthropic.com/api/oauth/unknown", nil,
	)
	unknownDecision := guard.Evaluate(unknown, BackendHTTPUpstream, WireProtocolHTTP)
	if unknownDecision.Allow || unknownDecision.RejectionReason != ReasonUnknownRoute {
		t.Fatalf("Claude 未登记 OAuth 出站没有 fail-close：%+v", unknownDecision)
	}
}

func TestClaudeFWGThirdPartyMessagesUsesUnifiedStrictChain(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Features.RequestGzip = true
	trusted.Features.CustomHeaderLines = []string{
		"X-FW-G-Probe: value:with:colon", "Authorization: <secret-forbidden-override>",
	}

	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "<secret-test-access-token>",
		TrustedFacts: trusted, InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	if !result.Stream || result.Model != "claude-sonnet-5" || result.Attempts != 1 ||
		result.StreamFallback == nil || len(runtime.RuleIDs()) != 40 {
		t.Fatalf("Claude strict 结果身份不一致：%+v", result)
	}

	requests := port.snapshot()
	if len(requests) != 4 {
		t.Fatalf("sdk-cli 首轮应发送 hello、policy、settings、messages：%d", len(requests))
	}
	message := findClaudeCapturedRequest(t, requests, http.MethodPost, "/v1/messages?beta=true")
	if claudeTestHeaderValue(message.Header, "Authorization") != "Bearer <secret-test-access-token>" ||
		claudeTestHeaderValue(message.Header, "X-FW-G-Probe") != "value:with:colon" ||
		claudeTestHeaderValue(message.Header, "X-Claude-Code-Session-Id") != claudeTestSessionID ||
		claudeTestHeaderValue(message.Header, "x-client-request-id") != claudeTestRequestID ||
		claudeTestHeaderValue(message.Header, "User-Agent") != "claude-cli/2.1.226 (external, sdk-cli)" ||
		claudeTestHeaderValue(message.Header, "Content-Encoding") != "gzip" {
		t.Fatalf("Claude messages Header 未按画像生成：%v", message.Header)
	}
	if len(message.Body) < 10 || !bytes.Equal(
		message.Body[:10], []byte{0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff},
	) {
		t.Fatalf("Claude gzip 固定头不一致：%x", message.Body[:min(10, len(message.Body))])
	}
	body := decodeClaudeTestBody(t, message)
	if !bytes.Contains(body, []byte(`cc_entrypoint=sdk-cli;`)) ||
		!bytes.Contains(body, []byte(`"metadata":{"user_id":"{\"device_id\"`)) ||
		!bytes.Contains(body, []byte(`"thinking":{"type":"adaptive"}`)) ||
		!bytes.Contains(body, []byte(`"stream":true`)) {
		t.Fatalf("Claude messages Body 未按画像生成：%s", body)
	}
	finalizeClaudeTestResult(t, &result)

	second, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "test-access-token",
		TrustedFacts: trusted, InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = second.Response.Body.Close()
	finalizeClaudeTestResult(t, &second)
	if len(port.snapshot()) != 5 {
		t.Fatal("同一账号、session、entrypoint 的 startup 必须只运行一次")
	}
}

func TestClaudeFWGStartupBranchesMatchEntrypointAndBackground(t *testing.T) {
	tests := []struct {
		name        string
		entrypoint  string
		background  bool
		wantTargets []string
	}{
		{
			name:       "sdk-cli",
			entrypoint: ClaudeEntrypointSDKCLI,
			wantTargets: []string{
				"/api/hello", "/api/claude_code/policy_limits", "/api/claude_code/settings",
			},
		},
		{
			name:       "tui",
			entrypoint: ClaudeEntrypointCLI,
			wantTargets: []string{
				"/api/hello", "/api/oauth/profile", "/v1/mcp_servers?limit=1000",
			},
		},
		{
			name:       "background",
			entrypoint: ClaudeEntrypointCLI,
			background: true,
			wantTargets: []string{
				"/api/hello", "/api/claude_code/policy_limits", "/api/claude_code/settings",
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			port := &claudeCapturePort{}
			runtime, _, _ := newClaudeTestRuntime(t, port)
			trusted := claudeTestTrustedFacts()
			trusted.Entrypoint.Entrypoint = test.entrypoint
			trusted.Agent.Background = test.background
			identity, err := deriveClaudeIdentityFacts(trusted)
			if err != nil {
				t.Fatal(err)
			}
			if err := runtime.executeClaudeStartup(
				context.Background(),
				ClaudeMessagesExecution{AccessToken: "token", TrustedFacts: trusted},
				identity,
			); err != nil {
				t.Fatal(err)
			}

			requests := port.snapshot()
			if len(requests) != len(test.wantTargets) {
				t.Fatalf("startup 请求数不一致：got=%d want=%d", len(requests), len(test.wantTargets))
			}
			gotTargets := make([]string, 0, len(requests))
			for _, request := range requests {
				gotTargets = append(
					gotTargets, strings.TrimPrefix(request.URL, "https://api.anthropic.com"),
				)
			}
			if test.entrypoint == ClaudeEntrypointCLI && !test.background {
				if !slices.Equal(gotTargets, test.wantTargets) {
					t.Fatalf("TUI startup 顺序不一致：got=%v want=%v", gotTargets, test.wantTargets)
				}
			} else {
				if gotTargets[0] != test.wantTargets[0] ||
					!slices.Contains(gotTargets[1:], test.wantTargets[1]) ||
					!slices.Contains(gotTargets[1:], test.wantTargets[2]) {
					// policy/settings 是官方并发启动面；只固定 hello 先行和成员闭集。
					t.Fatalf("非 TUI startup 闭集不一致：got=%v want=%v", gotTargets, test.wantTargets)
				}
			}
		})
	}

	t.Run("tui 与 background 启动状态隔离", func(t *testing.T) {
		port := &claudeCapturePort{}
		runtime, _, _ := newClaudeTestRuntime(t, port)
		trusted := claudeTestTrustedFacts()
		trusted.Entrypoint.Entrypoint = ClaudeEntrypointCLI
		mainIdentity, err := deriveClaudeIdentityFacts(trusted)
		if err != nil {
			t.Fatal(err)
		}
		input := ClaudeMessagesExecution{AccessToken: "token", TrustedFacts: trusted}
		if err := runtime.executeClaudeStartup(context.Background(), input, mainIdentity); err != nil {
			t.Fatal(err)
		}
		trusted.Agent.Background = true
		backgroundIdentity, err := deriveClaudeIdentityFacts(trusted)
		if err != nil {
			t.Fatal(err)
		}
		input.TrustedFacts = trusted
		if err := runtime.executeClaudeStartup(
			context.Background(), input, backgroundIdentity,
		); err != nil {
			t.Fatal(err)
		}
		requests := port.snapshot()
		if len(requests) != 6 {
			t.Fatalf("TUI 与 background 错误共用了 startup 状态：%d", len(requests))
		}
		if !strings.HasSuffix(requests[1].URL, "/api/oauth/profile") ||
			!strings.HasSuffix(requests[2].URL, "/v1/mcp_servers?limit=1000") ||
			!strings.HasSuffix(requests[3].URL, "/api/hello") {
			t.Fatalf("TUI/background startup 边界不一致：%+v", requests)
		}
	})
}

func TestClaudeFWGStartupAcceptsOnlyApprovedAbsentManagedState(t *testing.T) {
	port := &claudeCapturePort{
		respond: func(request claudeCapturedRequest, _ int) (*http.Response, error) {
			if strings.HasSuffix(request.URL, "/api/claude_code/policy_limits") ||
				strings.HasSuffix(request.URL, "/api/claude_code/settings") ||
				strings.HasSuffix(request.URL, "/api/oauth/profile") {
				return claudeTestResponse(http.StatusNotFound, `{}`), nil
			}
			return claudeTestResponse(http.StatusOK, `{}`), nil
		},
	}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Entrypoint.IngressProtocol = "managed-internal"

	for _, kind := range []string{"policy-limits", "remote-settings"} {
		err := runtime.executeClaudeStartupEndpoint(
			context.Background(), "token", trusted, kind,
		)
		if err != nil {
			t.Fatalf("%s 的合法 404 不应阻断启动：%v", kind, err)
		}
	}

	// 只有 policy/settings 的 404 是已批准“空配置”语义；其他辅助端点
	// 不得借此放宽。这里直接复用 startup 的状态判定验证 oauth-profile
	// 仍然 fail-close。
	err := runtime.executeClaudeStartupEndpoint(
		context.Background(), "token", trusted, "oauth-profile",
	)
	if err == nil || !strings.Contains(err.Error(), "返回状态 404") {
		t.Fatalf("oauth-profile 404 被错误放行：%v", err)
	}
}

func claudeCanonicalForWireScenario(
	t *testing.T,
	scenario claudeWireScenario,
	hint string,
	toolMode claudeToolMode,
) ClaudeCanonicalRequest {
	t.Helper()
	var maxTokens int
	if err := json.Unmarshal(scenario.MaxTokens, &maxTokens); err != nil {
		t.Fatal(err)
	}
	stream := false
	if scenario.StreamPresent {
		if err := json.Unmarshal(scenario.Stream, &stream); err != nil {
			t.Fatal(err)
		}
	}
	tools := json.RawMessage("[]")
	if scenario.ToolsPresent {
		tools = append(json.RawMessage(nil), scenario.Tools...)
	}
	canonical := ClaudeCanonicalRequest{
		model: scenario.Model,
		messages: json.RawMessage(
			`[{"role":"user","content":"scenario matrix probe"}]`,
		),
		tools: tools, toolsPresent: scenario.ToolsPresent, toolMode: toolMode,
		system:     cloneClaudeSystemBlocks(scenario.SystemBlocks),
		systemKind: claudeCanonicalSystemOfficial, scenarioHint: hint,
		firstUserText: "scenario matrix probe", maxTokens: maxTokens,
		streamPresent: scenario.StreamPresent, stream: stream, officialIngress: true,
	}
	if scenario.ThinkingPresent {
		canonical.thinking = append(json.RawMessage(nil), scenario.Thinking...)
		canonical.thinkingPresent = true
	}
	if scenario.ContextManagementPresent {
		canonical.contextManagement = append(
			json.RawMessage(nil), scenario.ContextManagement...,
		)
		canonical.contextManagementPresent = true
	}
	if scenario.OutputConfigPresent {
		canonical.outputConfig = append(json.RawMessage(nil), scenario.OutputConfig...)
	}
	if scenario.TemperaturePresent {
		canonical.temperature = append(json.RawMessage(nil), scenario.Temperature...)
	}
	return canonical
}

func TestClaudeFWGApprovedScenarioMatrixCompilesFrozenWire(t *testing.T) {
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name       string
		hint       string
		scenario   claudeWireScenario
		entrypoint string
		agent      bool
		background bool
		modelShape claudeModelShape
		toolMode   claudeToolMode
		features   ClaudeTrustedFeatureFacts
	}{
		{name: "sdk-cli", hint: "sdk-cli", scenario: wire.ImplementationPolicy.Scenarios.SDKCLI, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet},
		{name: "agent", hint: "agent", scenario: wire.ImplementationPolicy.Scenarios.Agent, entrypoint: ClaudeEntrypointSDKCLI, agent: true, modelShape: claudeModelShapeSonnet},
		{name: "tui-main", hint: "tui-main", scenario: wire.ImplementationPolicy.Scenarios.TUIMain, entrypoint: ClaudeEntrypointCLI, modelShape: claudeModelShapeSonnet},
		{name: "tui-title", hint: "tui-title", scenario: wire.ImplementationPolicy.Scenarios.TUITitle, entrypoint: ClaudeEntrypointCLI, modelShape: claudeModelShapeSonnet, features: ClaudeTrustedFeatureFacts{TUITitleRequest: true}},
		{name: "fallback", hint: "fallback", scenario: wire.ImplementationPolicy.Scenarios.Fallback, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeHaiku},
		{name: "custom-system", hint: "custom-system", scenario: wire.ImplementationPolicy.Scenarios.CustomSystem, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet},
		{name: "append-system", hint: "append-system", scenario: wire.ImplementationPolicy.Scenarios.AppendSystem, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet},
		{name: "exclude-dynamic", hint: "exclude-dynamic", scenario: wire.ImplementationPolicy.Scenarios.ExcludeDynamic, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet},
		{name: "custom-agent", hint: "custom-agent", scenario: wire.ImplementationPolicy.Scenarios.CustomAgent, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet},
		{name: "web-search-server", scenario: wire.ImplementationPolicy.Scenarios.WebSearchServer, entrypoint: ClaudeEntrypointSDKCLI, modelShape: claudeModelShapeSonnet, toolMode: claudeToolModeWebSearchServer, features: ClaudeTrustedFeatureFacts{WebSearchServerEnabled: true}},
	}
	for index, scenario := range wire.ImplementationPolicy.Scenarios.Background {
		shape := claudeModelShapeSonnet
		if !scenario.StreamPresent {
			shape = claudeModelShapeNonStream
		}
		tests = append(tests, struct {
			name       string
			hint       string
			scenario   claudeWireScenario
			entrypoint string
			agent      bool
			background bool
			modelShape claudeModelShape
			toolMode   claudeToolMode
			features   ClaudeTrustedFeatureFacts
		}{
			name:       "background-" + strconv.Itoa(index),
			hint:       "background-" + strconv.Itoa(index),
			scenario:   scenario,
			entrypoint: ClaudeEntrypointCLI,
			background: true,
			modelShape: shape,
		})
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			facts := claudeTestTrustedFacts()
			facts.Entrypoint.Entrypoint = test.entrypoint
			facts.Agent.Background = test.background
			if test.agent {
				facts.Agent.AgentID = "aaaaaaaaaaaaaaaaa"
				facts.Agent.Depth = 1
			}
			identity := mustClaudeTestIdentity(t, facts)
			canonical := claudeCanonicalForWireScenario(
				t, test.scenario, test.hint, test.toolMode,
			)
			if test.toolMode == claudeToolModeWebSearchServer {
				canonical.toolChoice = append(
					json.RawMessage(nil),
					wire.ImplementationPolicy.ToolPolicy.WebSearchServer.ToolChoice...,
				)
			}
			plan := ClaudeEgressPlan{
				endpointKind: "messages-inference", canonical: canonical,
				identity: identity, features: test.features,
				modelShape: test.modelShape, cch: "abcde",
			}
			body, model, beta, stream, err := compileClaudeMessagesBody(plan, wire)
			if err != nil {
				t.Fatal(err)
			}
			if model != test.scenario.Model || stream !=
				(claudeModelShapeIsStream(test.modelShape) && test.scenario.StreamPresent) {
				t.Fatalf("场景模型或 stream 不一致：model=%s stream=%t", model, stream)
			}
			wantBeta := claudeToolBeta(
				test.toolMode, test.scenario.AnthropicBeta,
				wire.ImplementationPolicy.ToolPolicy,
			)
			if beta != wantBeta {
				t.Fatalf("场景 beta 不一致：got=%s want=%s", beta, wantBeta)
			}

			fields, err := decodeClaudeOrderedObject(body)
			if err != nil {
				t.Fatal(err)
			}
			actual := make(map[string]json.RawMessage, len(fields))
			order := make([]string, 0, len(fields))
			for _, field := range fields {
				actual[field.name] = field.raw
				order = append(order, field.name)
			}
			wantOrder := []string{"model", "messages", "system"}
			if test.scenario.ToolsPresent {
				wantOrder = append(wantOrder, "tools")
			}
			if len(canonical.toolChoice) != 0 {
				wantOrder = append(wantOrder, "tool_choice")
			}
			if test.scenario.MetadataPresent {
				wantOrder = append(wantOrder, "metadata")
			}
			wantOrder = append(wantOrder, "max_tokens")
			for _, optional := range []struct {
				name    string
				present bool
			}{
				{name: "thinking", present: test.scenario.ThinkingPresent},
				{name: "context_management", present: test.scenario.ContextManagementPresent},
				{name: "output_config", present: test.scenario.OutputConfigPresent},
				{name: "temperature", present: test.scenario.TemperaturePresent},
				{name: "stream", present: stream},
			} {
				if optional.present {
					wantOrder = append(wantOrder, optional.name)
				}
			}
			if !slices.Equal(order, wantOrder) {
				t.Fatalf("场景顶层字段顺序不一致：got=%v want=%v", order, wantOrder)
			}
			for _, expected := range []struct {
				name    string
				present bool
				raw     json.RawMessage
			}{
				{name: "max_tokens", present: true, raw: test.scenario.MaxTokens},
				{name: "tools", present: test.scenario.ToolsPresent, raw: test.scenario.Tools},
				{name: "thinking", present: test.scenario.ThinkingPresent, raw: test.scenario.Thinking},
				{name: "context_management", present: test.scenario.ContextManagementPresent, raw: test.scenario.ContextManagement},
				{name: "output_config", present: test.scenario.OutputConfigPresent, raw: test.scenario.OutputConfig},
				{name: "temperature", present: test.scenario.TemperaturePresent, raw: test.scenario.Temperature},
			} {
				got, present := actual[expected.name]
				if present != expected.present ||
					(expected.present && !claudeJSONEqual(got, expected.raw)) {
					t.Fatalf("场景字段 %s 不一致：got=%s want=%s", expected.name, got, expected.raw)
				}
			}
			var system []claudeWireSystemBlock
			if err := json.Unmarshal(actual["system"], &system); err != nil {
				t.Fatal(err)
			}
			if len(system) != len(test.scenario.SystemBlocks)+1 ||
				!strings.HasPrefix(system[0].Text, "x-anthropic-billing-header:") ||
				!claudeSystemBlocksEqual(system[1:], test.scenario.SystemBlocks) {
				t.Fatalf("场景 system 未逐块复现：got=%d want=%d", len(system), len(test.scenario.SystemBlocks)+1)
			}

			endpoint, err := profile.endpoint("messages-inference")
			if err != nil {
				t.Fatal(err)
			}
			headers, _, err := compileClaudeEndpointHeaders(
				plan, endpoint, wire,
				AttemptAuthenticationInput{BearerToken: "test-access-token"},
				body, model, beta, claudeTestRequestID,
			)
			if err != nil {
				t.Fatal(err)
			}
			ingressHeaders := claudeTestCanonicalHeaders(headers)
			resolved, ingressState, officialIngress, err := resolveClaudeOfficialIngressBase(
				body,
				ClaudeIngressSnapshot{Captured: true, Headers: ingressHeaders},
				facts,
				profile,
				wire,
			)
			if err != nil || !officialIngress {
				t.Fatalf("官方场景入口识别失败：official=%t err=%v", officialIngress, err)
			}
			roundCanonical, _, err := parseClaudeCanonicalMessages(
				body, resolved, wire, true,
			)
			if err != nil {
				t.Fatal(err)
			}
			classifyClaudeOfficialFallback(&roundCanonical, wire)
			if err := completeClaudeOfficialIngressFeatures(
				&resolved, roundCanonical, ingressState, wire, ingressHeaders,
			); err != nil {
				t.Fatal(err)
			}
			roundIdentity := mustClaudeTestIdentity(t, resolved)
			roundBody, _, _, _, err := compileClaudeMessagesBody(ClaudeEgressPlan{
				endpointKind: "messages-inference", canonical: roundCanonical,
				identity: roundIdentity, features: resolved.Features,
				modelShape: claudeInitialModelShape(roundCanonical), cch: "abcde",
			}, wire)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(roundBody, body) {
				t.Fatalf("官方场景 Body 往返不一致：\nwant=%s\n got=%s", body, roundBody)
			}
		})
	}
}

func TestClaudeFWGConditionalHeadersMetadataCacheEffortAndTransport(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Features = ClaudeTrustedFeatureFacts{
		SystemMode:           ClaudeSystemDefault,
		DisablePromptCaching: true,
		Effort:               "max",
		MaxTokens:            1234,
		AdditionalProtection: true,
		ClientApp:            "cursor",
		RemoteContainerID:    "container-1",
		RemoteSessionID:      "remote-session-1",
		AgentSDKVersion:      "0.2.0",
		Workload:             "tool",
		CustomHeaderLines: []string{
			"X-FW-G-Probe: value:with:colon", "Authorization: <secret-forbidden-override>",
		},
		CustomBetas:   []string{"fw-g-probe-beta"},
		ExtraMetadata: json.RawMessage(`{"workspace":"repo","device_id":"spoof"}`),
		RequestGzip:   true,
		APITimeoutMS:  1501,
	}
	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "<secret-test-access-token>",
		TrustedFacts: trusted, InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	finalizeClaudeTestResult(t, &result)

	requests := port.snapshot()
	transports := port.transportSnapshot()
	messageIndex := -1
	for index, request := range requests {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messageIndex = index
			break
		}
	}
	if messageIndex < 0 || len(transports) != len(requests) {
		t.Fatalf("未取得 messages transport：requests=%d transports=%d", len(requests), len(transports))
	}
	message := requests[messageIndex]
	transport := transports[messageIndex]
	if claudeTestHeaderValue(message.Header, "Authorization") != "Bearer <secret-test-access-token>" ||
		claudeTestHeaderValue(message.Header, "X-FW-G-Probe") != "value:with:colon" ||
		claudeTestHeaderValue(message.Header, "x-anthropic-additional-protection") != "true" ||
		claudeTestHeaderValue(message.Header, "x-claude-remote-container-id") != "container-1" ||
		claudeTestHeaderValue(message.Header, "x-claude-remote-session-id") != "remote-session-1" ||
		claudeTestHeaderValue(message.Header, "x-client-app") != "cursor" ||
		claudeTestHeaderValue(message.Header, "x-stainless-timeout") != "2" ||
		claudeTestHeaderValue(message.Header, "Content-Encoding") != "gzip" {
		t.Fatalf("Claude 条件 Header 未按画像生成：%v", message.Header)
	}
	wantUserAgent := "claude-cli/2.1.226 (external, sdk-cli, agent-sdk/0.2.0, client-app/cursor, workload/tool)"
	if claudeTestHeaderValue(message.Header, "User-Agent") != wantUserAgent {
		t.Fatalf("Claude 条件 User-Agent 不一致：%s", message.Header.Get("User-Agent"))
	}
	beta := claudeTestHeaderValue(message.Header, "anthropic-beta")
	if !strings.Contains(
		beta,
		"mid-conversation-system-2026-04-07,fw-g-probe-beta,effort-2025-11-24",
	) {
		t.Fatalf("Claude custom beta 插入位置不一致：%s", beta)
	}
	if transport.TLS.Stack != "node-v26.3.0-openssl" ||
		!slices.Equal(transport.TLS.ALPN, []string{"http/1.1"}) ||
		!transport.TLS.StrictH1Wire || len(transport.TLS.H1HeaderOrders) != 1 ||
		!transport.TLS.H1HeaderOrders[0].RejectUnlisted ||
		!slices.Equal(
			transport.TLS.H1HeaderOrders[0].Order,
			transport.TLS.PreserveHeaderCase,
		) {
		t.Fatalf("Claude messages TLS/H1 画像未完整下沉：%+v", transport.TLS)
	}
	headerPositions := make(map[string]int)
	for index, name := range transport.TLS.H1HeaderOrders[0].Order {
		headerPositions[strings.ToLower(name)] = index
	}
	orderedNames := []string{
		"x-anthropic-additional-protection", "x-app", "x-claude-remote-container-id",
		"x-claude-remote-session-id", "x-client-app", "x-client-request-id",
		"content-encoding", "connection",
	}
	for index := 1; index < len(orderedNames); index++ {
		if headerPositions[orderedNames[index-1]] >= headerPositions[orderedNames[index]] {
			t.Fatalf("Claude 条件 Header 顺序不一致：%v", transport.TLS.H1HeaderOrders[0].Order)
		}
	}
	if headerPositions["x-claude-code-session-id"] >= headerPositions["x-fw-g-probe"] ||
		headerPositions["x-fw-g-probe"] >= headerPositions["x-stainless-arch"] {
		t.Fatalf("Claude custom Header 插入位置不一致：%v", transport.TLS.H1HeaderOrders[0].Order)
	}

	body := decodeClaudeTestBody(t, message)
	document, err := decodeClaudeUniqueObject(body)
	if err != nil {
		t.Fatal(err)
	}
	if string(document["max_tokens"]) != "1234" ||
		!claudeJSONEqual(document["output_config"], json.RawMessage(`{"effort":"max"}`)) {
		t.Fatalf("Claude max_tokens/effort 条件不一致：%s", body)
	}
	var metadata struct {
		UserID string `json:"user_id"`
	}
	if json.Unmarshal(document["metadata"], &metadata) != nil {
		t.Fatalf("Claude metadata 无法解析：%s", document["metadata"])
	}
	metadataFields, err := decodeClaudeOrderedObject([]byte(metadata.UserID))
	if err != nil {
		t.Fatal(err)
	}
	metadataOrder := make([]string, 0, len(metadataFields))
	for _, field := range metadataFields {
		metadataOrder = append(metadataOrder, field.name)
	}
	if !slices.Equal(metadataOrder, []string{"workspace", "device_id", "account_uuid", "session_id"}) ||
		!strings.Contains(metadata.UserID, `"account_uuid":"`+claudeTestAccountUUID+`"`) ||
		strings.Contains(metadata.UserID, `"device_id":"spoof"`) {
		t.Fatalf("Claude metadata 可信覆盖或顺序不一致：%s", metadata.UserID)
	}
	var system []claudeWireSystemBlock
	if json.Unmarshal(document["system"], &system) != nil || len(system) < 2 ||
		!strings.Contains(system[0].Text, "cc_workload=tool;") {
		t.Fatalf("Claude attribution/system 无法解析：%s", document["system"])
	}
	for index, block := range system {
		if len(bytes.TrimSpace(block.CacheControl)) != 0 {
			t.Fatalf("禁用 prompt cache 后 system[%d] 仍有 cache_control：%s", index, block.CacheControl)
		}
	}
}

func TestClaudeFWGThirdPartyCustomSystemUsesApprovedManagedBranch(t *testing.T) {
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	trusted := claudeTestTrustedFacts()
	body := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"custom system probe"}],"system":"Only use the supplied custom instruction.","tools":[],"stream":true}`)
	canonical, _, err := parseClaudeCanonicalMessages(body, trusted, wire, false)
	if err != nil {
		t.Fatal(err)
	}
	if canonical.systemKind != claudeCanonicalSystemCustom || canonical.scenarioHint != "custom-system" {
		t.Fatalf("第三方 custom system 未进入受管分支：%+v", canonical)
	}
	identity := mustClaudeTestIdentity(t, trusted)
	compile := func(features ClaudeTrustedFeatureFacts) []claudeWireSystemBlock {
		t.Helper()
		compiled, _, _, _, err := compileClaudeMessagesBody(ClaudeEgressPlan{
			endpointKind: "messages-inference", canonical: canonical, identity: identity,
			features: features, modelShape: claudeModelShapeSonnet, cch: "abcde",
		}, wire)
		if err != nil {
			t.Fatal(err)
		}
		document, err := decodeClaudeUniqueObject(compiled)
		if err != nil {
			t.Fatal(err)
		}
		var system []claudeWireSystemBlock
		if err := json.Unmarshal(document["system"], &system); err != nil {
			t.Fatal(err)
		}
		return system
	}
	defaultSystem := compile(ClaudeTrustedFeatureFacts{SystemMode: ClaudeSystemDefault})
	if len(defaultSystem) < 2 ||
		defaultSystem[len(defaultSystem)-1].Text != "Only use the supplied custom instruction." ||
		len(defaultSystem[len(defaultSystem)-1].CacheControl) == 0 ||
		!strings.HasPrefix(defaultSystem[0].Text, "x-anthropic-billing-header:") {
		t.Fatalf("第三方 custom system 未按批准模板合并：%+v", defaultSystem)
	}
	disabledSystem := compile(ClaudeTrustedFeatureFacts{
		SystemMode: ClaudeSystemDefault, DisableAttribution: true, DisablePromptCaching: true,
	})
	if len(disabledSystem) != len(defaultSystem)-1 ||
		disabledSystem[len(disabledSystem)-1].Text != "Only use the supplied custom instruction." {
		t.Fatalf("关闭 attribution 后 custom system 结构不一致：%+v", disabledSystem)
	}
	for index, block := range disabledSystem {
		if len(bytes.TrimSpace(block.CacheControl)) != 0 {
			t.Fatalf("关闭 prompt cache 后 custom system[%d] 仍有 cache_control", index)
		}
	}
}

func TestClaudeFWGOfficialIngressRoundTripsPersonaWire(t *testing.T) {
	firstPort := &claudeCapturePort{}
	firstRuntime, _, _ := newClaudeTestRuntime(t, firstPort)
	first, err := firstRuntime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "test-access-token",
		TrustedFacts: claudeTestTrustedFacts(), InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = first.Response.Body.Close()
	finalizeClaudeTestResult(t, &first)
	officialRequest := findClaudeCapturedRequest(
		t, firstPort.snapshot(), http.MethodPost, "/v1/messages?beta=true",
	)

	secondPort := &claudeCapturePort{}
	secondRuntime, _, _ := newClaudeTestRuntime(t, secondPort)
	trusted := claudeTestTrustedFacts()
	trusted.Session.SessionID = uuid.NewString()
	second, err := secondRuntime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: officialRequest.Body, AccessToken: "test-access-token", TrustedFacts: trusted,
		Ingress: ClaudeIngressSnapshot{
			Captured: true, Headers: claudeTestCanonicalHeaders(officialRequest.Header),
		},
		InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = second.Response.Body.Close()
	finalizeClaudeTestResult(t, &second)
	roundTrip := findClaudeCapturedRequest(
		t, secondPort.snapshot(), http.MethodPost, "/v1/messages?beta=true",
	)
	if !bytes.Equal(roundTrip.Body, officialRequest.Body) {
		t.Fatalf("官方入口 Body 未逐字节往返\nwant=%s\n got=%s", officialRequest.Body, roundTrip.Body)
	}
	for _, name := range []string{
		"Authorization", "User-Agent", "anthropic-beta", "x-app",
		"X-Claude-Code-Session-Id", "x-client-request-id", "x-stainless-timeout",
	} {
		if claudeTestHeaderValue(roundTrip.Header, name) !=
			claudeTestHeaderValue(officialRequest.Header, name) {
			t.Fatalf("官方入口 Header 往返不一致：%s", name)
		}
	}
}

func TestClaudeFWG404FallbackIgnoresStreamInterruptDisableFlag(t *testing.T) {
	messageAttempts := 0
	port := &claudeCapturePort{respond: func(request claudeCapturedRequest, _ int) (*http.Response, error) {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messageAttempts++
			if messageAttempts == 1 {
				return claudeTestResponse(http.StatusNotFound, `{}`), nil
			}
		}
		return claudeTestResponse(http.StatusOK, `{}`), nil
	}}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Features.MaxRetriesSet = true
	trusted.Features.MaxRetries = 0
	trusted.Features.DisableStreamFallback = true
	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token", TrustedFacts: trusted,
		InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	if result.Stream || result.Attempts != 2 || result.StreamFallback != nil {
		t.Fatalf("Claude 404 fallback 语义不一致：%+v", result)
	}
	finalizeClaudeTestResult(t, &result)
	messages := make([]claudeCapturedRequest, 0, 2)
	for _, request := range port.snapshot() {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messages = append(messages, request)
		}
	}
	if len(messages) != 2 || !bytes.Contains(messages[0].Body, []byte(`"stream":true`)) ||
		bytes.Contains(messages[1].Body, []byte(`"stream"`)) ||
		messages[1].Header.Get("x-stainless-timeout") != "300" {
		t.Fatalf("Claude 404 non-stream wire 不一致：%+v", messages)
	}
}

func TestClaudeFWGModelAndStreamFallbackKeepHaikuShape(t *testing.T) {
	messageAttempts := 0
	port := &claudeCapturePort{respond: func(request claudeCapturedRequest, _ int) (*http.Response, error) {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messageAttempts++
			if messageAttempts == 1 {
				return claudeTestResponse(http.StatusInternalServerError, `{}`), nil
			}
		}
		return claudeTestResponse(http.StatusOK, `{}`), nil
	}}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Features.MaxRetriesSet = true
	trusted.Features.MaxRetries = 0
	trusted.Features.FallbackModelEnabled = true
	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token", TrustedFacts: trusted,
		InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = result.Response.Body.Close()
	if !result.Stream || result.Model != "claude-haiku-4-5" ||
		result.Attempts != 2 || result.StreamFallback == nil {
		t.Fatalf("Claude model fallback 语义不一致：%+v", result)
	}
	continuation, err := result.StreamFallback(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = continuation.Response.Body.Close() }()
	if continuation.Stream || continuation.Model != "claude-haiku-4-5" ||
		continuation.Attempts != 3 {
		t.Fatalf("Claude Haiku non-stream continuation 降回错误模型：%+v", continuation)
	}
	finalizeClaudeTestResult(t, &continuation)
	messages := make([]claudeCapturedRequest, 0, 3)
	for _, request := range port.snapshot() {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messages = append(messages, request)
		}
	}
	if len(messages) != 3 ||
		!bytes.Contains(messages[1].Body, []byte(`"model":"claude-haiku-4-5"`)) ||
		!bytes.Contains(messages[1].Body, []byte(`"thinking":{"budget_tokens":31999,"type":"enabled"}`)) ||
		bytes.Contains(messages[1].Body, []byte(`"output_config"`)) ||
		bytes.Contains(messages[2].Body, []byte(`"stream"`)) {
		t.Fatalf("Claude Haiku fallback wire 不一致：%s", messages[1].Body)
	}
}

func TestClaudeFWGStrictAuxiliaryEndpointsUseExecutor(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Entrypoint.IngressProtocol = "managed-internal"

	cases := []ClaudeEndpointExecution{
		{EndpointKind: "lifecycle-hello"},
		{EndpointKind: "policy-limits", AccessToken: "token"},
		{EndpointKind: "remote-settings", AccessToken: "token"},
		{EndpointKind: "oauth-profile", AccessToken: "token"},
		{EndpointKind: "mcp-servers", AccessToken: "token"},
		{EndpointKind: "count-tokens", AccessToken: "token", Body: []byte(
			`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"count"}],"tools":[]}`,
		)},
		{EndpointKind: "oauth-token-refresh", RefreshToken: "<secret-refresh-token>", ClientID: "client-id", RefreshScope: "user:inference"},
	}
	for _, input := range cases {
		input.TrustedFacts = trusted
		input.InvocationID = uuid.NewString()
		response, err := runtime.ExecuteEndpoint(context.Background(), input)
		if err != nil {
			t.Fatalf("strict endpoint %s：%v", input.EndpointKind, err)
		}
		_ = response.Body.Close()
	}
	requests := port.snapshot()
	if len(requests) != len(cases) {
		t.Fatalf("strict endpoint 请求数不一致：%d", len(requests))
	}
	transports := port.transportSnapshot()
	if len(transports) != len(cases) {
		t.Fatalf("strict endpoint transport 数量不一致：%d", len(transports))
	}
	for index, input := range cases {
		endpoint, err := runtime.profile.endpoint(input.EndpointKind)
		if err != nil {
			t.Fatal(err)
		}
		request := requests[index]
		transport := transports[index]
		if request.Method != endpoint.method || request.URL != endpoint.target.String() ||
			transport.TLS.Stack != "node-v26.3.0-openssl" || !transport.TLS.StrictH1Wire ||
			len(transport.TLS.H1HeaderOrders) != 1 ||
			transport.TLS.H1HeaderOrders[0].Method != endpoint.method ||
			transport.TLS.H1HeaderOrders[0].Path != endpoint.target.EscapedPath() ||
			!transport.TLS.H1HeaderOrders[0].RejectUnlisted {
			t.Fatalf("strict endpoint 坐标或 transport 不一致：kind=%s request=%+v tls=%+v", input.EndpointKind, request, transport.TLS)
		}
		vector := runtime.wire.Transports.WithoutALPN
		if endpoint.withALPN {
			vector = runtime.wire.Transports.WithALPN
		}
		if !slices.Equal(transport.TLS.CipherSuites, vector.CipherSuites) ||
			!slices.Equal(transport.TLS.SupportedGroups, vector.SupportedGroups) ||
			!slices.Equal(transport.TLS.SignatureAlgorithms, vector.SignatureAlgorithms) ||
			!slices.Equal(transport.TLS.ALPN, vector.ALPN) ||
			!slices.Equal(transport.TLS.Extensions, vector.Extensions) ||
			!slices.Equal(transport.TLS.SupportedVersions, vector.SupportedVersions) ||
			!slices.Equal(transport.TLS.KeyShareGroups, vector.KeyShareGroups) ||
			!slices.Equal(transport.TLS.PSKModes, vector.PSKModes) ||
			transport.TLS.MinVersion != vector.TLSMinVersion ||
			transport.TLS.MaxVersion != vector.TLSMaxVersion {
			t.Fatalf("strict endpoint TLS 向量不一致：kind=%s", input.EndpointKind)
		}
		wantOrder, err := claudeHeaderOrder(endpoint.headers, false)
		if err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(transport.TLS.H1HeaderOrders[0].Order, wantOrder) {
			t.Fatalf("strict endpoint Header 顺序不一致：kind=%s got=%v want=%v", input.EndpointKind, transport.TLS.H1HeaderOrders[0].Order, wantOrder)
		}
	}
	refresh := findClaudeCapturedRequest(t, requests, http.MethodPost, "/v1/oauth/token")
	if claudeTestHeaderValue(refresh.Header, "Authorization") != "" ||
		!bytes.Equal(refresh.Body, []byte(
			`{"grant_type":"refresh_token","refresh_token":"<secret-refresh-token>","client_id":"client-id","scope":"user:inference"}`,
		)) {
		t.Fatalf("Claude OAuth refresh wire 不一致：headers=%v body=%s", refresh.Header, refresh.Body)
	}
	count := findClaudeCapturedRequest(t, requests, http.MethodPost, "/v1/messages/count_tokens?beta=true")
	var countBody map[string]json.RawMessage
	if json.Unmarshal(count.Body, &countBody) != nil ||
		string(countBody["model"]) != `"claude-sonnet-5"` ||
		claudeTestHeaderValue(count.Header, "x-client-request-id") != claudeTestRequestID {
		t.Fatalf("Claude count_tokens wire 不一致：headers=%v body=%s", count.Header, count.Body)
	}
	_, err := runtime.ExecuteEndpoint(context.Background(), ClaudeEndpointExecution{
		EndpointKind: "unknown-oauth-route", TrustedFacts: trusted,
	})
	if err == nil {
		t.Fatal("未知 Claude strict endpoint 未 fail-close")
	}
}

func TestClaudeFWGCountTokensOfficialIngressIsClosed(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	trusted.Entrypoint.IngressProtocol = "managed-internal"
	endpoint, err := runtime.profile.endpoint("count-tokens")
	if err != nil {
		t.Fatal(err)
	}
	headers := make(http.Header)
	for _, fact := range endpoint.headers.Facts {
		if fact.Value != "" {
			headers.Set(fact.Name, fact.Value)
		}
	}
	sessionID := "77777777-7777-4777-8777-777777777777"
	headers.Set("X-Claude-Code-Session-Id", sessionID)
	headers.Set("x-client-request-id", "88888888-8888-4888-8888-888888888888")
	body := []byte(
		`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"count"}],"tools":[]}`,
	)
	result, err := runtime.ExecuteCountTokens(context.Background(), ClaudeEndpointExecution{
		Body: body, AccessToken: "token", TrustedFacts: trusted,
		Ingress:      ClaudeIngressSnapshot{Captured: true, Headers: headers},
		InvocationID: "99999999-9999-4999-8999-999999999999",
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = result.Response.Body.Close()
	requests := port.snapshot()
	if len(requests) != 1 ||
		claudeTestHeaderValue(requests[0].Header, "X-Claude-Code-Session-Id") != sessionID ||
		!bytes.Equal(result.WireBody, requests[0].Body) {
		t.Fatalf("Claude 官方 count_tokens 未保持一致会话或 final wire：%+v", requests)
	}

	_, err = runtime.ExecuteCountTokens(context.Background(), ClaudeEndpointExecution{
		Body:        []byte(`{"model":"claude-haiku-4-5","messages":[],"tools":[]}`),
		AccessToken: "token", TrustedFacts: trusted,
	})
	if err == nil || !strings.Contains(err.Error(), "model 不在 SupportEnvelope") {
		t.Fatalf("Claude count_tokens 范围外模型未 fail-close：%v", err)
	}
	headers.Set("x-app", "cli-bg")
	_, err = runtime.ExecuteCountTokens(context.Background(), ClaudeEndpointExecution{
		Body: body, AccessToken: "token", TrustedFacts: trusted,
		Ingress: ClaudeIngressSnapshot{Captured: true, Headers: headers},
	})
	if err == nil || !strings.Contains(err.Error(), "固定 Header 不一致") {
		t.Fatalf("Claude 官方 count_tokens 畸形声明未 fail-close：%v", err)
	}
}

func TestClaudeFWGToolPolicyIsClosed(t *testing.T) {
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	policy := wire.ImplementationPolicy.ToolPolicy
	var deferredTools []json.RawMessage
	if err := json.Unmarshal(policy.MCPDeferred.Tools, &deferredTools); err != nil {
		t.Fatal(err)
	}
	if len(deferredTools) != 33 {
		t.Fatalf("Claude MCP deferred 全目录被截断：%d", len(deferredTools))
	}
	for name, catalog := range map[string]claudeWireToolCatalog{
		"agent": policy.Agent, "bash": policy.Bash, "mcp": policy.MCPDeferred,
		"advisor": policy.Advisor, "background": policy.Background,
		"web-search-outer": policy.WebSearchOuter, "web-search-server": policy.WebSearchServer,
	} {
		if _, _, _, err := compileClaudeApprovedTools(
			catalog.Tools, catalog.ToolChoice, wire,
		); err != nil {
			t.Fatalf("批准工具 %s 被拒绝：%v", name, err)
		}
	}
	_, _, _, err = compileClaudeApprovedTools(
		json.RawMessage(`[{"name":"UnknownTool","input_schema":{"type":"object"}}]`), nil, wire,
	)
	if err == nil {
		t.Fatal("缺少描述的未知 Claude 工具未 fail-close")
	}
	dynamic := json.RawMessage(`[{"name":"CronCreate","description":"Create a cron job","input_schema":{"type":"object","properties":{"schedule":{"type":"string"}},"required":["schedule"]}},{"name":"mcp__desktop__custom_tool","description":"Desktop MCP tool","input_schema":{"type":"object","properties":{}}}]`)
	compiledDynamic, dynamicChoice, dynamicMode, err := compileClaudeApprovedTools(dynamic, nil, wire)
	if err != nil || dynamicMode != claudeToolModeDynamic || len(dynamicChoice) != 0 ||
		!bytes.Equal(compiledDynamic, dynamic) {
		t.Fatalf("标准动态工具目录没有无损进入受管语义层：mode=%s err=%v", dynamicMode, err)
	}
	_, _, _, err = compileClaudeApprovedTools(
		json.RawMessage(`[{"name":"CronCreate","description":"one","input_schema":{"type":"object"}},{"name":"CronCreate","description":"two","input_schema":{"type":"object"}}]`),
		nil,
		wire,
	)
	if err == nil {
		t.Fatal("重复名称的动态工具目录未 fail-close")
	}
	_, _, _, err = compileClaudeApprovedTools(
		json.RawMessage(`[{"name":"future.tool","description":"future","input_schema":{"type":"object"}}]`),
		nil,
		wire,
	)
	if err == nil {
		t.Fatal("超出官方名称模式的动态工具未 fail-close")
	}
	_, _, _, err = compileClaudeApprovedTools(
		json.RawMessage(`[{"name":"Deferred","description":"deferred","input_schema":{"type":"object"},"defer_loading":true}]`),
		nil,
		wire,
	)
	if err == nil {
		t.Fatal("未命中批准目录的 deferred 工具未 fail-close")
	}
	oversized := make([]map[string]any, 0, claudeDynamicToolCatalogLimit+1)
	for index := 0; index <= claudeDynamicToolCatalogLimit; index++ {
		oversized = append(oversized, map[string]any{
			"name": fmt.Sprintf("Dynamic%d", index), "description": "dynamic",
			"input_schema": map[string]any{"type": "object"},
		})
	}
	oversizedRaw, marshalErr := json.Marshal(oversized)
	if marshalErr != nil {
		t.Fatal(marshalErr)
	}
	_, _, _, err = compileClaudeApprovedTools(oversizedRaw, nil, wire)
	if err == nil {
		t.Fatal("超过已实测上限的动态工具目录未 fail-close")
	}
	_, _, _, err = compileClaudeApprovedTools(
		json.RawMessage(`[{"type":"future_server_tool","name":"future"}]`), nil, wire,
	)
	if err == nil {
		t.Fatal("未批准的特殊 server tool 未 fail-close")
	}

	advisorBody, err := marshalClaudeOrderedObject([]claudeJSONField{
		{name: "model", raw: mustMarshalClaudeString(
			wire.ImplementationPolicy.Scenarios.SDKCLI.Model,
		)},
		{name: "messages", raw: json.RawMessage(`[{"role":"user","content":"advisor"}]`)},
		{name: "tools", raw: policy.Advisor.Tools},
		{name: "tool_choice", raw: policy.Advisor.ToolChoice},
		{name: "stream", raw: json.RawMessage("true")},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = parseClaudeCanonicalMessages(
		advisorBody, claudeTestTrustedFacts(), wire, false,
	)
	if err == nil || !strings.Contains(err.Error(), "可信 feature") {
		t.Fatalf("第三方入站可通过复制官方 JSON 冒充 advisor：%v", err)
	}
	canonical, _, err := parseClaudeCanonicalMessages(
		advisorBody, claudeTestTrustedFacts(), wire, true,
	)
	if err != nil {
		t.Fatal(err)
	}
	identity, err := deriveClaudeIdentityFacts(claudeTestTrustedFacts())
	if err != nil {
		t.Fatal(err)
	}
	plan := ClaudeEgressPlan{
		endpointKind: "messages-inference", canonical: canonical, identity: identity,
		features:   ClaudeTrustedFeatureFacts{AdvisorEnabled: true},
		modelShape: claudeModelShapeSonnet, cch: "abcde",
	}
	if _, _, _, _, err := compileClaudeMessagesBody(plan, wire); err != nil {
		t.Fatalf("可信 advisor feature 没有选择批准 ToolPolicy：%v", err)
	}
	plan.features.AdvisorEnabled = false
	if _, _, _, _, err := compileClaudeMessagesBody(plan, wire); err == nil {
		t.Fatal("advisor 工具在缺少可信 feature 时通过最终 Compiler")
	}

	webSearchBody, err := marshalClaudeOrderedObject([]claudeJSONField{
		{name: "model", raw: mustMarshalClaudeString(
			wire.ImplementationPolicy.Scenarios.WebSearchServer.Model,
		)},
		{name: "messages", raw: json.RawMessage(`[{"role":"user","content":"search"}]`)},
		{name: "tools", raw: policy.WebSearchServer.Tools},
		{name: "tool_choice", raw: policy.WebSearchServer.ToolChoice},
		{name: "stream", raw: json.RawMessage("true")},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = parseClaudeCanonicalMessages(
		webSearchBody, claudeTestTrustedFacts(), wire, false,
	)
	if err == nil || !strings.Contains(err.Error(), "可信 feature") {
		t.Fatalf("第三方入站可通过复制官方 JSON 冒充 server web_search：%v", err)
	}
	canonical, _, err = parseClaudeCanonicalMessages(
		webSearchBody, claudeTestTrustedFacts(), wire, true,
	)
	if err != nil {
		t.Fatal(err)
	}
	plan.canonical = canonical
	plan.features = ClaudeTrustedFeatureFacts{WebSearchServerEnabled: true}
	if _, _, _, _, err := compileClaudeMessagesBody(plan, wire); err != nil {
		t.Fatalf("可信 server web_search feature 没有选择批准 ToolPolicy：%v", err)
	}
	plan.features.WebSearchServerEnabled = false
	if _, _, _, _, err := compileClaudeMessagesBody(plan, wire); err == nil {
		t.Fatal("server web_search 工具在缺少可信 feature 时通过最终 Compiler")
	}
}

func TestClaudeFWGRejectsMalformedOfficialClaimAndLossyThirdParty(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	trusted := claudeTestTrustedFacts()
	_, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token", TrustedFacts: trusted,
		Ingress: ClaudeIngressSnapshot{Captured: true, Headers: http.Header{
			"User-Agent": []string{"claude-cli/not-approved"},
		}},
	})
	if err == nil {
		t.Fatal("伪造或畸形 Claude 官方声明未 fail-close")
	}
	if !IsClaudeSupportEnvelopeRejection(err) {
		t.Fatalf("畸形官方声明没有稳定范围拒绝类型：%T %v", err, err)
	}

	lossy := []byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":"x"}],"stream":true,"unsupported":true}`)
	_, err = runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: lossy, AccessToken: "token", TrustedFacts: trusted,
	})
	if err == nil || !strings.Contains(err.Error(), "lossless SupportEnvelope") {
		t.Fatalf("有损第三方请求未隔离：%v", err)
	}
	if !IsClaudeSupportEnvelopeRejection(err) {
		t.Fatalf("有损第三方请求没有稳定范围拒绝类型：%T %v", err, err)
	}
}

func TestClaudeFWGTransportErrorIsRetryableWithoutStateReuse(t *testing.T) {
	messageAttempts := 0
	port := &claudeCapturePort{respond: func(request claudeCapturedRequest, _ int) (*http.Response, error) {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messageAttempts++
			if messageAttempts == 1 {
				return nil, errors.New("synthetic transport error")
			}
		}
		return claudeTestResponse(http.StatusOK, `{}`), nil
	}}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	requestIDs := []string{
		"44444444-4444-4444-8444-444444444441",
		"44444444-4444-4444-8444-444444444442",
	}
	requestIDIndex := 0
	runtime.newRequestID = func() string {
		id := requestIDs[requestIDIndex]
		requestIDIndex++
		return id
	}
	compiler := runtime.executor.dialects.compilers[PersonaClaudeCode].(claudeDialectCompiler)
	compiler.newRequestID = runtime.newRequestID
	runtime.executor.dialects.compilers[PersonaClaudeCode] = compiler
	trusted := claudeTestTrustedFacts()
	trusted.Features.MaxRetriesSet = true
	trusted.Features.MaxRetries = 1
	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token", TrustedFacts: trusted,
		InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	messages := make([]claudeCapturedRequest, 0, 2)
	for _, request := range port.snapshot() {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messages = append(messages, request)
		}
	}
	if len(messages) != 2 || !bytes.Equal(messages[0].Body, messages[1].Body) ||
		claudeTestHeaderValue(messages[0].Header, "Connection") != "keep-alive" ||
		claudeTestHeaderValue(messages[1].Header, "Connection") != "" ||
		claudeTestHeaderValue(messages[0].Header, "x-client-request-id") ==
			claudeTestHeaderValue(messages[1].Header, "x-client-request-id") ||
		claudeTestHeaderValue(messages[0].Header, "x-client-request-id") != requestIDs[0] ||
		claudeTestHeaderValue(messages[1].Header, "x-client-request-id") != requestIDs[1] {
		t.Fatalf("transport retry 没有复用 Body、刷新 request-id 或去除 Connection：%+v", messages)
	}
	finalizeClaudeTestResult(t, &result)
}

func TestClaudeFWGRetryStatusBudgetAndRetryAfterPolicyAreClosed(t *testing.T) {
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	runtime := &ClaudeCandidateRuntime{wire: wire}
	wantRetryable := map[int]struct{}{
		401: {}, 408: {}, 409: {}, 429: {}, 500: {}, 502: {}, 503: {}, 529: {},
	}
	for status := 100; status <= 599; status++ {
		_, want := wantRetryable[status]
		if got := runtime.claudeRetryableStatus(status); got != want {
			t.Fatalf("Claude retry 状态分类不一致：status=%d got=%t want=%t", status, got, want)
		}
	}
	jitterMax := -1
	runtime.retryJitter = func(maxInclusive int) (int, error) {
		jitterMax = maxInclusive
		return 7, nil
	}
	response := claudeTestResponse(http.StatusTooManyRequests, `{}`)
	response.Header.Set("Retry-After", "2")
	delay, err := runtime.claudeRetryDelay(response, 1)
	if err != nil {
		t.Fatal(err)
	}
	if delay != 2007*time.Millisecond || jitterMax != 100 {
		t.Fatalf("Retry-After 秒值策略不一致：delay=%s jitter_max=%d", delay, jitterMax)
	}
	response.Header.Set("Retry-After", time.Now().Add(time.Minute).UTC().Format(http.TimeFormat))
	delay, err = runtime.claudeRetryDelay(response, 1)
	if err != nil {
		t.Fatal(err)
	}
	if delay != 507*time.Millisecond || jitterMax != 250 {
		t.Fatalf("Retry-After HTTP-date 未按批准策略回退：delay=%s jitter_max=%d", delay, jitterMax)
	}
	delay, err = runtime.claudeRetryDelay(nil, 2)
	if err != nil {
		t.Fatal(err)
	}
	if delay != 1007*time.Millisecond {
		t.Fatalf("第二次默认退避不一致：%s", delay)
	}
}

func TestClaudeFWGToolUseResultRelationsAreClosed(t *testing.T) {
	tests := []struct {
		name        string
		tools       string
		messages    string
		wantWeb     bool
		wantErrPart string
	}{
		{
			name:  "bash round trip",
			tools: `[{"name":"Bash"}]`,
			messages: `[
				{"role":"assistant","content":[{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{"command":"pwd"}}]},
				{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_A1","content":"ok"}]}
			]`,
		},
		{
			name:  "web search round trip",
			tools: `[{"name":"WebSearch"}]`,
			messages: `[
				{"role":"assistant","content":[{"type":"tool_use","id":"toolu_Web1","name":"WebSearch","input":{"query":"test"}}]},
				{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_Web1","content":"result"}]}
			]`,
			wantWeb: true,
		},
		{
			name:  "result id mismatch",
			tools: `[{"name":"Bash"}]`,
			messages: `[
				{"role":"assistant","content":[{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{}}]},
				{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_A2","content":"bad"}]}
			]`,
			wantErrPart: "缺少同 ID 的先行 tool_use",
		},
		{
			name:  "duplicate use id",
			tools: `[{"name":"Bash"}]`,
			messages: `[
				{"role":"assistant","content":[
					{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{}},
					{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{}}
				]}
			]`,
			wantErrPart: "tool_use id 重复",
		},
		{
			name:  "duplicate result consumption",
			tools: `[{"name":"Bash"}]`,
			messages: `[
				{"role":"assistant","content":[{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{}}]},
				{"role":"user","content":[
					{"type":"tool_result","tool_use_id":"toolu_A1","content":"one"},
					{"type":"tool_result","tool_use_id":"toolu_A1","content":"two"}
				]}
			]`,
			wantErrPart: "tool_result id 重复消费",
		},
		{
			name:        "unknown tool",
			tools:       `[{"name":"Bash"}]`,
			messages:    `[{"role":"assistant","content":[{"type":"tool_use","id":"toolu_A1","name":"UnknownTool","input":{}}]}]`,
			wantErrPart: "未绑定当前批准目录",
		},
		{
			name:        "tool use wrong role",
			tools:       `[{"name":"Bash"}]`,
			messages:    `[{"role":"user","content":[{"type":"tool_use","id":"toolu_A1","name":"Bash","input":{}}]}]`,
			wantErrPart: "tool_use 必须位于 assistant",
		},
		{
			name:        "tool result wrong role",
			tools:       `[{"name":"Bash"}]`,
			messages:    `[{"role":"assistant","content":[{"type":"tool_result","tool_use_id":"toolu_A1","content":"bad"}]}]`,
			wantErrPart: "tool_result 必须位于 user",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			relations, err := validateClaudeMessageRelations(ClaudeCanonicalRequest{
				tools: json.RawMessage(test.tools), messages: json.RawMessage(test.messages),
			})
			if test.wantErrPart != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantErrPart) {
					t.Fatalf("未按预期拒绝工具往返：%v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if relations.webSearchRoundTrip != test.wantWeb {
				t.Fatalf("WebSearch 往返识别不一致：%+v", relations)
			}
		})
	}
}

func TestClaudeFWGSessionLineagesAndConcurrencyAreIndependent(t *testing.T) {
	runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
	mainIdentity := mustClaudeTestIdentity(t, claudeTestTrustedFacts())
	mainLease, err := runtime.prepareClaudeSessionRequest(
		&mainIdentity, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	concurrentIdentity := mustClaudeTestIdentity(t, claudeTestTrustedFacts())
	if _, err := runtime.prepareClaudeSessionRequest(
		&concurrentIdentity, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "并发推理请求") {
		t.Fatalf("同谱系并发请求未 fail-close：%v", err)
	}
	finalizeClaudeTestLease(t, mainLease, "req_Main1")

	agentOneFacts := claudeTestTrustedFacts()
	agentOneFacts.Agent = ClaudeTrustedAgentLineageFacts{AgentID: "aaaaaaaaaaaaaaaaa", Depth: 1}
	agentOne := mustClaudeTestIdentity(t, agentOneFacts)
	agentOneLease, err := runtime.prepareClaudeSessionRequest(
		&agentOne, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if agentOne.previousRequestID != "" {
		t.Fatalf("agent 谱系继承了主谱系 previous request：%s", agentOne.previousRequestID)
	}
	finalizeClaudeTestLease(t, agentOneLease, "req_Agent1")

	agentTwoFacts := claudeTestTrustedFacts()
	agentTwoFacts.Agent = ClaudeTrustedAgentLineageFacts{AgentID: "bbbbbbbbbbbbbbbbb", Depth: 1}
	agentTwo := mustClaudeTestIdentity(t, agentTwoFacts)
	agentTwoLease, err := runtime.prepareClaudeSessionRequest(
		&agentTwo, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if agentTwo.previousRequestID != "" {
		t.Fatalf("不同 agent-id 发生 previous request 串线：%s", agentTwo.previousRequestID)
	}
	if err := agentTwoLease.runtime.finalizeClaudeSessionRequest(
		agentTwoLease, false, 0, "",
	); err != nil {
		t.Fatal(err)
	}

	mainFollowUp := mustClaudeTestIdentity(t, claudeTestTrustedFacts())
	mainFollowUpLease, err := runtime.prepareClaudeSessionRequest(
		&mainFollowUp, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if mainFollowUp.previousRequestID != "req_Main1" {
		t.Fatalf("主谱系 previous request 被 agent 覆盖：%s", mainFollowUp.previousRequestID)
	}
	if err := mainFollowUpLease.runtime.finalizeClaudeSessionRequest(
		mainFollowUpLease, false, 0, "",
	); err != nil {
		t.Fatal(err)
	}
}

func TestClaudeFWGAgentLineageRequiresAcceptedParentAndMaxThreeLevels(t *testing.T) {
	runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
	newAgentIdentity := func(agentID, parentID string, depth int) ClaudeIdentityFacts {
		facts := claudeTestTrustedFacts()
		facts.Session.Source = ClaudeSessionSourceOfficialConsistent
		facts.Agent = ClaudeTrustedAgentLineageFacts{
			AgentID: agentID, ParentAgentID: parentID, Depth: depth,
		}
		return mustClaudeTestIdentity(t, facts)
	}
	orphan := newAgentIdentity("bbbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaaa", 2)
	if _, err := runtime.prepareClaudeSessionRequest(
		&orphan, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "父谱系尚未") {
		t.Fatalf("孤儿 agent 父链未 fail-close：%v", err)
	}

	root := newAgentIdentity("aaaaaaaaaaaaaaaaa", "", 1)
	rootLease, err := runtime.prepareClaudeSessionRequest(
		&root, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	finalizeClaudeTestLease(t, rootLease, "req_AgentRoot")

	child := newAgentIdentity("bbbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaaa", 2)
	childLease, err := runtime.prepareClaudeSessionRequest(
		&child, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	finalizeClaudeTestLease(t, childLease, "req_AgentChild")

	grandchild := newAgentIdentity("ccccccccccccccccc", "bbbbbbbbbbbbbbbbb", 2)
	grandchildLease, err := runtime.prepareClaudeSessionRequest(
		&grandchild, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if grandchild.agentDepth != 3 || grandchildLease.agentDepth != 3 {
		t.Fatalf("三级 agent 未按已接受父链推导：identity=%d lease=%d", grandchild.agentDepth, grandchildLease.agentDepth)
	}
	finalizeClaudeTestLease(t, grandchildLease, "req_AgentGrandchild")

	fourth := newAgentIdentity("ddddddddddddddddd", "ccccccccccccccccc", 2)
	if _, err := runtime.prepareClaudeSessionRequest(
		&fourth, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "超出已批准三级") {
		t.Fatalf("第四层 agent 未 fail-close：%v", err)
	}
}

func TestClaudeFWGForkRequiresNewSessionAndTracksRequestOwnership(t *testing.T) {
	runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
	baseIdentity := mustClaudeTestIdentity(t, claudeTestTrustedFacts())
	baseLease, err := runtime.prepareClaudeSessionRequest(
		&baseIdentity, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	finalizeClaudeTestLease(t, baseLease, "req_BaseSession")

	reusedFacts := claudeTestTrustedFacts()
	reusedFacts.Session.Forked = true
	reusedFacts.Session.PreviousRequestID = "req_BaseSession"
	reused := mustClaudeTestIdentity(t, reusedFacts)
	if _, err := runtime.prepareClaudeSessionRequest(
		&reused, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "新的 Session-Id") {
		t.Fatalf("fork 复用旧 Session-Id 未 fail-close：%v", err)
	}

	forkFacts := claudeTestTrustedFacts()
	forkFacts.Session.SessionID = "66666666-6666-4666-8666-666666666666"
	forkFacts.Session.Source = ClaudeSessionSourceOfficialConsistent
	forkFacts.Session.PreviousRequestID = "req_BaseSession"
	fork := mustClaudeTestIdentity(t, forkFacts)
	forkLease, err := runtime.prepareClaudeSessionRequest(
		&fork, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatalf("新 Session 的 fork 被拒绝：%v", err)
	}
	if !fork.forked {
		t.Fatal("跨 Session previous request 未被识别为 fork")
	}
	finalizeClaudeTestLease(t, forkLease, "req_ForkSession")

	crossFacts := claudeTestTrustedFacts()
	crossFacts.Session.SessionID = forkFacts.Session.SessionID
	crossFacts.Session.Source = ClaudeSessionSourceOfficialConsistent
	crossFacts.Session.PreviousRequestID = "req_BaseSession"
	cross := mustClaudeTestIdentity(t, crossFacts)
	if _, err := runtime.prepareClaudeSessionRequest(
		&cross, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "跨入既有会话") {
		t.Fatalf("既有会话跨 Session 串用 previous request 未 fail-close：%v", err)
	}
}

func TestClaudeFWGTUITitleMustPrecedeMainRequest(t *testing.T) {
	runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
	facts := claudeTestTrustedFacts()
	facts.Session.Source = ClaudeSessionSourceOfficialConsistent
	facts.Entrypoint.Entrypoint = ClaudeEntrypointCLI

	mainBeforeTitle := mustClaudeTestIdentity(t, facts)
	if _, err := runtime.prepareClaudeSessionRequest(
		&mainBeforeTitle, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	); err == nil || !strings.Contains(err.Error(), "缺少同会话标题阶段") {
		t.Fatalf("TUI 主请求未被标题阶段约束：%v", err)
	}
	titleIdentity := mustClaudeTestIdentity(t, facts)
	titleLease, err := runtime.prepareClaudeSessionRequest(
		&titleIdentity, ClaudeCanonicalRequest{scenarioHint: "tui-title"}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatal(err)
	}
	finalizeClaudeTestLease(t, titleLease, "req_TUITitle")

	mainAfterTitle := mustClaudeTestIdentity(t, facts)
	mainLease, err := runtime.prepareClaudeSessionRequest(
		&mainAfterTitle, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatalf("TUI title→main 合法顺序被拒绝：%v", err)
	}
	if err := mainLease.runtime.finalizeClaudeSessionRequest(mainLease, false, 0, ""); err != nil {
		t.Fatal(err)
	}
}

func TestClaudeFWGWebSearchRequiresOuterServerContinuationOrder(t *testing.T) {
	newFacts := func(previous string) ClaudeTrustedFacts {
		facts := claudeTestTrustedFacts()
		facts.Session.Source = ClaudeSessionSourceOfficialConsistent
		facts.Session.PreviousRequestID = previous
		return facts
	}
	t.Run("server without outer", func(t *testing.T) {
		runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
		server := mustClaudeTestIdentity(t, newFacts(""))
		if _, err := runtime.prepareClaudeSessionRequest(
			&server, ClaudeCanonicalRequest{toolMode: claudeToolModeWebSearchServer},
			claudeMessageRelations{},
		); err == nil || !strings.Contains(err.Error(), "缺少同会话外层父请求") {
			t.Fatalf("孤立 server web_search 未 fail-close：%v", err)
		}
	})
	t.Run("complete ordered chain", func(t *testing.T) {
		runtime := &ClaudeCandidateRuntime{sessions: make(map[string]*claudeSessionState)}
		outer := mustClaudeTestIdentity(t, newFacts(""))
		outerLease, err := runtime.prepareClaudeSessionRequest(
			&outer, ClaudeCanonicalRequest{toolMode: claudeToolModeWebSearchOuter},
			claudeMessageRelations{},
		)
		if err != nil {
			t.Fatal(err)
		}
		finalizeClaudeTestLease(t, outerLease, "req_Outer")

		continuationBeforeServer := mustClaudeTestIdentity(t, newFacts("req_Outer"))
		if _, err := runtime.prepareClaudeSessionRequest(
			&continuationBeforeServer, ClaudeCanonicalRequest{},
			claudeMessageRelations{webSearchRoundTrip: true},
		); err == nil || !strings.Contains(err.Error(), "缺少已完成的 server") {
			t.Fatalf("未经过 server 的 continuation 未 fail-close：%v", err)
		}

		server := mustClaudeTestIdentity(t, newFacts(""))
		serverLease, err := runtime.prepareClaudeSessionRequest(
			&server, ClaudeCanonicalRequest{toolMode: claudeToolModeWebSearchServer},
			claudeMessageRelations{},
		)
		if err != nil {
			t.Fatal(err)
		}
		finalizeClaudeTestLease(t, serverLease, "req_Server")

		duplicateServer := mustClaudeTestIdentity(t, newFacts(""))
		if _, err := runtime.prepareClaudeSessionRequest(
			&duplicateServer, ClaudeCanonicalRequest{toolMode: claudeToolModeWebSearchServer},
			claudeMessageRelations{},
		); err == nil {
			t.Fatal("重复 server web_search 未 fail-close")
		}
		wrongParent := mustClaudeTestIdentity(t, newFacts("req_Server"))
		if _, err := runtime.prepareClaudeSessionRequest(
			&wrongParent, ClaudeCanonicalRequest{},
			claudeMessageRelations{webSearchRoundTrip: true},
		); err == nil {
			t.Fatal("continuation 错把 server response 当作 cc_prev_req")
		}

		continuation := mustClaudeTestIdentity(t, newFacts("req_Outer"))
		continuationLease, err := runtime.prepareClaudeSessionRequest(
			&continuation, ClaudeCanonicalRequest{},
			claudeMessageRelations{webSearchRoundTrip: true},
		)
		if err != nil {
			t.Fatalf("outer→server→continuation 合法顺序被拒绝：%v", err)
		}
		finalizeClaudeTestLease(t, continuationLease, "req_Continuation")
	})
}

func TestClaudeFWGStreamFallbackCommitsFallbackResponseID(t *testing.T) {
	messageAttempt := 0
	port := &claudeCapturePort{respond: func(request claudeCapturedRequest, _ int) (*http.Response, error) {
		response := claudeTestResponse(http.StatusOK, `{}`)
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messageAttempt++
			if messageAttempt == 1 {
				response.Header.Set("Request-Id", "req_Stream")
			} else {
				response.Header.Set("Request-Id", "req_Fallback")
			}
		}
		return response, nil
	}}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	first, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token",
		TrustedFacts: claudeTestTrustedFacts(), InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = first.Response.Body.Close()
	fallback, err := first.StreamFallback(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	_ = fallback.Response.Body.Close()
	finalizeClaudeTestResult(t, &fallback)

	followUp, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: claudeTestMessagesBody(), AccessToken: "token",
		TrustedFacts: claudeTestTrustedFacts(), InvocationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = followUp.Response.Body.Close()
	defer func() { _ = followUp.FinalizeSession(false) }()
	messages := make([]claudeCapturedRequest, 0, 3)
	for _, request := range port.snapshot() {
		if strings.HasSuffix(request.URL, "/v1/messages?beta=true") {
			messages = append(messages, request)
		}
	}
	if len(messages) != 3 || !bytes.Contains(
		decodeClaudeTestBody(t, messages[2]), []byte("cc_prev_req=req_Fallback;"),
	) || bytes.Contains(
		decodeClaudeTestBody(t, messages[2]), []byte("cc_prev_req=req_Stream;"),
	) {
		t.Fatalf("stream fallback 未提交最终响应 ID：%s", decodeClaudeTestBody(t, messages[2]))
	}
}

func mustClaudeTestIdentity(t *testing.T, facts ClaudeTrustedFacts) ClaudeIdentityFacts {
	t.Helper()
	identity, err := deriveClaudeIdentityFacts(facts)
	if err != nil {
		t.Fatal(err)
	}
	return identity
}

func finalizeClaudeTestLease(t *testing.T, lease claudeSessionLease, requestID string) {
	t.Helper()
	if err := lease.runtime.finalizeClaudeSessionRequest(
		lease, true, http.StatusOK, requestID,
	); err != nil {
		t.Fatal(err)
	}
}
