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
	codex01491R11AHarnessRepairPath       = "docs/egress/maintenance/codex-0.149.1-r11a-harness-repair-transition.json"
	codex01491R11BRelayCompletionPath     = "docs/egress/maintenance/codex-0.149.1-r11b-relay-completion-transition.json"
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

type codex01491R11AHarnessRepairReceipt struct {
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
		APIKeyRef                           string `json:"api_key_ref"`
		CaptureAccountRef                   string `json:"capture_account_ref"`
		CaptureAccount20Allowed             bool   `json:"capture_account_20_allowed"`
		CaptureAccount20Used                bool   `json:"capture_account_20_used"`
		ServicePrivateIP                    string `json:"service_private_ip"`
		ServiceDefaultGateway               string `json:"service_default_gateway"`
		CapturePrivateIP                    string `json:"capture_private_ip"`
		CaptureDefaultGateway               string `json:"capture_default_gateway"`
		RequiredPublicEgress                string `json:"required_public_egress"`
		DockerNetworkChanged                bool   `json:"docker_network_changed"`
		RouteNATIPTablesChanged             bool   `json:"route_nat_iptables_changed"`
		ProxyOrComposeNetworkChanged        bool   `json:"proxy_or_compose_network_changed"`
		ProductionSelectorChanged           bool   `json:"production_selector_changed"`
		HistoricalEvidencePreservedReadOnly bool   `json:"historical_evidence_preserved_read_only"`
		VircsAccessed                       bool   `json:"vircs_accessed"`
	} `json:"boundaries"`
	RepairFacts struct {
		PublishedPortAcceptsNonLoopbackBinding bool `json:"published_port_accepts_non_loopback_binding"`
		SharedProbeAddressResolvedFromService  bool `json:"shared_probe_address_resolved_from_service_container"`
		HostsWrittenImmediatelyAfterRestart    bool `json:"hosts_written_immediately_after_restart"`
		APIKeyGroupSingleAccountGate           bool `json:"api_key_group_single_account_gate"`
		APIKeyGroupSingleKeyGate               bool `json:"api_key_group_single_key_gate"`
		ImagePermissionTemporarilyEnabled      bool `json:"image_permission_temporarily_enabled_and_restored"`
		ImageProbeFiltersStartupModels         bool `json:"image_probe_filters_startup_models"`
		AccountGateRestored                    bool `json:"account_gate_restored"`
		KeeperHostsCAAndModelMappingRestored   bool `json:"keeper_hosts_ca_and_model_mapping_restored"`
		NewCampaignRequired                    bool `json:"new_campaign_required"`
	} `json:"repair_facts"`
	PreflightEvidence struct {
		H1RunID                    string `json:"h1_run_id"`
		H1RequestCount             int    `json:"h1_request_count"`
		H1WireSHA256               string `json:"h1_wire_sha256"`
		ImagesRunID                string `json:"images_run_id"`
		ImagesRequestCount         int    `json:"images_request_count"`
		ImagesWireSHA256           string `json:"images_wire_sha256"`
		CapturedCodexVersion       string `json:"captured_codex_version"`
		PreAndPostEgressVerified   bool   `json:"pre_and_post_egress_verified"`
		PostRunRestorationVerified bool   `json:"post_run_restoration_verified"`
	} `json:"preflight_evidence"`
	Transitions  []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification struct {
		BashSyntaxPassed           bool `json:"bash_syntax_passed"`
		TargetedPythonTestsPassed  bool `json:"targeted_python_tests_passed"`
		CaptureToolTestsPassed     bool `json:"capture_tool_tests_passed"`
		EgressSpecPassed           bool `json:"egress_spec_passed"`
		ARM64H1PreflightPassed     bool `json:"arm64_h1_preflight_passed"`
		ARM64ImagesPreflightPassed bool `json:"arm64_images_preflight_passed"`
		NetworkGatePassed          bool `json:"network_gate_passed"`
	} `json:"verification"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

type codex01491R11BRelayCompletionReceipt struct {
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
	FailedOfficialAttempt struct {
		CampaignID        string `json:"campaign_id"`
		AttemptID         string `json:"attempt_id"`
		Status            string `json:"status"`
		RestorationStatus string `json:"restoration_status"`
		ReuseForbidden    bool   `json:"reuse_forbidden"`
	} `json:"failed_official_attempt"`
	Boundaries struct {
		APIKeyRef                           string `json:"api_key_ref"`
		CaptureAccountRef                   string `json:"capture_account_ref"`
		CaptureAccount20Allowed             bool   `json:"capture_account_20_allowed"`
		CaptureAccount20Used                bool   `json:"capture_account_20_used"`
		ServicePrivateIP                    string `json:"service_private_ip"`
		ServiceDefaultGateway               string `json:"service_default_gateway"`
		CapturePrivateIP                    string `json:"capture_private_ip"`
		CaptureDefaultGateway               string `json:"capture_default_gateway"`
		RequiredPublicEgress                string `json:"required_public_egress"`
		DockerNetworkChanged                bool   `json:"docker_network_changed"`
		RouteNATIPTablesChanged             bool   `json:"route_nat_iptables_changed"`
		ProxyOrComposeNetworkChanged        bool   `json:"proxy_or_compose_network_changed"`
		ProductionSelectorChanged           bool   `json:"production_selector_changed"`
		HistoricalEvidencePreservedReadOnly bool   `json:"historical_evidence_preserved_read_only"`
		VircsAccessed                       bool   `json:"vircs_accessed"`
	} `json:"boundaries"`
	FailureFacts struct {
		RelayAttemptCount                int    `json:"relay_attempt_count"`
		ModelsHTTPStatus                 int    `json:"models_http_status"`
		ModelsContentLength              int    `json:"models_content_length"`
		CapturedResponseBytesEachAttempt []int  `json:"captured_response_bytes_each_attempt"`
		AppServerClosedBeforeResponse    bool   `json:"app_server_closed_before_response_complete"`
		HostPythonVersion                string `json:"host_python_version"`
		HostPythonZstandardMissing       bool   `json:"host_python_zstandard_missing"`
		OfflineZstandardRuntime          string `json:"offline_zstandard_runtime"`
	} `json:"failure_facts"`
	RepairFacts struct {
		WaitInsideAppServerLifetime  bool    `json:"wait_for_complete_relay_response_inside_app_server_lifetime"`
		RelayPollIntervalSeconds     float64 `json:"relay_poll_interval_seconds"`
		IncompleteResponseFailsClose bool    `json:"incomplete_response_fails_closed"`
		SystemPackageInstalled       bool    `json:"system_package_installed"`
		NetworkConfigurationChanged  bool    `json:"network_configuration_changed"`
		NewCampaignRequired          bool    `json:"new_campaign_required"`
	} `json:"repair_facts"`
	Transitions  []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification struct {
		FailedAttemptRestorationVerified bool `json:"failed_attempt_restoration_verified"`
		TargetedPythonTestsPassed        bool `json:"targeted_python_tests_passed"`
		CaptureToolTestsPassed           bool `json:"capture_tool_tests_passed"`
		EgressSpecPassed                 bool `json:"egress_spec_passed"`
		NetworkGatePassed                bool `json:"network_gate_passed"`
	} `json:"verification"`
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

func codex01491R11AHarnessRepairExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
		"tools/official_client_capture/h1_wire_probe.py",
		"tools/official_client_capture/run_h1_wire_probe.sh",
		"tools/official_client_capture/run_images_wire_probe.sh",
		"tools/official_client_capture/tests/test_account_gate_restoration.py",
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r9_contamination_recovery_transition.py",
		"tools/official_client_capture/tests/test_main_track_models.py",
	}
}

func codex01491R11BRelayCompletionExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
		"tools/official_client_capture/drive_codex_model_catalog.py",
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r11b_relay_completion_transition.py",
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py",
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

func loadCodex01491R11BRelayCompletionTransition() (
	codex01491R11BRelayCompletionReceipt,
	error,
) {
	var receipt codex01491R11BRelayCompletionReceipt
	raw, err := codex01491RepoFile(codex01491R11BRelayCompletionPath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r11b relay transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	if err := validateCodex01491R11BRelayCompletionTransition(receipt); err != nil {
		return receipt, err
	}
	successor, err := loadCodex01491R11CModelsSyncTransition()
	if err != nil {
		return receipt, err
	}
	receipt.Transitions = append(receipt.Transitions, successor.Transitions...)
	return receipt, nil
}

func validateCodex01491R11BRelayCompletionTransition(
	receipt codex01491R11BRelayCompletionReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r11b-relay-completion-transition/v1" ||
		receipt.BaseCommit != "7fa29213ef50e7d2efb81efe1aeeabcdcd749426" ||
		receipt.Scope != "codex-0.149.1-r11b-relay-completion" ||
		receipt.FrameworkStage != "VC-0/P0-R11B-RELAY-COMPLETION" ||
		receipt.Result != "r11b_campaign_required_with_repaired_relay_completion" {
		return errors.New("Codex 0.149.1 r11b relay transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r11b relay transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R11AHarnessRepairPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R11AHarnessRepairPath ||
		receipt.PredecessorTransition.FileSHA256 != "c348b081fdf4357a2dd87aab6e23b571d8578e62d710a9bdd85623c5cd2bda11" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "1104fbc1742b2a8e0985e96e0cb2902bc5a9e28fed914546c74b37abe1917469" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r11b relay transition 前序绑定非法")
	}
	if receipt.FailedOfficialAttempt.CampaignID != "codex-01491-r11a" ||
		receipt.FailedOfficialAttempt.AttemptID != "20260827T000102Z-a792726deea258ed" ||
		receipt.FailedOfficialAttempt.Status != "failed" ||
		receipt.FailedOfficialAttempt.RestorationStatus != "restored" ||
		!receipt.FailedOfficialAttempt.ReuseForbidden {
		return errors.New("Codex 0.149.1 r11b relay transition 失败 Attempt 事实非法")
	}
	if receipt.Boundaries.APIKeyRef != "#4" || receipt.Boundaries.CaptureAccountRef != "#21" ||
		receipt.Boundaries.CaptureAccount20Allowed || receipt.Boundaries.CaptureAccount20Used ||
		receipt.Boundaries.ServicePrivateIP != "172.25.0.3" ||
		receipt.Boundaries.ServiceDefaultGateway != "172.25.0.1" ||
		receipt.Boundaries.CapturePrivateIP != "172.30.0.10" ||
		receipt.Boundaries.CaptureDefaultGateway != "172.30.0.1" ||
		receipt.Boundaries.RequiredPublicEgress != "179.255.100.158" ||
		receipt.Boundaries.DockerNetworkChanged || receipt.Boundaries.RouteNATIPTablesChanged ||
		receipt.Boundaries.ProxyOrComposeNetworkChanged || receipt.Boundaries.ProductionSelectorChanged ||
		!receipt.Boundaries.HistoricalEvidencePreservedReadOnly || receipt.Boundaries.VircsAccessed {
		return errors.New("Codex 0.149.1 r11b relay transition 账号或网络边界非法")
	}
	if receipt.FailureFacts.RelayAttemptCount != 3 || receipt.FailureFacts.ModelsHTTPStatus != 200 ||
		receipt.FailureFacts.ModelsContentLength != 360785 ||
		!slices.Equal(receipt.FailureFacts.CapturedResponseBytesEachAttempt, []int{1369, 1369, 1369}) ||
		!receipt.FailureFacts.AppServerClosedBeforeResponse ||
		receipt.FailureFacts.HostPythonVersion != "3.12.3" ||
		!receipt.FailureFacts.HostPythonZstandardMissing ||
		receipt.FailureFacts.OfflineZstandardRuntime != "/root/oauth-capture/runtime/codex-upgrade-pydeps-zstandard-0.22.0" {
		return errors.New("Codex 0.149.1 r11b relay transition 失败根因事实非法")
	}
	if !receipt.RepairFacts.WaitInsideAppServerLifetime ||
		receipt.RepairFacts.RelayPollIntervalSeconds != 0.05 ||
		!receipt.RepairFacts.IncompleteResponseFailsClose || receipt.RepairFacts.SystemPackageInstalled ||
		receipt.RepairFacts.NetworkConfigurationChanged || !receipt.RepairFacts.NewCampaignRequired {
		return errors.New("Codex 0.149.1 r11b relay transition 修复事实非法")
	}
	if !receipt.Verification.FailedAttemptRestorationVerified ||
		!receipt.Verification.TargetedPythonTestsPassed ||
		!receipt.Verification.CaptureToolTestsPassed || !receipt.Verification.EgressSpecPassed ||
		!receipt.Verification.NetworkGatePassed {
		return errors.New("Codex 0.149.1 r11b relay transition 验证事实非法")
	}

	expectedPaths := codex01491R11BRelayCompletionExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go": {"c3bd9cf1c56ebb3e5d752bbaa29288e0f9744a93f99508af1545120fe9f98f48"},
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go":        {"b1642b50bb6199c5bafcaaef19aed6e07e080c53e2aab2370ad28a18ee7b2184"},
		"tools/official_client_capture/drive_codex_model_catalog.py":                               {"5487e87a4159222eb91d748e5cda44ce4d336b28d96d7a667e14fb2cfb325f1a"},
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py":          {"d2cd1ee825a2feed1a6bfccd8229f6b791d1d731dc33953a24f5b86b320f8a64"},
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py":                        {"7e9e9fc3b00b3b3d3576dd8a232f9699c997df5edbd5d8b877cc64dc58ee95e7"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r11b relay transition 路径数量非法")
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
			return errors.New("Codex 0.149.1 r11b relay transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R11CModelsSyncSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r11b relay transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R11BRelayCompletionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R11BRelayCompletionTransition()
	if err != nil {
		return false
	}
	cursor := priorDigest
	if cursor == currentDigest {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, cursor) {
			cursor = entry.ToSHA256
			if cursor == currentDigest {
				return true
			}
		}
	}
	return false
}

func loadCodex01491R11AHarnessRepairTransition() (
	codex01491R11AHarnessRepairReceipt,
	error,
) {
	var receipt codex01491R11AHarnessRepairReceipt
	raw, err := codex01491RepoFile(codex01491R11AHarnessRepairPath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r11a 脚手架 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	if err := validateCodex01491R11AHarnessRepairTransition(receipt); err != nil {
		return receipt, err
	}
	successor, err := loadCodex01491R11BRelayCompletionTransition()
	if err != nil {
		return receipt, err
	}
	receipt.Transitions = append(receipt.Transitions, successor.Transitions...)
	return receipt, nil
}

func validateCodex01491R11AHarnessRepairTransition(
	receipt codex01491R11AHarnessRepairReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r11a-harness-repair-transition/v1" ||
		receipt.BaseCommit != "3421eabbaea96dcdba25808281fc9804c87af70e" ||
		receipt.Scope != "codex-0.149.1-r11a-harness-repair" ||
		receipt.FrameworkStage != "VC-0/P0-R11A-HARNESS-REPAIR" ||
		receipt.Result != "r11a_campaign_required_with_repaired_harness" {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R9ContaminationRecoveryPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R9ContaminationRecoveryPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 前序绑定非法")
	}
	if receipt.FailedCandidateAttempt.CampaignID != "codex-01491-r10a" ||
		receipt.FailedCandidateAttempt.CandidateID != "codex-01491-r10a-93d179469-arm64-21" ||
		receipt.FailedCandidateAttempt.AttemptID != "20260826T204300Z-98b0048acd77b1a4" ||
		receipt.FailedCandidateAttempt.Status != "failed" ||
		!receipt.FailedCandidateAttempt.ReuseForbidden {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 失败 Attempt 事实非法")
	}
	if receipt.Boundaries.APIKeyRef != "#4" || receipt.Boundaries.CaptureAccountRef != "#21" ||
		receipt.Boundaries.CaptureAccount20Allowed || receipt.Boundaries.CaptureAccount20Used ||
		receipt.Boundaries.ServicePrivateIP != "172.25.0.3" ||
		receipt.Boundaries.ServiceDefaultGateway != "172.25.0.1" ||
		receipt.Boundaries.CapturePrivateIP != "172.30.0.10" ||
		receipt.Boundaries.CaptureDefaultGateway != "172.30.0.1" ||
		receipt.Boundaries.RequiredPublicEgress != "179.255.100.158" ||
		receipt.Boundaries.DockerNetworkChanged || receipt.Boundaries.RouteNATIPTablesChanged ||
		receipt.Boundaries.ProxyOrComposeNetworkChanged || receipt.Boundaries.ProductionSelectorChanged ||
		!receipt.Boundaries.HistoricalEvidencePreservedReadOnly || receipt.Boundaries.VircsAccessed {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 账号或网络边界非法")
	}
	if !receipt.RepairFacts.PublishedPortAcceptsNonLoopbackBinding ||
		!receipt.RepairFacts.SharedProbeAddressResolvedFromService ||
		!receipt.RepairFacts.HostsWrittenImmediatelyAfterRestart ||
		!receipt.RepairFacts.APIKeyGroupSingleAccountGate ||
		!receipt.RepairFacts.APIKeyGroupSingleKeyGate ||
		!receipt.RepairFacts.ImagePermissionTemporarilyEnabled ||
		!receipt.RepairFacts.ImageProbeFiltersStartupModels ||
		!receipt.RepairFacts.AccountGateRestored ||
		!receipt.RepairFacts.KeeperHostsCAAndModelMappingRestored ||
		!receipt.RepairFacts.NewCampaignRequired {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 修复事实非法")
	}
	if receipt.PreflightEvidence.H1RunID != "codex-01491-r10b-preflight-h1-final" ||
		receipt.PreflightEvidence.H1RequestCount != 3 ||
		receipt.PreflightEvidence.H1WireSHA256 != "8466f16f736ea077d34ae22e8232d04e685551f824175ab8bbb5500c7f59a1da" ||
		receipt.PreflightEvidence.ImagesRunID != "codex-01491-r10b-preflight-images-final2" ||
		receipt.PreflightEvidence.ImagesRequestCount != 1 ||
		receipt.PreflightEvidence.ImagesWireSHA256 != "45243c9d53b41a8cb6b9cd77d2ce2095efe1a8423c1ba056afa5fb24aaadcf28" ||
		receipt.PreflightEvidence.CapturedCodexVersion != "0.149.1" ||
		!receipt.PreflightEvidence.PreAndPostEgressVerified ||
		!receipt.PreflightEvidence.PostRunRestorationVerified {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 预检事实非法")
	}
	if !receipt.Verification.BashSyntaxPassed || !receipt.Verification.TargetedPythonTestsPassed ||
		!receipt.Verification.CaptureToolTestsPassed || !receipt.Verification.EgressSpecPassed ||
		!receipt.Verification.ARM64H1PreflightPassed ||
		!receipt.Verification.ARM64ImagesPreflightPassed || !receipt.Verification.NetworkGatePassed {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 验证事实非法")
	}

	expectedPaths := codex01491R11AHarnessRepairExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go":     {"f63ad881b9b9e2fccf036b113b0a3cbedf347179943045a2b290ff500300f26a"},
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go":            {"d16ed5a263c53cd5302692139036a7d5649e530de6baa605b8acd3d8ee123a14"},
		"tools/official_client_capture/h1_wire_probe.py":                                               {"9f4eab19e8321b67185246884d449f55f61855de81099a93807e0057154c81b2"},
		"tools/official_client_capture/run_h1_wire_probe.sh":                                           {"a4018f3c9ccc34867e197d13a335ca0a193818e96fae5c1129f3633c41304471"},
		"tools/official_client_capture/run_images_wire_probe.sh":                                       {"af14cca8f84b7784c5841190e4c70a81b7e707f71074ca1fe4bcfae22623f4f1"},
		"tools/official_client_capture/tests/test_account_gate_restoration.py":                         {"f6f143701b65a3cfe14ccdeee90a215ee6e2118b7491b4b9ca6918999baa8583"},
		"tools/official_client_capture/tests/test_codex_01491_r9_contamination_recovery_transition.py": {"663c46e41ef9cbac5daad39aad051f06312f6177a80e27290e9fb80bb68837d8"},
		"tools/official_client_capture/tests/test_main_track_models.py":                                {"5ecc7627c4b4ee8a674a3c99289423e45cea534f8bdd5b2da286ee0d080d0cac"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r11a 脚手架 transition 路径数量非法")
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
			return errors.New("Codex 0.149.1 r11a 脚手架 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R11BRelayCompletionSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 r11a 脚手架 transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R11AHarnessRepairSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R11AHarnessRepairTransition()
	if err != nil {
		return false
	}
	cursor := priorDigest
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, cursor) {
			cursor = entry.ToSHA256
		}
	}
	return cursor == currentDigest
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
	if err := validateCodex01491R9ContaminationRecoveryTransition(receipt); err != nil {
		return receipt, err
	}
	successor, err := loadCodex01491R11AHarnessRepairTransition()
	if err != nil {
		return receipt, err
	}
	receipt.Transitions = append(receipt.Transitions, successor.Transitions...)
	return receipt, nil
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
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R11AHarnessRepairSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
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

func TestCodex01491R11AHarnessRepairTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R11AHarnessRepairTransition()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Boundaries.CaptureAccountRef = "#20"
	if err := validateCodex01491R11AHarnessRepairTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11a 脚手架 transition 接受了账号 #20")
	}
	mutated = receipt
	mutated.Boundaries.DockerNetworkChanged = true
	if err := validateCodex01491R11AHarnessRepairTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11a 脚手架 transition 接受了 Docker 网络变更")
	}
}

func TestCodex01491R11BRelayCompletionTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R11BRelayCompletionTransition()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Boundaries.CaptureAccountRef = "#20"
	if err := validateCodex01491R11BRelayCompletionTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11b relay transition 接受了账号 #20")
	}
	mutated = receipt
	mutated.RepairFacts.IncompleteResponseFailsClose = false
	if err := validateCodex01491R11BRelayCompletionTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11b relay transition 接受了不完整响应")
	}
}
