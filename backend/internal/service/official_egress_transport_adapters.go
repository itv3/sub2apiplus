package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/proxyurl"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	openaiwsv2 "github.com/Wei-Shaw/sub2api/internal/service/openai_ws_v2"
	coderws "github.com/coder/websocket"
)

// officialEgressWebSocketRoundTripper 在 coder/websocket 生成握手请求后补齐
// 官方 Codex 的压缩扩展提议；底层压缩协商和帧处理仍由 coder/websocket 负责。
type officialEgressWebSocketRoundTripper struct {
	base http.RoundTripper
}

var guardedOpenAIWSDefaultClient = &http.Client{
	Transport: officialegress.NewGuardedRoundTripper(
		http.DefaultTransport,
		nil,
		officialegress.BackendWebSocket,
		officialegress.WireProtocolWebSocket,
	),
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

func buildOpenAIOfficialEgressWSTransport(
	profile *tlsfingerprint.Profile,
	rawProxyURL string,
) (*http.Transport, error) {
	if profile == nil {
		return nil, errors.New("official egress WebSocket TLS profile is nil")
	}
	transport := &http.Transport{
		MaxIdleConns:        openAIWSProxyTransportMaxIdleConns,
		MaxIdleConnsPerHost: openAIWSProxyTransportMaxIdleConnsPerHost,
		IdleConnTimeout:     openAIWSProxyTransportIdleConnTimeout,
		TLSHandshakeTimeout: 10 * time.Second,
		ForceAttemptHTTP2:   false,
		DisableCompression:  profile.Transport.DisableCompression,
	}
	_, parsedProxyURL, err := proxyurl.Parse(rawProxyURL)
	if err != nil {
		return nil, err
	}
	if parsedProxyURL == nil {
		transport.DialTLSContext = tlsfingerprint.NewDialer(profile, nil).DialTLSContext
		return transport, nil
	}
	switch strings.ToLower(parsedProxyURL.Scheme) {
	case "http", "https":
		transport.DialTLSContext = tlsfingerprint.NewHTTPProxyDialer(
			profile,
			parsedProxyURL,
		).DialTLSContext
	case "socks5", "socks5h":
		transport.DialTLSContext = tlsfingerprint.NewSOCKS5ProxyDialer(
			profile,
			parsedProxyURL,
		).DialTLSContext
	default:
		return nil, fmt.Errorf(
			"official egress WebSocket proxy scheme is unsupported: %s",
			parsedProxyURL.Scheme,
		)
	}
	return transport, nil
}

func (d *coderOpenAIWSClientDialer) compiledOfficialEgressHTTPClient(
	compiled officialCompiledWSTransport,
	proxyURL string,
) (*http.Client, error) {
	if d == nil || strings.TrimSpace(compiled.poolDigest) == "" {
		return nil, errors.New("compiled WebSocket transport 未初始化")
	}
	transportSpec := compiled.transport
	if transportSpec.Protocol != officialegress.WireProtocolWebSocket ||
		transportSpec.Backend != officialegress.BackendWebSocket {
		return nil, errors.New("compiled WebSocket TransportSpec 身份不符")
	}
	tlsProfile, err := tlsFingerprintProfileFromTransportSpec(transportSpec)
	if err != nil {
		return nil, err
	}
	cacheKey := "official:" + compiled.poolDigest +
		"|proxy_state=" + officialEgressProxyStateKey(strings.TrimSpace(proxyURL))
	now := time.Now().UnixNano()
	d.proxyMu.Lock()
	defer d.proxyMu.Unlock()
	if entry, ok := d.proxyClients[cacheKey]; ok && entry != nil && entry.client != nil {
		entry.lastUsedUnixNano = now
		d.proxyHits.Add(1)
		return entry.client, nil
	}
	d.cleanupProxyClientsLocked(now)
	transport, err := buildOpenAIOfficialEgressWSTransport(tlsProfile, proxyURL)
	if err != nil {
		return nil, err
	}
	guardedTransport := officialegress.NewGuardedRoundTripper(
		transport, nil, officialegress.BackendWebSocket, officialegress.WireProtocolWebSocket,
	)
	client := &http.Client{
		Transport:     &officialEgressWebSocketRoundTripper{base: guardedTransport},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	d.proxyClients[cacheKey] = &openAIWSProxyClientEntry{client: client, lastUsedUnixNano: now}
	d.ensureProxyClientCapacityLocked()
	d.proxyMisses.Add(1)
	return client, nil
}

func (d *coderOpenAIWSClientDialer) officialEgressHTTPClient(
	egressContext *OfficialEgressContext,
	targetURL string,
	proxyURL string,
) (*http.Client, error) {
	if d == nil {
		return nil, errors.New("openai ws dialer is nil")
	}
	if egressContext == nil || !egressContext.frozen {
		return nil, errors.New("official egress WebSocket context must be frozen before dialing")
	}
	parsedTarget, err := url.Parse(strings.TrimSpace(targetURL))
	if err != nil {
		return nil, fmt.Errorf("invalid official egress WebSocket URL: %w", err)
	}
	if !strings.EqualFold(parsedTarget.Scheme, "wss") ||
		normalizeOfficialEgressHost(parsedTarget.Host) != egressContext.upstreamHost {
		return nil, errors.New("official egress WebSocket URL conflicts with frozen context")
	}
	tlsProfile, err := resolveOfficialEgressWebSocketTransportProfile(egressContext)
	if err != nil {
		return nil, err
	}
	cacheKey := "official:" + egressContext.connectionPoolID +
		"|proxy_state=" + officialEgressProxyStateKey(strings.TrimSpace(proxyURL))
	now := time.Now().UnixNano()

	d.proxyMu.Lock()
	defer d.proxyMu.Unlock()
	if entry, ok := d.proxyClients[cacheKey]; ok && entry != nil && entry.client != nil {
		entry.lastUsedUnixNano = now
		d.proxyHits.Add(1)
		return entry.client, nil
	}
	d.cleanupProxyClientsLocked(now)
	transport, err := buildOpenAIOfficialEgressWSTransport(tlsProfile, proxyURL)
	if err != nil {
		return nil, err
	}
	guardedTransport := officialegress.NewGuardedRoundTripper(
		transport,
		nil,
		officialegress.BackendWebSocket,
		officialegress.WireProtocolWebSocket,
	)
	client := &http.Client{
		// 压缩扩展先完成最终 wire 变换，内层 Guard 再读取变换后的握手。
		Transport: &officialEgressWebSocketRoundTripper{base: guardedTransport},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	d.proxyClients[cacheKey] = &openAIWSProxyClientEntry{
		client:           client,
		lastUsedUnixNano: now,
	}
	d.ensureProxyClientCapacityLocked()
	d.proxyMisses.Add(1)
	return client, nil
}

func (r *officialEgressWebSocketRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if r == nil || r.base == nil {
		return nil, errors.New("official egress WebSocket transport is nil")
	}
	if req == nil {
		return nil, errors.New("official egress WebSocket request is nil")
	}
	cloned := req.Clone(req.Context())
	cloned.Header = cloneHeader(req.Header)
	if strings.EqualFold(strings.TrimSpace(cloned.Header.Get("Upgrade")), "websocket") &&
		strings.EqualFold(
			strings.TrimSpace(cloned.Header.Get("Sec-WebSocket-Extensions")),
			"permessage-deflate",
		) {
		cloned.Header.Set("Sec-WebSocket-Extensions", officialOpenAIWSCompressionOffer)
	}
	return r.base.RoundTrip(cloned)
}

func (r *officialEgressWebSocketRoundTripper) CloseIdleConnections() {
	if r == nil || r.base == nil {
		return
	}
	if closer, ok := r.base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}

// OfficialCodexReqProfileTransportResource 是 repository 向受信 adapter 暴露的
// 最小物理资源。它不解析 Bundle、Sink 或 persona，只消费已经签名的请求与画像。
type OfficialCodexReqProfileTransportResource interface {
	SendOfficialCodexReqProfile(
		ctx context.Context,
		request *http.Request,
		transport officialegress.TransportSpec,
		proxyURL string,
	) (*http.Response, error)
}

type officialCodexReqProfileTransportInput struct {
	proxyURL string
}

type officialCodexReqProfileTransportContextKey struct{}

func withOfficialCodexReqProfileTransport(
	ctx context.Context,
	proxyURL string,
) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, officialCodexReqProfileTransportContextKey{},
		officialCodexReqProfileTransportInput{proxyURL: strings.TrimSpace(proxyURL)})
}

type officialCodexReqProfilePort struct {
	resource OfficialCodexReqProfileTransportResource
}

func (p *officialCodexReqProfilePort) SendReqProfile(
	ctx context.Context,
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	if p == nil || p.resource == nil {
		return nil, errors.New("Codex Executor req-profile 资源未注入")
	}
	input, ok := ctx.Value(officialCodexReqProfileTransportContextKey{}).(officialCodexReqProfileTransportInput)
	if !ok {
		return nil, errors.New("Codex Executor req-profile 缺少传输上下文")
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	return p.resource.SendOfficialCodexReqProfile(ctx, request, prepared.Transport(), input.proxyURL)
}

// OfficialCodexWebSocketAcquirer 是 WebSocket adapter 的逻辑 Acquire 边界。
// 实现必须在复用连接和新拨号两条路径都验证当前 PreparedRequest 的 Token。
type OfficialCodexWebSocketAcquirer interface {
	AcquireOfficialCodexWebSocket(
		ctx context.Context,
		request officialegress.PreparedRequest,
	) (officialegress.WebSocketConnection, error)
}

type officialCodexWebSocketPort struct {
	mu       sync.RWMutex
	acquirer OfficialCodexWebSocketAcquirer
}

type officialCodexWebSocketAcquireInput struct {
	pool        *openAIWSConnPool
	poolRequest openAIWSAcquireRequest
	dialer      openAIWSClientDialer
	proxyURL    string
	guard       openAIWSAdmissionGuard
}

type officialCodexWebSocketAcquireContextKey struct{}

// officialCompiledWSTransport 是 Executor WebSocket adapter 交给终端拨号器的
// 可信传输事实。业务层和连接池只能透传，不能重组其中的 TransportSpec。
type officialCompiledWSTransportContextKey struct{}

type officialCompiledWSTransport struct {
	ctx        context.Context
	target     string
	headers    http.Header
	transport  officialegress.TransportSpec
	poolDigest string
}

func withOfficialCodexWebSocketAcquire(
	ctx context.Context,
	input officialCodexWebSocketAcquireInput,
) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, officialCodexWebSocketAcquireContextKey{}, input)
}

// officialCodexWebSocketAcquireRouter 是生产 WebSocket adapter 的逻辑 Acquire
// 入口。连接池复用与直接拨号都必须先消费当前 PreparedRequest 并执行连接准入。
type officialCodexWebSocketAcquireRouter struct{}

func (officialCodexWebSocketAcquireRouter) AcquireOfficialCodexWebSocket(
	ctx context.Context,
	prepared officialegress.PreparedRequest,
) (officialegress.WebSocketConnection, error) {
	input, ok := ctx.Value(officialCodexWebSocketAcquireContextKey{}).(officialCodexWebSocketAcquireInput)
	if !ok || input.pool == nil && input.dialer == nil {
		return nil, errors.New("Codex Executor WebSocket 缺少 Acquire 目标")
	}
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	transport := prepared.Transport()
	if transport.Backend != officialegress.BackendWebSocket ||
		transport.Protocol != officialegress.WireProtocolWebSocket {
		return nil, errors.New("Codex Executor WebSocket transport 身份不符")
	}
	identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
	if !ok || !identity.HasFinalizationToken || identity.AttemptOrdinal == 0 {
		return nil, errors.New("Codex Executor WebSocket Acquire 缺少当前 attempt token")
	}
	var guard openAIWSAdmissionGuard
	if input.pool != nil && input.pool.guard != nil {
		guard = input.pool.guard
	}
	if guard == nil {
		guard = input.guard
	}
	if guard == nil {
		guard = officialegress.DefaultGuard()
	}
	decision := guard.EvaluateConnectionAdmission(
		request, officialegress.BackendWebSocket, officialegress.WireProtocolWebSocket,
	)
	if !decision.Allow {
		return nil, officialegress.WrapRuntimeError(
			officialegress.RuntimeErrorCodeGuardRejected,
			"websocket.guard_admission",
			&officialegress.GuardRejectionError{
				Reason: decision.RejectionReason, SinkID: identity.SinkID, Route: decision.Route.Key,
			},
		)
	}
	compiled := officialCompiledWSTransport{
		ctx: request.Context(), target: request.URL.String(), headers: request.Header.Clone(),
		transport: transport.Clone(), poolDigest: transport.ConnectionPoolDigest,
	}
	acquireContext := context.WithValue(
		request.Context(), officialCompiledWSTransportContextKey{}, compiled,
	)
	if input.pool != nil {
		poolRequest := cloneOpenAIWSAcquireRequest(input.poolRequest)
		poolRequest.WSURL = request.URL.String()
		poolRequest.Headers = request.Header.Clone()
		poolRequest.ProxyURL = strings.TrimSpace(input.proxyURL)
		poolRequest.SinkID = identity.SinkID
		poolRequest.TransportKey = transport.ConnectionPoolDigest +
			"|proxy_state=" + officialEgressProxyStateKey(poolRequest.ProxyURL)
		poolRequest.HeadersFactory = nil
		poolRequest.OfficialEgressContext = nil
		poolRequest.AdmissionGuard = guard
		poolRequest.TokenBoundAcquire = true
		lease, err := input.pool.Acquire(acquireContext, poolRequest)
		if err != nil {
			return nil, err
		}
		return &officialCodexPooledWebSocketConnection{lease: lease}, nil
	}
	connection, status, responseHeaders, err := input.dialer.Dial(
		acquireContext, request.URL.String(), request.Header.Clone(), strings.TrimSpace(input.proxyURL),
	)
	if err != nil {
		return nil, &openAIWSDialError{
			StatusCode: status, ResponseHeaders: cloneHeader(responseHeaders), Err: err,
		}
	}
	if connection == nil {
		return nil, errors.New("Codex Executor WebSocket dialer 返回空连接")
	}
	return &officialCodexDirectWebSocketConnection{
		connection: connection, status: status, responseHeaders: cloneHeader(responseHeaders),
	}, nil
}

type officialCodexPooledWebSocketConnection struct {
	lease *openAIWSConnLease
}

func (c *officialCodexPooledWebSocketConnection) ReadMessage(ctx context.Context) ([]byte, error) {
	if c == nil || c.lease == nil {
		return nil, errOpenAIWSConnClosed
	}
	return c.lease.ReadMessageContext(ctx)
}

func (c *officialCodexPooledWebSocketConnection) ReadWebSocketFrame(
	ctx context.Context,
) (officialegress.WebSocketFrameType, []byte, error) {
	payload, err := c.ReadMessage(ctx)
	return officialegress.WebSocketFrameText, payload, err
}

func (c *officialCodexPooledWebSocketConnection) WriteMessage(
	ctx context.Context,
	payload []byte,
) error {
	if c == nil || c.lease == nil {
		return errOpenAIWSConnClosed
	}
	return c.lease.WriteJSONContext(ctx, json.RawMessage(append([]byte(nil), payload...)))
}

func (c *officialCodexPooledWebSocketConnection) Close() error {
	if c != nil && c.lease != nil {
		c.lease.Release()
	}
	return nil
}

func (c *officialCodexPooledWebSocketConnection) ConnectionID() string {
	if c == nil || c.lease == nil {
		return ""
	}
	return c.lease.ConnID()
}

func (c *officialCodexPooledWebSocketConnection) HandshakeStatus() int {
	return http.StatusSwitchingProtocols
}

func (c *officialCodexPooledWebSocketConnection) QueueWaitDuration() time.Duration {
	if c == nil || c.lease == nil {
		return 0
	}
	return c.lease.QueueWaitDuration()
}

func (c *officialCodexPooledWebSocketConnection) ConnectionPickDuration() time.Duration {
	if c == nil || c.lease == nil {
		return 0
	}
	return c.lease.ConnPickDuration()
}

func (c *officialCodexPooledWebSocketConnection) Reused() bool {
	return c != nil && c.lease != nil && c.lease.Reused()
}

func (c *officialCodexPooledWebSocketConnection) HandshakeHeader(name string) string {
	if c == nil || c.lease == nil {
		return ""
	}
	return c.lease.HandshakeHeader(name)
}

func (c *officialCodexPooledWebSocketConnection) HandshakeHeaders() http.Header {
	if c == nil || c.lease == nil {
		return nil
	}
	return c.lease.HandshakeHeaders()
}

func (c *officialCodexPooledWebSocketConnection) IsPrewarmed() bool {
	return c != nil && c.lease != nil && c.lease.IsPrewarmed()
}

func (c *officialCodexPooledWebSocketConnection) MarkPrewarmed() {
	if c != nil && c.lease != nil {
		c.lease.MarkPrewarmed()
	}
}

func (c *officialCodexPooledWebSocketConnection) MarkUnusable() {
	if c != nil && c.lease != nil {
		c.lease.MarkBroken()
	}
}

func (c *officialCodexPooledWebSocketConnection) Ping(ctx context.Context) error {
	if c == nil || c.lease == nil {
		return errOpenAIWSConnClosed
	}
	timeout := openAIWSConnHealthCheckTO
	if deadline, ok := ctx.Deadline(); ok {
		timeout = time.Until(deadline)
	}
	return c.lease.PingWithTimeout(timeout)
}

func (c *officialCodexPooledWebSocketConnection) SupportsIdlePingWithoutReader() bool {
	return c != nil && c.lease != nil && c.lease.SupportsIdlePingWithoutReader()
}

type officialCodexDirectWebSocketConnection struct {
	connection      openAIWSClientConn
	status          int
	responseHeaders http.Header
}

func (c *officialCodexDirectWebSocketConnection) ReadMessage(ctx context.Context) ([]byte, error) {
	if c == nil || c.connection == nil {
		return nil, errOpenAIWSConnClosed
	}
	return c.connection.ReadMessage(ctx)
}

func (c *officialCodexDirectWebSocketConnection) ReadWebSocketFrame(
	ctx context.Context,
) (officialegress.WebSocketFrameType, []byte, error) {
	if c == nil || c.connection == nil {
		return officialegress.WebSocketFrameText, nil, errOpenAIWSConnClosed
	}
	if frames, ok := c.connection.(openaiwsv2.FrameConn); ok {
		messageType, payload, err := frames.ReadFrame(ctx)
		if messageType == coderws.MessageBinary {
			return officialegress.WebSocketFrameBinary, payload, err
		}
		return officialegress.WebSocketFrameText, payload, err
	}
	payload, err := c.connection.ReadMessage(ctx)
	return officialegress.WebSocketFrameText, payload, err
}

func (c *officialCodexDirectWebSocketConnection) WriteMessage(
	ctx context.Context,
	payload []byte,
) error {
	if c == nil || c.connection == nil {
		return errOpenAIWSConnClosed
	}
	return c.connection.WriteJSON(ctx, json.RawMessage(append([]byte(nil), payload...)))
}

func (c *officialCodexDirectWebSocketConnection) WriteWebSocketFrame(
	ctx context.Context,
	frameType officialegress.WebSocketFrameType,
	payload []byte,
) error {
	if c == nil || c.connection == nil {
		return errOpenAIWSConnClosed
	}
	if frames, ok := c.connection.(openaiwsv2.FrameConn); ok {
		messageType := coderws.MessageText
		if frameType == officialegress.WebSocketFrameBinary {
			messageType = coderws.MessageBinary
		}
		return frames.WriteFrame(ctx, messageType, append([]byte(nil), payload...))
	}
	if frameType == officialegress.WebSocketFrameBinary {
		return errors.New("Codex direct WebSocket transport 不支持二进制帧")
	}
	return c.WriteMessage(ctx, payload)
}

func (c *officialCodexDirectWebSocketConnection) Close() error {
	if c == nil || c.connection == nil {
		return nil
	}
	return c.connection.Close()
}

func (c *officialCodexDirectWebSocketConnection) ConnectionID() string { return "" }
func (c *officialCodexDirectWebSocketConnection) HandshakeStatus() int {
	if c == nil {
		return 0
	}
	return c.status
}
func (c *officialCodexDirectWebSocketConnection) QueueWaitDuration() time.Duration {
	return 0
}
func (c *officialCodexDirectWebSocketConnection) ConnectionPickDuration() time.Duration {
	return 0
}
func (c *officialCodexDirectWebSocketConnection) Reused() bool { return false }
func (c *officialCodexDirectWebSocketConnection) HandshakeHeader(name string) string {
	if c == nil {
		return ""
	}
	return c.responseHeaders.Get(name)
}
func (c *officialCodexDirectWebSocketConnection) HandshakeHeaders() http.Header {
	if c == nil {
		return nil
	}
	return cloneHeader(c.responseHeaders)
}
func (c *officialCodexDirectWebSocketConnection) IsPrewarmed() bool { return false }
func (c *officialCodexDirectWebSocketConnection) MarkPrewarmed()    {}
func (c *officialCodexDirectWebSocketConnection) MarkUnusable() {
	if c != nil && c.connection != nil {
		_ = c.connection.Close()
	}
}
func (c *officialCodexDirectWebSocketConnection) Ping(ctx context.Context) error {
	if c == nil || c.connection == nil {
		return errOpenAIWSConnClosed
	}
	return c.connection.Ping(ctx)
}
func (c *officialCodexDirectWebSocketConnection) SupportsIdlePingWithoutReader() bool {
	if c == nil || c.connection == nil {
		return false
	}
	capable, ok := c.connection.(openAIWSIdlePingCapable)
	return ok && capable.SupportsIdlePingWithoutReader()
}

type executorWebSocketLeaseSession struct {
	session *officialegress.ExecutorWebSocketSession
}

// executorWebSocketFrameSession 是 direct 正式 Codex 路径唯一可见的帧接口。
// WriteFrame 只是 relay 兼容壳，内部仍必须先取得一次性 PreparedWebSocketFrame。
type executorWebSocketFrameSession struct {
	session *officialegress.ExecutorWebSocketSession
}

var _ openaiwsv2.FrameConn = (*executorWebSocketFrameSession)(nil)

func (s *executorWebSocketFrameSession) ReadFrame(
	ctx context.Context,
) (coderws.MessageType, []byte, error) {
	frameType, payload, err := s.session.ReadFrame(ctx)
	if frameType == officialegress.WebSocketFrameBinary {
		return coderws.MessageBinary, payload, err
	}
	return coderws.MessageText, payload, err
}

func (s *executorWebSocketFrameSession) PrepareFrame(
	messageType coderws.MessageType,
	payload []byte,
) (officialegress.PreparedWebSocketFrame, error) {
	frameType := officialegress.WebSocketFrameText
	if messageType == coderws.MessageBinary {
		frameType = officialegress.WebSocketFrameBinary
		return s.session.PrepareFrame(officialegress.WebSocketFramePlan{
			Type: frameType, EventType: "binary.transparent", Payload: append([]byte(nil), payload...),
			IdentityFacts: s.session.IdentityFacts(),
		})
	}
	semantic, eventType, facts, bodyConditions, err := prepareOfficialCodexExecutorWebSocketFrame(s.session, payload)
	if err != nil {
		return officialegress.PreparedWebSocketFrame{}, err
	}
	return s.session.PrepareFrame(officialegress.WebSocketFramePlan{
		Type: frameType, EventType: eventType, Body: semantic, IdentityFacts: facts,
		BodyConditions: bodyConditions,
	})
}

func (s *executorWebSocketFrameSession) WritePreparedFrame(
	ctx context.Context,
	frame officialegress.PreparedWebSocketFrame,
) error {
	return s.session.WritePreparedFrame(ctx, frame)
}

func (s *executorWebSocketFrameSession) WriteFrame(
	ctx context.Context,
	messageType coderws.MessageType,
	payload []byte,
) error {
	prepared, err := s.PrepareFrame(messageType, payload)
	if err != nil {
		return err
	}
	return s.WritePreparedFrame(ctx, prepared)
}

func (s *executorWebSocketFrameSession) Ping(ctx context.Context) error {
	return s.session.Ping(ctx)
}

func (s *executorWebSocketFrameSession) Close() error { return s.session.Close() }

func (s *executorWebSocketLeaseSession) ConnID() string { return s.session.ConnectionID() }
func (s *executorWebSocketLeaseSession) QueueWaitDuration() time.Duration {
	return s.session.QueueWaitDuration()
}
func (s *executorWebSocketLeaseSession) ConnPickDuration() time.Duration {
	return s.session.ConnectionPickDuration()
}
func (s *executorWebSocketLeaseSession) Reused() bool { return s.session.Reused() }
func (s *executorWebSocketLeaseSession) HandshakeHeader(name string) string {
	return s.session.HandshakeHeader(name)
}
func (s *executorWebSocketLeaseSession) HandshakeHeaders() http.Header {
	return s.session.HandshakeHeaders()
}
func (s *executorWebSocketLeaseSession) IsPrewarmed() bool { return s.session.IsPrewarmed() }
func (s *executorWebSocketLeaseSession) MarkPrewarmed()    { s.session.MarkPrewarmed() }
func (s *executorWebSocketLeaseSession) MarkBroken()       { s.session.MarkUnusable() }
func (s *executorWebSocketLeaseSession) Release()          { _ = s.session.Close() }
func (s *executorWebSocketLeaseSession) SupportsIdlePingWithoutReader() bool {
	return s.session.SupportsIdlePingWithoutReader()
}

func (s *executorWebSocketLeaseSession) PingWithTimeout(timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return s.session.Ping(ctx)
}

func (s *executorWebSocketLeaseSession) ReadMessageWithContextTimeout(
	ctx context.Context,
	timeout time.Duration,
) ([]byte, error) {
	readContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return s.session.ReadMessage(readContext)
}

func (s *executorWebSocketLeaseSession) WriteSemanticJSONWithContextTimeout(
	ctx context.Context,
	value any,
	timeout time.Duration,
) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	semantic, eventType, facts, bodyConditions, err := prepareOfficialCodexExecutorWebSocketFrame(s.session, payload)
	if err != nil {
		return err
	}
	prepared, err := s.session.PrepareFrame(officialegress.WebSocketFramePlan{
		Type: officialegress.WebSocketFrameText, EventType: eventType,
		Body: semantic, IdentityFacts: facts, BodyConditions: bodyConditions,
	})
	if err != nil {
		return err
	}
	writeContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return s.session.WritePreparedFrame(writeContext, prepared)
}

func prepareOfficialCodexExecutorWebSocketFrame(
	session *officialegress.ExecutorWebSocketSession,
	payload []byte,
) (officialegress.RequestBody, string, officialegress.CodexIdentityFacts, officialegress.BodyRuntimeConditions, error) {
	if session == nil {
		return officialegress.RequestBody{}, "", officialegress.CodexIdentityFacts{}, officialegress.BodyRuntimeConditions{}, errors.New("Codex Executor WebSocket session 为空")
	}
	body, ownedFields, err := officialegress.PrepareOfficialCodexAttemptBody("responses_ws", payload)
	if err != nil {
		return officialegress.RequestBody{}, "", officialegress.CodexIdentityFacts{}, officialegress.BodyRuntimeConditions{}, err
	}
	facts := session.IdentityFacts()
	bodyConditions := officialegress.BodyRuntimeConditions{
		PreviousResponseIDReusable: ownedFields.PreviousResponseIDReusable,
	}
	setFact := func(target *officialegress.CodexIdentityValue, name string, source officialegress.IdentityFactSource, lifecycle officialegress.IdentityFactLifecycle) error {
		value := strings.TrimSpace(ownedFields.Metadata[name])
		if value == "" {
			return nil
		}
		fact, err := officialegress.NewCodexIdentityValue(value, source, lifecycle)
		if err != nil {
			return err
		}
		*target = fact
		return nil
	}
	for _, field := range []struct {
		target    *officialegress.CodexIdentityValue
		name      string
		source    officialegress.IdentityFactSource
		lifecycle officialegress.IdentityFactLifecycle
	}{
		{&facts.InstallationID, "x-codex-installation-id", officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleInvocation},
		{&facts.SessionID, "session_id", officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.ThreadID, "thread_id", officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.WindowID, "x-codex-window-id", officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.TurnID, "turn_id", officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		{&facts.TurnMetadata, "x-codex-turn-metadata", officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
		{&facts.ParentThreadID, "x-codex-parent-thread-id", officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession},
		{&facts.Subagent, "x-openai-subagent", officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn},
	} {
		if err := setFact(field.target, field.name, field.source, field.lifecycle); err != nil {
			return officialegress.RequestBody{}, "", officialegress.CodexIdentityFacts{}, officialegress.BodyRuntimeConditions{}, err
		}
	}
	turnState := strings.TrimSpace(ownedFields.Metadata["x-codex-turn-state"])
	if turnState != "" {
		facts.TurnState, err = officialegress.NewCodexTurnStateValue(turnState)
		if err != nil {
			return officialegress.RequestBody{}, "", officialegress.CodexIdentityFacts{}, officialegress.BodyRuntimeConditions{}, err
		}
	}
	facts.Conditions.SessionIDPresent = facts.SessionID.Value != ""
	facts.Conditions.TurnStatePresent = facts.TurnState.Value != ""
	facts.Conditions.ParentThreadPresent = facts.ParentThreadID.Value != ""
	facts.Conditions.SubagentPresent = facts.Subagent.Value != ""
	return body, ownedFields.EventType, facts, bodyConditions, nil
}

func (p *officialCodexWebSocketPort) Bind(acquirer OfficialCodexWebSocketAcquirer) error {
	if p == nil || acquirer == nil {
		return errors.New("Codex Executor WebSocket acquirer 为空")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.acquirer != nil {
		if _, replaceable := p.acquirer.(officialCodexWebSocketAcquireRouter); !replaceable {
			return errors.New("Codex Executor WebSocket acquirer 已绑定")
		}
	}
	p.acquirer = acquirer
	return nil
}

func (p *officialCodexWebSocketPort) AcquireWebSocket(
	ctx context.Context,
	request officialegress.PreparedRequest,
) (officialegress.WebSocketConnection, error) {
	if p == nil {
		return nil, errors.New("Codex Executor WebSocket port 未初始化")
	}
	p.mu.RLock()
	acquirer := p.acquirer
	p.mu.RUnlock()
	if acquirer == nil {
		return nil, errors.New("Codex Executor WebSocket Acquire 边界尚未绑定")
	}
	return acquirer.AcquireOfficialCodexWebSocket(ctx, request)
}
