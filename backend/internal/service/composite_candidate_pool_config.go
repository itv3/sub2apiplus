package service

// 候选池的「配置态」缓存与「运行态」实时读取。
//
// 候选池每个 Composite 文本请求都要知道"分组里哪些账号声明了这个模型"。把账号全量加载放在
// 每个请求上代价太大（约 4 到 5 条 SQL），所以把账号拆成两部分：
//
//   - 配置态（分组里有哪些账号、平台、类型、映射、凭据、优先级、ID）变化很慢，按分组缓存。
//     复用网关服务的 modelsListCache：同一份缓存、同一个 TTL、同一条失效通道——键放在账号归属
//     解析缓存的前缀下，GatewayService.InvalidateAvailableModelsCache 清归属缓存时一并清掉本缓存。
//     缓存只用来挑出"声明命中的候选账号"，不参与任何资格判断。
//   - 运行态（状态、可调度开关、限流、过载、临时不可调度、模型级限流、配额自动暂停等）必须实时：
//     声明命中的候选账号逐个用调度器终检同一个读取入口（getSchedulableAccount：调度快照的账号
//     缓存，没有快照时按 ID 读仓储，并套用调度阈值与 Grok 免费额度闸门）取最新账号，
//     之后的资格过滤、优先级排序与零转换判定都基于这份最新账号，而不是缓存里的旧副本。
//
// 因此冷却、限流、停用等状态变化立即生效；配置变化中"账号被改动"（映射、优先级、地址、
// 解绑分组、删除）也因为按最新账号复核而立即生效，只有"新增声明账号"要等缓存过期或失效。

import (
	"context"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/domain"
	gocache "github.com/patrickmn/go-cache"
)

// compositeCandidateConfigLoadTimeout 是单次配置态加载的超时。加载放在与请求上下文脱钩的
// 上下文里：并发请求共享同一次加载，不能因为首个请求被客户端取消而让其它请求一起失败。
const compositeCandidateConfigLoadTimeout = 5 * time.Second

// compositeCandidateConfigCacheModel 是配置态缓存键里占 "模型" 位置的哨兵。键整体放在账号归属
// 解析缓存的前缀下（compositeModelOwnershipCacheKey），失效通道因此自动覆盖；控制字符开头保证
// 不会与任何真实模型名的归属缓存键冲突。
const compositeCandidateConfigCacheModel = "\x00composite-candidate-pool-config"

// compositeCandidateConfig 是一个候选账号的配置态快照。account 只在加载时写入，之后只读：
// 需要使用账号时必须先取副本（账号方法会写自身的内部缓存字段），资格判断一律基于实时读取的账号。
type compositeCandidateConfig struct {
	account Account
	// mapping 是凭据 model_mapping 的解析结果（只读）。
	mapping map[string]string
}

// strength 返回该账号对模型的声明强度。
func (c *compositeCandidateConfig) strength(model string) CompositeClaimStrength {
	if len(c.mapping) == 0 || model == "" {
		return CompositeClaimNone
	}
	return compositeMappingClaimStrength(c.account.Platform, c.mapping, model)
}

// compositeCandidateGroupConfig 是一个分组的配置态快照，加载后不可变，可被并发请求共享。
type compositeCandidateGroupConfig struct {
	candidates []*compositeCandidateConfig
	byID       map[int64]*compositeCandidateConfig
}

func newCompositeCandidateGroupConfig(accounts []Account) *compositeCandidateGroupConfig {
	cfg := &compositeCandidateGroupConfig{
		candidates: make([]*compositeCandidateConfig, 0, len(accounts)),
		byID:       make(map[int64]*compositeCandidateConfig, len(accounts)),
	}
	for i := range accounts {
		entry := &compositeCandidateConfig{account: accounts[i]}
		if accounts[i].Credentials != nil {
			entry.mapping = stringMappingFromRaw(accounts[i].Credentials["model_mapping"])
		}
		cfg.candidates = append(cfg.candidates, entry)
		cfg.byID[entry.account.ID] = entry
	}
	return cfg
}

// claimTier 按声明强度分层挑出候选：有强声明只取强声明，否则取弱声明。
func (c *compositeCandidateGroupConfig) claimTier(model string) (CompositeClaimStrength, []*compositeCandidateConfig) {
	return compositeClaimTier(c.candidates, func(entry *compositeCandidateConfig) CompositeClaimStrength {
		return entry.strength(model)
	})
}

// configCache 返回配置态缓存及其 TTL（与模型列表缓存、账号归属解析缓存相同）。
// 网关服务没有模型列表缓存时返回 nil，候选池退化为每个请求都加载。
func (p *CompositeCandidatePool) configCache() (*gocache.Cache, time.Duration) {
	if p == nil || p.gateway == nil || p.gateway.modelsListCache == nil {
		return nil, 0
	}
	return p.gateway.modelsListCache, p.gateway.modelsListCacheTTL
}

// groupConfig 返回分组的配置态快照：命中缓存直接返回；未命中时并发请求合并成一次账号加载。
func (p *CompositeCandidatePool) groupConfig(ctx context.Context, groupID int64) (*compositeCandidateGroupConfig, error) {
	key := compositeModelOwnershipCacheKey(groupID, compositeCandidateConfigCacheModel)
	cache, ttl := p.configCache()
	if cache != nil {
		if value, found := cache.Get(key); found {
			if cfg, typed := value.(*compositeCandidateGroupConfig); typed {
				return cfg, nil
			}
		}
	}
	value, err, _ := p.configLoads.Do(key, func() (any, error) {
		loadCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), compositeCandidateConfigLoadTimeout)
		defer cancel()
		accounts, listErr := p.accounts.ListModelAvailabilityCandidates(loadCtx, &groupID, domain.ConcretePlatformIDs(), false)
		if listErr != nil {
			return nil, listErr
		}
		cfg := newCompositeCandidateGroupConfig(accounts)
		if cache != nil {
			cache.Set(key, cfg, ttl)
		}
		return cfg, nil
	})
	if err != nil {
		return nil, err
	}
	cfg, _ := value.(*compositeCandidateGroupConfig)
	return cfg, nil
}

// freshAccount 返回候选账号的实时账号：用调度器终检同一个读取入口取最新账号，并复核
// 账号仍属于本分组、平台没变、对该模型仍有声明。读取或复核失败时返回配置态副本，
// 同时把原因记入 refreshFailed，该账号之后的资格评估一律不合格（配置态副本只用于
// 全员不合格时的排序写回）。同一次解析内每个账号只读取一次。
//
// 注意：getSchedulableAccount 会套用调度阈值判定，账号用量越过阈值时该判定会把账号标记为
// 临时不可调度（幂等的状态迁移，调度器对它评估的每个账号都会这么做）。候选池因此会对声明命中的
// 跨平台候选账号提前触发这一迁移，除此之外不写任何状态。
func (r *compositeCandidateRun) freshAccount(cfg *compositeCandidateConfig) *Account {
	id := cfg.account.ID
	if account, ok := r.fresh[id]; ok {
		return account
	}
	account, reason := r.readFreshAccount(cfg)
	if reason != "" {
		stale := cfg.account
		account = &stale
		r.refreshFailed[id] = reason
	}
	r.fresh[id] = account
	return account
}

func (r *compositeCandidateRun) readFreshAccount(cfg *compositeCandidateConfig) (*Account, string) {
	var (
		account *Account
		err     error
	)
	if domain.UsesOpenAIGateway(cfg.account.Platform) {
		svc := r.pool.openAI
		if svc == nil || (svc.schedulerSnapshot == nil && svc.accountRepo == nil) {
			return nil, "openai_gateway_unavailable"
		}
		if account, err = svc.getSchedulableAccount(r.ctx, cfg.account.ID); err == nil && account != nil &&
			!svc.openAIAccountMatchesSchedulingGroup(account, &r.groupID) {
			return nil, "not_in_group"
		}
	} else {
		svc := r.pool.gateway
		if svc == nil || (svc.schedulerSnapshot == nil && svc.accountRepo == nil) {
			return nil, "anthropic_gateway_unavailable"
		}
		if account, err = svc.getSchedulableAccount(r.ctx, cfg.account.ID); err == nil && account != nil &&
			!svc.compositeCandidateInGroup(account, r.groupID) {
			return nil, "not_in_group"
		}
	}
	if err != nil {
		return nil, "account_refresh_failed"
	}
	if account == nil {
		// 与调度器一致：快照里没有、已被调度阈值或 Grok 免费额度闸门挡掉的账号视为不可调度。
		return nil, "account_unavailable_for_scheduling"
	}
	if account.Platform != cfg.account.Platform {
		return nil, "platform_changed"
	}
	if !CompositeAccountClaimsModel(account, r.model) {
		return nil, "claim_removed"
	}
	return account, ""
}

// compositeCandidateInGroup 复核 Anthropic 网关族账号仍属于分组。简易模式下调度器不按分组过滤，
// 这里同样放行。
func (s *GatewayService) compositeCandidateInGroup(account *Account, groupID int64) bool {
	if s != nil && s.cfg != nil && s.cfg.RunMode == config.RunModeSimple {
		return account != nil
	}
	return s.isAccountInGroup(account, &groupID)
}
