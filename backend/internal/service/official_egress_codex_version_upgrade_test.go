package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodexProjectionUsesFormalReleaseCatalog(t *testing.T) {
	catalog := officialegress.DefaultReleaseCatalog()
	active, err := catalog.Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	previous, err := catalog.Resolve(officialegress.ReleaseModePrevious)
	require.NoError(t, err)
	require.NotEqual(t, active.ReleaseDigest(), previous.ReleaseDigest())
	require.Equal(t, active.Version(), mustOfficialCodexVersionForTest(t, officialClientProfileModeActive))
	require.Equal(t, previous.Version(), mustOfficialCodexVersionForTest(t, officialClientProfileModePrevious))
}

func TestOfficialCodexJSONFieldOrderIsResolvedWithoutPackageActiveSlice(t *testing.T) {
	fields, err := officialCodexBodyFieldOrderForMode(
		officialClientProfileModePrevious, officialCodexEndpointResponsesHTTP,
	)
	require.NoError(t, err)
	require.NotEmpty(t, fields)
	require.Equal(t, "model", fields[0])
}

func mustOfficialCodexVersionForTest(t *testing.T, mode string) string {
	t.Helper()
	version, err := officialCodexVersionForMode(mode)
	require.NoError(t, err)
	return version
}
