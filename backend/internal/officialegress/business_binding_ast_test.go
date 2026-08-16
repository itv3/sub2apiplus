package officialegress

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"testing"
)

type businessBindingExpectation struct {
	file           string
	function       string
	sinkIdentifier string
}

// TestAllBusinessSinkIDsAreBoundAtCallsitesAndDoNotCreateRawClients 把 27 个运行时
// SinkID 与真实业务函数逐项锁定。共享 facade/发送栈不得补 ID；in-scope 调用点也不得
// 新建 http.Client 或使用 http.DefaultClient 绕过四类 terminal Guard。
func TestAllBusinessSinkIDsAreBoundAtCallsitesAndDoNotCreateRawClients(t *testing.T) {
	expectations := []businessBindingExpectation{
		{"../service/account_test_service.go", "testOpenAICompactConnection", "officialEgressSinkAdminTestCompact"},
		{"../service/account_test_service.go", "testOpenAIAccountConnection", "officialEgressSinkAdminTestResponses"},
		{"../service/openai_alpha_search.go", "ForwardAlphaSearch", "officialEgressSinkAlphaSearchDirect"},
		{"../service/openai_alpha_search.go", "forwardAlphaSearchViaResponsesWebSearch", "officialEgressSinkAlphaSearchPATFallback"},
		{"../service/official_egress_codex_files.go", "uploadBlob", "officialEgressSinkFilesBlobUpload"},
		{"../service/official_egress_codex_files.go", "executeJSON", "officialEgressSinkFilesRegister"},
		{"../service/account_test_service.go", "testOpenAIImageOAuth", "officialEgressSinkImagesOAuthTest"},
		{"../service/openai_images_responses.go", "forwardOpenAIImagesOAuth", "officialEgressSinkImagesResponses"},
		{"../service/openai_codex_models_service.go", "fetchCodexModelsManifestUpstream", "officialEgressSinkModelsList"},
		{"../repository/openai_oauth_service.go", "ExchangeCode", "SinkCodexOAuthExchange"},
		{"../service/openai_oauth_service.go", "executeOAuthRefresh", "SinkCodexOAuthRefresh"},
		{"../service/openai_quota_service.go", "doCodexQuotaRequest", "officialEgressSinkQuotaWHAM"},
		{"../service/openai_live.go", "createUpstreamLiveCall", "officialEgressSinkRealtimeCalls"},
		{"../service/openai_live.go", "dialLiveSideband", "officialEgressSinkRealtimeSideband"},
		{"../service/openai_gateway_messages.go", "ForwardAsAnthropic", "officialEgressSinkResponsesAnthropicCompat"},
		{"../service/openai_gateway_chat_completions.go", "ForwardAsChatCompletions", "officialEgressSinkResponsesChatCompletions"},
		{"../service/openai_gateway_forward.go", "Forward", "officialEgressSinkResponsesForward"},
		{"../service/openai_gateway_passthrough.go", "forwardOpenAIPassthrough", "officialEgressSinkResponsesPassthrough"},
		{"../service/openai_ws_forwarder_v2.go", "forwardOpenAIWSV2", "officialEgressSinkResponsesWS"},
		{"../service/openai_ws_forwarder_ingress.go", "ProxyResponsesWebSocketFromClient", "officialEgressSinkResponsesWS"},
		{"../service/openai_ws_http_bridge.go", "proxyOpenAIWSHTTPBridgeTurn", "officialEgressSinkResponsesWSHTTPBridge"},
		{"../service/openai_ws_v2_passthrough_adapter.go", "proxyResponsesWebSocketV2Passthrough", "officialEgressSinkResponsesWSV2Passthrough"},
		{"../service/account_usage_service.go", "probeOpenAICodexSnapshot", "officialEgressSinkUsageProbe"},
		{"../service/openai_agent_identity.go", "registerAgentIdentityTask", "officialEgressSinkAgentTaskRegister"},
		{"../service/openai_codex_pat_service.go", "ValidateCodexPersonalAccessToken", "officialEgressSinkPATWhoAmI"},
		{"../service/openai_privacy_service.go", "fetchChatGPTAccountInfo", "officialEgressSinkPrivacyAccountInfo"},
		{"../service/openai_privacy_service.go", "disableOpenAITraining", "officialEgressSinkPrivacyDisableTraining"},
		{"../service/openai_privacy_service.go", "fetchChatGPTSubscriptionExpiresAt", "officialEgressSinkPrivacySubscription"},
	}
	uniqueSinkIdentifiers := make(map[string]struct{})
	for _, expectation := range expectations {
		uniqueSinkIdentifiers[expectation.sinkIdentifier] = struct{}{}
		assertBusinessBindingFunction(t, expectation)
	}
	if len(uniqueSinkIdentifiers) != 27 {
		t.Fatalf("运行时业务 SinkID 静态闭集=%d，期望 27", len(uniqueSinkIdentifiers))
	}
}

// TestAPIKeyOnlyAdminFunctionsDoNotBindCodexSinkID 防止通用 API-Key/custom base URL
// 再通过非空 SinkID 被误判为 OAuth Codex 管辖流量。
func TestAPIKeyOnlyAdminFunctionsDoNotBindCodexSinkID(t *testing.T) {
	for _, expectation := range []businessBindingExpectation{
		{"../service/account_test_service.go", "testOpenAIChatCompletionsConnection", "officialEgressSinkAdminTestChatCompletions"},
		{"../service/account_test_service_keeper.go", "ProxyKeeperOpenAIAccount", "officialEgressSinkAdminTestKeeper"},
	} {
		function := parseBusinessFunction(t, expectation.file, expectation.function)
		ast.Inspect(function.Body, func(node ast.Node) bool {
			switch current := node.(type) {
			case *ast.CallExpr:
				if identifier, ok := current.Fun.(*ast.Ident); ok && identifier.Name == "bindOfficialEgressSink" {
					t.Errorf("%s.%s 的 API-Key-only 路径禁止绑定官方 SinkID", expectation.file, expectation.function)
				}
			case *ast.Ident:
				if current.Name == expectation.sinkIdentifier {
					t.Errorf("%s.%s 仍引用历史 OAuth SinkID %s", expectation.file, expectation.function, expectation.sinkIdentifier)
				}
			}
			return true
		})
	}
}

// TestMixedAdminFunctionsBindOnlyInsideOAuthBranch 锁定 Responses/Compact 的混合账号
// 函数：绑定调用必须位于 `if isOAuth` 分支内，不能再次移到公共发送路径。
func TestMixedAdminFunctionsBindOnlyInsideOAuthBranch(t *testing.T) {
	for _, expectation := range []businessBindingExpectation{
		{"../service/account_test_service.go", "testOpenAIAccountConnection", "officialEgressSinkAdminTestResponses"},
		{"../service/account_test_service.go", "testOpenAICompactConnection", "officialEgressSinkAdminTestCompact"},
	} {
		function := parseBusinessFunction(t, expectation.file, expectation.function)
		var oauthBodies []*ast.BlockStmt
		ast.Inspect(function.Body, func(node ast.Node) bool {
			if statement, ok := node.(*ast.IfStmt); ok {
				if identifier, ok := statement.Cond.(*ast.Ident); ok && identifier.Name == "isOAuth" {
					oauthBodies = append(oauthBodies, statement.Body)
				}
			}
			return true
		})
		bindingCalls := 0
		ast.Inspect(function.Body, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok {
				return true
			}
			identifier, ok := call.Fun.(*ast.Ident)
			if !ok || identifier.Name != "bindOfficialEgressSink" {
				return true
			}
			bindingCalls++
			insideOAuth := false
			for _, body := range oauthBodies {
				insideOAuth = insideOAuth || (body.Pos() <= call.Pos() && call.End() <= body.End())
			}
			if !insideOAuth {
				t.Errorf("%s.%s 的 binding 不在 if isOAuth 内", expectation.file, expectation.function)
			}
			return true
		})
		if bindingCalls != 1 {
			t.Errorf("%s.%s 的 OAuth binding 数量=%d，期望 1", expectation.file, expectation.function, bindingCalls)
		}
	}
}

func assertBusinessBindingFunction(t *testing.T, expectation businessBindingExpectation) {
	t.Helper()
	path := filepath.Clean(expectation.file)
	target := parseBusinessFunction(t, expectation.file, expectation.function)
	foundSink := false
	foundBindingCall := false
	ast.Inspect(target.Body, func(node ast.Node) bool {
		switch current := node.(type) {
		case *ast.CallExpr:
			switch function := current.Fun.(type) {
			case *ast.Ident:
				foundBindingCall = foundBindingCall || function.Name == "bindOfficialEgressSink"
			case *ast.SelectorExpr:
				foundBindingCall = foundBindingCall || function.Sel.Name == "StartDefaultSinkAttempt" ||
					function.Sel.Name == "BeginInvocation"
			}
		case *ast.Ident:
			if current.Name == expectation.sinkIdentifier {
				foundSink = true
			}
		case *ast.SelectorExpr:
			if current.Sel.Name == expectation.sinkIdentifier {
				foundSink = true
			}
			if identifier, ok := current.X.(*ast.Ident); ok &&
				identifier.Name == "http" && current.Sel.Name == "DefaultClient" {
				t.Errorf("%s.%s 禁止使用 http.DefaultClient", path, expectation.function)
			}
		case *ast.CompositeLit:
			selector, ok := current.Type.(*ast.SelectorExpr)
			if !ok {
				break
			}
			identifier, ok := selector.X.(*ast.Ident)
			if ok && identifier.Name == "http" && selector.Sel.Name == "Client" {
				t.Errorf("%s.%s 禁止构造 http.Client", path, expectation.function)
			}
		}
		return true
	})
	if !foundSink {
		t.Errorf("%s.%s 未绑定 %s", path, expectation.function, expectation.sinkIdentifier)
	}
	if !foundBindingCall {
		t.Errorf("%s.%s 没有调用受控业务 binding API", path, expectation.function)
	}
}

func parseBusinessFunction(t *testing.T, filePath, functionName string) *ast.FuncDecl {
	t.Helper()
	fileSet := token.NewFileSet()
	path := filepath.Clean(filePath)
	file, err := parser.ParseFile(fileSet, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s：%v", path, err)
	}
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if ok && function.Name.Name == functionName {
			return function
		}
	}
	t.Fatalf("%s 未找到函数 %s", path, functionName)
	return nil
}
