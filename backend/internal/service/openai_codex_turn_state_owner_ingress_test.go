package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// turn-state 按账号 owner 隔离的入口回归（SPEC-BODY-004：账号 owner 变化时清空 turn-state）。
//
// WS 入口只有 HTTP 桥接会把本连接请求头里的 x-codex-turn-state 原样作为回送值出站；
// ctx_pool 连接在取得上游连接后改用握手值，passthrough 不采纳客户端回带值。因此跨账号
// 泄漏的出口是 HTTP 桥接，来源有两处：
//   - 上游经流内 response.metadata 事件下发的 turn-state 会转发给下游客户端，客户端新建
//     连接回带该值并被调度到其他账号（ctx_pool 与 passthrough 两种下发形态）；
//   - HTTP 桥接轮次下发的 turn-state 会写回本连接请求头供下一轮回带，账号 failover 时
//     handler 复用同一 gin 上下文，新账号的首轮桥接请求读到的仍是旧账号的值。
// 两处下发都必须记入铸造账号，入口隔离丢弃跨账号值后还要同步移除请求头；同账号回带照常
// 保留，隔离按 owner 判定而不是一律丢弃。普通 HTTP 官方出站的 turn-state 只来自按账号
// 隔离的存储，客户端回带值不上行，已由 TestOpenAIGatewayService_OAuthHTTPReplaysUpstreamTurnStateOnlyWithinRetry
// 与 TestOfficialOpenAITurnStateScopeIsolatesEveryAuthorityDimension 锁定，这里不重复。

const turnStateOwnerIngressSessionID = "turn-state-owner-ingress-session"

// turnStateOwnerIngressFirstFrame 是首帧：官方出站 WS 需要 developer 上下文构造连接级 prewarm。
const turnStateOwnerIngressFirstFrame = `{"type":"response.create","model":"gpt-5.5","stream":false,"input":[` +
	`{"type":"message","role":"developer","content":[{"type":"input_text","text":"developer context"}]},` +
	`{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}]}`

func newTurnStateOwnerIngressConfig() *config.Config {
	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.APIKeyEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.ModeRouterV2Enabled = true
	cfg.Gateway.OpenAIWS.IngressModeDefault = OpenAIWSIngressModeCtxPool
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.QueueLimitPerConn = 8
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3
	return cfg
}

func newTurnStateOwnerIngressService(
	cfg *config.Config,
	httpUpstream *httpUpstreamRecorder,
	accountIDs ...int64,
) *OpenAIGatewayService {
	svc := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     httpUpstream,
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
	}
	for _, accountID := range accountIDs {
		svc.openaiModelCapabilities.replaceFromManifest(
			accountID,
			[]byte(`{"models":[{"slug":"gpt-5.5","use_responses_lite":true}]}`),
		)
	}
	return svc
}

func newTurnStateOwnerIngressAccount(accountID int64, mode string) *Account {
	return &Account{
		ID:          accountID,
		Name:        fmt.Sprintf("turn-state-owner-%d", accountID),
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "turn-state-owner-token",
			"chatgpt_account_id": fmt.Sprintf("chatgpt-turn-state-owner-%d", accountID),
		},
		Extra: map[string]any{"openai_oauth_responses_websockets_v2_mode": mode},
	}
}

// newTurnStateOwnerIngressGinContext 按 handler 的方式由下游升级请求构造 gin 上下文：
// 第三方入站、/v1/responses 端点、同一 API Key。
func newTurnStateOwnerIngressGinContext(r *http.Request, apiKey *APIKey) *gin.Context {
	ginCtx, _ := gin.CreateTestContext(httptest.NewRecorder())
	req := r.Clone(r.Context())
	req.Header = req.Header.Clone()
	req.Header.Set("User-Agent", "kilo-code/1.0")
	req.URL.Path = "/v1/responses"
	ginCtx.Request = req
	ginCtx.Set("api_key", apiKey)
	return ginCtx
}

// readTurnStateOwnerIngressTurn 读下游消息直到本轮 response.completed。
func readTurnStateOwnerIngressTurn(t *testing.T, conn *coderws.Conn) [][]byte {
	t.Helper()
	messages := make([][]byte, 0, 2)
	for {
		readCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		_, message, err := conn.Read(readCtx)
		cancel()
		require.NoError(t, err)
		messages = append(messages, message)
		if gjson.GetBytes(message, "type").String() == "response.completed" {
			return messages
		}
	}
}

// runTurnStateOwnerIngressConnection 建立一条下游 WS 连接跑单轮：握手携带同一 session-id，
// clientTurnState 非空时模拟客户端回带；返回下游收到的本轮消息。
func runTurnStateOwnerIngressConnection(
	t *testing.T,
	svc *OpenAIGatewayService,
	account *Account,
	apiKey *APIKey,
	clientTurnState string,
) [][]byte {
	t.Helper()
	serverErr := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, nil)
		if err != nil {
			serverErr <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		ginCtx := newTurnStateOwnerIngressGinContext(r, apiKey)
		readCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			serverErr <- readErr
			return
		}
		serverErr <- svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, account, "turn-state-owner-token", firstMessage, nil)
	}))
	defer server.Close()

	headers := http.Header{}
	headers.Set("session-id", turnStateOwnerIngressSessionID)
	if clientTurnState != "" {
		headers.Set(openAIWSTurnStateHeader, clientTurnState)
	}
	dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{HTTPHeader: headers})
	cancelDial()
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()
	writeCtx, cancelWrite := context.WithTimeout(context.Background(), 3*time.Second)
	require.NoError(t, client.Write(writeCtx, coderws.MessageText, []byte(turnStateOwnerIngressFirstFrame)))
	cancelWrite()
	messages := readTurnStateOwnerIngressTurn(t, client)
	require.NoError(t, client.Close(coderws.StatusNormalClosure, "done"))
	select {
	case err := <-serverErr:
		if err != nil {
			require.Contains(t, err.Error(), "StatusNormalClosure")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("等待 WS 入口结束超时")
	}
	return messages
}

func newTurnStateOwnerIngressSSE(responseID string, turnState string) *http.Response {
	response := newOfficialOpenAIHTTPSSECompletedResponse(responseID)
	if turnState != "" {
		response.Header.Set(openAIWSTurnStateHeader, turnState)
	}
	return response
}

// TestOfficialCodexTurnStateOwnerIsolatesEventStreamValueOnNewConnection 复现并锁定：上游经流内
// response.metadata 事件下发、已转发给下游的 turn-state，客户端新建连接回带并被调度到其他
// 账号时，不得随 HTTP 桥接请求出站；同账号回带照常保留。
func TestOfficialCodexTurnStateOwnerIsolatesEventStreamValueOnNewConnection(t *testing.T) {
	for _, deliveringMode := range []string{OpenAIWSIngressModeCtxPool, OpenAIWSIngressModePassthrough} {
		t.Run(deliveringMode, func(t *testing.T) {
			gin.SetMode(gin.TestMode)
			withOfficialCodexSyntheticProfile(t, turnStateTargetMutation(t))
			require.True(t, officialCodexTurnStateOwnerIsolation(officialClientProfileModeActive))

			minted := "turn-state-" + uuid.NewString()
			upstreamConn := &openAIWSCaptureConn{events: [][]byte{
				[]byte(`{"type":"response.completed","response":{"id":"resp_owner_a_prewarm","model":"gpt-5.5","usage":{"input_tokens":1,"output_tokens":0}}}`),
				[]byte(`{"type":"response.metadata","headers":{"x-codex-turn-state":"` + minted + `"}}`),
				[]byte(`{"type":"response.completed","response":{"id":"resp_owner_a_turn","model":"gpt-5.5","usage":{"input_tokens":1,"output_tokens":1}}}`),
			}}
			dialer := &openAIWSCaptureDialer{conn: upstreamConn}
			httpUpstream := &httpUpstreamRecorder{responses: []*http.Response{
				newTurnStateOwnerIngressSSE("resp_owner_b_bridge", ""),
				newTurnStateOwnerIngressSSE("resp_owner_a_bridge", ""),
			}}
			cfg := newTurnStateOwnerIngressConfig()
			ownerA := newTurnStateOwnerIngressAccount(15731, deliveringMode)
			ownerB := newTurnStateOwnerIngressAccount(15732, OpenAIWSIngressModeHTTPBridge)
			svc := newTurnStateOwnerIngressService(cfg, httpUpstream, ownerA.ID, ownerB.ID)
			if deliveringMode == OpenAIWSIngressModePassthrough {
				svc.openaiWSPassthroughDialer = dialer
			} else {
				pool := newOpenAIWSConnPool(cfg)
				pool.setClientDialerForTest(dialer)
				svc.openaiWSPool = pool
			}
			apiKey := &APIKey{ID: 15730, UserID: 1}

			// 账号 A：上游经事件流下发 turn-state，下游客户端收到转发的 response.metadata。
			delivered := ""
			for _, message := range runTurnStateOwnerIngressConnection(t, svc, ownerA, apiKey, "") {
				if gjson.GetBytes(message, "type").String() == "response.metadata" {
					delivered = gjson.GetBytes(message, "headers.x-codex-turn-state").String()
				}
			}
			require.Equal(t, minted, delivered, "下游客户端必须能看到事件流下发的 turn-state")

			// 客户端新建连接回带该值，被调度到账号 B（HTTP 桥接）：旧 owner 铸造的值不得出站。
			runTurnStateOwnerIngressConnection(t, svc, ownerB, apiKey, minted)
			require.Len(t, httpUpstream.requests, 1)
			require.Empty(t, httpUpstream.requests[0].Header.Values(openAIWSTurnStateHeader),
				"账号 owner 变化后，事件流下发的旧 turn-state 不得随新账号的桥接请求出站")

			// 对照：同账号新建连接回带照常保留，隔离按 owner 判定而不是一律丢弃。
			sameOwnerBridge := newTurnStateOwnerIngressAccount(ownerA.ID, OpenAIWSIngressModeHTTPBridge)
			runTurnStateOwnerIngressConnection(t, svc, sameOwnerBridge, apiKey, minted)
			require.Len(t, httpUpstream.requests, 2)
			require.Equal(t, minted, httpUpstream.requests[1].Header.Get(openAIWSTurnStateHeader))
		})
	}
}

// TestOfficialCodexTurnStateOwnerIsolatesHTTPBridgeValueAcrossAccountFailover 复现并锁定：HTTP 桥接
// 轮次下发的 turn-state 写回本连接请求头后，handler 在后续轮次 failover 换号并复用同一 gin
// 上下文时，新账号的桥接请求不得携带旧账号铸造的值。
func TestOfficialCodexTurnStateOwnerIsolatesHTTPBridgeValueAcrossAccountFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	withOfficialCodexSyntheticProfile(t, turnStateTargetMutation(t))
	require.True(t, officialCodexTurnStateOwnerIsolation(officialClientProfileModeActive))

	minted := "turn-state-" + uuid.NewString()
	httpUpstream := &httpUpstreamRecorder{responses: []*http.Response{
		newTurnStateOwnerIngressSSE("resp_owner_a_first", minted),
		{
			StatusCode: http.StatusTooManyRequests,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"error":{"type":"rate_limit_error","message":"rate limited"}}`)),
		},
		newTurnStateOwnerIngressSSE("resp_owner_b_retry", ""),
	}}
	cfg := newTurnStateOwnerIngressConfig()
	ownerA := newTurnStateOwnerIngressAccount(15741, OpenAIWSIngressModeHTTPBridge)
	ownerB := newTurnStateOwnerIngressAccount(15742, OpenAIWSIngressModeHTTPBridge)
	svc := newTurnStateOwnerIngressService(cfg, httpUpstream, ownerA.ID, ownerB.ID)
	apiKey := &APIKey{ID: 15740, UserID: 1}
	secondFrame := `{"type":"response.create","model":"gpt-5.5","stream":false,"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"second"}]}]}`

	serverErr := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, nil)
		if err != nil {
			serverErr <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		// 与 handler 一致：同一下游连接的各次账号尝试复用同一个 gin 上下文。
		ginCtx := newTurnStateOwnerIngressGinContext(r, apiKey)
		readCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			serverErr <- readErr
			return
		}
		attemptErr := svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, ownerA, "turn-state-owner-token", firstMessage, nil)
		var failoverErr *UpstreamFailoverError
		if !errors.As(attemptErr, &failoverErr) {
			serverErr <- fmt.Errorf("第二轮 429 应触发账号 failover，实际：%w", attemptErr)
			return
		}
		retryPayload, retryCurrentTurn := OpenAIWSCurrentTurnRetryPayload(attemptErr)
		if !retryCurrentTurn {
			retryPayload = []byte(secondFrame)
		}
		serverErr <- svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, ownerB, "turn-state-owner-token", retryPayload, nil)
	}))
	defer server.Close()

	headers := http.Header{}
	headers.Set("session-id", turnStateOwnerIngressSessionID)
	dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{HTTPHeader: headers})
	cancelDial()
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()

	writeCtx, cancelWrite := context.WithTimeout(context.Background(), 3*time.Second)
	require.NoError(t, client.Write(writeCtx, coderws.MessageText, []byte(turnStateOwnerIngressFirstFrame)))
	cancelWrite()
	readTurnStateOwnerIngressTurn(t, client)
	writeCtx, cancelWrite = context.WithTimeout(context.Background(), 3*time.Second)
	require.NoError(t, client.Write(writeCtx, coderws.MessageText, []byte(secondFrame)))
	cancelWrite()
	completed := readTurnStateOwnerIngressTurn(t, client)
	require.Equal(t, "resp_owner_b_retry", gjson.GetBytes(completed[len(completed)-1], "response.id").String())
	require.NoError(t, client.Close(coderws.StatusNormalClosure, "done"))
	select {
	case err := <-serverErr:
		if err != nil {
			require.Contains(t, err.Error(), "StatusNormalClosure")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("等待 WS 入口结束超时")
	}

	require.Len(t, httpUpstream.requests, 3)
	require.Empty(t, httpUpstream.requests[0].Header.Values(openAIWSTurnStateHeader))
	require.Equal(t, minted, httpUpstream.requests[1].Header.Get(openAIWSTurnStateHeader),
		"同账号下一轮按桥接下发值回带")
	require.Empty(t, httpUpstream.requests[2].Header.Values(openAIWSTurnStateHeader),
		"failover 换号后，旧账号桥接下发的 turn-state 不得随新账号请求出站")
}
