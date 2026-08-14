package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

const versionRouteReceiptOutputEnv = "VERSION_ROUTE_RECEIPT_OUTPUT"

// TestGenerateVersionRouteReceipt 只向显式临时目录输出候选收据。受审资产必须先经
// 人工检查与 secret scan，再用独立补丁提升到 catalogdata。
func TestGenerateVersionRouteReceipt(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(versionRouteReceiptOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定版本 route 收据临时目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("版本 route 收据输出目录必须是绝对路径")
	}
	manifest, artifacts := buildVersionRouteReceiptCandidate(t)
	writeVersionRouteCandidate(t, output, "version-route-migration-receipts.json", marshalVersionRouteCandidate(t, manifest))
	for path, raw := range artifacts {
		writeVersionRouteCandidate(t, output, path, raw)
	}
}

func buildVersionRouteReceiptCandidate(t *testing.T) (versionRouteReceiptManifest, map[string][]byte) {
	t.Helper()
	base, ok := DefaultSinkCatalog().Resolve(SinkCodexQuotaWHAM)
	if !ok || base.migrationReceipt == nil || base.EnforcementState() != SinkStateEnforced {
		t.Fatal("WHAM 基线不是带收据的 enforced binding")
	}
	input := sinkBindingInputForVersionRoute(base)
	route := CatalogRoute{Key: RouteKey{
		Method: "GET", Host: "chatgpt.com", Path: "/backend-api/wham/settings/user",
		Purpose: base.Purpose(),
	}, Protocol: WireProtocolHTTP}
	priorReceipt := *base.migrationReceipt
	priorReceipt.routeClaims = append([]migrationRouteClaim(nil), priorReceipt.routeClaims...)
	// 清单已经落库后的重新生成也必须可复现：从最终 Catalog 明确还原追加前
	// 的历史 binding，而不是把同一 route 再追加一次。
	if versionRouteInputContains(input, route) {
		manifest, loadErr := loadVersionRouteReceiptManifest()
		if loadErr != nil {
			t.Fatal(loadErr)
		}
		var priorDigest string
		for _, document := range manifest.Receipts {
			if document.SinkID == string(base.ID()) &&
				document.Route.Route.Identity() == (receiptcontract.RouteIdentity{
					Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
					Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
				}).Identity() {
				priorDigest = document.PriorReceiptDigest
				break
			}
		}
		if priorDigest == "" {
			t.Fatal("最终 Catalog 中的版本 route 缺少对应追加收据")
		}
		input.Routes = versionRouteWithoutRoute(input.Routes, route)
		claims := make([]migrationRouteClaim, 0, len(priorReceipt.routeClaims)-1)
		for _, claim := range priorReceipt.routeClaims {
			if catalogRouteIdentity(claim.route) != catalogRouteIdentity(route) {
				claims = append(claims, claim)
			}
		}
		priorReceipt.routeClaims = claims
		priorReceipt.digest = priorDigest
		priorBindingDigest, digestErr := sinkBindingIdentityDigest(input)
		if digestErr != nil {
			t.Fatal(digestErr)
		}
		priorReceipt.bindingDigest = priorBindingDigest
	}
	input.migrationReceipt = &priorReceipt
	endpointBinding, profileDigests, err := resolveVersionRouteBinding(input, route)
	if err != nil {
		t.Fatal(err)
	}
	input.Routes = append(input.Routes, route)
	sort.Slice(input.Routes, func(i, j int) bool {
		return catalogRouteIdentity(input.Routes[i]) < catalogRouteIdentity(input.Routes[j])
	})
	transportID := versionRouteTransportID(t, endpointBinding.EndpointID(), profileDigests)
	bindingDigest, err := sinkBindingIdentityDigest(input)
	if err != nil {
		t.Fatal(err)
	}
	receipt := *input.migrationReceipt
	priorReceiptDigest := receipt.digest
	receipt.bindingDigest = bindingDigest
	receipt.digest = strings.Repeat("a", sha256.Size*2)
	receipt.routeClaims = append(receipt.routeClaims, migrationRouteClaim{
		route: route, evidenceKind: "codex_endpoint", evidenceID: endpointBinding.EndpointID(),
		backend: base.TargetBackend(), adapterID: adapterForBackend(base.TargetBackend()),
		transportID: transportID,
	})
	sort.Slice(receipt.routeClaims, func(i, j int) bool {
		return catalogRouteIdentity(receipt.routeClaims[i].route) < catalogRouteIdentity(receipt.routeClaims[j].route)
	})
	input.migrationReceipt = &receipt
	sinks, err := NewSinkCatalog([]SinkBindingInput{input})
	if err != nil {
		t.Fatal(err)
	}
	routes, err := NewOfficialRouteCatalog(sinks)
	if err != nil {
		t.Fatal(err)
	}
	binding, _ := sinks.Resolve(SinkCodexQuotaWHAM)
	capture := changeset3CaptureRouteWithCatalogs(
		t, receipt.authorityID, ReleaseModePrevious, binding, route, true, 3, sinks, routes,
	)
	if !capture.TerminalGuardAllow || capture.EndpointID != endpointBinding.EndpointID() ||
		capture.TransportID != transportID || capture.ProfileDigest != profileDigests[0] {
		t.Fatalf("版本 route 正式执行捕获未闭合：%+v", capture)
	}

	artifactRoot := "version-route-migration-artifacts/codex_quota_wham"
	wirePath := "catalogdata/" + artifactRoot + "/wire.json"
	executionPath := "catalogdata/" + artifactRoot + "/execution-verification.json"
	acceptancePath := "catalogdata/" + artifactRoot + "/canary-acceptance.json"
	wireRaw := marshalVersionRouteCandidate(t, capture)
	wireDigest := versionRouteCandidateSHA256(wireRaw)
	routeIdentity := receiptcontract.RouteIdentity{
		Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
		Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
	}
	execution := versionRouteExecutionVerification{
		SchemaVersion: 1, Result: "passed", SinkID: string(base.ID()), Route: routeIdentity,
		AuthorityKind: receipt.authorityKind, AuthorityID: string(receipt.authorityID),
		TokenIssuerID: string(receipt.tokenIssuerID), EvidenceKind: "codex_endpoint",
		EvidenceID: endpointBinding.EndpointID(), Backend: string(base.TargetBackend()),
		AdapterID: string(adapterForBackend(base.TargetBackend())), TransportID: transportID,
		WireSHA256: wireDigest, ProfileDigests: profileDigests,
		TerminalGuardAllow: true, ExternalTraffic: false,
	}
	executionRaw := marshalVersionRouteCandidate(t, execution)
	executionDigest := versionRouteCandidateSHA256(executionRaw)
	observation := sha256.Sum256([]byte(wireDigest + "\x00" + executionDigest))
	acceptance := versionRouteCanaryAcceptance{
		SchemaVersion: 1, Result: "accepted", SinkID: string(base.ID()), Route: routeIdentity,
		ProfileDigests: profileDigests, WireFixtureSHA256: wireDigest,
		ExecutionVerificationSHA256: executionDigest,
		ObservationDigest:           hex.EncodeToString(observation[:]), ExternalTraffic: false,
		ReviewedBy: "codex-version-route-audit", ReviewRef: "codex-0.147.0-settings-user/2026-08-14",
	}
	acceptanceRaw := marshalVersionRouteCandidate(t, acceptance)
	document := versionRouteReceiptDoc{
		SinkID: string(base.ID()), PriorReceiptDigest: priorReceiptDigest, BindingDigest: bindingDigest,
		Route: receiptcontract.RouteProof{
			Route: routeIdentity, EvidenceKind: "codex_endpoint", EvidenceID: endpointBinding.EndpointID(),
			Backend: string(base.TargetBackend()), AdapterID: string(adapterForBackend(base.TargetBackend())),
			TransportID:           transportID,
			WireFixture:           receiptcontract.ArtifactRef{Path: wirePath, SHA256: wireDigest},
			ExecutionVerification: receiptcontract.ArtifactRef{Path: executionPath, SHA256: executionDigest},
		},
		ProfileDigests: profileDigests, Source: versionRouteSourceEvidence(t),
		CanaryAcceptance: receiptcontract.ArtifactRef{
			Path: acceptancePath, SHA256: versionRouteCandidateSHA256(acceptanceRaw),
		},
		ReviewedBy: "codex-version-route-audit", ReviewRef: "codex-0.147.0-settings-user/2026-08-14",
		Rationale: "0.147 新增 settings/user endpoint；正式 Compiler、Executor、adapter 与 terminal Guard 的合成无外部流量捕获已通过。",
	}
	manifest := versionRouteReceiptManifest{
		SchemaVersion: 1, BootstrapCommit: BootstrapCommit, Receipts: []versionRouteReceiptDoc{document},
	}
	return manifest, map[string][]byte{
		artifactRoot + "/wire.json":                   wireRaw,
		artifactRoot + "/execution-verification.json": executionRaw,
		artifactRoot + "/canary-acceptance.json":      acceptanceRaw,
	}
}

func versionRouteInputContains(input SinkBindingInput, target CatalogRoute) bool {
	for _, route := range input.Routes {
		if catalogRouteIdentity(route) == catalogRouteIdentity(target) {
			return true
		}
	}
	return false
}

func versionRouteWithoutRoute(routes []CatalogRoute, target CatalogRoute) []CatalogRoute {
	filtered := make([]CatalogRoute, 0, len(routes)-1)
	for _, route := range routes {
		if catalogRouteIdentity(route) != catalogRouteIdentity(target) {
			filtered = append(filtered, route)
		}
	}
	return filtered
}

func sinkBindingInputForVersionRoute(binding SinkBinding) SinkBindingInput {
	return SinkBindingInput{
		ID: binding.id, Purpose: binding.purpose, Persona: binding.persona,
		EndpointEvidence: binding.endpointEvidence, Routes: binding.Routes(),
		TargetBackend: binding.targetBackend, LegacyBackends: binding.LegacyBackends(),
		EnforcementState: binding.enforcementState, Owner: binding.owner,
		MigrationChangeset: binding.migrationChangeset, ExpiryCondition: binding.expiryCondition,
		RuntimeBindable: binding.runtimeBindable, Override: binding.override,
	}
}

func versionRouteTransportID(t *testing.T, endpointID string, profileDigests []string) string {
	t.Helper()
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains("\x00"+strings.Join(profileDigests, "\x00")+"\x00", "\x00"+release.ProfileDigest()+"\x00") {
			continue
		}
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if endpoint.ID == endpointID {
				return endpoint.TransportID
			}
		}
	}
	t.Fatalf("版本 route endpoint 缺少 transport：%s", endpointID)
	return ""
}

func versionRouteSourceEvidence(t *testing.T) amendmentSourceEvidence {
	t.Helper()
	document, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := bindingcontract.NewBindingCatalog(document)
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := catalog.Resolve(string(SinkCodexQuotaWHAM))
	if !ok || len(binding.Candidates) != 1 {
		t.Fatal("WHAM 源码候选不是唯一证据")
	}
	candidate := binding.Candidates[0]
	repositoryRoot, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(repositoryRoot, candidate.File))
	if err != nil {
		t.Fatal(err)
	}
	return amendmentSourceEvidence{
		ScanCandidateID: candidate.ScanCandidateID, ASTFingerprint: candidate.ASTFingerprint,
		File: candidate.File, Function: candidate.Func, Callee: candidate.Callee,
		SourceBlobSHA256: versionRouteCandidateSHA256(raw),
	}
}

func marshalVersionRouteCandidate(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func versionRouteCandidateSHA256(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func writeVersionRouteCandidate(t *testing.T, root, path string, raw []byte) {
	t.Helper()
	target := filepath.Join(root, path)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, raw, 0o644); err != nil {
		t.Fatal(err)
	}
}
