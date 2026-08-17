package officialegress

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestCodexTurnStateAllowsOpaqueCredentialLikeSubstrings(t *testing.T) {
	for _, value := range []string{
		"opaque-sk-route-state",
		"opaque-refresh_token-route-state",
		"opaque-api_key=route-state",
		"opaque-bearer route-state",
	} {
		t.Run(value, func(t *testing.T) {
			turnState, err := NewCodexTurnStateValue(value)
			require.NoError(t, err)
			require.Equal(t, value, turnState.Value)

			facts := CodexIdentityFacts{
				TurnState:  turnState,
				Conditions: CodexRequestConditions{TurnStatePresent: true},
			}
			require.NoError(t, facts.Validate())
		})
	}
}

func TestCodexTurnStateExceptionDoesNotWeakenOtherIdentityFacts(t *testing.T) {
	_, err := NewCodexIdentityValue(
		"session-sk-secret",
		IdentitySourceInvocation,
		IdentityLifecycleSession,
	)
	require.ErrorContains(t, err, "身份事实疑似包含认证材料")

	facts := CodexIdentityFacts{
		TurnState: CodexIdentityValue{
			Value: "opaque-sk-route-state", Source: IdentitySourceIngress,
			Lifecycle: IdentityLifecycleSession,
		},
		Conditions: CodexRequestConditions{TurnStatePresent: true},
	}
	require.ErrorContains(t, facts.Validate(), "turn-state 身份事实来源或生命周期非法")
}
