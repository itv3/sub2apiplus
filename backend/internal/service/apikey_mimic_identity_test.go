package service

import (
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const openAICodexUUIDPattern = `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`

func TestApplyOpenAIAPIKeyCodexMimicryToBodyPreservesThirdPartyIdentityText(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.4",
		"instructions":"You are Kilo-Code. Keep repository rules.",
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"请列出工具名称"}]}],
		"tools":[{"type":"function","name":"kilo_local_recall","description":"Kilo recall","parameters":{"type":"object","properties":{"query":{"type":"string","description":"Kilo query"}}}}]
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Contains(t, gjson.GetBytes(out, "instructions").String(), "Kilo-Code")
	require.Contains(t, gjson.GetBytes(out, "instructions").String(), "Keep repository rules.")
	require.Contains(t, gjson.GetBytes(out, "input.0.content.0.text").String(), "请列出工具名称")
	require.Contains(t, gjson.GetBytes(out, "tools.0.description").String(), "Kilo")
	require.Contains(t, gjson.GetBytes(out, "tools.0.parameters.properties.query.description").String(), "Kilo")
	require.Equal(t, "kilo_local_recall", gjson.GetBytes(out, "tools.0.name").String(), "OpenAI 侧暂不改工具名，避免断开客户端执行链")
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyNormalizesBareRoleContentMessage(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"instructions":"You are Kilo-Code. Keep repository rules.",
		"input":[
			{"role":"developer","content":"Kilo developer instructions"},
			{"role":"user","content":[{"type":"input_text","text":"hello"}]}
		],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Equal(t, "message", gjson.GetBytes(out, "input.0.type").String())
	require.Equal(t, "developer", gjson.GetBytes(out, "input.0.role").String())
	require.Equal(t, "input_text", gjson.GetBytes(out, "input.0.content.0.type").String())
	require.Equal(t, "Kilo developer instructions", gjson.GetBytes(out, "input.0.content.0.text").String())
	require.Equal(t, "message", gjson.GetBytes(out, "input.1.type").String())
	require.Equal(t, "user", gjson.GetBytes(out, "input.1.role").String())
	require.Equal(t, "hello", gjson.GetBytes(out, "input.1.content.0.text").String())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyConvertsSystemRoleToDeveloper(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"instructions":"Keep repository rules.",
		"input":[
			{"type":"message","role":"system","content":[{"type":"input_text","text":"Project system prompt"}]},
			{"role":"system","content":"Bare system prompt"},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}
		],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Equal(t, "developer", gjson.GetBytes(out, "input.0.role").String())
	require.Equal(t, "Project system prompt", gjson.GetBytes(out, "input.0.content.0.text").String())
	require.Equal(t, "message", gjson.GetBytes(out, "input.1.type").String())
	require.Equal(t, "developer", gjson.GetBytes(out, "input.1.role").String())
	require.Equal(t, "Bare system prompt", gjson.GetBytes(out, "input.1.content.0.text").String())
	require.Equal(t, "user", gjson.GetBytes(out, "input.2.role").String())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyPreservesCursorIdentityText(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"instructions":"You are running as a coding agent in Cursor IDE on a user's computer.",
		"input":[{"role":"user","content":"Terminals folder: /Users/czs/.cursor/projects/demo/terminals"}],
		"tools":[{"type":"function","name":"rg","description":"Search the workspace; respects .cursorignore","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Defaults to Cursor workspace root."}}}}],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Contains(t, gjson.GetBytes(out, "instructions").String(), "Cursor IDE")
	require.Equal(t, "message", gjson.GetBytes(out, "input.0.type").String())
	require.Contains(t, gjson.GetBytes(out, "input.0.content.0.text").String(), "/Users/czs/.cursor/projects/demo/terminals")
	require.Contains(t, gjson.GetBytes(out, "tools.0.description").String(), ".cursorignore")
	require.Contains(t, gjson.GetBytes(out, "tools.0.parameters.properties.path.description").String(), "Cursor workspace root")
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyAddsPromptCacheKey(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"instructions":"You are Kilo-Code. Keep repository rules.",
		"input":[
			{"role":"developer","content":"Kilo developer instructions"},
			{"role":"user","content":[{"type":"input_text","text":"hello"}]}
		],
		"reasoning":{"effort":"xhigh"},
		"text":{"verbosity":"low"},
		"tools":[{"type":"function","name":"kilo_local_recall","description":"Kilo recall","parameters":{"type":"object"}}],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)
	key := gjson.GetBytes(out, "prompt_cache_key").String()

	require.NotEmpty(t, key)
	require.Regexp(t, openAICodexUUIDPattern, key)
	require.Equal(t, key, gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body), "prompt_cache_key").String())
	require.Equal(t, key, gjson.GetBytes(out, "client_metadata.session_id").String())
	require.Equal(t, key, gjson.GetBytes(out, "client_metadata.thread_id").String())
	require.Equal(t, key+":0", gjson.GetBytes(out, "client_metadata.x-codex-window-id").String())
	require.NotEmpty(t, gjson.GetBytes(out, "client_metadata.x-codex-installation-id").String())
	require.Contains(t, gjson.GetBytes(out, "client_metadata.x-codex-turn-metadata").String(), `"request_kind":"turn"`)
	require.Contains(t, gjson.GetBytes(out, "instructions").String(), "Kilo-Code")
	require.Equal(t, "xhigh", gjson.GetBytes(out, "reasoning.effort").String())
	require.Equal(t, "low", gjson.GetBytes(out, "text.verbosity").String())
	require.Contains(t, gjson.GetBytes(out, "tools.0.description").String(), "Kilo")
	require.Equal(t, "kilo_local_recall", gjson.GetBytes(out, "tools.0.name").String())
	require.False(t, gjson.GetBytes(out, "tools.0.strict").Exists(), "这一步不补 tools strict")
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyOverridesConflictingDesktopMetadata(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"prompt_cache_key":"client-cache-key",
		"client_metadata":{
			"session_id":"client-session",
			"thread_id":"client-thread",
			"turn_id":"client-turn",
			"x-codex-window-id":"client-window",
			"x-codex-turn-metadata":"client-metadata"
		},
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)
	key := gjson.GetBytes(out, "prompt_cache_key").String()

	require.NotEmpty(t, key)
	require.NotEqual(t, "client-cache-key", key)
	require.Equal(t, key, gjson.GetBytes(out, "client_metadata.session_id").String())
	require.Equal(t, key, gjson.GetBytes(out, "client_metadata.thread_id").String())
	require.NotEqual(t, "client-turn", gjson.GetBytes(out, "client_metadata.turn_id").String())
	require.Equal(t, key+":0", gjson.GetBytes(out, "client_metadata.x-codex-window-id").String())
	require.Contains(t, gjson.GetBytes(out, "client_metadata.x-codex-turn-metadata").String(), `"request_kind":"turn"`)
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyAddsDefaultInstructions(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"input":[{"role":"user","content":"hello"}],
		"stream":false
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.NotEmpty(t, strings.TrimSpace(gjson.GetBytes(out, "instructions").String()))
	require.True(t, gjson.GetBytes(out, "stream").Bool())
	require.False(t, gjson.GetBytes(out, "store").Bool())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(out, "include.0").String())
	require.Equal(t, "message", gjson.GetBytes(out, "input.0.type").String())
	require.Regexp(t, openAICodexUUIDPattern, gjson.GetBytes(out, "prompt_cache_key").String())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyCLIRSProfileUsesLegacyPromptCacheKey(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)
	scope := openAIAPIKeyCodexMimicScope{ClientProfile: openAIAPIKeyCodexMimicClientCLIRS0125}

	out := applyOpenAIAPIKeyCodexMimicryToBody(body, scope)

	require.True(t, strings.HasPrefix(gjson.GetBytes(out, "prompt_cache_key").String(), "codex-mimic-"))
	require.False(t, gjson.GetBytes(out, "client_metadata").Exists())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyAppendsIncludeInsteadOfReplacing(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"include":["file_search_call.results"],
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Equal(t, "file_search_call.results", gjson.GetBytes(out, "include.0").String())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(out, "include.1").String())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyPreservesPromptCacheKey(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"prompt_cache_key":"client-cache-key",
		"instructions":"You are Kilo-Code.",
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)

	out := applyOpenAIAPIKeyCodexMimicryToBody(body)

	require.Regexp(t, openAICodexUUIDPattern, gjson.GetBytes(out, "prompt_cache_key").String())
	require.NotEqual(t, "client-cache-key", gjson.GetBytes(out, "prompt_cache_key").String())
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyScopesDerivedPromptCacheKey(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)

	scopeA := openAIAPIKeyCodexMimicScope{
		AccountID:       11,
		APIKeyID:        101,
		UpstreamBaseURL: "https://api.openai.com",
	}
	scopeB := openAIAPIKeyCodexMimicScope{
		AccountID:       11,
		APIKeyID:        202,
		UpstreamBaseURL: "https://api.openai.com",
	}

	keyA := gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body, scopeA), "prompt_cache_key").String()
	keyB := gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body, scopeB), "prompt_cache_key").String()

	require.NotEmpty(t, keyA)
	require.NotEmpty(t, keyB)
	require.NotEqual(t, keyA, keyB)
}

func TestResolveOpenAIAPIKeyCodexMimicScopeIncludesServerSalt(t *testing.T) {
	account := &Account{ID: 11}
	cfg := &config.Config{JWT: config.JWTConfig{Secret: "jwt-secret-for-mimic-salt"}}

	scope := resolveOpenAIAPIKeyCodexMimicScope(account, 101, cfg)

	require.NotEmpty(t, scope.ServerSalt)
	require.NotContains(t, scope.ServerSalt, "jwt-secret-for-mimic-salt")
}

func TestApplyOpenAIAPIKeyCodexMimicryToBodyServerSaltAffectsDerivedPromptCacheKey(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"input":[{"role":"user","content":"hello"}],
		"stream":true
	}`)

	scopeA := openAIAPIKeyCodexMimicScope{
		AccountID:       11,
		APIKeyID:        101,
		UpstreamBaseURL: "https://api.openai.com",
		ServerSalt:      "salt-a",
	}
	scopeB := openAIAPIKeyCodexMimicScope{
		AccountID:       11,
		APIKeyID:        101,
		UpstreamBaseURL: "https://api.openai.com",
		ServerSalt:      "salt-b",
	}

	keyA := gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body, scopeA), "prompt_cache_key").String()
	keyB := gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body, scopeB), "prompt_cache_key").String()

	require.NotEmpty(t, keyA)
	require.NotEmpty(t, keyB)
	require.NotEqual(t, keyA, keyB)
}
