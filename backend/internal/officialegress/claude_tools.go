package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

type claudeToolMode string

const (
	claudeToolModeNone             claudeToolMode = "none"
	claudeToolModeStructuredOutput claudeToolMode = "structured-output"
	claudeToolModeAgent            claudeToolMode = "agent"
	claudeToolModeBash             claudeToolMode = "bash"
	claudeToolModeMCPDeferred      claudeToolMode = "mcp-deferred"
	claudeToolModeAdvisor          claudeToolMode = "advisor"
	claudeToolModeBackground       claudeToolMode = "background"
	claudeToolModeWebSearchOuter   claudeToolMode = "web-search-outer"
	claudeToolModeWebSearchServer  claudeToolMode = "web-search-server"
	claudeToolModeDynamic          claudeToolMode = "dynamic-client"
)

var claudeDynamicToolNamePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// claudeDynamicToolCatalogLimit 只冻结 2.1.226 官方样本中观测到的最大目录，
// 供证据收据校验使用；它不是第三方规范化语义请求的运行时准入上限。
const claudeDynamicToolCatalogLimit = 33

// compileClaudeApprovedTools 把规范化工具意图收敛到 FW-F 已实测目录，或验证为
// 2.1.226 已实测的标准动态工具描述符。未知特殊工具、截断的 deferred 目录和
// 伪造的 server tool 一律 fail-close。
func compileClaudeApprovedTools(
	toolsRaw json.RawMessage,
	toolChoiceRaw json.RawMessage,
	artifact claudeWireArtifact,
) (json.RawMessage, json.RawMessage, claudeToolMode, error) {
	toolsRaw = bytes.TrimSpace(toolsRaw)
	toolChoiceRaw = normalizeClaudeOptionalRaw(toolChoiceRaw)
	if len(toolsRaw) == 0 || bytes.Equal(toolsRaw, []byte("[]")) {
		if len(toolChoiceRaw) != 0 {
			return nil, nil, "", errors.New("Claude 空工具集合不得携带 tool_choice")
		}
		return json.RawMessage("[]"), nil, claudeToolModeNone, nil
	}
	if !rawJSONArray(toolsRaw) {
		return nil, nil, "", errors.New("Claude tools 必须是 JSON 数组")
	}
	policy := artifact.ImplementationPolicy.ToolPolicy
	for _, candidate := range []struct {
		mode    claudeToolMode
		catalog claudeWireToolCatalog
	}{
		{claudeToolModeAgent, policy.Agent},
		{claudeToolModeBash, policy.Bash},
		{claudeToolModeMCPDeferred, policy.MCPDeferred},
		{claudeToolModeAdvisor, policy.Advisor},
		{claudeToolModeBackground, policy.Background},
		{claudeToolModeWebSearchOuter, policy.WebSearchOuter},
		{claudeToolModeWebSearchServer, policy.WebSearchServer},
	} {
		if !claudeJSONEqual(toolsRaw, candidate.catalog.Tools) {
			continue
		}
		wantChoice := normalizeClaudeOptionalRaw(candidate.catalog.ToolChoice)
		if !claudeOptionalJSONEqual(toolChoiceRaw, wantChoice) {
			return nil, nil, "", fmt.Errorf(
				"Claude %s 工具的 tool_choice 与批准画像不一致", candidate.mode,
			)
		}
		return append(json.RawMessage(nil), candidate.catalog.Tools...),
			append(json.RawMessage(nil), wantChoice...), candidate.mode, nil
	}
	structured, ok, err := compileClaudeStructuredOutputTool(
		toolsRaw, policy.StructuredOutput.Tools,
	)
	if err != nil {
		return nil, nil, "", err
	}
	if ok {
		if len(toolChoiceRaw) != 0 {
			return nil, nil, "", errors.New("Claude StructuredOutput 不接受 tool_choice")
		}
		return structured, nil, claudeToolModeStructuredOutput, nil
	}
	dynamic, ok, err := compileClaudeDynamicTools(toolsRaw, toolChoiceRaw)
	if err != nil {
		return nil, nil, "", err
	}
	if ok {
		return dynamic, nil, claudeToolModeDynamic, nil
	}
	return nil, nil, "", errors.New("Claude tools 不在 FW-F 已批准 ToolPolicy 闭集")
}

// compileClaudeOfficialScenarioTools 只承接模型目录中已取证官方场景的精确工具形态。
// 第三方入口不会调用此路径，不能借模型专属工具目录扩大动态工具准入范围。
func compileClaudeOfficialScenarioTools(
	toolsRaw json.RawMessage,
	toolChoiceRaw json.RawMessage,
	primaryModel string,
	scenarioHint string,
	artifact claudeWireArtifact,
) (json.RawMessage, json.RawMessage, claudeToolMode, bool) {
	hintedModel, hintedScenario, qualified := parseClaudeCatalogScenarioHint(scenarioHint)
	if qualified {
		if primaryModel != "" && primaryModel != hintedModel {
			return nil, nil, "", false
		}
		primaryModel = hintedModel
		scenarioHint = hintedScenario
	}
	capability, ok := claudeModelCapabilityForAlias(artifact, primaryModel)
	if !ok || scenarioHint == "" {
		return nil, nil, "", false
	}
	for _, candidate := range claudeNamedModelScenarios(capability) {
		if candidate.name != scenarioHint || !candidate.scenario.ToolsPresent ||
			!claudeJSONEqual(toolsRaw, candidate.scenario.Tools) {
			continue
		}
		wantChoice, choiceKnown := claudeOfficialScenarioToolChoice(
			candidate.name, candidate.scenario, artifact.ImplementationPolicy.ToolPolicy,
		)
		if !choiceKnown || !claudeOptionalJSONEqual(toolChoiceRaw, wantChoice) {
			return nil, nil, "", false
		}
		mode, modeKnown := claudeOfficialScenarioToolMode(candidate.name)
		if !modeKnown {
			return nil, nil, "", false
		}
		return append(json.RawMessage(nil), candidate.scenario.Tools...),
			append(json.RawMessage(nil), wantChoice...), mode, true
	}
	return nil, nil, "", false
}

func claudeOfficialScenarioToolChoice(
	name string,
	scenario claudeWireScenario,
	policy claudeWireToolPolicy,
) (json.RawMessage, bool) {
	for _, field := range scenario.BodyOrder {
		if field != "tool_choice" {
			continue
		}
		if name != "web-search-server" {
			return nil, false
		}
		return normalizeClaudeOptionalRaw(policy.WebSearchServer.ToolChoice), true
	}
	return nil, true
}

func claudeOfficialScenarioToolMode(name string) (claudeToolMode, bool) {
	switch {
	case name == "agent", name == "agent-background",
		name == "sdk-cli-background-agent", name == "server-fallback":
		return claudeToolModeAgent, true
	case strings.HasPrefix(name, "background-"):
		return claudeToolModeBackground, true
	case name == "web-search-outer":
		return claudeToolModeWebSearchOuter, true
	case name == "web-search-server":
		return claudeToolModeWebSearchServer, true
	default:
		return "", false
	}
}

func compileClaudeDynamicTools(
	toolsRaw json.RawMessage,
	toolChoiceRaw json.RawMessage,
) (json.RawMessage, bool, error) {
	var tools []json.RawMessage
	if json.Unmarshal(toolsRaw, &tools) != nil || len(tools) == 0 {
		return nil, false, nil
	}
	if len(toolChoiceRaw) != 0 {
		return nil, false, errors.New("Claude 动态工具目录不接受未实测 tool_choice")
	}
	seen := make(map[string]struct{}, len(tools))
	for _, raw := range tools {
		fields, err := decodeClaudeUniqueObject(raw)
		if err != nil {
			return nil, false, errors.New("Claude 动态工具必须是对象")
		}
		for field := range fields {
			switch field {
			case "name", "description", "input_schema":
			default:
				return nil, false, nil
			}
		}
		var name string
		if json.Unmarshal(fields["name"], &name) != nil ||
			!claudeDynamicToolNamePattern.MatchString(name) {
			return nil, false, errors.New("Claude 动态工具 name 非法")
		}
		if _, duplicate := seen[name]; duplicate {
			return nil, false, errors.New("Claude 动态工具 name 重复")
		}
		seen[name] = struct{}{}
		var description string
		if json.Unmarshal(fields["description"], &description) != nil ||
			strings.TrimSpace(description) == "" {
			return nil, false, errors.New("Claude 动态工具 description 非法")
		}
		schema, err := decodeClaudeUniqueObject(fields["input_schema"])
		if err != nil {
			return nil, false, errors.New("Claude 动态工具 input_schema 必须是对象")
		}
		var schemaType string
		if json.Unmarshal(schema["type"], &schemaType) != nil || schemaType != "object" {
			return nil, false, errors.New("Claude 动态工具 input_schema.type 必须是 object")
		}
	}
	return append(json.RawMessage(nil), toolsRaw...), true, nil
}

func compileClaudeStructuredOutputTool(
	incoming json.RawMessage,
	template json.RawMessage,
) (json.RawMessage, bool, error) {
	var incomingItems []json.RawMessage
	var templateItems []json.RawMessage
	if json.Unmarshal(incoming, &incomingItems) != nil || len(incomingItems) != 1 ||
		json.Unmarshal(template, &templateItems) != nil || len(templateItems) != 1 {
		return nil, false, nil
	}
	incomingFields, err := decodeClaudeUniqueObject(incomingItems[0])
	if err != nil {
		return nil, false, errors.New("Claude StructuredOutput 工具必须是对象")
	}
	var name string
	if json.Unmarshal(incomingFields["name"], &name) != nil || name != "StructuredOutput" {
		return nil, false, nil
	}
	for field := range incomingFields {
		if field != "name" && field != "description" && field != "input_schema" {
			return nil, false, fmt.Errorf("Claude StructuredOutput 含未批准字段：%s", field)
		}
	}
	schema := bytes.TrimSpace(incomingFields["input_schema"])
	if len(schema) == 0 {
		return nil, false, errors.New("Claude StructuredOutput 缺少 input_schema")
	}
	if _, err := decodeClaudeUniqueObject(schema); err != nil {
		return nil, false, errors.New("Claude StructuredOutput input_schema 必须是对象")
	}
	templateFields, err := decodeClaudeUniqueObject(templateItems[0])
	if err != nil {
		return nil, false, errors.New("Claude ToolPolicy 的 StructuredOutput 模板非法")
	}
	if description := bytes.TrimSpace(incomingFields["description"]); len(description) != 0 &&
		!claudeJSONEqual(description, templateFields["description"]) {
		return nil, false, errors.New("Claude StructuredOutput description 不属于官方工具语义")
	}
	tool, err := marshalClaudeOrderedObject([]claudeJSONField{
		{name: "name", raw: append(json.RawMessage(nil), templateFields["name"]...)},
		{name: "description", raw: append(json.RawMessage(nil), templateFields["description"]...)},
		{name: "input_schema", raw: append(json.RawMessage(nil), schema...)},
	})
	if err != nil {
		return nil, false, err
	}
	return append(append(json.RawMessage("["), tool...), ']'), true, nil
}

func normalizeClaudeOptionalRaw(raw json.RawMessage) json.RawMessage {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil
	}
	return append(json.RawMessage(nil), trimmed...)
}

func claudeOptionalJSONEqual(left, right json.RawMessage) bool {
	left = normalizeClaudeOptionalRaw(left)
	right = normalizeClaudeOptionalRaw(right)
	if len(left) == 0 || len(right) == 0 {
		return len(left) == len(right)
	}
	return claudeJSONEqual(left, right)
}

func claudeToolBeta(
	mode claudeToolMode,
	base string,
	policy claudeWireToolPolicy,
) string {
	switch mode {
	case claudeToolModeAdvisor:
		return policy.Advisor.AnthropicBeta
	case claudeToolModeWebSearchServer:
		return policy.WebSearchServer.AnthropicBeta
	default:
		return strings.TrimSpace(base)
	}
}
