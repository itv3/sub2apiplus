//go:build unit

package service

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 独立 images 端点的回合头事实（x-codex-image-turn-id、x-codex-turn-metadata）：
// 只在当前 release 画像为该端点声明了对应槽位时才生成事实；旧画像一个字节都不变。
// 最终出站头由 officialegress 包的合成画像编译用例覆盖。

// withImagesTurnHeaderSlots 以正式目录 active 画像为底稿，给 images 两个端点按需声明回合头槽位，
// 并临时替换 service 读取画像的包级入口。用例结束自动恢复；使用本夹具的用例不得并行执行。
func withImagesTurnHeaderSlots(t *testing.T, imageTurnID, turnMetadata bool) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		endpoint := syntheticServiceSnapshotEndpoint(t, &doc, endpointID)
		kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers)+2)
		for _, slot := range endpoint.Headers {
			if slot.Name == officialCodexImageTurnIDHeader || slot.Name == officialCodexTurnMetadataHeader {
				continue
			}
			kept = append(kept, slot)
		}
		// 官方实采顺序：version(10)、x-codex-image-turn-id、x-codex-turn-metadata、authorization(30)……
		add := func(slotNumber int, name string) {
			kept = append(kept, profilecontract.SnapshotHeaderSlot{
				Slot: slotNumber, Name: name, WireName: name,
				Source: string(profilecontract.SourceTurn), Condition: string(profilecontract.ConditionAlways),
			})
		}
		if imageTurnID {
			add(20, officialCodexImageTurnIDHeader)
		}
		if turnMetadata {
			add(25, officialCodexTurnMetadataHeader)
		}
		endpoint.Headers = kept
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	executable, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	previous := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(string) (profilecontract.ExecutableProfile, error) {
		return executable, nil
	}
	t.Cleanup(func() { officialCodexExecutableProfileForMode = previous })
	return executable
}

func completeImagesTurnFactsForTest(
	t *testing.T,
	endpointID string,
	profileMode string,
	prefill func(*officialegress.CodexIdentityFacts),
) officialegress.CodexIdentityFacts {
	t.Helper()
	facts := officialegress.CodexIdentityFacts{}
	if prefill != nil {
		prefill(&facts)
	}
	require.NoError(t, completeOfficialCodexGeneratedIdentityFacts(
		&facts, endpointID, "images-seed", officialCodexFallbackTurnMetadataInput{profileMode: profileMode},
	))
	return facts
}

func TestImagesTurnFactsLegacyProfileGeneratesNothing(t *testing.T) {
	// 正式目录里没有为 images 声明回合头槽位的画像（晋升 0.164 之前）：事实保持为空。
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	imageTurnID, turnMetadata, _ := officialCodexImagesHeaderDeclarations(
		officialClientProfileModeActive, officialCodexEndpointImagesGenerations,
	)
	if imageTurnID || turnMetadata {
		t.Skipf("active 画像（%s）已声明 images 回合头槽位，旧画像对照由下方合成画像用例覆盖", release.Version())
	}
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, nil)
		require.Empty(t, facts.TurnID.Value)
		require.Empty(t, facts.TurnMetadata.Value)
	}
}

func TestImagesTurnFactsSyntheticLegacyProfileGeneratesNothing(t *testing.T) {
	withImagesTurnHeaderSlots(t, false, false)
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, nil)
		require.Empty(t, facts.TurnID.Value, "画像未声明槽位时不得多生成身份事实")
		require.Empty(t, facts.TurnMetadata.Value)
		require.Empty(t, facts.SessionID.Value)
		require.Empty(t, facts.ThreadID.Value)
	}
}

// withFixedImagesTurnStart 固定回合开始时间，用例结束自动恢复。
func withFixedImagesTurnStart(t *testing.T, ms int64) {
	t.Helper()
	previous := officialCodexImagesTurnStartedAtMs
	officialCodexImagesTurnStartedAtMs = func() int64 { return ms }
	t.Cleanup(func() { officialCodexImagesTurnStartedAtMs = previous })
}

func withExecSurface(facts *officialegress.CodexIdentityFacts) {
	facts.ProcessSurface = officialegress.CodexIdentityValue{
		Value: officialCodexSurfaceExec, Source: officialegress.IdentitySourceProcess,
		Lifecycle: officialegress.IdentityLifecycleProcess,
	}
}

func TestImagesTurnFactsFollowDeclaredSlots(t *testing.T) {
	profile := withImagesTurnHeaderSlots(t, true, true)
	withFixedImagesTurnStart(t, 1793036419096)
	turnID := officialCodexGeneratedUUIDLikeValue("images-seed", "turn")
	sessionID := officialCodexGeneratedUUIDLikeValue("images-seed", "session")
	for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, withExecSurface)
		require.Equal(t, turnID, facts.TurnID.Value)
		require.Equal(t, officialegress.IdentitySourceInvocation, facts.TurnID.Source)
		require.Equal(t, officialegress.IdentityLifecycleTurn, facts.TurnID.Lifecycle)
		// 入站没有回合事实时，从 Responses 兜底的基础键值出发，按官方 swap_remove 过程重排。
		// 这里没有 model／reasoning_effort（没有可信来源，不写）：初始序
		// [session_id, thread_id, agent_name, turn_id, root_turn_id, thread_source, turn_trigger, sandbox,
		//  sandbox_mode, 三个布尔, turn_started_at_unix_ms]，删 agent_name 时末尾的 turn_started_at_unix_ms
		// 换到第 3 位，删 root_turn_id 时末尾的 node_repl_disabled 换到第 5 位。
		require.Equal(t,
			`{"session_id":"`+sessionID+`","thread_id":"`+sessionID+`","turn_started_at_unix_ms":1793036419096,`+
				`"turn_id":"`+turnID+`","node_repl_disabled":false,"thread_source":"user","turn_trigger":"exec",`+
				`"sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,`+
				`"node_repl_auto_review_required":false,"codex_version":"`+profile.Version()+`"}`,
			facts.TurnMetadata.Value)
		require.NoError(t, facts.Validate())
		// 生成的会话标识只进入 turn metadata，不写进会话事实，避免影响 Conditions。
		require.Empty(t, facts.SessionID.Value)
		require.Empty(t, facts.ThreadID.Value)
	}
}

func TestImagesTurnFactsSynthesizedWithoutTurnTriggerWhenSurfaceUnknown(t *testing.T) {
	withImagesTurnHeaderSlots(t, false, true)
	withFixedImagesTurnStart(t, 1)
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, officialClientProfileModeActive, nil)
	require.NotContains(t, facts.TurnMetadata.Value, "turn_trigger", "入口无法确定时不写 turn_trigger")
	require.Contains(t, facts.TurnMetadata.Value, `"turn_id":"`+facts.TurnID.Value+`"`)
}

func TestImagesTurnFactsSlotsAreIndependent(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, false)
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, officialClientProfileModeActive, nil)
	require.NotEmpty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value, "未声明 x-codex-turn-metadata 槽位时不生成 turn metadata")

	withImagesTurnHeaderSlots(t, false, true)
	facts = completeImagesTurnFactsForTest(t, officialCodexEndpointImagesEdits, officialClientProfileModeActive, nil)
	require.NotEmpty(t, facts.TurnMetadata.Value)
	// turn metadata 里的 turn_id 总要有来源，所以即使只声明 metadata 槽位也会生成 TurnID 事实。
	require.Contains(t, facts.TurnMetadata.Value, `"turn_id":"`+facts.TurnID.Value+`"`)
}

// 入站已带同一回合的 Responses 形态 turn metadata 时：取其中的共有键与取值，转成 images 形态，
// turn_id 与 x-codex-image-turn-id 同源。期望值逐字取自官方实采。
func TestImagesTurnFactsConvertInboundResponsesTurnMetadata(t *testing.T) {
	profile := withImagesTurnHeaderSlots(t, true, true)
	for _, sample := range officialImagesTurnMetadataSamples {
		prefill := func(facts *officialegress.CodexIdentityFacts) {
			var err error
			facts.TurnMetadata, err = officialegress.NewCodexIdentityValue(
				sample.responses, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
			)
			require.NoError(t, err)
		}
		for _, endpointID := range []string{officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
			facts := completeImagesTurnFactsForTest(t, endpointID, officialClientProfileModeActive, prefill)
			expected := strings.Replace(sample.image, `"codex_version":"0.164.0"`, `"codex_version":"`+profile.Version()+`"`, 1)
			require.Equal(t, expected, facts.TurnMetadata.Value, sample.name)
			var turn struct {
				TurnID string `json:"turn_id"`
			}
			require.NoError(t, json.Unmarshal([]byte(facts.TurnMetadata.Value), &turn))
			require.Equal(t, turn.TurnID, facts.TurnID.Value, "%s：x-codex-image-turn-id 必须与 turn_id 同源", sample.name)
		}
	}
}

func TestImagesTurnFactsKeepStructuredTurnID(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, true)
	preset := func(facts *officialegress.CodexIdentityFacts) {
		var err error
		facts.TurnID, err = officialegress.NewCodexIdentityValue(
			"given-turn", officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
		)
		require.NoError(t, err)
	}
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, officialClientProfileModeActive, preset)
	require.Equal(t, "given-turn", facts.TurnID.Value)
	require.Contains(t, facts.TurnMetadata.Value, `"turn_id":"given-turn"`)
}

func TestImagesTurnMetadataMatchesCapturedSamples(t *testing.T) {
	require.NotEmpty(t, officialImagesTurnMetadataSamples)
	for _, sample := range officialImagesTurnMetadataSamples {
		got, err := officialImagesTurnMetadataFromResponses(sample.responses, "0.164.0")
		require.NoError(t, err, sample.name)
		require.Equal(t, sample.image, got, sample.name)
		var decoded map[string]json.RawMessage
		require.NoError(t, json.Unmarshal([]byte(got), &decoded))
		require.Len(t, decoded, sample.keyCount, sample.name)
	}
	// 覆盖面：默认路径 14 键、缺 thread_source／turn_trigger 的 12 键、带产品附加元数据与 workspaces 的 15／17 键。
	counts := map[int]bool{}
	for _, sample := range officialImagesTurnMetadataSamples {
		counts[sample.keyCount] = true
	}
	for _, want := range []int{12, 14, 15, 17} {
		require.True(t, counts[want], "实采样本缺少 %d 键的形态", want)
	}
}

func TestImagesTurnMetadataKeyOrderFollowsSwapRemove(t *testing.T) {
	full := []string{
		"session_id", "thread_id", "turn_id", "thread_source", "turn_trigger", "sandbox", "sandbox_mode",
		"auto_review_enabled", "node_repl_auto_review_required", "node_repl_disabled", "turn_started_at_unix_ms",
		"model", "reasoning_effort",
	}
	// 默认路径（实采 14 键序）。
	require.Equal(t, []string{
		"session_id", "thread_id", "reasoning_effort", "turn_id", "model", "thread_source", "turn_trigger",
		"sandbox", "sandbox_mode", "auto_review_enabled", "node_repl_auto_review_required", "node_repl_disabled",
		"turn_started_at_unix_ms", "codex_version",
	}, orderOfficialImagesTurnMetadataKeys(full, false))

	// reasoning_effort 缺失（没有生效强度）：extra 里只剩 model，换位结果不同（键序模拟器 keyorder_sim.py 的输出）。
	withoutEffort := full[:len(full)-1]
	require.Equal(t, []string{
		"session_id", "thread_id", "model", "turn_id", "turn_started_at_unix_ms", "thread_source", "turn_trigger",
		"sandbox", "sandbox_mode", "auto_review_enabled", "node_repl_auto_review_required", "node_repl_disabled",
		"codex_version",
	}, orderOfficialImagesTurnMetadataKeys(withoutEffort, false))

	// 触发过 request_user_input 的回合：末尾追加 user_input_requested_during_turn。
	require.Equal(t, "user_input_requested_during_turn",
		orderOfficialImagesTurnMetadataKeys(full, true)[len(full)+1])
}

func TestImagesTurnMetadataConditionalKeysPresence(t *testing.T) {
	base := `{"session_id":"s","thread_id":"s","agent_name":"/root","turn_id":"t","root_turn_id":"t",` +
		`"request_kind":"turn","installation_id":"i","window_id":"w","sandbox":"seccomp","model":"m"}`
	got, err := officialImagesTurnMetadataFromResponses(base, "0.164.0")
	require.NoError(t, err)
	// 无 thread_source／turn_trigger／reasoning_effort／workspaces；Responses 专有键与 agent_name、root_turn_id 都不出站。
	require.Equal(t, `{"session_id":"s","thread_id":"s","model":"m","turn_id":"t","sandbox":"seccomp","codex_version":"0.164.0"}`, got)

	// 产品附加元数据与 reasoning_effort 一起并入 extra，按键名字典序（display_name, model, reasoning_effort, source），再经两次换位删除得到下面的序。
	withExtras := `{"session_id":"s","thread_id":"s","turn_id":"t","sandbox":"seccomp","source":"x","model":"m",` +
		`"reasoning_effort":"low","display_name":"d"}`
	got, err = officialImagesTurnMetadataFromResponses(withExtras, "0.164.0")
	require.NoError(t, err)
	var keys []string
	dec := json.NewDecoder(strings.NewReader(got))
	_, _ = dec.Token()
	for dec.More() {
		tok, _ := dec.Token()
		keys = append(keys, tok.(string))
		var skip json.RawMessage
		require.NoError(t, dec.Decode(&skip))
	}
	require.Equal(t, []string{
		"session_id", "thread_id", "source", "turn_id", "reasoning_effort", "sandbox", "display_name", "model",
		"codex_version",
	}, keys)

	// 必需键缺失：失败关闭。
	_, err = officialImagesTurnMetadataFromResponses(`{"session_id":"s","thread_id":"s"}`, "0.164.0")
	require.Error(t, err)
	_, err = officialImagesTurnMetadataFromResponses(`not json`, "0.164.0")
	require.Error(t, err)
}

func TestImagesTurnMetadataIsASCII(t *testing.T) {
	input := `{"session_id":"s","thread_id":"s","turn_id":"t","workspaces":{"/tmp/東京😀":{"has_changes":false}}}`
	got, err := officialImagesTurnMetadataFromResponses(input, "0.164.0")
	require.NoError(t, err)
	for _, b := range []byte(got) {
		require.Less(t, b, byte(0x80), "官方 to_ascii_json_string：非 ASCII 必须转义：%s", got)
	}
	require.Contains(t, got, `\u6771\u4eac\ud83d\ude00`)
	var decoded struct {
		Workspaces map[string]json.RawMessage `json:"workspaces"`
	}
	require.NoError(t, json.Unmarshal([]byte(got), &decoded))
	require.Contains(t, decoded.Workspaces, "/tmp/東京😀")
}

func TestImagesTurnFactsNeedFrozenProfileMode(t *testing.T) {
	// 调用没有冻结 release mode 时不读画像，保持旧逻辑，不默认退化为 active。
	withImagesTurnHeaderSlots(t, true, true)
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointImagesGenerations, "", nil)
	require.Empty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value)
}

func TestImagesTurnFactsDoNotTouchOtherEndpoints(t *testing.T) {
	withImagesTurnHeaderSlots(t, true, true)
	// 非 images、非 Responses 端点（例如 models）即使画像里别处声明了槽位也不受影响。
	facts := completeImagesTurnFactsForTest(t, officialCodexEndpointModels, officialClientProfileModeActive, nil)
	require.Empty(t, facts.TurnID.Value)
	require.Empty(t, facts.TurnMetadata.Value)
}
