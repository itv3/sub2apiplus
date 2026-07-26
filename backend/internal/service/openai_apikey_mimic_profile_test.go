package service

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
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

func TestOpenAIAPIKeyCodexMimicPreviousModeRestoresDesktopProfile(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra:    map[string]any{"openai_apikey_mimic_codex_cli": true},
	}
	cfg := &config.Config{Gateway: config.GatewayConfig{
		OfficialClientProfiles: config.GatewayOfficialClientProfilesConfig{Mode: officialClientProfileModePrevious},
	}}
	profile := resolveOpenAIAPIKeyCodexMimicProfile(account, 22, cfg)
	require.Equal(t, openAIAPIKeyCodexMimicClientDesktop0144, profile.Client.ID)
	require.Equal(t, codexDesktopUserAgent, profile.Client.UserAgent)

	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", nil)
	require.NoError(t, err)
	profile.ApplyHeaders(req, true)
	require.Empty(t, getHeaderRaw(req.Header, "x-openai-internal-codex-responses-lite"))
	metadata := req.Header.Get("x-codex-turn-metadata")
	require.Equal(t, "none", gjson.Get(metadata, "sandbox").String())
	require.Equal(t, "project", gjson.Get(metadata, "workspace_kind").String())
}

func TestOpenAIAPIKeyCodexMimicActiveModeIgnoresDormantAccountProfile(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli":       true,
			openAIAPIKeyCodexMimicProfileExtraKey: openAIAPIKeyCodexMimicClientDesktop0144,
		},
	}
	profile := resolveOpenAIAPIKeyCodexMimicProfile(account, 22, nil)
	require.Equal(t, openAIAPIKeyCodexMimicClientCodexExec0145, profile.Client.ID)
}

func TestOpenAIAPIKeyCodexMimicCurrentCLIMetadataMatchesHeadersAndBody(t *testing.T) {
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
	require.Equal(t, "seccomp", gjson.Get(bodyTurnMetadata, "sandbox").String())
	require.False(t, gjson.Get(bodyTurnMetadata, "workspace_kind").Exists())
	require.Equal(t, "true", getHeaderRaw(req.Header, "x-openai-internal-codex-responses-lite"))
	require.Empty(t, req.Header.Get("OpenAI-Beta"))
	require.Empty(t, req.Header.Get("version"))

	var raw map[string]any
	require.NoError(t, json.Unmarshal([]byte(bodyTurnMetadata), &raw))
	require.Contains(t, raw, "turn_started_at_unix_ms")
}
