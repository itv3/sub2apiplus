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

// 官方出站画像激活事实。
//
// 候选验收要求一份「由运行中服务产生」的画像观测事实：只有服务自己能证明当前发布指针
// 真正解析到了哪个画像。构建期身份（镜像、源码树、build/deploy 编号）服务无从得知，
// 由部署方通过环境变量声明；服务把声明中的 profile digest 与自己实际解析出的 digest
// 逐字比对，不一致就拒绝落盘，声明因此无法凭空成立。
//
// 生产默认只写一条启动日志；只有显式配置 GATEWAY_EGRESS_ACTIVATION_FACT_PATH 时才落盘，
// 所以对正常部署零行为变化。
const (
	officialEgressActivationFactSchema = "codex-egress-activation-fact/v1"
	officialEgressActivationFactSource = "sub2api-runtime"
	officialEgressActivationFactEvent  = "profile_activated"

	officialEgressActivationPathEnv = "GATEWAY_EGRESS_ACTIVATION_FACT_PATH"
)

// officialEgressActivationIdentityEnv 是部署方声明的候选身份。全部为空时只记日志。
var officialEgressActivationIdentityEnv = []struct {
	env   string
	field string
}{
	{"GATEWAY_EGRESS_ACTIVATION_PROFILE_ID", "profile_id"},
	{"GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST", "profile_digest"},
	{"GATEWAY_EGRESS_ACTIVATION_IMAGE_ID", "image_id"},
	{"GATEWAY_EGRESS_ACTIVATION_IMAGE_REFERENCE", "image_reference"},
	{"GATEWAY_EGRESS_ACTIVATION_SOURCE_TREE_SHA256", "source_tree_sha256"},
	{"GATEWAY_EGRESS_ACTIVATION_BUILD_ID", "build_id"},
	{"GATEWAY_EGRESS_ACTIVATION_DEPLOYED_VERSION", "deployed_version"},
}

// OfficialEgressActivationFact 是启动时刻的画像激活事实。
type OfficialEgressActivationFact struct {
	SchemaVersion    string `json:"schema_version"`
	Source           string `json:"source"`
	EventType        string `json:"event_type"`
	EventID          string `json:"event_id"`
	ObservedAtUTC    string `json:"observed_at_utc"`
	ProfileMode      string `json:"profile_mode"`
	CodexVersion     string `json:"codex_version"`
	ProfileID        string `json:"profile_id"`
	ProfileDigest    string `json:"profile_digest"`
	ReleaseDigest    string `json:"release_digest"`
	ImageID          string `json:"image_id"`
	ImageReference   string `json:"image_reference"`
	SourceTreeSHA256 string `json:"source_tree_sha256"`
	BuildID          string `json:"build_id"`
	DeployedVersion  string `json:"deployed_version"`
}

// resolveOfficialEgressActivationFact 只读地解析当前发布指针，不产生副作用。
func resolveOfficialEgressActivationFact(
	cfg *config.Config,
	now time.Time,
	lookupEnv func(string) string,
) (OfficialEgressActivationFact, error) {
	mode := officialClientProfileModeFromConfig(cfg)
	release, err := officialegress.DefaultPersonaReleaseCatalog().ResolveCodexMode(
		officialegress.PersonaCodexCLI,
		officialegress.ReleaseMode(mode),
	)
	if err != nil {
		return OfficialEgressActivationFact{}, err
	}
	declared := map[string]string{}
	for _, item := range officialEgressActivationIdentityEnv {
		declared[item.field] = strings.TrimSpace(lookupEnv(item.env))
	}
	// 声明的 digest 必须与服务实际解析结果一致，否则事实不成立。
	if digest := declared["profile_digest"]; digest != "" && digest != release.ProfileDigest() {
		return OfficialEgressActivationFact{}, errors.New(
			"声明的 profile digest 与运行时解析结果不一致",
		)
	}
	fact := OfficialEgressActivationFact{
		SchemaVersion:    officialEgressActivationFactSchema,
		Source:           officialEgressActivationFactSource,
		EventType:        officialEgressActivationFactEvent,
		ObservedAtUTC:    now.UTC().Format(time.RFC3339Nano),
		ProfileMode:      mode,
		CodexVersion:     release.Version(),
		ProfileID:        declared["profile_id"],
		ProfileDigest:    release.ProfileDigest(),
		ReleaseDigest:    release.ReleaseDigest(),
		ImageID:          declared["image_id"],
		ImageReference:   declared["image_reference"],
		SourceTreeSHA256: declared["source_tree_sha256"],
		BuildID:          declared["build_id"],
		DeployedVersion:  declared["deployed_version"],
	}
	fact.EventID = officialEgressActivationEventID(fact)
	return fact, nil
}

// officialEgressActivationEventID 用内容寻址取代随机数，同一事实可重复复算。
func officialEgressActivationEventID(fact OfficialEgressActivationFact) string {
	fact.EventID = ""
	payload, err := json.Marshal(fact)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// EmitOfficialEgressActivationFact 在启动装配完成后记录画像激活事实。
// 解析失败或落盘失败都不阻断启动：网关其余能力与该事实无关。
func EmitOfficialEgressActivationFact(cfg *config.Config) {
	fact, err := resolveOfficialEgressActivationFact(cfg, time.Now(), os.Getenv)
	if err != nil {
		logger.L().Error("official_egress_activation_fact_unavailable", zap.Error(err))
		return
	}
	logger.L().Info(
		"official_egress_activation_fact",
		zap.String("profile_mode", fact.ProfileMode),
		zap.String("codex_version", fact.CodexVersion),
		zap.String("profile_digest", fact.ProfileDigest),
		zap.String("release_digest", fact.ReleaseDigest),
		zap.String("event_id", fact.EventID),
	)
	path := strings.TrimSpace(os.Getenv(officialEgressActivationPathEnv))
	if path == "" {
		return
	}
	if err := writeOfficialEgressActivationFact(path, fact); err != nil {
		logger.L().Error(
			"official_egress_activation_fact_write_failed",
			zap.String("path", path),
			zap.Error(err),
		)
	}
}

func writeOfficialEgressActivationFact(path string, fact OfficialEgressActivationFact) error {
	if !filepath.IsAbs(path) {
		return errors.New("画像激活事实输出路径必须是绝对路径")
	}
	payload, err := json.MarshalIndent(fact, "", "  ")
	if err != nil {
		return err
	}
	payload = append(payload, '\n')
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil { //nolint:gosec // path 已验证为部署者显式配置的绝对路径。
		return err
	}
	return os.WriteFile(path, payload, 0o600) //nolint:gosec // 激活事实只写入上述受信部署路径。
}
