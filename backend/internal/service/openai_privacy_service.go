package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/imroc/req/v3"
)

// PrivacyClientRequest 只包含选择 privacy 画像所需的非敏感参数。
type PrivacyClientRequest struct {
	ProxyURL   string
	RolloutKey string
}

// PrivacyHTTPClient 将已选画像和失败冷却策略与 HTTP 客户端一起冻结。
type PrivacyHTTPClient struct {
	Client          *req.Client
	Persona         string
	FailureCooldown time.Duration
}

// PrivacyClientFactory 由 repository 层注入，避免 service 反向依赖具体传输实现。
type PrivacyClientFactory func(request PrivacyClientRequest) (*PrivacyHTTPClient, error)

// OpenAIPrivacyResult 是训练数据关闭尝试的可持久化结果。
type OpenAIPrivacyResult struct {
	Mode       string
	Persona    string
	RetryAfter time.Time
	RolloutKey string
}

// openAIPrivacyEnsureInput 把账号级稳定分桶、已有持久化状态和显式强制语义冻结在
// settings 请求之前。任何调用方都不得绕过该输入直接发送训练数据设置请求。
type openAIPrivacyEnsureInput struct {
	RolloutKey    string
	ExistingExtra map[string]any
	Force         bool
}

const (
	openAISettingsURL = "https://chatgpt.com/backend-api/settings/account_user_setting"

	PrivacyModeTrainingOff = "training_off"
	PrivacyModeFailed      = "training_set_failed"
	PrivacyModeCFBlocked   = "training_set_cf_blocked"

	privacyModeExtraKey       = "privacy_mode"
	privacyRetryAfterExtraKey = "privacy_retry_after"
	privacyPersonaExtraKey    = "privacy_browser_persona"
	privacyRolloutKeyExtraKey = "privacy_rollout_key"
)

var openAIPrivacyManagedExtraKeys = [...]string{
	privacyModeExtraKey,
	privacyRetryAfterExtraKey,
	privacyPersonaExtraKey,
	privacyRolloutKeyExtraKey,
}

func shouldSkipOpenAIPrivacyEnsure(extra map[string]any) bool {
	return shouldSkipOpenAIPrivacyEnsureAt(extra, time.Now())
}

func shouldSkipOpenAIPrivacyEnsureAt(extra map[string]any, now time.Time) bool {
	if extra == nil {
		return false
	}
	raw, ok := extra[privacyModeExtraKey]
	if !ok {
		return false
	}
	mode, _ := raw.(string)
	mode = strings.TrimSpace(mode)
	if mode == "" {
		return false
	}
	if mode != PrivacyModeFailed && mode != PrivacyModeCFBlocked {
		return true
	}
	retryAfter, _ := extra[privacyRetryAfterExtraKey].(string)
	parsed, err := time.Parse(time.RFC3339, strings.TrimSpace(retryAfter))
	return err == nil && now.Before(parsed)
}

// ensureOpenAITraining 在任何 settings 网络请求之前读取已有状态和冷却时间。
// Force 只允许管理端显式操作使用；OAuth 刷新和后台刷新必须保持 false。
func ensureOpenAITraining(
	ctx context.Context,
	clientFactory PrivacyClientFactory,
	accessToken string,
	proxyURL string,
	input openAIPrivacyEnsureInput,
) OpenAIPrivacyResult {
	if !input.Force && shouldSkipOpenAIPrivacyEnsure(input.ExistingExtra) {
		return OpenAIPrivacyResult{}
	}
	return disableOpenAITraining(ctx, clientFactory, accessToken, proxyURL, input.RolloutKey)
}

// disableOpenAITraining 是训练数据设置的唯一低层网络实现，只能由 ensureOpenAITraining 调用。
func disableOpenAITraining(
	ctx context.Context,
	clientFactory PrivacyClientFactory,
	accessToken string,
	proxyURL string,
	rolloutKey string,
) OpenAIPrivacyResult {
	if accessToken == "" || clientFactory == nil {
		return OpenAIPrivacyResult{}
	}

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	boundCtx, bindErr := bindOfficialEgressSink(ctx, officialEgressSinkPrivacyDisableTraining)
	if bindErr != nil {
		slog.Warn("openai_privacy_sink_binding_error", "error", bindErr.Error())
		recordOpenAIPrivacyPersonaResult("disable_training", "unknown", "binding_error", 0)
		return OpenAIPrivacyResult{Mode: PrivacyModeFailed, RolloutKey: rolloutKey}
	}
	ctx = boundCtx

	client, err := clientFactory(PrivacyClientRequest{
		ProxyURL:   proxyURL,
		RolloutKey: rolloutKey,
	})
	if err != nil {
		slog.Warn("openai_privacy_client_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("disable_training", "unknown", "client_error", 0)
		return newOpenAIPrivacyFailureResult(PrivacyModeFailed, client, rolloutKey)
	}
	if client == nil || client.Client == nil {
		slog.Warn("openai_privacy_client_error", "error", "privacy client is nil")
		recordOpenAIPrivacyPersonaResult("disable_training", "unknown", "client_error", 0)
		return OpenAIPrivacyResult{Mode: PrivacyModeFailed, RolloutKey: rolloutKey}
	}

	resp, err := setOpenAIPrivacyXHRHeaders(client.Client.R(), accessToken).
		SetContext(ctx).
		SetQueryParam("feature", "training_allowed").
		SetQueryParam("value", "false").
		Patch(openAISettingsURL)

	if err != nil {
		slog.Warn("openai_privacy_request_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("disable_training", client.Persona, "request_error", 0)
		return newOpenAIPrivacyFailureResult(PrivacyModeFailed, client, rolloutKey)
	}

	if resp.StatusCode == 403 || resp.StatusCode == 503 {
		body := resp.String()
		if strings.Contains(body, "cloudflare") || strings.Contains(body, "cf-") || strings.Contains(body, "Just a moment") {
			slog.Warn("openai_privacy_cf_blocked", "status", resp.StatusCode, "persona", client.Persona)
			recordOpenAIPrivacyPersonaResult("disable_training", client.Persona, "cf_blocked", resp.StatusCode)
			return newOpenAIPrivacyFailureResult(PrivacyModeCFBlocked, client, rolloutKey)
		}
	}

	if !resp.IsSuccessState() {
		slog.Warn("openai_privacy_failed", "status", resp.StatusCode, "persona", client.Persona)
		recordOpenAIPrivacyPersonaResult("disable_training", client.Persona, "upstream_error", resp.StatusCode)
		return newOpenAIPrivacyFailureResult(PrivacyModeFailed, client, rolloutKey)
	}

	slog.Info("openai_privacy_training_disabled", "persona", client.Persona)
	recordOpenAIPrivacyPersonaResult("disable_training", client.Persona, "success", resp.StatusCode)
	return OpenAIPrivacyResult{
		Mode: PrivacyModeTrainingOff, Persona: client.Persona, RolloutKey: rolloutKey,
	}
}

func newOpenAIPrivacyFailureResult(mode string, client *PrivacyHTTPClient, rolloutKey string) OpenAIPrivacyResult {
	result := OpenAIPrivacyResult{Mode: mode, RolloutKey: rolloutKey}
	if client == nil {
		return result
	}
	result.Persona = client.Persona
	if client.FailureCooldown > 0 {
		result.RetryAfter = time.Now().UTC().Add(client.FailureCooldown)
	}
	return result
}

// ExtraUpdates 返回可原子合并到账号 Extra 的 privacy 结果。
func (r OpenAIPrivacyResult) ExtraUpdates() map[string]any {
	if strings.TrimSpace(r.Mode) == "" {
		return nil
	}
	updates := map[string]any{privacyModeExtraKey: r.Mode}
	if strings.TrimSpace(r.Persona) != "" {
		updates[privacyPersonaExtraKey] = r.Persona
	}
	if !r.RetryAfter.IsZero() {
		updates[privacyRetryAfterExtraKey] = r.RetryAfter.UTC().Format(time.RFC3339)
	}
	if rolloutKey := normalizeOpenAIPrivacyRolloutKey(r.RolloutKey); rolloutKey != "" {
		updates[privacyRolloutKeyExtraKey] = rolloutKey
	}
	return updates
}

// mergeOpenAIPrivacyManagedExtra 先移除客户端可能携带的受管 privacy 字段，再应用
// 服务端计算结果。base 中的普通账号配置保持不变。
func mergeOpenAIPrivacyManagedExtra(base, managed map[string]any) map[string]any {
	out := make(map[string]any, len(base)+len(managed))
	for key, value := range base {
		if !isOpenAIPrivacyManagedExtraKey(key) {
			out[key] = value
		}
	}
	for _, key := range openAIPrivacyManagedExtraKeys {
		if value, ok := managed[key]; ok {
			out[key] = value
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func isOpenAIPrivacyManagedExtraKey(key string) bool {
	for _, managedKey := range openAIPrivacyManagedExtraKeys {
		if key == managedKey {
			return true
		}
	}
	return false
}

// buildOpenAIPrivacyRolloutKey 只接受不会随 token 刷新轮换的账号级稳定标识。
func buildOpenAIPrivacyRolloutKey(stableAccountIdentity string) string {
	stableAccountIdentity = strings.TrimSpace(stableAccountIdentity)
	if stableAccountIdentity == "" {
		return ""
	}
	digest := sha256.Sum256([]byte("openai_privacy_account\x00" + stableAccountIdentity))
	return hex.EncodeToString(digest[:16])
}

func normalizeOpenAIPrivacyRolloutKey(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if len(value) != 32 {
		return ""
	}
	if _, err := hex.DecodeString(value); err != nil {
		return ""
	}
	return value
}

// openAIPrivacyRolloutKeyForAccount 优先复用首次授权时原子持久化的分桶键；
// 老账号按远端账号身份派生，字段缺失时才使用本地数据库账号 ID。
func openAIPrivacyRolloutKeyForAccount(account *Account) string {
	if account == nil {
		return ""
	}
	if account.Extra != nil {
		if stored, _ := account.Extra[privacyRolloutKeyExtraKey].(string); stored != "" {
			if normalized := normalizeOpenAIPrivacyRolloutKey(stored); normalized != "" {
				return normalized
			}
		}
	}
	for _, candidate := range []string{
		account.GetCredential("chatgpt_account_id"),
		account.GetCredential("organization_id"),
		account.GetCredential("chatgpt_user_id"),
	} {
		if strings.TrimSpace(candidate) != "" {
			return buildOpenAIPrivacyRolloutKey(candidate)
		}
	}
	if account.ID > 0 {
		return buildOpenAIPrivacyRolloutKey(fmt.Sprintf("local-account:%d", account.ID))
	}
	return ""
}

// ensureOpenAIPrivacyForAccount 是已有账号触发 settings 请求的唯一入口。
func ensureOpenAIPrivacyForAccount(
	ctx context.Context,
	clientFactory PrivacyClientFactory,
	account *Account,
	proxyURL string,
	force bool,
) OpenAIPrivacyResult {
	if account == nil {
		return OpenAIPrivacyResult{}
	}
	return ensureOpenAITraining(
		ctx,
		clientFactory,
		account.GetCredential("access_token"),
		proxyURL,
		openAIPrivacyEnsureInput{
			RolloutKey:    openAIPrivacyRolloutKeyForAccount(account),
			ExistingExtra: account.Extra,
			Force:         force,
		},
	)
}

func setOpenAIPrivacyBaseHeaders(request *req.Request, accessToken string) *req.Request {
	return request.
		SetHeader("Authorization", "Bearer "+accessToken).
		SetHeader("Origin", "https://chatgpt.com").
		SetHeader("Referer", "https://chatgpt.com/").
		SetHeader("Accept", "application/json")
}

func setOpenAIPrivacyXHRHeaders(request *req.Request, accessToken string) *req.Request {
	return setOpenAIPrivacyBaseHeaders(request, accessToken).
		SetHeader("sec-fetch-mode", "cors").
		SetHeader("sec-fetch-site", "same-origin").
		SetHeader("sec-fetch-dest", "empty")
}

func setOpenAIPrivacyPersonaHeaders(request *req.Request, accessToken, persona string) *req.Request {
	if persona == "chrome_133_xhr" {
		return setOpenAIPrivacyXHRHeaders(request, accessToken)
	}
	return setOpenAIPrivacyBaseHeaders(request, accessToken)
}

// OpenAIPrivacyPersonaMetricsSnapshot 提供按 endpoint/persona/outcome 分组的进程内累计值。
type OpenAIPrivacyPersonaMetricsSnapshot struct {
	Results map[string]uint64 `json:"results"`
}

var openAIPrivacyPersonaMetrics = struct {
	sync.Mutex
	results map[string]uint64
}{results: make(map[string]uint64)}

// SnapshotOpenAIPrivacyPersonaMetrics 返回当前 privacy 画像的通过率对照指标。
func SnapshotOpenAIPrivacyPersonaMetrics() OpenAIPrivacyPersonaMetricsSnapshot {
	openAIPrivacyPersonaMetrics.Lock()
	defer openAIPrivacyPersonaMetrics.Unlock()
	results := make(map[string]uint64, len(openAIPrivacyPersonaMetrics.results))
	for key, value := range openAIPrivacyPersonaMetrics.results {
		results[key] = value
	}
	return OpenAIPrivacyPersonaMetricsSnapshot{Results: results}
}

func recordOpenAIPrivacyPersonaResult(endpoint, persona, outcome string, status int) {
	endpoint = normalizeOpenAIPrivacyMetricLabel(endpoint, []string{"disable_training", "account_info", "subscription"})
	persona = normalizeOpenAIPrivacyMetricLabel(persona, []string{"chrome_133_xhr", "legacy_chrome_120", "unknown"})
	outcome = normalizeOpenAIPrivacyMetricLabel(outcome, []string{
		"success",
		"cf_blocked",
		"upstream_error",
		"request_error",
		"client_error",
		"binding_error",
		"invalid_payload",
	})
	key := endpoint + "|" + persona + "|" + outcome
	openAIPrivacyPersonaMetrics.Lock()
	openAIPrivacyPersonaMetrics.results[key]++
	openAIPrivacyPersonaMetrics.Unlock()
	slog.Info("openai_privacy_persona_result",
		"endpoint", endpoint,
		"persona", persona,
		"outcome", outcome,
		"status", status,
	)
}

func normalizeOpenAIPrivacyMetricLabel(value string, allowed []string) string {
	value = strings.TrimSpace(value)
	for _, candidate := range allowed {
		if value == candidate {
			return value
		}
	}
	return "unknown"
}

func privacyHTTPOutcome(status int) string {
	if status == 403 || status == 503 {
		return "cf_blocked"
	}
	return "upstream_error"
}

// ChatGPTAccountInfo 从 chatgpt.com/backend-api/accounts/check 获取的账号信息
type ChatGPTAccountInfo struct {
	PlanType string
	Email    string
	// AccountID 是本条信息所属账号的标识（优先取 account.account_id，否则取 accounts
	// 的 map key）。accounts/check 是多账号/工作区端点，调用方需要据此判断拿到的
	// plan_type / expires_at 到底属于个人账号还是某个 workspace。
	AccountID             string
	SubscriptionExpiresAt string // entitlement.expires_at (RFC3339)
}

var (
	chatGPTAccountsCheckURL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
	chatGPTSubscriptionsURL = "https://chatgpt.com/backend-api/subscriptions"
)

// fetchChatGPTAccountInfo calls ChatGPT backend-api to get account info (plan_type, etc.).
// Used as fallback when id_token doesn't contain these fields (e.g., Mobile RT).
// orgID is used to match the correct account when multiple accounts exist (e.g., personal + team).
// Returns nil on any failure (best-effort, non-blocking).
func fetchChatGPTAccountInfo(
	ctx context.Context,
	clientFactory PrivacyClientFactory,
	accessToken string,
	proxyURL string,
	orgID string,
	rolloutKey string,
) *ChatGPTAccountInfo {
	if accessToken == "" || clientFactory == nil {
		return nil
	}

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	boundCtx, bindErr := bindOfficialEgressSink(ctx, officialEgressSinkPrivacyAccountInfo)
	if bindErr != nil {
		slog.Debug("chatgpt_account_check_sink_binding_error", "error", bindErr.Error())
		return nil
	}
	ctx = boundCtx

	client, err := clientFactory(PrivacyClientRequest{
		ProxyURL:   proxyURL,
		RolloutKey: rolloutKey,
	})
	if err != nil {
		slog.Debug("chatgpt_account_check_client_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("account_info", "unknown", "client_error", 0)
		return nil
	}
	if client == nil || client.Client == nil {
		slog.Debug("chatgpt_account_check_client_error", "error", "privacy client is nil")
		recordOpenAIPrivacyPersonaResult("account_info", "unknown", "client_error", 0)
		return nil
	}

	var result map[string]any
	resp, err := setOpenAIPrivacyPersonaHeaders(client.Client.R(), accessToken, client.Persona).
		SetContext(ctx).
		SetSuccessResult(&result).
		Get(chatGPTAccountsCheckURL)

	if err != nil {
		slog.Debug("chatgpt_account_check_request_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("account_info", client.Persona, "request_error", 0)
		return nil
	}

	if !resp.IsSuccessState() {
		slog.Debug("chatgpt_account_check_failed", "status", resp.StatusCode, "persona", client.Persona)
		recordOpenAIPrivacyPersonaResult("account_info", client.Persona, privacyHTTPOutcome(resp.StatusCode), resp.StatusCode)
		return nil
	}

	info := &ChatGPTAccountInfo{}

	accounts, ok := result["accounts"].(map[string]any)
	if !ok {
		slog.Debug("chatgpt_account_check_no_accounts", "persona", client.Persona)
		recordOpenAIPrivacyPersonaResult("account_info", client.Persona, "invalid_payload", resp.StatusCode)
		return nil
	}

	// 优先匹配 orgID 对应的账号（access_token JWT 中的 poid）
	if orgID != "" {
		if acctRaw, ok := accounts[orgID]; ok {
			if acct, ok := acctRaw.(map[string]any); ok {
				if isUsableChatGPTAccountCandidate(acct, time.Now()) {
					fillAccountInfo(info, acct, orgID)
				}
			}
		}
	}

	// 未匹配到时，遍历所有账号：优先 is_default，次选非 free
	if info.PlanType == "" {
		type candidate struct {
			planType  string
			expiresAt string
			accountID string
		}
		var defaultC, paidC, anyC candidate
		for key, acctRaw := range accounts {
			acct, ok := acctRaw.(map[string]any)
			if !ok {
				continue
			}
			if !isUsableChatGPTAccountCandidate(acct, time.Now()) {
				continue
			}
			planType := extractPlanType(acct)
			if planType == "" {
				continue
			}
			ea := extractEntitlementExpiresAt(acct)
			id := chatGPTAccountObjectID(acct, key)
			if anyC.planType == "" {
				anyC = candidate{planType, ea, id}
			}
			if account, ok := acct["account"].(map[string]any); ok {
				if isDefault, _ := account["is_default"].(bool); isDefault {
					defaultC = candidate{planType, ea, id}
				}
			}
			if !strings.EqualFold(planType, "free") && paidC.planType == "" {
				paidC = candidate{planType, ea, id}
			}
		}
		// 优先级：default > 非 free > 任意
		switch {
		case defaultC.planType != "":
			info.PlanType, info.SubscriptionExpiresAt, info.AccountID = defaultC.planType, defaultC.expiresAt, defaultC.accountID
		case paidC.planType != "":
			info.PlanType, info.SubscriptionExpiresAt, info.AccountID = paidC.planType, paidC.expiresAt, paidC.accountID
		default:
			info.PlanType, info.SubscriptionExpiresAt, info.AccountID = anyC.planType, anyC.expiresAt, anyC.accountID
		}
	}

	if info.PlanType == "" {
		slog.Debug("chatgpt_account_check_no_plan_type", "persona", client.Persona)
		recordOpenAIPrivacyPersonaResult("account_info", client.Persona, "invalid_payload", resp.StatusCode)
		return nil
	}

	slog.Info("chatgpt_account_check_success", "plan_type", info.PlanType, "persona", client.Persona)
	recordOpenAIPrivacyPersonaResult("account_info", client.Persona, "success", resp.StatusCode)
	return info
}

// fetchChatGPTSubscriptionExpiresAt reads the lightweight subscription endpoint used by
// ChatGPT/Codex clients. Some Plus accounts no longer expose entitlement.expires_at in
// accounts/check, but this endpoint still returns active_until.
func fetchChatGPTSubscriptionExpiresAt(
	ctx context.Context,
	clientFactory PrivacyClientFactory,
	accessToken string,
	proxyURL string,
	accountID string,
	rolloutKey string,
) string {
	accountID = strings.TrimSpace(accountID)
	if accessToken == "" || accountID == "" || clientFactory == nil {
		return ""
	}

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	boundCtx, bindErr := bindOfficialEgressSink(ctx, officialEgressSinkPrivacySubscription)
	if bindErr != nil {
		slog.Debug("chatgpt_subscription_sink_binding_error", "error", bindErr.Error())
		return ""
	}
	ctx = boundCtx

	client, err := clientFactory(PrivacyClientRequest{
		ProxyURL:   proxyURL,
		RolloutKey: rolloutKey,
	})
	if err != nil {
		slog.Debug("chatgpt_subscription_client_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("subscription", "unknown", "client_error", 0)
		return ""
	}
	if client == nil || client.Client == nil {
		slog.Debug("chatgpt_subscription_client_error", "error", "privacy client is nil")
		recordOpenAIPrivacyPersonaResult("subscription", "unknown", "client_error", 0)
		return ""
	}

	var result struct {
		PlanType    string `json:"plan_type"`
		ActiveUntil string `json:"active_until"`
		WillRenew   bool   `json:"will_renew"`
		ID          string `json:"id"`
	}
	resp, err := setOpenAIPrivacyPersonaHeaders(client.Client.R(), accessToken, client.Persona).
		SetContext(ctx).
		SetSuccessResult(&result).
		SetQueryParam("account_id", accountID).
		Get(chatGPTSubscriptionsURL)
	if err != nil {
		slog.Debug("chatgpt_subscription_request_error", "error", err.Error())
		recordOpenAIPrivacyPersonaResult("subscription", client.Persona, "request_error", 0)
		return ""
	}
	if !resp.IsSuccessState() {
		slog.Debug("chatgpt_subscription_failed", "status", resp.StatusCode, "persona", client.Persona)
		recordOpenAIPrivacyPersonaResult("subscription", client.Persona, privacyHTTPOutcome(resp.StatusCode), resp.StatusCode)
		return ""
	}

	activeUntil := strings.TrimSpace(result.ActiveUntil)
	if activeUntil == "" {
		slog.Debug("chatgpt_subscription_no_active_until", "plan_type", result.PlanType, "has_subscription_id", strings.TrimSpace(result.ID) != "", "will_renew", result.WillRenew)
		recordOpenAIPrivacyPersonaResult("subscription", client.Persona, "invalid_payload", resp.StatusCode)
		return ""
	}
	if _, err := time.Parse(time.RFC3339, activeUntil); err != nil {
		slog.Debug("chatgpt_subscription_bad_active_until", "active_until", activeUntil, "error", err.Error())
		recordOpenAIPrivacyPersonaResult("subscription", client.Persona, "invalid_payload", resp.StatusCode)
		return ""
	}

	slog.Info("chatgpt_subscription_success", "plan_type", result.PlanType, "persona", client.Persona)
	recordOpenAIPrivacyPersonaResult("subscription", client.Persona, "success", resp.StatusCode)
	return activeUntil
}

// fillAccountInfo 从单个 account 对象中提取 plan_type 和 subscription_expires_at。
// fallbackID 是该对象在 accounts 里的 map key，用于 account.account_id 缺失时兜底。
func fillAccountInfo(info *ChatGPTAccountInfo, acct map[string]any, fallbackID string) {
	info.PlanType = extractPlanType(acct)
	info.SubscriptionExpiresAt = extractEntitlementExpiresAt(acct)
	info.AccountID = chatGPTAccountObjectID(acct, fallbackID)
}

// chatGPTAccountObjectID 取单个 account 对象的账号标识。
// accounts 的 map key 有时是 "default" 这类别名，所以优先读 account.account_id。
func chatGPTAccountObjectID(acct map[string]any, fallbackID string) string {
	if account, ok := acct["account"].(map[string]any); ok {
		if id, ok := account["account_id"].(string); ok && strings.TrimSpace(id) != "" {
			return strings.TrimSpace(id)
		}
	}
	return strings.TrimSpace(fallbackID)
}

// extractPlanType 从单个 account 对象中提取 plan_type
func extractPlanType(acct map[string]any) string {
	if account, ok := acct["account"].(map[string]any); ok {
		if planType, ok := account["plan_type"].(string); ok && planType != "" {
			return planType
		}
	}
	if entitlement, ok := acct["entitlement"].(map[string]any); ok {
		if subPlan, ok := entitlement["subscription_plan"].(string); ok && subPlan != "" {
			return subPlan
		}
	}
	return ""
}

func isUsableChatGPTAccountCandidate(acct map[string]any, now time.Time) bool {
	if acct == nil || hasChatGPTAccountDeactivatedMarker(acct) {
		return false
	}
	if account, ok := acct["account"].(map[string]any); ok && hasChatGPTAccountDeactivatedMarker(account) {
		return false
	}

	expiresAt := extractEntitlementExpiresAt(acct)
	if expiresAt == "" {
		return true
	}
	expiry, err := time.Parse(time.RFC3339, expiresAt)
	if err != nil {
		return true
	}
	return expiry.After(now)
}

func hasChatGPTAccountDeactivatedMarker(obj map[string]any) bool {
	for _, key := range []string{"deactivated", "is_deactivated", "disabled", "is_disabled"} {
		if value, ok := obj[key].(bool); ok && value {
			return true
		}
	}
	for _, key := range []string{"deactivated_at", "disabled_at", "deleted_at"} {
		if value, ok := obj[key].(string); ok && strings.TrimSpace(value) != "" {
			return true
		}
	}
	for _, key := range []string{"status", "state"} {
		value, _ := obj[key].(string)
		switch strings.ToLower(strings.TrimSpace(value)) {
		case "deactivated", "disabled", "deleted", "inactive", "suspended":
			return true
		}
	}
	return false
}

// extractEntitlementExpiresAt 从 entitlement 中提取 expires_at。
// 预期为 RFC3339 字符串格式，如 "2026-05-02T20:32:12+00:00"。
func extractEntitlementExpiresAt(acct map[string]any) string {
	entitlement, ok := acct["entitlement"].(map[string]any)
	if !ok {
		return ""
	}
	ea, _ := entitlement["expires_at"].(string)
	return ea
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + fmt.Sprintf("...(%d more)", len(s)-n)
}
