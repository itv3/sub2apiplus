package service

// Composite 候选池的"同级按剩余容量随机选择"与满载换号计划。
//
// 背景：候选池原先按"零转换优先、账号 Priority 升序、账号 ID 升序"取首选账号，再把首选账号
// 所在平台写成目标平台。同级（零转换属性与 Priority 都相同）的账号分属不同平台时，
// 永远是账号 ID 最小的那个平台被选中，其余平台的同级账号只能在它满载排队时干等。
//
// 本文件新增的行为（可用环境变量单独关闭，见 CompositeCapacitySelectionEnabled）：
//
//  1. 同级账号按"剩余并发容量"为权重随机排序：剩余容量相同时等概率，满载账号不参与随机；
//  2. 一个 Priority 级别里全部满载时，顺延到下一个 Priority 级别；全部级别都满载才退回原来的
//     确定性顺序（首选仍是原排序第一名，请求照旧在它身上排队）；
//  3. 把"首选平台之外还有空位的平台"按同样顺序写成换号计划（CompositeCapacityPlan）随路由决策
//     下发。调度器在首选平台内部抢不到槽位（快照有空位但实际抢槽失败，或首选平台整体满载）时，
//     处理器按计划临时换到下一个平台，见 composite_capacity_fallback.go。
//
// 边界（与方案一致）：
//   - 资格检查不变，仍由候选池原有的 evaluate 完成，本文件只重排"已合格"的候选；
//   - 只读：不抢槽、不递增任何计数，槽位的抢占与释放仍由各平台调度器和处理器的原子抢槽完成；
//   - 目录页共用的稳定排序（compositeReceiverGroup / compositeCandidateLess）保持原样；
//   - 续链请求不进入本逻辑（续链路径锁定归属账号，不下发换号计划）。

import (
	"context"
	"math/rand/v2"
	"os"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/domain"
)

// compositeCapacitySelectionEnv 是"同级按容量随机选择与满载换号"的总开关。
// 取值 0、false、off、no、disabled 时关闭，未设置或其它取值一律开启。
const compositeCapacitySelectionEnv = "SUB2API_COMPOSITE_CAPACITY_SELECTION"

// compositeEnvSwitchEnabled 读取环境变量开关，默认开启。每次现读，便于运维不重启也能在容器里验证，
// 单测可以直接 t.Setenv。
func compositeEnvSwitchEnabled(name string) bool {
	raw, ok := os.LookupEnv(name)
	if !ok {
		return true
	}
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "0", "false", "off", "no", "disabled", "disable":
		return false
	default:
		return true
	}
}

// CompositeCapacitySelectionEnabled 报告"同级按容量随机选择与满载换号"是否开启（默认开启）。
func CompositeCapacitySelectionEnabled() bool {
	return compositeEnvSwitchEnabled(compositeCapacitySelectionEnv)
}

// CompositeCapacityAlternate 是换号计划里的一个备选平台及其候选账号 ID（升序）。
type CompositeCapacityAlternate struct {
	Platform   string
	AccountIDs []int64
}

// CompositeCapacityPlan 是候选池随路由决策下发的换号计划：按优先顺序列出首选平台之外
// "快照里还有空位"的备选平台。计划只在请求上下文里传递，不进入 JSON。
type CompositeCapacityPlan struct {
	// Alternates 按尝试顺序排列。
	Alternates []CompositeCapacityAlternate
	// Temporary 为 true 表示本次请求命中了普通会话粘性：换号只是临时借用，
	// 不能改写会话与账号的粘性绑定。
	Temporary bool
}

// compositeCapacityPlanCtxKey 是换号计划的上下文键。
type compositeCapacityPlanCtxKey struct{}

// withCompositeCapacityPlan 把换号计划写入上下文；计划为空（没有备选）时不写。
func withCompositeCapacityPlan(ctx context.Context, plan *CompositeCapacityPlan) context.Context {
	if ctx == nil || plan == nil || len(plan.Alternates) == 0 {
		return ctx
	}
	return context.WithValue(ctx, compositeCapacityPlanCtxKey{}, plan)
}

// CompositeCapacityPlanFromContext 读取换号计划；没有计划时第二个返回值为 false。
func CompositeCapacityPlanFromContext(ctx context.Context) (*CompositeCapacityPlan, bool) {
	if ctx == nil {
		return nil, false
	}
	plan, ok := ctx.Value(compositeCapacityPlanCtxKey{}).(*CompositeCapacityPlan)
	return plan, ok && plan != nil && len(plan.Alternates) > 0
}

// compositeAccountCapacity 是候选账号在选号瞬间的容量快照。
type compositeAccountCapacity struct {
	// Unlimited 为 true 表示账号并发不设上限（Concurrency <= 0），永远不会满载。
	Unlimited bool
	// Remaining 是有限并发账号的剩余槽位（已扣除占用与排队），不小于 0。
	Remaining int
	// UnlimitedWeight 是无上限账号参与随机时使用的权重（取账号的有效负载因子，至少 1）。
	UnlimitedWeight int
}

// weight 返回账号参与随机的权重，0 表示满载（不参与随机）。
func (c compositeAccountCapacity) weight() int64 {
	if c.Unlimited {
		if c.UnlimitedWeight > 0 {
			return int64(c.UnlimitedWeight)
		}
		return 1
	}
	if c.Remaining > 0 {
		return int64(c.Remaining)
	}
	return 0
}

// compositeCapacityOf 由账号上限与负载信息算出容量快照。取不到负载信息时按"空闲"处理，
// 与调度器自己的口径一致（loadInfo 缺失时当作 0 负载）。
func compositeCapacityOf(account *Account, load *AccountLoadInfo) compositeAccountCapacity {
	if account.Concurrency <= 0 {
		return compositeAccountCapacity{Unlimited: true, UnlimitedWeight: account.EffectiveLoadFactor()}
	}
	used := 0
	if load != nil {
		used = load.CurrentConcurrency + load.WaitingCount
	}
	remaining := account.Concurrency - used
	if remaining < 0 {
		remaining = 0
	}
	return compositeAccountCapacity{Remaining: remaining}
}

// compositeCapacityConcurrencyService 返回候选池可用的并发服务（两套网关共用同一个服务实例，取任一）。
func (p *CompositeCandidatePool) compositeCapacityConcurrencyService() *ConcurrencyService {
	if p == nil {
		return nil
	}
	if p.openAI != nil && p.openAI.concurrencyService != nil {
		return p.openAI.concurrencyService
	}
	if p.gateway != nil && p.gateway.concurrencyService != nil {
		return p.gateway.concurrencyService
	}
	return nil
}

// loadCapacities 批量读取候选账号的容量快照。没有并发服务或批量读取失败时返回 false，
// 调用方据此退回原来的确定性排序，不能用"全部空闲"的假数据去随机。
func (p *CompositeCandidatePool) loadCapacities(ctx context.Context, candidates []*compositeCandidate) (map[int64]compositeAccountCapacity, bool) {
	svc := p.compositeCapacityConcurrencyService()
	if svc == nil || len(candidates) == 0 {
		return nil, false
	}
	requests := make([]AccountWithConcurrency, 0, len(candidates))
	for _, candidate := range candidates {
		requests = append(requests, AccountWithConcurrency{
			ID:             candidate.account.ID,
			MaxConcurrency: candidate.account.EffectiveLoadFactor(),
		})
	}
	loads, err := svc.GetAccountsLoadBatch(ctx, requests)
	if err != nil {
		return nil, false
	}
	capacities := make(map[int64]compositeAccountCapacity, len(candidates))
	for _, candidate := range candidates {
		capacities[candidate.account.ID] = compositeCapacityOf(candidate.account, loads[candidate.account.ID])
	}
	return capacities, true
}

// randomInt64N 返回 [0,n) 内的随机数，n 必须大于 0。可由测试替换为固定随机源。
func (p *CompositeCandidatePool) randomInt64N(n int64) int64 {
	if p != nil && p.capacityRand != nil {
		return p.capacityRand(n)
	}
	return rand.Int64N(n)
}

// compositeCapacityOrder 把已排好序的承接分组重排为"同级随机、满载下沉"的尝试顺序。
//
// group 必须已按 compositeCandidateLess 排序（同一 Priority 的账号相邻）。规则：
//  1. 按 Priority 分级，级别顺序不变；
//  2. 级内有剩余容量的账号按权重（剩余容量）无放回随机抽取，权重相同则等概率；
//  3. 满载账号不参与随机，统一排在所有"有空位账号"之后，保持原顺序；
//  4. 因此：同级还有空位先用同级，同级全满才轮到下一优先级，全部满载时顺序与原排序完全一致。
//
// randN 返回 [0,n) 内的随机整数，传入固定随机源即可得到确定性结果。
func compositeCapacityOrder(group []*compositeCandidate, capacities map[int64]compositeAccountCapacity, randN func(n int64) int64) []*compositeCandidate {
	ordered := make([]*compositeCandidate, 0, len(group))
	var full []*compositeCandidate
	for start := 0; start < len(group); {
		end := start + 1
		for end < len(group) && group[end].account.Priority == group[start].account.Priority {
			end++
		}
		var open []*compositeCandidate
		for _, candidate := range group[start:end] {
			if capacities[candidate.account.ID].weight() > 0 {
				open = append(open, candidate)
			} else {
				full = append(full, candidate)
			}
		}
		ordered = append(ordered, compositeWeightedDrawAll(open, capacities, randN)...)
		start = end
	}
	return append(ordered, full...)
}

// compositeWeightedDrawAll 按权重无放回地把 open 全部抽完，返回抽取顺序。
// 每一轮在剩余账号里按 [0,总权重) 区间落点选一个：权重相同的账号占据等长区间，概率相等。
func compositeWeightedDrawAll(open []*compositeCandidate, capacities map[int64]compositeAccountCapacity, randN func(n int64) int64) []*compositeCandidate {
	if len(open) <= 1 {
		return open
	}
	remaining := append([]*compositeCandidate(nil), open...)
	drawn := make([]*compositeCandidate, 0, len(open))
	for len(remaining) > 0 {
		if len(remaining) == 1 {
			// 只剩一个账号，无需再抽取随机数。
			drawn = append(drawn, remaining[0])
			break
		}
		var total int64
		for _, candidate := range remaining {
			total += capacities[candidate.account.ID].weight()
		}
		point := randN(total)
		pick := len(remaining) - 1
		var cumulative int64
		for i, candidate := range remaining {
			cumulative += capacities[candidate.account.ID].weight()
			if point < cumulative {
				pick = i
				break
			}
		}
		drawn = append(drawn, remaining[pick])
		remaining = append(remaining[:pick], remaining[pick+1:]...)
	}
	return drawn
}

// compositeCapacityPick 是一次容量感知选号的结果。
type compositeCapacityPick struct {
	// Platform 是首选账号所在平台，Members 是该平台在承接分组内的全部账号 ID（升序）。
	Platform string
	Members  []int64
	// Alternates 是首选平台之外、快照里还有空位的备选平台，按尝试顺序排列。
	Alternates []CompositeCapacityAlternate
}

// pickByCapacity 在承接分组 pool 内按剩余容量选出首选平台与换号计划。
// 开关关闭、没有并发服务、读取负载失败时返回 false，调用方沿用原来的确定性选择。
// 候选池全程只读：这里只读取负载快照，不抢槽。
func (r *compositeCandidateRun) pickByCapacity(pool []*compositeCandidate) (compositeCapacityPick, bool) {
	var pick compositeCapacityPick
	if !CompositeCapacitySelectionEnabled() {
		return pick, false
	}
	group := compositeReceiverGroup(pool)
	if len(group) == 0 {
		return pick, false
	}
	capacities, ok := r.pool.loadCapacities(r.ctx, group)
	if !ok {
		return pick, false
	}
	ordered := compositeCapacityOrder(group, capacities, r.pool.randomInt64N)

	primary := ordered[0].account.Platform
	pick.Platform = primary
	pick.Members = compositePlatformMembers(group, primary)
	pick.Alternates = compositeCapacityAlternates(group, ordered, capacities, primary)
	return pick, true
}

// compositePlatformMembers 返回承接分组内属于某个平台的账号 ID（升序）。
func compositePlatformMembers(group []*compositeCandidate, platform string) []int64 {
	members := make([]int64, 0, len(group))
	for _, candidate := range group {
		if candidate.account.Platform == platform {
			members = append(members, candidate.account.ID)
		}
	}
	sort.Slice(members, func(i, j int) bool { return members[i] < members[j] })
	return members
}

// compositeCapacityAlternates 由尝试顺序生成换号计划：跳过首选平台，只保留快照里至少有一个
// 空位账号的平台（全是满载账号的平台没有换过去的意义），平台按首次出现的顺序排列。
func compositeCapacityAlternates(group, ordered []*compositeCandidate, capacities map[int64]compositeAccountCapacity, primary string) []CompositeCapacityAlternate {
	var alternates []CompositeCapacityAlternate
	seen := map[string]bool{primary: true}
	for _, candidate := range ordered {
		platform := candidate.account.Platform
		if seen[platform] || capacities[candidate.account.ID].weight() <= 0 {
			continue
		}
		seen[platform] = true
		alternates = append(alternates, CompositeCapacityAlternate{
			Platform:   platform,
			AccountIDs: compositePlatformMembers(group, platform),
		})
	}
	return alternates
}

// capacityPlanForSticky 为命中普通会话粘性的请求生成"临时换号"计划：首选平台固定为绑定账号
// 所在平台（粘性不变），只是绑定账号所在平台整体满载时，允许临时借用其它平台的空位。
// eligible 是通过资格过滤的声明账号。开关关闭或读不到负载时返回 nil。
func (r *compositeCandidateRun) capacityPlanForSticky(eligible []*compositeCandidate, boundPlatform string) *CompositeCapacityPlan {
	if !CompositeCapacitySelectionEnabled() {
		return nil
	}
	group := compositeReceiverGroup(eligible)
	if len(group) == 0 {
		return nil
	}
	capacities, ok := r.pool.loadCapacities(r.ctx, group)
	if !ok {
		return nil
	}
	ordered := compositeCapacityOrder(group, capacities, r.pool.randomInt64N)
	alternates := compositeCapacityAlternates(group, ordered, capacities, boundPlatform)
	if len(alternates) == 0 {
		return nil
	}
	return &CompositeCapacityPlan{Alternates: alternates, Temporary: true}
}

// CompositeCapacitySwitchPlatformAllowed 报告备选平台是否属于 OpenAI 网关族：
// 处理器里的换号逻辑运行在 OpenAI 网关处理器内，无法把请求挪到 Anthropic 网关族平台。
func CompositeCapacitySwitchPlatformAllowed(platform string) bool {
	return domain.UsesOpenAIGateway(platform)
}
