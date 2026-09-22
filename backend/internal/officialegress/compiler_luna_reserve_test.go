package officialegress

import (
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
