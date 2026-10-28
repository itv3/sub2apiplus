package officialegress

import (
	"encoding/json"
	"strconv"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// WS 帧 client_metadata 的逐请求动态键（画像 ClientMetadata.DynamicKeys）：
//  1. 画像声明后，编译器按声明写入 root_turn_id（有帧事实才写）、Lite 标记（帧事实为真才写）与发送时间戳（编译该帧时的 Unix 毫秒）；
//  2. 没有声明（旧画像）时一律不写，client_metadata 只含身份键与常量；
//  3. 只声明部分键时只写这部分。

func clientMetadataDynamicSection(keys ...string) map[string]any {
	section := clientMetadataTargetSection()
	section["DynamicKeys"] = keys
	return section
}

func compileDynamicKeyFrame(
	t *testing.T,
	bundle ReleaseBundle,
	mutate func(facts *CodexIdentityFacts),
) map[string]string {
	t.Helper()
	frame := []byte(`{"type":"response.create","model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`)
	plan := syntheticPlanForEndpoint(t, bundle, "responses_ws")
	facts := executorInvocationIdentityFacts(t)
	if mutate != nil {
		mutate(&facts)
	}
	payload, _, err := compileWebSocketFrame(
		plan, NewReplayableRequestBody(frame), "response.create", facts, BodyRuntimeConditions{},
		bundle.release.ExecutableProfile().Features(), bundle.release.ExecutableProfile().Optional(),
	)
	if err != nil {
		t.Fatalf("编译 response.create 失败：%v", err)
	}
	fields := map[string]json.RawMessage{}
	if err := json.Unmarshal(payload, &fields); err != nil {
		t.Fatal(err)
	}
	return decodeClientMetadata(t, fields)
}

func TestWebSocketFrameClientMetadataDynamicKeysFollowProfile(t *testing.T) {
	const (
		rootKey  = "root_turn_id"
		liteKey  = "ws_request_header_x_openai_internal_codex_responses_lite"
		startKey = "x-codex-ws-stream-request-start-ms"
	)
	base := syntheticLegacyBundleForEndpoint(t, "responses_ws", "ClientMetadata 节",
		codexProfileDeclaresClientMetadata, stripClientMetadataSection)
	withFacts := func(facts *CodexIdentityFacts) {
		root, err := NewCodexIdentityValue("01a1838e-b9f3-7331-a343-6a8ced79f2fb", IdentitySourceTurn, IdentityLifecycleTurn)
		if err != nil {
			t.Fatal(err)
		}
		facts.RootTurnID = root
		facts.ResponsesLiteWS = true
	}

	// 旧画像：没有该节，一律不写。
	legacy := compileDynamicKeyFrame(t, base, withFacts)
	for _, key := range []string{rootKey, liteKey, startKey} {
		if _, exists := legacy[key]; exists {
			t.Fatalf("旧画像不得写入 %s", key)
		}
	}

	// 有常量节但没有声明动态键：同样不写（帧事实不会自己出站）。
	constantsOnly := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataTargetSection())
	})
	for _, key := range []string{rootKey, liteKey, startKey} {
		if _, exists := compileDynamicKeyFrame(t, constantsOnly, withFacts)[key]; exists {
			t.Fatalf("没有声明 DynamicKeys 时不得写入 %s", key)
		}
	}

	// 声明三个键。
	declared := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataDynamicSection(rootKey, liteKey, startKey))
	})
	before := time.Now().UnixMilli()
	got := compileDynamicKeyFrame(t, declared, withFacts)
	after := time.Now().UnixMilli()
	if got[rootKey] != "01a1838e-b9f3-7331-a343-6a8ced79f2fb" {
		t.Fatalf("root_turn_id 取自帧事实：%q", got[rootKey])
	}
	if got[liteKey] != "true" {
		t.Fatalf("Lite 标记取自帧事实：%q", got[liteKey])
	}
	stamp, err := strconv.ParseInt(got[startKey], 10, 64)
	if err != nil || stamp < before || stamp > after {
		t.Fatalf("发送时间戳应是编译该帧时的 Unix 毫秒：%q（%d～%d）", got[startKey], before, after)
	}
	// 常量与身份键照常。
	if got["mcp_attribution"] != clientMetadataTestMCPAttribution || got["guardian_credits_requested"] != "true" {
		t.Fatalf("常量不受影响：%v", got)
	}

	// 没有对应帧事实时：预热帧没有根回合、非 Lite 模型没有 Lite 标记，只有时间戳。
	bare := compileDynamicKeyFrame(t, declared, nil)
	if _, exists := bare[rootKey]; exists {
		t.Fatal("没有根回合事实时不得写 root_turn_id")
	}
	if _, exists := bare[liteKey]; exists {
		t.Fatal("非 Lite 帧不得写 Lite 标记")
	}
	if bare[startKey] == "" {
		t.Fatal("每个 WS 请求都带发送时间戳")
	}

	// 只声明一部分。
	partial := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataDynamicSection(rootKey))
	})
	partialGot := compileDynamicKeyFrame(t, partial, withFacts)
	if partialGot[rootKey] == "" {
		t.Fatal("声明的键要写")
	}
	for _, key := range []string{liteKey, startKey} {
		if _, exists := partialGot[key]; exists {
			t.Fatalf("没有声明的键不得写：%s", key)
		}
	}
}

// HTTP 回合请求的 client_metadata：官方 0.165 的 HTTP /responses 请求同样带 root_turn_id，但 Lite 在 HTTP 上是请求头、
// 发送时间戳是 WS 专属，所以画像声明的三个动态键里只有 root_turn_id 对 responses_http 生效；没有声明（旧画像）或没有帧事实时不写。
func TestHTTPClientMetadataRootTurnIDFollowsProfile(t *testing.T) {
	const (
		rootKey  = "root_turn_id"
		liteKey  = "ws_request_header_x_openai_internal_codex_responses_lite"
		startKey = "x-codex-ws-stream-request-start-ms"
		rootID   = "01a18395-e8ed-72f0-acf3-22d7c131055f"
	)
	base := syntheticLegacyBundleForEndpoint(t, "responses_http", "ClientMetadata 节",
		codexProfileDeclaresClientMetadata, stripClientMetadataSection)
	withFacts := func(facts *CodexIdentityFacts) {
		root, err := NewCodexIdentityValue(rootID, IdentitySourceTurn, IdentityLifecycleTurn)
		if err != nil {
			t.Fatal(err)
		}
		facts.RootTurnID = root
		// 即使帧事实里带着 WS 的 Lite 标记，HTTP 请求体也不写。
		facts.ResponsesLiteWS = true
	}
	compile := func(bundle ReleaseBundle, mutate func(*CodexIdentityFacts), invocation string) map[string]string {
		t.Helper()
		_, body := compilePromptCacheKeyTestRequest(t, bundle, mutate, invocation)
		return decodeClientMetadata(t, body)
	}

	// 旧画像：没有该节，一律不写。
	for _, key := range []string{rootKey, liteKey, startKey} {
		if _, exists := compile(base, withFacts, "http-root-legacy")[key]; exists {
			t.Fatalf("旧画像不得写入 %s", key)
		}
	}
	// 有常量节但没有声明动态键：不写。
	constantsOnly := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataTargetSection())
	})
	if _, exists := compile(constantsOnly, withFacts, "http-root-constants")[rootKey]; exists {
		t.Fatal("没有声明 DynamicKeys 时不得写入 root_turn_id")
	}

	// 声明三个键：HTTP 只写 root_turn_id。
	declared := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataDynamicSection(rootKey, liteKey, startKey))
	})
	got := compile(declared, withFacts, "http-root-declared")
	if got[rootKey] != rootID {
		t.Fatalf("root_turn_id 取自帧事实：%q", got[rootKey])
	}
	for _, key := range []string{liteKey, startKey} {
		if _, exists := got[key]; exists {
			t.Fatalf("%s 只属于 WS 帧，HTTP 请求体不得写", key)
		}
	}
	if got["mcp_attribution"] != clientMetadataTestMCPAttribution || got["guardian_credits_requested"] != "true" {
		t.Fatalf("常量不受影响：%v", got)
	}
	// 没有根回合事实（子代理线程等）：不写。
	if _, exists := compile(declared, nil, "http-root-no-fact")[rootKey]; exists {
		t.Fatal("没有根回合事实时不得写 root_turn_id")
	}
}
