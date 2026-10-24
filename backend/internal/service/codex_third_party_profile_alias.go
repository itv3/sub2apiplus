package service

// codex_third_party_profile_alias.go 处理「上游型号 → 能力档案」的两件事：
//
//  1. 内置匹配规则的逐级规范化（Ollama 标签、下划线、日期后缀），见 codexThirdPartyNormalizeKey；
//  2. 后台可维护的型号别名（“关联到已有档案”）：存在 settings 表的一个 JSON 键里，不加表与迁移。
//     匹配时先查别名表（按同一套规范化后的名字精确比较），命中即用对应档案，再走内置规则。
//     只允许关联到当前存在的档案；档案被删或改名后，失效的别名在页面上标出，不参与匹配。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

// ---------------------------------------------------------------------------
// 逐级规范化
// ---------------------------------------------------------------------------

// codexThirdPartyMatchLevels 是档案匹配的规范化级别数：
// 0 精确；1 去 Ollama 标签；2 再把下划线视同连字符；3 再去末尾日期后缀（含 -latest）。
const codexThirdPartyMatchLevels = 4

// codexThirdPartyNormalizeKey 把已去厂商前缀、转小写的匹配键规范化到指定级别（级别是累加的）。
// 档案别名一侧用同一函数规范化，比较的是规范化后的结果。
func codexThirdPartyNormalizeKey(key string, level int) string {
	if level >= 1 {
		if base, ok := codexThirdPartyStripOllamaTag(key); ok {
			key = base
		}
	}
	if level >= 2 {
		key = strings.ReplaceAll(key, "_", "-")
	}
	if level >= 3 {
		key = codexThirdPartyStripDateSuffix(key)
	}
	return key
}

// codexThirdPartyStripDateSuffix 去掉末尾一段日期形态后缀，剥完名字不能为空：
//   - `-latest`（视同 Ollama 的 :latest）；
//   - `-MMDD`、`-YYMMDD`、`-YYYYMMDD`、`-YYYY-MM-DD`，以及 `@` 加这些数字的写法。
//
// 参数规模（-8b、-70b）、版本号（-v4、-4o、-3.5）以及 -preview、-instruct、-mini 这类会改变能力的后缀不剥。
func codexThirdPartyStripDateSuffix(key string) string {
	if base, ok := strings.CutSuffix(key, "-latest"); ok && base != "" {
		return base
	}
	if at := strings.LastIndex(key, "@"); at > 0 {
		tail := key[at+1:]
		if isCompactDateTail(tail) || isDashedDate(tail) {
			return key[:at]
		}
		return key
	}
	if n := len(key); n > 11 && key[n-11] == '-' && isDashedDate(key[n-10:]) {
		return key[:n-11]
	}
	if at := strings.LastIndex(key, "-"); at > 0 && isCompactDateTail(key[at+1:]) {
		return key[:at]
	}
	return key
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func validMonthDay(month, day string) bool {
	m, errM := strconv.Atoi(month)
	d, errD := strconv.Atoi(day)
	return errM == nil && errD == nil && m >= 1 && m <= 12 && d >= 1 && d <= 31
}

// isCompactDateTail 识别不带分隔符的日期：MMDD（4 位）、YYMMDD（6 位）、YYYYMMDD（8 位，年份 19xx 或 20xx）。
func isCompactDateTail(tail string) bool {
	if !allDigits(tail) {
		return false
	}
	switch len(tail) {
	case 4:
		return validMonthDay(tail[0:2], tail[2:4])
	case 6:
		return validMonthDay(tail[2:4], tail[4:6])
	case 8:
		return (tail[0:2] == "19" || tail[0:2] == "20") && validMonthDay(tail[4:6], tail[6:8])
	}
	return false
}

// isDashedDate 识别 YYYY-MM-DD。
func isDashedDate(tail string) bool {
	if len(tail) != 10 || tail[4] != '-' || tail[7] != '-' {
		return false
	}
	year, month, day := tail[0:4], tail[5:7], tail[8:10]
	return allDigits(year) && allDigits(month) && allDigits(day) &&
		(year[0:2] == "19" || year[0:2] == "20") && validMonthDay(month, day)
}

// ---------------------------------------------------------------------------
// 型号别名：存储
// ---------------------------------------------------------------------------

const (
	// CodexProfileAliasSettingKey 是型号别名在 settings 表里的键。
	CodexProfileAliasSettingKey = "codex_third_party_profile_aliases"
	// CodexProfileAliasSchemaVersion 是别名配置的结构版本。
	CodexProfileAliasSchemaVersion = 1
	// codexProfileAliasModelMaxRunes 是上游型号名的长度上限。
	codexProfileAliasModelMaxRunes = 200
	// codexProfileAliasCacheTTL 是目录评估侧缓存别名表的时长；管理接口写入后会立即失效本进程的缓存。
	codexProfileAliasCacheTTL = 15 * time.Second
)

// CodexProfileAliasEntry 是一条型号别名：把某个上游型号关联到已有的能力档案。
type CodexProfileAliasEntry struct {
	// UpstreamModel 是管理员输入的原始上游型号（展示用）。
	UpstreamModel string `json:"upstream_model"`
	ProfileID     string `json:"profile_id"`
	// UpdatedBy 是操作人（管理员用户标识），UpdatedAt 是操作时间。
	UpdatedBy string    `json:"updated_by"`
	UpdatedAt time.Time `json:"updated_at"`
}

// CodexProfileAliasConfig 是 settings 里保存的别名表。Aliases 的键是规范化后的上游型号（见 codexProfileAliasKey）。
type CodexProfileAliasConfig struct {
	SchemaVersion int                               `json:"schema_version"`
	Revision      int64                             `json:"revision"`
	Aliases       map[string]CodexProfileAliasEntry `json:"aliases"`
}

// NewCodexProfileAliasConfig 返回空的别名表。
func NewCodexProfileAliasConfig() CodexProfileAliasConfig {
	return CodexProfileAliasConfig{SchemaVersion: CodexProfileAliasSchemaVersion, Aliases: map[string]CodexProfileAliasEntry{}}
}

// codexProfileAliasKey 把上游型号规范化成别名表的键：去厂商前缀、转小写，并规范化到最宽松的级别，
// 与档案匹配的各级规范化是同一套规则。
func codexProfileAliasKey(model string) string {
	return codexThirdPartyNormalizeKey(codexThirdPartyProfileKey(model), codexThirdPartyMatchLevels-1)
}

// codexProfileAliasIndex 是评估时使用的别名索引：规范化后的上游型号 → 档案 ID，只含档案仍存在的别名。
type codexProfileAliasIndex map[string]string

// LoadCodexProfileAliases 读取别名表；found=false 表示从未保存。
func (s *SettingService) LoadCodexProfileAliases(ctx context.Context) (CodexProfileAliasConfig, bool, error) {
	if s == nil || s.settingRepo == nil {
		return NewCodexProfileAliasConfig(), false, fmt.Errorf("codex profile aliases: setting repository unavailable")
	}
	raw, err := s.settingRepo.GetValue(ctx, CodexProfileAliasSettingKey)
	if err != nil {
		if errors.Is(err, ErrSettingNotFound) {
			return NewCodexProfileAliasConfig(), false, nil
		}
		return NewCodexProfileAliasConfig(), false, err
	}
	var cfg CodexProfileAliasConfig
	if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
		return NewCodexProfileAliasConfig(), true, fmt.Errorf("codex profile aliases: decode: %w", err)
	}
	if cfg.SchemaVersion != CodexProfileAliasSchemaVersion {
		return NewCodexProfileAliasConfig(), true, fmt.Errorf("codex profile aliases: unsupported schema_version %d", cfg.SchemaVersion)
	}
	if cfg.Aliases == nil {
		cfg.Aliases = map[string]CodexProfileAliasEntry{}
	}
	return cfg, true, nil
}

// SaveCodexProfileAliases 保存别名表。
func (s *SettingService) SaveCodexProfileAliases(ctx context.Context, cfg CodexProfileAliasConfig) error {
	if s == nil || s.settingRepo == nil {
		return fmt.Errorf("codex profile aliases: setting repository unavailable")
	}
	cfg.SchemaVersion = CodexProfileAliasSchemaVersion
	if cfg.Aliases == nil {
		cfg.Aliases = map[string]CodexProfileAliasEntry{}
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		return err
	}
	return s.settingRepo.Set(ctx, CodexProfileAliasSettingKey, string(raw))
}

// codexProfileAliasLoader 是目录评估侧可选使用的别名读取能力（*SettingService 实现）。
type codexProfileAliasLoader interface {
	LoadCodexProfileAliases(ctx context.Context) (CodexProfileAliasConfig, bool, error)
}

// ---------------------------------------------------------------------------
// 型号别名：目录评估侧
// ---------------------------------------------------------------------------

// codexProfileAliasCache 是评估侧缓存的别名索引。
type codexProfileAliasCache struct {
	mu     sync.Mutex
	index  codexProfileAliasIndex
	loaded bool
	at     time.Time
}

// profileAliasIndex 返回当前有效的别名索引（缓存 codexProfileAliasCacheTTL）。读取失败时沿用上一份缓存，
// 没有缓存就按没有别名处理并记日志：别名只会让更多型号可发布，读不到不应让评估失败。
func (s *CodexThirdPartyCatalogService) profileAliasIndex(ctx context.Context) codexProfileAliasIndex {
	if s == nil {
		return nil
	}
	loader, ok := s.settings.(codexProfileAliasLoader)
	if !ok {
		return nil
	}
	s.aliasCache.mu.Lock()
	defer s.aliasCache.mu.Unlock()
	if s.aliasCache.loaded && s.now().Sub(s.aliasCache.at) < codexProfileAliasCacheTTL {
		return s.aliasCache.index
	}
	cfg, _, err := loader.LoadCodexProfileAliases(ctx)
	if err != nil {
		slog.Warn("codex_third_party_profile_aliases_load_failed", "reason", err.Error())
		return s.aliasCache.index
	}
	index := codexProfileAliasIndex{}
	for _, entry := range cfg.Aliases {
		if s.profiles.byID(entry.ProfileID) == nil {
			continue // 档案已不存在：失效别名不参与匹配
		}
		if key := codexProfileAliasKey(entry.UpstreamModel); key != "" {
			index[key] = entry.ProfileID
		}
	}
	s.aliasCache.index, s.aliasCache.loaded, s.aliasCache.at = index, true, s.now()
	return index
}

// InvalidateCodexProfileAliases 让本进程缓存的别名索引立即失效（管理接口写入别名后调用）。
func (s *CodexThirdPartyCatalogService) InvalidateCodexProfileAliases() {
	if s == nil {
		return
	}
	s.aliasCache.mu.Lock()
	s.aliasCache.loaded = false
	s.aliasCache.mu.Unlock()
}

// CodexThirdPartyProfileSummary 是一份能力档案的展示摘要：页面下拉用它列出各档案已认得的型号。
type CodexThirdPartyProfileSummary struct {
	ID             string   `json:"id"`
	UpstreamModels []string `json:"upstream_models"`
	Protocols      []string `json:"protocols"`
}

// CodexThirdPartyProfileDirectory 是评估器可选实现的档案目录能力，管理服务据此校验与列出别名。
type CodexThirdPartyProfileDirectory interface {
	// ListCodexThirdPartyProfiles 按 ID 排序返回全部内置档案。
	ListCodexThirdPartyProfiles() []CodexThirdPartyProfileSummary
	// MatchBuiltinCodexThirdPartyProfile 返回内置规则（含各级规范化）能直接认出该上游型号的档案 ID。
	MatchBuiltinCodexThirdPartyProfile(model string) (profileID string, ok bool)
	// InvalidateCodexProfileAliases 让评估侧缓存的别名索引失效。
	InvalidateCodexProfileAliases()
}

var _ CodexThirdPartyProfileDirectory = (*CodexThirdPartyCatalogService)(nil)

func (s *CodexThirdPartyCatalogService) ListCodexThirdPartyProfiles() []CodexThirdPartyProfileSummary {
	if s == nil || s.profiles == nil {
		return []CodexThirdPartyProfileSummary{}
	}
	out := make([]CodexThirdPartyProfileSummary, 0, len(s.profiles.profiles))
	for _, p := range s.profiles.profiles {
		out = append(out, CodexThirdPartyProfileSummary{
			ID:             p.ID,
			UpstreamModels: append([]string(nil), p.UpstreamModels...),
			Protocols:      append([]string(nil), p.Protocols...),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (s *CodexThirdPartyCatalogService) MatchBuiltinCodexThirdPartyProfile(model string) (string, bool) {
	if s == nil || s.profiles == nil {
		return "", false
	}
	if p := s.profiles.match(model); p != nil {
		return p.ID, true
	}
	return "", false
}

// ---------------------------------------------------------------------------
// 型号别名：管理服务
// ---------------------------------------------------------------------------

var (
	// ErrCodexProfileAliasInvalid 表示别名请求不合法（400）。
	ErrCodexProfileAliasInvalid = infraerrors.BadRequest("CODEX_PROFILE_ALIAS_INVALID", "型号别名请求不合法")
	// ErrCodexProfileNotFound 表示要关联的档案不存在（400）。
	ErrCodexProfileNotFound = infraerrors.BadRequest("CODEX_PROFILE_NOT_FOUND", "要关联的能力档案不存在")
	// ErrCodexProfileAliasBuiltin 表示该上游型号已被内置档案直接认出，不能再关联到另一个档案（409）。
	ErrCodexProfileAliasBuiltin = infraerrors.Conflict("CODEX_PROFILE_ALIAS_BUILTIN", "该上游型号已被内置能力档案直接认出，无需关联")
)

// CodexProfileAliasView 是别名在管理页面上的展示项。
type CodexProfileAliasView struct {
	UpstreamModel string `json:"upstream_model"`
	// Key 是规范化后的键（别名表里的主键）。
	Key       string `json:"key"`
	ProfileID string `json:"profile_id"`
	// Valid 为 false 表示档案已被删除或改名，该别名不参与匹配。
	Valid     bool      `json:"valid"`
	UpdatedBy string    `json:"updated_by"`
	UpdatedAt time.Time `json:"updated_at"`
}

// CodexProfileAliasList 是别名接口的返回内容：全部别名，加可供关联的各内置档案。
type CodexProfileAliasList struct {
	Revision int64                           `json:"revision"`
	Aliases  []CodexProfileAliasView         `json:"aliases"`
	Profiles []CodexThirdPartyProfileSummary `json:"profiles"`
}

// ListProfileAliases 返回全部型号别名（含失效项）和各内置档案。
func (s *CodexThirdPartyAdminService) ListProfileAliases(ctx context.Context) (*CodexProfileAliasList, error) {
	directory, err := s.profileDirectory()
	if err != nil {
		return nil, err
	}
	cfg, _, err := s.settings.LoadCodexProfileAliases(ctx)
	if err != nil {
		return nil, ErrCodexCatalogEvaluationFailed.WithCause(err)
	}
	return buildCodexProfileAliasList(cfg, directory), nil
}

// SetProfileAlias 新增或更新一条关联：把上游型号关联到已有档案。重复提交相同内容是幂等的。
// 上游型号不能为空，档案必须存在；已被内置档案直接认出的型号不允许再关联（避免同名多档案）。
func (s *CodexThirdPartyAdminService) SetProfileAlias(ctx context.Context, upstreamModel, profileID, operator string) (*CodexProfileAliasList, error) {
	directory, err := s.profileDirectory()
	if err != nil {
		return nil, err
	}
	upstreamModel = strings.TrimSpace(upstreamModel)
	profileID = strings.TrimSpace(profileID)
	if err := validateCodexProfileAliasModel(upstreamModel); err != nil {
		return nil, err
	}
	if profileID == "" {
		return nil, codexProfileAliasInvalid("档案 ID 不能为空")
	}
	if !codexProfileExists(directory, profileID) {
		return nil, ErrCodexProfileNotFound.WithMetadata(map[string]string{"profile_id": profileID})
	}
	if builtin, ok := directory.MatchBuiltinCodexThirdPartyProfile(upstreamModel); ok {
		return nil, ErrCodexProfileAliasBuiltin.WithMetadata(map[string]string{"profile_id": builtin})
	}

	s.aliasMu.Lock()
	defer s.aliasMu.Unlock()
	cfg, _, err := s.settings.LoadCodexProfileAliases(ctx)
	if err != nil {
		// 已有配置损坏时不能当作空表覆盖。
		return nil, ErrCodexCatalogEvaluationFailed.WithCause(err)
	}
	cfg.Aliases[codexProfileAliasKey(upstreamModel)] = CodexProfileAliasEntry{
		UpstreamModel: upstreamModel,
		ProfileID:     profileID,
		UpdatedBy:     operator,
		UpdatedAt:     s.now().UTC(),
	}
	cfg.Revision++
	if err := s.settings.SaveCodexProfileAliases(ctx, cfg); err != nil {
		return nil, ErrCodexCatalogEvaluationFailed.WithCause(err)
	}
	directory.InvalidateCodexProfileAliases()
	return buildCodexProfileAliasList(cfg, directory), nil
}

// DeleteProfileAlias 解除关联。上游型号本来就没有关联时也成功（幂等）。
func (s *CodexThirdPartyAdminService) DeleteProfileAlias(ctx context.Context, upstreamModel string) (*CodexProfileAliasList, error) {
	directory, err := s.profileDirectory()
	if err != nil {
		return nil, err
	}
	upstreamModel = strings.TrimSpace(upstreamModel)
	if err := validateCodexProfileAliasModel(upstreamModel); err != nil {
		return nil, err
	}
	s.aliasMu.Lock()
	defer s.aliasMu.Unlock()
	cfg, _, err := s.settings.LoadCodexProfileAliases(ctx)
	if err != nil {
		return nil, ErrCodexCatalogEvaluationFailed.WithCause(err)
	}
	key := codexProfileAliasKey(upstreamModel)
	if _, exists := cfg.Aliases[key]; exists {
		delete(cfg.Aliases, key)
		cfg.Revision++
		if err := s.settings.SaveCodexProfileAliases(ctx, cfg); err != nil {
			return nil, ErrCodexCatalogEvaluationFailed.WithCause(err)
		}
		directory.InvalidateCodexProfileAliases()
	}
	return buildCodexProfileAliasList(cfg, directory), nil
}

func (s *CodexThirdPartyAdminService) profileDirectory() (CodexThirdPartyProfileDirectory, error) {
	directory, ok := s.evaluator.(CodexThirdPartyProfileDirectory)
	if !ok {
		return nil, ErrCodexCatalogEvaluationFailed.WithCause(errors.New("evaluator does not expose profile directory"))
	}
	return directory, nil
}

func codexProfileAliasInvalid(detail string) error {
	return infraerrors.BadRequest(ErrCodexProfileAliasInvalid.Reason, ErrCodexProfileAliasInvalid.Message+"："+detail)
}

// validateCodexProfileAliasModel 校验上游型号：非空、长度受限、不含控制字符与通配符。
func validateCodexProfileAliasModel(model string) error {
	if model == "" {
		return codexProfileAliasInvalid("上游型号不能为空")
	}
	if len([]rune(model)) > codexProfileAliasModelMaxRunes {
		return codexProfileAliasInvalid(fmt.Sprintf("上游型号不能超过 %d 个字符", codexProfileAliasModelMaxRunes))
	}
	for _, r := range model {
		if unicode.IsControl(r) || r == '*' {
			return codexProfileAliasInvalid("上游型号含有不允许的字符")
		}
	}
	if codexProfileAliasKey(model) == "" {
		return codexProfileAliasInvalid("上游型号不能为空")
	}
	return nil
}

func codexProfileExists(directory CodexThirdPartyProfileDirectory, id string) bool {
	for _, profile := range directory.ListCodexThirdPartyProfiles() {
		if profile.ID == id {
			return true
		}
	}
	return false
}

func buildCodexProfileAliasList(cfg CodexProfileAliasConfig, directory CodexThirdPartyProfileDirectory) *CodexProfileAliasList {
	profiles := directory.ListCodexThirdPartyProfiles()
	exists := make(map[string]struct{}, len(profiles))
	for _, profile := range profiles {
		exists[profile.ID] = struct{}{}
	}
	views := make([]CodexProfileAliasView, 0, len(cfg.Aliases))
	for key, entry := range cfg.Aliases {
		_, valid := exists[entry.ProfileID]
		views = append(views, CodexProfileAliasView{
			UpstreamModel: entry.UpstreamModel, Key: key, ProfileID: entry.ProfileID,
			Valid: valid, UpdatedBy: entry.UpdatedBy, UpdatedAt: entry.UpdatedAt,
		})
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Key < views[j].Key })
	return &CodexProfileAliasList{Revision: cfg.Revision, Aliases: views, Profiles: profiles}
}
