package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/google/uuid"
)

// ClaudeIngressSnapshot 是 body 解压前冻结的入站 wire 事实。它只用于验证
// 所选 Release 的官方入口条件，不能选择 Persona、Release、route 或 transport。
type ClaudeIngressSnapshot struct {
	Captured    bool
	RequestGzip bool
	Headers     http.Header
}

type claudeOfficialIngressState struct {
	Beta                      string
	AttributionPresent        bool
	FallbackLatchedBy         string
	RefusalFallback           bool
	ServerFallbackHeadersSeen bool
	CatalogMatch              claudeOfficialIngressMatch
}

var (
	claudeOfficialUAPattern = regexp.MustCompile(
		`^claude-cli/([0-9]+\.[0-9]+\.[0-9]+) \(external, (sdk-cli|cli|claude-desktop-3p|claude-vscode)((?:, (?:agent-sdk|client-app|workload)/[A-Za-z0-9._:/-]+)*)\)$`,
	)
	claudeCCHPattern = regexp.MustCompile(`^[0-9a-f]{5}$`)
)

func resolveClaudeOfficialIngressBase(
	body []byte,
	snapshot ClaudeIngressSnapshot,
	trusted ClaudeTrustedFacts,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (ClaudeTrustedFacts, claudeOfficialIngressState, bool, error) {
	resolved, state, official, err := validateClaudeOfficialIngressBase(
		body, snapshot, trusted, profile, artifact,
	)
	if err != nil {
		// Claude OAuth Persona 已收窄为官方客户端专用。只要网关冻结了真实
		// 入站 wire，Catalog 未命中就必须 fail-close，禁止再降级到第三方链。
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	return resolved, state, official, nil
}

func validateClaudeOfficialIngressBase(
	body []byte,
	snapshot ClaudeIngressSnapshot,
	trusted ClaudeTrustedFacts,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (ClaudeTrustedFacts, claudeOfficialIngressState, bool, error) {
	if !snapshot.Captured {
		return trusted, claudeOfficialIngressState{}, false, nil
	}
	headers := snapshot.Headers.Clone()
	match, err := defaultClaudeOfficialIngressCatalog().matchMessages(
		body, headers, profile, artifact,
	)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	metadata, err := validateClaudeOfficialMetadataSession(body, headers)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	sessionID := metadata.sessionID
	xApp := strings.TrimSpace(headers.Get("x-app"))
	if xApp != "cli" && xApp != "cli-bg" {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New("Claude 官方 x-app 非法")
	}
	if xApp == "cli-bg" &&
		(!match.nativeTargetScenario || match.sourceEntrypoint != ClaudeEntrypointCLI) {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude background 与 UA entrypoint 冲突",
		)
	}
	fallbackLatchedValues := headers.Values("x-cc-fallback-latched-by")
	refusalFallbackValues := headers.Values("x-is-refusal-fallback")
	if len(fallbackLatchedValues) > 1 || len(refusalFallbackValues) > 1 ||
		(len(fallbackLatchedValues) == 0) != (len(refusalFallbackValues) == 0) {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude server fallback Header 缺失、重复或不成对",
		)
	}
	fallbackLatchedBy := ""
	refusalFallback := false
	serverFallbackHeadersSeen := len(fallbackLatchedValues) == 1
	if serverFallbackHeadersSeen {
		fallbackLatchedBy = strings.TrimSpace(fallbackLatchedValues[0])
		if !claudeRequestIDPattern.MatchString(fallbackLatchedBy) ||
			strings.TrimSpace(refusalFallbackValues[0]) != "true" {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude server fallback Header 值非法",
			)
		}
		refusalFallback = true
	}

	agentID := strings.ToLower(strings.TrimSpace(headers.Get("x-claude-code-agent-id")))
	parentAgentID := strings.ToLower(strings.TrimSpace(headers.Get("x-claude-code-parent-agent-id")))
	if !match.nativeTargetScenario && (agentID != "" || parentAgentID != "" || xApp == "cli-bg") {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude 新版官方入口的 agent 条件尚未登记",
		)
	}
	agent := ClaudeTrustedAgentLineageFacts{Background: xApp == "cli-bg"}
	if agentID != "" {
		agent.AgentID = agentID
		agent.ParentAgentID = parentAgentID
		agent.Depth = 1
		if parentAgentID != "" {
			agent.Depth = 2
		}
	}
	if err := validateClaudeAgentLineage(agent); err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}

	// 入站 metadata 只证明官方 wire 形状。账号 UUID 和 device_id 不能跨越
	// 调度边界；最终值继续由被选中的 OAuth 账号与 Persona 重新构造。
	trusted.Session = ClaudeTrustedSessionFacts{
		SessionID: sessionID,
		Source:    ClaudeSessionSourceOfficialConsistent,
	}
	trusted.Entrypoint.Entrypoint = match.entry.targetEntrypoint
	if match.nativeTargetScenario {
		trusted.Entrypoint.Entrypoint = match.sourceEntrypoint
	}
	trusted.Agent = agent
	trusted.Features.RequestGzip = snapshot.RequestGzip
	trusted.Features.ExtraMetadata = nil
	trusted.Features.CustomHeaderLines = nil
	if match.nativeTargetScenario {
		_, uaFeatures, _ := parseClaudeOfficialUserAgent(
			strings.TrimSpace(headers.Get("User-Agent")), match.entry.version,
		)
		trusted.Features.AdditionalProtection = strings.EqualFold(
			strings.TrimSpace(headers.Get("x-anthropic-additional-protection")), "true",
		)
		trusted.Features.RemoteContainerID = strings.TrimSpace(headers.Get("x-claude-remote-container-id"))
		trusted.Features.RemoteSessionID = strings.TrimSpace(headers.Get("x-claude-remote-session-id"))
		trusted.Features.ClientApp = strings.TrimSpace(headers.Get("x-client-app"))
		trusted.Features.AgentSDKVersion = uaFeatures["agent-sdk"]
		trusted.Features.Workload = uaFeatures["workload"]
	}

	attribution, present, err := parseClaudeOfficialAttribution(body, artifact, match)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	trusted.Features.DisableAttribution = !present
	if present {
		if match.nativeTargetScenario && attribution["cc_entrypoint"] != match.sourceEntrypoint {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude attribution entrypoint 与 UA 冲突",
			)
		}
		if (agentID != "") != (attribution["cc_is_subagent"] == "true") {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude attribution subagent 与 Header 冲突",
			)
		}
		if previous := attribution["cc_prev_req"]; previous != "" {
			if !claudeRequestIDPattern.MatchString(previous) {
				return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
					"Claude attribution previous request 非法",
				)
			}
			trusted.Session.PreviousRequestID = previous
		}
		if workload := attribution["cc_workload"]; match.nativeTargetScenario &&
			workload != trusted.Features.Workload {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude attribution workload 与 UA 冲突",
			)
		}
	}
	return trusted, claudeOfficialIngressState{
		Beta: strings.TrimSpace(headers.Get("anthropic-beta")), AttributionPresent: present,
		FallbackLatchedBy: fallbackLatchedBy, RefusalFallback: refusalFallback,
		ServerFallbackHeadersSeen: serverFallbackHeadersSeen,
		CatalogMatch:              match,
	}, true, nil
}

// resolveClaudeOfficialCountTokensIngress 只在入站明确声明所选 Release
// 身份时采用客户端会话；普通第三方请求仍使用 Planner 派生会话。认证 Header
// 始终由已认证的 Sub2API OAuth 账号重新签发，不能信任入站值。
func resolveClaudeOfficialCountTokensIngress(
	snapshot ClaudeIngressSnapshot,
	trusted ClaudeTrustedFacts,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (ClaudeTrustedFacts, error) {
	resolved, err := validateClaudeOfficialCountTokensIngress(
		snapshot, trusted, profile, artifact,
	)
	if err != nil {
		return ClaudeTrustedFacts{}, err
	}
	return resolved, nil
}

func validateClaudeOfficialCountTokensIngress(
	snapshot ClaudeIngressSnapshot,
	trusted ClaudeTrustedFacts,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (ClaudeTrustedFacts, error) {
	if !snapshot.Captured {
		return trusted, nil
	}
	headers := snapshot.Headers.Clone()
	entry, _, _, err := defaultClaudeOfficialIngressCatalog().matchHeaders(
		headers, profile, artifact,
	)
	if err != nil {
		return ClaudeTrustedFacts{}, err
	}
	if strings.TrimSpace(headers.Get("x-app")) != "cli" {
		return ClaudeTrustedFacts{}, errors.New("Claude 官方 count_tokens x-app 未登记")
	}
	sessionID := strings.TrimSpace(headers.Get("X-Claude-Code-Session-Id"))
	if _, err := uuid.Parse(sessionID); err != nil {
		return ClaudeTrustedFacts{}, errors.New("Claude 官方 count_tokens session_id 非法")
	}
	trusted.Session = ClaudeTrustedSessionFacts{
		SessionID: sessionID,
		Source:    ClaudeSessionSourceOfficialConsistent,
	}
	trusted.Entrypoint.Entrypoint = ClaudeEntrypointCLI
	if entry.targetEntrypoint == ClaudeEntrypointCLI {
		trusted.Entrypoint.Entrypoint = entry.targetEntrypoint
	}
	trusted.Features.RequestGzip = snapshot.RequestGzip
	return trusted, nil
}

//nolint:unused // 旧官方入口已退休，保留该提取器用于历史 wire 证据复算。
func extractClaudeOfficialCustomHeaders(
	headers http.Header,
	profile claudeFWGProfile,
) ([]string, error) {
	endpoint, err := profile.endpoint("messages-inference")
	if err != nil {
		return nil, err
	}
	known := make(map[string]struct{})
	for name := range claudeHeaderFacts(endpoint.headers) {
		known[strings.ToLower(name)] = struct{}{}
	}
	for _, name := range []string{
		"authorization", "content-encoding", "x-claude-code-agent-id",
		"x-claude-code-parent-agent-id", "x-anthropic-additional-protection",
		"x-cc-fallback-latched-by", "x-is-refusal-fallback",
		"x-claude-remote-container-id", "x-claude-remote-session-id", "x-client-app",
		"x-api-key", "forwarded", "x-forwarded-for", "x-forwarded-host",
		"x-forwarded-port", "x-forwarded-proto", "x-real-ip", "x-request-id",
		"cf-connecting-ip", "cf-ipcountry", "cf-ray", "cdn-loop", "true-client-ip",
		"traceparent", "tracestate", "baggage",
	} {
		known[name] = struct{}{}
	}
	names := make([]string, 0)
	for name := range headers {
		lower := strings.ToLower(strings.TrimSpace(name))
		if _, exists := known[lower]; exists || strings.HasPrefix(lower, "x-forwarded-") ||
			strings.HasPrefix(lower, "cf-") {
			continue
		}
		names = append(names, name)
	}
	sort.Slice(names, func(left, right int) bool {
		return strings.ToLower(names[left]) < strings.ToLower(names[right])
	})
	lines := make([]string, 0, len(names))
	for _, name := range names {
		values := headers.Values(name)
		if len(values) != 1 || strings.ContainsAny(values[0], "\r\n") {
			return nil, errors.New("Claude 官方 custom Header 值非法或重复")
		}
		lines = append(lines, name+": "+strings.TrimSpace(values[0]))
	}
	return lines, nil
}

type claudeOfficialMetadata struct {
	deviceID    string
	accountUUID string
	sessionID   string
}

func extractClaudeOfficialMetadata(body []byte) (claudeOfficialMetadata, error) {
	document, err := decodeClaudeUniqueObject(body)
	if err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 Body 不是唯一对象")
	}
	metadata, err := decodeClaudeUniqueObject(document["metadata"])
	if err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata 非法")
	}
	var userID string
	if json.Unmarshal(metadata["user_id"], &userID) != nil || strings.TrimSpace(userID) == "" {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata.user_id 非法")
	}
	fields, err := decodeClaudeOrderedObject([]byte(userID))
	if err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata.user_id 内层非法")
	}
	wantOrder := []string{"device_id", "account_uuid", "session_id"}
	if len(fields) != len(wantOrder) {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata.user_id 字段闭集非法")
	}
	values := make(map[string]string, len(fields))
	for index, field := range fields {
		if field.name != wantOrder[index] {
			return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata.user_id 字段顺序非法")
		}
		var value string
		if json.Unmarshal(field.raw, &value) != nil {
			return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata 身份值不是字符串")
		}
		values[field.name] = strings.TrimSpace(value)
	}
	if !claudeDeviceIDPattern.MatchString(values["device_id"]) {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata device_id 形状非法")
	}
	if _, err := uuid.Parse(values["account_uuid"]); err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata account_uuid 形状非法")
	}
	if _, err := uuid.Parse(values["session_id"]); err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata session_id 非法")
	}
	return claudeOfficialMetadata{
		deviceID: values["device_id"], accountUUID: values["account_uuid"],
		sessionID: values["session_id"],
	}, nil
}

func parseClaudeOfficialUserAgent(
	value string,
	expectedVersion string,
) (string, map[string]string, error) {
	matches := claudeOfficialUAPattern.FindStringSubmatch(strings.TrimSpace(value))
	if len(matches) != 4 || matches[1] != strings.TrimSpace(expectedVersion) {
		return "", nil, fmt.Errorf(
			"Claude 官方 User-Agent 不属于 %s 批准形态", expectedVersion,
		)
	}
	features := make(map[string]string)
	segments := strings.TrimPrefix(matches[3], ", ")
	if segments != "" {
		for _, segment := range strings.Split(segments, ", ") {
			parts := strings.SplitN(segment, "/", 2)
			if len(parts) != 2 || features[parts[0]] != "" {
				return "", nil, errors.New("Claude 官方 User-Agent 条件段非法或重复")
			}
			features[parts[0]] = parts[1]
		}
	}
	return matches[2], features, nil
}

func parseClaudeOfficialAttribution(
	body []byte,
	artifact claudeWireArtifact,
	match claudeOfficialIngressMatch,
) (map[string]string, bool, error) {
	document, err := decodeClaudeUniqueObject(body)
	if err != nil {
		return nil, false, err
	}
	var blocks []claudeWireSystemBlock
	if raw := bytes.TrimSpace(document["system"]); len(raw) == 0 {
		return nil, false, nil
	} else if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, false, errors.New("Claude 官方 system 必须是 block 数组")
	}
	if len(blocks) == 0 || !strings.HasPrefix(strings.TrimSpace(blocks[0].Text), claudeBillingPrefix) {
		return nil, false, nil
	}
	parts := strings.Split(strings.TrimSpace(blocks[0].Text), ";")
	values := make(map[string]string)
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" || part == strings.TrimSuffix(claudeBillingPrefix, ":") {
			continue
		}
		part = strings.TrimPrefix(part, claudeBillingPrefix)
		pair := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(pair) != 2 || values[pair[0]] != "" {
			return nil, false, errors.New("Claude attribution 字段非法或重复")
		}
		values[pair[0]] = pair[1]
	}
	if match.nativeTargetScenario && !claudeCCHPattern.MatchString(values["cch"]) {
		return nil, false, errors.New("Claude attribution 版本指纹或 cch 非法")
	}
	if !match.nativeTargetScenario && values["cch"] != "" &&
		!claudeCCHPattern.MatchString(values["cch"]) {
		return nil, false, errors.New("Claude attribution 来源 cch 非法")
	}
	if match.nativeTargetScenario {
		text, err := firstClaudeUserText(document["messages"])
		if err != nil {
			return nil, false, err
		}
		fingerprint, err := claudeVersionFingerprint(artifact, text)
		if err != nil {
			return nil, false, err
		}
		if values["cc_version"] != artifact.Identity.Version+"."+fingerprint {
			return nil, false, errors.New("Claude attribution target Release 指纹非法")
		}
	} else {
		version := values["cc_version"]
		if version != match.entry.version &&
			(!strings.HasPrefix(version, match.entry.version+".") ||
				len(strings.TrimPrefix(version, match.entry.version+".")) != 3) {
			return nil, false, errors.New("Claude attribution 来源版本未登记")
		}
		switch values["cc_entrypoint"] {
		case ClaudeEntrypointSDKCLI, ClaudeEntrypointCLI, "claude-desktop", "claude-vscode":
		default:
			return nil, false, errors.New("Claude attribution 来源 entrypoint 未登记")
		}
	}
	return values, true, nil
}

func completeClaudeOfficialIngressFeatures(
	trusted *ClaudeTrustedFacts,
	canonical ClaudeCanonicalRequest,
	state claudeOfficialIngressState,
	artifact claudeWireArtifact,
	headers http.Header,
) error {
	if trusted == nil {
		return errors.New("Claude 官方 ingress facts 为空")
	}
	switch canonical.scenarioHint {
	case "custom-system":
		trusted.Features.SystemMode = ClaudeSystemCustom
	case "append-system":
		trusted.Features.SystemMode = ClaudeSystemAppend
	case "exclude-dynamic":
		trusted.Features.SystemMode = ClaudeSystemExcludeDynamic
	case "custom-agent":
		trusted.Features.SystemMode = ClaudeSystemCustomAgent
	default:
		trusted.Features.SystemMode = ClaudeSystemDefault
	}
	if canonical.scenarioHint == "tui-title" {
		trusted.Features.TUITitleRequest = true
	}
	trusted.Features.AdvisorEnabled = canonical.toolMode == claudeToolModeAdvisor
	trusted.Features.WebSearchServerEnabled = canonical.toolMode == claudeToolModeWebSearchServer
	shape := claudeInitialModelShape(canonical)
	pseudoIdentity := ClaudeIdentityFacts{
		entrypoint: trusted.Entrypoint.Entrypoint,
		agentID:    trusted.Agent.AgentID,
		background: trusted.Agent.Background,
	}
	scenario, err := selectClaudeWireScenario(ClaudeEgressPlan{
		canonical: canonical, identity: pseudoIdentity, features: trusted.Features, modelShape: shape,
	}, artifact)
	if err != nil {
		return err
	}
	if canonical.thinkingPresent {
		if canonical.disableThinking || !scenario.ThinkingPresent ||
			!claudeCanonicalThinkingMatchesScenario(
				canonical, scenario, artifact.ImplementationPolicy.Thinking,
			) {
			return errors.New("Claude 官方 thinking 不在批准场景")
		}
	} else if scenario.ThinkingPresent {
		trusted.Features.DisableThinking = true
	}
	if canonical.contextManagementPresent {
		if !scenario.ContextManagementPresent ||
			!claudeJSONEqual(canonical.contextManagement, scenario.ContextManagement) {
			return errors.New("Claude 官方 context_management 不在批准场景")
		}
	} else if scenario.ContextManagementPresent && !trusted.Features.DisableThinking {
		return errors.New("Claude 官方 context_management 缺失但 thinking 未关闭")
	}
	if canonical.fallbacksPresent != scenario.FallbacksPresent ||
		(canonical.fallbacksPresent && !claudeJSONEqual(canonical.fallbacks, scenario.Fallbacks)) {
		return errors.New("Claude 官方 fallbacks 与模型场景冲突")
	}
	if len(canonical.temperature) != 0 &&
		(!scenario.TemperaturePresent || !claudeJSONEqual(canonical.temperature, scenario.Temperature)) {
		return errors.New("Claude 官方 temperature 不在批准场景")
	}
	if canonical.effort == "" && len(canonical.outputConfig) != 0 &&
		(!scenario.OutputConfigPresent || !claudeJSONEqual(canonical.outputConfig, scenario.OutputConfig)) {
		return errors.New("Claude 官方 output_config 与场景冲突")
	}
	baseBeta := claudeToolBeta(
		canonical.toolMode, scenario.AnthropicBeta, artifact.ImplementationPolicy.ToolPolicy,
	)
	customBetas, err := extractClaudeCustomBetas(state.Beta, baseBeta)
	if err != nil {
		return err
	}
	trusted.Features.CustomBetas = customBetas
	if claudeSystemBlocksEqual(canonical.system, scenario.SystemBlocks) {
		trusted.Features.DisablePromptCaching = false
	} else if claudeSystemBlocksEqualIgnoringCache(canonical.system, scenario.SystemBlocks) {
		trusted.Features.DisablePromptCaching = true
	}
	defaultTimeout := artifact.ImplementationPolicy.Retry.StreamTimeoutSeconds
	if shape == claudeModelShapeNonStream || shape == claudeModelShapeHaikuNonStream ||
		!scenario.StreamPresent {
		defaultTimeout = artifact.ImplementationPolicy.Retry.NonStreamTimeoutSeconds
	}
	timeout, _ := strconv.Atoi(strings.TrimSpace(headers.Get("x-stainless-timeout")))
	if timeout != defaultTimeout {
		trusted.Features.APITimeoutMS = timeout * 1000
	}
	return nil
}

func extractClaudeCustomBetas(actual string, base string) ([]string, error) {
	actualParts := strings.Split(strings.TrimSpace(actual), ",")
	baseParts := strings.Split(strings.TrimSpace(base), ",")
	insertAt := len(baseParts)
	for index, part := range baseParts {
		if part == "effort-2025-11-24" || part == "extended-cache-ttl-2025-04-11" {
			insertAt = index
			break
		}
	}
	if len(actualParts) < len(baseParts) ||
		!stringSlicesEqual(actualParts[:insertAt], baseParts[:insertAt]) {
		return nil, errors.New("Claude 官方 anthropic-beta 前缀不在批准策略")
	}
	suffixLength := len(baseParts) - insertAt
	if suffixLength > len(actualParts)-insertAt ||
		!stringSlicesEqual(actualParts[len(actualParts)-suffixLength:], baseParts[insertAt:]) {
		return nil, errors.New("Claude 官方 anthropic-beta 后缀不在批准策略")
	}
	custom := append([]string(nil), actualParts[insertAt:len(actualParts)-suffixLength]...)
	for _, item := range custom {
		if strings.TrimSpace(item) == "" {
			return nil, errors.New("Claude 官方 anthropic-beta 含空条件")
		}
	}
	return custom, nil
}

func stringSlicesEqual(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func classifyClaudeOfficialFallback(
	canonical *ClaudeCanonicalRequest,
	artifact claudeWireArtifact,
) {
	if canonical == nil || canonical.serverFallback {
		return
	}
	capability, err := claudeModelCapabilityForPlan(ClaudeEgressPlan{
		canonical: *canonical,
	}, artifact)
	if err != nil || !capability.LegacyRetryFallbackSupported ||
		canonical.model != capability.Scenarios.Fallback.Model {
		return
	}
	approved := capability.Scenarios.Fallback
	candidate := *canonical
	if !candidate.streamPresent {
		candidate.streamPresent = approved.StreamPresent
		candidate.stream = true
	}
	if claudeCanonicalMatchesFixedScenario(candidate, approved) {
		canonical.scenarioHint = "fallback"
	}
}
