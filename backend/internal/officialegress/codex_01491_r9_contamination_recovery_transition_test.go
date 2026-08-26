package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"
	"time"
)

const (
	codex01491R9ContaminationRecoveryPath = "docs/egress/maintenance/codex-0.149.1-r9-contamination-recovery-transition.json"
	codex01491ModelCatalogH1SuccessorPath = "docs/egress/maintenance/codex-0.149.1-model-catalog-h1-successor-transition.json"
)

type codex01491R9ContaminationRecoveryReceipt struct {
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
	FailedCandidateAttempt struct {
		CampaignID     string `json:"campaign_id"`
		CandidateID    string `json:"candidate_id"`
		AttemptID      string `json:"attempt_id"`
		Status         string `json:"status"`
		ReuseForbidden bool   `json:"reuse_forbidden"`
	} `json:"failed_candidate_attempt"`
	Boundaries struct {
		APIKeyRef                   string `json:"api_key_ref"`
		CaptureAccountRef           string `json:"capture_account_ref"`
		CaptureAccount20Allowed     bool   `json:"capture_account_20_allowed"`
		CaptureAccount20Used        bool   `json:"capture_account_20_used"`
		CodexOnlySelectedAccountIDs []int  `json:"codex_only_selected_account_ids"`
		NewCampaignRequired         bool   `json:"new_campaign_required"`
		ProductionSelectorChanged   bool   `json:"production_selector_changed"`
		VircsAccessed               bool   `json:"vircs_accessed"`
	} `json:"boundaries"`
	RepairFacts struct {
		CaptureCLICandidateNetworkRequired string `json:"capture_cli_candidate_network_required"`
		CaptureCLIDNSFailureDetected       bool   `json:"capture_cli_dns_failure_detected"`
		CaptureCLINetworkRepairPerformed   bool   `json:"capture_cli_network_repair_performed"`
		CodexOnlyMatrixTouchesClaude       bool   `json:"codex_only_matrix_touches_claude_account"`
		DynamicAccountStateRestoration     bool   `json:"dynamic_account_state_restoration"`
		HistoricalEvidencePreserved        bool   `json:"historical_evidence_preserved_read_only"`
		SchedulableStateOrderNormalized    bool   `json:"schedulable_state_order_normalized"`
	} `json:"repair_facts"`
	Transitions  []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification struct {
		BashSyntaxPassed         bool `json:"bash_syntax_passed"`
		CaptureToolTestsPassed   bool `json:"capture_tool_tests_passed"`
		EgressSpecPassed         bool `json:"egress_spec_passed"`
		H1SuccessorChainReplayed bool `json:"h1_successor_chain_replayed"`
		TargetedTestsPassed      bool `json:"targeted_tests_passed"`
	} `json:"verification"`
	Safety struct {
		ActiveRemained01470                bool `json:"active_remained_0_147_0"`
		ARM64DeploymentPerformed           bool `json:"arm64_deployment_performed"`
		HistoricalArtifactsModified        bool `json:"historical_artifacts_modified"`
		PreviousRemained01491              bool `json:"previous_remained_0_149_1"`
		ProductionActivationReceiptCreated bool `json:"production_activation_receipt_created"`
		ProductionSelectorChanged          bool `json:"production_selector_changed"`
		VircsAccessed                      bool `json:"vircs_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

func codex01491R9ContaminationRecoveryExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go",
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
		"backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go",
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
		"tools/official_client_capture/run_sub2api_direct_matrix.sh",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r9_contamination_recovery_transition.py",
		"tools/official_client_capture/tests/test_direct_matrix_account_selection.py",
	}
}

func loadCodex01491ModelCatalogH1SuccessorTransitions() (
	[]codex01491CandidateSourceTransitionEntry,
	error,
) {
	raw, err := codex01491RepoFile(codex01491ModelCatalogH1SuccessorPath)
	if err != nil {
		return nil, err
	}
	if upstreamMergeFrameworkDigest(raw) != "e97b35438e82f4ee7adb905eadbb5483c035cd387b144e00d39c63321988bba6" {
		return nil, errors.New("Codex 0.149.1 模型目录 H1 后继 transition 文件摘要非法")
	}
	var receipt struct {
		SchemaVersion  string                                     `json:"schema_version"`
		Scope          string                                     `json:"scope"`
		Transitions    []codex01491CandidateSourceTransitionEntry `json:"transitions"`
		IdentitySHA256 string                                     `json:"identity_sha256"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return nil, err
	}
	if receipt.SchemaVersion != "official-client-codex-0.149.1-model-catalog-h1-successor-transition/v1" ||
		receipt.Scope != "codex-0.149.1-model-catalog-h1-successor" ||
		receipt.IdentitySHA256 != "688aa12024280bda592e54694a539ee32ac79a1db393a69032e212c8fc1b3217" {
		return nil, errors.New("Codex 0.149.1 模型目录 H1 后继 transition 身份非法")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
		return nil, err
	}
	return receipt.Transitions, nil
}

func loadCodex01491R9ContaminationRecoveryTransition() (
	codex01491R9ContaminationRecoveryReceipt,
	error,
) {
	var receipt codex01491R9ContaminationRecoveryReceipt
	raw, err := codex01491RepoFile(codex01491R9ContaminationRecoveryPath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r9 污染恢复 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	return receipt, validateCodex01491R9ContaminationRecoveryTransition(receipt)
}

func validateCodex01491R9ContaminationRecoveryTransition(
	receipt codex01491R9ContaminationRecoveryReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r9-contamination-recovery-transition/v1" ||
		receipt.BaseCommit != "620c3c8d1f7e7dd6734cb961cdb2b2904799974e" ||
		receipt.Scope != "codex-0.149.1-r9-contamination-recovery" ||
		receipt.FrameworkStage != "VC-0/P0-R9-CONTAMINATION-RECOVERY" ||
		receipt.Result != "r10_campaign_required_with_repaired_tooling" {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491ModelCatalogH1SuccessorPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491ModelCatalogH1SuccessorPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 前序绑定非法")
	}
	if receipt.FailedCandidateAttempt.CampaignID != "codex-01491-r9" ||
		receipt.FailedCandidateAttempt.CandidateID != "codex-01491-r9-93d179469-arm64-21" ||
		receipt.FailedCandidateAttempt.AttemptID != "20260826T185433Z-081e83190732044e" ||
		receipt.FailedCandidateAttempt.Status != "environment_contaminated" ||
		!receipt.FailedCandidateAttempt.ReuseForbidden {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition Attempt 事实非法")
	}
	if receipt.Boundaries.APIKeyRef != "#4" || receipt.Boundaries.CaptureAccountRef != "#21" ||
		receipt.Boundaries.CaptureAccount20Allowed || receipt.Boundaries.CaptureAccount20Used ||
		!slices.Equal(receipt.Boundaries.CodexOnlySelectedAccountIDs, []int{21}) ||
		!receipt.Boundaries.NewCampaignRequired || receipt.Boundaries.ProductionSelectorChanged ||
		receipt.Boundaries.VircsAccessed {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 账号或生产边界非法")
	}
	if receipt.RepairFacts.CaptureCLICandidateNetworkRequired != "sub2apiplus_sub2api-network" ||
		!receipt.RepairFacts.CaptureCLIDNSFailureDetected || receipt.RepairFacts.CaptureCLINetworkRepairPerformed ||
		receipt.RepairFacts.CodexOnlyMatrixTouchesClaude || !receipt.RepairFacts.DynamicAccountStateRestoration ||
		!receipt.RepairFacts.HistoricalEvidencePreserved || !receipt.RepairFacts.SchedulableStateOrderNormalized {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 修复事实非法")
	}
	if !receipt.Verification.BashSyntaxPassed || !receipt.Verification.CaptureToolTestsPassed ||
		!receipt.Verification.EgressSpecPassed || !receipt.Verification.H1SuccessorChainReplayed ||
		!receipt.Verification.TargetedTestsPassed {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 门禁未闭合")
	}
	if !receipt.Safety.ActiveRemained01470 || receipt.Safety.ARM64DeploymentPerformed ||
		receipt.Safety.HistoricalArtifactsModified || !receipt.Safety.PreviousRemained01491 ||
		receipt.Safety.ProductionActivationReceiptCreated || receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 安全边界非法")
	}

	expectedPaths := codex01491R9ContaminationRecoveryExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go":         {"1d2cec832db7fc457516a70863431f5207cf31dea6b9cd5e9dabb8f379ac2206"},
		"backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go":                {"ac51a0742fef4ac120705a91bdbf5267180ce4c6e913314e6311239d0825fd6e"},
		"tools/official_client_capture/run_sub2api_direct_matrix.sh":                                  {"9747ca92e20b9cdaf36f115f7d37d9a675c9e92a9ee56760ee314314bda31158"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"96482eee11a088a5c7f41127cd2c7f331e78cf198661fc6ebe00e46eafa81e94"},
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py":     {"c6ddc9a5c89e191a0b6896da681f17696c4055fcc33a23305187b0bd3d3d0874"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r9 污染恢复 transition 路径数量非法")
	}
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		predecessors := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(predecessors) > 0 {
			expectedChange = "modified"
		}
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, predecessors) || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" {
			return errors.New("Codex 0.149.1 r9 污染恢复 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r9 污染恢复 transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R9ContaminationRecoverySupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R9ContaminationRecoveryTransition()
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

func TestCodex01491R9ContaminationRecoveryTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R9ContaminationRecoveryTransition()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Boundaries.CaptureAccountRef = "#20"
	if err := validateCodex01491R9ContaminationRecoveryTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r9 污染恢复 transition 接受了账号 #20")
	}
}
