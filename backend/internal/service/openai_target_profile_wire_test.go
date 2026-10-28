package service

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 目标版本画像的合成与线上字节断言。
//
// 目标版本（0.165）的可执行画像要到版本登记的画像生成步骤才入库。在那之前，用“上一版画像＋规则补丁清单”合成目标画像：
// 补丁清单是 tools/official_client_capture/profile_rule_patches_0_165_0.json（登记产物，七条 add 补丁），用画像契约包
// NewProfileSpec → CompileExecutableProfile 解码编译（与登记阶段验证补丁的方法相同）。合成后同时替换两处：
//   - service 层读到的画像（withMutatedOfficialProfile，service 逻辑按画像门控的开关）；
//   - Executor runtime 的 BundleResolver（newTargetAwareGate 在建好测试网关后，用合成画像的发布目录副本重建它），让真实
//     Executor/Compiler 按合成画像编译线上字节。不改任何生产文件：runtime 的 BundleResolver 是导出字段，测试直接替换。
//
// 画像入库、Active 已是目标版本之后，补丁里的值已在画像里，本文件的应用逻辑对“已存在且取值相同”的补丁幂等跳过，测试无需改动。

type profileRulePatch struct {
	RuleIDs []string        `json:"rule_ids"`
	Op      string          `json:"op"`
	Path    string          `json:"path"`
	Before  json.RawMessage `json:"before"`
	After   json.RawMessage `json:"after"`
}

type profileRulePatchManifest struct {
	SchemaVersion string             `json:"schema_version"`
	Patches       []profileRulePatch `json:"rule_patches"`
}

// loadTargetProfileRulePatches 读取目标版本画像补丁清单。
func loadTargetProfileRulePatches(t *testing.T) []profileRulePatch {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "tools", "official_client_capture", "profile_rule_patches_0_165_0.json"))
	require.NoError(t, err)
	var manifest profileRulePatchManifest
	require.NoError(t, json.Unmarshal(raw, &manifest))
	require.NotEmpty(t, manifest.Patches)
	return manifest.Patches
}

// applyProfileRulePatches 把 add 补丁应用到快照文档的可选节上：路径形如 /<节>/<字段>。目标字段已存在且取值相同则跳过（幂等），
// 已存在但取值不同、节不存在、操作不是 add 都直接失败。
func applyProfileRulePatches(t *testing.T, doc *profilecontract.SnapshotDoc, patches []profileRulePatch) {
	t.Helper()
	for _, patch := range patches {
		require.Equal(t, "add", patch.Op, "补丁 %s", patch.Path)
		segments := strings.Split(strings.TrimPrefix(patch.Path, "/"), "/")
		require.Len(t, segments, 2, "补丁路径必须是 /<节>/<字段>：%s", patch.Path)
		field := reflect.ValueOf(doc).Elem().FieldByName(segments[0])
		require.True(t, field.IsValid() && field.Type() == reflect.TypeOf(json.RawMessage{}), "快照文档没有可选节 %s", segments[0])
		raw := field.Interface().(json.RawMessage)
		require.NotEmpty(t, raw, "上一版画像没有可选节 %s，补丁 %s 无法 add", segments[0], patch.Path)
		section := map[string]json.RawMessage{}
		require.NoError(t, json.Unmarshal(raw, &section))
		if existing, exists := section[segments[1]]; exists {
			require.JSONEq(t, string(patch.After), string(existing), "补丁 %s 的目标字段已存在且取值不同", patch.Path)
			continue
		}
		section[segments[1]] = patch.After
		encoded, err := json.Marshal(section)
		require.NoError(t, err)
		field.Set(reflect.ValueOf(json.RawMessage(encoded)))
	}
}

// withTargetProfileForWire 把 Active 槽位换成“上一版画像＋目标版本补丁”合成的目标画像，service 层与真实 Executor 同时生效。
func withTargetProfileForWire(t *testing.T, mode string) {
	t.Helper()
	require.Equal(t, officialClientProfileModeActive, normalizeOfficialClientProfileMode(mode), "合成目标画像只替换 Active 槽位")
	patches := loadTargetProfileRulePatches(t)
	withMutatedOfficialProfile(t, mode, func(doc *profilecontract.SnapshotDoc) {
		applyProfileRulePatches(t, doc, patches)
	})
	doc := replayTestProfileDocs[normalizeOfficialClientProfileMode(mode)]
	catalog, err := officialegress.DefaultReleaseCatalog().WithActiveProfileOverrideForTest(doc)
	require.NoError(t, err)
	previous := targetProfileWireCatalog
	targetProfileWireCatalog = &catalog
	t.Cleanup(func() { targetProfileWireCatalog = previous })

	// 合成结果必须带齐目标版本的七个开关（service 层与 Executor 层各验一次）。
	for label, optional := range map[string]profilecontract.OptionalSections{
		"service":  officialCodexOptionalSectionsForMode(mode),
		"executor": executorActiveOptional(t, catalog),
	} {
		require.NotNil(t, optional.WebSocketContinuation, label)
		require.True(t, optional.WebSocketContinuation.ReplayOnSameConnection, label)
		require.NotNil(t, optional.TurnState, label)
		require.True(t, optional.TurnState.ResetOnNewTurn, label)
		require.NotNil(t, optional.TurnMetadata, label)
		require.True(t, optional.TurnMetadata.WindowTracking && optional.TurnMetadata.WSCompactionRequests && optional.TurnMetadata.RootThreadKeys, label)
		require.NotNil(t, optional.ClientMetadata, label)
		require.Equal(t, []string{
			"root_turn_id", "ws_request_header_x_openai_internal_codex_responses_lite", "x-codex-ws-stream-request-start-ms",
		}, optional.ClientMetadata.DynamicKeys, label)
		require.NotNil(t, optional.ReasoningEffort, label)
		require.Equal(t, "http_first_turn", fmt.Sprint(optional.ReasoningEffort.ConfigurationUpdate), label)
	}
}

// targetProfileWireCatalog 非空时，newTargetAwareGate 建出的测试网关用它解析 Executor 的 Bundle（withTargetProfileForWire 设置并在清理时还原）。
var targetProfileWireCatalog *officialegress.ReleaseCatalog

// newTargetAwareGate 同 newCodexGateService；已经合成目标画像时，把 runtime 的 BundleResolver 换成按合成画像目录构造的版本。
// OfficialEgressTransitionRuntime.BundleResolver 是导出字段，Executor 的 Bundle 都经它解析（见 official_egress_*_invocation.go），
// 所以不需要改任何生产文件（Executor 构造函数所在文件被历史台账冻结，改一行就红）。
func newTargetAwareGate(t *testing.T, mode string, server *codexGateWireServer) *codexGateService {
	t.Helper()
	gate := newCodexGateService(t, mode, server)
	if targetProfileWireCatalog == nil {
		return gate
	}
	runtime := gate.service.officialEgress
	require.NotNil(t, runtime)
	resolver, err := officialegress.NewBundleResolver(*targetProfileWireCatalog, runtime.Guard.ProcessSinkCatalog())
	require.NoError(t, err)
	runtime.BundleResolver = resolver
	return gate
}

// forwardTargetAwareHTTPWire 同 codexGateForwardResponsesWire，但经 newTargetAwareGate 建网关（已合成目标画像时按它编译）。
func forwardTargetAwareHTTPWire(
	t *testing.T,
	mode string,
	server *codexGateWireServer,
	c *gin.Context,
	body []byte,
) codexGateWireRequest {
	t.Helper()
	before := len(server.httpRequestsForPath(codexGateResponsesPath))
	gate := newTargetAwareGate(t, mode, server)
	_, err := gate.forward(c, body)
	require.NoError(t, err, "%s 槽位的 Responses Forward 失败（握手错误 %v，上游错误 %v）",
		mode, server.handshakeErrors(), gate.upstream.failureList())
	requests := server.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, requests, before+1, "一次 Forward 应恰好产生一条 Responses 请求")
	return requests[len(requests)-1]
}

func executorActiveOptional(t *testing.T, catalog officialegress.ReleaseCatalog) profilecontract.OptionalSections {
	t.Helper()
	release, err := catalog.Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	return release.ExecutableProfile().Optional()
}

// 线上字节：官方 0.165 压缩续聊会话（c1）经真实 Executor 出站后，每个 WS 帧的 client_metadata 键集合等于官方实采——
// 预热帧 10 键、回合帧（含压缩帧与整段重放帧）11 键；root_turn_id 只在回合帧、等于该帧 turn_id；Lite 标记为 "true"；
// 发送时间戳是 13 位 Unix 毫秒。这是动态键（画像 ClientMetadata.DynamicKeys）的线上端到端断言。
func TestTargetProfileWSWireClientMetadataKeySetMatchesOfficial(t *testing.T) {
	mode := officialClientProfileModeActive
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if !section.UsesUUIDv7IDs() {
		t.Skip("槽位画像不是增量窗口方案，实采帧的前缀形态不适用")
	}
	session := loadCodex0165Session(t, "c1_ws_compact_continue")
	var steps []replayStep
	for index := 1; index <= 6; index++ {
		steps = append(steps, session.step(t, index))
	}
	withTargetProfileForWire(t, mode)
	handshakes, _ := replayRunSteps(t, mode, replay0165Manifest, steps)
	require.Len(t, handshakes, 1)
	frames := codexGateResponseCreateFrames(handshakes[0])
	require.Len(t, frames, 6)

	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		official := session.frames[index+1]
		prewarm := gjsonResultIsFalse(frame, "generate")
		wantKeys := replayObjectKeys(gjsonGet(official, "client_metadata"))
		gotKeys := replayObjectKeys(gjsonGet(frame, "client_metadata"))
		require.ElementsMatch(t, wantKeys, gotKeys, label+"：出站 client_metadata 键集合等于官方实采")
		if prewarm {
			require.Len(t, gotKeys, 10, label+"：预热帧 10 键")
			require.False(t, gjsonGet(frame, "client_metadata.root_turn_id").Exists(), label)
			continue
		}
		require.Len(t, gotKeys, 11, label+"：回合帧 11 键")
		turnID := gjsonGet(frame, "client_metadata.turn_id").String()
		require.NotEmpty(t, turnID, label)
		require.Equal(t, turnID, gjsonGet(frame, "client_metadata.root_turn_id").String(), label+"：根线程上 root_turn_id 等于 turn_id")
	}
	for index, frame := range frames {
		label := fmt.Sprintf("第 %d 帧", index+1)
		require.Equal(t, "true", gjsonGet(frame, "client_metadata.ws_request_header_x_openai_internal_codex_responses_lite").String(), label)
		require.Regexp(t, `^[0-9]{13}$`, gjsonGet(frame, "client_metadata.x-codex-ws-stream-request-start-ms").String(), label)
	}
}

func gjsonGet(raw []byte, path string) gjson.Result { return gjson.GetBytes(raw, path) }

func gjsonResultIsFalse(raw []byte, path string) bool {
	return gjson.GetBytes(raw, path).Type == gjson.False
}

// 线上字节：官方 HTTP 压缩续聊会话（c3）的每个 /codex/responses 请求的 client_metadata 都是同一组 9 个键（含 root_turn_id，
// 根线程上等于 turn_id）。同一类 HTTP 请求（含 HTTP 压缩请求）经真实 Executor 出站后，键集合与官方一致，root_turn_id 等于 turn_id。
func TestTargetProfileHTTPWireClientMetadataCarriesRootTurnID(t *testing.T) {
	mode := officialClientProfileModeActive
	samples := loadCodex0165HTTPRequests(t)
	officialKeys := map[string]bool{}
	for _, sample := range samples {
		keys := make([]string, 0, len(sample.ClientMetadata))
		for key := range sample.ClientMetadata {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		officialKeys[strings.Join(keys, ",")] = true
	}
	require.Len(t, officialKeys, 1, "官方实采的 7 个 HTTP 请求 client_metadata 键集合一致")
	var wantKeys []string
	for joined := range officialKeys {
		wantKeys = strings.Split(joined, ",")
	}
	require.Len(t, wantKeys, 9)
	require.Contains(t, wantKeys, "root_turn_id")

	cases := map[string]func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any){
		"普通回合请求": nil,
		"压缩请求": func(payload map[string]any, _ map[string]any, turnMetadata map[string]any) {
			input, _ := payload["input"].([]any)
			payload["input"] = append(input, map[string]any{"type": "compaction_trigger"})
			turnMetadata["request_kind"] = "compaction"
			turnMetadata["compaction"] = map[string]any{
				"trigger": "manual", "reason": "user_requested", "implementation": "responses_compaction_v2",
				"phase": "standalone_turn", "strategy": "memento",
			}
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			// 对照：目标画像之前（上一版画像）HTTP 请求没有 root_turn_id。
			legacyServer := startCodexGateWireServer(t, nil)
			legacyBody, legacyIngress := codexGateRootIngress(t, mutate)
			legacy := forwardTargetAwareHTTPWire(t, mode, legacyServer, legacyIngress, legacyBody)
			require.False(t, gjson.GetBytes(legacy.body, "client_metadata.root_turn_id").Exists(), "上一版画像不带 root_turn_id")

			withTargetProfileForWire(t, mode)
			server := startCodexGateWireServer(t, nil)
			body, ingress := codexGateRootIngress(t, mutate)
			wire := forwardTargetAwareHTTPWire(t, mode, server, ingress, body)
			metadata := gjson.GetBytes(wire.body, "client_metadata")
			require.ElementsMatch(t, wantKeys, replayObjectKeys(metadata), "出站 client_metadata 键集合等于官方实采 HTTP 请求")
			turnID := metadata.Get("turn_id").String()
			require.NotEmpty(t, turnID)
			require.Equal(t, turnID, metadata.Get("root_turn_id").String(), "根线程上 root_turn_id 等于 turn_id")
			require.False(t, metadata.Get("x-codex-ws-stream-request-start-ms").Exists(), "发送时间戳是 WS 专属")
			require.False(t, metadata.Get("ws_request_header_x_openai_internal_codex_responses_lite").Exists(), "Lite 在 HTTP 上是请求头")
		})
	}
}

// HTTP 回合请求 turn metadata 的窗口与根线程键（画像 WindowTracking／RootThreadKeys）。官方 HTTP 压缩续聊会话（c3）：
// 回合请求的 turn metadata 带 agent_name（/root）、window_number、context_window_id、root_turn_id（等于 turn_id），
// 压缩请求仍在窗口 0（同一上下文窗口 id），压缩后的请求窗口号 1 且上下文窗口 id 换新；窗口号就是 x-codex-window-id 的后缀。
// 与官方实采相比只差四个取决于本地配置的键（沙箱模式、自动审阅开关、node REPL 两个开关，网关刻意不写）。
func TestTargetProfileHTTPWireTurnMetadataWindowAndRootKeys(t *testing.T) {
	mode := officialClientProfileModeActive
	samples := loadCodex0165HTTPRequests(t)
	// 官方样本：请求 1 是窗口 0 的主线程回合请求，请求 5 是压缩请求，请求 6 是压缩后窗口 1 的回合请求。
	turnSample := replayHTTPSampleTurnMetadata(samples[0])
	compactionSample := replayHTTPSampleTurnMetadata(samples[4])
	afterSample := replayHTTPSampleTurnMetadata(samples[5])
	require.Equal(t, "turn", turnSample.Get("request_kind").String())
	require.Equal(t, "compaction", compactionSample.Get("request_kind").String())
	require.EqualValues(t, 0, turnSample.Get("window_number").Int())
	require.EqualValues(t, 0, compactionSample.Get("window_number").Int())
	require.EqualValues(t, 1, afterSample.Get("window_number").Int())
	require.Equal(t, turnSample.Get("context_window_id").String(), compactionSample.Get("context_window_id").String(), "官方：压缩请求仍在窗口 0")
	require.NotEqual(t, turnSample.Get("context_window_id").String(), afterSample.Get("context_window_id").String(), "官方：压缩后上下文窗口 id 换新")
	require.Equal(t, "/root", turnSample.Get("agent_name").String())
	require.Equal(t, turnSample.Get("turn_id").String(), turnSample.Get("root_turn_id").String())

	// forwardWire 经真实 Executor 发一次 HTTP 回合请求；windowSuffix 非空时把入站 x-codex-window-id 头的窗口号改成它。
	forwardWire := func(t *testing.T, windowSuffix string, mutate func(payload map[string]any, metadata map[string]any, turnMetadata map[string]any)) (turnMetadata gjson.Result, wire codexGateWireRequest) {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, mutate)
		if windowSuffix != "" {
			header := ingress.Request.Header.Get("x-codex-window-id")
			ingress.Request.Header.Set("x-codex-window-id", header[:strings.LastIndex(header, ":")+1]+windowSuffix)
		}
		wire = forwardTargetAwareHTTPWire(t, mode, server, ingress, body)
		raw := gjson.GetBytes(wire.body, `client_metadata.x-codex-turn-metadata`).String()
		require.NotEmpty(t, raw)
		require.Equal(t, raw, wire.header.Get("x-codex-turn-metadata"), "头与 client_metadata 里的 turn metadata 一致")
		return gjson.Parse(raw), wire
	}
	compaction := func(payload map[string]any, _ map[string]any, turnMetadata map[string]any) {
		input, _ := payload["input"].([]any)
		payload["input"] = append(input, map[string]any{"type": "compaction_trigger"})
		turnMetadata["request_kind"] = "compaction"
		turnMetadata["compaction"] = map[string]any{
			"trigger": "manual", "reason": "user_requested", "implementation": "responses_compaction_v2",
			"phase": "standalone_turn", "strategy": "memento",
		}
	}

	// 对照：上一版画像（没有这些开关）——四个键都不写，窗口号恒为 0，哪怕入站头是窗口 1。
	legacyTM, legacyWire := forwardWire(t, "1", nil)
	for _, key := range []string{"agent_name", "root_turn_id", "window_number", "context_window_id"} {
		require.False(t, legacyTM.Get(key).Exists(), "上一版画像不写 %s", key)
	}
	require.True(t, strings.HasSuffix(legacyTM.Get("window_id").String(), ":0"), "上一版画像窗口号恒为 0")
	require.True(t, strings.HasSuffix(legacyWire.header.Get("x-codex-window-id"), ":0"))

	withTargetProfileForWire(t, mode)

	// 窗口 0 的回合请求：键集合与键序等于官方（除四个取决于本地配置的键）。
	tm0, wire0 := forwardWire(t, "", nil)
	wantKeys := replaySubtract(replayObjectKeys(turnSample), replayConfigDependentTurnMetadataKeys...)
	require.Equal(t, wantKeys, replayObjectKeys(tm0), "turn metadata 的键集合与键序等于官方实采（除四个配置相关键）")
	require.Equal(t, "/root", tm0.Get("agent_name").String())
	require.Equal(t, tm0.Get("turn_id").String(), tm0.Get("root_turn_id").String(), "根线程上 root_turn_id 等于 turn_id")
	require.EqualValues(t, 0, tm0.Get("window_number").Int())
	require.True(t, strings.HasSuffix(tm0.Get("window_id").String(), ":0"))
	require.Regexp(t, replayUUIDPattern, tm0.Get("context_window_id").String())
	require.Equal(t, wire0.header.Get("x-codex-window-id"), tm0.Get("window_id").String(), "窗口头等于 turn metadata 的 window_id")
	require.Equal(t, tm0.Get("window_id").String(), gjson.GetBytes(wire0.body, `client_metadata.x-codex-window-id`).String())

	// 同一窗口内各请求上下文窗口 id 相同（含压缩请求），压缩请求带同样的四个键。
	tmAgain, _ := forwardWire(t, "0", nil)
	require.Equal(t, tm0.Get("context_window_id").String(), tmAgain.Get("context_window_id").String(), "同一窗口内上下文窗口 id 稳定")
	tmCompaction, _ := forwardWire(t, "0", compaction)
	require.Equal(t, "compaction", tmCompaction.Get("request_kind").String())
	require.Equal(t, tm0.Get("context_window_id").String(), tmCompaction.Get("context_window_id").String(), "压缩请求仍在窗口 0")
	require.EqualValues(t, 0, tmCompaction.Get("window_number").Int())
	require.Equal(t, "/root", tmCompaction.Get("agent_name").String())
	require.Equal(t, tmCompaction.Get("turn_id").String(), tmCompaction.Get("root_turn_id").String(), "压缩请求的 root_turn_id 同样等于 turn_id")
	require.Equal(t, []string{"agent_name", "window_number", "context_window_id", "root_turn_id"},
		replayKeysIn(replayObjectKeys(tmCompaction), "agent_name", "window_number", "context_window_id", "root_turn_id"),
		"四个键在压缩请求里的相对顺序与官方一致")
	require.Equal(t, replayKeysIn(replayObjectKeys(compactionSample), "agent_name", "window_number", "context_window_id", "root_turn_id"),
		replayKeysIn(replayObjectKeys(tmCompaction), "agent_name", "window_number", "context_window_id", "root_turn_id"))

	// 压缩之后（入站窗口头是窗口 1）：窗口号 1，窗口头、client_metadata、turn metadata 一致，上下文窗口 id 换新。
	tm1, wire1 := forwardWire(t, "1", nil)
	require.EqualValues(t, 1, tm1.Get("window_number").Int())
	require.True(t, strings.HasSuffix(tm1.Get("window_id").String(), ":1"))
	require.Equal(t, tm1.Get("window_id").String(), wire1.header.Get("x-codex-window-id"))
	require.Equal(t, tm1.Get("window_id").String(), gjson.GetBytes(wire1.body, `client_metadata.x-codex-window-id`).String())
	require.NotEqual(t, tm0.Get("context_window_id").String(), tm1.Get("context_window_id").String(), "压缩后上下文窗口 id 换新")
	require.Equal(t, strings.TrimSuffix(tm0.Get("window_id").String(), ":0"), strings.TrimSuffix(tm1.Get("window_id").String(), ":1"),
		"线程部分仍是网关派生的线程标识，只采窗口号")
	// 窗口 1 的回合请求键集合与键序同样等于官方压缩后的回合请求。
	require.Equal(t, replaySubtract(replayObjectKeys(afterSample), replayConfigDependentTurnMetadataKeys...), replayObjectKeys(tm1))
}

// replayHTTPSampleTurnMetadata 解析官方 HTTP 实采请求 client_metadata 里的 turn metadata。
func replayHTTPSampleTurnMetadata(sample replay0165HTTPRequest) gjson.Result {
	return gjson.Parse(sample.ClientMetadata["x-codex-turn-metadata"])
}

// replayKeysIn 按 keys 的出现先后顺序，返回 all 里属于 keys 的元素（保持 all 的顺序）。
func replayKeysIn(all []string, keys ...string) []string {
	var out []string
	for _, key := range all {
		if slices.Contains(keys, key) {
			out = append(out, key)
		}
	}
	return out
}
