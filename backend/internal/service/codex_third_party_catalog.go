package service

// codex_third_party_catalog.go 是「Codex 三方模型启用」的目录编排：
//
//	读取分组策略 → 获取 OpenAI 基础条目或空基础 → 逐条解析真实路由并区分原生 GPT 与第三方
//	→ 应用能力档案 → 合并并统一过滤 → 检查最终体积。
//
// 约束（来自客户端契约，见方案第 2.3、6.1、6.2 节）：
//   - 最终正文必须不超过 CodexCatalogMaxBytes，超限降级为只发原生条目，不截断、不静默丢型号；
//   - 第三方阶段任何失败（配置读取或损坏、路由查询失败、能力异常）都降级为只发原生，不返回错误；
//     纯第三方分组降级为 {"models":[]}；
//   - 只有原生部分自身超限才返回 ErrCodexCatalogUnavailable；
//   - 目录读取不写库、不触发上游同步；
//   - 日志只含分组/账号 ID、公开型号、阶段、字节数、耗时、原因，不含密钥与提示词。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"
)

// codexThirdPartyEvaluationBaseTimeout 是评估时获取单个官方账号基础目录的时限。
// 预览与保存时对官方账号并发拉取，慢的账号直接跳过，不能让管理页等上一整个上游超时。
const codexThirdPartyEvaluationBaseTimeout = 3 * time.Second

// codexThirdPartyBaseMemoTTL 是评估期间留存的官方账号基础目录的有效期：页面读取（离线评估）只用它，
// 过期视为没有缓存。
const codexThirdPartyBaseMemoTTL = 10 * time.Minute

// errCodexBaseDeferred 表示离线评估时没有该官方账号的目录缓存，预算留到预览与保存时检查。
var errCodexBaseDeferred = errors.New("codex third party: base catalog deferred to enable-time check")

// codexThirdPartySettings 是目录编排读取展示配置所需的最小接口（*SettingService 实现）。
// 候选名称来自分组默认模型名单，不再读取账号源快照。
type codexThirdPartySettings interface {
	LoadCodexModelVisibility(ctx context.Context, groupID int64) (CodexModelVisibilityConfig, bool, error)
}

// codexThirdPartyBaseFetcher 获取单个 OpenAI 账号的基础目录正文（已完成与请求路径相同的元数据补全与映射）。
type codexThirdPartyBaseFetcher interface {
	FetchBase(ctx context.Context, group *Group, account *Account, clientVersion string) ([]byte, error)
	// MergeGroupModels 对基础目录执行运行时在映射之后的分组合并（追加账号别名、白名单过滤、下线模型过滤）。
	MergeGroupModels(ctx context.Context, group *Group, body []byte) ([]byte, error)
}

// codexThirdPartyProtocolCataloger 是可选能力：只读缓存地查询按模型分流账号的上游模型目录里
// 某型号支持的协议（不发请求、不触发刷新），供跨平台承接分组的零转换判定使用。
type codexThirdPartyProtocolCataloger interface {
	CachedModelProtocols(account *Account, model string) []string
}

// codexThirdPartyProtocolCatalogRefresher 是可选能力：查询并同步刷新按模型分流账号的上游模型目录。
// 页面「刷新」按钮、开启（预览与保存）补拉、启动预热共用它，请求形态与运行时后台刷新一致。
type codexThirdPartyProtocolCatalogRefresher interface {
	// ModelCatalogState 只读地返回账号的目录缓存状态（不发请求）。
	ModelCatalogState(account *Account) modelCatalogCacheState
	// RefreshModelCatalog 同步拉取账号的目录并写入缓存，返回目录里的模型数；trigger 只用于日志区分触发来源。
	RefreshModelCatalog(ctx context.Context, account *Account, trigger string) (int, error)
}

// codexThirdPartyUpstreamLister 是可选能力：判断某个账号最近一次拉到的上游目录是否列出了某型号。
type codexThirdPartyUpstreamLister interface {
	UpstreamListsModel(account *Account, model string) bool
}

// openAICodexBaseFetcher 用现有 OpenAI 网关 service 获取基础目录，沿用账号级缓存，只读不改账号状态。
type openAICodexBaseFetcher struct {
	svc *OpenAIGatewayService
}

func (f openAICodexBaseFetcher) FetchBase(ctx context.Context, group *Group, account *Account, clientVersion string) ([]byte, error) {
	if f.svc == nil {
		return nil, fmt.Errorf("openai gateway service unavailable")
	}
	manifest, err := f.svc.fetchCodexModelsManifest(ctx, account, clientVersion, "", false)
	if err != nil {
		return nil, err
	}
	if err := f.svc.CompleteAPIKeyCodexModelsManifestForClient(manifest, account); err != nil {
		return nil, err
	}
	if err := ApplyPinnedCodexModelsMapping(manifest, account, group); err != nil {
		return nil, err
	}
	return manifest.Body, nil
}

// MergeGroupModels 与运行时同序：映射之后执行 MergeGroupConfiguredCodexModels，使评估基础与请求基础一致。
func (f openAICodexBaseFetcher) MergeGroupModels(ctx context.Context, group *Group, body []byte) ([]byte, error) {
	if f.svc == nil {
		return nil, fmt.Errorf("openai gateway service unavailable")
	}
	manifest := &OpenAIModelsResponse{Body: body}
	if err := f.svc.MergeGroupConfiguredCodexModels(ctx, group, manifest, ""); err != nil {
		return nil, err
	}
	return manifest.Body, nil
}

// CachedModelProtocols 只读缓存地返回按模型分流账号的上游模型目录里 model 支持的协议；
// 目录没缓存时返回 nil，调用方按内置规则判定。与请求路径共用同一份目录缓存。
func (f openAICodexBaseFetcher) CachedModelProtocols(account *Account, model string) []string {
	if f.svc == nil || account == nil {
		return nil
	}
	return f.svc.modelCatalogProtocolsCached(account, model)
}

// ModelCatalogState 只读地返回账号的上游模型目录缓存状态。
func (f openAICodexBaseFetcher) ModelCatalogState(account *Account) modelCatalogCacheState {
	if f.svc == nil || account == nil {
		return modelCatalogCacheState{}
	}
	return f.svc.modelCatalogCacheStateOf(account)
}

// RefreshModelCatalog 同步拉取账号的上游模型目录并写入缓存。
func (f openAICodexBaseFetcher) RefreshModelCatalog(ctx context.Context, account *Account, trigger string) (int, error) {
	if f.svc == nil || account == nil {
		return 0, fmt.Errorf("openai gateway service unavailable")
	}
	return f.svc.refreshModelProtocolCatalog(ctx, account, trigger)
}

// UpstreamListsModel 读取账号能力快照（只读）：快照未过期且上游清单列出了该型号。
// 快照缺失、过期或未列出都返回 false，调用方回退到名称范围规则。
func (f openAICodexBaseFetcher) UpstreamListsModel(account *Account, model string) bool {
	if f.svc == nil || account == nil {
		return false
	}
	return f.svc.openaiModelCapabilities.codexTPUpstreamListed(account.ID, model, time.Now())
}

// codexTPUpstreamListed 判断账号最近一次拉到的上游目录（未过期）是否列出了该型号。
// 无论清单是否 remoteAuthoritative，快照里的型号都来自上游清单本身；未列出时无法证明上游不服务，
// 因此只把「已列出」当作正向证据，其余交给名称范围规则。
func (s *openAIModelCapabilitySnapshot) codexTPUpstreamListed(accountID int64, model string, now time.Time) bool {
	if s == nil || accountID <= 0 {
		return false
	}
	model = strings.ToLower(strings.TrimSpace(model))
	if model == "" {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	snapshot, ok := s.accounts[accountID]
	if !ok || !now.Before(snapshot.expiresAt) {
		return false
	}
	_, listed := snapshot.models[model]
	return listed
}

// CodexThirdPartyCatalogService 实现目录编排与契约里的 CodexThirdPartyCatalogEvaluator。
type CodexThirdPartyCatalogService struct {
	settings    codexThirdPartySettings
	accountRepo AccountRepository
	fetcher     codexThirdPartyBaseFetcher
	channels    *ChannelService
	resolver    *CompositeRouteResolver
	// groupModels 由网关 handler 注入，返回分组默认启用的模型名单（与普通模型列表一致）。
	groupModels CodexThirdPartyGroupModelLister

	profiles    *codexThirdPartyProfileSet
	profilesErr error

	now         func() time.Time
	baseTimeout time.Duration

	// baseMemo 留存预览与保存时拉到的官方账号基础目录，供页面读取（离线评估）使用。
	baseMu   sync.Mutex
	baseMemo map[string]codexTPBaseMemoEntry

	// 页面依赖目录刷新（见 codex_third_party_refresh.go）：数值为零时取默认值，便于测试覆盖。
	refreshTuning codexTPRefreshTuning
	refreshMu     sync.Mutex
	refreshMemo   map[int64]codexTPRefreshMemo
	refreshFlight singleflight.Group
}

type codexTPBaseMemoEntry struct {
	body []byte
	at   time.Time
}

func codexTPBaseMemoKey(groupID, accountID int64) string {
	return fmt.Sprintf("%d:%d", groupID, accountID)
}

func (s *CodexThirdPartyCatalogService) rememberBase(groupID, accountID int64, body []byte) {
	if len(body) == 0 {
		return
	}
	s.baseMu.Lock()
	defer s.baseMu.Unlock()
	if s.baseMemo == nil {
		s.baseMemo = map[string]codexTPBaseMemoEntry{}
	}
	s.baseMemo[codexTPBaseMemoKey(groupID, accountID)] = codexTPBaseMemoEntry{body: body, at: s.now()}
}

func (s *CodexThirdPartyCatalogService) recallBase(groupID, accountID int64) ([]byte, bool) {
	s.baseMu.Lock()
	defer s.baseMu.Unlock()
	entry, ok := s.baseMemo[codexTPBaseMemoKey(groupID, accountID)]
	if !ok || s.now().Sub(entry.at) > codexThirdPartyBaseMemoTTL {
		return nil, false
	}
	return entry.body, true
}

var _ CodexThirdPartyCatalogEvaluator = (*CodexThirdPartyCatalogService)(nil)

// NewCodexThirdPartyCatalogService 构造目录编排服务，由依赖装配统一调用。
// openai 用于获取 OpenAI 基础目录；channels 与 resolver 用于还原真实路由。
func NewCodexThirdPartyCatalogService(
	settings *SettingService,
	accountRepo AccountRepository,
	openai *OpenAIGatewayService,
	channels *ChannelService,
	resolver *CompositeRouteResolver,
) *CodexThirdPartyCatalogService {
	svc := &CodexThirdPartyCatalogService{
		settings:    settings,
		accountRepo: accountRepo,
		fetcher:     openAICodexBaseFetcher{svc: openai},
		channels:    channels,
		resolver:    resolver,
		now:         time.Now,
		baseTimeout: codexThirdPartyEvaluationBaseTimeout,
	}
	svc.profiles, svc.profilesErr = loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	if svc.profilesErr != nil {
		slog.Error("codex_third_party_profiles_load_failed", "stage", "profiles", "reason", svc.profilesErr.Error())
	}
	return svc
}

// ApplyToManifest 对 OpenAI 基础目录应用第三方策略，结果写回 manifest.Body（ETag 由出口函数按最终正文计算）。
func (s *CodexThirdPartyCatalogService) ApplyToManifest(ctx context.Context, group *Group, manifest *OpenAIModelsResponse) error {
	if s == nil || manifest == nil || manifest.NotModified || len(manifest.Body) == 0 {
		return nil
	}
	body, err := s.ApplyToBody(ctx, group, manifest.Body)
	if err != nil {
		return err
	}
	if !bytes.Equal(body, manifest.Body) {
		manifest.Body = body
		manifest.ETag = codexModelsManifestBodyETag(body)
	}
	return nil
}

// HasOpenAIBaseSource 判断分组内是否存在能提供 OpenAI 基础目录的账号。
func (s *CodexThirdPartyCatalogService) HasOpenAIBaseSource(ctx context.Context, group *Group) (bool, error) {
	accounts, err := s.loadGroupAccounts(ctx, group)
	if err != nil {
		return false, err
	}
	for i := range accounts {
		if accounts[i].Platform == PlatformOpenAI {
			return true, nil
		}
	}
	return false, nil
}

// BuildThirdPartyOnly 以空基础生成目录（Composite 分组没有可用 OpenAI 账号时使用）。
func (s *CodexThirdPartyCatalogService) BuildThirdPartyOnly(ctx context.Context, group *Group) ([]byte, error) {
	return s.ApplyToBody(ctx, group, nil)
}

// ApplyToBody 是目录编排入口。base 为空表示没有 OpenAI 基础目录。
//
// 返回的正文要么就是传入的 base（策略为空操作时逐字节不变），要么是合并后的新正文。
// 第三方阶段失败时降级为只发原生条目，不返回错误；只有原生部分自身超限才返回 ErrCodexCatalogUnavailable。
func (s *CodexThirdPartyCatalogService) ApplyToBody(ctx context.Context, group *Group, base []byte) ([]byte, error) {
	if s == nil || group == nil || !CodexCatalogPlatformEligible(group.Platform) {
		return base, nil
	}
	start := s.now()
	haveBase := len(base) > 0
	env := emptyCodexCatalogEnvelope()
	var entries []codexCatalogEntry
	if haveBase {
		var err error
		env, entries, err = parseCodexCatalogBody(base)
		if err != nil {
			// 基础正文无法解析时保持原样，不在这里制造新的失败。
			s.logStage(group, "parse_base", start, len(base), err)
			return base, nil
		}
	}

	sess, err := s.newSession(ctx, group)
	if err != nil {
		return s.degrade(ctx, group, env, entries, nil, haveBase, "accounts", start, err)
	}
	if haveBase && sess.officialOnlyOpenAIGroup() {
		// OpenAI 或 Composite 分组且账号全部是官方 OpenAI 账号（含没有账号的 OpenAI 分组）：
		// 不可能出现第三方条目，原目录逐字节不变。其它平台的分组一律走统一过滤。
		return base, nil
	}
	cfg, _, err := s.settings.LoadCodexModelVisibility(ctx, group.ID)
	if err != nil {
		return s.degrade(ctx, group, env, entries, sess, haveBase, "visibility_config", start, err)
	}
	compiled, err := sess.compile(ctx, env, entries, cfg)
	if err != nil {
		return s.degrade(ctx, group, env, entries, sess, haveBase, "routes", start, err)
	}
	if compiled.fullSize > CodexCatalogMaxBytes {
		s.logStage(group, "budget_exceeded", start, compiled.fullSize, fmt.Errorf("catalog exceeds %d bytes, third-party models withheld", CodexCatalogMaxBytes))
		return s.finishNativeOnly(ctx, group, env, entries, sess, haveBase, base, start)
	}
	if !compiled.changed && haveBase {
		return base, nil
	}
	s.logPublished(group, compiled, start)
	return compiled.body, nil
}

// finishNativeOnly 生成只含原生条目的正文，并检查原生部分自身是否超限。
func (s *CodexThirdPartyCatalogService) finishNativeOnly(ctx context.Context, group *Group, env *codexCatalogEnvelope, entries []codexCatalogEntry, sess *codexTPSession, haveBase bool, base []byte, start time.Time) ([]byte, error) {
	body, err := sess.nativeOnlyBody(ctx, env, entries)
	if err != nil {
		return s.degrade(ctx, group, env, entries, nil, haveBase, "routes", start, err)
	}
	if len(body) > CodexCatalogMaxBytes {
		s.logStage(group, "native_over_limit", start, len(body), ErrCodexCatalogUnavailable)
		return nil, ErrCodexCatalogUnavailable
	}
	if haveBase && bytes.Equal(body, base) {
		return base, nil
	}
	return body, nil
}

// degrade 在第三方阶段失败时只发原生条目。sess 为空（连账号都读不到）时无法确认归属，
// 退而按官方目录范围的名称判断保留原生条目；这只发生在降级路径上。
func (s *CodexThirdPartyCatalogService) degrade(ctx context.Context, group *Group, env *codexCatalogEnvelope, entries []codexCatalogEntry, sess *codexTPSession, haveBase bool, stage string, start time.Time, cause error) ([]byte, error) {
	s.logStage(group, stage, start, 0, cause)
	var body []byte
	var err error
	if sess != nil {
		body, err = sess.nativeOnlyBody(ctx, env, entries)
	}
	if sess == nil || err != nil {
		kept := make([]codexCatalogEntry, 0, len(entries))
		for _, entry := range entries {
			if entry.slug == "" || (group.Platform != PlatformComposite && group.Platform != PlatformOpenAI) {
				continue
			}
			if isOfficialOpenAICodexCatalogModel(entry.slug) {
				kept = append(kept, entry)
			}
		}
		raws := make([]json.RawMessage, 0, len(kept))
		for _, entry := range kept {
			raws = append(raws, entry.raw)
		}
		body, err = env.render(raws)
		if err != nil {
			return nil, ErrCodexCatalogUnavailable
		}
	}
	if len(body) > CodexCatalogMaxBytes {
		return nil, ErrCodexCatalogUnavailable
	}
	return body, nil
}

func (s *CodexThirdPartyCatalogService) logStage(group *Group, stage string, start time.Time, size int, cause error) {
	reason := ""
	if cause != nil {
		reason = cause.Error()
	}
	slog.Warn("codex_third_party_catalog_degraded",
		"group_id", group.ID, "stage", stage, "bytes", size,
		"elapsed_ms", s.now().Sub(start).Milliseconds(), "reason", reason)
}

func (s *CodexThirdPartyCatalogService) logPublished(group *Group, compiled *codexTPCompiled, start time.Time) {
	slog.Info("codex_third_party_catalog_applied",
		"group_id", group.ID, "stage", "publish", "bytes", len(compiled.body),
		"published", strings.Join(compiled.published, ","), "removed_count", compiled.removed,
		"elapsed_ms", s.now().Sub(start).Milliseconds())
}

// ---------------------------------------------------------------------------
// 评估器
// ---------------------------------------------------------------------------

type codexTPBase struct {
	accountID int64
	body      []byte
	err       error
	// folded 表示固定账号模式下该账号的目录已并入另一个账号的合并基础，自身不再单独计算。
	folded bool
	// local 表示这份基础目录是按账号映射本地生成的（Composite 分组没有可用官方账号），不属于任何账号。
	local bool
	// elapsed 是联网拉取这个账号目录的耗时（离线评估与并入合并基础的账号为零），供页面刷新结果展示。
	elapsed time.Duration
}

// EvaluateCodexThirdPartyCatalog 实现契约里的评估接口：发现候选、判定状态、计算最坏体积预算。
// 评估不写库，也不触发上游同步（基础目录经现有账号级缓存获取）。
func (s *CodexThirdPartyCatalogService) EvaluateCodexThirdPartyCatalog(ctx context.Context, group *Group, cfg CodexModelVisibilityConfig) (CodexThirdPartyCatalogEvaluation, error) {
	if s == nil || group == nil {
		return CodexThirdPartyCatalogEvaluation{}, fmt.Errorf("codex third party: group is required")
	}
	eval := CodexThirdPartyCatalogEvaluation{
		GroupID:       group.ID,
		GroupPlatform: group.Platform,
		Revision:      cfg.Revision,
		Candidates:    []CodexThirdPartyCandidate{},
		EvaluatedAt:   s.now(),
	}
	eval.Budget = CodexCatalogBudget{LimitBytes: CodexCatalogMaxBytes}
	if !CodexCatalogPlatformEligible(group.Platform) {
		eval.Budget.Reason = "该分组平台不提供 Codex 在线目录"
		eval.InputFingerprint = codexTPHashJSON(struct {
			Group    int64  `json:"group"`
			Platform string `json:"platform"`
		}{group.ID, group.Platform})
		return eval, nil
	}
	sess, err := s.newSession(ctx, group)
	if err != nil {
		return eval, err
	}

	offline := codexThirdPartyOfflineEvaluation(ctx)
	if !offline {
		// 预览与保存：按模型分流账号的上游模型目录缺缓存（例如网关刚重启）时先补拉，再判定协议；
		// 拉取失败只记日志，判定维持「无法确认」。页面读取（离线）不发请求。
		s.refreshMissingModelCatalogs(ctx, sess)
	}
	bases := s.fetchBases(ctx, sess, offline)
	if len(bases) == 0 && group.Platform == PlatformComposite {
		// Composite 分组没有可用的官方账号：运行时的基础目录是按账号映射本地生成的，预算取同一份。
		// 这一步只查数据库，页面读取的离线评估也能算，结果是完整的。
		localBody, lerr := s.buildCompositeLocalBase(ctx, group)
		if lerr != nil {
			return eval, lerr
		}
		bases = []codexTPBase{{body: localBody, local: true}}
	}
	baseSlugs := map[string]struct{}{}
	var baseOrder []string
	parsed := make([]*codexTPParsedBase, 0, len(bases))
	for _, base := range bases {
		if base.folded || base.err != nil || len(base.body) == 0 {
			continue
		}
		env, entries, perr := parseCodexCatalogBody(base.body)
		if perr != nil {
			continue
		}
		parsed = append(parsed, &codexTPParsedBase{id: base.accountID, env: env, entries: entries, body: base.body})
		for _, entry := range entries {
			if entry.slug == "" {
				continue
			}
			if _, seen := baseSlugs[entry.slug]; !seen {
				baseSlugs[entry.slug] = struct{}{}
				baseOrder = append(baseOrder, entry.slug)
			}
		}
	}

	sources, err := sess.discoverNames(ctx, baseSlugs)
	if err != nil {
		return eval, err
	}

	// 逐名称评估路由与发布条件。
	names := make([]string, 0, len(sources))
	for name := range sources {
		names = append(names, name)
	}
	sort.Strings(names)
	type rowState struct {
		candidate CodexThirdPartyCandidate
		pub       *codexTPPublication
	}
	rows := map[string]*rowState{}
	for _, name := range names {
		route, rerr := sess.route(ctx, name)
		if rerr != nil {
			return eval, rerr
		}
		if route.Native {
			continue
		}
		pub := s.publicationFor(group, route)
		entry := cfg.Models[name]
		candidate := CodexThirdPartyCandidate{
			Slug:           name,
			DisplayName:    name,
			Enabled:        entry.Enabled,
			CanEnable:      pub.CanPublish,
			Status:         pub.Status,
			Reasons:        append([]string(nil), pub.Reasons...),
			Sources:        sources[name],
			TargetPlatform: route.TargetPlatform,
			UpstreamModel:  route.UpstreamModel,
			AccountIDs:     route.AccountIDs,
			Capabilities:   pub.Summary,
			EntryBytes:     len(pub.Entry),
			Fingerprint:    pub.Fingerprint,
			// 以下两项仅跨平台候选池承接时有值，其余情形为空，序列化时省略。
			PrimaryAccount:    route.Primary,
			AlternateAccounts: route.Alternates,
		}
		if len(route.Protocols) == 1 {
			candidate.Protocol = route.Protocols[0]
		} else if len(route.Protocols) > 1 {
			candidate.Protocol = strings.Join(route.Protocols, "+")
		}
		if pub.Profile != nil {
			candidate.ProfileID = pub.Profile.ID
		}
		rows[name] = &rowState{candidate: candidate, pub: pub}
	}

	// 预算：对每个能取到基础目录的账号取最终体积，取最坏值；纯第三方分组按空基础。
	// 与运行时一致：某个账号拉取失败只是换号，不挡住整个分组；至少一个账号成功（或分组没有
	// OpenAI 平台账号）即视为检查完成，全部失败才标记未完成。
	worst, nativeCount, overBudget := 0, 0, false
	budget := &eval.Budget
	incomplete := false
	var reasons []string
	for _, base := range bases {
		if !base.local {
			budget.BaseAccountIDs = append(budget.BaseAccountIDs, base.accountID)
		}
	}
	sort.Slice(budget.BaseAccountIDs, func(i, j int) bool { return budget.BaseAccountIDs[i] < budget.BaseAccountIDs[j] })
	compileOne := func(env *codexCatalogEnvelope, entries []codexCatalogEntry) error {
		compiled, cerr := sess.compile(ctx, env, entries, cfg)
		if cerr != nil {
			return cerr
		}
		if compiled.fullSize > worst {
			worst = compiled.fullSize
		}
		if compiled.nativeCount > nativeCount {
			nativeCount = compiled.nativeCount
		}
		return nil
	}
	if len(parsed) == 0 {
		if len(bases) > 0 {
			// 有官方账号但基础目录全部取不到：只能按空基础估算，且标记未完成。
			incomplete = true
			deferred := true
			for _, base := range bases {
				if base.folded {
					continue
				}
				if !errors.Is(base.err, errCodexBaseDeferred) {
					deferred = false
				}
				reasons = append(reasons, fmt.Sprintf("账号 %d 的基础目录不可得", base.accountID))
			}
			if deferred {
				// 页面读取不发上游请求，也没有缓存：不是失败，预算留到开启时检查。
				budget.Deferred = true
				reasons = []string{"页面读取不请求官方账号目录，目录体积在开启时检查"}
			}
		}
		if cerr := compileOne(emptyCodexCatalogEnvelope(), nil); cerr != nil {
			return eval, cerr
		}
	}
	for _, item := range parsed {
		if cerr := compileOne(item.env, item.entries); cerr != nil {
			return eval, cerr
		}
	}
	budget.WorstCaseBytes = worst
	budget.RemainingBytes = CodexCatalogMaxBytes - worst
	budget.NativeCount = nativeCount
	budget.Complete = !incomplete
	if incomplete {
		budget.Reason = strings.Join(reasons, "；")
		if budget.Reason == "" {
			budget.Reason = "基础目录数据缺失，预算未完成"
		}
	}
	overBudget = worst > CodexCatalogMaxBytes

	// 汇总候选行：原生只读行在前（按基础目录顺序），第三方按 slug 排序。
	for _, slug := range baseOrder {
		route, rerr := sess.route(ctx, slug)
		if rerr != nil {
			return eval, rerr
		}
		if !route.Native {
			continue
		}
		eval.Candidates = append(eval.Candidates, CodexThirdPartyCandidate{
			Slug: slug, DisplayName: slug, Native: true, Published: true,
			Status:         CodexModelStatusNative,
			Sources:        []string{"base_catalog"},
			TargetPlatform: route.TargetPlatform,
			AccountIDs:     route.AccountIDs,
		})
	}
	// 配置里仍记着、但已不在分组默认模型名单里的模型：作为不可开启的行保留展示，
	// 让管理员能看到并关闭它们（名单里没有就不会发布）。
	var orphans []string
	orphanSet := map[string]struct{}{}
	for slug := range cfg.Models {
		if _, listed := sources[slug]; !listed && cfg.Models[slug].Enabled {
			orphans = append(orphans, slug)
		}
	}
	sort.Strings(orphans)
	for _, slug := range orphans {
		if _, inBase := baseSlugs[slug]; inBase {
			continue
		}
		rows[slug] = &rowState{candidate: CodexThirdPartyCandidate{
			Slug:        slug,
			DisplayName: slug,
			Enabled:     true,
			Status:      CodexModelStatusUnavailable,
			Reasons:     []string{"该模型已不在分组默认启用的模型列表中，不会发布；可关闭该开关"},
			Sources:     []string{},
		}}
		names = append(names, slug)
		orphanSet[slug] = struct{}{}
	}
	enabledCount := 0
	for _, name := range names {
		state, ok := rows[name]
		if !ok {
			continue
		}
		c := state.candidate
		if c.Enabled && c.CanEnable {
			enabledCount++
			if overBudget {
				c.Status = CodexModelStatusOverBudget
				c.Reasons = append(c.Reasons, "启用后最坏目录体积超过 1048576 字节，第三方条目已停发")
			} else {
				c.Published = true
			}
		}
		if c.Status == CodexModelStatusConfigurable {
			if entry := cfg.Models[name]; entry.Verification != nil && c.Fingerprint != "" && entry.Verification.Fingerprint == c.Fingerprint {
				c.Status = CodexModelStatusVerified
			}
		}
		eval.Candidates = append(eval.Candidates, c)
	}
	budget.EnabledThirdPartyCount = enabledCount

	// 指纹只取决于开关以外的输入：配置里遗留的「已不在名单中」的行依赖开关，不参与。
	fingerprintRows := make([]CodexThirdPartyCandidate, 0, len(eval.Candidates))
	for _, c := range eval.Candidates {
		if _, orphan := orphanSet[c.Slug]; !orphan || c.Native {
			fingerprintRows = append(fingerprintRows, c)
		}
	}
	eval.InputFingerprint = s.inputFingerprint(group, sess, bases, fingerprintRows)
	return eval, nil
}

type codexTPParsedBase struct {
	id      int64
	env     *codexCatalogEnvelope
	entries []codexCatalogEntry
	body    []byte
}

// fetchBases 并发获取分组内官方 OpenAI 账号（ChatGPT 订阅或官方地址）的基础目录。
// 中转站点本来就给不出原生 GPT 目录，一律不参与体积预算。固定账号模式只取固定账号里的官方账号。
// 没有官方账号时返回空列表（按空基础估算）。
//
// offline 为 true 时不发任何上游请求：只使用预览与保存时留下的目录缓存，没有缓存的账号标记为
// 「开启时检查」（errCodexBaseDeferred）。否则对每个官方账号限时并发拉取，失败的账号由调用方跳过。
func (s *CodexThirdPartyCatalogService) fetchBases(ctx context.Context, sess *codexTPSession, offline bool) []codexTPBase {
	pinned := map[int64]struct{}{}
	pinnedOnly := sess.group.Platform == PlatformOpenAI && sess.group.CodexModelsManifestConfig.Enabled
	if pinnedOnly {
		for _, id := range sess.group.CodexModelsManifestConfig.AccountIDs {
			pinned[id] = struct{}{}
		}
	}
	var targets []*Account
	if pinnedOnly {
		// 固定账号模式与运行时一致：按配置顺序取固定账号。
		byID := map[int64]*Account{}
		for i := range sess.accounts {
			byID[sess.accounts[i].ID] = &sess.accounts[i]
		}
		seen := map[int64]struct{}{}
		for _, id := range sess.group.CodexModelsManifestConfig.AccountIDs {
			account, ok := byID[id]
			if _, dup := seen[id]; !ok || dup || account.Platform != PlatformOpenAI || !isCodexThirdPartyOfficialSourceAccount(account) {
				continue
			}
			seen[id] = struct{}{}
			targets = append(targets, account)
		}
	} else {
		for i := range sess.accounts {
			if sess.accounts[i].Platform == PlatformOpenAI && isCodexThirdPartyOfficialSourceAccount(&sess.accounts[i]) {
				targets = append(targets, &sess.accounts[i])
			}
		}
		sort.Slice(targets, func(i, j int) bool { return targets[i].ID < targets[j].ID })
	}
	results := make([]codexTPBase, len(targets))
	version := resolveVerifiedCodexClientVersion()
	sem := make(chan struct{}, 4)
	var wg sync.WaitGroup
	for i, account := range targets {
		wg.Add(1)
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			if offline {
				if body, ok := s.recallBase(sess.group.ID, account.ID); ok {
					results[i] = codexTPBase{accountID: account.ID, body: body}
				} else {
					results[i] = codexTPBase{accountID: account.ID, err: errCodexBaseDeferred}
				}
				return
			}
			started := time.Now()
			fetchCtx, cancel := context.WithTimeout(ctx, s.baseTimeout)
			defer cancel()
			body, err := s.fetcher.FetchBase(fetchCtx, sess.group, account, version)
			if err == nil && !pinnedOnly {
				// 调度路径：每个账号的目录在映射之后各自执行分组合并，与运行时同序。
				body, err = s.fetcher.MergeGroupModels(fetchCtx, sess.group, body)
			}
			if err == nil {
				s.rememberBase(sess.group.ID, account.ID, body)
			}
			results[i] = codexTPBase{accountID: account.ID, body: body, err: err, elapsed: time.Since(started)}
		}()
	}
	wg.Wait()
	if pinnedOnly {
		results = s.foldPinnedBases(ctx, sess.group, results)
	}
	return results
}

// foldPinnedBases 与运行时的固定账号路径一致：各固定账号按配置顺序按 slug 合并成一份目录，
// 再整体执行一次分组合并。合并结果放在第一个成功的账号上，其余成功账号标记为已并入。
func (s *CodexThirdPartyCatalogService) foldPinnedBases(ctx context.Context, group *Group, results []codexTPBase) []codexTPBase {
	var bodies [][]byte
	first := -1
	for i := range results {
		if results[i].err != nil || len(results[i].body) == 0 {
			continue
		}
		if first < 0 {
			first = i
		}
		bodies = append(bodies, results[i].body)
	}
	if first < 0 {
		return results
	}
	merged, err := mergeCodexModelsManifestBodies(bodies)
	if err == nil {
		fetchCtx, cancel := context.WithTimeout(ctx, s.baseTimeout)
		merged, err = s.fetcher.MergeGroupModels(fetchCtx, group, merged)
		cancel()
	}
	for i := range results {
		if i != first && results[i].err == nil && len(results[i].body) > 0 {
			results[i].body = nil
			results[i].folded = true
		}
	}
	results[first].body = merged
	results[first].err = err
	return results
}

// ---------------------------------------------------------------------------
// 编译
// ---------------------------------------------------------------------------

// codexTPCompiled 是一次合并的结果。
type codexTPCompiled struct {
	body []byte
	// fullSize 是未做超限降级前的合并后体积（预算按它计算）。
	fullSize    int
	changed     bool
	published   []string
	removed     int
	nativeCount int
}

// compile 对一份基础条目应用配置：移除全部非原生条目，替换或追加已开启且满足发布条件的第三方条目。
// 返回 error 表示路由查询失败，调用方降级。
func (sess *codexTPSession) compile(ctx context.Context, env *codexCatalogEnvelope, entries []codexCatalogEntry, cfg CodexModelVisibilityConfig) (*codexTPCompiled, error) {
	nativeBySlug := map[string]bool{}
	removed, nativeCount := 0, 0
	for _, entry := range entries {
		if entry.slug == "" {
			continue
		}
		route, err := sess.route(ctx, entry.slug)
		if err != nil {
			return nil, err
		}
		nativeBySlug[entry.slug] = route.Native
	}
	for _, entry := range entries {
		if entry.slug == "" || nativeBySlug[entry.slug] {
			nativeCount++
			continue
		}
		removed++
	}

	published := map[string][]byte{}
	var order []string
	enabled := make([]string, 0, len(cfg.Models))
	for slug, entry := range cfg.Models {
		if entry.Enabled {
			enabled = append(enabled, slug)
		}
	}
	sort.Strings(enabled)
	var listed map[string]struct{}
	if len(enabled) > 0 {
		// 只有存在已开启的模型才需要名单，没有开启项的分组不付出这次查询的代价。
		var err error
		if _, listed, err = sess.groupModelNames(ctx); err != nil {
			return nil, err
		}
	}
	for _, slug := range enabled {
		if _, ok := listed[slug]; !ok {
			// 已不在分组默认启用的模型名单里（例如账号被停用、映射被改掉）：停发，配置保留。
			continue
		}
		route, err := sess.route(ctx, slug)
		if err != nil {
			return nil, err
		}
		if route.Native {
			continue
		}
		pub := sess.svc.publicationFor(sess.group, route)
		if !pub.CanPublish {
			continue
		}
		published[slug] = pub.Entry
		order = append(order, slug)
	}

	keep := func(entry codexCatalogEntry) bool { return entry.slug == "" || nativeBySlug[entry.slug] }
	merged := mergeCodexCatalogEntries(entries, keep, published, order)
	body, err := env.render(merged)
	if err != nil {
		return nil, err
	}
	return &codexTPCompiled{
		body:        body,
		fullSize:    len(body),
		changed:     removed > 0 || len(order) > 0,
		published:   order,
		removed:     removed,
		nativeCount: nativeCount,
	}, nil
}

// nativeOnlyBody 生成只含原生条目的正文（降级用）。
func (sess *codexTPSession) nativeOnlyBody(ctx context.Context, env *codexCatalogEnvelope, entries []codexCatalogEntry) ([]byte, error) {
	kept := make([]json.RawMessage, 0, len(entries))
	for _, entry := range entries {
		if entry.slug != "" {
			route, err := sess.route(ctx, entry.slug)
			if err != nil {
				return nil, err
			}
			if !route.Native {
				continue
			}
		}
		kept = append(kept, entry.raw)
	}
	return env.render(kept)
}
