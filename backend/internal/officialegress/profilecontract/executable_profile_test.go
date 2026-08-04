package profilecontract

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func loadExecutableProfileDoc(t *testing.T) SnapshotDoc {
	t.Helper()
	raw, err := os.ReadFile("testdata/snapshots/0.145.0/e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b.json")
	if err != nil {
		t.Fatal(err)
	}
	doc, err := ParseSnapshot(raw)
	if err != nil {
		t.Fatal(err)
	}
	return doc
}

func compileExecutableDoc(t *testing.T, doc SnapshotDoc) (ProfileSpec, ExecutableProfile, error) {
	t.Helper()
	profile, err := NewProfileSpec(doc)
	if err != nil {
		return ProfileSpec{}, ExecutableProfile{}, err
	}
	executable, err := CompileExecutableProfile(profile)
	return profile, executable, err
}

func TestEvidenceOnlyLeafChangesProfileDigestButNotExecutableDigest(t *testing.T) {
	doc := loadExecutableProfileDoc(t)
	baseProfile, baseExecutable, err := compileExecutableDoc(t, doc)
	if err != nil {
		t.Fatal(err)
	}
	var rules []string
	if err := json.Unmarshal(doc.RequiredRules, &rules); err != nil {
		t.Fatal(err)
	}
	rules[0] = "SPEC-EVIDENCE-ONLY-MUTATION"
	doc.RequiredRules, err = json.Marshal(rules)
	if err != nil {
		t.Fatal(err)
	}
	mutatedProfile, mutatedExecutable, err := compileExecutableDoc(t, doc)
	if err != nil {
		t.Fatal(err)
	}
	baseEvidenceDigest, err := baseProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	mutatedEvidenceDigest, err := mutatedProfile.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if baseEvidenceDigest == mutatedEvidenceDigest {
		t.Fatal("证据专用叶子变化没有进入 ProfileSpec 摘要")
	}
	if baseExecutable.Digest() != mutatedExecutable.Digest() {
		t.Fatalf("证据专用叶子污染 executable digest: %s != %s", baseExecutable.Digest(), mutatedExecutable.Digest())
	}
}

func TestCompileExecutableProfileRejectsUnknownEnumAndUnboundField(t *testing.T) {
	t.Run("未知枚举", func(t *testing.T) {
		doc := loadExecutableProfileDoc(t)
		doc.Endpoints[0].Compression = "future_compression"
		_, _, err := compileExecutableDoc(t, doc)
		if err == nil || !strings.Contains(err.Error(), "未知枚举") {
			t.Fatalf("未知枚举未被拒绝: %v", err)
		}
	})

	t.Run("未绑定跨端点字段", func(t *testing.T) {
		doc := loadExecutableProfileDoc(t)
		var surfaces []map[string]any
		if err := json.Unmarshal(doc.Surfaces, &surfaces); err != nil {
			t.Fatal(err)
		}
		surfaces[0]["FutureExecutionField"] = true
		var err error
		doc.Surfaces, err = json.Marshal(surfaces)
		if err != nil {
			t.Fatal(err)
		}
		_, _, err = compileExecutableDoc(t, doc)
		if err == nil || !strings.Contains(err.Error(), "unknown field") {
			t.Fatalf("未绑定执行字段未被拒绝: %v", err)
		}
	})
}

func TestCompileExecutableProfileRejectsIllegalLifecycleCombinations(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*SnapshotDoc)
		want   string
	}{
		{
			name: "per_upper_api_call 跨调用复用",
			mutate: func(doc *SnapshotDoc) {
				doc.Transports[0].CrossCallConnectionReuse = true
			},
			want: "per_upper_api_call 禁止跨调用连接复用",
		},
		{
			name: "returned upload 跨调用复用",
			mutate: func(doc *SnapshotDoc) {
				doc.Transports[0].CrossCallConnectionReuse = true
				for i := range doc.Endpoints {
					if doc.Endpoints[i].Upgrade == "" &&
						doc.Endpoints[i].ClientLifecycle != string(LifecycleReturnedUploadUrlCall) {
						doc.Endpoints[i].ClientLifecycle = string(LifecycleBackendClientLongLived)
					}
				}
			},
			want: "returned_upload_url_call 禁止跨上传调用复用",
		},
		{
			name: "websocket 绑定 HTTP transport",
			mutate: func(doc *SnapshotDoc) {
				for i := range doc.Endpoints {
					if doc.Endpoints[i].ID == "responses_http" {
						doc.Endpoints[i].ClientLifecycle = string(LifecycleWebsocketConnection)
						break
					}
				}
			},
			want: "websocket_connection 必须绑定 WebSocket transport",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			doc := loadExecutableProfileDoc(t)
			test.mutate(&doc)
			_, _, err := compileExecutableDoc(t, doc)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("非法生命周期组合未按预期拒绝: %v", err)
			}
		})
	}
}

func TestBackendClientLongLivedRejectsDisabledCrossCallReuse(t *testing.T) {
	doc := loadExecutableProfileDoc(t)
	_, baseExecutable, err := compileExecutableDoc(t, doc)
	if err != nil {
		t.Fatalf("基准画像必须可编译: %v", err)
	}

	longLivedTransportID := ""
	for _, endpoint := range doc.Endpoints {
		if endpoint.ClientLifecycle == string(LifecycleBackendClientLongLived) {
			longLivedTransportID = endpoint.TransportID
			break
		}
	}
	if longLivedTransportID == "" {
		t.Fatal("基准画像缺少 backend_client_long_lived 端点")
	}
	found := false
	for i := range doc.Transports {
		if doc.Transports[i].ID == longLivedTransportID {
			if !doc.Transports[i].CrossCallConnectionReuse {
				t.Fatal("基准长期 transport 必须允许跨调用复用")
			}
			doc.Transports[i].CrossCallConnectionReuse = false
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("基准画像缺少长期 transport: %s", longLivedTransportID)
	}

	_, mutatedExecutable, err := compileExecutableDoc(t, doc)
	if err == nil {
		if mutatedExecutable.Digest() == baseExecutable.Digest() {
			t.Fatal("CrossCallConnectionReuse=false 被静默编译为相同 executable digest")
		}
		t.Fatal("backend_client_long_lived 禁用跨调用复用必须编译失败")
	}
	if !strings.Contains(err.Error(), "backend_client_long_lived 必须允许跨调用连接复用") {
		t.Fatalf("生命周期 mutation 未按预期拒绝: %v", err)
	}
}

func TestCompileExecutableProfileRejectsUnsupportedWSCompressionMutation(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*SnapshotWSTransport)
	}{
		{"offer", func(ws *SnapshotWSTransport) { ws.CompressionOffer = "permessage-deflate" }},
		{"context_takeover", func(ws *SnapshotWSTransport) { ws.ContextTakeover = false }},
		{"rsv1", func(ws *SnapshotWSTransport) { ws.CompressedTextRSV1 = false }},
		{"raw_deflate", func(ws *SnapshotWSTransport) { ws.RawDeflatePayload = false }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			doc := loadExecutableProfileDoc(t)
			for i := range doc.Transports {
				if doc.Transports[i].WebSocket != nil {
					test.mutate(doc.Transports[i].WebSocket)
				}
			}
			_, _, err := compileExecutableDoc(t, doc)
			if err == nil || !strings.Contains(err.Error(), "组合未获批准") {
				t.Fatalf("WS 压缩 mutation 未被拒绝: %v", err)
			}
		})
	}
}
