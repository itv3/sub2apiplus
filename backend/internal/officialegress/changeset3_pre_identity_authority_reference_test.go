package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

const changeset3PreIdentityReferenceOutputEnv = "CHANGESET3_PRE_IDENTITY_REFERENCE_OUTPUT"

type changeset3PreIdentityReference struct {
	SchemaVersion       string                                  `json:"schema_version"`
	ReferenceKind       string                                  `json:"reference_kind"`
	CaptureBoundary     string                                  `json:"capture_boundary"`
	ExternalTraffic     bool                                    `json:"external_traffic"`
	CredentialMaterial  string                                  `json:"credential_material"`
	RouteCount          int                                     `json:"route_count"`
	ReleaseModes        []ReleaseMode                           `json:"release_modes"`
	CaptureCount        int                                     `json:"capture_count"`
	SourceMaterial      []changeset3ReferenceSource             `json:"source_material"`
	KnownPreRefFindings []string                                `json:"known_pre_ref_findings"`
	Captures            []changeset3PreIdentityReferenceCapture `json:"captures"`
	Redaction           string                                  `json:"redaction"`
}

type changeset3ReferenceSource struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type changeset3PreIdentityReferenceCapture struct {
	SinkID                    string                        `json:"sink_id"`
	Anchor                    bool                          `json:"anchor"`
	ReleaseMode               ReleaseMode                   `json:"release_mode"`
	Method                    string                        `json:"method"`
	HostTemplate              string                        `json:"host_template"`
	FinalHost                 string                        `json:"final_host"`
	PathTemplate              string                        `json:"path_template"`
	FinalPath                 string                        `json:"final_path"`
	Protocol                  WireProtocol                  `json:"protocol"`
	Purpose                   Purpose                       `json:"purpose"`
	EndpointID                string                        `json:"endpoint_id"`
	AuthorityID               ExecutorID                    `json:"authority_id"`
	HasFinalizationToken      bool                          `json:"has_finalization_token"`
	TerminalGuardAllow        bool                          `json:"terminal_guard_allow"`
	TerminalGuardReason       GuardReason                   `json:"terminal_guard_reason,omitempty"`
	ProfileValidationResult   string                        `json:"profile_validation_result"`
	AttemptOrdinal            uint32                        `json:"attempt_ordinal"`
	AttemptReason             string                        `json:"attempt_reason"`
	ReleaseDigest             string                        `json:"release_digest"`
	ProfileDigest             string                        `json:"profile_digest"`
	BundleDigest              string                        `json:"bundle_digest"`
	Backend                   BackendKind                   `json:"backend"`
	AdapterID                 AdapterID                     `json:"adapter_id"`
	TransportID               string                        `json:"transport_id"`
	ConnectionIdentityDigest  string                        `json:"connection_identity_digest"`
	ConnectionPoolDigest      string                        `json:"connection_pool_digest"`
	TLSProfileDigest          string                        `json:"tls_profile_digest"`
	Normalization             WireNormalizationPlan         `json:"normalization"`
	OrderedHeaders            []changeset3ReferenceHeader   `json:"ordered_headers"`
	Body                      changeset3ReferenceBody       `json:"body"`
	WebSocket                 *changeset3ReferenceWebSocket `json:"websocket,omitempty"`
	DynamicTarget             *changeset3ReferenceDynamic   `json:"dynamic_target,omitempty"`
	SingleUseRawStream        bool                          `json:"single_use_raw_stream"`
	SingleUseConsumptionCount int                           `json:"single_use_consumption_count"`
}

type changeset3ReferenceHeader struct {
	Name        string `json:"name"`
	WireName    string `json:"wire_name"`
	Present     bool   `json:"present,omitempty"`
	Source      string `json:"source,omitempty"`
	ValueKind   string `json:"value_kind"`
	SafeValue   string `json:"safe_value,omitempty"`
	ValueSHA256 string `json:"value_sha256,omitempty"`
}

type changeset3ReferenceBody struct {
	Encoding           profilecontract.BodyEncoding `json:"encoding"`
	OrderedFields      []string                     `json:"ordered_fields"`
	TypeShape          []string                     `json:"type_shape"`
	SyntheticSHA256    string                       `json:"synthetic_sha256"`
	Compression        string                       `json:"compression"`
	AppliedCompression string                       `json:"applied_compression,omitempty"`
	CompressionLevel   int                          `json:"compression_level"`
	FinalWireBytes     int64                        `json:"final_wire_bytes,omitempty"`
	CredentialBearing  bool                         `json:"credential_bearing"`
}

type changeset3ReferenceWebSocket struct {
	HandshakeGeneratedHeaders []string                     `json:"handshake_generated_headers"`
	CompressionOffer          string                       `json:"compression_offer"`
	ContextTakeover           bool                         `json:"context_takeover"`
	EventMatrix               []changeset3ReferenceWSEvent `json:"event_matrix"`
}

type changeset3ReferenceWSEvent struct {
	EventType       string   `json:"event_type"`
	FrameType       string   `json:"frame_type"`
	Policy          string   `json:"policy"`
	FrameOrdinal    uint64   `json:"frame_ordinal,omitempty"`
	OrderedFields   []string `json:"ordered_fields"`
	TypeShapeSHA256 string   `json:"type_shape_sha256"`
	BodySHA256      string   `json:"body_sha256,omitempty"`
}

type changeset3ReferenceDynamic struct {
	SyntheticReturnedURL string `json:"synthetic_returned_url"`
	ValidationResult     string `json:"validation_result"`
}

type changeset3ReferenceTerminal struct {
	guard    *Guard
	captures []changeset3PreIdentityReferenceCapture
	base     changeset3PreIdentityReferenceCapture
}

func (p *changeset3ReferenceTerminal) SendHTTPUpstream(
	_ context.Context,
	prepared PreparedRequest,
) (*http.Response, error) {
	return p.captureHTTP(prepared)
}

func (p *changeset3ReferenceTerminal) SendReqProfile(
	_ context.Context,
	prepared PreparedRequest,
) (*http.Response, error) {
	return p.captureHTTP(prepared)
}

func (p *changeset3ReferenceTerminal) captureHTTP(
	prepared PreparedRequest,
) (*http.Response, error) {
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	transport := prepared.Transport()
	applyChangeset3ReferenceWireNormalization(request, transport.Normalization)
	decision := p.guard.Evaluate(request, transport.Backend, transport.Protocol)
	p.base.TerminalGuardAllow = decision.Allow
	p.base.TerminalGuardReason = decision.RejectionReason
	if err := p.appendCapture(request, transport); err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(bytes.NewReader(nil)),
		Request:    request,
	}, nil
}

func (p *changeset3ReferenceTerminal) AcquireWebSocket(
	_ context.Context,
	prepared PreparedRequest,
) (WebSocketConnection, error) {
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	transport := prepared.Transport()
	applyChangeset3ReferenceWireNormalization(request, transport.Normalization)
	decision := p.guard.Evaluate(request, transport.Backend, transport.Protocol)
	p.base.TerminalGuardAllow = decision.Allow
	p.base.TerminalGuardReason = decision.RejectionReason
	if err := p.appendCapture(request, transport); err != nil {
		return nil, err
	}
	return changeset3ReferenceWSConnection{}, nil
}

func (p *changeset3ReferenceTerminal) appendCapture(
	request *http.Request,
	transport TransportSpec,
) error {
	capture := p.base
	identity, ok := AttemptIdentityFromContext(request.Context())
	if !ok || !identity.HasFinalizationToken {
		return errors.New("迁移前参考 terminal 缺少 FinalizationToken")
	}
	capture.AuthorityID = identity.ExecutorID
	capture.HasFinalizationToken = identity.HasFinalizationToken
	capture.AttemptOrdinal = identity.AttemptOrdinal
	capture.AttemptReason = identity.AttemptReason
	capture.ReleaseDigest = identity.ReleaseDigest
	capture.ProfileDigest = identity.ProfileDigest
	capture.BundleDigest = identity.BundleDigest
	capture.Backend = transport.Backend
	capture.AdapterID = transport.Adapter
	capture.TransportID = transport.ID
	capture.ConnectionPoolDigest = transport.ConnectionPoolDigest
	capture.Normalization = transport.Normalization
	capture.FinalHost = request.URL.Hostname()
	capture.FinalPath = request.URL.EscapedPath()
	capture.TLSProfileDigest = changeset3ReferenceDigest(transport.TLS)
	capture.ConnectionIdentityDigest = changeset3ReferenceConnectionDigest(
		capture, request.URL,
	)
	capture.OrderedHeaders = changeset3ReferenceHeaders(request, transport)
	body, bodyErr := changeset3ReferenceReadBody(request)
	if bodyErr != nil {
		return bodyErr
	}
	if capture.SingleUseRawStream {
		capture.SingleUseConsumptionCount = 1
	}
	capture.Body = changeset3ReferenceBodyEvidence(
		capture.EndpointID, body, capture.ProfileDigest,
	)
	if capture.Protocol == WireProtocolWebSocket {
		capture.WebSocket = changeset3ReferenceWebSocketEvidence(capture.EndpointID, transport)
	}
	capture.ProfileValidationResult = "passed"
	p.captures = append(p.captures, capture)
	return nil
}

type changeset3ReferenceWSConnection struct{}

func (changeset3ReferenceWSConnection) ReadMessage(context.Context) ([]byte, error) {
	return nil, io.EOF
}
func (changeset3ReferenceWSConnection) WriteMessage(context.Context, []byte) error { return nil }
func (changeset3ReferenceWSConnection) Close() error                               { return nil }

// TestGenerateChangeset3PreIdentityAuthorityReference 只向显式临时目录输出候选参考。
// 受审资产必须在人工检查和 secret scan 后用独立补丁提升，测试不会直接改写仓库。
func TestGenerateChangeset3PreIdentityAuthorityReference(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset3PreIdentityReferenceOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定迁移前参考临时目录时生成")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("迁移前参考输出目录必须是绝对路径")
	}
	reference := changeset3BuildPreIdentityAuthorityReference(t)
	raw := changeset3ReferenceMarshal(t, reference)
	if err := os.MkdirAll(output, 0o755); err != nil {
		t.Fatal(err)
	}
	manifestPath := filepath.Join(output, "manifest.json")
	if err := os.WriteFile(manifestPath, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	secretScan := changeset3ReferenceSecretScan(t, raw)
	if err := os.WriteFile(
		filepath.Join(output, "secret-scan.json"),
		changeset3ReferenceMarshal(t, secretScan),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
}

func changeset3BuildPreIdentityAuthorityReference(t *testing.T) changeset3PreIdentityReference {
	t.Helper()
	const authority = ExecutorID("codex.executor.changeset1b")
	anchors := map[SinkID]bool{
		SinkCodexAdminTestCompact:       true,
		SinkCodexAdminTestResponses:     true,
		SinkCodexAlphaSearchPATFallback: true,
		SinkCodexUsageProbe:             true,
	}
	sources := changeset3ReferenceSources(t)
	allCaptures := make([]changeset3PreIdentityReferenceCapture, 0, 56)
	routeKeys := make(map[string]bool)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		for _, binding := range DefaultSinkCatalog().Bindings() {
			if binding.Persona() != PersonaCodexCLI ||
				binding.EndpointEvidence() != EndpointEvidenceCodexProfile ||
				!binding.RuntimeBindable() || binding.EnforcementState() != SinkStateEnforced {
				continue
			}
			for routeIndex, route := range binding.Routes() {
				routeKeys[route.Key.String()+"\x00"+string(binding.ID())] = true
				capture := changeset3CapturePreIdentityRoute(
					t, authority, mode, binding, route, anchors[binding.ID()], routeIndex,
				)
				allCaptures = append(allCaptures, capture)
			}
		}
	}
	sort.Slice(allCaptures, func(i, j int) bool {
		left, right := allCaptures[i], allCaptures[j]
		return strings.Join([]string{string(left.ReleaseMode), left.SinkID, left.Method, left.PathTemplate}, "\x00") <
			strings.Join([]string{string(right.ReleaseMode), right.SinkID, right.Method, right.PathTemplate}, "\x00")
	})
	if len(routeKeys) != 28 || len(allCaptures) != 56 {
		t.Fatalf("迁移前参考范围错误：routes=%d captures=%d", len(routeKeys), len(allCaptures))
	}
	return changeset3PreIdentityReference{
		SchemaVersion:   "changeset3-pre-identity-authority-reference/v1",
		ReferenceKind:   "pre_identity_authority_refactor_reference",
		CaptureBoundary: "production_executor_adapter_after_normalization_before_local_terminal_send",
		ExternalTraffic: false, CredentialMaterial: "synthetic_only",
		RouteCount:   28,
		ReleaseModes: []ReleaseMode{ReleaseModeActive, ReleaseModePrevious},
		CaptureCount: len(allCaptures), SourceMaterial: sources,
		KnownPreRefFindings: []string{
			"responses_ws 的当前 normalization 在 coder websocket 自动补 compression offer 后触发 terminal Guard 的 request_modified_after_finalize；参考如实冻结该迁移前缺陷，修复后必须变为 allow。",
		},
		Captures:  allCaptures,
		Redaction: "只使用合成凭据、账号、动态 ID、文件和正文；认证值仅记录类型，不保存值或可关联摘要。",
	}
}

func changeset3CapturePreIdentityRoute(
	t *testing.T,
	authority ExecutorID,
	mode ReleaseMode,
	binding SinkBinding,
	route CatalogRoute,
	anchor bool,
	routeIndex int,
) changeset3PreIdentityReferenceCapture {
	return changeset3CaptureRouteWithCatalogs(
		t, authority, mode, binding, route, anchor, routeIndex,
		DefaultSinkCatalog(), DefaultOfficialRouteCatalog(),
	)
}

func changeset3CaptureRouteWithCatalogs(
	t *testing.T,
	authority ExecutorID,
	mode ReleaseMode,
	binding SinkBinding,
	route CatalogRoute,
	anchor bool,
	routeIndex int,
	sinks SinkCatalog,
	routes OfficialRouteCatalog,
) changeset3PreIdentityReferenceCapture {
	t.Helper()
	baseGuard := DefaultGuard()
	guard, err := NewGuard(baseGuard.Config(), sinks, routes, baseGuard.Recorder())
	if err != nil {
		t.Fatal(err)
	}
	terminal := &changeset3ReferenceTerminal{guard: guard}
	httpAdapter, err := NewHTTPUpstreamTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	reqAdapter, err := NewReqProfileTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	wsAdapter, err := NewWebSocketTransportAdapter(terminal)
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(DefaultBackendDescriptors(), []TransportAdapter{httpAdapter, reqAdapter, wsAdapter})
	if err != nil {
		t.Fatal(err)
	}
	executor, err := NewExecutor(authority, NewCompiler(), registry, guard)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), sinks)
	if err != nil {
		t.Fatal(err)
	}
	execution := ExecutionPolicy{
		ID: "changeset3.pre-reference.execution", Source: "docs/egress/migration",
		MaxAttempts: 1, Replayable: binding.ID() != SinkCodexFilesBlobUpload,
		ConcurrencyLimit: 1,
	}
	deployment := DeploymentSupportPolicy{
		ID: "changeset3.pre-reference.deployment", Source: "docs/egress/migration",
		Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: "direct",
		SupportedBackends: []BackendKind{BackendHTTPUpstream, BackendReqProfile, BackendWebSocket},
	}
	behavior := BehaviorPolicy{
		ID: "changeset3.pre-reference.behavior", Source: "docs/egress/migration",
		Kind: BehaviorUserRequest, AttemptBudget: 1,
	}
	bundle, err := resolver.Resolve(BundleResolveRequest{
		SinkID: binding.ID(), Mode: mode, Execution: execution,
		Deployment: deployment, Behavior: behavior,
	})
	if err != nil {
		t.Fatal(err)
	}
	target, endpointID, dynamic := changeset3ReferenceTarget(t, bundle, route)
	body, singleUse := changeset3ReferenceSyntheticBody(t, bundle, endpointID)
	identityFacts := executorInvocationIdentityFacts(t)
	authenticationInput := AttemptAuthenticationInput{BearerToken: "synthetic-access-token"}
	if endpoint, ok := changeset3ReferenceEndpoint(bundle.Release().Profile(), endpointID); ok {
		for _, slot := range endpoint.Headers {
			if slot.Condition == profilecontract.ConditionCookiePresent {
				identityFacts.Conditions.CookiePresent = true
				authenticationInput.Cookie = "synthetic-cookie"
				break
			}
		}
	}
	authentication, err := NewAttemptAuthentication(authenticationInput)
	if err != nil {
		t.Fatal(err)
	}
	requestBody := NewReplayableRequestBody(body)
	if singleUse {
		requestBody, err = NewSingleUseRequestBody(io.NopCloser(bytes.NewReader(body)), int64(len(body)))
		if err != nil {
			t.Fatal(err)
		}
	}
	terminal.base = changeset3PreIdentityReferenceCapture{
		SinkID: string(binding.ID()), Anchor: anchor, ReleaseMode: mode,
		Method: route.Key.Method, HostTemplate: route.Key.Host, PathTemplate: route.Key.Path,
		Protocol: route.Protocol, Purpose: binding.Purpose(), EndpointID: endpointID,
		SingleUseRawStream: singleUse,
	}
	if dynamic.ReturnedURL != nil {
		terminal.base.DynamicTarget = &changeset3ReferenceDynamic{
			SyntheticReturnedURL: dynamic.ReturnedURL.String(), ValidationResult: "accepted_by_bundle_dynamic_target_policy",
		}
	}
	result, err := executor.Execute(context.Background(), ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: binding.ID(), Purpose: binding.Purpose(), EndpointID: endpointID,
			Mode: mode, Protocol: route.Protocol, Method: route.Key.Method, URL: target,
			Headers: make(http.Header), IdentityMode: IdentityCodexOAuthStrict,
			IdentityFacts: identityFacts, Authentication: authentication,
			HeaderPolicy:   HeaderPolicy{ID: "changeset3.pre-reference.headers", Source: "docs/egress/migration"},
			BodyPolicy:     BodyPolicy{ID: "changeset3.pre-reference.body", Source: "docs/egress/migration"},
			BehaviorPolicy: behavior, Body: requestBody,
			InvocationID:    fmt.Sprintf("pre-reference-%s-%s-%d", mode, strings.ReplaceAll(string(binding.ID()), ".", "-"), routeIndex),
			DeclaredPersona: PersonaCodexCLI,
		},
		DynamicInputs: dynamic, AttemptReason: AttemptReasonInitial,
		ExpectedAttemptOrdinal: 1, ExecutionScopeKey: "synthetic-account",
	})
	if err != nil {
		t.Fatalf("捕获 %s %s %s：%v", mode, binding.ID(), route.Key.String(), err)
	}
	if response := result.HTTPResponse(); response != nil && response.Body != nil {
		_ = response.Body.Close()
	}
	if connection := result.WebSocket(); connection != nil {
		_ = connection.Close()
	}
	if len(terminal.captures) != 1 {
		t.Fatalf("捕获 %s %s terminal 次数=%d", mode, binding.ID(), len(terminal.captures))
	}
	return terminal.captures[0]
}

func changeset3ReferenceTarget(
	t *testing.T,
	bundle ReleaseBundle,
	route CatalogRoute,
) (*url.URL, string, EndpointDynamicInputs) {
	t.Helper()
	host := route.Key.Host
	if strings.HasPrefix(host, "*.") {
		host = "upload" + strings.TrimPrefix(host, "*")
	}
	path := route.Key.Path
	path = strings.ReplaceAll(path, "{server_returned_path}", "synthetic/upload/blob")
	path = strings.ReplaceAll(path, "{file_id}", "synthetic-file-id")
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	scheme := "https"
	if route.Protocol == WireProtocolWebSocket {
		scheme = "wss"
	}
	target := &url.URL{Scheme: scheme, Host: host, Path: path}
	plan, err := bundle.ResolveEndpointPlan(
		bundle.PrimarySinkID(), route.Key.Method, target, route.Protocol,
	)
	if err != nil {
		t.Fatal(err)
	}
	endpoint, ok := changeset3ReferenceEndpoint(bundle.Release().Profile(), plan.EndpointID())
	if !ok {
		t.Fatalf("ProfileSpec 缺少 endpoint %s", plan.EndpointID())
	}
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
	dynamic := EndpointDynamicInputs{}
	if plan.DynamicTarget() {
		dynamic.ReturnedURL = target
	}
	return target, plan.EndpointID(), dynamic
}

func changeset3ReferenceSyntheticHeaders(
	t *testing.T,
	bundle ReleaseBundle,
	endpointID string,
) http.Header {
	t.Helper()
	endpoint, ok := changeset3ReferenceEndpoint(bundle.Release().Profile(), endpointID)
	if !ok {
		t.Fatalf("ProfileSpec 缺少 endpoint %s", endpointID)
	}
	headers := make(http.Header)
	node, ok := bundle.Release().Node(changeset3ReferenceReleasePurpose(bundle, endpointID))
	if !ok {
		t.Fatalf("Release 缺少 endpoint %s 的 node", endpointID)
	}
	for _, slot := range endpoint.Headers {
		if slot.Condition != "" && slot.Condition != profilecontract.ConditionAlways &&
			slot.Condition != profilecontract.ConditionAuto {
			continue
		}
		name := slot.WireName
		if name == "" {
			name = slot.Name
		}
		if officialCodexGeneratedHeaderForReference(name) {
			continue
		}
		value := slot.Value
		if value == "" {
			switch strings.ToLower(slot.Name) {
			case "authorization":
				value = "Bearer synthetic-access-token"
			case "chatgpt-account-id":
				value = "synthetic-account"
			case "user-agent":
				value = node.Build.UserAgent
			case "originator":
				value = node.Build.Originator
			default:
				value = "synthetic-" + strings.ReplaceAll(strings.ToLower(slot.Name), "_", "-")
			}
		}
		headers.Set(name, value)
	}
	return headers
}

func changeset3ReferenceEndpoint(
	profile profilecontract.ProfileSpec,
	endpointID string,
) (profilecontract.EndpointProfile, bool) {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == endpointID {
			return endpoint, true
		}
	}
	return profilecontract.EndpointProfile{}, false
}

func changeset3ReferenceReleasePurpose(bundle ReleaseBundle, endpointID string) string {
	for _, plan := range bundle.EndpointPlans() {
		if plan.EndpointID() == endpointID {
			return plan.template.binding.ReleasePurpose()
		}
	}
	return ""
}

func changeset3ReferenceSyntheticBody(
	t *testing.T,
	bundle ReleaseBundle,
	endpointID string,
) ([]byte, bool) {
	t.Helper()
	endpoint, ok := changeset3ReferenceEndpoint(bundle.Release().Profile(), endpointID)
	if !ok {
		t.Fatalf("ProfileSpec 缺少 endpoint %s", endpointID)
	}
	if endpoint.Body.Encoding == profilecontract.BodyRawBytes {
		return []byte("synthetic-upload-content\n"), true
	}
	if endpoint.Body.Encoding == profilecontract.BodyNone {
		return nil, false
	}
	values := make(map[string]any)
	for _, field := range endpoint.Body.Fields {
		if !field.Required && field.Name != "client_metadata" {
			continue
		}
		values[field.Name] = changeset3ReferenceSyntheticField(endpointID, field.Name)
	}
	if endpoint.Body.Discriminator != "" {
		parts := strings.SplitN(endpoint.Body.Discriminator, "=", 2)
		if len(parts) == 2 {
			values[parts[0]] = parts[1]
		}
	}
	raw, err := json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	return raw, false
}

func changeset3ReferenceSyntheticField(endpointID string, name string) any {
	switch name {
	case "type":
		if endpointID == "responses_ws" {
			return "response.create"
		}
		return "input_audio_buffer.append"
	case "input", "tools", "include", "commands", "images":
		return []any{}
	case "parallel_tool_calls", "store", "stream", "generate":
		return false
	case "reasoning", "settings", "session", "client_metadata":
		return map[string]any{}
	case "file_size", "max_output_tokens":
		return 24
	case "grant_type":
		return "refresh_token"
	case "refresh_token":
		return "synthetic-refresh-token"
	case "model":
		return "gpt-5.6-sol"
	case "tool_choice":
		return "auto"
	case "use_case":
		return "assistants"
	default:
		return "synthetic-" + strings.ReplaceAll(name, "_", "-")
	}
}

func applyChangeset3ReferenceWireNormalization(request *http.Request, plan WireNormalizationPlan) {
	if request == nil {
		return
	}
	if plan.HeaderMode == HeaderNormalizationLowercase {
		headers := make(http.Header, len(request.Header)+1)
		for name, values := range request.Header {
			lower := strings.ToLower(name)
			current, exists := headers[lower]
			// adapter 已可能同时放入小写官方 UA 与 net/http 空值哨兵；二次
			// 观察归一化遇到 casing 冲突时必须稳定保留非空 wire 值。
			if !exists || (allHeaderValuesEmpty(current) && !allHeaderValuesEmpty(values)) {
				headers[lower] = append([]string(nil), values...)
			}
		}
		if plan.SuppressDefaultUserAgent {
			// 保留小写画像 UA，同时加入 net/http 专用空值哨兵，阻止 transport
			// 自动写入 Go 默认 UA。证据读取必须按 wire casing 取小写值。
			headers["User-Agent"] = []string{""}
		}
		request.Header = headers
	}
	if offer := strings.TrimSpace(plan.WebSocketCompressionOffer); offer != "" {
		request.Header.Set("Sec-WebSocket-Extensions", offer)
	}
}

func changeset3ReferenceHeaders(
	request *http.Request,
	transport TransportSpec,
) []changeset3ReferenceHeader {
	order := changeset3ReferenceHeaderOrder(request, transport)
	result := make([]changeset3ReferenceHeader, 0, len(order))
	for _, name := range order {
		value, _ := changeset3ReferenceExactHeaderEntry(request.Header, name)
		entry := changeset3ReferenceHeader{Name: strings.ToLower(name), WireName: name}
		switch strings.ToLower(name) {
		case "authorization", "cookie":
			entry.ValueKind = "attempt_authentication"
		case "chatgpt-account-id", "session-id", "conversation-id", "thread-id", "x-client-request-id":
			entry.ValueKind = "synthetic_dynamic_identity"
		case "sec-websocket-key":
			entry.ValueKind = "transport_generated_synthetic_placeholder"
		default:
			entry.ValueKind = "static_or_synthetic_safe"
			entry.SafeValue = value
			entry.ValueSHA256 = changeset3ReferenceSHA256([]byte(value))
		}
		result = append(result, entry)
	}
	return result
}

func changeset3ReferenceExactHeaderEntry(headers http.Header, name string) (string, bool) {
	if values, exists := headers[name]; exists && len(values) != 0 {
		return values[0], true
	}
	var candidates []string
	for candidate := range headers {
		if strings.EqualFold(candidate, name) {
			candidates = append(candidates, candidate)
		}
	}
	sort.Strings(candidates)
	for _, candidate := range candidates {
		values := headers[candidate]
		if len(values) != 0 && values[0] != "" {
			return values[0], true
		}
	}
	for _, candidate := range candidates {
		if values := headers[candidate]; len(values) != 0 {
			return values[0], true
		}
	}
	return "", false
}

func changeset3ReferenceHeaderOrder(request *http.Request, transport TransportSpec) []string {
	for _, rule := range transport.TLS.H1HeaderOrders {
		if strings.EqualFold(rule.Method, request.Method) &&
			(rule.Path == request.URL.EscapedPath() || strings.Contains(rule.Path, "{")) {
			order := append([]string(nil), rule.Order...)
			if rule.Mode == "swap_remove" {
				order = append(order, rule.AppendHeaders...)
			}
			return order
		}
	}
	names := make([]string, 0, len(request.Header))
	for name := range request.Header {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func changeset3ReferenceReadBody(request *http.Request) ([]byte, error) {
	if request == nil || request.Body == nil || request.Body == http.NoBody {
		return nil, nil
	}
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	request.Body = io.NopCloser(bytes.NewReader(body))
	return body, nil
}

func changeset3ReferenceBodyEvidence(
	endpointID string,
	body []byte,
	_ string,
) changeset3ReferenceBody {
	profile, _ := DefaultReleaseCatalog().Resolve(ReleaseModeActive)
	endpoint, _ := changeset3ReferenceEndpoint(profile.Profile(), endpointID)
	evidence := changeset3ReferenceBody{
		Encoding:          endpoint.Body.Encoding,
		SyntheticSHA256:   changeset3ReferenceSHA256(body),
		Compression:       string(endpoint.Compression),
		CompressionLevel:  profile.Profile().Features().RequestCompressionLevel,
		CredentialBearing: endpointID == "oauth_refresh",
	}
	if endpoint.Body.Encoding == profilecontract.BodyJson ||
		endpoint.Body.Encoding == profilecontract.BodyWebsocketJson ||
		endpoint.Body.Encoding == profilecontract.BodyWebsocketDiscriminatedEvents {
		pairs, err := decodeOrderedJSONObject(body)
		if err == nil {
			for _, pair := range pairs {
				evidence.OrderedFields = append(evidence.OrderedFields, pair.name)
				evidence.TypeShape = append(evidence.TypeShape, pair.name+":"+changeset3ReferenceJSONType(pair.value))
			}
		}
	}
	return evidence
}

type changeset3ReferenceJSONPair struct {
	name  string
	value json.RawMessage
}

func decodeOrderedJSONObject(raw []byte) ([]changeset3ReferenceJSONPair, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, errors.New("不是 JSON object")
	}
	var pairs []changeset3ReferenceJSONPair
	for decoder.More() {
		nameToken, tokenErr := decoder.Token()
		if tokenErr != nil {
			return nil, tokenErr
		}
		name, ok := nameToken.(string)
		if !ok {
			return nil, errors.New("JSON 字段名非法")
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		pairs = append(pairs, changeset3ReferenceJSONPair{name: name, value: value})
	}
	return pairs, nil
}

func changeset3ReferenceJSONType(raw json.RawMessage) string {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return "empty"
	}
	switch trimmed[0] {
	case '{':
		return "object"
	case '[':
		return "array"
	case '"':
		return "string"
	case 't', 'f':
		return "boolean"
	case 'n':
		return "null"
	default:
		return "number"
	}
}

func changeset3ReferenceWebSocketEvidence(
	endpointID string,
	transport TransportSpec,
) *changeset3ReferenceWebSocket {
	ws := &changeset3ReferenceWebSocket{
		HandshakeGeneratedHeaders: []string{
			"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key",
		},
		CompressionOffer: transport.Normalization.WebSocketCompressionOffer,
	}
	profile, _ := DefaultReleaseCatalog().Resolve(ReleaseModeActive)
	for _, candidate := range profile.Profile().Transports() {
		if candidate.ID == transport.ID && candidate.WebSocket != nil {
			ws.ContextTakeover = candidate.WebSocket.ContextTakeover
		}
	}
	if endpointID == "responses_ws" {
		ws.EventMatrix = []changeset3ReferenceWSEvent{
			changeset3ReferenceEvent("response.create", "text", "profile_finalized", []string{
				"type", "model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include", "client_metadata",
			}),
		}
	} else {
		ws.EventMatrix = []changeset3ReferenceWSEvent{
			changeset3ReferenceEvent("input_audio_buffer.append", "text", "profile_declared_transparent_text", []string{"type", "audio"}),
			changeset3ReferenceEvent("opaque_binary", "binary", "profile_declared_transparent_binary", nil),
		}
	}
	return ws
}

func changeset3ReferenceEvent(
	eventType string,
	frameType string,
	policy string,
	fields []string,
) changeset3ReferenceWSEvent {
	return changeset3ReferenceWSEvent{
		EventType: eventType, FrameType: frameType, Policy: policy,
		OrderedFields: fields,
		TypeShapeSHA256: changeset3ReferenceDigest(struct {
			EventType string
			FrameType string
			Fields    []string
		}{eventType, frameType, fields}),
	}
}

func officialCodexGeneratedHeaderForReference(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "host", "content-length", "transfer-encoding", "connection", "upgrade",
		"sec-websocket-version", "sec-websocket-key", "sec-websocket-extensions":
		return true
	default:
		return false
	}
}

func changeset3ReferenceConnectionDigest(
	capture changeset3PreIdentityReferenceCapture,
	target *url.URL,
) string {
	return changeset3ReferenceDigest(struct {
		ReleaseDigest string
		BundleDigest  string
		TransportID   string
		Scheme        string
		Host          string
	}{capture.ReleaseDigest, capture.BundleDigest, capture.TransportID, target.Scheme, target.Host})
}

func changeset3ReferenceSources(t *testing.T) []changeset3ReferenceSource {
	t.Helper()
	paths := []string{
		"compiler.go",
		"executor.go",
		"release_catalog.go",
		"catalogdata/release-catalog.json",
		"../service/official_egress_http_invocation.go",
		"../service/official_egress_websocket_invocation.go",
		"../service/official_egress_transport_adapters.go",
		"../service/official_egress_codex_integration.go",
		"changeset3_pre_identity_authority_reference_test.go",
	}
	result := make([]changeset3ReferenceSource, 0, len(paths))
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		result = append(result, changeset3ReferenceSource{Path: path, SHA256: changeset3ReferenceSHA256(raw)})
	}
	return result
}

func changeset3ReferenceSecretScan(t *testing.T, manifest []byte) any {
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
		t.Fatalf("迁移前参考泄漏合成认证值：%v", matches)
	}
	return struct {
		SchemaVersion  string         `json:"schema_version"`
		Artifact       string         `json:"artifact"`
		ArtifactSHA256 string         `json:"artifact_sha256"`
		Matches        map[string]int `json:"matches"`
		Result         string         `json:"result"`
	}{
		SchemaVersion: "changeset3-secret-scan/v1", Artifact: "manifest.json",
		ArtifactSHA256: changeset3ReferenceSHA256(manifest), Matches: matches, Result: "passed",
	}
}

func changeset3ReferenceMarshal(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func changeset3ReferenceDigest(value any) string {
	raw, _ := json.Marshal(value)
	return changeset3ReferenceSHA256(raw)
}

func changeset3ReferenceSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
