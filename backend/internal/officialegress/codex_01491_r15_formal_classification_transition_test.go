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

const codex01491R15FormalClassificationPath = "docs/egress/maintenance/codex-0.149.1-r15-formal-classification-transition.json"

type codex01491R15FormalClassificationReceipt struct {
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
		ID                    string `json:"id"`
		Mode                  string `json:"mode"`
		Purpose               string `json:"purpose"`
		TargetVersion         string `json:"target_version"`
		OfficialAttemptID     string `json:"official_attempt_id"`
		ClassificationSHA256  string `json:"classification_sha256"`
		ClassificationPackage string `json:"classification_package_sha256"`
		ProfileDigest         string `json:"profile_digest"`
		RuleCount             int    `json:"rule_count"`
		DiscoveryCount        int    `json:"discovery_count"`
		BlockedCount          int    `json:"blocked_count"`
	} `json:"campaign"`
	StageProfile struct {
		ReceiptSHA256         string `json:"receipt_sha256"`
		InventorySHA256       string `json:"inventory_sha256"`
		ActiveVersion         string `json:"active_version"`
		ActiveProfileDigest   string `json:"active_profile_digest"`
		PreviousVersion       string `json:"previous_version"`
		PreviousProfileDigest string `json:"previous_profile_digest"`
		ReleaseGraphSHA256    string `json:"release_graph_sha256"`
		ReleaseCatalogSHA256  string `json:"release_catalog_sha256"`
		ActiveUnchanged       bool   `json:"active_unchanged"`
		SelectorChanged       bool   `json:"production_selector_changed"`
	} `json:"stage_profile"`
	Verification struct {
		ClassificationReplayPassed bool `json:"classification_replay_passed"`
		StageInventoryReplayPassed bool `json:"stage_inventory_replay_passed"`
		TargetedCatalogTestsPassed bool `json:"targeted_catalog_tests_passed"`
		HistoricalGateFailureCount int  `json:"historical_gate_failure_count"`
		RepositoryWideGatesPending bool `json:"repository_wide_gates_pending"`
	} `json:"verification"`
	Safety struct {
		HistoricalReceiptsModified     bool `json:"historical_receipts_modified"`
		HistoricalArtifactsOverwritten bool `json:"historical_artifacts_overwritten"`
		NetworkConfigurationChanged    bool `json:"network_configuration_changed"`
		ProductionSelectorChanged      bool `json:"production_selector_changed"`
		DeploymentPerformed            bool `json:"deployment_performed"`
		ProductionActivated            bool `json:"production_activated"`
		VircsAccessed                  bool `json:"vircs_accessed"`
	} `json:"safety"`
	PathSetSHA256  string                                     `json:"path_set_sha256"`
	Transitions    []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Result         string                                     `json:"result"`
	IdentitySHA256 string                                     `json:"identity_sha256"`
}

var (
	codex01491R15FormalClassificationOnce   sync.Once
	codex01491R15FormalClassificationCached codex01491R15FormalClassificationReceipt
	codex01491R15FormalClassificationError  error
)

func loadCodex01491R15FormalClassificationTransition() (
	codex01491R15FormalClassificationReceipt,
	error,
) {
	codex01491R15FormalClassificationOnce.Do(func() {
		var receipt codex01491R15FormalClassificationReceipt
		raw, err := codex01491RepoFile(codex01491R15FormalClassificationPath)
		if err != nil {
			codex01491R15FormalClassificationError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&receipt); err != nil {
			codex01491R15FormalClassificationError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R15FormalClassificationError = errors.New("Codex 0.149.1 r15 正式分类 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA256); err != nil {
			codex01491R15FormalClassificationError = err
			return
		}
		codex01491R15FormalClassificationCached = receipt
		codex01491R15FormalClassificationError = validateCodex01491R15FormalClassificationTransition(receipt)
	})
	return codex01491R15FormalClassificationCached, codex01491R15FormalClassificationError
}

func validateCodex01491R15FormalClassificationTransition(
	receipt codex01491R15FormalClassificationReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r15-formal-classification-transition/v1" ||
		receipt.BaseCommit != "165666267c07bafff6a2c04077a82187e157968e" ||
		receipt.Scope != "codex-0.149.1-r15-formal-classification" ||
		receipt.FrameworkStage != "VC-3/VC-4/FORMAL-CLASSIFICATION-STAGE" ||
		receipt.Result != "formal_classification_catalog_staged_transition_frozen" {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R14ModelPrewarmPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R14ModelPrewarmPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 前序绑定非法")
	}
	if receipt.Campaign.ID != "c1491-r14-f" ||
		receipt.Campaign.Mode != "formal" ||
		receipt.Campaign.Purpose != "production_replacement" ||
		receipt.Campaign.TargetVersion != "0.149.1" ||
		receipt.Campaign.OfficialAttemptID != "20260827T105419Z-823221b63b08688e" ||
		receipt.Campaign.ClassificationSHA256 != "97a6cf745c120db22d552b466c342c46d893ce0974521983e4a74ac1cf2654bd" ||
		receipt.Campaign.ClassificationPackage != "caa5d79776bf1765352e24245db8274e6aa6aef408b6129392ea1cf32419e179" ||
		receipt.Campaign.ProfileDigest != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" ||
		receipt.Campaign.RuleCount != 42 || receipt.Campaign.DiscoveryCount != 2101 ||
		receipt.Campaign.BlockedCount != 0 {
		return errors.New("Codex 0.149.1 r15 正式分类 transition Campaign 事实非法")
	}
	if receipt.StageProfile.ReceiptSHA256 != "cc9570a4d8a21bd7a43ca0a3dd870bbd8b9f7e93286d96faf1604c0e4df7ce9a" ||
		receipt.StageProfile.InventorySHA256 != "9b8f867f0b5d97f595909bbecdfd2519754b74e5a716f09eaef4d0f97af6e798" ||
		receipt.StageProfile.ActiveVersion != "0.147.0" ||
		receipt.StageProfile.ActiveProfileDigest != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		receipt.StageProfile.PreviousVersion != "0.149.1" ||
		receipt.StageProfile.PreviousProfileDigest != "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60" ||
		receipt.StageProfile.ReleaseGraphSHA256 != "cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e" ||
		receipt.StageProfile.ReleaseCatalogSHA256 != "24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7" ||
		!receipt.StageProfile.ActiveUnchanged || receipt.StageProfile.SelectorChanged {
		return errors.New("Codex 0.149.1 r15 正式分类 transition stage-profile 事实非法")
	}
	if !receipt.Verification.ClassificationReplayPassed ||
		!receipt.Verification.StageInventoryReplayPassed ||
		!receipt.Verification.TargetedCatalogTestsPassed ||
		receipt.Verification.HistoricalGateFailureCount != 52 ||
		!receipt.Verification.RepositoryWideGatesPending {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 验证事实非法")
	}
	if receipt.Safety.HistoricalReceiptsModified ||
		receipt.Safety.HistoricalArtifactsOverwritten ||
		receipt.Safety.NetworkConfigurationChanged ||
		receipt.Safety.ProductionSelectorChanged ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.ProductionActivated ||
		receipt.Safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 安全边界非法")
	}
	if len(receipt.Transitions) < 4 {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 路径闭集为空")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	required := map[string]string{
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json":                                                                 "24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7",
		"backend/internal/officialegress/catalogdata/runtime/release-graphs/cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e.json": "cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e",
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json":                                                              "cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e",
	}
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" ||
			!slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(append([]string(nil), entry.PredecessorSHA256s...))) {
			return errors.New("Codex 0.149.1 r15 正式分类 transition 条目非法：" + entry.Path)
		}
		for _, predecessor := range entry.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 r15 正式分类 transition 前序摘要非法：" + entry.Path)
			}
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 r15 正式分类 transition 新增条目非法：" + entry.Path)
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 r15 正式分类 transition 修改条目非法：" + entry.Path)
		}
		if strings.HasPrefix(entry.Path, "docs/egress/maintenance/") &&
			entry.Path != codex01491R15FormalClassificationPath {
			return errors.New("Codex 0.149.1 r15 正式分类 transition 禁止修改历史收据：" + entry.Path)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r15 正式分类 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
		if expected, ok := required[entry.Path]; ok {
			if entry.ToSHA256 != expected {
				return errors.New("Codex 0.149.1 r15 正式分类 transition Catalog 摘要非法：" + entry.Path)
			}
			delete(required, entry.Path)
		}
	}
	if len(required) != 0 || !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 路径未排序、重复或缺项")
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r15 正式分类 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R15FormalClassificationSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R15FormalClassificationTransition()
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

func TestCodex01491R15FormalClassificationTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R15FormalClassificationTransition()
	if err != nil {
		t.Fatal(err)
	}
	if !codex01491R15FormalClassificationSupersedes(
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
		"c26b274a5942eee249ae4755aa60c399745f03212077e798fb5d582f0ce6c81e",
		"24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7",
	) {
		t.Fatal("Codex 0.149.1 r15 正式分类 transition 未承认精确 Catalog 后继边")
	}
	receipt.Safety.NetworkConfigurationChanged = true
	if err := validateCodex01491R15FormalClassificationTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r15 正式分类 transition 接受了网络配置变更")
	}
}
