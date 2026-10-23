package service

// 管理服务的单元测试：所有外部依赖都用内存假实现，不访问数据库和网络。
// 配置与源快照经由真实的 SettingService 读写一份内存 settings，这样序列化和归一化逻辑也被覆盖。

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// 假实现
// ---------------------------------------------------------------------------

// codexAdminSettingRepo 是内存版 SettingRepository。
type codexAdminSettingRepo struct {
	mu     sync.Mutex
	values map[string]string
}

func newCodexAdminSettingRepo() *codexAdminSettingRepo {
	return &codexAdminSettingRepo{values: map[string]string{}}
}

func (r *codexAdminSettingRepo) Get(_ context.Context, key string) (*Setting, error) {
	value, err := r.GetValue(context.Background(), key)
	if err != nil {
		return nil, err
	}
	return &Setting{Key: key, Value: value}, nil
}

func (r *codexAdminSettingRepo) GetValue(_ context.Context, key string) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	value, ok := r.values[key]
	if !ok {
		return "", ErrSettingNotFound
	}
	return value, nil
}

func (r *codexAdminSettingRepo) Set(_ context.Context, key, value string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.values[key] = value
	return nil
}

func (r *codexAdminSettingRepo) GetMultiple(context.Context, []string) (map[string]string, error) {
	return nil, errors.New("not used")
}
func (r *codexAdminSettingRepo) SetMultiple(context.Context, map[string]string) error {
	return errors.New("not used")
}
func (r *codexAdminSettingRepo) GetAll(context.Context) (map[string]string, error) {
	return nil, errors.New("not used")
}

func (r *codexAdminSettingRepo) Delete(_ context.Context, key string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.values, key)
	return nil
}

func (r *codexAdminSettingRepo) has(key string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, ok := r.values[key]
	return ok
}

func (r *codexAdminSettingRepo) raw(key string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.values[key]
}

// codexAdminFakeStore 是内存版 CodexModelVisibilityStore，语义与真实实现一致：比较 revision、递增、冲突。
type codexAdminFakeStore struct {
	repo *codexAdminSettingRepo

	mu        sync.Mutex
	casCalls  int
	deleted   []int64
	casErr    error
	beforeCAS func() // 模拟其它节点在读取之后、提交之前改动了配置
}

func (s *codexAdminFakeStore) CompareAndSwapCodexModelVisibility(_ context.Context, groupID int64, expected int64, next CodexModelVisibilityConfig) (CodexModelVisibilityConfig, error) {
	s.mu.Lock()
	s.casCalls++
	hook, casErr := s.beforeCAS, s.casErr
	s.mu.Unlock()
	if hook != nil {
		hook()
	}
	if casErr != nil {
		return CodexModelVisibilityConfig{}, casErr
	}
	key := CodexModelVisibilitySettingKey(groupID)
	raw, err := s.repo.GetValue(context.Background(), key)
	switch {
	case errors.Is(err, ErrSettingNotFound):
		if expected != 0 {
			return CodexModelVisibilityConfig{}, ErrCodexModelVisibilityConflict
		}
	case err != nil:
		return CodexModelVisibilityConfig{}, err
	default:
		current, parseErr := ParseCodexModelVisibilityConfig(raw)
		if parseErr != nil {
			return CodexModelVisibilityConfig{}, parseErr
		}
		if expected == 0 || current.Revision != expected {
			return CodexModelVisibilityConfig{}, ErrCodexModelVisibilityConflict
		}
	}
	written := next.Clone()
	written.SchemaVersion = CodexModelVisibilitySchemaVersion
	written.Revision = expected + 1
	value, err := written.Marshal()
	if err != nil {
		return CodexModelVisibilityConfig{}, err
	}
	_ = s.repo.Set(context.Background(), key, value)
	return written, nil
}

func (s *codexAdminFakeStore) DeleteCodexModelVisibility(_ context.Context, groupID int64) error {
	s.mu.Lock()
	s.deleted = append(s.deleted, groupID)
	s.mu.Unlock()
	return s.repo.Delete(context.Background(), CodexModelVisibilitySettingKey(groupID))
}

func (s *codexAdminFakeStore) casCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.casCalls
}

// codexAdminFakeGroups 是内存版分组读取。
type codexAdminFakeGroups struct {
	groups map[int64]*Group
}

func (g *codexAdminFakeGroups) GetByIDLite(_ context.Context, id int64) (*Group, error) {
	group, ok := g.groups[id]
	if !ok {
		return nil, ErrGroupNotFound
	}
	copied := *group
	return &copied, nil
}

// codexAdminFakeAccounts 是内存版账号列表。
type codexAdminFakeAccounts struct {
	byGroup map[int64][]Account
	err     error
}

func (a *codexAdminFakeAccounts) ListModelAvailabilityCandidates(_ context.Context, groupID *int64, platforms []string, includeGrouped bool) ([]Account, error) {
	if a.err != nil {
		return nil, a.err
	}
	if groupID == nil || len(platforms) == 0 || includeGrouped {
		return nil, errors.New("unexpected candidate query")
	}
	out := append([]Account(nil), a.byGroup[*groupID]...)
	return out, nil
}

// codexAdminFakeSyncer 是可编程的上游同步假实现。
type codexAdminFakeSyncer struct {
	mu          sync.Mutex
	calls       map[int64]int
	total       int
	inflight    int
	maxInflight int
	results     map[int64]codexAdminSyncResult
	block       chan struct{} // 非空时同步会阻塞到该通道关闭或上下文结束
	ctxErrAtEnd map[int64]error
	deadlines   map[int64]time.Duration
}

type codexAdminSyncResult struct {
	models []string
	err    error
}

func newCodexAdminFakeSyncer() *codexAdminFakeSyncer {
	return &codexAdminFakeSyncer{
		calls:       map[int64]int{},
		results:     map[int64]codexAdminSyncResult{},
		ctxErrAtEnd: map[int64]error{},
		deadlines:   map[int64]time.Duration{},
	}
}

func (s *codexAdminFakeSyncer) SyncUpstreamModelCatalog(ctx context.Context, account *Account) (*UpstreamModelCatalog, error) {
	s.mu.Lock()
	s.calls[account.ID]++
	s.total++
	s.inflight++
	if s.inflight > s.maxInflight {
		s.maxInflight = s.inflight
	}
	if deadline, ok := ctx.Deadline(); ok {
		s.deadlines[account.ID] = time.Until(deadline)
	}
	block := s.block
	result, ok := s.results[account.ID]
	s.mu.Unlock()

	if block != nil {
		select {
		case <-block:
		case <-ctx.Done():
		}
	}

	s.mu.Lock()
	s.inflight--
	s.ctxErrAtEnd[account.ID] = ctx.Err()
	s.mu.Unlock()

	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !ok {
		return &UpstreamModelCatalog{Models: []string{"default-model"}}, nil
	}
	if result.err != nil {
		return nil, result.err
	}
	return &UpstreamModelCatalog{Models: result.models}, nil
}

func (s *codexAdminFakeSyncer) callCount(accountID int64) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls[accountID]
}

func (s *codexAdminFakeSyncer) totalCalls() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.total
}

func (s *codexAdminFakeSyncer) currentInflight() (current, max int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.inflight, s.maxInflight
}

// codexAdminFakeEvaluator 是可编程的目录评估器假实现。
type codexAdminFakeEvaluator struct {
	mu               sync.Mutex
	calls            int
	seen             []CodexModelVisibilityConfig
	err              error
	inputFingerprint string
	candidates       []CodexThirdPartyCandidate
	budget           CodexCatalogBudget
}

func newCodexAdminFakeEvaluator() *codexAdminFakeEvaluator {
	return &codexAdminFakeEvaluator{
		inputFingerprint: "input-fp-1",
		candidates: []CodexThirdPartyCandidate{
			{Slug: "gpt-5.5", DisplayName: "gpt-5.5", Native: true, Status: CodexModelStatusNative},
			{Slug: "kimi-k3", DisplayName: "kimi-k3", CanEnable: true, Status: CodexModelStatusConfigurable, Fingerprint: "fp-kimi"},
			{Slug: "deepseek-flash", DisplayName: "deepseek-flash", CanEnable: true, Status: CodexModelStatusConfigurable, Fingerprint: "fp-ds"},
			{Slug: "glm-5.3", DisplayName: "glm-5.3", Status: CodexModelStatusNeedsCapability, Reasons: []string{"缺少上下文窗口依据"}},
		},
		budget: CodexCatalogBudget{
			LimitBytes:     CodexCatalogMaxBytes,
			NativeCount:    1,
			WorstCaseBytes: 600000,
			RemainingBytes: CodexCatalogMaxBytes - 600000,
			Complete:       true,
		},
	}
}

func (e *codexAdminFakeEvaluator) EvaluateCodexThirdPartyCatalog(_ context.Context, group *Group, cfg CodexModelVisibilityConfig) (CodexThirdPartyCatalogEvaluation, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.calls++
	e.seen = append(e.seen, cfg.Clone())
	if e.err != nil {
		return CodexThirdPartyCatalogEvaluation{}, e.err
	}
	eval := CodexThirdPartyCatalogEvaluation{
		GroupID:          group.ID,
		GroupPlatform:    group.Platform,
		Revision:         cfg.Revision,
		InputFingerprint: e.inputFingerprint,
		Budget:           e.budget,
	}
	for _, candidate := range e.candidates {
		candidate.Enabled = !candidate.Native && cfg.IsEnabled(candidate.Slug)
		eval.Candidates = append(eval.Candidates, candidate)
	}
	return eval, nil
}

func (e *codexAdminFakeEvaluator) callCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.calls
}

func (e *codexAdminFakeEvaluator) lastSeen() CodexModelVisibilityConfig {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.seen[len(e.seen)-1]
}

// ---------------------------------------------------------------------------
// 测试装置
// ---------------------------------------------------------------------------

const codexAdminTestGroupID int64 = 11

type codexAdminHarness struct {
	t         *testing.T
	svc       *CodexThirdPartyAdminService
	repo      *codexAdminSettingRepo
	settings  *SettingService
	store     *codexAdminFakeStore
	groups    *codexAdminFakeGroups
	accounts  *codexAdminFakeAccounts
	syncer    *codexAdminFakeSyncer
	evaluator *codexAdminFakeEvaluator

	clockMu sync.Mutex
	clock   time.Time
}

func newCodexAdminHarness(t *testing.T) *codexAdminHarness {
	t.Helper()
	repo := newCodexAdminSettingRepo()
	h := &codexAdminHarness{
		t:         t,
		repo:      repo,
		settings:  NewSettingService(repo, nil),
		store:     &codexAdminFakeStore{repo: repo},
		groups:    &codexAdminFakeGroups{groups: map[int64]*Group{codexAdminTestGroupID: {ID: codexAdminTestGroupID, Name: "混合分组", Platform: PlatformComposite}}},
		accounts:  &codexAdminFakeAccounts{byGroup: map[int64][]Account{}},
		syncer:    newCodexAdminFakeSyncer(),
		evaluator: newCodexAdminFakeEvaluator(),
		clock:     time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC),
	}
	h.svc = newCodexThirdPartyAdminService(codexThirdPartyAdminDeps{
		Groups:    h.groups,
		Accounts:  h.accounts,
		Syncer:    h.syncer,
		Settings:  h.settings,
		Store:     h.store,
		Evaluator: h.evaluator,
	})
	h.svc.now = h.now
	return h
}

func (h *codexAdminHarness) now() time.Time {
	h.clockMu.Lock()
	defer h.clockMu.Unlock()
	return h.clock
}

func (h *codexAdminHarness) advance(d time.Duration) {
	h.clockMu.Lock()
	defer h.clockMu.Unlock()
	h.clock = h.clock.Add(d)
}

func codexAdminAccount(id int64, name, apiKey string) Account {
	return Account{
		ID:       id,
		Name:     name,
		Platform: PlatformKimi,
		Type:     AccountTypeAPIKey,
		Status:   StatusActive,
		Credentials: map[string]any{
			"base_url": "https://upstream.example.test/v1",
			"api_key":  apiKey,
		},
	}
}

func (h *codexAdminHarness) setAccounts(accounts ...Account) {
	h.accounts.byGroup[codexAdminTestGroupID] = accounts
}

// seedConfig 直接写入一份展示配置（revision 由参数指定）。
func (h *codexAdminHarness) seedConfig(revision int64, models map[string]CodexModelVisibilityEntry) {
	cfg := NewCodexModelVisibilityConfig()
	cfg.Revision = revision
	for slug, entry := range models {
		cfg.Models[slug] = entry
	}
	raw, err := cfg.Marshal()
	require.NoError(h.t, err)
	require.NoError(h.t, h.repo.Set(context.Background(), CodexModelVisibilitySettingKey(codexAdminTestGroupID), raw))
}

func (h *codexAdminHarness) storedConfig() (CodexModelVisibilityConfig, bool) {
	raw, err := h.repo.GetValue(context.Background(), CodexModelVisibilitySettingKey(codexAdminTestGroupID))
	if errors.Is(err, ErrSettingNotFound) {
		return NewCodexModelVisibilityConfig(), false
	}
	require.NoError(h.t, err)
	cfg, err := ParseCodexModelVisibilityConfig(raw)
	require.NoError(h.t, err)
	return cfg, true
}

func (h *codexAdminHarness) seedSource(account Account, mutate func(*CodexCatalogSourceSnapshot)) CodexCatalogSourceSnapshot {
	snap := CodexCatalogSourceSnapshot{
		AccountID:         account.ID,
		Models:            []string{"model-a", "model-b"},
		Source:            CodexCatalogSourceUpstreamSync,
		Status:            CodexCatalogSourceComplete,
		SourceFingerprint: CodexCatalogSourceFingerprint(&account),
		SyncedAt:          h.now().Add(-time.Hour),
		AttemptedAt:       h.now().Add(-time.Hour),
	}
	if mutate != nil {
		mutate(&snap)
	}
	require.NoError(h.t, h.settings.SaveCodexCatalogSource(context.Background(), snap))
	return snap
}

func (h *codexAdminHarness) loadSource(accountID int64) CodexCatalogSourceSnapshot {
	snap, found, err := h.settings.LoadCodexCatalogSource(context.Background(), accountID)
	require.NoError(h.t, err)
	require.True(h.t, found, "账号 %d 应该有源快照", accountID)
	return snap
}

func sourceByAccount(sources []CodexThirdPartySourceStatus, accountID int64) CodexThirdPartySourceStatus {
	for _, source := range sources {
		if source.AccountID == accountID {
			return source
		}
	}
	panic("source not found")
}

func requireAppError(t *testing.T, err error, want *infraerrors.ApplicationError) {
	t.Helper()
	require.Error(t, err)
	require.ErrorIs(t, err, want)
}

// ---------------------------------------------------------------------------
// 读取
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminGetReturnsConfigEvaluationAndSources(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(101, "kimi-主账号", "sk-live-secret")
	h.setAccounts(acct)
	h.seedConfig(3, map[string]CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})
	h.seedSource(acct, nil)

	state, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	h.svc.waitForBackground()

	require.Equal(t, codexAdminTestGroupID, state.GroupID)
	require.Equal(t, "混合分组", state.GroupName)
	require.Equal(t, PlatformComposite, state.GroupPlatform)
	require.EqualValues(t, 3, state.Config.Revision)
	require.True(t, state.Config.IsEnabled("kimi-k3"))
	require.Equal(t, "input-fp-1", state.Evaluation.InputFingerprint)
	require.Len(t, state.Evaluation.Candidates, 4)
	require.Len(t, state.Sources, 1)

	source := state.Sources[0]
	require.EqualValues(t, 101, source.AccountID)
	require.Equal(t, "kimi-主账号", source.AccountName)
	require.Equal(t, PlatformKimi, source.Platform)
	require.Equal(t, "complete", source.Status)
	require.Equal(t, 2, source.ModelCount)
	require.True(t, source.FingerprintMatches)
	require.False(t, source.Stale)
	require.NotNil(t, source.SyncedAt)
	require.False(t, source.Refreshing)
	require.Zero(t, h.syncer.totalCalls(), "新鲜且指纹匹配的源不应刷新")

	raw, err := json.Marshal(state)
	require.NoError(t, err)
	require.NotContains(t, string(raw), "sk-live-secret", "响应里不得出现任何凭据")
}

func TestCodexThirdPartyAdminGetWithNoConfigReturnsEmptyRevisionZero(t *testing.T) {
	h := newCodexAdminHarness(t)
	state, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	require.EqualValues(t, 0, state.Config.Revision)
	require.Empty(t, state.Config.Models)
	require.NotNil(t, state.Sources, "没有账号时返回空数组而不是 null")
	require.Empty(t, state.Sources)
}

func TestCodexThirdPartyAdminGetMissingGroupIs404AndCleansOrphanConfig(t *testing.T) {
	h := newCodexAdminHarness(t)
	const goneGroup int64 = 404
	key := CodexModelVisibilitySettingKey(goneGroup)
	require.NoError(t, h.repo.Set(context.Background(), key, `{"schema_version":1,"revision":2,"models":{}}`))

	_, err := h.svc.GetGroupState(context.Background(), goneGroup)
	requireAppError(t, err, ErrGroupNotFound)
	code, _ := infraerrors.ToHTTP(err)
	require.Equal(t, 404, code)
	require.Equal(t, []int64{goneGroup}, h.store.deleted)
	require.False(t, h.repo.has(key), "孤儿配置应被惰性删除")
	require.Zero(t, h.evaluator.callCount())
}

func TestCodexThirdPartyAdminGetRejectsIneligibleGroupPlatform(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.groups.groups[12] = &Group{ID: 12, Name: "claude", Platform: PlatformAnthropic}

	_, err := h.svc.GetGroupState(context.Background(), 12)
	requireAppError(t, err, ErrCodexThirdPartyGroupPlatformUnsupported)
	code, _ := infraerrors.ToHTTP(err)
	require.Equal(t, 400, code)
	require.Zero(t, h.evaluator.callCount())
	require.Empty(t, h.store.deleted, "平台不符不是孤儿，不能删除配置")
}

func TestCodexThirdPartyAdminAcceptsEveryEligiblePlatform(t *testing.T) {
	h := newCodexAdminHarness(t)
	for i, platform := range []string{PlatformComposite, PlatformOpenAI, PlatformGrok, PlatformKimi, PlatformZhipu, PlatformDeepseek, PlatformMiniMax} {
		id := int64(100 + i)
		h.groups.groups[id] = &Group{ID: id, Name: platform, Platform: platform}
		_, err := h.svc.GetGroupState(context.Background(), id)
		require.NoError(t, err, platform)
	}
	for i, platform := range []string{PlatformAnthropic, PlatformGemini, PlatformAntigravity} {
		id := int64(200 + i)
		h.groups.groups[id] = &Group{ID: id, Name: platform, Platform: platform}
		_, err := h.svc.GetGroupState(context.Background(), id)
		requireAppError(t, err, ErrCodexThirdPartyGroupPlatformUnsupported)
	}
}

func TestCodexThirdPartyAdminGetCorruptConfigIsErrorNotEmpty(t *testing.T) {
	h := newCodexAdminHarness(t)
	require.NoError(t, h.repo.Set(context.Background(), CodexModelVisibilitySettingKey(codexAdminTestGroupID), `{broken`))

	_, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	requireAppError(t, err, ErrCodexModelVisibilityCorrupt)
	require.Zero(t, h.evaluator.callCount())
	require.Equal(t, `{broken`, h.repo.raw(CodexModelVisibilitySettingKey(codexAdminTestGroupID)), "损坏的配置不能被改动")
}

func TestCodexThirdPartyAdminGetEvaluatorFailureIsInternalError(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.evaluator.err = errors.New("db exploded: password=hunter2")

	_, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	requireAppError(t, err, ErrCodexCatalogEvaluationFailed)
	_, body := infraerrors.ToHTTP(err)
	require.NotContains(t, body.Message, "hunter2", "底层错误细节不得进入响应")
}

func TestCodexThirdPartyAdminGetAccountListFailureIsReported(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.accounts.err = errors.New("db down")
	_, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.ErrorContains(t, err, "db down")
}

// ---------------------------------------------------------------------------
// 源快照：后台刷新的触发条件
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminGetRefreshesMissingStaleAndMismatchedSourcesInBackground(t *testing.T) {
	h := newCodexAdminHarness(t)
	missing := codexAdminAccount(1, "缺失", "k1")
	stale := codexAdminAccount(2, "过期", "k2")
	mismatched := codexAdminAccount(3, "指纹变了", "k3")
	fresh := codexAdminAccount(4, "新鲜", "k4")
	h.setAccounts(missing, stale, mismatched, fresh)

	h.seedSource(stale, func(s *CodexCatalogSourceSnapshot) {
		s.SyncedAt = h.now().Add(-7 * time.Hour)
		s.AttemptedAt = h.now().Add(-7 * time.Hour)
	})
	h.seedSource(mismatched, func(s *CodexCatalogSourceSnapshot) { s.SourceFingerprint = "fingerprint-of-old-config" })
	h.seedSource(fresh, nil)

	// 让同步阻塞，确认读取接口不会等它完成。
	h.syncer.block = make(chan struct{})
	state, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err, "读取不得被后台刷新阻塞")

	require.True(t, sourceByAccount(state.Sources, 1).Refreshing)
	require.Equal(t, "missing", sourceByAccount(state.Sources, 1).Status)
	require.True(t, sourceByAccount(state.Sources, 2).Stale)
	require.True(t, sourceByAccount(state.Sources, 2).Refreshing)
	require.False(t, sourceByAccount(state.Sources, 3).FingerprintMatches)
	require.True(t, sourceByAccount(state.Sources, 3).Refreshing)
	require.False(t, sourceByAccount(state.Sources, 4).Refreshing)

	close(h.syncer.block)
	h.svc.waitForBackground()

	require.Equal(t, 1, h.syncer.callCount(1))
	require.Equal(t, 1, h.syncer.callCount(2))
	require.Equal(t, 1, h.syncer.callCount(3))
	require.Zero(t, h.syncer.callCount(4))
	for _, id := range []int64{1, 2, 3} {
		snap := h.loadSource(id)
		require.Equal(t, CodexCatalogSourceComplete, snap.Status)
		require.Equal(t, []string{"default-model"}, snap.Models)
	}
	require.Equal(t, CodexCatalogSourceFingerprint(&mismatched), h.loadSource(3).SourceFingerprint, "刷新后指纹应更新为账号当前配置")
}

func TestCodexThirdPartyAdminAutoRetryIsThrottledAfterRecentFailure(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "失败中", "k")
	h.setAccounts(acct)
	h.seedSource(acct, func(s *CodexCatalogSourceSnapshot) {
		s.Status = CodexCatalogSourceFailed
		s.LastError = "Upstream model list request failed with HTTP 502"
		s.AttemptedAt = h.now().Add(-30 * time.Second)
	})

	state, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	h.svc.waitForBackground()
	require.Zero(t, h.syncer.totalCalls(), "刚失败过，页面读取不应再次打上游")
	source := sourceByAccount(state.Sources, 1)
	require.Equal(t, "failed", source.Status)
	require.True(t, source.Stale)
	require.Equal(t, "Upstream model list request failed with HTTP 502", source.LastError)
	require.Equal(t, 2, source.ModelCount, "失败后仍保留上一份清单")

	h.advance(3 * time.Minute)
	_, err = h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	h.svc.waitForBackground()
	require.Equal(t, 1, h.syncer.callCount(1), "超过重试间隔后才再次刷新")
}

func TestCodexThirdPartyAdminSourceListQueryUsesGroupAndConcretePlatforms(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.setAccounts(acct)
	h.seedSource(acct, nil)
	// 假实现在 includeGrouped、groupID 为空或平台为空时会报错，通过即说明调用形态正确。
	_, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
}

// ---------------------------------------------------------------------------
// 源刷新本身
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminRefreshSavesNormalizedCompleteList(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.syncer.results[1] = codexAdminSyncResult{models: []string{" kimi-k3", "deepseek-flash", "kimi-k3", "*", "", "glm-5.3"}}

	require.NoError(t, h.svc.refreshSource(&acct))

	snap := h.loadSource(1)
	require.Equal(t, []string{"deepseek-flash", "glm-5.3", "kimi-k3"}, snap.Models)
	require.Equal(t, CodexCatalogSourceComplete, snap.Status)
	require.Equal(t, CodexCatalogSourceUpstreamSync, snap.Source)
	require.Equal(t, CodexCatalogSourceFingerprint(&acct), snap.SourceFingerprint)
	require.True(t, snap.SyncedAt.Equal(h.now()))
	require.True(t, snap.AttemptedAt.Equal(h.now()))
	require.Empty(t, snap.LastError)
}

func TestCodexThirdPartyAdminRefreshDistinguishesRealEmptyListFromFailure(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.seedSource(acct, nil)
	h.syncer.results[1] = codexAdminSyncResult{models: []string{}}

	require.NoError(t, h.svc.refreshSource(&acct))

	snap := h.loadSource(1)
	require.Equal(t, CodexCatalogSourceEmpty, snap.Status, "上游明确返回空列表是真实的空，不是失败")
	require.Empty(t, snap.Models, "真实空列表替换旧清单")
	require.True(t, snap.SyncedAt.Equal(h.now()))
	require.False(t, snap.Stale(h.now()))
	require.Empty(t, snap.LastError)
}

func TestCodexThirdPartyAdminFailedRefreshKeepsPreviousModelsAndSyncedAt(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "sk-very-secret-key")
	previous := h.seedSource(acct, nil)
	h.advance(10 * time.Minute)
	h.syncer.results[1] = codexAdminSyncResult{err: &UpstreamModelSyncError{
		Kind:       UpstreamModelSyncErrorUpstream,
		Message:    "Upstream model list request failed with HTTP 502",
		StatusCode: 502,
		Err:        errors.New(`Get "https://secret-host.example/v1/models?key=sk-very-secret-key": {"error":"raw upstream body"}`),
	}}

	err := h.svc.refreshSource(&acct)
	require.Error(t, err)

	snap := h.loadSource(1)
	require.Equal(t, CodexCatalogSourceFailed, snap.Status)
	require.Equal(t, previous.Models, snap.Models, "失败时保留上一份完整清单")
	require.True(t, snap.SyncedAt.Equal(previous.SyncedAt), "失败时不更新 SyncedAt")
	require.Equal(t, previous.SourceFingerprint, snap.SourceFingerprint)
	require.True(t, snap.AttemptedAt.Equal(h.now()), "失败时更新 AttemptedAt")
	require.Equal(t, "Upstream model list request failed with HTTP 502", snap.LastError)
	require.True(t, snap.Stale(h.now()), "失败的快照必须标记为过期")

	stored := h.repo.raw(CodexCatalogSourceSettingKey(1))
	for _, leaked := range []string{"sk-very-secret-key", "secret-host", "raw upstream body"} {
		require.NotContains(t, stored, leaked, "LastError 与快照不得带出凭据、地址或上游原始正文")
	}
	require.NotContains(t, err.Error(), "sk-very-secret-key")
}

func TestCodexThirdPartyAdminFailedRefreshWithoutPreviousSnapshotStaysUnusable(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.syncer.results[1] = codexAdminSyncResult{err: errors.New("dial tcp 10.0.0.1:443: Bearer sk-abc connection refused")}

	require.Error(t, h.svc.refreshSource(&acct))

	snap := h.loadSource(1)
	require.Equal(t, CodexCatalogSourceFailed, snap.Status)
	require.Empty(t, snap.Models)
	require.True(t, snap.SyncedAt.IsZero())
	require.Empty(t, snap.SourceFingerprint, "从未成功过的失败快照不得匹配任何账号配置")
	require.False(t, snap.Matches(&acct))
	require.Equal(t, "同步上游模型列表失败", snap.LastError)
	require.NotContains(t, h.repo.raw(CodexCatalogSourceSettingKey(1)), "sk-abc")
}

func TestCodexThirdPartyAdminFailedRefreshAfterConfigChangeDoesNotRevalidateOldList(t *testing.T) {
	h := newCodexAdminHarness(t)
	oldAcct := codexAdminAccount(1, "a", "old-key")
	h.seedSource(oldAcct, nil)
	changed := codexAdminAccount(1, "a", "new-key")
	h.syncer.results[1] = codexAdminSyncResult{err: errors.New("boom")}

	require.Error(t, h.svc.refreshSource(&changed))

	snap := h.loadSource(1)
	require.Equal(t, []string{"model-a", "model-b"}, snap.Models)
	require.False(t, snap.Matches(&changed), "旧清单对应的是旧配置，不能因为一次失败的刷新变成匹配当前配置")
}

func TestCodexThirdPartyAdminRefreshTimeoutRecordsFailure(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.svc.refreshTimeout = 30 * time.Millisecond
	acct := codexAdminAccount(1, "a", "k")
	h.syncer.block = make(chan struct{}) // 永不放行，只能靠超时结束

	require.Error(t, h.svc.refreshSource(&acct))

	snap := h.loadSource(1)
	require.Equal(t, CodexCatalogSourceFailed, snap.Status)
	require.Equal(t, "同步超时", snap.LastError)
}

func TestCodexThirdPartyAdminRefreshUsesSixtySecondDeadlineNotRequestContext(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.setAccounts(acct)
	h.syncer.block = make(chan struct{})

	ctx, cancel := context.WithCancel(context.Background())
	_, err := h.svc.GetGroupState(ctx, codexAdminTestGroupID)
	require.NoError(t, err)
	// 请求结束、请求上下文被取消之后，后台任务必须照常完成。
	cancel()
	require.Eventually(t, func() bool { return h.syncer.callCount(1) == 1 }, 2*time.Second, 5*time.Millisecond)
	close(h.syncer.block)
	h.svc.waitForBackground()

	h.syncer.mu.Lock()
	ctxErr := h.syncer.ctxErrAtEnd[1]
	deadline := h.syncer.deadlines[1]
	h.syncer.mu.Unlock()
	require.NoError(t, ctxErr, "后台同步不得继承请求上下文的取消")
	require.Greater(t, deadline, 55*time.Second)
	require.LessOrEqual(t, deadline, 60*time.Second, "单账号超时为 60 秒")
	require.Equal(t, CodexCatalogSourceComplete, h.loadSource(1).Status)
}

func TestCodexThirdPartyAdminConcurrentRefreshOfSameAccountIsMerged(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.syncer.block = make(chan struct{})

	const callers = 6
	var wg sync.WaitGroup
	errs := make([]error, callers)
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			copied := acct
			errs[i] = h.svc.refreshSource(&copied)
		}(i)
	}
	require.Eventually(t, func() bool { return h.syncer.callCount(1) == 1 }, 2*time.Second, 5*time.Millisecond)
	time.Sleep(150 * time.Millisecond) // 留出时间让其余调用者并入同一次刷新
	close(h.syncer.block)
	wg.Wait()

	for _, err := range errs {
		require.NoError(t, err)
	}
	require.Equal(t, 1, h.syncer.callCount(1), "同一账号的并发刷新必须合并为一次上游同步")
}

func TestCodexThirdPartyAdminRefreshConcurrencyIsCappedAtTwo(t *testing.T) {
	h := newCodexAdminHarness(t)
	var accounts []Account
	for i := int64(1); i <= 6; i++ {
		accounts = append(accounts, codexAdminAccount(i, "a", "k"))
	}
	h.setAccounts(accounts...)
	h.syncer.block = make(chan struct{})

	_, err := h.svc.GetGroupState(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)

	require.Eventually(t, func() bool {
		current, _ := h.syncer.currentInflight()
		return current == 2
	}, 2*time.Second, 5*time.Millisecond)
	time.Sleep(100 * time.Millisecond) // 给多余的任务机会越过上限
	current, max := h.syncer.currentInflight()
	require.Equal(t, 2, current)
	require.Equal(t, 2, max, "全局并发上限为 2")
	require.Equal(t, 2, h.syncer.totalCalls())

	close(h.syncer.block)
	h.svc.waitForBackground()
	require.Equal(t, 6, h.syncer.totalCalls(), "排队的任务在放行后全部完成")
	_, max = h.syncer.currentInflight()
	require.LessOrEqual(t, max, 2)
}

// ---------------------------------------------------------------------------
// 手动同步
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminManualSyncForcesFreshSourcesAndReturnsImmediately(t *testing.T) {
	h := newCodexAdminHarness(t)
	fresh := codexAdminAccount(1, "新鲜", "k")
	failedRecently := codexAdminAccount(2, "刚失败", "k")
	h.setAccounts(fresh, failedRecently)
	h.seedSource(fresh, nil)
	h.seedSource(failedRecently, func(s *CodexCatalogSourceSnapshot) {
		s.Status = CodexCatalogSourceFailed
		s.AttemptedAt = h.now().Add(-5 * time.Second)
	})
	h.syncer.block = make(chan struct{})

	result, err := h.svc.SyncSources(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err, "手动同步在后台进行，不得阻塞响应")
	require.Len(t, result.Sources, 2)
	for _, source := range result.Sources {
		require.True(t, source.Refreshing, "账号 %d 应显示为刷新中", source.AccountID)
	}

	close(h.syncer.block)
	h.svc.waitForBackground()
	require.Equal(t, 1, h.syncer.callCount(1), "手动同步无视新鲜度")
	require.Equal(t, 1, h.syncer.callCount(2), "手动同步无视自动重试间隔")
	require.Zero(t, h.evaluator.callCount(), "同步接口不评估目录")
}

func TestCodexThirdPartyAdminManualSyncValidatesGroup(t *testing.T) {
	h := newCodexAdminHarness(t)
	_, err := h.svc.SyncSources(context.Background(), 999)
	requireAppError(t, err, ErrGroupNotFound)
	h.groups.groups[12] = &Group{ID: 12, Platform: PlatformGemini}
	_, err = h.svc.SyncSources(context.Background(), 12)
	requireAppError(t, err, ErrCodexThirdPartyGroupPlatformUnsupported)
}

func TestCodexThirdPartyAdminSecondManualSyncWhileRunningDoesNotDuplicate(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.setAccounts(acct)
	h.syncer.block = make(chan struct{})

	_, err := h.svc.SyncSources(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	require.Eventually(t, func() bool { return h.syncer.callCount(1) == 1 }, 2*time.Second, 5*time.Millisecond)
	_, err = h.svc.SyncSources(context.Background(), codexAdminTestGroupID)
	require.NoError(t, err)
	close(h.syncer.block)
	h.svc.waitForBackground()
	require.Equal(t, 1, h.syncer.callCount(1))
}

// ---------------------------------------------------------------------------
// 预览
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminPreviewOverlaysChangesOnCopy(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(2, map[string]CodexModelVisibilityEntry{"deepseek-flash": {Enabled: true}})

	result, err := h.svc.PreviewChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartyPreviewRequest{
		ExpectedRevision: 2,
		Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}, {Slug: "deepseek-flash", Enabled: false}},
	})
	require.NoError(t, err)
	require.EqualValues(t, 2, result.Revision)
	require.Equal(t, "input-fp-1", result.Evaluation.InputFingerprint)

	seen := h.evaluator.lastSeen()
	require.True(t, seen.IsEnabled("kimi-k3"), "评估器看到的是叠加后的副本")
	require.False(t, seen.IsEnabled("deepseek-flash"))
	stored, _ := h.storedConfig()
	require.EqualValues(t, 2, stored.Revision, "预览不得落库")
	require.True(t, stored.IsEnabled("deepseek-flash"))
	require.False(t, stored.IsEnabled("kimi-k3"))
	require.Zero(t, h.store.casCount())
}

func TestCodexThirdPartyAdminPreviewRevisionMismatchIs409(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(5, nil)
	_, err := h.svc.PreviewChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartyPreviewRequest{
		ExpectedRevision: 4,
		Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	code, _ := infraerrors.ToHTTP(err)
	require.Equal(t, 409, code)
	require.Zero(t, h.evaluator.callCount())
}

func TestCodexThirdPartyAdminPreviewWithNoConfigExpectsRevisionZero(t *testing.T) {
	h := newCodexAdminHarness(t)
	_, err := h.svc.PreviewChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartyPreviewRequest{
		ExpectedRevision: 1, Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	_, err = h.svc.PreviewChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartyPreviewRequest{
		ExpectedRevision: 0, Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	require.NoError(t, err)
}

func TestCodexThirdPartyAdminRejectsInvalidChangeLists(t *testing.T) {
	h := newCodexAdminHarness(t)
	tooMany := make([]CodexThirdPartyChange, codexModelVisibilityMaxModels+1)
	for i := range tooMany {
		tooMany[i] = CodexThirdPartyChange{Slug: "m-" + string(rune('a'+i%26)) + string(rune('a'+i/26%26)) + string(rune('a'+i/676)), Enabled: false}
	}
	tests := map[string][]CodexThirdPartyChange{
		"空列表":    nil,
		"空 slug": {{Slug: "", Enabled: true}},
		"首尾空白":   {{Slug: " kimi-k3", Enabled: true}},
		"通配符":    {{Slug: "kimi-*", Enabled: true}},
		"重复":     {{Slug: "kimi-k3", Enabled: true}, {Slug: "kimi-k3", Enabled: false}},
		"数量过多":   tooMany,
	}
	for name, changes := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := h.svc.PreviewChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartyPreviewRequest{Changes: changes})
			requireAppError(t, err, ErrCodexThirdPartyChangesInvalid)
			_, err = h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{Changes: changes})
			requireAppError(t, err, ErrCodexThirdPartyChangesInvalid)
		})
	}
	require.Zero(t, h.evaluator.callCount())
	require.Zero(t, h.store.casCount())
}

func TestCodexThirdPartyAdminOverlayRemovesClosedUnverifiedEntriesButKeepsVerification(t *testing.T) {
	verification := &CodexModelVerification{Fingerprint: "fp-kimi", VerifiedAt: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
	cfg := NewCodexModelVisibilityConfig()
	cfg.Revision = 4
	cfg.Models["kimi-k3"] = CodexModelVisibilityEntry{Enabled: true, Verification: verification}
	cfg.Models["deepseek-flash"] = CodexModelVisibilityEntry{Enabled: true}

	next, err := overlayCodexVisibilityChanges(cfg, []CodexThirdPartyChange{
		{Slug: "kimi-k3", Enabled: false},
		{Slug: "deepseek-flash", Enabled: false},
		{Slug: "glm-5.3", Enabled: true},
	})
	require.NoError(t, err)
	require.False(t, next.IsEnabled("kimi-k3"))
	require.NotNil(t, next.Models["kimi-k3"].Verification, "已有验收记录的条目只关闭，不删除记录")
	_, listed := next.Models["deepseek-flash"]
	require.False(t, listed, "关闭且无验收记录的条目直接移除")
	require.True(t, next.IsEnabled("glm-5.3"))
	require.True(t, cfg.IsEnabled("kimi-k3"), "叠加不得改动原配置")
	require.True(t, cfg.IsEnabled("deepseek-flash"))
	require.EqualValues(t, 4, next.Revision)
}

// ---------------------------------------------------------------------------
// 保存
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminSaveCloseOnlyNeverCallsEvaluatorOrUpstream(t *testing.T) {
	h := newCodexAdminHarness(t)
	acct := codexAdminAccount(1, "a", "k")
	h.setAccounts(acct)
	h.seedConfig(3, map[string]CodexModelVisibilityEntry{
		"kimi-k3":        {Enabled: true},
		"deepseek-flash": {Enabled: true},
	})
	// 评估器和上游全部不可用，纯关闭也必须成功。
	h.evaluator.err = errors.New("evaluator must not be called")
	h.syncer.results[1] = codexAdminSyncResult{err: errors.New("upstream must not be called")}
	h.evaluator.budget.Complete = false

	result, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 3,
		Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: false}},
	})
	require.NoError(t, err)
	h.svc.waitForBackground()

	require.Zero(t, h.evaluator.callCount(), "纯关闭不得调用评估器")
	require.Zero(t, h.syncer.totalCalls(), "纯关闭不得依赖上游")
	require.EqualValues(t, 4, result.Config.Revision)
	require.Nil(t, result.Evaluation)
	require.False(t, result.Config.IsEnabled("kimi-k3"))
	require.True(t, result.Config.IsEnabled("deepseek-flash"))
	stored, _ := h.storedConfig()
	require.EqualValues(t, 4, stored.Revision)
	require.False(t, stored.IsEnabled("kimi-k3"))
	require.True(t, stored.IsEnabled("deepseek-flash"))
}

func TestCodexThirdPartyAdminSaveCloseOnlyStillChecksRevisionAndGroup(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(3, map[string]CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})
	closeKimi := []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: false}}

	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{ExpectedRevision: 2, Changes: closeKimi})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	_, err = h.svc.SaveChanges(context.Background(), 999, CodexThirdPartySaveRequest{ExpectedRevision: 3, Changes: closeKimi})
	requireAppError(t, err, ErrGroupNotFound)
	h.groups.groups[12] = &Group{ID: 12, Platform: PlatformAnthropic}
	_, err = h.svc.SaveChanges(context.Background(), 12, CodexThirdPartySaveRequest{ExpectedRevision: 0, Changes: closeKimi})
	requireAppError(t, err, ErrCodexThirdPartyGroupPlatformUnsupported)

	stored, _ := h.storedConfig()
	require.True(t, stored.IsEnabled("kimi-k3"), "校验失败时配置不变")
	require.EqualValues(t, 3, stored.Revision)
}

func TestCodexThirdPartyAdminSaveEnableSucceedsWithMatchingFingerprint(t *testing.T) {
	h := newCodexAdminHarness(t)
	result, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 0,
		InputFingerprint: "input-fp-1",
		Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}, {Slug: "deepseek-flash", Enabled: true}},
	})
	require.NoError(t, err)
	require.EqualValues(t, 1, result.Config.Revision)
	require.True(t, result.Config.IsEnabled("kimi-k3"))
	require.True(t, result.Config.IsEnabled("deepseek-flash"))
	require.NotNil(t, result.Evaluation)
	require.Equal(t, 1, h.evaluator.callCount(), "含开启的保存必须重新评估")
	require.True(t, h.evaluator.lastSeen().IsEnabled("kimi-k3"), "评估的是叠加后的配置")
	stored, found := h.storedConfig()
	require.True(t, found)
	require.True(t, stored.IsEnabled("kimi-k3"))
}

func TestCodexThirdPartyAdminSaveEnableWithStaleFingerprintIs409AndWritesNothing(t *testing.T) {
	h := newCodexAdminHarness(t)
	for _, fingerprint := range []string{"input-fp-OLD", "", "  "} {
		_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
			ExpectedRevision: 0,
			InputFingerprint: fingerprint,
			Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
		})
		requireAppError(t, err, ErrCodexCatalogPreviewStale)
		code, _ := infraerrors.ToHTTP(err)
		require.Equal(t, 409, code)
	}
	require.Zero(t, h.store.casCount())
	_, found := h.storedConfig()
	require.False(t, found)
}

func TestCodexThirdPartyAdminSaveRejectsWholeBatchWhenAnyItemCannotBeEnabled(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(2, map[string]CodexModelVisibilityEntry{"deepseek-flash": {Enabled: true}})

	// kimi-k3 本身可开启，glm-5.3 缺能力：整批（包括顺带的关闭项）都不能保存。
	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 2,
		InputFingerprint: "input-fp-1",
		Changes: []CodexThirdPartyChange{
			{Slug: "kimi-k3", Enabled: true},
			{Slug: "glm-5.3", Enabled: true},
			{Slug: "deepseek-flash", Enabled: false},
		},
	})
	requireAppError(t, err, ErrCodexModelEnableRejected)
	code, body := infraerrors.ToHTTP(err)
	require.Equal(t, 400, code)
	require.Equal(t, "缺少上下文窗口依据", body.Metadata["model:glm-5.3"], "逐项给出原因")
	require.NotContains(t, body.Metadata, "model:kimi-k3")
	require.NotContains(t, body.Metadata, "budget")

	require.Zero(t, h.store.casCount(), "整批拒绝，不允许半成功")
	stored, _ := h.storedConfig()
	require.EqualValues(t, 2, stored.Revision)
	require.False(t, stored.IsEnabled("kimi-k3"))
	require.True(t, stored.IsEnabled("deepseek-flash"))
}

func TestCodexThirdPartyAdminSaveRejectsNativeUnknownAndReasonlessBlockedModels(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.evaluator.candidates = append(h.evaluator.candidates,
		CodexThirdPartyCandidate{Slug: "conflicted", CanEnable: false, Status: CodexModelStatusConflict})

	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 0,
		InputFingerprint: "input-fp-1",
		Changes: []CodexThirdPartyChange{
			{Slug: "gpt-5.5", Enabled: true},
			{Slug: "not-a-candidate", Enabled: true},
			{Slug: "conflicted", Enabled: true},
		},
	})
	requireAppError(t, err, ErrCodexModelEnableRejected)
	_, body := infraerrors.ToHTTP(err)
	require.Contains(t, body.Metadata["model:gpt-5.5"], "原生 GPT")
	require.Contains(t, body.Metadata["model:not-a-candidate"], "候选")
	require.Contains(t, body.Metadata["model:conflicted"], "conflict")
	require.Zero(t, h.store.casCount())
}

func TestCodexThirdPartyAdminSaveRejectsWhenBudgetIncompleteOrOverLimit(t *testing.T) {
	changes := []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}}
	tests := map[string]func(*CodexCatalogBudget){
		"预算未完成": func(b *CodexCatalogBudget) { b.Complete = false; b.Reason = "基础目录账号不可得" },
		"超过上限":  func(b *CodexCatalogBudget) { b.WorstCaseBytes = CodexCatalogMaxBytes + 1 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			h := newCodexAdminHarness(t)
			mutate(&h.evaluator.budget)
			_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
				ExpectedRevision: 0, InputFingerprint: "input-fp-1", Changes: changes,
			})
			requireAppError(t, err, ErrCodexModelEnableRejected)
			_, body := infraerrors.ToHTTP(err)
			require.Contains(t, body.Metadata, "budget")
			require.NotContains(t, body.Metadata, "model:kimi-k3", "kimi-k3 自身没问题，只是预算不过")
			require.Zero(t, h.store.casCount())
		})
	}
}

func TestCodexThirdPartyAdminSaveAcceptsBudgetExactlyAtLimit(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.evaluator.budget.WorstCaseBytes = CodexCatalogMaxBytes
	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 0, InputFingerprint: "input-fp-1",
		Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	require.NoError(t, err, "恰好等于上限（1048576 字节）可以通过")
}

func TestCodexThirdPartyAdminSaveRevisionConflictSkipsEvaluation(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(7, nil)
	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 6, InputFingerprint: "input-fp-1",
		Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	require.Zero(t, h.evaluator.callCount())
	require.Zero(t, h.store.casCount())
}

func TestCodexThirdPartyAdminSaveSurfacesStoreConflictFromConcurrentWriter(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(1, nil)
	// 读取之后、提交之前，另一个节点把 revision 推到了 2。
	h.store.beforeCAS = func() { h.seedConfig(2, map[string]CodexModelVisibilityEntry{"glm-5.3": {Enabled: true}}) }

	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 1,
		Changes:          []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: false}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	stored, _ := h.storedConfig()
	require.EqualValues(t, 2, stored.Revision, "不得覆盖别人的修改")
	require.True(t, stored.IsEnabled("glm-5.3"))
}

func TestCodexThirdPartyAdminSaveFirstCreateRaceReturnsConflict(t *testing.T) {
	h := newCodexAdminHarness(t)
	// 两个管理员同时对一个还没有配置的分组首次保存：对方先建好了行。
	h.store.beforeCAS = func() { h.seedConfig(1, map[string]CodexModelVisibilityEntry{"deepseek-flash": {Enabled: true}}) }

	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 0, InputFingerprint: "input-fp-1",
		Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityConflict)
	stored, _ := h.storedConfig()
	require.False(t, stored.IsEnabled("kimi-k3"))
	require.True(t, stored.IsEnabled("deepseek-flash"))
}

func TestCodexThirdPartyAdminSaveCorruptConfigIsNeverOverwritten(t *testing.T) {
	h := newCodexAdminHarness(t)
	key := CodexModelVisibilitySettingKey(codexAdminTestGroupID)
	require.NoError(t, h.repo.Set(context.Background(), key, `not json`))
	_, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 0, Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: false}},
	})
	requireAppError(t, err, ErrCodexModelVisibilityCorrupt)
	require.Equal(t, `not json`, h.repo.raw(key))
	require.Zero(t, h.store.casCount())
}

// ---------------------------------------------------------------------------
// 标记已验证
// ---------------------------------------------------------------------------

func TestCodexThirdPartyAdminMarkVerifiedWritesVerificationBoundToFingerprint(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(4, map[string]CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})

	result, err := h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 4, Slug: "kimi-k3", Fingerprint: "fp-kimi", Note: " 桌面端流式与 apply_patch 验收通过 ",
	})
	require.NoError(t, err)
	require.EqualValues(t, 5, result.Config.Revision)
	entry := result.Config.Models["kimi-k3"]
	require.True(t, entry.Enabled)
	require.NotNil(t, entry.Verification)
	require.Equal(t, "fp-kimi", entry.Verification.Fingerprint)
	require.Equal(t, "桌面端流式与 apply_patch 验收通过", entry.Verification.Note)
	require.True(t, entry.Verification.VerifiedAt.Equal(h.now()))
	stored, _ := h.storedConfig()
	require.EqualValues(t, 5, stored.Revision)
	require.Equal(t, "fp-kimi", stored.Models["kimi-k3"].Verification.Fingerprint)
}

func TestCodexThirdPartyAdminMarkVerifiedRequiresEnabledModelAndMatchingFingerprint(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(4, map[string]CodexModelVisibilityEntry{
		"kimi-k3":        {Enabled: true},
		"deepseek-flash": {Enabled: false, Verification: &CodexModelVerification{Fingerprint: "old", VerifiedAt: h.now()}},
	})
	call := func(slug, fingerprint string, revision int64) error {
		_, err := h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
			ExpectedRevision: revision, Slug: slug, Fingerprint: fingerprint,
		})
		return err
	}

	requireAppError(t, call("kimi-k3", "fp-OTHER", 4), ErrCodexVerificationStale)
	requireAppError(t, call("deepseek-flash", "fp-ds", 4), ErrCodexModelNotEnabled)
	requireAppError(t, call("glm-5.3", "x", 4), ErrCodexModelNotEnabled)
	requireAppError(t, call("kimi-k3", "fp-kimi", 3), ErrCodexModelVisibilityConflict)
	for _, err := range []error{call("kimi-k3", "fp-OTHER", 4), call("deepseek-flash", "fp-ds", 4)} {
		code, _ := infraerrors.ToHTTP(err)
		require.Equal(t, 409, code)
	}
	require.Zero(t, h.store.casCount(), "任何一种失败都不写库")

	stored, _ := h.storedConfig()
	require.Nil(t, stored.Models["kimi-k3"].Verification)
}

func TestCodexThirdPartyAdminMarkVerifiedRejectsEmptyFingerprintAndBadInput(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(1, map[string]CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})
	// 候选自身没有指纹时，即使请求也传空串也不能「相等」通过。
	h.evaluator.candidates[1].Fingerprint = ""

	_, err := h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 1, Slug: "kimi-k3", Fingerprint: "",
	})
	require.Error(t, err)
	code, _ := infraerrors.ToHTTP(err)
	require.Equal(t, 400, code)

	_, err = h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 1, Slug: "kimi-k3", Fingerprint: "anything",
	})
	requireAppError(t, err, ErrCodexVerificationStale)

	_, err = h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 1, Slug: "kimi-*", Fingerprint: "x",
	})
	requireAppError(t, err, ErrCodexThirdPartyChangesInvalid)

	longNote := make([]rune, codexThirdPartyVerifyNoteMaxRunes+1)
	for i := range longNote {
		longNote[i] = '字'
	}
	h.evaluator.candidates[1].Fingerprint = "fp-kimi"
	_, err = h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 1, Slug: "kimi-k3", Fingerprint: "fp-kimi", Note: string(longNote),
	})
	require.Error(t, err)
	code, _ = infraerrors.ToHTTP(err)
	require.Equal(t, 400, code)
	require.Zero(t, h.store.casCount())
}

func TestCodexThirdPartyAdminVerificationSurvivesCloseAndReopen(t *testing.T) {
	h := newCodexAdminHarness(t)
	h.seedConfig(1, map[string]CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})
	_, err := h.svc.MarkVerified(context.Background(), codexAdminTestGroupID, CodexThirdPartyVerifyRequest{
		ExpectedRevision: 1, Slug: "kimi-k3", Fingerprint: "fp-kimi",
	})
	require.NoError(t, err)

	closed, err := h.svc.SaveChanges(context.Background(), codexAdminTestGroupID, CodexThirdPartySaveRequest{
		ExpectedRevision: 2, Changes: []CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: false}},
	})
	require.NoError(t, err)
	require.False(t, closed.Config.IsEnabled("kimi-k3"))
	require.NotNil(t, closed.Config.Models["kimi-k3"].Verification, "关闭不丢验收记录；指纹是否仍有效由评估器按指纹判定")
}

// ---------------------------------------------------------------------------
// 构造函数
// ---------------------------------------------------------------------------

func TestNewCodexThirdPartyAdminServiceWiresConcreteDependencies(t *testing.T) {
	repo := newCodexAdminSettingRepo()
	svc := NewCodexThirdPartyAdminService(NewSettingService(repo, nil), nil, nil, nil,
		&codexAdminFakeStore{repo: repo}, newCodexAdminFakeEvaluator())
	require.NotNil(t, svc)
	require.Equal(t, codexThirdPartyRefreshTimeout, svc.refreshTimeout)
	require.Equal(t, codexThirdPartyRefreshConcurrency, cap(svc.refreshSlots))
	require.Equal(t, 60*time.Second, codexThirdPartyRefreshTimeout)
	require.Equal(t, 2, codexThirdPartyRefreshConcurrency)
}
