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
// fixture 来自 Codex CLI 0.145.0 的 /backend-api/codex/models 真实抓包。
// 官方新增模型或改变能力位时这条会失败——那是预期信号，应当同步更新 fixture
// 与 bundled，而不是放宽断言。
func TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest(t *testing.T) {
	raw, err := os.ReadFile("testdata/official_egress/codex_models_capabilities_0145.json")
	require.NoError(t, err, "缺少官方 models 能力 fixture")

	var fixture struct {
		Models []struct {
			Slug                      string `json:"slug"`
			UseResponsesLite          bool   `json:"use_responses_lite"`
			SupportsParallelToolCalls bool   `json:"supports_parallel_tool_calls"`
		} `json:"models"`
	}
	require.NoError(t, json.Unmarshal(raw, &fixture))
	require.NotEmpty(t, fixture.Models)

	require.Len(t, bundledOpenAIModelCapabilities, len(fixture.Models),
		"bundled 清单的模型数量必须与官方抓包一致")

	for _, model := range fixture.Models {
		bundled, exists := bundledOpenAIModelCapabilities[model.Slug]
		require.True(t, exists, "bundled 清单缺少官方模型 %s", model.Slug)
		require.Equal(t, model.UseResponsesLite, bundled.UseResponsesLite,
			"模型 %s 的 use_responses_lite 与官方抓包不一致", model.Slug)
		require.Equal(t, model.SupportsParallelToolCalls, bundled.SupportsParallelToolCalls,
			"模型 %s 的 supports_parallel_tool_calls 与官方抓包不一致", model.Slug)
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
}
