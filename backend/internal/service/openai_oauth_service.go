package service

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

// OpenAIOAuthService handles OpenAI OAuth authentication flows
type OpenAIOAuthService struct {
	sessionStore         *openai.SessionStore
	proxyRepo            ProxyRepository
	oauthClient          OpenAIOAuthClient
	officialEgress       *OfficialEgressTransitionRuntime
	privacyClientFactory PrivacyClientFactory // 用于调用 chatgpt.com/backend-api（ImpersonateChrome）
}

func (s *OpenAIOAuthService) SetOfficialEgressRuntime(runtime *OfficialEgressTransitionRuntime) {
	s.officialEgress = runtime
}

// NewOpenAIOAuthService creates a new OpenAI OAuth service
func NewOpenAIOAuthService(proxyRepo ProxyRepository, oauthClient OpenAIOAuthClient) *OpenAIOAuthService {
	return &OpenAIOAuthService{
		sessionStore: openai.NewSessionStore(),
		proxyRepo:    proxyRepo,
		oauthClient:  oauthClient,
	}
}

// SetPrivacyClientFactory 注入 ImpersonateChrome 客户端工厂，
// 用于调用 chatgpt.com/backend-api 获取账号信息（plan_type 等）。
func (s *OpenAIOAuthService) SetPrivacyClientFactory(factory PrivacyClientFactory) {
	s.privacyClientFactory = factory
}

// OpenAIAuthURLResult contains the authorization URL and session info
type OpenAIAuthURLResult struct {
	AuthURL   string `json:"auth_url"`
	SessionID string `json:"session_id"`
}

// GenerateAuthURL generates an OpenAI OAuth authorization URL
func (s *OpenAIOAuthService) GenerateAuthURL(ctx context.Context, proxyID *int64, redirectURI, platform string) (*OpenAIAuthURLResult, error) {
	// Generate PKCE values
	state, err := openai.GenerateState()
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_OAUTH_STATE_FAILED", "failed to generate state: %v", err)
	}

	codeVerifier, err := openai.GenerateCodeVerifier()
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_OAUTH_VERIFIER_FAILED", "failed to generate code verifier: %v", err)
	}

	codeChallenge := openai.GenerateCodeChallenge(codeVerifier)

	// Generate session ID
	sessionID, err := openai.GenerateSessionID()
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_OAUTH_SESSION_FAILED", "failed to generate session ID: %v", err)
	}

	// Get proxy URL if specified
	var proxyURL string
	if proxyID != nil {
		proxy, err := s.proxyRepo.GetByID(ctx, *proxyID)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusBadRequest, "OPENAI_OAUTH_PROXY_NOT_FOUND", "proxy not found: %v", err)
		}
		if proxy != nil {
			proxyURL = proxy.URL()
		}
	}

	// Use default redirect URI if not specified
	if redirectURI == "" {
		redirectURI = openai.DefaultRedirectURI
	}
	normalizedPlatform := normalizeOpenAIOAuthPlatform(platform)
	clientID, _ := openai.OAuthClientConfigByPlatform(normalizedPlatform)

	// Store session
	session := &openai.OAuthSession{
		State:        state,
		CodeVerifier: codeVerifier,
		ClientID:     clientID,
		RedirectURI:  redirectURI,
		ProxyURL:     proxyURL,
		CreatedAt:    time.Now(),
	}
	s.sessionStore.Set(sessionID, session)

	// Build authorization URL
	authURL := openai.BuildAuthorizationURLForPlatform(state, codeChallenge, redirectURI, normalizedPlatform)

	return &OpenAIAuthURLResult{
		AuthURL:   authURL,
		SessionID: sessionID,
	}, nil
}

// OpenAIExchangeCodeInput represents the input for code exchange
type OpenAIExchangeCodeInput struct {
	SessionID   string
	Code        string
	State       string
	RedirectURI string
	ProxyID     *int64
}

// OpenAITokenInfo represents the token information for OpenAI
type OpenAITokenInfo struct {
	AccessToken           string              `json:"access_token"`
	RefreshToken          string              `json:"refresh_token"`
	IDToken               string              `json:"id_token,omitempty"`
	ExpiresIn             int64               `json:"expires_in"`
	ExpiresAt             int64               `json:"expires_at"`
	ClientID              string              `json:"client_id,omitempty"`
	AuthMode              string              `json:"auth_mode,omitempty"`
	Email                 string              `json:"email,omitempty"`
	ChatGPTAccountID      string              `json:"chatgpt_account_id,omitempty"`
	ChatGPTUserID         string              `json:"chatgpt_user_id,omitempty"`
	ChatGPTAccountFedRAMP bool                `json:"chatgpt_account_is_fedramp,omitempty"`
	OrganizationID        string              `json:"organization_id,omitempty"`
	PlanType              string              `json:"plan_type,omitempty"`
	SubscriptionExpiresAt string              `json:"subscription_expires_at,omitempty"`
	PrivacyMode           string              `json:"privacy_mode,omitempty"`
	PrivacyResult         OpenAIPrivacyResult `json:"-"`
}

// openAITokenEnrichmentInput 在一次 token 获取开始时冻结账号级 privacy 上下文。
type openAITokenEnrichmentInput struct {
	RolloutKey             string
	FallbackStableIdentity string
	ExistingExtra          map[string]any
	EnsurePrivacy          bool
	LocalAccountID         int64
}

// ExchangeCode 只兑换授权码并补全账号信息，不执行 privacy settings。
// 需要创建或重授权账号的生产链必须使用账号感知入口，避免产生无法原子持久化的
// privacy 结果。
func (s *OpenAIOAuthService) ExchangeCode(ctx context.Context, input *OpenAIExchangeCodeInput) (*OpenAITokenInfo, error) {
	return s.exchangeCode(ctx, input, openAITokenEnrichmentInput{
		FallbackStableIdentity: "oauth-session:" + input.SessionID,
	})
}

// ExchangeCodeForNewAccount 为服务端原子创建入口兑换授权码，并在账号写入前完成
// 首次 privacy 尝试。OAuth session 派生的稳定分桶键会随账号一起持久化。
func (s *OpenAIOAuthService) ExchangeCodeForNewAccount(
	ctx context.Context,
	input *OpenAIExchangeCodeInput,
) (*OpenAITokenInfo, error) {
	return s.exchangeCode(ctx, input, openAITokenEnrichmentInput{
		FallbackStableIdentity: "oauth-session:" + input.SessionID,
		EnsurePrivacy:          true,
	})
}

// ExchangeCodeForAccount 为重授权入口在任何 browser 请求前冻结账号已有的 rollout key
// 与冷却状态。即使新 token 补齐远端账号 ID，本轮三个端点仍使用同一个分桶键。
func (s *OpenAIOAuthService) ExchangeCodeForAccount(
	ctx context.Context,
	input *OpenAIExchangeCodeInput,
	account *Account,
) (*OpenAITokenInfo, error) {
	if account == nil || account.Platform != PlatformOpenAI {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_INVALID_ACCOUNT", "account is not an OpenAI account")
	}
	if account.Type != AccountTypeOAuth || account.IsCredentialShadow() {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_INVALID_ACCOUNT_TYPE", "account is not a writable OAuth account")
	}
	return s.exchangeCode(ctx, input, openAITokenEnrichmentInput{
		RolloutKey:    openAIPrivacyRolloutKeyForAccount(account),
		ExistingExtra: account.Extra,
		EnsurePrivacy: true,
	})
}

func (s *OpenAIOAuthService) exchangeCode(
	ctx context.Context,
	input *OpenAIExchangeCodeInput,
	enrichment openAITokenEnrichmentInput,
) (*OpenAITokenInfo, error) {
	// Get session
	session, ok := s.sessionStore.Get(input.SessionID)
	if !ok {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_SESSION_NOT_FOUND", "session not found or expired")
	}
	if input.State == "" {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_STATE_REQUIRED", "oauth state is required")
	}
	if subtle.ConstantTimeCompare([]byte(input.State), []byte(session.State)) != 1 {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_INVALID_STATE", "invalid oauth state")
	}

	// Get proxy URL: prefer input.ProxyID, fallback to session.ProxyURL
	proxyURL := session.ProxyURL
	if input.ProxyID != nil {
		proxy, err := s.proxyRepo.GetByID(ctx, *input.ProxyID)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusBadRequest, "OPENAI_OAUTH_PROXY_NOT_FOUND", "proxy not found: %v", err)
		}
		if proxy != nil {
			proxyURL = proxy.URL()
		}
	}

	// Use redirect URI from session or input
	redirectURI := session.RedirectURI
	if input.RedirectURI != "" {
		redirectURI = input.RedirectURI
	}
	clientID := strings.TrimSpace(session.ClientID)
	if clientID == "" {
		clientID = openai.ClientID
	}

	// Exchange code for token
	var err error
	if s.officialEgress != nil {
		ctx, err = officialegress.WithReleaseMode(ctx, s.officialEgress.CodexReleaseMode)
		if err != nil {
			return nil, infraerrors.Newf(
				http.StatusBadGateway,
				"OPENAI_OAUTH_RELEASE_MODE_INVALID",
				"bind OAuth exchange release mode: %v",
				err,
			)
		}
	}
	tokenResp, err := s.oauthClient.ExchangeCode(ctx, input.Code, session.CodeVerifier, redirectURI, proxyURL, clientID)
	if err != nil {
		return nil, err
	}

	// Parse ID token to get user info
	var userInfo *openai.UserInfo
	if tokenResp.IDToken != "" {
		claims, parseErr := openai.ParseIDToken(tokenResp.IDToken)
		if parseErr != nil {
			slog.Warn("openai_oauth_id_token_parse_failed", "error", parseErr)
		} else {
			userInfo = claims.GetUserInfo()
		}
	}

	// Delete session after successful exchange
	s.sessionStore.Delete(input.SessionID)

	tokenInfo := &OpenAITokenInfo{
		AccessToken:  tokenResp.AccessToken,
		RefreshToken: tokenResp.RefreshToken,
		IDToken:      tokenResp.IDToken,
		ExpiresIn:    int64(tokenResp.ExpiresIn),
		ExpiresAt:    time.Now().Unix() + int64(tokenResp.ExpiresIn),
		ClientID:     clientID,
	}

	if userInfo != nil {
		tokenInfo.Email = userInfo.Email
		tokenInfo.ChatGPTAccountID = userInfo.ChatGPTAccountID
		tokenInfo.ChatGPTUserID = userInfo.ChatGPTUserID
		tokenInfo.OrganizationID = userInfo.OrganizationID
		tokenInfo.PlanType = userInfo.PlanType
	}

	if strings.TrimSpace(enrichment.FallbackStableIdentity) == "" {
		enrichment.FallbackStableIdentity = "oauth-session:" + input.SessionID
	}
	s.enrichTokenInfo(ctx, tokenInfo, proxyURL, enrichment)

	return tokenInfo, nil
}

// RefreshToken refreshes an OpenAI OAuth token
func (s *OpenAIOAuthService) RefreshToken(ctx context.Context, refreshToken string, proxyURL string) (*OpenAITokenInfo, error) {
	return s.RefreshTokenWithClientID(ctx, refreshToken, proxyURL, "")
}

// RefreshTokenWithClientID refreshes an OpenAI OAuth token with optional client_id.
func (s *OpenAIOAuthService) RefreshTokenWithClientID(ctx context.Context, refreshToken string, proxyURL string, clientID string) (*OpenAITokenInfo, error) {
	return s.refreshTokenWithClientID(ctx, refreshToken, proxyURL, clientID, openAITokenEnrichmentInput{})
}

func (s *OpenAIOAuthService) refreshTokenWithClientID(
	ctx context.Context,
	refreshToken string,
	proxyURL string,
	clientID string,
	enrichment openAITokenEnrichmentInput,
) (*OpenAITokenInfo, error) {
	decoder, ok := s.oauthClient.(OpenAIOAuthRefreshResponseDecoder)
	if !ok {
		return nil, infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_OAUTH_REFRESH_DECODER_MISSING",
			"OpenAI OAuth refresh 缺少受管响应解码端口",
		)
	}
	if s.officialEgress == nil || s.officialEgress.BundleResolver == nil ||
		s.officialEgress.CodexExecutor == nil {
		return nil, infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_OAUTH_RELEASE_RUNTIME_MISSING",
			"OpenAI OAuth refresh 缺少正式 Executor runtime",
		)
	}
	response, transportErr := s.executeOAuthRefresh(
		ctx, refreshToken, clientID, proxyURL, enrichment.LocalAccountID,
	)
	tokenResp, err := decoder.DecodeRefreshResponse(ctx, response, transportErr, proxyURL)
	if err != nil {
		return nil, err
	}

	// Parse ID token to get user info
	var userInfo *openai.UserInfo
	if tokenResp.IDToken != "" {
		claims, parseErr := openai.ParseIDToken(tokenResp.IDToken)
		if parseErr != nil {
			slog.Warn("openai_oauth_id_token_parse_failed", "error", parseErr)
		} else {
			userInfo = claims.GetUserInfo()
		}
	}

	tokenInfo := &OpenAITokenInfo{
		AccessToken:  tokenResp.AccessToken,
		RefreshToken: tokenResp.RefreshToken,
		IDToken:      tokenResp.IDToken,
		ExpiresIn:    int64(tokenResp.ExpiresIn),
		ExpiresAt:    time.Now().Unix() + int64(tokenResp.ExpiresIn),
	}
	if trimmed := strings.TrimSpace(clientID); trimmed != "" {
		tokenInfo.ClientID = trimmed
	}

	if userInfo != nil {
		tokenInfo.Email = userInfo.Email
		tokenInfo.ChatGPTAccountID = userInfo.ChatGPTAccountID
		tokenInfo.ChatGPTUserID = userInfo.ChatGPTUserID
		tokenInfo.OrganizationID = userInfo.OrganizationID
		tokenInfo.PlanType = userInfo.PlanType
	}

	s.enrichTokenInfo(ctx, tokenInfo, proxyURL, enrichment)

	return tokenInfo, nil
}

func (s *OpenAIOAuthService) executeOAuthRefresh(
	ctx context.Context,
	refreshToken string,
	clientID string,
	proxyURL string,
	localAccountID ...int64,
) (*http.Response, error) {
	if s.officialEgress == nil || s.officialEgress.BundleResolver == nil ||
		s.officialEgress.CodexExecutor == nil {
		return nil, infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_OAUTH_RELEASE_RUNTIME_MISSING",
			"OpenAI OAuth refresh 缺少正式 Executor runtime",
		)
	}
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		clientID = openai.ClientID
	}
	execution := officialegress.ExecutionPolicy{
		ID: "oauth.refresh.execution.v1", Source: "openai_oauth_service",
		MaxAttempts: 1, Replayable: true, ConcurrencyLimit: 1,
	}
	deployment := officialegress.DeploymentSupportPolicy{
		ID: "oauth.refresh.deployment.v1", Source: "openai_oauth_service",
		Platform: "server", ProxyMode: "runtime",
		ProxyIdentityDigest: officialEgressProxyStateKey(proxyURL),
		SupportedBackends:   []officialegress.BackendKind{officialegress.BackendReqProfile},
	}
	behavior := officialegress.BehaviorPolicy{
		ID: "oauth.refresh.behavior.v1", Source: "openai_oauth_service",
		Kind: officialegress.BehaviorOAuth, AttemptBudget: 1,
	}
	bundle, err := s.officialEgress.BundleResolver.Resolve(officialegress.BundleResolveRequest{
		SinkID:    officialegress.SinkCodexOAuthRefresh,
		Mode:      s.officialEgress.CodexReleaseMode,
		Execution: execution, Deployment: deployment, Behavior: behavior,
	})
	if err != nil {
		return nil, infraerrors.Newf(
			http.StatusBadGateway, "OPENAI_OAUTH_RELEASE_RESOLVE_FAILED",
			"resolve OAuth refresh ReleaseBundle: %v", err,
		)
	}
	target, err := url.Parse("https://auth.openai.com/oauth/token")
	if err != nil {
		return nil, err
	}
	body, err := json.Marshal(struct {
		ClientID  string `json:"client_id"`
		GrantType string `json:"grant_type"`
	}{
		ClientID: clientID, GrantType: "refresh_token",
	})
	if err != nil {
		return nil, err
	}
	binding, ok := s.officialEgress.ProcessSinks.Resolve(officialegress.SinkCodexOAuthRefresh)
	if !ok {
		return nil, errors.New("OAuth refresh SinkBinding 缺失")
	}
	invocation, err := s.officialEgress.CodexExecutor.BeginInvocation(ctx, bundle, "")
	if err != nil {
		return nil, err
	}
	authentication, err := officialegress.NewAttemptAuthentication(
		officialegress.AttemptAuthenticationInput{RefreshToken: strings.TrimSpace(refreshToken)},
	)
	if err != nil {
		return nil, err
	}
	identityFacts, err := officialCodexProcessIdentityFactsForReleaseMode(
		string(bundle.Mode()), officialCodexEndpointOAuthRefresh,
	)
	if err != nil {
		return nil, err
	}
	accountIdentitySeed := "oauth-refresh-invocation:" + invocation.InvocationID()
	accountIdentitySource := officialegress.IdentitySourceInvocation
	accountIdentityLifecycle := officialegress.IdentityLifecycleInvocation
	if len(localAccountID) > 0 && localAccountID[0] > 0 {
		accountIdentitySeed = "openai-oauth-account:" + strconv.FormatInt(localAccountID[0], 10)
		accountIdentitySource = officialegress.IdentitySourceAccount
		accountIdentityLifecycle = officialegress.IdentityLifecycleAccount
	}
	accountIdentityDigest := sha256.Sum256([]byte(accountIdentitySeed))
	identityFacts.AccountIdentityProjection, err = officialegress.NewCodexIdentityValue(
		hex.EncodeToString(accountIdentityDigest[:]),
		accountIdentitySource,
		accountIdentityLifecycle,
	)
	if err != nil {
		return nil, err
	}
	transportContext := withOfficialCodexReqProfileTransport(ctx, proxyURL)
	result, err := invocation.ExecuteAttempt(
		transportContext,
		officialegress.ExecutorRequest{
			Bundle: bundle,
			Plan: officialegress.CodexEgressPlan{
				SinkID: officialegress.SinkCodexOAuthRefresh, Purpose: binding.Purpose(),
				EndpointID: officialCodexEndpointOAuthRefresh,
				Mode:       bundle.Mode(), Protocol: officialegress.WireProtocolHTTP,
				Method: http.MethodPost, URL: target,
				IdentityMode:   officialegress.IdentityCodexOAuthStrict,
				IdentityFacts:  identityFacts,
				Authentication: authentication,
				HeaderPolicy: officialegress.HeaderPolicy{
					ID: "oauth.refresh.headers.v1", Source: "openai_oauth_service",
				},
				BodyPolicy: officialegress.BodyPolicy{
					ID: "oauth.refresh.body.v1", Source: "openai_oauth_service",
				},
				BehaviorPolicy:  behavior,
				Body:            officialegress.NewReplayableRequestBody(body),
				InvocationID:    invocation.InvocationID(),
				DeclaredPersona: officialegress.PersonaCodexCLI,
			},
			AttemptReason:          officialegress.AttemptReasonInitial,
			ExpectedAttemptOrdinal: 1,
			ExecutionScopeKey:      "oauth-refresh",
		},
	)
	if err != nil {
		return nil, err
	}
	response := result.HTTPResponse()
	if response == nil {
		return nil, errors.New("OAuth refresh req-profile adapter 返回空响应")
	}
	return response, nil
}

// enrichTokenInfo 通过 ChatGPT backend-api 尽力补全 tokenInfo。三个 browser 端点
// 必须共用 enrichment 中冻结的 rollout key；只有首次授权明确要求时才调用 settings。
func (s *OpenAIOAuthService) enrichTokenInfo(
	ctx context.Context,
	tokenInfo *OpenAITokenInfo,
	proxyURL string,
	enrichment openAITokenEnrichmentInput,
) {
	if tokenInfo.AccessToken == "" || s.privacyClientFactory == nil {
		return
	}

	// 从 access_token JWT 中提取 orgID（poid），用于匹配正确的账号
	orgID := tokenInfo.OrganizationID
	if orgID == "" {
		if atClaims, err := openai.DecodeIDToken(tokenInfo.AccessToken); err == nil && atClaims.OpenAIAuth != nil {
			orgID = atClaims.OpenAIAuth.POID
		}
	}
	rolloutKey := normalizeOpenAIPrivacyRolloutKey(enrichment.RolloutKey)
	if rolloutKey == "" {
		rolloutKey = openAIPrivacyRolloutKeyForTokenInfo(tokenInfo, orgID, enrichment.FallbackStableIdentity)
	}
	if info := fetchChatGPTAccountInfo(
		ctx,
		s.privacyClientFactory,
		tokenInfo.AccessToken,
		proxyURL,
		orgID,
		rolloutKey,
	); info != nil {
		// chatgpt_plan_type from the ID token is the canonical personal-plan value.
		// accounts/check is a multi-account/workspace endpoint; inactive team or
		// business workspaces can otherwise overwrite Pro/Free with internal
		// workspace billing plan names such as self_serve_business_usage_based.
		if shouldApplyChatGPTAccountInfoPlanType(tokenInfo.PlanType, info.PlanType) {
			tokenInfo.PlanType = info.PlanType
		}
		if info.SubscriptionExpiresAt != "" {
			tokenInfo.SubscriptionExpiresAt = info.SubscriptionExpiresAt
		}
		if tokenInfo.Email == "" && info.Email != "" {
			tokenInfo.Email = info.Email
		}
	}
	if strings.TrimSpace(tokenInfo.SubscriptionExpiresAt) == "" {
		if expiresAt := fetchChatGPTSubscriptionExpiresAt(
			ctx,
			s.privacyClientFactory,
			tokenInfo.AccessToken,
			proxyURL,
			resolveChatGPTSubscriptionAccountID(tokenInfo, orgID),
			rolloutKey,
		); expiresAt != "" {
			tokenInfo.SubscriptionExpiresAt = expiresAt
		}
	}

	if enrichment.EnsurePrivacy {
		result := ensureOpenAITraining(
			ctx,
			s.privacyClientFactory,
			tokenInfo.AccessToken,
			proxyURL,
			openAIPrivacyEnsureInput{
				RolloutKey:    rolloutKey,
				ExistingExtra: enrichment.ExistingExtra,
			},
		)
		tokenInfo.PrivacyMode = result.Mode
		tokenInfo.PrivacyResult = result
	}
}

func openAIPrivacyRolloutKeyForTokenInfo(
	tokenInfo *OpenAITokenInfo,
	orgID string,
	fallbackStableIdentity string,
) string {
	if tokenInfo != nil {
		for _, candidate := range []string{
			tokenInfo.ChatGPTAccountID,
			tokenInfo.OrganizationID,
			orgID,
			tokenInfo.ChatGPTUserID,
		} {
			if strings.TrimSpace(candidate) != "" {
				return buildOpenAIPrivacyRolloutKey(candidate)
			}
		}
	}
	return buildOpenAIPrivacyRolloutKey(fallbackStableIdentity)
}

func shouldApplyChatGPTAccountInfoPlanType(current, candidate string) bool {
	return strings.TrimSpace(candidate) != "" && strings.TrimSpace(current) == ""
}

func resolveChatGPTSubscriptionAccountID(tokenInfo *OpenAITokenInfo, orgID string) string {
	for _, candidate := range []string{
		tokenInfo.ChatGPTAccountID,
		tokenInfo.OrganizationID,
		orgID,
	} {
		if trimmed := strings.TrimSpace(candidate); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

// RefreshAccountToken refreshes token for an OpenAI OAuth account
func (s *OpenAIOAuthService) RefreshAccountToken(ctx context.Context, account *Account) (*OpenAITokenInfo, error) {
	if account.Platform != PlatformOpenAI {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_INVALID_ACCOUNT", "account is not an OpenAI account")
	}
	if account.Type != AccountTypeOAuth {
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_INVALID_ACCOUNT_TYPE", "account is not an OAuth account")
	}

	var proxyURL string
	if account.ProxyID != nil && s.proxyRepo != nil {
		proxy, err := s.proxyRepo.GetByID(ctx, *account.ProxyID)
		if err == nil && proxy != nil {
			proxyURL = proxy.URL()
		}
	}

	accessToken := account.GetCredential("access_token")
	if account.IsOpenAIPersonalAccessToken() {
		if accessToken == "" {
			return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_CODEX_PAT_REQUIRED", "access token is required")
		}
		return s.ValidateCodexPersonalAccessToken(ctx, accessToken, proxyURL)
	}

	refreshToken := account.GetCredential("refresh_token")
	if refreshToken == "" {
		if accessToken != "" {
			tokenInfo := &OpenAITokenInfo{
				AccessToken:           accessToken,
				RefreshToken:          "",
				IDToken:               account.GetCredential("id_token"),
				ClientID:              account.GetCredential("client_id"),
				Email:                 account.GetCredential("email"),
				ChatGPTAccountID:      account.GetCredential("chatgpt_account_id"),
				ChatGPTUserID:         account.GetCredential("chatgpt_user_id"),
				OrganizationID:        account.GetCredential("organization_id"),
				PlanType:              account.GetCredential("plan_type"),
				SubscriptionExpiresAt: account.GetCredential("subscription_expires_at"),
			}
			if expiresAt := account.GetCredentialAsTime("expires_at"); expiresAt != nil {
				tokenInfo.ExpiresAt = expiresAt.Unix()
				tokenInfo.ExpiresIn = int64(time.Until(*expiresAt).Seconds())
			}
			s.enrichTokenInfo(ctx, tokenInfo, proxyURL, openAITokenEnrichmentInput{
				RolloutKey: openAIPrivacyRolloutKeyForAccount(account),
			})
			return tokenInfo, nil
		}
		return nil, infraerrors.New(http.StatusBadRequest, "OPENAI_OAUTH_NO_REFRESH_TOKEN", "no refresh token available")
	}

	clientID := account.GetCredential("client_id")
	return s.refreshTokenWithClientID(ctx, refreshToken, proxyURL, clientID, openAITokenEnrichmentInput{
		RolloutKey:     openAIPrivacyRolloutKeyForAccount(account),
		LocalAccountID: account.ID,
	})
}

// BuildAccountCredentials builds credentials map from token info
func (s *OpenAIOAuthService) BuildAccountCredentials(tokenInfo *OpenAITokenInfo) map[string]any {
	creds := map[string]any{
		"access_token": tokenInfo.AccessToken,
	}
	if tokenInfo.ExpiresAt > 0 {
		creds["expires_at"] = time.Unix(tokenInfo.ExpiresAt, 0).Format(time.RFC3339)
	}
	// 仅在刷新响应返回了新的 refresh_token 时才更新，防止用空值覆盖已有令牌
	if strings.TrimSpace(tokenInfo.RefreshToken) != "" {
		creds["refresh_token"] = tokenInfo.RefreshToken
	}

	if tokenInfo.IDToken != "" {
		creds["id_token"] = tokenInfo.IDToken
	}
	if tokenInfo.Email != "" {
		creds["email"] = tokenInfo.Email
	}
	if tokenInfo.ChatGPTAccountID != "" {
		creds["chatgpt_account_id"] = tokenInfo.ChatGPTAccountID
	}
	if tokenInfo.ChatGPTUserID != "" {
		creds["chatgpt_user_id"] = tokenInfo.ChatGPTUserID
	}
	if tokenInfo.OrganizationID != "" {
		creds["organization_id"] = tokenInfo.OrganizationID
	}
	if tokenInfo.PlanType != "" {
		creds["plan_type"] = tokenInfo.PlanType
	}
	if tokenInfo.SubscriptionExpiresAt != "" {
		creds["subscription_expires_at"] = tokenInfo.SubscriptionExpiresAt
	}
	if strings.TrimSpace(tokenInfo.ClientID) != "" {
		creds["client_id"] = strings.TrimSpace(tokenInfo.ClientID)
	}
	if tokenInfo.AuthMode == OpenAIAuthModePersonalAccessToken {
		creds[openAIAuthModeCredentialKey] = OpenAIAuthModePersonalAccessToken
		creds[openAIAuthModeLegacyCredentialKey] = "personal_access_token"
		creds["token_type"] = "Bearer"
		creds["chatgpt_account_is_fedramp"] = tokenInfo.ChatGPTAccountFedRAMP
	} else if tokenInfo.ChatGPTAccountFedRAMP {
		creds["chatgpt_account_is_fedramp"] = true
	}

	return NormalizeOpenAIPersonalAccessTokenCredentials(nil, tokenInfo, creds)
}

// BuildAccountExtra 将普通账号配置与服务端产生的 privacy 状态合并。客户端携带的
// 同名受管字段会被移除，确保账号与凭据在同一次 CreateAccount 写入中原子持久化
// 可信结果。刷新路径不得把该状态写进 Credentials。
func (s *OpenAIOAuthService) BuildAccountExtra(
	tokenInfo *OpenAITokenInfo,
	baseExtras ...map[string]any,
) map[string]any {
	var base map[string]any
	if len(baseExtras) > 0 {
		base = baseExtras[0]
	}
	var managed map[string]any
	if tokenInfo != nil {
		managed = tokenInfo.PrivacyResult.ExtraUpdates()
	}
	return mergeOpenAIPrivacyManagedExtra(base, managed)
}

// Stop stops the session store cleanup goroutine
func (s *OpenAIOAuthService) Stop() {
	s.sessionStore.Stop()
}

func normalizeOpenAIOAuthPlatform(platform string) string {
	return openai.OAuthPlatformOpenAI
}
