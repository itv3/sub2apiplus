package officialegress

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
)

type claudeReleaseBundle struct {
	control executorBundleControl
}

func (b claudeReleaseBundle) executorControl() executorBundleControl { return b.control }

type claudeDialectPlan struct {
	plan     ClaudeEgressPlan
	endpoint claudeEndpointProfile
	state    executorPlanControl
}

func (claudeDialectPlan) persona() Persona { return PersonaClaudeCode }
func (p claudeDialectPlan) control() executorPlanControl {
	return p.state.clone()
}

type claudePreparedState struct{}

func (claudePreparedState) persona() Persona { return PersonaClaudeCode }

type claudeDialectCompiler struct {
	profile      claudeFWGProfile
	wire         claudeWireArtifact
	sinks        SinkCatalog
	newRequestID func() string
}

var (
	claudeCustomHeaderNamePattern = regexp.MustCompile("^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
	claudeToolUseIDPattern        = regexp.MustCompile(`^toolu_[A-Za-z0-9]+$`)
)

func (claudeDialectCompiler) Persona() Persona { return PersonaClaudeCode }

func (c claudeDialectCompiler) compile(
	ctx context.Context,
	bundle personaReleaseBundle,
	input typedEgressPlan,
) (CompiledExecution, error) {
	claudeBundle, bundleOK := bundle.(claudeReleaseBundle)
	plan, planOK := input.(claudeDialectPlan)
	if !bundleOK || !planOK || input.persona() != PersonaClaudeCode ||
		claudeBundle.control.persona != PersonaClaudeCode {
		return CompiledExecution{}, errors.New("Claude DialectCompiler 收到其他 Persona 的 Bundle/Plan")
	}
	if plan.plan.endpointKind != plan.endpoint.kind ||
		!claudeEndpointQueryMatches(plan.endpoint, plan.endpoint.target) {
		return CompiledExecution{}, errors.New("Claude TypedEgressPlan endpoint 身份不一致")
	}
	authentication, err := plan.plan.authentication.take()
	if err != nil {
		return CompiledExecution{}, err
	}
	body, model, beta, _, err := compileClaudeEndpointBody(plan.plan, c.wire, authentication)
	if err != nil {
		return CompiledExecution{}, err
	}
	requestID := ""
	if claudeEndpointNeedsRequestID(plan.endpoint.kind) {
		requestID = strings.TrimSpace(c.newRequestID())
		if _, err := uuid.Parse(requestID); err != nil {
			return CompiledExecution{}, errors.New("Claude request-id 生成器返回非法 UUID")
		}
	}
	headers, order, err := compileClaudeEndpointHeaders(
		plan.plan, plan.endpoint, c.wire, authentication, body, model, beta, requestID,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	requestBody := NewReplayableRequestBody(body)
	request, err := NewCompiledRequest(
		plan.endpoint.method, plan.endpoint.target, headers, requestBody,
	)
	if err != nil {
		return CompiledExecution{}, err
	}
	binding, ok := c.sinks.Resolve(plan.endpoint.sinkID)
	if !ok || binding.Persona() != PersonaClaudeCode || !binding.RuntimeBindable() {
		return CompiledExecution{}, errors.New("Claude compiler 缺少 candidate SinkBinding")
	}
	physicalKey := physicalRouteFromCatalogRoute(plan.endpoint.route)
	endpointPlan := ResolvedEndpointPlan{template: EndpointPlanTemplate{
		binding: EndpointBinding{
			key: EndpointBindingKey{
				SinkID: binding.ID(), Purpose: binding.Purpose(),
				PhysicalRouteID: physicalRouteID(physicalKey), Protocol: WireProtocolHTTP,
			},
			endpointID: plan.endpoint.id,
		},
		route: plan.endpoint.route,
		endpoint: profilecontract.ExecutableEndpointProfile{
			ID: plan.endpoint.id, Method: plan.endpoint.method,
			Host: plan.endpoint.target.Hostname(), Path: plan.endpoint.target.EscapedPath(),
			ResourceLifecycle: claudeResourceLifecycle(),
		},
		backend: BackendHTTPUpstream, adapter: AdapterHTTPUpstream,
		connectionGroup: "claude-code:" + plan.endpoint.routeID,
	}}
	poolDigest := claudeAttestationDigest(
		"connection-pool", string(PersonaClaudeCode), ClaudeFWGReleaseDigest,
		plan.plan.accountScope, string(plan.endpoint.sinkID), plan.endpoint.routeID,
		plan.endpoint.transportID,
	)
	transport := TransportSpec{
		ID: plan.endpoint.transportID, Backend: BackendHTTPUpstream,
		Protocol: WireProtocolHTTP, Adapter: AdapterHTTPUpstream,
		ProfileDigest:        ClaudeFWGProfileDigest,
		ConnectionGroup:      endpointPlan.template.connectionGroup,
		ConnectionPoolDigest: poolDigest,
		ResourceLifecycle:    claudeResourceLifecycle(),
		Normalization:        WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve},
		TLS:                  claudeTLSProfileSpec(c.wire, plan.endpoint, order),
	}
	if err := transport.Validate(); err != nil {
		return CompiledExecution{}, err
	}
	compiledDigest := claudeAttestationDigest(
		"compiled", ClaudeFWGReleaseDigest, string(plan.endpoint.sinkID),
		plan.endpoint.id, requestID, claudeSHA256Hex(body),
	)
	return CompiledExecution{
		request: request, endpointPlan: endpointPlan, transport: transport,
		control: compiledExecutionControl{
			persona: PersonaClaudeCode, sinkID: plan.endpoint.sinkID,
			purpose: plan.endpoint.purpose, endpointID: plan.endpoint.id,
			route: plan.endpoint.route.Key, protocol: WireProtocolHTTP,
			bodyReplayable:              true,
			identityAttestationDigest:   plan.state.identityAttestationDigest,
			dialectAttestationDigest:    plan.state.dialectAttestationDigest,
			invocationAttestationDigest: plan.state.invocationAttestationDigest,
		},
		dialectState:  claudePreparedState{},
		releaseDigest: ClaudeFWGReleaseDigest,
		profileDigest: ClaudeFWGProfileDigest,
		bundleDigest:  claudeBundle.control.bundleDigest,
		poolDigest:    poolDigest, compiledDigest: compiledDigest,
		connection: ConnectionIdentity{digest: poolDigest},
	}, nil
}

// ClaudeMessagesExecution 是 service 交给 Claude PersonaPlanner 的最小可信输入。
type ClaudeMessagesExecution struct {
	Body         []byte
	AccessToken  string
	TrustedFacts ClaudeTrustedFacts
	Ingress      ClaudeIngressSnapshot
	InvocationID string
}

// claudeSupportEnvelopeRejection 标记在任何上游发送前即可确定的入口拒绝。
// service 只能依据这个稳定类型返回 400，禁止解析中文错误文本或把传输故障误报为客户端错误。
type claudeSupportEnvelopeRejection struct {
	err error
}

func (e *claudeSupportEnvelopeRejection) Error() string {
	if e == nil || e.err == nil {
		return "Claude 请求不在 SupportEnvelope"
	}
	return e.err.Error()
}

func (e *claudeSupportEnvelopeRejection) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.err
}

func newClaudeSupportEnvelopeRejection(err error) error {
	if err == nil {
		return nil
	}
	return &claudeSupportEnvelopeRejection{err: err}
}

// IsClaudeSupportEnvelopeRejection 判断错误是否属于本地 strict 范围拒绝。
func IsClaudeSupportEnvelopeRejection(err error) bool {
	var rejection *claudeSupportEnvelopeRejection
	return errors.As(err, &rejection)
}

// ClaudeEndpointExecution 供 lifecycle／auxiliary 调用点接入同一 strict 链。
type ClaudeEndpointExecution struct {
	EndpointKind string
	Body         []byte
	AccessToken  string
	RefreshToken string
	ClientID     string
	RefreshScope string
	TrustedFacts ClaudeTrustedFacts
	Ingress      ClaudeIngressSnapshot
	InvocationID string
}

// ClaudeEndpointResult 保留辅助端点的实际响应与最终 wire body。当前只有
// count_tokens 需要把最终 body 交还网关；其他受管辅助端点继续返回空 body。
type ClaudeEndpointResult struct {
	Response *http.Response
	WireBody []byte
}

// ClaudeCandidateResult 保留上游响应和本次实际语义 wire，供网关响应链与计费复用。
type ClaudeCandidateResult struct {
	Response         *http.Response
	WireBody         []byte
	Model            string
	Stream           bool
	Attempts         int
	StreamFallback   func(context.Context) (ClaudeCandidateResult, error)
	sessionFinalizer *claudeSessionFinalizer
	sessionStatus    int
	sessionRequestID string
}

// FinalizeSession 在最终响应确定后提交或撤销本次会话状态。
func (r *ClaudeCandidateResult) FinalizeSession(accepted bool) error {
	if r == nil || r.sessionFinalizer == nil {
		return nil
	}
	return r.sessionFinalizer.finalize(accepted, r.sessionStatus, r.sessionRequestID)
}

func newClaudeCandidateResult(
	response *http.Response,
	wireBody []byte,
	model string,
	stream bool,
	attempts int,
	finalizer *claudeSessionFinalizer,
) ClaudeCandidateResult {
	result := ClaudeCandidateResult{
		Response: response, WireBody: wireBody, Model: model,
		Stream: stream, Attempts: attempts, sessionFinalizer: finalizer,
	}
	if response != nil {
		result.sessionStatus = response.StatusCode
		result.sessionRequestID = claudeResponseRequestID(response.Header)
	}
	return result
}

func claudeResponseRequestID(headers http.Header) string {
	requestID := strings.TrimSpace(headers.Get("request-id"))
	if requestID == "" {
		requestID = strings.TrimSpace(headers.Get("x-request-id"))
	}
	return requestID
}

// ClaudeCandidateRuntime 是 FW-G 隔离 candidate 的独立 Planner／Compiler／Executor。
type ClaudeCandidateRuntime struct {
	profile       claudeFWGProfile
	wire          claudeWireArtifact
	sinks         SinkCatalog
	executor      *Executor
	newRequestID  func() string
	newCCH        func() (string, error)
	retryJitter   func(int) (int, error)
	sleep         func(context.Context, time.Duration) error
	startupMu     sync.Mutex
	startupRuns   map[string]*claudeStartupRun
	sessionMu     sync.Mutex
	sessions      map[string]*claudeSessionState
	requestOwners map[string]string
}

type claudeStartupRun struct {
	done chan struct{}
	err  error
}

type claudeMessageRelations struct {
	webSearchRoundTrip bool
}

type claudeSessionRequestKind string

const (
	claudeSessionRequestMain                  claudeSessionRequestKind = "main"
	claudeSessionRequestTUITitle              claudeSessionRequestKind = "tui-title"
	claudeSessionRequestWebSearchOuter        claudeSessionRequestKind = "web-search-outer"
	claudeSessionRequestWebSearchServer       claudeSessionRequestKind = "web-search-server"
	claudeSessionRequestWebSearchContinuation claudeSessionRequestKind = "web-search-continuation"
)

type claudeSessionLineState struct {
	previousRequestID string
	inFlight          bool
}

type claudeAgentLineageState struct {
	parentAgentID string
	depth         int
}

type claudeSessionState struct {
	lines                    map[string]*claudeSessionLineState
	agentLineages            map[string]claudeAgentLineageState
	requestIDs               map[string]struct{}
	tuiTitleCompleted        bool
	tuiTitleInFlight         bool
	webSearchParentRequestID string
	webSearchServerCompleted bool
	webSearchServerInFlight  bool
	updatedAt                time.Time
}

type claudeSessionLease struct {
	runtime       *ClaudeCandidateRuntime
	sessionKey    string
	lineKey       string
	kind          claudeSessionRequestKind
	newAgent      bool
	agentID       string
	parentAgentID string
	agentDepth    int
}

type claudeSessionFinalizer struct {
	once  sync.Once
	lease claudeSessionLease
	err   error
}

func NewClaudeCandidateRuntime(
	sinks SinkCatalog,
	guard *Guard,
	port HTTPUpstreamTransportPort,
) (*ClaudeCandidateRuntime, error) {
	if guard == nil || port == nil {
		return nil, errors.New("Claude FW-G runtime 缺少 Guard 或 HTTPUpstream port")
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		return nil, err
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		return nil, err
	}
	for _, kind := range claudeStrictEndpointKinds() {
		endpoint, endpointErr := profile.endpoint(kind)
		if endpointErr != nil {
			return nil, endpointErr
		}
		binding, ok := sinks.Resolve(endpoint.sinkID)
		if !ok || binding.Persona() != PersonaClaudeCode ||
			binding.EndpointEvidence() != EndpointEvidenceClaudeProfile || !binding.RuntimeBindable() {
			return nil, fmt.Errorf("Claude FW-G runtime 缺少 strict Sink：%s", endpoint.sinkID)
		}
	}
	personas, err := newClaudeCandidatePersonaRegistry(sinks)
	if err != nil {
		return nil, err
	}
	requestIDGenerator := uuid.NewString
	dialect := claudeDialectCompiler{
		profile: profile, wire: wire, sinks: sinks, newRequestID: requestIDGenerator,
	}
	dialects, err := NewDialectCompilerRegistry(personas, []DialectCompiler{dialect})
	if err != nil {
		return nil, err
	}
	httpAdapter, err := NewHTTPUpstreamTransportAdapter(port)
	if err != nil {
		return nil, err
	}
	registry, err := NewAdapterRegistry(
		[]BackendDescriptor{{
			Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream,
		}},
		[]TransportAdapter{httpAdapter},
	)
	if err != nil {
		return nil, err
	}
	executor, err := newPersonaExecutorWithIssuer(
		ClaudeExecutorAuthorityID, ClaudeTokenIssuerID, PersonaClaudeCode,
		personas, dialects, registry, guard,
	)
	if err != nil {
		return nil, err
	}
	return &ClaudeCandidateRuntime{
		profile: profile, wire: wire, sinks: sinks, executor: executor,
		newRequestID: requestIDGenerator, newCCH: newClaudeCCH,
		retryJitter: newClaudeRetryJitter, sleep: sleepClaudeRetry,
		startupRuns:   make(map[string]*claudeStartupRun),
		sessions:      make(map[string]*claudeSessionState),
		requestOwners: make(map[string]string),
	}, nil
}

func (r *ClaudeCandidateRuntime) RuleIDs() []string {
	if r == nil {
		return nil
	}
	return r.profile.ruleIDs()
}

func (r *ClaudeCandidateRuntime) ExecuteMessages(
	ctx context.Context,
	input ClaudeMessagesExecution,
) (ClaudeCandidateResult, error) {
	if r == nil || r.executor == nil {
		return ClaudeCandidateResult{}, errors.New("Claude FW-G runtime 未初始化")
	}
	input.AccessToken = strings.TrimSpace(input.AccessToken)
	if input.AccessToken == "" {
		return ClaudeCandidateResult{}, errors.New("Claude candidate 缺少 OAuth access token")
	}
	trusted, ingressState, officialIngress, err := resolveClaudeOfficialIngressBase(
		input.Body, input.Ingress, input.TrustedFacts, r.profile, r.wire,
	)
	if err != nil {
		return ClaudeCandidateResult{}, newClaudeSupportEnvelopeRejection(err)
	}
	canonical, translation, err := parseClaudeCanonicalMessages(
		input.Body, trusted, r.wire, officialIngress,
	)
	if err != nil {
		return ClaudeCandidateResult{}, newClaudeSupportEnvelopeRejection(err)
	}
	if officialIngress {
		classifyClaudeOfficialFallback(&canonical, r.wire)
		if err := completeClaudeOfficialIngressFeatures(
			&trusted, canonical, ingressState, r.wire, input.Ingress.Headers,
		); err != nil {
			return ClaudeCandidateResult{}, newClaudeSupportEnvelopeRejection(err)
		}
	}
	relations, err := validateClaudeMessageRelations(canonical)
	if err != nil {
		return ClaudeCandidateResult{}, newClaudeSupportEnvelopeRejection(err)
	}
	identity, err := deriveClaudeIdentityFacts(trusted)
	if err != nil {
		return ClaudeCandidateResult{}, err
	}
	if identity.ingressProtocol != "anthropic-messages" {
		return ClaudeCandidateResult{}, errors.New("Claude messages 入口协议不是 anthropic-messages")
	}
	input.TrustedFacts = trusted
	if err := r.executeClaudeStartup(ctx, input, identity); err != nil {
		return ClaudeCandidateResult{}, err
	}
	lease, err := r.prepareClaudeSessionRequest(&identity, canonical, relations)
	if err != nil {
		return ClaudeCandidateResult{}, err
	}
	finalizer := &claudeSessionFinalizer{lease: lease}
	result, err := r.executeClaudeMessagesAttempts(
		ctx, input, canonical, translation, identity, finalizer,
	)
	if err != nil {
		_ = finalizer.finalize(false, 0, "")
		return ClaudeCandidateResult{}, err
	}
	return result, nil
}

func validateClaudeMessageRelations(
	canonical ClaudeCanonicalRequest,
) (claudeMessageRelations, error) {
	var relations claudeMessageRelations
	var tools []struct {
		Name string `json:"name"`
	}
	if json.Unmarshal(canonical.tools, &tools) != nil {
		return relations, errors.New("Claude ToolPolicy 目录无法解析")
	}
	allowedNames := make(map[string]struct{}, len(tools))
	for _, tool := range tools {
		name := strings.TrimSpace(tool.Name)
		if name == "" {
			return relations, errors.New("Claude ToolPolicy 含空工具名")
		}
		allowedNames[name] = struct{}{}
	}
	var messages []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(canonical.messages, &messages) != nil {
		return relations, errors.New("Claude messages 无法验证工具往返")
	}
	toolUses := make(map[string]string)
	toolResults := make(map[string]struct{})
	for _, message := range messages {
		var blocks []json.RawMessage
		if json.Unmarshal(message.Content, &blocks) != nil {
			continue
		}
		for _, raw := range blocks {
			fields, err := decodeClaudeUniqueObject(raw)
			if err != nil {
				return relations, errors.New("Claude message content block 不是唯一字段对象")
			}
			var kind string
			if json.Unmarshal(fields["type"], &kind) != nil {
				continue
			}
			switch kind {
			case "tool_use":
				if message.Role != "assistant" {
					return relations, errors.New("Claude tool_use 必须位于 assistant 消息")
				}
				var id, name string
				if json.Unmarshal(fields["id"], &id) != nil ||
					json.Unmarshal(fields["name"], &name) != nil ||
					!claudeToolUseIDPattern.MatchString(id) {
					return relations, errors.New("Claude tool_use 身份非法")
				}
				if _, ok := allowedNames[name]; !ok {
					return relations, fmt.Errorf("Claude tool_use 未绑定当前批准目录：%s", name)
				}
				if _, duplicate := toolUses[id]; duplicate {
					return relations, errors.New("Claude tool_use id 重复")
				}
				toolUses[id] = name
			case "tool_result":
				if message.Role != "user" {
					return relations, errors.New("Claude tool_result 必须位于 user 消息")
				}
				var id string
				if json.Unmarshal(fields["tool_use_id"], &id) != nil ||
					!claudeToolUseIDPattern.MatchString(id) {
					return relations, errors.New("Claude tool_result 身份非法")
				}
				name, ok := toolUses[id]
				if !ok {
					return relations, errors.New("Claude tool_result 缺少同 ID 的先行 tool_use")
				}
				if _, duplicate := toolResults[id]; duplicate {
					return relations, errors.New("Claude tool_result id 重复消费")
				}
				toolResults[id] = struct{}{}
				if name == "WebSearch" {
					relations.webSearchRoundTrip = true
				}
			}
		}
	}
	return relations, nil
}

func (r *ClaudeCandidateRuntime) prepareClaudeSessionRequest(
	identity *ClaudeIdentityFacts,
	canonical ClaudeCanonicalRequest,
	relations claudeMessageRelations,
) (claudeSessionLease, error) {
	if r == nil || identity == nil {
		return claudeSessionLease{}, errors.New("Claude 会话状态机未初始化")
	}
	sessionKey := claudeAttestationDigest(
		"session-state", ClaudeFWGReleaseDigest, identity.accountScope,
		identity.sessionID, identity.entrypoint,
	)
	lineKey := "main"
	if identity.agentID != "" {
		lineKey = "agent:" + identity.agentID
	} else if identity.background {
		lineKey = "background"
	}
	kind := claudeSessionRequestMain
	switch {
	case canonical.scenarioHint == "tui-title":
		kind = claudeSessionRequestTUITitle
	case canonical.toolMode == claudeToolModeWebSearchServer:
		kind = claudeSessionRequestWebSearchServer
	case relations.webSearchRoundTrip:
		kind = claudeSessionRequestWebSearchContinuation
	case canonical.toolMode == claudeToolModeWebSearchOuter:
		kind = claudeSessionRequestWebSearchOuter
	}

	r.sessionMu.Lock()
	defer r.sessionMu.Unlock()
	if r.requestOwners == nil {
		r.requestOwners = make(map[string]string)
	}
	now := time.Now()
	r.pruneClaudeSessionsLocked(now)
	state := r.sessions[sessionKey]
	stateExisted := state != nil
	if identity.forked && stateExisted {
		return claudeSessionLease{}, errors.New("Claude fork 必须使用新的 Session-Id")
	}
	if identity.previousRequestID != "" {
		if owner := r.requestOwners[identity.previousRequestID]; owner != "" && owner != sessionKey {
			if stateExisted {
				return claudeSessionLease{}, errors.New("Claude previous request 跨入既有会话")
			}
			identity.forked = true
		}
	}
	if state == nil {
		if len(r.sessions) >= 16384 {
			return claudeSessionLease{}, errors.New("Claude 会话状态容量已满")
		}
		state = &claudeSessionState{
			lines:         make(map[string]*claudeSessionLineState),
			agentLineages: make(map[string]claudeAgentLineageState),
			requestIDs:    make(map[string]struct{}),
		}
		r.sessions[sessionKey] = state
	}
	state.updatedAt = now
	if identity.entrypoint == ClaudeEntrypointCLI && !identity.background &&
		identity.agentID == "" && kind != claudeSessionRequestTUITitle &&
		!state.tuiTitleCompleted {
		return claudeSessionLease{}, errors.New("Claude TUI 主请求缺少同会话标题阶段")
	}

	newAgent := false
	if identity.agentID != "" {
		expectedDepth := 1
		if identity.parentAgentID != "" {
			parent, ok := state.agentLineages[identity.parentAgentID]
			if !ok {
				return claudeSessionLease{}, errors.New("Claude agent 父谱系尚未由已接受请求建立")
			}
			expectedDepth = parent.depth + 1
		}
		if expectedDepth > 3 {
			return claudeSessionLease{}, errors.New("Claude agent 深度超出已批准三级范围")
		}
		if identity.sessionSource == ClaudeSessionSourceOfficialConsistent {
			identity.agentDepth = expectedDepth
		} else if identity.agentDepth != expectedDepth {
			return claudeSessionLease{}, errors.New("Claude Planner agent 深度与父谱系冲突")
		}
		if existing, ok := state.agentLineages[identity.agentID]; ok {
			if existing.parentAgentID != identity.parentAgentID || existing.depth != expectedDepth {
				return claudeSessionLease{}, errors.New("Claude agent-id 被复用于不同父谱系")
			}
		} else {
			if len(state.agentLineages) >= 256 {
				return claudeSessionLease{}, errors.New("Claude 单会话 agent 谱系容量已满")
			}
			newAgent = true
		}
	}

	switch kind {
	case claudeSessionRequestTUITitle:
		if identity.sessionSource != ClaudeSessionSourceOfficialConsistent ||
			identity.previousRequestID != "" || state.tuiTitleCompleted || state.tuiTitleInFlight {
			return claudeSessionLease{}, errors.New("Claude TUI 标题阶段状态非法")
		}
		state.tuiTitleInFlight = true
	case claudeSessionRequestWebSearchServer:
		if identity.sessionSource != ClaudeSessionSourceOfficialConsistent ||
			identity.agentID != "" || identity.background || identity.previousRequestID != "" ||
			state.webSearchParentRequestID == "" || state.webSearchServerCompleted ||
			state.webSearchServerInFlight {
			return claudeSessionLease{}, errors.New("Claude server web_search 缺少同会话外层父请求")
		}
		state.webSearchServerInFlight = true
	default:
		line := state.lines[lineKey]
		if line == nil {
			line = &claudeSessionLineState{}
			state.lines[lineKey] = line
		}
		if line.inFlight {
			return claudeSessionLease{}, errors.New("Claude 同一会话谱系存在并发推理请求")
		}
		if identity.sessionSource == ClaudeSessionSourcePlannerDerived {
			if identity.previousRequestID != "" {
				return claudeSessionLease{}, errors.New("Claude Planner 会话不得预造 previous request")
			}
			identity.previousRequestID = line.previousRequestID
		} else if line.previousRequestID != "" &&
			identity.previousRequestID != line.previousRequestID {
			return claudeSessionLease{}, errors.New("Claude 官方 cc_prev_req 与会话谱系冲突")
		}
		if kind == claudeSessionRequestWebSearchContinuation &&
			(!state.webSearchServerCompleted || state.webSearchParentRequestID == "" ||
				identity.previousRequestID != state.webSearchParentRequestID) {
			return claudeSessionLease{}, errors.New("Claude WebSearch 续轮缺少已完成的 server 派生请求")
		}
		line.inFlight = true
	}
	return claudeSessionLease{
		runtime: r, sessionKey: sessionKey, lineKey: lineKey, kind: kind,
		newAgent: newAgent, agentID: identity.agentID,
		parentAgentID: identity.parentAgentID, agentDepth: identity.agentDepth,
	}, nil
}

func (r *ClaudeCandidateRuntime) pruneClaudeSessionsLocked(now time.Time) {
	if len(r.sessions) < 4096 {
		return
	}
	for key, state := range r.sessions {
		if now.Sub(state.updatedAt) < 24*time.Hour || state.tuiTitleInFlight ||
			state.webSearchServerInFlight {
			continue
		}
		inFlight := false
		for _, line := range state.lines {
			if line.inFlight {
				inFlight = true
				break
			}
		}
		if !inFlight {
			for requestID := range state.requestIDs {
				delete(r.requestOwners, requestID)
			}
			delete(r.sessions, key)
		}
	}
}

func (f *claudeSessionFinalizer) finalize(
	accepted bool,
	status int,
	requestID string,
) error {
	if f == nil {
		return nil
	}
	f.once.Do(func() {
		if f.lease.runtime == nil {
			f.err = errors.New("Claude 会话 finalizer 缺少 runtime")
			return
		}
		f.err = f.lease.runtime.finalizeClaudeSessionRequest(
			f.lease, accepted, status, requestID,
		)
	})
	return f.err
}

func (r *ClaudeCandidateRuntime) finalizeClaudeSessionRequest(
	lease claudeSessionLease,
	accepted bool,
	status int,
	requestID string,
) error {
	r.sessionMu.Lock()
	defer r.sessionMu.Unlock()
	state := r.sessions[lease.sessionKey]
	if state == nil {
		return errors.New("Claude 会话 finalizer 找不到租约状态")
	}
	state.updatedAt = time.Now()
	var line *claudeSessionLineState
	switch lease.kind {
	case claudeSessionRequestTUITitle:
		if !state.tuiTitleInFlight {
			return errors.New("Claude TUI 标题租约已失效")
		}
		state.tuiTitleInFlight = false
	case claudeSessionRequestWebSearchServer:
		if !state.webSearchServerInFlight {
			return errors.New("Claude server web_search 租约已失效")
		}
		state.webSearchServerInFlight = false
	default:
		line = state.lines[lease.lineKey]
		if line == nil || !line.inFlight {
			return errors.New("Claude 会话谱系租约已失效")
		}
		line.inFlight = false
	}
	if !accepted {
		return nil
	}
	if status < http.StatusOK || status >= http.StatusMultipleChoices {
		return errors.New("Claude 会话不能提交未接受的上游响应")
	}
	requestID = strings.TrimSpace(requestID)
	if !claudeRequestIDPattern.MatchString(requestID) {
		return errors.New("Claude 上游响应缺少合法 request-id")
	}
	if owner := r.requestOwners[requestID]; owner != "" && owner != lease.sessionKey {
		return errors.New("Claude 上游 request-id 跨会话重复")
	}
	if _, exists := state.requestIDs[requestID]; !exists && len(state.requestIDs) >= 4096 {
		return errors.New("Claude 单会话 request-id 容量已满")
	}
	switch lease.kind {
	case claudeSessionRequestTUITitle:
		state.tuiTitleCompleted = true
	case claudeSessionRequestWebSearchServer:
		state.webSearchServerCompleted = true
	case claudeSessionRequestWebSearchOuter:
		line.previousRequestID = requestID
		state.webSearchParentRequestID = requestID
		state.webSearchServerCompleted = false
	case claudeSessionRequestWebSearchContinuation:
		line.previousRequestID = requestID
		state.webSearchParentRequestID = ""
		state.webSearchServerCompleted = false
	default:
		line.previousRequestID = requestID
		if lease.lineKey == "main" {
			state.webSearchParentRequestID = ""
			state.webSearchServerCompleted = false
		}
	}
	if lease.newAgent {
		state.agentLineages[lease.agentID] = claudeAgentLineageState{
			parentAgentID: lease.parentAgentID,
			depth:         lease.agentDepth,
		}
	}
	state.requestIDs[requestID] = struct{}{}
	r.requestOwners[requestID] = lease.sessionKey
	return nil
}

func (r *ClaudeCandidateRuntime) executeClaudeStartup(
	ctx context.Context,
	input ClaudeMessagesExecution,
	identity ClaudeIdentityFacts,
) error {
	key := claudeAttestationDigest(
		"startup-run", identity.accountScope, identity.sessionID, identity.entrypoint,
		fmt.Sprintf("background=%t", identity.background),
	)
	r.startupMu.Lock()
	if current, exists := r.startupRuns[key]; exists {
		r.startupMu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-current.done:
			return current.err
		}
	}
	current := &claudeStartupRun{done: make(chan struct{})}
	r.startupRuns[key] = current
	r.startupMu.Unlock()

	err := r.executeClaudeStartupOnce(ctx, input, identity)
	r.startupMu.Lock()
	current.err = err
	close(current.done)
	if err != nil {
		delete(r.startupRuns, key)
	}
	r.startupMu.Unlock()
	return err
}

func (r *ClaudeCandidateRuntime) executeClaudeStartupOnce(
	ctx context.Context,
	input ClaudeMessagesExecution,
	identity ClaudeIdentityFacts,
) error {
	managed := input.TrustedFacts
	managed.Entrypoint.IngressProtocol = "managed-internal"
	if err := r.executeClaudeStartupEndpoint(ctx, input.AccessToken, managed, "lifecycle-hello"); err != nil {
		return err
	}
	if identity.entrypoint == ClaudeEntrypointCLI && !identity.background {
		for _, kind := range []string{"oauth-profile", "mcp-servers"} {
			if err := r.executeClaudeStartupEndpoint(ctx, input.AccessToken, managed, kind); err != nil {
				return err
			}
		}
		return nil
	}

	errs := make(chan error, 2)
	for _, kind := range []string{"policy-limits", "remote-settings"} {
		kind := kind
		go func() {
			errs <- r.executeClaudeStartupEndpoint(ctx, input.AccessToken, managed, kind)
		}()
	}
	var joined error
	for range 2 {
		joined = errors.Join(joined, <-errs)
	}
	return joined
}

func (r *ClaudeCandidateRuntime) executeClaudeStartupEndpoint(
	ctx context.Context,
	accessToken string,
	trusted ClaudeTrustedFacts,
	kind string,
) error {
	response, err := r.ExecuteEndpoint(ctx, ClaudeEndpointExecution{
		EndpointKind: kind, AccessToken: accessToken,
		TrustedFacts: trusted, InvocationID: uuid.NewString(),
	})
	if err != nil {
		return fmt.Errorf("Claude startup %s：%w", kind, err)
	}
	if response == nil {
		return fmt.Errorf("Claude startup %s 返回空响应", kind)
	}
	status := response.StatusCode
	drainClaudeRetryResponse(response)
	if status < 200 || status >= 300 {
		// Claude Code 把组织 policy／远程托管设置的 404 解释为“当前账号没有
		// 对应配置”，随后按空对象继续启动。FW-F 的受控 R 通道也以 404
		// 复现并确认了这条分支，因此 candidate 不能把合法的配置缺席升级为
		// 主推理故障。其他辅助端点和其他状态仍保持 fail-close。
		if status == http.StatusNotFound &&
			(kind == "policy-limits" || kind == "remote-settings") {
			return nil
		}
		return fmt.Errorf("Claude startup %s 返回状态 %d", kind, status)
	}
	return nil
}

func (r *ClaudeCandidateRuntime) executeClaudeMessagesAttempts(
	ctx context.Context,
	input ClaudeMessagesExecution,
	canonical ClaudeCanonicalRequest,
	translation TranslationReport,
	identity ClaudeIdentityFacts,
	finalizer *claudeSessionFinalizer,
) (ClaudeCandidateResult, error) {
	invocationID := strings.TrimSpace(input.InvocationID)
	if invocationID == "" {
		invocationID = uuid.NewString()
	}
	if _, err := uuid.Parse(invocationID); err != nil {
		return ClaudeCandidateResult{}, errors.New("Claude candidate InvocationID 非法")
	}
	cch, err := r.newCCH()
	if err != nil {
		return ClaudeCandidateResult{}, err
	}
	maxRetries := r.wire.ImplementationPolicy.Retry.DefaultMaxRetries
	if input.TrustedFacts.Features.MaxRetriesSet {
		maxRetries = input.TrustedFacts.Features.MaxRetries
	}
	if maxRetries < 0 || maxRetries > 2 {
		return ClaudeCandidateResult{}, errors.New("Claude candidate retry budget 超出已批准上限")
	}
	endpoint, _ := r.profile.endpoint("messages-inference")
	shape := claudeInitialModelShape(canonical)
	attemptBudget := 1 + maxRetries
	if claudeModelShapeIsStream(shape) {
		attemptBudget++
	}
	initialFallback := canonical.scenarioHint == "fallback"
	if input.TrustedFacts.Features.FallbackModelEnabled && !initialFallback {
		attemptBudget++
	}
	bundle := newClaudeReleaseBundle(endpoint, attemptBudget)
	invocation, err := r.executor.beginInvocation(ctx, bundle, invocationID)
	if err != nil {
		return ClaudeCandidateResult{}, err
	}
	streamFallbackUsed := false
	modelFallbackUsed := false
	retriesUsed := 0
	omitConnection := false
	transitionPending := false
	for ordinal := 1; ordinal <= attemptBudget; ordinal++ {
		authentication, authErr := NewAttemptAuthentication(AttemptAuthenticationInput{
			BearerToken: input.AccessToken,
		})
		if authErr != nil {
			return ClaudeCandidateResult{}, authErr
		}
		plan := ClaudeEgressPlan{
			endpointKind: endpoint.kind, canonical: canonical, translation: translation,
			identity: identity, features: input.TrustedFacts.Features,
			authentication: authentication, modelShape: shape,
			cch: cch, omitConnection: omitConnection, invocationID: invocationID,
			accountScope: identity.accountScope,
		}
		typed := newClaudeDialectPlan(plan, endpoint)
		reason := AttemptReasonInitial
		if ordinal > 1 {
			reason = AttemptReasonRetry
		}
		if transitionPending {
			reason = AttemptReasonFallback
			if err := prepareClaudeFallback(invocation, typed.state.targetIdentity()); err != nil {
				return ClaudeCandidateResult{}, err
			}
			transitionPending = false
		}
		attemptCtx := ctx
		cancel := func() {}
		if input.TrustedFacts.Features.APITimeoutMS > 0 {
			attemptCtx, cancel = context.WithTimeout(ctx, time.Duration(input.TrustedFacts.Features.APITimeoutMS)*time.Millisecond)
		}
		result, executeErr := invocation.executeTypedAttempt(attemptCtx, personaExecutorRequest{
			bundle: bundle, plan: typed, attemptReason: reason,
			expectedAttemptOrdinal: uint32(ordinal), executionScopeKey: identity.accountScope,
		})
		cancel()
		wireBody, model, _, stream, bodyErr := compileClaudeMessagesBody(plan, r.wire)
		if bodyErr != nil {
			return ClaudeCandidateResult{}, bodyErr
		}
		response := result.HTTPResponse()
		if executeErr == nil && response == nil {
			return ClaudeCandidateResult{}, errors.New("Claude Executor HTTP adapter 返回空响应")
		}
		if executeErr == nil && response.StatusCode == http.StatusNotFound && stream &&
			!streamFallbackUsed && ordinal < attemptBudget {
			drainClaudeRetryResponse(response)
			shape = claudeNonStreamModelShape(shape)
			streamFallbackUsed = true
			transitionPending = true
			cch, err = r.newCCH()
			if err != nil {
				return ClaudeCandidateResult{}, err
			}
			continue
		}
		if executeErr == nil && !r.claudeRetryableStatus(response.StatusCode) {
			candidate := newClaudeCandidateResult(
				response, wireBody, model, stream, ordinal, finalizer,
			)
			if stream && !input.TrustedFacts.Features.DisableStreamFallback && ordinal < attemptBudget {
				candidate.StreamFallback = r.newClaudeStreamFallback(
					invocation, input, canonical, translation, identity,
					endpoint, bundle, invocationID, shape, ordinal+1, finalizer,
				)
			}
			return candidate, nil
		}
		if retriesUsed >= maxRetries &&
			(!input.TrustedFacts.Features.FallbackModelEnabled || modelFallbackUsed) {
			if executeErr != nil {
				return ClaudeCandidateResult{}, executeErr
			}
			return newClaudeCandidateResult(
				response, wireBody, model, stream, ordinal, finalizer,
			), nil
		}
		delay, delayErr := r.claudeRetryDelay(response, retriesUsed+1)
		if delayErr != nil {
			return ClaudeCandidateResult{}, delayErr
		}
		if response != nil {
			drainClaudeRetryResponse(response)
		}
		if err := r.sleep(ctx, delay); err != nil {
			return ClaudeCandidateResult{}, err
		}
		if retriesUsed < maxRetries {
			retriesUsed++
			omitConnection = executeErr != nil
			continue
		}
		if input.TrustedFacts.Features.FallbackModelEnabled && !initialFallback && !modelFallbackUsed {
			shape = claudeFallbackModelShape(shape)
			modelFallbackUsed = true
			transitionPending = true
			omitConnection = false
			cch, err = r.newCCH()
			if err != nil {
				return ClaudeCandidateResult{}, err
			}
			continue
		}
		if executeErr != nil {
			return ClaudeCandidateResult{}, executeErr
		}
		return newClaudeCandidateResult(
			response, wireBody, model, stream, ordinal, finalizer,
		), nil
	}
	return ClaudeCandidateResult{}, errors.New("Claude candidate attempt 状态机异常退出")
}

func (r *ClaudeCandidateRuntime) newClaudeStreamFallback(
	invocation *ExecutorInvocation,
	input ClaudeMessagesExecution,
	canonical ClaudeCanonicalRequest,
	translation TranslationReport,
	identity ClaudeIdentityFacts,
	endpoint claudeEndpointProfile,
	bundle claudeReleaseBundle,
	invocationID string,
	shape claudeModelShape,
	ordinal int,
	finalizer *claudeSessionFinalizer,
) func(context.Context) (ClaudeCandidateResult, error) {
	var mu sync.Mutex
	used := false
	return func(ctx context.Context) (ClaudeCandidateResult, error) {
		mu.Lock()
		if used {
			mu.Unlock()
			return ClaudeCandidateResult{}, errors.New("Claude stream fallback continuation 已消费")
		}
		used = true
		mu.Unlock()

		cch, err := r.newCCH()
		if err != nil {
			return ClaudeCandidateResult{}, err
		}
		authentication, err := NewAttemptAuthentication(AttemptAuthenticationInput{
			BearerToken: input.AccessToken,
		})
		if err != nil {
			return ClaudeCandidateResult{}, err
		}
		plan := ClaudeEgressPlan{
			endpointKind: endpoint.kind, canonical: canonical, translation: translation,
			identity: identity, features: input.TrustedFacts.Features,
			authentication: authentication, modelShape: claudeNonStreamModelShape(shape),
			cch: cch, invocationID: invocationID, accountScope: identity.accountScope,
		}
		typed := newClaudeDialectPlan(plan, endpoint)
		if err := prepareClaudeFallback(invocation, typed.state.targetIdentity()); err != nil {
			return ClaudeCandidateResult{}, err
		}
		result, err := invocation.executeTypedAttempt(ctx, personaExecutorRequest{
			bundle: bundle, plan: typed, attemptReason: AttemptReasonFallback,
			expectedAttemptOrdinal: uint32(ordinal), executionScopeKey: identity.accountScope,
		})
		if err != nil {
			return ClaudeCandidateResult{}, err
		}
		response := result.HTTPResponse()
		if response == nil {
			return ClaudeCandidateResult{}, errors.New("Claude stream fallback 返回空响应")
		}
		wireBody, model, _, stream, err := compileClaudeMessagesBody(plan, r.wire)
		if err != nil {
			drainClaudeRetryResponse(response)
			return ClaudeCandidateResult{}, err
		}
		return newClaudeCandidateResult(
			response, wireBody, model, stream, ordinal, finalizer,
		), nil
	}
}

func (r *ClaudeCandidateRuntime) ExecuteEndpoint(
	ctx context.Context,
	input ClaudeEndpointExecution,
) (*http.Response, error) {
	result, err := r.executeEndpoint(ctx, input)
	if err != nil {
		return nil, err
	}
	return result.Response, nil
}

// ExecuteCountTokens 把公开 count_tokens 入口接入与其他 Claude strict 端点
// 相同的 Planner／Compiler／Executor／Guard 链，并把最终 wire body 交还网关。
func (r *ClaudeCandidateRuntime) ExecuteCountTokens(
	ctx context.Context,
	input ClaudeEndpointExecution,
) (ClaudeEndpointResult, error) {
	input.EndpointKind = "count-tokens"
	return r.executeEndpoint(ctx, input)
}

func (r *ClaudeCandidateRuntime) executeEndpoint(
	ctx context.Context,
	input ClaudeEndpointExecution,
) (ClaudeEndpointResult, error) {
	if r == nil || r.executor == nil {
		return ClaudeEndpointResult{}, errors.New("Claude FW-G runtime 未初始化")
	}
	endpoint, err := r.profile.endpoint(strings.TrimSpace(input.EndpointKind))
	if err != nil {
		return ClaudeEndpointResult{}, err
	}
	if endpoint.kind == "messages-inference" {
		return ClaudeEndpointResult{}, errors.New("messages-inference 必须使用 ExecuteMessages 状态机")
	}
	if endpoint.kind == "count-tokens" {
		input.TrustedFacts, err = resolveClaudeOfficialCountTokensIngress(
			input.Ingress, input.TrustedFacts, r.profile,
		)
		if err != nil {
			return ClaudeEndpointResult{}, err
		}
	}
	identity, err := deriveClaudeIdentityFacts(input.TrustedFacts)
	if err != nil {
		return ClaudeEndpointResult{}, err
	}
	if identity.ingressProtocol != "managed-internal" {
		return ClaudeEndpointResult{}, errors.New("Claude strict endpoint 缺少 managed-internal binding")
	}
	canonical := ClaudeCanonicalRequest{}
	if endpoint.kind == "count-tokens" {
		canonical, err = parseClaudeCountTokens(input.Body, r.wire)
		if err != nil {
			return ClaudeEndpointResult{}, err
		}
	}
	authInput := AttemptAuthenticationInput{}
	if claudeEndpointNeedsBearer(endpoint.kind) {
		authInput.BearerToken = strings.TrimSpace(input.AccessToken)
		if authInput.BearerToken == "" {
			return ClaudeEndpointResult{}, errors.New("Claude strict endpoint 缺少 OAuth access token")
		}
	}
	if endpoint.kind == "oauth-token-refresh" {
		authInput.RefreshToken = strings.TrimSpace(input.RefreshToken)
		authInput.Attestation = strings.TrimSpace(input.ClientID)
		if authInput.RefreshToken == "" || authInput.Attestation == "" ||
			strings.TrimSpace(input.RefreshScope) == "" {
			return ClaudeEndpointResult{}, errors.New("Claude OAuth refresh 缺少受管凭据")
		}
	}
	authentication, err := NewAttemptAuthentication(authInput)
	if err != nil {
		return ClaudeEndpointResult{}, err
	}
	invocationID := strings.TrimSpace(input.InvocationID)
	if invocationID == "" {
		invocationID = uuid.NewString()
	}
	if _, err := uuid.Parse(invocationID); err != nil {
		return ClaudeEndpointResult{}, errors.New("Claude strict endpoint InvocationID 非法")
	}
	plan := ClaudeEgressPlan{
		endpointKind: endpoint.kind, canonical: canonical,
		translation: TranslationReport{IngressProtocol: "managed-internal", Lossless: true, Compatibility: "persona_strict"},
		identity:    identity, features: input.TrustedFacts.Features,
		authentication: authentication,
		refreshScope:   strings.TrimSpace(input.RefreshScope),
		invocationID:   invocationID, accountScope: identity.accountScope,
	}
	wireBody := []byte(nil)
	if endpoint.kind == "count-tokens" {
		wireBody, _, _, _, err = compileClaudeEndpointBody(
			plan, r.wire, AttemptAuthenticationInput{},
		)
		if err != nil {
			return ClaudeEndpointResult{}, err
		}
	}
	typed := newClaudeDialectPlan(plan, endpoint)
	bundle := newClaudeReleaseBundle(endpoint, 1)
	invocation, err := r.executor.beginInvocation(ctx, bundle, invocationID)
	if err != nil {
		return ClaudeEndpointResult{}, err
	}
	result, err := invocation.executeTypedAttempt(ctx, personaExecutorRequest{
		bundle: bundle, plan: typed, attemptReason: AttemptReasonInitial,
		expectedAttemptOrdinal: 1, executionScopeKey: identity.accountScope,
	})
	if err != nil {
		return ClaudeEndpointResult{}, err
	}
	if result.HTTPResponse() == nil {
		return ClaudeEndpointResult{}, errors.New("Claude strict endpoint 返回空响应")
	}
	return ClaudeEndpointResult{Response: result.HTTPResponse(), WireBody: wireBody}, nil
}

func newClaudeReleaseBundle(endpoint claudeEndpointProfile, attempts int) claudeReleaseBundle {
	return claudeReleaseBundle{control: executorBundleControl{
		persona: PersonaClaudeCode, mode: ReleaseModeActive,
		profileDigest: ClaudeFWGProfileDigest, releaseDigest: ClaudeFWGReleaseDigest,
		bundleDigest: ClaudeFWGBundleDigest, primarySinkID: endpoint.sinkID,
		attempt: executorAttemptControl{
			policyID:               "claude-code-2.1.226." + endpoint.kind,
			invocationAttemptLimit: attempts, transportAttemptLimit: attempts,
			replayable: true,
		},
	}}
}

func newClaudeDialectPlan(
	plan ClaudeEgressPlan,
	endpoint claudeEndpointProfile,
) claudeDialectPlan {
	identityDigest := plan.identity.digest()
	dialectDigest := claudeAttestationDigest(
		"dialect", endpoint.kind, string(plan.modelShape),
		plan.translation.IngressProtocol, plan.translation.Compatibility,
		fmt.Sprintf("%t", plan.features.RequestGzip),
	)
	invocationDigest := claudeAttestationDigest(
		"invocation", plan.identity.invocationDigest(), endpoint.kind,
		plan.translation.IngressProtocol, plan.translation.Compatibility,
	)
	return claudeDialectPlan{
		plan: plan, endpoint: endpoint,
		state: executorPlanControl{
			persona: PersonaClaudeCode, sinkID: endpoint.sinkID, purpose: endpoint.purpose,
			endpointID: endpoint.id, mode: ReleaseModeActive, protocol: WireProtocolHTTP,
			method: endpoint.method, target: endpoint.target, invocationID: plan.invocationID,
			bodyReplayable: true, identityAttestationDigest: identityDigest,
			dialectAttestationDigest:    dialectDigest,
			invocationAttestationDigest: invocationDigest,
		},
	}
}

func compileClaudeEndpointBody(
	plan ClaudeEgressPlan,
	wire claudeWireArtifact,
	authentication AttemptAuthenticationInput,
) ([]byte, string, string, bool, error) {
	switch plan.endpointKind {
	case "messages-inference":
		return compileClaudeMessagesBody(plan, wire)
	case "count-tokens":
		model := wire.ImplementationPolicy.Scenarios.TUIMain.Model
		if model == "" {
			return nil, "", "", false, errors.New("Claude count_tokens 缺少 Wire 模型事实")
		}
		modelRaw, _ := marshalClaudeJSON(model)
		body, err := marshalClaudeOrderedObject([]claudeJSONField{
			{name: "model", raw: modelRaw},
			{name: "messages", raw: plan.canonical.messages},
			{name: "tools", raw: plan.canonical.tools},
		})
		return body, model,
			"claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-management-2025-06-27,token-counting-2024-11-01",
			false, err
	case "oauth-token-refresh":
		grant, _ := marshalClaudeJSON("refresh_token")
		refresh, _ := marshalClaudeJSON(authentication.RefreshToken)
		clientID, _ := marshalClaudeJSON(authentication.Attestation)
		scope, _ := marshalClaudeJSON(plan.refreshScope)
		body, err := marshalClaudeOrderedObject([]claudeJSONField{
			{name: "grant_type", raw: grant}, {name: "refresh_token", raw: refresh},
			{name: "client_id", raw: clientID}, {name: "scope", raw: scope},
		})
		return body, "", "", false, err
	case "lifecycle-hello", "policy-limits", "remote-settings", "oauth-profile", "mcp-servers":
		return nil, "", "", false, nil
	default:
		return nil, "", "", false, fmt.Errorf("Claude endpoint body policy 未登记：%s", plan.endpointKind)
	}
}

func compileClaudeEndpointHeaders(
	plan ClaudeEgressPlan,
	endpoint claudeEndpointProfile,
	wire claudeWireArtifact,
	authentication AttemptAuthenticationInput,
	body []byte,
	model string,
	beta string,
	requestID string,
) (http.Header, []string, error) {
	_ = model
	facts := claudeHeaderFacts(endpoint.headers)
	order, err := claudeHeaderOrder(endpoint.headers, false)
	if err != nil {
		return nil, nil, err
	}
	var scenario claudeWireScenario
	if endpoint.kind == "messages-inference" {
		scenario, err = selectClaudeWireScenario(plan, wire)
		if err != nil {
			return nil, nil, err
		}
		order, err = compileClaudeMessagesHeaderOrder(plan, wire, order)
		if err != nil {
			return nil, nil, err
		}
	}
	customHeaders, customHeaderOrder, err := parseClaudeCustomHeaders(
		plan.features.CustomHeaderLines, wire,
	)
	if err != nil {
		return nil, nil, err
	}
	if endpoint.kind == "messages-inference" && len(customHeaders) != 0 {
		order, err = insertClaudeHeadersAfter(
			order, wire.ImplementationPolicy.Headers.CustomInsertAfter, customHeaderOrder,
		)
		if err != nil {
			return nil, nil, err
		}
	}
	headers := make(http.Header)
	for _, wireName := range order {
		lower := strings.ToLower(wireName)
		switch lower {
		case "host", "content-length":
			continue
		case "authorization":
			if authentication.BearerToken == "" {
				return nil, nil, fmt.Errorf("Claude endpoint %s 缺少 Bearer token", endpoint.kind)
			}
			headers[wireName] = []string{"Bearer " + authentication.BearerToken}
		case "x-claude-code-session-id":
			if plan.identity.sessionID == "" {
				return nil, nil, errors.New("Claude endpoint 缺少 session identity")
			}
			headers[wireName] = []string{plan.identity.sessionID}
		case "x-client-request-id":
			headers[wireName] = []string{requestID}
		case "x-claude-code-agent-id":
			if plan.identity.agentID != "" {
				headers[wireName] = []string{plan.identity.agentID}
			}
		case "x-claude-code-parent-agent-id":
			if plan.identity.parentAgentID != "" {
				headers[wireName] = []string{plan.identity.parentAgentID}
			}
		case "x-anthropic-additional-protection":
			if plan.features.AdditionalProtection {
				headers[wireName] = []string{"true"}
			}
		case "x-claude-remote-container-id":
			if plan.features.RemoteContainerID != "" {
				headers[wireName] = []string{plan.features.RemoteContainerID}
			}
		case "x-claude-remote-session-id":
			if plan.features.RemoteSessionID != "" {
				headers[wireName] = []string{plan.features.RemoteSessionID}
			}
		case "x-client-app":
			if plan.features.ClientApp != "" {
				headers[wireName] = []string{plan.features.ClientApp}
			}
		case "anthropic-beta":
			value := beta
			if value == "" {
				value = facts[lower].Value
			}
			if value == "" {
				return nil, nil, errors.New("Claude endpoint 缺少 anthropic-beta")
			}
			headers[wireName] = []string{value}
		case "content-encoding":
			headers[wireName] = []string{"gzip"}
		case "user-agent":
			value := facts[lower].Value
			if endpoint.kind == "messages-inference" {
				value, err = compileClaudeUserAgent(scenario.UserAgent, plan.features)
				if err != nil {
					return nil, nil, err
				}
			}
			if value == "" {
				return nil, nil, fmt.Errorf("Claude endpoint %s 缺少 User-Agent", endpoint.kind)
			}
			headers[wireName] = []string{value}
		case "x-app":
			value := facts[lower].Value
			if endpoint.kind == "messages-inference" {
				value = scenario.XApp
			}
			if value == "" {
				return nil, nil, fmt.Errorf("Claude endpoint %s 缺少 x-app", endpoint.kind)
			}
			headers[wireName] = []string{value}
		case "x-stainless-timeout":
			value := facts[lower].Value
			if endpoint.kind == "messages-inference" {
				seconds := wire.ImplementationPolicy.Retry.StreamTimeoutSeconds
				if !claudeModelShapeIsStream(plan.modelShape) {
					seconds = wire.ImplementationPolicy.Retry.NonStreamTimeoutSeconds
				}
				if plan.features.APITimeoutMS > 0 {
					seconds = (plan.features.APITimeoutMS + 999) / 1000
				}
				value = strconv.Itoa(seconds)
			}
			headers[wireName] = []string{value}
		default:
			if value, ok := customHeaders[wireName]; ok {
				headers[wireName] = []string{value}
				continue
			}
			fact, ok := facts[lower]
			if !ok || fact.Value == "" {
				return nil, nil, fmt.Errorf("Claude endpoint %s 缺少 Header 事实：%s", endpoint.kind, wireName)
			}
			headers[wireName] = []string{fact.Value}
		}
	}
	if len(body) == 0 && claudeHTTPMethodAllowsBody(endpoint.method) {
		return nil, nil, fmt.Errorf("Claude POST endpoint %s 缺少 Body", endpoint.kind)
	}
	return headers, order, nil
}

func compileClaudeMessagesHeaderOrder(
	plan ClaudeEgressPlan,
	wire claudeWireArtifact,
	base []string,
) ([]string, error) {
	conditional := wire.ImplementationPolicy.Headers.ConditionalOrder
	if len(conditional) == 0 {
		return nil, errors.New("Claude Wire 缺少 conditional Header 顺序")
	}
	conditionalSet := make(map[string]struct{}, len(conditional))
	for _, name := range conditional {
		conditionalSet[strings.ToLower(name)] = struct{}{}
	}
	anchor := -1
	out := make([]string, 0, len(base)+len(conditional)+1)
	for _, name := range base {
		if _, managed := conditionalSet[strings.ToLower(name)]; managed {
			if anchor < 0 {
				anchor = len(out)
			}
			continue
		}
		out = append(out, name)
	}
	if anchor < 0 {
		return nil, errors.New("Claude 基础 Header 顺序缺少 conditional 锚点")
	}
	selected := make([]string, 0, len(conditional)+1)
	for _, name := range conditional {
		switch strings.ToLower(name) {
		case "x-anthropic-additional-protection":
			if plan.features.AdditionalProtection {
				selected = append(selected, name)
			}
		case "x-app", "x-client-request-id":
			selected = append(selected, name)
		case "x-claude-remote-container-id":
			if plan.features.RemoteContainerID != "" {
				selected = append(selected, name)
			}
		case "x-claude-remote-session-id":
			if plan.features.RemoteSessionID != "" {
				selected = append(selected, name)
			}
		case "x-client-app":
			if plan.features.ClientApp != "" {
				selected = append(selected, name)
			}
		case "x-claude-code-agent-id":
			if plan.identity.agentID != "" {
				selected = append(selected, name)
			}
		case "x-claude-code-parent-agent-id":
			if plan.identity.parentAgentID != "" {
				selected = append(selected, name)
			}
		default:
			return nil, fmt.Errorf("Claude Wire 含未知 conditional Header：%s", name)
		}
	}
	out = append(out, make([]string, len(selected))...)
	copy(out[anchor+len(selected):], out[anchor:len(out)-len(selected)])
	copy(out[anchor:], selected)
	if plan.features.RequestGzip {
		var err error
		out, err = insertClaudeHeaderAfter(out, "x-client-request-id", "Content-Encoding")
		if err != nil {
			return nil, err
		}
	}
	if plan.omitConnection {
		out = removeClaudeHeader(out, "Connection")
	}
	return out, nil
}

func parseClaudeCustomHeaders(
	lines []string,
	wire claudeWireArtifact,
) (map[string]string, []string, error) {
	protected := make(map[string]struct{}, len(wire.ImplementationPolicy.Headers.ProtectedNames))
	for _, name := range wire.ImplementationPolicy.Headers.ProtectedNames {
		protected[strings.ToLower(name)] = struct{}{}
	}
	out := make(map[string]string)
	order := make([]string, 0)
	seen := make(map[string]struct{})
	for _, source := range lines {
		for _, line := range strings.Split(strings.ReplaceAll(source, "\r\n", "\n"), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || !strings.Contains(line, ":") {
				continue
			}
			parts := strings.SplitN(line, ":", 2)
			name := strings.TrimSpace(parts[0])
			value := strings.TrimSpace(parts[1])
			if name == "" {
				return nil, nil, errors.New("Claude custom Header 名不能为空")
			}
			if !claudeCustomHeaderNamePattern.MatchString(name) || strings.ContainsAny(value, "\r\n") {
				return nil, nil, fmt.Errorf("Claude custom Header 非法：%s", name)
			}
			lower := strings.ToLower(name)
			if _, blocked := protected[lower]; blocked {
				continue
			}
			if _, duplicate := seen[lower]; duplicate {
				return nil, nil, fmt.Errorf("Claude custom Header 重复：%s", name)
			}
			seen[lower] = struct{}{}
			out[name] = value
			order = append(order, name)
		}
	}
	return out, order, nil
}

func insertClaudeHeadersAfter(
	order []string,
	anchor string,
	names []string,
) ([]string, error) {
	out := append([]string(nil), order...)
	for index := len(names) - 1; index >= 0; index-- {
		var err error
		out, err = insertClaudeHeaderAfter(out, anchor, names[index])
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}

func insertClaudeHeaderAfter(order []string, anchor string, name string) ([]string, error) {
	for index, current := range order {
		if !strings.EqualFold(current, anchor) {
			continue
		}
		out := append([]string(nil), order...)
		out = append(out, "")
		copy(out[index+2:], out[index+1:])
		out[index+1] = name
		return out, nil
	}
	return nil, fmt.Errorf("Claude Header 顺序缺少锚点：%s", anchor)
}

func removeClaudeHeader(order []string, name string) []string {
	out := make([]string, 0, len(order))
	for _, current := range order {
		if !strings.EqualFold(current, name) {
			out = append(out, current)
		}
	}
	return out
}

func compileClaudeUserAgent(base string, features ClaudeTrustedFeatureFacts) (string, error) {
	base = strings.TrimSpace(base)
	closing := strings.LastIndex(base, ")")
	if base == "" || closing < 0 || closing != len(base)-1 {
		return "", errors.New("Claude Wire User-Agent 形态非法")
	}
	segments := []string{}
	if features.AgentSDKVersion != "" {
		segments = append(segments, "agent-sdk/"+features.AgentSDKVersion)
	}
	if features.ClientApp != "" {
		segments = append(segments, "client-app/"+features.ClientApp)
	}
	if features.Workload != "" {
		segments = append(segments, "workload/"+features.Workload)
	}
	if len(segments) == 0 {
		return base, nil
	}
	return base[:closing] + ", " + strings.Join(segments, ", ") + ")", nil
}

func parseClaudeCountTokens(
	body []byte,
	wire claudeWireArtifact,
) (ClaudeCanonicalRequest, error) {
	document, err := decodeClaudeUniqueObject(body)
	if err != nil || !rawJSONArray(document["messages"]) {
		return ClaudeCanonicalRequest{}, errors.New("Claude count_tokens messages 必须是数组")
	}
	for name := range document {
		if name != "model" && name != "messages" && name != "tools" {
			return ClaudeCanonicalRequest{}, fmt.Errorf("Claude count_tokens 含未批准字段：%s", name)
		}
	}
	tools, toolsPresent := document["tools"]
	if !toolsPresent {
		return ClaudeCanonicalRequest{}, errors.New("Claude count_tokens tools 必须显式提供")
	}
	if !rawJSONArray(tools) {
		return ClaudeCanonicalRequest{}, errors.New("Claude count_tokens tools 必须是数组")
	}
	var model string
	if json.Unmarshal(document["model"], &model) != nil ||
		strings.TrimSpace(model) != wire.ImplementationPolicy.Scenarios.TUIMain.Model {
		return ClaudeCanonicalRequest{}, errors.New("Claude count_tokens model 不在 SupportEnvelope")
	}
	return ClaudeCanonicalRequest{
		messages: append(json.RawMessage(nil), document["messages"]...),
		tools:    append(json.RawMessage(nil), tools...),
	}, nil
}

func claudeEndpointNeedsBearer(kind string) bool {
	switch kind {
	case "messages-inference", "policy-limits", "remote-settings", "oauth-profile", "count-tokens", "mcp-servers":
		return true
	default:
		return false
	}
}

func claudeEndpointNeedsRequestID(kind string) bool {
	return kind == "messages-inference" || kind == "count-tokens"
}

func prepareClaudeFallback(
	invocation *ExecutorInvocation,
	target invocationAttemptTarget,
) error {
	if invocation == nil || invocation.executor == nil {
		return errors.New("Claude fallback invocation 未初始化")
	}
	invocation.mu.Lock()
	defer invocation.mu.Unlock()
	if invocation.attempts == 0 || invocation.pendingTransition != nil ||
		target != (invocationAttemptTarget{
			sinkID: invocation.currentSink, purpose: target.purpose,
			endpointID: target.endpointID, protocol: target.protocol,
		}) {
		return errors.New("Claude fallback transition 状态非法")
	}
	cloned := target
	invocation.pendingTransition = &cloned
	return nil
}

func newClaudeCCH() (string, error) {
	var raw [3]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("生成 Claude cch：%w", err)
	}
	value := (uint32(raw[0]) << 12) | (uint32(raw[1]) << 4) | uint32(raw[2]&0x0f)
	return fmt.Sprintf("%05x", value), nil
}

func newClaudeRetryJitter(maxInclusive int) (int, error) {
	if maxInclusive < 0 {
		return 0, errors.New("Claude retry jitter 上限非法")
	}
	value, err := rand.Int(rand.Reader, big.NewInt(int64(maxInclusive)+1))
	if err != nil {
		return 0, fmt.Errorf("生成 Claude retry jitter：%w", err)
	}
	return int(value.Int64()), nil
}

func (r *ClaudeCandidateRuntime) claudeRetryableStatus(status int) bool {
	for _, candidate := range r.wire.ImplementationPolicy.Retry.RetryableStatuses {
		if candidate == status {
			return true
		}
	}
	return false
}

func (r *ClaudeCandidateRuntime) claudeRetryDelay(
	response *http.Response,
	ordinal int,
) (time.Duration, error) {
	if response != nil {
		value := strings.TrimSpace(response.Header.Get("Retry-After"))
		if seconds, err := strconv.Atoi(value); err == nil && seconds >= 0 {
			jitter, jitterErr := r.retryJitter(
				r.wire.ImplementationPolicy.Retry.RetryAfterSecondsJitterMaxMS,
			)
			if jitterErr != nil {
				return 0, jitterErr
			}
			return time.Duration(seconds)*time.Second + time.Duration(jitter)*time.Millisecond, nil
		}
	}
	if ordinal < 1 {
		ordinal = 1
	}
	bases := r.wire.ImplementationPolicy.Retry.DefaultBaseMS
	if len(bases) == 0 {
		return 0, errors.New("Claude Wire 缺少 retry base")
	}
	index := ordinal - 1
	if index >= len(bases) {
		index = len(bases) - 1
	}
	jitter, err := r.retryJitter(r.wire.ImplementationPolicy.Retry.DefaultJitterMaxMS)
	if err != nil {
		return 0, err
	}
	return time.Duration(bases[index]+jitter) * time.Millisecond, nil
}

func sleepClaudeRetry(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func drainClaudeRetryResponse(response *http.Response) {
	if response == nil || response.Body == nil {
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	_ = response.Body.Close()
}

func claudeSHA256Hex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}
