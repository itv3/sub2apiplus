package service

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

// ErrSparkShadowResetNotSupported is returned when ResetCredit is called on a
// spark shadow account. Shadow accounts do not hold credentials of their own;
// the caller must reset the parent account directly. It is a structured
// infraerrors value so the handler maps it to 409 Conflict (not a bare 500);
// errors.Is still matches it by identity since ResetCredit returns this var.
var ErrSparkShadowResetNotSupported = infraerrors.New(http.StatusConflict, "SPARK_SHADOW_RESET_NOT_SUPPORTED", "spark shadow account does not support credit reset; reset the parent account")

// OpenAI/ChatGPT/Codex 配额查询与重置使用的运行参数。
const (
	openaiQuotaUpstreamTimeout   = 20 * time.Second
	openaiQuotaUpstreamBodyLimit = 2 << 20
	openaiQuotaResetCreditsKey   = "codex_reset_credit_snapshot"
)

// OpenAIRateLimitWindow describes a single rate-limit window returned by
// /wham/usage. The upstream returns an explicit `null` window when the slot
// is unused, so consumers should treat a nil pointer as "no data".
type OpenAIRateLimitWindow struct {
	UsedPercent        float64 `json:"used_percent"`
	LimitWindowSeconds int64   `json:"limit_window_seconds"`
	ResetAfterSeconds  int64   `json:"reset_after_seconds"`
	ResetAt            int64   `json:"reset_at"`

	usedPercentPresent bool
	limitWindowPresent bool
	resetAfterPresent  bool
	resetAtPresent     bool
}

// UnmarshalJSON 保存字段是否真实出现，避免把缺字段产生的零值与合法的
// used_percent=0 混为一谈。严格 WHAM 映射只接受结构完整的窗口。
func (w *OpenAIRateLimitWindow) UnmarshalJSON(data []byte) error {
	var raw struct {
		UsedPercent        *float64 `json:"used_percent"`
		LimitWindowSeconds *int64   `json:"limit_window_seconds"`
		ResetAfterSeconds  *int64   `json:"reset_after_seconds"`
		ResetAt            *int64   `json:"reset_at"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	*w = OpenAIRateLimitWindow{}
	if raw.UsedPercent != nil {
		w.UsedPercent = *raw.UsedPercent
		w.usedPercentPresent = true
	}
	if raw.LimitWindowSeconds != nil {
		w.LimitWindowSeconds = *raw.LimitWindowSeconds
		w.limitWindowPresent = true
	}
	if raw.ResetAfterSeconds != nil {
		w.ResetAfterSeconds = *raw.ResetAfterSeconds
		w.resetAfterPresent = true
	}
	if raw.ResetAt != nil {
		w.ResetAt = *raw.ResetAt
		w.resetAtPresent = true
	}
	return nil
}

// OpenAIRateLimit is a rate-limit envelope (primary + optional secondary window).
type OpenAIRateLimit struct {
	Allowed         bool                   `json:"allowed"`
	LimitReached    bool                   `json:"limit_reached"`
	PrimaryWindow   *OpenAIRateLimitWindow `json:"primary_window,omitempty"`
	SecondaryWindow *OpenAIRateLimitWindow `json:"secondary_window,omitempty"`
}

// OpenAIAdditionalRateLimit describes a per-feature rate limit (e.g. Codex Spark).
type OpenAIAdditionalRateLimit struct {
	LimitName      string           `json:"limit_name"`
	MeteredFeature string           `json:"metered_feature"`
	RateLimit      *OpenAIRateLimit `json:"rate_limit,omitempty"`
}

// OpenAIRateLimitResetCreditDetail is the sanitized metadata surfaced for one
// available reset credit. Do not add upstream ids or tokens here.
type OpenAIRateLimitResetCreditDetail struct {
	ExpiresAt string `json:"expires_at,omitempty"`
}

// OpenAIRateLimitResetCredits captures the "available_count" surfaced for the
// rate_limit_reset_credit grant type, which the reset action consumes.
type OpenAIRateLimitResetCredits struct {
	AvailableCount int                                `json:"available_count"`
	Credits        []OpenAIRateLimitResetCreditDetail `json:"credits,omitempty"`
}

// OpenAIQuotaUsage is the typed projection of /wham/usage we expose to the UI.
// Fields not relevant to the quota card are intentionally omitted to keep the
// surface narrow; full upstream payload preservation is unnecessary.
type OpenAIQuotaUsage struct {
	UserID                string                       `json:"user_id,omitempty"`
	AccountID             string                       `json:"account_id,omitempty"`
	Email                 string                       `json:"email,omitempty"`
	PlanType              string                       `json:"plan_type,omitempty"`
	RateLimit             *OpenAIRateLimit             `json:"rate_limit,omitempty"`
	AdditionalRateLimits  []OpenAIAdditionalRateLimit  `json:"additional_rate_limits,omitempty"`
	RateLimitResetCredits *OpenAIRateLimitResetCredits `json:"rate_limit_reset_credits,omitempty"`
	FetchedAt             int64                        `json:"fetched_at"`
}

// OpenAIQuotaResetCredit captures the redeemed credit metadata returned by the
// reset endpoint.
type OpenAIQuotaResetCredit struct {
	ID              string `json:"id,omitempty"`
	ResetType       string `json:"reset_type,omitempty"`
	Status          string `json:"status,omitempty"`
	GrantedAt       string `json:"granted_at,omitempty"`
	ExpiresAt       string `json:"expires_at,omitempty"`
	RedeemStartedAt string `json:"redeem_started_at,omitempty"`
	RedeemedAt      string `json:"redeemed_at,omitempty"`
}

// OpenAIQuotaResetResult is the typed projection of /wham/rate-limit-reset-credits/consume.
// The inner Credit also carries `redeemed_at` (RFC3339 string); we deliberately do
// NOT add a top-level redeemed_at to avoid ambiguity with the nested field.
type OpenAIQuotaResetResult struct {
	Code         string                  `json:"code"`
	Credit       *OpenAIQuotaResetCredit `json:"credit,omitempty"`
	WindowsReset int                     `json:"windows_reset"`
}

// OpenAIQuotaService 查询并消费 OpenAI OAuth 账号的 Codex 限流重置额度。
// wham 属于官方 backend-client 画像，必须经统一 HTTPUpstream 出站，不能使用会自动
// 添加浏览器头的 req/v3 隐私客户端。
type OpenAIQuotaService struct {
	accountRepo         AccountRepository
	proxyRepo           ProxyRepository
	tokenProvider       *OpenAITokenProvider
	httpUpstream        HTTPUpstream
	officialEgress      *OfficialEgressTransitionRuntime
	agentIdentityTaskMu sync.Mutex
	agentIdentityWS     agentIdentityWSConnectionInvalidator
}

// NewOpenAIQuotaService 创建配额服务。tokenProvider 负责复用网关既有刷新与加锁逻辑；
// httpUpstream 负责复用官方画像的 TLS、H1 线序和 backend-client 长连接。
func NewOpenAIQuotaService(
	accountRepo AccountRepository,
	proxyRepo ProxyRepository,
	tokenProvider *OpenAITokenProvider,
	httpUpstream HTTPUpstream,
) *OpenAIQuotaService {
	return &OpenAIQuotaService{
		accountRepo:   accountRepo,
		proxyRepo:     proxyRepo,
		tokenProvider: tokenProvider,
		httpUpstream:  httpUpstream,
	}
}

// QueryUsage 获取 OpenAI OAuth 账号最新的限流和额度快照，并按管理端显式操作补查
// reset-credit details。后台周期刷新不得调用本方法，因为它会产生第二次官方请求。
func (s *OpenAIQuotaService) QueryUsage(ctx context.Context, accountID int64) (*OpenAIQuotaUsage, error) {
	return s.queryUsage(ctx, accountID, true)
}

// QueryUsageOnly 只执行 GET /wham/usage。它是 1B 周期刷新的专用入口，成功时
// 严格保证只产生一次官方请求，不会隐式查询 reset-credit details。
func (s *OpenAIQuotaService) QueryUsageOnly(ctx context.Context, accountID int64) (*OpenAIQuotaUsage, error) {
	return s.queryUsage(ctx, accountID, false)
}

func (s *OpenAIQuotaService) queryUsage(
	ctx context.Context,
	accountID int64,
	includeResetCreditDetails bool,
) (*OpenAIQuotaUsage, error) {
	accessToken, chatGPTAccountID, proxyURL, _, err := s.prepareUpstreamCall(ctx, accountID)
	if err != nil {
		return nil, err
	}
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return nil, err
	}
	mode := string(runtimeState.CodexReleaseMode)

	callCtx, cancel := context.WithTimeout(ctx, openaiQuotaUpstreamTimeout)
	defer cancel()
	callCtx = context.WithValue(
		callCtx,
		officialCodexBundleHolderContextKey{},
		&officialCodexBundleHolder{httpAttemptBudget: 3},
	)
	agentIdentity := s.isAgentIdentityAccount(ctx, accountID)

	var payload OpenAIQuotaUsage
	settingsQueried := false
	// 该端点的有无由**当前发布指针解析到的画像**决定，不按 active/previous 槽位判断：
	// 槽位只表达「哪一份发布在生效」，与版本没有固定对应——候选期把目标画像装进
	// previous，生产启用后 Active 才是目标版本，用槽位当版本开关两个阶段必错一个。
	// 画像里没有该端点时，settingsSupported 为 false，本次调用维持原有两条 GET。
	_, settingsProfileErr := resolveCodexEndpointForMode(
		mode, codexEndpointID(officialCodexEndpointWhamSettingsUser),
	)
	settingsSupported := settingsProfileErr == nil
	for recovered := false; ; {
		quotaHeaders, expectedTaskID, headerErr := s.buildCodexQuotaHeaders(
			callCtx, mode, accountID, accessToken, chatGPTAccountID,
		)
		if headerErr != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_AUTH_FAILED", "failed to build upstream authentication: %v", headerErr)
		}
		if includeResetCreditDetails && settingsSupported && !settingsQueried {
			// 官方 WHAM 客户端在配额链路前先读一次用户设置（0.147 受控出站面实测
			// 34 次）。设置响应只作为客户端行为收据，不参与配额投影，因此失败只告警、
			// 不影响配额查询本身。
			settingsQueried = true
			settingsStatus, _, settingsErr := s.doCodexQuotaRequest(
				callCtx,
				accountID,
				proxyURL,
				officialCodexEndpointWhamSettingsUser,
				quotaHeaders,
				nil,
			)
			if settingsErr != nil || settingsStatus < http.StatusOK || settingsStatus >= http.StatusMultipleChoices {
				slog.Warn("openai_quota_settings_user_failed", "account_id", accountID, "status", settingsStatus, "error", settingsErr)
			}
		}
		status, responseBody, err := s.doCodexQuotaRequest(
			callCtx,
			accountID,
			proxyURL,
			officialCodexEndpointWhamUsage,
			quotaHeaders,
			nil,
		)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_REQUEST_FAILED", "upstream request failed: %v", err)
		}
		if status < http.StatusOK || status >= http.StatusMultipleChoices {
			if agentIdentity && !recovered && isAgentIdentityTaskInvalidHTTPResponse(status, responseBody) {
				recovered = true
				if err := s.recoverAgentIdentityTask(ctx, accountID, expectedTaskID); err != nil {
					return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_AUTH_FAILED", "agent identity task recovery failed: %v", err)
				}
				continue
			}
			body := truncate(s.redactQuotaErrorBody(ctx, accountID, string(responseBody)), 240)
			slog.Warn("openai_quota_query_failed", "account_id", accountID, "status", status, "body", body)
			return nil, infraerrors.Newf(
				mapUpstreamStatus(status),
				"OPENAI_QUOTA_UPSTREAM_ERROR",
				"upstream returned %d: %s",
				status,
				body,
			).WithMetadata(map[string]string{"upstream_status": strconv.Itoa(status)})
		}
		if err := json.Unmarshal(responseBody, &payload); err != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_RESPONSE_INVALID", "failed to decode upstream response: %v", err)
		}
		break
	}

	payload.FetchedAt = time.Now().Unix()
	if !includeResetCreditDetails {
		return &payload, nil
	}
	details := s.queryResetCreditDetails(callCtx, accessToken, chatGPTAccountID, proxyURL, accountID)
	if details != nil {
		hasDetailCount := details.AvailableCount != nil
		if payload.RateLimitResetCredits == nil {
			payload.RateLimitResetCredits = &OpenAIRateLimitResetCredits{}
		}
		if details.CreditListPresent {
			payload.RateLimitResetCredits.Credits = details.Credits
		}
		switch {
		case hasDetailCount:
			payload.RateLimitResetCredits.AvailableCount = *details.AvailableCount
		case details.CreditListPresent:
			payload.RateLimitResetCredits.AvailableCount = details.AvailableCreditCount
		}
	}
	return &payload, nil
}

// CacheResetCreditsSnapshot 在管理端显式刷新后保存完整的重置额度快照。
// 快照写入被查询的账号行；对于 Spark 影子账号，这仍是影子行，因为缓存只服务于
// 该行的展示，并不会赋予影子账号消费额度的能力。
//
// 若上游仅返回可用数量却缺少过期明细，则保留旧缓存。否则读取端无法淘汰过期额度，
// 可能持续展示已经失效的数据。调用方应把此错误视为“上游读取成功、缓存刷新失败”。
func (s *OpenAIQuotaService) CacheResetCreditsSnapshot(ctx context.Context, accountID int64, credits *OpenAIRateLimitResetCredits) error {
	if credits == nil || (credits.AvailableCount > 0 && len(credits.Credits) == 0) {
		return infraerrors.New(
			http.StatusBadGateway,
			"OPENAI_QUOTA_RESET_CREDITS_REFRESH_FAILED",
			"failed to refresh reset-credit expiration details; cached data was preserved",
		)
	}
	if err := s.accountRepo.UpdateExtra(ctx, accountID, map[string]any{
		openaiQuotaResetCreditsKey: credits,
	}); err != nil {
		return infraerrors.New(
			http.StatusInternalServerError,
			"OPENAI_QUOTA_CACHE_WRITE_FAILED",
			"failed to cache reset-credit details",
		).WithCause(err)
	}
	return nil
}

func (s *OpenAIQuotaService) queryResetCreditDetails(ctx context.Context, accessToken, chatGPTAccountID, proxyURL string, accountID int64) *openAIRateLimitResetCreditDetails {
	runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if runtimeErr != nil {
		slog.Warn("openai_quota_reset_credit_details_runtime_failed", "account_id", accountID, "error", runtimeErr)
		return nil
	}
	quotaHeaders, _, headerErr := s.buildCodexQuotaHeaders(
		ctx, string(runtimeState.CodexReleaseMode), accountID, accessToken, chatGPTAccountID,
	)
	if headerErr != nil {
		slog.Warn("openai_quota_reset_credit_details_auth_failed", "account_id", accountID, "error", headerErr)
		return nil
	}
	status, responseBody, err := s.doCodexQuotaRequest(
		ctx,
		accountID,
		proxyURL,
		officialCodexEndpointWhamResetCredits,
		quotaHeaders,
		nil,
	)
	if err != nil {
		slog.Warn("openai_quota_reset_credit_details_failed", "account_id", accountID, "error", err)
		return nil
	}
	if status < http.StatusOK || status >= http.StatusMultipleChoices {
		slog.Warn("openai_quota_reset_credit_details_failed", "account_id", accountID, "status", status)
		return nil
	}

	details, err := parseOpenAIRateLimitResetCreditDetails(responseBody)
	if err != nil {
		slog.Warn("openai_quota_reset_credit_details_parse_failed", "account_id", accountID, "error", err)
		if details.AvailableCount == nil {
			return nil
		}
	}
	if details.AvailableCount == nil && !details.CreditListPresent {
		return nil
	}
	return &details
}

// ResetCredit 为指定 OpenAI 账号消费一个 rate_limit_reset_credit。
// redeem_request_id 由服务端生成，用作上游幂等键。
func (s *OpenAIQuotaService) ResetCredit(ctx context.Context, accountID int64) (*OpenAIQuotaResetResult, error) {
	// Shadow guard: resetting credits via a shadow account would silently
	// operate on the parent's quota; that is surprising and unwanted. Callers
	// must reset the parent account directly.
	//
	// Fail-closed: if the account cannot be loaded (transient DB error), we
	// must NOT fall through to prepareUpstreamCall. That function resolves a
	// shadow to its parent and would perform a parent-level reset — exactly
	// what this guard must prevent. Return the load error instead.
	if s.accountRepo != nil {
		acc, loadErr := s.accountRepo.GetByID(ctx, accountID)
		if loadErr != nil {
			return nil, infraerrors.Newf(http.StatusNotFound, "OPENAI_QUOTA_ACCOUNT_NOT_FOUND", "account not found: %v", loadErr)
		}
		if acc.IsShadow() {
			return nil, ErrSparkShadowResetNotSupported
		}
	}

	accessToken, chatGPTAccountID, proxyURL, _, err := s.prepareUpstreamCall(ctx, accountID)
	if err != nil {
		return nil, err
	}
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return nil, err
	}
	mode := string(runtimeState.CodexReleaseMode)

	redeemRequestID, err := generateRedeemRequestID()
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_QUOTA_REDEEM_ID_FAILED", "failed to generate redeem id: %v", err)
	}

	callCtx, cancel := context.WithTimeout(ctx, openaiQuotaUpstreamTimeout)
	defer cancel()
	callCtx = context.WithValue(
		callCtx,
		officialCodexBundleHolderContextKey{},
		&officialCodexBundleHolder{httpAttemptBudget: 3},
	)
	agentIdentity := s.isAgentIdentityAccount(ctx, accountID)
	body, err := json.Marshal(struct {
		RedeemRequestID string `json:"redeem_request_id"`
	}{RedeemRequestID: redeemRequestID})
	if err != nil {
		return nil, infraerrors.Newf(http.StatusInternalServerError, "OPENAI_QUOTA_REQUEST_INVALID", "failed to encode reset request: %v", err)
	}

	var payload OpenAIQuotaResetResult
	for recovered := false; ; {
		headers, expectedTaskID, headerErr := s.buildCodexQuotaHeaders(
			callCtx, mode, accountID, accessToken, chatGPTAccountID,
		)
		if headerErr != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_AUTH_FAILED", "failed to build upstream authentication: %v", headerErr)
		}
		status, responseBody, err := s.doCodexQuotaRequest(
			callCtx,
			accountID,
			proxyURL,
			officialCodexEndpointWhamConsumeResetCredit,
			headers,
			body,
		)
		if err != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_RESET_REQUEST_FAILED", "upstream request failed: %v", err)
		}
		if status < http.StatusOK || status >= http.StatusMultipleChoices {
			if agentIdentity && !recovered && isAgentIdentityTaskInvalidHTTPResponse(status, responseBody) {
				recovered = true
				if err := s.recoverAgentIdentityTask(ctx, accountID, expectedTaskID); err != nil {
					return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_AUTH_FAILED", "agent identity task recovery failed: %v", err)
				}
				continue
			}
			redactedBody := truncate(s.redactQuotaErrorBody(callCtx, accountID, string(responseBody)), 240)
			slog.Warn("openai_quota_reset_failed", "account_id", accountID, "status", status, "body", redactedBody)
			return nil, infraerrors.Newf(mapUpstreamStatus(status), "OPENAI_QUOTA_RESET_UPSTREAM_ERROR", "upstream returned %d: %s", status, redactedBody)
		}
		if err := json.Unmarshal(responseBody, &payload); err != nil {
			return nil, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_RESET_RESPONSE_INVALID", "failed to decode upstream response: %v", err)
		}
		break
	}

	slog.Info("openai_quota_reset_success",
		"account_id", accountID,
		"code", payload.Code,
		"windows_reset", payload.WindowsReset,
	)
	return &payload, nil
}

// doCodexQuotaRequest 只负责把运行态凭据和 body 交给当前 Release 的通用执行器。
// URL、header 闭集、body 闭集、H1 线序、TLS 及 backend-client 生命周期均由
// 不可变端点画像决定；本函数不得再复制一套 WHAM 契约。
func (s *OpenAIQuotaService) doCodexQuotaRequest(
	ctx context.Context,
	accountID int64,
	proxyURL string,
	endpointID codexEndpointID,
	headers http.Header,
	body []byte,
) (int, []byte, error) {
	if s == nil || s.httpUpstream == nil {
		return 0, nil, fmt.Errorf("openai quota HTTP upstream is unavailable")
	}
	account, err := s.resolveCodexQuotaCredentialAccount(ctx, accountID)
	if err != nil {
		return 0, nil, err
	}
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return 0, nil, err
	}
	mode := string(runtimeState.CodexReleaseMode)
	endpoint, err := resolveCodexEndpointForMode(mode, endpointID)
	if err != nil {
		return 0, nil, err
	}
	target, err := buildCodexEndpointURLForMode(
		mode,
		endpointID,
		officialCodexEndpointURLInput{},
	)
	if err != nil {
		return 0, nil, err
	}
	if len(body) == 0 && endpoint.ContentType != "" {
		return 0, nil, fmt.Errorf("Codex %s 端点缺少请求体", endpoint.ID)
	}
	if len(body) != 0 && endpoint.ContentType == "" {
		return 0, nil, fmt.Errorf("Codex %s 端点不允许请求体", endpoint.ID)
	}

	requestContext, err := bindOfficialEgressSink(ctx, officialEgressSinkQuotaWHAM)
	if err != nil {
		return 0, nil, fmt.Errorf("绑定 Codex 配额 SinkID：%w", err)
	}
	requestContext = WithHTTPUpstreamRedirectsDisabled(
		WithHTTPUpstreamProfile(requestContext, HTTPUpstreamProfileOpenAI),
	)
	var bodyReader io.Reader
	if len(body) != 0 {
		bodyReader = bytes.NewReader(body)
	}
	request, err := http.NewRequestWithContext(requestContext, endpoint.Method, target.String(), bodyReader)
	if err != nil {
		return 0, nil, err
	}
	request.Host = endpoint.Host
	request.Header = cloneHeader(headers)
	if request.Header == nil {
		request.Header = make(http.Header)
	}
	holder, _ := ctx.Value(officialCodexBundleHolderContextKey{}).(*officialCodexBundleHolder)
	if holder == nil {
		holder = &officialCodexBundleHolder{httpAttemptBudget: 1}
	}
	holder.mu.Lock()
	defer holder.mu.Unlock()
	if holder.httpInvocation == nil {
		holder.httpInvocation, err = newOfficialCodexHTTPInvocation(
			request.Context(),
			officialCodexHTTPInvocationInput{
				Runtime: runtimeState, Account: account,
				SinkID: officialEgressSinkQuotaWHAM, ProxyURL: proxyURL,
				PolicyID: "changeset3.quota.wham", PolicySource: "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md#policy-changeset-3",
				BehaviorKind:  officialegress.BehaviorQuotaQuery,
				AttemptBudget: holder.httpAttemptBudget,
			},
		)
		if err != nil {
			return 0, nil, err
		}
	}
	response, err := holder.httpInvocation.Execute(
		request.Context(),
		officialCodexHTTPAttemptInput{EndpointID: string(endpointID), Request: request},
	)
	if err != nil {
		return 0, nil, err
	}
	if response == nil || response.Body == nil {
		return 0, nil, fmt.Errorf("Codex %s 上游返回空响应", endpoint.ID)
	}
	defer func() { _ = response.Body.Close() }()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, openaiQuotaUpstreamBodyLimit+1))
	if err != nil {
		return 0, nil, err
	}
	if len(responseBody) > openaiQuotaUpstreamBodyLimit {
		return 0, nil, fmt.Errorf("Codex %s 响应体过大", endpoint.ID)
	}
	return response.StatusCode, responseBody, nil
}

func (s *OpenAIQuotaService) resolveCodexQuotaCredentialAccount(
	ctx context.Context,
	accountID int64,
) (*Account, error) {
	if s == nil || s.accountRepo == nil {
		return nil, fmt.Errorf("openai quota account repository is unavailable")
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		return nil, fmt.Errorf("openai quota account is unavailable")
	}
	if account.IsShadow() {
		account, err = resolveCredentialAccount(ctx, s.accountRepo, account)
		if err != nil || account == nil {
			return nil, fmt.Errorf("openai quota credential account is unavailable")
		}
	}
	if !account.IsOpenAIOAuth() {
		return nil, fmt.Errorf("openai quota credential account is not OAuth")
	}
	return account, nil
}

// prepareUpstreamCall loads the account, validates it, obtains a fresh access
// token via the shared TokenProvider, and resolves the chatgpt-account-id and
// proxy URL. Centralized so QueryUsage / ResetCredit share validation.
func (s *OpenAIQuotaService) prepareUpstreamCall(ctx context.Context, accountID int64) (accessToken, chatGPTAccountID, proxyURL string, fedRAMP bool, err error) {
	if s == nil || s.accountRepo == nil || s.httpUpstream == nil {
		return "", "", "", false, infraerrors.New(http.StatusInternalServerError, "OPENAI_QUOTA_NOT_CONFIGURED", "openai quota service is not configured")
	}

	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil {
		return "", "", "", false, infraerrors.Newf(http.StatusNotFound, "OPENAI_QUOTA_ACCOUNT_NOT_FOUND", "account not found: %v", err)
	}
	if account == nil {
		return "", "", "", false, infraerrors.New(http.StatusNotFound, "OPENAI_QUOTA_ACCOUNT_NOT_FOUND", "account not found")
	}
	if account.Platform != PlatformOpenAI {
		return "", "", "", false, infraerrors.New(http.StatusBadRequest, "OPENAI_QUOTA_INVALID_PLATFORM", "account is not an OpenAI account")
	}
	if account.Type != AccountTypeOAuth {
		return "", "", "", false, infraerrors.New(http.StatusBadRequest, "OPENAI_QUOTA_INVALID_TYPE", "account is not an OAuth account")
	}

	// Spark shadow accounts do not hold their own credentials; resolve to the
	// parent account so that chatgpt_account_id / access_token / proxy all come
	// from the parent. This must happen BEFORE the chatgpt_account_id check.
	if account.IsShadow() {
		resolved, rerr := resolveCredentialAccount(ctx, s.accountRepo, account)
		if rerr != nil {
			return "", "", "", false, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_SHADOW_RESOLVE_FAILED", "failed to resolve shadow account: %v", rerr)
		}
		account = resolved
	}

	chatGPTAccountID = strings.TrimSpace(account.GetCredential("chatgpt_account_id"))
	if chatGPTAccountID == "" {
		// Fall back to organization_id — some legacy accounts only persisted poid.
		chatGPTAccountID = strings.TrimSpace(account.GetCredential("organization_id"))
	}
	if chatGPTAccountID == "" {
		return "", "", "", false, infraerrors.New(http.StatusBadRequest, "OPENAI_QUOTA_MISSING_ACCOUNT_ID", "chatgpt_account_id is missing; please re-authorize this account")
	}

	if !account.IsOpenAIAgentIdentity() {
		if s.tokenProvider == nil {
			return "", "", "", false, infraerrors.New(http.StatusInternalServerError, "OPENAI_QUOTA_NOT_CONFIGURED", "openai quota token provider is not configured")
		}
		accessToken, err = s.tokenProvider.GetAccessToken(ctx, account)
		if err != nil {
			return "", "", "", false, infraerrors.Newf(http.StatusBadGateway, "OPENAI_QUOTA_TOKEN_UNAVAILABLE", "failed to acquire access token: %v", err)
		}
		if strings.TrimSpace(accessToken) == "" {
			return "", "", "", false, infraerrors.New(http.StatusBadGateway, "OPENAI_QUOTA_TOKEN_UNAVAILABLE", "access token is empty")
		}
	}
	fedRAMP = account.IsChatGPTAccountFedRAMP()

	// account.Proxy is eager-loaded by accountRepo.GetByID (see
	// repository.accountsToService), so we can read the proxy URL directly
	// instead of round-tripping the DB again. Fall back to proxyRepo only
	// when Proxy isn't pre-populated (defensive — e.g. callers that built
	// the Account by hand).
	if account.ProxyID != nil {
		switch {
		case account.Proxy != nil:
			proxyURL = account.Proxy.URL()
		case s.proxyRepo != nil:
			if proxy, perr := s.proxyRepo.GetByID(ctx, *account.ProxyID); perr == nil && proxy != nil {
				proxyURL = proxy.URL()
			}
		}
	}

	return accessToken, chatGPTAccountID, proxyURL, fedRAMP, nil
}

func (s *OpenAIQuotaService) recoverAgentIdentityTask(ctx context.Context, accountID int64, expectedTaskID string) error {
	if s == nil || s.accountRepo == nil {
		return fmt.Errorf("account repository is unavailable")
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		return fmt.Errorf("account is unavailable")
	}
	if account.IsShadow() {
		account, err = resolveCredentialAccount(ctx, s.accountRepo, account)
		if err != nil || account == nil {
			return fmt.Errorf("credential account is unavailable")
		}
	}
	if !account.IsOpenAIAgentIdentity() {
		return nil
	}
	return ensureAgentIdentityTaskForAccount(ctx, s.accountRepo, s.agentIdentityWS, &s.agentIdentityTaskMu, account, expectedTaskID)
}

func (s *OpenAIQuotaService) isAgentIdentityAccount(ctx context.Context, accountID int64) bool {
	if s == nil || s.accountRepo == nil {
		return false
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		return false
	}
	if account.IsShadow() {
		account, err = resolveCredentialAccount(ctx, s.accountRepo, account)
		if err != nil || account == nil {
			return false
		}
	}
	return account.IsOpenAIAgentIdentity()
}

func (s *OpenAIQuotaService) buildCodexQuotaHeaders(
	ctx context.Context,
	mode string,
	accountID int64,
	accessToken string,
	chatGPTAccountID string,
) (http.Header, string, error) {
	headers, err := buildCodexCommonHeaders(ctx, mode, accessToken, chatGPTAccountID)
	if err != nil {
		return nil, "", err
	}
	if s == nil || s.accountRepo == nil {
		return headers, "", nil
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		if strings.TrimSpace(accessToken) == "" {
			return nil, "", fmt.Errorf("agent identity account credentials are unavailable")
		}
		return headers, "", nil
	}
	if account.IsShadow() {
		if resolved, resolveErr := resolveCredentialAccount(ctx, s.accountRepo, account); resolveErr == nil && resolved != nil {
			account = resolved
		} else if strings.TrimSpace(accessToken) == "" {
			return nil, "", fmt.Errorf("agent identity shadow credentials are unavailable")
		}
	}
	if account.IsChatGPTAccountFedRAMP() {
		headers.Set("x-openai-fedramp", "true")
	}
	if !account.IsOpenAIAgentIdentity() {
		return headers, "", nil
	}
	if err := ensureAgentIdentityTaskForAccount(ctx, s.accountRepo, s.agentIdentityWS, &s.agentIdentityTaskMu, account, ""); err != nil {
		return nil, "", err
	}
	key, err := agentIdentityKeyFromAccount(account)
	if err != nil {
		return nil, "", err
	}
	assertion, err := buildAgentAssertion(key, time.Now())
	if err != nil {
		return nil, "", err
	}
	headers.Set("authorization", assertion)
	return headers, key.taskID, nil
}

func (s *OpenAIQuotaService) redactQuotaErrorBody(ctx context.Context, accountID int64, body string) string {
	if s == nil || s.accountRepo == nil {
		return body
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		return body
	}
	return string(redactAgentIdentitySensitiveBodyForAccount(ctx, s.accountRepo, account, []byte(body)))
}

// buildCodexCommonHeaders 只生成 backend-client 画像所需的运行态基础头。
// accept 及其他常量由端点 Finalizer 注入；FedRAMP 则在凭据账号解析后按条件加入。
func buildCodexCommonHeaders(
	ctx context.Context,
	mode string,
	accessToken string,
	chatGPTAccountID string,
) (http.Header, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return nil, err
	}
	state, bound, err := officialCodexRuntimeStateFromContext(ctx)
	if err != nil {
		return nil, err
	}
	if !bound {
		state = defaultOfficialCodexRuntimeState()
		state.ProfileMode = normalizeOfficialClientProfileMode(mode)
	}
	userAgent, err := profile.RenderUserAgentWithTerminal(state.SurfaceID, state.TerminalToken, state.UserAgentSuffixEnabled)
	if err != nil {
		return nil, err
	}
	headers := make(http.Header, 3)
	headers.Set("user-agent", userAgent)
	headers.Set("authorization", "Bearer "+accessToken)
	headers.Set("chatgpt-account-id", chatGPTAccountID)
	return headers, nil
}

// generateRedeemRequestID produces a UUID-v4-shaped string without pulling in a
// new dependency. ChatGPT uses this as an idempotency key for the consume call.
func generateRedeemRequestID() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	// Set version (4) and variant (RFC 4122) bits.
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	hexStr := hex.EncodeToString(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s", hexStr[0:8], hexStr[8:12], hexStr[12:16], hexStr[16:20], hexStr[20:]), nil
}

// buildCodexSparkWindowExtraUpdates extracts Codex Spark usage windows from the
// /wham/usage response body's additional_rate_limits, matching the entry with
// MeteredFeature == "codex_bengalfox". It produces plain codex_* keys (NOT the
// Method-Z "codex_spark_" prefix) so that a spark shadow account's extra map
// is populated with the same key names used by the scheduling / frontend layers.
// Returns nil when no codex_bengalfox entry is present or when the RateLimit
// yields no window data.
func buildCodexSparkWindowExtraUpdates(usage *OpenAIQuotaUsage, now time.Time) map[string]any {
	if usage == nil {
		return nil
	}
	var spark *OpenAIRateLimit
	for i := range usage.AdditionalRateLimits {
		a := usage.AdditionalRateLimits[i]
		if a.MeteredFeature == "codex_bengalfox" {
			spark = a.RateLimit
			break
		}
	}
	if spark == nil {
		return nil
	}
	updates, err := buildStrictCodexWHAMWindowExtraUpdates(spark, now)
	if err != nil {
		return nil
	}
	return updates
}

// buildCodexGlobalWindowExtraUpdates 从普通 OAuth 的顶层 rate_limit 读取全局窗口。
func buildCodexGlobalWindowExtraUpdates(usage *OpenAIQuotaUsage, now time.Time) (map[string]any, error) {
	if usage == nil || usage.RateLimit == nil {
		return nil, errors.New("wham_rate_limit_missing")
	}
	return buildStrictCodexWHAMWindowExtraUpdates(usage.RateLimit, now)
}

const (
	codexWHAMFiveHourSeconds = int64(5 * time.Hour / time.Second)
	codexWHAMSevenDaySeconds = int64(7 * 24 * time.Hour / time.Second)
)

type strictCodexWHAMWindow struct {
	usedPercent       float64
	resetAfterSeconds int
	windowMinutes     int
}

func buildStrictCodexWHAMWindowExtraUpdates(
	rateLimit *OpenAIRateLimit,
	now time.Time,
) (map[string]any, error) {
	if rateLimit == nil {
		return nil, errors.New("wham_rate_limit_missing")
	}
	windows := []*OpenAIRateLimitWindow{rateLimit.PrimaryWindow, rateLimit.SecondaryWindow}
	resolved := make(map[int64]strictCodexWHAMWindow, 2)
	for _, window := range windows {
		if window == nil {
			continue
		}
		parsed, err := strictCodexWHAMWindowFromWire(window, now)
		if err != nil {
			return nil, err
		}
		if _, duplicate := resolved[window.LimitWindowSeconds]; duplicate {
			return nil, fmt.Errorf("wham_window_duplicate_%d", window.LimitWindowSeconds)
		}
		resolved[window.LimitWindowSeconds] = parsed
	}
	if _, ok := resolved[codexWHAMFiveHourSeconds]; !ok {
		return nil, errors.New("wham_window_5h_missing")
	}
	if _, ok := resolved[codexWHAMSevenDaySeconds]; !ok {
		return nil, errors.New("wham_window_7d_missing")
	}

	updates := make(map[string]any, 9)
	for duration, window := range resolved {
		prefix := ""
		switch duration {
		case codexWHAMFiveHourSeconds:
			prefix = "codex_5h_"
		case codexWHAMSevenDaySeconds:
			prefix = "codex_7d_"
		default:
			// 上游解析已经拒绝未知时长；本分支防止未来改动把 30d 等
			// 窗口静默吞成 7d。
			return nil, fmt.Errorf("wham_window_unknown_%d", duration)
		}
		updates[prefix+"used_percent"] = window.usedPercent
		updates[prefix+"reset_after_seconds"] = window.resetAfterSeconds
		updates[prefix+"window_minutes"] = window.windowMinutes
		if resetAt := codexResetAtRFC3339(now, &window.resetAfterSeconds); resetAt != nil {
			updates[prefix+"reset_at"] = *resetAt
		}
	}
	updates["codex_usage_updated_at"] = now.UTC().Format(time.RFC3339)
	return updates, nil
}

func strictCodexWHAMWindowFromWire(
	window *OpenAIRateLimitWindow,
	now time.Time,
) (strictCodexWHAMWindow, error) {
	if window == nil {
		return strictCodexWHAMWindow{}, errors.New("wham_window_missing")
	}
	if !window.usedPercentPresent || !window.limitWindowPresent ||
		(!window.resetAfterPresent && !window.resetAtPresent) {
		return strictCodexWHAMWindow{}, errors.New("wham_window_incomplete")
	}
	if math.IsNaN(window.UsedPercent) || math.IsInf(window.UsedPercent, 0) ||
		window.UsedPercent < 0 || window.UsedPercent > 100 {
		return strictCodexWHAMWindow{}, errors.New("wham_window_used_percent_invalid")
	}
	if window.LimitWindowSeconds != codexWHAMFiveHourSeconds &&
		window.LimitWindowSeconds != codexWHAMSevenDaySeconds {
		return strictCodexWHAMWindow{}, fmt.Errorf("wham_window_unknown_%d", window.LimitWindowSeconds)
	}
	resetAfter := window.ResetAfterSeconds
	if !window.resetAfterPresent {
		if window.ResetAt <= 0 {
			return strictCodexWHAMWindow{}, errors.New("wham_window_reset_invalid")
		}
		resetAfter = window.ResetAt - now.Unix()
	}
	if resetAfter < 0 {
		return strictCodexWHAMWindow{}, errors.New("wham_window_reset_invalid")
	}
	return strictCodexWHAMWindow{
		usedPercent:       window.UsedPercent,
		resetAfterSeconds: int(resetAfter),
		windowMinutes:     int(window.LimitWindowSeconds / 60),
	}, nil
}

// mapUpstreamStatus collapses upstream HTTP statuses into a stable set we
// surface from the admin handler. 4xx upstream errors are surfaced as 502
// (BadGateway) so callers can distinguish "your input is bad" (400) from
// "upstream said no" (502); 401/403 are bubbled directly to hint at re-auth.
func mapUpstreamStatus(status int) int {
	switch {
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return status
	case status == http.StatusTooManyRequests:
		return http.StatusTooManyRequests
	case status >= 400 && status < 500:
		return http.StatusBadGateway
	case status >= 500:
		return http.StatusBadGateway
	default:
		return http.StatusBadGateway
	}
}
