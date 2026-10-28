package handler

// Composite 满载换号在三个入口（Responses、Messages、ChatCompletions）里的接入层。
//
// 背景：入口的选号循环里需要"先试换号，成功后重算一批局部变量"，这些代码原先内联在
// openai_gateway_handler.go 与 openai_chat_completions.go 里。这两个文件是上游高频改动文件，
// 内联代码会让每次合并上游都要处理冲突，所以全部收进本文件，入口里每处只留一次调用。
//
// 约定（三个入口一致）：
//   - 入口把"被换号改写的局部变量"以指针传入，把"只读输入"以值传入，选号结果以返回值带回；
//   - 每轮选号循环都必须调用一次本文件的入口方法（即使不满足换号条件），因为它会重置"临时借用"标记，
//     等价于原先每轮循环开头的 compositeTemporarySwap := false；
//   - "临时借用"标记放在 gin.Context 里（见 compositeTemporarySwapKey），
//     由 acquireResponsesAccountSlot 在抢槽、准入后绑定前读取，命中时不带会话哈希，
//     保证粘性会话不会被绑到被借用的账号上（与原先 compositeAcquireSessionHash 的调用点等价）；
//   - 换号成功时 tryCompositeCapacitySwitch 已把请求上下文整体换成 sw.Ctx，
//     所以 apply 里读取的 c.Request.Context() 就是换号后的新上下文。
//
// 每个入口拆成三步，便于单测不依赖真实调度器：
//  1. params：由入口信息构造换号参数（含 Eligible 判定与两个闭包）；
//  2. tryCompositeCapacitySwitch：（在 openai_gateway_composite_capacity.go，未改动）真正调用 service 层换号；
//  3. apply：换号成功后改写局部变量、重算映射与转发体。

import (
	"context"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

// compositeTemporarySwapKey 是 gin.Context 里"本轮选号为粘性会话临时借用"标记的键。
const compositeTemporarySwapKey = "openai.composite_temporary_swap"

// setCompositeTemporarySwap 写入本轮的临时借用标记。每轮选号循环都会调用一次（先置 false，换号成功再按结果置位）。
func setCompositeTemporarySwap(c *gin.Context, temporary bool) {
	if c != nil {
		c.Set(compositeTemporarySwapKey, temporary)
	}
}

// compositeSlotSessionHash 返回 acquireResponsesAccountSlot 抢槽与准入后绑定应使用的会话哈希：
// 本轮是临时借用时返回空串，不改写粘性绑定；其它情况（含从未经过换号入口的调用方）原样返回。
func compositeSlotSessionHash(c *gin.Context, sessionHash string) string {
	if c == nil {
		return sessionHash
	}
	if v, ok := c.Get(compositeTemporarySwapKey); ok {
		if temporary, _ := v.(bool); temporary {
			return compositeAcquireSessionHash(sessionHash, true)
		}
	}
	return sessionHash
}

// compositeMappingResolver 按给定上下文解析渠道映射（映射按上下文里的目标平台取配置）。
type compositeMappingResolver func(ctx context.Context) service.ChannelMappingResult

// compositeMappingResolverFor 返回走真实网关服务的解析器，与原内联代码的
// h.gatewayService.ResolveChannelMappingAndRestrict(ctx, apiKey.GroupID, reqModel) 一致。
func (h *OpenAIGatewayHandler) compositeMappingResolverFor(apiKey *service.APIKey, reqModel string) compositeMappingResolver {
	return func(ctx context.Context) service.ChannelMappingResult {
		mapping, _ := h.gatewayService.ResolveChannelMappingAndRestrict(ctx, apiKey.GroupID, reqModel)
		return mapping
	}
}

// messagesDispatchMappedModelForPlatform 返回 Anthropic Messages 入口在某个目标平台下的调度级模型映射。
// composite 分组解析到 grok 或多协议 API Key 供应商时，分组的 Messages 调度映射（gpt-5.x 默认值）
// 是 openai 专属，发给这些上游必错，模型改写交给账号级 model_mapping，返回空串；
// 其它平台沿用分组配置。口径与 resolveOpenAIMessagesDispatchMappedModel 在 composite 分组下按
// 上下文目标平台取值时完全一致（该函数保持上游原样，不改走本函数；一致性由单测逐平台对照守住）。
func messagesDispatchMappedModelForPlatform(apiKey *service.APIKey, platform, requestedModel string) string {
	if apiKey == nil || apiKey.Group == nil {
		return ""
	}
	if apiKey.Group.Platform == service.PlatformComposite &&
		(platform == service.PlatformGrok || service.IsMultiProtocolAPIKeyProvider(platform)) {
		return ""
	}
	return strings.TrimSpace(apiKey.Group.ResolveMessagesDispatchModel(requestedModel))
}

// ---------------------------------------------------------------------------
// Responses 入口
// ---------------------------------------------------------------------------

// compositeResponsesSwitch 是 Responses 入口的换号信息：只读输入按值，被改写的局部变量按指针。
type compositeResponsesSwitch struct {
	// Fresh 为 true 表示尚无账号失败且未换过号（len(failedAccountIDs)==0 && switchCount==0）。
	Fresh              bool
	PreviousResponseID string
	ImageIntent        bool
	LegacyCompact      bool
	NativeV2           bool
	NeedsResponses     bool
	RequireCompact     bool
	SessionHash        string
	ReqModel           string
	Body               []byte

	// 以下是换号成功后会被改写的入口局部变量（ForwardModel 同时也是换号前的调度模型名）。
	Platform     *string
	Capability   *service.OpenAIEndpointCapability
	Mapping      *service.ChannelMappingResult
	ForwardBody  *[]byte
	ForwardModel *string
}

// params 构造换号参数：Eligible 为续链、compact、原生 v2、生图一律不换号。
func (st *compositeResponsesSwitch) params(resolve compositeMappingResolver) compositeCapacitySwitchParams {
	return compositeCapacitySwitchParams{
		Eligible: st.Fresh && st.PreviousResponseID == "" &&
			!st.ImageIntent && !st.LegacyCompact && !st.NativeV2,
		SessionHash: st.SessionHash,
		Model:       *st.ForwardModel,
		ModelFor: func(ctx context.Context, _ string) string {
			return openAIChannelForwardModel(resolve(ctx), st.ReqModel)
		},
		CapabilityFor: func(platform string) service.OpenAIEndpointCapability {
			return openAIResponsesRequiredCapabilityForRequest(st.ImageIntent, st.NeedsResponses, platform)
		},
		RequireCompact:       st.RequireCompact,
		UseUpstreamTokenCost: !st.ImageIntent,
	}
}

// apply 在换号成功后重算渠道映射、转发体与端点能力（映射按目标平台取配置），
// 并把转发模型写回请求上下文。c.Request 此时已是换号后的新上下文。返回应继续使用的选号结果。
func (st *compositeResponsesSwitch) apply(c *gin.Context, sw *service.CompositeCapacitySwitch, resolve compositeMappingResolver, replace openAIModelBodyReplaceFunc) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, sw.Temporary)
	*st.Platform = sw.Platform
	*st.Capability = openAIResponsesRequiredCapabilityForRequest(st.ImageIntent, st.NeedsResponses, *st.Platform)
	*st.Mapping = resolve(c.Request.Context())
	*st.ForwardBody = openAIModelMappedBody(st.Body, st.Mapping.Mapped, st.Mapping.MappedModel, replace)
	*st.ForwardModel = openAIChannelForwardModel(*st.Mapping, st.ReqModel)
	c.Request = c.Request.WithContext(service.WithOpenAIForwardModel(c.Request.Context(), *st.ForwardModel, st.LegacyCompact))
	return sw.Selection
}

// switchCompositeResponses 是 Responses 入口每轮选号后的唯一接入点：返回应继续使用的选号结果
// （换号成功时是备选平台已抢到槽位的结果，否则原样返回入参）。
func (h *OpenAIGatewayHandler) switchCompositeResponses(c *gin.Context, apiKey *service.APIKey, subscription *service.UserSubscription, reqLog *zap.Logger, selection *service.AccountSelectionResult, st *compositeResponsesSwitch) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, false)
	resolve := h.compositeMappingResolverFor(apiKey, st.ReqModel)
	sw, switched := h.tryCompositeCapacitySwitch(c, apiKey, subscription, selection, reqLog, st.params(resolve))
	if !switched {
		return selection
	}
	return st.apply(c, sw, resolve, h.gatewayService.ReplaceModelInBody)
}

// ---------------------------------------------------------------------------
// Messages 入口
// ---------------------------------------------------------------------------

// compositeMessagesSwitch 是 Messages 入口的换号信息。
type compositeMessagesSwitch struct {
	Fresh        bool
	SessionHash  string
	ReqModel     string
	RoutingModel string // 未经调度级映射的路由模型名（备选平台不适用调度映射时回退到它）
	// CurrentRoutingModel 是换号前本轮用于调度的模型名。
	CurrentRoutingModel string

	// 以下是换号成功后会被改写的入口局部变量。
	Platform             *string
	EffectiveMappedModel *string
	Mapping              *service.ChannelMappingResult
}

// params 构造换号参数：调度级模型映射随目标平台变化，grok 与多协议平台不适用分组的 Messages 调度映射。
func (st *compositeMessagesSwitch) params(apiKey *service.APIKey) compositeCapacitySwitchParams {
	return compositeCapacitySwitchParams{
		Eligible:    st.Fresh,
		SessionHash: st.SessionHash,
		Model:       st.CurrentRoutingModel,
		ModelFor: func(_ context.Context, platform string) string {
			if mapped := messagesDispatchMappedModelForPlatform(apiKey, platform, st.ReqModel); mapped != "" {
				return mapped
			}
			return st.RoutingModel
		},
		CapabilityFor: func(string) service.OpenAIEndpointCapability {
			return service.OpenAIEndpointCapabilityChatCompletions
		},
		UseUpstreamTokenCost: true,
	}
}

// apply 在换号成功后按新平台重算调度级模型映射与渠道映射，返回应继续使用的选号结果。
func (st *compositeMessagesSwitch) apply(c *gin.Context, apiKey *service.APIKey, sw *service.CompositeCapacitySwitch, resolve compositeMappingResolver) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, sw.Temporary)
	*st.Platform = sw.Platform
	*st.EffectiveMappedModel = messagesDispatchMappedModelForPlatform(apiKey, sw.Platform, st.ReqModel)
	*st.Mapping = resolve(c.Request.Context())
	return sw.Selection
}

// switchCompositeMessages 是 Messages 入口每轮选号后的唯一接入点，返回值语义同 switchCompositeResponses。
func (h *OpenAIGatewayHandler) switchCompositeMessages(c *gin.Context, apiKey *service.APIKey, subscription *service.UserSubscription, reqLog *zap.Logger, selection *service.AccountSelectionResult, st *compositeMessagesSwitch) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, false)
	sw, switched := h.tryCompositeCapacitySwitch(c, apiKey, subscription, selection, reqLog, st.params(apiKey))
	if !switched {
		return selection
	}
	return st.apply(c, apiKey, sw, h.compositeMappingResolverFor(apiKey, st.ReqModel))
}

// ---------------------------------------------------------------------------
// ChatCompletions 入口
// ---------------------------------------------------------------------------

// compositeChatSwitch 是 ChatCompletions 入口的换号信息。
type compositeChatSwitch struct {
	Fresh       bool
	SessionHash string
	ReqModel    string

	// 以下是换号成功后会被改写的入口局部变量（ForwardModel 同时也是换号前的调度模型名）。
	Platform     *string
	Mapping      *service.ChannelMappingResult
	ForwardModel *string
}

// params 构造换号参数：Chat 端点能力固定为 ChatCompletions，模型名按目标平台的渠道映射重算。
func (st *compositeChatSwitch) params(resolve compositeMappingResolver) compositeCapacitySwitchParams {
	return compositeCapacitySwitchParams{
		Eligible:    st.Fresh,
		SessionHash: st.SessionHash,
		Model:       *st.ForwardModel,
		ModelFor: func(ctx context.Context, _ string) string {
			return openAIChannelForwardModel(resolve(ctx), st.ReqModel)
		},
		CapabilityFor: func(string) service.OpenAIEndpointCapability {
			return service.OpenAIEndpointCapabilityChatCompletions
		},
		UseUpstreamTokenCost: true,
	}
}

// apply 在换号成功后按新平台重算渠道映射与调度模型名，返回应继续使用的选号结果。
func (st *compositeChatSwitch) apply(c *gin.Context, sw *service.CompositeCapacitySwitch, resolve compositeMappingResolver) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, sw.Temporary)
	*st.Platform = sw.Platform
	*st.Mapping = resolve(c.Request.Context())
	*st.ForwardModel = openAIChannelForwardModel(*st.Mapping, st.ReqModel)
	return sw.Selection
}

// switchCompositeChat 是 ChatCompletions 入口每轮选号后的唯一接入点，返回值语义同 switchCompositeResponses。
func (h *OpenAIGatewayHandler) switchCompositeChat(c *gin.Context, apiKey *service.APIKey, subscription *service.UserSubscription, reqLog *zap.Logger, selection *service.AccountSelectionResult, st *compositeChatSwitch) *service.AccountSelectionResult {
	setCompositeTemporarySwap(c, false)
	resolve := h.compositeMappingResolverFor(apiKey, st.ReqModel)
	sw, switched := h.tryCompositeCapacitySwitch(c, apiKey, subscription, selection, reqLog, st.params(resolve))
	if !switched {
		return selection
	}
	return st.apply(c, sw, resolve)
}
