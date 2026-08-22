package officialegress

import (
	"bytes"
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
	claudeFWHLegacyRetirementSourceSHA256 = "13918c77227fcfff5f44daa77d0382630e37998fd926bab2334cb9392bb7eabd"
	claudeFWHLegacyRetirementTestSHA256   = "6eae1f11f3abc5050ead6a31a7eb79ad5ef7e7066915fc2666fa45dcba8304e0"
)

type claudeFWHLegacyRetirementTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Scope         string `json:"scope"`
	ApprovalFact  struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"approval_fact"`
	Predecessors []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessors,omitempty"`
	SourceTransition *struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition,omitempty"`
	LogicalIngressIDs []string                          `json:"logical_ingress_ids"`
	Transitions       []changeset4SourceTransitionEntry `json:"transitions"`
	Gates             []string                          `json:"gates,omitempty"`
	Safety            struct {
		ProductionHost          string   `json:"production_host"`
		ProductionSelector      string   `json:"production_selector"`
		CandidateEnabled        bool     `json:"candidate_enabled"`
		RetainedLegacy          []string `json:"retained_legacy"`
		SetupTokenSemantics     string   `json:"setup_token_semantics"`
		APIKeySemantics         string   `json:"api_key_semantics"`
		ServiceAccountSemantics string   `json:"service_account_semantics"`
		RemovalReceiptAllowed   bool     `json:"removal_receipt_allowed"`
		DeploymentPerformed     bool     `json:"deployment_performed"`
		VircsProductionChanged  bool     `json:"vircs_production_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func readClaudeFWHLegacyRetirementTransition(
	path string,
) (claudeFWHLegacyRetirementTransitionReceipt, []byte, error) {
	var receipt claudeFWHLegacyRetirementTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("Claude FW-H 遗留退休 transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHLegacyRetirementTransition(
	receipt claudeFWHLegacyRetirementTransitionReceipt,
	raw []byte,
	testTransition bool,
) error {
	wantSchema := "official-egress-claude-fw-h-legacy-retirement-source-transition/v1"
	wantDigest := claudeFWHLegacyRetirementSourceSHA256
	if testTransition {
		wantSchema = "official-egress-claude-fw-h-legacy-retirement-test-transition/v1"
		wantDigest = claudeFWHLegacyRetirementTestSHA256
	}
	if claudeFWHSourceDigest(raw) != wantDigest || receipt.SchemaVersion != wantSchema ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "c51fb5d2cf052f47db9f27926d8340c2c218e2af" ||
		receipt.Scope != "legacy_retirement" || receipt.Result != "passed" ||
		receipt.ApprovalFact.Path != "backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json" ||
		receipt.ApprovalFact.SHA256 != ClaudeFWHProductionApprovalDigest {
		return errors.New("Claude FW-H 遗留退休 transition 顶层事实非法")
	}
	approvalRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(receipt.ApprovalFact.Path)))
	if err != nil || claudeFWHSourceDigest(approvalRaw) != receipt.ApprovalFact.SHA256 {
		return errors.New("Claude FW-H 遗留退休批准摘要不一致")
	}
	wantIngress := []string{
		"official-count-tokens-oauth", "official-messages-oauth",
		"third-party-count-tokens-oauth", "third-party-messages-oauth",
		"chat-completions-oauth", "responses-oauth",
	}
	if !slices.Equal(receipt.LogicalIngressIDs, wantIngress) ||
		receipt.Safety.ProductionHost != "DMIT" ||
		receipt.Safety.ProductionSelector != "active" || receipt.Safety.CandidateEnabled ||
		len(receipt.Safety.RetainedLegacy) != 0 ||
		receipt.Safety.SetupTokenSemantics != "non_persona_managed" ||
		receipt.Safety.APIKeySemantics != "retained" ||
		receipt.Safety.ServiceAccountSemantics != "retained" ||
		!receipt.Safety.RemovalReceiptAllowed || receipt.Safety.DeploymentPerformed ||
		receipt.Safety.VircsProductionChanged {
		return errors.New("Claude FW-H 遗留退休 transition 安全边界非法")
	}
	if testTransition {
		if len(receipt.Predecessors) != 0 || receipt.SourceTransition == nil ||
			receipt.SourceTransition.Path != "docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json" ||
			receipt.SourceTransition.SHA256 != claudeFWHLegacyRetirementSourceSHA256 ||
			len(receipt.Gates) == 0 {
			return errors.New("Claude FW-H 遗留退休测试 transition 前序非法")
		}
	} else if receipt.SourceTransition != nil || len(receipt.Predecessors) != 4 ||
		len(receipt.Gates) != 0 {
		return errors.New("Claude FW-H 遗留退休源码 transition 前序非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.FromSHA256) != 64 ||
			len(transition.ToSHA256) != 64 || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			testTransition != strings.HasSuffix(transition.Path, "_test.go") {
			return errors.New("Claude FW-H 遗留退休 transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if len(paths) == 0 || !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude FW-H 遗留退休 transition 路径未排序或重复")
	}
	return nil
}

func claudeFWHLegacyRetirementTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHLegacyRetirementTransition(item.path)
		if err != nil || validateClaudeFWHLegacyRetirementTransition(receipt, raw, item.test) != nil {
			return false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				transition.ToSHA256 == currentDigest {
				return true
			}
		}
	}
	return false
}

// claudeFWHLegacyRetirementTransitionPrior 返回本次退休变更前的精确摘要，供更早的
// 不可变 transition 先到达该摘要，再由本次收据承接到当前源码。
func claudeFWHLegacyRetirementTransitionPrior(path string, currentDigest string) (string, bool) {
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHLegacyRetirementTransition(item.path)
		if err != nil || validateClaudeFWHLegacyRetirementTransition(receipt, raw, item.test) != nil {
			return "", false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path == path && transition.ToSHA256 == currentDigest {
				return transition.FromSHA256, true
			}
		}
	}
	return "", false
}

func TestClaudeFWHLegacyRetirementTransitionsAreFrozen(t *testing.T) {
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHLegacyRetirementTransition(item.path)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateClaudeFWHLegacyRetirementTransition(receipt, raw, item.test); err != nil {
			t.Fatal(err)
		}
		for _, transition := range receipt.Transitions {
			source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
			if errors.Is(err, os.ErrNotExist) &&
				transition.ToSHA256 == claudeFWHSourceDigest(nil) {
				continue
			}
			if err != nil || claudeFWHSourceDigest(source) != transition.ToSHA256 {
				t.Fatalf("Claude FW-H 遗留退休摘要漂移：%s", transition.Path)
			}
		}
	}
}
