package handler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func handlerRequestBodyStorageErrors() map[string]error {
	return map[string]error{
		"inbound": &pkghttputil.RequestBodyStorageError{Err: errors.New("private-storage-path: disk full")},
		"outbound": fmt.Errorf("send body: %w", &officialegress.RequestBodyStorageError{
			Operation: "read", Err: errors.New("private-storage-path: disk full"),
		}),
	}
}

type openAIStorageFailureUpstream struct {
	service.HTTPUpstream
	mu       sync.Mutex
	err      error
	failTurn int
	calls    int
}

func (u *openAIStorageFailureUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	if req.Body != nil {
		_, _ = io.Copy(io.Discard, req.Body)
		_ = req.Body.Close()
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.calls++
	if u.calls >= u.failTurn {
		return nil, u.err
	}
	return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
		Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_first\",\"model\":\"gpt-5.1\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n"))}, nil
}

func (u *openAIStorageFailureUpstream) callCount() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.calls
}

func newOpenAIStorageFailureHandler(t *testing.T, upstream *openAIStorageFailureUpstream) (*OpenAIGatewayHandler, *gin.Engine) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	groupID := int64(921)
	accounts := []service.Account{}
	for i := int64(1); i <= 2; i++ {
		accounts = append(accounts, service.Account{
			ID: 9100 + i, Name: fmt.Sprintf("healthy-%d", i), Platform: service.PlatformOpenAI,
			Type: service.AccountTypeAPIKey, Status: service.StatusActive, Schedulable: true, Priority: int(i), Concurrency: 1,
			Credentials: map[string]any{"api_key": "sk-test", "base_url": "https://api.example.test"},
			Extra:       map[string]any{"openai_passthrough": true, "openai_apikey_responses_websockets_v2_mode": service.OpenAIWSIngressModeHTTPBridge},
		})
	}
	cfg := &config.Config{RunMode: config.RunModeSimple}
	cfg.Default.RateMultiplier = 1
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Gateway.MaxAccountSwitches = 1
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.APIKeyEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.ModeRouterV2Enabled = true
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.IngressInterTurnIdleTimeoutSeconds = 3
	repo := &openAIWSFailoverHandlerAccountRepoStub{accounts: accounts}
	billing := service.NewBillingCacheService(nil, nil, nil, nil, nil, nil, cfg, nil)
	t.Cleanup(billing.Stop)
	gateway := service.NewOpenAIGatewayService(repo, nil, nil, nil, nil, nil, nil, cfg, nil, nil,
		service.NewBillingService(cfg, nil), nil, billing, upstream, &service.DeferredService{},
		nil, nil, nil, nil, nil, nil, nil, nil)
	concurrencyCache := &concurrencyCacheMock{
		acquireUserSlotFn:    func(context.Context, int64, int, string) (bool, error) { return true, nil },
		acquireAccountSlotFn: func(context.Context, int64, int, string) (bool, error) { return true, nil },
	}
	h := NewOpenAIGatewayHandler(gateway, service.NewConcurrencyService(concurrencyCache), billing,
		service.NewAPIKeyService(nil, nil, nil, nil, nil, nil, cfg), nil, nil, nil, nil, cfg)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{
			ID: 981, GroupID: &groupID, User: &service.User{ID: 982, Status: service.StatusActive},
			Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI, Status: service.StatusActive},
		})
		c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 982, Concurrency: 1})
		c.Next()
	})
	router.POST("/v1/responses", h.Responses)
	router.GET("/v1/responses", h.ResponsesWebSocket)
	return h, router
}

func TestResponsesOutboundStorageFailureReturns503WithoutFailover(t *testing.T) {
	for name, localErr := range handlerRequestBodyStorageErrors() {
		t.Run(name, func(t *testing.T) {
			upstream := &openAIStorageFailureUpstream{err: localErr, failTurn: 1}
			_, router := newOpenAIStorageFailureHandler(t, upstream)
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"gpt-5.1","input":"hello","stream":false}`))
			req.Header.Set("Content-Type", "application/json")
			router.ServeHTTP(rec, req)
			require.Equal(t, http.StatusServiceUnavailable, rec.Code, rec.Body.String())
			require.Equal(t, "1", rec.Header().Get("Retry-After"))
			require.Equal(t, "service_unavailable", gjson.GetBytes(rec.Body.Bytes(), "error.type").String())
			require.NotContains(t, rec.Body.String(), "private-storage-path")
			require.Equal(t, 1, upstream.callCount(), "两账号可用时也不能因本地错误重试或换号")
		})
	}
}

func TestRequestBodyStorageFailureAfterStreamUsesProtocolError(t *testing.T) {
	for _, anthropic := range []bool{false, true} {
		t.Run(fmt.Sprintf("anthropic=%v", anthropic), func(t *testing.T) {
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Header("Content-Type", "text/event-stream")
			c.Writer.WriteHeader(http.StatusOK)
			c.Writer.Flush()
			h := &OpenAIGatewayHandler{}
			err := handlerRequestBodyStorageErrors()["outbound"]
			require.True(t, h.handleRequestBodyStorageFailure(c, err, false, anthropic))
			require.Equal(t, http.StatusOK, rec.Code, "流已开始时不能改写已发送的 HTTP 状态")
			require.Contains(t, rec.Body.String(), "storage temporarily unavailable")
			if anthropic {
				require.Contains(t, rec.Body.String(), "event: error")
			} else {
				require.Contains(t, rec.Body.String(), "response.failed")
			}
			require.NotContains(t, rec.Body.String(), "private-storage-path")
		})
	}
}

func TestWebSocketStorageFailureCloses1013WithoutFailover(t *testing.T) {
	for name, localErr := range handlerRequestBodyStorageErrors() {
		for _, failTurn := range []int{1, 2} {
			t.Run(fmt.Sprintf("%s/turn=%d", name, failTurn), func(t *testing.T) {
				upstream := &openAIStorageFailureUpstream{err: localErr, failTurn: failTurn}
				_, router := newOpenAIStorageFailureHandler(t, upstream)
				server := httptest.NewServer(router)
				defer server.Close()
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				client, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http")+"/v1/responses", nil)
				require.NoError(t, err)
				defer func() { _ = client.CloseNow() }()
				for turn := 1; turn <= failTurn; turn++ {
					err = client.Write(ctx, coderws.MessageText, []byte(`{"type":"response.create","model":"gpt-5.1","input":"hello"}`))
					require.NoError(t, err)
					_, frame, readErr := client.Read(ctx)
					if turn < failTurn {
						require.NoError(t, readErr)
						require.Equal(t, "response.completed", gjson.GetBytes(frame, "type").String())
						continue
					}
					require.Equal(t, coderws.StatusTryAgainLater, coderws.CloseStatus(readErr), "%v", readErr)
					require.NotContains(t, readErr.Error(), "private-storage-path")
				}
				require.Equal(t, failTurn, upstream.callCount(), "本地错误只关闭客户端连接，不新增上游 attempt")
			})
		}
	}
}

func TestStorageFailureNeverPenalizesWSAccount(t *testing.T) {
	for _, err := range handlerRequestBodyStorageErrors() {
		require.False(t, shouldReportOpenAIWSProxyAccountFailure(err))
		wrapped := service.NewOpenAIWSClientCloseError(coderws.StatusTryAgainLater, "retry", fmt.Errorf("bridge: %w", err))
		require.False(t, shouldReportOpenAIWSProxyAccountFailure(wrapped))
	}
	require.True(t, shouldReportOpenAIWSProxyAccountFailure(errors.New("upstream connection refused")))
}
