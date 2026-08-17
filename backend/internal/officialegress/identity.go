package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"sync"
)

// IdentityFactSource 只描述已经在 service 边界完成验证的事实来源。
// officialegress 不依赖 service.Account、repository 或请求框架类型。
type IdentityFactSource string

const (
	IdentitySourceAccount       IdentityFactSource = "account_projection"
	IdentitySourceIngress       IdentityFactSource = "validated_ingress"
	IdentitySourceProcess       IdentityFactSource = "process_snapshot"
	IdentitySourceManagedConfig IdentityFactSource = "managed_configuration"
	IdentitySourceInvocation    IdentityFactSource = "invocation"
	IdentitySourceTurn          IdentityFactSource = "turn"
)

func (s IdentityFactSource) Valid() bool {
	switch s {
	case IdentitySourceAccount, IdentitySourceIngress, IdentitySourceProcess,
		IdentitySourceManagedConfig, IdentitySourceInvocation, IdentitySourceTurn:
		return true
	default:
		return false
	}
}

// IdentityFactLifecycle 明确身份事实允许存活的最长边界。
type IdentityFactLifecycle string

const (
	IdentityLifecycleProcess    IdentityFactLifecycle = "process"
	IdentityLifecycleAccount    IdentityFactLifecycle = "account_selection"
	IdentityLifecycleInvocation IdentityFactLifecycle = "invocation"
	IdentityLifecycleSession    IdentityFactLifecycle = "session"
	IdentityLifecycleTurn       IdentityFactLifecycle = "turn"
)

func (l IdentityFactLifecycle) Valid() bool {
	switch l {
	case IdentityLifecycleProcess, IdentityLifecycleAccount, IdentityLifecycleInvocation,
		IdentityLifecycleSession, IdentityLifecycleTurn:
		return true
	default:
		return false
	}
}

// CodexIdentityValue 是一个已经验证的不可变标量。Value 不能承载凭据；Bearer、
// refresh token、API Key、Cookie 和一次性签名只能进入 AttemptAuthentication。
type CodexIdentityValue struct {
	Value     string
	Source    IdentityFactSource
	Lifecycle IdentityFactLifecycle
}

func NewCodexIdentityValue(
	value string,
	source IdentityFactSource,
	lifecycle IdentityFactLifecycle,
) (CodexIdentityValue, error) {
	fact := CodexIdentityValue{
		Value: strings.TrimSpace(value), Source: source, Lifecycle: lifecycle,
	}
	if err := fact.validate(); err != nil {
		return CodexIdentityValue{}, err
	}
	return fact, nil
}

// NewCodexTurnStateValue 构造上游签发的 opaque turn-state。turn-state 不是认证
// 材料，但其不透明内容可能偶然包含 sk- 等凭据特征，因此只能在专用字段中跳过
// 内容特征扫描；来源、生命周期和空值规则仍按身份事实校验。
func NewCodexTurnStateValue(value string) (CodexIdentityValue, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return CodexIdentityValue{}, nil
	}
	fact := CodexIdentityValue{
		Value: value, Source: IdentitySourceTurn, Lifecycle: IdentityLifecycleTurn,
	}
	if err := fact.validateTurnState(); err != nil {
		return CodexIdentityValue{}, err
	}
	return fact, nil
}

func (v CodexIdentityValue) validate() error {
	if err := v.validateShape(); err != nil {
		return err
	}
	if !v.present() {
		return nil
	}
	lower := strings.ToLower(strings.TrimSpace(v.Value))
	for _, forbidden := range []string{"bearer ", "sk-", "refresh_token", "api_key="} {
		if strings.Contains(lower, forbidden) {
			return errors.New("身份事实疑似包含认证材料")
		}
	}
	return nil
}

func (v CodexIdentityValue) validateShape() error {
	if strings.TrimSpace(v.Value) == "" {
		if v.Source == "" && v.Lifecycle == "" {
			return nil
		}
		return errors.New("空身份事实不得声明来源或生命周期")
	}
	if !v.Source.Valid() || !v.Lifecycle.Valid() {
		return errors.New("身份事实来源或生命周期非法")
	}
	return nil
}

func (v CodexIdentityValue) validateTurnState() error {
	if err := v.validateShape(); err != nil {
		return err
	}
	if !v.present() {
		return nil
	}
	if v.Source != IdentitySourceTurn || v.Lifecycle != IdentityLifecycleTurn {
		return errors.New("turn-state 身份事实来源或生命周期非法")
	}
	return nil
}

func (v CodexIdentityValue) present() bool { return strings.TrimSpace(v.Value) != "" }

// CodexRequestConditions 只承载可信运行条件，不允许调用方直接选择 Profile feature。
// compiler 会把这些条件与 ReleaseBundle/ProfileSpec 的 feature 默认值求交集。
type CodexRequestConditions struct {
	TurnStatePresent        bool
	SubagentPresent         bool
	MemoryGeneration        bool
	ParentThreadPresent     bool
	SessionIDPresent        bool
	AttestationPresent      bool
	FedRAMPAccount          bool
	ManagedResidencyPresent bool
	CookiePresent           bool
	CompressionEligible     bool
	ModelSupportsLite       bool
	BetaFeaturesPresent     bool
}

// CodexIdentityFacts 是 invocation 级不可变身份投影。它没有 map、service.Account、
// repository 类型或任何认证材料；所有字段在构造后只通过值拷贝进入 Plan。
type CodexIdentityFacts struct {
	AccountIdentityProjection  CodexIdentityValue
	ChatGPTAccountID           CodexIdentityValue
	WorkspaceID                CodexIdentityValue
	ProcessSurface             CodexIdentityValue
	ProcessPhase               CodexIdentityValue
	TerminalToken              CodexIdentityValue
	UserAgentSuffixEnabled     bool
	InstallationID             CodexIdentityValue
	SessionID                  CodexIdentityValue
	ConversationID             CodexIdentityValue
	ThreadID                   CodexIdentityValue
	WindowID                   CodexIdentityValue
	ClientRequestID            CodexIdentityValue
	TurnID                     CodexIdentityValue
	TurnMetadata               CodexIdentityValue
	TurnState                  CodexIdentityValue
	ParentThreadID             CodexIdentityValue
	Subagent                   CodexIdentityValue
	ManagedResidency           CodexIdentityValue
	ManagedConfigurationDigest string
	Conditions                 CodexRequestConditions
}

func (f CodexIdentityFacts) Validate() error {
	values := []CodexIdentityValue{
		f.AccountIdentityProjection, f.ChatGPTAccountID, f.WorkspaceID,
		f.ProcessSurface, f.ProcessPhase, f.TerminalToken, f.InstallationID, f.SessionID,
		f.ConversationID, f.ThreadID, f.WindowID, f.ClientRequestID,
		f.TurnID, f.TurnMetadata, f.ParentThreadID, f.Subagent,
		f.ManagedResidency,
	}
	for _, value := range values {
		if err := value.validate(); err != nil {
			return err
		}
	}
	if err := f.TurnState.validateTurnState(); err != nil {
		return err
	}
	managedDigest := strings.TrimSpace(f.ManagedConfigurationDigest)
	if managedDigest != "" {
		if len(managedDigest) != sha256.Size*2 {
			return errors.New("managed configuration digest 不是 SHA-256")
		}
		if _, err := hex.DecodeString(managedDigest); err != nil {
			return errors.New("managed configuration digest 非法")
		}
	}
	if f.Conditions.TurnStatePresent != f.TurnState.present() ||
		f.Conditions.SubagentPresent != f.Subagent.present() ||
		f.Conditions.ParentThreadPresent != f.ParentThreadID.present() ||
		f.Conditions.SessionIDPresent != f.SessionID.present() ||
		f.Conditions.ManagedResidencyPresent != f.ManagedResidency.present() {
		return errors.New("身份事实与请求条件不一致")
	}
	return nil
}

func (f CodexIdentityFacts) Digest() string {
	raw, err := json.Marshal(f)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// invocationDigest 只绑定跨 attempt 必须稳定的账号、进程、安装与会话事实。
// WS 当前 turn、turn-state、client request 与模型条件由每个受控帧/attempt 单独
// 进入 FinalizationToken，不能反向污染 invocation 的稳定身份。
func (f CodexIdentityFacts) invocationDigest() string {
	raw, err := json.Marshal(struct {
		AccountIdentityProjection  CodexIdentityValue
		ChatGPTAccountID           CodexIdentityValue
		WorkspaceID                CodexIdentityValue
		ProcessSurface             CodexIdentityValue
		ProcessPhase               CodexIdentityValue
		TerminalToken              CodexIdentityValue
		UserAgentSuffixEnabled     bool
		InstallationID             CodexIdentityValue
		SessionID                  CodexIdentityValue
		ConversationID             CodexIdentityValue
		ThreadID                   CodexIdentityValue
		WindowID                   CodexIdentityValue
		ManagedResidency           CodexIdentityValue
		ManagedConfigurationDigest string
		FedRAMPAccount             bool
	}{
		f.AccountIdentityProjection, f.ChatGPTAccountID, f.WorkspaceID,
		f.ProcessSurface, f.ProcessPhase, f.TerminalToken, f.UserAgentSuffixEnabled,
		f.InstallationID, f.SessionID,
		f.ConversationID, f.ThreadID, f.WindowID, f.ManagedResidency,
		f.ManagedConfigurationDigest, f.Conditions.FedRAMPAccount,
	})
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// AttemptAuthenticationInput 只在单个 attempt 开始前短暂存在。所有字段都会在
// compiler 取得一次后从能力中清空；结构体本身不进入日志、收据或公开诊断视图。
type AttemptAuthenticationInput struct {
	BearerToken   string
	AgentIdentity string
	Attestation   string
	Cookie        string
	RefreshToken  string
}

type attemptAuthenticationState struct {
	mu       sync.Mutex
	material AttemptAuthenticationInput
	consumed bool
}

// AttemptAuthentication 是一次性认证能力。Plan clone 共享同一消费状态，不能通过
// 值拷贝或跨 retry 复用认证材料。
type AttemptAuthentication struct {
	state *attemptAuthenticationState
}

func NewAttemptAuthentication(input AttemptAuthenticationInput) (AttemptAuthentication, error) {
	input.BearerToken = strings.TrimSpace(input.BearerToken)
	input.AgentIdentity = strings.TrimSpace(input.AgentIdentity)
	input.Attestation = strings.TrimSpace(input.Attestation)
	input.Cookie = strings.TrimSpace(input.Cookie)
	input.RefreshToken = strings.TrimSpace(input.RefreshToken)
	if input.BearerToken != "" && input.AgentIdentity != "" {
		return AttemptAuthentication{}, errors.New("单个 attempt 不能同时使用 Bearer 与 Agent Identity")
	}
	if input.BearerToken == "" && input.AgentIdentity == "" && input.Attestation == "" &&
		input.Cookie == "" && input.RefreshToken == "" {
		return AttemptAuthentication{}, nil
	}
	return AttemptAuthentication{state: &attemptAuthenticationState{material: input}}, nil
}

func (a AttemptAuthentication) clone() AttemptAuthentication {
	return AttemptAuthentication{state: a.state}
}

func (a AttemptAuthentication) take() (AttemptAuthenticationInput, error) {
	if a.state == nil {
		return AttemptAuthenticationInput{}, nil
	}
	a.state.mu.Lock()
	defer a.state.mu.Unlock()
	if a.state.consumed {
		return AttemptAuthenticationInput{}, errors.New("AttemptAuthentication 已消费，禁止跨 attempt 复用")
	}
	a.state.consumed = true
	material := a.state.material
	a.state.material = AttemptAuthenticationInput{}
	return material, nil
}
