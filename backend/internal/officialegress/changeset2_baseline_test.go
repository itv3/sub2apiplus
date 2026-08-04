package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

type changeset2CompatibilityLock struct {
	SchemaVersion   int    `json:"schema_version"`
	Changeset       string `json:"changeset"`
	Task            string `json:"task"`
	Status          string `json:"status"`
	FrozenAt        string `json:"frozen_at"`
	BootstrapCommit string `json:"bootstrap_commit"`
	Migration       struct {
		Path                string   `json:"path"`
		SHA256              string   `json:"sha256"`
		SchemaVersion       int      `json:"schema_version"`
		RouteIdentityFields []string `json:"route_identity_fields"`
		Receipts            []struct {
			SinkID        string                        `json:"sink_id"`
			ApprovedState string                        `json:"approved_state"`
			BindingDigest string                        `json:"binding_digest"`
			Route         receiptcontract.RouteIdentity `json:"route"`
		} `json:"receipts"`
	} `json:"migration_receipts"`
	ReleaseGraph struct {
		Path                   string `json:"path"`
		SHA256                 string `json:"sha256"`
		NodeCount              int    `json:"node_count"`
		ActiveBuildID          string `json:"active_build_id"`
		PreviousBuildID        string `json:"previous_build_id"`
		ActiveHTTPWireDigest   string `json:"active_http_wire_digest"`
		PreviousHTTPWireDigest string `json:"previous_http_wire_digest"`
		ActiveWSWireDigest     string `json:"active_ws_wire_digest"`
		PreviousWSWireDigest   string `json:"previous_ws_wire_digest"`
	} `json:"release_graph_before_changeset2"`
	Snapshot struct {
		CatalogPath        string `json:"catalog_path"`
		CatalogSHA256      string `json:"catalog_sha256"`
		Version            string `json:"version"`
		ProfileDigest      string `json:"profile_digest"`
		SnapshotPath       string `json:"snapshot_path"`
		SnapshotFileSHA256 string `json:"snapshot_file_sha256"`
	} `json:"snapshot_before_changeset2"`
	EnforcedWire struct {
		Source    string `json:"source"`
		SinkCount int    `json:"sink_count"`
		Rule      string `json:"rule"`
	} `json:"enforced_wire_evidence"`
}

type changeset2ScannerSnapshot struct {
	SchemaVersion int    `json:"schema_version"`
	Changeset     string `json:"changeset"`
	Task          string `json:"task"`
	CapturedAt    string `json:"captured_at"`
	Scanner       struct {
		Mode                            string   `json:"mode"`
		BuildContexts                   []string `json:"build_contexts"`
		AllCandidateCount               int      `json:"all_candidate_count"`
		AllCandidateIDsSHA256           string   `json:"all_candidate_ids_sha256"`
		CodexBusinessCandidateCount     int      `json:"codex_business_candidate_count"`
		CodexBusinessCandidateIDsSHA256 string   `json:"codex_business_candidate_ids_sha256"`
	} `json:"scanner"`
	Candidates []struct {
		ScanCandidateID  string   `json:"scan_candidate_id"`
		ASTFingerprint   string   `json:"ast_fingerprint"`
		RuntimeSinkID    string   `json:"runtime_sink_id"`
		Purpose          string   `json:"purpose"`
		EndpointEvidence string   `json:"endpoint_evidence"`
		Routes           []string `json:"routes"`
		Protocol         string   `json:"protocol"`
		Backend          string   `json:"backend"`
		EnforcementState string   `json:"enforcement_state"`
	} `json:"candidates"`
}

type changeset2ActiveSourceInventory struct {
	SchemaVersion   int    `json:"schema_version"`
	Changeset       string `json:"changeset"`
	Task            string `json:"task"`
	SnapshotKind    string `json:"snapshot_kind"`
	ProductionFiles []struct {
		Category string   `json:"category"`
		Paths    []string `json:"paths"`
		Symbols  []string `json:"symbols"`
	} `json:"production_files"`
	CompletionRule string `json:"completion_rule"`
}

func TestChangeset2CompatibilityBaselinePreservesReceiptsAndRouteIdentity(t *testing.T) {
	var lock changeset2CompatibilityLock
	readChangeset2StrictJSON(t, "../../../docs/changeset2/compatibility-lock.json", &lock)
	if lock.SchemaVersion != 1 || lock.Changeset != "2" || lock.Task != "2.0" ||
		lock.Status != "completed_baseline_frozen" || lock.BootstrapCommit != BootstrapCommit {
		t.Fatalf("变更集 2 基线元数据非法：%+v", lock)
	}
	wantRouteFields := []string{"method", "host", "path", "purpose", "protocol"}
	if !slices.Equal(lock.Migration.RouteIdentityFields, wantRouteFields) {
		t.Fatalf("旧版 RouteIdentity 审计投影发生变化：%v", lock.Migration.RouteIdentityFields)
	}

	receiptRaw := readChangeset2WorkspaceArtifact(t, lock.Migration.Path)
	assertChangeset2SHA256(t, receiptRaw, lock.Migration.SHA256, "MigrationReceipt 原文")
	manifest, err := receiptcontract.ParseManifest(receiptRaw)
	if err != nil {
		t.Fatal(err)
	}
	if manifest.SchemaVersion != lock.Migration.SchemaVersion ||
		len(manifest.Receipts) != len(lock.Migration.Receipts) || len(manifest.Receipts) != 8 {
		t.Fatalf("MigrationReceipt 数量或 schema 漂移：got=%d", len(manifest.Receipts))
	}

	for index, frozen := range lock.Migration.Receipts {
		actual := manifest.Receipts[index]
		if actual.SinkID != frozen.SinkID || actual.ApprovedState != frozen.ApprovedState ||
			actual.BindingDigest != frozen.BindingDigest || len(actual.Routes) != 1 ||
			actual.Routes[0].Route != frozen.Route {
			t.Fatalf("MigrationReceipt[%d] 与冻结身份不一致：got=%+v want=%+v", index, actual, frozen)
		}
		_, _, input := migrationTestBinding(t, frozen.SinkID)
		digest, digestErr := sinkBindingIdentityDigest(input)
		if digestErr != nil {
			t.Fatal(digestErr)
		}
		if digest != frozen.BindingDigest || len(input.Routes) != 1 {
			t.Fatalf("%s 的 binding_digest 无法按旧投影复算：got=%s want=%s", frozen.SinkID, digest, frozen.BindingDigest)
		}
		route := input.Routes[0]
		projection := receiptcontract.RouteIdentity{
			Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
			Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
		}
		if projection != frozen.Route {
			t.Fatalf("%s 无法展开为旧版五元组：got=%+v want=%+v", frozen.SinkID, projection, frozen.Route)
		}
	}

	// 旧 Snapshot 文件是追加式 Catalog 的不可变历史。后续允许增加同版本新摘要，
	// 但不得覆盖这个变更集 2 开始前的画像文件。
	oldSnapshot := readChangeset2WorkspaceArtifact(t, lock.Snapshot.SnapshotPath)
	assertChangeset2SHA256(t, oldSnapshot, lock.Snapshot.SnapshotFileSHA256, "旧 ProfileSpec Snapshot")
	if lock.ReleaseGraph.NodeCount != 4 || lock.ReleaseGraph.ActiveBuildID == lock.ReleaseGraph.PreviousBuildID ||
		lock.ReleaseGraph.ActiveHTTPWireDigest == lock.ReleaseGraph.PreviousHTTPWireDigest ||
		lock.ReleaseGraph.ActiveWSWireDigest == lock.ReleaseGraph.PreviousWSWireDigest ||
		lock.EnforcedWire.SinkCount != 4 {
		t.Fatalf("发布/线级变更前摘要未冻结真实 active/previous 差异：%+v", lock.ReleaseGraph)
	}
}

func TestChangeset2ScannerAndActiveSourceSnapshotsAreComplete(t *testing.T) {
	var scanner changeset2ScannerSnapshot
	readChangeset2StrictJSON(t, "../../../docs/changeset2/scanner-candidates.json", &scanner)
	if scanner.SchemaVersion != 1 || scanner.Changeset != "2" || scanner.Task != "2.0" ||
		scanner.Scanner.Mode != "bootstrap" || scanner.Scanner.AllCandidateCount != 179 ||
		scanner.Scanner.CodexBusinessCandidateCount != len(scanner.Candidates) || len(scanner.Candidates) != 26 ||
		!receiptcontract.ValidSHA256(scanner.Scanner.AllCandidateIDsSHA256) ||
		!receiptcontract.ValidSHA256(scanner.Scanner.CodexBusinessCandidateIDsSHA256) {
		t.Fatalf("scanner 基线摘要不完整：%+v", scanner.Scanner)
	}
	var candidateIDs strings.Builder
	previousID := ""
	seenSink := map[string]bool{}
	for _, candidate := range scanner.Candidates {
		if candidate.ScanCandidateID == "" || previousID >= candidate.ScanCandidateID ||
			len(candidate.ASTFingerprint) != 12 || candidate.RuntimeSinkID == "" ||
			candidate.Purpose == "" || len(candidate.Routes) == 0 ||
			candidate.EnforcementState != string(SinkStateLegacyObserve) {
			t.Fatalf("scanner 候选未按旧身份完整冻结：%+v", candidate)
		}
		previousID = candidate.ScanCandidateID
		_, _ = candidateIDs.WriteString(candidate.ScanCandidateID)
		_ = candidateIDs.WriteByte('\n')
		seenSink[candidate.RuntimeSinkID] = true
	}
	assertChangeset2SHA256(t, []byte(candidateIDs.String()), scanner.Scanner.CodexBusinessCandidateIDsSHA256, "Codex 业务候选 ID")
	for _, required := range []string{
		string(SinkCodexOAuthExchange), string(SinkCodexOAuthRefresh),
		string(SinkCodexResponsesWS), string(SinkCodexResponsesWSHTTPBridge),
		string(SinkCodexFilesBlobUpload),
	} {
		if !seenSink[required] {
			t.Fatalf("scanner 基线缺少 CS2 关键 Sink：%s", required)
		}
	}
	if scanner.Candidates[0].RuntimeSinkID != string(SinkCodexOAuthExchange) ||
		scanner.Candidates[0].EndpointEvidence != string(EndpointEvidenceTransportOnly) ||
		scanner.Candidates[1].RuntimeSinkID != string(SinkCodexOAuthRefresh) ||
		!slices.Equal(scanner.Candidates[0].Routes, scanner.Candidates[1].Routes) {
		t.Fatal("OAuth exchange/refresh 的同物理 route 与不同证据状态未正确冻结")
	}

	var inventory changeset2ActiveSourceInventory
	readChangeset2StrictJSON(t, "../../../docs/changeset2/active-source-inventory.json", &inventory)
	wantCategories := []string{
		"transition_adapter_and_provider", "service_registry_and_export_bridges",
		"active_version_and_wrappers", "package_active_body_order",
		"repository_reverse_active_builder",
	}
	gotCategories := make([]string, 0, len(inventory.ProductionFiles))
	seenPath := map[string]bool{}
	for _, group := range inventory.ProductionFiles {
		if group.Category == "" || len(group.Paths) == 0 || len(group.Symbols) == 0 {
			t.Fatalf("active 引用分组不完整：%+v", group)
		}
		gotCategories = append(gotCategories, group.Category)
		for _, path := range group.Paths {
			if path == "" || seenPath[path] {
				t.Fatalf("active 引用路径为空或重复：%s", path)
			}
			seenPath[path] = true
		}
	}
	if !slices.Equal(gotCategories, wantCategories) || inventory.SnapshotKind != "before_removal" ||
		!strings.Contains(inventory.CompletionRule, "归零") {
		t.Fatalf("active 引用清单未覆盖五类清理边界：%v", gotCategories)
	}
}

func readChangeset2StrictJSON(t *testing.T, path string, target any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("变更集 2 基线 JSON 尾部存在额外数据")
	}
}

func readChangeset2WorkspaceArtifact(t *testing.T, path string) []byte {
	t.Helper()
	if strings.TrimSpace(path) == "" || strings.HasPrefix(path, "/") || strings.Contains(path, "..") {
		t.Fatalf("变更集 2 基线引用路径非法：%s", path)
	}
	raw, err := os.ReadFile("../../../" + path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func assertChangeset2SHA256(t *testing.T, raw []byte, want, label string) {
	t.Helper()
	if !receiptcontract.ValidSHA256(want) {
		t.Fatalf("%s 的冻结摘要非法：%s", label, want)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != want {
		t.Fatalf("%s 已与任务 2.0 基线漂移", label)
	}
}
