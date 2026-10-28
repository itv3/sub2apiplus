package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// TurnState 节的 ResetOnNewTurn 字段（turn-state 只在产生它的回合内回送）：
//  1. 缺省表示旧行为：解码为 false，规范化 JSON 里没有该键，旧画像摘要不变；
//  2. 只接受 true；显式 false、显式 null、类型错误一律失败关闭；
//  3. 声明 true 的节进入执行投影与官方摘要，与缺省画像的摘要不同。

const (
	turnStateLegacyJSON  = `{"ResetOnAccountOwnerChange":true}`
	turnStateNewTurnJSON = `{"ResetOnAccountOwnerChange":true,"ResetOnNewTurn":true}`
)

func TestTurnStateResetOnNewTurnDefaultsToLegacy(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionTurnState, json.RawMessage(turnStateLegacyJSON))
	if err != nil {
		t.Fatalf("旧行为节被拒绝：%v", err)
	}
	legacy, ok := decoded.(*TurnStateSection)
	if !ok || legacy.ResetOnNewTurn {
		t.Fatalf("缺省 ResetOnNewTurn 应解码为 false：%#v", decoded)
	}
	encoded, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != turnStateLegacyJSON {
		t.Fatalf("旧行为节序列化应与输入逐字节相同：\n got %s\nwant %s", encoded, turnStateLegacyJSON)
	}
	decoded, err = DecodeOptionalSection(SectionTurnState, json.RawMessage(turnStateNewTurnJSON))
	if err != nil {
		t.Fatalf("声明 ResetOnNewTurn 的节被拒绝：%v", err)
	}
	declared, ok := decoded.(*TurnStateSection)
	if !ok || !declared.ResetOnNewTurn {
		t.Fatalf("ResetOnNewTurn 解码结果错误：%#v", decoded)
	}
	encoded, err = json.Marshal(declared)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != turnStateNewTurnJSON {
		t.Fatalf("声明后的节序列化应保持字段序：\n got %s\nwant %s", encoded, turnStateNewTurnJSON)
	}
}

func TestTurnStateResetOnNewTurnRejectsInvalidValues(t *testing.T) {
	cases := []struct {
		label   string
		raw     string
		wantErr string
	}{
		{"显式 false", `false`, "ResetOnNewTurn 存在时必须为 true"},
		{"显式 null", `null`, "不得显式为 null"},
		{"字符串", `"true"`, "解析可选节"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			raw := mutatedSectionJSON(t, turnStateNewTurnJSON, func(fields map[string]json.RawMessage) {
				fields["ResetOnNewTurn"] = json.RawMessage(item.raw)
			})
			_, err := DecodeOptionalSection(SectionTurnState, raw)
			if err == nil {
				t.Fatalf("应被拒绝：%s", raw)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

func TestTurnStateResetOnNewTurnEntersExecutableProjectionAndDigest(t *testing.T) {
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatalf("旧行为画像编译失败：%v", err)
	}
	_, newTurnDoc := withAllOptionalSections(t)
	newTurnDoc.TurnState = json.RawMessage(turnStateNewTurnJSON)
	newTurnProfile, newTurnExecutable, err := compileExecutableDoc(t, newTurnDoc)
	if err != nil {
		t.Fatalf("声明 ResetOnNewTurn 的画像编译失败：%v", err)
	}
	if section := legacyExecutable.Optional().TurnState; section == nil || section.ResetOnNewTurn {
		t.Fatalf("旧行为执行投影不应声明 ResetOnNewTurn：%#v", section)
	}
	if section := newTurnExecutable.Optional().TurnState; section == nil || !section.ResetOnNewTurn {
		t.Fatalf("ResetOnNewTurn 没有进入执行投影：%#v", section)
	}
	legacyDigest, err := legacyProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	newTurnDigest, err := newTurnProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if legacyDigest == newTurnDigest {
		t.Fatal("ResetOnNewTurn 不同的画像摘要必须不同")
	}
	if legacyExecutable.Digest() == newTurnExecutable.Digest() {
		t.Fatal("ResetOnNewTurn 不同的 executable 摘要必须不同")
	}
}
