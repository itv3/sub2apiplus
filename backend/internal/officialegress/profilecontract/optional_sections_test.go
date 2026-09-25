package profilecontract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 可选节的门禁（design Q1）：
//  1. 旧版本画像（不含任何可选节）的官方摘要、ProfileDigest 与 executable 摘要逐字节不变；
//  2. 含可选节的画像完整往返，节进入三类摘要；
//  3. 严格解码：未知字段、显式 null、未排序／重复、非法取值、未知节名一律拒绝。

// withAllOptionalSections 在真实 0.154.0 快照上附加全部可选节（取值与 0.156.1 设计稿一致）。
func withAllOptionalSections(t *testing.T) (SnapshotDoc, SnapshotDoc) {
	t.Helper()
	paths, err := filepath.Glob("testdata/snapshots/0.154.0/*.json")
	if err != nil || len(paths) == 0 {
		t.Fatalf("找不到 0.154.0 快照: %v", err)
	}
	raw, err := os.ReadFile(paths[0])
	if err != nil {
		t.Fatal(err)
	}
	base, err := ParseSnapshot(raw)
	if err != nil {
		t.Fatal(err)
	}
	extended := base
	extended.CookieJar = json.RawMessage(`{"AllowedNames":["__cf_bm","__oailb","_cfuvid"],"AllowedPrefixes":["cf_chl_"],"WebSocketHandshakeWriteBack":true}`)
	extended.WorkspaceRouting = json.RawMessage(`{"DiscoveryEndpointID":"wham_usage","DefaultOrigin":"https://chatgpt.com","AcceptedOriginValues":["NO_CONSTRAINT","https://chatgpt.com"],"AcceptedOverrideValues":["NO_CONSTRAINT"],"OverrideHeader":"x-openai-account-routing-override","NonDefaultAction":"fail_closed","RoutedEndpointIDs":["responses_http","responses_ws"]}`)
	extended.TurnMetadata = json.RawMessage(`{"Keys":["analytics_enabled","model","session_id"],"AnalyticsEnabled":true,"CompactionImplementations":["responses","responses_compaction_v2"],"CompactionPhases":["mid_turn","post_turn","pre_turn","standalone_turn"]}`)
	extended.ClientMetadata = json.RawMessage(`{"Constants":{"guardian_credits_requested":"true"}}`)
	extended.TurnState = json.RawMessage(`{"ResetOnAccountOwnerChange":true}`)
	extended.WebSocketRetry = json.RawMessage(`{"RetryableErrorCodes":["slow_down"],"RetryBudget":5,"FallbackTransport":"http"}`)
	extended.WebSocketContinuation = json.RawMessage(`{"ResetOn":["account_owner","auth_revision"]}`)
	return base, extended
}

func TestOptionalSectionsLeaveLegacyDigestsUnchanged(t *testing.T) {
	for _, version := range []string{"0.151.0", "0.154.0"} {
		paths, err := filepath.Glob(filepath.Join("testdata/snapshots", version, "*.json"))
		if err != nil || len(paths) == 0 {
			t.Fatalf("%s 快照缺失: %v", version, err)
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
			for index, field := range doc.optionalSectionFields() {
				if *field != nil {
					t.Fatalf("%s 不应含可选节 %s", path, OptionalSectionNames[index])
				}
			}
			// 官方摘要：文件名即 digest，规范化后重算必须一致。
			prepared, err := PrepareSnapshotForManifest(doc)
			if err != nil {
				t.Fatalf("%s 规范化失败: %v", path, err)
			}
			want := strings.TrimSuffix(filepath.Base(path), ".json")
			if prepared.Digest != want {
				t.Fatalf("%s 官方摘要漂移: %s", path, prepared.Digest)
			}
			// ProfileDigest 与 executable 摘要：往返后不含可选节的投影字节与改动前一致。
			spec, err := NewProfileSpec(doc)
			if err != nil {
				t.Fatal(err)
			}
			for _, section := range spec.CrossSections() {
				for _, name := range OptionalSectionNames {
					if section.Name == name {
						t.Fatalf("%s 旧画像 crossSections 不应出现 %s", path, name)
					}
				}
			}
			canon, err := CanonicalJSON(spec.ToSnapshot())
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range OptionalSectionNames {
				if strings.Contains(string(canon), `"`+name+`"`) {
					t.Fatalf("%s 旧画像规范化 JSON 不应出现 %s", path, name)
				}
			}
			executable, err := CompileExecutableProfile(spec)
			if err != nil {
				t.Fatalf("%s 编译可执行画像失败: %v", path, err)
			}
			if executable.Optional() != (OptionalSections{}) {
				t.Fatalf("%s 旧画像不应有可选节投影", path)
			}
		}
	}
}

func TestOptionalSectionsRoundTripAndEnterDigests(t *testing.T) {
	base, extended := withAllOptionalSections(t)
	baseSpec, err := NewProfileSpec(base)
	if err != nil {
		t.Fatal(err)
	}
	spec, err := NewProfileSpec(extended)
	if err != nil {
		t.Fatal(err)
	}
	roundTrip := spec.ToSnapshot()
	for index, field := range roundTrip.optionalSectionFields() {
		if *field == nil {
			t.Fatalf("往返丢失可选节 %s", OptionalSectionNames[index])
		}
	}
	baseDigest, err := baseSpec.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	digest, err := spec.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if baseDigest == digest {
		t.Fatal("可选节必须进入 ProfileDigest")
	}
	baseExecutable, err := CompileExecutableProfile(baseSpec)
	if err != nil {
		t.Fatal(err)
	}
	executable, err := CompileExecutableProfile(spec)
	if err != nil {
		t.Fatalf("含可选节的画像编译失败: %v", err)
	}
	if baseExecutable.Digest() == executable.Digest() {
		t.Fatal("可选节必须进入 executable 摘要")
	}
	optional := executable.Optional()
	if optional.CookieJar == nil || optional.WorkspaceRouting == nil || optional.TurnMetadata == nil ||
		optional.ClientMetadata == nil || optional.TurnState == nil || optional.WebSocketRetry == nil ||
		optional.WebSocketContinuation == nil {
		t.Fatal("可选节执行投影不完整")
	}
	if optional.WorkspaceRouting.NonDefaultAction != "fail_closed" || !optional.TurnMetadata.AnalyticsEnabled {
		t.Fatal("可选节取值未按原文解码")
	}
	prepared, err := PrepareSnapshotForManifest(extended)
	if err != nil {
		t.Fatalf("规范化含可选节的快照失败: %v", err)
	}
	if prepared.Digest == "" || prepared.Digest == base.Digest {
		t.Fatal("含可选节的快照必须得到新的官方摘要")
	}
}

func TestOptionalSectionsStrictDecoding(t *testing.T) {
	cases := map[string]struct {
		name string
		raw  string
	}{
		"显式 null":     {SectionTurnState, `null`},
		"未知字段":        {SectionTurnState, `{"ResetOnAccountOwnerChange":true,"Extra":1}`},
		"未排序":         {SectionCookieJar, `{"AllowedNames":["b","a"],"AllowedPrefixes":["cf_chl_"],"WebSocketHandshakeWriteBack":true}`},
		"重复取值":        {SectionWebSocketRetry, `{"RetryableErrorCodes":["slow_down","slow_down"],"RetryBudget":5,"FallbackTransport":"http"}`},
		"非法处置":        {SectionWorkspaceRouting, `{"DiscoveryEndpointID":"x","DefaultOrigin":"https://chatgpt.com","AcceptedOriginValues":["NO_CONSTRAINT"],"AcceptedOverrideValues":["NO_CONSTRAINT"],"OverrideHeader":"x-h","NonDefaultAction":"rewrite","RoutedEndpointIDs":["responses_http"]}`},
		"非法续接条件":      {SectionWebSocketContinuation, `{"ResetOn":["unknown_kind"]}`},
		"空常量":         {SectionClientMetadata, `{"Constants":{}}`},
		"TurnState 假": {SectionTurnState, `{"ResetOnAccountOwnerChange":false}`},
		"未知节名":        {"UnknownSection", `{}`},
		"尾随数据":        {SectionTurnState, `{"ResetOnAccountOwnerChange":true}{}`},
	}
	for label, item := range cases {
		if _, err := DecodeOptionalSection(item.name, json.RawMessage(item.raw)); err == nil {
			t.Fatalf("%s 应被拒绝", label)
		}
	}
}

func TestOptionalSectionRejectsNullInSnapshot(t *testing.T) {
	base, _ := withAllOptionalSections(t)
	base.TurnState = json.RawMessage(`null`)
	if _, err := PrepareSnapshotForManifest(base); err == nil {
		t.Fatal("快照中显式 null 的可选节应被拒绝")
	}
}

func TestEngineSupportsDeclaredConditionsAndSources(t *testing.T) {
	supported := EngineSupportedEnumValues()
	for _, value := range []string{
		string(ConditionGuardianReviewRequest),
		string(ConditionNotGuardianReviewRequest),
		string(ConditionAccountRoutingOverridePresent),
	} {
		if !supported.Contains(EnumDomainConditionKind, value) {
			t.Fatalf("引擎闭集缺少条件 %s", value)
		}
	}
	if !supported.Contains(EnumDomainValueSource, string(SourcePromptCacheKey)) {
		t.Fatal("引擎闭集缺少来源 prompt_cache_key")
	}
}

func TestWorkspaceRoutingRejectsUnknownEndpoint(t *testing.T) {
	_, extended := withAllOptionalSections(t)
	extended.WorkspaceRouting = json.RawMessage(`{"DiscoveryEndpointID":"no_such_endpoint","DefaultOrigin":"https://chatgpt.com","AcceptedOriginValues":["NO_CONSTRAINT"],"AcceptedOverrideValues":["NO_CONSTRAINT"],"OverrideHeader":"x-openai-account-routing-override","NonDefaultAction":"fail_closed","RoutedEndpointIDs":["responses_http"]}`)
	spec, err := NewProfileSpec(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := CompileExecutableProfile(spec); err == nil || !strings.Contains(err.Error(), "WorkspaceRouting 引用了未知 endpoint") {
		t.Fatalf("未知端点应被拒绝，实际: %v", err)
	}
}
