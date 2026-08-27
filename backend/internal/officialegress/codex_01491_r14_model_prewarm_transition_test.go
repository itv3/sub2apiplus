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

const codex01491R14ModelPrewarmPath = "docs/egress/maintenance/codex-0.149.1-r14-model-prewarm-transition.json"

type codex01491R14ModelPrewarmReceipt struct {
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
	Boundaries    json.RawMessage                            `json:"boundaries"`
	FormalFailure json.RawMessage                            `json:"formal_failure"`
	Diagnosis     json.RawMessage                            `json:"diagnosis"`
	RepairFacts   json.RawMessage                            `json:"repair_facts"`
	Timing        json.RawMessage                            `json:"timing"`
	PathSetSHA256 string                                     `json:"path_set_sha256"`
	Transitions   []codex01491CandidateSourceTransitionEntry `json:"transitions"`
	Verification  json.RawMessage                            `json:"verification"`
	Safety        json.RawMessage                            `json:"safety"`
	Result        string                                     `json:"result"`
	IdentitySHA   string                                     `json:"identity_sha256"`
}

var (
	codex01491R14ModelPrewarmOnce   sync.Once
	codex01491R14ModelPrewarmCached codex01491R14ModelPrewarmReceipt
	codex01491R14ModelPrewarmError  error
)

func codex01491R14ModelPrewarmExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r12e_preconnect_transition_test.go",
		"backend/internal/officialegress/codex_01491_r13_candidate_coordinate_transition_test.go",
		"backend/internal/officialegress/codex_01491_r14_model_prewarm_transition_test.go",
		"backend/internal/service/codex_01491_r12e_preconnect_transition_test.go",
		"backend/internal/service/codex_01491_r13_candidate_coordinate_transition_test.go",
		"backend/internal/service/codex_01491_r14_model_prewarm_transition_test.go",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
		"tools/official_client_capture/drive_codex_model_catalog.py",
		"tools/official_client_capture/run_official_relay_scenario.sh",
		"tools/official_client_capture/tests/test_codex_01491_r12e_preconnect_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r13_candidate_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r14_model_prewarm_transition.py",
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py",
	}
}

func codex01491R14ModelPrewarmSectionDigest(raw json.RawMessage) (string, error) {
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return upstreamMergeFrameworkDigest(canonical), nil
}

func loadCodex01491R14ModelPrewarmTransition() (
	codex01491R14ModelPrewarmReceipt,
	error,
) {
	codex01491R14ModelPrewarmOnce.Do(func() {
		codex01491R14ModelPrewarmCached, codex01491R14ModelPrewarmError =
			loadCodex01491R14ModelPrewarmTransitionUncached()
	})
	return codex01491R14ModelPrewarmCached, codex01491R14ModelPrewarmError
}

func loadCodex01491R14ModelPrewarmTransitionUncached() (
	codex01491R14ModelPrewarmReceipt,
	error,
) {
	var receipt codex01491R14ModelPrewarmReceipt
	raw, err := codex01491RepoFile(codex01491R14ModelPrewarmPath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r14 模型预热 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA); err != nil {
		return receipt, err
	}
	return receipt, validateCodex01491R14ModelPrewarmTransition(receipt)
}

func validateCodex01491R14ModelPrewarmTransition(
	receipt codex01491R14ModelPrewarmReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r14-model-prewarm-transition/v1" ||
		receipt.BaseCommit != "e697ef456ddc9cd54bac79f4126af938ab445be5" ||
		receipt.Scope != "codex-0.149.1-r14-model-prewarm" ||
		receipt.FrameworkStage != "VC-4/P0-R14-MODEL-PREWARM" ||
		receipt.Result != "r14_model_prewarm_verified_new_campaign_required" {
		return errors.New("Codex 0.149.1 r14 模型预热 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r14 模型预热 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R13CandidateCoordinatePath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R13CandidateCoordinatePath ||
		receipt.PredecessorTransition.FileSHA256 != "ff6eccbc50f80aac1e3956ff4e6de1df7bd99740d53b896ecc28bcc1e86ad393" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "5a0fca1636910ab01d308585de17ba39a132e5e1f4efdf8633222dbe11267a8c" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA {
		return errors.New("Codex 0.149.1 r14 模型预热 transition 前序绑定非法")
	}

	expectedSections := map[string]string{
		"boundaries":     "e7282c931cbb2690a5210c2852d7ce48d6618742dc8279a7eec4fae5f9d75249",
		"formal_failure": "44b904c3c3ba519d75d59ddec59f8b30190e4dd87a165a0db4413f19cd995af0",
		"diagnosis":      "d02d32d363261ccc8a2576cd8aee80cb7b10959fa1b1208f1e5c0dea5cfdf48c",
		"repair_facts":   "944fb8abfc8da889447b2f724b05b0a0b65f3e008384c39e076f8ec23783a678",
		"timing":         "309d7d01ba34b51b7964d179a4b7b303efda3cad05845fedd42a220a816f83ec",
		"verification":   "64e478cc5a2a202f2957a4a985400645971c882547b99692d720daa1c6bd91b4",
		"safety":         "a63fd9814edb81a1e9722490c2589ec1077e63711b0b6b3801b9ddfb43c2b49e",
	}
	sections := map[string]json.RawMessage{
		"boundaries":     receipt.Boundaries,
		"formal_failure": receipt.FormalFailure,
		"diagnosis":      receipt.Diagnosis,
		"repair_facts":   receipt.RepairFacts,
		"timing":         receipt.Timing,
		"verification":   receipt.Verification,
		"safety":         receipt.Safety,
	}
	for name, raw := range sections {
		digest, digestErr := codex01491R14ModelPrewarmSectionDigest(raw)
		if digestErr != nil || digest != expectedSections[name] {
			return errors.New("Codex 0.149.1 r14 模型预热 transition 事实非法：" + name)
		}
	}

	expectedPaths := codex01491R14ModelPrewarmExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r12e_preconnect_transition_test.go":              {"0774e56498c6ea87146708a0e48568e448603045d04fe8bcec8b6c15ab729c25"},
		"backend/internal/officialegress/codex_01491_r13_candidate_coordinate_transition_test.go":     {"2316818b0eb69f782fd60f23793afec9fe496a312a1e1b2a322add20680e7dc0"},
		"backend/internal/service/codex_01491_r12e_preconnect_transition_test.go":                     {"f9ded5a4bb97b1bba69ca4be82777a44423a6daf0b150eb8ddb1b0ab07538b48"},
		"backend/internal/service/codex_01491_r13_candidate_coordinate_transition_test.go":            {"655f1afbdd214575d393b0b8508dec3bf76d3434f5c77dca2a9c7841a5e7bbaf"},
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                          {"f82d566c7f203db446110b7a9aca7cf9f5daaf9a0d4df769d0cefc12c95af553"},
		"tools/official_client_capture/drive_codex_model_catalog.py":                                  {"579ce12a9cde1ba7d3bc3ed7d273c9dff8907998a05840890493ffa8be27aba5"},
		"tools/official_client_capture/run_official_relay_scenario.sh":                                {"c51daf313c5710659fbca0d22ebe5bb60a47c8d0181d448a966becae7ec8822a"},
		"tools/official_client_capture/tests/test_codex_01491_r12e_preconnect_transition.py":          {"47d3b9a876c700ffb5f23fd276e8790e000af10fa7c8f845b51dd2234bb53e49"},
		"tools/official_client_capture/tests/test_codex_01491_r13_candidate_coordinate_transition.py": {"0372fca217cbefd2c5dcfe9001e8583a23709a0b6ab04fab6c83119f328d002b"},
		"tools/official_client_capture/tests/test_model_catalog_prewarm.py":                           {"9501f7228b3c1176d76b33a9b0b13087a834a5b710fb79c74d7d6a1bd5cce015"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r14 模型预热 transition 路径数量非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
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
			return errors.New("Codex 0.149.1 r14 模型预热 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r14 模型预热 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r14 模型预热 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R14ModelPrewarmSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R14ModelPrewarmTransition()
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

func TestCodex01491R14ModelPrewarmTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R14ModelPrewarmTransition()
	if err != nil {
		t.Fatal(err)
	}
	var safety map[string]any
	if err := json.Unmarshal(receipt.Safety, &safety); err != nil {
		t.Fatal(err)
	}
	safety["network_configuration_changed"] = true
	receipt.Safety, err = json.Marshal(safety)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateCodex01491R14ModelPrewarmTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r14 模型预热 transition 接受了网络配置变更")
	}
}
