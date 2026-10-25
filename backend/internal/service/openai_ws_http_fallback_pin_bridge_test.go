package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 端到端：桥接因协议转换拒绝 WebSocket 时，握手带会话头的会话被记下；没带会话头的不记。
func TestStatelessBridgeUnsupportedUpstreamRecordsHTTPFallbackPin(t *testing.T) {
	gin.SetMode(gin.TestMode)
	original := openAIWSHTTPFallbackPins
	store, _ := newPinTestStore(time.Hour, 10)
	openAIWSHTTPFallbackPins = store
	t.Cleanup(func() { openAIWSHTTPFallbackPins = original })

	account := &Account{
		ID: 9201, Platform: PlatformCustom, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 1,
		Credentials: map[string]any{
			"api_key": "k", "api_protocol": APIProtocolAdaptive,
			"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://custom.example.test/v1"},
		},
	}

	run := func(t *testing.T, header http.Header) {
		t.Helper()
		upstream := &httpUpstreamRecorder{}
		svc := newStatelessBridgeService(upstream)
		errCh := make(chan error, 1)
		wsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
			if err != nil {
				errCh <- err
				return
			}
			defer func() { _ = conn.CloseNow() }()
			readCtx, cancelRead := context.WithTimeout(r.Context(), 3*time.Second)
			_, firstMessage, err := conn.Read(readCtx)
			cancelRead()
			if err != nil {
				errCh <- err
				return
			}
			ginCtx, _ := gin.CreateTestContext(httptest.NewRecorder())
			ginCtx.Request = r.Clone(r.Context())
			ginCtx.Set("api_key", &APIKey{ID: 7301})
			errCh <- svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, account, "upstream-api-key", firstMessage, &OpenAIWSIngressHooks{})
		}))
		defer wsServer.Close()

		dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancelDial()
		client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), &coderws.DialOptions{HTTPHeader: header})
		require.NoError(t, err)
		defer func() { _ = client.CloseNow() }()
		require.NoError(t, client.Write(dialCtx, coderws.MessageText, []byte(`{"type":"response.create","model":"deepseek-flash","input":[`+userMessageItem("hi")+`]}`)))

		select {
		case proxyErr := <-errCh:
			require.ErrorIs(t, proxyErr, ErrOpenAIWSBridgeUnsupportedUpstream)
		case <-time.After(5 * time.Second):
			t.Fatal("代理没有返回")
		}
		require.Empty(t, upstream.bodies, "拒绝发生在访问上游之前")
	}

	t.Run("带会话头则记录", func(t *testing.T) {
		run(t, http.Header{"Session-Id": []string{"codex-thread-e2e"}})
		require.Equal(t, 1, store.size())
		require.True(t, store.pinned(openAIWSHTTPFallbackPinKeyOf(7301, "codex-thread-e2e")))
	})
	t.Run("不带会话头则不记录", func(t *testing.T) {
		before := store.size()
		run(t, nil)
		require.Equal(t, before, store.size())
	})
}
