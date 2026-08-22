package officialegress

import (
	"context"
	"net/http"
	"testing"
)

func newClaudeProductionTestRuntime(
	t *testing.T,
	port HTTPUpstreamTransportPort,
) (*ClaudeRuntime, SinkCatalog, *Guard) {
	t.Helper()
	catalog, err := ClaudeProductionSinkCatalog(DefaultSinkCatalog())
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
	runtime, err := NewClaudeProductionRuntime(catalog, guard, port)
	if err != nil {
		t.Fatal(err)
	}
	return runtime, catalog, guard
}

func TestClaudeFWHProductionApprovalAndRuntimeAreBound(t *testing.T) {
	approval, err := loadClaudeFWHProductionApproval()
	if err != nil {
		t.Fatal(err)
	}
	if approval.Target.ReleaseArtifactSHA256 != ClaudeFWGReleaseDigest ||
		approval.Target.ReleaseBundleSHA256 != ClaudeFWGBundleDigest {
		t.Fatal("Claude FW-H ApprovalFact 未绑定当前 Release")
	}
	runtime, catalog, _ := newClaudeProductionTestRuntime(t, &claudeCapturePort{})
	if !runtime.IsProductionActive() ||
		runtime.ProductionApprovalDigest() != ClaudeFWHProductionApprovalDigest ||
		len(runtime.RuleIDs()) != 40 {
		t.Fatal("Claude production runtime 没有解析已批准的 40 条规则")
	}
	for _, kind := range claudeStrictEndpointKinds() {
		endpoint, endpointErr := runtime.profile.endpoint(kind)
		if endpointErr != nil {
			t.Fatal(endpointErr)
		}
		binding, ok := catalog.Resolve(endpoint.sinkID)
		if !ok || binding.MigrationChangeset() != claudeFWHProductionChangeset {
			t.Fatalf("Claude production Sink 未绑定 FW-H changeset：%s", endpoint.sinkID)
		}
	}
}

func TestClaudeFWHProductionSharedRoutesRequireExplicitPersonaBinding(t *testing.T) {
	_, catalog, guard := newClaudeProductionTestRuntime(t, &claudeCapturePort{})

	for _, testCase := range []struct {
		path   string
		sinkID SinkID
	}{
		{path: "/v1/messages", sinkID: SinkClaudeSetupTokenMessagesInference},
		{path: "/v1/messages/count_tokens", sinkID: SinkClaudeSetupTokenTokenCount},
	} {
		unbound, err := http.NewRequest(
			http.MethodPost, "https://api.anthropic.com"+testCase.path, nil,
		)
		if err != nil {
			t.Fatal(err)
		}
		unbound.Header.Set("x-api-key", "<api-key>")
		decision := guard.Evaluate(unbound, BackendHTTPUpstream, WireProtocolHTTP)
		if !decision.Allow || decision.Scope != EgressScopeOutOfScope {
			t.Fatalf("共享路由误把未绑定 API Key 纳入 Claude Persona：%s %+v", testCase.path, decision)
		}

		boundContext, err := catalog.StartAttemptContext(context.Background(), testCase.sinkID)
		if err != nil {
			t.Fatal(err)
		}
		bound, err := http.NewRequestWithContext(
			boundContext, http.MethodPost, "https://api.anthropic.com"+testCase.path, nil,
		)
		if err != nil {
			t.Fatal(err)
		}
		decision = guard.Evaluate(bound, BackendHTTPUpstream, WireProtocolHTTP)
		if !decision.Allow || decision.Scope != EgressScopeManaged ||
			decision.SinkState != SinkStateEnforced {
			t.Fatalf("已绑定 Setup Token 未进入 non_persona_managed：%s %+v", testCase.path, decision)
		}
	}
}

func TestClaudeCandidateAndProductionCatalogCannotBeCrossUsed(t *testing.T) {
	candidateCatalog, err := ClaudeFWGCandidateSinkCatalog(DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	candidateRoutes, err := NewOfficialRouteCatalog(candidateCatalog)
	if err != nil {
		t.Fatal(err)
	}
	candidateGuard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		CanaryPercent:          100,
	}, candidateCatalog, candidateRoutes, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := NewClaudeProductionRuntime(
		candidateCatalog, candidateGuard, &claudeCapturePort{},
	); err == nil {
		t.Fatal("production runtime 错误接受了 Candidate Catalog")
	}

	productionRuntime, productionCatalog, productionGuard :=
		newClaudeProductionTestRuntime(t, &claudeCapturePort{})
	if _, err := NewClaudeCandidateRuntime(
		productionCatalog, productionGuard, &claudeCapturePort{},
	); err == nil {
		t.Fatal("Candidate runtime 错误接受了 production Catalog")
	}
	if productionRuntime.ProductionApprovalDigest() == "" {
		t.Fatal("production runtime 缺少独立 ApprovalFact")
	}
}

func TestClaudeFWHApprovalRejectsEnvelopeOrReleaseDrift(t *testing.T) {
	approval, err := loadClaudeFWHProductionApproval()
	if err != nil {
		t.Fatal(err)
	}
	approval.Target.ReleaseArtifactSHA256 = "0000000000000000000000000000000000000000000000000000000000000000"
	if err := validateClaudeFWHProductionApproval(approval); err == nil {
		t.Fatal("Claude FW-H ApprovalFact 未拒绝 Release 漂移")
	}
	approval, err = loadClaudeFWHProductionApproval()
	if err != nil {
		t.Fatal(err)
	}
	approval.DeploymentTrafficEnvelope.LogicalIngressIDs =
		approval.DeploymentTrafficEnvelope.LogicalIngressIDs[:1]
	if err := validateClaudeFWHProductionApproval(approval); err == nil {
		t.Fatal("Claude FW-H ApprovalFact 未拒绝 DeploymentTrafficEnvelope 扩大")
	}
}
