package service

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
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
	body, err := marshalOfficialOpenAIHTTPJSON(officialClientProfileModeActive, payload, false)
	require.NoError(t, err)
	text := string(body)
	require.Less(t, strings.Index(text, `"model"`), strings.Index(text, `"input"`))
	require.Less(t, strings.Index(text, `"input"`), strings.Index(text, `"reasoning"`))
	require.Less(t, strings.Index(text, `"reasoning"`), strings.Index(text, `"service_tier"`))
	require.Less(t, strings.Index(text, `"service_tier"`), strings.Index(text, `"client_metadata"`))
}

func TestMarshalOfficialOpenAIWSJSONConsumesFrozenProfile(t *testing.T) {
	raw, err := os.ReadFile(
		"../officialegress/profilecontract/testdata/snapshots/0.145.0/" +
			"e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b.json",
	)
	require.NoError(t, err)
	activeDoc, err := profilecontract.ParseSnapshot(raw)
	require.NoError(t, err)
	previousDoc, err := profilecontract.ParseSnapshot(raw)
	require.NoError(t, err)
	for index := range previousDoc.Endpoints {
		endpoint := &previousDoc.Endpoints[index]
		if endpoint.ID == officialCodexEndpointResponsesWS && len(endpoint.Body.Fields) > 1 {
			endpoint.Body.Fields[0], endpoint.Body.Fields[1] =
				endpoint.Body.Fields[1], endpoint.Body.Fields[0]
		}
	}
	previousDoc.Digest = strings.Repeat("a", 64)
	activeProfile, err := profilecontract.NewProfileSpec(activeDoc)
	require.NoError(t, err)
	previousProfile, err := profilecontract.NewProfileSpec(previousDoc)
	require.NoError(t, err)
	activeExecutable, err := profilecontract.CompileExecutableProfile(activeProfile)
	require.NoError(t, err)
	previousExecutable, err := profilecontract.CompileExecutableProfile(previousProfile)
	require.NoError(t, err)
	payload := map[string]any{
		"type": "response.create", "model": "m", "input": []any{},
	}
	activeBody, err := marshalOfficialOpenAIWSJSONFromProfile(activeExecutable, payload)
	require.NoError(t, err)
	previousBody, err := marshalOfficialOpenAIWSJSONFromProfile(previousExecutable, payload)
	require.NoError(t, err)
	require.NotEqual(t, string(activeBody), string(previousBody))
	require.Less(t, strings.Index(string(activeBody), `"type"`), strings.Index(string(activeBody), `"model"`))
	require.Less(t, strings.Index(string(previousBody), `"model"`), strings.Index(string(previousBody), `"type"`))
}

func TestMarshalOfficialOrderedJSONPreservesNestedRawBytesAndLargeInteger(t *testing.T) {
	original := []byte(`{"input":[{"type":"message","payload":{"z":9007199254740993,"a":1}}],"model":"gpt-5.6-luna","client_extension":{"b":2,"a":1}}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	payload["stream"] = true

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(
		officialClientProfileModeActive, payload, false, original,
	)
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

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(
		officialClientProfileModeActive, payload, false, original,
	)
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

	body, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(
		officialClientProfileModeActive, payload, false, original,
	)
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
	body, err := marshalOfficialOpenAIHTTPJSON(officialClientProfileModeActive, payload, true)
	require.NoError(t, err)
	require.Equal(t, `{"model":"gpt-5.6","input":[],"instructions":"compact","prompt_cache_key":"cache","text":{"verbosity":"low"}}`, string(body))
}
