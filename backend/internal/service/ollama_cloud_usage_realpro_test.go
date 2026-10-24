package service

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// 真实 Pro 计划页面（已脱敏）：套餐徽章、合计余额、追加额度、月度额度（已用、总额、百分比）、
// 页面句子里的精确重置日期、最近请求。
func TestParseOllamaCloudUsageHTMLRealProPage(t *testing.T) {
	now := time.Date(2026, time.October, 24, 14, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt(readOllamaUsageTestdata(t, "ollama_settings_usage_pro_2026.html"), now)
	require.NoError(t, err)

	require.Equal(t, "Pro", data.Plan)
	require.Equal(t, "user@example.com", data.Email)
	// 余额是合计 $60；id 为 extra-usage-balance 的是追加额度 $0，不能当余额。
	require.Equal(t, "$60", data.Balance)
	require.Equal(t, "$0", data.AddedCredits)

	// 新版页面没有 5 小时 / 7 天窗口，也没有免费额度。
	require.Nil(t, data.FiveHour)
	require.Nil(t, data.SevenDay)
	require.Nil(t, data.FreeUsage)
	require.Empty(t, data.FreeModels)

	require.NotNil(t, data.Monthly)
	require.Equal(t, "$0", data.Monthly.UsedText)
	require.Equal(t, "$60", data.Monthly.LimitText)
	require.Equal(t, 0.0, data.Monthly.UsedPercent)
	// 重置时间取页面句子「Refills to $60 on November 24, 2026」，只有日期，按当天中午 UTC 记，不是约数。
	require.NotNil(t, data.Monthly.ResetAt)
	require.Equal(t, time.Date(2026, time.November, 24, 12, 0, 0, 0, time.UTC), *data.Monthly.ResetAt)
	require.False(t, data.Monthly.ResetApprox)
	require.Equal(t, "in 4 weeks", data.Monthly.ResetText)

	require.NotEmpty(t, data.RecentRequests)
	require.Equal(t, "gemma4:31b", data.RecentRequests[0].Model)
	require.Equal(t, "<$0.01", data.RecentRequests[0].Cost)
	require.NotNil(t, data.RecentRequests[0].At)
	require.Equal(t, time.Date(2026, time.October, 24, 12, 47, 5, 413281000, time.UTC), *data.RecentRequests[0].At)
}

// 自动充值弹窗里的 $5 / $20 / $100 / $500 选项与月上限输入不能被当成余额。
func TestParseOllamaCloudUsageHTMLRealProPageIgnoresDialogAmounts(t *testing.T) {
	data, err := parseOllamaCloudUsageHTMLAt(readOllamaUsageTestdata(t, "ollama_settings_usage_pro_2026.html"),
		time.Date(2026, time.October, 24, 14, 0, 0, 0, time.UTC))
	require.NoError(t, err)
	require.NotContains(t, []string{"$5", "$20", "$100", "$500"}, data.Balance)
	require.NotContains(t, []string{"$5", "$20", "$100", "$500"}, data.AddedCredits)
}

func TestParseOllamaTextDate(t *testing.T) {
	want := time.Date(2026, time.November, 24, 12, 0, 0, 0, time.UTC)
	for _, text := range []string{"November 24, 2026", "Nov 24 2026", "November 24th, 2026", "Nov. 24, 2026"} {
		got := parseOllamaTextDate(text)
		require.NotNil(t, got, text)
		require.Equal(t, want, *got, text)
	}
	require.Nil(t, parseOllamaTextDate("Smarch 24, 2026"))
	require.Nil(t, parseOllamaTextDate("November 2026"))
}
