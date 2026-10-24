package handler

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// Composite 分组 + 账号级候选池 + Responses WebSocket 的端到端用例。
//
// 现象回放：Codex 先用 WebSocket 连 /v1/responses。Composite 分组里同一个公开模型（deepseek-flash）
// 被多个平台的账号声明时，WS 首帧此前不带候选池入口标记，走旧的归属判定，判成歧义后被平台白名单以
// 1008 关闭；Codex 把握手之后的失败当作网络错误，重连满 5 次才回落 HTTP，界面上就是反复的
// 「正在重新连接 x/5」。这里用真实的候选池、真实的调度器和真实的 WS→HTTP 桥接复现整条链路。

const compositePoolWSModel = "deepseek-flash"

// compositePoolWSAccountRepo 同时扮演候选池的账号目录与调度器的账号仓储。
type compositePoolWSAccountRepo struct {
	service.AccountRepository
	accounts []service.Account
}

func (r *compositePoolWSAccountRepo) ListAllWithFilters(context.Context, string, string, string, string, int64, string) ([]service.Account, error) {
	return append([]service.Account(nil), r.accounts...), nil
}

func (r *compositePoolWSAccountRepo) ListSchedulableByPlatform(_ context.Context, platform string) ([]service.Account, error) {
	var out []service.Account
	for _, account := range r.accounts {
		if account.Platform == platform {
			out = append(out, account)
		}
	}
	return out, nil
}

func (r *compositePoolWSAccountRepo) ListSchedulableByGroupIDAndPlatform(ctx context.Context, _ int64, platform string) ([]service.Account, error) {
	return r.ListSchedulableByPlatform(ctx, platform)
}

func (r *compositePoolWSAccountRepo) GetByID(_ context.Context, id int64) (*service.Account, error) {
	for _, account := range r.accounts {
		if account.ID == id {
			copied := account
			return &copied, nil
		}
	}
	return nil, nil
}

// compositePoolUpstreamCall 是上游收到的一次 HTTP 请求。
type compositePoolUpstreamCall struct {
	authorization string
	path          string
	body          []byte
}

// compositePoolUpstream 是账号共用的假上游：HTTP 请求按 Responses SSE 应答并记录；
// WebSocket 升级请求（openai 平台账号的原生通路）按帧应答并记录。
type compositePoolUpstream struct {
	server *httptest.Server
	mu     sync.Mutex
	calls  []compositePoolUpstreamCall
	frames [][]byte
	count  int
}

func newCompositePoolUpstream(t *testing.T) *compositePoolUpstream {
	t.Helper()
	upstream := &compositePoolUpstream{}
	upstream.server = httptest.NewServer(http.HandlerFunc(upstream.serve))
	t.Cleanup(upstream.server.Close)
	return upstream
}

func (u *compositePoolUpstream) nextResponseNumber() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.count++
	return u.count
}

func (u *compositePoolUpstream) serve(w http.ResponseWriter, r *http.Request) {
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		u.serveWebSocket(w, r)
		return
	}
	body, _ := io.ReadAll(r.Body)
	u.mu.Lock()
	u.calls = append(u.calls, compositePoolUpstreamCall{authorization: r.Header.Get("Authorization"), path: r.URL.Path, body: body})
	u.mu.Unlock()

	n := u.nextResponseNumber()
	message := fmt.Sprintf(`{"type":"message","id":"msg_pool_%d","role":"assistant","status":"completed","content":[{"type":"output_text","text":"answer-%d"}]}`, n, n)
	w.Header().Set("Content-Type", "text/event-stream")
	events := []string{
		fmt.Sprintf(`{"type":"response.created","response":{"id":"resp_pool_%d","model":"deepseek-v4-flash"}}`, n),
		`{"type":"response.output_item.done","item":` + message + `}`,
		fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_pool_%d","model":"deepseek-v4-flash","output":[%s],"usage":{"input_tokens":4,"output_tokens":2}}}`, n, message),
	}
	for _, event := range events {
		_, _ = fmt.Fprintf(w, "data: %s\n\n", event)
	}
}

func (u *compositePoolUpstream) serveWebSocket(w http.ResponseWriter, r *http.Request) {
	conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
	if err != nil {
		return
	}
	defer func() { _ = conn.CloseNow() }()
	for {
		readCtx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		_, frame, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			return
		}
		u.mu.Lock()
		u.frames = append(u.frames, append([]byte(nil), frame...))
		u.mu.Unlock()
		n := u.nextResponseNumber()
		response := fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_native_%d","model":%q,"usage":{"input_tokens":2,"output_tokens":1}}}`, n, gjson.GetBytes(frame, "model").String())
		writeCtx, cancelWrite := context.WithTimeout(r.Context(), 5*time.Second)
		writeErr := conn.Write(writeCtx, coderws.MessageText, []byte(response))
		cancelWrite()
		if writeErr != nil {
			return
		}
	}
}

func (u *compositePoolUpstream) httpCalls() []compositePoolUpstreamCall {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]compositePoolUpstreamCall(nil), u.calls...)
}

func (u *compositePoolUpstream) wsFrames() [][]byte {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([][]byte(nil), u.frames...)
}

func compositePoolClaim() map[string]any {
	return map[string]any{compositePoolWSModel: "deepseek-v4-flash"}
}

// 各平台账号构造器：全部声明 deepseek-flash，上游都是原生 Responses，只差平台与优先级。
func compositePoolCommandCode(id int64, priority int, base string) service.Account {
	return service.Account{
		ID: id, Name: "command-code", Platform: service.PlatformCommandCode, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: priority,
		Credentials: map[string]any{"api_key": "key-command-code", "api_protocol": service.APIProtocolResponses, "base_url": base, "model_mapping": compositePoolClaim()},
	}
}

func compositePoolCustom(id int64, priority int, base string) service.Account {
	return service.Account{
		ID: id, Name: "custom", Platform: service.PlatformCustom, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: priority,
		Credentials: map[string]any{
			"api_key": "key-custom", "api_protocol": service.APIProtocolAdaptive,
			"api_base_urls": map[string]any{service.APIProtocolResponses: base}, "model_mapping": compositePoolClaim(),
		},
	}
}

func compositePoolDeepseek(id int64, priority int, base string) service.Account {
	return service.Account{
		ID: id, Name: "deepseek", Platform: service.PlatformDeepseek, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: priority,
		Credentials: map[string]any{"api_key": "key-deepseek", "api_protocol": service.APIProtocolResponses, "base_url": base, "model_mapping": compositePoolClaim()},
	}
}

func compositePoolOpenAI(id int64, priority int, base string) service.Account {
	return service.Account{
		ID: id, Name: "openai", Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: priority,
		Credentials: map[string]any{"api_key": "key-openai", "base_url": base, "model_mapping": compositePoolClaim()},
		Extra: map[string]any{
			"openai_apikey_responses_websockets_v2_enabled": true,
			"openai_apikey_responses_websockets_v2_mode":    service.OpenAIWSIngressModePassthrough,
		},
	}
}

// compositePoolWSEnv 是一套完整的处理器环境，调用方只需要写帧、读事件。
type compositePoolWSEnv struct {
	t        *testing.T
	client   *coderws.Conn
	upstream *compositePoolUpstream
	usage    chan *service.UsageLog
}

// startCompositePoolWS 启动处理器并建立客户端连接。withPool=false 时解析器不带候选池，
// 用于复现改动前「跨平台同名模型判歧义」的旧流程。
func startCompositePoolWS(t *testing.T, group *service.Group, withPool bool, accounts func(base string) []service.Account) *compositePoolWSEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)

	upstream := newCompositePoolUpstream(t)
	repo := &compositePoolWSAccountRepo{accounts: accounts(upstream.server.URL)}

	cfg := &config.Config{}
	cfg.RunMode = config.RunModeSimple
	cfg.Default.RateMultiplier = 1
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.APIKeyEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.ModeRouterV2Enabled = true
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3

	usageRepo := &openAIWSUsageHandlerUsageLogRepoStub{created: make(chan *service.UsageLog, 16)}
	billingCacheSvc := service.NewBillingCacheService(nil, nil, nil, nil, nil, nil, cfg, nil)
	t.Cleanup(billingCacheSvc.Stop)
	gatewaySvc := service.NewOpenAIGatewayService(
		repo, usageRepo, nil, nil, nil, nil, nil, cfg, nil, nil,
		service.NewBillingService(cfg, nil), nil, billingCacheSvc,
		&compositeWSHTTPUpstream{}, &service.DeferredService{},
		nil, nil, nil, nil, nil, nil, nil, nil,
	)

	resolver := service.NewCompositeRouteResolver(&compositeWSRouteRepo{})
	// 线上的归属解析器对「多个平台都声明了同一个模型」的判定是歧义；候选池接入后先于它处理请求。
	resolver.SetModelOwnershipResolver(func(context.Context, int64, string) (service.CompositeModelOwnership, error) {
		return service.CompositeModelOwnership{Ambiguous: true}, nil
	})
	if withPool {
		resolver.SetCandidatePool(service.NewCompositeCandidatePool(repo, gatewaySvc, nil, billingCacheSvc))
	}

	cache := &concurrencyCacheMock{
		acquireUserSlotFn: func(context.Context, int64, int, string) (bool, error) { return true, nil },
		acquireAccountSlotFn: func(context.Context, int64, int, string) (bool, error) {
			return true, nil
		},
	}
	h := &OpenAIGatewayHandler{
		cfg:                 cfg,
		compositeResolver:   resolver,
		gatewayService:      gatewaySvc,
		billingCacheService: billingCacheSvc,
		apiKeyService:       &service.APIKeyService{},
		concurrencyHelper:   NewConcurrencyHelper(service.NewConcurrencyService(cache), SSEPingFormatNone, time.Second),
	}

	groupID := group.ID
	apiKey := &service.APIKey{
		ID: 1801, GroupID: &groupID, Group: group, UserID: 1701,
		User: &service.User{ID: 1701, Status: service.StatusActive},
	}
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Set(string(middleware.ContextKeyAPIKey), apiKey)
		c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: apiKey.User.ID, Concurrency: 1})
		c.Next()
	})
	router.GET("/openai/v1/responses", h.ResponsesWebSocket)
	handlerServer := httptest.NewServer(router)
	t.Cleanup(handlerServer.Close)

	dialCtx, cancelDial := context.WithTimeout(context.Background(), 3*time.Second)
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(handlerServer.URL, "http")+"/openai/v1/responses", &coderws.DialOptions{
		CompressionMode: coderws.CompressionContextTakeover,
	})
	cancelDial()
	require.NoError(t, err)
	t.Cleanup(func() { _ = client.CloseNow() })

	return &compositePoolWSEnv{t: t, client: client, upstream: upstream, usage: usageRepo.created}
}

func (e *compositePoolWSEnv) send(frame string) {
	e.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	require.NoError(e.t, e.client.Write(ctx, coderws.MessageText, []byte(frame)))
}

// readTurn 读取一轮事件直到终态事件（response.completed 或 error）。
func (e *compositePoolWSEnv) readTurn() [][]byte {
	e.t.Helper()
	var events [][]byte
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		_, event, err := e.client.Read(ctx)
		cancel()
		require.NoError(e.t, err, "已收到 %d 个事件", len(events))
		events = append(events, append([]byte(nil), event...))
		switch gjson.GetBytes(event, "type").String() {
		case "response.completed", "response.failed", "error":
			return events
		}
	}
}

// expectClose 断言连接被服务端以给定状态码与原因关闭。
func (e *compositePoolWSEnv) expectClose(status coderws.StatusCode, reason string) {
	e.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, _, err := e.client.Read(ctx)
	require.Error(e.t, err)
	var closeErr coderws.CloseError
	require.ErrorAs(e.t, err, &closeErr)
	require.Equal(e.t, status, closeErr.Code)
	require.Contains(e.t, closeErr.Reason, reason)
}

func (e *compositePoolWSEnv) usageLogs(n int) []*service.UsageLog {
	e.t.Helper()
	logs := make([]*service.UsageLog, 0, n)
	for len(logs) < n {
		select {
		case log := <-e.usage:
			logs = append(logs, log)
		case <-time.After(3 * time.Second):
			e.t.Fatalf("等待用量日志超时：已收到 %d 条，期望 %d 条", len(logs), n)
		}
	}
	return logs
}

func compositePoolWSGroup() *service.Group {
	return &service.Group{ID: 4201, Platform: service.PlatformComposite}
}

func compositePoolUserItem(text string) string {
	return fmt.Sprintf(`{"type":"message","role":"user","content":[{"type":"input_text","text":%q}]}`, text)
}

// 复现并验证修复：跨平台同名模型的首选是 command_code、custom 或 deepseek 时，WS 首帧经候选池选定首选平台，
// 不再以 1008 关闭；连接内多轮对话经 WS→HTTP 桥接服务，上游每轮都拿到完整上下文。
func TestOpenAIResponsesWebSocket_CompositePoolBridgesNonOpenAIPrimary(t *testing.T) {
	type builder func(id int64, priority int, base string) service.Account
	builders := map[string]builder{
		service.PlatformCommandCode: compositePoolCommandCode,
		service.PlatformCustom:      compositePoolCustom,
		service.PlatformDeepseek:    compositePoolDeepseek,
	}
	primaryKeys := map[string]string{
		service.PlatformCommandCode: "Bearer key-command-code",
		service.PlatformCustom:      "Bearer key-custom",
		service.PlatformDeepseek:    "Bearer key-deepseek",
	}
	for platform, build := range builders {
		t.Run(platform, func(t *testing.T) {
			env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
				// 首选账号优先级数值小；另一个平台的账号和一个 openai 平台账号也声明了同一个公开模型。
				others := []service.Account{compositePoolOpenAI(9390, 5, base)}
				for other, otherBuild := range builders {
					if other != platform {
						others = append(others, otherBuild(9380+int64(len(others)), 3, base))
					}
				}
				return append([]service.Account{build(9301, 1, base)}, others...)
			})

			env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","store":false,"stream":true,"input":[` + compositePoolUserItem("hello") + `]}`)
			first := env.readTurn()
			completed := first[len(first)-1]
			require.Equal(t, "response.completed", gjson.GetBytes(completed, "type").String())
			// 客户端始终看到公开模型名，而不是上游模型名。
			require.Equal(t, compositePoolWSModel, gjson.GetBytes(completed, "response.model").String())
			firstID := gjson.GetBytes(completed, "response.id").String()
			require.True(t, strings.HasPrefix(firstID, "resp_pool_"), firstID)

			env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","store":false,"stream":true,"previous_response_id":"` + firstID + `","input":[` + compositePoolUserItem("again") + `]}`)
			second := env.readTurn()
			require.Equal(t, "response.completed", gjson.GetBytes(second[len(second)-1], "type").String())
			_ = env.client.Close(coderws.StatusNormalClosure, "done")

			calls := env.upstream.httpCalls()
			require.Len(t, calls, 2)
			require.Empty(t, env.upstream.wsFrames(), "非 openai 平台的账号不开上游 WebSocket")
			for i, call := range calls {
				// 两轮都落在候选池选出的首选平台的账号上（同一连接固定在首帧选定的平台）。
				require.Equal(t, primaryKeys[platform], call.authorization, "turn %d", i+1)
				require.True(t, strings.HasSuffix(call.path, "/responses"), call.path)
				require.Equal(t, "deepseek-v4-flash", gjson.GetBytes(call.body, "model").String(), "上游模型名来自账号映射")
				require.False(t, gjson.GetBytes(call.body, "store").Bool())
				for _, field := range []string{"previous_response_id", "conversation", "type", "generate"} {
					require.False(t, gjson.GetBytes(call.body, field).Exists(), "turn %d field %s", i+1, field)
				}
			}
			// 第 2 轮客户端只发了增量，上游收到的是：第 1 轮用户消息、第 1 轮助手回答、本轮用户消息。
			input := gjson.GetBytes(calls[1].body, "input").Array()
			require.Len(t, input, 3)
			require.Equal(t, "hello", input[0].Get("content.0.text").String())
			require.Equal(t, "assistant", input[1].Get("role").String())
			require.Equal(t, "answer-1", input[1].Get("content.0.text").String())
			require.Equal(t, "again", input[2].Get("content.0.text").String())

			logs := env.usageLogs(2)
			for _, log := range logs {
				require.Equal(t, int64(9301), log.AccountID)
				require.Equal(t, compositePoolWSModel, log.RequestedModel)
			}
		})
	}
}

// 控制组：解析器没有候选池时，同样的跨平台同名模型仍然判歧义并被 1008 关闭，
// 这是改动前的流程；说明上面的通过确实来自候选池入口标记，而不是别的路径。
func TestOpenAIResponsesWebSocket_CompositeAmbiguousWithoutPoolStillRejected(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), false, func(base string) []service.Account {
		return []service.Account{compositePoolCommandCode(9301, 1, base), compositePoolDeepseek(9302, 2, base)}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","input":[` + compositePoolUserItem("hello") + `]}`)
	env.expectClose(coderws.StatusPolicyViolation, "only supports OpenAI-compatible")
	require.Empty(t, env.upstream.httpCalls())
}

// 单平台 Composite（只有 openai 平台账号声明该模型）：候选池选出的仍是 openai，
// 走原有的上游 WebSocket 通路，帧原样到达上游 WS，不经 HTTP 桥接。
func TestOpenAIResponsesWebSocket_CompositePoolSingleOpenAIPlatformKeepsUpstreamWebSocket(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
		return []service.Account{compositePoolOpenAI(9301, 1, base)}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","input":[` + compositePoolUserItem("hello") + `]}`)
	events := env.readTurn()
	require.Equal(t, "response.completed", gjson.GetBytes(events[len(events)-1], "type").String())
	_ = env.client.Close(coderws.StatusNormalClosure, "done")

	require.Empty(t, env.upstream.httpCalls(), "openai 平台账号不走 HTTP 桥接")
	frames := env.upstream.wsFrames()
	require.Len(t, frames, 1)
	require.Equal(t, "response.create", gjson.GetBytes(frames[0], "type").String())
}

// Anthropic 网关族被候选池首选时维持拒绝，文案不变。
func TestOpenAIResponsesWebSocket_CompositePoolAnthropicPrimaryStillRejected(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
		return []service.Account{{
			ID: 9301, Name: "anthropic", Platform: service.PlatformAnthropic, Type: service.AccountTypeAPIKey,
			Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: 1,
			Credentials: map[string]any{"api_key": "key-anthropic", "base_url": base, "model_mapping": map[string]any{compositePoolWSModel: compositePoolWSModel}},
		}}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","input":[` + compositePoolUserItem("hello") + `]}`)
	env.expectClose(coderws.StatusPolicyViolation, "Responses WebSocket API only supports OpenAI-compatible models for composite groups")
	require.Empty(t, env.upstream.httpCalls())
}

// 首选账号的上游需要协议转换（Chat Completions）：桥接服务不了，关闭原因明确指向原生 Responses，
// 不访问上游；这与改动前对这类账号的结果一致（关闭），只是原因更明确。
func TestOpenAIResponsesWebSocket_CompositePoolChatOnlyPrimaryClosesWithClearReason(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
		account := compositePoolDeepseek(9301, 1, base)
		account.Credentials["api_protocol"] = service.APIProtocolChatCompletions
		return []service.Account{account}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","input":[` + compositePoolUserItem("hello") + `]}`)
	env.expectClose(coderws.StatusPolicyViolation, "native Responses")
	require.Empty(t, env.upstream.httpCalls())
}

// Codex 会话开始的预热帧（generate=false）在桥接下由网关本地应答，不访问上游、不产生真实输出；
// 之后的第一轮以预热应答的 ID 续链，上游拿到的仍是完整请求。
func TestOpenAIResponsesWebSocket_CompositePoolWarmupFrameAnsweredLocally(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
		return []service.Account{compositePoolCommandCode(9301, 1, base)}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","instructions":"be brief","store":false,"stream":true,"generate":false,"input":[]}`)
	warmup := env.readTurn()
	require.Len(t, warmup, 2)
	warmupID := gjson.GetBytes(warmup[1], "response.id").String()
	require.Equal(t, compositePoolWSModel, gjson.GetBytes(warmup[1], "response.model").String())
	require.Empty(t, env.upstream.httpCalls())

	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","instructions":"be brief","store":false,"stream":true,"previous_response_id":"` + warmupID + `","input":[` + compositePoolUserItem("real question") + `]}`)
	real := env.readTurn()
	require.Equal(t, "response.completed", gjson.GetBytes(real[len(real)-1], "type").String())
	_ = env.client.Close(coderws.StatusNormalClosure, "done")

	calls := env.upstream.httpCalls()
	require.Len(t, calls, 1)
	input := gjson.GetBytes(calls[0].body, "input").Array()
	require.Len(t, input, 1)
	require.Equal(t, "real question", input[0].Get("content.0.text").String())
	require.Equal(t, "be brief", gjson.GetBytes(calls[0].body, "instructions").String())
	require.False(t, gjson.GetBytes(calls[0].body, "previous_response_id").Exists())
	require.Len(t, env.usageLogs(2), 2)
}

// 首帧就带 previous_response_id（比如客户端沿用了旧连接的链）：对无状态桥接平台，处理器不再像对 OpenAI
// 那样剥离它后只发增量，而是保留给转发器判定；本连接没有这条链，回复 previous_response_not_found，
// 上游一个字节都收不到；客户端改发完整 input 后正常服务。
func TestOpenAIResponsesWebSocket_CompositePoolFirstFramePreviousResponseIDIsNotSilentlyDropped(t *testing.T) {
	env := startCompositePoolWS(t, compositePoolWSGroup(), true, func(base string) []service.Account {
		return []service.Account{compositePoolCommandCode(9301, 1, base)}
	})
	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","store":false,"previous_response_id":"resp_from_a_previous_connection","input":[` + compositePoolUserItem("delta only") + `]}`)
	rejected := env.readTurn()
	require.Len(t, rejected, 1)
	require.Equal(t, "error", gjson.GetBytes(rejected[0], "type").String())
	require.Equal(t, "previous_response_not_found", gjson.GetBytes(rejected[0], "error.code").String())
	require.Empty(t, env.upstream.httpCalls())

	env.send(`{"type":"response.create","model":"` + compositePoolWSModel + `","store":false,"input":[` + compositePoolUserItem("full context") + `]}`)
	full := env.readTurn()
	require.Equal(t, "response.completed", gjson.GetBytes(full[len(full)-1], "type").String())
	_ = env.client.Close(coderws.StatusNormalClosure, "done")

	calls := env.upstream.httpCalls()
	require.Len(t, calls, 1)
	input := gjson.GetBytes(calls[0].body, "input").Array()
	require.Len(t, input, 1)
	require.Equal(t, "full context", input[0].Get("content.0.text").String())
}

// 桥接无法服务（上游需要协议转换）是请求级的能力拒绝，不是账号故障：不能上报成账号失败，
// 否则每次 Codex 尝试 WebSocket 都会拉低这个健康账号的调度评分。
func TestShouldReportOpenAIWSProxyAccountFailureSkipsBridgeUnsupportedUpstream(t *testing.T) {
	err := service.NewOpenAIWSClientCloseError(
		coderws.StatusPolicyViolation,
		"Responses WebSocket only supports upstreams with native Responses support for this model",
		service.ErrOpenAIWSBridgeUnsupportedUpstream,
	)
	require.False(t, shouldReportOpenAIWSProxyAccountFailure(err))
	require.False(t, shouldReportOpenAIWSProxyAccountFailure(fmt.Errorf("wrapped: %w", err)))
	// 其它 1008 关闭（带别的原因）仍按原规则上报。
	require.True(t, shouldReportOpenAIWSProxyAccountFailure(service.NewOpenAIWSClientCloseError(
		coderws.StatusPolicyViolation, "upstream websocket authentication failed", fmt.Errorf("rejected"),
	)))
}

// 候选池入口标记的构造：Endpoint 固定为 responses，Body 取首帧，请求头取握手头，
// 用户优先取认证主体，其次 API Key 上的用户，再其次 API Key.User；订阅取自认证上下文。
// 返回的上下文只用于 Resolve，不能写回请求。
func TestCompositeCandidatePoolContextForResponsesWSBuildsEntryFromHandshakeAndFirstFrame(t *testing.T) {
	gin.SetMode(gin.TestMode)
	newContext := func() *gin.Context {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
		c.Request.Header.Set("session_id", "sess-handshake")
		c.Request.Header.Set("User-Agent", "codex-ws-test")
		return c
	}
	group := &service.Group{ID: 4201, Platform: service.PlatformComposite}
	frame := []byte(`{"type":"response.create","model":"deepseek-flash","previous_response_id":"resp_1","prompt_cache_key":"pck"}`)

	t.Run("full entry", func(t *testing.T) {
		c := newContext()
		subscription := &service.UserSubscription{ID: 55}
		c.Set(string(middleware.ContextKeySubscription), subscription)
		apiKey := &service.APIKey{ID: 1801, UserID: 1700, Group: group, User: &service.User{ID: 1699}}

		ctx := compositeCandidatePoolContextForResponsesWS(c, apiKey, middleware.AuthSubject{UserID: 1701}, frame)

		entry, ok := service.CompositeCandidatePoolEntryFromContext(ctx)
		require.True(t, ok)
		require.Equal(t, service.CompositeRouteEndpointResponses, entry.Endpoint)
		require.Equal(t, frame, entry.Body)
		require.Equal(t, "sess-handshake", entry.Header.Get("session_id"))
		require.Equal(t, "codex-ws-test", entry.UserAgent)
		require.Equal(t, int64(1801), entry.APIKeyID)
		require.Equal(t, int64(1701), entry.UserID, "认证主体优先")
		require.Same(t, group, entry.Group)
		require.Same(t, subscription, entry.Subscription)
		_, leaked := service.CompositeCandidatePoolEntryFromContext(c.Request.Context())
		require.False(t, leaked, "入口标记只在派生上下文里，不能写回请求")
	})

	t.Run("user fallbacks", func(t *testing.T) {
		apiKey := &service.APIKey{ID: 1801, UserID: 1700, Group: group, User: &service.User{ID: 1699}}
		entry, ok := service.CompositeCandidatePoolEntryFromContext(compositeCandidatePoolContextForResponsesWS(newContext(), apiKey, middleware.AuthSubject{}, frame))
		require.True(t, ok)
		require.Equal(t, int64(1700), entry.UserID)
		require.Nil(t, entry.Subscription)

		apiKey.UserID = 0
		entry, _ = service.CompositeCandidatePoolEntryFromContext(compositeCandidatePoolContextForResponsesWS(newContext(), apiKey, middleware.AuthSubject{}, frame))
		require.Equal(t, int64(1699), entry.UserID)
	})

	t.Run("missing api key leaves the context untouched", func(t *testing.T) {
		c := newContext()
		ctx := compositeCandidatePoolContextForResponsesWS(c, nil, middleware.AuthSubject{UserID: 1701}, frame)
		_, ok := service.CompositeCandidatePoolEntryFromContext(ctx)
		require.False(t, ok)
	})
}
