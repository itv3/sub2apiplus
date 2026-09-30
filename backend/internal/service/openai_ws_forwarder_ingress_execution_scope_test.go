package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func newOpenAIWSExecutionScopeTestConfig() *config.Config {
	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.APIKeyEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 2
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 2
	cfg.Gateway.OpenAIWS.QueueLimitPerConn = 8
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3
	return cfg
}

func dialOpenAIWSExecutionScopeClient(t *testing.T, wsURL, threadID string) *coderws.Conn {
	t.Helper()
	header := http.Header{}
	header.Set("session-id", "root-session")
	if threadID != "" {
		header.Set(openAIWSTurnMetadataHeader, `{"session_id":"root-session","thread_id":"`+threadID+`"}`)
	}
	dialCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsURL, "http"), &coderws.DialOptions{HTTPHeader: header})
	require.NoError(t, err)
	return conn
}

func writeOpenAIWSExecutionScopeRequest(t *testing.T, conn *coderws.Conn, body string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	require.NoError(t, conn.Write(ctx, coderws.MessageText, []byte(body)))
}

// 场景：codex 子智能体与父线程共用 session-id 头，只有 x-codex-turn-metadata 的 thread_id 不同。
// ctx_pool 下的 turn state 绑定与 store=false 的上游连接绑定必须落在执行作用域键下，
// 不能落在按 session-id 算出的会话哈希下，否则子智能体会覆盖父线程的绑定。
func TestOpenAIGatewayService_ProxyResponsesWebSocketFromClient_StateBoundToExecutionScope(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := newOpenAIWSExecutionScopeTestConfig()

	captureConn := &openAIWSCaptureConn{
		events: [][]byte{
			[]byte(`{"type":"response.completed","response":{"id":"resp_exec_scope","model":"gpt-5.1","usage":{"input_tokens":1,"output_tokens":1}}}`),
		},
	}
	handshake := http.Header{}
	handshake.Set(openAIWSTurnStateHeader, "turn-state-from-upstream")
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(&openAIWSCaptureDialer{conn: captureConn, handshake: handshake})
	defer pool.Close()

	stateStore := NewOpenAIWSStateStore(nil)
	svc := &OpenAIGatewayService{
		cfg:                cfg,
		httpUpstream:       &httpUpstreamRecorder{},
		cache:              &stubGatewayCache{},
		openaiWSResolver:   NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:      NewCodexToolCorrector(),
		openaiWSPool:       pool,
		openaiWSStateStore: stateStore,
	}
	groupID := int64(9)
	account := &Account{
		ID:          454,
		Name:        "openai-ingress-exec-scope",
		Platform:    PlatformOpenAI,
		Type:        AccountTypeAPIKey,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{"api_key": "sk-test"},
		Extra:       map[string]any{"responses_websockets_v2_enabled": true},
	}

	serverErrCh := make(chan error, 1)
	keysCh := make(chan [2]string, 1)
	wsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			serverErrCh <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		rec := httptest.NewRecorder()
		ginCtx, _ := gin.CreateTestContext(rec)
		req := r.Clone(r.Context())
		req.Header = req.Header.Clone()
		req.Header.Set("User-Agent", "unit-test-agent/1.0")
		ginCtx.Request = req
		ginCtx.Set("api_key", &APIKey{ID: 21, GroupID: &groupID})
		readCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			serverErrCh <- readErr
			return
		}
		scope, _ := resolveOpenAIWSExecutionScope(ginCtx, firstMessage, 21)
		keysCh <- [2]string{svc.GenerateSessionHash(ginCtx, firstMessage), scope}
		serverErrCh <- svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, account, "sk-test", firstMessage, nil)
	}))
	defer wsServer.Close()

	clientConn := dialOpenAIWSExecutionScopeClient(t, wsServer.URL, "child-thread")
	defer func() { _ = clientConn.CloseNow() }()
	writeOpenAIWSExecutionScopeRequest(t, clientConn, `{"type":"response.create","model":"gpt-5.1","stream":false,"store":false,"input":[{"role":"user","content":"first"}]}`)

	readCtx, cancelRead := context.WithTimeout(context.Background(), 3*time.Second)
	_, completed, readErr := clientConn.Read(readCtx)
	cancelRead()
	require.NoError(t, readErr)
	require.Equal(t, "resp_exec_scope", gjson.GetBytes(completed, "response.id").String())
	require.NoError(t, clientConn.Close(coderws.StatusNormalClosure, "done"))

	select {
	case serverErr := <-serverErrCh:
		require.NoError(t, serverErr)
	case <-time.After(5 * time.Second):
		t.Fatal("等待 ingress websocket 结束超时")
	}
	keys := <-keysCh
	legacyHash, scope := keys[0], keys[1]
	require.NotEmpty(t, legacyHash)
	require.Len(t, scope, 16)
	require.NotEqual(t, legacyHash, scope)

	_, connBoundToLegacy := stateStore.GetSessionConn(groupID, legacyHash)
	require.False(t, connBoundToLegacy, "上游连接不得绑定到按 session-id 算出的会话哈希")
	_, connBoundToScope := stateStore.GetSessionConn(groupID, scope)
	require.True(t, connBoundToScope, "上游连接应绑定到执行作用域")
	// 本分支 WS 路径只透传上游 turn state，不写入也不回填会话级状态（数据保真原则），
	// 因此两个键下都不应出现 turn state。
	_, turnStateOnLegacy := stateStore.GetSessionTurnState(groupID, legacyHash)
	require.False(t, turnStateOnLegacy, "turn state 不得绑定到会话哈希")
	_, turnStateOnScope := stateStore.GetSessionTurnState(groupID, scope)
	require.False(t, turnStateOnScope, "WS 路径不得把上游 turn state 写入会话级状态")
}
