package service

// 账号级跨平台候选池（Composite 分组）。
//
// 解决的问题：同一个公开模型名被多个平台下的账号声明时，旧流程只看"声明它的平台有几个"，
// 多于一个就判歧义、退回按前缀猜平台，账号优先级根本比不上。候选池改成"账号级"：
// 扫分组内所有声明了该模型的账号，按"能用客户端协议零转换直通的账号优先、再比账号优先级"
// 选出首选账号，把首选账号所在平台写成目标平台，并把"可供下游调度器选号的账号 ID 集合"
// 一并写回；下游调度器仍是单平台的，只在集合内按原策略选号。
//
// 本文件只放入口标记、上下文读写、集合谓词、候选池主流程与选择规则；
// 资格过滤与配额预检见 composite_candidate_pool_eligibility.go，
// 粘性与续链的只读查询见 composite_candidate_pool_sticky.go。
//
// 约束（与方案的最小入侵约束一致）：
//   - 资格、配额、健康、粘性判断全部通过薄适配直接调用两套网关现有的函数，不复制函数体；
//   - 候选池全程只读：不递增 RPM、不写粘性、不写续链绑定、不改任何持久状态；
//   - 上游模型名保持公开模型名，模型映射仍由账号层在转发时处理。

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/tidwall/gjson"
)

// compositeCandidatePoolEntryCtxKey 是入口标记的上下文键（包内私有类型，不占用 ctxkey 包）。
type compositeCandidatePoolEntryCtxKey struct{}

// compositeCandidateAccountSetCtxKey 是候选账号集合的上下文键。
type compositeCandidateAccountSetCtxKey struct{}

// compositeCandidateAccountSet 是候选账号 ID 集合，写入上下文后只读。
type compositeCandidateAccountSet map[int64]struct{}

// CompositeCandidatePoolEntry 是候选池的入口标记：路由中间件在解析目标平台之前把它写进
// 请求上下文，候选池据此知道"本次请求是 Composite 分组的 POST 文本入口"，并拿到计算
// 两套会话哈希、续链归属和配额预检所需的全部请求信息。没有这个标记的请求（模型列表、
// 管理端预览路由、Gemini 原生、生图、embeddings、WebSocket 等）一律不进池。
//
// 所有字段由调用方在写入后视为只读。
type CompositeCandidatePoolEntry struct {
	// Endpoint 是入站端点，只接受 CompositeRouteEndpointMessages、CompositeRouteEndpointCountTokens、
	// CompositeRouteEndpointResponses、CompositeRouteEndpointChatCompletions 四种。
	// 它必须与 Resolve 收到的 endpoint 一致，不一致时候选池放弃处理。
	Endpoint string
	// Body 是原始请求体，用于计算会话哈希、判定按模型分流账号的上游协议、解析 previous_response_id。
	Body []byte
	// Header 是原始请求头，用于读取会话相关头（session_id、conversation_id、
	// X-Claude-Code-Session-Id 等）。
	Header http.Header
	// ClientIP、UserAgent 与 APIKeyID 对应 Anthropic 网关族会话哈希第三级兜底（消息内容摘要）
	// 混入的 SessionContext。UserAgent 为空时回落到 Header 的 User-Agent。
	ClientIP  string
	UserAgent string
	APIKeyID  int64
	// UserID 是发起请求的用户，用于用户 × 平台配额预检。
	UserID int64
	// Group 与 Subscription 对应 CheckBillingEligibility 的计费模式判定：
	// 分组是订阅类型且用户持有订阅时为订阅模式，豁免用户 × 平台配额。
	Group        *Group
	Subscription *UserSubscription
	// PreviousResponseID 是 Responses 入站请求的 previous_response_id；留空时候选池从 Body 解析。
	PreviousResponseID string
}

// WithCompositeCandidatePoolEntry 把入口标记写入上下文。
func WithCompositeCandidatePoolEntry(ctx context.Context, entry CompositeCandidatePoolEntry) context.Context {
	if ctx == nil {
		return ctx
	}
	return context.WithValue(ctx, compositeCandidatePoolEntryCtxKey{}, entry)
}

// CompositeCandidatePoolEntryFromContext 读取入口标记；未写入时第二个返回值为 false。
func CompositeCandidatePoolEntryFromContext(ctx context.Context) (CompositeCandidatePoolEntry, bool) {
	if ctx == nil {
		return CompositeCandidatePoolEntry{}, false
	}
	entry, ok := ctx.Value(compositeCandidatePoolEntryCtxKey{}).(CompositeCandidatePoolEntry)
	return entry, ok
}

// withCompositeCandidateAccountIDs 把候选账号 ID 集合写入上下文；空集合不写。
func withCompositeCandidateAccountIDs(ctx context.Context, ids []int64) context.Context {
	if ctx == nil || len(ids) == 0 {
		return ctx
	}
	set := make(compositeCandidateAccountSet, len(ids))
	for _, id := range ids {
		set[id] = struct{}{}
	}
	return context.WithValue(ctx, compositeCandidateAccountSetCtxKey{}, set)
}

// CompositeCandidateAccountIDsFromContext 返回上下文里的候选账号 ID 集合（升序副本）；
// 没有集合时第二个返回值为 false。
func CompositeCandidateAccountIDsFromContext(ctx context.Context) ([]int64, bool) {
	set, ok := compositeCandidateAccountSetFromContext(ctx)
	if !ok {
		return nil, false
	}
	ids := make([]int64, 0, len(set))
	for id := range set {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids, true
}

func compositeCandidateAccountSetFromContext(ctx context.Context) (compositeCandidateAccountSet, bool) {
	if ctx == nil {
		return nil, false
	}
	set, ok := ctx.Value(compositeCandidateAccountSetCtxKey{}).(compositeCandidateAccountSet)
	return set, ok && len(set) > 0
}

// CompositeAccountClaimAllowed 是三处调度声明闸门共用的集合谓词：
// 上下文带候选集合时，账号 ID 在集合内即放行（声明强度已由候选池判过一次，调度器不再重判，
// 通配声明的账号因此也能被选中）；上下文没有集合时沿用 explicitModelMappingClaims 的精确判定，
// 行为与引入候选池之前完全一致。
func CompositeAccountClaimAllowed(ctx context.Context, account *Account, publicModel string) bool {
	if account == nil {
		return false
	}
	if set, ok := compositeCandidateAccountSetFromContext(ctx); ok {
		_, allowed := set[account.ID]
		return allowed
	}
	return explicitModelMappingClaims(*account, publicModel)
}

// CompositeCandidateAccountLister 列出分组内按持久化配置启用的账号（不过滤限流等瞬态状态）。
// AccountRepository 天然满足该接口；单独抽成接口是为了让候选池可以在测试里用桩替换，
// 也便于接线阶段套一层缓存。
type CompositeCandidateAccountLister interface {
	ListModelAvailabilityCandidates(ctx context.Context, groupID *int64, platforms []string, includeGrouped bool) ([]Account, error)
}

// CompositeCandidatePoolResolver 是 CompositeRouteResolver 持有的候选池接口。
// 返回值依次为：路由决策、是否已处理、错误。
//   - 已处理（true）：决策里带目标平台、公开模型名、来源 account_model 与候选账号 ID 集合；
//   - 未处理（false）：本次请求没有进池条件或零声明，调用方应交回原有归属判定流程；
//   - 错误：账号目录读取失败。是否退回原流程由调用方按"可识别模型名仍可由前缀识别"的既有口径决定。
type CompositeCandidatePoolResolver interface {
	ResolveCandidatePool(ctx context.Context, groupID int64, model, endpoint string) (CompositeRouteDecision, bool, error)
}

// CompositeCandidatePool 是账号级跨平台候选池的实现。
type CompositeCandidatePool struct {
	accounts CompositeCandidateAccountLister
	openAI   *OpenAIGatewayService
	gateway  *GatewayService
	billing  *BillingCacheService
}

var _ CompositeCandidatePoolResolver = (*CompositeCandidatePool)(nil)

// NewCompositeCandidatePool 创建候选池。四个依赖都只被"只读地"使用：
// accounts 扫账号，openAI 与 gateway 分别提供 OpenAI 网关族与 Anthropic 网关族的资格、
// 会话哈希、粘性与续链查询，billing 提供用户 × 平台配额预检。
// openAI 或 gateway 为空时，对应网关族的候选账号一律视为不合格。
func NewCompositeCandidatePool(accounts CompositeCandidateAccountLister, openAI *OpenAIGatewayService, gateway *GatewayService, billing *BillingCacheService) *CompositeCandidatePool {
	return &CompositeCandidatePool{accounts: accounts, openAI: openAI, gateway: gateway, billing: billing}
}

// AttachCompositeCandidatePool 用网关服务已持有的账号仓储与 Composite 路由解析器就地创建候选池，
// 并接入解析器。接线放在装配处理器的 Provide 函数里调用，不改 wire 依赖图。
// 网关服务没有账号仓储或解析器时不做任何事（候选池不启用，行为与原来一致）。
func (s *GatewayService) AttachCompositeCandidatePool(openAI *OpenAIGatewayService, billing *BillingCacheService) {
	if s == nil || s.compositeResolver == nil || s.accountRepo == nil {
		return
	}
	s.compositeResolver.SetCandidatePool(NewCompositeCandidatePool(s.accountRepo, openAI, s, billing))
}

// 候选池选定候选集合所走的路径，供日志与单测断言使用。
const (
	// compositeCandidatePathNone 表示未处理，交回原流程。
	compositeCandidatePathNone = ""
	// compositeCandidatePathContinuation 表示 Responses 续链命中：集合只放归属账号。
	compositeCandidatePathContinuation = "continuation"
	// compositeCandidatePathSticky 表示普通粘性命中：集合为绑定账号加同平台其它合格候选。
	compositeCandidatePathSticky = "sticky"
	// compositeCandidatePathRanked 表示无绑定，按零转换优先加优先级排序选出首选。
	compositeCandidatePathRanked = "ranked"
	// compositeCandidatePathNoEligible 表示声明账号都没通过资格过滤：仍按排序写回，
	// 让下游调度器与准入检查产生它们本来就有的错误，而不是在这里新造错误。
	compositeCandidatePathNoEligible = "no_eligible"
)

// compositeCandidatePoolOutcome 是一次解析的完整结果（含诊断信息），ResolveCandidatePool 只对外暴露其中的决策。
type compositeCandidatePoolOutcome struct {
	Path     string
	Decision CompositeRouteDecision
	// Tier 是实际采用的声明强度层级。
	Tier CompositeClaimStrength
	// Rejected 记录被资格过滤剔除的账号 ID 及原因，仅用于诊断。
	Rejected map[int64]string
}

// ResolveCandidatePool 实现 CompositeCandidatePoolResolver。
func (p *CompositeCandidatePool) ResolveCandidatePool(ctx context.Context, groupID int64, model, endpoint string) (CompositeRouteDecision, bool, error) {
	outcome, err := p.resolve(ctx, groupID, model, endpoint)
	if err != nil {
		return CompositeRouteDecision{}, false, err
	}
	if outcome.Path == compositeCandidatePathNone {
		return CompositeRouteDecision{}, false, nil
	}
	return outcome.Decision, true, nil
}

func (p *CompositeCandidatePool) resolve(ctx context.Context, groupID int64, model, endpoint string) (compositeCandidatePoolOutcome, error) {
	var unhandled compositeCandidatePoolOutcome
	if p == nil || p.accounts == nil || groupID <= 0 {
		return unhandled, nil
	}
	entry, ok := CompositeCandidatePoolEntryFromContext(ctx)
	if !ok {
		return unhandled, nil
	}
	model = strings.TrimSpace(model)
	endpoint = normalizeCompositeRouteEndpoint(endpoint)
	inbound, ok := compositeInboundProtocol(endpoint)
	if !ok || model == "" || normalizeCompositeRouteEndpoint(entry.Endpoint) != endpoint {
		return unhandled, nil
	}

	accounts, err := p.accounts.ListModelAvailabilityCandidates(ctx, &groupID, domain.ConcretePlatformIDs(), false)
	if err != nil {
		return unhandled, fmt.Errorf("list composite candidate accounts: %w", err)
	}
	tier, claimed := compositeClaimTierAccounts(accounts, model)
	if len(claimed) == 0 {
		// 零声明：交回原流程，由归属判定与前缀猜测处理。
		return unhandled, nil
	}

	run := p.newRun(ctx, entry, groupID, model, endpoint, inbound)
	outcome := compositeCandidatePoolOutcome{Tier: tier, Rejected: map[int64]string{}}

	// 先评估所有声明账号的资格，被剔除的原因留作诊断。
	eligible := make([]*compositeCandidate, 0, len(claimed))
	all := make([]*compositeCandidate, 0, len(claimed))
	for _, account := range claimed {
		candidate := &compositeCandidate{account: account}
		all = append(all, candidate)
		if verdict := run.evaluate(account, false); !verdict.eligible {
			outcome.Rejected[account.ID] = verdict.reason
			continue
		}
		candidate.zeroConversion = run.isZeroConversion(account)
		eligible = append(eligible, candidate)
	}

	// 1. 续链优先：续链状态依附于首轮账号，换账号就续不上，所以集合只放归属账号。
	if account := run.continuationOwner(accounts); account != nil {
		outcome.Path = compositeCandidatePathContinuation
		outcome.Decision = run.decision(account.Platform, []int64{account.ID})
		run.logOutcome(outcome)
		return outcome, nil
	}

	// 2. 普通粘性：绑定账号仍合格就用它，目标平台跟绑定账号走；
	// 集合为绑定账号加同平台其它合格候选，不按零转换过滤。
	// 绑定账号按粘性语义单独评估资格，所以从全部声明账号里找，而不只看 eligible。
	if bound := run.stickyCandidate(all); bound != nil {
		members := []int64{bound.account.ID}
		for _, candidate := range eligible {
			if candidate.account.Platform == bound.account.Platform && candidate.account.ID != bound.account.ID {
				members = append(members, candidate.account.ID)
			}
		}
		outcome.Path = compositeCandidatePathSticky
		outcome.Decision = run.decision(bound.account.Platform, members)
		run.logOutcome(outcome)
		return outcome, nil
	}

	// 3. 无绑定：零转换组优先，组内按 Priority、账号 ID 排序取首选。
	pool := eligible
	outcome.Path = compositeCandidatePathRanked
	if len(pool) == 0 {
		// 声明账号全部没过资格过滤。不退回原流程（原流程会按前缀乱猜平台），
		// 也不在这里新造错误：按声明账号本身排序后写回，由下游调度器的终检与
		// 准入检查返回它们原本的错误（无可用账号、用户平台配额用尽等）。
		pool = all
		for _, candidate := range pool {
			candidate.zeroConversion = run.isZeroConversion(candidate.account)
		}
		outcome.Path = compositeCandidatePathNoEligible
	}
	_, members := compositePickPrimary(pool)
	platform := run.platformOf(pool, members)
	outcome.Decision = run.decision(platform, members)
	run.logOutcome(outcome)
	return outcome, nil
}

// compositeCandidate 是进入排序的候选账号。
type compositeCandidate struct {
	account *Account
	// zeroConversion 为 true 表示该账号对本次入站协议无需协议转换即可直通上游。
	zeroConversion bool
}

// compositeCandidateLess 是候选账号的排序：零转换在前，其次 Priority 升序（数值小的优先），
// 再次账号 ID 升序。
func compositeCandidateLess(a, b *compositeCandidate) bool {
	if a.zeroConversion != b.zeroConversion {
		return a.zeroConversion
	}
	if a.account.Priority != b.account.Priority {
		return a.account.Priority < b.account.Priority
	}
	return a.account.ID < b.account.ID
}

// compositePickPrimary 选出首选账号及候选集合成员的账号 ID。
//
// 零转换组非空时只在零转换组内选，否则在需转换组内选（组间不混用，不做请求内换组）。
// 集合成员取"所选组里与首选账号同平台"的账号：下游两套调度器都是单平台的，
// 跨平台账号放进集合只会被平台门挡掉，没有意义。返回的成员 ID 升序。
func compositePickPrimary(candidates []*compositeCandidate) (*compositeCandidate, []int64) {
	var zero, convert []*compositeCandidate
	for _, candidate := range candidates {
		if candidate.zeroConversion {
			zero = append(zero, candidate)
		} else {
			convert = append(convert, candidate)
		}
	}
	group := zero
	if len(group) == 0 {
		group = convert
	}
	if len(group) == 0 {
		return nil, nil
	}
	sorted := append([]*compositeCandidate(nil), group...)
	sort.SliceStable(sorted, func(i, j int) bool { return compositeCandidateLess(sorted[i], sorted[j]) })
	primary := sorted[0]
	members := make([]int64, 0, len(sorted))
	for _, candidate := range sorted {
		if candidate.account.Platform == primary.account.Platform {
			members = append(members, candidate.account.ID)
		}
	}
	sort.Slice(members, func(i, j int) bool { return members[i] < members[j] })
	return primary, members
}

// compositeInboundProtocol 把入站端点换算成上游协议常量。count_tokens 按 Anthropic 处理。
func compositeInboundProtocol(endpoint string) (string, bool) {
	switch endpoint {
	case CompositeRouteEndpointMessages, CompositeRouteEndpointCountTokens:
		return APIProtocolAnthropic, true
	case CompositeRouteEndpointResponses:
		return APIProtocolResponses, true
	case CompositeRouteEndpointChatCompletions:
		return APIProtocolChatCompletions, true
	default:
		return "", false
	}
}

// compositeCandidateRun 保存一次解析的请求级状态与记忆表。一次请求只在单个 goroutine 内使用。
type compositeCandidateRun struct {
	pool     *CompositeCandidatePool
	ctx      context.Context
	entry    CompositeCandidatePoolEntry
	groupID  int64
	model    string
	endpoint string
	inbound  string

	verdicts  map[compositeVerdictKey]compositeCandidateVerdict
	quota     map[string]error
	zeroConv  map[int64]bool
	bound     map[string]int64
	parsedReq *ParsedRequest
	parsedErr bool
}

func (p *CompositeCandidatePool) newRun(ctx context.Context, entry CompositeCandidatePoolEntry, groupID int64, model, endpoint, inbound string) *compositeCandidateRun {
	return &compositeCandidateRun{
		pool:     p,
		ctx:      p.eligibilityBaseContext(ctx, groupID, endpoint),
		entry:    entry,
		groupID:  groupID,
		model:    model,
		endpoint: endpoint,
		inbound:  inbound,
		verdicts: make(map[compositeVerdictKey]compositeCandidateVerdict),
		quota:    make(map[string]error),
		zeroConv: make(map[int64]bool),
		bound:    make(map[string]int64),
	}
}

// decision 构造写回的路由决策：来源固定为 account_model，上游模型名保持公开模型名。
func (r *compositeCandidateRun) decision(platform string, ids []int64) CompositeRouteDecision {
	sorted := append([]int64(nil), ids...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	return CompositeRouteDecision{
		Matched:             true,
		Source:              CompositeRouteSourceAccount,
		GroupID:             r.groupID,
		PublicModel:         r.model,
		TargetPlatform:      platform,
		UpstreamModel:       r.model,
		Endpoint:            r.endpoint,
		CandidateAccountIDs: sorted,
	}
}

// platformOf 返回所选成员所在的平台（成员同平台，取第一个即可）。
func (r *compositeCandidateRun) platformOf(pool []*compositeCandidate, members []int64) string {
	if len(members) == 0 {
		return ""
	}
	for _, candidate := range pool {
		if candidate.account.ID == members[0] {
			return candidate.account.Platform
		}
	}
	return ""
}

func (r *compositeCandidateRun) logOutcome(outcome compositeCandidatePoolOutcome) {
	level := slog.LevelDebug
	if outcome.Path == compositeCandidatePathNoEligible {
		level = slog.LevelInfo
	}
	slog.Log(r.ctx, level, "composite_candidate_pool_resolved",
		"group_id", r.groupID,
		"model", r.model,
		"endpoint", r.endpoint,
		"path", outcome.Path,
		"claim_tier", int(outcome.Tier),
		"target_platform", outcome.Decision.TargetPlatform,
		"candidate_account_ids", outcome.Decision.CandidateAccountIDs,
		"rejected", outcome.Rejected,
	)
}

// previousResponseID 返回 Responses 入站请求的 previous_response_id（入口标记优先，缺省时从请求体解析）。
func (r *compositeCandidateRun) previousResponseID() string {
	if r.inbound != APIProtocolResponses {
		return ""
	}
	if id := strings.TrimSpace(r.entry.PreviousResponseID); id != "" {
		return id
	}
	return strings.TrimSpace(gjson.GetBytes(r.entry.Body, "previous_response_id").String())
}

// eligibilityBaseContext 构造资格过滤所用的基础上下文：
//   - 清掉 account_model 来源标记：下游调度器里的"账号是否声明了该模型"闸门读取该标记，
//     候选池自己已按声明强度收集过账号，资格过滤只关心调度状态，不能被精确键判定误伤通配账号；
//   - 装入 OpenAI 自动暂停设置与分组利润门，与调度器入口一致；count_tokens 不计费，按处理器的
//     做法豁免利润门。
func (p *CompositeCandidatePool) eligibilityBaseContext(ctx context.Context, groupID int64, endpoint string) context.Context {
	ctx = context.WithValue(ctx, ctxkey.CompositeRouteSource, "")
	if p.openAI == nil {
		return ctx
	}
	ctx = p.openAI.withOpenAIQuotaAutoPauseContext(ctx)
	if endpoint == CompositeRouteEndpointCountTokens {
		ctx = WithOpenAIProfitControlSuppressed(ctx)
	}
	return p.openAI.withOpenAIProfitControlGate(ctx, &groupID)
}
