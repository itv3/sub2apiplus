//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

// customBulkRecorder 记录批量更新的每一次仓储调用（既有桩只保留最后一次）。
type customBulkRecorder struct {
	*accountRepoStubForBulkUpdate
	calls []customBulkCall
}

type customBulkCall struct {
	ids     []int64
	updates AccountBulkUpdate
}

func (r *customBulkRecorder) BulkUpdate(ctx context.Context, ids []int64, updates AccountBulkUpdate) (int64, error) {
	r.calls = append(r.calls, customBulkCall{ids: append([]int64{}, ids...), updates: updates})
	return r.accountRepoStubForBulkUpdate.BulkUpdate(ctx, ids, updates)
}

func customCreateInput(platform, accountType string, credentials map[string]any) *CreateAccountInput {
	return &CreateAccountInput{
		Name:                  "custom-create",
		Platform:              platform,
		Type:                  accountType,
		Credentials:           credentials,
		Concurrency:           1,
		SkipDefaultGroupBind:  true,
		SkipMixedChannelCheck: true,
	}
}

func TestNormalizeCustomPlatformCredentials(t *testing.T) {
	t.Run("只填 Chat Completions 时 base_url 归一为同值", func(t *testing.T) {
		credentials := map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "  https://ollama.com/v1  "},
			"base_url":      "https://stale.example.com",
		}
		require.NoError(t, NormalizeCustomPlatformCredentials(PlatformCustom, AccountTypeAPIKey, credentials))
		require.Equal(t, "https://ollama.com/v1", credentials["base_url"])
	})

	t.Run("缺 base_url 字段时补齐", func(t *testing.T) {
		credentials := map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
		}
		require.NoError(t, NormalizeCustomPlatformCredentials(PlatformCustom, AccountTypeAPIKey, credentials))
		require.Equal(t, "https://ollama.com/v1", credentials["base_url"])
	})

	rejects := []struct {
		name        string
		accountType string
		credentials map[string]any
		reason      string
	}{
		{"缺 Chat Completions 地址", AccountTypeAPIKey, map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolResponses: "https://ollama.com/v1"},
			"base_url":      "https://ollama.com/v1",
		}, "CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED"},
		{"Chat Completions 地址为空白", AccountTypeAPIKey, map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "  "},
		}, "CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED"},
		{"完全没有凭据", AccountTypeAPIKey, nil, "CUSTOM_PLATFORM_API_PROTOCOL_INVALID"},
		{"协议不是 adaptive", AccountTypeAPIKey, map[string]any{
			"api_protocol":  APIProtocolChatCompletions,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
		}, "CUSTOM_PLATFORM_API_PROTOCOL_INVALID"},
		{"类型不是 API Key", AccountTypeOAuth, map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
		}, "CUSTOM_PLATFORM_ACCOUNT_TYPE_INVALID"},
	}
	for _, tc := range rejects {
		t.Run(tc.name, func(t *testing.T) {
			err := NormalizeCustomPlatformCredentials(PlatformCustom, tc.accountType, tc.credentials)
			requireApplicationErrorReason(t, err, tc.reason)
		})
	}

	t.Run("其它平台不受影响", func(t *testing.T) {
		credentials := map[string]any{"base_url": "https://keep.example.com"}
		require.NoError(t, NormalizeCustomPlatformCredentials(PlatformOpenAI, AccountTypeOAuth, credentials))
		require.Equal(t, map[string]any{"base_url": "https://keep.example.com"}, credentials)
	})
}

// 创建：缺 Chat Completions 地址被拒；只填该地址时 base_url 归一为同值。导入逐条走
// CreateAccount，复用同一条校验。
func TestAdminCreateAndImportCustomAccountValidation(t *testing.T) {
	repo := &accountRepoStubForBulkUpdate{createID: 11}
	svc := &adminServiceImpl{accountRepo: repo}

	_, err := svc.CreateAccount(context.Background(), customCreateInput(PlatformCustom, AccountTypeAPIKey, map[string]any{
		"api_key":       "sk-x",
		"api_protocol":  APIProtocolAdaptive,
		"api_base_urls": map[string]any{APIProtocolResponses: "https://ollama.com/v1"},
	}))
	requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED")
	require.Nil(t, repo.createAccount, "被拒的账号不得落库")

	_, err = svc.CreateAccount(context.Background(), customCreateInput(PlatformCustom, AccountTypeOAuth, map[string]any{
		"api_protocol":  APIProtocolAdaptive,
		"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
	}))
	requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_ACCOUNT_TYPE_INVALID")

	_, err = svc.CreateAccount(context.Background(), customCreateInput(PlatformCustom, AccountTypeAPIKey, map[string]any{
		"api_key":       "sk-x",
		"api_protocol":  APIProtocolChatCompletions,
		"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
	}))
	requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_API_PROTOCOL_INVALID")

	account, err := svc.CreateAccount(context.Background(), customCreateInput(PlatformCustom, AccountTypeAPIKey, map[string]any{
		"api_key":       "sk-x",
		"api_protocol":  APIProtocolAdaptive,
		"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
	}))
	require.NoError(t, err)
	require.Equal(t, int64(11), account.ID)
	require.Equal(t, "https://ollama.com/v1", repo.createAccount.Credentials["base_url"])
	// 用量识别只读旧字段 base_url：归一后识别正常。
	require.True(t, IsOllamaCloudUsageAccount(repo.createAccount))

	// 其它平台创建不受影响（没有 api_base_urls 也照常通过）。
	repo.createAccount = nil
	_, err = svc.CreateAccount(context.Background(), customCreateInput(PlatformOpenAI, AccountTypeAPIKey, map[string]any{"api_key": "sk-o"}))
	require.NoError(t, err)
	require.Equal(t, PlatformOpenAI, repo.createAccount.Platform)
	_, hasBaseURL := repo.createAccount.Credentials["base_url"]
	require.False(t, hasBaseURL)
}

// 单个更新：按「现有凭据 + 本次提交」合并后的完整凭据校验，不是只看增量。
func TestAdminUpdateCustomAccountValidatesMergedCredentials(t *testing.T) {
	newRepo := func() *accountRepoStubForBulkUpdate {
		return &accountRepoStubForBulkUpdate{getByIDAccounts: map[int64]*Account{
			7: {
				ID: 7, Platform: PlatformCustom, Type: AccountTypeAPIKey, Status: StatusActive, Extra: map[string]any{},
				Credentials: map[string]any{
					"api_key":       "sk-old",
					"api_protocol":  APIProtocolAdaptive,
					"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
					"base_url":      "https://ollama.com/v1",
				},
			},
		}}
	}

	t.Run("提交里清掉 Chat Completions 地址被拒", func(t *testing.T) {
		repo := newRepo()
		svc := &adminServiceImpl{accountRepo: repo}
		_, err := svc.UpdateAccount(context.Background(), 7, &UpdateAccountInput{Credentials: map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolResponses: "https://ollama.com/v1"},
			"base_url":      "https://ollama.com/v1",
		}})
		requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED")
		require.Empty(t, repo.updatedAccounts)
	})

	t.Run("提交里改协议被拒", func(t *testing.T) {
		repo := newRepo()
		svc := &adminServiceImpl{accountRepo: repo}
		_, err := svc.UpdateAccount(context.Background(), 7, &UpdateAccountInput{Credentials: map[string]any{
			"api_protocol":  APIProtocolAnthropic,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://ollama.com/v1"},
		}})
		requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_API_PROTOCOL_INVALID")
	})

	t.Run("改类型被拒", func(t *testing.T) {
		repo := newRepo()
		svc := &adminServiceImpl{accountRepo: repo}
		_, err := svc.UpdateAccount(context.Background(), 7, &UpdateAccountInput{Type: AccountTypeOAuth})
		requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_ACCOUNT_TYPE_INVALID")
	})

	t.Run("换 Chat Completions 地址时 base_url 同步归一", func(t *testing.T) {
		repo := newRepo()
		svc := &adminServiceImpl{accountRepo: repo}
		_, err := svc.UpdateAccount(context.Background(), 7, &UpdateAccountInput{Credentials: map[string]any{
			"api_protocol":  APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://relay.example.com/v1"},
			"base_url":      "https://stale.example.com",
		}})
		require.NoError(t, err)
		require.Len(t, repo.updatedAccounts, 1)
		require.Equal(t, "https://relay.example.com/v1", repo.updatedAccounts[0].Credentials["base_url"])
		// 敏感键沿用既有合并语义：未提供就保留。
		require.Equal(t, "sk-old", repo.updatedAccounts[0].Credentials["api_key"])
	})

	t.Run("只改名称不触发凭据校验", func(t *testing.T) {
		repo := newRepo()
		svc := &adminServiceImpl{accountRepo: repo}
		_, err := svc.UpdateAccount(context.Background(), 7, &UpdateAccountInput{Name: "renamed"})
		require.NoError(t, err)
	})
}

// 批量更新：凭据按 JSONB 顶层键合并且目标可能混合平台。custom 账号按合并后的完整凭据
// 校验并归一，其它平台账号的写入完全不变。
func TestAdminBulkUpdateCustomAccountsNormalizesOnlyCustom(t *testing.T) {
	customAccount := func(id int64, chatURL string) *Account {
		return &Account{
			ID: id, Platform: PlatformCustom, Type: AccountTypeAPIKey,
			Credentials: map[string]any{
				"api_key":       "sk-" + chatURL,
				"api_protocol":  APIProtocolAdaptive,
				"api_base_urls": map[string]any{APIProtocolChatCompletions: chatURL},
				"base_url":      chatURL,
			},
		}
	}

	t.Run("混合平台：增量带 api_base_urls 时只给 custom 追加 base_url", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			customAccount(1, "https://a.example.com/v1"),
			{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: map[string]any{"api_key": "sk-o"}},
			customAccount(3, "https://b.example.com/v1"),
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}
		patch := map[string]any{"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://new.example.com/v1"}}

		result, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{AccountIDs: []int64{1, 2, 3}, Credentials: patch})

		require.NoError(t, err)
		require.Equal(t, 3, result.Success)
		require.Len(t, recorder.calls, 2)
		// 非 custom 账号：沿用原增量，一次写入，不带 base_url。
		require.Equal(t, []int64{2}, recorder.calls[0].ids)
		require.NotContains(t, recorder.calls[0].updates.Credentials, "base_url")
		require.Contains(t, recorder.calls[0].updates.Credentials, "api_base_urls")
		// custom 账号：base_url 归一为同值。
		require.Equal(t, []int64{1, 3}, recorder.calls[1].ids)
		require.Equal(t, "https://new.example.com/v1", recorder.calls[1].updates.Credentials["base_url"])
		require.Contains(t, recorder.calls[1].updates.Credentials, "api_base_urls")
		// base_url 变化意味着上游身份变化，探测快照随之失效。
		require.Contains(t, recorder.calls[1].updates.Extra, UpstreamBillingProbeExtraKey)
		// 入参增量没有被改写。
		require.NotContains(t, patch, "base_url")
	})

	t.Run("增量只带 base_url：各 custom 账号按自己的 Chat Completions 地址归一", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			customAccount(1, "https://a.example.com/v1"),
			customAccount(3, "https://b.example.com/v1"),
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}

		_, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
			AccountIDs:  []int64{1, 3},
			Credentials: map[string]any{"base_url": "https://wrong.example.com"},
		})

		require.NoError(t, err)
		require.Len(t, recorder.calls, 2)
		require.Equal(t, []int64{1}, recorder.calls[0].ids)
		require.Equal(t, "https://a.example.com/v1", recorder.calls[0].updates.Credentials["base_url"])
		require.Equal(t, []int64{3}, recorder.calls[1].ids)
		require.Equal(t, "https://b.example.com/v1", recorder.calls[1].updates.Credentials["base_url"])
	})

	t.Run("增量把 Chat Completions 地址清掉：整体拒绝且不写库", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			customAccount(1, "https://a.example.com/v1"),
			{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeAPIKey},
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}

		result, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
			AccountIDs:  []int64{1, 2},
			Credentials: map[string]any{"api_base_urls": map[string]any{APIProtocolResponses: "https://x.example.com/v1"}},
		})

		require.Nil(t, result)
		requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED")
		require.Empty(t, recorder.calls)
	})

	t.Run("增量把协议改成非 adaptive：整体拒绝", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			customAccount(1, "https://a.example.com/v1"),
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}

		_, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
			AccountIDs:  []int64{1},
			Credentials: map[string]any{"api_protocol": APIProtocolResponses},
		})

		requireApplicationErrorReason(t, err, "CUSTOM_PLATFORM_API_PROTOCOL_INVALID")
		require.Empty(t, recorder.calls)
	})

	t.Run("没有 custom 目标：与原行为一致，只调用一次", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeAPIKey},
			{ID: 4, Platform: PlatformDeepseek, Type: AccountTypeAPIKey},
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}

		_, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
			AccountIDs:  []int64{2, 4},
			Credentials: map[string]any{"base_url": "https://relay.example.com"},
		})

		require.NoError(t, err)
		require.Len(t, recorder.calls, 1)
		require.Equal(t, []int64{2, 4}, recorder.calls[0].ids)
		require.Equal(t, map[string]any{"base_url": "https://relay.example.com"}, recorder.calls[0].updates.Credentials)
	})

	t.Run("不改凭据的批量更新不校验 custom 账号", func(t *testing.T) {
		recorder := &customBulkRecorder{accountRepoStubForBulkUpdate: &accountRepoStubForBulkUpdate{getByIDsAccounts: []*Account{
			{ID: 1, Platform: PlatformCustom, Type: AccountTypeAPIKey, Credentials: map[string]any{}},
		}}}
		svc := &adminServiceImpl{accountRepo: recorder}
		priority := 5

		_, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{AccountIDs: []int64{1}, Priority: &priority})

		require.NoError(t, err)
		require.Len(t, recorder.calls, 1)
	})
}
