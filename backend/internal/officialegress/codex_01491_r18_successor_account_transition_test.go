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

const codex01491R18SuccessorAccountPath = "docs/egress/maintenance/codex-0.149.1-r18-successor-account-transition.json"

type codex01491R18SuccessorAccountReceipt struct {
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
	AccountTransitionContract struct {
		Reason                            string `json:"reason"`
		OfficialRecaptureRequired         bool   `json:"official_recapture_required"`
		ClassificationReapprovalRequired  bool   `json:"classification_reapproval_required"`
		CandidateRecaptureRequired        bool   `json:"candidate_recapture_required"`
		KiloRevalidationRequired          bool   `json:"kilo_revalidation_required"`
		SuccessorAccountMustBeExplicit    bool   `json:"successor_account_must_be_explicit"`
		PredecessorImportSchema           string `json:"predecessor_import_schema"`
		HistoricalPredecessorImportSchema string `json:"historical_predecessor_import_schema"`
		OnlyMutableConfigurationField     string `json:"only_mutable_configuration_field"`
		ConfigurationTransitionReason     string `json:"configuration_transition_reason"`
	} `json:"account_transition_contract"`
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
	codex01491R18SuccessorAccountOnce   sync.Once
	codex01491R18SuccessorAccountCached codex01491R18SuccessorAccountReceipt
	codex01491R18SuccessorAccountError  error
)

func loadCodex01491R18SuccessorAccountTransition() (
	codex01491R18SuccessorAccountReceipt,
	error,
) {
	codex01491R18SuccessorAccountOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R18SuccessorAccountPath)
		if err != nil {
			codex01491R18SuccessorAccountError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R18SuccessorAccountCached); err != nil {
			codex01491R18SuccessorAccountError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R18SuccessorAccountError = errors.New("Codex 0.149.1 r18 后继账号 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R18SuccessorAccountCached.IdentitySHA256,
		); err != nil {
			codex01491R18SuccessorAccountError = err
			return
		}
		codex01491R18SuccessorAccountError =
			validateCodex01491R18SuccessorAccountTransition(
				codex01491R18SuccessorAccountCached,
			)
	})
	return codex01491R18SuccessorAccountCached, codex01491R18SuccessorAccountError
}

func validateCodex01491R18SuccessorAccountTransition(
	receipt codex01491R18SuccessorAccountReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r18-successor-account-transition/v1" ||
		receipt.BaseCommit != "fd64c83e7fc4b9f3094ac3d9f96c2fbd89c47fce" ||
		receipt.Scope != "codex-0.149.1-r18-successor-account" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "successor_account_transition_tooling_frozen" {
		return errors.New("Codex 0.149.1 r18 后继账号 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r18 后继账号 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R17KiloModelContractPath)
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
	if predecessor.SchemaVersion != "official-client-codex-0.149.1-r17-kilo-model-contract-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r17-kilo-model-contract" ||
		predecessor.Result != "kilo_model_contract_tooling_frozen" ||
		receipt.PredecessorTransition.Path != codex01491R17KiloModelContractPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r18 后继账号 transition 前序绑定非法")
	}

	contract := receipt.AccountTransitionContract
	if contract.Reason != "successor_runtime_account_rotation" ||
		contract.OfficialRecaptureRequired ||
		contract.ClassificationReapprovalRequired ||
		!contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired ||
		!contract.SuccessorAccountMustBeExplicit ||
		contract.PredecessorImportSchema != "codex-upgrade-predecessor-import/v2" ||
		contract.HistoricalPredecessorImportSchema != "codex-upgrade-predecessor-import/v1" ||
		contract.OnlyMutableConfigurationField != "codex_account_id" ||
		contract.ConfigurationTransitionReason != "operator_selected_active_account" {
		return errors.New("Codex 0.149.1 r18 后继账号合同非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.DeploymentPerformed ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.ProductionActivated ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r18 后继账号安全边界非法")
	}

	expectedPaths := []string{
		"backend/internal/officialegress/codex_01491_r17_kilo_model_contract_transition_test.go",
		"backend/internal/officialegress/codex_01491_r18_successor_account_transition_test.go",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r17_kilo_model_contract_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r18_successor_account_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r18 后继账号 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 r18 后继账号 transition 条目非法：" + entry.Path)
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 r18 后继账号 transition 新增条目非法：" + entry.Path)
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 r18 后继账号 transition 修改条目非法：" + entry.Path)
		}
		for _, predecessorSHA := range entry.PredecessorSHA256s {
			if len(predecessorSHA) != 64 || predecessorSHA == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 r18 后继账号 transition 前序摘要非法：" + entry.Path)
			}
		}
		if strings.HasPrefix(entry.Path, "docs/egress/maintenance/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/catalogdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/profilecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/releasecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "docs/egress/lifecycle/migration-artifacts/") {
			return errors.New("Codex 0.149.1 r18 后继账号 transition 越过历史只读边界：" + entry.Path)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R19SuccessorChainSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r18 后继账号 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r18 后继账号 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R18SuccessorAccountSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R18SuccessorAccountTransition()
	if err != nil {
		return false
	}
	if codex01491R19SuccessorChainSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.ToSHA256 == currentDigest ||
				codex01491R19SuccessorChainSupersedes(
					path,
					entry.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestCodex01491R18SuccessorAccountTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R18SuccessorAccountTransition()
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
	if modified.Path == "" || !codex01491R18SuccessorAccountSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r18 后继账号 transition 未承认精确后继边")
	}
	if codex01491R18SuccessorAccountSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r18 后继账号 transition 接受了未知前序摘要")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491R18SuccessorAccountTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r18 后继账号 transition 接受了网络配置变更")
	}
}
