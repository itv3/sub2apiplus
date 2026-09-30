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
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

type openAIWSRejectedFieldRetryError struct {
	body   []byte
	reason string
}

const openAIWSReplayRestartMessage = "websocket replay history is unavailable; please restart the conversation"

func newOpenAIWSReplayUnavailableCloseError(cause error) error {
	if cause == nil {
		cause = errors.New("websocket replay history is unavailable")
	}
	return NewOpenAIWSClientCloseError(
		coderws.StatusPolicyViolation,
		openAIWSReplayRestartMessage,
		cause,
	)
}

func (e *openAIWSRejectedFieldRetryError) Error() string {
	if e == nil || strings.TrimSpace(e.reason) == "" {
		return "retry websocket turn after rejected field normalization"
	}
	return "retry websocket turn after rejected field normalization: " + e.reason
}

func openAIWSRejectedFieldRetryHTTPStatus(message []byte) int {
	for _, value := range gjson.GetManyBytes(message, "status", "status_code", "error.status", "error.status_code") {
		status := int(value.Int())
		if status >= 100 && status <= 599 {
			return status
		}
	}
	return openAIWSErrorHTTPStatus(message)
}

func (s *OpenAIGatewayService) openAIWSIngressInterTurnIdleTimeout() time.Duration {
	if s == nil || s.cfg == nil || s.cfg.Gateway.OpenAIWS.IngressInterTurnIdleTimeoutSeconds <= 0 {
		return 0
	}
	return time.Duration(s.cfg.Gateway.OpenAIWS.IngressInterTurnIdleTimeoutSeconds) * time.Second
}

// openAIWSToolContinuationFullHistoryFallbackEnabled 表示断线后客户端重发完整历史、工具输出
// 轮次无法判定时，是否按完整历史开新链放行；关闭时保持 1008 失败关闭。
func (s *OpenAIGatewayService) openAIWSToolContinuationFullHistoryFallbackEnabled() bool {
	return s != nil && s.cfg != nil && s.cfg.Gateway.OpenAIWS.ToolContinuationFullHistoryFallbackEnabled
}

type openAIWSLeaseRetirementAware interface {
	ShouldRetire() bool
}

// shouldRetireOpenAIWSLease 采用可选能力探测，既让真实连接在轮次边界主动换连，
// 又不扩大 openAIWSLeaseSession 接口，避免测试替身和其他传输适配器被迫实现池内状态。
func shouldRetireOpenAIWSLease(lease openAIWSLeaseSession) bool {
	retirementAware, ok := lease.(openAIWSLeaseRetirementAware)
	return ok && retirementAware.ShouldRetire()
}

// newOpenAIWSDownstreamWriteContext binds writes directly to the client
// lifecycle while excluding the separate ingress-lease cancellation signal.
// This lets a lease-loss path finish its current client write before
// ReadOpenAIWSClientMessage sends the retryable close frame.
func newOpenAIWSDownstreamWriteContext(controlCtx context.Context, hooks *OpenAIWSIngressHooks, timeout time.Duration) (context.Context, context.CancelFunc) {
	writeParent := controlCtx
	if hooks != nil && hooks.ClientLifecycleContext != nil {
		writeParent = hooks.ClientLifecycleContext
	}
	if writeParent == nil {
		writeParent = context.Background()
	}
	return context.WithTimeout(writeParent, timeout)
}

func (s *OpenAIGatewayService) ProxyResponsesWebSocketFromClient(
	ctx context.Context,
	c *gin.Context,
	clientConn *coderws.Conn,
	account *Account,
	token string,
	firstClientMessage []byte,
	hooks *OpenAIWSIngressHooks,
) (returnErr error) {
	if s == nil {
		return errors.New("service is nil")
	}
	if c == nil {
		return errors.New("gin context is nil")
	}
	if clientConn == nil {
		return errors.New("client websocket is nil")
	}
	if account == nil {
		return errors.New("account is nil")
	}
	// A handler may reuse the same gin context across account failover attempts.
	// Never let an OAuth attempt's response aliases leak into the next account.
	setCodexToolNameReverse(c, nil)
	if _, err := s.prepareCodexAccountIdentitySource(ctx, c, account); err != nil {
		return err
	}
	if err := validateOpenAIWSBearerToken(account, token); err != nil {
		return err
	}
	ctx = WithOpenAIAPIKeyMimicRequestContext(ctx, c)
	officialProfileEnabled, _, profileErr := resolveOfficialEgressAccountProfile(account)
	if profileErr != nil {
		return fmt.Errorf("解析 OpenAI 官方出站画像: %w", profileErr)
	}
	if officialProfileEnabled && account.IsOpenAIOAuth() {
		if capabilityErr := s.ensureOpenAIModelCapability(ctx, account, firstClientMessage); capabilityErr != nil {
			return capabilityErr
		}
	}
	ctx = s.bindOpenAIResponsesLiteCapability(ctx, account, firstClientMessage)

	// 预取一次 OpenAI Fast Policy settings，绑定到 ctx，让该 WS session
	// 内所有帧的 evaluateOpenAIFastPolicy 调用复用同一份快照，避免每帧
	// 进入 DB / settingRepo。Trade-off 见 withOpenAIFastPolicyContext 注释。
	if s.settingService != nil {
		if settings, err := s.settingService.GetOpenAIFastPolicySettings(ctx); err == nil && settings != nil {
			ctx = withOpenAIFastPolicyContext(ctx, settings)
		}
	}

	// handler 通常会跨账号重试统一登记会话；直接调用者也必须获得同一套
	// session 级抢占语义，避免旧连接继续占用上游资源。
	if preemptCtx, cleanupPreempt, armed := s.BeginOpenAIWSIngressSessionPreemptionWithClient(ctx, c, account, firstClientMessage, clientConn); armed {
		ctx = preemptCtx
		defer cleanupPreempt()
		defer func() {
			if isOpenAIWSSessionPreempted(ctx) {
				returnErr = errOpenAIWSSessionPreempted
			}
		}()
	}

	wsDecision := resolveOpenAIWSProtocolForRequest(s.getOpenAIWSProtocolResolver(), ctx, account)
	if account != nil && account.IsOpenAIOAuth() {
		// WS ingress 已经证明调用方选择了官方默认传输。内置 OAuth 的当前 Release
		// 画像固定支持 Responses WS，不能被历史服务/账号开关降成 HTTP。
		wsDecision = OpenAIWSProtocolDecision{
			Transport: OpenAIUpstreamTransportResponsesWebsocketV2,
			Reason:    "codex_profile_default_ws",
		}
	}
	officialEgressEnabled, _, officialEgressConfigErr := resolveOfficialEgressAccountProfile(account)
	if officialEgressConfigErr != nil {
		return fmt.Errorf("resolve official egress config: %w", officialEgressConfigErr)
	}
	officialEgressWSURL := ""
	attachOfficialWebSocketProfile := func() error {
		if !officialEgressEnabled {
			return nil
		}
		var buildErr error
		officialEgressWSURL, buildErr = s.buildOpenAIResponsesWSURL(account)
		if buildErr != nil {
			return fmt.Errorf("build official egress ws url: %w", buildErr)
		}
		ctx = s.bindOfficialCodexWebSocketCookieJar(ctx, account, officialCodexWebSocketReleaseMode(s))
		ctx, buildErr = attachOfficialEgressWebSocketContext(
			ctx,
			c,
			account,
			officialEgressWSURL,
			firstClientMessage,
			s.cfg,
		)
		if buildErr != nil {
			return fmt.Errorf("resolve official egress profile: %w", buildErr)
		}
		return nil
	}
	forceHTTPBridge := account.Platform == PlatformGrok ||
		(!officialEgressEnabled && s.pluginManager != nil && s.pluginManager.ShouldRouteOpenAIOAuth(account))
	modeRouterV2Enabled := s != nil && s.cfg != nil && s.cfg.Gateway.OpenAIWS.ModeRouterV2Enabled
	ingressMode := OpenAIWSIngressModeCtxPool
	if modeRouterV2Enabled && !forceHTTPBridge {
		ingressMode = account.ResolveOpenAIResponsesWebSocketV2Mode(s.cfg.Gateway.OpenAIWS.IngressModeDefault)
		if ingressMode == OpenAIWSIngressModeOff {
			return NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"websocket mode is disabled for this account",
				nil,
			)
		}
		switch ingressMode {
		case OpenAIWSIngressModePassthrough:
			if wsDecision.Transport != OpenAIUpstreamTransportResponsesWebsocketV2 {
				return fmt.Errorf("websocket ingress requires ws_v2 transport, got=%s", wsDecision.Transport)
			}
			if err := attachOfficialWebSocketProfile(); err != nil {
				return err
			}
			if s.shouldBridgeOpenAIWSPassthroughFirstMessage(account, firstClientMessage) {
				forceHTTPBridge = true
				break
			}
			// 首轮准入由握手路径完成；后续 response.create 会在写入上游前
			// 依次回调 BeforeRequest 和 BeforeTurn，并在终止或失败时回调
			// AfterTurn，从而覆盖 turn 级利润复核、定价冻结和并发槽位释放。
			return s.proxyResponsesWebSocketV2Passthrough(
				ctx,
				c,
				clientConn,
				account,
				token,
				firstClientMessage,
				hooks,
				wsDecision,
			)
		case OpenAIWSIngressModeHTTPBridge:
			forceHTTPBridge = true
		case OpenAIWSIngressModeCtxPool, OpenAIWSIngressModeShared, OpenAIWSIngressModeDedicated:
			// continue
		default:
			return NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"websocket mode only supports ctx_pool/passthrough/http_bridge",
				nil,
			)
		}
	}
	if !forceHTTPBridge && wsDecision.Transport != OpenAIUpstreamTransportResponsesWebsocketV2 {
		return fmt.Errorf("websocket ingress requires ws_v2 transport, got=%s", wsDecision.Transport)
	}
	if !forceHTTPBridge {
		if err := attachOfficialWebSocketProfile(); err != nil {
			return err
		}
	}
	runtimeSinkID := officialEgressSinkResponsesWS
	if _, enabled := OfficialEgressContextFromContext(ctx); enabled && !forceHTTPBridge {
		boundCtx, bindErr := bindOfficialEgressSink(ctx, runtimeSinkID)
		if bindErr != nil {
			return fmt.Errorf("bind Responses WebSocket official egress sink: %w", bindErr)
		}
		ctx = boundCtx
	} else {
		runtimeSinkID = ""
	}
	var officialRuntime *OfficialEgressTransitionRuntime
	invocationID := ""
	if runtimeSinkID != "" {
		var runtimeErr error
		officialRuntime, runtimeErr = resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
		if runtimeErr != nil {
			return runtimeErr
		}
		if identity, ok := officialegress.AttemptIdentityFromContext(ctx); ok {
			invocationID = identity.InvocationID
		}
	}
	dedicatedMode := modeRouterV2Enabled && ingressMode == OpenAIWSIngressModeDedicated

	wsURL := ""
	wsHost := "-"
	wsPath := "-"
	if forceHTTPBridge {
		wsHost = "xai-http-bridge"
		wsPath = "/v1/responses"
	} else {
		var err error
		wsURL = officialEgressWSURL
		if wsURL == "" {
			wsURL, err = s.buildOpenAIResponsesWSURL(account)
		}
		if err != nil {
			return fmt.Errorf("build ws url: %w", err)
		}
		if parsedURL, parseErr := url.Parse(wsURL); parseErr == nil && parsedURL != nil {
			wsHost = normalizeOpenAIWSLogValue(parsedURL.Host)
			wsPath = normalizeOpenAIWSLogValue(parsedURL.Path)
		}
	}
	debugEnabled := isOpenAIWSModeDebugEnabled()
	isCodexCLI := openai.IsCodexOfficialClientByHeaders(c.GetHeader("User-Agent"), c.GetHeader("originator")) || (s.cfg != nil && s.cfg.Gateway.ForceCodexCLI)
	// 官方出站画像接管请求体形态后，本地注入的 image_generation 工具无法通过
	// 帧校验：官方入站按原样比对会判定请求被篡改，第三方入站则因 Responses Lite
	// 契约不接受顶层 image_generation 工具而被拒。两种情况都会断开连接，因此
	// 画像启用时（OAuth 账号）WS 链路一律不做图片桥接注入。
	skipCodexImageBridge := officialEgressEnabled

	type openAIWSClientPayload struct {
		payloadRaw               []byte
		accountIdentitySourceRaw []byte
		rawForHash               []byte
		promptCacheKey           string
		previousResponseID       string
		originalModel            string
		imageBillingModel        string
		imageSizeTier            string
		imageInputSize           string
		payloadBytes             int
		requestedReasoningEffort *string
	}
	ingressSessionOriginalModel := ""

	applyPayloadMutation := func(current []byte, path string, value any) ([]byte, error) {
		next, err := sjson.SetBytes(current, path, value)
		if err == nil {
			return next, nil
		}

		// 仅在确实需要修改 payload 且 sjson 失败时，退回 map 路径确保兼容性。
		payload := make(map[string]any)
		if unmarshalErr := json.Unmarshal(current, &payload); unmarshalErr != nil {
			return nil, err
		}
		switch path {
		case "type", "model":
			payload[path] = value
		case "client_metadata." + openAIWSTurnMetadataHeader:
			setOpenAIWSTurnMetadata(payload, fmt.Sprintf("%v", value))
		default:
			return nil, err
		}
		rebuilt, marshalErr := json.Marshal(payload)
		if marshalErr != nil {
			return nil, marshalErr
		}
		return rebuilt, nil
	}

	parseClientPayload := func(turn int, raw []byte) (openAIWSClientPayload, error) {
		trimmed := bytes.TrimSpace(raw)
		if len(trimmed) == 0 {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "empty websocket request payload", nil)
		}
		if !gjson.ValidBytes(trimmed) {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", errors.New("invalid json"))
		}

		values := gjson.GetManyBytes(trimmed, "type", "model", "prompt_cache_key", "previous_response_id")
		eventType := strings.TrimSpace(values[0].String())
		normalized := trimmed
		switch eventType {
		case "":
			eventType = "response.create"
			next, setErr := applyPayloadMutation(normalized, "type", eventType)
			if setErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", setErr)
			}
			normalized = next
		case "response.create":
		case "response.append":
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"response.append is not supported in ws v2; use response.create with previous_response_id",
				nil,
			)
		default:
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				fmt.Sprintf("unsupported websocket request type: %s", eventType),
				nil,
			)
		}
		requestedReasoningEffort := CanonicalRequestedReasoningEffort(normalized, strings.TrimSpace(values[1].String()))
		if next, policyErr := applyOpenAIWSReasoningEffortPolicy(normalized, hooks); policyErr != nil {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, policyErr.Error(), policyErr)
		} else {
			normalized = next
		}
		responsesLite := isOpenAIResponsesLiteWebSocketPayload(normalized)
		if compatibilityBody, compatibilityChanged, compatibilityErr := normalizeOpenAIResponsesWebSocketCompatibilityBody(normalized, account, responsesLite); compatibilityErr != nil {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", compatibilityErr)
		} else if compatibilityChanged {
			normalized = compatibilityBody
		}
		if account.IsOpenAIOAuthLike() {
			aliasedBody, reverse, aliased, aliasErr := aliasOpenAIOAuthReservedToolNamesBody(normalized)
			if aliasErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, aliasErr.Error(), aliasErr)
			}
			updateCodexToolNameReverseForWSFrame(c, normalized, reverse)
			if aliased {
				normalized = aliasedBody
			}
		}

		originalModel := strings.TrimSpace(values[1].String())
		modelMissing := originalModel == ""
		if originalModel == "" {
			// 入站 WS 长会话里，部分客户端只在第一轮 response.create 上声明
			// model，后续 turn 复用同一 session-level model。为避免因省略
			// model 直接断开用户连接，这里回落到上一轮已通过校验的客户端模型，
			// 并在下方写回上游 payload，保证账号模型映射/fast policy/图片权限
			// 仍按同一模型执行。
			originalModel = ingressSessionOriginalModel
			if originalModel == "" {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"model is required in response.create payload",
					nil,
				)
			}
		}
		promptCacheKey := strings.TrimSpace(values[2].String())
		previousResponseID := strings.TrimSpace(values[3].String())
		previousResponseIDKind := ClassifyOpenAIPreviousResponseIDKind(previousResponseID)
		if previousResponseID != "" && previousResponseIDKind == OpenAIPreviousResponseIDKindMessageID {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"previous_response_id must be a response.id (resp_*), not a message id",
				nil,
			)
		}
		// 官方 Codex 会在同一连接内为每个 response.create 更新 Body 中的
		// turn metadata；握手 Header 则保持建连时的 prewarm metadata。
		// Official Egress 必须保留逐帧真实值，不能用连接级 Header 覆盖。
		officialEgressFrameEnabled := officialEgressEnabled
		if turnMetadata := strings.TrimSpace(c.GetHeader(openAIWSTurnMetadataHeader)); turnMetadata != "" &&
			!officialEgressFrameEnabled {
			next, setErr := applyPayloadMutation(normalized, "client_metadata."+openAIWSTurnMetadataHeader, turnMetadata)
			if setErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", setErr)
			}
			normalized = next
		}
		accountIdentitySourceRaw := append([]byte(nil), normalized...)
		accountScopedPayload, accountScoped, scopeErr := applyCodexAccountIdentityClientMetadataRaw(normalized, codexAccountIdentitySource(c, account), getAPIKeyIDFromContext(c))
		if scopeErr != nil {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket identity metadata", scopeErr)
		}
		if accountScoped {
			normalized = accountScopedPayload
		}
		// OAuth 的模型能力判定和 Setup Token 的帧信号都必须保留；API Key
		// 由帧内 Lite 信号触发。统一使用按账号类型分派的归一化函数，吸收
		// 上游对 API Key 并行工具调用的兼容处理。
		useResponsesLite := account.IsOpenAIOAuth() && openAIResponsesLiteCapabilityFromContext(ctx)
		if account.IsOpenAI() && account.Type == AccountTypeSetupToken {
			useResponsesLite = isOpenAIResponsesLiteWebSocketPayload(normalized)
		}
		if useResponsesLite || responsesLite {
			litePayload, _, liteErr := normalizeOpenAIResponsesLitePayloadForAccount(normalized, account)
			if liteErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					liteErr.Error(),
					liteErr,
				)
			}
			normalized = litePayload
		}
		apiKey := getAPIKeyFromContext(c)
		imageGenerationAllowed := GroupAllowsImageGeneration(apiKeyGroup(apiKey))
		codexImageGenerationExplicitToolPolicy := codexImageGenerationExplicitToolPolicyAllow
		if isCodexCLI {
			codexImageGenerationExplicitToolPolicy = account.CodexImageGenerationExplicitToolPolicy()
		}
		codexBridgeEnabled := isCodexCLI &&
			!skipCodexImageBridge &&
			!isOpenAIResponsesLiteWebSocketPayload(normalized) &&
			imageGenerationAllowed &&
			codexImageGenerationExplicitToolPolicy != codexImageGenerationExplicitToolPolicyStrip &&
			s.isCodexImageGenerationBridgeEnabled(ctx, account, apiKey)
		if codexBridgeEnabled {
			payloadMap := make(map[string]any)
			if err := decodeOpenAIJSONUseNumber(normalized, &payloadMap); err != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", err)
			}
			bridgeModified := false
			if ensureOpenAIResponsesImageGenerationTool(payloadMap) {
				bridgeModified = true
				logOpenAIWSModeInfo("ingress_ws_codex_image_tool_injected account_id=%d", account.ID)
			}
			if ensureOpenAIResponsesImageGenerationToolChoiceAuto(payloadMap) {
				bridgeModified = true
				logOpenAIWSModeInfo("ingress_ws_codex_image_tool_choice_auto account_id=%d", account.ID)
			}
			if normalizeOpenAIResponsesImageGenerationTools(payloadMap) {
				bridgeModified = true
			}
			if applyCodexImageGenerationBridgeInstructions(payloadMap) {
				bridgeModified = true
				logOpenAIWSModeInfo("ingress_ws_codex_image_bridge_instructions_added account_id=%d", account.ID)
			}
			if bridgeModified {
				rebuilt, marshalErr := json.Marshal(payloadMap)
				if marshalErr != nil {
					return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", marshalErr)
				}
				normalized = rebuilt
			}
		}
		requestModel := originalModel
		if hooks != nil && hooks.MapRequestModel != nil {
			mappedModel, mapErr := hooks.MapRequestModel(turn, originalModel)
			if mapErr != nil {
				return openAIWSClientPayload{}, mapErr
			}
			if mappedModel = strings.TrimSpace(mappedModel); mappedModel != "" {
				requestModel = mappedModel
			}
		}
		upstreamModel := normalizeOpenAIModelForUpstream(account, account.GetMappedModel(requestModel))
		if modelMissing || upstreamModel != originalModel {
			next, setErr := applyPayloadMutation(normalized, "model", upstreamModel)
			if setErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", setErr)
			}
			normalized = next
		}
		SetOpsUpstreamModel(c, upstreamModel)
		if isCodexCLI && codexImageGenerationExplicitToolPolicy == codexImageGenerationExplicitToolPolicyStrip {
			if stripped, changed, stripErr := stripOpenAIImageGenerationToolsFromRawPayload(normalized); stripErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", stripErr)
			} else if changed {
				normalized = stripped
				logOpenAIWSModeInfo("ingress_ws_codex_image_tool_stripped_by_policy account_id=%d", account.ID)
			}
		}
		if stripped, changed, stripErr := stripCodexSparkImageGenerationToolFromRawPayload(normalized, upstreamModel); stripErr != nil {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", stripErr)
		} else if changed {
			normalized = stripped
			logOpenAIWSModeInfo("ingress_ws_codex_spark_image_tool_stripped account_id=%d", account.ID)
		}
		imageIntent := IsImageGenerationIntentForPlatform(openAIResponsesEndpoint, originalModel, normalized, account.Platform)
		if imageIntent && !imageGenerationAllowed {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, ImageGenerationPermissionMessage(), nil)
		}
		imageBillingModel := ""
		imageSizeTier := ""
		imageInputSize := ""
		if imageIntent {
			var imageCfgErr error
			imageCfg, imageCfgErr := resolveOpenAIResponsesImageBillingConfigDetailedFromBody(normalized, originalModel)
			if imageCfgErr != nil {
				return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, imageCfgErr.Error(), imageCfgErr)
			}
			imageBillingModel = imageCfg.Model
			imageSizeTier = imageCfg.SizeTier
			imageInputSize = imageCfg.InputSize
		}

		// Apply OpenAI Fast Policy on the response.create frame using the same
		// evaluator/normalize/scope rules as the HTTP entrypoints. This is the
		// single integration point for all WS ingress turns (first + follow-up
		// frames flow through here).
		//
		// Model fallback: first turn still requires model at the handler layer；
		// follow-up response.create frames may omit it and then reuse
		// ingressSessionOriginalModel. We always write a concrete upstream model
		// before evaluating policy, so whitelist / filter behavior remains stable.
		policyApplied, blocked, policyErr := s.applyOpenAIFastPolicyToWSResponseCreate(ctx, account, upstreamModel, normalized)
		if policyErr != nil {
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(coderws.StatusPolicyViolation, "invalid websocket request payload", policyErr)
		}
		if blocked != nil {
			MarkOpsClientBusinessLimited(c, OpsClientBusinessLimitedReasonLocalPolicyDenied)
			// Send a Realtime-style error event to the client first, then
			// signal the handler to close the connection with PolicyViolation.
			// We intentionally do NOT forward this frame upstream.
			//
			// coder/websocket@v1.8.14 Conn.Write is synchronous and flushes
			// the underlying bufio writer before returning (write.go:42 →
			// 307-311), and the subsequent close handshake re-acquires the
			// same writeFrameMu, so the error event is guaranteed to reach
			// the kernel send buffer before any close frame is queued.
			eventBytes := buildOpenAIFastPolicyBlockedWSEvent(blocked)
			if eventBytes != nil {
				writeCtx, cancel := newOpenAIWSDownstreamWriteContext(ctx, hooks, s.openAIWSWriteTimeout())
				_ = clientConn.Write(writeCtx, coderws.MessageText, eventBytes)
				cancel()
			}
			return openAIWSClientPayload{}, NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				blocked.Message,
				blocked,
			)
		}
		normalized = policyApplied
		ingressSessionOriginalModel = originalModel

		return openAIWSClientPayload{
			payloadRaw:               normalized,
			accountIdentitySourceRaw: accountIdentitySourceRaw,
			rawForHash:               trimmed,
			promptCacheKey:           promptCacheKey,
			previousResponseID:       previousResponseID,
			originalModel:            originalModel,
			imageBillingModel:        imageBillingModel,
			imageSizeTier:            imageSizeTier,
			imageInputSize:           imageInputSize,
			payloadBytes:             len(normalized),
			requestedReasoningEffort: requestedReasoningEffort,
		}, nil
	}

	writeClientMessage := func(message []byte) error {
		writeCtx, cancel := newOpenAIWSDownstreamWriteContext(ctx, hooks, s.openAIWSWriteTimeout())
		defer cancel()
		message = restoreCodexToolNamesFromContext(c, message)
		return clientConn.Write(writeCtx, coderws.MessageText, message)
	}

	readClientMessage := func() ([]byte, error) {
		idleTimeout := s.openAIWSIngressInterTurnIdleTimeout()
		msgType, payload, readErr := ReadOpenAIWSClientMessage(
			ctx,
			clientConn,
			idleTimeout,
			coderws.StatusNormalClosure,
			"websocket idle timeout",
		)
		if readErr != nil {
			var closeErr *OpenAIWSClientCloseError
			if errors.As(readErr, &closeErr) && closeErr.StatusCode() == coderws.StatusNormalClosure {
				logOpenAIWSModeInfo("ingress_ws_inter_turn_idle_timeout account_id=%d timeout_seconds=%d", account.ID, int(idleTimeout.Seconds()))
			}
			return nil, readErr
		}
		if msgType != coderws.MessageText && msgType != coderws.MessageBinary {
			return nil, NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				fmt.Sprintf("unsupported websocket client message type: %s", msgType.String()),
				nil,
			)
		}
		return payload, nil
	}

	firstPayload, err := parseClientPayload(1, firstClientMessage)
	if err != nil {
		return err
	}

	useHTTPBridge := forceHTTPBridge || s.shouldBridgeOpenAIWSHTTP(account, firstPayload.payloadBytes, firstPayload.previousResponseID)
	turnState := strings.TrimSpace(c.GetHeader(openAIWSTurnStateHeader))
	// 画像声明 turn-state 按账号 owner 隔离时：客户端回带的 turn-state 若已知由其他账号
	// 铸造则丢弃，也不采纳无法归属账号的会话级缓存值，账号切换后旧值不再回送。
	turnStateOwnerIsolation := officialEgressEnabled &&
		officialCodexTurnStateOwnerIsolation(officialCodexWebSocketReleaseMode(s))
	if turnStateOwnerIsolation {
		turnState = s.isolateOfficialCodexIngressTurnState(c, account, turnState)
		if turnState == "" && c.Request != nil {
			// HTTP 桥接轮次直接读取本连接请求头作为回送值（proxyOpenAIWSHTTPBridgeTurn）；
			// 被隔离丢弃的跨账号值必须同步从请求头移除，否则仍会经桥接出站。handler 在
			// 账号 failover 时复用同一 gin 上下文，这里同时清掉上一账号桥接轮次写回的值。
			c.Request.Header.Del(openAIWSTurnStateHeader)
		}
	}
	stateStore := s.getOpenAIWSStateStore()
	groupID := getOpenAIGroupIDFromContext(c)
	apiKeyID := getAPIKeyIDFromContext(c)
	storeDisabledConnMode := s.openAIWSStoreDisabledConnMode()
	sessionHash := ""
	preferredConnID := ""
	storeDisabled := false
	refreshIngressRouteState := func(payload openAIWSClientPayload) {
		// 会话级状态按执行作用域隔离：codex 多智能体共用 session-id，只有线程标识能把
		// 父线程与子智能体区分开；没有声明身份时沿用原会话哈希。账号粘性仍由 handler 决定。
		sessionHash = s.GenerateSessionHash(c, payload.rawForHash)
		if scope, _ := resolveOpenAIWSExecutionScope(c, payload.rawForHash, apiKeyID); scope != "" {
			sessionHash = scope
		}
		preferredConnID = ""
		storeDisabled = s.isOpenAIWSStoreDisabledInRequestRaw(payload.payloadRaw, account)
		if useHTTPBridge {
			// Sticky account affinity may be shared, but an HTTP bridge must not
			// inherit another connection's native WS turn state or socket binding.
			return
		}
		if turnState == "" && stateStore != nil && sessionHash != "" && !turnStateOwnerIsolation {
			if savedTurnState, ok := stateStore.GetSessionTurnState(groupID, sessionHash); ok {
				turnState = savedTurnState
			}
		}

		if stateStore != nil && payload.previousResponseID != "" {
			if connID, ok := stateStore.GetResponseConn(payload.previousResponseID); ok {
				preferredConnID = connID
			}
		}

		if stateStore != nil && storeDisabled && payload.previousResponseID == "" && sessionHash != "" {
			if connID, ok := stateStore.GetSessionConn(groupID, sessionHash); ok {
				preferredConnID = connID
			}
		}
	}
	refreshIngressRouteState(firstPayload)

	if useHTTPBridge {
		freezeOpenAIWSHTTPBridgeSessionAffinity(c, sessionHash)
		logOpenAIWSModeInfo(
			"ingress_ws_http_bridge_start account_id=%d account_type=%s payload_bytes=%d threshold_bytes=%d has_session_hash=%v store_disabled=%v",
			account.ID,
			account.Type,
			firstPayload.payloadBytes,
			s.openAIWSHTTPBridgeThresholdBytes(),
			sessionHash != "",
			storeDisabled,
		)
		currentBridgePayload := firstPayload
		// Keep the first turn as the stable conversation seed. The mapped model
		// is resolved again for each turn below so an in-connection model switch
		// cannot reuse another model's upstream cache identity.
		grokCacheSeedPayload := firstPayload.payloadRaw
		replayLimits := s.openAIWSReplayInputLimits()
		bridgeReplayState := openAIWSReplayInputState{}
		bridgeAccountFailoverState := openAIWSReplayInputState{}
		for turn := 1; ; turn++ {
			if turn > 1 && hooks != nil && hooks.BeforeRequest != nil {
				if err := hooks.BeforeRequest(turn, currentBridgePayload.rawForHash, currentBridgePayload.originalModel); err != nil {
					return err
				}
			}
			if hooks != nil && hooks.BeforeTurn != nil {
				if err := hooks.BeforeTurn(turn); err != nil {
					return err
				}
			}
			if turnState != "" && c != nil && c.Request != nil {
				c.Request.Header.Set(openAIWSTurnStateHeader, turnState)
			}
			if c != nil && sessionHash != "" {
				c.Set(openAIWSIngressSessionHashContextKey, sessionHash)
			}
			// 剥离本会话已知失效的加密项，阻断同一失效密文随历史反复触发上游拒绝。
			// 历史序列须同步剥离，否则与已剥离的当前 input 项错位，prefix 复用失配。
			if invalidDigests := s.sessionInvalidEncryptedContentDigests(groupID, sessionHash); len(invalidDigests) > 0 {
				strippedPayload, strippedCount := s.stripSessionInvalidEncryptedContentLogged(
					currentBridgePayload.payloadRaw, invalidDigests, "ingress_ws_http_bridge_invalid_encrypted_lineage_strip", account.ID, turn,
				)
				if strippedCount > 0 {
					currentBridgePayload.payloadRaw = strippedPayload
					currentBridgePayload.payloadBytes = len(strippedPayload)
				}
				if bridgeReplayState.exists && !bridgeReplayState.unavailable {
					stripped, count := stripOpenAIInvalidEncryptedContentFromReplayItems(bridgeReplayState.items, invalidDigests)
					if count > 0 {
						bridgeReplayState = newOpenAIWSReplayInputState(stripped, true)
					}
				}
				if bridgeAccountFailoverState.exists && !bridgeAccountFailoverState.unavailable {
					stripped, count := stripOpenAIInvalidEncryptedContentFromReplayItems(bridgeAccountFailoverState.items, invalidDigests)
					if count > 0 {
						bridgeAccountFailoverState = newOpenAIWSReplayInputState(stripped, true)
					}
				}
			}
			bridgePayloadRaw := currentBridgePayload.payloadRaw
			bridgePayloadBytes := currentBridgePayload.payloadBytes
			toolOutputCoverage := AnalyzeToolCallOutputContextCoverageBytes(currentBridgePayload.payloadRaw)
			needsBridgeReplay := currentBridgePayload.previousResponseID != "" ||
				(toolOutputCoverage.HasFunctionCallOutput && !toolOutputCoverage.ContextCoversAllCallIDs)
			// 一次解析当前 input，正常 replay 与 account-failover 两份序列共享
			// 同一批正文，同时沿用 Plus 的有界 ReplayInputState。
			currentInputItems, currentInputExists, currentInputErr := openAIWSExtractNormalizedInputSequence(
				currentBridgePayload.payloadRaw,
			)
			if currentInputErr != nil {
				return fmt.Errorf("extract websocket http bridge replay input: %w", currentInputErr)
			}
			turnReplayState, replayInputErr := buildOpenAIWSReplayInputState(
				bridgeReplayState,
				currentInputItems,
				currentInputExists,
				needsBridgeReplay,
				replayLimits,
			)
			if replayInputErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_replay_input_limit account_id=%d turn=%d mode=http_bridge scope=continuation action=disable_replay cause=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(replayInputErr.Error(), openAIWSLogValueMaxLen),
				)
			}
			if needsBridgeReplay && turnReplayState.unavailable {
				return newOpenAIWSReplayUnavailableCloseError(replayInputErr)
			}
			// 两份状态共享不可变的 RawMessage 正文，但使用独立的切片头数组，
			// 避免后续 append 互相覆盖。
			failoverCurrentItems := append([]json.RawMessage(nil), currentInputItems...)
			turnAccountFailoverState, failoverInputErr := buildOpenAIWSReplayInputState(
				bridgeAccountFailoverState,
				failoverCurrentItems,
				currentInputExists,
				needsBridgeReplay,
				replayLimits,
			)
			if failoverInputErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_replay_input_limit account_id=%d turn=%d mode=http_bridge scope=account_failover action=disable_replay cause=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(failoverInputErr.Error(), openAIWSLogValueMaxLen),
				)
			}
			if needsBridgeReplay && turnReplayState.exists {
				updatedPayload, setInputErr := setOpenAIWSPayloadInputSequence(
					currentBridgePayload.payloadRaw,
					turnReplayState.items,
					true,
				)
				if setInputErr != nil {
					return fmt.Errorf("set websocket http bridge replay input: %w", setInputErr)
				}
				bridgePayloadRaw = updatedPayload
				bridgePayloadBytes = len(updatedPayload)
				logOpenAIWSModeInfo(
					"ingress_ws_http_bridge_replay_input account_id=%d turn=%d input_items=%d previous_response_id_present=%v has_tool_output=%v",
					account.ID,
					turn,
					len(turnReplayState.items),
					currentBridgePayload.previousResponseID != "",
					openAIWSRawPayloadHasToolCallOutput(currentBridgePayload.payloadRaw),
				)
			}
			grokCacheIdentity := ""
			if account.Platform == PlatformGrok {
				grokCacheIdentity, err = resolveGrokWSCacheIdentity(
					c,
					account,
					grokCacheSeedPayload,
					currentBridgePayload.payloadRaw,
					currentBridgePayload.originalModel,
				)
				if err != nil {
					return fmt.Errorf("resolve Grok websocket cache identity: %w", err)
				}
			}
			result, bridgeErr := s.proxyOpenAIWSHTTPBridgeTurn(
				ctx,
				c,
				account,
				token,
				bridgePayloadRaw,
				bridgePayloadBytes,
				currentBridgePayload.originalModel,
				currentBridgePayload.imageBillingModel,
				currentBridgePayload.imageSizeTier,
				currentBridgePayload.imageInputSize,
				grokCacheIdentity,
				turn,
				writeClientMessage,
			)
			if bridgeErr != nil && isOpenAIWSSessionPreempted(ctx) {
				return errOpenAIWSSessionPreempted
			}
			if hooks != nil && hooks.AfterTurn != nil {
				hooks.AfterTurn(turn, result, bridgeErr)
			}
			if bridgeErr != nil {
				var failoverErr *UpstreamFailoverError
				if turn > 1 && errors.As(bridgeErr, &failoverErr) && failoverErr != nil {
					retryPayload, retrySafe, retryPayloadErr := buildOpenAIWSCurrentTurnRetryPayload(
						currentBridgePayload.accountIdentitySourceRaw,
						turnAccountFailoverState.items,
						turnAccountFailoverState.exists && !turnAccountFailoverState.unavailable,
						currentBridgePayload.originalModel,
					)
					if retryPayloadErr != nil {
						return fmt.Errorf("build websocket current-turn failover payload: %w", retryPayloadErr)
					}
					if !retrySafe {
						retryPayload = nil
					}
					return newOpenAIWSCurrentTurnFailoverError(bridgeErr, retryPayload)
				}
				return bridgeErr
			}
			if result == nil {
				return errors.New("websocket http bridge turn result is nil")
			}
			bridgeReplayState = turnReplayState
			if result.wsReplayInputExists {
				var appendErr error
				bridgeReplayState, appendErr = appendOpenAIWSReplayInputState(
					bridgeReplayState,
					result.wsReplayInput,
					replayLimits,
				)
				if appendErr != nil {
					logOpenAIWSModeInfo(
						"ingress_ws_replay_input_limit account_id=%d turn=%d mode=http_bridge scope=response action=disable_replay cause=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(appendErr.Error(), openAIWSLogValueMaxLen),
					)
				}
			}
			bridgeAccountFailoverState = turnAccountFailoverState
			if len(result.wsAccountFailoverReplayInput) > 0 {
				var appendErr error
				bridgeAccountFailoverState, appendErr = appendOpenAIWSReplayInputState(
					bridgeAccountFailoverState,
					result.wsAccountFailoverReplayInput,
					replayLimits,
				)
				if appendErr != nil {
					logOpenAIWSModeInfo(
						"ingress_ws_replay_input_limit account_id=%d turn=%d mode=http_bridge scope=account_failover_response action=disable_replay cause=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(appendErr.Error(), openAIWSLogValueMaxLen),
					)
				}
			}
			if bridgeTurnState := strings.TrimSpace(result.ResponseHeaders.Get(openAIWSTurnStateHeader)); bridgeTurnState != "" {
				// Follow-up turns on this bridge retain their own upstream state;
				// publishing it by session hash would leak it to independent bridges.
				turnState = bridgeTurnState
				if turnStateOwnerIsolation {
					// 下一轮会把该值写回本连接请求头；handler 账号 failover 复用同一 gin 上下文时，
					// 它会被当作客户端回带值读取。记入本连接账号，换号后才能被入口隔离识别并丢弃。
					s.noteOpenAICodexTurnStateProvenance(c, account)
				}
			}
			responseID := strings.TrimSpace(result.RequestID)
			if responseID != "" && stateStore != nil {
				ttl := s.openAIWSResponseStickyTTL()
				logOpenAIWSBindResponseAccountWarn(groupID, account.ID, responseID, stateStore.BindResponseAccount(ctx, groupID, responseID, account.ID, ttl))
			}
			nextClientMessage, readErr := readClientMessage()
			if readErr != nil {
				if isOpenAIWSSessionPreempted(ctx) {
					return errOpenAIWSSessionPreempted
				}
				if isOpenAIWSClientDisconnectError(readErr) {
					closeStatus, closeReason := summarizeOpenAIWSReadCloseError(readErr)
					logOpenAIWSModeInfo(
						"ingress_ws_http_bridge_client_closed account_id=%d close_status=%s close_reason=%s",
						account.ID,
						closeStatus,
						truncateOpenAIWSLogValue(closeReason, openAIWSHeaderValueMaxLen),
					)
					return nil
				}
				return fmt.Errorf("read client websocket request: %w", readErr)
			}
			nextPayload, parseErr := parseClientPayload(turn+1, nextClientMessage)
			if parseErr != nil {
				return parseErr
			}
			currentBridgePayload = nextPayload
		}
	}

	firstRoutingFields := gjson.GetManyBytes(firstPayload.payloadRaw, "model", "service_tier")
	var firstRoutingHint officialegress.CodexRoutingHintFacts
	if officialRuntime != nil {
		firstRoutingHint, err = officialegress.ParseOfficialCodexRoutingHintFacts(
			officialCodexEndpointResponsesWS,
			firstPayload.payloadRaw,
		)
		if err != nil {
			return NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"official egress websocket routing hint validation failed",
				err,
			)
		}
	}
	wsHeaders, _, buildHdrErr := s.buildOpenAIWSHeaders(
		ctx,
		c,
		account,
		token,
		wsDecision,
		isCodexCLI,
		turnState,
		strings.TrimSpace(c.GetHeader(openAIWSTurnMetadataHeader)),
		firstPayload.promptCacheKey,
		firstRoutingFields[0].String(),
		firstRoutingFields[1].String(),
	)
	if buildHdrErr != nil {
		return fmt.Errorf("build ws headers: %w", buildHdrErr)
	}
	baseAcquireReq := openAIWSAcquireRequest{
		Account:     account,
		WSURL:       wsURL,
		Headers:     wsHeaders,
		RoutingHint: firstRoutingHint,
		SinkID:      runtimeSinkID,
		HeadersFactory: func(factoryCtx context.Context, headers http.Header) (http.Header, error) {
			return s.refreshOpenAIAgentIdentityHeaders(factoryCtx, account, headers)
		},
		ProxyURL: func() string {
			if account.ProxyID != nil && account.Proxy != nil {
				return account.Proxy.URL()
			}
			return ""
		}(),
		ForceNewConn: false,
	}
	var officialInvocation *OpenAIForwardInvocationPlan
	if officialRuntime != nil {
		officialInvocation, err = officialCodexResponseForwardPlanForHolder(
			ctx,
			officialCodexBundleHolderForGin(c),
			officialCodexResponseForwardPlanInput{
				Runtime: officialRuntime, Account: account, PrimarySinkID: runtimeSinkID,
				InvocationID: invocationID, ProxyURL: baseAcquireReq.ProxyURL,
				PolicyID:        "changeset3.responses.ws",
				PolicySource:    "service.openAIWSForwardIngress",
				FallbackSinkIDs: []officialegress.SinkID{officialegress.SinkCodexResponsesWSHTTPBridge},
				AttemptBudget:   officialCodexIngressForwardAttemptBudget,
			},
		)
		if err != nil {
			return err
		}
	}
	pool := s.getOpenAIWSConnPool()
	if pool == nil {
		return errors.New("openai ws conn pool is nil")
	}

	logOpenAIWSModeInfo(
		"ingress_ws_protocol_confirm account_id=%d account_type=%s transport=%s ws_host=%s ws_path=%s ws_mode=%s store_disabled=%v has_session_hash=%v has_previous_response_id=%v",
		account.ID,
		account.Type,
		normalizeOpenAIWSLogValue(string(wsDecision.Transport)),
		wsHost,
		wsPath,
		normalizeOpenAIWSLogValue(ingressMode),
		storeDisabled,
		sessionHash != "",
		firstPayload.previousResponseID != "",
	)

	if debugEnabled {
		logOpenAIWSModeDebug(
			"ingress_ws_start account_id=%d account_type=%s transport=%s ws_host=%s preferred_conn_id=%s has_session_hash=%v has_previous_response_id=%v store_disabled=%v",
			account.ID,
			account.Type,
			normalizeOpenAIWSLogValue(string(wsDecision.Transport)),
			wsHost,
			truncateOpenAIWSLogValue(preferredConnID, openAIWSIDValueMaxLen),
			sessionHash != "",
			firstPayload.previousResponseID != "",
			storeDisabled,
		)
	}
	if firstPayload.previousResponseID != "" {
		firstPreviousResponseIDKind := ClassifyOpenAIPreviousResponseIDKind(firstPayload.previousResponseID)
		logOpenAIWSModeInfo(
			"ingress_ws_continuation_probe account_id=%d turn=%d previous_response_id=%s previous_response_id_kind=%s preferred_conn_id=%s session_hash=%s header_session_id=%s header_conversation_id=%s has_turn_state=%v turn_state_len=%d has_prompt_cache_key=%v store_disabled=%v",
			account.ID,
			1,
			truncateOpenAIWSLogValue(firstPayload.previousResponseID, openAIWSIDValueMaxLen),
			normalizeOpenAIWSLogValue(firstPreviousResponseIDKind),
			truncateOpenAIWSLogValue(preferredConnID, openAIWSIDValueMaxLen),
			truncateOpenAIWSLogValue(sessionHash, 12),
			openAIWSHeaderValueForLog(baseAcquireReq.Headers, "session_id"),
			openAIWSHeaderValueForLog(baseAcquireReq.Headers, "conversation_id"),
			turnState != "",
			len(turnState),
			firstPayload.promptCacheKey != "",
			storeDisabled,
		)
	}

	acquireTimeout := s.openAIWSAcquireTimeout()
	if acquireTimeout <= 0 {
		acquireTimeout = 30 * time.Second
	}

	agentTaskRecoveryTried := false
	forceFreshConn := false
	var acquireTurnLease func(int, string, bool) (openAIWSLeaseSession, error)
	acquireTurnLease = func(turn int, preferred string, forcePreferredConn bool) (openAIWSLeaseSession, error) {
		req := cloneOpenAIWSAcquireRequest(baseAcquireReq)
		req.PreferredConnID = strings.TrimSpace(preferred)
		req.ForcePreferredConn = forcePreferredConn
		// dedicated 模式下每次获取均新建连接；复用连接发生读写错误后的
		// 单次重试也必须新建，不能继续从池中挑选另一条可能同样失效的空闲连接。
		req.ForceNewConn = dedicatedMode || forceFreshConn
		acquireCtx, acquireCancel := context.WithTimeout(ctx, acquireTimeout)
		var lease openAIWSLeaseSession
		var acquireErr error
		if officialInvocation != nil {
			lease, acquireErr = officialInvocation.AcquireWebSocketPool(
				acquireCtx, pool, req, officialCodexEndpointResponsesWS,
			)
		} else {
			lease, acquireErr = pool.Acquire(acquireCtx, req)
		}
		acquireCancel()
		var dialErr *openAIWSDialError
		if acquireErr != nil && s.isAgentIdentityAccount(ctx, account) && errors.As(acquireErr, &dialErr) && isAgentIdentityTaskInvalidWSDialError(dialErr) && !agentTaskRecoveryTried {
			agentTaskRecoveryTried = true
			if recoveryErr := s.recoverAgentIdentityTask(ctx, account, account.GetCredential("task_id")); recoveryErr != nil {
				return nil, fmt.Errorf("agent identity task recovery failed: %w", recoveryErr)
			}
			return acquireTurnLease(turn, preferred, forcePreferredConn)
		}
		if acquireErr != nil {
			if isOpenAIWSSessionPreempted(ctx) {
				return nil, errOpenAIWSSessionPreempted
			}
			canonicalModel := canonicalOpenAIAccountSchedulingModel(account, ingressSessionOriginalModel)
			s.handleOpenAIWSDialTransientFailure(ctx, account, canonicalModel, acquireErr)
			dialStatus, dialClass, dialCloseStatus, dialCloseReason, dialRespServer, dialRespVia, dialRespCFRay, dialRespReqID := summarizeOpenAIWSDialError(acquireErr)
			dialRespCFMitigated := openAIWSHeaderValueForLog(openAIWSDialResponseHeaders(acquireErr), "cf-mitigated")
			dialRespBodySummary := openAIWSCloudflareResponseBodySummary(acquireErr)
			logOpenAIWSModeInfo(
				"ingress_ws_upstream_acquire_fail account_id=%d turn=%d reason=%s dial_status=%d dial_class=%s dial_close_status=%s dial_close_reason=%s dial_resp_server=%s dial_resp_via=%s dial_resp_cf_ray=%s dial_resp_cf_mitigated=%s dial_resp_x_request_id=%s dial_resp_body_summary=%s cause=%s preferred_conn_id=%s force_preferred_conn=%v ws_host=%s ws_path=%s proxy_enabled=%v",
				account.ID,
				turn,
				normalizeOpenAIWSLogValue(classifyOpenAIWSAcquireError(acquireErr)),
				dialStatus,
				dialClass,
				dialCloseStatus,
				truncateOpenAIWSLogValue(dialCloseReason, openAIWSHeaderValueMaxLen),
				dialRespServer,
				dialRespVia,
				dialRespCFRay,
				dialRespCFMitigated,
				dialRespReqID,
				dialRespBodySummary,
				truncateOpenAIWSLogValue(acquireErr.Error(), openAIWSLogValueMaxLen),
				truncateOpenAIWSLogValue(preferred, openAIWSIDValueMaxLen),
				forcePreferredConn,
				wsHost,
				wsPath,
				account.ProxyID != nil && account.Proxy != nil,
			)
			var dialErr *openAIWSDialError
			if errors.As(acquireErr, &dialErr) && dialErr != nil && dialErr.StatusCode == http.StatusTooManyRequests {
				s.persistOpenAIWSRateLimitSignal(ctx, account, dialErr.ResponseHeaders, nil, "rate_limit_exceeded", "rate_limit_error", strings.TrimSpace(acquireErr.Error()), canonicalModel)
				return nil, s.newOpenAIWSRateLimitFailoverError(account, dialErr.ResponseHeaders, nil, acquireErr.Error())
			}
			if errors.Is(acquireErr, errOpenAIWSPreferredConnUnavailable) {
				return nil, NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"upstream continuation connection is unavailable; please restart the conversation",
					acquireErr,
				)
			}
			if errors.Is(acquireErr, context.DeadlineExceeded) || errors.Is(acquireErr, errOpenAIWSConnQueueFull) {
				return nil, NewOpenAIWSClientCloseError(
					coderws.StatusTryAgainLater,
					"upstream websocket is busy, please retry later",
					acquireErr,
				)
			}
			return nil, acquireErr
		}
		forceFreshConn = false
		connID := strings.TrimSpace(lease.ConnID())
		handshakeTurnState := replaceOpenAIWSTurnStateFromLease(lease)
		turnState = handshakeTurnState
		if handshakeTurnState != "" && c != nil {
			c.Header(http.CanonicalHeaderKey(openAIWSTurnStateHeader), handshakeTurnState)
			if turnStateOwnerIsolation {
				// 记录本连接账号铸造了该 turn-state，供后续 failover 时识别跨账号回带值。
				s.noteOpenAICodexTurnStateProvenance(c, account)
			}
		}
		leaseIdleBefore, leaseAgeBefore, leaseUpstreamPings := openAIWSLeaseDiagnosticsOf(lease)
		logOpenAIWSModeInfo(
			"ingress_ws_upstream_connected account_id=%d turn=%d conn_id=%s conn_reused=%v conn_idle_ms=%d conn_age_ms=%d upstream_pings=%d conn_pick_ms=%d queue_wait_ms=%d preferred_conn_id=%s",
			account.ID,
			turn,
			truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
			lease.Reused(),
			leaseIdleBefore.Milliseconds(),
			leaseAgeBefore.Milliseconds(),
			leaseUpstreamPings,
			lease.ConnPickDuration().Milliseconds(),
			lease.QueueWaitDuration().Milliseconds(),
			truncateOpenAIWSLogValue(preferred, openAIWSIDValueMaxLen),
		)
		return lease, nil
	}

	var rejectedFieldRetryState *openAIResponsesRejectedFieldRetryState
	sendAndRelay := func(turn int, lease openAIWSLeaseSession, payload []byte, payloadBytes int, originalModel string, imageBillingModel string, imageSizeTier string, imageInputSize string, relayToClient bool, requestedReasoningEffort *string) (*OpenAIForwardResult, error) {
		responseModelObserver := &upstreamResponseModelObserver{}
		if lease == nil {
			return nil, errors.New("upstream websocket lease is nil")
		}
		turnStart := time.Now()
		wroteDownstream := false
		if err := lease.WriteSemanticJSONWithContextTimeout(ctx, json.RawMessage(payload), s.openAIWSWriteTimeout()); err != nil {
			return nil, wrapOpenAIWSIngressTurnError(
				"write_upstream",
				fmt.Errorf("write upstream websocket request: %w", err),
				false,
			)
		}
		if debugEnabled {
			logOpenAIWSModeDebug(
				"ingress_ws_turn_request_sent account_id=%d turn=%d conn_id=%s payload_bytes=%d",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
				payloadBytes,
			)
		}

		responseID := ""
		usage := OpenAIUsage{}
		imageCounter := newOpenAIImageOutputCounter()
		var firstTokenMs *int
		reqStream := openAIWSPayloadBoolFromRaw(payload, "stream", true)
		turnPreviousResponseID := openAIWSPayloadStringFromRaw(payload, "previous_response_id")
		turnPreviousResponseIDKind := ClassifyOpenAIPreviousResponseIDKind(turnPreviousResponseID)
		turnPromptCacheKey := openAIWSPayloadStringFromRaw(payload, "prompt_cache_key")
		turnStoreDisabled := s.isOpenAIWSStoreDisabledInRequestRaw(payload, account)
		turnHasFunctionCallOutput := openAIWSRawPayloadHasToolCallOutput(payload)
		eventCount := 0
		tokenEventCount := 0
		terminalEventCount := 0
		replayCollector := &openAIWSToolCallReplayCollector{}
		firstEventType := ""
		lastEventType := ""
		needModelReplace := false
		clientDisconnected := false
		mappedModel := ""
		var mappedModelBytes []byte
		if originalModel != "" {
			mappedModel = strings.TrimSpace(gjson.GetBytes(payload, "model").String())
			if mappedModel == "" {
				mappedModel = normalizeOpenAIModelForUpstream(account, account.GetMappedModel(originalModel))
			}
			needModelReplace = mappedModel != "" && mappedModel != originalModel
			if needModelReplace {
				mappedModelBytes = []byte(mappedModel)
			}
		}
		for {
			upstreamMessage, readErr := lease.ReadMessageWithContextTimeout(ctx, s.openAIWSReadTimeout())
			if readErr != nil {
				lease.MarkBroken()
				return nil, wrapOpenAIWSIngressTurnError(
					"read_upstream",
					fmt.Errorf("read upstream websocket event: %w", readErr),
					wroteDownstream,
				)
			}
			if normalized, changed := normalizeCompletedImageGenerationStatus(upstreamMessage); changed {
				upstreamMessage = normalized
			}

			eventType, eventResponseID, _ := parseOpenAIWSEventEnvelope(upstreamMessage)
			responseModelObserver.ObserveOpenAI(upstreamMessage, eventType)
			if responseID == "" && eventResponseID != "" {
				responseID = eventResponseID
			}
			// 官方从流内 response.metadata 事件取 turn-state，握手响应头在官方 CLI 里
			// 是死代码。这里以事件流为准更新，握手头仅作连接建立时的初值回退。
			if officialEgressEnabled {
				if eventTurnState := extractOpenAIWSTurnStateFromUpstreamEvent(upstreamMessage); eventTurnState != "" {
					turnState = eventTurnState
					if turnStateOwnerIsolation {
						// 事件流下发的值随 response.metadata 转发给下游，客户端可能在新连接回带；
						// 与握手下发同样记入本连接账号，换号后才能被入口隔离识别并丢弃。
						s.noteOpenAICodexTurnStateProvenance(c, account)
					}
				}
			}
			if eventType != "" {
				eventCount++
				if firstEventType == "" {
					firstEventType = eventType
				}
				lastEventType = eventType
			}
			if openAIWSMessageShouldParseUsage(eventType, upstreamMessage) {
				parseOpenAIWSResponseUsageFromCompletedEvent(upstreamMessage, &usage)
			}
			if eventType == "error" || eventType == "response.failed" {
				markOpenAICyberPolicyEvent(c, upstreamMessage, http.StatusOK, &usage)
			}
			if eventType == "error" {
				s.handleOpenAIWSErrorEventTransientFailure(ctx, account, mappedModel, lease.HandshakeHeaders(), upstreamMessage)
				errCodeRaw, errTypeRaw, errMsgRaw := parseOpenAIWSErrorEventFields(upstreamMessage)
				statusCode := openAIWSRejectedFieldRetryHTTPStatus(upstreamMessage)
				if !wroteDownstream && statusCode == http.StatusBadRequest && rejectedFieldRetryState != nil {
					retryBody, retryReason, changed, retryErr := normalizeOpenAIResponsesRejectedFieldRetryBody(
						statusCode,
						payload,
						upstreamMessage,
					)
					if retryErr != nil {
						return nil, fmt.Errorf("normalize websocket rejected field retry: %w", retryErr)
					}
					if changed && rejectedFieldRetryState.Allow(retryBody) {
						logOpenAIWSModeInfo(
							"ingress_ws_rejected_field_retry account_id=%d turn=%d conn_id=%s reason=%s",
							account.ID,
							turn,
							truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
							truncateOpenAIWSLogValue(retryReason, openAIWSLogValueMaxLen),
						)
						return nil, &openAIWSRejectedFieldRetryError{
							body:   append([]byte(nil), retryBody...),
							reason: retryReason,
						}
					}
				}
				s.persistOpenAIWSRateLimitSignal(ctx, account, lease.HandshakeHeaders(), upstreamMessage, errCodeRaw, errTypeRaw, errMsgRaw, mappedModel)
				fallbackReason, _ := classifyOpenAIWSErrorEventFromRaw(errCodeRaw, errTypeRaw, errMsgRaw)
				if fallbackReason == openAIWSFallbackReasonInvalidEncryptedContent {
					// 记录被上游拒绝的密文摘要；错误照旧透传，下一轮进场时按摘要预剥离。
					if digests := collectOpenAIEncryptedContentDigestsRaw(payload); len(digests) > 0 {
						s.markOpenAIWSInvalidEncryptedContentLineage(groupID, sessionHash, digests)
						logOpenAIWSModeInfo(
							"ingress_ws_invalid_encrypted_lineage_mark account_id=%d turn=%d digests=%d",
							account.ID,
							turn,
							len(digests),
						)
					}
				}
				errCode, errType, errMessage := summarizeOpenAIWSErrorEventFieldsFromRaw(errCodeRaw, errTypeRaw, errMsgRaw)
				recoverablePrevNotFound := fallbackReason == openAIWSIngressStagePreviousResponseNotFound &&
					turnPreviousResponseID != "" &&
					!turnHasFunctionCallOutput &&
					s.openAIWSIngressPreviousResponseRecoveryEnabled() &&
					!wroteDownstream
				if recoverablePrevNotFound {
					// 可恢复场景使用非 error 关键字日志，避免被 LegacyPrintf 误判为 ERROR 级别。
					logOpenAIWSModeInfo(
						"ingress_ws_prev_response_recoverable account_id=%d turn=%d conn_id=%s idx=%d reason=%s code=%s type=%s message=%s previous_response_id=%s previous_response_id_kind=%s response_id=%s store_disabled=%v has_prompt_cache_key=%v",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
						eventCount,
						truncateOpenAIWSLogValue(fallbackReason, openAIWSLogValueMaxLen),
						errCode,
						errType,
						errMessage,
						truncateOpenAIWSLogValue(turnPreviousResponseID, openAIWSIDValueMaxLen),
						normalizeOpenAIWSLogValue(turnPreviousResponseIDKind),
						truncateOpenAIWSLogValue(responseID, openAIWSIDValueMaxLen),
						turnStoreDisabled,
						turnPromptCacheKey != "",
					)
				} else {
					logOpenAIWSModeInfo(
						"ingress_ws_error_event account_id=%d turn=%d conn_id=%s idx=%d fallback_reason=%s err_code=%s err_type=%s err_message=%s previous_response_id=%s previous_response_id_kind=%s response_id=%s store_disabled=%v has_prompt_cache_key=%v",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
						eventCount,
						truncateOpenAIWSLogValue(fallbackReason, openAIWSLogValueMaxLen),
						errCode,
						errType,
						errMessage,
						truncateOpenAIWSLogValue(turnPreviousResponseID, openAIWSIDValueMaxLen),
						normalizeOpenAIWSLogValue(turnPreviousResponseIDKind),
						truncateOpenAIWSLogValue(responseID, openAIWSIDValueMaxLen),
						turnStoreDisabled,
						turnPromptCacheKey != "",
					)
				}
				// previous_response_not_found 在 ingress 模式支持单次恢复重试：
				// 不把该 error 直接下发客户端，而是由上层去掉 previous_response_id 后重放当前 turn。
				if recoverablePrevNotFound {
					lease.MarkBroken()
					errMsg := strings.TrimSpace(errMsgRaw)
					if errMsg == "" {
						errMsg = "previous response not found"
					}
					return nil, wrapOpenAIWSIngressTurnError(
						openAIWSIngressStagePreviousResponseNotFound,
						errors.New(errMsg),
						false,
					)
				}
				if !wroteDownstream && isOpenAIWSRateLimitError(errCodeRaw, errTypeRaw, errMsgRaw) {
					lease.MarkBroken()
					return nil, s.newOpenAIWSRateLimitFailoverError(account, lease.HandshakeHeaders(), upstreamMessage, errMsgRaw)
				}
			}
			isTokenEvent := isOpenAIWSTokenEvent(eventType)
			if isTokenEvent {
				tokenEventCount++
			}
			isTerminalEvent := isOpenAIWSTerminalEvent(eventType)
			if isTerminalEvent {
				terminalEventCount++
			}
			if firstTokenMs == nil && isTokenEvent {
				ms := int(time.Since(turnStart).Milliseconds())
				firstTokenMs = &ms
			}
			imageCounter.AddSSEData(upstreamMessage)

			if eventType == "response.failed" {
				if hit, code, msg := detectOpenAICyberPolicy(upstreamMessage); hit {
					MarkOpsCyberPolicy(c, CyberPolicyMark{
						Code:           code,
						Message:        msg,
						Body:           truncateString(string(upstreamMessage), 4096),
						UpstreamStatus: http.StatusOK,
						UpstreamInTok:  usage.InputTokens,
						UpstreamOutTok: usage.OutputTokens,
					})
				}
			}
			if relayToClient && !clientDisconnected {
				if needModelReplace && len(mappedModelBytes) > 0 && openAIWSEventMayContainModel(eventType) && bytes.Contains(upstreamMessage, mappedModelBytes) {
					upstreamMessage = replaceOpenAIWSMessageModel(upstreamMessage, mappedModel, originalModel)
				}
				if openAIWSEventMayContainToolCalls(eventType) && openAIWSMessageLikelyContainsToolCalls(upstreamMessage) {
					if corrected, changed := s.toolCorrector.CorrectToolCallsInSSEBytes(upstreamMessage); changed {
						upstreamMessage = corrected
					}
				}
				replayCollector.AddEvent(eventType, upstreamMessage)
				// 客户端写出副本改写容量降载码：Codex 对 error/response.failed 中的
				// server_is_overloaded / slow_down 判致命并终止会话，改写后走客户端
				// 内置退避重试。HTTP/SSE（openai_gateway_response_handling.go）与
				// http_bridge（openai_ws_http_bridge.go）两条路径早已这么做，
				// ctx_pool 的 ingress 直写路径是唯一漏掉的一条 —— 同一个上游降载
				// 事件在这里会让会话就地终止，切到 http_bridge 却能正常退避重试。
				//
				// 必须写进独立变量而不是原地改 upstreamMessage：下面的
				// markOpenAIWSClientVisibleFailure 与 handleOpenAIWSTerminalTransientFailure
				// 仍要按未改写的原始 payload 判定账号状态，这正是
				// sanitizeOpenAICapacityShedErrorCodeForClient 注释里写明的前提。
				clientMessage := upstreamMessage
				if eventType == "error" || eventType == "response.failed" {
					if rewritten, changed := sanitizeOpenAICapacityShedErrorCodeForClient(clientMessage); changed {
						clientMessage = rewritten
					}
				}
				if err := writeClientMessage(clientMessage); err != nil {
					if isOpenAIWSClientDisconnectError(err) {
						clientDisconnected = true
						closeStatus, closeReason := summarizeOpenAIWSReadCloseError(err)
						logOpenAIWSModeInfo(
							"ingress_ws_client_disconnected_drain account_id=%d turn=%d conn_id=%s close_status=%s close_reason=%s",
							account.ID,
							turn,
							truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
							closeStatus,
							truncateOpenAIWSLogValue(closeReason, openAIWSHeaderValueMaxLen),
						)
					} else {
						return nil, wrapOpenAIWSIngressTurnError(
							"write_client",
							fmt.Errorf("write client websocket event: %w", err),
							wroteDownstream,
						)
					}
				} else {
					wroteDownstream = true
					markOpenAIWSClientVisibleFailure(c, eventType, upstreamMessage)
				}
			}
			if isTerminalEvent {
				terminalEvent := s.handleOpenAIWSTerminalTransientFailure(ctx, account, mappedModel, lease.HandshakeHeaders(), upstreamMessage)
				// 客户端已断连时，上游连接的 session 状态不可信，标记 broken 避免回池复用。
				if clientDisconnected {
					lease.MarkBroken()
				}
				firstTokenMsValue := -1
				if firstTokenMs != nil {
					firstTokenMsValue = *firstTokenMs
				}
				if debugEnabled {
					logOpenAIWSModeDebug(
						"ingress_ws_turn_completed account_id=%d turn=%d conn_id=%s response_id=%s duration_ms=%d events=%d token_events=%d terminal_events=%d first_event=%s last_event=%s first_token_ms=%d client_disconnected=%v",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(lease.ConnID(), openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(responseID, openAIWSIDValueMaxLen),
						time.Since(turnStart).Milliseconds(),
						eventCount,
						tokenEventCount,
						terminalEventCount,
						truncateOpenAIWSLogValue(firstEventType, openAIWSLogValueMaxLen),
						truncateOpenAIWSLogValue(lastEventType, openAIWSLogValueMaxLen),
						firstTokenMsValue,
						clientDisconnected,
					)
				}
				imageCount := imageCounter.Count()
				result := &OpenAIForwardResult{
					RequestID:                     responseID,
					Usage:                         usage,
					Model:                         originalModel,
					UpstreamModel:                 mappedModel,
					UpstreamResponseModel:         responseModelObserver.Model(),
					UpstreamResponseModelConflict: responseModelObserver.Conflict(),
					UpstreamResponseServiceTier:   responseModelObserver.ServiceTier(),
					ServiceTier:                   resolvedOpenAIUpstreamServiceTierFromObserver(responseModelObserver, extractOpenAIServiceTierFromBody(payload)),
					ReasoningEffort:               ApplyThinkingEnabledFallback(extractOpenAIReasoningEffortFromBody(payload, mappedModel, originalModel), payload, mappedModel),
					RequestedReasoningEffort:      requestedReasoningEffort,
					Stream:                        reqStream,
					OpenAIWSMode:                  true,
					UpstreamTerminalEvent:         terminalEvent,
					ResponseHeaders:               lease.HandshakeHeaders(),
					Duration:                      time.Since(turnStart),
					FirstTokenMs:                  firstTokenMs,
				}
				if replayInput := replayCollector.Items(); len(replayInput) > 0 {
					result.wsReplayInput = replayInput
					result.wsReplayInputExists = true
				}
				if imageCount > 0 {
					result.ImageCount = imageCount
					result.ImageSize = imageSizeTier
					result.ImageInputSize = imageInputSize
					result.ImageOutputSizes = imageCounter.Sizes()
					result.BillingModel = imageBillingModel
				}
				return result, nil
			}
		}
	}

	currentPayload := firstPayload.payloadRaw
	// 客户端原始载荷：官方出站 WS 帧收口以它为语义基准；准入 hooks 也必须在上游模型映射
	// 之前看到客户端模型候选（上游 13fcfc0f3 的 currentClientPayload 与此同值，统一用本变量）。
	currentOriginalPayload := firstPayload.rawForHash
	currentOriginalModel := firstPayload.originalModel
	currentImageBillingModel := firstPayload.imageBillingModel
	currentImageSizeTier := firstPayload.imageSizeTier
	currentImageInputSize := firstPayload.imageInputSize
	currentPayloadBytes := firstPayload.payloadBytes
	currentRequestedReasoningEffort := firstPayload.requestedReasoningEffort
	isStrictAffinityTurn := func(payload []byte) bool {
		if !storeDisabled {
			return false
		}
		return strings.TrimSpace(openAIWSPayloadStringFromRaw(payload, "previous_response_id")) != ""
	}
	var sessionLease openAIWSLeaseSession
	sessionConnID := ""
	pinnedSessionConnID := ""
	unpinSessionConn := func(connID string) {
		connID = strings.TrimSpace(connID)
		if connID == "" || pinnedSessionConnID != connID {
			return
		}
		pool.UnpinConn(account.ID, connID)
		pinnedSessionConnID = ""
	}
	pinSessionConn := func(connID string) {
		if !storeDisabled {
			return
		}
		connID = strings.TrimSpace(connID)
		if connID == "" || pinnedSessionConnID == connID {
			return
		}
		if pinnedSessionConnID != "" {
			pool.UnpinConn(account.ID, pinnedSessionConnID)
			pinnedSessionConnID = ""
		}
		if pool.PinConn(account.ID, connID) {
			pinnedSessionConnID = connID
		}
	}
	// lastTurnClean 标记最后一轮 sendAndRelay 是否正常完成（收到终端事件且客户端未断连）。
	// 所有异常路径（读写错误、error 事件、客户端断连）已在各自分支或上层（L3403）中 MarkBroken，
	// 因此 releaseSessionLease 中只需在非正常结束时 MarkBroken。
	lastTurnClean := false
	releaseSessionLease := func() {
		if sessionLease == nil {
			return
		}
		if !lastTurnClean {
			sessionLease.MarkBroken()
		}
		unpinSessionConn(sessionConnID)
		sessionLease.Release()
		if debugEnabled {
			logOpenAIWSModeDebug(
				"ingress_ws_upstream_released account_id=%d conn_id=%s",
				account.ID,
				truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
			)
		}
	}
	defer releaseSessionLease()

	turn := 1
	rejectedFieldRetryState = newOpenAIResponsesRejectedFieldRetryState(currentPayload)
	turnRetry := 0
	turnPrevRecoveryTried := false
	derivedUserTurnPrepared := 0
	lastTurnFinishedAt := time.Time{}
	lastTurnResponseID := ""
	lastTurnWindowID := ""
	lastTurnPayload := []byte(nil)
	var lastTurnStrictState *openAIWSIngressPreviousTurnStrictState
	replayLimits := s.openAIWSReplayInputLimits()
	lastTurnReplayState := openAIWSReplayInputState{}
	currentTurnReplayState := openAIWSReplayInputState{}
	skipBeforeTurn := false
	hasCurrentOrReplayFunctionCallOutput := func(payload []byte) bool {
		if openAIWSRawPayloadHasToolCallOutput(payload) {
			return true
		}
		return !currentTurnReplayState.unavailable &&
			currentTurnReplayState.exists &&
			openAIWSRawItemsHasFunctionCallOutput(currentTurnReplayState.items)
	}
	resetSessionLease := func(markBroken bool) {
		if sessionLease == nil {
			return
		}
		if markBroken {
			sessionLease.MarkBroken()
		}
		releaseSessionLease()
		sessionLease = nil
		sessionConnID = ""
		preferredConnID = ""
	}
	recoverIngressPrevResponseNotFound := func(relayErr error, turn int, connID string) bool {
		if !isOpenAIWSIngressPreviousResponseNotFound(relayErr) {
			return false
		}
		if turnPrevRecoveryTried || !s.openAIWSIngressPreviousResponseRecoveryEnabled() {
			return false
		}
		// 携带 function_call_output 的请求不能丢弃 previous_response_id：
		// 上游 API 需要 response chain 来匹配 tool_result 与之前的 tool_use，
		// 丢弃后会导致 "No tool call found for function call output" 400 错误。
		if hasCurrentOrReplayFunctionCallOutput(currentPayload) {
			return false
		}
		if currentTurnReplayState.unavailable {
			logOpenAIWSModeInfo(
				"ingress_ws_prev_response_recovery_skip account_id=%d turn=%d conn_id=%s reason=replay_unavailable action=restart_conversation",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
			)
			return false
		}
		if isStrictAffinityTurn(currentPayload) {
			// Layer 2：严格亲和链路命中 previous_response_not_found 时，降级为“去掉 previous_response_id 后重放一次”。
			// 该错误说明续链锚点已失效，继续 strict fail-close 只会直接中断本轮请求。
			logOpenAIWSModeInfo(
				"ingress_ws_prev_response_recovery_layer2 account_id=%d turn=%d conn_id=%s store_disabled_conn_mode=%s action=drop_previous_response_id_retry",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				normalizeOpenAIWSLogValue(storeDisabledConnMode),
			)
		}
		turnPrevRecoveryTried = true
		updatedPayload, removed, dropErr := dropPreviousResponseIDFromRawPayload(currentPayload)
		if dropErr != nil || !removed {
			reason := "not_removed"
			if dropErr != nil {
				reason = "drop_error"
			}
			logOpenAIWSModeInfo(
				"ingress_ws_prev_response_recovery_skip account_id=%d turn=%d conn_id=%s reason=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				normalizeOpenAIWSLogValue(reason),
			)
			return false
		}
		updatedWithInput, setInputErr := setOpenAIWSPayloadInputSequence(
			updatedPayload,
			currentTurnReplayState.items,
			currentTurnReplayState.exists,
		)
		if setInputErr != nil {
			logOpenAIWSModeInfo(
				"ingress_ws_prev_response_recovery_skip account_id=%d turn=%d conn_id=%s reason=set_full_input_error cause=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(setInputErr.Error(), openAIWSLogValueMaxLen),
			)
			return false
		}
		logOpenAIWSModeInfo(
			"ingress_ws_prev_response_recovery account_id=%d turn=%d conn_id=%s action=drop_previous_response_id retry=1",
			account.ID,
			turn,
			truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
		)
		currentPayload = updatedWithInput
		currentPayloadBytes = len(updatedWithInput)
		resetSessionLease(true)
		skipBeforeTurn = true
		return true
	}
	retryIngressTurn := func(relayErr error, turn int, connID string) bool {
		if !isOpenAIWSIngressTurnRetryable(relayErr) || turnRetry >= 1 {
			return false
		}
		if isStrictAffinityTurn(currentPayload) {
			logOpenAIWSModeInfo(
				"ingress_ws_turn_retry_skip account_id=%d turn=%d conn_id=%s reason=strict_affinity",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
			)
			return false
		}
		turnRetry++
		logOpenAIWSModeInfo(
			"ingress_ws_turn_retry account_id=%d turn=%d retry=%d reason=%s conn_id=%s",
			account.ID,
			turn,
			turnRetry,
			truncateOpenAIWSLogValue(openAIWSIngressTurnRetryReason(relayErr), openAIWSLogValueMaxLen),
			truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
		)
		resetSessionLease(true)
		forceFreshConn = true
		skipBeforeTurn = true
		return true
	}
	for {
		if turn > 1 && !skipBeforeTurn && hooks != nil && hooks.BeforeRequest != nil {
			if err := hooks.BeforeRequest(turn, currentOriginalPayload, currentOriginalModel); err != nil {
				return err
			}
		}
		if !skipBeforeTurn && hooks != nil && hooks.BeforeTurn != nil {
			if err := hooks.BeforeTurn(turn); err != nil {
				return err
			}
		}
		skipBeforeTurn = false
		// 剥离本会话已知失效的加密项，阻断同一失效密文随历史反复触发上游拒绝。
		// 历史序列须同步剥离，否则与已剥离的当前 input 项错位，prefix 复用失配。
		if invalidDigests := s.sessionInvalidEncryptedContentDigests(groupID, sessionHash); len(invalidDigests) > 0 {
			strippedPayload, strippedCount := s.stripSessionInvalidEncryptedContentLogged(
				currentPayload, invalidDigests, "ingress_ws_invalid_encrypted_lineage_strip", account.ID, turn,
			)
			if strippedCount > 0 {
				currentPayload = strippedPayload
				currentPayloadBytes = len(strippedPayload)
			}
			if lastTurnReplayState.exists && !lastTurnReplayState.unavailable {
				stripped, strippedCount := stripOpenAIInvalidEncryptedContentFromReplayItems(lastTurnReplayState.items, invalidDigests)
				if strippedCount > 0 {
					lastTurnReplayState = newOpenAIWSReplayInputState(stripped, true)
				}
			}
		}
		boundaryPayload, contextWindowBoundary, boundaryErr := applyOpenAIWSContextWindowBoundary(
			currentPayload,
			lastTurnWindowID,
			officialEgressEnabled,
		)
		if boundaryErr != nil {
			return fmt.Errorf("normalize Codex websocket context-window boundary: %w", boundaryErr)
		}
		if contextWindowBoundary.Changed {
			currentPayload = boundaryPayload
			currentPayloadBytes = len(boundaryPayload)
			logOpenAIWSModeInfo(
				"ingress_ws_context_window_changed account_id=%d turn=%d conn_id=%s action=break_previous_response_chain previous_window_id=%s current_window_id=%s previous_response_id_removed=%v",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(lastTurnWindowID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(contextWindowBoundary.WindowID, openAIWSIDValueMaxLen),
				contextWindowBoundary.PreviousResponseIDRemoved,
			)
		}
		currentPreviousResponseID := openAIWSPayloadStringFromRaw(currentPayload, "previous_response_id")
		expectedPrev := strings.TrimSpace(lastTurnResponseID)
		if contextWindowBoundary.Changed {
			// A context-window rollover is a new Responses root. Do not infer a
			// continuation anchor from the response produced in the old window.
			expectedPrev = ""
		}
		toolSignals := ToolContinuationSignals{
			HasFunctionCallOutput: openAIWSRawPayloadHasToolCallOutput(currentPayload),
		}
		if isDerivedOpenAIOfficialEgressWSContext(ctx) {
			derivedPayload, decodeErr := decodeOfficialJSONObjectUseNumber(currentPayload)
			if decodeErr != nil {
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"official egress websocket tool continuation classification failed",
					decodeErr,
				)
			}
			egressContext, _ := OfficialEgressContextFromContext(ctx)
			_, hasCurrentToolOutput, reliable, classifyErr :=
				classifyOfficialOpenAIWSToolOutputTurnWithDerivedState(
					derivedPayload,
					egressContext.openAIWSDerived,
				)
			if classifyErr != nil {
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"official egress websocket tool continuation classification failed",
					classifyErr,
				)
			}
			// 断线重连是新会话，没有上一轮待回传 call_id 可用于消歧；客户端重发的完整历史
			// 若自身完整（没有续链锚点、每个工具输出都有对应的工具调用），就不再区分本轮与
			// 历史工具输出，原样按携带完整历史的普通轮次开新链。只在这种情况放行，其余照旧拒绝。
			fullHistoryFallback := false
			if !reliable {
				fullHistoryFallback = s.openAIWSToolContinuationFullHistoryFallbackEnabled() &&
					currentPreviousResponseID == "" &&
					AnalyzeToolCallOutputContextCoverageBytes(currentPayload).ConcreteContextCoversAllCallIDs
				if !fullHistoryFallback {
					egressContext.openAIWSDerived.setFullHistoryFallback(false)
					return NewOpenAIWSClientCloseError(
						coderws.StatusPolicyViolation,
						"official egress websocket tool continuation turn is ambiguous",
						errOpenAIOfficialEgressWSToolOutputTurnAmbiguous,
					)
				}
				logOpenAIWSModeInfo(
					"ingress_ws_tool_continuation_ambiguous_fallback account_id=%d turn=%d conn_id=%s reason=concrete_context_covers_all action=full_history_new_chain",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
				)
				hasCurrentToolOutput = false
			}
			egressContext.openAIWSDerived.setFullHistoryFallback(fullHistoryFallback)
			toolSignals.HasFunctionCallOutput = hasCurrentToolOutput
		}
		if toolSignals.HasFunctionCallOutput {
			var currentReqBody map[string]any
			if err := json.Unmarshal(currentPayload, &currentReqBody); err == nil {
				toolSignals = AnalyzeToolContinuationSignals(currentReqBody)
			}
		}
		hasFunctionCallOutput := toolSignals.HasFunctionCallOutput
		// Codex CLI 的每个新用户轮次都新建上游 WS，并先执行一轮
		// generate=false 预热；工具结果续轮则继续使用同一条连接。
		if isDerivedOpenAIOfficialEgressWSContext(ctx) &&
			!hasFunctionCallOutput &&
			currentPreviousResponseID == "" &&
			derivedUserTurnPrepared != turn {
			resetSessionLease(false)
			forceFreshConn = true
			derivedUserTurnPrepared = turn
		}
		// store=false + function_call_output 场景必须有续链锚点。
		// 若客户端未传 previous_response_id，优先回填上一轮响应 ID，避免上游报 call_id 无法关联。
		if shouldInferIngressFunctionCallOutputPreviousResponseID(
			storeDisabled,
			turn,
			toolSignals,
			currentPreviousResponseID,
			expectedPrev,
		) {
			updatedPayload, setPrevErr := setPreviousResponseIDToRawPayload(currentPayload, expectedPrev)
			if setPrevErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_function_call_output_prev_infer_skip account_id=%d turn=%d conn_id=%s reason=set_previous_response_id_error cause=%s expected_previous_response_id=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
					truncateOpenAIWSLogValue(setPrevErr.Error(), openAIWSLogValueMaxLen),
					truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
				)
			} else {
				currentPayload = updatedPayload
				currentPayloadBytes = len(updatedPayload)
				currentPreviousResponseID = expectedPrev
				logOpenAIWSModeInfo(
					"ingress_ws_function_call_output_prev_infer account_id=%d turn=%d conn_id=%s action=set_previous_response_id previous_response_id=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
					truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
				)
			}
		}
		currentInputItems, currentInputExists, replayInputErr := openAIWSExtractNormalizedInputSequence(currentPayload)
		nextReplayState := unavailableOpenAIWSReplayInputState()
		if replayInputErr == nil {
			nextReplayState, replayInputErr = buildOpenAIWSReplayInputState(
				lastTurnReplayState,
				currentInputItems,
				currentInputExists,
				currentPreviousResponseID != "",
				replayLimits,
			)
		}
		if replayInputErr != nil {
			logOpenAIWSModeInfo(
				"ingress_ws_replay_input_skip account_id=%d turn=%d conn_id=%s reason=build_error action=disable_replay cause=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(replayInputErr.Error(), openAIWSLogValueMaxLen),
			)
		}
		currentTurnReplayState = nextReplayState
		replayHasFunctionCallOutput := !currentTurnReplayState.unavailable &&
			currentTurnReplayState.exists &&
			openAIWSRawItemsHasFunctionCallOutput(currentTurnReplayState.items)
		hasFunctionCallOutput = hasFunctionCallOutput || replayHasFunctionCallOutput
		if storeDisabled && turn > 1 && currentPreviousResponseID != "" {
			shouldKeepPreviousResponseID := shouldPreserveOpenAIOfficialEgressWSPreviousResponseID(
				ctx,
				currentPreviousResponseID,
				expectedPrev,
			)
			strictReason := "official_egress_valid_chain"
			var strictErr error
			if !shouldKeepPreviousResponseID && lastTurnStrictState != nil {
				shouldKeepPreviousResponseID, strictReason, strictErr = shouldKeepIngressPreviousResponseIDWithStrictState(
					lastTurnStrictState,
					currentPayload,
					lastTurnResponseID,
					hasFunctionCallOutput,
				)
			} else if !shouldKeepPreviousResponseID {
				shouldKeepPreviousResponseID, strictReason, strictErr = shouldKeepIngressPreviousResponseID(
					lastTurnPayload,
					currentPayload,
					lastTurnResponseID,
					hasFunctionCallOutput,
				)
			}
			if strictErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_prev_response_strict_eval account_id=%d turn=%d conn_id=%s action=keep_previous_response_id reason=%s cause=%s previous_response_id=%s expected_previous_response_id=%s has_function_call_output=%v",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
					normalizeOpenAIWSLogValue(strictReason),
					truncateOpenAIWSLogValue(strictErr.Error(), openAIWSLogValueMaxLen),
					truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
					truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
					hasFunctionCallOutput,
				)
			} else if !shouldKeepPreviousResponseID {
				if currentTurnReplayState.unavailable {
					logOpenAIWSModeInfo(
						"ingress_ws_prev_response_strict_eval account_id=%d turn=%d conn_id=%s action=restart_conversation reason=replay_unavailable previous_response_id=%s expected_previous_response_id=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
					)
					return newOpenAIWSReplayUnavailableCloseError(nil)
				}
				updatedPayload, removed, dropErr := dropPreviousResponseIDFromRawPayload(currentPayload)
				if dropErr != nil || !removed {
					dropReason := "not_removed"
					if dropErr != nil {
						dropReason = "drop_error"
					}
					logOpenAIWSModeInfo(
						"ingress_ws_prev_response_strict_eval account_id=%d turn=%d conn_id=%s action=keep_previous_response_id reason=%s drop_reason=%s previous_response_id=%s expected_previous_response_id=%s has_function_call_output=%v",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
						normalizeOpenAIWSLogValue(strictReason),
						normalizeOpenAIWSLogValue(dropReason),
						truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
						hasFunctionCallOutput,
					)
				} else {
					updatedWithInput, setInputErr := setOpenAIWSPayloadInputSequence(
						updatedPayload,
						currentTurnReplayState.items,
						currentTurnReplayState.exists,
					)
					if setInputErr != nil {
						logOpenAIWSModeInfo(
							"ingress_ws_prev_response_strict_eval account_id=%d turn=%d conn_id=%s action=keep_previous_response_id reason=%s drop_reason=set_full_input_error previous_response_id=%s expected_previous_response_id=%s cause=%s has_function_call_output=%v",
							account.ID,
							turn,
							truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
							normalizeOpenAIWSLogValue(strictReason),
							truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
							truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
							truncateOpenAIWSLogValue(setInputErr.Error(), openAIWSLogValueMaxLen),
							hasFunctionCallOutput,
						)
					} else {
						currentPayload = updatedWithInput
						currentPayloadBytes = len(updatedWithInput)
						logOpenAIWSModeInfo(
							"ingress_ws_prev_response_strict_eval account_id=%d turn=%d conn_id=%s action=drop_previous_response_id_full_create reason=%s previous_response_id=%s expected_previous_response_id=%s has_function_call_output=%v",
							account.ID,
							turn,
							truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
							normalizeOpenAIWSLogValue(strictReason),
							truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
							truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
							hasFunctionCallOutput,
						)
						currentPreviousResponseID = ""
					}
				}
			}
		}
		forcePreferredConn := isStrictAffinityTurn(currentPayload)
		if sessionLease == nil {
			acquiredLease, acquireErr := acquireTurnLease(turn, preferredConnID, forcePreferredConn)
			if acquireErr != nil {
				return fmt.Errorf("acquire upstream websocket: %w", acquireErr)
			}
			sessionLease = acquiredLease
			sessionConnID = strings.TrimSpace(sessionLease.ConnID())
			if storeDisabled {
				pinSessionConn(sessionConnID)
			} else {
				unpinSessionConn(sessionConnID)
			}
		}
		retireBeforeTurn := turn > 1 && sessionLease != nil &&
			turnRetry == 0 && shouldRetireOpenAIWSLease(sessionLease)
		shouldPreflightPing := turn > 1 && sessionLease != nil &&
			!retireBeforeTurn && sessionLease.SupportsIdlePingWithoutReader() && turnRetry == 0
		if shouldPreflightPing && openAIWSIngressPreflightPingIdle > 0 && !lastTurnFinishedAt.IsZero() {
			if time.Since(lastTurnFinishedAt) < openAIWSIngressPreflightPingIdle {
				shouldPreflightPing = false
			}
		}
		var preflightErr error
		preflightTrigger := ""
		if retireBeforeTurn {
			preflightErr = errOpenAIWSConnRetiring
			preflightTrigger = "connection_retiring"
			logOpenAIWSModeInfo(
				"ingress_ws_upstream_retire_before_turn account_id=%d turn=%d conn_id=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
			)
		} else if shouldPreflightPing {
			preflightErr = sessionLease.PingWithTimeout(openAIWSProbePingTO)
			preflightTrigger = "ping_failed"
			if preflightErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_upstream_preflight_ping_fail account_id=%d turn=%d conn_id=%s cause=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
					truncateOpenAIWSLogValue(preflightErr.Error(), openAIWSLogValueMaxLen),
				)
			}
		}
		if preflightErr != nil {
			if forcePreferredConn {
				// 携带 function_call_output 的请求不能丢弃 previous_response_id：
				// 上游 API 需要 response chain 来匹配 tool_result 与之前的 tool_use，
				// 除非 replay input 已经包含与每个 tool_result 匹配的 tool_use 上下文。
				// 主动退休是确定性的换连边界；此时完整回放可以开启新链，不能继续
				// 因官方上一轮锚点有效而强行保留旧连接专属的 previous_response_id。
				hasFCOutput := hasFunctionCallOutput
				hasReplayToolContext := hasFCOutput &&
					!currentTurnReplayState.unavailable &&
					currentTurnReplayState.exists &&
					openAIWSRawItemsHaveToolCallContextForOutputs(currentTurnReplayState.items)
				preserveOfficialChain := shouldPreserveOpenAIOfficialEgressWSPreviousResponseID(
					ctx,
					currentPreviousResponseID,
					expectedPrev,
				) && !retireBeforeTurn
				if !preserveOfficialChain && currentTurnReplayState.unavailable {
					logOpenAIWSModeInfo(
						"ingress_ws_preflight_ping_recovery_skip account_id=%d turn=%d conn_id=%s trigger=%s reason=replay_unavailable action=restart_conversation previous_response_id=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
						preflightTrigger,
						truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
					)
					resetSessionLease(true)
					return newOpenAIWSReplayUnavailableCloseError(preflightErr)
				}
				if !preserveOfficialChain &&
					!turnPrevRecoveryTried &&
					currentPreviousResponseID != "" &&
					(!hasFCOutput || hasReplayToolContext) {
					updatedPayload, removed, dropErr := dropPreviousResponseIDFromRawPayload(currentPayload)
					if dropErr != nil || !removed {
						reason := "not_removed"
						if dropErr != nil {
							reason = "drop_error"
						}
						logOpenAIWSModeInfo(
							"ingress_ws_preflight_ping_recovery_skip account_id=%d turn=%d conn_id=%s trigger=%s reason=%s previous_response_id=%s",
							account.ID,
							turn,
							truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
							preflightTrigger,
							normalizeOpenAIWSLogValue(reason),
							truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
						)
					} else {
						updatedWithInput, setInputErr := setOpenAIWSPayloadInputSequence(
							updatedPayload,
							currentTurnReplayState.items,
							currentTurnReplayState.exists,
						)
						if setInputErr != nil {
							logOpenAIWSModeInfo(
								"ingress_ws_preflight_ping_recovery_skip account_id=%d turn=%d conn_id=%s trigger=%s reason=set_full_input_error previous_response_id=%s cause=%s",
								account.ID,
								turn,
								truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
								preflightTrigger,
								truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
								truncateOpenAIWSLogValue(setInputErr.Error(), openAIWSLogValueMaxLen),
							)
						} else {
							logOpenAIWSModeInfo(
								"ingress_ws_preflight_ping_recovery account_id=%d turn=%d conn_id=%s trigger=%s action=drop_previous_response_id_retry previous_response_id=%s has_function_call_output=%v has_replay_tool_context=%v",
								account.ID,
								turn,
								truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
								preflightTrigger,
								truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
								hasFCOutput,
								hasReplayToolContext,
							)
							turnPrevRecoveryTried = true
							currentPayload = updatedWithInput
							currentPayloadBytes = len(updatedWithInput)
							resetSessionLease(true)
							skipBeforeTurn = true
							continue
						}
					}
				}
				if hasFCOutput && currentPreviousResponseID != "" {
					reason := "function_call_output_missing_replay_context"
					if hasReplayToolContext {
						reason = "function_call_output_replay_not_applied"
					}
					logOpenAIWSModeInfo(
						"ingress_ws_preflight_ping_recovery_skip account_id=%d turn=%d conn_id=%s trigger=%s reason=%s action=fail_close previous_response_id=%s has_replay_tool_context=%v",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
						preflightTrigger,
						reason,
						truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
						hasReplayToolContext,
					)
				}
				resetSessionLease(true)
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"upstream continuation connection is unavailable; please restart the conversation",
					preflightErr,
				)
			}
			resetSessionLease(true)

			acquiredLease, acquireErr := acquireTurnLease(turn, preferredConnID, forcePreferredConn)
			if acquireErr != nil {
				return fmt.Errorf("acquire upstream websocket after preflight check: %w", acquireErr)
			}
			sessionLease = acquiredLease
			sessionConnID = strings.TrimSpace(sessionLease.ConnID())
			if storeDisabled {
				pinSessionConn(sessionConnID)
			}
		}
		connID := sessionConnID
		if currentPreviousResponseID != "" {
			chainedFromLast := expectedPrev != "" && currentPreviousResponseID == expectedPrev
			currentPreviousResponseIDKind := ClassifyOpenAIPreviousResponseIDKind(currentPreviousResponseID)
			logOpenAIWSModeInfo(
				"ingress_ws_turn_chain account_id=%d turn=%d conn_id=%s previous_response_id=%s previous_response_id_kind=%s last_turn_response_id=%s chained_from_last=%v preferred_conn_id=%s header_session_id=%s header_conversation_id=%s has_turn_state=%v turn_state_len=%d has_prompt_cache_key=%v store_disabled=%v",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(currentPreviousResponseID, openAIWSIDValueMaxLen),
				normalizeOpenAIWSLogValue(currentPreviousResponseIDKind),
				truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
				chainedFromLast,
				truncateOpenAIWSLogValue(preferredConnID, openAIWSIDValueMaxLen),
				openAIWSHeaderValueForLog(baseAcquireReq.Headers, "session_id"),
				openAIWSHeaderValueForLog(baseAcquireReq.Headers, "conversation_id"),
				turnState != "",
				len(turnState),
				openAIWSPayloadStringFromRaw(currentPayload, "prompt_cache_key") != "",
				storeDisabled,
			)
		}

		finalPayload, _, finalizeErr := prepareOpenAIOfficialEgressSemanticWSFrame(
			ctx,
			currentOriginalPayload,
			currentPayload,
			expectedPrev,
			turnPrevRecoveryTried,
		)
		if finalizeErr != nil {
			return NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"official egress websocket frame validation failed",
				finalizeErr,
			)
		}
		currentPayload = finalPayload
		currentPayloadBytes = len(finalPayload)
		if officialEgressEnabled && strings.TrimSpace(turnState) != "" {
			withTurnState, setTurnStateErr := injectOfficialOpenAIWSTurnState(
				ctx, currentPayload, turnState,
			)
			if setTurnStateErr != nil {
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"official egress websocket turn-state construction failed",
					setTurnStateErr,
				)
			}
			currentPayload = withTurnState
			currentPayloadBytes = len(withTurnState)
		}
		outboundPayload := currentPayload
		outboundPayloadBytes := currentPayloadBytes
		var result *OpenAIForwardResult
		var relayErr error
		toolContinuationPayload, toolContinuationChanged, toolContinuationErr :=
			buildDerivedOpenAIOfficialEgressWSToolContinuationFrame(
				ctx,
				outboundPayload,
				expectedPrev,
			)
		if toolContinuationErr != nil {
			return NewOpenAIWSClientCloseError(
				coderws.StatusPolicyViolation,
				"official egress websocket tool continuation construction failed",
				toolContinuationErr,
			)
		}
		if toolContinuationChanged {
			outboundPayload = toolContinuationPayload
			outboundPayloadBytes = len(outboundPayload)
		}
		{
			prewarmPayload, shouldInject, prewarmBuildErr :=
				buildDerivedOpenAIOfficialEgressWSPrewarmFrame(ctx, outboundPayload)
			if prewarmBuildErr != nil {
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"official egress websocket prewarm construction failed",
					prewarmBuildErr,
				)
			}
			if shouldInject {
				prewarmResult, prewarmRelayErr := sendAndRelay(
					turn,
					sessionLease,
					prewarmPayload,
					len(prewarmPayload),
					currentOriginalModel,
					currentImageBillingModel,
					currentImageSizeTier,
					currentImageInputSize,
					false,
					currentRequestedReasoningEffort,
				)
				if prewarmRelayErr != nil {
					relayErr = prewarmRelayErr
				} else if prewarmResult == nil ||
					!prewarmResult.SucceededForScheduling() ||
					strings.TrimSpace(prewarmResult.RequestID) == "" {
					relayErr = wrapOpenAIWSIngressTurnError(
						"read_upstream",
						errors.New("official egress websocket prewarm did not return a successful response ID"),
						false,
					)
				} else {
					outboundPayload, prewarmBuildErr =
						chainDerivedOpenAIOfficialEgressWSBusinessFrame(
							ctx,
							outboundPayload,
							prewarmResult.RequestID,
						)
					if prewarmBuildErr != nil {
						return NewOpenAIWSClientCloseError(
							coderws.StatusPolicyViolation,
							"official egress websocket prewarm chaining failed",
							prewarmBuildErr,
						)
					}
					outboundPayloadBytes = len(outboundPayload)
					logOpenAIWSModeInfo(
						"ingress_ws_official_prewarm_chain account_id=%d turn=%d conn_id=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
					)
				}
			}
		}
		if relayErr == nil {
			result, relayErr = sendAndRelay(
				turn,
				sessionLease,
				outboundPayload,
				outboundPayloadBytes,
				currentOriginalModel,
				currentImageBillingModel,
				currentImageSizeTier,
				currentImageInputSize,
				true,
				currentRequestedReasoningEffort,
			)
		}
		if relayErr != nil {
			lastTurnClean = false
			if isOpenAIWSSessionPreempted(ctx) {
				sessionLease.MarkBroken()
				return errOpenAIWSSessionPreempted
			}
			var rejectedFieldErr *openAIWSRejectedFieldRetryError
			if errors.As(relayErr, &rejectedFieldErr) && rejectedFieldErr != nil && len(rejectedFieldErr.body) > 0 {
				currentPayload = append([]byte(nil), rejectedFieldErr.body...)
				currentPayloadBytes = len(currentPayload)
				skipBeforeTurn = true
				continue
			}
			if recoverIngressPrevResponseNotFound(relayErr, turn, connID) {
				continue
			}
			if retryIngressTurn(relayErr, turn, connID) {
				continue
			}
			finalErr := relayErr
			if unwrapped := errors.Unwrap(relayErr); unwrapped != nil {
				finalErr = unwrapped
			}
			if shouldEmitOpenAIWSUpstreamFailureEvent(
				openAIWSIngressTurnRetryReason(relayErr),
				relayErr,
			) {
				if writeErr := writeClientMessage(buildOpenAIWSUpstreamFailureEvent()); writeErr == nil {
					finalErr = NewOpenAIWSClientCloseError(
						coderws.StatusInternalError,
						openAIWSUpstreamFailureMessage,
						finalErr,
					)
				}
			}
			if hooks != nil && hooks.AfterTurn != nil {
				hooks.AfterTurn(turn, nil, finalErr)
			}
			sessionLease.MarkBroken()
			return finalErr
		}
		turnRetry = 0
		turnPrevRecoveryTried = false
		lastTurnFinishedAt = time.Now()
		lastTurnClean = true
		if hooks != nil && hooks.AfterTurn != nil {
			hooks.AfterTurn(turn, result, nil)
		}
		if result == nil {
			return errors.New("websocket turn result is nil")
		}
		if egressContext, enabled := OfficialEgressContextFromContext(ctx); enabled &&
			egressContext != nil && egressContext.openAIWSDerived != nil {
			// 保存上一轮上游真实产生的工具调用 call_id。断线后客户端
			// 可能重发没有逐项 turn_id 的完整历史，下一轮只能用这份
			// 会话状态做安全消歧。
			egressContext.openAIWSDerived.setPendingToolCallIDs(result.wsReplayInput)
		}
		responseID := strings.TrimSpace(result.RequestID)
		lastTurnResponseID = responseID
		lastTurnReplayState = currentTurnReplayState
		if contextWindowBoundary.WindowID != "" {
			lastTurnWindowID = contextWindowBoundary.WindowID
		}
		if result.wsReplayInputExists {
			var appendErr error
			lastTurnReplayState, appendErr = appendOpenAIWSReplayInputState(
				lastTurnReplayState,
				result.wsReplayInput,
				replayLimits,
			)
			if appendErr != nil {
				logOpenAIWSModeInfo(
					"ingress_ws_replay_input_limit account_id=%d turn=%d mode=ctx_pool scope=response action=disable_replay cause=%s",
					account.ID,
					turn,
					truncateOpenAIWSLogValue(appendErr.Error(), openAIWSLogValueMaxLen),
				)
			}
		}
		currentTurnReplayState = openAIWSReplayInputState{}
		nextStrictState, strictStateErr := buildOpenAIWSIngressPreviousTurnStrictState(outboundPayload)
		if strictStateErr != nil {
			lastTurnStrictState = nil
			// strict 状态不可用时保留整份上一轮 payload 供慢路径比较。
			lastTurnPayload = currentPayload
			logOpenAIWSModeInfo(
				"ingress_ws_prev_response_strict_state_skip account_id=%d turn=%d conn_id=%s reason=build_error cause=%s",
				account.ID,
				turn,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(strictStateErr.Error(), openAIWSLogValueMaxLen),
			)
		} else {
			lastTurnStrictState = nextStrictState
			lastTurnPayload = nil
		}

		if responseID != "" && stateStore != nil {
			ttl := s.openAIWSResponseStickyTTL()
			logOpenAIWSBindResponseAccountWarn(groupID, account.ID, responseID, stateStore.BindResponseAccount(ctx, groupID, responseID, account.ID, ttl))
			stateStore.BindResponseConn(responseID, connID, ttl)
		}
		if stateStore != nil && storeDisabled && sessionHash != "" {
			stateStore.BindSessionConn(groupID, sessionHash, connID, s.openAIWSSessionStickyTTL())
		}
		if connID != "" {
			preferredConnID = connID
		}

		nextClientMessage, readErr := readClientMessage()
		if readErr != nil {
			if isOpenAIWSSessionPreempted(ctx) {
				return errOpenAIWSSessionPreempted
			}
			if isOpenAIWSClientDisconnectError(readErr) {
				closeStatus, closeReason := summarizeOpenAIWSReadCloseError(readErr)
				logOpenAIWSModeInfo(
					"ingress_ws_client_closed account_id=%d conn_id=%s close_status=%s close_reason=%s",
					account.ID,
					truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
					closeStatus,
					truncateOpenAIWSLogValue(closeReason, openAIWSHeaderValueMaxLen),
				)
				return nil
			}
			return fmt.Errorf("read client websocket request: %w", readErr)
		}

		nextPayload, parseErr := parseClientPayload(turn+1, nextClientMessage)
		if parseErr != nil {
			return parseErr
		}
		if officialRuntime != nil {
			nextRoutingHint, routingHintErr := officialegress.ParseOfficialCodexRoutingHintFacts(
				officialCodexEndpointResponsesWS,
				nextPayload.payloadRaw,
			)
			if routingHintErr != nil {
				return NewOpenAIWSClientCloseError(
					coderws.StatusPolicyViolation,
					"official egress websocket routing hint validation failed",
					routingHintErr,
				)
			}
			baseAcquireReq.RoutingHint = nextRoutingHint
		}
		nextRoutingFields := gjson.GetManyBytes(nextPayload.payloadRaw, "model", "service_tier")
		if nextPayload.promptCacheKey != "" {
			// ingress 会话在整个客户端 WS 生命周期内复用同一上游连接；
			// prompt_cache_key 对握手头的更新仅在未来需要重新建连时生效。
			updatedHeaders, _, updHdrErr := s.buildOpenAIWSHeaders(
				ctx,
				c,
				account,
				token,
				wsDecision,
				isCodexCLI,
				turnState,
				strings.TrimSpace(c.GetHeader(openAIWSTurnMetadataHeader)),
				nextPayload.promptCacheKey,
				nextRoutingFields[0].String(),
				nextRoutingFields[1].String(),
			)
			if updHdrErr != nil {
				logOpenAIWSModeInfo("ingress_ws_update_headers_failed account_id=%d err=%v", account.ID, updHdrErr)
			} else {
				baseAcquireReq.Headers = updatedHeaders
			}
		}
		setOpenAICodexRoutingHint(baseAcquireReq.Headers, account, nextRoutingFields[0].String(), nextRoutingFields[1].String())
		if nextPayload.previousResponseID != "" {
			expectedPrev := strings.TrimSpace(lastTurnResponseID)
			chainedFromLast := expectedPrev != "" && nextPayload.previousResponseID == expectedPrev
			nextPreviousResponseIDKind := ClassifyOpenAIPreviousResponseIDKind(nextPayload.previousResponseID)
			logOpenAIWSModeInfo(
				"ingress_ws_next_turn_chain account_id=%d turn=%d next_turn=%d conn_id=%s previous_response_id=%s previous_response_id_kind=%s last_turn_response_id=%s chained_from_last=%v has_prompt_cache_key=%v store_disabled=%v",
				account.ID,
				turn,
				turn+1,
				truncateOpenAIWSLogValue(connID, openAIWSIDValueMaxLen),
				truncateOpenAIWSLogValue(nextPayload.previousResponseID, openAIWSIDValueMaxLen),
				normalizeOpenAIWSLogValue(nextPreviousResponseIDKind),
				truncateOpenAIWSLogValue(expectedPrev, openAIWSIDValueMaxLen),
				chainedFromLast,
				nextPayload.promptCacheKey != "",
				storeDisabled,
			)
		}
		if stateStore != nil && nextPayload.previousResponseID != "" {
			if stickyConnID, ok := stateStore.GetResponseConn(nextPayload.previousResponseID); ok {
				if sessionConnID != "" && stickyConnID != "" && stickyConnID != sessionConnID {
					logOpenAIWSModeInfo(
						"ingress_ws_keep_session_conn account_id=%d turn=%d conn_id=%s sticky_conn_id=%s previous_response_id=%s",
						account.ID,
						turn,
						truncateOpenAIWSLogValue(sessionConnID, openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(stickyConnID, openAIWSIDValueMaxLen),
						truncateOpenAIWSLogValue(nextPayload.previousResponseID, openAIWSIDValueMaxLen),
					)
				} else {
					preferredConnID = stickyConnID
				}
			}
		}
		currentPayload = nextPayload.payloadRaw
		currentOriginalPayload = nextPayload.rawForHash
		currentOriginalModel = nextPayload.originalModel
		currentImageBillingModel = nextPayload.imageBillingModel
		currentImageSizeTier = nextPayload.imageSizeTier
		currentImageInputSize = nextPayload.imageInputSize
		currentPayloadBytes = nextPayload.payloadBytes
		currentRequestedReasoningEffort = nextPayload.requestedReasoningEffort
		rejectedFieldRetryState = newOpenAIResponsesRejectedFieldRetryState(currentPayload)
		storeDisabled = s.isOpenAIWSStoreDisabledInRequestRaw(currentPayload, account)
		if !storeDisabled {
			unpinSessionConn(sessionConnID)
		}
		turn++
	}
}
