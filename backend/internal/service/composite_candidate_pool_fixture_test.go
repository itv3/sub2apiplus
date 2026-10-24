//go:build unit

package service

// 账号级候选池单测的公共夹具：账号构造、桩仓储、桩缓存与请求上下文装配。
// 全部使用内存桩，不连接真实 Redis 或数据库。

import (
	"context"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	gocache "github.com/patrickmn/go-cache"
	"github.com/stretchr/testify/require"
)

const (
	compositePoolGroupID = int64(5001)
	compositePoolUserID  = int64(7001)
	compositePoolModel   = "deepseek-flash"
)

// compositePoolLister 同时扮演候选池的账号目录（配置态加载）和两套网关服务的账号仓储
// （按 ID 实时读取，对应调度器终检读的来源），分别记录调用次数。
// 测试可以随时改 accounts / byIDErr / byIDMissing，模拟账号状态在两次请求之间变化。
type compositePoolLister struct {
	AccountRepository
	mu          sync.Mutex
	accounts    []Account
	err         error
	calls       int
	byIDCalls   int
	byIDErr     map[int64]error
	byIDMissing map[int64]bool
	listDelay   time.Duration
}

func (l *compositePoolLister) ListModelAvailabilityCandidates(_ context.Context, _ *int64, _ []string, _ bool) ([]Account, error) {
	l.mu.Lock()
	l.calls++
	delay, err := l.listDelay, l.err
	out := make([]Account, len(l.accounts))
	copy(out, l.accounts)
	l.mu.Unlock()
	if delay > 0 {
		time.Sleep(delay)
	}
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (l *compositePoolLister) GetByID(_ context.Context, id int64) (*Account, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.byIDCalls++
	if err := l.byIDErr[id]; err != nil {
		return nil, err
	}
	if l.byIDMissing[id] {
		return nil, nil
	}
	for i := range l.accounts {
		if l.accounts[i].ID == id {
			account := l.accounts[i]
			return &account, nil
		}
	}
	return nil, ErrAccountNotFound
}

func (l *compositePoolLister) loadCalls() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.calls
}

func (l *compositePoolLister) readCalls() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.byIDCalls
}

// update 原地修改某个账号（模拟管理员改配置或账号进入冷却）。
func (l *compositePoolLister) update(id int64, mutate func(*Account)) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for i := range l.accounts {
		if l.accounts[i].ID == id {
			mutate(&l.accounts[i])
			return
		}
	}
}

// add 追加一个账号（模拟管理员新增声明账号）。
func (l *compositePoolLister) add(account Account) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.accounts = append(l.accounts, account)
}

// compositePoolQuotaRepo 按平台返回用户配额记录，并记录各平台被查询的次数。
type compositePoolQuotaRepo struct {
	UserPlatformQuotaRepository
	mu      sync.Mutex
	records map[string]*UserPlatformQuotaRecord
	reads   map[string]int
}

func (r *compositePoolQuotaRepo) GetByUserPlatform(_ context.Context, _ int64, platform string) (*UserPlatformQuotaRecord, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.reads == nil {
		r.reads = map[string]int{}
	}
	r.reads[platform]++
	return r.records[platform], nil
}

// compositePoolExhaustedQuota 返回"日额度为 0"的配额记录：余额模式下该平台立即被预检拒绝。
func compositePoolExhaustedQuota(platform string) *UserPlatformQuotaRecord {
	zero := 0.0
	return &UserPlatformQuotaRecord{
		UserID: compositePoolUserID, Platform: platform,
		DailyLimitUSD: &zero, DailyWindowStart: currentDayStart(),
	}
}

// compositePoolFixture 装配候选池及其依赖。
type compositePoolFixture struct {
	t       *testing.T
	lister  *compositePoolLister
	cache   *stubGatewayCache
	openAI  *OpenAIGatewayService
	gateway *GatewayService
	billing *BillingCacheService
	quota   *compositePoolQuotaRepo
	rpm     *userRPMCacheStub
	store   OpenAIWSStateStore
	pool    *CompositeCandidatePool
}

func newCompositePoolFixture(t *testing.T, accounts ...Account) *compositePoolFixture {
	t.Helper()
	cache := &stubGatewayCache{}
	store := NewOpenAIWSStateStore(cache)
	quota := &compositePoolQuotaRepo{records: map[string]*UserPlatformQuotaRecord{}}
	rpm := &userRPMCacheStub{}
	cfg := &config.Config{}
	billing := &BillingCacheService{cfg: cfg, userPlatformQuotaRepo: quota, userRPMCache: rpm}
	lister := &compositePoolLister{accounts: accounts}
	openAI := &OpenAIGatewayService{accountRepo: lister, cache: cache, cfg: cfg, openaiWSStateStore: store}
	gateway := &GatewayService{accountRepo: lister, cache: cache, cfg: cfg}
	return &compositePoolFixture{
		t: t, lister: lister, cache: cache, openAI: openAI, gateway: gateway,
		billing: billing, quota: quota, rpm: rpm, store: store,
		pool: NewCompositeCandidatePool(lister, openAI, gateway, billing),
	}
}

// enableConfigCache 给网关服务装上与线上相同类型的模型列表缓存（候选池的配置态缓存复用它）。
func (f *compositePoolFixture) enableConfigCache(ttl time.Duration) {
	f.gateway.modelsListCache = gocache.New(ttl, time.Minute)
	f.gateway.modelsListCacheTTL = ttl
}

// ctxFor 构造带入口标记的请求上下文。
func (f *compositePoolFixture) ctxFor(endpoint, body string, header http.Header, mods ...func(*CompositeCandidatePoolEntry)) context.Context {
	f.t.Helper()
	entry := CompositeCandidatePoolEntry{
		Endpoint:  endpoint,
		Body:      []byte(body),
		Header:    header,
		ClientIP:  "203.0.113.9",
		UserAgent: "composite-pool-test",
		APIKeyID:  9001,
		UserID:    compositePoolUserID,
		Group:     &Group{ID: compositePoolGroupID, Platform: PlatformComposite},
	}
	for _, mod := range mods {
		mod(&entry)
	}
	return WithCompositeCandidatePoolEntry(context.Background(), entry)
}

// resolve 以默认模型解析一次，并要求候选池已处理。
func (f *compositePoolFixture) resolve(ctx context.Context, endpoint string) compositeCandidatePoolOutcome {
	f.t.Helper()
	outcome, err := f.pool.resolve(ctx, compositePoolGroupID, compositePoolModel, endpoint)
	require.NoError(f.t, err)
	return outcome
}

func (f *compositePoolFixture) resolveHandled(ctx context.Context, endpoint string) compositeCandidatePoolOutcome {
	f.t.Helper()
	outcome := f.resolve(ctx, endpoint)
	require.NotEqual(f.t, compositeCandidatePathNone, outcome.Path, "候选池应已处理该请求")
	require.True(f.t, outcome.Decision.Matched)
	require.Equal(f.t, CompositeRouteSourceAccount, outcome.Decision.Source)
	require.Equal(f.t, compositePoolModel, outcome.Decision.PublicModel)
	require.Equal(f.t, compositePoolModel, outcome.Decision.UpstreamModel, "上游模型名保持公开模型名")
	return outcome
}

// 请求体构造。
func compositePoolResponsesBody(extra string) string {
	return `{"model":"` + compositePoolModel + `","input":"hello"` + extra + `}`
}

func compositePoolChatBody() string {
	return `{"model":"` + compositePoolModel + `","messages":[{"role":"user","content":"hello"}]}`
}

// compositePoolMessagesBody 构造 Anthropic Messages 请求体；sessionID 非空时通过
// metadata.user_id 携带会话号（Anthropic 网关族会话哈希的第一优先来源）。
func compositePoolMessagesBody(sessionID string) string {
	meta := ""
	if sessionID != "" {
		meta = `,"metadata":{"user_id":"{\"device_id\":\"dev1\",\"account_uuid\":\"\",\"session_id\":\"` + sessionID + `\"}"}`
	}
	return `{"model":"` + compositePoolModel + `","max_tokens":16,"messages":[{"role":"user","content":"hello"}]` + meta + `}`
}

// compositePoolMetadataSnippet 返回可拼进任意请求体的 metadata.user_id 片段（以逗号开头）。
func compositePoolMetadataSnippet(sessionID string) string {
	return `,"metadata":{"user_id":"{\"device_id\":\"dev1\",\"account_uuid\":\"\",\"session_id\":\"` + sessionID + `\"}"}`
}

func compositePoolSessionHeader(sessionID string) http.Header {
	return http.Header{"Session_id": []string{sessionID}}
}

// 账号构造。priority 数值越小越优先。

func compositePoolMapping(models ...string) map[string]any {
	mapping := map[string]any{}
	for _, model := range models {
		mapping[model] = model
	}
	return mapping
}

func compositePoolAccount(id int64, platform string, priority int, credentials map[string]any) Account {
	creds := map[string]any{"api_key": "sk-test"}
	for key, value := range credentials {
		creds[key] = value
	}
	return Account{
		ID: id, Name: platform, Platform: platform, Type: AccountTypeAPIKey,
		Status: StatusActive, Schedulable: true, Priority: priority, Concurrency: 1,
		Credentials: creds,
		GroupIDs:    []int64{compositePoolGroupID},
		AccountGroups: []AccountGroup{
			{AccountID: id, GroupID: compositePoolGroupID},
		},
	}
}

// compositePoolAdaptive 构造按入站协议分流（adaptive）的多协议账号，声明 compositePoolModel。
// Kimi、DeepSeek 与 MiniMax 三种协议齐全，Zhipu 没有原生 Responses 端点。
func compositePoolAdaptive(id int64, platform string, priority int) Account {
	return compositePoolAccount(id, platform, priority, map[string]any{
		"api_protocol":  APIProtocolAdaptive,
		"model_mapping": compositePoolMapping(compositePoolModel),
	})
}

func compositePoolOpenAI(id int64, priority int) Account {
	return compositePoolAccount(id, PlatformOpenAI, priority, map[string]any{
		"model_mapping": map[string]any{compositePoolModel: "gpt-5.1"},
	})
}

func compositePoolAnthropic(id int64, priority int) Account {
	return compositePoolAccount(id, PlatformAnthropic, priority, map[string]any{
		"model_mapping": map[string]any{compositePoolModel: "claude-sonnet-4"},
	})
}

func compositePoolGemini(id int64, priority int) Account {
	return compositePoolAccount(id, PlatformGemini, priority, map[string]any{
		"model_mapping": map[string]any{compositePoolModel: "gemini-2.5-pro"},
	})
}

// compositePoolWithMapping 返回替换了 model_mapping 的账号副本（凭据 map 深拷贝一层，不影响原账号）。
func compositePoolWithMapping(account Account, mapping map[string]any) Account {
	creds := make(map[string]any, len(account.Credentials)+1)
	for key, value := range account.Credentials {
		creds[key] = value
	}
	creds["model_mapping"] = mapping
	account.Credentials = creds
	return account
}
