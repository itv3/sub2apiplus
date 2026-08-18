package repository

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestBindClaudeLegacyObservationContextUsesExactOfficialURL(t *testing.T) {
	bound, err := bindClaudeLegacyObservationContext(
		context.Background(),
		"https://platform.claude.com/v1/oauth/token?source=claude-code",
		"platform.claude.com",
		"/v1/oauth/token",
		officialegress.SinkClaudeLegacyOAuthRefresh,
	)
	require.NoError(t, err)
	identity, ok := officialegress.AttemptIdentityFromContext(bound)
	require.True(t, ok)
	require.Equal(t, officialegress.SinkClaudeLegacyOAuthRefresh, identity.SinkID)
	require.Equal(t, officialegress.PersonaUnclassified, identity.DeclaredPersona)

	unbound, err := bindClaudeLegacyObservationContext(
		context.Background(),
		"https://claude-relay.example.com/v1/oauth/token",
		"platform.claude.com",
		"/v1/oauth/token",
		officialegress.SinkClaudeLegacyOAuthRefresh,
	)
	require.NoError(t, err)
	_, ok = officialegress.AttemptIdentityFromContext(unbound)
	require.False(t, ok)
}
