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
	claudeFWHStateDurabilitySourceSHA256 = "4d529a58d06a8a597da17b477c17fb5ee99dc6e2b6728e8adeaf3afb98f0fa60"
	claudeFWHStateDurabilityTestSHA256   = "5ab18f3424aa6b2300ae865999019c586814896a0d805c6fed6a48eaa7647d40"
)

type claudeFWHStateDurabilityRef struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeFWHStateDurabilityReceipt struct {
	SchemaVersion    string                        `json:"schema_version"`
	Date             string                        `json:"date"`
	Phase            string                        `json:"phase"`
	BaseCommit       string                        `json:"base_commit"`
	Scope            string                        `json:"scope"`
	Trigger          string                        `json:"trigger"`
	Predecessors     []claudeFWHStateDurabilityRef `json:"predecessors"`
	SourceTransition *claudeFWHStateDurabilityRef  `json:"source_transition,omitempty"`
	Release          struct {
		Version       string `json:"version"`
		ProfileSHA256 string `json:"profile_sha256"`
		WireSHA256    string `json:"wire_sha256"`
		ReleaseSHA256 string `json:"release_sha256"`
		BundleSHA256  string `json:"bundle_sha256"`
	} `json:"release"`
	Transitions  []changeset4SourceTransitionEntry `json:"transitions"`
	Verification []string                          `json:"verification"`
	Safety       struct {
		ProductionHost       string `json:"production_host"`
		ProductionSelector   string `json:"production_selector"`
		ProfileChanged       bool   `json:"profile_changed"`
		WireChanged          bool   `json:"wire_changed"`
		ReleaseBundleChanged bool   `json:"release_bundle_changed"`
		DeploymentPerformed  bool   `json:"deployment_performed"`
		DMITValidated        bool   `json:"dmit_validated"`
		VircsChanged         bool   `json:"vircs_production_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func readClaudeFWHStateDurabilityReceipt(
	path string,
) (claudeFWHStateDurabilityReceipt, []byte, error) {
	var receipt claudeFWHStateDurabilityReceipt
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
		return receipt, nil, errors.New("Claude 状态耐久性 transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHStateDurabilityReceipt(
	receipt claudeFWHStateDurabilityReceipt,
	raw []byte,
	testTransition bool,
) error {
	wantSchema := "official-egress-claude-fw-h-state-durability-source-transition/v1"
	wantDigest := claudeFWHStateDurabilitySourceSHA256
	if testTransition {
		wantSchema = "official-egress-claude-fw-h-state-durability-test-transition/v1"
		wantDigest = claudeFWHStateDurabilityTestSHA256
	}
	if claudeFWHSourceDigest(raw) != wantDigest || receipt.SchemaVersion != wantSchema ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "571370f3be67df3ccca594cd3e3b1eecf15e4421" ||
		receipt.Scope != "production_state_durability_correction" ||
		strings.TrimSpace(receipt.Trigger) == "" || receipt.Result != "passed" ||
		len(receipt.Transitions) == 0 || len(receipt.Verification) == 0 {
		return errors.New("Claude 状态耐久性 transition 顶层事实非法")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude 状态耐久性 Release 身份非法")
	}
	if receipt.Safety.ProductionHost != "DMIT" ||
		receipt.Safety.ProductionSelector != "active" || receipt.Safety.ProfileChanged ||
		receipt.Safety.WireChanged || receipt.Safety.ReleaseBundleChanged ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.DMITValidated ||
		receipt.Safety.VircsChanged {
		return errors.New("Claude 状态耐久性安全边界非法")
	}
	if testTransition {
		if receipt.SourceTransition == nil ||
			receipt.SourceTransition.Path != "docs/egress/maintenance/claude-fw-h-state-durability-source-transition.json" ||
			receipt.SourceTransition.SHA256 != claudeFWHStateDurabilitySourceSHA256 {
			return errors.New("Claude 状态耐久性测试 transition 缺少源码前序")
		}
	} else if receipt.SourceTransition != nil {
		return errors.New("Claude 状态耐久性源码 transition 错误引用自身")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.FromSHA256) != 64 ||
			len(transition.ToSHA256) != 64 || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			testTransition != strings.HasSuffix(transition.Path, "_test.go") {
			return errors.New("Claude 状态耐久性 transition 条目非法")
		}
		target, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if err != nil || claudeFWHSourceDigest(target) != transition.ToSHA256 {
			return errors.New("Claude 状态耐久性 transition 当前摘要不一致")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude 状态耐久性 transition 路径未排序或重复")
	}
	return nil
}

func loadClaudeFWHStateDurabilityReceipts() (
	[]claudeFWHStateDurabilityReceipt,
	error,
) {
	items := []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-state-durability-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-state-durability-test-transition.json", true},
	}
	receipts := make([]claudeFWHStateDurabilityReceipt, 0, len(items))
	for _, item := range items {
		receipt, raw, err := readClaudeFWHStateDurabilityReceipt(item.path)
		if err != nil || validateClaudeFWHStateDurabilityReceipt(receipt, raw, item.test) != nil {
			return nil, errors.New("Claude 状态耐久性 transition 无法验证")
		}
		receipts = append(receipts, receipt)
	}
	return receipts, nil
}

func claudeFWHStateDurabilityTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHResponseRequestIDTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipts, err := loadClaudeFWHStateDurabilityReceipts()
	if err != nil {
		return false
	}
	for _, receipt := range receipts {
		for _, transition := range receipt.Transitions {
			if transition.Path == path && transition.ToSHA256 == currentDigest &&
				(transition.FromSHA256 == priorDigest ||
					claudeFWHImmutableTransitionLedgerSupersedes(
						path, priorDigest, transition.FromSHA256,
					)) {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWHStateDurabilityTransitionsAreFrozen(t *testing.T) {
	if _, err := loadClaudeFWHStateDurabilityReceipts(); err != nil {
		t.Fatal(err)
	}
}
