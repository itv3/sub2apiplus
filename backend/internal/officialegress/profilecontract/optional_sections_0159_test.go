package profilecontract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 较新版本画像的新增行为（ReasoningEffort、ImageGeneration 两节）：
//  1. 合法取值能严格解码并进入执行投影；
//  2. 取值闭集、排序去重、不相交与“存在时不能为空”逐项失败关闭；
//  3. 未声明这些节或字段的画像，序列化形态、官方摘要与执行投影均不变（omitempty）。

func TestNewerProfileSectionsDecodeAndProject(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionReasoningEffort, json.RawMessage(`{"CustomNumericSerialization":"u64_integer"}`))
	if err != nil {
		t.Fatalf("合法的 ReasoningEffort 节被拒绝：%v", err)
	}
	if section, ok := decoded.(*ReasoningEffortSection); !ok || section.CustomNumericSerialization != "u64_integer" {
		t.Fatalf("ReasoningEffort 解码结果错误：%#v", decoded)
	}
	decoded, err = DecodeOptionalSection(SectionImageGeneration, json.RawMessage(`{"DefaultBackground":"opaque","TransparentBackground":"transparent","EditImageReferences":["file_id","image_url"]}`))
	if err != nil {
		t.Fatalf("合法的 ImageGeneration 节被拒绝：%v", err)
	}
	image, ok := decoded.(*ImageGenerationSection)
	if !ok || image.DefaultBackground != "opaque" || strings.Join(image.EditImageReferences, ",") != "file_id,image_url" {
		t.Fatalf("ImageGeneration 解码结果错误：%#v", decoded)
	}

	_, extended := withAllOptionalSections(t)
	spec, err := NewProfileSpec(extended)
	if err != nil {
		t.Fatal(err)
	}
	executable, err := CompileExecutableProfile(spec)
	if err != nil {
		t.Fatalf("含新节的画像编译失败：%v", err)
	}
	optional := executable.Optional()
	if optional.ReasoningEffort == nil || optional.ImageGeneration == nil {
		t.Fatalf("新节必须进入执行投影：%#v", optional)
	}
}

func TestNewerProfileSectionsStrictDecoding(t *testing.T) {
	cases := map[string]struct {
		name string
		raw  string
	}{
		"未知序列化":         {SectionReasoningEffort, `{"CustomNumericSerialization":"float"}`},
		"序列化缺省":         {SectionReasoningEffort, `{}`},
		"推理节未知字段":       {SectionReasoningEffort, `{"CustomNumericSerialization":"u64_integer","Extra":true}`},
		"背景取值非法":        {SectionImageGeneration, `{"DefaultBackground":"transparent","TransparentBackground":"transparent","EditImageReferences":["image_url"]}`},
		"透明取值非法":        {SectionImageGeneration, `{"DefaultBackground":"opaque","TransparentBackground":"clear","EditImageReferences":["image_url"]}`},
		"引用未排序":         {SectionImageGeneration, `{"DefaultBackground":"opaque","TransparentBackground":"transparent","EditImageReferences":["image_url","file_id"]}`},
		"引用未知形态":        {SectionImageGeneration, `{"DefaultBackground":"opaque","TransparentBackground":"transparent","EditImageReferences":["image_url","mask_id"]}`},
		"引用缺 image_url": {SectionImageGeneration, `{"DefaultBackground":"opaque","TransparentBackground":"transparent","EditImageReferences":["file_id"]}`},
		"引用为空":          {SectionImageGeneration, `{"DefaultBackground":"opaque","TransparentBackground":"transparent","EditImageReferences":[]}`},
		// 字段级 null 拒绝对所有节生效：这里选缺省值本身合法的字段，只有 null 检查能拦下。
		"字段显式 null": {SectionTurnState, `{"ResetOnAccountOwnerChange":null}`},
	}
	for label, item := range cases {
		if _, err := DecodeOptionalSection(item.name, json.RawMessage(item.raw)); err == nil {
			t.Fatalf("%s 应被拒绝", label)
		}
	}
}

func TestExistingSectionSnapshotsKeepDigestsWithoutNewerSections(t *testing.T) {
	paths, err := filepath.Glob("testdata/snapshots/0.157.0/*.json")
	if err != nil || len(paths) == 0 {
		t.Fatalf("0.157.0 快照缺失: %v", err)
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		doc, err := ParseSnapshot(raw)
		if err != nil {
			t.Fatal(err)
		}
		if doc.ReasoningEffort != nil || doc.ImageGeneration != nil {
			t.Fatalf("%s 不应含较新版本的可选节", path)
		}
		prepared, err := PrepareSnapshotForManifest(doc)
		if err != nil {
			t.Fatalf("%s 规范化失败: %v", path, err)
		}
		if want := strings.TrimSuffix(filepath.Base(path), ".json"); prepared.Digest != want {
			t.Fatalf("%s 官方摘要漂移: %s", path, prepared.Digest)
		}
		spec, err := NewProfileSpec(doc)
		if err != nil {
			t.Fatal(err)
		}
		executable, err := CompileExecutableProfile(spec)
		if err != nil {
			t.Fatalf("%s 编译失败: %v", path, err)
		}
		optional := executable.Optional()
		if optional.ReasoningEffort != nil || optional.ImageGeneration != nil {
			t.Fatalf("%s 不应有较新版本可选节的投影", path)
		}
	}
}
