package service

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// turn metadata 新键（VC-4 第 4 项）：
//   - 画像 TurnMetadata 节声明时追加 analytics_enabled（取画像值）、model、reasoning_effort、
//     turn_trigger，并按官方结构体声明序插入；prewarm 不写 turn_trigger；
//   - 压缩元数据的 implementation／phase 必须落在画像闭集内；
//   - 画像没有该节时四个生成点（HTTP、WS 握手、WS 每帧、兜底）的输出与改动前逐字节相同。

func turnMetadataTargetSection() *profilecontract.TurnMetadataSection {
	return &profilecontract.TurnMetadataSection{
		Keys: []string{
			"agent_name", "analytics_enabled", "auto_review_enabled", "context_window_id", "installation_id",
			"model", "node_repl_auto_review_required", "node_repl_disabled", "reasoning_effort", "request_kind",
			"root_turn_id", "sandbox", "sandbox_mode", "session_id", "thread_id", "thread_source", "turn_id",
			"turn_started_at_unix_ms", "turn_trigger", "window_id", "window_number",
		},
		AnalyticsEnabled:          true,
		CompactionImplementations: []string{"responses", "responses_compaction_v2"},
		CompactionPhases:          []string{"mid_turn", "post_turn", "pre_turn", "standalone_turn"},
	}
}

func turnMetadataTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.TurnMetadata = syntheticServiceRawSection(t, turnMetadataTargetSection())
	}
}

func jsonTopLevelKeys(t *testing.T, raw string) []string {
	t.Helper()
	keys := make([]string, 0)
	result := gjson.Parse(raw)
	require.True(t, result.IsObject(), "turn metadata 必须是 JSON 对象：%s", raw)
	result.ForEach(func(key, _ gjson.Result) bool {
		keys = append(keys, key.String())
		return true
	})
	return keys
}

func TestExtendOfficialOpenAITurnMetadataJSONFollowsSectionAndOrder(t *testing.T) {
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

	unchanged, err := extendOfficialOpenAITurnMetadataJSON(string(raw), nil, officialCodexTurnMetadataExtension{
		Model: "gpt-5.6-luna", ReasoningEffort: "high", TurnTrigger: "exec",
	})
	require.NoError(t, err)
	require.Equal(t, string(raw), unchanged, "画像没有 TurnMetadata 节时必须原样返回")

	extended, err := extendOfficialOpenAITurnMetadataJSON(string(raw), turnMetadataTargetSection(), officialCodexTurnMetadataExtension{
		Model: "gpt-5.6-luna", ReasoningEffort: "high", TurnTrigger: "exec",
	})
	require.NoError(t, err)
	require.Equal(t, []string{
		"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "thread_source",
		"turn_trigger", "sandbox", "turn_started_at_unix_ms", "analytics_enabled", "compaction", "model",
		"reasoning_effort", "parent_thread_id", "subagent_kind",
	}, jsonTopLevelKeys(t, extended))
	require.Contains(t, extended,
		`"compaction":{"trigger":"manual","reason":"user_requested","implementation":"responses_compaction_v2","phase":"standalone_turn","strategy":"memento"}`,
		"原有嵌套值必须保留原始字节与字段序")
	require.Contains(t, extended, `"turn_started_at_unix_ms":1784919086939`)
	require.True(t, gjson.Get(extended, "analytics_enabled").Bool())
	require.Equal(t, "gpt-5.6-luna", gjson.Get(extended, "model").String())
	require.Equal(t, "high", gjson.Get(extended, "reasoning_effort").String())
	require.Equal(t, "exec", gjson.Get(extended, "turn_trigger").String())

	// 空取值不写键（与官方 Option 为 None 时省略一致）；analytics_enabled 取画像值。
	disabled := turnMetadataTargetSection()
	disabled.AnalyticsEnabled = false
	sparse, err := extendOfficialOpenAITurnMetadataJSON(string(raw), disabled, officialCodexTurnMetadataExtension{})
	require.NoError(t, err)
	require.False(t, gjson.Get(sparse, "model").Exists())
	require.False(t, gjson.Get(sparse, "reasoning_effort").Exists())
	require.False(t, gjson.Get(sparse, "turn_trigger").Exists())
	require.Equal(t, "false", gjson.Get(sparse, "analytics_enabled").Raw)
}

func TestOfficialCodexTurnMetadataSectionRejectsCompactionOutsideClosure(t *testing.T) {
	values := map[string]any{"compaction": officialOpenAICompactionMetadata{
		Implementation: "responses_compact", Phase: "standalone_turn",
	}}
	require.ErrorContains(t,
		applyOfficialCodexTurnMetadataSection(values, turnMetadataTargetSection(), officialCodexTurnMetadataExtension{}),
		"compaction.implementation 不在画像闭集")
	values["compaction"] = map[string]any{"implementation": "responses_compaction_v2", "phase": "unknown_phase"}
	require.ErrorContains(t,
		applyOfficialCodexTurnMetadataSection(values, turnMetadataTargetSection(), officialCodexTurnMetadataExtension{}),
		"compaction.phase 不在画像闭集")
	require.NoError(t, applyOfficialCodexTurnMetadataSection(values, nil, officialCodexTurnMetadataExtension{}),
		"画像没有该节时不校验、不改动")
}

func TestOfficialCodexTurnTriggerAndEffectiveEffort(t *testing.T) {
	require.Equal(t, "exec", officialCodexTurnTrigger(officialCodexSurfaceExec, "", false))
	require.Equal(t, "user", officialCodexTurnTrigger(officialCodexSurfaceTUI, "", false))
	require.Equal(t, "exec", officialCodexTurnTrigger(officialCodexSurfaceExec, "collab_spawn", false), "普通子代理继承入口触发来源")
	require.Equal(t, "guardian_review", officialCodexTurnTrigger(officialCodexSurfaceTUI, "guardian", false))
	require.Equal(t, "memory_consolidation", officialCodexTurnTrigger(officialCodexSurfaceExec, "memory_consolidation", true))
	require.Empty(t, officialCodexTurnTrigger("unknown", "", false))

	defaults := officialOpenAIReasoningDefaults{Effort: "Medium", Known: true}
	require.Equal(t, "high", officialOpenAIEffectiveReasoningEffort(map[string]any{"reasoning": map[string]any{"effort": "high"}}, defaults))
	require.Equal(t, "max", officialOpenAIEffectiveReasoningEffort(map[string]any{"reasoning": map[string]any{"effort": "ultra"}}, defaults))
	require.Equal(t, "medium", officialOpenAIEffectiveReasoningEffort(map[string]any{}, defaults))
	require.Empty(t, officialOpenAIEffectiveReasoningEffort(map[string]any{}, officialOpenAIReasoningDefaults{}))
}

// HTTP 生成点：旧画像无新键；目标画像在登记的 turn metadata 中写入四个新键。
func TestOfficialOpenAIHTTPTurnMetadataFollowsProfileSection(t *testing.T) {
	build := func(t *testing.T) string {
		t.Helper()
		body := newOfficialOpenAIHTTPTestBody(t, false, false, false)
		contract, err := captureOfficialOpenAIHTTPBodyContract(body)
		require.NoError(t, err)
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
			c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
			openAIUpstreamRequestPlan{
				IsStream: true, PromptCacheKey: testOfficialOpenAISessionID, IsCodexCLI: true,
				OfficialEgressBodyContract: contract,
			},
		)
		require.NoError(t, err)
		egressContext, ok := OfficialEgressContextFromContext(req.Context())
		require.True(t, ok)
		return mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value()
	}

	legacy := build(t)
	for _, key := range []string{"analytics_enabled", "model", "reasoning_effort", "turn_trigger"} {
		require.False(t, gjson.Get(legacy, key).Exists(), "旧画像 turn metadata 不得出现 %s", key)
	}

	withOfficialCodexSyntheticProfile(t, turnMetadataTargetMutation(t))
	target := build(t)
	require.True(t, gjson.Get(target, "analytics_enabled").Bool())
	require.NotEmpty(t, gjson.Get(target, "model").String())
	require.Equal(t, "exec", gjson.Get(target, "turn_trigger").String())
	legacyKeys := jsonTopLevelKeys(t, legacy)
	targetKeys := jsonTopLevelKeys(t, target)
	filtered := make([]string, 0, len(targetKeys))
	for _, key := range targetKeys {
		switch key {
		case "analytics_enabled", "model", "reasoning_effort", "turn_trigger":
			continue
		}
		filtered = append(filtered, key)
	}
	require.Equal(t, legacyKeys, filtered, "目标画像只追加新键，不改变原有键的相对顺序")
}

func newTurnMetadataWSTestContext(t *testing.T) *OfficialEgressContext {
	t.Helper()
	state := defaultOfficialCodexRuntimeState()
	state.ProfileMode = officialClientProfileModeActive
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID: 157, TargetPlatform: PlatformOpenAI,
		ProfileVersion: officialCodexVersion0145, ProfileMode: officialClientProfileModeActive,
		Transport: OfficialEgressTransportWebSocket, UpstreamHost: "chatgpt.com",
		DefaultReasoningLevel: "medium", ReasoningDefaultsKnown: true,
		CodexRuntimeState: state,
	})
	for name, value := range map[OfficialEgressFieldName]string{
		OfficialEgressFieldDeviceID:  testOfficialOpenAIInstallationID,
		OfficialEgressFieldSessionID: testOfficialOpenAISessionID,
		OfficialEgressFieldThreadID:  testOfficialOpenAISessionID,
		OfficialEgressFieldWindowID:  testOfficialOpenAISessionID + ":0",
	} {
		require.NoError(t, egressContext.RegisterField(
			name, value, OfficialEgressFieldSourceDerived, OfficialEgressFieldLifecycleSession,
		))
	}
	egressContext.openAIWSDerived = &officialOpenAIWSDerivedState{}
	return egressContext
}

// WS 每帧生成点：普通帧写入 turn_trigger，prewarm 帧不写；旧画像无新键。
func TestOfficialOpenAIWSFrameTurnMetadataFollowsProfileSection(t *testing.T) {
	turnPayload := func() map[string]any {
		return map[string]any{
			"type": "response.create", "model": "gpt-5.6-luna",
			"input": []any{map[string]any{
				"type": "message", "role": "user",
				"content": []any{map[string]any{"type": "input_text", "text": "hi"}},
			}},
		}
	}
	legacyMetadata, _, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		newTurnMetadataWSTestContext(t), turnPayload(), false,
	)
	require.NoError(t, err)
	legacy, ok := legacyMetadata["x-codex-turn-metadata"].(string)
	require.True(t, ok, "旧画像的 turn metadata 必须是字符串")
	require.False(t, gjson.Get(legacy, "analytics_enabled").Exists())

	withOfficialCodexSyntheticProfile(t, turnMetadataTargetMutation(t))
	metadata, _, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		newTurnMetadataWSTestContext(t), turnPayload(), false,
	)
	require.NoError(t, err)
	turn, ok := metadata["x-codex-turn-metadata"].(string)
	require.True(t, ok, "目标画像的 turn metadata 必须是字符串")
	require.True(t, gjson.Get(turn, "analytics_enabled").Bool())
	require.Equal(t, "gpt-5.6-luna", gjson.Get(turn, "model").String())
	require.Equal(t, "medium", gjson.Get(turn, "reasoning_effort").String(), "缺省 effort 按模型默认值补齐")
	require.Equal(t, "exec", gjson.Get(turn, "turn_trigger").String())

	prewarm := turnPayload()
	prewarm["generate"] = false
	prewarmMetadata, _, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		newTurnMetadataWSTestContext(t), prewarm, false,
	)
	require.NoError(t, err)
	prewarmTurn, ok := prewarmMetadata["x-codex-turn-metadata"].(string)
	require.True(t, ok, "prewarm 帧的 turn metadata 必须是字符串")
	require.Equal(t, "prewarm", gjson.Get(prewarmTurn, "request_kind").String())
	require.False(t, gjson.Get(prewarmTurn, "turn_trigger").Exists(), "prewarm 不属于某一轮")
	require.True(t, gjson.Get(prewarmTurn, "analytics_enabled").Exists())
}

// 兜底生成点：未登记 turn metadata 的 Responses 请求。旧画像保持结构体序列化结果。
func TestOfficialCodexFallbackTurnMetadataFollowsProfileSection(t *testing.T) {
	build := func(t *testing.T) string {
		t.Helper()
		state := defaultOfficialCodexRuntimeState()
		state.ProfileMode = officialClientProfileModeActive
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
	legacy := build(t)
	require.Equal(t, []string{
		"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "thread_source", "sandbox",
	}, jsonTopLevelKeys(t, legacy))

	withOfficialCodexSyntheticProfile(t, turnMetadataTargetMutation(t))
	target := build(t)
	require.Equal(t, []string{
		"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "thread_source",
		"turn_trigger", "sandbox", "analytics_enabled", "model", "reasoning_effort",
	}, jsonTopLevelKeys(t, target))
	require.Equal(t, "low", gjson.Get(target, "reasoning_effort").String())
	// 基础键取值与旧逻辑一致。
	for _, key := range []string{"installation_id", "session_id", "thread_id", "turn_id", "window_id", "request_kind", "thread_source", "sandbox"} {
		require.Equal(t, gjson.Get(legacy, key).Raw, gjson.Get(target, key).Raw, key)
	}
}

// WS 握手生成点：prewarm 形态，写 analytics_enabled、model、reasoning_effort，不写 turn_trigger。
func TestOfficialOpenAIWSHandshakeTurnMetadataFollowsProfileSection(t *testing.T) {
	firstFrame := []byte(`{"type":"response.create","model":"gpt-5.6-luna","reasoning":{"effort":"high"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
	handshake := func(t *testing.T) string {
		t.Helper()
		egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID: 157, TargetPlatform: PlatformOpenAI,
			ProfileVersion: officialCodexVersion0145, ProfileMode: officialClientProfileModeActive,
			Transport: OfficialEgressTransportWebSocket, UpstreamHost: "chatgpt.com",
		})
		c := promptCacheTestContext(t, firstFrame)
		require.NoError(t, prepareOpenAIOfficialEgressWSContext(
			egressContext, c, newOfficialOpenAIHTTPTestAccount(157), firstFrame,
		))
		return mustOfficialEgressField(t, egressContext, OfficialEgressFieldTurnMetadata).Value()
	}
	legacy := handshake(t)
	require.Equal(t, "prewarm", gjson.Get(legacy, "request_kind").String())
	require.False(t, gjson.Get(legacy, "model").Exists())

	withOfficialCodexSyntheticProfile(t, turnMetadataTargetMutation(t))
	target := handshake(t)
	require.Equal(t, "prewarm", gjson.Get(target, "request_kind").String())
	require.True(t, gjson.Get(target, "analytics_enabled").Bool())
	require.Equal(t, "gpt-5.6-luna", gjson.Get(target, "model").String())
	require.Equal(t, "high", gjson.Get(target, "reasoning_effort").String())
	require.False(t, gjson.Get(target, "turn_trigger").Exists())
}
