package service

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 画像 TurnMetadata 节声明 history_initialization 键时，四个生成点（HTTP、WS 握手、WS 每帧含预热帧、
// 兜底）与压缩请求都恒写入该键（默认新会话），位置紧跟 request_kind；画像没有声明该键时输出与引入之前
// 逐字节相同（见 official_egress_openai_turn_metadata_section_test.go 的既有用例）。

func turnMetadataSectionWithHistoryInitialization() *profilecontract.TurnMetadataSection {
	section := turnMetadataTargetSection()
	section.Keys = append(append([]string(nil), section.Keys...), officialCodexTurnMetadataKeyHistoryInitialization)
	slices.Sort(section.Keys)
	return section
}

func TestApplyOfficialCodexTurnMetadataSectionWritesHistoryInitialization(t *testing.T) {
	section := turnMetadataSectionWithHistoryInitialization()
	const key = officialCodexTurnMetadataKeyHistoryInitialization

	values := map[string]any{}
	require.NoError(t, applyOfficialCodexTurnMetadataSection(values, section, officialCodexTurnMetadataExtension{}))
	require.Equal(t, "new", values[key], "扩展留空取默认的新会话；官方恒带该键，不同于没有可信取值就省略的其他键")

	for _, value := range []string{"new", "cleared", "cold_resume", "warm_fork", "cold_fork", "supplied_history"} {
		values = map[string]any{}
		require.NoError(t, applyOfficialCodexTurnMetadataSection(values, section,
			officialCodexTurnMetadataExtension{HistoryInitialization: value}), value)
		require.Equal(t, value, values[key])
	}
	values = map[string]any{}
	require.NoError(t, applyOfficialCodexTurnMetadataSection(values, section,
		officialCodexTurnMetadataExtension{HistoryInitialization: "  "}))
	require.Equal(t, "new", values[key], "空白视同留空")

	for _, invalid := range []string{"bogus", "New", "resume", "fork"} {
		values = map[string]any{}
		require.ErrorContains(t, applyOfficialCodexTurnMetadataSection(values, section,
			officialCodexTurnMetadataExtension{HistoryInitialization: invalid}), "history_initialization 不在官方取值闭集", invalid)
		require.NotContains(t, values, key, "非法取值不得写入")
	}

	// 画像没有声明该键：即使扩展给了值也不写。
	values = map[string]any{}
	require.NoError(t, applyOfficialCodexTurnMetadataSection(values, turnMetadataTargetSection(),
		officialCodexTurnMetadataExtension{HistoryInitialization: "cold_resume"}))
	require.NotContains(t, values, key)
	// 画像没有 TurnMetadata 节：恒等操作。
	values = map[string]any{}
	require.NoError(t, applyOfficialCodexTurnMetadataSection(values, nil, officialCodexTurnMetadataExtension{}))
	require.Empty(t, values)
}

func TestOfficialCodexHistoryInitializationValueDomain(t *testing.T) {
	require.Equal(t,
		[]string{"new", "cleared", "cold_resume", "warm_fork", "cold_fork", "supplied_history"},
		officialCodexHistoryInitializationValues)
	require.Equal(t, "new", officialCodexHistoryInitialization())
	for _, value := range officialCodexHistoryInitializationValues {
		require.NoError(t, validateOfficialCodexHistoryInitialization(value))
	}
	require.Error(t, validateOfficialCodexHistoryInitialization(""))
}

func TestExtendOfficialOpenAITurnMetadataJSONPlacesHistoryInitializationAfterRequestKind(t *testing.T) {
	raw, err := marshalOfficialOpenAITurnMetadata(map[string]any{
		"installation_id": "i", "session_id": "s", "thread_id": "t", "turn_id": "u", "window_id": "t:0",
		"request_kind": "compaction", "thread_source": "subagent", "sandbox": "seccomp",
		"turn_started_at_unix_ms": int64(1784919086939),
		"compaction": officialOpenAICompactionMetadata{
			Trigger: "manual", Reason: "user_requested", Implementation: "responses_compaction_v2",
			Phase: "standalone_turn", Strategy: "memento",
		},
		"parent_thread_id": "p", "subagent_kind": "guardian",
	})
	require.NoError(t, err)
	extension := officialCodexTurnMetadataExtension{Model: "gpt-5.6-luna", ReasoningEffort: "high", TurnTrigger: "exec"}

	declared, err := extendOfficialOpenAITurnMetadataJSON(string(raw), turnMetadataSectionWithHistoryInitialization(), extension)
	require.NoError(t, err)
	require.Equal(t, []string{
		"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "history_initialization",
		"thread_source", "turn_trigger", "sandbox", "turn_started_at_unix_ms", "analytics_enabled", "compaction",
		"model", "reasoning_effort", "parent_thread_id", "subagent_kind",
	}, jsonTopLevelKeys(t, declared))
	require.Equal(t, "new", gjson.Get(declared, "history_initialization").String())

	// 画像没有声明该键：键集与键序与引入之前相同。
	undeclared, err := extendOfficialOpenAITurnMetadataJSON(string(raw), turnMetadataTargetSection(), extension)
	require.NoError(t, err)
	require.Equal(t, []string{
		"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "thread_source",
		"turn_trigger", "sandbox", "turn_started_at_unix_ms", "analytics_enabled", "compaction", "model",
		"reasoning_effort", "parent_thread_id", "subagent_kind",
	}, jsonTopLevelKeys(t, undeclared))
	require.False(t, gjson.Get(undeclared, "history_initialization").Exists())
}

func requireHistoryInitializationAfterRequestKind(t *testing.T, metadata, label string) {
	t.Helper()
	require.Equal(t, "new", gjson.Get(metadata, "history_initialization").String(), label)
	keys := jsonTopLevelKeys(t, metadata)
	position := slices.Index(keys, officialCodexTurnMetadataKeyHistoryInitialization)
	require.Positive(t, position, label)
	require.Equal(t, "request_kind", keys[position-1], "%s：history_initialization 必须紧跟 request_kind", label)
}

func withCompactionTrigger(t *testing.T, body []byte) []byte {
	t.Helper()
	var payload map[string]any
	require.NoError(t, json.Unmarshal(body, &payload))
	input, ok := payload["input"].([]any)
	require.True(t, ok)
	payload["input"] = append(input, map[string]any{"type": "compaction_trigger"})
	out, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return out
}

// previous 槽位装了声明该键的合成画像：每个生成点都写入，同一请求走 active 槽位则没有。
func TestTurnMetadataHistoryInitializationAtEveryGenerationPoint(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	const key = officialCodexTurnMetadataKeyHistoryInitialization

	httpMetadata := func(t *testing.T, mode string, body []byte) string {
		t.Helper()
		egressContext, _ := buildOfficialOpenAIHTTPRequestForSlot(t, mode, body)
		return mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value()
	}

	t.Run("HTTP", func(t *testing.T) {
		body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
		metadata := httpMetadata(t, officialClientProfileModePrevious, body)
		requireHistoryInitializationAfterRequestKind(t, metadata, "HTTP")
		require.Equal(t, "turn", gjson.Get(metadata, "request_kind").String())
		require.False(t, gjson.Get(httpMetadata(t, officialClientProfileModeActive, body), key).Exists(), "active 槽位不声明该键")
	})

	t.Run("HTTP 压缩请求同路径自动带", func(t *testing.T) {
		body := withCompactionTrigger(t, newOfficialOpenAIHTTPTestBody(t, false, false, false))
		metadata := httpMetadata(t, officialClientProfileModePrevious, body)
		require.Equal(t, "compaction", gjson.Get(metadata, "request_kind").String(), "前提：这是一次压缩请求")
		require.True(t, gjson.Get(metadata, "compaction").Exists())
		requireHistoryInitializationAfterRequestKind(t, metadata, "HTTP 压缩请求")
	})

	t.Run("WS 每帧与预热帧", func(t *testing.T) {
		turnPayload := func() map[string]any {
			return map[string]any{
				"type": "response.create", "model": "gpt-5.6-luna",
				"input": []any{map[string]any{
					"type": "message", "role": "user",
					"content": []any{map[string]any{"type": "input_text", "text": "hi"}},
				}},
			}
		}
		frameMetadata := func(mode string, payload map[string]any) string {
			metadata, _, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
				newTurnMetadataWSTestContextForMode(t, mode), payload, false)
			require.NoError(t, err)
			text, ok := metadata["x-codex-turn-metadata"].(string)
			require.True(t, ok)
			return text
		}
		turn := frameMetadata(officialClientProfileModePrevious, turnPayload())
		require.Equal(t, "turn", gjson.Get(turn, "request_kind").String())
		requireHistoryInitializationAfterRequestKind(t, turn, "WS 业务帧")

		prewarm := turnPayload()
		prewarm["generate"] = false
		prewarmTurn := frameMetadata(officialClientProfileModePrevious, prewarm)
		require.Equal(t, "prewarm", gjson.Get(prewarmTurn, "request_kind").String())
		requireHistoryInitializationAfterRequestKind(t, prewarmTurn, "WS 预热帧")

		require.False(t, gjson.Get(frameMetadata(officialClientProfileModeActive, turnPayload()), key).Exists())
	})

	t.Run("WS 握手", func(t *testing.T) {
		firstFrame := []byte(`{"type":"response.create","model":"gpt-5.6-luna","reasoning":{"effort":"high"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
		handshake := func(mode string) string {
			egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
				AccountID: 157, TargetPlatform: PlatformOpenAI,
				ProfileVersion: officialCodexVersion0145, ProfileMode: mode,
				Transport: OfficialEgressTransportWebSocket, UpstreamHost: "chatgpt.com",
			})
			c := promptCacheTestContext(t, firstFrame)
			require.NoError(t, prepareOpenAIOfficialEgressWSContext(
				egressContext, c, newOfficialOpenAIHTTPTestAccount(157), firstFrame))
			return mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value()
		}
		metadata := handshake(officialClientProfileModePrevious)
		require.Equal(t, "prewarm", gjson.Get(metadata, "request_kind").String(), "握手是预热形态")
		requireHistoryInitializationAfterRequestKind(t, metadata, "WS 握手")
		require.False(t, gjson.Get(handshake(officialClientProfileModeActive), key).Exists())
	})

	t.Run("兜底", func(t *testing.T) {
		fallback := func(mode string) string {
			state := defaultOfficialCodexRuntimeState()
			state.ProfileMode = mode
			body := `{"model":"gpt-5.6-luna","input":[],"reasoning":{"effort":"low"}}`
			req := httptest.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", strings.NewReader(body))
			ctx, err := withOfficialCodexRuntimeState(req.Context(), state)
			require.NoError(t, err)
			req = req.WithContext(ctx)
			attempt, err := prepareOfficialCodexSemanticAttempt(
				req, []byte(body), officialCodexEndpointResponsesHTTP, "fallback-turn-metadata",
				projectOfficialCodexIdentityAccount(officialEgressTestAccount(157, PlatformOpenAI)),
			)
			require.NoError(t, err)
			return attempt.IdentityFacts.TurnMetadata.Value
		}
		requireHistoryInitializationAfterRequestKind(t, fallback(officialClientProfileModePrevious), "兜底")
		require.False(t, gjson.Get(fallback(officialClientProfileModeActive), key).Exists())
	})
}
