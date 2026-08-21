package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"go.uber.org/zap"
)

const (
	claudeEgressActivationFactSchema = "claude-egress-activation-fact/v1"
	claudeEgressActivationPathEnv    = "GATEWAY_CLAUDE_EGRESS_ACTIVATION_FACT_PATH"
)

var claudeEgressActivationIdentityEnv = []struct {
	env   string
	field string
}{
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST", "profile_digest"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST", "release_digest"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_APPROVAL_DIGEST", "approval_digest"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_ID", "image_id"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_REFERENCE", "image_reference"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_SOURCE_TREE_SHA256", "source_tree_sha256"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_BUILD_ID", "build_id"},
	{"GATEWAY_CLAUDE_EGRESS_ACTIVATION_DEPLOYED_VERSION", "deployed_version"},
}

// ClaudeOfficialEgressActivationFact 与 Codex 激活事实完全分离，避免一个 Persona
// 的 selector 或批准摘要被另一个 Persona 借用。
type ClaudeOfficialEgressActivationFact struct {
	SchemaVersion    string `json:"schema_version"`
	Source           string `json:"source"`
	EventType        string `json:"event_type"`
	EventID          string `json:"event_id"`
	ObservedAtUTC    string `json:"observed_at_utc"`
	ProfileMode      string `json:"profile_mode"`
	ClaudeVersion    string `json:"claude_version"`
	ProfileDigest    string `json:"profile_digest"`
	WireDigest       string `json:"wire_digest"`
	ReleaseDigest    string `json:"release_digest"`
	BundleDigest     string `json:"bundle_digest"`
	ApprovalDigest   string `json:"approval_digest"`
	ImageID          string `json:"image_id"`
	ImageReference   string `json:"image_reference"`
	SourceTreeSHA256 string `json:"source_tree_sha256"`
	BuildID          string `json:"build_id"`
	DeployedVersion  string `json:"deployed_version"`
}

func resolveClaudeOfficialEgressActivationFact(
	cfg *config.Config,
	now time.Time,
	lookupEnv func(string) string,
) (ClaudeOfficialEgressActivationFact, error) {
	mode := "legacy"
	if cfg != nil {
		mode = strings.ToLower(strings.TrimSpace(
			cfg.Gateway.ClaudeOfficialClientProfiles.Mode,
		))
	}
	if mode != "legacy" && mode != "active" {
		return ClaudeOfficialEgressActivationFact{}, errors.New("Claude production selector 非法")
	}
	declared := map[string]string{}
	for _, item := range claudeEgressActivationIdentityEnv {
		declared[item.field] = strings.TrimSpace(lookupEnv(item.env))
	}
	fact := ClaudeOfficialEgressActivationFact{
		SchemaVersion: claudeEgressActivationFactSchema,
		Source:        "sub2api-runtime", EventType: "claude_profile_observed",
		ObservedAtUTC: now.UTC().Format(time.RFC3339Nano), ProfileMode: mode,
		ImageID: declared["image_id"], ImageReference: declared["image_reference"],
		SourceTreeSHA256: declared["source_tree_sha256"], BuildID: declared["build_id"],
		DeployedVersion: declared["deployed_version"],
	}
	if mode == "active" {
		runtimeState := processOfficialEgressRuntime.Load()
		if runtimeState == nil || runtimeState.Claude == nil ||
			!runtimeState.Claude.IsProductionActive() {
			return ClaudeOfficialEgressActivationFact{}, errors.New(
				"Claude production selector 已激活但正式 Runtime 未生效",
			)
		}
		fact.ClaudeVersion = officialegress.ClaudeFWGVersion
		fact.ProfileDigest = officialegress.ClaudeFWGProfileDigest
		fact.WireDigest = officialegress.ClaudeFWGWireDigest()
		fact.ReleaseDigest = officialegress.ClaudeFWGReleaseDigest
		fact.BundleDigest = officialegress.ClaudeFWGBundleDigest
		fact.ApprovalDigest = runtimeState.Claude.ProductionApprovalDigest()
		for field, actual := range map[string]string{
			"profile_digest":  fact.ProfileDigest,
			"release_digest":  fact.ReleaseDigest,
			"approval_digest": fact.ApprovalDigest,
		} {
			if declared[field] != "" && declared[field] != actual {
				return ClaudeOfficialEgressActivationFact{}, errors.New(
					"声明的 Claude " + field + " 与运行时解析结果不一致",
				)
			}
		}
	}
	fact.EventID = claudeOfficialEgressActivationEventID(fact)
	return fact, nil
}

func claudeOfficialEgressActivationEventID(fact ClaudeOfficialEgressActivationFact) string {
	fact.EventID = ""
	payload, err := json.Marshal(fact)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// EmitClaudeOfficialEgressActivationFact 只追加 Claude 自己的运行时事实，不改变
// Codex 已有 fact schema、路径、内容或失败语义。
func EmitClaudeOfficialEgressActivationFact(cfg *config.Config) {
	fact, err := resolveClaudeOfficialEgressActivationFact(cfg, time.Now(), os.Getenv)
	if err != nil {
		logger.L().Error("claude_official_egress_activation_fact_unavailable", zap.Error(err))
		return
	}
	logger.L().Info(
		"claude_official_egress_activation_fact",
		zap.String("profile_mode", fact.ProfileMode),
		zap.String("claude_version", fact.ClaudeVersion),
		zap.String("profile_digest", fact.ProfileDigest),
		zap.String("release_digest", fact.ReleaseDigest),
		zap.String("approval_digest", fact.ApprovalDigest),
		zap.String("event_id", fact.EventID),
	)
	path := strings.TrimSpace(os.Getenv(claudeEgressActivationPathEnv))
	if path == "" {
		return
	}
	if err := writeClaudeOfficialEgressActivationFact(path, fact); err != nil {
		logger.L().Error(
			"claude_official_egress_activation_fact_write_failed",
			zap.String("path", path), zap.Error(err),
		)
	}
}

func writeClaudeOfficialEgressActivationFact(
	path string,
	fact ClaudeOfficialEgressActivationFact,
) error {
	if !filepath.IsAbs(path) {
		return errors.New("Claude 画像激活事实输出路径必须是绝对路径")
	}
	payload, err := json.MarshalIndent(fact, "", "  ")
	if err != nil {
		return err
	}
	payload = append(payload, '\n')
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	return os.WriteFile(path, payload, 0o600)
}
