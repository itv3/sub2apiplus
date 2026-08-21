package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	claudeFWGSourceTransitionSHA256 = "b84fa150c93856a7e3717c7053cd1e2d90f9a6adce259d68252d5aaf11fb6008"
	claudeFWGInitialProfileDigest   = "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
	claudeFWGInitialReleaseDigest   = "c1053492eabc0b10d9d5f92f807a1df0d507c777b64a528e938426350c0d5350"
	claudeFWGInitialBundleDigest    = "4213ea92a7d76c4ef3aa318f4d93628cbcf675dc86566b107dddb70a70e6eb41"
	claudeFWGInitialWireDigest      = "c1c3c8c83710c9afc7005f71fa45d0837484a6bd042f75c08e5cde5451822a3e"
)

type claudeFWGSourceTransitionReceipt struct {
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
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

func readClaudeFWGSourceTransition() (claudeFWGSourceTransitionReceipt, []byte, error) {
	var receipt claudeFWGSourceTransitionReceipt
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

func validateClaudeFWGSourceTransition(
	receipt claudeFWGSourceTransitionReceipt,
	raw []byte,
) error {
	if claudeFWGSourceDigest(raw) != claudeFWGSourceTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		strings.TrimSpace(receipt.BaseCommit) == "" || len(receipt.Prior) != 2 ||
		receipt.ProductionSelectorChanged || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" || len(receipt.Transitions) != 7 {
		return errors.New("Claude FW-G source transition 顶层事实非法")
	}
	if receipt.ArtifactIdentity.ProfileSHA256 != claudeFWGInitialProfileDigest ||
		receipt.ArtifactIdentity.ReleaseSHA256 != claudeFWGInitialReleaseDigest ||
		receipt.ArtifactIdentity.BundleSHA256 != claudeFWGInitialBundleDigest ||
		receipt.ArtifactIdentity.WireSHA256 != claudeFWGInitialWireDigest {
		return fmt.Errorf(
			"Claude FW-G source transition 制品身份不一致：profile=%s/%s release=%s/%s bundle=%s/%s wire=%s/%s",
			receipt.ArtifactIdentity.ProfileSHA256, claudeFWGInitialProfileDigest,
			receipt.ArtifactIdentity.ReleaseSHA256, claudeFWGInitialReleaseDigest,
			receipt.ArtifactIdentity.BundleSHA256, claudeFWGInitialBundleDigest,
			receipt.ArtifactIdentity.WireSHA256, claudeFWGInitialWireDigest,
		)
	}
	priorPaths := make([]string, 0, len(receipt.Prior))
	for _, prior := range receipt.Prior {
		if strings.TrimSpace(prior.Path) == "" || strings.TrimSpace(prior.SHA256) == "" {
			return errors.New("Claude FW-G prior transition 不完整")
		}
		rawPrior, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGSourceDigest(rawPrior) != prior.SHA256 {
			return errors.New("Claude FW-G prior transition 摘要不一致")
		}
		priorPaths = append(priorPaths, prior.Path)
	}
	if !slices.IsSorted(priorPaths) {
		return errors.New("Claude FW-G prior transition 未排序")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-G source transition 条目不完整")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Claude FW-G source transition 路径未排序或重复")
	}
	return nil
}

func loadClaudeFWGSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receipt, raw, err := readClaudeFWGSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWGSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		result[transition.Path] = transition
	}
	return result
}

func claudeFWGSourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if claudeFWGCountTokensSourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, raw, err := readClaudeFWGSourceTransition()
	if err != nil || validateClaudeFWGSourceTransition(receipt, raw) != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		if transition.FromSHA256 == priorDigest ||
			fwEObservationSourceTransitionSupersedes(path, priorDigest, transition.FromSHA256) {
			return true
		}
		if claudeFWGCatalogHistoryReaches(path, priorDigest, transition.FromSHA256) {
			return true
		}
	}
	return false
}

func claudeFWGCatalogHistoryReaches(path, priorDigest, targetDigest string) bool {
	raw, err := os.ReadFile("../../../docs/egress/maintenance/catalog-generic-bind-compat-retirement.json")
	if err != nil || catalogGenericBindRetirementDigest(raw) != catalogGenericBindRetirementSHA256 {
		return false
	}
	var receipt catalogGenericBindRetirementReceipt
	if json.Unmarshal(raw, &receipt) != nil {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			fwEObservationSourceTransitionSupersedes(path, transition.ToSHA256, targetDigest) {
			return true
		}
	}
	return false
}

func TestClaudeFWGSourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWGSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWGSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	for _, transition := range receipt.Transitions {
		source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := claudeFWGSourceDigest(source); got != transition.ToSHA256 &&
			!claudeFWGModelCapabilityTransitionSupersedes(
				transition.Path, transition.ToSHA256, got,
			) {
			t.Fatalf("Claude FW-G source transition 漂移：path=%s got=%s want=%s",
				transition.Path, got, transition.ToSHA256)
		}
	}
}

func claudeFWGSourceDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
