package handler

// Composite 满载换号在 OpenAI 网关处理器里的接点。
//
// 选号循环拿到调度器的选号结果后，如果结果只是"需要排队"（没有抢到槽位、只给了 WaitPlan），
// 在真正排队之前调用 tryCompositeCapacitySwitch：它按候选池随路由决策下发的换号计划，
// 依次试其它平台的空闲账号，第一个当场抢到槽位的账号胜出（逻辑在 service 包的
// CompositeCapacityFallback，槽位仍由调度器的原子抢槽获得）。全部抢不到才回到原账号排队，
// 即"全部满载才排队"。
//
// 调用方负责传入 Eligible：只有"尚无账号失败、不是续链、不是 compact／生图"的请求才允许换号，
// 这样不会在上游失败后跨平台重放，也不会让有状态续链离开归属账号。
// 换号成功后调用方需要：整体采用返回的选号结果与上下文、按新平台重算渠道映射与端点能力，
// 命中会话粘性的临时借用（Temporary）在抢槽时不能带会话哈希，以免改写粘性绑定。

import (
	"context"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

// compositeCapacitySwitchParams 是换号所需的请求信息。
type compositeCapacitySwitchParams struct {
	// Eligible 由调用方判定：尚无账号失败、不是续链、不是 compact 或生图请求。
	Eligible    bool
	SessionHash string
	// Model 是当前平台下用于调度的模型名；ModelFor 非空时按备选平台重新计算（渠道映射随平台变化）。
	Model                string
	ModelFor             func(ctx context.Context, platform string) string
	CapabilityFor        func(platform string) service.OpenAIEndpointCapability
	RequireCompact       bool
	UseUpstreamTokenCost bool
}

// tryCompositeCapacitySwitch 在选号结果需要排队时尝试换到其它平台。成功时已把新的目标平台与候选集合
// 写回请求上下文，返回的选号结果已抢到槽位。
func (h *OpenAIGatewayHandler) tryCompositeCapacitySwitch(
	c *gin.Context,
	apiKey *service.APIKey,
	subscription *service.UserSubscription,
	selection *service.AccountSelectionResult,
	reqLog *zap.Logger,
	params compositeCapacitySwitchParams,
) (*service.CompositeCapacitySwitch, bool) {
	if !params.Eligible || h == nil || h.gatewayService == nil || apiKey == nil ||
		selection == nil || selection.Acquired || selection.WaitPlan == nil {
		return nil, false
	}
	in := service.CompositeCapacityFallbackInput{
		GroupID:              apiKey.GroupID,
		SessionHash:          params.SessionHash,
		Model:                params.Model,
		ModelFor:             params.ModelFor,
		CapabilityFor:        params.CapabilityFor,
		RequireCompact:       params.RequireCompact,
		UseUpstreamTokenCost: params.UseUpstreamTokenCost,
		RequiredTransport:    service.OpenAIUpstreamTransportAny,
		// 切换平台后重新做用户 × 平台配额准入：只读预检，不递增 RPM 等计数。
		QuotaCheck: func(ctx context.Context, platform string) error {
			if h.billingCacheService == nil || apiKey.User == nil {
				return nil
			}
			return h.billingCacheService.CheckUserPlatformQuotaReadOnly(ctx, apiKey.User.ID, apiKey.Group, subscription, platform)
		},
	}
	sw, ok := h.gatewayService.CompositeCapacityFallback(c.Request.Context(), selection, in)
	if !ok {
		return nil, false
	}
	c.Request = c.Request.WithContext(sw.Ctx)
	if reqLog != nil {
		reqLog.Info("openai.composite_capacity_switched",
			zap.Int64("from_account_id", selection.Account.ID),
			zap.String("from_platform", selection.Account.Platform),
			zap.Int64("to_account_id", sw.Selection.Account.ID),
			zap.String("to_platform", sw.Platform),
			zap.Bool("temporary", sw.Temporary),
		)
	}
	return sw, true
}

// compositeAcquireSessionHash 返回抢槽与准入后绑定使用的会话哈希：临时借用时返回空串，不改写粘性绑定。
func compositeAcquireSessionHash(sessionHash string, temporarySwap bool) string {
	if temporarySwap {
		return ""
	}
	return sessionHash
}
