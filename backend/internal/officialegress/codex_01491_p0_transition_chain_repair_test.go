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
	"time"
)

const (
	codex01491DocPreTransitionPath = "docs/egress/maintenance/codex-0.149.1-doc-pre-tooling-transition.json"
	codex01491P0ChainRepairPath    = "docs/egress/maintenance/codex-0.149.1-p0-transition-chain-repair.json"
)

var codex01491DocPreExpectedPaths = []string{
	".gitignore",
	"Makefile",
	"docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
	"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
	"tools/check_spec_refs.py",
	"tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json",
	"tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
	"tools/official_client_capture/capturelib/model.py",
	"tools/official_client_capture/codex_upgrade.py",
	"tools/official_client_capture/codex_upgrade_campaign.schema.json",
	"tools/official_client_capture/codex_upgrade_rules_0_147_0.json",
	"tools/official_client_capture/codex_upgrade_rules_0_149_1.json",
	"tools/official_client_capture/codex_upgrade_scenarios_0_147_0.json",
	"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
	"tools/official_client_capture/extract_compaction_reason.py",
	"tools/official_client_capture/h1_wire_probe.py",
	"tools/official_client_capture/run_candidate_aux_capture.sh",
	"tools/official_client_capture/run_candidate_core_capture.sh",
	"tools/official_client_capture/scrub_raw_bytes.py",
	"tools/official_client_capture/tests/test_candidate_aux_capture.py",
	"tools/official_client_capture/tests/test_candidate_rule_assertion.py",
	"tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
	"tools/official_client_capture/tests/test_codex_upgrade.py",
	"tools/official_client_capture/tests/test_main_track_models.py",
	"tools/official_client_capture/tests/test_scenario_receipt.py",
	"tools/official_client_capture/tests/test_upstream_byte_relay.py",
	"tools/spec_ref_anchors_0_149_1.json",
	"tools/spec_source_deps/README.md",
	"tools/spec_source_deps/h2-0.4.16/Cargo.toml",
	"tools/spec_source_deps/h2-0.4.16/LICENSE",
	"tools/spec_source_deps/h2-0.4.16/src/frame/headers.rs",
	"tools/spec_source_deps/h2-0.4.16/src/frame/settings.rs",
	"tools/spec_source_deps/h2-0.4.16/src/hpack/encoder.rs",
	"tools/spec_source_deps/manifest_0_149_1.json",
	"tools/update_spec_ref_anchors.py",
}

var codex01491P0ChainRepairExpectedPaths = []string{
	"backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
	"backend/internal/officialegress/upstream_merge_framework_transition_test.go",
	"backend/internal/officialegress/upstream_v0180_source_transition_test.go",
}

type codex01491DocPreTransitionEntry struct {
	Path       string  `json:"path"`
	Change     string  `json:"change"`
	FromSHA256 *string `json:"from_sha256"`
	ToSHA256   string  `json:"to_sha256"`
	Reason     string  `json:"reason"`
}

type codex01491DocPreTransitionReceipt struct {
	SchemaVersion  string `json:"schema_version"`
	IssuedAtUTC    string `json:"issued_at_utc"`
	BaseCommit     string `json:"base_commit"`
	Scope          string `json:"scope"`
	FrameworkStage string `json:"framework_stage"`
	Baseline       struct {
		Sub2APIVersion     string `json:"sub2api_version"`
		ProductionActive   string `json:"production_active"`
		ProductionPrevious string `json:"production_previous"`
	} `json:"baseline"`
	Target struct {
		CodexVersion          string `json:"codex_version"`
		FormalCampaignCreated bool   `json:"formal_campaign_created"`
		CampaignPurpose       string `json:"campaign_purpose"`
	} `json:"target"`
	PrivateArchive             json.RawMessage                   `json:"private_archive"`
	HistoricalReadOnlyBindings json.RawMessage                   `json:"historical_read_only_bindings"`
	Transitions                []codex01491DocPreTransitionEntry `json:"transitions"`
	Verification               json.RawMessage                   `json:"verification"`
	Safety                     struct {
		ActivePreviousChanged              bool `json:"active_previous_changed"`
		CatalogPromoted                    bool `json:"catalog_promoted"`
		DeploymentPerformed                bool `json:"deployment_performed"`
		FormalCampaignCreated              bool `json:"formal_campaign_created"`
		HistoricalArtifactsModified        bool `json:"historical_artifacts_modified"`
		LiveRequestSent                    bool `json:"live_request_sent"`
		ProductionActivationReceiptCreated bool `json:"production_activation_receipt_created"`
		ProductionSelectorChanged          bool `json:"production_selector_changed"`
		ServerRequiredForThisTransition    bool `json:"server_required_for_this_transition"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

type codex01491P0ChainRepairEntry struct {
	Path               string   `json:"path"`
	Change             string   `json:"change"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type codex01491P0ChainRepairReceipt struct {
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
	Transitions  []codex01491P0ChainRepairEntry `json:"transitions"`
	Verification struct {
		DocPreTransitionReplayed  bool `json:"doc_pre_transition_replayed"`
		MutationTestsPassed       bool `json:"mutation_tests_passed"`
		OfficialEgressTestsPassed bool `json:"officialegress_tests_passed"`
		P0CaptureToolTestsPassed  bool `json:"p0_capture_tool_tests_passed"`
		P0EgressSpecPassed        bool `json:"p0_egress_spec_passed"`
	} `json:"verification"`
	Safety struct {
		ActivePreviousChanged      bool `json:"active_previous_changed"`
		CatalogPromoted            bool `json:"catalog_promoted"`
		DeploymentPerformed        bool `json:"deployment_performed"`
		FormalCampaignCreated      bool `json:"formal_campaign_created"`
		HistoricalReceiptsModified bool `json:"historical_receipts_modified"`
		LiveRequestSent            bool `json:"live_request_sent"`
		ProductionSelectorChanged  bool `json:"production_selector_changed"`
		ServerAccessed             bool `json:"server_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491DocPreOnce      sync.Once
	codex01491DocPreCached    codex01491DocPreTransitionReceipt
	codex01491DocPreLoadErr   error
	codex01491P0RepairOnce    sync.Once
	codex01491P0RepairCached  codex01491P0ChainRepairReceipt
	codex01491P0RepairLoadErr error
)

func codex01491RepoFile(path string) ([]byte, error) {
	return os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
}

func codex01491VerifyIdentity(raw []byte, expected string) error {
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil {
		return err
	}
	canonical = append(canonical, '\n')
	if upstreamMergeFrameworkDigest(canonical) != expected {
		return errors.New("Codex 0.149.1 transition 自摘要不一致")
	}
	return nil
}

func decodeCodex01491Transition(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("Codex 0.149.1 transition 尾部存在额外 JSON")
	}
	return nil
}

func loadCodex01491DocPreTransition() (codex01491DocPreTransitionReceipt, error) {
	codex01491DocPreOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491DocPreTransitionPath)
		if err != nil {
			codex01491DocPreLoadErr = err
			return
		}
		if err := decodeCodex01491Transition(raw, &codex01491DocPreCached); err != nil {
			codex01491DocPreLoadErr = err
			return
		}
		if err := codex01491VerifyIdentity(raw, codex01491DocPreCached.IdentitySHA256); err != nil {
			codex01491DocPreLoadErr = err
			return
		}
		codex01491DocPreLoadErr = validateCodex01491DocPreTransition(codex01491DocPreCached)
	})
	return codex01491DocPreCached, codex01491DocPreLoadErr
}

func validateCodex01491DocPreTransition(receipt codex01491DocPreTransitionReceipt) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-doc-pre-tooling-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-25T03:58:15Z" ||
		receipt.BaseCommit != "3d6082f44f289ec80e0e29eb2643cda78113eef0" ||
		receipt.Scope != "codex-0.149.1-doc-pre-tooling" ||
		receipt.FrameworkStage != "VC-0/DOC-PRE" ||
		receipt.Baseline.Sub2APIVersion != "0.1.180" ||
		receipt.Baseline.ProductionActive != "0.147.0" ||
		receipt.Baseline.ProductionPrevious != "0.145.0" ||
		receipt.Target.CodexVersion != "0.149.1" || receipt.Target.FormalCampaignCreated ||
		receipt.Target.CampaignPurpose != "unassigned_until_formal_campaign" ||
		len(receipt.PrivateArchive) == 0 || len(receipt.HistoricalReadOnlyBindings) == 0 ||
		len(receipt.Verification) == 0 || receipt.Result != "ready_for_clean_head_p0" {
		return errors.New("Codex 0.149.1 DOC-PRE transition 顶层事实非法")
	}
	if receipt.Safety.ActivePreviousChanged || receipt.Safety.CatalogPromoted ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.FormalCampaignCreated ||
		receipt.Safety.HistoricalArtifactsModified || receipt.Safety.LiveRequestSent ||
		receipt.Safety.ProductionActivationReceiptCreated ||
		receipt.Safety.ProductionSelectorChanged || receipt.Safety.ServerRequiredForThisTransition {
		return errors.New("Codex 0.149.1 DOC-PRE transition 安全边界非法")
	}
	return validateCodex01491DocPreEntries(receipt.Transitions)
}

func validateCodex01491DocPreEntries(entries []codex01491DocPreTransitionEntry) error {
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		if strings.TrimSpace(entry.Path) == "" || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" {
			return errors.New("Codex 0.149.1 DOC-PRE transition 条目非法")
		}
		if entry.Change == "added" {
			if entry.FromSHA256 != nil {
				return errors.New("Codex 0.149.1 DOC-PRE 新增条目存在前序摘要")
			}
		} else if entry.Change != "modified" || entry.FromSHA256 == nil ||
			len(*entry.FromSHA256) != 64 || *entry.FromSHA256 == entry.ToSHA256 {
			return errors.New("Codex 0.149.1 DOC-PRE 修改条目前序摘要非法")
		}
		for _, prefix := range []string{
			"backend/internal/officialegress/catalogdata/",
			"backend/internal/officialegress/profilecontract/testdata/",
			"backend/internal/officialegress/releasecontract/testdata/",
			"docs/egress/lifecycle/migration-artifacts/",
		} {
			if strings.HasPrefix(entry.Path, prefix) {
				return errors.New("Codex 0.149.1 DOC-PRE transition 命中历史只读路径")
			}
		}
		current, err := codex01491RepoFile(entry.Path)
		if err != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 DOC-PRE transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	if !slices.Equal(paths, codex01491DocPreExpectedPaths) {
		return errors.New("Codex 0.149.1 DOC-PRE transition 路径闭集非法")
	}
	return nil
}

func loadCodex01491P0ChainRepair() (codex01491P0ChainRepairReceipt, error) {
	codex01491P0RepairOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491P0ChainRepairPath)
		if err != nil {
			codex01491P0RepairLoadErr = err
			return
		}
		if err := decodeCodex01491Transition(raw, &codex01491P0RepairCached); err != nil {
			codex01491P0RepairLoadErr = err
			return
		}
		if err := codex01491VerifyIdentity(raw, codex01491P0RepairCached.IdentitySHA256); err != nil {
			codex01491P0RepairLoadErr = err
			return
		}
		codex01491P0RepairLoadErr = validateCodex01491P0ChainRepair(codex01491P0RepairCached)
	})
	return codex01491P0RepairCached, codex01491P0RepairLoadErr
}

func validateCodex01491P0ChainRepair(receipt codex01491P0ChainRepairReceipt) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-p0-transition-chain-repair/v1" ||
		receipt.BaseCommit != "75a4470e7fd6a2e77556d481ea879c77d441529a" ||
		receipt.Scope != "codex-0.149.1-p0-transition-chain-repair" ||
		receipt.FrameworkStage != "VC-0/P0" ||
		receipt.Result != "p0_transition_chain_repaired" {
		return errors.New("Codex 0.149.1 P0 transition 链修复顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 P0 transition 链修复时间非法")
	}
	docReceipt, err := loadCodex01491DocPreTransition()
	if err != nil {
		return err
	}
	docRaw, err := codex01491RepoFile(codex01491DocPreTransitionPath)
	if err != nil || receipt.PredecessorTransition.Path != codex01491DocPreTransitionPath ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(docRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != docReceipt.IdentitySHA256 {
		return errors.New("Codex 0.149.1 P0 transition 链修复前序绑定非法")
	}
	if !receipt.Verification.DocPreTransitionReplayed || !receipt.Verification.MutationTestsPassed ||
		!receipt.Verification.OfficialEgressTestsPassed || !receipt.Verification.P0CaptureToolTestsPassed ||
		!receipt.Verification.P0EgressSpecPassed {
		return errors.New("Codex 0.149.1 P0 transition 链修复门禁未闭合")
	}
	if receipt.Safety.ActivePreviousChanged || receipt.Safety.CatalogPromoted ||
		receipt.Safety.DeploymentPerformed || receipt.Safety.FormalCampaignCreated ||
		receipt.Safety.HistoricalReceiptsModified || receipt.Safety.LiveRequestSent ||
		receipt.Safety.ProductionSelectorChanged || receipt.Safety.ServerAccessed {
		return errors.New("Codex 0.149.1 P0 transition 链修复安全边界非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, entry := range receipt.Transitions {
		if strings.TrimSpace(entry.Path) == "" || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" || !slices.IsSorted(entry.PredecessorSHA256s) ||
			len(entry.PredecessorSHA256s) != len(slices.Compact(append([]string(nil), entry.PredecessorSHA256s...))) {
			return errors.New("Codex 0.149.1 P0 transition 链修复条目非法")
		}
		if entry.Change == "added" {
			if len(entry.PredecessorSHA256s) != 0 {
				return errors.New("Codex 0.149.1 P0 transition 链修复新增条目存在前序摘要")
			}
		} else if entry.Change != "modified" || len(entry.PredecessorSHA256s) == 0 {
			return errors.New("Codex 0.149.1 P0 transition 链修复修改条目缺少前序摘要")
		}
		for _, predecessor := range entry.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == entry.ToSHA256 {
				return errors.New("Codex 0.149.1 P0 transition 链修复前序摘要非法")
			}
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 P0 transition 链修复当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	if !slices.Equal(paths, codex01491P0ChainRepairExpectedPaths) {
		return errors.New("Codex 0.149.1 P0 transition 链修复路径闭集非法")
	}
	return nil
}

func codex01491DocPreTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491DocPreTransition()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && entry.FromSHA256 != nil &&
			*entry.FromSHA256 == priorDigest && entry.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func codex01491P0ChainRepairSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491P0ChainRepair()
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

func codex01491MaintenanceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	return codex01491DocPreTransitionSupersedes(path, priorDigest, currentDigest) ||
		codex01491P0ChainRepairSupersedes(path, priorDigest, currentDigest)
}

func TestCodex01491MaintenanceTransitionsAreFrozen(t *testing.T) {
	if _, err := loadCodex01491DocPreTransition(); err != nil {
		t.Fatal(err)
	}
	if _, err := loadCodex01491P0ChainRepair(); err != nil {
		t.Fatal(err)
	}
}

func TestCodex01491MaintenanceTransitionRejectsMutations(t *testing.T) {
	docReceipt, err := loadCodex01491DocPreTransition()
	if err != nil {
		t.Fatal(err)
	}
	docEntry := docReceipt.Transitions[0]
	if docEntry.FromSHA256 == nil || !codex01491MaintenanceTransitionSupersedes(
		docEntry.Path, *docEntry.FromSHA256, docEntry.ToSHA256,
	) {
		t.Fatal("DOC-PRE transition 的精确三元组未被承认")
	}
	if codex01491MaintenanceTransitionSupersedes(
		docEntry.Path+"-wrong", *docEntry.FromSHA256, docEntry.ToSHA256,
	) || codex01491MaintenanceTransitionSupersedes(
		docEntry.Path, strings.Repeat("0", 64), docEntry.ToSHA256,
	) || codex01491MaintenanceTransitionSupersedes(
		docEntry.Path, *docEntry.FromSHA256, strings.Repeat("f", 64),
	) {
		t.Fatal("DOC-PRE transition 接受了篡改三元组")
	}

	repairReceipt, err := loadCodex01491P0ChainRepair()
	if err != nil {
		t.Fatal(err)
	}
	repairEntry := repairReceipt.Transitions[1]
	if len(repairEntry.PredecessorSHA256s) == 0 || !codex01491MaintenanceTransitionSupersedes(
		repairEntry.Path, repairEntry.PredecessorSHA256s[0], repairEntry.ToSHA256,
	) {
		t.Fatal("P0 链修复 transition 的精确三元组未被承认")
	}
	if codex01491MaintenanceTransitionSupersedes(
		repairEntry.Path, strings.Repeat("0", 64), repairEntry.ToSHA256,
	) {
		t.Fatal("P0 链修复 transition 接受了未知前序摘要")
	}

	mutated := repairReceipt
	mutated.Transitions = append([]codex01491P0ChainRepairEntry(nil), repairReceipt.Transitions...)
	mutated.Transitions[0].ToSHA256 = strings.Repeat("0", 64)
	if validateCodex01491P0ChainRepair(mutated) == nil {
		t.Fatal("P0 链修复 transition 未拒绝当前摘要 mutation")
	}
	mutated = repairReceipt
	mutated.Safety.CatalogPromoted = true
	if validateCodex01491P0ChainRepair(mutated) == nil {
		t.Fatal("P0 链修复 transition 未拒绝 Catalog promotion mutation")
	}
}
