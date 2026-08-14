package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const candidateTraceFactPrefix = "CANDIDATE_TRACE_FACT "

// candidateTraceTargetProfile 解析本源码树的**目标**画像，而不是写死某个版本号。
//
// 依据是候选构建的既定槽位约定：egresscatalogstage 把待验收的目标画像装进
// previous 槽位并保持 Active 不变（catalog_stage.go 的 candidate_release_mode
// 固定为 previous，且导入时机器断言「候选导入意外修改了当前 Active」）。因此
//   - 主仓库：active 与 previous 同为 0.145.0，本函数返回 0.145.0；
//   - 候选源码树：active=0.145.0、previous=目标版本，本函数返回目标版本。
//
// surface identity 事实（a15.*）的 user_agent_prefix 必须随目标画像变化，否则
// 候选验收侧的 SPEC-HDR-005 会拿到上一版本的 UA。写死版本号则两侧只能满足一
// 侧：写 0.145 候选侧判据失败，写 0.147 主仓库根本没有该画像、测试直接报
// 「未知 Codex 官方出站版本画像」。
//
// 这里不额外断言"必须是 0.147"：目标版本是候选树的属性，测试不重复声明它。
// 版本若解析错误，产出的 user_agent_prefix 随即不符，accept 重放照样失败关闭。
func candidateTraceTargetProfile(t *testing.T) *officialCodexVersionProfile {
	t.Helper()
	profile, err := resolveCodexVersionProfileForMode(officialClientProfileModePrevious)
	require.NoError(t, err)
	return profile
}

type candidateTraceFactEnvelope struct {
	SchemaVersion string         `json:"schema_version"`
	FactID        string         `json:"fact_id"`
	ScenarioID    string         `json:"scenario_id"`
	RecordType    string         `json:"record_type"`
	Data          map[string]any `json:"data"`
}

// candidateTraceLogFact 只能在调用方完成全部 require 断言后调用。生成器还会核对
// 本文件摘要、精确测试名与 go test pass 事件，因此单独复制这行日志不能生成证据。
func candidateTraceLogFact(
	t *testing.T,
	factID string,
	scenarioID string,
	recordType string,
	data map[string]any,
) {
	t.Helper()
	encoded, err := json.Marshal(candidateTraceFactEnvelope{
		SchemaVersion: "codex-candidate-test-fact/v1",
		FactID:        factID,
		ScenarioID:    scenarioID,
		RecordType:    recordType,
		Data:          data,
	})
	require.NoError(t, err)
	t.Log(candidateTraceFactPrefix + string(encoded))
}

func candidateTraceSHA256(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func TestCandidateTraceCodex0145RuntimeAndBoundaryFacts(t *testing.T) {
	profile := candidateTraceTargetProfile(t)
	targetVersion := profile.Version

	execUserAgent, err := profile.RenderUserAgent(officialCodexSurfaceExec, true)
	require.NoError(t, err)
	tuiUserAgent, err := profile.RenderUserAgent(officialCodexSurfaceTUI, true)
	require.NoError(t, err)
	modelsWithoutSuffix, err := profile.RenderUserAgent(officialCodexSurfaceExec, false)
	require.NoError(t, err)
	require.True(t, strings.HasPrefix(execUserAgent, "codex_exec/"+targetVersion+" "))
	require.True(t, strings.HasSuffix(execUserAgent, "(codex_exec; "+targetVersion+")"))
	require.True(t, strings.HasPrefix(tuiUserAgent, "codex-tui/"+targetVersion+" "))
	require.True(t, strings.HasSuffix(tuiUserAgent, "(codex-tui; "+targetVersion+")"))
	require.NotContains(t, modelsWithoutSuffix, "(codex_exec; "+targetVersion+")")

	modelsState, err := resolveOfficialCodexRuntimeState(
		officialCodex0145RuntimeIngress(modelsWithoutSuffix, "codex_cli_rs"),
		officialEgressTestAccount(145, PlatformOpenAI), officialClientProfileModeActive,

		codexEndpointID(officialCodexEndpointModels))

	require.NoError(t, err)
	// 生产入口不能再用入站 UA 切换 wire 运行态；统一编译器始终采用 release
	// Build 默认身份。画像仍独立保留启动 models 可无 suffix 的官方观测能力。
	require.Equal(t, officialCodexProcessPhaseInitialized, modelsState.ProcessPhase)
	require.True(t, modelsState.UserAgentSuffixEnabled)
	require.Equal(t, "codex_exec", modelsState.Originator)

	candidateTraceLogFact(t, "a15.surface-exec", "A15", "surface_identity", map[string]any{
		"endpoint":          "models",
		"originator":        "codex_exec",
		"surface":           "exec",
		"suffix_state":      "present",
		"user_agent_prefix": strings.Fields(execUserAgent)[0],
		"user_agent_suffix": execUserAgent[strings.LastIndex(execUserAgent, "("):],
	})
	candidateTraceLogFact(t, "a15.surface-tui", "A15", "surface_identity", map[string]any{
		"endpoint":          "models",
		"originator":        "codex-tui",
		"surface":           "tui",
		"suffix_state":      "present",
		"user_agent_prefix": strings.Fields(tuiUserAgent)[0],
		"user_agent_suffix": tuiUserAgent[strings.LastIndex(tuiUserAgent, "("):],
	})
	candidateTraceLogFact(t, "a15.models-no-suffix", "A15", "surface_identity", map[string]any{
		"endpoint":          "models",
		"originator":        "codex_cli_rs",
		"surface":           "exec",
		"suffix_state":      "absent",
		"user_agent_prefix": strings.Fields(modelsWithoutSuffix)[0],
		"user_agent_suffix": "",
	})

	account := officialEgressTestAccount(145, PlatformOpenAI)
	account.Extra[officialCodexRuntimeMetricsAccountExtra] = true
	parentThreadID := uuid.NewString()
	memgenIngress := officialCodex0145RuntimeIngress(execUserAgent, "codex_exec")
	memgenIngress.Request.Header.Set("x-openai-subagent", "memory_consolidation")
	memgenIngress.Request.Header.Set("x-openai-memgen-request", "true")
	memgenIngress.Request.Header.Set(
		"x-codex-turn-metadata",
		`{"thread_source":"memory_consolidation"}`,
	)
	memgenState, err := resolveOfficialCodexRuntimeState(memgenIngress, account, officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "memory_consolidation", memgenState.ConditionalHeaders["x-openai-subagent"])
	require.Equal(t, "true", memgenState.ConditionalHeaders["x-openai-memgen-request"])
	require.Equal(t, "true", memgenState.ConditionalHeaders["x-responsesapi-include-timing-metrics"])

	spawnIngress := officialCodex0145RuntimeIngress(execUserAgent, "codex_exec")
	spawnIngress.Request.Header.Set("x-openai-subagent", "collab_spawn")
	spawnIngress.Request.Header.Set("x-codex-parent-thread-id", parentThreadID)
	spawnIngress.Request.Header.Set(
		"x-codex-turn-metadata",
		`{"thread_source":"subagent","subagent_kind":"thread_spawn","parent_thread_id":"`+parentThreadID+`"}`,
	)
	spawnState, err := resolveOfficialCodexRuntimeState(spawnIngress, account, officialClientProfileModeActive, officialClientProfileModeActive)
	require.NoError(t, err)
	require.Equal(t, "collab_spawn", spawnState.ConditionalHeaders["x-openai-subagent"])
	require.Equal(t, parentThreadID, spawnState.ConditionalHeaders["x-codex-parent-thread-id"])
	candidateTraceLogFact(t, "a04.conditional-subagent", "A04", "conditional_header", map[string]any{
		"name":  "x-openai-subagent",
		"value": spawnState.ConditionalHeaders["x-openai-subagent"],
	})
	candidateTraceLogFact(t, "a04.conditional-memgen", "A04", "conditional_header", map[string]any{
		"name":  "x-openai-memgen-request",
		"value": memgenState.ConditionalHeaders["x-openai-memgen-request"],
	})
	candidateTraceLogFact(t, "a04.conditional-parent-thread", "A04", "conditional_header", map[string]any{
		"name":         "x-codex-parent-thread-id",
		"value_sha256": candidateTraceSHA256(spawnState.ConditionalHeaders["x-codex-parent-thread-id"]),
	})
	candidateTraceLogFact(t, "a04.conditional-timing-metrics", "A04", "conditional_header", map[string]any{
		"name":  "x-responsesapi-include-timing-metrics",
		"value": memgenState.ConditionalHeaders["x-responsesapi-include-timing-metrics"],
	})

	unknownBody := []byte(`{"model":"gpt-5.6-sol","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{"effort":"high","context":"all_turns"},"store":false,"stream":true,"include":[],"candidate_unknown_sentinel":true}`)
	unknownEgress, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		unknownBody,
		nil,
	)
	require.ErrorContains(t, err, "不允许 JSON 字段")
	require.NotContains(t, string(unknownEgress), "candidate_unknown_sentinel")
	candidateTraceLogFact(t, "a04.serialization-boundary", "A04", "serialization_boundary", map[string]any{
		"egress_occurrences": 0,
		"ingress_sentinel":   "candidate_unknown_sentinel",
	})

	nonLiteImageBody := []byte(`{"model":"gpt-5.6-sol","input":[],"tools":[{"type":"namespace","name":"image_gen","description":"生成图片","tools":[{"type":"function","name":"imagegen","description":"生成一张图片","strict":false,"parameters":{"type":"object"}}]}]}`)
	require.NoError(t, officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		nonLiteImageBody,
		false,
	))
	liteImageBody := []byte(`{"input":[{"type":"additional_tools","tools":[{"type":"namespace","name":"image_gen","description":"生成图片","tools":[{"type":"function","name":"imagegen","description":"生成一张图片","strict":false,"parameters":{"type":"object"}}]}]}]}`)
	require.NoError(t, officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesWS),
		liteImageBody,
		true,
	))
	nonLiteTool := gjson.GetBytes(nonLiteImageBody, "tools.0")
	liteCarrier := gjson.GetBytes(liteImageBody, "input.0.type").String()
	require.Equal(t, "image_gen", nonLiteTool.Get("name").String())
	require.Equal(t, "imagegen", nonLiteTool.Get("tools.0.name").String())
	require.Equal(t, "additional_tools", liteCarrier)
	require.Equal(t, 0, len(gjson.GetBytes(nonLiteImageBody, `tools.#(type=="image_generation")`).Array()))
	candidateTraceLogFact(t, "a09.image-tool-nonlite", "A09", "image_tool_flow", map[string]any{
		"hosted_image_generation_occurrences": 0,
		"mode":                                "non_lite",
		"namespace":                           nonLiteTool.Get("name").String(),
		"tool":                                nonLiteTool.Get("tools.0.name").String(),
	})
	candidateTraceLogFact(t, "a09.image-tool-lite", "A09", "image_tool_flow", map[string]any{
		"location": "input." + liteCarrier,
		"mode":     "lite",
	})

	editBody := []byte(`{"images":[{"image_url":"data:image/png;base64,c291cmNl"}],"prompt":"replace background","background":"auto","model":"gpt-image-2","quality":"high","size":"1024x1024"}`)
	orderedEdit, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointImagesEdits),
		editBody,
		nil,
	)
	require.NoError(t, err)
	editEndpoint, err := resolveCodexEndpoint(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointImagesEdits),
	)
	require.NoError(t, err)
	imageURL := gjson.GetBytes(orderedEdit, "images.0.image_url").String()
	parsedDataURL, err := url.Parse(imageURL)
	require.NoError(t, err)
	require.Equal(t, "data", parsedDataURL.Scheme)
	require.Equal(t, "application/json", editEndpoint.ContentType)
	require.False(t, strings.Contains(strings.ToLower(editEndpoint.ContentType), "multipart"))
	candidateTraceLogFact(t, "a09.image-edit-encoding", "A09", "image_edit_encoding", map[string]any{
		"content_type":         editEndpoint.ContentType,
		"images_scheme":        parsedDataURL.Scheme,
		"multipart_part_count": 0,
	})
}

func TestCandidateTraceCodex0145LiteAndPrefixFacts(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":false,
		"store":true,
		"parallel_tool_calls":true,
		"tool_choice":"required",
		"instructions":"candidate lite instruction",
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}],
		"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],
		"reasoning":{"effort":"medium","context":"none"}
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "candidate-lite-session")
	c.Request.URL.Path = "/v1/responses"
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamRecorder{resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_candidate_lite")}
	result, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
	)
	require.NoError(t, err)
	require.NotNil(t, result)
	firstTurn := upstream.lastBody
	require.False(t, gjson.GetBytes(firstTurn, "instructions").Exists())
	require.False(t, gjson.GetBytes(firstTurn, "tools").Exists())
	require.Equal(t, "additional_tools", gjson.GetBytes(firstTurn, "input.0.type").String())
	require.Equal(t, "all_turns", gjson.GetBytes(firstTurn, "reasoning.context").String())
	require.False(t, gjson.GetBytes(firstTurn, "parallel_tool_calls").Bool())

	previous := []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[{"type":"message","role":"user","content":"hello"}]}`)
	continuation := []byte(`{"type":"response.create","model":"gpt-5.6-luna","previous_response_id":"resp_candidate_prefix","input":[{"type":"message","role":"user","content":"hello"},{"type":"function_call_output","call_id":"call_candidate","output":"ok"}]}`)
	prefixReusable, err := openAIWSInputIsPrefixExtended(previous, continuation)
	require.NoError(t, err)
	require.True(t, prefixReusable)
	previousResponseID := gjson.GetBytes(continuation, "previous_response_id").String()
	require.NotEmpty(t, previousResponseID)
	additionalToolsOccurrences := len(
		gjson.GetBytes(continuation, `input.#(type=="additional_tools")`).Array(),
	)
	require.Equal(t, 0, additionalToolsOccurrences)

	candidateTraceLogFact(t, "a03.lite-transform", "A03", "lite_transform", map[string]any{
		"additional_tools_occurrences": additionalToolsOccurrences,
		"instructions_location":        "input.additional_tools",
		"parallel_tool_calls":          gjson.GetBytes(firstTurn, "parallel_tool_calls").Bool(),
		"reasoning_context":            gjson.GetBytes(firstTurn, "reasoning.context").String(),
		"tools_location":               "input.additional_tools",
		"transport":                    "websocket_incremental",
	})
	candidateTraceLogFact(t, "a06.prefix-reuse", "A06", "response_prefix_reuse", map[string]any{
		"prefix_reusable":               map[bool]string{true: "yes", false: "no"}[prefixReusable],
		"previous_response_id_presence": map[bool]string{true: "present", false: "absent"}[previousResponseID != ""],
	})
}

func TestCandidateTraceCodex0145TurnStateFacts(t *testing.T) {
	receivedTurnState := "turn-state-" + uuid.NewString()
	receivedSHA := candidateTraceSHA256(receivedTurnState)
	identity := officialOpenAIHTTPIdentity{
		installationID: uuid.NewString(),
		sessionID:      uuid.NewString(),
		threadID:       uuid.NewString(),
		windowID:       uuid.NewString(),
		clientRequest:  uuid.NewString(),
		turnMetadata:   `{"request_kind":"turn"}`,
	}

	httpHeaders := make(http.Header)
	finalizeOfficialOpenAIHTTPHeaders(
		httpHeaders,
		officialCodexVersion0145,
		"codex_exec/0.145.0",
		"codex_exec",
		identity,
		false,
		false,
		receivedTurnState,
	)
	httpReturned := httpHeaders.Get(openAIWSTurnStateHeader)
	require.Equal(t, receivedTurnState, httpReturned)

	upstreamEvent := []byte(`{"type":"response.metadata","headers":{"X-Codex-Turn-State":"` + receivedTurnState + `"}}`)
	wsReceived := extractOpenAIWSTurnStateFromUpstreamEvent(upstreamEvent)
	require.Equal(t, receivedTurnState, wsReceived)
	wsContext := WithOfficialEgressContext(
		context.Background(),
		NewOfficialEgressContext(OfficialEgressContextInput{
			ProfileMode: officialClientProfileModeActive,
		}),
	)
	wsFrame, err := injectOfficialOpenAIWSTurnState(
		wsContext,
		[]byte(`{"type":"response.create","model":"gpt-5.6-sol","input":[]}`),
		wsReceived,
	)
	require.NoError(t, err)
	wsReturned := gjson.GetBytes(wsFrame, "client_metadata.x-codex-turn-state").String()
	require.Equal(t, receivedTurnState, wsReturned)

	compactHeaders := make(http.Header)
	finalizeOfficialOpenAIHTTPHeaders(
		compactHeaders,
		officialCodexVersion0145,
		"codex_exec/0.145.0",
		"codex_exec",
		identity,
		true,
		false,
		receivedTurnState,
	)
	compactReturned := compactHeaders.Get(openAIWSTurnStateHeader)
	require.Equal(t, receivedTurnState, compactReturned)

	for _, fact := range []struct {
		id       string
		scenario string
		channel  string
		value    string
	}{
		{id: "a03.turn-state-http", scenario: "A03", channel: "http_header", value: httpReturned},
		{id: "a05.turn-state-ws", scenario: "A05", channel: "websocket_client_metadata", value: wsReturned},
		{id: "a09.turn-state-compact", scenario: "A09", channel: "legacy_compact_header", value: compactReturned},
	} {
		candidateTraceLogFact(t, fact.id, fact.scenario, "turn_state_chain", map[string]any{
			"received_sha256": receivedSHA,
			"return_channel":  fact.channel,
			"returned_sha256": candidateTraceSHA256(fact.value),
		})
	}
}

func TestCandidateTraceCodex0145WSCompressionAndDefaultTransport(t *testing.T) {
	received := make(chan string, 2)
	serverErrors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := coderws.Accept(writer, request, &coderws.AcceptOptions{
			CompressionMode: coderws.CompressionContextTakeover,
		})
		if err != nil {
			serverErrors <- err
			return
		}
		defer func() { _ = connection.CloseNow() }()
		for index := 0; index < 2; index++ {
			ctx, cancel := context.WithTimeout(request.Context(), 3*time.Second)
			_, payload, readErr := connection.Read(ctx)
			cancel()
			if readErr != nil {
				serverErrors <- readErr
				return
			}
			received <- string(payload)
		}
		serverErrors <- nil
	}))
	defer server.Close()

	dialer := newDefaultOpenAIWSClientDialer()
	connection, status, responseHeaders, err := dialer.Dial(
		context.Background(),
		"ws"+strings.TrimPrefix(server.URL, "http"),
		http.Header{"Sec-WebSocket-Extensions": {officialOpenAIWSCompressionOffer}},
		"",
	)
	require.NoError(t, err)
	require.Contains(t, []int{0, http.StatusSwitchingProtocols}, status)
	require.Contains(t, strings.ToLower(responseHeaders.Get("Sec-WebSocket-Extensions")), "permessage-deflate")
	defer func() { _ = connection.Close() }()

	messages := []string{
		strings.Repeat("candidate-compression-context-a", 512),
		strings.Repeat("candidate-compression-context-b", 512),
	}
	for _, message := range messages {
		require.NoError(t, connection.WriteJSON(context.Background(), map[string]any{
			"type":    "candidate.trace",
			"payload": message,
		}))
	}
	results := make([]string, 0, 2)
	for index := 0; index < 2; index++ {
		select {
		case raw := <-received:
			var decoded map[string]any
			require.NoError(t, json.Unmarshal([]byte(raw), &decoded))
			require.Equal(t, messages[index], decoded["payload"])
			results = append(results, "ok")
		case <-time.After(4 * time.Second):
			t.Fatal("等待压缩 WS 双消息超时")
		}
	}
	require.NoError(t, <-serverErrors)

	candidateTraceLogFact(t, "a05.ws-compression-context", "A05", "websocket_compression_context", map[string]any{
		"decompression_results": results,
	})
	candidateTraceLogFact(t, "a05.default-websocket", "A05", "transport_fallback", map[string]any{
		"transports": []string{"websocket"},
	})
}

type candidateTraceFailingWSConn struct{}

func (candidateTraceFailingWSConn) WriteJSON(context.Context, any) error { return nil }
func (candidateTraceFailingWSConn) ReadMessage(context.Context) ([]byte, error) {
	return nil, io.ErrUnexpectedEOF
}
func (candidateTraceFailingWSConn) Ping(context.Context) error { return nil }
func (candidateTraceFailingWSConn) Close() error               { return nil }

type candidateTraceFailingWSDialer struct {
	mu            sync.Mutex
	invocationIDs []string
}

func (d *candidateTraceFailingWSDialer) Dial(
	ctx context.Context,
	_ string,
	_ http.Header,
	_ string,
) (openAIWSClientConn, int, http.Header, error) {
	egressContext, exists := OfficialEgressContextFromContext(ctx)
	if !exists {
		return nil, 0, nil, io.ErrUnexpectedEOF
	}
	d.mu.Lock()
	d.invocationIDs = append(d.invocationIDs, egressContext.InvocationID())
	d.mu.Unlock()
	return candidateTraceFailingWSConn{}, http.StatusSwitchingProtocols, http.Header{}, nil
}

func (d *candidateTraceFailingWSDialer) snapshot() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.invocationIDs...)
}

func TestCandidateTraceCodex0145OAuthFallbackSameInvocation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/openai/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "candidate-third-party/1.0")

	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 1
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 1
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 1

	dialer := &candidateTraceFailingWSDialer{}
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(dialer)
	t.Cleanup(pool.Close)
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"id":"resp_candidate_http_fallback","output":[],"usage":{"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}`)),
	}}
	service := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
		openaiWSPool:     pool,
	}
	account := &Account{
		ID:          14507,
		Name:        "candidate-oauth-fallback",
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "candidate-test-token",
			"chatgpt_account_id": "candidate-test-account",
		},
	}
	service.openaiModelCapabilities.replaceFromManifest(
		account.ID,
		[]byte(`{"models":[{"slug":"gpt-5.4","use_responses_lite":false}]}`),
	)
	body := []byte(`{"model":"gpt-5.4","stream":false,"input":[{"type":"message","role":"user","content":"hello"}]}`)
	result, err := service.Forward(context.Background(), c, account, body)
	require.NoError(t, err)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	wsInvocationIDs := dialer.snapshot()
	require.Len(t, wsInvocationIDs, openAIWSReconnectRetryLimit+1)
	httpEgressContext, exists := OfficialEgressContextFromContext(upstream.lastReq.Context())
	require.True(t, exists)
	allInvocationIDs := append(wsInvocationIDs, httpEgressContext.InvocationID())
	for _, invocationID := range allInvocationIDs {
		require.NotEmpty(t, invocationID)
		require.Equal(t, allInvocationIDs[0], invocationID)
	}
	hashedInvocationIDs := make([]string, 0, len(allInvocationIDs))
	transports := make([]string, 0, len(allInvocationIDs))
	for index, invocationID := range allInvocationIDs {
		hashedInvocationIDs = append(hashedInvocationIDs, candidateTraceSHA256(invocationID))
		if index < len(wsInvocationIDs) {
			transports = append(transports, "websocket")
		} else {
			transports = append(transports, "http")
		}
	}
	candidateTraceLogFact(t, "a07.oauth-fallback", "A07", "transport_fallback", map[string]any{
		"invocation_ids":     hashedInvocationIDs,
		"retry_budget_state": "exhausted",
		"transports":         transports,
	})
}

func TestCandidateTraceCodex0145HeaderAssembly(t *testing.T) {
	stages := make([]string, 0, 6)
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	require.Equal(t, officialCodexVersion0145, profile.Version)
	stages = append(stages, "provider")

	headers := http.Header{
		"authorization":         {"Bearer initial"},
		"chatgpt-account-id":    {"candidate-account"},
		"originator":            {"codex_exec"},
		"session-id":            {uuid.NewString()},
		"thread-id":             {uuid.NewString()},
		"user-agent":            {"codex_exec/0.145.0"},
		"x-client-request-id":   {uuid.NewString()},
		"x-codex-turn-metadata": {`{"request_kind":"turn"}`},
		"x-codex-window-id":     {uuid.NewString()},
	}
	fields, err := officialCodexApplyHeaderContract(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		headers,
		map[string]bool{officialCodexConditionRemoteCompactionV2: true},
	)
	require.NoError(t, err)
	require.NotEmpty(t, fields)
	stages = append(stages, "endpoint_headers")

	body := []byte(`{"model":"gpt-5.6-sol","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{"effort":"high","context":"all_turns"},"store":false,"stream":true,"include":[]}`)
	orderedBody, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		body,
		nil,
	)
	require.NoError(t, err)
	require.True(t, json.Valid(orderedBody))
	stages = append(stages, "body")

	tlsProfile, err := officialCodexResolveEndpointTLSProfile(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)
	require.Len(t, tlsProfile.CipherSuites, 30)
	stages = append(stages, "configure")

	target, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		officialCodexEndpointURLInput{},
	)
	require.NoError(t, err)
	request, err := http.NewRequest(http.MethodPost, target.String(), bytes.NewReader(orderedBody))
	require.NoError(t, err)
	require.Equal(t, "/backend-api/codex/responses", request.URL.Path)
	stages = append(stages, "prepared_request")

	retryHeaders := make(http.Header, len(fields))
	for _, field := range fields {
		retryHeaders[field.Name] = []string{field.Value}
	}
	retryHeaders["authorization"] = []string{"Bearer refreshed"}
	retryFields, err := officialCodexApplyHeaderContract(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		retryHeaders,
		map[string]bool{officialCodexConditionRemoteCompactionV2: true},
	)
	require.NoError(t, err)
	refreshed := ""
	for _, field := range retryFields {
		if field.Name == "authorization" {
			refreshed = field.Value
		}
	}
	require.Equal(t, "Bearer refreshed", refreshed)
	stages = append(stages, "retry_auth")
	require.Equal(t, []string{
		"provider", "endpoint_headers", "body", "configure", "prepared_request", "retry_auth",
	}, stages)
	candidateTraceLogFact(t, "a03.header-assembly", "A03", "header_assembly", map[string]any{
		"retry_stages": []string{"retry_auth"},
		"stages":       stages,
	})
}

func TestCandidateTraceCodex0145AlphaSearchPhases(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{
		ID:          14509,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "candidate-alpha-token",
			"chatgpt_account_id": "candidate-alpha-account",
		},
	}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: &liveHTTPUpstreamStub{}}
	commands := []string{
		`{"search_query":[{"q":"candidate phase one"}]}`,
		`{"open":[{"ref_id":"candidate phase two"}]}`,
	}
	commandHashes := make([]string, 0, len(commands))
	for index, command := range commands {
		body := []byte(`{"id":"candidate-search-` + string(rune('a'+index)) + `","model":"gpt-5.6-sol","input":[],"commands":` + command + `,"settings":{},"max_output_tokens":2000}`)
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))
		request, buildErr := service.buildOpenAIAlphaSearchRequest(
			context.Background(), c, account, body, "candidate-alpha-token",
		)
		require.NoError(t, buildErr)
		requestBody, readErr := io.ReadAll(request.Body)
		require.NoError(t, readErr)
		actualCommands := gjson.GetBytes(requestBody, "commands").Raw
		require.JSONEq(t, command, actualCommands)
		commandHashes = append(commandHashes, candidateTraceSHA256(actualCommands))
	}
	require.Len(t, commandHashes, 2)
	require.NotEqual(t, commandHashes[0], commandHashes[1])
	candidateTraceLogFact(t, "a09.alpha-search-phases", "A09", "alpha_search_flow", map[string]any{
		"command_sha256s": commandHashes,
	})
}

func TestCandidateTraceCodex0145RealtimeChain(t *testing.T) {
	upstream := &liveHTTPUpstreamStub{}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          14511,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "candidate-live-token",
			"chatgpt_account_id": "candidate-live-account",
		},
	}
	created, err := service.createUpstreamLiveCall(
		context.Background(),
		account,
		&LiveCallRequest{SDP: "v=offer\r\n", Session: json.RawMessage(`{"model":"gpt-live"}`)},
		`{"v":1,"s":0,"t":"candidate"}`,
		uuid.NewString(),
	)
	require.NoError(t, err)
	require.NotEmpty(t, created.CallID)
	require.NotNil(t, upstream.request)

	sidebandEndpoint, err := resolveCodexEndpoint(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
	)
	require.NoError(t, err)
	sidebandTarget, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
		officialCodexEndpointURLInput{QueryValues: map[string]string{"call_id": created.CallID}},
	)
	require.NoError(t, err)
	sidebandCallID := sidebandTarget.Query().Get("call_id")
	require.Equal(t, created.CallID, sidebandCallID)
	require.Equal(t, "quicksilver=v1", upstream.request.Header.Get("openai-alpha"))
	terminalEvent, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
		[]byte(`{"type":"session.ended"}`),
		nil,
	)
	require.NoError(t, err)
	require.Equal(t, "session.ended", gjson.GetBytes(terminalEvent, "type").String())

	candidateTraceLogFact(t, "a11.realtime-chain", "A11", "realtime_chain", map[string]any{
		"first_hop_call_id_sha256": candidateTraceSHA256(created.CallID),
		"first_hop_method":         upstream.request.Method,
		"first_hop_path":           upstream.request.URL.Path,
		"openai_alpha":             upstream.request.Header.Get("openai-alpha"),
		"sideband_call_id_sha256":  candidateTraceSHA256(sidebandCallID),
		"sideband_host":            sidebandTarget.Hostname(),
		"sideband_method":          sidebandEndpoint.Method,
		"sideband_path":            sidebandTarget.Path,
		"sideband_upgrade":         sidebandEndpoint.Upgrade,
		"terminal_event":           gjson.GetBytes(terminalEvent, "type").String(),
	})
}

func TestCandidateTraceCodex0145CompactionDecisions(t *testing.T) {
	legacyEndpoint, err := resolveCodexEndpoint(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
	)
	require.NoError(t, err)
	require.Equal(t, "/backend-api/codex/responses/compact", legacyEndpoint.Path)
	legacyState := "disabled_explicitly"

	triggerBody := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"compaction_trigger"}]}`)
	require.True(t, HasCompactionTriggerInInput(triggerBody))
	reasons := []string{"user_requested", "context_limit", "model_downshift", "comp_hash_changed"}
	for index, reason := range reasons {
		c := newOfficialOpenAIHTTPTestContext(triggerBody, "/v1/responses")
		c.Request.Header.Set("x-codex-turn-metadata", `{"compaction":{"reason":"`+reason+`"}}`)
		metadata, ok := resolveDerivedOfficialOpenAICompactionMetadata(c, triggerBody, officialClientProfileModeActive)
		require.True(t, ok)
		require.Equal(t, reason, metadata.Reason)
		require.Equal(t, "responses_compaction_v2", metadata.Implementation)
		data := map[string]any{
			"implementation":       metadata.Implementation,
			"reason":               metadata.Reason,
			"remote_compaction_v2": legacyState,
		}
		if index == 0 {
			data["provider_scope"] = "remote_default"
		}
		candidateTraceLogFact(t, "a10.compaction-reason-"+reason, "A10", "compaction_decision", data)
	}

	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	profileTokenBudgetEndpoints := 0
	for _, endpoint := range profile.Endpoints {
		if strings.Contains(strings.ToLower(endpoint.ID), "token_budget") {
			profileTokenBudgetEndpoints++
		}
	}
	require.Equal(t, 0, profileTokenBudgetEndpoints)
	candidateTraceLogFact(t, "a10.token-budget-no-egress", "A10", "compaction_decision", map[string]any{
		"implementation":       "token_budget",
		"remote_compaction_v2": legacyState,
		"summary_egress_count": profileTokenBudgetEndpoints,
	})

	beforeCount := len(gjson.GetBytes(triggerBody, `input.#(type=="compaction_trigger")`).Array())
	_, ok := resolveDerivedOfficialOpenAICompactionMetadata(
		newOfficialOpenAIHTTPTestContext(triggerBody, "/v1/responses"),
		triggerBody,
		officialClientProfileModeActive,
	)
	require.True(t, ok)
	afterCount := len(gjson.GetBytes(triggerBody, `input.#(type=="compaction_trigger")`).Array())
	require.Equal(t, beforeCount, afterCount)
	candidateTraceLogFact(t, "a10.existing-trigger", "A10", "compaction_decision", map[string]any{
		"appended_trigger_count": afterCount - beforeCount,
		"input_trigger_state":    "already_present",
		"remote_compaction_v2":   legacyState,
	})
	candidateTraceLogFact(t, "a09.explicit-legacy", "A09", "compaction_decision", map[string]any{
		"implementation":       "legacy",
		"legacy_endpoint":      legacyEndpoint.Path,
		"remote_compaction_v2": legacyState,
	})
}
