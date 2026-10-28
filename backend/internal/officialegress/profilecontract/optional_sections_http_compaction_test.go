package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// TurnMetadata 节的 HTTPCompactionRequests 字段（HTTP 压缩请求按官方写元数据与请求体）：
// 缺省为旧行为且序列化不出现；只接受 true；声明时 CompactionImplementations 必须含 responses_compaction_v2；进入投影与摘要。

func turnMetadataWithHTTPCompaction(t *testing.T, mutate func(fields map[string]json.RawMessage)) json.RawMessage {
	t.Helper()
	_, doc := withAllOptionalSections(t)
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(doc.TurnMetadata, &fields); err != nil {
		t.Fatal(err)
	}
	fields["HTTPCompactionRequests"] = json.RawMessage(`true`)
	if mutate != nil {
		mutate(fields)
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestTurnMetadataHTTPCompactionRequestsDefaultsToLegacyAndRejectsInvalid(t *testing.T) {
	_, doc := withAllOptionalSections(t)
	decoded, err := DecodeOptionalSection(SectionTurnMetadata, doc.TurnMetadata)
	if err != nil {
		t.Fatal(err)
	}
	if section := decoded.(*TurnMetadataSection); section.HTTPCompactionRequests {
		t.Fatal("缺省应为 false")
	}
	encoded, _ := json.Marshal(decoded)
	if strings.Contains(string(encoded), "HTTPCompactionRequests") {
		t.Fatalf("缺省不应出现在序列化结果里：%s", encoded)
	}
	declared, err := DecodeOptionalSection(SectionTurnMetadata, turnMetadataWithHTTPCompaction(t, nil))
	if err != nil {
		t.Fatalf("声明的节被拒绝：%v", err)
	}
	if !declared.(*TurnMetadataSection).HTTPCompactionRequests {
		t.Fatal("声明后应为 true")
	}
	cases := []struct {
		label   string
		mutate  func(f map[string]json.RawMessage)
		wantErr string
	}{
		{"显式 false", func(f map[string]json.RawMessage) { f["HTTPCompactionRequests"] = json.RawMessage(`false`) }, "HTTPCompactionRequests 存在时必须为 true"},
		{"显式 null", func(f map[string]json.RawMessage) { f["HTTPCompactionRequests"] = json.RawMessage(`null`) }, "不得显式为 null"},
		{"字符串", func(f map[string]json.RawMessage) { f["HTTPCompactionRequests"] = json.RawMessage(`"true"`) }, "解析可选节"},
		{"闭集缺 responses_compaction_v2", func(f map[string]json.RawMessage) {
			f["CompactionImplementations"] = json.RawMessage(`["responses"]`)
		}, "CompactionImplementations 必须包含 responses_compaction_v2"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			_, err := DecodeOptionalSection(SectionTurnMetadata, turnMetadataWithHTTPCompaction(t, item.mutate))
			if err == nil || !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("期望错误含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

func TestTurnMetadataHTTPCompactionRequestsEntersExecutableProjectionAndDigest(t *testing.T) {
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatal(err)
	}
	_, doc := withAllOptionalSections(t)
	doc.TurnMetadata = turnMetadataWithHTTPCompaction(t, nil)
	profile, executable, err := compileExecutableDoc(t, doc)
	if err != nil {
		t.Fatalf("声明的画像编译失败：%v", err)
	}
	if legacyExecutable.Optional().TurnMetadata.HTTPCompactionRequests || !executable.Optional().TurnMetadata.HTTPCompactionRequests {
		t.Fatal("字段没有正确进入执行投影")
	}
	legacyDigest, _ := legacyProfile.ProfileDigest()
	digest, _ := profile.ProfileDigest()
	if legacyDigest == digest || legacyExecutable.Digest() == executable.Digest() {
		t.Fatal("摘要必须随该字段变化")
	}
}
