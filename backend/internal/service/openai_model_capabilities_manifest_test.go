package service

import (
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// bundled 与该 fixture 的能力位一致性由 TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest
// 单独守卫，这里只复用同一份实抓清单验证合并语义。
func readCapturedCodexModelsManifest(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile("testdata/official_egress/codex_models_capabilities_0145.json")
	require.NoError(t, err, "缺少官方 models 能力 fixture")
	return body
}

// 清单含 visibility=list 时对齐官方 apply_remote_models：远端整体接管。
// 清单未列出的 slug 走 fallback 能力位（两个都是 false）并视为已知，不再回落 bundled。
func TestModelCapabilitiesRemoteAuthoritativeManifestDropsBundledFallback(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	snapshot.replaceFromManifest(90, readCapturedCodexModelsManifest(t))
	now := time.Now()

	lite, known, _ := snapshot.modelCapabilitiesState(90, "gpt-5.6-luna", now)
	require.True(t, known)
	require.True(t, lite.UseResponsesLite)
	require.True(t, lite.SupportsParallelToolCalls)

	// 上游把某个 bundled 里存在的 Lite 模型下线后，官方客户端按 fallback ModelInfo 处理。
	trimmed := []byte(`{"models":[{"slug":"gpt-5.5","visibility":"list","use_responses_lite":false,"supports_parallel_tool_calls":true}]}`)
	snapshot.replaceFromManifest(90, trimmed)
	value, known, _ := snapshot.modelCapabilitiesState(90, "gpt-5.6-luna", now)
	require.True(t, known, "远端接管后缺失 slug 属于已知不支持，而非未知")
	require.False(t, value.UseResponsesLite, "远端清单已下线该模型，不得回落 bundled 的 Lite 旧值")
	require.False(t, value.SupportsParallelToolCalls)
}

// 清单里没有任何 visibility=list 时不满足官方接管条件，保持 bundled 打底的旧行为。
func TestModelCapabilitiesNonAuthoritativeManifestKeepsBundledFallback(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	hiddenOnly := []byte(`{"models":[{"slug":"codex-auto-review","visibility":"hide","use_responses_lite":false,"supports_parallel_tool_calls":true}]}`)
	snapshot.replaceFromManifest(90, hiddenOnly)

	value, known, _ := snapshot.modelCapabilitiesState(90, "gpt-5.6-luna", time.Now())
	require.True(t, known)
	require.True(t, value.UseResponsesLite, "未满足接管条件时 bundled 仍应兜底")
}

// manifest 从未成功加载时（解析失败或空清单）继续用 bundled，
// 等价于官方刷新失败后保留内存中已装载的清单。
func TestModelCapabilitiesUnloadedManifestKeepsBundledFallback(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	snapshot.replaceFromManifest(90, []byte(`{"models":[]}`))

	value, known, _ := snapshot.modelCapabilitiesState(90, "gpt-5.6-luna", time.Now())
	require.True(t, known)
	require.True(t, value.UseResponsesLite)
}

// 远端接管时不得以 bundled 为基底做字段级合并：官方是整条替换，
// 清单里省略 supports_parallel_tool_calls 就应落到零值而非继承 bundled 的 true。
func TestModelCapabilitiesRemoteAuthoritativeDoesNotInheritBundledFields(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	partial := []byte(`{"models":[{"slug":"gpt-5.6-luna","visibility":"list","use_responses_lite":true}]}`)
	snapshot.replaceFromManifest(90, partial)

	value, known, _ := snapshot.modelCapabilitiesState(90, "gpt-5.6-luna", time.Now())
	require.True(t, known)
	require.True(t, value.UseResponsesLite)
	require.False(t, value.SupportsParallelToolCalls, "远端接管时不得继承 bundled 的并行能力位")
}
