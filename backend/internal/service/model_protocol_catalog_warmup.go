package service

// model_protocol_catalog_warmup.go 在网关启动后预热按模型分流账号的上游模型目录。
//
// 目录缓存是进程内的，重启后为空；页面读取只用缓存、不发上游请求，所以重启后管理页会在目录
// 被第一次真实请求触发刷新前，一直把按模型分流账号的协议判成「无法确认」。预热在后台延迟几秒后
// 对所有持久启用的按模型分流账号各拉一次目录：不阻塞启动、限并发、失败只记日志。
// 拉取复用 refreshModelProtocolCatalog（与运行时后台刷新同一个拉取函数与请求头），
// 只拉第三方 API Key 账号的 /models，不触碰官方 ChatGPT 账号。

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"
)

const (
	// modelProtocolCatalogWarmupDelay 是启动后到开始预热的延迟，让出启动阶段的数据库与网络。
	modelProtocolCatalogWarmupDelay = 5 * time.Second
	// modelProtocolCatalogWarmupConcurrency 是预热的并发上限。
	modelProtocolCatalogWarmupConcurrency = 2
)

// ModelProtocolCatalogWarmupService 是启动预热的后台任务，由依赖装配创建并 Start。
type ModelProtocolCatalogWarmupService struct {
	accountRepo AccountRepository
	openAI      *OpenAIGatewayService

	delay       time.Duration
	concurrency int
	// accountTimeout 是单账号拉取时限，默认与运行时后台刷新一致。
	accountTimeout time.Duration

	mu      sync.Mutex
	started bool
	stopped bool
	ctx     context.Context
	cancel  context.CancelFunc
	wg      sync.WaitGroup
}

// NewModelProtocolCatalogWarmupService 创建预热任务（尚未启动）。
func NewModelProtocolCatalogWarmupService(accountRepo AccountRepository, openAI *OpenAIGatewayService) *ModelProtocolCatalogWarmupService {
	ctx, cancel := context.WithCancel(context.Background())
	return &ModelProtocolCatalogWarmupService{
		accountRepo:    accountRepo,
		openAI:         openAI,
		delay:          modelProtocolCatalogWarmupDelay,
		concurrency:    modelProtocolCatalogWarmupConcurrency,
		accountTimeout: modelProtocolCatalogTimeout,
		ctx:            ctx,
		cancel:         cancel,
	}
}

// ProvideModelProtocolCatalogWarmupService 创建并启动预热任务，供依赖注入使用。
func ProvideModelProtocolCatalogWarmupService(accountRepo AccountRepository, openAI *OpenAIGatewayService) *ModelProtocolCatalogWarmupService {
	svc := NewModelProtocolCatalogWarmupService(accountRepo, openAI)
	svc.Start()
	return svc
}

// Start 在后台启动预热，立即返回；重复调用或已停止时不做任何事。
func (s *ModelProtocolCatalogWarmupService) Start() {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.started || s.stopped {
		s.mu.Unlock()
		return
	}
	s.started = true
	s.wg.Add(1)
	s.mu.Unlock()
	go func() {
		defer s.wg.Done()
		defer func() {
			if r := recover(); r != nil {
				slog.Error("model_protocol_catalog_warmup_panic", "panic", r)
			}
		}()
		timer := time.NewTimer(s.delay)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-s.ctx.Done():
			return
		}
		s.runOnce(s.ctx)
	}()
}

// Stop 取消预热并等待后台协程退出。
func (s *ModelProtocolCatalogWarmupService) Stop() {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		return
	}
	s.stopped = true
	s.mu.Unlock()
	s.cancel()
	s.wg.Wait()
}

// runOnce 对所有持久启用的按模型分流账号拉一次缺失的目录，返回成功与失败的账号数。
// 已有目录（运行时请求先到一步）的账号跳过；失败只记日志，不影响其它账号。
func (s *ModelProtocolCatalogWarmupService) runOnce(ctx context.Context) (refreshed, failed int) {
	if s.accountRepo == nil || s.openAI == nil {
		return 0, 0
	}
	platforms := modelCatalogPlatforms()
	if len(platforms) == 0 {
		return 0, 0
	}
	accounts, err := s.accountRepo.ListModelAvailabilityCandidates(ctx, nil, platforms, true)
	if err != nil {
		slog.Warn("model_protocol_catalog_warmup_list_failed", "error", err)
		return 0, 0
	}
	var targets []*Account
	for i := range accounts {
		account := &accounts[i]
		if !compositeAccountPersistentlyEnabled(account) {
			continue
		}
		if state := s.openAI.modelCatalogCacheStateOf(account); !state.Needed || state.Loaded {
			continue
		}
		targets = append(targets, account)
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i].ID < targets[j].ID })

	sem := make(chan struct{}, max(s.concurrency, 1))
	var (
		wg sync.WaitGroup
		mu sync.Mutex
	)
	for _, account := range targets {
		wg.Add(1)
		go func() {
			defer wg.Done()
			recorded := false
			defer func() {
				// 单个账号的拉取发生 panic：计为该账号失败并记日志，不能让进程退出。
				if r := recover(); r != nil && !recorded {
					slog.Error("model_protocol_catalog_warmup_panic", "account_id", account.ID, "panic", fmt.Sprint(r))
					mu.Lock()
					failed++
					mu.Unlock()
				}
			}()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				return
			}
			defer func() { <-sem }()
			accountCtx, cancel := context.WithTimeout(ctx, s.accountTimeout)
			defer cancel()
			_, err := s.openAI.refreshModelProtocolCatalog(accountCtx, account, modelCatalogTriggerStartupWarmup)
			mu.Lock()
			defer mu.Unlock()
			recorded = true
			if err != nil {
				failed++
				slog.Warn("model_protocol_catalog_warmup_failed", "account_id", account.ID, "reason", codexTPRefreshReason(err))
				return
			}
			refreshed++
		}()
	}
	wg.Wait()
	if len(targets) > 0 {
		slog.Info("model_protocol_catalog_warmup_done", "accounts", len(targets), "refreshed", refreshed, "failed", failed)
	}
	return refreshed, failed
}

// modelCatalogPlatforms 返回按模型分流且提供上游模型目录的平台（预热只需要拉这些平台的账号）。
func modelCatalogPlatforms() []string {
	platforms := make([]string, 0, len(providerProfiles))
	for platform, profile := range providerProfiles {
		if profile != nil && profile.Routing == ProviderRoutingByModel && profile.ModelCatalog {
			platforms = append(platforms, platform)
		}
	}
	sort.Strings(platforms)
	return platforms
}
