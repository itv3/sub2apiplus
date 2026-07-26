package service

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const (
	officialOpenAIHTTPUserAgent     = "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)"
	officialOpenAIHTTPOriginator    = "codex_exec"
	officialOpenAIHTTPBetaFeatures  = "remote_compaction_v2"
	officialOpenAIHTTPResponsesLite = "true"
)

type officialOpenAIHTTPCallID struct {
	ItemType string
	Value    string
}

// officialOpenAIHTTPBodyContract 保存入口明确提供且 T5 禁止改写的字段。
// 值只在请求内存中流转，不进入普通日志或审计。
type officialOpenAIHTTPBodyContract struct {
	instructionsPresent bool
	instructions        any
	clientMetadataSet   bool
	clientMetadata      map[string]any
	promptCacheKeySet   bool
	promptCacheKey      string
	additionalTools     []any
	callIDs             []officialOpenAIHTTPCallID
	includePresent      bool
	include             any
	parallelPresent     bool
	parallelToolCalls   any
}

type officialOpenAIHTTPIdentity struct {
	installationID string
	sessionID      string
	threadID       string
	windowID       string
	turnID         string
	turnMetadata   string
	clientRequest  string
	promptCacheKey string
	source         OfficialEgressFieldSource
}

// captureOfficialOpenAIHTTPBodyContract 在既有 OAuth 转换前记录入口契约。
// 标准第三方请求可以没有 Codex 身份字段；真正的官方 Codex 请求仍由 Finalizer
// 按入口 Header 严格校验完整身份。
func captureOfficialOpenAIHTTPBodyContract(body []byte) (*officialOpenAIHTTPBodyContract, error) {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("OpenAI official egress requires valid JSON body: %w", err)
	}
	contract := &officialOpenAIHTTPBodyContract{}
	contract.instructions, contract.instructionsPresent = payload["instructions"]
	contract.include, contract.includePresent = payload["include"]
	contract.parallelToolCalls, contract.parallelPresent = payload["parallel_tool_calls"]

	if promptCacheKey, ok := payload["prompt_cache_key"].(string); ok &&
		strings.TrimSpace(promptCacheKey) != "" {
		contract.promptCacheKeySet = true
		contract.promptCacheKey = strings.TrimSpace(promptCacheKey)
	}

	if clientMetadata, ok := payload["client_metadata"].(map[string]any); ok {
		contract.clientMetadataSet = true
		contract.clientMetadata = cloneOfficialOpenAIMap(clientMetadata)
	}
	contract.additionalTools = collectOfficialOpenAIAdditionalTools(payload)
	contract.callIDs = collectOfficialOpenAICallIDs(payload)
	return contract, nil
}

// bindGeneratedOfficialOpenAIHTTPBodyContract 绑定 Chat Completions/Messages
// 转换后的 Responses 语义字段。身份字段仍标记为非入口显式值，后续必须重新派生。
func bindGeneratedOfficialOpenAIHTTPBodyContract(
	contract *officialOpenAIHTTPBodyContract,
	body []byte,
) error {
	if contract == nil {
		return errors.New("OpenAI official egress body contract is nil")
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return fmt.Errorf("decode generated OpenAI official egress body: %w", err)
	}
	contract.additionalTools = collectOfficialOpenAIAdditionalTools(payload)
	contract.callIDs = collectOfficialOpenAICallIDs(payload)
	if contract.promptCacheKey == "" {
		if value, ok := payload["prompt_cache_key"].(string); ok {
			contract.promptCacheKey = strings.TrimSpace(value)
		}
	}
	return nil
}

// captureGeneratedOfficialOpenAIHTTPBodyContract 为未显式传递契约的兼容桥建立
// 最终语义快照。旧链路生成的 instructions 没有入口所有权，必须由 Finalizer 删除。
func captureGeneratedOfficialOpenAIHTTPBodyContract(
	body []byte,
) (*officialOpenAIHTTPBodyContract, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	if err != nil {
		return nil, err
	}
	contract.instructionsPresent = false
	contract.instructions = nil
	contract.clientMetadataSet = false
	contract.promptCacheKeySet = false
	return contract, nil
}

func cloneOfficialOpenAIMap(source map[string]any) map[string]any {
	cloned := make(map[string]any, len(source))
	for key, value := range source {
		cloned[key] = value
	}
	return cloned
}

func collectOfficialOpenAIAdditionalTools(payload map[string]any) []any {
	input, _ := payload["input"].([]any)
	items := make([]any, 0, 1)
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		if itemType, _ := item["type"].(string); itemType == "additional_tools" {
			items = append(items, item)
		}
	}
	return items
}

func collectOfficialOpenAICallIDs(payload map[string]any) []officialOpenAIHTTPCallID {
	input, _ := payload["input"].([]any)
	callIDs := make([]officialOpenAIHTTPCallID, 0)
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		itemType, _ := item["type"].(string)
		if !isCodexToolCallItemType(itemType) {
			continue
		}
		callID, _ := item["call_id"].(string)
		if strings.TrimSpace(callID) == "" {
			continue
		}
		callIDs = append(callIDs, officialOpenAIHTTPCallID{
			ItemType: itemType,
			Value:    callID,
		})
	}
	return callIDs
}

// finalizeOpenAIOfficialEgressHTTPRequest 在请求发送前执行 T5 最小差异修正。
// 没有 Profile 上下文时原样返回；存在时 Header 与 Body 必须能追溯到同一入口生命周期。
func finalizeOpenAIOfficialEgressHTTPRequest(
	req *http.Request,
	c *gin.Context,
	account *Account,
	body []byte,
	plan openAIUpstreamRequestPlan,
) (*http.Request, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	var err error
	if req == nil {
		return nil, result, errors.New("OpenAI official egress requires request")
	}
	egressContext, enabled := OfficialEgressContextFromContext(req.Context())
	if !enabled {
		return req, result, nil
	}
	if c == nil || c.Request == nil {
		return nil, result, errors.New("OpenAI official egress requires ingress request")
	}
	if plan.OfficialEgressBodyContract == nil {
		plan.OfficialEgressBodyContract, err = captureGeneratedOfficialOpenAIHTTPBodyContract(body)
		if err != nil {
			return nil, result, err
		}
	}

	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		c.Request.URL.Path,
	)
	if err != nil {
		return nil, result, err
	}
	if profile.TargetPlatform != PlatformOpenAI {
		return nil, result, errors.New("OpenAI finalizer received non-OpenAI profile")
	}
	clientProfile, err := resolveOfficialClientProfileByID(profile.ID)
	if err != nil {
		return nil, result, err
	}

	strictIngressIdentity := isInboundOpenAIOfficialClient(c)
	identity, err := resolveOfficialOpenAIHTTPIdentity(
		c,
		account,
		body,
		plan.OfficialEgressBodyContract,
		strictIngressIdentity,
	)
	if err != nil {
		return nil, result, err
	}
	finalBody, bodyModified, err := finalizeOfficialOpenAIHTTPBody(
		body,
		plan.OfficialEgressBodyContract,
		identity,
		strictIngressIdentity,
		plan.IsCompact,
	)
	if err != nil {
		return nil, result, err
	}
	if bodyModified {
		result.Modifications = append(result.Modifications,
			OfficialEgressModification{Kind: "body", Field: "instructions"},
		)
	}

	if err := registerOfficialOpenAIHTTPIdentity(egressContext, identity); err != nil {
		return nil, result, err
	}
	finalizeOfficialOpenAIHTTPHeaders(req.Header, clientProfile, identity, plan.IsCompact)
	result.Modifications = append(result.Modifications,
		OfficialEgressModification{Kind: "header", Field: "session-id"},
		OfficialEgressModification{Kind: "header", Field: "thread-id"},
		OfficialEgressModification{Kind: "header", Field: "x-client-request-id"},
		OfficialEgressModification{Kind: "header", Field: "x-codex-window-id"},
		OfficialEgressModification{Kind: "header", Field: "x-codex-turn-metadata"},
	)

	if err := ValidateOfficialEgressFinalState(egressContext, profile); err != nil {
		return nil, result, err
	}
	logOfficialEgressProfileResolved(egressContext, profile)
	resetOfficialEgressRequestBody(req, finalBody)
	return req, result, nil
}

func finalizeOfficialOpenAIHTTPBody(
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
	identity officialOpenAIHTTPIdentity,
	strictIngressIdentity bool,
	isCompact bool,
) ([]byte, bool, error) {
	if contract == nil {
		return nil, false, errors.New("OpenAI official egress body contract is nil")
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, false, fmt.Errorf("decode OpenAI official egress body: %w", err)
	}

	modified := false
	currentInstructions, currentInstructionsPresent := payload["instructions"]
	if strictIngressIdentity {
		if !contract.instructionsPresent {
			if currentInstructionsPresent {
				delete(payload, "instructions")
				modified = true
			}
		} else if !currentInstructionsPresent || !reflect.DeepEqual(contract.instructions, currentInstructions) {
			return nil, false, errors.New("OpenAI official egress explicit instructions were modified")
		}
	} else if currentInstructionsPresent {
		instructionsModified, err := moveOfficialOpenAIHTTPInstructionsToInput(payload, currentInstructions)
		if err != nil {
			return nil, false, err
		}
		if instructionsModified {
			modified = true
		}
	}

	// Codex 0.145.0 的 Responses Lite Header 与 reasoning.context 是绑定契约：
	// Header=true 时上游强制要求 context=all_turns。第三方入口通常不会提供该
	// 字段，因此必须在同一个最终修正器中补齐，避免只伪装 Header 却发送非法 Body。
	if !isCompact {
		reasoningModified, err := ensureOpenAIResponsesLiteReasoningContext(payload)
		if err != nil {
			return nil, false, err
		}
		if reasoningModified {
			modified = true
		}
	}
	if !isCompact && !strictIngressIdentity {
		profileModified, err := normalizeDerivedOfficialOpenAIHTTPBody(payload)
		if err != nil {
			return nil, false, err
		}
		if profileModified {
			modified = true
		}
	}

	if !isCompact && strictIngressIdentity {
		currentMetadata, ok := payload["client_metadata"].(map[string]any)
		if !ok || !reflect.DeepEqual(contract.clientMetadata, currentMetadata) {
			return nil, false, errors.New("OpenAI official egress client_metadata was modified")
		}
		currentPromptCacheKey, _ := payload["prompt_cache_key"].(string)
		if strings.TrimSpace(currentPromptCacheKey) != contract.promptCacheKey {
			return nil, false, errors.New("OpenAI official egress prompt_cache_key was modified")
		}
	} else if !isCompact {
		expectedMetadata := buildOfficialOpenAIHTTPClientMetadata(identity)
		currentMetadata, _ := payload["client_metadata"].(map[string]any)
		if !reflect.DeepEqual(expectedMetadata, currentMetadata) {
			payload["client_metadata"] = expectedMetadata
			modified = true
		}
		currentPromptCacheKey, _ := payload["prompt_cache_key"].(string)
		if strings.TrimSpace(currentPromptCacheKey) != identity.promptCacheKey {
			payload["prompt_cache_key"] = identity.promptCacheKey
			modified = true
		}
	}
	if strictIngressIdentity &&
		!reflect.DeepEqual(contract.additionalTools, collectOfficialOpenAIAdditionalTools(payload)) {
		return nil, false, errors.New("OpenAI official egress additional_tools were modified")
	}
	if !reflect.DeepEqual(contract.callIDs, collectOfficialOpenAICallIDs(payload)) {
		return nil, false, errors.New("OpenAI official egress call_id was modified")
	}
	// /responses/compact 使用独立 schema，入口 include 会在既有 compact 规范化中删除。
	// 普通 Responses 则必须保持入口显式 include。
	if contract.includePresent && !isCompact && strictIngressIdentity {
		if current, exists := payload["include"]; !exists || !reflect.DeepEqual(contract.include, current) {
			return nil, false, errors.New("OpenAI official egress include was modified")
		}
	}
	if contract.parallelPresent && strictIngressIdentity {
		if current, exists := payload["parallel_tool_calls"]; !exists || !reflect.DeepEqual(contract.parallelToolCalls, current) {
			return nil, false, errors.New("OpenAI official egress parallel_tool_calls was modified")
		}
	}

	if !modified {
		return body, false, nil
	}
	finalBody, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return nil, false, fmt.Errorf("serialize OpenAI official egress body: %w", err)
	}
	return finalBody, true, nil
}

// normalizeDerivedOfficialOpenAIHTTPBody 把第三方 Responses 请求归一化为
// Codex 0.145.0 Responses Lite 的固定外层契约。模型、用户输入、工具定义和
// reasoning.effort 等业务语义保持不变，只修正官方客户端固定发送的画像字段。
func normalizeDerivedOfficialOpenAIHTTPBody(payload map[string]any) (bool, error) {
	modified, err := normalizeDerivedOfficialOpenAIInput(payload)
	if err != nil {
		return false, err
	}
	liteModified, err := normalizeOpenAIResponsesLiteTools(payload)
	if err != nil {
		return false, err
	}
	if liteModified {
		modified = true
	}
	toolsModified, err := moveOfficialOpenAIHTTPToolsToInput(payload)
	if err != nil {
		return false, err
	}
	if toolsModified {
		modified = true
	}

	// Kilo/OpenAI OAuth 与 Codex CLI 均不会向 Codex 官方上游发送
	// max_output_tokens。第三方 Responses 客户端可能携带该标准 API 字段，
	// 但 ChatGPT Codex HTTP/WS 上游不接受，必须在最终画像层统一删除。
	if _, exists := payload["max_output_tokens"]; exists {
		delete(payload, "max_output_tokens")
		modified = true
	}

	for key, expected := range map[string]bool{
		"parallel_tool_calls": false,
		"store":               false,
		"stream":              true,
	} {
		if current, ok := payload[key].(bool); !ok || current != expected {
			payload[key] = expected
			modified = true
		}
	}
	if current, ok := payload["tool_choice"].(string); !ok || current != "auto" {
		payload["tool_choice"] = "auto"
		modified = true
	}

	expectedInclude := []any{"reasoning.encrypted_content"}
	if !reflect.DeepEqual(payload["include"], expectedInclude) {
		payload["include"] = expectedInclude
		modified = true
	}

	rawText, exists := payload["text"]
	text, ok := rawText.(map[string]any)
	if !exists || rawText == nil {
		text = map[string]any{}
		payload["text"] = text
		modified = true
	} else if !ok {
		return false, errors.New("OpenAI official egress text must be an object")
	}
	if verbosity, _ := text["verbosity"].(string); verbosity != "low" {
		text["verbosity"] = "low"
		modified = true
	}
	return modified, nil
}

// normalizeDerivedOfficialOpenAIInput 把第三方 Responses 的简写 input
// 归一化为 Codex 使用的显式 message 结构。文本和消息顺序保持不变。
func normalizeDerivedOfficialOpenAIInput(payload map[string]any) (bool, error) {
	rawInput, exists := payload["input"]
	if !exists || rawInput == nil {
		payload["input"] = []any{}
		return true, nil
	}
	if text, ok := rawInput.(string); ok {
		payload["input"] = []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": []any{map[string]any{"type": "input_text", "text": text}},
		}}
		return true, nil
	}
	input, ok := rawInput.([]any)
	if !ok {
		return false, errors.New("OpenAI official egress input must be a string or array")
	}
	modified := false
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		if strings.TrimSpace(officialOpenAIString(item, "type")) == "" &&
			strings.TrimSpace(officialOpenAIString(item, "role")) != "" {
			item["type"] = "message"
			modified = true
		}
	}
	return modified, nil
}

// moveOfficialOpenAIHTTPToolsToInput 将第三方标准 Responses 顶层工具无损搬到
// Codex 0.145.0 使用的 input.additional_tools 载体。工具对象本身不改名、不改
// schema；已有 additional_tools 会按类型和名称去重合并。
func moveOfficialOpenAIHTTPToolsToInput(payload map[string]any) (bool, error) {
	rawTools, exists := payload["tools"]
	if !exists || rawTools == nil {
		return false, nil
	}
	tools, ok := rawTools.([]any)
	if !ok {
		return false, errors.New("OpenAI official egress tools must be an array")
	}
	if len(tools) == 0 {
		delete(payload, "tools")
		return true, nil
	}

	input, err := appendOpenAIResponsesLiteAdditionalTools(payload["input"], tools)
	if err != nil {
		return false, err
	}
	// 官方 Codex 把 additional_tools 放在输入首位，后续 developer/user 消息
	// 保持原顺序。append helper 为兼容旧链路默认追加，这里只调整载体位置。
	additional := make([]any, 0, 1)
	remaining := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if ok && officialOpenAIString(item, "type") == "additional_tools" {
			additional = append(additional, rawItem)
			continue
		}
		remaining = append(remaining, rawItem)
	}
	payload["input"] = append(additional, remaining...)
	delete(payload, "tools")
	return true, nil
}

// moveOfficialOpenAIHTTPInstructionsToInput 将第三方入口的顶层 instructions
// 无损搬到 developer message。官方 Codex 0.145.0 不发送顶层 instructions，
// 但不能因此丢弃 Kilo 等客户端提供的系统指令。
func moveOfficialOpenAIHTTPInstructionsToInput(payload map[string]any, rawInstructions any) (bool, error) {
	instructions, ok := rawInstructions.(string)
	if !ok {
		return false, errors.New("OpenAI official egress third-party instructions must be a string")
	}
	delete(payload, "instructions")
	if strings.TrimSpace(instructions) == "" {
		return true, nil
	}

	var input []any
	switch value := payload["input"].(type) {
	case nil:
		input = []any{}
	case string:
		input = []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": []any{map[string]any{"type": "input_text", "text": value}},
		}}
	case []any:
		input = value
	default:
		return false, errors.New("OpenAI official egress input must be a string or array")
	}

	developerMessage := map[string]any{
		"type": "message",
		"role": "developer",
		"content": []any{map[string]any{
			"type": "input_text",
			"text": instructions,
		}},
	}
	insertAt := 0
	for insertAt < len(input) {
		item, ok := input[insertAt].(map[string]any)
		if !ok || officialOpenAIString(item, "type") != "additional_tools" {
			break
		}
		insertAt++
	}
	if insertAt < len(input) {
		if item, ok := input[insertAt].(map[string]any); ok &&
			officialOpenAIString(item, "role") == "developer" &&
			officialOpenAIHTTPMessageContentText(item["content"]) == strings.TrimSpace(instructions) {
			// 兼容层可能已把同一份 system 语义放入 developer input。
			// Finalizer 只移除顶层 instructions，避免重复提示词和额外 token。
			return true, nil
		}
	}
	input = append(input, nil)
	copy(input[insertAt+1:], input[insertAt:])
	input[insertAt] = developerMessage
	payload["input"] = input
	return true, nil
}

func resolveOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
	strictIngressIdentity bool,
) (officialOpenAIHTTPIdentity, error) {
	if contract == nil {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress body contract is nil")
	}
	if strictIngressIdentity {
		return resolveExplicitOfficialOpenAIHTTPIdentity(c, contract)
	}
	return deriveOfficialOpenAIHTTPIdentity(c, account, body, contract), nil
}

func resolveExplicitOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	contract *officialOpenAIHTTPBodyContract,
) (officialOpenAIHTTPIdentity, error) {
	if !contract.clientMetadataSet || !contract.promptCacheKeySet {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress requires complete identity from official ingress")
	}
	metadata := contract.clientMetadata
	identity := officialOpenAIHTTPIdentity{
		installationID: officialOpenAIString(metadata, "x-codex-installation-id"),
		sessionID:      officialOpenAIString(metadata, "session_id"),
		threadID:       officialOpenAIString(metadata, "thread_id"),
		windowID:       officialOpenAIString(metadata, "x-codex-window-id"),
		turnID:         officialOpenAIString(metadata, "turn_id"),
		turnMetadata:   officialOpenAIString(metadata, "x-codex-turn-metadata"),
		clientRequest:  strings.TrimSpace(c.GetHeader("x-client-request-id")),
		promptCacheKey: contract.promptCacheKey,
		source:         OfficialEgressFieldSourceIngressExplicit,
	}
	for name, value := range map[string]string{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"window_id":        identity.windowID,
		"turn_id":          identity.turnID,
		"turn_metadata":    identity.turnMetadata,
		"client_request":   identity.clientRequest,
		"prompt_cache_key": identity.promptCacheKey,
	} {
		if value == "" {
			return officialOpenAIHTTPIdentity{}, fmt.Errorf("OpenAI official egress requires %s from ingress", name)
		}
	}
	for name, value := range map[string]string{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"turn_id":          identity.turnID,
		"client_request":   identity.clientRequest,
		"prompt_cache_key": identity.promptCacheKey,
	} {
		if _, err := uuid.Parse(value); err != nil {
			return officialOpenAIHTTPIdentity{}, fmt.Errorf("OpenAI official egress %s must be UUID", name)
		}
	}
	if identity.sessionID != identity.threadID ||
		identity.sessionID != identity.clientRequest ||
		identity.sessionID != identity.promptCacheKey {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress session/thread/request/body identity conflicts")
	}
	windowParts := strings.Split(identity.windowID, ":")
	if len(windowParts) != 2 || windowParts[0] != identity.sessionID {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress window_id conflicts with session")
	}
	windowIndex, err := strconv.Atoi(windowParts[1])
	if err != nil || windowIndex < 0 {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress window_id has invalid index")
	}

	if strings.TrimSpace(c.GetHeader("session-id")) != identity.sessionID ||
		strings.TrimSpace(c.GetHeader("thread-id")) != identity.threadID ||
		strings.TrimSpace(c.GetHeader("x-codex-window-id")) != identity.windowID ||
		strings.TrimSpace(c.GetHeader("x-codex-turn-metadata")) != identity.turnMetadata {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress ingress headers conflict with body identity")
	}

	var turnMetadata map[string]any
	if err := json.Unmarshal([]byte(identity.turnMetadata), &turnMetadata); err != nil {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress turn metadata must be valid JSON")
	}
	if officialOpenAIString(turnMetadata, "installation_id") != identity.installationID ||
		officialOpenAIString(turnMetadata, "session_id") != identity.sessionID ||
		officialOpenAIString(turnMetadata, "thread_id") != identity.threadID ||
		officialOpenAIString(turnMetadata, "turn_id") != identity.turnID ||
		officialOpenAIString(turnMetadata, "window_id") != identity.windowID {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress turn metadata conflicts with identity")
	}
	return identity, nil
}

// deriveOfficialOpenAIHTTPIdentity 为普通第三方客户端生成 Codex 0.145.0
// 身份。安装 ID 按客户端稳定，会话 ID 按对话锚点稳定，Turn ID 按最后一条
// 用户消息稳定，因此工具结果续轮会复用同一个 Turn。
func deriveOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
) officialOpenAIHTTPIdentity {
	accountID := int64(0)
	if account != nil {
		accountID = account.ID
	}
	userAgent := ""
	if c != nil {
		userAgent = c.GetHeader("User-Agent")
	}
	clientScope := fmt.Sprintf(
		"account=%d|api_key=%d|ua=%s",
		accountID,
		getAPIKeyIDFromContext(c),
		NormalizeSessionUserAgent(userAgent),
	)
	installationID := generateSessionUUID(
		"openai-official-egress-installation|" + clientScope,
	)

	firstUserAnchor, lastUserAnchor := officialOpenAIHTTPUserAnchors(body)
	sessionAnchor := officialOpenAIHTTPSessionAnchor(c)
	if sessionAnchor == "" && contract != nil {
		sessionAnchor = strings.TrimSpace(contract.promptCacheKey)
	}
	if sessionAnchor == "" {
		sessionAnchor = "first_user=" + firstUserAnchor
	}
	sessionID := generateSessionUUID(
		"openai-official-egress-session|" + clientScope + "|" + sessionAnchor,
	)
	if lastUserAnchor == "" {
		lastUserAnchor = firstUserAnchor
	}
	turnID := generateSessionUUID(
		"openai-official-egress-turn|" + sessionID + "|" + lastUserAnchor,
	)
	windowID := sessionID + ":0"

	turnHash := sha256.Sum256([]byte(turnID))
	dayStart := time.Now().UTC().Truncate(24 * time.Hour).UnixMilli()
	turnStartedAtUnixMS := dayStart +
		int64(binary.BigEndian.Uint32(turnHash[:4])%uint32((24*time.Hour)/time.Millisecond))
	turnMetadataValues := map[string]any{
		"installation_id":         installationID,
		"session_id":              sessionID,
		"thread_id":               sessionID,
		"turn_id":                 turnID,
		"window_id":               windowID,
		"request_kind":            "turn",
		"thread_source":           "user",
		"sandbox":                 "seccomp",
		"turn_started_at_unix_ms": turnStartedAtUnixMS,
	}
	turnMetadataBytes, _ := json.Marshal(turnMetadataValues)
	return officialOpenAIHTTPIdentity{
		installationID: installationID,
		sessionID:      sessionID,
		threadID:       sessionID,
		windowID:       windowID,
		turnID:         turnID,
		turnMetadata:   string(turnMetadataBytes),
		clientRequest:  sessionID,
		promptCacheKey: sessionID,
		source:         OfficialEgressFieldSourceDerived,
	}
}

func officialOpenAIHTTPSessionAnchor(c *gin.Context) string {
	if c == nil {
		return ""
	}
	for _, name := range []string{
		"X-Session-Affinity",
		"session_id",
		"conversation_id",
		"X-Session-Id",
		"X-OpenCode-Session",
	} {
		if value := strings.TrimSpace(c.GetHeader(name)); value != "" {
			return strings.ToLower(name) + "=" + value
		}
	}
	return ""
}

func officialOpenAIHTTPUserAnchors(body []byte) (string, string) {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return "", ""
	}
	input, ok := payload["input"].([]any)
	if !ok {
		if text, ok := payload["input"].(string); ok {
			text = strings.TrimSpace(text)
			return text, "0:" + text
		}
		return "", ""
	}
	first := ""
	last := ""
	for index, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || officialOpenAIString(item, "type") != "message" ||
			officialOpenAIString(item, "role") != "user" {
			continue
		}
		text := officialOpenAIHTTPMessageContentText(item["content"])
		if text == "" {
			continue
		}
		if first == "" {
			first = text
		}
		last = strconv.Itoa(index) + ":" + text
	}
	return first, last
}

func officialOpenAIHTTPMessageContentText(content any) string {
	switch value := content.(type) {
	case string:
		return strings.TrimSpace(value)
	case []any:
		parts := make([]string, 0, len(value))
		for _, rawPart := range value {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			text := officialOpenAIString(part, "text")
			if text == "" {
				text = officialOpenAIString(part, "input_text")
			}
			if text != "" {
				parts = append(parts, text)
			}
		}
		return strings.TrimSpace(strings.Join(parts, "\n"))
	default:
		return ""
	}
}

func buildOfficialOpenAIHTTPClientMetadata(
	identity officialOpenAIHTTPIdentity,
) map[string]any {
	return map[string]any{
		"x-codex-installation-id": identity.installationID,
		"session_id":              identity.sessionID,
		"thread_id":               identity.threadID,
		"turn_id":                 identity.turnID,
		"x-codex-window-id":       identity.windowID,
		"x-codex-turn-metadata":   identity.turnMetadata,
	}
}

func officialOpenAIString(values map[string]any, key string) string {
	value, _ := values[key].(string)
	return strings.TrimSpace(value)
}

func registerOfficialOpenAIHTTPIdentity(
	egressContext *OfficialEgressContext,
	identity officialOpenAIHTTPIdentity,
) error {
	fields := []struct {
		name      OfficialEgressFieldName
		value     string
		lifecycle OfficialEgressFieldLifecycle
	}{
		{name: OfficialEgressFieldDeviceID, value: identity.installationID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldSessionID, value: identity.sessionID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldThreadID, value: identity.threadID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldWindowID, value: identity.windowID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldTurnMetadata, value: identity.turnMetadata, lifecycle: OfficialEgressFieldLifecycleTurn},
		{name: OfficialEgressFieldClientRequestID, value: identity.clientRequest, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldPromptCacheKey, value: identity.promptCacheKey, lifecycle: OfficialEgressFieldLifecycleSession},
	}
	for _, field := range fields {
		if err := egressContext.RegisterField(
			field.name,
			field.value,
			identity.source,
			field.lifecycle,
		); err != nil {
			return err
		}
	}
	return nil
}

func finalizeOfficialOpenAIHTTPHeaders(
	header http.Header,
	profile officialClientResolvedProfile,
	identity officialOpenAIHTTPIdentity,
	isCompact bool,
) {
	for _, name := range []string{
		"conversation_id",
		"session_id",
		"OpenAI-Beta",
		"X-Codex-Installation-ID",
		"version",
	} {
		header.Del(name)
	}

	accept := "text/event-stream"
	if isCompact {
		accept = "application/json"
	}
	header.Set("Accept", accept)
	header.Set("Content-Type", "application/json")
	header.Set("User-Agent", profile.Build.UserAgent)
	header.Set("originator", profile.Build.Originator)
	header.Set("session-id", identity.sessionID)
	header.Set("thread-id", identity.threadID)
	header.Set("x-client-request-id", identity.clientRequest)
	header.Set("x-codex-turn-metadata", identity.turnMetadata)
	header.Set("x-codex-window-id", identity.windowID)
	for _, item := range profile.Wire.StaticHeaders {
		header.Set(item.Name, item.Value)
	}
}
