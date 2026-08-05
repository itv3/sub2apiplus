package bindingcontract_test

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"

	b "github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

func loadBaseline(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("../../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func loadBindingCatalog(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("testdata/release-bindings.json")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func mustCatalog(t *testing.T) b.BindingCatalog {
	t.Helper()
	doc, err := b.ParseBindingCatalog(loadBindingCatalog(t))
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := b.NewBindingCatalog(doc)
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

func TestBindingCatalogIsExactProjectionOfBaseline(t *testing.T) {
	generated, err := b.BuildBindingCatalogDoc(loadBaseline(t))
	if err != nil {
		t.Fatal(err)
	}
	checkedIn, err := b.ParseBindingCatalog(loadBindingCatalog(t))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(generated, checkedIn) {
		t.Fatal("提交的 ReleaseBinding 目录不是当前 sink-baseline.json 的确定性投影")
	}

	catalog, err := b.NewBindingCatalog(generated)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(catalog.Bindings()); got != 34 {
		t.Fatalf("ReleaseBinding 数量=%d，期望 34", got)
	}
	if got := catalog.Source().BoundCandidates; got != 47 {
		t.Fatalf("绑定候选数量=%d，期望 47", got)
	}
	if bytes.Contains(loadBindingCatalog(t), []byte(`"retry"`)) {
		t.Fatal("ReleaseBinding 凭空承载了基线中不存在的 RetryPolicy")
	}
}

func TestBindingCatalogPreservesIdentityAndBackendEvidence(t *testing.T) {
	catalog := mustCatalog(t)

	for _, sinkID := range []string{
		"codex.usage.probe",
		"unclassified.agent.task_register",
		"unclassified.pat.whoami",
	} {
		binding, ok := catalog.Resolve(sinkID)
		if !ok {
			t.Fatalf("缺少 P0 Sink %s", sinkID)
		}
		if binding.TargetBackend != "http_upstream" {
			t.Fatalf("%s 目标 backend=%s，期望 http_upstream", sinkID, binding.TargetBackend)
		}
		plain := false
		for _, candidate := range binding.Candidates {
			plain = plain || candidate.ActualBackend == "plain_net_http"
		}
		if !plain {
			t.Fatalf("%s 丢失了当前 plain_net_http 旁路证据", sinkID)
		}
	}

	privacy, ok := catalog.Resolve("web.privacy.disable_training")
	if !ok || privacy.Persona != "chatgpt-web" || privacy.TargetBackend != "req_profile" ||
		privacy.EndpointEvidence != "external_persona" {
		t.Fatalf("Chrome persona 绑定失真: %+v", privacy)
	}

	exchange, ok := catalog.Resolve("codex.oauth.exchange")
	if !ok || exchange.EndpointEvidence != "transport_only" {
		t.Fatalf("OAuth exchange 不得冒充已有端点画像: %+v", exchange)
	}
	refresh, ok := catalog.Resolve("codex.oauth.refresh")
	if !ok || refresh.EndpointEvidence != "codex_profile" {
		t.Fatalf("OAuth refresh 应由现有画像端点支撑: %+v", refresh)
	}
	if _, ok := catalog.Resolve("codex.facade.oauth_client"); !ok {
		t.Fatal("共享 OAuth 客户端工厂必须作为 facade，而不能任意归给 refresh")
	}

	ws, ok := catalog.Resolve("codex.responses.ws")
	if !ok || len(ws.Routes) != 1 {
		t.Fatalf("Responses WS 绑定缺失: %+v", ws)
	}
	if ws.Routes[0].Method != "GET" || ws.Routes[0].Transport != "websocket" {
		t.Fatalf("Responses WS route 拆分错误: %+v", ws.Routes[0])
	}

	blob, ok := catalog.Resolve("codex.files.blob_upload")
	if !ok || len(blob.Routes) != 1 || blob.Routes[0].Host != "*.oaiusercontent.com" ||
		blob.Routes[0].Path != "/{server_returned_path}" {
		t.Fatalf("动态 blob route 证据失真: %+v", blob)
	}
}

func TestBindingCatalogIsDeeplyImmutable(t *testing.T) {
	catalog := mustCatalog(t)
	before, err := catalog.Digest()
	if err != nil {
		t.Fatal(err)
	}

	bindings := catalog.Bindings()
	bindings[0].SinkID = "polluted"
	if len(bindings[0].Routes) > 0 {
		bindings[0].Routes[0].Path = "/polluted"
	}
	bindings[0].Candidates[0].ResolvedHosts = append(bindings[0].Candidates[0].ResolvedHosts, "polluted.example")
	bindings[0].Candidates[0].ResolvedMethods = append(bindings[0].Candidates[0].ResolvedMethods, "DELETE")
	bindings[0].Candidates[0].ResolvedPaths = append(bindings[0].Candidates[0].ResolvedPaths, "/polluted")
	bindings[0].Candidates[0].ResolvedTargets = append(bindings[0].Candidates[0].ResolvedTargets, "https://polluted.example/")
	bindings[0].Candidates[0].BuildContexts[0] = "polluted/context"
	source := catalog.Source()
	source.BuildContexts[0] = "polluted/context"

	resolved, ok := catalog.Resolve("codex.usage.probe")
	if !ok {
		t.Fatal("测试前提缺少 codex.usage.probe")
	}
	resolved.Candidates[0].Rationale = "polluted"
	after, err := catalog.Digest()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("修改 getter 返回值污染了不可变 BindingCatalog")
	}
}

func TestBindingCatalogRejectsInvalidEvidence(t *testing.T) {
	t.Run("同一 Sink 分类冲突", func(t *testing.T) {
		baseline, err := b.ParseSinkBaseline(loadBaseline(t))
		if err != nil {
			t.Fatal(err)
		}
		for i := range baseline.Sinks {
			if baseline.Sinks[i].RuntimeSinkID == "codex.models.list" {
				baseline.Sinks[i].Purpose = "conflicting-purpose"
				break
			}
		}
		raw, err := json.Marshal(baseline)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := b.BuildBindingCatalogDoc(raw); err == nil {
			t.Fatal("同一 Sink 的 purpose 冲突未被拒绝")
		}
	})

	t.Run("未分类身份禁止 enforce", func(t *testing.T) {
		baseline, err := b.ParseSinkBaseline(loadBaseline(t))
		if err != nil {
			t.Fatal(err)
		}
		for i := range baseline.Sinks {
			if baseline.Sinks[i].Persona == "unclassified" {
				baseline.Sinks[i].EnforcementState = "enforced"
			}
		}
		raw, err := json.Marshal(baseline)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := b.BuildBindingCatalogDoc(raw); err == nil {
			t.Fatal("unclassified persona 进入 enforce 未被拒绝")
		}
	})

	t.Run("缺少端点证据状态", func(t *testing.T) {
		baseline, err := b.ParseSinkBaseline(loadBaseline(t))
		if err != nil {
			t.Fatal(err)
		}
		for i := range baseline.Sinks {
			if baseline.Sinks[i].RuntimeSinkID == "codex.oauth.exchange" {
				baseline.Sinks[i].EndpointEvidence = ""
			}
		}
		raw, err := json.Marshal(baseline)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := b.BuildBindingCatalogDoc(raw); err == nil {
			t.Fatal("缺少 endpoint_evidence 未被拒绝")
		}
	})

	t.Run("route 原文与拆分值不一致", func(t *testing.T) {
		doc, err := b.ParseBindingCatalog(loadBindingCatalog(t))
		if err != nil {
			t.Fatal(err)
		}
		for i := range doc.Bindings {
			if len(doc.Bindings[i].Routes) > 0 {
				doc.Bindings[i].Routes[0].Method = "DELETE"
				break
			}
		}
		if _, err := b.NewBindingCatalog(doc); err == nil {
			t.Fatal("route 拆分值漂移未被拒绝")
		}
	})

	t.Run("未知字段与尾随 JSON", func(t *testing.T) {
		raw := loadBindingCatalog(t)
		unknown := bytes.Replace(raw, []byte(`"schema_version": 1`), []byte(`"schema_version": 1, "unknown": true`), 1)
		if _, err := b.ParseBindingCatalog(unknown); err == nil {
			t.Fatal("未知字段未被拒绝")
		}
		if _, err := b.ParseBindingCatalog(append(raw, []byte(`{}`)...)); err == nil {
			t.Fatal("尾随 JSON 未被拒绝")
		}
	})
}
