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

const codex01491R22CandidateCatalogPath = "docs/egress/maintenance/codex-0.149.1-r22-candidate-catalog-transition.json"

type codex01491R22CandidateCatalogReceipt struct {
	SchemaVersion  string `json:"schema_version"`
	IssuedAtUTC    string `json:"issued_at_utc"`
	BaseCommit     string `json:"base_commit"`
	Scope          string `json:"scope"`
	FrameworkStage string `json:"framework_stage"`
	Predecessor    struct {
		Path           string `json:"path"`
		FileSHA256     string `json:"file_sha256"`
		IdentitySHA256 string `json:"identity_sha256"`
	} `json:"predecessor_transition"`
	Campaign struct {
		ID                    string `json:"id"`
		PredecessorCampaignID string `json:"predecessor_campaign_id"`
		Purpose               string `json:"purpose"`
		TargetVersion         string `json:"target_version"`
		ClassificationSHA256  string `json:"classification_sha256"`
		TargetProfileID       string `json:"target_profile_id"`
		TargetProfileDigest   string `json:"target_profile_digest"`
		ProfilePayloadSHA256  string `json:"profile_payload_sha256"`
		RequiredRuleCount     int    `json:"required_rule_count"`
		DiscoveryCount        int    `json:"discovery_count"`
		UnclassifiedCount     int    `json:"unclassified_count"`
		CodexAccountRef       string `json:"codex_account_ref"`
		APIKeyRef             string `json:"api_key_ref"`
	} `json:"campaign"`
	CatalogStage struct {
		ReceiptSchema                 string `json:"receipt_schema"`
		ReceiptSHA256                 string `json:"receipt_sha256"`
		InventorySHA256               string `json:"inventory_sha256"`
		ActiveVersion                 string `json:"active_version"`
		ActiveProfileDigest           string `json:"active_profile_digest"`
		ActiveReleaseDigest           string `json:"active_release_digest"`
		ActiveUnchanged               bool   `json:"active_unchanged"`
		ProductionSelectorChanged     bool   `json:"production_selector_changed"`
		CandidateReleaseMode          string `json:"candidate_release_mode"`
		CandidateReleaseDigest        string `json:"candidate_release_digest"`
		ReleaseGraphSHA256            string `json:"release_graph_sha256"`
		SnapshotCatalogSHA256         string `json:"snapshot_catalog_sha256"`
		RuntimeProfileFileSHA256      string `json:"runtime_profile_file_sha256"`
		ContractSnapshotCatalogSHA256 string `json:"contract_snapshot_catalog_sha256"`
	} `json:"catalog_stage"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		OfficialEvidenceReplayed   bool `json:"official_evidence_replayed"`
		ClassificationApproved     bool `json:"classification_approved"`
		AllRulesMapped             bool `json:"all_rules_mapped"`
		AllDiscoveriesMapped       bool `json:"all_discoveries_mapped"`
		CatalogInventoryVerified   bool `json:"catalog_inventory_verified"`
		HistoricalProfilesRehashed bool `json:"historical_profiles_rehashed"`
		ActiveSelectorUnchanged    bool `json:"active_selector_unchanged"`
		MutationTestsRequired      bool `json:"mutation_tests_required"`
	} `json:"verification"`
	Safety struct {
		HistoricalContentAddressedArtifactsOverwritten bool `json:"historical_content_addressed_artifacts_overwritten"`
		HistoricalProfile8E59Preserved                 bool `json:"historical_profile_8e59_preserved"`
		Historical0145ProfilesPreserved                bool `json:"historical_0_145_profiles_preserved"`
		HistoricalReceiptsModified                     bool `json:"historical_receipts_modified"`
		HistoricalTransitionsModified                  bool `json:"historical_transitions_modified"`
		NetworkConfigurationChanged                    bool `json:"network_configuration_changed"`
		DeploymentPerformed                            bool `json:"deployment_performed"`
		ProductionSelectorChanged                      bool `json:"production_selector_changed"`
		ProductionActivated                            bool `json:"production_activated"`
		OfficialRecapturePerformed                     bool `json:"official_recapture_performed"`
		CodexAccountRequestSent                        bool `json:"codex_account_request_sent"`
		VircsAccessed                                  bool `json:"vircs_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491R22CandidateCatalogOnce   sync.Once
	codex01491R22CandidateCatalogCached codex01491R22CandidateCatalogReceipt
	codex01491R22CandidateCatalogError  error
)

func codex01491R22CandidateCatalogExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/catalogdata/runtime/profiles/0.149.1/8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3.json",
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
		"backend/internal/officialegress/catalogdata/runtime/release-graphs/071362d48ff01553ba4ffb44371cf04c88f6224361090e62ee5e6ff03a619cfc.json",
		"backend/internal/officialegress/catalogdata/runtime/snapshot-catalogs/33c7840f95d32677e4189c57d8ee8d3b18fce040ed91c1a707f120e76ce6f905.json",
		"backend/internal/officialegress/codex_01491_r21_classification_fact_correction_transition_test.go",
		"backend/internal/officialegress/codex_01491_r22_candidate_catalog_transition_test.go",
		"backend/internal/officialegress/profilecontract/testdata/snapshot-catalog.json",
		"backend/internal/officialegress/profilecontract/testdata/snapshots/0.149.1/8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3.json",
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r21_classification_fact_correction_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r22_candidate_catalog_transition.py",
	}
}

func loadCodex01491R22CandidateCatalogTransition() (
	codex01491R22CandidateCatalogReceipt,
	error,
) {
	codex01491R22CandidateCatalogOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R22CandidateCatalogPath)
		if err != nil {
			codex01491R22CandidateCatalogError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R22CandidateCatalogCached); err != nil {
			codex01491R22CandidateCatalogError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R22CandidateCatalogError = errors.New("Codex 0.149.1 r22 Catalog transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R22CandidateCatalogCached.IdentitySHA256,
		); err != nil {
			codex01491R22CandidateCatalogError = err
			return
		}
		codex01491R22CandidateCatalogError =
			validateCodex01491R22CandidateCatalogTransition(
				codex01491R22CandidateCatalogCached,
			)
	})
	return codex01491R22CandidateCatalogCached, codex01491R22CandidateCatalogError
}

func validateCodex01491R22CandidateCatalogTransition(
	receipt codex01491R22CandidateCatalogReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r22-candidate-catalog-transition/v1" ||
		receipt.BaseCommit != "b37e16dd03fe92a5f0e32fd2e0291a5bcb9d3de8" ||
		receipt.Scope != "codex-0.149.1-r22-candidate-catalog" ||
		receipt.FrameworkStage != "VC-3/CANDIDATE-CATALOG" ||
		receipt.Result != "r22_candidate_catalog_staged" {
		return errors.New("Codex 0.149.1 r22 Catalog transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r22 Catalog transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R21ClassificationFactCorrectionPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		SchemaVersion  string `json:"schema_version"`
		Scope          string `json:"scope"`
		Result         string `json:"result"`
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if err := codex01491VerifyIdentity(predecessorRaw, predecessor.IdentitySHA256); err != nil {
		return err
	}
	if receipt.Predecessor.Path != codex01491R21ClassificationFactCorrectionPath ||
		receipt.Predecessor.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.Predecessor.IdentitySHA256 != predecessor.IdentitySHA256 ||
		predecessor.SchemaVersion != "official-client-codex-0.149.1-r21-classification-fact-correction-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r21-classification-fact-correction" ||
		predecessor.Result != "classification_fact_correction_tooling_frozen" {
		return errors.New("Codex 0.149.1 r22 Catalog transition 前序绑定非法")
	}

	if receipt.Campaign.ID != "c1491-r22-s8" ||
		receipt.Campaign.PredecessorCampaignID != "c1491-r20-s6" ||
		receipt.Campaign.Purpose != "production_replacement" ||
		receipt.Campaign.TargetVersion != "0.149.1" ||
		receipt.Campaign.ClassificationSHA256 != "62dd45d20c0fded5441b987fef0b913898fca98986b8705f3f42e0f83e221aca" ||
		receipt.Campaign.TargetProfileID != "codex-0.149.1-official-r1491-v2" ||
		receipt.Campaign.TargetProfileDigest != "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3" ||
		receipt.Campaign.ProfilePayloadSHA256 != "61d1db4b41ba97b9667678cb3cf219326d9ccb47c58c9ea2cae9e0e6160000d8" ||
		receipt.Campaign.RequiredRuleCount != 42 || receipt.Campaign.DiscoveryCount != 2101 ||
		receipt.Campaign.UnclassifiedCount != 0 || receipt.Campaign.CodexAccountRef != "#22" ||
		receipt.Campaign.APIKeyRef != "#4" {
		return errors.New("Codex 0.149.1 r22 Campaign 或账号身份非法")
	}

	stage := receipt.CatalogStage
	if stage.ReceiptSchema != "official-egress-catalog-stage/v1" ||
		stage.ReceiptSHA256 != "30374ad355cee7e79100172ef577ee9536dbdb861649c17ca1eb0a804443a0fb" ||
		stage.InventorySHA256 != "55d5493bd02073cbc69d4854e6821d64ad72ec5b132038858f96373dce3fb2a5" ||
		stage.ActiveVersion != "0.147.0" ||
		stage.ActiveProfileDigest != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		stage.ActiveReleaseDigest != "caa1948405136feaf159cbfdf3c164c056c1ea38cac6f87a007cfe69ead38707" ||
		!stage.ActiveUnchanged || stage.ProductionSelectorChanged ||
		stage.CandidateReleaseMode != "previous" ||
		stage.CandidateReleaseDigest != "ebf5947e7630e4b70efe48d5b52435a1a8b54c4c8675ebc134519b22a9175e2f" ||
		stage.ReleaseGraphSHA256 != "071362d48ff01553ba4ffb44371cf04c88f6224361090e62ee5e6ff03a619cfc" ||
		stage.SnapshotCatalogSHA256 != "33c7840f95d32677e4189c57d8ee8d3b18fce040ed91c1a707f120e76ce6f905" ||
		stage.RuntimeProfileFileSHA256 != "a8ec3ccf43748bd225a9bd753fb2a240d3f5667da76ccd344e370294178ec2df" ||
		stage.ContractSnapshotCatalogSHA256 != "f2a38b220a5edb0ee3c2e4f734da7a9d4d91bf80d0d5c2f7805f8dbfb5d87c37" {
		return errors.New("Codex 0.149.1 r22 stage-profile 收据事实非法")
	}

	verification := receipt.Verification
	if !verification.OfficialEvidenceReplayed || !verification.ClassificationApproved ||
		!verification.AllRulesMapped || !verification.AllDiscoveriesMapped ||
		!verification.CatalogInventoryVerified || !verification.HistoricalProfilesRehashed ||
		!verification.ActiveSelectorUnchanged || !verification.MutationTestsRequired {
		return errors.New("Codex 0.149.1 r22 Catalog 验证事实非法")
	}
	safety := receipt.Safety
	if safety.HistoricalContentAddressedArtifactsOverwritten ||
		!safety.HistoricalProfile8E59Preserved || !safety.Historical0145ProfilesPreserved ||
		safety.HistoricalReceiptsModified || safety.HistoricalTransitionsModified ||
		safety.NetworkConfigurationChanged || safety.DeploymentPerformed ||
		safety.ProductionSelectorChanged || safety.ProductionActivated ||
		safety.OfficialRecapturePerformed || safety.CodexAccountRequestSent || safety.VircsAccessed {
		return errors.New("Codex 0.149.1 r22 Catalog 安全边界非法")
	}

	expectedPaths := codex01491R22CandidateCatalogExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r22 Catalog transition 路径数量非法")
	}
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json":                              {"24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7"},
		"backend/internal/officialegress/codex_01491_r21_classification_fact_correction_transition_test.go":     {"7dd3240bb9997350445ff7b454a4e96cab2f73a41fa74611573f4aa0bce66745"},
		"backend/internal/officialegress/profilecontract/testdata/snapshot-catalog.json":                        {"4e22b13e6a3c6a4a2b7285c2b84b00c8e07415090d261e39ffbbfe457bbec239"},
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json":                           {"cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py":           {"33170592b18675c7d590cb644b15ab6e09e60d57b25960a52047f24fc018e00b"},
		"tools/official_client_capture/tests/test_codex_01491_r21_classification_fact_correction_transition.py": {"020bac623bfc717012b34d49acec89a4d26a3129a17edaa482e886a172e991dd"},
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
			return errors.New("Codex 0.149.1 r22 Catalog transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r22 Catalog transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r22 Catalog transition 路径摘要不一致")
	}
	return validateCodex01491R22CatalogSemantics()
}

func validateCodex01491R22CatalogSemantics() error {
	const profileDigest = "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3"
	const classificationSHA = "62dd45d20c0fded5441b987fef0b913898fca98986b8705f3f42e0f83e221aca"
	const releaseGraphSHA = "071362d48ff01553ba4ffb44371cf04c88f6224361090e62ee5e6ff03a619cfc"
	const snapshotCatalogSHA = "33c7840f95d32677e4189c57d8ee8d3b18fce040ed91c1a707f120e76ce6f905"

	releaseCatalogRaw, err := codex01491RepoFile("backend/internal/officialegress/catalogdata/runtime/release-catalog.json")
	if err != nil {
		return err
	}
	var releaseCatalog struct {
		SchemaVersion int `json:"schema_version"`
		ReleaseGraph  struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"release_graph"`
		SnapshotCatalog struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"snapshot_catalog"`
		Source string `json:"source"`
	}
	if err := json.Unmarshal(releaseCatalogRaw, &releaseCatalog); err != nil {
		return err
	}
	if releaseCatalog.SchemaVersion != 1 ||
		releaseCatalog.ReleaseGraph.Path != "catalogdata/runtime/release-graphs/"+releaseGraphSHA+".json" ||
		releaseCatalog.ReleaseGraph.SHA256 != releaseGraphSHA ||
		releaseCatalog.SnapshotCatalog.Path != "catalogdata/runtime/snapshot-catalogs/"+snapshotCatalogSHA+".json" ||
		releaseCatalog.SnapshotCatalog.SHA256 != snapshotCatalogSHA ||
		releaseCatalog.Source != "campaign:c1491-r22-s8/classification:"+classificationSHA {
		return errors.New("Codex 0.149.1 r22 release-catalog 身份非法")
	}

	releaseGraphRaw, err := codex01491RepoFile("backend/internal/officialegress/releasecontract/testdata/release-graph.json")
	if err != nil {
		return err
	}
	var releaseGraph struct {
		Nodes []struct {
			Mode  string `json:"mode"`
			Build struct {
				Source string `json:"source"`
			} `json:"build"`
			Snapshot struct {
				Version string `json:"version"`
				Digest  string `json:"digest"`
			} `json:"snapshot"`
		} `json:"nodes"`
	}
	if err := json.Unmarshal(releaseGraphRaw, &releaseGraph); err != nil {
		return err
	}
	activeCount := 0
	previousCount := 0
	for _, node := range releaseGraph.Nodes {
		switch node.Mode {
		case "active":
			activeCount++
			if node.Snapshot.Version != "0.147.0" {
				return errors.New("Codex 0.149.1 r22 Active 已被错误切换")
			}
		case "previous":
			previousCount++
			if node.Snapshot.Version != "0.149.1" || node.Snapshot.Digest != profileDigest ||
				node.Build.Source != "campaign:c1491-r22-s8/classification:"+classificationSHA {
				return errors.New("Codex 0.149.1 r22 Previous 未绑定 v2")
			}
		default:
			return errors.New("Codex 0.149.1 r22 ReleaseGraph mode 非法")
		}
	}
	if activeCount != 2 || previousCount != 2 {
		return errors.New("Codex 0.149.1 r22 ReleaseGraph 节点闭集非法")
	}

	snapshotRaw, err := codex01491RepoFile("backend/internal/officialegress/profilecontract/testdata/snapshot-catalog.json")
	if err != nil {
		return err
	}
	var snapshotCatalog struct {
		Snapshots []struct {
			Version string `json:"version"`
			Digest  string `json:"digest"`
		} `json:"snapshots"`
	}
	if err := json.Unmarshal(snapshotRaw, &snapshotCatalog); err != nil {
		return err
	}
	identities := make(map[string]bool)
	for _, snapshot := range snapshotCatalog.Snapshots {
		identities[snapshot.Version+":"+snapshot.Digest] = true
	}
	required := []string{
		"0.145.0:343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431",
		"0.145.0:e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b",
		"0.147.0:94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc",
		"0.149.1:" + profileDigest,
		"0.149.1:8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60",
	}
	if len(identities) != len(required) {
		return errors.New("Codex 0.149.1 r22 SnapshotCatalog 数量非法")
	}
	for _, identity := range required {
		if !identities[identity] {
			return errors.New("Codex 0.149.1 r22 SnapshotCatalog 缺少历史或 v2 画像")
		}
	}

	historical := map[string]string{
		"backend/internal/officialegress/catalogdata/runtime/profiles/0.149.1/8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60.json": "39e29520a4f10dc55c14f1b259c09d0058f3444d56824e5988850e4660e9123a",
		"backend/internal/officialegress/catalogdata/runtime/profiles/0.145.0/343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431.json": "ff503d73d7402fb0d6429a67b415aa7d2bc863a3002cf5e5ee9ab3baa3d7529f",
		"backend/internal/officialegress/catalogdata/runtime/profiles/0.145.0/e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b.json": "36c6c0e4464e6182347210d05d17ea85f6121e98f70f3c36b6ffc2b4230a5c66",
	}
	for path, expected := range historical {
		raw, readErr := codex01491RepoFile(path)
		if readErr != nil || upstreamMergeFrameworkDigest(raw) != expected {
			return errors.New("Codex 0.149.1 r22 历史画像发生漂移：" + path)
		}
	}
	return nil
}

func codex01491R22CandidateCatalogSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491R22CandidateCatalogTransition()
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

func TestCodex01491R22CandidateCatalogTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R22CandidateCatalogTransition()
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
	if modified.Path == "" || !codex01491R22CandidateCatalogSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) || codex01491R22CandidateCatalogSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r22 Catalog 精确三元组判据非法")
	}
	receipt.CatalogStage.ActiveUnchanged = false
	if err := validateCodex01491R22CandidateCatalogTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r22 Catalog transition 接受了 Active 切换")
	}
}
