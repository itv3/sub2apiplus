package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/klauspost/compress/zstd"
)

type IdentityMode string

const (
	IdentityCodexOAuthStrict IdentityMode = "codex_oauth_strict"
	IdentityCodexAPIKeyMimic IdentityMode = "codex_api_key_mimic"
	IdentityOfficialProxy    IdentityMode = "official_client_proxy"
	IdentityGenericOpenAI    IdentityMode = "generic_openai"
)

func (m IdentityMode) Valid() bool {
	switch m {
	case IdentityCodexOAuthStrict, IdentityCodexAPIKeyMimic,
		IdentityOfficialProxy, IdentityGenericOpenAI:
		return true
	default:
		return false
	}
}

type HeaderPolicy struct {
	ID                        string
	Source                    string
	AllowNonProtectedOverride bool
}

func (p HeaderPolicy) Digest() string {
	raw, _ := json.Marshal(p)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// BodyPolicy 标识业务语义 Body 进入 compiler 后适用的线协议定型策略。
// 字段闭集、线序、画像注入、压缩和长度仍由 Endpoint ProfileSpec 决定。
type BodyPolicy struct {
	ID         string
	Source     string
	Conditions BodyRuntimeConditions
}

// BodyRuntimeConditions 只承载 ProfileSpec 无法从 Body 字面值独立判断的、
// 已由生产语义链验证的运行条件。它不包含凭据、账号对象或可自由选择的 feature。
// 该值属于 attempt，并在 Codex 方言内折叠进 DialectAttestationDigest；共享
// FinalizationToken 不解释具体 Body Policy 字段。
type BodyRuntimeConditions struct {
	CreditIDPresent            bool
	PreviousResponseIDReusable bool
}

func (p BodyPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" {
		return errors.New("BodyPolicy 缺少 ID 或来源")
	}
	return nil
}

func (p BodyPolicy) Digest() string {
	raw, _ := json.Marshal(p)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func (p HeaderPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" {
		return errors.New("HeaderPolicy 缺少 ID 或来源")
	}
	return nil
}

// EndpointDynamicInputs 只承载编译时才能获得的可信动态事实。
type EndpointDynamicInputs struct {
	ReturnedURL *url.URL
	// ServerResponseQuery 承载静态 endpoint 画像中 Source==server_response 的
	// query 可信值，键为画像 query 名称。值必须来自受信服务器响应链
	// （如 realtime call 建立响应的 call_id），不能来自调用方 URL 本身。
	ServerResponseQuery map[string]string
}

func (in EndpointDynamicInputs) clone() EndpointDynamicInputs {
	out := in
	if in.ReturnedURL != nil {
		cloned := *in.ReturnedURL
		out.ReturnedURL = &cloned
	}
	if in.ServerResponseQuery != nil {
		out.ServerResponseQuery = make(map[string]string, len(in.ServerResponseQuery))
		for name, value := range in.ServerResponseQuery {
			out.ServerResponseQuery[name] = value
		}
	}
	return out
}

type ValidatedDynamicTarget struct {
	scheme string
	host   string
	path   string
}

type ConnectionIdentity struct {
	digest string
}

func (i ConnectionIdentity) Digest() string { return i.digest }

// CompiledExecution 把最终请求语义、EndpointBinding、TransportSpec 与全部摘要封装
// 成一个能力。业务层没有 getter 可以拆出请求并与另一个 release 的 transport 重组。
type CompiledExecution struct {
	request        CompiledRequest
	endpointPlan   ResolvedEndpointPlan
	transport      TransportSpec
	control        compiledExecutionControl
	dialectState   preparedDialectState
	releaseDigest  string
	profileDigest  string
	bundleDigest   string
	poolDigest     string
	compiledDigest string
	connection     ConnectionIdentity
	dynamicTarget  *ValidatedDynamicTarget
}

func (e CompiledExecution) SinkID() SinkID             { return e.endpointPlan.SinkID() }
func (e CompiledExecution) Purpose() Purpose           { return e.endpointPlan.Purpose() }
func (e CompiledExecution) EndpointID() string         { return e.endpointPlan.EndpointID() }
func (e CompiledExecution) ReleaseDigest() string      { return e.releaseDigest }
func (e CompiledExecution) ProfileDigest() string      { return e.profileDigest }
func (e CompiledExecution) BundleDigest() string       { return e.bundleDigest }
func (e CompiledExecution) PoolDigest() string         { return e.poolDigest }
func (e CompiledExecution) CompiledDigest() string     { return e.compiledDigest }
func (e CompiledExecution) ConnectionIdentity() string { return e.connection.digest }

// Compiler 是 officialegress 包内的正式中立 compiler。
type Compiler struct{}

func NewCompiler() *Compiler { return &Compiler{} }

func (c *Compiler) Compile(
	ctx context.Context,
	bundle ReleaseBundle,
	plan CodexEgressPlan,
	dynamic EndpointDynamicInputs,
) (CompiledExecution, error) {
	if c == nil {
		return CompiledExecution{}, errors.New("Compiler 为空")
	}
	plan = plan.clone()
	if err := validateCompilerPlan(plan, bundle); err != nil {
		return CompiledExecution{}, err
	}
	protocol := plan.Protocol
	if !protocol.Valid() {
		protocol = WireProtocolHTTP
	}
	endpointPlan, err := bundle.ResolveEndpointPlan(
		plan.SinkID, plan.Method, plan.URL, protocol,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	if plan.EndpointID != "" && plan.EndpointID != endpointPlan.EndpointID() {
		return CompiledExecution{}, errors.New("业务提交的 EndpointID 与权威 EndpointBinding 不一致")
	}
	if endpointPlan.Purpose() != plan.Purpose {
		return CompiledExecution{}, errors.New("Plan Purpose 与权威 EndpointBinding 不一致")
	}
	target, validatedDynamic, err := validateCompilerTarget(endpointPlan, plan.URL, dynamic)
	if err != nil {
		return CompiledExecution{}, err
	}
	authentication, err := plan.Authentication.take()
	if err != nil {
		return CompiledExecution{}, err
	}
	headers, err := compileEndpointHeaders(bundle, endpointPlan, plan, authentication)
	if err != nil {
		return CompiledExecution{}, err
	}
	body, err := compileEndpointBody(
		endpointPlan.template.endpoint,
		bundle.release.ExecutableProfile().Features(),
		headers,
		plan.Body,
		plan.BodyPolicy.Conditions,
		authentication,
		plan.IdentityFacts,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	compiledBody := newOwnedReplayableRequestBody(body)
	if plan.Body.Mode() == RequestBodySingleUse {
		compiledBody = plan.Body.clone()
	}
	request, err := NewCompiledRequest(plan.Method, target, headers, compiledBody)
	if err != nil {
		return CompiledExecution{}, err
	}
	connection, poolDigest, err := compileConnectionIdentity(
		ctx, bundle, endpointPlan, target, plan.IdentityFacts, plan.InvocationID,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	normalization := WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}
	if endpointPlan.template.transport.LowercaseHTTPHeaders {
		normalization.HeaderMode = HeaderNormalizationLowercase
		normalization.SuppressDefaultUserAgent = true
	}
	if endpointPlan.template.transport.WebSocket != nil {
		// 同一 WS transport 可被 Responses 与 realtime sideband 共用；压缩开关来自
		// 已编译端点语义，四项参数必须作为一个整体进入签名计划。
		if endpointPlan.template.endpoint.Compression ==
			profilecontract.CompressionPermessageDeflateContextTakeover {
			if !endpointHasHeader(endpointPlan.template.endpoint, "sec-websocket-extensions") {
				return CompiledExecution{}, errors.New("WebSocket 压缩端点缺少扩展 Header 消费者")
			}
			normalization.WebSocketCompressionOffer = strings.TrimSpace(
				endpointPlan.template.transport.WebSocket.CompressionOffer,
			)
			normalization.WebSocketContextTakeover =
				endpointPlan.template.transport.WebSocket.ContextTakeover
			normalization.WebSocketCompressedTextRSV1 =
				endpointPlan.template.transport.WebSocket.CompressedTextRSV1
			normalization.WebSocketRawDeflatePayload =
				endpointPlan.template.transport.WebSocket.RawDeflatePayload
		}
	}
	if err := normalization.Validate(endpointPlan.Protocol()); err != nil {
		return CompiledExecution{}, err
	}
	transport := TransportSpec{
		ID:      endpointPlan.template.transport.ID,
		Backend: endpointPlan.template.backend, Protocol: endpointPlan.Protocol(),
		Adapter:       endpointPlan.template.adapter,
		ProfileDigest: bundle.release.ExecutableProfileDigest(), ConnectionGroup: endpointPlan.template.connectionGroup,
		ConnectionPoolDigest: poolDigest,
		ResourceLifecycle:    endpointPlan.template.endpoint.ResourceLifecycle,
		Normalization:        normalization,
	}
	transport.TLS = endpointPlan.template.tls
	if target.EscapedPath() != endpointPlan.template.endpoint.Path {
		transport.TLS, err = compileTLSProfileSpec(
			bundle.release.ExecutableProfile(), endpointPlan.template.transport,
			endpointPlan.EndpointID(), target.EscapedPath(),
		)
		if err != nil {
			return CompiledExecution{}, err
		}
	}
	if err := transport.Validate(); err != nil {
		return CompiledExecution{}, err
	}
	compiledDigest, err := digestCompiledExecution(
		request, endpointPlan, transport, bundle.ReleaseDigest(), bundle.BundleDigest(), connection,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	return CompiledExecution{
		request: request, endpointPlan: endpointPlan, transport: transport,
		releaseDigest: bundle.ReleaseDigest(), profileDigest: bundle.ProfileDigest(),
		bundleDigest: bundle.BundleDigest(),
		poolDigest:   poolDigest, compiledDigest: compiledDigest, connection: connection,
		dynamicTarget: validatedDynamic,
	}, nil
}

func compileTLSProfileSpec(
	profile profilecontract.ExecutableProfile,
	transport profilecontract.ExecutableTransportProfile,
	selectedEndpointID string,
	selectedPath string,
) (TLSProfileSpec, error) {
	tls := TLSProfileSpec{
		Stack:               transport.TLSStack,
		CipherSuites:        append([]uint16(nil), transport.CipherSuites...),
		SupportedGroups:     append([]uint16(nil), transport.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), transport.SignatureAlgorithms...),
		ALPN:                append([]string(nil), transport.ALPN...),
		Extensions:          append([]uint16(nil), transport.Extensions...),
		RandomizeExtensions: transport.RandomizeExtensions,
		SupportedVersions:   append([]uint16(nil), transport.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), transport.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), transport.PSKModes...),
		MinVersion:          transport.TLSMinVersion, MaxVersion: transport.TLSMaxVersion,
		LowercaseHeaders: transport.LowercaseHTTPHeaders,
		StrictH1Wire:     true,
	}
	if strings.TrimSpace(tls.Stack) == "" || len(tls.CipherSuites) == 0 ||
		tls.MinVersion == 0 || tls.MaxVersion == 0 {
		return TLSProfileSpec{}, errors.New("正式 TransportProfile 的 TLS 事实不完整")
	}
	if transport.WebSocket != nil {
		tls.PreserveHeaderCase = append(
			[]string(nil), transport.WebSocket.FixedHandshakePrefix...,
		)
		tls.LowercaseHeaders = true
	}
	for _, endpoint := range profile.Endpoints() {
		if endpoint.TransportID != transport.ID {
			continue
		}
		ordered := append([]profilecontract.HeaderSlotProfile(nil), endpoint.Headers...)
		sort.SliceStable(ordered, func(i, j int) bool {
			if ordered[i].Slot != ordered[j].Slot {
				return ordered[i].Slot < ordered[j].Slot
			}
			return ordered[i].Sequence < ordered[j].Sequence
		})
		desired := make([]string, 0, len(ordered))
		for _, header := range ordered {
			desired = append(desired, strings.ToLower(strings.TrimSpace(header.Name)))
		}
		path := endpoint.Path
		if endpoint.ID == selectedEndpointID && strings.TrimSpace(selectedPath) != "" {
			path = selectedPath
		}
		rule := H1HeaderOrderRule{
			Method: endpoint.Method, Path: path, Order: desired,
			Mode: "static", RejectUnlisted: true,
		}
		if endpoint.Upgrade != "" {
			if transport.WebSocket == nil {
				return TLSProfileSpec{}, fmt.Errorf("WS endpoint %s 缺少 transport 配置", endpoint.ID)
			}
			rule.Mode = "swap_remove"
			rule.Order = lowerHeaderNames(endpoint.HeaderMapInsertionOrder)
			rule.PrefixHeaders = lowerHeaderNames(transport.WebSocket.FixedHandshakePrefix)
			rule.RemoveHeaders = append([]string(nil), rule.PrefixHeaders...)
			rule.AppendHeaders = lowerHeaderNames(endpoint.PostRemoveHeaders)
		}
		tls.H1HeaderOrders = append(tls.H1HeaderOrders, rule)
	}
	if len(tls.H1HeaderOrders) == 0 {
		return TLSProfileSpec{}, errors.New("TransportProfile 没有 endpoint H1 规则")
	}
	return tls, nil
}

func lowerHeaderNames(names []string) []string {
	out := make([]string, len(names))
	for i, name := range names {
		out[i] = strings.ToLower(strings.TrimSpace(name))
	}
	return out
}

func validateCompilerPlan(plan CodexEgressPlan, bundle ReleaseBundle) error {
	if strings.TrimSpace(string(plan.SinkID)) == "" || strings.TrimSpace(string(plan.Purpose)) == "" ||
		strings.TrimSpace(plan.Method) == "" || plan.URL == nil || !plan.DeclaredPersona.Valid() ||
		plan.DeclaredPersona != PersonaCodexCLI || plan.Mode != bundle.Mode() {
		return errors.New("CodexEgressPlan 与 Bundle 身份不完整或不一致")
	}
	if plan.IdentityMode != IdentityCodexOAuthStrict {
		return errors.New("只有 CodexOAuthStrict 具有生产证据，其他 IdentityMode 一律 fail-close")
	}
	if err := plan.IdentityFacts.Validate(); err != nil {
		return fmt.Errorf("CodexIdentityFacts 非法: %w", err)
	}
	if err := plan.HeaderPolicy.Validate(); err != nil {
		return err
	}
	if err := plan.BodyPolicy.Validate(); err != nil {
		return err
	}
	if plan.BehaviorPolicy.ID != "" && plan.BehaviorPolicy.ID != bundle.behavior.ID {
		return errors.New("Plan BehaviorPolicy 与 Bundle 不一致")
	}
	return nil
}

func validateCompilerTarget(
	endpoint ResolvedEndpointPlan,
	target *url.URL,
	dynamic EndpointDynamicInputs,
) (*url.URL, *ValidatedDynamicTarget, error) {
	if target == nil {
		return nil, nil, errors.New("编译 target 为空")
	}
	cloned := *target
	// 可信动态事实在入口统一冻结为私有克隆；后续所有分支只读取克隆值，调用方
	// 在编译期间修改原 map 或 URL 不会影响本次判定。
	dynamic = dynamic.clone()
	if !endpoint.DynamicTarget() {
		if dynamic.ReturnedURL != nil {
			return nil, nil, errors.New("静态 endpoint 禁止提交 ReturnedURL")
		}
		if err := validateStaticCompilerTarget(
			endpoint.template, target, dynamic.ServerResponseQuery,
		); err != nil {
			return nil, nil, err
		}
		return &cloned, nil, nil
	}
	// ReturnedURL 动态端点的 query 权威是服务器返回 URL 整体，与静态
	// ServerResponseQuery 可信通道互斥；混合提交 fail-close。
	if len(dynamic.ServerResponseQuery) != 0 {
		return nil, nil, errors.New("动态 endpoint 禁止提交 ServerResponseQuery")
	}
	if dynamic.ReturnedURL == nil || dynamic.ReturnedURL.String() != target.String() {
		return nil, nil, errors.New("动态 endpoint 缺少与 Plan 一致的 ReturnedURL")
	}
	if !strings.EqualFold(target.Scheme, "https") || target.User != nil ||
		!matchRouteHost(endpoint.template.route.Key.Host, target.Hostname()) ||
		!matchRoutePath(endpoint.template.route.Key.Path, target.EscapedPath()) {
		return nil, nil, errors.New("ReturnedURL 未通过 Bundle 冻结的动态 target 规则")
	}
	validated := &ValidatedDynamicTarget{
		scheme: strings.ToLower(target.Scheme), host: normalizeRouteHost(target.Host),
		path: target.EscapedPath(),
	}
	return &cloned, validated, nil
}

// validateStaticCompilerTarget 以当前 Bundle 画像为权威封闭静态 endpoint 的调用方 URL：
// scheme、authority、path 只接受规范形态；query 按画像闭集做结构化语义封闭，其中
// server_response 值必须与 trusted（EndpointDynamicInputs.ServerResponseQuery）逐字一致。
// scheme 不使用 EqualFold 或 canonicalRequestScheme 放宽——后者只承担签发后受信
// WebSocket adapter 的摘要等价，不参与 Compiler 输入合法性判断。
func validateStaticCompilerTarget(
	template EndpointPlanTemplate,
	target *url.URL,
	trusted map[string]string,
) error {
	if target.Opaque != "" {
		return errors.New("静态 endpoint target 禁止 opaque 形态")
	}
	if target.User != nil {
		return errors.New("静态 endpoint target 禁止 userinfo")
	}
	if target.Fragment != "" || target.RawFragment != "" {
		return errors.New("静态 endpoint target 禁止 fragment")
	}
	if target.ForceQuery {
		return errors.New("静态 endpoint target 禁止空 query 标记")
	}
	switch template.route.Protocol {
	case WireProtocolWebSocket:
		if target.Scheme != "wss" {
			return fmt.Errorf("WebSocket 静态 endpoint 只接受精确小写 wss scheme：%q", target.Scheme)
		}
	case WireProtocolHTTP:
		if target.Scheme != "https" {
			return fmt.Errorf("HTTP 静态 endpoint 只接受精确小写 https scheme：%q", target.Scheme)
		}
	default:
		return fmt.Errorf("静态 endpoint 协议缺少 scheme 封闭规则：%s", template.route.Protocol)
	}
	if target.Port() != "" {
		return fmt.Errorf("静态 endpoint target 禁止显式端口：%q", target.Port())
	}
	if target.Host != template.endpoint.Host {
		return fmt.Errorf("静态 endpoint target host 与画像不一致：%q", target.Host)
	}
	if err := validateStaticCompilerPath(template.endpoint.Path, target.EscapedPath()); err != nil {
		return err
	}
	return validateStaticCompilerQuery(template.endpoint.Query, target, trusted)
}

// validateStaticCompilerPath 要求 EscapedPath 与画像 path 模板段数相等、字面段逐字相等、
// {param} 段非空。含 %2F 等 escaped 字符的参数在 EscapedPath 中保持单段，不会被拆开。
func validateStaticCompilerPath(templatePath, actualPath string) error {
	if !strings.HasPrefix(templatePath, "/") {
		return fmt.Errorf("静态 endpoint 画像 path 非法：%q", templatePath)
	}
	if !strings.HasPrefix(actualPath, "/") {
		return fmt.Errorf("静态 endpoint target path 必须以 / 开头：%q", actualPath)
	}
	templateParts := strings.Split(templatePath[1:], "/")
	actualParts := strings.Split(actualPath[1:], "/")
	if len(templateParts) != len(actualParts) {
		return fmt.Errorf("静态 endpoint target path 段数与画像不一致：%q", actualPath)
	}
	for index, expected := range templateParts {
		if strings.HasPrefix(expected, "{") && strings.HasSuffix(expected, "}") {
			if actualParts[index] == "" {
				return fmt.Errorf("静态 endpoint target path 参数段为空：%s", expected)
			}
			continue
		}
		if expected != actualParts[index] {
			return fmt.Errorf("静态 endpoint target path 字面段与画像不一致：%q", actualParts[index])
		}
	}
	return nil
}

// validateStaticCompilerQuery 先封闭画像 query 定义，再对调用方 RawQuery 做结构化语义
// 封闭。键顺序与合法等价转义保持宽容，通过验证的原始 RawQuery 原表示保留；空 component
// 不属于合法等价表示。server_response 键的值语义以 trusted 可信输入为权威：值必须存在
// 且逐字一致，仅“非空”不构成来源封闭。
func validateStaticCompilerQuery(
	fields []profilecontract.QueryFieldProfile,
	target *url.URL,
	trusted map[string]string,
) error {
	declared := make(map[string]profilecontract.QueryFieldProfile, len(fields))
	serverResponseNames := make(map[string]bool, len(fields))
	for _, field := range fields {
		if field.Name == "" || field.Name == "*" {
			return fmt.Errorf("静态 endpoint 画像 query 名称非法：%q", field.Name)
		}
		if _, duplicated := declared[field.Name]; duplicated {
			return fmt.Errorf("静态 endpoint 画像 query 名称重复：%s", field.Name)
		}
		switch field.Source {
		case profilecontract.SourceConstant:
			if field.Required && field.Value == "" {
				return fmt.Errorf("静态 endpoint 画像 constant required query 值为空：%s", field.Name)
			}
		case profilecontract.SourceServerResponse:
			serverResponseNames[field.Name] = true
		default:
			return fmt.Errorf("静态 endpoint query source 尚无明确执行语义，fail-close：%s", field.Source)
		}
		declared[field.Name] = field
	}
	trustedNames := make([]string, 0, len(trusted))
	for name := range trusted {
		trustedNames = append(trustedNames, name)
	}
	sort.Strings(trustedNames)
	for _, name := range trustedNames {
		if !serverResponseNames[name] {
			return fmt.Errorf("可信 query 输入不在画像 server_response 闭集：%s", name)
		}
	}
	if len(declared) == 0 {
		if target.RawQuery != "" {
			return errors.New("画像未声明 query 的静态 endpoint 禁止携带 query")
		}
		return nil
	}
	if target.RawQuery != "" {
		for _, component := range strings.Split(target.RawQuery, "&") {
			if component == "" {
				return errors.New("静态 endpoint query 禁止空 component")
			}
		}
	}
	values, err := url.ParseQuery(target.RawQuery)
	if err != nil {
		return fmt.Errorf("静态 endpoint query 解析失败：%w", err)
	}
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		decoded := values[name]
		if len(decoded) > 1 {
			return fmt.Errorf("静态 endpoint query 键禁止多值：%s", name)
		}
		field, ok := declared[name]
		if !ok {
			return fmt.Errorf("静态 endpoint query 键不在画像闭集：%s", name)
		}
		switch field.Source {
		case profilecontract.SourceConstant:
			if decoded[0] != field.Value {
				return fmt.Errorf("静态 endpoint constant query 值与画像不一致：%s", name)
			}
		case profilecontract.SourceServerResponse:
			if decoded[0] == "" {
				return fmt.Errorf("静态 endpoint server_response query 值为空：%s", name)
			}
			trustedValue, bound := trusted[name]
			if !bound {
				return fmt.Errorf("静态 endpoint server_response query 缺少可信输入：%s", name)
			}
			if decoded[0] != trustedValue {
				return fmt.Errorf("静态 endpoint server_response query 与可信输入不一致：%s", name)
			}
		}
	}
	requiredNames := make([]string, 0, len(declared))
	for name, field := range declared {
		if field.Required {
			requiredNames = append(requiredNames, name)
		}
	}
	sort.Strings(requiredNames)
	for _, name := range requiredNames {
		if _, ok := values[name]; !ok {
			return fmt.Errorf("静态 endpoint required query 缺失：%s", name)
		}
	}
	return nil
}

func compileEndpointHeaders(
	bundle ReleaseBundle,
	endpoint ResolvedEndpointPlan,
	plan CodexEgressPlan,
	authentication AttemptAuthenticationInput,
) (http.Header, error) {
	if plan.IdentityMode != IdentityCodexOAuthStrict {
		return nil, fmt.Errorf("尚无生产证据的 IdentityMode fail-close：%s", plan.IdentityMode)
	}
	if err := plan.IdentityFacts.Validate(); err != nil {
		return nil, err
	}
	for name := range plan.Headers {
		if protectedOfficialHeader(name) {
			return nil, fmt.Errorf("CodexOAuthStrict 普通 Headers 禁止保护头：%s", name)
		}
		return nil, fmt.Errorf("CodexOAuthStrict Endpoint Header 闭集禁止普通 Header：%s", name)
	}
	for name := range plan.ResolvedHeaderOverrides {
		if protectedOfficialHeader(name) {
			return nil, fmt.Errorf("CodexOAuthStrict Header Override 禁止保护头：%s", name)
		}
		if !plan.HeaderPolicy.AllowNonProtectedOverride {
			return nil, fmt.Errorf("HeaderPolicy 禁止非保护头 override：%s", name)
		}
		return nil, fmt.Errorf("CodexOAuthStrict Endpoint Header 闭集禁止 override：%s", name)
	}
	node, ok := bundle.release.Node(endpoint.template.binding.ReleasePurpose())
	if !ok {
		return nil, errors.New("Bundle 缺少 endpoint release sibling")
	}
	userAgent, originator, err := renderCodexProcessIdentity(
		bundle.release.ExecutableProfile(), plan.IdentityFacts,
	)
	if err != nil {
		return nil, err
	}
	headers := make(http.Header)
	for _, header := range node.Build.RuntimeHeaders {
		headers.Set(header.Name, header.Value)
	}
	for _, header := range node.Wire.StaticHeaders {
		headers.Set(header.Name, header.Value)
	}
	for _, slot := range endpoint.template.endpoint.Headers {
		name := slot.WireName
		if name == "" {
			name = slot.Name
		}
		enabled := codexHeaderConditionEnabled(
			slot.Condition, bundle.release.ExecutableProfile().Features(), plan.IdentityFacts.Conditions,
			authentication,
		)
		if !enabled {
			continue
		}
		value, generated, valueErr := codexHeaderValue(
			slot, userAgent, originator,
			plan.IdentityFacts, authentication,
		)
		if valueErr != nil {
			return nil, fmt.Errorf("Endpoint %s Header %s：%w", endpoint.EndpointID(), slot.Name, valueErr)
		}
		if generated {
			continue
		}
		if strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("Endpoint %s 缺少结构化 Header 事实：%s", endpoint.EndpointID(), slot.Name)
		}
		headers.Set(name, value)
	}
	if authentication.AgentIdentity != "" &&
		!endpointHasHeader(endpoint.template.endpoint, "authorization") &&
		!endpointHasHeader(endpoint.template.endpoint, "x-codex-agent-identity") {
		return nil, errors.New("Endpoint ProfileSpec 不允许 Agent Identity")
	}
	if authentication.RefreshToken != "" && endpoint.EndpointID() != "oauth_refresh" {
		return nil, errors.New("非 OAuth refresh Endpoint 禁止 RefreshToken")
	}
	for _, name := range endpoint.template.endpoint.PostRemoveHeaders {
		headers.Del(name)
	}
	return headers, nil
}

// renderCodexProcessIdentity 从 ExecutableProfile 的 Surfaces 闭集与结构化进程事实
// 生成 UA/originator。Release build 中的静态值只描述发布节点，不能覆盖本次
// invocation 的可信 exec/tui surface 与 terminal 生命周期。
func renderCodexProcessIdentity(
	profile profilecontract.ExecutableProfile,
	facts CodexIdentityFacts,
) (string, string, error) {
	surfaces := profile.Surfaces()
	if len(surfaces) == 0 {
		return "", "", errors.New("ExecutableProfile 缺少 Surfaces 身份闭集")
	}
	surfaceID := strings.TrimSpace(facts.ProcessSurface.Value)
	phase := strings.TrimSpace(facts.ProcessPhase.Value)
	terminal := strings.TrimSpace(facts.TerminalToken.Value)
	for _, surface := range surfaces {
		if surface.ID != surfaceID {
			continue
		}
		matched, err := regexp.MatchString(surface.TerminalTokenPattern, terminal)
		if err != nil || !matched {
			return "", "", fmt.Errorf("Codex surface %s 的 terminal token 非法", surfaceID)
		}
		originator := surface.Originator
		switch phase {
		case "initialized":
		case "initial_models":
			if !surface.InitialModelsMayOmit || facts.UserAgentSuffixEnabled {
				return "", "", fmt.Errorf("Codex surface %s 的 initial_models suffix 条件非法", surfaceID)
			}
			originator = surface.InitialModelsOriginator
		default:
			return "", "", fmt.Errorf("Codex process phase 非法：%s", phase)
		}
		userAgent := strings.TrimSpace(
			surface.Product + "/" + surface.Version + " " + surface.PlatformPrefix + " " + terminal,
		)
		if facts.UserAgentSuffixEnabled {
			if !surface.SuffixOptional || surface.SuffixName == "" || surface.SuffixVersion == "" {
				return "", "", fmt.Errorf("Codex surface %s 不允许 UA suffix", surfaceID)
			}
			userAgent += fmt.Sprintf(" (%s; %s)", surface.SuffixName, surface.SuffixVersion)
		}
		if userAgent == "" || strings.TrimSpace(originator) == "" {
			return "", "", fmt.Errorf("Codex surface %s 身份画像不完整", surfaceID)
		}
		return userAgent, originator, nil
	}
	return "", "", fmt.Errorf("ProfileSpec 不允许 Codex surface：%s", surfaceID)
}

func codexHeaderConditionEnabled(
	condition profilecontract.ConditionKind,
	features profilecontract.FeatureDefaults,
	conditions CodexRequestConditions,
	authentication AttemptAuthenticationInput,
) bool {
	switch condition {
	case profilecontract.ConditionUnconditional, profilecontract.ConditionAlways:
		return true
	case profilecontract.ConditionAuto:
		return false
	case profilecontract.ConditionAttestationPresent:
		return conditions.AttestationPresent && authentication.Attestation != ""
	case profilecontract.ConditionBetaFeaturesPresent:
		return features.RemoteCompactionV2 && conditions.BetaFeaturesPresent
	case profilecontract.ConditionRemoteCompactionV2:
		return features.RemoteCompactionV2
	case profilecontract.ConditionCookiePresent:
		return conditions.CookiePresent && authentication.Cookie != ""
	case profilecontract.ConditionFedrampAccount:
		return conditions.FedRAMPAccount
	case profilecontract.ConditionManagedResidencyPresent:
		return conditions.ManagedResidencyPresent
	case profilecontract.ConditionMemoryGeneration:
		return conditions.MemoryGeneration
	case profilecontract.ConditionParentThreadPresent:
		return conditions.ParentThreadPresent
	case profilecontract.ConditionRequestCompressionEnabled:
		return features.EnableRequestCompression && conditions.CompressionEligible
	case profilecontract.ConditionResponsesLite:
		return features.ResponsesLiteFromModelManifest && conditions.ModelSupportsLite
	case profilecontract.ConditionRuntimeMetrics:
		return features.RuntimeMetrics
	case profilecontract.ConditionSessionIdPresent:
		return conditions.SessionIDPresent
	case profilecontract.ConditionSubagentPresent:
		return conditions.SubagentPresent
	case profilecontract.ConditionTurnStatePresent:
		return conditions.TurnStatePresent
	default:
		return false
	}
}

func codexHeaderValue(
	slot profilecontract.HeaderSlotProfile,
	userAgent string,
	originator string,
	facts CodexIdentityFacts,
	authentication AttemptAuthenticationInput,
) (string, bool, error) {
	name := strings.ToLower(strings.TrimSpace(slot.Name))
	if slot.Value != "" {
		return slot.Value, slot.Source == profilecontract.SourceGenerated, nil
	}
	// 动态上传的请求标识虽然在画像中归类为 generated，但其值必须由
	// invocation 的结构化身份事实提供；compiler 只负责取得所有权并写入 wire。
	if slot.Source == profilecontract.SourceGenerated && name == "x-ms-client-request-id" {
		if strings.TrimSpace(facts.ClientRequestID.Value) == "" {
			return "", false, errors.New("缺少动态上传 x-ms-client-request-id 身份事实")
		}
		return facts.ClientRequestID.Value, false, nil
	}
	switch slot.Source {
	case profilecontract.SourceGenerated, profilecontract.SourceRequestBody:
		return "", true, nil
	case profilecontract.SourceConstant:
		return "", false, errors.New("ProfileSpec constant 为空")
	case profilecontract.SourceAuthentication:
		switch name {
		case "authorization":
			if authentication.BearerToken != "" {
				return "Bearer " + authentication.BearerToken, false, nil
			}
			if authentication.AgentIdentity != "" {
				return authentication.AgentIdentity, false, nil
			}
			return "", false, errors.New("缺少 attempt-local Authorization")
		case "x-oai-attestation":
			return authentication.Attestation, false, nil
		case "x-codex-agent-identity":
			return authentication.AgentIdentity, false, nil
		default:
			return "", false, errors.New("未知认证 Header")
		}
	case profilecontract.SourceAccount:
		if name == "chatgpt-account-id" {
			return facts.ChatGPTAccountID.Value, false, nil
		}
	case profilecontract.SourceProcess:
		if name == "user-agent" {
			return userAgent, false, nil
		}
		if name == "originator" {
			return originator, false, nil
		}
	case profilecontract.SourceManagedConfig:
		return facts.ManagedResidency.Value, false, nil
	case profilecontract.SourceSession:
		switch name {
		case "x-codex-installation-id":
			return facts.InstallationID.Value, false, nil
		case "session-id", "x-session-id":
			return facts.SessionID.Value, false, nil
		case "conversation-id":
			return facts.ConversationID.Value, false, nil
		case "thread-id":
			return facts.ThreadID.Value, false, nil
		case "x-codex-window-id":
			return facts.WindowID.Value, false, nil
		case "x-client-request-id":
			return facts.ClientRequestID.Value, false, nil
		case "x-codex-parent-thread-id":
			return facts.ParentThreadID.Value, false, nil
		case "cookie":
			return authentication.Cookie, false, nil
		}
	case profilecontract.SourceTurn:
		switch name {
		case "x-codex-turn-metadata":
			return facts.TurnMetadata.Value, false, nil
		case "x-codex-turn-state":
			return facts.TurnState.Value, false, nil
		case "x-openai-subagent":
			return facts.Subagent.Value, false, nil
		}
	case profilecontract.SourceFeature:
		if name == "x-codex-beta-features" {
			return "remote_compaction_v2", false, nil
		}
	case profilecontract.SourceModelManifest:
		return "true", false, nil
	}
	return "", false, errors.New("结构化事实未覆盖 ProfileSpec Header source")
}

func endpointHasHeader(endpoint profilecontract.ExecutableEndpointProfile, want string) bool {
	for _, slot := range endpoint.Headers {
		if strings.EqualFold(slot.Name, want) || strings.EqualFold(slot.WireName, want) {
			return true
		}
	}
	return false
}

func protectedOfficialHeader(name string) bool {
	name = strings.ToLower(strings.TrimSpace(name))
	if name == "authorization" || name == "host" || name == "content-length" ||
		name == "transfer-encoding" || name == "connection" || name == "upgrade" ||
		name == "content-type" || name == "accept" || name == "content-encoding" ||
		name == "user-agent" || name == "originator" || name == "version" ||
		name == "openai-beta" || name == "chatgpt-account-id" ||
		name == "session-id" || name == "conversation-id" || name == "thread-id" ||
		name == "x-client-request-id" {
		return true
	}
	return strings.HasPrefix(name, "x-codex-") || strings.HasPrefix(name, "sec-websocket-")
}

// IsProtectedCodexHeader 返回严格身份模式下只能由结构化事实、认证能力、
// ProfileSpec 或 transport 生成的 Header 闭集。
func IsProtectedCodexHeader(name string) bool { return protectedOfficialHeader(name) }

func compileEndpointBody(
	endpoint profilecontract.ExecutableEndpointProfile,
	features profilecontract.FeatureDefaults,
	headers http.Header,
	body RequestBody,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
	identityFacts CodexIdentityFacts,
) ([]byte, error) {
	if body.Mode() == RequestBodySingleUse {
		if endpoint.Body.Encoding != profilecontract.BodyRawBytes {
			return nil, errors.New("只有 raw_bytes endpoint 可以使用 single-use Body")
		}
		return nil, nil
	}
	raw, ok := body.replayableView()
	if !ok {
		return nil, errors.New("replayable Body 无法读取")
	}
	compressed := headerHasToken(headers, "Content-Encoding", "zstd")
	if compressed && endpoint.Compression != profilecontract.CompressionZstdWhenFeatureEnabled {
		return nil, errors.New("端点画像不允许 zstd 请求体")
	}
	if endpoint.Upgrade != "" && len(bytes.TrimSpace(raw)) == 0 {
		return raw, nil
	}
	if endpoint.ID != "oauth_refresh" && authentication.RefreshToken != "" {
		return nil, errors.New("非 OAuth refresh Body 禁止 RefreshToken")
	}
	var compiled []byte
	var err error
	switch endpoint.Body.Encoding {
	case profilecontract.BodyNone, profilecontract.BodyRawBytes:
		compiled = raw
	case profilecontract.BodyJson, profilecontract.BodyWebsocketJson,
		profilecontract.BodyWebsocketDiscriminatedEvents:
		if len(bytes.TrimSpace(raw)) == 0 {
			if len(endpoint.Body.Fields) == 0 {
				compiled = raw
				break
			}
			return nil, errors.New("JSON Body 为空")
		}
		document := body.jsonDocument()
		if document == nil {
			document, err = newOrderedJSONDocument(raw)
			if err != nil {
				return nil, fmt.Errorf("解析 JSON Body: %w", err)
			}
		}
		if err = injectCompilerOwnedBodyFields(endpoint, document, authentication, identityFacts); err != nil {
			return nil, err
		}
		compiled, err = orderJSONDocumentWithPolicy(
			document, endpoint.Body, features, identityFacts.Conditions,
			bodyConditions, authentication,
		)
	case profilecontract.BodyFormUrlencoded:
		compiled, err = orderFormBody(
			raw, endpoint.Body, features, identityFacts.Conditions,
			bodyConditions, authentication,
		)
	default:
		return nil, fmt.Errorf("compiler 不支持 Body encoding: %s", endpoint.Body.Encoding)
	}
	if err != nil {
		return nil, err
	}
	if !compressed {
		return compiled, nil
	}
	if !features.EnableRequestCompression {
		return nil, errors.New("Bundle feature 禁止请求压缩")
	}
	encoder, err := zstd.NewWriter(
		nil,
		zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(features.RequestCompressionLevel)),
	)
	if err != nil {
		return nil, fmt.Errorf("创建 zstd 编码器: %w", err)
	}
	out := encoder.EncodeAll(compiled, nil)
	if err := encoder.Close(); err != nil {
		return nil, fmt.Errorf("关闭 zstd 编码器: %w", err)
	}
	return out, nil
}

func injectCompilerOwnedBodyFields(
	endpoint profilecontract.ExecutableEndpointProfile,
	document *orderedJSONDocument,
	authentication AttemptAuthenticationInput,
	identityFacts CodexIdentityFacts,
) error {
	if endpoint.ID != "oauth_refresh" {
		if endpoint.ID == "responses_http" || endpoint.ID == "responses_compact" ||
			endpoint.ID == "responses_ws" {
			return injectCodexResponsesOwnedBodyFields(endpoint.ID, document, identityFacts)
		}
		return nil
	}
	if authentication.RefreshToken == "" {
		return errors.New("OAuth refresh attempt 缺少一次性 RefreshToken")
	}
	if _, present := document.value("refresh_token"); present {
		return errors.New("OAuth refresh 语义 Body 禁止携带 refresh_token")
	}
	refreshRaw, err := json.Marshal(authentication.RefreshToken)
	if err != nil {
		return err
	}
	document.set("refresh_token", refreshRaw)
	return nil
}

func injectCodexResponsesOwnedBodyFields(
	endpointID string,
	document *orderedJSONDocument,
	facts CodexIdentityFacts,
) error {
	if _, present := document.value("prompt_cache_key"); present {
		return errors.New("Responses 语义 Body 禁止携带 compiler-owned prompt_cache_key")
	}
	if facts.SessionID.present() {
		promptCacheValue := facts.SessionID.Value
		if facts.Subagent.Value == "guardian" && facts.ParentThreadID.present() {
			promptCacheValue = "guardian:" + facts.ParentThreadID.Value
		}
		promptCacheKey, err := json.Marshal(promptCacheValue)
		if err != nil {
			return err
		}
		document.set("prompt_cache_key", promptCacheKey)
	}
	if endpointID == "responses_compact" {
		return nil
	}
	if _, present := document.value("client_metadata"); present {
		return errors.New("Responses 语义 Body 禁止携带 compiler-owned client_metadata")
	}
	metadata := make(map[string]string)
	for _, field := range []struct {
		name string
		fact CodexIdentityValue
	}{
		{name: "session_id", fact: facts.SessionID},
		{name: "turn_id", fact: facts.TurnID},
		{name: "thread_id", fact: facts.ThreadID},
		{name: "x-codex-window-id", fact: facts.WindowID},
		{name: "x-codex-installation-id", fact: facts.InstallationID},
		{name: "x-codex-turn-metadata", fact: facts.TurnMetadata},
		{name: "x-codex-turn-state", fact: facts.TurnState},
		{name: "x-codex-parent-thread-id", fact: facts.ParentThreadID},
		{name: "x-openai-subagent", fact: facts.Subagent},
	} {
		if field.fact.present() {
			metadata[field.name] = field.fact.Value
		}
	}
	if len(metadata) == 0 {
		return errors.New("Responses compiler 缺少 client_metadata 身份事实")
	}
	metadataRaw, err := json.Marshal(metadata)
	if err != nil {
		return err
	}
	document.set("client_metadata", metadataRaw)
	return nil
}

func headerHasToken(headers http.Header, name string, token string) bool {
	token = strings.ToLower(strings.TrimSpace(token))
	for _, value := range headers.Values(name) {
		for _, part := range strings.Split(value, ",") {
			if strings.ToLower(strings.TrimSpace(part)) == token {
				return true
			}
		}
	}
	return false
}

// compilerDecodeOrderedJSONObject 只解析顶层 JSON object，并保留字段原始顺序和值字节。
// duplicate 在这里直接 fail-close，避免 map 解码静默覆盖调用方输入。
func compilerDecodeOrderedJSONObject(raw []byte) ([]orderedJSONField, error) {
	document, err := newOrderedJSONDocument(raw)
	if err != nil {
		return nil, err
	}
	return append([]orderedJSONField(nil), document.fields...), nil
}

func orderJSONBody(raw []byte, contract profilecontract.BodyContractProfile) ([]byte, error) {
	return orderJSONBodyWithPolicy(
		raw, contract, profilecontract.FeatureDefaults{}, CodexRequestConditions{},
		BodyRuntimeConditions{}, AttemptAuthenticationInput{},
	)
}

func orderJSONBodyWithPolicy(
	raw []byte,
	contract profilecontract.BodyContractProfile,
	features profilecontract.FeatureDefaults,
	requestConditions CodexRequestConditions,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
) ([]byte, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		if len(contract.Fields) == 0 {
			return raw, nil
		}
		return nil, errors.New("JSON Body 为空")
	}
	document, err := newOrderedJSONDocument(raw)
	if err != nil {
		return nil, fmt.Errorf("解析 JSON Body: %w", err)
	}
	return orderJSONDocumentWithPolicy(
		document, contract, features, requestConditions, bodyConditions, authentication,
	)
}

func orderJSONDocumentWithPolicy(
	document *orderedJSONDocument,
	contract profilecontract.BodyContractProfile,
	features profilecontract.FeatureDefaults,
	requestConditions CodexRequestConditions,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
) ([]byte, error) {
	if document == nil || !document.duplicatesChecked {
		return nil, errors.New("JSON Body document 未完成 duplicate 校验")
	}
	known := make(map[string]bool, len(contract.Fields))
	for _, field := range contract.Fields {
		known[field.Name] = true
		value, present := document.value(field.Name)
		enabled, conditionErr := codexBodyFieldConditionEnabled(
			field.Condition, features, requestConditions, bodyConditions, authentication,
		)
		if conditionErr != nil {
			return nil, fmt.Errorf("JSON Body 字段 %s：%w", field.Name, conditionErr)
		}
		if !enabled {
			if present {
				return nil, fmt.Errorf("JSON Body 条件字段未启用: %s", field.Name)
			}
			continue
		}
		if present {
			omit, omitErr := shouldOmitJSONBodyField(field, value, bodyConditions)
			if omitErr != nil {
				return nil, omitErr
			}
			if omit {
				document.omit(field.Name)
				present = false
			}
		}
		if field.Required && !present {
			return nil, fmt.Errorf("JSON Body 缺少必需字段: %s", field.Name)
		}
		if field.Required && bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return nil, fmt.Errorf("JSON Body 必需字段不得为 null: %s", field.Name)
		}
	}
	inputNames := document.namesInSourceOrder()
	for _, name := range inputNames {
		if !known[name] {
			if contract.Closed {
				return nil, fmt.Errorf("JSON Body 存在闭集外字段: %s", name)
			}
		}
	}
	ordered := make([]string, 0, len(inputNames))
	added := make(map[string]bool, len(inputNames))
	for _, field := range contract.Fields {
		if _, present := document.value(field.Name); present {
			ordered = append(ordered, field.Name)
			added[field.Name] = true
		}
	}
	// 开放 WS event 的未知字段按原输入相对顺序追加；不得使用 map 迭代或排序。
	for _, name := range inputNames {
		if !added[name] {
			if _, present := document.value(name); present {
				ordered = append(ordered, name)
				added[name] = true
			}
		}
	}
	return document.encodeNames(ordered), nil
}

func codexBodyFieldConditionEnabled(
	condition profilecontract.ConditionKind,
	features profilecontract.FeatureDefaults,
	requestConditions CodexRequestConditions,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
) (bool, error) {
	switch condition {
	case profilecontract.ConditionUnconditional, profilecontract.ConditionAlways:
		return true, nil
	case profilecontract.ConditionCreditIdPresent:
		return bodyConditions.CreditIDPresent, nil
	case profilecontract.ConditionAuto:
		return false, nil
	case profilecontract.ConditionAttestationPresent,
		profilecontract.ConditionBetaFeaturesPresent,
		profilecontract.ConditionCookiePresent,
		profilecontract.ConditionFedrampAccount,
		profilecontract.ConditionManagedResidencyPresent,
		profilecontract.ConditionMemoryGeneration,
		profilecontract.ConditionParentThreadPresent,
		profilecontract.ConditionRemoteCompactionV2,
		profilecontract.ConditionRequestCompressionEnabled,
		profilecontract.ConditionResponsesLite,
		profilecontract.ConditionRuntimeMetrics,
		profilecontract.ConditionSessionIdPresent,
		profilecontract.ConditionSubagentPresent,
		profilecontract.ConditionTurnStatePresent:
		return codexHeaderConditionEnabled(
			condition, features, requestConditions, authentication,
		), nil
	default:
		return false, fmt.Errorf("不支持的 Body Condition: %s", condition)
	}
}

func shouldOmitJSONBodyField(
	field profilecontract.BodyFieldProfile,
	value json.RawMessage,
	conditions BodyRuntimeConditions,
) (bool, error) {
	trimmed := bytes.TrimSpace(value)
	switch field.OmitWhen {
	case profilecontract.OmitNever:
		return false, nil
	case profilecontract.OmitEmptyString:
		return bytes.Equal(trimmed, []byte(`""`)), nil
	case profilecontract.OmitNone:
		return bytes.Equal(trimmed, []byte("null")), nil
	case profilecontract.OmitNoneOrUnreusablePrefix:
		if field.Name != "previous_response_id" {
			return false, fmt.Errorf("Body 字段 %s 非法使用 none_or_unreusable_prefix", field.Name)
		}
		return bytes.Equal(trimmed, []byte("null")) ||
			!conditions.PreviousResponseIDReusable, nil
	default:
		return false, fmt.Errorf("不支持的 Body OmitWhen: %s", field.OmitWhen)
	}
}

func orderFormBody(
	raw []byte,
	contract profilecontract.BodyContractProfile,
	features profilecontract.FeatureDefaults,
	requestConditions CodexRequestConditions,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
) ([]byte, error) {
	values, err := url.ParseQuery(string(raw))
	if err != nil {
		return nil, fmt.Errorf("解析 form Body: %w", err)
	}
	known := make(map[string]bool, len(contract.Fields))
	parts := make([]string, 0, len(contract.Fields))
	for _, field := range contract.Fields {
		known[field.Name] = true
		fieldValues, exists := values[field.Name]
		enabled, conditionErr := codexBodyFieldConditionEnabled(
			field.Condition, features, requestConditions, bodyConditions, authentication,
		)
		if conditionErr != nil {
			return nil, fmt.Errorf("form Body 字段 %s：%w", field.Name, conditionErr)
		}
		if !enabled {
			if exists {
				return nil, fmt.Errorf("form Body 条件字段未启用: %s", field.Name)
			}
			continue
		}
		if !exists {
			if field.Required {
				return nil, fmt.Errorf("form Body 缺少必需字段: %s", field.Name)
			}
			continue
		}
		for _, value := range fieldValues {
			if field.OmitWhen == profilecontract.OmitEmptyString && value == "" {
				continue
			}
			if field.OmitWhen != profilecontract.OmitNever &&
				field.OmitWhen != profilecontract.OmitEmptyString {
				return nil, fmt.Errorf("form Body 字段 %s 不支持 OmitWhen %s", field.Name, field.OmitWhen)
			}
			parts = append(parts, url.QueryEscape(field.Name)+"="+url.QueryEscape(value))
		}
	}
	if contract.Closed {
		for name := range values {
			if !known[name] {
				return nil, fmt.Errorf("form Body 存在闭集外字段: %s", name)
			}
		}
	}
	return []byte(strings.Join(parts, "&")), nil
}

func compileConnectionIdentity(
	ctx context.Context,
	bundle ReleaseBundle,
	endpoint ResolvedEndpointPlan,
	target *url.URL,
	identityFacts CodexIdentityFacts,
	invocationID string,
) (ConnectionIdentity, string, error) {
	if target == nil {
		return ConnectionIdentity{}, "", errors.New("连接 target 为空")
	}
	authority, err := normalizeValidatedAuthority(target)
	if err != nil {
		return ConnectionIdentity{}, "", err
	}
	accountIdentity := strings.TrimSpace(identityFacts.AccountIdentityProjection.Value)
	if accountIdentity == "" {
		return ConnectionIdentity{}, "", errors.New("连接资源缺少本地账号身份投影")
	}
	policy := endpoint.template.endpoint.ResourceLifecycle
	attemptOrdinal := uint32(0)
	if metadata, ok := attemptMetadataFromContext(ctx); ok {
		attemptOrdinal = metadata.AttemptOrdinal
	}
	lifecycleScopeIdentity, err := resourceLifecycleScopeIdentity(
		policy, invocationID, attemptOrdinal, accountIdentity, authority,
	)
	if err != nil {
		return ConnectionIdentity{}, "", err
	}
	deployment := bundle.Deployment()
	tlsIdentityRaw, err := json.Marshal(endpoint.template.transport)
	if err != nil {
		return ConnectionIdentity{}, "", err
	}
	tlsIdentitySum := sha256.Sum256(tlsIdentityRaw)
	caIdentity := strings.TrimSpace(deployment.CustomCAContentDigest)
	if caIdentity == "" {
		caIdentity = "system_roots"
	}
	raw, err := json.Marshal(struct {
		ReleaseDigest          string
		ExecutableProfile      string
		BundleDigest           string
		LocalAccountIdentity   string
		TargetAuthority        string
		Backend                BackendKind
		Protocol               WireProtocol
		Adapter                AdapterID
		TransportID            string
		TLSIdentityDigest      string
		ConnectionGroup        string
		Platform               string
		ProxyMode              string
		ProxyIdentityDigest    string
		CustomCAContentDigest  string
		Lifecycle              profilecontract.ResourceLifecyclePolicy
		LifecycleScopeIdentity string
	}{
		ReleaseDigest:     bundle.ReleaseDigest(),
		ExecutableProfile: bundle.release.ExecutableProfileDigest(),
		BundleDigest:      bundle.BundleDigest(), LocalAccountIdentity: accountIdentity,
		TargetAuthority: authority, Backend: endpoint.template.backend,
		Protocol: endpoint.Protocol(), Adapter: endpoint.template.adapter,
		TransportID:       endpoint.template.transport.ID,
		TLSIdentityDigest: hex.EncodeToString(tlsIdentitySum[:]),
		ConnectionGroup:   endpoint.template.connectionGroup,
		Platform:          deployment.Platform, ProxyMode: deployment.ProxyMode,
		ProxyIdentityDigest:   deployment.ProxyIdentityDigest,
		CustomCAContentDigest: caIdentity,
		Lifecycle:             policy, LifecycleScopeIdentity: lifecycleScopeIdentity,
	})
	if err != nil {
		return ConnectionIdentity{}, "", err
	}
	connectionSum := sha256.Sum256(raw)
	connectionDigest := hex.EncodeToString(connectionSum[:])
	poolRaw, err := json.Marshal(struct {
		Domain           string
		ConnectionDigest string
	}{Domain: "sub2api.connection-pool.v1", ConnectionDigest: connectionDigest})
	if err != nil {
		return ConnectionIdentity{}, "", err
	}
	poolSum := sha256.Sum256(poolRaw)
	return ConnectionIdentity{digest: connectionDigest}, hex.EncodeToString(poolSum[:]), nil
}

func resourceLifecycleScopeIdentity(
	policy profilecontract.ResourceLifecyclePolicy,
	invocationID string,
	attemptOrdinal uint32,
	accountIdentity string,
	authority string,
) (string, error) {
	invocationID = strings.TrimSpace(invocationID)
	if invocationID == "" {
		return "", errors.New("资源生命周期缺少 InvocationID")
	}
	switch policy.Scope {
	case profilecontract.ResourceScopeInvocation:
		return "invocation:" + invocationID, nil
	case profilecontract.ResourceScopeInvocationAttempt:
		if attemptOrdinal == 0 {
			return "", errors.New("attempt 资源生命周期缺少 AttemptOrdinal")
		}
		return fmt.Sprintf("invocation:%s:attempt:%d", invocationID, attemptOrdinal), nil
	case profilecontract.ResourceScopeAccountTransport:
		if !policy.RetryReusesClient {
			if attemptOrdinal == 0 {
				return "", errors.New("retry 隔离资源缺少 AttemptOrdinal")
			}
			return fmt.Sprintf("account:%s:invocation:%s:attempt:%d", accountIdentity, invocationID, attemptOrdinal), nil
		}
		return "account:" + accountIdentity, nil
	case profilecontract.ResourceScopeWebSocketConnection:
		if !policy.RetryReusesClient {
			if attemptOrdinal == 0 {
				return "", errors.New("WebSocket retry 隔离缺少 AttemptOrdinal")
			}
			return fmt.Sprintf("websocket:%s:attempt:%d", invocationID, attemptOrdinal), nil
		}
		return "websocket-invocation:" + invocationID, nil
	case profilecontract.ResourceScopeReturnedAuthority:
		base := "upload:" + invocationID + ":authority:" + authority
		if !policy.RetryReusesClient {
			if attemptOrdinal == 0 {
				return "", errors.New("upload retry 隔离缺少 AttemptOrdinal")
			}
			base += fmt.Sprintf(":attempt:%d", attemptOrdinal)
		}
		return base, nil
	default:
		return "", fmt.Errorf("未知资源生命周期作用域: %s", policy.Scope)
	}
}

func digestCompiledExecution(
	request CompiledRequest,
	endpoint ResolvedEndpointPlan,
	transport TransportSpec,
	releaseDigest string,
	bundleDigest string,
	connection ConnectionIdentity,
) (string, error) {
	target := request.URL()
	bodyDigest := "single-use"
	if body, ok := request.body.replayableView(); ok {
		sum := sha256.Sum256(body)
		bodyDigest = hex.EncodeToString(sum[:])
	}
	raw, err := json.Marshal(struct {
		Method           string
		URL              string
		Headers          http.Header
		BodyDigest       string
		Binding          EndpointBindingKey
		Transport        TransportSpec
		ReleaseDigest    string
		BundleDigest     string
		ConnectionDigest string
	}{
		Method: request.Method(), URL: target.String(), Headers: request.Headers(),
		BodyDigest: bodyDigest, Binding: endpoint.template.binding.Key(),
		Transport: transport, ReleaseDigest: releaseDigest, BundleDigest: bundleDigest,
		ConnectionDigest: connection.Digest(),
	})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}
