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

// codexProfileDeclaresClientMetadata 判断画像是否声明 ClientMetadata 节（即是否有客户端
// 固定常量要写入 client_metadata）。
func codexProfileDeclaresClientMetadata(profile profilecontract.ExecutableProfile) bool {
	return profile.Optional().ClientMetadata != nil
}

// stripClientMetadataSection 去掉画像的 ClientMetadata 节，还原为未声明常量的旧画像形态。
func stripClientMetadataSection(doc *profilecontract.SnapshotDoc) {
	doc.ClientMetadata = nil
}

// codexServiceTierFollowsGuardianCondition 判断画像 responses_http 的 service_tier 字段是否以
// guardian 审阅条件决定写入（审阅请求与普通请求对 service_tier 的处理因此不同）。
func codexServiceTierFollowsGuardianCondition(profile profilecontract.ExecutableProfile) bool {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID != "responses_http" {
			continue
		}
		for _, field := range endpoint.Body.Fields {
			if field.Name == "service_tier" {
				return field.Condition == profilecontract.ConditionGuardianReviewRequest ||
					field.Condition == profilecontract.ConditionNotGuardianReviewRequest
			}
		}
	}
	return false
}

// codexWebSocketHandshakeDeclaresCookie 判断画像是否为 responses_ws 握手声明 cookie 槽位。
func codexWebSocketHandshakeDeclaresCookie(profile profilecontract.ExecutableProfile) bool {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID != "responses_ws" {
			continue
		}
		for _, slot := range endpoint.Headers {
			if strings.EqualFold(slot.Name, "cookie") {
				return true
			}
		}
	}
	return false
}

// TestCompilerClientMetadataConstantsFollowProfile 的“旧画像”一侧改动前直接取 Active 画像；
// 晋升后 Active 是目标画像、自带 ClientMetadata 节，旧画像前提失效。现按结构事实选出未声明
// ClientMetadata 节的真实发布作为对照组（候选期是 Active，晋升后是 Previous 中同一份旧画像，
// 见 syntheticLegacyBundleForEndpoint），目标一侧仍在该旧画像上追加节，断言原样保留。
func TestCompilerClientMetadataConstantsFollowProfile(t *testing.T) {
	base := syntheticLegacyBundleForEndpoint(t, "responses_http", "ClientMetadata 节",
		codexProfileDeclaresClientMetadata, stripClientMetadataSection)

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

// TestWebSocketFrameClientMetadataConstantsFollowProfile 的旧画像对照组同样按结构事实选出
// （未声明 ClientMetadata 节的真实发布），原因与 HTTP 用例相同。
func TestWebSocketFrameClientMetadataConstantsFollowProfile(t *testing.T) {
	base := syntheticLegacyBundleForEndpoint(t, "responses_ws", "ClientMetadata 节",
		codexProfileDeclaresClientMetadata, stripClientMetadataSection)
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

// TestCompilerOmitsGuardianConditionedServiceTier 的旧画像对照组按结构事实选出：service_tier
// 字段不带 guardian 审阅条件的真实发布（晋升后 Active 的目标画像已把该字段设为
// not_guardian_review_request，不能再充当“无条件”的旧画像）。都带条件时去掉条件合成旧形态。
func TestCompilerOmitsGuardianConditionedServiceTier(t *testing.T) {
	base := syntheticLegacyBundleForEndpoint(t, "responses_http", "service_tier 字段的 guardian 审阅条件",
		codexServiceTierFollowsGuardianCondition,
		func(doc *profilecontract.SnapshotDoc) {
			syntheticSetBodyFieldCondition(t, doc, "responses_http", "service_tier", "")
		})
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

// WS 握手 Cookie（VC-4 第 5 项，编译器侧）：画像为 responses_ws 声明 cookie 槽位并把它
// 追加到 HeaderMap 插入序后，携带 Cookie 的握手在签名前进入编译产物，H1 线序规则同步
// 包含 cookie；旧画像没有该槽位，即使 attempt 携带 Cookie 也不会出站。
//
// 旧画像对照组按结构事实选出：responses_ws 未声明 cookie 槽位的真实发布（晋升后 Active 的
// 目标画像已声明该槽位）；都已声明时删去该槽位合成旧形态。目标一侧在旧画像上追加槽位。
func TestCompilerWebSocketHandshakeCookieFollowsProfileSlot(t *testing.T) {
	base := syntheticLegacyBundleForEndpoint(t, "responses_ws", "responses_ws 的 cookie 槽位",
		codexWebSocketHandshakeDeclaresCookie,
		func(doc *profilecontract.SnapshotDoc) { syntheticRemoveHeaderSlot(t, doc, "responses_ws", "cookie") })
	compile := func(bundle ReleaseBundle, invocationID string) (CompiledExecution, error) {
		t.Helper()
		plan := syntheticPlanForEndpoint(t, bundle, "responses_ws")
		egressPlan := staticClosureEgressPlan(t, bundle, plan, staticClosureLegalTarget(plan.template), invocationID)
		authentication, err := NewAttemptAuthentication(AttemptAuthenticationInput{
			BearerToken: "ws-cookie-token", Cookie: "__oailb=lb; __cf_bm=bm",
		})
		if err != nil {
			t.Fatal(err)
		}
		egressPlan.Authentication = authentication
		egressPlan.IdentityFacts.Conditions.CookiePresent = true
		egressPlan.Body = NewReplayableRequestBody(nil)
		return NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	}

	legacy, err := compile(base, "ws-cookie-legacy")
	if err != nil {
		t.Fatalf("旧画像 WS 握手编译失败：%v", err)
	}
	if legacy.request.Headers().Get("cookie") != "" {
		t.Fatal("旧画像的 WS 握手没有 cookie 槽位，不得出站 Cookie")
	}

	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		syntheticAddHeaderSlot(t, doc, "responses_ws", profilecontract.SnapshotHeaderSlot{
			Slot: 195, Name: "cookie", WireName: "cookie",
			Source:    string(profilecontract.SourceSession),
			Condition: string(profilecontract.ConditionCookiePresent),
		})
	})
	target, err := compile(synthetic, "ws-cookie-target")
	if err != nil {
		t.Fatalf("目标画像 WS 握手编译失败：%v", err)
	}
	if got := target.request.Headers().Get("cookie"); got != "__oailb=lb; __cf_bm=bm" {
		t.Fatalf("WS 握手 Cookie 必须在签名前进入编译产物：%q", got)
	}
	found := false
	for _, rule := range target.transport.TLS.H1HeaderOrders {
		if rule.Mode == "swap_remove" && rule.Path == "/backend-api/codex/responses" {
			for _, name := range rule.Order {
				found = found || name == "cookie"
			}
		}
	}
	if !found {
		t.Fatal("WS swap_remove 线序规则必须包含画像插入序中的 cookie")
	}
}
