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
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"
)

// codexThirdPartyEvaluationBaseTimeout 是评估时获取单个基础目录的时限（管理接口可以比客户端目录请求宽松）。
const codexThirdPartyEvaluationBaseTimeout = 8 * time.Second

// codexThirdPartySettings 是目录编排读取配置与源快照所需的最小接口（*SettingService 实现）。
type codexThirdPartySettings interface {
	LoadCodexModelVisibility(ctx context.Context, groupID int64) (CodexModelVisibilityConfig, bool, error)
	LoadCodexCatalogSource(ctx context.Context, accountID int64) (CodexCatalogSourceSnapshot, bool, error)
}

// codexThirdPartyBaseFetcher 获取单个 OpenAI 账号的基础目录正文（已完成与请求路径相同的元数据补全与映射）。
type codexThirdPartyBaseFetcher interface {
	FetchBase(ctx context.Context, group *Group, account *Account, clientVersion string) ([]byte, error)
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

// CodexThirdPartyCatalogService 实现目录编排与契约里的 CodexThirdPartyCatalogEvaluator。
type CodexThirdPartyCatalogService struct {
	settings    codexThirdPartySettings
	accountRepo AccountRepository
	fetcher     codexThirdPartyBaseFetcher
	channels    *ChannelService
	resolver    *CompositeRouteResolver

	profiles    *codexThirdPartyProfileSet
	profilesErr error

	now         func() time.Time
	baseTimeout time.Duration
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

// BaseFetchContext 为目录请求里获取 OpenAI 基础目录加时限，保证整个响应留在客户端 5 秒总超时之内。
func (s *CodexThirdPartyCatalogService) BaseFetchContext(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, CodexCatalogBaseFetchBudget)
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
	if haveBase && !sess.hasThirdPartySource() {
		// 只有官方 OpenAI 账号：不可能出现第三方条目，原目录逐字节不变。
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

	bases := s.fetchBases(ctx, sess)
	baseSlugs := map[string]struct{}{}
	var baseOrder []string
	parsed := make([]*codexTPParsedBase, 0, len(bases))
	for _, base := range bases {
		if base.err != nil || len(base.body) == 0 {
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

	sources, sourceNotes, err := sess.discoverNames(ctx, baseSlugs)
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

	// 预算：对每个可能提供基础目录的账号取最终体积，取最坏值；纯第三方分组按空基础。
	worst, nativeCount, overBudget := 0, 0, false
	budget := &eval.Budget
	incomplete := false
	var reasons []string
	for _, base := range bases {
		budget.BaseAccountIDs = append(budget.BaseAccountIDs, base.accountID)
		if base.err != nil || len(base.body) == 0 {
			incomplete = true
			reasons = append(reasons, fmt.Sprintf("账号 %d 的基础目录不可得", base.accountID))
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
			// 有 OpenAI 账号但基础目录都取不到：只能按空基础估算，且标记未完成。
			incomplete = true
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

	eval.InputFingerprint = s.inputFingerprint(group, sess, bases, eval.Candidates, sourceNotes)
	return eval, nil
}

type codexTPParsedBase struct {
	id      int64
	env     *codexCatalogEnvelope
	entries []codexCatalogEntry
	body    []byte
}

// fetchBases 并发获取分组内全部可能提供基础目录的 OpenAI 账号的基础目录。
// 固定账号模式只取固定账号。没有 OpenAI 账号时返回空列表（纯第三方分组按空基础）。
func (s *CodexThirdPartyCatalogService) fetchBases(ctx context.Context, sess *codexTPSession) []codexTPBase {
	pinned := map[int64]struct{}{}
	pinnedOnly := sess.group.Platform == PlatformOpenAI && sess.group.CodexModelsManifestConfig.Enabled
	if pinnedOnly {
		for _, id := range sess.group.CodexModelsManifestConfig.AccountIDs {
			pinned[id] = struct{}{}
		}
	}
	var targets []*Account
	for i := range sess.accounts {
		account := &sess.accounts[i]
		if account.Platform != PlatformOpenAI {
			continue
		}
		if pinnedOnly {
			if _, ok := pinned[account.ID]; !ok {
				continue
			}
		}
		targets = append(targets, account)
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i].ID < targets[j].ID })
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
			fetchCtx, cancel := context.WithTimeout(ctx, s.baseTimeout)
			defer cancel()
			body, err := s.fetcher.FetchBase(fetchCtx, sess.group, account, version)
			results[i] = codexTPBase{accountID: account.ID, body: body, err: err}
		}()
	}
	wg.Wait()
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
	for _, slug := range enabled {
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
