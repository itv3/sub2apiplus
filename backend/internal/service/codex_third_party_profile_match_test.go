//go:build unit

package service

// 能力档案匹配的逐级规范化单测：精确 → 去 Ollama 标签 → 下划线 → 去末尾日期后缀（含 -latest）。

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestCodexThirdPartyProfileMatchNormalizesUnderscoreAndDateSuffix(t *testing.T) {
	set := loadTestProfileSet(t)
	exact := set.match("deepseek-v4.1-flash")
	require.NotNil(t, exact)

	for _, model := range []string{
		// 下划线写法
		"deepseek_v4.1_flash",
		"DeepSeek_V4.1_Flash",
		"deepseek-v4.1_flash",
		// 日期后缀：-MMDD、-YYMMDD、-YYYYMMDD、-YYYY-MM-DD、@ 加数字
		"deepseek-v4.1-flash-0925",
		"deepseek-v4.1-flash-250925",
		"deepseek-v4.1-flash-20250925",
		"deepseek-v4.1-flash-2025-09-25",
		"deepseek-v4.1-flash@20250925",
		"deepseek-v4.1-flash@0925",
		"deepseek-v4.1-flash@2025-09-25",
		// -latest 视同 :latest
		"deepseek-v4.1-flash-latest",
		// 与厂商前缀、Ollama 标签、下划线、日期组合
		"deepseek/deepseek-v4.1-flash-0925",
		"deepseek/deepseek-v4.1-flash-20250925:cloud",
		"deepseek/deepseek_v4.1_flash-2025-09-25:1017",
		"vendor/DeepSeek_V4.1_Flash@20250925",
		"deepseek_v4.1_flash-latest",
	} {
		require.Equal(t, exact, set.match(model), model)
	}
}

func TestCodexThirdPartyProfileMatchDoesNotStripCapabilityChangingSuffixes(t *testing.T) {
	set := loadTestProfileSet(t)
	for _, model := range []string{
		// 参数规模
		"deepseek-v4.1-flash-8b", "deepseek-v4.1-flash-70b", "deepseek-v4.1-flash:31b",
		// 版本号
		"deepseek-v4.1-flash-v4", "deepseek-v4.1-flash-4o", "deepseek-v4.1-flash-3.5",
		// 会改变能力的后缀
		"deepseek-v4.1-flash-preview", "deepseek-v4.1-flash-instruct", "deepseek-v4.1-flash-mini",
		// 不是合法日期的数字后缀（月份 13、日 00、年份 21xx）
		"deepseek-v4.1-flash-1325", "deepseek-v4.1-flash-0900", "deepseek-v4.1-flash-21250925",
		"deepseek-v4.1-flash-2025-13-01",
		// 长度不是 4、6、8 位的纯数字
		"deepseek-v4.1-flash-925", "deepseek-v4.1-flash-12345",
	} {
		require.Nil(t, set.match(model), "不应剥离: %s", model)
	}
	// 只剥末尾一段：日期后面还有会改变能力的后缀时不剥。
	require.Nil(t, set.match("deepseek-v4.1-flash-0925-preview"))
	require.Nil(t, set.match("deepseek-v4.1-flash-0925-mini"))
	// 剥完名字不能为空。
	require.Nil(t, set.match("-latest"))
	require.Nil(t, set.match("-0925"))
	require.Nil(t, set.match("@20250925"))
}

func TestCodexThirdPartyStripDateSuffix(t *testing.T) {
	cases := map[string]string{
		"name-0925":                  "name",
		"name-250925":                "name",
		"name-20250925":              "name",
		"name-2025-09-25":            "name",
		"name@20250925":              "name",
		"name@2025-09-25":            "name",
		"name-latest":                "name",
		"name-8b":                    "name-8b",
		"name-v4":                    "name-v4",
		"name-4o":                    "name-4o",
		"name-3.5":                   "name-3.5",
		"name-preview":               "name-preview",
		"name-0925-preview":          "name-0925-preview",
		"name-1325":                  "name-1325",
		"name@bad":                   "name@bad",
		"-latest":                    "-latest",
		"-0925":                      "-0925",
		"name-0925-0926":             "name-0925", // 只剥最后一段
		"gpt-4-1106":                 "gpt-4",
		"claude-3-5-sonnet-20241022": "claude-3-5-sonnet",
	}
	for input, want := range cases {
		require.Equal(t, want, codexThirdPartyStripDateSuffix(input), input)
	}
}

func TestCodexThirdPartyProfileMatchPrefersEarlierLevels(t *testing.T) {
	// 命中即止：精确 > 去标签 > 下划线 > 日期后缀。每一级都同时作用于档案别名一侧。
	dated := &codexThirdPartyProfile{ID: "dated", UpstreamModels: []string{"foo-0925"}}
	plain := &codexThirdPartyProfile{ID: "plain", UpstreamModels: []string{"foo"}}
	underscored := &codexThirdPartyProfile{ID: "underscored", UpstreamModels: []string{"bar_baz"}}
	hyphen := &codexThirdPartyProfile{ID: "hyphen", UpstreamModels: []string{"bar-baz"}}
	set := &codexThirdPartyProfileSet{profiles: []*codexThirdPartyProfile{plain, dated, underscored, hyphen}}

	require.Equal(t, dated, set.match("foo-0925"), "别名本身带日期：精确优先")
	// 放宽到去日期后，dated 与 plain 的别名规范化结果相同（都是 foo）：有歧义，宁可不匹配。
	require.Nil(t, set.match("foo-0926"))
	require.Equal(t, plain, (&codexThirdPartyProfileSet{profiles: []*codexThirdPartyProfile{plain}}).match("foo-0926"), "没有精确别名且无歧义才去日期")
	require.Equal(t, dated, set.match("foo-0925:cloud"), "先去标签得到 foo-0925，命中带日期的别名，不再继续去日期")
	require.Equal(t, underscored, set.match("bar_baz"))
	require.Equal(t, hyphen, set.match("bar-baz"))
	// 放宽后同时落在多份不同档案上：宁可不匹配。
	require.Nil(t, set.match("bar_baz-0925"), "日期级别 bar-baz 同时命中 underscored 与 hyphen")
}
