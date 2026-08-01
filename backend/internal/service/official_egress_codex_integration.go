package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const (
	officialCodexForceHTTPFallbackKey       = "official_codex_force_http_fallback"
	officialCodexResidencyAccountExtraKey   = "official_codex_enforce_residency"
	officialCodexRuntimeMetricsAccountExtra = "official_codex_runtime_metrics"
)

var officialCodex0145TrustedConditionalHeaders = map[string]string{
	"x-openai-internal-codex-residency":     officialCodexConditionResidency,
	"x-openai-subagent":                     officialCodexConditionSubagent,
	"x-openai-memgen-request":               officialCodexConditionMemoryGeneration,
	"x-codex-parent-thread-id":              officialCodexConditionParentThread,
	"x-responsesapi-include-timing-metrics": officialCodexConditionRuntimeMetrics,
}

type officialCodex0145RuntimeStateContextKey struct{}
type officialCodex0145IngressRuntimeContextKey struct{}

type officialCodex0145IngressRuntimeSnapshot struct {
	OfficialClient            bool
	UserAgent                 string
	Originator                string
	RequestCompressionEnabled bool
	Subagent                  string
	MemoryGeneration          string
	ParentThreadID            string
	TurnMetadata              string
}

// WithOfficialCodex0145IngressRuntime 在路由入口保存原始进程身份与 feature。
// 它不判定账号 requirements，也不生成出站 header；账号选定后仍须由版本画像完成
// 配对和条件关系校验。必须在 body 解压前调用，避免 Content-Encoding 被标准化删除后
// 丢失官方进程的 request compression 状态。重复调用保持首次快照，不能用已经标准化
// 的请求覆盖原始 wire 事实；realtime 也用同一入口跨调度边界冻结身份。
func WithOfficialCodex0145IngressRuntime(ctx context.Context, c *gin.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if _, captured := officialCodex0145IngressRuntimeSnapshotFromContext(ctx); captured {
		return ctx
	}
	return context.WithValue(
		ctx,
		officialCodex0145IngressRuntimeContextKey{},
		officialCodex0145IngressRuntimeSnapshotFromGin(c),
	)
}

func officialCodex0145IngressRuntimeSnapshotFromGin(c *gin.Context) officialCodex0145IngressRuntimeSnapshot {
	if c == nil || c.Request == nil {
		return officialCodex0145IngressRuntimeSnapshot{}
	}
	return officialCodex0145IngressRuntimeSnapshot{
		OfficialClient:            isInboundOpenAIOfficialClient(c),
		UserAgent:                 strings.TrimSpace(c.GetHeader("user-agent")),
		Originator:                strings.TrimSpace(c.GetHeader("originator")),
		RequestCompressionEnabled: headerContainsToken(c.Request.Header, "content-encoding", "zstd"),
		Subagent:                  strings.TrimSpace(c.GetHeader("x-openai-subagent")),
		MemoryGeneration:          strings.TrimSpace(c.GetHeader("x-openai-memgen-request")),
		ParentThreadID:            strings.TrimSpace(c.GetHeader("x-codex-parent-thread-id")),
		TurnMetadata:              strings.TrimSpace(c.GetHeader("x-codex-turn-metadata")),
	}
}

func officialCodex0145IngressRuntimeSnapshotFromContext(
	ctx context.Context,
) (officialCodex0145IngressRuntimeSnapshot, bool) {
	if ctx == nil {
		return officialCodex0145IngressRuntimeSnapshot{}, false
	}
	snapshot, ok := ctx.Value(officialCodex0145IngressRuntimeContextKey{}).(officialCodex0145IngressRuntimeSnapshot)
	return snapshot, ok
}

// withOfficialCodex0145RuntimeState 把已经完成来源校验的进程快照绑定到业务上下文。
// models、alpha-search、images 等辅助端点会在协议转换后重新构造请求，因此不能
// 再从最终 header 猜测入口；它们只消费这里冻结的值。
func withOfficialCodex0145RuntimeState(
	ctx context.Context,
	state officialCodex0145RuntimeState,
) (context.Context, error) {
	if err := validateOfficialCodex0145RuntimeState(state); err != nil {
		return nil, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(
		ctx,
		officialCodex0145RuntimeStateContextKey{},
		cloneOfficialCodex0145RuntimeState(state),
	), nil
}

func officialCodex0145RuntimeStateFromContext(
	ctx context.Context,
) (officialCodex0145RuntimeState, bool, error) {
	if ctx == nil {
		return officialCodex0145RuntimeState{}, false, nil
	}
	state, ok := ctx.Value(officialCodex0145RuntimeStateContextKey{}).(officialCodex0145RuntimeState)
	if !ok {
		return officialCodex0145RuntimeState{}, false, nil
	}
	state = cloneOfficialCodex0145RuntimeState(state)
	if err := validateOfficialCodex0145RuntimeState(state); err != nil {
		return officialCodex0145RuntimeState{}, false, err
	}
	return state, true, nil
}

// resolveBoundOrIngressOfficialCodex0145RuntimeState 统一所有 attach 路径的
// 运行态来源优先级：先消费 token 刷新前冻结的 context；只有调用方尚未绑定时，
// 才从当前入站对象解析。辅助端点可传 nil ingress，并得到受管理账号条件与默认
// exec 画像。该入口避免 HTTP、WS 和辅助链各自重新猜测进程状态。
func resolveBoundOrIngressOfficialCodex0145RuntimeState(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	endpointIDs ...codex0145EndpointID,
) (officialCodex0145RuntimeState, error) {
	state, bound, err := officialCodex0145RuntimeStateFromContext(ctx)
	if err != nil {
		return officialCodex0145RuntimeState{}, err
	}
	if bound {
		return state, nil
	}
	if snapshot, captured := officialCodex0145IngressRuntimeSnapshotFromContext(ctx); captured {
		return resolveOfficialCodex0145RuntimeStateFromSnapshot(snapshot, account, endpointIDs...)
	}
	if c != nil && c.Request != nil {
		if snapshot, captured := officialCodex0145IngressRuntimeSnapshotFromContext(c.Request.Context()); captured {
			return resolveOfficialCodex0145RuntimeStateFromSnapshot(snapshot, account, endpointIDs...)
		}
	}
	return resolveOfficialCodex0145RuntimeState(c, account, endpointIDs...)
}

func bindOfficialCodex0145RuntimeStateFromIngress(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	endpointIDs ...codex0145EndpointID,
) (context.Context, error) {
	state, err := resolveBoundOrIngressOfficialCodex0145RuntimeState(
		ctx,
		c,
		account,
		endpointIDs...,
	)
	if err != nil {
		return nil, err
	}
	return withOfficialCodex0145RuntimeState(ctx, state)
}

// BindOfficialCodex0145ResponsesWebSocketRuntime 在 handler 已选定 OAuth 账号后，
// 通过单一版本画像冻结 WS 入站进程状态。调用方必须在 token 刷新之前使用返回的
// context，并把同一 context 继续传给 WS 代理，避免刷新与最终握手出现两套身份。
func BindOfficialCodex0145ResponsesWebSocketRuntime(
	ctx context.Context,
	c *gin.Context,
	account *Account,
) (context.Context, error) {
	if account == nil || !account.IsOpenAIOAuth() {
		return nil, fmt.Errorf("Codex 0.145.0 Responses WebSocket 仅允许 OpenAI OAuth 账号")
	}
	return bindOfficialCodex0145RuntimeStateFromIngress(
		ctx,
		c,
		account,
		codex0145EndpointID(officialCodexEndpointResponsesWS),
	)
}

func bindOfficialCodex0145RuntimeStateFromCapturedIngress(
	ctx context.Context,
	account *Account,
	endpointIDs ...codex0145EndpointID,
) (context.Context, officialCodex0145RuntimeState, error) {
	snapshot := officialCodex0145IngressRuntimeSnapshot{}
	if ctx != nil {
		if captured, ok := ctx.Value(officialCodex0145IngressRuntimeContextKey{}).(officialCodex0145IngressRuntimeSnapshot); ok {
			snapshot = captured
		}
	}
	state, err := resolveOfficialCodex0145RuntimeStateFromSnapshot(snapshot, account, endpointIDs...)
	if err != nil {
		return nil, officialCodex0145RuntimeState{}, err
	}
	bound, err := withOfficialCodex0145RuntimeState(ctx, state)
	if err != nil {
		return nil, officialCodex0145RuntimeState{}, err
	}
	return bound, state, nil
}

// setOfficialCodexForceHTTPFallback 记录同一次上层调用已经完成 WS 重试预算。
// 该状态只影响传输选择，不创建新的调用身份或账号选择周期。
func setOfficialCodexForceHTTPFallback(c *gin.Context, enabled bool) {
	if c != nil {
		c.Set(officialCodexForceHTTPFallbackKey, enabled)
	}
}

func isOfficialCodexForceHTTPFallback(c *gin.Context) bool {
	if c == nil {
		return false
	}
	value, exists := c.Get(officialCodexForceHTTPFallbackKey)
	enabled, ok := value.(bool)
	return exists && ok && enabled
}

// activeOfficialCodexVersion 从 registry 的 release 指针解析当前 active Codex 版本。
// 端点级入口据此取版本而不写死常量：升级画像时只需登记新版本快照并调整 release
// 指针，无需改动本文件或 §3.5.2 的其他共享接入点。
func activeOfficialCodexVersion() (string, error) {
	resolved, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil {
		return "", err
	}
	version := strings.TrimSpace(resolved.Build.Version)
	if version == "" {
		return "", fmt.Errorf("official client registry 未提供 Codex 版本")
	}
	return version, nil
}

// attachOfficialCodex0145EndpointRequest 为 models/images/search/realtime/wham/files
// 等辅助端点绑定与主 Responses 相同的完整版本画像和调用级连接生命周期。
// invocationID 为空时创建新调用；同一次业务重试重建请求时必须显式复用返回上下文中的 ID。
func attachOfficialCodex0145EndpointRequest(
	req *http.Request,
	account *Account,
	endpointID codex0145EndpointID,
	invocationID string,
) (*http.Request, *OfficialEgressContext, error) {
	if req == nil || req.URL == nil {
		return nil, nil, fmt.Errorf("Codex 辅助端点请求为空")
	}
	if account == nil || !account.IsOpenAIOAuth() {
		return nil, nil, fmt.Errorf("Codex 辅助端点只支持 OpenAI OAuth 账号")
	}
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, nil, err
	}
	endpoint, err := resolveCodex0145Endpoint(version, endpointID)
	if err != nil {
		return nil, nil, err
	}
	if endpoint.Upgrade != "" {
		return nil, nil, fmt.Errorf("Codex 端点 %s 不是 HTTP 请求", endpoint.ID)
	}
	if strings.TrimSpace(invocationID) == "" {
		invocationID = uuid.NewString()
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	runtimeState, err := resolveBoundOrIngressOfficialCodex0145RuntimeState(
		req.Context(),
		nil,
		account,
		endpointID,
	)
	if err != nil {
		return nil, nil, err
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:         account.ID,
		TargetPlatform:    PlatformOpenAI,
		InboundEndpoint:   req.URL.Path,
		Transport:         OfficialEgressTransportHTTP,
		UpstreamHost:      req.URL.Hostname(),
		ProfileVersion:    version,
		ProfileMode:       officialClientProfileModeActive,
		AccountType:       account.Type,
		ProxyID:           proxyID,
		InvocationID:      invocationID,
		CodexEndpointID:   endpoint.ID,
		CodexRuntimeState: runtimeState,
	})
	egressContext.cookieJar = HTTPUpstreamCookieJarFromContext(req.Context())
	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		egressContext.InboundEndpoint(),
	)
	if err != nil {
		return nil, nil, err
	}
	if err := ValidateOfficialEgressFinalState(egressContext, profile); err != nil {
		return nil, nil, err
	}
	requestContext := WithHTTPUpstreamRedirectsDisabled(req.Context())
	requestContext = WithOfficialEgressContext(requestContext, egressContext)
	req = req.WithContext(requestContext)
	if err := validateOfficialEgressHTTPRequest(req, egressContext); err != nil {
		return nil, nil, err
	}
	logOfficialEgressProfileResolved(egressContext, profile)
	return req, egressContext, nil
}

// attachOfficialCodex0145EndpointWebSocketContext 为 realtime sideband 等辅助
// WebSocket 绑定端点画像。每次调用都形成独立连接上下文，且在拨号前冻结。
func attachOfficialCodex0145EndpointWebSocketContext(
	ctx context.Context,
	account *Account,
	endpointID codex0145EndpointID,
	targetURL string,
) (context.Context, *OfficialEgressContext, error) {
	if account == nil || !account.IsOpenAIOAuth() {
		return nil, nil, fmt.Errorf("Codex 辅助 WebSocket 只支持 OpenAI OAuth 账号")
	}
	target, err := url.Parse(strings.TrimSpace(targetURL))
	if err != nil {
		return nil, nil, fmt.Errorf("解析 Codex 辅助 WebSocket URL：%w", err)
	}
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, nil, err
	}
	endpoint, err := resolveCodex0145Endpoint(version, endpointID)
	if err != nil {
		return nil, nil, err
	}
	if endpoint.Upgrade != "websocket" {
		return nil, nil, fmt.Errorf("Codex 端点 %s 不是 WebSocket", endpoint.ID)
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	runtimeState, err := resolveBoundOrIngressOfficialCodex0145RuntimeState(
		ctx,
		nil,
		account,
		endpointID,
	)
	if err != nil {
		return nil, nil, err
	}
	egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
		AccountID:         account.ID,
		TargetPlatform:    PlatformOpenAI,
		InboundEndpoint:   target.Path,
		Transport:         OfficialEgressTransportWebSocket,
		UpstreamHost:      target.Hostname(),
		ProfileVersion:    version,
		ProfileMode:       officialClientProfileModeActive,
		AccountType:       account.Type,
		ProxyID:           proxyID,
		InvocationID:      uuid.NewString(),
		CodexEndpointID:   endpoint.ID,
		CodexRuntimeState: runtimeState,
	})
	profile, err := defaultOfficialEgressProfileResolver.ResolveWebSocketProfile(
		egressContext,
		account,
		egressContext.InboundEndpoint(),
	)
	if err != nil {
		return nil, nil, err
	}
	frozen, err := egressContext.Freeze()
	if err != nil {
		return nil, nil, err
	}
	if err := ValidateOfficialEgressFinalState(frozen, profile); err != nil {
		return nil, nil, err
	}
	if err := validateOfficialEgressWebSocketTarget(target, frozen); err != nil {
		return nil, nil, err
	}
	logOfficialEgressProfileResolved(frozen, profile)
	return WithOfficialEgressContext(ctx, frozen), frozen, nil
}

// resolveOfficialCodex0145RuntimeState 把入口身份与敏感条件头收敛成可信进程快照。
//
// 普通第三方入口始终模拟已初始化的 exec，且不能通过伪造 header 激活受管理或
// 内部 feature。只有已识别的官方 0.145.0 exec/TUI 入口可以把对应运行态带入
// 出站上下文；值和条件关系在冻结前一次性校验。
func resolveOfficialCodex0145RuntimeState(
	c *gin.Context,
	account *Account,
	endpointIDs ...codex0145EndpointID,
) (officialCodex0145RuntimeState, error) {
	return resolveOfficialCodex0145RuntimeStateFromSnapshot(
		officialCodex0145IngressRuntimeSnapshotFromGin(c),
		account,
		endpointIDs...,
	)
}

func resolveOfficialCodex0145RuntimeStateFromSnapshot(
	snapshot officialCodex0145IngressRuntimeSnapshot,
	account *Account,
	endpointIDs ...codex0145EndpointID,
) (officialCodex0145RuntimeState, error) {
	state := defaultOfficialCodex0145RuntimeState()
	profile, err := resolveActiveCodexVersionProfile()
	if err != nil {
		return officialCodex0145RuntimeState{}, err
	}
	// 普通第三方入口只消费不可变画像默认值；官方入口随后用解压前冻结的实值覆盖。
	state.RequestCompressionEnabled = profile.FeatureDefaults.EnableRequestCompression
	if account != nil {
		residency := strings.TrimSpace(account.GetExtraString(officialCodexResidencyAccountExtraKey))
		if residency != "" {
			if residency != "us" {
				return officialCodex0145RuntimeState{}, fmt.Errorf(
					"OpenAI OAuth 账号的 %s 只允许 us",
					officialCodexResidencyAccountExtraKey,
				)
			}
			state.ConditionalHeaders["x-openai-internal-codex-residency"] = residency
		}
		if account.getExtraBool(officialCodexRuntimeMetricsAccountExtra) {
			state.ConditionalHeaders["x-responsesapi-include-timing-metrics"] = "true"
		}
	}
	if !snapshot.OfficialClient {
		return state, validateOfficialCodex0145RuntimeState(state)
	}
	userAgent := snapshot.UserAgent
	originator := snapshot.Originator
	var selectedSurface *officialCodexSurfaceProfile
	selectedTerminalToken := ""
	selectedSuffixEnabled := false
	for _, surface := range profile.Surfaces {
		prefix := surface.Product + "/" + surface.Version + " " + surface.PlatformPrefix + " "
		if !strings.HasPrefix(userAgent, prefix) {
			continue
		}
		remainder := strings.TrimPrefix(userAgent, prefix)
		suffix := " (" + surface.SuffixName + "; " + surface.SuffixVersion + ")"
		includeSuffix := strings.HasSuffix(remainder, suffix)
		terminalToken := remainder
		if includeSuffix {
			terminalToken = strings.TrimSuffix(remainder, suffix)
		}
		rendered, renderErr := profile.RenderUserAgentWithTerminal(surface.ID, terminalToken, includeSuffix)
		if renderErr == nil && rendered == userAgent {
			candidate := surface
			selectedSurface = &candidate
			selectedTerminalToken = terminalToken
			selectedSuffixEnabled = includeSuffix
			break
		}
	}
	if selectedSurface == nil {
		// 出站形态一律由账号绑定的 active 画像定型，入站声明的版本与平台对上游
		// 不可见，因此任何官方或第三方入口都投影到该画像，不因身份对不上而拒绝
		// 服务。此处曾经失败关闭，既误拒新版本客户端，也误拒真正的官方同版本
		// 客户端——画像平台串固定为 Ubuntu，macOS/Windows 上的官方 CLI 同样
		// 通不过完整线形校验。
		logger.LegacyPrintf(
			"service.official_egress_codex",
			"[Codex] 入站身份未匹配 %s 画像 surface，按 active 画像投影出站：user-agent=%q originator=%q",
			profile.Version,
			userAgent,
			originator,
		)
		return state, validateOfficialCodex0145RuntimeState(state)
	}
	state.RequestCompressionEnabled = snapshot.RequestCompressionEnabled
	state.SurfaceID = selectedSurface.ID
	state.TerminalToken = selectedTerminalToken
	state.UserAgentSuffixEnabled = selectedSuffixEnabled
	state.ProcessPhase = officialCodexProcessPhaseInitialized
	state.Originator = selectedSurface.Originator
	if originator != selectedSurface.Originator {
		endpointID := codex0145EndpointID("")
		if len(endpointIDs) > 0 {
			endpointID = endpointIDs[0]
		}
		// 启动首次 models 省略 originator 是官方真实行为（SPEC-HDR-005），仍需
		// 识别以产生正确的进程阶段。其余不一致一律按画像 originator 出站：出站
		// 值本就来自画像，入站写了什么不影响最终线序，不构成拒绝服务的理由。
		if originator == selectedSurface.InitialModelsOriginator &&
			!state.UserAgentSuffixEnabled &&
			selectedSurface.InitialModelsMayOmit &&
			endpointID == codex0145EndpointID(officialCodexEndpointModels) {
			state.ProcessPhase = officialCodexProcessPhaseInitialModels
			state.Originator = originator
		} else {
			logger.LegacyPrintf(
				"service.official_egress_codex",
				"[Codex] 入站 originator 与 %s 画像 surface %q 不一致，按画像值出站：originator=%q endpoint=%q",
				profile.Version,
				selectedSurface.ID,
				originator,
				endpointID,
			)
		}
	}

	subagent := snapshot.Subagent
	memgen := snapshot.MemoryGeneration
	parentThreadID := snapshot.ParentThreadID
	if subagent != "" || memgen != "" || parentThreadID != "" {
		// 条件头只有在入站证据完整且自洽时才成立。证据缺失或冲突时按“条件不成立”
		// 处理——不发这几个条件头，请求仍按画像出站；条件头是官方的条件性行为，
		// 缺少它们产生的是合法形态，而拒绝服务不是。
		var turnMetadata map[string]any
		metadataErr := json.Unmarshal([]byte(snapshot.TurnMetadata), &turnMetadata)
		threadSource := officialOpenAIString(turnMetadata, "thread_source")
		subagentKind := officialOpenAIString(turnMetadata, "subagent_kind")
		metadataParent := officialOpenAIString(turnMetadata, "parent_thread_id")
		subagentErr := officialCodex0145ValidateSubagentRuntime(
			profile.Subagents,
			subagent,
			memgen,
			threadSource,
			subagentKind,
			parentThreadID,
		)
		switch {
		case metadataErr != nil:
			logger.LegacyPrintf(
				"service.official_egress_codex",
				"[Codex] 条件头缺少可验证的 turn metadata，按条件不成立出站：%v",
				metadataErr,
			)
		case subagentErr != nil:
			logger.LegacyPrintf(
				"service.official_egress_codex",
				"[Codex] 条件头不在 %s 画像声明范围，按条件不成立出站：%v",
				profile.Version,
				subagentErr,
			)
		case metadataParent != parentThreadID:
			logger.LegacyPrintf(
				"service.official_egress_codex",
				"[Codex] parent thread 与 turn metadata 冲突，按条件不成立出站：header=%q metadata=%q",
				parentThreadID,
				metadataParent,
			)
		default:
			if memgen != "" {
				state.ConditionalHeaders["x-openai-memgen-request"] = memgen
			}
			if subagent != "" {
				state.ConditionalHeaders["x-openai-subagent"] = subagent
			}
			if parentThreadID != "" {
				state.ConditionalHeaders["x-codex-parent-thread-id"] = parentThreadID
			}
		}
	}
	if err := validateOfficialCodex0145RuntimeState(state); err != nil {
		return officialCodex0145RuntimeState{}, err
	}
	return state, nil
}

func officialCodex0145ValidateSubagentRuntime(
	profile officialCodexSubagentProfile,
	headerValue string,
	memgenValue string,
	threadSource string,
	metadataKind string,
	parentThreadID string,
) error {
	if headerValue == "" {
		if memgenValue != "" || parentThreadID != "" {
			return fmt.Errorf("Codex 0.145.0 条件头缺少 x-openai-subagent")
		}
		return nil
	}
	memoryGeneration := memgenValue != ""
	if memoryGeneration && memgenValue != "true" {
		return fmt.Errorf("Codex 0.145.0 memory generation 只允许 true")
	}
	for _, mapping := range profile.Mappings {
		if mapping.HeaderValue != headerValue || mapping.MemoryGeneration != memoryGeneration {
			continue
		}
		if mapping.ThreadSource != threadSource || mapping.MetadataKind != metadataKind {
			continue
		}
		if mapping.ParentThreadRequired && parentThreadID == "" {
			return fmt.Errorf("Codex 0.145.0 subagent %s 缺少 parent thread", mapping.ID)
		}
		return nil
	}
	if !memoryGeneration && profile.OtherLabelAllowed &&
		threadSource == profile.OtherThreadSource &&
		(!profile.OtherHeaderEqualsKind || headerValue == metadataKind) &&
		metadataKind != "" {
		return nil
	}
	return fmt.Errorf("Codex 0.145.0 subagent 条件不在版本画像中：header=%q kind=%q source=%q memgen=%t", headerValue, metadataKind, threadSource, memoryGeneration)
}

func validateOfficialCodex0145RuntimeState(state officialCodex0145RuntimeState) error {
	profile, err := resolveActiveCodexVersionProfile()
	if err != nil {
		return err
	}
	var selectedSurface *officialCodexSurfaceProfile
	for _, surface := range profile.Surfaces {
		if surface.ID == state.SurfaceID {
			candidate := surface
			selectedSurface = &candidate
			break
		}
	}
	if selectedSurface == nil {
		return fmt.Errorf("Codex 0.145.0 运行态引用未知入口：%q", state.SurfaceID)
	}
	if _, err := profile.RenderUserAgentWithTerminal(
		state.SurfaceID,
		state.TerminalToken,
		state.UserAgentSuffixEnabled,
	); err != nil {
		return err
	}
	switch state.ProcessPhase {
	case officialCodexProcessPhaseInitialized:
		if state.Originator != selectedSurface.Originator {
			return fmt.Errorf("Codex 0.145.0 initialized 阶段 originator 与入口冲突")
		}
	case officialCodexProcessPhaseInitialModels:
		if !selectedSurface.InitialModelsMayOmit || state.UserAgentSuffixEnabled ||
			state.Originator != selectedSurface.InitialModelsOriginator {
			return fmt.Errorf("Codex 0.145.0 initial models 阶段状态无效")
		}
	default:
		return fmt.Errorf("Codex 0.145.0 运行态引用未知进程阶段：%q", state.ProcessPhase)
	}
	for rawName, rawValue := range state.ConditionalHeaders {
		name := strings.ToLower(strings.TrimSpace(rawName))
		value := strings.TrimSpace(rawValue)
		if _, ok := officialCodex0145TrustedConditionalHeaders[name]; !ok {
			return fmt.Errorf("Codex 0.145.0 运行态包含未知条件头：%s", rawName)
		}
		if value == "" {
			return fmt.Errorf("Codex 0.145.0 条件头 %s 为空", name)
		}
		switch name {
		case "x-openai-internal-codex-residency":
			if value != "us" {
				return fmt.Errorf("Codex 0.145.0 residency 只允许 us")
			}
		case "x-openai-memgen-request", "x-responsesapi-include-timing-metrics":
			if value != "true" {
				return fmt.Errorf("Codex 0.145.0 条件头 %s 只允许 true", name)
			}
		case "x-codex-parent-thread-id":
			if _, parseErr := uuid.Parse(value); parseErr != nil {
				return fmt.Errorf("Codex 0.145.0 parent thread 必须是 UUID")
			}
		}
	}
	_, hasSubagent := state.ConditionalHeaders["x-openai-subagent"]
	if _, hasMemgen := state.ConditionalHeaders["x-openai-memgen-request"]; hasMemgen && !hasSubagent {
		return fmt.Errorf("Codex 0.145.0 memory generation 必须同时声明 subagent")
	}
	if _, hasParent := state.ConditionalHeaders["x-codex-parent-thread-id"]; hasParent && !hasSubagent {
		return fmt.Errorf("Codex 0.145.0 parent thread 必须同时声明 subagent")
	}
	return nil
}

// officialCodex0145ProcessIdentity 从冻结上下文中的入口和进程状态生成身份。
func officialCodex0145ProcessIdentity(egressContext *OfficialEgressContext) (string, string, error) {
	if egressContext == nil {
		return "", "", fmt.Errorf("Codex 0.145.0 进程身份缺少出站上下文")
	}
	profile, err := resolveCodex0145VersionProfile(egressContext.ProfileVersion())
	if err != nil {
		return "", "", err
	}
	state := egressContext.codexRuntimeState
	if err := validateOfficialCodex0145RuntimeState(state); err != nil {
		return "", "", err
	}
	userAgent, err := profile.RenderUserAgentWithTerminal(
		state.SurfaceID,
		state.TerminalToken,
		state.UserAgentSuffixEnabled,
	)
	if err != nil {
		return "", "", err
	}
	if strings.TrimSpace(state.Originator) != "" {
		return userAgent, state.Originator, nil
	}
	return "", "", fmt.Errorf("Codex %s 缺少 %s 进程阶段 originator", profile.Version, state.ProcessPhase)
}

// officialCodex0145FinalizeEndpointHeaders 是版本画像与具体请求之间的唯一薄层。
// 调用方只提供运行态条件；允许的 header、常量值与线序全部由不可变端点画像决定。
func officialCodex0145FinalizeEndpointHeaders(
	egressContext *OfficialEgressContext,
	headers http.Header,
	conditionOverrides map[string]bool,
) ([]officialCodex0145HeaderField, error) {
	if egressContext == nil {
		return nil, fmt.Errorf("Codex 0.145.0 header 定型缺少出站上下文")
	}
	version := egressContext.ProfileVersion()
	endpointID := codex0145EndpointID(egressContext.CodexEndpointProfileID())
	endpoint, err := resolveCodex0145Endpoint(version, endpointID)
	if err != nil {
		return nil, err
	}

	// 入站白名单只决定哪些语义可以进入 Finalizer；最终出站必须再次按端点画像
	// 收敛为闭集，Accept-Language 等客户端环境噪声不能泄漏到官方 wire。
	allowed := make(map[string]struct{}, len(endpoint.Headers))
	for _, slot := range endpoint.Headers {
		allowed[strings.ToLower(slot.Name)] = struct{}{}
	}
	for name := range headers {
		if _, ok := allowed[strings.ToLower(name)]; !ok {
			headers.Del(name)
		}
	}
	// 五个敏感条件只能由冻结运行态回填。先删除请求/账号覆写中的同名值，
	// 再写入已经通过来源和值校验的快照，避免把“header 恰好存在”当作 feature。
	for name := range officialCodex0145TrustedConditionalHeaders {
		headers.Del(name)
		if _, endpointAllows := allowed[name]; !endpointAllows {
			continue
		}
		if value := strings.TrimSpace(egressContext.codexRuntimeState.ConditionalHeaders[name]); value != "" {
			headers.Set(name, value)
		}
	}

	conditions := officialCodex0145ConditionsFromHeaders(headers)
	for name, enabled := range conditionOverrides {
		if headerName, protected := officialCodex0145HeaderForTrustedCondition(name); protected {
			expected := strings.TrimSpace(egressContext.codexRuntimeState.ConditionalHeaders[headerName]) != ""
			if enabled != expected {
				return nil, fmt.Errorf("Codex 0.145.0 条件 %s 不能绕过冻结运行态", name)
			}
		}
		conditions[name] = enabled
	}
	for headerName, condition := range officialCodex0145TrustedConditionalHeaders {
		conditions[condition] = strings.TrimSpace(egressContext.codexRuntimeState.ConditionalHeaders[headerName]) != ""
	}
	return officialCodex0145ApplyHeaderContract(version, endpointID, headers, conditions)
}

func officialCodex0145HeaderForTrustedCondition(condition string) (string, bool) {
	for headerName, candidate := range officialCodex0145TrustedConditionalHeaders {
		if candidate == condition {
			return headerName, true
		}
	}
	return "", false
}

// officialCodex0145ConditionsFromHeaders 只把动态值映射成画像条件，不包含端点判断。
// 因而新增端点时只需扩展画像，不需要在业务代码追加 path/endpoint 分支。
func officialCodex0145ConditionsFromHeaders(headers http.Header) map[string]bool {
	present := func(name string) bool {
		return strings.TrimSpace(headers.Get(name)) != ""
	}
	conditions := map[string]bool{
		officialCodexConditionCookie:             present("cookie"),
		officialCodexConditionBetaFeatures:       present("x-codex-beta-features"),
		officialCodexConditionResponsesLite:      present(responsesLiteHeader),
		officialCodexConditionTurnState:          present("x-codex-turn-state"),
		officialCodexConditionResidency:          present("x-openai-internal-codex-residency"),
		officialCodexConditionSubagent:           present("x-openai-subagent"),
		officialCodexConditionMemoryGeneration:   present("x-openai-memgen-request"),
		officialCodexConditionParentThread:       present("x-codex-parent-thread-id"),
		officialCodexConditionRuntimeMetrics:     present("x-responsesapi-include-timing-metrics"),
		officialCodexConditionSessionID:          present("x-session-id"),
		officialCodexConditionCreditID:           present("credit-id"),
		officialCodexConditionAttestation:        present("x-oai-attestation"),
		officialCodexConditionFedRAMP:            present("x-openai-fedramp"),
		officialCodexConditionRequestCompression: headerContainsToken(headers, "content-encoding", "zstd"),
		officialCodexConditionRemoteCompactionV2: headerContainsToken(headers, "x-codex-beta-features", "remote_compaction_v2"),
	}
	return conditions
}

func headerContainsToken(headers http.Header, name, expected string) bool {
	for _, value := range headers.Values(name) {
		for _, token := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(token), expected) {
				return true
			}
		}
	}
	return false
}

// officialCodex0145FinalizeEndpointJSONBody 校验端点绑定后，再由同一画像完成闭集
// 校验与稳定字段排序。它拒绝上下文端点和调用端点发生漂移。
func officialCodex0145FinalizeEndpointJSONBody(
	egressContext *OfficialEgressContext,
	body []byte,
	conditions map[string]bool,
) ([]byte, error) {
	if egressContext == nil {
		return nil, fmt.Errorf("Codex 0.145.0 body 定型缺少出站上下文")
	}
	finalized, err := officialCodex0145ValidateAndOrderJSONBody(
		egressContext.ProfileVersion(),
		codex0145EndpointID(egressContext.CodexEndpointProfileID()),
		body,
		conditions,
	)
	if err != nil {
		return nil, err
	}
	if err := officialCodex0145ValidateToolPresentation(
		egressContext.ProfileVersion(),
		codex0145EndpointID(egressContext.CodexEndpointProfileID()),
		finalized,
		egressContext.responsesLite,
	); err != nil {
		return nil, err
	}
	return finalized, nil
}

// officialCodex0145ProjectEndpointJSONBody 用于第三方协议到 Codex 协议的派生路径：
// 先按画像字段闭集投影，再交给严格执行器校验省略条件和最终顺序。未知入站字段
// 只会在这一明确的“派生”入口被丢弃；官方直通请求仍使用严格函数并拒绝未知字段。
func officialCodex0145ProjectEndpointJSONBody(
	version string,
	endpointID codex0145EndpointID,
	payload map[string]any,
	original []byte,
	conditions map[string]bool,
) ([]byte, error) {
	endpoint, err := resolveCodex0145Endpoint(version, endpointID)
	if err != nil {
		return nil, err
	}
	if endpoint.Body.Encoding != "json" || !endpoint.Body.Closed {
		return nil, fmt.Errorf("Codex 端点 %s 不是封闭 JSON 派生目标", endpoint.ID)
	}
	declared := make(map[string]struct{}, len(endpoint.Body.Fields))
	projected := make(map[string]any, len(endpoint.Body.Fields))
	order := make([]string, 0, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		declared[field.Name] = struct{}{}
		order = append(order, field.Name)
		if value, exists := payload[field.Name]; exists {
			projected[field.Name] = value
		}
	}
	// 画像闭集之外的顶层字段在此被抹掉。新版本客户端的新增字段正是从这里无声
	// 消失的，表现为“功能不生效但请求成功”，必须留下可查信号——它同时也是
	// “该做新版本画像了”的唯一运行时提示。
	dropped := make([]string, 0, len(payload))
	for name := range payload {
		if _, ok := declared[name]; !ok {
			dropped = append(dropped, name)
		}
	}
	if len(dropped) > 0 {
		sort.Strings(dropped)
		logger.LegacyPrintf(
			"service.official_egress_codex",
			"[Codex] 端点 %s 的 body 投影丢弃画像闭集外字段：%s",
			endpoint.ID,
			strings.Join(dropped, ","),
		)
	}
	encoded, err := marshalOfficialOrderedJSONObjectPreservingRaw(projected, order, original)
	if err != nil {
		return nil, fmt.Errorf("编码 Codex 端点 %s 派生 body：%w", endpoint.ID, err)
	}
	return officialCodex0145ValidateAndOrderJSONBody(
		version,
		endpointID,
		encoded,
		conditions,
	)
}
