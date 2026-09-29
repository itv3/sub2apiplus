package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

const versionRouteReceiptOutputEnv = "VERSION_ROUTE_RECEIPT_OUTPUT"

// versionRouteCaptureRouteIndex 只用作合成捕获 InvocationID 的标签；取固定值，保证清单
// 落库后再次生成时逐字节复现。
const versionRouteCaptureRouteIndex = 3

// versionRouteGenerationSpec 描述一条由生成器维护的版本新增 route 收据。
//
// 生成器只产出候选态收据：wire fixture 与 execution verification 来自正式 Compiler、
// Executor、adapter 与 terminal Guard 的合成无外部流量捕获；生产 canary acceptance 必须
// 由生产 canary 实测产生，生成器既不生成、也不写占位文件。
type versionRouteGenerationSpec struct {
	sinkID   SinkID
	method   string
	host     string
	path     string
	protocol WireProtocol
	// artifactDirectory 是产物相对 catalogdata 的目录。每条 route 独占一个目录，
	// 不覆盖同一 Sink 既有收据的产物。
	artifactDirectory string
	// reviewTopic 与 rationale 不写版本号；版本号在生成时取自包含该端点的画像。
	reviewTopic string
	rationale   string
}

// versionRouteGenerationSpecs 列出仍处候选期、由生成器维护的收据。已完成生产 canary 验收
// 的历史收据（例如 settings/user）绑定的画像可能已经退休，无法也不得重新生成，生成器
// 按落库原样保留。同一 Sink 的待生成收据必须位于该 Sink 追加链的末尾，按本表顺序追加。
var versionRouteGenerationSpecs = []versionRouteGenerationSpec{{
	sinkID: SinkCodexQuotaWHAM, method: "GET", host: "chatgpt.com",
	path: "/backend-api/wham/accounts/check", protocol: WireProtocolHTTP,
	artifactDirectory: "version-route-migration-artifacts/codex_quota_wham/wham_accounts_check",
	reviewTopic:       "accounts-check",
	rationale:         "新增工作区路由发现 accounts/check endpoint（画像 WorkspaceRouting 节）",
}}

// TestGenerateVersionRouteReceipt 只向显式临时目录输出候选收据。受审资产必须先经
// 人工检查与 secret scan，再用独立补丁提升到 catalogdata。输出目录与 catalogdata
// 同构：清单位于根目录，产物位于 version-route-migration-artifacts 下。
func TestGenerateVersionRouteReceipt(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(versionRouteReceiptOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定版本 route 收据临时目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("版本 route 收据输出目录必须是绝对路径")
	}
	manifest, artifacts := buildVersionRouteReceiptCandidate(t)
	writeVersionRouteCandidate(t, output, "version-route-migration-receipts.json", marshalVersionRouteCandidate(t, manifest))
	for path, raw := range artifacts {
		writeVersionRouteCandidate(t, output, path, raw)
	}
}

// buildVersionRouteReceiptCandidate 生成完整清单与待生成收据的产物。清单落库后再次运行
// 必须逐字节复现：已落库的待生成收据先从最终 Catalog 还原为追加前的 binding，再重新生成；
// 其余收据原样保留。
func buildVersionRouteReceiptCandidate(t *testing.T) (versionRouteReceiptManifest, map[string][]byte) {
	t.Helper()
	landed, err := loadVersionRouteReceiptManifest()
	if err != nil {
		t.Fatal(err)
	}
	specIdentities := make(map[string]bool, len(versionRouteGenerationSpecs))
	for _, spec := range versionRouteGenerationSpecs {
		specIdentities[string(spec.sinkID)+"\x00"+versionRouteIdentity(spec.catalogRoute(t)).Identity()] = true
	}
	kept := make([]versionRouteReceiptDoc, 0, len(landed.Receipts))
	landedBySink := make(map[SinkID][]versionRouteReceiptDoc)
	for _, document := range landed.Receipts {
		sinkID := SinkID(document.SinkID)
		if specIdentities[document.SinkID+"\x00"+document.Route.Route.Identity()] {
			landedBySink[sinkID] = append(landedBySink[sinkID], document)
			continue
		}
		if len(landedBySink[sinkID]) > 0 {
			t.Fatalf("待生成的版本 route 收据必须位于 %s 追加链末尾", sinkID)
		}
		kept = append(kept, document)
	}

	inputs := make(map[SinkID]*SinkBindingInput)
	generated := make([]versionRouteReceiptDoc, 0, len(versionRouteGenerationSpecs))
	artifacts := make(map[string][]byte)
	for _, spec := range versionRouteGenerationSpecs {
		input, ok := inputs[spec.sinkID]
		if !ok {
			binding, found := DefaultSinkCatalog().Resolve(spec.sinkID)
			if !found || binding.migrationReceipt == nil || binding.EnforcementState() != SinkStateEnforced {
				t.Fatalf("%s 不是带收据的 enforced binding", spec.sinkID)
			}
			restored := versionRouteRestoreBefore(t, binding, landedBySink[spec.sinkID])
			input = &restored
			inputs[spec.sinkID] = input
		}
		document, files := generateVersionRouteReceipt(t, input, spec)
		generated = append(generated, document)
		for path, raw := range files {
			artifacts[path] = raw
		}
	}

	receipts := append(kept, generated...)
	// 稳定排序只按 SinkID 分组：同一 Sink 内保留“既有收据在前、新生成收据按表序在后”
	// 的追加顺序。
	sort.SliceStable(receipts, func(i, j int) bool { return receipts[i].SinkID < receipts[j].SinkID })
	if err := validateVersionRouteReceiptOrder(receipts); err != nil {
		t.Fatal(err)
	}
	return versionRouteReceiptManifest{
		SchemaVersion: 1, BootstrapCommit: BootstrapCommit, Receipts: receipts,
	}, artifacts
}

// generateVersionRouteReceipt 在 input（追加前状态）之上生成一条候选态收据，并把 input
// 推进到追加后的状态。推进使用与 applyVersionRouteReceipts 相同的摘要链，供同一 Sink 的
// 下一条收据继续追加。
func generateVersionRouteReceipt(
	t *testing.T,
	input *SinkBindingInput,
	spec versionRouteGenerationSpec,
) (versionRouteReceiptDoc, map[string][]byte) {
	t.Helper()
	route := spec.catalogRoute(t)
	if route.Key.Purpose != input.Purpose || versionRouteInputContains(*input, route) {
		t.Fatalf("版本 route 与追加前的 binding 冲突：%s", spec.path)
	}
	prior := *input.migrationReceipt
	endpointBinding, profileDigests, err := resolveVersionRouteBinding(*input, route)
	if err != nil {
		t.Fatal(err)
	}
	release := versionRouteCaptureRelease(t, profileDigests)
	transportID := versionRouteEndpointTransport(t, release, endpointBinding.EndpointID())
	adapterID := adapterForBackend(input.TargetBackend)

	next := *input
	next.Routes = append(append([]CatalogRoute(nil), input.Routes...), route)
	sort.Slice(next.Routes, func(i, j int) bool {
		return catalogRouteIdentity(next.Routes[i]) < catalogRouteIdentity(next.Routes[j])
	})
	bindingDigest, err := sinkBindingIdentityDigest(next)
	if err != nil {
		t.Fatal(err)
	}
	receipt := prior
	receipt.bindingDigest = bindingDigest
	// 捕获阶段只需要一个形状合法的收据摘要；它不进入任何产物，推进状态时再换成真实摘要链。
	receipt.digest = strings.Repeat("a", sha256.Size*2)
	receipt.routeClaims = append(append([]migrationRouteClaim(nil), prior.routeClaims...), migrationRouteClaim{
		route: route, evidenceKind: "codex_endpoint", evidenceID: endpointBinding.EndpointID(),
		backend: input.TargetBackend, adapterID: adapterID, transportID: transportID,
	})
	sort.Slice(receipt.routeClaims, func(i, j int) bool {
		return catalogRouteIdentity(receipt.routeClaims[i].route) < catalogRouteIdentity(receipt.routeClaims[j].route)
	})
	next.migrationReceipt = &receipt
	sinks, err := NewSinkCatalog([]SinkBindingInput{next})
	if err != nil {
		t.Fatal(err)
	}
	routes, err := NewOfficialRouteCatalog(sinks)
	if err != nil {
		t.Fatal(err)
	}
	binding, _ := sinks.Resolve(spec.sinkID)
	capture := changeset3CaptureRouteWithCatalogs(
		t, prior.authorityID, release.Mode(), binding, route, true, versionRouteCaptureRouteIndex, sinks, routes,
	)
	if !capture.TerminalGuardAllow || capture.EndpointID != endpointBinding.EndpointID() ||
		capture.TransportID != transportID || capture.ProfileDigest != release.ProfileDigest() {
		t.Fatalf("版本 route 正式执行捕获未闭合：%+v", capture)
	}

	wirePath := "catalogdata/" + spec.artifactDirectory + "/wire.json"
	executionPath := "catalogdata/" + spec.artifactDirectory + "/execution-verification.json"
	wireRaw := marshalVersionRouteCandidate(t, capture)
	wireDigest := versionRouteCandidateSHA256(wireRaw)
	routeIdentity := versionRouteIdentity(route)
	execution := versionRouteExecutionVerification{
		SchemaVersion: 1, Result: "passed", SinkID: string(spec.sinkID), Route: routeIdentity,
		AuthorityKind: prior.authorityKind, AuthorityID: string(prior.authorityID),
		TokenIssuerID: string(prior.tokenIssuerID), EvidenceKind: "codex_endpoint",
		EvidenceID: endpointBinding.EndpointID(), Backend: string(input.TargetBackend),
		AdapterID: string(adapterID), TransportID: transportID,
		WireSHA256: wireDigest, ProfileDigests: profileDigests,
		TerminalGuardAllow: true, ExternalTraffic: false,
	}
	executionRaw := marshalVersionRouteCandidate(t, execution)
	executionDigest := versionRouteCandidateSHA256(executionRaw)
	document := versionRouteReceiptDoc{
		SinkID: string(spec.sinkID), PriorReceiptDigest: prior.digest, BindingDigest: bindingDigest,
		Route: receiptcontract.RouteProof{
			Route: routeIdentity, EvidenceKind: "codex_endpoint", EvidenceID: endpointBinding.EndpointID(),
			Backend: string(input.TargetBackend), AdapterID: string(adapterID),
			TransportID:           transportID,
			WireFixture:           receiptcontract.ArtifactRef{Path: wirePath, SHA256: wireDigest},
			ExecutionVerification: receiptcontract.ArtifactRef{Path: executionPath, SHA256: executionDigest},
		},
		ProfileDigests: profileDigests, Source: versionRouteSourceEvidence(t, spec.sinkID),
		// 候选态：CanaryAcceptance 缺省，由生产 canary 实测后补齐。
		ReviewedBy: "codex-version-route-audit",
		ReviewRef:  fmt.Sprintf("codex-%s-%s/candidate", release.Version(), spec.reviewTopic),
		Rationale: fmt.Sprintf(
			"%s %s；正式 Compiler、Executor、adapter 与 terminal Guard 的合成无外部流量捕获已通过；"+
				"生产 canary acceptance 待生产 canary 实测后补齐。",
			release.Version(), spec.rationale,
		),
	}
	if err := validateVersionRouteReceiptShape(document); err != nil {
		t.Fatal(err)
	}
	files := fstest.MapFS{
		wirePath:      &fstest.MapFile{Data: wireRaw},
		executionPath: &fstest.MapFile{Data: executionRaw},
	}
	if err := verifyVersionRouteReceiptArtifacts(
		files, document, prior.authorityKind, string(prior.authorityID), string(prior.tokenIssuerID),
	); err != nil {
		t.Fatal(err)
	}

	raw, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	documentDigest := sha256.Sum256(raw)
	combined := sha256.Sum256([]byte(prior.digest + "\x00" + hex.EncodeToString(documentDigest[:])))
	receipt.digest = hex.EncodeToString(combined[:])
	if !receipt.validFor(next) {
		t.Fatalf("版本 route 收据追加后未形成完整 enforced binding：%s", spec.path)
	}
	*input = next
	return document, map[string][]byte{
		spec.artifactDirectory + "/wire.json":                   wireRaw,
		spec.artifactDirectory + "/execution-verification.json": executionRaw,
	}
}

// versionRouteRestoreBefore 从最终 Catalog 的 binding 还原到 appended 这些追加收据应用之前：
// 去掉它们追加的 route 与 route claim，收据摘要回到第一条的 prior_receipt_digest。
// appended 为空时原样返回最终状态。
func versionRouteRestoreBefore(
	t *testing.T,
	binding SinkBinding,
	appended []versionRouteReceiptDoc,
) SinkBindingInput {
	t.Helper()
	if binding.migrationReceipt == nil {
		t.Fatalf("%s 缺少迁移收据", binding.ID())
	}
	input := sinkBindingInputForVersionRoute(binding)
	receipt := *binding.migrationReceipt
	receipt.routeClaims = append([]migrationRouteClaim(nil), receipt.routeClaims...)
	if len(appended) > 0 {
		for _, document := range appended {
			route := versionRouteCatalogRoute(document.Route.Route)
			if !versionRouteInputContains(input, route) {
				t.Fatalf("最终 Catalog 缺少已落库的版本 route：%s", document.Route.Route.Path)
			}
			input.Routes = versionRouteWithoutRoute(input.Routes, route)
			claims := make([]migrationRouteClaim, 0, len(receipt.routeClaims))
			for _, claim := range receipt.routeClaims {
				if catalogRouteIdentity(claim.route) != catalogRouteIdentity(route) {
					claims = append(claims, claim)
				}
			}
			receipt.routeClaims = claims
		}
		receipt.digest = appended[0].PriorReceiptDigest
		bindingDigest, err := sinkBindingIdentityDigest(input)
		if err != nil {
			t.Fatal(err)
		}
		receipt.bindingDigest = bindingDigest
	}
	input.migrationReceipt = &receipt
	if !receipt.validFor(input) {
		t.Fatalf("未能还原 %s 在版本 route 追加前的 binding", binding.ID())
	}
	return input
}

func (spec versionRouteGenerationSpec) catalogRoute(t *testing.T) CatalogRoute {
	t.Helper()
	binding, ok := DefaultSinkCatalog().Resolve(spec.sinkID)
	if !ok {
		t.Fatalf("版本 route 生成目标不存在：%s", spec.sinkID)
	}
	return CatalogRoute{Key: RouteKey{
		Method: spec.method, Host: spec.host, Path: spec.path, Purpose: binding.Purpose(),
	}, Protocol: spec.protocol}
}

func versionRouteIdentity(route CatalogRoute) receiptcontract.RouteIdentity {
	return receiptcontract.RouteIdentity{
		Method: route.Key.Method, Host: route.Key.Host, Path: route.Key.Path,
		Purpose: string(route.Key.Purpose), Protocol: string(route.Protocol),
	}
}

func versionRouteInputContains(input SinkBindingInput, target CatalogRoute) bool {
	for _, route := range input.Routes {
		if catalogRouteIdentity(route) == catalogRouteIdentity(target) {
			return true
		}
	}
	return false
}

func versionRouteWithoutRoute(routes []CatalogRoute, target CatalogRoute) []CatalogRoute {
	filtered := make([]CatalogRoute, 0, len(routes))
	for _, route := range routes {
		if catalogRouteIdentity(route) != catalogRouteIdentity(target) {
			filtered = append(filtered, route)
		}
	}
	return filtered
}

func sinkBindingInputForVersionRoute(binding SinkBinding) SinkBindingInput {
	return SinkBindingInput{
		ID: binding.id, Purpose: binding.purpose, Persona: binding.persona,
		EndpointEvidence: binding.endpointEvidence, Routes: binding.Routes(),
		TargetBackend: binding.targetBackend, LegacyBackends: binding.LegacyBackends(),
		EnforcementState: binding.enforcementState, Owner: binding.owner,
		MigrationChangeset: binding.migrationChangeset, ExpiryCondition: binding.expiryCondition,
		RuntimeBindable: binding.runtimeBindable, Override: binding.override,
	}
}

// versionRouteCaptureRelease 选出第一个包含该 route 的 Active／Previous 发布，作为合成捕获
// 的 mode 与 transport 锚点。候选期目标画像在 previous、晋升后在 active，按结构事实选择，
// 不按槽位名写死。
func versionRouteCaptureRelease(t *testing.T, profileDigests []string) ResolvedCodexRelease {
	t.Helper()
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		if slices.Contains(profileDigests, release.ProfileDigest()) {
			return release
		}
	}
	t.Fatalf("版本 route 画像不在 Active/Previous 中：%v", profileDigests)
	return ResolvedCodexRelease{}
}

func versionRouteEndpointTransport(t *testing.T, release ResolvedCodexRelease, endpointID string) string {
	t.Helper()
	for _, endpoint := range release.ExecutableProfile().Endpoints() {
		if endpoint.ID == endpointID {
			return endpoint.TransportID
		}
	}
	t.Fatalf("版本 route endpoint 缺少 transport：%s", endpointID)
	return ""
}

func versionRouteSourceEvidence(t *testing.T, sinkID SinkID) amendmentSourceEvidence {
	t.Helper()
	document, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := bindingcontract.NewBindingCatalog(document)
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := catalog.Resolve(string(sinkID))
	if !ok || len(binding.Candidates) != 1 {
		t.Fatalf("%s 源码候选不是唯一证据", sinkID)
	}
	candidate := binding.Candidates[0]
	repositoryRoot, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(repositoryRoot, candidate.File))
	if err != nil {
		t.Fatal(err)
	}
	return amendmentSourceEvidence{
		ScanCandidateID: candidate.ScanCandidateID, ASTFingerprint: candidate.ASTFingerprint,
		File: candidate.File, Function: candidate.Func, Callee: candidate.Callee,
		SourceBlobSHA256: versionRouteCandidateSHA256(raw),
	}
}

func marshalVersionRouteCandidate(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func versionRouteCandidateSHA256(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func writeVersionRouteCandidate(t *testing.T, root, path string, raw []byte) {
	t.Helper()
	target := filepath.Join(root, path)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, raw, 0o644); err != nil {
		t.Fatal(err)
	}
}
