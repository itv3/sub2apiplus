package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

type alphaSearchAccountStateRepo struct {
	AccountRepository
	setErrorCalls int
	lastError     string
}

func (r *alphaSearchAccountStateRepo) SetError(_ context.Context, _ int64, errorMsg string) error {
	r.setErrorCalls++
	r.lastError = errorMsg
	return nil
}

func alphaSearchResponsesSSE(output string) string {
	return "event: response.output_text.delta\n" +
		`data: {"type":"response.output_text.delta","delta":` + strconv.Quote(output) + `}` + "\n\n" +
		"event: response.output_text.annotation.added\n" +
		`data: {"type":"response.output_text.annotation.added","annotation":{"type":"url_citation","url":"https://example.com/news","title":"Example News"}}` + "\n\n" +
		"event: response.completed\n" +
		`data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":` + strconv.Quote(output) + `}]}]}}` + "\n\n"
}

func TestForwardAlphaSearchOAuthAppliesCodex0145WireContract(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{
		"id":"search-session",
		"model":"gpt-5.6-sol",
		"reasoning":{"effort":"max","context":"all_turns"},
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"latest news"}]}],
		"commands":{"search_query":[{"q":"OpenAI news","recency":1}]},
		"settings":{"allowed_callers":["direct"],"external_web_access":true},
		"max_output_tokens":2000,
		"future_field":{"keep":true}
	}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search?feature=standalone", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", codexCLIUserAgent)
	c.Request.Header.Set("Originator", "codex_exec")
	c.Request.Header.Set("Version", "0.144.1")
	c.Request.Header.Set("X-Codex-Turn-Metadata", `{"session_id":"search-session","turn_id":"search-turn"}`)

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"encrypted_output":"ciphertext","output":"search result"}`)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          42,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.Equal(t, 1, result.WebSearchCalls)
	require.Equal(t, "gpt-5.6-sol", result.Model)
	require.Equal(t, http.StatusOK, recorder.Code)
	require.JSONEq(t, `{"encrypted_output":"ciphertext","output":"search result"}`, recorder.Body.String())
	// OAuth 路径不透传入站 query：官方 provider 的 query_params 恒为 None，
	// alpha/search 的 URL 不带任何参数（SPEC-EP-013）。
	require.Equal(t, chatgptCodexAlphaSearchURL, upstream.lastReq.URL.String())
	require.Equal(t, "chatgpt.com", upstream.lastReq.Host)
	require.Equal(t, "Bearer oauth-token", upstream.lastReq.Header.Get("Authorization"))
	require.Equal(t, "chatgpt-account", upstream.lastReq.Header.Get("chatgpt-account-id"))
	// 官方 alpha/search 走 execute，accept 由 reqwest 补默认 */*（SPEC-HDR-006）。
	require.Equal(t, "*/*", upstream.lastReq.Header.Get("Accept"))
	require.Equal(t, activeOpenAICodexVersionForTest(), upstream.lastReq.Header.Get("Version"))
	require.Equal(t, "application/json", upstream.lastReq.Header.Get("Content-Type"))
	require.NotEmpty(t, upstream.lastReq.Header.Get("X-Codex-Turn-Metadata"))
	require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))
	require.Equal(t,
		`{"id":"search-session","model":"gpt-5.6-sol","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"latest news"}]}],"commands":{"search_query":[{"q":"OpenAI news","recency":1}]},"settings":{"allowed_callers":["direct"],"external_web_access":true},"max_output_tokens":2000}`,
		string(upstream.lastBody),
	)
	require.False(t, gjson.GetBytes(upstream.lastBody, "reasoning").Exists())
	require.False(t, gjson.GetBytes(upstream.lastBody, "future_field").Exists())
	require.NotNil(t, upstream.lastTLSProfile)
	var alphaRule *tlsfingerprint.H1HeaderOrderRule
	for index := range upstream.lastTLSProfile.Transport.H1HeaderOrders {
		rule := &upstream.lastTLSProfile.Transport.H1HeaderOrders[index]
		if rule.Method == http.MethodPost && rule.Path == "/backend-api/codex/alpha/search" {
			alphaRule = rule
			break
		}
	}
	require.NotNil(t, alphaRule)
	require.Equal(t, chatgptCodexAlphaSearchURL, "https://"+upstream.lastReq.Host+alphaRule.Path)
	require.True(t, alphaRule.RejectUnlisted)
}

func TestBuildOpenAIAlphaSearchRequestReusesInvocationWithinIngressOnly(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{},"settings":{},"max_output_tokens":2000}`)
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: &liveHTTPUpstreamStub{}}
	account := &Account{
		ID:       42,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}
	newIngress := func() *gin.Context {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))
		return c
	}
	invocationID := func(req *http.Request) string {
		egressContext, ok := OfficialEgressContextFromContext(req.Context())
		require.True(t, ok)
		return egressContext.InvocationID()
	}

	firstIngress := newIngress()
	first, err := service.buildOpenAIAlphaSearchRequest(context.Background(), firstIngress, account, body, "oauth-token")
	require.NoError(t, err)
	second, err := service.buildOpenAIAlphaSearchRequest(context.Background(), firstIngress, account, body, "oauth-token")
	require.NoError(t, err)
	thirdIngress := newIngress()
	third, err := service.buildOpenAIAlphaSearchRequest(context.Background(), thirdIngress, account, body, "oauth-token")
	require.NoError(t, err)

	require.Equal(t, invocationID(first), invocationID(second))
	require.NotEqual(t, invocationID(first), invocationID(third))
}

func TestForwardAlphaSearchPATUsesResponsesWebSearchFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{
		"id":"search-session",
		"model":"gpt-5.6-sol",
		"input":[],
		"commands":{"search_query":[{"q":"OpenAI news"}]},
		"settings":{},
		"max_output_tokens":2000,
		"prompt_cache_key":"responses-cache-key",
		"prompt_cache_retention":"24h"
	}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", codexCLIUserAgent)
	c.Request.Header.Set("Originator", "codex_exec")
	c.Request.Header.Set("Version", "0.144.1")
	c.Request.Header.Set("OpenAI-Beta", "responses=experimental")
	c.Request.Header.Set("Accept-Language", "zh-CN")
	c.Request.Header.Set("Authorization", "Bearer client-token")
	c.Request.Header.Set("Session_ID", "session-client")
	c.Request.Header.Set("Conversation_ID", "conversation-client")
	c.Request.Header.Set("X-Codex-Beta-Features", "feature-a")
	c.Request.Header.Set("X-Codex-Turn-State", "turn-state")
	c.Request.Header.Set(responsesLiteHeaderKey, "true")
	c.Request.Header.Set("X-Codex-Turn-Metadata", `{"turn_id":"turn-1"}`)

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}, "x-request-id": []string{"req-search"}},
		Body:       io.NopCloser(strings.NewReader(alphaSearchResponsesSSE("search result"))),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          43,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":               "at-test-token",
			"auth_mode":                  OpenAIAuthModePersonalAccessToken,
			"chatgpt_account_id":         "chatgpt-account",
			"chatgpt_account_is_fedramp": true,
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	if err != nil {
		rawEvents, _ := c.Get(OpsUpstreamErrorsKey)
		events, _ := rawEvents.([]*OpsUpstreamErrorEvent)
		if len(events) > 0 {
			t.Fatalf("PAT Responses fallback 失败: %v, ops=%+v", err, *events[0])
		}
		t.Fatalf("PAT Responses fallback 失败: %v", err)
	}
	require.NotNil(t, result)
	require.Equal(t, 1, result.WebSearchCalls)
	require.Equal(t, "/v1/responses", result.UpstreamEndpoint)
	require.Equal(t, http.StatusOK, recorder.Code)
	require.JSONEq(t, `{"output":"search result","results":[{"type":"text_result","ref_id":"turn0search0","url":"https://example.com/news","title":"Example News"}]}`, recorder.Body.String())
	require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
	require.Equal(t, "Bearer at-test-token", upstream.lastReq.Header.Get("Authorization"))
	require.Equal(t, "chatgpt-account", upstream.lastReq.Header.Get("ChatGPT-Account-ID"))
	require.Equal(t, "true", upstream.lastReq.Header.Get("X-OpenAI-Fedramp"))
	require.Equal(t, "application/json", upstream.lastReq.Header.Get("Content-Type"))
	require.Equal(t, "text/event-stream", upstream.lastReq.Header.Get("Accept"))
	require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))
	require.Equal(t, activeOpenAICodexVersionForTest(), upstream.lastReq.Header.Get("Version"))
	turnMetadata := upstream.lastReq.Header.Get("X-Codex-Turn-Metadata")
	require.NotEmpty(t, gjson.Get(turnMetadata, "turn_id").String())
	require.NotEmpty(t, gjson.Get(turnMetadata, "session_id").String())
	require.NotEqual(t, "turn-1", gjson.Get(turnMetadata, "turn_id").String(),
		"重建的 Responses body 必须由 Executor 生成自身身份，不得套用 alpha 入站 turn")
	require.Equal(t, officialOpenAIHTTPOriginator, upstream.lastReq.Header.Get("Originator"))
	require.Equal(t, officialOpenAIHTTPBetaFeatures, upstream.lastReq.Header.Get("X-Codex-Beta-Features"))
	require.Empty(t, upstream.lastReq.Header.Get("X-Codex-Turn-State"))
	require.Empty(t, upstream.lastReq.Header.Get(responsesLiteHeaderKey))
	require.Empty(t, upstream.lastReq.Header.Get("Accept-Language"))
	identity, ok := officialegress.AttemptIdentityFromContext(upstream.lastReq.Context())
	require.True(t, ok)
	require.Equal(t, officialEgressSinkAlphaSearchPATFallback, identity.SinkID)
	require.True(t, identity.HasFinalizationToken,
		"PAT alpha fallback 必须由 Codex Executor 签发 FinalizationToken")
	require.NotEmpty(t, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.NotEqual(t, "responses-cache-key", gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "prompt_cache_retention").Exists())
	require.Equal(t, "gpt-5.6-sol", gjson.GetBytes(upstream.lastBody, "model").String())
	require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
	require.False(t, gjson.GetBytes(upstream.lastBody, "store").Bool())
	require.Equal(t, "web_search", gjson.GetBytes(upstream.lastBody, "tools.0.type").String())
	require.Contains(t, gjson.GetBytes(upstream.lastBody, "input.0.content.0.text").String(), `"search_query"`)
}

func TestForwardAlphaSearchPATBackfillsMissingChatGPTAccountMetadata(t *testing.T) {
	configureObserveGuardForLocalHTTPTest(t)
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"OpenAI news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")

	var whoamiCalls int32
	whoamiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&whoamiCalls, 1)
		require.Equal(t, "Bearer at-test-token", r.Header.Get("Authorization"))
		require.Equal(t, "application/json", r.Header.Get("Accept"))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"email":"pat@example.com",
			"chatgpt_user_id":"user-123",
			"chatgpt_account_id":"acct-123",
			"chatgpt_plan_type":"plus",
			"chatgpt_account_is_fedramp":true
		}`))
	}))
	defer whoamiServer.Close()
	oldWhoamiURL := openAICodexPATWhoamiURL
	openAICodexPATWhoamiURL = whoamiServer.URL
	defer func() { openAICodexPATWhoamiURL = oldWhoamiURL }()

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"output":"search result"}`)),
	}}
	oauthService := NewOpenAIOAuthService(nil, nil)
	service := &OpenAIGatewayService{
		cfg:                 &config.Config{},
		httpUpstream:        upstream,
		openAITokenProvider: NewOpenAITokenProvider(nil, nil, oauthService),
	}
	account := &Account{
		ID:          45,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token": "at-test-token",
			"auth_mode":    OpenAIAuthModePersonalAccessToken,
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.NoError(t, err)
	require.NotNil(t, result)
	require.Equal(t, int32(1), atomic.LoadInt32(&whoamiCalls))
	require.Equal(t, "acct-123", upstream.lastReq.Header.Get("ChatGPT-Account-ID"))
	require.Equal(t, "true", upstream.lastReq.Header.Get("X-OpenAI-Fedramp"))
	require.Equal(t, "acct-123", account.Credentials["chatgpt_account_id"])
	require.Equal(t, "user-123", account.Credentials["chatgpt_user_id"])
	require.Equal(t, OpenAIAuthModePersonalAccessToken, account.Credentials["auth_mode"])
}

func TestForwardAlphaSearchAPIKeyMapsModelAndPassesThroughError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/alpha/search", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")

	upstreamBody := `{"error":{"type":"invalid_request_error","message":"bad search"}}`
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusBadRequest,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(upstreamBody)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:       7,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"api_key":  "sk-test",
			"base_url": "https://compat.example/v4",
			"model_mapping": map[string]any{
				"gpt-5.6-sol": "upstream-5.6",
			},
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.NoError(t, err)
	// 上游错误透传不是一次成功的搜索：不返回 result、不产生按次计费。
	require.Nil(t, result)
	require.Equal(t, http.StatusBadRequest, recorder.Code)
	require.JSONEq(t, upstreamBody, recorder.Body.String())
	require.Equal(t, "https://compat.example/v4/alpha/search", upstream.lastReq.URL.String())
	require.Equal(t, "Bearer sk-test", upstream.lastReq.Header.Get("Authorization"))
	require.Equal(t, "upstream-5.6", gjson.GetBytes(upstream.lastBody, "model").String())
	require.True(t, gjson.GetBytes(upstream.lastBody, "commands.search_query").IsArray())
}

func TestForwardAlphaSearchReturnsFailoverBeforeWriting(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","commands":{}}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusTooManyRequests,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"error":{"message":"rate limited"}}`)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:       8,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"api_key": "sk-test",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.Equal(t, http.StatusTooManyRequests, failoverErr.StatusCode)
	require.Empty(t, failoverErr.Stage)
	require.Empty(t, failoverErr.Scope)
	require.Empty(t, failoverErr.Reason)
	require.Zero(t, failoverErr.ClientStatusCode)
	require.Empty(t, failoverErr.ClientMessage)
	require.Nil(t, failoverErr.ResponseHeaders)
	require.Equal(t, openAIPlatformAlphaSearchURL, upstream.lastReq.URL.String())
	require.False(t, c.Writer.Written())
	require.Empty(t, recorder.Body.String())
}

func TestForwardAlphaSearchSetupToken429CarriesSameAccountRetryWindow(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","commands":{}}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusTooManyRequests,
		Header: http.Header{
			"Content-Type": []string{"application/json"},
			"Retry-After":  []string{"1"},
			"X-Request-Id": []string{"req_alpha_oauth_429"},
		},
		Body: io.NopCloser(strings.NewReader(`{"error":{"type":"rate_limit_error","code":"rate_limit_exceeded","message":"rate limited"}}`)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          81,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeSetupToken,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}
	startedAt := time.Now()

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.True(t, failoverErr.RetryableOnSameAccount)
	require.Equal(t, time.Second, failoverErr.SameAccountRetryDelay)
	require.WithinDuration(t, startedAt.Add(openAIOAuth429RetryWindow), failoverErr.SameAccountRetryDeadline, time.Second)
	require.Equal(t, "req_alpha_oauth_429", failoverErr.ResponseHeaders.Get("x-request-id"))
	require.False(t, c.Writer.Written())
}

func TestForwardAlphaSearchAccessStateUsesTypedFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusForbidden,
		Header: http.Header{
			"Content-Type": []string{"application/json"},
			"X-Request-Id": []string{"req_alpha_access_state"},
		},
		Body: io.NopCloser(strings.NewReader(`{"error":{"code":"deactivated_workspace","message":"Workspace is deactivated"}}`)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          11,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	assertOpenAIAlphaSearchAccessStateFailover(t, err, "req_alpha_access_state")
	require.Equal(t, chatgptCodexAlphaSearchURL, upstream.lastReq.URL.String())
	require.False(t, c.Writer.Written())
}

func TestForwardAlphaSearchPATFallbackAccessStateUsesTypedFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusForbidden,
		Header: http.Header{
			"Content-Type": []string{"application/json"},
			"X-Request-Id": []string{"req_alpha_pat_access_state"},
		},
		Body: io.NopCloser(strings.NewReader(`{"error":{"code":"account_disabled","message":"Account is disabled"}}`)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          12,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "at-test-token",
			"auth_mode":          OpenAIAuthModePersonalAccessToken,
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	assertOpenAIAlphaSearchAccessStateFailover(t, err, "req_alpha_pat_access_state")
	require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
	require.False(t, c.Writer.Written())
}

func assertOpenAIAlphaSearchAccessStateFailover(t *testing.T, err error, requestID string) {
	t.Helper()
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.Equal(t, http.StatusForbidden, failoverErr.StatusCode)
	require.Equal(t, GatewayFailureStageAccountAuth, failoverErr.Stage)
	require.Equal(t, GatewayFailureScopeAccount, failoverErr.Scope)
	require.Equal(t, OpenAIUpstreamAccessStateReason, failoverErr.Reason)
	require.Equal(t, NextAccountRetry, failoverErr.NextAccountAction)
	require.Equal(t, http.StatusBadGateway, failoverErr.ClientStatusCode)
	require.Equal(t, openAIUpstreamAccessUnavailableClientMessage, failoverErr.ClientMessage)
	require.False(t, failoverErr.RetryableOnSameAccount)
	require.Equal(t, requestID, failoverErr.ResponseHeaders.Get("x-request-id"))
}

func TestForwardAlphaSearchUnauthorizedDoesNotMarkAccountError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusUnauthorized,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"detail":"Unauthorized"}`)),
	}}
	repo := &alphaSearchAccountStateRepo{}
	cfg := &config.Config{}
	service := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		accountRepo:      repo,
		rateLimitService: NewRateLimitService(repo, nil, cfg, nil, nil),
	}
	account := &Account{
		ID:          44,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			// 刻意不设置 auth_mode：覆盖历史上把 at- token 当普通 OAuth 导入的账号。
			"access_token":       "at-test-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.Equal(t, http.StatusUnauthorized, failoverErr.StatusCode)
	require.Zero(t, repo.setErrorCalls)
	require.Empty(t, repo.lastError)
	require.False(t, c.Writer.Written())
}

func TestForwardAlphaSearchPATResponsesFallbackUnauthorizedDoesNotMarkAccountError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusUnauthorized,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"detail":"Unauthorized"}`)),
	}}
	repo := &alphaSearchAccountStateRepo{}
	cfg := &config.Config{}
	service := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		accountRepo:      repo,
		rateLimitService: NewRateLimitService(repo, nil, cfg, nil, nil),
	}
	account := &Account{
		ID:          46,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "at-test-token",
			"auth_mode":          OpenAIAuthModePersonalAccessToken,
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.Equal(t, http.StatusUnauthorized, failoverErr.StatusCode)
	require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
	require.Equal(t, "text/event-stream", upstream.lastReq.Header.Get("Accept"))
	require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))
	require.Zero(t, repo.setErrorCalls)
	require.Empty(t, repo.lastError)
	require.False(t, c.Writer.Written())
}

// API key 上游（官方平台或第三方中转）不提供 /v1/alpha/search 时返回的
// 404/405 必须触发换号而不是把错误透传给客户端：混合分组里 OAuth 账号可以
// 承接搜索，请求不能死在先被选中的 API key 账号上。端点缺失也不能写账号
// 错误状态——账号本身是健康的。
func TestForwardAlphaSearchAPIKeyEndpointNotFoundFailsOver(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","commands":{"search_query":[{"q":"news"}]}}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusNotFound,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"error":{"message":"Not Found"}}`)),
	}}
	repo := &alphaSearchAccountStateRepo{}
	cfg := &config.Config{}
	service := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		accountRepo:      repo,
		rateLimitService: NewRateLimitService(repo, nil, cfg, nil, nil),
	}
	account := &Account{
		ID:       9,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"api_key":  "sk-test",
			"base_url": "https://relay.example",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.Nil(t, result)
	var failoverErr *UpstreamFailoverError
	require.ErrorAs(t, err, &failoverErr)
	require.Equal(t, http.StatusNotFound, failoverErr.StatusCode)
	require.Zero(t, repo.setErrorCalls)
	require.Empty(t, repo.lastError)
	require.False(t, c.Writer.Written())
	require.Empty(t, recorder.Body.String())
}

// OAuth 账号的 chatgpt.com 端点固定存在，404 保持原有透传行为不变。
func TestForwardAlphaSearchOAuthNotFoundPassesThrough(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"id":"search-session","model":"gpt-5.6-sol","input":[],"commands":{"search_query":[{"q":"news"}]},"settings":{},"max_output_tokens":2000}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/alpha/search", bytes.NewReader(body))

	upstreamBody := `{"detail":"Not Found"}`
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusNotFound,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(upstreamBody)),
	}}
	service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{
		ID:          10,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-account",
		},
	}

	result, err := service.ForwardAlphaSearch(context.Background(), c, account, body)

	require.NoError(t, err)
	require.Nil(t, result)
	require.Equal(t, http.StatusNotFound, recorder.Code)
	require.JSONEq(t, upstreamBody, recorder.Body.String())
}

func TestShouldApplyOpenAIAlphaSearchAccountErrorSideEffects(t *testing.T) {
	require.False(t, shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(http.StatusUnauthorized))
	require.False(t, shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(http.StatusNotFound))
	require.False(t, shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(http.StatusMethodNotAllowed))
	require.True(t, shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(http.StatusForbidden))
	require.True(t, shouldApplyOpenAIAlphaSearchAccountErrorSideEffects(http.StatusTooManyRequests))
}

func TestOpenAIAlphaSearchSchedulingModelUsesCanonicalAccountMapping(t *testing.T) {
	account := &Account{Credentials: map[string]any{
		"model_mapping": map[string]any{"client-visible": "canonical-upstream"},
	}}
	require.Equal(t, "canonical-upstream", openAIAlphaSearchSchedulingModel(account, "client-visible"))
	require.Equal(t, "unmapped", openAIAlphaSearchSchedulingModel(account, "unmapped"))
}

func TestSanitizeOpenAIAlphaSearchBody_RemovesResponsesOnlyFields(t *testing.T) {
	body := []byte(`{"id":"search-session","store":false,"prompt_cache_key":"cache","commands":{"search_query":[{"q":"news"}]}}`)

	normalized, err := sanitizeOpenAIAPIKeyAlphaSearchBody(body)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(normalized, "store").Exists())
	require.False(t, gjson.GetBytes(normalized, "prompt_cache_key").Exists())
	require.Equal(t, "news", gjson.GetBytes(normalized, "commands.search_query.0.q").String())
}

func TestIsOpenAIAlphaSearchEndpointUnsupported(t *testing.T) {
	apiKey := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey}
	oauth := &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}

	require.True(t, isOpenAIAlphaSearchEndpointUnsupported(apiKey, http.StatusNotFound))
	require.True(t, isOpenAIAlphaSearchEndpointUnsupported(apiKey, http.StatusMethodNotAllowed))
	require.False(t, isOpenAIAlphaSearchEndpointUnsupported(apiKey, http.StatusBadRequest))
	require.False(t, isOpenAIAlphaSearchEndpointUnsupported(oauth, http.StatusNotFound))
	require.False(t, isOpenAIAlphaSearchEndpointUnsupported(nil, http.StatusNotFound))
}

// 官方 alpha/search 的线序（clean-search-20260728T132311Z 实测）为
//
//	version, x-codex-turn-metadata, authorization, chatgpt-account-id,
//	content-type, accept, originator, user-agent, cookie, host, content-length
//
// 其中**没有** session-id / thread-id——SearchClient 的 search_request_headers()
// 只设 x-codex-turn-metadata 与 originator 两项（ext/web-search/src/tool.rs:185）。
// 规格表 SPEC-EP-015。
//
// 本测试锁住兜底剥离：account.ApplyHeaderOverrides 的白名单收了 session-id /
// thread-id，账号配置覆写就会把它们设进来，必须在出站前删掉。此前剥离列表只写了
// 下划线形式，Del 规范化后与连字符形式不是同一个键，防线形同虚设。
func TestStripOpenAIAlphaSearchResponsesHeadersRemovesSessionHeaders(t *testing.T) {
	headers := http.Header{}
	// 连字符形式：官方现行写法，也是账号覆写白名单里的键
	headers.Set("session-id", "sess-1")
	headers.Set("thread-id", "thread-1")
	// 下划线形式：历史写法，仍可能由旧配置带入
	headers.Set("Session_ID", "sess-2")
	headers.Set("Conversation_ID", "conv-2")
	// 其余 Responses 专用头
	headers.Set("OpenAI-Beta", "responses_websockets=2026-02-06")
	headers.Set("X-Codex-Beta-Features", "foo")
	headers.Set("X-Codex-Turn-State", "bar")
	// 官方 alpha/search 确实会发的两项，必须保留
	headers.Set("originator", "codex_exec")
	headers.Set("X-Codex-Turn-Metadata", "meta")

	stripOpenAIAlphaSearchResponsesHeaders(headers)

	for _, key := range []string{
		"session-id", "thread-id", "Session_ID", "Conversation_ID",
		"OpenAI-Beta", "X-Codex-Beta-Features", "X-Codex-Turn-State",
	} {
		require.Empty(t, headers.Get(key), "%s 必须被剥离：官方 alpha/search 不发该头", key)
	}
	require.Equal(t, "codex_exec", headers.Get("originator"), "originator 属官方默认客户端头，不得剥离")
	require.Equal(t, "meta", headers.Get("X-Codex-Turn-Metadata"), "x-codex-turn-metadata 是官方明确设置的两项之一")
}
