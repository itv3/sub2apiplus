package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
)

const codex01491TerminalStatePath = "docs/egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json"

type codex01491TerminalArtifact struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type codex01491TerminalTransition struct {
	Path               string   `json:"path"`
	State              string   `json:"state"`
	PriorSHA256s       []string `json:"prior_sha256s"`
	CurrentSHA256      string   `json:"current_sha256"`
	ConsolidatedReason string   `json:"consolidated_reason"`
}

type codex01491TerminalReceipt struct {
	SchemaVersion  string `json:"schema_version"`
	CompletedAtUTC string `json:"completed_at_utc"`
	Target         struct {
		Version               string `json:"version"`
		ProfileID             string `json:"profile_id"`
		ProfileSHA256         string `json:"profile_sha256"`
		PreviousVersion       string `json:"previous_version"`
		PreviousProfileSHA256 string `json:"previous_profile_sha256"`
	} `json:"target"`
	RuntimeCatalog struct {
		Catalog         codex01491TerminalArtifact `json:"catalog"`
		ReleaseGraph    codex01491TerminalArtifact `json:"release_graph"`
		SnapshotCatalog codex01491TerminalArtifact `json:"snapshot_catalog"`
		ActiveProfile   codex01491TerminalArtifact `json:"active_profile"`
	} `json:"runtime_catalog"`
	CatalogPromotion       codex01491TerminalArtifact     `json:"catalog_promotion"`
	ProductionActivation   codex01491TerminalArtifact     `json:"production_activation"`
	AuditArchive           codex01491TerminalArtifact     `json:"audit_archive"`
	SurfaceAdditions       []codex01491TerminalArtifact   `json:"surface_additions"`
	RetiredRuntimeProfiles []codex01491TerminalArtifact   `json:"retired_runtime_profiles"`
	Transitions            []codex01491TerminalTransition `json:"transitions"`
	Result                 string                         `json:"result"`
	IdentitySHA256         string                         `json:"identity_sha256"`
}

var (
	codex01491TerminalOnce    sync.Once
	codex01491TerminalCached  codex01491TerminalReceipt
	codex01491TerminalLoadErr error
)

func codex01491TerminalRepoPath(path string) string {
	return filepath.Join("../../..", filepath.FromSlash(path))
}

func validateCodex01491TerminalArtifact(artifact codex01491TerminalArtifact) error {
	if strings.TrimSpace(artifact.Path) == "" || len(artifact.SHA256) != 64 {
		return errors.New("0.149.1 终态制品坐标非法")
	}
	raw, err := os.ReadFile(codex01491TerminalRepoPath(artifact.Path))
	if err != nil || upstreamMergeFrameworkDigest(raw) != artifact.SHA256 {
		return errors.New("0.149.1 终态制品摘要不一致：" + artifact.Path)
	}
	return nil
}

func loadCodex01491TerminalState() (codex01491TerminalReceipt, error) {
	codex01491TerminalOnce.Do(func() {
		codex01491TerminalCached, codex01491TerminalLoadErr = readCodex01491TerminalState()
	})
	return codex01491TerminalCached, codex01491TerminalLoadErr
}

func readCodex01491TerminalState() (codex01491TerminalReceipt, error) {
	var receipt codex01491TerminalReceipt
	raw, err := os.ReadFile(codex01491TerminalRepoPath(codex01491TerminalStatePath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("0.149.1 终态收据尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil {
		return receipt, err
	}
	canonical = append(canonical, '\n')
	if upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("0.149.1 终态收据自摘要不一致")
	}
	if receipt.SchemaVersion != "official-client-codex-0.149.1-terminal-state/v1" ||
		receipt.Target.Version != "0.149.1" ||
		receipt.Target.ProfileID != "codex-0.149.1-official-r1491-v2" ||
		receipt.Target.ProfileSHA256 != "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3" ||
		receipt.Target.PreviousVersion != "0.147.0" ||
		receipt.Target.PreviousProfileSHA256 != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		receipt.Result != "passed" || strings.TrimSpace(receipt.CompletedAtUTC) == "" {
		return receipt, errors.New("0.149.1 终态收据顶层事实非法")
	}
	artifacts := []codex01491TerminalArtifact{
		receipt.RuntimeCatalog.Catalog,
		receipt.RuntimeCatalog.ReleaseGraph,
		receipt.RuntimeCatalog.SnapshotCatalog,
		receipt.RuntimeCatalog.ActiveProfile,
		receipt.CatalogPromotion,
		receipt.ProductionActivation,
	}
	for _, artifact := range artifacts {
		if err := validateCodex01491TerminalArtifact(artifact); err != nil {
			return receipt, err
		}
	}
	if receipt.AuditArchive.Path != "codex-0.149.1-consolidated-audit-20260830.tar.gz" ||
		receipt.AuditArchive.SHA256 != "cfb3d9afb4453b95662fbcfdf794b4d80efe168fd69accf909abdbf40b6e30d9" {
		return receipt, errors.New("0.149.1 外部审计归档坐标非法")
	}
	if len(receipt.SurfaceAdditions) != 1 ||
		receipt.SurfaceAdditions[0].Path != "backend/internal/officialegress/routing_hint.go" {
		return receipt, errors.New("0.149.1 出站面终态增量非法")
	}
	if err := validateCodex01491TerminalArtifact(receipt.SurfaceAdditions[0]); err != nil {
		return receipt, err
	}
	if len(receipt.RetiredRuntimeProfiles) != 2 {
		return receipt, errors.New("0.149.1 退休画像数量非法")
	}
	for _, artifact := range receipt.RetiredRuntimeProfiles {
		if _, err := os.Stat(codex01491TerminalRepoPath(artifact.Path)); !os.IsNotExist(err) {
			return receipt, errors.New("已退休 0.145 画像仍存在：" + artifact.Path)
		}
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.ConsolidatedReason) == "" ||
			len(transition.PriorSHA256s) == 0 ||
			!slices.IsSorted(transition.PriorSHA256s) ||
			len(transition.PriorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PriorSHA256s...),
			)) {
			return receipt, errors.New("0.149.1 终态后继条目非法")
		}
		for _, prior := range transition.PriorSHA256s {
			if len(prior) != 64 || prior == transition.CurrentSHA256 {
				return receipt, errors.New("0.149.1 终态前序摘要非法")
			}
		}
		currentRaw, readErr := os.ReadFile(codex01491TerminalRepoPath(transition.Path))
		switch transition.State {
		case "present":
			if readErr != nil || len(transition.CurrentSHA256) != 64 ||
				upstreamMergeFrameworkDigest(currentRaw) != transition.CurrentSHA256 {
				return receipt, errors.New("0.149.1 终态当前摘要不一致：" + transition.Path)
			}
		case "deleted":
			if !os.IsNotExist(readErr) || transition.CurrentSHA256 != "" {
				return receipt, errors.New("0.149.1 终态删除状态不一致：" + transition.Path)
			}
		default:
			return receipt, errors.New("0.149.1 终态文件状态非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("0.149.1 终态路径未严格排序")
	}
	return receipt, nil
}

// codex01491TerminalStateSupersedes 把已归档的逐轮 transition 压缩为一条精确终边。
// 只接受收据登记的 path／历史摘要／当前摘要三元组，不放宽生产路径或 wire 规则。
func codex01491TerminalStateSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491TerminalState()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.CurrentSHA256 == currentDigest &&
			slices.Contains(transition.PriorSHA256s, priorDigest) {
			return true
		}
	}
	return false
}

func TestCodex01491TerminalStateIsFrozen(t *testing.T) {
	if _, err := loadCodex01491TerminalState(); err != nil {
		t.Fatal(err)
	}
}
