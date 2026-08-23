package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"golang.org/x/net/http2"
	"golang.org/x/sync/singleflight"
)

// chatgptCodexModelsURL is the ChatGPT Codex models manifest endpoint.
// Package-level variable so tests can point it at a stub server.
const chatgptCodexModelsDefaultURL = "https://chatgpt.com/backend-api/codex/models"

var chatgptCodexModelsURL = chatgptCodexModelsDefaultURL

const (
	codexModelsManifestBodyLimit       int64 = 8 << 20
	codexModelsManifestCacheBodyLimit        = 1 << 20
	codexModelsManifestCacheMaxEntries       = 64
	codexModelsManifestCacheTTL              = 30 * time.Second
	codexModelsManifestCacheStaleTTL         = 5 * time.Minute
	codexModelsManifestRequestTimeout        = 15 * time.Second
)

// CodexModelsManifest carries the client representation plus caching metadata.
type CodexModelsManifest struct {
	Body         []byte
	ETag         string
	upstreamETag string
	NotModified  bool
}

type codexModelsManifestUpstreamError struct {
	err        error
	retryable  bool
	statusCode int
	headers    http.Header
	body       []byte
}

func (e *codexModelsManifestUpstreamError) Error() string { return e.err.Error() }

func (e *codexModelsManifestUpstreamError) Unwrap() error { return e.err }

// IsRetryableCodexModelsManifestError 判断在不修改请求的情况下换用其他账号是否可能成功。
// 配置错误和除 429、ChatGPT 后端 401 外的上游 4xx 响应不会重试。ChatGPT Codex
// 模型清单的 401 反映当前 OAuth 账号的上游令牌状态，因此其他账号仍可能成功；自定义
// API Key 上游继续沿用 401 不故障转移的行为，因为其 /models 鉴权语义不能代表账号状态。
func IsRetryableCodexModelsManifestError(err error) bool {
	var upstreamErr *codexModelsManifestUpstreamError
	if errors.As(err, &upstreamErr) {
		return upstreamErr.retryable
	}
	// 下列错误只说明当前候选账号无法提供 Codex 模型清单，不代表同组其他
	// OpenAI OAuth/API Key 账号也不可用。允许 Handler 排除该账号后继续尝试，
	// 避免 Composite 的无模型参数请求被单个 Chat Completions 专用账号阻断。
	switch infraerrors.Reason(err) {
	case "OPENAI_CODEX_MODELS_CREDENTIALS_FAILED",
		"OPENAI_CODEX_MODELS_TOKEN_MISSING",
		"OPENAI_CODEX_MODELS_API_KEY_UPSTREAM_UNSUPPORTED",
		"OPENAI_CODEX_MODELS_API_KEY_MISSING",
		"OPENAI_CODEX_MODELS_API_KEY_UPSTREAM_INVALID",
		"OPENAI_CODEX_MODELS_ACCOUNT_TYPE_UNSUPPORTED",
		"OPENAI_CODEX_MODELS_AUTH_FAILED":
		return true
	default:
		return false
	}
}

func isRetryableCodexModelsManifestTransportError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) ||
		errors.Is(err, io.EOF) ||
		errors.Is(err, io.ErrUnexpectedEOF) ||
		errors.Is(err, net.ErrClosed) {
		return true
	}

	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return true
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return true
	}
	var goAwayErr http2.GoAwayError
	if errors.As(err, &goAwayErr) {
		return true
	}
	var streamErr http2.StreamError
	if errors.As(err, &streamErr) {
		return true
	}
	var connectionErr http2.ConnectionError
	if errors.As(err, &connectionErr) {
		return true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}

	// net/http 使用未导出的 HTTP/2 错误类型，因此无法对标准库 Transport
	// 产生的错误执行类型匹配。
	message := strings.ToLower(err.Error())
	if strings.Contains(message, "http2:") &&
		(strings.Contains(message, "goaway") ||
			strings.Contains(message, "refused_stream") ||
			strings.Contains(message, "frame too large")) {
		return true
	}
	if strings.Contains(message, "stream error: stream id ") {
		return true
	}
	for _, code := range []http2.ErrCode{
		http2.ErrCodeNo,
		http2.ErrCodeProtocol,
		http2.ErrCodeInternal,
		http2.ErrCodeFlowControl,
		http2.ErrCodeSettingsTimeout,
		http2.ErrCodeStreamClosed,
		http2.ErrCodeFrameSize,
		http2.ErrCodeRefusedStream,
		http2.ErrCodeCancel,
		http2.ErrCodeCompression,
		http2.ErrCodeConnect,
		http2.ErrCodeEnhanceYourCalm,
		http2.ErrCodeInadequateSecurity,
		http2.ErrCodeHTTP11Required,
	} {
		if strings.Contains(message, "connection error: "+strings.ToLower(code.String())) {
			return true
		}
	}
	return false
}

type codexModelsManifestRequest struct {
	method              string
	url                 string
	host                string
	headers             http.Header
	proxyURL            string
	accountID           int64
	credentialAccountID int64
	credentialAccount   *Account
	accountConcurrency  int
	useAPIKeyUpstream   bool
	endpointID          codexEndpointID
	invocationID        string
	useCodexProfile     bool
	releaseMode         string
	officialRuntime     *OfficialEgressTransitionRuntime
	bundleHolder        *officialCodexBundleHolder
}

type codexModelsManifestCacheEntry struct {
	manifest   *CodexModelsManifest
	order      uint64
	expiresAt  time.Time
	staleUntil time.Time
}

type codexModelsManifestCacheState uint8

const (
	codexModelsManifestCacheMiss codexModelsManifestCacheState = iota
	codexModelsManifestCacheFresh
	codexModelsManifestCacheStale
)

type codexModelsManifestCache struct {
	mu        sync.Mutex
	entries   map[string]codexModelsManifestCacheEntry
	nextOrder uint64
	refresh   singleflight.Group
}

func (c *codexModelsManifestCache) get(key string, now time.Time) (*CodexModelsManifest, codexModelsManifestCacheState) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok {
		return nil, codexModelsManifestCacheMiss
	}
	if !now.Before(entry.staleUntil) {
		delete(c.entries, key)
		return nil, codexModelsManifestCacheMiss
	}
	if now.Before(entry.expiresAt) {
		return entry.manifest, codexModelsManifestCacheFresh
	}
	return entry.manifest, codexModelsManifestCacheStale
}

func (c *codexModelsManifestCache) set(key string, manifest *CodexModelsManifest, now time.Time) {
	if manifest == nil || len(manifest.Body) > codexModelsManifestCacheBodyLimit {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.entries == nil {
		c.entries = make(map[string]codexModelsManifestCacheEntry)
	}
	if _, exists := c.entries[key]; !exists && len(c.entries) >= codexModelsManifestCacheMaxEntries {
		oldestKey := ""
		var oldestOrder uint64
		for candidateKey, entry := range c.entries {
			if !now.Before(entry.staleUntil) {
				delete(c.entries, candidateKey)
				continue
			}
			if oldestKey == "" || entry.order < oldestOrder {
				oldestKey = candidateKey
				oldestOrder = entry.order
			}
		}
		if len(c.entries) >= codexModelsManifestCacheMaxEntries && oldestKey != "" {
			delete(c.entries, oldestKey)
		}
	}
	c.nextOrder++
	c.entries[key] = codexModelsManifestCacheEntry{
		manifest:   manifest,
		order:      c.nextOrder,
		expiresAt:  now.Add(codexModelsManifestCacheTTL),
		staleUntil: now.Add(codexModelsManifestCacheStaleTTL),
	}
}

// FetchCodexModelsManifest 从 OAuth 账号的 ChatGPT 后端或 API Key 账号的
// 自定义上游获取实时 Codex 模型清单。
//
// 校验稳定的顶层结构后，OAuth 响应体保持原样；自定义 API Key 清单只执行
// 自定义供应商模式所需的最小兼容性调整。入口上下文还用于绑定 Plus 官方出站画像。
func (s *OpenAIGatewayService) FetchCodexModelsManifest(
	ctx context.Context,
	account *Account,
	clientVersion string,
	ifNoneMatch string,
	ingress ...*gin.Context,
) (manifest *CodexModelsManifest, err error) {
	invocationID := ""
	if len(ingress) > 0 && ingress[0] != nil {
		invocationID, err = officialEgressInvocationIDForRequest(ingress[0])
		if err != nil {
			return nil, infraerrors.Newf(
				http.StatusInternalServerError,
				"OPENAI_CODEX_MODELS_PROFILE_INVALID",
				"resolve Codex models invocation: %v",
				err,
			)
		}
		if account != nil && account.IsOpenAIOAuth() {
			runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
			if runtimeErr != nil {
				return nil, infraerrors.Newf(
					http.StatusInternalServerError,
					"OPENAI_CODEX_MODELS_PROFILE_INVALID",
					"resolve Codex models runtime: %v",
					runtimeErr,
				)
			}
			ctx, err = bindOfficialCodexRuntimeStateFromIngress(
				ctx,
				ingress[0],
				account,
				string(runtimeState.CodexReleaseMode),
				codexEndpointID(officialCodexEndpointModels),
			)
			if err != nil {
				return nil, infraerrors.Newf(
					http.StatusBadRequest,
					"OPENAI_CODEX_MODELS_PROFILE_INVALID",
					"resolve Codex models runtime state: %v",
					err,
				)
			}
		}
	}
	return s.fetchCodexModelsManifest(ctx, account, clientVersion, ifNoneMatch, true, invocationID)
}

// fetchCodexModelsManifest 支持能力预热使用只读模式。只读模式仍执行相同的
// 鉴权、代理和响应校验，但辅助探测的 401 不得改变账号调度或禁用状态。
func (s *OpenAIGatewayService) fetchCodexModelsManifest(
	ctx context.Context,
	account *Account,
	clientVersion string,
	ifNoneMatch string,
	handleAccountAuthError bool,
	invocationIDs ...string,
) (manifest *CodexModelsManifest, err error) {
	if account == nil {
		return nil, infraerrors.New(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_ACCOUNT_REQUIRED", "account is required")
	}
	defer func() {
		if err == nil && manifest != nil && len(manifest.Body) > 0 {
			s.openaiModelCapabilities.replaceFromManifest(account.ID, manifest.Body)
		}
	}()
	credAccount, err := resolveCredentialAccount(ctx, s.accountRepo, account)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_CREDENTIALS_FAILED", "resolve credential account: %v", err)
	}

	requestedClientVersion := strings.TrimSpace(clientVersion)
	if requestedClientVersion == "" {
		requestedClientVersion = resolveVerifiedCodexClientVersion()
	}

	requestEndpoint := chatgptCodexModelsURL
	requestMethod := http.MethodGet
	requestHost := ""
	var endpointProfile officialCodexEndpointProfile
	endpointID := codexEndpointID(officialCodexEndpointModels)
	invocationID := ""
	if len(invocationIDs) > 0 {
		invocationID = strings.TrimSpace(invocationIDs[0])
	}
	authToken := ""
	useAPIKeyUpstream := false
	useCodexProfile := false
	releaseMode := ""
	var officialRuntime *OfficialEgressTransitionRuntime
	appendModelsPath := false
	switch {
	case credAccount.IsOpenAIOAuth():
		officialRuntime, err = resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "resolve official egress runtime: %v", err)
		}
		releaseMode = string(officialRuntime.CodexReleaseMode)
		endpointProfile, err = resolveCodexEndpointForMode(
			releaseMode,
			endpointID,
		)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "resolve Codex models endpoint profile: %v", err)
		}
		profileURL, profileErr := buildCodexEndpointURLForMode(
			releaseMode,
			endpointID,
			officialCodexEndpointURLInput{},
		)
		if profileErr != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "resolve Codex models URL: %v", profileErr)
		}
		requestedClientVersion = strings.TrimSpace(profileURL.Query().Get("client_version"))
		if requestedClientVersion == "" {
			return nil, infraerrors.New(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "Codex models profile has no client_version")
		}
		requestMethod = endpointProfile.Method
		requestHost = endpointProfile.Host
		if requestEndpoint == chatgptCodexModelsDefaultURL {
			requestEndpoint = profileURL.String()
			useCodexProfile = true
		}
		if invocationID == "" {
			invocationID = uuid.NewString()
		}
		authToken = strings.TrimSpace(credAccount.GetOpenAIAccessToken())
		if authToken == "" && !credAccount.IsOpenAIAgentIdentity() {
			return nil, infraerrors.New(http.StatusBadGateway, "OPENAI_CODEX_MODELS_TOKEN_MISSING", "account has no Codex backend access token")
		}
	case credAccount.IsOpenAIApiKey():
		baseURL := strings.TrimSpace(credAccount.GetCredential("base_url"))
		if baseURL == "" || isOfficialOpenAIModelsBaseURL(baseURL) {
			return nil, infraerrors.New(
				http.StatusBadGateway,
				"OPENAI_CODEX_MODELS_API_KEY_UPSTREAM_UNSUPPORTED",
				"Codex models manifest requires a custom API key upstream base URL",
			)
		}
		authToken = strings.TrimSpace(credAccount.GetOpenAIApiKey())
		if authToken == "" {
			return nil, infraerrors.New(http.StatusBadGateway, "OPENAI_CODEX_MODELS_API_KEY_MISSING", "account has no API key for the Codex models upstream")
		}
		normalizedBaseURL, validateErr := s.validateUpstreamBaseURL(baseURL)
		if validateErr != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_API_KEY_UPSTREAM_INVALID", "invalid Codex models upstream base URL: %v", validateErr)
		}
		requestEndpoint = normalizedBaseURL
		useAPIKeyUpstream = true
		appendModelsPath = true
	default:
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_ACCOUNT_TYPE_UNSUPPORTED", "account type %q cannot fetch the Codex models manifest", credAccount.Type)
	}

	var requestURL *url.URL
	if useCodexProfile {
		requestURL, err = url.Parse(requestEndpoint)
	} else {
		requestURL, err = buildCodexModelsManifestURL(requestEndpoint, appendModelsPath, requestedClientVersion)
	}
	if err != nil {
		if useAPIKeyUpstream {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_API_KEY_UPSTREAM_INVALID", "invalid Codex models upstream base URL: %v", err)
		}
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_REQUEST_FAILED", "parse codex models request URL: %v", err)
	}

	headers := make(http.Header)
	if useAPIKeyUpstream {
		headers.Set("Authorization", "Bearer "+authToken)
		credAccount.ApplyHeaderOverrides(headers)
	} else {
		authHeaders, authErr := s.buildOpenAIAuthenticationHeaders(ctx, credAccount, authToken)
		if authErr != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_AUTH_FAILED", "build Codex models authentication: %v", authErr)
		}
		for key, values := range authHeaders {
			for _, value := range values {
				headers.Add(key, value)
			}
		}
		setOpenAIChatGPTAccountHeaders(headers, credAccount)
	}
	if useAPIKeyUpstream {
		// 自定义 API Key 上游不属于官方 OAuth persona，保留上游通用 JSON 协商，
		// 同时校验客户端版本，避免把陈旧 version 头发送给兼容端点。
		headers.Set("Accept", "application/json")
		identity := resolveCodexOutboundIdentity("")
		headers.Set("Originator", identity.originator)
		headers.Set("User-Agent", identity.userAgent)
		headerVersion := NormalizeCodexClientVersion(requestedClientVersion)
		if headerVersion == "" || CompareVersions(headerVersion, codexUpstreamMinVersion) < 0 {
			headerVersion = identity.version
		}
		headers.Set("Version", headerVersion)
	} else {
		// 官方 models 端点层不设 accept，由 reqwest 补默认 */*（规格表 SPEC-HDR-006）。
		headers.Set("Accept", "*/*")
		applyOpenAICodexAuxiliaryHeaders(headers)
		headers.Set("Version", requestedClientVersion)
	}

	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}

	request := codexModelsManifestRequest{
		method:              requestMethod,
		url:                 requestURL.String(),
		host:                requestHost,
		headers:             headers,
		proxyURL:            proxyURL,
		accountID:           account.ID,
		credentialAccountID: credAccount.ID,
		credentialAccount:   credAccount,
		accountConcurrency:  account.Concurrency,
		useAPIKeyUpstream:   useAPIKeyUpstream,
		endpointID:          endpointID,
		invocationID:        invocationID,
		useCodexProfile:     useCodexProfile,
		releaseMode:         releaseMode, officialRuntime: officialRuntime,
		bundleHolder: &officialCodexBundleHolder{httpAttemptBudget: 2},
	}
	if useAPIKeyUpstream {
		return s.fetchCachedAPIKeyCodexModelsManifest(ctx, request, ifNoneMatch)
	}
	manifest, fetchErr := s.fetchCodexModelsManifestUpstream(ctx, request, ifNoneMatch)
	if !credAccount.IsOpenAIAgentIdentity() || !isAgentIdentityTaskInvalidCodexModelsError(fetchErr) {
		if handleAccountAuthError {
			s.handleCodexModelsManifestAccountAuthError(ctx, account, credAccount, fetchErr)
		}
		return manifest, fetchErr
	}
	expectedTaskID := strings.TrimSpace(credAccount.GetCredential("task_id"))
	if recoverErr := s.recoverAgentIdentityTask(ctx, credAccount, expectedTaskID); recoverErr != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_AUTH_FAILED", "agent identity task recovery failed: %v", recoverErr)
	}
	authHeaders, authErr := s.buildOpenAIAuthenticationHeaders(ctx, credAccount, "")
	if authErr != nil {
		return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_AUTH_FAILED", "build Codex models authentication after task recovery: %v", authErr)
	}
	request.headers.Del("Authorization")
	request.headers.Del("ChatGPT-Account-ID")
	for key, values := range authHeaders {
		for _, value := range values {
			request.headers.Add(key, value)
		}
	}
	setOpenAIChatGPTAccountHeaders(request.headers, credAccount)
	return s.fetchCodexModelsManifestUpstream(ctx, request, ifNoneMatch)
}

func isAgentIdentityTaskInvalidCodexModelsError(err error) bool {
	var upstreamErr *codexModelsManifestUpstreamError
	return errors.As(err, &upstreamErr) &&
		isAgentIdentityTaskInvalidHTTPResponse(upstreamErr.statusCode, upstreamErr.body)
}

// handleCodexModelsManifestAccountAuthError feeds manifest 401s from the
// ChatGPT Codex backend into the shared upstream-error state machinery
// (token cache invalidation, temp-unschedulable cooldown, or permanent
// disable for token_revoked/token_invalidated). Without this, an account
// whose OAuth token was revoked upstream stays active and schedulable and
// keeps being selected for every subsequent /models request (#4544).
//
// Scope is deliberately limited to plain OAuth accounts: the manifest
// endpoint authenticates with the same token as /responses forwarding, so a
// 401 is authoritative for the account. Agent Identity accounts are excluded
// because their 401s can be task-scoped and have a dedicated recovery flow,
// and API key manifests come from custom upstreams whose /models auth may
// diverge from their chat endpoints.
func (s *OpenAIGatewayService) handleCodexModelsManifestAccountAuthError(ctx context.Context, account, credAccount *Account, err error) {
	if s == nil || account == nil || err == nil {
		return
	}
	if credAccount == nil || !credAccount.IsOpenAIOAuth() || credAccount.IsOpenAIAgentIdentity() {
		return
	}
	var upstreamErr *codexModelsManifestUpstreamError
	if !errors.As(err, &upstreamErr) || upstreamErr.statusCode != http.StatusUnauthorized {
		return
	}
	headers := upstreamErr.headers
	if headers == nil {
		headers = http.Header{}
	}
	s.handleOpenAIAccountUpstreamError(ctx, account, upstreamErr.statusCode, headers, upstreamErr.body)
}

func (s *OpenAIGatewayService) fetchCachedAPIKeyCodexModelsManifest(ctx context.Context, request codexModelsManifestRequest, ifNoneMatch string) (*CodexModelsManifest, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	cacheKey := buildCodexModelsManifestCacheKey(request)
	manifest, state := s.codexModelsManifestCache.get(cacheKey, time.Now())
	if state == codexModelsManifestCacheFresh {
		return codexModelsManifestForClient(manifest, ifNoneMatch), nil
	}
	resultCh := s.refreshCachedAPIKeyCodexModelsManifest(cacheKey, request)
	if state == codexModelsManifestCacheStale {
		return codexModelsManifestForClient(manifest, ifNoneMatch), nil
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case result := <-resultCh:
		if result.Err != nil {
			return nil, result.Err
		}
		manifest, ok := result.Val.(*CodexModelsManifest)
		if !ok || manifest == nil {
			return nil, infraerrors.New(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_REQUEST_FAILED", "invalid shared Codex models manifest result")
		}
		return codexModelsManifestForClient(manifest, ifNoneMatch), nil
	}
}

func (s *OpenAIGatewayService) refreshCachedAPIKeyCodexModelsManifest(cacheKey string, request codexModelsManifestRequest) <-chan singleflight.Result {
	return s.codexModelsManifestCache.refresh.DoChan(cacheKey, func() (any, error) {
		cached, _ := s.codexModelsManifestCache.get(cacheKey, time.Now())
		ifNoneMatch := ""
		if cached != nil {
			ifNoneMatch = cached.upstreamETag
		}
		manifest, err := s.fetchCodexModelsManifestUpstream(context.Background(), request, ifNoneMatch)
		if err != nil {
			return nil, err
		}
		if manifest.NotModified && cached != nil {
			s.codexModelsManifestCache.set(cacheKey, cached, time.Now())
			return cached, nil
		}
		if !manifest.NotModified {
			s.codexModelsManifestCache.set(cacheKey, manifest, time.Now())
		}
		return manifest, nil
	})
}

func (s *OpenAIGatewayService) fetchCodexModelsManifestUpstream(ctx context.Context, request codexModelsManifestRequest, ifNoneMatch string) (*CodexModelsManifest, error) {
	reqCtx, cancel := context.WithTimeout(ctx, codexModelsManifestRequestTimeout)
	defer cancel()
	// API-Key 自定义上游是通用第三方发送，不得通过非空 SinkID 纳入官方受管
	// 闭集：绑定后 Guard 会按 codex.models.list 的官方 route 校验第三方 URL，
	// fail-closed 直接拒发（unknown_route）。仅官方链路绑定出站身份。
	if !request.useAPIKeyUpstream {
		boundCtx, bindErr := bindOfficialEgressSink(reqCtx, officialEgressSinkModelsList)
		if bindErr != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_REQUEST_FAILED", "bind Codex models official egress sink: %v", bindErr)
		}
		reqCtx = boundCtx
	}
	method := request.method
	if method == "" {
		method = http.MethodGet
	}
	req, err := http.NewRequestWithContext(reqCtx, method, request.url, nil)
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_REQUEST_FAILED", "create codex models request: %v", err)
	}
	req.Header = request.headers.Clone()
	if request.host != "" {
		req.Host = request.host
	}
	if request.useAPIKeyUpstream {
		if ifNoneMatch = strings.TrimSpace(ifNoneMatch); ifNoneMatch != "" {
			req.Header.Set("If-None-Match", ifNoneMatch)
		}
	} else if !request.useCodexProfile {
		// 非默认 URL 只用于包内测试桩；它不具备官方 host，不能绑定
		// 出站上下文，但 header 仍由同一端点画像收敛。
		if _, finalizeErr := applyCodexHeaderContractForMode(
			request.releaseMode,
			request.endpointID,
			req.Header,
			officialCodexConditionsFromHeaders(req.Header),
		); finalizeErr != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "finalize Codex models test headers: %v", finalizeErr)
		}
	}

	var resp *http.Response
	if request.useAPIKeyUpstream {
		if s.httpUpstream == nil {
			return nil, infraerrors.New(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_UPSTREAM_NOT_CONFIGURED", "Codex models upstream HTTP client is not configured")
		}
		req = req.WithContext(WithHTTPUpstreamProfile(req.Context(), HTTPUpstreamProfileOpenAI))
		resp, err = doOpenAIAPIKeyHTTPTransport(
			s.httpUpstream, req, request.proxyURL, request.credentialAccount, nil,
		)
	} else if request.useCodexProfile && s.httpUpstream != nil {
		if request.officialRuntime == nil || request.credentialAccount == nil || request.bundleHolder == nil {
			return nil, infraerrors.New(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_UPSTREAM_NOT_CONFIGURED", "Codex models official runtime is not configured")
		}
		request.bundleHolder.mu.Lock()
		if request.bundleHolder.httpInvocation == nil {
			request.bundleHolder.httpInvocation, err = newOfficialCodexHTTPInvocation(
				reqCtx,
				officialCodexHTTPInvocationInput{
					Runtime: request.officialRuntime, Account: request.credentialAccount,
					SinkID: officialegress.SinkCodexModelsList, InvocationID: request.invocationID,
					ProxyURL: request.proxyURL, PolicyID: "changeset3.models.list",
					PolicySource:  "service.fetchCodexModelsManifest",
					AttemptBudget: request.bundleHolder.httpAttemptBudget,
				},
			)
		}
		if err == nil {
			resp, err = request.bundleHolder.httpInvocation.Execute(
				reqCtx,
				officialCodexHTTPAttemptInput{
					EndpointID: string(request.endpointID), Request: req,
				},
			)
		}
		request.bundleHolder.mu.Unlock()
	} else {
		// models 是画像声明的无 Cookie 辅助端点。请求只能使用端点画像生成的固定
		// HTTP 传输与 header 集合，不能因为配置了代理而切换 ClientHello。
		if s.httpUpstream == nil {
			return nil, infraerrors.New(
				http.StatusInternalServerError,
				"OPENAI_CODEX_MODELS_UPSTREAM_NOT_CONFIGURED",
				"Codex models upstream HTTP client is not configured",
			)
		}
		var tlsProfileErr error
		var tlsProfile *tlsfingerprint.Profile
		tlsProfile, tlsProfileErr = resolveCodexEndpointTLSProfileForMode(
			request.releaseMode,
			request.endpointID,
		)
		if tlsProfileErr != nil {
			return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_CODEX_MODELS_PROFILE_INVALID", "resolve Codex models TLS profile: %v", tlsProfileErr)
		}
		resp, err = doOpenAIAPIKeyHTTPTransport(
			s.httpUpstream, req, request.proxyURL, request.credentialAccount, tlsProfile,
		)
	}
	// HTTPUpstream 是接口，实现（含测试替身）可能在未出错时返回空响应；
	// 直接解引用会 panic，因此在这里归一成上游错误。
	if err == nil && resp == nil {
		err = errors.New("codex models manifest upstream returned no response")
	}
	if err != nil {
		return nil, &codexModelsManifestUpstreamError{
			err:       infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_UPSTREAM_FAILED", "codex models manifest request failed: %v", err),
			retryable: isRetryableCodexModelsManifestTransportError(err),
		}
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == http.StatusNotModified {
		return &CodexModelsManifest{ETag: resp.Header.Get("ETag"), NotModified: true}, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		body = s.redactAgentIdentitySensitiveBody(reqCtx, request.credentialAccount, body)
		message := strings.TrimSpace(string(body))
		if message == "" {
			message = resp.Status
		}
		return nil, &codexModelsManifestUpstreamError{
			err:        infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_UPSTREAM_FAILED", "codex models manifest upstream error %d: %s", resp.StatusCode, message),
			statusCode: resp.StatusCode,
			headers:    resp.Header.Clone(),
			body:       body,
			retryable: (resp.StatusCode == http.StatusUnauthorized && !request.useAPIKeyUpstream) ||
				resp.StatusCode == http.StatusTooManyRequests ||
				(resp.StatusCode >= http.StatusInternalServerError && resp.StatusCode < 600),
		}
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, codexModelsManifestBodyLimit))
	if err != nil {
		return nil, &codexModelsManifestUpstreamError{
			err:       infraerrors.Newf(http.StatusBadGateway, "OPENAI_CODEX_MODELS_UPSTREAM_FAILED", "read codex models manifest response: %v", err),
			retryable: isRetryableCodexModelsManifestTransportError(err),
		}
	}
	upstreamBody := body
	if request.useAPIKeyUpstream {
		body = convertOpenAIModelListToCodexManifest(body)
	}
	if err := validateCodexModelsManifestEnvelope(body); err != nil {
		return nil, &codexModelsManifestUpstreamError{
			err: infraerrors.Newf(
				http.StatusBadGateway,
				"OPENAI_CODEX_MODELS_UPSTREAM_INVALID_MANIFEST",
				"codex models manifest upstream returned an invalid envelope: %v",
				err,
			),
			retryable: true,
		}
	}
	if request.useAPIKeyUpstream {
		body, err = adjustAPIKeyCodexModelsManifest(body)
		if err != nil {
			return nil, &codexModelsManifestUpstreamError{
				err: infraerrors.Newf(
					http.StatusBadGateway,
					"OPENAI_CODEX_MODELS_UPSTREAM_INVALID_MANIFEST",
					"codex models manifest upstream could not be adjusted: %v",
					err,
				),
				retryable: true,
			}
		}
	}
	etag := resp.Header.Get("ETag")
	manifest := &CodexModelsManifest{Body: body, ETag: etag}
	if request.useAPIKeyUpstream {
		manifest.upstreamETag = etag
		if !bytes.Equal(body, upstreamBody) {
			manifest.ETag = codexModelsManifestBodyETag(body)
		}
	}
	return manifest, nil
}

func codexModelsManifestBodyETag(body []byte) string {
	sum := sha256.Sum256(body)
	return fmt.Sprintf(`"%x"`, sum)
}

var apiKeyCodexModelsWithoutResponsesLite = map[string]struct{}{
	"gpt-5.6-sol":   {},
	"gpt-5.6-terra": {},
	"gpt-5.6-luna":  {},
}

// adjustAPIKeyCodexModelsManifest prevents Codex from selecting Responses
// Lite for custom API key providers. Those clients do not install web.run in
// Lite mode, so the affected model manifests must advertise the full Responses
// path. Return the original body when no targeted true value is present.
func adjustAPIKeyCodexModelsManifest(body []byte) ([]byte, error) {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("decode JSON object: %w", err)
	}
	var models []json.RawMessage
	if err := json.Unmarshal(envelope["models"], &models); err != nil {
		return nil, fmt.Errorf("decode top-level models array: %w", err)
	}

	changed := false
	for i, rawModel := range models {
		var model map[string]json.RawMessage
		if err := json.Unmarshal(rawModel, &model); err != nil || model == nil {
			continue
		}
		var slug string
		if err := json.Unmarshal(model["slug"], &slug); err != nil {
			continue
		}
		if _, targeted := apiKeyCodexModelsWithoutResponsesLite[slug]; !targeted {
			continue
		}
		var useResponsesLite bool
		if err := json.Unmarshal(model["use_responses_lite"], &useResponsesLite); err != nil || !useResponsesLite {
			continue
		}
		model["use_responses_lite"] = json.RawMessage("false")
		adjusted, err := json.Marshal(model)
		if err != nil {
			return nil, fmt.Errorf("encode model %q: %w", slug, err)
		}
		models[i] = adjusted
		changed = true
	}
	if !changed {
		return body, nil
	}

	adjustedModels, err := json.Marshal(models)
	if err != nil {
		return nil, fmt.Errorf("encode top-level models array: %w", err)
	}
	envelope["models"] = adjustedModels
	adjusted, err := json.Marshal(envelope)
	if err != nil {
		return nil, fmt.Errorf("encode JSON object: %w", err)
	}
	return adjusted, nil
}

// convertOpenAIModelListToCodexManifest rewrites a standard OpenAI
// GET /v1/models response ({"object":"list","data":[{"id":...},...]}) into the
// Codex manifest envelope ({"models":[{"slug":...},...]}) so custom API key
// upstreams that only implement the standard endpoint can serve Codex model
// discovery. Bodies that already carry a top-level models field, are not the
// standard list shape, or yield no usable model IDs are returned unchanged so
// envelope validation reports the original payload.
func convertOpenAIModelListToCodexManifest(body []byte) []byte {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil || envelope == nil {
		return body
	}
	if _, ok := envelope["models"]; ok {
		return body
	}
	data, ok := envelope["data"]
	if !ok {
		return body
	}
	var entries []struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(data, &entries); err != nil {
		return body
	}
	type codexModelEntry struct {
		Slug string `json:"slug"`
	}
	models := make([]codexModelEntry, 0, len(entries))
	for _, entry := range entries {
		id := strings.TrimSpace(entry.ID)
		if id == "" {
			continue
		}
		models = append(models, codexModelEntry{Slug: id})
	}
	if len(models) == 0 {
		return body
	}
	converted, err := json.Marshal(map[string][]codexModelEntry{"models": models})
	if err != nil {
		return body
	}
	return converted
}

func validateCodexModelsManifestEnvelope(body []byte) error {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil {
		return fmt.Errorf("decode JSON object: %w", err)
	}
	if envelope == nil {
		return errors.New("expected a JSON object")
	}
	models, ok := envelope["models"]
	if !ok {
		return errors.New("missing top-level models array")
	}
	models = bytes.TrimSpace(models)
	var entries []json.RawMessage
	if len(models) == 0 || models[0] != '[' {
		return errors.New("top-level models field is not an array")
	}
	if err := json.Unmarshal(models, &entries); err != nil {
		return fmt.Errorf("decode top-level models array: %w", err)
	}
	return nil
}

func buildCodexModelsManifestCacheKey(request codexModelsManifestRequest) string {
	hasher := sha256.New()
	_, _ = fmt.Fprintf(hasher, "%d\n%d\n%s\n%s\n", request.accountID, request.credentialAccountID, request.proxyURL, request.url)
	headerNames := make([]string, 0, len(request.headers))
	for name := range request.headers {
		headerNames = append(headerNames, name)
	}
	sort.Strings(headerNames)
	for _, name := range headerNames {
		_, _ = fmt.Fprintf(hasher, "%s\n", strings.ToLower(name))
		for _, value := range request.headers[name] {
			_, _ = fmt.Fprintf(hasher, "%s\n", value)
		}
	}
	return fmt.Sprintf("%x", hasher.Sum(nil))
}

func codexModelsManifestForClient(manifest *CodexModelsManifest, ifNoneMatch string) *CodexModelsManifest {
	if manifest == nil {
		return nil
	}
	if codexModelsManifestETagMatches(ifNoneMatch, manifest.ETag) {
		return &CodexModelsManifest{ETag: manifest.ETag, NotModified: true}
	}
	return manifest
}

func codexModelsManifestETagMatches(ifNoneMatch, etag string) bool {
	etag = strings.TrimSpace(etag)
	if etag == "" {
		return false
	}
	normalize := func(value string) string {
		value = strings.TrimSpace(value)
		if len(value) >= 2 && strings.EqualFold(value[:2], "W/") {
			value = strings.TrimSpace(value[2:])
		}
		return value
	}
	want := normalize(etag)
	for _, candidate := range strings.Split(ifNoneMatch, ",") {
		candidate = strings.TrimSpace(candidate)
		if candidate == "*" || normalize(candidate) == want {
			return true
		}
	}
	return false
}

func isOfficialOpenAIModelsBaseURL(raw string) bool {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return false
	}
	hostname := strings.TrimSuffix(parsed.Hostname(), ".")
	return strings.EqualFold(hostname, "api.openai.com")
}

func buildCodexModelsManifestURL(endpoint string, appendModelsPath bool, clientVersion string) (*url.URL, error) {
	requestURL, err := url.Parse(endpoint)
	if err != nil {
		return nil, err
	}
	if requestURL.Fragment != "" {
		return nil, fmt.Errorf("URL fragments are not supported")
	}

	query := requestURL.Query()
	requestURL.RawQuery = ""
	requestURL.ForceQuery = false
	if appendModelsPath {
		if isConcreteOpenAIEndpointPath(requestURL.Path) {
			return nil, fmt.Errorf("base URL points to a concrete OpenAI endpoint")
		}
		requestURL, err = url.Parse(buildOpenAIModelsURL(requestURL.String()))
		if err != nil {
			return nil, err
		}
	}
	query.Set("client_version", clientVersion)
	requestURL.RawQuery = query.Encode()
	return requestURL, nil
}

func isConcreteOpenAIEndpointPath(path string) bool {
	normalized := strings.ToLower(strings.TrimRight(strings.TrimSpace(path), "/"))
	for _, suffix := range []string{
		"/chat/completions",
		"/responses",
		"/embeddings",
		"/images/generations",
		"/images/edits",
		"/alpha/search",
	} {
		if strings.HasSuffix(normalized, suffix) {
			return true
		}
	}
	return false
}
