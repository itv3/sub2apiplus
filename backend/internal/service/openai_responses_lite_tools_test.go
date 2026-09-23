//go:build unit

package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestNormalizeOpenAIResponsesLiteTools_MovesNamespacesAndKeepsSupportedTools(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-terra",
		"tools": []any{
			map[string]any{"type": "function", "name": "shell"},
			map[string]any{"type": "custom", "name": "exec"},
			map[string]any{"type": "tool_search"},
			map[string]any{"type": "namespace", "name": "collaboration", "tools": []any{
				map[string]any{"type": "function", "name": "spawn_agent"},
			}},
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
			map[string]any{"type": "additional_tools", "role": "developer", "tools": []any{
				map[string]any{"type": "namespace", "name": "image_gen"},
				map[string]any{"type": "namespace", "name": "collaboration", "tools": []any{
					map[string]any{"type": "function", "name": "spawn_agent"},
				}},
			}},
		},
		"tool_choice": map[string]any{"type": "namespace", "name": "collaboration"},
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.NoError(t, err)
	require.True(t, changed)
	tools := requireType[[]any](t, reqBody["tools"])
	require.Len(t, tools, 3)
	require.Equal(t, "function", requireType[map[string]any](t, tools[0])["type"])
	require.Equal(t, "custom", requireType[map[string]any](t, tools[1])["type"])
	require.Equal(t, "tool_search", requireType[map[string]any](t, tools[2])["type"])
	input := requireType[[]any](t, reqBody["input"])
	require.Len(t, input, 2)
	additional := requireType[[]any](t, requireType[map[string]any](t, input[1])["tools"])
	require.Len(t, additional, 2)
	require.Equal(t, "image_gen", requireType[map[string]any](t, additional[0])["name"])
	require.Equal(t, "collaboration", requireType[map[string]any](t, additional[1])["name"], "existing namespace must not be duplicated")
	require.Equal(t, map[string]any{"type": "namespace", "name": "collaboration"}, reqBody["tool_choice"])
}

func TestNormalizeOpenAIResponsesLiteTools_PreservesDeferredFlagsWithToolSearch(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{"type": "tool_search"},
			map[string]any{"type": "function", "name": "shell", "defer_loading": true},
		},
	}

	_, err := normalizeOpenAIResponsesLiteTools(reqBody)
	require.NoError(t, err)
	tools := requireType[[]any](t, reqBody["tools"])
	require.Equal(t, "tool_search", requireType[map[string]any](t, tools[0])["type"])
	require.Equal(t, true, requireType[map[string]any](t, tools[1])["defer_loading"])
}

func TestNormalizeOpenAIResponsesLiteTools_RejectsConflictingAdditionalTool(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{map[string]any{
			"type":  "namespace",
			"name":  "collaboration",
			"tools": []any{map[string]any{"type": "function", "name": "spawn_agent"}},
		}},
		"input": []any{map[string]any{
			"type": "additional_tools",
			"tools": []any{map[string]any{
				"type":  "namespace",
				"name":  "collaboration",
				"tools": []any{map[string]any{"type": "function", "name": "send_message"}},
			}},
		}},
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.ErrorContains(t, err, `conflicts with migrated tool type "namespace" name "collaboration"`)
	require.False(t, changed)
	require.Len(t, reqBody["tools"], 1, "conflicts must not partially remove top-level tools")
}

func TestNormalizeOpenAIResponsesLiteTools_DeduplicatesAcrossAdditionalToolItems(t *testing.T) {
	namespace := map[string]any{
		"type":  "namespace",
		"name":  "collaboration",
		"tools": []any{map[string]any{"type": "function", "name": "spawn_agent"}},
	}
	reqBody := map[string]any{
		"tools": []any{namespace},
		"input": []any{
			map[string]any{
				"type":  "additional_tools",
				"tools": []any{map[string]any{"type": "custom", "name": "exec"}},
			},
			map[string]any{
				"type":  "additional_tools",
				"tools": []any{namespace},
			},
		},
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.NoError(t, err)
	require.True(t, changed)
	require.NotContains(t, reqBody, "tools")
	input := requireType[[]any](t, reqBody["input"])
	require.Len(t, requireType[map[string]any](t, input[0])["tools"], 1)
	require.Len(t, requireType[map[string]any](t, input[1])["tools"], 1)
}

func TestNormalizeOpenAIResponsesLiteTools_ConvertsStringInput(t *testing.T) {
	reqBody := map[string]any{
		"input": "hello",
		"tools": []any{map[string]any{
			"type": "namespace",
			"name": "collaboration",
		}},
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.NoError(t, err)
	require.True(t, changed)
	require.NotContains(t, reqBody, "tools")
	input := requireType[[]any](t, reqBody["input"])
	require.Len(t, input, 2)
	require.Equal(t, "message", requireType[map[string]any](t, input[0])["type"])
	require.Equal(t, "hello", requireType[map[string]any](t, input[0])["content"])
	require.Equal(t, "additional_tools", requireType[map[string]any](t, input[1])["type"])
}

func TestNormalizeOpenAIResponsesLiteTools_KeepsSupportedTopLevelTools(t *testing.T) {
	reqBody := map[string]any{
		"reasoning": map[string]any{"context": "all_turns"},
		"tools": []any{
			map[string]any{"type": "function", "name": "shell"},
			map[string]any{"type": "custom", "name": "exec"},
			map[string]any{"type": "tool_search"},
			"custom shorthand",
		},
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.NoError(t, err)
	require.True(t, changed)
	require.Len(t, reqBody["tools"], 4)
	require.Equal(t, false, reqBody["parallel_tool_calls"])
}

func TestNormalizeOpenAIResponsesLiteTools_ForcesParallelToolCallsFalse(t *testing.T) {
	tests := []struct {
		name string
		body map[string]any
	}{
		{
			name: "top-level tools",
			body: map[string]any{
				"tools":               []any{map[string]any{"type": "function", "name": "shell"}},
				"parallel_tool_calls": true,
			},
		},
		{
			name: "input additional tools",
			body: map[string]any{
				"input": []any{map[string]any{
					"type":  "additional_tools",
					"tools": []any{map[string]any{"type": "namespace", "name": "collaboration"}},
				}},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			changed, err := normalizeOpenAIResponsesLiteTools(tt.body)

			require.NoError(t, err)
			require.True(t, changed)
			require.Equal(t, false, tt.body["parallel_tool_calls"])
		})
	}
}

func TestNormalizeOpenAIResponsesLiteTools_PinsParallelToolCallsWithoutTools(t *testing.T) {
	tests := []struct {
		name        string
		parallel    any
		include     bool
		wantChanged bool
	}{
		{name: "字段缺失", wantChanged: true},
		{name: "值为 true", parallel: true, include: true, wantChanged: true},
		{name: "值为 false", parallel: false, include: true, wantChanged: false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reqBody := map[string]any{"reasoning": map[string]any{"context": "all_turns"}}
			if tt.include {
				reqBody["parallel_tool_calls"] = tt.parallel
			}

			changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

			require.NoError(t, err)
			require.Equal(t, tt.wantChanged, changed)
			require.Contains(t, reqBody, "parallel_tool_calls")
			require.Equal(t, false, reqBody["parallel_tool_calls"])
		})
	}
}

func TestNormalizeOpenAIResponsesLiteTools_RejectsNonBooleanParallelToolCalls(t *testing.T) {
	for _, value := range []any{"false", float64(0), nil, map[string]any{}} {
		reqBody := map[string]any{
			"tools":               []any{map[string]any{"type": "function", "name": "shell"}},
			"parallel_tool_calls": value,
		}

		changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

		require.ErrorContains(t, err, "parallel_tool_calls to be a boolean")
		require.False(t, changed)
		require.Equal(t, value, reqBody["parallel_tool_calls"])
	}

	reqBody := map[string]any{"parallel_tool_calls": []any{}}
	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)
	require.ErrorContains(t, err, "parallel_tool_calls to be a boolean")
	require.False(t, changed)
}

func TestNormalizeOpenAIResponsesLiteTools_ParallelToolCallsIsIdempotent(t *testing.T) {
	reqBody := map[string]any{
		"reasoning":           map[string]any{"context": "all_turns"},
		"tools":               []any{map[string]any{"type": "function", "name": "shell"}},
		"parallel_tool_calls": true,
	}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(t, false, reqBody["parallel_tool_calls"])

	changed, err = normalizeOpenAIResponsesLiteTools(reqBody)
	require.NoError(t, err)
	require.False(t, changed)
	require.Equal(t, false, reqBody["parallel_tool_calls"])
}

func TestNormalizeOpenAIResponsesLiteTools_EnsuresReasoningContext(t *testing.T) {
	tests := []struct {
		name      string
		reasoning any
	}{
		{name: "missing"},
		{name: "missing context", reasoning: map[string]any{"effort": "high"}},
		{name: "wrong context", reasoning: map[string]any{"effort": "medium", "context": "current_turn"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reqBody := map[string]any{"input": "hello"}
			if tt.reasoning != nil {
				reqBody["reasoning"] = tt.reasoning
			}

			changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

			require.NoError(t, err)
			require.True(t, changed)
			reasoning := requireType[map[string]any](t, reqBody["reasoning"])
			require.Equal(t, "all_turns", reasoning["context"])
			if tt.name != "missing" {
				require.Equal(t, requireType[map[string]any](t, tt.reasoning)["effort"], reasoning["effort"])
			}
		})
	}
}

func TestNormalizeOpenAIResponsesLiteTools_RejectsNonObjectReasoning(t *testing.T) {
	reqBody := map[string]any{"reasoning": "high"}

	changed, err := normalizeOpenAIResponsesLiteTools(reqBody)

	require.ErrorContains(t, err, "reasoning to be an object")
	require.False(t, changed)
	require.Equal(t, "high", reqBody["reasoning"])
}

func TestNormalizeOpenAIResponsesLiteTools_RejectsUnsupportedTools(t *testing.T) {
	tests := []struct {
		name string
		tool map[string]any
		want string
	}{
		{name: "hosted web search", tool: map[string]any{"type": "web_search"}, want: `top-level tool type "web_search"`},
		{name: "hosted image generation", tool: map[string]any{"type": "image_generation"}, want: `top-level tool type "image_generation"`},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reqBody := map[string]any{"tools": []any{tt.tool}}
			changed, err := normalizeOpenAIResponsesLiteTools(reqBody)
			require.ErrorContains(t, err, tt.want)
			require.False(t, changed)
			require.Len(t, reqBody["tools"], 1, "validation errors must not partially mutate tools")
		})
	}
}

func TestOpenAIResponsesLiteRequiresFullResponsesOnlyForHostedHistory(t *testing.T) {
	tests := []struct {
		name string
		body string
		want bool
	}{
		{
			name: "custom tool continuation remains lite",
			body: `{"input":[{"type":"custom_tool_call","call_id":"c1"},{"type":"custom_tool_call_output","call_id":"c1"}]}`,
			want: false,
		},
		{
			name: "tool search continuation remains lite",
			body: `{"input":[{"type":"tool_search_call","call_id":"s1"}]}`,
			want: false,
		},
		{
			name: "hosted web search history requires full responses",
			body: `{"input":[{"type":"web_search_call","id":"ws1"}]}`,
			want: true,
		},
		{
			name: "hosted mcp history requires full responses",
			body: `{"input":[{"type":"mcp_call","id":"mcp1"}]}`,
			want: true,
		},
		{
			name: "nested hosted declaration requires full responses",
			body: `{"tools":[{"type":"namespace","name":"remote","tools":[{"type":"file_search"}]}]}`,
			want: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			require.Equal(t, tt.want, openAIResponsesLiteRequiresFullResponses([]byte(tt.body)))
		})
	}
}

func TestOpenAIGatewayServiceForward_RoutesHostedWebSearchOutOfLite(t *testing.T) {
	gin.SetMode(gin.TestMode)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
	c.Request.Header.Set("User-Agent", "opencode/1.0.0")
	// Hosted 工具必须离开 Lite；固定 HTTP 使本测试只验证能力路由，不拨真实 WS。
	setOfficialCodexForceHTTPFallback(c, true)

	tools := make([]any, 0, 20)
	for range 19 {
		tools = append(tools, map[string]any{
			"type":       "function",
			"name":       "local_tool",
			"parameters": map[string]any{"type": "object"},
		})
	}
	tools = append(tools, map[string]any{"type": "web_search"})
	body, err := json.Marshal(map[string]any{
		"model":        "gpt-5.6-sol",
		"stream":       true,
		"instructions": "test",
		"input":        "hello",
		"tools":        tools,
	})
	require.NoError(t, err)

	upstream := &httpUpstreamRecorder{resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_hosted_web_search")}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	svc.openaiModelCapabilities.replaceFromManifest(502, []byte(
		`{"models":[{"slug":"gpt-5.6-sol","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true}]}`,
	))
	account := &Account{
		ID: 502, Name: "responses-lite", Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Concurrency: 1, Status: StatusActive, Schedulable: true, RateMultiplier: f64p(1),
		Credentials: map[string]any{"access_token": "oauth-token", "chatgpt_account_id": "chatgpt-account"},
	}

	result, forwardErr := svc.Forward(context.Background(), c, account, body)

	require.NoError(t, forwardErr)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	require.Empty(t, c.Request.Header.Get(responsesLiteHeader), "切换到完整 Responses 后不得伪造 Lite Header")
	require.Equal(t, "web_search", gjson.GetBytes(upstream.lastBody, "tools.19.type").String())
	require.Equal(t, "test", gjson.GetBytes(upstream.lastBody, "instructions").String())
	require.Equal(t, http.StatusOK, rec.Code)
}

func TestNormalizeOpenAIResponsesLiteToolsPayload_PreservesResponseCreateShape(t *testing.T) {
	body := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-terra",
		"client_metadata":{"ws_request_header_x_openai_internal_codex_responses_lite":"true"},
		"input":[{"type":"message","role":"user","content":"hello"}],
		"tools":[{"type":"namespace","name":"collaboration","tools":[{"type":"function","name":"spawn_agent"}]}],
		"tool_choice":{"type":"namespace","name":"collaboration"}
	}`)

	updated, changed, err := normalizeOpenAIResponsesLiteToolsPayload(body)

	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(t, "response.create", gjson.GetBytes(updated, "type").String())
	require.False(t, gjson.GetBytes(updated, "tools").Exists())
	require.Equal(t, "collaboration", gjson.GetBytes(updated, `input.#(type=="additional_tools").tools.0.name`).String())
	require.Equal(t, "namespace", gjson.GetBytes(updated, "tool_choice.type").String())
	require.True(t, gjson.GetBytes(updated, "parallel_tool_calls").Exists())
	require.False(t, gjson.GetBytes(updated, "parallel_tool_calls").Bool())
}

func TestNormalizeOpenAIResponsesLitePayloads_PreserveLargeSequence(t *testing.T) {
	body := []byte(`{
		"type":"response.create",
		"sequence":900719925474099312345,
		"tools":[{"type":"function","name":"lookup"}],
		"parallel_tool_calls":true
	}`)
	tests := []struct {
		name      string
		normalize func([]byte) ([]byte, bool, error)
	}{
		{name: "OAuth-like tools normalization", normalize: normalizeOpenAIResponsesLiteToolsPayload},
		{name: "API key parallel normalization", normalize: normalizeOpenAIResponsesLiteParallelToolCallsPayload},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			updated, changed, err := tt.normalize(body)

			require.NoError(t, err)
			require.True(t, changed)
			require.Equal(t, "900719925474099312345", gjson.GetBytes(updated, "sequence").Raw)
			require.True(t, gjson.GetBytes(updated, "parallel_tool_calls").Exists())
			require.False(t, gjson.GetBytes(updated, "parallel_tool_calls").Bool())
		})
	}
}

func TestApplyCodexOAuthTransform_PreservesLiteNamespaceToolChoice(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-terra",
		"input": []any{map[string]any{
			"type": "additional_tools",
			"tools": []any{map[string]any{
				"type": "namespace",
				"name": "collaboration",
			}},
		}},
		"tool_choice": map[string]any{"type": "namespace", "name": "collaboration"},
	}

	applyCodexOAuthTransform(reqBody, true, false)

	require.Equal(t, map[string]any{"type": "namespace", "name": "collaboration"}, reqBody["tool_choice"])
}

func TestOpenAIGatewayServiceForward_NormalizesResponsesLiteToolsForOAuth(t *testing.T) {
	gin.SetMode(gin.TestMode)

	for _, passthrough := range []bool{false, true} {
		name := "managed"
		if passthrough {
			name = "passthrough"
		}
		t.Run(name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
			c.Request.Header.Set("User-Agent", "codex_cli_rs/0.144.1")
			// 客户端自报不能决定 Lite 画像；故意声明 false，最终仍应由可信模型清单改写。
			c.Request.Header.Set(responsesLiteHeader, "false")
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
				Body: io.NopCloser(strings.NewReader(
					"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_lite\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n" +
						"data: [DONE]\n\n",
				)),
			}}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			svc.openaiModelCapabilities.replaceFromManifest(501, []byte(
				`{"models":[{"slug":"gpt-5.6-terra","visibility":"list","use_responses_lite":true,"supports_parallel_tool_calls":true}]}`,
			))
			account := &Account{
				ID: 501, Name: "responses-lite", Platform: PlatformOpenAI, Type: AccountTypeOAuth,
				Concurrency: 1, Status: StatusActive, Schedulable: true, RateMultiplier: f64p(1),
				Credentials: map[string]any{"access_token": "oauth-token", "chatgpt_account_id": "chatgpt-account"},
				Extra:       map[string]any{"openai_passthrough": passthrough},
			}
			body := []byte(`{
				"model":"gpt-5.6-terra","stream":true,"instructions":"test",
				"reasoning":{"effort":"high","context":"current_turn"},
				"parallel_tool_calls":true,
				"tools":[
					{"type":"function","name":"shell","parameters":{"type":"object"}},
					{"type":"custom","name":"exec"},
					{"type":"tool_search"},
					{"type":"namespace","name":"collaboration","tools":[{"type":"function","name":"spawn_agent","parameters":{"type":"object"}}]}
				],
				"input":[{"type":"message","role":"user","content":"hello"}],
				"tool_choice":"auto"
			}`)
			// 官方 UA 命中 strict 身份校验，须携带完整 Codex 身份
			body = codexOfficialIngressIdentityForTest(t, c, body)

			result, err := svc.Forward(context.Background(), c, account, body)

			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, "true", upstream.lastReq.Header.Get(responsesLiteHeader))
			require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "reasoning.effort").String())
			require.Equal(t, "all_turns", gjson.GetBytes(upstream.lastBody, "reasoning.context").String())
			// 官方 Lite 画像会把全部工具无损移动到首个 input.additional_tools，
			// 顶层 tools 必须删除，避免测试继续固化合并前的旧出站形态。
			require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
			require.Equal(t, "additional_tools", gjson.GetBytes(upstream.lastBody, "input.0.type").String())
			require.Equal(t, "collaboration", gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools").tools.0.name`).String())
			require.Equal(t, "shell", gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools").tools.#(type=="function").name`).String())
			require.Equal(t, "exec", gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools").tools.#(type=="custom").name`).String())
			require.True(t, gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools").tools.#(type=="tool_search")`).Exists())
			require.Equal(t, "auto", gjson.GetBytes(upstream.lastBody, "tool_choice").String())

			badRec := httptest.NewRecorder()
			badCtx, _ := gin.CreateTestContext(badRec)
			badCtx.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
			badCtx.Request.Header.Set(responsesLiteHeader, "true")
			badUpstream := &httpUpstreamRecorder{}
			svc.httpUpstream = badUpstream

			result, err = svc.Forward(context.Background(), badCtx, account, []byte(`{"model":"gpt-5.6-terra","tools":[{"type":"function","name":"shell"}],"parallel_tool_calls":"false"}`))

			require.ErrorContains(t, err, "parallel_tool_calls to be a boolean")
			require.Nil(t, result)
			require.Equal(t, http.StatusBadRequest, badRec.Code)
			require.Equal(t, "invalid_request_error", gjson.Get(badRec.Body.String(), "error.type").String())
			require.Equal(t, "parallel_tool_calls", gjson.Get(badRec.Body.String(), "error.param").String())
			require.Contains(t, gjson.Get(badRec.Body.String(), "error.message").String(), "parallel_tool_calls to be a boolean")
			require.Nil(t, badUpstream.lastReq)

			for _, malformed := range []struct {
				body      string
				wantParam string
			}{
				{body: `{"model":"gpt-5.6-terra","tools":{}}`, wantParam: "tools"},
				{body: `{"model":"gpt-5.6-terra","reasoning":[]}`, wantParam: "reasoning"},
			} {
				rec := httptest.NewRecorder()
				requestCtx, _ := gin.CreateTestContext(rec)
				requestCtx.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
				requestCtx.Request.Header.Set(responsesLiteHeader, "true")

				result, err = svc.Forward(context.Background(), requestCtx, account, []byte(malformed.body))

				require.Error(t, err)
				require.Nil(t, result)
				require.Equal(t, http.StatusBadRequest, rec.Code)
				require.Equal(t, malformed.wantParam, gjson.Get(rec.Body.String(), "error.param").String())
			}
		})
	}
}

func TestOpenAIGatewayServiceForward_PinsParallelToolCallsForToollessResponsesLite(t *testing.T) {
	gin.SetMode(gin.TestMode)

	accountCases := []struct {
		name        string
		accountType string
		credentials map[string]any
	}{
		{name: "oauth", accountType: AccountTypeOAuth, credentials: map[string]any{"access_token": "oauth-token", "chatgpt_account_id": "chatgpt-account"}},
		{name: "apikey", accountType: AccountTypeAPIKey, credentials: map[string]any{"api_key": "sk-test"}},
	}
	parallelCases := []struct {
		name  string
		field string
	}{
		{name: "字段缺失"},
		{name: "值为 true", field: `,"parallel_tool_calls":true`},
		{name: "值为 false", field: `,"parallel_tool_calls":false`},
	}

	for _, accountCase := range accountCases {
		for _, passthrough := range []bool{false, true} {
			mode := "managed"
			if passthrough {
				mode = "passthrough"
			}
			for _, parallelCase := range parallelCases {
				name := accountCase.name + "/" + mode + "/" + parallelCase.name
				t.Run(name, func(t *testing.T) {
					rec := httptest.NewRecorder()
					c, _ := gin.CreateTestContext(rec)
					c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
					c.Request.Header.Set("User-Agent", "codex_cli_rs/0.144.1")
					c.Request.Header.Set(responsesLiteHeader, "true")
					upstream := &httpUpstreamRecorder{resp: &http.Response{
						StatusCode: http.StatusOK,
						Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
						Body: io.NopCloser(strings.NewReader(
							"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_lite\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n" +
								"data: [DONE]\n\n",
						)),
					}}
					svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
					account := &Account{
						ID: 502, Name: "responses-lite-no-tools", Platform: PlatformOpenAI, Type: accountCase.accountType,
						Concurrency: 1, Status: StatusActive, Schedulable: true, RateMultiplier: f64p(1),
						Credentials: accountCase.credentials,
						Extra:       map[string]any{"openai_passthrough": passthrough},
					}
					body := []byte(`{
						"model":"gpt-5.6-terra","stream":true,"instructions":"test",
						"reasoning":{"effort":"high","context":"current_turn"},
						"input":[{"type":"message","role":"user","content":"hello"}]` + parallelCase.field + `
					}`)

					result, err := svc.Forward(context.Background(), c, account, body)

					require.NoError(t, err)
					require.NotNil(t, result)
					require.Equal(t, "true", upstream.lastReq.Header.Get(responsesLiteHeader))
					require.Equal(t, gjson.False, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Type, string(upstream.lastBody))
				})
			}
		}
	}
}

func TestOpenAIGatewayServiceForward_DisablesParallelToolCallsForResponsesLiteAPIKey(t *testing.T) {
	gin.SetMode(gin.TestMode)

	for _, passthrough := range []bool{false, true} {
		name := "managed"
		if passthrough {
			name = "passthrough"
		}
		t.Run(name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(nil))
			c.Request.Header.Set("User-Agent", "codex_cli_rs/0.144.1")
			c.Request.Header.Set(responsesLiteHeader, "true")
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
				Body: io.NopCloser(strings.NewReader(
					"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_lite\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n" +
						"data: [DONE]\n\n",
				)),
			}}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			account := &Account{
				ID: 503, Name: "responses-lite-api-key", Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
				Concurrency: 1, Status: StatusActive, Schedulable: true, RateMultiplier: f64p(1),
				Credentials: map[string]any{"api_key": "sk-test"},
				Extra:       map[string]any{"openai_passthrough": passthrough},
			}
			body := []byte(`{
				"model":"gpt-5.6-terra","stream":true,"instructions":"test",
				"tools":[{"type":"function","name":"lookup","parameters":{"type":"object"}}],
				"parallel_tool_calls":true,
				"input":[{"type":"message","role":"user","content":"hello"}]
			}`)

			result, err := svc.Forward(context.Background(), c, account, body)

			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, "true", upstream.lastReq.Header.Get(responsesLiteHeader))
			require.True(t, gjson.GetBytes(upstream.lastBody, "tools").IsArray())
			require.True(t, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Exists())
			require.False(t, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())
		})
	}
}
