package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// BaseInstructions 节的 IDScheme 字段（增量窗口方案）：
//  1. 缺省表示旧方案：解码结果为空串，规范化 JSON 里没有该键，因此节缺省 IDScheme 的画像摘要与引入该字段之前相同；
//  2. 只接受 uuidv7；其他取值、显式空串、显式 null 与大小写变体一律失败关闭；
//  3. 声明 uuidv7 的节进入执行投影与官方摘要，与旧方案的摘要不同；
//  4. UsesUUIDv7IDs 对 nil 接收者安全。

const baseInstructionsUUIDv7SectionJSON = `{"Carrier":"input_developer_message","ContentItemKind":"model.base_instructions","EndpointIDs":["responses_http","responses_ws"],"IDPrefix":"msg","IDScheme":"uuidv7"}`

func TestBaseInstructionsIDSchemeDefaultsToLegacyAndDeclaresUUIDv7(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionBaseInstructions, json.RawMessage(baseInstructionsSectionJSON))
	if err != nil {
		t.Fatalf("旧方案节被拒绝：%v", err)
	}
	legacy, ok := decoded.(*BaseInstructionsSection)
	if !ok || legacy.IDScheme != "" || legacy.UsesUUIDv7IDs() {
		t.Fatalf("缺省 IDScheme 应解码为空串且不是 uuidv7：%#v", decoded)
	}
	// 规范化编码里不能出现 IDScheme 键：旧画像的快照字节与摘要依赖这一点。
	encoded, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "IDScheme") {
		t.Fatalf("缺省 IDScheme 不应出现在序列化结果里：%s", encoded)
	}

	decoded, err = DecodeOptionalSection(SectionBaseInstructions, json.RawMessage(baseInstructionsUUIDv7SectionJSON))
	if err != nil {
		t.Fatalf("uuidv7 方案节被拒绝：%v", err)
	}
	v7, ok := decoded.(*BaseInstructionsSection)
	if !ok || v7.IDScheme != BaseInstructionsIDSchemeUUIDv7 || !v7.UsesUUIDv7IDs() {
		t.Fatalf("uuidv7 方案解码结果错误：%#v", decoded)
	}
	encoded, err = json.Marshal(v7)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != baseInstructionsUUIDv7SectionJSON {
		t.Fatalf("uuidv7 节序列化应保持字段序且 IDScheme 在末位：\n got %s\nwant %s", encoded, baseInstructionsUUIDv7SectionJSON)
	}

	var absent *BaseInstructionsSection
	if absent.UsesUUIDv7IDs() {
		t.Fatal("nil 节不应声明 uuidv7 方案")
	}
}

func TestBaseInstructionsIDSchemeRejectsInvalidValues(t *testing.T) {
	cases := []struct {
		label   string
		raw     string
		wantErr string
	}{
		{"未知取值", `"v7"`, "IDScheme 只支持 uuidv7"},
		{"大小写变体", `"UUIDv7"`, "IDScheme 只支持 uuidv7"},
		{"旧方案的显式写法", `"thread_content_v5"`, "IDScheme 只支持 uuidv7"},
		{"显式空串", `""`, "IDScheme 存在时不得为空串"},
		{"显式 null", `null`, "不得显式为 null"},
		{"类型错误", `7`, "解析可选节"},
		{"带空白的取值", `" uuidv7"`, "IDScheme 只支持 uuidv7"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			raw := mutatedSectionJSON(t, baseInstructionsUUIDv7SectionJSON, func(fields map[string]json.RawMessage) {
				fields["IDScheme"] = json.RawMessage(item.raw)
			})
			_, err := DecodeOptionalSection(SectionBaseInstructions, raw)
			if err == nil {
				t.Fatalf("应被拒绝：%s", raw)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

// 声明 uuidv7 的节进入执行投影；它与旧方案的画像摘要不同（同一份语义只能有一种表示，方案变化必须体现在摘要里）。
func TestBaseInstructionsIDSchemeEntersExecutableProjectionAndDigest(t *testing.T) {
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatalf("旧方案画像编译失败：%v", err)
	}

	_, v7Doc := withAllOptionalSections(t)
	v7Doc.BaseInstructions = json.RawMessage(baseInstructionsUUIDv7SectionJSON)
	v7Profile, v7Executable, err := compileExecutableDoc(t, v7Doc)
	if err != nil {
		t.Fatalf("uuidv7 方案画像编译失败：%v", err)
	}

	if section := legacyExecutable.Optional().BaseInstructions; section == nil || section.UsesUUIDv7IDs() {
		t.Fatalf("旧方案执行投影不应声明 uuidv7：%#v", section)
	}
	if section := v7Executable.Optional().BaseInstructions; section == nil || !section.UsesUUIDv7IDs() || section.IDPrefix != "msg" {
		t.Fatalf("uuidv7 方案没有进入执行投影：%#v", section)
	}

	legacyDigest, err := legacyProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	v7Digest, err := v7Profile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if legacyDigest == v7Digest {
		t.Fatal("IDScheme 不同的画像摘要必须不同")
	}
	if legacyExecutable.Digest() == v7Executable.Digest() {
		t.Fatal("IDScheme 不同的 executable 摘要必须不同")
	}
}
