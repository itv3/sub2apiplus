package service

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 通过真实客户端 WS 收两轮请求，确认首轮 owner 及时退役，后续 hook 仍拿到
// 隔离前原文。Setup Token 与 OAuth 共用身份隔离路径，又无需构造官方画像发布包。
func TestPassthroughPayloadLifecycleReleasesFirstFrameAndKeepsRawFollowup(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var reserved atomic.Int64
	memory := NewOpenAIWSRequestMemory(1<<20, 0, 1, func(weight int64) bool {
		reserved.Store(weight)
		return true
	})
	defer memory.Close()
	controlCtx, cancelControl := context.WithCancel(WithOpenAIWSRequestMemory(context.Background(), memory))
	defer cancelControl()
	upstream := newStagedPassthroughConn()
	cfg := passthroughLifecycleConfig()
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.IngressInterTurnIdleTimeoutSeconds = 3
	cfg.Gateway.OpenAIFirstOutputTimeoutSeconds = 3
	svc := newPassthroughLifecycleService(cfg, upstream)
	account := passthroughLifecycleAccount()
	account.Type = AccountTypeSetupToken
	account.Credentials = map[string]any{"access_token": "setup-token", "chatgpt_account_id": "identity-account"}
	account.Extra = map[string]any{"openai_oauth_responses_websockets_v2_mode": OpenAIWSIngressModePassthrough}
	first := `{"type":"response.create","model":"gpt-5.1","client_metadata":{"session_id":"raw-first"},"input":[{"role":"user","content":"` + strings.Repeat("a", 32<<10) + `"}]}`
	second := `{"type":"response.create","client_metadata":{"session_id":"raw-followup"},"input":[{"role":"user","content":"` + strings.Repeat("b", 32<<10) + `"}]}`
	completed := make(chan int, 2)
	hookBodies := make(chan string, 1)
	serverErr := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			serverErr <- err
			return
		}
		defer conn.CloseNow()
		conn.SetReadLimit(1 << 20)
		_, firstFrame, err := ReadOpenAIWSClientMessage(controlCtx, conn, 3*time.Second, coderws.StatusPolicyViolation, "missing first frame")
		if err != nil {
			serverErr <- err
			return
		}
		if err := memory.Retain("handler", firstFrame); err != nil {
			serverErr <- err
			return
		}
		var ownerMu sync.Mutex
		currentTurn := 1
		hooks := &OpenAIWSIngressHooks{
			InitialRequestModel: "gpt-5.1",
			BeforeRequest: func(turn int, body []byte, model string) error {
				if turn != 2 || model != "gpt-5.1" {
					return fmt.Errorf("后续轮次或模型错误：turn=%d model=%s", turn, model)
				}
				hookBodies <- string(body)
				ownerMu.Lock()
				defer ownerMu.Unlock()
				currentTurn = turn
				return memory.Retain("handler", body)
			},
			AfterTurn: func(turn int, _ *OpenAIForwardResult, turnErr error) {
				ownerMu.Lock()
				defer ownerMu.Unlock()
				if turnErr == nil && currentTurn == turn {
					_ = memory.Retain("handler")
					completed <- turn
				}
			},
		}
		ginCtx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ginCtx.Request = r.Clone(controlCtx)
		serverErr <- svc.ProxyResponsesWebSocketFromClient(controlCtx, ginCtx, conn, account, "setup-token", takeOpenAIWSClientPayload(&firstFrame), hooks)
	}))
	defer server.Close()
	client := dialPassthroughLifecycleClientWithPayload(t, server, first)
	defer client.CloseNow()

	for turn, source := range []string{first, second} {
		if turn == 1 {
			writeCtx, cancelWrite := context.WithTimeout(context.Background(), 3*time.Second)
			err := client.Write(writeCtx, coderws.MessageText, []byte(source))
			cancelWrite()
			require.NoError(t, err)
		}
		var wire []byte
		select {
		case wire = <-upstream.writes:
		case err := <-serverErr:
			t.Fatalf("等待第 %d 轮上游请求时服务已退出：%v", turn+1, err)
		case <-time.After(3 * time.Second):
			t.Fatalf("第 %d 轮请求未送达上游", turn+1)
		}
		rawSession := gjson.Get(source, "client_metadata.session_id").String()
		require.Equal(t, scopeCodexAccountIdentityValue(account, 0, "session", rawSession), gjson.GetBytes(wire, "client_metadata.session_id").String())
		require.Equal(t, gjson.Get(source, "input").Raw, gjson.GetBytes(wire, "input").Raw)
		upstream.Send(fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_owner_%d","model":"gpt-5.1","usage":{"input_tokens":1,"output_tokens":1}}}`, turn+1))
		event, err := readPassthroughLifecycleFrame(t, client, 3*time.Second)
		require.NoError(t, err)
		require.Equal(t, "response.completed", gjson.GetBytes(event, "type").String())
		select {
		case doneTurn := <-completed:
			require.Equal(t, turn+1, doneTurn)
		case <-time.After(3 * time.Second):
			t.Fatal("轮次完成回调未执行")
		}
		require.Eventually(t, func() bool { return reserved.Load() == 0 }, time.Second, time.Millisecond,
			"已完成轮次在等待下一帧时不应保留 handler/frame/passthrough 正文预算")
	}
	select {
	case original := <-hookBodies:
		require.Equal(t, second, original, "换号及审计 hook 必须收到尚未按当前账号隔离的完整原文")
	default:
		t.Fatal("未收到后续轮次原文")
	}
	require.NoError(t, client.Close(coderws.StatusNormalClosure, "done"))
	select {
	case err := <-serverErr:
		require.NoError(t, err)
	case <-time.After(3 * time.Second):
		t.Fatal("passthrough 连接未退出")
	}
}
