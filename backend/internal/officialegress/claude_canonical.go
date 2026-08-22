package officialegress

import (
	"encoding/json"
	"errors"
	"strings"
	"unicode/utf8"
)

// projectCanonicalRequestToClaude 是 Claude PersonaPlanner 的方言投影。
// 共享入站适配器不读取 Claude 画像，也不生成任何 Claude wire 字段。
func projectCanonicalRequestToClaude(
	canonical CanonicalRequest,
	artifact claudeWireArtifact,
) (ClaudeCanonicalRequest, error) {
	if strings.TrimSpace(canonical.IngressProtocol) == "" ||
		canonical.IngressProtocol == "anthropic-messages" {
		return ClaudeCanonicalRequest{}, errors.New("共享 CanonicalRequest 缺少第三方协议身份")
	}
	primaryModel, _, err := resolveClaudeMessagesModel(canonical.Model, artifact, false)
	if err != nil {
		return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 模型不在 Claude SupportEnvelope")
	}
	if len(canonical.Messages) == 0 || strings.TrimSpace(canonical.FirstUserText) == "" {
		return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 缺少对话或真实用户文本")
	}
	type wireTextBlock struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	type wireMessage struct {
		Role    string          `json:"role"`
		Content []wireTextBlock `json:"content"`
	}
	messages := make([]wireMessage, 0, len(canonical.Messages))
	for _, message := range canonical.Messages {
		if message.Role != "user" && message.Role != "assistant" {
			return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 含 Claude 无法表达的消息角色")
		}
		if len(message.Content) == 0 {
			return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 含空消息")
		}
		blocks := make([]wireTextBlock, 0, len(message.Content))
		for _, part := range message.Content {
			if !utf8.ValidString(part.Text) || strings.TrimSpace(part.Text) == "" {
				return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 含空或非法文本块")
			}
			blocks = append(blocks, wireTextBlock{Type: "text", Text: part.Text})
		}
		messages = append(messages, wireMessage{Role: message.Role, Content: blocks})
	}
	messagesRaw, err := marshalClaudeJSON(messages)
	if err != nil {
		return ClaudeCanonicalRequest{}, err
	}
	system := make([]claudeWireSystemBlock, 0, len(canonical.System))
	for _, part := range canonical.System {
		if !utf8.ValidString(part.Text) || strings.TrimSpace(part.Text) == "" {
			return ClaudeCanonicalRequest{}, errors.New("CanonicalRequest 含空或非法 system 文本")
		}
		system = append(system, claudeWireSystemBlock{Type: "text", Text: part.Text})
	}
	systemKind := claudeCanonicalSystemNone
	scenarioHint := ""
	if len(system) > 0 {
		systemKind = claudeCanonicalSystemCustom
		scenarioHint = "custom-system"
	}
	return ClaudeCanonicalRequest{
		model:           primaryModel,
		primaryModel:    primaryModel,
		messages:        messagesRaw,
		tools:           json.RawMessage("[]"),
		toolMode:        claudeToolModeNone,
		system:          system,
		systemKind:      systemKind,
		scenarioHint:    scenarioHint,
		firstUserText:   canonical.FirstUserText,
		maxTokens:       canonical.MaxOutputTokens,
		effort:          canonical.ReasoningEffort,
		streamPresent:   canonical.StreamSet,
		stream:          canonical.Stream,
		officialIngress: false,
	}, nil
}
