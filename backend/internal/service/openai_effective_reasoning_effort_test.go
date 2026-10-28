//go:build unit

package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 生效 effort（input 里最后一条 configuration_update，没有则取请求级 reasoning.effort）的读取与各读点的行为。
// 官方 Codex 较新版本默认开启 reasoning_effort_override：会话内改 effort 时请求级 reasoning.effort 在窗口内保持基线，
// 用户的实际选择在 input 里的 configuration_update 项中。

const (
	effUpdateMedium = `{"type":"configuration_update","reasoning":{"effort":"medium"}}`
	effUpdateHigh   = `{"type":"configuration_update","reasoning":{"effort":"high"}}`
	effUpdateXHigh  = `{"type":"configuration_update","reasoning":{"effort":"xhigh"}}`
	effUpdateLow    = `{"type":"configuration_update","reasoning":{"effort":"low"}}`
	effUserMessage  = `{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}`
)

func effBody(baseline string, items ...string) []byte {
	return []byte(fmt.Sprintf(`{"model":"gpt-6.1-sol","reasoning":{"effort":%q},"input":[%s]}`, baseline, strings.Join(items, ",")))
}

func TestLastConfigurationUpdateEffort(t *testing.T) {
	cases := []struct {
		name string
		body string
		want string
		ok   bool
	}{
		{"没有 input", `{"model":"m"}`, "", false},
		{"input 是字符串简写", `{"input":"configuration_update"}`, "", false},
		{"没有该项", `{"input":[` + effUserMessage + `]}`, "", false},
		{"一条", `{"input":[` + effUserMessage + `,` + effUpdateHigh + `]}`, "high", true},
		{"多条取最后一条", `{"input":[` + effUpdateLow + `,` + effUserMessage + `,` + effUpdateXHigh + `,` + effUpdateMedium + `]}`, "medium", true},
		{"去首尾空白", `{"input":[{"type":"configuration_update","reasoning":{"effort":" high "}}]}`, "high", true},
		{"type 带空白也认", `{"input":[{"type":" configuration_update ","reasoning":{"effort":"low"}}]}`, "low", true},
		{"最后一条 effort 不是字符串时取更早的", `{"input":[` + effUpdateHigh + `,{"type":"configuration_update","reasoning":{"effort":5}}]}`, "high", true},
		{"空 effort 被忽略", `{"input":[` + effUpdateHigh + `,{"type":"configuration_update","reasoning":{"effort":"  "}}]}`, "high", true},
		{"缺 reasoning", `{"input":[{"type":"configuration_update"}]}`, "", false},
		{"别的类型里出现同名字符串不算", `{"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"configuration_update"}]}]}`, "", false},
		{"嵌套在消息内容里的伪造项不算", `{"input":[{"type":"message","role":"user","content":[{"type":"configuration_update","reasoning":{"effort":"max"}}]}]}`, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := lastConfigurationUpdateEffort([]byte(tc.body))
			require.Equal(t, tc.ok, ok)
			require.Equal(t, tc.want, got)

			// 已解码 map 版本结论一致。
			var decoded map[string]any
			require.NoError(t, json.Unmarshal([]byte(tc.body), &decoded))
			gotMap, okMap := lastConfigurationUpdateEffortFromReqBody(decoded)
			require.Equal(t, tc.ok, okMap)
			require.Equal(t, tc.want, gotMap)
		})
	}
	_, ok := lastConfigurationUpdateEffortFromReqBody(nil)
	require.False(t, ok)
}

func TestEffectiveReasoningEffortPrefersLastConfigurationUpdate(t *testing.T) {
	// 请求级是钉死的基线 medium，用户中途改过两次，最后是 xhigh。
	body := effBody("medium", effUserMessage, effUpdateHigh, effUserMessage, effUpdateXHigh)
	require.Equal(t, "xhigh", effectiveReasoningEffortRaw(body))

	// 没有 update 时取请求级，再其次扁平字段。
	require.Equal(t, "medium", effectiveReasoningEffortRaw(effBody("medium", effUserMessage)))
	require.Equal(t, "low", effectiveReasoningEffortRaw([]byte(`{"reasoning_effort":"low","input":[`+effUserMessage+`]}`)))
	require.Equal(t, "", effectiveReasoningEffortRaw([]byte(`{"input":[`+effUserMessage+`]}`)))

	// 归一化后的用量值：升档后读到 xhigh；请求 effort 口径同样。
	extracted := extractOpenAIReasoningEffortFromBody(body, "gpt-6.1-sol")
	require.NotNil(t, extracted)
	require.Equal(t, "xhigh", *extracted)
	requested := CanonicalRequestedReasoningEffort(body, "gpt-6.1-sol")
	require.NotNil(t, requested)
	require.Equal(t, "xhigh", *requested)

	// 已解码 map 口径（HTTP 客户端 → WS 上游路径）。
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(body, &decoded))
	mapExtracted := extractOpenAIReasoningEffort(decoded, "gpt-6.1-sol")
	require.NotNil(t, mapExtracted)
	require.Equal(t, "xhigh", *mapExtracted)
	mapRequested := CanonicalRequestedReasoningEffortFromReqBody(decoded, "gpt-6.1-sol")
	require.NotNil(t, mapRequested)
	require.Equal(t, "xhigh", *mapRequested)

	// 没有 update：与改动之前一致。
	plain := extractOpenAIReasoningEffortFromBody(effBody("medium", effUserMessage), "gpt-6.1-sol")
	require.NotNil(t, plain)
	require.Equal(t, "medium", *plain)

	// Responses→Anthropic 等转换路径的用量读取。
	converted := ExtractResponsesReasoningEffortFromBody(body)
	require.NotNil(t, converted)
	require.Equal(t, "xhigh", *converted)
}

// 中途升档：请求级基线 medium，窗口中途升到 high，渠道 reasoning_effort_multipliers 按升档后的 effort 计费。
func TestMidWindowEffortUpgradeIsBilledAtUpgradedEffort(t *testing.T) {
	billing := NewBillingService(rawChatCompletionsTestConfig(), nil)
	group := &Group{ID: 1, Platform: PlatformOpenAI, ModelPricing: []ChannelModelPricing{{
		Models: []string{"gpt-6.1-sol"}, BillingMode: BillingModeToken,
		InputPrice: testPtrFloat64(1e-6), OutputPrice: testPtrFloat64(0),
		ReasoningEffortMultipliers: map[string]float64{"medium": 1.5, "high": 2, "xhigh": 3},
	}}}
	costFor := func(body []byte) float64 {
		effort := extractOpenAIReasoningEffortFromBody(body, "gpt-6.1-sol")
		cost, err := billing.CalculateTokenCostForRequest(TokenCostRequest{
			Ctx: context.Background(), Model: "gpt-6.1-sol", Group: group,
			Tokens:          UsageTokens{InputTokens: 1_000_000},
			ReasoningEffort: optionalStringValue(effort), RateMultiplier: 1,
			Resolver: NewModelPricingResolver(nil, billing),
		})
		require.NoError(t, err)
		return cost.ActualCost
	}
	require.InDelta(t, 1.5, costFor(effBody("medium", effUserMessage)), 1e-12, "没改档：按基线 medium 的 1.5 倍")
	require.InDelta(t, 2.0, costFor(effBody("medium", effUserMessage, effUpdateHigh)), 1e-12, "中途升到 high：按 2 倍")
	require.InDelta(t, 3.0, costFor(effBody("medium", effUserMessage, effUpdateHigh, effUserMessage, effUpdateXHigh)), 1e-12, "再升到 xhigh：按最后一条的 3 倍")
	require.InDelta(t, 1.5, costFor(effBody("medium", effUserMessage, effUpdateHigh, effUserMessage, effUpdateMedium)), 1e-12, "升后又降回基线：按 1.5 倍")
	require.InDelta(t, 1.0, costFor(effBody("medium", effUserMessage, effUpdateLow)), 1e-12, "降到 low（未配置倍率）：按 1 倍")
}

// 首包超时：高档位（high／xhigh／max）用单独的超时；中途升档要按升档后的 effort 取。
func TestMidWindowEffortUpgradeUsesHighEffortFirstOutputTimeout(t *testing.T) {
	svc := &OpenAIGatewayService{cfg: &config.Config{}}
	svc.cfg.Gateway.OpenAIFirstOutputTimeoutSeconds = 120
	svc.cfg.Gateway.OpenAIHighEffortFirstOutputTimeoutSeconds = 300
	timeoutFor := func(body []byte) time.Duration {
		effort := extractOpenAIReasoningEffortFromBody(body, "gpt-6.1-sol")
		return svc.openAIFirstOutputTimeout(optionalStringValue(effort))
	}
	require.Equal(t, 120*time.Second, timeoutFor(effBody("medium", effUserMessage)))
	require.Equal(t, 300*time.Second, timeoutFor(effBody("medium", effUserMessage, effUpdateHigh)), "基线 medium 中途升到 high：用高档位超时")
	require.Equal(t, 120*time.Second, timeoutFor(effBody("high", effUserMessage, effUpdateLow)), "基线 high 中途降到 low：用普通超时")
}

func TestOpenAIWindowEffortCarrier(t *testing.T) {
	frame := func(previous string, baseline string, items ...string) []byte {
		prefix := ""
		if previous != "" {
			prefix = fmt.Sprintf(`"previous_response_id":%q,`, previous)
		}
		return []byte(fmt.Sprintf(`{"type":"response.create","model":"gpt-6.1-sol",%s"reasoning":{"effort":%q},"input":[%s]}`, prefix, baseline, strings.Join(items, ",")))
	}
	resolve := func(c *openAIWindowEffortCarrier, body []byte) string {
		return effectiveReasoningEffortRawWithCarry(body, c.advance(body))
	}

	var c openAIWindowEffortCarrier
	// 首帧：基线 medium，没有 update。
	require.Equal(t, "medium", resolve(&c, frame("", "medium", effUserMessage)))
	// 增量帧（previous_response_id）：用户改到 high，update 在本帧。
	require.Equal(t, "high", resolve(&c, frame("resp_1", "medium", effUserMessage, effUpdateHigh)))
	// 之后的增量帧不再带 update：沿用 high，而不是回落到被钉死的基线 medium。
	require.Equal(t, "high", resolve(&c, frame("resp_2", "medium", effUserMessage)))
	require.Equal(t, "high", resolve(&c, frame("resp_3", "medium", effUserMessage)))
	// 又改到 xhigh、再降回 low。
	require.Equal(t, "xhigh", resolve(&c, frame("resp_4", "medium", effUserMessage, effUpdateXHigh)))
	require.Equal(t, "xhigh", resolve(&c, frame("resp_5", "medium", effUserMessage)))
	require.Equal(t, "low", resolve(&c, frame("resp_6", "medium", effUserMessage, effUpdateLow)))
	// 预热帧（generate=false）既不带 update 也不代表窗口延续：不清记录、不沿用记录。
	prewarm := []byte(`{"type":"response.create","model":"gpt-6.1-sol","generate":false,"reasoning":{"effort":"medium"},"input":[]}`)
	require.Equal(t, "medium", resolve(&c, prewarm), "预热帧取自己的请求级 effort")
	require.Equal(t, "low", resolve(&c, frame("resp_6b", "medium", effUserMessage)), "预热帧没有清掉窗口记录")
	// 整段重放（压缩或换模型后的新窗口，没有 previous_response_id、没有 update）：清掉记录，取新的基线。
	require.Equal(t, "high", resolve(&c, frame("", "high", effUserMessage)))
	require.Equal(t, "high", resolve(&c, frame("resp_7", "high", effUserMessage)), "清掉之后增量帧取请求级基线，不会带出旧的 low")
	// 整段重放帧里带着完整历史的 update：取其中最后一条。
	require.Equal(t, "xhigh", resolve(&c, frame("", "medium", effUserMessage, effUpdateHigh, effUserMessage, effUpdateXHigh)))
	require.Equal(t, "xhigh", resolve(&c, frame("resp_8", "medium", effUserMessage)))

	// nil carrier 与无状态读取一致。
	var nilCarrier *openAIWindowEffortCarrier
	require.Equal(t, "", nilCarrier.advance(frame("resp_9", "medium", effUserMessage)))

	// “请求 effort”口径：同样的携带规则，另外认 output_config.effort。
	var requested openAIWindowEffortCarrier
	body := []byte(`{"type":"response.create","output_config":{"effort":"max"},"input":[]}`)
	require.Equal(t, "max", requestedReasoningEffortRawWithCarry(body, requested.advance(body)))
}

// 官方出站 WS：turn metadata 的 reasoning_effort 记录用户当前选择，增量帧靠连接状态沿用之前帧里的 update。
func TestOfficialWSDerivedStateCarriesConfigurationUpdateEffort(t *testing.T) {
	state := &officialOpenAIWSDerivedState{}
	frame := func(previous string, items ...any) map[string]any {
		payload := map[string]any{"type": "response.create", "input": items}
		if previous != "" {
			payload["previous_response_id"] = previous
		}
		return payload
	}
	update := func(effort string) map[string]any {
		return map[string]any{"type": "configuration_update", "reasoning": map[string]any{"effort": effort}}
	}
	user := map[string]any{"type": "message", "role": "user"}

	_, ok := state.windowReasoningEffortForFrame(frame("", user))
	require.False(t, ok, "首帧没有 update：回落到请求级 effort")
	effort, ok := state.windowReasoningEffortForFrame(frame("resp_1", user, update("high")))
	require.True(t, ok)
	require.Equal(t, "high", effort)
	effort, ok = state.windowReasoningEffortForFrame(frame("resp_2", user))
	require.True(t, ok, "增量帧不带 update：沿用之前的 high")
	require.Equal(t, "high", effort)
	_, ok = state.windowReasoningEffortForFrame(frame("", user))
	require.False(t, ok, "整段重放（无 previous_response_id）：新基线，清掉记录")
	_, ok = state.windowReasoningEffortForFrame(frame("resp_3", user))
	require.False(t, ok, "清掉之后增量帧不再带出旧值")

	_, _ = state.windowReasoningEffortForFrame(frame("resp_3b", user, update("high")))
	prewarm := frame("", user)
	prewarm["generate"] = false
	_, ok = state.windowReasoningEffortForFrame(prewarm)
	require.False(t, ok, "预热帧不带取值")
	effort, ok = state.windowReasoningEffortForFrame(frame("resp_3c", user))
	require.True(t, ok, "网关为整段重放补的预热帧不能清掉窗口记录")
	require.Equal(t, "high", effort)

	var nilState *officialOpenAIWSDerivedState
	_, ok = nilState.windowReasoningEffortForFrame(frame("resp_4", user, update("low")))
	require.False(t, ok)

	// 无状态部分：update 优先于请求级与模型默认值。
	payload := frame("", user, update("xhigh"))
	payload["reasoning"] = map[string]any{"effort": "medium"}
	require.Equal(t, "xhigh", officialOpenAIEffectiveReasoningEffort(payload, officialOpenAIReasoningDefaults{Effort: "low"}))
	delete(payload, "input")
	require.Equal(t, "medium", officialOpenAIEffectiveReasoningEffort(payload, officialOpenAIReasoningDefaults{Effort: "low"}))
}

// WS passthrough 的用量元数据：逐帧推进，增量续链帧沿用之前帧里的 update；blocked／失败的帧不推进。
func TestWSPassthroughUsageMetaCarriesConfigurationUpdateEffortAcrossIncrementalFrames(t *testing.T) {
	first := []byte(`{"type":"response.create","model":"gpt-6.1-sol","reasoning":{"effort":"medium"},"input":[` + effUserMessage + `]}`)
	meta := newOpenAIWSPassthroughUsageMeta("gpt-6.1-sol", first)
	meta.initFromFirstFrame(first, "gpt-6.1-sol")
	meta.captureRequestedReasoningEffort(first, "gpt-6.1-sol")
	require.Equal(t, "medium", optionalStringValue(meta.reasoningEffort.Load()))
	require.Equal(t, "medium", optionalStringValue(meta.requestedReasoningEffort.Load()))

	incremental := func(previous string, items ...string) []byte {
		return []byte(fmt.Sprintf(`{"type":"response.create","model":"gpt-6.1-sol","previous_response_id":%q,"reasoning":{"effort":"medium"},"input":[%s]}`,
			previous, strings.Join(items, ",")))
	}
	step := func(frame []byte) {
		meta.captureRequestedReasoningEffort(frame)
		meta.updateFromResponseCreate(frame, "gpt-6.1-sol", "gpt-6.1-sol")
	}
	step(incremental("resp_1", effUserMessage, effUpdateHigh))
	require.Equal(t, "high", optionalStringValue(meta.reasoningEffort.Load()))
	require.Equal(t, "high", optionalStringValue(meta.requestedReasoningEffort.Load()))
	// 之后不带 update 的增量帧：沿用 high，首包超时与计费都按 high。
	step(incremental("resp_2", effUserMessage))
	require.Equal(t, "high", optionalStringValue(meta.reasoningEffort.Load()))
	require.Equal(t, "high", optionalStringValue(meta.requestedReasoningEffort.Load()))
	// 压缩后的整段重放（无 previous_response_id）：回到新基线。
	replay := []byte(`{"type":"response.create","model":"gpt-6.1-sol","reasoning":{"effort":"low"},"input":[` + effUserMessage + `]}`)
	step(replay)
	require.Equal(t, "low", optionalStringValue(meta.reasoningEffort.Load()))
	require.Equal(t, "low", optionalStringValue(meta.requestedReasoningEffort.Load()))

	// 出站帧上的 previous_response_id 可能是网关为整段重放补预热后链出来的：判断增量续链要看客户端帧。
	step(incremental("resp_3", effUserMessage, effUpdateXHigh))
	require.Equal(t, "xhigh", optionalStringValue(meta.reasoningEffort.Load()))
	chainedOut := incremental("resp_prewarm", effUserMessage)
	meta.updateFromResponseCreateFrame(chainedOut, replay, "gpt-6.1-sol", "gpt-6.1-sol")
	require.Equal(t, "medium", optionalStringValue(meta.reasoningEffort.Load()),
		"客户端帧是整段重放（无 previous_response_id）：清掉记录，出站帧的 previous_response_id 不算续链")
}

// 分组 effort 上限、映射与拒绝策略也要作用于 configuration_update，否则用户在窗口中途改档就能绕过上限。
func TestReasoningEffortPolicyAppliesToConfigurationUpdate(t *testing.T) {
	body := effBody("medium", effUserMessage, effUpdateXHigh, effUserMessage, effUpdateLow)

	// 降级：xhigh 的 update 压到上限 high，低于上限的 low 不动，请求级不变。
	capped, changed, err := ApplyOpenAIReasoningEffortPolicy(body, "high", nil, ReasoningEffortOverLimitDowngrade)
	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(t, "high", gjson.GetBytes(capped, "input.1.reasoning.effort").String())
	require.Equal(t, "low", gjson.GetBytes(capped, "input.3.reasoning.effort").String())
	require.Equal(t, "medium", gjson.GetBytes(capped, "reasoning.effort").String())
	require.Equal(t, `{"type":"configuration_update","reasoning":{"effort":"high"}}`, gjson.GetBytes(capped, "input.1").Raw)
	// 压完之后读到的生效 effort 是最后一条（low）。
	require.Equal(t, "low", effectiveReasoningEffortRaw(capped))

	// 拒绝：超过上限的 update 直接拒绝。
	_, _, err = ApplyOpenAIReasoningEffortPolicy(body, "high", nil, ReasoningEffortOverLimitDeny)
	var overLimit *ReasoningEffortOverLimitError
	require.ErrorAs(t, err, &overLimit)
	require.Equal(t, "xhigh", overLimit.Requested)

	// 映射：high→low 作用于 update 项；xhigh 的 update 经 high 再被上限处理。
	mappings := []ReasoningEffortMapping{{From: "xhigh", To: "high"}}
	mapped, changed, err := ApplyOpenAIReasoningEffortPolicy(body, "", mappings, ReasoningEffortOverLimitDowngrade)
	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(t, "high", gjson.GetBytes(mapped, "input.1.reasoning.effort").String())

	// 映射到 deny：命中的 update 拒绝请求。
	_, _, err = ApplyOpenAIReasoningEffortPolicy(body, "", []ReasoningEffortMapping{{From: "xhigh", To: ReasoningEffortMappingDeny}}, "")
	var denied *ReasoningEffortMappingDeniedError
	require.ErrorAs(t, err, &denied)

	// 没有 configuration_update 的请求：与改动之前一致（字节不变、changed 为假）。
	plain := effBody("medium", effUserMessage)
	same, changed, err := ApplyOpenAIReasoningEffortPolicy(plain, "high", nil, ReasoningEffortOverLimitDowngrade)
	require.NoError(t, err)
	require.False(t, changed)
	require.Equal(t, string(plain), string(same))

	// 未配置策略：原样返回。
	same, changed, err = ApplyOpenAIReasoningEffortPolicy(body, "", nil, "")
	require.NoError(t, err)
	require.False(t, changed)
	require.Equal(t, string(body), string(same))

	// WS 帧入口同口径。
	hooks := &OpenAIWSIngressHooks{MaxReasoningEffort: "high", MaxReasoningEffortOverLimit: ReasoningEffortOverLimitDowngrade}
	frame := []byte(`{"type":"response.create","input":[` + effUpdateXHigh + `]}`)
	out, err := applyOpenAIWSReasoningEffortPolicy(frame, hooks)
	require.NoError(t, err)
	require.Equal(t, "high", gjson.GetBytes(out, "input.0.reasoning.effort").String())
}

// 转换到 Chat Completions／Anthropic 的路径上游不认识 configuration_update：折进请求级 effort 后，转换、用量与计费一致。
func TestFoldConfigurationUpdateEffortIntoResponsesRequest(t *testing.T) {
	parse := func(body string) *apicompat.ResponsesRequest {
		var req apicompat.ResponsesRequest
		require.NoError(t, json.Unmarshal([]byte(body), &req))
		return &req
	}
	req := parse(`{"model":"m","reasoning":{"effort":"medium","summary":"auto"},"input":[` + effUserMessage + `,` + effUpdateHigh + `]}`)
	require.True(t, foldConfigurationUpdateEffortIntoResponsesRequest(req))
	require.Equal(t, "high", req.Reasoning.Effort)
	require.Equal(t, "auto", req.Reasoning.Summary, "只改 effort")

	req = parse(`{"model":"m","input":[` + effUpdateHigh + `]}`)
	require.True(t, foldConfigurationUpdateEffortIntoResponsesRequest(req))
	require.NotNil(t, req.Reasoning)
	require.Equal(t, "high", req.Reasoning.Effort)

	req = parse(`{"model":"m","reasoning":{"effort":"high"},"input":[` + effUpdateHigh + `]}`)
	require.False(t, foldConfigurationUpdateEffortIntoResponsesRequest(req), "已经相同：不算改动")
	req = parse(`{"model":"m","reasoning":{"effort":"medium"},"input":[` + effUserMessage + `]}`)
	require.False(t, foldConfigurationUpdateEffortIntoResponsesRequest(req))
	require.Equal(t, "medium", req.Reasoning.Effort)
	req = parse(`{"model":"m","input":"hello"}`)
	require.False(t, foldConfigurationUpdateEffortIntoResponsesRequest(req))
	require.Nil(t, req.Reasoning)
	require.False(t, foldConfigurationUpdateEffortIntoResponsesRequest(nil))
}

// 端到端：Codex 客户端的 Responses 请求（基线 medium，中途升到 high）转成 Anthropic 请求，转发的 output_config.effort、
// 用量日志里的 effort 与按渠道倍率算出的费用都是升档后的 high。
func TestResponsesToNativeAnthropicHonorsMidWindowEffortUpgrade(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			body, err := json.Marshal(map[string]any{
				"model":     "k3",
				"stream":    stream,
				"reasoning": map[string]string{"effort": "medium"},
				"input": []json.RawMessage{
					json.RawMessage(effUserMessage),
					json.RawMessage(effUpdateHigh),
				},
			})
			require.NoError(t, err)
			resp := nativeAnthropicStreamResponse()
			responseBody, err := io.ReadAll(resp.Body)
			require.NoError(t, err)
			resp.Body = io.NopCloser(strings.NewReader(strings.ReplaceAll(string(responseBody), `"input_tokens":93`, `"input_tokens":1000000`)))
			upstream := &httpUpstreamRecorder{resp: resp}
			svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
			c := adaptiveProtocolTestContext("/v1/responses", body)
			account := nativeAnthropicTestAccount()
			account.Credentials["base_url"] = "http://anthropic.example"
			result, err := svc.Forward(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, "/v1/messages", upstream.lastReq.URL.Path)
			require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "output_config.effort").String(), "上游收到升档后的 effort")
			require.Equal(t, "high", optionalStringValue(result.ReasoningEffort))
			assertConvertedReasoningPrice(t, result, 2)
		})
	}
}

// 端到端（ingress 会话，直连 WS 上游）：官方客户端用 previous_response_id 续链的增量帧只含新增项。第 2 帧中途升到 high（update 在本帧），
// 第 3 帧不再带 update，用量结果里的 effort（计费与首包超时的来源）仍是 high，而不是被钉死的请求级基线 medium；
// 压缩或换模型后的整段重放帧（无 previous_response_id）回到新基线。
func TestOpenAIGatewayService_ProxyResponsesWebSocketFromClient_CarriesConfigurationUpdateEffortAcrossIncrementalTurns(t *testing.T) {
	gin.SetMode(gin.TestMode)

	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.APIKeyEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.QueueLimitPerConn = 8
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3

	completed := func(id string) []byte {
		return []byte(`{"type":"response.completed","response":{"id":"` + id + `","model":"gpt-5.1","usage":{"input_tokens":1,"output_tokens":1}}}`)
	}
	captureConn := &openAIWSCaptureConn{events: [][]byte{
		completed("resp_eff_1"), completed("resp_eff_2"), completed("resp_eff_3"), completed("resp_eff_4"),
	}}
	captureDialer := &openAIWSCaptureDialer{conn: captureConn}
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(captureDialer)

	svc := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     &httpUpstreamRecorder{},
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
		openaiWSPool:     pool,
	}
	account := &Account{
		ID: 115, Name: "openai-ingress-effort-carry", Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Status: StatusActive, Schedulable: true, Concurrency: 1,
		Credentials: map[string]any{"api_key": "sk-test"},
		Extra:       map[string]any{"responses_websockets_v2_enabled": true},
	}

	type turnEfforts struct{ effective, requested string }
	serverErrCh := make(chan error, 1)
	turnCh := make(chan turnEfforts, 4)
	hooks := &OpenAIWSIngressHooks{
		AfterTurn: func(_ int, result *OpenAIForwardResult, turnErr error) {
			if turnErr == nil && result != nil {
				turnCh <- turnEfforts{optionalStringValue(result.ReasoningEffort), optionalStringValue(result.RequestedReasoningEffort)}
			}
		},
	}
	wsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			serverErrCh <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()

		rec := httptest.NewRecorder()
		ginCtx, _ := gin.CreateTestContext(rec)
		req := r.Clone(r.Context())
		req.Header = req.Header.Clone()
		req.Header.Set("User-Agent", "unit-test-agent/1.0")
		ginCtx.Request = req

		readCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			serverErrCh <- readErr
			return
		}
		memoryCtx, releaseMemory := openAIWSMemoryTestContext(t, r.Context(), firstMessage)
		defer releaseMemory()
		serverErrCh <- svc.ProxyResponsesWebSocketFromClient(memoryCtx, ginCtx, conn, account, "sk-test", firstMessage, hooks)
	}))
	defer wsServer.Close()

	dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
	clientConn, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), nil)
	cancelDial()
	require.NoError(t, err)
	defer func() { _ = clientConn.CloseNow() }()

	roundTrip := func(payload string, wantResponseID string) {
		t.Helper()
		writeCtx, cancelWrite := context.WithTimeout(context.Background(), 3*time.Second)
		require.NoError(t, clientConn.Write(writeCtx, coderws.MessageText, []byte(payload)))
		cancelWrite()
		readCtx, cancelRead := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancelRead()
		_, event, readErr := clientConn.Read(readCtx)
		require.NoError(t, readErr)
		require.Equal(t, wantResponseID, gjson.GetBytes(event, "response.id").String())
	}
	frame := func(previous string, baseline string, items ...string) string {
		prefix := ""
		if previous != "" {
			prefix = `"previous_response_id":"` + previous + `",`
		}
		return `{"type":"response.create","model":"gpt-5.1","stream":false,` + prefix +
			`"reasoning":{"effort":"` + baseline + `"},"input":[` + strings.Join(items, ",") + `]}`
	}
	nextTurn := func() turnEfforts {
		t.Helper()
		select {
		case got := <-turnCh:
			return got
		case <-time.After(3 * time.Second):
			t.Fatal("未收到 turn 结果回调")
			return turnEfforts{}
		}
	}

	roundTrip(frame("", "medium", effUserMessage), "resp_eff_1")
	require.Equal(t, turnEfforts{"medium", "medium"}, nextTurn(), "首帧：请求级基线")
	roundTrip(frame("resp_eff_1", "medium", effUserMessage, effUpdateHigh), "resp_eff_2")
	require.Equal(t, turnEfforts{"high", "high"}, nextTurn(), "中途升档：update 在本帧，按 high")
	roundTrip(frame("resp_eff_2", "medium", effUserMessage), "resp_eff_3")
	require.Equal(t, turnEfforts{"high", "high"}, nextTurn(), "增量帧不再带 update：沿用升档后的 high，不回落到基线")
	roundTrip(frame("", "low", effUserMessage), "resp_eff_4")
	require.Equal(t, turnEfforts{"low", "low"}, nextTurn(), "整段重放（新窗口）：回到新基线")

	_ = clientConn.Close(coderws.StatusNormalClosure, "done")
	select {
	case serverErr := <-serverErrCh:
		if serverErr != nil && !errors.Is(serverErr, context.Canceled) {
			require.Contains(t, serverErr.Error(), "StatusNormalClosure")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("等待 ingress websocket 结束超时")
	}
}

// 官方出站定型里网关合成的项是预编码 JSON（json.RawMessage），生效 effort 的读取要认它。
func TestLastConfigurationUpdateEffortFromInputReadsPreEncodedItems(t *testing.T) {
	input := []any{
		map[string]any{"type": "message", "role": "user"},
		json.RawMessage(`{"type":"configuration_update","reasoning":{"effort":"xhigh"}}`),
		json.RawMessage(`{"type":"message","id":"msg_x","role":"developer"}`),
	}
	effort, ok := lastConfigurationUpdateEffortFromInput(input)
	require.True(t, ok)
	require.Equal(t, "xhigh", effort)
	_, ok = lastConfigurationUpdateEffortFromInput([]any{json.RawMessage(`{"type":"message","role":"user"}`)})
	require.False(t, ok)
}
