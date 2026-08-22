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
	"slices"
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
	primaryModel             string
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
	thinkingDisplay          string
	contextManagement        json.RawMessage
	contextManagementPresent bool
	fallbacks                json.RawMessage
	fallbacksPresent         bool
	streamPresent            bool
	stream                   bool
	disableThinking          bool
	officialIngress          bool
	serverFallback           bool
	fallbackLatchedBy        string
	refusalFallback          bool
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
	return parseClaudeCanonicalMessagesWithCatalog(
		body, trusted, artifact, officialIngress, claudeOfficialIngressMatch{}, false,
	)
}

func parseClaudeCanonicalMessagesWithCatalog(
	body []byte,
	trusted ClaudeTrustedFacts,
	artifact claudeWireArtifact,
	officialIngress bool,
	match claudeOfficialIngressMatch,
	catalogValidated bool,
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
		"fallbacks": {}, "stream": {}, "temperature": {}, "tool_choice": {},
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
	modelRaw, ok := document["model"]
	if !ok {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate 缺少 model")
	}
	var model string
	if json.Unmarshal(modelRaw, &model) != nil || strings.TrimSpace(model) == "" {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New("Claude candidate model 必须是非空字符串")
	}
	model = strings.TrimSpace(model)
	primaryModel, serverFallback, modelErr := resolveClaudeMessagesModel(
		model, artifact, officialIngress,
	)
	if modelErr != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
			"Claude candidate model 不在 messages SupportEnvelope",
		)
	}
	fallbacksRaw, fallbacksPresent := document["fallbacks"]
	if fallbacksPresent {
		if !officialIngress {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
				"Claude third-party 不得控制官方 fallbacks",
			)
		}
		if !rawJSONArray(fallbacksRaw) {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
				"Claude candidate fallbacks 必须是数组",
			)
		}
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
	effort, outputConfig, officialTitle, err := parseClaudeRequestedOutputConfig(
		document["output_config"], artifact, officialIngress, catalogValidated,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	temperature, err := parseClaudeRequestedTemperature(document["temperature"], officialIngress)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	thinkingRaw, thinkingPresent := document["thinking"]
	thinkingDisplay, disableThinking, err := parseClaudeRequestedThinking(
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
	if err != nil && catalogValidated &&
		digestClaudeRaw(tools) == match.toolCatalogDigest {
		approvedTools = append(json.RawMessage(nil), bytes.TrimSpace(tools)...)
		approvedChoice = normalizeClaudeOptionalRaw(document["tool_choice"])
		toolMode = claudeToolModeOfficialCatalog
		err = nil
	}
	if err != nil && officialIngress {
		var matched bool
		approvedTools, approvedChoice, toolMode, matched =
			compileClaudeOfficialScenarioTools(
				tools, document["tool_choice"], primaryModel, scenarioHint, artifact,
			)
		if matched {
			err = nil
		}
	}
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
		model: model, primaryModel: primaryModel,
		messages: append(json.RawMessage(nil), bytes.TrimSpace(messages)...),
		tools:    approvedTools, toolChoice: approvedChoice, toolMode: toolMode,
		toolsPresent: toolsPresent,
		system:       system, systemKind: systemKind, scenarioHint: scenarioHint,
		firstUserText: firstUserText,
		maxTokens:     maxTokens, effort: effort, outputConfig: outputConfig,
		temperature:              temperature,
		thinking:                 append(json.RawMessage(nil), bytes.TrimSpace(thinkingRaw)...),
		thinkingPresent:          thinkingPresent,
		thinkingDisplay:          thinkingDisplay,
		contextManagement:        append(json.RawMessage(nil), bytes.TrimSpace(contextRaw)...),
		contextManagementPresent: contextPresent,
		fallbacks:                append(json.RawMessage(nil), bytes.TrimSpace(fallbacksRaw)...),
		fallbacksPresent:         fallbacksPresent,
		streamPresent:            streamPresent, stream: stream,
		disableThinking: disableThinking, officialIngress: officialIngress,
		serverFallback: serverFallback,
	}
	titleNormalized, err := normalizeClaudeOfficialTitleRequest(
		&canonical, artifact, officialTitle,
	)
	if err != nil {
		return ClaudeCanonicalRequest{}, TranslationReport{}, err
	}
	if catalogValidated && !titleNormalized && !match.nativeTargetScenario {
		canonical.system = cloneClaudeSystemBlocks(
			artifact.ImplementationPolicy.Scenarios.SDKCLI.SystemBlocks,
		)
		canonical.systemKind = claudeCanonicalSystemOfficial
		canonical.scenarioHint = "sdk-cli"
		canonical.officialIngress = true
	}
	if officialIngress {
		if err := refineClaudeOfficialModelScenario(&canonical, artifact); err != nil {
			return ClaudeCanonicalRequest{}, TranslationReport{}, err
		}
	}
	if officialIngress && trusted.Agent.Background {
		capability, ok := claudeModelCapabilityForAlias(artifact, canonical.primaryModel)
		if !ok {
			return ClaudeCanonicalRequest{}, TranslationReport{}, errors.New(
				"Claude 官方 background 模型能力未登记",
			)
		}
		matched := -1
		for index, scenario := range capability.Scenarios.Background {
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
		Compatibility:   "persona_strict_official_catalog",
		Fields:          claudeMessagesTranslationFields(document),
	}
	if titleNormalized {
		report.Compatibility = "official_desktop_title_to_tui_title"
	}
	return canonical, report, nil
}

func claudeMessagesTranslationFields(
	document map[string]json.RawMessage,
) []TranslationFieldDisposition {
	fields := make([]TranslationFieldDisposition, 0, len(document))
	for _, name := range claudeOfficialIngressBodyOrder {
		if _, present := document[name]; !present {
			continue
		}
		disposition := TranslationDispositionPreserved
		reason := "官方语义字段保持不变"
		switch name {
		case "system", "metadata":
			disposition = TranslationDispositionNormalized
			reason = "由目标 OAuth Persona 按 active Release 重建"
		case "model", "max_tokens", "thinking", "context_management", "output_config":
			disposition = TranslationDispositionNormalized
			reason = "按 active Release 的已批准语义场景归一化"
		case "fallbacks":
			disposition = TranslationDispositionLocal
			reason = "仅用于 Persona 内部重试状态机"
		}
		fields = append(fields, TranslationFieldDisposition{
			SourcePath: "$." + name, CanonicalPath: name,
			Disposition: disposition, Reason: reason,
		})
	}
	return fields
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
	officialTitleAllowed bool,
) (string, json.RawMessage, bool, error) {
	if len(raw) == 0 {
		return "", nil, false, nil
	}
	fields, err := decodeClaudeUniqueObject(raw)
	if err != nil {
		return "", nil, false, errors.New("Claude output_config 必须是对象")
	}
	if len(fields) == 1 && len(fields["effort"]) != 0 {
		var effort string
		if json.Unmarshal(fields["effort"], &effort) != nil || !validClaudeEffort(effort) {
			return "", nil, false, errors.New("Claude output_config.effort 不在批准闭集")
		}
		return effort, append(json.RawMessage(nil), bytes.TrimSpace(raw)...), false, nil
	}
	if officialTitleAllowed && len(fields) == 1 && len(fields["format"]) != 0 &&
		claudeTitleFormatEqual(fields["format"], artifact) {
		return "", nil, false, errors.New("Claude Desktop 标题 output_config 形态未登记")
	}
	if officialIngress && len(fields) == 1 && len(fields["format"]) != 0 {
		for _, scenario := range append(
			[]claudeWireScenario{artifact.ImplementationPolicy.Scenarios.TUITitle},
			artifact.ImplementationPolicy.Scenarios.Background...,
		) {
			if scenario.OutputConfigPresent && claudeJSONEqual(raw, scenario.OutputConfig) {
				return "", append(json.RawMessage(nil), bytes.TrimSpace(raw)...), false, nil
			}
		}
	}
	if officialTitleAllowed && len(fields) == 2 && len(fields["effort"]) != 0 &&
		len(fields["format"]) != 0 && claudeTitleFormatEqual(fields["format"], artifact) {
		var effort string
		if json.Unmarshal(fields["effort"], &effort) != nil || !validClaudeEffort(effort) {
			return "", nil, false, errors.New("Claude output_config.effort 不在批准闭集")
		}
		return effort, append(json.RawMessage(nil), bytes.TrimSpace(raw)...), true, nil
	}
	return "", nil, false, errors.New("Claude output_config 不在批准闭集")
}

func claudeTitleFormatEqual(raw json.RawMessage, artifact claudeWireArtifact) bool {
	titleFields, err := decodeClaudeUniqueObject(
		artifact.ImplementationPolicy.Scenarios.TUITitle.OutputConfig,
	)
	return err == nil && len(titleFields) == 1 &&
		claudeJSONEqual(raw, titleFields["format"])
}

const claudeDesktopTitlePromptSHA256 = "765b5ba2fa0a315a3c749c7e54bf5cef450084a745eb01e66371b8b6359d4752"

func normalizeClaudeOfficialTitleRequest(
	canonical *ClaudeCanonicalRequest,
	artifact claudeWireArtifact,
	candidate bool,
) (bool, error) {
	if !candidate {
		return false, nil
	}
	if canonical == nil || !canonical.officialIngress {
		return false, errors.New("Claude Desktop 标题语义来源非法")
	}
	capability, ok := claudeModelCapabilityForAlias(artifact, canonical.primaryModel)
	if !ok {
		return false, errors.New("Claude Desktop 标题模型不在能力目录")
	}
	title := capability.Scenarios.TUITitle
	main := capability.Scenarios.SDKCLI
	sourceThinkingAllowed := canonical.thinkingPresent &&
		claudeJSONEqual(canonical.thinking, title.Thinking)
	// Desktop 2.1.237.3c9 的 Fable 标题请求实测省略 thinking。这里只把
	// “Fable + 完整标题语义闭集 + thinking 缺省”视为显式关闭的等价表达；
	// Sonnet、Opus 以及任何携带其他 thinking 形态的请求仍然 fail-close。
	if capability.CanonicalModel == "claude-fable-5" && !canonical.thinkingPresent &&
		len(bytes.TrimSpace(canonical.thinking)) == 0 && !canonical.disableThinking {
		sourceThinkingAllowed = true
	}
	if canonical.model != main.Model || canonical.maxTokens != 64000 ||
		canonical.effort != "high" || !canonical.streamPresent || !canonical.stream ||
		!canonical.toolsPresent || canonical.toolMode != claudeToolModeNone ||
		len(canonical.toolChoice) != 0 || !claudeJSONEqual(canonical.tools, json.RawMessage("[]")) ||
		!sourceThinkingAllowed ||
		canonical.contextManagementPresent || len(canonical.temperature) != 0 {
		return false, errors.New("Claude Desktop 标题请求不在批准语义闭集")
	}
	if canonical.systemKind != claudeCanonicalSystemCustom ||
		canonical.scenarioHint != "custom-system" || len(canonical.system) != 2 ||
		len(artifact.Messages.SystemBlocks) == 0 ||
		canonical.system[0].Type != "text" ||
		canonical.system[0].Text != artifact.Messages.SystemBlocks[0].Text ||
		len(bytes.TrimSpace(canonical.system[0].CacheControl)) != 0 ||
		canonical.system[1].Type != "text" ||
		len(bytes.TrimSpace(canonical.system[1].CacheControl)) != 0 {
		return false, errors.New("Claude Desktop 标题 system 不在批准语义闭集")
	}
	promptDigest := sha256.Sum256([]byte(canonical.system[1].Text))
	if hex.EncodeToString(promptDigest[:]) != claudeDesktopTitlePromptSHA256 {
		return false, errors.New("Claude Desktop 标题提示词摘要不在批准闭集")
	}
	var maxTokens int
	var stream bool
	if json.Unmarshal(title.MaxTokens, &maxTokens) != nil ||
		json.Unmarshal(title.Stream, &stream) != nil || !stream {
		return false, errors.New("Claude TUI title 画像不完整")
	}
	canonical.model = title.Model
	canonical.tools = append(json.RawMessage(nil), title.Tools...)
	canonical.toolChoice = nil
	canonical.toolMode = claudeToolModeNone
	canonical.toolsPresent = title.ToolsPresent
	canonical.system = cloneClaudeSystemBlocks(title.SystemBlocks)
	canonical.systemKind = claudeCanonicalSystemOfficial
	canonical.scenarioHint = "tui-title"
	canonical.maxTokens = maxTokens
	canonical.effort = ""
	canonical.outputConfig = append(json.RawMessage(nil), title.OutputConfig...)
	canonical.temperature = append(json.RawMessage(nil), title.Temperature...)
	canonical.thinking = append(json.RawMessage(nil), title.Thinking...)
	canonical.thinkingPresent = title.ThinkingPresent
	canonical.thinkingDisplay = ""
	canonical.contextManagement = nil
	canonical.contextManagementPresent = false
	canonical.fallbacks = nil
	canonical.fallbacksPresent = false
	canonical.streamPresent = title.StreamPresent
	canonical.stream = stream
	canonical.disableThinking = false
	return true, nil
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
) (string, bool, error) {
	if len(raw) == 0 {
		return "", false, nil
	}
	fields, err := decodeClaudeUniqueObject(raw)
	if err != nil {
		return "", false, errors.New("Claude thinking 必须是对象")
	}
	var kind string
	if json.Unmarshal(fields["type"], &kind) != nil {
		return "", false, errors.New("Claude thinking.type 非法")
	}
	switch kind {
	case "adaptive":
		if len(fields) == 1 {
			return "", false, nil
		}
		if len(fields) != 2 || len(fields["display"]) == 0 {
			return "", false, errors.New("Claude adaptive thinking 含未批准字段")
		}
		var display string
		if json.Unmarshal(fields["display"], &display) != nil ||
			!slices.Contains(artifact.ImplementationPolicy.Thinking.DisplayValues, display) {
			return "", false, errors.New("Claude thinking.display 不在批准闭集")
		}
		return display, false, nil
	case "disabled":
		if len(fields) != 1 {
			return "", false, errors.New("Claude disabled thinking 含未批准字段")
		}
		// 官方 TUI/background 的固定场景会显式发送 disabled；第三方入站的
		// 同一形态表达受管关闭开关，Compiler 应省略 thinking 与 context。
		return "", !officialIngress, nil
	case "enabled":
		if officialIngress && claudeJSONEqual(
			raw, artifact.ImplementationPolicy.Scenarios.Fallback.Thinking,
		) {
			return "", false, nil
		}
		return "", false, errors.New("Claude enabled thinking 不在批准官方场景")
	default:
		return "", false, errors.New("Claude third-party thinking 形态不在 lossless SupportEnvelope")
	}
}

func compileClaudeAdaptiveThinking(
	policy claudeWireThinkingPolicy,
	display string,
) (json.RawMessage, error) {
	if display != "" && !slices.Contains(policy.DisplayValues, display) {
		return nil, errors.New("Claude thinking.display 不在批准闭集")
	}
	fields := make([]claudeJSONField, 0, len(policy.FieldOrder))
	for _, name := range policy.FieldOrder {
		switch name {
		case "type":
			fields = append(fields, claudeJSONField{
				name: name, raw: mustMarshalClaudeString(policy.Type),
			})
		case "display":
			if display != "" {
				fields = append(fields, claudeJSONField{
					name: name, raw: mustMarshalClaudeString(display),
				})
			}
		default:
			return nil, errors.New("Claude thinking 画像字段顺序非法")
		}
	}
	return marshalClaudeOrderedObject(fields)
}

func claudeCanonicalThinkingMatchesScenario(
	canonical ClaudeCanonicalRequest,
	scenario claudeWireScenario,
	policy claudeWireThinkingPolicy,
) bool {
	if !canonical.thinkingPresent || !scenario.ThinkingPresent {
		return false
	}
	if canonical.thinkingDisplay == "" {
		return claudeJSONEqual(canonical.thinking, scenario.Thinking)
	}
	base, err := compileClaudeAdaptiveThinking(policy, "")
	if err != nil || !claudeJSONEqual(scenario.Thinking, base) {
		return false
	}
	expected, err := compileClaudeAdaptiveThinking(policy, canonical.thinkingDisplay)
	return err == nil && claudeJSONEqual(canonical.thinking, expected)
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
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		for _, candidate := range claudeNamedModelScenarios(capability) {
			scenarios = append(scenarios, struct {
				name     string
				scenario claudeWireScenario
			}{
				name:     claudeCatalogScenarioHint(capability.CanonicalModel, candidate.name),
				scenario: candidate.scenario,
			})
		}
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

type claudeNamedScenario struct {
	name     string
	scenario claudeWireScenario
}

func claudeNamedModelScenarios(capability claudeWireModelCapability) []claudeNamedScenario {
	set := capability.Scenarios
	out := []claudeNamedScenario{
		{name: "sdk-cli-background-agent", scenario: set.SDKCLIBackgroundAgent},
		{name: "agent-background", scenario: set.AgentBackground},
		{name: "web-search-server", scenario: set.WebSearchServer},
		{name: "web-search-outer", scenario: set.WebSearchOuter},
		{name: "tui-title", scenario: set.TUITitle},
	}
	for index, scenario := range set.Background {
		out = append(out, claudeNamedScenario{
			name: fmt.Sprintf("background-%d", index), scenario: scenario,
		})
	}
	if capability.LegacyRetryFallbackSupported {
		out = append(out, claudeNamedScenario{name: "fallback", scenario: set.Fallback})
	}
	out = append(out,
		claudeNamedScenario{name: "agent", scenario: set.Agent},
		claudeNamedScenario{name: "custom-system", scenario: set.CustomSystem},
		claudeNamedScenario{name: "append-system", scenario: set.AppendSystem},
		claudeNamedScenario{name: "exclude-dynamic", scenario: set.ExcludeDynamic},
		claudeNamedScenario{name: "custom-agent", scenario: set.CustomAgent},
		claudeNamedScenario{name: "tui-main", scenario: set.TUIMain},
		claudeNamedScenario{name: "sdk-cli", scenario: set.SDKCLI},
	)
	if set.ServerFallback != nil {
		out = append([]claudeNamedScenario{{
			name: "server-fallback", scenario: *set.ServerFallback,
		}}, out...)
	}
	return slices.DeleteFunc(out, func(candidate claudeNamedScenario) bool {
		return !validClaudeWireScenario(candidate.scenario)
	})
}

const claudeCatalogHintPrefix = "model-capability:"

func claudeCatalogScenarioHint(model, scenario string) string {
	return claudeCatalogHintPrefix + model + ":" + scenario
}

func parseClaudeCatalogScenarioHint(value string) (string, string, bool) {
	if !strings.HasPrefix(value, claudeCatalogHintPrefix) {
		return "", value, false
	}
	parts := strings.SplitN(strings.TrimPrefix(value, claudeCatalogHintPrefix), ":", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
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

func resolveClaudeMessagesModel(
	model string,
	artifact claudeWireArtifact,
	officialIngress bool,
) (string, bool, error) {
	if capability, ok := claudeModelCapabilityForAlias(artifact, model); ok {
		return capability.CanonicalModel, false, nil
	}
	if !officialIngress {
		return "", false, errors.New("第三方模型别名未登记")
	}
	if capability, ok := claudeModelCapabilityForServerFallback(artifact, model); ok {
		return capability.CanonicalModel, true, nil
	}
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		for _, candidate := range claudeNamedModelScenarios(capability) {
			if candidate.name != "server-fallback" && candidate.scenario.Model == model {
				return capability.CanonicalModel, false, nil
			}
		}
	}
	return "", false, errors.New("官方辅助模型形态未登记")
}

func refineClaudeOfficialModelScenario(
	canonical *ClaudeCanonicalRequest,
	artifact claudeWireArtifact,
) error {
	if canonical == nil || !canonical.officialIngress {
		return errors.New("Claude 官方模型场景缺少 canonical request")
	}
	if hintedModel, hintedScenario, ok := parseClaudeCatalogScenarioHint(canonical.scenarioHint); ok {
		if canonical.primaryModel != "" && canonical.primaryModel != hintedModel &&
			canonical.model != artifact.ImplementationPolicy.Scenarios.TUITitle.Model {
			return errors.New("Claude 官方模型与 system 画像冲突")
		}
		canonical.primaryModel = hintedModel
		canonical.scenarioHint = hintedScenario
	}
	modelIsPrimary := false
	if capability, ok := claudeModelCapabilityForAlias(artifact, canonical.model); ok {
		canonical.primaryModel = capability.CanonicalModel
		modelIsPrimary = true
	}
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		if modelIsPrimary && capability.CanonicalModel != canonical.primaryModel {
			continue
		}
		if !modelIsPrimary && canonical.scenarioHint == "tui-title" &&
			canonical.primaryModel != "" && capability.CanonicalModel != canonical.primaryModel {
			continue
		}
		for _, candidate := range claudeNamedModelScenarios(capability) {
			if candidate.scenario.Model != canonical.model ||
				!claudeCanonicalMatchesFixedScenario(*canonical, candidate.scenario) {
				continue
			}
			canonical.primaryModel = capability.CanonicalModel
			canonical.scenarioHint = candidate.name
			canonical.serverFallback = candidate.name == "server-fallback"
			return nil
		}
	}
	if modelIsPrimary {
		return nil
	}
	return errors.New("Claude 官方辅助模型请求不在实测场景闭集")
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
			_, _ = selected.WriteRune(runes[index])
		} else {
			_ = selected.WriteByte('0')
		}
	}
	sum := sha256.Sum256([]byte(
		artifact.Attestation.VersionFingerprint.Salt + selected.String() + artifact.Identity.Version,
	))
	encoded := hex.EncodeToString(sum[:])
	return encoded[:artifact.Attestation.VersionFingerprint.HexLength], nil
}

func selectClaudeWireScenario(plan ClaudeEgressPlan, artifact claudeWireArtifact) (claudeWireScenario, error) {
	capability, err := claudeModelCapabilityForPlan(plan, artifact)
	if err != nil {
		return claudeWireScenario{}, err
	}
	policy := capability.Scenarios
	if plan.canonical.serverFallback {
		if policy.ServerFallback == nil ||
			plan.canonical.model != policy.ServerFallback.Model {
			return claudeWireScenario{}, errors.New("Claude server fallback 不在模型能力目录")
		}
		return *policy.ServerFallback, nil
	}
	if claudeModelShapeIsHaiku(plan.modelShape) {
		if !capability.LegacyRetryFallbackSupported {
			return claudeWireScenario{}, errors.New(
				"Claude 当前模型不支持 Sonnet 历史 Haiku fallback",
			)
		}
		return policy.Fallback, nil
	}
	if plan.canonical.toolMode == claudeToolModeWebSearchServer {
		return policy.WebSearchServer, nil
	}
	if plan.canonical.toolMode == claudeToolModeWebSearchOuter {
		return policy.WebSearchOuter, nil
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
	case "web-search-outer":
		return policy.WebSearchOuter, nil
	case "custom-system":
		return policy.CustomSystem, nil
	case "append-system":
		return policy.AppendSystem, nil
	case "exclude-dynamic":
		return policy.ExcludeDynamic, nil
	case "custom-agent":
		return policy.CustomAgent, nil
	case "sdk-cli-background-agent":
		if !validClaudeWireScenario(policy.SDKCLIBackgroundAgent) {
			return claudeWireScenario{}, errors.New("Claude SDK CLI background agent 场景未取证")
		}
		return policy.SDKCLIBackgroundAgent, nil
	case "agent-background":
		if !validClaudeWireScenario(policy.AgentBackground) {
			return claudeWireScenario{}, errors.New("Claude agent background 场景未取证")
		}
		return policy.AgentBackground, nil
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

func claudeModelCapabilityForPlan(
	plan ClaudeEgressPlan,
	artifact claudeWireArtifact,
) (claudeWireModelCapability, error) {
	if capability, ok := claudeModelCapabilityForAlias(artifact, plan.canonical.primaryModel); ok {
		return capability, nil
	}
	if capability, ok := claudeModelCapabilityForAlias(artifact, plan.canonical.model); ok {
		return capability, nil
	}
	if plan.canonical.serverFallback {
		if capability, ok := claudeModelCapabilityForServerFallback(
			artifact, plan.canonical.model,
		); ok {
			return capability, nil
		}
	}
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		for _, candidate := range claudeNamedModelScenarios(capability) {
			if candidate.name == plan.canonical.scenarioHint &&
				candidate.scenario.Model == plan.canonical.model {
				return capability, nil
			}
		}
	}
	return claudeWireModelCapability{}, errors.New("Claude 请求缺少已登记模型能力")
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
		canonical.toolsPresent != scenario.ToolsPresent ||
		canonical.thinkingPresent != scenario.ThinkingPresent ||
		canonical.contextManagementPresent != scenario.ContextManagementPresent ||
		canonical.fallbacksPresent != scenario.FallbacksPresent {
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
	if !claudeOptionalJSONEqual(canonical.thinking, func() json.RawMessage {
		if scenario.ThinkingPresent {
			return scenario.Thinking
		}
		return nil
	}()) || !claudeOptionalJSONEqual(canonical.contextManagement, func() json.RawMessage {
		if scenario.ContextManagementPresent {
			return scenario.ContextManagement
		}
		return nil
	}()) || !claudeOptionalJSONEqual(canonical.fallbacks, func() json.RawMessage {
		if scenario.FallbacksPresent {
			return scenario.Fallbacks
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
	if plan.canonical.thinkingDisplay != "" {
		base, err := compileClaudeAdaptiveThinking(
			artifact.ImplementationPolicy.Thinking, "",
		)
		if err != nil || !claudeJSONEqual(thinking, base) {
			return nil, "", "", false, errors.New(
				"Claude thinking.display 与画像场景冲突",
			)
		}
		thinking, err = compileClaudeAdaptiveThinking(
			artifact.ImplementationPolicy.Thinking, plan.canonical.thinkingDisplay,
		)
		if err != nil {
			return nil, "", "", false, err
		}
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
			artifact.Identity.Version, fingerprint, plan.identity.entrypoint, plan.cch,
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
	fieldValues := map[string]json.RawMessage{
		"model":      mustMarshalClaudeString(scenario.Model),
		"messages":   messages,
		"max_tokens": maxTokens,
	}
	if scenario.SystemPresent || len(system) != 0 {
		fieldValues["system"] = systemRaw
	}
	if scenario.ToolsPresent || plan.canonical.toolsPresent ||
		!bytes.Equal(bytes.TrimSpace(tools), []byte("[]")) {
		fieldValues["tools"] = tools
	}
	if len(plan.canonical.toolChoice) != 0 {
		fieldValues["tool_choice"] = plan.canonical.toolChoice
	}
	if scenario.MetadataPresent {
		fieldValues["metadata"] = metadata
	}
	if len(thinking) != 0 {
		fieldValues["thinking"] = thinking
	}
	if len(contextManagement) != 0 {
		fieldValues["context_management"] = contextManagement
	}
	if scenario.FallbacksPresent {
		fieldValues["fallbacks"] = scenario.Fallbacks
	}
	if len(outputConfig) != 0 {
		fieldValues["output_config"] = outputConfig
	}
	if scenario.TemperaturePresent {
		fieldValues["temperature"] = scenario.Temperature
	}
	stream := claudeModelShapeIsStream(plan.modelShape) && scenario.StreamPresent
	if stream {
		fieldValues["stream"] = json.RawMessage("true")
	}
	fields, err := orderClaudeScenarioBodyFields(scenario.BodyOrder, fieldValues)
	if err != nil {
		return nil, "", "", false, err
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

func orderClaudeScenarioBodyFields(
	order []string,
	values map[string]json.RawMessage,
) ([]claudeJSONField, error) {
	if len(order) == 0 {
		return nil, errors.New("Claude 场景缺少实测 Body 字段顺序")
	}
	fields := make([]claudeJSONField, 0, len(values))
	seen := make(map[string]struct{}, len(order))
	for _, name := range order {
		if _, duplicate := seen[name]; duplicate {
			return nil, fmt.Errorf("Claude 场景 Body 顺序字段重复：%s", name)
		}
		seen[name] = struct{}{}
		if raw, present := values[name]; present {
			fields = append(fields, claudeJSONField{name: name, raw: raw})
		}
	}
	if raw, present := values["tool_choice"]; present {
		if _, ordered := seen["tool_choice"]; !ordered {
			insertAt := -1
			for index := range fields {
				if fields[index].name == "tools" {
					insertAt = index + 1
					break
				}
			}
			if insertAt < 0 {
				return nil, errors.New("Claude 动态 tool_choice 缺少 tools 顺序锚点")
			}
			fields = append(fields, claudeJSONField{})
			copy(fields[insertAt+1:], fields[insertAt:])
			fields[insertAt] = claudeJSONField{name: "tool_choice", raw: raw}
			seen["tool_choice"] = struct{}{}
		}
	}
	for name := range values {
		if _, ordered := seen[name]; !ordered {
			return nil, fmt.Errorf("Claude 场景 Body 顺序缺少活动字段：%s", name)
		}
	}
	return fields, nil
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
	_ = output.WriteByte('{')
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
			_ = output.WriteByte(',')
		}
		name, _ := marshalClaudeJSON(field.name)
		_, _ = output.Write(name)
		_ = output.WriteByte(':')
		_, _ = output.Write(bytes.TrimSpace(field.raw))
	}
	_ = output.WriteByte('}')
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
