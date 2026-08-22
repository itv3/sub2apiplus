package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	claudeFWGDesktopCompatibilitySourceSHA256 = "7a01e9722ca6df4bc20c6822cc445c34d8541a2d9939560e3caff11f991596d7"
	claudeFWGDesktopCompatibilityTestSHA256   = "bace2ffed0f1410d3f024879b1687e6a6fcff6253392db33f6a5c13b646fe1f5"
)

type claudeFWGDesktopCompatibilitySourceReceipt struct {
	SchemaVersion         string                              `json:"schema_version"`
	Date                  string                              `json:"date"`
	Phase                 string                              `json:"phase"`
	BaseCommit            string                              `json:"base_commit"`
	Trigger               string                              `json:"trigger"`
	Prior                 []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	InvalidatedAcceptance struct {
		CandidateID         string `json:"candidate_id"`
		PublicReceiptCommit string `json:"public_receipt_commit"`
		Reason              string `json:"reason"`
	} `json:"invalidated_acceptance"`
	OfficialEvidence struct {
		TargetVersion                string   `json:"target_version"`
		DesktopVersion               string   `json:"desktop_version"`
		DesktopBinarySHA256          string   `json:"desktop_binary_sha256"`
		CaptureMethod                string   `json:"capture_method"`
		LocalClaudeLoginUsed         bool     `json:"local_claude_login_used"`
		LocalClientConnectedToDMIT   bool     `json:"local_client_connected_to_dmit"`
		UserAgents                   []string `json:"user_agents"`
		DesktopToolCount             int      `json:"desktop_tool_count"`
		DesktopToolCatalogSHA256     string   `json:"desktop_tool_catalog_sha256"`
		TargetMessagesRequests       int      `json:"target_messages_requests"`
		TargetToolCatalogVariants    int      `json:"target_tool_catalog_variants"`
		TargetMaximumToolCatalogSize int      `json:"target_maximum_tool_catalog_size"`
		StandardDescriptorFields     []string `json:"standard_descriptor_fields"`
	} `json:"official_evidence"`
	Dispositions []struct {
		Finding string `json:"finding"`
		Result  string `json:"result"`
	} `json:"dispositions"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                              `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

func loadClaudeFWGDesktopCompatibilitySourceReceipt() (
	claudeFWGDesktopCompatibilitySourceReceipt,
	error,
) {
	var receipt claudeFWGDesktopCompatibilitySourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-compat-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopCompatibilitySourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-compat-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "d5604befd8f6700bb875703b5f2d012b122bf764" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Transitions) != 4 || len(receipt.Dispositions) != 3 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Desktop compatibility source transition 顶层事实非法")
	}
	if receipt.InvalidatedAcceptance.CandidateID != "claude-code-2_1_226-fw-g-651ccd518" ||
		receipt.InvalidatedAcceptance.PublicReceiptCommit != receipt.BaseCommit ||
		strings.TrimSpace(receipt.InvalidatedAcceptance.Reason) == "" {
		return receipt, errors.New("Claude FW-G Desktop compatibility 未冻结失效验收")
	}
	evidence := receipt.OfficialEvidence
	if evidence.TargetVersion != "2.1.226" || evidence.DesktopVersion != "2.1.234" ||
		evidence.DesktopBinarySHA256 !=
			"08d8700313697cbe730a25420c908a299ce52d56f0eb2cf4fac94cab5109bc57" ||
		evidence.CaptureMethod != "official-binary-loopback-fake-token" ||
		evidence.LocalClaudeLoginUsed || evidence.LocalClientConnectedToDMIT ||
		len(evidence.UserAgents) != 2 || evidence.DesktopToolCount != 20 ||
		evidence.DesktopToolCatalogSHA256 !=
			"e2d1c9b02742888e96bfbc4315f3b8211429ab7336d1c14fadf1d828c3bce0f6" ||
		evidence.TargetMessagesRequests != 123 || evidence.TargetToolCatalogVariants != 11 ||
		evidence.TargetMaximumToolCatalogSize != claudeDynamicToolCatalogLimit ||
		strings.Join(evidence.StandardDescriptorFields, ",") !=
			"name,description,input_schema" {
		return receipt, errors.New("Claude FW-G Desktop compatibility 官方证据不完整")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G Desktop compatibility prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGDesktopCompatibilityTestReceipt(
	source claudeFWGDesktopCompatibilitySourceReceipt,
) (claudeFWGQueryFailCloseTestReceipt, error) {
	var receipt claudeFWGQueryFailCloseTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-compat-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopCompatibilityTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-compat-test-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit || receipt.Result != "passed" ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-desktop-compat-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGDesktopCompatibilitySourceSHA256 ||
		len(receipt.Transitions) != 5 {
		return receipt, errors.New("Claude FW-G Desktop compatibility test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGDesktopCompatibilityTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHBareChatRouteTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGDesktopTitleTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGIngressAuthorityTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	source, err := loadClaudeFWGDesktopCompatibilitySourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGDesktopCompatibilityTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path != path || transition.ToSHA256 != currentDigest {
				continue
			}
			if transition.FromSHA256 == priorDigest ||
				claudeFWGDesktopCompatibilityPriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func claudeFWGDesktopCompatibilityPriorReaches(path, priorDigest, targetDigest string) bool {
	if priorDigest == targetDigest {
		return true
	}
	countSource, err := loadClaudeFWGCountTokensSourceReceipt()
	if err != nil {
		return false
	}
	var countTest claudeFWGCountTokensTestReceipt
	countTestRaw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-count-tokens-test-transition.json",
		&countTest,
	)
	if err != nil || claudeFWGCountTokensDigest(countTestRaw) != claudeFWGCountTokensTestTransitionSHA256 {
		return false
	}
	querySource, err := loadClaudeFWGQueryFailCloseSourceReceipt()
	if err != nil {
		return false
	}
	queryTest, err := loadClaudeFWGQueryFailCloseTestReceipt(querySource)
	if err != nil {
		return false
	}
	aliasSource, err := loadClaudeFWGAliasRouteSourceReceipt()
	if err != nil {
		return false
	}
	aliasTest, err := loadClaudeFWGAliasRouteTestReceipt(aliasSource)
	if err != nil {
		return false
	}
	supportSource, err := loadClaudeFWGSupportEnvelopeSourceReceipt()
	if err != nil {
		return false
	}
	supportTest, err := loadClaudeFWGSupportEnvelopeTestReceipt(supportSource)
	if err != nil {
		return false
	}
	desktopSource, err := loadClaudeFWGDesktopIngressSourceReceipt()
	if err != nil {
		return false
	}
	desktopTest, err := loadClaudeFWGDesktopIngressTestReceipt(desktopSource)
	if err != nil {
		return false
	}
	transitionSets := [][]changeset4SourceTransitionEntry{
		countSource.Transitions,
		countTest.Transitions,
		querySource.Transitions,
		queryTest.Transitions,
		aliasSource.Transitions,
		aliasTest.Transitions,
		supportSource.Transitions,
		supportTest.Transitions,
		desktopSource.Transitions,
		desktopTest.Transitions,
	}
	reachable := map[string]struct{}{priorDigest: {}}
	for changed := true; changed; {
		changed = false
		for _, transitions := range transitionSets {
			for _, transition := range transitions {
				if transition.Path != path {
					continue
				}
				if _, ok := reachable[transition.FromSHA256]; !ok {
					continue
				}
				if _, known := reachable[transition.ToSHA256]; known {
					continue
				}
				reachable[transition.ToSHA256] = struct{}{}
				changed = true
			}
		}
	}
	_, ok := reachable[targetDigest]
	return ok
}

func TestClaudeFWGDesktopCompatibilityTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGDesktopCompatibilitySourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGDesktopCompatibilityTestReceipt(source)
	if err != nil {
		t.Fatal(err)
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
			if err != nil {
				t.Fatal(err)
			}
			if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 &&
				!claudeFWGIngressAuthorityTransitionSupersedes(
					transition.Path, transition.ToSHA256, got,
				) && !claudeFWGDesktopTitleTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) {
				t.Fatalf(
					"Claude FW-G Desktop compatibility transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
