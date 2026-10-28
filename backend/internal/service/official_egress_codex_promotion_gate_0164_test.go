package service

import (
	"fmt"
	"regexp"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// Codex 0.164.0 的晋升后判别式门禁（门禁映射为本轮每条受影响规则各绑一个用例）。
//
// 本轮受影响规则：SPEC-EP-002、SPEC-EP-022、SPEC-BODY-003、SPEC-BODY-006、SPEC-WS-005。三类判别式结构事实：
//   - 画像 oauth_refresh 端点的封闭 Body 声明 refresh_reason（EP-002）；
//   - 画像 images_generations／images_edits 声明 x-codex-image-turn-id 与 x-codex-turn-metadata 两个头槽位（EP-022）；
//   - 画像 BaseInstructions 节声明 IDScheme=uuidv7（BODY-003／BODY-006／WS-005：Lite 窗口开头的 additional_tools 与基础指令
//     消息 id 改为记录时随机的 <前缀>_<UUIDv7>，预热帧 input 为空，前缀由第一条业务帧带出）。
//
// 用例一律用 codexGateDiscriminatingReleaseMode 按画像结构事实定位目标槽位——候选态目标装在 previous，晋升后在 active，
// 两个阶段都成立，不写死槽位名；另一槽位（0.163.0）作对照，证明行为由画像驱动。0.163 起已继承的结构事实另有按继承特征
// 重放的门禁（official_egress_codex_promotion_gate_target_*_test.go）。
// ============================================================================

// codexGateUUIDv7Pattern 匹配规范 UUID 且版本位是 7、变体位是 RFC 4122。
var codexGateUUIDv7Pattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// codexGateUUIDv5Pattern 匹配规范 UUID 且版本位是 5（0.163 画像的基础指令消息 id 方案）。
var codexGateUUIDv5Pattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// codexGateDeclaresUUIDv7BaseInstructions 是 BODY-003／BODY-006／WS-005 的判别式结构事实。
func codexGateDeclaresUUIDv7BaseInstructions(profile profilecontract.ExecutableProfile) bool {
	return profile.Optional().BaseInstructions.UsesUUIDv7IDs()
}

// codexGateDeclaresRefreshReason 是 EP-002 的判别式结构事实。
func codexGateDeclaresRefreshReason(profile profilecontract.ExecutableProfile) bool {
	endpoint, ok := codexGateEndpoint(profile, officialCodexEndpointOAuthRefresh)
	if !ok {
		return false
	}
	for _, field := range endpoint.Body.Fields {
		if field.Name == "refresh_reason" {
			return true
		}
	}
	return false
}

// codexGateDeclaresImageTurnHeaders 是 EP-022 的判别式结构事实。
func codexGateDeclaresImageTurnHeaders(profile profilecontract.ExecutableProfile) bool {
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		endpoint, ok := codexGateEndpoint(profile, endpointID)
		if !ok {
			return false
		}
		names := map[string]bool{}
		for _, header := range endpoint.Headers {
			names[header.Name] = true
		}
		if !names["x-codex-image-turn-id"] || !names["x-codex-turn-metadata"] {
			return false
		}
	}
	return true
}

// SPEC-EP-002（condition_change）：业务、刷新、realtime 与文件上传使用各自域名来源；本轮另加——OAuth 刷新请求体在
// grant_type 与 refresh_token 之间多一个 refresh_reason，取官方 RefreshReason 的闭集四值。
//
// 检查项与网关观测（真实 wire 字节，生产 OpenAIOAuthService 刷新入口经正式 Compiler／Executor 发出）：
//   - oauth-refresh-body-keys：请求体键序为字典序 client_id, grant_type, refresh_reason, refresh_token（官方 BTreeMap 编码）；
//   - oauth-refresh-reason-closed-set：refresh_reason 取调用方显式要求刷新时的 "forced"，且属于官方闭集；
//   - auth-sni：刷新请求仍发往 auth.openai.com（域名来源判据不变）。
//
// 对照：另一槽位的画像没有 refresh_reason，请求体与引入之前相同，只有三个字段。
func TestCodexOAuthRefreshReasonReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "oauth_refresh 的封闭 Body 声明 refresh_reason", codexGateDeclaresRefreshReason)
	profile := codexGateExecutableProfile(t, mode)
	require.Equal(t, "auth.openai.com", codexGateMustEndpoint(t, profile, officialCodexEndpointOAuthRefresh).Host)

	refresh := func(mode string) (*codexGateWireServer, codexGateWireRequest) {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		require.NoError(t, gate.oauthRefresh(), "OAuth refresh 经生产入口发往 %s 槽位失败", mode)
		requests := server.requestsForPath("/oauth/token")
		require.Len(t, requests, 1)
		return server, requests[0]
	}
	server, request := refresh(mode)
	require.Equal(t, "auth.openai.com", codexGateHelloFor(t, server, request).serverName)
	require.Equal(t, []string{"client_id", "grant_type", "refresh_reason", "refresh_token"}, codexGateJSONFieldOrder(t, request.body),
		"刷新请求体键序必须是字典序并在 grant_type 与 refresh_token 之间带 refresh_reason：%s", request.body)
	reason := gjson.GetBytes(request.body, "refresh_reason").String()
	require.Equal(t, "forced", reason, "调用方显式要求刷新时 refresh_reason 取 forced")
	require.Contains(t, []string{"proactive_expiry", "unauthorized_recovery", "forced", "manual"}, reason,
		"refresh_reason 必须属于官方 RefreshReason 闭集")

	if control, ok := codexGateControlReleaseMode(t, mode); ok {
		_, controlRequest := refresh(control)
		require.Equal(t, []string{"client_id", "grant_type", "refresh_token"}, codexGateJSONFieldOrder(t, controlRequest.body),
			"对照：另一槽位的画像没有 refresh_reason，请求体只有三个字段")
	}
}

// SPEC-EP-022（change）：images generations/edits 使用 JSON、精确字段与相同 Header 线序；本轮另加——两个头
// x-codex-image-turn-id 与 x-codex-turn-metadata 紧跟 version、排在 authorization 之前，turn metadata 是官方 CLI 默认路径的
// 14 键（默认会话模型由画像 TurnMetadata.SessionDefaults 登记）。
//
// 检查项与网关观测（真实 wire 字节）：
//   - image-header-order：generation 与 edit 的线序一致，前三个头恰为 version, x-codex-image-turn-id, x-codex-turn-metadata，
//     authorization 在其后；
//   - image-turn-metadata-default-keys：x-codex-turn-metadata 恰 14 键，含 session_id、thread_id、turn_id、model、
//     reasoning_effort 与 codex_version，键值取自会话默认（model／reasoning_effort 来自画像 SessionDefaults）；
//   - x-codex-image-turn-id 与 turn metadata 里的 turn_id 逐字相同（同一回合）。
//
// 对照：另一槽位没有这两个头，线序以 version, authorization 开头。
func TestCodexImagesTurnHeadersReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "images_generations 与 images_edits 声明 x-codex-image-turn-id 与 x-codex-turn-metadata",
		codexGateDeclaresImageTurnHeaders)
	section := codexGateExecutableProfile(t, mode).Optional().TurnMetadata
	require.NotNil(t, section, "目标画像必须声明 TurnMetadata 节")
	require.NotNil(t, section.SessionDefaults, "目标画像必须登记 TurnMetadata.SessionDefaults（图像请求没有同回合 Responses 事实，14 键靠它补齐）")

	drive := func(mode string) (generation codexGateWireRequest, edit codexGateWireRequest) {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		gate.seedCookie("__cf_bm", "promotion-gate")
		_, err := gate.images(t, "/v1/images/generations", []byte(`{"size":"1024x1024","quality":"high","background":"auto","model":"gpt-image-2","prompt":"draw a cat"}`), "application/json")
		require.NoError(t, err, "图像生成经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
		editBody, editContentType := codexGateImageEditMultipart(t)
		_, err = gate.images(t, "/v1/images/edits", editBody, editContentType)
		require.NoError(t, err, "图像编辑经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
		generations := server.requestsForPath(codexGateImagesGenerationsPath)
		edits := server.requestsForPath(codexGateImagesEditsPath)
		require.Len(t, generations, 1)
		require.Len(t, edits, 1)
		return generations[0], edits[0]
	}
	generation, edit := drive(mode)
	for label, request := range map[string]codexGateWireRequest{"generation": generation, "edit": edit} {
		names := request.lowerHeaderNames()
		require.GreaterOrEqual(t, len(names), 4, label)
		require.Equal(t, []string{"version", "x-codex-image-turn-id", "x-codex-turn-metadata"}, names[:3],
			"%s 的 wire 线序前三个头必须是 version, x-codex-image-turn-id, x-codex-turn-metadata：%v", label, names)
		require.Greater(t, slices.Index(names, "authorization"), 2, "%s：authorization 必须排在两个新头之后：%v", label, names)
		metadata := request.header.Get("X-Codex-Turn-Metadata")
		keys := codexGateWireKeys(t, metadata)
		require.Len(t, keys, 14, "%s：图像请求的 turn metadata 必须是官方默认路径的 14 键（漏登记 SessionDefaults 会静默退回 12 键）：%s", label, metadata)
		for _, required := range []string{"session_id", "thread_id", "turn_id", "model", "reasoning_effort", "codex_version"} {
			require.Contains(t, keys, required, "%s：turn metadata 必须含 %s：%s", label, required, metadata)
		}
		require.Equal(t, section.SessionDefaults.Model, gjson.Get(metadata, "model").String(), "%s：model 取画像登记的默认会话模型", label)
		require.Equal(t, section.SessionDefaults.ReasoningEffort, gjson.Get(metadata, "reasoning_effort").String(), label)
		require.Equal(t, gjson.Get(metadata, "turn_id").String(), request.header.Get("X-Codex-Image-Turn-Id"),
			"%s：x-codex-image-turn-id 与 turn metadata 的 turn_id 必须是同一回合", label)
	}

	if control, ok := codexGateControlReleaseMode(t, mode); ok {
		controlGeneration, controlEdit := drive(control)
		for label, request := range map[string]codexGateWireRequest{"generation": controlGeneration, "edit": controlEdit} {
			names := request.lowerHeaderNames()
			require.Equal(t, []string{"version", "authorization"}, names[:2], "对照 %s：另一槽位没有两个图像回合头：%v", label, names)
			require.NotContains(t, names, "x-codex-image-turn-id", label)
			require.NotContains(t, names, "x-codex-turn-metadata", label)
		}
	}
}

// codexGateLiteThirdPartyHTTPRequest 经生产入口发一条 Lite 模型的 HTTP Responses（第三方风格：顶层 instructions＋函数工具），
// 返回终端上的 wire 请求。
func codexGateLiteThirdPartyHTTPRequest(t *testing.T, mode string) codexGateWireRequest {
	t.Helper()
	server := startCodexGateWireServer(t, nil)
	body, ingress := codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, _ map[string]any) {
		payload["model"] = "gpt-5.6-luna"
		payload["tools"] = []any{codexGateFunctionTool}
		payload["instructions"] = codexGateBaseInstructionsText
	})
	return codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
}

// codexGateRequireLitePrefix 校验 Lite 请求（HTTP 体或 WS 业务帧）input 最前的两项：additional_tools 与基础指令消息，
// 返回两项的 id。wantV7 为 false 时按 0.163 方案（UUIDv5）校验，用于对照槽位。
func codexGateRequireLitePrefix(t *testing.T, raw []byte, kind string, wantV7 bool, where string) (toolsID string, baseID string) {
	t.Helper()
	items := gjson.GetBytes(raw, "input").Array()
	require.GreaterOrEqual(t, len(items), 2, "%s：Lite input 至少有 additional_tools 与基础指令消息两项：%s", where, raw)
	require.Equal(t, "additional_tools", items[0].Get("type").String(), "%s：Lite input 首项必须是 additional_tools", where)
	require.Equal(t, []string{"type", "id", "role", "tools"}, codexGateJSONFieldOrder(t, []byte(items[0].Raw)),
		"%s：additional_tools 键序必须是 type,id,role,tools", where)
	toolsID = items[0].Get("id").String()
	baseID = items[1].Get("id").String()
	pattern := codexGateUUIDv5Pattern
	if wantV7 {
		pattern = codexGateUUIDv7Pattern
	}
	require.Regexp(t, regexp.MustCompile(`^at_`), toolsID, "%s：additional_tools 的 id 前缀 at_", where)
	require.Regexp(t, pattern, toolsID[len("at_"):], "%s：additional_tools 的 id 必须是 at_＋规范 UUID（wantV7=%v）", where, wantV7)
	require.Equal(t, "message", items[1].Get("type").String(), "%s：input 第 2 项必须是基础指令消息", where)
	require.Equal(t, []string{"type", "id", "role", "content", "internal_chat_message_metadata_passthrough"},
		codexGateJSONFieldOrder(t, []byte(items[1].Raw)), "%s：基础指令消息键序", where)
	require.Regexp(t, regexp.MustCompile(`^msg_`), baseID, "%s：基础指令消息 id 前缀 msg_", where)
	require.Regexp(t, pattern, baseID[len("msg_"):], "%s：基础指令消息 id 必须是 msg_＋规范 UUID（wantV7=%v）", where, wantV7)
	require.Len(t, codexGateBaseInstructionsItems(raw, kind), 1, "%s：基础指令消息恰好一条", where)
	return toolsID, baseID
}

// SPEC-BODY-006（change）：主线 Lite 的 Responses 请求体顶层没有 instructions 与 tools，基础指令由 input 的 developer 消息承载、
// 工具目录放在 input 的 additional_tools 项里。
//
// 检查项与网关观测（HTTP Responses 的真实 wire 字节，第三方入站带顶层 instructions 与函数工具）：
//   - main-instructions-absent / lite-top-level-omission：顶层没有 instructions 与 tools；
//   - main-additional-tools-present：input 首项是 additional_tools，键序 type,id,role,tools，id 为 at_＋规范 UUID，且由 UUIDv7 方案生成；
//   - main-base-instructions-item-form：input 第 2 项是官方形态的基础指令消息，id 为 msg_＋UUIDv7；
//   - 同一请求内基础指令消息只有一条，正文逐字等于入站 instructions。
//
// 对照：另一槽位（0.163.0）同一入站得到同样的承载位置，但 additional_tools 没有 id、基础指令消息 id 是 UUIDv5 方案，证明形态由画像 IDScheme 驱动。
func TestCodexLiteTopLevelOmissionReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "BaseInstructions 节声明 IDScheme=uuidv7", codexGateDeclaresUUIDv7BaseInstructions)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	request := codexGateLiteThirdPartyHTTPRequest(t, mode)
	order := codexGateJSONFieldOrder(t, request.body)
	require.NotContains(t, order, "instructions", "Lite 请求顶层不得有 instructions：%v", order)
	require.NotContains(t, order, "tools", "Lite 请求顶层不得有 tools：%v", order)
	codexGateRequireLitePrefix(t, request.body, section.ContentItemKind, true, "HTTP Lite")
	bases := codexGateBaseInstructionsItems(request.body, section.ContentItemKind)
	require.Equal(t, codexGateBaseInstructionsText, bases[0].Get("content.0.text").String(), "基础指令正文逐字等于入站 instructions")

	if control, ok := codexGateControlReleaseMode(t, mode); ok {
		controlRequest := codexGateLiteThirdPartyHTTPRequest(t, control)
		controlOrder := codexGateJSONFieldOrder(t, controlRequest.body)
		require.NotContains(t, controlOrder, "instructions", "对照：0.163 画像同样没有顶层 instructions")
		controlItems := gjson.GetBytes(controlRequest.body, "input").Array()
		require.GreaterOrEqual(t, len(controlItems), 2)
		require.Equal(t, "additional_tools", controlItems[0].Get("type").String(), "对照：0.163 画像 input 首项同样是 additional_tools")
		require.False(t, controlItems[0].Get("id").Exists(), "对照：0.163 画像的 additional_tools 没有 id：%s", controlItems[0].Raw)
		controlBaseID := controlItems[1].Get("id").String()
		require.Regexp(t, regexp.MustCompile(`^msg_`), controlBaseID)
		require.Regexp(t, codexGateUUIDv5Pattern, controlBaseID[len("msg_"):], "对照：0.163 画像的基础指令消息 id 是 UUIDv5 方案")
	}
}

// SPEC-BODY-003（change）：Lite 将工具迁入 additional_tools；本轮——前缀（additional_tools 与基础指令消息）作为窗口开头的历史条目
// 只记录一次，id 是记录时随机的 <前缀>_<UUIDv7>，passthrough 带新窗口四键，之后随历史原样重放、不再重复发出。
//
// 检查项与网关观测（官方 WS 入站会话的真实 wire 帧，Lite 模型）：
//   - main-additional-tools-item-form / main-base-instructions-item-form：前缀两项的键序与 id 形态（at_／msg_＋UUIDv7）；
//   - main-base-instructions-passthrough-keys：基础指令消息的 passthrough 键序是 turn_id, create_time, content_item_kinds,
//     content_item_metadata 的有序子集且必含后两个；
//   - main-passthrough-turn-id-form：项级 passthrough 里的 turn_id 是规范 UUID；
//   - incremental-no-repeat：首个业务帧之后的增量帧不重复 additional_tools 与基础指令消息；
//   - 一条连接上前缀恰好出现一次，id 在整个会话里不变（同一窗口历史条目）。
//
// 对照：另一槽位（0.163.0）的前缀 id 是 UUIDv5 方案、passthrough 只有 content_item_kinds，前缀在预热帧里。
func TestCodexLiteAdditionalToolsItemFormReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "BaseInstructions 节声明 IDScheme=uuidv7", codexGateDeclaresUUIDv7BaseInstructions)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	_, handshakes := codexGateRunWSIngress(t, mode, codexGateLiteThirdPartyWSSteps(t))
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 3, "预热、首个业务帧、增量各一帧")

	toolsID, baseID := codexGateRequireLitePrefix(t, frames[1], section.ContentItemKind, true, "WS 首个业务帧")
	base := codexGateBaseInstructionsItems(frames[1], section.ContentItemKind)[0]
	require.Equal(t, []string{"turn_id", "create_time", "content_item_kinds", "content_item_metadata"},
		codexGateJSONFieldOrder(t, []byte(base.Get("internal_chat_message_metadata_passthrough").Raw)),
		"基础指令消息的 passthrough 必须是新窗口四键且按官方键序")
	require.Regexp(t, codexGateUUIDv7Pattern, base.Get("internal_chat_message_metadata_passthrough.turn_id").String(),
		"项级 passthrough 的 turn_id 必须是规范 UUID")
	require.NotEmpty(t, toolsID)
	require.NotEmpty(t, baseID)

	for index, raw := range frames {
		if index == 1 {
			continue
		}
		for _, role := range codexGateInputRoles(raw, section.ContentItemKind) {
			require.NotEqual(t, "tools", role, "第 %d 帧不得带 additional_tools（前缀只在首个业务帧出现）", index+1)
			require.NotEqual(t, "base", role, "第 %d 帧不得带基础指令消息（前缀只在首个业务帧出现）", index+1)
		}
	}

	if control, ok := codexGateControlReleaseMode(t, mode); ok {
		_, controlHandshakes := codexGateRunWSIngress(t, control, codexGateLiteThirdPartyWSSteps(t))
		require.Len(t, controlHandshakes, 1)
		controlFrames := codexGateResponseCreateFrames(controlHandshakes[0])
		require.NotEmpty(t, controlFrames)
		controlBases := codexGateBaseInstructionsItems(controlFrames[0], section.ContentItemKind)
		require.Len(t, controlBases, 1, "对照：0.163 画像的前缀在预热帧里")
		require.Equal(t, []string{"content_item_kinds"},
			codexGateJSONFieldOrder(t, []byte(controlBases[0].Get("internal_chat_message_metadata_passthrough").Raw)),
			"对照：0.163 画像的基础指令消息 passthrough 只有 content_item_kinds")
	}
}

// codexGateLiteThirdPartyWSSteps 是第三方风格的 Lite WS 入站会话：首帧带顶层 instructions 与函数工具，第二帧是增量帧。
func codexGateLiteThirdPartyWSSteps(t *testing.T) []func(string) []byte {
	t.Helper()
	liteTools := map[string]any{"tools": []any{codexGateFunctionTool}}
	return []func(string) []byte{
		func(string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", "", codexGateBaseInstructionsText,
				[]any{codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")}, liteTools)
		},
		func(previous string) []byte {
			return codexGateWSIngressFrame(t, "gpt-5.6-luna", previous, codexGateBaseInstructionsText,
				[]any{codexGateMessage("user", "turn two")}, nil)
		},
	}
}

// SPEC-WS-005（condition_change）：response.create 字段遵循固定槽位和条件省略；本轮——Lite 预热帧（generate=false）的 input 是空数组，
// 工具目录与基础指令消息改由同一 WS 会话的第一条业务帧带出且排在 input 最前，该业务帧以预热响应 id 作 previous_response_id 续链。
//
// 检查项与网关观测（官方 WS 入站会话的真实 wire 帧，Lite 模型）：
//   - field-slots：每帧顶层字段都是批准字段序的有序子集并含必需项，顶层没有 instructions；
//   - warmup-generate-false / warmup-input-empty：预热帧 generate=false、不带 previous_response_id、input 为空数组；
//   - first-business-frame-carries-prefix：第 2 帧 input 以 additional_tools、基础指令消息开头；
//   - first-business-frame-chains-warmup：第 2 帧的 previous_response_id 恰为同一连接上预热帧的响应；
//   - incremental-prefix-reuse：第 3 帧续接第 2 帧的响应，且不重复前缀。
//
// 对照：另一槽位（0.163.0）预热帧自己带前缀（input 非空），业务帧不带。
func TestCodexLiteWarmupFrameReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "BaseInstructions 节声明 IDScheme=uuidv7", codexGateDeclaresUUIDv7BaseInstructions)
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	require.NotNil(t, section)
	_, handshakes := codexGateRunWSIngress(t, mode, codexGateLiteThirdPartyWSSteps(t))
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 3, "预热、首个业务帧、增量各一帧")
	for index, raw := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		order := codexGateJSONFieldOrder(t, raw)
		codexGateRequireOrderedSubset(t, order, codexGateApprovedWSFields, codexGateApprovedWSRequired, label+" 的顶层字段")
		require.NotContains(t, order, "instructions", "%s：顶层不得有 instructions", label)
	}
	warmup := frames[0]
	require.Equal(t, gjson.False, gjson.GetBytes(warmup, "generate").Type, "首帧必须是 generate=false 的预热帧")
	require.False(t, gjson.GetBytes(warmup, "previous_response_id").Exists(), "预热帧不携带 previous_response_id")
	require.True(t, gjson.GetBytes(warmup, "input").IsArray())
	require.Empty(t, gjson.GetBytes(warmup, "input").Array(), "0.164 起预热帧 input 是空数组：%s", warmup)

	first := frames[1]
	require.NotEqual(t, gjson.False, gjson.GetBytes(first, "generate").Type, "业务帧不得带 generate=false")
	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_1", handshakes[0].connection), gjson.GetBytes(first, "previous_response_id").String(),
		"首个业务帧以预热响应作 previous_response_id 续链")
	roles := codexGateInputRoles(first, section.ContentItemKind)
	require.GreaterOrEqual(t, len(roles), 2)
	require.Equal(t, []string{"tools", "base"}, roles[:2], "首个业务帧 input 以 additional_tools、基础指令消息开头：%v", roles)

	third := frames[2]
	require.Equal(t, fmt.Sprintf("resp_gate_ws_%d_2", handshakes[0].connection), gjson.GetBytes(third, "previous_response_id").String(),
		"增量帧续接紧邻的上一个响应（首个业务帧的响应）")

	if control, ok := codexGateControlReleaseMode(t, mode); ok {
		_, controlHandshakes := codexGateRunWSIngress(t, control, codexGateLiteThirdPartyWSSteps(t))
		require.Len(t, controlHandshakes, 1)
		controlFrames := codexGateResponseCreateFrames(controlHandshakes[0])
		require.NotEmpty(t, controlFrames)
		require.NotEmpty(t, gjson.GetBytes(controlFrames[0], "input").Array(), "对照：0.163 画像的预热帧自己带前缀，input 非空")
	}
}
