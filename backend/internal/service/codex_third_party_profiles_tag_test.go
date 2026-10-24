//go:build unit

package service

// 能力档案匹配对 Ollama 风格 `名称:标签` 写法的处理：精确匹配优先，匹配不到时去掉非参数规模的标签再匹配。

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// matchAllBuiltin 只用内置规则（不带型号别名）为一组上游型号找档案。
func matchAllBuiltin(set *codexThirdPartyProfileSet, models []string) (*codexThirdPartyProfile, string) {
	profile, reason, _ := set.matchAllWithAliases(models, nil)
	return profile, reason
}

func loadTestProfileSet(t *testing.T) *codexThirdPartyProfileSet {
	t.Helper()
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	return set
}

func TestCodexThirdPartyProfileMatchStripsOllamaVersionTags(t *testing.T) {
	set := loadTestProfileSet(t)
	exact := set.match("deepseek-v4.1-flash")
	require.NotNil(t, exact)
	require.Equal(t, "deepseek-flash", exact.ID)

	for _, model := range []string{
		"deepseek-v4.1-flash:1017",
		"deepseek-v4.1-flash:latest",
		"deepseek-v4.1-flash:cloud",
		"DeepSeek-V4.1-Flash:Cloud",
		"deepseek/deepseek-v4.1-flash:1017", // 厂商前缀加标签
		"  deepseek-v4.1-flash:1017  ",
	} {
		profile := set.match(model)
		require.NotNil(t, profile, model)
		require.Equal(t, exact, profile, model)
	}

	// matchAll：不同账号的原始名（带标签、带前缀、不带标签）落在同一档案上不算冲突。
	profile, reason := matchAllBuiltin(set, []string{"deepseek-flash", "deepseek/deepseek-v4.1-flash", "deepseek-v4.1-flash:1017"})
	require.NotNil(t, profile, reason)
	require.Equal(t, "deepseek-flash", profile.ID)
}

func TestCodexThirdPartyProfileMatchKeepsParameterSizeTags(t *testing.T) {
	set := loadTestProfileSet(t)
	for _, model := range []string{
		"deepseek-v4.1-flash:31b", "deepseek-v4.1-flash:120b", "deepseek-v4.1-flash:0.6b",
		"deepseek-v4.1-flash:70m", "deepseek-v4.1-flash:8B", "deepseek-v4.1-flash:1.5T", "deepseek-v4.1-flash:32k",
		"gemma4:31b",
	} {
		require.Nil(t, set.match(model), "参数规模标签不剥离，不得误配到别的档案: %s", model)
	}
	// 无标签、空标签、孤立冒号：不剥离。
	for _, model := range []string{"", ":1017", "deepseek-v4.1-flash:", "unknown-model", "unknown-model:1017", "unknown-model:latest"} {
		require.Nil(t, set.match(model), model)
	}

	// 仍匹配不上时，原因文案保持不变，且回显原始型号（含标签）。
	profile, reason := matchAllBuiltin(set, []string{"unknown-model:1017"})
	require.Nil(t, profile)
	require.Equal(t, "上游型号 unknown-model:1017 暂无能力档案（待补能力）", reason)
	profile, reason = matchAllBuiltin(set, []string{"gemma4:31b"})
	require.Nil(t, profile)
	require.Equal(t, "上游型号 gemma4:31b 暂无能力档案（待补能力）", reason)
}

func TestCodexThirdPartyStripOllamaTag(t *testing.T) {
	cases := []struct {
		key  string
		want string
		ok   bool
	}{
		{"name:1017", "name", true},
		{"name:latest", "name", true},
		{"name:cloud", "name", true},
		{"a:b:c", "a:b", true}, // 只去最后一个冒号及其后缀
		{"name:31b", "", false},
		{"name:120B", "", false},
		{"name:0.6b", "", false},
		{"name:70m", "", false},
		{"name:2t", "", false},
		{"name:7k", "", false},
		{"name:v1b", "name", true}, // 不是“纯数字加单位”形态
		{"name:", "", false},
		{":tag", "", false},
		{"name", "", false},
	}
	for _, tc := range cases {
		got, ok := codexThirdPartyStripOllamaTag(tc.key)
		require.Equal(t, tc.ok, ok, tc.key)
		require.Equal(t, tc.want, got, tc.key)
	}
}

func TestCodexThirdPartyProfileExactAliasWithTagWinsOverStripping(t *testing.T) {
	// 档案别名本身带标签：精确匹配优先，不被剥离后的基础名抢走。
	tagged := &codexThirdPartyProfile{ID: "tagged", UpstreamModels: []string{"foo:bar"}}
	base := &codexThirdPartyProfile{ID: "base", UpstreamModels: []string{"foo"}}
	set := &codexThirdPartyProfileSet{profiles: []*codexThirdPartyProfile{base, tagged}}
	require.Equal(t, tagged, set.match("foo:bar"))
	require.Equal(t, tagged, set.match("vendor/FOO:BAR"))
	require.Equal(t, base, set.match("foo"))
	// 放宽到去标签后，两份档案的别名规范化结果相同（都是 foo）：有歧义，宁可不匹配。
	require.Nil(t, set.match("foo:other"))
	// 没有歧义时才剥离：只有 base 一份档案时 foo:other 命中它。
	require.Equal(t, base, (&codexThirdPartyProfileSet{profiles: []*codexThirdPartyProfile{base}}).match("foo:other"))
}

// DMIT 形态：Composite 分组里 CommandCode（按模型分流，目录声明支持 Responses）、DeepSeek 官方、
// custom 平台的 Ollama 账号（把公开名映射到 `名称:1017`）都精确声明同一个公开模型，三者都是 Responses 零转换。
// 修复前 `:1017` 认不出档案，整组停在「暂无能力档案（待补能力）」，已开启的模型被撤下目录。
func TestCodexTPDMITShapeWithOllamaTaggedCustomAccountIsConfigurable(t *testing.T) {
	baseURL := fmt.Sprintf("http://cc-tag-%d.example/provider/v1", time.Now().UnixNano())
	commandCode := compositePoolWithMapping(cxtpPoolCommandCode(11, 2, baseURL), map[string]any{compositePoolModel: "deepseek/deepseek-v4.1-flash"})
	cxtpPoolWarmCatalog(t, &commandCode, baseURL, []string{APIProtocolChatCompletions, APIProtocolResponses})
	custom := compositePoolWithMapping(compositePoolCustom(12, 3, customFullURLs()), map[string]any{compositePoolModel: "deepseek-v4.1-flash:1017"})
	custom.Name = "Ollama"
	accounts := []Account{
		cxtpPoolNamed(compositePoolAdaptive(10, PlatformDeepseek, 1), "DeepSeek"),
		commandCode,
		custom,
	}
	svc := cxtpPoolService(t, accounts)
	svc.fetcher = openAICodexBaseFetcher{svc: &OpenAIGatewayService{httpUpstream: &recordingHTTPUpstream{}}}

	eval, err := svc.EvaluateCodexThirdPartyCatalog(WithCodexThirdPartyOfflineEvaluation(context.Background()), cxtpCompositeGroup(), NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	candidate, ok := findCodexCandidate(eval.Candidates, compositePoolModel)
	require.True(t, ok)
	require.Equal(t, CodexModelStatusConfigurable, candidate.Status, "原因：%v", candidate.Reasons)
	require.True(t, candidate.CanEnable)
	require.Equal(t, []int64{10, 11, 12}, candidate.AccountIDs)
	require.Equal(t, "deepseek-flash", candidate.ProfileID)
	require.Contains(t, candidate.UpstreamModel, "deepseek-v4.1-flash:1017")
	require.NotContains(t, candidate.Reasons, "上游型号 deepseek-v4.1-flash:1017 暂无能力档案（待补能力）")
}
