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

const codex01491R11CModelsSyncPath = "docs/egress/maintenance/codex-0.149.1-r11c-models-sync-transition.json"

type codex01491R11CModelCondition struct {
	Model            string `json:"model"`
	UseResponsesLite bool   `json:"use_responses_lite"`
}

type codex01491R11CArtifact struct {
	Role   string `json:"role"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Bytes  int    `json:"bytes"`
}

type codex01491R11CModelsSyncReceipt struct {
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
		MITMProxyUpstreamTLSHandshakeFailed bool   `json:"mitmproxy_upstream_tls_handshake_failed"`
		DirectTLSControlPassed              bool   `json:"direct_tls_control_passed"`
		MITMHTTP1AndHTTP2BothFailed         bool   `json:"mitm_http1_and_http2_both_failed"`
		FailureSegment                      string `json:"failure_segment"`
		ClientToMITMTLSFailed               bool   `json:"client_to_mitm_tls_failed"`
		CompleteRecaptureRequired           bool   `json:"complete_recapture_required"`
	} `json:"failure_facts"`
	RepairFacts struct {
		OfficialDebugModelsUsed        bool `json:"official_debug_models_used"`
		IsolatedCodexHomeUsed          bool `json:"isolated_codex_home_used"`
		BundledCatalogForbidden        bool `json:"bundled_catalog_forbidden"`
		BuiltInOpenAIProviderPreserved bool `json:"built_in_openai_provider_preserved"`
		RawModelsHTTP200Required       bool `json:"raw_models_http_200_required"`
		ModelCatalogOnlyMode           bool `json:"model_catalog_only_mode"`
		ResponsesRequestsSent          bool `json:"responses_requests_sent"`
		ByteRelayUpstreamTLSUsed       bool `json:"byte_relay_upstream_tls_used"`
		IncompleteResponseFailsClosed  bool `json:"incomplete_response_fails_closed"`
		SystemPackageInstalled         bool `json:"system_package_installed"`
		NetworkConfigurationChanged    bool `json:"network_configuration_changed"`
	} `json:"repair_facts"`
	OnlineVerification struct {
		RunID                      string                         `json:"run_id"`
		StartedAtUTC               string                         `json:"started_at_utc"`
		FinishedAtUTC              string                         `json:"finished_at_utc"`
		DurationSeconds            int                            `json:"duration_seconds"`
		AttemptCount               int                            `json:"attempt_count"`
		FirstAttemptResponseBytes  int                            `json:"first_attempt_response_bytes"`
		SuccessfulConnectionID     int                            `json:"successful_connection_id"`
		RequestMethod              string                         `json:"request_method"`
		RequestPath                string                         `json:"request_path"`
		ResponseStatus             int                            `json:"response_status"`
		ResponseContentLength      int                            `json:"response_content_length"`
		ResponseTotalBytes         int                            `json:"response_total_bytes"`
		CatalogModelCount          int                            `json:"catalog_model_count"`
		ModelConditions            []codex01491R11CModelCondition `json:"model_conditions"`
		RelayConnectionCount       int                            `json:"relay_connection_count"`
		RelayValidConnectionCount  int                            `json:"relay_valid_connection_count"`
		CredentialReplacementCount int                            `json:"credential_replacement_count"`
		ResidualCredentialCount    int                            `json:"residual_credential_count"`
		Artifacts                  []codex01491R11CArtifact       `json:"artifacts"`
	} `json:"online_verification"`
	Restoration struct {
		CaptureHostsSHA256Before   string `json:"capture_hosts_sha256_before"`
		CaptureHostsSHA256After    string `json:"capture_hosts_sha256_after"`
		CaptureCASHA256Before      string `json:"capture_ca_sha256_before"`
		CaptureCASHA256After       string `json:"capture_ca_sha256_after"`
		CapturePublicEgressAfter   string `json:"capture_public_egress_after"`
		ServicePublicEgressAfter   string `json:"service_public_egress_after"`
		TemporaryCAAbsent          bool   `json:"temporary_ca_absent"`
		ChatGPTHostsOverrideAbsent bool   `json:"chatgpt_hosts_override_absent"`
		RelayProcessAbsent         bool   `json:"relay_process_absent"`
		RelayPort443Free           bool   `json:"relay_port_443_free"`
		LoginStatusPreserved       bool   `json:"login_status_preserved"`
	} `json:"restoration"`
	Transitions  []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification struct {
		TargetedPythonTestsPassed    bool `json:"targeted_python_tests_passed"`
		CaptureToolTestsPassed       bool `json:"capture_tool_tests_passed"`
		EgressSpecPassed             bool `json:"egress_spec_passed"`
		GoTransitionTestsPassed      bool `json:"go_transition_tests_passed"`
		NetworkGatePassed            bool `json:"network_gate_passed"`
		OnlineModelsHTTP200Passed    bool `json:"online_models_http_200_passed"`
		HistoricalArtifactsUnchanged bool `json:"historical_artifacts_unchanged"`
	} `json:"verification"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

func codex01491R11CModelsSyncExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
		"backend/internal/service/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/drive_codex_model_catalog.py",
		"tools/official_client_capture/run_official_relay_scenario.sh",
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r11b_relay_completion_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r11c_models_sync_transition.py",
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py",
	}
}

func loadCodex01491R11CModelsSyncTransition() (
	codex01491R11CModelsSyncReceipt,
	error,
) {
	var receipt codex01491R11CModelsSyncReceipt
	raw, err := codex01491RepoFile(codex01491R11CModelsSyncPath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r11c 模型目录 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	return receipt, validateCodex01491R11CModelsSyncTransition(receipt)
}

func validateCodex01491R11CModelsSyncTransition(
	receipt codex01491R11CModelsSyncReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r11c-models-sync-transition/v1" ||
		receipt.BaseCommit != "87de87becf648148e02fe44ec5d9d91afdc1a708" ||
		receipt.Scope != "codex-0.149.1-r11c-models-sync" ||
		receipt.FrameworkStage != "VC-0/P0-R11C-MODELS-SYNC" ||
		receipt.Result != "r11c_models_sync_repair_verified" {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R11BRelayCompletionPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R11BRelayCompletionPath ||
		receipt.PredecessorTransition.FileSHA256 != "207bc45953007446a24ddd385fd3533102a2fa60ef95e3c31ce1238a885222d6" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "f52ba06b86b756b6a3e4c89e043e41f1210e8c43614859af9fb8ecf39d5c49b6" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 前序绑定非法")
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
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 账号或网络边界非法")
	}
	if !receipt.FailureFacts.MITMProxyUpstreamTLSHandshakeFailed ||
		!receipt.FailureFacts.DirectTLSControlPassed || !receipt.FailureFacts.MITMHTTP1AndHTTP2BothFailed ||
		receipt.FailureFacts.FailureSegment != "mitmproxy_to_chatgpt.com" ||
		receipt.FailureFacts.ClientToMITMTLSFailed || receipt.FailureFacts.CompleteRecaptureRequired {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition TLS 根因事实非法")
	}
	if !receipt.RepairFacts.OfficialDebugModelsUsed || !receipt.RepairFacts.IsolatedCodexHomeUsed ||
		!receipt.RepairFacts.BundledCatalogForbidden || !receipt.RepairFacts.BuiltInOpenAIProviderPreserved ||
		!receipt.RepairFacts.RawModelsHTTP200Required || !receipt.RepairFacts.ModelCatalogOnlyMode ||
		receipt.RepairFacts.ResponsesRequestsSent || !receipt.RepairFacts.ByteRelayUpstreamTLSUsed ||
		!receipt.RepairFacts.IncompleteResponseFailsClosed || receipt.RepairFacts.SystemPackageInstalled ||
		receipt.RepairFacts.NetworkConfigurationChanged {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 修复事实非法")
	}

	online := receipt.OnlineVerification
	if online.RunID != "codex-01491-r11c-models-sync-20260827T025700Z" ||
		online.StartedAtUTC != "2026-08-27T02:56:38Z" ||
		online.FinishedAtUTC != "2026-08-27T02:58:47Z" || online.DurationSeconds != 129 ||
		online.AttemptCount != 2 || online.FirstAttemptResponseBytes != 1369 ||
		online.SuccessfulConnectionID != 2 || online.RequestMethod != "GET" ||
		online.RequestPath != "/backend-api/codex/models?client_version=0.149.1" ||
		online.ResponseStatus != 200 || online.ResponseContentLength != 360785 ||
		online.ResponseTotalBytes != 362553 || online.CatalogModelCount != 9 ||
		online.RelayConnectionCount != 2 || online.RelayValidConnectionCount != 2 ||
		online.CredentialReplacementCount != 16 || online.ResidualCredentialCount != 0 ||
		!slices.Equal(online.ModelConditions, []codex01491R11CModelCondition{
			{Model: "gpt-5.5", UseResponsesLite: false},
			{Model: "gpt-5.6-luna", UseResponsesLite: true},
		}) {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 在线验证事实非法")
	}
	expectedArtifacts := []codex01491R11CArtifact{
		{Role: "model_catalog_prewarm", Path: "/root/oauth-capture/runs/codex-01491-r11c-models-sync-20260827T025700Z/model-catalog-prewarm.json", SHA256: "9b2b195ca303e04070088e482087bb0206fd71f620edcdd32c2021457a3553d4", Bytes: 594},
		{Role: "relay_manifest", Path: "/root/oauth-capture/runs/codex-01491-r11c-models-sync-20260827T025700Z/relay/relay.json", SHA256: "a0f6784afee87abb41299ce822a6819cf15832a4902b3a5ea23e2a1e01dd626f", Bytes: 13864},
		{Role: "models_request_scrubbed", Path: "/root/oauth-capture/runs/codex-01491-r11c-models-sync-20260827T025700Z/relay/conn002.client_to_upstream.bin", SHA256: "65b4cf4bf030ad023bbbe51b623f31a803c5b24ad4ad8133ce555c22a85acb6c", Bytes: 1967},
		{Role: "models_response_scrubbed", Path: "/root/oauth-capture/runs/codex-01491-r11c-models-sync-20260827T025700Z/relay/conn002.upstream_to_client.bin", SHA256: "468faa78f994964ddaa5ad7b427f8501d5183d9f42bc6d25c0d3eeaea0845788", Bytes: 362553},
		{Role: "run_log", Path: "/root/oauth-capture/diagnostics/codex-01491-r11c-models-sync-20260827T025700Z/run.log", SHA256: "981da85f19fb81f54f6f6c475d7c0d5ee902fddd1ef1d7ae806dd61d6e1ef57e", Bytes: 2952},
		{Role: "timing_log", Path: "/root/oauth-capture/diagnostics/codex-01491-r11c-models-sync-20260827T025700Z/timing.log", SHA256: "7504dcffe3708c417fec9e9e29cf265fda1571ee82338b299ae55ac7b21805a1", Bytes: 85},
	}
	if !slices.Equal(online.Artifacts, expectedArtifacts) {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 证据绑定非法")
	}
	restoration := receipt.Restoration
	if restoration.CaptureHostsSHA256Before != "658a0f13912d524aaf3e682eedfbd2fe1e75f10e0edd2c3292468429ad2bfd23" ||
		restoration.CaptureHostsSHA256After != restoration.CaptureHostsSHA256Before ||
		restoration.CaptureCASHA256Before != "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd" ||
		restoration.CaptureCASHA256After != restoration.CaptureCASHA256Before ||
		restoration.CapturePublicEgressAfter != "179.255.100.158" ||
		restoration.ServicePublicEgressAfter != "179.255.100.158" ||
		!restoration.TemporaryCAAbsent || !restoration.ChatGPTHostsOverrideAbsent ||
		!restoration.RelayProcessAbsent || !restoration.RelayPort443Free ||
		!restoration.LoginStatusPreserved {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 环境恢复事实非法")
	}
	if !receipt.Verification.TargetedPythonTestsPassed || !receipt.Verification.CaptureToolTestsPassed ||
		!receipt.Verification.EgressSpecPassed || !receipt.Verification.GoTransitionTestsPassed ||
		!receipt.Verification.NetworkGatePassed || !receipt.Verification.OnlineModelsHTTP200Passed ||
		!receipt.Verification.HistoricalArtifactsUnchanged {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 门禁未闭合")
	}

	expectedPaths := codex01491R11CModelsSyncExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go": {"cb7f69d38e6cdc2c6e2884b4553dc1af384e7205b762c73370cd0d88083ae56e"},
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go":        {"2b58acc2459a9bc80ddc5296729eae07473520ce4ed145e6d7d768b36c6f9ccf"},
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                       {"b7ae0e37ab4bcb79182bbd2d005e52f515b21fa659211cab9ef96db24c85e4e5"},
		"tools/official_client_capture/drive_codex_model_catalog.py":                               {"d2a7e3c81054a370913da4fe369075e7196dfa1e8a8eacd6ef3ab3f03ac776f4"},
		"tools/official_client_capture/run_official_relay_scenario.sh":                             {"668e5ea4059270218f4982ece6d6e2dc2a51912418e880ad8d9abff2b0de71ae"},
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py":          {"9209136f80c163bdfa21191be8e118291b1cd6a1db2592739f8c32e2ecd19a7e"},
		"tools/official_client_capture/tests/test_codex_01491_r11b_relay_completion_transition.py": {"77d7d069aaf99d27b8d25abcf2801b3f09065abab85eabb21fbea8e55f073fb8"},
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py":                        {"cb1c7cf907883286a14e9d873b867942b9d1c7ab7593b5c9888dad2b1db07362"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r11c 模型目录 transition 路径数量非法")
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
			return errors.New("Codex 0.149.1 r11c 模型目录 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r11c 模型目录 transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R11CModelsSyncSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R11CModelsSyncTransition()
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

func TestCodex01491R11CModelsSyncTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R11CModelsSyncTransition()
	if err != nil {
		t.Fatal(err)
	}
	mutated := receipt
	mutated.Boundaries.CaptureAccountRef = "#20"
	if err := validateCodex01491R11CModelsSyncTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11c 模型目录 transition 接受了账号 #20")
	}
	mutated = receipt
	mutated.RepairFacts.RawModelsHTTP200Required = false
	if err := validateCodex01491R11CModelsSyncTransition(mutated); err == nil {
		t.Fatal("Codex 0.149.1 r11c 模型目录 transition 接受了缺失的原始 HTTP 200")
	}
}
