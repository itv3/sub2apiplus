package profilecontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// 可选跨端点节：由目标版本画像声明的新行为开关（0.156.1 起）。
//
// # 语义
//
// 节不存在表示该版本画像没有这项行为，执行层保持旧逻辑；节存在时必须完整合法，
// 未知字段、显式 null、空列表、未排序或重复的取值一律拒绝，避免拼写错误让开关静默失效。
//
// # 与旧版本画像的关系
//
// 这些节只在存在时进入 ProfileSpec.crossSections 与可执行投影；快照 DTO 对它们使用
// omitempty。因此 0.154.0、0.151.0 等不含这些节的画像，规范化 JSON、ProfileDigest 与
// executable 摘要逐字节不变，回退到旧版本时也不会带出新行为。

const (
	// SectionCookieJar：共享 Cookie jar 的名单与 WS 握手回写（SPEC-H1-004）。
	SectionCookieJar = "CookieJar"
	// SectionWorkspaceRouting：工作区路由发现与非默认路由处置（SPEC-EP-002 及路由 override 头）。
	SectionWorkspaceRouting = "WorkspaceRouting"
	// SectionTurnMetadata：x-codex-turn-metadata 的目标键集与取值（压缩闭集、analytics_enabled 等）。
	SectionTurnMetadata = "TurnMetadata"
	// SectionClientMetadata：请求体 client_metadata 中由客户端固定写入的常量键。
	SectionClientMetadata = "ClientMetadata"
	// SectionTurnState：turn-state 保存与清空条件（SPEC-BODY-004）。
	SectionTurnState = "TurnState"
	// SectionWebSocketRetry：WS 可重试错误与降级预算（SPEC-PROTO-002）。
	SectionWebSocketRetry = "WebSocketRetry"
	// SectionWebSocketContinuation：WS 增量续接（previous_response_id）的失效条件（SPEC-WS-005）。
	SectionWebSocketContinuation = "WebSocketContinuation"
)

// OptionalSectionNames 按快照字段顺序列出全部可选节；顺序也是数据，不排序。
var OptionalSectionNames = []string{
	SectionCookieJar,
	SectionWorkspaceRouting,
	SectionTurnMetadata,
	SectionClientMetadata,
	SectionTurnState,
	SectionWebSocketRetry,
	SectionWebSocketContinuation,
}

// CookieJarSection 声明共享 Cookie jar 接受的 Cookie 名与 WS 握手回写。
type CookieJarSection struct {
	// AllowedNames：允许保存并回送的 Cookie 名（排序去重）。
	AllowedNames []string `json:"AllowedNames"`
	// AllowedPrefixes：按前缀允许的 Cookie 名（排序去重，例如 Cloudflare 挑战 Cookie）。
	AllowedPrefixes []string `json:"AllowedPrefixes"`
	// WebSocketHandshakeWriteBack：WS 握手响应（101 与被拒绝的升级）中的 Set-Cookie 写回同一 jar。
	WebSocketHandshakeWriteBack bool `json:"WebSocketHandshakeWriteBack"`
}

// WorkspaceRoutingSection 声明工作区路由发现请求与路由结果的处置。
type WorkspaceRoutingSection struct {
	// DiscoveryEndpointID：发现请求所用端点，必须存在于同一画像。
	DiscoveryEndpointID string `json:"DiscoveryEndpointID"`
	// DefaultOrigin：默认业务 origin。
	DefaultOrigin string `json:"DefaultOrigin"`
	// AcceptedOriginValues：workspace_backend_origin 中视为默认路由的取值（排序去重）。
	AcceptedOriginValues []string `json:"AcceptedOriginValues"`
	// AcceptedOverrideValues：account_routing_override 中视为无需 override 的取值（排序去重）。
	AcceptedOverrideValues []string `json:"AcceptedOverrideValues"`
	// OverrideHeader：官方客户端在非默认路由时追加的请求头名（记录用，首期不发出）。
	OverrideHeader string `json:"OverrideHeader"`
	// NonDefaultAction：发现非默认路由时的处置，目前只支持 fail_closed。
	NonDefaultAction string `json:"NonDefaultAction"`
	// RoutedEndpointIDs：受路由结果影响的端点（排序去重），必须存在于同一画像。
	RoutedEndpointIDs []string `json:"RoutedEndpointIDs"`
}

// TurnMetadataSection 声明 x-codex-turn-metadata 的目标键集与固定取值。
type TurnMetadataSection struct {
	// Keys：目标版本 turn metadata 的完整键集（排序去重）。
	Keys []string `json:"Keys"`
	// AnalyticsEnabled：analytics_enabled 的出站取值（按官方取证值固定，不透传下游）。
	AnalyticsEnabled bool `json:"AnalyticsEnabled"`
	// CompactionImplementations：compaction.implementation 允许的取值（排序去重）。
	CompactionImplementations []string `json:"CompactionImplementations"`
	// CompactionPhases：compaction.phase 允许的取值（排序去重）。
	CompactionPhases []string `json:"CompactionPhases"`
}

// ClientMetadataSection 声明请求体 client_metadata 中客户端固定写入的常量键。
type ClientMetadataSection struct {
	Constants map[string]string `json:"Constants"`
	// Condition：常量的写入条件，取引擎支持的请求条件闭集；恒定写入时为 always。
	// 0.156.1 的 guardian 审阅请求不写 guardian_credits_requested，取 not_guardian_review_request。
	Condition ConditionKind `json:"Condition"`
}

// TurnStateSection 声明 turn-state 的清空条件。
type TurnStateSection struct {
	// ResetOnAccountOwnerChange：同一会话内上游账号 owner 变化时丢弃已保存的 turn-state 与 WS 续接状态。
	ResetOnAccountOwnerChange bool `json:"ResetOnAccountOwnerChange"`
}

// WebSocketRetrySection 声明 WS 可重试错误与降级预算。
type WebSocketRetrySection struct {
	// RetryableErrorCodes：握手拒绝体或流内 wrapped error 中视为可重试的 error.code（排序去重）。
	RetryableErrorCodes []string `json:"RetryableErrorCodes"`
	// RetryBudget：计入 stream 重试预算的次数上限。
	RetryBudget int `json:"RetryBudget"`
	// FallbackTransport：预算耗尽后的降级传输，目前只支持 http。
	FallbackTransport string `json:"FallbackTransport"`
}

// WebSocketContinuationSection 声明 WS 增量续接的失效条件。
type WebSocketContinuationSection struct {
	// ResetOn：任一条件变化即新建 WS 并全量发送（排序去重，取值见 continuationResetKinds）。
	ResetOn []string `json:"ResetOn"`
}

var (
	sectionTokenPattern   = regexp.MustCompile(`^[a-z0-9_]+$`)
	cookieNamePattern     = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	headerNamePattern     = regexp.MustCompile(`^[a-z0-9-]+$`)
	errOptionalSectionNil = errors.New("可选节不得显式为 null")

	continuationResetKinds = map[string]bool{
		"account_owner":             true,
		"account_routing_override":  true,
		"auth_revision":             true,
		"base_url":                  true,
		"late_tool_result_metadata": true,
	}
)

// DecodeOptionalSection 严格解码并校验一个可选节；未知节名、未知字段或非法取值均返回错误。
func DecodeOptionalSection(name string, raw json.RawMessage) (any, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil, fmt.Errorf("%s: %w", name, errOptionalSectionNil)
	}
	var target any
	switch name {
	case SectionCookieJar:
		target = &CookieJarSection{}
	case SectionWorkspaceRouting:
		target = &WorkspaceRoutingSection{}
	case SectionTurnMetadata:
		target = &TurnMetadataSection{}
	case SectionClientMetadata:
		target = &ClientMetadataSection{}
	case SectionTurnState:
		target = &TurnStateSection{}
	case SectionWebSocketRetry:
		target = &WebSocketRetrySection{}
	case SectionWebSocketContinuation:
		target = &WebSocketContinuationSection{}
	default:
		return nil, fmt.Errorf("未知可选节: %s", name)
	}
	dec := json.NewDecoder(bytes.NewReader(trimmed))
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		return nil, fmt.Errorf("解析可选节 %s: %w", name, err)
	}
	if dec.More() {
		return nil, fmt.Errorf("可选节 %s 后存在多余数据", name)
	}
	if err := validateOptionalSection(name, target); err != nil {
		return nil, fmt.Errorf("校验可选节 %s: %w", name, err)
	}
	return target, nil
}

func validateOptionalSection(name string, value any) error {
	switch section := value.(type) {
	case *CookieJarSection:
		if err := requireSortedUnique("AllowedNames", section.AllowedNames, cookieNamePattern); err != nil {
			return err
		}
		return requireSortedUnique("AllowedPrefixes", section.AllowedPrefixes, cookieNamePattern)
	case *WorkspaceRoutingSection:
		if section.DiscoveryEndpointID == "" {
			return errors.New("DiscoveryEndpointID 不能为空")
		}
		if !strings.HasPrefix(section.DefaultOrigin, "https://") || strings.Count(section.DefaultOrigin, "/") != 2 {
			return errors.New("DefaultOrigin 必须是不带路径的 https origin")
		}
		if err := requireSortedUnique("AcceptedOriginValues", section.AcceptedOriginValues, nil); err != nil {
			return err
		}
		if err := requireSortedUnique("AcceptedOverrideValues", section.AcceptedOverrideValues, nil); err != nil {
			return err
		}
		if !headerNamePattern.MatchString(section.OverrideHeader) {
			return errors.New("OverrideHeader 必须是小写请求头名")
		}
		if section.NonDefaultAction != "fail_closed" {
			return fmt.Errorf("NonDefaultAction 只支持 fail_closed，实际 %q", section.NonDefaultAction)
		}
		return requireSortedUnique("RoutedEndpointIDs", section.RoutedEndpointIDs, sectionTokenPattern)
	case *TurnMetadataSection:
		if err := requireSortedUnique("Keys", section.Keys, sectionTokenPattern); err != nil {
			return err
		}
		if err := requireSortedUnique("CompactionImplementations", section.CompactionImplementations, sectionTokenPattern); err != nil {
			return err
		}
		return requireSortedUnique("CompactionPhases", section.CompactionPhases, sectionTokenPattern)
	case *ClientMetadataSection:
		if len(section.Constants) == 0 {
			return errors.New("Constants 不能为空")
		}
		for key, entry := range section.Constants {
			if !sectionTokenPattern.MatchString(key) || entry == "" {
				return fmt.Errorf("Constants 键或值非法: %q", key)
			}
		}
		// 条件必须显式给出：空串在条件闭集里表示“无条件”，这里不允许省略，恒定写入写 always。
		if section.Condition == "" || !EngineSupportedEnumValues().Contains(EnumDomainConditionKind, string(section.Condition)) {
			return fmt.Errorf("Condition 未受支持: %q", section.Condition)
		}
		return nil
	case *TurnStateSection:
		if !section.ResetOnAccountOwnerChange {
			return errors.New("TurnState 节存在时 ResetOnAccountOwnerChange 必须为 true")
		}
		return nil
	case *WebSocketRetrySection:
		if err := requireSortedUnique("RetryableErrorCodes", section.RetryableErrorCodes, sectionTokenPattern); err != nil {
			return err
		}
		if section.RetryBudget <= 0 {
			return errors.New("RetryBudget 必须为正")
		}
		if section.FallbackTransport != "http" {
			return fmt.Errorf("FallbackTransport 只支持 http，实际 %q", section.FallbackTransport)
		}
		return nil
	case *WebSocketContinuationSection:
		if err := requireSortedUnique("ResetOn", section.ResetOn, sectionTokenPattern); err != nil {
			return err
		}
		for _, kind := range section.ResetOn {
			if !continuationResetKinds[kind] {
				return fmt.Errorf("ResetOn 含未知条件: %s", kind)
			}
		}
		return nil
	default:
		return fmt.Errorf("未知可选节类型: %s", name)
	}
}

// requireSortedUnique 要求列表非空、严格升序（即排序且无重复），元素满足可选的格式约束。
func requireSortedUnique(field string, values []string, pattern *regexp.Regexp) error {
	if len(values) == 0 {
		return fmt.Errorf("%s 不能为空", field)
	}
	if !sort.StringsAreSorted(values) {
		return fmt.Errorf("%s 必须排序", field)
	}
	for index, value := range values {
		if value == "" || (pattern != nil && !pattern.MatchString(value)) {
			return fmt.Errorf("%s 含非法取值: %q", field, value)
		}
		if index > 0 && values[index-1] == value {
			return fmt.Errorf("%s 含重复取值: %q", field, value)
		}
	}
	return nil
}
