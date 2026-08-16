package service

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"path"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/gatewayendpoint"
)

const (
	// OfficialEgressProfileVersionPhase0 仅保留给旧配置和测试迁移识别，生产解析不再使用组合 Bundle。
	OfficialEgressProfileVersionPhase0 = "phase0-2026-07-24"
)

// officialEgressInboundHostHeaders 是三条官方出站路径共同剥离的入站宿主环境头。
// 官方 Claude Code / Codex CLI 都不发送它们：accept-language 与 sec-fetch-mode 来自浏览器
// 或 IDE 宿主，x-stainless-helper-method 来自 Stainless 生成的 SDK。入站白名单会放行这些头，
// 因此必须在终态定型阶段统一删除；否则第三方客户端的宿主特征会与官方身份头出现在同一请求上，
// 形成官方客户端不可能产生的混合形态。
var officialEgressInboundHostHeaders = [...]string{
	"accept-language",
	"sec-fetch-mode",
	"x-stainless-helper-method",
}

// stripOfficialEgressInboundHostHeaders 删除上述入站头的全部大小写形式。
func stripOfficialEgressInboundHostHeaders(header http.Header) {
	for _, name := range officialEgressInboundHostHeaders {
		deleteHeaderAllForms(header, name)
	}
}

// OfficialEgressTransport 区分 HTTP 请求与 WebSocket 连接，禁止二者共用画像状态。
type OfficialEgressTransport string

const (
	OfficialEgressTransportHTTP      OfficialEgressTransport = "http"
	OfficialEgressTransportWebSocket OfficialEgressTransport = "websocket"
)

// OfficialEgressFieldSource 记录动态字段的真实来源。
type OfficialEgressFieldSource string

const (
	OfficialEgressFieldSourceIngressExplicit OfficialEgressFieldSource = "ingress_explicit"
	OfficialEgressFieldSourceRedisSession    OfficialEgressFieldSource = "redis_session"
	OfficialEgressFieldSourceAccountStatic   OfficialEgressFieldSource = "account_static"
	OfficialEgressFieldSourceDerived         OfficialEgressFieldSource = "derived"
)

// OfficialEgressFieldLifecycle 记录字段在请求、会话、连接或单轮中的生命周期。
type OfficialEgressFieldLifecycle string

const (
	OfficialEgressFieldLifecycleRequest    OfficialEgressFieldLifecycle = "request"
	OfficialEgressFieldLifecycleSession    OfficialEgressFieldLifecycle = "session"
	OfficialEgressFieldLifecycleConnection OfficialEgressFieldLifecycle = "connection"
	OfficialEgressFieldLifecycleTurn       OfficialEgressFieldLifecycle = "turn"
)

// OfficialEgressFieldName 是公共上下文允许登记的动态身份字段。
type OfficialEgressFieldName string

const (
	OfficialEgressFieldSessionID          OfficialEgressFieldName = "session_id"
	OfficialEgressFieldDeviceID           OfficialEgressFieldName = "device_id"
	OfficialEgressFieldAccountUUID        OfficialEgressFieldName = "account_uuid"
	OfficialEgressFieldClientRequestID    OfficialEgressFieldName = "client_request_id"
	OfficialEgressFieldThreadID           OfficialEgressFieldName = "thread_id"
	OfficialEgressFieldWindowID           OfficialEgressFieldName = "window_id"
	OfficialEgressFieldTurnMetadata       OfficialEgressFieldName = "turn_metadata"
	OfficialEgressFieldTurnState          OfficialEgressFieldName = "turn_state"
	OfficialEgressFieldPromptCacheKey     OfficialEgressFieldName = "prompt_cache_key"
	OfficialEgressFieldPreviousResponseID OfficialEgressFieldName = "previous_response_id"
	OfficialEgressFieldCallID             OfficialEgressFieldName = "call_id"
)

// OfficialEgressField 保存动态字段值及其来源。值保持私有，避免被结构化日志意外展开。
type OfficialEgressField struct {
	value     string
	Source    OfficialEgressFieldSource
	Lifecycle OfficialEgressFieldLifecycle
}

// Value 返回字段原值，仅供最终修正器写入上游请求。
func (f OfficialEgressField) Value() string {
	return f.value
}

// String 始终返回脱敏文本，避免调试打印泄露会话身份。
func (f OfficialEgressField) String() string {
	if strings.TrimSpace(f.value) == "" {
		return "[empty]"
	}
	return "[redacted]"
}

// officialCodexRuntimeState 保存不能从最终 header 是否存在反推的进程状态。
//
// SurfaceID 与 UserAgentSuffixEnabled 对应 SPEC-HDR-005；RequestCompressionEnabled
// 保存画像默认值或官方入口在解压前冻结的 feature 状态；ConditionalHeaders 只允许
// 装入已经由官方入口或受管理配置证明来源的条件头。普通第三方请求即使伪造同名
// header，也不能把画像条件激活。
type officialCodexRuntimeState struct {
	ProfileMode               string
	SurfaceID                 string
	ProcessPhase              string
	Originator                string
	TerminalToken             string
	UserAgentSuffixEnabled    bool
	RequestCompressionEnabled bool
	ConditionalHeaders        map[string]string
}

const (
	officialCodexProcessPhaseInitialized   = "initialized"
	officialCodexProcessPhaseInitialModels = "initial_models"
)

func defaultOfficialCodexRuntimeState() officialCodexRuntimeState {
	return officialCodexRuntimeState{
		SurfaceID:              officialCodexSurfaceExec,
		ProcessPhase:           officialCodexProcessPhaseInitialized,
		Originator:             "codex_exec",
		TerminalToken:          "unknown",
		UserAgentSuffixEnabled: true,
		// 真正的默认来源仍由不可变 Release 画像校验；这里是零输入运行态的等价值。
		RequestCompressionEnabled: true,
		ConditionalHeaders:        make(map[string]string),
	}
}

func cloneOfficialCodexRuntimeState(state officialCodexRuntimeState) officialCodexRuntimeState {
	cloned := officialCodexRuntimeState{
		ProfileMode:               strings.TrimSpace(state.ProfileMode),
		SurfaceID:                 strings.TrimSpace(state.SurfaceID),
		ProcessPhase:              strings.TrimSpace(state.ProcessPhase),
		Originator:                strings.TrimSpace(state.Originator),
		TerminalToken:             strings.TrimSpace(state.TerminalToken),
		UserAgentSuffixEnabled:    state.UserAgentSuffixEnabled,
		RequestCompressionEnabled: state.RequestCompressionEnabled,
		ConditionalHeaders:        make(map[string]string, len(state.ConditionalHeaders)),
	}
	for name, value := range state.ConditionalHeaders {
		cloned.ConditionalHeaders[strings.ToLower(strings.TrimSpace(name))] = strings.TrimSpace(value)
	}
	return cloned
}

// OfficialEgressContextInput 是每次选定账号后构造公共上下文所需的稳定输入。
type OfficialEgressContextInput struct {
	AccountID       int64
	TargetPlatform  string
	InboundEndpoint string
	Transport       OfficialEgressTransport
	UpstreamHost    string
	ProfileVersion  string
	ProfileMode     string
	AccountType     string
	ProxyID         int64
	CAFingerprint   string
	ResponsesLite   bool
	ParallelTools   bool
	// Reasoning 默认值来自同一版本 `/models` 清单；Finalizer 只消费已经冻结的
	// 快照，不能在最终出站阶段再次查询或按模型名猜测。
	DefaultReasoningLevel             string
	DefaultReasoningSummary           string
	SupportsReasoningSummaryParameter bool
	ReasoningDefaultsKnown            bool
	// CodexEndpointID 仅用于已经由当前 Release 画像解析的辅助端点。
	// 主 Responses 入口留空，由公共 Resolver 根据传输与入口映射。
	CodexEndpointID string
	// InvocationID 标识一次上层 API 调用。同一次调用的 retry/rebuild 必须复用，
	// 不同调用必须不同，以复刻 Codex 主模型 HTTP Client 的生命周期。
	InvocationID string
	// CodexRuntimeState 是版本画像解析前已经完成来源校验的进程快照。
	// 零值只用于非 OpenAI 画像；OpenAI 零值会采用 exec 已初始化状态。
	CodexRuntimeState officialCodexRuntimeState
}

// OfficialEgressContext 保存最终出站画像所需的公共状态。
//
// 核心路由字段创建后不可修改；动态字段只能通过 RegisterField 登记。WebSocket
// 握手前必须 Freeze，冻结后的上下文不会接受任何身份字段变化。
type OfficialEgressContext struct {
	accountID           int64
	targetPlatform      string
	inboundEndpoint     string
	transport           OfficialEgressTransport
	upstreamHost        string
	profileVersion      string
	profileMode         string
	accountType         string
	proxyID             int64
	caFingerprint       string
	transportProfileID  string
	clientProfileID     string
	clientProfileDigest string
	// Codex 版本画像与旧的公共客户端 Registry 分开保存。Registry 只兼容历史
	// 入口选择；端点、字段、header 与传输的最终事实源是这三个不可变画像标识。
	codexVersionProfileID     string
	codexVersionProfileDigest string
	codexEndpointProfileID    string
	codexRequestedEndpointID  string
	connectionPoolID          string
	fields                    map[OfficialEgressFieldName]OfficialEgressField
	openAIWSDerived           *officialOpenAIWSDerivedState
	responsesLite             bool
	parallelTools             bool
	defaultReasoningLevel     string
	defaultReasoningSummary   string
	supportsReasoningSummary  bool
	reasoningDefaultsKnown    bool
	invocationID              string
	codexRuntimeState         officialCodexRuntimeState
	cookieJar                 http.CookieJar
	frozen                    bool
}

// NewOfficialEgressContext 创建一份请求级公共上下文，不执行路径画像修正。
func NewOfficialEgressContext(input OfficialEgressContextInput) *OfficialEgressContext {
	runtimeState := cloneOfficialCodexRuntimeState(input.CodexRuntimeState)
	if strings.EqualFold(strings.TrimSpace(input.TargetPlatform), PlatformOpenAI) && runtimeState.SurfaceID == "" {
		runtimeState = defaultOfficialCodexRuntimeState()
	}
	if strings.EqualFold(strings.TrimSpace(input.TargetPlatform), PlatformOpenAI) {
		runtimeState.ProfileMode = normalizeOfficialClientProfileMode(input.ProfileMode)
	}
	return &OfficialEgressContext{
		accountID:                input.AccountID,
		targetPlatform:           strings.ToLower(strings.TrimSpace(input.TargetPlatform)),
		inboundEndpoint:          canonicalOfficialEgressInboundEndpoint(input.InboundEndpoint),
		transport:                input.Transport,
		upstreamHost:             normalizeOfficialEgressHost(input.UpstreamHost),
		profileVersion:           strings.TrimSpace(input.ProfileVersion),
		profileMode:              normalizeOfficialClientProfileMode(input.ProfileMode),
		accountType:              strings.ToLower(strings.TrimSpace(input.AccountType)),
		proxyID:                  input.ProxyID,
		caFingerprint:            strings.TrimSpace(input.CAFingerprint),
		responsesLite:            input.ResponsesLite,
		parallelTools:            input.ParallelTools,
		defaultReasoningLevel:    strings.ToLower(strings.TrimSpace(input.DefaultReasoningLevel)),
		defaultReasoningSummary:  strings.ToLower(strings.TrimSpace(input.DefaultReasoningSummary)),
		supportsReasoningSummary: input.SupportsReasoningSummaryParameter,
		reasoningDefaultsKnown:   input.ReasoningDefaultsKnown,
		codexRequestedEndpointID: strings.TrimSpace(input.CodexEndpointID),
		invocationID:             strings.TrimSpace(input.InvocationID),
		codexRuntimeState:        runtimeState,
		fields:                   make(map[OfficialEgressFieldName]OfficialEgressField),
	}
}

// InvocationID 返回当前上层调用的连接生命周期标识。
func (c *OfficialEgressContext) InvocationID() string {
	if c == nil {
		return ""
	}
	return c.invocationID
}

func (c *OfficialEgressContext) AccountID() int64 {
	if c == nil {
		return 0
	}
	return c.accountID
}

func (c *OfficialEgressContext) TargetPlatform() string {
	if c == nil {
		return ""
	}
	return c.targetPlatform
}

func (c *OfficialEgressContext) InboundEndpoint() string {
	if c == nil {
		return ""
	}
	return c.inboundEndpoint
}

func (c *OfficialEgressContext) Transport() OfficialEgressTransport {
	if c == nil {
		return ""
	}
	return c.transport
}

func (c *OfficialEgressContext) UpstreamHost() string {
	if c == nil {
		return ""
	}
	return c.upstreamHost
}

func (c *OfficialEgressContext) ProfileVersion() string {
	if c == nil {
		return ""
	}
	return c.profileVersion
}

// ProfileMode 返回调用或 WebSocket 会话开始时冻结的发布模式。
func (c *OfficialEgressContext) ProfileMode() string {
	if c == nil {
		return ""
	}
	return c.profileMode
}

// ClientProfileID 返回经过 Registry 解析的不可变画像标识。
func (c *OfficialEgressContext) ClientProfileID() string {
	if c == nil {
		return ""
	}
	return c.clientProfileID
}

// ClientProfileDigest 返回画像静态数据摘要，不包含动态身份值。
func (c *OfficialEgressContext) ClientProfileDigest() string {
	if c == nil {
		return ""
	}
	return c.clientProfileDigest
}

// CodexVersionProfileID 返回当前 OpenAI OAuth 请求绑定的完整版本画像标识。
func (c *OfficialEgressContext) CodexVersionProfileID() string {
	if c == nil {
		return ""
	}
	return c.codexVersionProfileID
}

// CodexVersionProfileDigest 返回完整版本画像摘要；摘要覆盖 42 项规则对应的
// 端点、字段、header 与传输数据，不包含任何请求级身份值。
func (c *OfficialEgressContext) CodexVersionProfileDigest() string {
	if c == nil {
		return ""
	}
	return c.codexVersionProfileDigest
}

// CodexEndpointProfileID 返回本次调用已经严格解析的端点画像标识。
func (c *OfficialEgressContext) CodexEndpointProfileID() string {
	if c == nil {
		return ""
	}
	return c.codexEndpointProfileID
}

func (c *OfficialEgressContext) AccountType() string {
	if c == nil {
		return ""
	}
	return c.accountType
}

func (c *OfficialEgressContext) ProxyID() int64 {
	if c == nil {
		return 0
	}
	return c.proxyID
}

// TransportProfileID 返回当前路径已经解析的传输画像标识。
func (c *OfficialEgressContext) TransportProfileID() string {
	if c == nil {
		return ""
	}
	return c.transportProfileID
}

// ConnectionPoolID 返回包含账号、画像、传输、Host、代理与 CA 的稳定连接池键。
func (c *OfficialEgressContext) ConnectionPoolID() string {
	if c == nil {
		return ""
	}
	return c.connectionPoolID
}

func (c *OfficialEgressContext) IsFrozen() bool {
	return c != nil && c.frozen
}

// RegisterField 登记动态字段。相同字段出现不同值、来源或生命周期时明确失败。
func (c *OfficialEgressContext) RegisterField(
	name OfficialEgressFieldName,
	value string,
	source OfficialEgressFieldSource,
	lifecycle OfficialEgressFieldLifecycle,
) error {
	if c == nil {
		return errors.New("official egress context is nil")
	}
	if c.frozen {
		return errors.New("official egress context is frozen")
	}
	if !isKnownOfficialEgressFieldName(name) {
		return fmt.Errorf("unknown official egress field: %s", name)
	}
	if !isKnownOfficialEgressFieldSource(source) {
		return fmt.Errorf("unknown official egress field source for %s: %s", name, source)
	}
	if !isKnownOfficialEgressFieldLifecycle(lifecycle) {
		return fmt.Errorf("unknown official egress field lifecycle for %s: %s", name, lifecycle)
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return fmt.Errorf("official egress field %s is empty", name)
	}
	next := OfficialEgressField{value: value, Source: source, Lifecycle: lifecycle}
	if current, exists := c.fields[name]; exists {
		if current != next {
			return fmt.Errorf("official egress field %s has conflicting state", name)
		}
		return nil
	}
	c.fields[name] = next
	return nil
}

// Field 返回已登记字段；调用方不得把 Value 写入普通日志或审计。
func (c *OfficialEgressContext) Field(name OfficialEgressFieldName) (OfficialEgressField, bool) {
	if c == nil {
		return OfficialEgressField{}, false
	}
	field, ok := c.fields[name]
	return field, ok
}

// Freeze 返回独立的只读快照，供 WebSocket 握手和连接池生命周期使用。
func (c *OfficialEgressContext) Freeze() (*OfficialEgressContext, error) {
	if c == nil {
		return nil, errors.New("official egress context is nil")
	}
	clone := *c
	clone.fields = make(map[OfficialEgressFieldName]OfficialEgressField, len(c.fields))
	for name, field := range c.fields {
		clone.fields[name] = field
	}
	clone.codexRuntimeState = cloneOfficialCodexRuntimeState(c.codexRuntimeState)
	clone.frozen = true
	return &clone, nil
}

// OfficialEgressProfile 是 Resolver 输出的公共画像选择结果。
type OfficialEgressProfile struct {
	Enabled                   bool
	ID                        string
	Digest                    string
	Source                    string
	Version                   string
	TargetPlatform            string
	InboundEndpoint           string
	Transport                 OfficialEgressTransport
	UpstreamHost              string
	TransportProfileID        string
	CodexVersionProfileID     string
	CodexVersionProfileDigest string
	CodexEndpointProfileID    string
	ConnectionPoolID          string
}

// OfficialEgressProfileResolver 把账号配置与请求上下文解析为具体公共画像。
type OfficialEgressProfileResolver interface {
	ResolveHTTPProfile(*OfficialEgressContext, *Account, string) (OfficialEgressProfile, error)
	ResolveWebSocketProfile(*OfficialEgressContext, *Account, string) (OfficialEgressProfile, error)
}

// DefaultOfficialEgressProfileResolver 严格解析阶段 0 公共画像，不包含路径字段常量。
type DefaultOfficialEgressProfileResolver struct{}

func (DefaultOfficialEgressProfileResolver) ResolveHTTPProfile(
	egressContext *OfficialEgressContext,
	account *Account,
	endpoint string,
) (OfficialEgressProfile, error) {
	return resolveOfficialEgressProfile(egressContext, account, endpoint, OfficialEgressTransportHTTP)
}

func (DefaultOfficialEgressProfileResolver) ResolveWebSocketProfile(
	egressContext *OfficialEgressContext,
	account *Account,
	endpoint string,
) (OfficialEgressProfile, error) {
	return resolveOfficialEgressProfile(egressContext, account, endpoint, OfficialEgressTransportWebSocket)
}

func resolveOfficialEgressProfile(
	egressContext *OfficialEgressContext,
	account *Account,
	endpoint string,
	transport OfficialEgressTransport,
) (OfficialEgressProfile, error) {
	enabled, version, err := resolveOfficialEgressAccountProfile(account, egressContextProfileMode(egressContext))
	if err != nil {
		return OfficialEgressProfile{}, err
	}
	if !enabled {
		return OfficialEgressProfile{}, nil
	}
	if egressContext == nil {
		return OfficialEgressProfile{}, errors.New("official egress is enabled but context is nil")
	}
	if err := validateOfficialEgressScope(egressContext, account, endpoint, transport, version); err != nil {
		return OfficialEgressProfile{}, err
	}
	profile := OfficialEgressProfile{
		Enabled: true, Version: version, TargetPlatform: egressContext.targetPlatform,
		InboundEndpoint: egressContext.inboundEndpoint, Transport: transport,
		UpstreamHost: egressContext.upstreamHost,
	}
	if egressContext.targetPlatform == PlatformOpenAI {
		release, releaseErr := officialegress.DefaultReleaseCatalog().Resolve(
			officialegress.ReleaseMode(egressContext.profileMode),
		)
		if releaseErr != nil {
			return OfficialEgressProfile{}, releaseErr
		}
		releasePurpose := officialegress.RegistryPurposeOpenAIOAuthHTTP
		if transport == OfficialEgressTransportWebSocket {
			releasePurpose = officialegress.RegistryPurposeOpenAIOAuthWS
		}
		node, ok := release.Node(releasePurpose)
		if !ok || node.Build.Version != version {
			return OfficialEgressProfile{}, errors.New("正式 Codex release 与出站上下文不一致")
		}
		profile.ID = node.Wire.ID
		profile.Digest = node.Wire.Digest
		profile.Source = node.Wire.Source
		profile.TransportProfileID = node.Wire.TransportProfileID
	} else {
		purpose := resolveOfficialEgressClientPurpose(egressContext.targetPlatform, transport)
		resolvedClient, resolveErr := resolveOfficialClientProfile(purpose, egressContext.profileMode)
		if resolveErr != nil {
			return OfficialEgressProfile{}, resolveErr
		}
		if resolvedClient.Build.Version != version {
			return OfficialEgressProfile{}, errors.New("official egress build version conflicts with resolved client profile")
		}
		profile.ID = resolvedClient.Wire.ID
		profile.Digest = resolvedClient.Wire.Digest
		profile.Source = resolvedClient.Wire.Source
		profile.TransportProfileID = resolvedClient.Wire.TransportProfileID
	}
	if egressContext.targetPlatform == PlatformOpenAI {
		versionProfile, profileErr := resolveCodexVersionProfile(version)
		if profileErr != nil {
			return OfficialEgressProfile{}, profileErr
		}
		endpointID, profileErr := resolveOfficialCodexEndpointIDForContext(egressContext)
		if profileErr != nil {
			return OfficialEgressProfile{}, profileErr
		}
		endpointProfile, profileErr := versionProfile.ResolveEndpoint(endpointID)
		if profileErr != nil {
			return OfficialEgressProfile{}, profileErr
		}
		profile.CodexVersionProfileID = "codex-cli-" + versionProfile.Version
		profile.CodexVersionProfileDigest = versionProfile.Digest
		profile.CodexEndpointProfileID = endpointProfile.ID
		profile.TransportProfileID = endpointProfile.TransportID
	}
	if profile.TransportProfileID == "" {
		return OfficialEgressProfile{}, errors.New("official egress transport profile is unavailable")
	}
	profile.ConnectionPoolID = buildOfficialEgressConnectionPoolID(egressContext, profile)
	egressContext.transportProfileID = profile.TransportProfileID
	egressContext.clientProfileID = profile.ID
	egressContext.clientProfileDigest = profile.Digest
	egressContext.codexVersionProfileID = profile.CodexVersionProfileID
	egressContext.codexVersionProfileDigest = profile.CodexVersionProfileDigest
	egressContext.codexEndpointProfileID = profile.CodexEndpointProfileID
	egressContext.connectionPoolID = profile.ConnectionPoolID
	return profile, nil
}

// resolveOfficialCodexEndpointIDForContext 是公共 Resolver 到版本画像的唯一映射点。
// 上游路径、header 与传输层不得再次按入口散落判断；未知入口在这里明确失败。
func resolveOfficialCodexEndpointIDForContext(egressContext *OfficialEgressContext) (string, error) {
	if egressContext == nil || egressContext.targetPlatform != PlatformOpenAI {
		return "", errors.New("Codex 端点画像需要 OpenAI 官方出站上下文")
	}
	if endpointID := strings.TrimSpace(egressContext.codexRequestedEndpointID); endpointID != "" {
		endpoint, err := resolveCodexEndpoint(
			egressContext.profileVersion,
			codexEndpointID(endpointID),
		)
		if err != nil {
			return "", err
		}
		expectedTransport := OfficialEgressTransportHTTP
		if endpoint.Upgrade == "websocket" {
			expectedTransport = OfficialEgressTransportWebSocket
		}
		if egressContext.transport != expectedTransport {
			return "", fmt.Errorf("Codex 端点 %s 的传输与上下文冲突", endpoint.ID)
		}
		if !officialCodexHostMatches(endpoint.Host, egressContext.upstreamHost) {
			return "", fmt.Errorf("Codex 端点 %s 的 host 与上下文冲突", endpoint.ID)
		}
		return endpoint.ID, nil
	}
	if egressContext.transport == OfficialEgressTransportWebSocket {
		if egressContext.inboundEndpoint != "/v1/responses" {
			return "", fmt.Errorf("Codex WebSocket 不支持入口：%s", egressContext.inboundEndpoint)
		}
		return officialCodexEndpointResponsesWS, nil
	}
	if egressContext.transport != OfficialEgressTransportHTTP {
		return "", fmt.Errorf("Codex 不支持传输：%s", egressContext.transport)
	}
	if egressContext.inboundEndpoint == "/v1/responses/compact" {
		return officialCodexEndpointResponsesCompact, nil
	}
	if supportsOfficialEgressHTTPProfile(PlatformOpenAI, egressContext.inboundEndpoint) {
		return officialCodexEndpointResponsesHTTP, nil
	}
	return "", fmt.Errorf("Codex HTTP 不支持入口：%s", egressContext.inboundEndpoint)
}

func validateOfficialEgressScope(
	egressContext *OfficialEgressContext,
	account *Account,
	endpoint string,
	transport OfficialEgressTransport,
	version string,
) error {
	if account == nil {
		return errors.New("official egress is enabled but account is nil")
	}
	if account.ID <= 0 || egressContext.accountID != account.ID {
		return errors.New("official egress account state conflicts with request context")
	}
	if egressContext.targetPlatform != strings.ToLower(strings.TrimSpace(account.Platform)) {
		return errors.New("official egress target platform conflicts with account platform")
	}
	if egressContext.accountType != strings.ToLower(strings.TrimSpace(account.Type)) {
		return errors.New("official egress account type conflicts with request context")
	}
	if egressContext.transport != transport {
		return errors.New("official egress transport conflicts with resolver")
	}
	if egressContext.profileVersion != version {
		return errors.New("official egress profile version conflicts with resolved baseline")
	}
	normalizedEndpoint := canonicalOfficialEgressInboundEndpoint(endpoint)
	if normalizedEndpoint == "" || normalizedEndpoint != egressContext.inboundEndpoint {
		return errors.New("official egress endpoint conflicts with request context")
	}

	switch egressContext.targetPlatform {
	case PlatformAnthropic:
		if !account.IsOAuth() {
			return errors.New("official egress only supports Anthropic OAuth or SetupToken accounts")
		}
		if transport != OfficialEgressTransportHTTP ||
			!supportsOfficialEgressHTTPProfile(PlatformAnthropic, normalizedEndpoint) {
			return errors.New("official egress Anthropic HTTP profile does not support this model endpoint")
		}
		if egressContext.upstreamHost != "api.anthropic.com" {
			return fmt.Errorf("official egress rejected Anthropic upstream host: %s", egressContext.upstreamHost)
		}
	case PlatformOpenAI:
		if account.Type != AccountTypeOAuth {
			return errors.New("official egress only supports OpenAI OAuth accounts")
		}
		if strings.TrimSpace(egressContext.codexRequestedEndpointID) != "" {
			endpointProfile, endpointErr := resolveCodexEndpoint(
				egressContext.profileVersion,
				codexEndpointID(egressContext.codexRequestedEndpointID),
			)
			if endpointErr != nil {
				return endpointErr
			}
			if !officialCodexHostMatches(endpointProfile.Host, egressContext.upstreamHost) {
				return fmt.Errorf("official egress rejected Codex endpoint host: %s", egressContext.upstreamHost)
			}
			expectedTransport := OfficialEgressTransportHTTP
			if endpointProfile.Upgrade == "websocket" {
				expectedTransport = OfficialEgressTransportWebSocket
			}
			if transport != expectedTransport {
				return fmt.Errorf("official egress rejected Codex endpoint transport: %s", transport)
			}
			break
		}
		if egressContext.upstreamHost != "chatgpt.com" {
			return fmt.Errorf("official egress rejected OpenAI upstream host: %s", egressContext.upstreamHost)
		}
		if transport == OfficialEgressTransportHTTP {
			if !supportsOfficialEgressHTTPProfile(PlatformOpenAI, normalizedEndpoint) {
				return errors.New("official egress OpenAI HTTP profile does not support this model endpoint")
			}
		} else if normalizedEndpoint != "/v1/responses" {
			return errors.New("official egress OpenAI WebSocket profile only supports /v1/responses")
		}
	default:
		return fmt.Errorf("official egress does not support platform: %s", egressContext.targetPlatform)
	}
	return nil
}

// ValidateOfficialEgressFinalState 执行最终公共校验。路径 Finalizer 在发送前必须调用。
func ValidateOfficialEgressFinalState(egressContext *OfficialEgressContext, profile OfficialEgressProfile) error {
	if !profile.Enabled {
		return nil
	}
	if egressContext == nil {
		return errors.New("official egress final validation requires context")
	}
	if profile.Version != egressContext.profileVersion ||
		profile.ID != egressContext.clientProfileID ||
		profile.Digest != egressContext.clientProfileDigest ||
		profile.TargetPlatform != egressContext.targetPlatform ||
		profile.InboundEndpoint != egressContext.inboundEndpoint ||
		profile.Transport != egressContext.transport ||
		profile.UpstreamHost != egressContext.upstreamHost ||
		profile.TransportProfileID != egressContext.transportProfileID ||
		profile.CodexVersionProfileID != egressContext.codexVersionProfileID ||
		profile.CodexVersionProfileDigest != egressContext.codexVersionProfileDigest ||
		profile.CodexEndpointProfileID != egressContext.codexEndpointProfileID ||
		profile.ConnectionPoolID != egressContext.connectionPoolID {
		return errors.New("official egress final state conflicts with resolved profile")
	}
	if strings.TrimSpace(profile.ID) == "" || strings.TrimSpace(profile.Digest) == "" ||
		strings.TrimSpace(profile.TransportProfileID) == "" ||
		strings.TrimSpace(profile.ConnectionPoolID) == "" {
		return errors.New("official egress transport profile is incomplete")
	}
	if profile.TargetPlatform == PlatformOpenAI &&
		(strings.TrimSpace(profile.CodexVersionProfileID) == "" ||
			strings.TrimSpace(profile.CodexVersionProfileDigest) == "" ||
			strings.TrimSpace(profile.CodexEndpointProfileID) == "") {
		return errors.New("Codex 完整版本画像未绑定")
	}
	if profile.TargetPlatform == PlatformOpenAI {
		if err := validateOfficialCodexRuntimeState(egressContext.codexRuntimeState); err != nil {
			return err
		}
		if egressContext.codexRuntimeState.ProcessPhase == officialCodexProcessPhaseInitialModels &&
			egressContext.codexEndpointProfileID != officialCodexEndpointModels {
			return errors.New("Codex initial models 进程阶段只能绑定 models 端点")
		}
	}
	if profile.Transport == OfficialEgressTransportWebSocket && !egressContext.frozen {
		return errors.New("official egress WebSocket context must be frozen before dialing")
	}
	for name, field := range egressContext.fields {
		if strings.TrimSpace(field.value) == "" ||
			!isKnownOfficialEgressFieldSource(field.Source) ||
			!isKnownOfficialEgressFieldLifecycle(field.Lifecycle) {
			return fmt.Errorf("official egress field %s has invalid provenance", name)
		}
	}
	return nil
}

// OfficialEgressModification 描述 Finalizer 实际修改的 Header、Body、帧或传输项。
type OfficialEgressModification struct {
	Kind  string
	Field string
}

// OfficialEgressFinalizationResult 让测试可以逐项断言修改与最终校验结果。
type OfficialEgressFinalizationResult struct {
	Modifications []OfficialEgressModification
}

type officialEgressContextKey struct{}

// WithOfficialEgressContext 把已解析的公共上下文绑定到当前请求或连接。
func WithOfficialEgressContext(ctx context.Context, egressContext *OfficialEgressContext) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if egressContext == nil {
		return ctx
	}
	return context.WithValue(ctx, officialEgressContextKey{}, egressContext)
}

// OfficialEgressContextFromContext 读取当前请求或连接绑定的公共上下文。
func OfficialEgressContextFromContext(ctx context.Context) (*OfficialEgressContext, bool) {
	if ctx == nil {
		return nil, false
	}
	egressContext, ok := ctx.Value(officialEgressContextKey{}).(*OfficialEgressContext)
	return egressContext, ok && egressContext != nil
}

// OfficialEgressConnectionPoolIDFromContext 返回已经过严格解析的稳定连接池键。
func OfficialEgressConnectionPoolIDFromContext(ctx context.Context) (string, bool) {
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	if !ok {
		return "", false
	}
	poolID := strings.TrimSpace(egressContext.connectionPoolID)
	return poolID, poolID != ""
}

// UsesOfficialEgressProfile 返回账号是否必须使用内置官方出站画像。
//
// 该能力是 Anthropic/OpenAI OAuth 官方出站的固定组成部分，不读取账号 Extra，
// 也不允许由单个账号关闭。Anthropic SetupToken 与 OAuth 使用相同官方链路。
func (a *Account) UsesOfficialEgressProfile() bool {
	if a == nil {
		return false
	}
	return usesBuiltInOfficialEgressProfile(a.Platform, a.Type)
}

func usesBuiltInOfficialEgressProfile(platform, accountType string) bool {
	accountType = strings.ToLower(strings.TrimSpace(accountType))
	switch strings.ToLower(strings.TrimSpace(platform)) {
	case PlatformAnthropic:
		return accountType == AccountTypeOAuth || accountType == AccountTypeSetupToken
	case PlatformOpenAI:
		return accountType == AccountTypeOAuth
	default:
		return false
	}
}

func resolveOfficialEgressAccountProfile(account *Account, modes ...string) (bool, string, error) {
	if account == nil {
		return false, "", errors.New("account is nil")
	}
	if !account.UsesOfficialEgressProfile() {
		return false, "", nil
	}
	mode := officialClientProfileModeActive
	if len(modes) > 0 {
		mode = normalizeOfficialClientProfileMode(modes[0])
	}
	purpose := ""
	switch account.Platform {
	case PlatformAnthropic:
		purpose = officialClientPurposeAnthropicOAuthMessagesHTTP
	case PlatformOpenAI:
		release, releaseErr := officialegress.DefaultReleaseCatalog().Resolve(
			officialegress.ReleaseMode(mode),
		)
		if releaseErr != nil {
			return false, "", releaseErr
		}
		return true, release.Version(), nil
	}
	profile, err := resolveOfficialClientProfile(purpose, mode)
	if err != nil {
		return false, "", err
	}
	return true, profile.Build.Version, nil
}

// NormalizeBuiltInOfficialEgressExtra 仅清理已废弃的账号级画像键。
//
// 官方出站画像已经按账号平台与认证类型自动生效。管理端即使收到旧版客户端
// 提交的开关或版本字段，也不得继续保存。TLS、会话、缓存和自定义地址配置
// 必须原样保留为休眠配置，避免用户切换账号类型后丢失原设置。
func NormalizeBuiltInOfficialEgressExtra(platform, accountType string, extra map[string]any) (map[string]any, error) {
	if extra == nil {
		return nil, nil
	}
	normalized := make(map[string]any, len(extra))
	for key, value := range extra {
		normalized[key] = value
	}

	delete(normalized, "official_egress_enabled")
	delete(normalized, "official_egress_profile_version")

	return normalized, nil
}

// ValidateBuiltInOfficialEgressExtraTransition 拒绝新开启与内置 OAuth 画像冲突的
// 配置，但允许历史值继续休眠，也允许用户关闭这些配置。
func ValidateBuiltInOfficialEgressExtraTransition(
	platform, accountType string,
	current, incoming map[string]any,
) error {
	if !usesBuiltInOfficialEgressProfile(platform, accountType) || incoming == nil {
		return nil
	}
	conflicts := []struct {
		Key  string
		Code string
	}{
		{Key: "custom_base_url_enabled", Code: "OFFICIAL_EGRESS_CUSTOM_BASE_URL_CONFLICT"},
		{Key: "enable_tls_fingerprint", Code: "OFFICIAL_EGRESS_TLS_FINGERPRINT_CONFLICT"},
		{Key: "session_id_masking_enabled", Code: "OFFICIAL_EGRESS_SESSION_MASKING_CONFLICT"},
		{Key: "cache_ttl_override_enabled", Code: "OFFICIAL_EGRESS_CACHE_TTL_CONFLICT"},
	}
	for _, conflict := range conflicts {
		incomingEnabled, _ := incoming[conflict.Key].(bool)
		currentEnabled, _ := current[conflict.Key].(bool)
		if incomingEnabled && !currentEnabled {
			return infraerrors.BadRequest(
				conflict.Code,
				"内置官方出站画像生效时不能新开启配置 "+conflict.Key,
			)
		}
	}
	return nil
}

func buildOfficialEgressConnectionPoolID(
	egressContext *OfficialEgressContext,
	profile OfficialEgressProfile,
) string {
	caState := "system"
	if egressContext.caFingerprint != "" {
		caState = egressContext.caFingerprint
	}
	invocation := strings.TrimSpace(egressContext.invocationID)
	endpointPoolKey := profile.CodexEndpointProfileID
	if egressContext.targetPlatform == PlatformOpenAI && profile.CodexEndpointProfileID != "" {
		endpoint, err := resolveCodexEndpoint(
			egressContext.profileVersion,
			codexEndpointID(profile.CodexEndpointProfileID),
		)
		if err == nil && endpoint.ClientLifecycle == officialCodexClientBackendLongLived {
			// WHAM 的三个端点由同一个 backend-client 常驻 Client 发出。连接池键
			// 必须跨上层调用、跨 WHAM path 保持稳定；TLS/H1 画像本身已编译同一
			// transport 的完整 method/path 矩阵，因此无需为每个 path 拆分 Client。
			invocation = officialCodexClientBackendLongLived
			endpointPoolKey = officialCodexClientBackendLongLived
		}
	}
	if profile.Transport == OfficialEgressTransportHTTP && invocation == "" {
		// HTTP 官方画像必须在写出前由接入层补齐调用级生命周期。保留显式占位
		// 只为让旧测试能构造上下文；生产校验会拒绝该值。
		invocation = "missing"
	}
	return fmt.Sprintf(
		"account=%d|profile=%s|digest=%s|codex_profile=%s|codex_digest=%s|codex_endpoint=%s|transport=%s|host=%s|proxy=%d|ca=%s|tls_profile=%s|invocation=%s",
		egressContext.accountID,
		profile.ID,
		profile.Digest,
		profile.CodexVersionProfileID,
		profile.CodexVersionProfileDigest,
		endpointPoolKey,
		profile.Transport,
		profile.UpstreamHost,
		egressContext.proxyID,
		caState,
		profile.TransportProfileID,
		invocation,
	)
}

// officialEgressWebSocketIdentityKey 返回脱敏的连接身份键。
// 官方 WS 的握手身份在连接生命周期内不可变，因此不同会话不能只因账号与
// TLS Profile 相同就复用同一条上游连接。
func officialEgressWebSocketIdentityKey(
	egressContext *OfficialEgressContext,
) string {
	if egressContext == nil ||
		egressContext.transport != OfficialEgressTransportWebSocket {
		return ""
	}
	field, exists := egressContext.Field(OfficialEgressFieldSessionID)
	if !exists || strings.TrimSpace(field.value) == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(field.value))
	return fmt.Sprintf("%x", sum[:12])
}

func resolveOfficialEgressClientPurpose(targetPlatform string, transport OfficialEgressTransport) string {
	switch {
	case targetPlatform == PlatformAnthropic && transport == OfficialEgressTransportHTTP:
		return officialClientPurposeAnthropicOAuthMessagesHTTP
	case targetPlatform == PlatformOpenAI && transport == OfficialEgressTransportHTTP:
		return officialClientPurposeOpenAIOAuthResponsesHTTP
	case targetPlatform == PlatformOpenAI && transport == OfficialEgressTransportWebSocket:
		return officialClientPurposeOpenAIOAuthResponsesWS
	default:
		return ""
	}
}

func egressContextProfileMode(egressContext *OfficialEgressContext) string {
	if egressContext == nil {
		return officialClientProfileModeActive
	}
	return egressContext.profileMode
}

func officialEgressRedactedLogAttributes(
	egressContext *OfficialEgressContext,
	profile OfficialEgressProfile,
) []any {
	if egressContext == nil {
		return []any{"enabled", profile.Enabled}
	}
	fieldNames := make([]string, 0, len(egressContext.fields))
	fieldProvenance := make([]string, 0, len(egressContext.fields))
	for name, field := range egressContext.fields {
		fieldName := string(name)
		fieldNames = append(fieldNames, fieldName)
		fieldProvenance = append(
			fieldProvenance,
			fmt.Sprintf("%s:%s/%s", fieldName, field.Source, field.Lifecycle),
		)
	}
	sort.Strings(fieldNames)
	sort.Strings(fieldProvenance)
	return []any{
		"enabled", profile.Enabled,
		"account_id", egressContext.accountID,
		"platform", egressContext.targetPlatform,
		"endpoint", egressContext.inboundEndpoint,
		"transport", egressContext.transport,
		"upstream_host", egressContext.upstreamHost,
		"profile_version", profile.Version,
		"profile_id", profile.ID,
		"profile_digest", profile.Digest,
		"profile_source", profile.Source,
		"transport_profile", profile.TransportProfileID,
		"codex_version_profile", profile.CodexVersionProfileID,
		"codex_version_digest", profile.CodexVersionProfileDigest,
		"codex_endpoint_profile", profile.CodexEndpointProfileID,
		"proxy_id", egressContext.proxyID,
		"custom_ca", egressContext.caFingerprint != "",
		"field_names", fieldNames,
		"field_provenance", fieldProvenance,
		"field_count", len(fieldNames),
		"frozen", egressContext.frozen,
	}
}

func logOfficialEgressProfileResolved(egressContext *OfficialEgressContext, profile OfficialEgressProfile) {
	if !profile.Enabled {
		return
	}
	slog.Info("official_egress_profile_resolved", officialEgressRedactedLogAttributes(egressContext, profile)...)
}

func normalizeOfficialEgressHost(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if parsed, err := url.Parse(raw); err == nil && parsed.Hostname() != "" {
		return strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
	}
	if host, _, err := net.SplitHostPort(raw); err == nil {
		return strings.ToLower(strings.TrimSuffix(host, "."))
	}
	return strings.ToLower(strings.TrimSuffix(raw, "."))
}

func normalizeOfficialEgressEndpoint(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if parsed, err := url.Parse(raw); err == nil {
		if parsed.Path != "" {
			raw = parsed.Path
		}
	}
	raw = path.Clean("/" + strings.TrimPrefix(raw, "/"))
	if raw == "." {
		return "/"
	}
	return raw
}

// canonicalOfficialEgressInboundEndpoint 先完成 URL/path 清理，再复用网关入口别名规则。
// 上游目标路径校验仍使用 normalizeOfficialEgressEndpoint，不能把两类路径混为一谈。
func canonicalOfficialEgressInboundEndpoint(raw string) string {
	return gatewayendpoint.NormalizeInboundEndpoint(normalizeOfficialEgressEndpoint(raw))
}

func isKnownOfficialEgressFieldName(name OfficialEgressFieldName) bool {
	switch name {
	case OfficialEgressFieldSessionID,
		OfficialEgressFieldDeviceID,
		OfficialEgressFieldAccountUUID,
		OfficialEgressFieldClientRequestID,
		OfficialEgressFieldThreadID,
		OfficialEgressFieldWindowID,
		OfficialEgressFieldTurnMetadata,
		OfficialEgressFieldTurnState,
		OfficialEgressFieldPromptCacheKey,
		OfficialEgressFieldPreviousResponseID,
		OfficialEgressFieldCallID:
		return true
	default:
		return false
	}
}

func isKnownOfficialEgressFieldSource(source OfficialEgressFieldSource) bool {
	switch source {
	case OfficialEgressFieldSourceIngressExplicit,
		OfficialEgressFieldSourceRedisSession,
		OfficialEgressFieldSourceAccountStatic,
		OfficialEgressFieldSourceDerived:
		return true
	default:
		return false
	}
}

func isKnownOfficialEgressFieldLifecycle(lifecycle OfficialEgressFieldLifecycle) bool {
	switch lifecycle {
	case OfficialEgressFieldLifecycleRequest,
		OfficialEgressFieldLifecycleSession,
		OfficialEgressFieldLifecycleConnection,
		OfficialEgressFieldLifecycleTurn:
		return true
	default:
		return false
	}
}
