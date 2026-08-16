package service

import "strings"

// openAIWHAMPlanType 是当前 Codex CLI OpenAPI 暴露的完整计划类型闭集。
// 新增官方枚举时必须先扩展本目录和穷举测试，禁止把已知值折叠成泛化 unknown。
type openAIWHAMPlanType string

const (
	openAIWHAMPlanGuest                      openAIWHAMPlanType = "guest"
	openAIWHAMPlanFree                       openAIWHAMPlanType = "free"
	openAIWHAMPlanGo                         openAIWHAMPlanType = "go"
	openAIWHAMPlanPlus                       openAIWHAMPlanType = "plus"
	openAIWHAMPlanPro                        openAIWHAMPlanType = "pro"
	openAIWHAMPlanProlite                    openAIWHAMPlanType = "prolite"
	openAIWHAMPlanFreeWorkspace              openAIWHAMPlanType = "free_workspace"
	openAIWHAMPlanTeam                       openAIWHAMPlanType = "team"
	openAIWHAMPlanTeamUsageBased             openAIWHAMPlanType = "self_serve_business_usage_based"
	openAIWHAMPlanBusiness                   openAIWHAMPlanType = "business"
	openAIWHAMPlanBusinessUsageBased         openAIWHAMPlanType = "enterprise_cbp_usage_based"
	openAIWHAMPlanEducation                  openAIWHAMPlanType = "education"
	openAIWHAMPlanQuorum                     openAIWHAMPlanType = "quorum"
	openAIWHAMPlanK12                        openAIWHAMPlanType = "k12"
	openAIWHAMPlanEnterprise                 openAIWHAMPlanType = "enterprise"
	openAIWHAMPlanEdu                        openAIWHAMPlanType = "edu"
	openAIWHAMPlanUnknown                    openAIWHAMPlanType = "unknown"
	openAIWHAMPlanFamilyControlledFallback   string             = "known_plan_controlled_fallback"
	openAIWHAMPlanSemanticSupported          string             = "wham_supported"
	openAIWHAMPlanSemanticControlledFallback string             = "controlled_fallback"
)

type openAIWHAMPlanDefinition struct {
	Plan           openAIWHAMPlanType `json:"plan"`
	Family         string             `json:"family"`
	SemanticStatus string             `json:"semantic_status"`
}

// openAIWHAMPlanCatalog 同时承担计划闭集和初始语义支持矩阵的机器可读权威。
// OpenAPI 只能证明值可出现；只有标记为 wham_supported 的族才获准消费顶层窗口。
var openAIWHAMPlanCatalog = [...]openAIWHAMPlanDefinition{
	{Plan: openAIWHAMPlanGuest, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanFree, Family: "free", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanGo, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanPlus, Family: "plus", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanPro, Family: "pro", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanProlite, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanFreeWorkspace, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanTeam, Family: "team", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanTeamUsageBased, Family: "team", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanBusiness, Family: "business", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanBusinessUsageBased, Family: "business", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanEducation, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanQuorum, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanK12, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanEnterprise, Family: "enterprise", SemanticStatus: openAIWHAMPlanSemanticSupported},
	{Plan: openAIWHAMPlanEdu, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
	{Plan: openAIWHAMPlanUnknown, Family: openAIWHAMPlanFamilyControlledFallback, SemanticStatus: openAIWHAMPlanSemanticControlledFallback},
}

type openAIWHAMAuthenticationKind string

const (
	openAIWHAMAuthenticationOAuth               openAIWHAMAuthenticationKind = "oauth"
	openAIWHAMAuthenticationPersonalAccessToken openAIWHAMAuthenticationKind = "personal_access_token"
	openAIWHAMAuthenticationAgentIdentity       openAIWHAMAuthenticationKind = "agent_identity"
	openAIWHAMAuthenticationUnsupported         openAIWHAMAuthenticationKind = "unsupported"
)

type openAIWHAMDecision string

const (
	openAIWHAMDecisionSupported openAIWHAMDecision = "wham_supported"
	openAIWHAMDecisionFallback  openAIWHAMDecision = "fallback"
)

const (
	openAIWHAMFallbackAuthenticationKind = "authentication_kind_unsupported"
	openAIWHAMFallbackAgentIdentity      = "agent_identity_unverified"
	openAIWHAMFallbackFedRAMP            = "fedramp_unverified"
	openAIWHAMFallbackAccountIDMissing   = "account_id_missing"
	openAIWHAMFallbackPlanMissing        = "plan_missing"
	openAIWHAMFallbackCredentialUnknown  = "credential_plan_unknown"
	openAIWHAMFallbackKnownPlan          = "known_plan_controlled_fallback"
	openAIWHAMFallbackResponsePlanAbsent = "response_plan_missing"
	openAIWHAMFallbackResponseUnknown    = "response_plan_unknown"
	openAIWHAMFallbackResponsePlan       = "response_plan_mismatch"
	openAIWHAMFallbackServiceUnavailable = "wham_service_unavailable"
	openAIWHAMFallbackRemovalCondition   = "wham_semantic_evidence_expanded"
)

// openAIWHAMFallbackReasonCatalog 是所有稳定 fallback reason code 的闭集。
// 仅上游状态码的四个已批准值展开为独立成员，不允许拼接任意响应文本。
var openAIWHAMFallbackReasonCatalog = map[string]struct{}{
	openAIWHAMFallbackAuthenticationKind: {},
	openAIWHAMFallbackAgentIdentity:      {},
	openAIWHAMFallbackFedRAMP:            {},
	openAIWHAMFallbackAccountIDMissing:   {},
	openAIWHAMFallbackPlanMissing:        {},
	openAIWHAMFallbackCredentialUnknown:  {},
	openAIWHAMFallbackKnownPlan:          {},
	openAIWHAMFallbackResponsePlanAbsent: {},
	openAIWHAMFallbackResponseUnknown:    {},
	openAIWHAMFallbackResponsePlan:       {},
	openAIWHAMFallbackServiceUnavailable: {},
	"wham_response_invalid":              {},
	"wham_endpoint_incompatible_404":     {},
	"wham_endpoint_incompatible_405":     {},
	"wham_endpoint_incompatible_410":     {},
	"wham_endpoint_incompatible_422":     {},
	"wham_rate_limit_missing":            {},
	"wham_window_missing":                {},
	"wham_window_incomplete":             {},
	"wham_window_used_percent_invalid":   {},
	"wham_window_5h_missing":             {},
	"wham_window_7d_missing":             {},
	"wham_window_reset_invalid":          {},
	"wham_window_unknown_duration":       {},
	"wham_window_duplicate_duration":     {},
}

func isOpenAIWHAMFallbackReason(reason string) bool {
	_, ok := openAIWHAMFallbackReasonCatalog[reason]
	return ok
}

// openAIWHAMMatrixCell 保存一次判定的全部输入事实、计划族和最终结论。
// JSON 标签使日志、验收工具和后续证据导出无需重新推断判定过程。
type openAIWHAMMatrixCell struct {
	CredentialPlan     string                       `json:"credential_plan"`
	ResponsePlan       string                       `json:"response_plan"`
	PlanFamily         string                       `json:"plan_family,omitempty"`
	ApprovedFamily     bool                         `json:"approved_family"`
	AuthenticationKind openAIWHAMAuthenticationKind `json:"authentication_kind"`
	AgentIdentity      bool                         `json:"agent_identity"`
	FedRAMP            bool                         `json:"fedramp"`
	AccountIDPresent   bool                         `json:"account_id_present"`
	TopLevelRateLimit  bool                         `json:"top_level_rate_limit_present"`
	FiveHourComplete   bool                         `json:"five_hour_window_complete"`
	SevenDayComplete   bool                         `json:"seven_day_window_complete"`
	KnownDurations     bool                         `json:"known_durations"`
	FinalDecision      openAIWHAMDecision           `json:"final_decision"`
	FallbackReason     string                       `json:"fallback_reason,omitempty"`
}

func openAIWHAMPlanDefinitionFor(raw string) (openAIWHAMPlanDefinition, bool) {
	plan := openAIWHAMPlanType(strings.ToLower(strings.TrimSpace(raw)))
	for _, definition := range openAIWHAMPlanCatalog {
		if definition.Plan == plan {
			return definition, true
		}
	}
	return openAIWHAMPlanDefinition{}, false
}

func openAIWHAMAuthenticationKindForAccount(account *Account) openAIWHAMAuthenticationKind {
	if account == nil || !account.IsOpenAIOAuth() {
		return openAIWHAMAuthenticationUnsupported
	}
	if account.IsOpenAIAgentIdentity() {
		return openAIWHAMAuthenticationAgentIdentity
	}
	if account.IsOpenAIPersonalAccessToken() {
		return openAIWHAMAuthenticationPersonalAccessToken
	}
	return openAIWHAMAuthenticationOAuth
}

func newOpenAIWHAMMatrixCell(account *Account) openAIWHAMMatrixCell {
	cell := openAIWHAMMatrixCell{
		AuthenticationKind: openAIWHAMAuthenticationKindForAccount(account),
		KnownDurations:     true,
		FinalDecision:      openAIWHAMDecisionFallback,
	}
	if account == nil {
		return cell
	}
	cell.AgentIdentity = account.IsOpenAIAgentIdentity()
	cell.FedRAMP = account.IsChatGPTAccountFedRAMP()
	cell.AccountIDPresent = strings.TrimSpace(account.GetCredential("chatgpt_account_id")) != "" ||
		strings.TrimSpace(account.GetCredential("organization_id")) != ""
	cell.CredentialPlan = strings.ToLower(strings.TrimSpace(account.GetCredential("plan_type")))
	return cell
}

func openAIWHAMPreflightFallbackReason(cell openAIWHAMMatrixCell) string {
	switch {
	case cell.AuthenticationKind == openAIWHAMAuthenticationAgentIdentity || cell.AgentIdentity:
		return openAIWHAMFallbackAgentIdentity
	case cell.AuthenticationKind != openAIWHAMAuthenticationOAuth:
		return openAIWHAMFallbackAuthenticationKind
	case cell.FedRAMP:
		return openAIWHAMFallbackFedRAMP
	case !cell.AccountIDPresent:
		return openAIWHAMFallbackAccountIDMissing
	case cell.CredentialPlan == "":
		return openAIWHAMFallbackPlanMissing
	}
	definition, known := openAIWHAMPlanDefinitionFor(cell.CredentialPlan)
	if !known {
		return openAIWHAMFallbackCredentialUnknown
	}
	if definition.SemanticStatus != openAIWHAMPlanSemanticSupported {
		return openAIWHAMFallbackKnownPlan
	}
	return ""
}

// evaluateOpenAIWHAMMatrix 只在凭据计划、响应计划和完整窗口均有语义证据时放行。
// mappingFallbackReason 必须来自闭集映射器，防止上游错误文本进入稳定 reason code。
func evaluateOpenAIWHAMMatrix(
	account *Account,
	usage *OpenAIQuotaUsage,
	mappingFallbackReason string,
) openAIWHAMMatrixCell {
	if mappingFallbackReason != "" && !isOpenAIWHAMFallbackReason(mappingFallbackReason) {
		mappingFallbackReason = "wham_response_invalid"
	}
	cell := newOpenAIWHAMMatrixCell(account)
	if usage != nil {
		cell.ResponsePlan = strings.ToLower(strings.TrimSpace(usage.PlanType))
		applyOpenAIWHAMWindowFacts(&cell, usage.RateLimit)
	}
	if reason := openAIWHAMPreflightFallbackReason(cell); reason != "" {
		cell.FallbackReason = reason
		return cell
	}

	credentialDefinition, _ := openAIWHAMPlanDefinitionFor(cell.CredentialPlan)
	if cell.ResponsePlan == "" {
		cell.FallbackReason = openAIWHAMFallbackResponsePlanAbsent
		return cell
	}
	responseDefinition, known := openAIWHAMPlanDefinitionFor(cell.ResponsePlan)
	if !known {
		cell.FallbackReason = openAIWHAMFallbackResponseUnknown
		return cell
	}
	if responseDefinition.SemanticStatus != openAIWHAMPlanSemanticSupported {
		cell.FallbackReason = openAIWHAMFallbackKnownPlan
		return cell
	}
	cell.PlanFamily = credentialDefinition.Family
	cell.ApprovedFamily = credentialDefinition.Family == responseDefinition.Family
	if !cell.ApprovedFamily {
		cell.FallbackReason = openAIWHAMFallbackResponsePlan
		return cell
	}
	if !cell.TopLevelRateLimit {
		cell.FallbackReason = "wham_rate_limit_missing"
		return cell
	}
	if !cell.KnownDurations {
		cell.FallbackReason = "wham_window_unknown_duration"
		return cell
	}
	if !cell.FiveHourComplete || !cell.SevenDayComplete {
		if mappingFallbackReason != "" {
			cell.FallbackReason = mappingFallbackReason
		} else if !cell.FiveHourComplete {
			cell.FallbackReason = "wham_window_5h_missing"
		} else {
			cell.FallbackReason = "wham_window_7d_missing"
		}
		return cell
	}
	if mappingFallbackReason != "" {
		cell.FallbackReason = mappingFallbackReason
		return cell
	}
	cell.FinalDecision = openAIWHAMDecisionSupported
	return cell
}

func applyOpenAIWHAMWindowFacts(cell *openAIWHAMMatrixCell, rateLimit *OpenAIRateLimit) {
	if cell == nil || rateLimit == nil {
		return
	}
	cell.TopLevelRateLimit = true
	for _, window := range []*OpenAIRateLimitWindow{rateLimit.PrimaryWindow, rateLimit.SecondaryWindow} {
		if window == nil {
			continue
		}
		complete := window.usedPercentPresent && window.limitWindowPresent &&
			(window.resetAfterPresent || window.resetAtPresent)
		if !window.limitWindowPresent {
			cell.KnownDurations = false
			continue
		}
		switch window.LimitWindowSeconds {
		case codexWHAMFiveHourSeconds:
			cell.FiveHourComplete = cell.FiveHourComplete || complete
		case codexWHAMSevenDaySeconds:
			cell.SevenDayComplete = cell.SevenDayComplete || complete
		default:
			cell.KnownDurations = false
		}
	}
}
