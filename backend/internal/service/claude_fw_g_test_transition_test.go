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
)

const (
	claudeFWGServiceTestTransitionSHA256   = "e620702b24675126959f07963c99ea5a07ccf92c502b38d7cf95e295a390c8df"
	claudeFWGServiceSourceTransitionSHA256 = "b84fa150c93856a7e3717c7053cd1e2d90f9a6adce259d68252d5aaf11fb6008"
)

type claudeFWGServiceTestTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Source        struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition"`
	Transitions []compatibilityClosureSourceTransition `json:"transitions"`
	Result      string                                 `json:"result"`
}

func readClaudeFWGServiceTestTransition() (claudeFWGServiceTestTransitionReceipt, []byte, error) {
	var receipt claudeFWGServiceTestTransitionReceipt
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-g-test-transition.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return receipt, nil, errors.New("Claude FW-G test transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWGServiceTestTransition(
	receipt claudeFWGServiceTestTransitionReceipt,
	raw []byte,
) error {
	if compatibilityClosureDigest(raw) != claudeFWGServiceTestTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "12d21af2d7ca" || receipt.Result != "passed" ||
		len(receipt.Transitions) != 10 {
		return errors.New("Claude FW-G test transition 顶层事实非法")
	}
	if receipt.Source.Path != "docs/egress/maintenance/claude-fw-g-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGServiceSourceTransitionSHA256 {
		return errors.New("Claude FW-G test transition 未固定 Source Transition")
	}
	source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(receipt.Source.Path)))
	if err != nil || compatibilityClosureDigest(source) != receipt.Source.SHA256 {
		return errors.New("Claude FW-G Source Transition 摘要不一致")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	seen := make(map[string]struct{}, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-G test transition 条目不完整")
		}
		if _, duplicate := seen[transition.Path]; duplicate {
			return errors.New("Claude FW-G test transition 路径重复")
		}
		seen[transition.Path] = struct{}{}
		paths = append(paths, transition.Path)
	}
	if !sort.StringsAreSorted(paths) {
		return errors.New("Claude FW-G test transition 路径未排序")
	}
	return nil
}

func claudeFWGTestTransitionSupersedesService(path, priorDigest, currentDigest string) bool {
	if upstreamMergeFrameworkTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	receipt, raw, err := readClaudeFWGServiceTestTransition()
	if err != nil || validateClaudeFWGServiceTestTransition(receipt, raw) != nil {
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

func TestClaudeFWGServiceTestTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWGServiceTestTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWGServiceTestTransition(receipt, raw); err != nil {
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
			) && !upstreamMergeFrameworkTransitionSupersedesService(
			transition.Path, transition.ToSHA256, got,
		) {
			t.Fatalf(
				"Claude FW-G test transition 漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}
