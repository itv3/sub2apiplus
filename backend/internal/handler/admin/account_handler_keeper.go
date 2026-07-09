// Package admin provides keeper internal HTTP handlers.
package admin

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

type KeeperAccountConfigResponse struct {
	ID                int64      `json:"id"`
	Name              string     `json:"name"`
	Platform          string     `json:"platform"`
	Type              string     `json:"type"`
	Status            string     `json:"status"`
	Enabled           bool       `json:"enabled"`
	Executor          string     `json:"executor"`
	Model             string     `json:"model"`
	Mode              string     `json:"mode"`
	Workspace         string     `json:"workspace"`
	Prompt            string     `json:"prompt,omitempty"`
	MaxOutputTokens   int        `json:"max_output_tokens"`
	IntervalMinutes   int        `json:"interval_minutes"`
	WorkStart         string     `json:"work_start"`
	WorkEnd           string     `json:"work_end"`
	LastUsedAt        *time.Time `json:"last_used_at,omitempty"`
	LastKeepaliveAt   *time.Time `json:"last_keepalive_at,omitempty"`
	NextKeepaliveAt   *time.Time `json:"next_keepalive_at,omitempty"`
	Due               bool       `json:"due"`
	LastStatus        string     `json:"last_status,omitempty"`
	LastError         string     `json:"last_error,omitempty"`
	LastSummary       string     `json:"last_summary,omitempty"`
	BillingMultiplier float64    `json:"billing_multiplier"`
	ProxyToken        string     `json:"proxy_token,omitempty"`
}

type KeeperAccountsResponse struct {
	Accounts []KeeperAccountConfigResponse `json:"accounts"`
	Now      time.Time                     `json:"now"`
}

type KeeperProjectsResponse struct {
	Projects []string `json:"projects"`
}

type RecordKeeperKeepaliveRequest struct {
	Model                    string  `json:"model"`
	Prompt                   string  `json:"prompt"`
	MaxOutputTokens          int     `json:"max_output_tokens"`
	Status                   string  `json:"status"`
	SessionID                string  `json:"session_id"`
	Summary                  string  `json:"summary"`
	Error                    string  `json:"error"`
	InputTokens              int64   `json:"input_tokens"`
	OutputTokens             int64   `json:"output_tokens"`
	CachedInputTokens        int64   `json:"cached_input_tokens"`
	CacheCreationInputTokens int64   `json:"cache_creation_input_tokens"`
	TotalTokens              int64   `json:"total_tokens"`
	TotalCost                float64 `json:"total_cost"`
	LocalClientError         bool    `json:"local_client_error"`
}

const (
	keeperKeepaliveSummaryMaxRunes   = 500
	keeperKeepaliveErrorMaxRunes     = 16000
	keeperKeepaliveSessionIDMaxRunes = 255
	keeperKeepaliveModelMaxRunes     = 255
)

var keeperKeepaliveAllowedStatuses = map[string]struct{}{
	"success": {},
	"error":   {},
	"skipped": {},
	"running": {},
}

// GET /api/v1/internal/keeper/accounts
func (h *AccountHandler) ListKeeperAccounts(c *gin.Context) {
	ctx := c.Request.Context()
	now := time.Now().UTC()

	accounts, err := h.loadKeeperCandidateAccounts(ctx)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}

	out := make([]KeeperAccountConfigResponse, 0, len(accounts))
	includeProxyToken := c.GetBool(service.KeeperInternalAuthContextKey)
	tokenSecret := strings.TrimSpace(os.Getenv("SUB2APIPLUS_KEEPER_INTERNAL_TOKEN"))
	for _, account := range accounts {
		item := buildKeeperAccountConfig(account, now)
		if item.Enabled {
			if includeProxyToken {
				proxyToken, err := service.IssueKeeperProxyToken(tokenSecret, account.ID, account.Platform, now)
				if err != nil {
					response.Error(c, http.StatusServiceUnavailable, err.Error())
					return
				}
				item.ProxyToken = proxyToken
			}
			out = append(out, item)
		}
	}

	response.Success(c, KeeperAccountsResponse{Accounts: out, Now: now})
}

// GET /api/v1/internal/keeper/projects
func (h *AccountHandler) ListKeeperProjects(c *gin.Context) {
	raw := strings.TrimSpace(os.Getenv("SUB2APIPLUS_KEEPER_PROJECTS"))
	seen := map[string]struct{}{}
	projects := make([]string, 0)
	for _, part := range strings.Split(raw, ",") {
		project := strings.TrimSpace(part)
		if !isValidKeeperProjectName(project) {
			continue
		}
		if _, ok := seen[project]; ok {
			continue
		}
		seen[project] = struct{}{}
		projects = append(projects, project)
	}
	sort.Strings(projects)
	response.Success(c, KeeperProjectsResponse{Projects: projects})
}

func isValidKeeperProjectName(project string) bool {
	project = strings.TrimSpace(project)
	if project == "" {
		return false
	}
	if project == "." || project == ".." {
		return false
	}
	if strings.Contains(project, "..") {
		return false
	}
	if strings.HasPrefix(project, "/") || strings.HasPrefix(project, "\\") {
		return false
	}
	if strings.Contains(project, "/") || strings.Contains(project, "\\") {
		return false
	}
	return true
}

// GET /api/v1/internal/keeper/state
func (h *AccountHandler) GetKeeperState(c *gin.Context) {
	h.proxyKeeper(c, http.MethodGet, "/api/state", nil)
}

// GET|POST /api/v1/internal/keeper/settings
func (h *AccountHandler) ProxyKeeperSettings(c *gin.Context) {
	h.proxyKeeper(c, c.Request.Method, "/api/settings", c.Request.Body)
}

// POST /api/v1/internal/keeper/run?target=<target-name>
func (h *AccountHandler) RunKeeperTarget(c *gin.Context) {
	target := strings.TrimSpace(c.Query("target"))
	if target == "" {
		response.BadRequest(c, "target is required")
		return
	}
	h.proxyKeeper(c, http.MethodPost, "/api/run?target="+url.QueryEscape(target), nil)
}

// /api/v1/internal/keeper/openai/accounts/:id/*
func (h *AccountHandler) ProxyKeeperOpenAIAccount(c *gin.Context) {
	if h.accountTestService == nil {
		response.Error(c, http.StatusServiceUnavailable, "Account test service unavailable")
		return
	}
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}
	if c.Request.URL.RawQuery != "" {
		response.BadRequest(c, "keeper proxy does not accept query parameters")
		return
	}
	resp, err := h.accountTestService.ProxyKeeperOpenAIAccount(c.Request.Context(), accountID, service.KeeperOpenAIProxyRequest{
		Method: c.Request.Method,
		Path:   c.Param("proxy_path"),
		Header: c.Request.Header,
		Body:   c.Request.Body,
	})
	if err != nil {
		response.Error(c, http.StatusBadGateway, err.Error())
		return
	}
	defer func() { _ = resp.Body.Close() }()

	copyKeeperProxyResponseHeaders(c.Writer.Header(), resp.Header)
	c.Status(resp.StatusCode)
	_, _ = io.Copy(c.Writer, resp.Body)
}

// /api/v1/internal/keeper/anthropic/accounts/:id/*
func (h *AccountHandler) ProxyKeeperAnthropicAccount(c *gin.Context) {
	if h.accountTestService == nil {
		response.Error(c, http.StatusServiceUnavailable, "Account test service unavailable")
		return
	}
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}
	if c.Request.URL.RawQuery != "" {
		response.BadRequest(c, "keeper proxy does not accept query parameters")
		return
	}
	resp, err := h.accountTestService.ProxyKeeperAnthropicAccount(c.Request.Context(), accountID, service.KeeperOpenAIProxyRequest{
		Method: c.Request.Method,
		Path:   c.Param("proxy_path"),
		Header: c.Request.Header,
		Body:   c.Request.Body,
	})
	if err != nil {
		response.Error(c, http.StatusBadGateway, err.Error())
		return
	}
	defer func() { _ = resp.Body.Close() }()

	copyKeeperProxyResponseHeaders(c.Writer.Header(), resp.Header)
	c.Status(resp.StatusCode)
	_, _ = io.Copy(c.Writer, resp.Body)
}

func copyKeeperProxyResponseHeaders(dst http.Header, src http.Header) {
	for key, values := range src {
		if !isKeeperProxyResponseHeaderAllowed(key) {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func isKeeperProxyResponseHeaderAllowed(name string) bool {
	if isKeeperProxyHopByHopHeader(name) {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "content-type", "cache-control", "x-request-id", "request-id", "openai-processing-ms", "anthropic-request-id":
		return true
	default:
		return false
	}
}

func isKeeperProxyHopByHopHeader(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade":
		return true
	default:
		return false
	}
}

func (h *AccountHandler) proxyKeeper(c *gin.Context, method string, path string, body io.Reader) {
	baseURL := strings.TrimRight(strings.TrimSpace(os.Getenv("SUB2APIPLUS_KEEPER_BASE_URL")), "/")
	if baseURL == "" {
		baseURL = "http://sub2apiplus-keeper:38090"
	}
	token := strings.TrimSpace(os.Getenv("SUB2APIPLUS_KEEPER_INTERNAL_TOKEN"))
	if token == "" {
		response.Error(c, http.StatusServiceUnavailable, "SUB2APIPLUS_KEEPER_INTERNAL_TOKEN is not configured")
		return
	}
	req, err := http.NewRequestWithContext(c.Request.Context(), method, baseURL+path, body)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	req.Header.Set("x-api-key", token)
	if method == http.MethodPost {
		req.Header.Set("Content-Type", "application/json")
	}
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		response.Error(c, http.StatusBadGateway, err.Error())
		return
	}
	defer func() { _ = resp.Body.Close() }()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		response.Error(c, resp.StatusCode, strings.TrimSpace(string(raw)))
		return
	}
	var payload any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &payload); err != nil {
			response.Error(c, http.StatusBadGateway, "keeper returned invalid json")
			return
		}
	}
	response.Success(c, payload)
}

func (h *AccountHandler) loadKeeperCandidateAccounts(ctx context.Context) ([]service.Account, error) {
	const pageSize = 500
	var out []service.Account
	for _, platform := range []string{service.PlatformOpenAI, service.PlatformAnthropic} {
		page := 1
		for {
			accounts, total, err := h.adminService.ListAccounts(ctx, page, pageSize, platform, "", "", "", 0, "", "id", "desc")
			if err != nil {
				return nil, err
			}
			for _, account := range accounts {
				if isKeeperKeepaliveCandidate(account) {
					out = append(out, account)
				}
			}
			if int64(page*pageSize) >= total || len(accounts) == 0 {
				break
			}
			page++
		}
	}
	return out, nil
}

func isKeeperKeepaliveCandidate(account service.Account) bool {
	switch account.Platform {
	case service.PlatformOpenAI:
		return account.Type == service.AccountTypeAPIKey && strings.TrimSpace(account.GetOpenAIApiKey()) != ""
	case service.PlatformAnthropic:
		return account.Type == service.AccountTypeAPIKey && strings.TrimSpace(account.GetCredential("api_key")) != ""
	default:
		return false
	}
}

func buildKeeperAccountConfig(account service.Account, now time.Time) KeeperAccountConfigResponse {
	extra := account.Extra
	enabled := extraBool(extra, "keeper_keepalive_enabled")
	intervalMinutes := extraIntDefault(extra, "keeper_keepalive_interval_minutes", 8)
	if intervalMinutes < 1 {
		intervalMinutes = 1
	}
	executor := defaultKeeperExecutorForPlatform(account.Platform)

	lastKeepalive := extraTime(extra, "keeper_last_keepalive_at")
	base := latestTime(account.LastUsedAt, lastKeepalive)
	var next *time.Time
	due := enabled
	if base != nil {
		calculated := base.Add(time.Duration(intervalMinutes) * time.Minute)
		next = &calculated
		due = enabled && !calculated.After(now)
	}

	return KeeperAccountConfigResponse{
		ID:                account.ID,
		Name:              account.Name,
		Platform:          account.Platform,
		Type:              account.Type,
		Status:            account.Status,
		Enabled:           enabled,
		Executor:          executor,
		Model:             extraString(extra, "keeper_keepalive_model"),
		Mode:              normalizeKeeperModeDefault(extraString(extra, "keeper_keepalive_mode"), defaultKeeperModeForPlatform(account.Platform)),
		Workspace:         extraString(extra, "keeper_keepalive_workspace"),
		Prompt:            extraString(extra, "keeper_keepalive_prompt"),
		MaxOutputTokens:   service.KeeperMaxOutputTokens(&account),
		IntervalMinutes:   intervalMinutes,
		WorkStart:         extraStringDefault(extra, "keeper_keepalive_work_start", "04:00"),
		WorkEnd:           extraStringDefault(extra, "keeper_keepalive_work_end", "24:00"),
		LastUsedAt:        account.LastUsedAt,
		LastKeepaliveAt:   lastKeepalive,
		NextKeepaliveAt:   next,
		Due:               due,
		LastStatus:        extraString(extra, "keeper_last_status"),
		LastError:         extraString(extra, "keeper_last_error"),
		LastSummary:       extraString(extra, "keeper_last_summary"),
		BillingMultiplier: account.BillingRateMultiplier(),
	}
}

// POST /api/v1/internal/keeper/accounts/:id/keepalive
func (h *AccountHandler) RecordKeeperKeepalive(c *gin.Context) {
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}

	var req RecordKeeperKeepaliveRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	if strings.TrimSpace(req.Prompt) != "" {
		response.BadRequest(c, "keeper keepalive execution must run in sidecar")
		return
	}
	if err := normalizeKeeperKeepaliveRecordRequest(&req); err != nil {
		response.BadRequest(c, err.Error())
		return
	}

	billing, rateMultiplier := h.calculateKeeperKeepaliveBilling(c.Request.Context(), accountID, req)
	if billing != nil {
		req.TotalCost = billing.ActualCost
	}

	now := time.Now().UTC().Format(time.RFC3339)
	updates := map[string]any{
		"keeper_last_status":        req.Status,
		"keeper_last_session_id":    req.SessionID,
		"keeper_last_summary":       req.Summary,
		"keeper_last_error":         req.Error,
		"keeper_last_recorded_at":   now,
		"keeper_last_input_tokens":  req.InputTokens,
		"keeper_last_output_tokens": req.OutputTokens,
		"keeper_last_total_tokens":  req.TotalTokens,
		"keeper_last_total_cost":    req.TotalCost,
		"keeper_local_client_error": req.LocalClientError,
	}
	if req.Status == "success" || req.Status == "skipped" {
		updates["keeper_last_keepalive_at"] = now
	}
	if req.CachedInputTokens > 0 {
		updates["keeper_last_cached_input_tokens"] = req.CachedInputTokens
	}
	if req.CacheCreationInputTokens > 0 {
		updates["keeper_last_cache_creation_input_tokens"] = req.CacheCreationInputTokens
	}

	if err := h.adminService.UpdateAccountExtra(c.Request.Context(), accountID, updates); err != nil {
		response.ErrorFrom(c, err)
		return
	}

	response.Success(c, gin.H{"ok": true, "recorded_at": now, "billing": keeperBillingResponse(billing, rateMultiplier)})
}

func (h *AccountHandler) calculateKeeperKeepaliveBilling(ctx context.Context, accountID int64, req RecordKeeperKeepaliveRequest) (*service.CostBreakdown, float64) {
	if h.billingService == nil || strings.TrimSpace(req.Model) == "" {
		return nil, 0
	}
	account, err := h.adminService.GetAccount(ctx, accountID)
	if err != nil || account == nil {
		return nil, 0
	}
	rateMultiplier := account.BillingRateMultiplier()
	cost, err := h.billingService.CalculateCost(req.Model, service.UsageTokens{
		InputTokens:         int(req.InputTokens),
		OutputTokens:        int(req.OutputTokens),
		CacheCreationTokens: int(req.CacheCreationInputTokens),
		CacheReadTokens:     int(req.CachedInputTokens),
	}, rateMultiplier)
	if err != nil {
		return nil, rateMultiplier
	}
	return cost, rateMultiplier
}

func keeperBillingResponse(cost *service.CostBreakdown, rateMultiplier float64) any {
	if cost == nil {
		return nil
	}
	return gin.H{
		"available":           true,
		"input_cost":          cost.InputCost,
		"output_cost":         cost.OutputCost,
		"cache_creation_cost": cost.CacheCreationCost,
		"cache_read_cost":     cost.CacheReadCost,
		"total_cost":          cost.TotalCost,
		"actual_cost":         cost.ActualCost,
		"billing_mode":        cost.BillingMode,
		"pricing_source":      "sub2apiplus",
		"rate_multiplier":     rateMultiplier,
	}
}

func normalizeKeeperKeepaliveRecordRequest(req *RecordKeeperKeepaliveRequest) error {
	if req == nil {
		return nil
	}

	req.Model = truncateKeeperKeepaliveText(strings.TrimSpace(req.Model), keeperKeepaliveModelMaxRunes)
	req.Prompt = strings.TrimSpace(req.Prompt)
	req.Status = strings.ToLower(strings.TrimSpace(req.Status))
	req.SessionID = truncateKeeperKeepaliveText(strings.TrimSpace(req.SessionID), keeperKeepaliveSessionIDMaxRunes)
	req.Summary = truncateKeeperKeepaliveText(strings.TrimSpace(req.Summary), keeperKeepaliveSummaryMaxRunes)
	req.Error = truncateKeeperKeepaliveText(strings.TrimSpace(req.Error), keeperKeepaliveErrorMaxRunes)

	if req.Status == "" {
		return errors.New("status is required")
	}
	if _, ok := keeperKeepaliveAllowedStatuses[req.Status]; !ok {
		return errors.New("invalid status")
	}
	if req.InputTokens < 0 ||
		req.OutputTokens < 0 ||
		req.CachedInputTokens < 0 ||
		req.CacheCreationInputTokens < 0 ||
		req.TotalTokens < 0 {
		return errors.New("token usage must be non-negative")
	}
	if math.IsNaN(req.TotalCost) || math.IsInf(req.TotalCost, 0) || req.TotalCost < 0 {
		return errors.New("total_cost must be a non-negative finite number")
	}
	return nil
}

func truncateKeeperKeepaliveText(value string, maxRunes int) string {
	if maxRunes <= 0 || value == "" {
		return ""
	}
	runes := []rune(value)
	if len(runes) <= maxRunes {
		return value
	}
	return string(runes[:maxRunes])
}

func extraBool(extra map[string]any, key string) bool {
	if extra == nil {
		return false
	}
	v, _ := extra[key].(bool)
	return v
}

func extraString(extra map[string]any, key string) string {
	if extra == nil {
		return ""
	}
	switch v := extra[key].(type) {
	case string:
		return strings.TrimSpace(v)
	default:
		return ""
	}
}

func extraStringDefault(extra map[string]any, key, fallback string) string {
	if v := extraString(extra, key); v != "" {
		return v
	}
	return fallback
}

func normalizeKeeperMode(value string) string {
	return normalizeKeeperModeDefault(value, "resume_last")
}

func normalizeKeeperModeDefault(value string, fallback string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "fresh":
		return "fresh"
	default:
		if strings.TrimSpace(fallback) == "fresh" {
			return "fresh"
		}
		return "resume_last"
	}
}

func defaultKeeperModeForPlatform(platform string) string {
	if strings.EqualFold(strings.TrimSpace(platform), service.PlatformOpenAI) {
		return "fresh"
	}
	return "resume_last"
}

func defaultKeeperExecutorForPlatform(platform string) string {
	if strings.EqualFold(strings.TrimSpace(platform), service.PlatformAnthropic) {
		return "claude"
	}
	return "codex"
}

func extraIntDefault(extra map[string]any, key string, fallback int) int {
	if extra == nil {
		return fallback
	}
	switch v := extra[key].(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	case json.Number:
		n, err := v.Int64()
		if err == nil {
			return int(n)
		}
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(v))
		if err == nil {
			return n
		}
	}
	return fallback
}

func extraTime(extra map[string]any, key string) *time.Time {
	raw := extraString(extra, key)
	if raw == "" {
		return nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return nil
	}
	utc := t.UTC()
	return &utc
}

func latestTime(values ...*time.Time) *time.Time {
	var latest *time.Time
	for _, value := range values {
		if value == nil {
			continue
		}
		utc := value.UTC()
		if latest == nil || utc.After(*latest) {
			latest = &utc
		}
	}
	return latest
}
