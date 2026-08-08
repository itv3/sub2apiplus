package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// officialCodexSemanticAttempt 是 service 业务语义与 Executor 线协议权威之间的
// 唯一转换值。Headers 只允许保留非画像业务头；身份和认证材料分别进入专用结构。
type officialCodexSemanticAttempt struct {
	Headers        http.Header
	Body           officialegress.RequestBody
	BodyConditions officialegress.BodyRuntimeConditions
	IdentityFacts  officialegress.CodexIdentityFacts
	Authentication officialegress.AttemptAuthentication
}

type officialCodexIdentityAccountProjection struct {
	ID               int64
	ChatGPTAccountID string
	FedRAMP          bool
}

// officialCodexAttemptConditions 只记录本次 attempt 已验证材料所决定的条件，
// 避免身份事实构造器再从普通 Header 反推画像 feature 或动态身份。
type officialCodexAttemptConditions struct {
	AttestationPresent  bool
	CookiePresent       bool
	CompressionEligible bool
}

// officialCodexInvocationIdentityInput 是 service 向 Executor 身份权威提交的
// invocation 级结构化动态事实。字段均为不可变字符串，不携带账号对象、凭据、
// 可变 map 或调用方可自由选择的 feature 开关。
type officialCodexInvocationIdentityInput struct {
	InstallationID  string
	SessionID       string
	ConversationID  string
	ThreadID        string
	WindowID        string
	ClientRequestID string
	TurnID          string
	TurnMetadata    string
	TurnState       string
	ParentThreadID  string
	Subagent        string
}

type officialCodexInvocationIdentityContextKey struct{}

func withOfficialCodexInvocationIdentity(
	ctx context.Context,
	input officialCodexInvocationIdentityInput,
) context.Context {
	return context.WithValue(ctx, officialCodexInvocationIdentityContextKey{}, input)
}

func officialCodexInvocationIdentityFromContext(
	ctx context.Context,
) officialCodexInvocationIdentityInput {
	if ctx == nil {
		return officialCodexInvocationIdentityInput{}
	}
	input, _ := ctx.Value(officialCodexInvocationIdentityContextKey{}).(officialCodexInvocationIdentityInput)
	return input
}

// officialCodexRuntimeStateForReleaseMode 把 ReleaseCatalog 的 active/previous
// build 身份投影为结构化进程事实。它只用于没有入站客户端上下文的后台调用；
// 最终 Header 仍由 officialegress compiler 按 ProfileSpec 生成。
func officialCodexRuntimeStateForReleaseMode(
	mode string,
	endpointID string,
) (officialCodexRuntimeState, error) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP, mode,
	)
	if err != nil {
		return officialCodexRuntimeState{}, err
	}
	return resolveOfficialCodexRuntimeStateFromSnapshot(
		officialCodexIngressRuntimeSnapshot{
			OfficialClient: true, UserAgent: profile.Build.UserAgent,
			Originator: profile.Build.Originator, RequestCompressionEnabled: true,
		},
		nil, mode, codexEndpointID(endpointID),
	)
}

func officialCodexProcessIdentityFactsForReleaseMode(
	mode string,
	endpointID string,
) (officialegress.CodexIdentityFacts, error) {
	state, err := officialCodexRuntimeStateForReleaseMode(mode, endpointID)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts := officialegress.CodexIdentityFacts{UserAgentSuffixEnabled: state.UserAgentSuffixEnabled}
	facts.ProcessSurface, err = officialCodexIdentityValue(
		state.SurfaceID, officialegress.IdentitySourceProcess, officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.ProcessPhase, err = officialCodexIdentityValue(
		state.ProcessPhase, officialegress.IdentitySourceProcess, officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.TerminalToken, err = officialCodexIdentityValue(
		state.TerminalToken, officialegress.IdentitySourceProcess, officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	return facts, facts.Validate()
}

func projectOfficialCodexIdentityAccount(account *Account) officialCodexIdentityAccountProjection {
	if account == nil {
		return officialCodexIdentityAccountProjection{}
	}
	return officialCodexIdentityAccountProjection{
		ID: account.ID, ChatGPTAccountID: account.GetChatGPTAccountID(),
		FedRAMP: account.IsChatGPTAccountFedRAMP(),
	}
}

func prepareOfficialCodexSemanticAttempt(
	request *http.Request,
	body []byte,
	endpointID string,
	identitySeed string,
	account officialCodexIdentityAccountProjection,
) (officialCodexSemanticAttempt, error) {
	if request == nil || request.URL == nil || account.ID <= 0 {
		return officialCodexSemanticAttempt{}, errors.New("Codex 语义 attempt 输入不完整")
	}
	materializeOfficialCodexCookieJar(request)
	headers := request.Header.Clone()
	semanticBody, ownedFields, err := officialegress.PrepareOfficialCodexAttemptBody(endpointID, body)
	if err != nil {
		return officialCodexSemanticAttempt{}, err
	}
	metadata := ownedFields.Metadata
	bearer := strings.TrimSpace(headers.Get("Authorization"))
	agentIdentity := ""
	if bearer != "" {
		const prefix = "Bearer "
		if strings.HasPrefix(bearer, prefix) && strings.TrimSpace(strings.TrimPrefix(bearer, prefix)) != "" {
			bearer = strings.TrimSpace(strings.TrimPrefix(bearer, prefix))
		} else {
			agentIdentity = bearer
			bearer = ""
		}
	}
	refreshToken := ownedFields.RefreshToken
	attestation := headers.Get("X-Oai-Attestation")
	cookie := headers.Get("Cookie")
	authentication, err := officialegress.NewAttemptAuthentication(
		officialegress.AttemptAuthenticationInput{
			BearerToken: bearer, AgentIdentity: agentIdentity,
			Attestation: attestation,
			Cookie:      cookie, RefreshToken: refreshToken,
		},
	)
	if err != nil {
		return officialCodexSemanticAttempt{}, err
	}
	facts, err := buildOfficialCodexIdentityFacts(
		metadata, account, request, endpointID, identitySeed,
		officialCodexAttemptConditions{
			AttestationPresent:  strings.TrimSpace(attestation) != "",
			CookiePresent:       strings.TrimSpace(cookie) != "",
			CompressionEligible: headerContainsToken(headers, "Content-Encoding", "zstd"),
		},
	)
	if err != nil {
		return officialCodexSemanticAttempt{}, err
	}
	// 严格端点 Header 是画像闭集。当前 service 构造器生成的旧定型 Header 只作为
	// 结构化事实来源，绝不再以普通 Header 身份进入 compiler。
	semanticHeaders := make(http.Header)
	return officialCodexSemanticAttempt{
		Headers: semanticHeaders, Body: semanticBody,
		BodyConditions: officialegress.BodyRuntimeConditions{
			CreditIDPresent: strings.TrimSpace(headers.Get("credit-id")) != "",
		},
		IdentityFacts: facts, Authentication: authentication,
	}, nil
}

// materializeOfficialCodexCookieJar 在 Executor 生成 FinalizationToken 前把账号级
// Cloudflare Cookie 固化到本次语义请求。net/http 默认会在 Client.send 阶段才从 Jar
// 补 Cookie，该时点已经晚于 Executor 签名，terminal Guard 会将这种合法补充
// 误判为 request_modified_after_finalize。提前固化后，Cookie 的名称、值与顺序
// 都进入 Compiler 和最终请求摘要，Guard 仍保持 fail-close。
func materializeOfficialCodexCookieJar(request *http.Request) {
	if request == nil || request.URL == nil {
		return
	}
	jar := HTTPUpstreamCookieJarFromContext(request.Context())
	if jar == nil {
		return
	}
	for _, cookie := range jar.Cookies(request.URL) {
		if cookie != nil {
			request.AddCookie(cookie)
		}
	}
}

func buildOfficialCodexIdentityFacts(
	metadata map[string]string,
	account officialCodexIdentityAccountProjection,
	request *http.Request,
	endpointID string,
	identitySeed string,
	attemptConditions officialCodexAttemptConditions,
) (officialegress.CodexIdentityFacts, error) {
	facts := officialegress.CodexIdentityFacts{}
	structuredIdentity := officialCodexInvocationIdentityFromContext(request.Context())
	accountProjection := sha256.Sum256([]byte(fmt.Sprintf("openai-oauth-account:%d", account.ID)))
	var err error
	if facts.AccountIdentityProjection, err = officialCodexIdentityValue(
		hex.EncodeToString(accountProjection[:]), officialegress.IdentitySourceAccount,
		officialegress.IdentityLifecycleAccount,
	); err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.ChatGPTAccountID, err = officialCodexIdentityValue(
		account.ChatGPTAccountID, officialegress.IdentitySourceAccount,
		officialegress.IdentityLifecycleAccount,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	runtimeState := defaultOfficialCodexRuntimeState()
	egressContext, hasEgressContext := OfficialEgressContextFromContext(request.Context())
	if hasEgressContext {
		runtimeState = cloneOfficialCodexRuntimeState(egressContext.codexRuntimeState)
	} else if boundState, bound, stateErr := officialCodexRuntimeStateFromContext(request.Context()); stateErr != nil {
		return officialegress.CodexIdentityFacts{}, stateErr
	} else if bound {
		// 辅助端点可只携带已校验的运行态快照。它不是旧 Header finalizer，
		// compiler 仍是最终身份 Header 的唯一生成者。
		runtimeState = boundState
	}
	registeredField := func(name OfficialEgressFieldName) string {
		if !hasEgressContext {
			return ""
		}
		field, exists := egressContext.Field(name)
		if !exists {
			return ""
		}
		return strings.TrimSpace(field.Value())
	}
	conditionalField := func(name string) string {
		return strings.TrimSpace(runtimeState.ConditionalHeaders[strings.ToLower(name)])
	}
	facts.ProcessSurface, err = officialCodexIdentityValue(
		runtimeState.SurfaceID, officialegress.IdentitySourceProcess,
		officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.ProcessPhase, err = officialCodexIdentityValue(
		runtimeState.ProcessPhase, officialegress.IdentitySourceProcess,
		officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.TerminalToken, err = officialCodexIdentityValue(
		runtimeState.TerminalToken, officialegress.IdentitySourceProcess,
		officialegress.IdentityLifecycleProcess,
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	facts.UserAgentSuffixEnabled = runtimeState.UserAgentSuffixEnabled
	resolveStructured := func(name string, structured string, fallbacks ...string) (string, error) {
		return resolveOfficialCodexStructuredIdentity(name, structured, fallbacks...)
	}
	installationID, err := resolveStructured(
		"installation_id", structuredIdentity.InstallationID,
		registeredField(OfficialEgressFieldDeviceID), metadata["x-codex-installation-id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	sessionID, err := resolveStructured(
		"session_id", structuredIdentity.SessionID,
		registeredField(OfficialEgressFieldSessionID), metadata["session_id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	conversationID, err := resolveStructured(
		"conversation_id", structuredIdentity.ConversationID, metadata["conversation_id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	threadID, err := resolveStructured(
		"thread_id", structuredIdentity.ThreadID,
		registeredField(OfficialEgressFieldThreadID), metadata["thread_id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	windowID, err := resolveStructured(
		"window_id", structuredIdentity.WindowID,
		registeredField(OfficialEgressFieldWindowID), metadata["x-codex-window-id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	clientRequestID, err := resolveStructured(
		"client_request_id", structuredIdentity.ClientRequestID,
		registeredField(OfficialEgressFieldClientRequestID), metadata["x-client-request-id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	turnID, err := resolveStructured("turn_id", structuredIdentity.TurnID, metadata["turn_id"])
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	turnMetadata, err := resolveStructured(
		"turn_metadata", structuredIdentity.TurnMetadata,
		registeredField(OfficialEgressFieldTurnMetadata), metadata["x-codex-turn-metadata"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	turnState, err := resolveStructured(
		"turn_state", structuredIdentity.TurnState,
		registeredField(OfficialEgressFieldTurnState),
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	parentThreadID, err := resolveStructured(
		"parent_thread_id", structuredIdentity.ParentThreadID,
		conditionalField("x-codex-parent-thread-id"), metadata["x-codex-parent-thread-id"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	subagent, err := resolveStructured(
		"subagent", structuredIdentity.Subagent,
		conditionalField("x-openai-subagent"), metadata["x-openai-subagent"],
	)
	if err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	values := []struct {
		target    *officialegress.CodexIdentityValue
		value     string
		source    officialegress.IdentityFactSource
		lifecycle officialegress.IdentityFactLifecycle
	}{
		{&facts.InstallationID, installationID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleInvocation},
		{&facts.SessionID, sessionID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.ConversationID, conversationID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.ThreadID, threadID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.WindowID, windowID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.ClientRequestID, clientRequestID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleInvocation},
		{&facts.TurnID, turnID, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		{&facts.TurnMetadata, turnMetadata, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		// turn-state 只能来自本次调用内已登记的上游重试状态。客户端即使提供
		// 同名保护头也不能取得该身份槽位的所有权。
		{&facts.TurnState, turnState, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		{&facts.ParentThreadID, parentThreadID, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.Subagent, subagent, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		{&facts.ManagedResidency, conditionalField("x-openai-internal-codex-residency"), officialegress.IdentitySourceManagedConfig, officialegress.IdentityLifecycleProcess},
	}
	for _, value := range values {
		*value.target, err = officialCodexIdentityValue(
			value.value, value.source, value.lifecycle,
		)
		if err != nil {
			return officialegress.CodexIdentityFacts{}, err
		}
	}
	if err := completeOfficialCodexGeneratedIdentityFacts(
		&facts, endpointID, identitySeed,
	); err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	compressionEligible := attemptConditions.CompressionEligible || runtimeState.RequestCompressionEnabled
	if hasEgressContext {
		// 0.145/0.147 的 Responses HTTP 压缩能力由模型清单的 Lite 条件
		// 门控；画像默认值只表示客户端支持 zstd，不能让普通 Responses 请求
		// 也携带压缩头。入站已有 zstd 事实同样必须经过同一 Lite 门控。
		compressionEligible = egressContext.responsesLite && compressionEligible
	}
	facts.Conditions = officialegress.CodexRequestConditions{
		TurnStatePresent:        facts.TurnState.Value != "",
		SubagentPresent:         facts.Subagent.Value != "",
		MemoryGeneration:        strings.EqualFold(conditionalField("x-openai-memgen-request"), "true"),
		ParentThreadPresent:     facts.ParentThreadID.Value != "",
		SessionIDPresent:        facts.SessionID.Value != "",
		AttestationPresent:      attemptConditions.AttestationPresent,
		FedRAMPAccount:          account.FedRAMP,
		ManagedResidencyPresent: facts.ManagedResidency.Value != "",
		CookiePresent:           attemptConditions.CookiePresent,
		CompressionEligible:     compressionEligible,
		ModelSupportsLite:       hasEgressContext && egressContext.responsesLite,
		BetaFeaturesPresent:     conditionalField("x-codex-beta-features") != "",
	}
	managedRaw, _ := json.Marshal(struct {
		Residency string
		FedRAMP   bool
	}{facts.ManagedResidency.Value, facts.Conditions.FedRAMPAccount})
	managedDigest := sha256.Sum256(managedRaw)
	facts.ManagedConfigurationDigest = hex.EncodeToString(managedDigest[:])
	if err := facts.Validate(); err != nil {
		return officialegress.CodexIdentityFacts{}, err
	}
	return facts, nil
}

func completeOfficialCodexGeneratedIdentityFacts(
	facts *officialegress.CodexIdentityFacts,
	endpointID string,
	identitySeed string,
) error {
	if facts == nil {
		return errors.New("Codex 身份事实为空")
	}
	identitySeed = strings.TrimSpace(identitySeed)
	if identitySeed == "" {
		return errors.New("Codex 身份事实缺少 invocation seed")
	}
	needsResponsesSession := endpointID == officialCodexEndpointResponsesHTTP ||
		endpointID == officialCodexEndpointResponsesCompact ||
		endpointID == officialCodexEndpointResponsesWS
	needsTurnMetadata := needsResponsesSession || endpointID == officialCodexEndpointAlphaSearch
	setGenerated := func(target *officialegress.CodexIdentityValue, suffix string, lifecycle officialegress.IdentityFactLifecycle) error {
		if target.Value != "" {
			return nil
		}
		sum := sha256.Sum256([]byte(identitySeed + "\x00" + suffix))
		value := fmt.Sprintf(
			"%s-%s-%s-%s-%s",
			hex.EncodeToString(sum[0:4]), hex.EncodeToString(sum[4:6]),
			hex.EncodeToString(sum[6:8]), hex.EncodeToString(sum[8:10]),
			hex.EncodeToString(sum[10:16]),
		)
		generated, err := officialegress.NewCodexIdentityValue(
			value, officialegress.IdentitySourceInvocation, lifecycle,
		)
		if err != nil {
			return err
		}
		*target = generated
		return nil
	}
	if needsResponsesSession {
		for _, field := range []struct {
			target    *officialegress.CodexIdentityValue
			suffix    string
			lifecycle officialegress.IdentityFactLifecycle
		}{
			{&facts.InstallationID, "installation", officialegress.IdentityLifecycleInvocation},
			{&facts.SessionID, "session", officialegress.IdentityLifecycleSession},
			{&facts.ThreadID, "thread", officialegress.IdentityLifecycleSession},
			{&facts.WindowID, "window", officialegress.IdentityLifecycleSession},
			{&facts.TurnID, "turn", officialegress.IdentityLifecycleTurn},
		} {
			if err := setGenerated(field.target, field.suffix, field.lifecycle); err != nil {
				return err
			}
		}
	}
	if needsResponsesSession || endpointID == officialCodexEndpointFilesBlobUpload {
		if err := setGenerated(
			&facts.ClientRequestID, "client-request", officialegress.IdentityLifecycleInvocation,
		); err != nil {
			return err
		}
	}
	if needsTurnMetadata && facts.TurnMetadata.Value == "" {
		turnMetadata, err := json.Marshal(struct {
			InstallationID string `json:"installation_id"`
			SessionID      string `json:"session_id"`
			ThreadID       string `json:"thread_id"`
			TurnID         string `json:"turn_id"`
			WindowID       string `json:"window_id"`
			RequestKind    string `json:"request_kind"`
			ThreadSource   string `json:"thread_source"`
			Sandbox        string `json:"sandbox"`
		}{
			InstallationID: facts.InstallationID.Value,
			SessionID:      facts.SessionID.Value, ThreadID: facts.ThreadID.Value,
			TurnID: facts.TurnID.Value, WindowID: facts.WindowID.Value,
			RequestKind: "turn", ThreadSource: "user", Sandbox: "seccomp",
		})
		if err != nil {
			return err
		}
		facts.TurnMetadata, err = officialegress.NewCodexIdentityValue(
			string(turnMetadata), officialegress.IdentitySourceTurn,
			officialegress.IdentityLifecycleTurn,
		)
		if err != nil {
			return err
		}
	}
	return nil
}

func officialCodexIdentityValue(
	value string,
	source officialegress.IdentityFactSource,
	lifecycle officialegress.IdentityFactLifecycle,
) (officialegress.CodexIdentityValue, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return officialegress.CodexIdentityValue{}, nil
	}
	return officialegress.NewCodexIdentityValue(value, source, lifecycle)
}

func firstOfficialCodexIdentityValue(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

// resolveOfficialCodexStructuredIdentity 在专用结构化事实与其他受信来源同时存在时
// 要求值完全一致；普通 Header 不参与此解析。这样错误结构化身份会 fail-close，
// 而不是静默覆盖 ingress/runtime 已冻结的事实。
func resolveOfficialCodexStructuredIdentity(
	name string,
	structured string,
	fallbacks ...string,
) (string, error) {
	structured = strings.TrimSpace(structured)
	if structured == "" {
		return firstOfficialCodexIdentityValue(fallbacks...), nil
	}
	for _, fallback := range fallbacks {
		fallback = strings.TrimSpace(fallback)
		if fallback != "" && fallback != structured {
			return "", fmt.Errorf("结构化 Codex 身份 %s 与受信来源冲突", name)
		}
	}
	return structured, nil
}
