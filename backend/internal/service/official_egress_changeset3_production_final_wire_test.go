package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecapture"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	coderws "github.com/coder/websocket"
)

const (
	changeset3ProductionFinalWireOutputEnv = "CHANGESET3_PRODUCTION_FINAL_WIRE_OUTPUT"
	changeset3ApprovedDeltaOutputEnv       = "CHANGESET3_APPROVED_DELTA_OUTPUT"
)

type changeset3ProductionSource struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type changeset3ProductionComparison struct {
	ReferencePath         string `json:"reference_path"`
	ReferenceSHA256       string `json:"reference_sha256"`
	ReferenceScope        string `json:"reference_scope"`
	ComparedCaptureCount  int    `json:"compared_capture_count"`
	UnexpectedDiffCount   int    `json:"unexpected_diff_count"`
	ApprovedDeltaPath     string `json:"approved_delta_path"`
	ApprovedDeltaSHA256   string `json:"approved_delta_sha256"`
	ApprovedDeltaCount    int    `json:"approved_delta_count"`
	AppliedApprovedDeltas int    `json:"applied_approved_deltas"`
	Result                string `json:"result"`
}

type changeset3ProductionAnchorFixture struct {
	SinkID string `json:"sink_id"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type changeset3ProductionAnchorComparison struct {
	FixtureCount         int                                 `json:"fixture_count"`
	ComparedCaptureCount int                                 `json:"compared_capture_count"`
	Result               string                              `json:"result"`
	Fixtures             []changeset3ProductionAnchorFixture `json:"fixtures"`
}

type changeset3ProductionManifest struct {
	SchemaVersion      string                               `json:"schema_version"`
	CaptureKind        string                               `json:"capture_kind"`
	CaptureBoundary    string                               `json:"capture_boundary"`
	ExternalTraffic    bool                                 `json:"external_traffic"`
	CredentialMaterial string                               `json:"credential_material"`
	RouteCount         int                                  `json:"route_count"`
	ReleaseModes       []officialegress.ReleaseMode         `json:"release_modes"`
	CaptureCount       int                                  `json:"capture_count"`
	SourceMaterial     []changeset3ProductionSource         `json:"source_material"`
	Comparison         changeset3ProductionComparison       `json:"pre_refactor_comparison"`
	AnchorComparison   changeset3ProductionAnchorComparison `json:"existing_anchor_comparison"`
	Captures           []finalwirecapture.Capture           `json:"captures"`
	Redaction          string                               `json:"redaction"`
}

type changeset3ApprovedDeltaManifest struct {
	SchemaVersion   string                            `json:"schema_version"`
	ReferencePath   string                            `json:"reference_path"`
	ReferenceSHA256 string                            `json:"reference_sha256"`
	Entries         []finalwirecontract.ApprovedDelta `json:"entries"`
}

// changeset3ProductionBoundaryCompiler 在正式 Executor 调用 compiler 的精确边界
// 观察业务语义。它仍委托生产 Compiler 完成全部定型，只为证明 files.register
// 没有在进入权威边界前恢复 Profile 顺序或投影字段。
type changeset3ProductionBoundaryCompiler struct {
	delegate            *officialegress.Compiler
	filesCreateObserved bool
}

func (c *changeset3ProductionBoundaryCompiler) Compile(
	ctx context.Context,
	bundle officialegress.ReleaseBundle,
	plan officialegress.CodexEgressPlan,
	dynamic officialegress.EndpointDynamicInputs,
) (officialegress.CompiledExecution, error) {
	if plan.SinkID == officialegress.SinkCodexFilesRegister && plan.EndpointID == "files_create" {
		raw, replayable := plan.Body.ReplayableBytes()
		if !replayable {
			return officialegress.CompiledExecution{}, errors.New("files.register create 语义 Body 意外变成 single-use")
		}
		if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte(`{"use_case"`)) {
			return officialegress.CompiledExecution{}, fmt.Errorf(
				"files.register 在 Executor compiler 前已排序或投影 Body：%s", raw,
			)
		}
		c.filesCreateObserved = true
	}
	return c.delegate.Compile(ctx, bundle, plan, dynamic)
}

func TestGenerateChangeset3ProductionFinalWire(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset3ProductionFinalWireOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定 production final-wire 临时目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("production final-wire 输出目录必须是绝对路径")
	}
	manifest := changeset3BuildProductionFinalWireManifest(t)
	raw := changeset3ProductionMarshal(t, manifest)
	if err := os.MkdirAll(output, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(output, "manifest.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	scan := changeset3ProductionSecretScan(t, raw)
	if err := os.WriteFile(
		filepath.Join(output, "secret-scan.json"), changeset3ProductionMarshal(t, scan), 0o644,
	); err != nil {
		t.Fatal(err)
	}
}

func TestGenerateChangeset3ExactApprovedDeltas(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset3ApprovedDeltaOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定 approved delta 临时文件时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("approved delta 输出必须是绝对路径")
	}
	captures := changeset3BuildProductionFinalWireCaptures(t)
	referencePath := "../../../docs/changeset3/pre_identity_authority_refactor_reference/manifest.json"
	referenceRaw, err := os.ReadFile(referencePath)
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Captures []finalwirecapture.Capture `json:"captures"`
	}
	if err := json.Unmarshal(referenceRaw, &reference); err != nil {
		t.Fatal(err)
	}
	preByKey := make(map[string]finalwirecapture.Capture, len(reference.Captures))
	for _, capture := range reference.Captures {
		preByKey[changeset3ProductionCaptureKey(capture)] = capture
	}
	manifest := changeset3ApprovedDeltaManifest{
		SchemaVersion:   "changeset3-final-wire-approved-deltas/v1",
		ReferencePath:   "docs/changeset3/pre_identity_authority_refactor_reference/manifest.json",
		ReferenceSHA256: finalwirecapture.SHA256(referenceRaw),
	}
	for _, capture := range captures {
		key := changeset3ProductionCaptureKey(capture)
		pre, ok := preByKey[key]
		if !ok {
			t.Fatalf("approved delta 生成缺少 pre capture：%s", key)
		}
		result, compareErr := finalwirecontract.Compare(key, pre, capture, nil)
		if compareErr != nil {
			t.Fatal(compareErr)
		}
		for _, difference := range result.Unexpected {
			manifest.Entries = append(manifest.Entries, finalwirecontract.ApprovedDelta{
				CaptureKey: key, Path: difference.Path,
				BeforeSHA256: difference.BeforeSHA256, AfterSHA256: difference.AfterSHA256,
				Reason: changeset3ApprovedDeltaReason(difference.Path),
			})
		}
	}
	sort.Slice(manifest.Entries, func(i, j int) bool {
		left, right := manifest.Entries[i], manifest.Entries[j]
		return left.CaptureKey+"\x00"+left.Path < right.CaptureKey+"\x00"+right.Path
	})
	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(output, changeset3ProductionMarshal(t, manifest), 0o644); err != nil {
		t.Fatal(err)
	}
}

func changeset3ApprovedDeltaReason(path string) string {
	switch {
	case strings.HasPrefix(path, "/ordered_headers"):
		return "保护身份 Header 改由 compiler 根据结构化事实生成并记录安全值或摘要"
	case strings.HasPrefix(path, "/body"):
		return "Body 字段线序、omit、compiler-owned 注入、压缩与长度改由 compiler 最终定型"
	case strings.HasPrefix(path, "/websocket"):
		return "WebSocket 握手压缩与出站帧改由 Executor session 最终定型"
	case path == "/bundle_digest" || path == "/connection_identity_digest" ||
		path == "/connection_pool_digest":
		return "捕获链由合成 Plan 改为 production invocation 后，Bundle 与连接身份绑定真实生产策略"
	case strings.HasPrefix(path, "/terminal_guard"):
		return "terminal Guard 在正式 adapter 后验证当前 FinalizationToken"
	default:
		return "production semantic bridge 替代合成 Plan 后产生的精确、逐字段登记差异"
	}
}

func changeset3BuildProductionFinalWireManifest(t *testing.T) changeset3ProductionManifest {
	t.Helper()
	captures := changeset3BuildProductionFinalWireCaptures(t)
	return changeset3ProductionManifest{
		SchemaVersion:   "changeset3-post-identity-authority-final-wire/v1",
		CaptureKind:     "post_identity_authority_refactor_final_wire",
		CaptureBoundary: "production_semantic_bridge_to_production_invocation_to_official_adapter_after_transport_normalization_before_deterministic_local_terminal_send",
		ExternalTraffic: false, CredentialMaterial: "synthetic_attempt_local_only",
		RouteCount: 28,
		ReleaseModes: []officialegress.ReleaseMode{
			officialegress.ReleaseModeActive, officialegress.ReleaseModePrevious,
		},
		CaptureCount: len(captures), SourceMaterial: changeset3ProductionSources(t),
		Comparison:       changeset3CompareProductionToScopedReference(t, captures),
		AnchorComparison: changeset3CompareProductionAnchors(t, captures),
		Captures:         captures,
		Redaction:        "只保存合成最终 wire 的结构、顺序和脱敏摘要；认证材料在 attempt 内单次消费，不保存值或可关联真实摘要。",
	}
}

func changeset3BuildProductionFinalWireCaptures(t *testing.T) []finalwirecapture.Capture {
	t.Helper()
	anchors := map[officialegress.SinkID]bool{
		officialegress.SinkCodexAdminTestCompact:       true,
		officialegress.SinkCodexAdminTestResponses:     true,
		officialegress.SinkCodexAlphaSearchPATFallback: true,
		officialegress.SinkCodexUsageProbe:             true,
	}
	var captures []finalwirecapture.Capture
	routes := make(map[string]bool)
	for _, mode := range []officialegress.ReleaseMode{
		officialegress.ReleaseModeActive, officialegress.ReleaseModePrevious,
	} {
		for _, binding := range officialegress.DefaultSinkCatalog().Bindings() {
			if binding.Persona() != officialegress.PersonaCodexCLI ||
				binding.EndpointEvidence() != officialegress.EndpointEvidenceCodexProfile ||
				!binding.RuntimeBindable() ||
				binding.EnforcementState() != officialegress.SinkStateEnforced {
				continue
			}
			for routeIndex, route := range binding.Routes() {
				routes[string(binding.ID())+"\x00"+route.Key.String()] = true
				captures = append(captures, changeset3CaptureProductionRoute(
					t, mode, binding, route, anchors[binding.ID()], routeIndex,
				))
			}
		}
	}
	sort.Slice(captures, func(i, j int) bool {
		return changeset3ProductionCaptureKey(captures[i]) < changeset3ProductionCaptureKey(captures[j])
	})
	if len(routes) != 28 || len(captures) != 56 {
		t.Fatalf("production final-wire 捕获范围错误：routes=%d captures=%d", len(routes), len(captures))
	}
	return captures
}

func changeset3CaptureProductionRoute(
	t *testing.T,
	mode officialegress.ReleaseMode,
	binding officialegress.SinkBinding,
	route officialegress.CatalogRoute,
	anchor bool,
	routeIndex int,
) finalwirecapture.Capture {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(mode)
	if err != nil {
		t.Fatalf("缺少 release mode %s：%v", mode, err)
	}
	endpoint := changeset3ProductionEndpointForRoute(t, release.Profile(), route)
	target, dynamic := changeset3ProductionTarget(t, endpoint, route)
	baseGuard := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		baseGuard.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), baseGuard.Recorder(),
	)
	if err != nil {
		t.Fatal(err)
	}
	base := finalwirecapture.Capture{
		SinkID: string(binding.ID()), Anchor: anchor, ReleaseMode: mode,
		Method: route.Key.Method, HostTemplate: route.Key.Host,
		PathTemplate: route.Key.Path, Protocol: route.Protocol,
		Purpose: binding.Purpose(), EndpointID: endpoint.ID,
		SingleUseRawStream: endpoint.Body.Encoding == profilecontract.BodyRawBytes,
	}
	if dynamic.ReturnedURL != nil {
		base.DynamicTarget = &finalwirecapture.DynamicTarget{
			SyntheticReturnedURL: dynamic.ReturnedURL.String(),
			ValidationResult:     "accepted_by_bundle_dynamic_target_policy",
		}
	}
	terminal, err := finalwirecapture.NewTerminal(guard, base)
	if err != nil {
		t.Fatal(err)
	}
	httpAdapter, err := officialegress.NewHTTPUpstreamTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	reqAdapter, err := officialegress.NewReqProfileTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	wsAdapter, err := officialegress.NewWebSocketTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	registry, err := officialegress.NewAdapterRegistry(
		officialegress.DefaultBackendDescriptors(),
		[]officialegress.TransportAdapter{httpAdapter, reqAdapter, wsAdapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	boundaryCompiler := &changeset3ProductionBoundaryCompiler{delegate: officialegress.NewCompiler()}
	executor, err := officialegress.NewExecutor(
		officialCodexExecutorID, boundaryCompiler, registry, guard,
	)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := officialegress.NewBundleResolver(
		officialegress.DefaultReleaseCatalog(), officialegress.DefaultSinkCatalog(),
	)
	if err != nil {
		t.Fatal(err)
	}
	runtimeState := NewOfficialEgressTransitionRuntime(
		resolver, guard, mode,
	)
	runtimeState.CodexExecutor = executor
	account := changeset3ProductionAccount(routeIndex)
	ctx := changeset3ProductionContext(t, mode, endpoint.ID, routeIndex)

	switch {
	case endpoint.ID == officialCodexEndpointOAuthRefresh:
		// OAuth 生产入口通常自行生成 InvocationID。final-wire 采集必须显式冻结它，
		// 否则 invocation 生命周期会令 connection_pool_digest 每次重建都不同，
		// 从而无法区分真实 wire 漂移与夹具随机性。
		ctx, err = officialegress.WithAttemptMetadata(ctx, officialegress.AttemptMetadataInput{
			SinkID:          binding.ID(),
			Purpose:         binding.Purpose(),
			DeclaredPersona: binding.Persona(),
			EndpointID:      endpoint.ID,
			InvocationID:    fmt.Sprintf("cs3-oauth-%d", routeIndex),
			ReleaseMode:     mode,
		})
		if err != nil {
			t.Fatal(err)
		}
		service := &OpenAIOAuthService{officialEgress: runtimeState}
		response, executeErr := service.executeOAuthRefresh(
			ctx, "synthetic-refresh-token", "synthetic-client-id", "",
		)
		if executeErr != nil {
			t.Fatalf("OAuth production bridge 捕获失败：%v", executeErr)
		}
		_ = response.Body.Close()
	case route.Protocol == officialegress.WireProtocolWebSocket:
		changeset3CaptureProductionWebSocket(
			t, ctx, runtimeState, account, binding, endpoint, target, routeIndex,
		)
	case endpoint.ID == "responses_http" || endpoint.ID == "responses_compact":
		changeset3CaptureProductionForward(
			t, ctx, runtimeState, account, binding, endpoint, target, routeIndex,
		)
	default:
		changeset3CaptureProductionHTTP(
			t, ctx, runtimeState, account, binding, endpoint, target, dynamic, routeIndex,
		)
	}
	if endpoint.ID == "files_create" && !boundaryCompiler.filesCreateObserved {
		t.Fatal("files.register capture 未经过 production semantic→Executor compiler 边界")
	}
	captured := terminal.Captures()
	if len(captured) != 1 {
		t.Fatalf("production bridge terminal 捕获次数错误：sink=%s route=%s count=%d", binding.ID(), route.Key.String(), len(captured))
	}
	return captured[0]
}

func changeset3CaptureProductionForward(
	t *testing.T,
	ctx context.Context,
	runtimeState *OfficialEgressTransitionRuntime,
	account *Account,
	binding officialegress.SinkBinding,
	endpoint profilecontract.EndpointProfile,
	target *url.URL,
	routeIndex int,
) {
	t.Helper()
	policyID := fmt.Sprintf("changeset3.capture.forward.%d", routeIndex)
	behavior := officialegress.BehaviorPolicy{
		ID: policyID + ".behavior", Source: "changeset3-production-capture",
		Kind: officialegress.BehaviorUserRequest, AttemptBudget: 1,
	}
	plan, err := newOpenAIForwardInvocationPlan(ctx, openAIForwardInvocationPlanInput{
		Runtime: runtimeState, Account: account, PrimarySinkID: binding.ID(),
		InvocationID: fmt.Sprintf("cs3-forward-%d", routeIndex),
		IdentityMode: officialegress.IdentityCodexOAuthStrict,
		HeaderPolicy: officialegress.HeaderPolicy{
			ID: policyID + ".headers", Source: "changeset3-production-capture",
		},
		ExecutionPolicy: officialegress.ExecutionPolicy{
			ID: policyID + ".execution", Source: "changeset3-production-capture",
			MaxAttempts: 1, Replayable: true, ConcurrencyLimit: 1,
		},
		BehaviorPolicy: behavior,
		DeploymentPolicy: officialegress.DeploymentSupportPolicy{
			ID: policyID + ".deployment", Source: "changeset3-production-capture",
			Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: "direct",
			SupportedBackends: []officialegress.BackendKind{
				officialegress.BackendHTTPUpstream, officialegress.BackendWebSocket,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	body := changeset3ProductionSemanticBody(t, endpoint)
	request, err := http.NewRequestWithContext(ctx, endpoint.Method, target.String(), bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer synthetic-access-token")
	response, err := plan.ExecuteHTTPRequest(ctx, request, endpoint.ID)
	if err != nil {
		t.Fatalf("Responses production Forward bridge 失败：%v", err)
	}
	_ = response.Body.Close()
}

func changeset3CaptureProductionHTTP(
	t *testing.T,
	ctx context.Context,
	runtimeState *OfficialEgressTransitionRuntime,
	account *Account,
	binding officialegress.SinkBinding,
	endpoint profilecontract.EndpointProfile,
	target *url.URL,
	dynamic officialegress.EndpointDynamicInputs,
	routeIndex int,
) {
	t.Helper()
	policyID := fmt.Sprintf("changeset3.capture.http.%d", routeIndex)
	singleUse := endpoint.Body.Encoding == profilecontract.BodyRawBytes
	invocation, err := newOfficialCodexHTTPInvocation(ctx, officialCodexHTTPInvocationInput{
		Runtime: runtimeState, Account: account, SinkID: binding.ID(),
		InvocationID: fmt.Sprintf("cs3-http-%d", routeIndex),
		PolicyID:     policyID, PolicySource: "changeset3-production-capture",
		AttemptBudget: 1, SingleUseBody: singleUse,
	})
	if err != nil {
		t.Fatal(err)
	}
	var request *http.Request
	if singleUse {
		content := []byte("synthetic-upload-content\n")
		request, err = newOfficialCodexBlobUploadSemanticRequest(
			ctx, target, uint64(len(content)), bytes.NewReader(content),
		)
	} else {
		body := changeset3ProductionSemanticBody(t, endpoint)
		if endpoint.ID == "files_create" {
			body, err = marshalOfficialCodexFileSemanticBody(officialCodexFileCreateSemanticBody{
				UseCase: "codex", FileSize: 24, FileName: "synthetic.txt",
			})
			if err == nil && !bytes.HasPrefix(body, []byte(`{"use_case"`)) {
				t.Fatalf("files.register production semantic bridge 已在 Executor 前排序/投影：%s", body)
			}
		}
		if endpoint.ID == "files_uploaded" {
			body, err = marshalOfficialCodexFileSemanticBody(struct{}{})
		}
		if err == nil {
			request, err = http.NewRequestWithContext(ctx, endpoint.Method, target.String(), bytes.NewReader(body))
		}
	}
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer synthetic-access-token")
	response, err := invocation.Execute(ctx, officialCodexHTTPAttemptInput{
		EndpointID: endpoint.ID, Request: request, DynamicInputs: dynamic,
	})
	if err != nil {
		t.Fatalf("HTTP production invocation bridge 失败：endpoint=%s err=%v", endpoint.ID, err)
	}
	_ = response.Body.Close()
}

func changeset3CaptureProductionWebSocket(
	t *testing.T,
	ctx context.Context,
	runtimeState *OfficialEgressTransitionRuntime,
	account *Account,
	binding officialegress.SinkBinding,
	endpoint profilecontract.EndpointProfile,
	target *url.URL,
	routeIndex int,
) {
	t.Helper()
	invocation, err := newOfficialCodexWebSocketInvocation(ctx, officialCodexWebSocketInvocationInput{
		Runtime: runtimeState, Account: account, SinkID: binding.ID(),
		InvocationID: fmt.Sprintf("cs3-ws-%d", routeIndex),
		PolicyID:     fmt.Sprintf("changeset3.capture.ws.%d", routeIndex),
		PolicySource: "changeset3-production-capture", AttemptBudget: 1,
		IdentityFacts: changeset3ProductionInvocationIdentity(routeIndex),
	})
	if err != nil {
		t.Fatal(err)
	}
	request := openAIWSAcquireRequest{
		Account: account, WSURL: target.String(), SinkID: binding.ID(),
		Headers: http.Header{"Authorization": []string{"Bearer synthetic-access-token"}},
	}
	session, _, _, err := invocation.DialDirect(
		ctx, changeset3UnusedWSDialer{}, request, endpoint.ID,
	)
	if err != nil {
		t.Fatalf("WS production invocation bridge 失败：%v", err)
	}
	defer func() { _ = session.Close() }()
	if endpoint.ID == "realtime_sideband" {
		if err := session.WriteFrame(
			ctx, coderws.MessageText,
			[]byte(`{"zeta":1,"type":"session.update","alpha":{"value":2},"middle":true}`),
		); err != nil {
			t.Fatal(err)
		}
		if err := session.WriteFrame(ctx, coderws.MessageBinary, []byte("synthetic-binary-frame")); err != nil {
			t.Fatal(err)
		}
		return
	}
	body := changeset3ProductionSemanticBody(t, endpoint)
	if err := session.WriteFrame(ctx, coderws.MessageText, body); err != nil {
		t.Fatal(err)
	}
}

type changeset3UnusedWSDialer struct{}

func (changeset3UnusedWSDialer) Dial(
	context.Context,
	string,
	http.Header,
	string,
) (openAIWSClientConn, int, http.Header, error) {
	return nil, 0, nil, errors.New("deterministic terminal 应在正式 adapter 边界截获 WS")
}

func changeset3ProductionContext(
	t *testing.T,
	mode officialegress.ReleaseMode,
	endpointID string,
	routeIndex int,
) context.Context {
	t.Helper()
	state, err := officialCodexRuntimeStateForReleaseMode(string(mode), endpointID)
	if err != nil {
		t.Fatal(err)
	}
	ctx, err := withOfficialCodexRuntimeState(context.Background(), state)
	if err != nil {
		t.Fatal(err)
	}
	return withOfficialCodexInvocationIdentity(ctx, changeset3ProductionInvocationIdentity(routeIndex))
}

func changeset3ProductionInvocationIdentity(routeIndex int) officialCodexInvocationIdentityInput {
	suffix := fmt.Sprintf("%02d", routeIndex)
	return officialCodexInvocationIdentityInput{
		InstallationID:  "11111111-1111-1111-1111-1111111111" + suffix,
		SessionID:       "22222222-2222-2222-2222-2222222222" + suffix,
		ConversationID:  "33333333-3333-3333-3333-3333333333" + suffix,
		ThreadID:        "44444444-4444-4444-4444-4444444444" + suffix,
		WindowID:        "55555555-5555-5555-5555-5555555555" + suffix,
		ClientRequestID: "66666666-6666-6666-6666-6666666666" + suffix,
		TurnID:          "77777777-7777-7777-7777-7777777777" + suffix,
		TurnMetadata:    "synthetic-turn-metadata-" + suffix,
	}
}

func changeset3ProductionAccount(routeIndex int) *Account {
	return &Account{
		ID: int64(9000 + routeIndex), Platform: PlatformOpenAI,
		Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{
			"chatgpt_account_id":        fmt.Sprintf("synthetic-account-%02d", routeIndex),
			openAIAuthModeCredentialKey: "synthetic-oauth",
		},
	}
}

func changeset3ProductionEndpointForRoute(
	t *testing.T,
	profile profilecontract.ProfileSpec,
	route officialegress.CatalogRoute,
) profilecontract.EndpointProfile {
	t.Helper()
	var matches []profilecontract.EndpointProfile
	for _, endpoint := range profile.Endpoints() {
		protocol := officialegress.WireProtocolHTTP
		if endpoint.Upgrade != "" {
			protocol = officialegress.WireProtocolWebSocket
		}
		pathMatches := endpoint.Path == route.Key.Path ||
			strings.TrimPrefix(endpoint.Path, "/") == strings.TrimPrefix(route.Key.Path, "/")
		if endpoint.Method == route.Key.Method && endpoint.Host == route.Key.Host &&
			pathMatches && protocol == route.Protocol {
			matches = append(matches, endpoint)
		}
	}
	if len(matches) != 1 {
		t.Fatalf("route 无法唯一映射 Endpoint Profile：route=%s protocol=%s matches=%d", route.Key.String(), route.Protocol, len(matches))
	}
	return matches[0]
}

func changeset3ProductionTarget(
	t *testing.T,
	endpoint profilecontract.EndpointProfile,
	route officialegress.CatalogRoute,
) (*url.URL, officialegress.EndpointDynamicInputs) {
	t.Helper()
	host := route.Key.Host
	if strings.HasPrefix(host, "*.") {
		host = "upload" + strings.TrimPrefix(host, "*")
	}
	path := strings.ReplaceAll(route.Key.Path, "{server_returned_path}", "synthetic/upload/blob")
	path = strings.ReplaceAll(path, "{file_id}", "synthetic-file-id")
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	scheme := "https"
	if route.Protocol == officialegress.WireProtocolWebSocket {
		scheme = "wss"
	}
	target := &url.URL{Scheme: scheme, Host: host, Path: path}
	query := target.Query()
	for _, field := range endpoint.Query {
		if field.Name == "*" {
			query.Set("synthetic_signature", "synthetic")
			continue
		}
		value := field.Value
		if value == "" {
			value = "synthetic-" + strings.ReplaceAll(field.Name, "_", "-")
		}
		query.Set(field.Name, value)
	}
	target.RawQuery = query.Encode()
	dynamic := officialegress.EndpointDynamicInputs{}
	if endpoint.HostFromResponse {
		dynamic.ReturnedURL = target
	}
	return target, dynamic
}

func changeset3ProductionSemanticBody(
	t *testing.T,
	endpoint profilecontract.EndpointProfile,
) []byte {
	t.Helper()
	switch endpoint.Body.Encoding {
	case profilecontract.BodyNone:
		return nil
	case profilecontract.BodyRawBytes:
		return []byte("synthetic-upload-content\n")
	case profilecontract.BodyFormUrlencoded:
		values := make(url.Values)
		for _, field := range endpoint.Body.Fields {
			if field.Required {
				values.Set(field.Name, fmt.Sprint(changeset3ProductionFieldValue(endpoint.ID, field.Name)))
			}
		}
		return []byte(values.Encode())
	}
	type pair struct {
		name  string
		value any
	}
	var pairs []pair
	for index := len(endpoint.Body.Fields) - 1; index >= 0; index-- {
		field := endpoint.Body.Fields[index]
		if field.Name == "prompt_cache_key" || field.Name == "client_metadata" ||
			field.Name == "refresh_token" || field.Condition != profilecontract.ConditionUnconditional {
			continue
		}
		include := field.Required || field.Name == "instructions" ||
			field.Name == "previous_response_id" || field.OmitWhen == profilecontract.OmitNone
		if !include {
			continue
		}
		value := changeset3ProductionFieldValue(endpoint.ID, field.Name)
		if field.Name == "instructions" {
			value = ""
		} else if field.OmitWhen == profilecontract.OmitNone && !field.Required &&
			field.Name != "reasoning" {
			value = nil
		}
		pairs = append(pairs, pair{name: field.Name, value: value})
	}
	var output bytes.Buffer
	_ = output.WriteByte('{')
	for index, pair := range pairs {
		if index > 0 {
			_ = output.WriteByte(',')
		}
		nameRaw, _ := json.Marshal(pair.name)
		valueRaw, err := json.Marshal(pair.value)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = output.Write(nameRaw)
		_ = output.WriteByte(':')
		_, _ = output.Write(valueRaw)
	}
	_ = output.WriteByte('}')
	return output.Bytes()
}

func changeset3ProductionFieldValue(endpointID string, name string) any {
	switch name {
	case "type":
		if endpointID == "responses_ws" {
			return "response.create"
		}
		return "session.update"
	case "input", "tools", "include", "commands", "images":
		return []any{}
	case "parallel_tool_calls", "store", "stream", "generate":
		return false
	case "reasoning", "settings", "session", "client_metadata":
		return map[string]any{}
	case "file_size", "max_output_tokens":
		return 24
	case "model":
		return "gpt-5.6-sol"
	case "tool_choice":
		return "auto"
	case "previous_response_id":
		return "resp_synthetic_reusable"
	case "use_case":
		return "codex"
	default:
		return "synthetic-" + strings.ReplaceAll(name, "_", "-")
	}
}

func changeset3CompareProductionToScopedReference(
	t *testing.T,
	captures []finalwirecapture.Capture,
) changeset3ProductionComparison {
	t.Helper()
	referencePath := "../../../docs/changeset3/pre_identity_authority_refactor_reference/manifest.json"
	referenceRaw, err := os.ReadFile(referencePath)
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Captures []finalwirecapture.Capture `json:"captures"`
	}
	if err := json.Unmarshal(referenceRaw, &reference); err != nil {
		t.Fatal(err)
	}
	approvedPath := "../../../docs/changeset3/post_identity_authority_refactor_final_wire/approved-deltas.json"
	approvedRaw, err := os.ReadFile(approvedPath)
	if err != nil {
		t.Fatal(err)
	}
	var approved changeset3ApprovedDeltaManifest
	decoder := json.NewDecoder(bytes.NewReader(approvedRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&approved); err != nil {
		t.Fatal(err)
	}
	if approved.SchemaVersion != "changeset3-final-wire-approved-deltas/v1" ||
		approved.ReferenceSHA256 != finalwirecapture.SHA256(referenceRaw) {
		t.Fatal("approved delta 未绑定当前受限 pre reference")
	}
	preByKey := make(map[string]finalwirecapture.Capture, len(reference.Captures))
	for _, capture := range reference.Captures {
		preByKey[changeset3ProductionCaptureKey(capture)] = capture
	}
	applied := 0
	for _, capture := range captures {
		key := changeset3ProductionCaptureKey(capture)
		pre, ok := preByKey[key]
		if !ok {
			t.Fatalf("production capture 缺少 pre reference：%s", key)
		}
		result, compareErr := finalwirecontract.Compare(key, pre, capture, approved.Entries)
		if compareErr != nil {
			t.Fatal(compareErr)
		}
		if !result.OK() {
			t.Fatalf("统一正式比较器拒绝 production capture：key=%s unexpected=%+v unused=%+v", key, result.Unexpected, result.Unused)
		}
		applied += len(result.Applied)
	}
	if applied != len(approved.Entries) {
		t.Fatalf("approved delta 未全部应用：applied=%d total=%d", applied, len(approved.Entries))
	}
	return changeset3ProductionComparison{
		ReferencePath:        "docs/changeset3/pre_identity_authority_refactor_reference/manifest.json",
		ReferenceSHA256:      finalwirecapture.SHA256(referenceRaw),
		ReferenceScope:       "仅比较身份权威重构前双定型参考中已捕获的 adapter-terminal 字段；不声明完整历史生产业务链等价。",
		ComparedCaptureCount: len(captures), UnexpectedDiffCount: 0,
		ApprovedDeltaPath:   "docs/changeset3/post_identity_authority_refactor_final_wire/approved-deltas.json",
		ApprovedDeltaSHA256: finalwirecapture.SHA256(approvedRaw),
		ApprovedDeltaCount:  len(approved.Entries), AppliedApprovedDeltas: applied,
		Result: "scoped_reference_match_with_explicit_approved_deltas",
	}
}

func changeset3CompareProductionAnchors(
	t *testing.T,
	captures []finalwirecapture.Capture,
) changeset3ProductionAnchorComparison {
	t.Helper()
	fixtures := map[officialegress.SinkID]string{
		officialegress.SinkCodexAdminTestCompact:       "../officialegress/catalogdata/migration-artifacts/changeset1b/codex_admin_test_compact/wire.json",
		officialegress.SinkCodexAdminTestResponses:     "../officialegress/catalogdata/migration-artifacts/changeset1b/codex_admin_test_responses/wire.json",
		officialegress.SinkCodexAlphaSearchPATFallback: "../officialegress/catalogdata/migration-artifacts/changeset1b/codex_alpha_search_pat_fallback/wire.json",
		officialegress.SinkCodexUsageProbe:             "../officialegress/catalogdata/migration-artifacts/changeset1b/codex_usage_probe/wire.json",
	}
	type fixture struct {
		HeaderNames []string `json:"header_names"`
		BodyFields  []string `json:"body_fields"`
	}
	loaded := make(map[officialegress.SinkID]fixture)
	result := changeset3ProductionAnchorComparison{FixtureCount: len(fixtures), Result: "passed"}
	for sinkID, path := range fixtures {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var value fixture
		if err := json.Unmarshal(raw, &value); err != nil {
			t.Fatal(err)
		}
		loaded[sinkID] = value
		result.Fixtures = append(result.Fixtures, changeset3ProductionAnchorFixture{
			SinkID: string(sinkID),
			Path:   "backend/internal/officialegress/" + strings.TrimPrefix(path, "../officialegress/"),
			SHA256: finalwirecapture.SHA256(raw),
		})
	}
	sort.Slice(result.Fixtures, func(i, j int) bool { return result.Fixtures[i].SinkID < result.Fixtures[j].SinkID })
	for _, capture := range captures {
		if !capture.Anchor {
			continue
		}
		value := loaded[officialegress.SinkID(capture.SinkID)]
		headers := changeset3ProductionPresentHeaders(capture.OrderedHeaders)
		bodyFields := append([]string(nil), capture.Body.OrderedFields...)
		sort.Strings(headers)
		sort.Strings(bodyFields)
		wantHeaders := append([]string(nil), value.HeaderNames...)
		wantBody := append([]string(nil), value.BodyFields...)
		sort.Strings(wantHeaders)
		sort.Strings(wantBody)
		if !reflect.DeepEqual(headers, wantHeaders) || !reflect.DeepEqual(bodyFields, wantBody) {
			t.Fatalf("既有锚点 fixture 不兼容：%s/%s headers=%v body=%v", capture.SinkID, capture.ReleaseMode, headers, bodyFields)
		}
		result.ComparedCaptureCount++
	}
	if result.ComparedCaptureCount != 8 {
		t.Fatalf("既有锚点比较数量错误：%d", result.ComparedCaptureCount)
	}
	return result
}

func changeset3ProductionPresentHeaders(headers []finalwirecapture.Header) []string {
	var result []string
	for _, header := range headers {
		if !header.Present || header.Name == "host" || header.Name == "content-length" ||
			header.Name == "connection" || header.Name == "upgrade" ||
			header.Name == "sec-websocket-version" || header.Name == "sec-websocket-key" ||
			header.Name == "sec-websocket-extensions" {
			continue
		}
		result = append(result, header.Name)
	}
	return result
}

func changeset3ProductionCaptureKey(capture finalwirecapture.Capture) string {
	return strings.Join([]string{
		string(capture.ReleaseMode), capture.SinkID, capture.Method,
		capture.HostTemplate, capture.PathTemplate, string(capture.Protocol),
	}, "\x00")
}

func changeset3ProductionSources(t *testing.T) []changeset3ProductionSource {
	t.Helper()
	paths := []string{
		"../officialegress/compiler.go",
		"../officialegress/executor.go",
		"../officialegress/catalogdata/release-catalog.json",
		"../officialegress/finalwirecapture/capture.go",
		"../officialegress/finalwirecontract/comparator.go",
		"official_egress_identity_authority.go",
		"official_egress_http_invocation.go",
		"openai_forward_plan.go",
		"official_egress_websocket_invocation.go",
		"official_egress_transport_adapters.go",
		"official_egress_codex_files.go",
		"openai_oauth_service.go",
		"official_egress_changeset3_production_final_wire_test.go",
	}
	result := make([]changeset3ProductionSource, 0, len(paths))
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		manifestPath := "backend/internal/service/" + path
		if strings.HasPrefix(path, "../officialegress/") {
			manifestPath = "backend/internal/officialegress/" + strings.TrimPrefix(path, "../officialegress/")
		}
		result = append(result, changeset3ProductionSource{
			Path:   manifestPath,
			SHA256: finalwirecapture.SHA256(raw),
		})
	}
	return result
}

func changeset3ProductionSecretScan(t *testing.T, manifest []byte) any {
	t.Helper()
	patterns := []string{
		"Bearer ", "synthetic-access-token", "synthetic-refresh-token",
		"access_token\"", "refresh_token\"",
	}
	matches := make(map[string]int, len(patterns))
	for _, pattern := range patterns {
		matches[pattern] = bytes.Count(manifest, []byte(pattern))
	}
	if matches["Bearer "] != 0 || matches["synthetic-access-token"] != 0 ||
		matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("production final-wire 泄漏合成认证值：%v", matches)
	}
	return struct {
		SchemaVersion  string         `json:"schema_version"`
		Artifact       string         `json:"artifact"`
		ArtifactSHA256 string         `json:"artifact_sha256"`
		Matches        map[string]int `json:"matches"`
		Result         string         `json:"result"`
	}{
		SchemaVersion: "changeset3-secret-scan/v1", Artifact: "manifest.json",
		ArtifactSHA256: finalwirecapture.SHA256(manifest), Matches: matches, Result: "passed",
	}
}

func changeset3ProductionMarshal(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func TestChangeset3FrozenPostManifestRebuildsFromProductionSemanticBridge(t *testing.T) {
	const frozenManifestSHA256 = "c824ffb0ab6e2429c09f9ac517cf3e6f96860c7c6ef77c229757fd690bdbcf0f"
	frozenPath := "../../../docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json"
	frozenRaw, err := os.ReadFile(frozenPath)
	if err != nil {
		t.Fatal(err)
	}
	var frozen changeset3ProductionManifest
	if err := json.Unmarshal(frozenRaw, &frozen); err != nil {
		t.Fatal(err)
	}
	if finalwirecapture.SHA256(frozenRaw) != frozenManifestSHA256 ||
		frozen.SchemaVersion != "changeset3-post-identity-authority-final-wire/v1" ||
		frozen.RouteCount != 28 || frozen.CaptureCount != 56 || len(frozen.Captures) != 56 {
		t.Fatal("Changeset 3 production manifest 冻结事实已漂移")
	}
	// Changeset 4 会按已审核方案有意改变 executable、Bundle 与连接身份摘要，
	// 因而不得再用当前 compiler 重建并冒充 Changeset 3 字节。旧源码到新源码的
	// 精确 hash 迁移由 changeset3-source-transition.json 及 officialegress 冻结门禁验证。
}
