package service

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
	SchemaVersion  string                                   `json:"schema_version"`
	IssuedAtUTC    string                                   `json:"issued_at_utc"`
	BaseCommit     string                                   `json:"base_commit"`
	Scope          string                                   `json:"scope"`
	FrameworkStage string                                   `json:"framework_stage"`
	SuccessorChain []codex01491ServiceSuccessorChainBinding `json:"successor_chain"`
	PathSetSHA256  string                                   `json:"path_set_sha256"`
	Transitions    []codex01491CandidateSourceServiceEntry  `json:"transitions"`
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
	Transitions    []codex01491CandidateSourceServiceEntry `json:"transitions"`
	Result         string                                  `json:"result"`
	IdentitySHA256 string                                  `json:"identity_sha256"`
}

type codex01491ServiceSuccessorReplayGraph map[string]map[string][]string

var (
	codex01491ServiceSuccessorReplayOnce   sync.Once
	codex01491ServiceSuccessorReplayCached codex01491ServiceSuccessorReplayReceipt
	codex01491ServiceSuccessorReplayEdges  codex01491ServiceSuccessorReplayGraph
	codex01491ServiceSuccessorReplayError  error
)

func codex01491ServiceSuccessorExpectedChain() []codex01491ServiceSuccessorChainBinding {
	return []codex01491ServiceSuccessorChainBinding{
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r16-successor-carry-forward-transition.json",
			FileSHA256:     "97286f9cfec8fe2e0c4c60b7ea962bfa7e98bdaa3b083fe736628e68719ef222",
			IdentitySHA256: "bc8dd8444a5ba65ff30d0439a3e8937c4bf4ea9e2f0362a12b540789af1670c5",
			SchemaVersion:  "official-client-codex-0.149.1-r16-successor-carry-forward-transition/v1",
			Scope:          "codex-0.149.1-r16-successor-carry-forward",
			Result:         "successor_carry_forward_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r17-kilo-model-contract-transition.json",
			FileSHA256:     "67b78fc1f4db41dad476e3de3c83ea2ebabf3c33ff676502d1dda293ab8a3555",
			IdentitySHA256: "b7b7534f89f9d79e66970d54a5124b82e795693e75b32c26f9bd7a926169396c",
			SchemaVersion:  "official-client-codex-0.149.1-r17-kilo-model-contract-transition/v1",
			Scope:          "codex-0.149.1-r17-kilo-model-contract",
			Result:         "kilo_model_contract_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r18-successor-account-transition.json",
			FileSHA256:     "0d3fe228ab2fe418e07a31e2f60e8fcf0a72d23b3b2033e6b248117d694de361",
			IdentitySHA256: "39ab82fad57031021db3dd7e4a8dfb355317935cd4aac069b17554df593bdef8",
			SchemaVersion:  "official-client-codex-0.149.1-r18-successor-account-transition/v1",
			Scope:          "codex-0.149.1-r18-successor-account",
			Result:         "successor_account_transition_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r19-successor-chain-transition.json",
			FileSHA256:     "34a6ced196f49fd38a940969662f8292d3417dbad5da6a15c5cc38f1421e0226",
			IdentitySHA256: "6bf51f5a27d288833ba862ef6f91782aba17a2d6e6afdc8da200678b240ef14e",
			SchemaVersion:  "official-client-codex-0.149.1-r19-successor-chain-transition/v1",
			Scope:          "codex-0.149.1-r19-successor-chain",
			Result:         "successor_chain_replay_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r20-candidate-aux-transition.json",
			FileSHA256:     "d35fda7dd6a9250286821b5ce96a741ff650ded48a6005a78209008615db3d2d",
			IdentitySHA256: "6b928fec7aebf5424b48b86bda21aa6e1f5fd5429f41c09bb7d5b72b2d370c06",
			SchemaVersion:  "official-client-codex-0.149.1-r20-candidate-aux-transition/v1",
			Scope:          "codex-0.149.1-r20-candidate-aux",
			Result:         "candidate_aux_capture_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r21-classification-fact-correction-transition.json",
			FileSHA256:     "6d4195f248c12538363df71aefcd9ac4c1b98220effaa1024ff3c3c46515abc9",
			IdentitySHA256: "aa67d69ff36b6bf8ff01fcbbf36779fdb18d28d2e882ae82ee332498f3e4be33",
			SchemaVersion:  "official-client-codex-0.149.1-r21-classification-fact-correction-transition/v1",
			Scope:          "codex-0.149.1-r21-classification-fact-correction",
			Result:         "classification_fact_correction_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r22-candidate-catalog-transition.json",
			FileSHA256:     "e24ad2b2b956bfaa985ead30850623e99428a0ea0a5b84a3e90b84843c94a292",
			IdentitySHA256: "777fc13ced7f8a1ebb66d44864147dd4a86511bf4f0f76bd8fe2c336cbf05ad3",
			SchemaVersion:  "official-client-codex-0.149.1-r22-candidate-catalog-transition/v1",
			Scope:          "codex-0.149.1-r22-candidate-catalog",
			Result:         "r22_candidate_catalog_staged",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r23-runtime-coordinate-transition.json",
			FileSHA256:     "7b2b3cc09dd56d095230243c0e77c3f38c97b256bbaca43fb50bd61d51fdb1b9",
			IdentitySHA256: "c07a935f50e022a2469e234c71bf76d3e59272ac121698df7f33e6bf70106356",
			SchemaVersion:  "official-client-codex-0.149.1-r23-runtime-coordinate-transition/v1",
			Scope:          "codex-0.149.1-r23-runtime-coordinate",
			Result:         "runtime_coordinate_successor_tooling_frozen",
		},
		{
			Path:           "docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json",
			FileSHA256:     "9d78ecb8851935f40be42866f46f83d097b974a8deba0abe44ab46d242fc39b4",
			IdentitySHA256: "3fd50b27cd48f8a9df0b61e697cdf09f36673f765aa9ef81aed611c09fbed96d",
			SchemaVersion:  "official-client-codex-0.149.1-r24-selector-lite-coordinate-transition/v1",
			Scope:          "codex-0.149.1-r24-selector-lite-coordinate",
			Result:         "selector_lite_coordinate_successor_tooling_frozen",
		},
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
		raw, err := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(codex01491ServiceSuccessorReplayPath),
		))
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
			codex01491ServiceSuccessorReplayError = errors.New("Codex 0.149.1 service 后继重放 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyCandidateGateServiceIdentity(
			raw,
			codex01491ServiceSuccessorReplayCached.IdentitySHA256,
		); err != nil {
			codex01491ServiceSuccessorReplayError = err
			return
		}
		codex01491ServiceSuccessorReplayEdges,
			codex01491ServiceSuccessorReplayError =
			buildCodex01491ServiceSuccessorReplayGraph(
				codex01491ServiceSuccessorReplayCached,
			)
	})
	return codex01491ServiceSuccessorReplayCached,
		codex01491ServiceSuccessorReplayError
}

func validateCodex01491ServiceSuccessorReplayTransition(
	receipt codex01491ServiceSuccessorReplayReceipt,
) error {
	_, err := buildCodex01491ServiceSuccessorReplayGraph(receipt)
	return err
}

func buildCodex01491ServiceSuccessorReplayGraph(
	receipt codex01491ServiceSuccessorReplayReceipt,
) (codex01491ServiceSuccessorReplayGraph, error) {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-service-successor-replay-transition/v1" ||
		receipt.BaseCommit != "cd6adf31b510ab47e08386cedfa19b6eaad978f0" ||
		receipt.Scope != "codex-0.149.1-service-successor-replay" ||
		receipt.FrameworkStage != "VC-3/VC-4/HISTORICAL-VALIDATOR-CLOSURE" ||
		receipt.Result != "service_successor_replay_transition_frozen" {
		return nil, errors.New("Codex 0.149.1 service 后继重放 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return nil, errors.New("Codex 0.149.1 service 后继重放 transition 时间非法")
	}
	if !receipt.Verification.R15ReceiptIdentityVerified ||
		!receipt.Verification.SuccessorReceiptIdentityVerified ||
		!receipt.Verification.PredecessorChainVerified ||
		!receipt.Verification.TransitionGraphReplayed ||
		!receipt.Verification.MultiHopReplayTested ||
		!receipt.Verification.MutationTestsRequired {
		return nil, errors.New("Codex 0.149.1 service 后继重放验证事实非法")
	}
	if receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.OfficialRecapturePerformed ||
		receipt.Safety.CandidateCapturePerformed ||
		receipt.Safety.DeploymentPerformed ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.ProductionActivated || receipt.Safety.ARM64Accessed ||
		receipt.Safety.VircsAccessed || receipt.Safety.DMITServerAccessed {
		return nil, errors.New("Codex 0.149.1 service 后继重放安全边界非法")
	}

	expectedChain := codex01491ServiceSuccessorExpectedChain()
	if !slices.Equal(receipt.SuccessorChain, expectedChain) {
		return nil, errors.New("Codex 0.149.1 service r16-r24 后继绑定非法")
	}
	r15Raw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(codex01491ServiceSuccessorR15Path),
	))
	if err != nil || upstreamMergeFrameworkServiceDigest(r15Raw) != codex01491ServiceSuccessorR15SHA256 {
		return nil, errors.New("Codex 0.149.1 service r15 历史收据摘要非法")
	}
	var r15Historical codex01491ServiceSuccessorHistoricalReceipt
	r15Decoder := json.NewDecoder(bytes.NewReader(r15Raw))
	if err := r15Decoder.Decode(&r15Historical); err != nil ||
		r15Historical.SchemaVersion != "official-client-codex-0.149.1-r15-formal-classification-transition/v1" ||
		r15Historical.Scope != "codex-0.149.1-r15-formal-classification" ||
		r15Historical.Result != "formal_classification_catalog_staged_transition_frozen" ||
		r15Historical.IdentitySHA256 != codex01491ServiceSuccessorR15ID {
		return nil, errors.New("Codex 0.149.1 service r15 历史收据身份非法")
	}
	if err := r15Decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("Codex 0.149.1 service r15 历史收据尾部存在额外 JSON")
	}
	if err := codex01491VerifyCandidateGateServiceIdentity(
		r15Raw,
		codex01491ServiceSuccessorR15ID,
	); err != nil {
		return nil, err
	}

	edges := make(codex01491ServiceSuccessorReplayGraph)
	if err := codex01491ServiceSuccessorAddTransitions(edges, r15Historical.Transitions); err != nil {
		return nil, err
	}
	predecessorPath := codex01491ServiceSuccessorR15Path
	predecessorFileSHA256 := codex01491ServiceSuccessorR15SHA256
	predecessorIdentitySHA256 := codex01491ServiceSuccessorR15ID
	for _, binding := range expectedChain {
		raw, readErr := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(binding.Path),
		))
		if readErr != nil || upstreamMergeFrameworkServiceDigest(raw) != binding.FileSHA256 {
			return nil, errors.New("Codex 0.149.1 service 后继收据摘要非法：" + binding.Path)
		}
		var historical codex01491ServiceSuccessorHistoricalReceipt
		decoder := json.NewDecoder(bytes.NewReader(raw))
		if err := decoder.Decode(&historical); err != nil {
			return nil, err
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			return nil, errors.New("Codex 0.149.1 service 后继收据尾部存在额外 JSON：" + binding.Path)
		}
		if historical.SchemaVersion != binding.SchemaVersion ||
			historical.Scope != binding.Scope || historical.Result != binding.Result ||
			historical.IdentitySHA256 != binding.IdentitySHA256 {
			return nil, errors.New("Codex 0.149.1 service 后继收据顶层事实非法：" + binding.Path)
		}
		if err := codex01491VerifyCandidateGateServiceIdentity(
			raw,
			binding.IdentitySHA256,
		); err != nil {
			return nil, err
		}
		if historical.PredecessorTransition.Path != predecessorPath ||
			historical.PredecessorTransition.FileSHA256 != predecessorFileSHA256 ||
			historical.PredecessorTransition.IdentitySHA256 != predecessorIdentitySHA256 {
			return nil, errors.New("Codex 0.149.1 service 后继收据链不连续：" + binding.Path)
		}
		if err := codex01491ServiceSuccessorAddTransitions(edges, historical.Transitions); err != nil {
			return nil, err
		}
		predecessorPath = binding.Path
		predecessorFileSHA256 = binding.FileSHA256
		predecessorIdentitySHA256 = binding.IdentitySHA256
	}

	expectedPaths := codex01491ServiceSuccessorReplayExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return nil, errors.New("Codex 0.149.1 service 后继重放 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || strings.TrimSpace(entry.Reason) == "" ||
			len(entry.ToSHA256) != 64 {
			return nil, errors.New("Codex 0.149.1 service 后继重放 transition 条目非法：" + entry.Path)
		}
		switch index {
		case 0:
			if entry.Change != "modified" ||
				!slices.Equal(entry.PredecessorSHA256s, []string{
					"ab35846c004138b441e9e1b2a1045e78f09755c8ce0089f6eb4f5c222c62a4a9",
				}) {
				return nil, errors.New("Codex 0.149.1 officialegress r24 校验器后继条目非法")
			}
		case 1:
			if entry.Change != "added" || len(entry.PredecessorSHA256s) != 0 {
				return nil, errors.New("Codex 0.149.1 officialegress 后继重放门禁新增条目非法")
			}
		case 2:
			if entry.Change != "modified" ||
				!slices.Equal(entry.PredecessorSHA256s, []string{
					"14adcc0c3ab4826dab7f0edf13bb94e4a2f6661651a802fe5c27a8770365cb54",
				}) {
				return nil, errors.New("Codex 0.149.1 service candidate 校验器后继条目非法")
			}
		case 3:
			if entry.Change != "modified" ||
				!slices.Equal(entry.PredecessorSHA256s, []string{
					"198e4ee6c30020868887a793dacea60649c6ea23f0816c7adf197e618a042a65",
				}) {
				return nil, errors.New("Codex 0.149.1 service r15 校验器后继条目非法")
			}
		case 4:
			if entry.Change != "added" || len(entry.PredecessorSHA256s) != 0 {
				return nil, errors.New("Codex 0.149.1 service 后继重放门禁新增条目非法")
			}
		case 5:
			if entry.Change != "modified" ||
				!slices.Equal(entry.PredecessorSHA256s, []string{
					"23fe94c453166c079e5c1f1df8bfde7b0beba7e7bfec0c47118795b004c28948",
				}) {
				return nil, errors.New("Codex 0.149.1 Python candidate gate 后继条目非法")
			}
		case 6:
			if entry.Change != "modified" ||
				!slices.Equal(entry.PredecessorSHA256s, []string{
					"7f995f92d901d0f9d587242ca4ae7fabc2d6b0eaf937f06b6f4bcd68cad54099",
				}) {
				return nil, errors.New("Codex 0.149.1 Python r24 后继条目非法")
			}
		case 7:
			if entry.Change != "added" || len(entry.PredecessorSHA256s) != 0 {
				return nil, errors.New("Codex 0.149.1 Python service 后继门禁新增条目非法")
			}
		default:
			return nil, errors.New("Codex 0.149.1 service 后继重放 transition 出现未知路径")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(entry.Path),
		))
		if readErr != nil || upstreamMergeFrameworkServiceDigest(current) != entry.ToSHA256 {
			return nil, errors.New("Codex 0.149.1 service 后继重放当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return nil, err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkServiceDigest(pathRaw) != receipt.PathSetSHA256 {
		return nil, errors.New("Codex 0.149.1 service 后继重放路径摘要不一致")
	}
	if err := codex01491ServiceSuccessorAddTransitions(edges, receipt.Transitions); err != nil {
		return nil, err
	}
	return edges, nil
}

func codex01491ServiceSuccessorAddTransitions(
	edges codex01491ServiceSuccessorReplayGraph,
	transitions []codex01491CandidateSourceServiceEntry,
) error {
	for _, entry := range transitions {
		if strings.TrimSpace(entry.Path) == "" || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 service 后继图 transition 条目非法：" + entry.Path)
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 service 后继图新增条目非法：" + entry.Path)
			}
			continue
		}
		if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 service 后继图修改条目非法：" + entry.Path)
		}
		if edges[entry.Path] == nil {
			edges[entry.Path] = make(map[string][]string)
		}
		for _, predecessor := range entry.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 service 后继图前序摘要非法：" + entry.Path)
			}
			if !slices.Contains(edges[entry.Path][predecessor], entry.ToSHA256) {
				edges[entry.Path][predecessor] = append(
					edges[entry.Path][predecessor],
					entry.ToSHA256,
				)
			}
		}
	}
	return nil
}

// codex01491ServiceSuccessorReplaySupersedes 只重放已经由不可变 r16-r24
// 收据及本次门禁闭合收据固定的精确 path／from／to 边，不扩大任何运行时权限。
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
	if len(edges) == 0 {
		return false
	}
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
	releaseCatalogPath := "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
	releaseCatalogRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(releaseCatalogPath),
	))
	if err != nil {
		t.Fatal(err)
	}
	if !codex01491ServiceSuccessorReplaySupersedes(
		releaseCatalogPath,
		"24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7",
		upstreamMergeFrameworkServiceDigest(releaseCatalogRaw),
	) {
		t.Fatal("Codex 0.149.1 service 后继图未能从 r15 多跳重放到当前 Catalog")
	}
	r15ServicePath := "backend/internal/service/codex_01491_r15_formal_classification_transition_test.go"
	r15ServiceRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(r15ServicePath),
	))
	if err != nil {
		t.Fatal(err)
	}
	if !codex01491ServiceSuccessorReplaySupersedes(
		r15ServicePath,
		"198e4ee6c30020868887a793dacea60649c6ea23f0816c7adf197e618a042a65",
		upstreamMergeFrameworkServiceDigest(r15ServiceRaw),
	) || codex01491ServiceSuccessorReplaySupersedes(
		r15ServicePath,
		strings.Repeat("0", 64),
		upstreamMergeFrameworkServiceDigest(r15ServiceRaw),
	) {
		t.Fatal("Codex 0.149.1 service 后继图精确三元组判据非法")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491ServiceSuccessorReplayTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 service 后继重放 transition 接受了网络配置变更")
	}
}
