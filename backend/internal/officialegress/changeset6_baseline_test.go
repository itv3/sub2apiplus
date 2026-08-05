package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

const (
	changeset6WorkspaceBaselineSHA256 = "c8c6e6452390abd076ae771b17b11bf079a2a3c45359707cf6ac8fb11ed5c760"
	changeset6BenchmarkMetadataSHA256 = "bb206905aeae5933e5a6c254bf39677b14bb279169af307421ecde4bdee835ca"
	changeset6BaselineReceiptSHA256   = "832f2d0f28d564cb60f1fef3b0d3af587f39137d5ba7cec6a91ee7cc682cf0db"
)

type changeset6BaselineFile struct {
	Path      string `json:"path"`
	Existence string `json:"existence"`
	FileType  string `json:"file_type"`
	Mode      string `json:"mode"`
	Size      int64  `json:"size"`
	SHA256    string `json:"sha256"`
	GitStatus string `json:"git_status,omitempty"`
}

type changeset6WorkspaceBaseline struct {
	SchemaVersion       string                   `json:"schema_version"`
	Changeset           string                   `json:"changeset"`
	TaskStatus          string                   `json:"task_status"`
	Head                string                   `json:"head"`
	HeadTree            string                   `json:"head_tree"`
	WorkspacePathCount  int                      `json:"workspace_path_count"`
	WorkspacePathSetSHA string                   `json:"workspace_path_set_sha256"`
	WorkspaceEntries    []changeset6BaselineFile `json:"workspace_entries"`
	Changeset5FinalWire []changeset6BaselineFile `json:"changeset5_post_final_wire"`
	RuntimeReleaseData  []changeset6BaselineFile `json:"runtime_release_data"`
	Rules               []string                 `json:"rules"`
}

type changeset6BenchmarkMetadata struct {
	SchemaVersion string `json:"schema_version"`
	Changeset     string `json:"changeset"`
	CapturedAt    string `json:"captured_at"`
	Environment   struct {
		GoVersion  string `json:"go_version"`
		GOOS       string `json:"goos"`
		GOARCH     string `json:"goarch"`
		CPU        string `json:"cpu"`
		GOMAXPROCS int    `json:"gomaxprocs"`
	} `json:"environment"`
	Git struct {
		Head     string `json:"head"`
		HeadTree string `json:"head_tree"`
	} `json:"git"`
	Fixture struct {
		GeneratorPath   string         `json:"generator_path"`
		GeneratorSHA256 string         `json:"generator_sha256"`
		FixtureSHA256   string         `json:"fixture_sha256"`
		Sizes           map[string]int `json:"sizes"`
	} `json:"fixture"`
	Commands   []string `json:"commands"`
	RawResults []struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"raw_results"`
	Acceptance map[string]any `json:"acceptance"`
}

func TestChangeset6BaselineIsIndependentlyFrozen(t *testing.T) {
	root := filepath.Join("../../..", "docs", "egress", "validation", "baseline")
	workspaceRaw := changeset6ReadBaselineFile(t, filepath.Join(root, "workspace-manifest.json"))
	metadataRaw := changeset6ReadBaselineFile(t, filepath.Join(root, "benchmark-metadata.json"))
	receiptRaw := changeset6ReadBaselineFile(t, filepath.Join(root, "receipt.json"))
	if changeset6BaselineSHA256(workspaceRaw) != changeset6WorkspaceBaselineSHA256 ||
		changeset6BaselineSHA256(metadataRaw) != changeset6BenchmarkMetadataSHA256 ||
		changeset6BaselineSHA256(receiptRaw) != changeset6BaselineReceiptSHA256 {
		t.Fatal("变更集 6 基线 manifest、benchmark metadata 或 receipt 摘要漂移")
	}

	var workspace changeset6WorkspaceBaseline
	changeset6DecodeBaselineStrict(t, workspaceRaw, &workspace)
	if workspace.SchemaVersion != "changeset6-workspace-baseline/v1" || workspace.Changeset != "6" ||
		workspace.TaskStatus != "方案有条件通过" ||
		workspace.Head != "38a9929eac35a39c86de2f27de8f7a805d7dae52" ||
		workspace.HeadTree != "a8c3dee18a01a6138bfcea60860bb5ad11548c3a" ||
		workspace.WorkspacePathCount != 498 || len(workspace.WorkspaceEntries) != 498 ||
		workspace.WorkspacePathSetSHA != "b369b773c681bdcb4d50f44ffca0532c4e3ea0e7635474ffbb02fb82db0babf2" ||
		len(workspace.Changeset5FinalWire) != 3 || len(workspace.RuntimeReleaseData) != 5 {
		t.Fatalf("变更集 6 工作区基线顶层事实漂移：%+v", workspace)
	}
	paths := make([]string, 0, len(workspace.WorkspaceEntries))
	seen := make(map[string]bool, len(workspace.WorkspaceEntries))
	for _, entry := range workspace.WorkspaceEntries {
		if seen[entry.Path] || (entry.FileType != "regular" && entry.FileType != "absent") ||
			entry.GitStatus == "" {
			t.Fatalf("变更集 6 工作区基线路径重复、类型或状态非法：%+v", entry)
		}
		seen[entry.Path] = true
		paths = append(paths, entry.Path)
	}
	if !sort.StringsAreSorted(paths) {
		t.Fatal("变更集 6 工作区基线路径未严格排序")
	}

	var metadata changeset6BenchmarkMetadata
	changeset6DecodeBaselineStrict(t, metadataRaw, &metadata)
	if metadata.SchemaVersion != "changeset6-benchmark-baseline/v1" || metadata.Changeset != "6" ||
		metadata.Environment.GoVersion != "go1.26.4" || metadata.Environment.GOOS != "darwin" ||
		metadata.Environment.GOARCH != "arm64" || metadata.Environment.CPU != "Apple M4" ||
		metadata.Environment.GOMAXPROCS != 10 || metadata.Git.Head != workspace.Head ||
		metadata.Git.HeadTree != workspace.HeadTree ||
		metadata.Fixture.GeneratorPath != "backend/internal/service/official_egress_changeset6_benchmark_test.go" ||
		metadata.Fixture.GeneratorSHA256 != "c2902c023bfddb35d6b746a103069774d7d14c4b2a300efa1efb25632c320cab" ||
		metadata.Fixture.FixtureSHA256 != "7698cddeadace650567e46e7be9b66286212e26e983edb29e78da423ac713e08" ||
		len(metadata.Commands) != 3 || len(metadata.RawResults) != 3 {
		t.Fatalf("变更集 6 benchmark 基线顶层事实漂移：%+v", metadata)
	}
	for path, expected := range map[string]string{
		filepath.Join(root, "benchmark-drivers", "body-pre_test.go"):    metadata.Fixture.GeneratorSHA256,
		filepath.Join(root, "benchmark-drivers", "catalog-pre_test.go"): "f7731b9f5a2999e94ab869f245ba74e20654c22b018af74ab7a6f430f1822aed",
		filepath.Join(root, "benchmark-drivers", "profile-pre_test.go"): "66e0775b72be6456d71c5527b21664c1b06b9c743fdfb83515a808812331a846",
	} {
		if got := changeset6BaselineSHA256(changeset6ReadBaselineFile(t, path)); got != expected {
			t.Fatalf("变更集 6 pre benchmark driver 摘要漂移：path=%s got=%s", path, got)
		}
	}
	for _, result := range metadata.RawResults {
		path := strings.Replace(result.Path, "docs/changeset6/", "docs/egress/validation/", 1)
		raw := changeset6ReadBaselineFile(t, filepath.Join("../../..", path))
		if changeset6BaselineSHA256(raw) != result.SHA256 {
			t.Fatalf("变更集 6 benchmark 原始结果摘要漂移：%s", result.Path)
		}
	}
}

func changeset6ReadBaselineFile(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func changeset6DecodeBaselineStrict(t *testing.T, raw []byte, target any) {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("变更集 6 基线 JSON 尾部存在额外数据")
	}
}

func changeset6BaselineSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
