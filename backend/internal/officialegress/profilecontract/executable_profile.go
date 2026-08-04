package profilecontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
)

// ResourceScopeKind 是编译器批准的资源作用域闭集。执行器只消费该结果，
// 不再自行解释 ClientLifecycle 与两个连接复用布尔值。
type ResourceScopeKind string

const (
	ResourceScopeInvocation          ResourceScopeKind = "invocation"
	ResourceScopeInvocationAttempt   ResourceScopeKind = "invocation_attempt"
	ResourceScopeAccountTransport    ResourceScopeKind = "account_transport"
	ResourceScopeWebSocketConnection ResourceScopeKind = "websocket_connection"
	ResourceScopeReturnedAuthority   ResourceScopeKind = "invocation_returned_authority"
)

// ResourceLifecyclePolicy 是生命周期三个证据字段的唯一可执行投影。
// Lifecycle 保留已批准的语义名称；其余字段均是编译结果而非独立运行时输入。
type ResourceLifecyclePolicy struct {
	Lifecycle         LifecycleKind
	Scope             ResourceScopeKind
	RetryReusesClient bool
}

// Validate 强制生命周期、资源作用域和 retry 语义只能形成批准的真值表。
func (p ResourceLifecyclePolicy) Validate() error {
	switch p.Lifecycle {
	case LifecyclePerUpperApiCall:
		if p.RetryReusesClient && p.Scope == ResourceScopeInvocation {
			return nil
		}
		if !p.RetryReusesClient && p.Scope == ResourceScopeInvocationAttempt {
			return nil
		}
	case LifecycleBackendClientLongLived:
		if p.Scope == ResourceScopeAccountTransport {
			return nil
		}
	case LifecycleWebsocketConnection:
		if p.Scope == ResourceScopeWebSocketConnection {
			return nil
		}
	case LifecycleReturnedUploadUrlCall:
		if p.Scope == ResourceScopeReturnedAuthority {
			return nil
		}
	}
	return fmt.Errorf("资源生命周期策略组合未获批准: lifecycle=%s scope=%s retry_reuses_client=%t",
		p.Lifecycle, p.Scope, p.RetryReusesClient)
}

// ExecutableEndpointProfile 仅包含已有明确执行语义的端点字段。
type ExecutableEndpointProfile struct {
	ID                      string
	Method                  string
	Upgrade                 string
	TransportID             string
	Host                    string
	HostFromResponse        bool
	Path                    string
	Query                   []QueryFieldProfile
	Accept                  string
	ContentType             string
	Compression             CompressionKind
	ResourceLifecycle       ResourceLifecyclePolicy
	HeaderOrderMode         HeaderOrderKind
	Headers                 []HeaderSlotProfile
	HeaderMapInsertionOrder []string
	PostRemoveHeaders       []string
	Body                    BodyContractProfile
}

func (e ExecutableEndpointProfile) clone() ExecutableEndpointProfile {
	out := e
	out.Query = append([]QueryFieldProfile(nil), e.Query...)
	out.Headers = append([]HeaderSlotProfile(nil), e.Headers...)
	out.HeaderMapInsertionOrder = append([]string(nil), e.HeaderMapInsertionOrder...)
	out.PostRemoveHeaders = append([]string(nil), e.PostRemoveHeaders...)
	out.Body.Fields = append([]BodyFieldProfile(nil), e.Body.Fields...)
	return out
}

// ExecutableTransportProfile 不再暴露生命周期布尔值；它们已被编译进每个端点的
// ResourceLifecyclePolicy。
type ExecutableTransportProfile struct {
	ID                   string
	Protocol             string
	PlatformCondition    string
	TLSStack             string
	CipherSuites         []uint16
	SupportedGroups      []uint16
	SignatureAlgorithms  []uint16
	ALPN                 []string
	Extensions           []uint16
	RandomizeExtensions  bool
	SupportedVersions    []uint16
	KeyShareGroups       []uint16
	PSKModes             []uint16
	TLSMinVersion        uint16
	TLSMaxVersion        uint16
	LowercaseHTTPHeaders bool
	WebSocket            *WSTransportProfile
}

func (t ExecutableTransportProfile) clone() ExecutableTransportProfile {
	out := t
	out.CipherSuites = append([]uint16(nil), t.CipherSuites...)
	out.SupportedGroups = append([]uint16(nil), t.SupportedGroups...)
	out.SignatureAlgorithms = append([]uint16(nil), t.SignatureAlgorithms...)
	out.ALPN = append([]string(nil), t.ALPN...)
	out.Extensions = append([]uint16(nil), t.Extensions...)
	out.SupportedVersions = append([]uint16(nil), t.SupportedVersions...)
	out.KeyShareGroups = append([]uint16(nil), t.KeyShareGroups...)
	out.PSKModes = append([]uint16(nil), t.PSKModes...)
	if t.WebSocket != nil {
		ws := *t.WebSocket
		ws.FixedHandshakePrefix = append([]string(nil), t.WebSocket.FixedHandshakePrefix...)
		out.WebSocket = &ws
	}
	return out
}

type SurfaceProfile struct {
	ID                      string
	Product                 string
	Version                 string
	PlatformPrefix          string
	DefaultTerminalToken    string
	TerminalTokenPattern    string
	SuffixName              string
	SuffixVersion           string
	SuffixOptional          bool
	InitialModelsMayOmit    bool
	Originator              string
	InitialModelsOriginator string
}

type ToolPresentationProfile struct {
	EndpointIDs                  []string
	HostedImageGenerationAllowed bool
	HostedImageGenerationType    string
	NamespaceType                string
	NamespaceName                string
	FunctionType                 string
	FunctionName                 string
	LiteCarrierItemType          string
	NamespaceRequiredFields      []string
	FunctionRequiredFields       []string
}

type SubagentProfile struct {
	Mappings              []SubagentMapping
	OtherLabelAllowed     bool
	OtherThreadSource     string
	OtherHeaderEqualsKind bool
}

type SubagentMapping struct {
	ID                   string
	HeaderValue          string
	MetadataKind         string
	ThreadSource         string
	MemoryGeneration     bool
	ParentThreadRequired bool
}

type FilesProfile struct {
	CreateEndpointID         string
	BlobUploadEndpointID     string
	UploadedEndpointID       string
	UploadLimitBytes         uint64
	RequestTimeoutMillis     int64
	FinalizeTimeoutMillis    int64
	FinalizeRetryDelayMillis int64
	UseCase                  string
	URIPrefix                string
	FinalizeSuccessStatus    string
	FinalizeRetryStatus      string
}

// ExecutableProfile 是启动期编译后的唯一执行画像。它不保存 SnapshotDoc 原文、
// RequiredRules 或官方摘要，因此证据专用叶子不会污染 executable digest。
type ExecutableProfile struct {
	version          string
	endpoints        []ExecutableEndpointProfile
	transports       []ExecutableTransportProfile
	features         FeatureDefaults
	surfaces         []SurfaceProfile
	toolPresentation ToolPresentationProfile
	subagents        SubagentProfile
	files            FilesProfile
	digest           string
}

func (p ExecutableProfile) Version() string { return p.version }
func (p ExecutableProfile) Digest() string  { return p.digest }

func (p ExecutableProfile) Endpoints() []ExecutableEndpointProfile {
	out := make([]ExecutableEndpointProfile, len(p.endpoints))
	for i, endpoint := range p.endpoints {
		out[i] = endpoint.clone()
	}
	return out
}

func (p ExecutableProfile) Transports() []ExecutableTransportProfile {
	out := make([]ExecutableTransportProfile, len(p.transports))
	for i, transport := range p.transports {
		out[i] = transport.clone()
	}
	return out
}

func (p ExecutableProfile) Features() FeatureDefaults { return p.features }

func (p ExecutableProfile) Surfaces() []SurfaceProfile {
	return append([]SurfaceProfile(nil), p.surfaces...)
}

func (p ExecutableProfile) ToolPresentation() ToolPresentationProfile {
	out := p.toolPresentation
	out.EndpointIDs = append([]string(nil), out.EndpointIDs...)
	out.NamespaceRequiredFields = append([]string(nil), out.NamespaceRequiredFields...)
	out.FunctionRequiredFields = append([]string(nil), out.FunctionRequiredFields...)
	return out
}

func (p ExecutableProfile) Subagents() SubagentProfile {
	out := p.subagents
	out.Mappings = append([]SubagentMapping(nil), out.Mappings...)
	return out
}

func (p ExecutableProfile) Files() FilesProfile { return p.files }

// CompileExecutableProfile 完成证据层到执行层的唯一转换。任何未知枚举、非法字段
// 组合或缺少消费者绑定的跨端点字段都会在目录加载阶段失败。
func CompileExecutableProfile(profile ProfileSpec) (ExecutableProfile, error) {
	compiled := ExecutableProfile{version: profile.Version(), features: profile.Features()}
	if strings.TrimSpace(compiled.version) == "" {
		return ExecutableProfile{}, errors.New("可执行画像缺少版本")
	}

	transports := make(map[string]TransportProfile)
	for _, transport := range profile.Transports() {
		if err := validateTransportForExecution(transport); err != nil {
			return ExecutableProfile{}, fmt.Errorf("transport %s: %w", transport.ID, err)
		}
		if _, duplicate := transports[transport.ID]; duplicate {
			return ExecutableProfile{}, fmt.Errorf("transport 重复: %s", transport.ID)
		}
		transports[transport.ID] = transport
		compiled.transports = append(compiled.transports, executableTransport(transport))
	}

	endpointIDs := make(map[string]struct{})
	for _, endpoint := range profile.Endpoints() {
		transport, ok := transports[endpoint.TransportID]
		if !ok {
			return ExecutableProfile{}, fmt.Errorf("endpoint %s 引用了未知 transport: %s", endpoint.ID, endpoint.TransportID)
		}
		if _, duplicate := endpointIDs[endpoint.ID]; duplicate {
			return ExecutableProfile{}, fmt.Errorf("endpoint 重复: %s", endpoint.ID)
		}
		policy, err := compileResourceLifecyclePolicy(endpoint, transport)
		if err != nil {
			return ExecutableProfile{}, fmt.Errorf("endpoint %s: %w", endpoint.ID, err)
		}
		if err := validateEndpointEnums(endpoint); err != nil {
			return ExecutableProfile{}, fmt.Errorf("endpoint %s: %w", endpoint.ID, err)
		}
		if err := validateCompressionContract(endpoint, transport); err != nil {
			return ExecutableProfile{}, fmt.Errorf("endpoint %s: %w", endpoint.ID, err)
		}
		endpointIDs[endpoint.ID] = struct{}{}
		compiled.endpoints = append(compiled.endpoints, executableEndpoint(endpoint, policy))
	}

	if err := compileCrossSections(profile, &compiled); err != nil {
		return ExecutableProfile{}, err
	}
	if err := validateExecutableCrossReferences(compiled, endpointIDs); err != nil {
		return ExecutableProfile{}, err
	}
	digest, err := executableProfileDigest(compiled)
	if err != nil {
		return ExecutableProfile{}, err
	}
	compiled.digest = digest
	return compiled, nil
}

func validateEndpointEnums(endpoint EndpointProfile) error {
	supported := EngineSupportedEnumValues()
	checks := []struct {
		domain EnumDomain
		value  string
		field  string
	}{
		{EnumDomainCompressionKind, string(endpoint.Compression), "Compression"},
		{EnumDomainLifecycleKind, string(endpoint.ClientLifecycle), "ClientLifecycle"},
		{EnumDomainHeaderOrderKind, string(endpoint.HeaderOrderMode), "HeaderOrderMode"},
		{EnumDomainBodyEncoding, string(endpoint.Body.Encoding), "Body.Encoding"},
	}
	for _, query := range endpoint.Query {
		checks = append(checks, struct {
			domain EnumDomain
			value  string
			field  string
		}{EnumDomainValueSource, string(query.Source), "Query.Source"})
	}
	for _, header := range endpoint.Headers {
		checks = append(checks,
			struct {
				domain EnumDomain
				value  string
				field  string
			}{EnumDomainValueSource, string(header.Source), "Header.Source"},
			struct {
				domain EnumDomain
				value  string
				field  string
			}{EnumDomainConditionKind, string(header.Condition), "Header.Condition"},
		)
	}
	for _, field := range endpoint.Body.Fields {
		checks = append(checks,
			struct {
				domain EnumDomain
				value  string
				field  string
			}{EnumDomainOmitCondition, string(field.OmitWhen), "Body.OmitWhen"},
			struct {
				domain EnumDomain
				value  string
				field  string
			}{EnumDomainConditionKind, string(field.Condition), "Body.Condition"},
		)
	}
	for _, check := range checks {
		if !supported.Contains(check.domain, check.value) {
			return fmt.Errorf("%s 含未知枚举 %q", check.field, check.value)
		}
	}
	return nil
}

func validateTransportForExecution(transport TransportProfile) error {
	if strings.TrimSpace(transport.ID) == "" || strings.TrimSpace(transport.Protocol) == "" ||
		strings.TrimSpace(transport.TLSStack) == "" {
		return errors.New("执行字段不完整")
	}
	switch transport.Protocol {
	case "http/1.1":
		if transport.WebSocket != nil {
			return errors.New("HTTP transport 不得携带 WebSocket 语义")
		}
	case "websocket":
		if transport.WebSocket == nil {
			return errors.New("WebSocket transport 缺少协商语义")
		}
		if !EngineSupportedEnumValues().Contains(
			EnumDomainHeaderOrderKind,
			transport.WebSocket.RemainingHeaderMode,
		) {
			return fmt.Errorf("RemainingHeaderMode 未受支持: %q", transport.WebSocket.RemainingHeaderMode)
		}
	default:
		return fmt.Errorf("未知协议: %q", transport.Protocol)
	}
	return nil
}

func compileResourceLifecyclePolicy(
	endpoint EndpointProfile,
	transport TransportProfile,
) (ResourceLifecyclePolicy, error) {
	policy := ResourceLifecyclePolicy{
		Lifecycle:         endpoint.ClientLifecycle,
		RetryReusesClient: transport.RetryReusesClient,
	}
	switch endpoint.ClientLifecycle {
	case LifecyclePerUpperApiCall:
		if transport.CrossCallConnectionReuse {
			return ResourceLifecyclePolicy{}, errors.New("per_upper_api_call 禁止跨调用连接复用")
		}
		if transport.Protocol != "http/1.1" {
			return ResourceLifecyclePolicy{}, errors.New("per_upper_api_call 只允许 HTTP transport")
		}
		policy.Scope = ResourceScopeInvocation
		if !transport.RetryReusesClient {
			policy.Scope = ResourceScopeInvocationAttempt
		}
	case LifecycleBackendClientLongLived:
		if transport.Protocol != "http/1.1" {
			return ResourceLifecyclePolicy{}, errors.New("backend_client_long_lived 只允许 HTTP transport")
		}
		if !transport.CrossCallConnectionReuse {
			return ResourceLifecyclePolicy{}, errors.New("backend_client_long_lived 必须允许跨调用连接复用")
		}
		policy.Scope = ResourceScopeAccountTransport
	case LifecycleWebsocketConnection:
		if transport.Protocol != "websocket" || transport.WebSocket == nil {
			return ResourceLifecyclePolicy{}, errors.New("websocket_connection 必须绑定 WebSocket transport")
		}
		if transport.CrossCallConnectionReuse {
			return ResourceLifecyclePolicy{}, errors.New("websocket_connection 禁止普通 HTTP 跨调用池语义")
		}
		policy.Scope = ResourceScopeWebSocketConnection
	case LifecycleReturnedUploadUrlCall:
		if transport.Protocol != "http/1.1" || !endpoint.HostFromResponse {
			return ResourceLifecyclePolicy{}, errors.New("returned_upload_url_call 必须绑定已验证返回 authority")
		}
		if transport.CrossCallConnectionReuse {
			return ResourceLifecyclePolicy{}, errors.New("returned_upload_url_call 禁止跨上传调用复用")
		}
		policy.Scope = ResourceScopeReturnedAuthority
	default:
		return ResourceLifecyclePolicy{}, fmt.Errorf("未知 ClientLifecycle: %q", endpoint.ClientLifecycle)
	}
	if err := policy.Validate(); err != nil {
		return ResourceLifecyclePolicy{}, err
	}
	return policy, nil
}

func validateCompressionContract(endpoint EndpointProfile, transport TransportProfile) error {
	if transport.Protocol != "websocket" {
		if endpoint.Compression == CompressionPermessageDeflateContextTakeover {
			return errors.New("HTTP endpoint 不得声明 permessage-deflate")
		}
		return nil
	}
	if endpoint.Compression == CompressionNone {
		return nil
	}
	if endpoint.Compression != CompressionPermessageDeflateContextTakeover {
		return errors.New("当前 WebSocket 执行器只批准 permessage-deflate context takeover")
	}
	ws := transport.WebSocket
	if ws.CompressionOffer != "permessage-deflate; client_max_window_bits" ||
		!ws.ContextTakeover || !ws.CompressedTextRSV1 || !ws.RawDeflatePayload {
		return errors.New("WebSocket compression offer/context takeover/RSV1/raw-deflate 组合未获批准")
	}
	return nil
}

func compileCrossSections(profile ProfileSpec, compiled *ExecutableProfile) error {
	seen := make(map[string]bool)
	for _, section := range profile.CrossSections() {
		if seen[section.Name] {
			return fmt.Errorf("跨端点段重复: %s", section.Name)
		}
		seen[section.Name] = true
		switch section.Name {
		case "RequiredRules":
			// 仅作为证据层叶子保留，不进入执行投影或 executable digest。
			continue
		case "Surfaces":
			if err := decodeExecutableSection(section.RawJSON, &compiled.surfaces); err != nil {
				return fmt.Errorf("编译 Surfaces: %w", err)
			}
		case "ToolPresentation":
			if err := decodeExecutableSection(section.RawJSON, &compiled.toolPresentation); err != nil {
				return fmt.Errorf("编译 ToolPresentation: %w", err)
			}
		case "Subagents":
			if err := decodeExecutableSection(section.RawJSON, &compiled.subagents); err != nil {
				return fmt.Errorf("编译 Subagents: %w", err)
			}
		case "Files":
			if err := decodeExecutableSection(section.RawJSON, &compiled.files); err != nil {
				return fmt.Errorf("编译 Files: %w", err)
			}
		default:
			return fmt.Errorf("跨端点段没有执行消费者: %s", section.Name)
		}
	}
	for _, required := range []string{"Surfaces", "ToolPresentation", "Subagents", "Files"} {
		if !seen[required] {
			return fmt.Errorf("可执行画像缺少跨端点段: %s", required)
		}
	}
	return nil
}

func decodeExecutableSection(raw json.RawMessage, target any) error {
	if len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return errors.New("执行段为空")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("执行段尾部存在额外数据")
	}
	return nil
}

func validateExecutableCrossReferences(
	profile ExecutableProfile,
	endpointIDs map[string]struct{},
) error {
	if len(profile.surfaces) == 0 {
		return errors.New("Surfaces 为空")
	}
	seenSurfaces := make(map[string]bool)
	for _, surface := range profile.surfaces {
		if strings.TrimSpace(surface.ID) == "" || strings.TrimSpace(surface.Product) == "" ||
			strings.TrimSpace(surface.Originator) == "" {
			return errors.New("Surfaces 含未绑定身份字段")
		}
		if seenSurfaces[surface.ID] {
			return fmt.Errorf("Surface ID 重复: %s", surface.ID)
		}
		seenSurfaces[surface.ID] = true
		if _, err := regexp.Compile(surface.TerminalTokenPattern); err != nil {
			return fmt.Errorf("Surface %s 的 terminal pattern 非法: %w", surface.ID, err)
		}
	}
	for _, endpointID := range profile.toolPresentation.EndpointIDs {
		if _, ok := endpointIDs[endpointID]; !ok {
			return fmt.Errorf("ToolPresentation 引用了未知 endpoint: %s", endpointID)
		}
	}
	for _, endpointID := range []string{
		profile.files.CreateEndpointID,
		profile.files.BlobUploadEndpointID,
		profile.files.UploadedEndpointID,
	} {
		if _, ok := endpointIDs[endpointID]; !ok {
			return fmt.Errorf("Files 引用了未知 endpoint: %s", endpointID)
		}
	}
	if profile.files.UploadLimitBytes == 0 || profile.files.RequestTimeoutMillis <= 0 ||
		profile.files.FinalizeTimeoutMillis <= 0 || profile.files.FinalizeRetryDelayMillis <= 0 {
		return errors.New("Files 资源边界非法")
	}
	seenMappings := make(map[string]bool)
	for _, mapping := range profile.subagents.Mappings {
		if strings.TrimSpace(mapping.ID) == "" || strings.TrimSpace(mapping.HeaderValue) == "" ||
			strings.TrimSpace(mapping.ThreadSource) == "" {
			return errors.New("Subagents 含未绑定映射")
		}
		if seenMappings[mapping.ID] {
			return fmt.Errorf("Subagent mapping 重复: %s", mapping.ID)
		}
		seenMappings[mapping.ID] = true
	}
	return nil
}

func executableEndpoint(
	endpoint EndpointProfile,
	policy ResourceLifecyclePolicy,
) ExecutableEndpointProfile {
	return ExecutableEndpointProfile{
		ID: endpoint.ID, Method: endpoint.Method, Upgrade: endpoint.Upgrade,
		TransportID: endpoint.TransportID, Host: endpoint.Host,
		HostFromResponse: endpoint.HostFromResponse, Path: endpoint.Path,
		Query: append([]QueryFieldProfile(nil), endpoint.Query...), Accept: endpoint.Accept,
		ContentType: endpoint.ContentType, Compression: endpoint.Compression,
		ResourceLifecycle: policy, HeaderOrderMode: endpoint.HeaderOrderMode,
		Headers:                 append([]HeaderSlotProfile(nil), endpoint.Headers...),
		HeaderMapInsertionOrder: append([]string(nil), endpoint.HeaderMapInsertionOrder...),
		PostRemoveHeaders:       append([]string(nil), endpoint.PostRemoveHeaders...),
		Body: BodyContractProfile{
			Encoding: endpoint.Body.Encoding, Closed: endpoint.Body.Closed,
			Discriminator: endpoint.Body.Discriminator,
			Fields:        append([]BodyFieldProfile(nil), endpoint.Body.Fields...),
		},
	}
}

func executableTransport(transport TransportProfile) ExecutableTransportProfile {
	out := ExecutableTransportProfile{
		ID: transport.ID, Protocol: transport.Protocol,
		PlatformCondition: transport.PlatformCondition, TLSStack: transport.TLSStack,
		CipherSuites:        append([]uint16(nil), transport.CipherSuites...),
		SupportedGroups:     append([]uint16(nil), transport.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), transport.SignatureAlgorithms...),
		ALPN:                append([]string(nil), transport.ALPN...),
		Extensions:          append([]uint16(nil), transport.Extensions...),
		RandomizeExtensions: transport.RandomizeExtensions,
		SupportedVersions:   append([]uint16(nil), transport.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), transport.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), transport.PSKModes...),
		TLSMinVersion:       transport.TLSMinVersion, TLSMaxVersion: transport.TLSMaxVersion,
		LowercaseHTTPHeaders: transport.LowercaseHTTPHeaders,
	}
	if transport.WebSocket != nil {
		ws := *transport.WebSocket
		ws.FixedHandshakePrefix = append([]string(nil), transport.WebSocket.FixedHandshakePrefix...)
		out.WebSocket = &ws
	}
	return out
}

func executableProfileDigest(profile ExecutableProfile) (string, error) {
	projection := struct {
		Version          string
		Endpoints        []ExecutableEndpointProfile
		Transports       []ExecutableTransportProfile
		Features         FeatureDefaults
		Surfaces         []SurfaceProfile
		ToolPresentation ToolPresentationProfile
		Subagents        SubagentProfile
		Files            FilesProfile
	}{
		Version: profile.version, Endpoints: profile.endpoints, Transports: profile.transports,
		Features: profile.features, Surfaces: profile.surfaces,
		ToolPresentation: profile.toolPresentation, Subagents: profile.subagents, Files: profile.files,
	}
	raw, err := json.Marshal(projection)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// SortedEndpointIDs 为启动门禁和测试提供稳定的执行端点闭集。
func (p ExecutableProfile) SortedEndpointIDs() []string {
	ids := make([]string, 0, len(p.endpoints))
	for _, endpoint := range p.endpoints {
		ids = append(ids, endpoint.ID)
	}
	sort.Strings(ids)
	return ids
}
