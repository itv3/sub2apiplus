package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"
)

const (
	IngressProtocolOpenAIChatCompletions = "openai-chat-completions"
	IngressProtocolOpenAIResponses       = "openai-responses"
)

const (
	TranslationDispositionPreserved  = "preserved"
	TranslationDispositionNormalized = "normalized_lossless"
	TranslationDispositionLocal      = "protocol_local"
	TranslationDispositionRejected   = "rejected"
)

// CanonicalTextPart 保存一段有顺序的纯文本语义。
type CanonicalTextPart struct {
	Text string
}

// CanonicalMessage 保存与厂商协议无关的消息角色和文本块顺序。
type CanonicalMessage struct {
	Role    string
	Content []CanonicalTextPart
}

// CanonicalRequest 是标准入站协议适配器输出的规范化语义请求。
type CanonicalRequest struct {
	IngressProtocol    string
	Model              string
	System             []CanonicalTextPart
	Messages           []CanonicalMessage
	MaxOutputTokens    int
	MaxOutputTokensSet bool
	ReasoningEffort    string
	Stream             bool
	StreamSet          bool
	IncludeUsage       bool
	FirstUserText      string
}

// TranslationFieldDisposition 逐字段记录来源、规范化目标和处置结论。
type TranslationFieldDisposition struct {
	SourcePath    string `json:"source_path"`
	CanonicalPath string `json:"canonical_path,omitempty"`
	Disposition   string `json:"disposition"`
	Reason        string `json:"reason,omitempty"`
}

// TranslationReport 明确记录当前请求是否完成逐字段无损翻译。
type TranslationReport struct {
	IngressProtocol  string                        `json:"ingress_protocol"`
	Lossless         bool                          `json:"lossless"`
	Compatibility    string                        `json:"compatibility"`
	Fields           []TranslationFieldDisposition `json:"fields"`
	RejectionReasons []string                      `json:"rejection_reasons,omitempty"`
}

// IngressProtocolAdapter 只负责协议语义，不接触账号、Persona 或最终 wire。
type IngressProtocolAdapter interface {
	Protocol() string
	Adapt(body []byte) (CanonicalRequest, TranslationReport, error)
}

type openAIChatCompletionsIngressAdapter struct{}
type openAIResponsesIngressAdapter struct{}

func (openAIChatCompletionsIngressAdapter) Protocol() string {
	return IngressProtocolOpenAIChatCompletions
}

func (openAIResponsesIngressAdapter) Protocol() string {
	return IngressProtocolOpenAIResponses
}

// AdaptIngressProtocol 使用已登记的共享适配器生成规范化语义和逐字段报告。
func AdaptIngressProtocol(
	protocol string,
	body []byte,
) (CanonicalRequest, TranslationReport, error) {
	var adapter IngressProtocolAdapter
	switch strings.TrimSpace(protocol) {
	case IngressProtocolOpenAIChatCompletions:
		adapter = openAIChatCompletionsIngressAdapter{}
	case IngressProtocolOpenAIResponses:
		adapter = openAIResponsesIngressAdapter{}
	default:
		return CanonicalRequest{}, TranslationReport{}, fmt.Errorf(
			"未登记的入站协议适配器：%s", protocol,
		)
	}
	return adapter.Adapt(body)
}

func (openAIChatCompletionsIngressAdapter) Adapt(
	body []byte,
) (CanonicalRequest, TranslationReport, error) {
	report := newTranslationReport(IngressProtocolOpenAIChatCompletions)
	document, order, err := decodeIngressObject(body)
	if err != nil {
		return rejectCanonical(report, "$", fmt.Sprintf("Chat Completions 请求体非法：%v", err))
	}
	allowed := map[string]struct{}{
		"model": {}, "messages": {}, "max_tokens": {}, "max_completion_tokens": {},
		"reasoning_effort": {}, "stream": {}, "stream_options": {}, "store": {},
		"tools": {}, "tool_choice": {}, "parallel_tool_calls": {}, "response_format": {},
		"temperature": {}, "top_p": {}, "stop": {},
	}
	if name := firstUnknownIngressField(order, allowed); name != "" {
		return rejectCanonical(report, name, "Chat Completions strict 入口不支持未知字段："+name)
	}
	for _, name := range []string{"temperature", "top_p", "stop"} {
		if _, present := document[name]; present {
			return rejectCanonical(report, name, "Chat Completions strict 入口不接受采样或停止控制："+name)
		}
	}

	canonical := CanonicalRequest{IngressProtocol: IngressProtocolOpenAIChatCompletions}
	model, err := requireIngressString(document, "model", false)
	if err != nil {
		return rejectCanonical(report, "model", err.Error())
	}
	canonical.Model = model
	recordTranslation(&report, "model", "model", TranslationDispositionPreserved, "")

	messagesRaw, present := document["messages"]
	if !present {
		return rejectCanonical(report, "messages", "Chat Completions 缺少 messages")
	}
	canonical.System, canonical.Messages, canonical.FirstUserText, err =
		adaptChatMessages(messagesRaw, &report)
	if err != nil {
		return rejectCanonical(report, "messages", err.Error())
	}
	recordTranslation(&report, "messages", "system+messages", TranslationDispositionNormalized,
		"前置 system 与对话消息被分离，角色、文本块和顺序保持不变")

	canonical.MaxOutputTokens, canonical.MaxOutputTokensSet, err =
		adaptChatMaxOutputTokens(document, &report)
	if err != nil {
		return rejectCanonical(report, "max_tokens", err.Error())
	}
	canonical.ReasoningEffort, err = adaptIngressEffort(document["reasoning_effort"])
	if err != nil {
		return rejectCanonical(report, "reasoning_effort", err.Error())
	}
	if _, present := document["reasoning_effort"]; present {
		recordTranslation(&report, "reasoning_effort", "reasoning_effort", TranslationDispositionPreserved, "")
	}
	canonical.Stream, canonical.StreamSet, err = adaptIngressBool(document, "stream")
	if err != nil {
		return rejectCanonical(report, "stream", err.Error())
	}
	if canonical.StreamSet {
		recordTranslation(&report, "stream", "stream", TranslationDispositionPreserved, "")
	}
	canonical.IncludeUsage, err = adaptChatStreamOptions(document["stream_options"], canonical.Stream)
	if err != nil {
		return rejectCanonical(report, "stream_options", err.Error())
	}
	if _, present := document["stream_options"]; present {
		recordTranslation(&report, "stream_options", "response.include_usage", TranslationDispositionLocal,
			"只控制 Chat Completions 返回帧，不进入 Persona wire")
	}
	if err := validateFalseOnly(document, "store"); err != nil {
		return rejectCanonical(report, "store", err.Error())
	}
	if _, present := document["store"]; present {
		recordTranslation(&report, "store", "response.store", TranslationDispositionLocal,
			"store=false 不改变上游推理语义")
	}
	if err := validateEmptyArrayOnly(document, "tools"); err != nil {
		return rejectCanonical(report, "tools", err.Error())
	}
	if _, present := document["tools"]; present {
		recordTranslation(&report, "tools", "tools", TranslationDispositionLocal,
			"空工具目录不贡献第三方工具语义")
	}
	if err := validateNoneOnly(document, "tool_choice"); err != nil {
		return rejectCanonical(report, "tool_choice", err.Error())
	}
	if _, present := document["tool_choice"]; present {
		recordTranslation(&report, "tool_choice", "tools", TranslationDispositionLocal,
			"空工具目录下 tool_choice=none 与缺省等价")
	}
	if err := validateFalseOnly(document, "parallel_tool_calls"); err != nil {
		return rejectCanonical(report, "parallel_tool_calls", err.Error())
	}
	if _, present := document["parallel_tool_calls"]; present {
		recordTranslation(&report, "parallel_tool_calls", "tools", TranslationDispositionLocal,
			"空工具目录下 false 与缺省等价")
	}
	if err := validateChatTextFormat(document["response_format"]); err != nil {
		return rejectCanonical(report, "response_format", err.Error())
	}
	if _, present := document["response_format"]; present {
		recordTranslation(&report, "response_format", "response.text_format", TranslationDispositionLocal,
			"默认文本输出不增加结构化输出约束")
	}
	report.Lossless = true
	report.Compatibility = "lossless"
	return canonical, report, nil
}

func (openAIResponsesIngressAdapter) Adapt(
	body []byte,
) (CanonicalRequest, TranslationReport, error) {
	report := newTranslationReport(IngressProtocolOpenAIResponses)
	document, order, err := decodeIngressObject(body)
	if err != nil {
		return rejectCanonical(report, "$", fmt.Sprintf("Responses 请求体非法：%v", err))
	}
	allowed := map[string]struct{}{
		"model": {}, "input": {}, "instructions": {}, "max_output_tokens": {},
		"reasoning": {}, "stream": {}, "store": {}, "tools": {}, "tool_choice": {},
		"parallel_tool_calls": {}, "text": {}, "include": {}, "temperature": {},
		"top_p": {}, "previous_response_id": {}, "conversation": {}, "background": {},
	}
	if name := firstUnknownIngressField(order, allowed); name != "" {
		return rejectCanonical(report, name, "Responses strict 入口不支持未知字段："+name)
	}
	for _, name := range []string{
		"temperature", "top_p", "previous_response_id", "conversation", "background",
	} {
		if _, present := document[name]; present {
			return rejectCanonical(report, name, "Responses strict 入口不接受有状态、后台或采样控制："+name)
		}
	}

	canonical := CanonicalRequest{IngressProtocol: IngressProtocolOpenAIResponses}
	model, err := requireIngressString(document, "model", false)
	if err != nil {
		return rejectCanonical(report, "model", err.Error())
	}
	canonical.Model = model
	recordTranslation(&report, "model", "model", TranslationDispositionPreserved, "")

	instructions := make([]CanonicalTextPart, 0, 1)
	if raw, present := document["instructions"]; present {
		text, textErr := parseIngressNonEmptyString(raw, "Responses instructions")
		if textErr != nil {
			return rejectCanonical(report, "instructions", textErr.Error())
		}
		instructions = append(instructions, CanonicalTextPart{Text: text})
		recordTranslation(&report, "instructions", "system[0]", TranslationDispositionPreserved, "")
	}
	inputRaw, present := document["input"]
	if !present {
		return rejectCanonical(report, "input", "Responses 缺少 input")
	}
	inputSystem, messages, firstUserText, err := adaptResponsesInput(inputRaw, &report)
	if err != nil {
		return rejectCanonical(report, "input", err.Error())
	}
	canonical.System = append(instructions, inputSystem...)
	canonical.Messages = messages
	canonical.FirstUserText = firstUserText
	recordTranslation(&report, "input", "system+messages", TranslationDispositionNormalized,
		"前置 system 与对话消息被分离，角色、文本块和顺序保持不变")

	canonical.MaxOutputTokens, canonical.MaxOutputTokensSet, err =
		adaptPositiveTokenLimit(document["max_output_tokens"], "max_output_tokens")
	if err != nil {
		return rejectCanonical(report, "max_output_tokens", err.Error())
	}
	if canonical.MaxOutputTokensSet {
		recordTranslation(&report, "max_output_tokens", "max_output_tokens", TranslationDispositionPreserved, "")
	}
	canonical.ReasoningEffort, err = adaptResponsesEffort(document["reasoning"], &report)
	if err != nil {
		return rejectCanonical(report, "reasoning", err.Error())
	}
	canonical.Stream, canonical.StreamSet, err = adaptIngressBool(document, "stream")
	if err != nil {
		return rejectCanonical(report, "stream", err.Error())
	}
	if canonical.StreamSet {
		recordTranslation(&report, "stream", "stream", TranslationDispositionPreserved, "")
	}
	if err := validateFalseOnly(document, "store"); err != nil {
		return rejectCanonical(report, "store", err.Error())
	}
	if _, present := document["store"]; present {
		recordTranslation(&report, "store", "response.store", TranslationDispositionLocal,
			"store=false 不要求上游保存会话状态")
	}
	if err := validateEmptyArrayOnly(document, "tools"); err != nil {
		return rejectCanonical(report, "tools", err.Error())
	}
	if _, present := document["tools"]; present {
		recordTranslation(&report, "tools", "tools", TranslationDispositionLocal,
			"空工具目录不贡献第三方工具语义")
	}
	if err := validateNoneOnly(document, "tool_choice"); err != nil {
		return rejectCanonical(report, "tool_choice", err.Error())
	}
	if _, present := document["tool_choice"]; present {
		recordTranslation(&report, "tool_choice", "tools", TranslationDispositionLocal,
			"空工具目录下 tool_choice=none 与缺省等价")
	}
	if err := validateFalseOnly(document, "parallel_tool_calls"); err != nil {
		return rejectCanonical(report, "parallel_tool_calls", err.Error())
	}
	if _, present := document["parallel_tool_calls"]; present {
		recordTranslation(&report, "parallel_tool_calls", "tools", TranslationDispositionLocal,
			"空工具目录下 false 与缺省等价")
	}
	if err := validateResponsesTextFormat(document["text"]); err != nil {
		return rejectCanonical(report, "text", err.Error())
	}
	if _, present := document["text"]; present {
		recordTranslation(&report, "text", "response.text_format", TranslationDispositionLocal,
			"默认文本输出不增加结构化输出约束")
	}
	if err := validateEmptyArrayOnly(document, "include"); err != nil {
		return rejectCanonical(report, "include", err.Error())
	}
	if _, present := document["include"]; present {
		recordTranslation(&report, "include", "response.include", TranslationDispositionLocal,
			"空 include 不要求额外响应载荷")
	}
	report.Lossless = true
	report.Compatibility = "lossless"
	return canonical, report, nil
}

func newTranslationReport(protocol string) TranslationReport {
	return TranslationReport{IngressProtocol: protocol, Compatibility: "rejected"}
}

func rejectCanonical(
	report TranslationReport,
	path string,
	reason string,
) (CanonicalRequest, TranslationReport, error) {
	report.Lossless = false
	report.Compatibility = "rejected"
	report.RejectionReasons = append(report.RejectionReasons, reason)
	recordTranslation(&report, path, "", TranslationDispositionRejected, reason)
	return CanonicalRequest{}, report, errors.New(reason)
}

func recordTranslation(
	report *TranslationReport,
	sourcePath string,
	canonicalPath string,
	disposition string,
	reason string,
) {
	if report == nil {
		return
	}
	report.Fields = append(report.Fields, TranslationFieldDisposition{
		SourcePath: sourcePath, CanonicalPath: canonicalPath,
		Disposition: disposition, Reason: reason,
	})
}

func decodeIngressObject(body []byte) (map[string]json.RawMessage, []string, error) {
	if len(bytes.TrimSpace(body)) == 0 {
		return nil, nil, errors.New("请求体为空")
	}
	fields, err := decodeClaudeOrderedObject(body)
	if err != nil {
		return nil, nil, err
	}
	document := make(map[string]json.RawMessage, len(fields))
	order := make([]string, 0, len(fields))
	for _, field := range fields {
		document[field.name] = append(json.RawMessage(nil), field.raw...)
		order = append(order, field.name)
	}
	return document, order, nil
}

func firstUnknownIngressField(order []string, allowed map[string]struct{}) string {
	for _, name := range order {
		if _, ok := allowed[name]; !ok {
			return name
		}
	}
	return ""
}

func requireIngressString(
	document map[string]json.RawMessage,
	name string,
	allowEmpty bool,
) (string, error) {
	raw, present := document[name]
	if !present {
		return "", fmt.Errorf("缺少 %s", name)
	}
	var value string
	if json.Unmarshal(raw, &value) != nil {
		return "", fmt.Errorf("%s 必须是有效的非空字符串", name)
	}
	trimmed := strings.TrimSpace(value)
	if !utf8.ValidString(value) || trimmed != value ||
		(!allowEmpty && trimmed == "") {
		return "", fmt.Errorf("%s 必须是有效的非空字符串", name)
	}
	return value, nil
}

func parseIngressNonEmptyString(raw json.RawMessage, label string) (string, error) {
	var value string
	if json.Unmarshal(raw, &value) != nil || !utf8.ValidString(value) || strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("%s 必须是有效的非空字符串", label)
	}
	return value, nil
}

func adaptChatMessages(
	raw json.RawMessage,
	report *TranslationReport,
) ([]CanonicalTextPart, []CanonicalMessage, string, error) {
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || len(items) == 0 {
		return nil, nil, "", errors.New("Chat Completions messages 必须是非空数组")
	}
	return adaptIngressMessages(items, IngressProtocolOpenAIChatCompletions, report)
}

func adaptResponsesInput(
	raw json.RawMessage,
	report *TranslationReport,
) ([]CanonicalTextPart, []CanonicalMessage, string, error) {
	var plain string
	if json.Unmarshal(raw, &plain) == nil {
		if !utf8.ValidString(plain) || strings.TrimSpace(plain) == "" {
			return nil, nil, "", errors.New("Responses input 文本不能为空")
		}
		recordTranslation(report, "input", "messages[0].content[0]", TranslationDispositionNormalized,
			"Responses 字符串 input 等价为单条 user 文本消息")
		return nil, []CanonicalMessage{{
			Role: "user", Content: []CanonicalTextPart{{Text: plain}},
		}}, plain, nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || len(items) == 0 {
		return nil, nil, "", errors.New("Responses input 必须是非空字符串或消息数组")
	}
	return adaptIngressMessages(items, IngressProtocolOpenAIResponses, report)
}

func adaptIngressMessages(
	items []json.RawMessage,
	protocol string,
	report *TranslationReport,
) ([]CanonicalTextPart, []CanonicalMessage, string, error) {
	system := make([]CanonicalTextPart, 0)
	messages := make([]CanonicalMessage, 0, len(items))
	firstUserText := ""
	conversationStarted := false
	for index, raw := range items {
		fields, order, err := decodeIngressObject(raw)
		if err != nil {
			return nil, nil, "", fmt.Errorf("消息 %d 不是唯一字段对象：%w", index, err)
		}
		allowed := map[string]struct{}{"role": {}, "content": {}}
		if protocol == IngressProtocolOpenAIResponses {
			allowed["type"] = struct{}{}
		}
		if name := firstUnknownIngressField(order, allowed); name != "" {
			return nil, nil, "", fmt.Errorf("消息 %d 含未批准字段：%s", index, name)
		}
		if protocol == IngressProtocolOpenAIResponses {
			if typeRaw, present := fields["type"]; present {
				var kind string
				if json.Unmarshal(typeRaw, &kind) != nil || kind != "message" {
					return nil, nil, "", fmt.Errorf(
						"Responses input[%d] 子路径不是文本 message", index,
					)
				}
				recordTranslation(report, fmt.Sprintf("input[%d].type", index),
					fmt.Sprintf("messages[%d]", index), TranslationDispositionLocal,
					"type=message 只声明 Responses item 类型")
			}
		}
		role, err := requireIngressString(fields, "role", false)
		if err != nil {
			return nil, nil, "", fmt.Errorf("消息 %d role 非法", index)
		}
		if role != "system" && role != "user" && role != "assistant" {
			return nil, nil, "", fmt.Errorf("消息 %d role 不在无损闭集：%s", index, role)
		}
		contentRaw, present := fields["content"]
		if !present {
			return nil, nil, "", fmt.Errorf("消息 %d 缺少 content", index)
		}
		parts, err := adaptIngressTextContent(contentRaw, protocol, role, index, report)
		if err != nil {
			return nil, nil, "", err
		}
		if role == "system" {
			if conversationStarted {
				return nil, nil, "", fmt.Errorf("消息 %d 的 system 不是前置 system", index)
			}
			system = append(system, parts...)
			continue
		}
		conversationStarted = true
		messages = append(messages, CanonicalMessage{Role: role, Content: parts})
		if role == "user" && firstUserText == "" {
			for _, part := range parts {
				if isClaudeRealUserText(part.Text) {
					firstUserText = part.Text
					break
				}
			}
		}
	}
	if len(messages) == 0 {
		return nil, nil, "", errors.New("规范化请求缺少 user/assistant 对话消息")
	}
	if firstUserText == "" {
		return nil, nil, "", errors.New("规范化请求缺少真实 user 文本")
	}
	return system, messages, firstUserText, nil
}

func adaptIngressTextContent(
	raw json.RawMessage,
	protocol string,
	role string,
	messageIndex int,
	report *TranslationReport,
) ([]CanonicalTextPart, error) {
	var plain string
	if json.Unmarshal(raw, &plain) == nil {
		if !utf8.ValidString(plain) || strings.TrimSpace(plain) == "" {
			return nil, fmt.Errorf("消息 %d 文本不能为空", messageIndex)
		}
		recordTranslation(report, fmt.Sprintf("messages[%d].content", messageIndex),
			fmt.Sprintf("messages[%d].content[0]", messageIndex), TranslationDispositionPreserved, "")
		return []CanonicalTextPart{{Text: plain}}, nil
	}
	var blocks []json.RawMessage
	if json.Unmarshal(raw, &blocks) != nil || len(blocks) == 0 {
		return nil, fmt.Errorf("消息 %d content 不是非空文本或文本块数组", messageIndex)
	}
	parts := make([]CanonicalTextPart, 0, len(blocks))
	for blockIndex, blockRaw := range blocks {
		fields, order, err := decodeIngressObject(blockRaw)
		if err != nil {
			return nil, fmt.Errorf("消息 %d content[%d] 不是唯一字段对象", messageIndex, blockIndex)
		}
		allowed := map[string]struct{}{"type": {}, "text": {}}
		if name := firstUnknownIngressField(order, allowed); name != "" {
			return nil, fmt.Errorf("消息 %d content[%d] 含未批准字段：%s", messageIndex, blockIndex, name)
		}
		kind, err := requireIngressString(fields, "type", false)
		if err != nil {
			return nil, fmt.Errorf("消息 %d content[%d].type 非法", messageIndex, blockIndex)
		}
		expected := "text"
		if protocol == IngressProtocolOpenAIResponses {
			expected = "input_text"
			if role == "assistant" {
				expected = "output_text"
			}
		}
		if kind != expected {
			return nil, fmt.Errorf(
				"消息 %d content[%d] 不是批准的 %s 文本块", messageIndex, blockIndex, expected,
			)
		}
		text, err := parseIngressNonEmptyString(fields["text"],
			fmt.Sprintf("消息 %d content[%d].text", messageIndex, blockIndex))
		if err != nil {
			return nil, fmt.Errorf("消息 %d content[%d].text 不能为空", messageIndex, blockIndex)
		}
		parts = append(parts, CanonicalTextPart{Text: text})
		recordTranslation(report,
			fmt.Sprintf("messages[%d].content[%d]", messageIndex, blockIndex),
			fmt.Sprintf("messages[%d].content[%d]", messageIndex, blockIndex),
			TranslationDispositionNormalized, "协议文本块类型已规范化，文本与块顺序保持不变")
	}
	return parts, nil
}

func adaptChatMaxOutputTokens(
	document map[string]json.RawMessage,
	report *TranslationReport,
) (int, bool, error) {
	maxTokensRaw, maxTokensPresent := document["max_tokens"]
	completionRaw, completionPresent := document["max_completion_tokens"]
	if maxTokensPresent && completionPresent {
		return 0, false, errors.New("max_tokens 与 max_completion_tokens 不能同时出现")
	}
	if completionPresent {
		value, present, err := adaptPositiveTokenLimit(completionRaw, "max_completion_tokens")
		if err == nil {
			recordTranslation(report, "max_completion_tokens", "max_output_tokens",
				TranslationDispositionNormalized, "字段名规范化，数值保持不变")
		}
		return value, present, err
	}
	value, present, err := adaptPositiveTokenLimit(maxTokensRaw, "max_tokens")
	if err == nil && present {
		recordTranslation(report, "max_tokens", "max_output_tokens",
			TranslationDispositionNormalized, "字段名规范化，数值保持不变")
	}
	return value, present, err
}

func adaptPositiveTokenLimit(raw json.RawMessage, name string) (int, bool, error) {
	if len(raw) == 0 {
		return 0, false, nil
	}
	var value int
	if json.Unmarshal(raw, &value) != nil || value <= 0 || value > 64000 {
		return 0, false, fmt.Errorf("%s 必须是 1..64000 的整数", name)
	}
	return value, true, nil
}

func adaptIngressEffort(raw json.RawMessage) (string, error) {
	if len(raw) == 0 {
		return "", nil
	}
	var effort string
	if json.Unmarshal(raw, &effort) != nil || strings.TrimSpace(effort) != effort ||
		!validClaudeEffort(effort) {
		return "", errors.New("reasoning effort 不在批准闭集")
	}
	return effort, nil
}

func adaptResponsesEffort(raw json.RawMessage, report *TranslationReport) (string, error) {
	if len(raw) == 0 {
		return "", nil
	}
	fields, order, err := decodeIngressObject(raw)
	if err != nil {
		return "", errors.New("Responses reasoning 必须是对象")
	}
	if name := firstUnknownIngressField(order, map[string]struct{}{"effort": {}}); name != "" {
		return "", fmt.Errorf("Responses reasoning 含未批准字段：%s", name)
	}
	effort, err := adaptIngressEffort(fields["effort"])
	if err != nil || effort == "" {
		return "", errors.New("Responses reasoning.effort 必须位于批准闭集")
	}
	recordTranslation(report, "reasoning.effort", "reasoning_effort",
		TranslationDispositionNormalized, "字段路径规范化，effort 取值保持不变")
	return effort, nil
}

func adaptIngressBool(
	document map[string]json.RawMessage,
	name string,
) (bool, bool, error) {
	raw, present := document[name]
	if !present {
		return false, false, nil
	}
	switch string(bytes.TrimSpace(raw)) {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, false, fmt.Errorf("%s 必须是布尔值", name)
	}
}

func adaptChatStreamOptions(raw json.RawMessage, stream bool) (bool, error) {
	if len(raw) == 0 {
		return false, nil
	}
	if !stream {
		return false, errors.New("stream_options 只能用于 stream=true")
	}
	fields, order, err := decodeIngressObject(raw)
	if err != nil {
		return false, errors.New("stream_options 必须是对象")
	}
	if name := firstUnknownIngressField(order, map[string]struct{}{"include_usage": {}}); name != "" {
		return false, fmt.Errorf("stream_options 含未批准字段：%s", name)
	}
	if len(fields) == 0 {
		return false, nil
	}
	valueRaw, present := fields["include_usage"]
	if !present {
		return false, errors.New("stream_options.include_usage 缺失")
	}
	switch string(bytes.TrimSpace(valueRaw)) {
	case "true":
		return true, nil
	case "false":
		return false, nil
	default:
		return false, errors.New("stream_options.include_usage 必须是布尔值")
	}
}

func validateFalseOnly(document map[string]json.RawMessage, name string) error {
	raw, present := document[name]
	if !present {
		return nil
	}
	if string(bytes.TrimSpace(raw)) != "false" {
		return fmt.Errorf("%s 只允许 false", name)
	}
	return nil
}

func validateEmptyArrayOnly(document map[string]json.RawMessage, name string) error {
	raw, present := document[name]
	if !present {
		return nil
	}
	var values []json.RawMessage
	if !rawJSONArray(raw) || json.Unmarshal(raw, &values) != nil || len(values) != 0 {
		return fmt.Errorf("%s 只允许空数组", name)
	}
	return nil
}

func validateNoneOnly(document map[string]json.RawMessage, name string) error {
	raw, present := document[name]
	if !present {
		return nil
	}
	var value string
	if json.Unmarshal(raw, &value) != nil || value != "none" {
		return fmt.Errorf("%s 在空工具闭集内只允许 none", name)
	}
	return nil
}

func validateChatTextFormat(raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	fields, order, err := decodeIngressObject(raw)
	if err != nil || len(fields) != 1 || len(fields["type"]) == 0 ||
		firstUnknownIngressField(order, map[string]struct{}{"type": {}}) != "" {
		return errors.New("response_format 只允许默认 text")
	}
	var kind string
	if json.Unmarshal(fields["type"], &kind) != nil || kind != "text" {
		return errors.New("response_format 只允许默认 text")
	}
	return nil
}

func validateResponsesTextFormat(raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	fields, order, err := decodeIngressObject(raw)
	if err != nil {
		return errors.New("Responses text 必须是对象")
	}
	if len(fields) == 0 {
		return nil
	}
	if len(fields) != 1 || len(fields["format"]) == 0 ||
		firstUnknownIngressField(order, map[string]struct{}{"format": {}}) != "" {
		return errors.New("Responses text 只允许默认文本格式")
	}
	format, formatOrder, err := decodeIngressObject(fields["format"])
	if err != nil || len(format) != 1 || len(format["type"]) == 0 ||
		firstUnknownIngressField(formatOrder, map[string]struct{}{"type": {}}) != "" {
		return errors.New("Responses text.format 只允许 type=text")
	}
	var kind string
	if json.Unmarshal(format["type"], &kind) != nil || kind != "text" {
		return errors.New("Responses text.format 只允许 type=text")
	}
	return nil
}

func validateCanonicalTranslation(
	canonical CanonicalRequest,
	report TranslationReport,
) error {
	if !report.Lossless || report.Compatibility != "lossless" ||
		len(report.RejectionReasons) != 0 {
		return errors.New("TranslationReport 不是 lossless")
	}
	if canonical.IngressProtocol == "" ||
		report.IngressProtocol != canonical.IngressProtocol {
		return errors.New("CanonicalRequest 与 TranslationReport 协议身份冲突")
	}
	if canonical.IngressProtocol != IngressProtocolOpenAIChatCompletions &&
		canonical.IngressProtocol != IngressProtocolOpenAIResponses {
		return errors.New("CanonicalRequest 协议未登记")
	}
	if len(report.Fields) == 0 {
		return errors.New("TranslationReport 缺少逐字段处置")
	}
	for _, field := range report.Fields {
		if strings.TrimSpace(field.SourcePath) == "" ||
			field.Disposition == TranslationDispositionRejected {
			return errors.New("TranslationReport 含空字段或拒绝处置")
		}
		switch field.Disposition {
		case TranslationDispositionPreserved,
			TranslationDispositionNormalized,
			TranslationDispositionLocal:
		default:
			return errors.New("TranslationReport 含未知字段处置")
		}
	}
	return nil
}
