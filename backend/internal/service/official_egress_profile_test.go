package service

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestOfficialEgressT2_OAuthAccountAutomaticallyAttachesHTTPProfile(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{
		ID:       50,
		Platform: PlatformAnthropic,
		Type:     AccountTypeOAuth,
		Extra: map[string]any{
			"official_egress_enabled":         false,
			"official_egress_profile_version": "deprecated-version",
		},
	}
	upstreamRequest := httptest.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages", nil)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)

	got, err := attachOfficialEgressHTTPContext(upstreamRequest, ingressContext, account, PlatformAnthropic)
	require.NoError(t, err)
	require.NotSame(t, upstreamRequest, got)
	egressContext, exists := OfficialEgressContextFromContext(got.Context())
	require.True(t, exists)
	require.Equal(t, "2.1.220", egressContext.ProfileVersion())
}

func TestOfficialEgressT2_APIKeyAccountKeepsHTTPRequestUntouched(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{
		ID:       50,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra:    map[string]any{},
	}
	upstreamRequest := httptest.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages", nil)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)

	got, err := attachOfficialEgressHTTPContext(upstreamRequest, ingressContext, account, PlatformAnthropic)
	require.NoError(t, err)
	require.Same(t, upstreamRequest, got)
	_, exists := OfficialEgressContextFromContext(got.Context())
	require.False(t, exists)
}

func TestOfficialEgressN1_NonModelHTTPRouteKeepsRequestUntouched(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := officialEgressTestAccount(94, PlatformOpenAI)
	upstreamRequest := httptest.NewRequest(
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		nil,
	)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)

	got, err := attachOfficialEgressHTTPContext(
		upstreamRequest,
		ingressContext,
		account,
		PlatformOpenAI,
	)
	require.NoError(t, err)
	require.Same(t, upstreamRequest, got)
	_, exists := OfficialEgressContextFromContext(got.Context())
	require.False(t, exists)
}

func TestOfficialEgressT2_ResolverRejectsInvalidHostAndConflictingState(t *testing.T) {
	account := officialEgressTestAccount(50, PlatformAnthropic)
	resolver := DefaultOfficialEgressProfileResolver{}

	wrongHost := newOfficialEgressTestContext(account, OfficialEgressTransportHTTP, "/v1/messages", "relay.example.com")
	_, err := resolver.ResolveHTTPProfile(wrongHost, account, "/v1/messages")
	require.ErrorContains(t, err, "rejected Anthropic upstream host")

	wrongAccount := newOfficialEgressTestContext(account, OfficialEgressTransportHTTP, "/v1/messages", "api.anthropic.com")
	otherAccount := officialEgressTestAccount(51, PlatformAnthropic)
	_, err = resolver.ResolveHTTPProfile(wrongAccount, otherAccount, "/v1/messages")
	require.ErrorContains(t, err, "account state conflicts")

	wrongEndpoint := newOfficialEgressTestContext(account, OfficialEgressTransportHTTP, "/v1/messages", "api.anthropic.com")
	_, err = resolver.ResolveHTTPProfile(wrongEndpoint, account, "/v1/responses")
	require.ErrorContains(t, err, "endpoint conflicts")
}

func TestOfficialEgressT2_HTTPRetryRebuildsAccountContext(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	firstAccount := officialEgressTestAccount(94, PlatformOpenAI)
	secondAccount := officialEgressTestAccount(95, PlatformOpenAI)
	firstRequest := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	secondRequest := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)

	firstRequest, err := attachOfficialEgressHTTPContext(firstRequest, ingressContext, firstAccount, PlatformOpenAI)
	require.NoError(t, err)
	secondRequest, err = attachOfficialEgressHTTPContext(secondRequest, ingressContext, secondAccount, PlatformOpenAI)
	require.NoError(t, err)

	firstContext, exists := OfficialEgressContextFromContext(firstRequest.Context())
	require.True(t, exists)
	secondContext, exists := OfficialEgressContextFromContext(secondRequest.Context())
	require.True(t, exists)
	require.NotSame(t, firstContext, secondContext)
	require.Equal(t, int64(94), firstContext.AccountID())
	require.Equal(t, int64(95), secondContext.AccountID())
}

func TestOfficialEgressT2_WebSocketContextFreezesBeforeDial(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := officialEgressTestAccount(94, PlatformOpenAI)
	ingressContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ingressContext.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	firstPayload := newOfficialOpenAIWSFirstFrameForTest(t)
	applyOfficialOpenAIWSIngressHeadersForTest(ingressContext, firstPayload)

	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(),
		ingressContext,
		account,
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
	)
	require.NoError(t, err)
	egressContext, exists := OfficialEgressContextFromContext(ctx)
	require.True(t, exists)
	require.True(t, egressContext.IsFrozen())
	require.ErrorContains(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID,
		"should-not-change",
		OfficialEgressFieldSourceIngressExplicit,
		OfficialEgressFieldLifecycleSession,
	), "frozen")
}

func TestOfficialEgressT2_WebSocketFinalValidationRequiresFrozenContext(t *testing.T) {
	account := officialEgressTestAccount(94, PlatformOpenAI)
	egressContext := newOfficialEgressTestContext(
		account,
		OfficialEgressTransportWebSocket,
		"/v1/responses",
		"chatgpt.com",
	)
	profile, err := (DefaultOfficialEgressProfileResolver{}).ResolveWebSocketProfile(
		egressContext,
		account,
		"/v1/responses",
	)
	require.NoError(t, err)
	require.ErrorContains(t, ValidateOfficialEgressFinalState(egressContext, profile), "must be frozen")

	frozenContext, err := egressContext.Freeze()
	require.NoError(t, err)
	require.NoError(t, ValidateOfficialEgressFinalState(frozenContext, profile))
}

func TestOfficialEgressT2_FieldProvenanceRejectsConflict(t *testing.T) {
	account := officialEgressTestAccount(94, PlatformOpenAI)
	egressContext := newOfficialEgressTestContext(
		account,
		OfficialEgressTransportHTTP,
		"/v1/responses",
		"chatgpt.com",
	)
	require.NoError(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID,
		"session-secret-a",
		OfficialEgressFieldSourceIngressExplicit,
		OfficialEgressFieldLifecycleSession,
	))
	require.NoError(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID,
		"session-secret-a",
		OfficialEgressFieldSourceIngressExplicit,
		OfficialEgressFieldLifecycleSession,
	))
	require.ErrorContains(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID,
		"session-secret-b",
		OfficialEgressFieldSourceRedisSession,
		OfficialEgressFieldLifecycleSession,
	), "conflicting state")
}

func TestOfficialEgressT2_RedactedLogAttributesDoNotContainIdentityValues(t *testing.T) {
	account := officialEgressTestAccount(94, PlatformOpenAI)
	egressContext := newOfficialEgressTestContext(
		account,
		OfficialEgressTransportHTTP,
		"/v1/responses",
		"chatgpt.com",
	)
	const secret = "session-secret-must-not-be-logged"
	require.NoError(t, egressContext.RegisterField(
		OfficialEgressFieldSessionID,
		secret,
		OfficialEgressFieldSourceIngressExplicit,
		OfficialEgressFieldLifecycleSession,
	))
	profile, err := (DefaultOfficialEgressProfileResolver{}).ResolveHTTPProfile(
		egressContext,
		account,
		"/v1/responses",
	)
	require.NoError(t, err)

	rendered := fmt.Sprint(officialEgressRedactedLogAttributes(egressContext, profile))
	require.NotContains(t, rendered, secret)
	require.Contains(t, rendered, string(OfficialEgressFieldSessionID))
	require.Equal(t, "[redacted]", fmt.Sprint(mustOfficialEgressField(t, egressContext, OfficialEgressFieldSessionID)))
}

func TestOfficialEgressT2_BuiltInAccountExtraNormalization(t *testing.T) {
	normalized, err := NormalizeBuiltInOfficialEgressExtra(
		PlatformOpenAI,
		AccountTypeOAuth,
		map[string]any{
			"official_egress_enabled":         false,
			"official_egress_profile_version": "unknown-version",
			"unrelated":                       true,
		},
	)
	require.NoError(t, err)
	require.NotContains(t, normalized, "official_egress_enabled")
	require.NotContains(t, normalized, "official_egress_profile_version")
	require.Equal(t, true, normalized["unrelated"])

	normalized, err = NormalizeBuiltInOfficialEgressExtra(
		PlatformAnthropic,
		AccountTypeOAuth,
		map[string]any{
			"enable_tls_fingerprint":     true,
			"tls_fingerprint_profile_id": int64(12),
			"session_id_masking_enabled": true,
			"cache_ttl_override_enabled": true,
			"cache_ttl_override_target":  "1h",
		},
	)
	require.NoError(t, err)
	for _, dormantKey := range []string{
		"enable_tls_fingerprint",
		"tls_fingerprint_profile_id",
		"session_id_masking_enabled",
		"cache_ttl_override_enabled",
		"cache_ttl_override_target",
	} {
		_, exists := normalized[dormantKey]
		require.True(t, exists, dormantKey)
	}

	conflicting := map[string]any{
		"custom_base_url_enabled": true,
		"custom_base_url":         "https://relay.example.com",
	}
	normalized, err = NormalizeBuiltInOfficialEgressExtra(
		PlatformAnthropic,
		AccountTypeOAuth,
		conflicting,
	)
	require.NoError(t, err)
	require.Equal(t, true, normalized["custom_base_url_enabled"])
	require.Error(t, ValidateBuiltInOfficialEgressExtraTransition(
		PlatformAnthropic,
		AccountTypeOAuth,
		nil,
		conflicting,
	))
	require.NoError(t, ValidateBuiltInOfficialEgressExtraTransition(
		PlatformAnthropic,
		AccountTypeOAuth,
		conflicting,
		conflicting,
	))

	apiKeyExtra, err := NormalizeBuiltInOfficialEgressExtra(
		PlatformOpenAI,
		AccountTypeAPIKey,
		map[string]any{
			"official_egress_enabled":         true,
			"official_egress_profile_version": OfficialEgressProfileVersionPhase0,
			"enable_tls_fingerprint":          true,
		},
	)
	require.NoError(t, err)
	require.NotContains(t, apiKeyExtra, "official_egress_enabled")
	require.NotContains(t, apiKeyExtra, "official_egress_profile_version")
	require.Equal(t, true, apiKeyExtra["enable_tls_fingerprint"])
}

func TestOfficialEgressT2_LegacySettingsAreInactiveForBuiltInOAuthProfile(t *testing.T) {
	account := &Account{
		Platform: PlatformAnthropic,
		Type:     AccountTypeOAuth,
		Extra: map[string]any{
			"enable_tls_fingerprint":     true,
			"session_id_masking_enabled": true,
			"cache_ttl_override_enabled": true,
			"custom_base_url_enabled":    true,
			"custom_base_url":            "https://relay.example.com",
		},
	}

	require.False(t, account.IsTLSFingerprintEnabled())
	require.False(t, account.IsSessionIDMaskingEnabled())
	require.False(t, account.IsCacheTTLOverrideEnabled())
	require.False(t, account.IsCustomBaseURLEnabled())
}

func TestOfficialEgressT2_ConnectionPoolKeySeparatesAccountProxyAndTransport(t *testing.T) {
	firstAccount := officialEgressTestAccount(94, PlatformOpenAI)
	firstContext := newOfficialEgressTestContext(
		firstAccount,
		OfficialEgressTransportHTTP,
		"/v1/responses",
		"chatgpt.com",
	)
	firstProfile, err := (DefaultOfficialEgressProfileResolver{}).ResolveHTTPProfile(
		firstContext,
		firstAccount,
		"/v1/responses",
	)
	require.NoError(t, err)

	proxyID := int64(7)
	secondAccount := officialEgressTestAccount(95, PlatformOpenAI)
	secondAccount.ProxyID = &proxyID
	secondContext := newOfficialEgressTestContext(
		secondAccount,
		OfficialEgressTransportHTTP,
		"/v1/responses",
		"chatgpt.com",
	)
	secondContext.proxyID = proxyID
	secondProfile, err := (DefaultOfficialEgressProfileResolver{}).ResolveHTTPProfile(
		secondContext,
		secondAccount,
		"/v1/responses",
	)
	require.NoError(t, err)

	require.NotEqual(t, firstProfile.ConnectionPoolID, secondProfile.ConnectionPoolID)
	require.True(t, strings.Contains(firstProfile.ConnectionPoolID, "transport=http"))
	require.True(t, strings.Contains(secondProfile.ConnectionPoolID, "proxy=7"))
}

func officialEgressTestAccount(id int64, platform string) *Account {
	return &Account{
		ID:       id,
		Platform: platform,
		Type:     AccountTypeOAuth,
		Extra:    map[string]any{},
	}
}

func newOfficialEgressTestContext(
	account *Account,
	transport OfficialEgressTransport,
	endpoint string,
	host string,
) *OfficialEgressContext {
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	return NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:       account.ID,
		TargetPlatform:  account.Platform,
		InboundEndpoint: endpoint,
		Transport:       transport,
		UpstreamHost:    host,
		ProfileVersion:  officialEgressActiveVersionForTest(account),
		AccountType:     account.Type,
		ProxyID:         proxyID,
	})
}

func officialEgressActiveVersionForTest(account *Account) string {
	if account != nil && account.Platform == PlatformAnthropic {
		return "2.1.220"
	}
	return activeOpenAICodexProfileForTest().Build.Version
}

func activeOpenAICodexProfileForTest() officialClientResolvedProfile {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIAPIKeyResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil {
		panic(err)
	}
	return profile
}

func activeOpenAICodexVersionForTest() string {
	return activeOpenAICodexProfileForTest().Build.Version
}

func activeOpenAICodexUserAgentForTest() string {
	return activeOpenAICodexProfileForTest().Build.UserAgent
}

func activeOpenAICodexTransportIDForTest(endpointID string) string {
	profile, err := resolveCodexVersionProfile(activeOpenAICodexVersionForTest())
	if err != nil {
		panic(err)
	}
	endpoint, err := profile.ResolveEndpoint(endpointID)
	if err != nil {
		panic(err)
	}
	return endpoint.TransportID
}

func openAICodexReleaseModeForVersionForTest(version string) officialegress.ReleaseMode {
	for _, mode := range []officialegress.ReleaseMode{
		officialegress.ReleaseModeActive,
		officialegress.ReleaseModePrevious,
	} {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			panic(err)
		}
		if release.Version() == version {
			return mode
		}
	}
	panic("ReleaseCatalog 缺少测试版本：" + version)
}

func mustOfficialEgressField(
	t *testing.T,
	egressContext *OfficialEgressContext,
	name OfficialEgressFieldName,
) OfficialEgressField {
	t.Helper()
	field, exists := egressContext.Field(name)
	require.True(t, exists)
	return field
}
