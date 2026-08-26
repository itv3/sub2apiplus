package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

type changeset3CanaryObservation struct {
	SchemaVersion      int      `json:"schema_version"`
	SinkID             string   `json:"sink_id"`
	Result             string   `json:"result"`
	Environment        string   `json:"environment"`
	ObservedAt         string   `json:"observed_at"`
	ExerciseKind       string   `json:"exercise_kind"`
	Evidence           []string `json:"evidence"`
	GuardCanaryPercent int      `json:"guard_canary_percent"`
	GuardRejections    int      `json:"guard_rejections"`
	ExternalTraffic    bool     `json:"external_traffic"`
	ReviewedBy         string   `json:"reviewed_by"`
	ReviewRef          string   `json:"review_ref"`
}

func TestChangeset3MigrationReceiptsMatchRuntimeContracts(t *testing.T) {
	authoritativeCaptures := changeset3LoadAuthoritativePostCaptures(t)
	manifest, err := loadChangeset3MigrationReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	if len(manifest.Receipts) != 34 {
		t.Fatalf("变更集 3 收据数量=%d，期望 17 条 canary→enforced 链", len(manifest.Receipts))
	}

	embeddedRaw := mustReadEmbeddedTestFile(
		t, migrationReceiptFS, "catalogdata/changeset3-migration-receipts.json",
	)
	reviewedRaw, err := os.ReadFile("../../../docs/egress/migration/migration-receipts.json")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(embeddedRaw, reviewedRaw) {
		t.Fatal("变更集 3 生产收据与审核副本不一致")
	}

	catalog := DefaultSinkCatalog()
	physical, err := NewPhysicalRouteCatalog(catalog)
	if err != nil {
		t.Fatal(err)
	}
	historicalKey := profilecontract.SnapshotKey{
		Version: "0.145.0",
		Digest:  "e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b",
	}
	releaseCatalog := DefaultReleaseCatalog()
	historicalProfile, ok := releaseCatalog.snapshots.Resolve(historicalKey)
	if !ok {
		t.Fatal("变更集 3 历史收据缺少 0.145 Profile")
	}
	historicalExecutable, ok := releaseCatalog.snapshots.ResolveExecutable(historicalKey)
	if !ok {
		t.Fatal("变更集 3 历史收据缺少 0.145 执行画像")
	}
	release := ResolvedCodexRelease{
		mode: ReleaseModePrevious, profileDigest: historicalKey.Digest,
		profile: historicalProfile, executable: historicalExecutable,
	}
	endpointBindings, err := NewEndpointBindingCatalog(catalog, physical, release.ExecutableProfile())
	if err != nil {
		t.Fatal(err)
	}
	transportByEndpoint := make(map[string]string)
	lifecycleByEndpoint := make(map[string]profilecontract.LifecycleKind)
	for _, endpoint := range release.Profile().Endpoints() {
		transportByEndpoint[endpoint.ID] = endpoint.TransportID
		lifecycleByEndpoint[endpoint.ID] = endpoint.ClientLifecycle
	}

	targets := changeset3RuntimeSinkIDsForAudit()
	targetSet := make(map[string]bool, len(targets))
	for _, sinkID := range targets {
		targetSet[string(sinkID)] = true
	}
	seenCanary := make(map[string]string, len(targets))
	seenEnforced := make(map[string]bool, len(targets))
	versionRoutes, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	versionRouteCountBySink := make(map[string]int)
	for _, receipt := range versionRoutes.Receipts {
		versionRouteCountBySink[receipt.SinkID]++
	}
	for _, document := range manifest.Receipts {
		if !targetSet[document.SinkID] {
			t.Fatalf("变更集 3 收据包含范围外 Sink: %s", document.SinkID)
		}
		binding, ok := catalog.Resolve(SinkID(document.SinkID))
		if !ok || !binding.RuntimeBindable() || binding.Persona() != PersonaCodexCLI ||
			binding.EndpointEvidence() != EndpointEvidenceCodexProfile {
			t.Fatalf("收据目标不是 Codex Runtime Sink: %s", document.SinkID)
		}
		if document.AuthorityKind != receiptcontract.AuthorityCodexExecutor ||
			document.AuthorityID != "codex.executor.changeset1b" ||
			document.TokenIssuerID != document.AuthorityID {
			t.Fatalf("收据 authority 漂移: %s", document.SinkID)
		}
		if len(document.Routes)+versionRouteCountBySink[document.SinkID] != len(binding.Routes()) {
			t.Fatalf("历史收据与追加版本收据未覆盖完整 route: %s", document.SinkID)
		}
		for _, proof := range document.Routes {
			route, ok := changeset3FindCatalogRoute(binding.Routes(), proof.Route)
			if !ok {
				t.Fatalf("收据 route 不在 Catalog: %s %+v", document.SinkID, proof.Route)
			}
			endpointBinding, ok := endpointBindings.ResolveBindingRoute(binding, route, physical)
			if !ok {
				t.Fatalf("收据没有连接当前 EndpointBinding/ProfileSpec: %s %+v", document.SinkID, proof)
			}
			currentTransportID := transportByEndpoint[endpointBinding.EndpointID()]
			if proof.EvidenceID != endpointBinding.EndpointID() ||
				(proof.TransportID != currentTransportID && !changeset4ApprovesLongLivedTransportTransition(
					proof.TransportID,
					currentTransportID,
					lifecycleByEndpoint[endpointBinding.EndpointID()],
				)) {
				t.Fatalf("收据没有连接当前 EndpointBinding/ProfileSpec: %s %+v", document.SinkID, proof)
			}
			expectedAdapter := map[BackendKind]AdapterID{
				BackendHTTPUpstream: AdapterHTTPUpstream,
				BackendReqProfile:   AdapterReqProfile,
				BackendWebSocket:    AdapterWebSocket,
			}[binding.TargetBackend()]
			if proof.Backend != string(binding.TargetBackend()) ||
				proof.AdapterID != string(expectedAdapter) {
				t.Fatalf("收据 adapter/backend 与生产 binding 不一致: %s", document.SinkID)
			}
			changeset3AssertArtifactCopy(t, proof.WireFixture)
			changeset3AssertArtifactCopy(t, proof.ExecutionVerification)
			wireRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, proof.WireFixture.Path)
			var wire changeset3RebuiltWireFixture
			changeset3DecodeStrict(t, wireRaw, &wire)
			if wire.SchemaVersion != 2 || wire.CaptureKind != "adapter_terminal_final_wire" ||
				wire.SinkID != document.SinkID || wire.Route != proof.Route ||
				wire.AuthorityID != document.AuthorityID || !wire.HasFinalizationToken ||
				wire.EndpointID != proof.EvidenceID || wire.Backend != proof.Backend ||
				wire.AdapterID != proof.AdapterID || wire.TransportID != proof.TransportID ||
				!slices.Equal(wire.ReleaseModes, []ReleaseMode{ReleaseModeActive, ReleaseModePrevious}) ||
				wire.CredentialMaterial != "synthetic_attempt_local_redacted" ||
				wire.FinalWireManifest.SHA256 != changeset3PostFinalWireSHA256 ||
				wire.SecretScan.SHA256 != changeset3PostSecretScanSHA256 ||
				wire.ComparisonResult != "scoped_reference_match_with_explicit_approved_deltas" ||
				strings.TrimSpace(wire.Redaction) == "" {
				t.Fatalf("变更集 3 wire fixture 未绑定 adapter-terminal final-wire: %s %+v", document.SinkID, wire)
			}
			for mode, capture := range map[ReleaseMode]changeset3PreIdentityReferenceCapture{
				ReleaseModeActive: wire.ActiveCapture, ReleaseModePrevious: wire.PreviousCapture,
			} {
				key := changeset3PostCaptureKey(capture)
				authoritative, present := authoritativeCaptures[key]
				if !present {
					t.Fatalf("正式 post manifest 缺少收据 capture：%q", key)
				}
				changeset3RequireExactFinalWire(t, key, authoritative, capture)
				if capture.ReleaseMode != mode || capture.SinkID != document.SinkID ||
					capture.Method != proof.Route.Method || capture.HostTemplate != proof.Route.Host ||
					capture.PathTemplate != proof.Route.Path || string(capture.Protocol) != proof.Route.Protocol ||
					string(capture.Purpose) != proof.Route.Purpose || capture.EndpointID != proof.EvidenceID ||
					capture.AuthorityID != ExecutorID(document.AuthorityID) || !capture.HasFinalizationToken ||
					!capture.TerminalGuardAllow || capture.AttemptOrdinal != 1 ||
					capture.Backend != BackendKind(proof.Backend) || capture.AdapterID != AdapterID(proof.AdapterID) ||
					capture.ProfileValidationResult != "passed" {
					t.Fatalf("变更集 3 %s capture 未绑定正式 route/Executor/Guard: %s %+v", mode, document.SinkID, capture)
				}
			}
			if wire.ActiveCapture.TransportID != proof.TransportID ||
				wire.ActiveCaptureDigest != changeset3DigestFinalWireCapture(t, wire.ActiveCapture) ||
				wire.PreviousCaptureDigest != changeset3DigestFinalWireCapture(t, wire.PreviousCapture) {
				t.Fatalf("变更集 3 capture 摘要或 active transport 漂移: %s", document.SinkID)
			}
			verificationRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, proof.ExecutionVerification.Path)
			var verification changeset3RebuiltExecutionVerification
			changeset3DecodeStrict(t, verificationRaw, &verification)
			if verification.SchemaVersion != 2 || verification.Result != "passed" ||
				verification.SinkID != document.SinkID || verification.Route != proof.Route ||
				verification.AuthorityKind != document.AuthorityKind ||
				verification.AuthorityID != document.AuthorityID || verification.TokenIssuerID != document.TokenIssuerID ||
				verification.EvidenceKind != proof.EvidenceKind || verification.EvidenceID != proof.EvidenceID ||
				verification.Backend != proof.Backend || verification.AdapterID != proof.AdapterID ||
				verification.TransportID != proof.TransportID || verification.WireSHA256 != proof.WireFixture.SHA256 ||
				verification.FinalWireManifestSHA256 != changeset3PostFinalWireSHA256 ||
				verification.ActiveCaptureSHA256 != wire.ActiveCaptureDigest ||
				verification.PreviousCaptureSHA256 != wire.PreviousCaptureDigest ||
				!verification.TerminalGuardAllow ||
				!slices.Equal(verification.ComparedReleaseModes, []ReleaseMode{ReleaseModeActive, ReleaseModePrevious}) {
				t.Fatalf("变更集 3 execution verification 未绑定 final-wire: %s %+v", document.SinkID, verification)
			}
			for _, secretMarker := range []string{
				"Bearer ", "synthetic-refresh-token", "synthetic-attempt-token", "sk-",
			} {
				if strings.Contains(string(wireRaw), secretMarker) {
					t.Fatalf("wire fixture 含潜在凭据 %q: %s", secretMarker, proof.WireFixture.Path)
				}
			}
		}
		switch document.ApprovedState {
		case string(SinkStateCanaryEnforce):
			digest, digestErr := receiptcontract.DigestDocument(document)
			if digestErr != nil {
				t.Fatal(digestErr)
			}
			seenCanary[document.SinkID] = digest
		case string(SinkStateEnforced):
			if document.PriorCanaryReceiptDigest != seenCanary[document.SinkID] ||
				document.CanaryAcceptance == nil {
				t.Fatalf("enforced 收据没有连接同 Sink canary: %s", document.SinkID)
			}
			changeset3AssertArtifactCopy(t, *document.CanaryAcceptance)
			acceptanceRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, document.CanaryAcceptance.Path)
			var acceptance receiptcontract.CanaryAcceptance
			changeset3DecodeStrict(t, acceptanceRaw, &acceptance)
			observationPath := filepath.Join(
				filepath.Dir(document.CanaryAcceptance.Path), "canary-observation.json",
			)
			observationRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, observationPath)
			changeset3AssertReviewedBytes(t, observationPath, observationRaw)
			var observation changeset3CanaryObservation
			changeset3DecodeStrict(t, observationRaw, &observation)
			if acceptance.SchemaVersion != 2 || acceptance.Result != "accepted" ||
				acceptance.SinkID != document.SinkID ||
				acceptance.CanaryReceiptDigest != seenCanary[document.SinkID] ||
				observation.SchemaVersion != 2 || observation.Result != "accepted" ||
				observation.SinkID != document.SinkID ||
				observation.Environment != "local-deterministic" ||
				observation.ExerciseKind != "adapter_terminal_final_wire" ||
				observation.GuardCanaryPercent != 100 || observation.GuardRejections != 0 ||
				observation.ExternalTraffic || len(observation.Evidence) != 3 ||
				acceptance.ObservationDigest != changeset3AuditSHA256(observationRaw) {
				t.Fatalf("canary 观察记录不完整或错误声称外部流量: %s", document.SinkID)
			}
			seenEnforced[document.SinkID] = true
		}
	}
	if len(seenCanary) != len(targets) || len(seenEnforced) != len(targets) {
		t.Fatalf("变更集 3 收据链不完整: canary=%d enforced=%d", len(seenCanary), len(seenEnforced))
	}
}

func changeset4ApprovesLongLivedTransportTransition(
	previous string,
	current string,
	lifecycle profilecontract.LifecycleKind,
) bool {
	return lifecycle == profilecontract.LifecycleBackendClientLongLived &&
		previous == "codex-0.145.0-http-ubuntu24-native" &&
		current == "codex-0.145.0-http-ubuntu24-native-long-lived"
}

func changeset3LoadAuthoritativePostCaptures(
	t *testing.T,
) map[string]changeset3PreIdentityReferenceCapture {
	t.Helper()
	raw, err := os.ReadFile("../../../docs/egress/migration/post_identity_authority_refactor_final_wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset3PostFinalWireSHA256 {
		t.Fatalf("正式 post manifest 摘要漂移：got=%s want=%s", got, changeset3PostFinalWireSHA256)
	}
	var manifest changeset3PostFinalWireManifest
	changeset3DecodeStrict(t, raw, &manifest)
	result := make(map[string]changeset3PreIdentityReferenceCapture, len(manifest.Captures))
	for _, capture := range manifest.Captures {
		key := changeset3PostCaptureKey(capture)
		if _, exists := result[key]; exists {
			t.Fatalf("正式 post manifest capture 重复：%q", key)
		}
		result[key] = capture
	}
	return result
}

func changeset3RequireExactFinalWire(
	t *testing.T,
	key string,
	want changeset3PreIdentityReferenceCapture,
	got changeset3PreIdentityReferenceCapture,
) {
	t.Helper()
	result, err := finalwirecontract.Compare(key, want, got, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !result.OK() {
		t.Fatalf("正式 final-wire 严格比较失败：key=%q diffs=%+v", key, result.Unexpected)
	}
}

func TestChangeset3FinalCodexRuntimeBoundary(t *testing.T) {
	targets := changeset3RuntimeSinkIDsForAudit()
	for _, sinkID := range targets {
		binding, ok := DefaultSinkCatalog().Resolve(sinkID)
		if !ok || binding.EnforcementState() != SinkStateEnforced ||
			binding.MigrationReceiptDigest() == "" || !binding.RuntimeBindable() {
			t.Fatalf("变更集 3 Runtime Sink 未 enforced: %s", sinkID)
		}
	}
	excluded := []SinkID{
		"codex.admin_test.chat_completions",
		"codex.admin_test.keeper",
	}
	for _, sinkID := range excluded {
		binding, ok := DefaultSinkCatalog().Resolve(sinkID)
		if !ok || binding.RuntimeBindable() || binding.EnforcementState() != SinkStateLegacyObserve ||
			binding.MigrationReceiptDigest() != "" {
			t.Fatalf("API Key 专用历史候选不再是 scope exclusion: %s", sinkID)
		}
	}
	exchange, ok := DefaultSinkCatalog().Resolve(SinkCodexOAuthExchange)
	if !ok || exchange.RuntimeBindable() == false || exchange.EndpointEvidence() != EndpointEvidenceTransportOnly ||
		exchange.EnforcementState() != SinkStateLegacyObserve || exchange.MigrationReceiptDigest() != "" {
		t.Fatal("OAuth exchange 不再是唯一 transport-only Runtime 例外")
	}

	var runtimeProfileEnforced, runtimeProfileLegacy int
	var scopeExcludedProfile []string
	for _, binding := range DefaultSinkCatalog().Bindings() {
		if binding.Persona() != PersonaCodexCLI || binding.EndpointEvidence() != EndpointEvidenceCodexProfile {
			continue
		}
		if !binding.RuntimeBindable() {
			scopeExcludedProfile = append(scopeExcludedProfile, string(binding.ID()))
			continue
		}
		switch binding.EnforcementState() {
		case SinkStateEnforced:
			runtimeProfileEnforced++
		case SinkStateLegacyObserve:
			runtimeProfileLegacy++
		}
	}
	sort.Strings(scopeExcludedProfile)
	wantExcluded := []string{
		"codex.admin_test.chat_completions",
		"codex.admin_test.keeper",
	}
	if runtimeProfileEnforced != 21 || runtimeProfileLegacy != 0 ||
		!slices.Equal(scopeExcludedProfile, wantExcluded) {
		t.Fatalf(
			"Codex Runtime 最终边界错误: enforced=%d legacy=%d excluded=%v",
			runtimeProfileEnforced, runtimeProfileLegacy, scopeExcludedProfile,
		)
	}
}

func changeset3RuntimeSinkIDsForAudit() []SinkID {
	ids := []SinkID{
		SinkCodexAlphaSearchDirect,
		SinkCodexFilesBlobUpload,
		SinkCodexFilesRegister,
		SinkCodexImagesOAuthTest,
		SinkCodexImagesResponses,
		SinkCodexModelsList,
		SinkCodexOAuthRefresh,
		SinkCodexQuotaWHAM,
		SinkCodexRealtimeCalls,
		SinkCodexRealtimeSideband,
		SinkCodexResponsesAnthropicCompat,
		SinkCodexResponsesChatCompletions,
		SinkCodexResponsesForward,
		SinkCodexResponsesPassthrough,
		SinkCodexResponsesWS,
		SinkCodexResponsesWSHTTPBridge,
		SinkCodexResponsesWSV2Passthrough,
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

func changeset3FindCatalogRoute(
	routes []CatalogRoute,
	want receiptcontract.RouteIdentity,
) (CatalogRoute, bool) {
	for _, route := range routes {
		if route.Key.Method == want.Method && route.Key.Host == want.Host && route.Key.Path == want.Path &&
			string(route.Key.Purpose) == want.Purpose && string(route.Protocol) == want.Protocol {
			return route, true
		}
	}
	return CatalogRoute{}, false
}

func changeset3AssertArtifactCopy(t *testing.T, ref receiptcontract.ArtifactRef) {
	t.Helper()
	raw := mustReadEmbeddedTestFile(t, migrationReceiptFS, ref.Path)
	if changeset3AuditSHA256(raw) != ref.SHA256 {
		t.Fatalf("产物摘要不匹配: %s", ref.Path)
	}
	changeset3AssertReviewedBytes(t, ref.Path, raw)
}

func changeset3AssertReviewedBytes(t *testing.T, artifactPath string, embedded []byte) {
	t.Helper()
	const prefix = "catalogdata/migration-artifacts/"
	if !strings.HasPrefix(artifactPath, prefix) {
		t.Fatalf("产物路径不受控: %s", artifactPath)
	}
	reviewedPath := "../../../docs/egress/migration/migration-artifacts/" +
		strings.TrimPrefix(artifactPath, prefix)
	reviewed, err := os.ReadFile(reviewedPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(embedded, reviewed) {
		t.Fatalf("生产产物与审核副本不一致: %s", artifactPath)
	}
}

func changeset3DecodeStrict(t *testing.T, raw []byte, target any) {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		t.Fatal(err)
	}
}

func changeset3AuditSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
