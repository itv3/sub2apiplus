//go:build unit

package service

// 页面依赖目录刷新的单测：刷新函数（并发上限、单账号超时、部分失败、节流合并、跨平台账号收集）、
// 开启（预览与保存评估）补拉、DMIT 重启后场景复现、管理服务刷新入口、目录缓存的同步刷新。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai_compat"
	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// 夹具
// ---------------------------------------------------------------------------

// cxtpRefreshFetcher 在 cxtpFetcher 之上加上协议目录刷新能力的假实现：状态与刷新结果完全可编程。
type cxtpRefreshFetcher struct {
	*cxtpFetcher
	mu     sync.Mutex
	states map[int64]modelCatalogCacheState
	// refresh 是单账号刷新的实现；为空时直接成功并返回 12 个模型。
	refresh     func(ctx context.Context, account *Account) (int, error)
	calls       map[int64]int
	triggers    []string
	inflight    int
	maxInflight int
}

func newCXTPRefreshFetcher() *cxtpRefreshFetcher {
	return &cxtpRefreshFetcher{
		cxtpFetcher: &cxtpFetcher{bodies: map[int64][]byte{}, errs: map[int64]error{}},
		states:      map[int64]modelCatalogCacheState{},
		calls:       map[int64]int{},
	}
}

func (f *cxtpRefreshFetcher) ModelCatalogState(account *Account) modelCatalogCacheState {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.states[account.ID]
}

func (f *cxtpRefreshFetcher) RefreshModelCatalog(ctx context.Context, account *Account, trigger string) (int, error) {
	f.mu.Lock()
	f.triggers = append(f.triggers, trigger)
	f.mu.Unlock()
	f.mu.Lock()
	f.calls[account.ID]++
	f.inflight++
	if f.inflight > f.maxInflight {
		f.maxInflight = f.inflight
	}
	refresh := f.refresh
	f.mu.Unlock()
	defer func() {
		f.mu.Lock()
		f.inflight--
		f.mu.Unlock()
	}()
	if refresh == nil {
		return 12, nil
	}
	return refresh(ctx, account)
}

func (f *cxtpRefreshFetcher) callCount(id int64) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[id]
}

func (f *cxtpRefreshFetcher) totalCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	total := 0
	for _, n := range f.calls {
		total += n
	}
	return total
}

// catalogState 只读地取目录是否已加载与失败退避的截止时间。
func catalogState(c *modelProtocolCatalog, key string) (loaded bool, retryAt time.Time) {
	loaded, retryAt, _, _ = c.info(key)
	return loaded, retryAt
}

// cxtpRefreshCommandCode 构造持久启用、按模型分流的 CommandCode 账号（需要上游模型目录）。
func cxtpRefreshCommandCode(id int64, name string) Account {
	account := compositePoolAccount(id, PlatformCommandCode, int(id), map[string]any{
		"api_protocol":  APIProtocolAdaptive,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	account.Name = name
	return account
}

// cxtpRefreshService 构造 Composite 分组的目录服务并换上可编程的刷新夹具；返回可调的时钟指针。
func cxtpRefreshService(t *testing.T, accounts []Account, fetcher *cxtpRefreshFetcher) (*CodexThirdPartyCatalogService, *time.Time) {
	t.Helper()
	svc := cxtpPoolService(t, accounts)
	svc.fetcher = fetcher
	clock := time.Unix(1_700_000_000, 0).UTC()
	svc.now = func() time.Time { return clock }
	return svc, &clock
}

func cxtpRefreshItemsByKey(report CodexCatalogRefreshReport) map[string]CodexCatalogRefreshItem {
	out := map[string]CodexCatalogRefreshItem{}
	for _, item := range report.Items {
		out[fmt.Sprintf("%s:%d", item.Kind, item.AccountID)] = item
	}
	return out
}

// ---------------------------------------------------------------------------
// 目录缓存的同步刷新
// ---------------------------------------------------------------------------

func TestModelProtocolCatalogRefreshNowStoresAndReportsState(t *testing.T) {
	var catalog modelProtocolCatalog
	const key = "https://catalog.example/v1/models#account=1"
	loaded, retryAt := catalogState(&catalog, key)
	require.False(t, loaded)
	require.True(t, retryAt.IsZero())
	_, created := catalog.entries[key]
	require.False(t, created, "只读查询状态不创建条目")

	models := map[string][]string{"gpt-5.5": {APIProtocolResponses}}
	count, err := catalog.refreshNow(t.Context(), key, func(context.Context) (map[string][]string, error) { return models, nil })
	require.NoError(t, err)
	require.Equal(t, 1, count)
	loaded, retryAt = catalogState(&catalog, key)
	require.True(t, loaded)
	require.True(t, retryAt.IsZero())
	require.Equal(t, []string{APIProtocolResponses}, catalog.peek(key, "GPT-5.5"))
}

func TestModelProtocolCatalogRefreshNowFailureKeepsOldCatalogWithShortBackoff(t *testing.T) {
	var catalog modelProtocolCatalog
	const key = "https://catalog.example/v1/models#account=2"
	models := map[string][]string{"gpt-5.5": {APIProtocolResponses}}
	catalog.store(key, models, nil, time.Now())

	before := time.Now()
	_, err := catalog.refreshNow(t.Context(), key, func(context.Context) (map[string][]string, error) { return nil, errors.New("boom") })
	require.Error(t, err)
	require.Equal(t, []string{APIProtocolResponses}, catalog.peek(key, "gpt-5.5"), "失败保留旧目录")
	loaded, retryAt := catalogState(&catalog, key)
	require.True(t, loaded)
	require.WithinRange(t, retryAt, before.Add(modelProtocolCatalogManualRetryBackoff), time.Now().Add(modelProtocolCatalogManualRetryBackoff),
		"手动刷新失败只进短退避，不压住运行时 5 分钟")
	require.Less(t, modelProtocolCatalogManualRetryBackoff, modelProtocolCatalogRetryBackoff)

	// 已经有更晚的运行时退避时，手动失败不缩短它。
	catalog.store(key, nil, errors.New("runtime failure"), time.Now())
	_, laterRetry := catalogState(&catalog, key)
	_, err = catalog.refreshNow(t.Context(), key, func(context.Context) (map[string][]string, error) { return nil, errors.New("boom") })
	require.Error(t, err)
	_, afterManual := catalogState(&catalog, key)
	require.False(t, afterManual.Before(laterRetry))
}

func TestModelProtocolCatalogRefreshNowWaitsForInFlightRefresh(t *testing.T) {
	var catalog modelProtocolCatalog
	const key = "https://catalog.example/v1/models#account=3"
	models := map[string][]string{"gpt-5.5": {APIProtocolResponses}}
	release := make(chan struct{})
	var fetches atomic.Int32
	fetch := func(context.Context) (map[string][]string, error) {
		fetches.Add(1)
		<-release
		return models, nil
	}
	results := make(chan error, 2)
	go func() { _, err := catalog.refreshNow(t.Context(), key, fetch); results <- err }()
	require.Eventually(t, func() bool { return fetches.Load() == 1 }, time.Second, time.Millisecond)
	go func() { _, err := catalog.refreshNow(t.Context(), key, fetch); results <- err }()
	time.Sleep(20 * time.Millisecond)
	close(release)
	require.NoError(t, <-results)
	require.NoError(t, <-results)
	require.EqualValues(t, 1, fetches.Load(), "进行中的刷新被后来者复用，不重复请求")

	// 等待者的上下文取消时立即返回，不拖住调用方。
	catalog.store("busy", nil, errors.New("x"), time.Now())
	catalog.mu.Lock()
	entry := catalog.entryLocked("busy")
	entry.refreshing = true
	entry.done = make(chan struct{})
	catalog.mu.Unlock()
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	_, err := catalog.refreshNow(ctx, "busy", fetch)
	require.ErrorIs(t, err, context.Canceled)
}

func TestModelProtocolCatalogRefreshNowReportsTheWaitedRefreshResult(t *testing.T) {
	// 等待进行中的后台刷新：以那一次刷新的实际结果为准。后台刷新失败时旧目录会保留，
	// 但这次刷新不能被报成成功。
	const key = "https://catalog.example/v1/models#account=7"
	models := map[string][]string{"gpt-5.5": {APIProtocolResponses}}
	neverFetch := func(context.Context) (map[string][]string, error) {
		t.Fatal("已有刷新在进行时不应再发起拉取")
		return nil, nil
	}
	startInFlight := func(catalog *modelProtocolCatalog) {
		catalog.mu.Lock()
		entry := catalog.entryLocked(key)
		entry.refreshing = true
		entry.done = make(chan struct{})
		catalog.mu.Unlock()
	}

	t.Run("后台刷新失败：返回失败，旧目录保留", func(t *testing.T) {
		var catalog modelProtocolCatalog
		catalog.store(key, models, nil, time.Now())
		startInFlight(&catalog)
		result := make(chan error, 1)
		go func() { _, err := catalog.refreshNow(t.Context(), key, neverFetch); result <- err }()
		time.Sleep(20 * time.Millisecond)
		catalog.store(key, nil, errors.New("model list returned HTTP 502"), time.Now())
		err := <-result
		require.Error(t, err, "旧目录还在，但等待的那次刷新失败了")
		require.Equal(t, "上游返回 HTTP 502", codexTPRefreshReason(err), "对外只给分类文案")
		require.Equal(t, []string{APIProtocolResponses}, catalog.peek(key, "gpt-5.5"), "失败仍保留旧目录")
	})

	t.Run("后台刷新成功：返回成功与模型数", func(t *testing.T) {
		var catalog modelProtocolCatalog
		catalog.store(key, map[string][]string{"old": {APIProtocolResponses}}, nil, time.Now())
		startInFlight(&catalog)
		type outcome struct {
			count int
			err   error
		}
		result := make(chan outcome, 1)
		go func() { count, err := catalog.refreshNow(t.Context(), key, neverFetch); result <- outcome{count, err} }()
		time.Sleep(20 * time.Millisecond)
		catalog.store(key, map[string][]string{"a": {APIProtocolResponses}, "b": {APIProtocolChatCompletions}}, nil, time.Now())
		got := <-result
		require.NoError(t, got.err)
		require.Equal(t, 2, got.count)
	})

	t.Run("后台刷新失败后，下一次手动刷新仍可重新拉取", func(t *testing.T) {
		var catalog modelProtocolCatalog
		catalog.store(key, nil, errors.New("boom"), time.Now())
		count, err := catalog.refreshNow(t.Context(), key, func(context.Context) (map[string][]string, error) { return models, nil })
		require.NoError(t, err)
		require.Equal(t, 1, count)
	})
}

func TestModelCatalogEndpointKeepsRequiringAModel(t *testing.T) {
	account := cxtpRefreshCommandCode(11, "CommandCode")
	_, _, ok := modelCatalogEndpoint(&account, "  ")
	require.False(t, ok, "原有判定对空模型名仍返回 false")
	url, key, ok := accountModelCatalogEndpoint(&account)
	require.True(t, ok)
	url2, key2, ok2 := modelCatalogEndpoint(&account, compositePoolModel)
	require.True(t, ok2)
	require.Equal(t, url, url2)
	require.Equal(t, key, key2)

	deepseek := compositePoolAdaptive(10, PlatformDeepseek, 1)
	_, _, ok = accountModelCatalogEndpoint(&deepseek)
	require.False(t, ok, "不按模型分流的账号不需要目录")
}

// ---------------------------------------------------------------------------
// 刷新函数
// ---------------------------------------------------------------------------

func TestCodexTPRefreshCompositeCollectsModelRoutedAccountsAcrossPlatforms(t *testing.T) {
	disabled := cxtpRefreshCommandCode(12, "停用的 CommandCode")
	disabled.Schedulable = false
	official := cxtpOAuthAccount(20)
	official.Name = "官方 ChatGPT"
	accounts := []Account{
		cxtpRefreshCommandCode(11, "CommandCode"),
		disabled,
		compositePoolAdaptive(10, PlatformDeepseek, 1),
		cxtpPoolChatOnly(14, PlatformCline, 5, compositePoolMapping(compositePoolModel)),
		official,
	}
	fetcher := newCXTPRefreshFetcher()
	for _, id := range []int64{11, 12} {
		fetcher.states[id] = modelCatalogCacheState{Needed: true}
	}
	fetcher.bodies[20] = []byte(`{"models":[{"slug":"gpt-5.5"}]}`)
	svc, _ := cxtpRefreshService(t, accounts, fetcher)

	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.False(t, report.Reused)
	require.Equal(t, 2, report.Total)
	require.Zero(t, report.Failed)
	items := cxtpRefreshItemsByKey(report)
	require.Contains(t, items, "model_protocol:11")
	require.NotContains(t, items, "model_protocol:12", "持久停用的账号不刷新")
	require.NotContains(t, items, "model_protocol:10", "不按模型分流的平台不需要协议目录")
	require.NotContains(t, items, "model_protocol:14")
	require.Equal(t, CodexCatalogRefreshItem{
		AccountID: 11, AccountName: "CommandCode", Platform: PlatformCommandCode,
		Kind: CodexCatalogRefreshKindModelProtocol, OK: true, ModelCount: 12, ElapsedMS: items["model_protocol:11"].ElapsedMS,
	}, items["model_protocol:11"])
	official20 := items["official_base:20"]
	require.True(t, official20.OK)
	require.Equal(t, "官方 ChatGPT", official20.AccountName)
	require.Equal(t, []int64{20}, fetcher.fetchedIDs(), "官方账号基础目录经评估时已有的限时并发拉取")
	require.Zero(t, fetcher.callCount(12))
	_, remembered := svc.recallBase(8, 20)
	require.True(t, remembered, "拉到的基础目录留存给页面读取（体积预算）")
}

func TestCodexTPRefreshNonCompositeGroupUsesGroupAccounts(t *testing.T) {
	accounts := []Account{
		cxtpRefreshCommandCode(11, "CommandCode"),
		compositePoolAdaptive(10, PlatformDeepseek, 1),
	}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)

	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), &Group{ID: 9, Platform: PlatformCommandCode})
	require.NoError(t, err)
	require.Equal(t, 1, report.Total)
	require.Equal(t, int64(11), report.Items[0].AccountID)
	require.Equal(t, int64(9), report.GroupID)

	// 平台不提供 Codex 在线目录的分组：不做任何事。
	report, err = svc.RefreshCodexThirdPartyCatalogs(t.Context(), &Group{ID: 10, Platform: PlatformAnthropic})
	require.NoError(t, err)
	require.Empty(t, report.Items)
	require.Equal(t, 1, fetcher.totalCalls())
}

func TestCodexTPRefreshHonorsConcurrencyLimit(t *testing.T) {
	var accounts []Account
	fetcher := newCXTPRefreshFetcher()
	for id := int64(21); id <= 26; id++ {
		accounts = append(accounts, cxtpRefreshCommandCode(id, fmt.Sprintf("cc-%d", id)))
		fetcher.states[id] = modelCatalogCacheState{Needed: true}
	}
	fetcher.refresh = func(ctx context.Context, _ *Account) (int, error) {
		time.Sleep(30 * time.Millisecond)
		return 3, nil
	}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)
	svc.refreshTuning.concurrency = 2

	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Equal(t, 6, report.Total)
	require.Zero(t, report.Failed)
	require.LessOrEqual(t, fetcher.maxInflight, 2, "同时拉取的账号数不超过并发上限")
	require.Equal(t, 2, fetcher.maxInflight, "上限内确实并行了")
}

func TestCodexTPRefreshAccountTimeoutDoesNotBlockOthers(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "慢账号"), cxtpRefreshCommandCode(12, "正常账号")}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	fetcher.states[12] = modelCatalogCacheState{Needed: true}
	fetcher.refresh = func(ctx context.Context, account *Account) (int, error) {
		if account.ID == 11 {
			<-ctx.Done()
			return 0, ctx.Err()
		}
		return 7, nil
	}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)
	svc.refreshTuning.accountTimeout = 60 * time.Millisecond

	started := time.Now()
	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Less(t, time.Since(started), 5*time.Second)
	items := cxtpRefreshItemsByKey(report)
	require.False(t, items["model_protocol:11"].OK)
	require.Equal(t, "拉取超时", items["model_protocol:11"].Reason)
	require.GreaterOrEqual(t, items["model_protocol:11"].ElapsedMS, int64(50))
	require.True(t, items["model_protocol:12"].OK)
	require.Equal(t, 7, items["model_protocol:12"].ModelCount)
	require.Equal(t, 1, report.Failed)
}

func TestCodexTPRefreshTotalTimeoutBoundsTheWholeRun(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "a"), cxtpRefreshCommandCode(12, "b"), cxtpRefreshCommandCode(13, "c")}
	fetcher := newCXTPRefreshFetcher()
	for _, account := range accounts {
		fetcher.states[account.ID] = modelCatalogCacheState{Needed: true}
	}
	fetcher.refresh = func(ctx context.Context, _ *Account) (int, error) {
		<-ctx.Done()
		return 0, ctx.Err()
	}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)
	svc.refreshTuning = codexTPRefreshTuning{accountTimeout: time.Minute, totalTimeout: 80 * time.Millisecond, concurrency: 1}

	started := time.Now()
	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Less(t, time.Since(started), 5*time.Second, "总时限到了，排队中的账号不再发起拉取")
	require.Equal(t, 3, report.Total)
	require.Equal(t, 3, report.Failed)
	require.Equal(t, 1, fetcher.totalCalls(), "并发为 1 时只有第一个账号真正发起过拉取，其余排队时就被总时限取消")
}

func TestCodexTPRefreshPartialFailureReportsPerAccountReasons(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "ok"), cxtpRefreshCommandCode(12, "401"), cxtpRefreshCommandCode(13, "格式不对"), cxtpOAuthAccount(20)}
	fetcher := newCXTPRefreshFetcher()
	for _, id := range []int64{11, 12, 13} {
		fetcher.states[id] = modelCatalogCacheState{Needed: true}
	}
	fetcher.errs[20] = errors.New("dial tcp 10.0.0.1:443: connect: connection refused (token=sk-secret-value)")
	fetcher.refresh = func(_ context.Context, account *Account) (int, error) {
		switch account.ID {
		case 12:
			return 0, errors.New(`model list returned HTTP 401`)
		case 13:
			return 0, errors.New("model list has no supported_endpoints")
		}
		return 40, nil
	}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)

	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Equal(t, 4, report.Total)
	require.Equal(t, 3, report.Failed)
	items := cxtpRefreshItemsByKey(report)
	require.True(t, items["model_protocol:11"].OK, "单账号失败不影响其它账号")
	require.Equal(t, "上游返回 HTTP 401", items["model_protocol:12"].Reason)
	require.Equal(t, "上游目录不含协议信息", items["model_protocol:13"].Reason)
	require.Equal(t, "拉取失败，详见日志", items["official_base:20"].Reason)
	for _, item := range report.Items {
		require.NotContains(t, item.Reason, "sk-secret-value", "失败原因不回显凭据与地址")
		require.NotContains(t, item.Reason, "10.0.0.1")
	}
}

func TestCodexTPRefreshThrottlesAndMergesRepeatedRefresh(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "CommandCode"), cxtpRefreshCommandCode(12, "CommandCode-2")}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	fetcher.states[12] = modelCatalogCacheState{Needed: true}
	release := make(chan struct{})
	fetcher.refresh = func(ctx context.Context, _ *Account) (int, error) {
		<-release
		return 5, nil
	}
	svc, clock := cxtpRefreshService(t, accounts, fetcher)

	// 并发的多次刷新合并为一次执行：每个账号只拉一次。
	const callers = 5
	reports := make(chan CodexCatalogRefreshReport, callers)
	for range callers {
		go func() {
			report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
			if err == nil {
				reports <- report
			}
		}()
	}
	require.Eventually(t, func() bool { return fetcher.totalCalls() == 2 }, time.Second, time.Millisecond)
	time.Sleep(30 * time.Millisecond)
	close(release)
	for range callers {
		report := <-reports
		require.Equal(t, 2, report.Total)
	}
	require.Equal(t, 2, fetcher.totalCalls(), "并发刷新合并，每个账号只拉一次")

	// 合并窗口内再点：复用上次结果，不再发请求。
	*clock = clock.Add(5 * time.Second)
	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.True(t, report.Reused)
	require.Equal(t, 2, report.Total)
	require.Equal(t, 2, fetcher.totalCalls())

	// 窗口外：重新拉取。
	*clock = clock.Add(codexTPRefreshReuseWindow)
	report, err = svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.False(t, report.Reused)
	require.Equal(t, 4, fetcher.totalCalls())

	// 另一个分组不共用合并结果。
	other, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), &Group{ID: 99, Platform: PlatformComposite})
	require.NoError(t, err)
	require.False(t, other.Reused)
}

func TestCodexTPRefreshMissingModeOnlyFetchesAbsentCatalogs(t *testing.T) {
	accounts := []Account{
		cxtpRefreshCommandCode(11, "缺目录"),
		cxtpRefreshCommandCode(12, "已加载"),
		cxtpRefreshCommandCode(13, "退避中"),
		cxtpRefreshCommandCode(14, "缺目录-2"),
	}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	fetcher.states[12] = modelCatalogCacheState{Needed: true, Loaded: true}
	fetcher.states[13] = modelCatalogCacheState{Needed: true, RetryAt: time.Now().Add(time.Minute)}
	fetcher.states[14] = modelCatalogCacheState{Needed: true, RetryAt: time.Now().Add(-time.Minute)}
	fetcher.errs[20] = errors.New("不应被拉取")
	svc, _ := cxtpRefreshService(t, accounts, fetcher)

	sess, err := svc.newSession(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	svc.refreshMissingModelCatalogs(t.Context(), sess)
	require.Equal(t, 1, fetcher.callCount(11))
	require.Zero(t, fetcher.callCount(12), "已加载的目录不重拉")
	require.Zero(t, fetcher.callCount(13), "失败退避期内不重试，避免反复打上游")
	require.Equal(t, 1, fetcher.callCount(14), "退避期已过")
	require.Empty(t, fetcher.fetchedIDs(), "补拉模式不动官方账号基础目录（评估本身会拉）")
}

func TestCodexTPRefreshOfficialBaseFailureIsReportedPerAccount(t *testing.T) {
	official := cxtpOAuthAccount(20)
	official.Name = "官方-慢"
	fine := cxtpOAuthAccount(21)
	fine.Name = "官方-正常"
	fetcher := newCXTPRefreshFetcher()
	fetcher.bodies[21] = []byte(`{"models":[{"slug":"gpt-5.5"}]}`)
	fetcher.errs[20] = context.DeadlineExceeded
	svc, _ := cxtpRefreshService(t, []Account{official, fine}, fetcher)

	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	items := cxtpRefreshItemsByKey(report)
	require.False(t, items["official_base:20"].OK)
	require.Equal(t, "拉取超时", items["official_base:20"].Reason)
	require.Equal(t, "官方-慢", items["official_base:20"].AccountName)
	require.True(t, items["official_base:21"].OK)
	require.Equal(t, 1, report.Failed)
	_, remembered := svc.recallBase(8, 21)
	require.True(t, remembered)
	_, remembered = svc.recallBase(8, 20)
	require.False(t, remembered, "失败的账号不留存目录")
}

// ---------------------------------------------------------------------------
// 与真实拉取函数联调：重启后场景复现
// ---------------------------------------------------------------------------

const cxtpRefreshDMITModels = `{"object":"list","data":[
	{"id":"deepseek/deepseek-v4.1-flash","supported_endpoints":["/chat/completions","/responses"]},
	{"id":"claude-sonnet-4-6","supported_endpoints":["/messages"]}
]}`

// cxtpRefreshDMITAccounts 是 DMIT 的形态：DeepSeek 官方账号被关闭，CommandCode（按模型分流，映射到带厂商前缀的型号）、
// HUB（openai 平台、强制 Chat Completions）、Cline（只有 Chat Completions）都声明同一个公开模型。
func cxtpRefreshDMITAccounts(baseURL string) []Account {
	deepseek := cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 1), "DeepSeek")
	deepseek.Schedulable = false
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	hub := compositePoolAccount(13, PlatformOpenAI, 10, map[string]any{
		"base_url":      "https://hub.example.com/v1",
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
	hub.Name = "HUB_OpenAI"
	// 管理员把这个中转账号强制设为 Chat Completions：Responses 请求需转换，不在零转换组里。
	hub.Extra = map[string]any{openai_compat.ExtraKeyResponsesMode: string(openai_compat.ResponsesSupportModeForceChatCompletions)}
	return []Account{
		deepseek,
		commandCode,
		hub,
		cxtpPoolNamed(cxtpPoolChatOnly(14, PlatformCline, 5, compositePoolMapping(compositePoolModel)), "Cline"),
	}
}

func cxtpRefreshDMITService(t *testing.T, upstream HTTPUpstream) (*CodexThirdPartyCatalogService, string) {
	t.Helper()
	baseURL := fmt.Sprintf("http://cc-dmit-%d.example/provider/v1", time.Now().UnixNano())
	accounts := cxtpRefreshDMITAccounts(baseURL)
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(&accounts[1], buildOpenAIModelsURL(baseURL)))
	})
	svc := cxtpPoolService(t, accounts)
	svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}}
	return svc, baseURL
}

func TestCodexTPRefreshDMITRestartScenario(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{}}
	svc, baseURL := cxtpRefreshDMITService(t, upstream)
	path := "/provider/v1/models"
	upstream.responses[path] = commandCodeAlphaResponse{status: http.StatusOK, body: cxtpRefreshDMITModels}

	// ① 重启后：缓存是空的，读页面（离线评估）停在新文案，且不向上游发请求。
	cold := cxtpPoolEvaluate(t, svc)
	require.Equal(t, CodexModelStatusNeedsCapability, cold.Status)
	require.False(t, cold.CanEnable)
	require.Contains(t, cold.Reasons, codexTPCatalogNotReadyReason)
	require.NotContains(t, cold.Reasons, "部分承接账号按模型分流上游协议，无法静态确认 Responses 通路")
	require.Equal(t, []int64{11, 13, 14}, cold.AccountIDs, "零转换组为空，退到需转换组")
	upstream.mu.Lock()
	require.Empty(t, upstream.requests)
	upstream.mu.Unlock()

	// ② 点「刷新」：同步拉取 CommandCode 的上游模型目录（桩声明 deepseek 型号支持 Responses）。
	report, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Equal(t, 1, report.Total)
	require.Zero(t, report.Failed)
	require.Equal(t, CodexCatalogRefreshItem{
		AccountID: 11, AccountName: "CommandCode", Platform: PlatformCommandCode,
		Kind: CodexCatalogRefreshKindModelProtocol, OK: true, ModelCount: 2, ElapsedMS: report.Items[0].ElapsedMS,
	}, report.Items[0])
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1)
	require.Equal(t, baseURL+"/models", "http://"+upstream.requests[0].URL.Host+upstream.requests[0].URL.Path, "请求形态与运行时一致：账号 Chat Completions 基址下的 /models")
	require.Equal(t, "Bearer sk-test", upstream.requests[0].Header.Get("Authorization"))
	upstream.mu.Unlock()

	// ③ 刷新后再读页面：CommandCode 进零转换组，成为首选；备选不含 Cline 与强制 Chat 的 openai 账号。
	warm := cxtpPoolEvaluate(t, svc)
	require.Equal(t, CodexModelStatusConfigurable, warm.Status, "原因：%v", warm.Reasons)
	require.True(t, warm.CanEnable)
	require.Equal(t, CodexThirdPartyProtocolNative, warm.Protocol)
	require.Equal(t, []int64{11}, warm.AccountIDs)
	require.NotNil(t, warm.PrimaryAccount)
	require.Equal(t, CodexThirdPartyRouteAccount{ID: 11, Name: "CommandCode", Platform: PlatformCommandCode}, *warm.PrimaryAccount)
	require.Empty(t, warm.AlternateAccounts, "Cline 与强制 Chat Completions 的 HUB 都需转换，不在零转换组里")
	require.Equal(t, "deepseek-flash", warm.ProfileID)
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1, "读页面不再发请求")
	upstream.mu.Unlock()
}

func TestCodexTPEnableEvaluationPrefetchesMissingCatalog(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	svc, _ := cxtpRefreshDMITService(t, upstream)

	// 预览与保存使用的在线评估：缺目录缓存先补拉再判定，拉到 Responses 后可开启，无需先点刷新。
	eval, err := svc.EvaluateCodexThirdPartyCatalog(t.Context(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	candidate, ok := findCodexCandidate(eval.Candidates, compositePoolModel)
	require.True(t, ok)
	require.Equal(t, CodexModelStatusConfigurable, candidate.Status, "原因：%v", candidate.Reasons)
	require.True(t, candidate.CanEnable)
	require.Equal(t, []int64{11}, candidate.AccountIDs)
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1)
	upstream.mu.Unlock()

	// 目录已就绪：再次评估不重复请求。
	_, err = svc.EvaluateCodexThirdPartyCatalog(t.Context(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1)
	upstream.mu.Unlock()
}

func TestCodexTPEnableEvaluationKeepsUnconfirmedWhenPrefetchFails(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusBadGateway, body: `{"error":"bad gateway"}`},
	}}
	svc, _ := cxtpRefreshDMITService(t, upstream)

	eval, err := svc.EvaluateCodexThirdPartyCatalog(t.Context(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err, "补拉失败不让评估失败")
	candidate, ok := findCodexCandidate(eval.Candidates, compositePoolModel)
	require.True(t, ok)
	require.Equal(t, CodexModelStatusNeedsCapability, candidate.Status)
	require.False(t, candidate.CanEnable)
	require.Contains(t, candidate.Reasons, codexTPCatalogNotReadyReason)
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1)
	upstream.mu.Unlock()

	// 失败后的短退避期内，再次预览不重复打上游。
	_, err = svc.EvaluateCodexThirdPartyCatalog(t.Context(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	upstream.mu.Lock()
	require.Len(t, upstream.requests, 1, "失败退避期内不重试")
	upstream.mu.Unlock()
}

func TestCodexTPOfflineEvaluationNeverPrefetches(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	svc, _ := cxtpRefreshDMITService(t, upstream)
	candidate := cxtpPoolEvaluate(t, svc)
	require.Contains(t, candidate.Reasons, codexTPCatalogNotReadyReason)
	upstream.mu.Lock()
	defer upstream.mu.Unlock()
	require.Empty(t, upstream.requests, "页面读取只用缓存，不向上游发请求")
}

func TestCodexTPReasonKeepsOriginalWordingWhenCatalogLoadedButNoVerdict(t *testing.T) {
	// 目录已经成功加载，但里面没有该模型（或协议不明）：目录不是“没拉过”，保留原有说法。
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: `{"data":[{"id":"some/other-model","supported_endpoints":["/chat/completions"]}]}`},
	}}
	svc, _ := cxtpRefreshDMITService(t, upstream)
	_, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	candidate := cxtpPoolEvaluate(t, svc)
	require.Equal(t, CodexModelStatusNeedsCapability, candidate.Status)
	require.Contains(t, candidate.Reasons, "部分承接账号按模型分流上游协议，无法静态确认 Responses 通路")
	require.NotContains(t, candidate.Reasons, codexTPCatalogNotReadyReason)
}

// ---------------------------------------------------------------------------
// 管理服务刷新入口
// ---------------------------------------------------------------------------

type codexAdminRefreshEvaluator struct {
	*codexAdminFakeEvaluator
	// catalogStatus 模拟账号上游模型目录的缓存状态（与刷新写入的是同一份）。
	catalogStatus map[int64]modelCatalogCacheState
	report        CodexCatalogRefreshReport
	err           error
	groups        []int64
	// order 记录刷新与评估的先后：刷新必须发生在重新读取状态之前。
	order []string
}

func (e *codexAdminRefreshEvaluator) ModelCatalogCacheStatus(account *Account) (needed, loaded bool, models int, fetchedAt time.Time) {
	state := e.catalogStatus[account.ID]
	return state.Needed, state.Loaded, state.ModelCount, state.FetchedAt
}

func (e *codexAdminRefreshEvaluator) RefreshCodexThirdPartyCatalogs(_ context.Context, group *Group) (CodexCatalogRefreshReport, error) {
	e.groups = append(e.groups, group.ID)
	e.order = append(e.order, "refresh")
	return e.report, e.err
}

func (e *codexAdminRefreshEvaluator) EvaluateCodexThirdPartyCatalog(ctx context.Context, group *Group, cfg CodexModelVisibilityConfig) (CodexThirdPartyCatalogEvaluation, error) {
	e.order = append(e.order, "evaluate")
	return e.codexAdminFakeEvaluator.EvaluateCodexThirdPartyCatalog(ctx, group, cfg)
}

func TestCodexAdminRefreshCatalogsReturnsReportAndFreshState(t *testing.T) {
	h := newCodexAdminHarness(t)
	evaluator := &codexAdminRefreshEvaluator{
		codexAdminFakeEvaluator: h.evaluator,
		report: CodexCatalogRefreshReport{
			GroupID: codexAdminTestGroupID, Total: 1, Failed: 1,
			Items: []CodexCatalogRefreshItem{{AccountID: 5, Kind: CodexCatalogRefreshKindModelProtocol, Reason: "拉取超时"}},
		},
	}
	h.svc.evaluator = evaluator

	result, err := h.svc.RefreshCatalogs(t.Context(), codexAdminTestGroupID)
	require.NoError(t, err)
	require.Equal(t, evaluator.report, result.Refresh)
	require.NotNil(t, result.State)
	require.Equal(t, codexAdminTestGroupID, result.State.GroupID)
	require.NotEmpty(t, result.State.Evaluation.Candidates, "返回刷新后重新读取的分组状态，结构与读取接口相同")
	require.Equal(t, []string{"refresh", "evaluate"}, evaluator.order, "先刷新，再按最新缓存读取状态")
	require.Equal(t, []bool{true}, h.evaluator.offline, "刷新之后的状态读取仍是离线评估")

	// 分组不存在、平台不支持：与读取接口一致。
	_, err = h.svc.RefreshCatalogs(t.Context(), 999)
	require.ErrorIs(t, err, ErrGroupNotFound)
}

func TestCodexAdminRefreshCatalogsFailures(t *testing.T) {
	h := newCodexAdminHarness(t)

	// 评估器不支持刷新：整体失败。
	_, err := h.svc.RefreshCatalogs(t.Context(), codexAdminTestGroupID)
	require.ErrorIs(t, err, ErrCodexCatalogRefreshFailed)

	// 刷新整体失败（例如数据库错误）：映射为稳定错误码，不泄漏内部细节。
	h.svc.evaluator = &codexAdminRefreshEvaluator{codexAdminFakeEvaluator: h.evaluator, err: errors.New("pq: password authentication failed")}
	_, err = h.svc.RefreshCatalogs(t.Context(), codexAdminTestGroupID)
	require.ErrorIs(t, err, ErrCodexCatalogRefreshFailed)
	var appErr *infraerrors.ApplicationError
	require.True(t, errors.As(err, &appErr))
	require.NotContains(t, appErr.Message, "password", "对外消息不含内部细节")
}

// ---------------------------------------------------------------------------
// 启动预热
// ---------------------------------------------------------------------------

type warmupRepo struct {
	AccountRepository
	accounts  []Account
	err       error
	platforms []string
}

func (r *warmupRepo) ListModelAvailabilityCandidates(_ context.Context, groupID *int64, platforms []string, includeGrouped bool) ([]Account, error) {
	r.platforms = platforms
	if r.err != nil {
		return nil, r.err
	}
	if groupID != nil || !includeGrouped {
		return nil, errors.New("预热应列出全部分组的账号")
	}
	return r.accounts, nil
}

func warmupAccount(id int64, baseURL string) Account {
	account := cxtpPoolCommandCode(id, int(id), baseURL)
	return account
}

func TestModelProtocolCatalogWarmupRefreshesEnabledAccountsInBackground(t *testing.T) {
	base := fmt.Sprintf("http://cc-warm-%d.example/provider/v1", time.Now().UnixNano())
	loaded := warmupAccount(31, base)
	pending := warmupAccount(32, base)
	disabled := warmupAccount(33, base)
	disabled.Schedulable = false
	plain := compositePoolAdaptive(34, PlatformDeepseek, 1)
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		for _, account := range []*Account{&loaded, &pending, &disabled} {
			delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(account, buildOpenAIModelsURL(base)))
		}
	})
	upstreamModelProtocols.store(modelProtocolCatalogKey(&loaded, buildOpenAIModelsURL(base)), map[string][]string{"x": {APIProtocolResponses}}, nil, time.Now())

	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	repo := &warmupRepo{accounts: []Account{loaded, pending, disabled, plain}}
	svc := NewModelProtocolCatalogWarmupService(repo, &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream})
	svc.delay = 600 * time.Millisecond

	started := time.Now()
	svc.Start()
	require.Less(t, time.Since(started), 300*time.Millisecond, "Start 立即返回，不阻塞启动") // time-threshold: allow 预热延迟是 600ms，Start 若退化成同步执行至少耗时 600ms，上界 300ms 区分“立即返回”与“同步等待”（延迟再缩短会把判别余量压到噪声量级，所以不再缩）
	svc.Start()                                                                    // 重复启动无副作用

	require.Eventually(t, func() bool {
		return upstreamModelProtocols.peek(modelProtocolCatalogKey(&pending, buildOpenAIModelsURL(base)), "deepseek/deepseek-v4.1-flash") != nil
	}, 3*time.Second, 5*time.Millisecond)
	svc.Stop()
	upstream.mu.Lock()
	defer upstream.mu.Unlock()
	require.Len(t, upstream.requests, 1, "只拉持久启用且目录缺失的按模型分流账号：已加载、停用、不按模型分流的都跳过")
	require.Equal(t, []string{PlatformCommandCode}, repo.platforms, "只列提供上游模型目录的平台")
}

func TestModelProtocolCatalogWarmupFailuresOnlyLog(t *testing.T) {
	base := fmt.Sprintf("http://cc-warm-fail-%d.example/provider/v1", time.Now().UnixNano())
	failing := warmupAccount(41, base)
	okBase := fmt.Sprintf("http://cc-warm-ok-%d.example/other/v1", time.Now().UnixNano())
	ok := warmupAccount(42, okBase)
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(&failing, buildOpenAIModelsURL(base)))
		delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(&ok, buildOpenAIModelsURL(okBase)))
	})
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusInternalServerError, body: `{}`},
		"/other/v1/models":    {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	svc := NewModelProtocolCatalogWarmupService(&warmupRepo{accounts: []Account{failing, ok}}, &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream})

	refreshed, failed := svc.runOnce(t.Context())
	require.Equal(t, 1, refreshed)
	require.Equal(t, 1, failed, "单账号失败只计数与记日志，不影响其它账号")

	// 账号列表读取失败：同样只记日志。
	svc = NewModelProtocolCatalogWarmupService(&warmupRepo{err: errors.New("db down")}, &OpenAIGatewayService{httpUpstream: upstream})
	refreshed, failed = svc.runOnce(t.Context())
	require.Zero(t, refreshed+failed)
}

func TestModelProtocolCatalogWarmupStopCancelsPendingStart(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{}}
	repo := &warmupRepo{accounts: []Account{warmupAccount(51, fmt.Sprintf("http://cc-warm-stop-%d.example/provider/v1", time.Now().UnixNano()))}}
	svc := NewModelProtocolCatalogWarmupService(repo, &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream})
	svc.delay = time.Hour
	svc.Start()
	done := make(chan struct{})
	go func() { svc.Stop(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Stop 应当取消还在等待延迟的预热")
	}
	upstream.mu.Lock()
	defer upstream.mu.Unlock()
	require.Empty(t, upstream.requests)
	svc.Stop() // 重复 Stop 无副作用
}

// ---------------------------------------------------------------------------
// 子协程 panic：计为失败并记日志，不让进程退出
// ---------------------------------------------------------------------------

type panickingHTTPUpstream struct{ HTTPUpstream }

func (panickingHTTPUpstream) Do(*http.Request, string, int64, int) (*http.Response, error) {
	panic("上游客户端异常")
}

func TestModelProtocolCatalogWarmupPanicCountsAsFailure(t *testing.T) {
	base := fmt.Sprintf("http://cc-warm-panic-%d.example/provider/v1", time.Now().UnixNano())
	account := warmupAccount(61, base)
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(&account, buildOpenAIModelsURL(base)))
	})
	svc := NewModelProtocolCatalogWarmupService(&warmupRepo{accounts: []Account{account}},
		&OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: panickingHTTPUpstream{}})

	require.NotPanics(t, func() {
		refreshed, failed := svc.runOnce(t.Context())
		require.Zero(t, refreshed)
		require.Equal(t, 1, failed, "panic 计为该账号失败")
	})
	// 刷新标记已释放，目录不会永远处于“刷新中”。
	_, key, _ := accountModelCatalogEndpoint(&account)
	upstreamModelProtocols.mu.Lock()
	defer upstreamModelProtocols.mu.Unlock()
	require.False(t, upstreamModelProtocols.entries[key].refreshing)
}

func TestCodexTPRefreshBatchPanicCountsAsFailure(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "CommandCode"), cxtpRefreshCommandCode(12, "CommandCode-2"), cxtpOAuthAccount(20)}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	fetcher.states[12] = modelCatalogCacheState{Needed: true}
	fetcher.bodies[20] = []byte(`{"models":[{"slug":"gpt-5.5"}]}`)

	t.Run("协议目录批次 panic：这批账号全部计为失败", func(t *testing.T) {
		svc, _ := cxtpRefreshService(t, accounts, fetcher)
		svc.refreshTuning.onBatch = func(kind string) {
			if kind == CodexCatalogRefreshKindModelProtocol {
				panic("批次异常")
			}
		}
		var report CodexCatalogRefreshReport
		require.NotPanics(t, func() {
			var err error
			report, err = svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
			require.NoError(t, err)
		})
		items := cxtpRefreshItemsByKey(report)
		for _, id := range []int64{11, 12} {
			item := items[fmt.Sprintf("model_protocol:%d", id)]
			require.False(t, item.OK)
			require.Equal(t, codexTPRefreshPanicReason, item.Reason)
		}
		require.True(t, items["official_base:20"].OK, "另一批不受影响")
		require.Equal(t, 2, report.Failed)
	})

	t.Run("官方基础目录批次 panic：记一条该批失败，协议目录不受影响", func(t *testing.T) {
		svc, _ := cxtpRefreshService(t, accounts, fetcher)
		svc.refreshTuning.onBatch = func(kind string) {
			if kind == CodexCatalogRefreshKindOfficialBase {
				panic("批次异常")
			}
		}
		var report CodexCatalogRefreshReport
		require.NotPanics(t, func() {
			var err error
			report, err = svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
			require.NoError(t, err)
		})
		items := cxtpRefreshItemsByKey(report)
		require.True(t, items["model_protocol:11"].OK)
		require.True(t, items["model_protocol:12"].OK)
		batch := items["official_base:0"]
		require.False(t, batch.OK)
		require.Equal(t, codexTPRefreshPanicReason, batch.Reason)
		require.Equal(t, 1, report.Failed)
	})
}

// ---------------------------------------------------------------------------
// 账号源状态与刷新写入的是同一份目录缓存
// ---------------------------------------------------------------------------

func TestCodexAdminSourcesFollowRefreshedModelCatalog(t *testing.T) {
	h := newCodexAdminHarness(t)
	commandCode := cxtpRefreshCommandCode(11, "CommandCode")
	plain := codexAdminAccount(12, "普通账号", "sk-plain")
	h.accounts.byGroup[codexAdminTestGroupID] = []Account{commandCode, plain}
	evaluator := &codexAdminRefreshEvaluator{
		codexAdminFakeEvaluator: h.evaluator,
		catalogStatus:           map[int64]modelCatalogCacheState{11: {Needed: true}},
	}
	h.svc.evaluator = evaluator
	statusOf := func(state *CodexThirdPartyGroupState, id int64) CodexThirdPartySourceStatus {
		for _, source := range state.Sources {
			if source.AccountID == id {
				return source
			}
		}
		t.Fatalf("sources 里没有账号 %d", id)
		return CodexThirdPartySourceStatus{}
	}

	// 目录还没加载：沿用快照给出的状态（从未同步 = missing）。
	state, err := h.svc.GetGroupState(t.Context(), codexAdminTestGroupID)
	require.NoError(t, err)
	require.Equal(t, codexThirdPartySourceStatusMissing, statusOf(state, 11).Status)
	require.Zero(t, statusOf(state, 11).ModelCount)

	// 刷新写入目录缓存之后：读取到的源状态与它一致，不再是 missing / 0。
	fetched := h.now().Add(-time.Minute)
	evaluator.catalogStatus[11] = modelCatalogCacheState{Needed: true, Loaded: true, ModelCount: 93, FetchedAt: fetched}
	state, err = h.svc.GetGroupState(t.Context(), codexAdminTestGroupID)
	require.NoError(t, err)
	source := statusOf(state, 11)
	require.Equal(t, string(CodexCatalogSourceComplete), source.Status)
	require.Equal(t, 93, source.ModelCount)
	require.False(t, source.Stale)
	require.True(t, source.FingerprintMatches)
	require.NotNil(t, source.SyncedAt)
	require.True(t, source.SyncedAt.Equal(fetched))
	require.Empty(t, source.LastError)

	// 不使用上游模型目录的账号不受影响。
	require.Equal(t, codexThirdPartySourceStatusMissing, statusOf(state, 12).Status)

	// 超过缓存有效期后标记过期。
	h.advance(modelProtocolCatalogTTL + time.Minute)
	state, err = h.svc.GetGroupState(t.Context(), codexAdminTestGroupID)
	require.NoError(t, err)
	require.True(t, statusOf(state, 11).Stale)
}

func TestCodexTPModelCatalogCacheStatusReadsTheRefreshedCache(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	svc, _ := cxtpRefreshDMITService(t, upstream)
	accounts, err := svc.accountRepo.ListModelAvailabilityCandidates(t.Context(), nil, []string{PlatformCommandCode}, true)
	require.NoError(t, err)
	require.Len(t, accounts, 1)

	needed, loaded, _, _ := svc.ModelCatalogCacheStatus(&accounts[0])
	require.True(t, needed)
	require.False(t, loaded)

	_, err = svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	needed, loaded, models, fetchedAt := svc.ModelCatalogCacheStatus(&accounts[0])
	require.True(t, needed)
	require.True(t, loaded)
	require.Equal(t, 2, models)
	require.WithinDuration(t, time.Now(), fetchedAt, time.Minute)

	// 不需要上游模型目录的账号：needed 为 false。
	deepseek := compositePoolAdaptive(10, PlatformDeepseek, 1)
	needed, _, _, _ = svc.ModelCatalogCacheStatus(&deepseek)
	require.False(t, needed)
}

// ---------------------------------------------------------------------------
// 日志里的触发来源
// ---------------------------------------------------------------------------

func TestCodexTPRefreshTriggersAreDistinguishable(t *testing.T) {
	accounts := []Account{cxtpRefreshCommandCode(11, "CommandCode")}
	fetcher := newCXTPRefreshFetcher()
	fetcher.states[11] = modelCatalogCacheState{Needed: true}
	svc, _ := cxtpRefreshService(t, accounts, fetcher)

	_, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	sess, err := svc.newSession(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	svc.refreshMissingModelCatalogs(t.Context(), sess)
	require.Equal(t, []string{modelCatalogTriggerPageRefresh, modelCatalogTriggerEnablePrefetch}, fetcher.triggers)
	require.NotEqual(t, modelCatalogTriggerStartupWarmup, modelCatalogTriggerPageRefresh)
	require.NotEqual(t, modelCatalogTriggerPageRefresh, modelCatalogTriggerEnablePrefetch)
}

// logCapture 临时替换默认 slog 输出，收集日志文本。
func logCapture(t *testing.T) *bytes.Buffer {
	t.Helper()
	buf := &bytes.Buffer{}
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return buf
}

func TestModelCatalogRefreshLogCarriesTheRealTrigger(t *testing.T) {
	upstream := &commandCodeAlphaUpstream{responses: map[string]commandCodeAlphaResponse{
		"/provider/v1/models": {status: http.StatusOK, body: cxtpRefreshDMITModels},
	}}
	buf := logCapture(t)

	// 启动预热。
	base := fmt.Sprintf("http://cc-trigger-%d.example/provider/v1", time.Now().UnixNano())
	account := warmupAccount(71, base)
	t.Cleanup(func() {
		upstreamModelProtocols.mu.Lock()
		defer upstreamModelProtocols.mu.Unlock()
		delete(upstreamModelProtocols.entries, modelProtocolCatalogKey(&account, buildOpenAIModelsURL(base)))
	})
	warmup := NewModelProtocolCatalogWarmupService(&warmupRepo{accounts: []Account{account}}, &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream})
	refreshed, _ := warmup.runOnce(t.Context())
	require.Equal(t, 1, refreshed)
	require.Contains(t, buf.String(), "trigger=startup_warmup")
	require.NotContains(t, buf.String(), "trigger=manual")

	// 页面刷新与开启补拉。
	buf.Reset()
	svc, _ := cxtpRefreshDMITService(t, upstream)
	_, err := svc.RefreshCodexThirdPartyCatalogs(t.Context(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Contains(t, buf.String(), "trigger=page_refresh")

	buf.Reset()
	svc2, _ := cxtpRefreshDMITService(t, upstream)
	_, err = svc2.EvaluateCodexThirdPartyCatalog(t.Context(), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.Contains(t, buf.String(), "trigger=enable_prefetch")
}
