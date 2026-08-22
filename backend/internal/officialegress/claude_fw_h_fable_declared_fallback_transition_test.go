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
	"sync"
	"testing"
)

const (
	claudeFWHFableDeclaredFallbackSourceSHA256 = "e33912bec8e6ff619790fc187312f0d52817e07d6611a93a2d8a7dbfc46c6bc2"
	claudeFWHFableDeclaredFallbackTestSHA256   = "3092c2d422aea527ef5a2fbb4febbb57753fe5d4c0619a7232d819f7fefecf38"
)

var claudeFWHImmutableTransitionLedgerFiles = map[string]string{
	"docs/egress/maintenance/claude-fw-g-alias-route-source-transition.json":             "5d69b0c2ddb733686c2502bc352b2331229a1dcbad3ac6fa88e14dcdb24ded0d",
	"docs/egress/maintenance/claude-fw-g-alias-route-test-transition.json":               "3e2f6f4fd591b6e3ca602f88ac440bc15d75a65832ad603736c0a69178e9295d",
	"docs/egress/maintenance/claude-fw-g-count-tokens-source-transition.json":            "5d6c62d6dae7fb56a8871cb04db4253df73d92e2fa54b5ca6573df8e0816e56b",
	"docs/egress/maintenance/claude-fw-g-count-tokens-test-transition.json":              "f7b8f114ccda279ae5d3cebedce0846b495274fe9d6d8853733845e891ebea2b",
	"docs/egress/maintenance/claude-fw-g-desktop-compat-source-transition.json":          "7a01e9722ca6df4bc20c6822cc445c34d8541a2d9939560e3caff11f991596d7",
	"docs/egress/maintenance/claude-fw-g-desktop-compat-test-transition.json":            "bace2ffed0f1410d3f024879b1687e6a6fcff6253392db33f6a5c13b646fe1f5",
	"docs/egress/maintenance/claude-fw-g-desktop-ingress-source-transition.json":         "be30a4f8f65fd95adab521093cc3c7da9f45796ebf6fdc7e461e92044a40a71d",
	"docs/egress/maintenance/claude-fw-g-desktop-ingress-test-transition.json":           "edc54d5f2034dbf3bfefc8013fc874dfe4a95d94b1d78e554c85065b2a45f476",
	"docs/egress/maintenance/claude-fw-g-desktop-title-source-transition.json":           "3d176f9b24549388936fd918e3eb2a160263eac372160611b42ab3dd62da6884",
	"docs/egress/maintenance/claude-fw-g-desktop-title-test-transition.json":             "eecadfe40a4bf5f6e1e55ac2493d74557cccb0568e61d33ef0291c30ab5056d3",
	"docs/egress/maintenance/claude-fw-g-fable-desktop-title-source-transition.json":     "d03add436e6e970bbaca3947d7fdf9a50a43879f78a3f89e49bcf1534d28bc56",
	"docs/egress/maintenance/claude-fw-g-fable-desktop-title-test-transition.json":       "089b0f1c13f1e2a34aa982f8e2220c4a8c4149df1746d932ac14daa40babdb61",
	"docs/egress/maintenance/claude-fw-g-ingress-authority-source-transition.json":       "ee434c67912f0841421ec2370f75e35207ff47014015fb557ce680b6c5d69954",
	"docs/egress/maintenance/claude-fw-g-ingress-authority-test-transition.json":         "027fc30210229e84212551080f62bbffaf767f47f1c55526677e8600df2af208",
	"docs/egress/maintenance/claude-fw-g-model-capability-source-transition.json":        "f65639b6805a9bc43344bfaa7c912d6a7522df8db15510e6f35ef4aa786afb8f",
	"docs/egress/maintenance/claude-fw-g-query-fail-close-source-transition.json":        "e1a6731634ba567489b74ad22cc0acd33f6ed1bbfa348a46a1273ba3c8c2e5ce",
	"docs/egress/maintenance/claude-fw-g-query-fail-close-test-transition.json":          "decebbfe12ddc559e2b382cc3dc298c3367c1bc73ece22f5ed638968a9bab8e7",
	"docs/egress/maintenance/claude-fw-g-source-transition.json":                         "b84fa150c93856a7e3717c7053cd1e2d90f9a6adce259d68252d5aaf11fb6008",
	"docs/egress/maintenance/claude-fw-g-support-envelope-source-transition.json":        "70ff3e96565a9c758e9708523d5e07a2b45038b36942fb02b6556d67dfe3e170",
	"docs/egress/maintenance/claude-fw-g-support-envelope-test-transition.json":          "ae56ba2028807b88bb0d2d9a02bad25de5bc6c2a760289eddaf3df2cce531629",
	"docs/egress/maintenance/claude-fw-g-test-transition.json":                           "e620702b24675126959f07963c99ea5a07ccf92c502b38d7cf95e295a390c8df",
	"docs/egress/maintenance/claude-fw-g-thinking-display-source-transition.json":        "015b430cfd61cbf8434fe790a61b85e64a6425b8de6e5aeae366ea0649712d2a",
	"docs/egress/maintenance/claude-fw-g-thinking-display-test-transition.json":          "b23717fe96bfb4af9bb1c5b81c797911ef0c0aa59db375c078d83a87e3effa6f",
	"docs/egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json":            "7f6599b3ac521cf669bcccef8170984702b9d62feb5812d5bb2cb80c25c08319",
	"docs/egress/maintenance/claude-fw-g-three-model-acceptance.json":                    "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9",
	"docs/egress/maintenance/claude-fw-h-bare-chat-route-source-transition.json":         "bc4f57df92768a71b5210737121861d9c2dd8234d56e56c84e1a256bd31d60ec",
	"docs/egress/maintenance/claude-fw-h-bare-chat-route-test-transition.json":           "d3b0312e4f1f2032b36e136f04d60653698f2e1de071c68f4f6c94b4a19fae18",
	"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json": "e33912bec8e6ff619790fc187312f0d52817e07d6611a93a2d8a7dbfc46c6bc2",
	"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-test-transition.json":   "3092c2d422aea527ef5a2fbb4febbb57753fe5d4c0619a7232d819f7fefecf38",
	"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json":       "13918c77227fcfff5f44daa77d0382630e37998fd926bab2334cb9392bb7eabd",
	"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json":         "6eae1f11f3abc5050ead6a31a7eb79ad5ef7e7066915fc2666fa45dcba8304e0",
	"docs/egress/maintenance/claude-fw-h-production-acceptance-package.json":             "45b21fd3e74e6b2a3968c04bf18b416a0a43a157ba7cb7451f32a5d3c711cabd",
	"docs/egress/maintenance/claude-fw-h-source-transition.json":                         "b4638d412cfcd34e144dfbcce9b2326d2f6ed4df1987b55cac2c3209d3a5aa5f",
	"docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json":      "92c7a0f1d1150cb12004705347c18cc9d0b411439b8fca865cb8a395569b1f6d",
	"docs/egress/maintenance/claude-fw-h-third-party-strict-test-transition.json":        "646d66a654a2fece0081e08710a687563e6d67750351c48dbdb114c0b3dd234c",
}

var (
	claudeFWHImmutableTransitionLedgerOnce  sync.Once
	claudeFWHImmutableTransitionLedgerGraph map[string]map[string][]string
	claudeFWHImmutableTransitionLedgerErr   error
)

// claudeFWHImmutableTransitionLedgerSupersedes 只读取固定摘要白名单中的历史收据，
// 再按同一路径的精确 from→to 边做可达性判断。它不扫描目录，也不接受未登记文件。
func claudeFWHImmutableTransitionLedgerSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	claudeFWHImmutableTransitionLedgerOnce.Do(func() {
		claudeFWHImmutableTransitionLedgerGraph = make(map[string]map[string][]string)
		for receiptPath, wantDigest := range claudeFWHImmutableTransitionLedgerFiles {
			raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(receiptPath)))
			if err != nil || claudeFWHSourceDigest(raw) != wantDigest {
				claudeFWHImmutableTransitionLedgerErr = errors.New("Claude transition 账本摘要不一致")
				return
			}
			var receipt struct {
				Transitions []changeset4SourceTransitionEntry `json:"transitions"`
			}
			if err := json.Unmarshal(raw, &receipt); err != nil {
				claudeFWHImmutableTransitionLedgerErr = err
				return
			}
			for _, transition := range receipt.Transitions {
				if len(transition.FromSHA256) != 64 || len(transition.ToSHA256) != 64 ||
					transition.FromSHA256 == transition.ToSHA256 {
					claudeFWHImmutableTransitionLedgerErr = errors.New("Claude transition 账本边非法")
					return
				}
				byFrom := claudeFWHImmutableTransitionLedgerGraph[transition.Path]
				if byFrom == nil {
					byFrom = make(map[string][]string)
					claudeFWHImmutableTransitionLedgerGraph[transition.Path] = byFrom
				}
				byFrom[transition.FromSHA256] = append(
					byFrom[transition.FromSHA256], transition.ToSHA256,
				)
			}
		}
	})
	if claudeFWHImmutableTransitionLedgerErr != nil || priorDigest == currentDigest {
		return false
	}
	byFrom := claudeFWHImmutableTransitionLedgerGraph[path]
	if byFrom == nil {
		return false
	}
	queue := []string{priorDigest}
	visited := map[string]struct{}{priorDigest: {}}
	for len(queue) != 0 {
		from := queue[0]
		queue = queue[1:]
		for _, to := range byFrom[from] {
			if to == currentDigest {
				return true
			}
			if _, ok := visited[to]; ok {
				continue
			}
			visited[to] = struct{}{}
			queue = append(queue, to)
		}
	}
	return false
}

type claudeFWHFableDeclaredFallbackRef struct {
	Kind   string `json:"kind,omitempty"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeFWHFableDeclaredFallbackTransitionReceipt struct {
	SchemaVersion string                              `json:"schema_version"`
	Date          string                              `json:"date"`
	Phase         string                              `json:"phase"`
	BaseCommit    string                              `json:"base_commit"`
	Scope         string                              `json:"scope"`
	Predecessors  []claudeFWHFableDeclaredFallbackRef `json:"predecessors"`
	Source        *claudeFWHFableDeclaredFallbackRef  `json:"source_transition,omitempty"`
	Release       struct {
		Version       string `json:"version"`
		ProfileSHA256 string `json:"profile_sha256"`
		WireSHA256    string `json:"wire_sha256"`
		ReleaseSHA256 string `json:"release_sha256"`
		BundleSHA256  string `json:"bundle_sha256"`
	} `json:"release"`
	Observation struct {
		Host                  string `json:"host"`
		RequestModel          string `json:"request_model"`
		ResponseModel         string `json:"response_model"`
		DeclaredFallbackModel string `json:"declared_fallback_model"`
		Disposition           string `json:"disposition"`
	} `json:"observation"`
	Transitions []changeset4SourceTransitionEntry `json:"transitions"`
	Gates       []string                          `json:"gates,omitempty"`
	Safety      struct {
		ProductionHost       string `json:"production_host"`
		ProductionSelector   string `json:"production_selector"`
		CandidateEnabled     bool   `json:"candidate_enabled"`
		ProfileChanged       bool   `json:"profile_changed"`
		WireChanged          bool   `json:"wire_changed"`
		ReleaseBundleChanged bool   `json:"release_bundle_changed"`
		DeploymentPerformed  bool   `json:"deployment_performed"`
		VircsChanged         bool   `json:"vircs_production_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func readClaudeFWHFableDeclaredFallbackTransition(
	path string,
) (claudeFWHFableDeclaredFallbackTransitionReceipt, []byte, error) {
	var receipt claudeFWHFableDeclaredFallbackTransitionReceipt
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
		return receipt, nil, errors.New("Claude FW-H Fable fallback transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHFableDeclaredFallbackTransition(
	receipt claudeFWHFableDeclaredFallbackTransitionReceipt,
	raw []byte,
	testTransition bool,
) error {
	wantSchema := "official-egress-claude-fw-h-fable-declared-fallback-source-transition/v1"
	wantDigest := claudeFWHFableDeclaredFallbackSourceSHA256
	wantPredecessors := map[string]string{
		"docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json":                                                             claudeFWHThirdPartyStrictSourceSHA256,
		"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json":                                                              claudeFWHLegacyRetirementSourceSHA256,
		"backend/internal/officialegress/catalogdata/claude/profiles/2.1.226/e02a3af6fa56cf09b6525d884c9de3f7b76ffe84eb000d92606681b0085b9ab5.json": ClaudeFWGProfileDigest,
		"backend/internal/officialegress/catalogdata/claude/wire/2.1.226/a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d.json":     claudeFWGWireDigest,
	}
	wantTransitionCount := 1
	if testTransition {
		wantSchema = "official-egress-claude-fw-h-fable-declared-fallback-test-transition/v1"
		wantDigest = claudeFWHFableDeclaredFallbackTestSHA256
		wantPredecessors = map[string]string{
			"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json": claudeFWHLegacyRetirementTestSHA256,
		}
		wantTransitionCount = 6
	}
	if claudeFWHSourceDigest(raw) != wantDigest || receipt.SchemaVersion != wantSchema ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "6a54801ed0df531fe6f066a79315d0f4001b7ea1" ||
		receipt.Scope != "fable_declared_fallback_response_closure" ||
		len(receipt.Transitions) != wantTransitionCount || receipt.Result != "passed" {
		return errors.New("Claude FW-H Fable fallback transition 顶层事实非法")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude FW-H Fable fallback Release 身份非法")
	}
	if receipt.Observation.Host != "DMIT" ||
		receipt.Observation.RequestModel != "claude-fable-5" ||
		receipt.Observation.ResponseModel != "claude-opus-5" ||
		receipt.Observation.DeclaredFallbackModel != "claude-opus-5" ||
		strings.TrimSpace(receipt.Observation.Disposition) == "" {
		return errors.New("Claude FW-H Fable fallback 观察事实非法")
	}
	if receipt.Safety.ProductionHost != "DMIT" ||
		receipt.Safety.ProductionSelector != "active" || receipt.Safety.CandidateEnabled ||
		receipt.Safety.ProfileChanged || receipt.Safety.WireChanged ||
		receipt.Safety.ReleaseBundleChanged || receipt.Safety.DeploymentPerformed ||
		receipt.Safety.VircsChanged {
		return errors.New("Claude FW-H Fable fallback 安全边界非法")
	}
	if testTransition {
		if receipt.Source == nil ||
			receipt.Source.Path != "docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json" ||
			receipt.Source.SHA256 != claudeFWHFableDeclaredFallbackSourceSHA256 ||
			len(receipt.Gates) != 4 {
			return errors.New("Claude FW-H Fable fallback 测试前序非法")
		}
	} else if receipt.Source != nil || len(receipt.Gates) != 0 {
		return errors.New("Claude FW-H Fable fallback 源码前序非法")
	}
	if len(receipt.Predecessors) != len(wantPredecessors) {
		return errors.New("Claude FW-H Fable fallback 前序数量非法")
	}
	for _, predecessor := range receipt.Predecessors {
		wantDigest, ok := wantPredecessors[predecessor.Path]
		if !ok || predecessor.SHA256 != wantDigest || strings.TrimSpace(predecessor.Kind) == "" {
			return errors.New("Claude FW-H Fable fallback 前序引用非法")
		}
		predecessorRaw, err := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(predecessor.Path),
		))
		if err != nil || claudeFWHSourceDigest(predecessorRaw) != predecessor.SHA256 {
			return errors.New("Claude FW-H Fable fallback 前序摘要不一致")
		}
		delete(wantPredecessors, predecessor.Path)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude FW-H Fable fallback 前序集合不闭合")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.FromSHA256) != 64 ||
			len(transition.ToSHA256) != 64 || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			testTransition != strings.HasSuffix(transition.Path, "_test.go") {
			return errors.New("Claude FW-H Fable fallback transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude FW-H Fable fallback transition 路径未排序或重复")
	}
	return nil
}

func claudeFWHFableDeclaredFallbackDirectSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHFableDeclaredFallbackTransition(item.path)
		if err != nil || validateClaudeFWHFableDeclaredFallbackTransition(
			receipt, raw, item.test,
		) != nil {
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

func claudeFWHFableDeclaredFallbackTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHImmutableTransitionLedgerSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHFableDeclaredFallbackDirectSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHFableDeclaredFallbackTransition(item.path)
		if err != nil || validateClaudeFWHFableDeclaredFallbackTransition(
			receipt, raw, item.test,
		) != nil {
			return false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path != path || transition.ToSHA256 != currentDigest {
				continue
			}
			if transition.FromSHA256 == priorDigest {
				return true
			}
			switch transition.Path {
			case "backend/internal/officialegress/claude_runtime.go":
				if claudeFWHThirdPartyStrictSourceTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) {
					return true
				}
			case "backend/internal/officialegress/claude_runtime_test.go":
				if claudeFWHLegacyRetirementTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) {
					return true
				}
			}
		}
	}
	return false
}

func TestClaudeFWHFableDeclaredFallbackTransitionsAreFrozen(t *testing.T) {
	for _, item := range []struct {
		path string
		test bool
	}{
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json", false},
		{"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-test-transition.json", true},
	} {
		receipt, raw, err := readClaudeFWHFableDeclaredFallbackTransition(item.path)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateClaudeFWHFableDeclaredFallbackTransition(
			receipt, raw, item.test,
		); err != nil {
			t.Fatal(err)
		}
		for _, transition := range receipt.Transitions {
			target, err := os.ReadFile(filepath.Join(
				"../../..", filepath.FromSlash(transition.Path),
			))
			if err != nil || claudeFWHSourceDigest(target) != transition.ToSHA256 {
				t.Fatalf("Claude FW-H Fable fallback 摘要漂移：%s", transition.Path)
			}
		}
	}
}
