package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"sort"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

// TestRuntimeReceiptManifestsMatchReviewedDocs 防止生产 embed 与审核文件分叉。
func TestRuntimeReceiptManifestsMatchReviewedDocs(t *testing.T) {
	tests := []struct {
		name     string
		docPath  string
		embedded []byte
	}{
		{
			name: "pre-bootstrap supplements", docPath: "../../../docs/egress/lifecycle/pre-bootstrap-supplements.json",
			embedded: mustReadEmbeddedTestFile(t, preBootstrapSupplementFS, "catalogdata/pre-bootstrap-supplements.json"),
		},
		{
			name: "migration receipts", docPath: "../../../docs/egress/lifecycle/migration-receipts.json",
			embedded: mustReadEmbeddedTestFile(t, migrationReceiptFS, "catalogdata/migration-receipts.json"),
		},
		{
			name: "catalog amendments", docPath: "../../../docs/egress/lifecycle/catalog-amendments.json",
			embedded: mustReadEmbeddedTestFile(t, catalogAmendmentFS, "catalogdata/catalog-amendments.json"),
		},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			reviewed, err := os.ReadFile(testCase.docPath)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(reviewed, testCase.embedded) {
				t.Fatalf("生产 embed 与受审清单不一致：%s", testCase.docPath)
			}
		})
	}
}

func TestReferencedMigrationArtifactsMatchReviewedCopies(t *testing.T) {
	manifest, err := loadMigrationReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	compare := func(ref receiptcontract.ArtifactRef) {
		t.Helper()
		const prefix = "catalogdata/migration-artifacts/"
		if !strings.HasPrefix(ref.Path, prefix) {
			t.Fatalf("迁移产物不在受控目录：%s", ref.Path)
		}
		embedded := mustReadEmbeddedTestFile(t, migrationReceiptFS, ref.Path)
		reviewedPath := "../../../docs/egress/lifecycle/migration-artifacts/" + strings.TrimPrefix(ref.Path, prefix)
		reviewed, readErr := os.ReadFile(reviewedPath)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if !bytes.Equal(embedded, reviewed) {
			t.Fatalf("生产迁移产物与审核副本不一致：%s", ref.Path)
		}
	}
	for _, document := range manifest.Receipts {
		for _, route := range document.Routes {
			compare(route.WireFixture)
			compare(route.ExecutionVerification)
		}
		if document.CanaryAcceptance != nil {
			compare(*document.CanaryAcceptance)
		}
	}
}

// TestChangeset1BMigrationReceiptManifestMatchesProductionEvidence 从 ReleaseBinding、
// 已重放的执行产物与 1C canary 验收记录重算四条 canary→enforced 收据链，
// 防止人工编辑摘要、candidate 或 route claim 后仍误认为已完成迁移。
func TestChangeset1BMigrationReceiptManifestMatchesProductionEvidence(t *testing.T) {
	manifest := buildChangeset1BMigrationReceiptManifest(t)
	want, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	want = append(want, '\n')

	const updateEnv = "EGRESS_UPDATE_CHANGESET1B_MIGRATION_MANIFEST"
	paths := []string{
		"catalogdata/migration-receipts.json",
		"../../../docs/egress/lifecycle/migration-receipts.json",
	}
	for _, manifestPath := range paths {
		if os.Getenv(updateEnv) == "1" {
			if err := os.WriteFile(manifestPath, want, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		actual, err := os.ReadFile(manifestPath)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(actual, want) {
			t.Fatalf("1B MigrationReceipt 不是由当前绑定与执行产物重算得出：%s", manifestPath)
		}
	}
}

func buildChangeset1BMigrationReceiptManifest(t *testing.T) migrationReceiptManifest {
	t.Helper()
	sinkIDs := []string{
		string(SinkCodexAdminTestCompact),
		string(SinkCodexAdminTestResponses),
		string(SinkCodexAlphaSearchPATFallback),
		string(SinkCodexUsageProbe),
	}
	sort.Strings(sinkIDs)
	receipts := make([]migrationReceiptDoc, 0, len(sinkIDs)*2)
	for _, sinkID := range sinkIDs {
		_, binding, input := migrationTestBinding(t, sinkID)
		if len(input.Routes) != 1 {
			t.Fatalf("1B 收据生成器当前只允许单 route Sink：%s", sinkID)
		}
		bindingDigest, err := sinkBindingIdentityDigest(input)
		if err != nil {
			t.Fatal(err)
		}
		candidates := make([]migrationCandidateEvidence, 0, len(binding.Candidates))
		for _, candidate := range binding.Candidates {
			candidates = append(candidates, migrationCandidateEvidence{
				ScanCandidateID: candidate.ScanCandidateID,
				ASTFingerprint:  candidate.ASTFingerprint,
			})
		}
		sort.Slice(candidates, func(i, j int) bool {
			return candidates[i].ScanCandidateID < candidates[j].ScanCandidateID
		})

		artifactDirectory := "catalogdata/migration-artifacts/changeset1b/" + strings.ReplaceAll(sinkID, ".", "_")
		wireRef := changeset1BArtifactRef(t, artifactDirectory+"/wire.json")
		verificationRef := changeset1BArtifactRef(t, artifactDirectory+"/execution-verification.json")
		verificationRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, verificationRef.Path)
		var verification receiptcontract.ExecutionVerification
		if err := json.Unmarshal(verificationRaw, &verification); err != nil {
			t.Fatal(err)
		}
		route := input.Routes[0]
		routeIdentity := receiptcontract.RouteIdentity{
			Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
			Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
		}
		if verification.SinkID != sinkID || verification.Route != routeIdentity ||
			verification.AuthorityKind != receiptcontract.AuthorityCodexExecutor ||
			verification.AuthorityID != "codex.executor.changeset1b" ||
			verification.TokenIssuerID != verification.AuthorityID {
			t.Fatalf("1B 执行验证未绑定生产 Executor 与 ReleaseBinding：%s", sinkID)
		}
		canary := migrationReceiptDoc{
			SinkID: sinkID, ApprovedState: string(SinkStateCanaryEnforce),
			BindingDigest: bindingDigest,
			AuthorityKind: verification.AuthorityKind,
			AuthorityID:   verification.AuthorityID, TokenIssuerID: verification.TokenIssuerID,
			Routes: []receiptcontract.RouteProof{{
				Route:        routeIdentity,
				EvidenceKind: verification.EvidenceKind, EvidenceID: verification.EvidenceID,
				Backend: verification.Backend, AdapterID: verification.AdapterID,
				TransportID: verification.TransportID,
				WireFixture: wireRef, ExecutionVerification: verificationRef,
			}},
			Candidates: candidates,
			ReviewedBy: "user-approved-changesets-0-1a-1b",
			ReviewRef:  "changeset-1b-receipt-closure/2026-08-02",
			Rationale:  "变更集 1B 已通过生产 Executor 重放、100% canary Guard 校验和最终请求脱敏固化。",
		}
		canaryDigest, err := receiptcontract.DigestDocument(canary)
		if err != nil {
			t.Fatal(err)
		}

		observationPath := artifactDirectory + "/canary-observation.json"
		observationRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, observationPath)
		reviewedObservationPath := "../../../docs/egress/lifecycle/migration-artifacts/" +
			strings.TrimPrefix(observationPath, "catalogdata/migration-artifacts/")
		reviewedObservation, err := os.ReadFile(reviewedObservationPath)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(observationRaw, reviewedObservation) {
			t.Fatalf("生产 canary 观察记录与审核副本不一致：%s", observationPath)
		}

		acceptanceRef := changeset1BArtifactRef(t, artifactDirectory+"/canary-acceptance.json")
		acceptanceRaw := mustReadEmbeddedTestFile(t, migrationReceiptFS, acceptanceRef.Path)
		var acceptance receiptcontract.CanaryAcceptance
		if err := json.Unmarshal(acceptanceRaw, &acceptance); err != nil {
			t.Fatal(err)
		}
		if acceptance.SchemaVersion != 1 || acceptance.Result != "accepted" ||
			acceptance.SinkID != sinkID || acceptance.CanaryReceiptDigest != canaryDigest ||
			acceptance.ObservationDigest != sha256Hex(observationRaw) ||
			strings.TrimSpace(acceptance.ReviewedBy) == "" || strings.TrimSpace(acceptance.ReviewRef) == "" {
			t.Fatalf("1C canary 验收未绑定实际观察记录与 canary 收据：%s", sinkID)
		}

		enforced := canary
		enforced.ApprovedState = string(SinkStateEnforced)
		enforced.PriorCanaryReceiptDigest = canaryDigest
		enforced.CanaryAcceptance = &acceptanceRef
		enforced.ReviewedBy = "codex-audit-changeset-1c-remediation"
		enforced.ReviewRef = "changeset-1c-remediation-audit/2026-08-03"
		enforced.Rationale = "变更集 1C 主动演练与回滚证据已完成 Codex 技术复审，保留完整 canary 前序并等待用户最终验收。"
		receipts = append(receipts, canary, enforced)
	}
	manifest := migrationReceiptManifest{
		SchemaVersion:   receiptcontract.SchemaVersion,
		BootstrapCommit: BootstrapCommit,
		Receipts:        receipts,
	}
	if err := receiptcontract.ValidateManifest(manifest); err != nil {
		t.Fatal(err)
	}
	for _, receipt := range manifest.Receipts {
		if err := receiptcontract.VerifyArtifacts(migrationReceiptFS, receipt); err != nil {
			t.Fatal(err)
		}
	}
	return manifest
}

func changeset1BArtifactRef(t *testing.T, artifactPath string) receiptcontract.ArtifactRef {
	t.Helper()
	raw := mustReadEmbeddedTestFile(t, migrationReceiptFS, artifactPath)
	return receiptcontract.ArtifactRef{Path: artifactPath, SHA256: sha256Hex(raw)}
}

func TestMigrationReceiptAloneDrivesStateUpgrade(t *testing.T) {
	evidence, binding, input := migrationTestBinding(t, string(SinkCodexResponsesForward))
	receiptDoc, artifacts := migrationTestReceipt(
		t, input, binding, receiptcontract.AuthorityCodexExecutor, "test-executor",
	)
	upgraded, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{receiptDoc},
		},
		artifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	)
	if err != nil {
		t.Fatal(err)
	}
	if upgraded[0].EnforcementState != SinkStateCanaryEnforce || upgraded[0].migrationReceipt == nil {
		t.Fatalf("MigrationReceipt 未驱动状态升级：%+v", upgraded[0])
	}
	if _, err := NewSinkCatalog(upgraded); err != nil {
		t.Fatalf("收据驱动的 canary binding 无法进入 Catalog：%v", err)
	}
	_ = evidence
}

func TestMigrationReceiptSupportsBrowserPersonaAndRejectsWrongAuthority(t *testing.T) {
	_, binding, input := migrationTestBinding(t, string(SinkWebPrivacyAccountInfo))
	receiptDoc, artifacts := migrationTestReceipt(
		t, input, binding, receiptcontract.AuthorityChatGPTWebClient, "browser-client-test",
	)
	upgraded, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{receiptDoc},
		}, artifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	)
	if err != nil {
		t.Fatalf("chatgpt-web Browser Client 收据无法升级：%v", err)
	}
	if _, err := NewSinkCatalog(upgraded); err != nil {
		t.Fatalf("chatgpt-web canary Catalog 构造失败：%v", err)
	}

	wrong := receiptDoc
	wrong.AuthorityKind = receiptcontract.AuthorityCodexExecutor
	if _, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{wrong},
		}, artifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	); err == nil {
		t.Fatal("chatgpt-web 错误使用 Codex Executor 收据未被拒绝")
	}
}

func TestMigrationReceiptRequiresEveryRouteAndActualArtifacts(t *testing.T) {
	_, binding, input := migrationTestBinding(t, string(SinkCodexQuotaWHAM))
	receiptDoc, artifacts := migrationTestReceipt(
		t, input, binding, receiptcontract.AuthorityCodexExecutor, "test-executor",
	)
	missingRoute := receiptDoc
	missingRoute.Routes = append([]receiptcontract.RouteProof(nil), receiptDoc.Routes[:1]...)
	if _, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{missingRoute},
		}, artifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	); err == nil {
		t.Fatal("只证明多 route Sink 的一条 route 未被拒绝")
	}

	brokenArtifacts := fstest.MapFS{}
	for name, item := range artifacts {
		cloned := append([]byte(nil), item.Data...)
		brokenArtifacts[name] = &fstest.MapFile{Data: cloned}
	}
	brokenArtifacts[receiptDoc.Routes[0].WireFixture.Path].Data = []byte("tampered")
	if _, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{receiptDoc},
		}, brokenArtifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	); err == nil {
		t.Fatal("fixture 实际内容与摘要不一致未被拒绝")
	}
}

func TestEnforcedMigrationReceiptRequiresCanaryHistoryAndAcceptance(t *testing.T) {
	_, binding, input := migrationTestBinding(t, string(SinkCodexResponsesForward))
	canary, artifacts := migrationTestReceipt(
		t, input, binding, receiptcontract.AuthorityCodexExecutor, "test-executor",
	)
	direct := canary
	direct.ApprovedState = string(SinkStateEnforced)
	if err := receiptcontract.ValidateManifest(migrationReceiptManifest{
		SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
		Receipts: []migrationReceiptDoc{direct},
	}); err == nil {
		t.Fatal("直接签发 enforced 收据未被拒绝")
	}

	canaryDigest, err := receiptcontract.DigestDocument(canary)
	if err != nil {
		t.Fatal(err)
	}
	acceptance := receiptcontract.CanaryAcceptance{
		SchemaVersion: 1, Result: "accepted", SinkID: string(input.ID),
		CanaryReceiptDigest: canaryDigest,
		ObservationDigest:   sha256Hex([]byte("canary-observation")),
		ReviewedBy:          "reviewer", ReviewRef: "review/canary-acceptance",
	}
	acceptanceRaw, err := json.Marshal(acceptance)
	if err != nil {
		t.Fatal(err)
	}
	acceptancePath := "catalogdata/migration-artifacts/test/canary-acceptance.json"
	artifacts[acceptancePath] = &fstest.MapFile{Data: acceptanceRaw}
	enforced := canary
	enforced.ApprovedState = string(SinkStateEnforced)
	enforced.PriorCanaryReceiptDigest = canaryDigest
	enforced.CanaryAcceptance = &receiptcontract.ArtifactRef{
		Path: acceptancePath, SHA256: sha256Hex(acceptanceRaw),
	}
	upgraded, err := applyMigrationReceiptManifestWithFS(
		migrationReceiptManifest{
			SchemaVersion: receiptcontract.SchemaVersion, BootstrapCommit: BootstrapCommit,
			Receipts: []migrationReceiptDoc{canary, enforced},
		}, artifacts,
		map[string]bindingcontract.ReleaseBindingDoc{binding.SinkID: binding},
		[]SinkBindingInput{input},
	)
	if err != nil || upgraded[0].EnforcementState != SinkStateEnforced {
		t.Fatalf("合法 canary→enforced 收据链未生效：state=%s err=%v", upgraded[0].EnforcementState, err)
	}
}

func migrationTestBinding(
	t *testing.T,
	sinkID string,
) (bindingcontract.BindingCatalog, bindingcontract.ReleaseBindingDoc, SinkBindingInput) {
	t.Helper()
	document, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := bindingcontract.NewBindingCatalog(document)
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := evidence.Resolve(sinkID)
	if !ok {
		t.Fatalf("缺少测试绑定：%s", sinkID)
	}
	input, err := sinkBindingInputFromEvidence(binding)
	if err != nil {
		t.Fatal(err)
	}
	return evidence, binding, input
}

func migrationTestReceipt(
	t *testing.T,
	input SinkBindingInput,
	binding bindingcontract.ReleaseBindingDoc,
	authorityKind receiptcontract.AuthorityKind,
	authorityID string,
) (migrationReceiptDoc, fstest.MapFS) {
	t.Helper()
	bindingDigest, err := sinkBindingIdentityDigest(input)
	if err != nil {
		t.Fatal(err)
	}
	candidates := make([]migrationCandidateEvidence, 0, len(binding.Candidates))
	for _, candidate := range binding.Candidates {
		candidates = append(candidates, migrationCandidateEvidence{
			ScanCandidateID: candidate.ScanCandidateID, ASTFingerprint: candidate.ASTFingerprint,
		})
	}
	artifacts := fstest.MapFS{}
	proofs := make([]receiptcontract.RouteProof, 0, len(input.Routes))
	for index, route := range input.Routes {
		evidenceKind := "browser_profile"
		evidenceID := "browser-profile-test"
		if input.Persona == PersonaCodexCLI {
			endpointBinding, ok := resolveReceiptEndpointBinding(input, route)
			if !ok {
				t.Fatalf("测试 route 没有 Codex endpoint：%s", route.Key)
			}
			evidenceKind = "codex_endpoint"
			evidenceID = endpointBinding.EndpointID()
		}
		adapterID := map[BackendKind]AdapterID{
			BackendHTTPUpstream: AdapterHTTPUpstream,
			BackendReqProfile:   AdapterReqProfile, BackendWebSocket: AdapterWebSocket,
		}[input.TargetBackend]
		routeIdentity := receiptcontract.RouteIdentity{
			Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
			Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
		}
		wirePath := "catalogdata/migration-artifacts/test/wire-" + string(rune('a'+index)) + ".bin"
		wireRaw := []byte("wire-fixture:" + routeIdentity.Identity())
		artifacts[wirePath] = &fstest.MapFile{Data: wireRaw}
		verification := receiptcontract.ExecutionVerification{
			SchemaVersion: 1, Result: "passed", SinkID: string(input.ID), Route: routeIdentity,
			AuthorityKind: authorityKind, AuthorityID: authorityID, TokenIssuerID: authorityID,
			EvidenceKind: evidenceKind, EvidenceID: evidenceID,
			Backend: string(input.TargetBackend), AdapterID: string(adapterID),
			TransportID: "transport-test-" + string(rune('a'+index)), WireSHA256: sha256Hex(wireRaw),
		}
		verificationRaw, err := json.Marshal(verification)
		if err != nil {
			t.Fatal(err)
		}
		verificationPath := "catalogdata/migration-artifacts/test/verification-" + string(rune('a'+index)) + ".json"
		artifacts[verificationPath] = &fstest.MapFile{Data: verificationRaw}
		proofs = append(proofs, receiptcontract.RouteProof{
			Route: routeIdentity, EvidenceKind: evidenceKind, EvidenceID: evidenceID,
			Backend: string(input.TargetBackend), AdapterID: string(adapterID),
			TransportID: verification.TransportID,
			WireFixture: receiptcontract.ArtifactRef{Path: wirePath, SHA256: sha256Hex(wireRaw)},
			ExecutionVerification: receiptcontract.ArtifactRef{
				Path: verificationPath, SHA256: sha256Hex(verificationRaw),
			},
		})
	}
	return migrationReceiptDoc{
		SinkID: string(input.ID), ApprovedState: string(SinkStateCanaryEnforce),
		BindingDigest: bindingDigest, AuthorityKind: authorityKind,
		AuthorityID: authorityID, TokenIssuerID: authorityID,
		Routes: proofs, Candidates: candidates,
		ReviewedBy: "reviewer", ReviewRef: "review/ref", Rationale: "已完成迁移验收",
	}, artifacts
}

func sha256Hex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func TestSupplementCandidateDeterministicallyBuildsRuntimeBinding(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	baseline, err := bindingcontract.ParseSinkBaseline(raw)
	if err != nil {
		t.Fatal(err)
	}
	var candidate bindingcontract.SinkBaselineCandidateDoc
	for _, current := range baseline.Sinks {
		if current.RuntimeSinkID != "" && !current.IsFacade && current.EnforcementState == "legacy_observe" &&
			len(current.Routes) > 0 {
			candidate = current
			break
		}
	}
	if candidate.ScanCandidateID == "" {
		t.Fatal("基线缺少可用于补录构造测试的候选")
	}
	binding, err := bindingFromSupplement(candidate)
	if err != nil {
		t.Fatal(err)
	}
	input, err := sinkBindingInputFromEvidence(binding)
	if err != nil {
		t.Fatal(err)
	}
	if !input.RuntimeBindable || input.EnforcementState != SinkStateLegacyObserve ||
		string(input.ID) != candidate.RuntimeSinkID {
		t.Fatalf("补录没有确定性生成 legacy runtime binding：%+v", input)
	}
	beforeRoutes := len(input.Routes)
	beforeBackends := len(input.LegacyBackends)
	if err := mergeSupplementInput(&input, input); err != nil {
		t.Fatal(err)
	}
	if len(input.Routes) != beforeRoutes || len(input.LegacyBackends) != beforeBackends {
		t.Fatal("重复补录合并产生了 route/backend 重复项")
	}
}

type embeddedReader interface {
	ReadFile(string) ([]byte, error)
}

func mustReadEmbeddedTestFile(t *testing.T, filesystem embeddedReader, path string) []byte {
	t.Helper()
	raw, err := filesystem.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
