package officialegress

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// body 条件与 client_metadata 常量（VC-4 第 3 项）：
//  1. 画像 ClientMetadata 节按条件把客户端固定常量追加到 client_metadata（HTTP 请求体与
//     WS response.create 两处），KeyConditions 覆盖节级条件；
//  2. guardian 审阅请求按画像条件省略 service_tier，而不是把下游带来的字段当成错误拒绝；
//  3. 旧画像没有该节、也不引用 guardian 条件，client_metadata 与 service_tier 不受影响。

const clientMetadataTestMCPAttribution = `{"status":"none"}`

// clientMetadataTargetSection 与目标画像草稿一致：guardian_credits_requested 只对非审阅
// 请求写入；mcp_attribution 对所有请求写入（按键覆盖为 always）。
func clientMetadataTargetSection() map[string]any {
	return map[string]any{
		"Condition": "not_guardian_review_request",
		"Constants": map[string]string{
			"guardian_credits_requested": "true",
			"mcp_attribution":            clientMetadataTestMCPAttribution,
		},
		"KeyConditions": map[string]string{"mcp_attribution": "always"},
	}
}

func compileClientMetadataTestHTTP(
	t *testing.T,
	bundle ReleaseBundle,
	body []byte,
	guardian bool,
	invocationID string,
) map[string]json.RawMessage {
	t.Helper()
	plan := syntheticPlanForEndpoint(t, bundle, "responses_http")
	target := staticClosureLegalTarget(plan.template)
	egressPlan := staticClosureEgressPlan(t, bundle, plan, target, invocationID)
	egressPlan.IdentityFacts.Conditions.GuardianReviewRequest = guardian
	if body != nil {
		egressPlan.Body = NewReplayableRequestBody(body)
		hint, err := ParseOfficialCodexRoutingHintFacts("responses_http", body)
		if err != nil {
			t.Fatal(err)
		}
		egressPlan.RoutingHint = hint
	}
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译 responses_http 失败（guardian=%t）：%v", guardian, err)
	}
	raw, ok := execution.request.body.replayableView()
	if !ok {
		t.Fatal("编译产物缺少可重放 Body")
	}
	fields := map[string]json.RawMessage{}
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	return fields
}

func decodeClientMetadata(t *testing.T, fields map[string]json.RawMessage) map[string]string {
	t.Helper()
	metadata := map[string]string{}
	if err := json.Unmarshal(fields["client_metadata"], &metadata); err != nil {
		t.Fatalf("client_metadata 不是字符串映射：%s", fields["client_metadata"])
	}
	return metadata
}

func TestCompilerClientMetadataConstantsFollowProfile(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")

	legacyNormal := decodeClientMetadata(t, compileClientMetadataTestHTTP(t, base, nil, false, "cm-legacy-normal"))
	legacyReview := decodeClientMetadata(t, compileClientMetadataTestHTTP(t, base, nil, true, "cm-legacy-review"))
	for _, metadata := range []map[string]string{legacyNormal, legacyReview} {
		if _, exists := metadata["guardian_credits_requested"]; exists {
			t.Fatal("旧画像不得写入 guardian_credits_requested")
		}
		if _, exists := metadata["mcp_attribution"]; exists {
			t.Fatal("旧画像不得写入 mcp_attribution")
		}
	}
	if len(legacyNormal) != len(legacyReview) {
		t.Fatalf("旧画像的 client_metadata 受到 guardian 条件影响：%v / %v", legacyNormal, legacyReview)
	}

	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataTargetSection())
	})
	normalFields := compileClientMetadataTestHTTP(t, synthetic, nil, false, "cm-target-normal")
	normal := decodeClientMetadata(t, normalFields)
	if normal["guardian_credits_requested"] != "true" || normal["mcp_attribution"] != clientMetadataTestMCPAttribution {
		t.Fatalf("普通请求应写入两个常量：%v", normal)
	}
	if !strings.Contains(string(normalFields["client_metadata"]), `"mcp_attribution":"{\"status\":\"none\"}"`) {
		t.Fatalf("mcp_attribution 必须以 JSON 字符串形态出站：%s", normalFields["client_metadata"])
	}
	for key, value := range legacyNormal {
		if normal[key] != value {
			t.Fatalf("身份事实键 %s 不应因常量节变化：%q / %q", key, normal[key], value)
		}
	}
	review := decodeClientMetadata(t, compileClientMetadataTestHTTP(t, synthetic, nil, true, "cm-target-review"))
	if _, exists := review["guardian_credits_requested"]; exists {
		t.Fatal("guardian 审阅请求不得写入 guardian_credits_requested")
	}
	if review["mcp_attribution"] != clientMetadataTestMCPAttribution {
		t.Fatal("按键覆盖为 always 的 mcp_attribution 对审阅请求同样写入")
	}
}

func TestCompilerRejectsClientMetadataConstantCollidingWithIdentity(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, map[string]any{
			"Condition": "always", "Constants": map[string]string{"session_id": "forged"},
		})
	})
	plan := syntheticPlanForEndpoint(t, synthetic, "responses_http")
	egressPlan := staticClosureEgressPlan(t, synthetic, plan, staticClosureLegalTarget(plan.template), "cm-collision")
	if _, err := NewCompiler().Compile(context.Background(), synthetic, egressPlan, EndpointDynamicInputs{}); err == nil ||
		!strings.Contains(err.Error(), "常量键与身份事实冲突") {
		t.Fatalf("常量键覆盖身份事实必须失败关闭：%v", err)
	}
}

func TestWebSocketFrameClientMetadataConstantsFollowProfile(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_ws")
	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		doc.ClientMetadata = syntheticRawSection(t, clientMetadataTargetSection())
	})
	frame := []byte(`{"type":"response.create","model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`)
	compile := func(bundle ReleaseBundle, guardian bool) map[string]string {
		t.Helper()
		plan := syntheticPlanForEndpoint(t, bundle, "responses_ws")
		facts := executorInvocationIdentityFacts(t)
		facts.Conditions.GuardianReviewRequest = guardian
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
	legacy := compile(base, false)
	if _, exists := legacy["mcp_attribution"]; exists {
		t.Fatal("旧画像的 response.create 不得写入常量")
	}
	normal := compile(synthetic, false)
	if normal["guardian_credits_requested"] != "true" || normal["mcp_attribution"] != clientMetadataTestMCPAttribution {
		t.Fatalf("response.create 应写入两个常量：%v", normal)
	}
	review := compile(synthetic, true)
	if _, exists := review["guardian_credits_requested"]; exists || review["mcp_attribution"] == "" {
		t.Fatalf("审阅请求的 response.create 常量不符：%v", review)
	}
}

func TestCompilerOmitsGuardianConditionedServiceTier(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	body := []byte(`{"model":"gpt-test","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[],"service_tier":"priority"}`)

	// 旧画像：service_tier 无条件，审阅与否都保留。
	for _, guardian := range []bool{false, true} {
		fields := compileClientMetadataTestHTTP(t, base, body, guardian, "tier-legacy")
		if string(fields["service_tier"]) != `"priority"` {
			t.Fatalf("旧画像 service_tier 不应受 guardian 条件影响：%s", fields["service_tier"])
		}
	}

	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		syntheticSetBodyFieldCondition(t, doc, "responses_http", "service_tier",
			profilecontract.ConditionNotGuardianReviewRequest)
	})
	if fields := compileClientMetadataTestHTTP(t, synthetic, body, false, "tier-target-normal"); string(fields["service_tier"]) != `"priority"` {
		t.Fatalf("普通请求保留 service_tier：%s", fields["service_tier"])
	}
	if fields := compileClientMetadataTestHTTP(t, synthetic, body, true, "tier-target-review"); fields["service_tier"] != nil {
		t.Fatalf("guardian 审阅请求必须省略 service_tier：%s", fields["service_tier"])
	}
}
