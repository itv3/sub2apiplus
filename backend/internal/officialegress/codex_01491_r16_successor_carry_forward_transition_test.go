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

const codex01491R16SuccessorCarryForwardPath = "docs/egress/maintenance/codex-0.149.1-r16-successor-carry-forward-transition.json"

type codex01491R16SuccessorCarryForwardReceipt struct {
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
	CarryForwardContract struct {
		Reason                           string   `json:"reason"`
		OfficialRecaptureRequired        bool     `json:"official_recapture_required"`
		ClassificationReapprovalRequired bool     `json:"classification_reapproval_required"`
		CandidateRecaptureRequired       bool     `json:"candidate_recapture_required"`
		KiloRevalidationRequired         bool     `json:"kilo_revalidation_required"`
		ReplayOn                         []string `json:"replay_on"`
		ReplayedFacts                    []string `json:"replayed_facts"`
	} `json:"carry_forward_contract"`
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
	codex01491R16SuccessorCarryForwardOnce   sync.Once
	codex01491R16SuccessorCarryForwardCached codex01491R16SuccessorCarryForwardReceipt
	codex01491R16SuccessorCarryForwardError  error
)

func loadCodex01491R16SuccessorCarryForwardTransition() (
	codex01491R16SuccessorCarryForwardReceipt,
	error,
) {
	codex01491R16SuccessorCarryForwardOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R16SuccessorCarryForwardPath)
		if err != nil {
			codex01491R16SuccessorCarryForwardError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R16SuccessorCarryForwardCached); err != nil {
			codex01491R16SuccessorCarryForwardError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R16SuccessorCarryForwardError = errors.New("Codex 0.149.1 r16 后继承接 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R16SuccessorCarryForwardCached.IdentitySHA256,
		); err != nil {
			codex01491R16SuccessorCarryForwardError = err
			return
		}
		codex01491R16SuccessorCarryForwardError =
			validateCodex01491R16SuccessorCarryForwardTransition(
				codex01491R16SuccessorCarryForwardCached,
			)
	})
	return codex01491R16SuccessorCarryForwardCached, codex01491R16SuccessorCarryForwardError
}

func validateCodex01491R16SuccessorCarryForwardTransition(
	receipt codex01491R16SuccessorCarryForwardReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r16-successor-carry-forward-transition/v1" ||
		receipt.BaseCommit != "972c7e6e3fd2e9577cb8d1a3e7660b776d8a6ecd" ||
		receipt.Scope != "codex-0.149.1-r16-successor-carry-forward" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "successor_carry_forward_tooling_frozen" {
		return errors.New("Codex 0.149.1 r16 后继承接 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r16 后继承接 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R15FormalClassificationPath)
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
	if predecessor.SchemaVersion != "official-client-codex-0.149.1-r15-formal-classification-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r15-formal-classification" ||
		predecessor.Result != "formal_classification_catalog_staged_transition_frozen" ||
		receipt.PredecessorTransition.Path != codex01491R15FormalClassificationPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r16 后继承接 transition 前序绑定非法")
	}

	contract := receipt.CarryForwardContract
	if contract.Reason != "candidate_runtime_identity_correction" ||
		contract.OfficialRecaptureRequired ||
		contract.ClassificationReapprovalRequired ||
		!contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired ||
		!slices.Equal(contract.ReplayOn, []string{"accept", "compare", "status"}) ||
		!slices.Equal(contract.ReplayedFacts, []string{
			"approved_classification_five_files",
			"official_evidence_inventory",
			"official_security_gate",
			"official_stage_seal",
			"predecessor_campaign_manifest",
		}) {
		return errors.New("Codex 0.149.1 r16 后继承接合同非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.DeploymentPerformed ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.ProductionActivated ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r16 后继承接安全边界非法")
	}

	expectedPaths := []string{
		"backend/internal/officialegress/codex_01491_r15_formal_classification_transition_test.go",
		"backend/internal/officialegress/codex_01491_r16_successor_carry_forward_transition_test.go",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/codex_upgrade_campaign.schema.json",
		"tools/official_client_capture/codex_upgrade_predecessor_import.schema.json",
		"tools/official_client_capture/codex_upgrade_stage_result.schema.json",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r15_formal_classification_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r16_successor_carry_forward_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r16 后继承接 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 r16 后继承接 transition 条目非法：" + entry.Path)
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 r16 后继承接 transition 新增条目非法：" + entry.Path)
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 r16 后继承接 transition 修改条目非法：" + entry.Path)
		}
		for _, predecessorSHA := range entry.PredecessorSHA256s {
			if len(predecessorSHA) != 64 || predecessorSHA == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 r16 后继承接 transition 前序摘要非法：" + entry.Path)
			}
		}
		if strings.HasPrefix(entry.Path, "docs/egress/maintenance/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/catalogdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/profilecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/releasecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "docs/egress/lifecycle/migration-artifacts/") {
			return errors.New("Codex 0.149.1 r16 后继承接 transition 越过历史只读边界：" + entry.Path)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r16 后继承接 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r16 后继承接 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R16SuccessorCarryForwardSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R16SuccessorCarryForwardTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && entry.ToSHA256 == currentDigest &&
			slices.Contains(entry.PredecessorSHA256s, priorDigest) {
			return true
		}
	}
	return false
}

func TestCodex01491R16SuccessorCarryForwardTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R16SuccessorCarryForwardTransition()
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
	if modified.Path == "" || !codex01491R16SuccessorCarryForwardSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r16 后继承接 transition 未承认精确后继边")
	}
	if codex01491R16SuccessorCarryForwardSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r16 后继承接 transition 接受了未知前序摘要")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491R16SuccessorCarryForwardTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r16 后继承接 transition 接受了网络配置变更")
	}
}
