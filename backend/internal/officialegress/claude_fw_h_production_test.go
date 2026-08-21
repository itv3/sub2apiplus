package officialegress

import "testing"

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
	approval.DeploymentTrafficEnvelope.LogicalIngressIDs = append(
		approval.DeploymentTrafficEnvelope.LogicalIngressIDs,
		"responses-oauth",
	)
	if err := validateClaudeFWHProductionApproval(approval); err == nil {
		t.Fatal("Claude FW-H ApprovalFact 未拒绝 DeploymentTrafficEnvelope 扩大")
	}
}
