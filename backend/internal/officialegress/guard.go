package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

var ErrGuardRejected = errors.New("official egress Guard 拒绝发送")

type GuardConfig struct {
	UnknownRoutePolicy     UnknownRoutePolicy
	UnregisteredSinkPolicy UnregisteredSinkPolicy
	CanaryPercent          uint8
	MaxUniqueLogSamples    int
	InstanceID             string
	PolicyOverrides        []GuardPolicyOverride
}

func (c GuardConfig) normalize() (GuardConfig, error) {
	if c.UnknownRoutePolicy == "" {
		c.UnknownRoutePolicy = UnknownRoutePolicy(PolicyEnforce)
	}
	if c.UnregisteredSinkPolicy == "" {
		c.UnregisteredSinkPolicy = UnregisteredSinkPolicy(PolicyEnforce)
	}
	if !c.UnknownRoutePolicy.Valid() || !c.UnregisteredSinkPolicy.Valid() {
		return GuardConfig{}, errors.New("Guard 的 unknown/unregistered policy 非法")
	}
	if c.CanaryPercent > 100 {
		return GuardConfig{}, errors.New("Guard canary_percent 必须在 0..100")
	}
	if c.MaxUniqueLogSamples <= 0 {
		c.MaxUniqueLogSamples = 512
	}
	c.InstanceID = strings.TrimSpace(c.InstanceID)
	for index, override := range c.PolicyOverrides {
		if err := override.Validate(); err != nil {
			return GuardConfig{}, fmt.Errorf("Guard policy override[%d]：%w", index, err)
		}
		if c.InstanceID == "" || override.InstanceID != c.InstanceID {
			return GuardConfig{}, errors.New("Guard policy override 未绑定当前 instance_id")
		}
	}
	c.PolicyOverrides = append([]GuardPolicyOverride(nil), c.PolicyOverrides...)
	return c, nil
}

type GuardDecision struct {
	Allow           bool
	Scope           EgressScope
	Route           ResolvedOfficialRoute
	SinkState       SinkEnforcementState
	Reasons         []GuardReason
	RejectionReason GuardReason
	// Diagnostic 只在拒绝时填充，描述形态差异，不含头值。
	Diagnostic string
}

type GuardRejectionError struct {
	Reason GuardReason
	SinkID SinkID
	Route  RouteKey
	// Diagnostic 只描述形态差异（例如最终 header 名集合），不含任何头值或凭据，
	// 用于把 request_modified_after_finalize 这类同名原因区分到具体环节。
	Diagnostic string
}

func (e *GuardRejectionError) Error() string {
	if e == nil {
		return ErrGuardRejected.Error()
	}
	message := fmt.Sprintf("%s: reason=%s sink_id=%s route=%s", ErrGuardRejected, e.Reason, e.SinkID, e.Route)
	if strings.TrimSpace(e.Diagnostic) != "" {
		message += " diagnostic=" + e.Diagnostic
	}
	return message
}

func (e *GuardRejectionError) Unwrap() error { return ErrGuardRejected }

// Guard 在四类发送栈的 terminal 边界执行同一状态机。
type Guard struct {
	config           GuardConfig
	sinks            SinkCatalog
	routes           OfficialRouteCatalog
	scope            EgressScopeCatalog
	recorder         GuardRecorder
	now              func() time.Time
	recorderFailures atomic.Uint64

	issuerMu sync.RWMutex
	issuers  map[ExecutorID]*tokenIssuer
}

func NewGuard(
	config GuardConfig,
	sinks SinkCatalog,
	routes OfficialRouteCatalog,
	recorder GuardRecorder,
) (*Guard, error) {
	normalized, err := config.normalize()
	if err != nil {
		return nil, err
	}
	if len(sinks.bindings) == 0 || len(routes.entries) == 0 {
		return nil, errors.New("Guard 缺少 SinkCatalog 或 OfficialRouteCatalog")
	}
	if normalized.CanaryPercent == 0 {
		for _, binding := range sinks.Bindings() {
			if binding.EnforcementState() == SinkStateCanaryEnforce {
				return nil, errors.New("Guard 禁止 canary_enforce Sink 使用 0% canary；全量 observe 必须使用限时覆盖")
			}
		}
	}
	if recorder == nil {
		recorder = NewBoundedGuardRecorder(normalized.MaxUniqueLogSamples, nil)
	}
	return &Guard{
		config: normalized, sinks: sinks, routes: routes,
		scope: NewEgressScopeCatalog(sinks, routes), recorder: recorder,
		now: time.Now, issuers: make(map[ExecutorID]*tokenIssuer),
	}, nil
}

func (g *Guard) Config() GuardConfig {
	if g == nil {
		return GuardConfig{}
	}
	config := g.config
	config.PolicyOverrides = append([]GuardPolicyOverride(nil), g.config.PolicyOverrides...)
	return config
}

func (g *Guard) Recorder() GuardRecorder {
	if g == nil {
		return nil
	}
	return g.recorder
}

// ProcessSinkCatalog 返回 Guard 实际执行的同一份进程级 Catalog 快照。
// BundleResolver 与 Executor 必须共享该快照，避免 runtime control 应用后各组件观察到
// 不同 enforcement state。
func (g *Guard) ProcessSinkCatalog() SinkCatalog {
	if g == nil {
		return SinkCatalog{}
	}
	return g.sinks
}

// RecorderFailureCount 只用于健康指标；记录器故障绝不能改变 Guard 的发送判定。
func (g *Guard) RecorderFailureCount() uint64 {
	if g == nil {
		return 0
	}
	return g.recorderFailures.Load()
}

func (g *Guard) registerIssuer(issuer *tokenIssuer) error {
	if g == nil || issuer == nil || issuer.id == "" || !issuer.authorityKind.Valid() ||
		!issuer.persona.Valid() || issuer.persona == PersonaUnclassified ||
		issuer.persona == PersonaDeadCode {
		return errors.New("Guard 无法登记空 Executor issuer")
	}
	g.issuerMu.Lock()
	defer g.issuerMu.Unlock()
	if existing, ok := g.issuers[issuer.id]; ok && existing != issuer {
		return fmt.Errorf("ExecutorID 重复登记: %s", issuer.id)
	}
	g.issuers[issuer.id] = issuer
	return nil
}

func (g *Guard) tokenTrusted(token *FinalizationToken) bool {
	if g == nil || token == nil {
		return false
	}
	g.issuerMu.RLock()
	issuer := g.issuers[token.payload.IssuerID]
	g.issuerMu.RUnlock()
	return issuer != nil && issuer.verify(*token)
}

func (g *Guard) Evaluate(req *http.Request, backend BackendKind, protocol WireProtocol) GuardDecision {
	return g.evaluate(req, backend, protocol, true)
}

// EvaluateConnectionAdmission 校验每一次长连接获取所携带的当前 invocation Token。
// Admission 不校验仅在新拨号握手中存在的最终 wire 摘要；terminal Guard 仍负责该部分。
func (g *Guard) EvaluateConnectionAdmission(
	req *http.Request,
	backend BackendKind,
	protocol WireProtocol,
) GuardDecision {
	return g.evaluate(req, backend, protocol, false)
}

func (g *Guard) evaluate(
	req *http.Request,
	backend BackendKind,
	protocol WireProtocol,
	terminal bool,
) GuardDecision {
	decision := GuardDecision{Allow: false, Scope: EgressScopeManaged}
	if g == nil || req == nil || req.URL == nil || !backend.Valid() || !protocol.Valid() {
		decision.Reasons = []GuardReason{ReasonSinkBindingMismatch}
		decision.RejectionReason = ReasonSinkBindingMismatch
		return decision
	}
	metadata, _ := attemptMetadataFromContext(req.Context())
	trustedToken := g.tokenTrusted(metadata.Token)
	scopeDecision := g.scope.Classify(req.Method, req.URL, protocol, metadata, trustedToken)
	decision.Scope = scopeDecision.Scope
	if scopeDecision.Scope == EgressScopeOutOfScope {
		decision.Allow = true
		decision.Reasons = []GuardReason{ReasonOutOfScopePassthrough}
		g.record(req, backend, protocol, metadata, ResolvedOfficialRoute{}, "",
			ReasonOutOfScopePassthrough, scopeDecision.PathTemplate)
		return decision
	}

	physicalRoute, physicalOK := g.routes.MatchPhysical(req.Method, req.URL, protocol)
	if !physicalOK {
		decision.Reasons = []GuardReason{ReasonUnknownRoute}
		g.record(req, backend, protocol, metadata, ResolvedOfficialRoute{}, "",
			ReasonUnknownRoute, scopeDecision.PathTemplate)
		if g.config.UnknownRoutePolicy == UnknownRoutePolicy(PolicyObserve) ||
			g.policyOverrideActive(
				GuardPolicyOverrideUnknownRoute, req, protocol, metadata.SinkID,
				scopeDecision.PathTemplate, scopeDecision.PathSHA256,
			) {
			decision.Allow = true
			observedReason := ReasonUnknownRouteObserved
			if g.config.UnknownRoutePolicy != UnknownRoutePolicy(PolicyObserve) {
				observedReason = ReasonUnknownRouteOverrideObserved
			}
			decision.Reasons = append(decision.Reasons, observedReason)
			g.record(req, backend, protocol, metadata, ResolvedOfficialRoute{}, "",
				observedReason, scopeDecision.PathTemplate)
			return decision
		}
		decision.RejectionReason = ReasonUnknownRoute
		return decision
	}
	// 此时只确认了物理 route。purpose/persona/endpoint 必须等 SinkCatalog 提供
	// 权威 binding 后才能解析，不能从请求 context 预先选择。
	physicalView := ResolvedOfficialRoute{
		Key: RouteKey{
			Method: physicalRoute.Method, Host: physicalRoute.HostTemplate,
			Path: physicalRoute.PathTemplate,
		},
		Protocol: physicalRoute.Protocol,
	}
	decision.Route = physicalView

	if metadata.SinkID == "" {
		return g.unregisteredDecision(
			decision, req, backend, protocol, metadata, physicalView,
			ReasonMissingSinkID,
		)
	}
	binding, registered := g.sinks.Resolve(metadata.SinkID)
	if !registered || !binding.RuntimeBindable() {
		return g.unregisteredDecision(
			decision, req, backend, protocol, metadata, physicalView,
			ReasonUnregisteredSink,
		)
	}
	decision.SinkState = binding.EnforcementState()
	resolvedRoute, bindingRouteOK := g.routes.ResolveBinding(req.Method, req.URL, protocol, binding)
	if !bindingRouteOK {
		return g.unregisteredDecision(
			decision, req, backend, protocol, metadata, physicalView,
			ReasonSinkBindingMismatch,
		)
	}
	decision.Route = resolvedRoute

	tupleReasons, tupleMismatch := g.bindingTupleReasons(binding, resolvedRoute, metadata, backend, protocol)
	if tupleMismatch {
		for _, reason := range tupleReasons {
			g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(), reason, "")
		}
		decision.Reasons = append(decision.Reasons, tupleReasons...)
		decision = g.unregisteredDecision(
			decision, req, backend, protocol, metadata, resolvedRoute,
			ReasonSinkBindingMismatch,
		)
		return decision
	}

	if binding.Persona() == PersonaUnclassified {
		decision.Reasons = append(decision.Reasons, ReasonUnclassifiedPersona)
		g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(),
			ReasonUnclassifiedPersona, "")
	}
	tokenReasons := make([]GuardReason, 0)
	if policy, managed := binding.ManagedPolicy(); managed {
		if metadata.ManagedPolicyDigest != policy.Digest() || metadata.Token != nil {
			tokenReasons = append(tokenReasons, ReasonManagedPolicyMismatch)
		}
	} else {
		tokenReasons = g.finalizationIdentityReasons(
			binding, resolvedRoute, metadata, backend, protocol, trustedToken,
		)
		if terminal && len(tokenReasons) == 0 {
			wireReasons, wireDiagnostic := g.finalizationWireReasons(req, metadata, protocol)
			tokenReasons = append(tokenReasons, wireReasons...)
			decision.Diagnostic = wireDiagnostic
		}
	}
	decision.Reasons = append(decision.Reasons, tokenReasons...)
	for _, reason := range tokenReasons {
		g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(), reason, "")
	}

	if override, ok := binding.Override(); ok && override.Active(g.now()) {
		decision.Allow = true
		decision.Reasons = append(decision.Reasons, ReasonSinkOverrideObserved)
		g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(),
			ReasonSinkOverrideObserved, "")
		return decision
	}

	switch binding.EnforcementState() {
	case SinkStateLegacyObserve:
		decision.Allow = true
		decision.Reasons = append(decision.Reasons, ReasonLegacyObservePassthrough)
		g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(),
			ReasonLegacyObservePassthrough, "")
		return decision
	case SinkStateCanaryEnforce:
		if !g.inCanary(metadata, resolvedRoute) {
			decision.Allow = true
			decision.Reasons = append(decision.Reasons, ReasonCanaryObservePassthrough)
			g.record(req, backend, protocol, metadata, resolvedRoute, binding.EnforcementState(),
				ReasonCanaryObservePassthrough, "")
			return decision
		}
	case SinkStateEnforced:
	default:
		decision.Reasons = append(decision.Reasons, ReasonSinkBindingMismatch)
		tokenReasons = append(tokenReasons, ReasonSinkBindingMismatch)
	}

	if len(tokenReasons) != 0 {
		decision.RejectionReason = tokenReasons[0]
		return decision
	}
	decision.Allow = true
	return decision
}

func (g *Guard) unregisteredDecision(
	decision GuardDecision,
	req *http.Request,
	backend BackendKind,
	protocol WireProtocol,
	metadata attemptMetadata,
	route ResolvedOfficialRoute,
	reason GuardReason,
) GuardDecision {
	decision.Reasons = append(decision.Reasons, reason)
	g.record(req, backend, protocol, metadata, route, decision.SinkState, reason, "")
	if g.config.UnregisteredSinkPolicy == UnregisteredSinkPolicy(PolicyObserve) ||
		g.policyOverrideActive(
			GuardPolicyOverrideUnregisteredSink, req, protocol, metadata.SinkID, route.Key.Path, "",
		) {
		decision.Allow = true
		observedReason := ReasonUnregisteredSinkObserved
		if g.config.UnregisteredSinkPolicy != UnregisteredSinkPolicy(PolicyObserve) {
			observedReason = ReasonUnregisteredSinkOverrideObserved
		}
		decision.Reasons = append(decision.Reasons, observedReason)
		g.record(req, backend, protocol, metadata, route, decision.SinkState,
			observedReason, "")
		return decision
	}
	decision.Allow = false
	decision.RejectionReason = reason
	return decision
}

func (g *Guard) policyOverrideActive(
	policy GuardPolicyOverrideKind,
	req *http.Request,
	protocol WireProtocol,
	sinkID SinkID,
	pathTemplate string,
	pathSHA256 string,
) bool {
	if g == nil || req == nil || req.URL == nil || pathTemplate == "" {
		return false
	}
	now := g.now()
	for _, override := range g.config.PolicyOverrides {
		if override.active(now) && override.matches(
			policy, g.config.InstanceID, req.Method, req.URL.Hostname(),
			pathTemplate, pathSHA256, protocol, sinkID,
		) {
			return true
		}
	}
	return false
}

func (g *Guard) bindingTupleReasons(
	binding SinkBinding,
	route ResolvedOfficialRoute,
	metadata attemptMetadata,
	backend BackendKind,
	protocol WireProtocol,
) ([]GuardReason, bool) {
	reasons := make([]GuardReason, 0, 2)
	if metadata.DeclaredPersona != route.Persona {
		reasons = append(reasons, ReasonRoutePersonaMismatch)
	}
	mismatch := metadata.DeclaredPersona != route.Persona ||
		binding.ID() != metadata.SinkID || binding.Purpose() != metadata.Purpose ||
		binding.Persona() != metadata.DeclaredPersona || !binding.backendAllowed(backend) ||
		!bindingHasRoute(binding, route, protocol)
	if mismatch {
		if !binding.backendAllowed(backend) {
			reasons = append(reasons, ReasonWrongBackend)
		}
	}
	return uniqueGuardReasons(reasons), mismatch
}

func bindingHasRoute(binding SinkBinding, resolved ResolvedOfficialRoute, protocol WireProtocol) bool {
	for _, route := range binding.Routes() {
		if route.Protocol == protocol && route.Key == resolved.Key {
			return true
		}
	}
	return false
}

func (g *Guard) finalizationIdentityReasons(
	binding SinkBinding,
	route ResolvedOfficialRoute,
	metadata attemptMetadata,
	backend BackendKind,
	protocol WireProtocol,
	trustedToken bool,
) []GuardReason {
	if metadata.Token == nil {
		return []GuardReason{ReasonMissingFinalizationToken, ReasonWrongExecutor}
	}
	if !trustedToken {
		return []GuardReason{ReasonWrongExecutor}
	}
	token := metadata.Token.payload
	reasons := make([]GuardReason, 0, 4)
	expectedEvidenceID := route.EndpointID
	if binding.migrationReceipt != nil {
		claim, ok := binding.migrationReceipt.claimFor(CatalogRoute{Key: route.Key, Protocol: protocol})
		if !ok || binding.migrationReceipt.authorityID != metadata.ExecutorID ||
			binding.migrationReceipt.authorityKind != token.AuthorityKind ||
			binding.migrationReceipt.authorityID != token.AuthorityID ||
			binding.migrationReceipt.tokenIssuerID != token.IssuerID ||
			claim.backend != backend || claim.adapterID != token.AdapterID ||
			!claim.matchesTransport(token.ReleaseDigest, token.TransportID) ||
			claim.evidenceID != token.EndpointID {
			reasons = append(reasons, ReasonWrongExecutor)
		} else {
			expectedEvidenceID = claim.evidenceID
		}
	} else if metadata.ExecutorID != token.AuthorityID {
		reasons = append(reasons, ReasonWrongExecutor)
	}
	if token.SinkID != binding.ID() || token.Route != route.Key || token.Persona != binding.Persona() ||
		token.Protocol != protocol || token.EndpointID != expectedEvidenceID ||
		(metadata.EndpointID != "" && metadata.EndpointID != token.EndpointID) ||
		metadata.InvocationID != token.InvocationID ||
		metadata.AttemptOrdinal == 0 || metadata.AttemptOrdinal != token.AttemptOrdinal ||
		metadata.AttemptReason != token.AttemptReason {
		reasons = append(reasons, ReasonWrongExecutor)
	}
	if token.Backend != backend {
		reasons = append(reasons, ReasonWrongBackend)
	}
	if metadata.ReleaseDigest == "" || token.ReleaseDigest != metadata.ReleaseDigest ||
		metadata.ProfileDigest == "" || token.ProfileDigest != metadata.ProfileDigest ||
		metadata.BundleDigest == "" || token.BundleDigest != metadata.BundleDigest {
		reasons = append(reasons, ReasonReleaseDigestMismatch)
	}
	if metadata.ConnectionPoolDigest == "" ||
		token.ConnectionPoolDigest != metadata.ConnectionPoolDigest {
		reasons = append(reasons, ReasonConnectionPoolMismatch)
	}
	return uniqueGuardReasons(reasons)
}

func (g *Guard) finalizationWireReasons(
	req *http.Request,
	metadata attemptMetadata,
	protocol WireProtocol,
) ([]GuardReason, string) {
	if metadata.Token == nil {
		return []GuardReason{ReasonMissingFinalizationToken, ReasonWrongExecutor}, ""
	}
	token := metadata.Token.payload
	reasons := make([]GuardReason, 0, 1)
	// 规范化不符与摘要不符共用同一个 GuardReason，排查时必须能区分：前者指向 header
	// 形态，后者指向定型后被改写。诊断只描述形态，不含任何头值。
	diagnostics := make([]string, 0, 2)
	if err := validateFinalWireNormalization(req, token.Normalization, protocol); err != nil {
		diagnostics = append(diagnostics, "normalization="+err.Error())
		reasons = append(reasons, ReasonRequestModifiedAfterFinalize)
	}
	digest, err := requestDigest(req, token.Normalization, protocol)
	switch {
	case err != nil:
		diagnostics = append(diagnostics, "digest_error="+err.Error())
		reasons = append(reasons, ReasonRequestModifiedAfterFinalize)
	case digest != token.RequestDigest:
		diagnostics = append(
			diagnostics,
			"digest_mismatch final_header_names=["+strings.Join(sortedHeaderNames(req), ",")+"]",
		)
		reasons = append(reasons, ReasonRequestModifiedAfterFinalize)
	}
	return uniqueGuardReasons(reasons), strings.Join(diagnostics, "; ")
}

// sortedHeaderNames 只返回 header 名，用于定位定型后被谁加了头；绝不返回头值。
func sortedHeaderNames(req *http.Request) []string {
	if req == nil {
		return nil
	}
	names := make([]string, 0, len(req.Header))
	for name := range req.Header {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func uniqueGuardReasons(input []GuardReason) []GuardReason {
	seen := make(map[GuardReason]struct{}, len(input))
	out := make([]GuardReason, 0, len(input))
	for _, reason := range input {
		if _, exists := seen[reason]; exists {
			continue
		}
		seen[reason] = struct{}{}
		out = append(out, reason)
	}
	return out
}

func (g *Guard) inCanary(metadata attemptMetadata, route ResolvedOfficialRoute) bool {
	if g.config.CanaryPercent == 0 {
		return false
	}
	if g.config.CanaryPercent >= 100 {
		return true
	}
	seed := strings.Join([]string{
		string(metadata.SinkID), string(metadata.Purpose), metadata.InvocationID,
		route.Key.String(),
	}, "\x00")
	sum := sha256.Sum256([]byte(seed))
	return uint8(sum[0]%100) < g.config.CanaryPercent
}

func (g *Guard) record(
	req *http.Request,
	backend BackendKind,
	protocol WireProtocol,
	metadata attemptMetadata,
	route ResolvedOfficialRoute,
	state SinkEnforcementState,
	reason GuardReason,
	fallbackPath string,
) {
	if g == nil || g.recorder == nil || req == nil || req.URL == nil {
		return
	}
	hostTemplate := normalizeRouteHost(req.URL.Hostname())
	pathTemplate := fallbackPath
	resolvedPersona := route.Persona
	if route.Key.Host != "" {
		hostTemplate = route.Key.Host
	}
	if route.Key.Path != "" {
		pathTemplate = route.Key.Path
	}
	if pathTemplate == "" {
		pathTemplate = normalizedScopePath(req.URL)
	}
	scope := EgressScopeManaged
	if reason == ReasonOutOfScopePassthrough {
		scope = EgressScopeOutOfScope
	}
	g.recordSafely(GuardEvent{
		Reason: reason, Scope: scope, SinkID: metadata.SinkID,
		Method: strings.ToUpper(req.Method), HostTemplate: hostTemplate, PathTemplate: pathTemplate,
		DeclaredPersona: metadata.DeclaredPersona, ResolvedPersona: resolvedPersona,
		Backend: backend, Protocol: protocol, ProfileDigest: safeProfileDigest(metadata.ProfileDigest),
		EnforcementState: state,
	})
}

func (g *Guard) recordSafely(event GuardEvent) {
	if g == nil || g.recorder == nil {
		return
	}
	defer func() {
		if recover() != nil {
			g.recorderFailures.Add(1)
		}
	}()
	if err := g.recorder.RecordOfficialEgressEvent(event); err != nil {
		g.recorderFailures.Add(1)
	}
}

func safeProfileDigest(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) == sha256.Size*2 {
		if _, err := hex.DecodeString(value); err == nil {
			return strings.ToLower(value)
		}
	}
	sum := sha256.Sum256([]byte(value))
	return "sha256:" + hex.EncodeToString(sum[:])
}

type guardedRoundTripper struct {
	base     http.RoundTripper
	guard    *Guard
	backend  BackendKind
	protocol WireProtocol
}

// HTTPTransportFacts 是 Guard 所包裹标准库 transport 的只读诊断事实。
// 它不暴露底层 RoundTripper，避免调用方取得引用后绕过 Guard 直接发送。
type HTTPTransportFacts struct {
	ResponseHeaderTimeout  time.Duration
	TLSHandshakeTimeout    time.Duration
	ForceAttemptHTTP2      bool
	TLSNextProtoConfigured bool
	MaxConnsPerHost        int
	MaxIdleConns           int
	MaxIdleConnsPerHost    int
	DisableCompression     bool
}

// InspectHTTPTransport 只读取 Guard 直属底层的标准库 transport 参数。
// 非 Guard、嵌套其他 wire wrapper 或非标准库 transport 均返回 false。
func InspectHTTPTransport(transport http.RoundTripper) (HTTPTransportFacts, bool) {
	guarded, ok := transport.(*guardedRoundTripper)
	if !ok || guarded == nil {
		return HTTPTransportFacts{}, false
	}
	base, ok := guarded.base.(*http.Transport)
	if !ok || base == nil {
		return HTTPTransportFacts{}, false
	}
	return HTTPTransportFacts{
		ResponseHeaderTimeout:  base.ResponseHeaderTimeout,
		TLSHandshakeTimeout:    base.TLSHandshakeTimeout,
		ForceAttemptHTTP2:      base.ForceAttemptHTTP2,
		TLSNextProtoConfigured: base.TLSNextProto != nil,
		MaxConnsPerHost:        base.MaxConnsPerHost,
		MaxIdleConns:           base.MaxIdleConns,
		MaxIdleConnsPerHost:    base.MaxIdleConnsPerHost,
		DisableCompression:     base.DisableCompression,
	}, true
}

// NewGuardedRoundTripper 把 Guard 放在实际 socket RoundTripper 之前。
// guard=nil 时每次读取进程默认 Guard，便于 wiring 完成后原子更新配置。
func NewGuardedRoundTripper(
	base http.RoundTripper,
	guard *Guard,
	backend BackendKind,
	protocol WireProtocol,
) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return &guardedRoundTripper{base: base, guard: guard, backend: backend, protocol: protocol}
}

func (r *guardedRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if r == nil || r.base == nil {
		return nil, errors.New("official egress Guard transport 为空")
	}
	guard := r.guard
	if guard == nil {
		guard = DefaultGuard()
	}
	decision := guard.Evaluate(req, r.backend, r.protocol)
	if !decision.Allow {
		return nil, WrapRuntimeError(RuntimeErrorCodeGuardRejected, "guard.round_trip", &GuardRejectionError{
			Reason:     decision.RejectionReason,
			SinkID:     attemptSinkID(req),
			Route:      decision.Route.Key,
			Diagnostic: decision.Diagnostic,
		})
	}
	return r.base.RoundTrip(req)
}

func (r *guardedRoundTripper) CloseIdleConnections() {
	if r == nil || r.base == nil {
		return
	}
	if closer, ok := r.base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}

func attemptSinkID(req *http.Request) SinkID {
	if req == nil {
		return ""
	}
	metadata, _ := attemptMetadataFromContext(req.Context())
	return metadata.SinkID
}

var defaultGuardPointer atomic.Pointer[Guard]

func init() {
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
	}, defaultSinkCatalog, defaultRouteCatalog, nil)
	if err != nil {
		panic(fmt.Sprintf("初始化 official egress Guard: %v", err))
	}
	defaultGuardPointer.Store(guard)
}

func DefaultGuard() *Guard {
	return defaultGuardPointer.Load()
}

func ConfigureDefaultGuard(config GuardConfig, recorder GuardRecorder) (*Guard, error) {
	return ConfigureDefaultGuardWithSinkCatalog(config, defaultSinkCatalog, recorder)
}

// ConfigureDefaultGuardWithSinkCatalog 允许生产 wiring 在启动时注入已校验的
// 单 Sink 回滚快照；路由闭集仍使用同一不可变默认 Catalog。
func ConfigureDefaultGuardWithSinkCatalog(
	config GuardConfig,
	sinks SinkCatalog,
	recorder GuardRecorder,
) (*Guard, error) {
	routes, err := NewOfficialRouteCatalog(sinks)
	if err != nil {
		return nil, err
	}
	guard, err := NewGuard(config, sinks, routes, recorder)
	if err != nil {
		return nil, err
	}
	defaultGuardPointer.Store(guard)
	return guard, nil
}
