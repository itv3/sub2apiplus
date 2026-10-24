package service

// 候选池的资格过滤、配额预检与零转换判定。
//
// 资格过滤的原则：对每个候选账号按它自己的平台，直接调用两套调度器已有的判断函数，
// 不复制函数体，也不另造一套健康状态。清单必须完整，否则会提前选定一个最终不可用的平台。
// 候选池不做调度栈内才有的代理流隔离、compact 与生图能力筛选，这些留给平台内调度器终检。
//
//	判断项                 OpenAI 网关族                                   Anthropic 网关族
//	停用、限流、模型级限流 openAICompatibleAccountEligibilityFailureReason  isAccountSchedulableForSelection、
//	（含利润门、自动暂停、                                                   isAccountSchedulableForModelSelection、
//	  模型支持、端点能力）                                                   isModelSupportedByAccountWithContext
//	利润门                 同上（先装入分组利润门）                         isGatewayAccountProfitEligible
//	账号额度、窗口费用、RPM 不适用                                          isAccountSchedulableForQuota、
//	                                                                        isAccountSchedulableForWindowCost、
//	                                                                        isAccountSchedulableForRPM
//	渠道限制               needsUpstreamChannelRestrictionCheck 决定是否查  同左（GatewayService 同名函数）
//	                       isUpstreamModelRestrictedByChannel
//	进程内运行时封禁       isOpenAIAccountRequestRuntimeBlocked             不适用
//	影子账号母账号健康     parentHealthyForShadow 配 parentAccountLookup    不适用
//	用户 × 平台配额        checkUserPlatformQuotaEligibility（只读，按平台去重）
//
// 渠道限制、利润门等判断读取的是"目标平台作用域"的配置（Composite 分组按已解析的目标平台
// 取渠道配置），所以每个候选账号都在写入了它自己平台的上下文里评估。

import (
	"context"
	"errors"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/domain"
)

// compositeVerdictKey 是资格评估结果的记忆键。sticky 为 true 表示按"粘性命中"语义评估
// （Anthropic 族里窗口费用与 RPM 的"仅限粘性"状态对它放行）。
type compositeVerdictKey struct {
	id     int64
	sticky bool
}

// compositeCandidateVerdict 是一次资格评估的结果；不合格时 reason 记录首个否决点，仅用于诊断。
type compositeCandidateVerdict struct {
	eligible bool
	reason   string
}

// evaluate 评估账号当前是否可被调度。同一账号同一语义只评估一次。
func (r *compositeCandidateRun) evaluate(account *Account, sticky bool) compositeCandidateVerdict {
	if domain.UsesOpenAIGateway(account.Platform) {
		// OpenAI 网关族没有"仅限粘性"的资格状态，两种语义等价，合并记忆。
		sticky = false
	}
	key := compositeVerdictKey{id: account.ID, sticky: sticky}
	if verdict, ok := r.verdicts[key]; ok {
		return verdict
	}
	if reason, failed := r.refreshFailed[account.ID]; failed {
		// 实时读取或复核失败的账号一律不合格，原因照记。
		verdict := compositeCandidateVerdict{eligible: false, reason: reason}
		r.verdicts[key] = verdict
		return verdict
	}
	// 每个候选在写入了自己平台的上下文里评估。
	ctx := WithResolvedTargetPlatform(r.ctx, account.Platform)
	var reason string
	if domain.UsesOpenAIGateway(account.Platform) {
		reason = r.openAIFamilyIneligibleReason(ctx, account)
	} else {
		reason = r.anthropicFamilyIneligibleReason(ctx, account, sticky)
	}
	if reason == "" {
		if err := r.userPlatformQuotaError(ctx, account.Platform); err != nil {
			reason = compositeUserPlatformQuotaReason(err)
		}
	}
	verdict := compositeCandidateVerdict{eligible: reason == "", reason: reason}
	r.verdicts[key] = verdict
	return verdict
}

// compositeUserPlatformQuotaReason 区分用户平台配额预检被拒的原因：配额真的用尽，
// 还是预检本身出错（Redis、DB 故障等）。两种情况都剔除该平台，只是诊断标签不同。
func compositeUserPlatformQuotaReason(err error) string {
	if errors.Is(err, ErrUserPlatformDailyQuotaExhausted) ||
		errors.Is(err, ErrUserPlatformWeeklyQuotaExhausted) ||
		errors.Is(err, ErrUserPlatformMonthlyQuotaExhausted) {
		return "user_platform_quota_exhausted"
	}
	return "user_platform_quota_check_error"
}

// openAIFamilyIneligibleReason 对 OpenAI 网关族账号调用调度器现有的资格判断，返回首个否决原因（通过为空串）。
func (r *compositeCandidateRun) openAIFamilyIneligibleReason(ctx context.Context, account *Account) string {
	svc := r.pool.openAI
	if svc == nil {
		return "openai_gateway_unavailable"
	}
	platform := NormalizeOpenAICompatiblePlatform(account.Platform)
	// 文本入口的处理器默认要求 chat_completions 端点能力；compact 与生图才会改要求 responses，
	// 那两类请求不经候选池的文本入口标记，这里按默认值过滤。
	if reason := openAICompatibleAccountEligibilityFailureReason(
		ctx, account, platform, r.model, false, OpenAIEndpointCapabilityChatCompletions,
	); reason != "" {
		return reason
	}
	if svc.isOpenAIAccountRequestRuntimeBlocked(account, r.model) {
		return "runtime_blocked"
	}
	if !parentHealthyForShadow(account, svc.parentAccountLookup(ctx)) {
		return "shadow_parent_unhealthy"
	}
	if svc.needsUpstreamChannelRestrictionCheck(ctx, &r.groupID) &&
		svc.isUpstreamModelRestrictedByChannel(ctx, r.groupID, account, r.model, false) {
		return "channel_restricted"
	}
	return ""
}

// anthropicFamilyIneligibleReason 对 Anthropic 网关族账号调用调度器现有的资格判断，返回首个否决原因（通过为空串）。
func (r *compositeCandidateRun) anthropicFamilyIneligibleReason(ctx context.Context, account *Account, sticky bool) string {
	gw := r.pool.gateway
	if gw == nil {
		return "anthropic_gateway_unavailable"
	}
	if !gw.isAccountSchedulableForSelection(account) {
		return "not_schedulable"
	}
	if !gw.isAccountSchedulableForModelSelection(ctx, account, r.model) {
		return "model_rate_limited"
	}
	if !gw.isGatewayAccountProfitEligible(ctx, account) {
		return "profit_veto"
	}
	if !gw.isModelSupportedByAccountWithContext(ctx, account, r.model) {
		return "model_not_supported"
	}
	if !gw.isAccountSchedulableForQuota(account) {
		return "account_quota_exceeded"
	}
	if !gw.isAccountSchedulableForWindowCost(ctx, account, sticky) {
		return "window_cost_limited"
	}
	if !gw.isAccountSchedulableForRPM(ctx, account, sticky) {
		return "rpm_limited"
	}
	if gw.needsUpstreamChannelRestrictionCheck(ctx, &r.groupID) &&
		gw.isUpstreamModelRestrictedByChannel(ctx, r.groupID, account, r.model) {
		return "channel_restricted"
	}
	return ""
}

// userPlatformQuotaError 返回用户在该平台的配额预检结果（nil 表示放行），按平台去重。
func (r *compositeCandidateRun) userPlatformQuotaError(ctx context.Context, platform string) error {
	if err, ok := r.quota[platform]; ok {
		return err
	}
	err := r.pool.checkUserPlatformQuota(ctx, r.entry, platform)
	r.quota[platform] = err
	return err
}

// checkUserPlatformQuota 只读地预检用户 × 平台配额，模式豁免与 CheckBillingEligibility 一致：
// 简易模式跳过，订阅模式（分组是订阅类型且用户持有订阅）豁免，只有余额模式才检查。
//
// 这里刻意不调用 CheckBillingEligibility：它会递增分组与用户的 RPM 计数，候选预检每个平台
// 调一次就会重复计数。正式准入与 RPM 计数仍由处理器按首选平台执行一次，位置不变。
func (p *CompositeCandidatePool) checkUserPlatformQuota(ctx context.Context, entry CompositeCandidatePoolEntry, platform string) error {
	billing := p.billing
	if billing == nil || entry.UserID <= 0 || platform == "" {
		return nil
	}
	if billing.cfg != nil && billing.cfg.RunMode == config.RunModeSimple {
		return nil
	}
	if entry.Group != nil && entry.Group.IsSubscriptionType() && entry.Subscription != nil {
		return nil
	}
	return billing.checkUserPlatformQuotaEligibility(ctx, entry.UserID, platform)
}

// isZeroConversion 判定账号对本次入站协议是否无需协议转换即可直通上游（结果按账号记忆）。
//
//   - OpenAI 网关族账号：resolveUpstreamProtocolFor 返回的上游协议等于入站协议即零转换，
//     按模型分流的账号带上游模型目录一并判定；
//   - Grok 在 Responses 与 Chat Completions 入口有专属直通链路（resolveUpstreamProtocol 的说明要求
//     调用前先分流），这两种入站对它视为零转换，Anthropic 入站需要转换；
//   - Anthropic 平台账号只对 Anthropic 入站（含 count_tokens）零转换；
//     其余 Anthropic 网关族平台（Gemini、Antigravity、TypeSafe）上游不是 Anthropic 原生协议，一律需转换。
func (r *compositeCandidateRun) isZeroConversion(account *Account) bool {
	if zero, ok := r.zeroConv[account.ID]; ok {
		return zero
	}
	var zero bool
	switch {
	case domain.UsesOpenAIGateway(account.Platform):
		if account.IsGrok() {
			zero = r.inbound == APIProtocolResponses || r.inbound == APIProtocolChatCompletions
			break
		}
		ctx := WithResolvedTargetPlatform(r.ctx, account.Platform)
		routingModel := upstreamRoutingModel(account, r.entry.Body, "")
		zero = r.pool.openAI.resolveUpstreamProtocolFor(ctx, account, r.inbound, routingModel) == r.inbound
	default:
		zero = account.Platform == PlatformAnthropic && r.inbound == APIProtocolAnthropic
	}
	r.zeroConv[account.ID] = zero
	return zero
}
