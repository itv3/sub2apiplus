package service

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestApplyAnthropicAPIKeyOfficialIdentityMimicryToBodySanitizesKiloIdentity(t *testing.T) {
	body := []byte(`{
		"model":"claude-opus-4-8",
		"system":"You are Kilo, an interactive CLI coding agent.\nKeep project rules.",
		"messages":[{"role":"user","content":[{"type":"text","text":"你是什么模型？"}]}],
		"tools":[{"name":"kilo_local_recall","description":"Kilo local recall tool","input_schema":{"type":"object","properties":{"query":{"type":"string","description":"Kilo query"}}}}]
	}`)

	out := applyAnthropicAPIKeyOfficialIdentityMimicryToBody(body)

	require.Contains(t, gjson.GetBytes(out, "system").String(), "You are Claude Code")
	require.Contains(t, gjson.GetBytes(out, "system").String(), "Keep project rules.")
	require.Contains(t, gjson.GetBytes(out, "messages.0.content.0.text").String(), "你是什么模型？")
	require.NotContains(t, gjson.GetBytes(out, "system").String(), "Kilo")
	require.Contains(t, gjson.GetBytes(out, "tools.0.description").String(), "Kilo")
	require.Contains(t, gjson.GetBytes(out, "tools.0.input_schema.properties.query.description").String(), "Kilo")
	require.Equal(t, "kilo_local_recall", gjson.GetBytes(out, "tools.0.name").String(), "身份净化不在 helper 层改工具名")
}

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
	require.True(t, strings.HasPrefix(key, "codex-mimic-"))
	require.Equal(t, key, gjson.GetBytes(applyOpenAIAPIKeyCodexMimicryToBody(body), "prompt_cache_key").String())
	require.Contains(t, gjson.GetBytes(out, "instructions").String(), "Kilo-Code")
	require.Equal(t, "xhigh", gjson.GetBytes(out, "reasoning.effort").String())
	require.Equal(t, "low", gjson.GetBytes(out, "text.verbosity").String())
	require.Contains(t, gjson.GetBytes(out, "tools.0.description").String(), "Kilo")
	require.Equal(t, "kilo_local_recall", gjson.GetBytes(out, "tools.0.name").String())
	require.False(t, gjson.GetBytes(out, "tools.0.strict").Exists(), "这一步不补 tools strict")
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

	require.Equal(t, "client-cache-key", gjson.GetBytes(out, "prompt_cache_key").String())
}
