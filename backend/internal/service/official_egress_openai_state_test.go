package service

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOfficialOpenAITurnStateScopeIsolatesEveryAuthorityDimension(t *testing.T) {
	identity := officialOpenAIHTTPIdentity{
		sessionID: "session-scope-1", turnID: "turn-scope-1",
		sessionProvenance: officialOpenAIProvenanceExplicitIngress,
		turnProvenance:    officialOpenAIProvenanceExplicitSessionDerivedTurn,
	}
	base := newOfficialOpenAITurnStateScope(
		101, 202, "https://chatgpt.com:443", strings.Repeat("a", 64), identity,
	)
	baseKey := base.persistentStoreKey()
	require.NotEmpty(t, baseKey)
	require.Len(t, strings.TrimPrefix(baseKey, "http-turn:v1:"), 64)

	mutations := []struct {
		name   string
		mutate func(*officialOpenAITurnStateScope)
	}{
		{"group", func(scope *officialOpenAITurnStateScope) { scope.groupTenantID++ }},
		{"account", func(scope *officialOpenAITurnStateScope) { scope.localAccountID++ }},
		{"authority", func(scope *officialOpenAITurnStateScope) { scope.upstreamAuthority = "https://auth.openai.com:443" }},
		{"release", func(scope *officialOpenAITurnStateScope) { scope.releaseDigest = strings.Repeat("b", 64) }},
		{"session", func(scope *officialOpenAITurnStateScope) { scope.sessionIdentity += "-other" }},
		{"turn", func(scope *officialOpenAITurnStateScope) { scope.turnIdentity += "-other" }},
		{"session_provenance", func(scope *officialOpenAITurnStateScope) {
			scope.sessionProvenance = officialOpenAIProvenanceContentFallback
		}},
		{"turn_provenance", func(scope *officialOpenAITurnStateScope) {
			scope.turnProvenance = officialOpenAIProvenanceExplicitIngress
		}},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			changed := base
			mutation.mutate(&changed)
			changedKey := changed.persistentStoreKey()
			if mutation.name == "session_provenance" {
				require.Empty(t, changedKey)
				return
			}
			require.NotEmpty(t, changedKey)
			require.NotEqual(t, baseKey, changedKey)
		})
	}
}

func TestOfficialOpenAITurnStateScopeRejectsRequestLocalProvenance(t *testing.T) {
	for _, provenance := range []officialOpenAIIdentityProvenance{
		officialOpenAIProvenanceContentFallback,
		officialOpenAIProvenanceGeneratedRequestLocal,
	} {
		identity := officialOpenAIHTTPIdentity{
			sessionID: "same-content-session", turnID: "same-content-turn",
			sessionProvenance: provenance, turnProvenance: provenance,
		}
		scope := newOfficialOpenAITurnStateScope(
			101, 202, "https://chatgpt.com:443", strings.Repeat("a", 64), identity,
		)
		require.Empty(t, scope.persistentStoreKey())
	}
}

func TestOfficialOpenAITurnStateScopeRequiresNormalizedBundleAuthority(t *testing.T) {
	identity := officialOpenAIHTTPIdentity{
		sessionID: "session", turnID: "turn",
		sessionProvenance: officialOpenAIProvenanceExplicitIngress,
		turnProvenance:    officialOpenAIProvenanceExplicitIngress,
	}
	for _, authority := range []string{
		"https://chatgpt.com",
		"HTTPS://chatgpt.com:443",
		"https://chatgpt.com:443/path",
		"https://user@chatgpt.com:443",
	} {
		scope := newOfficialOpenAITurnStateScope(
			101, 202, authority, strings.Repeat("a", 64), identity,
		)
		require.Emptyf(t, scope.persistentStoreKey(), "authority=%s", authority)
	}
}
