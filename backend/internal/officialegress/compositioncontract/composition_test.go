package compositioncontract_test

import (
	"encoding/json"
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

// releaseProfileForTest 取发布图中 purpose+mode 节点引用的不可变画像。
func releaseProfileForTest(
	t *testing.T,
	releases releasecontract.ReleaseGraph,
	snapshots profilecontract.SnapshotCatalog,
	purpose string,
	mode releasecontract.ReleaseMode,
) profilecontract.ProfileSpec {
	t.Helper()
	release, ok := releases.Resolve(purpose, mode)
	if !ok {
		t.Fatalf("发布坐标不存在: purpose=%s mode=%s", purpose, mode)
	}
	profile, ok := snapshots.Resolve(profilecontract.SnapshotKey{
		Version: release.Snapshot.Version, Digest: release.Snapshot.Digest,
	})
	if !ok {
		t.Fatalf("发布节点引用的画像不在测试快照索引中: %s/%s", release.Snapshot.Version, release.Snapshot.Digest)
	}
	return profile
}

// profileDeclaresRouteForTest 是测试侧独立实现的结构判定：画像是否声明了与 route 同 method、
// host、传输与 path 的端点（口径与 Composer 的端点匹配一致，含 server_returned_path 的证据
// 表达差异）。它不调用被测的 Compose，只用来决定某个 Sink 在某个发布上“应当”能否成包。
func profileDeclaresRouteForTest(profile profilecontract.ProfileSpec, route bindingcontract.RouteEvidenceDoc) bool {
	routePath := route.Path
	if routePath == "{server_returned_path}" {
		routePath = "/{server_returned_path}"
	}
	for _, endpoint := range profile.Endpoints() {
		transport := "http"
		if endpoint.Upgrade == "websocket" {
			transport = "websocket"
		}
		endpointPath := endpoint.Path
		if endpointPath == "{server_returned_path}" {
			endpointPath = "/{server_returned_path}"
		}
		if endpoint.Method == route.Method && endpoint.Host == route.Host &&
			transport == route.Transport && endpointPath == routePath {
			return true
		}
	}
	return false
}

// releaseRetiredRoutesForTest 读取运行时“发布退役 route”清单（catalogdata/release-route-retirements.json），
// 返回 method、host、path、transport 四元组集合；测试侧独立读取，不调用被测包的退役判定。
func releaseRetiredRoutesForTest(t *testing.T) map[string]bool {
	t.Helper()
	raw, err := os.ReadFile("../catalogdata/release-route-retirements.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Routes []struct {
			Method   string `json:"method"`
			Host     string `json:"host"`
			Path     string `json:"path"`
			Protocol string `json:"protocol"`
		} `json:"routes"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	retired := make(map[string]bool, len(manifest.Routes))
	for _, route := range manifest.Routes {
		retired[strings.Join([]string{route.Method, route.Host, route.Path, route.Protocol}, " ")] = true
	}
	return retired
}

// TestAllCodexBusinessBindingsJoinThreeEvidenceLayers 证明每个具备端点画像的 Codex 业务 Sink
// 都能把 binding、发布与画像三层证据拼成 EvidenceBundle。
//
// 改动前只在 Active 组合并要求 23 个 Sink 全部成功。VC-6 晋升后 Active 的目标画像删除了
// legacy compact 端点，只绑定该端点的 Sink 在 Active 下组合失败——这是版本 route 口径允许的
// “版本删除的端点在不含它的单个发布中零匹配”。现按结构事实核验，判别力不低于改动前：
//   - 逐 Sink 在 Active 组合：Active 画像声明了该 Sink 全部 route 时必须成功；否则必须恰以
//     “匹配 0 个端点”失败（结构上确实缺失，其余任何失败都报错）；
//   - Active 结构上缺失 route 的 Sink 必须在 Previous 组合成功（两槽位并集覆盖）；
//   - 组合成功的 bundle 匹配数必须等于 route 数，成功组合的业务 Sink 总数仍须等于 23。
//
// 候选期 Active 声明全部 route，上面的分支与改动前逐条相同。
//
// 最后一个声明某端点的画像离开 Active／Previous 后（legacy compact 在 0.154.0 离开之后），只绑定该端点、
// 且登记在发布退役清单里的 Sink 两种 mode 都必须恰以“匹配 0 个端点”失败，单独计数；成功组合数与退役数之和仍须等于 23。
func TestAllCodexBusinessBindingsJoinThreeEvidenceLayers(t *testing.T) {
	bindings := loadBindings(t)
	releases := loadReleases(t)
	snapshots := loadSnapshots(t)
	composer := c.NewComposer(bindings, releases, snapshots)
	retiredRoutes := releaseRetiredRoutesForTest(t)
	composed, retired := 0, 0
	for _, binding := range bindings.Bindings() {
		if binding.Persona != "codex-cli" || binding.Purpose == "facade" ||
			binding.EndpointEvidence != "codex_profile" {
			continue
		}
		purpose := releasePurposeForTest(binding)
		activeProfile := releaseProfileForTest(t, releases, snapshots, purpose, releasecontract.ReleaseModeActive)
		previousProfile := releaseProfileForTest(t, releases, snapshots, purpose, releasecontract.ReleaseModePrevious)
		allRetired := len(binding.Routes) > 0
		for _, route := range binding.Routes {
			if !retiredRoutes[strings.Join([]string{route.Method, route.Host, route.Path, route.Transport}, " ")] ||
				profileDeclaresRouteForTest(activeProfile, route) || profileDeclaresRouteForTest(previousProfile, route) {
				allRetired = false
			}
		}
		if allRetired {
			for _, mode := range []releasecontract.ReleaseMode{releasecontract.ReleaseModeActive, releasecontract.ReleaseModePrevious} {
				_, err := composer.Compose(c.CompositionRequest{SinkID: binding.SinkID, ReleasePurpose: purpose, Mode: mode})
				if err == nil || !strings.Contains(err.Error(), "匹配 0 个端点") {
					t.Errorf("%s 的 route 已发布退役，%s 组合必须以“匹配 0 个端点”失败，实际错误=%v", binding.SinkID, mode, err)
				}
			}
			retired++
			continue
		}
		declaredInActive := true
		for _, route := range binding.Routes {
			if !profileDeclaresRouteForTest(activeProfile, route) {
				declaredInActive = false
			}
		}
		bundle, err := composer.Compose(c.CompositionRequest{
			SinkID:         binding.SinkID,
			ReleasePurpose: purpose,
			Mode:           releasecontract.ReleaseModeActive,
		})
		switch {
		case declaredInActive:
			if err != nil {
				t.Errorf("组合 %s: %v", binding.SinkID, err)
				continue
			}
		case err == nil || !strings.Contains(err.Error(), "匹配 0 个端点"):
			t.Errorf("%s 的 route 在 Active 画像中结构上缺失，组合必须以“匹配 0 个端点”失败，实际错误=%v",
				binding.SinkID, err)
			continue
		default:
			bundle, err = composer.Compose(c.CompositionRequest{
				SinkID:         binding.SinkID,
				ReleasePurpose: purpose,
				Mode:           releasecontract.ReleaseModePrevious,
			})
			if err != nil {
				t.Errorf("%s 在 Active 画像中缺少端点，且无法在 Previous 组合（并集覆盖失败）：%v", binding.SinkID, err)
				continue
			}
		}
		if len(bundle.EndpointMatches()) != len(binding.Routes) {
			t.Errorf("%s 的 route/endpoint 匹配数量不一致", binding.SinkID)
		}
		composed++
	}
	if composed+retired != 23 {
		t.Fatalf("成功组合具备端点画像的 Codex 业务 Sink=%d、发布退役=%d，合计期望 23", composed, retired)
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
	//
	// 夹具维护说明：Composer 要从测试快照索引（profilecontract/testdata/snapshot-catalog.json）
	// 解析发布节点引用的画像，而该索引由目录暂存工具按运行目录重建，画像随版本退休后即从
	// 索引消失。夹具因此只引用索引中仍存在的画像：active 取当前 Active 发布的真实节点，
	// previous 与之同版本、同画像快照、同 transport，只把终端 token 换成 xterm-256color
	// 并使用显式标注为夹具来源的 Build/Wire 身份。被测语义与改动前完全一致——版本号和
	// 画像快照都相同，唯一的差异来自 purpose+mode 坐标下的 Build 身份。该画像退休时按同一
	// 方式换到索引中仍存在的画像即可，不得改成两份不同画像（那会让 Bundle digest 天然不同）。
	// releasecontract 的同名夹具只做发布图解析、不查快照索引，保留为升级前冻结的历史夹具，
	// 两份夹具不必同步。
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
	// 同画像快照同样是本用例的前提（见上方说明）：锁定它，避免夹具被换成两份不同画像后
	// 下面的 Bundle digest 断言因画像差异而天然成立。
	if active.Release().Snapshot != previous.Release().Snapshot {
		t.Fatal("测试前提改变：active/previous 应当引用同一画像快照")
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
