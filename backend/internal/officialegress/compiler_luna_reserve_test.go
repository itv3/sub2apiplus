package officialegress

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// Luna Reserve 是 wham_usage 端点的条件 Header：条件只来自 attempt 级事实
// LunaReservePresent，与 feature 默认值和认证材料无关。
func TestCodexHeaderConditionLunaReservePresent(t *testing.T) {
	features := profilecontract.FeatureDefaults{}
	auth := AttemptAuthenticationInput{}
	if !codexHeaderConditionEnabled(conditionLunaReservePresent, features, CodexRequestConditions{LunaReservePresent: true}, auth) {
		t.Fatal("LunaReservePresent=true 时条件必须成立")
	}
	if codexHeaderConditionEnabled(conditionLunaReservePresent, features, CodexRequestConditions{}, auth) {
		t.Fatal("LunaReservePresent=false 时条件不得成立")
	}
	enabled, err := codexBodyFieldConditionEnabled(
		conditionLunaReservePresent, features, CodexRequestConditions{LunaReservePresent: true}, BodyRuntimeConditions{}, auth,
	)
	if err != nil || !enabled {
		t.Fatalf("Body 条件求值必须复用同一 Header 条件：enabled=%v err=%v", enabled, err)
	}
	if !profilecontract.EngineSupportedEnumValues().Contains(
		profilecontract.EnumDomainConditionKind, string(conditionLunaReservePresent),
	) {
		t.Fatal("执行引擎必须显式登记 luna_reserve_present，否则含该槽位的画像会被拒绝")
	}
}
