package officialegress

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// prompt_cache_key 来源（VC-4 第 2 项）：
//  1. 画像把 session-id 来源声明为 prompt_cache_key 时，根会话的 session-id 头与请求体
//     prompt_cache_key 同取 PromptCacheKey 事实（临时 fork 为源会话键）；子代理仍取
//     真实 SessionID；缺少事实时回退按 SessionID 推导；
//  2. 旧画像（未声明该来源）忽略 PromptCacheKey 事实，出站字节与不带该事实时完全相同；
//  3. PromptCacheKey 事实缺席时身份摘要与旧结构相同，出现时进入摘要与 WS 帧身份校验。

const promptCacheKeyTestForkSource = "019f9577-d69f-7892-809e-8a3a4198c6aa"

func promptCacheKeyTestValue(t *testing.T, raw string, source IdentityFactSource, lifecycle IdentityFactLifecycle) CodexIdentityValue {
	t.Helper()
	value, err := NewCodexIdentityValue(raw, source, lifecycle)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func compilePromptCacheKeyTestRequest(
	t *testing.T,
	bundle ReleaseBundle,
	mutateFacts func(*CodexIdentityFacts),
	invocationID string,
) (map[string]string, map[string]json.RawMessage) {
	t.Helper()
	plan := syntheticPlanForEndpoint(t, bundle, "responses_http")
	target := staticClosureLegalTarget(plan.template)
	egressPlan := staticClosureEgressPlan(t, bundle, plan, target, invocationID)
	if mutateFacts != nil {
		mutateFacts(&egressPlan.IdentityFacts)
	}
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译 responses_http 失败：%v", err)
	}
	headers := map[string]string{}
	for name := range execution.request.Headers() {
		headers[strings.ToLower(name)] = execution.request.Headers().Get(name)
	}
	body, ok := execution.request.body.replayableView()
	if !ok {
		t.Fatal("编译产物缺少可重放 Body")
	}
	fields := map[string]json.RawMessage{}
	if err := json.Unmarshal(body, &fields); err != nil {
		t.Fatalf("解析编译后 Body 失败：%v", err)
	}
	return headers, fields
}

func jsonStringField(t *testing.T, fields map[string]json.RawMessage, name string) string {
	t.Helper()
	raw, ok := fields[name]
	if !ok {
		t.Fatalf("Body 缺少字段 %s", name)
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatalf("Body 字段 %s 不是字符串：%s", name, raw)
	}
	return value
}

func withForkPromptCacheKey(t *testing.T) func(*CodexIdentityFacts) {
	return func(facts *CodexIdentityFacts) {
		facts.PromptCacheKey = promptCacheKeyTestValue(
			t, promptCacheKeyTestForkSource, IdentitySourceInvocation, IdentityLifecycleSession,
		)
	}
}

func TestCompilerPromptCacheKeySourceFollowsProfile(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")

	// 旧画像：PromptCacheKey 事实不改变任何出站字节。
	plainHeaders, plainBody := compilePromptCacheKeyTestRequest(t, base, nil, "pck-legacy-plain")
	forkHeaders, forkBody := compilePromptCacheKeyTestRequest(t, base, withForkPromptCacheKey(t), "pck-legacy-fork")
	if plainHeaders["session-id"] != "synthetic-session" || forkHeaders["session-id"] != "synthetic-session" {
		t.Fatalf("旧画像 session-id 必须取 SessionID：%q / %q", plainHeaders["session-id"], forkHeaders["session-id"])
	}
	if jsonStringField(t, forkBody, "prompt_cache_key") != "synthetic-session" {
		t.Fatal("旧画像的 prompt_cache_key 不得消费 PromptCacheKey 事实")
	}
	if len(plainHeaders) != len(forkHeaders) {
		t.Fatalf("旧画像 Header 集合因 PromptCacheKey 事实变化：%v / %v", plainHeaders, forkHeaders)
	}
	for name, value := range plainHeaders {
		if forkHeaders[name] != value {
			t.Fatalf("旧画像 Header %s 因 PromptCacheKey 事实变化：%q / %q", name, value, forkHeaders[name])
		}
	}
	for name, value := range plainBody {
		if string(forkBody[name]) != string(value) {
			t.Fatalf("旧画像 Body 字段 %s 因 PromptCacheKey 事实变化", name)
		}
	}

	// 目标画像：session-id 来源改为 prompt_cache_key。
	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		syntheticSetHeaderSource(t, doc, "responses_http", "session-id", profilecontract.SourcePromptCacheKey)
	})
	rootHeaders, rootBody := compilePromptCacheKeyTestRequest(t, synthetic, withForkPromptCacheKey(t), "pck-target-fork")
	if rootHeaders["session-id"] != promptCacheKeyTestForkSource {
		t.Fatalf("根会话 fork 的 session-id 必须取源会话键：%q", rootHeaders["session-id"])
	}
	if rootHeaders["thread-id"] != "synthetic-thread" {
		t.Fatalf("thread-id 仍是本线程身份：%q", rootHeaders["thread-id"])
	}
	if jsonStringField(t, rootBody, "prompt_cache_key") != promptCacheKeyTestForkSource {
		t.Fatal("请求体 prompt_cache_key 必须与 session-id 头同源")
	}
	var metadata map[string]string
	if err := json.Unmarshal(rootBody["client_metadata"], &metadata); err != nil {
		t.Fatal(err)
	}
	if metadata["session_id"] != "synthetic-session" {
		t.Fatalf("client_metadata.session_id 仍是本会话身份：%q", metadata["session_id"])
	}

	subagentHeaders, subagentBody := compilePromptCacheKeyTestRequest(t, synthetic, func(facts *CodexIdentityFacts) {
		withForkPromptCacheKey(t)(facts)
		facts.Subagent = promptCacheKeyTestValue(t, "collab_spawn", IdentitySourceTurn, IdentityLifecycleTurn)
		facts.Conditions.SubagentPresent = true
	}, "pck-target-subagent")
	if subagentHeaders["session-id"] != "synthetic-session" {
		t.Fatalf("子代理 session-id 必须取真实 SessionID：%q", subagentHeaders["session-id"])
	}
	if jsonStringField(t, subagentBody, "prompt_cache_key") != promptCacheKeyTestForkSource {
		t.Fatal("子代理的 prompt_cache_key 仍取 PromptCacheKey 事实")
	}

	fallbackHeaders, fallbackBody := compilePromptCacheKeyTestRequest(t, synthetic, nil, "pck-target-fallback")
	if fallbackHeaders["session-id"] != "synthetic-session" ||
		jsonStringField(t, fallbackBody, "prompt_cache_key") != "synthetic-session" {
		t.Fatalf("缺少 PromptCacheKey 事实时回退按 SessionID 推导：%q", fallbackHeaders["session-id"])
	}
}

func TestCodexIdentityFactsPromptCacheKeyDigestAndFrameIdentity(t *testing.T) {
	facts := executorInvocationIdentityFacts(t)
	raw, err := json.Marshal(facts)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "PromptCacheKey") {
		t.Fatalf("PromptCacheKey 缺席时不得出现在序列化形态中：%s", raw)
	}
	withKey := facts
	withForkPromptCacheKey(t)(&withKey)
	if err := withKey.Validate(); err != nil {
		t.Fatalf("带 PromptCacheKey 的身份事实应合法：%v", err)
	}
	if facts.Digest() == withKey.Digest() || facts.invocationDigest() == withKey.invocationDigest() {
		t.Fatal("PromptCacheKey 必须进入身份摘要与 invocation 摘要")
	}
	if err := validateWebSocketFrameIdentity(withKey, withKey); err != nil {
		t.Fatalf("同一 PromptCacheKey 的帧应通过：%v", err)
	}
	if err := validateWebSocketFrameIdentity(withKey, facts); err == nil ||
		!strings.Contains(err.Error(), "prompt-cache-key") {
		t.Fatalf("帧改写 PromptCacheKey 必须被拒绝：%v", err)
	}
}
