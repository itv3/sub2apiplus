package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
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
)

// compileClaudeApprovedTools 把规范化工具意图收敛到 FW-F 已实测目录。
// 未知名称、截断的 MCP 目录或伪造的官方描述一律 fail-close。
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
	return nil, nil, "", errors.New("Claude tools 不在 FW-F 已批准 ToolPolicy 闭集")
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
