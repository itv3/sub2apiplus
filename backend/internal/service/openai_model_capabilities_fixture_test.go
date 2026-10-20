package service

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest 给 bundled 兜底清单
// 建立权威来源。
//
// 在此之前 bundled 的 8 个能力位没有任何可核对的出处：端到端用例的 service 工厂
// 会无条件 replaceFromManifest 预置清单，WS 用例直接往 context 里塞 Lite 能力，
// 于是把 bundled 整个清空、或把 true/false 写反，测试依然全绿，而生产中第三方
// 请求的画像已经失效。这条用例让 bundled 与真实官方响应直接对账，是唯一能在
// 不依赖脚手架的前提下发现清单写错的守卫。
//
// 基线 fixture 来自 Codex CLI 0.145.0 的 /backend-api/codex/models 真实抓包；
// 后续画像升级新增的条目分别记在 codex_models_capabilities_0154_additions.json 与
// codex_models_capabilities_0163_additions.json（来源为官方内置 models-manager 目录），并额外核对
// reasoning 默认能力位。
// 官方新增模型或改变能力位时这条会失败——那是预期信号，应当同步更新 fixture
// 与 bundled，而不是放宽断言。
func TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest(t *testing.T) {
	type fixtureModel struct {
		Slug                              string  `json:"slug"`
		UseResponsesLite                  bool    `json:"use_responses_lite"`
		SupportsParallelToolCalls         bool    `json:"supports_parallel_tool_calls"`
		DefaultReasoningLevel             *string `json:"default_reasoning_level"`
		DefaultReasoningSummary           *string `json:"default_reasoning_summary"`
		SupportsReasoningSummaryParameter *bool   `json:"supports_reasoning_summary_parameter"`
	}
	loadFixture := func(path string) []fixtureModel {
		raw, err := os.ReadFile(path)
		require.NoError(t, err, "缺少官方 models 能力 fixture %s", path)
		var fixture struct {
			Models []fixtureModel `json:"models"`
		}
		require.NoError(t, json.Unmarshal(raw, &fixture))
		require.NotEmpty(t, fixture.Models, path)
		return fixture.Models
	}
	baseline := loadFixture("testdata/official_egress/codex_models_capabilities_0145.json")
	additions := loadFixture("testdata/official_egress/codex_models_capabilities_0154_additions.json")
	additions0163 := loadFixture("testdata/official_egress/codex_models_capabilities_0163_additions.json")

	expected := make(map[string]fixtureModel, len(baseline)+len(additions)+len(additions0163))
	for _, model := range baseline {
		expected[model.Slug] = model
	}
	for _, model := range additions {
		_, duplicated := expected[model.Slug]
		require.False(t, duplicated, "增量 fixture 不得重复基线模型 %s", model.Slug)
		expected[model.Slug] = model
	}
	for _, model := range additions0163 {
		_, duplicated := expected[model.Slug]
		require.False(t, duplicated, "增量 fixture 不得重复已有模型 %s", model.Slug)
		expected[model.Slug] = model
	}

	require.Len(t, bundledOpenAIModelCapabilities, len(expected),
		"bundled 清单的模型数量必须与官方基线抓包加本次增量一致")

	for slug, model := range expected {
		bundled, exists := bundledOpenAIModelCapabilities[slug]
		require.True(t, exists, "bundled 清单缺少官方模型 %s", slug)
		require.Equal(t, model.UseResponsesLite, bundled.UseResponsesLite,
			"模型 %s 的 use_responses_lite 与官方来源不一致", slug)
		require.Equal(t, model.SupportsParallelToolCalls, bundled.SupportsParallelToolCalls,
			"模型 %s 的 supports_parallel_tool_calls 与官方来源不一致", slug)
		if model.DefaultReasoningLevel != nil {
			require.Equal(t, *model.DefaultReasoningLevel, bundled.DefaultReasoningLevel,
				"模型 %s 的 default_reasoning_level 与官方内置目录不一致", slug)
		}
		if model.DefaultReasoningSummary != nil {
			require.Equal(t, *model.DefaultReasoningSummary, bundled.DefaultReasoningSummary,
				"模型 %s 的 default_reasoning_summary 与官方内置目录不一致", slug)
		}
		if model.SupportsReasoningSummaryParameter != nil {
			require.Equal(t, *model.SupportsReasoningSummaryParameter, bundled.SupportsReasoningSummaryParameter,
				"模型 %s 的 supports_reasoning_summary_parameter 与官方内置目录不一致", slug)
			require.True(t, bundled.ReasoningDefaultsKnown,
				"模型 %s 带 reasoning 默认值时 ReasoningDefaultsKnown 必须为 true", slug)
		}
	}
}

// TestOpenAIResponsesLiteCapabilityResolvesFromBundledWithoutManifest 验证不预置
// 任何账号 manifest 时，Lite 判定仍能从 bundled 得到正确结果——这正是第三方客户端
// 在生产中的真实处境：它们从不请求 /backend-api/codex/models，账号快照为空。
func TestOpenAIResponsesLiteCapabilityResolvesFromBundledWithoutManifest(t *testing.T) {
	service := &OpenAIGatewayService{}
	account := newOfficialOpenAIHTTPTestAccount(94)

	// 刻意不调用 replaceFromManifest：完全依赖 bundled。
	require.True(t,
		service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-5.6-sol"}`)),
		"Lite 模型在无账号 manifest 时必须仍判为 Lite")
	require.False(t,
		service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-5.4"}`)),
		"非 Lite 模型不得被误判")

	// 别名必须与真实 slug 得到同一结论，否则入站与出站定型会分裂。
	require.True(t,
		service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-5.6"}`)),
		"别名 gpt-5.6 必须解析到 gpt-5.6-sol 并判为 Lite")

	// 目标版本默认 Lite 模型：无账号 manifest（冷启动或 /models 退避期）时也必须
	// 判为 Lite，且 reasoning 默认能力位与远端真实清单一致（effort=medium，而非内置目录
	// 的离线兜底值 low），首条请求才不会与官方 wire 分裂；公开别名 gpt-6 同样解析到它。
	astra := service.resolveOpenAIModelCapabilities(account, []byte(`{"model":"gpt-6-astra"}`))
	require.True(t, astra.UseResponsesLite, "gpt-6-astra 在无账号 manifest 时必须判为 Lite")
	require.True(t, astra.ReasoningDefaultsKnown)
	require.Equal(t, "medium", astra.DefaultReasoningLevel)
	require.Equal(t, "none", astra.DefaultReasoningSummary)
	require.True(t, astra.SupportsReasoningSummaryParameter)
	require.True(t,
		service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-6"}`)),
		"别名 gpt-6 必须解析到 gpt-6-astra 并判为 Lite")
}

// 0.163 主线模型 gpt-6.1-sol 是 Lite 模型：无账号 manifest（冷启动或 /models 拉取失败退避期）时也必须判为 Lite，
// 否则会把它当未知模型判成非 Lite，出站丢失 Lite 头与 reasoning.context（gpt-6-astra 当年踩过同一个坑）。
// reasoning 默认能力位取官方内置目录值（effort=low）；真实账号清单的取值要等 VC-1 目录证据复核，复核若不同，
// 应同步修改 bundled 与 fixture，不要放宽这里的断言。
func TestOpenAIResponsesLiteCapabilityResolvesGPT61SolFromBundledWithoutManifest(t *testing.T) {
	service := &OpenAIGatewayService{}
	account := newOfficialOpenAIHTTPTestAccount(94)

	// 公开写法、大小写与 effort 后缀写法都必须落到同一个 slug 并得到同一组能力位。
	for _, model := range []string{"gpt-6.1-sol", "GPT-6.1-Sol", "gpt-6.1-sol-high", "gpt-6.1-sol-xhigh", "openai/gpt-6.1-sol"} {
		capabilities := service.resolveOpenAIModelCapabilities(account, []byte(`{"model":"`+model+`"}`))
		require.True(t, capabilities.UseResponsesLite, "%s 在无账号 manifest 时必须判为 Lite", model)
		require.True(t, capabilities.SupportsParallelToolCalls, model)
		require.True(t, capabilities.ReasoningDefaultsKnown, model)
		require.Equal(t, "low", capabilities.DefaultReasoningLevel, model)
		require.Equal(t, "none", capabilities.DefaultReasoningSummary, model)
		require.True(t, capabilities.SupportsReasoningSummaryParameter, model)
	}

	// 账号模型映射把别名映射到 gpt-6.1-sol 时，按映射后的真实模型查表。
	mapped := newOfficialOpenAIHTTPTestAccount(94)
	mapped.Credentials["model_mapping"] = map[string]any{"my-sol": "gpt-6.1-sol"}
	require.True(t, service.resolveOpenAIResponsesLiteCapability(mapped, []byte(`{"model":"my-sol"}`)),
		"映射到 gpt-6.1-sol 的别名必须判为 Lite")

	// 账号 manifest 接管后，以 manifest 为准（含 manifest 把它标成非 Lite 的情形），bundled 不再兜底。
	owned := &OpenAIGatewayService{}
	owned.openaiModelCapabilities.replaceFromManifest(94, []byte(
		`{"models":[{"slug":"gpt-6.1-sol","visibility":"list","use_responses_lite":false,"supports_parallel_tool_calls":true,"default_reasoning_level":"medium"}]}`,
	))
	fromManifest := owned.resolveOpenAIModelCapabilities(account, []byte(`{"model":"gpt-6.1-sol"}`))
	require.False(t, fromManifest.UseResponsesLite, "账号 manifest 的同名条目拥有更高优先级")
	require.Equal(t, "medium", fromManifest.DefaultReasoningLevel)

	// 旁证：已有 Lite 模型与非 Lite 模型的结论不受影响。
	require.True(t, service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-6-astra"}`)))
	require.False(t, service.resolveOpenAIResponsesLiteCapability(account, []byte(`{"model":"gpt-5.5"}`)))
}
