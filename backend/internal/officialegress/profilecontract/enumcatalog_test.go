package profilecontract_test

import (
	"testing"

	p "github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func TestObservedValuesAreEngineSupported(t *testing.T) {
	observed := p.ObservedEnumValues()
	supported := p.EngineSupportedEnumValues()
	if err := p.ValidateObservedSubset(observed, supported); err != nil {
		t.Fatal(err)
	}
	if !observed.Contains(p.EnumDomainConditionKind, "") ||
		!observed.Contains(p.EnumDomainOmitCondition, "") {
		t.Fatal("真实快照中的 Condition/OmitWhen 零值必须进入 Observed")
	}
	if observed.Contains(p.EnumDomainValueSource, "") {
		t.Fatal("当前快照没有空 Source，不得凭空加入 Observed")
	}
}

func TestObservedSubsetGateRejectsUnsupportedValue(t *testing.T) {
	observed := p.ObservedEnumValues()
	supportedMap := p.EngineSupportedEnumValues().AsMap()
	values := supportedMap[p.EnumDomainConditionKind]
	if len(values) == 0 {
		t.Fatal("测试夹具缺少 condition")
	}
	supportedMap[p.EnumDomainConditionKind] = append([]string(nil), values[1:]...)
	supported, err := p.NewEnumCatalog(supportedMap)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.ValidateObservedSubset(observed, supported); err == nil {
		t.Fatal("缺少任一观测值时门禁必须失败")
	}
}

func TestEnumCatalogGettersDoNotExposeInternalMaps(t *testing.T) {
	catalog := p.EngineSupportedEnumValues()
	copyMap := catalog.AsMap()
	copyMap[p.EnumDomainBodyEncoding][0] = "mutated"
	copyMap[p.EnumDomainValueSource] = nil
	if catalog.Contains(p.EnumDomainBodyEncoding, "mutated") ||
		len(catalog.Values(p.EnumDomainValueSource)) == 0 {
		t.Fatal("调用方修改返回 map/slice 污染了枚举目录")
	}
}
