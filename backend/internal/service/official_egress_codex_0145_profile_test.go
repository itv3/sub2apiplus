package service

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func officialCodex0145RequiredEndpointIDs() []string {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		panic(err)
	}
	ids := make([]string, 0, len(profile.Endpoints))
	for _, endpoint := range profile.Endpoints {
		ids = append(ids, endpoint.ID)
	}
	return ids
}

func TestOfficialCodexRemoteCompactionV2DefaultFollowsExplicitReleaseMode(t *testing.T) {
	base, err := resolveCodexVersionProfileForMode(officialClientProfileModeActive)
	if err != nil {
		t.Fatal(err)
	}
	active, err := cloneOfficialCodexVersionProfile(base)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := cloneOfficialCodexVersionProfile(base)
	if err != nil {
		t.Fatal(err)
	}
	active.FeatureDefaults.RemoteCompactionV2 = true
	previous.FeatureDefaults.RemoteCompactionV2 = false

	resolver := func(mode string) (*officialCodexVersionProfile, error) {
		switch mode {
		case officialClientProfileModeActive:
			return &active, nil
		case officialClientProfileModePrevious:
			return &previous, nil
		default:
			return nil, fmt.Errorf("测试 release mode 非法：%s", mode)
		}
	}
	activeEnabled, err := resolveOfficialCodexRemoteCompactionV2Default(
		officialClientProfileModeActive,
		resolver,
	)
	if err != nil {
		t.Fatal(err)
	}
	previousEnabled, err := resolveOfficialCodexRemoteCompactionV2Default(
		officialClientProfileModePrevious,
		resolver,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !activeEnabled || previousEnabled {
		t.Fatalf("compaction feature 发生跨 mode 混搭：active=%t previous=%t", activeEnabled, previousEnabled)
	}
	if _, err := resolveOfficialCodexRemoteCompactionV2Default("", resolver); err == nil {
		t.Fatal("空 release mode 不得退化为 active")
	}
}

func TestOfficialCodex0145ExactResolution(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatalf("解析精确版本失败：%v", err)
	}
	if profile.Version != officialCodexVersion0145 {
		t.Fatalf("版本不一致：%q", profile.Version)
	}

	for _, version := range []string{"", "0.145", "0.145.1", " 0.145.0", "0.145.0 ", "v0.145.0"} {
		if _, err := resolveCodexVersionProfile(version); err == nil {
			t.Errorf("近似或未知版本不应解析成功：%q", version)
		}
	}

	userAgentCases := []struct {
		name          string
		surfaceID     string
		includeSuffix bool
		want          string
	}{
		{
			name: "exec 无 suffix", surfaceID: officialCodexSurfaceExec,
			want: "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown",
		},
		{
			name: "exec 含 suffix", surfaceID: officialCodexSurfaceExec, includeSuffix: true,
			want: "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)",
		},
		{
			name: "TUI 无 suffix", surfaceID: officialCodexSurfaceTUI,
			want: "codex-tui/0.145.0 (Ubuntu 24.4.0; x86_64) unknown",
		},
		{
			name: "TUI 含 suffix", surfaceID: officialCodexSurfaceTUI, includeSuffix: true,
			want: "codex-tui/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex-tui; 0.145.0)",
		},
	}
	for _, testCase := range userAgentCases {
		t.Run(testCase.name, func(t *testing.T) {
			got, err := profile.RenderUserAgent(testCase.surfaceID, testCase.includeSuffix)
			if err != nil {
				t.Fatalf("生成 UA 失败：%v", err)
			}
			if got != testCase.want {
				t.Fatalf("UA 不一致：\n实际：%s\n期望：%s", got, testCase.want)
			}
		})
	}
	for _, surfaceID := range []string{"", "Exec", " exec", "tui "} {
		if _, err := profile.RenderUserAgent(surfaceID, false); err == nil {
			t.Errorf("未知或近似入口不应解析成功：%q", surfaceID)
		}
	}

	for _, endpointID := range officialCodex0145RequiredEndpointIDs() {
		endpoint, err := resolveCodexEndpoint(officialCodexVersion0145, codexEndpointID(endpointID))
		if err != nil {
			t.Errorf("解析必需端点 %s 失败：%v", endpointID, err)
			continue
		}
		if endpoint.ID != endpointID {
			t.Errorf("端点 ID 不一致：实际 %s，期望 %s", endpoint.ID, endpointID)
		}
	}
	for _, endpointID := range []codexEndpointID{"", "models ", "Models", "/backend-api/codex/models"} {
		if _, err := resolveCodexEndpoint(officialCodexVersion0145, endpointID); err == nil {
			t.Errorf("未知或近似端点不应解析成功：%q", endpointID)
		}
	}
	if _, err := resolveCodexEndpoint("0.145.1", codexEndpointID(officialCodexEndpointModels)); err == nil {
		t.Error("端点解析不得绕过精确版本检查")
	}

	var nilProfile *officialCodexVersionProfile
	if _, err := nilProfile.ResolveEndpoint(officialCodexEndpointModels); err == nil {
		t.Error("空画像解析端点应失败")
	}
	if _, err := nilProfile.RenderUserAgent(officialCodexSurfaceExec, false); err == nil {
		t.Error("空画像生成 UA 应失败")
	}
}

func TestOfficialCodex0145RequiredRuleAndEndpointUniverse(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	expectedRules := []string{
		"SPEC-TLS-001", "SPEC-TLS-003", "SPEC-PROTO-001", "SPEC-PROTO-002",
		"SPEC-CONN-001", "SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003",
		"SPEC-H1-004", "SPEC-WS-001", "SPEC-WS-002", "SPEC-WS-004",
		"SPEC-WS-005", "SPEC-HDR-001", "SPEC-HDR-002", "SPEC-HDR-004",
		"SPEC-HDR-005", "SPEC-HDR-006", "SPEC-HDR-007", "SPEC-HDR-008",
		"SPEC-BODY-001", "SPEC-BODY-002", "SPEC-BODY-003", "SPEC-BODY-004",
		"SPEC-BODY-005", "SPEC-BODY-006", "SPEC-EP-001", "SPEC-EP-002",
		"SPEC-EP-005", "SPEC-EP-006", "SPEC-EP-007", "SPEC-EP-008",
		"SPEC-EP-009", "SPEC-EP-012", "SPEC-EP-013", "SPEC-EP-014",
		"SPEC-EP-015", "SPEC-EP-019", "SPEC-EP-020", "SPEC-EP-021",
		"SPEC-EP-022", "SPEC-EP-023",
	}
	if len(profile.RequiredRules) != 0 {
		t.Fatalf("证据专用 RequiredRules 不得进入 service 可执行投影：%v", profile.RequiredRules)
	}
	formal := mustOfficialCodexHistoricalFixture(t)
	var evidenceRules []string
	if err := json.Unmarshal(formal.Profile().ToSnapshot().RequiredRules, &evidenceRules); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(evidenceRules, expectedRules) {
		t.Fatalf("42 条 required SPEC ID 不一致：\n实际：%v\n期望：%v", evidenceRules, expectedRules)
	}

	expectedEndpoints := []string{
		officialCodexEndpointModels,
		officialCodexEndpointResponsesHTTP,
		officialCodexEndpointResponsesWS,
		officialCodexEndpointResponsesCompact,
		officialCodexEndpointAlphaSearch,
		officialCodexEndpointImagesGenerations,
		officialCodexEndpointImagesEdits,
		officialCodexEndpointRealtimeCalls,
		officialCodexEndpointRealtimeSideband,
		officialCodexEndpointWhamUsage,
		officialCodexEndpointWhamResetCredits,
		officialCodexEndpointWhamConsumeResetCredit,
		officialCodexEndpointOAuthRefresh,
		officialCodexEndpointFilesCreate,
		officialCodexEndpointFilesBlobUpload,
		officialCodexEndpointFilesUploaded,
	}
	actualEndpoints := make([]string, 0, len(profile.Endpoints))
	for _, endpoint := range profile.Endpoints {
		actualEndpoints = append(actualEndpoints, endpoint.ID)
	}
	if !reflect.DeepEqual(actualEndpoints, expectedEndpoints) {
		t.Fatalf("必需端点全集不一致：\n实际：%v\n期望：%v", actualEndpoints, expectedEndpoints)
	}
}

func TestOfficialCodex0145FeatureAndTransportProfile(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	expectedFeatures := officialCodexFeatureDefaults{
		SupportsWebSockets:             true,
		RemoteCompactionV2:             true,
		EnableRequestCompression:       true,
		RequestCompressionLevel:        3,
		RuntimeMetrics:                 false,
		ForceHTTPFallback:              false,
		ResponsesLiteFromModelManifest: true,
		ParallelToolsFromModelManifest: true,
	}
	if !reflect.DeepEqual(profile.FeatureDefaults, expectedFeatures) {
		t.Fatalf("FeatureDefaults 不一致：%+v", profile.FeatureDefaults)
	}

	httpTransport := mustOfficialCodex0145Transport(t, profile, officialCodexTransportHTTPDefault)
	if httpTransport.Protocol != "http/1.1" || httpTransport.TLSStack != "native_tls_openssl" {
		t.Fatalf("默认 HTTP 协议或 TLS 栈不一致：%+v", httpTransport)
	}
	expectedHTTPCiphers := []uint16{
		0x1302, 0x1303, 0x1301, 0xc02c, 0xc030, 0x009f,
		0xcca9, 0xcca8, 0xccaa, 0xc02b, 0xc02f, 0x009e,
		0xc024, 0xc028, 0x006b, 0xc023, 0xc027, 0x0067,
		0xc00a, 0xc014, 0x0039, 0xc009, 0xc013, 0x0033,
		0x009d, 0x009c, 0x003d, 0x003c, 0x0035, 0x002f,
	}
	if !reflect.DeepEqual(httpTransport.CipherSuites, expectedHTTPCiphers) {
		t.Fatalf("默认 HTTP 30 cipher 画像不一致：%v", httpTransport.CipherSuites)
	}
	if len(httpTransport.ALPN) != 0 || !httpTransport.LowercaseHTTPHeaders {
		t.Fatalf("默认 HTTP ALPN 或大小写画像不一致：%+v", httpTransport)
	}
	if !reflect.DeepEqual(httpTransport.Extensions, []uint16{65281, 0, 11, 10, 35, 22, 23, 13, 43, 45, 51}) {
		t.Fatalf("默认 HTTP 扩展集合不一致：%v", httpTransport.Extensions)
	}

	wsTransport := mustOfficialCodex0145Transport(t, profile, officialCodexTransportWS)
	expectedWSCiphers := []uint16{0x1302, 0x1301, 0x1303, 0xc02c, 0xc02b, 0xcca9, 0xc030, 0xc02f, 0xcca8, 0x00ff}
	if wsTransport.Protocol != "websocket" || wsTransport.TLSStack != "rustls" ||
		!reflect.DeepEqual(wsTransport.CipherSuites, expectedWSCiphers) || !wsTransport.RandomizeExtensions {
		t.Fatalf("WS TLS 画像不一致：%+v", wsTransport)
	}
	if !reflect.DeepEqual(wsTransport.Extensions, []uint16{0, 5, 10, 11, 13, 23, 35, 43, 45, 51}) {
		t.Fatalf("WS 扩展集合不一致：%v", wsTransport.Extensions)
	}
	if wsTransport.WebSocket == nil {
		t.Fatal("WS 画像缺少握手与压缩参数")
	}
	expectedPrefix := []string{"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key"}
	if !reflect.DeepEqual(wsTransport.WebSocket.FixedHandshakePrefix, expectedPrefix) ||
		wsTransport.WebSocket.RemainingHeaderMode != officialCodexHeaderOrderWSSwapRemove ||
		wsTransport.WebSocket.CompressionOffer != "permessage-deflate; client_max_window_bits" ||
		!wsTransport.WebSocket.CompressedTextRSV1 || !wsTransport.WebSocket.RawDeflatePayload ||
		!wsTransport.WebSocket.ContextTakeover {
		t.Fatalf("WS 握手或帧压缩画像不一致：%+v", wsTransport.WebSocket)
	}
}

func TestOfficialCodex0145FilesFlowProfile(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	expected := officialCodexFilesProfile{
		CreateEndpointID:         officialCodexEndpointFilesCreate,
		BlobUploadEndpointID:     officialCodexEndpointFilesBlobUpload,
		UploadedEndpointID:       officialCodexEndpointFilesUploaded,
		UploadLimitBytes:         512 * 1024 * 1024,
		RequestTimeoutMillis:     60 * 1000,
		FinalizeTimeoutMillis:    30 * 1000,
		FinalizeRetryDelayMillis: 250,
		UseCase:                  "codex",
		URIPrefix:                "sediment://",
		FinalizeSuccessStatus:    "success",
		FinalizeRetryStatus:      "retry",
	}
	if !reflect.DeepEqual(profile.Files, expected) {
		t.Fatalf("Files 跨端点流程画像不一致：\n实际：%+v\n期望：%+v", profile.Files, expected)
	}
}

func TestOfficialCodex0145EndpointContracts(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}

	testCases := []struct {
		id                string
		method            string
		upgrade           string
		transportID       string
		host              string
		hostFromResponse  bool
		path              string
		query             []officialCodexQueryField
		accept            string
		contentType       string
		compression       string
		clientLifecycle   string
		headerOrderMode   string
		headerNames       []string
		bodyEncoding      string
		bodyClosed        bool
		bodyDiscriminator string
		bodyFields        []string
	}{
		{
			id: officialCodexEndpointModels, method: http.MethodGet,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/models",
			query:  []officialCodexQueryField{{Name: "client_version", Value: "0.145.0", Source: officialCodexSourceConstant, Required: true}},
			accept: "*/*", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"version", "authorization", "chatgpt-account-id", "x-openai-fedramp", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "host"},
			bodyEncoding: "none", bodyClosed: true,
		},
		{
			id: officialCodexEndpointResponsesHTTP, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/responses",
			accept: "text/event-stream", contentType: "application/json", compression: officialCodexCompressionZstdFeature,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames: []string{
				"version", "x-codex-beta-features", "x-codex-turn-state", "x-codex-window-id", "x-codex-turn-metadata",
				"x-openai-subagent", "x-openai-memgen-request", "x-codex-parent-thread-id",
				"x-openai-internal-codex-responses-lite", "x-client-request-id", "session-id", "thread-id", "accept", "content-encoding",
				"content-type", "authorization", "chatgpt-account-id", "x-openai-fedramp", "originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length",
			},
			bodyEncoding: "json", bodyClosed: true,
			bodyFields: []string{"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "stream_options", "include", "service_tier", "prompt_cache_key", "text", "client_metadata"},
		},
		{
			id: officialCodexEndpointResponsesWS, method: http.MethodGet, upgrade: "websocket",
			transportID: officialCodexTransportWS, host: "chatgpt.com", path: "/backend-api/codex/responses",
			compression:     officialCodexCompressionPerMessageDeflate,
			clientLifecycle: officialCodexClientWebSocket, headerOrderMode: officialCodexHeaderOrderWSSwapRemove,
			headerNames: []string{
				"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key", "chatgpt-account-id", "x-openai-fedramp", "authorization",
				"user-agent", "originator", "x-openai-internal-codex-residency", "openai-beta", "version", "x-codex-beta-features",
				"x-client-request-id", "session-id", "thread-id", "x-codex-window-id", "x-codex-turn-metadata", "x-openai-subagent",
				"x-openai-memgen-request", "x-codex-parent-thread-id", "x-responsesapi-include-timing-metrics", "sec-websocket-extensions",
			},
			bodyEncoding: "websocket_json", bodyClosed: true, bodyDiscriminator: "type=response.create",
			bodyFields: []string{"type", "model", "instructions", "previous_response_id", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "stream_options", "include", "service_tier", "prompt_cache_key", "text", "generate", "client_metadata"},
		},
		{
			id: officialCodexEndpointResponsesCompact, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/responses/compact",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames: []string{
				"version", "x-codex-installation-id", "x-codex-beta-features", "x-codex-turn-state", "x-codex-window-id", "x-codex-turn-metadata",
				"x-openai-subagent", "x-openai-memgen-request", "x-codex-parent-thread-id", "session-id", "thread-id", "x-openai-internal-codex-responses-lite", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept",
				"originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length",
			},
			bodyEncoding: "json", bodyClosed: true,
			bodyFields: []string{"model", "input", "instructions", "tools", "parallel_tool_calls", "reasoning", "service_tier", "prompt_cache_key", "text"},
		},
		{
			id: officialCodexEndpointAlphaSearch, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/alpha/search",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"version", "x-codex-turn-metadata", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true,
			bodyFields: []string{"id", "model", "input", "commands", "settings", "max_output_tokens"},
		},
		{
			id: officialCodexEndpointImagesGenerations, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/images/generations",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"version", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true,
			bodyFields: []string{"prompt", "background", "model", "quality", "size"},
		},
		{
			id: officialCodexEndpointImagesEdits, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/images/edits",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"version", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true,
			bodyFields: []string{"images", "prompt", "background", "model", "quality", "size"},
		},
		{
			id: officialCodexEndpointRealtimeCalls, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/codex/realtime/calls",
			query: []officialCodexQueryField{
				{Name: "intent", Value: "quicksilver", Source: officialCodexSourceConstant, Required: true},
				{Name: "architecture", Value: "avas", Source: officialCodexSourceConstant, Required: true},
			},
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"version", "openai-alpha", "x-session-id", "x-oai-attestation", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "cookie", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true, bodyFields: []string{"sdp", "session"},
		},
		{
			id: officialCodexEndpointRealtimeSideband, method: http.MethodGet, upgrade: "websocket",
			transportID: officialCodexTransportWS, host: "api.openai.com", path: "/v1/realtime",
			query: []officialCodexQueryField{
				{Name: "intent", Value: "quicksilver", Source: officialCodexSourceConstant, Required: true},
				{Name: "call_id", Source: officialCodexSourceServerResponse, Required: true},
			},
			compression:     officialCodexCompressionNone,
			clientLifecycle: officialCodexClientWebSocket, headerOrderMode: officialCodexHeaderOrderWSSwapRemove,
			headerNames:  []string{"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key", "user-agent", "originator", "x-openai-internal-codex-residency", "chatgpt-account-id", "x-openai-fedramp", "authorization", "x-session-id", "x-oai-attestation", "version", "openai-alpha"},
			bodyEncoding: "websocket_discriminated_events", bodyDiscriminator: "type", bodyFields: []string{"type"},
		},
		{
			id: officialCodexEndpointWhamUsage, method: http.MethodGet,
			transportID: officialCodexTransportHTTPLongLived, host: "chatgpt.com", path: "/backend-api/wham/usage",
			accept: "*/*", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientBackendLongLived, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"user-agent", "authorization", "chatgpt-account-id", "x-openai-fedramp", "accept", "host"},
			bodyEncoding: "none", bodyClosed: true,
		},
		{
			id: officialCodexEndpointWhamResetCredits, method: http.MethodGet,
			transportID: officialCodexTransportHTTPLongLived, host: "chatgpt.com", path: "/backend-api/wham/rate-limit-reset-credits",
			accept: "*/*", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientBackendLongLived, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"user-agent", "authorization", "chatgpt-account-id", "x-openai-fedramp", "accept", "host"},
			bodyEncoding: "none", bodyClosed: true,
		},
		{
			id: officialCodexEndpointWhamConsumeResetCredit, method: http.MethodPost,
			transportID: officialCodexTransportHTTPLongLived, host: "chatgpt.com", path: "/backend-api/wham/rate-limit-reset-credits/consume",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientBackendLongLived, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"user-agent", "authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true, bodyFields: []string{"redeem_request_id", "credit_id"},
		},
		{
			id: officialCodexEndpointOAuthRefresh, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "auth.openai.com", path: "/oauth/token",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderExplicit,
			headerNames:  []string{"content-type", "accept", "originator", "user-agent", "x-openai-internal-codex-residency", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true, bodyFields: []string{"client_id", "grant_type", "refresh_token"},
		},
		{
			id: officialCodexEndpointFilesCreate, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/files",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true, bodyFields: []string{"file_name", "file_size", "use_case"},
		},
		{
			id: officialCodexEndpointFilesBlobUpload, method: http.MethodPut,
			transportID: officialCodexTransportHTTPDefault, host: "*.oaiusercontent.com", hostFromResponse: true, path: "{server_returned_path}",
			query:           []officialCodexQueryField{{Name: "*", Source: officialCodexSourceServerResponse, Required: true}},
			accept:          "*/*",
			compression:     officialCodexCompressionNone,
			clientLifecycle: officialCodexClientReturnedUploadURL, headerOrderMode: officialCodexHeaderOrderExplicit,
			headerNames:  []string{"x-ms-blob-type", "x-ms-client-request-id", "content-length", "accept", "host"},
			bodyEncoding: "raw_bytes", bodyClosed: true,
		},
		{
			id: officialCodexEndpointFilesUploaded, method: http.MethodPost,
			transportID: officialCodexTransportHTTPDefault, host: "chatgpt.com", path: "/backend-api/files/{file_id}/uploaded",
			accept: "*/*", contentType: "application/json", compression: officialCodexCompressionNone,
			clientLifecycle: officialCodexClientPerUpperCall, headerOrderMode: officialCodexHeaderOrderH1HeaderMap,
			headerNames:  []string{"authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "host", "content-length"},
			bodyEncoding: "json", bodyClosed: true,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.id, func(t *testing.T) {
			endpoint := mustOfficialCodex0145Endpoint(t, profile, testCase.id)
			if endpoint.Method != testCase.method || endpoint.Upgrade != testCase.upgrade ||
				endpoint.TransportID != testCase.transportID || endpoint.Host != testCase.host ||
				endpoint.HostFromResponse != testCase.hostFromResponse || endpoint.Path != testCase.path ||
				!reflect.DeepEqual(endpoint.Query, testCase.query) || endpoint.Accept != testCase.accept ||
				endpoint.ContentType != testCase.contentType || endpoint.Compression != testCase.compression ||
				endpoint.ClientLifecycle != testCase.clientLifecycle || endpoint.HeaderOrderMode != testCase.headerOrderMode {
				t.Fatalf("端点基础契约不一致：%+v", endpoint)
			}
			if got := officialCodex0145HeaderNamesBySlot(endpoint.Headers); !reflect.DeepEqual(got, testCase.headerNames) {
				t.Fatalf("header 线序不一致：\n实际：%v\n期望：%v", got, testCase.headerNames)
			}
			if endpoint.Body.Encoding != testCase.bodyEncoding || endpoint.Body.Closed != testCase.bodyClosed ||
				endpoint.Body.Discriminator != testCase.bodyDiscriminator {
				t.Fatalf("body 封闭契约不一致：%+v", endpoint.Body)
			}
			if got := officialCodex0145BodyFieldNames(endpoint.Body.Fields); !reflect.DeepEqual(got, testCase.bodyFields) {
				t.Fatalf("body 字段顺序不一致：\n实际：%v\n期望：%v", got, testCase.bodyFields)
			}
			if endpoint.Upgrade == "" {
				for _, header := range endpoint.Headers {
					if header.WireName != lowercaseASCII(header.WireName) {
						t.Fatalf("普通 HTTP header 未小写：%s", header.WireName)
					}
				}
			}
		})
	}
}

func TestOfficialCodex0145ConnectionLifecycleComesFromEndpointProfile(t *testing.T) {
	account := &Account{
		ID:       94,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Extra:    map[string]any{},
	}
	resolvePoolID := func(endpointID codexEndpointID, invocationID string) string {
		t.Helper()
		endpoint, err := resolveCodexEndpoint(officialCodexVersion0145, endpointID)
		if err != nil {
			t.Fatal(err)
		}
		egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID:       account.ID,
			TargetPlatform:  PlatformOpenAI,
			InboundEndpoint: endpoint.Path,
			Transport:       OfficialEgressTransportHTTP,
			UpstreamHost:    endpoint.Host,
			ProfileVersion:  officialCodexVersion0145,
			ProfileMode:     "historical_audit",
			AccountType:     account.Type,
			CodexEndpointID: endpoint.ID,
			InvocationID:    invocationID,
		})
		profile := OfficialEgressProfile{
			Enabled:                   true,
			Version:                   officialCodexVersion0145,
			TargetPlatform:            PlatformOpenAI,
			InboundEndpoint:           endpoint.Path,
			Transport:                 OfficialEgressTransportHTTP,
			UpstreamHost:              endpoint.Host,
			CodexVersionProfileID:     "codex-cli-" + officialCodexVersion0145,
			CodexVersionProfileDigest: officialCodexHistoricalProfileDigest,
			CodexEndpointProfileID:    endpoint.ID,
			TransportProfileID:        endpoint.TransportID,
		}
		return buildOfficialEgressConnectionPoolID(egressContext, profile)
	}

	usagePool := resolvePoolID(officialCodexEndpointWhamUsage, "call-a")
	resetPool := resolvePoolID(officialCodexEndpointWhamResetCredits, "call-b")
	if usagePool != resetPool {
		t.Fatalf("WHAM backend-client 应跨端点复用：\nusage=%s\nreset=%s", usagePool, resetPool)
	}
	if !strings.Contains(usagePool, "invocation="+officialCodexClientBackendLongLived) {
		t.Fatalf("WHAM 连接池未绑定常驻生命周期：%s", usagePool)
	}

	firstResponsesPool := resolvePoolID(officialCodexEndpointResponsesHTTP, "call-a")
	secondResponsesPool := resolvePoolID(officialCodexEndpointResponsesHTTP, "call-b")
	if firstResponsesPool == secondResponsesPool {
		t.Fatal("Responses HTTP 不同上层调用不得复用 Client/TCP")
	}
}

func TestOfficialCodex0145ConditionalHeaderSlots(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}

	compact := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointResponsesCompact)
	beta := mustOfficialCodex0145Header(t, compact, "x-codex-beta-features")
	turnState := mustOfficialCodex0145Header(t, compact, "x-codex-turn-state")
	if beta.Slot != 30 || beta.Sequence != 0 || beta.Condition != officialCodexConditionBetaFeatures ||
		beta.AlternateGroup != "compact-third-slot" {
		t.Fatalf("legacy compact beta 第三槽不一致：%+v", beta)
	}
	if turnState.Slot != 30 || turnState.Sequence != 1 || turnState.Condition != officialCodexConditionTurnState ||
		turnState.AlternateGroup != "compact-third-slot" {
		t.Fatalf("legacy compact turn-state 第三槽不一致：%+v", turnState)
	}

	httpResponses := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointResponsesHTTP)
	conditionalHeaders := map[string]string{
		"x-openai-subagent":                      officialCodexConditionSubagent,
		"x-openai-memgen-request":                officialCodexConditionMemoryGeneration,
		"x-codex-parent-thread-id":               officialCodexConditionParentThread,
		"content-encoding":                       officialCodexConditionRequestCompression,
		"x-openai-internal-codex-responses-lite": officialCodexConditionResponsesLite,
		"x-openai-internal-codex-residency":      officialCodexConditionResidency,
	}
	for name, condition := range conditionalHeaders {
		header := mustOfficialCodex0145Header(t, httpResponses, name)
		if header.Condition != condition {
			t.Errorf("%s 条件不一致：实际 %s，期望 %s", name, header.Condition, condition)
		}
	}

	wsResponses := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointResponsesWS)
	runtimeMetrics := mustOfficialCodex0145Header(t, wsResponses, "x-responsesapi-include-timing-metrics")
	if runtimeMetrics.Condition != officialCodexConditionRuntimeMetrics {
		t.Fatalf("WS runtime metrics 条件不一致：%+v", runtimeMetrics)
	}
	if _, ok := findOfficialCodex0145Header(httpResponses, "x-responsesapi-include-timing-metrics"); ok {
		t.Fatal("HTTP responses 不应携带 WS 专属 runtime metrics header")
	}
	for name, condition := range map[string]string{
		"x-openai-subagent":        officialCodexConditionSubagent,
		"x-openai-memgen-request":  officialCodexConditionMemoryGeneration,
		"x-codex-parent-thread-id": officialCodexConditionParentThread,
	} {
		header := mustOfficialCodex0145Header(t, compact, name)
		if header.Condition != condition {
			t.Errorf("compact %s 条件不一致：实际 %s，期望 %s", name, header.Condition, condition)
		}
	}
	openAIBeta := mustOfficialCodex0145Header(t, wsResponses, "openai-beta")
	if openAIBeta.Value != "responses_websockets=2026-02-06" || openAIBeta.Condition != officialCodexConditionAlways {
		t.Fatalf("WS OpenAI-Beta 契约不一致：%+v", openAIBeta)
	}
	for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointImagesGenerations, officialCodexEndpointImagesEdits} {
		endpoint := mustOfficialCodex0145Endpoint(t, profile, endpointID)
		if _, ok := findOfficialCodex0145Header(endpoint, "openai-beta"); ok {
			t.Errorf("普通 HTTP 端点 %s 不应携带 openai-beta", endpointID)
		}
	}

	realtimeCalls := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointRealtimeCalls)
	realtimeAlpha := mustOfficialCodex0145Header(t, realtimeCalls, "openai-alpha")
	if realtimeAlpha.Slot != 20 || realtimeAlpha.Value != "quicksilver=v1" || realtimeAlpha.Condition != officialCodexConditionAlways {
		t.Fatalf("realtime 第一跳 openai-alpha 契约不一致：%+v", realtimeAlpha)
	}
	realtimeSideband := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointRealtimeSideband)
	expectedSidebandSlots := map[string]int{
		"user-agent": 60, "originator": 70, "chatgpt-account-id": 80, "authorization": 90,
		"x-session-id": 100, "version": 110, "openai-alpha": 120,
	}
	for name, slot := range expectedSidebandSlots {
		header := mustOfficialCodex0145Header(t, realtimeSideband, name)
		if header.Slot != slot {
			t.Errorf("realtime sideband %s 槽位不一致：实际 %d，期望 %d", name, header.Slot, slot)
		}
	}
	if header := mustOfficialCodex0145Header(t, realtimeSideband, "version"); header.Value != officialCodexVersion0145 {
		t.Fatalf("realtime sideband version 不一致：%+v", header)
	}
	if header := mustOfficialCodex0145Header(t, realtimeSideband, "openai-alpha"); header.Value != "quicksilver=v1" {
		t.Fatalf("realtime sideband openai-alpha 不一致：%+v", header)
	}

	consume := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointWhamConsumeResetCredit)
	expectedWhamPositions := map[string]int{
		"user-agent": 10, "authorization": 20, "chatgpt-account-id": 30,
		"content-type": 35, "accept": 40, "host": 50, "content-length": 60,
	}
	for name, slot := range expectedWhamPositions {
		header := mustOfficialCodex0145Header(t, consume, name)
		if header.Slot != slot {
			t.Errorf("wham consume %s 槽位不一致：实际 %d，期望 %d", name, header.Slot, slot)
		}
	}

	images := mustOfficialCodex0145Endpoint(t, profile, officialCodexEndpointImagesGenerations)
	if _, ok := findOfficialCodex0145BodyField(images.Body, "n"); ok {
		t.Error("Codex 0.145.0 images 的 n=None 不应进入可见 body 字段契约")
	}
	creditID, ok := findOfficialCodex0145BodyField(consume.Body, "credit_id")
	if !ok || creditID.Condition != officialCodexConditionCreditID || creditID.OmitWhen != "none" {
		t.Fatalf("wham credit_id 条件槽不一致：%+v", creditID)
	}
}

func TestOfficialCodex0145DeepCopyAndDigest(t *testing.T) {
	first, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	pristine, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	// 解析交付的是进程内只读单例：重复解析必须命中同一份编译结果，而不是每次
	// 重新解码 48 KB 快照并重算摘要。需要可变副本的调用方走
	// cloneOfficialCodexVersionProfile，隔离责任因此从解析路径移交给调用方。
	if first != pristine {
		t.Fatal("两次版本解析返回了不同指针，只读单例契约被破坏")
	}
	formal := mustOfficialCodexHistoricalFixture(t)
	if first.Digest != pristine.Digest || first.Digest != formal.ExecutableProfileDigest() {
		t.Fatalf("可执行画像摘要不稳定：%s / %s / %s", first.Digest, pristine.Digest, formal.ExecutableProfileDigest())
	}
	if decoded, err := hex.DecodeString(first.Digest); err != nil || len(decoded) != sha256DigestSize {
		t.Fatalf("版本画像摘要不是 64 位十六进制 SHA-256：%q，错误：%v", first.Digest, err)
	}
	const expectedDigest = "dc5ac8042a8b46312047a16c3bd4e9685a16135d8cddef5a76ba073e49e4212c"
	if first.Digest != expectedDigest {
		t.Fatalf("Codex 0.145.0 稳定摘要变化：实际 %s，期望 %s", first.Digest, expectedDigest)
	}
	recomputed, err := digestOfficialCodexVersionProfile(*first)
	if err != nil {
		t.Fatal(err)
	}
	serviceProjectionDigest := recomputed

	// 显式深拷贝必须与只读单例完全隔离：改写副本不得影响后续任何一次解析。
	mutable, err := cloneOfficialCodexVersionProfile(first)
	if err != nil {
		t.Fatal(err)
	}
	mutable.Surfaces[0].Product = "mutated"
	mutable.ToolPresentation.EndpointIDs[0] = "mutated"
	mutable.Subagents.Mappings[0].HeaderValue = "mutated"
	mutable.Transports[0].CipherSuites[0] = 0
	mutable.Transports[1].WebSocket.FixedHandshakePrefix[0] = "mutated"
	mutable.Endpoints[0].Query[0].Value = "mutated"
	mutable.Endpoints[1].Headers[0].Name = "mutated"
	mutable.Endpoints[1].Body.Fields[0].Name = "mutated"
	fresh, err := resolveCodexVersionProfile(officialCodexVersion0145)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(&mutable, fresh) {
		t.Fatal("深拷贝没有与只读单例隔离")
	}
	if !reflect.DeepEqual(pristine, fresh) {
		t.Fatal("修改深拷贝后污染了不可变规范快照")
	}
	// 单例在进程内被任何调用方改写都会体现为摘要漂移，这里是最后一道自检。
	singletonDigest, err := digestOfficialCodexVersionProfile(*fresh)
	if err != nil {
		t.Fatal(err)
	}
	if singletonDigest != serviceProjectionDigest {
		t.Fatalf("只读单例在进程内被改写：摘要 %s，期望 %s", singletonDigest, serviceProjectionDigest)
	}

	endpointFirst, err := resolveCodexEndpoint(officialCodexVersion0145, codexEndpointID(officialCodexEndpointResponsesHTTP))
	if err != nil {
		t.Fatal(err)
	}
	endpointPristine, err := resolveCodexEndpoint(officialCodexVersion0145, codexEndpointID(officialCodexEndpointResponsesHTTP))
	if err != nil {
		t.Fatal(err)
	}
	endpointFirst.Headers[0].Name = "mutated"
	endpointFirst.Body.Fields[0].Name = "mutated"
	orderedHeaders := endpointPristine.OrderedHeaders()
	orderedHeaders[0].Name = "mutated"
	endpointFresh, err := resolveCodexEndpoint(officialCodexVersion0145, codexEndpointID(officialCodexEndpointResponsesHTTP))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(endpointPristine, endpointFresh) {
		t.Fatal("调用方修改端点画像后污染了不可变规范快照")
	}
	if endpointPristine.OrderedHeaders()[0].Name == "mutated" {
		t.Fatal("调用方修改排序后 header 时污染了端点画像")
	}

	mutations := []struct {
		name   string
		mutate func(*officialCodexVersionProfile)
	}{
		{name: "入口", mutate: func(profile *officialCodexVersionProfile) { profile.Surfaces[0].Originator = "mutated" }},
		{name: "终端", mutate: func(profile *officialCodexVersionProfile) { profile.Surfaces[0].TerminalTokenPattern = "mutated" }},
		{name: "Feature", mutate: func(profile *officialCodexVersionProfile) { profile.FeatureDefaults.RequestCompressionLevel++ }},
		{name: "工具呈现", mutate: func(profile *officialCodexVersionProfile) { profile.ToolPresentation.FunctionName = "mutated" }},
		{name: "Subagent", mutate: func(profile *officialCodexVersionProfile) { profile.Subagents.Mappings[0].MetadataKind = "mutated" }},
		{name: "TLS", mutate: func(profile *officialCodexVersionProfile) { profile.Transports[0].CipherSuites[0]++ }},
		{name: "WS", mutate: func(profile *officialCodexVersionProfile) { profile.Transports[1].WebSocket.ContextTakeover = false }},
		{name: "端点", mutate: func(profile *officialCodexVersionProfile) { profile.Endpoints[0].Path = "/mutated" }},
		{name: "query", mutate: func(profile *officialCodexVersionProfile) { profile.Endpoints[0].Query[0].Value = "mutated" }},
		{name: "header", mutate: func(profile *officialCodexVersionProfile) { profile.Endpoints[1].Headers[0].Value = "mutated" }},
		{name: "body", mutate: func(profile *officialCodexVersionProfile) { profile.Endpoints[1].Body.Fields[0].Name = "mutated" }},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
			if err != nil {
				t.Fatal(err)
			}
			// 解析结果是只读单例，mutation 必须作用在深拷贝上，
			// 否则会把损坏画像泄漏给同进程内其余测试。
			mutable, err := cloneOfficialCodexVersionProfile(profile)
			if err != nil {
				t.Fatal(err)
			}
			mutation.mutate(&mutable)
			mutatedDigest, err := digestOfficialCodexVersionProfile(mutable)
			if err != nil {
				t.Fatal(err)
			}
			if mutatedDigest == serviceProjectionDigest {
				t.Fatalf("%s 变化没有进入全画像摘要", mutation.name)
			}
		})
	}
}

const sha256DigestSize = 32

func mustOfficialCodex0145Transport(t *testing.T, profile *officialCodexVersionProfile, id string) officialCodexTransportProfile {
	t.Helper()
	for _, transport := range profile.Transports {
		if transport.ID == id {
			return transport
		}
	}
	t.Fatalf("缺少传输画像：%s", id)
	return officialCodexTransportProfile{}
}

func mustOfficialCodex0145Endpoint(t *testing.T, profile *officialCodexVersionProfile, id string) officialCodexEndpointProfile {
	t.Helper()
	endpoint, err := profile.ResolveEndpoint(id)
	if err != nil {
		t.Fatalf("缺少端点画像 %s：%v", id, err)
	}
	return endpoint
}

func mustOfficialCodex0145Header(t *testing.T, endpoint officialCodexEndpointProfile, name string) officialCodexHeaderSlot {
	t.Helper()
	header, ok := findOfficialCodex0145Header(endpoint, name)
	if !ok {
		t.Fatalf("端点 %s 缺少 header：%s", endpoint.ID, name)
	}
	return header
}

func findOfficialCodex0145Header(endpoint officialCodexEndpointProfile, name string) (officialCodexHeaderSlot, bool) {
	for _, header := range endpoint.Headers {
		if header.Name == name {
			return header, true
		}
	}
	return officialCodexHeaderSlot{}, false
}

func findOfficialCodex0145BodyField(body officialCodexBodyContract, name string) (officialCodexBodyField, bool) {
	for _, field := range body.Fields {
		if field.Name == name {
			return field, true
		}
	}
	return officialCodexBodyField{}, false
}

func officialCodex0145HeaderNamesBySlot(headers []officialCodexHeaderSlot) []string {
	ordered := (officialCodexEndpointProfile{Headers: headers}).OrderedHeaders()
	names := make([]string, 0, len(ordered))
	for _, header := range ordered {
		names = append(names, header.WireName)
	}
	return names
}

func officialCodex0145BodyFieldNames(fields []officialCodexBodyField) []string {
	if len(fields) == 0 {
		return nil
	}
	names := make([]string, 0, len(fields))
	for _, field := range fields {
		names = append(names, field.Name)
	}
	return names
}

func lowercaseASCII(value string) string {
	buffer := []byte(value)
	for index, character := range buffer {
		if character >= 'A' && character <= 'Z' {
			buffer[index] = character + ('a' - 'A')
		}
	}
	return string(buffer)
}
