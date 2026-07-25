package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const (
	officialOpenAIWSClientVersion      = "0.145.0"
	officialOpenAIWSCompressionOffer   = "permessage-deflate; client_max_window_bits"
	officialOpenAIWSResponseCreateType = "response.create"
	officialOpenAIAdditionalToolsType  = "additional_tools"
)

type officialOpenAIWSIdentity struct {
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

// officialOpenAIWSDerivedState 仅保存普通第三方 WS 会话的逐轮身份。
// 连接级身份在拨号前冻结；逐轮 Turn ID 则必须在工具续轮中延续，因此单独按
// 当前下游 WebSocket 会话串行维护。
type officialOpenAIWSDerivedState struct {
	mu                  sync.Mutex
	lastTurnID          string
	lastTurnStartedAtMS int64
}

// prepareOpenAIOfficialEgressWSContext 从入口首帧和握手头登记真实身份。
// 这些字段在拨号前冻结，后续帧只能验证，不能改写连接级身份。
func prepareOpenAIOfficialEgressWSContext(
	egressContext *OfficialEgressContext,
	c *gin.Context,
	account *Account,
	firstPayload []byte,
) error {
	if egressContext == nil {
		return errors.New("OpenAI official egress WebSocket context is nil")
	}
	if c == nil || c.Request == nil {
		return errors.New("OpenAI official egress WebSocket ingress request is unavailable")
	}
	identity, err := resolveOfficialOpenAIWSIdentity(c, account, firstPayload)
	if err != nil {
		return err
	}
	if identity.source == OfficialEgressFieldSourceDerived {
		egressContext.openAIWSDerived = &officialOpenAIWSDerivedState{}
	}
	return registerOfficialOpenAIWSIdentity(egressContext, identity)
}

func resolveOfficialOpenAIWSIdentity(
	c *gin.Context,
	account *Account,
	firstPayload []byte,
) (officialOpenAIWSIdentity, error) {
	if !isInboundOpenAIOfficialClient(c) {
		return deriveOfficialOpenAIWSIdentity(c, account, firstPayload)
	}
	return resolveExplicitOfficialOpenAIWSIdentity(c, firstPayload)
}

func resolveExplicitOfficialOpenAIWSIdentity(
	c *gin.Context,
	firstPayload []byte,
) (officialOpenAIWSIdentity, error) {
	var payload map[string]any
	if err := json.Unmarshal(firstPayload, &payload); err != nil {
		return officialOpenAIWSIdentity{}, fmt.Errorf(
			"OpenAI official egress requires valid WebSocket first frame: %w",
			err,
		)
	}
	if strings.TrimSpace(officialOpenAIString(payload, "type")) != officialOpenAIWSResponseCreateType {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket first frame must be response.create",
		)
	}
	metadata, ok := payload["client_metadata"].(map[string]any)
	if !ok {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket requires client_metadata from ingress frame",
		)
	}
	identity := officialOpenAIWSIdentity{
		installationID: officialOpenAIString(metadata, "x-codex-installation-id"),
		sessionID:      officialOpenAIString(metadata, "session_id"),
		threadID:       officialOpenAIString(metadata, "thread_id"),
		windowID:       officialOpenAIString(metadata, "x-codex-window-id"),
		turnID:         officialOpenAIString(metadata, "turn_id"),
		turnMetadata:   officialOpenAIString(metadata, openAIWSTurnMetadataHeader),
		clientRequest:  strings.TrimSpace(c.GetHeader("x-client-request-id")),
		promptCacheKey: officialOpenAIString(payload, "prompt_cache_key"),
		source:         OfficialEgressFieldSourceIngressExplicit,
	}
	for name, value := range map[string]string{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"window_id":        identity.windowID,
		"turn_metadata":    identity.turnMetadata,
		"client_request":   identity.clientRequest,
		"prompt_cache_key": identity.promptCacheKey,
	} {
		if value == "" {
			return officialOpenAIWSIdentity{}, fmt.Errorf(
				"OpenAI official egress WebSocket requires %s from ingress",
				name,
			)
		}
	}
	for name, value := range map[string]string{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"client_request":   identity.clientRequest,
		"prompt_cache_key": identity.promptCacheKey,
	} {
		if _, err := uuid.Parse(value); err != nil {
			return officialOpenAIWSIdentity{}, fmt.Errorf(
				"OpenAI official egress WebSocket %s must be UUID",
				name,
			)
		}
	}
	// 官方预热帧的 turn_id 为空；普通帧一旦提供 turn_id，仍必须是 UUID。
	if identity.turnID != "" {
		if _, err := uuid.Parse(identity.turnID); err != nil {
			return officialOpenAIWSIdentity{}, errors.New(
				"OpenAI official egress WebSocket turn_id must be UUID when present",
			)
		}
	}
	if identity.sessionID != identity.threadID ||
		identity.sessionID != identity.clientRequest ||
		identity.sessionID != identity.promptCacheKey {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket session/thread/request/body identity conflicts",
		)
	}
	windowParts := strings.Split(identity.windowID, ":")
	if len(windowParts) != 2 || windowParts[0] != identity.sessionID {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket window_id conflicts with session",
		)
	}
	windowIndex, err := strconv.Atoi(windowParts[1])
	if err != nil || windowIndex < 0 {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket window_id has invalid index",
		)
	}

	if strings.TrimSpace(c.GetHeader("session-id")) != identity.sessionID ||
		strings.TrimSpace(c.GetHeader("thread-id")) != identity.threadID ||
		strings.TrimSpace(c.GetHeader("x-codex-window-id")) != identity.windowID ||
		strings.TrimSpace(c.GetHeader(openAIWSTurnMetadataHeader)) != identity.turnMetadata {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket ingress headers conflict with frame identity",
		)
	}

	var turnMetadata map[string]any
	if err := json.Unmarshal([]byte(identity.turnMetadata), &turnMetadata); err != nil {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket turn metadata must be valid JSON",
		)
	}
	if officialOpenAIString(turnMetadata, "installation_id") != identity.installationID ||
		officialOpenAIString(turnMetadata, "session_id") != identity.sessionID ||
		officialOpenAIString(turnMetadata, "thread_id") != identity.threadID ||
		officialOpenAIString(turnMetadata, "turn_id") != identity.turnID ||
		officialOpenAIString(turnMetadata, "window_id") != identity.windowID {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket turn metadata conflicts with identity",
		)
	}
	return identity, nil
}

// deriveOfficialOpenAIWSIdentity 为 Kilo 等第三方客户端派生连接级身份。
// 握手使用官方 prewarm metadata；真正的 turn metadata 在每个
// response.create 出站前重新生成。
func deriveOfficialOpenAIWSIdentity(
	c *gin.Context,
	account *Account,
	firstPayload []byte,
) (officialOpenAIWSIdentity, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContract(firstPayload)
	if err != nil {
		return officialOpenAIWSIdentity{}, err
	}
	base := deriveOfficialOpenAIHTTPIdentity(c, account, firstPayload, contract)
	turnMetadataBytes, err := json.Marshal(map[string]any{
		"installation_id": base.installationID,
		"session_id":      base.sessionID,
		"thread_id":       base.threadID,
		"turn_id":         "",
		"window_id":       base.windowID,
		"request_kind":    "prewarm",
		"thread_source":   "user",
		"sandbox":         "seccomp",
	})
	if err != nil {
		return officialOpenAIWSIdentity{}, fmt.Errorf(
			"encode OpenAI official egress WebSocket prewarm metadata: %w",
			err,
		)
	}
	return officialOpenAIWSIdentity{
		installationID: base.installationID,
		sessionID:      base.sessionID,
		threadID:       base.threadID,
		windowID:       base.windowID,
		turnID:         "",
		turnMetadata:   string(turnMetadataBytes),
		clientRequest:  base.clientRequest,
		promptCacheKey: base.promptCacheKey,
		source:         OfficialEgressFieldSourceDerived,
	}, nil
}

func registerOfficialOpenAIWSIdentity(
	egressContext *OfficialEgressContext,
	identity officialOpenAIWSIdentity,
) error {
	fields := []struct {
		name      OfficialEgressFieldName
		value     string
		lifecycle OfficialEgressFieldLifecycle
	}{
		{name: OfficialEgressFieldDeviceID, value: identity.installationID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldSessionID, value: identity.sessionID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldThreadID, value: identity.threadID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldWindowID, value: identity.windowID, lifecycle: OfficialEgressFieldLifecycleConnection},
		{name: OfficialEgressFieldTurnMetadata, value: identity.turnMetadata, lifecycle: OfficialEgressFieldLifecycleConnection},
		{name: OfficialEgressFieldClientRequestID, value: identity.clientRequest, lifecycle: OfficialEgressFieldLifecycleConnection},
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

// finalizeOpenAIOfficialEgressWSHandshakeHeaders 在账号覆写之后执行终态修正。
// 它删除旧下划线身份，只写入已经冻结且来源可追踪的官方连接级身份。
func finalizeOpenAIOfficialEgressWSHandshakeHeaders(
	ctx context.Context,
	headers http.Header,
) (OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled {
		return result, nil
	}
	if headers == nil {
		return result, errors.New("OpenAI official egress WebSocket headers are nil")
	}
	if !egressContext.IsFrozen() ||
		egressContext.TargetPlatform() != PlatformOpenAI ||
		egressContext.Transport() != OfficialEgressTransportWebSocket ||
		egressContext.UpstreamHost() != "chatgpt.com" {
		return result, errors.New(
			"OpenAI official egress WebSocket context conflicts with handshake",
		)
	}

	sessionID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldSessionID,
	)
	if err != nil {
		return result, err
	}
	threadID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldThreadID,
	)
	if err != nil {
		return result, err
	}
	clientRequestID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldClientRequestID,
	)
	if err != nil {
		return result, err
	}
	windowID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldWindowID,
	)
	if err != nil {
		return result, err
	}
	turnMetadata, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldTurnMetadata,
	)
	if err != nil {
		return result, err
	}

	for _, name := range []string{
		"conversation_id",
		"session_id",
		"X-Codex-Installation-ID",
	} {
		headers.Del(name)
	}
	headers.Set("session-id", sessionID)
	headers.Set("thread-id", threadID)
	headers.Set("x-client-request-id", clientRequestID)
	headers.Set("x-codex-window-id", windowID)
	headers.Set(openAIWSTurnMetadataHeader, turnMetadata)
	headers.Set("User-Agent", officialOpenAIHTTPUserAgent)
	headers.Set("originator", officialOpenAIHTTPOriginator)
	headers.Set("x-codex-beta-features", officialOpenAIHTTPBetaFeatures)
	headers.Set("OpenAI-Beta", openAIWSBetaV2Value)
	headers.Set("version", officialOpenAIWSClientVersion)

	for _, name := range []string{
		"User-Agent",
		"originator",
		"x-codex-beta-features",
		"session-id",
		"thread-id",
		"x-client-request-id",
		"x-codex-window-id",
		openAIWSTurnMetadataHeader,
		"OpenAI-Beta",
		"version",
	} {
		result.Modifications = append(result.Modifications, OfficialEgressModification{
			Kind:  "header",
			Field: name,
		})
	}
	return result, nil
}

func requiredOfficialEgressFieldValue(
	egressContext *OfficialEgressContext,
	name OfficialEgressFieldName,
) (string, error) {
	field, exists := egressContext.Field(name)
	if !exists || strings.TrimSpace(field.Value()) == "" {
		return "", fmt.Errorf(
			"OpenAI official egress WebSocket requires frozen field %s",
			name,
		)
	}
	return field.Value(), nil
}

// shouldPreserveOpenAIOfficialEgressWSPreviousResponseID 只在官方画像中识别
// 已由上一轮真实响应确认的续链。无效或外部 previous_response_id 继续走原受控回放。
func shouldPreserveOpenAIOfficialEgressWSPreviousResponseID(
	ctx context.Context,
	currentPreviousResponseID string,
	expectedPreviousResponseID string,
) bool {
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled ||
		egressContext.TargetPlatform() != PlatformOpenAI ||
		egressContext.Transport() != OfficialEgressTransportWebSocket {
		return false
	}
	currentPreviousResponseID = strings.TrimSpace(currentPreviousResponseID)
	expectedPreviousResponseID = strings.TrimSpace(expectedPreviousResponseID)
	return currentPreviousResponseID != "" &&
		expectedPreviousResponseID != "" &&
		currentPreviousResponseID == expectedPreviousResponseID
}

func isDerivedOpenAIOfficialEgressWSContext(ctx context.Context) bool {
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	return enabled &&
		egressContext.TargetPlatform() == PlatformOpenAI &&
		egressContext.Transport() == OfficialEgressTransportWebSocket &&
		egressContext.openAIWSDerived != nil
}

// finalizeOpenAIOfficialEgressWSFrame 对已知出站帧执行最小差异校验。
// 未知帧必须逐字节保持；只有上游已经证明续链无效时，调用方才可声明受控回放。
func finalizeOpenAIOfficialEgressWSFrame(
	ctx context.Context,
	original []byte,
	candidate []byte,
	expectedPreviousResponseID string,
	allowControlledReplay bool,
) ([]byte, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled {
		return candidate, result, nil
	}
	if egressContext.TargetPlatform() != PlatformOpenAI ||
		egressContext.Transport() != OfficialEgressTransportWebSocket ||
		!egressContext.IsFrozen() {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket frame context is invalid",
		)
	}
	if egressContext.openAIWSDerived != nil {
		return finalizeDerivedOpenAIOfficialEgressWSFrame(
			egressContext,
			original,
			candidate,
		)
	}
	// 正常官方链路的候选帧与入口帧逐字节一致；先走零分配快路径，
	// 避免对包含大型工具描述的首帧重复执行两次 JSON 解析。
	if bytes.Equal(original, candidate) {
		return candidate, result, nil
	}

	var originalPayload map[string]any
	if err := json.Unmarshal(original, &originalPayload); err != nil {
		return nil, result, fmt.Errorf(
			"decode OpenAI official egress ingress WebSocket frame: %w",
			err,
		)
	}
	eventType := strings.TrimSpace(officialOpenAIString(originalPayload, "type"))
	if eventType != officialOpenAIWSResponseCreateType {
		if !bytes.Equal(original, candidate) {
			return nil, result, errors.New(
				"OpenAI official egress unknown WebSocket frame was modified",
			)
		}
		return candidate, result, nil
	}

	var candidatePayload map[string]any
	if err := json.Unmarshal(candidate, &candidatePayload); err != nil {
		return nil, result, fmt.Errorf(
			"decode OpenAI official egress outbound WebSocket frame: %w",
			err,
		)
	}
	if strings.TrimSpace(officialOpenAIString(candidatePayload, "type")) != eventType {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket frame type was modified",
		)
	}
	if allowControlledReplay {
		return candidate, result, nil
	}

	originalPreviousResponseID, originalPreviousPresent := originalPayload["previous_response_id"]
	candidatePreviousResponseID, candidatePreviousPresent := candidatePayload["previous_response_id"]
	if originalPreviousPresent != candidatePreviousPresent ||
		!reflect.DeepEqual(originalPreviousResponseID, candidatePreviousResponseID) {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket previous_response_id was modified",
		)
	}
	if originalPreviousPresent {
		originalPrevious, _ := originalPreviousResponseID.(string)
		if strings.TrimSpace(expectedPreviousResponseID) != "" &&
			strings.TrimSpace(originalPrevious) != strings.TrimSpace(expectedPreviousResponseID) {
			return nil, result, errors.New(
				"OpenAI official egress WebSocket previous_response_id conflicts with prior response",
			)
		}
	}
	for _, field := range []string{
		"input",
		"client_metadata",
		"prompt_cache_key",
	} {
		originalValue, originalPresent := originalPayload[field]
		candidateValue, candidatePresent := candidatePayload[field]
		if originalPresent != candidatePresent || !reflect.DeepEqual(originalValue, candidateValue) {
			return nil, result, fmt.Errorf(
				"OpenAI official egress WebSocket %s was modified",
				field,
			)
		}
	}
	if !reflect.DeepEqual(
		collectOfficialOpenAIAdditionalTools(originalPayload),
		collectOfficialOpenAIAdditionalTools(candidatePayload),
	) {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket additional_tools were modified",
		)
	}
	if !reflect.DeepEqual(
		collectOfficialOpenAICallIDs(originalPayload),
		collectOfficialOpenAICallIDs(candidatePayload),
	) {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket call_id was modified",
		)
	}
	return candidate, result, nil
}

// finalizeDerivedOpenAIOfficialEgressWSFrame 把第三方 response.create
// 归一化为 Codex 0.145.0 的 WS 帧。业务输入、模型、reasoning 配置和
// call_id 保持不变，只补齐官方固定外层与动态身份。
func finalizeDerivedOpenAIOfficialEgressWSFrame(
	egressContext *OfficialEgressContext,
	original []byte,
	candidate []byte,
) ([]byte, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	var payload map[string]any
	if err := json.Unmarshal(candidate, &payload); err != nil {
		return nil, result, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket frame: %w",
			err,
		)
	}
	eventType := strings.TrimSpace(officialOpenAIString(payload, "type"))
	if eventType != officialOpenAIWSResponseCreateType {
		if !bytes.Equal(original, candidate) {
			return nil, result, errors.New(
				"OpenAI official egress unknown WebSocket frame was modified",
			)
		}
		return candidate, result, nil
	}

	originalCallIDs := collectOfficialOpenAICallIDs(payload)
	if instructions, exists := payload["instructions"]; exists {
		if _, err := moveOfficialOpenAIHTTPInstructionsToInput(
			payload,
			instructions,
		); err != nil {
			return nil, result, err
		}
	}
	if _, err := ensureOpenAIResponsesLiteReasoningContext(payload); err != nil {
		return nil, result, err
	}
	if _, err := normalizeDerivedOfficialOpenAIHTTPBody(payload); err != nil {
		return nil, result, err
	}

	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		egressContext,
		payload,
		openAIWSRawPayloadHasToolCallOutput(candidate),
	)
	if err != nil {
		return nil, result, err
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey
	if !reflect.DeepEqual(originalCallIDs, collectOfficialOpenAICallIDs(payload)) {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket call_id was modified",
		)
	}

	finalized, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return nil, result, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket frame: %w",
			err,
		)
	}
	for _, field := range []string{
		"client_metadata",
		"prompt_cache_key",
		"parallel_tool_calls",
		"store",
		"stream",
		"tool_choice",
		"include",
		"text.verbosity",
		"reasoning.context",
		"input",
	} {
		result.Modifications = append(result.Modifications, OfficialEgressModification{
			Kind:  "frame",
			Field: field,
		})
	}
	return finalized, result, nil
}

// buildDerivedOpenAIOfficialEgressWSPrewarmFrame 为普通第三方客户端补出
// Codex CLI 在每条新 WS 连接上的 generate=false 预热帧。
//
// 预热只承载开发者指令和 additional_tools，不得提前发送用户输入；正式帧会
// 使用预热响应 ID 作为 previous_response_id，从而复现官方客户端的连接内续链。
func buildDerivedOpenAIOfficialEgressWSPrewarmFrame(
	ctx context.Context,
	candidate []byte,
) ([]byte, bool, error) {
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled ||
		egressContext.TargetPlatform() != PlatformOpenAI ||
		egressContext.Transport() != OfficialEgressTransportWebSocket ||
		egressContext.openAIWSDerived == nil {
		return nil, false, nil
	}
	if openAIWSRawPayloadHasToolCallOutput(candidate) {
		return nil, false, nil
	}

	var payload map[string]any
	if err := json.Unmarshal(candidate, &payload); err != nil {
		return nil, false, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket prewarm source: %w",
			err,
		)
	}
	if strings.TrimSpace(officialOpenAIString(payload, "type")) != officialOpenAIWSResponseCreateType {
		return nil, false, nil
	}
	if _, exists := payload["previous_response_id"]; exists {
		return nil, false, nil
	}
	if _, exists := payload["generate"]; exists {
		return nil, false, nil
	}

	input, _ := payload["input"].([]any)
	prewarmInput := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		itemType := strings.TrimSpace(officialOpenAIString(item, "type"))
		role := strings.TrimSpace(officialOpenAIString(item, "role"))
		if itemType == officialOpenAIAdditionalToolsType || role == "developer" {
			prewarmInput = append(prewarmInput, item)
		}
	}
	if len(prewarmInput) == 0 {
		return nil, false, errors.New(
			"OpenAI official egress WebSocket prewarm requires developer context",
		)
	}

	payload["input"] = prewarmInput
	payload["generate"] = false
	delete(payload, "previous_response_id")
	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadata(
		egressContext,
		payload,
	)
	if err != nil {
		return nil, false, err
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey

	prewarm, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return nil, false, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket prewarm frame: %w",
			err,
		)
	}
	return prewarm, true, nil
}

// chainDerivedOpenAIOfficialEgressWSBusinessFrame 把预热响应挂到正式业务帧上。
// additional_tools 已在预热帧登记，正式帧只保留业务消息，与 Codex CLI
// 捕获到的 prewarm → response.create 两帧关系一致。
func chainDerivedOpenAIOfficialEgressWSBusinessFrame(
	ctx context.Context,
	candidate []byte,
	prewarmResponseID string,
) ([]byte, error) {
	prewarmResponseID = strings.TrimSpace(prewarmResponseID)
	if prewarmResponseID == "" {
		return nil, errors.New(
			"OpenAI official egress WebSocket prewarm response ID is empty",
		)
	}
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled || egressContext.openAIWSDerived == nil {
		return nil, errors.New(
			"OpenAI official egress WebSocket derived context is unavailable",
		)
	}

	var payload map[string]any
	if err := json.Unmarshal(candidate, &payload); err != nil {
		return nil, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket business frame: %w",
			err,
		)
	}
	input, _ := payload["input"].([]any)
	businessInput := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if ok &&
			strings.TrimSpace(officialOpenAIString(item, "type")) == officialOpenAIAdditionalToolsType {
			continue
		}
		businessInput = append(businessInput, rawItem)
	}
	payload["input"] = businessInput
	payload["previous_response_id"] = prewarmResponseID
	delete(payload, "generate")

	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadata(
		egressContext,
		payload,
	)
	if err != nil {
		return nil, err
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey

	finalized, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return nil, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket business frame: %w",
			err,
		)
	}
	return finalized, nil
}

// buildDerivedOpenAIOfficialEgressWSToolContinuationFrame 把第三方客户端
// 携带完整历史的工具结果帧收敛为 Codex CLI 的最小续链形态。
//
// 上一轮工具调用已经存在于上游响应中，因此这里只发送新增的工具输出，并通过
// previous_response_id 关联上一轮。call_id 原样保留，不能重新生成或改写。
func buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(
	ctx context.Context,
	candidate []byte,
	previousResponseID string,
) ([]byte, bool, error) {
	if !isDerivedOpenAIOfficialEgressWSContext(ctx) ||
		!openAIWSRawPayloadHasToolCallOutput(candidate) {
		return candidate, false, nil
	}
	previousResponseID = strings.TrimSpace(previousResponseID)
	if previousResponseID == "" {
		return nil, false, errors.New(
			"OpenAI official egress WebSocket tool continuation requires prior response ID",
		)
	}

	egressContext, _ := OfficialEgressContextFromContext(ctx)
	var payload map[string]any
	if err := json.Unmarshal(candidate, &payload); err != nil {
		return nil, false, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket tool continuation: %w",
			err,
		)
	}
	input, _ := payload["input"].([]any)
	toolOutputs := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		if !isCodexToolCallOutputItemType(
			strings.TrimSpace(officialOpenAIString(item, "type")),
		) {
			continue
		}
		if strings.TrimSpace(officialOpenAIString(item, "call_id")) == "" {
			return nil, false, errors.New(
				"OpenAI official egress WebSocket tool output call_id is empty",
			)
		}
		toolOutputs = append(toolOutputs, item)
	}
	if len(toolOutputs) == 0 {
		return nil, false, errors.New(
			"OpenAI official egress WebSocket tool continuation has no tool output",
		)
	}

	payload["input"] = toolOutputs
	payload["previous_response_id"] = previousResponseID
	delete(payload, "generate")
	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadata(
		egressContext,
		payload,
	)
	if err != nil {
		return nil, false, err
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey

	finalized, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return nil, false, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket tool continuation: %w",
			err,
		)
	}
	return finalized, true, nil
}

func buildDerivedOfficialOpenAIWSFrameMetadata(
	egressContext *OfficialEgressContext,
	payload map[string]any,
) (map[string]any, string, error) {
	return buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		egressContext,
		payload,
		false,
	)
}

// buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy 生成 Codex WS
// 动态身份。工具结果属于上一轮工具调用的继续，即使第三方客户端重复发送的完整
// 历史中包含已变化的动态环境文本，也必须继承现有 turn_id 与开始时间。
func buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
	egressContext *OfficialEgressContext,
	payload map[string]any,
	preserveCurrentTurn bool,
) (map[string]any, string, error) {
	if egressContext == nil || egressContext.openAIWSDerived == nil {
		return nil, "", errors.New(
			"OpenAI official egress WebSocket derived state is unavailable",
		)
	}
	installationID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldDeviceID,
	)
	if err != nil {
		return nil, "", err
	}
	sessionID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldSessionID,
	)
	if err != nil {
		return nil, "", err
	}
	threadID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldThreadID,
	)
	if err != nil {
		return nil, "", err
	}
	windowID, err := requiredOfficialEgressFieldValue(
		egressContext,
		OfficialEgressFieldWindowID,
	)
	if err != nil {
		return nil, "", err
	}

	generate, _ := payload["generate"].(bool)
	prewarm := generate == false && payload["generate"] != nil
	turnID := ""
	turnStartedAtMS := int64(0)
	requestKind := "prewarm"
	if !prewarm {
		requestKind = "turn"
		_, lastUserAnchor := officialOpenAIHTTPUserAnchorsFromPayload(payload)
		state := egressContext.openAIWSDerived
		state.mu.Lock()
		if !preserveCurrentTurn && lastUserAnchor != "" {
			state.lastTurnID = generateSessionUUID(
				"openai-official-egress-turn|" + sessionID + "|" + lastUserAnchor,
			)
			state.lastTurnStartedAtMS = time.Now().UnixMilli()
		} else if state.lastTurnID == "" {
			frameBytes, _ := marshalOpenAIUpstreamJSON(payload)
			state.lastTurnID = generateSessionUUID(
				"openai-official-egress-turn|" + sessionID + "|" +
					fmt.Sprintf("%x", frameBytes),
			)
			state.lastTurnStartedAtMS = time.Now().UnixMilli()
		}
		turnID = state.lastTurnID
		turnStartedAtMS = state.lastTurnStartedAtMS
		state.mu.Unlock()
	}

	turnMetadata := map[string]any{
		"installation_id": installationID,
		"session_id":      sessionID,
		"thread_id":       threadID,
		"turn_id":         turnID,
		"window_id":       windowID,
		"request_kind":    requestKind,
		"thread_source":   "user",
		"sandbox":         "seccomp",
	}
	if !prewarm {
		turnMetadata["turn_started_at_unix_ms"] = turnStartedAtMS
	}
	turnMetadataBytes, err := json.Marshal(turnMetadata)
	if err != nil {
		return nil, "", fmt.Errorf(
			"encode derived OpenAI official egress WebSocket turn metadata: %w",
			err,
		)
	}
	return map[string]any{
		"x-codex-installation-id": installationID,
		"session_id":              sessionID,
		"thread_id":               threadID,
		"turn_id":                 turnID,
		"x-codex-window-id":       windowID,
		"x-codex-turn-metadata":   string(turnMetadataBytes),
		"x-codex-ws-stream-request-start-ms": strconv.FormatInt(
			time.Now().UnixMilli(),
			10,
		),
		"ws_request_header_x_openai_internal_codex_responses_lite": "true",
	}, sessionID, nil
}

func officialOpenAIHTTPUserAnchorsFromPayload(
	payload map[string]any,
) (string, string) {
	body, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return "", ""
	}
	return officialOpenAIHTTPUserAnchors(body)
}
