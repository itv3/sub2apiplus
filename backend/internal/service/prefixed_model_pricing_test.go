//go:build unit

package service

import (
	"context"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

func TestPrefixedModelPricingGlobalLookup(t *testing.T) {
	const bareModel = "deepseek-v4.1-flash"
	for _, tc := range []struct {
		name      string
		model     string
		exact     bool
		zero      bool
		available bool
	}{
		{name: "完整名优先于裸名", model: "cline-pass/" + bareModel, exact: true, available: true},
		{name: "完整名零价仍然优先", model: "cline-pass/" + bareModel, exact: true, zero: true, available: true},
		{name: "多层前缀保留完整名独立价格", model: "router/cline-pass/" + bareModel, exact: true, available: true},
		{name: "多层前缀缺少完整名时查找最后一段", model: "router/cline-pass/" + bareModel, available: true},
		{name: "规范前缀同样先匹配完整名", model: "models/" + bareModel, exact: true, available: true},
		{name: "末段也未知时不编造价格", model: "router/cline-pass/no-known-price"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			barePricing := &LiteLLMModelPricing{InputCostPerToken: 3e-6, OutputCostPerToken: 5e-6}
			pricing := &PricingService{pricingData: map[string]*LiteLLMModelPricing{bareModel: barePricing}}
			want := barePricing
			if tc.exact {
				want = &LiteLLMModelPricing{InputCostPerToken: 7e-6, OutputCostPerToken: 11e-6}
				if tc.zero {
					want = &LiteLLMModelPricing{}
				}
				pricing.pricingData[tc.model] = want
			}
			if !tc.available {
				require.Nil(t, pricing.GetModelPricing(tc.model))
				require.Nil(t, pricing.GetIdentifiedModelPricing(tc.model))
				return
			}
			require.Same(t, want, pricing.GetModelPricing(tc.model))
			require.Same(t, want, pricing.GetIdentifiedModelPricing(tc.model), "确定性查价应保留相同的完整名优先级")
		})
	}
}

func TestPrefixedModelPricingCanonicalAlias(t *testing.T) {
	for _, tc := range []struct {
		name  string
		exact bool
	}{
		{name: "规范别名保留完整名独立价格", exact: true},
		{name: "完整名缺省时继续识别规范型号"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			canonical := &LiteLLMModelPricing{InputCostPerToken: 3e-6}
			pricing := &PricingService{pricingData: map[string]*LiteLLMModelPricing{"gpt-5.6-sol": canonical}}
			want := canonical
			if tc.exact {
				want = &LiteLLMModelPricing{InputCostPerToken: 7e-6}
				pricing.pricingData["models/gpt-5.6"] = want
			}
			require.Same(t, want, pricing.GetModelPricing("models/gpt-5.6"))
			require.Same(t, want, pricing.GetIdentifiedModelPricing("models/gpt-5.6"))
		})
	}
}

func TestPrefixedModelPricingUnknownProviderName(t *testing.T) {
	t.Run("供应商前缀中的家族关键词不能为未知末段提供价格", func(t *testing.T) {
		pricing := &PricingService{pricingData: map[string]*LiteLLMModelPricing{
			"claude-sonnet-4": {InputCostPerToken: 3e-6},
			"claude-3-opus":   {InputCostPerToken: 15e-6},
		}}
		billing := NewBillingService(&config.Config{}, pricing)
		for _, model := range []string{"claude-provider/no-known-price", "opus-provider/no-known-price", "router/claude-opus-provider/no-known-price"} {
			require.Nil(t, pricing.GetModelPricing(model), model)
			require.False(t, billing.HasIdentifiedTokenPricing(model), model)
		}
	})
}

func TestPrefixedModelPricingLegacyPathAlias(t *testing.T) {
	t.Run("保留已有路径型别名的内置兜底价格", func(t *testing.T) {
		billing := newTestBillingService()
		tokens := UsageTokens{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 1000}
		expected, err := billing.CalculateCost("kimi-k2", tokens, 1)
		require.NoError(t, err)
		require.Greater(t, expected.TotalCost, 0.0)
		for _, model := range []string{"kimi/k2", "router/kimi/k2"} {
			cost, err := billing.CalculateCostUnified(CostInput{
				Ctx: context.Background(), Model: model, Tokens: tokens,
				RateMultiplier: 1, Resolver: NewModelPricingResolver(nil, billing),
			})
			require.NoError(t, err, model)
			require.InDelta(t, expected.InputCost, cost.InputCost, 1e-12, model)
			require.InDelta(t, expected.OutputCost, cost.OutputCost, 1e-12, model)
			require.InDelta(t, expected.CacheReadCost, cost.CacheReadCost, 1e-12, model)
			require.InDelta(t, expected.TotalCost, cost.TotalCost, 1e-12, model)
		}
	})
}

func TestPrefixedModelPricingImageOnlyEntry(t *testing.T) {
	for _, tc := range []struct {
		name  string
		model string
	}{
		{name: "完整名图片价不能充当合法的零元令牌价", model: "cline-pass/imagen-private"},
		{name: "完整名图片价不能退回同名末段的令牌价", model: "provider/jev-latest"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pricing := newStubPricingServiceFromJSON(t, `{"`+tc.model+`":{"output_cost_per_image":0.04,"mode":"image_generation"}}`)
			raw := pricing.GetModelPricing(tc.model)
			require.NotNil(t, raw)
			require.True(t, raw.TokenPricingAbsent)
			require.InDelta(t, 0.04, raw.OutputCostPerImage, 1e-12)
			billing := NewBillingService(&config.Config{}, pricing)
			require.False(t, billing.HasIdentifiedTokenPricing(tc.model))
			_, err := billing.GetModelPricing(tc.model)
			require.ErrorIs(t, err, ErrModelPricingUnavailable)
			_, err = billing.CalculateCostUnified(CostInput{
				Ctx: context.Background(), Model: tc.model, Tokens: UsageTokens{InputTokens: 1000},
				RateMultiplier: 1, Resolver: NewModelPricingResolver(nil, billing),
			})
			require.ErrorIs(t, err, ErrModelPricingUnavailable)
		})
	}
}

func TestPrefixedModelPricingConfiguredLayers(t *testing.T) {
	const model = "router/cline-pass/deepseek-v4.1-flash"
	const bareModel = "deepseek-v4.1-flash"
	for _, tc := range []struct {
		name          string
		groupModels   []string
		groupPrices   []float64
		channelModels []string
		channelPrices []float64
		wantSource    string
		wantInput     float64
	}{
		{
			name:        "分组完整名优先于裸名",
			groupModels: []string{bareModel, model}, groupPrices: []float64{0.3, 0.7},
			channelModels: []string{model}, channelPrices: []float64{0.9}, wantSource: PricingSourceGroup, wantInput: 0.7,
		},
		{
			name:        "分组完整名零价不回退",
			groupModels: []string{bareModel, model}, groupPrices: []float64{0.3, 0},
			channelModels: []string{model}, channelPrices: []float64{0.9}, wantSource: PricingSourceGroup,
		},
		{
			name:        "分组完整名通配优先于裸名精确价",
			groupModels: []string{bareModel, "router/cline-pass/*"}, groupPrices: []float64{0.3, 0.8},
			wantSource: PricingSourceGroup, wantInput: 0.8,
		},
		{
			name:        "分组裸名回退仍优先于渠道完整名",
			groupModels: []string{bareModel}, groupPrices: []float64{0.3},
			channelModels: []string{model}, channelPrices: []float64{0.9}, wantSource: PricingSourceGroup, wantInput: 0.3,
		},
		{
			name:        "分组裸名通配保持自定义计费",
			groupModels: []string{"deepseek-*"}, groupPrices: []float64{0.35}, wantSource: PricingSourceGroup, wantInput: 0.35,
		},
		{
			name:          "渠道完整名优先于裸名",
			channelModels: []string{bareModel, model}, channelPrices: []float64{0.3, 0.7}, wantSource: PricingSourceChannel, wantInput: 0.7,
		},
		{
			name:          "渠道完整名零价不回退",
			channelModels: []string{bareModel, model}, channelPrices: []float64{0.3, 0}, wantSource: PricingSourceChannel,
		},
		{
			name:          "渠道完整名通配优先于裸名精确价",
			channelModels: []string{bareModel, "router/cline-pass/*"}, channelPrices: []float64{0.3, 0.8}, wantSource: PricingSourceChannel, wantInput: 0.8,
		},
		{
			name:          "渠道裸名回退优先于全局完整名",
			channelModels: []string{bareModel}, channelPrices: []float64{0.4}, wantSource: PricingSourceChannel, wantInput: 0.4,
		},
		{
			name:          "渠道裸名通配保持自定义计费",
			channelModels: []string{"deepseek-*"}, channelPrices: []float64{0.45}, wantSource: PricingSourceChannel, wantInput: 0.45,
		},
		{
			name:        "分组与渠道均不匹配时使用全局完整名",
			groupModels: []string{"another-model"}, groupPrices: []float64{0.1},
			channelModels: []string{"another-model"}, channelPrices: []float64{0.2}, wantSource: PricingSourceLiteLLM, wantInput: 7,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			const groupID = int64(778)
			group := &Group{ID: groupID, Platform: PlatformOpenAI}
			for i, pattern := range tc.groupModels {
				group.ModelPricing = append(group.ModelPricing, tokenPricingForModels([]string{pattern}, tc.groupPrices[i]))
			}
			channelPricing := make([]ChannelModelPricing, 0, len(tc.channelModels))
			for i, pattern := range tc.channelModels {
				channelPricing = append(channelPricing, tokenPricingForModels([]string{pattern}, tc.channelPrices[i]))
			}
			channel := newChannelServiceWithPricings(groupID, channelPricing)
			billing := NewBillingService(&config.Config{}, &PricingService{pricingData: map[string]*LiteLLMModelPricing{
				model: {InputCostPerToken: 7e-6, OutputCostPerToken: 11e-6},
			}})
			resolver := NewModelPricingResolver(channel, billing)
			resolved := resolver.Resolve(context.Background(), PricingInput{Model: model, GroupID: i64p(groupID), Group: group})
			require.Equal(t, tc.wantSource, resolved.Source)
			require.NotNil(t, resolved.BasePricing)
			require.InDelta(t, tc.wantInput/1e6, resolved.BasePricing.InputPricePerToken, 1e-15)

			// 固定在官方高峰期，自定义分组和渠道价格仍必须按运营者配置计算。
			cost, err := billing.CalculateCostUnified(CostInput{
				Ctx: context.Background(), Model: model, GroupID: i64p(groupID), Group: group,
				Tokens: UsageTokens{InputTokens: 1_000_000}, RateMultiplier: 1,
				Resolver: resolver, Resolved: resolved, PricingAt: time.Date(2026, 8, 24, 2, 0, 0, 0, time.UTC),
			})
			require.NoError(t, err)
			require.InDelta(t, tc.wantInput, cost.InputCost, 1e-12)
		})
	}
}

func TestPrefixedModelPricingBuiltinDeepSeek(t *testing.T) {
	for _, tc := range []struct {
		name       string
		pricingAt  time.Time
		multiplier float64
	}{
		{name: "固定工作日低谷价", pricingAt: time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC), multiplier: 1},
		{name: "固定工作日高峰价", pricingAt: time.Date(2026, 8, 24, 2, 0, 0, 0, time.UTC), multiplier: 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, source := range []struct {
				name    string
				dynamic bool
			}{
				{name: "无动态价格服务使用内置兜底"},
				{name: "动态裸名价仍适用官方峰谷口径", dynamic: true},
			} {
				t.Run(source.name, func(t *testing.T) {
					billing := newTestBillingService()
					if source.dynamic {
						// 故意提供旧目录价格，验证前缀回退后仍执行官方价修正。
						billing.pricingService = newStubPricingServiceFromJSON(t, `{"deepseek-v4.1-flash":{"input_cost_per_token":9e-6,"output_cost_per_token":19e-6,"cache_read_input_token_cost":7e-6}}`)
					} else {
						require.Nil(t, billing.pricingService, "回归必须覆盖未启用动态价格服务的部署")
					}
					resolver := NewModelPricingResolver(nil, billing)
					tokens := UsageTokens{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 1000}
					for _, model := range []string{"deepseek-v4.1-flash", "cline-pass/deepseek-v4.1-flash", "router/cline-pass/deepseek-v4.1-flash"} {
						cost, err := billing.CalculateCostUnified(CostInput{
							Ctx: context.Background(), Model: model, Tokens: tokens,
							RateMultiplier: 1, Resolver: resolver, PricingAt: tc.pricingAt,
						})
						require.NoError(t, err, "模型 %s 必须能使用内置兜底价格", model)
						require.InDelta(t, 1000*1.5e-7*tc.multiplier, cost.InputCost, 1e-12, model)
						require.InDelta(t, 500*6e-7*tc.multiplier, cost.OutputCost, 1e-12, model)
						require.InDelta(t, 1000*3e-9*tc.multiplier, cost.CacheReadCost, 1e-12, model)
						require.InDelta(t, (1000*1.5e-7+500*6e-7+1000*3e-9)*tc.multiplier, cost.TotalCost, 1e-12, model)
						require.Greater(t, cost.ActualCost, 0.0)
					}
				})
			}
		})
	}
}

func TestPrefixedModelPricingIdentifiedFallback(t *testing.T) {
	billing := newTestBillingService()
	for _, tc := range []struct {
		name  string
		model string
		want  bool
	}{
		{name: "完整前缀加已知精确型号可以确定性识别", model: "cline-pass/gpt-5.4", want: true},
		{name: "多层前缀加已知精确型号可以确定性识别", model: "router/cline-pass/deepseek-v4-flash", want: true},
		{name: "精确型号兜底不局限于模型家族", model: "router/cline-pass/jev-latest", want: true},
		{name: "未知型号不能仅凭家族关键词识别", model: "cline-pass/claude-invented-opus"},
		{name: "未知深度求索型号不能冒充精确型号", model: "cline-pass/deepseek-invented-model"},
		{name: "完全未知型号保持不可识别", model: "router/cline-pass/no-known-price"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, billing.HasIdentifiedTokenPricing(tc.model))
		})
	}
}

func TestPrefixedModelPricingExplicitGlobalCard(t *testing.T) {
	for _, tc := range []struct {
		name    string
		pricing *LiteLLMModelPricing
	}{
		{name: "完整名独立价格不被裸名政策覆盖", pricing: &LiteLLMModelPricing{InputCostPerToken: 7e-6, OutputCostPerToken: 11e-6, CacheReadInputTokenCost: 2e-6}},
		{name: "完整名独立零价不触发官方兜底", pricing: &LiteLLMModelPricing{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, provider := range []struct {
				name   string
				prefix string
			}{
				{name: "常规供应商前缀", prefix: "cline-pass"},
				{name: "供应商前缀自身含模型家族关键词", prefix: "deepseek-provider"},
			} {
				t.Run(provider.name, func(t *testing.T) {
					model := provider.prefix + "/deepseek-v4.1-flash"
					billing := NewBillingService(&config.Config{}, &PricingService{pricingData: map[string]*LiteLLMModelPricing{
						model:                 tc.pricing,
						"deepseek-v4.1-flash": {InputCostPerToken: 99e-6, OutputCostPerToken: 199e-6},
					}})
					resolver := NewModelPricingResolver(nil, billing)
					for _, hour := range []int{2, 12} {
						cost, err := billing.CalculateCostUnified(CostInput{
							Ctx: context.Background(), Model: model,
							Tokens:         UsageTokens{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 1000},
							RateMultiplier: 1, Resolver: resolver, PricingAt: time.Date(2026, 8, 24, hour, 0, 0, 0, time.UTC),
						})
						require.NoError(t, err)
						require.InDelta(t, 1000*tc.pricing.InputCostPerToken, cost.InputCost, 1e-12)
						require.InDelta(t, 500*tc.pricing.OutputCostPerToken, cost.OutputCost, 1e-12)
						require.InDelta(t, 1000*tc.pricing.CacheReadInputTokenCost, cost.CacheReadCost, 1e-12)
					}
				})
			}
		})
	}
}

func TestPrefixedModelPricingRecordUsage(t *testing.T) {
	t.Run("前缀模型记录非零费用并扣款且保留完整名称", func(t *testing.T) {
		const model = "cline-pass/deepseek-v4.1-flash"
		usageRepo := &openAIRecordUsageLogRepoStub{inserted: true}
		userRepo := &openAIRecordUsageUserRepoStub{}
		service := newOpenAIRecordUsageServiceForTest(usageRepo, userRepo, &openAIRecordUsageSubRepoStub{}, nil)
		require.Nil(t, service.billingService.pricingService)
		err := service.RecordUsage(context.Background(), &OpenAIRecordUsageInput{
			Result: &OpenAIForwardResult{
				RequestID: "prefixed-model-pricing-record-usage", Model: model, BillingModel: model, UpstreamModel: model,
				Usage: OpenAIUsage{InputTokens: 2000, OutputTokens: 500, CacheReadInputTokens: 1000}, Duration: time.Second,
			},
			APIKey: &APIKey{ID: 10}, User: &User{ID: 20}, Account: &Account{ID: 30, Platform: PlatformOpenAI, Type: AccountTypeAPIKey},
			ChannelUsageFields: ChannelUsageFields{OriginalModel: model},
			PricingAt:          time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC),
		})
		require.NoError(t, err)
		require.Equal(t, 1, usageRepo.calls)
		require.NotNil(t, usageRepo.lastLog)
		log := usageRepo.lastLog
		require.Equal(t, model, log.Model)
		require.Equal(t, model, log.RequestedModel)
		require.NotNil(t, log.UpstreamModel)
		require.Equal(t, model, *log.UpstreamModel)
		expectedTotal := 1000*1.5e-7 + 500*6e-7 + 1000*3e-9
		require.InDelta(t, expectedTotal, log.TotalCost, 1e-12)
		require.InDelta(t, expectedTotal*1.1, log.ActualCost, 1e-12)
		require.Greater(t, log.ActualCost, 0.0, "有真实用量的已知模型不得因前缀而零计费")
		require.Equal(t, 1, userRepo.deductCalls)
		require.InDelta(t, log.ActualCost, userRepo.lastAmount, 1e-12)
	})
}
