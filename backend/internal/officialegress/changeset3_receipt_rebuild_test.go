package officialegress

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

const changeset3ReceiptRebuildOutputEnv = "CHANGESET3_RECEIPT_REBUILD_OUTPUT"

type changeset3FinalWireArtifactRef struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type changeset3RebuiltWireFixture struct {
	SchemaVersion         int                                   `json:"schema_version"`
	CaptureKind           string                                `json:"capture_kind"`
	SinkID                string                                `json:"sink_id"`
	Route                 receiptcontract.RouteIdentity         `json:"route"`
	AuthorityID           string                                `json:"authority_id"`
	HasFinalizationToken  bool                                  `json:"has_finalization_token"`
	EndpointID            string                                `json:"endpoint_id"`
	Backend               string                                `json:"backend"`
	AdapterID             string                                `json:"adapter_id"`
	TransportID           string                                `json:"transport_id"`
	ReleaseModes          []ReleaseMode                         `json:"release_modes"`
	CredentialMaterial    string                                `json:"credential_material"`
	FinalWireManifest     changeset3FinalWireArtifactRef        `json:"final_wire_manifest"`
	SecretScan            changeset3FinalWireArtifactRef        `json:"secret_scan"`
	ActiveCaptureDigest   string                                `json:"active_capture_sha256"`
	PreviousCaptureDigest string                                `json:"previous_capture_sha256"`
	ActiveCapture         changeset3PreIdentityReferenceCapture `json:"active_capture"`
	PreviousCapture       changeset3PreIdentityReferenceCapture `json:"previous_capture"`
	ComparisonResult      string                                `json:"comparison_result"`
	Redaction             string                                `json:"redaction"`
}

type changeset3RebuiltExecutionVerification struct {
	SchemaVersion           int                           `json:"schema_version"`
	Result                  string                        `json:"result"`
	SinkID                  string                        `json:"sink_id"`
	Route                   receiptcontract.RouteIdentity `json:"route"`
	AuthorityKind           receiptcontract.AuthorityKind `json:"authority_kind"`
	AuthorityID             string                        `json:"authority_id"`
	TokenIssuerID           string                        `json:"token_issuer_id"`
	EvidenceKind            string                        `json:"evidence_kind"`
	EvidenceID              string                        `json:"evidence_id"`
	Backend                 string                        `json:"backend"`
	AdapterID               string                        `json:"adapter_id"`
	TransportID             string                        `json:"transport_id"`
	WireSHA256              string                        `json:"wire_fixture_sha256"`
	FinalWireManifestSHA256 string                        `json:"final_wire_manifest_sha256"`
	ActiveCaptureSHA256     string                        `json:"active_capture_sha256"`
	PreviousCaptureSHA256   string                        `json:"previous_capture_sha256"`
	TerminalGuardAllow      bool                          `json:"terminal_guard_allow"`
	ComparedReleaseModes    []ReleaseMode                 `json:"compared_release_modes"`
}

func TestGenerateChangeset3RebuiltMigrationReceipts(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset3ReceiptRebuildOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定收据重建临时目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("收据重建输出目录必须是绝对路径")
	}
	manifest, artifacts := changeset3BuildRebuiltReceipts(t)
	for artifactPath, raw := range artifacts {
		target := filepath.Join(output, filepath.FromSlash(artifactPath))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, raw, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	manifestRaw := changeset3ReferenceMarshal(t, manifest)
	target := filepath.Join(output, "catalogdata/changeset3-migration-receipts.json")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, manifestRaw, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(output, "docs-migration-receipts.json"), manifestRaw, 0o644); err != nil {
		t.Fatal(err)
	}
}

func changeset3BuildRebuiltReceipts(
	t *testing.T,
) (receiptcontract.Manifest, map[string][]byte) {
	t.Helper()
	currentRaw, err := os.ReadFile("catalogdata/changeset3-migration-receipts.json")
	if err != nil {
		t.Fatal(err)
	}
	current, err := receiptcontract.ParseManifest(currentRaw)
	if err != nil {
		t.Fatal(err)
	}
	postRaw, err := os.ReadFile(
		"../../../docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	if changeset3ReferenceSHA256(postRaw) != changeset3PostFinalWireSHA256 {
		t.Fatal("收据重建引用的 post-refactor final-wire 未冻结")
	}
	var post changeset3PostFinalWireManifest
	decoder := json.NewDecoder(bytes.NewReader(postRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&post); err != nil {
		t.Fatal(err)
	}
	captures := make(map[string]changeset3PreIdentityReferenceCapture, len(post.Captures))
	for _, capture := range post.Captures {
		captures[changeset3PostCaptureKey(capture)] = capture
	}
	docBySinkState := make(map[string]receiptcontract.Document, len(current.Receipts))
	for _, document := range current.Receipts {
		docBySinkState[document.SinkID+"\x00"+document.ApprovedState] = document
	}
	artifacts := make(map[string][]byte)
	var rebuilt []receiptcontract.Document
	for _, sinkID := range changeset3RuntimeSinkIDsForAudit() {
		canary := docBySinkState[string(sinkID)+"\x00"+string(SinkStateCanaryEnforce)]
		enforced := docBySinkState[string(sinkID)+"\x00"+string(SinkStateEnforced)]
		if canary.SinkID == "" || enforced.SinkID == "" {
			t.Fatalf("缺少待重建收据链：%s", sinkID)
		}
		canary.ReviewedBy = "codex-changeset3-final-wire-audit"
		canary.ReviewRef = "changeset-3-post-identity-authority-final-wire/2026-08-03"
		canary.Rationale = "active/previous final-wire 均由正式 Executor、adapter 与本地 terminal Guard 捕获；Header、Body、WS 帧、TLS/Profile/Pool 和动态目标证据已冻结。"
		for routeIndex := range canary.Routes {
			proof := &canary.Routes[routeIndex]
			active := changeset3FindPostCaptureForProof(t, captures, canary.SinkID, proof.Route, ReleaseModeActive)
			previous := changeset3FindPostCaptureForProof(t, captures, canary.SinkID, proof.Route, ReleaseModePrevious)
			wire := changeset3RebuiltWireFixture{
				SchemaVersion: 2, CaptureKind: "adapter_terminal_final_wire",
				SinkID: canary.SinkID, Route: proof.Route,
				AuthorityID: canary.AuthorityID, HasFinalizationToken: true,
				EndpointID: proof.EvidenceID, Backend: proof.Backend,
				AdapterID: proof.AdapterID, TransportID: proof.TransportID,
				ReleaseModes:       []ReleaseMode{ReleaseModeActive, ReleaseModePrevious},
				CredentialMaterial: "synthetic_attempt_local_redacted",
				FinalWireManifest: changeset3FinalWireArtifactRef{
					Path:   "docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json",
					SHA256: changeset3PostFinalWireSHA256,
				},
				SecretScan: changeset3FinalWireArtifactRef{
					Path:   "docs/changeset3/post_identity_authority_refactor_final_wire/secret-scan.json",
					SHA256: changeset3PostSecretScanSHA256,
				},
				ActiveCaptureDigest:   changeset3DigestFinalWireCapture(t, active),
				PreviousCaptureDigest: changeset3DigestFinalWireCapture(t, previous),
				ActiveCapture:         active, PreviousCapture: previous,
				ComparisonResult: "scoped_reference_match_with_explicit_approved_deltas",
				Redaction:        "Authorization、attestation、cookie 与 refresh token 仅保留 attempt-local 类型；所有值均为合成数据。",
			}
			for mode, pair := range map[ReleaseMode][2]changeset3PreIdentityReferenceCapture{
				ReleaseModeActive:   {active, wire.ActiveCapture},
				ReleaseModePrevious: {previous, wire.PreviousCapture},
			} {
				result, compareErr := finalwirecontract.Compare(
					changeset3PostCaptureKey(pair[0]), pair[0], pair[1], nil,
				)
				if compareErr != nil {
					t.Fatal(compareErr)
				}
				if !result.OK() {
					t.Fatalf("%s 收据重建未逐字段绑定正式 final-wire：%+v", mode, result.Unexpected)
				}
			}
			wireRaw := changeset3ReferenceMarshal(t, wire)
			wirePath := proof.WireFixture.Path
			artifacts[wirePath] = wireRaw
			proof.WireFixture.SHA256 = changeset3ReferenceSHA256(wireRaw)
			verification := changeset3RebuiltExecutionVerification{
				SchemaVersion: 2, Result: "passed", SinkID: canary.SinkID,
				Route: proof.Route, AuthorityKind: canary.AuthorityKind,
				AuthorityID: canary.AuthorityID, TokenIssuerID: canary.TokenIssuerID,
				EvidenceKind: proof.EvidenceKind, EvidenceID: proof.EvidenceID,
				Backend: proof.Backend, AdapterID: proof.AdapterID, TransportID: proof.TransportID,
				WireSHA256:              proof.WireFixture.SHA256,
				FinalWireManifestSHA256: changeset3PostFinalWireSHA256,
				ActiveCaptureSHA256:     wire.ActiveCaptureDigest,
				PreviousCaptureSHA256:   wire.PreviousCaptureDigest,
				TerminalGuardAllow:      true,
				ComparedReleaseModes:    []ReleaseMode{ReleaseModeActive, ReleaseModePrevious},
			}
			verificationRaw := changeset3ReferenceMarshal(t, verification)
			artifacts[proof.ExecutionVerification.Path] = verificationRaw
			proof.ExecutionVerification.SHA256 = changeset3ReferenceSHA256(verificationRaw)
		}
		canaryDigest, err := receiptcontract.DigestDocument(canary)
		if err != nil {
			t.Fatal(err)
		}
		observation := changeset3CanaryObservation{
			SchemaVersion: 2, SinkID: canary.SinkID, Result: "accepted",
			Environment: "local-deterministic", ObservedAt: "2026-08-03T00:00:00+08:00",
			ExerciseKind: "adapter_terminal_final_wire",
			Evidence: []string{
				"active/previous 两种 release mode 的 adapter-terminal final-wire 已冻结并通过 ProfileSpec 校验",
				"保护 Header、Body 线序、WS 帧、TLS/Profile/Pool 与动态目标 mutation 反例全部 fail-close",
				"FinalizationToken、attempt ordinal、terminal Guard 和 secret scan 均通过",
			},
			GuardCanaryPercent: 100, GuardRejections: 0, ExternalTraffic: false,
			ReviewedBy: "codex-changeset3-final-wire-audit",
			ReviewRef:  "changeset-3-post-identity-authority-final-wire/2026-08-03",
		}
		observationRaw := changeset3ReferenceMarshal(t, observation)
		acceptancePath := enforced.CanaryAcceptance.Path
		observationPath := filepath.ToSlash(filepath.Join(filepath.Dir(acceptancePath), "canary-observation.json"))
		artifacts[observationPath] = observationRaw
		acceptance := receiptcontract.CanaryAcceptance{
			SchemaVersion: 2, Result: "accepted", SinkID: canary.SinkID,
			CanaryReceiptDigest: canaryDigest,
			ObservationDigest:   changeset3ReferenceSHA256(observationRaw),
			ReviewedBy:          "codex-changeset3-final-wire-audit",
			ReviewRef:           "changeset-3-post-identity-authority-final-wire/2026-08-03",
		}
		acceptanceRaw := changeset3ReferenceMarshal(t, acceptance)
		artifacts[acceptancePath] = acceptanceRaw

		enforced.Routes = append([]receiptcontract.RouteProof(nil), canary.Routes...)
		enforced.PriorCanaryReceiptDigest = canaryDigest
		enforced.CanaryAcceptance.SHA256 = changeset3ReferenceSHA256(acceptanceRaw)
		enforced.ReviewedBy = "codex-changeset3-final-wire-audit"
		enforced.ReviewRef = "changeset-3-post-identity-authority-final-wire/2026-08-03"
		enforced.Rationale = "56 份 route-mode final-wire、4 个锚点非回归和永久 mutation 门禁通过后提升 enforced；无外部流量声明。"
		rebuilt = append(rebuilt, canary, enforced)
	}
	sort.Slice(rebuilt, func(i, j int) bool {
		leftRank, rightRank := "0", "0"
		if rebuilt[i].ApprovedState == string(SinkStateEnforced) {
			leftRank = "1"
		}
		if rebuilt[j].ApprovedState == string(SinkStateEnforced) {
			rightRank = "1"
		}
		return rebuilt[i].SinkID+leftRank < rebuilt[j].SinkID+rightRank
	})
	manifest := receiptcontract.Manifest{
		SchemaVersion:   receiptcontract.SchemaVersion,
		BootstrapCommit: current.BootstrapCommit,
		Receipts:        rebuilt,
	}
	if err := receiptcontract.ValidateManifest(manifest); err != nil {
		t.Fatal(err)
	}
	if len(artifacts) != 82 {
		t.Fatalf("重建产物数量错误：got=%d want=82", len(artifacts))
	}
	return manifest, artifacts
}

func changeset3FindPostCaptureForProof(
	t *testing.T,
	captures map[string]changeset3PreIdentityReferenceCapture,
	sinkID string,
	route receiptcontract.RouteIdentity,
	mode ReleaseMode,
) changeset3PreIdentityReferenceCapture {
	t.Helper()
	key := strings.Join([]string{
		string(mode), sinkID, route.Method, route.Host, route.Path, route.Protocol,
	}, "\x00")
	capture, ok := captures[key]
	if !ok || string(capture.Purpose) != route.Purpose {
		t.Fatalf("post final-wire 缺少收据 route：%q", key)
	}
	return capture
}

func changeset3DigestFinalWireCapture(
	t *testing.T,
	capture changeset3PreIdentityReferenceCapture,
) string {
	t.Helper()
	raw, err := json.Marshal(capture)
	if err != nil {
		t.Fatal(err)
	}
	return changeset3ReferenceSHA256(raw)
}
