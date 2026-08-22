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
	claudeOfflineReleaseRepairTransitionPath = "docs/egress/maintenance/claude-offline-release-repair-transition.json"
	claudeOfflineReleaseRepairTransitionSHA  = "3f13b450e44ae619520bae561b9fd6f68ba08c032aabdf94cfea35cbbd0258ec"
)

type claudeOfflineReleaseRepairPredecessor struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeOfflineReleaseRepairSafety struct {
	LiveAccountUsed           bool   `json:"live_account_used"`
	OnlineAcceptancePerformed bool   `json:"online_acceptance_performed"`
	ReleaseReadinessChanged   bool   `json:"release_readiness_changed"`
	ReleaseState              string `json:"release_state"`
	VircsAccessed             bool   `json:"vircs_accessed"`
	VircsChanged              bool   `json:"vircs_changed"`
}

type claudeOfflineReleaseRepairReceipt struct {
	SchemaVersion    string                                `json:"schema_version"`
	IssuedAtUTC      string                                `json:"issued_at_utc"`
	BaseCommit       string                                `json:"base_commit"`
	CheckpointCommit string                                `json:"checkpoint_commit"`
	Scope            string                                `json:"scope"`
	Predecessor      claudeOfflineReleaseRepairPredecessor `json:"predecessor"`
	Transitions      []changeset4SourceTransitionEntry     `json:"transitions"`
	Verification     []string                              `json:"verification"`
	Safety           claudeOfflineReleaseRepairSafety      `json:"safety"`
	Result           string                                `json:"result"`
}

func loadClaudeOfflineReleaseRepairTransition() (
	claudeOfflineReleaseRepairReceipt,
	error,
) {
	var receipt claudeOfflineReleaseRepairReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(claudeOfflineReleaseRepairTransitionPath),
	))
	if err != nil {
		return receipt, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != claudeOfflineReleaseRepairTransitionSHA {
		return receipt, errors.New("Claude 离线发版修复 transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Claude 离线发版修复 transition 尾部存在额外 JSON")
	}
	if err := validateClaudeOfflineReleaseRepairTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudeOfflineReleaseRepairTransition(
	receipt claudeOfflineReleaseRepairReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-claude-offline-release-repair-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T15:30:00Z" ||
		receipt.BaseCommit != "8a33e1c902f5b5bf911b4625dea6f35b70321183" ||
		receipt.CheckpointCommit != "e2ef4c5c28f79ea86b32e582ba1d7a24ee4d91c8" ||
		receipt.Scope != "claude-offline-ci-repair" ||
		receipt.Result != "passed_offline_ci_validation" ||
		len(receipt.Transitions) != 5 {
		return errors.New("Claude 离线发版修复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "official_client_only_transition" ||
		receipt.Predecessor.Path != claudeOfficialClientOnlyTransitionPath ||
		receipt.Predecessor.SHA256 != claudeOfficialClientOnlyTransitionSHA {
		return errors.New("Claude 离线发版修复 transition 前序非法")
	}
	if !slices.Equal(receipt.Verification, []string{
		"go test -tags=unit ./internal/service",
		"go test -tags=unit ./internal/officialegress",
		"EGRESS_SEAL_BASE_REF=origin/main make check-egress-spec-ci",
	}) {
		return errors.New("Claude 离线发版修复 transition 验证集合非法")
	}
	safety := receipt.Safety
	if safety.LiveAccountUsed || safety.OnlineAcceptancePerformed ||
		safety.ReleaseReadinessChanged || safety.VircsAccessed || safety.VircsChanged ||
		safety.ReleaseState != "candidate_deployed/not_ready_for_operator_release" {
		return errors.New("Claude 离线发版修复 transition 安全边界非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			!receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			!claudeOfflineReleaseRepairTargetMatches(
				transition.Path, transition.ToSHA256,
			) {
			return errors.New("Claude 离线发版修复 transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude 离线发版修复 transition 路径未排序或重复")
	}
	return nil
}

func claudeOfflineReleaseRepairTargetMatches(path string, want string) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]) == want
}

// claudeOfflineReleaseRepairTransitionSupersedes 只接受固定修复收据中的精确
// path/from/to，不把离线 CI 修复解释为在线验收或发布就绪状态变化。
func claudeOfflineReleaseRepairTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	receipt, err := loadClaudeOfflineReleaseRepairTransition()
	if err != nil {
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

func TestClaudeOfflineReleaseRepairTransitionIsFrozen(t *testing.T) {
	if _, err := loadClaudeOfflineReleaseRepairTransition(); err != nil {
		t.Fatal(err)
	}
}
