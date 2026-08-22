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
	claudeFWHFinalAcceptanceSHA256             = "0965d67f4e46f365ac41a3d92dc11012ff9d2c4a47f8f179d18bbe0407888336"
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
	"docs/egress/maintenance/claude-fw-h-final-acceptance-package.json":                  claudeFWHFinalAcceptanceSHA256,
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
	if claudeFWHStateDurabilityTransitionSupersedes(path, priorDigest, currentDigest) {
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
	if claudeFWHStateDurabilityTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
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
			currentDigest := claudeFWHSourceDigest(target)
			if err != nil || currentDigest != transition.ToSHA256 &&
				!claudeFWHStateDurabilityTransitionSupersedes(
					transition.Path, transition.ToSHA256, currentDigest,
				) {
				t.Fatalf("Claude FW-H Fable fallback 摘要漂移：%s", transition.Path)
			}
		}
	}
}

type claudeFWHFinalAcceptancePackage struct {
	SchemaVersion string `json:"schema_version"`
	Phase         string `json:"phase"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	AcceptanceID  string `json:"acceptance_id"`
	Target        struct {
		Product                string   `json:"product"`
		Version                string   `json:"version"`
		Platform               string   `json:"platform"`
		Authentication         string   `json:"authentication"`
		Privacy                string   `json:"privacy"`
		Models                 []string `json:"models"`
		RequiredRules          int      `json:"required_rules"`
		ProfileSHA256          string   `json:"profile_sha256"`
		WireSHA256             string   `json:"wire_sha256"`
		ReleaseSHA256          string   `json:"release_sha256"`
		BundleSHA256           string   `json:"bundle_sha256"`
		ActiveApprovalSHA256   string   `json:"active_approval_sha256"`
		RollbackApprovalSHA256 string   `json:"rollback_approval_sha256"`
	} `json:"target"`
	Predecessors   []claudeFWHFableDeclaredFallbackRef `json:"predecessors"`
	DeploymentFact struct {
		Stage          string `json:"stage"`
		ProductionHost string `json:"production_host"`
		Active         struct {
			Commit              string `json:"commit"`
			Tree                string `json:"tree"`
			Version             string `json:"version"`
			BuildID             string `json:"build_id"`
			Image               string `json:"image"`
			ImageID             string `json:"image_id"`
			BinarySHA256        string `json:"binary_sha256"`
			SourceArchiveSHA256 string `json:"source_archive_sha256"`
			ContainerID         string `json:"container_id"`
			StartedAtUTC        string `json:"started_at_utc"`
			ActivationEventID   string `json:"activation_event_id"`
			Health              string `json:"health"`
			RestartCount        int    `json:"restart_count"`
		} `json:"active"`
		Rollback struct {
			Branch                    string `json:"branch"`
			Commit                    string `json:"commit"`
			Tree                      string `json:"tree"`
			Version                   string `json:"version"`
			BuildID                   string `json:"build_id"`
			Image                     string `json:"image"`
			ImageID                   string `json:"image_id"`
			BinarySHA256              string `json:"binary_sha256"`
			SourceArchiveSHA256       string `json:"source_archive_sha256"`
			ActivationEventID         string `json:"activation_event_id"`
			ContainsRetiredOAuthChain bool   `json:"contains_retired_oauth_chain"`
			Role                      string `json:"role"`
		} `json:"rollback"`
		StabilityObservation struct {
			ObservedAtUTC         string `json:"observed_at_utc"`
			StableSeconds         int    `json:"stable_seconds"`
			FatalOrPanicCount     int    `json:"fatal_or_panic_count"`
			GuardFailureCount     int    `json:"guard_failure_count"`
			ClaudeErrorCount      int    `json:"claude_error_count"`
			DependenciesUnchanged bool   `json:"dependencies_unchanged"`
		} `json:"stability_observation"`
		Result string `json:"result"`
	} `json:"deployment_fact"`
	ProductionIngressInventory struct {
		OAuthEntries []struct {
			LogicalIngressID       string   `json:"logical_ingress_id"`
			ProtocolClass          string   `json:"protocol_class"`
			PhysicalRoutes         []string `json:"physical_routes,omitempty"`
			PhysicalAliasIDs       []string `json:"physical_alias_ids,omitempty"`
			RequestIdentityClass   string   `json:"request_identity_class"`
			UnsupportedShapePolicy string   `json:"unsupported_shape_policy,omitempty"`
			CurrentDisposition     string   `json:"current_disposition"`
		} `json:"oauth_entries"`
		SharedRouteClassification struct {
			Messages    string `json:"messages"`
			CountTokens string `json:"count_tokens"`
		} `json:"shared_route_classification"`
		RetainedLegacy []string `json:"retained_legacy"`
		Rerouted       []struct {
			LogicalIngressID   string   `json:"logical_ingress_id"`
			PhysicalAliasIDs   []string `json:"physical_alias_ids"`
			CurrentDisposition string   `json:"current_disposition"`
		} `json:"rerouted"`
		UnknownOAuthEgress string `json:"unknown_oauth_egress"`
	} `json:"production_ingress_inventory"`
	RemovalReceipt struct {
		SchemaVersion               string   `json:"schema_version"`
		ReceiptID                   string   `json:"receipt_id"`
		IssuedAtUTC                 string   `json:"issued_at_utc"`
		Status                      string   `json:"status"`
		RemovedCapability           string   `json:"removed_capability"`
		ActiveSourceAndImageRemoved bool     `json:"active_source_and_image_removed"`
		OperationalRollbackRetained bool     `json:"operational_rollback_copy_retained"`
		RetainedLegacy              []string `json:"retained_legacy"`
		ConsumerClosure             string   `json:"consumer_closure"`
		StrictReplacement           string   `json:"strict_replacement"`
		PreservedProductSemantics   []string `json:"preserved_product_semantics"`
		ApprovalSHA256              string   `json:"approval_sha256"`
		Result                      string   `json:"result"`
	} `json:"removal_receipt"`
	AcceptancePackage struct {
		Envelopes struct {
			ActiveSupport             []string `json:"active_support"`
			RollbackOperational       []string `json:"rollback_operational"`
			DeploymentTraffic         []string `json:"deployment_traffic"`
			DeploymentSubsetInvariant bool     `json:"deployment_subset_invariant"`
		} `json:"envelopes"`
		Matrix struct {
			ScriptSHA256              string `json:"script_sha256"`
			TotalCases                int    `json:"total_cases"`
			PositiveAndIsolationCases int    `json:"positive_and_isolation_cases"`
			FailCloseCases            int    `json:"fail_close_cases"`
			Coverage                  struct {
				Models                    []string `json:"models"`
				APIClasses                []string `json:"api_classes"`
				StreamModes               []bool   `json:"stream_modes"`
				IngressClasses            []string `json:"ingress_classes"`
				ThinkingDisplaySummarized bool     `json:"thinking_display_summarized"`
				CodexRouteIsolation       bool     `json:"codex_route_isolation"`
			} `json:"coverage"`
			FirstRollbackAttempt struct {
				StartedAtUTC                      string `json:"started_at_utc"`
				CompletedCases                    int    `json:"completed_cases"`
				MatrixRC                          int    `json:"matrix_rc"`
				FailureResponsePreserved          bool   `json:"failure_response_preserved"`
				RootCause                         string `json:"root_cause"`
				ImageChangedBeforeRetest          bool   `json:"image_changed_before_retest"`
				OriginalAndAlternatePromptRetests string `json:"original_and_alternate_prompt_retests"`
				AcceptanceUse                     string `json:"acceptance_use"`
			} `json:"first_rollback_attempt"`
			RollbackFull struct {
				StartedAtUTC  string `json:"started_at_utc"`
				FinishedAtUTC string `json:"finished_at_utc"`
				Passed        int    `json:"passed"`
				Failed        int    `json:"failed"`
				MatrixRC      int    `json:"matrix_rc"`
				Result        string `json:"result"`
			} `json:"rollback_full"`
			RestoredActiveFull struct {
				StartedAtUTC  string `json:"started_at_utc"`
				FinishedAtUTC string `json:"finished_at_utc"`
				Passed        int    `json:"passed"`
				Failed        int    `json:"failed"`
				Result        string `json:"result"`
			} `json:"restored_active_full"`
		} `json:"matrix"`
		FableFallback struct {
			RequestModel                               string `json:"request_model"`
			DeclaredFallbackModel                      string `json:"declared_fallback_model"`
			ObservedResponseModel                      string `json:"observed_response_model"`
			DeclaredFallbackSessionLatch               bool   `json:"declared_fallback_session_latch"`
			ServerFallbackModel                        string `json:"server_fallback_model"`
			ServerFallbackSessionLatchOnActualResponse bool   `json:"server_fallback_session_latch_on_actual_response"`
		} `json:"fable_fallback"`
		Result string `json:"result"`
	} `json:"acceptance_package"`
	Safety struct {
		ProductionHost                  string `json:"production_host"`
		VircsScope                      string `json:"vircs_scope"`
		VircsAccessed                   bool   `json:"vircs_accessed"`
		VircsChanged                    bool   `json:"vircs_changed"`
		ComposeDownUsed                 bool   `json:"compose_down_used"`
		UnscopedPruneUsed               bool   `json:"unscoped_prune_used"`
		DatabaseOrCacheRecreated        bool   `json:"database_or_cache_recreated"`
		DependencyContainersChanged     bool   `json:"dependency_containers_changed"`
		ActiveAndRollbackImagesRetained bool   `json:"active_and_rollback_images_retained"`
		SecretScan                      struct {
			MatchedPatterns int    `json:"matched_patterns"`
			Result          string `json:"result"`
		} `json:"secret_scan"`
	} `json:"safety"`
	Transitions     []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionState string                            `json:"production_state"`
	RetirementState string                            `json:"retirement_state"`
	Result          string                            `json:"result"`
}

func readClaudeFWHFinalAcceptancePackage() (claudeFWHFinalAcceptancePackage, []byte, error) {
	var receipt claudeFWHFinalAcceptancePackage
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-h-final-acceptance-package.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("Claude FW-H 最终聚合验收包尾部存在额外 JSON")
	}
	if claudeFWHSourceDigest(raw) != claudeFWHFinalAcceptanceSHA256 {
		return receipt, nil, errors.New("Claude FW-H 最终聚合验收包摘要漂移")
	}
	return receipt, raw, nil
}

func TestClaudeFWHFinalAcceptancePackageIsFrozen(t *testing.T) {
	receipt, raw, err := readClaudeFWHFinalAcceptancePackage()
	if err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-claude-fw-h-final-acceptance-package/v1" ||
		receipt.Phase != "FW-H" || receipt.IssuedAtUTC != "2026-08-22T04:56:09Z" ||
		receipt.AcceptanceID != "claude-code-2.1.226-fw-h-final-e2c80213a-dmit" ||
		receipt.ProductionState != "restored_active" ||
		receipt.RetirementState != "completed_removal_receipt_issued" ||
		receipt.Result != "accepted" {
		t.Fatal("Claude FW-H 最终聚合验收包顶层事实非法")
	}
	validateClaudeFWHFinalTargetAndPredecessors(t, receipt)
	validateClaudeFWHFinalDeployment(t, receipt)
	validateClaudeFWHFinalIngressAndRemoval(t, receipt)
	validateClaudeFWHFinalAcceptanceMatrix(t, receipt)
	validateClaudeFWHFinalSafetyAndTransitions(t, receipt)
	for _, marker := range []string{
		"sk-", "access_token", "refresh_token", "authorization: bearer", "bearer ",
	} {
		if strings.Contains(strings.ToLower(string(raw)), marker) {
			t.Fatalf("Claude FW-H 最终聚合验收包包含秘密模式：%s", marker)
		}
	}
}

func validateClaudeFWHFinalTargetAndPredecessors(
	t *testing.T,
	receipt claudeFWHFinalAcceptancePackage,
) {
	t.Helper()
	target := receipt.Target
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		target.Platform != "linux/amd64" || target.Authentication != "claude.ai-oauth" ||
		target.Privacy != "essential-traffic-no-telemetry" || target.RequiredRules != 40 ||
		!slices.Equal(target.Models, []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}) ||
		target.ProfileSHA256 != ClaudeFWGProfileDigest ||
		target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		target.BundleSHA256 != ClaudeFWGBundleDigest ||
		target.ActiveApprovalSHA256 != ClaudeFWHProductionApprovalDigest ||
		target.RollbackApprovalSHA256 != ClaudeFWHInitialProductionApprovalDigest {
		t.Fatal("Claude FW-H 最终目标身份非法")
	}
	want := map[string]string{
		"docs/egress/maintenance/claude-fw-h-production-acceptance-package.json":                                                 claudeFWHProductionAcceptanceSHA256,
		"docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json":                                           claudeFWHLegacyRetirementSourceSHA256,
		"docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json":                                             claudeFWHLegacyRetirementTestSHA256,
		"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-source-transition.json":                                     claudeFWHFableDeclaredFallbackSourceSHA256,
		"docs/egress/maintenance/claude-fw-h-fable-declared-fallback-test-transition.json":                                       claudeFWHFableDeclaredFallbackTestSHA256,
		"backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json": ClaudeFWHProductionApprovalDigest,
	}
	if len(receipt.Predecessors) != len(want) {
		t.Fatal("Claude FW-H 最终前序数量非法")
	}
	for _, predecessor := range receipt.Predecessors {
		wantDigest, ok := want[predecessor.Path]
		if !ok || predecessor.SHA256 != wantDigest || strings.TrimSpace(predecessor.Kind) == "" {
			t.Fatal("Claude FW-H 最终前序引用非法")
		}
		predecessorRaw, err := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(predecessor.Path),
		))
		if err != nil || claudeFWHSourceDigest(predecessorRaw) != predecessor.SHA256 {
			t.Fatal("Claude FW-H 最终前序摘要不一致")
		}
		delete(want, predecessor.Path)
	}
	if len(want) != 0 {
		t.Fatal("Claude FW-H 最终前序集合不闭合")
	}
}

func validateClaudeFWHFinalDeployment(t *testing.T, receipt claudeFWHFinalAcceptancePackage) {
	t.Helper()
	fact := receipt.DeploymentFact
	active := fact.Active
	rollback := fact.Rollback
	stable := fact.StabilityObservation
	if fact.Stage != "restored_active" || fact.ProductionHost != "DMIT" || fact.Result != "passed" ||
		active.Commit != "e2c80213ab338cc6f91eee00e28a96bc956f0512" ||
		active.Tree != "0e7bb9cd9fe66848f3810e4ea7f10f639a638dbc" ||
		active.Version != "0.1.177-4-fw-h-final-e2c80213a" ||
		active.BuildID != "sub2api-v0.1.177-4-fw-h-final-e2c80213a" ||
		active.Image != "sub2apiplus:fw-h-final-e2c80213a" ||
		active.ImageID != "sha256:356384ce0429a2dc30d484648371d1921644184fc978195ae583c23e968dc3d6" ||
		active.BinarySHA256 != "05415bb8a59f0b02555a1e2357d87efad3b8a3333912c8220124835c821828a0" ||
		active.SourceArchiveSHA256 != "0a483d9d209e9a1502aebc015afee3d283e27754dfb66bfc25b0cecae7ebd72e" ||
		active.ContainerID != "3521e509b068fd9e76ed591226e9519629473533309a4558b08573cabd380536" ||
		active.StartedAtUTC != "2026-08-22T04:40:12.230157753Z" ||
		active.ActivationEventID != "5682a83c57d5e947ddaf60169fcb5db1d5f217fc11165489479f1f443341d7b0" ||
		active.Health != "healthy" || active.RestartCount != 0 {
		t.Fatal("Claude FW-H 最终 active DeploymentFact 非法")
	}
	if rollback.Branch != "codex/fw-h-rollback-legacy-fable" ||
		rollback.Commit != "f388cd7d86ebff625bf7f886c06161cff94db22d" ||
		rollback.Tree != "332cb5762a4636bc970d4ff85d6e753e3c5df5b2" ||
		rollback.Version != "0.1.177-4-fw-h-rollback-f388cd7d8" ||
		rollback.BuildID != "sub2api-v0.1.177-4-fw-h-rollback-f388cd7d8" ||
		rollback.Image != "sub2apiplus:fw-h-rollback-f388cd7d8" ||
		rollback.ImageID != "sha256:3bde555633311a212387bf6a54eb0bf72e3d17ea35efbce444baaa7f5d165993" ||
		rollback.BinarySHA256 != "5ef83eb9c4c193f25914f52c3eb6340e6a0d22d1cf78e7d0e93660ba6f503e1a" ||
		rollback.SourceArchiveSHA256 != "b763f783f05e795e7df903bc5d27ed13d58d915b1d59093aea7221cacd892478" ||
		rollback.ActivationEventID != "f74c842055527a07a6dbb90a0504ef6cadf88746c9556a9bf97ed01c9872ec49" ||
		!rollback.ContainsRetiredOAuthChain || rollback.Role != "operational_rollback_only" {
		t.Fatal("Claude FW-H 最终 rollback DeploymentFact 非法")
	}
	if stable.ObservedAtUTC != "2026-08-22T04:43:50Z" || stable.StableSeconds != 218 ||
		stable.FatalOrPanicCount != 0 || stable.GuardFailureCount != 0 ||
		stable.ClaudeErrorCount != 0 || !stable.DependenciesUnchanged {
		t.Fatal("Claude FW-H 最终稳定观察事实非法")
	}
}

func validateClaudeFWHFinalIngressAndRemoval(
	t *testing.T,
	receipt claudeFWHFinalAcceptancePackage,
) {
	t.Helper()
	inventory := receipt.ProductionIngressInventory
	type wantIngress struct {
		protocol    string
		routes      []string
		aliases     []string
		identity    string
		unsupported string
	}
	want := map[string]wantIngress{
		"official-count-tokens-oauth": {
			protocol: "anthropic-count-tokens", routes: []string{"POST /v1/messages/count_tokens"}, identity: "official",
		},
		"official-messages-oauth": {
			protocol: "anthropic-messages", routes: []string{"POST /v1/messages"}, identity: "official",
		},
		"third-party-count-tokens-oauth": {
			protocol: "anthropic-count-tokens", routes: []string{"POST /v1/messages/count_tokens"}, identity: "third-party",
		},
		"third-party-messages-oauth": {
			protocol: "anthropic-messages", routes: []string{"POST /v1/messages"}, identity: "third-party",
		},
		"chat-completions-oauth": {
			protocol: "openai-chat-completions", identity: "third-party",
			aliases: []string{"alias-chat-bare", "alias-chat-v1"},
		},
		"responses-oauth": {
			protocol: "openai-responses", identity: "third-party",
			aliases: []string{
				"alias-responses-bare-http", "alias-responses-bare-subpath",
				"alias-responses-bare-ws", "alias-responses-v1-http",
				"alias-responses-v1-subpath", "alias-responses-v1-ws",
			},
			unsupported: "strict_fail_close_before_oauth_credential_use",
		},
	}
	if len(inventory.OAuthEntries) != len(want) {
		t.Fatal("Claude FW-H 最终 OAuth 入口数量非法")
	}
	for _, entry := range inventory.OAuthEntries {
		expected, ok := want[entry.LogicalIngressID]
		if !ok || entry.ProtocolClass != expected.protocol ||
			!slices.Equal(entry.PhysicalRoutes, expected.routes) ||
			!slices.Equal(entry.PhysicalAliasIDs, expected.aliases) ||
			entry.RequestIdentityClass != expected.identity ||
			entry.UnsupportedShapePolicy != expected.unsupported ||
			entry.CurrentDisposition != "migrated_strict" {
			t.Fatalf("Claude FW-H 最终 OAuth 入口非法：%s", entry.LogicalIngressID)
		}
		delete(want, entry.LogicalIngressID)
	}
	if len(want) != 0 || len(inventory.RetainedLegacy) != 0 ||
		inventory.SharedRouteClassification.Messages != "official 与 third-party 共享物理路由，按受信请求身份分成两个逻辑入口" ||
		inventory.SharedRouteClassification.CountTokens != "official 与 third-party 共享物理路由，按受信请求身份分成两个逻辑入口" ||
		inventory.UnknownOAuthEgress != "denied" || len(inventory.Rerouted) != 1 ||
		inventory.Rerouted[0].LogicalIngressID != "codex-direct-rerouted" ||
		inventory.Rerouted[0].CurrentDisposition != "rerouted" ||
		!slices.Equal(inventory.Rerouted[0].PhysicalAliasIDs, []string{
			"alias-codex-direct-responses-http", "alias-codex-direct-responses-subpath",
			"alias-codex-direct-responses-ws",
		}) {
		t.Fatal("Claude FW-H 最终 Inventory 闭集非法")
	}
	removal := receipt.RemovalReceipt
	if removal.SchemaVersion != "official-egress-claude-oauth-legacy-removal-receipt/v1" ||
		removal.ReceiptID != "claude-code-2.1.226-fw-h-legacy-removal-e2c80213a" ||
		removal.IssuedAtUTC != receipt.IssuedAtUTC || removal.Status != "issued" ||
		removal.RemovedCapability != "claude-oauth-legacy-execution-chain" ||
		!removal.ActiveSourceAndImageRemoved || !removal.OperationalRollbackRetained ||
		len(removal.RetainedLegacy) != 0 || removal.ConsumerClosure != "passed" ||
		removal.StrictReplacement != "six_oauth_ingress_entries" ||
		!slices.Equal(removal.PreservedProductSemantics, []string{
			"setup-token-non-persona-managed", "key-based-auth-retained",
			"service-account-retained", "codex-direct-rerouted",
		}) || removal.ApprovalSHA256 != ClaudeFWHProductionApprovalDigest ||
		removal.Result != "passed" {
		t.Fatal("Claude FW-H RemovalReceipt 非法")
	}
}

func validateClaudeFWHFinalAcceptanceMatrix(
	t *testing.T,
	receipt claudeFWHFinalAcceptancePackage,
) {
	t.Helper()
	wantIngress := []string{
		"official-count-tokens-oauth", "official-messages-oauth",
		"third-party-count-tokens-oauth", "third-party-messages-oauth",
		"chat-completions-oauth", "responses-oauth",
	}
	acceptance := receipt.AcceptancePackage
	if !slices.Equal(acceptance.Envelopes.ActiveSupport, wantIngress) ||
		!slices.Equal(acceptance.Envelopes.RollbackOperational, wantIngress) ||
		!slices.Equal(acceptance.Envelopes.DeploymentTraffic, wantIngress) ||
		!acceptance.Envelopes.DeploymentSubsetInvariant || acceptance.Result != "accepted" {
		t.Fatal("Claude FW-H 最终三个 Envelope 非法")
	}
	matrix := acceptance.Matrix
	coverage := matrix.Coverage
	first := matrix.FirstRollbackAttempt
	rollback := matrix.RollbackFull
	active := matrix.RestoredActiveFull
	if matrix.ScriptSHA256 != "466afe463258f73bfb81a9fb4137aa13157eafca1a4cbba1d94f5cc634948806" ||
		matrix.TotalCases != 46 || matrix.PositiveAndIsolationCases != 29 ||
		matrix.FailCloseCases != 17 ||
		!slices.Equal(coverage.Models, []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}) ||
		!slices.Equal(coverage.APIClasses, []string{
			"anthropic-messages", "anthropic-count-tokens",
			"openai-chat-completions", "openai-responses",
		}) || !slices.Equal(coverage.StreamModes, []bool{false, true}) ||
		!slices.Equal(coverage.IngressClasses, []string{"official", "third-party"}) ||
		!coverage.ThinkingDisplaySummarized || !coverage.CodexRouteIsolation {
		t.Fatal("Claude FW-H 最终矩阵覆盖事实非法")
	}
	if first.StartedAtUTC != "2026-08-22T04:34:34Z" || first.CompletedCases != 20 ||
		first.MatrixRC != 1 || first.FailureResponsePreserved || first.RootCause != "unresolved" ||
		first.ImageChangedBeforeRetest || first.OriginalAndAlternatePromptRetests != "passed" ||
		first.AcceptanceUse != "excluded_incomplete_attempt" {
		t.Fatal("Claude FW-H 首次瞬态退出记录非法")
	}
	if rollback.StartedAtUTC != "2026-08-22T04:39:04Z" ||
		rollback.FinishedAtUTC != "2026-08-22T04:40:11Z" || rollback.Passed != 46 ||
		rollback.Failed != 0 || rollback.MatrixRC != 0 || rollback.Result != "passed" ||
		active.StartedAtUTC != "2026-08-22T04:40:27Z" ||
		active.FinishedAtUTC != "2026-08-22T04:41:29Z" || active.Passed != 46 ||
		active.Failed != 0 || active.Result != "passed" {
		t.Fatal("Claude FW-H rollback／active 完整矩阵非法")
	}
	fallback := acceptance.FableFallback
	if fallback.RequestModel != "claude-fable-5" ||
		fallback.DeclaredFallbackModel != "claude-opus-5" ||
		fallback.ObservedResponseModel != "claude-opus-5" ||
		fallback.DeclaredFallbackSessionLatch ||
		fallback.ServerFallbackModel != "claude-opus-4-8" ||
		!fallback.ServerFallbackSessionLatchOnActualResponse {
		t.Fatal("Claude FW-H 最终 Fable fallback 事实非法")
	}
}

func validateClaudeFWHFinalSafetyAndTransitions(
	t *testing.T,
	receipt claudeFWHFinalAcceptancePackage,
) {
	t.Helper()
	safety := receipt.Safety
	if safety.ProductionHost != "DMIT" ||
		safety.VircsScope != "final_retirement_and_acceptance_closeout" ||
		safety.VircsAccessed || safety.VircsChanged || safety.ComposeDownUsed ||
		safety.UnscopedPruneUsed || safety.DatabaseOrCacheRecreated ||
		safety.DependencyContainersChanged || !safety.ActiveAndRollbackImagesRetained ||
		safety.SecretScan.MatchedPatterns != 0 || safety.SecretScan.Result != "passed" {
		t.Fatal("Claude FW-H 最终安全事实非法")
	}
	want := map[string][2]string{
		"docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md": {
			"258bdff4347573da38df8dcb721c92489dc43fc488018b7f062d25fbe8a65c0c",
			"6729311fb0152a2e6c100412af96ed5c3c8939675e7daa584349e1bccd5704f8",
		},
		"docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md": {
			"d66f73d33602faacf1d7d1b796aa38a9f842e077609960328ec0ac38d64be7eb",
			"af1066176c33db3580ff4f47b22bbc90ae2fde587357570ad43430ac6975522e",
		},
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		digests, ok := want[transition.Path]
		if !ok || transition.FromSHA256 != digests[0] || transition.ToSHA256 != digests[1] ||
			strings.TrimSpace(transition.Reason) == "" {
			t.Fatalf("Claude FW-H 最终文档 transition 非法：%s", transition.Path)
		}
		target, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		currentDigest := claudeFWHSourceDigest(target)
		if err != nil || currentDigest != transition.ToSHA256 &&
			!claudeFWHStateDurabilityTransitionSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) {
			t.Fatalf("Claude FW-H 最终文档摘要漂移：%s", transition.Path)
		}
		paths = append(paths, transition.Path)
		delete(want, transition.Path)
	}
	if len(want) != 0 || len(paths) != 2 || !slices.IsSorted(paths) ||
		len(paths) != len(slices.Compact(paths)) {
		t.Fatal("Claude FW-H 最终文档 transition 集合不闭合")
	}
}
