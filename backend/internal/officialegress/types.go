// Package officialegress 提供官方上游出站身份的中立生产边界。
//
// 本包只承载 persona、route、release、请求定型、Guard 与 transport port；
// 禁止依赖 service、repository、Gin、账号模型或具体 HTTP 客户端实现。
package officialegress

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

type Persona string

const (
	PersonaCodexCLI     Persona = "codex-cli"
	PersonaClaudeCode   Persona = "claude-code"
	PersonaChatGPTWeb   Persona = "chatgpt-web"
	PersonaUnclassified Persona = "unclassified"
	PersonaDeadCode     Persona = "dead-code"
)

func (p Persona) Valid() bool {
	switch p {
	case PersonaCodexCLI, PersonaClaudeCode, PersonaChatGPTWeb, PersonaUnclassified, PersonaDeadCode:
		return true
	default:
		return false
	}
}

type SinkID string
type Purpose string
type AdapterID string
type ExecutorID string

type BackendKind string

const (
	BackendNone         BackendKind = "none"
	BackendPlainNetHTTP BackendKind = "plain_net_http"
	BackendHTTPUpstream BackendKind = "http_upstream"
	BackendReqProfile   BackendKind = "req_profile"
	BackendWebSocket    BackendKind = "websocket"
)

func (b BackendKind) Valid() bool {
	switch b {
	case BackendPlainNetHTTP, BackendHTTPUpstream, BackendReqProfile, BackendWebSocket:
		return true
	default:
		return false
	}
}

type WireProtocol string

const (
	WireProtocolHTTP      WireProtocol = "http"
	WireProtocolWebSocket WireProtocol = "websocket"
)

func (p WireProtocol) Valid() bool {
	return p == WireProtocolHTTP || p == WireProtocolWebSocket
}

type ReleaseMode string

const (
	ReleaseModeActive   ReleaseMode = "active"
	ReleaseModePrevious ReleaseMode = "previous"
)

func (m ReleaseMode) Valid() bool {
	return m == ReleaseModeActive || m == ReleaseModePrevious
}

type UnknownRoutePolicy string
type UnregisteredSinkPolicy string

const (
	PolicyObserve = "observe"
	PolicyEnforce = "enforce"
)

func (p UnknownRoutePolicy) Valid() bool {
	return p == UnknownRoutePolicy(PolicyObserve) || p == UnknownRoutePolicy(PolicyEnforce)
}

func (p UnregisteredSinkPolicy) Valid() bool {
	return p == UnregisteredSinkPolicy(PolicyObserve) || p == UnregisteredSinkPolicy(PolicyEnforce)
}

type SinkEnforcementState string

const (
	SinkStateLegacyObserve  SinkEnforcementState = "legacy_observe"
	SinkStateCanaryEnforce  SinkEnforcementState = "canary_enforce"
	SinkStateEnforced       SinkEnforcementState = "enforced"
	SinkStatePendingRemoval SinkEnforcementState = "pending_removal"
)

func (s SinkEnforcementState) Valid() bool {
	switch s {
	case SinkStateLegacyObserve, SinkStateCanaryEnforce, SinkStateEnforced, SinkStatePendingRemoval:
		return true
	default:
		return false
	}
}

type EndpointEvidence string

const (
	EndpointEvidenceCodexProfile    EndpointEvidence = "codex_profile"
	EndpointEvidenceClaudeProfile   EndpointEvidence = "claude_profile"
	EndpointEvidenceTransportOnly   EndpointEvidence = "transport_only"
	EndpointEvidenceExternalPersona EndpointEvidence = "external_persona"
	EndpointEvidenceMissing         EndpointEvidence = "missing"
	EndpointEvidenceNotApplicable   EndpointEvidence = "not_applicable"
)

func (e EndpointEvidence) Valid() bool {
	switch e {
	case EndpointEvidenceCodexProfile, EndpointEvidenceClaudeProfile, EndpointEvidenceTransportOnly,
		EndpointEvidenceExternalPersona, EndpointEvidenceMissing,
		EndpointEvidenceNotApplicable:
		return true
	default:
		return false
	}
}

// RouteKey 是 Guard 绑定校验使用的稳定四元组。Host 与 Path 保存规范化模板，
// 不保存 query，也不保存含用户标识的实际路径。
type RouteKey struct {
	Method  string
	Host    string
	Path    string
	Purpose Purpose
}

func (k RouteKey) String() string {
	return strings.Join([]string{k.Method, k.Host, k.Path, string(k.Purpose)}, " ")
}

func (k RouteKey) Valid() bool {
	return k.Method != "" && k.Method == strings.ToUpper(k.Method) &&
		k.Host != "" && strings.HasPrefix(k.Path, "/") && k.Purpose != ""
}

// SinkGuardOverride 是独立于验收状态的限时 observe 覆盖。
type SinkGuardOverride struct {
	ObserveUntil time.Time
	Owner        string
	ReasonCode   string
}

func (o SinkGuardOverride) Active(now time.Time) bool {
	return !o.ObserveUntil.IsZero() && now.Before(o.ObserveUntil)
}

func (o SinkGuardOverride) Validate() error {
	if o.ObserveUntil.IsZero() {
		return errors.New("SinkGuardOverride 缺少 observe_until")
	}
	if strings.TrimSpace(o.Owner) == "" || strings.TrimSpace(o.ReasonCode) == "" {
		return errors.New("SinkGuardOverride 缺少责任人或 reason code")
	}
	return nil
}

// ExecutionPolicy 是显式执行策略输入。它不能由 ProfileSpec 默认补齐。
type ExecutionPolicy struct {
	ID                   string
	Source               string
	MaxAttempts          int
	Replayable           bool
	MinimumInterval      time.Duration
	ConcurrencyLimit     int
	HasBillingSideEffect bool
}

type HeaderNormalizationMode string

const (
	HeaderNormalizationPreserve  HeaderNormalizationMode = "preserve"
	HeaderNormalizationLowercase HeaderNormalizationMode = "lowercase"
)

// WireNormalizationPlan 明确列出 Executor 签名后、terminal Guard 之前允许发生的
// adapter 变换。Token 对语义摘要和本计划同时签名；Guard 再验证最终 wire 形态。
type WireNormalizationPlan struct {
	HeaderMode                  HeaderNormalizationMode
	SuppressDefaultUserAgent    bool
	WebSocketCompressionOffer   string
	WebSocketContextTakeover    bool `json:"WebSocketContextTakeover,omitempty"`
	WebSocketCompressedTextRSV1 bool `json:"WebSocketCompressedTextRSV1,omitempty"`
	WebSocketRawDeflatePayload  bool `json:"WebSocketRawDeflatePayload,omitempty"`
}

func (p WireNormalizationPlan) Validate(protocol WireProtocol) error {
	if p.HeaderMode != HeaderNormalizationPreserve && p.HeaderMode != HeaderNormalizationLowercase {
		return errors.New("WireNormalizationPlan header mode 非法")
	}
	offer := strings.TrimSpace(p.WebSocketCompressionOffer)
	compressionFlags := p.WebSocketContextTakeover || p.WebSocketCompressedTextRSV1 ||
		p.WebSocketRawDeflatePayload
	if protocol != WireProtocolWebSocket && (offer != "" || compressionFlags) {
		return errors.New("非 WebSocket transport 不得声明压缩 offer 变换")
	}
	if (offer == "") != !compressionFlags {
		return errors.New("WebSocket 压缩 offer 与执行字段不完整")
	}
	if offer != "" && (!p.WebSocketContextTakeover || !p.WebSocketCompressedTextRSV1 ||
		!p.WebSocketRawDeflatePayload) {
		return errors.New("WebSocket 压缩执行组合未获批准")
	}
	return nil
}

func (p WireNormalizationPlan) Digest() string {
	raw := strings.Join([]string{
		string(p.HeaderMode), strconv.FormatBool(p.SuppressDefaultUserAgent),
		strings.TrimSpace(p.WebSocketCompressionOffer),
		strconv.FormatBool(p.WebSocketContextTakeover),
		strconv.FormatBool(p.WebSocketCompressedTextRSV1),
		strconv.FormatBool(p.WebSocketRawDeflatePayload),
	}, "\x00")
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

func (p ExecutionPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" {
		return errors.New("ExecutionPolicy 缺少 ID 或来源")
	}
	if p.MaxAttempts <= 0 || p.ConcurrencyLimit < 0 || p.MinimumInterval < 0 {
		return errors.New("ExecutionPolicy 数值非法")
	}
	return nil
}

// DeploymentSupportPolicy 是显式部署能力输入，不从 TLS/ProfileSpec 反推。
type DeploymentSupportPolicy struct {
	ID                    string
	Source                string
	Platform              string
	ProxyMode             string
	ProxyIdentityDigest   string
	CustomCARoots         bool
	CustomCAContentDigest string
	SupportedBackends     []BackendKind
}

func (p DeploymentSupportPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" ||
		strings.TrimSpace(p.Platform) == "" || strings.TrimSpace(p.ProxyMode) == "" {
		return errors.New("DeploymentSupportPolicy 缺少显式来源或平台")
	}
	if strings.TrimSpace(p.ProxyIdentityDigest) == "" {
		return errors.New("DeploymentSupportPolicy 缺少具体代理身份")
	}
	if p.CustomCARoots != (strings.TrimSpace(p.CustomCAContentDigest) != "") {
		return errors.New("DeploymentSupportPolicy 自定义 CA 标志与内容摘要不一致")
	}
	if p.CustomCARoots && !isSHA256HexOrOpaque(p.CustomCAContentDigest) {
		return errors.New("DeploymentSupportPolicy 自定义 CA 内容摘要非法")
	}
	if len(p.SupportedBackends) == 0 {
		return errors.New("DeploymentSupportPolicy 未声明 backend")
	}
	seen := make(map[BackendKind]struct{}, len(p.SupportedBackends))
	for _, backend := range p.SupportedBackends {
		if !backend.Valid() {
			return fmt.Errorf("DeploymentSupportPolicy backend 非法: %s", backend)
		}
		if _, duplicate := seen[backend]; duplicate {
			return fmt.Errorf("DeploymentSupportPolicy backend 重复: %s", backend)
		}
		seen[backend] = struct{}{}
	}
	return nil
}

// ReleaseSelectionPolicy 显式连接业务 purpose、registry purpose 和画像 endpoint。
// RegistryPurpose 保留证据层原名；FamilyID 可使用更清晰的 HTTP/WS 家族命名。
type ReleaseSelectionPolicy struct {
	ID              string
	Source          string
	BusinessPurpose Purpose
	FamilyID        string
	RegistryPurpose string
	EndpointID      string
}

func (p ReleaseSelectionPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" || p.BusinessPurpose == "" ||
		strings.TrimSpace(p.FamilyID) == "" || strings.TrimSpace(p.RegistryPurpose) == "" ||
		strings.TrimSpace(p.EndpointID) == "" {
		return errors.New("ReleaseSelectionPolicy 字段不完整")
	}
	return nil
}

// TransportSpec 只能由 ReleaseBundle/Executor 选择，业务 Plan 不包含该字段。
type TransportSpec struct {
	ID                   string
	Backend              BackendKind
	Protocol             WireProtocol
	Adapter              AdapterID
	ProfileDigest        string
	ConnectionGroup      string
	ConnectionPoolDigest string
	ResourceLifecycle    profilecontract.ResourceLifecyclePolicy
	Normalization        WireNormalizationPlan
	TLS                  TLSProfileSpec
}

type H1HeaderOrderRule struct {
	Method           string
	Path             string
	RequiredHeaders  []string
	ForbiddenHeaders []string
	Order            []string
	Mode             string
	PrefixHeaders    []string
	RemoveHeaders    []string
	AppendHeaders    []string
	RejectUnlisted   bool
}

type TLSProfileSpec struct {
	Stack               string
	CipherSuites        []uint16
	SupportedGroups     []uint16
	SignatureAlgorithms []uint16
	ALPN                []string
	Extensions          []uint16
	RandomizeExtensions bool
	SupportedVersions   []uint16
	KeyShareGroups      []uint16
	PSKModes            []uint16
	MinVersion          uint16
	MaxVersion          uint16
	LowercaseHeaders    bool
	PreserveHeaderCase  []string
	H1HeaderOrders      []H1HeaderOrderRule
	StrictH1Wire        bool
}

func (s TLSProfileSpec) clone() TLSProfileSpec {
	out := s
	out.CipherSuites = cloneSlicePreservingNil(s.CipherSuites)
	out.SupportedGroups = cloneSlicePreservingNil(s.SupportedGroups)
	out.SignatureAlgorithms = cloneSlicePreservingNil(s.SignatureAlgorithms)
	out.ALPN = cloneSlicePreservingNil(s.ALPN)
	out.Extensions = cloneSlicePreservingNil(s.Extensions)
	out.SupportedVersions = cloneSlicePreservingNil(s.SupportedVersions)
	out.KeyShareGroups = cloneSlicePreservingNil(s.KeyShareGroups)
	out.PSKModes = cloneSlicePreservingNil(s.PSKModes)
	out.PreserveHeaderCase = cloneSlicePreservingNil(s.PreserveHeaderCase)
	out.H1HeaderOrders = cloneSlicePreservingNil(s.H1HeaderOrders)
	for i, rule := range s.H1HeaderOrders {
		out.H1HeaderOrders[i] = rule
		out.H1HeaderOrders[i].RequiredHeaders = cloneSlicePreservingNil(rule.RequiredHeaders)
		out.H1HeaderOrders[i].ForbiddenHeaders = cloneSlicePreservingNil(rule.ForbiddenHeaders)
		out.H1HeaderOrders[i].Order = cloneSlicePreservingNil(rule.Order)
		out.H1HeaderOrders[i].PrefixHeaders = cloneSlicePreservingNil(rule.PrefixHeaders)
		out.H1HeaderOrders[i].RemoveHeaders = cloneSlicePreservingNil(rule.RemoveHeaders)
		out.H1HeaderOrders[i].AppendHeaders = cloneSlicePreservingNil(rule.AppendHeaders)
	}
	return out
}

func cloneSlicePreservingNil[T any](source []T) []T {
	if source == nil {
		return nil
	}
	cloned := make([]T, len(source))
	copy(cloned, source)
	return cloned
}

func (s TransportSpec) Clone() TransportSpec {
	out := s
	out.TLS = s.TLS.clone()
	return out
}

func (s TransportSpec) Validate() error {
	if strings.TrimSpace(s.ID) == "" || !s.Backend.Valid() || !s.Protocol.Valid() ||
		strings.TrimSpace(string(s.Adapter)) == "" || !isSHA256HexOrOpaque(s.ProfileDigest) ||
		strings.TrimSpace(s.ConnectionGroup) == "" || !isSHA256HexOrOpaque(s.ConnectionPoolDigest) ||
		strings.TrimSpace(string(s.ResourceLifecycle.Lifecycle)) == "" ||
		strings.TrimSpace(string(s.ResourceLifecycle.Scope)) == "" {
		return errors.New("TransportSpec 字段不完整或非法")
	}
	if err := s.ResourceLifecycle.Validate(); err != nil {
		return err
	}
	return s.Normalization.Validate(s.Protocol)
}

func resourceLifecycleDigest(policy profilecontract.ResourceLifecyclePolicy) string {
	raw, err := json.Marshal(policy)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func isSHA256HexOrOpaque(value string) bool {
	value = strings.TrimSpace(value)
	if value == "" {
		return false
	}
	if len(value) != sha256.Size*2 {
		return true
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

type ResolvedEndpoint struct {
	ID        string
	Route     RouteKey
	Protocol  WireProtocol
	Transport TransportSpec
}

// ResolvedRelease 是 1A 的中立发布值。所有 slice/map 均在构造和 getter 时深拷贝。
type ResolvedRelease struct {
	id         string
	purpose    string
	mode       ReleaseMode
	version    string
	digest     string
	persona    Persona
	execution  ExecutionPolicy
	deployment DeploymentSupportPolicy
	selection  ReleaseSelectionPolicy
	endpoints  map[string]ResolvedEndpoint
}

type ResolvedReleaseInput struct {
	ID         string
	Purpose    string
	Mode       ReleaseMode
	Version    string
	Digest     string
	Persona    Persona
	Execution  ExecutionPolicy
	Deployment DeploymentSupportPolicy
	Selection  ReleaseSelectionPolicy
	Endpoints  []ResolvedEndpoint
}

func NewResolvedRelease(input ResolvedReleaseInput) (ResolvedRelease, error) {
	if strings.TrimSpace(input.ID) == "" || strings.TrimSpace(input.Purpose) == "" || !input.Mode.Valid() ||
		strings.TrimSpace(input.Version) == "" || !isSHA256HexOrOpaque(input.Digest) || !input.Persona.Valid() {
		return ResolvedRelease{}, errors.New("ResolvedRelease 身份字段不完整")
	}
	if err := input.Execution.Validate(); err != nil {
		return ResolvedRelease{}, err
	}
	if err := input.Deployment.Validate(); err != nil {
		return ResolvedRelease{}, err
	}
	if err := input.Selection.Validate(); err != nil {
		return ResolvedRelease{}, err
	}
	if len(input.Endpoints) == 0 {
		return ResolvedRelease{}, errors.New("ResolvedRelease 没有 endpoint")
	}
	release := ResolvedRelease{
		id: input.ID, purpose: input.Purpose, mode: input.Mode, version: input.Version,
		digest: input.Digest, persona: input.Persona, execution: input.Execution,
		deployment: cloneDeploymentPolicy(input.Deployment), selection: input.Selection,
		endpoints: make(map[string]ResolvedEndpoint, len(input.Endpoints)),
	}
	for _, endpoint := range input.Endpoints {
		if strings.TrimSpace(endpoint.ID) == "" || !endpoint.Route.Valid() || !endpoint.Protocol.Valid() {
			return ResolvedRelease{}, errors.New("ResolvedRelease endpoint 非法")
		}
		if err := endpoint.Transport.Validate(); err != nil {
			return ResolvedRelease{}, fmt.Errorf("endpoint %s: %w", endpoint.ID, err)
		}
		if endpoint.Protocol != endpoint.Transport.Protocol {
			return ResolvedRelease{}, fmt.Errorf("endpoint %s 的 protocol 与 TransportSpec 不一致", endpoint.ID)
		}
		if endpoint.Transport.ProfileDigest != input.Digest {
			return ResolvedRelease{}, fmt.Errorf("endpoint %s 的 profile digest 与 Release 不一致", endpoint.ID)
		}
		if !containsBackend(input.Deployment.SupportedBackends, endpoint.Transport.Backend) {
			return ResolvedRelease{}, fmt.Errorf("endpoint %s 的 backend 不在 DeploymentSupportPolicy 中", endpoint.ID)
		}
		if _, duplicate := release.endpoints[endpoint.ID]; duplicate {
			return ResolvedRelease{}, fmt.Errorf("ResolvedRelease endpoint 重复: %s", endpoint.ID)
		}
		release.endpoints[endpoint.ID] = endpoint
	}
	return release, nil
}

func containsBackend(backends []BackendKind, expected BackendKind) bool {
	for _, backend := range backends {
		if backend == expected {
			return true
		}
	}
	return false
}

func cloneDeploymentPolicy(in DeploymentSupportPolicy) DeploymentSupportPolicy {
	out := in
	out.SupportedBackends = append([]BackendKind(nil), in.SupportedBackends...)
	return out
}

func (r ResolvedRelease) ID() string                 { return r.id }
func (r ResolvedRelease) Purpose() string            { return r.purpose }
func (r ResolvedRelease) Mode() ReleaseMode          { return r.mode }
func (r ResolvedRelease) Version() string            { return r.version }
func (r ResolvedRelease) Digest() string             { return r.digest }
func (r ResolvedRelease) Persona() Persona           { return r.persona }
func (r ResolvedRelease) Execution() ExecutionPolicy { return r.execution }
func (r ResolvedRelease) Deployment() DeploymentSupportPolicy {
	return cloneDeploymentPolicy(r.deployment)
}
func (r ResolvedRelease) Selection() ReleaseSelectionPolicy { return r.selection }

func (r ResolvedRelease) Endpoint(id string) (ResolvedEndpoint, bool) {
	endpoint, ok := r.endpoints[id]
	return endpoint, ok
}

func (r ResolvedRelease) Endpoints() []ResolvedEndpoint {
	ids := make([]string, 0, len(r.endpoints))
	for id := range r.endpoints {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]ResolvedEndpoint, 0, len(ids))
	for _, id := range ids {
		out = append(out, r.endpoints[id])
	}
	return out
}

type ReleaseResolveRequest struct {
	BusinessPurpose Purpose
	Mode            ReleaseMode
	Selection       ReleaseSelectionPolicy
	Execution       ExecutionPolicy
	Deployment      DeploymentSupportPolicy
}

type RequestBodyMode string

const (
	RequestBodyReplayable RequestBodyMode = "replayable_bytes"
	RequestBodySingleUse  RequestBodyMode = "single_use_stream"
)

type requestBodyState struct {
	mu         sync.Mutex
	mode       RequestBodyMode
	replayable []byte
	document   *orderedJSONDocument
	stream     io.ReadCloser
	length     int64
	capability [sha256.Size]byte
	consumed   bool
}

// RequestBody 是跨 RequestCompiler 传递的不可透明读取 Body 句柄。compiler 可以读取
// replayable bytes，但 single-use stream 只能原样交还 Executor，不能预读或复制。
type RequestBody struct {
	state *requestBodyState
}

func NewReplayableRequestBody(body []byte) RequestBody {
	return newOwnedReplayableRequestBody(append([]byte(nil), body...))
}

// newOwnedReplayableRequestBody 只接收包内新产生、此后不再改写的字节。
// 对外构造函数仍复制一次；Plan、CompiledRequest 与 PreparedRequest 仅共享该
// 不可变 backing，避免在一次 attempt 内重复复制整段 Body。
func newOwnedReplayableRequestBody(body []byte) RequestBody {
	return RequestBody{state: &requestBodyState{
		mode: RequestBodyReplayable, replayable: body, length: int64(len(body)),
	}}
}

func NewSingleUseRequestBody(stream io.ReadCloser, contentLength int64) (RequestBody, error) {
	if stream == nil || contentLength < 0 {
		return RequestBody{}, errors.New("single-use stream 或 ContentLength 非法")
	}
	state := &requestBodyState{
		mode: RequestBodySingleUse, stream: stream, length: contentLength,
	}
	if _, err := rand.Read(state.capability[:]); err != nil {
		return RequestBody{}, fmt.Errorf("生成 single-use capability: %w", err)
	}
	return RequestBody{state: state}, nil
}

func (b RequestBody) Mode() RequestBodyMode {
	if b.state == nil {
		return RequestBodyReplayable
	}
	return b.state.mode
}

func (b RequestBody) ContentLength() int64 {
	if b.state == nil {
		return 0
	}
	return b.state.length
}

func (b RequestBody) ReplayableBytes() ([]byte, bool) {
	if b.state != nil && b.state.document != nil {
		return b.state.document.encodeSourceOrder(), true
	}
	view, ok := b.replayableView()
	if !ok {
		return nil, false
	}
	return append([]byte(nil), view...), true
}

// replayableView 是包内只读视图。返回切片的 backing 在构造后永久不可写，且该
// 方法不得暴露到包外；公开 ReplayableBytes 始终返回副本。
func (b RequestBody) replayableView() ([]byte, bool) {
	if b.state == nil {
		return nil, true
	}
	if b.state.mode != RequestBodyReplayable {
		return nil, false
	}
	return b.state.replayable, true
}

func (b RequestBody) clone() RequestBody {
	if b.state == nil {
		return RequestBody{}
	}
	if b.state.mode == RequestBodySingleUse {
		// single-use 句柄共享同一消费状态，禁止通过 Plan/CompiledRequest 拷贝获得
		// 第二个 reader。
		return RequestBody{state: b.state}
	}
	if b.state.document != nil {
		return RequestBody{state: &requestBodyState{
			mode:       RequestBodyReplayable,
			replayable: b.state.replayable,
			document:   b.state.document.clone(),
			length:     b.state.length,
		}}
	}
	return RequestBody{state: b.state}
}

func (b RequestBody) jsonDocument() *orderedJSONDocument {
	if b.state == nil || b.state.mode != RequestBodyReplayable {
		return nil
	}
	return b.state.document
}

// sameCapability 只在包内比较 single-use 私有能力。compiler 无法读取 capability，
// 也不能用另一个同长度 stream 伪造同一 requestBodyState。
func (b RequestBody) sameCapability(other RequestBody) bool {
	if b.state == nil || other.state == nil || b.state != other.state {
		return false
	}
	b.state.mu.Lock()
	defer b.state.mu.Unlock()
	return b.state.mode == RequestBodySingleUse && b.state.capability != [sha256.Size]byte{}
}

func (b RequestBody) takeSingleUse() (io.ReadCloser, [sha256.Size]byte, int64, error) {
	if b.state == nil {
		return nil, [sha256.Size]byte{}, 0, errors.New("Body 不是 single-use stream")
	}
	b.state.mu.Lock()
	defer b.state.mu.Unlock()
	if b.state.mode != RequestBodySingleUse || b.state.stream == nil || b.state.consumed {
		return nil, [sha256.Size]byte{}, 0, errors.New("single-use stream 已消费")
	}
	b.state.consumed = true
	stream := b.state.stream
	b.state.stream = nil
	return stream, b.state.capability, b.state.length, nil
}

// CodexEgressPlan 是未定型业务输入，不允许携带 TransportSpec。
type CodexEgressPlan struct {
	SinkID                  SinkID
	Purpose                 Purpose
	EndpointID              string
	Mode                    ReleaseMode
	Protocol                WireProtocol
	Method                  string
	URL                     *url.URL
	Headers                 http.Header
	ResolvedHeaderOverrides http.Header
	IdentityMode            IdentityMode
	IdentityFacts           CodexIdentityFacts
	Authentication          AttemptAuthentication
	HeaderPolicy            HeaderPolicy
	BodyPolicy              BodyPolicy
	BehaviorPolicy          BehaviorPolicy
	Body                    RequestBody
	InvocationID            string
	DeclaredPersona         Persona
}

func (p CodexEgressPlan) clone() CodexEgressPlan {
	out := p
	if p.URL != nil {
		cloned := *p.URL
		out.URL = &cloned
	}
	out.Headers = p.Headers.Clone()
	out.ResolvedHeaderOverrides = p.ResolvedHeaderOverrides.Clone()
	out.Authentication = p.Authentication.clone()
	out.BehaviorPolicy = cloneBehaviorPolicy(p.BehaviorPolicy)
	out.Body = p.Body.clone()
	return out
}

// CompiledRequest 是 RequestCompiler 的未签名输出。
type CompiledRequest struct {
	method  string
	url     *url.URL
	headers http.Header
	body    RequestBody
}

func NewCompiledRequest(method string, target *url.URL, headers http.Header, body RequestBody) (CompiledRequest, error) {
	method = strings.ToUpper(strings.TrimSpace(method))
	if method == "" || target == nil || strings.TrimSpace(target.Hostname()) == "" {
		return CompiledRequest{}, errors.New("CompiledRequest method/URL 非法")
	}
	clonedURL := *target
	return CompiledRequest{
		method:  method,
		url:     &clonedURL,
		headers: headers.Clone(),
		body:    body.clone(),
	}, nil
}

func (r CompiledRequest) Method() string { return r.method }
func (r CompiledRequest) URL() *url.URL {
	if r.url == nil {
		return nil
	}
	cloned := *r.url
	return &cloned
}
func (r CompiledRequest) Headers() http.Header { return r.headers.Clone() }
func (r CompiledRequest) Body() RequestBody    { return r.body.clone() }

type tokenPayload struct {
	IssuerID                  ExecutorID
	AuthorityKind             receiptcontract.AuthorityKind
	AuthorityID               ExecutorID
	ReleaseDigest             string
	ProfileDigest             string
	BundleDigest              string
	SinkID                    SinkID
	Route                     RouteKey
	Persona                   Persona
	EndpointID                string
	TransportID               string
	AdapterID                 AdapterID
	Backend                   BackendKind
	Protocol                  WireProtocol
	ResourceLifecycleDigest   string
	ConnectionPoolDigest      string
	InvocationID              string
	AttemptOrdinal            uint32
	AttemptReason             string
	IdentityAttestationDigest string
	DialectAttestationDigest  string
	RequestDigest             string
	Normalization             WireNormalizationPlan
}

// FinalizationToken 的全部字段私有，只有本包内的 tokenIssuer 能签发。
type FinalizationToken struct {
	payload   tokenPayload
	signature [sha256.Size]byte
}

type tokenIssuer struct {
	id            ExecutorID
	authorityKind receiptcontract.AuthorityKind
	persona       Persona
	secret        [sha256.Size]byte
}

func newTokenIssuer(id ExecutorID) (*tokenIssuer, error) {
	return newTokenIssuerForPersona(
		receiptcontract.AuthorityCodexExecutor, PersonaCodexCLI, id,
	)
}

func newTokenIssuerForPersona(
	authorityKind receiptcontract.AuthorityKind,
	persona Persona,
	id ExecutorID,
) (*tokenIssuer, error) {
	if strings.TrimSpace(string(id)) == "" {
		return nil, errors.New("ExecutorID 为空")
	}
	if !authorityKind.Valid() || !persona.Valid() || persona == PersonaUnclassified ||
		persona == PersonaDeadCode {
		return nil, errors.New("执行 authority kind/persona 非法")
	}
	issuer := &tokenIssuer{id: id, authorityKind: authorityKind, persona: persona}
	if _, err := rand.Read(issuer.secret[:]); err != nil {
		return nil, fmt.Errorf("生成 FinalizationToken 密钥: %w", err)
	}
	return issuer, nil
}

func (i *tokenIssuer) sign(payload tokenPayload) FinalizationToken {
	payload.IssuerID = i.id
	payload.AuthorityKind = i.authorityKind
	payload.Persona = i.persona
	mac := hmac.New(sha256.New, i.secret[:])
	_, _ = io.WriteString(mac, canonicalTokenPayload(payload))
	var signature [sha256.Size]byte
	copy(signature[:], mac.Sum(nil))
	return FinalizationToken{payload: payload, signature: signature}
}

func (i *tokenIssuer) verify(token FinalizationToken) bool {
	if i == nil || token.payload.IssuerID != i.id || token.payload.Persona != i.persona ||
		token.payload.AuthorityKind != i.authorityKind {
		return false
	}
	mac := hmac.New(sha256.New, i.secret[:])
	_, _ = io.WriteString(mac, canonicalTokenPayload(token.payload))
	return hmac.Equal(token.signature[:], mac.Sum(nil))
}

func canonicalTokenPayload(payload tokenPayload) string {
	parts := []string{
		string(payload.IssuerID), string(payload.AuthorityKind), string(payload.AuthorityID),
		payload.ReleaseDigest, payload.ProfileDigest, payload.BundleDigest, string(payload.SinkID),
		payload.Route.String(), string(payload.Persona), payload.EndpointID,
		payload.TransportID, string(payload.AdapterID), string(payload.Backend), string(payload.Protocol),
		payload.ResourceLifecycleDigest, payload.ConnectionPoolDigest,
		payload.InvocationID, strconv.FormatUint(uint64(payload.AttemptOrdinal), 10),
		payload.AttemptReason, payload.IdentityAttestationDigest,
		payload.DialectAttestationDigest,
		payload.RequestDigest, payload.Normalization.Digest(),
	}
	return strings.Join(parts, "\x00")
}

// PreparedRequest 只能由 Executor 构造。adapter 可取得一次深拷贝用于实际发送，
// 但 terminal Guard 仍会用 token 校验最终请求是否发生变化。
type PreparedRequest struct {
	request   *http.Request
	requestMu *sync.Mutex
	consumed  *bool
	singleUse bool
	token     FinalizationToken
	transport TransportSpec
	bundle    ReleaseBundle
	endpoint  ResolvedEndpointPlan
	identity  CodexIdentityFacts
	dialect   preparedDialectState
}

func (p PreparedRequest) TakeHTTPRequest() (*http.Request, error) {
	if p.request == nil {
		return nil, errors.New("PreparedRequest 请求为空")
	}
	if p.singleUse {
		if p.requestMu == nil || p.consumed == nil {
			return nil, errors.New("single-use PreparedRequest 状态非法")
		}
		p.requestMu.Lock()
		defer p.requestMu.Unlock()
		if *p.consumed {
			return nil, errors.New("single-use PreparedRequest 已消费")
		}
		*p.consumed = true
		cloned := p.request.Clone(p.request.Context())
		cloned.Header = p.request.Header.Clone()
		cloned.Body = p.request.Body
		p.request.Body = http.NoBody
		return cloned, nil
	}
	cloned := p.request.Clone(p.request.Context())
	cloned.Header = p.request.Header.Clone()
	if p.request.GetBody != nil {
		body, err := p.request.GetBody()
		if err != nil {
			return nil, err
		}
		cloned.Body = body
	}
	return cloned, nil
}

func (p PreparedRequest) Token() FinalizationToken { return p.token }
func (p PreparedRequest) Transport() TransportSpec { return p.transport.Clone() }
func (p PreparedRequest) Bundle() ReleaseBundle {
	if state, ok := p.dialect.(codexPreparedState); ok {
		return state.bundle
	}
	return p.bundle
}

type attemptMetadata struct {
	SinkID               SinkID
	Purpose              Purpose
	DeclaredPersona      Persona
	EndpointID           string
	InvocationID         string
	AttemptOrdinal       uint32
	AttemptReason        string
	ExecutorID           ExecutorID
	ReleaseMode          ReleaseMode
	ReleaseDigest        string
	BundleDigest         string
	ProfileDigest        string
	ConnectionPoolDigest string
	ManagedPolicyDigest  string
	Token                *FinalizationToken
}

type attemptMetadataContextKey struct{}

type AttemptMetadataInput struct {
	SinkID               SinkID
	Purpose              Purpose
	DeclaredPersona      Persona
	EndpointID           string
	InvocationID         string
	AttemptOrdinal       uint32
	AttemptReason        string
	ExecutorID           ExecutorID
	ReleaseMode          ReleaseMode
	ReleaseDigest        string
	BundleDigest         string
	ProfileDigest        string
	ConnectionPoolDigest string
	ManagedPolicyDigest  string
}

func WithAttemptMetadata(ctx context.Context, input AttemptMetadataInput) (context.Context, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	metadata := attemptMetadata{
		SinkID: input.SinkID, Purpose: input.Purpose, DeclaredPersona: input.DeclaredPersona,
		EndpointID:     strings.TrimSpace(input.EndpointID),
		InvocationID:   strings.TrimSpace(input.InvocationID),
		AttemptOrdinal: input.AttemptOrdinal, AttemptReason: strings.TrimSpace(input.AttemptReason),
		ExecutorID:  input.ExecutorID,
		ReleaseMode: input.ReleaseMode, ReleaseDigest: strings.TrimSpace(input.ReleaseDigest),
		BundleDigest:         strings.TrimSpace(input.BundleDigest),
		ProfileDigest:        strings.TrimSpace(input.ProfileDigest),
		ConnectionPoolDigest: strings.TrimSpace(input.ConnectionPoolDigest),
		ManagedPolicyDigest:  strings.TrimSpace(input.ManagedPolicyDigest),
	}
	if existing, ok := attemptMetadataFromContext(ctx); ok {
		if existing.SinkID != metadata.SinkID || existing.Purpose != metadata.Purpose ||
			existing.DeclaredPersona != metadata.DeclaredPersona {
			return nil, errors.New("禁止 facade 覆盖既有官方出站业务 binding")
		}
		if existing.InvocationID != "" && metadata.InvocationID != "" &&
			existing.InvocationID != metadata.InvocationID {
			return nil, errors.New("禁止覆盖既有 InvocationID")
		}
		if existing.AttemptOrdinal != 0 && metadata.AttemptOrdinal != 0 &&
			existing.AttemptOrdinal != metadata.AttemptOrdinal {
			return nil, errors.New("禁止覆盖既有 AttemptOrdinal")
		}
		if existing.EndpointID != "" && metadata.EndpointID != "" &&
			existing.EndpointID != metadata.EndpointID {
			return nil, errors.New("禁止覆盖既有 EndpointID")
		}
		if metadata.EndpointID != "" {
			existing.EndpointID = metadata.EndpointID
		}
		if existing.BundleDigest != "" && metadata.BundleDigest != "" &&
			existing.BundleDigest != metadata.BundleDigest {
			return nil, errors.New("禁止覆盖既有 BundleDigest")
		}
		if existing.ManagedPolicyDigest != "" && metadata.ManagedPolicyDigest != "" &&
			existing.ManagedPolicyDigest != metadata.ManagedPolicyDigest {
			return nil, errors.New("禁止覆盖既有 ManagedEgressPolicy")
		}
		if metadata.InvocationID != "" {
			existing.InvocationID = metadata.InvocationID
		}
		if metadata.AttemptOrdinal != 0 {
			existing.AttemptOrdinal = metadata.AttemptOrdinal
			existing.AttemptReason = metadata.AttemptReason
		}
		if metadata.ExecutorID != "" {
			existing.ExecutorID = metadata.ExecutorID
		}
		if metadata.ReleaseMode.Valid() {
			existing.ReleaseMode = metadata.ReleaseMode
		}
		if metadata.ReleaseDigest != "" {
			existing.ReleaseDigest = metadata.ReleaseDigest
		}
		if metadata.BundleDigest != "" {
			existing.BundleDigest = metadata.BundleDigest
		}
		if metadata.ProfileDigest != "" {
			existing.ProfileDigest = metadata.ProfileDigest
		}
		if metadata.ConnectionPoolDigest != "" {
			existing.ConnectionPoolDigest = metadata.ConnectionPoolDigest
		}
		if metadata.ManagedPolicyDigest != "" {
			existing.ManagedPolicyDigest = metadata.ManagedPolicyDigest
		}
		return context.WithValue(ctx, attemptMetadataContextKey{}, existing), nil
	}
	return context.WithValue(ctx, attemptMetadataContextKey{}, metadata), nil
}

func withFinalizationToken(ctx context.Context, token FinalizationToken) context.Context {
	metadata, _ := attemptMetadataFromContext(ctx)
	cloned := token
	metadata.Token = &cloned
	metadata.ExecutorID = token.payload.AuthorityID
	metadata.EndpointID = token.payload.EndpointID
	metadata.ReleaseDigest = token.payload.ReleaseDigest
	metadata.ProfileDigest = token.payload.ProfileDigest
	metadata.BundleDigest = token.payload.BundleDigest
	metadata.ConnectionPoolDigest = token.payload.ConnectionPoolDigest
	metadata.InvocationID = token.payload.InvocationID
	metadata.AttemptOrdinal = token.payload.AttemptOrdinal
	metadata.AttemptReason = token.payload.AttemptReason
	return context.WithValue(ctx, attemptMetadataContextKey{}, metadata)
}

// AttemptIdentity 是不暴露 Token 内容的只读诊断视图，供发送栈契约测试确认
// SinkID 没有被共享 facade 覆盖。
type AttemptIdentity struct {
	SinkID               SinkID
	Purpose              Purpose
	DeclaredPersona      Persona
	EndpointID           string
	InvocationID         string
	AttemptOrdinal       uint32
	AttemptReason        string
	ExecutorID           ExecutorID
	ReleaseMode          ReleaseMode
	ReleaseDigest        string
	BundleDigest         string
	ProfileDigest        string
	ConnectionPoolDigest string
	ManagedPolicyDigest  string
	HasFinalizationToken bool
}

func AttemptIdentityFromContext(ctx context.Context) (AttemptIdentity, bool) {
	metadata, ok := attemptMetadataFromContext(ctx)
	if !ok {
		return AttemptIdentity{}, false
	}
	return AttemptIdentity{
		SinkID:               metadata.SinkID,
		Purpose:              metadata.Purpose,
		DeclaredPersona:      metadata.DeclaredPersona,
		EndpointID:           metadata.EndpointID,
		InvocationID:         metadata.InvocationID,
		AttemptOrdinal:       metadata.AttemptOrdinal,
		AttemptReason:        metadata.AttemptReason,
		ExecutorID:           metadata.ExecutorID,
		ReleaseMode:          metadata.ReleaseMode,
		ReleaseDigest:        metadata.ReleaseDigest,
		BundleDigest:         metadata.BundleDigest,
		ProfileDigest:        metadata.ProfileDigest,
		ConnectionPoolDigest: metadata.ConnectionPoolDigest,
		ManagedPolicyDigest:  metadata.ManagedPolicyDigest,
		HasFinalizationToken: metadata.Token != nil,
	}, true
}

func attemptMetadataFromContext(ctx context.Context) (attemptMetadata, bool) {
	if ctx == nil {
		return attemptMetadata{}, false
	}
	metadata, ok := ctx.Value(attemptMetadataContextKey{}).(attemptMetadata)
	if !ok {
		return attemptMetadata{}, false
	}
	if metadata.Token != nil {
		cloned := *metadata.Token
		metadata.Token = &cloned
	}
	return metadata, true
}

func requestDigest(
	req *http.Request,
	normalization WireNormalizationPlan,
	protocol WireProtocol,
) (string, error) {
	if req == nil || req.URL == nil {
		return "", errors.New("请求为空")
	}
	if err := normalization.Validate(protocol); err != nil {
		return "", err
	}
	hash := sha256.New()
	_, _ = io.WriteString(hash, strings.ToUpper(req.Method))
	// ForceQuery 是独立摘要字段：签发后新增或删除裸 “?” 必须改变摘要，即使 RawQuery
	// 保持为空也不能与无 “?” 的请求同摘要。
	_, _ = io.WriteString(hash, "\x00"+canonicalRequestScheme(req.URL.Scheme, protocol)+"\x00"+req.URL.Host+"\x00"+req.URL.EscapedPath()+"\x00"+strconv.FormatBool(req.URL.ForceQuery)+"\x00"+req.URL.RawQuery)
	effectiveHost := req.Host
	if effectiveHost == "" {
		effectiveHost = req.URL.Host
	}
	_, _ = io.WriteString(hash, "\x00"+effectiveHost)
	canonicalHeaders := make(map[string][]string, len(req.Header))
	for name, values := range req.Header {
		if normalization.SuppressDefaultUserAgent && name == "User-Agent" && allHeaderValuesEmpty(values) {
			// lowercase adapter 使用这个私有 wire 哨兵阻止 Go 自动补默认 UA；它不属于
			// 业务语义，最终形态由 validateFinalWireNormalization 单独验证。
			continue
		}
		canonicalName := strings.ToLower(strings.TrimSpace(name))
		for _, value := range values {
			canonicalHeaders[canonicalName] = append(
				canonicalHeaders[canonicalName],
				canonicalHeaderValue(canonicalName, value, normalization),
			)
		}
	}
	// coder/websocket 会在真正写握手时才加入压缩 offer。FinalizationToken 必须
	// 绑定画像声明的最终 wire，而不是要求 service 在签名前伪造 transport 自动头。
	// 因此“签名时尚未出现”与“terminal 已按计划加入”具有同一个语义摘要；
	// 非计划值仍由 validateFinalWireNormalization fail-close。
	if protocol == WireProtocolWebSocket {
		// 这些头由 coder/websocket 在 RoundTripper 前生成。Token 绑定画像允许的
		// 最终形态，其中随机 Sec-WebSocket-Key 只绑定“由 transport 生成且非空”
		// 的类型事实，不绑定一次性随机值。
		canonicalHeaders["connection"] = []string{"Upgrade"}
		canonicalHeaders["upgrade"] = []string{"websocket"}
		canonicalHeaders["sec-websocket-version"] = []string{"13"}
		canonicalHeaders["sec-websocket-key"] = []string{"<transport-generated>"}
		if strings.TrimSpace(normalization.WebSocketCompressionOffer) != "" {
			canonicalHeaders["sec-websocket-extensions"] = []string{
				canonicalHeaderValue(
					"sec-websocket-extensions",
					normalization.WebSocketCompressionOffer,
					normalization,
				),
			}
		} else {
			delete(canonicalHeaders, "sec-websocket-extensions")
		}
	}
	names := make([]string, 0, len(canonicalHeaders))
	for name := range canonicalHeaders {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		_, _ = io.WriteString(hash, "\x00"+name)
		for _, value := range canonicalHeaders[name] {
			_, _ = io.WriteString(hash, "\x00"+value)
		}
	}
	if req.GetBody != nil {
		body, err := req.GetBody()
		if err != nil {
			return "", err
		}
		defer func() { _ = body.Close() }()
		if _, err := io.Copy(hash, body); err != nil {
			return "", err
		}
	} else if req.Body != nil && req.Body != http.NoBody {
		attestation, ok := req.Context().Value(singleUseBodyContextKey{}).(singleUseBodyAttestation)
		stream, streamOK := req.Body.(*executorSingleUseBody)
		if !ok || !streamOK || stream.capability != attestation.capability ||
			attestation.contentLength != req.ContentLength {
			return "", errors.New("single-use stream capability 不匹配")
		}
		_, _ = io.WriteString(hash, "\x00single-use:")
		_, _ = io.WriteString(hash, hex.EncodeToString(attestation.capability[:]))
		_, _ = io.WriteString(hash, fmt.Sprintf(":%d", attestation.contentLength))
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// canonicalRequestScheme 将 WebSocket 拨号器交给 HTTP RoundTripper 前必然发生的
// 协议别名转换收敛为同一个摘要事实。coder/websocket 会把 wss/ws 分别改写为
// https/http；二者只是在不同调用层表达同一条安全或非安全连接，不能被 Guard
// 误判为定型后篡改。安全与非安全连接仍保持不同摘要。
func canonicalRequestScheme(scheme string, protocol WireProtocol) string {
	normalized := strings.ToLower(strings.TrimSpace(scheme))
	if protocol != WireProtocolWebSocket {
		return normalized
	}
	switch normalized {
	case "https":
		return "wss"
	case "http":
		return "ws"
	default:
		return normalized
	}
}

type singleUseBodyContextKey struct{}

type singleUseBodyAttestation struct {
	capability    [sha256.Size]byte
	contentLength int64
}

type executorSingleUseBody struct {
	io.ReadCloser
	capability [sha256.Size]byte
}

func allHeaderValuesEmpty(values []string) bool {
	for _, value := range values {
		if value != "" {
			return false
		}
	}
	return true
}

func canonicalHeaderValue(name, value string, normalization WireNormalizationPlan) string {
	if name != "sec-websocket-extensions" || normalization.WebSocketCompressionOffer == "" {
		return value
	}
	trimmed := strings.TrimSpace(value)
	if strings.EqualFold(trimmed, "permessage-deflate") ||
		strings.EqualFold(trimmed, strings.TrimSpace(normalization.WebSocketCompressionOffer)) {
		return strings.TrimSpace(normalization.WebSocketCompressionOffer)
	}
	return value
}

func validateFinalWireNormalization(
	req *http.Request,
	normalization WireNormalizationPlan,
	protocol WireProtocol,
) error {
	if req == nil {
		return errors.New("最终 wire 请求为空")
	}
	if err := normalization.Validate(protocol); err != nil {
		return err
	}
	if normalization.HeaderMode == HeaderNormalizationLowercase {
		for name, values := range req.Header {
			if normalization.SuppressDefaultUserAgent && name == "User-Agent" && allHeaderValuesEmpty(values) {
				continue
			}
			if name != strings.ToLower(name) {
				return fmt.Errorf("最终 header casing 不符合 lowercase 计划: %s", name)
			}
		}
		if normalization.SuppressDefaultUserAgent {
			values, exists := req.Header["User-Agent"]
			if !exists || !allHeaderValuesEmpty(values) {
				return errors.New("最终请求缺少 Go User-Agent 抑制哨兵")
			}
		}
	}
	if protocol == WireProtocolWebSocket {
		connection := strings.TrimSpace(req.Header.Get("Connection"))
		upgrade := strings.TrimSpace(req.Header.Get("Upgrade"))
		version := strings.TrimSpace(req.Header.Get("Sec-WebSocket-Version"))
		key := strings.TrimSpace(req.Header.Get("Sec-WebSocket-Key"))
		extensions := strings.TrimSpace(req.Header.Get("Sec-WebSocket-Extensions"))
		generatedPresent := connection != "" || upgrade != "" || version != "" || key != "" || extensions != ""
		if generatedPresent {
			if !strings.EqualFold(connection, "Upgrade") ||
				!strings.EqualFold(upgrade, "websocket") || version != "13" || key == "" {
				return errors.New("最终 WebSocket transport 自动握手头不符合计划")
			}
			offer := strings.TrimSpace(normalization.WebSocketCompressionOffer)
			if extensions != offer {
				return errors.New("最终 WebSocket compression offer 不符合计划")
			}
		}
	}
	return nil
}

func requestFromCompiled(ctx context.Context, compiled CompiledRequest) (*http.Request, error) {
	if compiled.url == nil {
		return nil, errors.New("CompiledRequest URL 为空")
	}
	if body, replayable := compiled.body.replayableView(); replayable {
		req, err := http.NewRequestWithContext(ctx, compiled.method, compiled.url.String(), bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header = compiled.headers.Clone()
		req.ContentLength = int64(len(body))
		req.GetBody = func() (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(body)), nil
		}
		return req, nil
	}
	stream, capability, contentLength, err := compiled.body.takeSingleUse()
	if err != nil {
		return nil, err
	}
	wrapped := &executorSingleUseBody{ReadCloser: stream, capability: capability}
	ctx = context.WithValue(ctx, singleUseBodyContextKey{}, singleUseBodyAttestation{
		capability: capability, contentLength: contentLength,
	})
	req, err := http.NewRequestWithContext(ctx, compiled.method, compiled.url.String(), wrapped)
	if err != nil {
		_ = stream.Close()
		return nil, err
	}
	req.Header = compiled.headers.Clone()
	req.ContentLength = contentLength
	req.GetBody = nil
	return req, nil
}
