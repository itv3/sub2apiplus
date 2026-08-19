package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

const claudeFWGServiceWireDigest = "c1c3c8c83710c9afc7005f71fa45d0837484a6bd042f75c08e5cde5451822a3e"

type claudeFWGServiceSourceTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Prior         []struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"prior_transitions"`
	ArtifactIdentity struct {
		ProfileSHA256 string `json:"profile_sha256"`
		ReleaseSHA256 string `json:"release_sha256"`
		BundleSHA256  string `json:"bundle_sha256"`
		WireSHA256    string `json:"wire_sha256"`
	} `json:"artifact_identity"`
	Transitions               []compatibilityClosureSourceTransition `json:"transitions"`
	ProductionSelectorChanged bool                                   `json:"production_selector_changed"`
	CodexFinalWire            string                                 `json:"codex_final_wire"`
	Result                    string                                 `json:"result"`
}

func readClaudeFWGServiceSourceTransition() (claudeFWGServiceSourceTransitionReceipt, []byte, error) {
	var receipt claudeFWGServiceSourceTransitionReceipt
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-g-source-transition.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return receipt, nil, errors.New("Claude FW-G source transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWGServiceSourceTransition(
	receipt claudeFWGServiceSourceTransitionReceipt,
	raw []byte,
) error {
	if compatibilityClosureDigest(raw) != claudeFWGServiceSourceTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "12d21af2d7ca" || len(receipt.Prior) != 2 ||
		receipt.ProductionSelectorChanged || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" || len(receipt.Transitions) != 7 {
		return errors.New("Claude FW-G source transition 顶层事实非法")
	}
	if receipt.ArtifactIdentity.ProfileSHA256 != officialegress.ClaudeFWGProfileDigest ||
		receipt.ArtifactIdentity.ReleaseSHA256 != officialegress.ClaudeFWGReleaseDigest ||
		receipt.ArtifactIdentity.BundleSHA256 != officialegress.ClaudeFWGBundleDigest ||
		receipt.ArtifactIdentity.WireSHA256 != claudeFWGServiceWireDigest {
		return errors.New("Claude FW-G source transition 制品身份不一致")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	seen := make(map[string]struct{}, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-G source transition 条目不完整")
		}
		if _, duplicate := seen[transition.Path]; duplicate {
			return errors.New("Claude FW-G source transition 路径重复")
		}
		seen[transition.Path] = struct{}{}
		paths = append(paths, transition.Path)
	}
	if !sort.StringsAreSorted(paths) {
		return errors.New("Claude FW-G source transition 路径未排序")
	}
	return nil
}

func claudeFWGSourceTransitionSupersedesService(path, priorDigest, currentDigest string) bool {
	receipt, raw, err := readClaudeFWGServiceSourceTransition()
	if err != nil || validateClaudeFWGServiceSourceTransition(receipt, raw) != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		if transition.FromSHA256 == priorDigest ||
			fwEObservationSourceTransitionSupersedes(path, priorDigest, transition.FromSHA256) ||
			upstreamV0177SourceTransitionSupersedes(path, priorDigest, transition.FromSHA256) {
			return true
		}
	}
	return false
}

func TestClaudeFWGServiceSourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWGServiceSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWGServiceSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	for _, transition := range receipt.Transitions {
		source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := compatibilityClosureDigest(source); got != transition.ToSHA256 {
			t.Fatalf(
				"Claude FW-G source transition 漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}
