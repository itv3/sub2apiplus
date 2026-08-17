package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	changeset3PostFinalWireSHA256       = "c824ffb0ab6e2429c09f9ac517cf3e6f96860c7c6ef77c229757fd690bdbcf0f"
	changeset3PostSecretScanSHA256      = "94e400de321b64784203041ac6320186b6b8757723205681306333318f372050"
	changeset4SourceTransitionSHA256    = "85cfb8e5324b5063b480f5e27cab1781500a5a3aba1957aae581f0ed0e62d478"
	changeset5SourceTransitionSHA256    = "e022d78b3af69a937a60a388009fa4ecafa8042410cf5602f32ecff7c29b176d"
	changeset6SourceTransitionSHA256    = "cdd6eab06acac15923e17562525eed974ee12e0e5cf3421e1303d01ecba549d7"
	changeset6ReviewTransitionSHA256    = "75044063ea497e07ad9818d0061f3f7fe4d7a7fa350fa4f6c554dfc108322fe2"
	maintenanceRetirementSHA256         = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"
	maintenanceCIRepairSHA256           = "b2a395259b2b0b8b95aca993f1a5aaf93e4928878cefd8675f34844a8b7fb3a5"
	maintenanceCookieFinalizeSHA256     = "0c6f4c6a73bf7005c0472e4f43a271f9314ea36b1a0fabe82cad7a32d77fd97f"
	evidenceDirectoryTransitionSHA256   = "524364297baf9b8492802c49fcf3963be15c0b320fa1558966487d62ea03d96f"
	staticURLClosureTransitionSHA256    = "c9a0765c332e0b28fe866fe444522613bbe99bacd1bf1ce520e17e15ec5b5e4b"
	requestCompressionTransitionSHA256  = "aae6b2569aa7d80e2fffdc7ff51b2b79a825e2d7ec9f2375b820b7bfdaa026fc"
	executorOneShotTransitionSHA256     = "73b79e4061a8e3becabb5a361aa341c855ddd0296a94fd2057296d798b6048ec"
	multiPersonaControlTransitionSHA256 = "139f4844085942b709a68b61d2b51f863189ada780d361ace91cad8a6ae86bb2"
	multiPersonaControlV2SHA256         = "354d32c903a14cd287c1a9fc9269f20af1e307b589947bbc29a8d883b0963872"
)

func TestChangeset3PostIdentityAuthorityFinalWireIsFrozen(t *testing.T) {
	manifestPath := "../../../docs/egress/migration/post_identity_authority_refactor_final_wire/manifest.json"
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
	maintenanceTransition := loadMaintenanceFinalWireSourceTransition(t)
	ciRepairTransition := loadMaintenanceCIRepairSourceTransition(t)
	cookieFinalizeTransition := loadMaintenanceCookieFinalizeSourceTransition(t)
	directoryTransition := loadEvidenceDirectoryConsolidationSourceTransition(t)
	staticURLClosureTransition := loadCompilerStaticURLClosureSourceTransition(t)
	requestCompressionTransition := loadCodexRequestCompressionSourceTransition(t)
	executorOneShotTransition := loadExecutorOneShotSourceTransition(t)
	multiPersonaControlTransition := loadMultiPersonaControlSourceTransition(t)
	multiPersonaControlV2Transition := loadMultiPersonaControlSourceTransitionV2(t)
	// Codex 手册由上游 source-transition 门禁消费，不属于 final-wire 源码清单。
	delete(multiPersonaControlV2Transition, "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md")
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
		if approved, ok := maintenanceTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("维护退休 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(maintenanceTransition, source.Path)
		}
		if approved, ok := ciRepairTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("CI 修复 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(ciRepairTransition, source.Path)
		}
		if approved, ok := cookieFinalizeTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("HTTP Cookie 定型 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(cookieFinalizeTransition, source.Path)
		}
		if approved, ok := directoryTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("证据目录收口 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(directoryTransition, source.Path)
		}
		if approved, ok := staticURLClosureTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Compiler 静态 URL 封闭 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(staticURLClosureTransition, source.Path)
		}
		if approved, ok := requestCompressionTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Codex 请求压缩 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(requestCompressionTransition, source.Path)
		}
		if approved, ok := executorOneShotTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Executor 一次性兼容退休 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(executorOneShotTransition, source.Path)
		}
		if approved, ok := multiPersonaControlTransition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("多 Persona 控制层 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(multiPersonaControlTransition, source.Path)
		}
		if approved, ok := multiPersonaControlV2Transition[source.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("多 Persona 控制层 v2 source transition 未承接上一层摘要：%s", source.Path)
			}
			expected = approved.ToSHA256
			delete(multiPersonaControlV2Transition, source.Path)
		}
		if got != expected &&
			compatibilityCodeRetirementTransitionSupersedes(source.Path, expected, got) {
			expected = got
		}
		if got != expected {
			t.Fatalf("post-refactor 捕获源码已漂移：%s got=%s want=%s", source.Path, got, expected)
		}
	}
	if len(changeset4Transition) != 0 || len(changeset5Transition) != 0 || len(changeset6Transition) != 0 ||
		len(changeset6ReviewTransition) != 0 || len(maintenanceTransition) != 0 || len(ciRepairTransition) != 0 ||
		len(cookieFinalizeTransition) != 0 || len(directoryTransition) != 0 || len(staticURLClosureTransition) != 0 ||
		len(requestCompressionTransition) != 0 || len(executorOneShotTransition) != 0 ||
		len(multiPersonaControlTransition) != 0 || len(multiPersonaControlV2Transition) != 0 {
		t.Fatalf("source transition 含未发生的漂移：changeset4=%v changeset5=%v changeset6=%v changeset6_review=%v maintenance=%v ci_repair=%v cookie_finalize=%v directory=%v static_url_closure=%v request_compression=%v executor_one_shot=%v multi_persona=%v multi_persona_v2=%v",
			changeset4Transition, changeset5Transition, changeset6Transition, changeset6ReviewTransition,
			maintenanceTransition, ciRepairTransition, cookieFinalizeTransition, directoryTransition,
			staticURLClosureTransition, requestCompressionTransition, executorOneShotTransition,
			multiPersonaControlTransition, multiPersonaControlV2Transition)
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
	approvedDeltaPath := strings.Replace(
		manifest.Comparison.ApprovedDeltaPath,
		"docs/changeset3/", "docs/egress/migration/", 1,
	)
	approvedPath := filepath.Join("../../..", filepath.FromSlash(approvedDeltaPath))
	approvedRaw, err := os.ReadFile(approvedPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(approvedRaw); got != manifest.Comparison.ApprovedDeltaSHA256 {
		t.Fatalf("approved delta 摘要漂移：got=%s want=%s", got, manifest.Comparison.ApprovedDeltaSHA256)
	}

	scanPath := "../../../docs/egress/migration/post_identity_authority_refactor_final_wire/secret-scan.json"
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
	transitionPath := "../../../docs/egress/source-freeze/changeset3-source-transition.json"
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
	transitionPath := "../../../docs/egress/consolidation/changeset3-source-transition.json"
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
	transitionPath := "../../../docs/egress/validation/changeset3-source-transition.json"
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
	transitionPath := "../../../docs/egress/validation/review-source-transition.json"
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

func loadMaintenanceFinalWireSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/official-egress-consolidation-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != maintenanceRetirementSHA256 {
		t.Fatalf("维护退休收据摘要漂移：got=%s want=%s", got, maintenanceRetirementSHA256)
	}
	var receipt struct {
		SchemaVersion             string                          `json:"schema_version"`
		BaseCommit                string                          `json:"base_commit"`
		FinalWireSourceTransition changeset4SourceTransitionEntry `json:"final_wire_source_transition"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil {
		t.Fatal(err)
	}
	entry := receipt.FinalWireSourceTransition
	if receipt.SchemaVersion != "official-egress-consolidation-retirement/v1" ||
		receipt.BaseCommit != "ba485663c3bc0af6802cf6dfc4e19139ef00e371" ||
		strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
		strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
		t.Fatalf("维护退休 source transition 非法：%+v", receipt)
	}
	return map[string]changeset4SourceTransitionEntry{entry.Path: entry}
}

func loadMaintenanceCIRepairSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/ci-repair-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != maintenanceCIRepairSHA256 {
		t.Fatalf("CI 修复 source transition 摘要漂移：got=%s want=%s", got, maintenanceCIRepairSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-ci-repair-source-transition/v1" ||
		receipt.PriorTransition != "docs/maintenance/official-egress-consolidation-retirement.json" ||
		receipt.PriorTransitionSHA256 != maintenanceRetirementSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 5 {
		t.Fatalf("CI 修复 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("CI 修复 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("CI 修复 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

func loadMaintenanceCookieFinalizeSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/http-cookie-finalization-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != maintenanceCookieFinalizeSHA256 {
		t.Fatalf("HTTP Cookie 定型 source transition 摘要漂移：got=%s want=%s", got, maintenanceCookieFinalizeSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-http-cookie-finalization-source-transition/v1" ||
		receipt.PriorTransition != "docs/maintenance/ci-repair-source-transition.json" ||
		receipt.PriorTransitionSHA256 != maintenanceCIRepairSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 1 {
		t.Fatalf("HTTP Cookie 定型 source transition 顶层事实非法：%+v", receipt)
	}
	entry := receipt.Transitions[0]
	if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
		strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
		t.Fatalf("HTTP Cookie 定型 source transition 条目不完整：%+v", entry)
	}
	return map[string]changeset4SourceTransitionEntry{entry.Path: entry}
}

func loadEvidenceDirectoryConsolidationSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/evidence-directory-consolidation-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != evidenceDirectoryTransitionSHA256 {
		t.Fatalf("证据目录收口 source transition 摘要漂移：got=%s want=%s", got, evidenceDirectoryTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion         string `json:"schema_version"`
		PriorTransition       string `json:"prior_transition"`
		PriorTransitionSHA256 string `json:"prior_transition_sha256"`
		PathMappings          []struct {
			From string `json:"from"`
			To   string `json:"to"`
		} `json:"path_mappings"`
		RemovedNarrativeFiles      []string                          `json:"removed_narrative_files"`
		RetainedProductDirectories []string                          `json:"retained_product_directories"`
		Transitions                []changeset4SourceTransitionEntry `json:"transitions"`
		Result                     string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-evidence-directory-consolidation-source-transition/v1" ||
		receipt.PriorTransition != "docs/egress/maintenance/http-cookie-finalization-source-transition.json" ||
		receipt.PriorTransitionSHA256 != maintenanceCookieFinalizeSHA256 || receipt.Result != "passed" ||
		len(receipt.PathMappings) != 10 || len(receipt.RemovedNarrativeFiles) != 16 ||
		!slices.Equal(receipt.RetainedProductDirectories, []string{"docs/legal"}) || len(receipt.Transitions) != 1 {
		t.Fatalf("证据目录收口 source transition 顶层事实非法：%+v", receipt)
	}
	entry := receipt.Transitions[0]
	if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
		strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
		t.Fatalf("证据目录收口 source transition 条目不完整：%+v", entry)
	}
	return map[string]changeset4SourceTransitionEntry{entry.Path: entry}
}

func loadCodexRequestCompressionSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/codex-request-compression-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != requestCompressionTransitionSHA256 {
		t.Fatalf("Codex 请求压缩 source transition 摘要漂移：got=%s want=%s", got, requestCompressionTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-codex-request-compression-source-transition/v1" ||
		receipt.PriorTransition != "docs/egress/maintenance/compiler-static-url-closure-source-transition.json" ||
		receipt.PriorTransitionSHA256 != staticURLClosureTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 1 {
		t.Fatalf("Codex 请求压缩 source transition 顶层事实非法：%+v", receipt)
	}
	entry := receipt.Transitions[0]
	if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
		strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
		t.Fatalf("Codex 请求压缩 source transition 条目不完整：%+v", entry)
	}
	return map[string]changeset4SourceTransitionEntry{entry.Path: entry}
}

func loadExecutorOneShotSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/executor-one-shot-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != executorOneShotTransitionSHA256 {
		t.Fatalf("Executor 一次性兼容退休 source transition 摘要漂移：got=%s want=%s", got, executorOneShotTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-executor-one-shot-source-transition/v1" ||
		receipt.PriorTransition != "docs/egress/maintenance/codex-request-compression-source-transition.json" ||
		receipt.PriorTransitionSHA256 != requestCompressionTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 1 {
		t.Fatalf("Executor 一次性兼容退休 source transition 顶层事实非法：%+v", receipt)
	}
	entry := receipt.Transitions[0]
	if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
		strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
		t.Fatalf("Executor 一次性兼容退休 source transition 条目不完整：%+v", entry)
	}
	return map[string]changeset4SourceTransitionEntry{entry.Path: entry}
}

func loadMultiPersonaControlSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/multi-persona-control-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != multiPersonaControlTransitionSHA256 {
		t.Fatalf("多 Persona 控制层 source transition 摘要漂移：got=%s want=%s",
			got, multiPersonaControlTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-multi-persona-control-source-transition/v1" ||
		receipt.PriorTransition != "docs/egress/maintenance/executor-one-shot-source-transition.json" ||
		receipt.PriorTransitionSHA256 != executorOneShotTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 2 {
		t.Fatalf("多 Persona 控制层 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("多 Persona 控制层 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("多 Persona 控制层 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

type multiPersonaControlSourceTransitionV2Receipt struct {
	SchemaVersion         string `json:"schema_version"`
	PriorTransition       string `json:"prior_transition"`
	PriorTransitionSHA256 string `json:"prior_transition_sha256"`
	AdditionalPrior       []struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"additional_prior_transitions"`
	Transitions []changeset4SourceTransitionEntry `json:"transitions"`
	Result      string                            `json:"result"`
}

func readMultiPersonaControlSourceTransitionV2() (
	multiPersonaControlSourceTransitionV2Receipt,
	[]byte,
	error,
) {
	var receipt multiPersonaControlSourceTransitionV2Receipt
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/multi-persona-control-source-transition-v2.json",
	)
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	return receipt, raw, nil
}

func validateMultiPersonaControlSourceTransitionV2(
	receipt multiPersonaControlSourceTransitionV2Receipt,
	raw []byte,
) error {
	if changeset3ReferenceSHA256(raw) != multiPersonaControlV2SHA256 ||
		receipt.SchemaVersion != "official-egress-multi-persona-control-source-transition/v2" ||
		receipt.PriorTransition != "docs/egress/maintenance/multi-persona-control-source-transition.json" ||
		receipt.PriorTransitionSHA256 != multiPersonaControlTransitionSHA256 ||
		len(receipt.AdditionalPrior) != 1 ||
		receipt.AdditionalPrior[0].Path !=
			"docs/egress/maintenance/runtime-reliability-repair-source-transition.json" ||
		receipt.AdditionalPrior[0].SHA256 != runtimeReliabilityRepairTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 3 {
		return errors.New("多 Persona 控制层 v2 source transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			return errors.New("多 Persona 控制层 v2 source transition 条目不完整")
		}
		paths = append(paths, entry.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("多 Persona 控制层 v2 source transition 路径未排序或重复")
	}
	return nil
}

func loadMultiPersonaControlSourceTransitionV2(
	t *testing.T,
) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receipt, raw, err := readMultiPersonaControlSourceTransitionV2()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateMultiPersonaControlSourceTransitionV2(receipt, raw); err != nil {
		t.Fatal(err)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("多 Persona 控制层 v2 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}

func multiPersonaControlSourceTransitionV2Supersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, raw, err := readMultiPersonaControlSourceTransitionV2()
	if err != nil || validateMultiPersonaControlSourceTransitionV2(receipt, raw) != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func loadCompilerStaticURLClosureSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receiptPath := "../../../docs/egress/maintenance/compiler-static-url-closure-source-transition.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != staticURLClosureTransitionSHA256 {
		t.Fatalf("Compiler 静态 URL 封闭 source transition 摘要漂移：got=%s want=%s", got, staticURLClosureTransitionSHA256)
	}
	var receipt struct {
		SchemaVersion         string                            `json:"schema_version"`
		PriorTransition       string                            `json:"prior_transition"`
		PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
		Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
		Result                string                            `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-compiler-static-url-closure-source-transition/v1" ||
		receipt.PriorTransition != "docs/egress/maintenance/evidence-directory-consolidation-source-transition.json" ||
		receipt.PriorTransitionSHA256 != evidenceDirectoryTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 3 {
		t.Fatalf("Compiler 静态 URL 封闭 source transition 顶层事实非法：%+v", receipt)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || strings.TrimSpace(entry.FromSHA256) == "" ||
			strings.TrimSpace(entry.ToSHA256) == "" || strings.TrimSpace(entry.Reason) == "" {
			t.Fatalf("Compiler 静态 URL 封闭 source transition 条目不完整：%+v", entry)
		}
		if _, duplicate := result[entry.Path]; duplicate {
			t.Fatalf("Compiler 静态 URL 封闭 source transition 路径重复：%s", entry.Path)
		}
		result[entry.Path] = entry
	}
	return result
}
