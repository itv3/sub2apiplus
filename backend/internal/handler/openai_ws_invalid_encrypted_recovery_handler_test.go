package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// 0.2.18-4 在 DMIT 复测仍断流的第三轮缺陷（跨模型旧会话的失效密文恢复），handler 层贯通用例。
//
// 现场：真实上游对 invalid_encrypted_content 只发 error 事件，既不发终态也不断连。转发循环只认终态，
// 在 error 之后继续阻塞读上游，handler 不返回，该连接按大历史首帧加权的 WS 内存预留一直占着；
// 客户端收到 error 重连，新连接读首帧要先过同一个共享预算的准入，被拒（websocket_request_memory_rejected）
// 就读不到首帧、也无法抢占旧连接，重连 5 次全部用完。
//
// 这些用例真正走 handler（含内存准入、账号槽位、调度上报），假上游只发 error、不发终态、不断连，首帧是几 MiB 的大历史、
// 共享预算压到只够一个连接。service 层的 Go 测试绕过 handler 准入，抓不到这个缺陷。

const (
	invalidEncGroupID   = int64(4301)
	invalidEncAccountID = int64(9921)
)

// invalidEncAccountRepo 是带“状态变更记录”的账号仓库桩：这类输入校验失败不能触发账号冷却、不可调度、
// 限流或错误标记，任何一次状态变更调用都应让用例失败。
type invalidEncAccountRepo struct {
	*openAIWSUsageHandlerAccountRepoStub
	mu    sync.Mutex
	calls []string
}

func (r *invalidEncAccountRepo) record(name string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, name)
}

func (r *invalidEncAccountRepo) stateCalls() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.calls...)
}

func (r *invalidEncAccountRepo) SetError(context.Context, int64, string) error {
	r.record("SetError")
	return nil
}

func (r *invalidEncAccountRepo) SetSchedulable(context.Context, int64, bool) error {
	r.record("SetSchedulable")
	return nil
}

func (r *invalidEncAccountRepo) SetRateLimited(context.Context, int64, time.Time) error {
	r.record("SetRateLimited")
	return nil
}

func (r *invalidEncAccountRepo) SetModelRateLimit(context.Context, int64, string, time.Time, ...string) error {
	r.record("SetModelRateLimit")
	return nil
}

func (r *invalidEncAccountRepo) SetOverloaded(context.Context, int64, time.Time) error {
	r.record("SetOverloaded")
	return nil
}

func (r *invalidEncAccountRepo) SetTempUnschedulable(context.Context, int64, time.Time, string) error {
	r.record("SetTempUnschedulable")
	return nil
}

func (r *invalidEncAccountRepo) Update(context.Context, *service.Account) error {
	r.record("Update")
	return nil
}

// invalidEncCipher 生成形如真实密文的脱敏样本：统一前缀 + 标签 + 定长尾，首尾 4 位用于错误文案里的“首...尾”片段。
func invalidEncCipher(label string) string {
	return "gAAA" + label + strings.Repeat("x", 24) + "AB" + label[len(label)-2:]
}

// invalidEncHistoryFrame 构造客户端的整段历史帧：padBytes 大小的历史正文把首帧撑到几 MiB（触发预算加权），
// 其中两个推理项是别家模型留下的（带非空 content，密文 GPT 无法验证），另有一个 OpenAI 格式的有效推理项（无 content）。
func invalidEncHistoryFrame(padBytes int) []byte {
	reasoning := func(id, cipher string, withContent bool) string {
		content := ""
		if withContent {
			content = `,"content":[{"type":"reasoning_text","text":"别家模型的可见推理"}]`
		}
		return `{"type":"reasoning","id":"` + id + `","summary":[{"type":"summary_text","text":"摘要-` + id + `"}]` + content +
			`,"encrypted_content":"` + cipher + `"}`
	}
	return []byte(`{"type":"response.create","model":"gpt-5.6-sol","stream":false,"prompt_cache_key":"pck-handler-recovery","input":[` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"` + strings.Repeat("历", padBytes/3) + `"}]},` +
		reasoning("rs_bad1", invalidEncCipher("bad1"), true) + `,` +
		reasoning("rs_good", invalidEncCipher("good"), false) + `,` +
		reasoning("rs_bad2", invalidEncCipher("bad2"), true) + `,` +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}]}`)
}

// invalidEncUpstream 是假上游：带“失效密文”的帧只回 invalid_encrypted_content 的 error（一次只点名遇到的第一项），
// 不发终态、不断连；其余帧回 response.completed。记录每一帧里出现过的密文。
type invalidEncUpstream struct {
	t      *testing.T
	server *httptest.Server
	bad    map[string]struct{}
	mu     sync.Mutex
	frames [][]byte
}

func newInvalidEncUpstream(t *testing.T, bad ...string) *invalidEncUpstream {
	u := &invalidEncUpstream{t: t, bad: map[string]struct{}{}}
	for _, cipher := range bad {
		u.bad[cipher] = struct{}{}
	}
	u.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			return
		}
		defer func() { _ = conn.CloseNow() }()
		conn.SetReadLimit(32 << 20) // 大历史首帧远超默认的 32KiB 读上限
		for {
			_, payload, readErr := conn.Read(r.Context())
			if readErr != nil {
				return // 网关关闭上游连接，或测试结束
			}
			u.mu.Lock()
			u.frames = append(u.frames, append([]byte(nil), payload...))
			u.mu.Unlock()
			rejected := ""
			gjson.GetBytes(payload, "input").ForEach(func(_, item gjson.Result) bool {
				if _, hit := u.bad[item.Get("encrypted_content").String()]; hit {
					rejected = item.Get("encrypted_content").String()
					return false
				}
				return true
			})
			message := fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_recovered_%d","model":"gpt-5.6-sol","usage":{"input_tokens":2,"output_tokens":1}}}`, len(u.frameSnapshot()))
			if rejected != "" {
				// 上游文案只带被拒密文的“首 4 位...末 4 位”，且只发 error：没有 response.failed，也不断连。
				message = `{"type":"error","status":400,"error":{"type":"invalid_request_error","code":"invalid_encrypted_content","message":"The encrypted content ` +
					rejected[:4] + `...` + rejected[len(rejected)-4:] + ` could not be verified. Reason: Encrypted content could not be decrypted or parsed."}}`
			}
			writeCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
			writeErr := conn.Write(writeCtx, coderws.MessageText, []byte(message))
			cancel()
			if writeErr != nil {
				return
			}
		}
	}))
	t.Cleanup(u.server.Close)
	return u
}

func (u *invalidEncUpstream) frameSnapshot() [][]byte {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([][]byte(nil), u.frames...)
}

type invalidEncEnv struct {
	handlerServer *httptest.Server
	handler       *OpenAIGatewayHandler
	accountRepo   *invalidEncAccountRepo
	logs          *observer.ObservedLogs
	upstream      *invalidEncUpstream
}

// newInvalidEncEnv 搭一个真实的 ctx_pool WS 入站环境（含 handler 的内存准入）。budgetBytes 是共享预算。
func newInvalidEncEnv(t *testing.T, upstream *invalidEncUpstream, budgetBytes int64) *invalidEncEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)

	account := service.Account{
		ID:          invalidEncAccountID,
		Name:        "openai-ws-invalid-encrypted-recovery",
		Platform:    service.PlatformOpenAI,
		Type:        service.AccountTypeAPIKey,
		Status:      service.StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"api_key":  "sk-test",
			"base_url": upstream.server.URL,
		},
		Extra: map[string]any{
			"openai_apikey_responses_websockets_v2_enabled": true,
			"openai_apikey_responses_websockets_v2_mode":    service.OpenAIWSIngressModeCtxPool,
		},
	}

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
	// 读上游超时拉长：未修复时旧连接就是卡在这里，不能让超时把缺陷掩盖成“自己结束”。
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 60
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3
	// 共享内存预算：只够一个大历史连接。
	cfg.Gateway.RequestMemoryBudgetBytes = budgetBytes
	cfg.Gateway.RequestMemoryFixedBytes = 8 << 20
	cfg.Gateway.RequestMemoryAmplification = 5
	cfg.Gateway.RequestMemoryMaxRequestBytes = 16 << 20

	accountRepo := &invalidEncAccountRepo{openAIWSUsageHandlerAccountRepoStub: &openAIWSUsageHandlerAccountRepoStub{account: account}}
	usageRepo := &openAIWSUsageHandlerUsageLogRepoStub{created: make(chan *service.UsageLog, 16)}
	billingCacheSvc := service.NewBillingCacheService(nil, nil, nil, nil, nil, nil, cfg, nil)
	t.Cleanup(billingCacheSvc.Stop)
	gatewaySvc := service.NewOpenAIGatewayService(
		accountRepo,
		usageRepo,
		nil,
		nil,
		nil,
		nil,
		nil,
		cfg,
		nil,
		nil,
		service.NewBillingService(cfg, nil),
		nil,
		billingCacheSvc,
		&compositeWSHTTPUpstream{},
		&service.DeferredService{},
		nil,
		nil,
		nil,
		nil,
		nil,
		nil,
		nil,
		nil, // userPlatformQuotaRepo
	)
	cache := &concurrencyCacheMock{
		acquireUserSlotFn: func(context.Context, int64, int, string) (bool, error) { return true, nil },
		acquireAccountSlotFn: func(context.Context, int64, int, string) (bool, error) {
			return true, nil
		},
	}
	h := &OpenAIGatewayHandler{
		cfg:                 cfg,
		gatewayService:      gatewaySvc,
		billingCacheService: billingCacheSvc,
		apiKeyService:       &service.APIKeyService{},
		concurrencyHelper:   NewConcurrencyHelper(service.NewConcurrencyService(cache), SSEPingFormatNone, time.Second),
	}

	groupID := invalidEncGroupID
	apiKey := &service.APIKey{ID: 1802, GroupID: &groupID, User: &service.User{ID: 1702, Status: service.StatusActive}}
	core, observed := observer.New(zap.DebugLevel)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(logger.IntoContext(c.Request.Context(), zap.New(core)))
		c.Set(string(middleware.ContextKeyAPIKey), apiKey)
		c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: apiKey.User.ID, Concurrency: 1})
		c.Next()
	})
	router.GET("/openai/v1/responses", h.ResponsesWebSocket)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	return &invalidEncEnv{handlerServer: server, handler: h, accountRepo: accountRepo, logs: observed, upstream: upstream}
}

func (e *invalidEncEnv) dial(t *testing.T) *coderws.Conn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(e.handlerServer.URL, "http")+"/openai/v1/responses",
		&coderws.DialOptions{CompressionMode: coderws.CompressionContextTakeover})
	require.NoError(t, err)
	conn.SetReadLimit(32 << 20)
	t.Cleanup(func() { _ = conn.CloseNow() })
	return conn
}

func (e *invalidEncEnv) memoryStats() (used, capacity int64, rejected uint64) {
	used, capacity, _, rejected, _ = e.handler.responsesRequestMemoryAdmission().stats()
	return used, capacity, rejected
}

func (e *invalidEncEnv) logCount(message string) int {
	return len(e.logs.FilterMessage(message).All())
}

// invalidEncAttempt 是客户端一次尝试（一个连接）的结果。
type invalidEncAttempt struct {
	eventTypes []string
	errorCode  string
	completed  bool
	closeCode  coderws.StatusCode // 读到服务端关闭时的关闭码；没读到关闭为 -1
	readErr    error
}

// invalidEncRunAttempt 拨号、发首帧，然后读到 response.completed 或服务端关闭为止。
func (e *invalidEncEnv) runAttempt(t *testing.T, frame []byte, readTimeout time.Duration) invalidEncAttempt {
	t.Helper()
	conn := e.dial(t)
	writeCtx, cancelWrite := context.WithTimeout(context.Background(), 5*time.Second)
	require.NoError(t, conn.Write(writeCtx, coderws.MessageText, frame))
	cancelWrite()
	attempt := invalidEncAttempt{closeCode: -1}
	for {
		readCtx, cancel := context.WithTimeout(context.Background(), readTimeout)
		_, message, err := conn.Read(readCtx)
		cancel()
		if err != nil {
			attempt.readErr = err
			var closeErr coderws.CloseError
			if errors.As(err, &closeErr) {
				attempt.closeCode = closeErr.Code
			}
			return attempt
		}
		eventType := gjson.GetBytes(message, "type").String()
		attempt.eventTypes = append(attempt.eventTypes, eventType)
		switch eventType {
		case "error":
			attempt.errorCode = gjson.GetBytes(message, "error.code").String()
		case "response.completed":
			attempt.completed = true
			// 客户端完成本次对话后正常关闭连接，handler 随之返回。
			_ = conn.Close(coderws.StatusNormalClosure, "done")
			return attempt
		}
	}
}

// 贯通用例：只发 error + 大历史首帧 + 两个带 content 的坏密文 + 共享预算只够一个连接。
//
//	连接一：首帧过准入 → 上游只回 error → error 原样写给客户端 → 网关结束本轮（1000）→ handler 返回并释放预算；
//	客户端按真实行为重连（最多 5 次，每次间隔很短）：新连接读完首帧过准入 → 一次清完两个坏密文 → response.completed。
//
// 还原“结束本轮”后：旧连接卡在读上游、一直占着预算，重连的新连接全部被准入拒绝（5/5 用尽）。
func TestOpenAIWSInvalidEncryptedContentErrorOnlyUpstreamReleasesBudgetAndReconnectSucceeds(t *testing.T) {
	bad1, bad2, good := invalidEncCipher("bad1"), invalidEncCipher("bad2"), invalidEncCipher("good")
	upstream := newInvalidEncUpstream(t, bad1, bad2)
	frame := invalidEncHistoryFrame(3 << 20)
	// 一个 3MiB 级别的连接加权 = 固定 8MiB + 放大 5 倍的正文；预算 40MiB 只够一个。
	env := newInvalidEncEnv(t, upstream, 40<<20)

	// 连接一：被拒。读到 error 之后继续读，等网关的 Close 帧（客户端回应关闭握手后服务端才会真正返回）。
	first := env.dial(t)
	writeCtx, cancelWrite := context.WithTimeout(context.Background(), 5*time.Second)
	require.NoError(t, first.Write(writeCtx, coderws.MessageText, frame))
	cancelWrite()
	readCtx, cancelRead := context.WithTimeout(context.Background(), 10*time.Second)
	_, errorEvent, err := first.Read(readCtx)
	cancelRead()
	require.NoError(t, err)
	require.Equal(t, "error", gjson.GetBytes(errorEvent, "type").String())
	require.Equal(t, "invalid_encrypted_content", gjson.GetBytes(errorEvent, "error.code").String(), "error 事件原样写给客户端")

	// 前提：旧连接此刻确实占着预算（这就是线上被卡住的那份），且共享预算已经压到放不下第二个同样的连接。
	used, capacity, _ := env.memoryStats()
	require.Greater(t, used, int64(20<<20), "大历史首帧按放大倍数加权占用预算")
	require.Less(t, capacity-used, used, "剩余预算放不下第二个同样大小的连接")

	// 旧连接的客户端侧继续在后台读（真实客户端的读循环）：网关结束本轮后会发 Close(1000)，读到它并回应关闭握手。
	// 客户端收到 error 就立刻重连，不等旧连接关完——正是线上的时序。
	oldClosed := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_, _, readErr := first.Read(ctx)
		oldClosed <- readErr
	}()

	// 客户端重连：最多 5 次（Codex 的重连上限），被准入拒绝（1013）就稍后再来。
	var final invalidEncAttempt
	attempts := 0
	for attempts < 5 {
		attempts++
		final = env.runAttempt(t, frame, 10*time.Second)
		if final.completed {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	require.True(t, final.completed, "重连 %d 次后仍未完成：events=%v closeCode=%d err=%v memory_rejected_logs=%d",
		attempts, final.eventTypes, final.closeCode, final.readErr, env.logCount("openai.websocket_request_memory_rejected"))
	require.Empty(t, final.errorCode, "一次清完后不再被拒")
	require.Zero(t, env.logCount("openai.websocket_request_memory_rejected"), "重连没有被内存准入拒绝")

	// 旧连接是被网关主动关闭的（1000），不是等到超时。
	select {
	case closeReadErr := <-oldClosed:
		var closeErr coderws.CloseError
		require.ErrorAs(t, closeReadErr, &closeErr, "error 之后网关应主动关闭旧连接，而不是继续等上游终态")
		require.Equal(t, coderws.StatusNormalClosure, closeErr.Code)
	case <-time.After(10 * time.Second):
		t.Fatal("旧连接没有被网关关闭")
	}

	// 上游共收到两帧：首帧原样带着两个坏密文；重连帧里两个坏密文都被一次清掉，有效推理项原样保留。
	frames := upstream.frameSnapshot()
	require.Len(t, frames, 2, "一次清完：被拒一次、重连一次")
	require.Contains(t, string(frames[0]), bad1)
	require.Contains(t, string(frames[0]), bad2)
	require.NotContains(t, string(frames[1]), bad1)
	require.NotContains(t, string(frames[1]), bad2)
	require.Contains(t, string(frames[1]), good, "OpenAI 格式（无 content）的有效推理项不被误清")
	require.Contains(t, string(frames[1]), "摘要-rs_bad1", "推理骨架保留")

	// 预算全部归还：旧连接与新连接的 handler 都已返回。
	require.Eventually(t, func() bool {
		usedNow, _, _ := env.memoryStats()
		return usedNow == 0
	}, 10*time.Second, 50*time.Millisecond, "handler 返回后内存预留全部释放")

	// 账号状态不受影响：没有任何冷却／不可调度／限流／错误标记写入；网关结束本轮按“正常结束”记录，不是代理失败。
	require.Empty(t, env.accountRepo.stateCalls(), "不得变更账号状态")
	require.GreaterOrEqual(t, env.logCount("openai.websocket_ingress_closed_normally"), 1, "结束本轮走 handler 的正常结束分支")
	require.Zero(t, env.logCount("openai.websocket_proxy_failed"), "不得被当成代理失败（会上报调度失败并计健康熔断）")
}

// 单连接最小用例：handler 返回即释放预算。与上一个用例互补——这里不重连，只证明 error 写出后预算归零。
func TestOpenAIWSInvalidEncryptedContentErrorOnlyHandlerReturnsAndReleasesBudget(t *testing.T) {
	bad1 := invalidEncCipher("bad1")
	upstream := newInvalidEncUpstream(t, bad1)
	env := newInvalidEncEnv(t, upstream, 40<<20)

	attempt := env.runAttempt(t, invalidEncHistoryFrame(3<<20), 10*time.Second)
	require.Equal(t, "invalid_encrypted_content", attempt.errorCode)
	require.False(t, attempt.completed)
	require.Equal(t, coderws.StatusNormalClosure, attempt.closeCode)
	require.Equal(t, "error", attempt.eventTypes[len(attempt.eventTypes)-1], "客户端收到的最后一个事件就是 error，没有伪造终态")

	require.Eventually(t, func() bool {
		used, _, _ := env.memoryStats()
		return used == 0
	}, 10*time.Second, 50*time.Millisecond, "handler 返回后内存预留释放")
	require.Empty(t, env.accountRepo.stateCalls())
	require.Zero(t, env.logCount("openai.websocket_proxy_failed"))
}

// 账号归因：网关以 1000 结束本轮，handler 视作正常结束，不上报账号调度失败（与其它关闭码对照）。
func TestOpenAIWSInvalidEncryptedTurnEndStatusIsNotAccountFailure(t *testing.T) {
	endTurn := service.NewOpenAIWSClientCloseError(coderws.StatusNormalClosure, "encrypted content rejected by upstream, please reconnect", nil)
	require.True(t, openAIWSIngressEndedByClient(endTurn))
	for _, status := range []coderws.StatusCode{coderws.StatusPolicyViolation, coderws.StatusInternalError, coderws.StatusTryAgainLater} {
		other := service.NewOpenAIWSClientCloseError(status, "x", nil)
		require.False(t, openAIWSIngressEndedByClient(other))
		require.True(t, shouldReportOpenAIWSProxyAccountFailure(other), "其它关闭码会被记成账号故障，所以这里必须用 1000")
	}
}
