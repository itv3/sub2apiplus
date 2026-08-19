package officialegress

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	AdapterHTTPUpstream AdapterID = "officialegress.http_upstream"
	AdapterReqProfile   AdapterID = "officialegress.req_profile"
	AdapterWebSocket    AdapterID = "officialegress.websocket"
)

// RequestCompiler 只产出未签名且不可拆分的执行能力，不能签发 FinalizationToken。
type RequestCompiler interface {
	Compile(
		ctx context.Context,
		bundle ReleaseBundle,
		plan CodexEgressPlan,
		dynamic EndpointDynamicInputs,
	) (CompiledExecution, error)
}

type HTTPUpstreamTransportPort interface {
	SendHTTPUpstream(ctx context.Context, request PreparedRequest) (*http.Response, error)
}

type ReqProfileTransportPort interface {
	SendReqProfile(ctx context.Context, request PreparedRequest) (*http.Response, error)
}

// WebSocketConnection 是不依赖 coder/gorilla 的最小中立连接 port。
type WebSocketConnection interface {
	ReadMessage(ctx context.Context) ([]byte, error)
	WriteMessage(ctx context.Context, payload []byte) error
	Close() error
}

// WebSocketFrameReader 由受信 transport port 在能保留文本/二进制类型时实现。
type WebSocketFrameReader interface {
	ReadWebSocketFrame(ctx context.Context) (WebSocketFrameType, []byte, error)
}

// WebSocketFrameWriter 仅由受信 transport port 实现，用于消费 Executor 已定型
// 的文本或二进制帧。业务层拿不到此接口。
type WebSocketFrameWriter interface {
	WriteWebSocketFrame(ctx context.Context, frameType WebSocketFrameType, payload []byte) error
}

// WebSocketConnectionState 只暴露连接池的安全标量与生命周期操作；不得返回底层
// socket、lease 或任意写接口。
type WebSocketConnectionState interface {
	HandshakeStatus() int
	ConnectionID() string
	QueueWaitDuration() time.Duration
	ConnectionPickDuration() time.Duration
	Reused() bool
	HandshakeHeader(name string) string
	HandshakeHeaders() http.Header
	IsPrewarmed() bool
	MarkPrewarmed()
	MarkUnusable()
	Ping(ctx context.Context) error
	SupportsIdlePingWithoutReader() bool
}

type WebSocketTransportPort interface {
	AcquireWebSocket(ctx context.Context, request PreparedRequest) (WebSocketConnection, error)
}

type TransportResult struct {
	httpResponse *http.Response
	webSocket    *ExecutorWebSocketSession
}

func (r TransportResult) HTTPResponse() *http.Response         { return r.httpResponse }
func (r TransportResult) WebSocket() *ExecutorWebSocketSession { return r.webSocket }

type TransportAdapter interface {
	AdapterID() AdapterID
	ExecuteOfficialEgress(ctx context.Context, request PreparedRequest) (TransportResult, error)
}

type httpUpstreamAdapter struct {
	id   AdapterID
	port HTTPUpstreamTransportPort
}

func NewHTTPUpstreamTransportAdapter(port HTTPUpstreamTransportPort) (TransportAdapter, error) {
	if port == nil {
		return nil, errors.New("HTTPUpstream transport port 为空")
	}
	return &httpUpstreamAdapter{id: AdapterHTTPUpstream, port: port}, nil
}

func (a *httpUpstreamAdapter) AdapterID() AdapterID { return a.id }

func (a *httpUpstreamAdapter) ExecuteOfficialEgress(
	ctx context.Context,
	request PreparedRequest,
) (TransportResult, error) {
	response, err := a.port.SendHTTPUpstream(ctx, request)
	return TransportResult{httpResponse: response}, err
}

type reqProfileAdapter struct {
	id   AdapterID
	port ReqProfileTransportPort
}

func NewReqProfileTransportAdapter(port ReqProfileTransportPort) (TransportAdapter, error) {
	if port == nil {
		return nil, errors.New("req-profile transport port 为空")
	}
	return &reqProfileAdapter{id: AdapterReqProfile, port: port}, nil
}

func (a *reqProfileAdapter) AdapterID() AdapterID { return a.id }

func (a *reqProfileAdapter) ExecuteOfficialEgress(
	ctx context.Context,
	request PreparedRequest,
) (TransportResult, error) {
	response, err := a.port.SendReqProfile(ctx, request)
	return TransportResult{httpResponse: response}, err
}

type webSocketAdapter struct {
	id   AdapterID
	port WebSocketTransportPort
}

func NewWebSocketTransportAdapter(port WebSocketTransportPort) (TransportAdapter, error) {
	if port == nil {
		return nil, errors.New("WebSocket transport port 为空")
	}
	return &webSocketAdapter{id: AdapterWebSocket, port: port}, nil
}

func (a *webSocketAdapter) AdapterID() AdapterID { return a.id }

func (a *webSocketAdapter) ExecuteOfficialEgress(
	ctx context.Context,
	request PreparedRequest,
) (TransportResult, error) {
	connection, err := a.port.AcquireWebSocket(ctx, request)
	if err != nil {
		return TransportResult{}, err
	}
	session, err := newExecutorWebSocketSession(connection, request)
	if err != nil {
		if connection != nil {
			_ = connection.Close()
		}
		return TransportResult{}, err
	}
	return TransportResult{webSocket: session}, nil
}

// BackendDescriptor 是 Executor 唯一允许消费的 backend → protocol → adapter 闭集。
type BackendDescriptor struct {
	Backend   BackendKind
	Protocol  WireProtocol
	AdapterID AdapterID
}

func (d BackendDescriptor) Validate() error {
	if !d.Backend.Valid() || !d.Protocol.Valid() || strings.TrimSpace(string(d.AdapterID)) == "" {
		return errors.New("BackendDescriptor 字段非法")
	}
	if d.Backend == BackendPlainNetHTTP {
		return errors.New("plain_net_http 只允许作为 legacy Guard backend，不得登记为 Executor adapter")
	}
	return nil
}

func DefaultBackendDescriptors() []BackendDescriptor {
	return []BackendDescriptor{
		{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream},
		{Backend: BackendReqProfile, Protocol: WireProtocolHTTP, AdapterID: AdapterReqProfile},
		{Backend: BackendWebSocket, Protocol: WireProtocolWebSocket, AdapterID: AdapterWebSocket},
	}
}

type adapterRegistryEntry struct {
	descriptor BackendDescriptor
	adapter    TransportAdapter
}

// AdapterRegistry 在构造后不可变；请求期没有 default/fallback 分支。
type AdapterRegistry struct {
	byBackend map[BackendKind]adapterRegistryEntry
}

func NewAdapterRegistry(
	descriptors []BackendDescriptor,
	adapters []TransportAdapter,
) (AdapterRegistry, error) {
	if len(descriptors) == 0 {
		return AdapterRegistry{}, errors.New("AdapterRegistry descriptor 为空")
	}
	byAdapterID := make(map[AdapterID]TransportAdapter, len(adapters))
	for _, adapter := range adapters {
		if adapter == nil || strings.TrimSpace(string(adapter.AdapterID())) == "" {
			return AdapterRegistry{}, errors.New("AdapterRegistry 存在空 adapter")
		}
		if _, duplicate := byAdapterID[adapter.AdapterID()]; duplicate {
			return AdapterRegistry{}, fmt.Errorf("AdapterID 重复: %s", adapter.AdapterID())
		}
		byAdapterID[adapter.AdapterID()] = adapter
	}

	registry := AdapterRegistry{byBackend: make(map[BackendKind]adapterRegistryEntry, len(descriptors))}
	for _, descriptor := range descriptors {
		if err := descriptor.Validate(); err != nil {
			return AdapterRegistry{}, err
		}
		if _, duplicate := registry.byBackend[descriptor.Backend]; duplicate {
			return AdapterRegistry{}, fmt.Errorf("BackendKind 重复登记: %s", descriptor.Backend)
		}
		adapter, exists := byAdapterID[descriptor.AdapterID]
		if !exists {
			return AdapterRegistry{}, fmt.Errorf("Backend %s 缺少 adapter %s", descriptor.Backend, descriptor.AdapterID)
		}
		registry.byBackend[descriptor.Backend] = adapterRegistryEntry{descriptor: descriptor, adapter: adapter}
	}
	if len(byAdapterID) != len(registry.byBackend) {
		return AdapterRegistry{}, errors.New("存在未被 BackendDescriptor 引用的 adapter")
	}
	return registry, nil
}

func (r AdapterRegistry) resolve(spec TransportSpec) (adapterRegistryEntry, error) {
	entry, ok := r.byBackend[spec.Backend]
	if !ok {
		return adapterRegistryEntry{}, fmt.Errorf("未登记 backend: %s", spec.Backend)
	}
	if entry.descriptor.Protocol != spec.Protocol {
		return adapterRegistryEntry{}, fmt.Errorf(
			"backend %s 的 protocol 不匹配: descriptor=%s release=%s",
			spec.Backend,
			entry.descriptor.Protocol,
			spec.Protocol,
		)
	}
	if entry.descriptor.AdapterID != spec.Adapter || entry.adapter.AdapterID() != spec.Adapter {
		return adapterRegistryEntry{}, fmt.Errorf(
			"backend %s 的 AdapterID 不匹配: descriptor=%s release=%s",
			spec.Backend,
			entry.descriptor.AdapterID,
			spec.Adapter,
		)
	}
	return entry, nil
}

type ExecutorRequest struct {
	Bundle        ReleaseBundle
	Plan          CodexEgressPlan
	DynamicInputs EndpointDynamicInputs
	AttemptReason AttemptReason
	// ExpectedAttemptOrdinal 由上层不可变 Attempt 模型携带，必须与
	// ExecutorInvocation 原子签发的下一序号完全一致，禁止省略。
	ExpectedAttemptOrdinal uint32
	// ExecutionScopeKey 是运行期限流分区，不参与 Release/Profile digest。
	// 调用方应传账号级稳定键；Executor 会再组合 SinkID 与 PolicyID。
	ExecutionScopeKey string
}

// personaExecutorRequest 是共享 Executor 控制外壳的内部输入。
// 对外的 Codex ExecutorRequest 会先被封装为 codexDialectPlan；未来 Persona
// 只能新增自己的窄封装，不能扩张该结构为联合 Plan。
type personaExecutorRequest struct {
	bundle                 personaReleaseBundle
	plan                   typedEgressPlan
	attemptReason          AttemptReason
	expectedAttemptOrdinal uint32
	executionScopeKey      string
}

// AttemptReason 是 Executor 签发 attempt 时冻结的生命周期原因。
type AttemptReason string

const (
	AttemptReasonInitial     AttemptReason = "initial"
	AttemptReasonRetry       AttemptReason = "retry"
	AttemptReasonFallback    AttemptReason = "fallback"
	AttemptReasonReconnect   AttemptReason = "reconnect"
	AttemptReasonAcquire     AttemptReason = "acquire"
	AttemptReasonIndependent AttemptReason = "independent"
)

func (r AttemptReason) Valid() bool {
	switch r {
	case AttemptReasonInitial, AttemptReasonRetry, AttemptReasonFallback,
		AttemptReasonReconnect, AttemptReasonAcquire, AttemptReasonIndependent:
		return true
	default:
		return false
	}
}

var (
	ErrBehaviorAttemptBudgetExceeded  = errors.New("BehaviorPolicy attempt budget 已耗尽")
	ErrTransportAttemptBudgetExceeded = errors.New("ExecutionPolicy transport attempt budget 已耗尽")
	ErrFallbackTransitionRequired     = errors.New("切换 fallback endpoint 前必须取得 Executor transition capability")
)

type invocationAttemptTarget struct {
	sinkID     SinkID
	purpose    Purpose
	endpointID string
	protocol   WireProtocol
}

func invocationTargetFromFallback(target FallbackNode) invocationAttemptTarget {
	return invocationAttemptTarget{
		sinkID: target.SinkID, purpose: target.Purpose,
		endpointID: target.EndpointID, protocol: target.Protocol,
	}
}

// ExecutorInvocation 是由 Executor 创建的不可伪造调用能力。全部 retry、重拨和
// fallback 都必须复用同一个实例，才能共享 Bundle 与原子 attempt 预算。
type ExecutorInvocation struct {
	executor      *Executor
	bundle        personaReleaseBundle
	bundleControl executorBundleControl
	invocationID  string

	mu                          sync.Mutex
	attempts                    uint32
	currentSink                 SinkID
	pendingTransition           *invocationAttemptTarget
	invocationAttestationBound  bool
	invocationAttestationDigest string
}

func (i *ExecutorInvocation) InvocationID() string {
	if i == nil {
		return ""
	}
	return i.invocationID
}

func (i *ExecutorInvocation) Bundle() ReleaseBundle {
	if i == nil {
		return ReleaseBundle{}
	}
	bundle, _ := i.bundle.(ReleaseBundle)
	return bundle
}

// TransitionFallback 是 Codex facade 的兼容入口。fallback 合法性只在 Codex Bundle
// 内判断；共享 Executor 仅接收已批准的通用 attempt target，不解释 fallback 闭集。
func (i *ExecutorInvocation) TransitionFallback(target FallbackNode) error {
	if i == nil || i.executor == nil {
		return errors.New("ExecutorInvocation 未初始化")
	}
	codexBundle, ok := i.bundle.(ReleaseBundle)
	if !ok {
		return errors.New("非 Codex invocation 不能使用 Codex fallback facade")
	}
	matched := false
	for _, candidate := range codexBundle.FallbackNodes() {
		if candidate == target {
			matched = true
			break
		}
	}
	if !matched {
		return errors.New("fallback target 不在 Bundle 冻结闭集中")
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.attempts == 0 {
		return errors.New("首次 attempt 前禁止切换 fallback")
	}
	if i.pendingTransition != nil {
		return errors.New("已有尚未消费的 fallback transition")
	}
	transition := invocationTargetFromFallback(target)
	i.pendingTransition = &transition
	return nil
}

// ErrExecutionPolicyMinimumInterval 表示已有原子 lease 尚未到期。
var ErrExecutionPolicyMinimumInterval = errors.New("ExecutionPolicy 最短间隔尚未到期")

// ExecutionPolicyMinimumIntervalError 携带可观测但不泄露账号分区键的重试时间。
type ExecutionPolicyMinimumIntervalError struct {
	PolicyID   string
	RetryAfter time.Time
}

func (e *ExecutionPolicyMinimumIntervalError) Error() string {
	return fmt.Sprintf("ExecutionPolicy %s 最短间隔尚未到期，最早重试时间 %s",
		e.PolicyID, e.RetryAfter.UTC().Format(time.RFC3339))
}

func (e *ExecutionPolicyMinimumIntervalError) Unwrap() error {
	return ErrExecutionPolicyMinimumInterval
}

type executionPolicyState struct {
	mu          sync.Mutex
	active      int
	nextAllowed time.Time
	changed     chan struct{}
}

type executionPolicyController struct {
	mu     sync.Mutex
	states map[string]*executionPolicyState
}

func newExecutionPolicyController() *executionPolicyController {
	return &executionPolicyController{states: make(map[string]*executionPolicyState)}
}

func (c *executionPolicyController) acquire(
	ctx context.Context,
	scopeKey string,
	sinkID SinkID,
	policy ExecutionPolicy,
) (func(), error) {
	if err := policy.Validate(); err != nil {
		return nil, err
	}
	return c.acquireControl(ctx, scopeKey, sinkID, executorAttemptControl{
		policyID:               policy.ID,
		invocationAttemptLimit: policy.MaxAttempts,
		transportAttemptLimit:  policy.MaxAttempts,
		replayable:             policy.Replayable,
		minimumInterval:        policy.MinimumInterval,
		concurrencyLimit:       policy.ConcurrencyLimit,
	})
}

// acquireControl 是共享 Executor 使用的入口，只解释 Persona 方言已经投影出的
// lease 标量；上面的 acquire 仅为现有 Codex 单元测试保留兼容 facade。
func (c *executionPolicyController) acquireControl(
	ctx context.Context,
	scopeKey string,
	sinkID SinkID,
	control executorAttemptControl,
) (func(), error) {
	if err := control.validate(); err != nil {
		return nil, err
	}
	if control.concurrencyLimit <= 0 && control.minimumInterval <= 0 {
		return func() {}, nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	scopeKey = strings.TrimSpace(scopeKey)
	if scopeKey == "" {
		scopeKey = "global"
	}
	key := strings.Join([]string{scopeKey, string(sinkID), control.policyID}, "\x00")
	c.mu.Lock()
	state := c.states[key]
	if state == nil {
		state = &executionPolicyState{changed: make(chan struct{})}
		c.states[key] = state
	}
	c.mu.Unlock()

	state.mu.Lock()
	for {
		now := time.Now()
		if control.minimumInterval > 0 && now.Before(state.nextAllowed) {
			retryAfter := state.nextAllowed
			state.mu.Unlock()
			return nil, &ExecutionPolicyMinimumIntervalError{
				PolicyID: control.policyID, RetryAfter: retryAfter,
			}
		}
		if control.concurrencyLimit <= 0 || state.active < control.concurrencyLimit {
			state.active++
			if control.minimumInterval > 0 {
				// 在发送前预占下一时段；失败尝试同样进入冷却，避免计费端点被快速重打。
				state.nextAllowed = now.Add(control.minimumInterval)
			}
			state.mu.Unlock()
			var once sync.Once
			return func() {
				once.Do(func() {
					state.mu.Lock()
					state.active--
					close(state.changed)
					state.changed = make(chan struct{})
					state.mu.Unlock()
				})
			}, nil
		}
		changed := state.changed
		state.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-changed:
		}
		state.mu.Lock()
	}
}

// Executor 是 FinalizationToken 的唯一签发边界。
type Executor struct {
	id                ExecutorID
	persona           Persona
	personas          PersonaRegistry
	dialects          DialectCompilerRegistry
	registry          AdapterRegistry
	issuer            *tokenIssuer
	executionPolicies *executionPolicyController
}

func NewExecutor(
	id ExecutorID,
	compiler RequestCompiler,
	registry AdapterRegistry,
	guard *Guard,
) (*Executor, error) {
	if compiler == nil || len(registry.byBackend) == 0 {
		return nil, errors.New("Executor 缺少 compiler/adapter registry")
	}
	if guard == nil {
		guard = DefaultGuard()
	}
	personas, err := NewCodexPersonaRegistry(guard.ProcessSinkCatalog())
	if err != nil {
		return nil, err
	}
	descriptor, ok := personas.ResolveIdentity(codexPersonaDescriptorInput().Identity)
	if !ok || descriptor.Persona() != PersonaCodexCLI {
		return nil, errors.New("Codex Persona 未登记")
	}
	dialects, err := newCodexDialectCompilerRegistry(personas, compiler)
	if err != nil {
		return nil, err
	}
	return newPersonaExecutor(
		id, descriptor.Persona(), personas, dialects, registry, guard,
	)
}

func newPersonaExecutor(
	id ExecutorID,
	persona Persona,
	personas PersonaRegistry,
	dialects DialectCompilerRegistry,
	registry AdapterRegistry,
	guard *Guard,
) (*Executor, error) {
	return newPersonaExecutorWithIssuer(
		id, id, persona, personas, dialects, registry, guard,
	)
}

// newPersonaExecutorWithIssuer 允许 Persona authority 与一次性 token issuer 使用
// 不同稳定身份。Codex 现有构造仍把两者设为同一值，保持既有 final wire 与收据语义。
func newPersonaExecutorWithIssuer(
	authorityID ExecutorID,
	issuerID ExecutorID,
	persona Persona,
	personas PersonaRegistry,
	dialects DialectCompilerRegistry,
	registry AdapterRegistry,
	guard *Guard,
) (*Executor, error) {
	descriptor, ok := personas.Resolve(persona)
	if !ok || descriptor.Persona() != persona {
		return nil, errors.New("Executor Persona 未登记")
	}
	if dialects.personas.Digest() == "" || dialects.personas.Digest() != personas.Digest() {
		return nil, errors.New("Executor 与 DialectCompilerRegistry 的 PersonaRegistry 不一致")
	}
	compiler, err := dialects.resolve(persona)
	if err != nil || compiler.Persona() != persona {
		return nil, errors.New("Executor Persona 缺少匹配的 DialectCompiler")
	}
	if len(registry.byBackend) == 0 || guard == nil {
		return nil, errors.New("Executor 缺少 adapter registry/Guard")
	}
	issuer, err := newTokenIssuerForPersona(
		descriptor.AuthorityKind(), descriptor.Persona(), issuerID,
	)
	if err != nil {
		return nil, err
	}
	if err := guard.registerIssuer(issuer); err != nil {
		return nil, err
	}
	return &Executor{
		id: authorityID, persona: persona, personas: personas, dialects: dialects,
		registry: registry, issuer: issuer,
		executionPolicies: newExecutionPolicyController(),
	}, nil
}

func (e *Executor) ID() ExecutorID {
	if e == nil {
		return ""
	}
	return e.id
}

// BeginInvocation 冻结单次调用的 Bundle、InvocationID 与 primary sink。
func (e *Executor) BeginInvocation(
	ctx context.Context,
	bundle ReleaseBundle,
	invocationID string,
) (*ExecutorInvocation, error) {
	return e.beginInvocation(ctx, bundle, invocationID)
}

func (e *Executor) beginInvocation(
	ctx context.Context,
	bundle personaReleaseBundle,
	invocationID string,
) (*ExecutorInvocation, error) {
	if e == nil || e.issuer == nil || e.persona == "" || len(e.dialects.compilers) == 0 {
		return nil, errors.New("Executor 未初始化")
	}
	if bundle == nil {
		return nil, errors.New("ExecutorInvocation 缺少 ReleaseBundle")
	}
	control := bundle.executorControl()
	if err := control.validate(); err != nil {
		return nil, err
	}
	if control.persona != e.persona {
		return nil, errors.New("ExecutorInvocation 的 Bundle Persona 与 Executor 不一致")
	}
	invocationID = strings.TrimSpace(invocationID)
	if invocationID == "" {
		if identity, ok := AttemptIdentityFromContext(ctx); ok && identity.InvocationID != "" {
			invocationID = identity.InvocationID
		} else {
			var err error
			invocationID, err = newInvocationID()
			if err != nil {
				return nil, err
			}
		}
	}
	return &ExecutorInvocation{
		executor: e, bundle: bundle, bundleControl: control, invocationID: invocationID,
		currentSink: control.primarySinkID,
	}, nil
}

func (i *ExecutorInvocation) PrepareAttempt(
	ctx context.Context,
	input ExecutorRequest,
) (PreparedRequest, error) {
	typed, err := i.codexExecutorRequest(input)
	if err != nil {
		return PreparedRequest{}, err
	}
	return i.prepareTypedAttempt(ctx, typed)
}

func (i *ExecutorInvocation) codexExecutorRequest(
	input ExecutorRequest,
) (personaExecutorRequest, error) {
	if i == nil || i.executor == nil {
		return personaExecutorRequest{}, errors.New("ExecutorInvocation 未初始化")
	}
	plan := input.Plan.clone()
	inputControl := input.Bundle.executorControl()
	if inputControl.bundleDigest != i.bundleControl.bundleDigest ||
		inputControl.releaseDigest != i.bundleControl.releaseDigest {
		return personaExecutorRequest{}, errors.New("attempt 不能更换 Invocation 冻结的 Bundle")
	}
	if plan.InvocationID != "" && strings.TrimSpace(plan.InvocationID) != i.invocationID {
		return personaExecutorRequest{}, errors.New("attempt InvocationID 与 invocation capability 不一致")
	}
	plan.InvocationID = i.invocationID
	return personaExecutorRequest{
		bundle: input.Bundle, plan: newCodexDialectPlan(plan, input.DynamicInputs),
		attemptReason:          input.AttemptReason,
		expectedAttemptOrdinal: input.ExpectedAttemptOrdinal,
		executionScopeKey:      input.ExecutionScopeKey,
	}, nil
}

func (i *ExecutorInvocation) prepareTypedAttempt(
	ctx context.Context,
	input personaExecutorRequest,
) (PreparedRequest, error) {
	if i == nil || i.executor == nil || input.plan == nil || input.bundle == nil {
		return PreparedRequest{}, errors.New("ExecutorInvocation 或 Persona attempt 未初始化")
	}
	e := i.executor
	inputBundleControl := input.bundle.executorControl()
	if inputBundleControl.bundleDigest != i.bundleControl.bundleDigest ||
		inputBundleControl.releaseDigest != i.bundleControl.releaseDigest ||
		inputBundleControl.persona != i.bundleControl.persona {
		return PreparedRequest{}, errors.New("attempt 不能更换 Invocation 冻结的 Bundle")
	}
	input.bundle = i.bundle
	control := input.plan.control()
	if control.invocationID != i.invocationID {
		return PreparedRequest{}, errors.New("TypedEgressPlan InvocationID 与 invocation capability 不一致")
	}
	if err := validateExecutorInput(control, i.bundleControl); err != nil {
		return PreparedRequest{}, err
	}
	reason, ordinal, transitioned, err := i.reserveAttempt(
		control, input.attemptReason, input.expectedAttemptOrdinal,
	)
	if err != nil {
		return PreparedRequest{}, err
	}
	attemptContext := contextWithExecutorAttempt(
		ctx, control, i.bundleControl, i.invocationID, ordinal, reason,
	)
	if transitioned != nil {
		if control.targetIdentity() != *transitioned {
			return PreparedRequest{}, errors.New("fallback attempt 未匹配已签发 transition target")
		}
	}
	dialect, err := e.dialects.resolve(control.persona)
	if err != nil {
		return PreparedRequest{}, err
	}
	compiled, err := dialect.compile(attemptContext, input.bundle, input.plan)
	if err != nil {
		return PreparedRequest{}, WrapRuntimeError(
			RuntimeErrorCodeCompilerRejected,
			"compiler.compile",
			fmt.Errorf("编译 official egress request: %w", err),
		)
	}
	if err := validateCompiledExecutionForPlan(compiled, i.bundleControl, control); err != nil {
		return PreparedRequest{}, err
	}
	if !e.personas.AuthorizeRoute(
		compiled.control.persona, compiled.control.sinkID, compiled.control.purpose,
		compiled.control.route, compiled.control.protocol,
	) {
		return PreparedRequest{}, errors.New("CompiledExecution 未获 Persona Registry route 授权")
	}
	entry, err := e.registry.resolve(compiled.transport)
	if err != nil {
		return PreparedRequest{}, err
	}
	_ = entry // Prepare 只验证闭集；Execute 再取得同一不可变 entry。
	requestContext, err := WithAttemptMetadata(attemptContext, AttemptMetadataInput{
		SinkID:               control.sinkID,
		Purpose:              control.purpose,
		DeclaredPersona:      control.persona,
		EndpointID:           compiled.control.endpointID,
		InvocationID:         control.invocationID,
		AttemptOrdinal:       ordinal,
		AttemptReason:        string(reason),
		ExecutorID:           e.id,
		ReleaseMode:          i.bundleControl.mode,
		ReleaseDigest:        i.bundleControl.releaseDigest,
		BundleDigest:         i.bundleControl.bundleDigest,
		ProfileDigest:        i.bundleControl.profileDigest,
		ConnectionPoolDigest: compiled.poolDigest,
	})
	if err != nil {
		return PreparedRequest{}, err
	}
	request, err := requestFromCompiled(requestContext, compiled.request)
	if err != nil {
		return PreparedRequest{}, err
	}
	digest, err := requestDigest(
		request,
		compiled.transport.Normalization,
		compiled.control.protocol,
	)
	if err != nil {
		return PreparedRequest{}, fmt.Errorf("计算定型请求摘要: %w", err)
	}
	token := e.issuer.sign(tokenPayload{
		AuthorityID: e.id, ReleaseDigest: i.bundleControl.releaseDigest,
		ProfileDigest: i.bundleControl.profileDigest, BundleDigest: i.bundleControl.bundleDigest,
		SinkID: control.sinkID, Route: compiled.control.route,
		Persona: compiled.control.persona, EndpointID: compiled.control.endpointID,
		TransportID: compiled.transport.ID, AdapterID: compiled.transport.Adapter,
		Backend: compiled.transport.Backend, Protocol: compiled.transport.Protocol,
		ResourceLifecycleDigest: resourceLifecycleDigest(compiled.transport.ResourceLifecycle),
		ConnectionPoolDigest:    compiled.transport.ConnectionPoolDigest,
		InvocationID:            control.invocationID, RequestDigest: digest,
		AttemptOrdinal: ordinal, AttemptReason: string(reason),
		IdentityAttestationDigest: compiled.control.identityAttestationDigest,
		DialectAttestationDigest:  compiled.control.dialectAttestationDigest,
		Normalization:             compiled.transport.Normalization,
	})
	request = request.WithContext(withFinalizationToken(request.Context(), token))
	if compiled.dialectState == nil || compiled.dialectState.persona() != control.persona {
		return PreparedRequest{}, errors.New("DialectCompiler 缺少匹配的 PreparedRequest 状态")
	}
	consumed := false
	prepared := PreparedRequest{
		request: request, requestMu: &sync.Mutex{}, consumed: &consumed,
		singleUse: !compiled.control.bodyReplayable,
		token:     token, transport: compiled.transport, dialect: compiled.dialectState,
	}
	if codexState, ok := compiled.dialectState.(codexPreparedState); ok {
		prepared.bundle = codexState.bundle
		prepared.endpoint = compiled.endpointPlan
		prepared.identity = codexState.identity
	}
	return prepared, nil
}

func (i *ExecutorInvocation) ExecuteAttempt(
	ctx context.Context,
	input ExecutorRequest,
) (TransportResult, error) {
	typed, err := i.codexExecutorRequest(input)
	if err != nil {
		return TransportResult{}, err
	}
	return i.executeTypedAttempt(ctx, typed)
}

func (i *ExecutorInvocation) executeTypedAttempt(
	ctx context.Context,
	input personaExecutorRequest,
) (TransportResult, error) {
	if i == nil || i.executor == nil || i.executor.executionPolicies == nil {
		return TransportResult{}, errors.New("ExecutorInvocation 执行策略控制器未初始化")
	}
	if input.plan == nil {
		return TransportResult{}, errors.New("Persona attempt 缺少 TypedEgressPlan")
	}
	attemptControl := i.bundleControl.attempt
	if err := attemptControl.validate(); err != nil {
		return TransportResult{}, err
	}
	releasePolicy, err := i.executor.executionPolicies.acquireControl(
		ctx,
		input.executionScopeKey,
		input.plan.control().sinkID,
		attemptControl,
	)
	if err != nil {
		return TransportResult{}, WrapRuntimeError(
			RuntimeErrorCodeExecutionPolicyRejected, "executor.acquire_policy", err,
		)
	}
	defer releasePolicy()

	prepared, err := i.prepareTypedAttempt(ctx, input)
	if err != nil {
		return TransportResult{}, err
	}
	entry, err := i.executor.registry.resolve(prepared.transport)
	if err != nil {
		return TransportResult{}, err
	}
	result, err := entry.adapter.ExecuteOfficialEgress(ctx, prepared)
	if err != nil {
		return TransportResult{}, WrapRuntimeError(
			RuntimeErrorCodeTransportFailed, "executor.execute_transport", err,
		)
	}
	return result, nil
}

func (i *ExecutorInvocation) reserveAttempt(
	control executorPlanControl,
	requested AttemptReason,
	expectedOrdinal uint32,
) (AttemptReason, uint32, *invocationAttemptTarget, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if !receiptSHA256(control.invocationAttestationDigest) {
		return "", 0, nil, errors.New("attempt 缺少 invocation attestation")
	}
	if !i.invocationAttestationBound {
		i.invocationAttestationBound = true
		i.invocationAttestationDigest = control.invocationAttestationDigest
	} else if i.invocationAttestationDigest != control.invocationAttestationDigest {
		return "", 0, nil, errors.New("attempt 不能更换 invocation 冻结的 Persona attestation")
	}
	attemptControl := i.bundleControl.attempt
	if int(i.attempts) >= attemptControl.invocationAttemptLimit {
		return "", 0, nil, WrapRuntimeError(
			RuntimeErrorCodeBehaviorAttemptBudgetExceeded,
			"executor.reserve_attempt",
			ErrBehaviorAttemptBudgetExceeded,
		)
	}
	if int(i.attempts) >= attemptControl.transportAttemptLimit {
		return "", 0, nil, WrapRuntimeError(
			RuntimeErrorCodeTransportAttemptBudgetExceeded,
			"executor.reserve_attempt",
			ErrTransportAttemptBudgetExceeded,
		)
	}
	nextOrdinal := i.attempts + 1
	if expectedOrdinal == 0 {
		return "", 0, nil, errors.New("ExpectedAttemptOrdinal 必须显式设置")
	}
	if expectedOrdinal != nextOrdinal {
		return "", 0, nil, errors.New("ExpectedAttemptOrdinal 与 Executor 原子序号不一致")
	}

	reason := requested
	if !reason.Valid() {
		if i.attempts == 0 {
			reason = AttemptReasonInitial
		} else {
			reason = AttemptReasonRetry
		}
	}
	var transitioned *invocationAttemptTarget
	if i.attempts == 0 {
		if control.sinkID != i.bundleControl.primarySinkID || reason != AttemptReasonInitial {
			return "", 0, nil, errors.New("首次 attempt 必须使用 primary sink 和 initial reason")
		}
	} else if i.pendingTransition != nil {
		if reason != AttemptReasonFallback ||
			control.targetIdentity() != *i.pendingTransition {
			return "", 0, nil, ErrFallbackTransitionRequired
		}
		cloned := *i.pendingTransition
		transitioned = &cloned
		i.currentSink = cloned.sinkID
		i.pendingTransition = nil
	} else if control.sinkID != i.currentSink {
		return "", 0, nil, ErrFallbackTransitionRequired
	} else if reason == AttemptReasonInitial || reason == AttemptReasonFallback {
		return "", 0, nil, errors.New("非首次 attempt 的 reason 与生命周期不一致")
	}
	i.attempts++
	return reason, i.attempts, transitioned, nil
}

func contextWithExecutorAttempt(
	ctx context.Context,
	control executorPlanControl,
	bundle executorBundleControl,
	invocationID string,
	ordinal uint32,
	reason AttemptReason,
) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	metadata := attemptMetadata{
		SinkID: control.sinkID, Purpose: control.purpose, DeclaredPersona: control.persona,
		EndpointID: strings.TrimSpace(control.endpointID), InvocationID: invocationID,
		AttemptOrdinal: ordinal, AttemptReason: string(reason), ReleaseMode: bundle.mode,
		ReleaseDigest: bundle.releaseDigest, BundleDigest: bundle.bundleDigest,
		ProfileDigest: bundle.profileDigest,
	}
	return context.WithValue(ctx, attemptMetadataContextKey{}, metadata)
}

func validateExecutorInput(
	control executorPlanControl,
	bundle executorBundleControl,
) error {
	if strings.TrimSpace(string(control.sinkID)) == "" ||
		strings.TrimSpace(string(control.purpose)) == "" || !control.mode.Valid() ||
		strings.TrimSpace(control.endpointID) == "" || !control.protocol.Valid() ||
		strings.TrimSpace(control.method) == "" || control.target == nil ||
		!control.persona.Valid() || control.persona != bundle.persona ||
		!receiptSHA256(control.identityAttestationDigest) ||
		!receiptSHA256(control.dialectAttestationDigest) ||
		!receiptSHA256(control.invocationAttestationDigest) {
		return errors.New("TypedEgressPlan 字段不完整或 Persona 不一致")
	}
	if err := bundle.validate(); err != nil {
		return err
	}
	if bundle.mode != control.mode {
		return errors.New("ExecutorRequest 缺少与 Plan 一致的 ReleaseBundle")
	}
	if !control.bodyReplayable &&
		(bundle.attempt.replayable || bundle.attempt.transportAttemptLimit != 1) {
		return errors.New("single-use stream 必须使用 MaxAttempts=1 且 Replayable=false")
	}
	return nil
}

func validateCompiledExecutionForPlan(
	compiled CompiledExecution,
	bundle executorBundleControl,
	control executorPlanControl,
) error {
	if compiled.bundleDigest != bundle.bundleDigest ||
		compiled.releaseDigest != bundle.releaseDigest ||
		compiled.profileDigest != bundle.profileDigest ||
		compiled.control.persona != control.persona ||
		compiled.control.sinkID != control.sinkID || compiled.control.purpose != control.purpose ||
		compiled.control.endpointID != strings.TrimSpace(control.endpointID) ||
		compiled.control.protocol != control.protocol ||
		compiled.control.protocol != compiled.transport.Protocol ||
		compiled.control.bodyReplayable != control.bodyReplayable ||
		compiled.control.identityAttestationDigest != control.identityAttestationDigest ||
		compiled.control.dialectAttestationDigest != control.dialectAttestationDigest ||
		compiled.control.invocationAttestationDigest != control.invocationAttestationDigest ||
		compiled.request.Method() != strings.ToUpper(strings.TrimSpace(control.method)) {
		return errors.New("CompiledExecution 与 Bundle/Plan 不一致")
	}
	target := compiled.request.URL()
	if target == nil || compiled.control.route.Method != compiled.request.Method() ||
		!matchRouteHost(compiled.control.route.Host, target.Hostname()) ||
		!matchRoutePath(compiled.control.route.Path, target.EscapedPath()) {
		return errors.New("CompiledExecution 的 route 与 final request 不一致")
	}
	return compiled.transport.Validate()
}

// validateCompiledRequestForEndpoint 保留 single-use capability 的包内单元门禁。
// 正式执行链使用更窄的 CompiledExecution；该辅助函数不参与生产请求选择。
func validateCompiledRequestForEndpoint(
	compiled CompiledRequest,
	endpoint ResolvedEndpoint,
	plan CodexEgressPlan,
) error {
	target := compiled.URL()
	if target == nil || strings.ToUpper(compiled.Method()) != endpoint.Route.Method ||
		strings.ToUpper(plan.Method) != endpoint.Route.Method || endpoint.Route.Purpose != plan.Purpose ||
		!matchRouteHost(endpoint.Route.Host, target.Hostname()) ||
		!matchRoutePath(endpoint.Route.Path, target.EscapedPath()) {
		return errors.New("CompiledRequest 与 endpoint route 不一致")
	}
	if compiled.body.Mode() != plan.Body.Mode() ||
		compiled.body.ContentLength() != plan.Body.ContentLength() {
		return errors.New("RequestCompiler 改变了 Body mode 或 ContentLength")
	}
	if plan.Body.Mode() == RequestBodySingleUse && !plan.Body.sameCapability(compiled.body) {
		return errors.New("RequestCompiler 替换了 single-use Body capability")
	}
	return nil
}

func newInvocationID() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("生成 InvocationID: %w", err)
	}
	return hex.EncodeToString(raw[:]), nil
}
