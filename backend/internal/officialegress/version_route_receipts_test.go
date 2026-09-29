package officialegress

import (
	"io/fs"
	"net/url"
	"path"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

func TestVersionRouteFrozenProfileIsHistoricalOnly(t *testing.T) {
	const retiredDigest = "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc"
	executable, archived, err := loadVersionRouteFrozenProfile(retiredDigest)
	if err != nil || !archived || executable.Version() != "0.147.0" {
		t.Fatalf("历史 version-route 冻结画像无效：archived=%v version=%s err=%v",
			archived, executable.Version(), err)
	}
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, resolveErr := DefaultReleaseCatalog().Resolve(mode)
		if resolveErr != nil {
			t.Fatal(resolveErr)
		}
		if release.ProfileDigest() == retiredDigest || release.Version() == "0.147.0" {
			t.Fatalf("历史证明画像被生产 selector 选中：mode=%s version=%s", mode, release.Version())
		}
	}
	if _, found, loadErr := loadVersionRouteFrozenProfile(strings.Repeat("1", 64)); loadErr != nil || found {
		t.Fatalf("未知历史画像未失败关闭：found=%v err=%v", found, loadErr)
	}
}

// 每条版本 route 都进入最终 Catalog；在包含该端点的 mode 中生成对应 EndpointPlan，在
// 不含该端点的 mode 中零匹配（版本新增 route 可以只属于其中一个 Release）。
func TestVersionRouteReceiptBindsCurrentProfilesThatContainEndpoint(t *testing.T) {
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := DefaultSinkCatalog().Resolve(SinkCodexQuotaWHAM)
	// WHAM 历史 binding 3 条 route，另由 settings/user 与 accounts/check 两条版本 route 收据追加。
	if !ok || len(binding.Routes()) != 5 {
		t.Fatalf("WHAM 版本 route 未进入最终 Catalog：%+v", binding.Routes())
	}
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	for _, document := range manifest.Receipts {
		if SinkID(document.SinkID) != SinkCodexQuotaWHAM {
			continue
		}
		target, err := url.Parse("https://" + document.Route.Route.Host + document.Route.Route.Path)
		if err != nil {
			t.Fatal(err)
		}
		containing := 0
		for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
			release, err := DefaultReleaseCatalog().Resolve(mode)
			if err != nil {
				t.Fatal(err)
			}
			targetBundle := versionRouteResolveBundle(t, resolver, mode)
			plan, planErr := targetBundle.ResolveEndpointPlan(
				SinkCodexQuotaWHAM, document.Route.Route.Method, target, WireProtocolHTTP,
			)
			if !versionRouteProfileHasEndpoint(release, document.Route.EvidenceID) {
				if planErr == nil {
					t.Fatalf("%s/%s 画像不含 %s 却生成了 EndpointPlan", mode, release.Version(), document.Route.EvidenceID)
				}
				continue
			}
			containing++
			if planErr != nil || plan.EndpointID() != document.Route.EvidenceID {
				t.Fatalf("%s/%s 画像未生成 %s EndpointPlan：plan=%+v err=%v",
					mode, targetBundle.Version(), document.Route.EvidenceID, plan, planErr)
			}
		}
		if containing == 0 {
			t.Fatalf("版本 route %s 在 Active/Previous 中均无 EndpointPlan", document.Route.Route.Path)
		}
	}
}

func TestVersionRouteReceiptFailsClosedOnMutations(t *testing.T) {
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	var acceptedCanary *receiptcontract.ArtifactRef
	for _, document := range manifest.Receipts {
		if document.CanaryAcceptance != nil {
			acceptedCanary = document.CanaryAcceptance
			break
		}
	}
	if acceptedCanary == nil {
		t.Fatal("清单缺少带生产 canary acceptance 的历史收据")
	}
	tests := []struct {
		name   string
		mutate func(*versionRouteReceiptDoc)
	}{
		{name: "prior receipt", mutate: func(document *versionRouteReceiptDoc) {
			document.PriorReceiptDigest = strings.Repeat("0", 64)
		}},
		{name: "profile digest", mutate: func(document *versionRouteReceiptDoc) {
			document.ProfileDigests = []string{strings.Repeat("1", 64)}
		}},
		{name: "endpoint evidence", mutate: func(document *versionRouteReceiptDoc) {
			document.Route.EvidenceID = "wham_usage"
		}},
		{name: "route absent from every profile", mutate: func(document *versionRouteReceiptDoc) {
			document.Route.Route.Path = "/backend-api/wham/not-present"
		}},
		{name: "artifact digest", mutate: func(document *versionRouteReceiptDoc) {
			document.Route.WireFixture.SHA256 = strings.Repeat("2", 64)
		}},
		{name: "canary acceptance swapped", mutate: func(document *versionRouteReceiptDoc) {
			// 候选态收据挂上别的 route 的真实验收，已验收收据的验收被移除变成候选态：
			// 前者 canary 与本收据不一致，后者绑定的是已退休的冻结历史画像，都必须失败关闭。
			if document.CanaryAcceptance == nil {
				swapped := *acceptedCanary
				document.CanaryAcceptance = &swapped
				return
			}
			document.CanaryAcceptance = nil
		}},
	}
	for index := range manifest.Receipts {
		document := manifest.Receipts[index]
		t.Run(document.Route.Route.Path+"/unmodified", func(t *testing.T) {
			appended, evidence, input := versionRouteInputBefore(t, index)
			applied, err := applyVersionRouteReceipts(appended, evidence, []SinkBindingInput{input})
			if err != nil {
				t.Fatalf("未篡改的版本 route 收据链无法重放：%v", err)
			}
			final, _ := DefaultSinkCatalog().Resolve(SinkID(document.SinkID))
			if applied[0].migrationReceipt.Digest() != final.migrationReceipt.Digest() ||
				len(applied[0].Routes) != len(final.Routes()) {
				t.Fatal("重放版本 route 收据链后未回到最终 Catalog 状态")
			}
		})
		for _, test := range tests {
			t.Run(document.Route.Route.Path+"/"+test.name, func(t *testing.T) {
				appended, evidence, input := versionRouteInputBefore(t, index)
				mutated := appended.Receipts[0]
				test.mutate(&mutated)
				appended.Receipts = []versionRouteReceiptDoc{mutated}
				if _, err := applyVersionRouteReceipts(appended, evidence, []SinkBindingInput{input}); err == nil {
					t.Fatal("被篡改的版本 route 收据未失败关闭")
				}
			})
		}
	}
}

func TestVersionRouteReceiptArtifactsBindHistoricalAuthority(t *testing.T) {
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	for _, document := range manifest.Receipts {
		if err := verifyVersionRouteReceiptArtifacts(
			versionRouteReceiptFS, document,
			"codex_executor", "wrong-authority", "wrong-issuer",
		); err == nil {
			t.Fatalf("版本 route 执行产物未绑定历史 authority：%s", document.Route.Route.Path)
		}
	}
}

// 清单顺序即追加顺序：同一 Sink 内后追加的 route 字典序可以更小，但顺序颠倒会让
// prior_receipt_digest 链断开；重复 route 与 Sink 分组乱序在加载时就失败关闭。
func TestVersionRouteReceiptOrderFollowsPerSinkAppendChain(t *testing.T) {
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateVersionRouteReceiptOrder(manifest.Receipts); err != nil {
		t.Fatal(err)
	}
	duplicate := append(append([]versionRouteReceiptDoc(nil), manifest.Receipts...), manifest.Receipts[0])
	if validateVersionRouteReceiptOrder(duplicate) == nil {
		t.Fatal("同一 Sink 重复登记同一 route 未失败关闭")
	}
	misplaced := manifest.Receipts[0]
	misplaced.SinkID = "codex.a"
	if validateVersionRouteReceiptOrder(append(
		append([]versionRouteReceiptDoc(nil), manifest.Receipts...), misplaced,
	)) == nil {
		t.Fatal("Sink 分组乱序未失败关闭")
	}

	first := -1
	for index, document := range manifest.Receipts {
		if SinkID(document.SinkID) == SinkCodexQuotaWHAM {
			first = index
			break
		}
	}
	appended, evidence, input := versionRouteInputBefore(t, first)
	if len(appended.Receipts) < 2 {
		t.Fatal("WHAM 缺少可用于颠倒顺序的多条版本 route 收据")
	}
	swapped := append([]versionRouteReceiptDoc(nil), appended.Receipts...)
	swapped[0], swapped[len(swapped)-1] = swapped[len(swapped)-1], swapped[0]
	appended.Receipts = swapped
	if validateVersionRouteReceiptOrder(swapped) != nil {
		t.Fatal("同一 Sink 内的顺序不应由形状校验拒绝")
	}
	if _, err := applyVersionRouteReceipts(appended, evidence, []SinkBindingInput{input}); err == nil {
		t.Fatal("颠倒追加顺序的版本 route 收据链未失败关闭")
	}
}

// 候选态收据只准备 wire fixture 与 execution verification；canary acceptance 由生产 canary
// 实测后补齐，产物目录里不得出现占位验收文件，且只能绑定当前 Active／Previous 画像。
func TestVersionRouteCandidateReceiptDefersCanaryAcceptance(t *testing.T) {
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	for _, document := range manifest.Receipts {
		if document.CanaryAcceptance != nil {
			continue
		}
		directory := path.Dir(document.Route.WireFixture.Path)
		if path.Dir(document.Route.ExecutionVerification.Path) != directory {
			t.Fatalf("候选态收据产物不在同一目录：%s", document.Route.Route.Path)
		}
		entries, err := fs.ReadDir(versionRouteReceiptFS, directory)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			if strings.Contains(entry.Name(), "canary") {
				t.Fatalf("候选态收据目录出现 canary 占位产物：%s/%s", directory, entry.Name())
			}
		}
		if err := requireVersionRouteCandidateProfiles(document); err != nil {
			t.Fatal(err)
		}
	}
}

func versionRouteProfileHasEndpoint(release ResolvedCodexRelease, endpointID string) bool {
	for _, endpoint := range release.ExecutableProfile().Endpoints() {
		if endpoint.ID == endpointID {
			return true
		}
	}
	return false
}

func versionRouteResolveBundle(t *testing.T, resolver *BundleResolver, mode ReleaseMode) ReleaseBundle {
	t.Helper()
	bundle, err := resolver.Resolve(BundleResolveRequest{
		SinkID: SinkCodexQuotaWHAM, Mode: mode,
		Execution: ExecutionPolicy{
			ID: "version-route-test-execution", Source: "test", MaxAttempts: 1,
			Replayable: true, ConcurrencyLimit: 1,
		},
		Deployment: DeploymentSupportPolicy{
			ID: "version-route-test-deployment", Source: "test", Platform: "linux/amd64",
			ProxyMode: "direct", SupportedBackends: []BackendKind{BackendHTTPUpstream},
		},
		Behavior: BehaviorPolicy{
			ID: "version-route-test-behavior", Source: "test",
			Kind: BehaviorUserRequest, AttemptBudget: 1,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return bundle
}

// versionRouteInputBefore 返回清单第 index 条收据应用之前的 binding，以及该 Sink 自这一条起
// 的全部追加收据（按清单顺序）。
func versionRouteInputBefore(
	t *testing.T,
	index int,
) (versionRouteReceiptManifest, map[string]bindingcontract.ReleaseBindingDoc, SinkBindingInput) {
	t.Helper()
	manifest, err := loadVersionRouteReceiptManifest()
	if err != nil || index < 0 || index >= len(manifest.Receipts) {
		t.Fatalf("加载版本 route 收据：index=%d manifest=%+v err=%v", index, manifest, err)
	}
	document := manifest.Receipts[index]
	appended := make([]versionRouteReceiptDoc, 0, len(manifest.Receipts)-index)
	for _, candidate := range manifest.Receipts[index:] {
		if candidate.SinkID == document.SinkID {
			appended = append(appended, candidate)
		}
	}
	binding, ok := DefaultSinkCatalog().Resolve(SinkID(document.SinkID))
	if !ok || binding.migrationReceipt == nil {
		t.Fatal("最终 Catalog 缺少版本 route 目标")
	}
	input := versionRouteRestoreBefore(t, binding, appended)

	bindingDocument, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := bindingcontract.NewBindingCatalog(bindingDocument)
	if err != nil {
		t.Fatal(err)
	}
	evidence, ok := catalog.Resolve(document.SinkID)
	if !ok {
		t.Fatalf("ReleaseBinding 缺少 %s 证据", document.SinkID)
	}
	manifest.Receipts = appended
	return manifest, map[string]bindingcontract.ReleaseBindingDoc{document.SinkID: evidence}, input
}
