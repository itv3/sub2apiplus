package officialegress

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// guardian 审阅条件与工作区路由 override 条件（VC-4 第 1 项）：
//  1. 头与 body 两个条件求值函数对三个新条件给出一致的真值表；
//  2. 新字段为 false 时 CodexRequestConditions 的 JSON 与旧结构逐字节相同，
//     旧画像下的身份事实摘要不变；
//  3. 正式目录中未声明对应槽位／节的画像不引用新条件，求值结果无法影响其出站字节；
//     声明了的画像（目标画像）引用与其声明一致；
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

// codexProfileDeclaresGuardianReviewSlot 判断画像是否声明了 guardian 审阅槽位：至少一个
// header 槽位或 body 字段以 guardian_review_request 为条件（即审阅请求有专属出站内容）。
func codexProfileDeclaresGuardianReviewSlot(profile profilecontract.ExecutableProfile) bool {
	for _, endpoint := range profile.Endpoints() {
		for _, slot := range endpoint.Headers {
			if slot.Condition == profilecontract.ConditionGuardianReviewRequest {
				return true
			}
		}
		for _, field := range endpoint.Body.Fields {
			if field.Condition == profilecontract.ConditionGuardianReviewRequest {
				return true
			}
		}
	}
	return false
}

// codexNewConditionReferenceViolations 按结构事实列出画像对 guardian／路由 override 新条件
// 的越界引用，返回空切片表示合规。判定只依据条件本身与画像声明的槽位／可选节，不写死
// 版本、槽位名或端点：
//   - guardian_review_request／not_guardian_review_request：只有声明了 guardian 审阅槽位的
//     画像才允许引用。未声明时端点 header、body 字段与 ClientMetadata 节（按每个常量键
//     实际生效的写入条件）都不得引用两者之一，否则 guardian 标记的求值会改变该画像下
//     请求的出站字节（例如 not_guardian 条件的槽位在审阅请求里消失）。
//   - account_routing_override_present：只有声明了 WorkspaceRouting 节的画像才允许引用，
//     且只能出现在该节 OverrideHeader 所指的 header 槽位、RoutedEndpointIDs 列出的端点上；
//     body 字段与 ClientMetadata 节不承载路由 override，引用即越界。
func codexNewConditionReferenceViolations(profile profilecontract.ExecutableProfile) []string {
	declaresGuardian := codexProfileDeclaresGuardianReviewSlot(profile)
	optional := profile.Optional()
	routing := optional.WorkspaceRouting
	var violations []string
	checkGuardian := func(where string, condition profilecontract.ConditionKind) {
		if (condition == profilecontract.ConditionGuardianReviewRequest ||
			condition == profilecontract.ConditionNotGuardianReviewRequest) && !declaresGuardian {
			violations = append(violations, fmt.Sprintf("%s 引用 %s，但画像未声明 guardian 审阅槽位", where, condition))
		}
	}
	for _, endpoint := range profile.Endpoints() {
		for _, slot := range endpoint.Headers {
			where := fmt.Sprintf("端点 %s 的 header %s", endpoint.ID, slot.Name)
			checkGuardian(where, slot.Condition)
			if slot.Condition != profilecontract.ConditionAccountRoutingOverridePresent {
				continue
			}
			switch {
			case routing == nil:
				violations = append(violations, where+" 引用路由 override 条件，但画像未声明 WorkspaceRouting 节")
			case !strings.EqualFold(slot.Name, routing.OverrideHeader) ||
				!slices.Contains(routing.RoutedEndpointIDs, endpoint.ID):
				violations = append(violations,
					where+" 引用路由 override 条件，但与 WorkspaceRouting 节的 OverrideHeader／RoutedEndpointIDs 不一致")
			}
		}
		for _, field := range endpoint.Body.Fields {
			where := fmt.Sprintf("端点 %s 的 body 字段 %s", endpoint.ID, field.Name)
			checkGuardian(where, field.Condition)
			if field.Condition == profilecontract.ConditionAccountRoutingOverridePresent {
				violations = append(violations, where+" 引用路由 override 条件，但 WorkspaceRouting 节只声明 override header")
			}
		}
	}
	if metadata := optional.ClientMetadata; metadata != nil {
		keys := make([]string, 0, len(metadata.Constants))
		for key := range metadata.Constants {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			where := fmt.Sprintf("ClientMetadata 节常量 %s", key)
			condition := metadata.ConditionFor(key)
			checkGuardian(where, condition)
			if condition == profilecontract.ConditionAccountRoutingOverridePresent {
				violations = append(violations, where+" 引用路由 override 条件，但 ClientMetadata 节不承载路由 override")
			}
		}
	}
	return violations
}

// syntheticRoutingOverrideSection 是合成判定用的 WorkspaceRouting 节：取值与目标画像形态
// 一致，发现端点借用各版本画像都有的 wham_usage，仅为让合成画像通过端点存在性校验。
func syntheticRoutingOverrideSection() profilecontract.WorkspaceRoutingSection {
	return profilecontract.WorkspaceRoutingSection{
		DiscoveryEndpointID:    "wham_usage",
		DefaultOrigin:          "https://chatgpt.com",
		AcceptedOriginValues:   []string{"NO_CONSTRAINT", "https://chatgpt.com"},
		AcceptedOverrideValues: []string{"NO_CONSTRAINT"},
		OverrideHeader:         "x-openai-account-routing-override",
		NonDefaultAction:       "fail_closed",
		RoutedEndpointIDs:      []string{"responses_http", "responses_ws"},
	}
}

// TestEmbeddedReleasesDoNotReferenceGuardianOrRoutingConditions 的本意：新条件的求值不能
// 影响未声明它们的画像的出站字节。改动前正式目录两个 mode 都是旧画像，用例直接要求
// “两个 mode 都不引用新条件”；RuntimeCatalog 切换后 previous 槽位装入目标画像，它声明了
// guardian 审阅槽位与 WorkspaceRouting 节，引用新条件是合法的。因此改为按结构事实逐 mode
// 判定：未声明对应槽位／节的画像不得引用，声明了的画像引用必须与其声明一致。判定本身
// 的判别力用合成画像锁定，不依赖目录里恰好有未声明或已声明的版本。
func TestEmbeddedReleasesDoNotReferenceGuardianOrRoutingConditions(t *testing.T) {
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		if violations := codexNewConditionReferenceViolations(release.ExecutableProfile()); len(violations) > 0 {
			t.Fatalf("%s 画像 %s 的新条件引用越界：\n%s",
				mode, release.ProfileDigest(), strings.Join(violations, "\n"))
		}
	}

	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	// dropGuardianReviewSlots 把画像里全部以 guardian_review_request 为条件的槽位改为恒定
	// 写入，使合成画像不再声明审阅槽位（底稿是旧画像时为空操作）。
	dropGuardianReviewSlots := func(doc *profilecontract.SnapshotDoc) {
		for endpointIndex := range doc.Endpoints {
			endpoint := &doc.Endpoints[endpointIndex]
			for index := range endpoint.Headers {
				if endpoint.Headers[index].Condition == string(profilecontract.ConditionGuardianReviewRequest) {
					endpoint.Headers[index].Condition = string(profilecontract.ConditionAlways)
				}
			}
			for index := range endpoint.Body.Fields {
				if endpoint.Body.Fields[index].Condition == string(profilecontract.ConditionGuardianReviewRequest) {
					endpoint.Body.Fields[index].Condition = string(profilecontract.ConditionAlways)
				}
			}
		}
	}
	violationCases := []struct {
		name   string
		mutate func(*testing.T, *profilecontract.SnapshotDoc)
		want   string
	}{
		{
			name: "未声明审阅槽位却引用 not_guardian",
			mutate: func(t *testing.T, doc *profilecontract.SnapshotDoc) {
				dropGuardianReviewSlots(doc)
				syntheticSetHeaderCondition(t, doc, "responses_http", "x-codex-routing-hint",
					profilecontract.ConditionNotGuardianReviewRequest)
			},
			want: "但画像未声明 guardian 审阅槽位",
		},
		{
			name: "未声明 WorkspaceRouting 节却引用路由 override",
			mutate: func(t *testing.T, doc *profilecontract.SnapshotDoc) {
				doc.WorkspaceRouting = nil
				syntheticSetHeaderCondition(t, doc, "responses_http", "x-codex-routing-hint",
					profilecontract.ConditionAccountRoutingOverridePresent)
			},
			want: "但画像未声明 WorkspaceRouting 节",
		},
		{
			name: "路由 override 落在节未声明的槽位",
			mutate: func(t *testing.T, doc *profilecontract.SnapshotDoc) {
				doc.WorkspaceRouting = syntheticRawSection(t, syntheticRoutingOverrideSection())
				syntheticSetHeaderCondition(t, doc, "responses_http", "x-codex-routing-hint",
					profilecontract.ConditionAccountRoutingOverridePresent)
			},
			want: "与 WorkspaceRouting 节的 OverrideHeader／RoutedEndpointIDs 不一致",
		},
	}
	for _, tc := range violationCases {
		t.Run(tc.name, func(t *testing.T) {
			synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) { tc.mutate(t, doc) })
			violations := codexNewConditionReferenceViolations(synthetic.release.executable)
			if !strings.Contains(strings.Join(violations, "\n"), tc.want) {
				t.Fatalf("判定未检出越界引用 %q：%v", tc.want, violations)
			}
		})
	}

	t.Run("完整声明 guardian 审阅槽位时放行", func(t *testing.T) {
		synthetic := base
		if !codexProfileDeclaresGuardianReviewSlot(base.release.executable) {
			synthetic = syntheticCodexBundle(t, base, guardianTargetProfileMutation(t))
		}
		if violations := codexNewConditionReferenceViolations(synthetic.release.executable); len(violations) > 0 {
			t.Fatalf("按目标画像形态声明的 guardian 条件被误判越界：%v", violations)
		}
	})
	t.Run("路由 override 落在节声明的槽位时放行", func(t *testing.T) {
		synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
			routing := syntheticRoutingOverrideSection()
			doc.WorkspaceRouting = syntheticRawSection(t, routing)
			endpoint := syntheticSnapshotEndpoint(t, doc, "responses_http")
			nextSlot := 0
			for _, slot := range endpoint.Headers {
				nextSlot = max(nextSlot, slot.Slot+1)
			}
			syntheticAddHeaderSlot(t, doc, "responses_http", profilecontract.SnapshotHeaderSlot{
				Slot: nextSlot, Name: routing.OverrideHeader, WireName: routing.OverrideHeader,
				Value: "synthetic-override", Source: string(profilecontract.SourceConstant),
				Condition: string(profilecontract.ConditionAccountRoutingOverridePresent),
			})
		})
		if violations := codexNewConditionReferenceViolations(synthetic.release.executable); len(violations) > 0 {
			t.Fatalf("与 WorkspaceRouting 节一致的路由 override 引用被误判越界：%v", violations)
		}
	})
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
