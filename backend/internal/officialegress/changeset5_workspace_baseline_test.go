package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	changeset5WorkspaceManifestSHA256           = "c0d23f184627ff2d1b2aa7705db63d9a7f11c5bcc31adbe64cc9a72a11a50162"
	changeset5WorkspaceReceiptSHA256            = "97bea92fe429fcad67fa3cb3bca086f9e7f5aa03478cf8b4e333ce1f3700a33e"
	changeset5WorkspaceTransitionManifestSHA256 = "5e144f2f88b62c27647ab0557a7020a19fb1a54dedb0ef598e13f6c387716698"
	changeset5WorkspaceTransitionReceiptSHA256  = "e38384f401dcd2cb93f4e900926cb84b606f29685237c8394edb718ffaa87af1"
	changeset5WorkspaceTransitionEntryCount     = 50
)

type changeset5WorkspaceBaselineEntry struct {
	Path                 string `json:"path"`
	FileType             string `json:"file_type"`
	Mode                 string `json:"mode"`
	Size                 int64  `json:"size"`
	SHA256               string `json:"sha256"`
	Changeset4BaseSHA256 string `json:"changeset4_base_sha256,omitempty"`
}

type changeset5WorkspaceBaselineManifest struct {
	SchemaVersion              string                             `json:"schema_version"`
	Changeset                  string                             `json:"changeset"`
	ClassificationUpstreamBase string                             `json:"classification_upstream_base"`
	ObservedRemoteHead         string                             `json:"observed_remote_head"`
	Changeset4BaseManifest     string                             `json:"changeset4_base_manifest"`
	Changeset4BaseSHA256       string                             `json:"changeset4_base_manifest_sha256"`
	Protected                  []changeset5WorkspaceBaselineEntry `json:"protected_prior_artifacts"`
	Incidental                 []changeset5WorkspaceBaselineEntry `json:"incidental_non_authoritative_paths"`
	Prerequisites              []changeset5WorkspaceBaselineEntry `json:"changeset5_prerequisite_artifacts"`
	Rules                      []string                           `json:"rules"`
}

type changeset5WorkspaceBaselineReceipt struct {
	SchemaVersion     string `json:"schema_version"`
	Changeset         string `json:"changeset"`
	ManifestPath      string `json:"manifest_path"`
	ManifestSHA256    string `json:"manifest_sha256"`
	ProtectedCount    int    `json:"protected_prior_count"`
	IncidentalCount   int    `json:"incidental_non_authoritative_count"`
	PrerequisiteCount int    `json:"changeset5_prerequisite_count"`
	FileTypePolicy    string `json:"file_type_policy"`
}

type changeset5WorkspaceTransitionState struct {
	FileType string `json:"file_type"`
	Mode     string `json:"mode"`
	Size     int64  `json:"size"`
	SHA256   string `json:"sha256"`
}

type changeset5WorkspaceTransitionEntry struct {
	Path            string                             `json:"path"`
	Classification  string                             `json:"classification"`
	Before          changeset5WorkspaceTransitionState `json:"before"`
	After           changeset5WorkspaceTransitionState `json:"after"`
	Reason          string                             `json:"reason"`
	MigrationUnits  []string                           `json:"migration_units"`
	DeletionAllowed bool                               `json:"deletion_allowed"`
	MachineProofs   []string                           `json:"machine_proofs"`
}

type changeset5WorkspaceTransitionManifest struct {
	SchemaVersion        string                               `json:"schema_version"`
	Changeset            string                               `json:"changeset"`
	BaselineManifestPath string                               `json:"baseline_manifest_path"`
	BaselineManifestSHA  string                               `json:"baseline_manifest_sha256"`
	BaselineReceiptPath  string                               `json:"baseline_receipt_path"`
	BaselineReceiptSHA   string                               `json:"baseline_receipt_sha256"`
	FrozenPathCount      int                                  `json:"frozen_path_count"`
	EntryCount           int                                  `json:"entry_count"`
	Entries              []changeset5WorkspaceTransitionEntry `json:"entries"`
	Rules                []string                             `json:"rules"`
}

type changeset5WorkspaceTransitionReceipt struct {
	SchemaVersion        string         `json:"schema_version"`
	Changeset            string         `json:"changeset"`
	ManifestPath         string         `json:"manifest_path"`
	ManifestSHA256       string         `json:"manifest_sha256"`
	FrozenPathCount      int            `json:"frozen_path_count"`
	TransitionEntryCount int            `json:"transition_entry_count"`
	TransitionCounts     map[string]int `json:"transition_counts"`
	UnchangedCounts      map[string]int `json:"unchanged_counts"`
	DeletedEntryCount    int            `json:"deleted_entry_count"`
	Result               string         `json:"result"`
}

func TestChangeset5WorkspaceBaselineIsIndependentlyFrozen(t *testing.T) {
	root := filepath.Join("../../..", "docs", "egress", "consolidation", "workspace-baseline")
	manifestRaw, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	receiptRaw, err := os.ReadFile(filepath.Join(root, "receipt.json"))
	if err != nil {
		t.Fatal(err)
	}
	if changeset5WorkspaceSHA256(manifestRaw) != changeset5WorkspaceManifestSHA256 ||
		changeset5WorkspaceSHA256(receiptRaw) != changeset5WorkspaceReceiptSHA256 {
		t.Fatal("变更集 5 工作区基线 manifest 或 receipt 摘要漂移")
	}
	var manifest changeset5WorkspaceBaselineManifest
	changeset5WorkspaceDecodeStrict(t, manifestRaw, &manifest)
	var receipt changeset5WorkspaceBaselineReceipt
	changeset5WorkspaceDecodeStrict(t, receiptRaw, &receipt)
	if manifest.SchemaVersion != "changeset5-workspace-baseline/v1" || manifest.Changeset != "5" ||
		manifest.ClassificationUpstreamBase != "26d894ef4f50645a4bf1030e378ac892f17d0223" ||
		manifest.ObservedRemoteHead != "825ca7b1fc9335f904bc077f051de815fb61e47f" ||
		len(manifest.Protected) != 546 || len(manifest.Incidental) != 2 || len(manifest.Prerequisites) != 10 {
		t.Fatalf("变更集 5 工作区基线顶层事实漂移：%+v", manifest)
	}
	if receipt.SchemaVersion != "changeset5-workspace-baseline-receipt/v1" ||
		receipt.ManifestSHA256 != changeset5WorkspaceManifestSHA256 ||
		receipt.ProtectedCount != len(manifest.Protected) ||
		receipt.IncidentalCount != len(manifest.Incidental) ||
		receipt.PrerequisiteCount != len(manifest.Prerequisites) ||
		receipt.FileTypePolicy != "regular_or_explicit_absent; symlink_forbidden" {
		t.Fatalf("变更集 5 工作区基线 receipt 非法：%+v", receipt)
	}
	seen := make(map[string]bool, len(manifest.Protected)+len(manifest.Incidental)+len(manifest.Prerequisites))
	for _, group := range [][]changeset5WorkspaceBaselineEntry{
		manifest.Protected, manifest.Incidental, manifest.Prerequisites,
	} {
		for _, entry := range group {
			if seen[entry.Path] || (entry.FileType != "regular" && entry.FileType != "absent") {
				t.Fatalf("变更集 5 工作区基线路径重复或类型非法：%+v", entry)
			}
			seen[entry.Path] = true
		}
	}
	incidental := map[string]string{}
	for _, entry := range manifest.Incidental {
		incidental[entry.Path] = entry.SHA256
	}
	if incidental[".vite/vitest/results.json"] != "54fcdf5208ec1feaf30e28625df5f54b9e730336abfe90b760517f0c45dbada2" ||
		incidental["backend/-h"] != "6b1b69ba293fb692fe506822a17325d7d2e5735fa3095e47da7ae78d00c9843d" {
		t.Fatalf("临时产物未被精确隔离：%v", incidental)
	}
}

func TestChangeset5WorkspaceTransitionIsIndependentlyFrozen(t *testing.T) {
	root := filepath.Join("../../..", "docs", "egress", "consolidation", "workspace-transition")
	manifestRaw, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	receiptRaw, err := os.ReadFile(filepath.Join(root, "receipt.json"))
	if err != nil {
		t.Fatal(err)
	}
	if changeset5WorkspaceSHA256(manifestRaw) != changeset5WorkspaceTransitionManifestSHA256 ||
		changeset5WorkspaceSHA256(receiptRaw) != changeset5WorkspaceTransitionReceiptSHA256 {
		t.Fatal("变更集 5 workspace transition manifest 或 receipt 摘要漂移")
	}
	var manifest changeset5WorkspaceTransitionManifest
	changeset5WorkspaceDecodeStrict(t, manifestRaw, &manifest)
	var receipt changeset5WorkspaceTransitionReceipt
	changeset5WorkspaceDecodeStrict(t, receiptRaw, &receipt)
	if manifest.SchemaVersion != "changeset5-workspace-transition/v1" || manifest.Changeset != "5" ||
		manifest.BaselineManifestSHA != changeset5WorkspaceManifestSHA256 ||
		manifest.BaselineReceiptSHA != changeset5WorkspaceReceiptSHA256 ||
		manifest.FrozenPathCount != 558 || manifest.EntryCount != changeset5WorkspaceTransitionEntryCount ||
		len(manifest.Entries) != changeset5WorkspaceTransitionEntryCount {
		t.Fatalf("workspace transition 顶层事实漂移：%+v", manifest)
	}
	if receipt.SchemaVersion != "changeset5-workspace-transition-receipt/v1" ||
		receipt.Changeset != "5" || receipt.ManifestSHA256 != changeset5WorkspaceTransitionManifestSHA256 ||
		receipt.FrozenPathCount != 558 || receipt.TransitionEntryCount != changeset5WorkspaceTransitionEntryCount ||
		receipt.TransitionCounts["incidental_non_authoritative_paths"] != 0 ||
		receipt.UnchangedCounts["incidental_non_authoritative_paths"] != 2 ||
		receipt.DeletedEntryCount != 2 || receipt.Result != "passed" {
		t.Fatalf("workspace transition receipt 非法：%+v", receipt)
	}
	seen := make(map[string]bool, len(manifest.Entries))
	for _, item := range manifest.Entries {
		if seen[item.Path] || item.Path == ".vite/vitest/results.json" || item.Path == "backend/-h" ||
			item.Before == item.After || item.Reason == "" || len(item.MigrationUnits) == 0 ||
			len(item.MachineProofs) == 0 {
			t.Fatalf("workspace transition 出现重复、虚假或无证明条目：%+v", item)
		}
		seen[item.Path] = true
		deleted := item.Before.FileType == "regular" && item.After.FileType == "absent"
		if deleted != item.DeletionAllowed ||
			(item.DeletionAllowed && (item.Before.FileType != "regular" || item.After.FileType != "absent")) {
			t.Fatalf("workspace transition 删除三条件非法：%+v", item)
		}
	}
}

func TestChangeset5WorkspaceTransitionDeletionMutationFails(t *testing.T) {
	valid := changeset5WorkspaceTransitionEntry{
		Path: "deleted.go", Classification: "protected_prior_artifacts",
		Before: changeset5WorkspaceTransitionState{FileType: "regular", Mode: "0644", Size: 3, SHA256: strings.Repeat("a", 64)},
		After:  changeset5WorkspaceTransitionState{FileType: "absent"},
		Reason: "测试删除", MigrationUnits: []string{"changeset5:test"},
		DeletionAllowed: true, MachineProofs: []string{"mutation"},
	}
	mutations := []changeset5WorkspaceTransitionEntry{
		func() changeset5WorkspaceTransitionEntry { item := valid; item.DeletionAllowed = false; return item }(),
		func() changeset5WorkspaceTransitionEntry { item := valid; item.Before.FileType = "absent"; return item }(),
		func() changeset5WorkspaceTransitionEntry { item := valid; item.After.FileType = "regular"; return item }(),
	}
	for _, item := range mutations {
		deleted := item.Before.FileType == "regular" && item.After.FileType == "absent"
		if deleted == item.DeletionAllowed {
			t.Fatalf("workspace transition 删除 mutation 未被拒绝：%+v", item)
		}
	}
}

func changeset5WorkspaceDecodeStrict(t *testing.T, raw []byte, target any) {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		t.Fatal(err)
	}
}

func changeset5WorkspaceSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
