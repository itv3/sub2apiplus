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

const codex01491R23RuntimeCoordinatePath = "docs/egress/maintenance/codex-0.149.1-r23-runtime-coordinate-transition.json"

type codex01491R23RuntimeCoordinateReceipt struct {
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
	RuntimeContract struct {
		ReceiptSchema                      string   `json:"receipt_schema"`
		Reason                             string   `json:"reason"`
		ConfigurationFields                []string `json:"configuration_fields"`
		ComposeCoordinatesRequiredTogether bool     `json:"compose_coordinates_required_together"`
		ComposeDirectoryMustBeCanonical    bool     `json:"compose_directory_must_be_canonical"`
		ComposeFilesMustExistAndBeRegular  bool     `json:"compose_files_must_exist_and_be_regular"`
		UnlistedConfigurationChangesDenied bool     `json:"unlisted_configuration_changes_denied"`
		OfficialRecaptureRequired          bool     `json:"official_recapture_required"`
		ClassificationReapprovalRequired   bool     `json:"classification_reapproval_required"`
	} `json:"runtime_contract"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  struct {
		LegacyV1V2V3ReceiptsReplayed      bool `json:"legacy_v1_v2_v3_receipts_replayed"`
		V4PositivePathReplayed            bool `json:"v4_positive_path_replayed"`
		PartialCoordinatesRejected        bool `json:"partial_coordinates_rejected"`
		ReclassificationRebindingRejected bool `json:"reclassification_rebinding_rejected"`
		MutationTestsRequired             bool `json:"mutation_tests_required"`
	} `json:"verification"`
	Safety struct {
		HistoricalCampaignsModified       bool `json:"historical_campaigns_modified"`
		HistoricalComposeFilesOverwritten bool `json:"historical_compose_files_overwritten"`
		OfficialRecapturePerformed        bool `json:"official_recapture_performed"`
		CandidateCapturePerformed         bool `json:"candidate_capture_performed"`
		DeploymentPerformed               bool `json:"deployment_performed"`
		NetworkConfigurationChanged       bool `json:"network_configuration_changed"`
		ProductionSelectorChanged         bool `json:"production_selector_changed"`
		ProductionActivated               bool `json:"production_activated"`
		VircsAccessed                     bool `json:"vircs_accessed"`
		DMITServerAccessed                bool `json:"dmit_server_accessed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	codex01491R23RuntimeCoordinateOnce   sync.Once
	codex01491R23RuntimeCoordinateCached codex01491R23RuntimeCoordinateReceipt
	codex01491R23RuntimeCoordinateError  error
)

func codex01491R23RuntimeCoordinateExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r22_candidate_catalog_transition_test.go",
		"backend/internal/officialegress/codex_01491_r23_runtime_coordinate_transition_test.go",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r22_candidate_catalog_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r23_runtime_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade.py",
	}
}

func loadCodex01491R23RuntimeCoordinateTransition() (
	codex01491R23RuntimeCoordinateReceipt,
	error,
) {
	codex01491R23RuntimeCoordinateOnce.Do(func() {
		raw, err := codex01491RepoFile(codex01491R23RuntimeCoordinatePath)
		if err != nil {
			codex01491R23RuntimeCoordinateError = err
			return
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&codex01491R23RuntimeCoordinateCached); err != nil {
			codex01491R23RuntimeCoordinateError = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex01491R23RuntimeCoordinateError = errors.New("Codex 0.149.1 r23 运行时坐标 transition 尾部存在额外 JSON")
			return
		}
		if err := codex01491VerifyIdentity(
			raw,
			codex01491R23RuntimeCoordinateCached.IdentitySHA256,
		); err != nil {
			codex01491R23RuntimeCoordinateError = err
			return
		}
		codex01491R23RuntimeCoordinateError =
			validateCodex01491R23RuntimeCoordinateTransition(
				codex01491R23RuntimeCoordinateCached,
			)
	})
	return codex01491R23RuntimeCoordinateCached, codex01491R23RuntimeCoordinateError
}

func validateCodex01491R23RuntimeCoordinateTransition(
	receipt codex01491R23RuntimeCoordinateReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r23-runtime-coordinate-transition/v1" ||
		receipt.BaseCommit != "f7c3521bc80e350ae529496e9f0656079cdad78c" ||
		receipt.Scope != "codex-0.149.1-r23-runtime-coordinate" ||
		receipt.FrameworkStage != "VC-0/RUNTIME-COORDINATE-REBINDING" ||
		receipt.Result != "runtime_coordinate_successor_tooling_frozen" {
		return errors.New("Codex 0.149.1 r23 运行时坐标 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r23 运行时坐标 transition 时间非法")
	}

	predecessorRaw, err := codex01491RepoFile(codex01491R22CandidateCatalogPath)
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
	if receipt.Predecessor.Path != codex01491R22CandidateCatalogPath ||
		receipt.Predecessor.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.Predecessor.IdentitySHA256 != predecessor.IdentitySHA256 ||
		predecessor.SchemaVersion != "official-client-codex-0.149.1-r22-candidate-catalog-transition/v1" ||
		predecessor.Scope != "codex-0.149.1-r22-candidate-catalog" ||
		predecessor.Result != "r22_candidate_catalog_staged" {
		return errors.New("Codex 0.149.1 r23 运行时坐标 transition 前序绑定非法")
	}

	contract := receipt.RuntimeContract
	if contract.ReceiptSchema != "codex-upgrade-predecessor-import/v4" ||
		contract.Reason != "candidate_runtime_identity_correction" ||
		!slices.Equal(contract.ConfigurationFields, []string{
			"codex_account_id",
			"live_attestation_compose_dir",
			"live_attestation_compose_files",
		}) || !contract.ComposeCoordinatesRequiredTogether ||
		!contract.ComposeDirectoryMustBeCanonical ||
		!contract.ComposeFilesMustExistAndBeRegular ||
		!contract.UnlistedConfigurationChangesDenied ||
		contract.OfficialRecaptureRequired || contract.ClassificationReapprovalRequired {
		return errors.New("Codex 0.149.1 r23 运行时坐标合同非法")
	}
	verification := receipt.Verification
	if !verification.LegacyV1V2V3ReceiptsReplayed || !verification.V4PositivePathReplayed ||
		!verification.PartialCoordinatesRejected ||
		!verification.ReclassificationRebindingRejected || !verification.MutationTestsRequired {
		return errors.New("Codex 0.149.1 r23 运行时坐标验证事实非法")
	}
	safety := receipt.Safety
	if safety.HistoricalCampaignsModified || safety.HistoricalComposeFilesOverwritten ||
		safety.OfficialRecapturePerformed || safety.CandidateCapturePerformed ||
		safety.DeploymentPerformed || safety.NetworkConfigurationChanged ||
		safety.ProductionSelectorChanged || safety.ProductionActivated ||
		safety.VircsAccessed || safety.DMITServerAccessed {
		return errors.New("Codex 0.149.1 r23 运行时坐标安全边界非法")
	}

	expectedPaths := codex01491R23RuntimeCoordinateExpectedPaths()
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r23 运行时坐标 transition 路径数量非法")
	}
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r22_candidate_catalog_transition_test.go":        {"5de84b918bc1091e4e139904c50fb3aea52ffb89e39e8a72255a5c33f46e81ce"},
		"tools/official_client_capture/codex_upgrade.py":                                              {"70ebfc64d69b004b731bd23c783454f70cb4645d57d6e9031e8a804711f90cf7"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"e402b05692bbfc2bed56ebd5b7ba75a3c211c130ad68795c438cbed0f2c56b53"},
		"tools/official_client_capture/tests/test_codex_01491_r22_candidate_catalog_transition.py":    {"f4dca65be19d26bb75f227d769c652c9e049b9fb23ea80666f541eee83f77641"},
		"tools/official_client_capture/tests/test_codex_upgrade.py":                                   {"91457388e664c7269ae1bc086aafc635471bbe113665459695ea4f98eecc996b"},
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
			!slices.Equal(entry.PredecessorSHA256s, expectedFrom) ||
			len(entry.ToSHA256) != 64 || strings.TrimSpace(entry.Reason) == "" {
			return errors.New("Codex 0.149.1 r23 运行时坐标 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r23 运行时坐标 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r23 运行时坐标 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R23RuntimeCoordinateSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491R23RuntimeCoordinateTransition()
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

func TestCodex01491R23RuntimeCoordinateTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R23RuntimeCoordinateTransition()
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
	if modified.Path == "" || !codex01491R23RuntimeCoordinateSupersedes(
		modified.Path,
		modified.PredecessorSHA256s[0],
		modified.ToSHA256,
	) || codex01491R23RuntimeCoordinateSupersedes(
		modified.Path,
		strings.Repeat("0", 64),
		modified.ToSHA256,
	) {
		t.Fatal("Codex 0.149.1 r23 运行时坐标精确三元组判据非法")
	}
	receipt.RuntimeContract.UnlistedConfigurationChangesDenied = false
	if err := validateCodex01491R23RuntimeCoordinateTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r23 运行时坐标 transition 接受了配置范围扩大")
	}
}
