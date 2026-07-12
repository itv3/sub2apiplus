package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/stretchr/testify/require"
)

func newAnthropicAPIKeyMimicTLSAccount() *Account {
	return &Account{
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
			"enable_tls_fingerprint":             true,
		},
	}
}

func TestResolveAnthropicTLSProfileForRequestUsesCapturedMimicDefault(t *testing.T) {
	account := newAnthropicAPIKeyMimicTLSAccount()

	profile := resolveAnthropicTLSProfileForRequest(account, true, nil)

	require.NotNil(t, profile)
	require.Contains(t, profile.Name, "Claude CLI 2.1.207")
	require.Equal(t, []string{"http/1.1"}, profile.ALPNProtocols)
	require.Equal(t, []uint16{0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21}, profile.Extensions)
	require.Contains(t, profile.Extensions, uint16(21))
	require.NotContains(t, profile.Extensions, uint16(65037))
}

func TestResolveAnthropicTLSProfileForRequestRequiresCurrentMimicAndTLSOptIn(t *testing.T) {
	account := newAnthropicAPIKeyMimicTLSAccount()

	// mimic=false 表示本次请求被识别为官方客户端或未命中 mimic，不能应用专用指纹。
	require.Nil(t, resolveAnthropicTLSProfileForRequest(account, false, &TLSFingerprintProfileService{}))

	account.Extra["enable_tls_fingerprint"] = false
	require.Nil(t, resolveAnthropicTLSProfileForRequest(account, true, &TLSFingerprintProfileService{}))

	account.Extra["enable_tls_fingerprint"] = true
	account.Extra["anthropic_apikey_mimic_claude_code"] = false
	require.Nil(t, resolveAnthropicTLSProfileForRequest(account, true, &TLSFingerprintProfileService{}))
}

func TestResolveAnthropicTLSProfileForRequestHonorsAdministratorProfile(t *testing.T) {
	account := newAnthropicAPIKeyMimicTLSAccount()
	tlsService := &TLSFingerprintProfileService{
		localCache: map[int64]*model.TLSFingerprintProfile{
			42: {ID: 42, Name: "administrator-fixed-profile", Extensions: []uint16{0, 16, 43}},
		},
	}

	account.Extra["tls_fingerprint_profile_id"] = int64(42)
	profile := resolveAnthropicTLSProfileForRequest(account, true, tlsService)
	require.NotNil(t, profile)
	require.Equal(t, "administrator-fixed-profile", profile.Name)
	require.Equal(t, []uint16{0, 16, 43}, profile.Extensions)

	account.Extra["tls_fingerprint_profile_id"] = int64(-1)
	profile = resolveAnthropicTLSProfileForRequest(account, true, tlsService)
	require.NotNil(t, profile)
	require.Equal(t, "administrator-fixed-profile", profile.Name)
}

func TestResolveAnthropicTLSProfileForRequestDoesNotReplaceMissingExplicitProfile(t *testing.T) {
	account := newAnthropicAPIKeyMimicTLSAccount()
	account.Extra["tls_fingerprint_profile_id"] = int64(42)

	profile := resolveAnthropicTLSProfileForRequest(account, true, &TLSFingerprintProfileService{})

	require.NotNil(t, profile)
	require.Equal(t, "Built-in Default (Node.js 24.x)", profile.Name)
}

func TestResolveAnthropicTLSProfileForRequestKeepsOAuthResolution(t *testing.T) {
	account := &Account{
		Platform: PlatformAnthropic,
		Type:     AccountTypeOAuth,
		Extra: map[string]any{
			"enable_tls_fingerprint": true,
		},
	}

	profile := resolveAnthropicTLSProfileForRequest(account, false, &TLSFingerprintProfileService{})

	require.NotNil(t, profile)
	require.Equal(t, "Built-in Default (Node.js 24.x)", profile.Name)
}
