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
	claudeFWHResponseRequestIDSourceSHA256 = "b4febb67d2690fe2a95c47258b66b6e5d7dea8b551f3635a281073709ae76b76"
	claudeFWHResponseRequestIDTestSHA256   = "cb540e6053aa693a9de3b210094f5a517bff2c430b5c9c49ade01458ba1cdeab"
)

type claudeFWHResponseRequestIDRef struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeFWHResponseRequestIDReceipt struct {
	SchemaVersion    string                          `json:"schema_version"`
	Date             string                          `json:"date"`
	Phase            string                          `json:"phase"`
	BaseCommit       string                          `json:"base_commit"`
	Scope            string                          `json:"scope"`
	Trigger          string                          `json:"trigger"`
	Predecessors     []claudeFWHResponseRequestIDRef `json:"predecessors"`
	SourceTransition *claudeFWHResponseRequestIDRef  `json:"source_transition,omitempty"`
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

func readClaudeFWHResponseRequestIDReceipt(
	path string,
) (claudeFWHResponseRequestIDReceipt, []byte, error) {
	var receipt claudeFWHResponseRequestIDReceipt
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
		return receipt, nil, errors.New("Claude request-id transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHResponseRequestIDReceipt(
	receipt claudeFWHResponseRequestIDReceipt,
	raw []byte,
	testTransition bool,
) error {
	wantSchema := "official-egress-claude-fw-h-response-request-id-source-transition/v1"
	wantDigest := claudeFWHResponseRequestIDSourceSHA256
	wantTransitionCount := 1
	wantPredecessors := map[string]string{
		"docs/egress/maintenance/claude-fw-h-final-acceptance-package.json":           claudeFWHFinalAcceptanceSHA256,
		"docs/egress/maintenance/claude-fw-h-state-durability-source-transition.json": claudeFWHStateDurabilitySourceSHA256,
		"docs/egress/maintenance/claude-fw-h-state-durability-test-transition.json":   claudeFWHStateDurabilityTestSHA256,
	}
	if testTransition {
		wantSchema = "official-egress-claude-fw-h-response-request-id-test-transition/v1"
		wantDigest = claudeFWHResponseRequestIDTestSHA256
		wantTransitionCount = 2
		wantPredecessors = map[string]string{
			"docs/egress/maintenance/claude-fw-h-state-durability-test-transition.json": claudeFWHStateDurabilityTestSHA256,
		}
	}
	if claudeFWHSourceDigest(raw) != wantDigest || receipt.SchemaVersion != wantSchema ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "a6a5ce61fbadac73a821debef94c0d18156e421c" ||
		receipt.Scope != "official_response_request_id_continuity_correction" ||
		strings.TrimSpace(receipt.Trigger) == "" || receipt.Result != "passed" ||
		len(receipt.Transitions) != wantTransitionCount || len(receipt.Verification) == 0 {
		return errors.New("Claude request-id transition 顶层事实非法")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude request-id transition Release 身份非法")
	}
	if receipt.Safety.ProductionHost != "DMIT" ||
		receipt.Safety.ProductionSelector != "active" || receipt.Safety.ProfileChanged ||
		receipt.Safety.WireChanged || receipt.Safety.ReleaseBundleChanged ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.DMITValidated ||
		receipt.Safety.VircsChanged {
		return errors.New("Claude request-id transition 安全边界非法")
	}
	if testTransition {
		if receipt.SourceTransition == nil ||
			receipt.SourceTransition.Path != "docs/egress/maintenance/claude-fw-h-response-request-id-source-transition.json" ||
			receipt.SourceTransition.SHA256 != claudeFWHResponseRequestIDSourceSHA256 {
			return errors.New("Claude request-id 测试 transition 缺少源码前序")
		}
	} else if receipt.SourceTransition != nil {
		return errors.New("Claude request-id 源码 transition 错误引用自身")
	}
	if len(receipt.Predecessors) != len(wantPredecessors) {
		return errors.New("Claude request-id transition 前序数量非法")
	}
	for _, predecessor := range receipt.Predecessors {
		want, ok := wantPredecessors[predecessor.Path]
		if !ok || predecessor.SHA256 != want || strings.TrimSpace(predecessor.Kind) == "" {
			return errors.New("Claude request-id transition 前序引用非法")
		}
		predecessorRaw, err := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(predecessor.Path),
		))
		if err != nil || claudeFWHSourceDigest(predecessorRaw) != predecessor.SHA256 {
			return errors.New("Claude request-id transition 前序摘要不一致")
		}
		delete(wantPredecessors, predecessor.Path)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude request-id transition 前序集合不闭合")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.FromSHA256) != 64 ||
			len(transition.ToSHA256) != 64 || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			testTransition != strings.HasSuffix(transition.Path, "_test.go") {
			return errors.New("Claude request-id transition 条目非法")
		}
		target, err := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		if err != nil || claudeFWHSourceDigest(target) != transition.ToSHA256 {
			return errors.New("Claude request-id transition 当前摘要不一致")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude request-id transition 路径未排序或重复")
	}
	return nil
}

func loadClaudeFWHResponseRequestIDReceipts() (
	[]claudeFWHResponseRequestIDReceipt,
	error,
) {
	items := []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-response-request-id-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-response-request-id-test-transition.json", true},
	}
	receipts := make([]claudeFWHResponseRequestIDReceipt, 0, len(items))
	for _, item := range items {
		receipt, raw, err := readClaudeFWHResponseRequestIDReceipt(item.path)
		if err != nil || validateClaudeFWHResponseRequestIDReceipt(
			receipt, raw, item.test,
		) != nil {
			return nil, errors.New("Claude request-id transition 无法验证")
		}
		receipts = append(receipts, receipt)
	}
	return receipts, nil
}

func claudeFWHResponseRequestIDTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipts, err := loadClaudeFWHResponseRequestIDReceipts()
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

func TestClaudeFWHResponseRequestIDTransitionsAreFrozen(t *testing.T) {
	if _, err := loadClaudeFWHResponseRequestIDReceipts(); err != nil {
		t.Fatal(err)
	}
}
