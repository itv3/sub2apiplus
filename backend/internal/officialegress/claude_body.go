package officialegress

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"unicode/utf8"
)

const claudeBillingPrefix = "x-anthropic-billing-header:"

type claudeCanonicalSystemKind string

const (
	claudeCanonicalSystemNone     claudeCanonicalSystemKind = "none"
	claudeCanonicalSystemOfficial claudeCanonicalSystemKind = "official"
	claudeCanonicalSystemCustom   claudeCanonicalSystemKind = "custom"
)

// ClaudeCanonicalRequest 是 IngressProtocolAdapter 输出的 Messages 语义。
// Persona 身份、版本、模型、缓存和 transport 均不从任意入站字段继承。
type ClaudeCanonicalRequest struct {
	model                    string
	messages                 json.RawMessage
	tools                    json.RawMessage
	toolChoice               json.RawMessage
	toolMode                 claudeToolMode
	toolsPresent             bool
	system                   []claudeWireSystemBlock
	systemKind               claudeCanonicalSystemKind
	scenarioHint             string
	firstUserText            string
	maxTokens                int
	effort                   string
	outputConfig             json.RawMessage
	temperature              json.RawMessage
	thinking                 json.RawMessage
	thinkingPresent          bool
	contextManagement        json.RawMessage
	contextManagementPresent bool
	streamPresent            bool
	stream                   bool
	disableThinking          bool
	officialIngress          bool
}

// TranslationReport 明确记录当前 candidate 是否进行了有损协议翻译。
type TranslationReport struct {
	IngressProtocol string
	Lossless        bool
	Compatibility   string
}

type claudeModelShape string

const (
	claudeModelShapeSonnet         claudeModelShape = "sonnet"
	claudeModelShapeHaiku          claudeModelShape = "haiku_fallback"
	claudeModelShapeNonStream      claudeModelShape = "sonnet_non_stream"
	claudeModelShapeHaikuNonStream claudeModelShape = "haiku_fallback_non_stream"
)

func claudeInitialModelShape(canonical ClaudeCanonicalRequest) claudeModelShape {
	if canonical.officialIngress && canonical.scenarioHint == "fallback" {
		if !canonical.streamPresent {
			return claudeModelShapeHaikuNonStream
		}
		return claudeModelShapeHaiku
	}
	if canonical.officialIngress && !canonical.streamPresent {
		return claudeModelShapeNonStream
	}
	return claudeModelShapeSonnet
}

func claudeNonStreamModelShape(shape claudeModelShape) claudeModelShape {
	if shape == claudeModelShapeHaiku || shape == claudeModelShapeHaikuNonStream {
		return claudeModelShapeHaikuNonStream
	}
	return claudeModelShapeNonStream
}

func claudeFallbackModelShape(shape claudeModelShape) claudeModelShape {
	if shape == claudeModelShapeNonStream || shape == claudeModelShapeHaikuNonStream {
		return claudeModelShapeHaikuNonStream
	}
	return claudeModelShapeHaiku
}

func claudeModelShapeIsStream(shape claudeModelShape) bool {
	return shape != claudeModelShapeNonStream && shape != claudeModelShapeHaikuNonStream
}

func claudeModelShapeIsHaiku(shape claudeModelShape) bool {
	return shape == claudeModelShapeHaiku || shape == claudeModelShapeHaikuNonStream
}

// ClaudeEgressPlan 是 Claude Persona 私有的 typed plan；共享 Executor 只消费摘要投影。
type ClaudeEgressPlan struct {
	endpointKind   string
	canonical      ClaudeCanonicalRequest
	translation    TranslationReport
	identity       ClaudeIdentityFacts
	features       ClaudeTrustedFeatureFacts
	authentication AttemptAuthentication
	refreshScope   string
	modelShape     claudeModelShape
	cch            string
	omitConnection bool
	invocationID   string
	accountScope   string
}

func parseClaudeCanonicalMessages(
	body []byte,
	trusted ClaudeTrustedFacts,
	artifact claudeWireArtifact,
	officialIngress bool,
) (ClaudeCanonicalRequest, TranslationReport, error) {
	if len(bytes.TrimSpace(body)) == 0 {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate 缺少请求体")
	}
	document, err := decodeClaudeUniqueObject(body)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, fmt.Errorf("解析 Claude canonical request：%w", err)
	}
	allowed := map[string]struct{}{
		"model": {}, "messages": {}, "system": {}, "tools": {}, "metadata": {},
		"max_tokens": {}, "thinking": {}, "context_management": {}, "output_config": {},
		"stream": {}, "temperature": {}, "tool_choice": {},
	}
	for name := range document {
		if _, ok := allowed[name]; !ok {
			return ClaudeCanonicalRequest{}, TranslationReport{}, fmt.Errorf(
				"Claude third-party lossless SupportEnvelope 不支持字段：%s", name,
			)
		}
	}
	messages, ok := document["messages"]
	if !ok || !rawJSONArray(messages) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate messages 必须是数组")
	}
	tools, toolsPresent := document["tools"]
	if len(tools) == 0 {
		tools = json.RawMessage("[]")
	}
	if !rawJSONArray(tools) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate tools 必须是数组")
	}
	streamRaw, streamPresent := document["stream"]
	var stream bool
	if streamPresent {
		if err := json.Unmarshal(streamRaw, &stream); err != nil {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate stream 必须是布尔值")
		}
		if officialIngress && !stream {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
				"Claude 官方 non-stream fallback 必须省略 stream 字段",
			)
		}
	}
	if !officialIngress && (!streamPresent || !stream) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
			"Claude 第三方 lossless SupportEnvelope 只批准显式 stream=true",
		)
	}
	modelRaw, ok := document["model"]
	if !ok {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate 缺少 model")
	}
	var model string
	if json.Unmarshal(modelRaw, &model) != nil || strings.TrimSpace(model) == "" {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate model 必须是非空字符串")
	}
	model = strings.TrimSpace(model)
	if !claudeApprovedMessagesModel(model, artifact) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
			"Claude candidate model 不在 messages SupportEnvelope",
		)
	}
	if metadataRaw := document["metadata"]; len(metadataRaw) != 0 {
		if _, err := decodeClaudeUniqueObject(metadataRaw); err != nil {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate metadata 必须是对象")
		}
	}
	firstUserText, err := firstClaudeUserText(messages)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	system, systemKind, scenarioHint, err := parseClaudeInboundSystem(
		document["system"], artifact, officialIngress,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	maxTokens, err := parseClaudeRequestedMaxTokens(document["max_tokens"])
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	effort, outputConfig, err := parseClaudeRequestedOutputConfig(
		document["output_config"], artifact, officialIngress,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	temperature, err := parseClaudeRequestedTemperature(document["temperature"], officialIngress)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	thinkingRaw, thinkingPresent := document["thinking"]
	disableThinking, err := parseClaudeRequestedThinking(
		thinkingRaw, artifact, officialIngress,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	contextRaw, contextPresent := document["context_management"]
	if raw := contextRaw; !officialIngress && len(raw) != 0 && !claudeJSONEqual(
		raw, artifact.ImplementationPolicy.Scenarios.SDKCLI.ContextManagement,
	) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
			"Claude third-party context_management 不在 lossless SupportEnvelope",
		)
	}
	approvedTools, approvedChoice, toolMode, err := compileClaudeApprovedTools(
		tools, document["tool_choice"], artifact,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	if !officialIngress &&
		(toolMode == claudeToolModeAdvisor || toolMode == claudeToolModeWebSearchServer) {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
			"Claude advisor/server web_search 只能由可信 feature 条件选择",
		)
	}
	canonical := ClaudeCanonicalRequest{
		model:    model,
		messages: append(json.RawMessage(nil), bytes.TrimSpace(messages)...),
		tools:    approvedTools, toolChoice: approvedChoice, toolMode: toolMode,
		toolsPresent: toolsPresent,
		system:       system, systemKind: systemKind, scenarioHint: scenarioHint,
		firstUserText: firstUserText,
		maxTokens:     maxTokens, effort: effort, outputConfig: outputConfig,
		temperature:              temperature,
		thinking:                 append(json.RawMessage(nil), bytes.TrimSpace(thinkingRaw)...),
		thinkingPresent:          thinkingPresent,
		contextManagement:        append(json.RawMessage(nil), bytes.TrimSpace(contextRaw)...),
		contextManagementPresent: contextPresent,
		streamPresent:            streamPresent, stream: stream,
		disableThinking: disableThinking, officialIngress: officialIngress,
	}
	if officialIngress && trusted.Agent.Background {
		matched := -1
		for index, scenario := range artifact.ImplementationPolicy.Scenarios.Background {
			if !claudeCanonicalMatchesFixedScenario(canonical, scenario) {
				continue
			}
			if matched >= 0 {
				return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
					"Claude 官方 background 形态命中多个场景",
				)
			}
			matched = index
		}
		if matched < 0 {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
				"Claude 官方 background 形态不在批准闭集",
			)
		}
		canonical.systemKind = claudeCanonicalSystemOfficial
		canonical.scenarioHint = fmt.Sprintf("background-%d", matched)
	}
	report := TranslationReport{
		IngressProtocol: trusted.Entrypoint.IngressProtocol,
		Lossless:        true,
		Compatibility:   "reuse_lossless",
	}
	return canonical, report, nil
}

func decodeClaudeUniqueObject(raw []byte) (map[string]json.RawMessage, error) {
	fields, err := decodeClaudeOrderedObject(raw)
	if err != nil {
		return nil, err
	}
	out := make(map[string]json.RawMessage, len(fields))
	for _, field := range fields {
		out[field.name] = append(json.RawMessage(nil), field.raw...)
	}
	return out, nil
}

func decodeClaudeOrderedObject(raw []byte) ([]claudeJSONField, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return nil, errors.New("必须是 JSON 对象")
	}
	out := make([]claudeJSONField, 0)
	seen := make(map[string]struct{})
	for decoder.More() {
		nameToken, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		name, ok := nameToken.(string)
		if !ok || strings.TrimSpace(name) == "" {
			return nil, errors.New("JSON 对象字段名非法")
		}
		if _, duplicate := seen[name]; duplicate {
			return nil, fmt.Errorf("JSON 对象字段重复：%s", name)
		}
		seen[name] = struct{}{}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		out = append(out, claudeJSONField{
			name: name, raw: append(json.RawMessage(nil), bytes.TrimSpace(value)...),
		})
	}
	if token, err = decoder.Token(); err != nil || token != json.Delim('}') {
		return nil, errors.New("JSON 对象没有正常结束")
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return nil, errors.New("JSON 对象尾部存在额外值")
		}
		return nil, err
	}
	return out, nil
}

func rawJSONArray(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) >= 2 && trimmed[0] == '[' && trimmed[len(trimmed)-1] == ']' && json.Valid(trimmed)
}

func parseClaudeRequestedMaxTokens(raw json.RawMessage) (int, error) {
	if len(raw) == 0 {
		return 0, nil
	}
	var value int
	if json.Unmarshal(raw, &value) != nil || value <= 0 || value > 64000 {
		return 0, errors.New("Claude max_tokens 必须是 1..64000 的整数")
	}
	return value, nil
}

func parseClaudeRequestedOutputConfig(
	raw json.RawMessage,
	artifact claudeWireArtifact,
	officialIngress bool,
) (string, json.RawMessage, error) {
	if len(raw) == 0 {
		return "", nil, nil
	}
	fields, err := decodeClaudeUniqueObject(raw)
	if err != nil {
		return "", nil, errors.New("Claude output_config 必须是对象")
	}
	if len(fields) == 1 && len(fields["effort"]) != 0 {
		var effort string
		if json.Unmarshal(fields["effort"], &effort) != nil || !validClaudeEffort(effort) {
			return "", nil, errors.New("Claude output_config.effort 不在批准闭集")
		}
		return effort, append(json.RawMessage(nil), bytes.TrimSpace(raw)...), nil
	}
	if officialIngress && len(fields) == 1 && len(fields["format"]) != 0 {
		for _, scenario := range append(
			[]claudeWireScenario{artifact.ImplementationPolicy.Scenarios.TUITitle},
			artifact.ImplementationPolicy.Scenarios.Background...,
		) {
			if scenario.OutputConfigPresent && claudeJSONEqual(raw, scenario.OutputConfig) {
				return "", append(json.RawMessage(nil), bytes.TrimSpace(raw)...), nil
			}
		}
	}
	return "", nil, errors.New("Claude output_config 不在批准闭集")
}

func parseClaudeRequestedTemperature(
	raw json.RawMessage,
	officialIngress bool,
) (json.RawMessage, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, nil
	}
	if !officialIngress {
		return nil, errors.New("Claude 第三方 lossless SupportEnvelope 不接受 temperature")
	}
	var value float64
	if json.Unmarshal(raw, &value) != nil || value < 0 || value > 1 {
		return nil, errors.New("Claude temperature 不在批准范围")
	}
	return append(json.RawMessage(nil), bytes.TrimSpace(raw)...), nil
}

func parseClaudeRequestedThinking(
	raw json.RawMessage,
	artifact claudeWireArtifact,
	officialIngress bool,
) (bool, error) {
	if len(raw) == 0 {
		return false, nil
	}
	fields, err := decodeClaudeUniqueObject(raw)
	if err != nil {
		return false, errors.New("Claude thinking 必须是对象")
	}
	var kind string
	if json.Unmarshal(fields["type"], &kind) != nil {
		return false, errors.New("Claude thinking.type 非法")
	}
	switch kind {
	case "adaptive":
		if len(fields) != 1 {
			return false, errors.New("Claude adaptive thinking 含未批准字段")
		}
		return false, nil
	case "disabled":
		if len(fields) != 1 {
			return false, errors.New("Claude disabled thinking 含未批准字段")
		}
		// 官方 TUI/background 的固定场景会显式发送 disabled；第三方入站的
		// 同一形态表达受管关闭开关，Compiler 应省略 thinking 与 context。
		return !officialIngress, nil
	case "enabled":
		if officialIngress && claudeJSONEqual(
			raw, artifact.ImplementationPolicy.Scenarios.Fallback.Thinking,
		) {
			return false, nil
		}
		return false, errors.New("Claude enabled thinking 不在批准官方场景")
	default:
		return false, errors.New("Claude third-party thinking 形态不在 lossless SupportEnvelope")
	}
}

func parseClaudeInboundSystem(
	raw json.RawMessage,
	artifact claudeWireArtifact,
	officialIngress bool,
) ([]claudeWireSystemBlock, claudeCanonicalSystemKind, string, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil, claudeCanonicalSystemNone, "", nil
	}
	var blocks []claudeWireSystemBlock
	var plain string
	if json.Unmarshal(trimmed, &plain) == nil {
		if strings.TrimSpace(plain) == "" {
			return nil, "", "", errors.New("Claude candidate system 不能为空")
		}
		blocks = []claudeWireSystemBlock{{Type: "text", Text: plain}}
	} else if err := json.Unmarshal(trimmed, &blocks); err != nil || len(blocks) == 0 {
		return nil, "", "", errors.New("Claude candidate system 不是受支持的文本块数组")
	}
	filtered := make([]claudeWireSystemBlock, 0, len(blocks))
	for index, block := range blocks {
		if block.Type != "text" || block.Text == "" {
			return nil, "", "", errors.New("Claude candidate system 只允许非空 text block")
		}
		if strings.HasPrefix(strings.TrimSpace(block.Text), claudeBillingPrefix) {
			if index != 0 {
				return nil, "", "", errors.New("Claude billing attribution 只能位于首个 system block")
			}
			continue
		}
		cache, err := normalizeClaudeCacheControl(block.CacheControl)
		if err != nil {
			return nil, "", "", err
		}
		block.CacheControl = cache
		filtered = append(filtered, block)
	}
	if len(filtered) == 0 {
		return nil, claudeCanonicalSystemNone, "", nil
	}
	if recognized, scenarioHint := classifyClaudeOfficialSystem(filtered, artifact); recognized {
		if !officialIngress {
			return nil, claudeCanonicalSystemNone, "", nil
		}
		return cloneClaudeSystemBlocks(filtered), claudeCanonicalSystemOfficial, scenarioHint, nil
	}
	for index := range filtered {
		filtered[index].CacheControl = nil
	}
	return filtered, claudeCanonicalSystemCustom, "custom-system", nil
}

func classifyClaudeOfficialSystem(
	blocks []claudeWireSystemBlock,
	artifact claudeWireArtifact,
) (bool, string) {
	if len(blocks) == 0 {
		return false, ""
	}
	scenarios := []struct {
		name     string
		scenario claudeWireScenario
	}{
		{name: "sdk-cli", scenario: artifact.ImplementationPolicy.Scenarios.SDKCLI},
		{name: "agent", scenario: artifact.ImplementationPolicy.Scenarios.Agent},
		{name: "tui-main", scenario: artifact.ImplementationPolicy.Scenarios.TUIMain},
		{name: "tui-title", scenario: artifact.ImplementationPolicy.Scenarios.TUITitle},
		{name: "custom-system", scenario: artifact.ImplementationPolicy.Scenarios.CustomSystem},
		{name: "append-system", scenario: artifact.ImplementationPolicy.Scenarios.AppendSystem},
		{name: "exclude-dynamic", scenario: artifact.ImplementationPolicy.Scenarios.ExcludeDynamic},
		{name: "custom-agent", scenario: artifact.ImplementationPolicy.Scenarios.CustomAgent},
		{name: "web-search-server", scenario: artifact.ImplementationPolicy.Scenarios.WebSearchServer},
	}
	for index, scenario := range artifact.ImplementationPolicy.Scenarios.Background {
		scenarios = append(scenarios, struct {
			name     string
			scenario claudeWireScenario
		}{name: fmt.Sprintf("background-%d", index), scenario: scenario})
	}
	for _, candidate := range scenarios {
		scenario := candidate.scenario
		if len(scenario.SystemBlocks) == 0 || blocks[0].Text != scenario.SystemBlocks[0].Text {
			continue
		}
		if claudeSystemBlocksEqual(blocks, scenario.SystemBlocks) ||
			claudeSystemBlocksEqualIgnoringCache(blocks, scenario.SystemBlocks) {
			return true, candidate.name
		}
	}
	return false, ""
}

func claudeSystemBlocksEqualIgnoringCache(left, right []claudeWireSystemBlock) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index].Type != right[index].Type || left[index].Text != right[index].Text ||
			len(bytes.TrimSpace(left[index].CacheControl)) != 0 {
			return false
		}
	}
	return true
}

func claudeSystemBlocksEqual(left, right []claudeWireSystemBlock) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index].Type != right[index].Type || left[index].Text != right[index].Text ||
			!claudeJSONEqual(left[index].CacheControl, right[index].CacheControl) {
			return false
		}
	}
	return true
}

func claudeApprovedMessagesModel(model string, artifact claudeWireArtifact) bool {
	scenarios := []claudeWireScenario{
		artifact.ImplementationPolicy.Scenarios.SDKCLI,
		artifact.ImplementationPolicy.Scenarios.Agent,
		artifact.ImplementationPolicy.Scenarios.TUIMain,
		artifact.ImplementationPolicy.Scenarios.TUITitle,
		artifact.ImplementationPolicy.Scenarios.Fallback,
	}
	scenarios = append(scenarios, artifact.ImplementationPolicy.Scenarios.Background...)
	for _, scenario := range scenarios {
		if model == scenario.Model {
			return true
		}
	}
	return false
}

func normalizeClaudeCacheControl(raw json.RawMessage) (json.RawMessage, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, nil
	}
	fields, err := decodeClaudeUniqueObject(raw)
	if err != nil {
		return nil, errors.New("Claude system cache_control 必须是对象")
	}
	var value struct {
		Type  string `json:"type"`
		TTL   string `json:"ttl"`
		Scope string `json:"scope"`
	}
	if json.Unmarshal(raw, &value) != nil || value.Type != "ephemeral" ||
		(value.TTL != "" && value.TTL != "1h") || (value.Scope != "" && value.Scope != "global") ||
		len(fields) > 3 {
		return nil, errors.New("Claude system cache_control 不在批准闭集")
	}
	return append(json.RawMessage(nil), bytes.TrimSpace(raw)...), nil
}

func firstClaudeUserText(messages json.RawMessage) (string, error) {
	var items []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if err := json.Unmarshal(messages, &items); err != nil {
		return "", errors.New("Claude candidate messages 无法解析")
	}
	for _, item := range items {
		if item.Role != "user" {
			continue
		}
		var text string
		if json.Unmarshal(item.Content, &text) == nil {
			if isClaudeRealUserText(text) {
				return text, nil
			}
			continue
		}
		var blocks []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if err := json.Unmarshal(item.Content, &blocks); err != nil {
			return "", errors.New("Claude candidate user content 不是受支持的块数组")
		}
		for _, block := range blocks {
			if block.Type == "text" && isClaudeRealUserText(block.Text) {
				return block.Text, nil
			}
		}
	}
	return "", errors.New("Claude candidate 无法取得版本指纹所需的真实用户文本")
}

func isClaudeRealUserText(value string) bool {
	trimmed := strings.TrimSpace(value)
	return trimmed != "" && !strings.HasPrefix(trimmed, "<system-reminder>")
}

func cloneClaudeSystemBlocks(source []claudeWireSystemBlock) []claudeWireSystemBlock {
	out := make([]claudeWireSystemBlock, len(source))
	for index, block := range source {
		out[index] = block
		out[index].CacheControl = append(json.RawMessage(nil), block.CacheControl...)
	}
	return out
}

func claudeVersionFingerprint(artifact claudeWireArtifact, text string) (string, error) {
	if !utf8.ValidString(text) {
		return "", errors.New("Claude 用户文本不是有效 UTF-8")
	}
	runes := []rune(text)
	var selected strings.Builder
	for _, index := range artifact.Attestation.VersionFingerprint.MessageIndexes {
		if index >= 0 && index < len(runes) {
			selected.WriteRune(runes[index])
		} else {
			selected.WriteByte('0')
		}
	}
	sum := sha256.Sum256([]byte(
		artifact.Attestation.VersionFingerprint.Salt + selected.String() + ClaudeFWGVersion,
	))
	encoded := hex.EncodeToString(sum[:])
	return encoded[:artifact.Attestation.VersionFingerprint.HexLength], nil
}

func selectClaudeWireScenario(plan ClaudeEgressPlan, artifact claudeWireArtifact) (claudeWireScenario, error) {
	policy := artifact.ImplementationPolicy.Scenarios
	if claudeModelShapeIsHaiku(plan.modelShape) {
		return policy.Fallback, nil
	}
	if plan.canonical.toolMode == claudeToolModeWebSearchServer {
		return policy.WebSearchServer, nil
	}
	if plan.features.TUITitleRequest || plan.canonical.scenarioHint == "tui-title" {
		if !claudeCanonicalMatchesFixedScenario(plan.canonical, policy.TUITitle) {
			return claudeWireScenario{}, errors.New("Claude TUI title 请求与批准画像不一致")
		}
		return policy.TUITitle, nil
	}
	if plan.identity.background {
		return selectClaudeBackgroundScenario(plan.canonical, policy.Background)
	}
	switch plan.canonical.scenarioHint {
	case "agent":
		return policy.Agent, nil
	case "custom-system":
		return policy.CustomSystem, nil
	case "append-system":
		return policy.AppendSystem, nil
	case "exclude-dynamic":
		return policy.ExcludeDynamic, nil
	case "custom-agent":
		return policy.CustomAgent, nil
	case "tui-main":
		return policy.TUIMain, nil
	case "sdk-cli", "":
	default:
		return claudeWireScenario{}, fmt.Errorf("Claude scenario hint 不在批准闭集：%s", plan.canonical.scenarioHint)
	}
	switch plan.features.SystemMode {
	case ClaudeSystemCustom:
		return policy.CustomSystem, nil
	case ClaudeSystemAppend:
		return policy.AppendSystem, nil
	case ClaudeSystemExcludeDynamic:
		return policy.ExcludeDynamic, nil
	case ClaudeSystemCustomAgent:
		return policy.CustomAgent, nil
	}
	if plan.canonical.systemKind == claudeCanonicalSystemCustom {
		return policy.CustomSystem, nil
	}
	if plan.identity.agentID != "" {
		return policy.Agent, nil
	}
	if plan.identity.entrypoint == ClaudeEntrypointCLI {
		return policy.TUIMain, nil
	}
	return policy.SDKCLI, nil
}

func selectClaudeBackgroundScenario(
	canonical ClaudeCanonicalRequest,
	background []claudeWireScenario,
) (claudeWireScenario, error) {
	if len(background) != 4 {
		return claudeWireScenario{}, errors.New("Claude background 场景没有冻结为四类")
	}
	if strings.HasPrefix(canonical.scenarioHint, "background-") {
		index, err := strconv.Atoi(strings.TrimPrefix(canonical.scenarioHint, "background-"))
		if err != nil || index < 0 || index >= len(background) ||
			!claudeCanonicalMatchesFixedScenario(canonical, background[index]) {
			return claudeWireScenario{}, errors.New("Claude background hint 与静态形态冲突")
		}
		return background[index], nil
	}
	matched := -1
	for index, scenario := range background {
		if !claudeCanonicalMatchesFixedScenario(canonical, scenario) {
			continue
		}
		if matched >= 0 {
			return claudeWireScenario{}, errors.New("Claude background 形态命中多个场景")
		}
		matched = index
	}
	if matched < 0 {
		return claudeWireScenario{}, errors.New("Claude background 形态不在批准闭集")
	}
	return background[matched], nil
}

func claudeCanonicalMatchesFixedScenario(
	canonical ClaudeCanonicalRequest,
	scenario claudeWireScenario,
) bool {
	if canonical.model != scenario.Model || canonical.streamPresent != scenario.StreamPresent ||
		canonical.toolsPresent != scenario.ToolsPresent {
		return false
	}
	if scenario.StreamPresent {
		var expected bool
		if json.Unmarshal(scenario.Stream, &expected) != nil || canonical.stream != expected {
			return false
		}
	}
	if canonical.maxTokens > 0 && !claudeJSONEqual(
		json.RawMessage(strconv.Itoa(canonical.maxTokens)), scenario.MaxTokens,
	) {
		return false
	}
	if !claudeOptionalJSONEqual(canonical.temperature, func() json.RawMessage {
		if scenario.TemperaturePresent {
			return scenario.Temperature
		}
		return nil
	}()) {
		return false
	}
	if !claudeOptionalJSONEqual(canonical.outputConfig, func() json.RawMessage {
		if scenario.OutputConfigPresent {
			return scenario.OutputConfig
		}
		return nil
	}()) {
		return false
	}
	if !claudeJSONEqual(canonical.tools, func() json.RawMessage {
		if scenario.ToolsPresent {
			return scenario.Tools
		}
		return json.RawMessage("[]")
	}()) {
		return false
	}
	return claudeSystemBlocksEqual(canonical.system, scenario.SystemBlocks) ||
		claudeSystemBlocksEqualIgnoringCache(canonical.system, scenario.SystemBlocks)
}

func buildClaudeScenarioSystem(plan ClaudeEgressPlan, scenario claudeWireScenario) []claudeWireSystemBlock {
	base := cloneClaudeSystemBlocks(scenario.SystemBlocks)
	if plan.canonical.systemKind != claudeCanonicalSystemCustom || len(plan.canonical.system) == 0 {
		return base
	}
	if len(base) == 0 {
		return cloneClaudeSystemBlocks(plan.canonical.system)
	}
	templateCache := append(json.RawMessage(nil), base[len(base)-1].CacheControl...)
	out := append([]claudeWireSystemBlock(nil), base[:len(base)-1]...)
	for index, block := range plan.canonical.system {
		block.CacheControl = nil
		if index == len(plan.canonical.system)-1 {
			block.CacheControl = templateCache
		}
		out = append(out, block)
	}
	return out
}

func compileClaudeMessagesBody(
	plan ClaudeEgressPlan,
	artifact claudeWireArtifact,
) ([]byte, string, string, bool, error) {
	if (plan.canonical.toolMode == claudeToolModeAdvisor) != plan.features.AdvisorEnabled {
		return nil, "", "", false, errors.New("Claude advisor 工具与可信 feature 条件不一致")
	}
	if (plan.canonical.toolMode == claudeToolModeWebSearchServer) !=
		plan.features.WebSearchServerEnabled {
		return nil, "", "", false, errors.New(
			"Claude server web_search 工具与可信 feature 条件不一致",
		)
	}
	if plan.canonical.scenarioHint == "agent" && plan.identity.agentID == "" {
		return nil, "", "", false, errors.New("Claude agent 场景缺少可信 agent 身份")
	}
	if plan.canonical.scenarioHint == "tui-title" && plan.identity.entrypoint != ClaudeEntrypointCLI {
		return nil, "", "", false, errors.New("Claude TUI title 场景缺少可信 cli 入口")
	}
	scenario, err := selectClaudeWireScenario(plan, artifact)
	if err != nil {
		return nil, "", "", false, err
	}
	if !claudeModelShapeIsHaiku(plan.modelShape) && plan.canonical.model != scenario.Model {
		return nil, "", "", false, errors.New("Claude 入站模型与画像场景不一致")
	}
	fingerprint, err := claudeVersionFingerprint(artifact, plan.canonical.firstUserText)
	if err != nil {
		return nil, "", "", false, err
	}
	maxTokens := append(json.RawMessage(nil), scenario.MaxTokens...)
	requestedMax := plan.canonical.maxTokens
	if plan.features.MaxTokens > 0 {
		requestedMax = plan.features.MaxTokens
	}
	if requestedMax > 0 {
		maxTokens = json.RawMessage(fmt.Sprintf("%d", requestedMax))
	}
	thinking := json.RawMessage(nil)
	if scenario.ThinkingPresent {
		thinking = append(json.RawMessage(nil), scenario.Thinking...)
	}
	contextManagement := json.RawMessage(nil)
	if scenario.ContextManagementPresent {
		contextManagement = append(json.RawMessage(nil), scenario.ContextManagement...)
	}
	outputConfig := json.RawMessage(nil)
	if scenario.OutputConfigPresent {
		outputConfig = append(json.RawMessage(nil), scenario.OutputConfig...)
	}
	disableThinking := plan.features.DisableThinking || plan.canonical.disableThinking
	if disableThinking {
		thinking = nil
		contextManagement = nil
	}
	effort := plan.features.Effort
	if effort == "" {
		effort = plan.canonical.effort
	}
	if effort != "" {
		outputConfig, _ = marshalClaudeOrderedObject([]claudeJSONField{
			{name: "effort", raw: mustMarshalClaudeString(effort)},
		})
	}
	system := buildClaudeScenarioSystem(plan, scenario)
	if plan.features.DisablePromptCaching {
		for index := range system {
			system[index].CacheControl = nil
		}
	}
	if !plan.features.DisableAttribution {
		attribution := fmt.Sprintf(
			"x-anthropic-billing-header: cc_version=%s.%s; cc_entrypoint=%s; cch=%s;",
			ClaudeFWGVersion, fingerprint, plan.identity.entrypoint, plan.cch,
		)
		if plan.identity.agentID != "" {
			attribution += " cc_is_subagent=true;"
		}
		if plan.identity.previousRequestID != "" {
			attribution += " cc_prev_req=" + plan.identity.previousRequestID + ";"
		}
		if plan.features.Workload != "" {
			attribution += " cc_workload=" + plan.features.Workload + ";"
		}
		system = append([]claudeWireSystemBlock{{Type: "text", Text: attribution}}, system...)
	}
	tools := append(json.RawMessage(nil), plan.canonical.tools...)
	if (len(tools) == 0 || bytes.Equal(bytes.TrimSpace(tools), []byte("[]"))) &&
		scenario.ToolsPresent {
		tools = append(json.RawMessage(nil), scenario.Tools...)
	}
	if plan.features.TUITitleRequest {
		tools = json.RawMessage("[]")
	}
	metadata, err := plan.identity.metadataJSON()
	if err != nil {
		return nil, "", "", false, err
	}
	systemRaw, err := marshalClaudeJSON(system)
	if err != nil {
		return nil, "", "", false, err
	}
	messages := append(json.RawMessage(nil), plan.canonical.messages...)
	if claudeModelShapeIsHaiku(plan.modelShape) {
		messages, err = compileClaudeFallbackMessages(messages)
		if err != nil {
			return nil, "", "", false, err
		}
	}
	fields := []claudeJSONField{
		{name: "model", raw: mustMarshalClaudeString(scenario.Model)},
		{name: "messages", raw: messages},
	}
	if scenario.SystemPresent || len(system) != 0 {
		fields = append(fields, claudeJSONField{name: "system", raw: systemRaw})
	}
	if scenario.ToolsPresent || plan.canonical.toolsPresent ||
		!bytes.Equal(bytes.TrimSpace(tools), []byte("[]")) {
		fields = append(fields, claudeJSONField{name: "tools", raw: tools})
	}
	if len(plan.canonical.toolChoice) != 0 {
		fields = append(fields, claudeJSONField{name: "tool_choice", raw: plan.canonical.toolChoice})
	}
	if scenario.MetadataPresent {
		fields = append(fields, claudeJSONField{name: "metadata", raw: metadata})
	}
	fields = append(fields,
		claudeJSONField{name: "max_tokens", raw: maxTokens},
	)
	if len(thinking) != 0 {
		fields = append(fields, claudeJSONField{name: "thinking", raw: thinking})
	}
	if len(contextManagement) != 0 {
		fields = append(fields, claudeJSONField{name: "context_management", raw: contextManagement})
	}
	if len(outputConfig) != 0 {
		fields = append(fields, claudeJSONField{name: "output_config", raw: outputConfig})
	}
	if scenario.TemperaturePresent {
		fields = append(fields, claudeJSONField{name: "temperature", raw: scenario.Temperature})
	}
	stream := claudeModelShapeIsStream(plan.modelShape) && scenario.StreamPresent
	if stream {
		fields = append(fields, claudeJSONField{name: "stream", raw: json.RawMessage("true")})
	}
	body, err := marshalClaudeOrderedObject(fields)
	if err != nil {
		return nil, "", "", false, err
	}
	if plan.features.RequestGzip {
		body, err = gzipClaudeBody(body)
		if err != nil {
			return nil, "", "", false, err
		}
	}
	betaBase := claudeToolBeta(
		plan.canonical.toolMode, scenario.AnthropicBeta,
		artifact.ImplementationPolicy.ToolPolicy,
	)
	beta := compileClaudeBeta(betaBase, plan.features.CustomBetas)
	return body, scenario.Model, beta, stream, nil
}

func compileClaudeFallbackMessages(messages json.RawMessage) (json.RawMessage, error) {
	var items []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(messages, &items) != nil || len(items) == 0 {
		return nil, errors.New("Claude model fallback 的 messages 非法")
	}
	systemBlocks := make([]claudeWireSystemBlock, 0)
	userBlocks := make([]claudeWireSystemBlock, 0)
	for _, item := range items {
		if item.Role != "system" && item.Role != "user" {
			return nil, errors.New("Claude model fallback 只批准 system/user 文本历史")
		}
		blocks, err := claudeTextContentBlocks(item.Content)
		if err != nil {
			return nil, err
		}
		for index := range blocks {
			blocks[index].CacheControl = nil
		}
		if item.Role == "system" {
			systemBlocks = append(systemBlocks, blocks...)
		} else {
			userBlocks = append(userBlocks, blocks...)
		}
	}
	blocks := append(systemBlocks, userBlocks...)
	if len(blocks) == 0 {
		return nil, errors.New("Claude model fallback 没有文本内容")
	}
	for index := range systemBlocks {
		if !strings.HasSuffix(blocks[index].Text, "\n") {
			blocks[index].Text += "\n"
		}
	}
	blocks[len(blocks)-1].CacheControl = json.RawMessage(`{"type":"ephemeral","ttl":"1h"}`)
	content, err := marshalClaudeJSON(blocks)
	if err != nil {
		return nil, err
	}
	role, _ := marshalClaudeJSON("user")
	message, err := marshalClaudeOrderedObject([]claudeJSONField{
		{name: "role", raw: role}, {name: "content", raw: content},
	})
	if err != nil {
		return nil, err
	}
	return append(append(json.RawMessage("["), message...), ']'), nil
}

func claudeTextContentBlocks(raw json.RawMessage) ([]claudeWireSystemBlock, error) {
	var plain string
	if json.Unmarshal(raw, &plain) == nil {
		if plain == "" {
			return nil, errors.New("Claude fallback 文本不能为空")
		}
		return []claudeWireSystemBlock{{Type: "text", Text: plain}}, nil
	}
	var blocks []claudeWireSystemBlock
	if json.Unmarshal(raw, &blocks) != nil || len(blocks) == 0 {
		return nil, errors.New("Claude fallback content 不是文本块数组")
	}
	for _, block := range blocks {
		if block.Type != "text" || block.Text == "" {
			return nil, errors.New("Claude fallback 只批准 text content")
		}
	}
	return blocks, nil
}

func compileClaudeBeta(base string, custom []string) string {
	if len(custom) == 0 {
		return base
	}
	baseParts := strings.Split(base, ",")
	insertAt := len(baseParts)
	for index, part := range baseParts {
		if part == "effort-2025-11-24" || part == "extended-cache-ttl-2025-04-11" {
			insertAt = index
			break
		}
	}
	customParts := make([]string, 0, len(custom))
	for _, raw := range custom {
		for _, part := range strings.Split(raw, ",") {
			if part = strings.TrimSpace(part); part != "" {
				customParts = append(customParts, part)
			}
		}
	}
	out := append([]string(nil), baseParts[:insertAt]...)
	out = append(out, customParts...)
	out = append(out, baseParts[insertAt:]...)
	return strings.Join(out, ",")
}

type claudeJSONField struct {
	name string
	raw  json.RawMessage
}

func marshalClaudeOrderedObject(fields []claudeJSONField) ([]byte, error) {
	var output bytes.Buffer
	output.WriteByte('{')
	seen := make(map[string]struct{}, len(fields))
	for index, field := range fields {
		if strings.TrimSpace(field.name) == "" || !json.Valid(field.raw) {
			return nil, fmt.Errorf("Claude ordered JSON 字段非法：%s", field.name)
		}
		if _, duplicate := seen[field.name]; duplicate {
			return nil, fmt.Errorf("Claude ordered JSON 字段重复：%s", field.name)
		}
		seen[field.name] = struct{}{}
		if index > 0 {
			output.WriteByte(',')
		}
		name, _ := marshalClaudeJSON(field.name)
		output.Write(name)
		output.WriteByte(':')
		output.Write(bytes.TrimSpace(field.raw))
	}
	output.WriteByte('}')
	return output.Bytes(), nil
}

func marshalClaudeJSON(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func mustMarshalClaudeString(value string) json.RawMessage {
	raw, _ := marshalClaudeJSON(value)
	return raw
}

func gzipClaudeBody(body []byte) ([]byte, error) {
	var output bytes.Buffer
	writer := gzip.NewWriter(&output)
	if _, err := writer.Write(body); err != nil {
		_ = writer.Close()
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func claudeJSONEqual(left, right json.RawMessage) bool {
	if len(bytes.TrimSpace(left)) == 0 || len(bytes.TrimSpace(right)) == 0 {
		return len(bytes.TrimSpace(left)) == len(bytes.TrimSpace(right))
	}
	var leftValue any
	var rightValue any
	return json.Unmarshal(left, &leftValue) == nil && json.Unmarshal(right, &rightValue) == nil &&
		fmt.Sprintf("%#v", leftValue) == fmt.Sprintf("%#v", rightValue)
}

func claudeAttestationDigest(domain string, values ...string) string {
	parts := append([]string{strings.TrimSpace(domain)}, values...)
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}
