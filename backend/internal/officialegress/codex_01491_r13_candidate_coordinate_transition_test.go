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

const codex01491R13CandidateCoordinatePath = "docs/egress/maintenance/codex-0.149.1-r13-candidate-coordinate-transition.json"

type codex01491R13CandidateCoordinateReceipt struct {
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
	FailedAttempt json.RawMessage                            `json:"failed_attempt"`
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
	codex01491R13CandidateCoordinateOnce   sync.Once
	codex01491R13CandidateCoordinateCached codex01491R13CandidateCoordinateReceipt
	codex01491R13CandidateCoordinateError  error
)

func codex01491R13CandidateCoordinateExpectedPaths() []string {
	return []string{
		"backend/internal/officialegress/codex_01491_r12e_preconnect_transition_test.go",
		"backend/internal/officialegress/codex_01491_r13_candidate_coordinate_transition_test.go",
		"backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go",
		"backend/internal/service/codex_01491_r12e_preconnect_transition_test.go",
		"backend/internal/service/codex_01491_r13_candidate_coordinate_transition_test.go",
		"backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"tools/official_client_capture/codex_upgrade.py",
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r12e_preconnect_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r13_candidate_coordinate_transition.py",
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py",
		"tools/official_client_capture/tests/test_codex_upgrade_capture_lifecycle.py",
	}
}

func codex01491R13CandidateCoordinateSectionDigest(raw json.RawMessage) (string, error) {
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

func loadCodex01491R13CandidateCoordinateTransition() (
	codex01491R13CandidateCoordinateReceipt,
	error,
) {
	codex01491R13CandidateCoordinateOnce.Do(func() {
		codex01491R13CandidateCoordinateCached, codex01491R13CandidateCoordinateError =
			loadCodex01491R13CandidateCoordinateTransitionUncached()
	})
	return codex01491R13CandidateCoordinateCached, codex01491R13CandidateCoordinateError
}

func loadCodex01491R13CandidateCoordinateTransitionUncached() (
	codex01491R13CandidateCoordinateReceipt,
	error,
) {
	var receipt codex01491R13CandidateCoordinateReceipt
	raw, err := codex01491RepoFile(codex01491R13CandidateCoordinatePath)
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex 0.149.1 r13 候选坐标 transition 尾部存在额外 JSON")
	}
	if err := codex01491VerifyIdentity(raw, receipt.IdentitySHA); err != nil {
		return receipt, err
	}
	return receipt, validateCodex01491R13CandidateCoordinateTransition(receipt)
}

func validateCodex01491R13CandidateCoordinateTransition(
	receipt codex01491R13CandidateCoordinateReceipt,
) error {
	if receipt.SchemaVersion != "official-client-codex-0.149.1-r13-candidate-coordinate-transition/v1" ||
		receipt.BaseCommit != "66d2c5ce29d8911982c91b738ab623a83c8aea4e" ||
		receipt.Scope != "codex-0.149.1-r13-candidate-coordinate" ||
		receipt.FrameworkStage != "VC-4/P0-R13-CANDIDATE-COORDINATE" ||
		receipt.Result != "r13_candidate_coordinate_repair_verified_new_campaign_required" {
		return errors.New("Codex 0.149.1 r13 候选坐标 transition 顶层事实非法")
	}
	if _, err := time.Parse(time.RFC3339, receipt.IssuedAtUTC); err != nil {
		return errors.New("Codex 0.149.1 r13 候选坐标 transition 时间非法")
	}
	predecessorRaw, err := codex01491RepoFile(codex01491R12EPreconnectPath)
	if err != nil {
		return err
	}
	var predecessor struct {
		IdentitySHA string `json:"identity_sha256"`
	}
	if err := json.Unmarshal(predecessorRaw, &predecessor); err != nil {
		return err
	}
	if receipt.PredecessorTransition.Path != codex01491R12EPreconnectPath ||
		receipt.PredecessorTransition.FileSHA256 != "3ac2793fa7fca9f8f0b4c7362b5c4c1fe18ff919a9b9470af2db02062183df45" ||
		receipt.PredecessorTransition.FileSHA256 != upstreamMergeFrameworkDigest(predecessorRaw) ||
		receipt.PredecessorTransition.IdentitySHA256 != "7f802944d28fe644f2d7da7190448a8bca7797b2901ed483606e1fe6e8725944" ||
		receipt.PredecessorTransition.IdentitySHA256 != predecessor.IdentitySHA {
		return errors.New("Codex 0.149.1 r13 候选坐标 transition 前序绑定非法")
	}

	expectedSections := map[string]string{
		"boundaries":     "e7282c931cbb2690a5210c2852d7ce48d6618742dc8279a7eec4fae5f9d75249",
		"failed_attempt": "bddc72a7b5ba4543d5cd4fcacd6763a2f0daefa87d64fe242be651f9d2439a37",
		"diagnosis":      "6eb3d0faa7c46863c9c7472e3bec93e717602a74adfb02798c55376edb92c827",
		"repair_facts":   "71c6a5d2c7cfe40af754c215731f15be803d117f8560cfb65dd1338685bca4f8",
		"timing":         "604770e99949be33fab332649dd3f37d3f5b688373025a01d2c1d707b09e64a4",
		"verification":   "972387e5c0de2aa4cd72599ff0159ae0e1c7012d70411e69d4fb18fc3967eaf0",
		"safety":         "acd60e4d983d6242446ee5e3604b5e7dee55a09265c1b4726f7ae1db117ef27b",
	}
	sections := map[string]json.RawMessage{
		"boundaries":     receipt.Boundaries,
		"failed_attempt": receipt.FailedAttempt,
		"diagnosis":      receipt.Diagnosis,
		"repair_facts":   receipt.RepairFacts,
		"timing":         receipt.Timing,
		"verification":   receipt.Verification,
		"safety":         receipt.Safety,
	}
	for name, raw := range sections {
		digest, digestErr := codex01491R13CandidateCoordinateSectionDigest(raw)
		if digestErr != nil || digest != expectedSections[name] {
			return errors.New("Codex 0.149.1 r13 候选坐标 transition 事实非法：" + name)
		}
	}

	expectedPaths := codex01491R13CandidateCoordinateExpectedPaths()
	expectedPredecessors := map[string][]string{
		"backend/internal/officialegress/codex_01491_r12e_preconnect_transition_test.go":              {"36f55ef0e05a6ad18638210c11d5ca22ad0103b7601bb0a7688e33ab6c233322"},
		"backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go":         {"1d2cec832db7fc457516a70863431f5207cf31dea6b9cd5e9dabb8f379ac2206", "3408049d51027e558aac14f2bef89a6942d61695170afccbad553c7745b9277f"},
		"backend/internal/service/codex_01491_r12e_preconnect_transition_test.go":                     {"1bfdd64988feb0ac4636a7a987ed45750182399aff3309d68e043b54cc730a3e"},
		"backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go":                {"06d610fae1c5c82765813da9ba1aed09ab90b6a8307ddfd59d0db2670386c971", "ac51a0742fef4ac120705a91bdbf5267180ce4c6e913314e6311239d0825fd6e"},
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md":                                                    {"2a72327aa8210d2163073a9bc656b5578fbb7c1aa4197deb88343a5a156a6646"},
		"tools/official_client_capture/codex_upgrade.py":                                              {"ca8af9d63169a8781c5ad278bb7e31fec02a5ea040aa6d3aeb76a2f794bde401"},
		"tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py": {"0b1fd92538669cc3b4780e89ab29fdbd5964f5233687526cbf795ddddb82f6dc", "96482eee11a088a5c7f41127cd2c7f331e78cf198661fc6ebe00e46eafa81e94"},
		"tools/official_client_capture/tests/test_codex_01491_r12e_preconnect_transition.py":          {"dc544ede64430c9704035d428c64f062d7c7572ed217ee65b1e9f19c993a7c33"},
		"tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py":     {"607a579b0cab4109883204b72bae32d765be1823897ccbbfaf7b11b519fabdc3", "c6ddc9a5c89e191a0b6896da681f17696c4055fcc33a23305187b0bd3d3d0874"},
		"tools/official_client_capture/tests/test_codex_upgrade_capture_lifecycle.py":                 {"10c58cd23995dd2ae61bd81df591be3fca7540177ad605ccc319cf7dba28f15f"},
	}
	if len(receipt.Transitions) != len(expectedPaths) {
		return errors.New("Codex 0.149.1 r13 候选坐标 transition 路径数量非法")
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
			return errors.New("Codex 0.149.1 r13 候选坐标 transition 条目非法：" + expectedPath)
		}
		current, readErr := codex01491RepoFile(entry.Path)
		if readErr != nil || upstreamMergeFrameworkDigest(current) != entry.ToSHA256 {
			return errors.New("Codex 0.149.1 r13 候选坐标 transition 当前摘要不一致：" + entry.Path)
		}
		paths = append(paths, entry.Path)
	}
	pathRaw, err := json.Marshal(paths)
	if err != nil {
		return err
	}
	pathRaw = append(pathRaw, '\n')
	if upstreamMergeFrameworkDigest(pathRaw) != receipt.PathSetSHA256 {
		return errors.New("Codex 0.149.1 r13 候选坐标 transition 路径摘要不一致")
	}
	return nil
}

func codex01491R13CandidateCoordinateSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCodex01491R13CandidateCoordinateTransition()
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

func TestCodex01491R13CandidateCoordinateTransitionIsFrozen(t *testing.T) {
	receipt, err := loadCodex01491R13CandidateCoordinateTransition()
	if err != nil {
		t.Fatal(err)
	}
	var safety map[string]any
	if err := json.Unmarshal(receipt.Safety, &safety); err != nil {
		t.Fatal(err)
	}
	safety["vircs_accessed"] = true
	receipt.Safety, err = json.Marshal(safety)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateCodex01491R13CandidateCoordinateTransition(receipt); err == nil {
		t.Fatal("Codex 0.149.1 r13 候选坐标 transition 接受了 Vircs 访问")
	}
}
