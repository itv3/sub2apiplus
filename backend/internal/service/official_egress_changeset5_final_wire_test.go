package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecapture"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
)

const (
	changeset5NormalizedPreFinalWireOutputEnv = "CHANGESET5_NORMALIZED_PRE_FINAL_WIRE_OUTPUT"
	changeset5PostRefactorFinalWireOutputEnv  = "CHANGESET5_POST_REFACTOR_FINAL_WIRE_OUTPUT"
)

// original pre 是变更集 5 开发前不可变时间锚点；normalized pre 只消除两条
// OAuth InvocationID 随机噪声；post 必须与 normalized pre 严格零差异。
const (
	changeset5OriginalPreManifestSHA256     = "959b3179c0a54ffa81fa58057e43994e237f1f8565de0f28788a01f18e2d316d"
	changeset5OriginalPreSecretSHA256       = "6446d2ce06d745dd1240a513b77d261cca89a719ab9e6e34164891fce60e7488"
	changeset5OriginalPreReceiptSHA256      = "3cb84244a4a5b0a1d056bdff5616eea8bcc49c668f6c3039722c4c8e187edabd"
	changeset5NormalizedPreManifestSHA256   = "51501f4c140a417d81c3d1a8f8525be4ef2f77c11248ffa63ca8e50150d011f7"
	changeset5NormalizedPreSecretSHA256     = "281bcfae80abb2b60156a71fa8d3d3fe9667f52196762a0ba15794bf1c82e438"
	changeset5NormalizedPreReceiptSHA256    = "90e6ddfdaa9d200ec0a51523b4ff6cec8d56b6c67fd5ddb2d51d9b10382c8491"
	changeset5NormalizationTransitionSHA256 = "c037cc323431ac3180ab98e9e229d3cc1e6e3a34371b8c793dbf3ff25b7c2445"
	changeset5PostRefactorManifestSHA256    = "3b1dfc541086da317c288bc064016acda9003780f907c3d5ae6c1720a7a78fb5"
	changeset5PostRefactorSecretSHA256      = "ee808ca8738c8d04e5d4c5ad3ae09b4e649c32758c8f982f0b65874416f6ffc2"
	changeset5PostRefactorReceiptSHA256     = "9a9c8d86f4b35a33f5f6615e27e21bec25e7860eae5e533fec9333897554c1de"
)

type changeset5PreRefactorFinalWireManifest struct {
	SchemaVersion      string                       `json:"schema_version"`
	CaptureKind        string                       `json:"capture_kind"`
	CaptureBoundary    string                       `json:"capture_boundary"`
	Baseline           string                       `json:"baseline"`
	ExternalTraffic    bool                         `json:"external_traffic"`
	CredentialMaterial string                       `json:"credential_material"`
	RouteCount         int                          `json:"route_count"`
	ReleaseModes       []officialegress.ReleaseMode `json:"release_modes"`
	CaptureCount       int                          `json:"capture_count"`
	Captures           []finalwirecapture.Capture   `json:"captures"`
	Redaction          string                       `json:"redaction"`
}

type changeset5PreRefactorSecretScan struct {
	SchemaVersion  string         `json:"schema_version"`
	Artifact       string         `json:"artifact"`
	ArtifactSHA256 string         `json:"artifact_sha256"`
	Matches        map[string]int `json:"matches"`
	Result         string         `json:"result"`
}

type changeset5PreRefactorFinalWireReceipt struct {
	SchemaVersion              string   `json:"schema_version"`
	Changeset                  string   `json:"changeset"`
	Baseline                   string   `json:"baseline"`
	ManifestPath               string   `json:"manifest_path"`
	ManifestSHA256             string   `json:"manifest_sha256"`
	SecretScanPath             string   `json:"secret_scan_path"`
	SecretScanSHA256           string   `json:"secret_scan_sha256"`
	RouteCount                 int      `json:"route_count"`
	CaptureCount               int      `json:"capture_count"`
	CaptureKeySetSHA256        string   `json:"capture_key_set_sha256"`
	ClassificationUpstreamBase string   `json:"classification_upstream_base"`
	ObservedRemoteHead         string   `json:"observed_remote_head"`
	Rules                      []string `json:"rules"`
}

type changeset5NormalizedPreFinalWireReceipt struct {
	SchemaVersion               string   `json:"schema_version"`
	Changeset                   string   `json:"changeset"`
	Baseline                    string   `json:"baseline"`
	ManifestPath                string   `json:"manifest_path"`
	ManifestSHA256              string   `json:"manifest_sha256"`
	SecretScanPath              string   `json:"secret_scan_path"`
	SecretScanSHA256            string   `json:"secret_scan_sha256"`
	OriginalPreManifestPath     string   `json:"original_pre_manifest_path"`
	OriginalPreManifestSHA256   string   `json:"original_pre_manifest_sha256"`
	NormalizationTransitionPath string   `json:"normalization_transition_path"`
	RouteCount                  int      `json:"route_count"`
	CaptureCount                int      `json:"capture_count"`
	CaptureKeySetSHA256         string   `json:"capture_key_set_sha256"`
	Rules                       []string `json:"rules"`
}

type changeset5FinalWireNormalizationTransition struct {
	SchemaVersion             string                            `json:"schema_version"`
	Changeset                 string                            `json:"changeset"`
	OriginalPreManifestPath   string                            `json:"original_pre_manifest_path"`
	OriginalPreManifestSHA256 string                            `json:"original_pre_manifest_sha256"`
	NormalizedPreManifestPath string                            `json:"normalized_pre_manifest_path"`
	NormalizedPreSHA256       string                            `json:"normalized_pre_manifest_sha256"`
	ApprovedDeltas            []finalwirecontract.ApprovedDelta `json:"approved_deltas"`
	AppliedDeltaCount         int                               `json:"applied_delta_count"`
	UnexpectedDeltaCount      int                               `json:"unexpected_delta_count"`
	Result                    string                            `json:"result"`
}

type changeset5PostRefactorFinalWireReceipt struct {
	SchemaVersion               string   `json:"schema_version"`
	Changeset                   string   `json:"changeset"`
	Baseline                    string   `json:"baseline"`
	ManifestPath                string   `json:"manifest_path"`
	ManifestSHA256              string   `json:"manifest_sha256"`
	SecretScanPath              string   `json:"secret_scan_path"`
	SecretScanSHA256            string   `json:"secret_scan_sha256"`
	NormalizedPreManifestPath   string   `json:"normalized_pre_manifest_path"`
	NormalizedPreManifestSHA256 string   `json:"normalized_pre_manifest_sha256"`
	NormalizationTransitionPath string   `json:"normalization_transition_path"`
	NormalizationTransitionSHA  string   `json:"normalization_transition_sha256"`
	RouteCount                  int      `json:"route_count"`
	CaptureCount                int      `json:"capture_count"`
	CaptureKeySetSHA256         string   `json:"capture_key_set_sha256"`
	UnexpectedWireDiffCount     int      `json:"unexpected_wire_diff_count"`
	ComparisonResult            string   `json:"comparison_result"`
	ClassificationUpstreamBase  string   `json:"classification_upstream_base"`
	ObservedRemoteHead          string   `json:"observed_remote_head"`
	Rules                       []string `json:"rules"`
}

func TestGenerateChangeset5NormalizedPreFinalWire(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset5NormalizedPreFinalWireOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定变更集 5 normalized pre final-wire 输出目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("变更集 5 normalized pre final-wire 输出目录必须是绝对路径")
	}
	captures := changeset3BuildProductionFinalWireCaptures(t)
	manifest := changeset5PreRefactorFinalWireManifest{
		SchemaVersion: "changeset5-pre-refactor-final-wire/v1",
		CaptureKind:   "changeset4_accepted_current_production_final_wire",
		CaptureBoundary: "production_semantic_bridge_to_production_invocation_to_official_adapter_" +
			"after_transport_normalization_before_deterministic_local_terminal_send",
		Baseline:        "changeset4_accepted_workspace_before_changeset5_refactor",
		ExternalTraffic: false, CredentialMaterial: "synthetic_attempt_local_only",
		RouteCount: 28,
		ReleaseModes: []officialegress.ReleaseMode{
			officialegress.ReleaseModeActive, officialegress.ReleaseModePrevious,
		},
		CaptureCount: len(captures), Captures: captures,
		Redaction: "仅保存合成最终 wire 的结构、顺序和脱敏摘要；认证材料不保存值或可关联真实摘要。",
	}
	manifestRaw := changeset3ProductionMarshal(t, manifest)
	scan := changeset5BuildFinalWireSecretScan(
		t, "changeset5-normalized-pre-refactor-secret-scan/v1", manifestRaw,
	)
	scanRaw := changeset3ProductionMarshal(t, scan)
	receipt := changeset5NormalizedPreFinalWireReceipt{
		SchemaVersion: "changeset5-normalized-pre-refactor-final-wire-receipt/v1",
		Changeset:     "5", Baseline: manifest.Baseline,
		ManifestPath:                "docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json",
		ManifestSHA256:              finalwirecapture.SHA256(manifestRaw),
		SecretScanPath:              "docs/egress/consolidation/normalized-pre-refactor-final-wire/secret-scan.json",
		SecretScanSHA256:            finalwirecapture.SHA256(scanRaw),
		OriginalPreManifestPath:     "docs/egress/consolidation/pre-refactor-final-wire/manifest.json",
		OriginalPreManifestSHA256:   changeset5OriginalPreManifestSHA256,
		NormalizationTransitionPath: "docs/egress/consolidation/final-wire-normalization-transition.json",
		RouteCount:                  manifest.RouteCount, CaptureCount: manifest.CaptureCount,
		CaptureKeySetSHA256: changeset5CaptureKeySetSHA256(captures),
		Rules: []string{
			"original pre 保持字节不变，normalized pre 只能通过结构化 normalization transition 承接",
			"只允许两条 OAuth connection_pool_digest 随机噪声归一化",
			"post 与 normalized pre 必须使用空允许列表逐 capture 零差异比较",
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

func TestGenerateChangeset5PostRefactorFinalWire(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset5PostRefactorFinalWireOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定变更集 5 完成后 final-wire 输出目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("变更集 5 完成后 final-wire 输出目录必须是绝对路径")
	}
	normalizedRaw, err := os.ReadFile("../../../docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	if finalwirecapture.SHA256(normalizedRaw) != changeset5NormalizedPreManifestSHA256 {
		t.Fatal("生成完成后证据前，变更集 5 normalized pre final-wire 摘要已经漂移")
	}
	var normalized changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, normalizedRaw, &normalized)

	captures := changeset3BuildProductionFinalWireCaptures(t)
	// 写出任何完成后证据之前，先由统一严格比较器证明 56 份 capture 零差异。
	if err := changeset5CompareFinalWireCaptures(normalized.Captures, captures, nil); err != nil {
		t.Fatal(err)
	}
	manifest := changeset5PreRefactorFinalWireManifest{
		SchemaVersion: "changeset5-post-refactor-final-wire/v1",
		CaptureKind:   "changeset5_completed_current_production_final_wire",
		CaptureBoundary: "production_semantic_bridge_to_production_invocation_to_official_adapter_" +
			"after_transport_normalization_before_deterministic_local_terminal_send",
		Baseline:        "changeset5_completed_workspace",
		ExternalTraffic: false, CredentialMaterial: "synthetic_attempt_local_only",
		RouteCount: 28,
		ReleaseModes: []officialegress.ReleaseMode{
			officialegress.ReleaseModeActive, officialegress.ReleaseModePrevious,
		},
		CaptureCount: len(captures), Captures: captures,
		Redaction: "仅保存合成最终 wire 的结构、顺序和脱敏摘要；认证材料不保存值或可关联真实摘要。",
	}
	manifestRaw := changeset3ProductionMarshal(t, manifest)
	scan := changeset5BuildFinalWireSecretScan(
		t, "changeset5-post-refactor-secret-scan/v1", manifestRaw,
	)
	scanRaw := changeset3ProductionMarshal(t, scan)
	receipt := changeset5PostRefactorFinalWireReceipt{
		SchemaVersion: "changeset5-post-refactor-final-wire-receipt/v1",
		Changeset:     "5", Baseline: manifest.Baseline,
		ManifestPath:                "docs/egress/consolidation/post-refactor-final-wire/manifest.json",
		ManifestSHA256:              finalwirecapture.SHA256(manifestRaw),
		SecretScanPath:              "docs/egress/consolidation/post-refactor-final-wire/secret-scan.json",
		SecretScanSHA256:            finalwirecapture.SHA256(scanRaw),
		NormalizedPreManifestPath:   "docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json",
		NormalizedPreManifestSHA256: changeset5NormalizedPreManifestSHA256,
		NormalizationTransitionPath: "docs/egress/consolidation/final-wire-normalization-transition.json",
		NormalizationTransitionSHA:  changeset5NormalizationTransitionSHA256,
		RouteCount:                  manifest.RouteCount, CaptureCount: manifest.CaptureCount,
		CaptureKeySetSHA256:     changeset5CaptureKeySetSHA256(captures),
		UnexpectedWireDiffCount: 0, ComparisonResult: "passed",
		ClassificationUpstreamBase: "26d894ef4f50645a4bf1030e378ac892f17d0223",
		ObservedRemoteHead:         "825ca7b1fc9335f904bc077f051de815fb61e47f",
		Rules: []string{
			"远端观察值不参与本变更集 diff、分类或验收",
			"OAuth refresh 采集由测试边界固定 InvocationID，排除连接生命周期摘要的随机噪声",
			"normalized pre/post 使用统一严格比较器逐字段比较，允许列表为空",
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

func TestChangeset5OriginalPreFinalWireIsByteExactAndFrozen(t *testing.T) {
	root := "../../../docs/egress/consolidation/pre-refactor-final-wire"
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
	if finalwirecapture.SHA256(manifestRaw) != changeset5OriginalPreManifestSHA256 ||
		finalwirecapture.SHA256(scanRaw) != changeset5OriginalPreSecretSHA256 ||
		finalwirecapture.SHA256(receiptRaw) != changeset5OriginalPreReceiptSHA256 {
		t.Fatal("变更集 5 original pre final-wire 的 manifest、secret-scan 或 receipt 摘要漂移")
	}
	var manifest changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, manifestRaw, &manifest)
	if manifest.SchemaVersion != "changeset5-pre-refactor-final-wire/v1" ||
		manifest.ExternalTraffic || manifest.RouteCount != 28 || manifest.CaptureCount != 56 ||
		len(manifest.Captures) != 56 {
		t.Fatalf("变更集 5 前置 final-wire 顶层事实非法：%+v", manifest)
	}
	var scan changeset5PreRefactorSecretScan
	changeset5DecodeStrict(t, scanRaw, &scan)
	if scan.SchemaVersion != "changeset5-pre-refactor-secret-scan/v1" ||
		scan.ArtifactSHA256 != changeset5OriginalPreManifestSHA256 || scan.Result != "passed" ||
		scan.Matches["Bearer "] != 0 || scan.Matches["synthetic-access-token"] != 0 ||
		scan.Matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("变更集 5 前置 final-wire secret scan 非法：%+v", scan)
	}
	var receipt changeset5PreRefactorFinalWireReceipt
	changeset5DecodeStrict(t, receiptRaw, &receipt)
	if receipt.SchemaVersion != "changeset5-pre-refactor-final-wire-receipt/v1" ||
		receipt.ManifestSHA256 != changeset5OriginalPreManifestSHA256 ||
		receipt.SecretScanSHA256 != changeset5OriginalPreSecretSHA256 ||
		receipt.RouteCount != 28 || receipt.CaptureCount != 56 ||
		receipt.CaptureKeySetSHA256 != changeset5CaptureKeySetSHA256(manifest.Captures) {
		t.Fatalf("变更集 5 前置 final-wire receipt 非法：%+v", receipt)
	}
}

func TestChangeset5NormalizedPreAppliesOnlyExactOAuthNoiseTransition(t *testing.T) {
	originalRaw, err := os.ReadFile("../../../docs/egress/consolidation/pre-refactor-final-wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	root := "../../../docs/egress/consolidation/normalized-pre-refactor-final-wire"
	normalizedRaw, err := os.ReadFile(filepath.Join(root, "manifest.json"))
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
	transitionRaw, err := os.ReadFile("../../../docs/egress/consolidation/final-wire-normalization-transition.json")
	if err != nil {
		t.Fatal(err)
	}
	if finalwirecapture.SHA256(originalRaw) != changeset5OriginalPreManifestSHA256 ||
		finalwirecapture.SHA256(normalizedRaw) != changeset5NormalizedPreManifestSHA256 ||
		finalwirecapture.SHA256(scanRaw) != changeset5NormalizedPreSecretSHA256 ||
		finalwirecapture.SHA256(receiptRaw) != changeset5NormalizedPreReceiptSHA256 ||
		finalwirecapture.SHA256(transitionRaw) != changeset5NormalizationTransitionSHA256 {
		t.Fatal("变更集 5 original/normalized pre 或 normalization transition 摘要漂移")
	}
	var original, normalized changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, originalRaw, &original)
	changeset5DecodeStrict(t, normalizedRaw, &normalized)
	var scan changeset5PreRefactorSecretScan
	changeset5DecodeStrict(t, scanRaw, &scan)
	if scan.SchemaVersion != "changeset5-normalized-pre-refactor-secret-scan/v1" ||
		scan.ArtifactSHA256 != changeset5NormalizedPreManifestSHA256 || scan.Result != "passed" {
		t.Fatalf("变更集 5 normalized pre secret scan 非法：%+v", scan)
	}
	var receipt changeset5NormalizedPreFinalWireReceipt
	changeset5DecodeStrict(t, receiptRaw, &receipt)
	if receipt.SchemaVersion != "changeset5-normalized-pre-refactor-final-wire-receipt/v1" ||
		receipt.ManifestSHA256 != changeset5NormalizedPreManifestSHA256 ||
		receipt.SecretScanSHA256 != changeset5NormalizedPreSecretSHA256 ||
		receipt.OriginalPreManifestSHA256 != changeset5OriginalPreManifestSHA256 ||
		receipt.RouteCount != 28 || receipt.CaptureCount != 56 ||
		receipt.CaptureKeySetSHA256 != changeset5CaptureKeySetSHA256(normalized.Captures) {
		t.Fatalf("变更集 5 normalized pre receipt 非法：%+v", receipt)
	}
	var transition changeset5FinalWireNormalizationTransition
	changeset5DecodeStrict(t, transitionRaw, &transition)
	expected := changeset5NormalizationApprovedDeltas()
	if transition.SchemaVersion != "changeset5-final-wire-normalization-transition/v1" ||
		transition.Changeset != "5" || transition.OriginalPreManifestSHA256 != changeset5OriginalPreManifestSHA256 ||
		transition.NormalizedPreSHA256 != changeset5NormalizedPreManifestSHA256 ||
		transition.AppliedDeltaCount != 2 || transition.UnexpectedDeltaCount != 0 ||
		transition.Result != "passed" || !changeset5ApprovedDeltasEqual(transition.ApprovedDeltas, expected) {
		t.Fatalf("变更集 5 final-wire normalization transition 非法：%+v", transition)
	}
	if err := changeset5CompareFinalWireCaptures(original.Captures, normalized.Captures, expected); err != nil {
		t.Fatal(err)
	}
}

func TestChangeset5HistoricalFinalWireRemainsFrozenAfterRuntimeRetirement(t *testing.T) {
	normalized := changeset5ReadFinalWireManifest(
		t, "../../../docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json",
	)
	if len(normalized.Captures) == 0 {
		t.Fatal("变更集 5 冻结 final-wire 为空")
	}
	for _, mode := range []officialegress.ReleaseMode{
		officialegress.ReleaseModeActive,
		officialegress.ReleaseModePrevious,
	} {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		if release.Version() == officialCodexVersion0145 {
			t.Fatalf("0.145 不得继续占用生产 selector：mode=%s", mode)
		}
	}
	historical, err := officialegress.DefaultReleaseCatalog().ResolveSnapshotExact(
		officialCodexVersion0145,
		officialCodexHistoricalProfileDigest,
	)
	if err != nil {
		t.Fatal(err)
	}
	if historical.Version() != officialCodexVersion0145 ||
		historical.ProfileDigest() != officialCodexHistoricalProfileDigest {
		t.Fatal("0.145 只读历史画像坐标漂移")
	}
}

func TestChangeset5CurrentFinalWireComparatorRejectsWireDrift(t *testing.T) {
	normalized := changeset5ReadFinalWireManifest(
		t, "../../../docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json",
	)
	mutated := append([]finalwirecapture.Capture(nil), normalized.Captures...)
	mutated[0].Body.FinalWireBytes++
	if err := changeset5CompareCurrentFinalWireCaptures(normalized.Captures, mutated); err == nil {
		t.Fatal("跨平台 current final-wire 比较器未拒绝真实 wire 字段漂移")
	}
}

func TestChangeset5NormalizationTransitionRejectsWrongOrExpandedApproval(t *testing.T) {
	original := changeset5ReadFinalWireManifest(t, "../../../docs/egress/consolidation/pre-refactor-final-wire/manifest.json")
	normalized := changeset5ReadFinalWireManifest(t, "../../../docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json")
	valid := changeset5NormalizationApprovedDeltas()
	mutations := map[string][]finalwirecontract.ApprovedDelta{
		"错误 capture": append([]finalwirecontract.ApprovedDelta(nil), valid...),
		"错误路径":       append([]finalwirecontract.ApprovedDelta(nil), valid...),
		"错误 before":  append([]finalwirecontract.ApprovedDelta(nil), valid...),
		"错误 after":   append([]finalwirecontract.ApprovedDelta(nil), valid...),
		"缺少一条":       append([]finalwirecontract.ApprovedDelta(nil), valid[:1]...),
		"增加第三条":      append(append([]finalwirecontract.ApprovedDelta(nil), valid...), valid[0]),
	}
	mutations["错误 capture"][0].CaptureKey = "active\x00wrong.capture\x00POST\x00auth.openai.com\x00/oauth/token\x00http"
	mutations["错误路径"][0].Path = "/connection_identity_digest"
	mutations["错误 before"][0].BeforeSHA256 = strings.Repeat("0", 64)
	mutations["错误 after"][0].AfterSHA256 = strings.Repeat("f", 64)
	for name, approved := range mutations {
		t.Run(name, func(t *testing.T) {
			if err := changeset5CompareFinalWireCaptures(original.Captures, normalized.Captures, approved); err == nil {
				t.Fatal("错误或扩大的 normalization approval 未被拒绝")
			}
		})
	}
}

func TestChangeset5PostRefactorFinalWireIsFrozenAndMatchesPre(t *testing.T) {
	normalizedRaw, err := os.ReadFile("../../../docs/egress/consolidation/normalized-pre-refactor-final-wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	root := "../../../docs/egress/consolidation/post-refactor-final-wire"
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
	if finalwirecapture.SHA256(normalizedRaw) != changeset5NormalizedPreManifestSHA256 ||
		finalwirecapture.SHA256(manifestRaw) != changeset5PostRefactorManifestSHA256 ||
		finalwirecapture.SHA256(scanRaw) != changeset5PostRefactorSecretSHA256 ||
		finalwirecapture.SHA256(receiptRaw) != changeset5PostRefactorReceiptSHA256 {
		t.Fatal("变更集 5 pre/post final-wire 冻结摘要漂移")
	}
	var normalized, post changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, normalizedRaw, &normalized)
	changeset5DecodeStrict(t, manifestRaw, &post)
	if post.SchemaVersion != "changeset5-post-refactor-final-wire/v1" ||
		post.ExternalTraffic || post.RouteCount != 28 || post.CaptureCount != 56 ||
		len(post.Captures) != 56 {
		t.Fatalf("变更集 5 完成后 final-wire 顶层事实非法：%+v", post)
	}
	var scan changeset5PreRefactorSecretScan
	changeset5DecodeStrict(t, scanRaw, &scan)
	if scan.SchemaVersion != "changeset5-post-refactor-secret-scan/v1" ||
		scan.ArtifactSHA256 != changeset5PostRefactorManifestSHA256 || scan.Result != "passed" ||
		scan.Matches["Bearer "] != 0 || scan.Matches["synthetic-access-token"] != 0 ||
		scan.Matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("变更集 5 完成后 final-wire secret scan 非法：%+v", scan)
	}
	var receipt changeset5PostRefactorFinalWireReceipt
	changeset5DecodeStrict(t, receiptRaw, &receipt)
	if receipt.SchemaVersion != "changeset5-post-refactor-final-wire-receipt/v1" ||
		receipt.ManifestSHA256 != changeset5PostRefactorManifestSHA256 ||
		receipt.SecretScanSHA256 != changeset5PostRefactorSecretSHA256 ||
		receipt.NormalizedPreManifestSHA256 != changeset5NormalizedPreManifestSHA256 ||
		receipt.NormalizationTransitionSHA != changeset5NormalizationTransitionSHA256 ||
		receipt.RouteCount != 28 || receipt.CaptureCount != 56 ||
		receipt.UnexpectedWireDiffCount != 0 || receipt.ComparisonResult != "passed" ||
		receipt.CaptureKeySetSHA256 != changeset5CaptureKeySetSHA256(post.Captures) {
		t.Fatalf("变更集 5 完成后 final-wire receipt 非法：%+v", receipt)
	}
	if err := changeset5CompareFinalWireCaptures(normalized.Captures, post.Captures, nil); err != nil {
		t.Fatal(err)
	}
}

func changeset5NormalizationApprovedDeltas() []finalwirecontract.ApprovedDelta {
	const reason = "OAuth 生产入口原先随机生成 InvocationID；测试采集边界固定 InvocationID，仅消除连接池生命周期摘要噪声"
	return []finalwirecontract.ApprovedDelta{
		{
			CaptureKey:   "active\x00codex.oauth.refresh\x00POST\x00auth.openai.com\x00/oauth/token\x00http",
			Path:         "/connection_pool_digest",
			BeforeSHA256: "d2fa19a77a08f911adc49e004770bcf0151e52ea6b6e5db9e1077def934056d0",
			AfterSHA256:  "b1c81c8f4f9439ae294061b9b8b92604c8c2773ac728b176b828e3bda8e2bbca",
			Reason:       reason,
		},
		{
			CaptureKey:   "previous\x00codex.oauth.refresh\x00POST\x00auth.openai.com\x00/oauth/token\x00http",
			Path:         "/connection_pool_digest",
			BeforeSHA256: "d5567cd034eb25f151a5e00025a94b54d210a761f39472a11d681f27fa8af858",
			AfterSHA256:  "fb412c32660c858109360ef5f7c043792c9287c450a43d385e179a70701d2ff3",
			Reason:       reason,
		},
	}
}

func changeset5ApprovedDeltasEqual(
	left []finalwirecontract.ApprovedDelta,
	right []finalwirecontract.ApprovedDelta,
) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func changeset5ReadFinalWireManifest(
	t *testing.T,
	path string,
) changeset5PreRefactorFinalWireManifest {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var manifest changeset5PreRefactorFinalWireManifest
	changeset5DecodeStrict(t, raw, &manifest)
	return manifest
}

func changeset5CompareFinalWireCaptures(
	before []finalwirecapture.Capture,
	after []finalwirecapture.Capture,
	approved []finalwirecontract.ApprovedDelta,
) error {
	if len(before) == 0 || len(before) != len(after) {
		return fmt.Errorf("变更集 5 final-wire capture 数量漂移：before=%d after=%d", len(before), len(after))
	}
	beforeByKey := make(map[string]finalwirecapture.Capture, len(before))
	for _, capture := range before {
		key := changeset3ProductionCaptureKey(capture)
		if _, exists := beforeByKey[key]; exists {
			return fmt.Errorf("变更集 5 前置 final-wire capture key 重复：%q", key)
		}
		beforeByKey[key] = capture
	}
	appliedCount := 0
	for _, capture := range after {
		key := changeset3ProductionCaptureKey(capture)
		frozen, ok := beforeByKey[key]
		if !ok {
			return fmt.Errorf("变更集 5 新增 final-wire capture：%q", key)
		}
		result, err := finalwirecontract.Compare(key, frozen, capture, approved)
		if err != nil {
			return err
		}
		if !result.OK() {
			return fmt.Errorf("变更集 5 final-wire 漂移：key=%q result=%+v", key, result)
		}
		appliedCount += len(result.Applied)
		delete(beforeByKey, key)
	}
	if len(beforeByKey) != 0 {
		return fmt.Errorf("变更集 5 删除 final-wire capture：%v", beforeByKey)
	}
	if appliedCount != len(approved) {
		return fmt.Errorf("normalization approval 未全部精确应用：applied=%d approved=%d", appliedCount, len(approved))
	}
	return nil
}

// changeset5CompareCurrentFinalWireCaptures 只归一 Bundle 中的运行主机平台派生摘要。
// 冻结证据生成于 darwin/arm64，而 CI 和生产目标为 linux/amd64；这三个摘要会把
// DeploymentSupportPolicy.Platform 纳入哈希，但不代表线上字节。Header、Body、TLS、
// WebSocket、端点和其他身份字段仍由统一比较器逐字段严格比较。
func changeset5CompareCurrentFinalWireCaptures(
	before []finalwirecapture.Capture,
	after []finalwirecapture.Capture,
) error {
	beforeByKey := make(map[string]finalwirecapture.Capture, len(before))
	for _, capture := range before {
		beforeByKey[changeset3ProductionCaptureKey(capture)] = capture
	}
	normalized := append([]finalwirecapture.Capture(nil), after...)
	for index := range normalized {
		frozen, ok := beforeByKey[changeset3ProductionCaptureKey(normalized[index])]
		if !ok {
			continue
		}
		normalized[index].BundleDigest = frozen.BundleDigest
		normalized[index].ReleaseDigest = frozen.ReleaseDigest
		normalized[index].ConnectionIdentityDigest = frozen.ConnectionIdentityDigest
		normalized[index].ConnectionPoolDigest = frozen.ConnectionPoolDigest
	}
	return changeset5CompareFinalWireCaptures(before, normalized, nil)
}

func changeset5BuildFinalWireSecretScan(
	t *testing.T,
	schemaVersion string,
	manifestRaw []byte,
) changeset5PreRefactorSecretScan {
	t.Helper()
	patterns := []string{
		"Bearer ", "synthetic-access-token", "synthetic-refresh-token",
		"access_token\"", "refresh_token\"",
	}
	matches := make(map[string]int, len(patterns))
	for _, pattern := range patterns {
		matches[pattern] = bytes.Count(manifestRaw, []byte(pattern))
	}
	if matches["Bearer "] != 0 || matches["synthetic-access-token"] != 0 ||
		matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("变更集 5 前置 final-wire 泄漏合成认证值：%v", matches)
	}
	return changeset5PreRefactorSecretScan{
		SchemaVersion: schemaVersion,
		Artifact:      "manifest.json", ArtifactSHA256: finalwirecapture.SHA256(manifestRaw),
		Matches: matches, Result: "passed",
	}
}

func changeset5CaptureKeySetSHA256(captures []finalwirecapture.Capture) string {
	keys := make([]string, 0, len(captures))
	for _, capture := range captures {
		keys = append(keys, changeset3ProductionCaptureKey(capture))
	}
	sort.Strings(keys)
	sum := sha256.Sum256([]byte(strings.Join(keys, "\n") + "\n"))
	return hex.EncodeToString(sum[:])
}

func changeset5DecodeStrict(t *testing.T, raw []byte, target any) {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		t.Fatal(err)
	}
}
