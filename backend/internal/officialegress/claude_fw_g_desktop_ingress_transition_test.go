package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

const (
	claudeFWGDesktopIngressSourceSHA256 = "be30a4f8f65fd95adab521093cc3c7da9f45796ebf6fdc7e461e92044a40a71d"
	claudeFWGDesktopIngressTestSHA256   = "edc54d5f2034dbf3bfefc8013fc874dfe4a95d94b1d78e554c85065b2a45f476"
)

func loadClaudeFWGDesktopIngressSourceReceipt() (
	claudeFWGQueryFailCloseSourceReceipt,
	error,
) {
	var receipt claudeFWGQueryFailCloseSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-ingress-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopIngressSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-ingress-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "4ada0887f95045e980c629f144150a59f7936862" ||
		len(receipt.Prior) != 2 || len(receipt.Transitions) != 2 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Desktop 入站 source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G Desktop 入站 prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGDesktopIngressTestReceipt(
	source claudeFWGQueryFailCloseSourceReceipt,
) (claudeFWGQueryFailCloseTestReceipt, error) {
	var receipt claudeFWGQueryFailCloseTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-ingress-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopIngressTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-ingress-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit || receipt.Result != "passed" ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-desktop-ingress-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGDesktopIngressSourceSHA256 ||
		len(receipt.Transitions) != 6 {
		return receipt, errors.New("Claude FW-G Desktop 入站 test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGDesktopIngressTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	source, err := loadClaudeFWGDesktopIngressSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGDesktopIngressTestReceipt(source)
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
				claudeFWGSupportEnvelopeTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) ||
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

func TestClaudeFWGDesktopIngressTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGDesktopIngressSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGDesktopIngressTestReceipt(source)
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
			if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 {
				t.Fatalf(
					"Claude FW-G Desktop 入站 transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
