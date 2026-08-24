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
	"testing"
)

const (
	compositeModelProtocolSourceTransitionPath = "docs/egress/maintenance/composite-model-protocol-source-transition.json"
	compositeModelProtocolSourceTransitionSHA  = "acfcd0f6f627d104a993d28bbb80d3ccd63d776372f574b0354c98db8326a65f"
)

type compositeModelProtocolTransitionPredecessor struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type compositeModelProtocolTransitionEntry struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type compositeModelProtocolTransitionSafety struct {
	OAuthWireContractChanged bool `json:"oauth_wire_contract_changed"`
	DatabaseMigrationAdded   bool `json:"database_migration_added"`
	KeeperChanged            bool `json:"keeper_changed"`
	LiveAccountUsed          bool `json:"live_account_used"`
	DeploymentPerformed      bool `json:"deployment_performed"`
}

type compositeModelProtocolTransitionReceipt struct {
	SchemaVersion string                                      `json:"schema_version"`
	IssuedAtUTC   string                                      `json:"issued_at_utc"`
	BaseCommit    string                                      `json:"base_commit"`
	Scope         string                                      `json:"scope"`
	Predecessor   compositeModelProtocolTransitionPredecessor `json:"predecessor"`
	Transitions   []compositeModelProtocolTransitionEntry     `json:"transitions"`
	Verification  []string                                    `json:"verification"`
	Safety        compositeModelProtocolTransitionSafety      `json:"safety"`
	Result        string                                      `json:"result"`
}

func loadCompositeModelProtocolSourceTransition() (
	compositeModelProtocolTransitionReceipt,
	error,
) {
	var receipt compositeModelProtocolTransitionReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(compositeModelProtocolSourceTransitionPath),
	))
	if err != nil {
		return receipt, err
	}
	if upstreamMergeFrameworkDigest(raw) != compositeModelProtocolSourceTransitionSHA {
		return receipt, errors.New("Composite 模型协议 transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Composite 模型协议 transition 尾部存在额外 JSON")
	}
	if err := validateCompositeModelProtocolSourceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateCompositeModelProtocolSourceTransition(
	receipt compositeModelProtocolTransitionReceipt,
) error {
	if receipt.SchemaVersion != "sub2apiplus-composite-model-protocol-source-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-24T04:45:26Z" ||
		receipt.BaseCommit != "3a34b59ba98d303bc575b2e214c200b14752d979" ||
		receipt.Scope != "composite-model-list-protocol-isolation" ||
		receipt.Result != "passed_local_release_gates" || len(receipt.Transitions) != 9 {
		return errors.New("Composite 模型协议 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "upstream_release_ci_repair" ||
		receipt.Predecessor.Path != upstreamV0179ReleaseCIRepairTransitionPath ||
		receipt.Predecessor.SHA256 != upstreamV0179ReleaseCIRepairTransitionSHA {
		return errors.New("Composite 模型协议 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(receipt.Predecessor.Path),
	))
	if err != nil || upstreamMergeFrameworkDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("Composite 模型协议 transition 前序摘要不一致")
	}
	if !slices.Equal(receipt.Verification, []string{
		"go test ./internal/handler -run '^(TestGatewayModels_Composite.*|TestCompositeModelListProtocolForRequest)$' -count=1",
		"go test ./internal/service -run '^TestAccountSupportsCompositeModelListProtocol$' -count=1",
		"go test -tags=unit ./...",
		"EGRESS_SEAL_BASE_REF=3a34b59ba98d303bc575b2e214c200b14752d979 make check-egress-spec-ci",
	}) {
		return errors.New("Composite 模型协议 transition 验证集合非法")
	}
	if receipt.Safety.OAuthWireContractChanged || receipt.Safety.DatabaseMigrationAdded ||
		receipt.Safety.KeeperChanged || receipt.Safety.LiveAccountUsed ||
		receipt.Safety.DeploymentPerformed {
		return errors.New("Composite 模型协议 transition 安全边界非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || !receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Composite 模型协议 transition 条目非法")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		if readErr != nil || upstreamMergeFrameworkDigest(current) != transition.ToSHA256 {
			return errors.New("Composite 模型协议 transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.Equal(paths, []string{
		"README.md",
		"backend/internal/handler/gateway_handler.go",
		"backend/internal/officialegress/upstream_merge_framework_transition_test.go",
		"backend/internal/officialegress/upstream_v0179_release_ci_repair_transition_test.go",
		"backend/internal/service/upstream_merge_framework_transition_test.go",
		"backend/internal/service/upstream_v0179_release_ci_repair_transition_test.go",
		"docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
		"docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
		"docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
	}) {
		return errors.New("Composite 模型协议 transition 路径闭集非法")
	}
	return nil
}

// compositeModelProtocolSourceTransitionSupersedes 只接受本轮收据中的精确
// path/from/to，不能借模型目录变更扩大 OAuth 出站画像或生产选择权。
func compositeModelProtocolSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadCompositeModelProtocolSourceTransition()
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

func TestCompositeModelProtocolSourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadCompositeModelProtocolSourceTransition(); err != nil {
		t.Fatal(err)
	}
}
