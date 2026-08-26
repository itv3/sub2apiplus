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
	codex01491CandidateSourceTransitionPath = "docs/egress/maintenance/codex-0.149.1-candidate-source-transition.json"
	codex01491CandidateSourcePathCount      = 220
	codex01491CandidateSourcePathSetSHA256  = "f660a1b9af62eda0fa4d0062104a9b2a7c88d4e9e0c6e356cdd1850981bcc418"
	codex01491CandidateGateSuccessorPath    = "docs/egress/maintenance/codex-0.149.1-candidate-gate-successor-transition.json"
	codex01491CandidateSurfaceSuccessorPath = "docs/egress/maintenance/codex-0.149.1-egress-surface-successor-transition.json"
)

type codex01491CandidateSourceTransitionEntry struct {
	Path               string   `json:"path"`
	Change             string   `json:"change"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type codex01491CandidateSourceTransitionReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	IssuedAtUTC           string `json:"issued_at_utc"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	FrameworkStage        string `json:"framework_stage"`
	PredecessorTransition struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	Campaign struct {
		ID                string `json:"id"`
		Purpose           string `json:"purpose"`
		TargetVersion     string `json:"target_version"`
		TargetProfileSHA  string `json:"target_profile_sha256"`
		RequiredRuleCount int    `json:"required_rule_count"`
		ScenarioCount     int    `json:"scenario_count"`
	} `json:"campaign"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		ProfileStaged                   bool `json:"profile_staged"`
		SourceDigestsVerified           bool `json:"source_digests_verified"`
		TargetedFunctionalTestsPassed   bool `json:"targeted_functional_tests_passed"`
		RepositoryWideGatesPending      bool `json:"repository_wide_gates_pending"`
		HistoricalReceiptReplayRequired bool `json:"historical_receipt_replay_required"`
	} `json:"verification"`
	Safety struct {
		ActiveRemained01470          bool `json:"active_remained_0_147_0"`
		PreviousStaged01491          bool `json:"previous_staged_0_149_1"`
		CandidateCatalogStaged       bool `json:"candidate_catalog_staged"`
		ProductionSelectorChanged    bool `json:"production_selector_changed"`
		HistoricalReceiptsModified   bool `json:"historical_receipts_modified"`
		HistoricalSnapshotsOverwrote bool `json:"historical_snapshots_overwritten"`
		DeploymentPerformed          bool `json:"deployment_performed"`
		LiveRequestSent              bool `json:"live_request_sent"`
		ARM64ReadOnlyDiagnostics     bool `json:"arm64_read_only_diagnostics_performed"`
		VircsAccessed                bool `json:"vircs_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491CandidateSourceOnce     sync.Once
	codex01491CandidateSourceCached   codex01491CandidateSourceTransitionReceipt
	codex01491CandidateSourceLoadErr  error
	codex01491CandidateGateOnce       sync.Once
	codex01491CandidateGateCached     codex01491CandidateGateSuccessorReceipt
	codex01491CandidateGateLoadErr    error
	codex01491CandidateSurfaceOnce    sync.Once
	codex01491CandidateSurfaceCached  codex01491CandidateSurfaceSuccessorReceipt
	codex01491CandidateSurfaceLoadErr error
)

type codex01491CandidateGateSuccessorReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	IssuedAtUTC           string `json:"issued_at_utc"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	FrameworkStage        string `json:"framework_stage"`
	PredecessorTransition struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	Campaign struct {
		ID               string `json:"id"`
		Purpose          string `json:"purpose"`
		TargetVersion    string `json:"target_version"`
		TargetProfileSHA string `json:"target_profile_sha256"`
	} `json:"campaign"`
	StagedCatalog struct {
		ActiveVersion              string `json:"active_version"`
		PreviousVersion            string `json:"previous_version"`
		ReleaseCatalogPath         string `json:"release_catalog_path"`
		ReleaseCatalogSHA256       string `json:"release_catalog_sha256"`
		ReleaseGraphPath           string `json:"release_graph_path"`
		ReleaseGraphSHA256         string `json:"release_graph_sha256"`
		ContractReleaseGraphPath   string `json:"contract_release_graph_path"`
		ContractReleaseGraphSHA256 string `json:"contract_release_graph_sha256"`
	} `json:"staged_catalog"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		CandidateSourceTransitionReplayed bool `json:"candidate_source_transition_replayed"`
		HistoricalTransitionChainReplayed bool `json:"historical_transition_chain_replayed"`
		MutationTestsPassed               bool `json:"mutation_tests_passed"`
		StagedCatalogStateVerified        bool `json:"staged_catalog_state_verified"`
		TargetedTestsPassed               bool `json:"targeted_tests_passed"`
	} `json:"verification"`
	Safety struct {
		ActiveRemained01470                          bool `json:"active_remained_0_147_0"`
		ARM64AccessedForThisTransition               bool `json:"arm64_accessed_for_this_transition"`
		CatalogPromoted                              bool `json:"catalog_promoted"`
		DeploymentPerformed                          bool `json:"deployment_performed"`
		HistoricalContentAddressedArtifactsOverwrote bool `json:"historical_content_addressed_artifacts_overwritten"`
		HistoricalReceiptsModified                   bool `json:"historical_receipts_modified"`
		HistoricalTransitionsModified                bool `json:"historical_transitions_modified"`
		LiveRequestSent                              bool `json:"live_request_sent"`
		PreviousStaged01491                          bool `json:"previous_staged_0_149_1"`
		ProductionSelectorChanged                    bool `json:"production_selector_changed"`
		VircsAccessed                                bool `json:"vircs_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

type codex01491CandidateSurfaceSuccessorReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	IssuedAtUTC           string `json:"issued_at_utc"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	PredecessorTransition struct {
		Path       string `json:"path"`
		FileSHA256 string `json:"file_sha256"`
	} `json:"predecessor_transition"`
	CandidateGateTransition struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"candidate_gate_transition"`
	BaseInventory             json.RawMessage                            `json:"base_inventory"`
	Additions                 json.RawMessage                            `json:"additions"`
	Removals                  []string                                   `json:"removals"`
	ResultingSurfaceCount     int                                        `json:"resulting_surface_count"`
	ImplementationTransitions []codex01491CandidateSourceTransitionEntry `json:"implementation_transitions"`
	Safety                    json.RawMessage                            `json:"safety"`
	Result                    string                                     `json:"result"`
	IdentitySHA256            string                                     `json:"identity_sha256"`
}

func codex01491CandidateGateExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_candidate_source_transition_test.go",
		"backend/internal/officialegress/routing_hint.go",
		"backend/internal/service/codex_01491_candidate_source_transition_test.go",
		"backend/internal/service/official_egress_changeset5_final_wire_test.go",
		"backend/internal/service/official_egress_codex_0145_profile.go",
		"backend/internal/service/official_egress_codex_0145_profile_test.go",
		"backend/internal/service/openai_forward_plan_test.go",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_capture_runtime_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_direct_readiness_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_egress_gate_chain_repair_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_target_scenario_binding_transition.py",
	}
}

func codex01491CandidateSurfaceExpectedPaths() []string {
	return []string{
		"backend/cmd/egressruntimedump/main.go",
		"backend/internal/officialegress/codex_01491_candidate_source_transition_test.go",
		"backend/internal/officialegress/runtime_catalog_files.go",
		"backend/internal/officialegress/runtime_catalog_files_test.go",
		"backend/internal/officialegress/upstream_merge_framework_transition_test.go",
		"backend/internal/service/codex_01491_candidate_source_transition_test.go",
		"tools/check_ledger_completeness.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_egress_surface_successor_transition.py",
	}
}

func loadCodex01491CandidateSurfaceSuccessorTransition() (
	codex01491CandidateSurfaceSuccessorReceipt,
	error,
) {
	codex01491CandidateSurfaceOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491CandidateSurfaceSuccessorPath)
		if err != nil {
			codex01491CandidateSurfaceLoadErr = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491CandidateSurfaceCached); err != nil {
			codex01491CandidateSurfaceLoadErr = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491CandidateSurfaceCached.IdentitySHA256,
		); err != nil {
			codex01491CandidateSurfaceLoadErr = err
			return
		}
		receipt := codex01491CandidateSurfaceCached
		if receipt.SchemaVersion != "official-client-codex-0.149.1-egress-surface-successor-transition/v1" ||
			receipt.IssuedAtUTC != "2026-08-26T11:50:00Z" ||
			receipt.BaseCommit != "580ac615c759170cfb745e7b71fa02a9e1c3f12e" ||
			receipt.Scope != "codex-0.149.1-egress-surface-successor" ||
			receipt.PredecessorTransition.Path != "docs/egress/validation/egress-surface-transition.json" ||
			receipt.ResultingSurfaceCount != 54 || len(receipt.Removals) != 0 ||
			len(receipt.BaseInventory) == 0 || len(receipt.Additions) == 0 ||
			len(receipt.Safety) == 0 || receipt.Result != "candidate_surface_successor_frozen" {
			codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 顶层事实非法")
			return
		}
		predecessorRaw, readErr := codex01491RepoFile(receipt.PredecessorTransition.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(predecessorRaw) != receipt.PredecessorTransition.FileSHA256 {
			codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 前序绑定非法")
			return
		}
		candidateGateRaw, readErr := codex01491RepoFile(codex01491CandidateGateSuccessorPath)
		if readErr != nil {
			codex01491CandidateSurfaceLoadErr = readErr
			return
		}
		var candidateGate codex01491CandidateGateSuccessorReceipt
		if err := json.Unmarshal(candidateGateRaw, &candidateGate); err != nil {
			codex01491CandidateSurfaceLoadErr = err
			return
		}
		if err := codex01491VerifyIdentity(candidateGateRaw, candidateGate.IdentitySHA256); err != nil {
			codex01491CandidateSurfaceLoadErr = err
			return
		}
		if receipt.CandidateGateTransition.Path != codex01491CandidateGateSuccessorPath ||
			receipt.CandidateGateTransition.FileSHA256 != upstreamMergeFrameworkDigest(candidateGateRaw) ||
			receipt.CandidateGateTransition.IdentitySHA256 != candidateGate.IdentitySHA256 {
			codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 候选门禁绑定非法")
			return
		}
		expectedPaths := codex01491CandidateSurfaceExpectedPaths()
		if len(receipt.ImplementationTransitions) != len(expectedPaths) {
			codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 实现路径数量非法")
			return
		}
		for index, entry := range receipt.ImplementationTransitions {
			if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
				strings.TrimSpace(entry.Reason) == "" || !slices.IsSorted(entry.PredecessorSHA256s) ||
				len(entry.PredecessorSHA256s) != len(slices.Compact(
					append([]string(nil), entry.PredecessorSHA256s...),
				)) {
				codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 实现条目非法")
				return
			}
			if entry.Change == "added" {
				if len(entry.PredecessorSHA256s) != 0 {
					codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 新增条目非法")
					return
				}
			} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
				codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 修改条目非法")
				return
			}
			current, currentErr := codex01491RepoFile(entry.Path)
			if currentErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
				codex01491CandidateSurfaceLoadErr = errors.New("Codex 0.149.1 出站面后继 transition 当前摘要不一致：" + entry.Path)
				return
			}
		}
	})
	return codex01491CandidateSurfaceCached, codex01491CandidateSurfaceLoadErr
}

func codex01491CandidateSurfaceSuccessorSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491CandidateSurfaceSuccessorTransition()
	if err != nil {
		return false
	}
	candidateGateRaw, err := codex01491RepoFile(codex01491CandidateGateSuccessorPath)
	if err != nil {
		return false
	}
	var candidateGate codex01491CandidateGateSuccessorReceipt
	if err := json.Unmarshal(candidateGateRaw, &candidateGate); err != nil {
		return false
	}
	edges := make(map[string][]string)
	for _, entries := range [][]codex01491CandidateSourceTransitionEntry{
		candidateGate.Transitions,
		receipt.ImplementationTransitions,
	} {
		for _, entry := range entries {
			if entry.Path != path {
				continue
			}
			for _, predecessor := range entry.PredecessorSHA256s {
				edges[predecessor] = append(edges[predecessor], entry.ToSHA256)
			}
		}
	}
	queue := []string{priorDigest}
	visited := make(map[string]bool)
	for len(queue) > 0 {
		digest := queue[0]
		queue = queue[1:]
		if digest == currentDigest {
			return true
		}
		if visited[digest] {
			continue
		}
		visited[digest] = true
		queue = append(queue, edges[digest]...)
	}
	return false
}

func loadCodex01491CandidateGateSuccessorTransition() (
	codex01491CandidateGateSuccessorReceipt,
	error,
) {
	codex01491CandidateGateOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491CandidateGateSuccessorPath)
		if err != nil {
			codex01491CandidateGateLoadErr = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491CandidateGateCached); err != nil {
			codex01491CandidateGateLoadErr = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491CandidateGateLoadErr = errors.New("Codex 0.149.1 candidate gate transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491CandidateGateCached.IdentitySHA256,
		); err != nil {
			codex01491CandidateGateLoadErr = err
			return
		}
		codex01491CandidateGateLoadErr = validateCodex01491CandidateGateSuccessorTransition(
			codex01491CandidateGateCached,
		)
	})
	return codex01491CandidateGateCached, codex01491CandidateGateLoadErr
}

func validateCodex01491CandidateGateSuccessorTransition(
	receipt codex01491CandidateGateSuccessorReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-candidate-gate-successor-transition/v1" ||
		receipt.BaseCommit != "580ac615c759170cfb745e7b71fa02a9e1c3f12e" ||
		receipt.Scope != "codex-0.149.1-candidate-gate-successor" ||
		receipt.FrameworkStage != "VC-4/CANDIDATE-GATE-SUCCESSOR" ||
		receipt.Result != "candidate_gate_successor_frozen" {
		return errors.New("Codex 0.149.1 candidate gate transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 candidate gate transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491CandidateSourceTransitionPath)
	if err != nil {
		return err
	}
	var predecessor codex01491CandidateSourceTransitionReceipt
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491CandidateSourceTransitionPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 candidate gate transition 前序绑定非法")
	}
	if receipt.Campaign.ID != "codex-0_149_1-formal-production-replacement-20260826T092125Z-580ac615c" ||
		receipt.Campaign.Purpose != "production_replacement" ||
		receipt.Campaign.TargetVersion != "0.149.1" ||
		receipt.Campaign.TargetProfileSHA != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" {
		return errors.New("Codex 0.149.1 candidate gate transition Campaign 身份非法")
	}
	if receipt.StagedCatalog.ActiveVersion != "0.147.0" ||
		receipt.StagedCatalog.PreviousVersion != "0.149.1" ||
		receipt.StagedCatalog.ReleaseCatalogPath != "backend/internal/officialegress/catalogdata/runtime/release-catalog.json" ||
		receipt.StagedCatalog.ReleaseCatalogSHA256 != "f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259" ||
		receipt.StagedCatalog.ReleaseGraphPath != "backend/internal/officialegress/catalogdata/runtime/release-graphs/591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25.json" ||
		receipt.StagedCatalog.ReleaseGraphSHA256 != "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25" ||
		receipt.StagedCatalog.ContractReleaseGraphPath != "backend/internal/officialegress/releasecontract/testdata/release-graph.json" ||
		receipt.StagedCatalog.ContractReleaseGraphSHA256 != "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25" {
		return errors.New("Codex 0.149.1 candidate gate transition 暂存 Catalog 事实非法")
	}
	for _, binding := range []struct {
		path   string
		digest string
	}{
		{receipt.StagedCatalog.ReleaseCatalogPath, receipt.StagedCatalog.ReleaseCatalogSHA256},
		{receipt.StagedCatalog.ReleaseGraphPath, receipt.StagedCatalog.ReleaseGraphSHA256},
		{receipt.StagedCatalog.ContractReleaseGraphPath, receipt.StagedCatalog.ContractReleaseGraphSHA256},
	} {
		raw, readErr := codex01491RepoFile(binding.path)
		if readErr != nil || upstreamMergeFrameworkDigest(raw) != binding.digest {
			return errors.New("Codex 0.149.1 candidate gate transition 暂存 Catalog 摘要不一致")
		}
	}
	if !receipt.Verification.CandidateSourceTransitionReplayed ||
		!receipt.Verification.HistoricalTransitionChainReplayed ||
		!receipt.Verification.MutationTestsPassed ||
		!receipt.Verification.StagedCatalogStateVerified ||
		!receipt.Verification.TargetedTestsPassed {
		return errors.New("Codex 0.149.1 candidate gate transition 验证事实非法")
	}
	if !receipt.Safety.ActiveRemained01470 || receipt.Safety.ARM64AccessedForThisTransition ||
		receipt.Safety.CatalogPromoted || receipt.Safety.DeploymentPerformed ||
		receipt.Safety.HistoricalContentAddressedArtifactsOverwrote ||
		receipt.Safety.HistoricalReceiptsModified || receipt.Safety.HistoricalTransitionsModified ||
		receipt.Safety.LiveRequestSent || !receipt.Safety.PreviousStaged01491 ||
		receipt.Safety.ProductionSelectorChanged || receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 candidate gate transition 安全边界非法")
	}
	expectedPaths := codex01491CandidateGateExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 candidate gate transition 路径数量非法")
	}
	predecessors := make(map[string]codex01491CandidateSourceTransitionEntry)
	for _, entry := range predecessor.Transitions {
		predecessors[entry.Path] = entry
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		if entry.Path != expectedPaths[index] || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" || !slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 candidate gate transition 条目非法")
		}
		predecessor, ok := predecessors[entry.Path]
		switch {
		case ok:
			if entry.Change != "modified" || !slices.Equal(
				entry.PredecessorSHA256s,
				[]string{predecessor.ToSHA256},
			) {
				return errors.New("Codex 0.149.1 candidate gate transition 前序摘要非法")
			}
		case entry.Path == "tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py":
			if entry.Change != "added" || len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 candidate gate transition 新增条目非法")
			}
		case entry.Path == "tools/official_client_capture/tests/test_codex_01491_target_scenario_binding_transition.py":
			if entry.Change != "modified" || !slices.Equal(
				entry.PredecessorSHA256s,
				[]string{"fe037949dfe4c24a07092572c3edb7678d13ec34e0c1243db532a25c6a1bf4df"},
			) {
				return errors.New("Codex 0.149.1 candidate gate transition target 前序摘要非法")
			}
		case entry.Path == "tools/official_client_capture/tests/test_codex_01491_direct_readiness_transition.py":
			if entry.Change != "modified" || !slices.Equal(
				entry.PredecessorSHA256s,
				[]string{"7de5edbccb9552f89e11c727d65465cf52bf7185422ed345f8d8126053735d2f"},
			) {
				return errors.New("Codex 0.149.1 candidate gate transition direct 前序摘要非法")
			}
		default:
			return errors.New("Codex 0.149.1 candidate gate transition 路径来源非法")
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491CandidateSurfaceSuccessorSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 candidate gate transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 candidate gate transition 路径摘要不一致")
	}
	return nil
}

func codex01491CandidateGateSuccessorSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491CandidateGateSuccessorTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && entry.ToSHA256 == currentDigest &&
			slices.Contains(entry.PredecessorSHA256s, priorDigest) {
			return true
		}
	}
	return false
}

func loadCodex01491CandidateSourceTransition() (
	codex01491CandidateSourceTransitionReceipt,
	error,
) {
	codex01491CandidateSourceOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491CandidateSourceTransitionPath)
		if err != nil {
			codex01491CandidateSourceLoadErr = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491CandidateSourceCached); err != nil {
			codex01491CandidateSourceLoadErr = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491CandidateSourceLoadErr = errors.New("Codex 0.149.1 candidate transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491CandidateSourceCached.IdentitySHA256,
		); err != nil {
			codex01491CandidateSourceLoadErr = err
			return
		}
		codex01491CandidateSourceLoadErr = validateCodex01491CandidateSourceTransition(
			codex01491CandidateSourceCached,
		)
	})
	return codex01491CandidateSourceCached, codex01491CandidateSourceLoadErr
}

func validateCodex01491CandidateSourceTransition(
	receipt codex01491CandidateSourceTransitionReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-candidate-source-transition/v1" ||
		receipt.BaseCommit != "580ac615c759170cfb745e7b71fa02a9e1c3f12e" ||
		receipt.Scope != "codex-0.149.1-candidate-source-transition" ||
		receipt.FrameworkStage != "VC-4/CANDIDATE-SOURCE-CLOSURE" ||
		receipt.PathSetSHA256 != codex01491CandidateSourcePathSetSHA256 ||
		receipt.Result != "candidate_source_transition_frozen_pending_full_gates" {
		return errors.New("Codex 0.149.1 candidate transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 candidate transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491TargetScenarioBindingPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491TargetScenarioBindingPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 candidate transition 前序绑定非法")
	}
	if receipt.Campaign.ID != "codex-0_149_1-formal-production-replacement-20260826T092125Z-580ac615c" ||
		receipt.Campaign.Purpose != "production_replacement" ||
		receipt.Campaign.TargetVersion != "0.149.1" ||
		receipt.Campaign.TargetProfileSHA != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" ||
		receipt.Campaign.RequiredRuleCount != 42 || receipt.Campaign.ScenarioCount != 15 {
		return errors.New("Codex 0.149.1 candidate transition Campaign 身份非法")
	}
	if !receipt.Verification.ProfileStaged || !receipt.Verification.SourceDigestsVerified ||
		!receipt.Verification.TargetedFunctionalTestsPassed ||
		!receipt.Verification.RepositoryWideGatesPending ||
		!receipt.Verification.HistoricalReceiptReplayRequired {
		return errors.New("Codex 0.149.1 candidate transition 验证事实非法")
	}
	if !receipt.Safety.ActiveRemained01470 || !receipt.Safety.PreviousStaged01491 ||
		!receipt.Safety.CandidateCatalogStaged || receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.HistoricalReceiptsModified || receipt.Safety.HistoricalSnapshotsOverwrote ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.LiveRequestSent ||
		!receipt.Safety.ARM64ReadOnlyDiagnostics || receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 candidate transition 安全边界非法")
	}
	if len(receipt.Transitions) != codex01491CandidateSourcePathCount {
		return errors.New("Codex 0.149.1 candidate transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" || !slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), entry.PredecessorSHA256s...),
			)) {
			return errors.New("Codex 0.149.1 candidate transition 条目非法")
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 candidate transition 新增条目存在前序摘要")
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 candidate transition 修改条目缺少前序摘要")
		}
		for _, predecessorSHA := range entry.PredecessorSHA256s {
			if len(predecessorSHA) != 64 || predecessorSHA == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 candidate transition 前序摘要非法")
			}
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491CandidateGateSuccessorSupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			) && !codex01491CandidateSurfaceSuccessorSupersedes(
			entry.Path,
			entry.ToSHA256,
			currentDigest,
		)) {
			return errors.New("Codex 0.149.1 candidate transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Codex 0.149.1 candidate transition 路径未严格排序")
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != codex01491CandidateSourcePathSetSHA256 {
		return errors.New("Codex 0.149.1 candidate transition 路径闭集摘要不一致")
	}
	return nil
}

// codex01491CandidateSourceTransitionSupersedes 只承认本次候选收据中固定的
// path／predecessor／to 三元组。它只修复追加式摘要链，不修改历史收据、画像或生产选择器。
func codex01491CandidateSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491CandidateSourceTransition()
	if err != nil {
		return false
	}
	if codex01491CandidateGateSuccessorSupersedes(
		path,
		priorDigest,
		currentDigest,
	) {
		return true
	}
	if codex01491CandidateSurfaceSuccessorSupersedes(
		path,
		priorDigest,
		currentDigest,
	) {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path != path || !slices.Contains(entry.PredecessorSHA256s, priorDigest) {
			continue
		}
		if entry.ToSHA256 == currentDigest || codex01491CandidateGateSuccessorSupersedes(
			path,
			entry.ToSHA256,
			currentDigest,
		) || codex01491CandidateSurfaceSuccessorSupersedes(
			path,
			entry.ToSHA256,
			currentDigest,
		) {
			return true
		}
	}
	return false
}

func TestCodex01491CandidateSourceTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491CandidateSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	var modified codex01491CandidateSourceTransitionEntry
	for _, entry := range receipt.Transitions {
		if entry.Change == "modified" {
			modified = entry
			break
		}
	}
	if modified.Path == "" || !codex01491CandidateSourceTransitionSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 candidate transition 精确三元组未被承认")
	}
	if codex01491CandidateSourceTransitionSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 candidate transition 接受了未知前序摘要")
	}
}

func TestCodex01491CandidateGateSuccessorTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491CandidateGateSuccessorTransition()
	if err != nil {
		t.Fatal(err)
	}
	entry := receipt.Transitions[0]
	if !codex01491CandidateGateSuccessorSupersedes(
		entry.Path,
		entry.PredecessorSHA256s[0],
		entry.ToSHA256,
	) || codex01491CandidateGateSuccessorSupersedes(
		entry.Path,
		strings.Repeat("0", 64),
		entry.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 candidate gate transition 精确三元组判据非法")
	}
}
