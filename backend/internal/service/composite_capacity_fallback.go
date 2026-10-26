package service

// Composite 满载换号：首选平台抢不到槽位时，在排队之前先试其它平台的同级、再下一优先级账号。
//
// 触发位置：OpenAI 网关处理器的账号选择循环。平台内调度器（selectAccountWithScheduler）已经
// 负责"同平台内先试同级其它账号、再试下一优先级"，只有它返回"需要排队"（没有抢到槽位、只给了
// WaitPlan）时，说明首选平台里所有候选都抢不到槽位——此时才轮到本文件：按候选池下发的换号计划，
// 依次切到备选平台再选一次，第一个"当场抢到槽位"的账号胜出；全部抢不到才回到首选平台的
// WaitPlan 排队（"全部满载才排队"）。
//
// 复用原子抢槽：备选平台的选号仍走 SelectAccountWithSchedulerForCapability，槽位由调度器内部的
// 原子抢槽（tryAcquireAccountSlot）获得，释放仍走选号结果里的 ReleaseFunc，不看负载快照做决定，
// 也不会突破账号并发上限。
//
// 切换平台时需要重新计算的东西都在这里或调用方完成：
//   - 目标平台与候选账号集合：写入新的请求上下文（WithResolvedTargetPlatform + 候选集合）；
//   - 用户 × 平台配额准入：用只读预检（CheckUserPlatformQuotaReadOnly）重新判断，
//     刻意不调用 CheckBillingEligibility，它会递增分组与用户的 RPM 计数，造成重复计数；
//   - 渠道模型映射：调用方用新上下文重新解析（渠道映射按目标平台取配置）；
//   - 计费：用量记录只在最终转发的账号上发生一次，未转发的备选选号不产生任何扣费。
//
// 不做的事：
//   - 不在上游失败之后跨平台重放（调用方只在尚未出现任何账号失败时调用）；
//   - 不对有状态续链换号（带 previous_response_id 的请求调用方直接跳过）；
//   - 不对已开始输出的流换号（选号发生在转发之前）。

import (
	"context"

	"github.com/Wei-Shaw/sub2api/internal/config"
)

// CheckUserPlatformQuotaReadOnly 只读地预检用户 × 平台配额，模式豁免与 CheckBillingEligibility 一致：
// 简易模式跳过，订阅模式（分组是订阅类型且用户持有订阅）豁免，只有余额模式才检查。
//
// 它不会递增任何计数（RPM 等），因此候选池预检与满载换号都可以对多个平台反复调用。
func (s *BillingCacheService) CheckUserPlatformQuotaReadOnly(ctx context.Context, userID int64, group *Group, subscription *UserSubscription, platform string) error {
	if s == nil || userID <= 0 || platform == "" {
		return nil
	}
	if s.cfg != nil && s.cfg.RunMode == config.RunModeSimple {
		return nil
	}
	if group != nil && group.IsSubscriptionType() && subscription != nil {
		return nil
	}
	return s.checkUserPlatformQuotaEligibility(ctx, userID, platform)
}

// CompositeCapacityFallbackInput 是满载换号所需的请求信息。
type CompositeCapacityFallbackInput struct {
	GroupID     *int64
	SessionHash string
	// ModelFor 返回目标平台下用于调度的模型名（渠道映射按平台取配置，换平台要重算）。
	// 为空时沿用 Model。
	ModelFor func(ctx context.Context, platform string) string
	Model    string
	// CapabilityFor 返回目标平台要求的端点能力。
	CapabilityFor func(platform string) OpenAIEndpointCapability
	// QuotaCheck 在切换到某个备选平台前做用户 × 平台配额的只读预检；返回错误则跳过该平台。可为空。
	QuotaCheck func(ctx context.Context, platform string) error

	RequiredTransport    OpenAIUpstreamTransport
	RequireCompact       bool
	UseUpstreamTokenCost bool
}

// CompositeCapacitySwitch 描述一次成功的换号结果。
type CompositeCapacitySwitch struct {
	// Selection 是备选平台上已抢到槽位的选号结果（Acquired 为 true，ReleaseFunc 非空时需由调用方释放）。
	Selection *AccountSelectionResult
	// Ctx 是写入了新目标平台与候选集合的请求上下文，调用方应整体替换请求上下文。
	Ctx context.Context
	// Platform 是新的目标平台。
	Platform string
	// Temporary 为 true 表示命中了会话粘性、只是临时借用：调用方后续抢槽、转发都不能改写粘性绑定。
	Temporary bool
}

// CompositeCapacityFallback 在平台内调度器返回"需要排队"时，按换号计划尝试其它平台。
// current 必须是尚未抢到槽位的选号结果（Acquired 为 false 且带 WaitPlan）；其它情形、开关关闭、
// 请求上下文里没有换号计划时直接返回 false，调用方继续原有的排队流程。
func (s *OpenAIGatewayService) CompositeCapacityFallback(ctx context.Context, current *AccountSelectionResult, in CompositeCapacityFallbackInput) (*CompositeCapacitySwitch, bool) {
	if s == nil || current == nil || current.Account == nil || current.Acquired || current.WaitPlan == nil {
		return nil, false
	}
	if !CompositeCapacitySelectionEnabled() {
		return nil, false
	}
	plan, ok := CompositeCapacityPlanFromContext(ctx)
	if !ok {
		return nil, false
	}
	// 只有候选池自动路由写入了换号计划；这里再确认一次来源，显式路由与前缀猜测永远不换号。
	if source, hasSource := CompositeRouteSourceFromContext(ctx); !hasSource || source != CompositeRouteSourceAccount {
		return nil, false
	}
	sessionHash := in.SessionHash
	if plan.Temporary {
		// 粘性会话的临时借用：不带会话哈希选号，避免调度器把粘性绑定改写到备选账号上。
		sessionHash = ""
	}

	for _, alternate := range plan.Alternates {
		if !CompositeCapacitySwitchPlatformAllowed(alternate.Platform) || len(alternate.AccountIDs) == 0 {
			continue
		}
		altCtx := WithResolvedTargetPlatform(ctx, alternate.Platform)
		altCtx = withCompositeCandidateAccountIDs(altCtx, alternate.AccountIDs)
		// 换号只发生一次：新上下文不再携带计划，后续循环不会再次触发跨平台切换。
		altCtx = context.WithValue(altCtx, compositeCapacityPlanCtxKey{}, (*CompositeCapacityPlan)(nil))

		if in.QuotaCheck != nil {
			if err := in.QuotaCheck(altCtx, alternate.Platform); err != nil {
				continue
			}
		}
		model := in.Model
		if in.ModelFor != nil {
			if mapped := in.ModelFor(altCtx, alternate.Platform); mapped != "" {
				model = mapped
			}
		}
		capability := OpenAIEndpointCapability("")
		if in.CapabilityFor != nil {
			capability = in.CapabilityFor(alternate.Platform)
		}

		selection, _, err := s.SelectAccountWithSchedulerForCapability(
			altCtx,
			in.GroupID,
			"", // 续链请求不会走到这里
			sessionHash,
			model,
			nil,
			in.RequiredTransport,
			capability,
			in.RequireCompact,
			false,
			in.UseUpstreamTokenCost,
			alternate.Platform,
		)
		if err != nil || selection == nil || selection.Account == nil {
			continue
		}
		if !selection.Acquired {
			// 备选平台同样要排队：没有占用槽位，直接试下一个平台。
			if selection.ReleaseFunc != nil {
				selection.ReleaseFunc()
			}
			continue
		}
		return &CompositeCapacitySwitch{
			Selection: selection,
			Ctx:       altCtx,
			Platform:  alternate.Platform,
			Temporary: plan.Temporary,
		}, true
	}
	return nil, false
}
