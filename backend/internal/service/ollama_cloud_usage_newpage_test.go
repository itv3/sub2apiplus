package service

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// ---- 新版（免费 / Pro）设置页解析与 Cookie 到期时间 ----

func readOllamaUsageTestdata(t *testing.T, name string) []byte {
	t.Helper()
	body, err := os.ReadFile("testdata/" + name)
	require.NoError(t, err)
	return body
}

// 真实免费计划页面（已脱敏）：套餐徽章、余额、免费额度、精确重置时间、免费模型清单、最近请求。
func TestParseOllamaCloudUsageHTMLRealFreePlanPage(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt(readOllamaUsageTestdata(t, "ollama_settings_usage_free_2026.html"), now)
	require.NoError(t, err)

	require.Equal(t, "Free", data.Plan)
	require.Equal(t, "user@example.com", data.Email)
	require.Equal(t, "$0", data.Balance)
	require.Nil(t, data.FiveHour)
	require.Nil(t, data.SevenDay)
	require.Nil(t, data.Monthly)

	require.NotNil(t, data.FreeUsage)
	require.Equal(t, 0.0, data.FreeUsage.UsedPercent)
	require.NotNil(t, data.FreeUsage.ResetAt)
	// 页面 local-time 元素带精确时间，不应按「2 weeks」相对 now 推算。
	require.Equal(t, time.Date(2026, time.November, 14, 5, 33, 24, 0, time.UTC), *data.FreeUsage.ResetAt)
	require.Equal(t, "in 2 weeks", data.FreeUsage.ResetText)
	require.False(t, data.FreeUsage.ResetApprox)

	require.Equal(t, []string{
		"beam", "gemma4:31b", "gpt-oss:120b", "gpt-oss:20b",
		"nemotron-3-nano:30b", "nemotron-3-super", "nemotron-3-ultra",
	}, data.FreeModels)

	// 「Models used this month」进度条分段：归入按月统计的模型请求。
	require.Equal(t, []OllamaCloudUsageModel{{Model: "gemma4:31b", Window: OllamaCloudUsageModelWindowMonthly, Requests: 53}}, data.Models)

	require.Len(t, data.RecentRequests, 25)
	first := data.RecentRequests[0]
	require.Equal(t, "21 minutes ago", first.Time)
	require.Equal(t, "gemma4:31b", first.Model)
	require.Equal(t, "<$0.01", first.Cost)
	require.NotNil(t, first.At)
	require.Equal(t, time.Date(2026, time.October, 24, 12, 47, 5, 413281000, time.UTC), *first.At)
}

// 合成的新版 Pro 页面：套餐徽章、$60 余额、月度额度（金额、进度条百分比、相对重置文案）、最近请求。
func TestParseOllamaCloudUsageHTMLSyntheticProPage(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt(readOllamaUsageTestdata(t, "ollama_settings_usage_pro_synthetic.html"), now)
	require.NoError(t, err)

	require.Equal(t, "Pro", data.Plan)
	require.Equal(t, "pro-user@example.com", data.Email)
	require.Equal(t, "$60", data.Balance)
	require.Nil(t, data.FiveHour)
	require.Nil(t, data.SevenDay)
	require.Nil(t, data.FreeUsage)
	require.Empty(t, data.FreeModels)

	require.NotNil(t, data.Monthly)
	require.Equal(t, "$12", data.Monthly.UsedText)
	// 外层进度条宽度 20%，内层分段宽度是相对已填充部分的比例，不能当总进度。
	require.Equal(t, 20.0, data.Monthly.UsedPercent)
	require.Equal(t, "in 4 weeks", data.Monthly.ResetText)
	require.NotNil(t, data.Monthly.ResetAt)
	// 没有精确时间，按相对文案推算（四周 = 28 天）。
	require.Equal(t, now.Add(28*24*time.Hour), *data.Monthly.ResetAt)
	require.True(t, data.Monthly.ResetApprox)

	require.Equal(t, []OllamaCloudUsageModel{
		{Model: "gemma4:31b", Window: OllamaCloudUsageModelWindowMonthly, Requests: 40},
		{Model: "gpt-oss:120b", Window: OllamaCloudUsageModelWindowMonthly, Requests: 9},
	}, data.Models)
	require.Len(t, data.RecentRequests, 2)
	require.Equal(t, "28 minutes ago", data.RecentRequests[0].Time)
	require.Equal(t, "gemma4:31b", data.RecentRequests[0].Model)
	require.Equal(t, "<$0.01", data.RecentRequests[0].Cost)
	require.Equal(t, "$0.01", data.RecentRequests[1].Cost)
}

// 月度重置有精确 data-time 时取精确值；读不到进度条时按金额近似；没有月度行则无窗口。
func TestParseOllamaCloudUsageHTMLMonthlyResetAndFallbacks(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt([]byte(`
		<div><h2>Usage credits<span>Pro</span></h2><span id="extra-usage-balance">$60</span></div>
		<div class="local-time" data-time="2026-11-24T00:00:00Z">Monthly credits refill in 4 weeks</div>
		<div><span>Monthly credits used</span><span>$0</span></div>
		<div data-usage-meter><div data-usage-track><div style="width: 0%"></div></div></div>`), now)
	require.NoError(t, err)
	require.NotNil(t, data.Monthly)
	require.Equal(t, "$0", data.Monthly.UsedText)
	require.Equal(t, 0.0, data.Monthly.UsedPercent)
	require.Equal(t, "in 4 weeks", data.Monthly.ResetText)
	require.NotNil(t, data.Monthly.ResetAt)
	require.Equal(t, time.Date(2026, time.November, 24, 0, 0, 0, 0, time.UTC), *data.Monthly.ResetAt)
	require.False(t, data.Monthly.ResetApprox)

	// 读不到进度条：已用非 0 则按 已用/(已用+余额) 近似。
	data, err = parseOllamaCloudUsageHTMLAt([]byte(`
		<div><h2>Usage credits<span>Pro</span></h2><span id="extra-usage-balance">$48</span></div>
		<div><span>Monthly credits used</span><span>$12</span></div>`), now)
	require.NoError(t, err)
	require.NotNil(t, data.Monthly)
	require.InDelta(t, 20.0, data.Monthly.UsedPercent, 0.001)
	require.Nil(t, data.Monthly.ResetAt)

	// 没有任何月度行：不产生窗口。
	data, err = parseOllamaCloudUsageHTMLAt([]byte(`<div><h2>Usage credits<span>Pro</span></h2><span id="extra-usage-balance">$60</span></div>`), now)
	require.NoError(t, err)
	require.Nil(t, data.Monthly)
}

// 套餐徽章的几种常见 DOM 写法，以及说明句不能被误当徽章。
func TestParseOllamaCloudUsageHTMLPlanBadgeVariants(t *testing.T) {
	for name, markup := range map[string]string{
		"badge inside heading":   `<h2>Usage credits<span>free</span></h2><span>$0</span>`,
		"badge as sibling":       `<div><span>Usage credits</span><span>Free</span></div><div>$0 Add funds</div>`,
		"plain text after label": `<div>Usage credits Free $0 Add funds</div>`,
	} {
		t.Run(name, func(t *testing.T) {
			data, err := parseOllamaCloudUsageHTML([]byte(markup))
			require.NoError(t, err)
			require.Equal(t, "Free", data.Plan)
			require.Equal(t, "$0", data.Balance)
		})
	}
	// 说明句不是徽章：套餐为空。
	data, err := parseOllamaCloudUsageHTML([]byte(`<p>Usage credits can be used with any cloud model $5</p>`))
	require.NoError(t, err)
	require.Empty(t, data.Plan)
}

// 免费额度窗口：百分比文本优先，进度条宽度兜底，说明句不当标签。
func TestParseOllamaCloudUsageHTMLFreeUsageVariants(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	data, err := parseOllamaCloudUsageHTMLAt([]byte(`
		<p>Free usage credits can be used with the following cloud models: beam</p>
		<div><span>Free usage</span><span>12.5% used</span></div>`), now)
	require.NoError(t, err)
	require.NotNil(t, data.FreeUsage)
	require.Equal(t, 12.5, data.FreeUsage.UsedPercent)

	data, err = parseOllamaCloudUsageHTMLAt([]byte(`
		<div><span>Free usage</span></div>
		<div data-usage-meter><div style="width: 37%"></div></div>
		<div>Refills in 3 days.</div>`), now)
	require.NoError(t, err)
	require.NotNil(t, data.FreeUsage)
	require.Equal(t, 37.0, data.FreeUsage.UsedPercent)
	require.Equal(t, "in 3 days", data.FreeUsage.ResetText)
	require.NotNil(t, data.FreeUsage.ResetAt)
	require.Equal(t, now.Add(72*time.Hour), *data.FreeUsage.ResetAt)
	require.True(t, data.FreeUsage.ResetApprox)

	// 只有说明句时没有免费额度窗口。
	data, err = parseOllamaCloudUsageHTMLAt([]byte(`<p>Free usage credits can be used with the following cloud models: beam</p><p>Balance remaining $3</p>`), now)
	require.NoError(t, err)
	require.Nil(t, data.FreeUsage)
	require.Equal(t, []string{"beam"}, data.FreeModels)
}

// 免费模型清单：清单在说明句同一元素或紧随的兄弟元素；遇到非模型词即停。
func TestParseOllamaCloudUsageHTMLFreeModelsVariants(t *testing.T) {
	data, err := parseOllamaCloudUsageHTML([]byte(`<p>Free usage credits can be used with the following cloud models: beam, gemma4:31b, and more. Add usage credits to pay.</p>`))
	require.NoError(t, err)
	require.Equal(t, []string{"beam", "gemma4:31b"}, data.FreeModels)

	data, err = parseOllamaCloudUsageHTML([]byte(`<div><p>Free usage credits can be used with the following cloud models:</p><ul><li><a>beam</a></li><li><a>gpt-oss:20b</a></li></ul><p>Upgrade now</p></div>`))
	require.NoError(t, err)
	require.Equal(t, []string{"beam", "gpt-oss:20b"}, data.FreeModels)
}

// 旧版样本不得出现新字段，既有解析行为保持不变。
func TestParseOllamaCloudUsageHTMLLegacyFixtureHasNoNewFields(t *testing.T) {
	data, err := parseOllamaCloudUsageHTML(ollamaUsageFixture(t))
	require.NoError(t, err)
	require.Nil(t, data.FreeUsage)
	require.Nil(t, data.Monthly)
	require.Empty(t, data.FreeModels)
	require.Empty(t, data.RecentRequests)
	require.Empty(t, data.Email)
}

func TestParseOllamaRelativeTime(t *testing.T) {
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	for text, want := range map[string]time.Duration{
		"in 4 weeks":        28 * 24 * time.Hour,
		"in about 2 hours":  2 * time.Hour,
		"in an hour":        time.Hour,
		"in 1 month":        30 * 24 * time.Hour,
		"Refills in 3 days": 72 * time.Hour,
	} {
		got := parseOllamaRelativeTime(text, now)
		require.NotNil(t, got, text)
		require.Equal(t, now.Add(want), *got, text)
	}
	require.Nil(t, parseOllamaRelativeTime("soon", now))
	require.Nil(t, parseOllamaRelativeTime("in 0 days", now))
}

// 邮箱只取页头账号菜单，不扫全页。
func TestParseOllamaAccountEmailOnlyFromAccountMenu(t *testing.T) {
	data, err := parseOllamaCloudUsageHTML([]byte(`
		<footer>Contact support@ollama.example</footer>
		<details id="settings-account-menu"><summary>name</summary><div>me@example.com</div></details>
		<div>Balance remaining $1</div>`))
	require.NoError(t, err)
	require.Equal(t, "me@example.com", data.Email)

	data, err = parseOllamaCloudUsageHTML([]byte(`<footer>Contact support@ollama.example</footer><div>Balance remaining $1</div>`))
	require.NoError(t, err)
	require.Empty(t, data.Email)
}

// 解析不到邮箱时，刷新沿用上次读到的值。
func TestOllamaCloudUsageRefreshKeepsPreviousEmailWhenPageHasNone(t *testing.T) {
	account := ollamaUsageAccount(31)
	account.Extra[OllamaCloudUsageSessionExtraKey] = "cipher:wos-session=initial"
	repo := &ollamaUsageTestRepo{upstreamBillingProbeAccountRepo: &upstreamBillingProbeAccountRepo{accounts: map[int64]*Account{31: account}}}
	upstream := &ollamaUsageHTTPStub{body: readOllamaUsageTestdata(t, "ollama_settings_usage_free_2026.html")}
	svc := newOllamaUsageTestService(t, repo, upstream, &upstreamBillingProbeSettingRepo{}, true)
	now := time.Date(2026, time.October, 24, 13, 0, 0, 0, time.UTC)
	svc.now = func() time.Time { return now }

	state, err := svc.Refresh(context.Background(), 31)
	require.NoError(t, err)
	require.Equal(t, "user@example.com", state.Snapshot.Data.Email)
	require.Equal(t, "Free", state.Snapshot.Data.Plan)

	// 第二次页面没有账号菜单（只剩旧版可识别字段）：邮箱沿用上次读到的值。
	upstream.body = ollamaUsageFixture(t)
	now = now.Add(time.Hour)
	state, err = svc.Refresh(context.Background(), 31)
	require.NoError(t, err)
	require.Equal(t, "user@example.com", state.Snapshot.Data.Email)
	require.Equal(t, "max", state.Snapshot.Data.Plan)
}

func TestOllamaCloudUsageSessionExpiryLifecycle(t *testing.T) {
	account := ollamaUsageAccount(32)
	repo := &ollamaUsageTestRepo{upstreamBillingProbeAccountRepo: &upstreamBillingProbeAccountRepo{accounts: map[int64]*Account{32: account}}}
	svc := newOllamaUsageTestService(t, repo, &ollamaUsageHTTPStub{}, &upstreamBillingProbeSettingRepo{}, true)

	// 未配置会话时不能单独设置到期时间。
	expiry := time.Date(2026, time.December, 1, 8, 30, 15, 999, time.FixedZone("UTC+8", 8*3600))
	_, err := svc.SetSessionExpiry(context.Background(), 32, &expiry)
	require.ErrorIs(t, err, ErrOllamaCloudUsageSessionRequired)

	// 保存 Cookie 并带到期时间：转 UTC、截到秒，状态可回显。
	state, err := svc.SaveSession(context.Background(), 32, "wos-session=abc", &expiry)
	require.NoError(t, err)
	require.True(t, state.Configured)
	require.NotNil(t, state.SessionExpiresAt)
	require.Equal(t, time.Date(2026, time.December, 1, 0, 30, 15, 0, time.UTC), *state.SessionExpiresAt)

	// 只更新到期时间，不重填 Cookie：会话密文保持不变。
	cipherBefore := account.Extra[OllamaCloudUsageSessionExtraKey]
	next := time.Date(2027, time.January, 5, 0, 0, 0, 0, time.UTC)
	state, err = svc.SetSessionExpiry(context.Background(), 32, &next)
	require.NoError(t, err)
	require.Equal(t, next, *state.SessionExpiresAt)
	require.Equal(t, cipherBefore, account.Extra[OllamaCloudUsageSessionExtraKey])

	// 清除到期时间。
	state, err = svc.SetSessionExpiry(context.Background(), 32, nil)
	require.NoError(t, err)
	require.Nil(t, state.SessionExpiresAt)

	// 离谱年份被拒绝。
	tooOld := time.Date(1999, time.January, 1, 0, 0, 0, 0, time.UTC)
	_, err = svc.SetSessionExpiry(context.Background(), 32, &tooOld)
	require.Error(t, err)

	// 重新保存 Cookie 但不填到期时间：旧到期时间随旧会话清除。
	_, err = svc.SetSessionExpiry(context.Background(), 32, &next)
	require.NoError(t, err)
	state, err = svc.SaveSession(context.Background(), 32, "wos-session=def", nil)
	require.NoError(t, err)
	require.Nil(t, state.SessionExpiresAt)

	// 删除会话一并清除到期时间。
	_, err = svc.SetSessionExpiry(context.Background(), 32, &next)
	require.NoError(t, err)
	state, err = svc.DeleteSession(context.Background(), 32)
	require.NoError(t, err)
	require.False(t, state.Configured)
	require.Nil(t, state.SessionExpiresAt)
	require.NotContains(t, account.Extra, OllamaCloudUsageSessionExpiresAtExtraKey)
}

func TestOllamaCloudUsageSessionExpiryOnlyEchoedWithSession(t *testing.T) {
	account := ollamaUsageAccount(33)
	account.Extra[OllamaCloudUsageSessionExtraKey] = "cipher:wos-session=abc"
	account.Extra[OllamaCloudUsageSessionExpiresAtExtraKey] = "2026-12-01T00:00:00Z"
	require.NotNil(t, OllamaCloudUsageStateFromAccount(account).SessionExpiresAt)
	// 没有会话时不回显到期时间。
	delete(account.Extra, OllamaCloudUsageSessionExtraKey)
	require.Nil(t, OllamaCloudUsageStateFromAccount(account).SessionExpiresAt)
	require.True(t, accountHasOllamaCloudUsageExtra(&Account{Extra: map[string]any{OllamaCloudUsageSessionExpiresAtExtraKey: "x"}}))
}
