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
	"slices"
	"strings"
	"testing"
)

const (
	claudeOfflineCIStabilizationTransitionPath = "docs/egress/maintenance/claude-offline-ci-stabilization-transition.json"
	claudeOfflineCIStabilizationTransitionSHA  = "f86194ed0167485c2ff91efca6b4b834ab5c000b3768a35ad9924f84818b47d5"
)

type claudeOfflineCIStabilizationReceipt struct {
	SchemaVersion string                                `json:"schema_version"`
	IssuedAtUTC   string                                `json:"issued_at_utc"`
	BaseCommit    string                                `json:"base_commit"`
	Scope         string                                `json:"scope"`
	Predecessor   claudeOfflineReleaseRepairPredecessor `json:"predecessor"`
	Transitions   []changeset4SourceTransitionEntry     `json:"transitions"`
	Verification  []string                              `json:"verification"`
	Safety        claudeOfflineReleaseRepairSafety      `json:"safety"`
	Result        string                                `json:"result"`
}

func loadClaudeOfflineCIStabilizationTransition() (
	claudeOfflineCIStabilizationReceipt,
	error,
) {
	var receipt claudeOfflineCIStabilizationReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(claudeOfflineCIStabilizationTransitionPath),
	))
	if err != nil {
		return receipt, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != claudeOfflineCIStabilizationTransitionSHA {
		return receipt, errors.New("Claude 离线 CI 稳定化 transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Claude 离线 CI 稳定化 transition 尾部存在额外 JSON")
	}
	if err := validateClaudeOfflineCIStabilizationTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudeOfflineCIStabilizationTransition(
	receipt claudeOfflineCIStabilizationReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-claude-offline-ci-stabilization-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T16:05:00Z" ||
		receipt.BaseCommit != "7a0c4bdc199bfda81e3cdd64c4ad4237c4ffe5d9" ||
		receipt.Scope != "claude-offline-ci-stabilization" ||
		receipt.Result != "passed_offline_ci_stabilization" ||
		len(receipt.Transitions) != 27 {
		return errors.New("Claude 离线 CI 稳定化 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "offline_release_repair_transition" ||
		receipt.Predecessor.Path != claudeOfflineReleaseRepairTransitionPath ||
		receipt.Predecessor.SHA256 != claudeOfflineReleaseRepairTransitionSHA {
		return errors.New("Claude 离线 CI 稳定化 transition 前序非法")
	}
	if !slices.Equal(receipt.Verification, []string{
		"make test-capture-tools",
		"go test -tags=unit ./...",
		"go test -tags=integration ./...",
		"golangci-lint run --timeout=30m",
		"EGRESS_SEAL_BASE_REF=cb7e85b577cbe8fcc75c34b56238981cc4f7c8ea make check-egress-spec-ci",
	}) {
		return errors.New("Claude 离线 CI 稳定化 transition 验证集合非法")
	}
	safety := receipt.Safety
	if safety.LiveAccountUsed || safety.OnlineAcceptancePerformed ||
		safety.ReleaseReadinessChanged || safety.VircsAccessed || safety.VircsChanged ||
		safety.ReleaseState != "candidate_deployed/not_ready_for_operator_release" {
		return errors.New("Claude 离线 CI 稳定化 transition 安全边界非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			!receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			!claudeOfflineCIStabilizationTargetMatches(
				transition.Path, transition.ToSHA256,
			) {
			return errors.New("Claude 离线 CI 稳定化 transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude 离线 CI 稳定化 transition 路径未排序或重复")
	}
	return nil
}

func claudeOfflineCIStabilizationTargetMatches(path string, want string) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	current := hex.EncodeToString(sum[:])
	return current == want || upstreamMergeFrameworkTransitionSupersedes(
		path, want, current,
	)
}

// claudeOfflineCIStabilizationTransitionSupersedes 只接受本轮固定收据中的精确
// path/from/to；它不代表在线验收完成，也不改变发布就绪状态。
func claudeOfflineCIStabilizationTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if upstreamMergeFrameworkTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	receipt, err := loadClaudeOfflineCIStabilizationTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest &&
			claudeOfflineCIStabilizationPriorReaches(
				path, priorDigest, transition.FromSHA256,
			) {
			return true
		}
	}
	return false
}

func claudeOfflineCIStabilizationPriorReaches(
	path string,
	priorDigest string,
	targetDigest string,
) bool {
	if claudeOfflineCIStabilizationPriorReachesBeforeRepair(
		path, priorDigest, targetDigest,
	) {
		return true
	}
	var repairReceipt claudeOfflineReleaseRepairReceipt
	if !claudeOfflineCIStabilizationReadFrozenReceipt(
		claudeOfflineReleaseRepairTransitionPath,
		claudeOfflineReleaseRepairTransitionSHA,
		&repairReceipt,
	) {
		return false
	}
	for _, transition := range repairReceipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == targetDigest &&
			claudeOfflineCIStabilizationPriorReachesBeforeRepair(
				path, priorDigest, transition.FromSHA256,
			) {
			return true
		}
	}
	return false
}

func claudeOfflineCIStabilizationPriorReachesBeforeRepair(
	path string,
	priorDigest string,
	targetDigest string,
) bool {
	if claudeOfflineCIStabilizationPriorReachesBeforeOfficialClientOnly(
		path, priorDigest, targetDigest,
	) {
		return true
	}
	var officialReceipt claudeOfficialClientOnlyTransitionReceipt
	if !claudeOfflineCIStabilizationReadFrozenReceipt(
		claudeOfficialClientOnlyTransitionPath,
		claudeOfficialClientOnlyTransitionSHA,
		&officialReceipt,
	) {
		return false
	}
	for _, transition := range officialReceipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == targetDigest &&
			claudeOfflineCIStabilizationPriorReachesBeforeOfficialClientOnly(
				path, priorDigest, transition.FromSHA256,
			) {
			return true
		}
	}
	return false
}

func claudeOfflineCIStabilizationPriorReachesBeforeOfficialClientOnly(
	path string,
	priorDigest string,
	targetDigest string,
) bool {
	if priorDigest == targetDigest ||
		claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
			path, priorDigest, targetDigest,
		) {
		return true
	}
	var catalogReceipt claudePersonaReleaseCatalogTransitionReceipt
	if !claudeOfflineCIStabilizationReadFrozenReceipt(
		claudePersonaReleaseCatalogTransitionPath,
		claudePersonaReleaseCatalogTransitionSHA256,
		&catalogReceipt,
	) {
		return false
	}
	for _, transition := range catalogReceipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == targetDigest &&
			(transition.FromSHA256 == priorDigest ||
				claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
					path, priorDigest, transition.FromSHA256,
				)) {
			return true
		}
	}
	return false
}

func claudeOfflineCIStabilizationReadFrozenReceipt(
	path string,
	wantDigest string,
	target any,
) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]) == wantDigest && json.Unmarshal(raw, target) == nil
}

func TestClaudeOfflineCIStabilizationTransitionIsFrozen(t *testing.T) {
	if _, err := loadClaudeOfflineCIStabilizationTransition(); err != nil {
		t.Fatal(err)
	}
}
