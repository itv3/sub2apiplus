package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
)

const codex0151FormalRecoverySourceTransitionPath = "docs/egress/maintenance/codex-cli-0151-formal-recovery-source-transition.json"
const codex0151WorktreeSuccessorPath = "docs/egress/maintenance/codex-cli-0151-worktree-successor.json"

type codex0151WorktreeSuccessorState struct {
	Existence string `json:"existence"`
	FileType  string `json:"file_type"`
	Mode      string `json:"mode"`
	Size      int    `json:"size"`
	SHA256    string `json:"sha256"`
}

type codex0151WorktreeSuccessorEntry struct {
	Path   string                          `json:"path"`
	Before codex0151WorktreeSuccessorState `json:"before"`
	After  codex0151WorktreeSuccessorState `json:"after"`
	Reason string                          `json:"reason"`
}

type codex0151WorktreeSuccessorReceipt struct {
	SchemaVersion string `json:"schema_version"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	BaseCommit    string `json:"base_commit"`
	Scope         string `json:"scope"`
	Predecessor   struct {
		Release string `json:"release"`
		Commit  string `json:"commit"`
	} `json:"predecessor"`
	Policy struct {
		HistoricalReceiptsRewriteAllowed bool   `json:"historical_receipts_rewrite_allowed"`
		HistoricalSourceDriftLedgerUsed  bool   `json:"historical_source_drift_ledger_used"`
		Arm64DeploymentAllowed           bool   `json:"arm64_deployment_allowed"`
		Reason                           string `json:"reason"`
	} `json:"policy"`
	Entries        []codex0151WorktreeSuccessorEntry `json:"entries"`
	Verification   []string                          `json:"verification"`
	Result         string                            `json:"result"`
	IdentitySHA256 string                            `json:"identity_sha256"`
}

var (
	codex0151WorktreeSuccessorOnce    sync.Once
	codex0151WorktreeSuccessorCached  codex0151WorktreeSuccessorReceipt
	codex0151WorktreeSuccessorLoadErr error
)

func loadCodex0151WorktreeSuccessor() (codex0151WorktreeSuccessorReceipt, error) {
	codex0151WorktreeSuccessorOnce.Do(func() {
		raw, err := os.ReadFile(codex01491TerminalRepoPath(codex0151WorktreeSuccessorPath))
		if err != nil {
			codex0151WorktreeSuccessorLoadErr = err
			return
		}
		var receipt codex0151WorktreeSuccessorReceipt
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&receipt); err != nil {
			codex0151WorktreeSuccessorLoadErr = err
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			codex0151WorktreeSuccessorLoadErr = errors.New("Codex CLI 0.151 工作区 successor 尾部存在额外 JSON")
			return
		}
		if receipt.SchemaVersion != "sub2api-codex-cli-0151-worktree-successor/v1" &&
			receipt.SchemaVersion != "sub2apiplus-codex-cli-0151-worktree-successor/v1" {
			codex0151WorktreeSuccessorLoadErr = errors.New("Codex CLI 0.151 工作区 successor schema 非法")
			return
		}
		if receipt.BaseCommit != "86aed19f738326e528808c5a4438bc4bcfaee02f" ||
			receipt.Scope != "codex-cli-0.151-current-worktree" ||
			receipt.Policy.HistoricalReceiptsRewriteAllowed ||
			receipt.Policy.HistoricalSourceDriftLedgerUsed ||
			receipt.Policy.Arm64DeploymentAllowed ||
			receipt.Policy.Reason == "" || len(receipt.Entries) == 0 {
			codex0151WorktreeSuccessorLoadErr = errors.New("Codex CLI 0.151 工作区 successor 顶层事实非法")
			return
		}
		for index, entry := range receipt.Entries {
			if entry.Path == "" || entry.Reason == "" ||
				!receiptSHA256(entry.After.SHA256) || entry.After.Existence != "present" ||
				entry.Before == entry.After ||
				(index > 0 && entry.Path <= receipt.Entries[index-1].Path) {
				codex0151WorktreeSuccessorLoadErr = errors.New("Codex CLI 0.151 工作区 successor 条目非法")
				return
			}
		}
		codex0151WorktreeSuccessorCached = receipt
	})
	return codex0151WorktreeSuccessorCached, codex0151WorktreeSuccessorLoadErr
}

func codex0151WorktreeSuccessorAfter(path, currentDigest string) bool {
	if !receiptSHA256(currentDigest) {
		return false
	}
	receipt, err := loadCodex0151WorktreeSuccessor()
	if err != nil {
		return false
	}
	for _, entry := range receipt.Entries {
		// 历史 addition 也必须沿同一路径、已审计的精确摘要边承接后续实现。
		if entry.Path == path && (entry.After.SHA256 == currentDigest ||
			auditedSourceSuccessorReaches(path, entry.After.SHA256, currentDigest)) {
			return true
		}
	}
	return false
}

func TestCodex0151WorktreeSuccessorAfterRejectsUnregisteredDigest(t *testing.T) {
	const path = "backend/internal/service/openai_invalid_encrypted_content_state.go"
	raw, err := os.ReadFile(codex01491TerminalRepoPath(path))
	if err != nil {
		t.Fatal(err)
	}
	digest := upstreamMergeFrameworkDigest(raw)
	if !codex0151WorktreeSuccessorAfter(path, digest) {
		t.Fatal("已登记的精确后继链必须能够承接历史 addition")
	}
	if codex0151WorktreeSuccessorAfter(path, strings.Repeat("a", 64)) ||
		codex0151WorktreeSuccessorAfter(path+".unknown", digest) {
		t.Fatal("未登记的摘要或其他路径不能借用已登记后继链")
	}
}

func codex0151WorktreeSuccessorEdge(path, priorDigest, currentDigest string) bool {
	if !receiptSHA256(priorDigest) || strings.Trim(priorDigest, "0") == "" {
		return false
	}
	return codex0151WorktreeSuccessorAfter(path, currentDigest)
}

var (
	codex0151FormalRecoverySourceTransitionOnce   sync.Once
	codex0151FormalRecoverySourceTransitionCached codex0151ToolReadinessReceipt
	codex0151FormalRecoverySourceTransitionErr    error
)

func loadCodex0151FormalRecoverySourceTransition() (codex0151ToolReadinessReceipt, error) {
	codex0151FormalRecoverySourceTransitionOnce.Do(func() {
		codex0151FormalRecoverySourceTransitionCached,
			codex0151FormalRecoverySourceTransitionErr =
			readCodex0151FormalRecoverySourceTransition()
	})
	return codex0151FormalRecoverySourceTransitionCached,
		codex0151FormalRecoverySourceTransitionErr
}

func readCodex0151FormalRecoverySourceTransition() (codex0151ToolReadinessReceipt, error) {
	var receipt codex0151ToolReadinessReceipt
	raw, err := os.ReadFile(codex01491TerminalRepoPath(codex0151FormalRecoverySourceTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Codex CLI 0.151 Formal 恢复 transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil {
		return receipt, err
	}
	canonical = append(canonical, '\n')
	if upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("Codex CLI 0.151 Formal 恢复 transition 自摘要不一致")
	}
	if err := validateCodex0151FormalRecoverySourceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateCodex0151FormalRecoverySourceTransition(receipt codex0151ToolReadinessReceipt) error {
	if receipt.SchemaVersion != "sub2apiplus-codex-cli-0151-formal-recovery-source-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-31T05:15:56Z" ||
		receipt.BaseCommit != "a7ff0ad3955af68e8878b552d6a58ae73fda1f62" ||
		receipt.Scope != "codex-cli-0.151-formal-recovery" ||
		receipt.Result != "passed_codex_cli_0151_formal_recovery" {
		return errors.New("Codex CLI 0.151 Formal 恢复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "codex_cli_0151_system_proxy_capture_tool_successor_source_transition" ||
		receipt.Predecessor.Path != codex0151SystemProxyCaptureToolSuccessorPath {
		return errors.New("Codex CLI 0.151 Formal 恢复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(codex01491TerminalRepoPath(receipt.Predecessor.Path))
	if err != nil || upstreamMergeFrameworkDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("Codex CLI 0.151 Formal 恢复 transition 前序摘要不一致")
	}
	expectedVerification := []string{
		"python3 -m unittest discover -s tools/official_client_capture/tests -p 'test_*.py'",
		"go test ./internal/officialegress ./internal/service -run 'TestCodex(01491Terminal|0151)' -count=1",
		"make check-egress-spec",
	}
	if !slices.Equal(receipt.Verification, expectedVerification) {
		return errors.New("Codex CLI 0.151 Formal 恢复 transition 验证集合非法")
	}
	if receipt.Safety.LiveAccountUsed || receipt.Safety.OnlineAcceptancePerformed ||
		receipt.Safety.ProductionConfigChanged || receipt.Safety.OfficialEgressProfileChanged {
		return errors.New("Codex CLI 0.151 Formal 恢复 transition 安全边界非法")
	}
	expectedFrom := map[string]string{
		"backend/internal/officialegress/catalogdata/runtime/release-catalog.json":               "88470ee5918ff558d0c32136499fa7e6d595834c5b7a084a02785e0e51585c3d",
		"backend/internal/officialegress/codex_01491_terminal_state_test.go":                     "1d2ecea92a9952f75007669cda6553bfbd27c6983e36435ece5de8c573a1870c",
		"backend/internal/officialegress/codex_0151_system_proxy_capture_tool_successor_test.go": "831d49a2917282857e2e075771ad7e1d39f97edd017b27a5824e831855086bdb",
		"backend/internal/officialegress/compiler_static_url_closure_test.go":                    "274799ed7352a5ddb73d76184b441cc5ca4ec61db54abcf9769633ea5ea6ac97",
		"backend/internal/officialegress/profilecontract/testdata/snapshot-catalog.json":         "f2a38b220a5edb0ee3c2e4f734da7a9d4d91bf80d0d5c2f7805f8dbfb5d87c37",
		"backend/internal/officialegress/releasecontract/testdata/release-graph.json":            "057264d864aea27ebafecf504e95b8c948f25ac20f11fdabbfd2385d35c85465",
		"backend/internal/officialegress/version_route_receipts_test.go":                         "404c059d0234182c767ae927bf9705343a6f7ecbf0b4e9518199c9d7202964b2",
		"backend/internal/service/codex_0151_system_proxy_capture_tool_successor_test.go":        "b8def6b7002949dc50eb6b4c072b12406d5a57ceba119e01a66d4c11f2146acd",
		"backend/internal/service/official_egress_codex_files.go":                                "2421f642b3e3ecf167cce207c9a9c9a8d1167a1c907d476ab6e2941629263df4",
		"backend/internal/service/official_egress_codex_files_test.go":                           "6b7816e4062c60b259abf77b66c4c282e03de17b90dd9163d8e5621cc448a53b",
		"backend/internal/service/official_egress_openai_http.go":                                "d38f0c9f2286b06400f769a50a762bbb6b95dce5710c7a81e723615e5f8d9b70",
		"backend/internal/service/official_egress_openai_ws.go":                                  "69f4ae554f120d6d5847d7caf39871062a149789a760b6c175d98556767d64fd",
		"backend/internal/service/official_egress_uuid.go":                                       "2617876b34300d33867cef83ea59dd0c05f89b9c677488016cbe8b3f353d210a",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md":                                               "5025a988e5dee9dcb6fc653c238c3c3803f0f2fe6588d5e243d7b83e5f3e6f77",
		"docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md":                                            "f836ec63336f2f4c178a5617e6208e22a6573d7416938d6bb389e31856cc4eb3",
		"tools/check_ledger_completeness.py":                                                     "b9481a9b6acaa7c1b26943ca51dd8e5f98fb8b8e039aeab597d7bdbc3bd169c0",
		"tools/official_client_capture/candidate_rule_assertion.py":                              "3c4af6be2e6c862b9150898c75151b7cc72569694472956aed928ded1245fa3e",
		"tools/official_client_capture/candidate_rule_expectations_0_149_1.json":                 "90641bb30f8ad56a9ff99eabb22965141814694a042bebb1324cdb8d306dc487",
		"tools/official_client_capture/candidate_test_trace.py":                                  "52c5143452082cd83d13ea92bac441df5b3f775beccd4978e390760c8300cbc1",
		"tools/official_client_capture/codex_upgrade.py":                                         "33016a16fcbb93eaf6fde54a2fd3982ddc856dd7dbe4d8d1f69128b9e1979f55",
		"tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json":                     "fae060c223a5b2be282027cf1a249052108cabe533f1ab79ec26b08d24934836",
		"tools/official_client_capture/codex_upgrade_scenarios_0_151_0.json":                     "a431a562e347e0d7faf7106456e171facdab09edeb4367ca91630bdc30cc30f1",
		"tools/official_client_capture/tests/test_candidate_rule_assertion.py":                   "d8931d2229997220c60a97fde68f02077201a984d376552e8e60e047bfeaf100",
		"tools/official_client_capture/tests/test_candidate_test_trace.py":                       "290e022cd74d8a2c427b28b1add1ff5397824ce019a2ad6a7ade95200e3305bd",
		"tools/official_client_capture/tests/test_codex_upgrade.py":                              "3df8d26a1264bacb899188fa2d69954968a90f91aab4aff11151c7193c45841f",
	}
	expectedAdditions := map[string]struct{}{
		"backend/internal/officialegress/codex_0151_formal_recovery_source_transition_test.go":                                                             {},
		"backend/internal/officialegress/catalogdata/runtime/profiles/0.151.0/dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json":       {},
		"backend/internal/officialegress/catalogdata/runtime/release-graphs/14b8b4a1e52e69aaf3625cff5a89b81ce2f3cd4b39b8f1416911b72f00a38815.json":         {},
		"backend/internal/officialegress/catalogdata/runtime/snapshot-catalogs/af1b4a7513b02556d4a3245a709e1e153969892a3d8ab6e1abebff6c3284b450.json":      {},
		"backend/internal/officialegress/profilecontract/testdata/snapshots/0.151.0/dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json": {},
		"backend/internal/service/codex_0151_formal_recovery_source_transition_test.go":                                                                    {},
		"backend/internal/service/official_egress_openai_anchor.go":                                                                                        {},
		"backend/internal/service/official_egress_uuid_test.go":                                                                                            {},
		"docs/egress/maintenance/codex-cli-0151-formal-recovery/plan.json":                                                                                 {},
		"tools/official_client_capture/candidate_rule_expectations_0_151_0.json":                                                                           {},
		"tools/official_client_capture/candidate_test_fact_map_0_151_0.json":                                                                               {},
	}
	transitionPaths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if expectedFrom[transition.Path] != transition.FromSHA256 ||
			!receiptSHA256(transition.ToSHA256) || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Codex CLI 0.151 Formal 恢复 transition 条目非法")
		}
		current, readErr := os.ReadFile(codex01491TerminalRepoPath(transition.Path))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!codex0151Arm64TypescriptDependencySourceTransitionSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			) && !codex0151WorktreeSuccessorAfter(transition.Path, currentDigest)) {
			return errors.New("Codex CLI 0.151 Formal 恢复 transition 当前摘要不一致：" + transition.Path)
		}
		transitionPaths = append(transitionPaths, transition.Path)
	}
	additionPaths := make([]string, 0, len(receipt.Additions))
	for _, addition := range receipt.Additions {
		if _, ok := expectedAdditions[addition.Path]; !ok || !receiptSHA256(addition.SHA256) ||
			strings.TrimSpace(addition.Reason) == "" {
			return errors.New("Codex CLI 0.151 Formal 恢复 addition 条目非法")
		}
		current, readErr := os.ReadFile(codex01491TerminalRepoPath(addition.Path))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != addition.SHA256 &&
			!codex0151Arm64TypescriptDependencySourceTransitionSupersedes(
				addition.Path,
				addition.SHA256,
				currentDigest,
			) && !codex0151WorktreeSuccessorAfter(addition.Path, currentDigest)) {
			return errors.New("Codex CLI 0.151 Formal 恢复 addition 当前摘要不一致：" + addition.Path)
		}
		additionPaths = append(additionPaths, addition.Path)
	}
	if len(receipt.Transitions) != len(expectedFrom) || len(receipt.Additions) != len(expectedAdditions) ||
		!slices.IsSorted(transitionPaths) || len(transitionPaths) != len(slices.Compact(append([]string(nil), transitionPaths...))) ||
		!slices.IsSorted(additionPaths) || len(additionPaths) != len(slices.Compact(append([]string(nil), additionPaths...))) {
		return errors.New("Codex CLI 0.151 Formal 恢复路径闭集非法")
	}
	return nil
}

func codex0151FormalRecoverySourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if codex0151CurrentSourceDigestAccepted(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadCodex0151FormalRecoverySourceTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			(transition.ToSHA256 == currentDigest ||
				codex0151Arm64TypescriptDependencySourceTransitionSupersedes(
					path,
					transition.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	for _, addition := range receipt.Additions {
		if addition.Path == path && addition.SHA256 == priorDigest &&
			codex0151WorktreeSuccessorAfter(path, currentDigest) {
			return true
		}
	}
	return codex0151Arm64TypescriptDependencySourceTransitionSupersedes(path, priorDigest, currentDigest)
}

func TestCodex0151FormalRecoverySourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadCodex0151FormalRecoverySourceTransition(); err != nil {
		t.Fatal(err)
	}
}

func TestCodex0151FormalRecoverySourceTransitionRejectsMutation(t *testing.T) {
	receipt, err := loadCodex0151FormalRecoverySourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name   string
		mutate func(*codex0151ToolReadinessReceipt)
	}{
		{name: "路径摘要漂移", mutate: func(mutated *codex0151ToolReadinessReceipt) {
			mutated.Transitions[0].ToSHA256 = strings.Repeat("0", 64)
		}},
		{name: "安全边界放宽", mutate: func(mutated *codex0151ToolReadinessReceipt) {
			mutated.Safety.ProductionConfigChanged = true
		}},
		{name: "闭集缺项", mutate: func(mutated *codex0151ToolReadinessReceipt) {
			mutated.Additions = mutated.Additions[1:]
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			mutated := receipt
			mutated.Transitions = append([]openAIReplayOOMRepairTransition(nil), receipt.Transitions...)
			mutated.Additions = append([]openAIReplayOOMRepairAddition(nil), receipt.Additions...)
			test.mutate(&mutated)
			if err := validateCodex0151FormalRecoverySourceTransition(mutated); err == nil {
				t.Fatal("变异后的 Formal 恢复 transition 被错误接受")
			}
		})
	}
}
