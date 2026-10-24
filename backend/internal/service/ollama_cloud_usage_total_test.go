package service

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// 余额取「合计」（标题行右侧主金额），id 为 extra-usage-balance 的元素是「追加额度」。
func TestParseOllamaCloudUsageHTMLBalanceIsTotalNotAddedCredits(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt([]byte(`
		<div id="extra-usage">
			<div class="flex"><h2>Usage credits<span>pro</span></h2><span class="text-xl">$60</span></div>
			<p>Monthly credits refill soon</p>
			<span id="extra-usage-balance">$0</span>
			<button>Add funds</button>
		</div>
		<div><span>Monthly credits used</span><span>$5</span></div>`), now)
	require.NoError(t, err)
	require.Equal(t, "Pro", data.Plan)
	require.Equal(t, "$60", data.Balance)
	require.Equal(t, "$0", data.AddedCredits)
	// 月度已用进度条按 已用/(已用+合计) 近似，用的是合计而不是追加额度。
	require.NotNil(t, data.Monthly)
	require.InDelta(t, 5.0/65.0*100, data.Monthly.UsedPercent, 0.001)

	// 页面文本里明确写着 Total 时以它为准。
	data, err = parseOllamaCloudUsageHTMLAt([]byte(`
		<h2>Usage credits<span>Pro</span></h2><span id="extra-usage-balance">$0</span>
		<div>Monthly credits $60 Added credits $0 Total $60</div>`), now)
	require.NoError(t, err)
	require.Equal(t, "$60", data.Balance)
	require.Equal(t, "$0", data.AddedCredits)

	// 标题行之后其他区块的金额（月度已用）不能当合计：回退到追加额度元素。
	data, err = parseOllamaCloudUsageHTMLAt([]byte(`
		<div><h2>Usage credits<span>Pro</span></h2><span id="extra-usage-balance">$48</span></div>
		<div><span>Monthly credits used</span><span>$12</span></div>`), now)
	require.NoError(t, err)
	require.Equal(t, "$48", data.Balance)
	require.Equal(t, "$48", data.AddedCredits)
}

// 真实免费页面：主金额就是 extra-usage-balance，合计与追加额度同为 $0。
func TestParseOllamaCloudUsageHTMLRealFreePageBalanceAndAdded(t *testing.T) {
	data, err := parseOllamaCloudUsageHTMLAt(readOllamaUsageTestdata(t, "ollama_settings_usage_free_2026.html"),
		time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC))
	require.NoError(t, err)
	require.Equal(t, "$0", data.Balance)
	require.Equal(t, "$0", data.AddedCredits)
}

// 额度重置时间：文案由脚本生成时服务端 HTML 里只有时间属性；排除最近请求表里的时间。
func TestParseOllamaCloudUsageHTMLMonthlyResetFromFutureTimeAttribute(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	want := time.Date(2026, time.November, 24, 0, 0, 0, 0, time.UTC)

	for name, resetMarkup := range map[string]string{
		"empty element carrying data-time":  `<span class="local-time" data-time="2026-11-24T00:00:00Z"></span>`,
		"icon inside the refill sentence":   `<p>Monthly credits refill in 4 weeks <svg data-time="2026-11-24T00:00:00Z"></svg></p>`,
		"time element":                      `<time datetime="2026-11-24T00:00:00Z"></time>`,
		"dedicated reset attribute":         `<div data-reset-at="2026-11-24T00:00:00Z"></div>`,
		"refill sentence wraps the element": `<p>Monthly credits refill <span data-time="2026-11-24T00:00:00Z">in 4 weeks</span></p>`,
	} {
		t.Run(name, func(t *testing.T) {
			data, err := parseOllamaCloudUsageHTMLAt([]byte(`
				<div><h2>Usage credits<span>pro</span></h2><span>$60</span>`+resetMarkup+`</div>
				<div><span>Monthly credits used</span><span>$0</span></div>
				<div data-usage-meter><div style="width: 0%"></div></div>
				<section id="recent-requests-self"><h2>Recent requests</h2><table><tbody>
					<tr><td data-time="2026-10-24T12:47:05Z">21 minutes ago</td><td><div>gemma4:31b</div></td><td>&lt;$0.01</td></tr>
					<tr><td data-time="2026-12-30T00:00:00Z">just now</td><td><div>other</div></td><td>$0.01</td></tr>
				</tbody></table></section>`), now)
			require.NoError(t, err)
			require.NotNil(t, data.Monthly)
			require.NotNil(t, data.Monthly.ResetAt)
			require.Equal(t, want, *data.Monthly.ResetAt)
			require.False(t, data.Monthly.ResetApprox)
		})
	}

	// 过去的时间、最近请求里的时间都不当作重置时间；只有相对文案时仍按约数推算。
	data, err := parseOllamaCloudUsageHTMLAt([]byte(`
		<div><h2>Usage credits<span>pro</span></h2><span>$60</span><p>Monthly credits refill in 2 weeks</p><span data-time="2026-01-01T00:00:00Z"></span></div>
		<div><span>Monthly credits used</span><span>$0</span></div>
		<table><tr><td data-time="2026-12-30T00:00:00Z">just now</td><td>m</td><td>$1</td></tr></table>`), now)
	require.NoError(t, err)
	require.NotNil(t, data.Monthly.ResetAt)
	require.Equal(t, now.Add(14*24*time.Hour), *data.Monthly.ResetAt)
	require.True(t, data.Monthly.ResetApprox)
}
