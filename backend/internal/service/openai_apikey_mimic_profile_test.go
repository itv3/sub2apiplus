package service

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestOpenAIAPIKeyCodexMimicClientProfileCLIRS0125KeepsLegacyHeadersAndPromptKey(t *testing.T) {
	scope := openAIAPIKeyCodexMimicScope{
		AccountID:       11,
		APIKeyID:        22,
		UpstreamBaseURL: "https://api.openai.com",
		ClientProfile:   openAIAPIKeyCodexMimicClientCLIRS0125,
	}
	profile := openAIAPIKeyCodexMimicProfile{
		Enabled: true,
		Scope:   scope,
		Client:  resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope),
	}

	req, err := http.NewRequest(http.MethodPost, "https://compat-upstream.example/v1/responses", strings.NewReader(`{"model":"gpt-5.5","input":[{"role":"user","content":"hello"}]}`))
	require.NoError(t, err)

	profile.ApplyHeaders(req, true)
	out := applyOpenAIAPIKeyCodexMimicryToBody([]byte(`{"model":"gpt-5.5","input":[{"role":"user","content":"hello"}]}`), scope)

	require.Equal(t, codexCLIUserAgent, req.Header.Get("User-Agent"))
	require.Equal(t, "codex_cli_rs", req.Header.Get("originator"))
	require.Equal(t, "responses=experimental", req.Header.Get("OpenAI-Beta"))
	require.Equal(t, codexCLIVersion, req.Header.Get("version"))
	require.Empty(t, req.Header.Get("session-id"))
	require.True(t, strings.HasPrefix(gjson.GetBytes(out, "prompt_cache_key").String(), "codex-mimic-"))
	require.False(t, gjson.GetBytes(out, "client_metadata").Exists())
}

func TestOpenAIAPIKeyCodexMimicDesktopMetadataMatchesHeadersAndBody(t *testing.T) {
	account := &Account{
		ID:       11,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"base_url": "https://api.openai.com",
		},
		Extra: map[string]any{"openai_apikey_mimic_codex_cli": true},
	}
	profile := resolveOpenAIAPIKeyCodexMimicProfile(account, 22, nil)
	require.NotEmpty(t, profile.Scope.TurnID)
	require.Greater(t, profile.Scope.TurnStartedAtUnixMS, int64(0))

	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", nil)
	require.NoError(t, err)
	profile.ApplyHeaders(req, true)
	out := profile.RewriteBody([]byte(`{"model":"gpt-5.5","input":[{"role":"user","content":"hello"}]}`))

	sessionID := req.Header.Get("session-id")
	require.Regexp(t, openAICodexUUIDPattern, sessionID)
	require.Equal(t, sessionID, req.Header.Get("x-client-request-id"))
	require.Equal(t, sessionID, req.Header.Get("thread-id"))
	require.Equal(t, sessionID, gjson.GetBytes(out, "prompt_cache_key").String())
	require.Equal(t, sessionID, gjson.GetBytes(out, "client_metadata.session_id").String())
	require.Equal(t, sessionID, gjson.GetBytes(out, "client_metadata.thread_id").String())
	require.Equal(t, profile.Scope.TurnID, gjson.GetBytes(out, "client_metadata.turn_id").String())

	headerTurnMetadata := req.Header.Get("x-codex-turn-metadata")
	bodyTurnMetadata := gjson.GetBytes(out, "client_metadata.x-codex-turn-metadata").String()
	require.JSONEq(t, headerTurnMetadata, bodyTurnMetadata)
	require.Equal(t, profile.Scope.TurnID, gjson.Get(bodyTurnMetadata, "turn_id").String())
	require.Equal(t, profile.Scope.TurnStartedAtUnixMS, gjson.Get(bodyTurnMetadata, "turn_started_at_unix_ms").Int())
	require.Equal(t, "none", gjson.Get(bodyTurnMetadata, "sandbox").String())
	require.Equal(t, "project", gjson.Get(bodyTurnMetadata, "workspace_kind").String())
	require.Empty(t, req.Header.Get("x-openai-internal-codex-responses-lite"))

	var raw map[string]any
	require.NoError(t, json.Unmarshal([]byte(bodyTurnMetadata), &raw))
	require.Contains(t, raw, "turn_started_at_unix_ms")
}
