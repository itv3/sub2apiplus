package officialegress

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	changeset3PostFinalWireSHA256    = "c824ffb0ab6e2429c09f9ac517cf3e6f96860c7c6ef77c229757fd690bdbcf0f"
	changeset3PostSecretScanSHA256   = "94e400de321b64784203041ac6320186b6b8757723205681306333318f372050"
	changeset4SourceTransitionSHA256 = "85cfb8e5324b5063b480f5e27cab1781500a5a3aba1957aae581f0ed0e62d478"
	changeset5SourceTransitionSHA256 = "e022d78b3af69a937a60a388009fa4ecafa8042410cf5602f32ecff7c29b176d"
	changeset6SourceTransitionSHA256 = "cdd6eab06acac15923e17562525eed974ee12e0e5cf3421e1303d01ecba549d7"
	changeset6ReviewTransitionSHA256 = "75044063ea497e07ad9818d0061f3f7fe4d7a7fa350fa4f6c554dfc108322fe2"
)

func TestChangeset3PostIdentityAuthorityFinalWireIsFrozen(t *testing.T) {
	manifestPath := "../../../docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json"
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset3PostFinalWireSHA256 {
		t.Fatalf("post-refactor final-wire 摘要漂移：got=%s want=%s", got, changeset3PostFinalWireSHA256)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var manifest changeset3PostFinalWireManifest
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.SchemaVersion != "changeset3-post-identity-authority-final-wire/v1" ||
		manifest.CaptureKind != "post_identity_authority_refactor_final_wire" ||
		manifest.ExternalTraffic || manifest.CredentialMaterial != "synthetic_attempt_local_only" ||
		manifest.RouteCount != 28 || manifest.CaptureCount != 56 || len(manifest.Captures) != 56 {
		t.Fatalf("post-refactor final-wire 顶层事实非法：%+v", manifest)
	}
	changeset4Transition := loadChangeset4SourceTransition(t)
	changeset5Transition := loadChangeset5SourceTransition(t)
	changeset6Transition := loadChangeset6SourceTransition(t)
	changeset6ReviewTransition := loadChangeset6ReviewSourceTransition(t)
	for _, source := range manifest.SourceMaterial {
		sourcePath := source.Path
		if !filepath.IsAbs(sourcePath) {
			sourcePath = filepath.Join("../../..", filepath.FromSlash(sourcePath))
		}
		sourceRaw, readErr := os.ReadFile(sourcePath)
		if readErr != nil {
			t.Fatalf("读取 post-refactor 捕获源码 %s：%v", source.Path, readErr)
		}
		got := changeset3ReferenceSHA256(sourceRaw)
		expected := source.SHA256
		if approved, ok := changeset4Transition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Changeset 4 source transition 未承接历史摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(changeset4Transition, source.Path)
		}
		if approved, ok := changeset5Transition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Changeset 5 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(changeset5Transition, source.Path)
		}
		if approved, ok := changeset6Transition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Changeset 6 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(changeset6Transition, source.Path)
		}
		if approved, ok := changeset6ReviewTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Changeset 6 复审 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(changeset6ReviewTransition, source.Path)
		}
		if got != expected {
			t.Fatalf("post-refactor 捕获源码已漂移：%s got=%s want=%s", source.Path, got, expected)
		}
	}
	if len(changeset4Transition) != 0 || len(changeset5Transition) != 0 || len(changeset6Transition) != 0 ||
		len(changeset6ReviewTransition) != 0 {
		t.Fatalf("source transition 含未发生的漂移：changeset4=%v changeset5=%v changeset6=%v changeset6_review=%v",
			changeset4Transition, changeset5Transition, changeset6Transition, changeset6ReviewTransition)
	}
	modes := map[ReleaseMode]int{}
	wsCaptureCount := 0
	uploadCaptureCount := 0
	anchorCaptureCount := 0
	for _, capture := range manifest.Captures {
		modes[capture.ReleaseMode]++
		if !capture.HasFinalizationToken || !capture.TerminalGuardAllow ||
			capture.AuthorityID != "codex.executor.changeset1b" ||
			capture.AttemptOrdinal != 1 || capture.AttemptReason != string(AttemptReasonInitial) ||
			capture.ProfileValidationResult != "passed" {
			t.Fatalf("post-refactor capture 权威事实非法：%s/%s", capture.SinkID, capture.ReleaseMode)
		}
		if capture.Anchor {
			anchorCaptureCount++
		}
		if capture.Protocol == WireProtocolWebSocket {
			wsCaptureCount++
			if capture.WebSocket == nil || len(capture.WebSocket.EventMatrix) == 0 {
				t.Fatalf("WS capture 缺少实际事件矩阵：%s/%s", capture.SinkID, capture.ReleaseMode)
			}
			for ordinal, event := range capture.WebSocket.EventMatrix {
				if event.FrameOrdinal != uint64(ordinal+1) || event.BodySHA256 == "" ||
					event.TypeShapeSHA256 == "" {
					t.Fatalf("WS frame 证据不完整：%s/%s %+v", capture.SinkID, capture.ReleaseMode, event)
				}
			}
		}
		if capture.SingleUseRawStream {
			uploadCaptureCount++
			if capture.SingleUseConsumptionCount != 1 || capture.DynamicTarget == nil ||
				capture.DynamicTarget.ValidationResult != "accepted_by_bundle_dynamic_target_policy" {
				t.Fatalf("动态上传单次消费证据非法：%+v", capture)
			}
		}
	}
	if modes[ReleaseModeActive] != 28 || modes[ReleaseModePrevious] != 28 ||
		wsCaptureCount != 6 || uploadCaptureCount != 2 || anchorCaptureCount != 8 {
		t.Fatalf("post-refactor capture 矩阵错误：modes=%v ws=%d upload=%d anchors=%d",
			modes, wsCaptureCount, uploadCaptureCount, anchorCaptureCount)
	}
	if manifest.Comparison.ReferenceSHA256 != changeset3PreIdentityReferenceSHA256 ||
		strings.TrimSpace(manifest.Comparison.ReferenceScope) == "" ||
		manifest.Comparison.ComparedCaptureCount != 56 ||
		manifest.Comparison.UnexpectedDiffCount != 0 ||
		manifest.Comparison.ApprovedDeltaCount == 0 ||
		manifest.Comparison.AppliedApprovedDeltas != manifest.Comparison.ApprovedDeltaCount ||
		manifest.Comparison.Result != "scoped_reference_match_with_explicit_approved_deltas" ||
		manifest.AnchorComparison.FixtureCount != 4 ||
		manifest.AnchorComparison.ComparedCaptureCount != 8 ||
		manifest.AnchorComparison.Result != "passed" {
		t.Fatalf("post-refactor 非回归比较非法：%+v %+v", manifest.Comparison, manifest.AnchorComparison)
	}
	approvedPath := filepath.Join("../../..", filepath.FromSlash(manifest.Comparison.ApprovedDeltaPath))
	approvedRaw, err := os.ReadFile(approvedPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(approvedRaw); got != manifest.Comparison.ApprovedDeltaSHA256 {
		t.Fatalf("approved delta 摘要漂移：got=%s want=%s", got, manifest.Comparison.ApprovedDeltaSHA256)
	}

	scanPath := "../../../docs/changeset3/post_identity_authority_refactor_final_wire/secret-scan.json"
	scanRaw, err := os.ReadFile(scanPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(scanRaw); got != changeset3PostSecretScanSHA256 {
		t.Fatalf("post-refactor secret scan 摘要漂移：got=%s", got)
	}
	var scan struct {
		SchemaVersion  string         `json:"schema_version"`
		Artifact       string         `json:"artifact"`
		ArtifactSHA256 string         `json:"artifact_sha256"`
		Matches        map[string]int `json:"matches"`
		Result         string         `json:"result"`
	}
	decoder = json.NewDecoder(bytes.NewReader(scanRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&scan); err != nil {
		t.Fatal(err)
	}
	if scan.SchemaVersion != "changeset3-secret-scan/v1" || scan.Artifact != "manifest.json" ||
		scan.ArtifactSHA256 != changeset3PostFinalWireSHA256 || scan.Result != "passed" ||
		scan.Matches["Bearer "] != 0 || scan.Matches["synthetic-access-token"] != 0 ||
		scan.Matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("post-refactor secret scan 非法：%+v", scan)
	}
}

type changeset4SourceTransitionEntry struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

func loadChangeset4SourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	transitionPath := "../../../docs/changeset4/changeset3-source-transition.json"
	raw, err := os.ReadFile(transitionPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset4SourceTransitionSHA256 {
		t.Fatalf("Changeset 4 source transition 摘要漂移：got=%s want=%s", got, changeset4SourceTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion           string                            `json:"schema_version"`
		Changeset               string                            `json:"changeset"`
		ReferenceManifest       string                            `json:"reference_manifest"`
		ReferenceManifestSHA256 string                            `json:"reference_manifest_sha256"`
		Transitions             []changeset4SourceTransitionEntry `json:"transitions"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "sub2api.changeset4.changeset3_source_transition.v1" ||
		receipt.Changeset != "4" ||
		receipt.ReferenceManifest != "docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json" ||
		receipt.ReferenceManifestSHA256 != changeset3PostFinalWireSHA256 || len(receipt.Transitions) == 0 {
		t.Fatalf("Changeset 4 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("Changeset 4 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("Changeset 4 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

func loadChangeset5SourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	transitionPath := "../../../docs/changeset5/changeset3-source-transition.json"
	raw, err := os.ReadFile(transitionPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset5SourceTransitionSHA256 {
		t.Fatalf("Changeset 5 source transition 摘要漂移：got=%s want=%s", got, changeset5SourceTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion           string                            `json:"schema_version"`
		Changeset               string                            `json:"changeset"`
		ReferenceManifest       string                            `json:"reference_manifest"`
		ReferenceManifestSHA256 string                            `json:"reference_manifest_sha256"`
		PriorTransition         string                            `json:"prior_transition"`
		PriorTransitionSHA256   string                            `json:"prior_transition_sha256"`
		Transitions             []changeset4SourceTransitionEntry `json:"transitions"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "sub2api.changeset5.changeset3_source_transition.v1" ||
		receipt.Changeset != "5" ||
		receipt.ReferenceManifest != "docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json" ||
		receipt.ReferenceManifestSHA256 != changeset3PostFinalWireSHA256 ||
		receipt.PriorTransition != "docs/changeset4/changeset3-source-transition.json" ||
		receipt.PriorTransitionSHA256 != changeset4SourceTransitionSHA256 ||
		len(receipt.Transitions) == 0 {
		t.Fatalf("Changeset 5 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("Changeset 5 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("Changeset 5 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

func loadChangeset6SourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	transitionPath := "../../../docs/changeset6/changeset3-source-transition.json"
	raw, err := os.ReadFile(transitionPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset6SourceTransitionSHA256 {
		t.Fatalf("Changeset 6 source transition 摘要漂移：got=%s want=%s", got, changeset6SourceTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion           string                            `json:"schema_version"`
		Changeset               string                            `json:"changeset"`
		ReferenceManifest       string                            `json:"reference_manifest"`
		ReferenceManifestSHA256 string                            `json:"reference_manifest_sha256"`
		PriorTransition         string                            `json:"prior_transition"`
		PriorTransitionSHA256   string                            `json:"prior_transition_sha256"`
		Transitions             []changeset4SourceTransitionEntry `json:"transitions"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "sub2api.changeset6.changeset3_source_transition.v1" ||
		receipt.Changeset != "6" ||
		receipt.ReferenceManifest != "docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json" ||
		receipt.ReferenceManifestSHA256 != changeset3PostFinalWireSHA256 ||
		receipt.PriorTransition != "docs/changeset5/changeset3-source-transition.json" ||
		receipt.PriorTransitionSHA256 != changeset5SourceTransitionSHA256 ||
		len(receipt.Transitions) == 0 {
		t.Fatalf("Changeset 6 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("Changeset 6 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("Changeset 6 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

func loadChangeset6ReviewSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	transitionPath := "../../../docs/changeset6/review-source-transition.json"
	raw, err := os.ReadFile(transitionPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset6ReviewTransitionSHA256 {
		t.Fatalf("Changeset 6 复审 source transition 摘要漂移：got=%s want=%s", got, changeset6ReviewTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion           string                            `json:"schema_version"`
		Changeset               string                            `json:"changeset"`
		ReferenceManifest       string                            `json:"reference_manifest"`
		ReferenceManifestSHA256 string                            `json:"reference_manifest_sha256"`
		PriorTransition         string                            `json:"prior_transition"`
		PriorTransitionSHA256   string                            `json:"prior_transition_sha256"`
		Transitions             []changeset4SourceTransitionEntry `json:"transitions"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "sub2api.changeset6.review_source_transition.v1" ||
		receipt.Changeset != "6-review" ||
		receipt.ReferenceManifest != "docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json" ||
		receipt.ReferenceManifestSHA256 != changeset3PostFinalWireSHA256 ||
		receipt.PriorTransition != "docs/changeset6/changeset3-source-transition.json" ||
		receipt.PriorTransitionSHA256 != changeset6SourceTransitionSHA256 ||
		len(receipt.Transitions) == 0 {
		t.Fatalf("Changeset 6 复审 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("Changeset 6 复审 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("Changeset 6 复审 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}
