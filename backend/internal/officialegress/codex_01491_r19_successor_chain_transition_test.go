package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

const codex01491R19SuccessorChainPath = "docs/egress/maintenance/codex-0.149.1-r19-successor-chain-transition.json"

type codex01491R19SuccessorChainReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	IssuedAtUTC           string `json:"issued_at_utc"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	FrameworkStage        string `json:"framework_stage"`
	PredecessorTransition struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	SuccessorChainContract struct {
		Reason                             string `json:"reason"`
		OfficialRecaptureRequired          bool   `json:"official_recapture_required"`
		ClassificationReapprovalRequired   bool   `json:"classification_reapproval_required"`
		CandidateRecaptureRequired         bool   `json:"candidate_recapture_required"`
		KiloRevalidationRequired           bool   `json:"kilo_revalidation_required"`
		RecursivePredecessorReplayRequired bool   `json:"recursive_predecessor_replay_required"`
		OriginalAttemptDirectoryRequired   bool   `json:"original_attempt_directory_required"`
		RelativePathReinterpretation       bool   `json:"relative_path_reinterpretation_allowed"`
		AccountTransitionContractPreserved bool   `json:"account_transition_contract_preserved"`
	} `json:"successor_chain_contract"`
	Safety struct {
		HistoricalArtifactsOverwritten bool `json:"historical_artifacts_overwritten"`
		HistoricalReceiptsModified     bool `json:"historical_receipts_modified"`
		NetworkConfigurationChanged    bool `json:"network_configuration_changed"`
		DeploymentPerformed            bool `json:"deployment_performed"`
		ProductionSelectorChanged      bool `json:"production_selector_changed"`
		ProductionActivated            bool `json:"production_activated"`
		VircsAccessed                  bool `json:"vircs_accessed"`
	} `json:"safety"`
	PathSetSHA256  string                                     `json:"path_set_sha256"`
	Transitions    []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Result         string                                     `json:"result"`
	IdentitySHA256 string                                     `json:"identity_sha256"`
}

var (
	codex01491R19SuccessorChainOnce   sync.Once
	codex01491R19SuccessorChainCached codex01491R19SuccessorChainReceipt
	codex01491R19SuccessorChainError  error
)

func loadCodex01491R19SuccessorChainTransition() (
	codex01491R19SuccessorChainReceipt,
	error,
) {
	codex01491R19SuccessorChainOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R19SuccessorChainPath)
		if err != nil {
			codex01491R19SuccessorChainError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R19SuccessorChainCached); err != nil {
			codex01491R19SuccessorChainError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R19SuccessorChainError = errors.New("Codex 0.149.1 r19 多级后继 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R19SuccessorChainCached.IdentitySHA256,
		); err != nil {
			codex01491R19SuccessorChainError = err
			return
		}
		codex01491R19SuccessorChainError =
			validateCodex01491R19SuccessorChainTransition(
				codex01491R19SuccessorChainCached,
			)
	})
	return codex01491R19SuccessorChainCached, codex01491R19SuccessorChainError
}

func validateCodex01491R19SuccessorChainTransition(
	receipt codex01491R19SuccessorChainReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r19-successor-chain-transition/v1" ||
		receipt.BaseCommit != "0be1e9c115fc5da8ef8b216ed3b8b0521ace5924" ||
		receipt.Scope != "codex-0.149.1-r19-successor-chain" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "successor_chain_replay_tooling_frozen" {
		return errors.New("Codex 0.149.1 r19 多级后继 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r19 多级后继 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R18SuccessorAccountPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		SchemaVersion  string `json:"schema_version"`
		Scope          string `json:"scope"`
		Result         string `json:"result"`
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if err := codex01491VerifyIdentity(predecessorRaw, predecessor.IdentitySHA256); err != nil {
		return err
	}
	if predecessor.SchemaVersion != "official-client-codex-0.149.1-r18-successor-account-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r18-successor-account" ||
		predecessor.Result != "successor_account_transition_tooling_frozen" ||
		receipt.PredecessorTransition.Path != codex01491R18SuccessorAccountPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r19 多级后继 transition 前序绑定非法")
	}

	contract := receipt.SuccessorChainContract
	if contract.Reason != "successor_chain_origin_resolution" ||
		contract.OfficialRecaptureRequired ||
		contract.ClassificationReapprovalRequired ||
		!contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired ||
		!contract.RecursivePredecessorReplayRequired ||
		!contract.OriginalAttemptDirectoryRequired ||
		contract.RelativePathReinterpretation ||
		!contract.AccountTransitionContractPreserved {
		return errors.New("Codex 0.149.1 r19 多级后继递归合同非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.DeploymentPerformed ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.ProductionActivated ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r19 多级后继安全边界非法")
	}

	expectedPaths := []string{
		"backend/internal/officialegress/codex_01491_r18_successor_account_transition_test.go",
		"backend/internal/officialegress/codex_01491_r19_successor_chain_transition_test.go",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r18_successor_account_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r19_successor_chain_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r19 多级后继 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 r19 多级后继 transition 条目非法：" + entry.Path)
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 r19 多级后继 transition 新增条目非法：" + entry.Path)
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 r19 多级后继 transition 修改条目非法：" + entry.Path)
		}
		for _, predecessorSHA := range entry.PredecessorSHA256s {
			if len(predecessorSHA) != 64 || predecessorSHA == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 r19 多级后继 transition 前序摘要非法：" + entry.Path)
			}
		}
		if strings.HasPrefix(entry.Path, "docs/egress/maintenance/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/catalogdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/profilecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/releasecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "docs/egress/lifecycle/migration-artifacts/") {
			return errors.New("Codex 0.149.1 r19 多级后继 transition 越过历史只读边界：" + entry.Path)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R20CandidateAuxSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r19 多级后继 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r19 多级后继 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R19SuccessorChainSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R19SuccessorChainTransition()
	if err != nil {
		return false
	}
	if codex01491R20CandidateAuxSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.ToSHA256 == currentDigest ||
				codex01491R20CandidateAuxSupersedes(
					path,
					entry.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestCodex01491R19SuccessorChainTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R19SuccessorChainTransition()
	if err != nil {
		t.Fatal(err)
	}
	var modified codex01491CandidateSourceTransitionEntry
	for _, entry := range receipt.Transitions {
		if entry.Change == "modified" {
			modified = entry
			break
		}
	}
	if modified.Path == "" || !codex01491R19SuccessorChainSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r19 多级后继 transition 未承认精确后继边")
	}
	if codex01491R19SuccessorChainSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r19 多级后继 transition 接受了未知前序摘要")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491R19SuccessorChainTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r19 多级后继 transition 接受了网络配置变更")
	}
}
