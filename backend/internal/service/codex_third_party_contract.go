package service

// codex_third_party_contract.go 定义「Codex 三方模型启用」功能两路开发共用的契约：
//
//   - 分组展示配置（哪些第三方模型在 Codex 选择器中启用）的存储格式、读取与原子写入接口；
//   - 账号级完整模型 ID 源快照的存储格式与读写；
//   - 目录评估结果（候选、状态、预算）的数据结构与评估接口。
//
// 本文件只放类型、常量和不依赖业务编排的小函数。目录编排、能力档案和入口接入在
// 其它 codex_third_party_*.go 文件实现；管理接口与前端只依赖这里的类型和接口。
// 所有键名与 JSON 字段使用英文，注释与对外说明使用中文。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const (
	// codexModelVisibilitySettingKeyPrefix 是分组展示配置的 settings 键前缀，完整键为前缀加分组 ID。
	// 公开 slug 放在 JSON 内，避免超过 settings 键 100 字符的长度限制。
	codexModelVisibilitySettingKeyPrefix = "plus_codex_model_visibility:"
	// codexCatalogSourceSettingKeyPrefix 是账号级完整模型 ID 源快照的 settings 键前缀，完整键为前缀加账号 ID。
	codexCatalogSourceSettingKeyPrefix = "plus_codex_catalog_source:"

	// CodexModelVisibilitySchemaVersion 是分组展示配置当前的结构版本。
	CodexModelVisibilitySchemaVersion = 1
	// CodexCatalogSourceSchemaVersion 是源快照当前的结构版本。
	CodexCatalogSourceSchemaVersion = 1

	// CodexCatalogMaxBytes 是 Codex 客户端在线目录响应体的硬上限（1 MiB），超过即被客户端整体拒绝。
	CodexCatalogMaxBytes = 1 << 20
	// CodexCatalogBaseFetchBudget 是目录请求中获取 OpenAI 基础目录的时限。客户端对整次目录请求有
	// 5 秒总超时，超时即清空目录，这里留出编排与写出的余量。
	CodexCatalogBaseFetchBudget = 3 * time.Second
	// CodexCatalogSourceStaleAfter 是源快照的刷新阈值。只表示需要刷新，不是自动停发期限。
	CodexCatalogSourceStaleAfter = 6 * time.Hour

	// codexModelVisibilityMaxModels 是单个分组配置可登记的公开模型数上限，防止异常请求写入超大配置。
	codexModelVisibilityMaxModels = 512
	// codexModelSlugMaxLength 是公开 slug 的最大长度。
	codexModelSlugMaxLength = 256
)

var (
	// ErrCodexModelVisibilityConflict 表示保存时配置 revision 已被他人修改，页面应重新加载后重试（409）。
	ErrCodexModelVisibilityConflict = infraerrors.Conflict("CODEX_MODEL_VISIBILITY_CONFLICT", "Codex 三方模型配置已被修改，请刷新后重试")
	// ErrCodexCatalogPreviewStale 表示开启操作携带的预览指纹与当前输入不一致，须重新预览（409）。
	ErrCodexCatalogPreviewStale = infraerrors.Conflict("CODEX_CATALOG_PREVIEW_STALE", "候选或能力输入已变化，请重新预览后再保存")
	// ErrCodexModelVisibilityInvalid 表示配置内容不合法（400）。
	ErrCodexModelVisibilityInvalid = infraerrors.BadRequest("CODEX_MODEL_VISIBILITY_INVALID", "Codex 三方模型配置不合法")
	// ErrCodexCatalogUnavailable 表示基础目录不可得且没有可发布的原生 GPT 条目，目录请求只能返回错误（503）。
	// 第三方阶段的任何失败都按降级处理，不使用这个错误。
	ErrCodexCatalogUnavailable = infraerrors.ServiceUnavailable("CODEX_CATALOG_UNAVAILABLE", "Codex 模型目录暂不可用")
)

// CodexModelVisibilitySettingKey 返回分组展示配置的 settings 键。
func CodexModelVisibilitySettingKey(groupID int64) string {
	return fmt.Sprintf("%s%d", codexModelVisibilitySettingKeyPrefix, groupID)
}

// CodexCatalogSourceSettingKey 返回账号源快照的 settings 键。
func CodexCatalogSourceSettingKey(accountID int64) string {
	return fmt.Sprintf("%s%d", codexCatalogSourceSettingKeyPrefix, accountID)
}

// CodexCatalogPlatformEligible 判断分组平台是否提供 Codex 在线目录：Composite 与所有走 OpenAI 网关的平台。
// Anthropic、Gemini、Antigravity 等分组仍按普通模型列表处理。
func CodexCatalogPlatformEligible(platform string) bool {
	platform = strings.TrimSpace(platform)
	if platform == PlatformComposite {
		return true
	}
	return domain.UsesOpenAIGateway(platform)
}

// ---------------------------------------------------------------------------
// 分组展示配置
// ---------------------------------------------------------------------------

// CodexModelVisibilityConfig 是一个分组的第三方模型展示配置。
// 未列出的第三方模型一律视为关闭；原生 GPT 不受此配置控制。
type CodexModelVisibilityConfig struct {
	SchemaVersion int                                  `json:"schema_version"`
	Revision      int64                                `json:"revision"`
	Models        map[string]CodexModelVisibilityEntry `json:"models"`
}

// CodexModelVisibilityEntry 是单个公开模型的展示配置。
type CodexModelVisibilityEntry struct {
	// Enabled 表示管理员意图：在 Codex 中启用该模型。是否真正发布还取决于发布条件。
	Enabled bool `json:"enabled"`
	// Verification 记录管理员确认真实调用验收通过时的指纹。指纹与当前评估指纹一致时状态才显示为已验证。
	Verification *CodexModelVerification `json:"verification,omitempty"`
}

// CodexModelVerification 是一次真实调用验收的记录。
type CodexModelVerification struct {
	Fingerprint string    `json:"fingerprint"`
	VerifiedAt  time.Time `json:"verified_at"`
	Note        string    `json:"note,omitempty"`
}

// NewCodexModelVisibilityConfig 返回空配置（revision 为 0，表示尚未保存）。
func NewCodexModelVisibilityConfig() CodexModelVisibilityConfig {
	return CodexModelVisibilityConfig{
		SchemaVersion: CodexModelVisibilitySchemaVersion,
		Models:        map[string]CodexModelVisibilityEntry{},
	}
}

// ParseCodexModelVisibilityConfig 严格解析已保存的配置。结构版本未知或内容不合法时返回错误，
// 调用方不得把解析失败当作「成功但为空」。
func ParseCodexModelVisibilityConfig(raw string) (CodexModelVisibilityConfig, error) {
	cfg := NewCodexModelVisibilityConfig()
	if strings.TrimSpace(raw) == "" {
		return cfg, fmt.Errorf("codex model visibility: empty value")
	}
	if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
		return NewCodexModelVisibilityConfig(), fmt.Errorf("codex model visibility: decode: %w", err)
	}
	if cfg.Models == nil {
		cfg.Models = map[string]CodexModelVisibilityEntry{}
	}
	if err := cfg.Validate(); err != nil {
		return NewCodexModelVisibilityConfig(), err
	}
	return cfg, nil
}

// Validate 校验配置结构。slug 必须是去空白后的非空具体名称，不允许通配符。
func (c CodexModelVisibilityConfig) Validate() error {
	if c.SchemaVersion != CodexModelVisibilitySchemaVersion {
		return fmt.Errorf("codex model visibility: unsupported schema_version %d", c.SchemaVersion)
	}
	if c.Revision < 0 {
		return fmt.Errorf("codex model visibility: negative revision")
	}
	if len(c.Models) > codexModelVisibilityMaxModels {
		return fmt.Errorf("codex model visibility: too many models (%d > %d)", len(c.Models), codexModelVisibilityMaxModels)
	}
	for slug, entry := range c.Models {
		if err := validateCodexModelSlug(slug); err != nil {
			return err
		}
		if entry.Verification != nil && strings.TrimSpace(entry.Verification.Fingerprint) == "" {
			return fmt.Errorf("codex model visibility: empty verification fingerprint for %q", slug)
		}
	}
	return nil
}

func validateCodexModelSlug(slug string) error {
	if slug == "" || strings.TrimSpace(slug) != slug {
		return fmt.Errorf("codex model visibility: invalid slug %q", slug)
	}
	if len(slug) > codexModelSlugMaxLength {
		return fmt.Errorf("codex model visibility: slug too long %q", slug)
	}
	if strings.ContainsAny(slug, "*\n\r\t") {
		return fmt.Errorf("codex model visibility: slug must be a concrete model name %q", slug)
	}
	return nil
}

// IsEnabled 返回管理员是否启用了该公开模型；未列出视为关闭。
func (c CodexModelVisibilityConfig) IsEnabled(slug string) bool {
	entry, ok := c.Models[slug]
	return ok && entry.Enabled
}

// Clone 返回深拷贝，供预览时在副本上叠加变更。
func (c CodexModelVisibilityConfig) Clone() CodexModelVisibilityConfig {
	out := CodexModelVisibilityConfig{
		SchemaVersion: c.SchemaVersion,
		Revision:      c.Revision,
		Models:        make(map[string]CodexModelVisibilityEntry, len(c.Models)),
	}
	for slug, entry := range c.Models {
		if entry.Verification != nil {
			v := *entry.Verification
			entry.Verification = &v
		}
		out.Models[slug] = entry
	}
	return out
}

// Marshal 序列化为 settings 值。Go 对 map 键排序，输出稳定。
func (c CodexModelVisibilityConfig) Marshal() (string, error) {
	if c.Models == nil {
		c.Models = map[string]CodexModelVisibilityEntry{}
	}
	if err := c.Validate(); err != nil {
		return "", err
	}
	raw, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// LoadCodexModelVisibility 读取分组展示配置。
// 返回 found=false 表示配置不存在（全部第三方模型关闭）；返回 error 表示读取失败或配置损坏，
// 目录读路径据此降级为只发布原生 GPT，不得当作空配置继续。
func (s *SettingService) LoadCodexModelVisibility(ctx context.Context, groupID int64) (CodexModelVisibilityConfig, bool, error) {
	if s == nil || s.settingRepo == nil {
		return NewCodexModelVisibilityConfig(), false, fmt.Errorf("codex model visibility: setting repository unavailable")
	}
	raw, err := s.settingRepo.GetValue(ctx, CodexModelVisibilitySettingKey(groupID))
	if err != nil {
		if errors.Is(err, ErrSettingNotFound) {
			return NewCodexModelVisibilityConfig(), false, nil
		}
		return NewCodexModelVisibilityConfig(), false, err
	}
	cfg, err := ParseCodexModelVisibilityConfig(raw)
	if err != nil {
		return NewCodexModelVisibilityConfig(), true, err
	}
	return cfg, true, nil
}

// CodexModelVisibilityStore 是展示配置的原子写入接口，由 repository 层用数据库事务实现。
// 不能用无条件 upsert 加进程锁代替，多节点部署下必须依赖数据库行锁与 revision 比较。
type CodexModelVisibilityStore interface {
	// CompareAndSwapCodexModelVisibility 在事务内比较当前 revision 与 expectedRevision，相等时写入 next，
	// 并把 revision 设为 expectedRevision+1 后返回写入结果。expectedRevision 为 0 表示期望配置尚不存在。
	// revision 不匹配（含并发首次创建）返回 ErrCodexModelVisibilityConflict。
	CompareAndSwapCodexModelVisibility(ctx context.Context, groupID int64, expectedRevision int64, next CodexModelVisibilityConfig) (CodexModelVisibilityConfig, error)
	// DeleteCodexModelVisibility 删除分组配置；不存在时视为成功。用于惰性清理已删除分组的孤儿配置。
	DeleteCodexModelVisibility(ctx context.Context, groupID int64) error
}

// ---------------------------------------------------------------------------
// 账号源快照
// ---------------------------------------------------------------------------

// CodexCatalogSourceStatus 区分源列表的同步结果，避免把失败或不完整当成真实空列表。
type CodexCatalogSourceStatus string

const (
	// CodexCatalogSourceComplete 表示最近一次同步拿到了完整列表。
	CodexCatalogSourceComplete CodexCatalogSourceStatus = "complete"
	// CodexCatalogSourceEmpty 表示上游明确返回空列表。
	CodexCatalogSourceEmpty CodexCatalogSourceStatus = "empty"
	// CodexCatalogSourceFailed 表示最近一次同步失败；Models 保留上一份完整清单（可能为空），并标记为过期。
	CodexCatalogSourceFailed CodexCatalogSourceStatus = "failed"
)

// CodexCatalogSourceSnapshot 是单个账号的完整上游模型 ID 清单。它与账号 Extra 中的能力快照
// upstream_model_metadata 分开保存：能力不完整的型号也必须保留名称。
type CodexCatalogSourceSnapshot struct {
	SchemaVersion int                      `json:"schema_version"`
	AccountID     int64                    `json:"account_id"`
	Models        []string                 `json:"models"`
	Source        string                   `json:"source"`
	Status        CodexCatalogSourceStatus `json:"status"`
	// SourceFingerprint 是同步时账号源配置的指纹，见 CodexCatalogSourceFingerprint。
	SourceFingerprint string `json:"source_fingerprint"`
	// SyncedAt 是最近一次拿到完整列表（含真实空列表）的时间。
	SyncedAt time.Time `json:"synced_at"`
	// AttemptedAt 是最近一次尝试同步的时间。
	AttemptedAt time.Time `json:"attempted_at"`
	// LastError 是最近一次失败原因，不得包含凭据或上游原始响应正文。
	LastError string `json:"last_error,omitempty"`
}

// CodexCatalogSourceUpstreamSync 是由现有账号上游同步得到的源。
const CodexCatalogSourceUpstreamSync = "upstream_sync"

// Matches 判断快照是否仍对应账号当前的源配置。指纹不一致的快照不得参与发现。
func (s CodexCatalogSourceSnapshot) Matches(account *Account) bool {
	return account != nil && s.AccountID == account.ID && s.SourceFingerprint != "" &&
		s.SourceFingerprint == CodexCatalogSourceFingerprint(account)
}

// Stale 判断快照是否需要刷新：从未成功、最近一次失败或超过刷新阈值。
func (s CodexCatalogSourceSnapshot) Stale(now time.Time) bool {
	if s.SyncedAt.IsZero() || s.Status == CodexCatalogSourceFailed {
		return true
	}
	return now.Sub(s.SyncedAt) > CodexCatalogSourceStaleAfter
}

// CodexCatalogSourceFingerprint 计算账号源配置的指纹。只纳入决定「上游提供哪些模型」的稳定字段；
// 凭据只以摘要形式参与，OAuth 访问令牌等会周期刷新的字段不纳入，避免指纹频繁失效。
func CodexCatalogSourceFingerprint(account *Account) string {
	if account == nil {
		return ""
	}
	type fingerprintInput struct {
		Platform     string            `json:"platform"`
		Type         string            `json:"type"`
		BaseURL      string            `json:"base_url"`
		APIProtocol  string            `json:"api_protocol"`
		AccountMode  string            `json:"account_mode"`
		APIKeyDigest string            `json:"api_key_digest"`
		ModelMapping map[string]string `json:"model_mapping"`
	}
	input := fingerprintInput{
		Platform:     account.Platform,
		Type:         account.Type,
		BaseURL:      strings.TrimSpace(account.GetCredential("base_url")),
		APIProtocol:  strings.TrimSpace(account.GetCredential("api_protocol")),
		AccountMode:  strings.TrimSpace(account.GetCredential("account_mode")),
		ModelMapping: account.GetModelMapping(),
	}
	if key := strings.TrimSpace(account.GetCredential("api_key")); key != "" {
		sum := sha256.Sum256([]byte(key))
		input.APIKeyDigest = hex.EncodeToString(sum[:])
	}
	raw, err := json.Marshal(input)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// NormalizeCodexCatalogSourceModels 去空白、去重、去掉通配键并排序，得到可直接保存的 ID 清单。
func NormalizeCodexCatalogSourceModels(models []string) []string {
	seen := make(map[string]struct{}, len(models))
	out := make([]string, 0, len(models))
	for _, id := range models {
		id = strings.TrimSpace(id)
		if id == "" || strings.Contains(id, "*") {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// LoadCodexCatalogSource 读取账号源快照；found=false 表示从未保存。
func (s *SettingService) LoadCodexCatalogSource(ctx context.Context, accountID int64) (CodexCatalogSourceSnapshot, bool, error) {
	if s == nil || s.settingRepo == nil {
		return CodexCatalogSourceSnapshot{}, false, fmt.Errorf("codex catalog source: setting repository unavailable")
	}
	raw, err := s.settingRepo.GetValue(ctx, CodexCatalogSourceSettingKey(accountID))
	if err != nil {
		if errors.Is(err, ErrSettingNotFound) {
			return CodexCatalogSourceSnapshot{}, false, nil
		}
		return CodexCatalogSourceSnapshot{}, false, err
	}
	var snap CodexCatalogSourceSnapshot
	if err := json.Unmarshal([]byte(raw), &snap); err != nil {
		return CodexCatalogSourceSnapshot{}, true, fmt.Errorf("codex catalog source: decode: %w", err)
	}
	if snap.SchemaVersion != CodexCatalogSourceSchemaVersion || snap.AccountID != accountID {
		return CodexCatalogSourceSnapshot{}, true, fmt.Errorf("codex catalog source: unexpected snapshot for account %d", accountID)
	}
	return snap, true, nil
}

// SaveCodexCatalogSource 保存账号源快照。同一账号的刷新任务由调用方合并，这里不做并发比较。
func (s *SettingService) SaveCodexCatalogSource(ctx context.Context, snap CodexCatalogSourceSnapshot) error {
	if s == nil || s.settingRepo == nil {
		return fmt.Errorf("codex catalog source: setting repository unavailable")
	}
	if snap.AccountID <= 0 {
		return fmt.Errorf("codex catalog source: invalid account id")
	}
	snap.SchemaVersion = CodexCatalogSourceSchemaVersion
	snap.Models = NormalizeCodexCatalogSourceModels(snap.Models)
	raw, err := json.Marshal(snap)
	if err != nil {
		return err
	}
	return s.settingRepo.Set(ctx, CodexCatalogSourceSettingKey(snap.AccountID), string(raw))
}

// ---------------------------------------------------------------------------
// 目录评估
// ---------------------------------------------------------------------------

// CodexThirdPartyModelStatus 是候选模型在管理页面上显示的状态。
type CodexThirdPartyModelStatus string

const (
	// CodexModelStatusNative 表示原生 GPT，只读展示，不受开关控制。
	CodexModelStatusNative CodexThirdPartyModelStatus = "native"
	// CodexModelStatusNeedsCapability 表示能力依据不足（待补能力），不能开启。
	CodexModelStatusNeedsCapability CodexThirdPartyModelStatus = "needs_capability"
	// CodexModelStatusConfigurable 表示静态发布条件满足（配置可用），尚无与当前指纹一致的真实验收记录。
	CodexModelStatusConfigurable CodexThirdPartyModelStatus = "configurable"
	// CodexModelStatusVerified 表示存在与当前指纹一致的真实验收记录（已验证）。
	CodexModelStatusVerified CodexThirdPartyModelStatus = "verified"
	// CodexModelStatusConflict 表示归属、路由或工具契约冲突（配置冲突），停止发布。
	CodexModelStatusConflict CodexThirdPartyModelStatus = "conflict"
	// CodexModelStatusOverBudget 表示启用后最坏目录体积超过上限（目录超限）。
	CodexModelStatusOverBudget CodexThirdPartyModelStatus = "over_budget"
	// CodexModelStatusUnavailable 表示没有候选账号、分组不允许或路由无法确认。
	CodexModelStatusUnavailable CodexThirdPartyModelStatus = "unavailable"
)

// CodexThirdPartyCapabilitySummary 是候选模型最终将发布的关键能力摘要，供页面展示。
type CodexThirdPartyCapabilitySummary struct {
	InputModalities       []string `json:"input_modalities,omitempty"`
	ReasoningLevels       []string `json:"reasoning_levels,omitempty"`
	DefaultReasoningLevel string   `json:"default_reasoning_level,omitempty"`
	ContextWindow         int64    `json:"context_window,omitempty"`
	ShellType             string   `json:"shell_type,omitempty"`
	ApplyPatchToolType    string   `json:"apply_patch_tool_type,omitempty"`
	SupportsSearchTool    bool     `json:"supports_search_tool"`
	WebSearchToolType     string   `json:"web_search_tool_type,omitempty"`
}

// CodexThirdPartyRouteAccount 是路由结构里一个承接账号的展示信息，不含任何凭据。
type CodexThirdPartyRouteAccount struct {
	ID       int64  `json:"id"`
	Name     string `json:"name"`
	Platform string `json:"platform"`
}

// CodexThirdPartyCandidate 是一个公开模型在当前分组下的评估结果。
type CodexThirdPartyCandidate struct {
	Slug        string `json:"slug"`
	DisplayName string `json:"display_name"`
	// Native 为 true 表示原生 GPT 条目，只读展示。
	Native bool `json:"native"`
	// Enabled 是配置中的管理员意图。
	Enabled bool `json:"enabled"`
	// CanEnable 表示静态发布条件满足、允许开启。
	CanEnable bool `json:"can_enable"`
	// Published 表示按当前配置会进入最终目录。
	Published bool                       `json:"published"`
	Status    CodexThirdPartyModelStatus `json:"status"`
	// Reasons 是面向管理员的中文原因列表，不含凭据。
	Reasons []string `json:"reasons,omitempty"`
	// Sources 记录名称出处：group_models（分组默认启用的模型名单，一定有）、mapping、alias、base_catalog。
	Sources        []string `json:"sources,omitempty"`
	TargetPlatform string   `json:"target_platform,omitempty"`
	UpstreamModel  string   `json:"upstream_model,omitempty"`
	AccountIDs     []int64  `json:"account_ids,omitempty"`
	// PrimaryAccount 与 AlternateAccounts 仅在 Composite 分组按账号级跨平台候选池承接时出现：
	// 首选账号决定 TargetPlatform，备选账号是同一承接分组里的其余账号（只读展示，不含凭据）。
	PrimaryAccount    *CodexThirdPartyRouteAccount  `json:"primary_account,omitempty"`
	AlternateAccounts []CodexThirdPartyRouteAccount `json:"alternate_accounts,omitempty"`
	// Protocol 是实际上游通路：responses_native 或 responses_via_chat。
	Protocol  string `json:"protocol,omitempty"`
	ProfileID string `json:"profile_id,omitempty"`
	// Fingerprint 绑定客户端契约、路由、协议与能力档案；验收记录须与它一致才算已验证。
	Fingerprint  string                           `json:"fingerprint,omitempty"`
	Capabilities CodexThirdPartyCapabilitySummary `json:"capabilities"`
	// EntryBytes 是该条目序列化后的字节数，用于预算展示。
	EntryBytes int `json:"entry_bytes,omitempty"`
}

// CodexCatalogBudget 是目录体积预算。Complete=false 时不得显示为通过。
type CodexCatalogBudget struct {
	LimitBytes             int  `json:"limit_bytes"`
	NativeCount            int  `json:"native_count"`
	EnabledThirdPartyCount int  `json:"enabled_third_party_count"`
	WorstCaseBytes         int  `json:"worst_case_bytes"`
	RemainingBytes         int  `json:"remaining_bytes"`
	Complete               bool `json:"complete"`
	// Deferred 为 true 表示页面读取时没有发起任何上游请求、也没有可用的官方账号目录缓存，
	// 预算留到预览与保存（开启）时检查。此时 Complete 为 false，页面显示「开启时检查」，
	// 不能显示为通过；开启仍必须经过预览，由预览完成真正的检查。
	Deferred       bool    `json:"deferred,omitempty"`
	Reason         string  `json:"reason,omitempty"`
	BaseAccountIDs []int64 `json:"base_account_ids,omitempty"`
}

// CodexThirdPartyCatalogEvaluation 是一个分组在给定配置下的完整评估结果。
type CodexThirdPartyCatalogEvaluation struct {
	GroupID       int64  `json:"group_id"`
	GroupPlatform string `json:"group_platform"`
	// Revision 是被评估配置的 revision。
	Revision int64 `json:"revision"`
	// InputFingerprint 是除配置开关以外全部评估输入的确定性摘要（候选集合、路由、能力、基础目录身份）。
	// 相同输入必须得到相同指纹；保存开启操作时用它确认预览仍然有效。
	InputFingerprint string                     `json:"input_fingerprint"`
	Candidates       []CodexThirdPartyCandidate `json:"candidates"`
	Budget           CodexCatalogBudget         `json:"budget"`
	EvaluatedAt      time.Time                  `json:"evaluated_at"`
}

// codexEvalOfflineKey 标记「离线评估」的上下文键。
type codexEvalOfflineKey struct{}

// WithCodexThirdPartyOfflineEvaluation 返回要求评估器不发任何上游请求的上下文：页面读取使用。
// 评估只查数据库计算候选、路由与能力；体积预算只用最近一次预览或保存时留下的官方账号目录缓存，
// 没有缓存就标记为「开启时检查」。预览与保存使用默认模式（拉取官方账号基础目录）。
func WithCodexThirdPartyOfflineEvaluation(ctx context.Context) context.Context {
	return context.WithValue(ctx, codexEvalOfflineKey{}, true)
}

func codexThirdPartyOfflineEvaluation(ctx context.Context) bool {
	offline, _ := ctx.Value(codexEvalOfflineKey{}).(bool)
	return offline
}

// CodexThirdPartyCatalogEvaluator 由目录编排实现，管理接口调用它完成读取、预览和保存前复核。
type CodexThirdPartyCatalogEvaluator interface {
	// EvaluateCodexThirdPartyCatalog 在给定配置下评估分组。预览时 cfg 是叠加了变更的副本。
	// 评估本身不写库、不触发上游同步。
	EvaluateCodexThirdPartyCatalog(ctx context.Context, group *Group, cfg CodexModelVisibilityConfig) (CodexThirdPartyCatalogEvaluation, error)
}
