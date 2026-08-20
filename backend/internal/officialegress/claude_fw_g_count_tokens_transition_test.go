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

const (
	claudeFWGCountTokensSourceTransitionSHA256 = "5d6c62d6dae7fb56a8871cb04db4253df73d92e2fa54b5ca6573df8e0816e56b"
	claudeFWGCountTokensTestTransitionSHA256   = "f7b8f114ccda279ae5d3cebedce0846b495274fe9d6d8853733845e891ebea2b"
)

type claudeFWGCountTokensTransitionRef struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeFWGCountTokensSourceReceipt struct {
	SchemaVersion             string                              `json:"schema_version"`
	Date                      string                              `json:"date"`
	Phase                     string                              `json:"phase"`
	BaseCommit                string                              `json:"base_commit"`
	Prior                     []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	Transitions               []changeset4SourceTransitionEntry   `json:"transitions"`
	ProductionSelectorChanged bool                                `json:"production_selector_changed"`
	VircsServiceChanged       bool                                `json:"vircs_service_changed"`
	CodexFinalWire            string                              `json:"codex_final_wire"`
	Result                    string                              `json:"result"`
}

type claudeFWGCountTokensTestReceipt struct {
	SchemaVersion string                            `json:"schema_version"`
	Date          string                            `json:"date"`
	Phase         string                            `json:"phase"`
	BaseCommit    string                            `json:"base_commit"`
	Source        claudeFWGCountTokensTransitionRef `json:"source_transition"`
	Transitions   []changeset4SourceTransitionEntry `json:"transitions"`
	Result        string                            `json:"result"`
}

func readClaudeFWGCountTokensReceipt(path string, target any) ([]byte, error) {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, errors.New("Claude FW-G count_tokens transition 尾部存在额外 JSON")
	}
	return raw, nil
}

func validateClaudeFWGCountTokensSourceReceipt(
	receipt claudeFWGCountTokensSourceReceipt,
	raw []byte,
) error {
	if claudeFWGCountTokensDigest(raw) != claudeFWGCountTokensSourceTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-g-count-tokens-source-transition/v1" ||
		receipt.Date != "2026-08-20" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "76562bc4cacafc79363a83d154d5cc0fa5f68c72" ||
		len(receipt.Prior) != 2 || len(receipt.Transitions) != 7 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		receipt.CodexFinalWire != "zero_difference_required" || receipt.Result != "passed" {
		return errors.New("Claude FW-G count_tokens source transition 顶层事实非法")
	}
	for _, prior := range receipt.Prior {
		priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return errors.New("Claude FW-G count_tokens prior transition 摘要不一致")
		}
	}
	return validateClaudeFWGCountTokensTransitions(receipt.Transitions)
}

func validateClaudeFWGCountTokensTransitions(
	transitions []changeset4SourceTransitionEntry,
) error {
	paths := make([]string, 0, len(transitions))
	for _, transition := range transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-G count_tokens transition 条目不完整")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Claude FW-G count_tokens transition 路径未排序或重复")
	}
	return nil
}

func loadClaudeFWGCountTokensSourceReceipt() (claudeFWGCountTokensSourceReceipt, error) {
	var receipt claudeFWGCountTokensSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-count-tokens-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	return receipt, validateClaudeFWGCountTokensSourceReceipt(receipt, raw)
}

func claudeFWGCountTokensSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadClaudeFWGCountTokensSourceReceipt()
	if err != nil {
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

func TestClaudeFWGCountTokensTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGCountTokensSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	for _, transition := range source.Transitions {
		assertClaudeFWGCountTokensTransitionTarget(t, transition)
	}

	var testReceipt claudeFWGCountTokensTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-count-tokens-test-transition.json",
		&testReceipt,
	)
	if err != nil {
		t.Fatal(err)
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGCountTokensTestTransitionSHA256 ||
		testReceipt.SchemaVersion != "official-egress-claude-fw-g-count-tokens-test-transition/v1" ||
		testReceipt.Date != "2026-08-20" || testReceipt.Phase != "FW-G" ||
		testReceipt.BaseCommit != source.BaseCommit || testReceipt.Result != "passed" ||
		testReceipt.Source.Path != "docs/egress/maintenance/claude-fw-g-count-tokens-source-transition.json" ||
		testReceipt.Source.SHA256 != claudeFWGCountTokensSourceTransitionSHA256 ||
		len(testReceipt.Transitions) != 3 {
		t.Fatal("Claude FW-G count_tokens test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(testReceipt.Transitions); err != nil {
		t.Fatal(err)
	}
	for _, transition := range testReceipt.Transitions {
		assertClaudeFWGCountTokensTransitionTarget(t, transition)
	}
}

func assertClaudeFWGCountTokensTransitionTarget(
	t *testing.T,
	transition changeset4SourceTransitionEntry,
) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
	if err != nil {
		t.Fatal(err)
	}
	if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 &&
		!claudeFWGQueryFailCloseTransitionSupersedes(
			transition.Path, transition.ToSHA256, got,
		) && !claudeFWGAliasRouteTransitionSupersedes(
		transition.Path, transition.ToSHA256, got,
	) && !claudeFWGSupportEnvelopeTransitionSupersedes(
		transition.Path, transition.ToSHA256, got,
	) {
		t.Fatalf(
			"Claude FW-G count_tokens transition 漂移：path=%s got=%s want=%s",
			transition.Path, got, transition.ToSHA256,
		)
	}
}

func claudeFWGCountTokensDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
