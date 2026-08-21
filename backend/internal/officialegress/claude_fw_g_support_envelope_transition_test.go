package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

const (
	claudeFWGSupportEnvelopeSourceSHA256 = "70ff3e96565a9c758e9708523d5e07a2b45038b36942fb02b6556d67dfe3e170"
	claudeFWGSupportEnvelopeTestSHA256   = "ae56ba2028807b88bb0d2d9a02bad25de5bc6c2a760289eddaf3df2cce531629"
)

func loadClaudeFWGSupportEnvelopeSourceReceipt() (
	claudeFWGQueryFailCloseSourceReceipt,
	error,
) {
	var receipt claudeFWGQueryFailCloseSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-support-envelope-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGSupportEnvelopeSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-support-envelope-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "5ad45153ade7c61e0fe38e3f9f951d24d32eef5c" ||
		len(receipt.Prior) != 2 || len(receipt.Transitions) != 3 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G SupportEnvelope source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G SupportEnvelope prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGSupportEnvelopeTestReceipt(
	source claudeFWGQueryFailCloseSourceReceipt,
) (claudeFWGQueryFailCloseTestReceipt, error) {
	var receipt claudeFWGQueryFailCloseTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-support-envelope-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGSupportEnvelopeTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-support-envelope-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit || receipt.Result != "passed" ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-support-envelope-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGSupportEnvelopeSourceSHA256 ||
		len(receipt.Transitions) != 5 {
		return receipt, errors.New("Claude FW-G SupportEnvelope test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGSupportEnvelopeTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWGDesktopTitleTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	source, err := loadClaudeFWGSupportEnvelopeSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGSupportEnvelopeTestReceipt(source)
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
				claudeFWGAliasRouteTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) ||
				claudeFWGQueryFailCloseTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) ||
				claudeFWGCountTokensSourceTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWGSupportEnvelopeTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGSupportEnvelopeSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGSupportEnvelopeTestReceipt(source)
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
				!claudeFWGDesktopIngressTransitionSupersedes(
					transition.Path, transition.ToSHA256, got,
				) && !claudeFWGDesktopTitleTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) {
				t.Fatalf(
					"Claude FW-G SupportEnvelope transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
