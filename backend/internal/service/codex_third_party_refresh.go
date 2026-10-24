package service

// codex_third_party_refresh.go 实现「Codex 三方模型启用」页面依赖的上游目录刷新。
//
// 页面读取（离线评估）只用进程内缓存，不向上游发请求；网关重启后缓存是空的，页面会误判
// 「能力依据不足」。本文件提供一个共享函数，把页面判定依赖的上游目录一次性同步刷新：
//
//   - 按模型分流账号（CommandCode 这类）的上游模型目录（model_protocol_catalog 的进程内缓存），
//     决定账号对某个模型走 Responses 还是 Chat Completions，也决定零转换组；
//   - 官方 ChatGPT 账号的基础目录：体积预算要用，同时顺带刷新 upstreamListsModel 读取的账号能力快照。
//
// 三处入口共用它：
//   - 管理页右上角「刷新」按钮（全量刷新，带短时间合并与节流）；
//   - 预览与保存（含批量开启）时，缺目录缓存的账号先补拉再判定（只补缺失的协议目录）；
//   - 启动预热见 model_protocol_catalog_warmup.go，直接复用 refreshModelProtocolCatalog。
//
// 约束：只调用现有拉取函数（fetchModelProtocolCatalog、FetchBase），不新增上游请求形态；
// 单账号与总时限、并发上限、单账号失败互不影响，结果逐账号返回；失败原因只给分类说明，
// 不含地址、凭据与上游原始正文。

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const (
	// CodexCatalogRefreshKindModelProtocol 表示按模型分流账号的上游模型目录（协议目录）。
	CodexCatalogRefreshKindModelProtocol = "model_protocol"
	// CodexCatalogRefreshKindOfficialBase 表示官方 ChatGPT 账号的基础目录（体积预算与上游型号列表）。
	CodexCatalogRefreshKindOfficialBase = "official_base"

	// codexTPRefreshAccountTimeout 是单个按模型分流账号一次协议目录拉取的时限。
	// 官方账号基础目录沿用评估时限 codexThirdPartyEvaluationBaseTimeout，不另设。
	codexTPRefreshAccountTimeout = 8 * time.Second
	// codexTPRefreshTotalTimeout 是页面「刷新」一次刷新的总时限。
	codexTPRefreshTotalTimeout = 20 * time.Second
	// codexTPRefreshMissingTotalTimeout 是预览与保存时补拉缺失目录的总时限，比页面刷新短：
	// 开启操作在等这一步，拉不到就按「无法确认」处理。
	codexTPRefreshMissingTotalTimeout = 10 * time.Second
	// codexTPRefreshConcurrency 是同时拉取的账号数上限（协议目录与官方基础目录各自计）。
	codexTPRefreshConcurrency = 4
	// codexTPRefreshReuseWindow 是同一分组两次页面刷新之间的合并窗口：窗口内再点刷新直接复用上次结果，
	// 防止连点把上游打满。
	codexTPRefreshReuseWindow = 10 * time.Second

	// codexTPCatalogNotReadyReason 是上游模型目录没拉过（缓存缺失）时给管理员的可照做提示。
	codexTPCatalogNotReadyReason = "上游模型目录尚未就绪，点右上角「刷新」拉取后再试"
)

// CodexCatalogRefreshItem 是一个账号一类目录的刷新结果。
type CodexCatalogRefreshItem struct {
	AccountID   int64  `json:"account_id"`
	AccountName string `json:"account_name"`
	Platform    string `json:"platform"`
	// Kind 取 model_protocol（按模型分流账号的协议目录）或 official_base（官方账号基础目录）。
	Kind string `json:"kind"`
	OK   bool   `json:"ok"`
	// Reason 仅失败时有值：分类后的简短说明，不含地址、凭据与上游原始正文。
	Reason string `json:"reason,omitempty"`
	// ElapsedMS 是这个账号这一次拉取的耗时（毫秒）。
	ElapsedMS int64 `json:"elapsed_ms"`
	// ModelCount 是协议目录里的模型数（仅 model_protocol 成功时有值）。
	ModelCount int `json:"model_count,omitempty"`
}

// CodexCatalogRefreshReport 是一次刷新的逐账号结果与汇总。
type CodexCatalogRefreshReport struct {
	GroupID     int64     `json:"group_id"`
	RefreshedAt time.Time `json:"refreshed_at"`
	// Reused 为 true 表示命中了合并窗口，直接复用了上一次刷新的结果，没有再发上游请求。
	Reused bool                      `json:"reused"`
	Total  int                       `json:"total"`
	Failed int                       `json:"failed"`
	Items  []CodexCatalogRefreshItem `json:"items"`
}

// CodexThirdPartyCatalogRefresher 是评估器可选实现的刷新能力，管理服务通过它做页面「刷新」。
type CodexThirdPartyCatalogRefresher interface {
	RefreshCodexThirdPartyCatalogs(ctx context.Context, group *Group) (CodexCatalogRefreshReport, error)
}

var _ CodexThirdPartyCatalogRefresher = (*CodexThirdPartyCatalogService)(nil)

// codexTPRefreshTuning 是刷新的时限与并发配置，零值取上面的默认常量，测试可单独覆盖。
type codexTPRefreshTuning struct {
	accountTimeout time.Duration
	totalTimeout   time.Duration
	missingTotal   time.Duration
	concurrency    int
	reuseWindow    time.Duration
}

func (t codexTPRefreshTuning) withDefaults() codexTPRefreshTuning {
	if t.accountTimeout <= 0 {
		t.accountTimeout = codexTPRefreshAccountTimeout
	}
	if t.totalTimeout <= 0 {
		t.totalTimeout = codexTPRefreshTotalTimeout
	}
	if t.missingTotal <= 0 {
		t.missingTotal = codexTPRefreshMissingTotalTimeout
	}
	if t.concurrency <= 0 {
		t.concurrency = codexTPRefreshConcurrency
	}
	if t.reuseWindow <= 0 {
		t.reuseWindow = codexTPRefreshReuseWindow
	}
	return t
}

// codexTPRefreshMemo 记录分组最近一次页面刷新的结果，用于合并窗口内复用。
type codexTPRefreshMemo struct {
	at     time.Time
	report CodexCatalogRefreshReport
}

// codexTPRefreshMode 决定刷新哪些目录。
type codexTPRefreshMode int

const (
	// codexTPRefreshAll 页面「刷新」：协议目录一律重拉，并同步刷新官方账号基础目录。
	codexTPRefreshAll codexTPRefreshMode = iota
	// codexTPRefreshMissing 预览与保存补拉：只补缓存里缺失且不在退避期的协议目录；
	// 官方账号基础目录由评估本身拉取，这里不重复。
	codexTPRefreshMissing
)

// RefreshCodexThirdPartyCatalogs 同步刷新分组页面判定依赖的全部上游目录（页面「刷新」按钮）。
// 同一分组在合并窗口内重复调用直接复用上次结果；并发调用合并为一次执行。
// 刷新使用脱离请求取消的上下文：浏览器断开不会让正在进行的拉取半途而废，总时限仍然生效。
func (s *CodexThirdPartyCatalogService) RefreshCodexThirdPartyCatalogs(ctx context.Context, group *Group) (CodexCatalogRefreshReport, error) {
	if s == nil || group == nil {
		return CodexCatalogRefreshReport{}, fmt.Errorf("codex third party: group is required")
	}
	if !CodexCatalogPlatformEligible(group.Platform) {
		return CodexCatalogRefreshReport{GroupID: group.ID, RefreshedAt: s.now(), Items: []CodexCatalogRefreshItem{}}, nil
	}
	tuning := s.refreshTuning.withDefaults()
	if report, ok := s.recentRefresh(group.ID, tuning.reuseWindow); ok {
		return report, nil
	}
	value, err, _ := s.refreshFlight.Do(strconv.FormatInt(group.ID, 10), func() (any, error) {
		runCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), tuning.totalTimeout)
		defer cancel()
		sess, err := s.newSession(runCtx, group)
		if err != nil {
			return nil, err
		}
		report, err := s.runRefresh(runCtx, sess, codexTPRefreshAll)
		if err != nil {
			return nil, err
		}
		s.rememberRefresh(group.ID, report)
		return report, nil
	})
	if err != nil {
		return CodexCatalogRefreshReport{}, err
	}
	report, _ := value.(CodexCatalogRefreshReport)
	report.Items = slices.Clone(report.Items)
	return report, nil
}

// recentRefresh 返回合并窗口内的上次刷新结果（标记为复用）。
func (s *CodexThirdPartyCatalogService) recentRefresh(groupID int64, window time.Duration) (CodexCatalogRefreshReport, bool) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	memo, ok := s.refreshMemo[groupID]
	if !ok || s.now().Sub(memo.at) >= window {
		return CodexCatalogRefreshReport{}, false
	}
	report := memo.report
	report.Items = slices.Clone(report.Items)
	report.Reused = true
	return report, true
}

func (s *CodexThirdPartyCatalogService) rememberRefresh(groupID int64, report CodexCatalogRefreshReport) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	if s.refreshMemo == nil {
		s.refreshMemo = map[int64]codexTPRefreshMemo{}
	}
	stored := report
	stored.Items = slices.Clone(report.Items)
	s.refreshMemo[groupID] = codexTPRefreshMemo{at: s.now(), report: stored}
}

// refreshMissingModelCatalogs 在预览与保存评估前补拉缺失的协议目录：拉到了，后面的判定就能确认
// 协议；拉取失败只记日志，判定维持「无法确认」。受总时限约束，不改变评估的成败。
func (s *CodexThirdPartyCatalogService) refreshMissingModelCatalogs(ctx context.Context, sess *codexTPSession) {
	tuning := s.refreshTuning.withDefaults()
	runCtx, cancel := context.WithTimeout(ctx, tuning.missingTotal)
	defer cancel()
	report, err := s.runRefresh(runCtx, sess, codexTPRefreshMissing)
	if err != nil {
		slog.Warn("codex_third_party_catalog_prefetch_failed", "group_id", sess.group.ID, "reason", err.Error())
		return
	}
	for _, item := range report.Items {
		if !item.OK {
			slog.Warn("codex_third_party_catalog_prefetch_item_failed",
				"group_id", sess.group.ID, "account_id", item.AccountID, "kind", item.Kind, "reason", item.Reason)
		}
	}
}

// runRefresh 按模式刷新目录并汇总逐账号结果。协议目录与官方基础目录并行拉取，各自限并发。
func (s *CodexThirdPartyCatalogService) runRefresh(ctx context.Context, sess *codexTPSession, mode codexTPRefreshMode) (CodexCatalogRefreshReport, error) {
	tuning := s.refreshTuning.withDefaults()
	report := CodexCatalogRefreshReport{GroupID: sess.group.ID, Items: []CodexCatalogRefreshItem{}}
	targets, err := s.protocolCatalogTargets(ctx, sess, mode)
	if err != nil {
		return report, err
	}

	var (
		protocolItems []CodexCatalogRefreshItem
		officialItems []CodexCatalogRefreshItem
		wg            sync.WaitGroup
	)
	wg.Add(1)
	go func() {
		defer wg.Done()
		protocolItems = s.refreshProtocolCatalogs(ctx, targets, tuning)
	}()
	if mode == codexTPRefreshAll {
		wg.Add(1)
		go func() {
			defer wg.Done()
			officialItems = s.refreshOfficialBases(ctx, sess)
		}()
	}
	wg.Wait()

	report.Items = append(report.Items, protocolItems...)
	report.Items = append(report.Items, officialItems...)
	sort.SliceStable(report.Items, func(i, j int) bool {
		a, b := report.Items[i], report.Items[j]
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		return a.AccountID < b.AccountID
	})
	for _, item := range report.Items {
		report.Total++
		if !item.OK {
			report.Failed++
		}
	}
	report.RefreshedAt = s.now()
	return report, nil
}

// protocolCatalogTargets 收集需要拉协议目录的账号：分组内持久启用的、按模型分流且平台提供目录的账号。
// Composite 分组按各具体平台的账号收集（候选池跨平台承接），其它分组取同平台账号。
// 补拉模式只取缓存缺失且不在失败退避期的账号。
func (s *CodexThirdPartyCatalogService) protocolCatalogTargets(ctx context.Context, sess *codexTPSession, mode codexTPRefreshMode) ([]*Account, error) {
	refresher, ok := s.fetcher.(codexThirdPartyProtocolCatalogRefresher)
	if !ok {
		return nil, nil
	}
	var pool []*Account
	if sess.group.Platform == PlatformComposite {
		all, err := sess.allPlatformAccounts(ctx)
		if err != nil {
			return nil, err
		}
		pool = all
	} else {
		for i := range sess.accounts {
			pool = append(pool, &sess.accounts[i])
		}
	}
	now := time.Now()
	seen := map[int64]struct{}{}
	var targets []*Account
	for _, account := range pool {
		if account == nil || !compositeAccountPersistentlyEnabled(account) || !domain.UsesOpenAIGateway(account.Platform) || account.IsGrok() {
			continue
		}
		if _, dup := seen[account.ID]; dup {
			continue
		}
		state := refresher.ModelCatalogState(account)
		if !state.Needed {
			continue
		}
		if mode == codexTPRefreshMissing && (state.Loaded || now.Before(state.RetryAt)) {
			continue
		}
		seen[account.ID] = struct{}{}
		targets = append(targets, account)
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i].ID < targets[j].ID })
	return targets, nil
}

// refreshProtocolCatalogs 限并发、限时地逐账号同步拉取协议目录，单账号失败不影响其它账号。
func (s *CodexThirdPartyCatalogService) refreshProtocolCatalogs(ctx context.Context, targets []*Account, tuning codexTPRefreshTuning) []CodexCatalogRefreshItem {
	if len(targets) == 0 {
		return nil
	}
	refresher, ok := s.fetcher.(codexThirdPartyProtocolCatalogRefresher)
	if !ok {
		return nil
	}
	items := make([]CodexCatalogRefreshItem, len(targets))
	sem := make(chan struct{}, tuning.concurrency)
	var wg sync.WaitGroup
	for i, account := range targets {
		item := CodexCatalogRefreshItem{
			AccountID: account.ID, AccountName: account.Name, Platform: account.Platform,
			Kind: CodexCatalogRefreshKindModelProtocol,
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				item.Reason = codexTPRefreshReason(ctx.Err())
				items[i] = item
				return
			}
			defer func() { <-sem }()
			started := time.Now()
			defer func() {
				if r := recover(); r != nil {
					slog.Error("codex_third_party_catalog_refresh_panic", "account_id", account.ID, "panic", fmt.Sprint(r))
					item.OK, item.Reason = false, "刷新异常，详见日志"
				}
				item.ElapsedMS = time.Since(started).Milliseconds()
				items[i] = item
			}()
			accountCtx, cancel := context.WithTimeout(ctx, tuning.accountTimeout)
			defer cancel()
			count, err := refresher.RefreshModelCatalog(accountCtx, account)
			if err != nil {
				item.Reason = codexTPRefreshReason(err)
				return
			}
			item.OK, item.ModelCount = true, count
		}()
	}
	wg.Wait()
	return items
}

// refreshOfficialBases 复用评估时已有的限时并发拉取，刷新分组内官方 ChatGPT 账号的基础目录：
// 成功的目录会留存给页面读取（体积预算），并刷新 upstreamListsModel 读取的账号能力快照。
func (s *CodexThirdPartyCatalogService) refreshOfficialBases(ctx context.Context, sess *codexTPSession) []CodexCatalogRefreshItem {
	bases := s.fetchBases(ctx, sess, false)
	names := map[int64]*Account{}
	for i := range sess.accounts {
		names[sess.accounts[i].ID] = &sess.accounts[i]
	}
	items := make([]CodexCatalogRefreshItem, 0, len(bases))
	for _, base := range bases {
		if base.local {
			continue
		}
		item := CodexCatalogRefreshItem{
			AccountID: base.accountID, Kind: CodexCatalogRefreshKindOfficialBase,
			ElapsedMS: base.elapsed.Milliseconds(),
		}
		if account := names[base.accountID]; account != nil {
			item.AccountName, item.Platform = account.Name, account.Platform
		}
		if base.err != nil {
			item.Reason = codexTPRefreshReason(base.err)
			slog.Warn("codex_third_party_official_base_refresh_failed", "group_id", sess.group.ID, "account_id", base.accountID, "error", base.err)
		} else {
			item.OK = true
		}
		items = append(items, item)
	}
	return items
}

// modelCatalogNotReady 判断账号的协议判定是否因上游模型目录缺失而无法确认：
// 账号需要目录、目录从未成功加载。拉过但目录里没有该模型的情形不算（目录已就绪）。
func (s *CodexThirdPartyCatalogService) modelCatalogNotReady(account *Account) bool {
	refresher, ok := s.fetcher.(codexThirdPartyProtocolCatalogRefresher)
	if !ok || account == nil {
		return false
	}
	state := refresher.ModelCatalogState(account)
	return state.Needed && !state.Loaded
}

// codexTPHTTPStatusPattern 从协议目录拉取错误（"model list returned HTTP 401"）里取出上游状态码。
var codexTPHTTPStatusPattern = regexp.MustCompile(`(?i)returned http (\d{3})`)

// codexTPRefreshReason 把拉取错误归类成给管理员看的简短说明：不回显地址、凭据与上游原始正文。
func codexTPRefreshReason(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "拉取超时"
	}
	if errors.Is(err, context.Canceled) {
		return "刷新已取消"
	}
	message := strings.ToLower(err.Error())
	if match := codexTPHTTPStatusPattern.FindStringSubmatch(message); match != nil {
		return "上游返回 HTTP " + match[1]
	}
	switch {
	case strings.Contains(message, "supported_endpoints"):
		return "上游目录不含协议信息"
	case strings.Contains(message, "exceeds"):
		return "上游目录过大"
	case strings.Contains(message, "unavailable"):
		return "上游客户端不可用"
	}
	var appErr *infraerrors.ApplicationError
	if errors.As(err, &appErr) && appErr.Code >= 400 {
		return fmt.Sprintf("请求失败（HTTP %d）", appErr.Code)
	}
	return "拉取失败，详见日志"
}
