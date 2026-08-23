package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const claudeFWHProductionAcceptanceSHA256 = "45b21fd3e74e6b2a3968c04bf18b416a0a43a157ba7cb7451f32a5d3c711cabd"

type claudeFWHProductionAcceptancePackage struct {
	SchemaVersion string `json:"schema_version"`
	Phase         string `json:"phase"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	DeploymentID  string `json:"deployment_id"`
	Target        struct {
		Product        string   `json:"product"`
		Version        string   `json:"version"`
		Platform       string   `json:"platform"`
		Authentication string   `json:"authentication"`
		Privacy        string   `json:"privacy"`
		Models         []string `json:"models"`
		RequiredRules  int      `json:"required_rules"`
		ProfileSHA256  string   `json:"profile_sha256"`
		WireSHA256     string   `json:"wire_sha256"`
		ReleaseSHA256  string   `json:"release_sha256"`
		BundleSHA256   string   `json:"bundle_sha256"`
		ApprovalSHA256 string   `json:"approval_sha256"`
	} `json:"target"`
	Predecessors []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessors"`
	PromotionReceipt struct {
		Candidate struct {
			CandidateID string `json:"candidate_id"`
			Commit      string `json:"commit"`
			ImageID     string `json:"image_id"`
		} `json:"candidate"`
		FormalRelease struct {
			Commit             string `json:"commit"`
			Tree               string `json:"tree"`
			SourceTreeSHA256   string `json:"source_tree_sha256"`
			BinarySHA256       string `json:"binary_sha256"`
			Version            string `json:"version"`
			Image              string `json:"image"`
			ImageID            string `json:"image_id"`
			ImageArchiveSHA256 string `json:"image_archive_sha256"`
		} `json:"formal_release"`
		Invariants struct {
			ProfileUnchanged       bool   `json:"profile_unchanged"`
			WireUnchanged          bool   `json:"wire_unchanged"`
			ReleaseUnchanged       bool   `json:"release_unchanged"`
			BundleUnchanged        bool   `json:"bundle_unchanged"`
			RequiredRulesUnchanged bool   `json:"required_rules_unchanged"`
			CodexFinalWire         string `json:"codex_final_wire"`
		} `json:"invariants"`
		Result string `json:"result"`
	} `json:"promotion_receipt"`
	Envelopes struct {
		ActiveSupport struct {
			LogicalIngressIDs []string `json:"logical_ingress_ids"`
			Models            []string `json:"models"`
			RequiredRules     int      `json:"required_rules"`
		} `json:"active_support"`
		RollbackOperational struct {
			Kind                    string   `json:"kind"`
			ImageID                 string   `json:"image_id"`
			WireEvidence            string   `json:"wire_evidence"`
			LogicalIngressIDs       []string `json:"logical_ingress_ids"`
			DMITFunctionalMatrix    string   `json:"dmit_functional_matrix"`
			VircsMechanicalRollback string   `json:"vircs_mechanical_rollback"`
		} `json:"rollback_operational"`
		DeploymentTraffic struct {
			LogicalIngressIDs         []string `json:"logical_ingress_ids"`
			SubsetOfActiveAndRollback bool     `json:"subset_of_active_and_rollback"`
		} `json:"deployment_traffic"`
	} `json:"envelopes"`
	DeploymentFacts []struct {
		Sequence       int            `json:"sequence"`
		FactID         string         `json:"fact_id"`
		Stage          string         `json:"stage"`
		PreviousFactID *string        `json:"previous_fact_id"`
		Host           string         `json:"host"`
		IssuedAtUTC    string         `json:"issued_at_utc"`
		ContainerID    string         `json:"container_id"`
		ImageID        string         `json:"image_id"`
		Selector       string         `json:"selector"`
		Health         string         `json:"health"`
		RestartCount   int            `json:"restart_count"`
		StableSeconds  int            `json:"stable_seconds"`
		FatalCount     int            `json:"fatal_count"`
		GuardFailures  int            `json:"guard_failure_count"`
		Checks         map[string]any `json:"checks"`
		Result         string         `json:"result"`
	} `json:"deployment_facts"`
	ActivationReceipt struct {
		FinalState string `json:"final_state"`
		Host       string `json:"host"`
		Claude     struct {
			EventID        string `json:"event_id"`
			ProfileMode    string `json:"profile_mode"`
			ProfileSHA256  string `json:"profile_sha256"`
			WireSHA256     string `json:"wire_sha256"`
			ReleaseSHA256  string `json:"release_sha256"`
			BundleSHA256   string `json:"bundle_sha256"`
			ApprovalSHA256 string `json:"approval_sha256"`
		} `json:"claude"`
		Codex struct {
			EventID       string `json:"event_id"`
			ProfileMode   string `json:"profile_mode"`
			ProfileSHA256 string `json:"profile_sha256"`
			ReleaseSHA256 string `json:"release_sha256"`
		} `json:"codex"`
		PrivateEvidence struct {
			Path                string `json:"path"`
			SHA256              string `json:"sha256"`
			StageManifestSHA256 string `json:"stage_manifest_sha256"`
		} `json:"private_evidence"`
		Result string `json:"result"`
	} `json:"activation_receipt"`
	InventoryCurrent struct {
		Ingress map[string][]string `json:"ingress"`
		Egress  struct {
			PersonaStrict     []string `json:"persona_strict"`
			NonPersonaManaged []string `json:"non_persona_managed"`
			UnknownOAuth      string   `json:"unknown_oauth_egress"`
		} `json:"egress"`
	} `json:"inventory_current"`
	Safety struct {
		ComposeDownUsed       bool `json:"compose_down_used"`
		UnscopedPruneUsed     bool `json:"unscoped_prune_used"`
		PostgresRecreated     bool `json:"postgres_recreated"`
		RedisRecreated        bool `json:"redis_recreated"`
		KeeperRecreated       bool `json:"keeper_recreated"`
		ThreeXUIRecreated     bool `json:"three_x_ui_recreated"`
		DependenciesUnchanged bool `json:"dependencies_unchanged"`
		FirstSwitchAttempt    struct {
			Result           string `json:"result"`
			CandidateFailure bool   `json:"candidate_failure"`
			Cause            string `json:"cause"`
		} `json:"first_switch_attempt"`
		VircsLiveProbe struct {
			Performed            bool   `json:"performed"`
			Reason               string `json:"reason"`
			DMITEquivalentMatrix string `json:"dmit_equivalent_matrix"`
		} `json:"vircs_live_upstream_probe"`
		RetainedLegacy       []string `json:"retained_legacy"`
		RemovalReceiptIssued bool     `json:"removal_receipt_issued"`
		LegacyChainRemoved   bool     `json:"legacy_chain_removed"`
	} `json:"safety"`
	Transitions     []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionState string                            `json:"production_state"`
	RetirementState string                            `json:"retirement_state"`
	Result          string                            `json:"result"`
}

func loadClaudeFWHProductionAcceptance() (claudeFWHProductionAcceptancePackage, []byte, error) {
	var receipt claudeFWHProductionAcceptancePackage
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-h-production-acceptance-package.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("Claude FW-H 生产验收包尾部存在额外 JSON")
	}
	if claudeFWHSourceDigest(raw) != claudeFWHProductionAcceptanceSHA256 {
		return receipt, nil, errors.New("Claude FW-H 生产验收包摘要漂移")
	}
	return receipt, raw, nil
}

func claudeFWHProductionAcceptanceSupersedes(path, priorDigest, currentDigest string) bool {
	if upstreamMergeFrameworkTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHLegacyRetirementTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHBareChatRouteTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHThirdPartyStrictSourceTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, _, err := loadClaudeFWHProductionAcceptance()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			(transition.ToSHA256 == currentDigest ||
				claudeFWHLegacyRetirementTransitionSupersedes(
					path, transition.ToSHA256, currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestClaudeFWHProductionAcceptancePackageIsFrozen(t *testing.T) {
	receipt, raw, err := loadClaudeFWHProductionAcceptance()
	if err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "claude-fw-h-production-acceptance-package/v1" ||
		receipt.Phase != "FW-H" || receipt.IssuedAtUTC != "2026-08-21T15:18:57Z" ||
		receipt.DeploymentID != "claude-code-2.1.226-fw-h-e3d698a31-vircs" ||
		receipt.ProductionState != "restored_active" ||
		receipt.RetirementState != "deferred_retained_legacy" ||
		receipt.Result != "accepted" {
		t.Fatal("Claude FW-H 生产验收包顶层事实非法")
	}
	validateClaudeFWHProductionTarget(t, receipt)
	validateClaudeFWHProductionPredecessors(t, receipt)
	validateClaudeFWHProductionPromotion(t, receipt)
	validateClaudeFWHProductionEnvelopes(t, receipt)
	validateClaudeFWHProductionDeployment(t, receipt)
	validateClaudeFWHProductionActivation(t, receipt)
	validateClaudeFWHProductionInventoryAndSafety(t, receipt)
	validateClaudeFWHProductionTransitions(t, receipt)
	for _, marker := range []string{"sk-", "access_token", "refresh_token", "authorization: bearer"} {
		if strings.Contains(strings.ToLower(string(raw)), marker) {
			t.Fatalf("Claude FW-H 生产验收包包含秘密模式：%s", marker)
		}
	}
}

func validateClaudeFWHProductionTarget(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	target := receipt.Target
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		target.Platform != "linux/amd64" || target.Authentication != "claude.ai-oauth" ||
		target.Privacy != "essential-traffic-no-telemetry" || target.RequiredRules != 40 ||
		!slices.Equal(target.Models, []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}) ||
		target.ProfileSHA256 != ClaudeFWGProfileDigest || target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseSHA256 != ClaudeFWGReleaseDigest || target.BundleSHA256 != ClaudeFWGBundleDigest ||
		target.ApprovalSHA256 != ClaudeFWHInitialProductionApprovalDigest {
		t.Fatal("Claude FW-H 生产目标身份非法")
	}
}

func validateClaudeFWHProductionPredecessors(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	want := map[string]string{
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance.json":                                      "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9",
		"docs/egress/maintenance/claude-fw-h-source-transition.json":                                           claudeFWHSourceTransitionSHA256,
		"backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-approval.json": ClaudeFWHInitialProductionApprovalDigest,
	}
	for _, predecessor := range receipt.Predecessors {
		if want[predecessor.Path] != predecessor.SHA256 {
			t.Fatal("Claude FW-H 生产验收包前序引用非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(predecessor.Path)))
		if err != nil || claudeFWHSourceDigest(raw) != predecessor.SHA256 {
			t.Fatal("Claude FW-H 生产验收包前序摘要不一致")
		}
		delete(want, predecessor.Path)
	}
	if len(want) != 0 {
		t.Fatal("Claude FW-H 生产验收包前序集合不闭合")
	}
}

func validateClaudeFWHProductionPromotion(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	promotion := receipt.PromotionReceipt
	formal := promotion.FormalRelease
	invariants := promotion.Invariants
	if promotion.Candidate.CandidateID != "claude-code-2_1_226-fw-g-three-model-2f224831f" ||
		promotion.Candidate.Commit != "2f224831fcbad72e03782d109f9f94458457e2ff" ||
		formal.Commit != "e3d698a31574294eb30e7a31d477556a47378863" ||
		formal.Tree != "bde85a3d3ef0dc35f9c926895555b7dd757e191c" ||
		formal.SourceTreeSHA256 != "0f68c5334d56cb625d6bef39c01b05e17b6a8520f487002b46f60a3c71f862fd" ||
		formal.BinarySHA256 != "de4b0c74f28bb6f3ea8637b94ac0403c0eba8eb5c47d98f9e8eecdd47c1e6e74" ||
		formal.Version != "0.1.177-4-fw-h-e3d698a31" ||
		formal.Image != "sub2apiplus:fw-h-e3d698a31" ||
		formal.ImageID != "sha256:9b53120a8d61c46ae32a6336b1201f1c8736c710eb692cceb468ad3e1c4ce846" ||
		formal.ImageArchiveSHA256 != "299105723d24db3bd2b153b7ed6a9b16e376fadef03be198559e070f1b469033" ||
		!invariants.ProfileUnchanged || !invariants.WireUnchanged ||
		!invariants.ReleaseUnchanged || !invariants.BundleUnchanged ||
		!invariants.RequiredRulesUnchanged || invariants.CodexFinalWire != "zero-difference" ||
		promotion.Result != "passed" {
		t.Fatal("Claude FW-H 晋升收据非法")
	}
}

func validateClaudeFWHProductionEnvelopes(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	wantIngress := []string{
		"official-count-tokens-oauth",
		"official-messages-oauth",
		"third-party-count-tokens-oauth",
		"third-party-messages-oauth",
	}
	wantModels := []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}
	active := receipt.Envelopes.ActiveSupport
	rollback := receipt.Envelopes.RollbackOperational
	traffic := receipt.Envelopes.DeploymentTraffic
	if !slices.Equal(active.LogicalIngressIDs, wantIngress) ||
		!slices.Equal(active.Models, wantModels) || active.RequiredRules != 40 ||
		rollback.Kind != "frozen-legacy-deployment" ||
		rollback.ImageID != "sha256:9399e13dea365354311476b919b39d2c9d28d538d125fa7fc397745a7101c096" ||
		rollback.WireEvidence != "diagnostic-only" ||
		!slices.Equal(rollback.LogicalIngressIDs, wantIngress) ||
		rollback.DMITFunctionalMatrix != "passed" ||
		rollback.VircsMechanicalRollback != "passed" ||
		!slices.Equal(traffic.LogicalIngressIDs, wantIngress) ||
		!traffic.SubsetOfActiveAndRollback {
		t.Fatal("Claude FW-H 三个 Envelope 非法")
	}
}

func validateClaudeFWHProductionDeployment(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	wantStages := []string{"accepted_not_activated", "canary_passed", "active", "rollback_verified", "restored_active"}
	if len(receipt.DeploymentFacts) != len(wantStages) {
		t.Fatal("Claude FW-H DeploymentFact 阶段数量非法")
	}
	for index, fact := range receipt.DeploymentFacts {
		if fact.Sequence != index+1 || fact.Stage != wantStages[index] || fact.Result != "passed" {
			t.Fatal("Claude FW-H DeploymentFact 顺序非法")
		}
		if index == 0 && fact.PreviousFactID != nil || index > 0 &&
			(fact.PreviousFactID == nil || *fact.PreviousFactID != receipt.DeploymentFacts[index-1].FactID) {
			t.Fatal("Claude FW-H DeploymentFact 前序链非法")
		}
	}
	canary := receipt.DeploymentFacts[1]
	if canary.Host != "DMIT" || canary.ContainerID != "b1a9907430ba7450ea4ea296a9e71f99c4a0a54887bb7309d1d42c114fc91126" ||
		canary.ImageID != "sha256:9b53120a8d61c46ae32a6336b1201f1c8736c710eb692cceb468ad3e1c4ce846" ||
		canary.Checks["messages_three_models"] != "passed" ||
		canary.Checks["count_tokens_three_models"] != "passed" ||
		canary.Checks["unknown_model_and_query"] != "fail-close-passed" ||
		canary.Checks["retained_legacy_ingress"] != "passed" ||
		canary.Checks["claude_code_2_1_237"] != "passed" ||
		canary.Checks["claude_desktop"] != "passed" ||
		canary.Checks["kilocode_thinking_display_summarized"] != "passed" ||
		canary.Checks["rollback_and_restoration"] != "passed" ||
		canary.Checks["stable_seconds"] != float64(85) {
		t.Fatal("Claude FW-H DMIT canary 事实非法")
	}
	active := receipt.DeploymentFacts[2]
	rollback := receipt.DeploymentFacts[3]
	final := receipt.DeploymentFacts[4]
	if active.Host != "Vircs" || active.Health != "healthy" ||
		active.ContainerID != "2cd8d0a74d57d2958baa4981a9be25dcac7d33d5c81bc937da95731c00bb2bb8" ||
		active.ImageID != "sha256:9b53120a8d61c46ae32a6336b1201f1c8736c710eb692cceb468ad3e1c4ce846" ||
		rollback.Host != "Vircs" || rollback.Selector != "legacy" || rollback.Health != "healthy" ||
		rollback.ContainerID != "bf2bc80b2415b6a27325009f1f152657af30660b4f3cc96ff4160c4b2d75458a" ||
		rollback.ImageID != "sha256:9399e13dea365354311476b919b39d2c9d28d538d125fa7fc397745a7101c096" ||
		final.Host != "Vircs" || final.Selector != "active" || final.Health != "healthy" ||
		final.ContainerID != "5228fd302af75e6d00a496ac27163fb886ee99c13e39224e633fe777c4e14332" ||
		final.ImageID != "sha256:9b53120a8d61c46ae32a6336b1201f1c8736c710eb692cceb468ad3e1c4ce846" ||
		final.RestartCount != 0 || final.StableSeconds < 90 || final.FatalCount != 0 || final.GuardFailures != 0 {
		t.Fatal("Claude FW-H 回滚或 restored_active 事实非法")
	}
}

func validateClaudeFWHProductionActivation(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	activation := receipt.ActivationReceipt
	if activation.FinalState != "restored_active" || activation.Host != "Vircs" ||
		activation.Claude.EventID != "8d29fb955a3100228174ecb275adc1b92bb34451ac66bbde1a87c58620a3bb32" ||
		activation.Claude.ProfileMode != "active" ||
		activation.Claude.ProfileSHA256 != ClaudeFWGProfileDigest ||
		activation.Claude.WireSHA256 != claudeFWGWireDigest ||
		activation.Claude.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		activation.Claude.BundleSHA256 != ClaudeFWGBundleDigest ||
		activation.Claude.ApprovalSHA256 != ClaudeFWHInitialProductionApprovalDigest ||
		activation.Codex.EventID != "2d7136df4d50c1625bbfd3ddfcc21db9394fedb98842ddf7ecf0f12e868afd69" ||
		activation.Codex.ProfileMode != "active" ||
		activation.Codex.ProfileSHA256 != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		activation.Codex.ReleaseSHA256 != "caa1948405136feaf159cbfdf3c164c056c1ea38cac6f87a007cfe69ead38707" ||
		activation.PrivateEvidence.Path != "/root/sub2apiplus-fw-h-e3d698a31.eoGLa9/deployment-evidence.json" ||
		activation.PrivateEvidence.SHA256 != "aa3d07f67c1428b5655765080f4750ee1e8053c22c1ee9d977ab8e2d73e06b29" ||
		activation.PrivateEvidence.StageManifestSHA256 != "c46c191fe98f2cb0ed2041742bc95f975692cfd031b269c967ab5eed3576d8f6" ||
		activation.Result != "passed" {
		t.Fatal("Claude FW-H ActivationReceipt 非法")
	}
}

func validateClaudeFWHProductionInventoryAndSafety(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	if !slices.Equal(receipt.InventoryCurrent.Ingress["migrated_strict"], []string{
		"official-count-tokens-oauth",
		"official-messages-oauth",
		"third-party-count-tokens-oauth",
		"third-party-messages-oauth",
	}) || !slices.Equal(receipt.InventoryCurrent.Ingress["retained_legacy"], []string{
		"chat-completions-oauth", "responses-oauth",
	}) || !slices.Equal(receipt.InventoryCurrent.Ingress["rerouted"], []string{
		"codex-direct-rerouted",
	}) || !slices.Equal(receipt.InventoryCurrent.Egress.PersonaStrict, []string{
		"egress-claude-count-tokens",
		"egress-claude-lifecycle-hello",
		"egress-claude-mcp-servers",
		"egress-claude-messages-inference",
		"egress-claude-oauth-profile",
		"egress-claude-oauth-token-refresh",
		"egress-claude-policy-limits",
		"egress-claude-settings",
	}) || !slices.Equal(receipt.InventoryCurrent.Egress.NonPersonaManaged, []string{
		"egress-claude-account-test",
		"egress-claude-cookie-authorize",
		"egress-claude-cookie-organizations",
		"egress-claude-oauth-exchange",
		"egress-claude-oauth-refresh",
		"egress-claude-token-count",
		"egress-claude-upstream-models",
		"egress-claude-usage",
	}) ||
		receipt.InventoryCurrent.Egress.UnknownOAuth != "denied" {
		t.Fatal("Claude FW-H 当前 Inventory 非法")
	}
	safety := receipt.Safety
	if safety.ComposeDownUsed || safety.UnscopedPruneUsed || safety.PostgresRecreated ||
		safety.RedisRecreated || safety.KeeperRecreated || safety.ThreeXUIRecreated ||
		!safety.DependenciesUnchanged || safety.FirstSwitchAttempt.CandidateFailure ||
		safety.FirstSwitchAttempt.Result != "automatic_rollback_passed" ||
		safety.FirstSwitchAttempt.Cause != "校验脚本误用宿主端口且证据目录属主错误；应用本身已 healthy" ||
		safety.VircsLiveProbe.Performed ||
		safety.VircsLiveProbe.Reason != "唯一 Anthropic OAuth 账号不可调度；未擅自改变生产账号状态" ||
		safety.VircsLiveProbe.DMITEquivalentMatrix != "passed" ||
		!slices.Equal(safety.RetainedLegacy, []string{"chat-completions-oauth", "responses-oauth"}) ||
		safety.RemovalReceiptIssued || safety.LegacyChainRemoved {
		t.Fatal("Claude FW-H 生产安全事实非法")
	}
}

func validateClaudeFWHProductionTransitions(t *testing.T, receipt claudeFWHProductionAcceptancePackage) {
	t.Helper()
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		currentDigest := claudeFWHSourceDigest(raw)
		if err != nil || currentDigest != transition.ToSHA256 &&
			!claudeFWHLegacyRetirementTransitionSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) ||
			transition.FromSHA256 == transition.ToSHA256 || strings.TrimSpace(transition.Reason) == "" {
			t.Fatalf("Claude FW-H 生产 transition 非法：%s", transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if len(paths) != 5 || !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		t.Fatal("Claude FW-H 生产 transition 集合不闭合")
	}
}
