package repository

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestProvideOfficialEgressGuardInstallsClaudeProductionCatalogOnlyForActive(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.OfficialEgressGuard.UnknownRoutePolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.UnregisteredSinkPolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.CanaryPercent = 100
	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "active"
	guard, err := ProvideOfficialEgressGuard(cfg)
	require.NoError(t, err)
	binding, ok := guard.ProcessSinkCatalog().Resolve(officialegress.SinkClaudeMessagesInference)
	require.True(t, ok)
	require.Equal(t, officialegress.PersonaClaudeCode, binding.Persona())
	require.True(t, binding.RuntimeBindable())
	require.Contains(t, binding.MigrationChangeset(), "fw-h-production")

	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "legacy"
	guard, err = ProvideOfficialEgressGuard(cfg)
	require.NoError(t, err)
	_, ok = guard.ProcessSinkCatalog().Resolve(officialegress.SinkClaudeMessagesInference)
	require.False(t, ok)
}
