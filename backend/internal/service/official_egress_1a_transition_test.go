package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestOfficialEgressRuntimeUsesFormalReleaseCatalog(t *testing.T) {
	resolver, err := officialegress.NewBundleResolver(
		officialegress.DefaultReleaseCatalog(), officialegress.DefaultSinkCatalog(),
	)
	require.NoError(t, err)
	runtimeState := NewOfficialEgressTransitionRuntime(
		resolver, officialegress.NewCompiler(), nil, officialegress.DefaultGuard(),
		officialegress.ReleaseModePrevious,
	)
	require.Same(t, resolver, runtimeState.BundleResolver)
	require.Equal(t, officialegress.ReleaseModePrevious, runtimeState.CodexReleaseMode)
}

func TestFormalReleaseCatalogDoesNotFoldActiveAndPrevious(t *testing.T) {
	catalog := officialegress.DefaultReleaseCatalog()
	active, err := catalog.Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	previous, err := catalog.Resolve(officialegress.ReleaseModePrevious)
	require.NoError(t, err)
	require.NotEqual(t, active.ReleaseDigest(), previous.ReleaseDigest())
}
