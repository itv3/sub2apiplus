package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

const codex01491R25EP014CookieConditionServicePath = "docs/egress/maintenance/codex-0.149.1-r25-ep014-cookie-condition-transition.json"

type codex01491R25EP014CookieConditionServiceReceipt struct {
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
	ToolingClosure struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"tooling_closure"`
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
	CorrectedFact struct {
		RuleID   string `json:"rule_id"`
		CheckID  string `json:"check_id"`
		Selector struct {
			Path    string `json:"path"`
			Variant string `json:"variant"`
			Track   string `json:"track"`
		} `json:"selector"`
		OfficialEvidence []struct {
			Path              string `json:"path"`
			Track             string `json:"track"`
			LiteHeaderPresent bool   `json:"lite_header_present"`
			CookiePresent     bool   `json:"cookie_present"`
		} `json:"official_evidence"`
		Assertion struct {
			Allowed  []string `json:"allowed"`
			Operator string   `json:"operator"`
			Path     string   `json:"path"`
			Required []string `json:"required"`
		} `json:"assertion"`
		CookieCondition struct {
			PresentWhen string `json:"present_when"`
			AbsentWhen  string `json:"absent_when"`
			SlotAfter   string `json:"slot_after"`
			SlotBefore  string `json:"slot_before"`
		} `json:"cookie_condition"`
	} `json:"corrected_fact"`
	ModelCoordinates struct {
		MainTrack             string `json:"main_track"`
		MainModel             string `json:"main_model"`
		LiteTrack             string `json:"lite_track"`
		LiteModel             string `json:"lite_model"`
		WholeMainSwitchedLuna bool   `json:"whole_main_switched_to_luna"`
	} `json:"model_coordinates"`
	PathSetSHA256 string                                  `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceServiceEntry `json:"transitions"`
	Verification  struct {
		OfficialRawHeadersReplayed        bool `json:"official_raw_headers_replayed"`
		ColdAndWarmCookieConditionsTested bool `json:"cold_and_warm_cookie_conditions_tested"`
		OverrideGenerationTested          bool `json:"override_generation_tested"`
		MutationTestsRequired             bool `json:"mutation_tests_required"`
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
		ARM64ReadOnlyEvidenceAccessed  bool `json:"arm64_read_only_evidence_accessed"`
		VircsAccessed                  bool `json:"vircs_accessed"`
		DMITServerAccessed             bool `json:"dmit_server_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491R25EP014CookieConditionServiceOnce   sync.Once
	codex01491R25EP014CookieConditionServiceCached codex01491R25EP014CookieConditionServiceReceipt
	codex01491R25EP014CookieConditionServiceError  error
)

func codex01491R25EP014CookieConditionExpectedServicePaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r25_ep014_cookie_condition_transition_test.go",
		"backend/internal/officialegress/codex_01491_service_successor_replay_transition_test.go",
		"backend/internal/service/codex_01491_r25_ep014_cookie_condition_transition_test.go",
		"backend/internal/service/codex_01491_service_successor_replay_transition_test.go",
		"docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
		"tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json",
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/codex_upgrade_evidence_labels_0_149_1.json",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/tests/test_candidate_rule_assertion.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r25_ep014_cookie_condition_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_service_successor_replay_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
}

func codex01491R25EP014CookieConditionServiceFile(path string) ([]byte, error) {
	return os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
}

func loadCodex01491R25EP014CookieConditionServiceTransition() (
	codex01491R25EP014CookieConditionServiceReceipt,
	error,
) {
	codex01491R25EP014CookieConditionServiceOnce.Do(func() {
		raw, err := codex01491R25EP014CookieConditionServiceFile(
			codex01491R25EP014CookieConditionServicePath,
		)
		if err != nil {
			codex01491R25EP014CookieConditionServiceError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R25EP014CookieConditionServiceCached); err != nil {
			codex01491R25EP014CookieConditionServiceError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R25EP014CookieConditionServiceError = errors.New("Codex 0.149.1 service r25 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyCandidateGateServiceIdentity(
			raw,
			codex01491R25EP014CookieConditionServiceCached.IdentitySHA256,
		); err != nil {
			codex01491R25EP014CookieConditionServiceError = err
			return
		}
		codex01491R25EP014CookieConditionServiceError =
			validateCodex01491R25EP014CookieConditionServiceTransition(
				codex01491R25EP014CookieConditionServiceCached,
			)
	})
	return codex01491R25EP014CookieConditionServiceCached,
		codex01491R25EP014CookieConditionServiceError
}

func validateCodex01491R25EP014CookieConditionServiceTransition(
	receipt codex01491R25EP014CookieConditionServiceReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r25-ep014-cookie-condition-transition/v1" ||
		receipt.BaseCommit != "1369078cecee21296978bae23a151206d250acfb" ||
		receipt.Scope != "codex-0.149.1-r25-ep014-cookie-condition" ||
		receipt.FrameworkStage != "VC-3/VC-4/SAME-VERSION-SUCCESSOR" ||
		receipt.Result != "ep014_cookie_condition_successor_tooling_frozen" {
		return errors.New("Codex 0.149.1 service r25 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 service r25 transition 时间非法")
	}
	predecessorRaw, err := codex01491R25EP014CookieConditionServiceFile(
		"docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json",
	)
	if err != nil {
		return err
	}
	closureRaw, err := codex01491R25EP014CookieConditionServiceFile(
		codex01491ServiceSuccessorReplayPath,
	)
	if err != nil {
		return err
	}
	if receipt.Predecessor.Path != "docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json" ||
		receipt.Predecessor.FileSHA256 != upstreamMergeFrameworkServiceDigest(predecessorRaw) ||
		receipt.Predecessor.IdentitySHA256 != "3fd50b27cd48f8a9df0b61e697cdf09f36673f765aa9ef81aed611c09fbed96d" ||
		receipt.ToolingClosure.Path != codex01491ServiceSuccessorReplayPath ||
		receipt.ToolingClosure.FileSHA256 != upstreamMergeFrameworkServiceDigest(closureRaw) ||
		receipt.ToolingClosure.IdentitySHA256 != "7db2e0f598a4b4b53a7dcb4e7c4c47e05aecb149352793b5516e92d94912a430" {
		return errors.New("Codex 0.149.1 service r25 前序或闭合绑定非法")
	}
	contract := receipt.CorrectionContract
	if contract.Reason != "classification_fact_correction" ||
		contract.PredecessorImportSchema != "codex-upgrade-predecessor-import/v3" ||
		contract.ImportMode != "official_only_reclassification" ||
		contract.OfficialRecaptureRequired || !contract.OfficialEvidenceReplayRequired ||
		contract.ApprovedClassificationImported || !contract.ClassificationReapprovalRequired ||
		!contract.ApprovedScenarioRebindRequired || !contract.CandidateRecaptureRequired ||
		!contract.KiloRevalidationRequired {
		return errors.New("Codex 0.149.1 service r25 后继合同非法")
	}
	allowed := []string{
		"version", "x-codex-installation-id", "x-codex-window-id",
		"x-codex-turn-metadata", "session-id", "thread-id",
		"x-codex-routing-hint", "x-openai-internal-codex-responses-lite",
		"authorization", "chatgpt-account-id", "content-type", "accept",
		"originator", "user-agent", "cookie", "host", "content-length",
	}
	required := slices.DeleteFunc(slices.Clone(allowed), func(header string) bool {
		return header == "cookie"
	})
	fact := receipt.CorrectedFact
	if fact.RuleID != "SPEC-EP-014" || fact.CheckID != "legacy-default-headers" ||
		fact.Selector.Path != "/backend-api/codex/responses/compact" ||
		fact.Selector.Variant != "default" || fact.Selector.Track != "lite" ||
		len(fact.OfficialEvidence) != 2 ||
		fact.OfficialEvidence[0].Path != "c1491-r14-f-lite-legacy-compact-default/relay/conn006.client_to_upstream.bin" ||
		fact.OfficialEvidence[0].Track != "lite" || !fact.OfficialEvidence[0].LiteHeaderPresent ||
		fact.OfficialEvidence[0].CookiePresent ||
		fact.OfficialEvidence[1].Path != "c1491-r14-f-official-legacy-compact-default/relay/conn007.client_to_upstream.bin" ||
		fact.OfficialEvidence[1].Track != "main" || fact.OfficialEvidence[1].LiteHeaderPresent ||
		!fact.OfficialEvidence[1].CookiePresent ||
		fact.Assertion.Operator != "all_ordered_subset_of" ||
		fact.Assertion.Path != "data.header_names_in_order" ||
		!slices.Equal(fact.Assertion.Allowed, allowed) ||
		!slices.Equal(fact.Assertion.Required, required) ||
		fact.CookieCondition.PresentWhen != "cookie_jar_established" ||
		fact.CookieCondition.AbsentWhen != "cold_start_without_cookie_jar" ||
		fact.CookieCondition.SlotAfter != "user-agent" ||
		fact.CookieCondition.SlotBefore != "host" {
		return errors.New("Codex 0.149.1 service r25 条件事实非法")
	}
	models := receipt.ModelCoordinates
	if models.MainTrack != "main" || models.MainModel != "gpt-5.5" ||
		models.LiteTrack != "lite" || models.LiteModel != "gpt-5.6-luna" ||
		models.WholeMainSwitchedLuna {
		return errors.New("Codex 0.149.1 service r25 模型坐标非法")
	}
	verification := receipt.Verification
	if !verification.OfficialRawHeadersReplayed ||
		!verification.ColdAndWarmCookieConditionsTested ||
		!verification.OverrideGenerationTested || !verification.MutationTestsRequired {
		return errors.New("Codex 0.149.1 service r25 验证事实非法")
	}
	safety := receipt.Safety
	if safety.HistoricalArtifactsOverwritten || safety.HistoricalReceiptsModified ||
		safety.OfficialRecapturePerformed || safety.CandidateCapturePerformed ||
		safety.DeploymentPerformed || safety.NetworkConfigurationChanged ||
		safety.ProductionSelectorChanged || safety.ProductionActivated ||
		!safety.ARM64ReadOnlyEvidenceAccessed || safety.VircsAccessed ||
		safety.DMITServerAccessed {
		return errors.New("Codex 0.149.1 service r25 安全边界非法")
	}
	expectedPaths := codex01491R25EP014CookieConditionExpectedServicePaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_service_successor_replay_transition_test.go":     {"23e71227b1332676b58cfd919695fe672294b7380bd56574e54f0161b3758feb"},
		"backend/internal/service/codex_01491_service_successor_replay_transition_test.go":            {"756ba7b034d916be8cb530ac53a967d79bf2c3abfc8999a899686fe4e659814a"},
		"docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md":                                            {"978b27a8fe7948e8a6623d4013f7b2034758b748221d6a67c0be0e682f6223fb"},
		"tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json":             {"e6c81e1e739ea95414fa412dde460430047798f7af182cdd38293de57f2a0dfa"},
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json":                      {"2aa98c9529be6748e692382c60f9c719860ebccb063c25634cea173a2371a865"},
		"tools/official_client_capture/codex_upgrade.py":                                              {"4f45ecf5f87f16a9e3379335d0f06c02b3c7058dce54ee08ae7d7bed960a847d"},
		"tools/official_client_capture/codex_upgrade_evidence_labels_0_149_1.json":                    {"685a892e57224a3af3edc5abeb22279ccbb91624ff76972027248070113ef8bd"},
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                          {"4852bb85360fb3ccad112f426fc7313b7aaa715a8035928f4a3f428cfd02ee25"},
		"tools/official_client_capture/tests/test_candidate_rule_assertion.py":                        {"a88f2a00b93afeaff836f93f66b01cc13204459e93ad037f1a3d1519ea1353ce"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"bb162972ded26c2ec15c0c3e724e59f6f5e6d82a42fabfcc425e5b2f9cc315cb"},
		"tools/official_client_capture/tests/test_codex_01491_service_successor_replay_transition.py": {"c5229424b1806a3a3015ed545b8281dbd7986f01b7db12c7d0b5aad6a540bec6"},
		"tools/official_client_capture/tests/test_codex_upgrade.py":                                   {"944f7bf20742fa8afc830e55cb30d3fcd57056003e122a7e1b421b893931bc02"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 service r25 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		expectedFrom := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(expectedFrom) > 0 {
			expectedChange = "modified"
		}
		current, readErr := codex01491R25EP014CookieConditionServiceFile(entry.Path)
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, expectedFrom) || readErr != nil ||
			upstreamMergeFrameworkServiceDigest(current) != entry.ToSHA256 ||
			len(entry.ToSHA256) != 64 || strings.TrimSpace(entry.Reason) == "" ||
			strings.HasPrefix(entry.Path, "docs/egress/maintenance/") {
			return errors.New("Codex 0.149.1 service r25 transition 条目非法：" + expectedPath)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkServiceDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 service r25 路径摘要非法")
	}
	return nil
}

func codex01491R25EP014CookieConditionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R25EP014CookieConditionServiceTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, priorDigest) &&
			entry.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func TestCodex01491R25EP014CookieConditionServiceTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R25EP014CookieConditionServiceTransition()
	if err != nil {
		t.Fatal(err)
	}
	var modified codex01491CandidateSourceServiceEntry
	for _, entry := range receipt.Transitions {
		if entry.Change == "modified" {
			modified = entry
			break
		}
	}
	if modified.Path == "" || !codex01491R25EP014CookieConditionSupersedesService(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) || codex01491R25EP014CookieConditionSupersedesService(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 service r25 精确三元组判据非法")
	}
	receipt.ModelCoordinates.MainModel = "gpt-5.6-luna"
	receipt.ModelCoordinates.WholeMainSwitchedLuna = true
	if err := validateCodex01491R25EP014CookieConditionServiceTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 service r25 接受了全主线 Luna 扩大")
	}
}
