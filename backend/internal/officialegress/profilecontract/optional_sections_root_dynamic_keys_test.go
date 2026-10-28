package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// TurnMetadata 节的 RootThreadKeys 与 ClientMetadata 节的 DynamicKeys：
// 缺省为旧行为且序列化不出现；显式 false／null／类型错误／空列表／未知键／未排序／重复／与常量重名一律拒绝；进入投影与摘要。

func turnMetadataWithRootThreadKeys(t *testing.T, keys string) json.RawMessage {
	t.Helper()
	_, doc := withAllOptionalSections(t)
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(doc.TurnMetadata, &fields); err != nil {
		t.Fatal(err)
	}
	fields["Keys"] = json.RawMessage(keys)
	fields["RootThreadKeys"] = json.RawMessage(`true`)
	raw, _ := json.Marshal(fields)
	return raw
}

func TestTurnMetadataRootThreadKeysDefaultsAndValidation(t *testing.T) {
	_, doc := withAllOptionalSections(t)
	decoded, err := DecodeOptionalSection(SectionTurnMetadata, doc.TurnMetadata)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(decoded)
	if decoded.(*TurnMetadataSection).RootThreadKeys || strings.Contains(string(encoded), "RootThreadKeys") {
		t.Fatal("缺省应为 false 且不出现在序列化里")
	}
	good := turnMetadataWithRootThreadKeys(t, `["agent_name","analytics_enabled","model","root_turn_id","session_id"]`)
	declared, err := DecodeOptionalSection(SectionTurnMetadata, good)
	if err != nil || !declared.(*TurnMetadataSection).RootThreadKeys {
		t.Fatalf("声明被拒绝或未生效：%v", err)
	}
	cases := []struct {
		label   string
		raw     func() json.RawMessage
		wantErr string
	}{
		{"显式 false", func() json.RawMessage {
			return mutatedSectionJSON(t, string(good), func(f map[string]json.RawMessage) { f["RootThreadKeys"] = json.RawMessage(`false`) })
		}, "RootThreadKeys 存在时必须为 true"},
		{"显式 null", func() json.RawMessage {
			return mutatedSectionJSON(t, string(good), func(f map[string]json.RawMessage) { f["RootThreadKeys"] = json.RawMessage(`null`) })
		}, "不得显式为 null"},
		{"Keys 缺 agent_name", func() json.RawMessage {
			return turnMetadataWithRootThreadKeys(t, `["analytics_enabled","model","root_turn_id","session_id"]`)
		}, "Keys 必须包含 agent_name"},
		{"Keys 缺 root_turn_id", func() json.RawMessage {
			return turnMetadataWithRootThreadKeys(t, `["agent_name","analytics_enabled","model","session_id"]`)
		}, "Keys 必须包含 root_turn_id"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			if _, err := DecodeOptionalSection(SectionTurnMetadata, item.raw()); err == nil || !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("期望错误含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
	// 摘要随字段变化。
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatal(err)
	}
	_, trackedDoc := withAllOptionalSections(t)
	trackedDoc.TurnMetadata = turnMetadataWithRootThreadKeys(t, `["agent_name","analytics_enabled","model","root_turn_id","session_id"]`)
	profile, executable, err := compileExecutableDoc(t, trackedDoc)
	if err != nil {
		t.Fatalf("声明的画像编译失败：%v", err)
	}
	if !executable.Optional().TurnMetadata.RootThreadKeys || legacyExecutable.Optional().TurnMetadata.RootThreadKeys {
		t.Fatal("字段没有正确进入执行投影")
	}
	legacyDigest, _ := legacyProfile.ProfileDigest()
	digest, _ := profile.ProfileDigest()
	if legacyDigest == digest || legacyExecutable.Digest() == executable.Digest() {
		t.Fatal("摘要必须随该字段变化")
	}
}

const clientMetadataDynamicBaseJSON = `{"Condition":"not_guardian_review_request","Constants":{"guardian_credits_requested":"true"}}`

func clientMetadataWithDynamicKeys(t *testing.T, value string) json.RawMessage {
	t.Helper()
	return mutatedSectionJSON(t, clientMetadataDynamicBaseJSON, func(f map[string]json.RawMessage) {
		f["DynamicKeys"] = json.RawMessage(value)
	})
}

func TestClientMetadataDynamicKeysDefaultsAndValidation(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionClientMetadata, json.RawMessage(clientMetadataDynamicBaseJSON))
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(decoded)
	if decoded.(*ClientMetadataSection).DynamicKeys != nil || strings.Contains(string(encoded), "DynamicKeys") {
		t.Fatal("缺省应为空且不出现在序列化里")
	}
	allKeys := `["root_turn_id","ws_request_header_x_openai_internal_codex_responses_lite","x-codex-ws-stream-request-start-ms"]`
	declared, err := DecodeOptionalSection(SectionClientMetadata, clientMetadataWithDynamicKeys(t, allKeys))
	if err != nil || len(declared.(*ClientMetadataSection).DynamicKeys) != 3 {
		t.Fatalf("合法声明被拒绝：%v", err)
	}
	cases := []struct {
		label   string
		value   string
		wantErr string
	}{
		{"显式 null", `null`, "不得显式为 null"},
		{"空列表", `[]`, "DynamicKeys 不能为空"},
		{"未知键", `["unknown_key"]`, "DynamicKeys 含未知取值"},
		{"未排序", `["x-codex-ws-stream-request-start-ms","root_turn_id"]`, "DynamicKeys 必须排序"},
		{"重复", `["root_turn_id","root_turn_id"]`, "DynamicKeys 含重复取值"},
		{"类型错误", `"root_turn_id"`, "解析可选节"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			if _, err := DecodeOptionalSection(SectionClientMetadata, clientMetadataWithDynamicKeys(t, item.value)); err == nil || !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("期望错误含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
	// 与常量重名。
	collide := mutatedSectionJSON(t, clientMetadataDynamicBaseJSON, func(f map[string]json.RawMessage) {
		f["Constants"] = json.RawMessage(`{"root_turn_id":"x"}`)
		f["DynamicKeys"] = json.RawMessage(`["root_turn_id"]`)
	})
	if _, err := DecodeOptionalSection(SectionClientMetadata, collide); err == nil || !strings.Contains(err.Error(), "DynamicKeys 与 Constants 重名") {
		t.Fatalf("与常量重名必须拒绝：%v", err)
	}
	// 摘要随字段变化。
	_, legacyDoc := withAllOptionalSections(t)
	legacyDoc.ClientMetadata = json.RawMessage(clientMetadataDynamicBaseJSON)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatal(err)
	}
	_, doc := withAllOptionalSections(t)
	doc.ClientMetadata = clientMetadataWithDynamicKeys(t, allKeys)
	profile, executable, err := compileExecutableDoc(t, doc)
	if err != nil {
		t.Fatalf("声明的画像编译失败：%v", err)
	}
	if len(executable.Optional().ClientMetadata.DynamicKeys) != 3 || len(legacyExecutable.Optional().ClientMetadata.DynamicKeys) != 0 {
		t.Fatal("字段没有正确进入执行投影")
	}
	legacyDigest, _ := legacyProfile.ProfileDigest()
	digest, _ := profile.ProfileDigest()
	if legacyDigest == digest || legacyExecutable.Digest() == executable.Digest() {
		t.Fatal("摘要必须随该字段变化")
	}
}
