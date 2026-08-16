package service

import (
	"go/ast"
	"go/token"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// changeset3RuntimeAuthorityEntry 把 21 个 Runtime Sink 与其生产入口、入口内
// 必须出现的统一 Executor 桥接调用绑定。入口只准备业务语义，最终 Header、
// Body、WS 握手和帧均由下方共享权威函数收口。
type changeset3RuntimeAuthorityEntry struct {
	sink     officialegress.SinkID
	file     string
	function string
	bridge   string
}

func TestChangeset3RuntimeSinksEnterExecutorWithoutLegacyFinalizers(t *testing.T) {
	entries := []changeset3RuntimeAuthorityEntry{
		{officialegress.SinkCodexAdminTestCompact, "account_test_service.go", "testOpenAICompactConnection", "ExecuteCodexHTTP"},
		{officialegress.SinkCodexAdminTestResponses, "account_test_service.go", "testOpenAIAccountConnection", "ExecuteCodexHTTP"},
		{officialegress.SinkCodexAlphaSearchPATFallback, "openai_alpha_search.go", "forwardAlphaSearchViaResponsesWebSearch", "ExecuteCodexHTTP"},
		{officialegress.SinkCodexUsageProbe, "account_usage_service.go", "probeOpenAICodexSnapshot", "ExecuteCodexHTTP"},
		{officialegress.SinkCodexAlphaSearchDirect, "openai_alpha_search.go", "ForwardAlphaSearch", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexFilesBlobUpload, "official_egress_codex_files.go", "uploadBlob", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexFilesRegister, "official_egress_codex_files.go", "executeJSON", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexImagesOAuthTest, "account_test_service.go", "testOpenAIImageOAuth", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexImagesResponses, "openai_images_responses.go", "forwardOpenAIImagesOAuth", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexModelsList, "openai_codex_models_service.go", "fetchCodexModelsManifestUpstream", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexOAuthRefresh, "openai_oauth_service.go", "refreshTokenWithClientID", "executeOAuthRefresh"},
		{officialegress.SinkCodexQuotaWHAM, "openai_quota_service.go", "doCodexQuotaRequest", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexRealtimeCalls, "openai_live.go", "createUpstreamLiveCall", "newOfficialCodexHTTPInvocation"},
		{officialegress.SinkCodexRealtimeSideband, "openai_live.go", "dialLiveSideband", "newOfficialCodexWebSocketInvocation"},
		{officialegress.SinkCodexResponsesAnthropicCompat, "openai_gateway_messages.go", "ForwardAsAnthropic", "officialCodexResponseForwardPlan"},
		{officialegress.SinkCodexResponsesChatCompletions, "openai_gateway_chat_completions.go", "ForwardAsChatCompletions", "officialCodexResponseForwardPlan"},
		{officialegress.SinkCodexResponsesForward, "openai_gateway_forward.go", "Forward", "officialCodexResponseForwardPlanForHolder"},
		{officialegress.SinkCodexResponsesPassthrough, "openai_gateway_passthrough.go", "forwardOpenAIPassthrough", "officialCodexResponseForwardPlan"},
		{officialegress.SinkCodexResponsesWS, "openai_ws_forwarder_v2.go", "forwardOpenAIWSV2", "officialCodexResponseForwardPlanForHolder"},
		{officialegress.SinkCodexResponsesWSHTTPBridge, "openai_ws_http_bridge.go", "proxyOpenAIWSHTTPBridgeTurn", "officialCodexResponseForwardPlan"},
		{officialegress.SinkCodexResponsesWSV2Passthrough, "openai_ws_v2_passthrough_adapter.go", "proxyResponsesWebSocketV2Passthrough", "newOfficialCodexWebSocketInvocation"},
	}
	if len(entries) != 21 {
		t.Fatalf("Runtime Sink AST 清单数量错误：%d", len(entries))
	}

	fset := token.NewFileSet()
	files := parseOfficialEgressPackage(t, fset)
	seen := make(map[officialegress.SinkID]bool, len(entries))
	for _, entry := range entries {
		if seen[entry.sink] {
			t.Fatalf("Runtime Sink AST 清单重复：%s", entry.sink)
		}
		seen[entry.sink] = true
		binding, ok := officialegress.DefaultSinkCatalog().Resolve(entry.sink)
		if !ok || !binding.RuntimeBindable() ||
			binding.EnforcementState() != officialegress.SinkStateEnforced {
			t.Fatalf("Runtime Sink 未保持 enforced/runtime-bindable：%s", entry.sink)
		}
		fn := changeset3FindProductionFunction(t, files, entry.file, entry.function)
		calls := changeset3FunctionCalls(fn)
		if !calls[entry.bridge] {
			t.Fatalf("%s 入口 %s.%s 未进入 %s", entry.sink, entry.file, entry.function, entry.bridge)
		}
	}

	// 四条共享权威链必须同时包含“结构化语义转换”和 Executor attempt。
	for _, chain := range []struct {
		file     string
		function string
		required []string
	}{
		{"official_egress_1b_executor.go", "ExecuteCodexHTTP", []string{"prepareOfficialCodexSemanticAttempt", "BeginInvocation", "ExecuteAttempt"}},
		{"official_egress_http_invocation.go", "Execute", []string{"prepareOfficialCodexSemanticAttempt", "ExecuteAttempt"}},
		{"official_egress_websocket_invocation.go", "executeAcquire", []string{"prepareOfficialCodexSemanticAttempt", "ExecuteAttempt"}},
		{"openai_forward_plan.go", "ExecuteAttempt", []string{"prepareOfficialCodexSemanticAttempt", "ExecuteAttempt"}},
		{"openai_oauth_service.go", "executeOAuthRefresh", []string{"NewAttemptAuthentication", "ExecuteAttempt"}},
	} {
		fn := changeset3FindProductionFunction(t, files, chain.file, chain.function)
		calls := changeset3FunctionCalls(fn)
		for _, required := range chain.required {
			if !calls[required] {
				t.Fatalf("统一权威链 %s.%s 缺少 %s", chain.file, chain.function, required)
			}
		}
	}
}

func TestChangeset3RuntimeSinksHaveNoPreExecutorBodyFinalization(t *testing.T) {
	fset := token.NewFileSet()
	files := parseOfficialEgressPackage(t, fset)
	forbidden := map[string]bool{
		"officialCodex0145FinalizeEndpointJSONBody": true,
		"resolveOfficialOpenAIRequestCompression":   true,
		"compressOfficialOpenAIHTTPRequest":         true,
		"finalizeOpenAIOfficialEgressHTTPRequest":   true,
		"finalizeOpenAIOfficialEgressWSFrame":       true,
	}

	for fileName, file := range files {
		for _, declaration := range file.Decls {
			function, ok := declaration.(*ast.FuncDecl)
			if !ok || function.Body == nil {
				continue
			}
			for callee := range changeset3FunctionCalls(function) {
				if forbidden[callee] {
					t.Fatalf("生产函数 %s.%s 仍在 Executor 前调用旧 Body 定型：%s", fileName, function.Name.Name, callee)
				}
			}
		}
	}
}

func changeset3FindProductionFunction(
	t *testing.T,
	files map[string]*ast.File,
	fileName string,
	function string,
) *ast.FuncDecl {
	t.Helper()
	file := files[fileName]
	if file == nil {
		t.Fatalf("缺少生产文件：%s", fileName)
		return nil
	}
	for _, decl := range file.Decls {
		if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == function {
			return fn
		}
	}
	t.Fatalf("缺少生产函数：%s.%s", fileName, function)
	return nil
}

func changeset3FunctionCalls(fn *ast.FuncDecl) map[string]bool {
	calls := make(map[string]bool)
	ast.Inspect(fn.Body, func(node ast.Node) bool {
		if call, ok := node.(*ast.CallExpr); ok {
			calls[calleeName(call)] = true
		}
		return true
	})
	return calls
}
