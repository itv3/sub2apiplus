package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

type officialEgressT4Cache struct {
}

func (c *officialEgressT4Cache) GetSessionAccountID(context.Context, int64, string) (int64, error) {
	return 0, nil
}

func (c *officialEgressT4Cache) SetSessionAccountID(context.Context, int64, string, int64, time.Duration) error {
	return nil
}

func (c *officialEgressT4Cache) RefreshSessionTTL(context.Context, int64, string, time.Duration) error {
	return nil
}

func (c *officialEgressT4Cache) DeleteSessionAccountID(context.Context, int64, string) error {
	return nil
}

func (c *officialEgressT4Cache) SetGrokVideoPendingBilling(context.Context, string, []byte, time.Duration) error {
	return nil
}

func (c *officialEgressT4Cache) GetGrokVideoPendingBilling(context.Context, string) ([]byte, error) {
	return nil, nil
}

func (c *officialEgressT4Cache) ClaimGrokVideoBilled(context.Context, string, time.Duration) (bool, error) {
	return true, nil
}

func (c *officialEgressT4Cache) ReleaseGrokVideoBilled(context.Context, string) error {
	return nil
}

type officialEgressT4IdentityCache struct {
	getCalls int
}

func (c *officialEgressT4IdentityCache) GetFingerprint(context.Context, int64) (*Fingerprint, error) {
	c.getCalls++
	return &Fingerprint{
		ClientID:                strings.Repeat("b", 64),
		UserAgent:               "Mozilla/5.0 Claude Desktop Electron",
		StainlessLang:           "js",
		StainlessPackageVersion: "0.94.0",
		StainlessOS:             "MacOS",
		StainlessArch:           "arm64",
		StainlessRuntime:        "node",
		StainlessRuntimeVersion: "v26.3.0",
	}, nil
}

func (c *officialEgressT4IdentityCache) SetFingerprint(context.Context, int64, *Fingerprint) error {
	return nil
}

func (c *officialEgressT4IdentityCache) GetMaskedSessionID(context.Context, int64) (string, error) {
	return "99999999-9999-4999-8999-999999999999", nil
}

func (c *officialEgressT4IdentityCache) SetMaskedSessionID(context.Context, int64, string) error {
	return nil
}

func TestOfficialEgressT4_AnthropicFinalizerMatchesPhase0ApplicationContract(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		accountUUID = "11111111-1111-4111-8111-111111111111"
		deviceID    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	)
	sessions := []string{
		"22222222-2222-4222-8222-222222222222",
		"33333333-3333-4333-8333-333333333333",
		"33333333-3333-4333-8333-333333333333",
		"44444444-4444-4444-8444-444444444444",
		"44444444-4444-4444-8444-444444444444",
	}
	cache := &officialEgressT4Cache{}
	identityCache := &officialEgressT4IdentityCache{}
	svc := &GatewayService{
		cache:           cache,
		identityService: NewIdentityService(identityCache),
	}
	account := officialEgressT4AnthropicAccount(accountUUID)

	seenSessions := make(map[string]struct{})
	seenClientRequestIDs := make(map[string]struct{})
	for _, sessionID := range sessions {
		body := officialEgressT4AnthropicBody(deviceID, sessionID)
		c := officialEgressT4GinContext(sessionID)

		req, wireBody, err := svc.buildUpstreamRequest(
			context.Background(),
			c,
			account,
			body,
			"oauth-token",
			"oauth",
			"claude-sonnet-5",
			true,
			false,
		)
		require.NoError(t, err)
		require.Equal(t, claudeAPIURL, req.URL.String())
		require.Equal(t, int64(len(wireBody)), req.ContentLength)

		require.Equal(t, officialAnthropicUserAgent, getHeaderRaw(req.Header, "User-Agent"))
		require.Equal(t, officialAnthropicBetaHeader, getHeaderRaw(req.Header, "anthropic-beta"))
		require.Equal(t, sessionID, getHeaderRaw(req.Header, "X-Claude-Code-Session-Id"))
		clientRequestID := getHeaderRaw(req.Header, "x-client-request-id")
		require.NoError(t, uuid.Validate(clientRequestID))
		seenClientRequestIDs[clientRequestID] = struct{}{}
		for _, expected := range officialAnthropicCurrentTestHeaders(t) {
			require.Equal(t, expected.Value, getHeaderRaw(req.Header, expected.Name), expected.Name)
		}
		require.Empty(t, getHeaderRaw(req.Header, "accept-language"))
		require.Empty(t, getHeaderRaw(req.Header, "sec-fetch-mode"))
		require.Empty(t, getHeaderRaw(req.Header, "x-stainless-helper-method"))

		require.Equal(t, int64(4), gjson.GetBytes(wireBody, "system.#").Int())
		require.False(t, gjson.GetBytes(wireBody, "system.0.cache_control").Exists())
		require.False(t, gjson.GetBytes(wireBody, "system.1.cache_control").Exists())
		require.Equal(t, "ephemeral", gjson.GetBytes(wireBody, "system.2.cache_control.type").String())
		require.Equal(t, "1h", gjson.GetBytes(wireBody, "system.2.cache_control.ttl").String())
		require.Equal(t, "global", gjson.GetBytes(wireBody, "system.2.cache_control.scope").String())
		require.Equal(t, "ephemeral", gjson.GetBytes(wireBody, "system.3.cache_control.type").String())
		require.Equal(t, "1h", gjson.GetBytes(wireBody, "system.3.cache_control.ttl").String())
		require.False(t, gjson.GetBytes(wireBody, "system.3.cache_control.scope").Exists())
		require.True(
			t,
			strings.HasPrefix(
				gjson.GetBytes(wireBody, "system.3.text").String(),
				officialAnthropicSystemSplitMarker,
			),
		)

		billing := gjson.GetBytes(wireBody, "system.0.text").String()
		require.Contains(t, billing, "cc_version="+officialAnthropicCLIVersion+".")
		require.Contains(t, billing, "cc_entrypoint=sdk-cli;")
		require.NotContains(t, billing, "cch=", "未证明的新 cch 算法不得伪造")

		metadata := ParseMetadataUserID(gjson.GetBytes(wireBody, "metadata.user_id").String())
		require.NotNil(t, metadata)
		require.Equal(t, deviceID, metadata.DeviceID)
		require.Equal(t, accountUUID, metadata.AccountUUID)
		require.Equal(t, sessionID, metadata.SessionID)
		seenSessions[metadata.SessionID] = struct{}{}

		require.Equal(t, "Bash", gjson.GetBytes(wireBody, "tools.0.name").String())
		require.False(t, gjson.GetBytes(wireBody, "temperature").Exists())
		require.Contains(
			t,
			string(wireBody),
			`"opaque":{"z":9007199254740993,"a":{"y":2,"x":1}}`,
			"Finalizer 必须原样保留嵌套用户对象的整数精度与键序",
		)
		require.NotContains(t, string(wireBody), "9007199254740992")

		require.NotContains(t, billing, "cc_prev_req=", "官方请求不得伪造未取证的续轮字段")
	}

	require.Len(t, seenSessions, 3, "五条 S1/S2/S4 请求必须保持三个客户端会话身份")
	require.Len(t, seenClientRequestIDs, 5, "x-client-request-id 必须按请求生成")
	require.Zero(t, identityCache.getCalls, "Official Egress 不得再读取账号级 Desktop 指纹")
}

func officialAnthropicCurrentTestHeaders(t *testing.T) []officialClientHeaderValue {
	t.Helper()
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicSetupTokenMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	headers := append([]officialClientHeaderValue(nil), profile.Wire.StaticHeaders...)
	headers = append(headers, profile.Build.RuntimeHeaders...)
	headers = append(headers, officialClientHeaderValue{Name: "User-Agent", Value: profile.Build.UserAgent})
	return headers
}

func TestOfficialEgressT4_AnthropicFinalizerRejectsSessionConflict(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		deviceID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		sessionA = "22222222-2222-4222-8222-222222222222"
		sessionB = "33333333-3333-4333-8333-333333333333"
	)
	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	_, _, err := svc.buildUpstreamRequest(
		context.Background(),
		officialEgressT4GinContext(sessionB),
		officialEgressT4AnthropicAccount("11111111-1111-4111-8111-111111111111"),
		officialEgressT4AnthropicBody(deviceID, sessionA),
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.ErrorContains(t, err, "session header conflicts with metadata")
}

func TestOfficialEgressT4_AnthropicFinalizerNormalizesKiloRequest(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const accountUUID = "11111111-1111-4111-8111-111111111111"
	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	account := officialEgressT4AnthropicAccount(accountUUID)
	body := officialEgressT4KiloAnthropicBody(t)

	firstContext := officialEgressT4KiloGinContext("kilo-session-a")
	firstRequest, firstBody, err := svc.buildUpstreamRequest(
		context.Background(),
		firstContext,
		account,
		body,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)

	firstIdentity := ParseMetadataUserID(gjson.GetBytes(firstBody, "metadata.user_id").String())
	require.NotNil(t, firstIdentity)
	require.Len(t, firstIdentity.DeviceID, 64)
	require.Equal(t, accountUUID, firstIdentity.AccountUUID)
	require.NoError(t, uuid.Validate(firstIdentity.SessionID))
	require.Equal(t, firstIdentity.SessionID, getHeaderRaw(firstRequest.Header, "X-Claude-Code-Session-Id"))
	require.Equal(t, officialAnthropicUserAgent, getHeaderRaw(firstRequest.Header, "User-Agent"))

	require.Equal(t, int64(4), gjson.GetBytes(firstBody, "system.#").Int())
	require.True(t, strings.HasPrefix(
		gjson.GetBytes(firstBody, "system.0.text").String(),
		"x-anthropic-billing-header:",
	))
	require.Equal(t, claudeSDKCLIIdentityPrompt, gjson.GetBytes(firstBody, "system.1.text").String())
	require.True(t, strings.HasPrefix(
		gjson.GetBytes(firstBody, "system.3.text").String(),
		officialAnthropicSystemSplitMarker,
	))
	require.Equal(t, "Kilo third-party instruction", gjson.GetBytes(firstBody, "messages.0.content.0.text").String())
	require.Equal(t, int64(16), gjson.GetBytes(firstBody, "tools.#").Int())
	require.Equal(t, "kilo_tool_15", gjson.GetBytes(firstBody, "tools.15.name").String())
	require.False(t, gjson.GetBytes(firstBody, "tools.15.cache_control").Exists())
	require.Equal(t, "1h", gjson.GetBytes(firstBody, "messages.1.content.0.cache_control.ttl").String())
	requireOfficialAnthropicCacheProfile(t, firstBody, "messages.1.content.0.cache_control")

	// 相同客户端会话锚点必须跨重试和续轮保持同一身份。
	secondRequest, secondBody, err := svc.buildUpstreamRequest(
		context.Background(),
		officialEgressT4KiloGinContext("kilo-session-a"),
		account,
		body,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)
	secondIdentity := ParseMetadataUserID(gjson.GetBytes(secondBody, "metadata.user_id").String())
	require.NotNil(t, secondIdentity)
	require.Equal(t, firstIdentity.DeviceID, secondIdentity.DeviceID)
	require.Equal(t, firstIdentity.SessionID, secondIdentity.SessionID)
	require.Equal(t, secondIdentity.SessionID, getHeaderRaw(secondRequest.Header, "X-Claude-Code-Session-Id"))

	// 同一 Kilo 客户端的新会话复用设备身份，但必须生成新的会话身份。
	_, thirdBody, err := svc.buildUpstreamRequest(
		context.Background(),
		officialEgressT4KiloGinContext("kilo-session-b"),
		account,
		body,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)
	thirdIdentity := ParseMetadataUserID(gjson.GetBytes(thirdBody, "metadata.user_id").String())
	require.NotNil(t, thirdIdentity)
	require.Equal(t, firstIdentity.DeviceID, thirdIdentity.DeviceID)
	require.NotEqual(t, firstIdentity.SessionID, thirdIdentity.SessionID)

	// Kilo 多轮请求会同时携带上一轮和当前轮的 messages 缓存点；最终出站
	// 必须只保留当前最后一个消息断点，不能与两个 system 断点叠加后超过上限。
	multiTurnBody, err := sjson.SetRawBytes(body, "messages", []byte(`[
		{"role":"user","content":[{"type":"text","text":"第一轮"}]},
		{"role":"assistant","content":[{"type":"text","text":"第一轮回复","cache_control":{"type":"ephemeral"}}]},
		{"role":"user","content":[{"type":"text","text":"第二轮","cache_control":{"type":"ephemeral"}}]}
	]`))
	require.NoError(t, err)
	_, multiTurnFinalBody, err := svc.buildUpstreamRequest(
		context.Background(),
		officialEgressT4KiloGinContext("kilo-session-a"),
		account,
		multiTurnBody,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)
	requireOfficialAnthropicCacheProfile(
		t,
		multiTurnFinalBody,
		"messages.3.content.0.cache_control",
	)
	require.False(t, gjson.GetBytes(
		multiTurnFinalBody,
		"messages.2.content.0.cache_control",
	).Exists())
}

func TestOfficialEgressT4_CompatibilityLayerDefersProfileFieldsToFinalizer(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const accountUUID = "11111111-1111-4111-8111-111111111111"
	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	account := officialEgressT4AnthropicAccount(accountUUID)
	ingressBody := officialEgressT4KiloAnthropicBody(t)
	c := officialEgressT4KiloGinContext("kilo-ownership-session")
	captureOfficialAnthropicIngressContract(c, ingressBody)

	compatibilityBody, model := svc.applyClaudeSetupTokenThirdPartyCompatibilityToBody(
		context.Background(),
		c,
		account,
		ingressBody,
		gjson.GetBytes(ingressBody, "system").Value(),
		"claude-sonnet-5",
		true,
	)

	// 兼容层只能处理模型、工具名和请求结构；入口画像字段必须保持到 Finalizer。
	require.Equal(t, "claude-sonnet-5", model)
	require.Equal(t, int64(1), gjson.GetBytes(compatibilityBody, "system.#").Int())
	require.Equal(t, "Kilo third-party instruction", gjson.GetBytes(compatibilityBody, "system.0.text").String())
	require.False(t, gjson.GetBytes(compatibilityBody, "metadata").Exists())
	require.Equal(t, "5m", gjson.GetBytes(compatibilityBody, "tools.15.cache_control.ttl").String())
	require.True(t, gjson.GetBytes(compatibilityBody, "messages.0.content.0.cache_control").Exists())

	req, finalBody, err := svc.buildUpstreamRequest(
		context.Background(),
		c,
		account,
		compatibilityBody,
		"oauth-token",
		"oauth",
		model,
		true,
		true,
	)
	require.NoError(t, err)
	require.Equal(t, officialAnthropicUserAgent, getHeaderRaw(req.Header, "User-Agent"))
	require.Equal(t, int64(4), gjson.GetBytes(finalBody, "system.#").Int())
	require.Equal(t, "Kilo third-party instruction", gjson.GetBytes(finalBody, "messages.0.content.0.text").String())
	require.NotEmpty(t, gjson.GetBytes(finalBody, "metadata.user_id").String())
	requireOfficialAnthropicCacheProfile(t, finalBody, "messages.1.content.0.cache_control")
}

func TestOfficialEgressT4_CrossProtocolIngressDefersProfileToAnthropicFinalizer(t *testing.T) {
	gin.SetMode(gin.TestMode)

	for _, endpoint := range []string{"/v1/responses", "/v1/chat/completions"} {
		t.Run(endpoint, func(t *testing.T) {
			svc := &GatewayService{cache: &officialEgressT4Cache{}}
			account := officialEgressT4AnthropicAccount("11111111-1111-4111-8111-111111111111")
			c := officialEgressT4KiloGinContext("kilo-cross-protocol-session")
			c.Request.URL.Path = endpoint
			ingressBody := officialEgressT4KiloAnthropicBody(t)

			officialEgressOwnsProfile, err := resolveAnthropicOfficialEgressOwnership(account, c)
			require.NoError(t, err)
			require.True(t, officialEgressOwnsProfile)

			compatibilityBody, model := svc.applyClaudeSetupTokenThirdPartyCompatibilityToBody(
				context.Background(),
				c,
				account,
				ingressBody,
				gjson.GetBytes(ingressBody, "system").Value(),
				"claude-sonnet-5",
				officialEgressOwnsProfile,
			)

			req, finalBody, err := svc.buildUpstreamRequest(
				context.Background(),
				c,
				account,
				compatibilityBody,
				"oauth-token",
				"oauth",
				model,
				true,
				true,
			)
			require.NoError(t, err)
			require.Equal(t, officialAnthropicUserAgent, getHeaderRaw(req.Header, "User-Agent"))
			require.Equal(t, int64(4), gjson.GetBytes(finalBody, "system.#").Int())
			require.NotEmpty(t, gjson.GetBytes(finalBody, "metadata.user_id").String())
			requireOfficialAnthropicCacheProfile(
				t,
				finalBody,
				"messages.1.content.0.cache_control",
			)
		})
	}
}

func requireOfficialAnthropicCacheProfile(t *testing.T, body []byte, messagePathsExpected ...string) {
	t.Helper()
	invalidThinking, messagePaths, toolPaths, systemPaths := collectCacheControlPaths(body)
	require.Empty(t, invalidThinking)
	require.Equal(t, messagePathsExpected, messagePaths)
	require.Empty(t, toolPaths)
	require.Equal(t, []string{
		"system.2.cache_control",
		"system.3.cache_control",
	}, systemPaths)
	for _, messagePath := range messagePathsExpected {
		require.Equal(t, "1h", gjson.GetBytes(body, messagePath+".ttl").String())
	}
}

func TestOfficialEgressT4_AnthropicFinalizerUsesRealKiloIngressContract(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		accountUUID       = "11111111-1111-4111-8111-111111111111"
		legacyDeviceID    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		legacySessionID   = "22222222-2222-4222-8222-222222222222"
		firstSessionToken = "kilo-real-session-a"
	)
	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	account := officialEgressT4AnthropicAccount(accountUUID)
	ingressBody := officialEgressT4KiloAnthropicBody(t)
	transformedBody, ok := setJSONValueBytes(
		ingressBody,
		"metadata.user_id",
		FormatMetadataUserID(
			legacyDeviceID,
			accountUUID,
			legacySessionID,
			officialAnthropicCLIVersion,
		),
	)
	require.True(t, ok)
	transformedBody, ok = setJSONValueBytes(transformedBody, "temperature", 1)
	require.True(t, ok)

	firstContext := officialEgressT4KiloGinContext(firstSessionToken)
	captureOfficialAnthropicIngressContract(firstContext, ingressBody)
	firstRequest, firstBody, err := svc.buildUpstreamRequest(
		context.Background(),
		firstContext,
		account,
		transformedBody,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)
	firstIdentity := ParseMetadataUserID(gjson.GetBytes(firstBody, "metadata.user_id").String())
	require.NotNil(t, firstIdentity)
	require.NotEqual(t, legacyDeviceID, firstIdentity.DeviceID)
	require.NotEqual(t, legacySessionID, firstIdentity.SessionID)
	require.Equal(t, firstIdentity.SessionID, getHeaderRaw(firstRequest.Header, "X-Claude-Code-Session-Id"))
	require.False(t, gjson.GetBytes(firstBody, "temperature").Exists())

	secondContext := officialEgressT4KiloGinContext("kilo-real-session-b")
	captureOfficialAnthropicIngressContract(secondContext, ingressBody)
	_, secondBody, err := svc.buildUpstreamRequest(
		context.Background(),
		secondContext,
		account,
		transformedBody,
		"oauth-token",
		"oauth",
		"claude-sonnet-5",
		true,
		false,
	)
	require.NoError(t, err)
	secondIdentity := ParseMetadataUserID(gjson.GetBytes(secondBody, "metadata.user_id").String())
	require.NotNil(t, secondIdentity)
	require.Equal(t, firstIdentity.DeviceID, secondIdentity.DeviceID)
	require.NotEqual(t, firstIdentity.SessionID, secondIdentity.SessionID)
}

func TestOfficialEgressT4_AnthropicFinalizerRecordsDerivedKiloIdentity(t *testing.T) {
	gin.SetMode(gin.TestMode)

	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	account := officialEgressT4AnthropicAccount("11111111-1111-4111-8111-111111111111")
	c := officialEgressT4KiloGinContext("kilo-session-source")
	body := officialEgressT4KiloAnthropicBody(t)
	req, err := http.NewRequest(http.MethodPost, claudeAPIURL, strings.NewReader(string(body)))
	require.NoError(t, err)
	req, err = attachOfficialEgressHTTPContext(req, c, account, PlatformAnthropic)
	require.NoError(t, err)

	req, _, _, err = svc.finalizeAnthropicSetupTokenEgressRequest(req, c, account, body)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(req.Context())
	require.True(t, ok)

	deviceField, ok := egressContext.Field(OfficialEgressFieldDeviceID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceDerived, deviceField.Source)
	sessionField, ok := egressContext.Field(OfficialEgressFieldSessionID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceDerived, sessionField.Source)
}

func TestOfficialEgressT4_AnthropicFinalizerReportsModificationsAndProvenance(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		accountUUID = "11111111-1111-4111-8111-111111111111"
		deviceID    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		sessionID   = "22222222-2222-4222-8222-222222222222"
	)
	svc := &GatewayService{cache: &officialEgressT4Cache{}}
	account := officialEgressT4AnthropicAccount(accountUUID)
	c := officialEgressT4GinContext(sessionID)
	body := officialEgressT4AnthropicBody(deviceID, sessionID)
	req, err := http.NewRequest(http.MethodPost, claudeAPIURL, strings.NewReader(string(body)))
	require.NoError(t, err)
	req, err = attachOfficialEgressHTTPContext(req, c, account, PlatformAnthropic)
	require.NoError(t, err)

	req, _, result, err := svc.finalizeAnthropicSetupTokenEgressRequest(req, c, account, body)
	require.NoError(t, err)
	require.Equal(t, []OfficialEgressModification{
		{Kind: "body", Field: "metadata.user_id"},
		{Kind: "body", Field: "system"},
		{Kind: "header", Field: "User-Agent"},
		{Kind: "header", Field: "anthropic-beta"},
		{Kind: "header", Field: "X-Claude-Code-Session-Id"},
		{Kind: "header", Field: "x-client-request-id"},
	}, result.Modifications)

	egressContext, ok := OfficialEgressContextFromContext(req.Context())
	require.True(t, ok)
	sessionField, ok := egressContext.Field(OfficialEgressFieldSessionID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceIngressExplicit, sessionField.Source)
	require.Equal(t, OfficialEgressFieldLifecycleSession, sessionField.Lifecycle)
	accountField, ok := egressContext.Field(OfficialEgressFieldAccountUUID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceAccountStatic, accountField.Source)
	clientRequestField, ok := egressContext.Field(OfficialEgressFieldClientRequestID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceDerived, clientRequestField.Source)
	require.Equal(t, OfficialEgressFieldLifecycleRequest, clientRequestField.Lifecycle)
}

func TestOfficialEgressT4_AnthropicFingerprintSkipsSystemReminder(t *testing.T) {
	body := []byte(`{
		"messages":[{
			"role":"user",
			"content":[
				{"type":"text","text":"<system-reminder>\n自动上下文\n</system-reminder>"},
				{"type":"text","text":"你好，执行测试命令并返回结果"}
			]
		},{
			"role":"assistant",
			"content":[{"type":"text","text":"处理中"}]
		},{
			"role":"user",
			"content":[{"type":"text","text":"后续工具结果不能改变首轮指纹"}]
		}]
	}`)
	require.Equal(t, "9c5", computeOfficialAnthropicFingerprint(body, officialAnthropicCLIVersion))
}

func officialEgressT4AnthropicAccount(accountUUID string) *Account {
	return &Account{
		ID:       50,
		Platform: PlatformAnthropic,
		Type:     AccountTypeSetupToken,
		Extra: map[string]any{
			"account_uuid":               accountUUID,
			"session_id_masking_enabled": true,
		},
	}
}

func officialEgressT4GinContext(sessionID string) *gin.Context {
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", officialAnthropicUserAgent)
	c.Request.Header.Set("X-Claude-Code-Session-Id", sessionID)
	return c
}

func officialEgressT4KiloGinContext(sessionAffinity string) *gin.Context {
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set(
		"User-Agent",
		"Kilo-Code/7.4.1101 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
	)
	c.Request.Header.Set("X-Session-Affinity", sessionAffinity)
	return c
}

func officialEgressT4KiloAnthropicBody(t *testing.T) []byte {
	t.Helper()
	tools := make([]map[string]any, 0, 16)
	for index := 0; index < 16; index++ {
		tool := map[string]any{
			"name":        fmt.Sprintf("kilo_tool_%02d", index),
			"description": "Kilo 工具",
			"input_schema": map[string]any{
				"type":       "object",
				"properties": map[string]any{},
			},
		}
		if index == 15 {
			tool["cache_control"] = map[string]any{
				"type": "ephemeral",
				"ttl":  "5m",
			}
		}
		tools = append(tools, tool)
	}
	body, err := json.Marshal(map[string]any{
		"model":      "claude-sonnet-5",
		"max_tokens": 4096,
		"stream":     true,
		"system": []map[string]any{
			{"type": "text", "text": "Kilo third-party instruction"},
		},
		"messages": []map[string]any{
			{
				"role": "user",
				"content": []map[string]any{
					{
						"type":          "text",
						"text":          "KILO_S1_OK",
						"cache_control": map[string]any{"type": "ephemeral"},
					},
				},
			},
		},
		"tools": tools,
	})
	require.NoError(t, err)
	return body
}

func officialEgressT4AnthropicBody(deviceID, sessionID string) []byte {
	metadataUserID := FormatMetadataUserID(deviceID, "", sessionID, officialAnthropicCLIVersion)
	return []byte(fmt.Sprintf(`{
		"model":"claude-sonnet-5",
		"system":[
			{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.218.000; cc_entrypoint=cli;"},
			{"type":"text","text":"You are Claude Code, Anthropic's official CLI for Claude.","cache_control":{"type":"ephemeral"}},
			{"type":"text","text":"第三块稳定内容\n\n# Text output (does not apply to tool calls)\n第四块稳定内容","cache_control":{"type":"ephemeral"}}
		],
		"metadata":{"user_id":%q},
		"messages":[{"role":"user","content":[{"type":"text","text":"abcdefghijklmnopqrstuvwxyz","opaque":{"z":9007199254740993,"a":{"y":2,"x":1}}}]}],
		"tools":[{"name":"Bash","description":"执行命令","input_schema":{"type":"object"}}],
		"stream":true
	}`, metadataUserID))
}
