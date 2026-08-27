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

const codex01491R11CModelsSyncServicePath = "docs/egress/maintenance/codex-0.149.1-r11c-models-sync-transition.json"

type codex01491R11CModelsSyncServiceReceipt struct {
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
	Boundaries         json.RawMessage                         `json:"boundaries"`
	FailureFacts       json.RawMessage                         `json:"failure_facts"`
	RepairFacts        json.RawMessage                         `json:"repair_facts"`
	OnlineVerification json.RawMessage                         `json:"online_verification"`
	Restoration        json.RawMessage                         `json:"restoration"`
	Transitions        []codex01491CandidateSourceServiceEntry `json:"transitions"`
	Verification       json.RawMessage                         `json:"verification"`
	Result             string                                  `json:"result"`
	IdentitySHA256     string                                  `json:"identity_sha256"`
}

var (
	codex01491R11CModelsSyncServiceOnce   sync.Once
	codex01491R11CModelsSyncServiceCached codex01491R11CModelsSyncServiceReceipt
	codex01491R11CModelsSyncServiceError  error
)

func codex01491R11CModelsSyncExpectedServicePaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
		"backend/internal/service/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/drive_codex_model_catalog.py",
		"tools/official_client_capture/run_official_relay_scenario.sh",
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r11b_relay_completion_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r11c_models_sync_transition.py",
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py",
	}
}

func codex01491R11CSectionDigest(raw json.RawMessage) (string, error) {
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return upstreamMergeFrameworkServiceDigest(canonical), nil
}

func loadCodex01491R11CModelsSyncServiceTransition() (
	codex01491R11CModelsSyncServiceReceipt,
	error,
) {
	codex01491R11CModelsSyncServiceOnce.Do(func() {
		codex01491R11CModelsSyncServiceCached, codex01491R11CModelsSyncServiceError =
			loadCodex01491R11CModelsSyncServiceTransitionUncached()
	})
	return codex01491R11CModelsSyncServiceCached, codex01491R11CModelsSyncServiceError
}

func loadCodex01491R11CModelsSyncServiceTransitionUncached() (
	codex01491R11CModelsSyncServiceReceipt,
	error,
) {
	var receipt codex01491R11CModelsSyncServiceReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(codex01491R11CModelsSyncServicePath),
	))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 service r11c 模型目录 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyCandidateGateServiceIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	if err := validateCodex01491R11CModelsSyncServiceTransition(receipt); err != nil {
		return receipt, err
	}
	successor, err := loadCodex01491R12EPreconnectServiceTransition()
	if err != nil {
		return receipt, err
	}
	receipt.Transitions = append(receipt.Transitions, successor.Transitions...)
	return receipt, nil
}

func validateCodex01491R11CModelsSyncServiceTransition(
	receipt codex01491R11CModelsSyncServiceReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r11c-models-sync-transition/v1" ||
		receipt.BaseCommit != "87de87becf648148e02fe44ec5d9d91afdc1a708" ||
		receipt.Scope != "codex-0.149.1-r11c-models-sync" ||
		receipt.FrameworkStage != "VC-0/P0-R11C-MODELS-SYNC" ||
		receipt.Result != "r11c_models_sync_repair_verified" {
		return errors.New("Codex 0.149.1 service r11c 模型目录 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 service r11c 模型目录 transition 时间非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(codex01491R11BRelayCompletionServicePath),
	))
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA256 string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R11BRelayCompletionServicePath ||
		receipt.PredecessorTransition.FileSHA256 != "207bc45953007446a24ddd385fd3533102a2fa60ef95e3c31ce1238a885222d6" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkServiceDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "f52ba06b86b756b6a3e4c89e043e41f1210e8c43614859af9fb8ecf39d5c49b6" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 service r11c 模型目录 transition 前序绑定非法")
	}

	expectedSections := map[string]string{
		"boundaries":          "e7282c931cbb2690a5210c2852d7ce48d6618742dc8279a7eec4fae5f9d75249",
		"failure_facts":       "32461343057ffd2daac6b283eb9e091d7976c072f7f1784303fcf1aadcc79001",
		"repair_facts":        "1ede347432d7393859537c1c490a7af31cff46298c8ce39d411e540ba27efca3",
		"online_verification": "3b09816fe8c927f960c1763e6c364808cea9a86d6e046b51e9a27b2eb0b67977",
		"restoration":         "2fa92d13b55ab6e2f373dd1e8edfa459859a7b1c202cd9f8e7032f8a7b9149a9",
		"verification":        "31be71b10aa2d3914a381839227493560849eaa8a44cc3ec1c3c2173c74226df",
	}
	sections := map[string]json.RawMessage{
		"boundaries":          receipt.Boundaries,
		"failure_facts":       receipt.FailureFacts,
		"repair_facts":        receipt.RepairFacts,
		"online_verification": receipt.OnlineVerification,
		"restoration":         receipt.Restoration,
		"verification":        receipt.Verification,
	}
	for name, raw := range sections {
		digest, digestErr := codex01491R11CSectionDigest(raw)
		if digestErr != nil || digest != expectedSections[name] {
			return errors.New("Codex 0.149.1 service r11c 模型目录 transition 事实非法：" + name)
		}
	}

	expectedPaths := codex01491R11CModelsSyncExpectedServicePaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go": {"cb7f69d38e6cdc2c6e2884b4553dc1af384e7205b762c73370cd0d88083ae56e"},
		"backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go":        {"2b58acc2459a9bc80ddc5296729eae07473520ce4ed145e6d7d768b36c6f9ccf"},
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                       {"b7ae0e37ab4bcb79182bbd2d005e52f515b21fa659211cab9ef96db24c85e4e5"},
		"tools/official_client_capture/drive_codex_model_catalog.py":                               {"d2a7e3c81054a370913da4fe369075e7196dfa1e8a8eacd6ef3ab3f03ac776f4"},
		"tools/official_client_capture/run_official_relay_scenario.sh":                             {"668e5ea4059270218f4982ece6d6e2dc2a51912418e880ad8d9abff2b0de71ae"},
		"tools/official_client_capture/tests/test_codex_01491_r11a_harness_transition.py":          {"9209136f80c163bdfa21191be8e118291b1cd6a1db2592739f8c32e2ecd19a7e"},
		"tools/official_client_capture/tests/test_codex_01491_r11b_relay_completion_transition.py": {"77d7d069aaf99d27b8d25abcf2801b3f09065abab85eabb21fbea8e55f073fb8"},
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py":                        {"cb1c7cf907883286a14e9d873b867942b9d1c7ab7593b5c9888dad2b1db07362"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 service r11c 模型目录 transition 路径数量非法")
	}
	for index, entry := range receipt.Transitions {
		expectedPath := expectedPaths[index]
		predecessors := expectedPredecessors[expectedPath]
		expectedChange := "added"
		if len(predecessors) > 0 {
			expectedChange = "modified"
		}
		if entry.Path != expectedPath || entry.Change != expectedChange ||
			!slices.Equal(entry.PredecessorSHA256s, predecessors) || len(entry.ToSHA256) != 64 ||
			strings.TrimSpace(entry.Reason) == "" {
			return errors.New("Codex 0.149.1 service r11c 模型目录 transition 条目非法：" + expectedPath)
		}
		current, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(entry.Path)))
		currentDigest := upstreamMergeFrameworkServiceDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R12EPreconnectSupersedesService(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 service r11c 模型目录 transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R11CModelsSyncSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491R15FormalClassificationSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadCodex01491R11CModelsSyncServiceTransition()
	if err != nil {
		return false
	}
	cursor := priorDigest
	if cursor == currentDigest {
		return true
	}
	for _, entry := range receipt.Transitions {
		if entry.Path == path && slices.Contains(entry.PredecessorSHA256s, cursor) {
			cursor = entry.ToSHA256
			if cursor == currentDigest {
				return true
			}
		}
	}
	return false
}

func TestCodex01491R11CModelsSyncServiceTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R11CModelsSyncServiceTransition()
	if err != nil {
		t.Fatal(err)
	}
	var boundaries map[string]any
	if err := json.Unmarshal(receipt.Boundaries, &boundaries); err != nil {
		t.Fatal(err)
	}
	boundaries["capture_account_ref"] = "#20"
	receipt.Boundaries, err = json.Marshal(boundaries)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateCodex01491R11CModelsSyncServiceTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 service r11c 模型目录 transition 接受了账号 #20")
	}
}
