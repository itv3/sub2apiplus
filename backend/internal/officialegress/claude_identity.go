package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/google/uuid"
)

const (
	ClaudeSessionSourceOfficialConsistent = "official-consistent"
	ClaudeSessionSourcePlannerDerived     = "planner-derived"

	ClaudeEntrypointSDKCLI = "sdk-cli"
	ClaudeEntrypointCLI    = "cli"
)

var (
	claudeDeviceIDPattern        = regexp.MustCompile(`^[0-9a-f]{64}$`)
	claudeAgentIDPattern         = regexp.MustCompile(`^[0-9a-f]{17}$`)
	claudeRequestIDPattern       = regexp.MustCompile(`^req_[A-Za-z0-9]+$`)
	claudeControlledValuePattern = regexp.MustCompile(`^[A-Za-z0-9._:/-]{1,200}$`)
)

// ClaudeTrustedAccountFacts 只能由已认证 OAuth 账号状态构造。
type ClaudeTrustedAccountFacts struct {
	AccountScope string
	AccountUUID  string
	DeviceID     string
}

// ClaudeTrustedSessionFacts 只能由一致的官方会话身份或受管 Planner 会话构造。
type ClaudeTrustedSessionFacts struct {
	SessionID         string
	Source            string
	PreviousRequestID string
	Forked            bool
}

// ClaudeTrustedEntrypointFacts 把入口和已认证入站绑定与原始客户端 Header 隔离。
type ClaudeTrustedEntrypointFacts struct {
	Entrypoint       string
	IngressProtocol  string
	IngressBindingID string
}

// ClaudeTrustedAgentLineageFacts 只接受 Planner 已批准的 agent 谱系。
type ClaudeTrustedAgentLineageFacts struct {
	AgentID       string
	ParentAgentID string
	Depth         int
	Background    bool
}

type ClaudeSystemMode string

const (
	ClaudeSystemDefault        ClaudeSystemMode = "default"
	ClaudeSystemCustom         ClaudeSystemMode = "custom"
	ClaudeSystemAppend         ClaudeSystemMode = "append"
	ClaudeSystemExcludeDynamic ClaudeSystemMode = "exclude-dynamic"
	ClaudeSystemCustomAgent    ClaudeSystemMode = "custom-agent"
)

// ClaudeTrustedFeatureFacts 只表达已批准条件，不能直接从任意同名入站 Header 构造。
type ClaudeTrustedFeatureFacts struct {
	SystemMode              ClaudeSystemMode
	DisableAttribution      bool
	DisablePromptCaching    bool
	DisableThinking         bool
	DisableAdaptiveThinking bool
	Effort                  string
	MaxTokens               int
	AdditionalProtection    bool
	ClientApp               string
	RemoteContainerID       string
	RemoteSessionID         string
	AgentSDKVersion         string
	Workload                string
	CustomHeaderLines       []string
	CustomBetas             []string
	ExtraMetadata           json.RawMessage
	RequestGzip             bool
	FallbackModelEnabled    bool
	MaxRetries              int
	MaxRetriesSet           bool
	APITimeoutMS            int
	DisableStreamFallback   bool
	TUITitleRequest         bool
	AdvisorEnabled          bool
	WebSearchServerEnabled  bool
}

// ClaudeTrustedFacts 是 PersonaPlanner 的完整可信输入。
type ClaudeTrustedFacts struct {
	Account    ClaudeTrustedAccountFacts
	Session    ClaudeTrustedSessionFacts
	Entrypoint ClaudeTrustedEntrypointFacts
	Agent      ClaudeTrustedAgentLineageFacts
	Features   ClaudeTrustedFeatureFacts
}

type ClaudeIdentityDerivation struct {
	Field     string
	Source    string
	Reason    string
	Scope     string
	Lifecycle string
}

// ClaudeIdentityFacts 是可信事实经 Identity Authority 校验后的内部形态。
type ClaudeIdentityFacts struct {
	accountScope      string
	accountUUID       string
	deviceID          string
	metadataUserID    string
	sessionID         string
	sessionSource     string
	entrypoint        string
	ingressProtocol   string
	ingressBindingID  string
	agentID           string
	parentAgentID     string
	agentDepth        int
	background        bool
	previousRequestID string
	forked            bool
	derivations       []ClaudeIdentityDerivation
}

func deriveClaudeIdentityFacts(trusted ClaudeTrustedFacts) (ClaudeIdentityFacts, error) {
	trusted.Account.AccountScope = strings.TrimSpace(trusted.Account.AccountScope)
	trusted.Account.AccountUUID = strings.TrimSpace(trusted.Account.AccountUUID)
	trusted.Account.DeviceID = strings.ToLower(strings.TrimSpace(trusted.Account.DeviceID))
	trusted.Session.SessionID = strings.TrimSpace(trusted.Session.SessionID)
	trusted.Session.Source = strings.TrimSpace(trusted.Session.Source)
	trusted.Session.PreviousRequestID = strings.TrimSpace(trusted.Session.PreviousRequestID)
	trusted.Entrypoint.Entrypoint = strings.TrimSpace(trusted.Entrypoint.Entrypoint)
	trusted.Entrypoint.IngressProtocol = strings.TrimSpace(trusted.Entrypoint.IngressProtocol)
	trusted.Entrypoint.IngressBindingID = strings.TrimSpace(trusted.Entrypoint.IngressBindingID)
	trusted.Agent.AgentID = strings.ToLower(strings.TrimSpace(trusted.Agent.AgentID))
	trusted.Agent.ParentAgentID = strings.ToLower(strings.TrimSpace(trusted.Agent.ParentAgentID))
	if trusted.Account.AccountScope == "" || trusted.Entrypoint.IngressBindingID == "" {
		return ClaudeIdentityFacts{}, errors.New("Claude Identity Authority 缺少账号或 ingress binding")
	}
	if _, err := uuid.Parse(trusted.Account.AccountUUID); err != nil {
		return ClaudeIdentityFacts{}, errors.New("Claude OAuth account_uuid 缺失或非法")
	}
	if _, err := uuid.Parse(trusted.Session.SessionID); err != nil {
		return ClaudeIdentityFacts{}, errors.New("Claude Planner session_id 非法")
	}
	if trusted.Session.Source != ClaudeSessionSourceOfficialConsistent &&
		trusted.Session.Source != ClaudeSessionSourcePlannerDerived {
		return ClaudeIdentityFacts{}, errors.New("Claude session 不是批准的可信来源")
	}
	if trusted.Session.PreviousRequestID != "" &&
		!claudeRequestIDPattern.MatchString(trusted.Session.PreviousRequestID) {
		return ClaudeIdentityFacts{}, errors.New("Claude previous request identity 非法")
	}
	if trusted.Entrypoint.Entrypoint != ClaudeEntrypointSDKCLI &&
		trusted.Entrypoint.Entrypoint != ClaudeEntrypointCLI {
		return ClaudeIdentityFacts{}, errors.New("Claude entrypoint 不在 SupportEnvelope")
	}
	if trusted.Entrypoint.IngressProtocol != "anthropic-messages" &&
		trusted.Entrypoint.IngressProtocol != "managed-internal" {
		return ClaudeIdentityFacts{}, errors.New("Claude ingress protocol 不在批准闭集")
	}
	if err := validateClaudeAgentLineage(trusted.Agent); err != nil {
		return ClaudeIdentityFacts{}, err
	}
	if trusted.Agent.Background && trusted.Entrypoint.Entrypoint != ClaudeEntrypointCLI {
		return ClaudeIdentityFacts{}, errors.New("Claude background 只允许可信 cli entrypoint")
	}
	if err := validateClaudeTrustedFeatures(trusted); err != nil {
		return ClaudeIdentityFacts{}, err
	}
	deviceID := trusted.Account.DeviceID
	deviceSource := "official-metadata"
	if deviceID == "" {
		deviceRaw := sha256.Sum256([]byte(strings.Join([]string{
			"claude-device-v1", trusted.Account.AccountScope,
			trusted.Entrypoint.IngressBindingID,
		}, "\x00")))
		deviceID = hex.EncodeToString(deviceRaw[:])
		deviceSource = "ingress-binding"
	}
	if !claudeDeviceIDPattern.MatchString(deviceID) {
		return ClaudeIdentityFacts{}, errors.New("Claude device_id 派生失败")
	}
	metadataUserID, err := buildClaudeMetadataUserID(
		trusted.Features.ExtraMetadata,
		deviceID,
		trusted.Account.AccountUUID,
		trusted.Session.SessionID,
	)
	if err != nil {
		return ClaudeIdentityFacts{}, err
	}
	return ClaudeIdentityFacts{
		accountScope:      trusted.Account.AccountScope,
		accountUUID:       trusted.Account.AccountUUID,
		deviceID:          deviceID,
		metadataUserID:    metadataUserID,
		sessionID:         trusted.Session.SessionID,
		sessionSource:     trusted.Session.Source,
		entrypoint:        trusted.Entrypoint.Entrypoint,
		ingressProtocol:   trusted.Entrypoint.IngressProtocol,
		ingressBindingID:  trusted.Entrypoint.IngressBindingID,
		agentID:           trusted.Agent.AgentID,
		parentAgentID:     trusted.Agent.ParentAgentID,
		agentDepth:        trusted.Agent.Depth,
		background:        trusted.Agent.Background,
		previousRequestID: trusted.Session.PreviousRequestID,
		forked:            trusted.Session.Forked,
		derivations: []ClaudeIdentityDerivation{
			{Field: "account_uuid", Source: "oauth-account", Reason: "OAuth 账号状态", Scope: "account", Lifecycle: "credential"},
			{Field: "device_id", Source: deviceSource, Reason: "官方 metadata 或账号与认证入口绑定派生", Scope: "account-binding", Lifecycle: "binding"},
			{Field: "session_id", Source: trusted.Session.Source, Reason: "一致官方会话或受管 Planner 会话", Scope: "session", Lifecycle: "session"},
			{Field: "entrypoint", Source: "planner-entrypoint", Reason: "受管入口选择", Scope: "invocation", Lifecycle: "invocation"},
		},
	}, nil
}

func validateClaudeTrustedFeatures(trusted ClaudeTrustedFacts) error {
	features := trusted.Features
	switch features.SystemMode {
	case "", ClaudeSystemDefault, ClaudeSystemCustom, ClaudeSystemAppend,
		ClaudeSystemExcludeDynamic, ClaudeSystemCustomAgent:
	default:
		return errors.New("Claude system mode 不在批准闭集")
	}
	if features.Effort != "" && !validClaudeEffort(features.Effort) {
		return errors.New("Claude effort 不在批准闭集")
	}
	if features.MaxTokens < 0 || features.MaxTokens > 64000 {
		return errors.New("Claude max_tokens 超出批准范围")
	}
	if features.MaxRetriesSet && (features.MaxRetries < 0 || features.MaxRetries > 2) {
		return errors.New("Claude max retries 超出实测范围")
	}
	if features.APITimeoutMS < 0 {
		return errors.New("Claude API timeout 非法")
	}
	for name, value := range map[string]string{
		"client-app":          features.ClientApp,
		"remote-container-id": features.RemoteContainerID,
		"remote-session-id":   features.RemoteSessionID,
		"agent-sdk-version":   features.AgentSDKVersion,
		"workload":            features.Workload,
	} {
		if err := validateClaudeControlledValue(name, strings.TrimSpace(value)); err != nil {
			return err
		}
	}
	for _, beta := range features.CustomBetas {
		if strings.ContainsAny(beta, "\r\n") {
			return errors.New("Claude custom beta 含换行")
		}
	}
	if features.TUITitleRequest &&
		(trusted.Entrypoint.Entrypoint != ClaudeEntrypointCLI || trusted.Agent.AgentID != "" || trusted.Agent.Background) {
		return errors.New("Claude TUI title 场景身份非法")
	}
	if features.AdvisorEnabled && features.WebSearchServerEnabled {
		return errors.New("Claude advisor 与 server web_search 条件不能同时启用")
	}
	return nil
}

func validClaudeEffort(value string) bool {
	switch strings.TrimSpace(value) {
	case "low", "medium", "high", "xhigh", "max":
		return true
	default:
		return false
	}
}

func validateClaudeAgentLineage(agent ClaudeTrustedAgentLineageFacts) error {
	if agent.AgentID == "" {
		if agent.ParentAgentID != "" || agent.Depth != 0 {
			return errors.New("Claude 主请求不得携带 agent 谱系")
		}
		return nil
	}
	if !claudeAgentIDPattern.MatchString(strings.ToLower(strings.TrimSpace(agent.AgentID))) ||
		agent.Depth < 1 || agent.Depth > 3 {
		return errors.New("Claude agent-id 或深度非法")
	}
	if agent.Depth == 1 && strings.TrimSpace(agent.ParentAgentID) != "" {
		return errors.New("Claude 一级 agent 不得携带 parent-agent-id")
	}
	if agent.Depth > 1 && !claudeAgentIDPattern.MatchString(
		strings.ToLower(strings.TrimSpace(agent.ParentAgentID)),
	) {
		return errors.New("Claude 深层 agent 缺少直接父 agent-id")
	}
	if strings.EqualFold(agent.AgentID, agent.ParentAgentID) {
		return errors.New("Claude agent-id 不得等于 parent-agent-id")
	}
	return nil
}

func buildClaudeMetadataUserID(
	extraRaw json.RawMessage,
	deviceID string,
	accountUUID string,
	sessionID string,
) (string, error) {
	extra := make([]claudeJSONField, 0)
	if len(strings.TrimSpace(string(extraRaw))) != 0 {
		var err error
		extra, err = decodeClaudeOrderedObject(extraRaw)
		if err != nil {
			return "", errors.New("Claude extra metadata 必须是 JSON 对象")
		}
	}
	filtered := make([]claudeJSONField, 0, len(extra)+3)
	for _, field := range extra {
		if strings.TrimSpace(field.name) != field.name || field.name == "" || !json.Valid(field.raw) {
			return "", errors.New("Claude extra metadata 含非法字段")
		}
		switch field.name {
		case "device_id", "account_uuid", "session_id":
			continue
		default:
			filtered = append(filtered, claudeJSONField{
				name: field.name, raw: append(json.RawMessage(nil), field.raw...),
			})
		}
	}
	deviceRaw, _ := marshalClaudeJSON(deviceID)
	accountRaw, _ := marshalClaudeJSON(accountUUID)
	sessionRaw, _ := marshalClaudeJSON(sessionID)
	filtered = append(filtered,
		claudeJSONField{name: "device_id", raw: deviceRaw},
		claudeJSONField{name: "account_uuid", raw: accountRaw},
		claudeJSONField{name: "session_id", raw: sessionRaw},
	)
	inner, err := marshalClaudeOrderedObject(filtered)
	if err != nil {
		return "", err
	}
	return string(inner), nil
}

func validateClaudeControlledValue(name string, value string) error {
	if value == "" {
		return nil
	}
	if !claudeControlledValuePattern.MatchString(value) {
		return fmt.Errorf("Claude %s 含未批准字符", name)
	}
	return nil
}

func (f ClaudeIdentityFacts) metadataJSON() (json.RawMessage, error) {
	userID, err := marshalClaudeJSON(f.metadataUserID)
	if err != nil {
		return nil, err
	}
	return marshalClaudeOrderedObject([]claudeJSONField{{name: "user_id", raw: userID}})
}

func (f ClaudeIdentityFacts) digest() string {
	return claudeAttestationDigest(
		"identity", f.accountScope, f.accountUUID, f.deviceID, f.sessionID,
		f.sessionSource, f.entrypoint, f.ingressProtocol, f.ingressBindingID,
		f.agentID, f.parentAgentID, fmt.Sprintf("%d", f.agentDepth),
		fmt.Sprintf("%t", f.background), f.previousRequestID, fmt.Sprintf("%t", f.forked),
	)
}

func (f ClaudeIdentityFacts) invocationDigest() string {
	return claudeAttestationDigest(
		"invocation", f.accountScope, f.sessionID, f.entrypoint,
		f.agentID, f.parentAgentID, fmt.Sprintf("%t", f.background),
	)
}
