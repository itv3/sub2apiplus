package service

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
	upstreamV023SourceTransitionServicePath = "docs/egress/maintenance/upstream-v0.2.3-source-transition.json"
	upstreamV023SourceBaseCommitService     = "681909c2e1bddcbf2a3700c7713e03647507fe26"
)

type upstreamV023SourceTransitionEntryService struct {
	Path              string  `json:"path"`
	OldPath           string  `json:"old_path"`
	Status            string  `json:"status"`
	PredecessorSHA256 *string `json:"predecessor_sha256"`
	CurrentSHA256     *string `json:"current_sha256"`
	Reason            string  `json:"reason"`
}

type upstreamV023SourceTransitionReceiptService struct {
	SchemaVersion       string                                     `json:"schema_version"`
	BaseCommit          string                                     `json:"base_commit"`
	CurrentCommit       string                                     `json:"current_commit"`
	BaseTree            string                                     `json:"base_tree"`
	CurrentTree         string                                     `json:"current_tree"`
	ChainSequence       int                                        `json:"chain_sequence"`
	PredecessorRegister json.RawMessage                            `json:"predecessor_register"`
	Entries             []upstreamV023SourceTransitionEntryService `json:"entries"`
	EntryCount          int                                        `json:"entry_count"`
	ReasonPolicy        string                                     `json:"reason_policy"`
	Result              string                                     `json:"result"`
	IdentitySHA256      string                                     `json:"identity_sha256"`
}

var (
	upstreamV023SourceTransitionServiceOnce    sync.Once
	upstreamV023SourceTransitionServiceCached  upstreamV023SourceTransitionReceiptService
	upstreamV023SourceTransitionServiceLoadErr error
)

func loadUpstreamV023SourceTransitionService() (upstreamV023SourceTransitionReceiptService, error) {
	upstreamV023SourceTransitionServiceOnce.Do(func() {
		upstreamV023SourceTransitionServiceCached, upstreamV023SourceTransitionServiceLoadErr =
			readUpstreamV023SourceTransitionService()
	})
	return upstreamV023SourceTransitionServiceCached, upstreamV023SourceTransitionServiceLoadErr
}

func readUpstreamV023SourceTransitionService() (upstreamV023SourceTransitionReceiptService, error) {
	var receipt upstreamV023SourceTransitionReceiptService
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(upstreamV023SourceTransitionServicePath)))
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
	if err != nil || upstreamMergeFrameworkServiceDigest(append(canonical, '\n')) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游 v0.2.3 源码 transition 自摘要不一致")
	}
	if err := validateUpstreamV023SourceTransitionService(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateUpstreamV023SourceTransitionService(receipt upstreamV023SourceTransitionReceiptService) error {
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v2" ||
		receipt.BaseCommit != upstreamV023SourceBaseCommitService ||
		!upstreamV023GitObjectService(receipt.CurrentCommit) ||
		!upstreamV023GitObjectService(receipt.BaseTree) || !upstreamV023GitObjectService(receipt.CurrentTree) ||
		receipt.ChainSequence != 1 || string(receipt.PredecessorRegister) != "null" ||
		receipt.EntryCount != len(receipt.Entries) || receipt.EntryCount == 0 ||
		strings.TrimSpace(receipt.ReasonPolicy) == "" || receipt.Result != "generated" ||
		!validOpenAIReplayOOMRepairServiceSHA(receipt.IdentitySHA256) {
		return errors.New("上游 v0.2.3 源码 transition 顶层事实非法")
	}
	baseTree, err := upstreamV023GitOutputService("rev-parse", receipt.BaseCommit+"^{tree}")
	if err != nil || baseTree != receipt.BaseTree {
		return errors.New("上游 v0.2.3 源码 transition 基准 tree 不一致")
	}
	currentTree, err := upstreamV023GitOutputService("rev-parse", receipt.CurrentCommit+"^{tree}")
	if err != nil || currentTree != receipt.CurrentTree {
		return errors.New("上游 v0.2.3 源码 transition 当前 tree 不一致")
	}
	if err := upstreamV023GitAncestorService(receipt.BaseCommit, receipt.CurrentCommit); err != nil {
		return errors.New("上游 v0.2.3 源码 transition 提交关系非法")
	}
	if err := upstreamV023GitAncestorService(receipt.CurrentCommit, "HEAD"); err != nil {
		return errors.New("上游 v0.2.3 源码 transition 未被当前 HEAD 承接")
	}
	paths := make([]string, 0, len(receipt.Entries))
	for _, entry := range receipt.Entries {
		if err := validateUpstreamV023SourceTransitionEntryService(entry); err != nil {
			return err
		}
		paths = append(paths, entry.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 v0.2.3 源码 transition 路径未严格排序")
	}
	return nil
}

func validateUpstreamV023SourceTransitionEntryService(entry upstreamV023SourceTransitionEntryService) error {
	if strings.TrimSpace(entry.Path) == "" || filepath.IsAbs(filepath.FromSlash(entry.Path)) ||
		strings.HasPrefix(filepath.ToSlash(entry.Path), "../") || strings.TrimSpace(entry.Reason) == "" {
		return errors.New("上游 v0.2.3 源码 transition 条目非法")
	}
	if entry.OldPath != "" && entry.Status != "R" && entry.Status != "C" {
		return errors.New("上游 v0.2.3 源码 transition old_path 非法")
	}
	switch entry.Status {
	case "A":
		if entry.PredecessorSHA256 != nil || !upstreamV023DigestPointerService(entry.CurrentSHA256) {
			return errors.New("上游 v0.2.3 源码 transition 新增条目非法")
		}
	case "D":
		if !upstreamV023DigestPointerService(entry.PredecessorSHA256) || entry.CurrentSHA256 != nil {
			return errors.New("上游 v0.2.3 源码 transition 删除条目非法")
		}
	case "M", "R", "C", "T":
		if !upstreamV023DigestPointerService(entry.PredecessorSHA256) ||
			!upstreamV023DigestPointerService(entry.CurrentSHA256) {
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
	if !auditedSourceCurrentStateAcceptedService(entry.Path, *entry.CurrentSHA256, raw, err) {
		return errors.New("上游 v0.2.3 源码 transition 当前摘要不一致：" + entry.Path)
	}
	return nil
}

func upstreamV023DigestPointerService(value *string) bool {
	return value != nil && validOpenAIReplayOOMRepairServiceSHA(*value)
}

func upstreamV023GitObjectService(value string) bool {
	return len(value) == 40 && strings.Trim(value, "0123456789abcdef") == ""
}

func upstreamV023GitOutputService(arguments ...string) (string, error) {
	command := exec.Command("git", arguments...)
	command.Dir = filepath.Join("../../..")
	raw, err := command.Output()
	return strings.TrimSpace(string(raw)), err
}

func upstreamV023GitAncestorService(before, after string) error {
	command := exec.Command("git", "merge-base", "--is-ancestor", before, after)
	command.Dir = filepath.Join("../../..")
	return command.Run()
}

// service 包独立重放同一 Git 节点，避免 officialegress 包通过而 service 漏检。
func upstreamV023SourceTransitionSupersedesService(path, priorDigest, currentDigest string) bool {
	if !validOpenAIReplayOOMRepairServiceSHA(priorDigest) ||
		!validOpenAIReplayOOMRepairServiceSHA(currentDigest) || priorDigest == currentDigest {
		return false
	}
	if auditedSourceSuccessorReachesService(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamV023PostBootstrapSourceSuccessorSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamV023SourceTransitionDirectSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadUpstreamV023SourceTransitionService()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Entries {
		if entry.Path != path || entry.PredecessorSHA256 == nil || entry.CurrentSHA256 == nil ||
			*entry.CurrentSHA256 != currentDigest {
			continue
		}
		baseDigest := *entry.PredecessorSHA256
		if upstreamV023PriorReachesSourceBaseService(path, priorDigest, baseDigest) {
			return true
		}
	}
	return false
}

func upstreamV023SourceTransitionDirectSupersedesService(path, priorDigest, currentDigest string) bool {
	receipt, err := loadUpstreamV023SourceTransitionService()
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

func upstreamV023PriorReachesSourceBaseService(path, priorDigest, baseDigest string) bool {
	if priorDigest == baseDigest ||
		upstreamMergeFrameworkV3SuccessorSupersedesService(path, priorDigest, baseDigest) ||
		historicalSourceDriftSupersedes(path, priorDigest, baseDigest) ||
		upstreamV023ScannerSuccessorTransitionSupersedesService(path, priorDigest, baseDigest) {
		return true
	}
	if entries, err := loadHistoricalSourceDriftEntries(); err == nil {
		if entry, ok := entries[path]; ok && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.HeadSHA256 == baseDigest ||
				upstreamMergeFrameworkV3SuccessorSupersedesService(path, entry.HeadSHA256, baseDigest) ||
				upstreamV023ScannerSuccessorTransitionSupersedesService(path, entry.HeadSHA256, baseDigest)) {
			return true
		}
	}
	if scanner, err := loadUpstreamV023ScannerSuccessorTransitionService(); err == nil {
		for _, transition := range scanner.Transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				(transition.ToSHA256 == baseDigest ||
					upstreamMergeFrameworkV3SuccessorSupersedesService(path, transition.ToSHA256, baseDigest) ||
					historicalSourceDriftSupersedes(path, transition.ToSHA256, baseDigest)) {
				return true
			}
		}
	}
	return codex0151WorktreeSuccessorEdgeService(path, priorDigest, baseDigest)
}

func TestUpstreamV023SourceTransitionServiceIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV023SourceTransitionService(); err != nil {
		t.Fatal(err)
	}
}

func TestUpstreamV023SourceTransitionServiceRejectsMutation(t *testing.T) {
	receipt, err := loadUpstreamV023SourceTransitionService()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Entries = append([]upstreamV023SourceTransitionEntryService(nil), receipt.Entries...)
	mutated.Entries[0].Path = "../越界"
	if err := validateUpstreamV023SourceTransitionService(mutated); err == nil {
		t.Fatal("变异后的上游 v0.2.3 源码 transition 被错误接受")
	}
}
