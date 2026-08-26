package service

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
)

const (
	// 主 Forward 最多容纳 WS 初始/重连、一次显式 HTTP fallback 与有界 HTTP 修正重试。
	officialCodexMainForwardAttemptBudget = 16
	// WS ingress 是一个可承载多 turn 的上层 invocation，仍以显式总预算限制失控会话。
	officialCodexIngressForwardAttemptBudget = 4096
	// 单纯 HTTP Responses 入口的修正重试均为有界分支，八次足以覆盖全部互斥恢复条件。
	officialCodexHTTPForwardAttemptBudget = 8
)

// OpenAIForwardAccountIdentity 是 invocation 开始时冻结的账号身份投影。
// access token 不属于该投影，只能在单个 attempt 中即时提供。
type OpenAIForwardAccountIdentity struct {
	AccountID int64
	Platform  string
	Type      string
	AuthMode  string
}

// OpenAIForwardInvocationPlan 冻结一次调用中不得随 retry/fallback 改变的事实。
// TransportSpec 不在本结构中，只能由 ReleaseBundle 的 endpoint 画像决定。
type OpenAIForwardInvocationPlan struct {
	runtime    *OfficialEgressTransitionRuntime
	bundle     officialegress.ReleaseBundle
	invocation *officialegress.ExecutorInvocation

	accountIdentity        OpenAIForwardAccountIdentity
	identityAccount        officialCodexIdentityAccountProjection
	identityMode           officialegress.IdentityMode
	resolvedHeaderOverride http.Header
	headerPolicy           officialegress.HeaderPolicy
	executionPolicy        officialegress.ExecutionPolicy
	behaviorPolicy         officialegress.BehaviorPolicy
	deploymentPolicy       officialegress.DeploymentSupportPolicy
	proxyURL               string
	concurrencyLimit       int

	mu             sync.Mutex
	nextOrdinal    uint32
	currentSink    officialegress.SinkID
	pendingOrdinal uint32
}

type openAIForwardInvocationPlanInput struct {
	Runtime          *OfficialEgressTransitionRuntime
	Account          *Account
	PrimarySinkID    officialegress.SinkID
	InvocationID     string
	IdentityMode     officialegress.IdentityMode
	HeaderPolicy     officialegress.HeaderPolicy
	ExecutionPolicy  officialegress.ExecutionPolicy
	BehaviorPolicy   officialegress.BehaviorPolicy
	DeploymentPolicy officialegress.DeploymentSupportPolicy
	ProxyURL         string
}

type officialCodexResponseForwardPlanInput struct {
	Runtime         *OfficialEgressTransitionRuntime
	Account         *Account
	PrimarySinkID   officialegress.SinkID
	InvocationID    string
	ProxyURL        string
	PolicyID        string
	PolicySource    string
	FallbackSinkIDs []officialegress.SinkID
	AttemptBudget   int
}

// newOfficialCodexResponseForwardPlan 统一构造 Responses 主链的冻结策略。
// 调用方只声明业务 sink 与预算，不能选择 TransportSpec。
func newOfficialCodexResponseForwardPlan(
	ctx context.Context,
	input officialCodexResponseForwardPlanInput,
) (*OpenAIForwardInvocationPlan, error) {
	if input.AttemptBudget <= 0 {
		return nil, errors.New("Responses Forward attempt budget 必须显式大于零")
	}
	if strings.TrimSpace(input.PolicyID) == "" || strings.TrimSpace(input.PolicySource) == "" {
		return nil, errors.New("Responses Forward 缺少策略来源")
	}
	proxyMode := "direct"
	if strings.TrimSpace(input.ProxyURL) != "" {
		proxyMode = "configured_proxy"
	}
	concurrencyLimit := 1
	if input.Account != nil && input.Account.EffectiveLoadFactor() > 0 {
		concurrencyLimit = input.Account.EffectiveLoadFactor()
	}
	backends := []officialegress.BackendKind{officialegress.BackendHTTPUpstream}
	if input.PrimarySinkID == officialegress.SinkCodexResponsesWS {
		backends = append(backends, officialegress.BackendWebSocket)
	}
	return newOpenAIForwardInvocationPlan(ctx, openAIForwardInvocationPlanInput{
		Runtime: input.Runtime, Account: input.Account,
		PrimarySinkID: input.PrimarySinkID, InvocationID: input.InvocationID,
		IdentityMode: officialegress.IdentityCodexOAuthStrict,
		HeaderPolicy: officialegress.HeaderPolicy{
			ID: input.PolicyID + ".headers", Source: input.PolicySource,
		},
		ExecutionPolicy: officialegress.ExecutionPolicy{
			ID: input.PolicyID + ".execution", Source: input.PolicySource,
			MaxAttempts: input.AttemptBudget, Replayable: true,
			ConcurrencyLimit: concurrencyLimit,
		},
		BehaviorPolicy: officialegress.BehaviorPolicy{
			ID: input.PolicyID + ".behavior", Source: input.PolicySource,
			Kind:            officialegress.BehaviorUserRequest,
			FallbackSinkIDs: append([]officialegress.SinkID(nil), input.FallbackSinkIDs...),
			AttemptBudget:   input.AttemptBudget,
		},
		DeploymentPolicy: officialegress.DeploymentSupportPolicy{
			ID: input.PolicyID + ".deployment", Source: input.PolicySource,
			Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: proxyMode,
			ProxyIdentityDigest: officialEgressProxyStateKey(input.ProxyURL),
			SupportedBackends:   backends,
		},
		ProxyURL: input.ProxyURL,
	})
}

func newOpenAIForwardInvocationPlan(
	ctx context.Context,
	input openAIForwardInvocationPlanInput,
) (*OpenAIForwardInvocationPlan, error) {
	if input.Runtime == nil || input.Runtime.BundleResolver == nil ||
		input.Runtime.CodexExecutor == nil {
		return nil, errors.New("OpenAI Forward 缺少正式 Codex Executor runtime")
	}
	if input.Account == nil || input.Account.ID <= 0 ||
		input.Account.Platform != PlatformOpenAI || !input.IdentityMode.Valid() {
		return nil, errors.New("OpenAI Forward invocation 账号或身份模式非法")
	}
	if err := input.HeaderPolicy.Validate(); err != nil {
		return nil, err
	}
	bundle, err := input.Runtime.BundleResolver.Resolve(officialegress.BundleResolveRequest{
		SinkID: input.PrimarySinkID, Mode: input.Runtime.CodexReleaseMode,
		Execution: input.ExecutionPolicy, Deployment: input.DeploymentPolicy,
		Behavior: input.BehaviorPolicy,
	})
	if err != nil {
		return nil, fmt.Errorf("解析 OpenAI Forward ReleaseBundle：%w", err)
	}
	invocation, err := input.Runtime.CodexExecutor.BeginInvocation(ctx, bundle, input.InvocationID)
	if err != nil {
		return nil, fmt.Errorf("创建 OpenAI Forward invocation：%w", err)
	}
	overrides := make(http.Header)
	for name, value := range input.Account.GetHeaderOverrides() {
		overrides.Set(name, value)
	}
	concurrencyLimit := input.Account.EffectiveLoadFactor()
	if concurrencyLimit < 1 {
		concurrencyLimit = 1
	}
	authMode := strings.TrimSpace(input.Account.GetCredential(openAIAuthModeCredentialKey))
	if authMode == "" {
		authMode = strings.TrimSpace(input.Account.GetCredential(openAIAuthModeLegacyCredentialKey))
	}
	return &OpenAIForwardInvocationPlan{
		runtime: input.Runtime, bundle: bundle, invocation: invocation,
		accountIdentity: OpenAIForwardAccountIdentity{
			AccountID: input.Account.ID, Platform: input.Account.Platform,
			Type: input.Account.Type, AuthMode: authMode,
		},
		identityAccount: projectOfficialCodexIdentityAccount(input.Account),
		identityMode:    input.IdentityMode, resolvedHeaderOverride: overrides,
		headerPolicy: input.HeaderPolicy, executionPolicy: input.ExecutionPolicy,
		behaviorPolicy:   officialegress.CloneBehaviorPolicyForService(input.BehaviorPolicy),
		deploymentPolicy: cloneOpenAIForwardDeploymentPolicy(input.DeploymentPolicy),
		proxyURL:         strings.TrimSpace(input.ProxyURL), concurrencyLimit: concurrencyLimit,
		currentSink: input.PrimarySinkID,
	}, nil
}

func cloneOpenAIForwardDeploymentPolicy(
	input officialegress.DeploymentSupportPolicy,
) officialegress.DeploymentSupportPolicy {
	output := input
	output.SupportedBackends = append([]officialegress.BackendKind(nil), input.SupportedBackends...)
	return output
}

// validatedTurnStateScopeFacts 只从冻结 Bundle 与其验证通过的 target 生成状态域事实。
// 入站 Host 和可变账号 base URL 均不能直接进入 turn-state key。
func (p *OpenAIForwardInvocationPlan) validatedTurnStateScopeFacts(
	method string,
	target *url.URL,
	protocol officialegress.WireProtocol,
) (string, string, error) {
	if p == nil {
		return "", "", errors.New("OpenAI Forward invocation plan 为空")
	}
	authority, err := p.bundle.ValidatedRequestTargetAuthority(method, target, protocol)
	if err != nil {
		return "", "", err
	}
	return p.bundle.ReleaseDigest(), authority, nil
}

// OpenAIForwardAttempt 是 invocation 下的不可变 endpoint attempt。认证材料只在
// 本层存在；Plan 冻结的账号身份和 Header Override 不包含原始 access token。
type OpenAIForwardAttempt struct {
	owner          *OpenAIForwardInvocationPlan
	ordinal        uint32
	reason         officialegress.AttemptReason
	sinkID         officialegress.SinkID
	purpose        officialegress.Purpose
	endpointID     string
	protocol       officialegress.WireProtocol
	method         string
	target         *url.URL
	headers        http.Header
	authentication http.Header
	body           officialegress.RequestBody
	// WS 握手本身没有语义 Body；路由提示只能从已解析的首个
	// response.create 帧单独携带，不能回读同名普通 Header。
	routingHint   officialegress.CodexRoutingHintFacts
	dynamicInputs officialegress.EndpointDynamicInputs
}

type openAIForwardAttemptInput struct {
	Reason         officialegress.AttemptReason
	SinkID         officialegress.SinkID
	EndpointID     string
	Protocol       officialegress.WireProtocol
	Method         string
	URL            *url.URL
	Headers        http.Header
	Authentication http.Header
	Body           officialegress.RequestBody
	RoutingHint    officialegress.CodexRoutingHintFacts
	DynamicInputs  officialegress.EndpointDynamicInputs
}

func (p *OpenAIForwardInvocationPlan) NewAttempt(
	input openAIForwardAttemptInput,
) (OpenAIForwardAttempt, error) {
	if p == nil || p.invocation == nil {
		return OpenAIForwardAttempt{}, errors.New("OpenAI Forward invocation 未初始化")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if input.SinkID != p.currentSink {
		return OpenAIForwardAttempt{}, officialegress.ErrFallbackTransitionRequired
	}
	return p.newAttemptLocked(input)
}

// TransitionFallbackAttempt 在同一个 Bundle 和 InvocationID 内原子切换 endpoint。
func (p *OpenAIForwardInvocationPlan) TransitionFallbackAttempt(
	target officialegress.FallbackNode,
	input openAIForwardAttemptInput,
) (OpenAIForwardAttempt, error) {
	if p == nil || p.invocation == nil {
		return OpenAIForwardAttempt{}, errors.New("OpenAI Forward invocation 未初始化")
	}
	if input.SinkID != target.SinkID || input.EndpointID != target.EndpointID ||
		input.Protocol != target.Protocol || input.Reason != officialegress.AttemptReasonFallback {
		return OpenAIForwardAttempt{}, errors.New("fallback attempt 与目标节点不一致")
	}
	binding, ok := p.runtime.ProcessSinks.Resolve(target.SinkID)
	if !ok || binding.Purpose() != target.Purpose {
		return OpenAIForwardAttempt{}, errors.New("fallback target 缺少权威 SinkBinding")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := validateOpenAIForwardAttemptInput(input); err != nil {
		return OpenAIForwardAttempt{}, err
	}
	if err := p.invocation.TransitionFallback(target); err != nil {
		return OpenAIForwardAttempt{}, err
	}
	p.currentSink = target.SinkID
	return p.newAttemptLocked(input)
}

func (p *OpenAIForwardInvocationPlan) newAttemptLocked(
	input openAIForwardAttemptInput,
) (OpenAIForwardAttempt, error) {
	if err := validateOpenAIForwardAttemptInput(input); err != nil {
		return OpenAIForwardAttempt{}, err
	}
	if p.pendingOrdinal != 0 {
		return OpenAIForwardAttempt{}, errors.New("上一 OpenAI Forward attempt 尚未执行")
	}
	next := p.nextOrdinal + 1
	if next == 1 && input.Reason != officialegress.AttemptReasonInitial {
		return OpenAIForwardAttempt{}, errors.New("首次 OpenAI Forward attempt 必须是 initial")
	}
	if next > 1 && (input.Reason == officialegress.AttemptReasonInitial ||
		input.Reason == officialegress.AttemptReasonFallback && input.SinkID == p.bundle.PrimarySinkID()) {
		return OpenAIForwardAttempt{}, errors.New("OpenAI Forward attempt reason 与生命周期不一致")
	}
	binding, ok := p.runtime.ProcessSinks.Resolve(input.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.Persona() != officialegress.PersonaCodexCLI {
		return OpenAIForwardAttempt{}, errors.New("OpenAI Forward attempt SinkBinding 非法")
	}
	p.nextOrdinal = next
	p.pendingOrdinal = next
	clonedTarget := *input.URL
	return OpenAIForwardAttempt{
		owner: p, ordinal: next, reason: input.Reason,
		sinkID: input.SinkID, purpose: binding.Purpose(), endpointID: input.EndpointID,
		protocol: input.Protocol, method: strings.ToUpper(strings.TrimSpace(input.Method)),
		target: &clonedTarget, headers: input.Headers.Clone(),
		authentication: input.Authentication.Clone(), body: input.Body,
		routingHint:   input.RoutingHint,
		dynamicInputs: input.DynamicInputs,
	}, nil
}

func validateOpenAIForwardAttemptInput(input openAIForwardAttemptInput) error {
	if !input.Reason.Valid() || strings.TrimSpace(string(input.SinkID)) == "" ||
		strings.TrimSpace(input.EndpointID) == "" || !input.Protocol.Valid() ||
		strings.TrimSpace(input.Method) == "" || input.URL == nil ||
		strings.TrimSpace(input.URL.Hostname()) == "" {
		return errors.New("OpenAI Forward attempt 字段不完整")
	}
	if !input.RoutingHint.IsZero() {
		if input.Protocol != officialegress.WireProtocolWebSocket ||
			input.EndpointID != officialCodexEndpointResponsesWS {
			return errors.New("Codex routing hint 只能由 Responses WebSocket attempt 携带")
		}
		if err := input.RoutingHint.Validate(); err != nil {
			return fmt.Errorf("Codex routing hint 非法：%w", err)
		}
	}
	return nil
}

func (p *OpenAIForwardInvocationPlan) ExecuteAttempt(
	ctx context.Context,
	attempt OpenAIForwardAttempt,
) (officialegress.TransportResult, error) {
	if p == nil || p.invocation == nil || attempt.owner != p {
		return officialegress.TransportResult{}, errors.New("OpenAI Forward attempt 不属于当前 invocation")
	}
	p.mu.Lock()
	if p.pendingOrdinal != attempt.ordinal {
		p.mu.Unlock()
		return officialegress.TransportResult{}, errors.New("OpenAI Forward attempt ordinal 已消费或乱序")
	}
	p.pendingOrdinal = 0
	p.mu.Unlock()

	headers := attempt.headers.Clone()
	for name, values := range attempt.authentication {
		headers.Del(name)
		for _, value := range values {
			headers.Add(name, value)
		}
	}
	bodyBytes, replayable := attempt.body.ReplayableBytes()
	if !replayable {
		return officialegress.TransportResult{}, errors.New("Responses Forward 只接受 replayable 语义 Body")
	}
	semanticRequest, err := http.NewRequestWithContext(
		ctx, attempt.method, attempt.target.String(), bytes.NewReader(bodyBytes),
	)
	if err != nil {
		return officialegress.TransportResult{}, err
	}
	semanticRequest.Header = headers
	semantic, err := prepareOfficialCodexSemanticAttempt(
		semanticRequest, bodyBytes, attempt.endpointID, p.invocation.InvocationID(), p.identityAccount,
	)
	if err != nil {
		return officialegress.TransportResult{}, err
	}
	routingHint := semantic.RoutingHint
	if !attempt.routingHint.IsZero() {
		if !routingHint.IsZero() && routingHint.Digest() != attempt.routingHint.Digest() {
			return officialegress.TransportResult{}, errors.New("OpenAI Forward attempt 的 Body 与 WebSocket routing hint 冲突")
		}
		routingHint = attempt.routingHint
	}
	transportContext := context.WithValue(
		ctx,
		officialCodexHTTPTransportContextKey{},
		officialCodexHTTPTransportInput{
			proxyURL: p.proxyURL, accountID: p.accountIdentity.AccountID,
			concurrencyLimit: p.concurrencyLimit,
		},
	)
	transportContext = withOfficialCodexReqProfileTransport(transportContext, p.proxyURL)
	return p.invocation.ExecuteAttempt(transportContext, officialegress.ExecutorRequest{
		Bundle: p.bundle,
		Plan: officialegress.CodexEgressPlan{
			SinkID: attempt.sinkID, Purpose: attempt.purpose, EndpointID: attempt.endpointID,
			Mode: p.bundle.Mode(), Protocol: attempt.protocol,
			Method: attempt.method, URL: attempt.target, Headers: semantic.Headers,
			ResolvedHeaderOverrides: p.resolvedHeaderOverride,
			IdentityMode:            p.identityMode, IdentityFacts: semantic.IdentityFacts,
			Authentication: semantic.Authentication, HeaderPolicy: p.headerPolicy,
			BodyPolicy: officialegress.BodyPolicy{
				ID: p.headerPolicy.ID + ".body", Source: p.headerPolicy.Source,
				Conditions: semantic.BodyConditions,
			},
			RoutingHint:    routingHint,
			BehaviorPolicy: p.behaviorPolicy,
			Body:           semantic.Body,
			InvocationID:   p.invocation.InvocationID(), DeclaredPersona: officialegress.PersonaCodexCLI,
		},
		DynamicInputs: attempt.dynamicInputs, AttemptReason: attempt.reason,
		ExpectedAttemptOrdinal: attempt.ordinal,
		ExecutionScopeKey:      fmt.Sprintf("account:%d", p.accountIdentity.AccountID),
	})
}

// ExecuteHTTPRequest 将一个已经完成业务语义构造、但尚未签名的请求提交给
// 当前 sink。Authorization 会从基础 Header 中分离，作为 attempt-local 材料。
func (p *OpenAIForwardInvocationPlan) ExecuteHTTPRequest(
	ctx context.Context,
	request *http.Request,
	endpointID string,
) (*http.Response, error) {
	return p.executeHTTPRequest(ctx, request, endpointID, nil)
}

// TransitionHTTPFallback 原子完成 WS→HTTP sink 切换并执行首个 fallback attempt。
// 后续 HTTP retry 必须调用 ExecuteHTTPRequest，不能重复 transition。
func (p *OpenAIForwardInvocationPlan) TransitionHTTPFallback(
	ctx context.Context,
	request *http.Request,
	target officialegress.FallbackNode,
) (*http.Response, error) {
	return p.executeHTTPRequest(ctx, request, target.EndpointID, &target)
}

func (p *OpenAIForwardInvocationPlan) executeHTTPRequest(
	ctx context.Context,
	request *http.Request,
	endpointID string,
	fallback *officialegress.FallbackNode,
) (*http.Response, error) {
	if p == nil || request == nil || request.URL == nil {
		return nil, errors.New("OpenAI Forward HTTP attempt 输入不完整")
	}
	body, err := readReplayableHTTPRequestBody(request)
	if err != nil {
		return nil, fmt.Errorf("读取 OpenAI Forward HTTP 请求体：%w", err)
	}
	headers, authentication := splitOpenAIForwardAttemptHeaders(request.Header)

	p.mu.Lock()
	nextOrdinal := p.nextOrdinal + 1
	currentSink := p.currentSink
	p.mu.Unlock()
	reason := officialegress.AttemptReasonRetry
	if nextOrdinal == 1 {
		reason = officialegress.AttemptReasonInitial
	}
	input := openAIForwardAttemptInput{
		Reason: reason, SinkID: currentSink, EndpointID: endpointID,
		Protocol: officialegress.WireProtocolHTTP,
		Method:   request.Method, URL: request.URL,
		Headers: headers, Authentication: authentication,
		Body: officialegress.NewReplayableRequestBody(body),
	}
	var attempt OpenAIForwardAttempt
	if fallback != nil {
		input.Reason = officialegress.AttemptReasonFallback
		input.SinkID = fallback.SinkID
		input.EndpointID = fallback.EndpointID
		input.Protocol = fallback.Protocol
		attempt, err = p.TransitionFallbackAttempt(*fallback, input)
	} else {
		attempt, err = p.NewAttempt(input)
	}
	if err != nil {
		return nil, err
	}
	result, err := p.ExecuteAttempt(ctx, attempt)
	if err != nil {
		return nil, err
	}
	response := result.HTTPResponse()
	if response == nil {
		return nil, errors.New("OpenAI Forward HTTP adapter 返回空响应")
	}
	return response, nil
}

func splitOpenAIForwardAttemptHeaders(headers http.Header) (http.Header, http.Header) {
	base := headers.Clone()
	authentication := make(http.Header)
	if values := base.Values("Authorization"); len(values) > 0 {
		for _, value := range values {
			authentication.Add("Authorization", value)
		}
		base.Del("Authorization")
	}
	return base, authentication
}

// AcquireWebSocketPool 把“取得一条可用连接”作为逻辑 attempt。即使连接池命中
// 复用连接，也必须先由 Executor 签发并消费当前 token。
func (p *OpenAIForwardInvocationPlan) AcquireWebSocketPool(
	ctx context.Context,
	pool *openAIWSConnPool,
	request openAIWSAcquireRequest,
	endpointID string,
) (openAIWSLeaseSession, error) {
	if p == nil || pool == nil || request.Account == nil ||
		request.Account.ID != p.accountIdentity.AccountID {
		return nil, errors.New("OpenAI Forward WebSocket Acquire 输入不完整")
	}
	headers := cloneHeader(request.Headers)
	var err error
	if request.HeadersFactory != nil {
		headers, err = request.HeadersFactory(ctx, headers)
		if err != nil {
			return nil, err
		}
	}
	target, err := url.Parse(strings.TrimSpace(request.WSURL))
	if err != nil || target.Hostname() == "" {
		return nil, errors.New("OpenAI Forward WebSocket target 非法")
	}
	baseHeaders, authentication := splitOpenAIForwardAttemptHeaders(headers)
	p.mu.Lock()
	reason := officialegress.AttemptReasonReconnect
	if p.nextOrdinal == 0 {
		reason = officialegress.AttemptReasonInitial
	}
	currentSink := p.currentSink
	p.mu.Unlock()
	attempt, err := p.NewAttempt(openAIForwardAttemptInput{
		Reason: reason, SinkID: currentSink, EndpointID: endpointID,
		Protocol: officialegress.WireProtocolWebSocket,
		Method:   http.MethodGet, URL: target,
		Headers: baseHeaders, Authentication: authentication,
		Body:        officialegress.NewReplayableRequestBody(nil),
		RoutingHint: request.RoutingHint,
	})
	if err != nil {
		return nil, err
	}
	request.Headers = headers.Clone()
	request.HeadersFactory = nil
	transportContext := withOfficialCodexWebSocketAcquire(ctx, officialCodexWebSocketAcquireInput{
		pool: pool, poolRequest: request, proxyURL: request.ProxyURL, guard: p.runtime.Guard,
	})
	result, err := p.ExecuteAttempt(transportContext, attempt)
	if err != nil {
		return nil, err
	}
	connection := result.WebSocket()
	return &executorWebSocketLeaseSession{session: connection}, nil
}

func (p *OpenAIForwardInvocationPlan) FallbackNode(
	sinkID officialegress.SinkID,
) (officialegress.FallbackNode, bool) {
	if p == nil {
		return officialegress.FallbackNode{}, false
	}
	for _, node := range p.bundle.FallbackNodes() {
		if node.SinkID == sinkID {
			return node, true
		}
	}
	return officialegress.FallbackNode{}, false
}

func officialCodexResponseForwardPlanForHolder(
	ctx context.Context,
	holder *officialCodexBundleHolder,
	input officialCodexResponseForwardPlanInput,
) (*OpenAIForwardInvocationPlan, error) {
	if input.Account == nil {
		return nil, errors.New("Responses Forward holder 缺少账号")
	}
	if holder == nil {
		return newOfficialCodexResponseForwardPlan(ctx, input)
	}
	holder.mu.Lock()
	defer holder.mu.Unlock()
	if holder.forwardPlans == nil {
		holder.forwardPlans = make(map[officialCodexForwardPlanKey]*OpenAIForwardInvocationPlan)
	}
	key := officialCodexForwardPlanKey{sinkID: input.PrimarySinkID, accountID: input.Account.ID}
	if existing := holder.forwardPlans[key]; existing != nil {
		return existing, nil
	}
	plan, err := newOfficialCodexResponseForwardPlan(ctx, input)
	if err != nil {
		return nil, err
	}
	holder.forwardPlans[key] = plan
	return plan, nil
}

func officialCodexResponseForwardPlanFromHolder(
	holder *officialCodexBundleHolder,
	primarySinkID officialegress.SinkID,
	accountID int64,
) *OpenAIForwardInvocationPlan {
	if holder == nil {
		return nil
	}
	holder.mu.Lock()
	defer holder.mu.Unlock()
	return holder.forwardPlans[officialCodexForwardPlanKey{
		sinkID: primarySinkID, accountID: accountID,
	}]
}

func (s *OpenAIGatewayService) officialCodexResponseForwardPlan(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	primarySinkID officialegress.SinkID,
	policyID string,
	policySource string,
	attemptBudget int,
	fallbackSinkIDs ...officialegress.SinkID,
) (*OpenAIForwardInvocationPlan, error) {
	if s == nil || account == nil {
		return nil, errors.New("Responses Forward service 或账号为空")
	}
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return nil, err
	}
	invocationID, err := officialEgressInvocationIDForRequest(c)
	if err != nil {
		return nil, err
	}
	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	return officialCodexResponseForwardPlanForHolder(
		ctx,
		officialCodexBundleHolderForGin(c),
		officialCodexResponseForwardPlanInput{
			Runtime: runtimeState, Account: account, PrimarySinkID: primarySinkID,
			InvocationID: invocationID, ProxyURL: proxyURL,
			PolicyID: policyID, PolicySource: policySource,
			FallbackSinkIDs: fallbackSinkIDs, AttemptBudget: attemptBudget,
		},
	)
}

func (p *OpenAIForwardInvocationPlan) CurrentSinkID() officialegress.SinkID {
	if p == nil {
		return ""
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.currentSink
}

func (p *OpenAIForwardInvocationPlan) Bundle() officialegress.ReleaseBundle {
	if p == nil {
		return officialegress.ReleaseBundle{}
	}
	return p.bundle
}

func (p *OpenAIForwardInvocationPlan) AccountIdentity() OpenAIForwardAccountIdentity {
	if p == nil {
		return OpenAIForwardAccountIdentity{}
	}
	return p.accountIdentity
}

func (p *OpenAIForwardInvocationPlan) ResolvedHeaderOverrides() http.Header {
	if p == nil {
		return nil
	}
	return p.resolvedHeaderOverride.Clone()
}

func (a OpenAIForwardAttempt) Ordinal() uint32                      { return a.ordinal }
func (a OpenAIForwardAttempt) Reason() officialegress.AttemptReason { return a.reason }
func (a OpenAIForwardAttempt) SinkID() officialegress.SinkID        { return a.sinkID }
