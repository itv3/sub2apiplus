package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func TestOpenAIWSOrphanOutputIndexMatchesDecodedCleanup(t *testing.T) {
	fixtures := []string{
		`{"input":[{"type":"custom_tool_call","call_id":"c"},{"type":"custom_tool_call_output","call_id":"c","output":"ok"}]}`,
		`{"input":[{"type":"function_call_output","name":"task","output":"具名输入"}]}`,
		`{"input":[{"type":"function_call_output","call_id":"c"},{"type":"tool_call","id":"c"}]}`,
		`{"input":[{"type":"function_call_output","call_id":"c"},{"type":"item_reference","id":"c"}]}`,
		`{"input":[{"type":"function_call_output","call_id":"c"}],"previous_response_id":"resp_previous"}`,
		`{"input":[{"type":"custom_tool_call","call_id":"old","call_id":"c"},{"type":"custom_tool_call_output","call_id":"c"}]}`,
		`{"input":[],"input":[{"type":"function_call_output","call_id":"unknown"}]}`,
		`{"input":[{"type":"function_call_output","call_id":1},{"type":"tool_call","call_id":1}]}`,
		`{"input":[{"type":"function_call_output","call_id":"c"},{"type":"tool_call","call_id":" ","id":"c"}]}`,
	}
	rng := rand.New(rand.NewSource(20261006))
	types := []string{"function_call", "custom_tool_call", "tool_call", "local_shell_call", "mcp_tool_call", "tool_search_call", "item_reference", "function_call_output", "custom_tool_call_output", "tool_search_output", "mcp_tool_call_output", "message", "other"}
	values := []any{"a", "b", " a ", "", " ", nil, 1, false}
	for i := 0; i < 250; i++ {
		items := make([]any, rng.Intn(20))
		for j := range items {
			items[j] = map[string]any{
				"type": types[rng.Intn(len(types))], "call_id": values[rng.Intn(len(values))],
				"id": values[rng.Intn(len(values))], "name": values[rng.Intn(len(values))],
				"output": "只读工具输出",
			}
		}
		body, err := json.Marshal(map[string]any{"input": items})
		require.NoError(t, err)
		fixtures = append(fixtures, string(body))
	}
	for i, fixture := range fixtures {
		var decoded map[string]any
		require.NoError(t, decodeOpenAIJSONUseNumber([]byte(fixture), &decoded))
		input, _ := decoded["input"].([]any)
		want := sanitizeOpenAIResponsesOrphanToolOutputs(decoded, input, firstNonEmptyString(decoded["previous_response_id"]) != "")
		index, err := buildOfficialJSONRawIndexForDecode([]byte(fixture))
		require.NoError(t, err)
		require.Equal(t, want, openAIResponsesHasOrphanToolOutputsFromIndex(index), "fixture=%d", i)
	}
}

func TestOpenAIWSExplicitToolDeclarationViewMatchesLegacy(t *testing.T) {
	for _, body := range []string{
		`{"tools":null}`, `{"input":null}`, `{"input":"text"}`, `{"input":[1,null]}`,
		`{"input":{"type":"additional_tools","tools":[]}}`,
		`{"input":[{"type":" ADDITIONAL_TOOLS ","tools":null}]}`,
		`{"input":[{"type":"additional_tools","type":"message","tools":[]}]}`,
		`{"input":[{"type":"message","content":"large"},{"type":"additional_tools","tools":["f"]}]}`,
	} {
		want := gjson.Get(body, "tools").Exists()
		for _, item := range gjson.Get(body, "input").Array() {
			if strings.EqualFold(strings.TrimSpace(item.Get("type").String()), "additional_tools") && item.Get("tools").Exists() {
				want = true
			}
		}
		require.Equal(t, want, openAIWSFrameHasExplicitToolDeclarations([]byte(body)), body)
	}
}

func TestOpenAIWSAccountIdentityRawOptimizationMatchesLegacy(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)
	fixtures := [][]byte{
		buildOfficialEgressMemoryProfileBody(t, 128<<10),
		[]byte(`{ "client_metadata" : {"session_id":"tiny","thread_id":"tiny"}, "prompt_cache_key" : "tiny", "input" : [] }`),
		[]byte(`{"client_metadata":{"session_id":"long-client-session-name-long-client-session-name-long-client-session-name"},"prompt_cache_key":"long-client-session-name-long-client-session-name-long-client-session-name"}`),
		[]byte(`{"client_metadata":{"session_id":"quote\"slash\\"},"prompt_cache_key":"quote\"slash\\"}`),
		[]byte(`{"prompt_cache_key":"first","prompt_cache_key":"last","client_metadata":{"session_id":"first"}}`),
		[]byte(`{"client_metadata":null,"prompt_cache_key":"standalone"}`),
		[]byte(`{"client_metadata":[],"prompt_cache_key":null}`), []byte(`[]`), []byte(`null`),
	}
	for i, fixture := range fixtures {
		original := bytes.Clone(fixture)
		want, wantChanged, wantErr := legacyOpenAIWSAccountIdentityRawForMemoryTest(fixture, account, 77)
		got, gotChanged, gotErr := applyCodexAccountIdentityClientMetadataRaw(fixture, account, 77)
		require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), "fixture=%d", i)
		require.Equal(t, wantChanged, gotChanged, "fixture=%d", i)
		require.Equal(t, want, got, "身份字段的字节布局必须保持原样，fixture=%d", i)
		require.Equal(t, original, fixture, "身份改写不得修改换号和审计仍在使用的原文")
	}
}

// 保留优化前的两次 sjson 写入作为线形基准，验证独占新缓冲替换不会改变格式或入口原文。
func legacyOpenAIWSAccountIdentityRawForMemoryTest(body []byte, account *Account, apiKeyID int64) ([]byte, bool, error) {
	if len(body) == 0 || codexAccountIdentityNamespace(account) == "" || !gjson.ParseBytes(body).IsObject() {
		return body, false, nil
	}
	next, changed, originalBodySessionID := body, false, ""
	if cm := gjson.GetBytes(body, "client_metadata"); cm.IsObject() {
		metadata := map[string]any{}
		if err := json.Unmarshal([]byte(cm.Raw), &metadata); err != nil {
			return body, false, fmt.Errorf("decode client_metadata for account identity: %w", err)
		}
		originalBodySessionID, _ = metadata["session_id"].(string)
		metadataChanged := applyCodexAccountIdentityFields(metadata, account, apiKeyID)
		if applyCodexAccountIdentityEmbeddedMetadata(metadata, account, apiKeyID) {
			metadataChanged = true
		}
		if metadataChanged {
			raw, err := json.Marshal(metadata)
			if err != nil {
				return body, false, fmt.Errorf("encode account-scoped client_metadata: %w", err)
			}
			next, err = sjson.SetRawBytes(next, "client_metadata", raw)
			if err != nil {
				return body, false, fmt.Errorf("splice account-scoped client_metadata: %w", err)
			}
			changed = true
		}
	}
	if key := gjson.GetBytes(body, "prompt_cache_key"); key.Type == gjson.String && strings.TrimSpace(key.String()) != "" {
		raw, kind := key.String(), "prompt-cache"
		if strings.TrimSpace(originalBodySessionID) != "" && raw == originalBodySessionID {
			kind = "session"
		}
		if scoped := scopeCodexAccountIdentityValue(account, apiKeyID, kind, raw); scoped != raw {
			var err error
			next, err = sjson.SetBytes(next, "prompt_cache_key", scoped)
			if err != nil {
				return body, false, fmt.Errorf("splice account-scoped prompt_cache_key: %w", err)
			}
			changed = true
		}
	}
	return next, changed, nil
}
