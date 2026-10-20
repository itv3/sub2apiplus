package service

import (
	"fmt"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

var (
	// codexGateApprovedTurnMetadataKeys 是批准的画像 TurnMetadata.Keys（SPEC-BODY-009 turn-metadata-keys 的 allowed，按键名排序）。
	codexGateApprovedTurnMetadataKeys = []string{
		"agent_name", "analytics_enabled", "auto_review_enabled", "context_window_id", "history_initialization",
		"installation_id", "model", "node_repl_auto_review_required", "node_repl_disabled", "reasoning_effort",
		"request_kind", "root_turn_id", "sandbox", "sandbox_mode", "session_id", "thread_id", "thread_source", "turn_id",
		"turn_started_at_unix_ms", "turn_trigger", "window_id", "window_number",
	}
	// codexGateApprovedTurnMetadataRequired 是网关可信生成、出站必须携带的 14 个键（turn-metadata-keys 的 required）；
	// 官方另 8 键网关没有可信取值，不仿真，不在必需项内。
	codexGateApprovedTurnMetadataRequired = []string{
		"analytics_enabled", "history_initialization", "installation_id", "model", "reasoning_effort", "request_kind",
		"sandbox", "session_id", "thread_id", "thread_source", "turn_id", "turn_started_at_unix_ms", "turn_trigger", "window_id",
	}
)

// codexGateTurnMetadataDeclaresHistoryInitialization 是 SPEC-BODY-009 的判别式结构事实：画像 TurnMetadata 节声明了
// history_initialization 键。
func codexGateTurnMetadataDeclaresHistoryInitialization(profile profilecontract.ExecutableProfile) bool {
	section := profile.Optional().TurnMetadata
	return section != nil && slices.Contains(section.Keys, "history_initialization")
}

// codexGateWireKeys 返回 turn metadata JSON 的键，按线序。
func codexGateWireKeys(t *testing.T, raw string) []string {
	t.Helper()
	require.True(t, gjson.Valid(raw), "turn metadata 不是合法 JSON：%s", raw)
	var keys []string
	gjson.Parse(raw).ForEach(func(key, _ gjson.Result) bool {
		keys = append(keys, key.String())
		return true
	})
	return keys
}

// SPEC-BODY-009（add）：x-codex-turn-metadata 的键为画像 TurnMetadata.Keys 的有序子集，必含网关可信生成的键；本轮新增
// history_initialization，紧跟 request_kind，取值为官方闭集里的默认值 new，默认配置 analytics_enabled=true。
//
// 检查项与网关观测（真实 wire 字节）：
//   - turn-metadata-keys：HTTP Responses 的 x-codex-turn-metadata 头（与请求体 client_metadata 中的同名值逐字节一致）、
//     Remote Compaction V2 请求、WS 握手头（预热形态）与 WS response.create 帧（预热、首轮、增量）里的 turn metadata，
//     键排序后都是批准的 22 键的有序子集并含 14 个必需键（预热形态没有轮次，不要求 turn_trigger 与 turn_started_at_unix_ms），
//     history_initialization 在线序上紧跟 request_kind；
//   - turn-metadata-analytics-default：analytics_enabled 取画像固定值 true（JSON 布尔）；
//   - 一致性：同一运行时的所有生成点——HTTP 头、请求体、压缩请求、WS 握手、WS 每帧——history_initialization 取同一个值。
//
// 对照：另一槽位的画像没有该键，上述每个生成点都不出现 history_initialization，键序与取值与引入之前相同。
func TestCodexTurnMetadataHistoryInitializationReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "TurnMetadata 节声明 history_initialization 键",
		codexGateTurnMetadataDeclaresHistoryInitialization)
	section := codexGateExecutableProfile(t, mode).Optional().TurnMetadata
	require.NotNil(t, section, "目标画像必须声明 TurnMetadata 节")
	require.Equal(t, codexGateApprovedTurnMetadataKeys, section.Keys, "画像 TurnMetadata.Keys 必须恰为批准的 22 键")
	require.True(t, section.AnalyticsEnabled, "画像默认 analytics_enabled 必须为 true")

	observed := map[string]bool{}
	// requireMetadata 按批准判据校验一份出站 turn metadata，并登记它的 history_initialization 取值。
	requireMetadata := func(raw string, where string) {
		t.Helper()
		keys := codexGateWireKeys(t, raw)
		sorted := slices.Clone(keys)
		required := codexGateApprovedTurnMetadataRequired
		switch gjson.Get(raw, "request_kind").String() {
		case "prewarm":
			required = slices.DeleteFunc(slices.Clone(required), func(key string) bool {
				return key == "turn_trigger" || key == "turn_started_at_unix_ms"
			})
		case "compaction":
			// 压缩请求另带官方的 compaction 对象，它不在普通轮次的 22 键边界里，由压缩语义的门禁单独锁定。
			sorted = slices.DeleteFunc(sorted, func(key string) bool { return key == "compaction" })
		}
		slices.Sort(sorted)
		codexGateRequireOrderedSubset(t, sorted, codexGateApprovedTurnMetadataKeys, required, where+" 的 turn metadata 键")
		position := slices.Index(keys, "history_initialization")
		require.Positive(t, position, "%s：必须携带 history_initialization：%s", where, raw)
		require.Equal(t, "request_kind", keys[position-1], "%s：history_initialization 在线序上必须紧跟 request_kind：%v", where, keys)
		require.Equal(t, gjson.True, gjson.Get(raw, "analytics_enabled").Type, "%s：默认配置下 analytics_enabled 必须为 JSON true", where)
		observed[gjson.Get(raw, "history_initialization").String()] = true
	}

	// ---- HTTP：普通 Responses 与 Remote Compaction V2 ----
	server := startCodexGateWireServer(t, nil)
	body, ingress := codexGateRootIngress(t, nil)
	request := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	header := request.header.Get("X-Codex-Turn-Metadata")
	requireMetadata(header, "HTTP Responses")
	require.Equal(t, "turn", gjson.Get(header, "request_kind").String())
	require.Equal(t, header, gjson.GetBytes(request.body, "client_metadata.x-codex-turn-metadata").String(),
		"请求体 client_metadata 中的 turn metadata 必须与头逐字节一致")
	compactionBody, compactionIngress := codexGateCompactionIngress(t, "user_requested")
	compaction := codexGateForwardResponsesWire(t, mode, server, compactionIngress, compactionBody, nil)
	compactionHeader := compaction.header.Get("X-Codex-Turn-Metadata")
	requireMetadata(compactionHeader, "HTTP 压缩请求")
	require.Equal(t, "compaction", gjson.Get(compactionHeader, "request_kind").String())
	require.Equal(t, "responses_compaction_v2", gjson.Get(compactionHeader, "compaction.implementation").String())
	require.Equal(t, compactionHeader, gjson.GetBytes(compaction.body, "client_metadata.x-codex-turn-metadata").String(),
		"压缩请求体 client_metadata 中的 turn metadata 必须与头逐字节一致")

	// ---- WS：HTTP 入站派生的上游 WS（握手头与帧） ----
	wsServer := startCodexGateWireServer(t, nil)
	wsGate := newCodexGateService(t, mode, wsServer)
	codexGateUseRecorderManifest(wsGate.service, wsGate.account)
	wsGate.enableWebSocket(t, wsServer)
	derived, err := wsGate.forwardViaWebSocket(t, wsServer, codexGateWebSocketBody(codexGateNonLiteModel, nil), nil)
	require.NoError(t, err)
	requireMetadata(derived.header.Get("X-Codex-Turn-Metadata"), "WS 派生握手头")
	require.Equal(t, "prewarm", gjson.Get(derived.header.Get("X-Codex-Turn-Metadata"), "request_kind").String())
	for index, frame := range codexGateResponseCreateFrames(derived) {
		requireMetadata(gjson.GetBytes(frame, "client_metadata.x-codex-turn-metadata").String(),
			fmt.Sprintf("WS 派生第 %d 帧", index+1))
	}

	// ---- WS：官方 WS 入站（握手头、预热帧、首轮、增量） ----
	text := codexGateBaseInstructionsText
	_, handshakes := codexGateRunWSIngress(t, mode, []func(string) []byte{
		func(string) []byte {
			return codexGateWSIngressFrame(t, codexGateNonLiteModel, "", text,
				[]any{codexGateMessage("developer", "developer context"), codexGateMessage("user", "turn one")}, nil)
		},
		func(previous string) []byte {
			return codexGateWSIngressFrame(t, codexGateNonLiteModel, previous, text,
				[]any{codexGateMessage("user", "turn two")}, nil)
		},
	})
	require.Len(t, handshakes, 1)
	requireMetadata(handshakes[0].header.Get("X-Codex-Turn-Metadata"), "WS 入站握手头")
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 3, "预热、首轮、增量各一帧")
	for index, frame := range frames {
		requireMetadata(gjson.GetBytes(frame, "client_metadata.x-codex-turn-metadata").String(),
			fmt.Sprintf("WS 入站第 %d 帧", index+1))
	}
	require.Equal(t, map[string]bool{"new": true}, observed, "所有生成点的 history_initialization 必须取同一个值（默认的新会话）")

	// ---- 对照：另一槽位没有该键 ----
	control, ok := codexGateControlReleaseMode(t, mode)
	require.True(t, ok, "判别式门禁必有一个不具备该结构事实的对照槽位")
	controlServer := startCodexGateWireServer(t, nil)
	controlBody, controlIngress := codexGateRootIngress(t, nil)
	controlRequest := codexGateForwardResponsesWire(t, control, controlServer, controlIngress, controlBody, nil)
	controlCompactionBody, controlCompactionIngress := codexGateCompactionIngress(t, "user_requested")
	controlCompaction := codexGateForwardResponsesWire(t, control, controlServer, controlCompactionIngress, controlCompactionBody, nil)
	controlWSServer := startCodexGateWireServer(t, nil)
	controlWSGate := newCodexGateService(t, control, controlWSServer)
	codexGateUseRecorderManifest(controlWSGate.service, controlWSGate.account)
	controlWSGate.enableWebSocket(t, controlWSServer)
	controlDerived, err := controlWSGate.forwardViaWebSocket(t, controlWSServer, codexGateWebSocketBody(codexGateNonLiteModel, nil), nil)
	require.NoError(t, err)
	controlMetadata := map[string]string{
		"HTTP 头":     controlRequest.header.Get("X-Codex-Turn-Metadata"),
		"HTTP 请求体":   gjson.GetBytes(controlRequest.body, "client_metadata.x-codex-turn-metadata").String(),
		"压缩请求头":      controlCompaction.header.Get("X-Codex-Turn-Metadata"),
		"压缩请求体":      gjson.GetBytes(controlCompaction.body, "client_metadata.x-codex-turn-metadata").String(),
		"WS 握手头":     controlDerived.header.Get("X-Codex-Turn-Metadata"),
		"WS 派生第 1 帧": gjson.GetBytes(codexGateResponseCreateFrames(controlDerived)[0], "client_metadata.x-codex-turn-metadata").String(),
	}
	for where, raw := range controlMetadata {
		require.NotEmpty(t, raw, "对照：%s 的 turn metadata 缺失", where)
		require.NotContains(t, codexGateWireKeys(t, raw), "history_initialization", "对照：另一槽位的 %s 不得出现该键：%s", where, raw)
	}
}
