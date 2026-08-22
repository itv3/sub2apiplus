package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

const claudeFWHThirdPartyStrictDMITAcceptancePath = "docs/egress/maintenance/claude-fw-h-third-party-strict-dmit-acceptance.json"

const claudeFWHThirdPartyStrictDMITAcceptanceSHA256 = "1a77cc26952355e13d486cdf7979f1bf9ed8a40d413cf55999cb5e8093c8fcfc"

type claudeFWHThirdPartyStrictDMITAcceptance struct {
	SchemaVersion string `json:"schema_version"`
	AcceptanceID  string `json:"acceptance_id"`
	Phase         string `json:"phase"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	Predecessors  []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessors"`
	Candidate struct {
		Commit               string `json:"commit"`
		Tree                 string `json:"tree"`
		SourceArchiveSHA256  string `json:"source_archive_sha256"`
		BinarySHA256         string `json:"binary_sha256"`
		Version              string `json:"version"`
		Architecture         string `json:"architecture"`
		Image                string `json:"image"`
		ImageID              string `json:"image_id"`
		DeployedHost         string `json:"deployed_host"`
		ContainerID          string `json:"container_id"`
		ActivationFactPath   string `json:"activation_fact_path"`
		ActivationFactSHA256 string `json:"activation_fact_sha256"`
		Selector             string `json:"selector"`
		CandidateOverride    bool   `json:"candidate_override"`
		Health               string `json:"health"`
		RestartCount         int    `json:"restart_count"`
	} `json:"candidate"`
	IngressDispositions []struct {
		LogicalIngressID             string `json:"logical_ingress_id"`
		CandidateDisposition         string `json:"candidate_disposition"`
		ProductionCurrentDisposition string `json:"production_current_disposition"`
	} `json:"ingress_dispositions"`
	Validation struct {
		PositiveMatrix struct {
			Models                  []string `json:"models"`
			Protocols               []string `json:"protocols"`
			StreamModes             []bool   `json:"stream_modes"`
			ThreeModelProtocolCases int      `json:"three_model_protocol_cases"`
			BareAliasCases          int      `json:"bare_alias_cases"`
			LosslessFieldCases      int      `json:"lossless_field_cases"`
			HTTP200Cases            int      `json:"http_200_cases"`
			Result                  string   `json:"result"`
		} `json:"positive_matrix"`
		NegativeMatrix struct {
			HTTPFailCloseCases       int    `json:"http_fail_close_cases"`
			HTTPStatus               int    `json:"http_status"`
			ResponsesWebSocketPaths  int    `json:"responses_websocket_paths"`
			WebSocketHandshakeStatus int    `json:"websocket_handshake_status"`
			WebSocketCloseCode       int    `json:"websocket_close_code"`
			Result                   string `json:"result"`
		} `json:"negative_matrix"`
		AnthropicMessages struct {
			ThinkingDisplaySummarized string `json:"thinking_display_summarized"`
			ConsecutiveFableRequests  string `json:"consecutive_fable_requests"`
		} `json:"anthropic_messages"`
		RollbackAndRestoration struct {
			RollbackImage           string `json:"rollback_image"`
			RollbackImageID         string `json:"rollback_image_id"`
			RollbackContainerID     string `json:"rollback_container_id"`
			RollbackHealth          string `json:"rollback_health"`
			RollbackMessages        string `json:"rollback_messages"`
			RollbackChatCompletions string `json:"rollback_chat_completions"`
			RestoredImageID         string `json:"restored_image_id"`
			RestorationHealth       string `json:"restoration_health"`
			DependenciesUnchanged   bool   `json:"dependencies_unchanged"`
			Result                  string `json:"result"`
		} `json:"rollback_and_restoration"`
		Stability struct {
			ObservedSeconds       int    `json:"observed_seconds"`
			RestartCount          int    `json:"restart_count"`
			FatalOrPanicCount     int    `json:"fatal_or_panic_count"`
			GuardOrH1FailureCount int    `json:"guard_or_h1_failure_count"`
			StrictRouteLogCount   int    `json:"strict_route_log_count"`
			Result                string `json:"result"`
		} `json:"stability"`
		CodexIsolation struct {
			CurrentLiveProbe               string `json:"current_live_probe"`
			OpenAIOAuthAccountsObserved    int    `json:"openai_oauth_accounts_observed"`
			SchedulableOpenAIOAuthAccounts int    `json:"schedulable_openai_oauth_accounts"`
			AccountStateModified           bool   `json:"account_state_modified"`
			LocalFullGoTest                string `json:"local_full_go_test"`
			LocalGoVet                     string `json:"local_go_vet"`
			TransportProfileIsolation      string `json:"transport_profile_isolation_regression"`
			PriorDMITLiveEvidencePath      string `json:"prior_dmit_live_evidence_path"`
			PriorDMITLiveEvidenceSHA256    string `json:"prior_dmit_live_evidence_sha256"`
			PriorDMITLiveResult            string `json:"prior_dmit_live_result"`
			Result                         string `json:"result"`
		} `json:"codex_isolation"`
	} `json:"validation"`
	DependencyContainerIDs map[string]string `json:"dependency_container_ids"`
	Safety                 struct {
		ComposeDownUsed              bool `json:"compose_down_used"`
		UnscopedPruneUsed            bool `json:"unscoped_prune_used"`
		DatabaseOrCacheRecreated     bool `json:"database_or_cache_recreated"`
		UnrelatedContainersRecreated bool `json:"unrelated_containers_recreated"`
		VircsConnectedForMutation    bool `json:"vircs_connected_for_mutation"`
		VircsServiceChanged          bool `json:"vircs_service_changed"`
		ProductionDispositionChanged bool `json:"production_disposition_changed"`
		LegacyChainRemoved           bool `json:"legacy_chain_removed"`
		RemovalReceiptAllowed        bool `json:"removal_receipt_allowed"`
	} `json:"safety"`
	CandidateState  string `json:"candidate_state"`
	ProductionState string `json:"production_state"`
	RetirementState string `json:"retirement_state"`
	Result          string `json:"result"`
}

func TestClaudeFWHThirdPartyStrictDMITAcceptanceIsFrozen(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("../../..", claudeFWHThirdPartyStrictDMITAcceptancePath))
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != claudeFWHThirdPartyStrictDMITAcceptanceSHA256 {
		t.Fatal("Claude FW-H 第三方 strict DMIT AcceptanceFact 原文字节摘要不一致")
	}
	var acceptance claudeFWHThirdPartyStrictDMITAcceptance
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&acceptance); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("Claude FW-H 第三方 strict DMIT AcceptanceFact 尾部存在额外 JSON")
	}
	if acceptance.SchemaVersion != "claude-fw-h-third-party-strict-dmit-acceptance/v1" ||
		acceptance.AcceptanceID != "claude-code-2.1.226-fw-h-third-party-strict-a9b5dfd20-dmit" ||
		acceptance.Phase != "FW-H" || acceptance.IssuedAtUTC != "2026-08-22T01:42:37Z" ||
		acceptance.Result != "accepted" {
		t.Fatal("Claude FW-H 第三方 strict DMIT AcceptanceFact 顶层身份非法")
	}
	validateClaudeFWHThirdPartyStrictDMITPredecessors(t, acceptance.Predecessors)
	candidate := acceptance.Candidate
	if candidate.Commit != "a9b5dfd2070d1423824ef785faa563d34797ec8f" ||
		candidate.Tree != "bbe6c211fe9e7f84eec714bb9a7214eeaccfef38" ||
		candidate.SourceArchiveSHA256 != "8375c146f3558dfcae65b075101c7e2e3ba65c962074d643e26bb8d652af1d33" ||
		candidate.BinarySHA256 != "8b7cd68a50f1e47d03daf32ca1b11deeb9d1c16e6c0f4bc2e9eff84367e53ac6" ||
		candidate.Version != "0.1.177-4-fw-h-strict-a9b5dfd20" ||
		candidate.Architecture != "linux/amd64" ||
		candidate.Image != "sub2apiplus:fw-h-strict-a9b5dfd20" ||
		candidate.ImageID != "sha256:821037ac096d8698f2a051dcb72ecbdfff1868c83ec6f1a83b5937b1d0a3be57" ||
		candidate.DeployedHost != "DMIT" || candidate.Selector != "legacy" ||
		!candidate.CandidateOverride || candidate.Health != "healthy" || candidate.RestartCount != 0 {
		t.Fatal("Claude FW-H 第三方 strict DMIT candidate 身份非法")
	}
	if len(acceptance.IngressDispositions) != 2 {
		t.Fatal("Claude FW-H 第三方 strict 入口处置数量非法")
	}
	for _, disposition := range acceptance.IngressDispositions {
		if disposition.CandidateDisposition != "migrated_strict" ||
			disposition.ProductionCurrentDisposition != "retained_legacy" ||
			(disposition.LogicalIngressID != "chat-completions-oauth" &&
				disposition.LogicalIngressID != "responses-oauth") {
			t.Fatal("Claude FW-H 第三方 strict 入口处置非法")
		}
	}
	positive := acceptance.Validation.PositiveMatrix
	if !slices.Equal(positive.Models, []string{
		"claude-fable-5", "claude-sonnet-5", "claude-opus-5",
	}) || !slices.Equal(positive.Protocols, []string{
		IngressProtocolOpenAIChatCompletions, IngressProtocolOpenAIResponses,
	}) || !slices.Equal(positive.StreamModes, []bool{false, true}) ||
		positive.ThreeModelProtocolCases != 12 || positive.BareAliasCases != 2 ||
		positive.LosslessFieldCases != 2 || positive.HTTP200Cases != 16 ||
		positive.Result != "passed" {
		t.Fatal("Claude FW-H 第三方 strict 正例矩阵非法")
	}
	negative := acceptance.Validation.NegativeMatrix
	if negative.HTTPFailCloseCases != 24 || negative.HTTPStatus != 400 ||
		negative.ResponsesWebSocketPaths != 2 || negative.WebSocketHandshakeStatus != 101 ||
		negative.WebSocketCloseCode != 1008 || negative.Result != "passed" {
		t.Fatal("Claude FW-H 第三方 strict 负例矩阵非法")
	}
	rollback := acceptance.Validation.RollbackAndRestoration
	if rollback.RollbackImage != "sub2apiplus:fw-h-e3d698a31" ||
		rollback.RollbackHealth != "passed" || rollback.RestorationHealth != "passed" ||
		!rollback.DependenciesUnchanged || rollback.Result != "passed" {
		t.Fatal("Claude FW-H 第三方 strict 回滚或恢复事实非法")
	}
	stability := acceptance.Validation.Stability
	if stability.ObservedSeconds < 120 || stability.RestartCount != 0 ||
		stability.FatalOrPanicCount != 0 || stability.GuardOrH1FailureCount != 0 ||
		stability.StrictRouteLogCount < 1 || stability.Result != "passed" {
		t.Fatal("Claude FW-H 第三方 strict 稳定观察事实非法")
	}
	codex := acceptance.Validation.CodexIsolation
	if codex.CurrentLiveProbe != "not_executable_no_schedulable_openai_account" ||
		codex.OpenAIOAuthAccountsObserved != 2 || codex.SchedulableOpenAIOAuthAccounts != 0 ||
		codex.AccountStateModified || codex.LocalFullGoTest != "passed" ||
		codex.LocalGoVet != "passed" || codex.TransportProfileIsolation != "passed" ||
		codex.PriorDMITLiveEvidencePath != "docs/egress/guard/active-exercises.json" ||
		codex.PriorDMITLiveEvidenceSHA256 != "88dc1e2dfed3ffa4dd78718cfc701ce917fdd0d447a86f25dfaf524a3aedb80b" ||
		codex.Result != "passed_with_current_local_gate_and_frozen_live_evidence" {
		t.Fatal("Claude FW-H 第三方 strict Codex 隔离事实非法")
	}
	if acceptance.Safety.ComposeDownUsed || acceptance.Safety.UnscopedPruneUsed ||
		acceptance.Safety.DatabaseOrCacheRecreated || acceptance.Safety.UnrelatedContainersRecreated ||
		acceptance.Safety.VircsConnectedForMutation || acceptance.Safety.VircsServiceChanged ||
		acceptance.Safety.ProductionDispositionChanged || acceptance.Safety.LegacyChainRemoved ||
		acceptance.Safety.RemovalReceiptAllowed ||
		acceptance.CandidateState != "ready_for_production_canary" ||
		acceptance.ProductionState != "not_changed_by_this_acceptance" ||
		acceptance.RetirementState != "blocked_until_production_migration_and_consumer_closure" {
		t.Fatal("Claude FW-H 第三方 strict 安全边界非法")
	}
}

func validateClaudeFWHThirdPartyStrictDMITPredecessors(
	t *testing.T,
	predecessors []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	},
) {
	t.Helper()
	want := map[string]string{
		"candidate_approval\x00docs/egress/maintenance/claude-fw-h-third-party-strict-candidate-approval.json":       "7763d9337f1529dba2bdb8bdf07a98d78cb57749f75ba38a7f80fa82bd80e297",
		"bare_route_source_transition\x00docs/egress/maintenance/claude-fw-h-bare-chat-route-source-transition.json": "bc4f57df92768a71b5210737121861d9c2dd8234d56e56c84e1a256bd31d60ec",
		"bare_route_test_transition\x00docs/egress/maintenance/claude-fw-h-bare-chat-route-test-transition.json":     "d3b0312e4f1f2032b36e136f04d60653698f2e1de071c68f4f6c94b4a19fae18",
		"prior_production_acceptance\x00docs/egress/maintenance/claude-fw-h-production-acceptance-package.json":      "45b21fd3e74e6b2a3968c04bf18b416a0a43a157ba7cb7451f32a5d3c711cabd",
	}
	if len(predecessors) != len(want) {
		t.Fatal("Claude FW-H 第三方 strict DMIT 前序数量非法")
	}
	for _, predecessor := range predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if want[key] != predecessor.SHA256 {
			t.Fatal("Claude FW-H 第三方 strict DMIT 前序引用非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(predecessor.Path)))
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(raw)
		if hex.EncodeToString(sum[:]) != predecessor.SHA256 {
			t.Fatal("Claude FW-H 第三方 strict DMIT 前序摘要不一致")
		}
		delete(want, key)
	}
	if len(want) != 0 {
		t.Fatal("Claude FW-H 第三方 strict DMIT 前序未闭合")
	}
}
