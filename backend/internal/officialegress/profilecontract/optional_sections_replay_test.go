package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// WebSocketContinuation 节的 ReplayOnSameConnection 字段（官方整段重放不重连）：
//  1. 缺省表示旧行为：解码结果为 false，规范化 JSON 里没有该键，因此节缺省该字段的画像摘要与引入该字段之前相同；
//  2. 只接受 true；显式 false、显式 null、类型错误一律失败关闭（false 与缺省无法区分，会让同一份语义有两种表示）；
//  3. 声明 true 的节进入执行投影与官方摘要，与缺省画像的摘要不同（行为变化必须体现在摘要里）。

const (
	webSocketContinuationLegacyJSON = `{"ResetOn":["account_owner","auth_revision"]}`
	webSocketContinuationReplayJSON = `{"ResetOn":["account_owner","auth_revision"],"ReplayOnSameConnection":true}`
)

func TestWebSocketContinuationReplayOnSameConnectionDefaultsToLegacy(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionWebSocketContinuation, json.RawMessage(webSocketContinuationLegacyJSON))
	if err != nil {
		t.Fatalf("旧行为节被拒绝：%v", err)
	}
	legacy, ok := decoded.(*WebSocketContinuationSection)
	if !ok || legacy.ReplayOnSameConnection {
		t.Fatalf("缺省 ReplayOnSameConnection 应解码为 false：%#v", decoded)
	}
	// 规范化编码里不能出现该键：旧画像的快照字节与摘要依赖这一点。
	encoded, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "ReplayOnSameConnection") {
		t.Fatalf("缺省 ReplayOnSameConnection 不应出现在序列化结果里：%s", encoded)
	}
	if string(encoded) != webSocketContinuationLegacyJSON {
		t.Fatalf("旧行为节序列化应与输入逐字节相同：\n got %s\nwant %s", encoded, webSocketContinuationLegacyJSON)
	}

	decoded, err = DecodeOptionalSection(SectionWebSocketContinuation, json.RawMessage(webSocketContinuationReplayJSON))
	if err != nil {
		t.Fatalf("声明 ReplayOnSameConnection 的节被拒绝：%v", err)
	}
	replay, ok := decoded.(*WebSocketContinuationSection)
	if !ok || !replay.ReplayOnSameConnection {
		t.Fatalf("ReplayOnSameConnection 解码结果错误：%#v", decoded)
	}
	encoded, err = json.Marshal(replay)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != webSocketContinuationReplayJSON {
		t.Fatalf("声明后的节序列化应保持字段序且该键在 ResetOn 之后：\n got %s\nwant %s", encoded, webSocketContinuationReplayJSON)
	}
}

func TestWebSocketContinuationReplayOnSameConnectionRejectsInvalidValues(t *testing.T) {
	cases := []struct {
		label   string
		raw     string
		wantErr string
	}{
		{"显式 false", `false`, "ReplayOnSameConnection 存在时必须为 true"},
		{"显式 null", `null`, "不得显式为 null"},
		{"字符串", `"true"`, "解析可选节"},
		{"数字", `1`, "解析可选节"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			raw := mutatedSectionJSON(t, webSocketContinuationReplayJSON, func(fields map[string]json.RawMessage) {
				fields["ReplayOnSameConnection"] = json.RawMessage(item.raw)
			})
			_, err := DecodeOptionalSection(SectionWebSocketContinuation, raw)
			if err == nil {
				t.Fatalf("应被拒绝：%s", raw)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

// 声明 true 的节进入执行投影；它与缺省画像的摘要不同。
func TestWebSocketContinuationReplayOnSameConnectionEntersExecutableProjectionAndDigest(t *testing.T) {
	_, legacyDoc := withAllOptionalSections(t)
	legacyProfile, legacyExecutable, err := compileExecutableDoc(t, legacyDoc)
	if err != nil {
		t.Fatalf("旧行为画像编译失败：%v", err)
	}

	_, replayDoc := withAllOptionalSections(t)
	replayDoc.WebSocketContinuation = json.RawMessage(webSocketContinuationReplayJSON)
	replayProfile, replayExecutable, err := compileExecutableDoc(t, replayDoc)
	if err != nil {
		t.Fatalf("声明重放同连接的画像编译失败：%v", err)
	}

	if section := legacyExecutable.Optional().WebSocketContinuation; section == nil || section.ReplayOnSameConnection {
		t.Fatalf("旧行为执行投影不应声明 ReplayOnSameConnection：%#v", section)
	}
	if section := replayExecutable.Optional().WebSocketContinuation; section == nil || !section.ReplayOnSameConnection {
		t.Fatalf("ReplayOnSameConnection 没有进入执行投影：%#v", section)
	}

	legacyDigest, err := legacyProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	replayDigest, err := replayProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if legacyDigest == replayDigest {
		t.Fatal("ReplayOnSameConnection 不同的画像摘要必须不同")
	}
	if legacyExecutable.Digest() == replayExecutable.Digest() {
		t.Fatal("ReplayOnSameConnection 不同的 executable 摘要必须不同")
	}
}
