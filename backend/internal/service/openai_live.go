package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/platform/liveattestation"
	coderws "github.com/coder/websocket"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
	"go.uber.org/zap"
)

const (
	defaultLiveMaxSessionDuration = time.Hour
	liveLeaseRefreshInterval      = 20 * time.Second
	liveRedisOperationTimeout     = 3 * time.Second
	liveClosedRecordTTL           = 24 * time.Hour
	liveObserverPollInterval      = 250 * time.Millisecond
	liveUpstreamBodyLimit         = 2 << 20
)

type liveFrameConn interface {
	ReadFrame(ctx context.Context) (coderws.MessageType, []byte, error)
	WriteFrame(ctx context.Context, msgType coderws.MessageType, payload []byte) error
	Close() error
}

// liveSidebandClientDialer 标记专用于 realtime sideband 的无压缩握手。
// 生产 dialer 必须实现此接口；测试注入的旧 dialer 可继续走通用 Dial。
type liveSidebandClientDialer interface {
	DialLiveSideband(
		ctx context.Context,
		wsURL string,
		headers http.Header,
		proxyURL string,
	) (openAIWSClientConn, int, http.Header, error)
}

// DialLiveSideband 使用 Codex 0.145.0 WS TLS 画像建立 realtime sideband，且显式
// 禁用 permessage-deflate。Responses WS 仍由通用 Dial 使用上下文接管压缩，两者不能
// 共享握手选项。
func (d *coderOpenAIWSClientDialer) DialLiveSideband(
	ctx context.Context,
	wsURL string,
	headers http.Header,
	proxyURL string,
) (openAIWSClientConn, int, http.Header, error) {
	if d == nil {
		return nil, 0, nil, errors.New("live sideband dialer is nil")
	}
	targetURL := strings.TrimSpace(wsURL)
	if targetURL == "" {
		return nil, 0, nil, errors.New("live sideband url is empty")
	}
	tlsProfile := newOpenAIOfficialEgressWebSocketTLSProfile()
	if egressContext, enabled := OfficialEgressContextFromContext(ctx); enabled {
		if _, err := resolveOfficialEgressWebSocketTransportProfile(egressContext); err != nil {
			return nil, 0, nil, err
		}
		parsedTarget, err := url.Parse(targetURL)
		if err != nil {
			return nil, 0, nil, fmt.Errorf("解析 Live sideband URL：%w", err)
		}
		if err := validateOfficialEgressWebSocketTarget(parsedTarget, egressContext); err != nil {
			return nil, 0, nil, err
		}
		tlsProfile, err = officialCodex0145ResolveEndpointTLSProfileForURL(
			egressContext.ProfileVersion(),
			codex0145EndpointID(egressContext.CodexEndpointProfileID()),
			parsedTarget,
		)
		if err != nil {
			return nil, 0, nil, fmt.Errorf("解析 Live sideband TLS 画像：%w", err)
		}
	}
	transport, err := buildOpenAIOfficialEgressWSTransport(
		tlsProfile,
		proxyURL,
	)
	if err != nil {
		return nil, 0, nil, err
	}
	httpClient := &http.Client{
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	connection, response, err := coderws.Dial(ctx, targetURL, &coderws.DialOptions{
		HTTPClient:      httpClient,
		HTTPHeader:      cloneHeader(headers),
		CompressionMode: coderws.CompressionDisabled,
	})
	if err != nil {
		status := 0
		responseHeaders := http.Header(nil)
		var responseBody []byte
		if response != nil {
			status = response.StatusCode
			responseHeaders = cloneHeader(response.Header)
			if response.Body != nil {
				responseBody, _ = io.ReadAll(io.LimitReader(response.Body, 8<<10))
				_ = response.Body.Close()
			}
		}
		transport.CloseIdleConnections()
		return nil, status, responseHeaders, &openAIWSHandshakeError{Body: responseBody, Err: err}
	}
	connection.SetReadLimit(openAIWSMessageReadLimitBytes)
	responseHeaders := http.Header(nil)
	if response != nil {
		responseHeaders = cloneHeader(response.Header)
	}
	return &coderOpenAIWSClientConn{conn: connection}, http.StatusSwitchingProtocols, responseHeaders, nil
}

func liveSidebandReadError(err error) error {
	if coderws.CloseStatus(err) == coderws.StatusNormalClosure {
		return ErrLiveCallNotFound
	}
	return err
}

func hashLiveCallID(callID string) string {
	sum := sha256.Sum256([]byte(callID))
	return hex.EncodeToString(sum[:])
}

// liveRealtimeSessionID 从持久化的 Live 租约生成稳定 UUID。第一跳与后续 observer／
// proxy 重连都使用同一个 x-session-id，同时不需要扩展 Redis 记录结构。
func liveRealtimeSessionID(leaseID string) string {
	seed := strings.TrimSpace(leaseID)
	if seed == "" {
		seed = "missing-live-lease"
	}
	return uuid.NewSHA1(uuid.NameSpaceURL, []byte("sub2api/live/realtime-session/"+seed)).String()
}

func liveGroupID(groupID *int64) int64 {
	if groupID == nil {
		return 0
	}
	return *groupID
}

func liveOptionalID(value int64) *int64 {
	if value <= 0 {
		return nil
	}
	result := value
	return &result
}

func (s *OpenAIGatewayService) liveStore() (LiveCallStore, error) {
	if s == nil || s.cache == nil {
		return nil, ErrLiveUnavailable
	}
	store, ok := s.cache.(LiveCallStore)
	if !ok {
		return nil, ErrLiveUnavailable
	}
	return store, nil
}

func (s *OpenAIGatewayService) liveConcurrencyCache() (LiveConcurrencyCache, error) {
	if s == nil || s.concurrencyService == nil || s.concurrencyService.cache == nil {
		return nil, ErrLiveUnavailable
	}
	cache, ok := s.concurrencyService.cache.(LiveConcurrencyCache)
	if !ok {
		return nil, ErrLiveUnavailable
	}
	return cache, nil
}

func (s *OpenAIGatewayService) liveMaxSessionDuration() time.Duration {
	if s != nil && s.cfg != nil && s.cfg.Gateway.Live.MaxSessionDurationSeconds > 0 {
		return time.Duration(s.cfg.Gateway.Live.MaxSessionDurationSeconds) * time.Second
	}
	return defaultLiveMaxSessionDuration
}

func ValidateLiveCallRequest(request *LiveCallRequest) error {
	if request == nil || strings.TrimSpace(request.SDP) == "" {
		return errors.New("sdp is required")
	}
	if len(request.Session) == 0 || !json.Valid(request.Session) {
		return errors.New("session must be valid JSON")
	}
	var sessionObject map[string]json.RawMessage
	if err := json.Unmarshal(request.Session, &sessionObject); err != nil {
		return errors.New("session must be a JSON object")
	}
	if sessionObject == nil {
		return errors.New("session must be a JSON object")
	}
	return nil
}

// CreateLiveCall 创建 Frameless 会话。调用方须在调用期间持有普通用户槽位；
// 调度器持有的普通账号槽位会被同一个 Live 租约原子接替。
func (s *OpenAIGatewayService) CreateLiveCall(
	ctx context.Context,
	request *LiveCallRequest,
	identity LiveCallIdentity,
	userMaxConcurrency int,
) (*LiveCallCreated, error) {
	if err := ValidateLiveCallRequest(request); err != nil {
		return nil, err
	}
	store, err := s.liveStore()
	if err != nil {
		return nil, err
	}
	liveCache, err := s.liveConcurrencyCache()
	if err != nil {
		return nil, err
	}
	excluded := make(map[int64]struct{})
	var lastErr error
	for attempt := 0; attempt <= 3; attempt++ {
		selection, _, selectErr := s.SelectAccountWithSchedulerForCapability(
			ctx,
			identity.GroupID,
			"",
			uuid.NewString(),
			"",
			excluded,
			OpenAIUpstreamTransportHTTPSSE,
			OpenAIEndpointCapabilityLive,
			false,
			false,
			false,
		)
		if selectErr != nil {
			if lastErr != nil {
				return nil, lastErr
			}
			return nil, selectErr
		}
		if selection == nil || selection.Account == nil || !selection.Acquired {
			if selection != nil && selection.ReleaseFunc != nil {
				selection.ReleaseFunc()
			}
			return nil, ErrLiveConcurrencyFull
		}

		account := selection.Account
		proxyIsolated := account.ProxyID != nil && account.Proxy != nil &&
			*account.ProxyID == account.Proxy.ID && account.ProxyFallbackOriginID == nil &&
			account.Proxy.Protocol == "http" && account.Proxy.Status == StatusActive &&
			account.Proxy.FallbackMode == FallbackModeNone && account.Proxy.BackupProxyID == nil
		var proxyID int64
		var proxyName, proxyHost string
		var proxyPort int
		if account.ProxyID != nil && account.Proxy != nil {
			proxyID = *account.ProxyID
			proxyName = account.Proxy.Name
			proxyHost = account.Proxy.Host
			proxyPort = account.Proxy.Port
		}
		attestationContext := liveattestation.WithCandidateCaptureScope(
			ctx,
			liveattestation.CandidateCaptureScope{
				APIKeyID:      identity.APIKeyID,
				GroupID:       liveGroupID(identity.GroupID),
				AccountID:     account.ID,
				ProxyID:       proxyID,
				ProxyName:     proxyName,
				ProxyHost:     proxyHost,
				ProxyPort:     proxyPort,
				ProxyIsolated: proxyIsolated,
			},
		)
		attestation, attestationCiphertext, attestationErr := s.prepareLiveAttestation(attestationContext)
		if attestationErr != nil {
			selection.ReleaseFunc()
			return nil, attestationErr
		}
		attemptContext, runtimeState, runtimeErr := bindOfficialCodex0145RuntimeStateFromCapturedIngress(
			ctx,
			account,
			codex0145EndpointID(officialCodexEndpointRealtimeCalls),
		)
		if runtimeErr != nil {
			selection.ReleaseFunc()
			return nil, fmt.Errorf("解析 Live 进程画像：%w", runtimeErr)
		}
		leaseID := generateRequestID()
		realtimeSessionID := liveRealtimeSessionID(leaseID)
		acquired, acquireErr := liveCache.AcquireLiveLease(
			ctx,
			account.ID,
			account.Concurrency,
			identity.UserID,
			userMaxConcurrency,
			identity.APIKeyID,
			leaseID,
			true,
		)
		if acquireErr != nil || !acquired {
			selection.ReleaseFunc()
			if acquireErr != nil {
				return nil, acquireErr
			}
			return nil, ErrLiveConcurrencyFull
		}

		created, createErr := s.createUpstreamLiveCall(
			attemptContext,
			account,
			request,
			attestation,
			realtimeSessionID,
		)
		selection.ReleaseFunc()
		if createErr != nil {
			s.releaseLiveLease(account.ID, identity.UserID, identity.APIKeyID, leaseID)
			if !s.shouldFailoverLiveCreateError(createErr) {
				return nil, createErr
			}
			excluded[account.ID] = struct{}{}
			lastErr = createErr
			continue
		}

		now := time.Now()
		model := strings.TrimSpace(gjson.GetBytes(request.Session, "model").String())
		if model == "" {
			model = "gpt-live"
		}
		record := &LiveCallRecord{
			CallID:                created.CallID,
			CallHash:              hashLiveCallID(created.CallID),
			AccountID:             account.ID,
			APIKeyID:              identity.APIKeyID,
			UserID:                identity.UserID,
			GroupID:               liveGroupID(identity.GroupID),
			SubscriptionID:        liveGroupID(identity.SubscriptionID),
			LeaseID:               leaseID,
			Model:                 model,
			CreatedAt:             now,
			ExpiresAt:             now.Add(s.liveMaxSessionDuration()),
			Controller:            LiveControllerPending,
			UserAgent:             identity.UserAgent,
			IPAddress:             identity.IPAddress,
			InboundEndpoint:       identity.InboundEndpoint,
			CodexRuntimeState:     liveCodexRuntimeStateFromOfficial(runtimeState),
			AttestationCiphertext: attestationCiphertext,
		}
		mappingTTL := s.liveMaxSessionDuration() + 5*time.Minute
		if saveErr := store.SaveLiveCall(ctx, record, mappingTTL); saveErr != nil {
			s.releaseLiveLease(account.ID, identity.UserID, identity.APIKeyID, leaseID)
			return nil, fmt.Errorf("save live call mapping: %w", saveErr)
		}
		created.Account = account
		go s.observeLiveCall(record.CallHash)
		return created, nil
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, ErrLiveUnavailable
}

func (s *OpenAIGatewayService) shouldFailoverLiveCreateError(err error) bool {
	var upstreamErr *UpstreamFailoverError
	if !errors.As(err, &upstreamErr) {
		// 凭证读取和网络传输错误都可能只影响当前账号或代理。
		return true
	}
	return s.shouldFailoverOpenAIUpstreamResponse(
		upstreamErr.StatusCode,
		"",
		upstreamErr.ResponseBody,
	)
}

func (s *OpenAIGatewayService) createUpstreamLiveCall(
	ctx context.Context,
	account *Account,
	request *LiveCallRequest,
	attestation string,
	realtimeSessionID string,
) (*LiveCallCreated, error) {
	endpointID := codex0145EndpointID(officialCodexEndpointRealtimeCalls)
	endpoint, err := resolveCodex0145Endpoint(officialCodexVersion0145, endpointID)
	if err != nil {
		return nil, err
	}
	target, err := officialCodex0145BuildEndpointURL(
		officialCodexVersion0145,
		endpointID,
		officialCodex0145EndpointURLInput{},
	)
	if err != nil {
		return nil, err
	}
	token, _, err := s.GetAccessToken(ctx, account)
	if err != nil {
		logLiveCreateStageFailure(ctx, account.ID, "access_token", err)
		return nil, err
	}
	body, err := json.Marshal(struct {
		SDP     string          `json:"sdp"`
		Session json.RawMessage `json:"session"`
	}{
		SDP:     request.SDP,
		Session: request.Session,
	})
	if err != nil {
		return nil, err
	}
	reqCtx := WithHTTPUpstreamProfile(ctx, HTTPUpstreamProfileOpenAI)
	upstreamReq, err := http.NewRequestWithContext(reqCtx, endpoint.Method, target.String(), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	upstreamReq, egressContext, err := attachOfficialCodex0145EndpointRequest(
		upstreamReq,
		account,
		endpointID,
		realtimeSessionID,
	)
	if err != nil {
		return nil, fmt.Errorf("绑定 Live 首跳画像：%w", err)
	}
	body, err = officialCodex0145FinalizeEndpointJSONBody(egressContext, body, nil)
	if err != nil {
		return nil, fmt.Errorf("定型 Live 首跳 body：%w", err)
	}
	resetOfficialEgressRequestBody(upstreamReq, body)
	authHeaders, err := s.buildOpenAIAuthenticationHeaders(ctx, account, token)
	if err != nil {
		logLiveCreateStageFailure(ctx, account.ID, "authentication_headers", err)
		return nil, err
	}
	for key, values := range authHeaders {
		for _, value := range values {
			upstreamReq.Header.Add(key, value)
		}
	}
	if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, upstreamReq.Header, account); err != nil {
		logLiveCreateStageFailure(ctx, account.ID, "account_headers", err)
		return nil, err
	}
	upstreamReq.Header.Set("x-session-id", realtimeSessionID)
	if strings.TrimSpace(attestation) != "" {
		upstreamReq.Header.Set(liveAttestationHeader, attestation)
	}
	userAgent, originator, err := officialCodex0145ProcessIdentity(egressContext)
	if err != nil {
		return nil, fmt.Errorf("解析 Live 首跳进程身份：%w", err)
	}
	upstreamReq.Header.Set("user-agent", userAgent)
	upstreamReq.Header.Set("originator", originator)
	if _, err := officialCodex0145FinalizeEndpointHeaders(egressContext, upstreamReq.Header, nil); err != nil {
		return nil, fmt.Errorf("定型 Live 首跳 header：%w", err)
	}
	tlsProfile, err := officialCodex0145ResolveEndpointTLSProfileForURL(
		egressContext.ProfileVersion(),
		endpointID,
		upstreamReq.URL,
	)
	if err != nil {
		return nil, fmt.Errorf("解析 Live 首跳 TLS 画像：%w", err)
	}

	resp, err := s.httpUpstream.DoWithTLS(
		upstreamReq,
		resolveAccountProxyURL(account),
		account.ID,
		account.Concurrency,
		tlsProfile,
	)
	if err != nil {
		logLiveCreateStageFailure(ctx, account.ID, "upstream_transport", err)
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	responseBody, readErr := io.ReadAll(io.LimitReader(resp.Body, liveUpstreamBodyLimit+1))
	if readErr != nil {
		return nil, readErr
	}
	if len(responseBody) > liveUpstreamBodyLimit {
		return nil, errors.New("live upstream response is too large")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		logLiveUpstreamFailure(ctx, account.ID, resp.StatusCode, resp.Header, responseBody)
		return nil, &UpstreamFailoverError{
			StatusCode:      resp.StatusCode,
			ResponseBody:    responseBody,
			ResponseHeaders: resp.Header.Clone(),
		}
	}
	callID, err := liveCallIDFromLocation(resp.Header.Get("Location"))
	if err != nil {
		return nil, err
	}
	return &LiveCallCreated{
		SDP:      responseBody,
		CallID:   callID,
		Location: resp.Header.Get("Location"),
	}, nil
}

func logLiveCreateStageFailure(ctx context.Context, accountID int64, stage string, err error) {
	logger.FromContext(ctx).Warn(
		"OpenAI Live 创建阶段失败",
		zap.Int64("account_id", accountID),
		zap.String("stage", stage),
		zap.String("error_type", fmt.Sprintf("%T", err)),
	)
}

func logLiveUpstreamFailure(
	ctx context.Context,
	accountID int64,
	statusCode int,
	headers http.Header,
	body []byte,
) {
	errorType := strings.TrimSpace(gjson.GetBytes(body, "error.type").String())
	errorCode := strings.TrimSpace(gjson.GetBytes(body, "error.code").String())
	errorMessage := strings.TrimSpace(gjson.GetBytes(body, "error.message").String())
	if errorType == "" {
		errorType = strings.TrimSpace(gjson.GetBytes(body, "type").String())
	}
	if errorCode == "" {
		errorCode = strings.TrimSpace(gjson.GetBytes(body, "code").String())
	}
	if errorMessage == "" {
		errorMessage = strings.TrimSpace(gjson.GetBytes(body, "message").String())
	}
	if errorMessage == "" {
		errorMessage = strings.TrimSpace(gjson.GetBytes(body, "detail").String())
	}

	logger.FromContext(ctx).Warn(
		"OpenAI Live 上游拒绝请求",
		zap.Int64("account_id", accountID),
		zap.Int("upstream_status_code", statusCode),
		zap.String("upstream_error_type", truncateOpenAIWSLogValue(errorType, 120)),
		zap.String("upstream_error_code", truncateOpenAIWSLogValue(errorCode, 120)),
		zap.String("upstream_error_message", truncateOpenAIWSLogValue(errorMessage, 300)),
		zap.String("upstream_content_type", truncateOpenAIWSLogValue(headers.Get("Content-Type"), 120)),
		zap.String("upstream_server", truncateOpenAIWSLogValue(headers.Get("Server"), 120)),
		zap.String("upstream_cf_mitigated", truncateOpenAIWSLogValue(headers.Get("Cf-Mitigated"), 120)),
		zap.String("upstream_cf_ray", truncateOpenAIWSLogValue(headers.Get("Cf-Ray"), 120)),
		zap.String("upstream_request_id", truncateOpenAIWSLogValue(headers.Get("X-Request-Id"), 120)),
	)
}

func liveCallIDFromLocation(location string) (string, error) {
	location = strings.TrimSpace(location)
	if location == "" {
		return "", errors.New("live upstream response has no Location")
	}
	parsed, err := url.Parse(location)
	if err != nil {
		return "", fmt.Errorf("parse live Location: %w", err)
	}
	callID := strings.TrimSpace(path.Base(strings.TrimSuffix(parsed.Path, "/")))
	if callID == "" || callID == "." || callID == "codex" {
		return "", errors.New("live upstream Location has no call id")
	}
	return callID, nil
}

func (s *OpenAIGatewayService) liveSidebandHeaders(
	ctx context.Context,
	account *Account,
	egressContext *OfficialEgressContext,
	record *LiveCallRecord,
) (http.Header, error) {
	if account == nil || record == nil {
		return nil, ErrLiveCallNotFound
	}
	token, _, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return nil, err
	}
	authenticationHeaders, err := s.buildOpenAIAuthenticationHeaders(ctx, account, token)
	if err != nil {
		return nil, err
	}
	headers := cloneHeader(authenticationHeaders)
	if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, headers, account); err != nil {
		return nil, err
	}
	attestation, err := s.decryptLiveAttestation(record)
	if err != nil {
		return nil, err
	}
	userAgent, originator, err := officialCodex0145ProcessIdentity(egressContext)
	if err != nil {
		return nil, err
	}
	headers.Set("x-session-id", liveRealtimeSessionID(record.LeaseID))
	headers.Set(liveAttestationHeader, attestation)
	headers.Set("user-agent", userAgent)
	headers.Set("originator", originator)
	if _, err := officialCodex0145FinalizeEndpointHeaders(egressContext, headers, nil); err != nil {
		return nil, fmt.Errorf("定型 Live sideband header：%w", err)
	}
	return headers, nil
}

func (s *OpenAIGatewayService) dialLiveSideband(ctx context.Context, record *LiveCallRecord) (liveFrameConn, error) {
	account, err := s.accountRepo.GetByID(ctx, record.AccountID)
	if err != nil {
		return nil, err
	}
	if account == nil || !account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityLive) {
		return nil, ErrLiveUnavailable
	}
	ctx, _, err = restoreLiveCodexRuntimeState(ctx, account, record)
	if err != nil {
		return nil, fmt.Errorf("恢复 Live 进程画像：%w", err)
	}
	endpointID := codex0145EndpointID(officialCodexEndpointRealtimeSideband)
	target, err := officialCodex0145BuildEndpointURL(
		officialCodexVersion0145,
		endpointID,
		officialCodex0145EndpointURLInput{
			QueryValues: map[string]string{"call_id": record.CallID},
		},
	)
	if err != nil {
		return nil, err
	}
	wsContext, egressContext, err := attachOfficialCodex0145EndpointWebSocketContext(
		ctx,
		account,
		endpointID,
		target.String(),
	)
	if err != nil {
		return nil, fmt.Errorf("绑定 Live sideband 画像：%w", err)
	}
	headers, err := s.liveSidebandHeaders(wsContext, account, egressContext, record)
	if err != nil {
		return nil, err
	}
	dialer := s.getOpenAIWSPassthroughDialer()
	if dialer == nil {
		return nil, errors.New("live sideband dialer is unavailable")
	}
	var conn openAIWSClientConn
	var status int
	if specialized, ok := dialer.(liveSidebandClientDialer); ok {
		conn, status, _, err = specialized.DialLiveSideband(
			wsContext,
			target.String(),
			headers,
			resolveAccountProxyURL(account),
		)
	} else {
		// 兼容测试注入的最小 dialer；生产默认实现始终走上面的无压缩分支。
		conn, status, _, err = dialer.Dial(wsContext, target.String(), headers, resolveAccountProxyURL(account))
	}
	if err != nil {
		return nil, fmt.Errorf("dial live sideband (status %d): %w", status, err)
	}
	raw, ok := conn.(liveFrameConn)
	if !ok {
		_ = conn.Close()
		return nil, errors.New("live sideband transport does not support raw frames")
	}
	return raw, nil
}

func liveCodexRuntimeStateFromOfficial(state officialCodex0145RuntimeState) LiveCodexRuntimeState {
	cloned := cloneOfficialCodex0145RuntimeState(state)
	return LiveCodexRuntimeState{
		SurfaceID:              cloned.SurfaceID,
		ProcessPhase:           cloned.ProcessPhase,
		Originator:             cloned.Originator,
		TerminalToken:          cloned.TerminalToken,
		UserAgentSuffixEnabled: cloned.UserAgentSuffixEnabled,
		ConditionalHeaders:     cloned.ConditionalHeaders,
	}
}

func officialCodexRuntimeStateFromLive(state LiveCodexRuntimeState) officialCodex0145RuntimeState {
	return cloneOfficialCodex0145RuntimeState(officialCodex0145RuntimeState{
		SurfaceID:              state.SurfaceID,
		ProcessPhase:           state.ProcessPhase,
		Originator:             state.Originator,
		TerminalToken:          state.TerminalToken,
		UserAgentSuffixEnabled: state.UserAgentSuffixEnabled,
		ConditionalHeaders:     state.ConditionalHeaders,
	})
}

func restoreLiveCodexRuntimeState(
	ctx context.Context,
	account *Account,
	record *LiveCallRecord,
) (context.Context, officialCodex0145RuntimeState, error) {
	var runtimeState officialCodex0145RuntimeState
	var err error
	if record != nil && strings.TrimSpace(record.CodexRuntimeState.SurfaceID) != "" {
		runtimeState = officialCodexRuntimeStateFromLive(record.CodexRuntimeState)
	} else {
		// 兼容更新前已写入 Redis 的短生命周期记录；新记录必须走完整快照。
		runtimeState, err = resolveOfficialCodex0145RuntimeState(nil, account)
		if err != nil {
			return nil, officialCodex0145RuntimeState{}, err
		}
	}
	bound, err := withOfficialCodex0145RuntimeState(ctx, runtimeState)
	if err != nil {
		return nil, officialCodex0145RuntimeState{}, err
	}
	return bound, runtimeState, nil
}

func (s *OpenAIGatewayService) GetLiveCallForIdentity(
	ctx context.Context,
	callID string,
	identity LiveCallIdentity,
) (*LiveCallRecord, error) {
	store, err := s.liveStore()
	if err != nil {
		return nil, err
	}
	record, err := store.GetLiveCall(ctx, hashLiveCallID(callID))
	if err != nil {
		return nil, err
	}
	if record.CallID != callID ||
		record.APIKeyID != identity.APIKeyID ||
		record.UserID != identity.UserID ||
		record.GroupID != liveGroupID(identity.GroupID) {
		return nil, ErrLiveIdentityMismatch
	}
	if record.Controller == LiveControllerClosed {
		return nil, ErrLiveCallNotFound
	}
	return record, nil
}

// ProxyLiveSideband 让认证后的客户端接管控制连接；媒体始终不经过这里。
func (s *OpenAIGatewayService) ProxyLiveSideband(
	ctx context.Context,
	record *LiveCallRecord,
	downstream *coderws.Conn,
) error {
	if record == nil || downstream == nil {
		return ErrLiveCallNotFound
	}
	store, err := s.liveStore()
	if err != nil {
		return err
	}
	owner := uuid.NewString()
	claimed, err := store.ClaimLiveController(ctx, record.CallHash, LiveControllerProxy, owner)
	if err != nil {
		return err
	}
	if !claimed {
		return ErrLiveControllerChanged
	}

	// observer 轮询到接管状态后会关闭旧控制连接；同一个 call 可重新加入。
	time.Sleep(liveObserverPollInterval)
	upstream, err := s.dialLiveSideband(ctx, record)
	if err != nil {
		_, _ = store.ReleaseLiveController(context.Background(), record.CallHash, owner)
		go s.observeLiveCall(record.CallHash)
		return err
	}
	defer func() { _ = upstream.Close() }()
	downstream.SetReadLimit(openAIWSMessageReadLimitBytes)

	proxyCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	errCh := make(chan error, 2)
	go func() {
		for {
			messageType, payload, readErr := downstream.Read(proxyCtx)
			if readErr != nil {
				errCh <- readErr
				return
			}
			if writeErr := upstream.WriteFrame(proxyCtx, messageType, payload); writeErr != nil {
				errCh <- writeErr
				return
			}
		}
	}()
	go func() {
		for {
			messageType, payload, readErr := upstream.ReadFrame(proxyCtx)
			if readErr != nil {
				errCh <- liveSidebandReadError(readErr)
				return
			}
			if writeErr := downstream.Write(proxyCtx, messageType, payload); writeErr != nil {
				errCh <- writeErr
				return
			}
			if messageType == coderws.MessageText {
				eventType := strings.TrimSpace(gjson.GetBytes(payload, "type").String())
				if eventType == "session.closed" || eventType == "session.ended" {
					errCh <- ErrLiveCallNotFound
					return
				}
			}
		}
	}()

	runErr := s.runLiveController(proxyCtx, record, upstream, errCh)
	cancel()
	_, _ = store.ReleaseLiveController(context.Background(), record.CallHash, owner)
	if liveSessionEnded(runErr) || !time.Now().Before(record.ExpiresAt) {
		s.finalizeLiveCall(record)
		return runErr
	}
	go s.observeLiveCall(record.CallHash)
	return runErr
}

// liveSessionEnded 判断控制连接的退出原因是否意味着会话已终结（应 finalize：写
// usage log 并释放租约），而不是可以交给 observer 重连的临时错误。
//
// ErrLiveUnavailable 在控制循环里只会来自租约续租失败。RefreshLiveLease 的 Lua 在
// leaseID 被 GC 后不会重新写入，重连也拿不回并发槽 —— 若按临时错误重试，会话会以
// 约 1 秒一轮的节奏空转到 ExpiresAt，期间持着上游连接却不计入任何并发限制。
func liveSessionEnded(err error) bool {
	return errors.Is(err, ErrLiveCallNotFound) ||
		errors.Is(err, ErrLiveUnavailable) ||
		errors.Is(err, context.DeadlineExceeded)
}

func (s *OpenAIGatewayService) runLiveController(
	ctx context.Context,
	record *LiveCallRecord,
	upstream liveFrameConn,
	errCh <-chan error,
) error {
	refreshTicker := time.NewTicker(liveLeaseRefreshInterval)
	defer refreshTicker.Stop()
	maxTimer := time.NewTimer(time.Until(record.ExpiresAt))
	defer maxTimer.Stop()
	for {
		select {
		case <-ctx.Done():
			return context.Cause(ctx)
		case err := <-errCh:
			return err
		case <-maxTimer.C:
			closeCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = upstream.WriteFrame(closeCtx, coderws.MessageText, []byte(`{"type":"session.close"}`))
			cancel()
			return context.DeadlineExceeded
		case <-refreshTicker.C:
			if !s.refreshLiveLease(record) {
				return ErrLiveUnavailable
			}
		}
	}
}

func (s *OpenAIGatewayService) observeLiveCall(callHash string) {
	store, err := s.liveStore()
	if err != nil {
		return
	}
	owner := uuid.NewString()
	claimed, err := store.ClaimLiveController(context.Background(), callHash, LiveControllerObserver, owner)
	if err != nil || !claimed {
		return
	}
	for {
		record, getErr := store.GetLiveCall(context.Background(), callHash)
		if getErr != nil || record.Controller != LiveControllerObserver {
			return
		}
		if !time.Now().Before(record.ExpiresAt) {
			s.finalizeLiveCall(record)
			return
		}
		upstream, dialErr := s.dialLiveSideband(context.Background(), record)
		if dialErr != nil {
			if !s.waitForLiveObserverRetry(record) {
				return
			}
			continue
		}
		runErr := s.runLiveObserverConnection(record, upstream)
		_ = upstream.Close()
		if errors.Is(runErr, ErrLiveControllerChanged) {
			return
		}
		if liveSessionEnded(runErr) {
			s.finalizeLiveCall(record)
			return
		}
		if !s.waitForLiveObserverRetry(record) {
			return
		}
	}
}

func (s *OpenAIGatewayService) runLiveObserverConnection(record *LiveCallRecord, upstream liveFrameConn) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	frameCh := make(chan []byte, 1)
	errCh := make(chan error, 1)
	go func() {
		for {
			messageType, payload, err := upstream.ReadFrame(ctx)
			if err != nil {
				select {
				case errCh <- liveSidebandReadError(err):
				case <-ctx.Done():
				}
				return
			}
			if messageType == coderws.MessageText {
				select {
				case frameCh <- payload:
				case <-ctx.Done():
					return
				}
			}
		}
	}()
	refreshTicker := time.NewTicker(liveLeaseRefreshInterval)
	defer refreshTicker.Stop()
	controllerTicker := time.NewTicker(liveObserverPollInterval)
	defer controllerTicker.Stop()
	maxTimer := time.NewTimer(time.Until(record.ExpiresAt))
	defer maxTimer.Stop()
	store, _ := s.liveStore()
	for {
		select {
		case payload := <-frameCh:
			eventType := strings.TrimSpace(gjson.GetBytes(payload, "type").String())
			if eventType == "session.closed" || eventType == "session.ended" {
				return ErrLiveCallNotFound
			}
		case err := <-errCh:
			return err
		case <-controllerTicker.C:
			controller, err := store.GetLiveController(context.Background(), record.CallHash)
			if err != nil {
				return err
			}
			if controller != LiveControllerObserver {
				return ErrLiveControllerChanged
			}
		case <-refreshTicker.C:
			if !s.refreshLiveLease(record) {
				return ErrLiveUnavailable
			}
		case <-maxTimer.C:
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = upstream.WriteFrame(closeCtx, coderws.MessageText, []byte(`{"type":"session.close"}`))
			closeCancel()
			return context.DeadlineExceeded
		}
	}
}

func (s *OpenAIGatewayService) waitForLiveObserverRetry(record *LiveCallRecord) bool {
	timer := time.NewTimer(time.Second)
	defer timer.Stop()
	<-timer.C
	store, err := s.liveStore()
	if err != nil {
		return false
	}
	controller, err := store.GetLiveController(context.Background(), record.CallHash)
	// 过期不在此处判定：返回 true 让调用方回到循环顶部的过期分支，由它 finalize
	// （写 usage log + 释放租约）。在这里直接返回 false 会让会话静默结束、不留记录。
	return err == nil && controller == LiveControllerObserver
}

func (s *OpenAIGatewayService) refreshLiveLease(record *LiveCallRecord) bool {
	cache, err := s.liveConcurrencyCache()
	if err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), liveRedisOperationTimeout)
	defer cancel()
	refreshed, err := cache.RefreshLiveLease(ctx, record.AccountID, record.UserID, record.APIKeyID, record.LeaseID)
	return err == nil && refreshed
}

func (s *OpenAIGatewayService) releaseLiveLease(accountID, userID, apiKeyID int64, leaseID string) {
	cache, err := s.liveConcurrencyCache()
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), liveRedisOperationTimeout)
	defer cancel()
	_ = cache.ReleaseLiveLease(ctx, accountID, userID, apiKeyID, leaseID)
}

func (s *OpenAIGatewayService) finalizeLiveCall(record *LiveCallRecord) {
	if record == nil {
		return
	}
	store, err := s.liveStore()
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), liveRedisOperationTimeout)
	first, err := store.MarkLiveCallClosed(ctx, record.CallHash, liveClosedRecordTTL)
	cancel()
	if err != nil || !first {
		return
	}
	s.releaseLiveLease(record.AccountID, record.UserID, record.APIKeyID, record.LeaseID)
	if s.usageLogRepo == nil {
		return
	}
	duration := int(time.Since(record.CreatedAt).Milliseconds())
	if duration < 0 {
		duration = 0
	}
	inboundEndpoint := record.InboundEndpoint
	upstreamEndpoint := "/backend-api/codex/realtime/calls"
	userAgent := record.UserAgent
	ipAddress := record.IPAddress
	billingType := int8(BillingTypeBalance)
	if record.SubscriptionID > 0 {
		billingType = BillingTypeSubscription
	}
	_, _ = s.usageLogRepo.Create(context.Background(), &UsageLog{
		UserID:           record.UserID,
		APIKeyID:         record.APIKeyID,
		AccountID:        record.AccountID,
		RequestID:        record.CallHash,
		Model:            record.Model,
		RequestedModel:   record.Model,
		GroupID:          liveOptionalID(record.GroupID),
		SubscriptionID:   liveOptionalID(record.SubscriptionID),
		RateMultiplier:   1,
		BillingType:      billingType,
		RequestType:      RequestTypeLive,
		DurationMs:       &duration,
		UserAgent:        &userAgent,
		IPAddress:        &ipAddress,
		InboundEndpoint:  &inboundEndpoint,
		UpstreamEndpoint: &upstreamEndpoint,
		CreatedAt:        record.CreatedAt,
	})
}
