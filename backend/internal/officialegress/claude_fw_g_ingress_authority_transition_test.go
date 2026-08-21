package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	claudeFWGIngressAuthoritySourceSHA256 = "ee434c67912f0841421ec2370f75e35207ff47014015fb557ce680b6c5d69954"
	claudeFWGIngressAuthorityTestSHA256   = "027fc30210229e84212551080f62bbffaf767f47f1c55526677e8600df2af208"
)

type claudeFWGIngressAuthoritySourceReceipt struct {
	SchemaVersion   string                              `json:"schema_version"`
	Date            string                              `json:"date"`
	Phase           string                              `json:"phase"`
	BaseCommit      string                              `json:"base_commit"`
	Trigger         string                              `json:"trigger"`
	Prior           []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	RuntimeEvidence struct {
		TargetVersion                 string   `json:"target_version"`
		DesktopAppVersion             string   `json:"desktop_app_version"`
		EmbeddedClaudeCodeVersion     string   `json:"embedded_claude_code_version"`
		CaptureMethod                 string   `json:"capture_method"`
		LocalClientConnectedToDMIT    bool     `json:"local_client_connected_to_dmit"`
		DMITAccountID                 int64    `json:"dmit_account_id"`
		UserAgents                    []string `json:"user_agents"`
		ObservedRejections            []string `json:"observed_rejections"`
		ControlledLocalAgentToolCount int      `json:"controlled_local_agent_tool_count"`
		ControlledLocalAgentBodyBytes int      `json:"controlled_local_agent_body_bytes"`
		RequestContentArchived        bool     `json:"request_content_archived"`
		AuthorizationArchived         bool     `json:"authorization_archived"`
	} `json:"runtime_evidence"`
	Dispositions []struct {
		Finding string `json:"finding"`
		Result  string `json:"result"`
	} `json:"dispositions"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                              `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	RequiredRuleCountBefore   int                               `json:"required_rule_count_before"`
	RequiredRuleCountAfter    int                               `json:"required_rule_count_after"`
	Result                    string                            `json:"result"`
}

type claudeFWGIngressAuthorityTestReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Source        struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition"`
	Transitions       []changeset4SourceTransitionEntry `json:"transitions"`
	RequiredRuleCount int                               `json:"required_rule_count"`
	CodexFinalWire    string                            `json:"codex_final_wire"`
	Result            string                            `json:"result"`
}

func loadClaudeFWGIngressAuthoritySourceReceipt() (
	claudeFWGIngressAuthoritySourceReceipt,
	error,
) {
	var receipt claudeFWGIngressAuthoritySourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-ingress-authority-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGIngressAuthoritySourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-ingress-authority-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "28e03d88c59685cdee4a7b2bd006eadfa8cddac0" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Dispositions) != 3 || len(receipt.Transitions) != 2 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.RequiredRuleCountBefore != 40 || receipt.RequiredRuleCountAfter != 40 ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G ingress authority source transition 顶层事实非法")
	}
	evidence := receipt.RuntimeEvidence
	if evidence.TargetVersion != "2.1.226" || evidence.DesktopAppVersion != "1.34493.0" ||
		evidence.EmbeddedClaudeCodeVersion != "2.1.235" ||
		evidence.CaptureMethod != "dmit-nginx-access-log-and-loopback-pcap-controlled-prompt" ||
		!evidence.LocalClientConnectedToDMIT || evidence.DMITAccountID != 100 ||
		len(evidence.UserAgents) != 2 || len(evidence.ObservedRejections) != 2 ||
		evidence.ControlledLocalAgentToolCount != 31 ||
		evidence.ControlledLocalAgentBodyBytes != 135190 ||
		evidence.RequestContentArchived || evidence.AuthorizationArchived {
		return receipt, errors.New("Claude FW-G ingress authority 运行证据不完整")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G ingress authority prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGIngressAuthorityTestReceipt(
	source claudeFWGIngressAuthoritySourceReceipt,
) (claudeFWGIngressAuthorityTestReceipt, error) {
	var receipt claudeFWGIngressAuthorityTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-ingress-authority-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGIngressAuthorityTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-ingress-authority-test-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-ingress-authority-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGIngressAuthoritySourceSHA256 ||
		len(receipt.Transitions) != 4 || receipt.RequiredRuleCount != 40 ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G ingress authority test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGIngressAuthorityTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWGThinkingDisplayTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	source, err := loadClaudeFWGIngressAuthoritySourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGIngressAuthorityTestReceipt(source)
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
				claudeFWGIngressAuthorityPriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func claudeFWGIngressAuthorityPriorReaches(path, priorDigest, targetDigest string) bool {
	if priorDigest == targetDigest ||
		claudeFWGDesktopCompatibilityPriorReaches(path, priorDigest, targetDigest) {
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
	reachable := map[string]struct{}{priorDigest: {}}
	for changed := true; changed; {
		changed = false
		for _, transitions := range [][]changeset4SourceTransitionEntry{
			source.Transitions,
			testReceipt.Transitions,
		} {
			for _, transition := range transitions {
				if transition.Path != path {
					continue
				}
				_, direct := reachable[transition.FromSHA256]
				if !direct && !claudeFWGDesktopCompatibilityPriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
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

func TestClaudeFWGIngressAuthorityTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGIngressAuthoritySourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGIngressAuthorityTestReceipt(source)
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
				!claudeFWGThinkingDisplayTransitionSupersedes(
					transition.Path, transition.ToSHA256, got,
				) {
				t.Fatalf(
					"Claude FW-G ingress authority transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
