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
// 官方 2.1.226 条件，不能选择 Persona、Release、route 或 transport。
type ClaudeIngressSnapshot struct {
	Captured    bool
	RequestGzip bool
	Headers     http.Header
}

type claudeOfficialIngressState struct {
	Beta               string
	AttributionPresent bool
}

var (
	claudeOfficialUAPattern = regexp.MustCompile(
		`^claude-cli/2\.1\.226 \(external, (sdk-cli|cli)((?:, (?:agent-sdk|client-app|workload)/[A-Za-z0-9._:/-]+)*)\)$`,
	)
	claudeDesktopThirdPartyUAPattern = regexp.MustCompile(
		`^claude-cli/[0-9]+\.[0-9]+\.[0-9]+ \(external, claude-desktop-3p, agent-sdk/[0-9]+\.[0-9]+\.[0-9]+\)$`,
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
	if !snapshot.Captured {
		return trusted, claudeOfficialIngressState{}, false, nil
	}
	headers := snapshot.Headers.Clone()
	userAgent := strings.TrimSpace(headers.Get("User-Agent"))
	// claude-desktop-3p 明确表示第三方网关入口，不能把它的客户端版本
	// 或 Header 当作目标 Release 的可信官方身份。
	if claudeDesktopThirdPartyUAPattern.MatchString(userAgent) {
		return trusted, claudeOfficialIngressState{}, false, nil
	}
	claimed := strings.HasPrefix(strings.ToLower(userAgent), "claude-cli/")
	if !claimed {
		return trusted, claudeOfficialIngressState{}, false, nil
	}
	entrypoint, uaFeatures, err := parseClaudeOfficialUserAgent(userAgent)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	if err := validateClaudeOfficialIngressHeaders(headers, profile); err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	metadata, err := extractClaudeOfficialMetadata(body, trusted.Account.AccountUUID)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	sessionID := strings.TrimSpace(headers.Get("X-Claude-Code-Session-Id"))
	if _, err := uuid.Parse(sessionID); err != nil || metadata.sessionID != sessionID {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude 官方 Session Header 与 metadata 缺失或冲突",
		)
	}
	requestID := strings.TrimSpace(headers.Get("x-client-request-id"))
	if _, err := uuid.Parse(requestID); err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude 官方 x-client-request-id 非法",
		)
	}
	xApp := strings.TrimSpace(headers.Get("x-app"))
	if xApp != "cli" && xApp != "cli-bg" {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New("Claude 官方 x-app 非法")
	}
	if xApp == "cli-bg" && entrypoint != ClaudeEntrypointCLI {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
			"Claude background 与 UA entrypoint 冲突",
		)
	}

	agentID := strings.ToLower(strings.TrimSpace(headers.Get("x-claude-code-agent-id")))
	parentAgentID := strings.ToLower(strings.TrimSpace(headers.Get("x-claude-code-parent-agent-id")))
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

	trusted.Account.DeviceID = metadata.deviceID
	trusted.Session = ClaudeTrustedSessionFacts{
		SessionID: sessionID,
		Source:    ClaudeSessionSourceOfficialConsistent,
	}
	trusted.Entrypoint.Entrypoint = entrypoint
	trusted.Agent = agent
	trusted.Features.RequestGzip = snapshot.RequestGzip
	trusted.Features.AdditionalProtection = strings.EqualFold(
		strings.TrimSpace(headers.Get("x-anthropic-additional-protection")), "true",
	)
	trusted.Features.RemoteContainerID = strings.TrimSpace(headers.Get("x-claude-remote-container-id"))
	trusted.Features.RemoteSessionID = strings.TrimSpace(headers.Get("x-claude-remote-session-id"))
	trusted.Features.ClientApp = strings.TrimSpace(headers.Get("x-client-app"))
	trusted.Features.AgentSDKVersion = uaFeatures["agent-sdk"]
	trusted.Features.Workload = uaFeatures["workload"]
	if uaClientApp := uaFeatures["client-app"]; uaClientApp != "" {
		if trusted.Features.ClientApp != "" && trusted.Features.ClientApp != uaClientApp {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude 官方 UA client-app 与 Header 冲突",
			)
		}
		trusted.Features.ClientApp = uaClientApp
	}
	trusted.Features.ExtraMetadata = metadata.extra
	customHeaders, err := extractClaudeOfficialCustomHeaders(headers, profile)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	trusted.Features.CustomHeaderLines = customHeaders

	attribution, present, err := parseClaudeOfficialAttribution(body, artifact)
	if err != nil {
		return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, err
	}
	trusted.Features.DisableAttribution = !present
	if present {
		if attribution["cc_entrypoint"] != entrypoint {
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
		if workload := attribution["cc_workload"]; workload != trusted.Features.Workload {
			return ClaudeTrustedFacts{}, claudeOfficialIngressState{}, false, errors.New(
				"Claude attribution workload 与 UA 冲突",
			)
		}
	}
	return trusted, claudeOfficialIngressState{
		Beta: strings.TrimSpace(headers.Get("anthropic-beta")), AttributionPresent: present,
	}, true, nil
}

// resolveClaudeOfficialCountTokensIngress 只在入站明确声明官方 2.1.226
// 身份时采用客户端会话；普通第三方请求仍使用 Planner 派生会话。认证 Header
// 始终由已认证的 Sub2API OAuth 账号重新签发，不能信任入站值。
func resolveClaudeOfficialCountTokensIngress(
	snapshot ClaudeIngressSnapshot,
	trusted ClaudeTrustedFacts,
	profile claudeFWGProfile,
) (ClaudeTrustedFacts, error) {
	if !snapshot.Captured {
		return trusted, nil
	}
	headers := snapshot.Headers.Clone()
	userAgent := strings.TrimSpace(headers.Get("User-Agent"))
	if claudeDesktopThirdPartyUAPattern.MatchString(userAgent) {
		return trusted, nil
	}
	if !strings.HasPrefix(strings.ToLower(userAgent), "claude-cli/") {
		return trusted, nil
	}
	entrypoint, features, err := parseClaudeOfficialUserAgent(userAgent)
	if err != nil {
		return ClaudeTrustedFacts{}, err
	}
	if entrypoint != ClaudeEntrypointCLI || len(features) != 0 {
		return ClaudeTrustedFacts{}, errors.New("Claude 官方 count_tokens entrypoint 非法")
	}
	endpoint, err := profile.endpoint("count-tokens")
	if err != nil {
		return ClaudeTrustedFacts{}, err
	}
	facts := claudeHeaderFacts(endpoint.headers)
	for _, name := range []string{
		"accept", "accept-encoding", "anthropic-beta",
		"anthropic-dangerous-direct-browser-access", "anthropic-version", "content-type",
		"x-app", "x-stainless-arch", "x-stainless-lang", "x-stainless-os",
		"x-stainless-package-version", "x-stainless-retry-count", "x-stainless-runtime",
		"x-stainless-runtime-version",
	} {
		if strings.TrimSpace(headers.Get(name)) != facts[name].Value {
			return ClaudeTrustedFacts{}, fmt.Errorf(
				"Claude 官方 count_tokens 固定 Header 不一致：%s", name,
			)
		}
	}
	sessionID := strings.TrimSpace(headers.Get("X-Claude-Code-Session-Id"))
	if _, err := uuid.Parse(sessionID); err != nil {
		return ClaudeTrustedFacts{}, errors.New("Claude 官方 count_tokens session_id 非法")
	}
	requestID := strings.TrimSpace(headers.Get("x-client-request-id"))
	if _, err := uuid.Parse(requestID); err != nil {
		return ClaudeTrustedFacts{}, errors.New("Claude 官方 count_tokens request_id 非法")
	}
	trusted.Session = ClaudeTrustedSessionFacts{
		SessionID: sessionID,
		Source:    ClaudeSessionSourceOfficialConsistent,
	}
	trusted.Entrypoint.Entrypoint = ClaudeEntrypointCLI
	trusted.Features.RequestGzip = snapshot.RequestGzip
	return trusted, nil
}

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
	deviceID  string
	sessionID string
	extra     json.RawMessage
}

func extractClaudeOfficialMetadata(body []byte, expectedAccountUUID string) (claudeOfficialMetadata, error) {
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
	values := make(map[string]string)
	extra := make([]claudeJSONField, 0)
	for _, field := range fields {
		switch field.name {
		case "device_id", "account_uuid", "session_id":
			var value string
			if json.Unmarshal(field.raw, &value) != nil {
				return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata 身份值不是字符串")
			}
			values[field.name] = strings.TrimSpace(value)
		default:
			extra = append(extra, claudeJSONField{
				name: field.name, raw: append(json.RawMessage(nil), field.raw...),
			})
		}
	}
	if !claudeDeviceIDPattern.MatchString(values["device_id"]) ||
		values["account_uuid"] != strings.TrimSpace(expectedAccountUUID) {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata 账号或 device 身份冲突")
	}
	if _, err := uuid.Parse(values["session_id"]); err != nil {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 metadata session_id 非法")
	}
	extraRaw := json.RawMessage(nil)
	if len(extra) != 0 {
		extraRaw, err = marshalClaudeOrderedObject(extra)
		if err != nil {
			return claudeOfficialMetadata{}, err
		}
	}
	return claudeOfficialMetadata{
		deviceID: values["device_id"], sessionID: values["session_id"], extra: extraRaw,
	}, nil
}

func parseClaudeOfficialUserAgent(value string) (string, map[string]string, error) {
	matches := claudeOfficialUAPattern.FindStringSubmatch(strings.TrimSpace(value))
	if len(matches) != 3 {
		return "", nil, errors.New("Claude 官方 User-Agent 不属于 2.1.226 批准形态")
	}
	features := make(map[string]string)
	segments := strings.TrimPrefix(matches[2], ", ")
	if segments != "" {
		for _, segment := range strings.Split(segments, ", ") {
			parts := strings.SplitN(segment, "/", 2)
			if len(parts) != 2 || features[parts[0]] != "" {
				return "", nil, errors.New("Claude 官方 User-Agent 条件段非法或重复")
			}
			features[parts[0]] = parts[1]
		}
	}
	return matches[1], features, nil
}

func validateClaudeOfficialIngressHeaders(headers http.Header, profile claudeFWGProfile) error {
	endpoint, err := profile.endpoint("messages-inference")
	if err != nil {
		return err
	}
	facts := claudeHeaderFacts(endpoint.headers)
	for _, name := range []string{
		"accept", "accept-encoding", "anthropic-dangerous-direct-browser-access",
		"anthropic-version", "content-type", "x-stainless-arch", "x-stainless-lang",
		"x-stainless-os", "x-stainless-package-version", "x-stainless-retry-count",
		"x-stainless-runtime", "x-stainless-runtime-version",
	} {
		if strings.TrimSpace(headers.Get(name)) != facts[name].Value {
			return fmt.Errorf("Claude 官方入站固定 Header 不一致：%s", name)
		}
	}
	if strings.TrimSpace(headers.Get("Authorization")) == "" ||
		strings.TrimSpace(headers.Get("anthropic-beta")) == "" {
		return errors.New("Claude 官方入站缺少认证或 anthropic-beta")
	}
	timeout, err := strconv.Atoi(strings.TrimSpace(headers.Get("x-stainless-timeout")))
	if err != nil || timeout <= 0 || timeout > 3600 {
		return errors.New("Claude 官方 x-stainless-timeout 非法")
	}
	additional := strings.TrimSpace(headers.Get("x-anthropic-additional-protection"))
	if additional != "" && additional != "true" {
		return errors.New("Claude additional-protection 条件值非法")
	}
	return nil
}

func parseClaudeOfficialAttribution(
	body []byte,
	artifact claudeWireArtifact,
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
	text, err := firstClaudeUserText(document["messages"])
	if err != nil {
		return nil, false, err
	}
	fingerprint, err := claudeVersionFingerprint(artifact, text)
	if err != nil {
		return nil, false, err
	}
	if values["cc_version"] != ClaudeFWGVersion+"."+fingerprint ||
		!claudeCCHPattern.MatchString(values["cch"]) {
		return nil, false, errors.New("Claude attribution 版本指纹或 cch 非法")
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
			!claudeJSONEqual(canonical.thinking, scenario.Thinking) {
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
	if canonical == nil || canonical.model != artifact.ImplementationPolicy.Scenarios.Fallback.Model {
		return
	}
	approved := artifact.ImplementationPolicy.Scenarios.Fallback
	candidate := *canonical
	if !candidate.streamPresent {
		candidate.streamPresent = approved.StreamPresent
		candidate.stream = true
	}
	if claudeCanonicalMatchesFixedScenario(candidate, approved) {
		canonical.scenarioHint = "fallback"
	}
}
