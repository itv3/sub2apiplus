package service

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"path"
	"sort"
	"strings"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const (
	// OfficialEgressProfileVersionPhase0 固定到 2026-07-24 的阶段 0 抓包基线。
	OfficialEgressProfileVersionPhase0 = "phase0-2026-07-24"

	officialEgressTransportProfileAnthropicHTTP = "anthropic-http-phase0-2026-07-24"
	officialEgressTransportProfileOpenAIHTTP    = "openai-http-phase0-2026-07-24"
	officialEgressTransportProfileOpenAIWS      = "openai-ws-phase0-2026-07-24"
)

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
	OfficialEgressFieldSourceResponseMapping OfficialEgressFieldSource = "response_mapping"
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
	OfficialEgressFieldLifecycleResponse   OfficialEgressFieldLifecycle = "response"
)

// OfficialEgressFieldName 是公共上下文允许登记的动态身份字段。
type OfficialEgressFieldName string

const (
	OfficialEgressFieldSessionID          OfficialEgressFieldName = "session_id"
	OfficialEgressFieldDeviceID           OfficialEgressFieldName = "device_id"
	OfficialEgressFieldAccountUUID        OfficialEgressFieldName = "account_uuid"
	OfficialEgressFieldClientRequestID    OfficialEgressFieldName = "client_request_id"
	OfficialEgressFieldPreviousRequestID  OfficialEgressFieldName = "previous_request_id"
	OfficialEgressFieldThreadID           OfficialEgressFieldName = "thread_id"
	OfficialEgressFieldWindowID           OfficialEgressFieldName = "window_id"
	OfficialEgressFieldTurnMetadata       OfficialEgressFieldName = "turn_metadata"
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

// OfficialEgressContextInput 是每次选定账号后构造公共上下文所需的稳定输入。
type OfficialEgressContextInput struct {
	AccountID       int64
	TargetPlatform  string
	InboundEndpoint string
	Transport       OfficialEgressTransport
	UpstreamHost    string
	ProfileVersion  string
	AccountType     string
	ProxyID         int64
	CAFingerprint   string
}

// OfficialEgressContext 保存最终出站画像所需的公共状态。
//
// 核心路由字段创建后不可修改；动态字段只能通过 RegisterField 登记。WebSocket
// 握手前必须 Freeze，冻结后的上下文不会接受任何身份字段变化。
type OfficialEgressContext struct {
	accountID          int64
	targetPlatform     string
	inboundEndpoint    string
	transport          OfficialEgressTransport
	upstreamHost       string
	profileVersion     string
	accountType        string
	proxyID            int64
	caFingerprint      string
	transportProfileID string
	connectionPoolID   string
	fields             map[OfficialEgressFieldName]OfficialEgressField
	openAIWSDerived    *officialOpenAIWSDerivedState
	frozen             bool
}

// NewOfficialEgressContext 创建一份请求级公共上下文，不执行路径画像修正。
func NewOfficialEgressContext(input OfficialEgressContextInput) *OfficialEgressContext {
	return &OfficialEgressContext{
		accountID:       input.AccountID,
		targetPlatform:  strings.ToLower(strings.TrimSpace(input.TargetPlatform)),
		inboundEndpoint: normalizeOfficialEgressEndpoint(input.InboundEndpoint),
		transport:       input.Transport,
		upstreamHost:    normalizeOfficialEgressHost(input.UpstreamHost),
		profileVersion:  strings.TrimSpace(input.ProfileVersion),
		accountType:     strings.ToLower(strings.TrimSpace(input.AccountType)),
		proxyID:         input.ProxyID,
		caFingerprint:   strings.TrimSpace(input.CAFingerprint),
		fields:          make(map[OfficialEgressFieldName]OfficialEgressField),
	}
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
	clone.frozen = true
	return &clone, nil
}

// OfficialEgressProfile 是 Resolver 输出的公共画像选择结果。
type OfficialEgressProfile struct {
	Enabled            bool
	Version            string
	TargetPlatform     string
	InboundEndpoint    string
	Transport          OfficialEgressTransport
	UpstreamHost       string
	TransportProfileID string
	ConnectionPoolID   string
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
	enabled, version, err := resolveOfficialEgressAccountProfile(account)
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
		Enabled:         true,
		Version:         version,
		TargetPlatform:  egressContext.targetPlatform,
		InboundEndpoint: egressContext.inboundEndpoint,
		Transport:       transport,
		UpstreamHost:    egressContext.upstreamHost,
		TransportProfileID: resolveOfficialEgressTransportProfileID(
			egressContext.targetPlatform,
			transport,
		),
	}
	if profile.TransportProfileID == "" {
		return OfficialEgressProfile{}, errors.New("official egress transport profile is unavailable")
	}
	profile.ConnectionPoolID = buildOfficialEgressConnectionPoolID(egressContext, profile)
	egressContext.transportProfileID = profile.TransportProfileID
	egressContext.connectionPoolID = profile.ConnectionPoolID
	return profile, nil
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
	normalizedEndpoint := normalizeOfficialEgressEndpoint(endpoint)
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
		profile.TargetPlatform != egressContext.targetPlatform ||
		profile.InboundEndpoint != egressContext.inboundEndpoint ||
		profile.Transport != egressContext.transport ||
		profile.UpstreamHost != egressContext.upstreamHost ||
		profile.TransportProfileID != egressContext.transportProfileID ||
		profile.ConnectionPoolID != egressContext.connectionPoolID {
		return errors.New("official egress final state conflicts with resolved profile")
	}
	if strings.TrimSpace(profile.TransportProfileID) == "" ||
		strings.TrimSpace(profile.ConnectionPoolID) == "" {
		return errors.New("official egress transport profile is incomplete")
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

func resolveOfficialEgressAccountProfile(account *Account) (bool, string, error) {
	if account == nil {
		return false, "", errors.New("account is nil")
	}
	if !account.UsesOfficialEgressProfile() {
		return false, "", nil
	}
	return true, OfficialEgressProfileVersionPhase0, nil
}

// NormalizeBuiltInOfficialEgressExtra 清理已废弃的账号级画像键和冲突配置。
//
// 官方出站画像已经按账号平台与认证类型自动生效。管理端即使收到旧版客户端
// 提交的开关或版本字段，也不得继续保存。符合条件的账号同时清理过去由独立
// TLS、会话和缓存开关控制的重复画像字段。
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

	if !usesBuiltInOfficialEgressProfile(platform, accountType) {
		return normalized, nil
	}
	if customBaseURLEnabled, _ := normalized["custom_base_url_enabled"].(bool); customBaseURLEnabled {
		return nil, infraerrors.BadRequest(
			"OFFICIAL_EGRESS_CUSTOM_BASE_URL_CONFLICT",
			"built-in official egress cannot be used with custom_base_url",
		)
	}

	// 内置官方画像统一接管应用字段、会话身份和传输画像。
	for _, key := range []string{
		"enable_tls_fingerprint",
		"tls_fingerprint_profile_id",
		"session_id_masking_enabled",
		"cache_ttl_override_enabled",
		"cache_ttl_override_target",
	} {
		delete(normalized, key)
	}
	return normalized, nil
}

func buildOfficialEgressConnectionPoolID(
	egressContext *OfficialEgressContext,
	profile OfficialEgressProfile,
) string {
	caState := "system"
	if egressContext.caFingerprint != "" {
		caState = egressContext.caFingerprint
	}
	return fmt.Sprintf(
		"account=%d|profile=%s|transport=%s|host=%s|proxy=%d|ca=%s|tls_profile=%s",
		egressContext.accountID,
		profile.Version,
		profile.Transport,
		profile.UpstreamHost,
		egressContext.proxyID,
		caState,
		profile.TransportProfileID,
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

func resolveOfficialEgressTransportProfileID(
	targetPlatform string,
	transport OfficialEgressTransport,
) string {
	switch {
	case targetPlatform == PlatformAnthropic && transport == OfficialEgressTransportHTTP:
		return officialEgressTransportProfileAnthropicHTTP
	case targetPlatform == PlatformOpenAI && transport == OfficialEgressTransportHTTP:
		return officialEgressTransportProfileOpenAIHTTP
	case targetPlatform == PlatformOpenAI && transport == OfficialEgressTransportWebSocket:
		return officialEgressTransportProfileOpenAIWS
	default:
		return ""
	}
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
		"transport_profile", profile.TransportProfileID,
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

func isKnownOfficialEgressFieldName(name OfficialEgressFieldName) bool {
	switch name {
	case OfficialEgressFieldSessionID,
		OfficialEgressFieldDeviceID,
		OfficialEgressFieldAccountUUID,
		OfficialEgressFieldClientRequestID,
		OfficialEgressFieldPreviousRequestID,
		OfficialEgressFieldThreadID,
		OfficialEgressFieldWindowID,
		OfficialEgressFieldTurnMetadata,
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
		OfficialEgressFieldSourceResponseMapping,
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
		OfficialEgressFieldLifecycleTurn,
		OfficialEgressFieldLifecycleResponse:
		return true
	default:
		return false
	}
}
