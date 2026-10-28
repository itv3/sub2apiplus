package profilecontract

import (
	"encoding/json"
	"testing"
)

// ReasoningEffort 节的 ConfigurationUpdate 字段：声明 HTTP 出站对第三方入站合成 configuration_update 的方式。
// 缺省（旧版本画像）不合成，规范化形态与摘要不变（omitempty）；非法取值失败关闭。

func TestReasoningEffortSectionConfigurationUpdateDecodes(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionReasoningEffort,
		json.RawMessage(`{"CustomNumericSerialization":"u64_integer","ConfigurationUpdate":"http_first_turn"}`))
	if err != nil {
		t.Fatalf("合法的 ConfigurationUpdate 被拒绝：%v", err)
	}
	section, ok := decoded.(*ReasoningEffortSection)
	if !ok || section.ConfigurationUpdate != ReasoningEffortConfigurationUpdateHTTPFirstTurn {
		t.Fatalf("解码结果错误：%#v", decoded)
	}

	// 缺省：旧版本画像的节照旧合法，字段为空，重新序列化后不带该键（摘要不变）。
	legacy, err := DecodeOptionalSection(SectionReasoningEffort, json.RawMessage(`{"CustomNumericSerialization":"u64_integer"}`))
	if err != nil {
		t.Fatalf("旧版本节被拒绝：%v", err)
	}
	if legacy.(*ReasoningEffortSection).ConfigurationUpdate != "" {
		t.Fatalf("缺省必须为空：%#v", legacy)
	}
	encoded, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != `{"CustomNumericSerialization":"u64_integer"}` {
		t.Fatalf("缺省时不得出现 ConfigurationUpdate 键：%s", encoded)
	}
}

func TestReasoningEffortSectionConfigurationUpdateRejectsUnknownValues(t *testing.T) {
	for label, raw := range map[string]string{
		"未知取值":    `{"CustomNumericSerialization":"u64_integer","ConfigurationUpdate":"ws_first_turn"}`,
		"大小写不同":   `{"CustomNumericSerialization":"u64_integer","ConfigurationUpdate":"HTTP_FIRST_TURN"}`,
		"显式 null": `{"CustomNumericSerialization":"u64_integer","ConfigurationUpdate":null}`,
	} {
		if _, err := DecodeOptionalSection(SectionReasoningEffort, json.RawMessage(raw)); err == nil {
			t.Fatalf("%s 应被拒绝", label)
		}
	}
}
