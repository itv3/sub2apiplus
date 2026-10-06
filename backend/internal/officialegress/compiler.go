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
	HostedFileUploadPresent    bool
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
		bundle.release.ExecutableProfile().Optional(),
		headers,
		plan.Body,
		plan.BodyPolicy.Conditions,
		authentication,
		plan.IdentityFacts,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	request, err := NewCompiledRequest(plan.Method, target, headers, body)
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
	if !plan.RoutingHint.IsZero() {
		if err := plan.RoutingHint.Validate(); err != nil {
			return err
		}
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
			plan.IdentityFacts, plan.RoutingHint, authentication,
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
	case profilecontract.ConditionLunaReservePresent:
		return conditions.LunaReservePresent
	case profilecontract.ConditionGuardianReviewRequest:
		// guardian 同步审阅请求：只由 service 边界已验证的请求条件决定，
		// 与 feature 默认值和认证材料无关。
		return conditions.GuardianReviewRequest
	case profilecontract.ConditionNotGuardianReviewRequest:
		// 与上一条严格互补：画像用它表达“仅普通请求才有”的槽位（routing hint、
		// service_tier、guardian_credits_requested 等）。
		return !conditions.GuardianReviewRequest
	case profilecontract.ConditionAccountRoutingOverridePresent:
		// 首期非默认工作区路由失败关闭，service 永不置位；这里只按事实求值，
		// 不做任何推断。
		return conditions.AccountRoutingOverridePresent
	default:
		return false
	}
}

func codexHeaderValue(
	slot profilecontract.HeaderSlotProfile,
	userAgent string,
	originator string,
	facts CodexIdentityFacts,
	routingHint CodexRoutingHintFacts,
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
	case profilecontract.SourceRequestBody:
		if name == "content-length" {
			return "", true, nil
		}
		if name == "x-codex-routing-hint" {
			value, err := routingHint.HeaderValue()
			return value, false, err
		}
		return "", false, errors.New("未知 request_body Header")
	case profilecontract.SourceGenerated:
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
	case profilecontract.SourcePromptCacheKey:
		switch name {
		case "session-id", "x-session-id":
			return codexResponsesSessionHeaderValue(facts), false, nil
		}
	}
	return "", false, errors.New("结构化事实未覆盖 ProfileSpec Header source")
}

// codexResponsesSessionHeaderValue 是 session-id 头在 prompt_cache_key 来源下的取值：
// 根会话取 prompt cache 亲和键（通常等于 SessionID，临时 fork 时为源会话键），
// 子代理与内部会话（x-openai-subagent 存在）仍取本会话真实 SessionID，
// 不能取带 guardian: 等前缀的 prompt cache 键。
func codexResponsesSessionHeaderValue(facts CodexIdentityFacts) string {
	if facts.Subagent.present() {
		return facts.SessionID.Value
	}
	value, _ := codexPromptCacheKeyValue(facts, true)
	return value
}

// codexPromptCacheKeyValue 返回 compiler 独占的 prompt_cache_key 取值。
//
// useFact 只在端点画像声明了 prompt_cache_key 来源时为 true：此时以 service 边界已
// 验证的 PromptCacheKey 事实为准（支持临时 fork 的源会话键）；否则保持旧逻辑，
// 按 SessionID 推导（guardian 子代理为 guardian:<parent>），旧画像出站字节不变。
func codexPromptCacheKeyValue(facts CodexIdentityFacts, useFact bool) (string, bool) {
	if useFact && facts.PromptCacheKey.present() {
		return facts.PromptCacheKey.Value, true
	}
	if !facts.SessionID.present() {
		return "", false
	}
	if facts.Subagent.Value == "guardian" && facts.ParentThreadID.present() {
		return "guardian:" + facts.ParentThreadID.Value, true
	}
	return facts.SessionID.Value, true
}

// endpointDeclaresValueSource 判断端点画像是否有 Header 槽位使用指定取值来源。
func endpointDeclaresValueSource(
	endpoint profilecontract.ExecutableEndpointProfile,
	source profilecontract.ValueSource,
) bool {
	for _, slot := range endpoint.Headers {
		if slot.Source == source {
			return true
		}
	}
	return false
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
	optional profilecontract.OptionalSections,
	headers http.Header,
	body RequestBody,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
	identityFacts CodexIdentityFacts,
) (RequestBody, error) {
	if body.Mode() == RequestBodySingleUse {
		if endpoint.Body.Encoding != profilecontract.BodyRawBytes {
			return RequestBody{}, errors.New("只有 raw_bytes endpoint 可以使用 single-use Body")
		}
		return body.clone(), nil
	}
	// 按顶层成员装配且已建好 document 的 Body（Finalizer 按成员交接的终态正文）不物化整段
	// 正文：顶层对象至少写成 "{}"，一定非空白；JSON 端点直接按 document 定型，只有需要整段
	// 字节的编码分支才物化。其余 Body 与过去一样先取整段只读视图。
	membersDocument := body.jsonDocument() != nil && body.jsonObjectMembers() != nil
	var raw []byte
	blank := false
	if !membersDocument {
		view, ok := body.replayableView()
		if !ok {
			return RequestBody{}, errors.New("replayable Body 无法读取")
		}
		raw = view
		blank = len(bytes.TrimSpace(raw)) == 0
	}
	wholeBody := func() []byte {
		if membersDocument {
			view, _ := body.replayableView()
			return view
		}
		return raw
	}
	compressed := headerHasToken(headers, "Content-Encoding", "zstd")
	if compressed && endpoint.Compression != profilecontract.CompressionZstdWhenFeatureEnabled {
		return RequestBody{}, errors.New("端点画像不允许 zstd 请求体")
	}
	if endpoint.Upgrade != "" && blank {
		return newOwnedReplayableRequestBody(raw), nil
	}
	if endpoint.ID != "oauth_refresh" && authentication.RefreshToken != "" {
		return RequestBody{}, errors.New("非 OAuth refresh Body 禁止 RefreshToken")
	}
	var compiled []byte
	var err error
	switch endpoint.Body.Encoding {
	case profilecontract.BodyNone, profilecontract.BodyRawBytes:
		compiled = wholeBody()
	case profilecontract.BodyJson, profilecontract.BodyWebsocketJson,
		profilecontract.BodyWebsocketDiscriminatedEvents:
		if blank {
			if len(endpoint.Body.Fields) == 0 {
				compiled = raw
				break
			}
			return RequestBody{}, errors.New("JSON Body 为空")
		}
		document := body.jsonDocument()
		if document == nil {
			document, err = newOrderedJSONDocument(raw)
			if err != nil {
				return RequestBody{}, fmt.Errorf("解析 JSON Body: %w", err)
			}
		}
		if err = injectCompilerOwnedBodyFields(
			endpoint, document, authentication, identityFacts,
			codexClientMetadataConstants{
				section: optional.ClientMetadata, features: features, authentication: authentication,
			},
		); err != nil {
			return RequestBody{}, err
		}
		ordered, orderErr := orderedJSONNamesWithPolicy(
			document, endpoint.Body, features, identityFacts.Conditions,
			bodyConditions, authentication,
		)
		if orderErr != nil {
			return RequestBody{}, orderErr
		}
		if compressed {
			if !features.EnableRequestCompression {
				return RequestBody{}, errors.New("Bundle feature 禁止请求压缩")
			}
			return compressRequestBodyZstd(
				features.RequestCompressionLevel, int64(document.encodedNamesLength(ordered)),
				func(writer io.Writer) error { return document.writeNames(writer, ordered) },
			)
		}
		compiled = document.encodeNames(ordered)
	case profilecontract.BodyFormUrlencoded:
		compiled, err = orderFormBody(
			wholeBody(), endpoint.Body, features, identityFacts.Conditions,
			bodyConditions, authentication,
		)
	default:
		return RequestBody{}, fmt.Errorf("compiler 不支持 Body encoding: %s", endpoint.Body.Encoding)
	}
	if err != nil {
		return RequestBody{}, err
	}
	if !compressed {
		return newOwnedReplayableRequestBody(compiled), nil
	}
	if !features.EnableRequestCompression {
		return RequestBody{}, errors.New("Bundle feature 禁止请求压缩")
	}
	return compressRequestBodyZstd(
		features.RequestCompressionLevel, int64(len(compiled)),
		func(writer io.Writer) error { return writeJSONDocumentPart(writer, compiled) },
	)
}

// requestBodyZstdWindowSize 把正式流式压缩的历史窗口限制为 512KiB。
// 固定正文测量相较 2MiB 窗口再减少约 1.5MiB 分配；压缩等级仍由画像决定。
// 窗口变化允许压缩字节改变，但必须保留单帧、准确 FCS、校验和、字典设置及原文字节。
const requestBodyZstdWindowSize = 512 << 10

// compressRequestBodyZstd 把正文逐段写成单个 zstd 帧，并保留可重放的分段输出。
// 正式 JSON 路径由 document 提供字段区间，不生成完整的未压缩出站正文；压缩结果也不
// 拼成连续切片。压缩仅执行一次，关闭编码器后即可得到准确 Content-Length。
//
// 压缩等级保持画像既有值，低内存选项与有界窗口共同降低编码器内部缓冲占用。
// ResetContentSize 保留准确的原文长度；多块流式编码的帧组织可能不同于 EncodeAll，
// 但解压后的字节、校验和开关和字典设置必须保持一致。
func compressRequestBodyZstd(level int, contentLength int64, writeBody func(io.Writer) error) (RequestBody, error) {
	return compressRequestBodyZstdWithCache(level, contentLength, writeBody, sharedRequestBodyZstdEncoderCache)
}

// compressRequestBodyZstdWithCache 的 nil cache 路径保留新建编码器对照。
// 只有完整写入、长度校验及 Close 均成功的编码器可以归还缓存。
func compressRequestBodyZstdWithCache(
	level int,
	contentLength int64,
	writeBody func(io.Writer) error,
	cache *requestBodyZstdEncoderCache,
) (RequestBody, error) {
	output := newSegmentedBodyWriter()
	encoder, err := cache.take(level)
	if err != nil {
		return RequestBody{}, fmt.Errorf("创建 zstd 编码器: %w", err)
	}
	encoder.ResetContentSize(output, contentLength)
	if err := writeBody(encoder); err != nil {
		_ = encoder.Close()
		return RequestBody{}, fmt.Errorf("写入 zstd 正文: %w", err)
	}
	if err := encoder.Close(); err != nil {
		return RequestBody{}, fmt.Errorf("关闭 zstd 编码器: %w", err)
	}
	cache.put(level, encoder)
	return output.finish(), nil
}

// compressCompiledBodyZstd 保留旧 EncodeAll 编码方式作为兼容与差分测量基准；正式编译路径
// 使用 compressRequestBodyZstd，避免物化完整 JSON 和连续压缩输出。
//
// 两处分配与输出无关，按最小占用设置：
//   - EncodeAll 每次调用只占用一个编码器，缺省并发度却会在初始化时按 GOMAXPROCS 预建同样数量
//     的编码器状态（每个约 1 MiB 匹配表），这里固定为 1；并发度只决定编码器池大小。
//   - 输出缓冲按经验比例一次预留（zstdOutputReservation）：缺省从小容量起按约 1.25 倍反复扩容，
//     大正文累计分配约五倍压缩结果；按最坏长度预留则让一份与正文等大的缓冲在压缩期间常驻。
//     EncodeAll 只向 dst 追加，预留容量不改变输出字节。
//
// 输出与缺省参数的新建编码器逐字节一致，由差分测试锁定。
func compressCompiledBodyZstd(level int, compiled []byte) ([]byte, error) {
	encoder, err := zstd.NewWriter(
		nil,
		zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(level)),
		zstd.WithEncoderConcurrency(1),
	)
	if err != nil {
		return nil, fmt.Errorf("创建 zstd 编码器: %w", err)
	}
	var dst []byte
	if len(compiled) > 0 {
		// 空正文保持 nil 目标缓冲，与过去 EncodeAll(nil, nil) 的返回值完全相同。
		dst = make([]byte, 0, zstdOutputReservation(
			encoder.MaxEncodedSize(len(compiled)), len(compiled), zstdLikelyIncompressibleBytes(compiled),
		))
	}
	out := encoder.EncodeAll(compiled, dst)
	if err := encoder.Close(); err != nil {
		return nil, fmt.Errorf("关闭 zstd 编码器: %w", err)
	}
	return out, nil
}

// zstdOutputReserveRatioPercent 是压缩输出缓冲按最坏长度（MaxEncodedSize）预留的基准百分比（问题四 M3-b）。
//
// 取舍：
//   - 过去按最坏长度一次预留，压缩期间一份与正文等大的缓冲常驻；可压缩的正文实际只用到其中六到七成。
//   - 此兼容基准保持 EncodeAll 的原有压缩字节，因此不使用正式路径的流式输出，也不复用生命周期
//     不明确的大缓冲或进行两遍压缩。压缩前的容量估计只影响容量，不影响输出字节。
//   - 基准取 81%：Go 对大切片扩容一次至少放大到约 1.25 倍，81% × 1.25 > 100%，因此无论估计如何，
//     EncodeAll 追加过程中最多扩容一次（扩容瞬间新旧两块同时存活，比按最坏长度预留多约 0.8 倍正文）。
//   - 该编码器（等级 3）的实测规律是两极的：base64 片段与足量可匹配的文本交错时，字面量被熵编码，
//     整体压缩比约 0.6～0.75；一个块里几乎只有 base64（图片 data URL，或加密推理内容背靠背、之间几乎
//     没有文本）时块按原样存储，压缩比约为 1。后一类若按 81% 预留必然扩容一次，所以先用
//     zstdLikelyIncompressibleBytes 廉价识别这类内容，按原长计入预留，其余按 81%：可压缩正文预留少约
//     两成且不扩容，按原样存储为主的正文预留接近最坏长度、与过去相同；识别不准时至多扩容一次。
const zstdOutputReserveRatioPercent = 81

const (
	// zstdBase64StringMinBytes 以上、抽样几乎全是 base64 字符的 JSON 字符串内容视为 base64 长串。
	zstdBase64StringMinBytes = 4 << 10
	// zstdIncompressibleStringMinBytes 以上的 base64 长串无论周围内容如何都按原样存储计。
	zstdIncompressibleStringMinBytes = 64 << 10
	// zstdMatchableContentMinPercent：base64 长串之外的内容不足正文的这一比例时，视为块内没有可匹配的
	// 内容，整段按原样存储计。实测每 13 KB base64 之间约有 500 字节文本（约 4%）时块已被熵编码。
	zstdMatchableContentMinPercent = 2
	// zstdRawBlockBytes 是编码器的块大小（等级 3 缺省 128 KiB），用作超长串两端边界块的余量。
	zstdRawBlockBytes = 128 << 10
)

// zstdOutputReservation 返回压缩输出缓冲的预留容量：帧头与每块封装开销（最坏长度减输入长度）与
// likelyIncompressible 字节按原长计入，其余输入按 81% 计入，不超过最坏长度。结果不低于最坏长度的 81%，
// 因此最多扩容一次。极小正文至少预留 64 字节（不超过最坏长度），避免为几十字节的输出再扩容。
func zstdOutputReservation(maxEncodedSize int, inputLen int, likelyIncompressible int) int {
	inputLen = min(max(inputLen, 0), maxEncodedSize)
	likelyIncompressible = min(max(likelyIncompressible, 0), inputLen)
	rest := inputLen - likelyIncompressible
	reservation := maxEncodedSize - inputLen + likelyIncompressible +
		rest/100*zstdOutputReserveRatioPercent + rest%100*zstdOutputReserveRatioPercent/100
	if reservation < 64 {
		reservation = min(64, maxEncodedSize)
	}
	return min(reservation, maxEncodedSize)
}

// zstdLikelyIncompressibleBytes 粗估定型正文里会被按原样存储的字节数，只用于预留容量。原样存储以块
// （128 KiB）为单位，块内的 JSON 结构也一并按原样存储：
//   - base64 长串（不短于 zstdBase64StringMinBytes）之外的内容不足正文 zstdMatchableContentMinPercent% 时，
//     几乎每个块都没有可匹配的内容，整段按原样计；
//   - 否则每个超长 base64 串（不短于 zstdIncompressibleStringMinBytes）按原长计，另加两端各一个块的余量，
//     覆盖边界块也被原样存储的情况；其余内容视为可压缩。
//
// 只用 bytes.IndexByte 在引号间跳跃（转义引号按前导反斜杠个数识别），对长串只做抽样检查，开销远小于
// 压缩本身。
func zstdLikelyIncompressibleBytes(compiled []byte) int {
	long, longCount, medium := 0, 0, 0
	for position := 0; position < len(compiled); {
		opening := bytes.IndexByte(compiled[position:], '"')
		if opening < 0 {
			break
		}
		start := position + opening + 1
		end := start
		for {
			closing := bytes.IndexByte(compiled[end:], '"')
			if closing < 0 {
				end = -1
				break
			}
			end += closing
			backslashes := 0
			for i := end - 1; i >= start && compiled[i] == '\\'; i-- {
				backslashes++
			}
			if backslashes%2 == 0 {
				break
			}
			end++
		}
		if end < 0 {
			break
		}
		if content := compiled[start:end]; len(content) >= zstdBase64StringMinBytes && zstdLooksBase64(content) {
			if len(content) >= zstdIncompressibleStringMinBytes {
				long += len(content)
				longCount++
			} else {
				medium += len(content)
			}
		}
		position = end + 1
	}
	if matchable := len(compiled) - long - medium; matchable*100 < len(compiled)*zstdMatchableContentMinPercent {
		return len(compiled)
	}
	return min(len(compiled), long+longCount*2*zstdRawBlockBytes)
}

// zstdLooksBase64 抽样判断内容是否几乎全由 base64 字符（A-Z、a-z、0-9、+、/、=、-、_）组成：首尾各 256 字节
// 与均匀分布的 16 段各 64 字节中，非 base64 字符不超过 1%。只用于容量估计。
func zstdLooksBase64(content []byte) bool {
	isBase64 := func(c byte) bool {
		return c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' ||
			c == '+' || c == '/' || c == '=' || c == '-' || c == '_'
	}
	sampled, other := 0, 0
	inspect := func(window []byte) {
		for _, c := range window {
			sampled++
			if !isBase64(c) {
				other++
			}
		}
	}
	const edge, segment, segments = 256, 64, 16
	if len(content) <= 2*edge+segment*segments {
		inspect(content)
	} else {
		inspect(content[:edge])
		inspect(content[len(content)-edge:])
		stride := (len(content) - 2*edge) / segments
		for i := 0; i < segments; i++ {
			offset := edge + i*stride
			inspect(content[offset : offset+segment])
		}
	}
	return other*100 <= sampled
}

// codexClientMetadataConstants 是 ClientMetadata 可选节在一次编译中的求值输入。
// section 为 nil 表示当前画像没有该节，client_metadata 只由身份事实重建（旧逻辑）。
type codexClientMetadataConstants struct {
	section        *profilecontract.ClientMetadataSection
	features       profilecontract.FeatureDefaults
	authentication AttemptAuthenticationInput
}

// apply 按画像条件把常量写入 client_metadata。常量键与身份事实键冲突属于画像错误，
// 失败关闭；条件不成立的键不写入。
func (c codexClientMetadataConstants) apply(
	metadata map[string]string,
	conditions CodexRequestConditions,
) error {
	if c.section == nil {
		return nil
	}
	keys := make([]string, 0, len(c.section.Constants))
	for key := range c.section.Constants {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if _, exists := metadata[key]; exists {
			return fmt.Errorf("ClientMetadata 常量键与身份事实冲突：%s", key)
		}
		if !codexHeaderConditionEnabled(
			c.section.ConditionFor(key), c.features, conditions, c.authentication,
		) {
			continue
		}
		metadata[key] = c.section.Constants[key]
	}
	return nil
}

func injectCompilerOwnedBodyFields(
	endpoint profilecontract.ExecutableEndpointProfile,
	document *orderedJSONDocument,
	authentication AttemptAuthenticationInput,
	identityFacts CodexIdentityFacts,
	constants codexClientMetadataConstants,
) error {
	if endpoint.ID != "oauth_refresh" {
		if endpoint.ID == "responses_http" || endpoint.ID == "responses_compact" ||
			endpoint.ID == "responses_ws" {
			return injectCodexResponsesOwnedBodyFields(endpoint, document, identityFacts, constants)
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
	endpoint profilecontract.ExecutableEndpointProfile,
	document *orderedJSONDocument,
	facts CodexIdentityFacts,
	constants codexClientMetadataConstants,
) error {
	if _, present := document.value("prompt_cache_key"); present {
		return errors.New("Responses 语义 Body 禁止携带 compiler-owned prompt_cache_key")
	}
	// “先头后体”：session-id 头（prompt_cache_key 来源）与请求体 prompt_cache_key
	// 共用同一取值函数，保证两处一致。
	if promptCacheValue, ok := codexPromptCacheKeyValue(
		facts, endpointDeclaresValueSource(endpoint, profilecontract.SourcePromptCacheKey),
	); ok {
		promptCacheKey, err := json.Marshal(promptCacheValue)
		if err != nil {
			return err
		}
		document.set("prompt_cache_key", promptCacheKey)
	}
	if endpoint.ID == "responses_compact" {
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
	// 画像 ClientMetadata 节声明的客户端固定常量（如 guardian_credits_requested、
	// mcp_attribution）在身份事实之后按条件追加；画像没有该节时保持旧逻辑。
	if err := constants.apply(metadata, facts.Conditions); err != nil {
		return err
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
	ordered, err := orderedJSONNamesWithPolicy(
		document, contract, features, requestConditions, bodyConditions, authentication,
	)
	if err != nil {
		return nil, err
	}
	return document.encodeNames(ordered), nil
}

// orderedJSONNamesWithPolicy 先完成闭集、条件和省略规则校验，再返回唯一的字段线序。
// 流式与整段编码共用此处的权威校验，避免为压缩路径另建一套放宽的正文规则。
func orderedJSONNamesWithPolicy(
	document *orderedJSONDocument,
	contract profilecontract.BodyContractProfile,
	features profilecontract.FeatureDefaults,
	requestConditions CodexRequestConditions,
	bodyConditions BodyRuntimeConditions,
	authentication AttemptAuthenticationInput,
) ([]string, error) {
	if document == nil || !document.duplicatesChecked {
		return nil, errors.New("JSON Body document 未完成 duplicate 校验")
	}
	known := make(map[string]bool, len(contract.Fields))
	for _, field := range contract.Fields {
		known[field.Name] = true
		bodyField, present := document.field(field.Name)
		value := bodyField.policyValue()
		enabled, conditionErr := codexBodyFieldConditionEnabled(
			field.Condition, features, requestConditions, bodyConditions, authentication,
		)
		if conditionErr != nil {
			return nil, fmt.Errorf("JSON Body 字段 %s：%w", field.Name, conditionErr)
		}
		if !enabled {
			if present {
				if codexBodyConditionOmitsPresentField(field.Condition) {
					document.omit(field.Name)
					continue
				}
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
		if _, present := document.field(field.Name); present {
			ordered = append(ordered, field.Name)
			added[field.Name] = true
		}
	}
	// 开放 WS event 的未知字段按原输入相对顺序追加；不得使用 map 迭代或排序。
	for _, name := range inputNames {
		if !added[name] {
			if _, present := document.field(name); present {
				ordered = append(ordered, name)
				added[name] = true
			}
		}
	}
	return ordered, nil
}

// codexBodyConditionOmitsPresentField 列出“条件不成立时省略语义体中已有字段”的条件。
//
// guardian 审阅与否是请求级事实：官方客户端在构造审阅请求时自行删除 service_tier
// 等字段，而网关收到的语义体来自下游，可能照常携带这些字段。对这类条件，编译器按
// 画像省略字段，而不是像其他条件那样把“字段存在但条件未启用”当成调用方错误拒绝。
// 旧版本画像不引用这两个条件，其余条件仍保持失败关闭。
func codexBodyConditionOmitsPresentField(condition profilecontract.ConditionKind) bool {
	switch condition {
	case profilecontract.ConditionGuardianReviewRequest,
		profilecontract.ConditionNotGuardianReviewRequest:
		return true
	default:
		return false
	}
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
	case profilecontract.ConditionHostedFileUpload:
		return bodyConditions.HostedFileUploadPresent, nil
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
		profilecontract.ConditionTurnStatePresent,
		profilecontract.ConditionLunaReservePresent,
		profilecontract.ConditionGuardianReviewRequest,
		profilecontract.ConditionNotGuardianReviewRequest,
		profilecontract.ConditionAccountRoutingOverridePresent:
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
	if body, ok := request.body.openReplayable(); ok {
		hash := sha256.New()
		_, copyErr := io.Copy(hash, body)
		closeErr := body.Close()
		if copyErr != nil {
			return "", fmt.Errorf("读取编译正文摘要: %w", copyErr)
		}
		if closeErr != nil {
			return "", fmt.Errorf("关闭编译正文摘要: %w", closeErr)
		}
		bodyDigest = hex.EncodeToString(hash.Sum(nil))
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
