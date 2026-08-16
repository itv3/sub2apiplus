package officialegress

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// ============================================================================
// Compiler 静态 URL 封闭测试。
//
// 静态 helper 直接单测负责证明新增规则本身；Compiler/Executor 测试负责证明整体
// 控制流与签发边界；摘要与 Guard 测试负责证明 ForceQuery 终态绑定。
// ============================================================================

// codexProfileSinkBindings 返回全部走 codex_profile 证据、可运行时绑定且已 enforce
// 的 Codex sink，与 changeset3/changeset6 final-wire 捕获的筛选口径一致。
func codexProfileSinkBindings(t *testing.T) []SinkBinding {
	t.Helper()
	var bindings []SinkBinding
	for _, binding := range DefaultSinkCatalog().Bindings() {
		if binding.Persona() != PersonaCodexCLI ||
			binding.EndpointEvidence() != EndpointEvidenceCodexProfile ||
			!binding.RuntimeBindable() ||
			binding.EnforcementState() != SinkStateEnforced {
			continue
		}
		bindings = append(bindings, binding)
	}
	if len(bindings) == 0 {
		t.Fatal("缺少 codex_profile sink binding")
	}
	return bindings
}

func staticClosureBundle(t *testing.T, mode ReleaseMode, sinkID SinkID) ReleaseBundle {
	t.Helper()
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	request := changeset2BundleRequest(sinkID)
	request.Mode = mode
	bundle, err := resolver.Resolve(request)
	if err != nil {
		t.Fatalf("解析 %s/%s Bundle：%v", mode, sinkID, err)
	}
	return bundle
}

// staticClosurePlanForEndpoint 在真实 catalog 中找到承载指定静态 endpoint 的
// Bundle 与 plan。
func staticClosurePlanForEndpoint(
	t *testing.T,
	mode ReleaseMode,
	endpointID string,
) (ReleaseBundle, ResolvedEndpointPlan) {
	t.Helper()
	for _, binding := range codexProfileSinkBindings(t) {
		bundle := staticClosureBundle(t, mode, binding.ID())
		for _, plan := range bundle.EndpointPlans() {
			if plan.EndpointID() == endpointID && !plan.DynamicTarget() {
				return bundle, plan
			}
		}
	}
	t.Fatalf("catalog 中找不到静态 endpoint plan：%s", endpointID)
	return ReleaseBundle{}, ResolvedEndpointPlan{}
}

// staticClosureLegalTarget 按当前画像构造合法静态 URL：scheme 由 route.Protocol
// 决定，query 按画像声明顺序与 url.QueryEscape 生成，与生产
// buildOfficialCodexEndpointURL 的唯一生成形态一致。
func staticClosureLegalTarget(template EndpointPlanTemplate) *url.URL {
	scheme := "https"
	if template.route.Protocol == WireProtocolWebSocket {
		scheme = "wss"
	}
	path := strings.ReplaceAll(template.endpoint.Path, "{file_id}", "synthetic-file-id")
	target := &url.URL{Scheme: scheme, Host: template.endpoint.Host, Path: path}
	pairs := make([]string, 0, len(template.endpoint.Query))
	for _, field := range template.endpoint.Query {
		value := staticClosureQueryValue(field)
		pairs = append(pairs, url.QueryEscape(field.Name)+"="+url.QueryEscape(value))
	}
	target.RawQuery = strings.Join(pairs, "&")
	return target
}

func staticClosureQueryValue(field profilecontract.QueryFieldProfile) string {
	if field.Value != "" {
		return field.Value
	}
	return "synthetic-" + strings.ReplaceAll(field.Name, "_", "-")
}

// staticClosureDynamicInputs 为画像 server_response query 生成与
// staticClosureLegalTarget 一致的可信输入，模拟生产链（如 dialLiveSideband 传
// record.CallID）提交的受信服务器响应事实。
func staticClosureDynamicInputs(template EndpointPlanTemplate) EndpointDynamicInputs {
	dynamic := EndpointDynamicInputs{}
	for _, field := range template.endpoint.Query {
		if field.Source != profilecontract.SourceServerResponse {
			continue
		}
		if dynamic.ServerResponseQuery == nil {
			dynamic.ServerResponseQuery = map[string]string{}
		}
		dynamic.ServerResponseQuery[field.Name] = staticClosureQueryValue(field)
	}
	return dynamic
}

// staticClosureSemanticBody 生成能通过画像 Body 契约的语义 body，逻辑与
// changeset3 production final-wire 捕获使用的语义 body 构造一致。
func staticClosureSemanticBody(
	t *testing.T,
	endpoint profilecontract.ExecutableEndpointProfile,
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
				values.Set(field.Name, fmt.Sprint(staticClosureFieldValue(endpoint.ID, field.Name)))
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
		value := staticClosureFieldValue(endpoint.ID, field.Name)
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

func staticClosureFieldValue(endpointID string, name string) any {
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

// staticClosureEgressPlan 为指定静态 plan 构造完整合法 CodexEgressPlan。
func staticClosureEgressPlan(
	t *testing.T,
	bundle ReleaseBundle,
	plan ResolvedEndpointPlan,
	target *url.URL,
	invocationID string,
) CodexEgressPlan {
	t.Helper()
	authenticationInput := AttemptAuthenticationInput{BearerToken: "static-closure-token"}
	if plan.EndpointID() == "oauth_refresh" {
		authenticationInput.RefreshToken = "synthetic-refresh-token"
	}
	authentication, err := NewAttemptAuthentication(authenticationInput)
	if err != nil {
		t.Fatal(err)
	}
	return CodexEgressPlan{
		SinkID:  plan.SinkID(),
		Purpose: plan.Purpose(), EndpointID: plan.EndpointID(),
		InvocationID: invocationID,
		Mode:         bundle.Mode(), Protocol: plan.Protocol(),
		Method: plan.template.route.Key.Method, URL: target,
		Headers: make(http.Header), IdentityMode: IdentityCodexOAuthStrict,
		IdentityFacts:  executorInvocationIdentityFacts(t),
		Authentication: authentication,
		HeaderPolicy:   HeaderPolicy{ID: "static-closure-headers", Source: "test"},
		BodyPolicy:     BodyPolicy{ID: "static-closure-body", Source: "test"},
		BehaviorPolicy: bundle.Behavior(),
		Body: NewReplayableRequestBody(
			staticClosureSemanticBody(t, plan.template.endpoint),
		),
		DeclaredPersona: PersonaCodexCLI,
	}
}

// ----------------------------------------------------------------------------
// 静态 helper：结构负例
// ----------------------------------------------------------------------------

func TestValidateStaticCompilerTargetRejectsStructuralViolations(t *testing.T) {
	_, httpPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	_, wsPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_ws")
	_, filesPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "files_uploaded")

	mutate := func(template EndpointPlanTemplate, change func(*url.URL)) *url.URL {
		target := staticClosureLegalTarget(template)
		change(target)
		return target
	}
	cases := []struct {
		name     string
		template EndpointPlanTemplate
		target   *url.URL
	}{
		{"opaque 形态", httpPlan.template, &url.URL{
			Scheme: "https", Opaque: "chatgpt.com/backend-api/codex/responses",
		}},
		{"userinfo", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.User = url.User("intruder")
		})},
		{"fragment", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Fragment = "frag"
		})},
		{"raw fragment", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Fragment = "f g"
			u.RawFragment = "f%20g"
		})},
		{"ForceQuery 空 query 标记", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.ForceQuery = true
		})},
		{"HTTP 端点 http scheme", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Scheme = "http"
		})},
		{"HTTP 端点大写 scheme", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Scheme = "HTTPS"
		})},
		{"HTTP 端点 wss scheme", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Scheme = "wss"
		})},
		{"WS 端点 https scheme", wsPlan.template, mutate(wsPlan.template, func(u *url.URL) {
			u.Scheme = "https"
		})},
		{"WS 端点 ws scheme", wsPlan.template, mutate(wsPlan.template, func(u *url.URL) {
			u.Scheme = "ws"
		})},
		{"WS 端点 http scheme", wsPlan.template, mutate(wsPlan.template, func(u *url.URL) {
			u.Scheme = "http"
		})},
		{"WS 端点大写 scheme", wsPlan.template, mutate(wsPlan.template, func(u *url.URL) {
			u.Scheme = "WSS"
		})},
		{"默认端口 443", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Host = "chatgpt.com:443"
		})},
		{"非默认端口 8443", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Host = "chatgpt.com:8443"
		})},
		{"host 大小写", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Host = "ChatGPT.com"
		})},
		{"host 尾点", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Host = "chatgpt.com."
		})},
		{"host 改写", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Host = "example.com"
		})},
		{"path 增段", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Path = u.Path + "/extra"
		})},
		{"path 减段", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Path = "/backend-api/codex"
		})},
		{"path 字面改写", httpPlan.template, mutate(httpPlan.template, func(u *url.URL) {
			u.Path = "/backend-api/codex/responsesx"
		})},
		{"path 参数段为空", filesPlan.template, mutate(filesPlan.template, func(u *url.URL) {
			u.Path = "/backend-api/files//uploaded"
			u.RawPath = ""
		})},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := validateStaticCompilerTarget(tc.template, tc.target, nil); err == nil {
				t.Fatalf("非法静态 target 未被拒绝：%s", tc.target)
			}
		})
	}
}

// ----------------------------------------------------------------------------
// 静态 helper：query 负例（含画像定义负例）
// ----------------------------------------------------------------------------

func TestValidateStaticCompilerTargetRejectsQueryViolations(t *testing.T) {
	_, httpPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	_, modelsPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "models")
	_, sidebandPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "realtime_sideband")

	withQuery := func(template EndpointPlanTemplate, rawQuery string) *url.URL {
		target := staticClosureLegalTarget(template)
		target.RawQuery = rawQuery
		return target
	}
	sidebandTrusted := staticClosureDynamicInputs(sidebandPlan.template).ServerResponseQuery
	modelsQuery := staticClosureLegalTarget(modelsPlan.template).RawQuery
	callerCases := []struct {
		name     string
		template EndpointPlanTemplate
		target   *url.URL
		trusted  map[string]string
	}{
		{"画像空 query 配 RawQuery", httpPlan.template,
			withQuery(httpPlan.template, "x=1"), nil},
		{"画像外键", modelsPlan.template,
			withQuery(modelsPlan.template, modelsQuery+"&extra=1"), nil},
		{"constant 值改写", modelsPlan.template,
			withQuery(modelsPlan.template, "client_version=9.9.9"), nil},
		{"required 缺失", modelsPlan.template,
			withQuery(modelsPlan.template, ""), nil},
		{"required 部分缺失", sidebandPlan.template,
			withQuery(sidebandPlan.template, "intent=quicksilver"), sidebandTrusted},
		{"server_response 值为空", sidebandPlan.template,
			withQuery(sidebandPlan.template, "intent=quicksilver&call_id="), sidebandTrusted},
		{"server_response 非空但与可信输入不一致", sidebandPlan.template,
			withQuery(sidebandPlan.template, "intent=quicksilver&call_id=forged-call-id"),
			sidebandTrusted},
		{"server_response 缺少可信输入", sidebandPlan.template,
			withQuery(sidebandPlan.template, "intent=quicksilver&call_id=synthetic-call-id"),
			nil},
		{"可信输入含画像外键", modelsPlan.template,
			withQuery(modelsPlan.template, modelsQuery),
			map[string]string{"call_id": "synthetic-call-id"}},
		{"重复键", modelsPlan.template,
			withQuery(modelsPlan.template, modelsQuery+"&"+modelsQuery), nil},
		{"解析失败", modelsPlan.template,
			withQuery(modelsPlan.template, "client_version=%zz"), nil},
		{"分号分隔符", modelsPlan.template,
			withQuery(modelsPlan.template, modelsQuery+";a=b"), nil},
		{"前导 &", modelsPlan.template,
			withQuery(modelsPlan.template, "&"+modelsQuery), nil},
		{"尾随 &", modelsPlan.template,
			withQuery(modelsPlan.template, modelsQuery+"&"), nil},
		{"连续 &&", sidebandPlan.template,
			withQuery(sidebandPlan.template, "intent=quicksilver&&call_id=synthetic"),
			sidebandTrusted},
	}
	for _, tc := range callerCases {
		t.Run("调用方/"+tc.name, func(t *testing.T) {
			if err := validateStaticCompilerTarget(tc.template, tc.target, tc.trusted); err == nil {
				t.Fatalf("非法静态 query 未被拒绝：%q", tc.target.RawQuery)
			}
		})
	}

	// 画像定义自身违反静态闭包约束时必须 fail-close，即使调用方 URL 与其一致。
	profileCases := []struct {
		name   string
		fields []profilecontract.QueryFieldProfile
		raw    string
	}{
		{"静态画像声明 *", []profilecontract.QueryFieldProfile{{
			Name: "*", Source: profilecontract.SourceServerResponse, Required: true,
		}}, "anything=1"},
		{"画像名称为空", []profilecontract.QueryFieldProfile{{
			Name: "", Source: profilecontract.SourceConstant, Value: "v",
		}}, ""},
		{"画像名称重复", []profilecontract.QueryFieldProfile{
			{Name: "dup", Source: profilecontract.SourceConstant, Value: "v", Required: true},
			{Name: "dup", Source: profilecontract.SourceConstant, Value: "v", Required: true},
		}, "dup=v"},
		{"query source 无执行语义", []profilecontract.QueryFieldProfile{{
			Name: "probe", Source: profilecontract.SourceAccount, Required: true,
		}}, "probe=x"},
		{"constant required 画像值为空", []profilecontract.QueryFieldProfile{{
			Name: "empty", Source: profilecontract.SourceConstant, Value: "", Required: true,
		}}, "empty="},
	}
	for _, tc := range profileCases {
		t.Run("画像/"+tc.name, func(t *testing.T) {
			template := modelsPlan.template
			template.endpoint.Query = tc.fields
			target := staticClosureLegalTarget(modelsPlan.template)
			target.RawQuery = tc.raw
			if err := validateStaticCompilerTarget(template, target, nil); err == nil {
				t.Fatalf("非法画像 query 定义未 fail-close：%+v", tc.fields)
			}
		})
	}
}

// ----------------------------------------------------------------------------
// 可信动态事实所有权：clone 必须形成完整快照
// ----------------------------------------------------------------------------

func TestEndpointDynamicInputsCloneDetachesServerResponseQuery(t *testing.T) {
	original := EndpointDynamicInputs{
		ServerResponseQuery: map[string]string{"call_id": "call-before-mutation"},
	}
	cloned := original.clone()
	original.ServerResponseQuery["call_id"] = "mutated-after-clone"
	original.ServerResponseQuery["injected"] = "extra"
	if cloned.ServerResponseQuery["call_id"] != "call-before-mutation" ||
		len(cloned.ServerResponseQuery) != 1 {
		t.Fatalf("clone 与原 ServerResponseQuery 共享可变 map：%v", cloned.ServerResponseQuery)
	}
	// validateCompilerTarget 在入口统一冻结克隆：编译期间修改调用方 map 不得
	// 影响判定所依据的可信值。
	_, sidebandPlan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "realtime_sideband")
	target := staticClosureLegalTarget(sidebandPlan.template)
	dynamic := staticClosureDynamicInputs(sidebandPlan.template)
	if _, _, err := validateCompilerTarget(sidebandPlan, target, dynamic); err != nil {
		t.Fatalf("合法 sideband target 意外被拒：%v", err)
	}
	dynamic.ServerResponseQuery["call_id"] = "forged-after-validate"
	if _, _, err := validateCompilerTarget(sidebandPlan, target, dynamic); err == nil {
		t.Fatal("改写后的可信 map 未被重新判定拒绝")
	}
}

// ----------------------------------------------------------------------------
// 动态 ReturnedURL 端点：ServerResponseQuery 可信通道互斥
// ----------------------------------------------------------------------------

func TestValidateCompilerTargetRejectsServerResponseQueryForDynamicEndpoint(t *testing.T) {
	var dynamicPlan ResolvedEndpointPlan
	found := false
	for _, binding := range codexProfileSinkBindings(t) {
		bundle := staticClosureBundle(t, ReleaseModeActive, binding.ID())
		for _, plan := range bundle.EndpointPlans() {
			if plan.DynamicTarget() {
				dynamicPlan = plan
				found = true
				break
			}
		}
		if found {
			break
		}
	}
	if !found {
		t.Fatal("catalog 中缺少 ReturnedURL 动态 endpoint plan")
	}
	returned, err := url.Parse(
		"https://upload.oaiusercontent.com/synthetic/upload/blob?synthetic_signature=synthetic",
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := validateCompilerTarget(dynamicPlan, returned, EndpointDynamicInputs{
		ReturnedURL:         returned,
		ServerResponseQuery: map[string]string{"call_id": "synthetic-call-id"},
	}); err == nil {
		t.Fatal("动态 endpoint 提交 ServerResponseQuery 未被拒绝")
	}
	// 对照：既有 ReturnedURL 验证模型行为不变。
	_, validated, err := validateCompilerTarget(dynamicPlan, returned, EndpointDynamicInputs{
		ReturnedURL: returned,
	})
	if err != nil || validated == nil {
		t.Fatalf("合法 ReturnedURL 行为被误伤：validated=%v err=%v", validated, err)
	}
}

// ----------------------------------------------------------------------------
// Compiler：Active/Previous 全部静态端点正例
// ----------------------------------------------------------------------------

func TestCompilerAcceptsProfileShapedStaticTargetsAcrossModes(t *testing.T) {
	compiler := NewCompiler()
	expectedEndpointIDs := map[string]bool{}
	validatedEndpointIDs := map[string]bool{}
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if !endpoint.HostFromResponse {
				expectedEndpointIDs[endpoint.ID] = true
			}
		}
	}
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		for _, binding := range codexProfileSinkBindings(t) {
			bundle := staticClosureBundle(t, mode, binding.ID())
			for _, plan := range bundle.EndpointPlans() {
				if plan.DynamicTarget() {
					continue
				}
				validatedEndpointIDs[plan.EndpointID()] = true
				target := staticClosureLegalTarget(plan.template)
				invocationID := fmt.Sprintf(
					"chg01-%s-%s-%s", mode, plan.SinkID(), plan.EndpointID(),
				)
				egressPlan := staticClosureEgressPlan(t, bundle, plan, target, invocationID)
				execution, err := compiler.Compile(
					context.Background(), bundle, egressPlan,
					staticClosureDynamicInputs(plan.template),
				)
				if err != nil {
					t.Fatalf("合法静态 target 编译失败 %s/%s/%s：%v",
						mode, plan.SinkID(), plan.EndpointID(), err)
				}
				compiled := execution.request.URL()
				if compiled.Scheme != target.Scheme || compiled.Host != target.Host ||
					compiled.EscapedPath() != target.EscapedPath() ||
					compiled.RawQuery != target.RawQuery ||
					compiled.ForceQuery != target.ForceQuery {
					t.Fatalf("编译产物 URL 与合法输入不一致 %s/%s：%s vs %s",
						mode, plan.EndpointID(), compiled, target)
				}
			}
		}
	}
	validated := sortedStringSet(validatedEndpointIDs)
	expected := sortedStringSet(expectedEndpointIDs)
	if !slices.Equal(validated, expected) {
		t.Fatalf(
			"已验证 endpoint ID 集合不等于 Active/Previous ReleaseCatalog 并集：validated=%v expected=%v",
			validated, expected,
		)
	}
}

func sortedStringSet(values map[string]bool) []string {
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

// ----------------------------------------------------------------------------
// Compiler：query 合法等价表示
// ----------------------------------------------------------------------------

func TestCompilerPreservesLegalQueryEquivalentRepresentations(t *testing.T) {
	cases := []struct {
		name       string
		endpointID string
		rewrite    func(string) string
	}{
		{"键顺序不同", "realtime_sideband", func(string) string {
			return "call_id=synthetic-call-id&intent=quicksilver"
		}},
		{"合法转义不同", "models", func(raw string) string {
			return strings.ReplaceAll(raw, ".", "%2E")
		}},
	}
	compiler := NewCompiler()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bundle, plan := staticClosurePlanForEndpoint(t, ReleaseModeActive, tc.endpointID)
			target := staticClosureLegalTarget(plan.template)
			target.RawQuery = tc.rewrite(target.RawQuery)
			egressPlan := staticClosureEgressPlan(
				t, bundle, plan, target, "chg01-query-equivalent-"+tc.endpointID,
			)
			execution, err := compiler.Compile(
				context.Background(), bundle, egressPlan,
				staticClosureDynamicInputs(plan.template),
			)
			if err != nil {
				t.Fatalf("结构语义合法的等价 query 被拒绝：%v", err)
			}
			if execution.request.URL().RawQuery != target.RawQuery {
				t.Fatalf("调用方合法 RawQuery 原表示未保留：%q vs %q",
					execution.request.URL().RawQuery, target.RawQuery)
			}
		})
	}
}

// ----------------------------------------------------------------------------
// Compiler：files_uploaded {file_id} escaped 参数段
// ----------------------------------------------------------------------------

func TestCompilerAcceptsEscapedFileIDPathSegments(t *testing.T) {
	cases := []struct {
		name   string
		fileID string
	}{
		{"包含斜杠", "dir/nested-file"},
		{"包含空格", "file id with space"},
		{"包含百分号", "file-100%"},
	}
	compiler := NewCompiler()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bundle, plan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "files_uploaded")
			escaped := url.PathEscape(tc.fileID)
			target := staticClosureLegalTarget(plan.template)
			target.Path = "/backend-api/files/" + tc.fileID + "/uploaded"
			target.RawPath = "/backend-api/files/" + escaped + "/uploaded"
			if target.EscapedPath() != "/backend-api/files/"+escaped+"/uploaded" {
				t.Fatalf("RawPath 构造不合法：%q", target.EscapedPath())
			}
			egressPlan := staticClosureEgressPlan(
				t, bundle, plan, target, "chg01-file-id-"+tc.name,
			)
			execution, err := compiler.Compile(
				context.Background(), bundle, egressPlan, EndpointDynamicInputs{},
			)
			if err != nil {
				t.Fatalf("合法 escaped file_id 段被拒绝：%v", err)
			}
			if execution.request.URL().EscapedPath() != target.EscapedPath() {
				t.Fatalf("escaped 参数段未保持：%q vs %q",
					execution.request.URL().EscapedPath(), target.EscapedPath())
			}
		})
	}
}

// ----------------------------------------------------------------------------
// Executor：真实签发边界负例
// ----------------------------------------------------------------------------

type staticClosureCountingHTTPPort struct{ calls int }

func (p *staticClosureCountingHTTPPort) SendHTTPUpstream(
	_ context.Context,
	request PreparedRequest,
) (*http.Response, error) {
	p.calls++
	return nil, errors.New("被拒绝的静态 target 不得到达 HTTP port")
}

func (p *staticClosureCountingHTTPPort) SendReqProfile(
	_ context.Context,
	request PreparedRequest,
) (*http.Response, error) {
	p.calls++
	return nil, errors.New("被拒绝的静态 target 不得到达 req profile port")
}

type staticClosureCountingWSPort struct{ calls int }

func (p *staticClosureCountingWSPort) AcquireWebSocket(
	context.Context,
	PreparedRequest,
) (WebSocketConnection, error) {
	p.calls++
	return nil, errors.New("被拒绝的静态 target 不得到达 WebSocket port")
}

func TestExecutorRejectsNonProfileStaticTargetsAtRealBoundary(t *testing.T) {
	parse := func(raw string) *url.URL {
		target, err := url.Parse(raw)
		if err != nil {
			t.Fatal(err)
		}
		return target
	}
	forceQueryTarget := parse("https://chatgpt.com/backend-api/codex/responses")
	forceQueryTarget.ForceQuery = true
	cases := []struct {
		name       string
		endpointID string
		target     *url.URL
		dynamic    EndpointDynamicInputs
	}{
		{"WS scheme 降级 https", "responses_ws",
			parse("https://chatgpt.com/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"HTTP scheme 降级 http", "responses_http",
			parse("http://chatgpt.com/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"显式默认端口", "responses_http",
			parse("https://chatgpt.com:443/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"非画像端口", "responses_http",
			parse("https://chatgpt.com:8443/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"userinfo", "responses_http",
			parse("https://intruder@chatgpt.com/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"fragment", "responses_http",
			parse("https://chatgpt.com/backend-api/codex/responses#frag"), EndpointDynamicInputs{}},
		{"裸 ? 空 query 标记", "responses_http", forceQueryTarget, EndpointDynamicInputs{}},
		{"host 大小写", "responses_http",
			parse("https://ChatGPT.com/backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"host 尾点", "responses_http",
			parse("https://chatgpt.com./backend-api/codex/responses"), EndpointDynamicInputs{}},
		{"path 增段", "responses_http",
			parse("https://chatgpt.com/backend-api/codex/responses/extra"), EndpointDynamicInputs{}},
		{"画像外 query", "responses_http",
			parse("https://chatgpt.com/backend-api/codex/responses?injected=1"), EndpointDynamicInputs{}},
		{"constant query 改写", "models",
			parse("https://chatgpt.com/backend-api/codex/models?client_version=9.9.9"),
			EndpointDynamicInputs{}},
		{"server_response query 非空改写", "realtime_sideband",
			parse("wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=forged-call-id"),
			EndpointDynamicInputs{ServerResponseQuery: map[string]string{
				"call_id": "synthetic-call-id",
			}}},
		{"server_response query 缺少可信输入", "realtime_sideband",
			parse("wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=orphan-call-id"),
			EndpointDynamicInputs{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bundle, plan := staticClosurePlanForEndpoint(t, ReleaseModeActive, tc.endpointID)
			httpPort := &staticClosureCountingHTTPPort{}
			wsPort := &staticClosureCountingWSPort{}
			httpAdapter, err := NewHTTPUpstreamTransportAdapter(httpPort)
			if err != nil {
				t.Fatal(err)
			}
			reqAdapter, err := NewReqProfileTransportAdapter(httpPort)
			if err != nil {
				t.Fatal(err)
			}
			wsAdapter, err := NewWebSocketTransportAdapter(wsPort)
			if err != nil {
				t.Fatal(err)
			}
			registry, err := NewAdapterRegistry(
				DefaultBackendDescriptors(),
				[]TransportAdapter{httpAdapter, reqAdapter, wsAdapter},
			)
			if err != nil {
				t.Fatal(err)
			}
			executor, err := NewExecutor(
				ExecutorID(t.Name()), NewCompiler(), registry, DefaultGuard(),
			)
			if err != nil {
				t.Fatal(err)
			}
			egressPlan := staticClosureEgressPlan(
				t, bundle, plan, tc.target, "chg01-executor-negative",
			)
			result, err := executeSingleExecutorTestAttempt(context.Background(), executor, ExecutorRequest{
				Bundle: bundle, Plan: egressPlan, DynamicInputs: tc.dynamic,
			})
			if err == nil {
				t.Fatalf("偏离画像的静态 target 通过了真实 Executor：%s", tc.target)
			}
			if result.HTTPResponse() != nil || result.WebSocket() != nil {
				t.Fatalf("被拒绝的静态 target 产生了 TransportResult：%+v", result)
			}
			code, ok := RuntimeErrorCodeOf(err)
			if !ok || code != RuntimeErrorCodeCompilerRejected {
				t.Fatalf("错误码不是 compiler.rejected：ok=%v code=%s err=%v", ok, code, err)
			}
			if httpPort.calls != 0 || wsPort.calls != 0 {
				t.Fatalf("被拒绝请求调用了 adapter port：http=%d ws=%d",
					httpPort.calls, wsPort.calls)
			}
		})
	}
}

// ----------------------------------------------------------------------------
// 摘要与 Guard：ForceQuery 终态绑定
// ----------------------------------------------------------------------------

func TestRequestDigestBindsForceQueryAsIndependentField(t *testing.T) {
	plan := WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}
	base, err := http.NewRequest(
		http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	forced := base.Clone(context.Background())
	forcedURL := *base.URL
	forcedURL.ForceQuery = true
	forced.URL = &forcedURL
	baseDigest, err := requestDigest(base, plan, WireProtocolHTTP)
	if err != nil {
		t.Fatal(err)
	}
	forcedDigest, err := requestDigest(forced, plan, WireProtocolHTTP)
	if err != nil {
		t.Fatal(err)
	}
	// 两个请求的 RawQuery 均为空，仅 ForceQuery 不同；独立摘要字段必须区分二者。
	if baseDigest == forcedDigest {
		t.Fatal("仅 ForceQuery 不同的请求摘要相同，裸 ? 未被绑定为独立摘要字段")
	}
}

func TestGuardAcceptsTrustedSchemeAliasAndRejectsForceQueryMutation(t *testing.T) {
	_, _, request := newExecutorInvocationTestFixture(t, 1, 1)
	request = freshExecutorInvocationRequest(t, request)
	// responses_ws 的 migration receipt 把签发 authority 绑定为生产 Executor；
	// 走真实 Guard 终态判定必须使用生产 ExecutorID。
	httpAdapter, err := NewHTTPUpstreamTransportAdapter(&fakeHTTPUpstreamPort{})
	if err != nil {
		t.Fatal(err)
	}
	wsAdapter, err := NewWebSocketTransportAdapter(invocationTestWebSocketPort{})
	if err != nil {
		t.Fatal(err)
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{
			{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream},
			{Backend: BackendWebSocket, Protocol: WireProtocolWebSocket, AdapterID: AdapterWebSocket},
		},
		[]TransportAdapter{httpAdapter, wsAdapter},
	)
	if err != nil {
		t.Fatal(err)
	}
	executor, err := NewExecutor(
		ExecutorID("codex.executor.changeset1b"), NewCompiler(), registry, DefaultGuard(),
	)
	if err != nil {
		t.Fatal(err)
	}
	prepared, err := prepareSingleExecutorTestAttempt(context.Background(), executor, request)
	if err != nil {
		t.Fatal(err)
	}
	httpRequest, err := prepared.TakeHTTPRequest()
	if err != nil {
		t.Fatal(err)
	}
	guard := DefaultGuard()
	// coder/websocket 受信 adapter 在 RoundTripper 前把 wss 改写为 https；
	// 该计划内别名不改变语义摘要，Guard 必须继续允许。
	if httpRequest.URL.Scheme != "wss" {
		t.Fatalf("WS 签发请求 scheme 非 wss：%s", httpRequest.URL.Scheme)
	}
	httpRequest.URL.Scheme = "https"
	decision := guard.Evaluate(httpRequest, BackendWebSocket, WireProtocolWebSocket)
	if !decision.Allow || decision.RejectionReason != "" {
		t.Fatalf("受信 wss→https 别名被 Guard 拒绝：%+v", decision)
	}
	// 签发后追加裸 ?（ForceQuery=true、RawQuery 仍为空）必须被判定为终态篡改。
	httpRequest.URL.ForceQuery = true
	decision = guard.Evaluate(httpRequest, BackendWebSocket, WireProtocolWebSocket)
	if decision.Allow || decision.RejectionReason != ReasonRequestModifiedAfterFinalize {
		t.Fatalf("签发后 ForceQuery 篡改未被 Guard 拒绝：%+v", decision)
	}
}
