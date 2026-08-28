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

const codex01491R24SelectorLiteCoordinatePath = "docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json"

type codex01491R24SelectorLiteCoordinateReceipt struct {
	SchemaVersion  string `json:"schema_version"`
	IssuedAtUTC    string `json:"issued_at_utc"`
	BaseCommit     string `json:"base_commit"`
	Scope          string `json:"scope"`
	FrameworkStage string `json:"framework_stage"`
	Predecessor    struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	CorrectionContract struct {
		Reason                           string `json:"reason"`
		PredecessorImportSchema          string `json:"predecessor_import_schema"`
		ImportMode                       string `json:"import_mode"`
		OfficialRecaptureRequired        bool   `json:"official_recapture_required"`
		OfficialEvidenceReplayRequired   bool   `json:"official_evidence_replay_required"`
		ApprovedClassificationImported   bool   `json:"approved_classification_imported"`
		ClassificationReapprovalRequired bool   `json:"classification_reapproval_required"`
		ApprovedScenarioRebindRequired   bool   `json:"approved_scenario_rebind_required"`
		CandidateRecaptureRequired       bool   `json:"candidate_recapture_required"`
		KiloRevalidationRequired         bool   `json:"kilo_revalidation_required"`
	} `json:"correction_contract"`
	CorrectedCoordinates struct {
		AuxiliaryJobID                   string   `json:"auxiliary_job_id"`
		AuxiliaryTrack                   string   `json:"auxiliary_track"`
		AuxiliaryModelID                 string   `json:"auxiliary_model_id"`
		AuxiliaryExpectedResponsesLite   bool     `json:"auxiliary_expected_use_responses_lite"`
		AuxiliaryRequiredModelReceipt    bool     `json:"auxiliary_required_model_receipt"`
		SessionHeaderRuleID              string   `json:"session_header_rule_id"`
		SessionHeaderCheckIDs            []string `json:"session_header_check_ids"`
		SessionHeaderAllowedPaths        []string `json:"session_header_allowed_paths"`
		AuxiliaryPostsExcludedFromHeader bool     `json:"auxiliary_posts_excluded_from_session_header_scope"`
	} `json:"corrected_coordinates"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		ScenarioCoordinateTested bool `json:"scenario_coordinate_tested"`
		SelectorScopeTested      bool `json:"selector_scope_tested"`
		AnalyticsExclusionTested bool `json:"analytics_exclusion_tested"`
		MutationTestsRequired    bool `json:"mutation_tests_required"`
	} `json:"verification"`
	Safety struct {
		HistoricalArtifactsOverwritten bool `json:"historical_artifacts_overwritten"`
		HistoricalReceiptsModified     bool `json:"historical_receipts_modified"`
		OfficialRecapturePerformed     bool `json:"official_recapture_performed"`
		CandidateCapturePerformed      bool `json:"candidate_capture_performed"`
		DeploymentPerformed            bool `json:"deployment_performed"`
		NetworkConfigurationChanged    bool `json:"network_configuration_changed"`
		ProductionSelectorChanged      bool `json:"production_selector_changed"`
		ProductionActivated            bool `json:"production_activated"`
		VircsAccessed                  bool `json:"vircs_accessed"`
		DMITServerAccessed             bool `json:"dmit_server_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491R24SelectorLiteCoordinateOnce   sync.Once
	codex01491R24SelectorLiteCoordinateCached codex01491R24SelectorLiteCoordinateReceipt
	codex01491R24SelectorLiteCoordinateError  error
)

func codex01491R24SelectorLiteCoordinateExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r23_runtime_coordinate_transition_test.go",
		"backend/internal/officialegress/codex_01491_r24_selector_lite_coordinate_transition_test.go",
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/tests/test_candidate_rule_assertion.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r23_runtime_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r24_selector_lite_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
}

func loadCodex01491R24SelectorLiteCoordinateTransition() (
	codex01491R24SelectorLiteCoordinateReceipt,
	error,
) {
	codex01491R24SelectorLiteCoordinateOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R24SelectorLiteCoordinatePath)
		if err != nil {
			codex01491R24SelectorLiteCoordinateError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R24SelectorLiteCoordinateCached); err != nil {
			codex01491R24SelectorLiteCoordinateError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R24SelectorLiteCoordinateError = errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R24SelectorLiteCoordinateCached.IdentitySHA256,
		); err != nil {
			codex01491R24SelectorLiteCoordinateError = err
			return
		}
		codex01491R24SelectorLiteCoordinateError =
			validateCodex01491R24SelectorLiteCoordinateTransition(
				codex01491R24SelectorLiteCoordinateCached,
			)
	})
	return codex01491R24SelectorLiteCoordinateCached,
		codex01491R24SelectorLiteCoordinateError
}

func validateCodex01491R24SelectorLiteCoordinateTransition(
	receipt codex01491R24SelectorLiteCoordinateReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r24-selector-lite-coordinate-transition/v1" ||
		receipt.BaseCommit != "8d8252c519663a7165a0258ee1e97c4159751282" ||
		receipt.Scope != "codex-0.149.1-r24-selector-lite-coordinate" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "selector_lite_coordinate_successor_tooling_frozen" {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R23RuntimeCoordinatePath)
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
	if receipt.Predecessor.Path != codex01491R23RuntimeCoordinatePath ||
		receipt.Predecessor.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.Predecessor.IdentitySHA256 != predecessor.IdentitySHA256 ||
		predecessor.SchemaVersion != "official-client-codex-0.149.1-r23-runtime-coordinate-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r23-runtime-coordinate" ||
		predecessor.Result != "runtime_coordinate_successor_tooling_frozen" {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 前序绑定非法")
	}

	contract := receipt.CorrectionContract
	if contract.Reason != "classification_fact_correction" ||
		contract.PredecessorImportSchema != "codex-upgrade-predecessor-import/v3" ||
		contract.ImportMode != "official_only_reclassification" ||
		contract.OfficialRecaptureRequired ||
		!contract.OfficialEvidenceReplayRequired ||
		contract.ApprovedClassificationImported ||
		!contract.ClassificationReapprovalRequired ||
		!contract.ApprovedScenarioRebindRequired ||
		!contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标纠正合同非法")
	}
	coordinates := receipt.CorrectedCoordinates
	if coordinates.AuxiliaryJobID != "candidate-frozen-aux" ||
		coordinates.AuxiliaryTrack != "lite" ||
		coordinates.AuxiliaryModelID != "{lite_model}" ||
		!coordinates.AuxiliaryExpectedResponsesLite ||
		coordinates.AuxiliaryRequiredModelReceipt ||
		coordinates.SessionHeaderRuleID != "SPEC-HDR-007" ||
		!slices.Equal(coordinates.SessionHeaderCheckIDs, []string{
			"responses-session-id",
			"responses-thread-id",
		}) || !slices.Equal(coordinates.SessionHeaderAllowedPaths, []string{
		"/backend-api/codex/responses",
		"/backend-api/codex/responses/compact",
	}) || !coordinates.AuxiliaryPostsExcludedFromHeader {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标纠正事实非法")
	}
	verification := receipt.Verification
	if !verification.ScenarioCoordinateTested || !verification.SelectorScopeTested ||
		!verification.AnalyticsExclusionTested || !verification.MutationTestsRequired {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标验证事实非法")
	}
	safety := receipt.Safety
	if safety.HistoricalArtifactsOverwritten || safety.HistoricalReceiptsModified ||
		safety.OfficialRecapturePerformed || safety.CandidateCapturePerformed ||
		safety.DeploymentPerformed || safety.NetworkConfigurationChanged ||
		safety.ProductionSelectorChanged || safety.ProductionActivated ||
		safety.VircsAccessed || safety.DMITServerAccessed {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标安全边界非法")
	}

	expectedPaths := codex01491R24SelectorLiteCoordinateExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 路径数量非法")
	}
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r23_runtime_coordinate_transition_test.go":       {"7236cd06a5a014b007477e1bd1ca27e9b8a83665264c3bcd9852039c9790fe09"},
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json":                      {"824f481187b788e510d25064cb204bb7ad051ad9f7ecd8119eb082f62ba98ac4"},
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                          {"578753152bf4d86a799df39dc14b3d5ea3c623612cfacc4c43180375538b5df2"},
		"tools/official_client_capture/tests/test_candidate_rule_assertion.py":                        {"7903954d793b1ea450e4b3dea6f0cbab21737308529de6ff9a7e72abe4afca94"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"ff8d4fc171863a3d2ef589765bf2237f3e724f537f3d4455b1eb607e89182ac9"},
		"tools/official_client_capture/tests/test_codex_01491_r23_runtime_coordinate_transition.py":   {"409d96410fee5907df7d895a1306299a6e14806f9118d1fd7eab65c7e52da69c"},
		"tools/official_client_capture/tests/test_codex_upgrade.py":                                   {"960505d49d080bca01a3bb9753e40bd931371e0e727d6cfee845608ab3fc48a6"},
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		expectedFrom := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(expectedFrom) > 0 {
			expectedChange = "modified"
		}
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, expectedFrom) ||
			len(entry.ToSHA256) != 64 || strings.TrimSpace(entry.Reason) == "" ||
			strings.HasPrefix(entry.Path, "docs/egress/maintenance/") {
			return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491ServiceSuccessorReplaySupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 路径摘要不一致")
	}
	return validateCodex01491R24SelectorLiteCoordinateSemantics(coordinates)
}

func validateCodex01491R24SelectorLiteCoordinateSemantics(
	coordinates struct {
		AuxiliaryJobID                   string   `json:"auxiliary_job_id"`
		AuxiliaryTrack                   string   `json:"auxiliary_track"`
		AuxiliaryModelID                 string   `json:"auxiliary_model_id"`
		AuxiliaryExpectedResponsesLite   bool     `json:"auxiliary_expected_use_responses_lite"`
		AuxiliaryRequiredModelReceipt    bool     `json:"auxiliary_required_model_receipt"`
		SessionHeaderRuleID              string   `json:"session_header_rule_id"`
		SessionHeaderCheckIDs            []string `json:"session_header_check_ids"`
		SessionHeaderAllowedPaths        []string `json:"session_header_allowed_paths"`
		AuxiliaryPostsExcludedFromHeader bool     `json:"auxiliary_posts_excluded_from_session_header_scope"`
	},
) error {
	scenarioRaw, err := codex01491RepoFile("tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json")
	if err != nil {
		return err
	}
	var scenario struct {
		CaptureJobs []struct {
			ID                    string `json:"id"`
			Track                 string `json:"track"`
			ModelID               string `json:"model_id"`
			ExpectedResponsesLite *bool  `json:"expected_use_responses_lite"`
			RequiredModelReceipt  *bool  `json:"required_model_receipt"`
		} `json:"capture_jobs"`
	}
	if err := json.Unmarshal(scenarioRaw, &scenario); err != nil {
		return err
	}
	jobCount := 0
	for _, job := range scenario.CaptureJobs {
		if job.ID != coordinates.AuxiliaryJobID {
			continue
		}
		jobCount++
		if job.Track != coordinates.AuxiliaryTrack || job.ModelID != coordinates.AuxiliaryModelID ||
			job.ExpectedResponsesLite == nil || !*job.ExpectedResponsesLite ||
			job.RequiredModelReceipt == nil || *job.RequiredModelReceipt {
			return errors.New("Codex 0.149.1 r24 辅助任务 Lite 坐标未落入场景清单")
		}
	}
	if jobCount != 1 {
		return errors.New("Codex 0.149.1 r24 辅助任务 Lite 坐标数量非法")
	}

	expectationRaw, err := codex01491RepoFile("tools/official_client_capture/candidate_rule_expectations_0_149_1.json")
	if err != nil {
		return err
	}
	var expectation struct {
		Rules []struct {
			RuleID string `json:"rule_id"`
			Checks []struct {
				ID     string `json:"id"`
				Select struct {
					Where []struct {
						Path     string          `json:"path"`
						Operator string          `json:"operator"`
						Value    json.RawMessage `json:"value"`
					} `json:"where"`
				} `json:"select"`
			} `json:"checks"`
		} `json:"rules"`
	}
	if err := json.Unmarshal(expectationRaw, &expectation); err != nil {
		return err
	}
	matchedChecks := 0
	for _, rule := range expectation.Rules {
		if rule.RuleID != coordinates.SessionHeaderRuleID {
			continue
		}
		for _, check := range rule.Checks {
			if !slices.Contains(coordinates.SessionHeaderCheckIDs, check.ID) {
				continue
			}
			matchedChecks++
			matchedPaths := 0
			for _, condition := range check.Select.Where {
				if condition.Path != "data.path" {
					continue
				}
				matchedPaths++
				var paths []string
				if condition.Operator != "in" || json.Unmarshal(condition.Value, &paths) != nil ||
					!slices.Equal(paths, coordinates.SessionHeaderAllowedPaths) {
					return errors.New("Codex 0.149.1 r24 会话头选择器路径闭集非法")
				}
			}
			if matchedPaths != 1 {
				return errors.New("Codex 0.149.1 r24 会话头选择器路径条件数量非法")
			}
		}
	}
	if matchedChecks != len(coordinates.SessionHeaderCheckIDs) {
		return errors.New("Codex 0.149.1 r24 会话头选择器检查数量非法")
	}
	return nil
}

func codex01491R24SelectorLiteCoordinateSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491ServiceSuccessorReplaySupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadCodex01491R24SelectorLiteCoordinateTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			(entry.ToSHA256 == currentDigest ||
				codex01491ServiceSuccessorReplaySupersedes(
					path,
					entry.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestCodex01491R24SelectorLiteCoordinateTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R24SelectorLiteCoordinateTransition()
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
	if modified.Path == "" || !codex01491R24SelectorLiteCoordinateSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) || codex01491R24SelectorLiteCoordinateSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r24 选择器与 Lite 坐标精确三元组判据非法")
	}
	receipt.CorrectionContract.OfficialRecaptureRequired = true
	if err := validateCodex01491R24SelectorLiteCoordinateTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r24 选择器与 Lite 坐标 transition 接受了重复官方取证")
	}
}
