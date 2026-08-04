package service

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecapture"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
)

const changeset6PostFinalWireOutputEnv = "CHANGESET6_POST_FINAL_WIRE_OUTPUT"

const (
	changeset6PostFinalWireManifestSHA256 = "169152bcd97bd3ce63983b850f08ac4031597c2a8b855f27304d1fc4b6285e5f"
	changeset6PostFinalWireSecretSHA256   = "64b960366f3a14efdb3aa4b1e0418c154a9d40079be1c985e3dbf942722484d3"
	changeset6PostFinalWireReceiptSHA256  = "444c357ec788540cbd523694eab9b19df4ab1c8844624eb8e6df59a95c980634"
)

type changeset6PostFinalWireReceipt struct {
	SchemaVersion          string                            `json:"schema_version"`
	Changeset              string                            `json:"changeset"`
	BaselineManifestPath   string                            `json:"baseline_manifest_path"`
	BaselineManifestSHA256 string                            `json:"baseline_manifest_sha256"`
	ManifestPath           string                            `json:"manifest_path"`
	ManifestSHA256         string                            `json:"manifest_sha256"`
	SecretScanPath         string                            `json:"secret_scan_path"`
	SecretScanSHA256       string                            `json:"secret_scan_sha256"`
	RouteCount             int                               `json:"route_count"`
	CaptureCount           int                               `json:"capture_count"`
	CaptureKeySetSHA256    string                            `json:"capture_key_set_sha256"`
	AllowedDeltas          []finalwirecontract.ApprovedDelta `json:"allowed_deltas"`
	UnexpectedWireDiffs    int                               `json:"unexpected_wire_diff_count"`
	ComparisonResult       string                            `json:"comparison_result"`
	Rules                  []string                          `json:"rules"`
}

func TestGenerateChangeset6PostFinalWire(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset6PostFinalWireOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定变更集 6 post final-wire 输出目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("变更集 6 post final-wire 输出目录必须是绝对路径")
	}
	baselineRaw, err := os.ReadFile("../../../docs/changeset5/post-refactor-final-wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	if finalwirecapture.SHA256(baselineRaw) != changeset5PostRefactorManifestSHA256 {
		t.Fatal("生成变更集 6 post 前，变更集 5 post final-wire 已漂移")
	}
	var baseline changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, baselineRaw, &baseline)

	captures := changeset3BuildProductionFinalWireCaptures(t)
	if err := changeset5CompareFinalWireCaptures(baseline.Captures, captures, nil); err != nil {
		t.Fatal(err)
	}
	manifest := changeset5PreRefactorFinalWireManifest{
		SchemaVersion: "changeset6-post-final-wire/v1",
		CaptureKind:   "changeset6_completed_current_production_final_wire",
		CaptureBoundary: "production_semantic_bridge_to_production_invocation_to_official_adapter_" +
			"after_transport_normalization_before_deterministic_local_terminal_send",
		Baseline:           "changeset5_post_refactor_final_wire_byte_exact",
		ExternalTraffic:    false,
		CredentialMaterial: "synthetic_attempt_local_only",
		RouteCount:         28,
		ReleaseModes:       baseline.ReleaseModes,
		CaptureCount:       len(captures),
		Captures:           captures,
		Redaction:          "仅保存合成最终 wire 的结构、顺序和脱敏摘要；认证材料不保存值或可关联真实摘要。",
	}
	manifestRaw := changeset3ProductionMarshal(t, manifest)
	scan := changeset5BuildFinalWireSecretScan(
		t, "changeset6-post-final-wire-secret-scan/v1", manifestRaw,
	)
	scanRaw := changeset3ProductionMarshal(t, scan)
	receipt := changeset6PostFinalWireReceipt{
		SchemaVersion:          "changeset6-post-final-wire-receipt/v1",
		Changeset:              "6",
		BaselineManifestPath:   "docs/changeset5/post-refactor-final-wire/manifest.json",
		BaselineManifestSHA256: changeset5PostRefactorManifestSHA256,
		ManifestPath:           "docs/changeset6/post-final-wire/manifest.json",
		ManifestSHA256:         finalwirecapture.SHA256(manifestRaw),
		SecretScanPath:         "docs/changeset6/post-final-wire/secret-scan.json",
		SecretScanSHA256:       finalwirecapture.SHA256(scanRaw),
		RouteCount:             manifest.RouteCount,
		CaptureCount:           manifest.CaptureCount,
		CaptureKeySetSHA256:    changeset5CaptureKeySetSHA256(captures),
		AllowedDeltas:          []finalwirecontract.ApprovedDelta{},
		UnexpectedWireDiffs:    0,
		ComparisonResult:       "passed",
		Rules: []string{
			"变更集 5 post final-wire 三件套只读，禁止重新生成或覆盖",
			"变更集 6 post 与变更集 5 post 使用统一严格比较器逐字段比较",
			"允许列表固定为空，任何最终 wire 差异均失败",
		},
	}
	receiptRaw := changeset3ProductionMarshal(t, receipt)
	if err := os.MkdirAll(output, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, raw := range map[string][]byte{
		"manifest.json": manifestRaw, "secret-scan.json": scanRaw, "receipt.json": receiptRaw,
	} {
		if err := os.WriteFile(filepath.Join(output, name), raw, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestChangeset6PostFinalWireIsFrozenAndMatchesChangeset5(t *testing.T) {
	root := "../../../docs/changeset6/post-final-wire"
	manifestRaw, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	scanRaw, err := os.ReadFile(filepath.Join(root, "secret-scan.json"))
	if err != nil {
		t.Fatal(err)
	}
	receiptRaw, err := os.ReadFile(filepath.Join(root, "receipt.json"))
	if err != nil {
		t.Fatal(err)
	}
	if finalwirecapture.SHA256(manifestRaw) != changeset6PostFinalWireManifestSHA256 ||
		finalwirecapture.SHA256(scanRaw) != changeset6PostFinalWireSecretSHA256 ||
		finalwirecapture.SHA256(receiptRaw) != changeset6PostFinalWireReceiptSHA256 {
		t.Fatal("变更集 6 post final-wire 三件套摘要漂移")
	}
	baselineRaw, err := os.ReadFile("../../../docs/changeset5/post-refactor-final-wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var baseline, post changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, baselineRaw, &baseline)
	changeset5DecodeStrict(t, manifestRaw, &post)
	if err := changeset5CompareFinalWireCaptures(baseline.Captures, post.Captures, nil); err != nil {
		t.Fatal(err)
	}
	var receipt changeset6PostFinalWireReceipt
	changeset5DecodeStrict(t, receiptRaw, &receipt)
	if receipt.SchemaVersion != "changeset6-post-final-wire-receipt/v1" ||
		receipt.BaselineManifestSHA256 != changeset5PostRefactorManifestSHA256 ||
		receipt.RouteCount != 28 || receipt.CaptureCount != 56 ||
		len(receipt.AllowedDeltas) != 0 || receipt.UnexpectedWireDiffs != 0 ||
		receipt.ComparisonResult != "passed" {
		t.Fatalf("变更集 6 post final-wire receipt 非法：%+v", receipt)
	}
	var scan changeset5PreRefactorSecretScan
	changeset5DecodeStrict(t, scanRaw, &scan)
	if scan.SchemaVersion != "changeset6-post-final-wire-secret-scan/v1" ||
		scan.ArtifactSHA256 != changeset6PostFinalWireManifestSHA256 || scan.Result != "passed" {
		t.Fatalf("变更集 6 secret scan 非法：%+v", scan)
	}
}
