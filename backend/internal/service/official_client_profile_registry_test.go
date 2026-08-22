package service

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

// TestOfficialClientProfileRegistryDigestsAreLocked 锁定全部 wire 画像的聚合摘要。
//
// registry 会为每个画像计算 SHA-256，但此前测试只断言它非空：任何 build 或 wire
// 字段被改动，测试仍然全绿，与完整 Codex 版本画像的摘要锁定形成保护不对称。
// 聚合摘要覆盖全部画像却只需锁定一个常量；它变化时必须回到抓包证据确认改动属实，
// 再同步 §3.5 台账与本常量。
func TestAnthropicClientProfileCatalogDigestsAreLocked(t *testing.T) {
	profiles := defaultAnthropicClientProfileCatalog.profiles
	entries := make([]string, 0, len(profiles))
	for id, profile := range profiles {
		require.NotEmptyf(t, profile.Digest, "official client 画像 %s 缺少摘要", id)
		entries = append(entries, id+"="+profile.Digest)
	}
	sort.Strings(entries)
	sum := sha256.Sum256([]byte(strings.Join(entries, "\n")))
	aggregate := hex.EncodeToString(sum[:])

	const expectedProfileCount = 6
	// FW-H 退休 Claude OAuth 旧画像后，这组遗留 HTTP 画像仅服务 Setup Token
	// 产品语义；摘要变化来自身份范围收窄，不是官方抓包规则变化。
	const expectedAggregateDigest = "2140255a783a98614b16092dc6ee0bc6efc4459ac4878edb8116782cf91b8a30"
	require.Equalf(
		t,
		expectedProfileCount,
		len(entries),
		"official client 画像数量变化：新增或删除画像必须同步 §3.5 台账与抓包来源",
	)
	require.Equalf(
		t,
		expectedAggregateDigest,
		aggregate,
		"official client 画像摘要变化：共 %d 个画像，需复核抓包来源并同步 §3.5 台账",
		len(entries),
	)
}

func TestOfficialClientProfileRegistryActiveReleasesMatchCurrentCaptures(t *testing.T) {
	claudeProfile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.Equal(t, "2.1.220", claudeProfile.Build.Version)
	require.Equal(t, "apikey", claudeProfile.Wire.AuthMode)
	require.Equal(t, officialEgressTransportProfileAnthropicHTTP, claudeProfile.Wire.TransportProfileID)
	require.NotEmpty(t, claudeProfile.Wire.Digest)

	codexProfile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIAPIKeyResponsesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	activeRelease, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	activeHTTPNode, ok := activeRelease.Node(officialegress.RegistryPurposeOpenAIOAuthHTTP)
	require.True(t, ok)
	require.Equal(t, activeRelease.Version(), codexProfile.Build.Version)
	require.Equal(t, "codex_exec", codexProfile.Build.Originator)
	require.Equal(t, activeHTTPNode.Wire.TransportProfileID, codexProfile.Wire.TransportProfileID)
	require.NotEmpty(t, codexProfile.Wire.Digest)
}

func TestOfficialClientProfileRegistryReturnsDefensiveCopies(t *testing.T) {
	first, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	first.Build.RuntimeHeaders[0].Value = "tampered"
	first.Wire.StaticHeaders[0].Value = "tampered"

	second, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyMessagesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.NotEqual(t, "tampered", second.Build.RuntimeHeaders[0].Value)
	require.NotEqual(t, "tampered", second.Wire.StaticHeaders[0].Value)
}

func TestOfficialClientProfileRegistryFailsClosed(t *testing.T) {
	_, err := resolveOfficialClientProfile("unknown-purpose", officialClientProfileModeActive)
	require.Error(t, err)
	_, err = resolveOfficialClientProfile(
		officialClientPurposeOpenAIAPIKeyResponsesHTTP,
		"unknown-mode",
	)
	require.Error(t, err)
	_, err = resolveOfficialClientProfileByID("unknown-profile")
	require.Error(t, err)
}

func TestOfficialClientProfileCatalogProjectsAPIKeyWebSocketFromRelease(t *testing.T) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIAPIKeyResponsesWS,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.Contains(t, profile.Wire.ID, "apikey_projection")
	require.NotContains(t, profile.Wire.StaticHeaders, officialClientHeaderValue{Name: "version", Value: "0.145.0"})
}
