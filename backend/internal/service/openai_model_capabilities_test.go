package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestOpenAIModelCapabilitiesUseExactManifestBit(t *testing.T) {
	service := &OpenAIGatewayService{}
	account := newOfficialOpenAIHTTPTestAccount(94)
	service.openaiModelCapabilities.replaceFromManifest(94, []byte(`{
		"models":[
			{"slug":"gpt-5.6-luna","use_responses_lite":true},
			{"slug":"gpt-5.4","use_responses_lite":false}
		]
	}`))

	require.True(t, service.resolveOpenAIResponsesLiteCapability(
		account,
		[]byte(`{"model":"gpt-5.6-luna"}`),
	))
	require.False(t, service.resolveOpenAIResponsesLiteCapability(
		account,
		[]byte(`{"model":"gpt-5.4"}`),
	))
	require.False(t, service.resolveOpenAIResponsesLiteCapability(
		account,
		[]byte(`{"model":"unknown-model"}`),
	))
}

func TestOpenAIModelCapabilitiesRecordParallelToolBit(t *testing.T) {
	service := &OpenAIGatewayService{}
	service.openaiModelCapabilities.replaceFromManifest(94, []byte(`{
		"models":[
			{"slug":"parallel-model","use_responses_lite":false,"supports_parallel_tool_calls":true},
			{"slug":"serial-model","use_responses_lite":false,"supports_parallel_tool_calls":false}
		]
	}`))

	parallel, known := service.openaiModelCapabilities.modelCapabilities(94, "parallel-model")
	require.True(t, known)
	require.True(t, parallel.SupportsParallelToolCalls)
	require.False(t, parallel.UseResponsesLite)
	serial, known := service.openaiModelCapabilities.modelCapabilities(94, "serial-model")
	require.True(t, known)
	require.False(t, serial.SupportsParallelToolCalls)
}

func TestOpenAIModelCapabilitiesBundledColdStartAndManifestOverride(t *testing.T) {
	service := &OpenAIGatewayService{}

	enabled, known := service.openaiModelCapabilities.responsesLite(94, "gpt-5.6-sol")
	require.True(t, known)
	require.True(t, enabled)
	enabled, known = service.openaiModelCapabilities.responsesLite(94, "gpt-5.4")
	require.True(t, known)
	require.False(t, enabled)
	enabled, known = service.openaiModelCapabilities.responsesLite(94, "gpt-5.5")
	require.True(t, known)
	require.False(t, enabled)
	_, known = service.openaiModelCapabilities.responsesLite(94, "gpt-5.1")
	require.False(t, known, "内置快照不得臆测官方清单没有返回的历史模型")

	service.openaiModelCapabilities.replaceFromManifest(
		94,
		[]byte(`{"models":[{"slug":"gpt-5.6-sol","use_responses_lite":false}]}`),
	)
	enabled, known = service.openaiModelCapabilities.responsesLite(94, "gpt-5.6-sol")
	require.True(t, known)
	require.False(t, enabled)
	enabled, known = service.openaiModelCapabilities.responsesLite(94, "gpt-5.4")
	require.True(t, known, "账号清单裁剪不得抹掉当前客户端已知的 bundled 能力")
	require.False(t, enabled)
}

func TestOpenAIModelCapabilitiesFailureBackoffPreservesStaleSnapshot(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	now := time.Now()
	snapshot.replaceFromManifest(
		94,
		[]byte(`{"models":[{"slug":"account-only-model","use_responses_lite":true}]}`),
	)
	snapshot.mu.Lock()
	stale := snapshot.accounts[94]
	stale.expiresAt = now.Add(-time.Millisecond)
	snapshot.accounts[94] = stale
	snapshot.mu.Unlock()
	snapshot.markLoadFailure(94, now)

	enabled, known, shouldRefresh := snapshot.responsesLiteState(94, "account-only-model", now)
	require.True(t, enabled)
	require.True(t, known)
	require.False(t, shouldRefresh, "失败退避期内不得重复请求上游 manifest")

	_, _, shouldRefresh = snapshot.responsesLiteState(
		94,
		"account-only-model",
		now.Add(openAIModelCapabilityFailureTTL+time.Millisecond),
	)
	require.True(t, shouldRefresh)
}

func TestEnsureOpenAIModelCapabilityFailsOpenAndCachesFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"detail":"temporary"}`, http.StatusInternalServerError)
	}))
	defer server.Close()

	original := chatgptCodexModelsURL
	chatgptCodexModelsURL = server.URL
	defer func() { chatgptCodexModelsURL = original }()

	service := &OpenAIGatewayService{}
	account := newCodexModelsTestAccount()
	body := []byte(`{"model":"unknown-future-model"}`)
	require.NoError(t, service.ensureOpenAIModelCapability(context.Background(), account, body))

	_, _, shouldRefresh := service.openaiModelCapabilities.responsesLiteState(
		account.ID,
		"unknown-future-model",
		time.Now(),
	)
	require.False(t, shouldRefresh, "加载失败后必须进入有界负缓存")
}

func TestOpenAIModelCapabilityKeyNormalizesReasoningSuffix(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)
	require.Equal(t, "gpt-5.4", openAIModelCapabilityKey(account, []byte(`{"model":"gpt-5.4-high"}`)))
	// gpt-5.1 是别名表中的精确条目，实际出站会被改写成 gpt-5.4，
	// 能力查表键必须落到同一个 slug，否则入站与出站判定分裂。
	require.Equal(t, "gpt-5.4", openAIModelCapabilityKey(account, []byte(`{"model":"gpt-5.1"}`)))
}

// TestOpenAIModelCapabilityKeyMatchesUpstreamAliasResolution 锁定 Lite 判定不分裂：
// 入站别名与出站真实 slug 必须解析到同一个能力键。回退到「只做拼写归一」会让
// gpt-5.6 在入站判为非 Lite（工具被不可逆摊平）、出站按 gpt-5.6-sol 判为 Lite
// 再包进 additional_tools，产出官方客户端不会发送的形态。
func TestOpenAIModelCapabilityKeyMatchesUpstreamAliasResolution(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)

	alias := openAIModelCapabilityKey(account, []byte(`{"model":"gpt-5.6"}`))
	canonical := openAIModelCapabilityKey(account, []byte(`{"model":"gpt-5.6-sol"}`))
	require.Equal(t, "gpt-5.6-sol", alias, "别名必须解析到 manifest 真实 slug")
	require.Equal(t, alias, canonical, "别名与真实 slug 必须得到同一个能力键")

	withSuffix := openAIModelCapabilityKey(account, []byte(`{"model":"gpt-5.6-high"}`))
	require.Equal(t, alias, withSuffix, "reasoning 后缀不得改变能力键")

	// 完全陌生的模型不得冒名命中已知能力位。
	require.Equal(
		t,
		"totally-unknown-model",
		openAIModelCapabilityKey(account, []byte(`{"model":"totally-unknown-model"}`)),
	)
}

func TestOpenAIOfficialEgressNonLiteUsesOfficialTopLevelContract(t *testing.T) {
	payload := []byte(`{
		"model":"gpt-5.4",
		"instructions":"keep-top-level",
		"input":"hello",
		"tools":[{"type":"function","name":"lookup","parameters":{"type":"object"}}],
		"parallel_tool_calls":true,
		"store":true,
		"stream":false,
		"tool_choice":"required",
		"include":["message.output_text.logprobs"],
		"text":{"verbosity":"high"},
		"reasoning":{"effort":"medium","context":"none"},
		"max_output_tokens":123
	}`)
	contract, err := captureGeneratedOfficialOpenAIHTTPBodyContract(payload)
	require.NoError(t, err)
	identity := deriveOfficialOpenAIHTTPIdentity(nil, newOfficialOpenAIHTTPTestAccount(94), payload, contract)

	finalized, _, err := finalizeOfficialOpenAIHTTPBody(
		payload,
		contract,
		identity,
		officialOpenAIReasoningDefaults{},
		false,
		false,
		false,
		true,
	)
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
	require.Equal(t, "medium", gjson.GetBytes(finalized, "reasoning.effort").String())
	require.False(t, gjson.GetBytes(finalized, "reasoning.context").Exists())
	require.False(t, gjson.GetBytes(finalized, "max_output_tokens").Exists())
}

func TestOfficialOpenAIHTTPTurnStartUsesCurrentStableTime(t *testing.T) {
	gin.SetMode(gin.TestMode)
	firstContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	firstContext.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	secondContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	secondContext.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	body := []byte(`{"model":"gpt-5.4","input":"turn-start-cross-request-94001"}`)
	contract, err := captureGeneratedOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	account := newOfficialOpenAIHTTPTestAccount(94001)

	before := time.Now().UnixMilli()
	first := deriveOfficialOpenAIHTTPIdentity(firstContext, account, body, contract)
	second := deriveOfficialOpenAIHTTPIdentity(secondContext, account, body, contract)
	after := time.Now().UnixMilli()
	firstStartedAt := gjson.Get(first.turnMetadata, "turn_started_at_unix_ms").Int()
	secondStartedAt := gjson.Get(second.turnMetadata, "turn_started_at_unix_ms").Int()

	require.GreaterOrEqual(t, firstStartedAt, before)
	require.LessOrEqual(t, firstStartedAt, after)
	require.Equal(t, first.turnID, second.turnID)
	require.Equal(t, firstStartedAt, secondStartedAt, "同一 turn 的独立请求必须复用开始时间")
	require.Less(t, strings.Index(first.turnMetadata, `"installation_id"`), strings.Index(first.turnMetadata, `"session_id"`))
	require.Less(t, strings.Index(first.turnMetadata, `"session_id"`), strings.Index(first.turnMetadata, `"turn_started_at_unix_ms"`))

	installationID, err := uuid.Parse(first.installationID)
	require.NoError(t, err)
	require.Equal(t, uuid.Version(4), installationID.Version(), "installation-id 必须保持官方 v4")
	sessionID, err := uuid.Parse(first.sessionID)
	require.NoError(t, err)
	require.Equal(t, uuid.Version(7), sessionID.Version(), "session-id 必须使用带时间的 v7")
	turnID, err := uuid.Parse(first.turnID)
	require.NoError(t, err)
	require.Equal(t, uuid.Version(7), turnID.Version(), "turn-id 必须使用带时间的 v7")
}
