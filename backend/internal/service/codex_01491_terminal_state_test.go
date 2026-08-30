package service

import (
	"bytes"
	"encoding/hex"
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

const codex01491TerminalStateServicePath = "docs/egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json"

type codex01491TerminalServiceTransition struct {
	Path          string   `json:"path"`
	State         string   `json:"state"`
	PriorSHA256s  []string `json:"prior_sha256s"`
	CurrentSHA256 string   `json:"current_sha256"`
}

type codex01491TerminalServiceReceipt struct {
	SchemaVersion string                                `json:"schema_version"`
	Transitions   []codex01491TerminalServiceTransition `json:"transitions"`
	Result        string                                `json:"result"`
}

var (
	codex01491TerminalServiceOnce    sync.Once
	codex01491TerminalServiceCached  codex01491TerminalServiceReceipt
	codex01491TerminalServiceLoadErr error
)

func loadCodex01491TerminalServiceState() (codex01491TerminalServiceReceipt, error) {
	codex01491TerminalServiceOnce.Do(func() {
		raw, err := os.ReadFile(filepath.Join("../../..", codex01491TerminalStateServicePath))
		if err != nil {
			codex01491TerminalServiceLoadErr = err
			return
		}
		if err := json.Unmarshal(raw, &codex01491TerminalServiceCached); err != nil {
			codex01491TerminalServiceLoadErr = err
			return
		}
		if codex01491TerminalServiceCached.SchemaVersion !=
			"official-client-codex-0.149.1-terminal-state/v1" ||
			codex01491TerminalServiceCached.Result != "passed" {
			codex01491TerminalServiceLoadErr = errors.New("0.149.1 终态收据顶层事实非法")
			return
		}
		for _, transition := range codex01491TerminalServiceCached.Transitions {
			current, readErr := os.ReadFile(filepath.Join(
				"../../..", filepath.FromSlash(transition.Path),
			))
			switch transition.State {
			case "present":
				currentDigest := upstreamMergeFrameworkServiceDigest(current)
				if readErr != nil || (currentDigest != transition.CurrentSHA256 &&
					!openAIReplayOOMRepairSupersedesService(
						transition.Path,
						transition.CurrentSHA256,
						currentDigest,
					) && !openAIWSCompatibilityGuardRepairSupersedesService(
					transition.Path,
					transition.CurrentSHA256,
					currentDigest,
				)) {
					codex01491TerminalServiceLoadErr = errors.New(
						"0.149.1 终态当前摘要不一致：" + transition.Path,
					)
					return
				}
			case "deleted":
				if !os.IsNotExist(readErr) || transition.CurrentSHA256 != "" {
					codex01491TerminalServiceLoadErr = errors.New(
						"0.149.1 终态删除状态不一致：" + transition.Path,
					)
					return
				}
			default:
				codex01491TerminalServiceLoadErr = errors.New("0.149.1 终态文件状态非法")
				return
			}
		}
	})
	return codex01491TerminalServiceCached, codex01491TerminalServiceLoadErr
}

func codex01491TerminalStateSupersedesService(path, priorDigest, currentDigest string) bool {
	if openAIReplayOOMRepairSupersedesService(path, priorDigest, currentDigest) ||
		openAIWSCompatibilityGuardRepairSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadCodex01491TerminalServiceState()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && slices.Contains(transition.PriorSHA256s, priorDigest) &&
			(transition.CurrentSHA256 == currentDigest ||
				openAIReplayOOMRepairSupersedesService(
					path,
					transition.CurrentSHA256,
					currentDigest,
				) || openAIWSCompatibilityGuardRepairSupersedesService(
				path,
				transition.CurrentSHA256,
				currentDigest,
			)) {
			return true
		}
	}
	return false
}

func TestCodex01491TerminalServiceStateIsFrozen(t *testing.T) {
	if _, err := loadCodex01491TerminalServiceState(); err != nil {
		t.Fatal(err)
	}
}

const openAIReplayOOMRepairTransitionServicePath = "docs/egress/maintenance/openai-replay-oom-repair-source-transition.json"

type openAIReplayOOMRepairPredecessorService struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type openAIReplayOOMRepairTransitionService struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type openAIReplayOOMRepairAdditionService struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Reason string `json:"reason"`
}

type openAIReplayOOMRepairSafetyService struct {
	LiveAccountUsed              bool `json:"live_account_used"`
	OnlineAcceptancePerformed    bool `json:"online_acceptance_performed"`
	ProductionConfigChanged      bool `json:"production_config_changed"`
	OfficialEgressProfileChanged bool `json:"official_egress_profile_changed"`
}

type openAIReplayOOMRepairReceiptService struct {
	SchemaVersion  string                                   `json:"schema_version"`
	IssuedAtUTC    string                                   `json:"issued_at_utc"`
	BaseCommit     string                                   `json:"base_commit"`
	Scope          string                                   `json:"scope"`
	Predecessor    openAIReplayOOMRepairPredecessorService  `json:"predecessor"`
	Transitions    []openAIReplayOOMRepairTransitionService `json:"transitions"`
	Additions      []openAIReplayOOMRepairAdditionService   `json:"additions"`
	Verification   []string                                 `json:"verification"`
	Safety         openAIReplayOOMRepairSafetyService       `json:"safety"`
	Result         string                                   `json:"result"`
	IdentitySHA256 string                                   `json:"identity_sha256"`
}

var (
	openAIReplayOOMRepairServiceOnce    sync.Once
	openAIReplayOOMRepairServiceCached  openAIReplayOOMRepairReceiptService
	openAIReplayOOMRepairServiceLoadErr error
)

func loadOpenAIReplayOOMRepairTransitionService() (
	openAIReplayOOMRepairReceiptService,
	error,
) {
	openAIReplayOOMRepairServiceOnce.Do(func() {
		openAIReplayOOMRepairServiceCached, openAIReplayOOMRepairServiceLoadErr =
			readOpenAIReplayOOMRepairTransitionService()
	})
	return openAIReplayOOMRepairServiceCached, openAIReplayOOMRepairServiceLoadErr
}

func readOpenAIReplayOOMRepairTransitionService() (
	openAIReplayOOMRepairReceiptService,
	error,
) {
	var receipt openAIReplayOOMRepairReceiptService
	raw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(openAIReplayOOMRepairTransitionServicePath),
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
	if upstreamMergeFrameworkServiceDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("OpenAI replay OOM 修复 transition 自摘要不一致")
	}
	if err := validateOpenAIReplayOOMRepairTransitionService(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validOpenAIReplayOOMRepairServiceSHA(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validateOpenAIReplayOOMRepairTransitionService(
	receipt openAIReplayOOMRepairReceiptService,
) error {
	if receipt.SchemaVersion != "sub2apiplus-openai-replay-oom-repair-source-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-30T07:53:26Z" ||
		receipt.BaseCommit != "b7541fbaaed38c1f64ab65b2c12b5e8c561fa5e9" ||
		receipt.Scope != "openai-replay-oom-repair" ||
		receipt.Result != "passed_openai_replay_oom_repair" ||
		len(receipt.Transitions) != 11 || len(receipt.Additions) != 2 {
		return errors.New("OpenAI replay OOM 修复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "codex_01491_terminal_state" ||
		receipt.Predecessor.Path != codex01491TerminalStateServicePath ||
		receipt.Predecessor.SHA256 != "4e068ca39f8c31b8e459d4dfb8b9c365631877d46d671f3df9d9a187fd9ea6fa" {
		return errors.New("OpenAI replay OOM 修复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(receipt.Predecessor.Path),
	))
	if err != nil || upstreamMergeFrameworkServiceDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
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
		if strings.TrimSpace(transition.Path) == "" ||
			!validOpenAIReplayOOMRepairServiceSHA(transition.FromSHA256) ||
			!validOpenAIReplayOOMRepairServiceSHA(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 || strings.TrimSpace(transition.Reason) == "" {
			return errors.New("OpenAI replay OOM 修复 transition 条目非法")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(transition.Path),
		))
		currentDigest := upstreamMergeFrameworkServiceDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!openAIWSCompatibilityGuardRepairSupersedesService(
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
		if strings.TrimSpace(addition.Path) == "" ||
			!validOpenAIReplayOOMRepairServiceSHA(addition.SHA256) ||
			strings.TrimSpace(addition.Reason) == "" {
			return errors.New("OpenAI replay OOM 修复 addition 条目非法")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(addition.Path),
		))
		if readErr != nil || upstreamMergeFrameworkServiceDigest(current) != addition.SHA256 {
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

func openAIReplayOOMRepairSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadOpenAIReplayOOMRepairTransitionService()
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

func TestOpenAIReplayOOMRepairSourceTransitionServiceIsFrozen(t *testing.T) {
	if _, err := loadOpenAIReplayOOMRepairTransitionService(); err != nil {
		t.Fatal(err)
	}
}

const openAIWSCompatibilityGuardRepairTransitionServicePath = "docs/egress/maintenance/openai-ws-compatibility-guard-repair-source-transition.json"

type openAIWSCompatibilityGuardRepairReceiptService struct {
	SchemaVersion  string                                   `json:"schema_version"`
	IssuedAtUTC    string                                   `json:"issued_at_utc"`
	BaseCommit     string                                   `json:"base_commit"`
	Scope          string                                   `json:"scope"`
	Predecessor    openAIReplayOOMRepairPredecessorService  `json:"predecessor"`
	Transitions    []openAIReplayOOMRepairTransitionService `json:"transitions"`
	Verification   []string                                 `json:"verification"`
	Safety         openAIReplayOOMRepairSafetyService       `json:"safety"`
	Result         string                                   `json:"result"`
	IdentitySHA256 string                                   `json:"identity_sha256"`
}

var (
	openAIWSCompatibilityGuardRepairServiceOnce    sync.Once
	openAIWSCompatibilityGuardRepairServiceCached  openAIWSCompatibilityGuardRepairReceiptService
	openAIWSCompatibilityGuardRepairServiceLoadErr error
)

func loadOpenAIWSCompatibilityGuardRepairTransitionService() (
	openAIWSCompatibilityGuardRepairReceiptService,
	error,
) {
	openAIWSCompatibilityGuardRepairServiceOnce.Do(func() {
		openAIWSCompatibilityGuardRepairServiceCached,
			openAIWSCompatibilityGuardRepairServiceLoadErr =
			readOpenAIWSCompatibilityGuardRepairTransitionService()
	})
	return openAIWSCompatibilityGuardRepairServiceCached,
		openAIWSCompatibilityGuardRepairServiceLoadErr
}

func readOpenAIWSCompatibilityGuardRepairTransitionService() (
	openAIWSCompatibilityGuardRepairReceiptService,
	error,
) {
	var receipt openAIWSCompatibilityGuardRepairReceiptService
	raw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(openAIWSCompatibilityGuardRepairTransitionServicePath),
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
	if upstreamMergeFrameworkServiceDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("OpenAI WS 兼容守卫修复 transition 自摘要不一致")
	}
	if err := validateOpenAIWSCompatibilityGuardRepairTransitionService(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateOpenAIWSCompatibilityGuardRepairTransitionService(
	receipt openAIWSCompatibilityGuardRepairReceiptService,
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
		receipt.Predecessor.Path != openAIReplayOOMRepairTransitionServicePath ||
		receipt.Predecessor.SHA256 != "b7ba33123c8a2415c08574f1e8d009b52ad5220c32494a0abffb54c94f2ef62f" {
		return errors.New("OpenAI WS 兼容守卫修复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..",
		filepath.FromSlash(receipt.Predecessor.Path),
	))
	if err != nil || upstreamMergeFrameworkServiceDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
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
			!validOpenAIReplayOOMRepairServiceSHA(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("OpenAI WS 兼容守卫修复 transition 条目非法")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..",
			filepath.FromSlash(transition.Path),
		))
		if readErr != nil || upstreamMergeFrameworkServiceDigest(current) != transition.ToSHA256 {
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

func openAIWSCompatibilityGuardRepairSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadOpenAIWSCompatibilityGuardRepairTransitionService()
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
	predecessor, err := loadOpenAIReplayOOMRepairTransitionService()
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

func TestOpenAIWSCompatibilityGuardRepairSourceTransitionServiceIsFrozen(t *testing.T) {
	if _, err := loadOpenAIWSCompatibilityGuardRepairTransitionService(); err != nil {
		t.Fatal(err)
	}
}
