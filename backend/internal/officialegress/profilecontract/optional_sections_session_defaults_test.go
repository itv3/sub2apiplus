package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// TurnMetadata 节的 SessionDefaults（独立 images 端点在没有同回合 Responses 事实时使用的默认会话模型）：
//  1. 缺省时序列化字节不含该字段，旧画像摘要不变；
//  2. 存在时严格校验形态，且对应的键必须出现在节 Keys 里，避免执行层因键未声明而静默丢弃。

const sessionDefaultsKeysJSON = `"Keys":["model","node_repl_auto_review_required","reasoning_effort","session_id"],` +
	`"AnalyticsEnabled":true,"CompactionImplementations":["responses"],"CompactionPhases":["pre_turn"]`

func TestTurnMetadataSessionDefaultsOmittedWhenAbsent(t *testing.T) {
	raw := json.RawMessage(`{` + sessionDefaultsKeysJSON + `}`)
	decoded, err := DecodeOptionalSection(SectionTurnMetadata, raw)
	if err != nil {
		t.Fatal(err)
	}
	section := decoded.(*TurnMetadataSection)
	if section.SessionDefaults != nil {
		t.Fatalf("缺省时不应有会话默认值：%+v", section.SessionDefaults)
	}
	encoded, err := json.Marshal(section)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "SessionDefaults") {
		t.Fatalf("缺省时序列化不得出现 SessionDefaults（旧画像字节必须不变）：%s", encoded)
	}
}

func TestTurnMetadataSessionDefaultsAccepted(t *testing.T) {
	for name, defaults := range map[string]string{
		"完整":   `{"Model":"gpt-6.1-sol","ReasoningEffort":"low","NodeReplAutoReviewRequired":true}`,
		"强度为空": `{"Model":"gpt-5.5","ReasoningEffort":"","NodeReplAutoReviewRequired":false}`,
		"数字档位": `{"Model":"gpt-6-astra","ReasoningEffort":"32","NodeReplAutoReviewRequired":true}`,
	} {
		raw := json.RawMessage(`{` + sessionDefaultsKeysJSON + `,"SessionDefaults":` + defaults + `}`)
		decoded, err := DecodeOptionalSection(SectionTurnMetadata, raw)
		if err != nil {
			t.Fatalf("%s：%v", name, err)
		}
		section := decoded.(*TurnMetadataSection)
		if section.SessionDefaults == nil {
			t.Fatalf("%s：会话默认值丢失", name)
		}
		// 往返：再序列化、再解码，取值不变。
		encoded, err := json.Marshal(section)
		if err != nil {
			t.Fatal(err)
		}
		again, err := DecodeOptionalSection(SectionTurnMetadata, encoded)
		if err != nil {
			t.Fatalf("%s 往返：%v", name, err)
		}
		if *again.(*TurnMetadataSection).SessionDefaults != *section.SessionDefaults {
			t.Fatalf("%s 往返后取值漂移", name)
		}
	}
}

func TestTurnMetadataSessionDefaultsRejected(t *testing.T) {
	const compaction = `"AnalyticsEnabled":true,"CompactionImplementations":["responses"],"CompactionPhases":["pre_turn"]`
	good := `"SessionDefaults":{"Model":"gpt-6.1-sol","ReasoningEffort":"low","NodeReplAutoReviewRequired":true}`
	for name, body := range map[string]string{
		"模型为空":         `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":{"Model":"","ReasoningEffort":"low","NodeReplAutoReviewRequired":true}}`,
		"模型含大写":        `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":{"Model":"GPT-6","ReasoningEffort":"low","NodeReplAutoReviewRequired":true}}`,
		"模型含空格":        `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":{"Model":"gpt 6","ReasoningEffort":"low","NodeReplAutoReviewRequired":true}}`,
		"强度含大写":        `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":{"Model":"gpt-6.1-sol","ReasoningEffort":"Low","NodeReplAutoReviewRequired":true}}`,
		"未知嵌套字段":       `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":{"Model":"gpt-6.1-sol","ReasoningEffort":"low","NodeReplAutoReviewRequired":true,"Extra":1}}`,
		"显式 null":      `{` + sessionDefaultsKeysJSON + `,"SessionDefaults":null}`,
		"Keys 缺 model": `{"Keys":["reasoning_effort","session_id"],` + compaction + `,` + good + `}`,
		"强度非空但 Keys 缺 reasoning_effort": `{"Keys":["model","session_id"],` + compaction + `,` + good + `}`,
	} {
		if _, err := DecodeOptionalSection(SectionTurnMetadata, json.RawMessage(body)); err == nil {
			t.Fatalf("%s 应被拒绝：%s", name, body)
		}
	}
	// 强度为空时 Keys 不要求含 reasoning_effort。
	ok := `{"Keys":["model","session_id"],` + compaction + `,"SessionDefaults":{"Model":"gpt-5.5","ReasoningEffort":"","NodeReplAutoReviewRequired":false}}`
	if _, err := DecodeOptionalSection(SectionTurnMetadata, json.RawMessage(ok)); err != nil {
		t.Fatalf("强度为空且 Keys 不含 reasoning_effort 应通过：%v", err)
	}
}
