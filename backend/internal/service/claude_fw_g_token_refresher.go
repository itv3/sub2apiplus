package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/oauth"
	"github.com/google/uuid"
)

type claudeFWGTokenRefresher struct {
	legacy    *ClaudeTokenRefresher
	runtime   *officialegress.ClaudeCandidateRuntime
	proxyRepo ProxyRepository
}

func newClaudeFWGTokenRefresher(
	legacy *ClaudeTokenRefresher,
	runtime *officialegress.ClaudeCandidateRuntime,
	proxyRepo ProxyRepository,
) (*claudeFWGTokenRefresher, error) {
	if legacy == nil || runtime == nil || proxyRepo == nil {
		return nil, errors.New("Claude FW-G refresh 缺少 legacy policy、runtime 或 proxy repository")
	}
	return &claudeFWGTokenRefresher{legacy: legacy, runtime: runtime, proxyRepo: proxyRepo}, nil
}

func (r *claudeFWGTokenRefresher) CacheKey(account *Account) string {
	return r.legacy.CacheKey(account)
}

func (r *claudeFWGTokenRefresher) CanRefresh(account *Account) bool {
	return r.legacy.CanRefresh(account)
}

func (r *claudeFWGTokenRefresher) NeedsRefresh(account *Account, refreshWindow time.Duration) bool {
	return r.legacy.NeedsRefresh(account, refreshWindow)
}

func (r *claudeFWGTokenRefresher) Refresh(
	ctx context.Context,
	account *Account,
) (map[string]any, error) {
	if account == nil {
		return nil, errors.New("Claude FW-G refresh 账号为空")
	}
	if account.Platform != PlatformAnthropic || account.Type != AccountTypeOAuth {
		return r.legacy.Refresh(ctx, account)
	}
	accountUUID := strings.TrimSpace(account.GetExtraString("account_uuid"))
	if _, err := uuid.Parse(accountUUID); err != nil {
		return nil, errors.New("Claude FW-G refresh 缺少有效 account_uuid")
	}
	refreshToken := strings.TrimSpace(account.GetCredential("refresh_token"))
	if refreshToken == "" {
		return nil, errors.New("Claude FW-G refresh 缺少 refresh_token")
	}
	scope := strings.TrimSpace(account.GetCredential("scope"))
	if scope == "" {
		scope = oauth.ScopeAPI
	}
	proxyURL, err := r.resolveProxyURL(ctx, account)
	if err != nil {
		return nil, err
	}
	sessionID := uuid.NewString()
	trusted := officialegress.ClaudeTrustedFacts{
		Account: officialegress.ClaudeTrustedAccountFacts{
			AccountScope: "anthropic-oauth-account:" + strconv.FormatInt(account.ID, 10),
			AccountUUID:  accountUUID,
		},
		Session: officialegress.ClaudeTrustedSessionFacts{
			SessionID: sessionID,
			Source:    officialegress.ClaudeSessionSourcePlannerDerived,
		},
		Entrypoint: officialegress.ClaudeTrustedEntrypointFacts{
			Entrypoint:       officialegress.ClaudeEntrypointSDKCLI,
			IngressProtocol:  "managed-internal",
			IngressBindingID: "oauth-refresh:" + strconv.FormatInt(account.ID, 10),
		},
		Features: officialegress.ClaudeTrustedFeatureFacts{
			SystemMode: officialegress.ClaudeSystemDefault,
		},
	}
	executionContext := withClaudeCandidateHTTPTransport(
		ctx,
		proxyURL,
		account.ID,
		claudeFWGConcurrencyLimit(account),
	)
	response, err := r.runtime.ExecuteEndpoint(executionContext, officialegress.ClaudeEndpointExecution{
		EndpointKind: "oauth-token-refresh",
		RefreshToken: refreshToken,
		ClientID:     oauth.ClientID,
		RefreshScope: scope,
		TrustedFacts: trusted,
		InvocationID: uuid.NewString(),
	})
	if err != nil {
		return nil, fmt.Errorf("Claude FW-G refresh strict 执行失败：%w", err)
	}
	if response == nil || response.Body == nil {
		return nil, errors.New("Claude FW-G refresh 返回空响应")
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64<<10))
		return nil, fmt.Errorf("Claude FW-G refresh 上游状态 %d", response.StatusCode)
	}
	var tokenResponse struct {
		AccessToken  string `json:"access_token"`
		TokenType    string `json:"token_type"`
		ExpiresIn    int64  `json:"expires_in"`
		RefreshToken string `json:"refresh_token"`
		Scope        string `json:"scope"`
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 1<<20))
	if err := decoder.Decode(&tokenResponse); err != nil {
		return nil, fmt.Errorf("解析 Claude FW-G refresh 响应：%w", err)
	}
	if strings.TrimSpace(tokenResponse.AccessToken) == "" || tokenResponse.ExpiresIn <= 0 {
		return nil, errors.New("Claude FW-G refresh 响应缺少 access_token 或 expires_in")
	}
	if strings.TrimSpace(tokenResponse.RefreshToken) == "" {
		tokenResponse.RefreshToken = refreshToken
	}
	if strings.TrimSpace(tokenResponse.Scope) == "" {
		tokenResponse.Scope = scope
	}
	tokenInfo := &TokenInfo{
		AccessToken:  tokenResponse.AccessToken,
		TokenType:    tokenResponse.TokenType,
		ExpiresIn:    tokenResponse.ExpiresIn,
		ExpiresAt:    time.Now().Unix() + tokenResponse.ExpiresIn,
		RefreshToken: tokenResponse.RefreshToken,
		Scope:        tokenResponse.Scope,
	}
	return MergeCredentials(account.Credentials, BuildClaudeAccountCredentials(tokenInfo)), nil
}

func (r *claudeFWGTokenRefresher) resolveProxyURL(
	ctx context.Context,
	account *Account,
) (string, error) {
	if account.ProxyID == nil {
		return "", nil
	}
	if account.Proxy != nil {
		return account.Proxy.URL(), nil
	}
	proxy, err := r.proxyRepo.GetByID(ctx, *account.ProxyID)
	if err != nil {
		return "", fmt.Errorf("读取 Claude FW-G refresh 代理：%w", err)
	}
	if proxy == nil {
		return "", errors.New("Claude FW-G refresh 配置的代理不存在")
	}
	return proxy.URL(), nil
}
