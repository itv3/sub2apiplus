package service

import (
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestDecideResponsesProbeSupport(t *testing.T) {
	fnCall := []byte(`{"output":[{"type":"reasoning"},{"type":"function_call","name":"probe_ping"}]}`)
	reasoningOnly := []byte(`{"output":[{"type":"reasoning"}]}`)

	cases := []struct {
		name   string
		status int
		body   []byte
		want   bool
	}{
		// Endpoint clearly absent on third-party OpenAI-compatible upstreams.
		{"404 endpoint absent", 404, fnCall, false},
		{"405 method not allowed", 405, fnCall, false},
		// 2xx: tool capability is judged by presence of a function_call output item.
		{"200 with function_call", 200, fnCall, true},
		// Volcengine Ark coding/v3 × kimi-k2.6: reasoning only, no function_call.
		{"200 reasoning only", 200, reasoningOnly, false},
		{"200 invalid json", 200, []byte("not-json"), false},
		{"200 no output field", 200, []byte(`{"status":"completed"}`), false},
		// Non-2xx (other than 404/405): endpoint exists, capability undecidable -> conservative true.
		{"400 conservative true", 400, reasoningOnly, true},
		{"401 conservative true", 401, nil, true},
		{"500 conservative true", 500, nil, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, decideResponsesProbeSupport(tc.status, tc.body))
		})
	}
}

func TestResponsesProbeBodyHasFunctionCall(t *testing.T) {
	require.True(t, responsesProbeBodyHasFunctionCall([]byte(`{"output":[{"type":"function_call"}]}`)))
	require.True(t, responsesProbeBodyHasFunctionCall([]byte(`{"output":[{"type":"reasoning"},{"type":"function_call"}]}`)))
	require.True(t, responsesProbeBodyHasFunctionCall([]byte("event: response.output_item.added\n"+
		"data: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"function_call\",\"name\":\"probe_ping\"}}\n\n"+
		"data: [DONE]\n\n")))
	require.True(t, responsesProbeBodyHasFunctionCall([]byte("data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\"}]}}\n\n")))
	require.False(t, responsesProbeBodyHasFunctionCall([]byte(`{"output":[{"type":"reasoning"}]}`)))
	require.False(t, responsesProbeBodyHasFunctionCall([]byte("event: response.output_item.added\n"+
		"data: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"reasoning\"}}\n\n"+
		"data: [DONE]\n\n")))
	require.False(t, responsesProbeBodyHasFunctionCall([]byte(`{"output":[]}`)))
	require.False(t, responsesProbeBodyHasFunctionCall([]byte(`{}`)))
	require.False(t, responsesProbeBodyHasFunctionCall([]byte(`garbage`)))
}

func TestSelectResponsesProbeModel(t *testing.T) {
	// No model_mapping -> fall back to DefaultTestModel (OpenAI official APIKey).
	require.Equal(t, openai.DefaultTestModel, selectResponsesProbeModel(&Account{}))

	// model_mapping values are upstream models; pick first by sort for reproducibility.
	acct := &Account{Credentials: map[string]any{
		"model_mapping": map[string]any{
			"client-b": "zeta-model",
			"client-a": "alpha-model",
		},
	}}
	require.Equal(t, "alpha-model", selectResponsesProbeModel(acct))

	// Wildcard / blank upstream values are skipped.
	acctWild := &Account{Credentials: map[string]any{
		"model_mapping": map[string]any{
			"a": "*",
			"b": "  ",
			"c": "real-model",
		},
	}}
	require.Equal(t, "real-model", selectResponsesProbeModel(acctWild))

	// Only wildcard mappings -> DefaultTestModel.
	acctAllWild := &Account{Credentials: map[string]any{
		"model_mapping": map[string]any{"a": "gpt-*"},
	}}
	require.Equal(t, openai.DefaultTestModel, selectResponsesProbeModel(acctAllWild))
}

func TestBuildOpenAIResponsesProbeRequest_MimicUsesCodexHeadersAndBody(t *testing.T) {
	account := &Account{
		ID:       71,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
		},
		Credentials: map[string]any{
			"base_url": "https://compat-upstream.example",
		},
	}

	req, body, err := buildOpenAIResponsesProbeRequest(account, &config.Config{JWT: config.JWTConfig{Secret: "probe-test-secret"}}, "https://compat-upstream.example/v1/responses", "sk-test", "gpt-5.5")
	require.NoError(t, err)
	require.NotNil(t, req)
	require.Equal(t, codexDesktopUserAgent, req.Header.Get("User-Agent"))
	require.Equal(t, codexDesktopOriginator, req.Header.Get("originator"))
	require.Empty(t, req.Header.Get("OpenAI-Beta"))
	require.Empty(t, req.Header.Get("version"))
	require.Equal(t, "text/event-stream", req.Header.Get("Accept"))
	require.Equal(t, req.Header.Get("session-id"), req.Header.Get("thread-id"))
	require.Equal(t, req.Header.Get("session-id")+":0", req.Header.Get("x-codex-window-id"))
	require.Equal(t, codexDesktopBetaFeatures, req.Header.Get("x-codex-beta-features"))
	require.True(t, gjson.GetBytes(body, "stream").Bool())
	require.Regexp(t, openAICodexUUIDPattern, gjson.GetBytes(body, "prompt_cache_key").String())
	require.NotEmpty(t, strings.TrimSpace(gjson.GetBytes(body, "instructions").String()))
}
