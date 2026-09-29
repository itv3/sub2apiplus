package officialegress

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// Luna Reserve 是 wham_usage 端点的条件 Header：条件只来自 attempt 级事实
// LunaReservePresent，与 feature 默认值和认证材料无关。
// lunaReserveCondition 直接写画像枚举字面量：该测试只关心 compiler 对条件的解释，
// 不与 -enums 生成常量绑定，避免候选快照入库顺序影响编译。
const lunaReserveCondition profilecontract.ConditionKind = "luna_reserve_present"

func TestCodexHeaderConditionLunaReservePresent(t *testing.T) {
	features := profilecontract.FeatureDefaults{}
	auth := AttemptAuthenticationInput{}
	if !codexHeaderConditionEnabled(lunaReserveCondition, features, CodexRequestConditions{LunaReservePresent: true}, auth) {
		t.Fatal("LunaReservePresent=true 时条件必须成立")
	}
	if codexHeaderConditionEnabled(lunaReserveCondition, features, CodexRequestConditions{}, auth) {
		t.Fatal("LunaReservePresent=false 时条件不得成立")
	}
	enabled, err := codexBodyFieldConditionEnabled(
		lunaReserveCondition, features, CodexRequestConditions{LunaReservePresent: true}, BodyRuntimeConditions{}, auth,
	)
	if err != nil || !enabled {
		t.Fatalf("Body 条件求值必须复用同一 Header 条件：enabled=%v err=%v", enabled, err)
	}
	if !profilecontract.EngineSupportedEnumValues().Contains(
		profilecontract.EnumDomainConditionKind, string(lunaReserveCondition),
	) {
		t.Fatal("执行引擎必须显式登记 luna_reserve_present，否则含该槽位的画像会被拒绝")
	}
}

// TestCompilerLunaReserveReachesWireOnlyThroughProfileSlot 证明 Luna Reserve 条件事实只经
// 画像槽位落到 wire。service 的配额链路总为 wham_usage 声明该条件事实，是否出站完全由
// Bundle 画像的槽位决定；而 service 包无法向配额链路注入合成 Bundle（BundleResolver 只能由
// 正式 ReleaseCatalog 构造），所以“画像没有该槽位时条件头不进 wire”的负例在此用合成画像
// 证明，不依赖目录里恰好有无槽位的版本：
//   - 底稿取正式目录中 wham_usage 声明了该槽位的发布（按结构事实选择 mode）：条件成立时
//     出站值等于画像常量，条件不成立时不出站；
//   - 用 syntheticCodexBundle 去掉该槽位后，条件成立也不出站，且出站 Header 与“底稿、条件
//     不成立”逐项一致——条件事实对无槽位画像的出站字节没有任何影响。
func TestCompilerLunaReserveReachesWireOnlyThroughProfileSlot(t *testing.T) {
	const lunaReserveHeader = "x-openai-codex-luna-reserve"
	const endpointID = "wham_usage"
	var base ReleaseBundle
	slotValue := ""
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		bundle, plan := staticClosurePlanForEndpoint(t, mode, endpointID)
		for _, slot := range plan.template.endpoint.Headers {
			if strings.EqualFold(slot.Name, lunaReserveHeader) && slot.Condition == lunaReserveCondition {
				base, slotValue = bundle, slot.Value
			}
		}
		if slotValue != "" {
			break
		}
	}
	if slotValue == "" {
		t.Fatal("正式目录的 Active/Previous 都没有 wham_usage 的 Luna Reserve 槽位：对照组缺少底稿")
	}

	compile := func(t *testing.T, bundle ReleaseBundle, reserve bool) http.Header {
		t.Helper()
		plan := syntheticPlanForEndpoint(t, bundle, endpointID)
		target := staticClosureLegalTarget(plan.template)
		egressPlan := staticClosureEgressPlan(t, bundle, plan, target, "luna-reserve-profile-slot")
		egressPlan.IdentityFacts.Conditions.LunaReservePresent = reserve
		execution, err := NewCompiler().Compile(
			context.Background(), bundle, egressPlan, staticClosureDynamicInputs(plan.template),
		)
		if err != nil {
			t.Fatalf("编译 %s 失败（LunaReservePresent=%t）：%v", endpointID, reserve, err)
		}
		return execution.request.Headers()
	}

	declared := compile(t, base, true)
	if got := declared.Get(lunaReserveHeader); got != slotValue {
		t.Fatalf("画像声明槽位且条件成立时应出站 %s=%q，实际 %q", lunaReserveHeader, slotValue, got)
	}
	withoutFact := compile(t, base, false)
	if got := withoutFact.Get(lunaReserveHeader); got != "" {
		t.Fatalf("条件不成立时不得出站 %s，实际 %q", lunaReserveHeader, got)
	}

	withoutSlot := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
		kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers))
		for _, slot := range endpoint.Headers {
			if !strings.EqualFold(slot.Name, lunaReserveHeader) {
				kept = append(kept, slot)
			}
		}
		if len(kept) == len(endpoint.Headers) {
			t.Fatalf("合成前提不成立：底稿 %s 没有 %s 槽位", endpointID, lunaReserveHeader)
		}
		endpoint.Headers = kept
	})
	leaked := compile(t, withoutSlot, true)
	if got := leaked.Get(lunaReserveHeader); got != "" {
		t.Fatalf("画像没有 Luna Reserve 槽位时条件事实泄漏到 wire：%s=%q", lunaReserveHeader, got)
	}
	if !equalHeaderSets(leaked, withoutFact) {
		t.Fatalf("无槽位画像的出站 Header 受 Luna Reserve 条件事实影响：\n%v\n%v", leaked, withoutFact)
	}
}
