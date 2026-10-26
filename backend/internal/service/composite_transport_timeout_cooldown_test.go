//go:build unit

package service

// Composite 传输层超时短冷却的单测。
//
// 覆盖：连接超时、TLS 握手超时、等待响应头超时（真实走一次本地慢服务）、读超时、
// 上游超时设置产生的 context.DeadlineExceeded 计入；客户端取消、请求自身期限到、
// DNS 解析失败、连接被拒、其它无超时语义的错误不计入；
// 非 Composite 请求、开关关闭、已有更长冷却不触发或不缩短；
// 首输出超时（含等待响应头阶段）同样补冷却。

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// timeoutNetError 是一个明确带 Timeout() 的 net.Error，模拟连接／读超时。
type timeoutNetError struct{ msg string }

func (e timeoutNetError) Error() string   { return e.msg }
func (e timeoutNetError) Timeout() bool   { return true }
func (e timeoutNetError) Temporary() bool { return true }

// newTransportTimeoutService 返回带冷却桩的 OpenAI 网关服务，用来走真实的传输层错误入口。
func newTransportTimeoutService() (*OpenAIGatewayService, *cooldownFixture) {
	f := newCooldownFixture()
	return &OpenAIGatewayService{rateLimitService: f.service}, f
}

func TestCompositeTransportTimeoutKindCountsExplicitUpstreamTimeouts(t *testing.T) {
	cases := map[string]error{
		"连接超时（dial）": &url.Error{Op: "Post", URL: "https://up.example/v1/chat/completions",
			Err: &net.OpError{Op: "dial", Net: "tcp", Err: timeoutNetError{"i/o timeout"}}},
		"TLS 握手超时": &url.Error{Op: "Post", URL: "https://up.example/v1", Err: timeoutNetError{"net/http: TLS handshake timeout"}},
		"读超时":      &net.OpError{Op: "read", Net: "tcp", Err: timeoutNetError{"i/o timeout"}},
		"等待响应头超时（类型保留）": &url.Error{Op: "Post", URL: "https://up.example/v1",
			Err: timeoutNetError{"net/http: timeout awaiting response headers"}},
		"Client.Timeout 包装的 DeadlineExceeded": fmt.Errorf(`Post "https://up.example": %w (Client.Timeout exceeded while awaiting headers)`, context.DeadlineExceeded),
		"仅剩文本的等待响应头超时":                        errors.New(`Post "https://up.example": net/http: timeout awaiting response headers`),
		"仅剩文本的 TLS 握手超时":                      errors.New(`Post "https://up.example": net/http: TLS handshake timeout`),
		"代理建连超时（暂时性）": &url.Error{Op: "Post", URL: "https://up.example",
			Err: &net.OpError{Op: "proxyconnect", Net: "tcp", Err: timeoutNetError{"i/o timeout"}}},
	}
	for name, err := range cases {
		t.Run(name, func(t *testing.T) {
			require.Equal(t, "transport_timeout", compositeTransportTimeoutKind(context.Background(), err))
		})
	}
}

func TestCompositeTransportTimeoutKindRejectsNonTimeouts(t *testing.T) {
	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	deadlineCtx, cancelDeadline := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelDeadline()

	type tc struct {
		ctx context.Context
		err error
	}
	cases := map[string]tc{
		"nil 错误":             {context.Background(), nil},
		"客户端取消（错误值）":         {context.Background(), context.Canceled},
		"客户端取消（包装）":          {context.Background(), fmt.Errorf("Post: %w", context.Canceled)},
		"客户端取消（上下文已取消）":      {canceledCtx, timeoutNetError{"i/o timeout"}},
		"请求自身期限已到（下游整条请求超时）": {deadlineCtx, context.DeadlineExceeded},
		"DNS 解析失败": {context.Background(), &url.Error{Op: "Post", URL: "https://nohost.invalid",
			Err: &net.OpError{Op: "dial", Net: "tcp", Err: &net.DNSError{Err: "no such host", Name: "nohost.invalid", IsNotFound: true}}}},
		"DNS 解析超时": {context.Background(), &net.DNSError{Err: "i/o timeout", Name: "up.example", IsTimeout: true}},
		"连接被拒（类型）": {context.Background(), &url.Error{Op: "Post", URL: "https://up.example",
			Err: &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}}},
		"连接被拒（文本）":       {context.Background(), errors.New("dial tcp 127.0.0.1:1: connect: connection refused")},
		"网络不可达":          {context.Background(), errors.New("connect: network is unreachable")},
		"代理凭据被拒":         {context.Background(), errors.New("socks connect: username/password authentication failed")},
		"连接被对端重置":        {context.Background(), errors.New("read tcp: connection reset by peer")},
		"提前 EOF":         {context.Background(), errors.New("unexpected EOF")},
		"证书错误":           {context.Background(), errors.New("x509: certificate signed by unknown authority")},
		"没有超时语义的 net 错误": {context.Background(), &net.OpError{Op: "write", Net: "tcp", Err: errors.New("broken pipe")}},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			require.Equal(t, "", compositeTransportTimeoutKind(c.ctx, c.err))
		})
	}
}

// 真实走一次本地慢服务，确认 http.Transport 产生的"等待响应头超时"错误被判为上游超时，
// 而本地端口无人监听产生的"连接被拒"不被判为超时。
func TestCompositeTransportTimeoutKindWithRealTransportErrors(t *testing.T) {
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-time.After(2 * time.Second):
		case <-r.Context().Done():
		}
	}))
	defer slow.Close()
	client := &http.Client{Transport: &http.Transport{ResponseHeaderTimeout: 50 * time.Millisecond}}
	req, err := http.NewRequest(http.MethodPost, slow.URL, nil)
	require.NoError(t, err)
	_, err = client.Do(req)
	require.Error(t, err)
	require.Equal(t, "transport_timeout", compositeTransportTimeoutKind(context.Background(), err), "真实错误：%v", err)

	// 刚关闭的本地服务端口：连接被拒。
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL
	closed.Close()
	req2, err := http.NewRequest(http.MethodPost, closedURL, nil)
	require.NoError(t, err)
	_, err = (&http.Client{Transport: &http.Transport{}}).Do(req2)
	require.Error(t, err)
	require.Equal(t, "", compositeTransportTimeoutKind(context.Background(), err), "真实错误：%v", err)
}

func TestCompositeTransportTimeoutCooldownTriggersThroughTransportErrorEntry(t *testing.T) {
	svc, f := newTransportTimeoutService()
	account := cooldownTestAccount()
	c, _ := newOpenAITransportErrTestContext()
	timeoutErr := &url.Error{Op: "Post", URL: "https://up.example", Err: timeoutNetError{"net/http: timeout awaiting response headers"}}

	before := time.Now()
	retErr := svc.handleOpenAIUpstreamTransportError(compositeAutoCtx(), c, account, timeoutErr, false)

	// 原有行为不变：仍返回 502 的 failover 错误。
	var fo *UpstreamFailoverError
	require.True(t, errors.As(retErr, &fo))
	require.Equal(t, http.StatusBadGateway, fo.StatusCode)
	// 补 30 秒冷却：写库、写缓存、通知进程内封禁。
	require.Equal(t, 1, f.repo.tempCalls)
	require.Equal(t, account.ID, f.repo.lastTempID)
	require.WithinDuration(t, before.Add(30*time.Second), f.repo.lastUntil, 2*time.Second)
	require.Equal(t, 1, f.cache.setCalls)
	require.Equal(t, compositeFailureCooldownMarker, f.cache.lastState.MatchedKeyword)
	require.Equal(t, 0, f.cache.lastState.StatusCode, "没有 HTTP 响应，状态码记 0")
	require.Equal(t, TempUnschedScopeAccount, f.cache.lastState.Scope)
	require.Equal(t, 1, f.blocker.calls)
}

func TestCompositeTransportTimeoutCooldownSkipsNonTimeoutTransportErrors(t *testing.T) {
	for name, err := range map[string]error{
		"DNS 解析失败": &net.DNSError{Err: "no such host", Name: "x.invalid", IsNotFound: true},
		"连接被拒":     errors.New("dial tcp 10.0.0.1:443: connect: connection refused"),
		"连接重置":     errors.New("connection reset by peer"),
	} {
		t.Run(name, func(t *testing.T) {
			svc, f := newTransportTimeoutService()
			c, _ := newOpenAITransportErrTestContext()
			retErr := svc.handleOpenAIUpstreamTransportError(compositeAutoCtx(), c, cooldownTestAccount(), err, false)
			var fo *UpstreamFailoverError
			require.True(t, errors.As(retErr, &fo), "仍然换号")
			require.Equal(t, 0, f.cache.setCalls, "不写 30 秒冷却")
			// 永久性故障原有的 10 分钟冷却写在网关服务自己的 accountRepo（此处为空），不经过冷却桩。
			require.Equal(t, 0, f.repo.tempCalls)
			require.Equal(t, 0, f.blocker.calls)
		})
	}
}

func TestCompositeTransportTimeoutCooldownClientCancelDoesNothing(t *testing.T) {
	svc, f := newTransportTimeoutService()
	c, _ := newOpenAITransportErrTestContext()
	ctx, cancel := context.WithCancel(compositeAutoCtx())
	cancel()
	retErr := svc.handleOpenAIUpstreamTransportError(ctx, c, cooldownTestAccount(), context.Canceled, false)
	require.ErrorIs(t, retErr, context.Canceled, "客户端取消：不换号")
	require.Equal(t, 0, f.repo.tempCalls)
	require.Equal(t, 0, f.cache.setCalls)
	require.Equal(t, 0, f.blocker.calls)

	// 请求自身期限已到（下游整条请求超时）的 DeadlineExceeded 同样不冷却。
	dctx, dcancel := context.WithDeadline(compositeAutoCtx(), time.Now().Add(-time.Second))
	defer dcancel()
	retErr = svc.handleOpenAIUpstreamTransportError(dctx, c, cooldownTestAccount(), context.DeadlineExceeded, false)
	require.ErrorIs(t, retErr, context.DeadlineExceeded)
	require.Equal(t, 0, f.repo.tempCalls)
}

func TestCompositeTransportTimeoutCooldownRequiresCompositeAutoRoute(t *testing.T) {
	timeoutErr := timeoutNetError{"i/o timeout"}
	cases := map[string]context.Context{
		"非 Composite 请求": context.Background(),
		"显式路由": WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
			Matched: true, Source: CompositeRouteSourceExplicit, GroupID: 1, PublicModel: "m", TargetPlatform: PlatformKimi, UpstreamModel: "m",
		}),
		"前缀猜测": WithCompositeRouteDecision(context.Background(), CompositeRouteDecision{
			Matched: true, Source: CompositeRouteSourceDetector, GroupID: 1, PublicModel: "m", TargetPlatform: PlatformKimi, UpstreamModel: "m",
		}),
	}
	for name, ctx := range cases {
		t.Run(name, func(t *testing.T) {
			svc, f := newTransportTimeoutService()
			c, _ := newOpenAITransportErrTestContext()
			_ = svc.handleOpenAIUpstreamTransportError(ctx, c, cooldownTestAccount(), timeoutErr, false)
			require.Equal(t, 0, f.repo.tempCalls)
			require.Equal(t, 0, f.cache.setCalls)
			require.Equal(t, 0, f.blocker.calls)
		})
	}
}

func TestCompositeTransportTimeoutCooldownSwitchOff(t *testing.T) {
	t.Setenv(compositeFailureCooldownEnv, "false")
	svc, f := newTransportTimeoutService()
	c, _ := newOpenAITransportErrTestContext()
	retErr := svc.handleOpenAIUpstreamTransportError(compositeAutoCtx(), c, cooldownTestAccount(), timeoutNetError{"i/o timeout"}, false)
	var fo *UpstreamFailoverError
	require.True(t, errors.As(retErr, &fo), "关闭开关后仍按原规则换号")
	require.Equal(t, 0, f.repo.tempCalls)
	require.Equal(t, 0, f.cache.setCalls)
	require.Equal(t, 0, f.blocker.calls)
}

func TestCompositeTransportTimeoutCooldownDoesNotShortenExistingBlock(t *testing.T) {
	later := time.Now().Add(10 * time.Minute)
	timeoutErr := timeoutNetError{"i/o timeout"}
	for name, mutate := range map[string]func(*Account){
		"已有更长的临时不可调度": func(a *Account) { a.TempUnschedulableUntil = &later },
		"已有更长的限流窗口":   func(a *Account) { a.RateLimitResetAt = &later },
		"已有更长的过载窗口":   func(a *Account) { a.OverloadUntil = &later },
	} {
		t.Run(name+"（调用方快照）", func(t *testing.T) {
			_, f := newTransportTimeoutService()
			account := cooldownTestAccount()
			mutate(account)
			require.False(t, f.service.ApplyCompositeTransportTimeoutCooldown(compositeAutoCtx(), account, timeoutErr))
			require.Equal(t, 0, f.repo.tempCalls)
			require.Equal(t, 0, f.cache.setCalls)
		})
	}
	t.Run("库里最新账号已有更长冷却", func(t *testing.T) {
		_, f := newTransportTimeoutService()
		fresh := cooldownTestAccount()
		fresh.TempUnschedulableUntil = &later
		f.repo.fresh = fresh
		require.False(t, f.service.ApplyCompositeTransportTimeoutCooldown(compositeAutoCtx(), cooldownTestAccount(), timeoutErr))
		require.Equal(t, 0, f.repo.tempCalls)
	})
	t.Run("已有更短的冷却会被延长到 30 秒", func(t *testing.T) {
		_, f := newTransportTimeoutService()
		account := cooldownTestAccount()
		shorter := time.Now().Add(5 * time.Second)
		account.TempUnschedulableUntil = &shorter
		require.True(t, f.service.ApplyCompositeTransportTimeoutCooldown(compositeAutoCtx(), account, timeoutErr))
		require.True(t, f.repo.lastUntil.After(shorter))
	})
}

func TestCompositeTransportTimeoutCooldownNilReceiversAreSafe(t *testing.T) {
	var s *RateLimitService
	require.False(t, s.ApplyCompositeTransportTimeoutCooldown(compositeAutoCtx(), cooldownTestAccount(), timeoutNetError{"i/o timeout"}))
	require.False(t, s.ApplyCompositeFirstOutputTimeoutCooldown(compositeAutoCtx(), cooldownTestAccount(), "response_headers"))
	f := newCooldownFixture()
	require.False(t, f.service.ApplyCompositeTransportTimeoutCooldown(compositeAutoCtx(), nil, timeoutNetError{"i/o timeout"}))
	// 没有 rateLimitService 的网关服务走传输层入口不应 panic，也不冷却。
	svc := &OpenAIGatewayService{}
	c, _ := newOpenAITransportErrTestContext()
	require.NotPanics(t, func() {
		_ = svc.handleOpenAIUpstreamTransportError(compositeAutoCtx(), c, cooldownTestAccount(), timeoutNetError{"i/o timeout"}, false)
	})
}

func TestCompositeFirstOutputTimeoutCooldown(t *testing.T) {
	for _, phase := range []string{"response_headers", "semantic_output"} {
		t.Run("Composite 自动路由_"+phase, func(t *testing.T) {
			svc, f := newTransportTimeoutService()
			c, _ := newOpenAITransportErrTestContext()
			account := cooldownTestAccount()
			before := time.Now()
			fo := svc.newOpenAIFirstOutputTimeoutError(compositeAutoCtx(), c, account, nil, "", before, "m", "", time.Second, phase, nil)
			require.Equal(t, http.StatusGatewayTimeout, fo.StatusCode, "原有失败切换结果不变")
			require.Equal(t, 1, f.repo.tempCalls)
			require.WithinDuration(t, before.Add(30*time.Second), f.repo.lastUntil, 2*time.Second)
			require.Equal(t, http.StatusGatewayTimeout, f.cache.lastState.StatusCode)
			require.Equal(t, compositeFailureCooldownMarker, f.cache.lastState.MatchedKeyword)
		})
	}
	t.Run("非 Composite 请求不冷却", func(t *testing.T) {
		svc, f := newTransportTimeoutService()
		c, _ := newOpenAITransportErrTestContext()
		_ = svc.newOpenAIFirstOutputTimeoutError(context.Background(), c, cooldownTestAccount(), nil, "", time.Now(), "m", "", time.Second, "response_headers", nil)
		require.Equal(t, 0, f.repo.tempCalls)
		require.Equal(t, 0, f.cache.setCalls)
	})
	t.Run("开关关闭不冷却", func(t *testing.T) {
		t.Setenv(compositeFailureCooldownEnv, "off")
		svc, f := newTransportTimeoutService()
		c, _ := newOpenAITransportErrTestContext()
		_ = svc.newOpenAIFirstOutputTimeoutError(compositeAutoCtx(), c, cooldownTestAccount(), nil, "", time.Now(), "m", "", time.Second, "response_headers", nil)
		require.Equal(t, 0, f.repo.tempCalls)
	})
	t.Run("已有更长冷却不缩短", func(t *testing.T) {
		svc, f := newTransportTimeoutService()
		c, _ := newOpenAITransportErrTestContext()
		later := time.Now().Add(10 * time.Minute)
		account := cooldownTestAccount()
		account.TempUnschedulableUntil = &later
		_ = svc.newOpenAIFirstOutputTimeoutError(compositeAutoCtx(), c, account, nil, "", time.Now(), "m", "", time.Second, "response_headers", nil)
		require.Equal(t, 0, f.repo.tempCalls)
	})
}
