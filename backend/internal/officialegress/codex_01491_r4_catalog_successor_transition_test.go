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

const codex01491R4CatalogSuccessorPath = "docs/egress/maintenance/codex-0.149.1-r4-catalog-successor-transition.json"

type codex01491R4CatalogBinding struct {
	Path              string `json:"path"`
	PredecessorSHA256 string `json:"predecessor_sha256,omitempty"`
	SHA256            string `json:"sha256"`
}

type codex01491R4CatalogSuccessorReceipt struct {
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
		ID                  string `json:"id"`
		OfficialAttemptID   string `json:"official_attempt_id"`
		Purpose             string `json:"purpose"`
		TargetVersion       string `json:"target_version"`
		TargetProfileSHA256 string `json:"target_profile_sha256"`
		ClassificationSHA   string `json:"classification_sha256"`
		ReviewSHA256        string `json:"review_sha256"`
		RequiredRuleCount   int    `json:"required_rule_count"`
		DiscoveryCount      int    `json:"discovery_count"`
		CaptureAccountRef   string `json:"capture_account_ref"`
		APIKeyRef           string `json:"api_key_ref"`
	} `json:"campaign"`
	StagedCatalog struct {
		ActiveVersion             string                     `json:"active_version"`
		PreviousVersion           string                     `json:"previous_version"`
		ActiveUnchanged           bool                       `json:"active_unchanged"`
		ProductionSelectorChanged bool                       `json:"production_selector_changed"`
		ReleaseCatalog            codex01491R4CatalogBinding `json:"release_catalog"`
		ReleaseGraph              codex01491R4CatalogBinding `json:"release_graph"`
		ContractReleaseGraph      codex01491R4CatalogBinding `json:"contract_release_graph"`
		InventorySHA256           string                     `json:"inventory_sha256"`
	} `json:"staged_catalog"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		OfficialAttemptSealed             bool `json:"official_attempt_sealed"`
		ClassificationApproved            bool `json:"classification_approved"`
		AllRulesMapped                    bool `json:"all_rules_mapped"`
		AllDiscoveriesMapped              bool `json:"all_discoveries_mapped"`
		SecretScanClean                   bool `json:"secret_scan_clean"`
		CatalogInventoryVerified          bool `json:"catalog_inventory_verified"`
		HistoricalTransitionChainReplayed bool `json:"historical_transition_chain_replayed"`
		MutationTestsPassed               bool `json:"mutation_tests_passed"`
	} `json:"verification"`
	Safety struct {
		ActiveRemained01470                          bool `json:"active_remained_0_147_0"`
		PreviousStaged01491                          bool `json:"previous_staged_0_149_1"`
		CandidateCatalogStaged                       bool `json:"candidate_catalog_staged"`
		ProductionSelectorChanged                    bool `json:"production_selector_changed"`
		HistoricalContentAddressedArtifactsOverwrote bool `json:"historical_content_addressed_artifacts_overwritten"`
		HistoricalReceiptsModified                   bool `json:"historical_receipts_modified"`
		HistoricalTransitionsModified                bool `json:"historical_transitions_modified"`
		DeploymentPerformed                          bool `json:"deployment_performed"`
		LiveRequestSent                              bool `json:"live_request_sent"`
		ARM64AccessedForThisTransition               bool `json:"arm64_accessed_for_this_transition"`
		VircsAccessed                                bool `json:"vircs_accessed"`
		CaptureAccount20Used                         bool `json:"capture_account_20_used"`
		CaptureAccount21Used                         bool `json:"capture_account_21_used"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491R4CatalogSuccessorOnce    sync.Once
	codex01491R4CatalogSuccessorCached  codex01491R4CatalogSuccessorReceipt
	codex01491R4CatalogSuccessorLoadErr error
)

func codex01491R4CatalogSuccessorExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
		"backend/internal/officialegress/catalogdata/runtime/release-graphs/9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4.json",
		"backend/internal/officialegress/codex_01491_candidate_source_transition_test.go",
		"backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
		"backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go",
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json",
		"backend/internal/service/codex_01491_candidate_source_transition_test.go",
		"backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go",
		"backend/internal/service/official_egress_changeset5_final_wire_test.go",
		"backend/internal/service/official_egress_profile_test.go",
		"tools/check_ledger_completeness.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py",
	}
}

func loadCodex01491R4CatalogSuccessorTransition() (
	codex01491R4CatalogSuccessorReceipt,
	error,
) {
	codex01491R4CatalogSuccessorOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R4CatalogSuccessorPath)
		if err != nil {
			codex01491R4CatalogSuccessorLoadErr = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R4CatalogSuccessorCached); err != nil {
			codex01491R4CatalogSuccessorLoadErr = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R4CatalogSuccessorLoadErr = errors.New("Codex 0.149.1 r4 Catalog 后继 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R4CatalogSuccessorCached.IdentitySHA256,
		); err != nil {
			codex01491R4CatalogSuccessorLoadErr = err
			return
		}
		codex01491R4CatalogSuccessorLoadErr = validateCodex01491R4CatalogSuccessorTransition(
			codex01491R4CatalogSuccessorCached,
		)
	})
	return codex01491R4CatalogSuccessorCached, codex01491R4CatalogSuccessorLoadErr
}

func validateCodex01491R4CatalogSuccessorTransition(
	receipt codex01491R4CatalogSuccessorReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r4-catalog-successor-transition/v1" ||
		receipt.BaseCommit != "6d773d01aa5c81ec949355976568c770d1977207" ||
		receipt.Scope != "codex-0.149.1-r4-catalog-successor" ||
		receipt.FrameworkStage != "VC-3/CANDIDATE-CATALOG-SUCCESSOR" ||
		receipt.Result != "r4_candidate_catalog_successor_frozen" {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491CandidateSurfaceSuccessorPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491CandidateSurfaceSuccessorPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 前序绑定非法")
	}
	if receipt.Campaign.ID != "codex-0_149_1-formal-production-replacement-20260826T140949Z-2c1ab3b9e-r4" ||
		receipt.Campaign.OfficialAttemptID != "20260826T141046Z-90545b2a079aa94a" ||
		receipt.Campaign.Purpose != "production_replacement" ||
		receipt.Campaign.TargetVersion != "0.149.1" ||
		receipt.Campaign.TargetProfileSHA256 != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" ||
		receipt.Campaign.ClassificationSHA != "058bb9a2d78ba64ecda1a2a8025158b6cf64f0183a931a2fd1b519146838a599" ||
		receipt.Campaign.ReviewSHA256 != "a72a22ef4ec9d8d78329d313175c4cbff97e0537b19eafde0aebadbf83f436bd" ||
		receipt.Campaign.RequiredRuleCount != 42 || receipt.Campaign.DiscoveryCount != 2090 ||
		receipt.Campaign.CaptureAccountRef != "#21" || receipt.Campaign.APIKeyRef != "#4" {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition Campaign 身份非法")
	}
	if receipt.StagedCatalog.ActiveVersion != "0.147.0" ||
		receipt.StagedCatalog.PreviousVersion != "0.149.1" ||
		!receipt.StagedCatalog.ActiveUnchanged || receipt.StagedCatalog.ProductionSelectorChanged ||
		receipt.StagedCatalog.ReleaseCatalog != (codex01491R4CatalogBinding{
			Path:              "backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
			PredecessorSHA256: "f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259",
			SHA256:            "c26b274a5942eee249ae4755aa60c399745f03212077e798fb5d582f0ce6c81e",
		}) || receipt.StagedCatalog.ReleaseGraph != (codex01491R4CatalogBinding{
		Path:   "backend/internal/officialegress/catalogdata/runtime/release-graphs/9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4.json",
		SHA256: "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4",
	}) || receipt.StagedCatalog.ContractReleaseGraph != (codex01491R4CatalogBinding{
		Path:              "backend/internal/officialegress/releasecontract/testdata/release-graph.json",
		PredecessorSHA256: "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25",
		SHA256:            "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4",
	}) || receipt.StagedCatalog.InventorySHA256 != "f00545f1296175c1a60f51ec770bf050832dbf80bbc61569fbfe9ec78759111b" {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 暂存 Catalog 事实非法")
	}
	if !receipt.Verification.OfficialAttemptSealed || !receipt.Verification.ClassificationApproved ||
		!receipt.Verification.AllRulesMapped || !receipt.Verification.AllDiscoveriesMapped ||
		!receipt.Verification.SecretScanClean || !receipt.Verification.CatalogInventoryVerified ||
		!receipt.Verification.HistoricalTransitionChainReplayed || !receipt.Verification.MutationTestsPassed {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 验证事实非法")
	}
	if !receipt.Safety.ActiveRemained01470 || !receipt.Safety.PreviousStaged01491 ||
		!receipt.Safety.CandidateCatalogStaged || receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.HistoricalContentAddressedArtifactsOverwrote ||
		receipt.Safety.HistoricalReceiptsModified || receipt.Safety.HistoricalTransitionsModified ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.LiveRequestSent ||
		receipt.Safety.ARM64AccessedForThisTransition || receipt.Safety.VircsAccessed ||
		receipt.Safety.CaptureAccount20Used || !receipt.Safety.CaptureAccount21Used {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 安全边界非法")
	}
	expectedPaths := codex01491R4CatalogSuccessorExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 路径数量非法")
	}
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json":                    {"f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259"},
		"backend/internal/officialegress/codex_01491_candidate_source_transition_test.go":             {"84df55efad07da2e5efa3b150fe1eb1745817aff12956ee2b6ff3763d3a0dcfe"},
		"backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go":              {"e5983e782e6875937ae82e4271f3406ef745b1dbd0d07985afd9172bcb86475c"},
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json":                 {"591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25"},
		"backend/internal/service/codex_01491_candidate_source_transition_test.go":                    {"d870c8d511544e7ea81e0153fa722f00fa8ca1e98b70144bea071f3c95a426fb"},
		"backend/internal/service/official_egress_changeset5_final_wire_test.go":                      {"687a27153325f0d242f8bbd363ac4cf2bde249c92b9396bd98ef35de5b4c05b9"},
		"backend/internal/service/official_egress_profile_test.go":                                    {"f1a744dc697603b5983cf7c94c4af900238fe633068701636f0ddfb0ead18591"},
		"tools/check_ledger_completeness.py":                                                          {"3eea18d00402462c4652f58f9d7d482523acb52c38bf6ed192bc8a53a20d1685"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"2c364f6f6221c38a89c06b0b6aaf30d0fd067956e43b2f33575c134410e29696"},
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		expectedFrom := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(expectedFrom) > 0 {
			expectedChange = "modified"
		}
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, expectedFrom) || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" {
			return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R9ContaminationRecoverySupersedes(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			) && !codex01491R13CandidateCoordinateSupersedes(
			entry.Path,
			entry.ToSHA256,
			currentDigest,
		)) {
			return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r4 Catalog 后继 transition 路径摘要不一致")
	}
	return nil
}

// codex01491R4CatalogSuccessorSupersedes 重放 r4 与 r9 污染恢复的追加式摘要链。
func codex01491R4CatalogSuccessorSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R4CatalogSuccessorTransition()
	if err != nil {
		return false
	}
	recovery, err := loadCodex01491R9ContaminationRecoveryTransition()
	if err != nil {
		return false
	}
	h1Transitions, err := loadCodex01491ModelCatalogH1SuccessorTransitions()
	if err != nil {
		return false
	}
	r13, err := loadCodex01491R13CandidateCoordinateTransition()
	if err != nil {
		return false
	}
	edges := make(map[string][]string)
	for _, transitions := range [][]codex01491CandidateSourceTransitionEntry{
		receipt.Transitions,
		h1Transitions,
		recovery.Transitions,
		r13.Transitions,
	} {
		for _, entry := range transitions {
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

func TestCodex01491R4CatalogSuccessorTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R4CatalogSuccessorTransition()
	if err != nil {
		t.Fatal(err)
	}
	entry := receipt.Transitions[0]
	if !codex01491R4CatalogSuccessorSupersedes(
		entry.Path,
		entry.PredecessorSHA256s[0],
		entry.ToSHA256,
	) || codex01491R4CatalogSuccessorSupersedes(
		entry.Path,
		strings.Repeat("0", 64),
		entry.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r4 Catalog 后继 transition 精确三元组判据非法")
	}
}
