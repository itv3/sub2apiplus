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
	if !account.getExtraBool("keeper_keepalive_enabled") {
		return nil, fmt.Errorf("account %d keeper keepalive is not enabled", accountID)
	}
	apiKey := strings.TrimSpace(account.GetOpenAIApiKey())
	if apiKey == "" {
		return nil, fmt.Errorf("account %d does not have OpenAI API key credentials", accountID)
	}
	baseURL, err := s.validateUpstreamBaseURL(account.GetOpenAIBaseURL())
	if err != nil {
		return nil, err
	}
	proxyPath, err := validateKeeperOpenAIProxyPath(in.Method, in.Path)
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
	applyAccountTestHeaderOverrides(account, req.Header)
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
	if !account.getExtraBool("keeper_keepalive_enabled") {
		return nil, fmt.Errorf("account %d keeper keepalive is not enabled", accountID)
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
	proxyPath, err := validateKeeperAnthropicProxyPath(in.Method, in.Path)
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
	applyAccountTestHeaderOverrides(account, req.Header)
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
	if value > KeeperProxyMaxOutputTokensHardCap {
		return KeeperProxyMaxOutputTokensHardCap
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

func validateKeeperOpenAIProxyPath(method string, proxyPath string) (string, error) {
	path, err := normalizeKeeperProxyPath(proxyPath)
	if err != nil {
		return "", err
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	if method == http.MethodPost && isKeeperProxyPathExact(path, "/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses") {
		return path, nil
	}
	if method == http.MethodGet && (isKeeperProxyPathExact(path, "/v1/models", "/models") ||
		isKeeperProxyPathPrefix(path, "/v1/responses", "/responses")) {
		return path, nil
	}
	return "", fmt.Errorf("keeper OpenAI proxy method/path is not allowed: %s %s", method, path)
}

func validateKeeperAnthropicProxyPath(method string, proxyPath string) (string, error) {
	path, err := normalizeKeeperProxyPath(proxyPath)
	if err != nil {
		return "", err
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	if method == http.MethodPost && isKeeperProxyPathExact(path, "/v1/messages", "/v1/messages/count_tokens") {
		return path, nil
	}
	if method == http.MethodGet && isKeeperProxyPathExact(path, "/v1/models") {
		return path, nil
	}
	return "", fmt.Errorf("keeper Anthropic proxy method/path is not allowed: %s %s", method, path)
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
		if !isKeeperProxyRequestHeaderAllowed(key) {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func isKeeperProxyRequestHeaderAllowed(name string) bool {
	if isHopByHopHeader(name) {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "accept",
		"anthropic-beta",
		"anthropic-client-sha",
		"anthropic-client-version",
		"anthropic-dangerous-direct-browser-access",
		"anthropic-version",
		"content-type",
		"conversation_id",
		"conversation-id",
		"originator",
		"openai-beta",
		"session_id",
		"session-id",
		"user-agent",
		"version",
		"x-codex-turn-metadata",
		"x-codex-turn-state",
		"x-request-id",
		"x-stainless-arch",
		"x-stainless-lang",
		"x-stainless-os",
		"x-stainless-package-version",
		"x-stainless-runtime",
		"x-stainless-runtime-version":
		return true
	default:
		return false
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

type KeeperKeepaliveUsage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CachedInputTokens        int64 `json:"cached_input_tokens,omitempty"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens,omitempty"`
	TotalTokens              int64 `json:"total_tokens"`
}

func firstPositiveInt(values ...int) int {
	for _, value := range values {
		if value > 0 {
			return value
		}
	}
	return 0
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
