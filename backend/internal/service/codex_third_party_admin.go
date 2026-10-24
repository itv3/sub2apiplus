package service

// codex_third_party_admin.go 实现「Codex 三方模型启用」的管理服务，供 Plus 增强页的管理接口调用：
//
//   - 读取：返回分组展示配置、目录评估结果和每个账号的模型 ID 源快照状态。读取只查数据库，
//     不发任何上游请求（离线评估），也不触发源刷新；体积预算没有缓存时标记为「开启时检查」；
//   - 预览：在当前配置的副本上叠加变更并评估（拉取官方账号基础目录，限时并发、失败跳过），
//     返回评估结果与输入指纹；
//   - 保存：纯关闭不依赖评估器和上游；含开启则用与预览相同的模式重新评估、核对预览指纹、整批校验后原子提交；
//   - 标记已验证：把管理员的真实调用验收结果绑定到候选指纹；
//   - 手动同步：对分组全部账号强制刷新源。
//
// 目录评估由另一路实现的 CodexThirdPartyCatalogEvaluator 提供，这里只通过接口调用。
// 本文件所有对外依赖都是窄接口，便于用假实现做单元测试。

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"golang.org/x/sync/singleflight"
)

const (
	// codexThirdPartyRefreshTimeout 是单个账号一次源刷新（上游同步）的超时。
	codexThirdPartyRefreshTimeout = 60 * time.Second
	// codexThirdPartyRefreshConcurrency 是源刷新的全局并发上限，避免一次页面读取把上游和本机打满。
	codexThirdPartyRefreshConcurrency = 2
	// codexThirdPartySourceSaveTimeout 是写入源快照的独立超时。即使上游同步已经把 60 秒用完，
	// 失败记录仍然要能落库，所以不和同步共用同一个超时。
	codexThirdPartySourceSaveTimeout = 10 * time.Second
	// codexThirdPartyLastErrorMaxRunes 是写入快照的错误说明长度上限。
	codexThirdPartyLastErrorMaxRunes = 200
	// codexThirdPartyVerifyNoteMaxRunes 是验收备注长度上限。
	codexThirdPartyVerifyNoteMaxRunes = 500

	// 源快照在管理页面上的状态：除了快照自身的 complete、empty、failed，还有从未同步过的 missing。
	codexThirdPartySourceStatusMissing = "missing"
	// codexThirdPartySourceStatusOfficialSkipped 表示官方 OpenAI 账号：它不可能承接第三方模型，
	// 同步只会白白多一次官方出站并改写账号 Extra，所以源刷新（自动与手动）一律跳过，
	// 页面以该状态展示，不计为过期。
	codexThirdPartySourceStatusOfficialSkipped = "official_skipped"
)

var (
	// ErrCodexThirdPartyGroupPlatformUnsupported 表示分组平台不提供 Codex 在线目录（400）。
	ErrCodexThirdPartyGroupPlatformUnsupported = infraerrors.BadRequest(
		"CODEX_THIRD_PARTY_GROUP_PLATFORM_UNSUPPORTED", "该分组平台不提供 Codex 在线目录，无需配置")
	// ErrCodexThirdPartyChangesInvalid 表示预览或保存请求的变更列表不合法（400）。
	ErrCodexThirdPartyChangesInvalid = infraerrors.BadRequest(
		"CODEX_THIRD_PARTY_CHANGES_INVALID", "变更列表不合法")
	// ErrCodexModelEnableRejected 表示开启批次中有模型不满足发布条件，整批拒绝（400）。逐项原因放在 metadata。
	ErrCodexModelEnableRejected = infraerrors.BadRequest(
		"CODEX_MODEL_ENABLE_REJECTED", "所选模型无法全部开启，整批均未保存")
	// ErrCodexModelNotEnabled 表示标记已验证时该模型尚未开启（409）。
	ErrCodexModelNotEnabled = infraerrors.Conflict(
		"CODEX_MODEL_NOT_ENABLED", "该模型尚未在 Codex 中开启，无法标记已验证")
	// ErrCodexVerificationStale 表示标记已验证时候选指纹已变化（409）。
	ErrCodexVerificationStale = infraerrors.Conflict(
		"CODEX_MODEL_VERIFICATION_STALE", "模型的路由或能力指纹已变化，请刷新后重新验收")
	// ErrCodexModelVisibilityCorrupt 表示已保存的分组展示配置无法解析，需要人工处理（500）。
	ErrCodexModelVisibilityCorrupt = infraerrors.InternalServer(
		"CODEX_MODEL_VISIBILITY_CORRUPT", "已保存的 Codex 三方模型配置无法解析，请联系维护人员处理")
	// ErrCodexCatalogEvaluationFailed 表示目录评估失败（500）。
	ErrCodexCatalogEvaluationFailed = infraerrors.InternalServer(
		"CODEX_CATALOG_EVALUATION_FAILED", "Codex 目录评估失败")
	// ErrCodexCatalogRefreshFailed 表示刷新页面依赖的上游目录时出现整体性失败（500）。
	// 单个账号拉取失败不属于它，逐账号结果在返回内容里。
	ErrCodexCatalogRefreshFailed = infraerrors.InternalServer(
		"CODEX_CATALOG_REFRESH_FAILED", "Codex 上游目录刷新失败")
)

// ---------------------------------------------------------------------------
// 窄接口
// ---------------------------------------------------------------------------

// codexThirdPartyGroupReader 读取分组。
type codexThirdPartyGroupReader interface {
	GetByIDLite(ctx context.Context, id int64) (*Group, error)
}

// codexThirdPartyAccountLister 列出分组内持久启用的账号（忽略限流等瞬时状态）。
type codexThirdPartyAccountLister interface {
	ListModelAvailabilityCandidates(ctx context.Context, groupID *int64, platforms []string, includeGrouped bool) ([]Account, error)
}

// codexThirdPartyUpstreamSyncer 是现有的上游模型同步能力。
type codexThirdPartyUpstreamSyncer interface {
	SyncUpstreamModelCatalog(ctx context.Context, account *Account) (*UpstreamModelCatalog, error)
}

// codexThirdPartyModelCatalogReader 是评估器可选实现的只读能力：账号上游模型目录的缓存状态。
// 页面「刷新」写入的就是这份缓存，账号源状态据此展示，避免刷新成功后仍显示 missing。
type codexThirdPartyModelCatalogReader interface {
	ModelCatalogCacheStatus(account *Account) (needed, loaded bool, models int, fetchedAt time.Time)
}

// codexThirdPartyAdminSettings 读取展示配置，并读写账号源快照。*SettingService 满足该接口。
type codexThirdPartyAdminSettings interface {
	LoadCodexModelVisibility(ctx context.Context, groupID int64) (CodexModelVisibilityConfig, bool, error)
	LoadCodexCatalogSource(ctx context.Context, accountID int64) (CodexCatalogSourceSnapshot, bool, error)
	SaveCodexCatalogSource(ctx context.Context, snap CodexCatalogSourceSnapshot) error
}

// ---------------------------------------------------------------------------
// 请求与响应结构
// ---------------------------------------------------------------------------

// CodexThirdPartyChange 是对单个公开模型的一项开关变更。
type CodexThirdPartyChange struct {
	Slug    string `json:"slug"`
	Enabled bool   `json:"enabled"`
}

// CodexThirdPartyPreviewRequest 是预览请求。
type CodexThirdPartyPreviewRequest struct {
	ExpectedRevision int64                   `json:"expected_revision"`
	Changes          []CodexThirdPartyChange `json:"changes"`
}

// CodexThirdPartySaveRequest 是保存请求。含开启的变更必须携带预览返回的输入指纹。
type CodexThirdPartySaveRequest struct {
	ExpectedRevision int64                   `json:"expected_revision"`
	Changes          []CodexThirdPartyChange `json:"changes"`
	InputFingerprint string                  `json:"input_fingerprint"`
}

// CodexThirdPartyVerifyRequest 是标记已验证请求。
type CodexThirdPartyVerifyRequest struct {
	ExpectedRevision int64  `json:"expected_revision"`
	Slug             string `json:"slug"`
	Fingerprint      string `json:"fingerprint"`
	Note             string `json:"note,omitempty"`
}

// CodexThirdPartySourceStatus 是一个账号的模型 ID 源快照在管理页面上的状态。
type CodexThirdPartySourceStatus struct {
	AccountID     int64  `json:"account_id"`
	AccountName   string `json:"account_name"`
	Platform      string `json:"platform"`
	AccountStatus string `json:"account_status"`
	// Status 取 missing（从未同步）、complete、empty（上游明确返回空列表）、failed，
	// 以及 official_skipped（官方 OpenAI 账号，不同步也不计为过期）。
	Status      string     `json:"status"`
	SyncedAt    *time.Time `json:"synced_at,omitempty"`
	AttemptedAt *time.Time `json:"attempted_at,omitempty"`
	// Stale 为 true 表示需要刷新：从未成功、最近一次失败或超过刷新阈值。
	Stale bool `json:"stale"`
	// FingerprintMatches 为 false 表示账号源配置在快照之后变过，快照不参与候选发现。
	FingerprintMatches bool   `json:"fingerprint_matches"`
	ModelCount         int    `json:"model_count"`
	LastError          string `json:"last_error,omitempty"`
	// Refreshing 为 true 表示后台正在刷新这个账号的源。
	Refreshing bool `json:"refreshing"`
}

// CodexThirdPartyGroupState 是读取接口的返回内容。
type CodexThirdPartyGroupState struct {
	GroupID       int64                            `json:"group_id"`
	GroupName     string                           `json:"group_name"`
	GroupPlatform string                           `json:"group_platform"`
	Config        CodexModelVisibilityConfig       `json:"config"`
	Evaluation    CodexThirdPartyCatalogEvaluation `json:"evaluation"`
	Sources       []CodexThirdPartySourceStatus    `json:"sources"`
}

// CodexThirdPartyPreviewResult 是预览接口的返回内容。
type CodexThirdPartyPreviewResult struct {
	// Revision 是被叠加的当前配置 revision，保存时应原样带回。
	Revision   int64                            `json:"revision"`
	Evaluation CodexThirdPartyCatalogEvaluation `json:"evaluation"`
}

// CodexThirdPartySaveResult 是保存接口的返回内容。纯关闭不评估，Evaluation 为空。
type CodexThirdPartySaveResult struct {
	Config     CodexModelVisibilityConfig        `json:"config"`
	Evaluation *CodexThirdPartyCatalogEvaluation `json:"evaluation,omitempty"`
}

// CodexThirdPartyVerifyResult 是标记已验证接口的返回内容。
type CodexThirdPartyVerifyResult struct {
	Config CodexModelVisibilityConfig `json:"config"`
}

// CodexThirdPartyRefreshResult 是页面「刷新」接口的返回内容：逐账号的刷新结果，
// 加刷新之后重新读取的分组状态（与读取接口同结构，前端可直接替换）。
type CodexThirdPartyRefreshResult struct {
	Refresh CodexCatalogRefreshReport  `json:"refresh"`
	State   *CodexThirdPartyGroupState `json:"state"`
}

// CodexThirdPartySyncResult 是手动同步接口的返回内容。同步在后台进行，立即返回各账号状态。
type CodexThirdPartySyncResult struct {
	Sources []CodexThirdPartySourceStatus `json:"sources"`
}

// ---------------------------------------------------------------------------
// 服务
// ---------------------------------------------------------------------------

// CodexThirdPartyAdminService 是「Codex 三方模型启用」的管理服务。
type CodexThirdPartyAdminService struct {
	groups    codexThirdPartyGroupReader
	accounts  codexThirdPartyAccountLister
	syncer    codexThirdPartyUpstreamSyncer
	settings  codexThirdPartyAdminSettings
	store     CodexModelVisibilityStore
	evaluator CodexThirdPartyCatalogEvaluator

	now            func() time.Time
	refreshTimeout time.Duration
	saveTimeout    time.Duration

	// refreshSlots 是全局并发上限信号量。
	refreshSlots chan struct{}
	// refreshGroup 合并同一账号的并发刷新。
	refreshGroup singleflight.Group

	mu sync.Mutex
	// refreshing 记录后台正在刷新的账号，既用于避免重复起协程，也用于在状态里标记 refreshing。
	refreshing map[int64]struct{}
	// background 用于测试等待后台任务结束。
	background sync.WaitGroup
}

// codexThirdPartyAdminDeps 是构造服务所需的全部窄依赖。
type codexThirdPartyAdminDeps struct {
	Groups    codexThirdPartyGroupReader
	Accounts  codexThirdPartyAccountLister
	Syncer    codexThirdPartyUpstreamSyncer
	Settings  codexThirdPartyAdminSettings
	Store     CodexModelVisibilityStore
	Evaluator CodexThirdPartyCatalogEvaluator
}

// NewCodexThirdPartyAdminService 用现有具体类型装配管理服务，供依赖注入使用。
func NewCodexThirdPartyAdminService(
	settingService *SettingService,
	groupRepo GroupRepository,
	accountRepo AccountRepository,
	accountTestService *AccountTestService,
	store CodexModelVisibilityStore,
	evaluator CodexThirdPartyCatalogEvaluator,
) *CodexThirdPartyAdminService {
	return newCodexThirdPartyAdminService(codexThirdPartyAdminDeps{
		Groups:    groupRepo,
		Accounts:  accountRepo,
		Syncer:    accountTestService,
		Settings:  settingService,
		Store:     store,
		Evaluator: evaluator,
	})
}

func newCodexThirdPartyAdminService(deps codexThirdPartyAdminDeps) *CodexThirdPartyAdminService {
	return &CodexThirdPartyAdminService{
		groups:         deps.Groups,
		accounts:       deps.Accounts,
		syncer:         deps.Syncer,
		settings:       deps.Settings,
		store:          deps.Store,
		evaluator:      deps.Evaluator,
		now:            time.Now,
		refreshTimeout: codexThirdPartyRefreshTimeout,
		saveTimeout:    codexThirdPartySourceSaveTimeout,
		refreshSlots:   make(chan struct{}, codexThirdPartyRefreshConcurrency),
		refreshing:     make(map[int64]struct{}),
	}
}

// ---------------------------------------------------------------------------
// 读取
// ---------------------------------------------------------------------------

// GetGroupState 返回分组的展示配置、评估结果和源快照状态。
// 分组不存在返回 404 并顺带删除孤儿配置；平台不提供 Codex 在线目录返回 400。
func (s *CodexThirdPartyAdminService) GetGroupState(ctx context.Context, groupID int64) (*CodexThirdPartyGroupState, error) {
	group, err := s.loadEligibleGroup(ctx, groupID)
	if err != nil {
		return nil, err
	}
	cfg, err := s.loadConfig(ctx, groupID)
	if err != nil {
		return nil, err
	}
	accounts, err := s.listGroupAccounts(ctx, groupID)
	if err != nil {
		return nil, err
	}
	// 页面读取只查数据库：源快照状态只读展示，不触发后台刷新；评估使用离线模式，不发上游请求。
	sources := s.collectSources(ctx, accounts, codexSourceReadOnly)
	eval, err := s.evaluate(WithCodexThirdPartyOfflineEvaluation(ctx), group, cfg)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartyGroupState{
		GroupID:       group.ID,
		GroupName:     group.Name,
		GroupPlatform: group.Platform,
		Config:        cfg,
		Evaluation:    eval,
		Sources:       sources,
	}, nil
}

// ---------------------------------------------------------------------------
// 刷新页面依赖的上游目录
// ---------------------------------------------------------------------------

// RefreshCatalogs 同步刷新分组页面判定依赖的全部上游目录（按模型分流账号的协议目录、官方账号基础目录），
// 再按刷新后的缓存重新读取分组状态一并返回。同一分组短时间内重复刷新由评估器合并或节流。
// 单个账号拉取失败不算整体失败，逐账号结果在 Refresh 里。
func (s *CodexThirdPartyAdminService) RefreshCatalogs(ctx context.Context, groupID int64) (*CodexThirdPartyRefreshResult, error) {
	group, err := s.loadEligibleGroup(ctx, groupID)
	if err != nil {
		return nil, err
	}
	refresher, ok := s.evaluator.(CodexThirdPartyCatalogRefresher)
	if !ok {
		return nil, ErrCodexCatalogRefreshFailed.WithCause(errors.New("evaluator does not support catalog refresh"))
	}
	report, err := refresher.RefreshCodexThirdPartyCatalogs(ctx, group)
	if err != nil {
		var appErr *infraerrors.ApplicationError
		if errors.As(err, &appErr) {
			return nil, err
		}
		slog.Warn("codex_third_party_catalog_refresh_failed", "group_id", groupID, "error", err)
		return nil, ErrCodexCatalogRefreshFailed.WithCause(err)
	}
	state, err := s.GetGroupState(ctx, groupID)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartyRefreshResult{Refresh: report, State: state}, nil
}

// ---------------------------------------------------------------------------
// 预览
// ---------------------------------------------------------------------------

// PreviewChanges 在当前配置的副本上叠加变更并评估，返回含输入指纹的评估结果。
func (s *CodexThirdPartyAdminService) PreviewChanges(ctx context.Context, groupID int64, req CodexThirdPartyPreviewRequest) (*CodexThirdPartyPreviewResult, error) {
	group, err := s.loadEligibleGroup(ctx, groupID)
	if err != nil {
		return nil, err
	}
	changes, err := normalizeCodexThirdPartyChanges(req.Changes)
	if err != nil {
		return nil, err
	}
	cfg, err := s.loadConfig(ctx, groupID)
	if err != nil {
		return nil, err
	}
	if cfg.Revision != req.ExpectedRevision {
		return nil, ErrCodexModelVisibilityConflict
	}
	overlay, err := overlayCodexVisibilityChanges(cfg, changes)
	if err != nil {
		return nil, err
	}
	eval, err := s.evaluate(ctx, group, overlay)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartyPreviewResult{Revision: cfg.Revision, Evaluation: eval}, nil
}

// ---------------------------------------------------------------------------
// 保存
// ---------------------------------------------------------------------------

// SaveChanges 保存一批开关变更，整批原子提交，禁止半成功。
//
//   - 变更全部是关闭：不调用评估器，不依赖上游，只校验分组、平台与 revision 后原子保存；
//   - 含任何开启：重新评估叠加后的配置，输入指纹必须与预览一致，每个被开启的模型都必须能开启，
//     并且目录预算完整且不超限，否则整批拒绝。
func (s *CodexThirdPartyAdminService) SaveChanges(ctx context.Context, groupID int64, req CodexThirdPartySaveRequest) (*CodexThirdPartySaveResult, error) {
	group, err := s.loadEligibleGroup(ctx, groupID)
	if err != nil {
		return nil, err
	}
	changes, err := normalizeCodexThirdPartyChanges(req.Changes)
	if err != nil {
		return nil, err
	}
	cfg, err := s.loadConfig(ctx, groupID)
	if err != nil {
		return nil, err
	}
	if cfg.Revision != req.ExpectedRevision {
		return nil, ErrCodexModelVisibilityConflict
	}
	overlay, err := overlayCodexVisibilityChanges(cfg, changes)
	if err != nil {
		return nil, err
	}

	var evaluation *CodexThirdPartyCatalogEvaluation
	if codexThirdPartyHasEnable(changes) {
		eval, err := s.evaluate(ctx, group, overlay)
		if err != nil {
			return nil, err
		}
		if strings.TrimSpace(req.InputFingerprint) == "" || req.InputFingerprint != eval.InputFingerprint {
			return nil, ErrCodexCatalogPreviewStale
		}
		if err := checkCodexEnableChanges(eval, changes); err != nil {
			return nil, err
		}
		evaluation = &eval
	}

	written, err := s.store.CompareAndSwapCodexModelVisibility(ctx, groupID, cfg.Revision, overlay)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartySaveResult{Config: written, Evaluation: evaluation}, nil
}

// ---------------------------------------------------------------------------
// 标记已验证
// ---------------------------------------------------------------------------

// MarkVerified 把管理员的真实调用验收结果绑定到候选的当前指纹。
// 仅当该模型已开启且当前评估里该候选的指纹与请求一致时才写入，否则返回 409。
func (s *CodexThirdPartyAdminService) MarkVerified(ctx context.Context, groupID int64, req CodexThirdPartyVerifyRequest) (*CodexThirdPartyVerifyResult, error) {
	group, err := s.loadEligibleGroup(ctx, groupID)
	if err != nil {
		return nil, err
	}
	slug := req.Slug
	if err := validateCodexModelSlug(slug); err != nil {
		return nil, codexChangesInvalid("模型名不合法：" + codexTruncateForMessage(slug))
	}
	fingerprint := strings.TrimSpace(req.Fingerprint)
	if fingerprint == "" {
		return nil, infraerrors.BadRequest("CODEX_THIRD_PARTY_FINGERPRINT_REQUIRED", "缺少候选指纹")
	}
	note, err := normalizeCodexVerificationNote(req.Note)
	if err != nil {
		return nil, err
	}
	cfg, err := s.loadConfig(ctx, groupID)
	if err != nil {
		return nil, err
	}
	if cfg.Revision != req.ExpectedRevision {
		return nil, ErrCodexModelVisibilityConflict
	}
	if !cfg.IsEnabled(slug) {
		return nil, ErrCodexModelNotEnabled
	}
	// 候选指纹只取决于路由、协议和能力档案，与基础目录无关，离线评估即可。
	eval, err := s.evaluate(WithCodexThirdPartyOfflineEvaluation(ctx), group, cfg)
	if err != nil {
		return nil, err
	}
	candidate, ok := findCodexCandidate(eval.Candidates, slug)
	if !ok || candidate.Fingerprint == "" || candidate.Fingerprint != fingerprint {
		return nil, ErrCodexVerificationStale
	}

	next := cfg.Clone()
	entry := next.Models[slug]
	entry.Verification = &CodexModelVerification{
		Fingerprint: fingerprint,
		VerifiedAt:  s.now().UTC(),
		Note:        note,
	}
	next.Models[slug] = entry
	written, err := s.store.CompareAndSwapCodexModelVisibility(ctx, groupID, cfg.Revision, next)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartyVerifyResult{Config: written}, nil
}

// ---------------------------------------------------------------------------
// 手动同步
// ---------------------------------------------------------------------------

// SyncSources 对分组全部账号强制刷新源（异步），立即返回各账号状态。
func (s *CodexThirdPartyAdminService) SyncSources(ctx context.Context, groupID int64) (*CodexThirdPartySyncResult, error) {
	if _, err := s.loadEligibleGroup(ctx, groupID); err != nil {
		return nil, err
	}
	accounts, err := s.listGroupAccounts(ctx, groupID)
	if err != nil {
		return nil, err
	}
	return &CodexThirdPartySyncResult{Sources: s.collectSources(ctx, accounts, codexSourceForceRefresh)}, nil
}

// ---------------------------------------------------------------------------
// 分组、配置、评估的公共步骤
// ---------------------------------------------------------------------------

// loadEligibleGroup 读取分组并校验平台。分组不存在时顺带清理孤儿配置。
func (s *CodexThirdPartyAdminService) loadEligibleGroup(ctx context.Context, groupID int64) (*Group, error) {
	if groupID <= 0 {
		return nil, infraerrors.BadRequest("CODEX_THIRD_PARTY_GROUP_INVALID", "分组 ID 不合法")
	}
	group, err := s.groups.GetByIDLite(ctx, groupID)
	if err != nil {
		if errors.Is(err, ErrGroupNotFound) {
			s.cleanupOrphanConfig(ctx, groupID)
			return nil, ErrGroupNotFound
		}
		return nil, err
	}
	if group == nil {
		s.cleanupOrphanConfig(ctx, groupID)
		return nil, ErrGroupNotFound
	}
	if !CodexCatalogPlatformEligible(group.Platform) {
		return nil, ErrCodexThirdPartyGroupPlatformUnsupported
	}
	return group, nil
}

// cleanupOrphanConfig 惰性删除已删除分组遗留的展示配置。失败只记日志，不影响 404 的返回。
func (s *CodexThirdPartyAdminService) cleanupOrphanConfig(ctx context.Context, groupID int64) {
	if s.store == nil {
		return
	}
	if err := s.store.DeleteCodexModelVisibility(ctx, groupID); err != nil {
		slog.Warn("codex_third_party_orphan_config_cleanup_failed", "group_id", groupID, "error", err)
	}
}

// loadConfig 读取当前展示配置。配置不存在返回 revision 为 0 的空配置；已存在但损坏时返回错误，
// 绝不当作空配置继续，否则后续保存会覆盖掉无法解析的原内容。
func (s *CodexThirdPartyAdminService) loadConfig(ctx context.Context, groupID int64) (CodexModelVisibilityConfig, error) {
	cfg, found, err := s.settings.LoadCodexModelVisibility(ctx, groupID)
	if err != nil {
		if found {
			slog.Error("codex_third_party_config_unreadable", "group_id", groupID, "error", err)
			return CodexModelVisibilityConfig{}, ErrCodexModelVisibilityCorrupt.WithCause(err)
		}
		return CodexModelVisibilityConfig{}, err
	}
	return cfg, nil
}

// evaluate 调用评估器，并保证返回的候选列表不是 nil（序列化为 [] 而不是 null）。
func (s *CodexThirdPartyAdminService) evaluate(ctx context.Context, group *Group, cfg CodexModelVisibilityConfig) (CodexThirdPartyCatalogEvaluation, error) {
	if s.evaluator == nil {
		return CodexThirdPartyCatalogEvaluation{}, ErrCodexCatalogEvaluationFailed.WithCause(errors.New("evaluator is not configured"))
	}
	eval, err := s.evaluator.EvaluateCodexThirdPartyCatalog(ctx, group, cfg)
	if err != nil {
		var appErr *infraerrors.ApplicationError
		if errors.As(err, &appErr) {
			return CodexThirdPartyCatalogEvaluation{}, err
		}
		slog.Warn("codex_third_party_evaluation_failed", "group_id", group.ID, "error", err)
		return CodexThirdPartyCatalogEvaluation{}, ErrCodexCatalogEvaluationFailed.WithCause(err)
	}
	if eval.Candidates == nil {
		eval.Candidates = []CodexThirdPartyCandidate{}
	}
	return eval, nil
}

// ---------------------------------------------------------------------------
// 变更列表的校验与叠加
// ---------------------------------------------------------------------------

// normalizeCodexThirdPartyChanges 校验变更列表：非空、数量受限、slug 合法且不重复。
func normalizeCodexThirdPartyChanges(changes []CodexThirdPartyChange) ([]CodexThirdPartyChange, error) {
	if len(changes) == 0 {
		return nil, codexChangesInvalid("变更列表为空")
	}
	if len(changes) > codexModelVisibilityMaxModels {
		return nil, codexChangesInvalid(fmt.Sprintf("单次最多变更 %d 个模型", codexModelVisibilityMaxModels))
	}
	seen := make(map[string]struct{}, len(changes))
	out := make([]CodexThirdPartyChange, 0, len(changes))
	for _, change := range changes {
		if err := validateCodexModelSlug(change.Slug); err != nil {
			return nil, codexChangesInvalid("模型名不合法：" + codexTruncateForMessage(change.Slug))
		}
		if _, dup := seen[change.Slug]; dup {
			return nil, codexChangesInvalid("模型名重复：" + codexTruncateForMessage(change.Slug))
		}
		seen[change.Slug] = struct{}{}
		out = append(out, change)
	}
	return out, nil
}

// overlayCodexVisibilityChanges 在配置副本上叠加变更，原配置保持不变。
// 开启或已有验收记录的条目保留在配置里；关闭且没有验收记录的条目直接移除，
// 因为「未列出」与「已关闭」在语义上完全等价，这样配置不会随着反复开关而膨胀。
func overlayCodexVisibilityChanges(cfg CodexModelVisibilityConfig, changes []CodexThirdPartyChange) (CodexModelVisibilityConfig, error) {
	next := cfg.Clone()
	for _, change := range changes {
		entry := next.Models[change.Slug]
		entry.Enabled = change.Enabled
		if !change.Enabled && entry.Verification == nil {
			delete(next.Models, change.Slug)
			continue
		}
		next.Models[change.Slug] = entry
	}
	if err := next.Validate(); err != nil {
		return CodexModelVisibilityConfig{}, ErrCodexModelVisibilityInvalid.WithCause(err)
	}
	return next, nil
}

// codexChangesInvalid 返回带具体说明的变更列表错误；错误码不变，errors.Is 仍可匹配 ErrCodexThirdPartyChangesInvalid。
func codexChangesInvalid(detail string) error {
	return infraerrors.BadRequest(ErrCodexThirdPartyChangesInvalid.Reason, ErrCodexThirdPartyChangesInvalid.Message+"："+detail)
}

// codexTruncateForMessage 截断要回显给管理员的用户输入，避免超长字符串撑爆错误信息。
func codexTruncateForMessage(value string) string {
	runes := []rune(value)
	if len(runes) > 64 {
		return string(runes[:64]) + "…"
	}
	return value
}

func codexThirdPartyHasEnable(changes []CodexThirdPartyChange) bool {
	for _, change := range changes {
		if change.Enabled {
			return true
		}
	}
	return false
}

func findCodexCandidate(candidates []CodexThirdPartyCandidate, slug string) (CodexThirdPartyCandidate, bool) {
	for _, candidate := range candidates {
		if candidate.Slug == slug {
			return candidate, true
		}
	}
	return CodexThirdPartyCandidate{}, false
}

// checkCodexEnableChanges 核对每个被开启的模型是否满足发布条件，以及目录预算是否完整且不超限。
// 任何一项不满足都整批拒绝，逐项原因放进错误的 metadata：键为 model:<slug> 或 budget。
func checkCodexEnableChanges(eval CodexThirdPartyCatalogEvaluation, changes []CodexThirdPartyChange) error {
	reasons := make(map[string]string)
	for _, change := range changes {
		if !change.Enabled {
			continue
		}
		candidate, ok := findCodexCandidate(eval.Candidates, change.Slug)
		switch {
		case !ok:
			reasons["model:"+change.Slug] = "不在当前分组的候选列表中"
		case candidate.Native:
			reasons["model:"+change.Slug] = "原生 GPT 不受该开关控制"
		case !candidate.CanEnable:
			reason := strings.Join(candidate.Reasons, "；")
			if reason == "" {
				reason = fmt.Sprintf("当前不满足开启条件（状态：%s）", candidate.Status)
			}
			reasons["model:"+change.Slug] = reason
		}
	}
	budget := eval.Budget
	switch {
	case !budget.Complete:
		reason := "目录体积预算检查未完成，不能确认开启后不会超限"
		if detail := strings.TrimSpace(budget.Reason); detail != "" {
			reason += "：" + detail
		}
		reasons["budget"] = reason
	case budget.WorstCaseBytes > budget.LimitBytes:
		reasons["budget"] = fmt.Sprintf("开启后目录最坏体积 %d 字节，超过上限 %d 字节", budget.WorstCaseBytes, budget.LimitBytes)
	}
	if len(reasons) == 0 {
		return nil
	}
	return ErrCodexModelEnableRejected.WithMetadata(reasons)
}

// normalizeCodexVerificationNote 清理验收备注：去首尾空白、去掉控制字符、限制长度。
func normalizeCodexVerificationNote(note string) (string, error) {
	note = strings.TrimSpace(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) && r != '\n' {
			return -1
		}
		return r
	}, note))
	if len([]rune(note)) > codexThirdPartyVerifyNoteMaxRunes {
		return "", infraerrors.BadRequest("CODEX_THIRD_PARTY_NOTE_TOO_LONG",
			fmt.Sprintf("验收备注不能超过 %d 个字符", codexThirdPartyVerifyNoteMaxRunes))
	}
	return note, nil
}

// ---------------------------------------------------------------------------
// 账号源快照：状态汇总与后台刷新
// ---------------------------------------------------------------------------

// listGroupAccounts 返回分组内持久启用的账号，按账号 ID 排序保证输出稳定。
func (s *CodexThirdPartyAdminService) listGroupAccounts(ctx context.Context, groupID int64) ([]Account, error) {
	gid := groupID
	accounts, err := s.accounts.ListModelAvailabilityCandidates(ctx, &gid, domain.ConcretePlatformIDs(), false)
	if err != nil {
		return nil, err
	}
	sort.Slice(accounts, func(i, j int) bool { return accounts[i].ID < accounts[j].ID })
	return accounts, nil
}

// codexSourceMode 决定 collectSources 是否触发源刷新。
type codexSourceMode int

const (
	// codexSourceReadOnly 只读取快照状态，不触发任何刷新（页面读取）。
	codexSourceReadOnly codexSourceMode = iota
	// codexSourceForceRefresh 对非官方账号无条件刷新（手动同步）。
	codexSourceForceRefresh
)

// collectSources 汇总每个账号的源快照状态。只有手动同步（codexSourceForceRefresh）才会起后台刷新。
func (s *CodexThirdPartyAdminService) collectSources(ctx context.Context, accounts []Account, mode codexSourceMode) []CodexThirdPartySourceStatus {
	now := s.now()
	statuses := make([]CodexThirdPartySourceStatus, 0, len(accounts))
	for i := range accounts {
		account := accounts[i]
		if isCodexThirdPartyOfficialSourceAccount(&account) {
			// 官方 OpenAI 账号：不读快照、不刷新，只在列表里标明跳过原因。
			statuses = append(statuses, buildOfficialSkippedSourceStatus(&account))
			continue
		}
		snap, found, loadErr := s.settings.LoadCodexCatalogSource(ctx, account.ID)
		if loadErr != nil {
			// 快照损坏或读取失败：按「需要刷新」处理，刷新成功会覆盖损坏的内容。
			snap, found = CodexCatalogSourceSnapshot{}, false
		}
		if mode == codexSourceForceRefresh {
			s.startBackgroundRefresh(account)
		}
		status := s.buildSourceStatus(&account, snap, found, loadErr, now)
		s.applyModelCatalogStatus(&status, &account, now)
		statuses = append(statuses, status)
	}
	return statuses
}

// isCodexThirdPartyOfficialSourceAccount 判断是否官方 OpenAI 账号（ChatGPT 订阅 OAuth、SetupToken，
// 或指向官方 API 地址的 API Key）。判定与目录编排保持一致，直接复用现有判定函数，不另写一份。
func isCodexThirdPartyOfficialSourceAccount(account *Account) bool {
	return account != nil && (account.IsOpenAIOAuthLike() || isOfficialOpenAICodexAccount(account))
}

// buildOfficialSkippedSourceStatus 构造官方账号在源列表里的展示项：状态为 official_skipped，
// 不过期、没有同步记录，也不会出现「配置已变更」之类的提示。
func buildOfficialSkippedSourceStatus(account *Account) CodexThirdPartySourceStatus {
	return CodexThirdPartySourceStatus{
		AccountID:     account.ID,
		AccountName:   account.Name,
		Platform:      account.Platform,
		AccountStatus: account.Status,
		Status:        codexThirdPartySourceStatusOfficialSkipped,
	}
}

// applyModelCatalogStatus 让按模型分流账号的源状态与页面「刷新」写入的上游模型目录缓存一致：
// 目录已加载时以它为准（complete、模型数、加载时间、是否超过缓存有效期），不再显示 missing。
// 目录还没加载时保留快照给出的状态。
func (s *CodexThirdPartyAdminService) applyModelCatalogStatus(status *CodexThirdPartySourceStatus, account *Account, now time.Time) {
	reader, ok := s.evaluator.(codexThirdPartyModelCatalogReader)
	if !ok {
		return
	}
	needed, loaded, models, fetchedAt := reader.ModelCatalogCacheStatus(account)
	if !needed || !loaded {
		return
	}
	status.Status = string(CodexCatalogSourceComplete)
	status.ModelCount = models
	status.FingerprintMatches = true
	status.LastError = ""
	synced := fetchedAt
	status.SyncedAt, status.AttemptedAt = &synced, &synced
	status.Stale = now.Sub(fetchedAt) >= modelProtocolCatalogTTL
}

func (s *CodexThirdPartyAdminService) buildSourceStatus(account *Account, snap CodexCatalogSourceSnapshot, found bool, loadErr error, now time.Time) CodexThirdPartySourceStatus {
	status := CodexThirdPartySourceStatus{
		AccountID:     account.ID,
		AccountName:   account.Name,
		Platform:      account.Platform,
		AccountStatus: account.Status,
		Refreshing:    s.isRefreshing(account.ID),
	}
	switch {
	case loadErr != nil:
		status.Status = string(CodexCatalogSourceFailed)
		status.Stale = true
		status.LastError = "源快照无法读取，等待重新同步"
	case !found:
		status.Status = codexThirdPartySourceStatusMissing
		status.Stale = true
	default:
		status.Status = string(snap.Status)
		status.Stale = snap.Stale(now)
		status.FingerprintMatches = snap.Matches(account)
		status.ModelCount = len(snap.Models)
		status.LastError = snap.LastError
		if !snap.SyncedAt.IsZero() {
			synced := snap.SyncedAt
			status.SyncedAt = &synced
		}
		if !snap.AttemptedAt.IsZero() {
			attempted := snap.AttemptedAt
			status.AttemptedAt = &attempted
		}
	}
	return status
}

func (s *CodexThirdPartyAdminService) isRefreshing(accountID int64) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.refreshing[accountID]
	return ok
}

// startBackgroundRefresh 在后台刷新一个账号的源，立即返回。后台任务使用独立的上下文，
// 不受请求取消影响；同一账号已有任务在跑时直接合并。
func (s *CodexThirdPartyAdminService) startBackgroundRefresh(account Account) {
	if isCodexThirdPartyOfficialSourceAccount(&account) {
		return
	}
	s.mu.Lock()
	if _, running := s.refreshing[account.ID]; running {
		s.mu.Unlock()
		return
	}
	s.refreshing[account.ID] = struct{}{}
	s.background.Add(1)
	s.mu.Unlock()

	// 后台协程持有账号副本：上游同步会往 Extra 里写能力快照，不能和请求线程共享同一个 map。
	copied := account
	copied.Extra = maps.Clone(account.Extra)
	copied.Credentials = maps.Clone(account.Credentials)

	go func() {
		defer s.background.Done()
		defer func() {
			s.mu.Lock()
			delete(s.refreshing, copied.ID)
			s.mu.Unlock()
		}()
		defer func() {
			if r := recover(); r != nil {
				slog.Error("codex_third_party_source_refresh_panic", "account_id", copied.ID, "panic", fmt.Sprint(r))
			}
		}()
		if err := s.refreshSource(&copied); err != nil {
			slog.Warn("codex_third_party_source_refresh_failed", "account_id", copied.ID, "reason", err.Error())
		}
	}()
}

// waitForBackground 等待所有后台刷新结束，仅供测试使用。
func (s *CodexThirdPartyAdminService) waitForBackground() {
	s.background.Wait()
}

// refreshSource 同步刷新一个账号的源快照。同一账号的并发调用合并为一次上游同步。
// 返回的错误已经去除凭据和上游原始正文，可以直接记日志。
func (s *CodexThirdPartyAdminService) refreshSource(account *Account) error {
	if isCodexThirdPartyOfficialSourceAccount(account) {
		// 兜底：无论从哪条路径进来，官方账号都不发起同步。
		return nil
	}
	_, err, _ := s.refreshGroup.Do(strconv.FormatInt(account.ID, 10), func() (any, error) {
		return nil, s.doRefreshSource(account)
	})
	return err
}

func (s *CodexThirdPartyAdminService) doRefreshSource(account *Account) error {
	// 全局并发上限：排队等待不计入单账号超时。
	s.refreshSlots <- struct{}{}
	defer func() { <-s.refreshSlots }()

	// 后台任务不使用请求上下文，否则页面关闭或请求结束会把同步打断。
	fingerprint := CodexCatalogSourceFingerprint(account)
	syncCtx, cancel := context.WithTimeout(context.Background(), s.refreshTimeout)
	catalog, syncErr := s.syncer.SyncUpstreamModelCatalog(syncCtx, account)
	cancel()
	finished := s.now().UTC()
	if syncErr == nil && catalog == nil {
		syncErr = errors.New("upstream sync returned no result")
	}

	// 写快照用独立的短超时，并且在同步结束之后才创建：同步可能已经把 60 秒用完，
	// 失败记录仍然必须能落库。
	saveCtx, cancelSave := context.WithTimeout(context.Background(), s.saveTimeout)
	defer cancelSave()

	if syncErr != nil {
		// 失败时以库里最新的快照为准保留旧清单（其它节点可能刚写过）。
		previous, hasPrevious, loadErr := s.settings.LoadCodexCatalogSource(saveCtx, account.ID)
		if loadErr != nil {
			// 上一份快照损坏或读不出来：当作没有，本次结果会覆盖它。
			previous, hasPrevious = CodexCatalogSourceSnapshot{}, false
		}
		reason := safeCodexSourceError(syncErr)
		failed := CodexCatalogSourceSnapshot{
			AccountID: account.ID,
			Source:    CodexCatalogSourceUpstreamSync,
			Status:    CodexCatalogSourceFailed,
			// 失败只更新尝试时间与错误；上一份完整清单、同步时间和对应的源指纹原样保留，
			// 页面和目录仍可使用旧清单，只是被标记为过期。
			AttemptedAt: finished,
			LastError:   reason,
		}
		if hasPrevious {
			failed.Models = previous.Models
			failed.SyncedAt = previous.SyncedAt
			failed.SourceFingerprint = previous.SourceFingerprint
		}
		if err := s.settings.SaveCodexCatalogSource(saveCtx, failed); err != nil {
			return fmt.Errorf("%s (save failed snapshot: %w)", reason, err)
		}
		return errors.New(reason)
	}

	models := NormalizeCodexCatalogSourceModels(catalog.Models)
	status := CodexCatalogSourceComplete
	if len(models) == 0 {
		// 上游明确返回了空列表：这是真实的空结果，不是失败，也不保留旧清单。
		status = CodexCatalogSourceEmpty
	}
	snapshot := CodexCatalogSourceSnapshot{
		AccountID:         account.ID,
		Models:            models,
		Source:            CodexCatalogSourceUpstreamSync,
		Status:            status,
		SourceFingerprint: fingerprint,
		SyncedAt:          finished,
		AttemptedAt:       finished,
	}
	if err := s.settings.SaveCodexCatalogSource(saveCtx, snapshot); err != nil {
		return fmt.Errorf("save codex catalog source: %w", err)
	}
	return nil
}

// safeCodexSourceError 把同步错误转成可写入快照的简短说明：只使用同步层提供的安全消息，
// 不带凭据、请求地址或上游原始响应正文。
func safeCodexSourceError(err error) string {
	var syncErr *UpstreamModelSyncError
	var message string
	switch {
	case errors.As(err, &syncErr):
		message = syncErr.SafeMessage()
	case errors.Is(err, context.DeadlineExceeded):
		message = "同步超时"
	case errors.Is(err, context.Canceled):
		message = "同步已取消"
	default:
		message = "同步上游模型列表失败"
	}
	runes := []rune(strings.TrimSpace(message))
	if len(runes) > codexThirdPartyLastErrorMaxRunes {
		runes = runes[:codexThirdPartyLastErrorMaxRunes]
	}
	return string(runes)
}
