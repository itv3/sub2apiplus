package service

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"runtime"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// officialCodexHTTPInvocation 冻结一组同 Sink HTTP attempt 共用的发布、账号与策略事实。
// 每个请求体和认证头仍只属于单个 attempt，不会保存回 invocation。
type officialCodexHTTPInvocation struct {
	runtime    *OfficialEgressTransitionRuntime
	bundle     officialegress.ReleaseBundle
	invocation *officialegress.ExecutorInvocation

	accountID          int64
	identityAccount    officialCodexIdentityAccountProjection
	identityFacts      officialCodexInvocationIdentityInput
	identityMode       officialegress.IdentityMode
	headerPolicy       officialegress.HeaderPolicy
	headerOverrides    http.Header
	proxyURL           string
	concurrencyLimit   int
	behaviorPolicy     officialegress.BehaviorPolicy
	sinkID             officialegress.SinkID
	lastEndpointID     string
	nextAttemptOrdinal uint32
	singleUseBody      bool

	mu sync.Mutex
}

type officialCodexHTTPInvocationInput struct {
	Runtime           *OfficialEgressTransitionRuntime
	Bundle            *officialegress.ReleaseBundle
	Account           *Account
	SinkID            officialegress.SinkID
	InvocationID      string
	ProxyURL          string
	PolicyID          string
	PolicySource      string
	BehaviorKind      officialegress.BehaviorKind
	FallbackSinkIDs   []officialegress.SinkID
	AttemptBudget     int
	SingleUseBody     bool
	IdentityMode      officialegress.IdentityMode
	IdentityFacts     officialCodexInvocationIdentityInput
	HeaderPolicy      officialegress.HeaderPolicy
	MinimumInterval   int64
	BillingSideEffect bool
}

type officialCodexHTTPAttemptInput struct {
	EndpointID    string
	Request       *http.Request
	DynamicInputs officialegress.EndpointDynamicInputs
	Reason        officialegress.AttemptReason
}

func newOfficialCodexHTTPInvocation(
	ctx context.Context,
	input officialCodexHTTPInvocationInput,
) (*officialCodexHTTPInvocation, error) {
	if input.Runtime == nil || input.Runtime.BundleResolver == nil ||
		input.Runtime.CodexExecutor == nil {
		return nil, errors.New("Codex HTTP invocation 缺少正式 Executor runtime")
	}
	if input.Account == nil || input.Account.ID <= 0 || input.Account.Platform != PlatformOpenAI {
		return nil, errors.New("Codex HTTP invocation 账号非法")
	}
	binding, ok := input.Runtime.ProcessSinks.Resolve(input.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.Persona() != officialegress.PersonaCodexCLI {
		return nil, fmt.Errorf("Codex HTTP invocation SinkBinding 非法：%s", input.SinkID)
	}
	if strings.TrimSpace(input.PolicyID) == "" || strings.TrimSpace(input.PolicySource) == "" {
		return nil, errors.New("Codex HTTP invocation 缺少策略来源")
	}
	attemptBudget := input.AttemptBudget
	if attemptBudget <= 0 {
		attemptBudget = 1
	}
	if input.SingleUseBody && attemptBudget != 1 {
		return nil, errors.New("single-use Codex HTTP invocation 的 attempt budget 必须为 1")
	}
	identityMode := input.IdentityMode
	if !identityMode.Valid() {
		identityMode = officialegress.IdentityCodexOAuthStrict
	}
	headerPolicy := input.HeaderPolicy
	if strings.TrimSpace(headerPolicy.ID) == "" {
		headerPolicy = officialegress.HeaderPolicy{
			ID: input.PolicyID + ".headers", Source: input.PolicySource,
		}
	}
	if err := headerPolicy.Validate(); err != nil {
		return nil, err
	}
	behaviorKind := input.BehaviorKind
	if behaviorKind == "" {
		behaviorKind = officialegress.BehaviorUserRequest
	}
	behavior := officialegress.BehaviorPolicy{
		ID: input.PolicyID + ".behavior", Source: input.PolicySource,
		Kind:            behaviorKind,
		FallbackSinkIDs: append([]officialegress.SinkID(nil), input.FallbackSinkIDs...),
		AttemptBudget:   attemptBudget,
	}
	execution := officialegress.ExecutionPolicy{
		ID: input.PolicyID + ".execution", Source: input.PolicySource,
		MaxAttempts: attemptBudget, Replayable: !input.SingleUseBody,
		ConcurrencyLimit:     input.Account.EffectiveLoadFactor(),
		HasBillingSideEffect: input.BillingSideEffect,
	}
	if execution.ConcurrencyLimit < 1 {
		execution.ConcurrencyLimit = 1
	}
	proxyMode := "direct"
	if strings.TrimSpace(input.ProxyURL) != "" {
		proxyMode = "configured_proxy"
	}
	deployment := officialegress.DeploymentSupportPolicy{
		ID: input.PolicyID + ".deployment", Source: input.PolicySource,
		Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: proxyMode,
		ProxyIdentityDigest: officialEgressProxyStateKey(input.ProxyURL),
		SupportedBackends:   []officialegress.BackendKind{officialegress.BackendHTTPUpstream},
	}
	var bundle officialegress.ReleaseBundle
	if input.Bundle != nil {
		bundle = *input.Bundle
		if bundle.PrimarySinkID() != input.SinkID {
			return nil, errors.New("复用的 Codex HTTP Bundle 与 primary sink 不一致")
		}
		if bundle.Execution() != execution || bundle.BundleDigest() == "" {
			return nil, errors.New("复用的 Codex HTTP Bundle 与冻结执行策略不一致")
		}
	} else {
		var err error
		bundle, err = input.Runtime.BundleResolver.Resolve(officialegress.BundleResolveRequest{
			SinkID: input.SinkID, Mode: input.Runtime.CodexReleaseMode,
			Execution: execution, Deployment: deployment, Behavior: behavior,
		})
		if err != nil {
			return nil, fmt.Errorf("解析 Codex HTTP ReleaseBundle：%w", err)
		}
	}
	coreInvocation, err := input.Runtime.CodexExecutor.BeginInvocation(ctx, bundle, input.InvocationID)
	if err != nil {
		return nil, fmt.Errorf("创建 Codex HTTP ExecutorInvocation：%w", err)
	}
	overrides := make(http.Header)
	for name, value := range input.Account.GetHeaderOverrides() {
		overrides.Set(name, value)
	}
	return &officialCodexHTTPInvocation{
		runtime: input.Runtime, bundle: bundle, invocation: coreInvocation,
		accountID: input.Account.ID, identityMode: identityMode,
		identityAccount: projectOfficialCodexIdentityAccount(input.Account),
		identityFacts:   input.IdentityFacts,
		headerPolicy:    headerPolicy, headerOverrides: overrides,
		proxyURL:         strings.TrimSpace(input.ProxyURL),
		concurrencyLimit: execution.ConcurrencyLimit,
		behaviorPolicy:   officialegress.CloneBehaviorPolicyForService(behavior),
		sinkID:           input.SinkID, singleUseBody: input.SingleUseBody,
	}, nil
}

// Execute 把单个 HTTP attempt 的可变材料转换为语义 Plan，再交给同一 ExecutorInvocation。
func (i *officialCodexHTTPInvocation) Execute(
	ctx context.Context,
	input officialCodexHTTPAttemptInput,
) (*http.Response, error) {
	if i == nil || i.invocation == nil || input.Request == nil || input.Request.URL == nil {
		return nil, errors.New("Codex HTTP attempt 输入不完整")
	}
	i.mu.Lock()
	defer i.mu.Unlock()

	i.nextAttemptOrdinal++
	ordinal := i.nextAttemptOrdinal
	reason := input.Reason
	if !reason.Valid() {
		switch {
		case ordinal == 1:
			reason = officialegress.AttemptReasonInitial
		case strings.TrimSpace(input.EndpointID) == i.lastEndpointID:
			reason = officialegress.AttemptReasonRetry
		default:
			reason = officialegress.AttemptReasonIndependent
		}
	}
	i.lastEndpointID = strings.TrimSpace(input.EndpointID)

	request := input.Request.Clone(input.Request.Context())
	request = request.WithContext(withOfficialCodexInvocationIdentity(
		request.Context(), i.identityFacts,
	))
	request.Header = input.Request.Header.Clone()
	var body officialegress.RequestBody
	semantic := officialCodexSemanticAttempt{}
	var err error
	if i.singleUseBody {
		semantic, err = prepareOfficialCodexSemanticAttempt(
			request, nil, input.EndpointID, i.invocation.InvocationID(), i.identityAccount,
		)
		if err != nil {
			return nil, err
		}
		body, err = officialegress.NewSingleUseRequestBody(input.Request.Body, input.Request.ContentLength)
		if err != nil {
			return nil, err
		}
		input.Request.Body = http.NoBody
	} else {
		bodyBytes, readErr := readReplayableHTTPRequestBody(input.Request)
		if readErr != nil {
			return nil, readErr
		}
		semantic, err = prepareOfficialCodexSemanticAttempt(
			request, bodyBytes, input.EndpointID, i.invocation.InvocationID(), i.identityAccount,
		)
		if err != nil {
			return nil, err
		}
		body = semantic.Body
	}
	binding, ok := i.runtime.ProcessSinks.Resolve(i.sinkID)
	if !ok {
		return nil, errors.New("Codex HTTP attempt 的 SinkBinding 已丢失")
	}
	transportContext := context.WithValue(
		ctx,
		officialCodexHTTPTransportContextKey{},
		officialCodexHTTPTransportInput{
			proxyURL: i.proxyURL, accountID: i.accountID,
			concurrencyLimit: i.concurrencyLimit,
		},
	)
	result, err := i.invocation.ExecuteAttempt(transportContext, officialegress.ExecutorRequest{
		Bundle: i.bundle,
		Plan: officialegress.CodexEgressPlan{
			SinkID: i.sinkID, Purpose: binding.Purpose(), EndpointID: input.EndpointID,
			Mode: i.bundle.Mode(), Protocol: officialegress.WireProtocolHTTP,
			Method: request.Method, URL: request.URL, Headers: semantic.Headers,
			ResolvedHeaderOverrides: i.headerOverrides,
			IdentityMode:            i.identityMode, IdentityFacts: semantic.IdentityFacts,
			Authentication: semantic.Authentication, HeaderPolicy: i.headerPolicy,
			BodyPolicy: officialegress.BodyPolicy{
				ID: i.headerPolicy.ID + ".body", Source: i.headerPolicy.Source,
				Conditions: semantic.BodyConditions,
			},
			RoutingHint:    semantic.RoutingHint,
			BehaviorPolicy: i.behaviorPolicy, Body: body,
			InvocationID: i.invocation.InvocationID(), DeclaredPersona: officialegress.PersonaCodexCLI,
		},
		DynamicInputs: input.DynamicInputs, AttemptReason: reason,
		ExpectedAttemptOrdinal: ordinal,
		ExecutionScopeKey:      fmt.Sprintf("account:%d", i.accountID),
	})
	if err != nil {
		return nil, err
	}
	response := result.HTTPResponse()
	if response == nil {
		return nil, errors.New("Codex HTTP adapter 返回空响应")
	}
	return response, nil
}

func (i *officialCodexHTTPInvocation) Bundle() officialegress.ReleaseBundle {
	if i == nil {
		return officialegress.ReleaseBundle{}
	}
	return i.bundle
}
