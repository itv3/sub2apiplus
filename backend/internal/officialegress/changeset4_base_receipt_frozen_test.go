package officialegress

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const (
	changeset4BaseReceiptSHA256  = "95c11c24707242adca40456495c6be2113a56d37d5964696a00936c2973020bf"
	changeset4BaseManifestSHA256 = "701782a72cf23b032e9a11aed5d08de9978cc5041f52ed7aa731035c94e7369e"
	changeset4BaseManifestCount  = 536
)

type changeset4BaseReceipt struct {
	SchemaVersion        string                    `json:"schema_version"`
	Changeset            string                    `json:"changeset"`
	CreatedAt            string                    `json:"created_at"`
	Git                  changeset4BaseReceiptGit  `json:"git"`
	WorkingTree          changeset4BaseWorkingTree `json:"working_tree"`
	ManifestScope        []string                  `json:"manifest_scope"`
	BaselineVerification []string                  `json:"baseline_verification"`
	Rules                []string                  `json:"rules"`
}

type changeset4BaseReceiptGit struct {
	Branch   string `json:"branch"`
	Head     string `json:"head"`
	HeadTree string `json:"head_tree"`
}

type changeset4BaseWorkingTree struct {
	StatusSHA256               string `json:"status_porcelain_v1_z_sha256"`
	DiffSHA256                 string `json:"diff_binary_full_index_against_head_sha256"`
	TrackedDirtyPathCount      int    `json:"tracked_dirty_path_count"`
	UntrackedPathCount         int    `json:"untracked_nonignored_path_count"`
	IgnoredEvidenceFileCount   int    `json:"accepted_ignored_evidence_file_count"`
	BaseFileManifest           string `json:"base_file_manifest"`
	BaseFileManifestEntryCount int    `json:"base_file_manifest_entry_count"`
	BaseFileManifestSHA256     string `json:"base_file_manifest_sha256"`
}

func TestChangeset4BaseReceiptIsIndependentlyFrozen(t *testing.T) {
	receiptPath := filepath.Join("../../..", "docs", "egress", "source-freeze", "base-receipt.json")
	receiptRaw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset4BaseSHA256(receiptRaw); got != changeset4BaseReceiptSHA256 {
		t.Fatalf("BaseReceipt 摘要漂移：got=%s want=%s", got, changeset4BaseReceiptSHA256)
	}

	var receipt changeset4BaseReceipt
	if err := changeset4DecodeBaseReceipt(receiptRaw, &receipt); err != nil {
		t.Fatalf("BaseReceipt 结构非法：%v", err)
	}
	changeset4RequireBaseReceiptFacts(t, receipt)

	manifestPath := filepath.Join("../../..", filepath.FromSlash(strings.Replace(
		receipt.WorkingTree.BaseFileManifest,
		"docs/changeset4/", "docs/egress/source-freeze/", 1,
	)))
	manifestRaw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset4BaseSHA256(manifestRaw); got != changeset4BaseManifestSHA256 {
		t.Fatalf("BaseReceipt manifest 摘要漂移：got=%s want=%s", got, changeset4BaseManifestSHA256)
	}
	if receipt.WorkingTree.BaseFileManifestSHA256 != changeset4BaseManifestSHA256 {
		t.Fatalf("BaseReceipt 声明的 manifest 摘要与源码锚点不一致：%s", receipt.WorkingTree.BaseFileManifestSHA256)
	}
	entries, err := changeset4ParseBaseManifest(manifestRaw)
	if err != nil {
		t.Fatalf("BaseReceipt manifest 结构非法：%v", err)
	}
	if len(entries) != changeset4BaseManifestCount ||
		receipt.WorkingTree.BaseFileManifestEntryCount != changeset4BaseManifestCount {
		t.Fatalf("BaseReceipt manifest 条目数非法：actual=%d declared=%d want=%d",
			len(entries), receipt.WorkingTree.BaseFileManifestEntryCount, changeset4BaseManifestCount)
	}
}

func changeset4DecodeBaseReceipt(raw []byte, target *changeset4BaseReceipt) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("BaseReceipt 后存在多余数据")
	}
	return nil
}

func changeset4RequireBaseReceiptFacts(t *testing.T, receipt changeset4BaseReceipt) {
	t.Helper()
	if receipt.SchemaVersion != "sub2api.changeset4.base_receipt.v1" ||
		receipt.Changeset != "4" || receipt.CreatedAt != "2026-08-03T22:27:37Z" {
		t.Fatalf("BaseReceipt 顶层事实漂移：%+v", receipt)
	}
	wantGit := changeset4BaseReceiptGit{
		Branch:   "main",
		Head:     "38a9929eac35a39c86de2f27de8f7a805d7dae52",
		HeadTree: "a8c3dee18a01a6138bfcea60860bb5ad11548c3a",
	}
	if receipt.Git != wantGit || !changeset4IsCanonicalLowerHex(receipt.Git.Head, 20) ||
		!changeset4IsCanonicalLowerHex(receipt.Git.HeadTree, 20) {
		t.Fatalf("BaseReceipt Git 锚点漂移：got=%+v want=%+v", receipt.Git, wantGit)
	}
	wantWorkingTree := changeset4BaseWorkingTree{
		StatusSHA256:               "41bbc49a3fac0dcaa0cb43af0d091868d431028a7f43effc96d2fe9a5f20e243",
		DiffSHA256:                 "34ef83c3999345d2595a48df4d7ff1598dd025746c1076eda47cdd10d22135b9",
		TrackedDirtyPathCount:      141,
		UntrackedPathCount:         293,
		IgnoredEvidenceFileCount:   157,
		BaseFileManifest:           "docs/changeset4/base-files.sha256",
		BaseFileManifestEntryCount: changeset4BaseManifestCount,
		BaseFileManifestSHA256:     changeset4BaseManifestSHA256,
	}
	if receipt.WorkingTree != wantWorkingTree ||
		!changeset4IsCanonicalSHA256(receipt.WorkingTree.StatusSHA256) ||
		!changeset4IsCanonicalSHA256(receipt.WorkingTree.DiffSHA256) {
		t.Fatalf("BaseReceipt 工作区事实漂移：got=%+v want=%+v", receipt.WorkingTree, wantWorkingTree)
	}
	wantScope := []string{
		"相对 HEAD 的全部已跟踪差异路径",
		"全部未忽略的未跟踪路径",
		"docs/changeset0 至 docs/changeset3 的已验收且被忽略证据文件",
	}
	wantVerification := []string{
		"go test ./internal/officialegress/... -count=1",
		"状态键、WHAM、WS compression、连接池隔离相关聚焦 service 测试",
		"git diff --check",
	}
	wantRules := []string{
		"本收据只冻结变更集 4 开发前已验收脏工作区，不把 HEAD 冒充完整基线",
		"后续变更必须以 HEAD 内容、base-files.sha256 与新增文件三者共同区分",
		"原始 SnapshotDoc/ProfileSpec 证据层保持保真；执行层由 ExecutableProfile 承担",
		"不得自动覆盖或原地重写本收据和 base-files.sha256",
	}
	if !reflect.DeepEqual(receipt.ManifestScope, wantScope) ||
		!reflect.DeepEqual(receipt.BaselineVerification, wantVerification) ||
		!reflect.DeepEqual(receipt.Rules, wantRules) {
		t.Fatalf("BaseReceipt 范围、验证或规则漂移：%+v", receipt)
	}
}

func changeset4ParseBaseManifest(raw []byte) ([]string, error) {
	if len(raw) == 0 || raw[len(raw)-1] != '\n' {
		return nil, errors.New("manifest 必须非空并以换行结束")
	}
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 1024), 1024*1024)
	paths := make([]string, 0, changeset4BaseManifestCount)
	seen := make(map[string]struct{}, changeset4BaseManifestCount)
	previous := ""
	for lineNumber := 1; scanner.Scan(); lineNumber++ {
		line := scanner.Text()
		if len(line) < sha256.Size*2+3 || line[sha256.Size*2:sha256.Size*2+2] != "  " {
			return nil, fmt.Errorf("第 %d 行不是 '<sha256><双空格><路径>'", lineNumber)
		}
		digest := line[:sha256.Size*2]
		entryPath := line[sha256.Size*2+2:]
		if !changeset4IsCanonicalSHA256(digest) {
			return nil, fmt.Errorf("第 %d 行不是完整小写 SHA-256", lineNumber)
		}
		if err := changeset4ValidateBaseManifestPath(entryPath); err != nil {
			return nil, fmt.Errorf("第 %d 行路径非法：%w", lineNumber, err)
		}
		if _, duplicate := seen[entryPath]; duplicate {
			return nil, fmt.Errorf("第 %d 行路径重复：%s", lineNumber, entryPath)
		}
		if previous != "" && previous >= entryPath {
			return nil, fmt.Errorf("第 %d 行没有按路径严格递增排序：%s", lineNumber, entryPath)
		}
		seen[entryPath] = struct{}{}
		paths = append(paths, entryPath)
		previous = entryPath
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return paths, nil
}

func changeset4ValidateBaseManifestPath(value string) error {
	if value == "" || path.IsAbs(value) || path.Clean(value) != value || value == "." ||
		strings.HasPrefix(value, "../") || strings.Contains(value, "\\") {
		return fmt.Errorf("不是仓库内规范相对路径：%q", value)
	}
	for _, character := range value {
		if character < 0x20 || character == 0x7f {
			return fmt.Errorf("包含控制字符：%q", value)
		}
	}
	return nil
}

func changeset4IsCanonicalSHA256(value string) bool {
	return changeset4IsCanonicalLowerHex(value, sha256.Size)
}

func changeset4IsCanonicalLowerHex(value string, byteCount int) bool {
	if len(value) != byteCount*2 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == byteCount
}

func changeset4BaseSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
