package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const (
	officialCodexForceHTTPFallbackKey       = "official_codex_force_http_fallback"
	officialCodexResidencyAccountExtraKey   = "official_codex_enforce_residency"
	officialCodexRuntimeMetricsAccountExtra = "official_codex_runtime_metrics"
)

var officialCodexTrustedConditionalHeaders = map[string]string{
	"x-openai-internal-codex-residency":     officialCodexConditionResidency,
	"x-openai-subagent":                     officialCodexConditionSubagent,
	"x-openai-memgen-request":               officialCodexConditionMemoryGeneration,
	"x-codex-parent-thread-id":              officialCodexConditionParentThread,
	"x-responsesapi-include-timing-metrics": officialCodexConditionRuntimeMetrics,
}

type officialCodexRuntimeStateContextKey struct{}
type officialCodexIngressRuntimeContextKey struct{}

type officialCodexIngressRuntimeSnapshot struct {
	OfficialClient            bool
	UserAgent                 string
	Originator                string
	RequestCompressionEnabled bool
	Subagent                  string
	MemoryGeneration          string
	ParentThreadID            string
	TurnMetadata              string
}

// WithOfficialCodexIngressRuntime 在路由入口保存原始进程身份与 feature。
// 它不判定账号 requirements，也不生成出站 header；账号选定后仍须由版本画像完成
// 配对和条件关系校验。必须在 body 解压前调用，避免 Content-Encoding 被标准化删除后
// 丢失官方进程的 request compression 状态。重复调用保持首次快照，不能用已经标准化
// 的请求覆盖原始 wire 事实；realtime 也用同一入口跨调度边界冻结身份。
func WithOfficialCodexIngressRuntime(ctx context.Context, c *gin.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if _, captured := officialCodexIngressRuntimeSnapshotFromContext(ctx); captured {
		return ctx
	}
	return context.WithValue(
		ctx,
		officialCodexIngressRuntimeContextKey{},
		officialCodexIngressRuntimeSnapshotFromGin(c),
	)
}

func officialCodexIngressRuntimeSnapshotFromGin(c *gin.Context) officialCodexIngressRuntimeSnapshot {
	if c == nil || c.Request == nil {
		return officialCodexIngressRuntimeSnapshot{}
	}
	return officialCodexIngressRuntimeSnapshot{
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

func officialCodexIngressRuntimeSnapshotFromContext(
	ctx context.Context,
) (officialCodexIngressRuntimeSnapshot, bool) {
	if ctx == nil {
		return officialCodexIngressRuntimeSnapshot{}, false
	}
	snapshot, ok := ctx.Value(officialCodexIngressRuntimeContextKey{}).(officialCodexIngressRuntimeSnapshot)
	return snapshot, ok
}

// withOfficialCodexRuntimeState 把已经完成来源校验的进程快照绑定到业务上下文。
// models、alpha-search、images 等辅助端点会在协议转换后重新构造请求，因此不能
// 再从最终 header 猜测入口；它们只消费这里冻结的值。
func withOfficialCodexRuntimeState(
	ctx context.Context,
	state officialCodexRuntimeState,
) (context.Context, error) {
	if err := validateOfficialCodexRuntimeState(state); err != nil {
		return nil, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(
		ctx,
		officialCodexRuntimeStateContextKey{},
		cloneOfficialCodexRuntimeState(state),
	), nil
}

func officialCodexRuntimeStateFromContext(
	ctx context.Context,
) (officialCodexRuntimeState, bool, error) {
	if ctx == nil {
		return officialCodexRuntimeState{}, false, nil
	}
	state, ok := ctx.Value(officialCodexRuntimeStateContextKey{}).(officialCodexRuntimeState)
	if !ok {
		return officialCodexRuntimeState{}, false, nil
	}
	state = cloneOfficialCodexRuntimeState(state)
	if err := validateOfficialCodexRuntimeState(state); err != nil {
		return officialCodexRuntimeState{}, false, err
	}
	return state, true, nil
}

// resolveBoundOrIngressOfficialCodexRuntimeState 统一所有 attach 路径的
// 运行态来源优先级：先消费 token 刷新前冻结的 context；只有调用方尚未绑定时，
// 才从当前入站对象解析。辅助端点可传 nil ingress，并得到受管理账号条件与默认
// exec 画像。该入口避免 HTTP、WS 和辅助链各自重新猜测进程状态。
func resolveBoundOrIngressOfficialCodexRuntimeState(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	mode string,
	endpointIDs ...codexEndpointID,
) (officialCodexRuntimeState, error) {
	state, bound, err := officialCodexRuntimeStateFromContext(ctx)
	if err != nil {
		return officialCodexRuntimeState{}, err
	}
	if bound {
		return state, nil
	}
	if snapshot, captured := officialCodexIngressRuntimeSnapshotFromContext(ctx); captured {
		return resolveOfficialCodexRuntimeStateFromSnapshot(snapshot, account, mode, endpointIDs...)
	}
	if c != nil && c.Request != nil {
		if snapshot, captured := officialCodexIngressRuntimeSnapshotFromContext(c.Request.Context()); captured {
			return resolveOfficialCodexRuntimeStateFromSnapshot(snapshot, account, mode, endpointIDs...)
		}
	}
	return resolveOfficialCodexRuntimeState(c, account, mode, endpointIDs...)
}

func bindOfficialCodexRuntimeStateFromIngress(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	mode string,
	endpointIDs ...codexEndpointID,
) (context.Context, error) {
	state, err := resolveBoundOrIngressOfficialCodexRuntimeState(
		ctx,
		c,
		account,
		mode,
		endpointIDs...,
	)
	if err != nil {
		return nil, err
	}
	return withOfficialCodexRuntimeState(ctx, state)
}

// BindOfficialCodexResponsesWebSocketRuntime 在 handler 已选定 OAuth 账号后，
// 通过单一版本画像冻结 WS 入站进程状态。调用方必须在 token 刷新之前使用返回的
// context，并把同一 context 继续传给 WS 代理，避免刷新与最终握手出现两套身份。
func BindOfficialCodexResponsesWebSocketRuntime(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	modes ...string,
) (context.Context, error) {
	if account == nil || !account.IsOpenAIOAuth() {
		return nil, fmt.Errorf("Codex 0.145.0 Responses WebSocket 仅允许 OpenAI OAuth 账号")
	}
	mode := ""
	if len(modes) > 0 {
		mode = normalizeOfficialClientProfileMode(modes[0])
	} else {
		var err error
		mode, err = officialCodexProcessProfileMode()
		if err != nil {
			return nil, err
		}
	}
	return bindOfficialCodexRuntimeStateFromIngress(
		ctx,
		c,
		account,
		mode,
		codexEndpointID(officialCodexEndpointResponsesWS),
	)
}

func bindOfficialCodexRuntimeStateFromCapturedIngress(
	ctx context.Context,
	account *Account,
	mode string,
	endpointIDs ...codexEndpointID,
) (context.Context, officialCodexRuntimeState, error) {
	snapshot := officialCodexIngressRuntimeSnapshot{}
	if ctx != nil {
		if captured, ok := ctx.Value(officialCodexIngressRuntimeContextKey{}).(officialCodexIngressRuntimeSnapshot); ok {
			snapshot = captured
		}
	}
	state, err := resolveOfficialCodexRuntimeStateFromSnapshot(
		snapshot, account, mode, endpointIDs...,
	)
	if err != nil {
		return nil, officialCodexRuntimeState{}, err
	}
	bound, err := withOfficialCodexRuntimeState(ctx, state)
	if err != nil {
		return nil, officialCodexRuntimeState{}, err
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

// officialCodexVersionForMode 只从 officialegress 的正式 ReleaseCatalog 读取发布事实。
func officialCodexVersionForMode(mode string) (string, error) {
	releaseMode := officialegress.ReleaseMode(mode)
	resolved, err := officialegress.DefaultReleaseCatalog().Resolve(releaseMode)
	if err != nil {
		return "", err
	}
	version := strings.TrimSpace(resolved.Version())
	if version == "" {
		return "", fmt.Errorf("正式 ReleaseCatalog 未提供 Codex 版本")
	}
	return version, nil
}

// resolveOfficialCodexRuntimeState 把入口语义与敏感条件头收敛成可信进程快照。
// surface、终端指纹、originator 和版本只来自 active 画像默认值；压缩开关
// 由官方客户端在解压前冻结的 wire 状态决定。子代理等条件事实仍在冻结前独立校验。
func resolveOfficialCodexRuntimeState(
	c *gin.Context,
	account *Account,
	mode string,
	endpointIDs ...codexEndpointID,
) (officialCodexRuntimeState, error) {
	return resolveOfficialCodexRuntimeStateFromSnapshot(
		officialCodexIngressRuntimeSnapshotFromGin(c),
		account,
		mode,
		endpointIDs...,
	)
}

func resolveOfficialCodexRuntimeStateFromSnapshot(
	snapshot officialCodexIngressRuntimeSnapshot,
	account *Account,
	mode string,
	endpointIDs ...codexEndpointID,
) (officialCodexRuntimeState, error) {
	state := defaultOfficialCodexRuntimeState()
	state.ProfileMode = normalizeOfficialClientProfileMode(mode)
	profile, err := resolveCodexVersionProfileForMode(state.ProfileMode)
	if err != nil {
		return officialCodexRuntimeState{}, err
	}
	trustedClient, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		state.ProfileMode,
	)
	if err != nil {
		return officialCodexRuntimeState{}, err
	}
	if err := applyOfficialCodexTrustedBuildRuntimeDefaults(&state, profile, trustedClient.Build); err != nil {
		return officialCodexRuntimeState{}, err
	}
	// 画像只决定是否支持压缩；一次普通 Responses 请求是否已经启用压缩，必须
	// 使用 body 解压前冻结的官方客户端 wire 状态。第三方客户端即使伪造
	// Content-Encoding，也不能取得该条件的所有权；Lite 模型能力在后续身份投影
	// 中独立启用画像支持的压缩。
	state.RequestCompressionEnabled = snapshot.OfficialClient && snapshot.RequestCompressionEnabled
	if account != nil {
		residency := strings.TrimSpace(account.GetExtraString(officialCodexResidencyAccountExtraKey))
		if residency != "" {
			if residency != "us" {
				return officialCodexRuntimeState{}, fmt.Errorf(
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
	_ = endpointIDs

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
		subagentErr := officialCodexValidateSubagentRuntime(
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
	if err := validateOfficialCodexRuntimeState(state); err != nil {
		return officialCodexRuntimeState{}, err
	}
	return state, nil
}

// applyOfficialCodexTrustedBuildRuntimeDefaults 把 ReleaseCatalog 已签入的 Build UA
// 还原成结构化运行态。这里的输入是内部发布事实，不是入站客户端声明；因此它只负责
// 保留 active/previous 的冻结 surface、终端指纹与 suffix，不构成入口 UA 匹配分支。
func applyOfficialCodexTrustedBuildRuntimeDefaults(
	state *officialCodexRuntimeState,
	profile *officialCodexVersionProfile,
	build officialClientBuild,
) error {
	if state == nil || profile == nil {
		return errors.New("Codex release Build 运行态投影输入为空")
	}
	userAgent := strings.TrimSpace(build.UserAgent)
	if userAgent == "" || strings.TrimSpace(build.Version) != profile.Version {
		return fmt.Errorf("Codex release Build 与版本画像不一致：build=%q profile=%q", build.Version, profile.Version)
	}
	for _, surface := range profile.Surfaces {
		prefix := strings.TrimSpace(surface.Product+"/"+surface.Version+" "+surface.PlatformPrefix) + " "
		if !strings.HasPrefix(userAgent, prefix) {
			continue
		}
		terminalToken := strings.TrimPrefix(userAgent, prefix)
		includeSuffix := false
		if surface.SuffixName != "" && surface.SuffixVersion != "" {
			suffix := fmt.Sprintf(" (%s; %s)", surface.SuffixName, surface.SuffixVersion)
			if strings.HasSuffix(terminalToken, suffix) {
				terminalToken = strings.TrimSuffix(terminalToken, suffix)
				includeSuffix = true
			}
		}
		rendered, err := profile.RenderUserAgentWithTerminal(surface.ID, terminalToken, includeSuffix)
		if err != nil || rendered != userAgent || strings.TrimSpace(build.Originator) != surface.Originator {
			continue
		}
		state.SurfaceID = surface.ID
		state.ProcessPhase = officialCodexProcessPhaseInitialized
		state.Originator = surface.Originator
		state.TerminalToken = terminalToken
		state.UserAgentSuffixEnabled = includeSuffix
		return nil
	}
	return fmt.Errorf("Codex release Build UA 无法由版本画像解释：%q", userAgent)
}

func officialCodexValidateSubagentRuntime(
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

func validateOfficialCodexRuntimeState(state officialCodexRuntimeState) error {
	if strings.TrimSpace(state.ProfileMode) == "" {
		return errors.New("Codex runtime state 缺少冻结 ProfileMode")
	}
	profile, err := resolveCodexVersionProfileForMode(state.ProfileMode)
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
		if _, ok := officialCodexTrustedConditionalHeaders[name]; !ok {
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

// officialCodexConditionsFromHeaders 只把动态值映射成画像条件，不包含端点判断。
// 因而新增端点时只需扩展画像，不需要在业务代码追加 path/endpoint 分支。
func officialCodexConditionsFromHeaders(headers http.Header) map[string]bool {
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

// officialCodexProjectEndpointJSONBody 用于第三方协议到 Codex 协议的派生路径：
// 先按画像字段闭集投影，再交给严格执行器校验省略条件和最终顺序。未知入站字段
// 只会在这一明确的“派生”入口被丢弃；官方直通请求仍使用严格函数并拒绝未知字段。
func officialCodexProjectEndpointJSONBody(
	version string,
	endpointID codexEndpointID,
	payload map[string]any,
	original []byte,
	conditions map[string]bool,
) ([]byte, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return projectOfficialCodexEndpointJSONBody(
		profile, string(endpointID), payload, original, conditions,
	)
}

func projectOfficialCodexEndpointJSONBody(
	profile *officialCodexVersionProfile,
	endpointID string,
	payload map[string]any,
	original []byte,
	conditions map[string]bool,
) ([]byte, error) {
	if profile == nil {
		return nil, errors.New("Codex JSON 投影画像为空")
	}
	endpoint, err := profile.ResolveEndpoint(endpointID)
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
	return validateAndOrderOfficialCodexJSONBody(profile, endpointID, encoded, conditions)
}
