package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	claudeFWGQueryFailCloseSourceSHA256 = "e1a6731634ba567489b74ad22cc0acd33f6ed1bbfa348a46a1273ba3c8c2e5ce"
	claudeFWGQueryFailCloseTestSHA256   = "decebbfe12ddc559e2b382cc3dc298c3367c1bc73ece22f5ed638968a9bab8e7"
)

type claudeFWGQueryFailCloseSourceReceipt struct {
	SchemaVersion             string                              `json:"schema_version"`
	Date                      string                              `json:"date"`
	Phase                     string                              `json:"phase"`
	BaseCommit                string                              `json:"base_commit"`
	Trigger                   string                              `json:"trigger"`
	Prior                     []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	Transitions               []changeset4SourceTransitionEntry   `json:"transitions"`
	ProductionSelectorChanged bool                                `json:"production_selector_changed"`
	VircsServiceChanged       bool                                `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                                `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                              `json:"codex_final_wire"`
	Result                    string                              `json:"result"`
}

type claudeFWGQueryFailCloseTestReceipt struct {
	SchemaVersion string                            `json:"schema_version"`
	Date          string                            `json:"date"`
	Phase         string                            `json:"phase"`
	BaseCommit    string                            `json:"base_commit"`
	Source        claudeFWGCountTokensTransitionRef `json:"source_transition"`
	Transitions   []changeset4SourceTransitionEntry `json:"transitions"`
	Result        string                            `json:"result"`
}

func loadClaudeFWGQueryFailCloseSourceReceipt() (
	claudeFWGQueryFailCloseSourceReceipt,
	error,
) {
	var receipt claudeFWGQueryFailCloseSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-query-fail-close-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGQueryFailCloseSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-query-fail-close-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "b7d04bcfd3a17bcb47a8de80863b8bc625f678d8" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Transitions) != 3 || receipt.ProductionSelectorChanged ||
		receipt.VircsServiceChanged || !receipt.DMITCandidateRebuild ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G query fail-close source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G query fail-close prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGQueryFailCloseTestReceipt(
	source claudeFWGQueryFailCloseSourceReceipt,
) (claudeFWGQueryFailCloseTestReceipt, error) {
	var receipt claudeFWGQueryFailCloseTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-query-fail-close-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGQueryFailCloseTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-query-fail-close-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit || receipt.Result != "passed" ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-query-fail-close-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGQueryFailCloseSourceSHA256 ||
		len(receipt.Transitions) != 4 {
		return receipt, errors.New("Claude FW-G query fail-close test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGQueryFailCloseTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	source, err := loadClaudeFWGQueryFailCloseSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGQueryFailCloseTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				transition.ToSHA256 == currentDigest {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWGQueryFailCloseTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGQueryFailCloseSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGQueryFailCloseTestReceipt(source)
	if err != nil {
		t.Fatal(err)
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			raw, err := os.ReadFile(
				filepath.Join("../../..", filepath.FromSlash(transition.Path)),
			)
			if err != nil {
				t.Fatal(err)
			}
			if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 &&
				!claudeFWGAliasRouteTransitionSupersedes(
					transition.Path, transition.ToSHA256, got,
				) && !claudeFWGSupportEnvelopeTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) && !claudeFWGDesktopIngressTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) {
				t.Fatalf(
					"Claude FW-G query fail-close transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
