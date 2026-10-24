package service

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
	"github.com/Wei-Shaw/sub2api/internal/pkg/xai"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ---------- 谓词与小工具 ----------

func TestOpenAIPlatformUsesWSHTTPBridge(t *testing.T) {
	// 上游不提供 Responses WebSocket 的 OpenAI 网关族平台：全部必须走桥接。
	bridged := []string{
		PlatformGrok, PlatformKimi, PlatformZhipu, PlatformDeepseek, PlatformMiniMax,
		PlatformOpenCodeGo, PlatformCommandCode, PlatformCline, PlatformCustom,
	}
	for _, platform := range bridged {
		require.True(t, OpenAIPlatformUsesWSHTTPBridge(platform), "platform=%s", platform)
		// 只有 Grok 保持它自己的专属桥接；其余是本次新增的「无状态桥接平台」。
		require.Equal(t, platform != PlatformGrok, OpenAIPlatformIsStatelessWSHTTPBridge(platform), "platform=%s", platform)
	}
	// openai 平台沿用上游 WebSocket；Anthropic 网关族不走 OpenAI 网关；空值与未知值不桥接。
	for _, platform := range []string{
		PlatformOpenAI, PlatformAnthropic, PlatformGemini, PlatformAntigravity, PlatformTypeSafe,
		PlatformComposite, "", "unknown-platform",
	} {
		require.False(t, OpenAIPlatformUsesWSHTTPBridge(platform), "platform=%s", platform)
		require.False(t, OpenAIPlatformIsStatelessWSHTTPBridge(platform), "platform=%s", platform)
	}
	require.False(t, isOpenAIWSStatelessBridgeAccount(nil))
	require.True(t, isOpenAIWSStatelessBridgeAccount(&Account{Platform: PlatformCommandCode}))
	require.False(t, isOpenAIWSStatelessBridgeAccount(&Account{Platform: PlatformGrok}))
	require.False(t, isOpenAIWSStatelessBridgeAccount(&Account{Platform: PlatformOpenAI}))
}

// 桥接决策：新增平台无论帧大小、有无 previous_response_id 都桥接；
// openai 平台保持「开关 + 阈值 + 无 previous_response_id」的原有判定，一行不变。
func TestOpenAIWSBridgeDecisionCoversStatelessPlatformsAndKeepsOpenAI(t *testing.T) {
	disabled := &OpenAIGatewayService{cfg: &config.Config{}}
	enabled := &OpenAIGatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{OpenAIWS: config.GatewayOpenAIWSConfig{
		HTTPBridgeEnabled: true, HTTPBridgeThresholdBytes: 100,
	}}}}
	smallFrame := []byte(`{"type":"response.create","model":"m","input":"hi"}`)

	for _, platform := range []string{PlatformGrok, PlatformDeepseek, PlatformCommandCode, PlatformCustom, PlatformKimi} {
		account := &Account{Platform: platform}
		for _, svc := range []*OpenAIGatewayService{disabled, enabled} {
			require.True(t, svc.shouldBridgeOpenAIWSHTTP(account, 1, ""), "platform=%s", platform)
			require.True(t, svc.shouldBridgeOpenAIWSHTTP(account, 1, "resp_1"), "platform=%s", platform)
			require.True(t, svc.shouldBridgeOpenAIWSPassthroughFirstMessage(account, smallFrame), "platform=%s", platform)
		}
	}

	openAIAccount := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey}
	require.False(t, disabled.shouldBridgeOpenAIWSHTTP(openAIAccount, 1<<30, ""))
	require.False(t, enabled.shouldBridgeOpenAIWSHTTP(openAIAccount, 1, ""))
	require.True(t, enabled.shouldBridgeOpenAIWSHTTP(openAIAccount, 100, ""))
	require.False(t, enabled.shouldBridgeOpenAIWSHTTP(openAIAccount, 100, "resp_1"))
	require.False(t, disabled.shouldBridgeOpenAIWSPassthroughFirstMessage(openAIAccount, smallFrame))
	require.False(t, enabled.shouldBridgeOpenAIWSPassthroughFirstMessage(openAIAccount, smallFrame))
}

func TestIsOpenAIWSGenerateFalseFrame(t *testing.T) {
	require.True(t, isOpenAIWSGenerateFalseFrame([]byte(`{"type":"response.create","generate":false}`)))
	require.False(t, isOpenAIWSGenerateFalseFrame([]byte(`{"type":"response.create","generate":true}`)))
	require.False(t, isOpenAIWSGenerateFalseFrame([]byte(`{"type":"response.create"}`)))
	require.False(t, isOpenAIWSGenerateFalseFrame([]byte(`{"type":"response.create","generate":null}`)))
	require.False(t, isOpenAIWSGenerateFalseFrame([]byte(`{"type":"response.create","generate":"false"}`)))
	require.False(t, isOpenAIWSGenerateFalseFrame(nil))
}

func TestOpenAIWSStatelessBridgeContinuationBroken(t *testing.T) {
	available := openAIWSReplayInputState{exists: true}
	unavailable := unavailableOpenAIWSReplayInputState()

	// 完整请求（没有 previous_response_id）永远不算断裂，即使历史不可用。
	require.False(t, openAIWSStatelessBridgeContinuationBroken("", "", available))
	require.False(t, openAIWSStatelessBridgeContinuationBroken("  ", "resp_1", unavailable))
	// previous_response_id 恰好是上一轮的响应且历史可用：可以续链。
	require.False(t, openAIWSStatelessBridgeContinuationBroken("resp_1", "resp_1", available))
	require.False(t, openAIWSStatelessBridgeContinuationBroken(" resp_1 ", "resp_1", openAIWSReplayInputState{}))
	// 新连接首帧（还没有上一轮）、指向更早的响应、上一轮没有响应 ID、历史超限：全部断裂。
	require.True(t, openAIWSStatelessBridgeContinuationBroken("resp_1", "", available))
	require.True(t, openAIWSStatelessBridgeContinuationBroken("resp_0", "resp_1", available))
	require.True(t, openAIWSStatelessBridgeContinuationBroken("resp_1", "resp_1", unavailable))
}

func TestBuildOpenAIWSBridgePreviousResponseNotFoundEvent(t *testing.T) {
	event := buildOpenAIWSBridgePreviousResponseNotFoundEvent("resp_lost")
	require.Equal(t, "error", gjson.GetBytes(event, "type").String())
	// Codex 先看 error.code 把它映射成可重试错误，重试时改发完整 input。
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(event, "error.code").String())
	require.Equal(t, int64(400), gjson.GetBytes(event, "status").Int())
	require.Equal(t, "previous_response_id", gjson.GetBytes(event, "error.param").String())
	require.Contains(t, gjson.GetBytes(event, "error.message").String(), "resp_lost")

	// 没有 ID 时（工具输出续链无法还原）仍是同一个错误码。
	empty := buildOpenAIWSBridgePreviousResponseNotFoundEvent("")
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(empty, "error.code").String())
	require.NotContains(t, gjson.GetBytes(empty, "error.message").String(), "''")
}

func TestBuildOpenAIWSBridgeWarmupEvents(t *testing.T) {
	events, err := buildOpenAIWSBridgeWarmupEvents("resp_bridge_warmup_x", "deepseek-flash", 1700000000)
	require.NoError(t, err)
	require.Len(t, events, 2)

	require.Equal(t, "response.created", gjson.GetBytes(events[0], "type").String())
	require.Equal(t, "resp_bridge_warmup_x", gjson.GetBytes(events[0], "response.id").String())
	require.Equal(t, "in_progress", gjson.GetBytes(events[0], "response.status").String())

	// Codex 预热流程只等 response.completed，并取 response.id 作为之后的 previous_response_id；
	// usage 的三个计数字段是必填项。
	require.Equal(t, "response.completed", gjson.GetBytes(events[1], "type").String())
	require.Equal(t, "resp_bridge_warmup_x", gjson.GetBytes(events[1], "response.id").String())
	require.Equal(t, "completed", gjson.GetBytes(events[1], "response.status").String())
	require.Equal(t, "deepseek-flash", gjson.GetBytes(events[1], "response.model").String())
	require.Equal(t, int64(1700000000), gjson.GetBytes(events[1], "response.created_at").Int())
	require.True(t, gjson.GetBytes(events[1], "response.output").IsArray())
	require.Empty(t, gjson.GetBytes(events[1], "response.output").Array())
	for _, field := range []string{"input_tokens", "output_tokens", "total_tokens"} {
		usage := gjson.GetBytes(events[1], "response.usage."+field)
		require.True(t, usage.Exists(), field)
		require.Zero(t, usage.Int(), field)
	}
}

func TestOpenAIWSHTTPBridgeLogHost(t *testing.T) {
	require.Equal(t, "http-bridge", openAIWSHTTPBridgeLogHost(&Account{Platform: PlatformCommandCode}))
	require.Equal(t, "xai-http-bridge", openAIWSHTTPBridgeLogHost(&Account{Platform: PlatformGrok}))
	require.Equal(t, "xai-http-bridge", openAIWSHTTPBridgeLogHost(&Account{Platform: PlatformOpenAI}))
}

// ---------- 端到端：客户端 WS → 无状态上游 HTTP ----------

// statelessBridgeAfterTurn 是 AfterTurn 钩子收到的一次回调。
type statelessBridgeAfterTurn struct {
	turn   int
	result *OpenAIForwardResult
	err    error
}

// statelessBridgeSession 在真实 WebSocket 上驱动 ProxyResponsesWebSocketFromClient：
// 客户端写帧、读事件；上游是按顺序应答的 HTTP 记录器。
type statelessBridgeSession struct {
	t          *testing.T
	svc        *OpenAIGatewayService
	upstream   *httpUpstreamRecorder
	client     *coderws.Conn
	errCh      chan error
	mu         sync.Mutex
	afterTurns []statelessBridgeAfterTurn
}

func newStatelessBridgeService(upstream *httpUpstreamRecorder) *OpenAIGatewayService {
	cfg := &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}}
	cfg.Security.URLAllowlist.Enabled = false
	return &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream, cache: &stubGatewayCache{}}
}

func startStatelessBridgeSession(t *testing.T, account *Account, responses ...*http.Response) *statelessBridgeSession {
	t.Helper()
	gin.SetMode(gin.TestMode)

	upstream := &httpUpstreamRecorder{responses: responses}
	session := &statelessBridgeSession{t: t, upstream: upstream, svc: newStatelessBridgeService(upstream), errCh: make(chan error, 1)}

	wsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			session.errCh <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		// 与处理器一致放宽读上限，否则大帧会被库默认的 32KiB 上限截断连接。
		conn.SetReadLimit(16 << 20)

		readCtx, cancelRead := context.WithTimeout(r.Context(), 3*time.Second)
		_, firstMessage, err := conn.Read(readCtx)
		cancelRead()
		if err != nil {
			session.errCh <- err
			return
		}
		ginCtx, _ := gin.CreateTestContext(httptest.NewRecorder())
		req := r.Clone(r.Context())
		req.Header = req.Header.Clone()
		ginCtx.Request = req
		ginCtx.Set("api_key", &APIKey{ID: 7201})
		session.errCh <- session.svc.ProxyResponsesWebSocketFromClient(r.Context(), ginCtx, conn, account, "upstream-api-key", firstMessage, &OpenAIWSIngressHooks{
			AfterTurn: func(turn int, result *OpenAIForwardResult, turnErr error) {
				session.mu.Lock()
				defer session.mu.Unlock()
				session.afterTurns = append(session.afterTurns, statelessBridgeAfterTurn{turn: turn, result: result, err: turnErr})
			},
		})
	}))
	t.Cleanup(wsServer.Close)

	dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), nil)
	cancelDial()
	require.NoError(t, err)
	t.Cleanup(func() { _ = client.CloseNow() })
	session.client = client
	return session
}

func (s *statelessBridgeSession) send(frame string) {
	s.t.Helper()
	writeCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	require.NoError(s.t, s.client.Write(writeCtx, coderws.MessageText, []byte(frame)))
}

// readTurn 读取一轮的全部事件，直到出现终态事件（response.completed 或 error）。
func (s *statelessBridgeSession) readTurn() [][]byte {
	s.t.Helper()
	var events [][]byte
	for {
		readCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		_, event, err := s.client.Read(readCtx)
		cancel()
		if err != nil {
			select {
			case proxyErr := <-s.errCh:
				s.t.Fatalf("读取桥接事件失败：read=%v proxy=%v events=%d", err, proxyErr, len(events))
			default:
				s.t.Fatalf("读取桥接事件失败：%v events=%d", err, len(events))
			}
		}
		events = append(events, append([]byte(nil), event...))
		switch gjson.GetBytes(event, "type").String() {
		case "response.completed", "response.failed", "error":
			return events
		}
	}
}

// finish 由客户端正常关闭连接并等待代理返回。
func (s *statelessBridgeSession) finish() error {
	s.t.Helper()
	_ = s.client.Close(coderws.StatusNormalClosure, "done")
	select {
	case err := <-s.errCh:
		return err
	case <-time.After(5 * time.Second):
		s.t.Fatal("代理在客户端关闭后没有返回")
		return nil
	}
}

func (s *statelessBridgeSession) turns() []statelessBridgeAfterTurn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]statelessBridgeAfterTurn(nil), s.afterTurns...)
}

func statelessBridgeSSE(events ...string) *http.Response {
	var body strings.Builder
	for _, event := range events {
		_, _ = body.WriteString("data: " + event + "\n\n")
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
		Body:       io.NopCloser(strings.NewReader(body.String())),
	}
}

// statelessBridgeAssistantTurn 是一轮「推理项 + 助手文本」的上游应答。
func statelessBridgeAssistantTurn(responseID, text string) *http.Response {
	reasoning := fmt.Sprintf(`{"type":"reasoning","id":"rs_%s","summary":[]}`, responseID)
	message := fmt.Sprintf(`{"type":"message","id":"msg_%s","role":"assistant","status":"completed","content":[{"type":"output_text","text":%q}]}`, responseID, text)
	return statelessBridgeSSE(
		fmt.Sprintf(`{"type":"response.created","response":{"id":%q,"model":"deepseek-v4-flash"}}`, responseID),
		`{"type":"response.output_item.done","item":`+reasoning+`}`,
		`{"type":"response.output_item.done","item":`+message+`}`,
		fmt.Sprintf(`{"type":"response.completed","response":{"id":%q,"model":"deepseek-v4-flash","output":[%s,%s],"usage":{"input_tokens":5,"output_tokens":3}}}`, responseID, reasoning, message),
	)
}

// statelessBridgeToolCallTurn 是一轮只发工具调用的上游应答。
func statelessBridgeToolCallTurn(responseID, callID string) *http.Response {
	call := fmt.Sprintf(`{"type":"function_call","id":"fc_%s","call_id":%q,"name":"exec","arguments":"{\"cmd\":\"pwd\"}","status":"completed"}`, responseID, callID)
	return statelessBridgeSSE(
		fmt.Sprintf(`{"type":"response.created","response":{"id":%q,"model":"deepseek-v4-flash"}}`, responseID),
		`{"type":"response.output_item.done","item":`+call+`}`,
		fmt.Sprintf(`{"type":"response.completed","response":{"id":%q,"model":"deepseek-v4-flash","output":[%s],"usage":{"input_tokens":5,"output_tokens":3}}}`, responseID, call),
	)
}

// statelessBridgeAccounts 是三个代表性的无状态桥接平台账号，上游都是原生 Responses：
//   - command_code：按模型分流平台，固定 responses 协议避免触发上游模型目录探测；
//   - custom：自定义多协议平台，adaptive 且只填了 Responses 地址；
//   - deepseek：国产厂商，固定 responses 协议。
func statelessBridgeAccounts() map[string]*Account {
	return map[string]*Account{
		PlatformCommandCode: {
			ID: 9101, Name: "command-code", Platform: PlatformCommandCode, Type: AccountTypeAPIKey,
			Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{"api_key": "cc-key", "api_protocol": APIProtocolResponses},
		},
		PlatformCustom: {
			ID: 9102, Name: "custom", Platform: PlatformCustom, Type: AccountTypeAPIKey,
			Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{
				"api_key": "custom-key", "api_protocol": APIProtocolAdaptive,
				"api_base_urls": map[string]any{APIProtocolResponses: "https://custom.example.test/v1"},
			},
		},
		PlatformDeepseek: {
			ID: 9103, Name: "deepseek", Platform: PlatformDeepseek, Type: AccountTypeAPIKey,
			Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{"api_key": "ds-key", "api_protocol": APIProtocolResponses, "base_url": "https://deepseek.example.test"},
		},
	}
}

func userMessageItem(text string) string {
	return fmt.Sprintf(`{"type":"message","role":"user","content":[{"type":"input_text","text":%q}]}`, text)
}

// 核心场景：Codex 会话开始的预热帧由网关本地应答；之后每一轮只发增量 input 加 previous_response_id，
// 上游（无状态）每次都必须收到完整上下文：历史输入、模型上一轮的推理与回答、本轮增量。
func TestStatelessBridgeKeepsFullContextAcrossIncrementalTurns(t *testing.T) {
	for platform, account := range statelessBridgeAccounts() {
		t.Run(platform, func(t *testing.T) {
			session := startStatelessBridgeSession(t, account,
				statelessBridgeAssistantTurn("resp_up_1", "Hello!"),
				statelessBridgeToolCallTurn("resp_up_2", "call_1"),
				statelessBridgeAssistantTurn("resp_up_3", "Done."),
			)

			// 预热帧：generate=false。网关本地应答，不访问上游。
			session.send(`{"type":"response.create","model":"deepseek-flash","instructions":"be brief","store":false,"stream":true,"generate":false,"input":[]}`)
			warmup := session.readTurn()
			require.Len(t, warmup, 2)
			require.Equal(t, "response.created", gjson.GetBytes(warmup[0], "type").String())
			require.Equal(t, "response.completed", gjson.GetBytes(warmup[1], "type").String())
			warmupID := gjson.GetBytes(warmup[1], "response.id").String()
			require.True(t, strings.HasPrefix(warmupID, "resp_bridge_warmup_"), warmupID)
			require.Equal(t, "deepseek-flash", gjson.GetBytes(warmup[1], "response.model").String())
			require.Empty(t, session.upstream.bodies, "预热帧不能触发上游调用")

			// 第 1 轮：previous_response_id 指向预热应答，input 是全部真实上下文。
			session.send(`{"type":"response.create","model":"deepseek-flash","instructions":"be brief","store":false,"stream":true,"conversation":"conv_1","previous_response_id":"` + warmupID + `","input":[` + userMessageItem("Hi") + `]}`)
			first := session.readTurn()
			require.Equal(t, "response.completed", gjson.GetBytes(first[len(first)-1], "type").String())
			require.Equal(t, "resp_up_1", gjson.GetBytes(first[len(first)-1], "response.id").String())
			require.Len(t, session.upstream.bodies, 1)

			// 第 2 轮：只发增量（新的用户消息），上游必须还能看到第 1 轮的用户消息与助手回答。
			session.send(`{"type":"response.create","model":"deepseek-flash","instructions":"be brief","store":false,"stream":true,"previous_response_id":"resp_up_1","input":[` + userMessageItem("Say it again") + `]}`)
			second := session.readTurn()
			require.Equal(t, "resp_up_2", gjson.GetBytes(second[len(second)-1], "response.id").String())

			// 第 3 轮：工具调用的结果，只发 function_call_output。
			session.send(`{"type":"response.create","model":"deepseek-flash","instructions":"be brief","store":false,"stream":true,"previous_response_id":"resp_up_2","input":[{"type":"function_call_output","call_id":"call_1","output":"/work"}]}`)
			third := session.readTurn()
			require.Equal(t, "resp_up_3", gjson.GetBytes(third[len(third)-1], "response.id").String())

			require.NoError(t, session.finish())
			require.Len(t, session.upstream.bodies, 3)

			for i, body := range session.upstream.bodies {
				// 无状态上游的请求形态：强制 store=false，没有链式字段，也没有 WS 专属字段。
				require.False(t, gjson.GetBytes(body, "store").Bool(), "turn %d", i+1)
				require.True(t, gjson.GetBytes(body, "store").Exists(), "turn %d", i+1)
				for _, field := range []string{"previous_response_id", "conversation", "type", "generate"} {
					require.False(t, gjson.GetBytes(body, field).Exists(), "turn %d field %s", i+1, field)
				}
				require.True(t, gjson.GetBytes(body, "stream").Bool(), "turn %d", i+1)
				require.Equal(t, "deepseek-flash", gjson.GetBytes(body, "model").String(), "turn %d", i+1)
				require.Equal(t, "be brief", gjson.GetBytes(body, "instructions").String(), "turn %d", i+1)
			}

			// 第 1 轮：只有这一轮的用户消息（预热帧的空 input 是基线）。
			input1 := gjson.GetBytes(session.upstream.bodies[0], "input").Array()
			require.Len(t, input1, 1)
			require.Equal(t, "Hi", input1[0].Get("content.0.text").String())

			// 第 2 轮：用户消息、模型上一轮的推理与回答、新的用户消息，顺序不乱。
			input2 := gjson.GetBytes(session.upstream.bodies[1], "input").Array()
			require.Len(t, input2, 4)
			require.Equal(t, "Hi", input2[0].Get("content.0.text").String())
			require.Equal(t, "reasoning", input2[1].Get("type").String())
			require.Equal(t, "message", input2[2].Get("type").String())
			require.Equal(t, "assistant", input2[2].Get("role").String())
			require.Equal(t, "Hello!", input2[2].Get("content.0.text").String())
			require.Equal(t, "Say it again", input2[3].Get("content.0.text").String())

			// 第 3 轮：前两轮的全部上下文，加上第 2 轮的工具调用与它的输出，调用与输出成对。
			input3 := gjson.GetBytes(session.upstream.bodies[2], "input").Array()
			require.Len(t, input3, 6)
			require.Equal(t, "Say it again", input3[3].Get("content.0.text").String())
			require.Equal(t, "function_call", input3[4].Get("type").String())
			require.Equal(t, "call_1", input3[4].Get("call_id").String())
			require.Equal(t, "function_call_output", input3[5].Get("type").String())
			require.Equal(t, "call_1", input3[5].Get("call_id").String())

			// 上游请求打到了账号自己的 Responses 端点，带的是处理器取到的上游凭据（Proxy 的 token 参数）。
			require.Equal(t, http.MethodPost, session.upstream.lastReq.Method)
			require.True(t, strings.HasSuffix(session.upstream.lastReq.URL.Path, "/responses"), session.upstream.lastReq.URL.String())
			require.Equal(t, "Bearer upstream-api-key", session.upstream.lastReq.Header.Get("Authorization"))

			// AfterTurn 回调：预热轮次仍回调一次（释放并发槽位与正文引用），但结果带 LocallyAnswered 标记，
			// 处理器据此不结算用量、不计费、不上报调度结果；真实轮次不带标记，带上游响应 ID 与用量。
			turns := session.turns()
			require.Len(t, turns, 4)
			for i, turn := range turns {
				require.Equal(t, i+1, turn.turn)
				require.NoError(t, turn.err)
				require.NotNil(t, turn.result)
				require.True(t, turn.result.OpenAIWSMode)
				require.Equal(t, i == 0, turn.result.LocallyAnswered, "turn %d：只有本地合成的预热轮次带标记", turn.turn)
			}
			require.True(t, strings.HasPrefix(turns[0].result.RequestID, "resp_bridge_warmup_"))
			require.Zero(t, turns[0].result.Usage.InputTokens+turns[0].result.Usage.OutputTokens)
			require.Equal(t, "resp_up_1", turns[1].result.RequestID)
			require.Equal(t, 5, turns[1].result.Usage.InputTokens)
			require.Equal(t, 3, turns[1].result.Usage.OutputTokens)

			// 预热应答是网关本地合成的，不能当作真实响应绑定到账号；上游响应照常绑定。
			store := session.svc.getOpenAIWSStateStore()
			require.NotNil(t, store)
			boundWarmup, _ := store.GetResponseAccount(context.Background(), 0, warmupID)
			require.Zero(t, boundWarmup)
			boundReal, err := store.GetResponseAccount(context.Background(), 0, "resp_up_1")
			require.NoError(t, err)
			require.Equal(t, account.ID, boundReal)
		})
	}
}

// 客户端发完整 input（没有 previous_response_id）的请求：上游收到的就是这份完整 input，
// 之后的增量轮次以它为基线，不会把更早的历史重复拼回去。
func TestStatelessBridgeFullRequestResetsReplayBaseline(t *testing.T) {
	account := statelessBridgeAccounts()[PlatformCustom]
	session := startStatelessBridgeSession(t, account,
		statelessBridgeAssistantTurn("resp_up_1", "One."),
		statelessBridgeAssistantTurn("resp_up_2", "Two."),
		statelessBridgeAssistantTurn("resp_up_3", "Three."),
	)

	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"input":[` + userMessageItem("first") + `]}`)
	session.readTurn()
	// 客户端做了压缩或改了工具集：改发完整 input，不带 previous_response_id。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"input":[` + userMessageItem("compacted summary") + `,` + userMessageItem("second") + `]}`)
	session.readTurn()
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"previous_response_id":"resp_up_2","input":[` + userMessageItem("third") + `]}`)
	session.readTurn()
	require.NoError(t, session.finish())

	require.Len(t, session.upstream.bodies, 3)
	input2 := gjson.GetBytes(session.upstream.bodies[1], "input").Array()
	require.Len(t, input2, 2)
	require.Equal(t, "compacted summary", input2[0].Get("content.0.text").String())
	require.Equal(t, "second", input2[1].Get("content.0.text").String())

	input3 := gjson.GetBytes(session.upstream.bodies[2], "input").Array()
	require.Len(t, input3, 5)
	require.Equal(t, "compacted summary", input3[0].Get("content.0.text").String())
	require.Equal(t, "second", input3[1].Get("content.0.text").String())
	require.Equal(t, "reasoning", input3[2].Get("type").String())
	require.Equal(t, "Two.", input3[3].Get("content.0.text").String())
	require.Equal(t, "third", input3[4].Get("content.0.text").String())
}

// 无法在本连接内续链的帧：回复 previous_response_not_found，绝不把只有增量的请求发给无状态上游；
// 连接保持打开，客户端改发完整 input 后正常继续。
func TestStatelessBridgeRejectsContinuationItCannotRestore(t *testing.T) {
	account := statelessBridgeAccounts()[PlatformCommandCode]
	session := startStatelessBridgeSession(t, account,
		statelessBridgeAssistantTurn("resp_up_1", "Ready."),
		statelessBridgeAssistantTurn("resp_up_2", "Answered."),
	)

	// 新连接的首帧就带 previous_response_id（比如客户端重连后沿用了旧链）：本连接没有这条链。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"previous_response_id":"resp_from_old_connection","input":[` + userMessageItem("delta only") + `]}`)
	rejected := session.readTurn()
	require.Len(t, rejected, 1)
	require.Equal(t, "error", gjson.GetBytes(rejected[0], "type").String())
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(rejected[0], "error.code").String())
	require.Empty(t, session.upstream.bodies, "被拒绝的帧不能到达上游")

	// 客户端按协议约定改发完整 input：同一条连接继续服务。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"input":[` + userMessageItem("full context") + `]}`)
	full := session.readTurn()
	require.Equal(t, "resp_up_1", gjson.GetBytes(full[len(full)-1], "response.id").String())

	// 指向更早（不是上一轮）响应的 previous_response_id 同样拒绝。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"previous_response_id":"resp_stale","input":[` + userMessageItem("branch") + `]}`)
	stale := session.readTurn()
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(stale[0], "error.code").String())
	require.Len(t, session.upstream.bodies, 1)

	// 指向上一轮的正常续链不受影响，且上下文完整。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"previous_response_id":"resp_up_1","input":[` + userMessageItem("next") + `]}`)
	next := session.readTurn()
	require.Equal(t, "resp_up_2", gjson.GetBytes(next[len(next)-1], "response.id").String())
	require.NoError(t, session.finish())

	require.Len(t, session.upstream.bodies, 2)
	input := gjson.GetBytes(session.upstream.bodies[1], "input").Array()
	require.Len(t, input, 4)
	require.Equal(t, "full context", input[0].Get("content.0.text").String())
	require.Equal(t, "Ready.", input[2].Get("content.0.text").String())
	require.Equal(t, "next", input[3].Get("content.0.text").String())

	// 每个被拒绝的轮次也要回调 AfterTurn（无结果、无错误），让并发槽位与请求体引用得到释放。
	turns := session.turns()
	require.Len(t, turns, 4)
	require.Nil(t, turns[0].result)
	require.NoError(t, turns[0].err)
	require.NotNil(t, turns[1].result)
	require.Nil(t, turns[2].result)
	require.NotNil(t, turns[3].result)
}

// 重放历史合并后超出上限：无状态平台不是要求用户「重启对话」，而是让客户端改发完整 input。
func TestStatelessBridgeReplayLimitAsksClientToResendFullInput(t *testing.T) {
	account := statelessBridgeAccounts()[PlatformDeepseek]
	session := startStatelessBridgeSession(t, account,
		statelessBridgeAssistantTurn("resp_up_1", "Ready."),
		statelessBridgeAssistantTurn("resp_up_2", "Huge."),
	)

	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"input":[` + userMessageItem("start") + `]}`)
	session.readTurn()

	// 增量 input 本身就超过历史条数上限（4096）：合并后无法保存重放历史。
	items := make([]string, openAIWSReplayInputMaxItemsDefault+10)
	for i := range items {
		items[i] = `{"type":"message","role":"user","content":"x"}`
	}
	huge := strings.Join(items, ",")
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"previous_response_id":"resp_up_1","input":[` + huge + `]}`)
	rejected := session.readTurn()
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(rejected[0], "error.code").String())
	require.Len(t, session.upstream.bodies, 1)

	// 客户端改发完整 input：不再需要续链，请求原样到达上游，连接未被关闭。
	session.send(`{"type":"response.create","model":"deepseek-flash","store":false,"input":[` + huge + `]}`)
	full := session.readTurn()
	require.Equal(t, "resp_up_2", gjson.GetBytes(full[len(full)-1], "response.id").String())
	require.NoError(t, session.finish())
	require.Len(t, session.upstream.bodies, 2)
	require.Len(t, gjson.GetBytes(session.upstream.bodies[1], "input").Array(), len(items))
}

// 上游不是原生 Responses 的账号（Chat Completions 或 Anthropic 协议）需要协议转换，转换链路只存在于
// HTTP 入口：桥接在入口处以明确原因关闭连接（与改动前对这类账号的结果一致），不访问上游。
func TestStatelessBridgeRejectsAccountsNeedingProtocolConversion(t *testing.T) {
	cases := map[string]*Account{
		"deepseek_chat_completions": {
			ID: 9111, Platform: PlatformDeepseek, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{"api_key": "k", "api_protocol": APIProtocolChatCompletions, "base_url": "https://deepseek.example.test"},
		},
		"custom_chat_only": {
			ID: 9112, Platform: PlatformCustom, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{
				"api_key": "k", "api_protocol": APIProtocolAdaptive,
				"api_base_urls": map[string]any{APIProtocolChatCompletions: "https://custom.example.test/v1"},
			},
		},
		"zhipu_chat_only": {
			ID: 9113, Platform: PlatformZhipu, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{"api_key": "k"},
		},
		"command_code_chat_only": {
			ID: 9114, Platform: PlatformCommandCode, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Concurrency: 1,
			Credentials: map[string]any{"api_key": "k", "api_protocol": APIProtocolChatCompletions},
		},
	}
	for name, account := range cases {
		t.Run(name, func(t *testing.T) {
			session := startStatelessBridgeSession(t, account)
			session.send(`{"type":"response.create","model":"deepseek-flash","input":[` + userMessageItem("hi") + `]}`)

			var proxyErr error
			select {
			case proxyErr = <-session.errCh:
			case <-time.After(5 * time.Second):
				t.Fatal("代理没有返回")
			}
			var closeErr *OpenAIWSClientCloseError
			require.True(t, errors.As(proxyErr, &closeErr), "got %v", proxyErr)
			require.Equal(t, coderws.StatusPolicyViolation, closeErr.StatusCode())
			require.Contains(t, closeErr.Reason(), "native Responses")
			// 带哨兵错误：处理器据此不把这次能力拒绝上报成账号故障。
			require.ErrorIs(t, proxyErr, ErrOpenAIWSBridgeUnsupportedUpstream)
			require.Empty(t, session.upstream.bodies)
			require.Empty(t, session.turns())
		})
	}
}

// Grok 的桥接行为保持原样：不进入预热本地应答、历史仍只收工具调用项、previous_response_id 不做连接内校验。
func TestGrokBridgeKeepsLegacyReplayAndSkipsStatelessBranches(t *testing.T) {
	account := &Account{
		ID: 71, Name: "grok", Platform: PlatformGrok, Type: AccountTypeOAuth, Concurrency: 1, Status: StatusActive,
		Credentials: map[string]any{"base_url": xai.DefaultCLIBaseURL},
	}
	session := startStatelessBridgeSession(t, account,
		statelessBridgeAssistantTurn("resp_grok_1", "Grok one."),
		statelessBridgeAssistantTurn("resp_grok_2", "Grok two."),
	)

	// generate=false 对 Grok 不是预热本地应答：照旧转发上游（并剥离 generate 字段）。
	session.send(`{"type":"response.create","model":"grok-4.3","generate":false,"input":[` + userMessageItem("warm") + `]}`)
	warm := session.readTurn()
	require.Equal(t, "resp_grok_1", gjson.GetBytes(warm[len(warm)-1], "response.id").String())
	require.Len(t, session.upstream.bodies, 1)
	require.False(t, gjson.GetBytes(session.upstream.bodies[0], "generate").Exists())

	// previous_response_id 不是上一轮的响应：Grok 不做连接内校验，照旧交给重放逻辑，不返回 previous_response_not_found。
	session.send(`{"type":"response.create","model":"grok-4.3","previous_response_id":"resp_anything","input":[` + userMessageItem("next") + `]}`)
	next := session.readTurn()
	require.Equal(t, "response.completed", gjson.GetBytes(next[len(next)-1], "type").String())
	require.NoError(t, session.finish())
	require.Len(t, session.upstream.bodies, 2)

	// Grok 的 generate=false 帧真的发给了上游：结果不带本地应答标记，用量照常结算。
	turns := session.turns()
	require.Len(t, turns, 2)
	for _, turn := range turns {
		require.NotNil(t, turn.result)
		require.False(t, turn.result.LocallyAnswered, "turn %d", turn.turn)
	}

	// 历史只收工具调用项：助手文本、推理项不并入（既有行为，不属于本次范围）。
	input := gjson.GetBytes(session.upstream.bodies[1], "input").Array()
	require.Len(t, input, 2)
	require.Equal(t, "warm", input[0].Get("content.0.text").String())
	require.Equal(t, "next", input[1].Get("content.0.text").String())
}

// ---------- 自由格式工具适配（与 HTTP 入口的 DeepSeek 原生 Responses 适配对齐） ----------

func TestOpenAIWSHTTPBridgeAdaptsClientToolsTruthTable(t *testing.T) {
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(nil))
	// 既有行为不变：openai 的 API Key 账号与 Grok 适配；openai OAuth 不适配。
	require.True(t, openAIWSHTTPBridgeAdaptsClientTools(&Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey}))
	require.True(t, openAIWSHTTPBridgeAdaptsClientTools(&Account{Platform: PlatformGrok, Type: AccountTypeOAuth}))
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(&Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}))
	// DeepSeek 原生 Responses 适配（HTTP 入口同样适配）；固定走 Chat Completions 的不适配（桥接本来就服务不了）。
	accounts := statelessBridgeAccounts()
	require.True(t, openAIWSHTTPBridgeAdaptsClientTools(accounts[PlatformDeepseek]))
	chatDeepseek := *accounts[PlatformDeepseek]
	chatDeepseek.Credentials = map[string]any{"api_key": "k", "api_protocol": APIProtocolChatCompletions}
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(&chatDeepseek))
	// HTTP 入口不适配的平台，桥接也不适配。
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(accounts[PlatformCommandCode]))
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(accounts[PlatformCustom]))
	require.False(t, openAIWSHTTPBridgeAdaptsClientTools(&Account{Platform: PlatformKimi, Type: AccountTypeAPIKey}))
}

// DeepSeek 原生 Responses：Codex 的自由格式工具（custom）经桥接发给上游前适配成 function 工具，
// 上游的 function_call 在回给客户端前还原成 custom_tool_call，和 HTTP 入口一致。
func TestProxyOpenAIWSHTTPBridgeTurnDeepSeekAdaptsClientTools(t *testing.T) {
	gin.SetMode(gin.TestMode)

	call := `{"type":"function_call","id":"item_patch","call_id":"call_patch","name":"apply_patch","arguments":"{\"input\":\"*** Begin Patch\"}","status":"completed"}`
	sse := strings.Join([]string{
		`data: {"type":"response.output_item.done","sequence_number":0,"output_index":0,"item":` + call + `}`,
		``,
		`data: {"type":"response.completed","sequence_number":1,"response":{"id":"resp_ds_tools","status":"completed","output":[` + call + `],"usage":{"input_tokens":1,"output_tokens":1}}}`,
		``,
	}, "\n")
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
		Body:       io.NopCloser(strings.NewReader(sse)),
	}}
	svc := newStatelessBridgeService(upstream)
	account := statelessBridgeAccounts()[PlatformDeepseek]

	payload := []byte(`{
		"type":"response.create","model":"deepseek-v4-flash","stream":true,
		"tools":[{"type":"custom","name":"apply_patch","description":"Edit files"}],
		"input":[{"type":"message","role":"user","content":"patch it"}]
	}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
	var events [][]byte

	result, err := svc.proxyOpenAIWSHTTPBridgeTurn(
		context.Background(), c, account, "upstream-api-key", payload, len(payload),
		"deepseek-flash", "", "", "", "", 1,
		func(message []byte) error {
			events = append(events, append([]byte(nil), message...))
			return nil
		},
	)
	require.NoError(t, err)
	require.NotNil(t, result)

	// 上游收到的是 function 工具，而不是上游不认的 custom 工具。
	require.Equal(t, "function", gjson.GetBytes(upstream.lastBody, "tools.0.type").String())
	require.Equal(t, "apply_patch", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())

	// 客户端收到的 function_call 被还原成 custom_tool_call。
	var outputDone []byte
	for _, event := range events {
		if gjson.GetBytes(event, "type").String() == "response.output_item.done" {
			outputDone = event
		}
	}
	require.NotEmpty(t, outputDone)
	require.Equal(t, "custom_tool_call", gjson.GetBytes(outputDone, "item.type").String())
	require.Equal(t, "*** Begin Patch", gjson.GetBytes(outputDone, "item.input").String())
}

// HTTP 入口对国产厂商平台修复工具参数根的 null type（否则上游 400）；桥接对无状态桥接平台补上同一步，
// 对不做这步修复的平台（command_code、custom）保持原样。
func TestProxyOpenAIWSHTTPBridgeTurnRepairsNullToolSchemaTypeLikeHTTPEntry(t *testing.T) {
	gin.SetMode(gin.TestMode)
	accounts := statelessBridgeAccounts()
	payload := []byte(`{
		"type":"response.create","model":"deepseek-v4-flash","stream":true,
		"tools":[{"type":"function","name":"mcp_tool","parameters":{"type":null,"properties":{"q":{"type":"string"}}}}],
		"input":[{"type":"message","role":"user","content":"hi"}]
	}`)
	run := func(account *Account) []byte {
		upstream := &httpUpstreamRecorder{resp: statelessBridgeAssistantTurn("resp_schema", "ok")}
		svc := newStatelessBridgeService(upstream)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
		_, err := svc.proxyOpenAIWSHTTPBridgeTurn(
			context.Background(), c, account, "upstream-api-key", payload, len(payload),
			"deepseek-flash", "", "", "", "", 1, func([]byte) error { return nil },
		)
		require.NoError(t, err)
		return upstream.lastBody
	}

	// deepseek 是国产厂商：根 type 的 null 被修复成 object，和 HTTP 入口一致。
	require.Equal(t, "object", gjson.GetBytes(run(accounts[PlatformDeepseek]), "tools.0.parameters.type").String())
	// command_code、custom 在 HTTP 入口也不做这步修复，桥接不多做。
	require.True(t, gjson.GetBytes(run(accounts[PlatformCommandCode]), "tools.0.parameters.type").Exists())
	require.Equal(t, gjson.Null, gjson.GetBytes(run(accounts[PlatformCommandCode]), "tools.0.parameters.type").Type)
	require.Equal(t, gjson.Null, gjson.GetBytes(run(accounts[PlatformCustom]), "tools.0.parameters.type").Type)
}

// 桥接只服务上游原生 Responses 的账号。按模型分流的账号（command_code）看「账号映射后的路由模型名」判定，
// 与 HTTP 入口、候选池的零转换判定同源；未取到上游模型目录时（httpUpstream 为空，不发请求）按内置规则。
func TestOpenAIWSStatelessBridgeNativeResponsesByModelRouting(t *testing.T) {
	svc := &OpenAIGatewayService{cfg: &config.Config{}}
	account := &Account{
		ID: 9201, Platform: PlatformCommandCode, Type: AccountTypeAPIKey, Status: StatusActive, Concurrency: 1,
		Credentials: map[string]any{
			"api_key":       "k",
			"model_mapping": map[string]any{"my-alias": "gpt-5.5", "my-claude": "claude-sonnet-4-6"},
		},
	}
	ctx := context.Background()

	// 内置规则：GPT 支持 Responses；DeepSeek 只保证 Chat Completions；Claude 走 Anthropic。
	require.True(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, account, "gpt-5.5"))
	require.False(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, account, "deepseek/deepseek-v4-flash"))
	require.False(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, account, "claude-sonnet-4-6"))
	// 账号映射先于协议判定：别名按映射后的模型走，且对已映射的最终模型名同样成立（幂等）。
	require.True(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, account, "my-alias"))
	require.False(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, account, "my-claude"))

	// 显式固定协议优先于内置规则。
	pinned := *account
	pinned.Credentials = map[string]any{"api_key": "k", "api_protocol": APIProtocolResponses}
	require.True(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, &pinned, "deepseek/deepseek-v4-flash"))
	pinned.Credentials = map[string]any{"api_key": "k", "api_protocol": APIProtocolChatCompletions}
	require.False(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, &pinned, "gpt-5.5"))

	require.False(t, (*OpenAIGatewayService)(nil).openAIWSStatelessBridgeNativeResponses(ctx, account, "gpt-5.5"))
	require.False(t, svc.openAIWSStatelessBridgeNativeResponses(ctx, nil, "gpt-5.5"))
}
