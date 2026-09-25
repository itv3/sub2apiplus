package officialegress

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// guardian 审阅条件与工作区路由 override 条件（VC-4 第 1 项）：
//  1. 头与 body 两个条件求值函数对三个新条件给出一致的真值表；
//  2. 新字段为 false 时 CodexRequestConditions 的 JSON 与旧结构逐字节相同，
//     旧画像下的身份事实摘要不变；
//  3. 正式目录的 Active/Previous 画像不引用新条件，求值结果无法影响其出站字节；
//  4. 合成画像声明 guardian 槽位时，审阅请求与普通请求互斥地出现 x-codex-guardian
//     与 routing hint。

func TestCodexGuardianAndRoutingOverrideConditionTruthTable(t *testing.T) {
	features := profilecontract.FeatureDefaults{}
	auth := AttemptAuthenticationInput{}
	cases := []struct {
		name        string
		conditions  CodexRequestConditions
		guardian    bool
		notGuardian bool
		override    bool
	}{
		{name: "普通请求", conditions: CodexRequestConditions{}, notGuardian: true},
		{name: "guardian 审阅请求", conditions: CodexRequestConditions{GuardianReviewRequest: true}, guardian: true},
		{name: "路由 override", conditions: CodexRequestConditions{AccountRoutingOverridePresent: true}, notGuardian: true, override: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for condition, want := range map[profilecontract.ConditionKind]bool{
				profilecontract.ConditionGuardianReviewRequest:         tc.guardian,
				profilecontract.ConditionNotGuardianReviewRequest:      tc.notGuardian,
				profilecontract.ConditionAccountRoutingOverridePresent: tc.override,
			} {
				if got := codexHeaderConditionEnabled(condition, features, tc.conditions, auth); got != want {
					t.Fatalf("header 条件 %s 求值 %t，期望 %t", condition, got, want)
				}
				got, err := codexBodyFieldConditionEnabled(condition, features, tc.conditions, BodyRuntimeConditions{}, auth)
				if err != nil || got != want {
					t.Fatalf("body 条件 %s 求值 %t/%v，期望 %t", condition, got, err, want)
				}
			}
		})
	}
}

// legacyCodexRequestConditions 是新增 guardian／路由字段之前的结构形态，
// 用来证明新字段取零值时序列化结果逐字节不变。
type legacyCodexRequestConditions struct {
	TurnStatePresent        bool
	SubagentPresent         bool
	MemoryGeneration        bool
	ParentThreadPresent     bool
	SessionIDPresent        bool
	AttestationPresent      bool
	FedRAMPAccount          bool
	ManagedResidencyPresent bool
	CookiePresent           bool
	CompressionEligible     bool
	ModelSupportsLite       bool
	BetaFeaturesPresent     bool
	LunaReservePresent      bool
}

func TestCodexRequestConditionsNewFieldsKeepLegacyJSON(t *testing.T) {
	current := CodexRequestConditions{
		TurnStatePresent: true, SessionIDPresent: true, CookiePresent: true,
		CompressionEligible: true, BetaFeaturesPresent: true, LunaReservePresent: true,
	}
	legacy := legacyCodexRequestConditions{
		TurnStatePresent: true, SessionIDPresent: true, CookiePresent: true,
		CompressionEligible: true, BetaFeaturesPresent: true, LunaReservePresent: true,
	}
	currentRaw, err := json.Marshal(current)
	if err != nil {
		t.Fatal(err)
	}
	legacyRaw, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if string(currentRaw) != string(legacyRaw) {
		t.Fatalf("新条件取 false 时 JSON 形态漂移：\n当前 %s\n旧版 %s", currentRaw, legacyRaw)
	}
	facts := executorInvocationIdentityFacts(t)
	guardianFacts := facts
	guardianFacts.Conditions.GuardianReviewRequest = true
	if facts.Digest() == guardianFacts.Digest() {
		t.Fatal("guardian 审阅条件必须进入身份事实摘要")
	}
}

func TestEmbeddedReleasesDoNotReferenceGuardianOrRoutingConditions(t *testing.T) {
	forbidden := map[profilecontract.ConditionKind]bool{
		profilecontract.ConditionGuardianReviewRequest:         true,
		profilecontract.ConditionNotGuardianReviewRequest:      true,
		profilecontract.ConditionAccountRoutingOverridePresent: true,
	}
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			for _, slot := range endpoint.Headers {
				if forbidden[slot.Condition] {
					t.Fatalf("%s 画像 %s 的 header %s 引用了新条件 %s", mode, endpoint.ID, slot.Name, slot.Condition)
				}
			}
			for _, field := range endpoint.Body.Fields {
				if forbidden[field.Condition] {
					t.Fatalf("%s 画像 %s 的 body 字段 %s 引用了新条件 %s", mode, endpoint.ID, field.Name, field.Condition)
				}
			}
		}
	}
}

// guardianTargetProfileMutation 按目标画像设计稿追加 guardian 相关槽位：
// x-codex-guardian（常量 reviewer，条件 guardian_review_request）、routing hint 改为
// not_guardian_review_request。槽位号取 routing hint 之后，仅为让合成画像自洽。
func guardianTargetProfileMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		for _, endpointID := range []string{"responses_http", "responses_ws"} {
			syntheticSetHeaderCondition(t, doc, endpointID, "x-codex-routing-hint",
				profilecontract.ConditionNotGuardianReviewRequest)
			syntheticAddHeaderSlot(t, doc, endpointID, profilecontract.SnapshotHeaderSlot{
				Slot: 66, Name: "x-codex-guardian", WireName: "x-codex-guardian", Value: "reviewer",
				Source:    string(profilecontract.SourceConstant),
				Condition: string(profilecontract.ConditionGuardianReviewRequest),
			})
		}
	}
}

func compileGuardianTestRequest(
	t *testing.T,
	bundle ReleaseBundle,
	guardian bool,
	invocationID string,
) http.Header {
	t.Helper()
	plan := syntheticPlanForEndpoint(t, bundle, "responses_http")
	target := staticClosureLegalTarget(plan.template)
	egressPlan := staticClosureEgressPlan(t, bundle, plan, target, invocationID)
	egressPlan.IdentityFacts.Conditions.GuardianReviewRequest = guardian
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译 responses_http 失败（guardian=%t）：%v", guardian, err)
	}
	return execution.request.Headers()
}

func TestCompilerGuardianSlotsFollowProfileConditions(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")

	// 旧画像：无论条件取值如何，出站 Header 完全一致，且没有 x-codex-guardian。
	legacyNormal := compileGuardianTestRequest(t, base, false, "guardian-legacy-normal")
	legacyReview := compileGuardianTestRequest(t, base, true, "guardian-legacy-review")
	if !equalHeaderSets(legacyNormal, legacyReview) {
		t.Fatalf("旧画像的出站 Header 受到 guardian 条件影响：\n%v\n%v", legacyNormal, legacyReview)
	}
	if legacyReview.Get("x-codex-guardian") != "" {
		t.Fatal("旧画像不得发出 x-codex-guardian")
	}
	if legacyReview.Get("x-codex-routing-hint") == "" {
		t.Fatal("旧画像的 routing hint 不应受 guardian 条件影响")
	}

	// 目标画像：审阅请求带 x-codex-guardian、不带 routing hint；普通请求相反。
	synthetic := syntheticCodexBundle(t, base, guardianTargetProfileMutation(t))
	normal := compileGuardianTestRequest(t, synthetic, false, "guardian-target-normal")
	review := compileGuardianTestRequest(t, synthetic, true, "guardian-target-review")
	if normal.Get("x-codex-guardian") != "" || normal.Get("x-codex-routing-hint") == "" {
		t.Fatalf("普通请求应只带 routing hint：%v", normal)
	}
	if review.Get("x-codex-guardian") != "reviewer" || review.Get("x-codex-routing-hint") != "" {
		t.Fatalf("审阅请求应只带 x-codex-guardian: reviewer：%v", review)
	}
}

func equalHeaderSets(left, right http.Header) bool {
	if len(left) != len(right) {
		return false
	}
	for name, values := range left {
		other := right.Values(name)
		if strings.Join(values, "\x00") != strings.Join(other, "\x00") {
			return false
		}
	}
	return true
}
