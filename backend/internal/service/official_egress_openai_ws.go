package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
)

const (
	officialOpenAIWSCompressionOffer   = "permessage-deflate; client_max_window_bits"
	officialOpenAIWSResponseCreateType = "response.create"
	officialOpenAIAdditionalToolsType  = "additional_tools"
	officialOpenAIWSItemTurnMetadata   = "internal_chat_message_metadata_passthrough"
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
	parentThreadID string
	subagent       string
	memoryGenerate bool
	source         OfficialEgressFieldSource
}

// officialOpenAIWSDerivedState 保存所有 WS 会话统一派生的逐轮身份。
// 连接级身份在拨号前冻结；逐轮 Turn ID 则必须在工具续轮中延续，因此单独按
// 当前下游 WebSocket 会话串行维护。
type officialOpenAIWSDerivedState struct {
	mu                  sync.Mutex
	lastTurnID          string
	lastTurnStartedAtMS int64
}

// prepareOpenAIOfficialEgressWSContext 从入口首帧和握手头提取语义锚点，再登记
// active 画像统一派生的身份。这些字段在拨号前冻结，后续帧不能改写连接级身份。
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
	identity, err := resolveOfficialOpenAIWSIdentity(
		c,
		account,
		firstPayload,
		egressContext.ProfileMode(),
	)
	if err != nil {
		return err
	}
	normalizeOfficialCodexConditionalIdentity(
		egressContext,
		identity.subagent,
		identity.parentThreadID,
		identity.memoryGenerate,
	)
	if identity.source == OfficialEgressFieldSourceDerived {
		egressContext.openAIWSDerived = &officialOpenAIWSDerivedState{}
	}
	return registerOfficialOpenAIWSIdentity(egressContext, identity)
}

func resolveOfficialOpenAIWSIdentity(
	c *gin.Context,
	account *Account,
	firstPayload []byte,
	profileMode string,
) (officialOpenAIWSIdentity, error) {
	return deriveOfficialOpenAIWSIdentity(c, account, firstPayload, profileMode)
}

// resolveExplicitOfficialOpenAIWSIdentity 只用于离线 fixture 与诊断，验证某份
// 握手/首帧样本内部是否自洽。生产出站统一调用 resolveOfficialOpenAIWSIdentity
// 派生 active 画像身份，绝不让入站客户端类型取得 wire 身份所有权。
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
		"installation_id": identity.installationID,
		"session_id":      identity.sessionID,
		"thread_id":       identity.threadID,
		"client_request":  identity.clientRequest,
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
	if identity.threadID != identity.clientRequest {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket thread/request identity conflicts",
		)
	}
	windowParts := strings.Split(identity.windowID, ":")
	if len(windowParts) != 2 || windowParts[0] != identity.threadID {
		return officialOpenAIWSIdentity{}, errors.New(
			"OpenAI official egress WebSocket window_id conflicts with thread",
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
	if _, err := validateOfficialOpenAIIngressIdentityKind(
		"OpenAI official egress WebSocket",
		c,
		metadata,
		turnMetadata,
		officialOpenAIIngressIdentityValues{
			sessionID:      identity.sessionID,
			threadID:       identity.threadID,
			promptCacheKey: identity.promptCacheKey,
		},
		false,
	); err != nil {
		return officialOpenAIWSIdentity{}, err
	}
	return identity, nil
}

// deriveOfficialOpenAIWSIdentity 为所有客户端统一派生连接级身份。
// 握手使用官方 prewarm metadata；真正的 turn metadata 在每个
// response.create 出站前重新生成。
func deriveOfficialOpenAIWSIdentity(
	c *gin.Context,
	account *Account,
	firstPayload []byte,
	profileMode string,
) (officialOpenAIWSIdentity, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContract(firstPayload)
	if err != nil {
		return officialOpenAIWSIdentity{}, err
	}
	base, err := deriveOfficialOpenAIHTTPIdentity(c, account, firstPayload, contract, profileMode)
	if err != nil {
		return officialOpenAIWSIdentity{}, err
	}
	turnMetadataBytes, err := marshalOfficialOpenAITurnMetadata(map[string]any{
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
		parentThreadID: base.parentThreadID,
		subagent:       base.subagent,
		memoryGenerate: base.memoryGenerate,
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

// prepareOpenAIOfficialEgressSemanticWSFrame 只执行 service 所有的业务帧转换与
// 身份来源校验。最终字段闭集、线序和一次性写能力由 ExecutorWebSocketSession 决定。
func prepareOpenAIOfficialEgressSemanticWSFrame(
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
		return prepareDerivedOpenAIOfficialEgressWSFrame(
			egressContext,
			original,
			candidate,
			expectedPreviousResponseID,
			allowControlledReplay,
		)
	}

	originalPayload, err := decodeOfficialJSONObjectUseNumber(original)
	if err != nil {
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

	candidatePayload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
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
	allowedTopLevel, err := officialOpenAITopLevelAllowSetForMode(
		egressContext.ProfileMode(),
		officialCodexEndpointResponsesWS,
	)
	if err != nil {
		return nil, result, err
	}
	if err := validateOfficialOpenAITopLevelContract(
		candidatePayload,
		allowedTopLevel,
		true,
	); err != nil {
		return nil, result, err
	}
	if !allowControlledReplay {
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
	}

	modified, err := finalizeOfficialOpenAIWSInputTurnMetadata(candidatePayload)
	if err != nil {
		return nil, result, err
	}
	if !modified {
		return candidate, result, nil
	}
	finalized, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), candidatePayload, candidate,
	)
	if err != nil {
		return nil, result, fmt.Errorf(
			"encode OpenAI official egress WebSocket item turn metadata: %w",
			err,
		)
	}
	result.Modifications = append(result.Modifications, OfficialEgressModification{
		Kind:  "frame",
		Field: "input.*." + officialOpenAIWSItemTurnMetadata + ".turn_id",
	})
	return finalized, result, nil
}

// prepareDerivedOpenAIOfficialEgressWSFrame 把任意入口的 response.create
// 归一化为 ProfileSpec 冻结版本的 WS 帧。业务输入、模型、reasoning 配置和
// call_id 保持不变，只补齐官方固定外层与动态身份。
func prepareDerivedOpenAIOfficialEgressWSFrame(
	egressContext *OfficialEgressContext,
	original []byte,
	candidate []byte,
	expectedPreviousResponseID string,
	allowControlledReplay bool,
) ([]byte, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	if err := validateUnifiedOpenAIWSBusinessContract(
		original,
		candidate,
		expectedPreviousResponseID,
		allowControlledReplay,
	); err != nil {
		return nil, result, err
	}
	payload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
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
	toolPresentationModified, err := officialCodexNormalizeDerivedToolPresentation(
		egressContext.ProfileVersion(),
		codexEndpointID(egressContext.CodexEndpointProfileID()),
		payload,
	)
	if err != nil {
		return nil, result, err
	}
	if instructions, exists := payload["instructions"]; exists && egressContext.responsesLite {
		if _, err := moveOfficialOpenAIHTTPInstructionsToInput(
			payload,
			instructions,
		); err != nil {
			return nil, result, err
		}
	}
	if egressContext.responsesLite {
		if _, err := ensureOpenAIResponsesLiteReasoningContext(payload); err != nil {
			return nil, result, err
		}
	}
	allowedTopLevel, err := officialOpenAITopLevelAllowSetForMode(
		egressContext.ProfileMode(),
		officialCodexEndpointResponsesWS,
	)
	if err != nil {
		return nil, result, err
	}
	if _, err := normalizeDerivedOfficialOpenAIHTTPBody(
		payload,
		egressContext.responsesLite,
		egressContext.parallelTools,
		officialOpenAIReasoningDefaultsFromContext(egressContext),
		allowedTopLevel,
		// WS 帧定型阶段拿不到入站 HTTP 头，投影告警只报告字段名与入口类型；
		// 握手阶段的入站身份已由 WS 入口自行记录。
		"",
	); err != nil {
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
	if _, err := finalizeOfficialOpenAIWSInputTurnMetadata(payload); err != nil {
		return nil, result, err
	}
	if !reflect.DeepEqual(originalCallIDs, collectOfficialOpenAICallIDs(payload)) {
		return nil, result, errors.New(
			"OpenAI official egress WebSocket call_id was modified",
		)
	}

	finalized, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), payload, candidate,
	)
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
	if toolPresentationModified {
		result.Modifications = append(result.Modifications, OfficialEgressModification{
			Kind:  "frame",
			Field: "tools.image_gen",
		})
	}
	return finalized, result, nil
}

// validateUnifiedOpenAIWSBusinessContract 只保护业务语义，不校验 wire 身份。
// client_metadata、prompt_cache_key 与身份头都由 active 画像重建，因此入口冲突
// 不能成为 502；但适配器擅自删除续链或扩张历史输入仍属于业务破坏，必须拒绝。
func validateUnifiedOpenAIWSBusinessContract(
	original []byte,
	candidate []byte,
	expectedPreviousResponseID string,
	allowControlledReplay bool,
) error {
	originalPayload, err := decodeOfficialJSONObjectUseNumber(original)
	if err != nil {
		return fmt.Errorf("decode unified OpenAI official egress ingress WebSocket frame: %w", err)
	}
	if strings.TrimSpace(officialOpenAIString(originalPayload, "type")) != officialOpenAIWSResponseCreateType {
		if !bytes.Equal(original, candidate) {
			return errors.New("OpenAI official egress unknown WebSocket frame was modified")
		}
		return nil
	}
	candidatePayload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
		return fmt.Errorf("decode unified OpenAI official egress outbound WebSocket frame: %w", err)
	}
	if strings.TrimSpace(officialOpenAIString(candidatePayload, "type")) != officialOpenAIWSResponseCreateType {
		return errors.New("OpenAI official egress WebSocket frame type was modified")
	}
	if allowControlledReplay {
		return nil
	}
	originalPrevious, originalPreviousPresent := originalPayload["previous_response_id"]
	candidatePrevious, candidatePreviousPresent := candidatePayload["previous_response_id"]
	if originalPreviousPresent != candidatePreviousPresent || !reflect.DeepEqual(originalPrevious, candidatePrevious) {
		return errors.New("OpenAI official egress WebSocket previous_response_id was modified")
	}
	if originalPreviousPresent {
		originalValue, _ := originalPrevious.(string)
		if strings.TrimSpace(expectedPreviousResponseID) != "" &&
			strings.TrimSpace(originalValue) != strings.TrimSpace(expectedPreviousResponseID) {
			return errors.New("OpenAI official egress WebSocket previous_response_id conflicts with prior response")
		}
	}
	originalHistory, err := canonicalOfficialOpenAIWSBusinessHistory(originalPayload)
	if err != nil {
		return err
	}
	candidateHistory, err := canonicalOfficialOpenAIWSBusinessHistory(candidatePayload)
	if err != nil {
		return err
	}
	if !bytes.Equal(originalHistory, candidateHistory) {
		return errors.New("OpenAI official egress WebSocket input was modified")
	}
	return nil
}

// canonicalOfficialOpenAIWSBusinessHistory 把允许变化的表示层字段剥离后再比较。
// additional_tools 与逐项 turn metadata 都由画像/适配器合法重建；消息、工具调用、
// 工具输出及其业务参数仍必须保持一致。
func canonicalOfficialOpenAIWSBusinessHistory(payload map[string]any) ([]byte, error) {
	cloned := cloneOfficialOpenAIMap(payload)
	if _, err := normalizeDerivedOfficialOpenAIInput(cloned); err != nil {
		return nil, fmt.Errorf("normalize OpenAI official egress WebSocket business history: %w", err)
	}
	input, _ := cloned["input"].([]any)
	filtered := make([]any, 0, len(input))
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			filtered = append(filtered, rawItem)
			continue
		}
		if officialOpenAIString(item, "type") == officialOpenAIAdditionalToolsType {
			continue
		}
		itemClone := cloneOfficialOpenAIMap(item)
		delete(itemClone, officialOpenAIWSItemTurnMetadata)
		if officialOpenAIString(itemClone, "type") == "message" {
			itemClone["content"] = officialOpenAIHTTPMessageContentText(itemClone["content"])
		}
		filtered = append(filtered, itemClone)
	}
	encoded, err := json.Marshal(filtered)
	if err != nil {
		return nil, fmt.Errorf("encode OpenAI official egress WebSocket business history: %w", err)
	}
	return encoded, nil
}

// injectOfficialOpenAIWSTurnState 把上游握手返回的连接级 turn-state 写入
// response.create 的 client_metadata，并用官方顶层字段顺序重新编码。
// extractOpenAIWSTurnStateFromUpstreamEvent 从上游事件流中提取 turn-state。
//
// 官方 Codex 的来源是流内 `response.metadata` 事件顶层的 `headers` 对象，在其中
// 大小写不敏感地查找 x-codex-turn-state（codex-api/src/sse/responses.rs 的
// turn_state() 与 header_turn_state_value_from_json）。握手响应头那条路在官方
// CLI 里是死代码——core 调用时固定传 None。
//
// 此前 Sub2API 只认握手响应头，而上游按协议在事件流里下发，101 响应通常不带该头，
// 于是整条 turn-state 链路长期取到空串。这也是历次抓包"从未观察到 turn-state"的原因。
// 返回空串表示该帧不携带 turn-state，调用方应保持现值不变。
func extractOpenAIWSTurnStateFromUpstreamEvent(message []byte) string {
	if len(message) == 0 {
		return ""
	}
	if eventType := strings.TrimSpace(gjson.GetBytes(message, "type").String()); eventType != openAIWSResponseMetadataEvent {
		return ""
	}
	headers := gjson.GetBytes(message, "headers")
	if !headers.IsObject() {
		return ""
	}
	turnState := ""
	headers.ForEach(func(key, value gjson.Result) bool {
		if !strings.EqualFold(strings.TrimSpace(key.String()), openAIWSTurnStateHeader) {
			return true
		}
		turnState = strings.TrimSpace(value.String())
		return false
	})
	return turnState
}

func injectOfficialOpenAIWSTurnState(
	ctx context.Context,
	payload []byte,
	turnState string,
) ([]byte, error) {
	turnState = strings.TrimSpace(turnState)
	if turnState == "" {
		return payload, nil
	}
	body, err := decodeOfficialJSONObjectUseNumber(payload)
	if err != nil {
		return nil, fmt.Errorf("decode OpenAI official egress WebSocket turn-state frame: %w", err)
	}
	metadata, _ := body["client_metadata"].(map[string]any)
	if metadata == nil {
		metadata = make(map[string]any)
		body["client_metadata"] = metadata
	}
	metadata[openAIWSTurnStateHeader] = turnState
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	if !ok {
		return nil, errors.New("WebSocket turn-state 缺少冻结出站上下文")
	}
	encoded, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), body, payload,
	)
	if err != nil {
		return nil, fmt.Errorf("encode OpenAI official egress WebSocket turn-state frame: %w", err)
	}
	return encoded, nil
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

	payload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
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
	if _, err := finalizeOfficialOpenAIWSInputTurnMetadata(payload); err != nil {
		return nil, false, err
	}

	prewarm, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), payload, candidate,
	)
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

	payload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
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

	// 正式业务帧与刚刚规范化的首帧属于同一轮；移除 additional_tools
	// 改变了 input 下标，但不能因此生成新的 Turn ID。
	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		egressContext,
		payload,
		true,
	)
	if err != nil {
		return nil, err
	}
	// turn-state 是上游按连接下发的粘性路由令牌，属于连接级状态而非逐帧重建的身份字段。
	// metadata 在这里整体替换，会把预热之前已经注入的 turn-state 一并丢掉，
	// 因此必须先从改写前的帧取回再并入新 metadata。
	if previousMetadata, ok := payload["client_metadata"].(map[string]any); ok {
		if turnState, ok := previousMetadata[openAIWSTurnStateHeader].(string); ok &&
			strings.TrimSpace(turnState) != "" {
			metadata[openAIWSTurnStateHeader] = turnState
		}
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey
	if _, err := finalizeOfficialOpenAIWSInputTurnMetadata(payload); err != nil {
		return nil, err
	}

	finalized, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), payload, candidate,
	)
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
// previous_response_id 关联上一轮。断线后若可信锚点不可用，则仅在 input 内的实际
// 工具调用完整覆盖所有输出时保留完整上下文并开启新链。call_id 原样保留，不能
// 重新生成或改写。
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
		coverage := AnalyzeToolCallOutputContextCoverageBytes(candidate)
		if coverage.ConcreteContextCoversAllCallIDs {
			withoutPrevious, removed, err := dropPreviousResponseIDFromRawPayload(candidate)
			if err != nil {
				return nil, false, fmt.Errorf(
					"remove untrusted previous response ID from complete OpenAI official egress WebSocket context: %w",
					err,
				)
			}
			if removed {
				return withoutPrevious, true, nil
			}
			return candidate, false, nil
		}
		return nil, false, errors.New(
			"OpenAI official egress WebSocket tool continuation requires prior response ID or complete tool call context",
		)
	}

	egressContext, _ := OfficialEgressContextFromContext(ctx)
	payload, err := decodeOfficialJSONObjectUseNumber(candidate)
	if err != nil {
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
	if _, err := finalizeOfficialOpenAIWSInputTurnMetadata(payload); err != nil {
		return nil, false, err
	}

	finalized, err := marshalOfficialOpenAIWSJSONPreservingRaw(
		egressContext.ProfileMode(), payload, candidate,
	)
	if err != nil {
		return nil, false, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket tool continuation: %w",
			err,
		)
	}
	return finalized, true, nil
}

// finalizeOfficialOpenAIWSInputTurnMetadata 对齐 Codex OAuth WebSocket 的
// 逐项 Turn 元数据。官方预热帧不携带该字段；业务帧中每个 input 项都带有
// 所属轮次的 turn_id，历史项保留历史轮次，当前后缀使用 client_metadata.turn_id。
func finalizeOfficialOpenAIWSInputTurnMetadata(payload map[string]any) (bool, error) {
	input, ok := payload["input"].([]any)
	if !ok {
		return false, errors.New(
			"OpenAI official egress WebSocket response.create requires input array",
		)
	}
	generateValue, generatePresent := payload["generate"]
	prewarm := false
	if generatePresent {
		generate, valid := generateValue.(bool)
		if !valid {
			return false, errors.New(
				"OpenAI official egress WebSocket generate must be boolean",
			)
		}
		prewarm = !generate
	}
	if prewarm {
		modified := false
		for _, rawItem := range input {
			item, valid := rawItem.(map[string]any)
			if !valid {
				continue
			}
			if _, exists := item[officialOpenAIWSItemTurnMetadata]; exists {
				delete(item, officialOpenAIWSItemTurnMetadata)
				modified = true
			}
		}
		return modified, nil
	}

	metadata, ok := payload["client_metadata"].(map[string]any)
	if !ok {
		return false, errors.New(
			"OpenAI official egress WebSocket business frame requires client_metadata",
		)
	}
	currentTurnID := strings.TrimSpace(officialOpenAIString(metadata, "turn_id"))
	if _, err := uuid.Parse(currentTurnID); err != nil {
		return false, errors.New(
			"OpenAI official egress WebSocket business turn_id must be UUID",
		)
	}
	sessionID := strings.TrimSpace(officialOpenAIString(metadata, "session_id"))
	if _, err := uuid.Parse(sessionID); err != nil {
		return false, errors.New(
			"OpenAI official egress WebSocket business session_id must be UUID",
		)
	}

	segments := splitOfficialOpenAIWSInputTurnSegments(input)
	modified := false
	for segmentIndex, segment := range segments {
		turnID := currentTurnID
		if segmentIndex < len(segments)-1 {
			var err error
			turnID, err = resolveOfficialOpenAIWSHistoricalTurnID(
				input,
				segment,
				sessionID,
				segmentIndex,
			)
			if err != nil {
				return false, err
			}
		}
		for _, itemIndex := range segment {
			item, valid := input[itemIndex].(map[string]any)
			if !valid {
				return false, fmt.Errorf(
					"OpenAI official egress WebSocket input item %d must be object",
					itemIndex,
				)
			}
			itemMetadata, exists := item[officialOpenAIWSItemTurnMetadata]
			if !exists {
				item[officialOpenAIWSItemTurnMetadata] = map[string]any{"turn_id": turnID}
				modified = true
				continue
			}
			itemMetadataMap, valid := itemMetadata.(map[string]any)
			if !valid {
				return false, fmt.Errorf(
					"OpenAI official egress WebSocket input item %d turn metadata must be object",
					itemIndex,
				)
			}
			existingTurnID := strings.TrimSpace(
				officialOpenAIString(itemMetadataMap, "turn_id"),
			)
			if existingTurnID == "" {
				itemMetadataMap["turn_id"] = turnID
				modified = true
				continue
			}
			if _, err := uuid.Parse(existingTurnID); err != nil {
				return false, fmt.Errorf(
					"OpenAI official egress WebSocket input item %d turn_id must be UUID",
					itemIndex,
				)
			}
			if existingTurnID != turnID {
				return false, fmt.Errorf(
					"OpenAI official egress WebSocket input item %d turn_id conflicts with its turn",
					itemIndex,
				)
			}
		}
	}
	return modified, nil
}

// splitOfficialOpenAIWSInputTurnSegments 以“助手输出后的下一条用户消息”为
// 新轮次边界。连续的用户上下文项属于同一轮，工具输出也继续归入当前轮次。
func splitOfficialOpenAIWSInputTurnSegments(input []any) [][]int {
	if len(input) == 0 {
		return nil
	}
	segments := make([][]int, 1)
	completedAssistantTurn := false
	for index, rawItem := range input {
		item, _ := rawItem.(map[string]any)
		role := strings.TrimSpace(officialOpenAIString(item, "role"))
		if role == "user" && completedAssistantTurn && len(segments[len(segments)-1]) > 0 {
			segments = append(segments, nil)
			completedAssistantTurn = false
		}
		segments[len(segments)-1] = append(segments[len(segments)-1], index)
		if role == "assistant" {
			completedAssistantTurn = true
		}
	}
	return segments
}

func resolveOfficialOpenAIWSHistoricalTurnID(
	input []any,
	segment []int,
	sessionID string,
	segmentIndex int,
) (string, error) {
	existingTurnID := ""
	lastUserAnchor := ""
	for _, itemIndex := range segment {
		item, valid := input[itemIndex].(map[string]any)
		if !valid {
			return "", fmt.Errorf(
				"OpenAI official egress WebSocket input item %d must be object",
				itemIndex,
			)
		}
		if role := strings.TrimSpace(officialOpenAIString(item, "role")); role == "user" {
			if text := officialOpenAIHTTPMessageContentText(item["content"]); text != "" {
				lastUserAnchor = strconv.Itoa(itemIndex) + ":" + text
			}
		}
		itemMetadata, exists := item[officialOpenAIWSItemTurnMetadata]
		if !exists {
			continue
		}
		itemMetadataMap, valid := itemMetadata.(map[string]any)
		if !valid {
			return "", fmt.Errorf(
				"OpenAI official egress WebSocket input item %d turn metadata must be object",
				itemIndex,
			)
		}
		itemTurnID := strings.TrimSpace(officialOpenAIString(itemMetadataMap, "turn_id"))
		if itemTurnID == "" {
			continue
		}
		if _, err := uuid.Parse(itemTurnID); err != nil {
			return "", fmt.Errorf(
				"OpenAI official egress WebSocket input item %d turn_id must be UUID",
				itemIndex,
			)
		}
		if existingTurnID != "" && existingTurnID != itemTurnID {
			return "", fmt.Errorf(
				"OpenAI official egress WebSocket historical turn %d has conflicting turn_id",
				segmentIndex,
			)
		}
		existingTurnID = itemTurnID
	}
	if existingTurnID != "" {
		return existingTurnID, nil
	}
	if lastUserAnchor == "" {
		segmentPayload := make([]any, 0, len(segment))
		for _, itemIndex := range segment {
			segmentPayload = append(segmentPayload, input[itemIndex])
		}
		segmentBytes, err := marshalOpenAIUpstreamJSON(segmentPayload)
		if err != nil {
			return "", fmt.Errorf(
				"encode OpenAI official egress WebSocket historical turn %d: %w",
				segmentIndex,
				err,
			)
		}
		lastUserAnchor = fmt.Sprintf("%x", segmentBytes)
	}
	return generateOfficialStableUUIDV7(
		"openai-official-egress-turn|" + sessionID + "|" + lastUserAnchor,
	), nil
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
	prewarm := !generate && payload["generate"] != nil
	turnID := ""
	turnStartedAtMS := int64(0)
	requestKind := "prewarm"
	if !prewarm {
		requestKind = "turn"
		_, lastUserAnchor := officialOpenAIHTTPUserAnchorsFromPayload(
			egressContext.ProfileMode(), payload,
		)
		state := egressContext.openAIWSDerived
		state.mu.Lock()
		var seedErr error
		if !preserveCurrentTurn && lastUserAnchor != "" {
			state.lastTurnID = generateOfficialStableUUIDV7(
				"openai-official-egress-turn|" + sessionID + "|" + lastUserAnchor,
			)
			state.lastTurnStartedAtMS = time.Now().UnixMilli()
		} else if state.lastTurnID == "" {
			// 没有用户锚点时以整帧内容做 seed。marshal 失败不能静默退化成空
			// seed：那会让本会话此后所有帧共用同一个 Turn ID，而定型链路其余
			// 环节都观察不到异常。这里把错误交回调用方按定型失败处理。
			frameBytes, marshalErr := marshalOfficialOpenAIWSJSON(
				egressContext.ProfileMode(), payload,
			)
			if marshalErr != nil {
				seedErr = fmt.Errorf("构造 WebSocket Turn ID seed：%w", marshalErr)
			} else {
				state.lastTurnID = generateOfficialStableUUIDV7(
					"openai-official-egress-turn|" + sessionID + "|" +
						fmt.Sprintf("%x", frameBytes),
				)
				state.lastTurnStartedAtMS = time.Now().UnixMilli()
			}
		}
		turnID = state.lastTurnID
		turnStartedAtMS = state.lastTurnStartedAtMS
		state.mu.Unlock()
		if seedErr != nil {
			return nil, "", seedErr
		}
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
	turnMetadataBytes, err := marshalOfficialOpenAITurnMetadata(turnMetadata)
	if err != nil {
		return nil, "", fmt.Errorf(
			"encode derived OpenAI official egress WebSocket turn metadata: %w",
			err,
		)
	}
	metadata := map[string]any{
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
	}
	if egressContext.responsesLite {
		metadata[responsesLiteWSMetadataKey] = "true"
	}
	return metadata, sessionID, nil
}

func officialOpenAIHTTPUserAnchorsFromPayload(
	mode string,
	payload map[string]any,
) (string, string) {
	body, err := marshalOfficialOpenAIWSJSON(mode, payload)
	if err != nil {
		return "", ""
	}
	return officialOpenAIHTTPUserAnchors(body)
}
