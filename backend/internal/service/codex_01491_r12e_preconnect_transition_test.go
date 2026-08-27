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

const codex01491R12EPreconnectServicePath = "docs/egress/maintenance/codex-0.149.1-r12e-preconnect-transition.json"

type codex01491R12EPreconnectServiceReceipt struct {
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
	FailedValidation   json.RawMessage                         `json:"failed_validation"`
	Diagnosis          json.RawMessage                         `json:"diagnosis"`
	RepairFacts        json.RawMessage                         `json:"repair_facts"`
	OnlineVerification json.RawMessage                         `json:"online_verification"`
	Restoration        json.RawMessage                         `json:"restoration"`
	Timing             json.RawMessage                         `json:"timing"`
	Transitions        []codex01491CandidateSourceServiceEntry `json:"transitions"`
	Verification       json.RawMessage                         `json:"verification"`
	Result             string                                  `json:"result"`
	IdentitySHA256     string                                  `json:"identity_sha256"`
}

var (
	codex01491R12EPreconnectServiceOnce   sync.Once
	codex01491R12EPreconnectServiceCached codex01491R12EPreconnectServiceReceipt
	codex01491R12EPreconnectServiceError  error
)

func codex01491R12EPreconnectExpectedServicePaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/officialegress/codex_01491_r12e_preconnect_transition_test.go",
		"backend/internal/service/codex_01491_r11c_models_sync_transition_test.go",
		"backend/internal/service/codex_01491_r12e_preconnect_transition_test.go",
		"tools/official_client_capture/run_official_relay_scenario.sh",
		"tools/official_client_capture/tests/test_codex_01491_r11c_models_sync_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r12e_preconnect_transition.py",
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py",
		"tools/official_client_capture/tests/test_upstream_byte_relay.py",
		"tools/official_client_capture/upstream_byte_relay.py",
	}
}

func codex01491R12EPreconnectServiceSectionDigest(raw json.RawMessage) (string, error) {
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

func loadCodex01491R12EPreconnectServiceTransition() (
	codex01491R12EPreconnectServiceReceipt,
	error,
) {
	codex01491R12EPreconnectServiceOnce.Do(func() {
		codex01491R12EPreconnectServiceCached, codex01491R12EPreconnectServiceError =
			loadCodex01491R12EPreconnectServiceTransitionUncached()
	})
	return codex01491R12EPreconnectServiceCached, codex01491R12EPreconnectServiceError
}

func loadCodex01491R12EPreconnectServiceTransitionUncached() (
	codex01491R12EPreconnectServiceReceipt,
	error,
) {
	var receipt codex01491R12EPreconnectServiceReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(codex01491R12EPreconnectServicePath),
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
		return receipt, errors.New("Codex 0.149.1 service r12e 预连接 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyCandidateGateServiceIdentity(raw, receipt.IdentitySHA256); err != nil {
		return receipt, err
	}
	if err := validateCodex01491R12EPreconnectServiceTransition(receipt); err != nil {
		return receipt, err
	}
	successor, err := loadCodex01491R13CandidateCoordinateServiceTransition()
	if err != nil {
		return receipt, err
	}
	receipt.Transitions = append(receipt.Transitions, successor.Transitions...)
	return receipt, nil
}

func validateCodex01491R12EPreconnectServiceTransition(
	receipt codex01491R12EPreconnectServiceReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r12e-preconnect-transition/v1" ||
		receipt.BaseCommit != "48f851d415442b59d53d507e7a8a23218572439a" ||
		receipt.Scope != "codex-0.149.1-r12e-preconnect" ||
		receipt.FrameworkStage != "VC-0/P0-R12E-PRECONNECT" ||
		receipt.Result != "r12e_preconnect_repair_verified_new_campaign_required" {
		return errors.New("Codex 0.149.1 service r12e 预连接 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 service r12e 预连接 transition 时间非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(codex01491R11CModelsSyncServicePath),
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
	if receipt.PredecessorTransition.Path != codex01491R11CModelsSyncServicePath ||
		receipt.PredecessorTransition.FileSHA256 != "ce4f55368f4821278971ffd23321ef2ef1dac8a95e09b27a6db6cc1158f3ca5f" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkServiceDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "916041cf4e953b21a44a693b5941681046265dd849294daecb6801a3ea6e0245" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA256 {
		return errors.New("Codex 0.149.1 service r12e 预连接 transition 前序绑定非法")
	}

	expectedSections := map[string]string{
		"boundaries":          "e94b213a76a174b6c3b7a8ae955c6c3980ecd493896db06c741486dce6e34f26",
		"failed_validation":   "77a1b0a37c957922a92049b7f863eb40c6005490915fc288727fb2a41886260f",
		"diagnosis":           "1784e1f53d3b77ef8a0e727b6e4ebb972a9e55683aca0541802a74bff50b5754",
		"repair_facts":        "a343e6fe1cf237328f1fc8acf58766b7ed9d6901adbddefaa524a3625fc228ea",
		"online_verification": "e461f7b90f305f6d91fda216f4ecf809aa2c0c1b6e7f51525043fa89cdf91bb7",
		"restoration":         "fa99fafad5e09b7ec7170b826957ad4090188c2246f34b99ee609a1b2bbd6693",
		"timing":              "f8da34fc7fb6e36319841505df3ce1afa01bf7c0e485830a0c03dfc573aa6f3d",
		"verification":        "95a6136acc434ded61f60dfd932d93b10a6f2ef8800680045f3509abe234229f",
	}
	sections := map[string]json.RawMessage{
		"boundaries":          receipt.Boundaries,
		"failed_validation":   receipt.FailedValidation,
		"diagnosis":           receipt.Diagnosis,
		"repair_facts":        receipt.RepairFacts,
		"online_verification": receipt.OnlineVerification,
		"restoration":         receipt.Restoration,
		"timing":              receipt.Timing,
		"verification":        receipt.Verification,
	}
	for name, raw := range sections {
		digest, digestErr := codex01491R12EPreconnectServiceSectionDigest(raw)
		if digestErr != nil || digest != expectedSections[name] {
			return errors.New("Codex 0.149.1 service r12e 预连接 transition 事实非法：" + name)
		}
	}

	expectedPaths := codex01491R12EPreconnectExpectedServicePaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r11c_models_sync_transition_test.go":     {"fa230dae124a282236f7af33de820c8b7a9d2524ab5ed85e03edf2f874b8a684"},
		"backend/internal/service/codex_01491_r11c_models_sync_transition_test.go":            {"f0d94437a73fa4ad59e639bb270b06d60d9aef1e8d6cf514b3bcfd950c2ffc49"},
		"tools/official_client_capture/run_official_relay_scenario.sh":                        {"b775367d4f6ea02f2cc577d290c4e495f8334c42e7e0a7d3e79b379c7645b77c"},
		"tools/official_client_capture/tests/test_codex_01491_r11c_models_sync_transition.py": {"202416a36f9c6344c2645a06063cfdea462b16a32112f688d9c5d03830978d38"},
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py":                   {"aed3bdfc614ea6e68499ee64929835682c8c67b4167b055c3041dcf91ed51e10"},
		"tools/official_client_capture/tests/test_upstream_byte_relay.py":                     {"a305671d383299d435320e012140237592a77489ed5450fc7c646adfafeac70a"},
		"tools/official_client_capture/upstream_byte_relay.py":                                {"c98b456bf592fb2c7c66ce13e7ffeb4c6cb31e79d70a701b92310583d21e6a2e"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 service r12e 预连接 transition 路径数量非法")
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
			return errors.New("Codex 0.149.1 service r12e 预连接 transition 条目非法：" + expectedPath)
		}
		current, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(entry.Path)))
		currentDigest := upstreamMergeFrameworkServiceDigest(current)
		if readErr != nil || (currentDigest != entry.ToSHA256 &&
			!codex01491R13CandidateCoordinateSupersedesService(
				entry.Path,
				entry.ToSHA256,
				currentDigest,
			)) {
			return errors.New("Codex 0.149.1 service r12e 预连接 transition 当前摘要不一致：" + entry.Path)
		}
	}
	return nil
}

func codex01491R12EPreconnectSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491R15FormalClassificationSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadCodex01491R12EPreconnectServiceTransition()
	if err != nil {
		return false
	}
	edges := make(map[string][]string)
	for _, entry := range receipt.Transitions {
		if entry.Path != path {
			continue
		}
		for _, predecessor := range entry.PredecessorSHA256s {
			edges[predecessor] = append(edges[predecessor], entry.ToSHA256)
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

func TestCodex01491R12EPreconnectServiceTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R12EPreconnectServiceTransition()
	if err != nil {
		t.Fatal(err)
	}
	var repair map[string]any
	if err := json.Unmarshal(receipt.RepairFacts, &repair); err != nil {
		t.Fatal(err)
	}
	repair["request_or_response_bytes_modified"] = true
	receipt.RepairFacts, err = json.Marshal(repair)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateCodex01491R12EPreconnectServiceTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 service r12e 预连接 transition 接受了字节改写")
	}
}
