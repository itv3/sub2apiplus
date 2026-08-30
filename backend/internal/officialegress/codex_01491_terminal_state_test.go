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
)

const codex01491TerminalStatePath = "docs/egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json"

type codex01491TerminalArtifact struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type codex01491TerminalTransition struct {
	Path               string   `json:"path"`
	State              string   `json:"state"`
	PriorSHA256s       []string `json:"prior_sha256s"`
	CurrentSHA256      string   `json:"current_sha256"`
	ConsolidatedReason string   `json:"consolidated_reason"`
}

type codex01491TerminalReceipt struct {
	SchemaVersion  string `json:"schema_version"`
	CompletedAtUTC string `json:"completed_at_utc"`
	Target         struct {
		Version               string `json:"version"`
		ProfileID             string `json:"profile_id"`
		ProfileSHA256         string `json:"profile_sha256"`
		PreviousVersion       string `json:"previous_version"`
		PreviousProfileSHA256 string `json:"previous_profile_sha256"`
	} `json:"target"`
	RuntimeCatalog struct {
		Catalog         codex01491TerminalArtifact `json:"catalog"`
		ReleaseGraph    codex01491TerminalArtifact `json:"release_graph"`
		SnapshotCatalog codex01491TerminalArtifact `json:"snapshot_catalog"`
		ActiveProfile   codex01491TerminalArtifact `json:"active_profile"`
	} `json:"runtime_catalog"`
	CatalogPromotion       codex01491TerminalArtifact     `json:"catalog_promotion"`
	ProductionActivation   codex01491TerminalArtifact     `json:"production_activation"`
	AuditArchive           codex01491TerminalArtifact     `json:"audit_archive"`
	SurfaceAdditions       []codex01491TerminalArtifact   `json:"surface_additions"`
	RetiredRuntimeProfiles []codex01491TerminalArtifact   `json:"retired_runtime_profiles"`
	Transitions            []codex01491TerminalTransition `json:"transitions"`
	Result                 string                         `json:"result"`
	IdentitySHA256         string                         `json:"identity_sha256"`
}

var (
	codex01491TerminalOnce    sync.Once
	codex01491TerminalCached  codex01491TerminalReceipt
	codex01491TerminalLoadErr error
)

func codex01491TerminalRepoPath(path string) string {
	return filepath.Join("../../..", filepath.FromSlash(path))
}

func validateCodex01491TerminalArtifact(artifact codex01491TerminalArtifact) error {
	if strings.TrimSpace(artifact.Path) == "" || len(artifact.SHA256) != 64 {
		return errors.New("0.149.1 终态制品坐标非法")
	}
	raw, err := os.ReadFile(codex01491TerminalRepoPath(artifact.Path))
	if err != nil || upstreamMergeFrameworkDigest(raw) != artifact.SHA256 {
		return errors.New("0.149.1 终态制品摘要不一致：" + artifact.Path)
	}
	return nil
}

func loadCodex01491TerminalState() (codex01491TerminalReceipt, error) {
	codex01491TerminalOnce.Do(func() {
		codex01491TerminalCached, codex01491TerminalLoadErr = readCodex01491TerminalState()
	})
	return codex01491TerminalCached, codex01491TerminalLoadErr
}

func readCodex01491TerminalState() (codex01491TerminalReceipt, error) {
	var receipt codex01491TerminalReceipt
	raw, err := os.ReadFile(codex01491TerminalRepoPath(codex01491TerminalStatePath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("0.149.1 终态收据尾部存在额外 JSON")
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
		return receipt, errors.New("0.149.1 终态收据自摘要不一致")
	}
	if receipt.SchemaVersion != "official-client-codex-0.149.1-terminal-state/v1" ||
		receipt.Target.Version != "0.149.1" ||
		receipt.Target.ProfileID != "codex-0.149.1-official-r1491-v2" ||
		receipt.Target.ProfileSHA256 != "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3" ||
		receipt.Target.PreviousVersion != "0.147.0" ||
		receipt.Target.PreviousProfileSHA256 != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		receipt.Result != "passed" || strings.TrimSpace(receipt.CompletedAtUTC) == "" {
		return receipt, errors.New("0.149.1 终态收据顶层事实非法")
	}
	artifacts := []codex01491TerminalArtifact{
		receipt.RuntimeCatalog.Catalog,
		receipt.RuntimeCatalog.ReleaseGraph,
		receipt.RuntimeCatalog.SnapshotCatalog,
		receipt.RuntimeCatalog.ActiveProfile,
		receipt.CatalogPromotion,
		receipt.ProductionActivation,
	}
	for _, artifact := range artifacts {
		if err := validateCodex01491TerminalArtifact(artifact); err != nil {
			return receipt, err
		}
	}
	if receipt.AuditArchive.Path != "codex-0.149.1-consolidated-audit-20260830.tar.gz" ||
		receipt.AuditArchive.SHA256 != "cfb3d9afb4453b95662fbcfdf794b4d80efe168fd69accf909abdbf40b6e30d9" {
		return receipt, errors.New("0.149.1 外部审计归档坐标非法")
	}
	if len(receipt.SurfaceAdditions) != 1 ||
		receipt.SurfaceAdditions[0].Path != "backend/internal/officialegress/routing_hint.go" {
		return receipt, errors.New("0.149.1 出站面终态增量非法")
	}
	if err := validateCodex01491TerminalArtifact(receipt.SurfaceAdditions[0]); err != nil {
		return receipt, err
	}
	if len(receipt.RetiredRuntimeProfiles) != 2 {
		return receipt, errors.New("0.149.1 退休画像数量非法")
	}
	for _, artifact := range receipt.RetiredRuntimeProfiles {
		if _, err := os.Stat(codex01491TerminalRepoPath(artifact.Path)); !os.IsNotExist(err) {
			return receipt, errors.New("已退休 0.145 画像仍存在：" + artifact.Path)
		}
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.ConsolidatedReason) == "" ||
			len(transition.PriorSHA256s) == 0 ||
			!slices.IsSorted(transition.PriorSHA256s) ||
			len(transition.PriorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PriorSHA256s...),
			)) {
			return receipt, errors.New("0.149.1 终态后继条目非法")
		}
		for _, prior := range transition.PriorSHA256s {
			if len(prior) != 64 || prior == transition.CurrentSHA256 {
				return receipt, errors.New("0.149.1 终态前序摘要非法")
			}
		}
		currentRaw, readErr := os.ReadFile(codex01491TerminalRepoPath(transition.Path))
		switch transition.State {
		case "present":
			currentDigest := upstreamMergeFrameworkDigest(currentRaw)
			if readErr != nil || len(transition.CurrentSHA256) != 64 ||
				(currentDigest != transition.CurrentSHA256 &&
					!openAIReplayOOMRepairSupersedes(
						transition.Path,
						transition.CurrentSHA256,
						currentDigest,
					) && !openAIWSCompatibilityGuardRepairSupersedes(
					transition.Path,
					transition.CurrentSHA256,
					currentDigest,
				)) {
				return receipt, errors.New("0.149.1 终态当前摘要不一致：" + transition.Path)
			}
		case "deleted":
			if !os.IsNotExist(readErr) || transition.CurrentSHA256 != "" {
				return receipt, errors.New("0.149.1 终态删除状态不一致：" + transition.Path)
			}
		default:
			return receipt, errors.New("0.149.1 终态文件状态非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("0.149.1 终态路径未严格排序")
	}
	return receipt, nil
}

// codex01491TerminalStateSupersedes 把已归档的逐轮 transition 压缩为一条精确终边。
// 只接受收据登记的 path／历史摘要／当前摘要三元组，不放宽生产路径或 wire 规则。
func codex01491TerminalStateSupersedes(path, priorDigest, currentDigest string) bool {
	if openAIReplayOOMRepairSupersedes(path, priorDigest, currentDigest) ||
		openAIWSCompatibilityGuardRepairSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadCodex01491TerminalState()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && slices.Contains(transition.PriorSHA256s, priorDigest) &&
			(transition.CurrentSHA256 == currentDigest ||
				openAIReplayOOMRepairSupersedes(
					path,
					transition.CurrentSHA256,
					currentDigest,
				) || openAIWSCompatibilityGuardRepairSupersedes(
				path,
				transition.CurrentSHA256,
				currentDigest,
			)) {
			return true
		}
	}
	return false
}

func TestCodex01491TerminalStateIsFrozen(t *testing.T) {
	if _, err := loadCodex01491TerminalState(); err != nil {
		t.Fatal(err)
	}
}

const openAIReplayOOMRepairTransitionPath = "docs/egress/maintenance/openai-replay-oom-repair-source-transition.json"

type openAIReplayOOMRepairPredecessor struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type openAIReplayOOMRepairTransition struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type openAIReplayOOMRepairAddition struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Reason string `json:"reason"`
}

type openAIReplayOOMRepairSafety struct {
	LiveAccountUsed              bool `json:"live_account_used"`
	OnlineAcceptancePerformed    bool `json:"online_acceptance_performed"`
	ProductionConfigChanged      bool `json:"production_config_changed"`
	OfficialEgressProfileChanged bool `json:"official_egress_profile_changed"`
}

type openAIReplayOOMRepairReceipt struct {
	SchemaVersion  string                            `json:"schema_version"`
	IssuedAtUTC    string                            `json:"issued_at_utc"`
	BaseCommit     string                            `json:"base_commit"`
	Scope          string                            `json:"scope"`
	Predecessor    openAIReplayOOMRepairPredecessor  `json:"predecessor"`
	Transitions    []openAIReplayOOMRepairTransition `json:"transitions"`
	Additions      []openAIReplayOOMRepairAddition   `json:"additions"`
	Verification   []string                          `json:"verification"`
	Safety         openAIReplayOOMRepairSafety       `json:"safety"`
	Result         string                            `json:"result"`
	IdentitySHA256 string                            `json:"identity_sha256"`
}

var (
	openAIReplayOOMRepairOnce    sync.Once
	openAIReplayOOMRepairCached  openAIReplayOOMRepairReceipt
	openAIReplayOOMRepairLoadErr error
)

func loadOpenAIReplayOOMRepairTransition() (openAIReplayOOMRepairReceipt, error) {
	openAIReplayOOMRepairOnce.Do(func() {
		openAIReplayOOMRepairCached, openAIReplayOOMRepairLoadErr =
			readOpenAIReplayOOMRepairTransition()
	})
	return openAIReplayOOMRepairCached, openAIReplayOOMRepairLoadErr
}

func readOpenAIReplayOOMRepairTransition() (openAIReplayOOMRepairReceipt, error) {
	var receipt openAIReplayOOMRepairReceipt
	raw, err := os.ReadFile(codex01491TerminalRepoPath(openAIReplayOOMRepairTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("OpenAI replay OOM 修复 transition 尾部存在额外 JSON")
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
		return receipt, errors.New("OpenAI replay OOM 修复 transition 自摘要不一致")
	}
	if err := validateOpenAIReplayOOMRepairTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateOpenAIReplayOOMRepairTransition(receipt openAIReplayOOMRepairReceipt) error {
	if receipt.SchemaVersion != "sub2apiplus-openai-replay-oom-repair-source-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-30T07:53:26Z" ||
		receipt.BaseCommit != "b7541fbaaed38c1f64ab65b2c12b5e8c561fa5e9" ||
		receipt.Scope != "openai-replay-oom-repair" ||
		receipt.Result != "passed_openai_replay_oom_repair" ||
		len(receipt.Transitions) != 11 || len(receipt.Additions) != 2 {
		return errors.New("OpenAI replay OOM 修复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "codex_01491_terminal_state" ||
		receipt.Predecessor.Path != codex01491TerminalStatePath ||
		receipt.Predecessor.SHA256 != "4e068ca39f8c31b8e459d4dfb8b9c365631877d46d671f3df9d9a187fd9ea6fa" {
		return errors.New("OpenAI replay OOM 修复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(codex01491TerminalRepoPath(receipt.Predecessor.Path))
	if err != nil || upstreamMergeFrameworkDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("OpenAI replay OOM 修复 transition 前序摘要不一致")
	}
	expectedVerification := []string{
		"go test ./internal/service -run 'TestOpenAIInvalidEncryptedAccountCache|TestBuildOpenAIWSReplayInputState|TestOpenAIGatewayService_Forward_(StoreFalseReusesInvalidEncryptedDigest|HTTPRetryRecovery|WSv2InvalidEncryptedContentRecoversOnce)' -count=1",
		"go test -race ./internal/service -run 'TestOpenAIInvalidEncryptedAccountCache|TestBuildOpenAIWSReplayInputState|TestOpenAIGatewayService_Forward_(StoreFalseReusesInvalidEncryptedDigest|HTTPRetryRecovery|WSv2InvalidEncryptedContentRecoversOnce)' -count=1",
		"go test ./internal/service -run 'TestCodex01491TerminalServiceStateIsFrozen|TestOpenAIReplayOOMRepairSourceTransitionServiceIsFrozen|TestUpstreamV0180SourceTransitionIsFrozen' -count=1",
		"make check-egress-spec",
	}
	if !slices.Equal(receipt.Verification, expectedVerification) {
		return errors.New("OpenAI replay OOM 修复 transition 验证集合非法")
	}
	if receipt.Safety.LiveAccountUsed || receipt.Safety.OnlineAcceptancePerformed ||
		receipt.Safety.ProductionConfigChanged || receipt.Safety.OfficialEgressProfileChanged {
		return errors.New("OpenAI replay OOM 修复 transition 安全边界非法")
	}
	transitionPaths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || !receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("OpenAI replay OOM 修复 transition 条目非法")
		}
		current, readErr := os.ReadFile(codex01491TerminalRepoPath(transition.Path))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!openAIWSCompatibilityGuardRepairSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			)) {
			return errors.New("OpenAI replay OOM 修复 transition 当前摘要不一致：" + transition.Path)
		}
		transitionPaths = append(transitionPaths, transition.Path)
	}
	additionPaths := make([]string, 0, len(receipt.Additions))
	for _, addition := range receipt.Additions {
		if strings.TrimSpace(addition.Path) == "" || !receiptSHA256(addition.SHA256) ||
			strings.TrimSpace(addition.Reason) == "" {
			return errors.New("OpenAI replay OOM 修复 addition 条目非法")
		}
		current, readErr := os.ReadFile(codex01491TerminalRepoPath(addition.Path))
		if readErr != nil || upstreamMergeFrameworkDigest(current) != addition.SHA256 {
			return errors.New("OpenAI replay OOM 修复 addition 当前摘要不一致：" + addition.Path)
		}
		additionPaths = append(additionPaths, addition.Path)
	}
	if !slices.IsSorted(transitionPaths) ||
		len(transitionPaths) != len(slices.Compact(append([]string(nil), transitionPaths...))) ||
		!slices.IsSorted(additionPaths) ||
		len(additionPaths) != len(slices.Compact(append([]string(nil), additionPaths...))) {
		return errors.New("OpenAI replay OOM 修复路径未严格排序")
	}
	transitionSet := make(map[string]struct{}, len(transitionPaths))
	for _, path := range transitionPaths {
		transitionSet[path] = struct{}{}
	}
	for _, path := range additionPaths {
		if _, exists := transitionSet[path]; exists {
			return errors.New("OpenAI replay OOM 修复 transition 与 addition 路径重复")
		}
	}
	return nil
}

func openAIReplayOOMRepairSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, err := loadOpenAIReplayOOMRepairTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func TestOpenAIReplayOOMRepairSourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadOpenAIReplayOOMRepairTransition(); err != nil {
		t.Fatal(err)
	}
}

const openAIWSCompatibilityGuardRepairTransitionPath = "docs/egress/maintenance/openai-ws-compatibility-guard-repair-source-transition.json"

type openAIWSCompatibilityGuardRepairReceipt struct {
	SchemaVersion  string                            `json:"schema_version"`
	IssuedAtUTC    string                            `json:"issued_at_utc"`
	BaseCommit     string                            `json:"base_commit"`
	Scope          string                            `json:"scope"`
	Predecessor    openAIReplayOOMRepairPredecessor  `json:"predecessor"`
	Transitions    []openAIReplayOOMRepairTransition `json:"transitions"`
	Verification   []string                          `json:"verification"`
	Safety         openAIReplayOOMRepairSafety       `json:"safety"`
	Result         string                            `json:"result"`
	IdentitySHA256 string                            `json:"identity_sha256"`
}

var (
	openAIWSCompatibilityGuardRepairOnce    sync.Once
	openAIWSCompatibilityGuardRepairCached  openAIWSCompatibilityGuardRepairReceipt
	openAIWSCompatibilityGuardRepairLoadErr error
)

func loadOpenAIWSCompatibilityGuardRepairTransition() (
	openAIWSCompatibilityGuardRepairReceipt,
	error,
) {
	openAIWSCompatibilityGuardRepairOnce.Do(func() {
		openAIWSCompatibilityGuardRepairCached,
			openAIWSCompatibilityGuardRepairLoadErr =
			readOpenAIWSCompatibilityGuardRepairTransition()
	})
	return openAIWSCompatibilityGuardRepairCached,
		openAIWSCompatibilityGuardRepairLoadErr
}

func readOpenAIWSCompatibilityGuardRepairTransition() (
	openAIWSCompatibilityGuardRepairReceipt,
	error,
) {
	var receipt openAIWSCompatibilityGuardRepairReceipt
	raw, err := os.ReadFile(codex01491TerminalRepoPath(
		openAIWSCompatibilityGuardRepairTransitionPath,
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
		return receipt, errors.New("OpenAI WS 兼容守卫修复 transition 尾部存在额外 JSON")
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
		return receipt, errors.New("OpenAI WS 兼容守卫修复 transition 自摘要不一致")
	}
	if err := validateOpenAIWSCompatibilityGuardRepairTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateOpenAIWSCompatibilityGuardRepairTransition(
	receipt openAIWSCompatibilityGuardRepairReceipt,
) error {
	if receipt.SchemaVersion != "sub2apiplus-openai-ws-compatibility-guard-repair-source-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-30T08:50:00Z" ||
		receipt.BaseCommit != "d5c1aa1ce7d2808c6ccdfd484672444629d4fd72" ||
		receipt.Scope != "openai-ws-compatibility-guard-repair" ||
		receipt.Result != "passed_openai_ws_compatibility_guard_repair" ||
		len(receipt.Transitions) != 5 {
		return errors.New("OpenAI WS 兼容守卫修复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "openai_replay_oom_repair_source_transition" ||
		receipt.Predecessor.Path != openAIReplayOOMRepairTransitionPath ||
		receipt.Predecessor.SHA256 != "b7ba33123c8a2415c08574f1e8d009b52ad5220c32494a0abffb54c94f2ef62f" {
		return errors.New("OpenAI WS 兼容守卫修复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(codex01491TerminalRepoPath(receipt.Predecessor.Path))
	if err != nil || upstreamMergeFrameworkDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("OpenAI WS 兼容守卫修复 transition 前序摘要不一致")
	}
	expectedVerification := []string{
		"go test ./internal/service -run 'TestOpenAIOfficialEgressWSBusinessGuard|TestOpenAIGatewayService_ProxyResponsesWebSocketFromClient_(OfficialEgressSkipsCodexImageBridge|PassthroughHeadersUsePromptCacheAndTurnState)' -count=1",
		"go test -race ./internal/service -run 'TestOpenAIOfficialEgressWSBusinessGuard|TestOpenAIGatewayService_ProxyResponsesWebSocketFromClient_(OfficialEgressSkipsCodexImageBridge|PassthroughHeadersUsePromptCacheAndTurnState)' -count=1",
		"go test ./internal/service -count=1",
		"make check-egress-spec",
	}
	if !slices.Equal(receipt.Verification, expectedVerification) {
		return errors.New("OpenAI WS 兼容守卫修复 transition 验证集合非法")
	}
	if receipt.Safety.LiveAccountUsed || receipt.Safety.OnlineAcceptancePerformed ||
		receipt.Safety.ProductionConfigChanged || receipt.Safety.OfficialEgressProfileChanged {
		return errors.New("OpenAI WS 兼容守卫修复 transition 安全边界非法")
	}
	expectedFrom := map[string]string{
		"backend/internal/officialegress/codex_01491_terminal_state_test.go":   "a3b81880e79d229ef9ecc9e83a66045f6602383101055644016dec1b097a58a3",
		"backend/internal/service/codex_01491_terminal_state_test.go":          "355ee6e796cf98ffa45080aaddf59d6c74a36e52b59fd77ffb6cd99744307864",
		"backend/internal/service/official_egress_openai_ws.go":                "8ccd28fc1fb21d7d2abcd32914b3ac1c25d5b834cd13904121f4d81225f4d363",
		"backend/internal/service/official_egress_openai_ws_test.go":           "ad385312fadcdda6fa6f219e8829c828323d8c3ed80c8c2b7789046ebbc12188",
		"backend/internal/service/openai_ws_forwarder_ingress_session_test.go": "857b6c70620cc34f9acce725c776dce2ebba3cc5f2afeff070e432e1ebac9444",
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if expectedFrom[transition.Path] != transition.FromSHA256 ||
			!receiptSHA256(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("OpenAI WS 兼容守卫修复 transition 条目非法")
		}
		current, readErr := os.ReadFile(codex01491TerminalRepoPath(transition.Path))
		if readErr != nil || upstreamMergeFrameworkDigest(current) != transition.ToSHA256 {
			return errors.New("OpenAI WS 兼容守卫修复 transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) ||
		len(paths) != len(slices.Compact(append([]string(nil), paths...))) ||
		len(expectedFrom) != len(paths) {
		return errors.New("OpenAI WS 兼容守卫修复路径未严格排序")
	}
	return nil
}

func openAIWSCompatibilityGuardRepairSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadOpenAIWSCompatibilityGuardRepairTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	// 两个终态验证器已经由 OOM 收据接管；只允许本收据精确承接该中间摘要。
	predecessor, err := loadOpenAIReplayOOMRepairTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		for _, priorTransition := range predecessor.Transitions {
			if priorTransition.Path == path && priorTransition.FromSHA256 == priorDigest &&
				priorTransition.ToSHA256 == transition.FromSHA256 {
				return true
			}
		}
	}
	return false
}

func TestOpenAIWSCompatibilityGuardRepairSourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadOpenAIWSCompatibilityGuardRepairTransition(); err != nil {
		t.Fatal(err)
	}
}
