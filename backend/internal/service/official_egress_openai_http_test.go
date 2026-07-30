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
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const (
	testOfficialOpenAIInstallationID = "bee3cd38-4511-4497-899e-f19f04f953fd"
	testOfficialOpenAISessionID      = "019f9577-d69f-7892-809e-8a3a4198c671"
	testOfficialOpenAIChildThreadID  = "019f9577-d69f-7892-809e-8a3a4198c672"
	testOfficialOpenAIOtherThreadID  = "019f9577-d69f-7892-809e-8a3a4198c673"
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
	wireBody := mustReadRequestBody(t, req)
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
	wireBody := mustReadRequestBody(t, req)
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

func TestOpenAIOfficialEgressHTTPFinalizerAcceptsGuardianIdentity(t *testing.T) {
	body := newOfficialOpenAIGuardianHTTPBody(t)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")

	req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"oauth-token",
		openAIUpstreamRequestPlan{
			IsStream:                   true,
			PromptCacheKey:             "guardian:" + testOfficialOpenAISessionID,
			IsCodexCLI:                 true,
			OfficialEgressBodyContract: contract,
		},
	)

	require.NoError(t, err)
	wireBody := mustReadRequestBody(t, req)
	require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("session-id"))
	require.Equal(t, testOfficialOpenAIChildThreadID, req.Header.Get("thread-id"))
	require.Equal(t, testOfficialOpenAIChildThreadID, req.Header.Get("x-client-request-id"))
	require.Equal(t, testOfficialOpenAIChildThreadID+":0", req.Header.Get("x-codex-window-id"))
	require.Equal(t, "guardian", req.Header.Get("x-openai-subagent"))
	require.Equal(t, testOfficialOpenAISessionID, req.Header.Get("x-codex-parent-thread-id"))
	require.Equal(t, "guardian:"+testOfficialOpenAISessionID, gjson.GetBytes(wireBody, "prompt_cache_key").String())
	require.Equal(t, testOfficialOpenAISessionID, gjson.GetBytes(wireBody, "client_metadata.session_id").String())
	require.Equal(t, testOfficialOpenAIChildThreadID, gjson.GetBytes(wireBody, "client_metadata.thread_id").String())
	require.Equal(t, "guardian", gjson.GetBytes(wireBody, "client_metadata.x-openai-subagent").String())
	require.Equal(t, testOfficialOpenAISessionID, gjson.GetBytes(wireBody, "client_metadata.x-codex-parent-thread-id").String())

	turnMetadata := gjson.Parse(req.Header.Get("x-codex-turn-metadata"))
	require.Equal(t, testOfficialOpenAISessionID, turnMetadata.Get("session_id").String())
	require.Equal(t, testOfficialOpenAIChildThreadID, turnMetadata.Get("thread_id").String())
	require.Equal(t, testOfficialOpenAIChildThreadID+":0", turnMetadata.Get("window_id").String())
}

func TestResolveExplicitOfficialOpenAIHTTPIdentityAcceptsMemoryConsolidation(t *testing.T) {
	body := newOfficialOpenAIMemoryConsolidationHTTPBody(t)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIMemoryConsolidationHTTPContext(t, body, "/v1/responses")

	identity, err := resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, false)
	require.NoError(t, err)
	require.Equal(t, testOfficialOpenAISessionID, identity.sessionID)
	require.Equal(t, testOfficialOpenAIChildThreadID, identity.threadID)
	require.Equal(t, testOfficialOpenAIChildThreadID, identity.clientRequest)
	require.Equal(t, testOfficialOpenAISessionID, identity.promptCacheKey)
}

func TestResolveExplicitOfficialOpenAIHTTPIdentityAcceptsOrdinaryChild(t *testing.T) {
	body := newOfficialOpenAIChildHTTPBody(
		t,
		testOfficialOpenAISessionID,
		"collab_spawn",
		testOfficialOpenAISessionID,
		"subagent",
		"thread_spawn",
	)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIChildHTTPContext(
		t,
		body,
		"/v1/responses",
		"collab_spawn",
		false,
		testOfficialOpenAISessionID,
	)

	identity, err := resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, false)
	require.NoError(t, err)
	require.Equal(t, testOfficialOpenAISessionID, identity.sessionID)
	require.Equal(t, testOfficialOpenAIChildThreadID, identity.threadID)
	require.Equal(t, testOfficialOpenAIChildThreadID, identity.clientRequest)
	require.Equal(t, testOfficialOpenAISessionID, identity.promptCacheKey)
}

func TestResolveExplicitOfficialOpenAIHTTPIdentityRequiresThreadAnchors(t *testing.T) {
	tests := []struct {
		name      string
		mutate    func(t *testing.T, body []byte, c *gin.Context) []byte
		errorText string
	}{
		{
			name: "client request 必须等于 thread",
			mutate: func(_ *testing.T, body []byte, c *gin.Context) []byte {
				c.Request.Header.Set("x-client-request-id", testOfficialOpenAIOtherThreadID)
				return body
			},
			errorText: "thread/request identity conflicts",
		},
		{
			name: "guardian prompt cache key 必须绑定父线程",
			mutate: func(t *testing.T, body []byte, _ *gin.Context) []byte {
				return mutateOfficialOpenAIHTTPTestBody(t, body, func(payload map[string]any, _ map[string]any, _ map[string]any) {
					payload["prompt_cache_key"] = testOfficialOpenAIOtherThreadID
				})
			},
			errorText: "guardian prompt_cache_key conflicts with identity",
		},
		{
			name: "window 前缀必须等于 thread",
			mutate: func(t *testing.T, body []byte, c *gin.Context) []byte {
				body = mutateOfficialOpenAIHTTPTestBody(t, body, func(_ map[string]any, metadata map[string]any, turnMetadata map[string]any) {
					metadata["x-codex-window-id"] = testOfficialOpenAISessionID + ":0"
					turnMetadata["window_id"] = testOfficialOpenAISessionID + ":0"
				})
				c.Request.Header.Set("x-codex-window-id", testOfficialOpenAISessionID+":0")
				c.Request.Header.Set("x-codex-turn-metadata", gjson.GetBytes(body, "client_metadata.x-codex-turn-metadata").String())
				return body
			},
			errorText: "window_id conflicts with thread",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := newOfficialOpenAIGuardianHTTPBody(t)
			c := newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")
			body = tt.mutate(t, body, c)
			contract, err := captureOfficialOpenAIHTTPBodyContract(body)
			require.NoError(t, err)

			_, err = resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, false)
			require.ErrorContains(t, err, tt.errorText)
		})
	}
}

func TestResolveExplicitOfficialOpenAIHTTPCompactIdentityUsesSessionPromptCacheKey(t *testing.T) {
	fullBody := newOfficialOpenAIHTTPTestBody(t, false, false, false)
	turnMetadata := gjson.GetBytes(fullBody, "client_metadata.x-codex-turn-metadata").String()
	body := mutateOfficialOpenAIHTTPTestBody(t, fullBody, func(payload map[string]any, _ map[string]any, _ map[string]any) {
		delete(payload, "client_metadata")
	})
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact")
	c.Request.Header.Del("x-client-request-id")
	c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
	c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)

	identity, err := resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, true)
	require.NoError(t, err)
	require.Equal(t, testOfficialOpenAISessionID, identity.sessionID)
	require.Equal(t, testOfficialOpenAISessionID, identity.threadID)
	require.Equal(t, testOfficialOpenAISessionID, identity.promptCacheKey)
	require.Empty(t, identity.clientRequest)

	conflictingBody := mutateOfficialOpenAIHTTPTestBody(t, body, func(payload map[string]any, _ map[string]any, _ map[string]any) {
		payload["prompt_cache_key"] = testOfficialOpenAIOtherThreadID
	})
	conflictingContract, err := captureOfficialOpenAIHTTPBodyContract(conflictingBody)
	require.NoError(t, err)
	_, err = resolveExplicitOfficialOpenAIHTTPIdentity(c, conflictingContract, true)
	require.ErrorContains(t, err, "root prompt_cache_key conflicts with identity")
}

func TestResolveExplicitOfficialOpenAIHTTPCompactIdentityAcceptsConditionalKinds(t *testing.T) {
	tests := []struct {
		name           string
		newFullBody    func(t *testing.T) []byte
		subagent       string
		memoryGenerate bool
		parentThreadID string
		expectedPrompt string
	}{
		{
			name: "普通子代理沿用 session key",
			newFullBody: func(t *testing.T) []byte {
				return newOfficialOpenAIChildHTTPBody(
					t,
					testOfficialOpenAISessionID,
					"collab_spawn",
					testOfficialOpenAISessionID,
					"subagent",
					"thread_spawn",
				)
			},
			subagent:       "collab_spawn",
			parentThreadID: testOfficialOpenAISessionID,
			expectedPrompt: testOfficialOpenAISessionID,
		},
		{
			name:           "内部记忆合并沿用 session key",
			newFullBody:    newOfficialOpenAIMemoryConsolidationHTTPBody,
			subagent:       "memory_consolidation",
			memoryGenerate: true,
			expectedPrompt: testOfficialOpenAISessionID,
		},
		{
			name:           "guardian 绑定 parent thread",
			newFullBody:    newOfficialOpenAIGuardianHTTPBody,
			subagent:       "guardian",
			parentThreadID: testOfficialOpenAISessionID,
			expectedPrompt: "guardian:" + testOfficialOpenAISessionID,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fullBody := tt.newFullBody(t)
			turnMetadata := gjson.GetBytes(fullBody, "client_metadata.x-codex-turn-metadata").String()
			body := mutateOfficialOpenAIHTTPTestBody(t, fullBody, func(payload map[string]any, _ map[string]any, _ map[string]any) {
				delete(payload, "client_metadata")
			})
			c := newOfficialOpenAIChildHTTPContext(
				t,
				body,
				"/v1/responses/compact",
				tt.subagent,
				tt.memoryGenerate,
				tt.parentThreadID,
			)
			c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
			c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
			c.Request.Header.Del("x-client-request-id")
			contract, err := captureOfficialOpenAIHTTPBodyContract(body)
			require.NoError(t, err)

			identity, err := resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, true)
			require.NoError(t, err)
			require.Equal(t, testOfficialOpenAISessionID, identity.sessionID)
			require.Equal(t, testOfficialOpenAIChildThreadID, identity.threadID)
			require.Equal(t, tt.expectedPrompt, identity.promptCacheKey)
			require.Empty(t, identity.clientRequest)
		})
	}
}

func TestResolveExplicitOfficialOpenAIHTTPIdentityRejectsConditionalMetadataHeaderConflict(t *testing.T) {
	tests := []struct {
		name      string
		field     string
		bodyValue any
	}{
		{name: "subagent", field: "x-openai-subagent", bodyValue: "guardian-mismatch"},
		{name: "parent thread", field: "x-codex-parent-thread-id", bodyValue: testOfficialOpenAIOtherThreadID},
		{name: "非字符串", field: "x-openai-subagent", bodyValue: 7},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := newOfficialOpenAIGuardianHTTPBody(t)
			body = mutateOfficialOpenAIHTTPTestBody(t, body, func(_ map[string]any, metadata map[string]any, _ map[string]any) {
				metadata[tt.field] = tt.bodyValue
			})
			contract, err := captureOfficialOpenAIHTTPBodyContract(body)
			require.NoError(t, err)
			c := newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")

			_, err = resolveExplicitOfficialOpenAIHTTPIdentity(c, contract, false)
			require.ErrorContains(t, err, "client_metadata "+tt.field+" conflicts with ingress header")
		})
	}
}

func TestResolveDerivedOfficialOpenAICompactionMetadataCoversAllReasons(t *testing.T) {
	tests := []struct {
		name     string
		reason   string
		trigger  string
		phase    string
		expected string
	}{
		{
			name:     "用户主动触发",
			reason:   "user_requested",
			trigger:  "manual",
			phase:    "standalone_turn",
			expected: `{"trigger":"manual","reason":"user_requested","implementation":"responses_compaction_v2","phase":"standalone_turn","strategy":"memento"}`,
		},
		{
			name:     "上下文阈值触发",
			reason:   "context_limit",
			trigger:  "auto",
			phase:    "mid_turn",
			expected: `{"trigger":"auto","reason":"context_limit","implementation":"responses_compaction_v2","phase":"mid_turn","strategy":"memento"}`,
		},
		{
			name:     "模型降级触发",
			reason:   "model_downshift",
			trigger:  "auto",
			phase:    "pre_turn",
			expected: `{"trigger":"auto","reason":"model_downshift","implementation":"responses_compaction_v2","phase":"pre_turn","strategy":"memento"}`,
		},
		{
			name:     "压缩哈希变化触发",
			reason:   "comp_hash_changed",
			trigger:  "auto",
			phase:    "pre_turn",
			expected: `{"trigger":"auto","reason":"comp_hash_changed","implementation":"responses_compaction_v2","phase":"pre_turn","strategy":"memento"}`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"compaction_trigger"}]}`)
			c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			c.Request.Header.Set("x-codex-turn-metadata", `{"compaction":{"reason":"`+tt.reason+`"}}`)

			metadata, ok := resolveDerivedOfficialOpenAICompactionMetadata(c, body)
			require.True(t, ok)
			require.Equal(t, tt.trigger, metadata.Trigger)
			require.Equal(t, tt.phase, metadata.Phase)
			encoded, err := json.Marshal(metadata)
			require.NoError(t, err)
			require.JSONEq(t, tt.expected, string(encoded))
			// JSONEq 不校验字段顺序；再比较原始字节确保 wire 顺序不被 map 改写。
			require.Equal(t, tt.expected, string(encoded))
		})
	}
}

func TestResolveDerivedOfficialOpenAICompactionMetadataLegacyImplementation(t *testing.T) {
	body := []byte(`{"model":"gpt-5.6-luna","input":[]}`)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact")

	metadata, ok := resolveDerivedOfficialOpenAICompactionMetadata(c, body)
	require.True(t, ok)
	encoded, err := json.Marshal(metadata)
	require.NoError(t, err)
	require.Equal(
		t,
		`{"trigger":"manual","reason":"user_requested","implementation":"responses_compact","phase":"standalone_turn","strategy":"memento"}`,
		string(encoded),
	)
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
	bodyWithMetadata := newOfficialOpenAIHTTPTestBody(t, false, true, true)
	turnMetadata := gjson.GetBytes(bodyWithMetadata, "client_metadata.x-codex-turn-metadata").String()
	var compactPayload map[string]any
	require.NoError(t, json.Unmarshal(bodyWithMetadata, &compactPayload))
	delete(compactPayload, "client_metadata")
	body, err := marshalOpenAIUpstreamJSON(compactPayload)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(body, "client_metadata").Exists(), "官方 compact fixture 不得合成 client_metadata")
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact")
	c.Request.Header.Set("Accept", "application/json")
	c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
	c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
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
	// 官方 compact 走 execute，端点层不设 accept，由 reqwest 补默认 */*（SPEC-HDR-006）。
	require.Equal(t, "*/*", upstream.lastReq.Header.Get("Accept"))
	require.Equal(t, testOfficialOpenAISessionID, upstream.lastReq.Header.Get("session-id"))
	require.Equal(t, "入口显式指令", gjson.GetBytes(upstream.lastBody, "instructions").String())
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(upstream.lastBody, "input.3.call_id").String())
	require.Equal(t, testOfficialOpenAISessionID, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "client_metadata").Exists())
	require.Equal(t, testOfficialOpenAIInstallationID, upstream.lastReq.Header.Get("X-Codex-Installation-ID"))
}

func TestOpenAIGatewayForwardOfficialEgressHTTPCompactRestoresOnlyCapturedSeed(t *testing.T) {
	originalBody := newOfficialOpenAIHTTPTestBody(t, false, true, true)
	normalizedBody, changed, err := normalizeOpenAICompactRequestBody(originalBody)
	require.NoError(t, err)
	require.True(t, changed)
	require.False(t, gjson.GetBytes(normalizedBody, "prompt_cache_key").Exists())
	require.False(t, gjson.GetBytes(normalizedBody, "client_metadata").Exists())

	for _, tt := range []struct {
		name        string
		captureSeed bool
		wantError   string
	}{
		{name: "原始 seed 已捕获", captureSeed: true},
		{name: "没有显式 seed", wantError: "requires complete identity from official ingress"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			c := newOfficialOpenAIHTTPTestContext(originalBody, "/v1/responses/compact")
			c.Request.Header.Set("Accept", "application/json")
			c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
			if tt.captureSeed {
				c.Set(openAICompactSessionSeedKey, testOfficialOpenAISessionID)
			}
			upstream := &httpUpstreamRecorder{resp: newOfficialOpenAIHTTPJSONResponse(
				http.StatusOK,
				`{"id":"resp_compact_seed","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":2,"output_tokens":1}}`,
			)}

			result, forwardErr := newOfficialOpenAIHTTPTestService(upstream).Forward(
				context.Background(),
				c,
				newOfficialOpenAIHTTPTestAccount(94),
				normalizedBody,
			)
			if tt.wantError != "" {
				require.ErrorContains(t, forwardErr, tt.wantError)
				require.Nil(t, result)
				require.Nil(t, upstream.lastReq)
				return
			}

			require.NoError(t, forwardErr)
			require.NotNil(t, result)
			require.NotNil(t, upstream.lastReq)
			require.Equal(t, testOfficialOpenAISessionID, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
			require.False(t, gjson.GetBytes(upstream.lastBody, "client_metadata").Exists())
		})
	}
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
	require.Equal(t, "0.145.0", upstream.lastReq.Header.Get("version"))
	require.Equal(t, "zstd", upstream.lastReq.Header.Get("Content-Encoding"))
	rawCompressedBody, err := io.ReadAll(upstream.lastReq.Body)
	require.NoError(t, err)
	require.True(t, bytes.HasPrefix(rawCompressedBody, []byte{0x28, 0xb5, 0x2f, 0xfd}), "请求体必须是真实 zstd 帧，不能只伪造 Header")
	require.Equal(t, int64(len(rawCompressedBody)), upstream.lastReq.ContentLength)
	require.NotNil(t, upstream.lastReq.GetBody, "压缩后的请求必须支持重试重放")
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
	require.Equal(t, codexCLIVersion, upstream.lastReq.Header.Get("version"))
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

func TestFinalizeOfficialOpenAIHTTPBodyPreservesNestedRawData(t *testing.T) {
	body := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hello","opaque":{"z":9007199254740993,"a":1}}]}],"tool_choice":"required","reasoning":{"effort":"medium"}}`)
	contract, err := captureGeneratedOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	identity := deriveOfficialOpenAIHTTPIdentity(
		nil,
		newOfficialOpenAIHTTPTestAccount(94991),
		body,
		contract,
	)

	finalized, modified, err := finalizeOfficialOpenAIHTTPBody(
		body,
		contract,
		identity,
		officialOpenAIReasoningDefaults{},
		false,
		false,
		true,
		true,
	)
	require.NoError(t, err)
	require.True(t, modified)
	require.Contains(t, string(finalized), `"opaque":{"z":9007199254740993,"a":1}`)
	require.NotContains(t, string(finalized), "9007199254740992")
	require.Equal(t, "auto", gjson.GetBytes(finalized, "tool_choice").String())
}

func TestOpenAIGatewayForwardOfficialEgressHTTPProactiveNamespaceStripPreservesIdentity(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	input, ok := payload["input"].([]any)
	require.True(t, ok)
	require.Greater(t, len(input), 3)
	item, ok := input[3].(map[string]any)
	require.True(t, ok)
	item["namespace"] = "remove-before-forward"
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)

	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	upstream := &httpUpstreamRecorder{responses: []*http.Response{
		newOfficialOpenAIHTTPJSONResponse(
			http.StatusOK,
			`{"id":"resp_namespace","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`,
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
	require.Len(t, upstream.requests, 1)
	require.False(t, gjson.GetBytes(upstream.bodies[0], "input.3.namespace").Exists())
	require.Equal(t, testOfficialOpenAISessionID, upstream.requests[0].Header.Get("session-id"))
	require.Equal(t, testOfficialOpenAICallID, gjson.GetBytes(mustReadRequestBody(t, upstream.requests[0]), "input.3.call_id").String())
	_, contextOK := OfficialEgressContextFromContext(upstream.requests[0].Context())
	require.True(t, contextOK)
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

func newOfficialOpenAIGuardianHTTPBody(t *testing.T) []byte {
	t.Helper()
	return newOfficialOpenAIChildHTTPBody(
		t,
		"guardian:"+testOfficialOpenAISessionID,
		"guardian",
		testOfficialOpenAISessionID,
		"subagent",
		"guardian",
	)
}

func newOfficialOpenAIMemoryConsolidationHTTPBody(t *testing.T) []byte {
	t.Helper()
	return newOfficialOpenAIChildHTTPBody(
		t,
		testOfficialOpenAISessionID,
		"memory_consolidation",
		"",
		"memory_consolidation",
		"",
	)
}

func newOfficialOpenAIChildHTTPBody(
	t *testing.T,
	promptCacheKey string,
	subagent string,
	parentThreadID string,
	threadSource string,
	turnSubagentKind string,
) []byte {
	t.Helper()
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	return mutateOfficialOpenAIHTTPTestBody(t, body, func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any) {
		payload["prompt_cache_key"] = promptCacheKey
		metadata["thread_id"] = testOfficialOpenAIChildThreadID
		metadata["x-codex-window-id"] = testOfficialOpenAIChildThreadID + ":0"
		metadata["x-openai-subagent"] = subagent
		if parentThreadID == "" {
			delete(metadata, "x-codex-parent-thread-id")
		} else {
			metadata["x-codex-parent-thread-id"] = parentThreadID
		}
		turnMetadata["thread_id"] = testOfficialOpenAIChildThreadID
		turnMetadata["window_id"] = testOfficialOpenAIChildThreadID + ":0"
		turnMetadata["thread_source"] = threadSource
		if turnSubagentKind == "" {
			delete(turnMetadata, "subagent_kind")
		} else {
			turnMetadata["subagent_kind"] = turnSubagentKind
		}
		if parentThreadID == "" {
			delete(turnMetadata, "parent_thread_id")
		} else {
			turnMetadata["parent_thread_id"] = parentThreadID
		}
	})
}

func mutateOfficialOpenAIHTTPTestBody(
	t *testing.T,
	body []byte,
	mutate func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any),
) []byte {
	t.Helper()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	metadata, _ := payload["client_metadata"].(map[string]any)
	turnMetadata := map[string]any{}
	if metadata != nil {
		rawTurnMetadata, _ := metadata["x-codex-turn-metadata"].(string)
		if strings.TrimSpace(rawTurnMetadata) != "" {
			require.NoError(t, json.Unmarshal([]byte(rawTurnMetadata), &turnMetadata))
		}
	}
	mutate(payload, metadata, turnMetadata)
	if metadata != nil {
		encodedTurnMetadata, err := json.Marshal(turnMetadata)
		require.NoError(t, err)
		metadata["x-codex-turn-metadata"] = string(encodedTurnMetadata)
	}
	mutated, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return mutated
}

func newOfficialOpenAIGuardianHTTPContext(t *testing.T, body []byte, path string) *gin.Context {
	t.Helper()
	return newOfficialOpenAIChildHTTPContext(
		t,
		body,
		path,
		"guardian",
		false,
		testOfficialOpenAISessionID,
	)
}

func newOfficialOpenAIMemoryConsolidationHTTPContext(t *testing.T, body []byte, path string) *gin.Context {
	t.Helper()
	return newOfficialOpenAIChildHTTPContext(
		t,
		body,
		path,
		"memory_consolidation",
		true,
		"",
	)
}

func newOfficialOpenAIChildHTTPContext(
	t *testing.T,
	body []byte,
	path string,
	subagent string,
	memoryGeneration bool,
	parentThreadID string,
) *gin.Context {
	t.Helper()
	c := newOfficialOpenAIHTTPTestContext(body, path)
	c.Request.Header.Set("session-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("thread-id", testOfficialOpenAIChildThreadID)
	c.Request.Header.Set("x-client-request-id", testOfficialOpenAIChildThreadID)
	c.Request.Header.Set("x-codex-window-id", testOfficialOpenAIChildThreadID+":0")
	c.Request.Header.Set("x-openai-subagent", subagent)
	if memoryGeneration {
		c.Request.Header.Set("x-openai-memgen-request", "true")
	}
	if parentThreadID != "" {
		c.Request.Header.Set("x-codex-parent-thread-id", parentThreadID)
	}
	return c
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
	c.Request.Header.Set("User-Agent", officialOpenAIHTTPUserAgent)
	c.Request.Header.Set("originator", "codex_exec")
	c.Request.Header.Set("authorization", "Bearer oauth-test-token")
	c.Request.Header.Set("chatgpt-account-id", "chatgpt-test-account")
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
	setOfficialCodexForceHTTPFallback(c, true)
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
	service := &OpenAIGatewayService{
		cfg: &config.Config{Security: config.SecurityConfig{
			URLAllowlist: config.URLAllowlistConfig{Enabled: false},
		}},
		httpUpstream: upstream,
	}
	service.openaiModelCapabilities.replaceFromManifest(
		94,
		[]byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`),
	)
	return service
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
	if strings.EqualFold(req.Header.Get("Content-Encoding"), "zstd") {
		decoder, decodeErr := zstd.NewReader(nil)
		require.NoError(t, decodeErr)
		defer decoder.Close()
		data, decodeErr = decoder.DecodeAll(data, nil)
		require.NoError(t, decodeErr)
	}
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
