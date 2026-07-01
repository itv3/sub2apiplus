package service

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

const (
	defaultKeeperMaxOutputTokens = 512
	maxKeeperOutputTokens        = 4096
)

var (
	ErrKeeperAccountNotFound     = errors.New("keeper account not found")
	ErrKeeperAccountUnavailable  = errors.New("keeper account is not schedulable")
	ErrKeeperUnsupportedAccount  = errors.New("keeper only supports openai and anthropic accounts")
	ErrKeeperBillingUnavailable  = errors.New("keeper billing service unavailable")
	ErrKeeperUpstreamUnavailable = errors.New("keeper upstream unavailable")
)

type KeeperKeepaliveRequest struct {
	Model           string `json:"model"`
	Prompt          string `json:"prompt"`
	MaxOutputTokens int    `json:"max_output_tokens"`
}

type KeeperUsage struct {
	InputTokens           int `json:"input_tokens"`
	OutputTokens          int `json:"output_tokens"`
	TotalTokens           int `json:"total_tokens"`
	CacheCreationTokens   int `json:"cache_creation_tokens,omitempty"`
	CacheReadTokens       int `json:"cache_read_tokens,omitempty"`
	CacheCreation5mTokens int `json:"cache_creation_5m_tokens,omitempty"`
	CacheCreation1hTokens int `json:"cache_creation_1h_tokens,omitempty"`
}

type KeeperBilling struct {
	Available            bool    `json:"available"`
	RateMultiplier       float64 `json:"rate_multiplier"`
	InputCost            float64 `json:"input_cost,omitempty"`
	OutputCost           float64 `json:"output_cost,omitempty"`
	CacheCreationCost    float64 `json:"cache_creation_cost,omitempty"`
	CacheReadCost        float64 `json:"cache_read_cost,omitempty"`
	TotalCost            float64 `json:"total_cost,omitempty"`
	ActualCost           float64 `json:"actual_cost,omitempty"`
	BillingMode          string  `json:"billing_mode,omitempty"`
	PricingSource        string  `json:"pricing_source"`
	UnavailableReason    string  `json:"unavailable_reason,omitempty"`
	UnavailableReasonRaw string  `json:"unavailable_reason_raw,omitempty"`
}

type KeeperKeepaliveResult struct {
	Success           bool          `json:"success"`
	AccountID         int64         `json:"account_id"`
	AccountName       string        `json:"account_name"`
	Platform          string        `json:"platform"`
	AccountType       string        `json:"account_type"`
	Model             string        `json:"model"`
	Prompt            string        `json:"prompt"`
	ReplyText         string        `json:"reply_text"`
	Usage             KeeperUsage   `json:"usage"`
	Billing           KeeperBilling `json:"billing"`
	StartedAt         time.Time     `json:"started_at"`
	CompletedAt       time.Time     `json:"completed_at"`
	LatencyMS         int64         `json:"latency_ms"`
	UpstreamRequestID string        `json:"upstream_request_id,omitempty"`
}

func (s *AccountTestService) RunKeeperKeepalive(ctx context.Context, accountID int64, req KeeperKeepaliveRequest) (*KeeperKeepaliveResult, error) {
	if s == nil || s.accountRepo == nil {
		return nil, ErrKeeperAccountNotFound
	}
	if s.httpUpstream == nil {
		return nil, ErrKeeperUpstreamUnavailable
	}

	account, err := s.accountRepo.GetByID(ctx, accountID)
	if err != nil || account == nil {
		return nil, ErrKeeperAccountNotFound
	}

	prompt := strings.TrimSpace(req.Prompt)
	if prompt == "" {
		return nil, fmt.Errorf("keeper prompt is required")
	}
	maxOutputTokens := normalizeKeeperMaxOutputTokens(req.MaxOutputTokens)
	startedAt := time.Now()

	var result *KeeperKeepaliveResult
	switch account.Platform {
	case PlatformOpenAI:
		result, err = s.runOpenAIKeeperKeepalive(ctx, account, req.Model, prompt, maxOutputTokens)
	case PlatformAnthropic:
		result, err = s.runClaudeKeeperKeepalive(ctx, account, req.Model, prompt, maxOutputTokens)
	default:
		return nil, ErrKeeperUnsupportedAccount
	}
	if err != nil {
		return nil, err
	}

	result.Success = true
	result.AccountID = account.ID
	result.AccountName = account.Name
	result.Platform = account.Platform
	result.AccountType = account.Type
	result.Prompt = prompt
	result.StartedAt = startedAt
	result.CompletedAt = time.Now()
	result.LatencyMS = result.CompletedAt.Sub(startedAt).Milliseconds()
	result.Billing = s.calculateKeeperBilling(result.Model, result.Usage, account.BillingRateMultiplier())
	return result, nil
}

func (s *AccountTestService) runClaudeKeeperKeepalive(ctx context.Context, account *Account, modelID, prompt string, maxOutputTokens int) (*KeeperKeepaliveResult, error) {
	testModelID := strings.TrimSpace(modelID)
	if testModelID == "" {
		testModelID = claude.DefaultTestModel
	}
	if account.Type == AccountTypeAPIKey {
		testModelID = account.GetMappedModel(testModelID)
	}
	if account.IsBedrock() || account.Type == AccountTypeServiceAccount {
		return nil, fmt.Errorf("keeper does not support anthropic account type: %s", account.Type)
	}

	authToken, useBearer, apiURL, err := s.resolveKeeperClaudeAuth(account)
	if err != nil {
		return nil, err
	}

	payload, err := createKeeperClaudePayload(testModelID, prompt, maxOutputTokens)
	if err != nil {
		return nil, err
	}
	payloadBytes, _ := json.Marshal(payload)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, apiURL, bytes.NewReader(payloadBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create keeper anthropic request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("anthropic-version", "2023-06-01")
	for key, value := range claude.DefaultHeaders {
		httpReq.Header.Set(key, value)
	}
	if useBearer {
		httpReq.Header.Set("anthropic-beta", claude.DefaultBetaHeader)
		httpReq.Header.Set("Authorization", "Bearer "+authToken)
	} else {
		httpReq.Header.Set("anthropic-beta", claude.APIKeyBetaHeader)
		httpReq.Header.Set("x-api-key", authToken)
	}

	resp, err := s.httpUpstream.DoWithTLS(httpReq, keeperProxyURL(account), account.ID, account.Concurrency, s.resolveKeeperTLSProfile(account))
	if err != nil {
		return nil, fmt.Errorf("keeper anthropic request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		return nil, fmt.Errorf("keeper anthropic returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	reply, usage, err := collectKeeperClaudeStream(resp.Body)
	if err != nil {
		return nil, err
	}
	return &KeeperKeepaliveResult{
		Model:             testModelID,
		ReplyText:         reply,
		Usage:             usage,
		UpstreamRequestID: resp.Header.Get("request-id"),
	}, nil
}

func (s *AccountTestService) runOpenAIKeeperKeepalive(ctx context.Context, account *Account, modelID, prompt string, maxOutputTokens int) (*KeeperKeepaliveResult, error) {
	testModelID := strings.TrimSpace(modelID)
	if testModelID == "" {
		testModelID = openai.DefaultTestModel
	}
	testModelID = account.GetMappedModel(testModelID)

	authToken, apiURL, isOAuth, mimicProfile, err := s.resolveKeeperOpenAIAuth(account)
	if err != nil {
		return nil, err
	}
	if account.Type == AccountTypeAPIKey && !mimicProfile.ShouldUseResponsesAPI(account.Extra) {
		return s.runOpenAIChatCompletionsKeeperKeepalive(ctx, account, testModelID, prompt, authToken, apiURL)
	}

	payload := createKeeperOpenAIResponsesPayload(testModelID, prompt, isOAuth, maxOutputTokens)
	payloadBytes, _ := json.Marshal(payload)
	if mimicProfile.Enabled {
		payloadBytes = mimicProfile.RewriteBody(payloadBytes)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, apiURL, bytes.NewReader(payloadBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create keeper openai request: %w", err)
	}
	httpReq = httpReq.WithContext(WithHTTPUpstreamProfile(httpReq.Context(), HTTPUpstreamProfileOpenAI))
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")
	httpReq.Header.Set("Authorization", "Bearer "+authToken)
	if isOAuth {
		httpReq.Host = "chatgpt.com"
		setOpenAIChatGPTAccountHeaders(httpReq.Header, account)
	}
	if mimicProfile.Enabled {
		mimicProfile.ApplyHeaders(httpReq, true)
	}

	resp, err := doOpenAIHTTPUpstream(s.httpUpstream, httpReq, keeperProxyURL(account), account, s.tlsFPProfileService)
	if err != nil {
		return nil, fmt.Errorf("keeper openai request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		return nil, fmt.Errorf("keeper openai returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	reply, usage, err := collectKeeperOpenAIResponsesStream(resp.Body)
	if err != nil {
		return nil, err
	}
	return &KeeperKeepaliveResult{
		Model:             testModelID,
		ReplyText:         reply,
		Usage:             usage,
		UpstreamRequestID: resp.Header.Get("x-request-id"),
	}, nil
}

func (s *AccountTestService) runOpenAIChatCompletionsKeeperKeepalive(ctx context.Context, account *Account, modelID, prompt, authToken, responsesURL string) (*KeeperKeepaliveResult, error) {
	apiURL := strings.TrimSuffix(responsesURL, "/v1/responses") + "/v1/chat/completions"
	payloadBytes, _ := json.Marshal(createOpenAIChatCompletionsTestPayload(modelID, prompt))

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, apiURL, bytes.NewReader(payloadBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create keeper chat completions request: %w", err)
	}
	httpReq = httpReq.WithContext(WithHTTPUpstreamProfile(httpReq.Context(), HTTPUpstreamProfileOpenAI))
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")
	httpReq.Header.Set("Authorization", "Bearer "+authToken)

	resp, err := doOpenAIHTTPUpstream(s.httpUpstream, httpReq, keeperProxyURL(account), account, s.tlsFPProfileService)
	if err != nil {
		return nil, fmt.Errorf("keeper chat completions request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		return nil, fmt.Errorf("keeper chat completions returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	reply, usage, err := collectKeeperOpenAIChatCompletionsStream(resp.Body)
	if err != nil {
		return nil, err
	}
	return &KeeperKeepaliveResult{
		Model:             modelID,
		ReplyText:         reply,
		Usage:             usage,
		UpstreamRequestID: resp.Header.Get("x-request-id"),
	}, nil
}

func (s *AccountTestService) resolveKeeperClaudeAuth(account *Account) (string, bool, string, error) {
	if account.IsOAuth() {
		authToken := account.GetCredential("access_token")
		if authToken == "" {
			return "", false, "", fmt.Errorf("no anthropic access token available")
		}
		return authToken, true, testClaudeAPIURL, nil
	}
	if account.Type == AccountTypeAPIKey {
		authToken := account.GetCredential("api_key")
		if authToken == "" {
			return "", false, "", fmt.Errorf("no anthropic api key available")
		}
		baseURL := account.GetBaseURL()
		if baseURL == "" {
			baseURL = "https://api.anthropic.com"
		}
		normalizedBaseURL, err := s.validateUpstreamBaseURL(baseURL)
		if err != nil {
			return "", false, "", fmt.Errorf("invalid anthropic base url: %w", err)
		}
		return authToken, false, strings.TrimSuffix(normalizedBaseURL, "/") + "/v1/messages?beta=true", nil
	}
	return "", false, "", fmt.Errorf("unsupported anthropic account type: %s", account.Type)
}

func (s *AccountTestService) resolveKeeperOpenAIAuth(account *Account) (string, string, bool, openAIAPIKeyCodexMimicProfile, error) {
	mimicProfile := resolveOpenAIAPIKeyCodexMimicProfile(account, 0, s.cfg)
	if account.IsOAuth() {
		authToken := account.GetOpenAIAccessToken()
		if authToken == "" {
			return "", "", false, mimicProfile, fmt.Errorf("no openai access token available")
		}
		return authToken, chatgptCodexAPIURL, true, mimicProfile, nil
	}
	if account.Type == AccountTypeAPIKey {
		authToken := account.GetOpenAIApiKey()
		if authToken == "" {
			return "", "", false, mimicProfile, fmt.Errorf("no openai api key available")
		}
		baseURL := account.GetOpenAIBaseURL()
		if baseURL == "" {
			baseURL = "https://api.openai.com"
		}
		normalizedBaseURL, err := s.validateUpstreamBaseURL(baseURL)
		if err != nil {
			return "", "", false, mimicProfile, fmt.Errorf("invalid openai base url: %w", err)
		}
		return authToken, buildOpenAIResponsesURL(normalizedBaseURL), false, mimicProfile, nil
	}
	return "", "", false, mimicProfile, fmt.Errorf("unsupported openai account type: %s", account.Type)
}

func (s *AccountTestService) calculateKeeperBilling(model string, usage KeeperUsage, rateMultiplier float64) KeeperBilling {
	billing := KeeperBilling{
		Available:      false,
		RateMultiplier: rateMultiplier,
		PricingSource:  "sub2apiplus",
	}
	if s == nil || s.billingService == nil {
		billing.UnavailableReason = ErrKeeperBillingUnavailable.Error()
		return billing
	}
	breakdown, err := s.billingService.CalculateCost(model, UsageTokens{
		InputTokens:           usage.InputTokens,
		OutputTokens:          usage.OutputTokens,
		CacheCreationTokens:   usage.CacheCreationTokens,
		CacheReadTokens:       usage.CacheReadTokens,
		CacheCreation5mTokens: usage.CacheCreation5mTokens,
		CacheCreation1hTokens: usage.CacheCreation1hTokens,
	}, rateMultiplier)
	if err != nil {
		billing.UnavailableReason = "pricing unavailable"
		billing.UnavailableReasonRaw = err.Error()
		return billing
	}
	billing.Available = true
	billing.InputCost = breakdown.InputCost
	billing.OutputCost = breakdown.OutputCost
	billing.CacheCreationCost = breakdown.CacheCreationCost
	billing.CacheReadCost = breakdown.CacheReadCost
	billing.TotalCost = breakdown.TotalCost
	billing.ActualCost = breakdown.ActualCost
	billing.BillingMode = breakdown.BillingMode
	return billing
}

func createKeeperClaudePayload(modelID, prompt string, maxOutputTokens int) (map[string]any, error) {
	sessionID, err := generateSessionString()
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"model": modelID,
		"messages": []map[string]any{
			{
				"role": "user",
				"content": []map[string]any{
					{
						"type": "text",
						"text": prompt,
						"cache_control": map[string]string{
							"type": "ephemeral",
						},
					},
				},
			},
		},
		"system": []map[string]any{
			{
				"type": "text",
				"text": claudeCodeSystemPrompt,
				"cache_control": map[string]string{
					"type": "ephemeral",
				},
			},
		},
		"metadata": map[string]string{
			"user_id": sessionID,
		},
		"max_tokens":  maxOutputTokens,
		"temperature": 1,
		"stream":      true,
	}, nil
}

func createKeeperOpenAIResponsesPayload(modelID, prompt string, isOAuth bool, maxOutputTokens int) map[string]any {
	payload := map[string]any{
		"model": modelID,
		"input": []map[string]any{
			{
				"role": "user",
				"content": []map[string]any{
					{
						"type": "input_text",
						"text": prompt,
					},
				},
			},
		},
		"instructions":        openai.DefaultInstructions,
		"max_output_tokens":   maxOutputTokens,
		"parallel_tool_calls": false,
		"stream":              true,
	}
	if isOAuth {
		payload["store"] = false
	}
	return payload
}

func collectKeeperClaudeStream(body io.Reader) (string, KeeperUsage, error) {
	reader := bufio.NewReader(body)
	var reply strings.Builder
	var usage KeeperUsage
	seenStop := false

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			if err == io.EOF {
				if seenStop {
					usage.TotalTokens = usage.InputTokens + usage.OutputTokens + usage.CacheCreationTokens + usage.CacheReadTokens
					return reply.String(), usage, nil
				}
				return reply.String(), usage, fmt.Errorf("keeper anthropic stream ended before message_stop")
			}
			return reply.String(), usage, fmt.Errorf("keeper anthropic stream read error: %w", err)
		}
		line = strings.TrimSpace(line)
		if line == "" || !sseDataPrefix.MatchString(line) {
			continue
		}
		jsonStr := sseDataPrefix.ReplaceAllString(line, "")
		if jsonStr == "[DONE]" {
			seenStop = true
			continue
		}

		var data map[string]any
		if err := json.Unmarshal([]byte(jsonStr), &data); err != nil {
			continue
		}
		switch eventType, _ := data["type"].(string); eventType {
		case "message_start":
			if msg, ok := data["message"].(map[string]any); ok {
				mergeKeeperClaudeUsage(&usage, msg["usage"])
			}
		case "content_block_delta":
			if delta, ok := data["delta"].(map[string]any); ok {
				reply.WriteString(stringFromKeeperMap(delta, "text"))
			}
		case "message_delta":
			mergeKeeperClaudeUsage(&usage, data["usage"])
		case "message_stop":
			seenStop = true
			usage.TotalTokens = usage.InputTokens + usage.OutputTokens + usage.CacheCreationTokens + usage.CacheReadTokens
			return reply.String(), usage, nil
		case "error":
			return reply.String(), usage, fmt.Errorf("keeper anthropic stream error: %s", keeperStreamErrorMessage(data))
		}
	}
}

func collectKeeperOpenAIResponsesStream(body io.Reader) (string, KeeperUsage, error) {
	reader := bufio.NewReader(body)
	var reply strings.Builder
	var usage KeeperUsage
	seenCompleted := false

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			if err == io.EOF {
				if seenCompleted {
					fillKeeperUsageTotalTokens(&usage)
					return reply.String(), usage, nil
				}
				return reply.String(), usage, fmt.Errorf("keeper openai stream ended before response.completed")
			}
			return reply.String(), usage, fmt.Errorf("keeper openai stream read error: %w", err)
		}
		line = strings.TrimSpace(line)
		if line == "" || !sseDataPrefix.MatchString(line) {
			continue
		}
		jsonStr := sseDataPrefix.ReplaceAllString(line, "")
		if jsonStr == "[DONE]" {
			if seenCompleted {
				fillKeeperUsageTotalTokens(&usage)
				return reply.String(), usage, nil
			}
			continue
		}

		var data map[string]any
		if err := json.Unmarshal([]byte(jsonStr), &data); err != nil {
			continue
		}
		switch eventType, _ := data["type"].(string); eventType {
		case "response.output_text.delta":
			reply.WriteString(stringFromKeeperMap(data, "delta"))
		case "response.completed", "response.done":
			seenCompleted = true
			if responseData, ok := data["response"].(map[string]any); ok {
				mergeKeeperOpenAIUsage(&usage, responseData["usage"])
			}
			fillKeeperUsageTotalTokens(&usage)
			return reply.String(), usage, nil
		case "response.failed":
			if responseData, ok := data["response"].(map[string]any); ok {
				return reply.String(), usage, fmt.Errorf("keeper openai response failed: %s", keeperStreamErrorMessage(responseData))
			}
			return reply.String(), usage, fmt.Errorf("keeper openai response failed")
		case "error":
			return reply.String(), usage, fmt.Errorf("keeper openai stream error: %s", keeperStreamErrorMessage(data))
		}
	}
}

func collectKeeperOpenAIChatCompletionsStream(body io.Reader) (string, KeeperUsage, error) {
	reader := bufio.NewReader(body)
	var reply strings.Builder
	var usage KeeperUsage
	seenFinish := false

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			if err == io.EOF {
				if seenFinish {
					fillKeeperUsageTotalTokens(&usage)
					return reply.String(), usage, nil
				}
				return reply.String(), usage, fmt.Errorf("keeper chat completions stream ended before [DONE]")
			}
			return reply.String(), usage, fmt.Errorf("keeper chat completions stream read error: %w", err)
		}
		line = strings.TrimSpace(line)
		if line == "" || !sseDataPrefix.MatchString(line) {
			continue
		}
		jsonStr := sseDataPrefix.ReplaceAllString(line, "")
		if jsonStr == "[DONE]" {
			seenFinish = true
			fillKeeperUsageTotalTokens(&usage)
			return reply.String(), usage, nil
		}

		var data map[string]any
		if err := json.Unmarshal([]byte(jsonStr), &data); err != nil {
			continue
		}
		if errData, ok := data["error"].(map[string]any); ok {
			return reply.String(), usage, fmt.Errorf("keeper chat completions stream error: %s", keeperStreamErrorMessage(map[string]any{"error": errData}))
		}
		if rawUsage, ok := data["usage"]; ok {
			mergeKeeperChatCompletionsUsage(&usage, rawUsage)
		}
		if choices, ok := data["choices"].([]any); ok {
			for _, choice := range choices {
				choiceMap, _ := choice.(map[string]any)
				if delta, ok := choiceMap["delta"].(map[string]any); ok {
					reply.WriteString(stringFromKeeperMap(delta, "content"))
				}
				if finishReason := stringFromKeeperMap(choiceMap, "finish_reason"); finishReason != "" {
					seenFinish = true
				}
			}
		}
	}
}

func mergeKeeperClaudeUsage(target *KeeperUsage, raw any) {
	usage, ok := raw.(map[string]any)
	if !ok || usage == nil {
		return
	}
	if v := intFromKeeperMap(usage, "input_tokens"); v > 0 {
		target.InputTokens = v
	}
	if v := intFromKeeperMap(usage, "output_tokens"); v > 0 {
		target.OutputTokens = v
	}
	if v := intFromKeeperMap(usage, "cache_creation_input_tokens"); v > 0 {
		target.CacheCreationTokens = v
	}
	if v := intFromKeeperMap(usage, "cache_read_input_tokens"); v > 0 {
		target.CacheReadTokens = v
	}
	if v := intFromKeeperMap(usage, "cache_creation_5m_input_tokens"); v > 0 {
		target.CacheCreation5mTokens = v
	}
	if v := intFromKeeperMap(usage, "cache_creation_1h_input_tokens"); v > 0 {
		target.CacheCreation1hTokens = v
	}
}

func mergeKeeperOpenAIUsage(target *KeeperUsage, raw any) {
	usage, ok := raw.(map[string]any)
	if !ok || usage == nil {
		return
	}
	if v := intFromKeeperMap(usage, "input_tokens"); v > 0 {
		target.InputTokens = v
	}
	if v := intFromKeeperMap(usage, "output_tokens"); v > 0 {
		target.OutputTokens = v
	}
	if v := intFromKeeperMap(usage, "total_tokens"); v > 0 {
		target.TotalTokens = v
	}
	if details, ok := usage["input_tokens_details"].(map[string]any); ok {
		if v := intFromKeeperMap(details, "cached_tokens"); v > 0 {
			target.CacheReadTokens = v
		}
	}
}

func mergeKeeperChatCompletionsUsage(target *KeeperUsage, raw any) {
	usage, ok := raw.(map[string]any)
	if !ok || usage == nil {
		return
	}
	if v := intFromKeeperMap(usage, "prompt_tokens"); v > 0 {
		target.InputTokens = v
	}
	if v := intFromKeeperMap(usage, "completion_tokens"); v > 0 {
		target.OutputTokens = v
	}
	if v := intFromKeeperMap(usage, "total_tokens"); v > 0 {
		target.TotalTokens = v
	}
}

func intFromKeeperMap(data map[string]any, key string) int {
	switch v := data[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	case json.Number:
		i, _ := v.Int64()
		return int(i)
	default:
		return 0
	}
}

func stringFromKeeperMap(data map[string]any, key string) string {
	v, _ := data[key].(string)
	return v
}

func keeperStreamErrorMessage(data map[string]any) string {
	if errData, ok := data["error"].(map[string]any); ok {
		if msg := stringFromKeeperMap(errData, "message"); msg != "" {
			return msg
		}
		if typ := stringFromKeeperMap(errData, "type"); typ != "" {
			return typ
		}
	}
	if msg := stringFromKeeperMap(data, "message"); msg != "" {
		return msg
	}
	return "unknown error"
}

func normalizeKeeperMaxOutputTokens(v int) int {
	if v <= 0 {
		return defaultKeeperMaxOutputTokens
	}
	if v > maxKeeperOutputTokens {
		return maxKeeperOutputTokens
	}
	return v
}

func fillKeeperUsageTotalTokens(usage *KeeperUsage) {
	if usage != nil && usage.TotalTokens == 0 {
		usage.TotalTokens = usage.InputTokens + usage.OutputTokens + usage.CacheCreationTokens + usage.CacheReadTokens
	}
}

func keeperProxyURL(account *Account) string {
	if account != nil && account.ProxyID != nil && account.Proxy != nil {
		return account.Proxy.URL()
	}
	return ""
}

func (s *AccountTestService) resolveKeeperTLSProfile(account *Account) *tlsfingerprint.Profile {
	if s == nil || s.tlsFPProfileService == nil {
		return nil
	}
	return s.tlsFPProfileService.ResolveTLSProfile(account)
}
