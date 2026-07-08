package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

const (
	DefaultKeeperMaxOutputTokens = 512
	keeperProxyBodyLimitBytes    = 16 << 20
)

type KeeperOpenAIProxyRequest struct {
	Method string
	Path   string
	Header http.Header
	Body   io.Reader
}

func (s *AccountTestService) ProxyKeeperOpenAIAccount(ctx context.Context, accountID int64, in KeeperOpenAIProxyRequest) (*http.Response, error) {
	if s == nil || s.accountRepo == nil {
		return nil, errors.New("account test service unavailable")
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil {
		return nil, err
	}
	if account == nil || !account.IsOpenAI() {
		return nil, fmt.Errorf("account %d is not an OpenAI account", accountID)
	}
	if !account.IsSchedulable() {
		return nil, fmt.Errorf("account %d is not schedulable", accountID)
	}
	apiKey := strings.TrimSpace(account.GetOpenAIApiKey())
	if apiKey == "" {
		return nil, fmt.Errorf("account %d does not have OpenAI API key credentials", accountID)
	}
	baseURL, err := s.validateUpstreamBaseURL(account.GetOpenAIBaseURL())
	if err != nil {
		return nil, err
	}
	proxyPath, err := validateKeeperOpenAIProxyPath(in.Path)
	if err != nil {
		return nil, err
	}
	upstreamURL := buildKeeperOpenAIProxyURL(baseURL, proxyPath)
	body := in.Body
	if isKeeperProxyPathExact(proxyPath, "/v1/responses", "/responses") {
		body, err = clampKeeperProxyJSONMaxTokens(body, KeeperMaxOutputTokens(account), "max_output_tokens", "max_output_tokens")
	} else if isKeeperProxyPathExact(proxyPath, "/v1/chat/completions", "/chat/completions") {
		body, err = clampKeeperProxyJSONMaxTokens(body, KeeperMaxOutputTokens(account), "max_completion_tokens", "max_completion_tokens", "max_tokens")
	}
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, in.Method, upstreamURL, body)
	if err != nil {
		return nil, err
	}
	copyProxyRequestHeaders(req.Header, in.Header)
	req.Header.Set("Authorization", "Bearer "+apiKey)
	if orgID := strings.TrimSpace(account.GetOpenAIOrganizationID()); orgID != "" {
		req.Header.Set("OpenAI-Organization", orgID)
	}
	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	return doOpenAIHTTPUpstream(s.httpUpstream, req, proxyURL, account, s.tlsFPProfileService)
}

func (s *AccountTestService) ProxyKeeperAnthropicAccount(ctx context.Context, accountID int64, in KeeperOpenAIProxyRequest) (*http.Response, error) {
	if s == nil || s.accountRepo == nil {
		return nil, errors.New("account test service unavailable")
	}
	if s.httpUpstream == nil {
		return nil, errors.New("http upstream unavailable")
	}
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil {
		return nil, err
	}
	if account == nil || account.Platform != PlatformAnthropic {
		return nil, fmt.Errorf("account %d is not an Anthropic account", accountID)
	}
	if account.Type != AccountTypeAPIKey {
		return nil, fmt.Errorf("account %d is not an Anthropic API key account", accountID)
	}
	if !account.IsSchedulable() {
		return nil, fmt.Errorf("account %d is not schedulable", accountID)
	}
	apiKey := strings.TrimSpace(account.GetCredential("api_key"))
	if apiKey == "" {
		return nil, fmt.Errorf("account %d does not have Anthropic API key credentials", accountID)
	}
	baseURL := strings.TrimRight(strings.TrimSpace(account.GetBaseURL()), "/")
	if baseURL == "" {
		baseURL = "https://api.anthropic.com"
	} else {
		var err error
		baseURL, err = s.validateUpstreamBaseURL(baseURL)
		if err != nil {
			return nil, err
		}
	}
	proxyPath, err := validateKeeperAnthropicProxyPath(in.Path)
	if err != nil {
		return nil, err
	}
	upstreamURL := buildKeeperOpenAIProxyURL(baseURL, proxyPath)
	body := in.Body
	if isKeeperProxyPathExact(proxyPath, "/v1/messages") {
		body, err = clampKeeperProxyJSONMaxTokens(body, KeeperMaxOutputTokens(account), "max_tokens", "max_tokens")
		if err != nil {
			return nil, err
		}
	}
	req, err := http.NewRequestWithContext(ctx, in.Method, upstreamURL, body)
	if err != nil {
		return nil, err
	}
	copyProxyRequestHeaders(req.Header, in.Header)
	setAnthropicAPIKeyAuthHeader(req.Header, account, apiKey)
	if req.Header.Get("content-type") == "" {
		req.Header.Set("content-type", "application/json")
	}
	if req.Header.Get("anthropic-version") == "" {
		req.Header.Set("anthropic-version", "2023-06-01")
	}
	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	if s.tlsFPProfileService != nil && account.IsTLSFingerprintEnabled() {
		return s.httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, s.tlsFPProfileService.ResolveTLSProfile(account))
	}
	return s.httpUpstream.Do(req, proxyURL, account.ID, account.Concurrency)
}

func buildKeeperOpenAIProxyURL(baseURL string, proxyPath string) string {
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	path := "/" + strings.TrimLeft(strings.TrimSpace(proxyPath), "/")
	if strings.HasPrefix(path, "/v1/") && strings.HasSuffix(base, "/v1") {
		path = strings.TrimPrefix(path, "/v1")
	}
	return base + path
}

func KeeperMaxOutputTokens(account *Account) int {
	if account == nil {
		return DefaultKeeperMaxOutputTokens
	}
	value := keeperExtraInt(account.Extra, "keeper_keepalive_max_output_tokens")
	if value <= 0 {
		return DefaultKeeperMaxOutputTokens
	}
	return value
}

func keeperExtraInt(extra map[string]any, key string) int {
	if extra == nil {
		return 0
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
	return 0
}

func clampKeeperProxyJSONMaxTokens(body io.Reader, limit int, defaultField string, fields ...string) (io.Reader, error) {
	if limit <= 0 || body == nil {
		return body, nil
	}
	raw, err := io.ReadAll(io.LimitReader(body, keeperProxyBodyLimitBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > keeperProxyBodyLimitBytes {
		return nil, fmt.Errorf("keeper proxy request body exceeds %d bytes", keeperProxyBodyLimitBytes)
	}
	if strings.TrimSpace(string(raw)) == "" {
		return bytes.NewReader(raw), nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		return bytes.NewReader(raw), nil
	}

	changed := false
	found := false
	for _, field := range fields {
		current, exists := payload[field]
		if !exists {
			continue
		}
		found = true
		if n, ok := keeperJSONPositiveInt(current); ok && n <= limit {
			continue
		}
		payload[field] = limit
		changed = true
	}
	if !found && defaultField != "" {
		payload[defaultField] = limit
		changed = true
	}
	if !changed {
		return bytes.NewReader(raw), nil
	}
	updated, err := json.Marshal(payload)
	if err != nil {
		return bytes.NewReader(raw), nil
	}
	return bytes.NewReader(updated), nil
}

func keeperJSONPositiveInt(value any) (int, bool) {
	switch v := value.(type) {
	case int:
		return v, v > 0
	case int64:
		return int(v), v > 0
	case float64:
		return int(v), v > 0
	case json.Number:
		n, err := v.Int64()
		if err == nil && n > 0 {
			return int(n), true
		}
		f, err := v.Float64()
		if err == nil && f > 0 {
			return int(f), true
		}
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(v))
		if err == nil && n > 0 {
			return n, true
		}
	}
	return 0, false
}

func validateKeeperOpenAIProxyPath(proxyPath string) (string, error) {
	path, err := normalizeKeeperProxyPath(proxyPath)
	if err != nil {
		return "", err
	}
	if isKeeperProxyPathExact(path, "/v1/chat/completions", "/chat/completions", "/v1/models", "/models") ||
		isKeeperProxyPathPrefix(path, "/v1/responses", "/responses") {
		return path, nil
	}
	return "", fmt.Errorf("keeper OpenAI proxy path is not allowed: %s", path)
}

func validateKeeperAnthropicProxyPath(proxyPath string) (string, error) {
	path, err := normalizeKeeperProxyPath(proxyPath)
	if err != nil {
		return "", err
	}
	if isKeeperProxyPathExact(path, "/v1/messages", "/v1/messages/count_tokens", "/v1/models") {
		return path, nil
	}
	return "", fmt.Errorf("keeper Anthropic proxy path is not allowed: %s", path)
}

func normalizeKeeperProxyPath(proxyPath string) (string, error) {
	path := "/" + strings.TrimLeft(strings.TrimSpace(proxyPath), "/")
	if path == "/" {
		return "", errors.New("keeper proxy path is required")
	}
	if strings.Contains(path, "?") || strings.Contains(path, "#") {
		return "", fmt.Errorf("keeper proxy path must not include query or fragment: %s", path)
	}
	for _, segment := range strings.Split(path, "/") {
		if segment == "." || segment == ".." || strings.EqualFold(segment, "%2e") || strings.EqualFold(segment, "%2e%2e") {
			return "", fmt.Errorf("keeper proxy path contains unsafe segment: %s", path)
		}
	}
	return path, nil
}

func isKeeperProxyPathExact(path string, allowed ...string) bool {
	for _, item := range allowed {
		if path == item {
			return true
		}
	}
	return false
}

func isKeeperProxyPathPrefix(path string, allowed ...string) bool {
	for _, item := range allowed {
		if path == item || strings.HasPrefix(path, item+"/") {
			return true
		}
	}
	return false
}

func copyProxyRequestHeaders(dst http.Header, src http.Header) {
	for key, values := range src {
		if isHopByHopHeader(key) || strings.EqualFold(key, "Authorization") || strings.EqualFold(key, "X-Api-Key") {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func isHopByHopHeader(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade":
		return true
	default:
		return false
	}
}

type KeeperKeepaliveRequest struct {
	Model           string
	Prompt          string
	MaxOutputTokens int
}

type KeeperKeepaliveUsage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CachedInputTokens        int64 `json:"cached_input_tokens,omitempty"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens,omitempty"`
	TotalTokens              int64 `json:"total_tokens"`
}

type KeeperKeepaliveBilling struct {
	Available      bool    `json:"available"`
	RateMultiplier float64 `json:"rate_multiplier"`
	TotalCost      float64 `json:"total_cost,omitempty"`
	PricingSource  string  `json:"pricing_source"`
}

type KeeperKeepaliveResult struct {
	ID          string                  `json:"id"`
	AccountID   int64                   `json:"account_id"`
	AccountName string                  `json:"account_name"`
	Platform    string                  `json:"platform"`
	AccountType string                  `json:"account_type"`
	Model       string                  `json:"model"`
	Prompt      string                  `json:"prompt"`
	ReplyText   string                  `json:"reply_text,omitempty"`
	Summary     string                  `json:"summary,omitempty"`
	Status      string                  `json:"status"`
	Error       string                  `json:"error,omitempty"`
	Usage       *KeeperKeepaliveUsage   `json:"usage,omitempty"`
	Billing     *KeeperKeepaliveBilling `json:"billing,omitempty"`
	StartedAt   time.Time               `json:"started_at"`
	CompletedAt time.Time               `json:"completed_at,omitempty"`
	LatencyMS   int64                   `json:"latency_ms"`
}

// RunKeeperKeepalive executes one low-frequency keepalive request through the
// existing account test path, so credentials and upstream details stay inside
// sub2apiplus.
func (s *AccountTestService) RunKeeperKeepalive(ctx context.Context, accountID int64, req KeeperKeepaliveRequest) (*KeeperKeepaliveResult, error) {
	startedAt := time.Now()
	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil {
		return &KeeperKeepaliveResult{
			ID:          startedAt.Format("20060102T150405.000000000"),
			AccountID:   accountID,
			Model:       strings.TrimSpace(req.Model),
			Prompt:      strings.TrimSpace(req.Prompt),
			Status:      "error",
			Error:       "Account not found",
			StartedAt:   startedAt,
			CompletedAt: time.Now(),
		}, err
	}
	if account == nil || !account.IsSchedulable() {
		completedAt := time.Now()
		return &KeeperKeepaliveResult{
			ID:          startedAt.Format("20060102T150405.000000000"),
			AccountID:   accountID,
			AccountName: accountName(account),
			Platform:    accountPlatform(account),
			AccountType: accountType(account),
			Model:       strings.TrimSpace(req.Model),
			Prompt:      strings.TrimSpace(req.Prompt),
			Status:      "error",
			Error:       "Account is not schedulable",
			StartedAt:   startedAt,
			CompletedAt: completedAt,
			LatencyMS:   completedAt.Sub(startedAt).Milliseconds(),
		}, fmt.Errorf("account is not schedulable")
	}
	if account.Platform != PlatformOpenAI && account.Platform != PlatformAnthropic {
		completedAt := time.Now()
		return &KeeperKeepaliveResult{
			ID:          startedAt.Format("20060102T150405.000000000"),
			AccountID:   account.ID,
			AccountName: account.Name,
			Platform:    account.Platform,
			AccountType: account.Type,
			Model:       strings.TrimSpace(req.Model),
			Prompt:      strings.TrimSpace(req.Prompt),
			Status:      "error",
			Error:       "Unsupported account platform",
			StartedAt:   startedAt,
			CompletedAt: completedAt,
			LatencyMS:   completedAt.Sub(startedAt).Milliseconds(),
		}, fmt.Errorf("unsupported account platform")
	}

	w := httptest.NewRecorder()
	ginCtx, _ := gin.CreateTestContext(w)
	ginCtx.Request = (&http.Request{}).WithContext(ctx)

	model := strings.TrimSpace(req.Model)
	prompt := strings.TrimSpace(req.Prompt)
	testErr := s.TestAccountConnection(ginCtx, account.ID, model, prompt, AccountTestModeDefault, req.MaxOutputTokens)

	completedAt := time.Now()
	body := w.Body.String()
	replyText, errMsg := parseTestSSEOutput(body)
	usage := parseTestSSEUsage(body)
	status := "success"
	if testErr != nil || errMsg != "" {
		status = "error"
		if errMsg == "" && testErr != nil {
			errMsg = testErr.Error()
		}
	}
	if model == "" {
		model = modelFromCapturedSSE(body)
	}
	result := &KeeperKeepaliveResult{
		ID:          startedAt.Format("20060102T150405.000000000"),
		AccountID:   account.ID,
		AccountName: account.Name,
		Platform:    account.Platform,
		AccountType: account.Type,
		Model:       model,
		Prompt:      prompt,
		ReplyText:   replyText,
		Summary:     truncateKeeperSummary(replyText),
		Status:      status,
		Error:       errMsg,
		Usage:       usage,
		Billing: &KeeperKeepaliveBilling{
			Available:      false,
			RateMultiplier: account.BillingRateMultiplier(),
			PricingSource:  "sub2apiplus",
		},
		StartedAt:   startedAt,
		CompletedAt: completedAt,
		LatencyMS:   completedAt.Sub(startedAt).Milliseconds(),
	}
	if status != "success" {
		return result, fmt.Errorf("%s", errMsg)
	}
	return result, nil
}

func accountName(account *Account) string {
	if account == nil {
		return ""
	}
	return account.Name
}

func accountPlatform(account *Account) string {
	if account == nil {
		return ""
	}
	return account.Platform
}

func accountType(account *Account) string {
	if account == nil {
		return ""
	}
	return account.Type
}

func firstPositiveInt(values ...int) int {
	for _, value := range values {
		if value > 0 {
			return value
		}
	}
	return 0
}

func modelFromCapturedSSE(body string) string {
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		jsonStr := strings.TrimPrefix(line, "data: ")
		var event TestEvent
		if err := json.Unmarshal([]byte(jsonStr), &event); err != nil {
			continue
		}
		if event.Type == "test_start" && strings.TrimSpace(event.Model) != "" {
			return strings.TrimSpace(event.Model)
		}
	}
	return ""
}

func truncateKeeperSummary(text string) string {
	text = strings.TrimSpace(text)
	if len([]rune(text)) <= 240 {
		return text
	}
	runes := []rune(text)
	return string(runes[:240])
}

// RunTestBackground executes an account test in-memory (no real HTTP client),
// capturing SSE output via httptest.NewRecorder, then parses the result.
func (s *AccountTestService) RunTestBackground(ctx context.Context, accountID int64, modelID string) (*ScheduledTestResult, error) {
	startedAt := time.Now()

	w := httptest.NewRecorder()
	ginCtx, _ := gin.CreateTestContext(w)
	ginCtx.Request = (&http.Request{}).WithContext(ctx)

	testErr := s.TestAccountConnection(ginCtx, accountID, modelID, "", AccountTestModeDefault)

	finishedAt := time.Now()
	body := w.Body.String()
	responseText, errMsg := parseTestSSEOutput(body)

	status := "success"
	if testErr != nil || errMsg != "" {
		status = "failed"
		if errMsg == "" && testErr != nil {
			errMsg = testErr.Error()
		}
	}

	return &ScheduledTestResult{
		Status:       status,
		ResponseText: responseText,
		ErrorMessage: errMsg,
		LatencyMs:    finishedAt.Sub(startedAt).Milliseconds(),
		StartedAt:    startedAt,
		FinishedAt:   finishedAt,
	}, nil
}

// parseTestSSEOutput extracts response text and error message from captured SSE output.
func parseTestSSEOutput(body string) (responseText, errMsg string) {
	var texts []string
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		jsonStr := strings.TrimPrefix(line, "data: ")
		var event TestEvent
		if err := json.Unmarshal([]byte(jsonStr), &event); err != nil {
			continue
		}
		switch event.Type {
		case "content":
			if event.Text != "" {
				texts = append(texts, event.Text)
			}
		case "error":
			errMsg = event.Error
		}
	}
	responseText = strings.Join(texts, "")
	return
}

func parseTestSSEUsage(body string) *KeeperKeepaliveUsage {
	var usage *KeeperKeepaliveUsage
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		jsonStr := strings.TrimPrefix(line, "data: ")
		var event TestEvent
		if err := json.Unmarshal([]byte(jsonStr), &event); err != nil {
			continue
		}
		if event.Type == "usage" && event.Usage != nil {
			usage = event.Usage
		}
	}
	return usage
}

func keeperUsageFromResponseEvent(data map[string]any) *KeeperKeepaliveUsage {
	if usage := keeperUsageFromAny(data["usage"]); usage != nil {
		return usage
	}
	responseData, _ := data["response"].(map[string]any)
	return keeperUsageFromAny(responseData["usage"])
}

func keeperUsageFromAny(value any) *KeeperKeepaliveUsage {
	raw, ok := value.(map[string]any)
	if !ok || len(raw) == 0 {
		return nil
	}
	usage := &KeeperKeepaliveUsage{
		InputTokens:  int64FromAny(raw["input_tokens"]),
		OutputTokens: int64FromAny(raw["output_tokens"]),
		TotalTokens:  int64FromAny(raw["total_tokens"]),
	}
	if usage.TotalTokens == 0 {
		usage.TotalTokens = usage.InputTokens + usage.OutputTokens
	}
	if details, ok := raw["input_tokens_details"].(map[string]any); ok {
		usage.CachedInputTokens = int64FromAny(details["cached_tokens"])
		usage.CacheCreationInputTokens = int64FromAny(details["cache_creation_tokens"])
	}
	return usage
}

func int64FromAny(value any) int64 {
	switch v := value.(type) {
	case int64:
		return v
	case int:
		return int64(v)
	case float64:
		return int64(v)
	case json.Number:
		n, _ := v.Int64()
		return n
	default:
		return 0
	}
}
