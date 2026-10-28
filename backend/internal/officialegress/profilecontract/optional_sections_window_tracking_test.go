package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// TurnMetadata 节的 WindowTracking 字段（WS 回合元数据按官方跟踪上下文窗口）：
//  1. 缺省表示旧行为：解码为 false，规范化 JSON 里没有该键，旧画像摘要不变；
//  2. 只接受 true；显式 false、显式 null、类型错误一律失败关闭；声明时 Keys 必须含 window_id、window_number、context_window_id；
//  3. 声明 true 的节进入执行投影与官方摘要，与缺省画像的摘要不同。

func turnMetadataWithWindowTracking(t *testing.T, mutate func(fields map[string]json.RawMessage)) json.RawMessage {
	t.Helper()
	_, doc := withAllOptionalSections(t)
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(doc.TurnMetadata, &fields); err != nil {
		t.Fatal(err)
	}
	fields["Keys"] = json.RawMessage(`["analytics_enabled","context_window_id","model","session_id","window_id","window_number"]`)
	fields["WindowTracking"] = json.RawMessage(`true`)
	if mutate != nil {
		mutate(fields)
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestTurnMetadataWindowTrackingDefaultsToLegacy(t *testing.T) {
	_, doc := withAllOptionalSections(t)
	decoded, err := DecodeOptionalSection(SectionTurnMetadata, doc.TurnMetadata)
	if err != nil {
		t.Fatalf("旧行为节被拒绝：%v", err)
	}
	legacy, ok := decoded.(*TurnMetadataSection)
	if !ok || legacy.WindowTracking {
		t.Fatalf("缺省 WindowTracking 应解码为 false：%#v", decoded)
	}
	encoded, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "WindowTracking") {
		t.Fatalf("缺省 WindowTracking 不应出现在序列化结果里：%s", encoded)
	}
	declared, err := DecodeOptionalSection(SectionTurnMetadata, turnMetadataWithWindowTracking(t, nil))
	if err != nil {
		t.Fatalf("声明 WindowTracking 的节被拒绝：%v", err)
	}
	if section, ok := declared.(*TurnMetadataSection); !ok || !section.WindowTracking {
		t.Fatalf("WindowTracking 解码结果错误：%#v", declared)
	}
}

func TestTurnMetadataWindowTrackingRejectsInvalidValues(t *testing.T) {
	cases := []struct {
		label   string
		mutate  func(fields map[string]json.RawMessage)
		wantErr string
	}{
		{"显式 false", func(f map[string]json.RawMessage) { f["WindowTracking"] = json.RawMessage(`false`) }, "WindowTracking 存在时必须为 true"},
		{"显式 null", func(f map[string]json.RawMessage) { f["WindowTracking"] = json.RawMessage(`null`) }, "不得显式为 null"},
		{"字符串", func(f map[string]json.RawMessage) { f["WindowTracking"] = json.RawMessage(`"true"`) }, "解析可选节"},
		{"Keys 缺 window_number", func(f map[string]json.RawMessage) {
			f["Keys"] = json.RawMessage(`["analytics_enabled","context_window_id","model","session_id","window_id"]`)
		}, "Keys 必须包含 window_number"},
		{"Keys 缺 context_window_id", func(f map[string]json.RawMessage) {
			f["Keys"] = json.RawMessage(`["analytics_enabled","model","session_id","window_id","window_number"]`)
		}, "Keys 必须包含 context_window_id"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			_, err := DecodeOptionalSection(SectionTurnMetadata, turnMetadataWithWindowTracking(t, item.mutate))
			if err == nil {
				t.Fatal("应被拒绝")
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

func TestTurnMetadataWindowTrackingEntersExecutableProjectionAndDigest(t *testing.T) {
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatalf("旧行为画像编译失败：%v", err)
	}
	_, trackedDoc := withAllOptionalSections(t)
	trackedDoc.TurnMetadata = turnMetadataWithWindowTracking(t, nil)
	trackedProfile, trackedExecutable, err := compileExecutableDoc(t, trackedDoc)
	if err != nil {
		t.Fatalf("声明 WindowTracking 的画像编译失败：%v", err)
	}
	if section := legacyExecutable.Optional().TurnMetadata; section == nil || section.WindowTracking {
		t.Fatalf("旧行为执行投影不应声明 WindowTracking：%#v", section)
	}
	if section := trackedExecutable.Optional().TurnMetadata; section == nil || !section.WindowTracking {
		t.Fatalf("WindowTracking 没有进入执行投影：%#v", section)
	}
	legacyDigest, err := legacyProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	trackedDigest, err := trackedProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if legacyDigest == trackedDigest || legacyExecutable.Digest() == trackedExecutable.Digest() {
		t.Fatal("WindowTracking 不同的画像摘要与 executable 摘要必须不同")
	}
}
