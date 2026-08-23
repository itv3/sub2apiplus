package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

const (
	claudeFWGAliasRouteSourceSHA256 = "5d69b0c2ddb733686c2502bc352b2331229a1dcbad3ac6fa88e14dcdb24ded0d"
	claudeFWGAliasRouteTestSHA256   = "3e2f6f4fd591b6e3ca602f88ac440bc15d75a65832ad603736c0a69178e9295d"
)

func loadClaudeFWGAliasRouteSourceReceipt() (
	claudeFWGQueryFailCloseSourceReceipt,
	error,
) {
	var receipt claudeFWGQueryFailCloseSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-alias-route-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGAliasRouteSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-alias-route-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "516db697cf889b39f7b966e04b08ba53191e1634" ||
		len(receipt.Prior) != 2 || len(receipt.Transitions) != 4 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G 裸路径别名 source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G 裸路径别名 prior transition 摘要不一致")
		}
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGAliasRouteTestReceipt(
	source claudeFWGQueryFailCloseSourceReceipt,
) (claudeFWGQueryFailCloseTestReceipt, error) {
	var receipt claudeFWGQueryFailCloseTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-alias-route-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGAliasRouteTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-alias-route-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit || receipt.Result != "passed" ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-alias-route-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGAliasRouteSourceSHA256 ||
		len(receipt.Transitions) != 5 {
		return receipt, errors.New("Claude FW-G 裸路径别名 test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGAliasRouteTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if upstreamMergeFrameworkTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	source, err := loadClaudeFWGAliasRouteSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGAliasRouteTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.ToSHA256 == currentDigest &&
				(transition.FromSHA256 == priorDigest ||
					claudeFWGQueryFailCloseTransitionSupersedes(
						path, priorDigest, transition.FromSHA256,
					)) {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWGAliasRouteTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGAliasRouteSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGAliasRouteTestReceipt(source)
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
				!claudeFWGSupportEnvelopeTransitionSupersedes(
					transition.Path, transition.ToSHA256, got,
				) && !claudeFWGDesktopIngressTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) && !upstreamMergeFrameworkTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) {
				t.Fatalf(
					"Claude FW-G 裸路径别名 transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
}
