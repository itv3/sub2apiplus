package admin

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestPlusExtraAnthropicStandardAPICompat(t *testing.T) {
	key := service.AnthropicAPIKeyStandardAPICompatExtraKey
	account := &service.Account{Platform: service.PlatformAnthropic, Type: service.AccountTypeAPIKey}
	require.NoError(t, validatePlusExtraPatchForAccount(map[string]any{key: true}, account))
	require.NoError(t, validatePlusExtraPatchForAccount(map[string]any{key: nil}, account))
	require.Error(t, validatePlusExtraPatchForAccount(map[string]any{key: "true"}, account))
	account.Extra = map[string]any{"anthropic_apikey_mimic_claude_code": true}
	require.Error(t, validatePlusExtraPatchForAccount(map[string]any{key: true}, account))
	require.NoError(t, validatePlusExtraPatchForAccount(map[string]any{key: true, "anthropic_apikey_mimic_claude_code": false}, account))
	for _, other := range []*service.Account{
		{Platform: service.PlatformAnthropic, Type: service.AccountTypeOAuth},
		{Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey},
	} {
		require.Error(t, validatePlusExtraPatchForAccount(map[string]any{key: true}, other))
	}
}
