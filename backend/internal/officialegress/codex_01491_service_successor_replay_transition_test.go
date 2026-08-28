package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	codex01491ServiceSuccessorReplayPath = "docs/egress/maintenance/codex-0.149.1-service-successor-replay-transition.json"
	codex01491ServiceSuccessorR15Path    = "docs/egress/maintenance/codex-0.149.1-r15-formal-classification-transition.json"
	codex01491ServiceSuccessorR15SHA256  = "206b3a7e6e25bf408d6b3c4fbdbaf6e420d1cf500cb132a8e830d5163a274048"
	codex01491ServiceSuccessorR15ID      = "79996daa098909ace8b2332fd6c0bb5803087ff2585e6c482ae6a1ae7ac8ee36"
)

type codex01491ServiceSuccessorChainBinding struct {
	Path           string `json:"path"`
	FileSHA256     string `json:"file_sha256"`
	IdentitySHA256 string `json:"identity_sha256"`
	SchemaVersion  string `json:"schema_version"`
	Scope          string `json:"scope"`
	Result         string `json:"result"`
}

type codex01491ServiceSuccessorReplayReceipt struct {
	SchemaVersion  string                                     `json:"schema_version"`
	IssuedAtUTC    string                                     `json:"issued_at_utc"`
	BaseCommit     string                                     `json:"base_commit"`
	Scope          string                                     `json:"scope"`
	FrameworkStage string                                     `json:"framework_stage"`
	SuccessorChain []codex01491ServiceSuccessorChainBinding   `json:"successor_chain"`
	PathSetSHA256  string                                     `json:"path_set_sha256"`
	Transitions    []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification   struct {
		R15ReceiptIdentityVerified       bool `json:"r15_receipt_identity_verified"`
		SuccessorReceiptIdentityVerified bool `json:"successor_receipt_identity_verified"`
		PredecessorChainVerified         bool `json:"predecessor_chain_verified"`
		TransitionGraphReplayed          bool `json:"transition_graph_replayed"`
		MultiHopReplayTested             bool `json:"multi_hop_replay_tested"`
		MutationTestsRequired            bool `json:"mutation_tests_required"`
	} `json:"verification"`
	Safety struct {
		HistoricalArtifactsOverwritten bool `json:"historical_artifacts_overwritten"`
		HistoricalReceiptsModified     bool `json:"historical_receipts_modified"`
		OfficialRecapturePerformed     bool `json:"official_recapture_performed"`
		CandidateCapturePerformed      bool `json:"candidate_capture_performed"`
		DeploymentPerformed            bool `json:"deployment_performed"`
		NetworkConfigurationChanged    bool `json:"network_configuration_changed"`
		ProductionSelectorChanged      bool `json:"production_selector_changed"`
		ProductionActivated            bool `json:"production_activated"`
		ARM64Accessed                  bool `json:"arm64_accessed"`
		VircsAccessed                  bool `json:"vircs_accessed"`
		DMITServerAccessed             bool `json:"dmit_server_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

type codex01491ServiceSuccessorHistoricalReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	Scope                 string `json:"scope"`
	PredecessorTransition struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

type codex01491ServiceSuccessorExpectedBinding struct {
	Path           string
	FileSHA256     string
	IdentitySHA256 string
}

var (
	codex01491ServiceSuccessorReplayOnce   sync.Once
	codex01491ServiceSuccessorReplayCached codex01491ServiceSuccessorReplayReceipt
	codex01491ServiceSuccessorReplayEdges  map[string]map[string][]string
	codex01491ServiceSuccessorReplayError  error
)

func codex01491ServiceSuccessorExpectedBindings() []codex01491ServiceSuccessorExpectedBinding {
	return []codex01491ServiceSuccessorExpectedBinding{
		{"docs/egress/maintenance/codex-0.149.1-r16-successor-carry-forward-transition.json", "97286f9cfec8fe2e0c4c60b7ea962bfa7e98bdaa3b083fe736628e68719ef222", "bc8dd8444a5ba65ff30d0439a3e8937c4bf4ea9e2f0362a12b540789af1670c5"},
		{"docs/egress/maintenance/codex-0.149.1-r17-kilo-model-contract-transition.json", "67b78fc1f4db41dad476e3de3c83ea2ebabf3c33ff676502d1dda293ab8a3555", "b7b7534f89f9d79e66970d54a5124b82e795693e75b32c26f9bd7a926169396c"},
		{"docs/egress/maintenance/codex-0.149.1-r18-successor-account-transition.json", "0d3fe228ab2fe418e07a31e2f60e8fcf0a72d23b3b2033e6b248117d694de361", "39ab82fad57031021db3dd7e4a8dfb355317935cd4aac069b17554df593bdef8"},
		{"docs/egress/maintenance/codex-0.149.1-r19-successor-chain-transition.json", "34a6ced196f49fd38a940969662f8292d3417dbad5da6a15c5cc38f1421e0226", "6bf51f5a27d288833ba862ef6f91782aba17a2d6e6afdc8da200678b240ef14e"},
		{"docs/egress/maintenance/codex-0.149.1-r20-candidate-aux-transition.json", "d35fda7dd6a9250286821b5ce96a741ff650ded48a6005a78209008615db3d2d", "6b928fec7aebf5424b48b86bda21aa6e1f5fd5429f41c09bb7d5b72b2d370c06"},
		{"docs/egress/maintenance/codex-0.149.1-r21-classification-fact-correction-transition.json", "6d4195f248c12538363df71aefcd9ac4c1b98220effaa1024ff3c3c46515abc9", "aa67d69ff36b6bf8ff01fcbbf36779fdb18d28d2e882ae82ee332498f3e4be33"},
		{"docs/egress/maintenance/codex-0.149.1-r22-candidate-catalog-transition.json", "e24ad2b2b956bfaa985ead30850623e99428a0ea0a5b84a3e90b84843c94a292", "777fc13ced7f8a1ebb66d44864147dd4a86511bf4f0f76bd8fe2c336cbf05ad3"},
		{"docs/egress/maintenance/codex-0.149.1-r23-runtime-coordinate-transition.json", "7b2b3cc09dd56d095230243c0e77c3f38c97b256bbaca43fb50bd61d51fdb1b9", "c07a935f50e022a2469e234c71bf76d3e59272ac121698df7f33e6bf70106356"},
		{"docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json", "9d78ecb8851935f40be42866f46f83d097b974a8deba0abe44ab46d242fc39b4", "3fd50b27cd48f8a9df0b61e697cdf09f36673f765aa9ef81aed611c09fbed96d"},
	}
}

func codex01491ServiceSuccessorReplayExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r24_selector_lite_coordinate_transition_test.go",
		"backend/internal/officialegress/codex_01491_service_successor_replay_transition_test.go",
		"backend/internal/service/codex_01491_candidate_source_transition_test.go",
		"backend/internal/service/codex_01491_r15_formal_classification_transition_test.go",
		"backend/internal/service/codex_01491_service_successor_replay_transition_test.go",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r24_selector_lite_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_service_successor_replay_transition.py",
	}
}

func loadCodex01491ServiceSuccessorReplayTransition() (
	codex01491ServiceSuccessorReplayReceipt,
	error,
) {
	codex01491ServiceSuccessorReplayOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491ServiceSuccessorReplayPath)
		if err != nil {
			codex01491ServiceSuccessorReplayError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491ServiceSuccessorReplayCached); err != nil {
			codex01491ServiceSuccessorReplayError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491ServiceSuccessorReplayError = errors.New("Codex 0.149.1 officialegress service 后继重放 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491ServiceSuccessorReplayCached.IdentitySHA256,
		); err != nil {
			codex01491ServiceSuccessorReplayError = err
			return
		}
		codex01491ServiceSuccessorReplayEdges,
			codex01491ServiceSuccessorReplayError =
			buildCodex01491ServiceSuccessorReplayEdges(
				codex01491ServiceSuccessorReplayCached,
			)
	})
	return codex01491ServiceSuccessorReplayCached,
		codex01491ServiceSuccessorReplayError
}

func validateCodex01491ServiceSuccessorReplayTransition(
	receipt codex01491ServiceSuccessorReplayReceipt,
) error {
	_, err := buildCodex01491ServiceSuccessorReplayEdges(receipt)
	return err
}

func buildCodex01491ServiceSuccessorReplayEdges(
	receipt codex01491ServiceSuccessorReplayReceipt,
) (map[string]map[string][]string, error) {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-service-successor-replay-transition/v1" ||
		receipt.BaseCommit != "cd6adf31b510ab47e08386cedfa19b6eaad978f0" ||
		receipt.Scope != "codex-0.149.1-service-successor-replay" ||
		receipt.FrameworkStage != "VC-3/VC-4/HISTORICAL-VALIDATOR-CLOSURE" ||
		receipt.Result != "service_successor_replay_transition_frozen" {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放 transition 时间非法")
	}
	if !receipt.Verification.R15ReceiptIdentityVerified ||
		!receipt.Verification.SuccessorReceiptIdentityVerified ||
		!receipt.Verification.PredecessorChainVerified ||
		!receipt.Verification.TransitionGraphReplayed ||
		!receipt.Verification.MultiHopReplayTested ||
		!receipt.Verification.MutationTestsRequired {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放验证事实非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.OfficialRecapturePerformed ||
		receipt.Safety.CandidateCapturePerformed || receipt.Safety.DeploymentPerformed ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.ProductionSelectorChanged || receipt.Safety.ProductionActivated ||
		receipt.Safety.ARM64Accessed || receipt.Safety.VircsAccessed ||
		receipt.Safety.DMITServerAccessed {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放安全边界非法")
	}

	expectedBindings := codex01491ServiceSuccessorExpectedBindings()
	if len(receipt.SuccessorChain) != len(expectedBindings) {
		return nil, errors.New("Codex 0.149.1 officialegress service r16-r24 后继数量非法")
	}
	predecessorPath := codex01491ServiceSuccessorR15Path
	predecessorSHA256 := codex01491ServiceSuccessorR15SHA256
	predecessorIdentity := codex01491ServiceSuccessorR15ID
	for index, expected := range expectedBindings {
		binding := receipt.SuccessorChain[index]
		if binding.Path != expected.Path || binding.FileSHA256 != expected.FileSHA256 ||
			binding.IdentitySHA256 != expected.IdentitySHA256 {
			return nil, errors.New("Codex 0.149.1 officialegress service 后继绑定非法：" + expected.Path)
		}
		raw, err := codex01491RepoFile(expected.Path)
		if err != nil || upstreamMergeFrameworkDigest(raw) != expected.FileSHA256 {
			return nil, errors.New("Codex 0.149.1 officialegress service 后继收据摘要非法：" + expected.Path)
		}
		var historical codex01491ServiceSuccessorHistoricalReceipt
		if err := json.Unmarshal(raw, &historical); err != nil {
			return nil, err
		}
		if err := codex01491VerifyIdentity(raw, expected.IdentitySHA256); err != nil {
			return nil, err
		}
		if historical.IdentitySHA256 != expected.IdentitySHA256 ||
			binding.SchemaVersion != historical.SchemaVersion ||
			binding.Scope != historical.Scope || binding.Result != historical.Result ||
			historical.PredecessorTransition.Path != predecessorPath ||
			historical.PredecessorTransition.FileSHA256 != predecessorSHA256 ||
			historical.PredecessorTransition.IdentitySHA256 != predecessorIdentity {
			return nil, errors.New("Codex 0.149.1 officialegress service 后继收据链不连续：" + expected.Path)
		}
		predecessorPath = expected.Path
		predecessorSHA256 = expected.FileSHA256
		predecessorIdentity = expected.IdentitySHA256
	}

	expectedPaths := codex01491ServiceSuccessorReplayExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放路径数量非法")
	}
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r24_selector_lite_coordinate_transition_test.go":     {"ab35846c004138b441e9e1b2a1045e78f09755c8ce0089f6eb4f5c222c62a4a9"},
		"backend/internal/service/codex_01491_candidate_source_transition_test.go":                        {"14adcc0c3ab4826dab7f0edf13bb94e4a2f6661651a802fe5c27a8770365cb54"},
		"backend/internal/service/codex_01491_r15_formal_classification_transition_test.go":               {"198e4ee6c30020868887a793dacea60649c6ea23f0816c7adf197e618a042a65"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py":     {"23fe94c453166c079e5c1f1df8bfde7b0beba7e7bfec0c47118795b004c28948"},
		"tools/official_client_capture/tests/test_codex_01491_r24_selector_lite_coordinate_transition.py": {"7f995f92d901d0f9d587242ca4ae7fabc2d6b0eaf937f06b6f4bcd68cad54099"},
	}
	edges := make(map[string]map[string][]string)
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		expectedFrom := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(expectedFrom) > 0 {
			expectedChange = "modified"
		}
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, expectedFrom) ||
			len(entry.ToSHA256) != 64 || strings.TrimSpace(entry.Reason) == "" {
			return nil, errors.New("Codex 0.149.1 officialegress service 后继重放条目非法：" + entry.Path)
		}
		current, err := codex01491RepoFile(entry.Path)
		if err != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return nil, errors.New("Codex 0.149.1 officialegress service 后继重放当前摘要不一致：" + entry.Path)
		}
		if len(expectedFrom) > 0 {
			edges[entry.Path] = make(map[string][]string)
			for _, predecessor := range expectedFrom {
				edges[entry.Path][predecessor] = []string{entry.ToSHA256}
			}
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return nil, err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return nil, errors.New("Codex 0.149.1 officialegress service 后继重放路径摘要不一致")
	}
	return edges, nil
}

// codex01491ServiceSuccessorReplaySupersedes 只消费本次追加收据固定的
// 精确 path／from／to，不改变运行时 Catalog、模型坐标或网络策略。
func codex01491ServiceSuccessorReplaySupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if priorDigest == currentDigest || len(priorDigest) != 64 || len(currentDigest) != 64 {
		return false
	}
	if _, err := loadCodex01491ServiceSuccessorReplayTransition(); err != nil {
		return false
	}
	edges := codex01491ServiceSuccessorReplayEdges[path]
	queue := []string{priorDigest}
	visited := map[string]bool{priorDigest: true}
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		for _, successor := range edges[current] {
			if successor == currentDigest {
				return true
			}
			if !visited[successor] {
				visited[successor] = true
				queue = append(queue, successor)
			}
		}
	}
	return false
}

func TestCodex01491ServiceSuccessorReplayTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491ServiceSuccessorReplayTransition()
	if err != nil {
		t.Fatal(err)
	}
	r24Path := "backend/internal/officialegress/codex_01491_r24_selector_lite_coordinate_transition_test.go"
	r24Raw, err := codex01491RepoFile(r24Path)
	if err != nil {
		t.Fatal(err)
	}
	if !codex01491ServiceSuccessorReplaySupersedes(
		r24Path,
		"ab35846c004138b441e9e1b2a1045e78f09755c8ce0089f6eb4f5c222c62a4a9",
		upstreamMergeFrameworkDigest(r24Raw),
	) || codex01491ServiceSuccessorReplaySupersedes(
		r24Path,
		strings.Repeat("0", 64),
		upstreamMergeFrameworkDigest(r24Raw),
	) {
		t.Fatal("Codex 0.149.1 officialegress service 后继重放精确三元组判据非法")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491ServiceSuccessorReplayTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 officialegress service 后继重放接受了网络配置变更")
	}
}
