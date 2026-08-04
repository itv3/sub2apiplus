//go:build unit

package service

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

func TestAdminService_EnsureOpenAIPrivacy_RetriesNonSuccessModes(t *testing.T) {
	t.Parallel()

	for _, mode := range []string{PrivacyModeFailed, PrivacyModeCFBlocked} {
		t.Run(mode, func(t *testing.T) {
			t.Parallel()

			privacyCalls := 0
			svc := &adminServiceImpl{
				accountRepo: &mockAccountRepoForGemini{},
				privacyClientFactory: func(request PrivacyClientRequest) (*PrivacyHTTPClient, error) {
					privacyCalls++
					return nil, errors.New("factory failed")
				},
			}

			account := &Account{
				ID:       101,
				Platform: PlatformOpenAI,
				Type:     AccountTypeOAuth,
				Credentials: map[string]any{
					"access_token": "token-1",
				},
				Extra: map[string]any{
					"privacy_mode": mode,
				},
			}

			got := svc.EnsureOpenAIPrivacy(context.Background(), account)

			require.Equal(t, PrivacyModeFailed, got)
			require.Equal(t, 1, privacyCalls)
		})
	}
}

func TestTokenRefreshService_ensureOpenAIPrivacy_RetriesNonSuccessModes(t *testing.T) {
	t.Parallel()

	cfg := &config.Config{
		TokenRefresh: config.TokenRefreshConfig{
			MaxRetries:          1,
			RetryBackoffSeconds: 0,
		},
	}

	for _, mode := range []string{PrivacyModeFailed, PrivacyModeCFBlocked} {
		t.Run(mode, func(t *testing.T) {
			t.Parallel()

			service := NewTokenRefreshService(&tokenRefreshAccountRepo{}, nil, nil, nil, nil, nil, nil, cfg, nil)
			privacyCalls := 0
			service.SetPrivacyDeps(func(request PrivacyClientRequest) (*PrivacyHTTPClient, error) {
				privacyCalls++
				return nil, errors.New("factory failed")
			}, nil)

			account := &Account{
				ID:       202,
				Platform: PlatformOpenAI,
				Type:     AccountTypeOAuth,
				Credentials: map[string]any{
					"access_token": "token-2",
				},
				Extra: map[string]any{
					"privacy_mode": mode,
				},
			}

			service.ensureOpenAIPrivacy(context.Background(), account)

			require.Equal(t, 1, privacyCalls)
		})
	}
}

func TestAdminServiceUpdateAccountPreservesManagedPrivacyExtra(t *testing.T) {
	retryAfter := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	repo := &longContextBillingRepoStub{account: &Account{
		ID:       301,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Extra: map[string]any{
			privacyModeExtraKey:       PrivacyModeCFBlocked,
			privacyRetryAfterExtraKey: retryAfter,
			privacyPersonaExtraKey:    "chrome_133_xhr",
			privacyRolloutKeyExtraKey: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"old_client_setting":      true,
		},
	}}
	svc := &adminServiceImpl{accountRepo: repo}

	updated, err := svc.UpdateAccount(context.Background(), 301, &UpdateAccountInput{Extra: map[string]any{
		privacyModeExtraKey:       PrivacyModeTrainingOff,
		privacyRetryAfterExtraKey: "2020-01-01T00:00:00Z",
		privacyPersonaExtraKey:    "client-controlled",
		privacyRolloutKeyExtraKey: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"new_client_setting":      true,
	}})

	require.NoError(t, err)
	require.Equal(t, PrivacyModeCFBlocked, updated.Extra[privacyModeExtraKey])
	require.Equal(t, retryAfter, updated.Extra[privacyRetryAfterExtraKey])
	require.Equal(t, "chrome_133_xhr", updated.Extra[privacyPersonaExtraKey])
	require.Equal(t, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", updated.Extra[privacyRolloutKeyExtraKey])
	require.Equal(t, true, updated.Extra["new_client_setting"])
	require.NotContains(t, updated.Extra, "old_client_setting",
		"普通 Extra 仍保持显式替换语义，只有受管 privacy 字段必须保留")
}

func TestAdminServiceManagedPrivacyUpdateIsAtomicWithCredentials(t *testing.T) {
	repo := &longContextBillingRepoStub{account: &Account{
		ID:          302,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Status:      StatusActive,
		Credentials: map[string]any{"access_token": "old-token"},
		Extra:       map[string]any{privacyRolloutKeyExtraKey: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
	}}
	svc := &adminServiceImpl{accountRepo: repo}
	retryAfter := time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)

	updated, err := svc.UpdateAccount(context.Background(), 302, &UpdateAccountInput{
		Credentials: map[string]any{"access_token": "new-token"},
		ManagedPrivacyExtra: map[string]any{
			privacyModeExtraKey:       PrivacyModeCFBlocked,
			privacyRetryAfterExtraKey: retryAfter,
			privacyPersonaExtraKey:    "chrome_133_xhr",
			privacyRolloutKeyExtraKey: "cccccccccccccccccccccccccccccccc",
		},
	})

	require.NoError(t, err)
	require.Equal(t, 1, repo.updateCalls,
		"Credentials 与完整 privacy Extra 必须通过同一次 repository Update 写入")
	require.Equal(t, "new-token", updated.Credentials["access_token"])
	require.Equal(t, PrivacyModeCFBlocked, updated.Extra[privacyModeExtraKey])
	require.Equal(t, retryAfter, updated.Extra[privacyRetryAfterExtraKey])
	require.Equal(t, "chrome_133_xhr", updated.Extra[privacyPersonaExtraKey])
	require.Equal(t, "cccccccccccccccccccccccccccccccc", updated.Extra[privacyRolloutKeyExtraKey])
}

func TestAdminServiceCreateAccountStripsClientManagedPrivacyExtra(t *testing.T) {
	repo := &longContextBillingRepoStub{}
	svc := &adminServiceImpl{accountRepo: repo}

	created, err := svc.CreateAccount(context.Background(), &CreateAccountInput{
		Name:                 "generic-openai-create",
		Platform:             PlatformOpenAI,
		Type:                 AccountTypeOAuth,
		SkipDefaultGroupBind: true,
		Extra: map[string]any{
			privacyModeExtraKey:       PrivacyModeTrainingOff,
			privacyRetryAfterExtraKey: "2099-01-01T00:00:00Z",
			privacyPersonaExtraKey:    "legacy_chrome_120",
			privacyRolloutKeyExtraKey: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"custom_setting":          true,
		},
	})

	require.NoError(t, err)
	require.Same(t, created, repo.createdAccount)
	for _, key := range openAIPrivacyManagedExtraKeys {
		require.NotContains(t, created.Extra, key,
			"通用 CreateAccount 的 Extra 不得预置受管 privacy 字段")
	}
	require.Equal(t, true, created.Extra["custom_setting"])
}

func TestAdminServiceCreateAccountAcceptsInternalManagedPrivacyExtra(t *testing.T) {
	repo := &longContextBillingRepoStub{}
	svc := &adminServiceImpl{accountRepo: repo}
	retryAfter := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)

	created, err := svc.CreateAccount(context.Background(), &CreateAccountInput{
		Name:                 "managed-openai-create",
		Platform:             PlatformOpenAI,
		Type:                 AccountTypeOAuth,
		SkipDefaultGroupBind: true,
		Extra: map[string]any{
			privacyRolloutKeyExtraKey: "client-controlled-key-is-ignored",
			"custom_setting":          true,
		},
		ManagedPrivacyExtra: map[string]any{
			privacyModeExtraKey:       PrivacyModeCFBlocked,
			privacyRetryAfterExtraKey: retryAfter,
			privacyPersonaExtraKey:    "chrome_133_xhr",
			privacyRolloutKeyExtraKey: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		},
	})

	require.NoError(t, err)
	require.Equal(t, PrivacyModeCFBlocked, created.Extra[privacyModeExtraKey])
	require.Equal(t, retryAfter, created.Extra[privacyRetryAfterExtraKey])
	require.Equal(t, "chrome_133_xhr", created.Extra[privacyPersonaExtraKey])
	require.Equal(t, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", created.Extra[privacyRolloutKeyExtraKey])
	require.Equal(t, true, created.Extra["custom_setting"])
}

func TestAdminServiceUpdateAccountExtraRejectsManagedPrivacyExtra(t *testing.T) {
	for _, key := range openAIPrivacyManagedExtraKeys {
		t.Run(key, func(t *testing.T) {
			repo := &longContextBillingRepoStub{account: &Account{ID: 1, Platform: PlatformOpenAI}}
			svc := &adminServiceImpl{accountRepo: repo}

			err := svc.UpdateAccountExtra(context.Background(), 1, map[string]any{key: "client-value"})

			require.Equal(t, http.StatusBadRequest, infraerrors.Code(err))
			require.Zero(t, repo.updateExtraCalls)
		})
	}
}

func TestAdminServiceBulkUpdateAccountsRejectsManagedPrivacyExtra(t *testing.T) {
	for _, key := range openAIPrivacyManagedExtraKeys {
		t.Run(key, func(t *testing.T) {
			repo := &longContextBillingRepoStub{}
			svc := &adminServiceImpl{accountRepo: repo}

			result, err := svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
				AccountIDs: []int64{1},
				Extra:      map[string]any{key: "client-value"},
			})

			require.Nil(t, result)
			require.Equal(t, http.StatusBadRequest, infraerrors.Code(err))
			require.Zero(t, repo.bulkUpdateCalls)
		})
	}
}

type openAIPrivacyCreateCFTransport struct {
	settingsCalls *atomic.Int32
}

func (t openAIPrivacyCreateCFTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if strings.Contains(request.URL.Path, "/settings/") {
		t.settingsCalls.Add(1)
	}
	return &http.Response{
		StatusCode: http.StatusServiceUnavailable,
		Status:     http.StatusText(http.StatusServiceUnavailable),
		Header:     http.Header{"Content-Type": []string{"text/html"}},
		Body:       io.NopCloser(strings.NewReader("Cloudflare: Just a moment")),
		Request:    request,
	}, nil
}

func TestLegacyExchangeThenGenericCreateCannotDoubleSendPrivacySettings(t *testing.T) {
	settingsCalls := &atomic.Int32{}
	repo := &longContextBillingRepoStub{}
	svc := &adminServiceImpl{
		accountRepo: repo,
		privacyClientFactory: func(PrivacyClientRequest) (*PrivacyHTTPClient, error) {
			client := req.NewClient()
			client.GetClient().Transport = openAIPrivacyCreateCFTransport{settingsCalls: settingsCalls}
			return &PrivacyHTTPClient{
				Client: client, Persona: "chrome_133_xhr", FailureCooldown: time.Hour,
			}, nil
		},
	}

	_, err := svc.CreateAccount(context.Background(), &CreateAccountInput{
		Name:                 "legacy-generic-create",
		Platform:             PlatformOpenAI,
		Type:                 AccountTypeOAuth,
		Credentials:          map[string]any{"access_token": "token-from-exchange"},
		SkipDefaultGroupBind: true,
	})
	require.NoError(t, err)
	require.Eventually(t, func() bool { return settingsCalls.Load() == 1 }, time.Second, 10*time.Millisecond)
	require.Never(t, func() bool { return settingsCalls.Load() > 1 }, 150*time.Millisecond, 10*time.Millisecond,
		"普通 exchange-code 已无 settings 副作用，通用创建后的 Ensure 最多只能发送一次")
}

func TestCreateAccountWithPersistedCooldownDoesNotImmediatelyRetryPrivacy(t *testing.T) {
	privacyFactoryCalls := &atomic.Int32{}
	repo := &longContextBillingRepoStub{}
	svc := &adminServiceImpl{
		accountRepo: repo,
		privacyClientFactory: func(PrivacyClientRequest) (*PrivacyHTTPClient, error) {
			privacyFactoryCalls.Add(1)
			return nil, errors.New("冷却期内不应创建 privacy 客户端")
		},
	}

	_, err := svc.CreateAccount(context.Background(), &CreateAccountInput{
		Name:                 "atomic-cooldown-create",
		Platform:             PlatformOpenAI,
		Type:                 AccountTypeOAuth,
		Credentials:          map[string]any{"access_token": "initial-token"},
		SkipDefaultGroupBind: true,
		ManagedPrivacyExtra: map[string]any{
			privacyModeExtraKey:       PrivacyModeCFBlocked,
			privacyRetryAfterExtraKey: time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			privacyPersonaExtraKey:    "chrome_133_xhr",
			privacyRolloutKeyExtraKey: "dddddddddddddddddddddddddddddddd",
		},
	})
	require.NoError(t, err)
	require.Never(t, func() bool { return privacyFactoryCalls.Load() > 0 }, 150*time.Millisecond, 10*time.Millisecond,
		"首次失败状态已随账号创建持久化，异步 Ensure 必须读取冷却并跳过")
}
