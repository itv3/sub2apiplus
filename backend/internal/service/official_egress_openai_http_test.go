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
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const (
	testOfficialOpenAIInstallationID = "bee3cd38-4511-4497-899e-f19f04f953fd"
	testOfficialOpenAISessionID      = "019f9577-d69f-7892-809e-8a3a4198c671"
	testOfficialOpenAITurnID         = "019f9577-d70a-7553-ad23-8de3ede39d8b"
	testOfficialOpenAICallID         = "call_5FYHRGgugSt5anQYPCM8LO1B"
)

func TestOpenAIOfficialEgressHTTPFinalizerUsesIngressLifecycle(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)

	// 防御性模拟旧链路再次注入默认 instructions；Finalizer 必须只删除这一无来源字段。
	var transformed map[string]any
	require.NoError(t, json.Unmarshal(body, &transformed))
	transformed["instructions"] = "legacy synthesized instructions"
	transformedBody, err := marshalOpenAIUpstreamJSON(transformed)
	require.NoError(t, err)

	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	account := newOfficialOpenAIHTTPTestAccount(94)
	svc := &OpenAIGatewayService{}
	req, err := svc.buildUpstreamRequest(
		c.Request.Context(),
		c,
		account,
		transformedBody,
		"oauth-token",
		openAIUpstreamRequestPlan{
			IsStream:                   true,
			PromptCacheKey:             testOfficialOpenAISessionID,
			IsCodexCLI:                 true,
			OfficialEgressBodyContract: contract,
		},
	)

	require.NoError(t, err)
	wireBody, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(wireBody, "instructions").Exists())
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(wireBody, "input.3.call_id").String())
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(wireBody, "input.4.call_id").String())
	require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("session-id"))
	require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("thread-id"))
	require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("x-client-request-id"))
	require.Equal(t, testOfficialOpenAISessionID+":0", req.Header.Get("x-codex-window-id"))
	require.Equal(t, c.GetHeader("x-codex-turn-metadata"), req.Header.Get("x-codex-turn-metadata"))
	require.Empty(t, req.Header.Get("session_id"))
	require.Empty(t, req.Header.Get("conversation_id"))
	require.Empty(t, req.Header.Get("OpenAI-Beta"))

	egressContext, ok := OfficialEgressContextFromContext(req.Context())
	require.True(t, ok)
	require.Equal(t, int64(94), egressContext.AccountID())
	requireOfficialOpenAIField(t, egressContext, OfficialEgressFieldSessionID, testOfficialOpenAISessionID, OfficialEgressFieldLifecycleSession)
	requireOfficialOpenAIField(t, egressContext, OfficialEgressFieldThreadID, testOfficialOpenAISessionID, OfficialEgressFieldLifecycleSession)
	requireOfficialOpenAIField(t, egressContext, OfficialEgressFieldWindowID, testOfficialOpenAISessionID+":0", OfficialEgressFieldLifecycleSession)
	requireOfficialOpenAIField(t, egressContext, OfficialEgressFieldTurnMetadata, c.GetHeader("x-codex-turn-metadata"), OfficialEgressFieldLifecycleTurn)
	requireOfficialOpenAIField(t, egressContext, OfficialEgressFieldPromptCacheKey, testOfficialOpenAISessionID, OfficialEgressFieldLifecycleSession)
}

func TestOpenAIOfficialEgressHTTPFinalizerPreservesExplicitInstructions(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, true, false)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)

	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"oauth-token",
		openAIUpstreamRequestPlan{
			IsStream:                   true,
			PromptCacheKey:             testOfficialOpenAISessionID,
			IsCodexCLI:                 true,
			OfficialEgressBodyContract: contract,
		},
	)

	require.NoError(t, err)
	wireBody, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	require.Equal(t, "入口显式指令", gjson.GetBytes(wireBody, "instructions").String())
}

func TestOpenAIOfficialEgressHTTPFinalizerRejectsIdentityConflict(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	c.Request.Header.Set("thread-id", "019f9577-d69f-7892-809e-8a3a4198c672")

	_, err = (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"oauth-token",
		openAIUpstreamRequestPlan{
			IsStream:                   true,
			PromptCacheKey:             testOfficialOpenAISessionID,
			IsCodexCLI:                 true,
			OfficialEgressBodyContract: contract,
		},
	)

	require.ErrorContains(t, err, "ingress headers conflict with body identity")
}

func TestOpenAIGatewayForwardOfficialEgressHTTPNonStreamAndSSE(t *testing.T) {
	tests := []struct {
		name     string
		stream   bool
		response *http.Response
	}{
		{
			name:   "非流式",
			stream: false,
			response: newOfficialOpenAIHTTPJSONResponse(
				http.StatusOK,
				`{"id":"resp_non_stream","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":1,"output_tokens":2}}`,
			),
		},
		{
			name:     "SSE",
			stream:   true,
			response: newOfficialOpenAIHTTPSSECompletedResponse("resp_stream"),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := newOfficialOpenAIHTTPTestBody(t, tt.stream, false, true)
			c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			upstream := &httpUpstreamRecorder{resp: tt.response}
			svc := newOfficialOpenAIHTTPTestService(upstream)

			result, err := svc.Forward(context.Background(), c, newOfficialOpenAIHTTPTestAccount(94), body)

			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, tt.stream, result.Stream)
			require.NotNil(t, upstream.lastTLSProfile)
			require.Contains(t, upstream.lastTLSProfile.Name, "Codex CLI 0.145.0 HTTP")
			require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
			require.Equal(t, false, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())
			require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(upstream.lastBody, "include.0").String())
			require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.3.call_id").String())
			require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.4.call_id").String())
			require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("session-id"))
			require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("thread-id"))
		})
	}
}

func TestOpenAIGatewayForwardOfficialEgressHTTPCompactPreservesExplicitContract(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, true, true)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact")
	c.Request.Header.Set("Accept", "application/json")
	upstream := &httpUpstreamRecorder{resp: newOfficialOpenAIHTTPJSONResponse(
		http.StatusOK,
		`{"id":"resp_compact","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":2,"output_tokens":1}}`,
	)}

	result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
	)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.Equal(t, chatgptCodexURL+"/compact", upstream.lastReq.URL.String())
	require.Equal(t, "application/json", upstream.lastReq.Header.Get("Accept"))
	require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("session-id"))
	require.Equal(t, "入口显式指令", gjson.GetBytes(upstream.lastBody, "instructions").String())
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.3.call_id").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").Exists())
	require.False(t, gjson.GetBytes(upstream.lastBody, "client_metadata").Exists())
}

func TestOpenAIGatewayForwardOfficialEgressHTTPPassthroughUnaffectedByWSMode(t *testing.T) {
	for _, mode := range []string{
		OpenAIWSIngressModeOff,
		OpenAIWSIngressModeCtxPool,
		OpenAIWSIngressModePassthrough,
		OpenAIWSIngressModeHTTPBridge,
	} {
		t.Run(mode, func(t *testing.T) {
			body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
			c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			upstream := &httpUpstreamRecorder{resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_passthrough")}
			account := newOfficialOpenAIHTTPTestAccount(94)
			account.Extra["openai_passthrough"] = true
			account.Extra["openai_oauth_responses_websockets_v2_mode"] = mode

			result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
				context.Background(),
				c,
				account,
				body,
			)

			require.NoError(t, err)
			require.NotNil(t, result)
			require.NotNil(t, upstream.lastTLSProfile)
			require.Contains(t, upstream.lastTLSProfile.Name, "Codex CLI 0.145.0 HTTP")
			require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
			require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.3.call_id").String())
			require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("session-id"))
			require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("thread-id"))
			require.Empty(t, upstream.lastReq.Header.Get("session_id"))
			require.Empty(t, upstream.lastReq.Header.Get("conversation_id"))
		})
	}
}

func TestOpenAIGatewayForwardOfficialEgressHTTPNormalizesKiloChatCompletions(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":true,
		"messages":[
			{"role":"system","content":"Kilo third-party instruction"},
			{"role":"user","content":"KILO_OPENAI_HTTP_S1_OK"}
		],
		"tools":[{
			"type":"function",
			"function":{
				"name":"read_file",
				"description":"读取文件",
				"parameters":{"type":"object","properties":{"path":{"type":"string"}}}
			}
		}]
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "kilo-openai-http-session-a")
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_kilo_http"),
	}
	result, err := newOfficialOpenAIHTTPTestService(upstream).ForwardAsChatCompletions(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"",
		"gpt-5.6-luna",
	)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists(), string(upstream.lastBody))
	require.Equal(t, "gpt-5.6-luna", gjson.GetBytes(upstream.lastBody, "model").String())
	require.Contains(t, string(upstream.lastBody), "KILO_OPENAI_HTTP_S1_OK")
	require.Contains(t, string(upstream.lastBody), "Kilo third-party instruction")
	require.Equal(t, "all_turns", gjson.GetBytes(upstream.lastBody, "reasoning.context").String())
	require.Equal(t, false, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())
	require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
	require.Equal(t, "additional_tools", gjson.GetBytes(upstream.lastBody, "input.0.type").String())
	require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "input.0.tools.0.name").String())

	sessionID := gjson.GetBytes(upstream.lastBody, "client_metadata.session_id").String()
	require.NoError(t, uuid.Validate(sessionID))
	require.Equal(t, sessionID, gjson.GetBytes(upstream.lastBody, "client_metadata.thread_id").String())
	require.Equal(t, sessionID, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("session-id"))
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("thread-id"))
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("x-client-request-id"))
	require.Equal(t, sessionID+":0", upstream.lastReq.Header.Get("x-codex-window-id"))
	require.Equal(t, officialOpenAIHTTPUserAgent, upstream.lastReq.Header.Get("User-Agent"))
	require.Equal(t, officialOpenAIHTTPOriginator, upstream.lastReq.Header.Get("originator"))
	require.Equal(t, officialOpenAIHTTPBetaFeatures, upstream.lastReq.Header.Get("x-codex-beta-features"))
	require.Equal(t, officialOpenAIHTTPResponsesLite, upstream.lastReq.Header.Get(responsesLiteHeader))
	require.Empty(t, upstream.lastReq.Header.Get("version"))
	require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))

	egressContext, ok := OfficialEgressContextFromContext(upstream.lastReq.Context())
	require.True(t, ok)
	field, ok := egressContext.Field(OfficialEgressFieldSessionID)
	require.True(t, ok)
	require.Equal(t, OfficialEgressFieldSourceDerived, field.Source)
}

func TestOpenAIGatewayForwardOfficialEgressHTTPNormalizesKiloMessages(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":true,
		"max_tokens":128,
		"system":[{"type":"text","text":"Kilo third-party instruction"}],
		"messages":[{"role":"user","content":[{"type":"text","text":"KILO_OPENAI_MESSAGES_S1_OK"}]}]
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "kilo-openai-messages-session-a")
	c.Request.URL.Path = "/v1/messages"
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_kilo_messages"),
	}

	result, err := newOfficialOpenAIHTTPTestService(upstream).ForwardAsAnthropic(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"",
		"gpt-5.6-luna",
	)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
	require.Contains(t, string(upstream.lastBody), "KILO_OPENAI_MESSAGES_S1_OK")
	require.Equal(t, "all_turns", gjson.GetBytes(upstream.lastBody, "reasoning.context").String())

	sessionID := gjson.GetBytes(upstream.lastBody, "client_metadata.session_id").String()
	require.NoError(t, uuid.Validate(sessionID))
	require.Equal(t, sessionID, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("session-id"))
	require.Equal(t, sessionID, upstream.lastReq.Header.Get("thread-id"))
	require.Equal(t, officialOpenAIHTTPUserAgent, upstream.lastReq.Header.Get("User-Agent"))
	require.Equal(t, officialOpenAIHTTPOriginator, upstream.lastReq.Header.Get("originator"))
	require.Empty(t, upstream.lastReq.Header.Get("session_id"))
	require.Empty(t, upstream.lastReq.Header.Get("conversation_id"))
	require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))
	require.Empty(t, upstream.lastReq.Header.Get("version"))
}

func TestOpenAIGatewayForwardOfficialEgressHTTPNormalizesKiloResponsesLiteContract(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":false,
		"store":true,
		"parallel_tool_calls":true,
		"tool_choice":"required",
		"max_output_tokens":32000,
		"instructions":"Kilo raw responses instruction",
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"KILO_OPENAI_RESPONSES_S1_OK"}]}],
		"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],
		"reasoning":{"effort":"medium","context":"none"},
		"include":["message.output_text.logprobs"],
		"text":{"verbosity":"high"}
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "kilo-openai-responses-session-a")
	c.Request.URL.Path = "/v1/responses"
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_kilo_responses"),
	}

	result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
	)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
	require.Contains(t, string(upstream.lastBody), "Kilo raw responses instruction")
	require.Equal(t, "additional_tools", gjson.GetBytes(upstream.lastBody, "input.0.type").String())
	require.Equal(t, "developer", gjson.GetBytes(upstream.lastBody, "input.1.role").String())
	require.Equal(t, "all_turns", gjson.GetBytes(upstream.lastBody, "reasoning.context").String())
	require.Equal(t, "medium", gjson.GetBytes(upstream.lastBody, "reasoning.effort").String())
	require.Equal(t, false, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())
	require.Equal(t, false, gjson.GetBytes(upstream.lastBody, "store").Bool())
	require.Equal(t, true, gjson.GetBytes(upstream.lastBody, "stream").Bool())
	require.Equal(t, "auto", gjson.GetBytes(upstream.lastBody, "tool_choice").String())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(upstream.lastBody, "include.0").String())
	require.Len(t, gjson.GetBytes(upstream.lastBody, "include").Array(), 1)
	require.Equal(t, "low", gjson.GetBytes(upstream.lastBody, "text.verbosity").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
	require.False(t, gjson.GetBytes(upstream.lastBody, "max_output_tokens").Exists())
	require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "input.0.tools.0.name").String())
}

func TestOpenAIGatewayForwardOfficialEgressHTTPRetryRebuildsIdentity(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	input, ok := payload["input"].([]any)
	require.True(t, ok)
	require.Greater(t, len(input), 3)
	retryItem, ok := input[3].(map[string]any)
	require.True(t, ok)
	retryItem["namespace"] = "remove-on-retry"
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)

	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	upstream := &httpUpstreamRecorder{responses: []*http.Response{
		newOfficialOpenAIHTTPJSONResponse(
			http.StatusBadRequest,
			`{"error":{"code":"unknown_parameter","message":"Unknown parameter: input[3].namespace","param":"input[3].namespace"}}`,
		),
		newOfficialOpenAIHTTPJSONResponse(
			http.StatusOK,
			`{"id":"resp_retry","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`,
		),
	}}

	result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
	)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.Len(t, upstream.requests, 2)
	require.Equal(t, "remove-on-retry", gjson.GetBytes(upstream.bodies[0], "input.3.namespace").String())
	require.False(t, gjson.GetBytes(upstream.bodies[1], "input.3.namespace").Exists())
	for _, req := range upstream.requests {
		require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("session-id"))
		require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(mustReadRequestBody(t, req), "input.3.call_id").String())
	}
	firstContext, firstOK := OfficialEgressContextFromContext(upstream.requests[0].Context())
	secondContext, secondOK := OfficialEgressContextFromContext(upstream.requests[1].Context())
	require.True(t, firstOK)
	require.True(t, secondOK)
	require.NotSame(t, firstContext, secondContext)
}

func TestOpenAIOfficialEgressHTTPAccountSwitchRebuildsContext(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	plan := openAIUpstreamRequestPlan{
		IsStream:                   true,
		PromptCacheKey:             testOfficialOpenAISessionID,
		IsCodexCLI:                 true,
		OfficialEgressBodyContract: contract,
	}

	firstReq, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "token-94", plan,
	)
	require.NoError(t, err)
	secondReq, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(95), body, "token-95", plan,
	)
	require.NoError(t, err)

	firstContext, firstOK := OfficialEgressContextFromContext(firstReq.Context())
	secondContext, secondOK := OfficialEgressContextFromContext(secondReq.Context())
	require.True(t, firstOK)
	require.True(t, secondOK)
	require.Equal(t, int64(94), firstContext.AccountID())
	require.Equal(t, int64(95), secondContext.AccountID())
	require.NotEqual(t, firstContext.ConnectionPoolID(), secondContext.ConnectionPoolID())
	requireOfficialOpenAIField(t, firstContext, OfficialEgressFieldSessionID, testOfficialOpenAISessionID, OfficialEgressFieldLifecycleSession)
	requireOfficialOpenAIField(t, secondContext, OfficialEgressFieldSessionID, testOfficialOpenAISessionID, OfficialEgressFieldLifecycleSession)
}

func newOfficialOpenAIHTTPTestBody(
	t *testing.T,
	stream bool,
	explicitInstructions bool,
	withToolContinuation bool,
) []byte {
	t.Helper()
	turnMetadataBytes, err := json.Marshal(map[string]any{
		"installation_id":         testOfficialOpenAIInstallationID,
		"session_id":              testOfficialOpenAISessionID,
		"thread_id":               testOfficialOpenAISessionID,
		"turn_id":                 testOfficialOpenAITurnID,
		"window_id":               testOfficialOpenAISessionID + ":0",
		"request_kind":            "turn",
		"thread_source":           "user",
		"sandbox":                 "seccomp",
		"turn_started_at_unix_ms": int64(1784919086939),
	})
	require.NoError(t, err)
	turnMetadata := string(turnMetadataBytes)

	input := []any{
		map[string]any{
			"type": "additional_tools",
			"role": "developer",
			"tools": []any{
				map[string]any{"type": "custom", "name": "exec"},
				map[string]any{"type": "custom", "name": "wait"},
				map[string]any{"type": "custom", "name": "request_user_input"},
			},
		},
		map[string]any{"type": "message", "role": "developer", "content": "保持入口语义"},
		map[string]any{"type": "message", "role": "user", "content": "执行测试"},
	}
	if withToolContinuation {
		input = append(input,
			map[string]any{
				"type":               "custom_tool_call",
				"name":               "exec",
				"call_id":            testOfficialOpenAICallID,
				"input":              "{}",
				"status":             "completed",
				"provider_data_hash": "hash",
			},
			map[string]any{
				"type":    "custom_tool_call_output",
				"call_id": testOfficialOpenAICallID,
				"output":  "ok",
			},
		)
	}

	payload := map[string]any{
		"model":               "gpt-5.6-luna",
		"stream":              stream,
		"store":               false,
		"prompt_cache_key":    testOfficialOpenAISessionID,
		"parallel_tool_calls": false,
		"include":             []any{"reasoning.encrypted_content"},
		"reasoning":           map[string]any{"effort": "high", "summary": "auto"},
		"text":                map[string]any{"verbosity": "low"},
		"tool_choice":         "auto",
		"client_metadata": map[string]any{
			"x-codex-installation-id": testOfficialOpenAIInstallationID,
			"session_id":              testOfficialOpenAISessionID,
			"thread_id":               testOfficialOpenAISessionID,
			"turn_id":                 testOfficialOpenAITurnID,
			"x-codex-window-id":       testOfficialOpenAISessionID + ":0",
			"x-codex-turn-metadata":   turnMetadata,
		},
		"input": input,
	}
	if explicitInstructions {
		payload["instructions"] = "入口显式指令"
	}
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return body
}

func newOfficialOpenAIHTTPTestContext(body []byte, path string) *gin.Context {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	c.Request.Header.Set("Accept", "text/event-stream")
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.145.0)")
	c.Request.Header.Set("originator", "codex_exec")
	c.Request.Header.Set("session-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("thread-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("x-client-request-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("x-codex-beta-features", "remote_compaction_v2")
	c.Request.Header.Set("x-codex-window-id", testOfficialOpenAISessionID+":0")
	c.Request.Header.Set("x-openai-internal-codex-responses-lite", "true")
	c.Request.Header.Set("OpenAI-Beta", "responses=experimental")
	c.Request.Header.Set("session_id", "legacy-session")
	c.Request.Header.Set("conversation_id", "legacy-conversation")

	turnMetadata := gjson.GetBytes(body, "client_metadata.x-codex-turn-metadata").String()
	c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
	return c
}

func newOfficialOpenAIHTTPKiloContext(body []byte, sessionAffinity string) *gin.Context {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set(
		"User-Agent",
		"Kilo-Code/7.4.1101 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
	)
	c.Request.Header.Set("X-Session-Affinity", sessionAffinity)
	c.Set("api_key", &APIKey{ID: 1})
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	return c
}

func newOfficialOpenAIHTTPTestAccount(accountID int64) *Account {
	return &Account{
		ID:          accountID,
		Name:        "OpenAI Official Egress",
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-test-token",
			"chatgpt_account_id": "chatgpt-test-account",
		},
		Extra:       map[string]any{},
		Status:      StatusActive,
		Schedulable: true,
	}
}

func newOfficialOpenAIHTTPTestService(upstream *httpUpstreamRecorder) *OpenAIGatewayService {
	return &OpenAIGatewayService{
		cfg: &config.Config{Security: config.SecurityConfig{
			URLAllowlist: config.URLAllowlistConfig{Enabled: false},
		}},
		httpUpstream: upstream,
	}
}

func newOfficialOpenAIHTTPJSONResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func newOfficialOpenAIHTTPSSECompletedResponse(responseID string) *http.Response {
	body := strings.Join([]string{
		`data: {"type":"response.completed","response":{"id":"` + responseID + `","object":"response","model":"gpt-5.6-luna","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func mustReadRequestBody(t *testing.T, req *http.Request) []byte {
	t.Helper()
	require.NotNil(t, req)
	require.NotNil(t, req.GetBody)
	body, err := req.GetBody()
	require.NoError(t, err)
	defer func() { _ = body.Close() }()
	data, err := io.ReadAll(body)
	require.NoError(t, err)
	return data
}

func requireOfficialOpenAIField(
	t *testing.T,
	egressContext *OfficialEgressContext,
	name OfficialEgressFieldName,
	wantValue string,
	wantLifecycle OfficialEgressFieldLifecycle,
) {
	t.Helper()
	field, ok := egressContext.Field(name)
	require.True(t, ok)
	require.Equal(t, wantValue, field.Value())
	require.Equal(t, OfficialEgressFieldSourceIngressExplicit, field.Source)
	require.Equal(t, wantLifecycle, field.Lifecycle)
}
