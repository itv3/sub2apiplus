package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// Responses 主链（HTTP）受影响规则的晋升后门禁。
//
// 请求一律从官方 Codex 客户端形态的入站请求出发，经生产 OpenAIGatewayService.Forward
// 发往按结构事实定位的目标槽位，断言本地 TLS 终端观测到的真实 wire。
// ============================================================================

// codexGateZstdMagic 是 zstd 帧的魔数，用来确认请求体在 wire 上确实被压缩。
var codexGateZstdMagic = []byte{0x28, 0xB5, 0x2F, 0xFD}

// codexGateNonLiteModel 是模型清单里 use_responses_lite=false 的模型，用来排除
// “Lite 模型强制压缩”这一独立条件，单独观察压缩开关。
const codexGateNonLiteModel = "gpt-5.5"

// codexGateUseRecorderManifest 让测试账号使用与生产同源的完整模型清单
// （含 Lite 与非 Lite 模型）。
func codexGateUseRecorderManifest(service *OpenAIGatewayService, account *Account) {
	service.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(codexModelsRecorderManifest))
}

// SPEC-BODY-002（condition_change）：Responses 尊重压缩开关（legacy compact 已删除）。
//
// 检查项与网关观测：
//   - responses-zstd：压缩开启的官方客户端请求（入站 content-encoding: zstd，非 Lite
//     模型，排除 Lite 这一独立压缩条件）→ wire 上 content-encoding 恰为 [zstd]，请求体
//     是 zstd 帧，解压后是本次 JSON；
//   - responses-plain：压缩关闭（入站明文、非 Lite 模型）→ wire 上完全没有
//     content-encoding，请求体为明文 JSON。
//
// 条件变化的来源是 legacy compact 删除：目标画像里只剩 responses_http 声明请求压缩，
// 压缩开关只作用于普通 Responses。
func TestCodexResponsesCompressionSwitchReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)
	profile := codexGateExecutableProfile(t, mode)
	require.True(t, codexGateLegacyCompactRemoved(profile), "条件变化的来源：目标画像不再有 legacy compact 端点")
	require.True(t, profile.Features().EnableRequestCompression, "目标画像必须开启请求压缩能力")
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == officialCodexEndpointResponsesHTTP {
			require.Equal(t, profilecontract.CompressionZstdWhenFeatureEnabled, endpoint.Compression)
			slot, ok := codexGateHeaderSlot(endpoint, "content-encoding")
			require.True(t, ok, "responses_http 必须声明 content-encoding 条件槽位")
			require.Equal(t, profilecontract.ConditionRequestCompressionEnabled, slot.Condition)
			require.Equal(t, "zstd", slot.Value)
			continue
		}
		require.NotEqual(t, profilecontract.CompressionZstdWhenFeatureEnabled, endpoint.Compression,
			"%s 不得声明请求压缩：压缩开关只作用于普通 Responses", endpoint.ID)
	}

	server := startCodexGateWireServer(t, nil)
	nonLiteBody := mutateOfficialOpenAIHTTPTestBody(t, newOfficialOpenAIHTTPTestBody(t, true, false, false),
		func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["model"] = codexGateNonLiteModel
		})

	// responses-zstd：官方客户端以 zstd 发来的请求（入站快照冻结的压缩事实）。
	zstdIngress := newOfficialOpenAIHTTPTestContext(nonLiteBody, "/v1/responses")
	zstdIngress.Request.Header.Del("x-openai-internal-codex-responses-lite")
	zstdIngress.Request.Header.Set("Content-Encoding", "zstd")
	compressed := codexGateForwardResponsesWire(t, mode, server, zstdIngress, nonLiteBody, codexGateUseRecorderManifest)
	require.Equal(t, []string{"zstd"}, compressed.values("content-encoding"),
		"压缩开启时 Responses 必须以 content-encoding: zstd 出站")
	require.True(t, bytes.HasPrefix(compressed.rawBody, codexGateZstdMagic), "压缩开启时 wire 请求体必须是 zstd 帧")
	require.Equal(t, codexGateNonLiteModel, gjson.GetBytes(compressed.body, "model").String(),
		"zstd 帧解压后必须是本次请求的 JSON")

	// responses-plain：压缩关闭（入站明文、非 Lite 模型）。
	plainIngress := newOfficialOpenAIHTTPTestContext(nonLiteBody, "/v1/responses")
	plainIngress.Request.Header.Del("x-openai-internal-codex-responses-lite")
	plain := codexGateForwardResponsesWire(t, mode, server, plainIngress, nonLiteBody, codexGateUseRecorderManifest)
	require.False(t, plain.has("content-encoding"), "压缩关闭时 Responses 不得发送 content-encoding")
	require.NotContains(t, plain.lowerHeaderNames(), "content-encoding")
	require.False(t, bytes.HasPrefix(plain.rawBody, codexGateZstdMagic), "压缩关闭时请求体不得被压缩")
	require.True(t, gjson.ValidBytes(plain.rawBody), "压缩关闭时 wire 请求体必须是明文 JSON")
	require.Equal(t, codexGateNonLiteModel, gjson.GetBytes(plain.rawBody, "model").String())
}

// codexGateModelsPath 是 Codex 模型清单端点路径。
const codexGateModelsPath = "/backend-api/codex/models"

// codexGateResponsesDefaultAllowed/Required 是 Responses 默认（http_default 变体）
// 线序的允许全集与必需项，与批准断言 responses-order 相同。
var (
	codexGateResponsesDefaultAllowed = []string{
		"version", "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
		"x-openai-internal-codex-responses-lite", "x-codex-routing-hint", "x-client-request-id",
		"session-id", "thread-id", "accept", "content-encoding", "content-type", "authorization",
		"chatgpt-account-id", "originator", "user-agent", "cookie", "host", "content-length",
	}
	codexGateResponsesDefaultRequired = []string{
		"version", "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
		"x-openai-internal-codex-responses-lite", "x-codex-routing-hint", "x-client-request-id",
		"session-id", "thread-id", "accept", "content-encoding", "content-type", "authorization",
		"chatgpt-account-id", "originator", "user-agent", "host", "content-length",
	}
)

// SPEC-H1-004（change）：用户 header 使用逐端点冻结的 HeaderMap 最终线序。
//
// 检查项与网关观测：
//   - models-order：models 默认请求（无 Cookie 变体）的真实 wire 线序恰为 version,
//     authorization, chatgpt-account-id, originator, user-agent, accept, host（目标画像把
//     accept 移到 originator、user-agent 之后）；
//   - responses-order：Responses 默认压缩请求的 wire 线序是批准允许全集的有序子集且含
//     全部必需项；Cookie jar 建立后 cookie 落在 user-agent 与 host 之间；
//   - responses-lite-header-value：Lite 请求的 x-openai-internal-codex-responses-lite 恰为 true；
//   - responses-routing-hint-value：Lite 请求的 x-codex-routing-hint 携带本轮 Lite 模型。
//     官方样本的模型取决于采集轮次，网关的等价事实是取值恰为 "model=" 加出站请求体的
//     model（Lite 轨模型）。
//
// 画像补丁中的 CookieJar 节同属本规则：目标画像的 jar 名单新增 __oailb，只有 __oailb
// 的 jar 也必须让 Responses 在固定槽位携带它（读取名单跟随调用冻结的目标画像）。
func TestCodexHeaderMapFinalOrderReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t,
		"CookieJar 节允许 __oailb 且 models 的 accept 位于 originator、user-agent 之后",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().CookieJar
			return section != nil && slices.Contains(section.AllowedNames, "__oailb") &&
				codexGateAcceptAfterIdentity(profile, officialCodexEndpointModels)
		})
	server := startCodexGateWireServer(t, nil)

	// models-order：无 Cookie 变体（models 画像本身不声明 cookie 槽位）。
	modelsGate := newCodexGateService(t, mode, server)
	require.NoError(t, modelsGate.models(), "models 经生产入口发往 %s 槽位失败", mode)
	models := server.requestsForPath(codexGateModelsPath)
	require.Len(t, models, 1)
	require.Equal(t,
		[]string{"version", "authorization", "chatgpt-account-id", "originator", "user-agent", "accept", "host"},
		models[0].lowerHeaderNames(), "models 默认请求的 wire 线序必须与官方原始字节一致")

	// responses-order 与两个 Lite 取值：默认 Lite 请求，jar 为空。
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	responses := codexGateForwardResponsesWire(t, mode, server,
		newOfficialOpenAIHTTPTestContext(body, "/v1/responses"), body, nil)
	codexGateRequireOrderedSubset(t, responses.lowerHeaderNames(),
		codexGateResponsesDefaultAllowed, codexGateResponsesDefaultRequired,
		"Responses 默认压缩请求的 wire 线序")
	require.Equal(t, []string{"true"}, responses.values("x-openai-internal-codex-responses-lite"),
		"Lite 请求的 x-openai-internal-codex-responses-lite 必须恰为 true")
	liteModel := gjson.GetBytes(responses.body, "model").String()
	require.NotEmpty(t, liteModel)
	require.Equal(t, []string{"model=" + liteModel}, responses.values("x-codex-routing-hint"),
		"Lite 请求的 routing hint 必须携带本轮 Lite 模型")

	// CookieJar 节：jar 只有 __oailb（旧名单没有它）时，cookie 仍在固定槽位出站。
	cookieBody := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	withCookie := codexGateForwardResponsesWire(t, mode, server,
		newOfficialOpenAIHTTPTestContext(cookieBody, "/v1/responses"), cookieBody,
		func(service *OpenAIGatewayService, account *Account) {
			service.openAICookieJar(account).SetCookies(
				&url.URL{Scheme: "https", Host: "chatgpt.com", Path: "/"},
				[]*http.Cookie{{Name: "__oailb", Value: "promotion-gate-lb", Path: "/"}},
			)
		})
	require.Equal(t, []string{"__oailb=promotion-gate-lb"}, withCookie.values("cookie"),
		"目标画像的 jar 名单包含 __oailb，Responses 必须携带它")
	codexGateRequireOrderedSubset(t, withCookie.lowerHeaderNames(),
		codexGateResponsesDefaultAllowed, append(append([]string(nil), codexGateResponsesDefaultRequired...), "cookie"),
		"jar 建立后的 Responses wire 线序")
	names := withCookie.lowerHeaderNames()
	cookieIndex := slices.Index(names, "cookie")
	require.Equal(t, "user-agent", names[cookieIndex-1], "cookie 必须紧跟 user-agent")
	require.Equal(t, "host", names[cookieIndex+1], "cookie 之后紧接 host")
}

// ----------------------------------------------------------------------------
// 官方客户端入站形态
// ----------------------------------------------------------------------------

// codexGateRootIngress 构造官方 Codex 客户端根会话的 Responses 入站（Lite 模型、流式）。
func codexGateRootIngress(
	t *testing.T,
	mutate func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any),
) ([]byte, *gin.Context) {
	t.Helper()
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	if mutate != nil {
		body = mutateOfficialOpenAIHTTPTestBody(t, body, mutate)
	}
	return body, newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
}

// codexGateGuardianReviewIngress 构造官方 guardian 同步审阅请求的入站：guardian 子代理
// 身份（x-openai-subagent: guardian、父线程、turn metadata 的 subagent_kind）加审阅标记
// x-codex-guardian: reviewer。二者同时成立时网关才把它识别为审阅请求。
func codexGateGuardianReviewIngress(
	t *testing.T,
	mutate func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any),
) ([]byte, *gin.Context) {
	t.Helper()
	body := newOfficialOpenAIGuardianHTTPBody(t)
	if mutate != nil {
		body = mutateOfficialOpenAIHTTPTestBody(t, body, mutate)
	}
	c := newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")
	c.Request.Header.Set("x-codex-guardian", "reviewer")
	return body, c
}

// codexGateResponseCreateFrame 把 Responses 请求体改写成首个 response.create 语义帧。
func codexGateResponseCreateFrame(t *testing.T, body []byte) []byte {
	t.Helper()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	payload["type"] = "response.create"
	raw, err := json.Marshal(payload)
	require.NoError(t, err)
	return raw
}

// codexGateDialResponsesWebSocket 经生产 WS 入口装配在 mode 槽位建立一次握手，返回
// 终端观测到的握手请求。
func codexGateDialResponsesWebSocket(
	t *testing.T,
	gate *codexGateService,
	server *codexGateWireServer,
	body []byte,
	c *gin.Context,
) codexGateWireRequest {
	t.Helper()
	session, _, err := gate.dialResponsesWebSocket(t, c, codexGateResponseCreateFrame(t, body), gate.webSocketDialer(server))
	require.NoError(t, err, "Responses WS 握手经生产入口装配失败（握手错误 %v）", server.handshakeErrors())
	require.NoError(t, session.Close())
	var handshakes []codexGateWireRequest
	for _, request := range server.wireRequests() {
		if request.webSocket {
			handshakes = append(handshakes, request)
		}
	}
	require.NotEmpty(t, handshakes, "终端没有收到 WS 握手")
	return handshakes[len(handshakes)-1]
}

// SPEC-HDR-010（add）：guardian 审阅请求携带 x-codex-guardian: reviewer，不带 routing hint；
// 普通 Responses 请求不携带 x-codex-guardian。
//
// 检查项与网关观测：
//   - guardian-reviewer-value：官方 guardian 审阅请求（入站同时具备 guardian 子代理身份与
//     reviewer 标记）在 HTTP 与 WS 握手的真实 wire 上都恰好携带 x-codex-guardian: reviewer；
//   - http-guardian-absent：普通 HTTP Responses 的 wire 上没有 x-codex-guardian；
//   - ws-guardian-absent：普通 WS 握手的 wire 上没有 x-codex-guardian。
//
// 同属本规则的画像补丁一并重放：HTTP 槽位 66 位于 routing hint 与 x-client-request-id
// 之间（wire 上紧邻 x-client-request-id 之前）；WS 插入序紧跟
// x-responsesapi-include-timing-metrics，经 swap_remove 后在 wire 上紧跟 originator；
// 审阅请求不带 routing hint，也不带 service_tier（入站携带时按画像省略），client_metadata
// 不写 guardian_credits_requested。
func TestCodexGuardianReviewHeaderReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "Responses 声明 guardian 审阅条件头 x-codex-guardian: reviewer",
		func(profile profilecontract.ExecutableProfile) bool {
			for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
				endpoint, ok := codexGateEndpoint(profile, endpointID)
				slot, has := codexGateHeaderSlot(endpoint, "x-codex-guardian")
				if !ok || !has || slot.Condition != profilecontract.ConditionGuardianReviewRequest || slot.Value != "reviewer" {
					return false
				}
			}
			return true
		})
	profile := codexGateExecutableProfile(t, mode)
	responsesHTTP := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesHTTP)
	require.True(t, codexGateSlotPrecedes(responsesHTTP, "x-codex-routing-hint", "x-codex-guardian") &&
		codexGateSlotPrecedes(responsesHTTP, "x-codex-guardian", "x-client-request-id"),
		"HTTP 的 x-codex-guardian 槽位必须位于 routing hint 与 x-client-request-id 之间")
	responsesWS := codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesWS)
	insertion := responsesWS.HeaderMapInsertionOrder
	guardianIndex := slices.Index(insertion, "x-codex-guardian")
	require.Positive(t, guardianIndex)
	require.Equal(t, "x-responsesapi-include-timing-metrics", insertion[guardianIndex-1],
		"WS 插入序中 x-codex-guardian 必须紧跟 x-responsesapi-include-timing-metrics")
	for _, endpoint := range []profilecontract.ExecutableEndpointProfile{responsesHTTP, responsesWS} {
		slot, ok := codexGateHeaderSlot(endpoint, "x-codex-routing-hint")
		require.True(t, ok)
		require.Equal(t, profilecontract.ConditionNotGuardianReviewRequest, slot.Condition,
			"%s 的 routing hint 只属于非审阅请求", endpoint.ID)
		for _, field := range endpoint.Body.Fields {
			if field.Name == "service_tier" {
				require.Equal(t, profilecontract.ConditionNotGuardianReviewRequest, field.Condition,
					"%s 的 service_tier 只属于非审阅请求", endpoint.ID)
			}
		}
	}

	server := startCodexGateWireServer(t, nil)
	withServiceTier := func(payload map[string]any, _ map[string]any, _ map[string]any) {
		payload["service_tier"] = "priority"
	}

	// http-guardian-absent：普通请求。
	normalBody, normalIngress := codexGateRootIngress(t, withServiceTier)
	normal := codexGateForwardResponsesWire(t, mode, server, normalIngress, normalBody, nil)
	require.False(t, normal.has("x-codex-guardian"), "普通 HTTP Responses 不得发送 x-codex-guardian")
	require.NotEmpty(t, normal.values("x-codex-routing-hint"), "普通请求保留 routing hint")
	require.Equal(t, "priority", gjson.GetBytes(normal.body, "service_tier").String(), "普通请求保留 service_tier")

	// guardian-reviewer-value（HTTP）。
	reviewBody, reviewIngress := codexGateGuardianReviewIngress(t, withServiceTier)
	review := codexGateForwardResponsesWire(t, mode, server, reviewIngress, reviewBody, nil)
	require.Equal(t, []string{"reviewer"}, review.values("x-codex-guardian"),
		"guardian 审阅请求必须发送 x-codex-guardian: reviewer")
	require.False(t, review.has("x-codex-routing-hint"), "guardian 审阅请求不得发送 routing hint")
	names := review.lowerHeaderNames()
	guardianAt := slices.Index(names, "x-codex-guardian")
	require.Equal(t, "x-client-request-id", names[guardianAt+1],
		"x-codex-guardian 在 wire 上必须紧邻 x-client-request-id 之前（槽位 66）")
	require.False(t, gjson.GetBytes(review.body, "service_tier").Exists(), "审阅请求按画像省略 service_tier")
	require.False(t, gjson.GetBytes(review.body, "client_metadata.guardian_credits_requested").Exists(),
		"审阅请求不写 guardian_credits_requested")

	// WS：同一入口装配下的普通握手与审阅握手。
	gate := newCodexGateService(t, mode, server)
	wsNormalBody, wsNormalIngress := codexGateRootIngress(t, nil)
	wsNormal := codexGateDialResponsesWebSocket(t, gate, server, wsNormalBody, wsNormalIngress)
	require.False(t, wsNormal.has("x-codex-guardian"), "普通 WS 握手不得发送 x-codex-guardian")
	wsReviewBody, wsReviewIngress := codexGateGuardianReviewIngress(t, nil)
	wsReview := codexGateDialResponsesWebSocket(t, gate, server, wsReviewBody, wsReviewIngress)
	require.Equal(t, []string{"reviewer"}, wsReview.values("x-codex-guardian"),
		"guardian 审阅的 WS 握手必须发送 x-codex-guardian: reviewer")
	require.False(t, wsReview.has("x-codex-routing-hint"), "guardian 审阅的 WS 握手不得发送 routing hint")
	remaining := codexGateRemainingHandshakeNames(wsReview)
	originatorAt := slices.Index(remaining, "originator")
	require.GreaterOrEqual(t, originatorAt, 0)
	require.Equal(t, "x-codex-guardian", remaining[originatorAt+1],
		"按插入序经 swap_remove 后，x-codex-guardian 在 wire 上紧跟 originator")
}

// SPEC-BODY-008（add）：普通会话每个 Responses 请求的 client_metadata 携带
// guardian_credits_requested="true" 与常量 mcp_attribution（guardian 审阅请求同样写入后者）。
//
// 检查项与网关观测：
//   - http-guardian-credits / http-mcp-attribution：普通 HTTP Responses 的 wire 请求体中
//     client_metadata.guardian_credits_requested 恰为字符串 "true"，mcp_attribution 恰为
//     字符串 {"status":"none"}；
//   - ws-guardian-credits / ws-mcp-attribution：WS response.create 帧（真实 wire 帧）的
//     client_metadata 同上。
//
// 画像节的 KeyConditions 一并重放：guardian 审阅请求只写 mcp_attribution（always），
// 不写 guardian_credits_requested（not_guardian_review_request）。
func TestCodexClientMetadataConstantsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "ClientMetadata 节声明 guardian_credits_requested 与 mcp_attribution 常量",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().ClientMetadata
			return section != nil &&
				section.Constants["guardian_credits_requested"] == "true" &&
				section.Constants["mcp_attribution"] == `{"status":"none"}`
		})
	section := codexGateExecutableProfile(t, mode).Optional().ClientMetadata
	require.NotNil(t, section, "目标画像必须声明 ClientMetadata 节")
	require.Equal(t, profilecontract.ConditionNotGuardianReviewRequest, section.ConditionFor("guardian_credits_requested"))
	require.Equal(t, profilecontract.ConditionAlways, section.ConditionFor("mcp_attribution"))

	server := startCodexGateWireServer(t, nil)
	requireConstants := func(metadata gjson.Result, where string) {
		t.Helper()
		credits := metadata.Get("guardian_credits_requested")
		require.Equal(t, gjson.String, credits.Type, "%s 的 guardian_credits_requested 必须是字符串", where)
		require.Equal(t, "true", credits.String(), "%s 的 guardian_credits_requested 必须为 \"true\"", where)
		attribution := metadata.Get("mcp_attribution")
		require.Equal(t, gjson.String, attribution.Type, "%s 的 mcp_attribution 必须是字符串", where)
		require.Equal(t, `{"status":"none"}`, attribution.String(), "%s 的 mcp_attribution 必须为常量", where)
	}

	// HTTP 普通请求。
	httpBody, httpIngress := codexGateRootIngress(t, nil)
	httpRequest := codexGateForwardResponsesWire(t, mode, server, httpIngress, httpBody, nil)
	requireConstants(gjson.GetBytes(httpRequest.body, "client_metadata"), "HTTP Responses")

	// HTTP guardian 审阅请求：KeyConditions。
	reviewBody, reviewIngress := codexGateGuardianReviewIngress(t, nil)
	review := codexGateForwardResponsesWire(t, mode, server, reviewIngress, reviewBody, nil)
	reviewMetadata := gjson.GetBytes(review.body, "client_metadata")
	require.Equal(t, `{"status":"none"}`, reviewMetadata.Get("mcp_attribution").String(),
		"guardian 审阅请求同样写入 mcp_attribution")
	require.False(t, reviewMetadata.Get("guardian_credits_requested").Exists(),
		"guardian 审阅请求不写 guardian_credits_requested")

	// WS response.create 帧。
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err, "第三方入站经 WS 出站失败")
	frames := codexGateResponseCreateFrames(handshake)
	require.NotEmpty(t, frames, "WS 连接上必须发出 response.create 帧")
	for index, frame := range frames {
		requireConstants(gjson.GetBytes(frame, "client_metadata"), fmt.Sprintf("第 %d 个 WS response.create", index+1))
	}
}

// codexGateEndpointSlotOrder 返回端点画像按 Slot/Sequence 排序后的 header 名，即该端点
// HeaderMap 的冻结最终线序。
func codexGateEndpointSlotOrder(endpoint profilecontract.ExecutableEndpointProfile) []string {
	slots := append([]profilecontract.HeaderSlotProfile(nil), endpoint.Headers...)
	slices.SortStableFunc(slots, func(left, right profilecontract.HeaderSlotProfile) int {
		if left.Slot != right.Slot {
			return left.Slot - right.Slot
		}
		return left.Sequence - right.Sequence
	})
	names := make([]string, 0, len(slots))
	for _, slot := range slots {
		names = append(names, strings.ToLower(slot.Name))
	}
	return names
}

// SPEC-HDR-001（condition_change）：provider、端点、configure、prepared 与 retry auth 的
// 组装顺序；guardian 审阅请求不生成 routing hint。
//
// 检查项与网关观测：
//   - assembly-stages：官方客户端的内部 trace 在网关没有同名事件，对应的是 Compiler 的
//     固定组装流水线，每一段都有 wire 上可验证的产物——provider（release 节点给出的
//     version、user-agent 版本与目标发布一致）、endpoint_headers（wire 线序是目标端点
//     槽位序的有序子集，全部小写且没有画像外 header）、body（content-type、content-length
//     与实际字节一致，压缩与 content-encoding 一致）、configure/prepared_request（host、
//     content-length 由传输层最后写出）；
//   - retry-auth-last：同一调用的重试（同账号、同一 gin 上下文、同一 invocation）由
//     Executor 重新签发 attempt，认证作为 attempt 材料最后注入——两次 wire 只有
//     authorization 随刷新后的凭据变化，其余 header 名序与取值完全相同，attempt 序号
//     1→2 且原因为 retry；
//   - guardian-review-no-routing-hint：guardian 审阅请求在 HTTP 与 WS 握手上都不生成
//     x-codex-routing-hint（目标画像把 routing hint 条件改为 not_guardian_review_request）。
func TestCodexHeaderAssemblyReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "Responses 的 routing hint 条件为 not_guardian_review_request",
		func(profile profilecontract.ExecutableProfile) bool {
			for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
				endpoint, ok := codexGateEndpoint(profile, endpointID)
				slot, has := codexGateHeaderSlot(endpoint, "x-codex-routing-hint")
				if !ok || !has || slot.Condition != profilecontract.ConditionNotGuardianReviewRequest {
					return false
				}
			}
			return true
		})
	profile := codexGateExecutableProfile(t, mode)
	version, err := officialCodexVersionForMode(mode)
	require.NoError(t, err)

	var responsesCalls atomic.Int32
	server := startCodexGateWireServer(t, func(request codexGateWireRequest) codexGateWireResponse {
		if request.path == codexGateResponsesPath && responsesCalls.Add(1) == 1 {
			// 首次返回可同账号重试的瞬时 429（无配额窗口头），完整读完后连接保持可复用。
			return codexGateWireResponse{
				status: http.StatusTooManyRequests,
				header: http.Header{"Content-Type": []string{"application/json"}},
				body:   []byte(`{"error":{"type":"rate_limit_exceeded","message":"transient"}}`),
			}
		}
		return codexGateDefaultResponse(request)
	})
	gate := newCodexGateService(t, mode, server)
	body, ingress := codexGateRootIngress(t, nil)

	_, err = gate.forward(ingress, body)
	var failover *UpstreamFailoverError
	require.ErrorAs(t, err, &failover, "首次瞬时 429 必须以可重试的 failover 错误返回")
	require.True(t, failover.RetryableOnSameAccount, "瞬时 429 必须允许同账号重试（同一调用）")
	// 两次尝试之间凭据被刷新：重试必须使用新凭据，且只有认证随之变化。
	gate.account.Credentials["access_token"] = "promotion-gate-rotated-token"
	_, err = gate.forward(ingress, body)
	require.NoError(t, err, "同一调用的重试必须成功（上游错误 %v）", gate.upstream.failureList())

	requests := server.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, requests, 2)
	first, retry := requests[0], requests[1]

	// provider：版本与身份来自目标发布。
	require.Equal(t, []string{version}, first.values("version"))
	require.Contains(t, first.header.Get("User-Agent"), "/"+version+" ")
	// endpoint_headers：线序是端点槽位序的有序子集（小写、无画像外 header）。
	slotOrder := codexGateEndpointSlotOrder(codexGateMustEndpoint(t, profile, officialCodexEndpointResponsesHTTP))
	codexGateRequireOrderedSubset(t, first.lowerHeaderNames(), slotOrder, nil, "Responses wire 线序对照端点槽位序")
	require.Equal(t, first.lowerHeaderNames(), first.headerNames, "wire header 名必须全部小写")
	// body：长度与压缩和实际字节一致。
	require.Equal(t, []string{strconv.Itoa(len(first.rawBody))}, first.values("content-length"))
	require.Equal(t, []string{"application/json"}, first.values("content-type"))
	require.Equal(t, first.has("content-encoding"), bytes.HasPrefix(first.rawBody, codexGateZstdMagic),
		"content-encoding 必须与请求体是否 zstd 压缩一致")
	// configure / prepared_request：host、content-length 由传输层最后写出。
	names := first.lowerHeaderNames()
	require.Equal(t, []string{"host", "content-length"}, names[len(names)-2:])

	// retry-auth-last。
	require.Equal(t, []string{"Bearer oauth-test-token"}, first.values("authorization"))
	require.Equal(t, []string{"Bearer promotion-gate-rotated-token"}, retry.values("authorization"),
		"重试必须以本次 attempt 的认证材料最后注入")
	require.Equal(t, first.lowerHeaderNames(), retry.lowerHeaderNames(), "重试只重做认证，header 名序不变")
	for _, name := range first.lowerHeaderNames() {
		if name == "authorization" {
			continue
		}
		require.Equal(t, first.values(name), retry.values(name), "重试不得改变 %s", name)
	}
	require.Equal(t, first.body, retry.body, "重试的请求体必须与首次一致")
	attempts := gate.upstream.attemptIdentities()
	require.Len(t, attempts, 2)
	require.Equal(t, attempts[0].InvocationID, attempts[1].InvocationID, "重试属于同一上层调用")
	require.Equal(t, attempts[0].BundleDigest, attempts[1].BundleDigest)
	require.Equal(t, []uint32{1, 2}, []uint32{attempts[0].AttemptOrdinal, attempts[1].AttemptOrdinal})
	require.Equal(t, string(officialegress.AttemptReasonRetry), attempts[1].AttemptReason)

	// guardian-review-no-routing-hint。
	require.NotEmpty(t, first.values("x-codex-routing-hint"), "普通请求生成 routing hint")
	reviewBody, reviewIngress := codexGateGuardianReviewIngress(t, nil)
	review := codexGateForwardResponsesWire(t, mode, server, reviewIngress, reviewBody, nil)
	require.False(t, review.has("x-codex-routing-hint"), "guardian 审阅请求不得生成 routing hint")
	wsGate := newCodexGateService(t, mode, server)
	wsReviewBody, wsReviewIngress := codexGateGuardianReviewIngress(t, nil)
	wsReview := codexGateDialResponsesWebSocket(t, wsGate, server, wsReviewBody, wsReviewIngress)
	require.False(t, wsReview.has("x-codex-routing-hint"), "guardian 审阅的 WS 握手不得生成 routing hint")
}

// codexGateCompactionIngress 构造官方客户端的 Remote Compaction V2 请求：普通 Responses
// 的 input 末尾追加 compaction_trigger，turn metadata 携带压缩原因。
func codexGateCompactionIngress(t *testing.T, reason string) ([]byte, *gin.Context) {
	t.Helper()
	return codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, turnMetadata map[string]any) {
		input, _ := payload["input"].([]any)
		payload["input"] = append(input, map[string]any{"type": "compaction_trigger"})
		turnMetadata["compaction"] = map[string]any{"reason": reason}
	})
}

// codexGateCompactionTriggers 统计 wire 请求体 input 中 compaction_trigger 的个数与末项类型。
func codexGateCompactionTriggers(body []byte) (int, string) {
	items := gjson.GetBytes(body, "input").Array()
	count := 0
	for _, item := range items {
		if item.Get("type").String() == "compaction_trigger" {
			count++
		}
	}
	last := ""
	if len(items) > 0 {
		last = items[len(items)-1].Get("type").String()
	}
	return count, last
}

// SPEC-EP-021（condition_change）：默认 manual/auto compaction 使用 Responses V2；legacy
// 分支删除，x-codex-beta-features 恒含 remote_compaction_v2。
//
// 检查项与网关观测：
//   - v2-compaction-trigger：manual（user_requested）与 auto（context_limit）两种压缩请求在
//     wire 上都发往普通 /responses，input 保留且只保留一个 compaction_trigger（位于末尾）；
//   - v2-no-legacy-call：整个过程终端没有收到任何 /responses/compact 请求；
//   - beta-header-always-http：即使下游没有发送 beta 头（例如官方客户端显式禁用该特性），
//     HTTP Responses 的 wire 仍恰好携带 x-codex-beta-features: remote_compaction_v2——目标
//     画像把该头的条件从 remote_compaction_v2 改为 always；
//   - beta-header-always-ws：第三方入站（从不发送 beta 头）经上游 WS 出站时，握手同样
//     恰好携带该头。
func TestCodexRemoteCompactionV2ReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "Responses 的 x-codex-beta-features 条件为 always",
		func(profile profilecontract.ExecutableProfile) bool {
			for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
				endpoint, ok := codexGateEndpoint(profile, endpointID)
				slot, has := codexGateHeaderSlot(endpoint, "x-codex-beta-features")
				if !ok || !has || slot.Condition != profilecontract.ConditionAlways || slot.Value != "remote_compaction_v2" {
					return false
				}
			}
			return true
		})
	profile := codexGateExecutableProfile(t, mode)
	require.True(t, profile.Features().RemoteCompactionV2, "目标画像默认启用 Remote Compaction V2")
	for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
		slot, ok := codexGateHeaderSlot(codexGateMustEndpoint(t, profile, endpointID), "x-codex-beta-features")
		require.True(t, ok)
		require.Equal(t, profilecontract.ConditionAlways, slot.Condition,
			"%s 的 beta 头条件必须为 always（不再随特性开关省略）", endpointID)
		require.Equal(t, "remote_compaction_v2", slot.Value)
	}
	require.True(t, codexGateLegacyCompactRemoved(profile), "legacy 分支随 compact 端点删除")
	require.True(t, OfficialCodexRemoteCompactionV2Default(mode), "第三方入口按目标画像默认走 V2")

	server := startCodexGateWireServer(t, nil)
	for _, reason := range []string{"user_requested", "context_limit"} {
		body, ingress := codexGateCompactionIngress(t, reason)
		ingress.Request.Header.Del("x-codex-beta-features")
		request := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
		require.Equal(t, codexGateResponsesPath, request.path, "%s 压缩必须发往普通 /responses", reason)
		count, last := codexGateCompactionTriggers(request.body)
		require.Equal(t, 1, count, "%s 压缩的 input 必须恰好保留一个 compaction_trigger", reason)
		require.Equal(t, "compaction_trigger", last, "%s 压缩的 compaction_trigger 位于 input 末尾", reason)
		require.Equal(t, []string{"remote_compaction_v2"}, request.values("x-codex-beta-features"),
			"下游未发送 beta 头时 HTTP Responses 仍必须恒发 remote_compaction_v2")
	}

	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err)
	require.Equal(t, []string{"remote_compaction_v2"}, handshake.values("x-codex-beta-features"),
		"WS 握手必须恒发 x-codex-beta-features: remote_compaction_v2")
	require.Empty(t, server.requestsForPath(codexGateLegacyCompactPath), "默认 V2 不得请求 /responses/compact")
}

// codexGateCompactionPlan 是四种压缩原因在网关派生元数据中的期望（trigger 与 phase）。
var codexGateCompactionPlan = []struct {
	reason  string
	trigger string
	phase   string
}{
	{reason: "user_requested", trigger: "manual", phase: "standalone_turn"},
	{reason: "context_limit", trigger: "auto", phase: "mid_turn"},
	{reason: "model_downshift", trigger: "auto", phase: "pre_turn"},
	{reason: "comp_hash_changed", trigger: "auto", phase: "pre_turn"},
}

// SPEC-EP-023（change）：压缩选择、四种 reason、TokenBudget 零出站与去重语义。
//
// 批准断言的记录来自官方客户端内部的 compaction_decision trace，网关对应的是它在
// 出站 turn metadata 中派生的 compaction 元数据与 wire 形态：
//   - four-reasons：四种 reason 各发一次压缩请求，wire turn metadata 的 compaction.reason
//     分别如实出现（request_kind=compaction，trigger/phase 与官方映射一致）；
//   - remote-v2-default：compaction.implementation 恒为 responses_compaction_v2；
//   - token-budget-no-egress：网关没有 TokenBudget 分支，也从不自行发出摘要请求——每次
//     压缩调用恰好一条 Responses 出站，目标画像不存在摘要/预算端点，压缩实现闭集只含
//     responses 与 responses_compaction_v2；
//   - existing-trigger-no-double：input 已含 compaction_trigger 时 wire 上仍只有一个；
//   - no-legacy-implementation：实现闭集校验拒绝 responses_compact，显式 legacy compact
//     请求在端点解析处失败关闭（与 legacy compact 删除门禁同一真实路径）。
func TestCodexCompactionSelectionReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "TurnMetadata 节的压缩实现闭集含 responses_compaction_v2",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().TurnMetadata
			return section != nil && slices.Contains(section.CompactionImplementations, "responses_compaction_v2")
		})
	profile := codexGateExecutableProfile(t, mode)
	section := profile.Optional().TurnMetadata
	require.NotNil(t, section, "目标画像必须声明 TurnMetadata 节")
	require.Equal(t, []string{"responses", "responses_compaction_v2"}, section.CompactionImplementations,
		"压缩实现闭集不得包含 legacy 或 TokenBudget 实现")
	require.Equal(t, []string{"mid_turn", "post_turn", "pre_turn", "standalone_turn"}, section.CompactionPhases)
	for _, endpoint := range profile.Endpoints() {
		require.NotContains(t, strings.ToLower(endpoint.ID), "token_budget", "目标画像不得有 TokenBudget 出站端点")
		require.NotContains(t, strings.ToLower(endpoint.ID), "summary", "目标画像不得有摘要出站端点")
	}

	server := startCodexGateWireServer(t, nil)
	seenReasons := map[string]bool{}
	for _, plan := range codexGateCompactionPlan {
		before := len(server.wireRequests())
		body, ingress := codexGateCompactionIngress(t, plan.reason)
		request := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
		require.Len(t, server.wireRequests(), before+1, "%s 压缩调用必须恰好一条出站，不得另发摘要请求", plan.reason)
		metadata := gjson.Parse(request.header.Get("X-Codex-Turn-Metadata"))
		require.Equal(t, "compaction", metadata.Get("request_kind").String())
		compaction := metadata.Get("compaction")
		require.Equal(t, plan.reason, compaction.Get("reason").String(), "wire 必须如实携带压缩原因")
		require.Equal(t, "responses_compaction_v2", compaction.Get("implementation").String(),
			"远程默认实现必须是 responses_compaction_v2")
		require.Equal(t, plan.trigger, compaction.Get("trigger").String())
		require.Equal(t, plan.phase, compaction.Get("phase").String())
		count, _ := codexGateCompactionTriggers(request.body)
		require.Equal(t, 1, count, "%s：已有 compaction_trigger 时不得再次追加", plan.reason)
		seenReasons[plan.reason] = true
	}
	require.Len(t, seenReasons, 4, "四种压缩原因必须各有独立出站")

	require.Error(t, validateOfficialCodexTurnMetadataCompaction(
		officialOpenAICompactionMetadata{Implementation: "responses_compact", Phase: "standalone_turn"}, section,
	), "legacy 实现必须被目标画像的实现闭集拒绝")
	require.NoError(t, validateOfficialCodexTurnMetadataCompaction(
		officialOpenAICompactionMetadata{Implementation: "responses_compaction_v2", Phase: "pre_turn"}, section,
	))
	codexGateRequireLegacyCompactForwardFailsClosed(t, mode)
	require.Empty(t, server.requestsForPath(codexGateLegacyCompactPath))
}

// SPEC-HDR-009（add）：x-openai-account-routing-override 只在 accounts/check 给出非默认路由
// override 时出现；默认路由下 Responses 请求不携带。
//
// 检查项与网关观测：
//   - http-override-absent：默认路由（尚未发现或发现为默认）下，HTTP Responses 的 wire
//     没有该头，即使下游请求带着它（网关不透传，Compiler 头闭集也不含它）；
//   - ws-override-absent：WS 握手同样没有该头。
//
// 目标画像的 WorkspaceRouting 节把该头登记为“记录用、首期不发出”，任何端点都没有它的
// 槽位；非默认路由按 NonDefaultAction=fail_closed 失败关闭——受路由端点根本不出站，
// 因而 wire 上永远不会出现带 override 的请求。
func TestCodexAccountRoutingOverrideReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "WorkspaceRouting 节登记 override 头",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().WorkspaceRouting
			return section != nil && section.OverrideHeader == "x-openai-account-routing-override"
		})
	profile := codexGateExecutableProfile(t, mode)
	section := profile.Optional().WorkspaceRouting
	require.NotNil(t, section, "目标画像必须声明 WorkspaceRouting 节")
	require.Equal(t, "fail_closed", section.NonDefaultAction)
	require.Equal(t, []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS}, section.RoutedEndpointIDs)
	for _, endpoint := range profile.Endpoints() {
		_, declared := codexGateHeaderSlot(endpoint, section.OverrideHeader)
		require.False(t, declared, "%s 不得声明 override 头槽位（首期不发出）", endpoint.ID)
	}
	resetOfficialCodexWorkspaceRoutingResults(t)

	server := startCodexGateWireServer(t, nil)
	body, ingress := codexGateRootIngress(t, nil)
	ingress.Request.Header.Set(section.OverrideHeader, "us")
	request := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	require.False(t, request.has(section.OverrideHeader), "默认路由下 HTTP Responses 不得发送 override 头")

	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil),
		http.Header{section.OverrideHeader: []string{"us"}})
	require.NoError(t, err)
	require.False(t, handshake.has(section.OverrideHeader), "默认路由下 WS 握手不得发送 override 头")

	// 非默认路由：受路由端点失败关闭，不会带着 override 出站。
	decision := decideOfficialCodexWorkspaceRouting(section, "chatgpt-test-account",
		[]byte(`{"accounts":[{"id":"chatgpt-test-account","workspace_backend_origin":"https://chatgpt.com","account_routing_override":"us"}]}`))
	require.False(t, decision.Default)
	recordOfficialCodexWorkspaceRouting(decision)
	before := len(server.wireRequests())
	nonDefaultBody, nonDefaultIngress := codexGateRootIngress(t, nil)
	_, _, err = codexGateForwardResponses(t, mode, server, nonDefaultIngress, nonDefaultBody, nil)
	require.ErrorIs(t, err, ErrOfficialCodexWorkspaceRoutingNonDefault, "非默认路由必须失败关闭")
	require.Len(t, server.wireRequests(), before, "非默认路由下不得产生任何出站")
}

// SPEC-HDR-007（change）：普通 Responses 使用连字符会话头（session-id 取 prompt_cache_key），
// realtime 使用独立 x-session-id。
//
// 检查项与网关观测：
//   - responses-session-id / responses-thread-id：普通 HTTP Responses 与 WS 握手都携带
//     session-id 与 thread-id；
//   - legacy-names-absent / conversation-id-absent：下游即使带着 session_id、conversation_id
//     旧名头，wire 上也没有 session_id、conversation_id、conversation-id；
//   - realtime-x-session：realtime 第一跳（生产 createUpstreamLiveCall）使用 x-session-id，
//     不使用 session-id。
//
// 目标画像把 Responses 的 session-id 来源改为 prompt_cache_key，其可观测差异在临时 fork：
// fork 的 session-id 等于请求体 prompt_cache_key（源会话键），不等于本会话的
// client_metadata.session_id；thread-id 仍是本会话。根会话三者相同；guardian 子代理的
// session-id 仍是本会话真实会话 ID，不取带 guardian: 前缀的 prompt cache 键。
func TestCodexSessionHeadersReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "Responses 的 session-id 取值来源为 prompt_cache_key",
		func(profile profilecontract.ExecutableProfile) bool {
			for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
				endpoint, ok := codexGateEndpoint(profile, endpointID)
				slot, has := codexGateHeaderSlot(endpoint, "session-id")
				if !ok || !has || slot.Source != profilecontract.SourcePromptCacheKey {
					return false
				}
			}
			return true
		})
	server := startCodexGateWireServer(t, codexGateRealtimeResponse)
	requireNoLegacyNames := func(request codexGateWireRequest, where string) {
		t.Helper()
		for _, name := range []string{"session_id", "conversation_id", "conversation-id"} {
			require.NotContains(t, request.lowerHeaderNames(), name, "%s 不得发送旧名 %s", where, name)
		}
	}

	// 根会话：下游带着 session_id / conversation_id 旧名头。
	rootBody, rootIngress := codexGateRootIngress(t, nil)
	root := codexGateForwardResponsesWire(t, mode, server, rootIngress, rootBody, nil)
	sessionID := root.header.Get("Session-Id")
	require.NotEmpty(t, sessionID, "普通 Responses 必须包含 session-id")
	require.NotEmpty(t, root.header.Get("Thread-Id"), "普通 Responses 必须包含 thread-id")
	require.Equal(t, gjson.GetBytes(root.body, "prompt_cache_key").String(), sessionID,
		"根会话的 session-id 等于 prompt_cache_key")
	requireNoLegacyNames(root, "HTTP Responses")

	// 临时 fork：prompt_cache_key 是源会话，client_metadata.session_id 是 fork 本会话。
	forkBody := promptCacheTestBody(t, promptCacheTestSourceSession, promptCacheTestForkSession)
	fork := codexGateForwardResponsesWire(t, mode, server, promptCacheTestContext(t, forkBody), forkBody, nil)
	forkSession := fork.header.Get("Session-Id")
	require.Equal(t, gjson.GetBytes(fork.body, "prompt_cache_key").String(), forkSession,
		"fork 的 session-id 必须取 prompt_cache_key（源会话键）")
	require.NotEqual(t, gjson.GetBytes(fork.body, "client_metadata.session_id").String(), forkSession,
		"fork 的 session-id 不得取本会话 ID")
	require.Equal(t, gjson.GetBytes(fork.body, "client_metadata.thread_id").String(), fork.header.Get("Thread-Id"),
		"fork 的 thread-id 仍是本会话线程")

	// guardian 子代理：session-id 取本会话真实 ID，而不是 guardian: 前缀的 prompt cache 键。
	reviewBody, reviewIngress := codexGateGuardianReviewIngress(t, nil)
	review := codexGateForwardResponsesWire(t, mode, server, reviewIngress, reviewBody, nil)
	require.True(t, strings.HasPrefix(gjson.GetBytes(review.body, "prompt_cache_key").String(), "guardian:"))
	require.Equal(t, gjson.GetBytes(review.body, "client_metadata.session_id").String(), review.header.Get("Session-Id"),
		"子代理的 session-id 取本会话真实会话 ID")

	// WS 握手：session-id 与首帧 prompt_cache_key 一致，thread-id 存在，没有旧名。
	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil),
		http.Header{"Session_id": []string{"legacy-session"}, "Conversation_id": []string{"legacy-conversation"}})
	require.NoError(t, err)
	frames := codexGateResponseCreateFrames(handshake)
	require.NotEmpty(t, frames)
	require.NotEmpty(t, handshake.header.Get("Thread-Id"), "WS 握手必须包含 thread-id")
	require.Equal(t, gjson.GetBytes(frames[0], "prompt_cache_key").String(), handshake.header.Get("Session-Id"),
		"WS 握手的 session-id 等于 prompt_cache_key")
	requireNoLegacyNames(handshake, "WS 握手")

	// realtime 第一跳：x-session-id。
	_, _, err = gate.realtimeCall(t, mode)
	require.NoError(t, err, "realtime 第一跳经生产入口失败")
	calls := server.requestsForPath(codexGateRealtimeCallsPath)
	require.Len(t, calls, 1)
	require.NotEmpty(t, calls[0].header.Get("X-Session-Id"), "realtime 第一跳必须使用 x-session-id")
	require.NotContains(t, calls[0].lowerHeaderNames(), "session-id", "realtime 第一跳不使用 Responses 的 session-id")
	requireNoLegacyNames(calls[0], "realtime 第一跳")
}

// codexGateTurnMetadataRequiredKeys 是网关可信生成、出站必须携带的 13 个 turn metadata 键
// （含目标画像新增的 analytics_enabled、model、reasoning_effort、turn_trigger）。画像 21 键
// 中另 8 键由官方客户端产生，网关没有可信取值、不仿真，不在必需项内。
var codexGateTurnMetadataRequiredKeys = []string{
	"analytics_enabled", "installation_id", "model", "reasoning_effort", "request_kind", "sandbox",
	"session_id", "thread_id", "thread_source", "turn_id", "turn_started_at_unix_ms", "turn_trigger", "window_id",
}

// codexGateRequireTurnMetadata 按批准断言检查一份出站 turn metadata：键（排序后）是画像
// Keys 的有序子集且含必需 13 键，analytics_enabled 为 JSON true。
func codexGateRequireTurnMetadata(t *testing.T, raw string, allowed []string, where string) {
	t.Helper()
	require.True(t, gjson.Valid(raw), "%s 的 turn metadata 不是合法 JSON：%s", where, raw)
	parsed := gjson.Parse(raw)
	var keys []string
	parsed.ForEach(func(key, _ gjson.Result) bool {
		keys = append(keys, key.String())
		return true
	})
	slices.Sort(keys)
	codexGateRequireOrderedSubset(t, keys, allowed, codexGateTurnMetadataRequiredKeys, where+" 的 turn metadata 键")
	analytics := parsed.Get("analytics_enabled")
	require.Equal(t, gjson.True, analytics.Type, "%s 默认配置下 analytics_enabled 必须为 JSON true", where)
}

// SPEC-BODY-009（add）：x-codex-turn-metadata 的键为画像 TurnMetadata.Keys（21 键）的有序
// 子集，必含新增的 analytics_enabled、model、reasoning_effort、turn_trigger 及网关既有键；
// 默认配置 analytics_enabled=true。
//
// 检查项与网关观测：
//   - turn-metadata-keys：HTTP Responses 的 x-codex-turn-metadata 头（与请求体
//     client_metadata 中的同名值逐字节一致）、WS response.create 帧 client_metadata 中的
//     turn metadata，键排序后都是画像 21 键的有序子集并含网关可信生成的 13 键；
//   - turn-metadata-analytics-default：analytics_enabled 取画像固定值 true（JSON 布尔）。
//
// 画像数据层同时锁定：TurnMetadata.Keys 恰为 21 键，AnalyticsEnabled 为 true。官方另 8 键
// 网关没有可信取值，属既有仿真缺口，不在本门禁的必需项内。
func TestCodexTurnMetadataKeysReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "TurnMetadata 节声明 analytics_enabled 等新键",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().TurnMetadata
			return section != nil && slices.Contains(section.Keys, "analytics_enabled") &&
				slices.Contains(section.Keys, "turn_trigger")
		})
	section := codexGateExecutableProfile(t, mode).Optional().TurnMetadata
	require.NotNil(t, section, "目标画像必须声明 TurnMetadata 节")
	allowed := []string{
		"agent_name", "analytics_enabled", "auto_review_enabled", "context_window_id", "installation_id", "model",
		"node_repl_auto_review_required", "node_repl_disabled", "reasoning_effort", "request_kind", "root_turn_id",
		"sandbox", "sandbox_mode", "session_id", "thread_id", "thread_source", "turn_id", "turn_started_at_unix_ms",
		"turn_trigger", "window_id", "window_number",
	}
	require.Equal(t, allowed, section.Keys, "画像 TurnMetadata.Keys 必须恰为批准的 21 键")
	require.True(t, section.AnalyticsEnabled, "画像默认 analytics_enabled 必须为 true")

	server := startCodexGateWireServer(t, nil)
	body, ingress := codexGateRootIngress(t, nil)
	request := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	header := request.header.Get("X-Codex-Turn-Metadata")
	codexGateRequireTurnMetadata(t, header, allowed, "HTTP Responses")
	require.Equal(t, header, gjson.GetBytes(request.body, "client_metadata.x-codex-turn-metadata").String(),
		"请求体 client_metadata 中的 turn metadata 必须与头逐字节一致")

	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil), nil)
	require.NoError(t, err)
	frames := codexGateResponseCreateFrames(handshake)
	require.NotEmpty(t, frames)
	for index, frame := range frames {
		codexGateRequireTurnMetadata(t, gjson.GetBytes(frame, "client_metadata.x-codex-turn-metadata").String(),
			allowed, fmt.Sprintf("第 %d 个 WS response.create", index+1))
	}
}

// codexGateConnectionReuseEndpoints 是连接复用规则覆盖的端点（官方指南：models、responses、
// images、alpha-search；不含 backend-client 与 WS prewarm）。legacy compact 随端点删除退出范围。
var codexGateConnectionReuseEndpoints = []string{
	officialCodexEndpointResponsesHTTP, officialCodexEndpointModels,
	officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits, officialCodexEndpointAlphaSearch,
}

// SPEC-CONN-001（condition_change）：跨调用不复用，同调用 keepalive 重试复用，断连重试新建连接。
//
// 检查项与网关观测（本地终端给每条 TCP 连接编号，连接号即 connection_id）：
//   - cross-call-distinct：同一服务、同一账号的两次上层 Responses 调用落在不同连接上，
//     且 Executor 编译出的连接池摘要不同（按 invocation 隔离）；
//   - keepalive-reuse：同一调用的重试（瞬时 429 后同账号重试，响应体已读完、连接保持）
//     复用同一条连接，重试是该连接上的第 2 个请求，连接池摘要不变；
//   - disconnect-new-connection：同一调用中上游断开连接后重试，连接池摘要不变但建立
//     新连接。
//
// 画像事实：范围内端点的生命周期都是 per_upper_api_call，编译为 invocation 作用域且重试
// 复用客户端；传输声明不跨调用复用。条件变化来自 legacy compact 删除（它退出规则范围）。
// 生产 HTTPUpstream 按 TLS 画像身份（名称携带连接池摘要）缓存客户端，捕获上游按同一组
// 键缓存，这里一并锁定“画像名称 = 固定前缀 + 连接池摘要”这一隔离约定。
func TestCodexConnectionReuseReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)
	profile := codexGateExecutableProfile(t, mode)
	require.True(t, codexGateLegacyCompactRemoved(profile), "条件变化的来源：legacy compact 已退出规则范围")
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
	require.NoError(t, err)
	for _, endpointID := range codexGateConnectionReuseEndpoints {
		endpoint := codexGateMustEndpoint(t, profile, endpointID)
		require.Equal(t, profilecontract.LifecyclePerUpperApiCall, endpoint.ResourceLifecycle.Lifecycle, endpointID)
		require.Equal(t, profilecontract.ResourceScopeInvocation, endpoint.ResourceLifecycle.Scope, endpointID)
		require.True(t, endpoint.ResourceLifecycle.RetryReusesClient, "%s 的重试复用客户端", endpointID)
		for _, transport := range release.Profile().Transports() {
			if transport.ID == endpoint.TransportID {
				require.False(t, transport.CrossCallConnectionReuse, "%s 的传输不得跨调用复用连接", endpointID)
				require.True(t, transport.RetryReusesClient, endpointID)
			}
		}
	}

	var responsesCalls atomic.Int32
	server := startCodexGateWireServer(t, func(request codexGateWireRequest) codexGateWireResponse {
		if request.path != codexGateResponsesPath {
			return codexGateDefaultResponse(request)
		}
		switch responsesCalls.Add(1) {
		case 3:
			return codexGateWireResponse{
				status: http.StatusTooManyRequests,
				header: http.Header{"Content-Type": []string{"application/json"}},
				body:   []byte(`{"error":{"type":"rate_limit_exceeded","message":"transient"}}`),
			}
		case 5:
			return codexGateWireResponse{dropWithoutResponse: true}
		default:
			return codexGateDefaultResponse(request)
		}
	})
	gate := newCodexGateService(t, mode, server)
	call := func() {
		body, ingress := codexGateRootIngress(t, nil)
		_, err := gate.forward(ingress, body)
		require.NoError(t, err, "上层调用失败（上游错误 %v）", gate.upstream.failureList())
	}
	retry := func(expectFailure bool) {
		body, ingress := codexGateRootIngress(t, nil)
		_, err := gate.forward(ingress, body)
		require.Equal(t, expectFailure, err != nil, "首次尝试的结果不符合预期：%v", err)
		_, err = gate.forward(ingress, body)
		require.NoError(t, err, "同一调用的重试必须成功")
	}

	call()      // 请求 1：调用 A
	call()      // 请求 2：调用 B
	retry(true) // 请求 3（429）与 4：调用 C 的 keepalive 重试
	retry(true) // 请求 5（断连）与 6：调用 D 的断连重试
	requests := server.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, requests, 6)
	attempts := gate.upstream.attemptIdentities()
	require.Len(t, attempts, 6)
	profileNames := gate.upstream.profileNames()
	for index, attempt := range attempts {
		require.Equal(t, "Official Codex compiled "+attempt.ConnectionPoolDigest, profileNames[index],
			"TLS 画像名称必须携带本次 attempt 的连接池摘要（生产连接池隔离的依据）")
	}

	// cross-call-distinct。
	require.NotEqual(t, requests[0].connection, requests[1].connection, "不同上层调用不得复用连接")
	require.NotEqual(t, attempts[0].ConnectionPoolDigest, attempts[1].ConnectionPoolDigest, "不同上层调用的连接池摘要不同")
	require.NotEqual(t, attempts[0].InvocationID, attempts[1].InvocationID)

	// keepalive-reuse。
	require.Equal(t, attempts[2].InvocationID, attempts[3].InvocationID, "429 后的重试属于同一调用")
	require.Equal(t, uint32(2), attempts[3].AttemptOrdinal)
	require.Equal(t, attempts[2].ConnectionPoolDigest, attempts[3].ConnectionPoolDigest)
	require.Equal(t, requests[2].connection, requests[3].connection, "同调用 keepalive 重试必须复用连接")
	require.Equal(t, 2, requests[3].sequence, "重试是该连接上的第 2 个请求")
	require.NotEqual(t, requests[1].connection, requests[2].connection, "调用 C 不复用调用 B 的连接")

	// disconnect-new-connection。
	require.Equal(t, attempts[4].InvocationID, attempts[5].InvocationID, "断连后的重试属于同一调用")
	require.Equal(t, attempts[4].ConnectionPoolDigest, attempts[5].ConnectionPoolDigest)
	require.NotEqual(t, requests[4].connection, requests[5].connection, "同调用断连重试必须新建连接")
	require.Equal(t, 1, requests[5].sequence, "断连后的重试是新连接上的首个请求")
}
