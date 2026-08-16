//go:build unit

package service

import (
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/stretchr/testify/require"
)

func TestCodexDefaultIdentityMatchesActiveRelease(t *testing.T) {
	activeProfile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	require.Equal(t, activeProfile.Build.Version, codexCLIVersion)
	require.Equal(t, activeProfile.Build.UserAgent, codexCLIUserAgent)

	require.True(t, strings.Contains(codexCLIUserAgent, "codex_exec/"+codexCLIVersion),
		"默认 User-Agent 必须包含 Active ReleaseCatalog 的版本")

	require.Equal(t, codexCLIUserAgent, DefaultOpenAICodexUserAgent)
}
