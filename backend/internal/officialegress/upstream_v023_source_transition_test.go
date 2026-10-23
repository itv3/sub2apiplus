package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
)

const (
	upstreamV023SourceTransitionPath = "docs/egress/maintenance/upstream-v0.2.3-source-transition.json"
	upstreamV023SourceBaseCommit     = "681909c2e1bddcbf2a3700c7713e03647507fe26"
)

type upstreamV023SourceTransitionEntry struct {
	Path              string  `json:"path"`
	OldPath           string  `json:"old_path"`
	Status            string  `json:"status"`
	PredecessorSHA256 *string `json:"predecessor_sha256"`
	CurrentSHA256     *string `json:"current_sha256"`
	Reason            string  `json:"reason"`
}

type upstreamV023SourceTransitionReceipt struct {
	SchemaVersion       string                              `json:"schema_version"`
	BaseCommit          string                              `json:"base_commit"`
	CurrentCommit       string                              `json:"current_commit"`
	BaseTree            string                              `json:"base_tree"`
	CurrentTree         string                              `json:"current_tree"`
	ChainSequence       int                                 `json:"chain_sequence"`
	PredecessorRegister json.RawMessage                     `json:"predecessor_register"`
	Entries             []upstreamV023SourceTransitionEntry `json:"entries"`
	EntryCount          int                                 `json:"entry_count"`
	ReasonPolicy        string                              `json:"reason_policy"`
	Result              string                              `json:"result"`
	IdentitySHA256      string                              `json:"identity_sha256"`
}

var (
	upstreamV023SourceTransitionOnce    sync.Once
	upstreamV023SourceTransitionCached  upstreamV023SourceTransitionReceipt
	upstreamV023SourceTransitionLoadErr error
)

func loadUpstreamV023SourceTransition() (upstreamV023SourceTransitionReceipt, error) {
	upstreamV023SourceTransitionOnce.Do(func() {
		upstreamV023SourceTransitionCached, upstreamV023SourceTransitionLoadErr =
			readUpstreamV023SourceTransition()
	})
	return upstreamV023SourceTransitionCached, upstreamV023SourceTransitionLoadErr
}

func readUpstreamV023SourceTransition() (upstreamV023SourceTransitionReceipt, error) {
	var receipt upstreamV023SourceTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(upstreamV023SourceTransitionPath)))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游 v0.2.3 源码 transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkDigest(append(canonical, '\n')) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游 v0.2.3 源码 transition 自摘要不一致")
	}
	if err := validateUpstreamV023SourceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateUpstreamV023SourceTransition(receipt upstreamV023SourceTransitionReceipt) error {
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v2" ||
		receipt.BaseCommit != upstreamV023SourceBaseCommit ||
		!upstreamV023GitObject(receipt.CurrentCommit) ||
		!upstreamV023GitObject(receipt.BaseTree) || !upstreamV023GitObject(receipt.CurrentTree) ||
		receipt.ChainSequence != 1 || string(receipt.PredecessorRegister) != "null" ||
		receipt.EntryCount != len(receipt.Entries) || receipt.EntryCount == 0 ||
		strings.TrimSpace(receipt.ReasonPolicy) == "" || receipt.Result != "generated" ||
		!receiptSHA256(receipt.IdentitySHA256) {
		return errors.New("上游 v0.2.3 源码 transition 顶层事实非法")
	}
	baseTree, err := upstreamV023GitOutput("rev-parse", receipt.BaseCommit+"^{tree}")
	if err != nil || baseTree != receipt.BaseTree {
		return errors.New("上游 v0.2.3 源码 transition 基准 tree 不一致")
	}
	currentTree, err := upstreamV023GitOutput("rev-parse", receipt.CurrentCommit+"^{tree}")
	if err != nil || currentTree != receipt.CurrentTree {
		return errors.New("上游 v0.2.3 源码 transition 当前 tree 不一致")
	}
	if err := upstreamV023GitAncestor(receipt.BaseCommit, receipt.CurrentCommit); err != nil {
		return errors.New("上游 v0.2.3 源码 transition 提交关系非法")
	}
	if err := upstreamV023GitAncestor(receipt.CurrentCommit, "HEAD"); err != nil {
		return errors.New("上游 v0.2.3 源码 transition 未被当前 HEAD 承接")
	}
	paths := make([]string, 0, len(receipt.Entries))
	for _, entry := range receipt.Entries {
		if err := validateUpstreamV023SourceTransitionEntry(entry); err != nil {
			return err
		}
		paths = append(paths, entry.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 v0.2.3 源码 transition 路径未严格排序")
	}
	return nil
}

func validateUpstreamV023SourceTransitionEntry(entry upstreamV023SourceTransitionEntry) error {
	if strings.TrimSpace(entry.Path) == "" || filepath.IsAbs(filepath.FromSlash(entry.Path)) ||
		strings.HasPrefix(filepath.ToSlash(entry.Path), "../") || strings.TrimSpace(entry.Reason) == "" {
		return errors.New("上游 v0.2.3 源码 transition 条目非法")
	}
	if entry.OldPath != "" && entry.Status != "R" && entry.Status != "C" {
		return errors.New("上游 v0.2.3 源码 transition old_path 非法")
	}
	switch entry.Status {
	case "A":
		if entry.PredecessorSHA256 != nil || !upstreamV023DigestPointer(entry.CurrentSHA256) {
			return errors.New("上游 v0.2.3 源码 transition 新增条目非法")
		}
	case "D":
		if !upstreamV023DigestPointer(entry.PredecessorSHA256) || entry.CurrentSHA256 != nil {
			return errors.New("上游 v0.2.3 源码 transition 删除条目非法")
		}
	case "M", "R", "C", "T":
		if !upstreamV023DigestPointer(entry.PredecessorSHA256) || !upstreamV023DigestPointer(entry.CurrentSHA256) {
			return errors.New("上游 v0.2.3 源码 transition 修改条目非法")
		}
	default:
		return errors.New("上游 v0.2.3 源码 transition 状态非法")
	}
	currentPath := filepath.Join("../../..", filepath.FromSlash(entry.Path))
	if entry.CurrentSHA256 == nil {
		if _, err := os.Stat(currentPath); !errors.Is(err, os.ErrNotExist) {
			return errors.New("上游 v0.2.3 源码 transition 删除路径仍存在：" + entry.Path)
		}
		return nil
	}
	raw, err := os.ReadFile(currentPath)
	if !auditedSourceCurrentStateAccepted(entry.Path, *entry.CurrentSHA256, raw, err) {
		return errors.New("上游 v0.2.3 源码 transition 当前摘要不一致：" + entry.Path)
	}
	return nil
}

func upstreamV023DigestPointer(value *string) bool {
	return value != nil && receiptSHA256(*value)
}

func upstreamV023GitObject(value string) bool {
	return len(value) == 40 && strings.Trim(value, "0123456789abcdef") == ""
}

func upstreamV023GitOutput(arguments ...string) (string, error) {
	command := exec.Command("git", arguments...)
	command.Dir = filepath.Join("../../..")
	raw, err := command.Output()
	return strings.TrimSpace(string(raw)), err
}

func upstreamV023GitAncestor(before, after string) error {
	command := exec.Command("git", "merge-base", "--is-ancestor", before, after)
	command.Dir = filepath.Join("../../..")
	return command.Run()
}

// upstreamV023SourceTransitionSupersedes 只允许本次 Git 自动生成节点中的精确
// path／基准摘要／当前摘要边；历史收据必须先由既有 successor 到达该基准摘要。
func upstreamV023SourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) || priorDigest == currentDigest {
		return false
	}
	if auditedSourceSuccessorReaches(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamV023PostBootstrapSourceSuccessorSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamV023SourceTransitionDirectSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadUpstreamV023SourceTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Entries {
		if entry.Path != path || entry.PredecessorSHA256 == nil || entry.CurrentSHA256 == nil ||
			*entry.CurrentSHA256 != currentDigest {
			continue
		}
		baseDigest := *entry.PredecessorSHA256
		if upstreamV023PriorReachesSourceBase(path, priorDigest, baseDigest) {
			return true
		}
	}
	return false
}

func upstreamV023SourceTransitionDirectSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadUpstreamV023SourceTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Entries {
		if entry.Path == path && entry.PredecessorSHA256 != nil && entry.CurrentSHA256 != nil &&
			*entry.PredecessorSHA256 == priorDigest && *entry.CurrentSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func upstreamV023PriorReachesSourceBase(path, priorDigest, baseDigest string) bool {
	if priorDigest == baseDigest ||
		upstreamMergeFrameworkV3SuccessorSupersedes(path, priorDigest, baseDigest) ||
		historicalSourceDriftSupersedes(path, priorDigest, baseDigest) ||
		upstreamV023ScannerSuccessorTransitionSupersedes(path, priorDigest, baseDigest) {
		return true
	}
	if entries, err := loadHistoricalSourceDriftEntries(); err == nil {
		if entry, ok := entries[path]; ok && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.HeadSHA256 == baseDigest ||
				upstreamMergeFrameworkV3SuccessorSupersedes(path, entry.HeadSHA256, baseDigest) ||
				upstreamV023ScannerSuccessorTransitionSupersedes(path, entry.HeadSHA256, baseDigest)) {
			return true
		}
	}
	if scanner, err := loadUpstreamV023ScannerSuccessorTransition(); err == nil {
		for _, transition := range scanner.Transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				(transition.ToSHA256 == baseDigest ||
					upstreamMergeFrameworkV3SuccessorSupersedes(path, transition.ToSHA256, baseDigest) ||
					historicalSourceDriftSupersedes(path, transition.ToSHA256, baseDigest)) {
				return true
			}
		}
	}
	return codex0151WorktreeSuccessorEdge(path, priorDigest, baseDigest)
}

func TestUpstreamV023SourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV023SourceTransition(); err != nil {
		t.Fatal(err)
	}
}

func TestUpstreamV023SourceTransitionRejectsMutation(t *testing.T) {
	receipt, err := loadUpstreamV023SourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Entries = append([]upstreamV023SourceTransitionEntry(nil), receipt.Entries...)
	mutated.Entries[0].Path = "../越界"
	if err := validateUpstreamV023SourceTransition(mutated); err == nil {
		t.Fatal("变异后的上游 v0.2.3 源码 transition 被错误接受")
	}
}
