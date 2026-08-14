package compositioncontract_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	c "github.com/Wei-Shaw/sub2api/internal/officialegress/compositioncontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

func loadBindings(t *testing.T) bindingcontract.BindingCatalog {
	t.Helper()
	raw, err := os.ReadFile("../bindingcontract/testdata/release-bindings.json")
	if err != nil {
		t.Fatal(err)
	}
	doc, err := bindingcontract.ParseBindingCatalog(raw)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := bindingcontract.NewBindingCatalog(doc)
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

func loadReleasesFrom(t *testing.T, path string) releasecontract.ReleaseGraph {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	doc, err := releasecontract.ParseReleaseGraph(raw)
	if err != nil {
		t.Fatal(err)
	}
	graph, err := releasecontract.NewReleaseGraph(doc)
	if err != nil {
		t.Fatal(err)
	}
	return graph
}

func loadReleases(t *testing.T) releasecontract.ReleaseGraph {
	t.Helper()
	return loadReleasesFrom(t, "../releasecontract/testdata/release-graph.json")
}

func loadSnapshots(t *testing.T) profilecontract.SnapshotCatalog {
	t.Helper()
	root := "../profilecontract/testdata"
	raw, err := os.ReadFile(filepath.Join(root, "snapshot-catalog.json"))
	if err != nil {
		t.Fatal(err)
	}
	doc, err := profilecontract.ParseSnapshotCatalog(raw)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := profilecontract.NewSnapshotCatalog(doc, func(relativePath string) ([]byte, error) {
		return os.ReadFile(filepath.Join(root, relativePath))
	})
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

func mustComposer(t *testing.T) c.Composer {
	t.Helper()
	return c.NewComposer(loadBindings(t), loadReleases(t), loadSnapshots(t))
}

func mustSameVersionComposer(t *testing.T) c.Composer {
	t.Helper()
	return c.NewComposer(
		loadBindings(t),
		loadReleasesFrom(t, "testdata/release-graph-same-version.json"),
		loadSnapshots(t),
	)
}

func releasePurposeForTest(binding bindingcontract.ReleaseBindingDoc) string {
	if binding.TargetBackend == "websocket" {
		return c.CodexOAuthWSReleasePurpose
	}
	return c.CodexOAuthHTTPReleasePurpose
}

func TestAllCodexBusinessBindingsJoinThreeEvidenceLayers(t *testing.T) {
	bindings := loadBindings(t)
	composer := c.NewComposer(bindings, loadReleases(t), loadSnapshots(t))
	composed := 0
	for _, binding := range bindings.Bindings() {
		if binding.Persona != "codex-cli" || binding.Purpose == "facade" ||
			binding.EndpointEvidence != "codex_profile" {
			continue
		}
		bundle, err := composer.Compose(c.CompositionRequest{
			SinkID:         binding.SinkID,
			ReleasePurpose: releasePurposeForTest(binding),
			Mode:           releasecontract.ReleaseModeActive,
		})
		if err != nil {
			t.Errorf("组合 %s: %v", binding.SinkID, err)
			continue
		}
		if len(bundle.EndpointMatches()) != len(binding.Routes) {
			t.Errorf("%s 的 route/endpoint 匹配数量不一致", binding.SinkID)
		}
		composed++
	}
	if composed != 23 {
		t.Fatalf("成功组合具备端点画像的 Codex 业务 Sink=%d，期望 23", composed)
	}
}

func TestOAuthExchangeCannotMasqueradeAsRefreshEndpoint(t *testing.T) {
	bindings := loadBindings(t)
	exchange, ok := bindings.Resolve("codex.oauth.exchange")
	if !ok {
		t.Fatal("缺少 OAuth exchange 绑定")
	}
	if exchange.EndpointEvidence != "transport_only" {
		t.Fatalf("OAuth exchange 证据状态=%s，期望 transport_only", exchange.EndpointEvidence)
	}
	_, err := mustComposer(t).Compose(c.CompositionRequest{
		SinkID:         exchange.SinkID,
		ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
		Mode:           releasecontract.ReleaseModeActive,
	})
	if err == nil || !strings.Contains(err.Error(), "不能冒充画像端点") {
		t.Fatalf("OAuth exchange 被错误组合成 refresh 端点，错误=%v", err)
	}
}

func TestPurposeAndModeRemainExplicitCoordinates(t *testing.T) {
	// 该性质必须在同版本、同 Snapshot 下验证，避免混版本夹具让 Build/Wire
	// 身份差异天然成立而失去检出能力。
	composer := mustSameVersionComposer(t)
	active, err := composer.Compose(c.CompositionRequest{
		SinkID:         "codex.responses.forward",
		ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
		Mode:           releasecontract.ReleaseModeActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	previous, err := composer.Compose(c.CompositionRequest{
		SinkID:         "codex.responses.forward",
		ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
		Mode:           releasecontract.ReleaseModePrevious,
	})
	if err != nil {
		t.Fatal(err)
	}
	if active.Release().Build.Version != previous.Release().Build.Version {
		t.Fatal("测试前提改变：active/previous 应当版本号相同")
	}
	if active.Release().Build.ID == previous.Release().Build.ID {
		t.Fatal("purpose+mode 被错误折叠成版本号：active/previous BuildID 相同")
	}
	if active.Release().Build.RuntimeHeaders == nil || active.Release().Wire.StaticHeaders == nil {
		t.Fatal("组合时把发布图的非 nil 空数组改成了 null")
	}
	activeDigest, err := active.Digest()
	if err != nil {
		t.Fatal(err)
	}
	previousDigest, err := previous.Digest()
	if err != nil {
		t.Fatal(err)
	}
	if activeDigest == previousDigest {
		t.Fatal("active/previous 的证据 Bundle digest 不应相同")
	}
}

func TestCompositionRejectsImplicitOrInconsistentSelection(t *testing.T) {
	composer := mustComposer(t)
	tests := []struct {
		name    string
		request c.CompositionRequest
		want    string
	}{
		{
			name: "HTTP Sink 误选 WS 发布",
			request: c.CompositionRequest{SinkID: "codex.responses.forward",
				ReleasePurpose: c.CodexOAuthWSReleasePurpose, Mode: releasecontract.ReleaseModeActive},
			want: "需要 http",
		},
		{
			name: "WS Sink 误选 HTTP 发布",
			request: c.CompositionRequest{SinkID: "codex.responses.ws",
				ReleasePurpose: c.CodexOAuthHTTPReleasePurpose, Mode: releasecontract.ReleaseModeActive},
			want: "需要 websocket",
		},
		{
			name: "发布 purpose 不得省略",
			request: c.CompositionRequest{SinkID: "codex.responses.forward",
				Mode: releasecontract.ReleaseModeActive},
			want: "缺少合法",
		},
		{
			name: "非空未知发布 purpose 必须显式失败",
			request: c.CompositionRequest{SinkID: "codex.responses.forward",
				ReleasePurpose: "openai_oauth_unknown", Mode: releasecontract.ReleaseModeActive},
			want: "发布坐标不存在",
		},
		{
			name: "Chrome persona 不得套 Codex Release",
			request: c.CompositionRequest{SinkID: "web.privacy.disable_training",
				ReleasePurpose: c.CodexOAuthHTTPReleasePurpose, Mode: releasecontract.ReleaseModeActive},
			want: "不能组合",
		},
		{
			name: "未分类 persona 不得套 Codex Release",
			request: c.CompositionRequest{SinkID: "unclassified.pat.whoami",
				ReleasePurpose: c.CodexOAuthHTTPReleasePurpose, Mode: releasecontract.ReleaseModeActive},
			want: "不能组合",
		},
		{
			name: "共享 facade 不得生成业务 Bundle",
			request: c.CompositionRequest{SinkID: "codex.facade.upstream",
				ReleasePurpose: c.CodexOAuthHTTPReleasePurpose, Mode: releasecontract.ReleaseModeActive},
			want: "业务调用点",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := composer.Compose(test.request)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("错误=%v，期望包含 %q", err, test.want)
			}
		})
	}
}

func TestCompositionRejectsUnregisteredTargetBackend(t *testing.T) {
	bindings := loadBindings(t).ToDoc()
	found := false
	for index := range bindings.Bindings {
		if bindings.Bindings[index].SinkID != "codex.responses.forward" {
			continue
		}
		bindings.Bindings[index].TargetBackend = "future_unregistered_backend"
		found = true
		break
	}
	if !found {
		t.Fatal("测试夹具缺少 codex.responses.forward")
	}

	catalog, err := bindingcontract.NewBindingCatalog(bindings)
	if err != nil {
		t.Fatalf("构造带未登记 backend 的证据目录: %v", err)
	}
	composer := c.NewComposer(catalog, loadReleases(t), loadSnapshots(t))
	_, err = composer.Compose(c.CompositionRequest{
		SinkID:         "codex.responses.forward",
		ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
		Mode:           releasecontract.ReleaseModeActive,
	})
	if err == nil || !strings.Contains(err.Error(), "不能映射到 Codex OAuth wire transport") {
		t.Fatalf("未登记 TargetBackend 未被显式拒绝，错误=%v", err)
	}
}

func TestCompositionRequiresSnapshotAndEndpointEvidence(t *testing.T) {
	t.Run("发布引用的快照必须存在", func(t *testing.T) {
		releases := loadReleases(t).ToDoc()
		for i := range releases.Nodes {
			releases.Nodes[i].Snapshot.Digest = strings.Repeat("a", 64)
		}
		graph, err := releasecontract.NewReleaseGraph(releases)
		if err != nil {
			t.Fatal(err)
		}
		composer := c.NewComposer(loadBindings(t), graph, loadSnapshots(t))
		_, err = composer.Compose(c.CompositionRequest{
			SinkID:         "codex.responses.forward",
			ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
			Mode:           releasecontract.ReleaseModeActive,
		})
		if err == nil || !strings.Contains(err.Error(), "不可变画像不存在") {
			t.Fatalf("错误=%v", err)
		}
	})

	t.Run("业务 route 必须在画像中唯一存在", func(t *testing.T) {
		bindings := loadBindings(t).ToDoc()
		for i := range bindings.Bindings {
			if bindings.Bindings[i].SinkID == "codex.responses.forward" {
				bindings.Bindings[i].Routes[0] = bindingcontract.RouteEvidenceDoc{
					Raw: "POST chatgpt.com/backend-api/codex/missing", Method: "POST",
					Host: "chatgpt.com", Path: "/backend-api/codex/missing", Transport: "http",
				}
			}
		}
		catalog, err := bindingcontract.NewBindingCatalog(bindings)
		if err != nil {
			t.Fatal(err)
		}
		composer := c.NewComposer(catalog, loadReleases(t), loadSnapshots(t))
		_, err = composer.Compose(c.CompositionRequest{
			SinkID:         "codex.responses.forward",
			ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
			Mode:           releasecontract.ReleaseModeActive,
		})
		if err == nil || !strings.Contains(err.Error(), "匹配 0 个端点") {
			t.Fatalf("错误=%v", err)
		}
	})
}

func TestEvidenceBundleIsDeeplyImmutable(t *testing.T) {
	bundle, err := mustComposer(t).Compose(c.CompositionRequest{
		SinkID:         "codex.responses.forward",
		ReleasePurpose: c.CodexOAuthHTTPReleasePurpose,
		Mode:           releasecontract.ReleaseModeActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	before, err := bundle.Digest()
	if err != nil {
		t.Fatal(err)
	}
	binding := bundle.Binding()
	binding.Routes[0].Path = "/polluted"
	binding.Candidates[0].BuildContexts[0] = "polluted/context"
	binding.Candidates[0].ResolvedHosts = append(binding.Candidates[0].ResolvedHosts, "polluted.example")
	binding.Candidates[0].ResolvedMethods = append(binding.Candidates[0].ResolvedMethods, "DELETE")
	binding.Candidates[0].ResolvedPaths = append(binding.Candidates[0].ResolvedPaths, "/polluted")
	release := bundle.Release()
	release.Build.RuntimeHeaders = append(release.Build.RuntimeHeaders, releasecontract.HeaderValueDoc{Name: "x", Value: "y"})
	matches := bundle.EndpointMatches()
	matches[0].EndpointID = "polluted"
	after, err := bundle.Digest()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("修改 EvidenceBundle getter 返回值污染了内部证据")
	}
}
