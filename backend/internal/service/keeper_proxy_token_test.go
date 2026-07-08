package service

import (
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestKeeperProxyTokenRoundTripBindsAccountPlatformAndExpiry(t *testing.T) {
	now := time.Date(2026, 7, 8, 12, 34, 0, 0, time.UTC)
	token, err := IssueKeeperProxyToken("internal-secret", 42, PlatformOpenAI, now)
	require.NoError(t, err)
	require.True(t, strings.HasPrefix(token, "skp1."))

	require.NoError(t, ValidateKeeperProxyToken(token, "internal-secret", 42, PlatformOpenAI, now))
	require.Error(t, ValidateKeeperProxyToken(token, "internal-secret", 43, PlatformOpenAI, now))
	require.Error(t, ValidateKeeperProxyToken(token, "internal-secret", 42, PlatformAnthropic, now))
	require.Error(t, ValidateKeeperProxyToken(token, "other-secret", 42, PlatformOpenAI, now))
	require.Error(t, ValidateKeeperProxyToken(token, "internal-secret", 42, PlatformOpenAI, now.Add(KeeperProxyTokenTTL)))
}

func TestKeeperProxyTokenRejectsMissingSecretAndUnsupportedPlatform(t *testing.T) {
	now := time.Date(2026, 7, 8, 12, 0, 0, 0, time.UTC)

	_, err := IssueKeeperProxyToken("", 42, PlatformOpenAI, now)
	require.Error(t, err)

	_, err = IssueKeeperProxyToken("internal-secret", 42, "gemini", now)
	require.Error(t, err)
}
