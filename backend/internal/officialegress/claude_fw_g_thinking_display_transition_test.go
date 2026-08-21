package officialegress

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	claudeFWGThinkingDisplaySourceSHA256 = "015b430cfd61cbf8434fe790a61b85e64a6425b8de6e5aeae366ea0649712d2a"
	claudeFWGThinkingDisplayTestSHA256   = "b23717fe96bfb4af9bb1c5b81c797911ef0c0aa59db375c078d83a87e3effa6f"
)

type claudeFWGThinkingDisplayEvidenceRef struct {
	State  string `json:"state"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeFWGThinkingDisplaySourceReceipt struct {
	SchemaVersion    string                              `json:"schema_version"`
	Date             string                              `json:"date"`
	Phase            string                              `json:"phase"`
	BaseCommit       string                              `json:"base_commit"`
	Trigger          string                              `json:"trigger"`
	Prior            []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	OfficialEvidence struct {
		TargetVersion         string                                `json:"target_version"`
		Platform              string                                `json:"platform"`
		Authentication        string                                `json:"authentication"`
		Privacy               string                                `json:"privacy"`
		CaptureHost           string                                `json:"capture_host"`
		CaptureRoot           string                                `json:"capture_root"`
		FieldOrder            []string                              `json:"field_order"`
		DisplayValues         []string                              `json:"display_values"`
		Evidence              []claudeFWGThinkingDisplayEvidenceRef `json:"evidence"`
		ProductionDifferences []json.RawMessage                     `json:"production_differences"`
	} `json:"official_evidence"`
	ArtifactTransition struct {
		ProfileSHA256                      string `json:"profile_sha256"`
		ReleaseSHA256                      string `json:"release_sha256"`
		ReleaseBundleSHA256                string `json:"release_bundle_sha256"`
		WireBeforeSHA256                   string `json:"wire_before_sha256"`
		WireAfterSHA256                    string `json:"wire_after_sha256"`
		RequiredRulesBeforeSHA256          string `json:"required_rules_before_sha256"`
		RequiredRulesAfterSHA256           string `json:"required_rules_after_sha256"`
		ImplementationCoverageBeforeSHA256 string `json:"implementation_coverage_before_sha256"`
		ImplementationCoverageAfterSHA256  string `json:"implementation_coverage_after_sha256"`
		RequiredRuleCountBefore            int    `json:"required_rule_count_before"`
		RequiredRuleCountAfter             int    `json:"required_rule_count_after"`
		AtomicAssertionCountBefore         int    `json:"atomic_assertion_count_before"`
		AtomicAssertionCountAfter          int    `json:"atomic_assertion_count_after"`
	} `json:"artifact_transition"`
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

type claudeFWGThinkingDisplayTestReceipt struct {
	SchemaVersion             string                            `json:"schema_version"`
	Date                      string                            `json:"date"`
	Phase                     string                            `json:"phase"`
	BaseCommit                string                            `json:"base_commit"`
	Source                    claudeFWGCountTokensTransitionRef `json:"source_transition"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	RequiredRules             int                               `json:"required_rule_count"`
	AtomicRules               int                               `json:"atomic_assertion_count"`
	WireSHA256                string                            `json:"wire_sha256"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

func loadClaudeFWGThinkingDisplaySourceReceipt() (
	claudeFWGThinkingDisplaySourceReceipt,
	error,
) {
	var receipt claudeFWGThinkingDisplaySourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-thinking-display-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGThinkingDisplaySourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-thinking-display-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "86b67672e6abdd49801f54cc9062a8403759372f" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Dispositions) != 3 || len(receipt.Transitions) != 12 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G thinking.display source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if readErr != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G thinking.display prior transition 摘要不一致")
		}
	}
	evidence := receipt.OfficialEvidence
	if evidence.TargetVersion != ClaudeFWGVersion || evidence.Platform != "linux/amd64" ||
		evidence.Authentication != "claude.ai-oauth" ||
		evidence.Privacy != "essential-traffic-no-telemetry" ||
		evidence.CaptureHost != "Vircs" || strings.TrimSpace(evidence.CaptureRoot) == "" ||
		!slices.Equal(evidence.FieldOrder, []string{"type", "display"}) ||
		!slices.Equal(evidence.DisplayValues, []string{"summarized", "omitted"}) ||
		len(evidence.Evidence) != 4 || len(evidence.ProductionDifferences) != 0 {
		return receipt, errors.New("Claude FW-G thinking.display 官方证据不完整")
	}
	expectedEvidence := map[string]string{
		"default":                   "ca039dfcc431f7980bade1d06302da3703c87c350eac504b7b89161a87e34097",
		"summarized":                "454962fdfd52d81ab8200e78b6d158dbc3595c94810737a3c6dd58ad2db096f9",
		"omitted":                   "b958407da791047ac69fe05ac5c5fb93e87c839942d4a321d5140650ae49efee",
		"production_change_receipt": "e583e7506bf9aae250a9c20e1ba537d08a366ff6fc5d36a4be5cf74baf190426",
	}
	for _, ref := range evidence.Evidence {
		if expectedEvidence[ref.State] != ref.SHA256 || strings.TrimSpace(ref.Path) == "" {
			return receipt, errors.New("Claude FW-G thinking.display 证据引用非法")
		}
		delete(expectedEvidence, ref.State)
	}
	if len(expectedEvidence) != 0 {
		return receipt, errors.New("Claude FW-G thinking.display 证据状态缺失")
	}
	artifact := receipt.ArtifactTransition
	if artifact.ProfileSHA256 != ClaudeFWGProfileDigest ||
		artifact.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		artifact.ReleaseBundleSHA256 != ClaudeFWGBundleDigest ||
		artifact.WireBeforeSHA256 != claudeFWGInitialWireDigest ||
		artifact.WireAfterSHA256 != claudeFWGWireDigest ||
		artifact.RequiredRulesBeforeSHA256 !=
			"50261962778b8a7cf85f2dd01a8057f8004e92c0978456e88d9457d4ef8030b3" ||
		artifact.RequiredRulesAfterSHA256 != claudeFWGRequiredRulesManifestSHA256 ||
		artifact.ImplementationCoverageBeforeSHA256 !=
			"79c208e3775b3dfdc3f62ea95548550801bf2aea96e67d5010b390f894ec008c" ||
		artifact.ImplementationCoverageAfterSHA256 != claudeFWGImplementationCoverageSHA256 ||
		artifact.RequiredRuleCountBefore != 40 || artifact.RequiredRuleCountAfter != 40 ||
		artifact.AtomicAssertionCountBefore != 110 || artifact.AtomicAssertionCountAfter != 110 {
		return receipt, errors.New("Claude FW-G thinking.display 制品迁移身份非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGThinkingDisplayTestReceipt(
	source claudeFWGThinkingDisplaySourceReceipt,
) (claudeFWGThinkingDisplayTestReceipt, error) {
	var receipt claudeFWGThinkingDisplayTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-thinking-display-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGThinkingDisplayTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-thinking-display-test-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-thinking-display-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGThinkingDisplaySourceSHA256 ||
		len(receipt.Transitions) != 7 || receipt.RequiredRules != 40 ||
		receipt.AtomicRules != 110 || receipt.WireSHA256 != claudeFWGWireDigest ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G thinking.display test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGThinkingDisplayTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	source, err := loadClaudeFWGThinkingDisplaySourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGThinkingDisplayTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.ToSHA256 == currentDigest &&
				claudeFWGThinkingDisplayPriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func claudeFWGThinkingDisplayPriorReaches(path, priorDigest, targetDigest string) bool {
	if priorDigest == targetDigest ||
		claudeFWGIngressAuthorityPriorReaches(path, priorDigest, targetDigest) {
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
				if !direct && !claudeFWGIngressAuthorityPriorReaches(
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

func TestClaudeFWGThinkingDisplayTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGThinkingDisplaySourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGThinkingDisplayTestReceipt(source)
	if err != nil {
		t.Fatal(err)
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			raw, readErr := os.ReadFile(
				filepath.Join("../../..", filepath.FromSlash(transition.Path)),
			)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 {
				t.Fatalf(
					"Claude FW-G thinking.display transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
	if _, err := loadClaudeFWGWire(); err != nil {
		t.Fatal(err)
	}
}
