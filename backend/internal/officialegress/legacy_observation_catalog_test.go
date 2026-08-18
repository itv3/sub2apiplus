package officialegress

import (
	"context"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"
)

func TestFWELegacyObservationSinksAreBoundWithoutRegisteringClaudePersona(t *testing.T) {
	t.Parallel()

	type observationCase struct {
		sinkID  SinkID
		method  string
		target  string
		backend BackendKind
	}
	cases := []observationCase{
		{SinkClaudeLegacyAccountTest, http.MethodPost, "https://api.anthropic.com/v1/messages?beta=true", BackendHTTPUpstream},
		{SinkClaudeLegacyCookieAuthorize, http.MethodPost, "https://claude.ai/v1/oauth/00000000-0000-0000-0000-000000000000/authorize", BackendReqProfile},
		{SinkClaudeLegacyCookieOrganizations, http.MethodGet, "https://claude.ai/api/organizations", BackendReqProfile},
		{SinkClaudeLegacyMessagesInference, http.MethodPost, "https://api.anthropic.com/v1/messages?beta=true", BackendHTTPUpstream},
		{SinkClaudeLegacyOAuthExchange, http.MethodPost, "https://platform.claude.com/v1/oauth/token", BackendReqProfile},
		{SinkClaudeLegacyOAuthRefresh, http.MethodPost, "https://platform.claude.com/v1/oauth/token", BackendReqProfile},
		{SinkClaudeLegacyTokenCount, http.MethodPost, "https://api.anthropic.com/v1/messages/count_tokens?beta=true", BackendHTTPUpstream},
		{SinkClaudeLegacyUpstreamModels, http.MethodGet, "https://api.anthropic.com/v1/models", BackendHTTPUpstream},
		{SinkClaudeLegacyUsage, http.MethodGet, "https://api.anthropic.com/api/oauth/usage", BackendPlainNetHTTP},
	}

	guard := newTestDefaultGuard(t, &captureGuardRecorder{})
	for _, testCase := range cases {
		t.Run(string(testCase.sinkID), func(t *testing.T) {
			binding, ok := DefaultSinkCatalog().Resolve(testCase.sinkID)
			if !ok || !binding.RuntimeBindable() {
				t.Fatalf("FW-E observation-only Sink 未登记：%s", testCase.sinkID)
			}
			if binding.Persona() != PersonaUnclassified ||
				binding.EndpointEvidence() != EndpointEvidenceExternalPersona ||
				binding.EnforcementState() != SinkStateLegacyObserve ||
				binding.MigrationReceiptDigest() != "" {
				t.Fatalf("FW-E observation-only Sink 越权：%s", testCase.sinkID)
			}

			unbound := httptest.NewRequest(testCase.method, testCase.target, nil)
			unboundDecision := guard.Evaluate(unbound, testCase.backend, WireProtocolHTTP)
			if !unboundDecision.Allow || unboundDecision.Scope != EgressScopeOutOfScope ||
				!slices.Contains(unboundDecision.Reasons, ReasonOutOfScopePassthrough) {
				t.Fatalf("未绑定的同端点其他产品流量被意外纳管：%+v", unboundDecision)
			}

			boundContext, err := DefaultSinkCatalog().StartAttemptContext(
				context.Background(), testCase.sinkID,
			)
			if err != nil {
				t.Fatalf("绑定 FW-E observation-only Sink：%v", err)
			}
			bound := httptest.NewRequest(testCase.method, testCase.target, nil).WithContext(boundContext)
			decision := guard.Evaluate(bound, testCase.backend, WireProtocolHTTP)
			if !decision.Allow || decision.Scope != EgressScopeManaged ||
				decision.SinkState != SinkStateLegacyObserve ||
				!slices.Contains(decision.Reasons, ReasonUnclassifiedPersona) ||
				!slices.Contains(decision.Reasons, ReasonLegacyObservePassthrough) {
				t.Fatalf("FW-E observation-only Sink 未进入 legacy_observe：%+v", decision)
			}
		})
	}

	if Persona("claude-code").Valid() {
		t.Fatal("FW-E 禁止提前登记 Claude Persona")
	}
	if _, ok := DefaultSinkCatalog().Resolve("unclassified.claude.head_hello"); ok {
		t.Fatal("当前没有发送源的 HEAD /api/hello 不得伪造 observation-only Sink")
	}
}

func TestFWELegacyObservationSinkRejectsBoundUnknownRoute(t *testing.T) {
	t.Parallel()
	guard, err := NewGuard(
		GuardConfig{
			UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
			UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		},
		DefaultSinkCatalog(),
		DefaultOfficialRouteCatalog(),
		&captureGuardRecorder{},
	)
	if err != nil {
		t.Fatal(err)
	}
	ctx, err := DefaultSinkCatalog().StartAttemptContext(
		context.Background(), SinkClaudeLegacyMessagesInference,
	)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(
		http.MethodPost,
		"https://api.anthropic.com/v1/unknown",
		nil,
	).WithContext(ctx)
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if decision.Allow || decision.RejectionReason != ReasonUnknownRoute {
		t.Fatalf("带 observation-only SinkID 的未知 route 必须 fail-close：%+v", decision)
	}
}
