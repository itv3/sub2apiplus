//go:build unit

package service

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// widthLimitedUsageLogRepo 模拟 usage_logs.request_id 的 varchar(64) 列宽：
// 超长就像真实数据库那样报错，整条用量记录写不进去。
type widthLimitedUsageLogRepo struct {
	UsageLogRepository
	logs []*UsageLog
}

func (r *widthLimitedUsageLogRepo) Create(_ context.Context, log *UsageLog) (bool, error) {
	if len(log.RequestID) > 64 {
		return false, errors.New("pq: value too long for type character varying(64)")
	}
	r.logs = append(r.logs, log)
	return true, nil
}

func TestCapUsageLogRequestID(t *testing.T) {
	// 未超长原样返回，包括恰好 64 个字符。
	for _, id := range []string{"resp_short", "client:00000000-0000-0000-0000-000000000000", strings.Repeat("a", 64)} {
		require.Equal(t, id, capUsageLogRequestID(id))
	}

	long := "resp_" + strings.Repeat("x", 120)
	capped := capUsageLogRequestID(long)
	require.LessOrEqual(t, len(capped), 64)
	require.True(t, strings.HasPrefix(capped, "resp_xxxx"), "保留可读前缀")
	require.Contains(t, capped, "~")
	// 确定性：同一个 ID 永远得到同一个 request_id（幂等写入与计费去重依赖它）。
	require.Equal(t, capped, capUsageLogRequestID(long))
	// 前缀相同、仅尾部不同的超长 ID 不会撞键。
	other := "resp_" + strings.Repeat("x", 119) + "y"
	require.NotEqual(t, capped, capUsageLogRequestID(other))
	// 65 个字符也要收敛。
	require.LessOrEqual(t, len(capUsageLogRequestID(strings.Repeat("a", 65))), 64)

	// 多字节字符不会被截成非法 UTF-8。
	multibyte := strings.Repeat("响应", 60)
	cappedMultibyte := capUsageLogRequestID(multibyte)
	require.LessOrEqual(t, len(cappedMultibyte), 64)
	require.True(t, strings.ToValidUTF8(cappedMultibyte, "") == cappedMultibyte)
}

func TestUsageUpstreamRequestIDForCappedRequestID(t *testing.T) {
	// 没有收敛时不写，保持既有行为。
	require.Nil(t, usageUpstreamRequestIDForCappedRequestID("resp_short", "resp_short"))
	// 收敛时写完整原始 ID，按 upstream_request_id 的列宽（128）截断。
	original := "resp_" + strings.Repeat("z", 200)
	got := usageUpstreamRequestIDForCappedRequestID(original, capUsageLogRequestID(original))
	require.NotNil(t, got)
	require.Len(t, *got, 128)
	require.True(t, strings.HasPrefix(original, *got))
	// 不超过 128 的原始 ID 完整保留，可按原 ID 直接追溯。
	mid := "resp_" + strings.Repeat("m", 90)
	got = usageUpstreamRequestIDForCappedRequestID(mid, capUsageLogRequestID(mid))
	require.NotNil(t, got)
	require.Equal(t, mid, *got)
}

// 回归 ARM64 现场：WS 轮次以上游响应 ID 作 request_id，Command Code 的响应 ID 超过 64 个字符，
// 整条用量记录写不进 varchar(64)，而计费去重（varchar(255)）已经扣费。收敛之后用量记录写入成功，
// 计费去重键与用量记录的 request_id 一致，完整响应 ID 另存 upstream_request_id。
func TestOpenAIGatewayServiceRecordUsage_OversizedWSResponseIDStillRecorded(t *testing.T) {
	longID := "resp_" + strings.Repeat("c", 90)
	for _, platform := range []string{PlatformCommandCode, PlatformGrok, PlatformOpenAI, PlatformCustom, PlatformDeepseek} {
		t.Run(platform, func(t *testing.T) {
			usageRepo := &widthLimitedUsageLogRepo{}
			userRepo := &openAIRecordUsageUserRepoStub{}
			billingRepo := &openAIRecordUsageBillingRepoStub{result: &UsageBillingApplyResult{Applied: true}}
			svc := newOpenAIRecordUsageServiceForTest(usageRepo, userRepo, &openAIRecordUsageSubRepoStub{}, nil)
			svc.usageBillingRepo = billingRepo
			group := &Group{ID: 1, Platform: platform, RateMultiplier: 1.1}

			err := svc.RecordUsage(context.Background(), &OpenAIRecordUsageInput{
				Result: &OpenAIForwardResult{
					RequestID: longID, OpenAIWSMode: true, Stream: true,
					Model: "gpt-5.1", BillingModel: "gpt-5.1", UpstreamModel: "gpt-5.1",
					Usage: OpenAIUsage{InputTokens: 100, OutputTokens: 20}, Duration: time.Second,
				},
				APIKey:  &APIKey{ID: 10, GroupID: &group.ID, Group: group},
				User:    &User{ID: 20},
				Account: &Account{ID: 30, Platform: platform, Type: AccountTypeAPIKey},
			})

			require.NoError(t, err)
			require.Len(t, usageRepo.logs, 1, "用量记录必须写入成功")
			log := usageRepo.logs[0]
			require.LessOrEqual(t, len(log.RequestID), 64)
			require.Equal(t, capUsageLogRequestID(longID), log.RequestID)
			require.True(t, log.OpenAIWSMode)
			require.NotNil(t, log.UpstreamRequestID)
			require.Equal(t, longID, *log.UpstreamRequestID, "完整响应 ID 另存，可按原 ID 追溯")
			require.NotNil(t, billingRepo.lastCmd)
			require.Equal(t, log.RequestID, billingRepo.lastCmd.RequestID, "计费去重键与用量记录一致")
		})
	}
}

// 不超长的 WS 响应 ID（官方 OpenAI 的常见情形）与 HTTP 路径的既有取法完全不变。
func TestOpenAIGatewayServiceRecordUsage_ShortWSResponseIDUnchanged(t *testing.T) {
	usageRepo := &widthLimitedUsageLogRepo{}
	svc := newOpenAIRecordUsageServiceForTest(usageRepo, &openAIRecordUsageUserRepoStub{}, &openAIRecordUsageSubRepoStub{}, nil)
	group := &Group{ID: 1, Platform: PlatformOpenAI, RateMultiplier: 1.1}
	shortID := "resp_" + strings.Repeat("a", 48)

	err := svc.RecordUsage(context.Background(), &OpenAIRecordUsageInput{
		Result: &OpenAIForwardResult{
			RequestID: shortID, OpenAIWSMode: true,
			Model: "gpt-5.1", BillingModel: "gpt-5.1", UpstreamModel: "gpt-5.1",
			Usage: OpenAIUsage{InputTokens: 10, OutputTokens: 2}, Duration: time.Second,
		},
		APIKey:  &APIKey{ID: 10, GroupID: &group.ID, Group: group},
		User:    &User{ID: 20},
		Account: &Account{ID: 30, Platform: PlatformOpenAI, Type: AccountTypeAPIKey},
	})

	require.NoError(t, err)
	require.Len(t, usageRepo.logs, 1)
	require.Equal(t, shortID, usageRepo.logs[0].RequestID)
	require.Nil(t, usageRepo.logs[0].UpstreamRequestID, "WS 轮次没有 HTTP 响应头：未收敛时保持 nil")
}
