//go:build unit

package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func plazaGroups() []service.PlazaGroup {
	return []service.PlazaGroup{
		{ID: 1, Name: "public-standard", Platform: "anthropic", SubscriptionType: "standard", RateMultiplier: 1},
		{ID: 2, Name: "exclusive-a", Platform: "anthropic", IsExclusive: true, RateMultiplier: 0.5},
		{ID: 3, Name: "public-subscription", Platform: "openai", SubscriptionType: "subscription", RateMultiplier: 1},
		{ID: 4, Name: "exclusive-b", Platform: "openai", IsExclusive: true, RateMultiplier: 0.8},
	}
}

func TestFilterPlazaVisibleGroups_AnonymousSeesOnlyNonExclusive(t *testing.T) {
	// 匿名(allowedExclusive == nil):仅非专属分组;订阅型公开分组照常可见(橱窗语义)。
	visible := filterPlazaVisibleGroups(plazaGroups(), nil, false)
	require.Len(t, visible, 2)
	ids := []int64{visible[0].ID, visible[1].ID}
	require.ElementsMatch(t, []int64{1, 3}, ids)
}

func TestFilterPlazaVisibleGroups_AuthedSeesGrantedExclusive(t *testing.T) {
	// 登录:非专属 + 授权的专属;未授权的专属仍不可见。
	allowed := map[int64]struct{}{2: {}}
	visible := filterPlazaVisibleGroups(plazaGroups(), allowed, false)
	require.Len(t, visible, 3)
	ids := make([]int64, 0, len(visible))
	for _, g := range visible {
		ids = append(ids, g.ID)
	}
	require.ElementsMatch(t, []int64{1, 2, 3}, ids)
}

func TestFilterPlazaVisibleGroups_AuthedEmptySetSeesNoExclusive(t *testing.T) {
	// 登录但无任何专属授权(空集合,非 nil):与匿名同样只见非专属,
	// 但语义区分要保持——空集合不能被当作 nil 匿名分支。
	visible := filterPlazaVisibleGroups(plazaGroups(), map[int64]struct{}{}, false)
	require.Len(t, visible, 2)
}

func TestFilterPlazaVisibleGroups_RestrictedUserSeesOnlyGrantedPublic(t *testing.T) {
	// 开启公开分组限制后，公开分组也必须落在授权集合内，否则用户会在广场
	// 看到自己实际绑定不了的分组。
	allowed := map[int64]struct{}{1: {}, 2: {}}
	visible := filterPlazaVisibleGroups(plazaGroups(), allowed, true)
	ids := make([]int64, 0, len(visible))
	for _, g := range visible {
		ids = append(ids, g.ID)
	}
	// 3 是未授权的公开分组，受限后不可见；4 是未授权的专属分组，一贯不可见。
	require.ElementsMatch(t, []int64{1, 2}, ids)
}

func TestFilterPlazaVisibleGroups_RestrictionDoesNotAffectAnonymous(t *testing.T) {
	// 匿名没有用户记录，限制标志无从谈起，可见性必须与未受限时一致。
	visible := filterPlazaVisibleGroups(plazaGroups(), nil, true)
	ids := make([]int64, 0, len(visible))
	for _, g := range visible {
		ids = append(ids, g.ID)
	}
	require.ElementsMatch(t, []int64{1, 3}, ids)
}

func TestModelPlazaHandler_NilSettingServiceFailsClosed404(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := &ModelPlazaHandler{} // settingService == nil → fail-closed
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/v1/model-plaza", nil)

	h.Get(c)

	require.Equal(t, http.StatusNotFound, w.Code)
}

func TestToModelPlazaGroupDTO_UserRateAndFieldWhitelist(t *testing.T) {
	g := service.PlazaGroup{
		ID: 2, Name: "vip", Description: "d", Platform: "anthropic",
		SubscriptionType: "standard", RateMultiplier: 1, IsExclusive: true,
		Models: []service.PlazaModel{{
			Name:     "claude-sonnet",
			Platform: "anthropic",
			Pricing: &service.ChannelModelPricing{
				BillingMode: service.BillingModeToken,
				InputPrice:  testPtr(3e-6),
			},
			OfficialPricing: &service.PlazaOfficialPricing{
				InputPrice:     testPtr(3e-6),
				CacheReadPrice: testPtr(3e-7),
			},
		}},
	}

	// 有专属倍率:user_rate_multiplier 序列化输出
	dto := toModelPlazaGroupDTO(&g, map[int64]float64{2: 0.5})
	raw, err := json.Marshal(dto)
	require.NoError(t, err)
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(raw, &decoded))

	for _, key := range []string{
		"id", "name", "description", "platform", "subscription_type",
		"rate_multiplier", "user_rate_multiplier", "is_exclusive", "models",
		"peak_rate_enabled", "peak_start", "peak_end", "peak_rate_multiplier",
		"image_rate_independent", "image_rate_multiplier", "long_context_pricing_enabled",
	} {
		_, exists := decoded[key]
		require.Truef(t, exists, "plaza group DTO must expose %q", key)
	}
	require.InDelta(t, 0.5, requireType[float64](t, decoded["user_rate_multiplier"]), 1e-9)

	// 模型条目:pricing + official_pricing 并存;official 缺失字段输出 null 而非省略
	models := requireType[[]any](t, decoded["models"])
	require.Len(t, models, 1)
	model := requireType[map[string]any](t, models[0])
	require.Contains(t, model, "pricing")
	require.Contains(t, model, "official_pricing")
	official := requireType[map[string]any](t, model["official_pricing"])
	require.Contains(t, official, "input_price")
	require.Contains(t, official, "cache_read_price")
	_, has1h := official["cache_write_1h_price"]
	require.False(t, has1h, "1h 缓存写价为 nil 时应 omitempty")
	_, hasOfficialIntervals := official["intervals"]
	require.False(t, hasOfficialIntervals, "官方无阶梯时 intervals 应 omitempty")
	_, hasBasis := model["long_context_basis"]
	require.False(t, hasBasis, "单档模型不输出 long_context_basis")
	_, hasTimePricing := model["time_pricing"]
	require.False(t, hasTimePricing, "无分时时不输出 time_pricing")

	// 无专属倍率:user_rate_multiplier 整个字段省略
	dtoNoRate := toModelPlazaGroupDTO(&g, nil)
	rawNoRate, err := json.Marshal(dtoNoRate)
	require.NoError(t, err)
	var decodedNoRate map[string]any
	require.NoError(t, json.Unmarshal(rawNoRate, &decodedNoRate))
	_, hasRate := decodedNoRate["user_rate_multiplier"]
	require.False(t, hasRate, "无专属倍率时 user_rate_multiplier 应 omitempty")
}

func TestToModelPlazaOfficialPricing_NilPassthrough(t *testing.T) {
	require.Nil(t, toModelPlazaOfficialPricing(nil))
}

func TestToModelPlazaGroupDTO_LongContextTiersAndBasis(t *testing.T) {
	maxTokens := 272000
	g := service.PlazaGroup{
		ID: 3, Name: "ladder", Platform: "openai", SubscriptionType: "standard", RateMultiplier: 1,
		LongContextPricingEnabled: true,
		Models: []service.PlazaModel{{
			Name:     "gpt-5.4",
			Platform: "openai",
			Pricing: &service.ChannelModelPricing{
				BillingMode: service.BillingModeToken,
				InputPrice:  testPtr(2.5e-6),
				Intervals: []service.PricingInterval{
					{MinTokens: 0, MaxTokens: &maxTokens, TierLabel: "≤272K", InputPrice: testPtr(2.5e-6)},
					{MinTokens: 272000, TierLabel: ">272K", InputPrice: testPtr(5e-6)},
				},
			},
			OfficialPricing: &service.PlazaOfficialPricing{
				InputPrice: testPtr(2.5e-6),
				Intervals: []service.PricingInterval{
					{MinTokens: 0, MaxTokens: &maxTokens, TierLabel: "≤272K", InputPrice: testPtr(2.5e-6)},
					{MinTokens: 272000, TierLabel: ">272K", InputPrice: testPtr(5e-6)},
				},
			},
			LongContextBasis: service.ContextPricingBasisWholeRequest,
		}},
	}

	raw, err := json.Marshal(toModelPlazaGroupDTO(&g, nil))
	require.NoError(t, err)
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(raw, &decoded))
	require.Equal(t, true, decoded["long_context_pricing_enabled"])

	model := requireType[map[string]any](t, requireType[[]any](t, decoded["models"])[0])
	require.Equal(t, "whole_request", model["long_context_basis"])

	pricing := requireType[map[string]any](t, model["pricing"])
	paidTiers := requireType[[]any](t, pricing["intervals"])
	require.Len(t, paidTiers, 2)
	require.Equal(t, ">272K", requireType[map[string]any](t, paidTiers[1])["tier_label"])

	official := requireType[map[string]any](t, model["official_pricing"])
	officialTiers := requireType[[]any](t, official["intervals"])
	require.Len(t, officialTiers, 2)
	first := requireType[map[string]any](t, officialTiers[0])
	require.Equal(t, "≤272K", first["tier_label"])
	require.InDelta(t, 272000, requireType[float64](t, first["max_tokens"]), 0)
	require.Contains(t, first, "cache_write_price", "区间 DTO 字段齐全（nil 输出 null）")
}

func testPtr(v float64) *float64 { return &v }

func TestToModelPlazaGroupDTO_TimePricing(t *testing.T) {
	g := service.PlazaGroup{
		ID: 4, Name: "cn", Platform: "deepseek", SubscriptionType: "standard", RateMultiplier: 1,
		Models: []service.PlazaModel{{
			Name:     "deepseek-chat",
			Platform: "deepseek",
			Pricing:  &service.ChannelModelPricing{BillingMode: service.BillingModeToken, InputPrice: testPtr(0.28e-6)},
			TimePricing: &service.TimePricingSchedule{Timezone: "Asia/Shanghai", Periods: []service.TimePricingPeriod{
				{StartTime: "00:30", EndTime: "08:30", Multiplier: 0.5},
			}},
		}, {
			Name:     "deepseek-reasoner",
			Platform: "deepseek",
			Pricing:  &service.ChannelModelPricing{BillingMode: service.BillingModeToken, InputPrice: testPtr(0.56e-6)},
			TimePricing: &service.TimePricingSchedule{Timezone: "Asia/Shanghai", WeekdaysOnly: true, Periods: []service.TimePricingPeriod{
				{StartTime: "00:30", EndTime: "08:30", Multiplier: 0.5},
			}},
		}},
	}
	raw, err := json.Marshal(toModelPlazaGroupDTO(&g, nil))
	require.NoError(t, err)
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(raw, &decoded))
	model := requireType[map[string]any](t, requireType[[]any](t, decoded["models"])[0])
	tp := requireType[map[string]any](t, model["time_pricing"])
	require.Equal(t, "Asia/Shanghai", tp["timezone"])
	_, hasWeekdaysOnly := tp["weekdays_only"]
	require.False(t, hasWeekdaysOnly, "未开启仅工作日时字段省略")
	periods := requireType[[]any](t, tp["periods"])
	require.Len(t, periods, 1)
	first := requireType[map[string]any](t, periods[0])
	require.Equal(t, "00:30", first["start_time"])
	require.Equal(t, "08:30", first["end_time"])
	require.InDelta(t, 0.5, requireType[float64](t, first["multiplier"]), 1e-12)

	weekdaysModel := requireType[map[string]any](t, requireType[[]any](t, decoded["models"])[1])
	weekdaysTP := requireType[map[string]any](t, weekdaysModel["time_pricing"])
	require.Equal(t, true, weekdaysTP["weekdays_only"])
}

func TestFilterPlazaVisibleGroups_SubscribedExclusiveGroup(t *testing.T) {
	groups := []service.PlazaGroup{
		{ID: 42, IsExclusive: true, SubscriptionType: "subscription"},
		{ID: 43, IsExclusive: true, SubscriptionType: "subscription"},
		{ID: 44, IsExclusive: true, SubscriptionType: "standard"},
	}
	require.Empty(t, filterPlazaVisibleGroups(groups, nil, false))
	for _, restricted := range []bool{false, true} {
		visible := filterPlazaVisibleGroups(groups, map[int64]struct{}{42: {}}, restricted)
		require.Len(t, visible, 1)
		require.Equal(t, int64(42), visible[0].ID)
	}
}
