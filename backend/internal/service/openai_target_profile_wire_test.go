package service

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 目标版本画像的合成与线上字节断言。
//
// 目标版本（0.165）的可执行画像要到版本登记的画像生成步骤才入库。在那之前，用“上一版画像＋规则补丁清单”合成目标画像：
// 补丁清单是 tools/official_client_capture/profile_rule_patches_0_165_0.json（登记产物，七条 add 补丁），用画像契约包
// NewProfileSpec → CompileExecutableProfile 解码编译（与登记阶段验证补丁的方法相同）。合成后同时替换两处：
//   - service 层读到的画像（withMutatedOfficialProfile，service 逻辑按画像门控的开关）；
//   - Executor runtime 解析 Bundle 用的发布目录（officialEgressRuntimeReleaseCatalog），让真实 Executor/Compiler 按合成画像编译线上字节。
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
	previous := officialEgressRuntimeReleaseCatalog
	officialEgressRuntimeReleaseCatalog = func() officialegress.ReleaseCatalog { return catalog }
	t.Cleanup(func() { officialEgressRuntimeReleaseCatalog = previous })

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
			legacy := codexGateForwardResponsesWire(t, mode, legacyServer, legacyIngress, legacyBody, nil)
			require.False(t, gjson.GetBytes(legacy.body, "client_metadata.root_turn_id").Exists(), "上一版画像不带 root_turn_id")

			withTargetProfileForWire(t, mode)
			server := startCodexGateWireServer(t, nil)
			body, ingress := codexGateRootIngress(t, mutate)
			wire := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
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
