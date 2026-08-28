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

const codex01491R21ClassificationFactCorrectionPath = "docs/egress/maintenance/codex-0.149.1-r21-classification-fact-correction-transition.json"

type codex01491R21ClassificationFactCorrectionReceipt struct {
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
	ClassificationFactCorrectionContract struct {
		Reason                           string `json:"reason"`
		PredecessorImportSchema          string `json:"predecessor_import_schema"`
		ImportMode                       string `json:"import_mode"`
		OfficialRecaptureRequired        bool   `json:"official_recapture_required"`
		OfficialEvidenceReplayRequired   bool   `json:"official_evidence_replay_required"`
		ApprovedClassificationImported   bool   `json:"approved_classification_imported"`
		ClassificationReapprovalRequired bool   `json:"classification_reapproval_required"`
		HistoricalScenarioBindingScope   string `json:"historical_scenario_source_binding_scope"`
		ApprovedScenarioRebindRequired   bool   `json:"approved_scenario_rebind_required"`
		CandidateRecaptureRequired       bool   `json:"candidate_recapture_required"`
		KiloRevalidationRequired         bool   `json:"kilo_revalidation_required"`
	} `json:"classification_fact_correction_contract"`
	CorrectedFact struct {
		RuleID                 string   `json:"rule_id"`
		Transport              string   `json:"transport"`
		OfficialEvidence       string   `json:"official_evidence"`
		ColdStartCookiePresent bool     `json:"cold_start_cookie_present"`
		LiteHeaderSlot         int      `json:"lite_header_slot"`
		RoutingHintSlot        int      `json:"routing_hint_slot"`
		RequiredOrder          []string `json:"required_order"`
	} `json:"corrected_fact"`
	ProfileTransition struct {
		TargetVersion                string `json:"target_version"`
		PredecessorProfileDigest     string `json:"predecessor_profile_digest"`
		SuccessorProfileID           string `json:"successor_profile_id"`
		SuccessorProfileDigest       string `json:"successor_profile_digest"`
		HistoricalProfileOverwritten bool   `json:"historical_profile_overwritten"`
		CatalogActivationPerformed   bool   `json:"catalog_activation_performed"`
	} `json:"profile_transition"`
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
	codex01491R21ClassificationFactCorrectionOnce   sync.Once
	codex01491R21ClassificationFactCorrectionCached codex01491R21ClassificationFactCorrectionReceipt
	codex01491R21ClassificationFactCorrectionError  error
)

func loadCodex01491R21ClassificationFactCorrectionTransition() (
	codex01491R21ClassificationFactCorrectionReceipt,
	error,
) {
	codex01491R21ClassificationFactCorrectionOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R21ClassificationFactCorrectionPath)
		if err != nil {
			codex01491R21ClassificationFactCorrectionError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R21ClassificationFactCorrectionCached); err != nil {
			codex01491R21ClassificationFactCorrectionError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R21ClassificationFactCorrectionError = errors.New("Codex 0.149.1 r21 分类事实纠正 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R21ClassificationFactCorrectionCached.IdentitySHA256,
		); err != nil {
			codex01491R21ClassificationFactCorrectionError = err
			return
		}
		codex01491R21ClassificationFactCorrectionError =
			validateCodex01491R21ClassificationFactCorrectionTransition(
				codex01491R21ClassificationFactCorrectionCached,
			)
	})
	return codex01491R21ClassificationFactCorrectionCached,
		codex01491R21ClassificationFactCorrectionError
}

func validateCodex01491R21ClassificationFactCorrectionTransition(
	receipt codex01491R21ClassificationFactCorrectionReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r21-classification-fact-correction-transition/v1" ||
		receipt.BaseCommit != "3825f879b39e5f9aeb3e175d59cfc781b3f25ecf" ||
		receipt.Scope != "codex-0.149.1-r21-classification-fact-correction" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "classification_fact_correction_tooling_frozen" {
		return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R20CandidateAuxPath)
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
	if predecessor.SchemaVersion != "official-client-codex-0.149.1-r20-candidate-aux-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r20-candidate-aux" ||
		predecessor.Result != "candidate_aux_capture_tooling_frozen" ||
		receipt.PredecessorTransition.Path != codex01491R20CandidateAuxPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 前序绑定非法")
	}

	contract := receipt.ClassificationFactCorrectionContract
	if contract.Reason != "classification_fact_correction" ||
		contract.PredecessorImportSchema != "codex-upgrade-predecessor-import/v3" ||
		contract.ImportMode != "official_only_reclassification" ||
		contract.OfficialRecaptureRequired ||
		!contract.OfficialEvidenceReplayRequired ||
		contract.ApprovedClassificationImported ||
		!contract.ClassificationReapprovalRequired ||
		contract.HistoricalScenarioBindingScope != "successor_plan_rebuild_only" ||
		!contract.ApprovedScenarioRebindRequired ||
		!contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired {
		return errors.New("Codex 0.149.1 r21 分类事实纠正承接合同非法")
	}
	if receipt.CorrectedFact.RuleID != "SPEC-H1-004" ||
		receipt.CorrectedFact.Transport != "responses_http" ||
		receipt.CorrectedFact.OfficialEvidence != "c1491-r14-f-lite-http-response/relay/conn005.client_to_upstream.bin" ||
		receipt.CorrectedFact.ColdStartCookiePresent ||
		receipt.CorrectedFact.LiteHeaderSlot != 60 ||
		receipt.CorrectedFact.RoutingHintSlot != 65 ||
		!slices.Equal(receipt.CorrectedFact.RequiredOrder, []string{
			"x-codex-turn-metadata",
			"x-openai-internal-codex-responses-lite",
			"x-codex-routing-hint",
			"x-client-request-id",
		}) {
		return errors.New("Codex 0.149.1 r21 SPEC-H1-004 纠正事实非法")
	}
	if receipt.ProfileTransition.TargetVersion != "0.149.1" ||
		receipt.ProfileTransition.PredecessorProfileDigest != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" ||
		receipt.ProfileTransition.SuccessorProfileID != "codex-0.149.1-official-r1491-v2" ||
		receipt.ProfileTransition.SuccessorProfileDigest != "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3" ||
		receipt.ProfileTransition.HistoricalProfileOverwritten ||
		receipt.ProfileTransition.CatalogActivationPerformed {
		return errors.New("Codex 0.149.1 r21 画像追加式过渡非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.DeploymentPerformed ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.ProductionActivated ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r21 分类事实纠正安全边界非法")
	}

	expectedPaths := []string{
		"backend/internal/officialegress/codex_01491_r20_candidate_aux_transition_test.go",
		"backend/internal/officialegress/codex_01491_r21_classification_fact_correction_transition_test.go",
		"docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
		"tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json",
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/codex_upgrade_evidence_labels_0_149_1.json",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/run_candidate_core_capture.sh",
		"tools/official_client_capture/tests/test_build_evidence_catalog.py",
		"tools/official_client_capture/tests/test_candidate_core_capture.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r20_candidate_aux_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r21_classification_fact_correction_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
		"tools/spec_ref_anchors_0_149_1.json",
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 条目非法：" + entry.Path)
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 新增条目非法：" + entry.Path)
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 修改条目非法：" + entry.Path)
		}
		for _, predecessorSHA := range entry.PredecessorSHA256s {
			if len(predecessorSHA) != 64 || predecessorSHA == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 前序摘要非法：" + entry.Path)
			}
		}
		if strings.HasPrefix(entry.Path, "docs/egress/maintenance/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/catalogdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/profilecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "backend/internal/officialegress/releasecontract/testdata/") ||
			strings.HasPrefix(entry.Path, "docs/egress/lifecycle/migration-artifacts/") {
			return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 越过历史只读边界：" + entry.Path)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R22CandidateCatalogSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r21 分类事实纠正 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R21ClassificationFactCorrectionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R21ClassificationFactCorrectionTransition()
	if err != nil {
		return false
	}
	if codex01491R22CandidateCatalogSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.ToSHA256 == currentDigest ||
				codex01491R22CandidateCatalogSupersedes(
					path,
					entry.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestCodex01491R21ClassificationFactCorrectionTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R21ClassificationFactCorrectionTransition()
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
	if modified.Path == "" || !codex01491R21ClassificationFactCorrectionSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r21 分类事实纠正 transition 未承认精确后继边")
	}
	if codex01491R21ClassificationFactCorrectionSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r21 分类事实纠正 transition 接受了未知前序摘要")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491R21ClassificationFactCorrectionTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r21 分类事实纠正 transition 接受了网络配置变更")
	}
}
