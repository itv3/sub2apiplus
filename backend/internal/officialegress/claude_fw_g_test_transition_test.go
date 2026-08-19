package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const claudeFWGTestTransitionSHA256 = "e620702b24675126959f07963c99ea5a07ccf92c502b38d7cf95e295a390c8df"

type claudeFWGTestTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Source        struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition"`
	Transitions []changeset4SourceTransitionEntry `json:"transitions"`
	Result      string                            `json:"result"`
}

func readClaudeFWGTestTransition() (claudeFWGTestTransitionReceipt, []byte, error) {
	var receipt claudeFWGTestTransitionReceipt
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

func validateClaudeFWGTestTransition(receipt claudeFWGTestTransitionReceipt, raw []byte) error {
	if claudeFWGTestDigest(raw) != claudeFWGTestTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-test-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "12d21af2d7ca" || receipt.Result != "passed" ||
		len(receipt.Transitions) != 10 {
		return errors.New("Claude FW-G test transition 顶层事实非法")
	}
	if receipt.Source.Path != "docs/egress/maintenance/claude-fw-g-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGSourceTransitionSHA256 {
		return errors.New("Claude FW-G test transition 未固定对应 Source Transition")
	}
	sourceRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(receipt.Source.Path)))
	if err != nil || claudeFWGTestDigest(sourceRaw) != receipt.Source.SHA256 {
		return errors.New("Claude FW-G test transition 引用的 Source Transition 摘要不一致")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-G test transition 条目不完整")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Claude FW-G test transition 路径未排序或重复")
	}
	return nil
}

func claudeFWGTestTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, raw, err := readClaudeFWGTestTransition()
	if err != nil || validateClaudeFWGTestTransition(receipt, raw) != nil {
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
	}
	return false
}

func TestClaudeFWGTestTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWGTestTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWGTestTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	for _, transition := range receipt.Transitions {
		source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := claudeFWGTestDigest(source); got != transition.ToSHA256 {
			t.Fatalf(
				"Claude FW-G test transition 漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}

func claudeFWGTestDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
