package service

import (
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

const officialCodexBundleHolderGinKey = "official_codex_changeset3_bundle_holder"

func officialCodexBundleHolderForGin(c *gin.Context) *officialCodexBundleHolder {
	if c == nil {
		return &officialCodexBundleHolder{}
	}
	if value, ok := c.Get(officialCodexBundleHolderGinKey); ok {
		if holder, valid := value.(*officialCodexBundleHolder); valid && holder != nil {
			return holder
		}
	}
	holder := &officialCodexBundleHolder{}
	c.Set(officialCodexBundleHolderGinKey, holder)
	return holder
}

// officialCodexWebSocketInvocation 冻结一条上层 WS 调用的 Bundle 与 attempt 预算。
// 每次 Acquire 都重新取得认证头并签发当前 attempt token，连接池不能缓存该能力。
type officialCodexWebSocketInvocation struct {
	runtime         *OfficialEgressTransitionRuntime
	bundle          officialegress.ReleaseBundle
	invocation      *officialegress.ExecutorInvocation
	accountID       int64
	identityAccount officialCodexIdentityAccountProjection
	identityFacts   officialCodexInvocationIdentityInput
	sinkID          officialegress.SinkID
	proxyURL        string
	behavior        officialegress.BehaviorPolicy
	overrides       http.Header

	mu          sync.Mutex
	nextOrdinal uint32
}

type officialCodexWebSocketInvocationInput struct {
	Runtime         *OfficialEgressTransitionRuntime
	Account         *Account
	SinkID          officialegress.SinkID
	InvocationID    string
	ProxyURL        string
	PolicyID        string
	PolicySource    string
	FallbackSinkIDs []officialegress.SinkID
	AttemptBudget   int
	IdentityFacts   officialCodexInvocationIdentityInput
}

func newOfficialCodexWebSocketInvocation(
	ctx context.Context,
	input officialCodexWebSocketInvocationInput,
) (*officialCodexWebSocketInvocation, error) {
	if input.Runtime == nil || input.Runtime.BundleResolver == nil ||
		input.Runtime.CodexExecutor == nil {
		return nil, errors.New("Codex WebSocket invocation 缺少正式 Executor runtime")
	}
	if input.Account == nil || input.Account.ID <= 0 || input.Account.Platform != PlatformOpenAI {
		return nil, errors.New("Codex WebSocket invocation 账号非法")
	}
	binding, ok := input.Runtime.ProcessSinks.Resolve(input.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.Persona() != officialegress.PersonaCodexCLI {
		return nil, fmt.Errorf("Codex WebSocket SinkBinding 非法：%s", input.SinkID)
	}
	if strings.TrimSpace(input.PolicyID) == "" || strings.TrimSpace(input.PolicySource) == "" {
		return nil, errors.New("Codex WebSocket invocation 缺少策略来源")
	}
	attemptBudget := input.AttemptBudget
	if attemptBudget <= 0 {
		attemptBudget = 1
	}
	concurrencyLimit := input.Account.EffectiveLoadFactor()
	if concurrencyLimit < 1 {
		concurrencyLimit = 1
	}
	execution := officialegress.ExecutionPolicy{
		ID: input.PolicyID + ".execution", Source: input.PolicySource,
		MaxAttempts: attemptBudget, Replayable: true, ConcurrencyLimit: concurrencyLimit,
	}
	proxyMode := "direct"
	if strings.TrimSpace(input.ProxyURL) != "" {
		proxyMode = "configured_proxy"
	}
	deployment := officialegress.DeploymentSupportPolicy{
		ID: input.PolicyID + ".deployment", Source: input.PolicySource,
		Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: proxyMode,
		ProxyIdentityDigest: officialEgressProxyStateKey(input.ProxyURL),
		SupportedBackends: []officialegress.BackendKind{
			officialegress.BackendHTTPUpstream, officialegress.BackendWebSocket,
		},
	}
	behavior := officialegress.BehaviorPolicy{
		ID: input.PolicyID + ".behavior", Source: input.PolicySource,
		Kind:            officialegress.BehaviorUserRequest,
		FallbackSinkIDs: append([]officialegress.SinkID(nil), input.FallbackSinkIDs...),
		AttemptBudget:   attemptBudget,
	}
	bundle, err := input.Runtime.BundleResolver.Resolve(officialegress.BundleResolveRequest{
		SinkID: input.SinkID, Mode: input.Runtime.CodexReleaseMode,
		Execution: execution, Deployment: deployment, Behavior: behavior,
	})
	if err != nil {
		return nil, fmt.Errorf("解析 Codex WebSocket ReleaseBundle：%w", err)
	}
	core, err := input.Runtime.CodexExecutor.BeginInvocation(ctx, bundle, input.InvocationID)
	if err != nil {
		return nil, err
	}
	overrides := make(http.Header)
	for name, value := range input.Account.GetHeaderOverrides() {
		overrides.Set(name, value)
	}
	return &officialCodexWebSocketInvocation{
		runtime: input.Runtime, bundle: bundle, invocation: core,
		accountID: input.Account.ID, sinkID: input.SinkID,
		identityAccount: projectOfficialCodexIdentityAccount(input.Account),
		identityFacts:   input.IdentityFacts,
		proxyURL:        strings.TrimSpace(input.ProxyURL),
		behavior:        officialegress.CloneBehaviorPolicyForService(behavior),
		overrides:       overrides,
	}, nil
}

func (i *officialCodexWebSocketInvocation) AcquirePool(
	ctx context.Context,
	pool *openAIWSConnPool,
	request openAIWSAcquireRequest,
	endpointID string,
) (openAIWSLeaseSession, error) {
	if pool == nil {
		return nil, errors.New("Codex WebSocket 连接池为空")
	}
	connection, err := i.executeAcquire(
		ctx, request, endpointID,
		officialCodexWebSocketAcquireInput{
			pool: pool, poolRequest: request, proxyURL: request.ProxyURL,
		},
	)
	if err != nil {
		return nil, err
	}
	return &executorWebSocketLeaseSession{session: connection}, nil
}

func (i *officialCodexWebSocketInvocation) DialDirect(
	ctx context.Context,
	dialer openAIWSClientDialer,
	request openAIWSAcquireRequest,
	endpointID string,
) (*executorWebSocketFrameSession, int, http.Header, error) {
	if dialer == nil {
		return nil, 0, nil, errors.New("Codex WebSocket dialer 为空")
	}
	connection, err := i.executeAcquire(
		ctx, request, endpointID,
		officialCodexWebSocketAcquireInput{dialer: dialer, proxyURL: request.ProxyURL},
	)
	if err != nil {
		var dialErr *openAIWSDialError
		if errors.As(err, &dialErr) {
			return nil, dialErr.StatusCode, cloneHeader(dialErr.ResponseHeaders), err
		}
		return nil, 0, nil, err
	}
	return &executorWebSocketFrameSession{session: connection},
		connection.HandshakeStatus(), connection.HandshakeHeaders(), nil
}

func (i *officialCodexWebSocketInvocation) executeAcquire(
	ctx context.Context,
	request openAIWSAcquireRequest,
	endpointID string,
	acquireInput officialCodexWebSocketAcquireInput,
) (*officialegress.ExecutorWebSocketSession, error) {
	if i == nil || i.invocation == nil || request.Account == nil ||
		request.Account.ID != i.accountID {
		return nil, errors.New("Codex WebSocket attempt 与 invocation 账号不一致")
	}
	target, err := url.Parse(strings.TrimSpace(request.WSURL))
	if err != nil || target.Hostname() == "" {
		return nil, errors.New("Codex WebSocket target 非法")
	}
	headers := cloneHeader(request.Headers)
	if request.HeadersFactory != nil {
		headers, err = request.HeadersFactory(ctx, headers)
		if err != nil {
			return nil, err
		}
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	i.nextOrdinal++
	ordinal := i.nextOrdinal
	reason := officialegress.AttemptReasonReconnect
	if ordinal == 1 {
		reason = officialegress.AttemptReasonInitial
	}
	binding, ok := i.runtime.ProcessSinks.Resolve(i.sinkID)
	if !ok {
		return nil, errors.New("Codex WebSocket SinkBinding 已丢失")
	}
	semanticRequest, err := http.NewRequestWithContext(
		withOfficialCodexInvocationIdentity(ctx, i.identityFacts),
		http.MethodGet, target.String(), nil,
	)
	if err != nil {
		return nil, err
	}
	semanticRequest.Header = headers.Clone()
	semantic, err := prepareOfficialCodexSemanticAttempt(
		semanticRequest, nil, endpointID, i.invocation.InvocationID(), i.identityAccount,
	)
	if err != nil {
		return nil, err
	}
	request.Headers = headers.Clone()
	request.HeadersFactory = nil
	acquireInput.guard = i.runtime.Guard
	acquireInput.poolRequest = request
	transportContext := withOfficialCodexWebSocketAcquire(ctx, acquireInput)
	dynamicInputs := officialegress.EndpointDynamicInputs{}
	if len(request.ServerResponseQuery) != 0 {
		dynamicInputs.ServerResponseQuery = make(
			map[string]string, len(request.ServerResponseQuery),
		)
		for name, value := range request.ServerResponseQuery {
			dynamicInputs.ServerResponseQuery[name] = value
		}
	}
	result, err := i.invocation.ExecuteAttempt(transportContext, officialegress.ExecutorRequest{
		Bundle: i.bundle,
		Plan: officialegress.CodexEgressPlan{
			SinkID: i.sinkID, Purpose: binding.Purpose(), EndpointID: endpointID,
			Mode: i.bundle.Mode(), Protocol: officialegress.WireProtocolWebSocket,
			Method: http.MethodGet, URL: target, Headers: semantic.Headers,
			ResolvedHeaderOverrides: i.overrides,
			IdentityMode:            officialegress.IdentityCodexOAuthStrict,
			IdentityFacts:           semantic.IdentityFacts, Authentication: semantic.Authentication,
			HeaderPolicy: officialegress.HeaderPolicy{
				ID: i.behavior.ID + ".headers", Source: i.behavior.Source,
			},
			BodyPolicy: officialegress.BodyPolicy{
				ID: i.behavior.ID + ".body", Source: i.behavior.Source,
			},
			RoutingHint:    request.RoutingHint,
			BehaviorPolicy: i.behavior, Body: semantic.Body,
			InvocationID: i.invocation.InvocationID(), DeclaredPersona: officialegress.PersonaCodexCLI,
		},
		DynamicInputs: dynamicInputs,
		AttemptReason: reason, ExpectedAttemptOrdinal: ordinal,
		ExecutionScopeKey: fmt.Sprintf("account:%d", i.accountID),
	})
	if err != nil {
		return nil, err
	}
	connection := result.WebSocket()
	if connection == nil {
		return nil, errors.New("Codex WebSocket adapter 返回空连接")
	}
	return connection, nil
}

func (i *officialCodexWebSocketInvocation) Bundle() officialegress.ReleaseBundle {
	if i == nil {
		return officialegress.ReleaseBundle{}
	}
	return i.bundle
}
