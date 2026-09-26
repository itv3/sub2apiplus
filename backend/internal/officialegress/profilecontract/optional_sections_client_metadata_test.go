package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// ClientMetadata 节的按键条件（KeyConditions）：
//  1. 同一节内不同常量可以有不同写入条件，未覆盖的键取节级 Condition；
//  2. 覆盖表只能引用已声明常量、取引擎支持的非空条件，且存在时不能为空；
//  3. 未使用该字段的节序列化形态不变（omitempty），已有目标画像草稿的摘要不受影响。

func TestClientMetadataKeyConditionsOverrideSectionCondition(t *testing.T) {
	raw := json.RawMessage(`{"Condition":"not_guardian_review_request","Constants":{"guardian_credits_requested":"true","mcp_attribution":"{\"status\":\"none\"}"},"KeyConditions":{"mcp_attribution":"always"}}`)
	decoded, err := DecodeOptionalSection(SectionClientMetadata, raw)
	if err != nil {
		t.Fatalf("合法的按键条件被拒绝：%v", err)
	}
	section, ok := decoded.(*ClientMetadataSection)
	if !ok {
		t.Fatalf("解码类型错误：%T", decoded)
	}
	if got := section.ConditionFor("mcp_attribution"); got != ConditionAlways {
		t.Fatalf("mcp_attribution 应取覆盖条件 always，实际 %s", got)
	}
	if got := section.ConditionFor("guardian_credits_requested"); got != ConditionNotGuardianReviewRequest {
		t.Fatalf("未覆盖的键应取节级条件，实际 %s", got)
	}
	if section.Constants["mcp_attribution"] != `{"status":"none"}` {
		t.Fatalf("JSON 形态的字符串常量必须原样保留：%q", section.Constants["mcp_attribution"])
	}
}

func TestClientMetadataKeyConditionsStrictDecoding(t *testing.T) {
	cases := map[string]string{
		"空覆盖表":   `{"Condition":"always","Constants":{"a":"1"},"KeyConditions":{}}`,
		"未声明的键":  `{"Condition":"always","Constants":{"a":"1"},"KeyConditions":{"b":"always"}}`,
		"未知条件":   `{"Condition":"always","Constants":{"a":"1"},"KeyConditions":{"a":"sometimes"}}`,
		"空串条件":   `{"Condition":"always","Constants":{"a":"1"},"KeyConditions":{"a":""}}`,
		"覆盖表类型错": `{"Condition":"always","Constants":{"a":"1"},"KeyConditions":["a"]}`,
	}
	for label, raw := range cases {
		if _, err := DecodeOptionalSection(SectionClientMetadata, json.RawMessage(raw)); err == nil {
			t.Fatalf("%s 应被拒绝", label)
		}
	}
}

func TestClientMetadataSectionWithoutKeyConditionsKeepsSerializedShape(t *testing.T) {
	section := ClientMetadataSection{
		Constants: map[string]string{"guardian_credits_requested": "true"},
		Condition: ConditionNotGuardianReviewRequest,
	}
	raw, err := json.Marshal(section)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "KeyConditions") {
		t.Fatalf("未使用 KeyConditions 的节不得改变序列化形态：%s", raw)
	}
	if want := `{"Constants":{"guardian_credits_requested":"true"},"Condition":"not_guardian_review_request"}`; string(raw) != want {
		t.Fatalf("序列化形态漂移：\n实际 %s\n期望 %s", raw, want)
	}
}
