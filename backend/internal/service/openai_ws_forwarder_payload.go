package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"unsafe"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func validateOpenAIWSBearerToken(account *Account, token string) error {
	if account == nil {
		return errors.New("account is nil")
	}
	if strings.TrimSpace(token) == "" && !account.IsOpenAIAgentIdentity() {
		return errors.New("token is empty")
	}
	return nil
}

func (s *OpenAIGatewayService) buildOpenAIResponsesWSURL(account *Account) (string, error) {
	if account == nil {
		return "", errors.New("account is nil")
	}
	var targetURL string
	switch account.Type {
	case AccountTypeOAuth:
		targetURL = chatgptCodexURL
	case AccountTypeSetupToken:
		if account.IsOpenAIOAuthLike() {
			targetURL = chatgptCodexURL
		} else {
			targetURL = openaiPlatformAPIURL
		}
	case AccountTypeAPIKey:
		baseURL := account.GetOpenAIBaseURL()
		if account.UsesNativeCNResponses() && account.IsAdaptiveAPIProtocol() {
			baseURL = account.GetCNProtocolBaseURL(APIProtocolResponses)
		}
		if baseURL == "" {
			targetURL = openaiPlatformAPIURL
		} else {
			validatedURL, err := s.validateUpstreamBaseURL(baseURL)
			if err != nil {
				return "", err
			}
			targetURL = buildOpenAIResponsesURLForPlatform(account.Platform, validatedURL)
		}
	default:
		targetURL = openaiPlatformAPIURL
	}

	parsed, err := url.Parse(strings.TrimSpace(targetURL))
	if err != nil {
		return "", fmt.Errorf("invalid target url: %w", err)
	}
	switch strings.ToLower(parsed.Scheme) {
	case "https":
		parsed.Scheme = "wss"
	case "http":
		parsed.Scheme = "ws"
	case "wss", "ws":
		// 保持不变
	default:
		return "", fmt.Errorf("unsupported scheme for ws: %s", parsed.Scheme)
	}
	return parsed.String(), nil
}

func (s *OpenAIGatewayService) buildOpenAIWSHeaders(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	token string,
	decision OpenAIWSProtocolDecision,
	isCodexCLI bool,
	_ string,
	turnMetadata string,
	promptCacheKey string,
	routingModel string,
	routingServiceTier string,
) (http.Header, openAIWSSessionHeaderResolution, error) {
	headers := make(http.Header)
	if account == nil || !account.IsOpenAIAgentIdentity() {
		headers.Set("authorization", "Bearer "+token)
	}

	sessionResolution := resolveOpenAIWSSessionHeaders(c, promptCacheKey)
	if c != nil && c.Request != nil {
		if v := strings.TrimSpace(c.Request.Header.Get("accept-language")); v != "" {
			headers.Set("accept-language", v)
		}
		for _, value := range c.Request.Header.Values("x-codex-beta-features") {
			if value = strings.TrimSpace(value); value != "" {
				headers.Add("x-codex-beta-features", value)
			}
		}
		for _, name := range [...]string{
			"x-codex-window-id",
			"x-codex-installation-id",
			"session-id",
			"thread-id",
			"x-client-request-id",
		} {
			if value := c.Request.Header.Get(name); strings.TrimSpace(value) != "" {
				headers.Set(name, value)
			}
		}
	}
	// 真实 Codex 的 WS 握手同样携带会话级 x-codex-beta-features
	// （client.rs build_websocket_headers 复用 build_responses_headers），
	// 客户端未声明时补成默认形态，与 HTTP 出站保持一致。放在客户端头拷贝
	// 之外：该头是账号/会话级属性，不依赖入站请求是否存在，也避免预热与
	// 实际请求因头差异落进不同的连接池兼容分桶。
	applyOpenAICodexBetaFeatures(c, account, headers)
	// OAuth 账号：将 apiKeyID 混入 session 标识符，防止跨用户会话碰撞。
	if account != nil && account.UsesOpenAICodexProtocol() {
		apiKeyID := getAPIKeyIDFromContext(c)
		if sessionResolution.SessionID != "" {
			headers.Set("session_id", isolateOpenAIUpstreamSessionID(apiKeyID, codexAccountIdentitySource(c, account), sessionResolution.SessionID))
		}
		if sessionResolution.ConversationID != "" {
			headers.Set("conversation_id", isolateOpenAIUpstreamSessionID(apiKeyID, codexAccountIdentitySource(c, account), sessionResolution.ConversationID))
		}
	} else {
		if sessionResolution.SessionID != "" {
			headers.Set("session_id", sessionResolution.SessionID)
		}
		if sessionResolution.ConversationID != "" {
			headers.Set("conversation_id", sessionResolution.ConversationID)
		}
	}
	// x-codex-turn-state 不能作为 WS 握手请求头上行；画像只会把上游握手
	// 响应值写进同一连接、同一轮 response.create 的 client_metadata。
	headers.Del(openAIWSTurnStateHeader)
	if metadata := strings.TrimSpace(turnMetadata); metadata != "" {
		headers.Set(openAIWSTurnMetadataHeader, metadata)
	}
	applyCodexAccountIdentityHeaders(headers, codexAccountIdentitySource(c, account), getAPIKeyIDFromContext(c))
	applyStagedCodexFingerprintHeaders(c, account, headers)

	if account != nil && account.UsesOpenAICodexProtocol() {
		if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, headers, account); err != nil {
			return nil, sessionResolution, fmt.Errorf("resolve chatgpt account headers: %w", err)
		}
		headers.Set("originator", resolveOpenAIUpstreamOriginator(c, isCodexCLI))
	}

	betaValue := openAIWSBetaV2Value
	if decision.Transport == OpenAIUpstreamTransportResponsesWebsocket {
		betaValue = openAIWSBetaV1Value
	}
	headers.Set("OpenAI-Beta", betaValue)

	customUA := ""
	if account != nil {
		customUA = account.GetOpenAIUserAgent()
	}
	if strings.TrimSpace(customUA) != "" {
		headers.Set("user-agent", customUA)
	} else if c != nil {
		if ua := strings.TrimSpace(c.GetHeader("User-Agent")); ua != "" {
			headers.Set("user-agent", ua)
		}
	}
	if s != nil && s.cfg != nil && s.cfg.Gateway.ForceCodexCLI {
		headers.Set("user-agent", CodexCanonicalUserAgent())
	}
	// 此处只组装业务语义和候选 UA，不执行身份终态收口。WS Executor 会在握手发送前
	// 按 active ReleaseBundle 一次性重建身份；提前补 version 会破坏单一 Finalizer 边界。

	// 账号级请求头覆写（仅 openai api_key 账号启用时生效；OAuth 路径 no-op）。
	// 覆盖所有 WS 模式（ctx_pool/dedicated/passthrough）的握手头。
	account.ApplyHeaderOverrides(headers)
	setOpenAICodexRoutingHint(headers, account, routingModel, routingServiceTier)
	logOpenAIRoutingDiagnostics(
		ctx,
		account,
		string(decision.Transport),
		routingModel,
		routingServiceTier,
		strings.TrimSpace(headers.Get(openAICodexRoutingHintHeader)) != "",
		"soft_routing_hint",
	)

	return headers, sessionResolution, nil
}

func (s *OpenAIGatewayService) buildOpenAIWSCreatePayload(reqBody map[string]any, account *Account) map[string]any {
	// OpenAI WS Mode 协议：response.create 字段与 HTTP /responses 基本一致。
	// 保留 stream 字段（与 Codex CLI 一致），仅移除 background。
	payload := make(map[string]any, len(reqBody)+1)
	for k, v := range reqBody {
		payload[k] = v
	}

	delete(payload, "background")
	if _, exists := payload["stream"]; !exists {
		payload["stream"] = true
	}
	payload["type"] = "response.create"

	// OAuth 默认保持 store=false，避免误依赖服务端历史。
	if account != nil && account.UsesOpenAICodexProtocol() && !s.isOpenAIWSStoreRecoveryAllowed(account) {
		payload["store"] = false
	}
	return payload
}

func setOpenAIWSTurnMetadata(payload map[string]any, turnMetadata string) {
	if len(payload) == 0 {
		return
	}
	metadata := strings.TrimSpace(turnMetadata)
	if metadata == "" {
		return
	}

	switch existing := payload["client_metadata"].(type) {
	case map[string]any:
		existing[openAIWSTurnMetadataHeader] = metadata
		payload["client_metadata"] = existing
	case map[string]string:
		next := make(map[string]any, len(existing)+1)
		for k, v := range existing {
			next[k] = v
		}
		next[openAIWSTurnMetadataHeader] = metadata
		payload["client_metadata"] = next
	default:
		payload["client_metadata"] = map[string]any{
			openAIWSTurnMetadataHeader: metadata,
		}
	}
}

func (s *OpenAIGatewayService) isOpenAIWSStoreRecoveryAllowed(account *Account) bool {
	if account != nil && account.IsOpenAIWSAllowStoreRecoveryEnabled() {
		return true
	}
	if s != nil && s.cfg != nil && s.cfg.Gateway.OpenAIWS.AllowStoreRecovery {
		return true
	}
	return false
}

func (s *OpenAIGatewayService) isOpenAIWSStoreDisabledInRequest(reqBody map[string]any, account *Account) bool {
	if account != nil && account.UsesOpenAICodexProtocol() && !s.isOpenAIWSStoreRecoveryAllowed(account) {
		return true
	}
	if len(reqBody) == 0 {
		return false
	}
	rawStore, ok := reqBody["store"]
	if !ok {
		return false
	}
	storeEnabled, ok := rawStore.(bool)
	if !ok {
		return false
	}
	return !storeEnabled
}

func (s *OpenAIGatewayService) isOpenAIWSStoreDisabledInRequestRaw(reqBody []byte, account *Account) bool {
	if account != nil && account.UsesOpenAICodexProtocol() && !s.isOpenAIWSStoreRecoveryAllowed(account) {
		return true
	}
	if len(reqBody) == 0 {
		return false
	}
	storeValue := gjson.GetBytes(reqBody, "store")
	if !storeValue.Exists() {
		return false
	}
	if storeValue.Type != gjson.True && storeValue.Type != gjson.False {
		return false
	}
	return !storeValue.Bool()
}

func (s *OpenAIGatewayService) openAIWSStoreDisabledConnMode() string {
	if s == nil || s.cfg == nil {
		return openAIWSStoreDisabledConnModeStrict
	}
	mode := strings.ToLower(strings.TrimSpace(s.cfg.Gateway.OpenAIWS.StoreDisabledConnMode))
	switch mode {
	case openAIWSStoreDisabledConnModeStrict, openAIWSStoreDisabledConnModeAdaptive, openAIWSStoreDisabledConnModeOff:
		return mode
	case "":
		// 兼容旧配置：仅配置了布尔开关时按旧语义推导。
		if s.cfg.Gateway.OpenAIWS.StoreDisabledForceNewConn {
			return openAIWSStoreDisabledConnModeStrict
		}
		return openAIWSStoreDisabledConnModeOff
	default:
		return openAIWSStoreDisabledConnModeStrict
	}
}

func shouldForceNewConnOnStoreDisabled(mode, lastFailureReason string) bool {
	switch mode {
	case openAIWSStoreDisabledConnModeOff:
		return false
	case openAIWSStoreDisabledConnModeAdaptive:
		reason := strings.TrimPrefix(strings.TrimSpace(lastFailureReason), "prewarm_")
		switch reason {
		case "policy_violation", "message_too_big", "auth_failed", "write_request", "write":
			return true
		default:
			return false
		}
	default:
		return true
	}
}

func dropPreviousResponseIDFromRawPayload(payload []byte) ([]byte, bool, error) {
	return dropPreviousResponseIDFromRawPayloadWithDeleteFn(payload, sjson.DeleteBytes)
}

func dropPreviousResponseIDFromRawPayloadWithDeleteFn(
	payload []byte,
	deleteFn func([]byte, string) ([]byte, error),
) ([]byte, bool, error) {
	if len(payload) == 0 {
		return payload, false, nil
	}
	if !gjson.GetBytes(payload, "previous_response_id").Exists() {
		return payload, false, nil
	}
	if deleteFn == nil {
		deleteFn = sjson.DeleteBytes
	}

	updated := payload
	for i := 0; i < openAIWSMaxPrevResponseIDDeletePasses &&
		gjson.GetBytes(updated, "previous_response_id").Exists(); i++ {
		next, err := deleteFn(updated, "previous_response_id")
		if err != nil {
			return payload, false, err
		}
		updated = next
	}
	return updated, !gjson.GetBytes(updated, "previous_response_id").Exists(), nil
}

func setPreviousResponseIDToRawPayload(payload []byte, previousResponseID string) ([]byte, error) {
	normalizedPrevID := strings.TrimSpace(previousResponseID)
	if len(payload) == 0 || normalizedPrevID == "" {
		return payload, nil
	}
	updated, err := sjson.SetBytes(payload, "previous_response_id", normalizedPrevID)
	if err == nil {
		return updated, nil
	}

	var reqBody map[string]any
	if unmarshalErr := decodeOpenAIJSONUseNumber(payload, &reqBody); unmarshalErr != nil {
		return nil, err
	}
	reqBody["previous_response_id"] = normalizedPrevID
	rebuilt, marshalErr := json.Marshal(reqBody)
	if marshalErr != nil {
		return nil, marshalErr
	}
	return rebuilt, nil
}

type openAIWSContextWindowBoundary struct {
	WindowID                  string
	Changed                   bool
	PreviousResponseIDRemoved bool
}

func openAIWSPayloadCodexWindowID(payload []byte) string {
	if len(payload) == 0 {
		return ""
	}
	if windowID := strings.TrimSpace(gjson.GetBytes(payload, "client_metadata.x-codex-window-id").String()); windowID != "" {
		return windowID
	}
	turnMetadata := strings.TrimSpace(gjson.GetBytes(payload, "client_metadata.x-codex-turn-metadata").String())
	if turnMetadata == "" {
		return ""
	}
	return strings.TrimSpace(gjson.Get(turnMetadata, "window_id").String())
}

// applyOpenAIWSContextWindowBoundary 只在非官方出站路径上执行窗口切换断链。
// 官方出站路径保持客户端帧保真：出站帧的 previous_response_id 必须与客户端原始帧一致
// （官方出站 WS 帧准备环节校验），删掉它会让该帧被判为篡改并关闭连接；
// 这里只记录窗口编号，续接锚点照常按上一轮响应推导，与合并前行为一致。
func applyOpenAIWSContextWindowBoundary(
	payload []byte,
	previousWindowID string,
	officialEgress bool,
) ([]byte, openAIWSContextWindowBoundary, error) {
	if officialEgress {
		return payload, openAIWSContextWindowBoundary{WindowID: openAIWSPayloadCodexWindowID(payload)}, nil
	}
	return normalizeOpenAIWSContextWindowBoundary(payload, previousWindowID)
}

// normalizeOpenAIWSContextWindowBoundary breaks a Responses continuation chain
// when Codex moves to a new local context window. WebSocket response.create can
// still carry the previous window's previous_response_id after new_context,
// while HTTP starts the new window without that continuation anchor.
func normalizeOpenAIWSContextWindowBoundary(
	payload []byte,
	previousWindowID string,
) ([]byte, openAIWSContextWindowBoundary, error) {
	currentWindowID := openAIWSPayloadCodexWindowID(payload)
	boundary := openAIWSContextWindowBoundary{WindowID: currentWindowID}
	if previousWindowID == "" || currentWindowID == "" || currentWindowID == previousWindowID {
		return payload, boundary, nil
	}
	boundary.Changed = true
	updated, removed, err := dropPreviousResponseIDFromRawPayload(payload)
	if err != nil {
		return payload, boundary, err
	}
	boundary.PreviousResponseIDRemoved = removed
	return updated, boundary, nil
}

func shouldInferIngressFunctionCallOutputPreviousResponseID(
	storeDisabled bool,
	turn int,
	signals ToolContinuationSignals,
	currentPreviousResponseID string,
	expectedPreviousResponseID string,
) bool {
	if !storeDisabled || turn <= 1 || !signals.HasFunctionCallOutput {
		return false
	}
	if strings.TrimSpace(currentPreviousResponseID) != "" {
		return false
	}
	if signals.HasFunctionCallOutputMissingCallID {
		return false
	}
	// If the client already sent the actual tool-call context, treat this as
	// a full replay / self-contained continuation payload rather than
	// downgrading it into an inferred delta continuation. item_reference alone
	// is not enough on the store=false WS path: it still needs a valid prior
	// response anchor so upstream can resolve the referenced function_call.
	if signals.HasToolCallContext {
		return false
	}
	return strings.TrimSpace(expectedPreviousResponseID) != ""
}

func alignStoreDisabledPreviousResponseID(
	payload []byte,
	expectedPreviousResponseID string,
) ([]byte, bool, error) {
	if len(payload) == 0 {
		return payload, false, nil
	}
	expected := strings.TrimSpace(expectedPreviousResponseID)
	if expected == "" {
		return payload, false, nil
	}
	current := openAIWSPayloadStringFromRaw(payload, "previous_response_id")
	if current == "" || current == expected {
		return payload, false, nil
	}

	withoutPrev, removed, dropErr := dropPreviousResponseIDFromRawPayload(payload)
	if dropErr != nil {
		return payload, false, dropErr
	}
	if !removed {
		return payload, false, nil
	}
	updated, setErr := setPreviousResponseIDToRawPayload(withoutPrev, expected)
	if setErr != nil {
		return payload, false, setErr
	}
	return updated, true, nil
}

// Replay 状态所有权不变式：replay 序列中的 json.RawMessage 正文一经放入即视为
// 不可变，所有持有者共享同一份字节，任何修改都必须整体替换元素或重建 payload。
// 序列头数组在跨持有者保存时必须新建（combineOpenAIWSReplayItems），禁止通过
// 共享头 append，否则会写入其他持有者可见的底层数组。

// combineOpenAIWSReplayItems 合并历史与增量为新头数组，正文共享不复制。
func combineOpenAIWSReplayItems(history, delta []json.RawMessage) []json.RawMessage {
	if len(delta) == 0 {
		return history
	}
	combined := make([]json.RawMessage, 0, len(history)+len(delta))
	combined = append(combined, history...)
	return append(combined, delta...)
}

// openAIWSPayloadStringView 返回与 payload 共享底层数组的零拷贝 string 视图，
// 供 gjson.Get 使用（gjson.GetBytes 会整段复制结果 Raw，对 input 这类占
// payload 主体的字段是每次 O(payload) 分配）。调用方必须保证 payload 在结果
// 存活期间不可变（replay 所有权不变式）。
func openAIWSPayloadStringView(payload []byte) string {
	return unsafe.String(unsafe.SliceData(payload), len(payload))
}

// openAIWSRawMessageFromResult 优先返回 parent 的子切片（gjson 值零拷贝共享），
// Index 不可用时回退为复制。共享要求 parent 遵守上面的不可变约定。
func openAIWSRawMessageFromResult(parent []byte, value gjson.Result) json.RawMessage {
	idx := value.Index
	if idx > 0 && idx+len(value.Raw) <= len(parent) && string(parent[idx:idx+len(value.Raw)]) == value.Raw {
		return json.RawMessage(parent[idx : idx+len(value.Raw)])
	}
	return json.RawMessage(value.Raw)
}

const (
	openAIWSReplayInputMaxBytesDefault int64 = 16 * 1024 * 1024
	openAIWSReplayInputMaxItemsDefault       = 4096
)

type openAIWSReplayInputLimits struct {
	maxBytes int64
	maxItems int
}

type openAIWSReplayInputState struct {
	items       []json.RawMessage
	exists      bool
	unavailable bool
	rawBytes    int64
}

type openAIWSReplayInputLimitError struct {
	items    int
	rawBytes int64
	limits   openAIWSReplayInputLimits
}

func (e *openAIWSReplayInputLimitError) Error() string {
	if e == nil {
		return "websocket replay input exceeds configured limit"
	}
	return fmt.Sprintf(
		"websocket replay input exceeds configured limit: items=%d max_items=%d raw_bytes=%d max_bytes=%d",
		e.items,
		e.limits.maxItems,
		e.rawBytes,
		e.limits.maxBytes,
	)
}

func defaultOpenAIWSReplayInputLimits() openAIWSReplayInputLimits {
	return openAIWSReplayInputLimits{
		maxBytes: openAIWSReplayInputMaxBytesDefault,
		maxItems: openAIWSReplayInputMaxItemsDefault,
	}
}

func openAIWSRawMessagesBytes(items []json.RawMessage) int64 {
	var total int64
	for _, item := range items {
		total += int64(len(item))
	}
	return total
}

func newOpenAIWSReplayInputState(items []json.RawMessage, exists bool) openAIWSReplayInputState {
	return openAIWSReplayInputState{
		items:    items,
		exists:   exists,
		rawBytes: openAIWSRawMessagesBytes(items),
	}
}

func unavailableOpenAIWSReplayInputState() openAIWSReplayInputState {
	return openAIWSReplayInputState{unavailable: true}
}

func validateOpenAIWSReplayInputState(
	state openAIWSReplayInputState,
	limits openAIWSReplayInputLimits,
) (openAIWSReplayInputState, error) {
	if state.unavailable {
		return state, nil
	}
	if (limits.maxItems > 0 && len(state.items) > limits.maxItems) ||
		(limits.maxBytes > 0 && state.rawBytes > limits.maxBytes) {
		return unavailableOpenAIWSReplayInputState(), &openAIWSReplayInputLimitError{
			items:    len(state.items),
			rawBytes: state.rawBytes,
			limits:   limits,
		}
	}
	return state, nil
}

func appendOpenAIWSReplayInputState(
	state openAIWSReplayInputState,
	items []json.RawMessage,
	limits openAIWSReplayInputLimits,
) (openAIWSReplayInputState, error) {
	if state.unavailable || len(items) == 0 {
		return state, nil
	}
	nextItems := len(state.items) + len(items)
	nextRawBytes := state.rawBytes + openAIWSRawMessagesBytes(items)
	if (limits.maxItems > 0 && nextItems > limits.maxItems) ||
		(limits.maxBytes > 0 && nextRawBytes > limits.maxBytes) {
		return unavailableOpenAIWSReplayInputState(), &openAIWSReplayInputLimitError{
			items:    nextItems,
			rawBytes: nextRawBytes,
			limits:   limits,
		}
	}
	state.items = combineOpenAIWSReplayItems(state.items, items)
	state.exists = true
	state.rawBytes = nextRawBytes
	return state, nil
}

func normalizeOpenAIWSJSONForCompare(raw []byte) ([]byte, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return nil, errors.New("json is empty")
	}
	var decoded any
	if err := decodeOpenAIJSONUseNumber(trimmed, &decoded); err != nil {
		return nil, err
	}
	return json.Marshal(decoded)
}

func normalizeOpenAIWSJSONForCompareOrRaw(raw []byte) []byte {
	normalized, err := normalizeOpenAIWSJSONForCompare(raw)
	if err != nil {
		return bytes.TrimSpace(raw)
	}
	return normalized
}

func openAIWSRawJSONEqual(left, right []byte) bool {
	leftTrimmed := bytes.TrimSpace(left)
	rightTrimmed := bytes.TrimSpace(right)
	if bytes.Equal(leftTrimmed, rightTrimmed) {
		return true
	}
	return bytes.Equal(
		normalizeOpenAIWSJSONForCompareOrRaw(leftTrimmed),
		normalizeOpenAIWSJSONForCompareOrRaw(rightTrimmed),
	)
}

func normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID(payload []byte) ([]byte, error) {
	if len(payload) == 0 {
		return nil, errors.New("payload is empty")
	}
	var decoded map[string]any
	index, indexErr := buildOfficialJSONRawIndexForDecode(payload)
	if indexErr == nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		// 跨轮严格状态只需要头部字段。扫描仍验证整帧，但不解码随后必定删除的
		// input；保留下来的小状态使用独立字符串，不会钉住上一轮正文或映射。
		decoded = make(map[string]any)
		for _, member := range index.objectMembers(index.root) {
			if member.key != "input" && member.key != "previous_response_id" {
				decoded[member.key] = index.decodeValue(member.node)
			}
		}
	} else {
		// 非对象、非法 JSON 和多个顶层值沿用原解码器的错误行为。
		if err := decodeOpenAIJSONUseNumber(payload, &decoded); err != nil {
			return nil, err
		}
	}
	delete(decoded, "input")
	delete(decoded, "previous_response_id")
	return json.Marshal(decoded)
}

// openAIWSExtractNormalizedInputSequence 拆出 input 序列。返回的正文尽可能与
// payload 共享底层数组（零拷贝），受 replay 所有权不变式保护。
func openAIWSExtractNormalizedInputSequence(payload []byte) ([]json.RawMessage, bool, error) {
	if len(payload) == 0 {
		return nil, false, nil
	}
	inputValue := gjson.Get(openAIWSPayloadStringView(payload), "input")
	if !inputValue.Exists() {
		return nil, false, nil
	}
	if inputValue.Type == gjson.JSON {
		if inputValue.IsArray() {
			// gjson 宽容解析；数组整体先做零分配合法性校验，避免把断裂
			// JSON 塞进 replay 历史。
			arrayRaw := openAIWSRawMessageFromResult(payload, inputValue)
			if !json.Valid(arrayRaw) {
				return nil, true, errors.New("input array json is invalid")
			}
			// 只需原始区间，先计数再精确分配，避免 Array 先建立更大的 Result 数组。
			count := 0
			inputValue.ForEach(func(_, _ gjson.Result) bool {
				count++
				return true
			})
			items := make([]json.RawMessage, 0, count)
			inputValue.ForEach(func(_, elem gjson.Result) bool {
				items = append(items, openAIWSRawMessageFromResult(payload, elem))
				return true
			})
			return items, true, nil
		}
		return []json.RawMessage{openAIWSRawMessageFromResult(payload, inputValue)}, true, nil
	}
	if inputValue.Type == gjson.String {
		encoded, _ := json.Marshal(inputValue.String())
		return []json.RawMessage{encoded}, true, nil
	}
	return []json.RawMessage{openAIWSRawMessageFromResult(payload, inputValue)}, true, nil
}

func openAIWSInputIsPrefixExtended(previousPayload, currentPayload []byte) (bool, error) {
	previousItems, previousExists, prevErr := openAIWSExtractNormalizedInputSequence(previousPayload)
	if prevErr != nil {
		return false, prevErr
	}
	currentItems, currentExists, currentErr := openAIWSExtractNormalizedInputSequence(currentPayload)
	if currentErr != nil {
		return false, currentErr
	}
	if !previousExists && !currentExists {
		return true, nil
	}
	if !previousExists {
		return len(currentItems) == 0, nil
	}
	if !currentExists {
		return len(previousItems) == 0, nil
	}
	if len(currentItems) < len(previousItems) {
		return false, nil
	}

	for idx := range previousItems {
		if !openAIWSRawJSONEqual(previousItems[idx], currentItems[idx]) {
			return false, nil
		}
	}
	return true, nil
}

func openAIWSRawItemsHasPrefix(items []json.RawMessage, prefix []json.RawMessage) bool {
	if len(prefix) == 0 {
		return true
	}
	if len(items) < len(prefix) {
		return false
	}
	for idx := range prefix {
		// 快路径：客户端逐字节重发历史时直接比较，避免整轮历史的解码/再编码。
		if !openAIWSRawJSONEqual(prefix[idx], items[idx]) {
			return false
		}
	}
	return true
}

func openAIWSRawItemsHasFunctionCallOutput(items []json.RawMessage) bool {
	for _, item := range items {
		if isCodexToolCallOutputItemType(gjson.GetBytes(item, "type").String()) {
			return true
		}
	}
	return false
}

func openAIWSRawItemsHaveToolCallContextForOutputs(items []json.RawMessage) bool {
	if len(items) == 0 {
		return false
	}
	contextCallIDs := make(map[string]struct{})
	outputCallIDs := make(map[string]struct{})
	for _, item := range items {
		itemType := gjson.GetBytes(item, "type").String()
		callID := strings.TrimSpace(gjson.GetBytes(item, "call_id").String())
		switch {
		case isCodexToolCallContextItemType(itemType):
			if callID != "" {
				contextCallIDs[callID] = struct{}{}
			}
		case isCodexToolCallOutputItemType(itemType):
			if callID == "" {
				return false
			}
			outputCallIDs[callID] = struct{}{}
		}
	}
	if len(outputCallIDs) == 0 || len(contextCallIDs) == 0 {
		return false
	}
	for callID := range outputCallIDs {
		if _, ok := contextCallIDs[callID]; !ok {
			return false
		}
	}
	return true
}

// sanitizeOpenAIWSHistoricalReplayToolCalls 返回的新头数组与 previousItems 共享正文。
func sanitizeOpenAIWSHistoricalReplayToolCalls(
	previousItems []json.RawMessage,
	currentItems []json.RawMessage,
) []json.RawMessage {
	if len(previousItems) == 0 {
		return previousItems
	}
	outputCallIDs := make(map[string]struct{})
	collectOutputCallIDs := func(items []json.RawMessage) {
		for _, item := range items {
			if !isCodexToolCallOutputItemType(gjson.GetBytes(item, "type").String()) {
				continue
			}
			if callID := strings.TrimSpace(gjson.GetBytes(item, "call_id").String()); callID != "" {
				outputCallIDs[callID] = struct{}{}
			}
		}
	}
	collectOutputCallIDs(previousItems)
	collectOutputCallIDs(currentItems)

	var sanitized []json.RawMessage
	for idx, item := range previousItems {
		if isCodexToolCallContextItemType(gjson.GetBytes(item, "type").String()) {
			callID := strings.TrimSpace(gjson.GetBytes(item, "call_id").String())
			if _, paired := outputCallIDs[callID]; !paired {
				if sanitized == nil {
					sanitized = make([]json.RawMessage, 0, len(previousItems)-1)
					sanitized = append(sanitized, previousItems[:idx]...)
				}
				continue
			}
		}
		if sanitized != nil {
			sanitized = append(sanitized, item)
		}
	}
	if sanitized == nil {
		return previousItems
	}
	return sanitized
}

func openAIWSRawPayloadHasToolCallOutput(payload []byte) bool {
	if len(payload) == 0 {
		return false
	}
	input := gjson.Get(openAIWSPayloadStringView(payload), "input")
	if !input.Exists() {
		return false
	}
	if input.IsArray() {
		found := false
		input.ForEach(func(_, item gjson.Result) bool {
			found = isCodexToolCallOutputItemType(item.Get("type").String())
			return !found
		})
		return found
	}
	if input.Type == gjson.JSON {
		return isCodexToolCallOutputItemType(input.Get("type").String())
	}
	return false
}

func buildOpenAIWSReplayInputSequence(
	previousFullInput []json.RawMessage,
	previousFullInputExists bool,
	currentPayload []byte,
	hasPreviousResponseID bool,
) ([]json.RawMessage, bool, error) {
	currentItems, currentExists, currentErr := openAIWSExtractNormalizedInputSequence(currentPayload)
	if currentErr != nil {
		return nil, false, currentErr
	}
	state, stateErr := buildOpenAIWSReplayInputState(
		newOpenAIWSReplayInputState(previousFullInput, previousFullInputExists),
		currentItems,
		currentExists,
		hasPreviousResponseID,
		defaultOpenAIWSReplayInputLimits(),
	)
	return state.items, state.exists, stateErr
}

func buildOpenAIWSReplayInputState(
	previous openAIWSReplayInputState,
	currentItems []json.RawMessage,
	currentExists bool,
	hasPreviousResponseID bool,
	limits openAIWSReplayInputLimits,
) (openAIWSReplayInputState, error) {
	if !hasPreviousResponseID {
		return validateOpenAIWSReplayInputState(
			newOpenAIWSReplayInputState(currentItems, currentExists),
			limits,
		)
	}
	if previous.unavailable {
		return unavailableOpenAIWSReplayInputState(), nil
	}
	if !previous.exists {
		return validateOpenAIWSReplayInputState(
			newOpenAIWSReplayInputState(currentItems, currentExists),
			limits,
		)
	}
	previousItems := sanitizeOpenAIWSHistoricalReplayToolCalls(previous.items, currentItems)
	if !currentExists || len(currentItems) == 0 {
		return validateOpenAIWSReplayInputState(
			newOpenAIWSReplayInputState(previousItems, true),
			limits,
		)
	}
	if openAIWSRawItemsHasPrefix(currentItems, previousItems) {
		return validateOpenAIWSReplayInputState(
			newOpenAIWSReplayInputState(currentItems, true),
			limits,
		)
	}
	merged := combineOpenAIWSReplayItems(previousItems, currentItems)
	return validateOpenAIWSReplayInputState(
		newOpenAIWSReplayInputState(merged, true),
		limits,
	)
}

func setOpenAIWSPayloadInputSequence(
	payload []byte,
	fullInput []json.RawMessage,
	fullInputExists bool,
) ([]byte, error) {
	if !fullInputExists {
		return payload, nil
	}
	// Preserve [] vs null semantics when input exists but is empty.
	inputForMarshal := fullInput
	if inputForMarshal == nil {
		inputForMarshal = []json.RawMessage{}
	}
	inputRaw, marshalErr := json.Marshal(inputForMarshal)
	if marshalErr != nil {
		return nil, marshalErr
	}
	return sjson.SetRawBytes(payload, "input", inputRaw)
}

func buildOpenAIWSCurrentTurnRetryPayload(
	payload []byte,
	fullInput []json.RawMessage,
	fullInputExists bool,
	originalModel string,
) ([]byte, bool, error) {
	if !fullInputExists {
		return nil, false, nil
	}
	retryPayload, err := setOpenAIWSPayloadInputSequence(payload, fullInput, true)
	if err != nil {
		return nil, false, err
	}
	retryPayload = RemovePreviousResponseIDFromBody(retryPayload)
	if model := strings.TrimSpace(originalModel); model != "" {
		retryPayload, err = sjson.SetBytes(retryPayload, "model", model)
		if err != nil {
			return nil, false, err
		}
	}
	coverage := AnalyzeToolCallOutputContextCoverageBytes(retryPayload)
	if coverage.HasFunctionCallOutput && !coverage.ContextCoversAllCallIDs {
		return nil, false, nil
	}
	return retryPayload, true, nil
}

func shouldKeepIngressPreviousResponseID(
	previousPayload []byte,
	currentPayload []byte,
	lastTurnResponseID string,
	hasFunctionCallOutput bool,
) (bool, string, error) {
	if hasFunctionCallOutput {
		return true, "has_function_call_output", nil
	}
	currentPreviousResponseID := strings.TrimSpace(openAIWSPayloadStringFromRaw(currentPayload, "previous_response_id"))
	if currentPreviousResponseID == "" {
		return false, "missing_previous_response_id", nil
	}
	expectedPreviousResponseID := strings.TrimSpace(lastTurnResponseID)
	if expectedPreviousResponseID == "" {
		return false, "missing_last_turn_response_id", nil
	}
	if currentPreviousResponseID != expectedPreviousResponseID {
		return false, "previous_response_id_mismatch", nil
	}
	if len(previousPayload) == 0 {
		return false, "missing_previous_turn_payload", nil
	}

	previousComparable, previousComparableErr := normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID(previousPayload)
	if previousComparableErr != nil {
		return false, "non_input_compare_error", previousComparableErr
	}
	currentComparable, currentComparableErr := normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID(currentPayload)
	if currentComparableErr != nil {
		return false, "non_input_compare_error", currentComparableErr
	}
	if !bytes.Equal(previousComparable, currentComparable) {
		return false, "non_input_changed", nil
	}
	return true, "strict_incremental_ok", nil
}

type openAIWSIngressPreviousTurnStrictState struct {
	nonInputComparable []byte
}

func buildOpenAIWSIngressPreviousTurnStrictState(payload []byte) (*openAIWSIngressPreviousTurnStrictState, error) {
	if len(payload) == 0 {
		return nil, nil
	}
	nonInputComparable, nonInputErr := normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID(payload)
	if nonInputErr != nil {
		return nil, nonInputErr
	}
	return &openAIWSIngressPreviousTurnStrictState{
		nonInputComparable: nonInputComparable,
	}, nil
}

func shouldKeepIngressPreviousResponseIDWithStrictState(
	previousState *openAIWSIngressPreviousTurnStrictState,
	currentPayload []byte,
	lastTurnResponseID string,
	hasFunctionCallOutput bool,
) (bool, string, error) {
	if hasFunctionCallOutput {
		return true, "has_function_call_output", nil
	}
	currentPreviousResponseID := strings.TrimSpace(openAIWSPayloadStringFromRaw(currentPayload, "previous_response_id"))
	if currentPreviousResponseID == "" {
		return false, "missing_previous_response_id", nil
	}
	expectedPreviousResponseID := strings.TrimSpace(lastTurnResponseID)
	if expectedPreviousResponseID == "" {
		return false, "missing_last_turn_response_id", nil
	}
	if currentPreviousResponseID != expectedPreviousResponseID {
		return false, "previous_response_id_mismatch", nil
	}
	if previousState == nil {
		return false, "missing_previous_turn_payload", nil
	}

	currentComparable, currentComparableErr := normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID(currentPayload)
	if currentComparableErr != nil {
		return false, "non_input_compare_error", currentComparableErr
	}
	if !bytes.Equal(previousState.nonInputComparable, currentComparable) {
		return false, "non_input_changed", nil
	}
	return true, "strict_incremental_ok", nil
}
