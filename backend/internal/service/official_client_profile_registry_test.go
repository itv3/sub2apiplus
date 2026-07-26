package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

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
	require.Equal(t, "0.145.0", codexProfile.Build.Version)
	require.Equal(t, "codex_exec", codexProfile.Build.Originator)
	require.Equal(t, officialEgressTransportProfileOpenAIHTTP, codexProfile.Wire.TransportProfileID)
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

func TestOfficialClientProfileRegistryKeepsAPIKeyWebSocketInactive(t *testing.T) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIAPIKeyResponsesWS,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.Contains(t, profile.Wire.ID, "inactive")
	require.NotContains(t, profile.Wire.StaticHeaders, officialClientHeaderValue{Name: "version", Value: "0.145.0"})
}
