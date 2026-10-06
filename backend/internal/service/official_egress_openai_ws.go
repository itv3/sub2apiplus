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

var errOpenAIOfficialEgressWSToolOutputTurnAmbiguous = errors.New(
	"tool output turn cannot be determined reliably",
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
	// pendingToolCallIDs 来自上一轮上游 response.output 中实际产生的
	// function_call/tool_call。客户端断线后重发完整历史时，逐项 turn_id
	// 可能已经丢失；这些会话级 call_id 是仍然可信的消歧锚点。
	pendingToolCallIDs map[string]struct{}
	// fullHistoryFallback 只由 WS 入站在每轮判定时写入：工具输出的轮次归属无法可靠判定，
	// 但帧内没有 previous_response_id，且每个工具输出都能在同一帧里找到对应的工具调用，
	// 即断线后客户端重发的完整历史。此时本轮不做工具续接收敛，把全部工具输出视为历史，
	// 按携带完整历史的普通轮次在新连接上开新链。其他入口从不写这个标记，保持失败关闭。
	fullHistoryFallback bool
}

// setFullHistoryFallback 记录本轮是否按“断线后重发的完整历史”开新链。
func (s *officialOpenAIWSDerivedState) setFullHistoryFallback(active bool) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.fullHistoryFallback = active
	s.mu.Unlock()
}

func (s *officialOpenAIWSDerivedState) fullHistoryFallbackActive() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.fullHistoryFallback
}

// applyOfficialOpenAIWSFullHistoryFallback 对入站已确认的“断线后重发完整历史”轮次改判：会话打了
// 兜底标记且帧内没有 previous_response_id 时，全部工具输出按历史处理，本轮按普通轮次对待。
// 帧整理会给每一项补上本轮的逐项轮次标记，之后再判定会变成“可靠且全是本轮输出”，所以这里不看
// 原判定是否可靠，统一改判：预热照常发出，续接构帧也不会按旧连接上的上一轮响应 ID 收敛。
// 未打标记或帧带续链锚点时原样返回，保持原有的失败关闭语义。
func applyOfficialOpenAIWSFullHistoryFallback(
	payload map[string]any,
	state *officialOpenAIWSDerivedState,
	hasCurrent bool,
	reliable bool,
) (bool, bool) {
	if !state.fullHistoryFallbackActive() ||
		strings.TrimSpace(officialOpenAIString(payload, "previous_response_id")) != "" {
		return hasCurrent, reliable
	}
	return false, true
}

func (s *officialOpenAIWSDerivedState) setPendingToolCallIDs(items []json.RawMessage) {
	if s == nil {
		return
	}
	pending := make(map[string]struct{})
	for _, item := range items {
		itemType := strings.TrimSpace(gjson.GetBytes(item, "type").String())
		if !isCodexToolCallContextItemType(itemType) {
			continue
		}
		callID := strings.TrimSpace(gjson.GetBytes(item, "call_id").String())
		if callID != "" {
			pending[callID] = struct{}{}
		}
	}
	s.mu.Lock()
	s.pendingToolCallIDs = pending
	s.mu.Unlock()
}

func (s *officialOpenAIWSDerivedState) pendingToolCallIDSet() map[string]struct{} {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.pendingToolCallIDs) == 0 {
		return nil
	}
	cloned := make(map[string]struct{}, len(s.pendingToolCallIDs))
	for callID := range s.pendingToolCallIDs {
		cloned[callID] = struct{}{}
	}
	return cloned
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
	// 身份契约和握手小字段读取同一首帧，复用一次严格扫描的索引。
	index, _ := buildOfficialJSONRawIndexForDecode(firstPayload)
	identity, err := deriveOfficialOpenAIWSIdentityWithIndex(
		c,
		account,
		firstPayload,
		egressContext.ProfileMode(),
		index,
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
	if section := officialCodexOptionalSectionsForMode(egressContext.ProfileMode()).TurnMetadata; section != nil {
		// 握手 turn metadata 是 prewarm 形态：不属于某一轮，不写 turn_trigger；
		// model 与 reasoning_effort 取首帧请求体（缺省 effort 按模型默认值补齐）。
		firstFrame, decodeErr := decodeOfficialOpenAIWSTurnMetadataFieldsWithIndex(firstPayload, index)
		if decodeErr != nil {
			return fmt.Errorf("decode OpenAI official egress WebSocket first frame: %w", decodeErr)
		}
		identity.turnMetadata, err = extendOfficialOpenAITurnMetadataJSON(
			identity.turnMetadata, section,
			officialCodexTurnMetadataExtension{
				Model: officialOpenAIString(firstFrame, "model"),
				ReasoningEffort: officialOpenAIEffectiveReasoningEffort(
					firstFrame, officialOpenAIReasoningDefaultsFromContext(egressContext),
				),
			},
		)
		if err != nil {
			return err
		}
	}
	if identity.source == OfficialEgressFieldSourceDerived {
		egressContext.openAIWSDerived = &officialOpenAIWSDerivedState{}
	}
	return registerOfficialOpenAIWSIdentity(egressContext, identity)
}

// 握手只需要模型和推理档位。仍严格扫描整帧、按同名键末值解析，但不为
// input 历史建立对象树或复制长字符串；数值保持 json.Number 的原十进制文本。
func decodeOfficialOpenAIWSTurnMetadataFields(body []byte) (map[string]any, error) {
	return decodeOfficialOpenAIWSTurnMetadataFieldsWithIndex(body, nil)
}

func decodeOfficialOpenAIWSTurnMetadataFieldsWithIndex(body []byte, index *officialJSONRawIndex) (map[string]any, error) {
	var err error
	if index == nil {
		index, err = buildOfficialJSONRawIndexForDecode(body)
	}
	if err != nil || index.nodes[index.root].kind != officialJSONRawKindObject {
		// 保留旧解码器对非法、null 和非对象首帧的原有错误与返回值。
		return decodeOfficialJSONObjectUseNumberSlow(body)
	}
	payload := make(map[string]any, 2)
	if node := index.memberNode(index.root, "model"); node >= 0 && index.nodes[node].kind == officialJSONRawKindString {
		payload["model"] = index.decodeString(node)
	}
	if node := index.memberNode(index.root, "reasoning"); node >= 0 && index.nodes[node].kind == officialJSONRawKindObject {
		if effort := index.memberNode(node, "effort"); effort >= 0 {
			switch index.nodes[effort].kind {
			case officialJSONRawKindString, officialJSONRawKindNumber:
				payload["reasoning"] = map[string]any{"effort": index.decodeValue(effort)}
			}
		}
	}
	return payload, nil
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

	forkAllowed := officialOpenAIExplicitForkAllowed(c)
	if !officialOpenAIIngressSessionHeaderMatches(
		strings.TrimSpace(c.GetHeader("session-id")), identity.sessionID, identity.promptCacheKey, forkAllowed,
	) ||
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
			sessionID:                 identity.sessionID,
			threadID:                  identity.threadID,
			promptCacheKey:            identity.promptCacheKey,
			forkPromptCacheKeyAllowed: forkAllowed,
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
	return deriveOfficialOpenAIWSIdentityWithIndex(c, account, firstPayload, profileMode, nil)
}

func deriveOfficialOpenAIWSIdentityWithIndex(
	c *gin.Context,
	account *Account,
	firstPayload []byte,
	profileMode string,
	index *officialJSONRawIndex,
) (officialOpenAIWSIdentity, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContractWithIndex(firstPayload, index)
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
			officialWSFrameBodyFromContext(ctx),
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
	body *officialWSFrameBody,
) ([]byte, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	payload, index, err := decodeAndValidateUnifiedOpenAIWSBusinessContract(
		original,
		candidate,
		expectedPreviousResponseID,
		allowControlledReplay,
		body,
	)
	if err != nil {
		return nil, result, err
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
	// 不要仅凭完整历史中是否出现过工具输出就进入“工具续接”语义。
	// 历史轮次可能包含 function_call_output，而当前轮只是普通用户消息；
	// 这种请求必须生成新的普通轮次身份，否则后续会被误裁剪成空工具续接。
	_, hasCurrentToolOutput, toolOutputTurnReliable, toolOutputTurnErr :=
		classifyOfficialOpenAIWSToolOutputTurnWithDerivedState(
			payload,
			egressContext.openAIWSDerived,
		)
	if toolOutputTurnErr != nil {
		return nil, result, toolOutputTurnErr
	}
	hasCurrentToolOutput, toolOutputTurnReliable = applyOfficialOpenAIWSFullHistoryFallback(
		payload,
		egressContext.openAIWSDerived,
		hasCurrentToolOutput,
		toolOutputTurnReliable,
	)
	if !toolOutputTurnReliable {
		return nil, result, fmt.Errorf(
			"OpenAI official egress WebSocket %w",
			errOpenAIOfficialEgressWSToolOutputTurnAmbiguous,
		)
	}
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
		hasCurrentToolOutput,
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

	finalized, err := marshalOfficialWSFrameBody(
		egressContext.ProfileMode(), payload, candidate, index,
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
	_, _, err := decodeAndValidateUnifiedOpenAIWSBusinessContract(
		original, candidate, expectedPreviousResponseID, allowControlledReplay, nil,
	)
	return err
}

// decodeAndValidateUnifiedOpenAIWSBusinessContract 将校验后的候选树直接交给定型器，
// 避免校验完丢弃再整帧解码。比较只读取树，绝不改写业务输入和借用的源正文。
func decodeAndValidateUnifiedOpenAIWSBusinessContract(
	original, candidate []byte,
	expectedPreviousResponseID string,
	allowControlledReplay bool,
	body *officialWSFrameBody,
) (map[string]any, *officialJSONRawIndex, error) {
	originalPayload, originalIndex, err := body.decode(original)
	if err != nil {
		return nil, nil, fmt.Errorf("decode unified OpenAI official egress ingress WebSocket frame: %w", err)
	}
	if strings.TrimSpace(officialOpenAIString(originalPayload, "type")) != officialOpenAIWSResponseCreateType {
		if !bytes.Equal(original, candidate) {
			return nil, nil, errors.New("OpenAI official egress unknown WebSocket frame was modified")
		}
		return originalPayload, originalIndex, nil
	}
	candidatePayload, candidateIndex := originalPayload, originalIndex
	if !officialForwardSameBody(original, candidate) {
		candidatePayload, candidateIndex, err = body.decode(candidate)
	}
	if err != nil {
		return nil, nil, fmt.Errorf("decode unified OpenAI official egress outbound WebSocket frame: %w", err)
	}
	if strings.TrimSpace(officialOpenAIString(candidatePayload, "type")) != officialOpenAIWSResponseCreateType {
		return nil, nil, errors.New("OpenAI official egress WebSocket frame type was modified")
	}
	if allowControlledReplay {
		return candidatePayload, candidateIndex, nil
	}
	originalPrevious, originalPreviousPresent := originalPayload["previous_response_id"]
	candidatePrevious, candidatePreviousPresent := candidatePayload["previous_response_id"]
	if originalPreviousPresent != candidatePreviousPresent || !reflect.DeepEqual(originalPrevious, candidatePrevious) {
		return nil, nil, errors.New("OpenAI official egress WebSocket previous_response_id was modified")
	}
	if originalPreviousPresent {
		originalValue, _ := originalPrevious.(string)
		if strings.TrimSpace(expectedPreviousResponseID) != "" &&
			strings.TrimSpace(originalValue) != strings.TrimSpace(expectedPreviousResponseID) {
			return nil, nil, errors.New("OpenAI official egress WebSocket previous_response_id conflicts with prior response")
		}
	}
	equal, err := equalOfficialOpenAIWSBusinessHistory(originalPayload, candidatePayload)
	if err != nil {
		return nil, nil, err
	}
	if !equal {
		return nil, nil, errors.New("OpenAI official egress WebSocket input was modified")
	}
	return candidatePayload, candidateIndex, nil
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
	body, index, err := decodeOfficialWSFrameBody(ctx, payload)
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
	encoded, err := marshalOfficialWSFrameBody(
		egressContext.ProfileMode(), body, payload, index,
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
	payload, index, err := decodeOfficialWSFrameBody(ctx, candidate)
	if err != nil {
		return nil, false, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket prewarm source: %w",
			err,
		)
	}
	hasAnyToolOutput, hasCurrentToolOutput, reliable, classifyErr :=
		classifyOfficialOpenAIWSToolOutputTurnWithDerivedState(
			payload,
			egressContext.openAIWSDerived,
		)
	if classifyErr != nil {
		return nil, false, classifyErr
	}
	hasCurrentToolOutput, reliable = applyOfficialOpenAIWSFullHistoryFallback(
		payload,
		egressContext.openAIWSDerived,
		hasCurrentToolOutput,
		reliable,
	)
	if !reliable {
		return nil, false, fmt.Errorf(
			"OpenAI official egress WebSocket %w",
			errOpenAIOfficialEgressWSToolOutputTurnAmbiguous,
		)
	}
	if hasAnyToolOutput && hasCurrentToolOutput {
		return nil, false, nil
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

	prewarm, err := marshalOfficialWSFrameBody(
		egressContext.ProfileMode(), payload, candidate, index,
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

	payload, index, err := decodeOfficialWSFrameBody(ctx, candidate)
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

	finalized, err := marshalOfficialWSFrameBody(
		egressContext.ProfileMode(), payload, candidate, index,
	)
	if err != nil {
		return nil, fmt.Errorf(
			"encode derived OpenAI official egress WebSocket business frame: %w",
			err,
		)
	}
	return finalized, nil
}

// classifyOfficialOpenAIWSToolOutputTurn 根据当前帧的轮次元数据和 input
// 分段判断工具输出是否属于当前轮。完整历史中存在旧轮 function_call_output
// 并不代表当前请求就是工具续接；只有当前轮确实有工具输出时才允许最小化裁剪。
// 返回值依次为：是否存在任意工具输出、是否存在当前轮工具输出、轮次判断是否可靠。
func classifyOfficialOpenAIWSToolOutputTurn(payload map[string]any) (bool, bool, bool, error) {
	if payload == nil {
		return false, false, true, nil
	}
	input, ok := payload["input"].([]any)
	if !ok {
		// finalizeOfficialOpenAIWSInputTurnMetadata 会对缺少 input 的帧给出
		// 更具体的协议错误；这里不把普通缺失字段误判成工具续接不可靠。
		return false, false, true, nil
	}

	metadata, _ := payload["client_metadata"].(map[string]any)
	currentTurnID := strings.TrimSpace(officialOpenAIString(metadata, "turn_id"))
	segments := splitOfficialOpenAIWSInputTurnSegments(input)
	currentSegment := make(map[int]struct{})
	currentSegmentStart, currentSegmentEnd := 0, -1
	if len(segments) > 0 {
		lastSegment := segments[len(segments)-1]
		if len(lastSegment) > 0 {
			currentSegmentStart = lastSegment[0]
			currentSegmentEnd = lastSegment[len(lastSegment)-1]
		}
		for _, index := range lastSegment {
			currentSegment[index] = struct{}{}
		}
	}
	// 预先计算“输出后是否还有普通项”和“输出前是否有未标注工具调用”，
	// 避免对每个 output 重扫整个当前段；长上下文只做 O(n) 分类。
	nonToolAfter := make([]bool, len(input))
	seenNonTool := false
	for index := len(input) - 1; index >= 0; index-- {
		nonToolAfter[index] = seenNonTool
		item, valid := input[index].(map[string]any)
		if !valid || !isCodexToolCallOutputItemType(
			strings.TrimSpace(officialOpenAIString(item, "type")),
		) {
			seenNonTool = true
		}
	}
	unmarkedToolCallBefore := make([]bool, len(input))
	seenUnmarkedToolCall := false
	for index := currentSegmentStart; index <= currentSegmentEnd; index++ {
		rawItem := input[index]
		unmarkedToolCallBefore[index] = seenUnmarkedToolCall
		item, valid := rawItem.(map[string]any)
		if !valid || !isCodexToolCallContextItemType(
			strings.TrimSpace(officialOpenAIString(item, "type")),
		) {
			continue
		}
		itemMetadata, hasMetadata := item[officialOpenAIWSItemTurnMetadata].(map[string]any)
		if !hasMetadata || strings.TrimSpace(officialOpenAIString(itemMetadata, "turn_id")) == "" {
			seenUnmarkedToolCall = true
		}
	}
	hasCurrentSegment := len(currentSegment) > 0

	hasAny := false
	hasCurrent := false
	reliable := true
	for index, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid || !isCodexToolCallOutputItemType(
			strings.TrimSpace(officialOpenAIString(item, "type")),
		) {
			continue
		}
		hasAny = true

		itemTurnID := ""
		if rawItemMetadata, exists := item[officialOpenAIWSItemTurnMetadata]; exists {
			itemMetadata, validMetadata := rawItemMetadata.(map[string]any)
			if !validMetadata {
				return hasAny, hasCurrent, false, errors.New(
					"OpenAI official egress WebSocket tool output turn metadata must be object",
				)
			}
			itemTurnID = strings.TrimSpace(officialOpenAIString(itemMetadata, "turn_id"))
			if itemTurnID != "" {
				if _, err := uuid.Parse(itemTurnID); err != nil {
					return hasAny, hasCurrent, false, fmt.Errorf(
						"OpenAI official egress WebSocket tool output turn_id must be UUID: %w",
						err,
					)
				}
			}
		}

		isCurrent := false
		_, inCurrentSegment := currentSegment[index]
		switch {
		case currentTurnID != "" && itemTurnID != "":
			isCurrent = itemTurnID == currentTurnID
		case inCurrentSegment:
			// 在帧尚未补齐逐项 metadata 时，位于当前输入段末尾的工具
			// 输出可视为当前轮；若输出后又出现普通输入，则更像历史轮次。
			// 但同一段此前还有未标注轮次的工具调用时，无法安全区分，
			// 必须 fail-close，不能把整段历史误裁剪成工具续接。
			if nonToolAfter[index] {
				if unmarkedToolCallBefore[index] {
					reliable = false
				}
				isCurrent = false
			} else {
				isCurrent = true
			}
		case hasCurrentSegment:
			// 已能识别当前分段，位于其前的工具输出属于历史轮次。
			isCurrent = false
		default:
			// 没有 metadata 且不在可识别的当前分段中，无法安全猜测归属。
			reliable = false
		}
		if isCurrent {
			hasCurrent = true
		}
	}

	return hasAny, hasCurrent, reliable, nil
}

// classifyOfficialOpenAIWSToolOutputTurnWithDerivedState 先执行逐项 metadata
// 的严格判断；只有判断不可靠时，才使用同一入站 WS 会话上一轮真实产生的
// call_id 消歧。若工具输出混合了“上一轮已完成”和“当前待回传”两组
// call_id，仍然 fail-close，禁止按数组位置猜测。
func classifyOfficialOpenAIWSToolOutputTurnWithDerivedState(
	payload map[string]any,
	state *officialOpenAIWSDerivedState,
) (bool, bool, bool, error) {
	hasAny, hasCurrent, reliable, err := classifyOfficialOpenAIWSToolOutputTurn(payload)
	if err != nil || reliable || !hasAny || state == nil {
		return hasAny, hasCurrent, reliable, err
	}
	pending := state.pendingToolCallIDSet()
	if len(pending) == 0 {
		return hasAny, hasCurrent, reliable, nil
	}
	input, ok := payload["input"].([]any)
	if !ok {
		return hasAny, hasCurrent, reliable, nil
	}
	totalOutputs := 0
	currentOutputs := 0
	for _, rawItem := range input {
		item, valid := rawItem.(map[string]any)
		if !valid || !isCodexToolCallOutputItemType(
			strings.TrimSpace(officialOpenAIString(item, "type")),
		) {
			continue
		}
		callID := strings.TrimSpace(officialOpenAIString(item, "call_id"))
		if callID == "" {
			return hasAny, hasCurrent, false, nil
		}
		totalOutputs++
		if _, ok := pending[callID]; ok {
			currentOutputs++
		}
	}
	switch {
	case totalOutputs == 0:
		return hasAny, false, true, nil
	case currentOutputs == 0:
		return hasAny, false, true, nil
	case currentOutputs == totalOutputs:
		return hasAny, true, true, nil
	default:
		return hasAny, true, false, nil
	}
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
	if !isDerivedOpenAIOfficialEgressWSContext(ctx) {
		return candidate, false, nil
	}
	egressContext, enabled := OfficialEgressContextFromContext(ctx)
	if !enabled || egressContext == nil {
		return candidate, false, nil
	}
	payload, index, err := decodeOfficialWSFrameBody(ctx, candidate)
	if err != nil {
		return nil, false, fmt.Errorf(
			"decode derived OpenAI official egress WebSocket tool continuation: %w",
			err,
		)
	}
	hasAnyToolOutput, hasCurrentToolOutput, reliable, classifyErr :=
		classifyOfficialOpenAIWSToolOutputTurnWithDerivedState(
			payload,
			egressContext.openAIWSDerived,
		)
	if classifyErr != nil {
		return nil, false, classifyErr
	}
	hasCurrentToolOutput, reliable = applyOfficialOpenAIWSFullHistoryFallback(
		payload,
		egressContext.openAIWSDerived,
		hasCurrentToolOutput,
		reliable,
	)
	if !reliable {
		return nil, false, fmt.Errorf(
			"OpenAI official egress WebSocket %w",
			errOpenAIOfficialEgressWSToolOutputTurnAmbiguous,
		)
	}
	if !hasAnyToolOutput || !hasCurrentToolOutput {
		// 历史轮次的工具输出不应触发当前轮工具续接；保留原帧，
		// 让普通用户轮次按完整语义继续处理。
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

	previousMetadata, _ := payload["client_metadata"].(map[string]any)
	currentTurnID := strings.TrimSpace(officialOpenAIString(previousMetadata, "turn_id"))
	if currentTurnID == "" {
		return nil, false, errors.New(
			"OpenAI official egress WebSocket tool continuation current turn_id is empty",
		)
	}
	input, _ := payload["input"].([]any)
	pendingToolCallIDs := egressContext.openAIWSDerived.pendingToolCallIDSet()
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
		itemTurnID := ""
		if rawItemMetadata, exists := item[officialOpenAIWSItemTurnMetadata]; exists {
			itemMetadata, valid := rawItemMetadata.(map[string]any)
			if !valid {
				return nil, false, errors.New(
					"OpenAI official egress WebSocket tool output turn metadata must be object",
				)
			}
			itemTurnID = strings.TrimSpace(officialOpenAIString(itemMetadata, "turn_id"))
		}
		// 完整历史可能包含以前轮次的工具输出；previous_response_id 已经让
		// 上游持有这些历史项，本次只发送当前轮新增的工具结果。
		if itemTurnID != "" && itemTurnID != currentTurnID {
			continue
		}
		if itemTurnID == "" && len(pendingToolCallIDs) > 0 {
			if _, ok := pendingToolCallIDs[strings.TrimSpace(officialOpenAIString(item, "call_id"))]; !ok {
				continue
			}
		}
		toolOutputs = append(toolOutputs, item)
	}
	if len(toolOutputs) == 0 {
		return nil, false, errors.New(
			"OpenAI official egress WebSocket tool continuation has no current-turn tool output",
		)
	}

	payload["input"] = toolOutputs
	payload["previous_response_id"] = previousResponseID
	delete(payload, "generate")
	metadata, promptCacheKey, err := buildDerivedOfficialOpenAIWSFrameMetadataWithTurnPolicy(
		egressContext,
		payload,
		true,
	)
	if err != nil {
		return nil, false, err
	}
	// turn-state 属于当前连接，重建逐帧 metadata 时必须原样带回。
	if turnState, ok := previousMetadata[openAIWSTurnStateHeader].(string); ok &&
		strings.TrimSpace(turnState) != "" {
		metadata[openAIWSTurnStateHeader] = turnState
	}
	payload["client_metadata"] = metadata
	payload["prompt_cache_key"] = promptCacheKey
	if _, err := finalizeOfficialOpenAIWSInputTurnMetadata(payload); err != nil {
		return nil, false, err
	}

	finalized, err := marshalOfficialWSFrameBody(
		egressContext.ProfileMode(), payload, candidate, index,
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
// 新轮次边界。Responses 工具调用项由助手产生，但通常不携带 role=assistant，
// 因此必须把工具调用本身也视为助手已输出；否则“历史工具调用/输出 + 新用户消息”
// 会被错误合并为同一轮，并触发工具续接的歧义保护。
// 连续的用户上下文项属于同一轮，工具输出也继续归入当前轮次。
func splitOfficialOpenAIWSInputTurnSegments(input []any) [][]int {
	if len(input) == 0 {
		return nil
	}
	segments := make([][]int, 1)
	completedAssistantTurn := false
	for index, rawItem := range input {
		item, _ := rawItem.(map[string]any)
		role := strings.TrimSpace(officialOpenAIString(item, "role"))
		itemType := strings.TrimSpace(officialOpenAIString(item, "type"))
		if role == "user" && completedAssistantTurn && len(segments[len(segments)-1]) > 0 {
			segments = append(segments, nil)
			completedAssistantTurn = false
		}
		segments[len(segments)-1] = append(segments[len(segments)-1], index)
		if role == "assistant" || isCodexToolCallContextItemType(itemType) {
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
	var lastUserAnchor officialOpenAIUserAnchor
	lastUserFound := false
	for _, itemIndex := range segment {
		item, valid := input[itemIndex].(map[string]any)
		if !valid {
			return "", fmt.Errorf(
				"OpenAI official egress WebSocket input item %d must be object",
				itemIndex,
			)
		}
		if role := strings.TrimSpace(officialOpenAIString(item, "role")); role == "user" {
			// 只保留用户文本的摘要与下标，用户全文不再拼入种子。
			if digest, found := digestOfficialOpenAIMessageContent(item["content"]); found {
				lastUserAnchor = officialOpenAIUserAnchor{index: itemIndex, digest: digest}
				lastUserFound = true
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
	seed := newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).WriteString(sessionID)
	if lastUserFound {
		seed.WriteUserAnchor(lastUserAnchor, true)
	} else {
		// 没有用户文本的历史片段以其确定性 JSON 编码的摘要作兜底锚点。编码结果
		// 直接流入 SHA-256，不再生成两倍体积的十六进制字符串驻留在缓存键中。
		segmentPayload := make([]any, 0, len(segment))
		for _, itemIndex := range segment {
			segmentPayload = append(segmentPayload, input[itemIndex])
		}
		digest, err := digestOfficialJSONValue(segmentPayload)
		if err != nil {
			return "", fmt.Errorf(
				"encode OpenAI official egress WebSocket historical turn %d: %w",
				segmentIndex,
				err,
			)
		}
		seed.WriteString("segment_json").WriteDigest(digest)
	}
	return generateOfficialStableUUIDV7(seed.Key()), nil
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
		// 已有结构化 payload，直接遍历 input 提取锚点摘要；不得再先编码整帧、
		// 随后又解码来提取用户锚点。
		anchors := officialOpenAIUserAnchorsFromInput(payload["input"])
		state := egressContext.openAIWSDerived
		state.mu.Lock()
		var seedErr error
		if !preserveCurrentTurn && anchors.lastFound {
			state.lastTurnID = generateOfficialStableUUIDV7(
				newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
					WriteString(sessionID).
					WriteUserAnchor(anchors.last, true).
					Key(),
			)
			state.lastTurnStartedAtMS = time.Now().UnixMilli()
		} else if state.lastTurnID == "" {
			// 没有用户锚点时以整帧内容的 JSON 摘要做 seed。编码失败不能静默退化
			// 成空 seed：那会让本会话此后所有帧共用同一个 Turn ID，而定型链路
			// 其余环节都观察不到异常。这里把错误交回调用方按定型失败处理。
			digest, digestErr := digestOfficialJSONValue(payload)
			if digestErr != nil {
				seedErr = fmt.Errorf("构造 WebSocket Turn ID seed：%w", digestErr)
			} else {
				state.lastTurnID = generateOfficialStableUUIDV7(
					newOfficialUUIDV7Seed(officialUUIDV7DomainTurn).
						WriteString(sessionID).
						WriteString("frame_json").
						WriteDigest(digest).
						Key(),
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
	// 画像 TurnMetadata 节声明的新键：payload 已完成定型（reasoning 默认值已补齐）；
	// prewarm 帧不属于某一轮，不写 turn_trigger。画像没有该节时不做任何改动。
	extension := officialCodexTurnMetadataExtension{
		Model:           officialOpenAIString(payload, "model"),
		ReasoningEffort: officialOpenAIEffectiveReasoningEffort(payload, officialOpenAIReasoningDefaultsFromContext(egressContext)),
	}
	if !prewarm {
		conditional := egressContext.codexRuntimeState.ConditionalHeaders
		extension.TurnTrigger = officialCodexTurnTrigger(
			egressContext.codexRuntimeState.SurfaceID,
			conditional["x-openai-subagent"],
			strings.EqualFold(conditional["x-openai-memgen-request"], "true"),
		)
	}
	if err := applyOfficialCodexTurnMetadataSection(
		turnMetadata,
		officialCodexOptionalSectionsForMode(egressContext.ProfileMode()).TurnMetadata,
		extension,
	); err != nil {
		return nil, "", err
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
