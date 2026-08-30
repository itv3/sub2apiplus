package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	//nolint:depguard // 该文件是进程级运行时 wiring，Redis 仅用于构造状态存储适配器。
	"github.com/redis/go-redis/v9"
)

const officialCodexExecutorID = officialegress.ExecutorID("codex.executor.changeset1b")

var officialEgressTestRuntimeFactory func(HTTPUpstream) (*OfficialEgressTransitionRuntime, error)
var processOfficialEgressRuntime atomic.Pointer[OfficialEgressTransitionRuntime]

// OfficialCodexHTTPExecution 描述变更集 1B 已迁移的单次 Codex HTTP 发送。
//
// Request 仍是 1A 过渡期的 service 层输入，但它不会直接进入 transport：本执行器会先
// 重新绑定端点画像、完成 Header/Body 定型，再把不可变 Plan 交给中立 Executor 签名。
// 业务调用方拿不到 PreparedRequest，因此 FinalizationToken 之后不存在继续改写的机会。
type OfficialCodexHTTPExecution struct {
	SinkID               officialegress.SinkID
	EndpointID           string
	Account              *Account
	ProxyURL             string
	Request              *http.Request
	Ingress              *gin.Context
	PolicyID             string
	PolicySource         string
	MinimumInterval      time.Duration
	ConcurrencyLimit     int
	HasBillingSideEffect bool
}

type officialCodexHTTPTransportInput struct {
	proxyURL         string
	accountID        int64
	concurrencyLimit int
}

type officialCodexHTTPTransportContextKey struct{}

type officialCodexHTTPUpstreamPort struct {
	httpUpstream HTTPUpstream
}

type claudeHTTPTransportInput struct {
	proxyURL         string
	accountID        int64
	concurrencyLimit int
}

type claudeHTTPTransportContextKey struct{}

// withClaudeHTTPTransport 只把物理账号资源绑定给 Claude transport port。
// Persona、Release、端点和 TLS 画像仍只能来自已定型的 PreparedRequest。
func withClaudeHTTPTransport(
	ctx context.Context,
	proxyURL string,
	accountID int64,
	concurrencyLimit int,
) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, claudeHTTPTransportContextKey{},
		claudeHTTPTransportInput{
			proxyURL: strings.TrimSpace(proxyURL), accountID: accountID,
			concurrencyLimit: concurrencyLimit,
		})
}

// withClaudeCandidateHTTPTransport 只为 FW-G 历史测试保留旧名。
func withClaudeCandidateHTTPTransport(
	ctx context.Context,
	proxyURL string,
	accountID int64,
	concurrencyLimit int,
) context.Context {
	return withClaudeHTTPTransport(ctx, proxyURL, accountID, concurrencyLimit)
}

type claudeHTTPUpstreamPort struct {
	httpUpstream HTTPUpstream
}

func (p *claudeHTTPUpstreamPort) SendHTTPUpstream(
	ctx context.Context,
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	if p == nil || p.httpUpstream == nil {
		return nil, errors.New("Claude Executor HTTPUpstream port 未配置")
	}
	input, ok := ctx.Value(claudeHTTPTransportContextKey{}).(claudeHTTPTransportInput)
	if !ok || input.accountID <= 0 {
		return nil, errors.New("Claude Executor 缺少账号传输上下文")
	}
	if input.concurrencyLimit <= 0 {
		input.concurrencyLimit = 1
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	request = request.WithContext(WithHTTPUpstreamRedirectsDisabled(request.Context()))
	tlsProfile, err := tlsFingerprintProfileFromTransportSpec(prepared.Transport())
	if err != nil {
		return nil, fmt.Errorf("解析 Claude Executor TLS 画像：%w", err)
	}
	if tlsProfile == nil {
		return nil, errors.New("Claude Executor 请求缺少已定型 TLS 画像")
	}
	return p.httpUpstream.DoWithTLS(
		request,
		input.proxyURL,
		input.accountID,
		input.concurrencyLimit,
		tlsProfile,
	)
}

func (p *officialCodexHTTPUpstreamPort) SendHTTPUpstream(
	ctx context.Context,
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	if p == nil || p.httpUpstream == nil {
		return nil, errors.New("Codex Executor HTTPUpstream port 未配置")
	}
	input, ok := ctx.Value(officialCodexHTTPTransportContextKey{}).(officialCodexHTTPTransportInput)
	if !ok || input.accountID <= 0 {
		return nil, errors.New("Codex Executor 缺少账号传输上下文")
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	request = request.WithContext(WithHTTPUpstreamRedirectsDisabled(
		WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI),
	))
	tlsProfile, err := tlsFingerprintProfileFromTransportSpec(prepared.Transport())
	if err != nil {
		return nil, fmt.Errorf("解析 Codex Executor TLS 画像：%w", err)
	}
	if tlsProfile == nil {
		return nil, errors.New("Codex Executor 请求缺少已定型 TLS 画像")
	}
	return p.httpUpstream.DoWithTLS(
		request,
		input.proxyURL,
		input.accountID,
		input.concurrencyLimit,
		tlsProfile,
	)
}

// ProvideOfficialEgressTransitionRuntime 在生产 wiring 中把 1A 的两个中立 port、
// HTTPUpstream adapter 与同一个进程级 Guard 组装为真正可执行的 Codex Executor。
func ProvideOfficialEgressTransitionRuntime(
	guard *officialegress.Guard,
	httpUpstream HTTPUpstream,
	cfg *config.Config,
	reqProfileResource OfficialCodexReqProfileTransportResource,
	redisClient *redis.Client,
) (*OfficialEgressTransitionRuntime, error) {
	if redisClient == nil {
		return nil, errors.New("Claude production Persona 缺少 Redis 状态存储")
	}
	runtimeState, err := BuildOfficialEgressTransitionRuntime(
		guard, httpUpstream, cfg, reqProfileResource,
		newClaudeRedisStateStore(redisClient),
	)
	if err != nil {
		return nil, err
	}
	processOfficialEgressRuntime.Store(runtimeState)
	return runtimeState, nil
}

// BuildOfficialEgressTransitionRuntime 组装一个不写入进程级状态的完整 Runtime。
// 生产 provider 在构造后统一发布进程级实例；需要隔离状态的测试可显式注入返回值。
func BuildOfficialEgressTransitionRuntime(
	guard *officialegress.Guard,
	httpUpstream HTTPUpstream,
	cfg *config.Config,
	reqProfileResource OfficialCodexReqProfileTransportResource,
	claudeStateStores ...officialegress.ClaudeStateStore,
) (*OfficialEgressTransitionRuntime, error) {
	if len(claudeStateStores) > 1 {
		return nil, errors.New("Official Egress runtime 只能绑定一个 Claude 状态存储")
	}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard,
		httpUpstream,
		officialCodexExecutorID,
		officialEgressReleaseModeFromConfig(cfg),
		reqProfileResource,
	)
	if err != nil {
		return nil, err
	}
	candidateEnabled := cfg != nil && cfg.Gateway.ClaudeFWGCandidateEnabled
	productionActive := cfg != nil && strings.EqualFold(
		strings.TrimSpace(cfg.Gateway.ClaudeOfficialClientProfiles.Mode), "active",
	)
	if !candidateEnabled && !productionActive {
		return runtimeState, nil
	}
	claudePort := &claudeHTTPUpstreamPort{httpUpstream: httpUpstream}
	var claudeRuntime *officialegress.ClaudeRuntime
	if candidateEnabled {
		if len(claudeStateStores) == 1 {
			claudeRuntime, err = officialegress.NewClaudeCandidateRuntime(
				runtimeState.ProcessSinks, guard, claudePort, claudeStateStores[0],
			)
		} else {
			claudeRuntime, err = officialegress.NewClaudeCandidateRuntime(
				runtimeState.ProcessSinks, guard, claudePort,
			)
		}
	} else {
		if len(claudeStateStores) == 1 {
			claudeRuntime, err = officialegress.NewClaudeProductionRuntime(
				runtimeState.ProcessSinks, guard, claudePort, claudeStateStores[0],
			)
		} else {
			claudeRuntime, err = officialegress.NewClaudeProductionRuntime(
				runtimeState.ProcessSinks, guard, claudePort,
			)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("构造 Claude Persona runtime：%w", err)
	}
	runtimeState.Claude = claudeRuntime
	if candidateEnabled {
		runtimeState.ClaudeCandidate = claudeRuntime
	}
	return runtimeState, nil
}

func officialEgressReleaseModeFromConfig(cfg *config.Config) officialegress.ReleaseMode {
	if officialClientProfileModeFromConfig(cfg) == officialClientProfileModePrevious {
		return officialegress.ReleaseModePrevious
	}
	return officialegress.ReleaseModeActive
}

func officialCodexProcessProfileMode() (string, error) {
	runtimeState := processOfficialEgressRuntime.Load()
	if runtimeState == nil || !runtimeState.CodexReleaseMode.Valid() {
		return "", errors.New("Codex 进程级 release mode 尚未冻结")
	}
	return string(runtimeState.CodexReleaseMode), nil
}

// OfficialCodexProcessProfileMode 返回进程启动时冻结的 Codex 发布模式。
// handler 在账号选择前只能读取这份事实，不能自行默认 active。
func OfficialCodexProcessProfileMode() (string, error) {
	return officialCodexProcessProfileMode()
}

func newOfficialEgressTransitionRuntimeWithExecutor(
	guard *officialegress.Guard,
	httpUpstream HTTPUpstream,
	executorID officialegress.ExecutorID,
	releaseMode officialegress.ReleaseMode,
	reqProfileResources ...OfficialCodexReqProfileTransportResource,
) (*OfficialEgressTransitionRuntime, error) {
	if !releaseMode.Valid() {
		return nil, errors.New("Codex Executor release mode 非法")
	}
	if guard == nil {
		return nil, errors.New("Codex Executor 缺少进程级 Guard")
	}
	processSinks := guard.ProcessSinkCatalog()
	resolver, err := officialegress.NewBundleResolver(
		officialegress.DefaultReleaseCatalog(),
		processSinks,
	)
	if err != nil {
		return nil, fmt.Errorf("构造正式 BundleResolver：%w", err)
	}
	compiler := officialegress.NewCompiler()
	runtimeState := NewOfficialEgressTransitionRuntime(
		resolver, guard, releaseMode,
	)
	port := &officialCodexHTTPUpstreamPort{httpUpstream: httpUpstream}
	httpAdapter, err := officialegress.NewHTTPUpstreamTransportAdapter(port)
	if err != nil {
		return nil, err
	}
	var reqProfileResource OfficialCodexReqProfileTransportResource
	if len(reqProfileResources) > 0 {
		reqProfileResource = reqProfileResources[0]
	}
	reqProfilePort := &officialCodexReqProfilePort{resource: reqProfileResource}
	reqProfileAdapter, err := officialegress.NewReqProfileTransportAdapter(reqProfilePort)
	if err != nil {
		return nil, err
	}
	webSocketPort := &officialCodexWebSocketPort{acquirer: officialCodexWebSocketAcquireRouter{}}
	webSocketAdapter, err := officialegress.NewWebSocketTransportAdapter(webSocketPort)
	if err != nil {
		return nil, err
	}
	registry, err := officialegress.NewAdapterRegistry(
		officialegress.DefaultBackendDescriptors(),
		[]officialegress.TransportAdapter{httpAdapter, reqProfileAdapter, webSocketAdapter},
	)
	if err != nil {
		return nil, err
	}
	executor, err := officialegress.NewExecutor(
		executorID,
		compiler,
		registry,
		guard,
	)
	if err != nil {
		return nil, err
	}
	runtimeState.CodexExecutor = executor
	runtimeState.webSocketPort = webSocketPort
	return runtimeState, nil
}

// resolveOfficialEgressRuntime 禁止在未走生产 wiring 时临时创建 active runtime。
// 测试与小型工具必须显式注入正式 Catalog runtime，避免保留第二事实源。
func resolveOfficialEgressRuntime(
	configured *OfficialEgressTransitionRuntime,
	httpUpstream HTTPUpstream,
) (*OfficialEgressTransitionRuntime, error) {
	if configured != nil && configured.CodexExecutor != nil && configured.BundleResolver != nil {
		return configured, nil
	}
	if officialEgressTestRuntimeFactory != nil {
		return officialEgressTestRuntimeFactory(httpUpstream)
	}
	if processRuntime := processOfficialEgressRuntime.Load(); processRuntime != nil &&
		processRuntime.CodexExecutor != nil && processRuntime.BundleResolver != nil {
		return processRuntime, nil
	}
	return nil, errors.New("Codex Executor 未注入正式 ReleaseCatalog runtime")
}

// ExecuteCodexHTTP 是 1B 已迁移 HTTP sink 的唯一发送入口。
func (r *OfficialEgressTransitionRuntime) ExecuteCodexHTTP(
	ctx context.Context,
	input OfficialCodexHTTPExecution,
) (*http.Response, error) {
	if r == nil || r.CodexExecutor == nil {
		return nil, errors.New("Codex Executor 未接入生产运行时")
	}
	if input.Account == nil || !input.Account.IsOpenAIOAuth() {
		return nil, errors.New("Codex Executor 只接受 OpenAI OAuth 账号")
	}
	if input.Request == nil || input.Request.URL == nil {
		return nil, errors.New("Codex Executor 请求为空")
	}
	binding, ok := r.ProcessSinks.Resolve(input.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.Persona() != officialegress.PersonaCodexCLI {
		return nil, fmt.Errorf("Codex Executor SinkID 未登记或 persona 不符：%s", input.SinkID)
	}
	if strings.TrimSpace(input.PolicyID) == "" || strings.TrimSpace(input.PolicySource) == "" {
		return nil, errors.New("Codex Executor 缺少显式 BehaviorPolicy 来源")
	}
	isResponsesEndpoint := input.EndpointID == officialCodexEndpointResponsesHTTP ||
		input.EndpointID == officialCodexEndpointResponsesCompact
	if !isResponsesEndpoint {
		return nil, fmt.Errorf("变更集 1B 的 Codex Executor 仅开放 Responses/compact：%s", input.EndpointID)
	}
	if input.ConcurrencyLimit <= 0 {
		input.ConcurrencyLimit = 1
	}
	releaseMode := r.CodexReleaseMode
	if !releaseMode.Valid() {
		return nil, errors.New("Codex Executor 运行时 release mode 非法")
	}

	requestContext := input.Request.Context()
	if requestContext == nil {
		requestContext = ctx
	}
	if requestContext == nil {
		requestContext = context.Background()
	}
	if _, hasEgressContext := OfficialEgressContextFromContext(requestContext); !hasEgressContext {
		if _, bound, stateErr := officialCodexRuntimeStateFromContext(requestContext); stateErr != nil {
			return nil, stateErr
		} else if !bound {
			state, stateErr := officialCodexRuntimeStateForReleaseMode(
				string(releaseMode), input.EndpointID,
			)
			if stateErr != nil {
				return nil, stateErr
			}
			requestContext, stateErr = withOfficialCodexRuntimeState(requestContext, state)
			if stateErr != nil {
				return nil, stateErr
			}
		}
	}
	if identity, exists := officialegress.AttemptIdentityFromContext(requestContext); exists {
		if identity.SinkID != input.SinkID || identity.Purpose != binding.Purpose() ||
			identity.DeclaredPersona != binding.Persona() {
			return nil, errors.New("Codex Executor 请求继承了不一致的业务 binding")
		}
	} else {
		var err error
		requestContext, err = r.ProcessSinks.StartAttemptContext(requestContext, input.SinkID)
		if err != nil {
			return nil, err
		}
	}

	body, err := readReplayableHTTPRequestBody(input.Request)
	if err != nil {
		return nil, fmt.Errorf("读取 Codex Executor 请求体：%w", err)
	}
	request := input.Request.Clone(requestContext)
	request.Header = input.Request.Header.Clone()
	resetOfficialEgressRequestBody(request, body)
	identity, _ := officialegress.AttemptIdentityFromContext(requestContext)
	invocationID := strings.TrimSpace(identity.InvocationID)
	if invocationID == "" {
		invocationID = uuid.NewString()
	}
	semantic, err := prepareOfficialCodexSemanticAttempt(
		request, body, input.EndpointID, invocationID,
		projectOfficialCodexIdentityAccount(input.Account),
	)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex Executor 语义请求：%w", err)
	}
	execution := officialegress.ExecutionPolicy{
		ID:                   input.PolicyID,
		Source:               input.PolicySource,
		MaxAttempts:          1,
		Replayable:           true,
		MinimumInterval:      input.MinimumInterval,
		ConcurrencyLimit:     input.ConcurrencyLimit,
		HasBillingSideEffect: input.HasBillingSideEffect,
	}
	proxyMode := "direct"
	if strings.TrimSpace(input.ProxyURL) != "" {
		proxyMode = "configured_proxy"
	}
	deployment := officialegress.DeploymentSupportPolicy{
		ID:                  input.PolicyID + ".deployment",
		Source:              input.PolicySource,
		Platform:            runtime.GOOS + "/" + runtime.GOARCH,
		ProxyMode:           proxyMode,
		ProxyIdentityDigest: officialEgressProxyStateKey(input.ProxyURL),
		CustomCARoots:       false,
		SupportedBackends:   []officialegress.BackendKind{officialegress.BackendHTTPUpstream},
	}
	behavior := officialegress.BehaviorPolicy{
		ID: input.PolicyID + ".behavior", Source: input.PolicySource,
		Kind: officialegress.BehaviorUserRequest, AttemptBudget: execution.MaxAttempts,
	}
	bundle, err := r.BundleResolver.Resolve(officialegress.BundleResolveRequest{
		SinkID: input.SinkID, Mode: releaseMode,
		Execution: execution, Deployment: deployment, Behavior: behavior,
	})
	if err != nil {
		return nil, fmt.Errorf("解析 Codex ReleaseBundle：%w", err)
	}
	executorContext := context.WithValue(
		request.Context(),
		officialCodexHTTPTransportContextKey{},
		officialCodexHTTPTransportInput{
			proxyURL: input.ProxyURL, accountID: input.Account.ID, concurrencyLimit: input.ConcurrencyLimit,
		},
	)
	invocation, err := r.CodexExecutor.BeginInvocation(executorContext, bundle, invocationID)
	if err != nil {
		return nil, err
	}
	result, err := invocation.ExecuteAttempt(executorContext, officialegress.ExecutorRequest{
		Bundle: bundle,
		Plan: officialegress.CodexEgressPlan{
			SinkID: input.SinkID, Purpose: binding.Purpose(),
			EndpointID: string(input.EndpointID), Mode: releaseMode,
			Protocol: officialegress.WireProtocolHTTP,
			Method:   request.Method, URL: request.URL, Headers: semantic.Headers,
			IdentityMode:  officialegress.IdentityCodexOAuthStrict,
			IdentityFacts: semantic.IdentityFacts, Authentication: semantic.Authentication,
			HeaderPolicy: officialegress.HeaderPolicy{
				ID: input.PolicyID + ".headers", Source: input.PolicySource,
			},
			BodyPolicy: officialegress.BodyPolicy{
				ID: input.PolicyID + ".body", Source: input.PolicySource,
				Conditions: semantic.BodyConditions,
			},
			RoutingHint:     semantic.RoutingHint,
			BehaviorPolicy:  behavior,
			Body:            semantic.Body,
			InvocationID:    invocationID,
			DeclaredPersona: officialegress.PersonaCodexCLI,
		},
		AttemptReason:          officialegress.AttemptReasonInitial,
		ExpectedAttemptOrdinal: 1,
		ExecutionScopeKey:      fmt.Sprintf("account:%d", input.Account.ID),
	})
	if err != nil {
		return nil, err
	}
	response := result.HTTPResponse()
	if response == nil {
		return nil, errors.New("Codex Executor HTTP adapter 返回空响应")
	}
	return response, nil
}

func readReplayableHTTPRequestBody(request *http.Request) ([]byte, error) {
	if request == nil || request.Body == nil || request.Body == http.NoBody {
		return nil, nil
	}
	if request.GetBody != nil {
		body, err := request.GetBody()
		if err != nil {
			return nil, err
		}
		defer func() { _ = body.Close() }()
		return io.ReadAll(body)
	}
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	resetOfficialEgressRequestBody(request, body)
	return body, nil
}
