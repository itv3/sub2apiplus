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

const (
	claudeFWGServiceInitialProfileDigest = "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
	claudeFWGServiceInitialReleaseDigest = "c1053492eabc0b10d9d5f92f807a1df0d507c777b64a528e938426350c0d5350"
	claudeFWGServiceInitialBundleDigest  = "4213ea92a7d76c4ef3aa318f4d93628cbcf675dc86566b107dddb70a70e6eb41"
	claudeFWGServiceInitialWireDigest    = "c1c3c8c83710c9afc7005f71fa45d0837484a6bd042f75c08e5cde5451822a3e"
	claudeFWGServiceCurrentWireDigest    = "a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d"
)

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
	if receipt.ArtifactIdentity.ProfileSHA256 != claudeFWGServiceInitialProfileDigest ||
		receipt.ArtifactIdentity.ReleaseSHA256 != claudeFWGServiceInitialReleaseDigest ||
		receipt.ArtifactIdentity.BundleSHA256 != claudeFWGServiceInitialBundleDigest ||
		receipt.ArtifactIdentity.WireSHA256 != claudeFWGServiceInitialWireDigest {
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
	if claudeFWHSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGModelCapabilitySourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWGQueryFailCloseSourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWGCountTokensSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	receipt, raw, err := readClaudeFWGServiceSourceTransition()
	if err != nil || validateClaudeFWGServiceSourceTransition(receipt, raw) != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path ||
			(transition.ToSHA256 != currentDigest &&
				!claudeFWHSourceTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				)) {
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

type claudeFWGServiceModelCapabilityTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Artifacts     struct {
		After struct {
			ProfileSHA256 string `json:"profile_sha256"`
			WireSHA256    string `json:"wire_sha256"`
			ReleaseSHA256 string `json:"release_sha256"`
			BundleSHA256  string `json:"bundle_sha256"`
		} `json:"after"`
	} `json:"artifacts"`
	Transitions []compatibilityClosureSourceTransition `json:"transitions"`
	Result      string                                 `json:"result"`
}

func claudeFWGModelCapabilitySourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/claude-fw-g-model-capability-source-transition.json",
	)
	if err != nil {
		return false
	}
	var receipt claudeFWGServiceModelCapabilityTransitionReceipt
	if json.Unmarshal(raw, &receipt) != nil ||
		receipt.SchemaVersion !=
			"official-egress-claude-model-capability-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "c662b59a3e558f7aa352cf3a424a603aa0c254eb" ||
		receipt.Result != "passed" || len(receipt.Transitions) != 16 ||
		receipt.Artifacts.After.ProfileSHA256 != officialegress.ClaudeFWGProfileDigest ||
		receipt.Artifacts.After.WireSHA256 != claudeFWGServiceCurrentWireDigest ||
		receipt.Artifacts.After.ReleaseSHA256 != officialegress.ClaudeFWGReleaseDigest ||
		receipt.Artifacts.After.BundleSHA256 != officialegress.ClaudeFWGBundleDigest {
		return false
	}
	for _, transition := range receipt.Transitions {
		currentReached := transition.ToSHA256 == currentDigest ||
			claudeFWHSourceTransitionSupersedesService(
				path, transition.ToSHA256, currentDigest,
			)
		if transition.Path == path && currentReached &&
			(transition.FromSHA256 == priorDigest ||
				claudeFWGServiceHistoricalSourceReaches(
					path, priorDigest, transition.FromSHA256,
				)) {
			return true
		}
	}
	return false
}

func claudeFWGServiceHistoricalSourceReaches(
	path string,
	priorDigest string,
	targetDigest string,
) bool {
	if priorDigest == targetDigest {
		return true
	}
	receipts := []struct {
		path   string
		digest string
	}{
		{"claude-fw-g-source-transition.json", claudeFWGServiceSourceTransitionSHA256},
		{"claude-fw-g-count-tokens-source-transition.json", "5d6c62d6dae7fb56a8871cb04db4253df73d92e2fa54b5ca6573df8e0816e56b"},
		{"claude-fw-g-query-fail-close-source-transition.json", "e1a6731634ba567489b74ad22cc0acd33f6ed1bbfa348a46a1273ba3c8c2e5ce"},
		{"claude-fw-g-alias-route-source-transition.json", "5d69b0c2ddb733686c2502bc352b2331229a1dcbad3ac6fa88e14dcdb24ded0d"},
		{"claude-fw-g-support-envelope-source-transition.json", "70ff3e96565a9c758e9708523d5e07a2b45038b36942fb02b6556d67dfe3e170"},
		{"claude-fw-g-desktop-ingress-source-transition.json", "be30a4f8f65fd95adab521093cc3c7da9f45796ebf6fdc7e461e92044a40a71d"},
		{"claude-fw-g-desktop-compat-source-transition.json", "7a01e9722ca6df4bc20c6822cc445c34d8541a2d9939560e3caff11f991596d7"},
		{"claude-fw-g-ingress-authority-source-transition.json", "ee434c67912f0841421ec2370f75e35207ff47014015fb557ce680b6c5d69954"},
		{"claude-fw-g-thinking-display-source-transition.json", "015b430cfd61cbf8434fe790a61b85e64a6425b8de6e5aeae366ea0649712d2a"},
		{"claude-fw-g-desktop-title-source-transition.json", "3d176f9b24549388936fd918e3eb2a160263eac372160611b42ab3dd62da6884"},
	}
	transitionSets := make([][]compatibilityClosureSourceTransition, 0, len(receipts))
	for _, frozen := range receipts {
		raw, err := os.ReadFile(filepath.Join(
			"../../../docs/egress/maintenance", frozen.path,
		))
		if err != nil || compatibilityClosureDigest(raw) != frozen.digest {
			return false
		}
		var receipt struct {
			Transitions []compatibilityClosureSourceTransition `json:"transitions"`
		}
		if json.Unmarshal(raw, &receipt) != nil {
			return false
		}
		transitionSets = append(transitionSets, receipt.Transitions)
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

func claudeFWGQueryFailCloseSourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	var receipt struct {
		SchemaVersion string                                 `json:"schema_version"`
		Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
	}
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/claude-fw-g-query-fail-close-source-transition.json",
	)
	if err != nil || compatibilityClosureDigest(raw) !=
		"e1a6731634ba567489b74ad22cc0acd33f6ed1bbfa348a46a1273ba3c8c2e5ce" ||
		json.Unmarshal(raw, &receipt) != nil ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-query-fail-close-source-transition/v1" {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func claudeFWGCountTokensSourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	var receipt struct {
		SchemaVersion string                                 `json:"schema_version"`
		Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
	}
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/claude-fw-g-count-tokens-source-transition.json",
	)
	if err != nil || compatibilityClosureDigest(raw) !=
		"5d6c62d6dae7fb56a8871cb04db4253df73d92e2fa54b5ca6573df8e0816e56b" ||
		json.Unmarshal(raw, &receipt) != nil ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-count-tokens-source-transition/v1" {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
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
		if got := compatibilityClosureDigest(source); got != transition.ToSHA256 &&
			!claudeFWHSourceTransitionSupersedesService(
				transition.Path, transition.ToSHA256, got,
			) &&
			!claudeFWGModelCapabilitySourceTransitionSupersedesService(
				transition.Path, transition.ToSHA256, got,
			) {
			t.Fatalf(
				"Claude FW-G source transition 漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}
