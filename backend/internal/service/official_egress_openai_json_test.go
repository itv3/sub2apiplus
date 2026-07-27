package service

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestMarshalOfficialOpenAIHTTPJSONUsesCodexFieldOrder(t *testing.T) {
	payload := map[string]any{
		"client_metadata": map[string]any{"session_id": "s"},
		"input":           []any{},
		"model":           "gpt-5.6",
		"reasoning":       map[string]any{"effort": "high"},
		"service_tier":    "priority",
	}
	body, err := marshalOfficialOpenAIHTTPJSON(payload, false)
	require.NoError(t, err)
	text := string(body)
	require.Less(t, strings.Index(text, `"model"`), strings.Index(text, `"input"`))
	require.Less(t, strings.Index(text, `"input"`), strings.Index(text, `"reasoning"`))
	require.Less(t, strings.Index(text, `"reasoning"`), strings.Index(text, `"service_tier"`))
	require.Less(t, strings.Index(text, `"service_tier"`), strings.Index(text, `"client_metadata"`))
}

func TestMarshalOfficialOrderedJSONPreservesNestedRawBytesAndLargeInteger(t *testing.T) {
	original := []byte(`{"input":[{"type":"message","payload":{"z":9007199254740993,"a":1}}],"model":"gpt-5.6-luna","client_extension":{"b":2,"a":1}}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	payload["stream"] = true

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(payload, false, original)
	require.NoError(t, err)
	require.Contains(t, string(body), `"payload":{"z":9007199254740993,"a":1}`)
	require.Contains(t, string(body), `"client_extension":{"b":2,"a":1}`)
	require.NotContains(t, string(body), "9007199254740992")
	require.True(t, json.Valid(body))
}

func TestMarshalOfficialOrderedJSONPreservesUnchangedSiblingsDuringNestedEdit(t *testing.T) {
	original := []byte(`{"model":"gpt-5.6-luna","input":[{"role":"user","content":[{"type":"input_text","opaque":{"z":3,"a":1}}]}]}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	input, ok := payload["input"].([]any)
	require.True(t, ok)
	item, ok := input[0].(map[string]any)
	require.True(t, ok)
	item["type"] = "message"

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(payload, false, original)
	require.NoError(t, err)
	require.Contains(t, string(body), `"opaque":{"z":3,"a":1}`)
	require.Contains(t, string(body), `"type":"message"`)
}

func TestMarshalOfficialOrderedJSONPreservesRawObjectMovedAcrossTopLevelFields(t *testing.T) {
	original := []byte(`{"model":"gpt-5.6-luna","tools":[{"type":"function","name":"lookup","parameters":{"z":9007199254740993,"a":1}}],"input":[]}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	tools, ok := payload["tools"].([]any)
	require.True(t, ok)
	delete(payload, "tools")
	payload["input"] = []any{map[string]any{
		"type":  "additional_tools",
		"tools": tools,
	}}

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(payload, false, original)
	require.NoError(t, err)
	require.Contains(t, string(body), `"parameters":{"z":9007199254740993,"a":1}`)
	require.NotContains(t, string(body), "9007199254740992")
}

func TestMarshalOfficialOpenAICompactJSONUsesCompactionOrder(t *testing.T) {
	payload := map[string]any{
		"text":             map[string]any{"verbosity": "low"},
		"prompt_cache_key": "cache",
		"instructions":     "compact",
		"input":            []any{},
		"model":            "gpt-5.6",
	}
	body, err := marshalOfficialOpenAIHTTPJSON(payload, true)
	require.NoError(t, err)
	require.Equal(t, `{"model":"gpt-5.6","input":[],"instructions":"compact","prompt_cache_key":"cache","text":{"verbosity":"low"}}`, string(body))
}
