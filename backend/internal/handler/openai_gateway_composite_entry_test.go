//go:build unit

package handler

// 特征测试：钉住"满载换号接入层"（openai_gateway_composite_entry.go）与瘦身前内联代码算出的值完全一致。
//
// 瘦身前的内联代码（提交 1d8318668 引入）在换号成功后依次执行，期望值逐行推导如下
// （以下"入口"指 Responses、Messages、ChatCompletions，局部变量名取自入口）：
//
// Responses：
//   selection = sw.Selection；compositeTemporarySwap = sw.Temporary；requestPlatform = sw.Platform；
//   requiredCapability = openAIResponsesRequiredCapabilityForRequest(imageIntent, needsResponses, requestPlatform)；
//   channelMapping = Resolve(换号后上下文)；
//   forwardBody = openAIModelMappedBody(body, channelMapping.Mapped, channelMapping.MappedModel, ReplaceModelInBody)
//     （未映射时原样返回 body，不调用替换函数）；
//   forwardModel = openAIChannelForwardModel(channelMapping, reqModel)（映射且 MappedModel 非空白时取 MappedModel，否则 reqModel）；
//   c.Request 上下文 = WithOpenAIForwardModel(换号后上下文, forwardModel, legacyCompact)。
//   Eligible = 无账号失败 && 未换过号 && 无 previous_response_id && 非生图 && 非 legacy compact && 非原生 v2。
//   换号参数 Model = 换号前的 forwardModel；UseUpstreamTokenCost = !imageIntent；RequireCompact = requireCompact；
//   ModelFor 返回目标平台上下文下的 openAIChannelForwardModel；CapabilityFor(platform) = openAIResponsesRequiredCapabilityForRequest(imageIntent, needsResponses, platform)。
//
// Messages：
//   selection = sw.Selection；requestPlatform = sw.Platform；
//   effectiveMappedModel = messagesDispatchMappedModelForPlatform(apiKey, sw.Platform, reqModel)；
//   channelMappingMsg = Resolve(换号后上下文)。
//   Eligible = 无账号失败 && 未换过号；Model = currentRoutingModel；UseUpstreamTokenCost = true；能力恒为 ChatCompletions；
//   ModelFor(platform) = 调度级映射非空则取之，否则 routingModel。
//
// ChatCompletions：
//   selection = sw.Selection；requestPlatform = sw.Platform；channelMapping = Resolve(换号后上下文)；
//   forwardModel = openAIChannelForwardModel(channelMapping, reqModel)。
//   Eligible = 无账号失败 && 未换过号；Model = 换号前的 forwardModel；UseUpstreamTokenCost = true；能力恒为 ChatCompletions；
//   ModelFor 返回目标平台上下文下的 openAIChannelForwardModel。
//
// 三个入口共有：换号成功时 compositeTemporarySwap = sw.Temporary，抢槽前 compositeAcquireSessionHash(sessionHash, compositeTemporarySwap)
// 临时借用返回空串；每轮循环开头 compositeTemporarySwap 重置为 false。接入层把该标志放在 gin.Context 里，
// 由 acquireResponsesAccountSlot 读取（compositeSlotSessionHash），等价于原调用点。

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// newCompositeEntryTestContext 构造带请求的 gin 上下文。
func newCompositeEntryTestContext() *gin.Context {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	return c
}

// fakeCompositeSwitch 构造一个换号结果，并按 tryCompositeCapacitySwitch 的行为把请求上下文换成 sw.Ctx。
func fakeCompositeSwitch(c *gin.Context, platform string, temporary bool) *service.CompositeCapacitySwitch {
	sw := &service.CompositeCapacitySwitch{
		Selection: &service.AccountSelectionResult{
			Account:  &service.Account{ID: 42, Platform: platform},
			Acquired: true,
		},
		Ctx:       service.WithResolvedTargetPlatform(context.Background(), platform),
		Platform:  platform,
		Temporary: temporary,
	}
	c.Request = c.Request.WithContext(sw.Ctx)
	return sw
}

// recordingResolver 记录每次被调用时收到的上下文，并返回固定的渠道映射。
type recordingResolver struct {
	mapping service.ChannelMappingResult
	ctxs    []context.Context
}

func (r *recordingResolver) resolve(ctx context.Context) service.ChannelMappingResult {
	r.ctxs = append(r.ctxs, ctx)
	return r.mapping
}

// requireResolvedPlatform 断言解析器收到的上下文带着换号后的目标平台。
func requireResolvedPlatform(t *testing.T, ctx context.Context, want string) {
	t.Helper()
	got, ok := service.ResolvedTargetPlatformFromContext(ctx)
	require.True(t, ok)
	require.Equal(t, want, got)
}

func TestCompositeSlotSessionHash(t *testing.T) {
	c := newCompositeEntryTestContext()
	// 从未经过换号入口的调用方（生图、搜索、grok 等）：原样返回。
	require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"))
	require.Equal(t, "abc", compositeSlotSessionHash(nil, "abc"))

	setCompositeTemporarySwap(c, true)
	require.Equal(t, "", compositeSlotSessionHash(c, "abc"), "临时借用不带会话哈希，避免把粘性绑定改写到被借用的账号")
	// 下一轮选号循环开头重置后恢复原样。
	setCompositeTemporarySwap(c, false)
	require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"))

	// 键里不是布尔值时按未借用处理，不 panic。
	c.Set(compositeTemporarySwapKey, "yes")
	require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"))
	// 与原来的纯函数口径一致。
	require.Equal(t, compositeAcquireSessionHash("abc", false), compositeSlotSessionHash(c, "abc"))
}

// acquireResponsesAccountSlot 必须经 compositeSlotSessionHash 取会话哈希：这是"临时借用不改写粘性绑定"的唯一落点，
// 合并上游时若这一行被冲突覆盖，行为测试看不出来，所以用语法树守住。
func TestAcquireResponsesAccountSlotUsesCompositeSlotSessionHash(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "openai_gateway_handler.go", nil, 0)
	require.NoError(t, err)
	found := false
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "acquireResponsesAccountSlot" {
			continue
		}
		ast.Inspect(fn.Body, func(n ast.Node) bool {
			if call, ok := n.(*ast.CallExpr); ok {
				if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "compositeSlotSessionHash" {
					found = true
				}
			}
			return true
		})
	}
	require.True(t, found, "acquireResponsesAccountSlot 必须用 compositeSlotSessionHash 取会话哈希")
}

// 三个入口在"不换号"时：选号结果原样返回、临时借用标志被重置为 false、被改写的局部变量与请求上下文都不动。
func TestSwitchCompositeEntriesNoSwitchKeepsState(t *testing.T) {
	h := &OpenAIGatewayHandler{} // 没有网关服务：tryCompositeCapacitySwitch 直接放行
	apiKey := &service.APIKey{Group: &service.Group{Platform: service.PlatformComposite}}
	waiting := &service.AccountSelectionResult{
		Account:  &service.Account{ID: 1, Platform: service.PlatformOpenAI},
		WaitPlan: &service.AccountWaitPlan{AccountID: 1, MaxConcurrency: 1},
	}
	body := []byte(`{"model":"m"}`)

	t.Run("Responses", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		setCompositeTemporarySwap(c, true) // 模拟上一轮遗留的标志
		ctxBefore := c.Request.Context()
		platform, capability := service.PlatformOpenAI, service.OpenAIEndpointCapabilityResponses
		mapping := service.ChannelMappingResult{MappedModel: "keep"}
		forwardBody, forwardModel := body, "fm"
		got := h.switchCompositeResponses(c, apiKey, nil, nil, waiting, &compositeResponsesSwitch{
			Fresh: true, ReqModel: "m", Body: body, Platform: &platform, Capability: &capability,
			Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
		})
		require.Same(t, waiting, got)
		require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"), "每轮必须先把临时借用标志重置为 false")
		require.Equal(t, service.PlatformOpenAI, platform)
		require.Equal(t, service.OpenAIEndpointCapabilityResponses, capability)
		require.Equal(t, service.ChannelMappingResult{MappedModel: "keep"}, mapping)
		require.Equal(t, "fm", forwardModel)
		require.Equal(t, body, forwardBody)
		require.Equal(t, ctxBefore, c.Request.Context())
	})

	t.Run("Messages", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		setCompositeTemporarySwap(c, true)
		platform, mapped := service.PlatformOpenAI, "dispatch"
		mapping := service.ChannelMappingResult{MappedModel: "keep"}
		got := h.switchCompositeMessages(c, apiKey, nil, nil, waiting, &compositeMessagesSwitch{
			Fresh: true, ReqModel: "m", RoutingModel: "r", CurrentRoutingModel: "cr",
			Platform: &platform, EffectiveMappedModel: &mapped, Mapping: &mapping,
		})
		require.Same(t, waiting, got)
		require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"))
		require.Equal(t, service.PlatformOpenAI, platform)
		require.Equal(t, "dispatch", mapped)
		require.Equal(t, service.ChannelMappingResult{MappedModel: "keep"}, mapping)
	})

	t.Run("ChatCompletions", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		setCompositeTemporarySwap(c, true)
		platform, forwardModel := service.PlatformOpenAI, "fm"
		mapping := service.ChannelMappingResult{MappedModel: "keep"}
		got := h.switchCompositeChat(c, apiKey, nil, nil, waiting, &compositeChatSwitch{
			Fresh: true, ReqModel: "m", Platform: &platform, Mapping: &mapping, ForwardModel: &forwardModel,
		})
		require.Same(t, waiting, got)
		require.Equal(t, "abc", compositeSlotSessionHash(c, "abc"))
		require.Equal(t, service.PlatformOpenAI, platform)
		require.Equal(t, "fm", forwardModel)
		require.Equal(t, service.ChannelMappingResult{MappedModel: "keep"}, mapping)
	})
}

func TestCompositeResponsesSwitchParams(t *testing.T) {
	forwardModel := "fm"
	base := func() compositeResponsesSwitch {
		return compositeResponsesSwitch{
			Fresh: true, SessionHash: "sess", ReqModel: "gpt-5.5", RequireCompact: true, ForwardModel: &forwardModel,
		}
	}
	resolver := &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "mapped-x"}}

	// Eligible：任一条件不满足都不换号（有账号失败或换过号、续链、生图、legacy compact、原生 v2）。
	for name, mutate := range map[string]func(*compositeResponsesSwitch){
		"not-fresh":        func(s *compositeResponsesSwitch) { s.Fresh = false },
		"previous-id":      func(s *compositeResponsesSwitch) { s.PreviousResponseID = "resp_1" },
		"image-intent":     func(s *compositeResponsesSwitch) { s.ImageIntent = true },
		"legacy-compact":   func(s *compositeResponsesSwitch) { s.LegacyCompact = true },
		"native-v2":        func(s *compositeResponsesSwitch) { s.NativeV2 = true },
		"image-and-legacy": func(s *compositeResponsesSwitch) { s.ImageIntent, s.LegacyCompact = true, true },
	} {
		st := base()
		mutate(&st)
		require.False(t, st.params(resolver.resolve).Eligible, name)
	}
	// needsResponses／requireCompact 本身不影响 Eligible（原条件里没有它们）。
	st := base()
	st.NeedsResponses = true
	p := st.params(resolver.resolve)
	require.True(t, p.Eligible)
	require.Equal(t, "sess", p.SessionHash)
	require.Equal(t, "fm", p.Model, "Model 取换号前的 forwardModel")
	require.True(t, p.RequireCompact)
	require.True(t, p.UseUpstreamTokenCost)

	// 生图请求：UseUpstreamTokenCost = !imageIntent。
	st = base()
	st.ImageIntent = true
	require.False(t, st.params(resolver.resolve).UseUpstreamTokenCost)

	// ModelFor：用目标平台上下文解析映射；CapabilityFor 与 openAIResponsesRequiredCapabilityForRequest 一致。
	st = base()
	p = st.params(resolver.resolve)
	altCtx := service.WithResolvedTargetPlatform(context.Background(), service.PlatformKimi)
	require.Equal(t, "mapped-x", p.ModelFor(altCtx, service.PlatformKimi))
	require.Equal(t, altCtx, resolver.ctxs[len(resolver.ctxs)-1])
	resolver.mapping = service.ChannelMappingResult{} // 未映射：回到请求模型名
	require.Equal(t, "gpt-5.5", p.ModelFor(altCtx, service.PlatformKimi))
	// needsResponses 且目标平台为 openai → Responses；其它平台 → ChatCompletions。
	st.NeedsResponses = true
	p = st.params(resolver.resolve)
	require.Equal(t, service.OpenAIEndpointCapabilityResponses, p.CapabilityFor(service.PlatformOpenAI))
	require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, p.CapabilityFor(service.PlatformKimi))
	// 生图且目标平台为 openai → Responses；
	st.NeedsResponses, st.ImageIntent = false, true
	p = st.params(resolver.resolve)
	require.Equal(t, service.OpenAIEndpointCapabilityResponses, p.CapabilityFor(service.PlatformOpenAI))
	require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, p.CapabilityFor(service.PlatformGrok))
}

func TestCompositeResponsesSwitchApply(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","input":"hi"}`)
	// 假的替换函数：记录入参并返回可辨认的结果，证明"已映射才替换、未映射原样返回 body"。
	var replacedWith string
	replace := func(b []byte, m string) []byte {
		replacedWith = m
		return append([]byte("replaced:"), b...)
	}

	t.Run("已映射且临时借用", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		sw := fakeCompositeSwitch(c, service.PlatformKimi, true)
		resolver := &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "mapped-x"}}
		platform, capability := service.PlatformOpenAI, service.OpenAIEndpointCapabilityResponses
		var mapping service.ChannelMappingResult
		forwardBody, forwardModel := body, "old"
		st := &compositeResponsesSwitch{
			NeedsResponses: true, LegacyCompact: true, ReqModel: "gpt-5.5", Body: body,
			Platform: &platform, Capability: &capability, Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
		}

		got := st.apply(c, sw, resolver.resolve, replace)

		require.Same(t, sw.Selection, got)
		require.Equal(t, service.PlatformKimi, platform)
		// needsResponses 但目标平台是 kimi → ChatCompletions。
		require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, capability)
		require.Equal(t, resolver.mapping, mapping)
		require.Equal(t, "mapped-x", replacedWith)
		require.Equal(t, append([]byte("replaced:"), body...), forwardBody)
		require.Equal(t, "mapped-x", forwardModel)
		// 映射用换号后的上下文解析（带新目标平台）。
		require.Len(t, resolver.ctxs, 1)
		requireResolvedPlatform(t, resolver.ctxs[0], service.PlatformKimi)
		// 请求上下文：换号后的上下文 + 转发模型（legacyCompact 标志随之写入）。
		require.True(t, reflect.DeepEqual(c.Request.Context(), service.WithOpenAIForwardModel(sw.Ctx, "mapped-x", true)))
		require.False(t, reflect.DeepEqual(c.Request.Context(), service.WithOpenAIForwardModel(sw.Ctx, "mapped-x", false)))
		requireResolvedPlatform(t, c.Request.Context(), service.PlatformKimi)
		// 临时借用：抢槽不带会话哈希。
		require.Equal(t, "", compositeSlotSessionHash(c, "sess"))
	})

	t.Run("未映射且非临时借用", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		sw := fakeCompositeSwitch(c, service.PlatformOpenAI, false)
		replacedWith = ""
		resolver := &recordingResolver{mapping: service.ChannelMappingResult{MappedModel: "gpt-5.5"}}
		platform, capability := service.PlatformKimi, service.OpenAIEndpointCapabilityChatCompletions
		var mapping service.ChannelMappingResult
		forwardBody, forwardModel := []byte("x"), "old"
		st := &compositeResponsesSwitch{
			NeedsResponses: true, ReqModel: "gpt-5.5", Body: body,
			Platform: &platform, Capability: &capability, Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
		}

		st.apply(c, sw, resolver.resolve, replace)

		require.Equal(t, service.PlatformOpenAI, platform)
		// needsResponses 且目标平台是 openai → Responses。
		require.Equal(t, service.OpenAIEndpointCapabilityResponses, capability)
		require.Equal(t, "", replacedWith, "未映射时不调用替换函数")
		require.Equal(t, body, forwardBody, "未映射时转发体就是原始 body")
		require.Equal(t, "gpt-5.5", forwardModel)
		require.True(t, reflect.DeepEqual(c.Request.Context(), service.WithOpenAIForwardModel(sw.Ctx, "gpt-5.5", false)))
		require.Equal(t, "sess", compositeSlotSessionHash(c, "sess"), "非临时借用沿用会话哈希")
	})

	t.Run("映射目标为空白时回到请求模型名", func(t *testing.T) {
		c := newCompositeEntryTestContext()
		sw := fakeCompositeSwitch(c, service.PlatformOpenAI, false)
		resolver := &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "  "}}
		platform, capability := service.PlatformKimi, service.OpenAIEndpointCapabilityChatCompletions
		var mapping service.ChannelMappingResult
		forwardBody, forwardModel := body, "old"
		st := &compositeResponsesSwitch{
			ImageIntent: true, ReqModel: "gpt-5.5", Body: body,
			Platform: &platform, Capability: &capability, Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
		}
		st.apply(c, sw, resolver.resolve, replace)
		// 生图且目标平台是 openai → Responses；forwardModel 回到请求模型名；
		// 但 Mapped 为 true，转发体仍用 MappedModel 替换（与原内联代码一致，原代码只看 Mapped 与 replace 是否为空）。
		require.Equal(t, service.OpenAIEndpointCapabilityResponses, capability)
		require.Equal(t, "gpt-5.5", forwardModel)
		require.Equal(t, "  ", replacedWith)
	})
}

func TestCompositeMessagesSwitchParamsAndApply(t *testing.T) {
	group := &service.Group{Platform: service.PlatformComposite, MessagesDispatchModelConfig: service.OpenAIMessagesDispatchModelConfig{OpusMappedModel: "gpt-5.5"}}
	apiKey := &service.APIKey{Group: group}
	st := &compositeMessagesSwitch{
		Fresh: true, SessionHash: "sess", ReqModel: "claude-opus-4", RoutingModel: "routing", CurrentRoutingModel: "current",
	}

	p := st.params(apiKey)
	require.True(t, p.Eligible)
	require.Equal(t, "sess", p.SessionHash)
	require.Equal(t, "current", p.Model, "Model 取本轮的 currentRoutingModel")
	require.True(t, p.UseUpstreamTokenCost)
	require.False(t, p.RequireCompact)
	require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, p.CapabilityFor(service.PlatformOpenAI))
	require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, p.CapabilityFor(service.PlatformGrok))
	// ModelFor：grok／多协议平台不适用调度映射 → 回退 routingModel；openai 取分组映射。
	require.Equal(t, "routing", p.ModelFor(context.Background(), service.PlatformGrok))
	require.Equal(t, "routing", p.ModelFor(context.Background(), service.PlatformDeepseek))
	require.Equal(t, "gpt-5.5", p.ModelFor(context.Background(), service.PlatformOpenAI))
	st.Fresh = false
	require.False(t, st.params(apiKey).Eligible)

	// apply：换到 deepseek（多协议）→ 调度映射为空串；换到 openai → 分组映射。
	for _, tc := range []struct {
		platform  string
		temporary bool
		mapped    string
	}{
		{service.PlatformDeepseek, true, ""},
		{service.PlatformOpenAI, false, "gpt-5.5"},
	} {
		c := newCompositeEntryTestContext()
		sw := fakeCompositeSwitch(c, tc.platform, tc.temporary)
		resolver := &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "chan-" + tc.platform}}
		platform, mapped := service.PlatformKimi, "stale"
		var mapping service.ChannelMappingResult
		st := &compositeMessagesSwitch{ReqModel: "claude-opus-4", Platform: &platform, EffectiveMappedModel: &mapped, Mapping: &mapping}

		got := st.apply(c, apiKey, sw, resolver.resolve)

		require.Same(t, sw.Selection, got)
		require.Equal(t, tc.platform, platform)
		require.Equal(t, tc.mapped, mapped)
		require.Equal(t, resolver.mapping, mapping)
		require.Len(t, resolver.ctxs, 1)
		requireResolvedPlatform(t, resolver.ctxs[0], tc.platform)
		// 请求上下文不被改写（Messages 入口原先就不写转发模型）。
		require.Equal(t, sw.Ctx, c.Request.Context())
		if tc.temporary {
			require.Equal(t, "", compositeSlotSessionHash(c, "sess"))
		} else {
			require.Equal(t, "sess", compositeSlotSessionHash(c, "sess"))
		}
	}
}

func TestCompositeChatSwitchParamsAndApply(t *testing.T) {
	forwardModel := "fm"
	resolver := &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "mapped-x"}}
	st := &compositeChatSwitch{Fresh: true, SessionHash: "sess", ReqModel: "gpt-5.5", ForwardModel: &forwardModel}

	p := st.params(resolver.resolve)
	require.True(t, p.Eligible)
	require.Equal(t, "sess", p.SessionHash)
	require.Equal(t, "fm", p.Model)
	require.True(t, p.UseUpstreamTokenCost)
	require.False(t, p.RequireCompact)
	require.Equal(t, service.OpenAIEndpointCapabilityChatCompletions, p.CapabilityFor(service.PlatformOpenAI))
	altCtx := service.WithResolvedTargetPlatform(context.Background(), service.PlatformKimi)
	require.Equal(t, "mapped-x", p.ModelFor(altCtx, service.PlatformKimi))
	require.Equal(t, altCtx, resolver.ctxs[0])
	st.Fresh = false
	require.False(t, st.params(resolver.resolve).Eligible)

	// apply：换到 kimi 临时借用。
	c := newCompositeEntryTestContext()
	sw := fakeCompositeSwitch(c, service.PlatformKimi, true)
	resolver = &recordingResolver{mapping: service.ChannelMappingResult{Mapped: true, MappedModel: "mapped-y"}}
	platform := service.PlatformOpenAI
	var mapping service.ChannelMappingResult
	forwardModel = "old"
	st = &compositeChatSwitch{ReqModel: "gpt-5.5", Platform: &platform, Mapping: &mapping, ForwardModel: &forwardModel}

	got := st.apply(c, sw, resolver.resolve)

	require.Same(t, sw.Selection, got)
	require.Equal(t, service.PlatformKimi, platform)
	require.Equal(t, resolver.mapping, mapping)
	require.Equal(t, "mapped-y", forwardModel)
	require.Len(t, resolver.ctxs, 1)
	requireResolvedPlatform(t, resolver.ctxs[0], service.PlatformKimi)
	require.Equal(t, sw.Ctx, c.Request.Context(), "Chat 入口原先不写转发模型到上下文")
	require.Equal(t, "", compositeSlotSessionHash(c, "sess"))
}

// 调度级模型映射的两份实现（上游原样的 resolveOpenAIMessagesDispatchMappedModel 与满载换号用的
// messagesDispatchMappedModelForPlatform）必须在所有平台上口径一致。
func TestMessagesDispatchMappedModelParity(t *testing.T) {
	mappingCfg := service.OpenAIMessagesDispatchModelConfig{OpusMappedModel: " gpt-5.5 ", SonnetMappedModel: "gpt-5.4", HaikuMappedModel: "gpt-5.4-mini"}
	groups := map[string]*service.Group{
		"composite": {Platform: service.PlatformComposite, MessagesDispatchModelConfig: mappingCfg},
		"openai":    {Platform: service.PlatformOpenAI, MessagesDispatchModelConfig: mappingCfg},
	}
	platforms := []string{service.PlatformOpenAI, service.PlatformGrok, service.PlatformDeepseek, service.PlatformKimi, service.PlatformAnthropic}
	models := []string{"claude-opus-4", "claude-sonnet-4", "claude-haiku-4", "gpt-5.5", ""}
	for gname, group := range groups {
		apiKey := &service.APIKey{Group: group}
		for _, platform := range platforms {
			c := newCompositeEntryTestContext()
			c.Request = c.Request.WithContext(service.WithResolvedTargetPlatform(c.Request.Context(), platform))
			for _, model := range models {
				require.Equal(t,
					resolveOpenAIMessagesDispatchMappedModel(c, apiKey, model),
					messagesDispatchMappedModelForPlatform(apiKey, platform, model),
					"group=%s platform=%s model=%q", gname, platform, model)
			}
		}
	}
}
