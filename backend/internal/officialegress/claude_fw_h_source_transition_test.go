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

const claudeFWHSourceTransitionSHA256 = "b4638d412cfcd34e144dfbcce9b2326d2f6ed4df1987b55cac2c3209d3a5aa5f"

type claudeFWHSourceTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	ApprovalFact  struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"approval_fact"`
	PriorReceipts []struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"prior_receipts"`
	Release struct {
		Version       string   `json:"version"`
		Models        []string `json:"models"`
		ProfileSHA256 string   `json:"profile_sha256"`
		WireSHA256    string   `json:"wire_sha256"`
		ReleaseSHA256 string   `json:"release_sha256"`
		BundleSHA256  string   `json:"bundle_sha256"`
	} `json:"release"`
	Transitions []changeset4SourceTransitionEntry `json:"transitions"`
	Safety      struct {
		ClaudeSelectorScope    string   `json:"claude_selector_scope"`
		CandidateCatalog       string   `json:"candidate_catalog"`
		ProductionCatalog      string   `json:"production_catalog"`
		CodexFinalWire         string   `json:"codex_final_wire"`
		RetainedLegacy         []string `json:"retained_legacy"`
		RemovalReceiptAllowed  bool     `json:"removal_receipt_allowed"`
		DeploymentPerformed    bool     `json:"deployment_performed"`
		VircsProductionChanged bool     `json:"vircs_production_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func readClaudeFWHSourceTransition() (claudeFWHSourceTransitionReceipt, []byte, error) {
	var receipt claudeFWHSourceTransitionReceipt
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-h-source-transition.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("Claude FW-H source transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHSourceTransition(
	receipt claudeFWHSourceTransitionReceipt,
	raw []byte,
) error {
	if claudeFWHSourceDigest(raw) != claudeFWHSourceTransitionSHA256 ||
		receipt.SchemaVersion != "official-egress-claude-fw-h-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "619a63818f49ecc0b99bf4a16b7d776f939a452e" ||
		receipt.ApprovalFact.Path !=
			"backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-approval.json" ||
		receipt.ApprovalFact.SHA256 != ClaudeFWHProductionApprovalDigest ||
		len(receipt.PriorReceipts) != 3 || len(receipt.Transitions) != 27 ||
		receipt.Result != "passed" {
		return errors.New("Claude FW-H source transition 顶层事实非法")
	}
	approvalRaw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(receipt.ApprovalFact.Path),
	))
	if err != nil || claudeFWHSourceDigest(approvalRaw) != receipt.ApprovalFact.SHA256 {
		return errors.New("Claude FW-H source transition ApprovalFact 摘要不一致")
	}
	wantPrior := map[string]string{
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-source-transition.json": "d03add436e6e970bbaca3947d7fdf9a50a43879f78a3f89e49bcf1534d28bc56",
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-test-transition.json":   "089b0f1c13f1e2a34aa982f8e2220c4a8c4149df1746d932ac14daa40babdb61",
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance.json":                "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9",
	}
	for _, prior := range receipt.PriorReceipts {
		if wantPrior[prior.Path] != prior.SHA256 {
			return errors.New("Claude FW-H source transition 前序引用非法")
		}
		rawPrior, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWHSourceDigest(rawPrior) != prior.SHA256 {
			return errors.New("Claude FW-H source transition 前序摘要不一致")
		}
		delete(wantPrior, prior.Path)
	}
	if len(wantPrior) != 0 {
		return errors.New("Claude FW-H source transition 前序集合不闭合")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		!slices.Equal(receipt.Release.Models, []string{
			"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
		}) || receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude FW-H source transition Release 身份非法")
	}
	safety := receipt.Safety
	if safety.ClaudeSelectorScope != "independent" ||
		safety.CandidateCatalog != claudeFWGCandidateChangeset ||
		safety.ProductionCatalog != claudeFWHProductionChangeset ||
		safety.CodexFinalWire != "zero_difference_required" ||
		!slices.Equal(safety.RetainedLegacy, []string{
			"chat-completions-oauth", "responses-oauth",
		}) || safety.RemovalReceiptAllowed || safety.DeploymentPerformed ||
		safety.VircsProductionChanged {
		return errors.New("Claude FW-H source transition 安全不变量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-H source transition 条目不完整")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude FW-H source transition 路径未排序或重复")
	}
	return nil
}

func loadClaudeFWHSourceTransition(t *testing.T) map[string]changeset4SourceTransitionEntry {
	t.Helper()
	receipt, raw, err := readClaudeFWHSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	result := make(map[string]changeset4SourceTransitionEntry, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		result[transition.Path] = transition
	}
	return result
}

// claudeFWHSourceTransitionSupersedes 只承认本轮收据中的精确 path/from/to，
// 不覆盖或改写任何 FW-G 历史事实。
func claudeFWHSourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, raw, err := readClaudeFWHSourceTransition()
	if err != nil || validateClaudeFWHSourceTransition(receipt, raw) != nil {
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

func TestClaudeFWHSourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWHSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	for _, transition := range receipt.Transitions {
		source, readErr := os.ReadFile(
			filepath.Join("../../..", filepath.FromSlash(transition.Path)),
		)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := claudeFWHSourceDigest(source); got != transition.ToSHA256 {
			t.Fatalf(
				"Claude FW-H source transition 漂移：path=%s got=%s want=%s",
				transition.Path, got, transition.ToSHA256,
			)
		}
	}
}

func claudeFWHSourceDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
