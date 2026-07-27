package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const testOfficialOpenAIWSTurnID = "7a46fb58-2930-4d6c-9cca-ea1124fcc871"

func TestOpenAIOfficialEgressWSContextFreezesIngressIdentity(t *testing.T) {
	ctx, egressContext, c, _ := newOfficialOpenAIWSContextForTest(t)

	require.True(t, egressContext.IsFrozen())
	require.Equal(
		t,
		testOfficialOpenAISessionID,
		mustOfficialEgressField(t, egressContext, OfficialEgressFieldSessionID).Value(),
	)
	require.Equal(
		t,
		testOfficialOpenAISessionID,
		mustOfficialEgressField(t, egressContext, OfficialEgressFieldClientRequestID).Value(),
	)
	require.Equal(
		t,
		c.GetHeader(openAIWSTurnMetadataHeader),
		mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value(),
	)
	_, enabled := OfficialEgressContextFromContext(ctx)
	require.True(t, enabled)
}

func TestOpenAIOfficialEgressWSHandshakeUsesFrozenOfficialHeaders(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	headers := http.Header{
		"User-Agent":              []string{"codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64)"},
		"Originator":              []string{"codex_exec"},
		"X-Codex-Beta-Features":   []string{"remote_compaction_v2"},
		"Session_id":              []string{"legacy-session"},
		"Conversation_id":         []string{"legacy-conversation"},
		"X-Codex-Installation-Id": []string{testOfficialOpenAIInstallationID},
	}

	result, err := finalizeOpenAIOfficialEgressWSHandshakeHeaders(ctx, headers)
	require.NoError(t, err)
	require.NotEmpty(t, result.Modifications)
	require.Empty(t, headers.Get("session_id"))
	require.Empty(t, headers.Get("conversation_id"))
	require.Empty(t, headers.Get("x-codex-installation-id"))
	require.Equal(t, testOfficialOpenAISessionID, headers.Get("session-id"))
	require.Equal(t, testOfficialOpenAISessionID, headers.Get("thread-id"))
	require.Equal(t, testOfficialOpenAISessionID, headers.Get("x-client-request-id"))
	require.Equal(t, testOfficialOpenAISessionID+":0", headers.Get("x-codex-window-id"))
	require.Equal(t, openAIWSBetaV2Value, headers.Get("OpenAI-Beta"))
	require.Equal(t, officialOpenAIWSClientVersion, headers.Get("version"))
}

func TestOpenAIOfficialEgressWSDerivesKiloIdentityAndCanonicalFrame(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "Kilo-Code/7.4.0")
	c.Request.Header.Set("X-Session-Affinity", "kilo-ws-session-1")
	account := newOfficialOpenAIHTTPTestAccount(94)
	firstPayload := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"instructions":"只回复 KILO_WS_OK",
		"input":[
			{"role":"user","content":[{"type":"input_text","text":"第一轮","opaque":{"z":9007199254740993,"a":1}}]}
		],
		"tools":[
			{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}
		],
		"parallel_tool_calls":true,
		"store":true,
		"stream":true,
		"max_output_tokens":32000,
		"reasoning":{"effort":"high","summary":"detailed"}
	}`)

	ctx, err := attachOfficialEgressWebSocketContext(
		withOpenAIResponsesLiteCapability(context.Background(), true),
		c,
		account,
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
	)
	require.NoError(t, err)
	egressContext, exists := OfficialEgressContextFromContext(ctx)
	require.True(t, exists)
	require.True(t, egressContext.IsFrozen())
	require.NotNil(t, egressContext.openAIWSDerived)
	require.Equal(
		t,
		OfficialEgressFieldSourceDerived,
		mustOfficialEgressField(t, egressContext, OfficialEgressFieldSessionID).Source,
	)

	headers := http.Header{
		"User-Agent": []string{"Kilo-Code/7.4.0"},
		"Originator": []string{"third-party"},
	}
	_, err = finalizeOpenAIOfficialEgressWSHandshakeHeaders(ctx, headers)
	require.NoError(t, err)
	require.Equal(t, officialOpenAIHTTPUserAgent, headers.Get("User-Agent"))
	require.Equal(t, officialOpenAIHTTPOriginator, headers.Get("originator"))
	require.Equal(t, officialOpenAIHTTPBetaFeatures, headers.Get("x-codex-beta-features"))
	require.Equal(t, openAIWSBetaV2Value, headers.Get("OpenAI-Beta"))
	require.Equal(t, officialOpenAIWSClientVersion, headers.Get("version"))

	firstFinal, firstResult, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		firstPayload,
		firstPayload,
		"",
		false,
	)
	require.NoError(t, err)
	require.NotEmpty(t, firstResult.Modifications)
	require.False(t, gjson.GetBytes(firstFinal, "instructions").Exists())
	require.False(t, gjson.GetBytes(firstFinal, "tools").Exists())
	require.False(t, gjson.GetBytes(firstFinal, "parallel_tool_calls").Bool())
	require.False(t, gjson.GetBytes(firstFinal, "store").Bool())
	require.True(t, gjson.GetBytes(firstFinal, "stream").Bool())
	require.Equal(t, "auto", gjson.GetBytes(firstFinal, "tool_choice").String())
	require.Equal(t, "all_turns", gjson.GetBytes(firstFinal, "reasoning.context").String())
	require.Equal(t, "high", gjson.GetBytes(firstFinal, "reasoning.effort").String())
	require.Equal(t, "detailed", gjson.GetBytes(firstFinal, "reasoning.summary").String())
	require.Equal(t, "low", gjson.GetBytes(firstFinal, "text.verbosity").String())
	require.False(t, gjson.GetBytes(firstFinal, "max_output_tokens").Exists())
	require.Equal(
		t,
		"reasoning.encrypted_content",
		gjson.GetBytes(firstFinal, "include.0").String(),
	)
	require.Equal(
		t,
		"additional_tools",
		gjson.GetBytes(firstFinal, "input.0.type").String(),
	)
	require.Equal(t, "developer", gjson.GetBytes(firstFinal, "input.1.role").String())
	require.Equal(t, "message", gjson.GetBytes(firstFinal, "input.2.type").String())
	require.Equal(t, "user", gjson.GetBytes(firstFinal, "input.2.role").String())
	require.Contains(t, string(firstFinal), `"opaque":{"z":9007199254740993,"a":1}`)
	require.NotContains(t, string(firstFinal), "9007199254740992")
	require.Equal(
		t,
		"true",
		gjson.GetBytes(
			firstFinal,
			"client_metadata.ws_request_header_x_openai_internal_codex_responses_lite",
		).String(),
	)
	firstTurnID := gjson.GetBytes(firstFinal, "client_metadata.turn_id").String()
	require.NotEmpty(t, firstTurnID)
	for index := range gjson.GetBytes(firstFinal, "input").Array() {
		require.Equal(
			t,
			firstTurnID,
			gjson.GetBytes(
				firstFinal,
				"input."+strconv.Itoa(index)+"."+officialOpenAIWSItemTurnMetadata+".turn_id",
			).String(),
		)
	}
	require.Equal(
		t,
		gjson.GetBytes(firstFinal, "prompt_cache_key").String(),
		gjson.GetBytes(firstFinal, "client_metadata.session_id").String(),
	)

	prewarm, shouldInject, err :=
		buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, firstFinal)
	require.NoError(t, err)
	require.True(t, shouldInject)
	require.True(t, gjson.GetBytes(prewarm, "generate").Exists())
	require.False(t, gjson.GetBytes(prewarm, "generate").Bool())
	require.False(t, gjson.GetBytes(prewarm, "previous_response_id").Exists())
	require.Equal(
		t,
		[]string{"additional_tools", "message"},
		[]string{
			gjson.GetBytes(prewarm, "input.0.type").String(),
			gjson.GetBytes(prewarm, "input.1.type").String(),
		},
	)
	require.Equal(t, "developer", gjson.GetBytes(prewarm, "input.1.role").String())
	require.Equal(t, "", gjson.GetBytes(prewarm, "client_metadata.turn_id").String())
	for index := range gjson.GetBytes(prewarm, "input").Array() {
		require.False(
			t,
			gjson.GetBytes(
				prewarm,
				"input."+strconv.Itoa(index)+"."+officialOpenAIWSItemTurnMetadata,
			).Exists(),
		)
	}
	prewarmTurnMetadata := gjson.Parse(
		gjson.GetBytes(prewarm, "client_metadata.x-codex-turn-metadata").String(),
	)
	require.Equal(t, "prewarm", prewarmTurnMetadata.Get("request_kind").String())

	business, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(
		ctx,
		firstFinal,
		"resp_kilo_prewarm_1",
	)
	require.NoError(t, err)
	require.Equal(
		t,
		"resp_kilo_prewarm_1",
		gjson.GetBytes(business, "previous_response_id").String(),
	)
	require.False(t, gjson.GetBytes(business, "generate").Exists())
	require.Equal(t, "message", gjson.GetBytes(business, "input.0.type").String())
	require.Equal(t, "developer", gjson.GetBytes(business, "input.0.role").String())
	require.Equal(t, "message", gjson.GetBytes(business, "input.1.type").String())
	require.Equal(t, "user", gjson.GetBytes(business, "input.1.role").String())
	businessTurnID := gjson.GetBytes(business, "client_metadata.turn_id").String()
	require.NotEmpty(t, businessTurnID)
	for index := range gjson.GetBytes(business, "input").Array() {
		require.Equal(
			t,
			businessTurnID,
			gjson.GetBytes(
				business,
				"input."+strconv.Itoa(index)+"."+officialOpenAIWSItemTurnMetadata+".turn_id",
			).String(),
		)
	}
	businessTurnMetadata := gjson.Parse(
		gjson.GetBytes(business, "client_metadata.x-codex-turn-metadata").String(),
	)
	require.Equal(t, "turn", businessTurnMetadata.Get("request_kind").String())

	continuation := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"previous_response_id":"resp_kilo_ws_1",
		"input":[{"type":"function_call_output","call_id":"call_kilo_ws_1","output":"ok"}]
	}`)
	secondFinal, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		continuation,
		continuation,
		"resp_kilo_ws_1",
		false,
	)
	require.NoError(t, err)
	require.Equal(
		t,
		businessTurnID,
		gjson.GetBytes(secondFinal, "client_metadata.turn_id").String(),
	)
	require.Equal(
		t,
		"call_kilo_ws_1",
		gjson.GetBytes(secondFinal, "input.0.call_id").String(),
	)
	require.Equal(
		t,
		"resp_kilo_ws_1",
		gjson.GetBytes(secondFinal, "previous_response_id").String(),
	)

	fullHistoryToolRequest := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"input":[
			{"type":"additional_tools","tools":[]},
			{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件\n<environment_details>time=1</environment_details>"}]}
		]
	}`)
	toolRequestFinal, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		fullHistoryToolRequest,
		fullHistoryToolRequest,
		"resp_kilo_ws_before_tool",
		false,
	)
	require.NoError(t, err)
	toolRequestTurnID := gjson.GetBytes(
		toolRequestFinal,
		"client_metadata.turn_id",
	).String()
	require.NotEmpty(t, toolRequestTurnID)

	fullHistoryToolContinuation := []byte(`{
		"type":"response.create",
		"model":"gpt-5.6-luna",
		"input":[
			{"type":"additional_tools","tools":[]},
			{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"读取文件\n<environment_details>time=2</environment_details>"}]},
			{"type":"reasoning","encrypted_content":"encrypted"},
			{"type":"function_call","call_id":"call_kilo_ws_2","name":"read","arguments":"{}"},
			{"type":"function_call_output","call_id":"call_kilo_ws_2","output":"ok"}
		]
	}`)
	fullHistoryFinal, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		fullHistoryToolContinuation,
		fullHistoryToolContinuation,
		"resp_kilo_ws_tool_parent",
		false,
	)
	require.NoError(t, err)
	fullHistoryTurnID := gjson.GetBytes(
		fullHistoryFinal,
		"client_metadata.turn_id",
	).String()
	require.Equal(t, toolRequestTurnID, fullHistoryTurnID)
	toolDelta, changed, err :=
		buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(
			ctx,
			fullHistoryFinal,
			"resp_kilo_ws_tool_parent",
		)
	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(
		t,
		"resp_kilo_ws_tool_parent",
		gjson.GetBytes(toolDelta, "previous_response_id").String(),
	)
	require.Equal(t, 1, len(gjson.GetBytes(toolDelta, "input").Array()))
	require.Equal(
		t,
		"function_call_output",
		gjson.GetBytes(toolDelta, "input.0.type").String(),
	)
	require.Equal(
		t,
		"call_kilo_ws_2",
		gjson.GetBytes(toolDelta, "input.0.call_id").String(),
	)
	require.Equal(
		t,
		fullHistoryTurnID,
		gjson.GetBytes(toolDelta, "client_metadata.turn_id").String(),
	)
	require.Equal(
		t,
		fullHistoryTurnID,
		gjson.GetBytes(
			toolDelta,
			"input.0."+officialOpenAIWSItemTurnMetadata+".turn_id",
		).String(),
	)
}

func TestOpenAIOfficialEgressWSDerivedNonLiteUsesOfficialContract(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "third-party-ws/1.0")
	c.Request.Header.Set("X-Session-Affinity", "nonlite-ws-session")
	payload := []byte(`{
		"type":"response.create",
		"model":"gpt-5.4",
		"instructions":"keep-top-level",
		"input":[{"role":"user","content":[{"type":"input_text","text":"hello"}]}],
		"tools":[{"type":"function","name":"lookup","parameters":{"type":"object"}}],
		"parallel_tool_calls":false,
		"store":true,
		"stream":false,
		"tool_choice":"required",
		"include":["message.output_text.logprobs"],
		"max_output_tokens":32000,
		"reasoning":{"effort":"high","context":"none"},
		"text":{"verbosity":"high"}
	}`)

	ctx, err := attachOfficialEgressWebSocketContext(
		withOpenAIModelCapabilities(context.Background(), openAIModelCapabilities{
			SupportsParallelToolCalls: true,
		}),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		"wss://chatgpt.com/backend-api/codex/responses",
		payload,
	)
	require.NoError(t, err)
	finalized, _, err := finalizeOpenAIOfficialEgressWSFrame(ctx, payload, payload, "", false)
	require.NoError(t, err)
	require.Equal(t, "keep-top-level", gjson.GetBytes(finalized, "instructions").String())
	require.Equal(t, "lookup", gjson.GetBytes(finalized, "tools.0.name").String())
	require.Len(t, gjson.GetBytes(finalized, "input").Array(), 1)
	require.True(t, gjson.GetBytes(finalized, "parallel_tool_calls").Bool())
	require.False(t, gjson.GetBytes(finalized, "store").Bool())
	require.True(t, gjson.GetBytes(finalized, "stream").Bool())
	require.Equal(t, "auto", gjson.GetBytes(finalized, "tool_choice").String())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(finalized, "include.0").String())
	require.Equal(t, "high", gjson.GetBytes(finalized, "text.verbosity").String())
	require.Equal(t, "high", gjson.GetBytes(finalized, "reasoning.effort").String())
	require.False(t, gjson.GetBytes(finalized, "reasoning.context").Exists())
	require.False(t, gjson.GetBytes(finalized, "max_output_tokens").Exists())
}

func TestOpenAIOfficialEgressWSConnectionIdentitySeparatesSessions(t *testing.T) {
	newContext := func(session string) *OfficialEgressContext {
		gin.SetMode(gin.TestMode)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
		c.Request.Header.Set("User-Agent", "Kilo-Code/7.4.0")
		c.Request.Header.Set("X-Session-Affinity", session)
		account := newOfficialOpenAIHTTPTestAccount(94)
		ctx, err := attachOfficialEgressWebSocketContext(
			context.Background(),
			c,
			account,
			"wss://chatgpt.com/backend-api/codex/responses",
			[]byte(`{
				"type":"response.create",
				"model":"gpt-5.6-luna",
				"input":[{"role":"user","content":[{"type":"input_text","text":"你好"}]}]
			}`),
		)
		require.NoError(t, err)
		egressContext, exists := OfficialEgressContextFromContext(ctx)
		require.True(t, exists)
		return egressContext
	}

	first := newContext("kilo-session-a")
	second := newContext("kilo-session-b")
	firstKey := officialEgressWebSocketIdentityKey(first)
	secondKey := officialEgressWebSocketIdentityKey(second)
	require.NotEmpty(t, firstKey)
	require.NotEmpty(t, secondKey)
	require.NotEqual(t, firstKey, secondKey)
	require.NotContains(t, firstKey, "kilo-session")
	require.NotContains(t, secondKey, "kilo-session")
}

func TestOpenAIOfficialEgressWSFramePreservesValidContinuation(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	previousResponseID := "resp_valid_previous"
	original := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"previous_response_id":"resp_valid_previous",
		"input":[
			{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]},
			{"type":"custom_tool_call_output","call_id":"call_t6","output":"ok"}
		]
	}`))

	finalized, result, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		original,
		previousResponseID,
		false,
	)
	require.NoError(t, err)
	require.NotEmpty(t, result.Modifications)
	require.Equal(
		t,
		testOfficialOpenAIWSTurnID,
		gjson.GetBytes(
			finalized,
			"input.0."+officialOpenAIWSItemTurnMetadata+".turn_id",
		).String(),
	)
	require.Equal(
		t,
		"call_t6",
		gjson.GetBytes(finalized, "input.1.call_id").String(),
	)
	require.True(t, shouldPreserveOpenAIOfficialEgressWSPreviousResponseID(
		ctx,
		previousResponseID,
		previousResponseID,
	))
	require.False(t, shouldPreserveOpenAIOfficialEgressWSPreviousResponseID(
		ctx,
		"resp_other",
		previousResponseID,
	))
}

func TestOpenAIOfficialEgressWSFrameRejectsHistoryExpansionAndIdentityChanges(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	original := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"previous_response_id":"resp_valid_previous",
		"prompt_cache_key":"019f9577-d69f-7892-809e-8a3a4198c671",
		"input":[{"type":"custom_tool_call_output","call_id":"call_t6","output":"ok"}]
	}`))

	withoutPrevious := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"prompt_cache_key":"019f9577-d69f-7892-809e-8a3a4198c671",
		"input":[{"type":"custom_tool_call_output","call_id":"call_t6","output":"ok"}]
	}`))
	_, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		withoutPrevious,
		"resp_valid_previous",
		false,
	)
	require.ErrorContains(t, err, "previous_response_id")

	expandedInput := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"previous_response_id":"resp_valid_previous",
		"prompt_cache_key":"019f9577-d69f-7892-809e-8a3a4198c671",
		"input":[
			{"type":"additional_tools","tools":[{"type":"custom","name":"exec"}]},
			{"type":"custom_tool_call_output","call_id":"call_t6","output":"ok"}
		]
	}`))
	_, _, err = finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		expandedInput,
		"resp_valid_previous",
		false,
	)
	require.ErrorContains(t, err, "input")

	// 上游已经明确返回 previous_response_not_found 后，现有受控回放仍可工作。
	finalized, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		expandedInput,
		"resp_valid_previous",
		true,
	)
	require.NoError(t, err)
	require.Equal(
		t,
		testOfficialOpenAIWSTurnID,
		gjson.GetBytes(
			finalized,
			"input.1."+officialOpenAIWSItemTurnMetadata+".turn_id",
		).String(),
	)
}

func TestOpenAIOfficialEgressWSFrameAddsHistoricalAndCurrentTurnMetadata(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	original := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"input":[
			{"type":"message","role":"developer","content":[{"type":"input_text","text":"规则"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"第一轮环境"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"第一轮问题"}]},
			{"type":"message","role":"assistant","content":[{"type":"output_text","text":"第一轮回答"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"第二轮环境"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"第二轮问题"}]}
		]
	}`))

	finalized, result, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		original,
		"",
		false,
	)
	require.NoError(t, err)
	require.NotEmpty(t, result.Modifications)
	historicalTurnID := gjson.GetBytes(
		finalized,
		"input.0."+officialOpenAIWSItemTurnMetadata+".turn_id",
	).String()
	require.NotEmpty(t, historicalTurnID)
	require.NotEqual(t, testOfficialOpenAIWSTurnID, historicalTurnID)
	for index := 0; index < 4; index++ {
		require.Equal(
			t,
			historicalTurnID,
			gjson.GetBytes(
				finalized,
				"input."+strconv.Itoa(index)+"."+officialOpenAIWSItemTurnMetadata+".turn_id",
			).String(),
		)
	}
	for index := 4; index < 6; index++ {
		require.Equal(
			t,
			testOfficialOpenAIWSTurnID,
			gjson.GetBytes(
				finalized,
				"input."+strconv.Itoa(index)+"."+officialOpenAIWSItemTurnMetadata+".turn_id",
			).String(),
		)
	}
	require.Equal(t, "第一轮问题", gjson.GetBytes(finalized, "input.2.content.0.text").String())
	require.Equal(t, "第二轮问题", gjson.GetBytes(finalized, "input.5.content.0.text").String())
}

func TestOpenAIOfficialEgressWSFrameRejectsConflictingItemTurnMetadata(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	original := withOfficialOpenAIWSBusinessMetadataForTest(t, []byte(`{
		"type":"response.create",
		"input":[{
			"type":"message",
			"role":"user",
			"content":[{"type":"input_text","text":"你好"}],
			"internal_chat_message_metadata_passthrough":{"turn_id":"bee3cd38-4511-4497-899e-f19f04f953fd"}
		}]
	}`))

	_, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		original,
		"",
		false,
	)
	require.ErrorContains(t, err, "turn_id conflicts with its turn")
}

func TestOpenAIOfficialEgressWSFrameWithoutProfilePreservesPayload(t *testing.T) {
	original := []byte(`{
		"type":"response.create",
		"input":[{"type":"message","role":"user","content":"你好"}]
	}`)
	finalized, result, err := finalizeOpenAIOfficialEgressWSFrame(
		context.Background(),
		original,
		original,
		"",
		false,
	)
	require.NoError(t, err)
	require.Empty(t, result.Modifications)
	require.Equal(t, original, finalized)
}

func TestOpenAIOfficialEgressWSUnknownFrameIsNeverModified(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	original := []byte(`{"type":"session.update","session":{"model":"gpt-5.6-luna"}}`)

	finalized, _, err := finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		original,
		"",
		false,
	)
	require.NoError(t, err)
	require.Equal(t, original, finalized)

	_, _, err = finalizeOpenAIOfficialEgressWSFrame(
		ctx,
		original,
		[]byte(`{"type":"session.update","session":{"model":"changed"}}`),
		"",
		false,
	)
	require.ErrorContains(t, err, "unknown WebSocket frame was modified")
}

func TestOfficialEgressWebSocketRoundTripperAddsClientWindowBits(t *testing.T) {
	base := &officialEgressWSRoundTripRecorder{}
	transport := &officialEgressWebSocketRoundTripper{base: base}
	req := httptest.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api/codex/responses", nil)
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate")

	resp, err := transport.RoundTrip(req)
	require.NoError(t, err)
	require.NotNil(t, resp)
	require.Equal(t, "permessage-deflate", req.Header.Get("Sec-WebSocket-Extensions"))
	require.Equal(
		t,
		officialOpenAIWSCompressionOffer,
		base.request.Header.Get("Sec-WebSocket-Extensions"),
	)
	transport.CloseIdleConnections()
	require.True(t, base.closed)
}

func newOfficialOpenAIWSContextForTest(
	t *testing.T,
) (context.Context, *OfficialEgressContext, *gin.Context, []byte) {
	t.Helper()
	firstPayload := newOfficialOpenAIWSFirstFrameForTest(t)
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	applyOfficialOpenAIWSIngressHeadersForTest(c, firstPayload)
	account := newOfficialOpenAIHTTPTestAccount(94)
	ctx, err := attachOfficialEgressWebSocketContext(
		context.Background(),
		c,
		account,
		"wss://chatgpt.com/backend-api/codex/responses",
		firstPayload,
	)
	require.NoError(t, err)
	egressContext, exists := OfficialEgressContextFromContext(ctx)
	require.True(t, exists)
	return ctx, egressContext, c, firstPayload
}

func newOfficialOpenAIWSFirstFrameForTest(t *testing.T) []byte {
	t.Helper()
	turnMetadata := `{"installation_id":"` + testOfficialOpenAIInstallationID +
		`","session_id":"` + testOfficialOpenAISessionID +
		`","thread_id":"` + testOfficialOpenAISessionID +
		`","turn_id":"","window_id":"` + testOfficialOpenAISessionID +
		`:0","request_kind":"prewarm","thread_source":"user","sandbox":"seccomp"}`
	payload := map[string]any{
		"type":             officialOpenAIWSResponseCreateType,
		"model":            "gpt-5.6-luna",
		"generate":         false,
		"stream":           true,
		"store":            false,
		"prompt_cache_key": testOfficialOpenAISessionID,
		"client_metadata": map[string]any{
			"x-codex-installation-id": testOfficialOpenAIInstallationID,
			"session_id":              testOfficialOpenAISessionID,
			"thread_id":               testOfficialOpenAISessionID,
			"turn_id":                 "",
			"x-codex-window-id":       testOfficialOpenAISessionID + ":0",
			"x-codex-turn-metadata":   turnMetadata,
		},
		"input": []any{
			map[string]any{"type": "additional_tools", "tools": []any{}},
			map[string]any{
				"type": "message",
				"role": "user",
				"content": []any{
					map[string]any{"type": "input_text", "text": "你好"},
				},
			},
		},
	}
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return body
}

func withOfficialOpenAIWSBusinessMetadataForTest(t *testing.T, body []byte) []byte {
	t.Helper()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	payload["client_metadata"] = map[string]any{
		"session_id": testOfficialOpenAISessionID,
		"turn_id":    testOfficialOpenAIWSTurnID,
	}
	finalized, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return finalized
}

func applyOfficialOpenAIWSIngressHeadersForTest(c *gin.Context, firstPayload []byte) {
	if c == nil || c.Request == nil {
		return
	}
	turnMetadata := gjson.GetBytes(
		firstPayload,
		"client_metadata.x-codex-turn-metadata",
	).String()
	c.Request.Header.Set(
		"User-Agent",
		"codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.145.0)",
	)
	c.Request.Header.Set("originator", "codex_exec")
	c.Request.Header.Set("OpenAI-Beta", openAIWSBetaV2Value)
	c.Request.Header.Set("x-codex-beta-features", "remote_compaction_v2")
	c.Request.Header.Set("x-client-request-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("session-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("thread-id", testOfficialOpenAISessionID)
	c.Request.Header.Set("x-codex-window-id", testOfficialOpenAISessionID+":0")
	c.Request.Header.Set(openAIWSTurnMetadataHeader, turnMetadata)
}

type officialEgressWSRoundTripRecorder struct {
	request *http.Request
	closed  bool
}

func (r *officialEgressWSRoundTripRecorder) RoundTrip(
	req *http.Request,
) (*http.Response, error) {
	r.request = req.Clone(req.Context())
	r.request.Header = req.Header.Clone()
	return &http.Response{
		StatusCode: http.StatusSwitchingProtocols,
		Header:     http.Header{},
		Body:       io.NopCloser(bytes.NewReader(nil)),
		Request:    req,
	}, nil
}

func (r *officialEgressWSRoundTripRecorder) CloseIdleConnections() {
	r.closed = true
}
