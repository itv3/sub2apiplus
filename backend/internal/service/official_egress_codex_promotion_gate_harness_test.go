package service

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha1" //nolint:gosec // RFC 6455 规定 Sec-WebSocket-Accept 用 SHA-1 计算，这里只为本地终端完成握手。
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

// ============================================================================
// 晋升后门禁的公共夹具（Codex 发布槽位）
//
// 门禁语义：VC-3 为本轮每条受影响规则生成一项门禁需求——“在晋升后的目标制品上
// 重放该规则的批准断言语义”。候选期目标画像装在 previous 槽位，VC-6 晋升后装在
// active 槽位；同一条测试必须在两个阶段都成立，所以一律按目标画像独有的结构事实
// 定位槽位，不按槽位名写死（先例：codexWhamGateReleaseMode）。
//
// 观测边界：请求经生产 service 构造、正式 Compiler/Executor 与生产 HTTPUpstream
// 端口定型后，交给本文件的捕获上游；捕获上游用 tlsfingerprint 按已定型 TLS 画像
// 真实拨号到本地 TLS 终端（SNI 仍是官方主机名，连接被重定向到 127.0.0.1），终端
// 记录 ClientHello 与解密后的 HTTP/1.1 原始字节。于是 wire 线序、SNI、签名算法、
// 扩展顺序都取自真实字节，而不是由测试按规则推导。整个过程不产生外部流量。
// ============================================================================

// codexGateTargetReleaseMode 在 previous/active 中找出唯一具备 feature 结构事实的
// 发布槽位，并返回该槽位名（"previous" 或 "active"）。
//
// 两种情形都必须失败而不是退回任一槽位：
//   - 两个槽位都不具备：目标制品没有入库，门禁无从重放；
//   - 两个槽位都具备：该事实已不能区分目标制品，门禁失去判别力（例如下一轮升级把
//     目标画像挤到 previous 后，本轮门禁应由新一轮门禁接替）。
//
// 直接读 officialegress 的正式 ReleaseCatalog，不经过 service 的包级画像入口，
// 避免被其他测试注入的合成画像影响。
func codexGateTargetReleaseMode(
	t *testing.T,
	feature string,
	holds func(profilecontract.ExecutableProfile) bool,
) string {
	t.Helper()
	var matched []string
	for _, mode := range []string{officialClientProfileModePrevious, officialClientProfileModeActive} {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
		if err != nil {
			continue
		}
		if holds(release.ExecutableProfile()) {
			matched = append(matched, mode)
		}
	}
	switch len(matched) {
	case 1:
		return matched[0]
	case 0:
		t.Fatalf("ReleaseCatalog 的 previous/active 都不具备结构事实「%s」：目标制品未入库", feature)
	default:
		t.Fatalf("previous 与 active 同时具备结构事实「%s」：无法区分目标制品，门禁失去判别力", feature)
	}
	return ""
}

// codexGateOtherReleaseMode 返回另一个发布槽位，用于“另一槽位不具备该事实”的对照。
func codexGateOtherReleaseMode(mode string) string {
	if mode == officialClientProfileModePrevious {
		return officialClientProfileModeActive
	}
	return officialClientProfileModePrevious
}

// codexGateExecutableProfile 读取指定槽位的正式可执行画像。
func codexGateExecutableProfile(t *testing.T, mode string) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
	require.NoError(t, err, "解析 %s 槽位的正式发布失败", mode)
	return release.ExecutableProfile()
}

// codexGateEndpoint 在可执行画像中按端点 ID 查找端点。
func codexGateEndpoint(
	profile profilecontract.ExecutableProfile,
	endpointID string,
) (profilecontract.ExecutableEndpointProfile, bool) {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == endpointID {
			return endpoint, true
		}
	}
	return profilecontract.ExecutableEndpointProfile{}, false
}

// codexGateMustEndpoint 与 codexGateEndpoint 相同，找不到时直接失败。
func codexGateMustEndpoint(
	t *testing.T,
	profile profilecontract.ExecutableProfile,
	endpointID string,
) profilecontract.ExecutableEndpointProfile {
	t.Helper()
	endpoint, ok := codexGateEndpoint(profile, endpointID)
	require.True(t, ok, "目标画像缺少端点 %s", endpointID)
	return endpoint
}

// codexGateHeaderSlot 在端点画像中按 header 名查找槽位。
func codexGateHeaderSlot(
	endpoint profilecontract.ExecutableEndpointProfile,
	name string,
) (profilecontract.HeaderSlotProfile, bool) {
	for _, slot := range endpoint.Headers {
		if strings.EqualFold(slot.Name, name) {
			return slot, true
		}
	}
	return profilecontract.HeaderSlotProfile{}, false
}

// codexGateSlotPrecedes 判断 before 槽位在画像线序上是否先于 after 槽位。
func codexGateSlotPrecedes(
	endpoint profilecontract.ExecutableEndpointProfile,
	before string,
	after string,
) bool {
	left, leftOK := codexGateHeaderSlot(endpoint, before)
	right, rightOK := codexGateHeaderSlot(endpoint, after)
	if !leftOK || !rightOK {
		return false
	}
	if left.Slot != right.Slot {
		return left.Slot < right.Slot
	}
	return left.Sequence < right.Sequence
}

// codexGateAcceptAfterIdentity 是“无显式 accept 的端点，accept 位于 originator、
// user-agent（及 residency）之后”这一结构事实（SPEC-HDR-002/H1-004/EP-015/EP-022
// 的画像补丁）。
func codexGateAcceptAfterIdentity(profile profilecontract.ExecutableProfile, endpointID string) bool {
	endpoint, ok := codexGateEndpoint(profile, endpointID)
	if !ok {
		return false
	}
	return codexGateSlotPrecedes(endpoint, "originator", "accept") &&
		codexGateSlotPrecedes(endpoint, "user-agent", "accept") &&
		codexGateSlotPrecedes(endpoint, "x-openai-internal-codex-residency", "accept")
}

// newCodexGateRuntime 以正式 ReleaseCatalog 构造指定槽位的官方出站 runtime：
// Compiler、Executor、Guard 与 HTTPUpstream 端口全部是生产实现，只有 HTTPUpstream
// 本身由测试提供。
func newCodexGateRuntime(
	t *testing.T,
	upstream HTTPUpstream,
	mode string,
) *OfficialEgressTransitionRuntime {
	t.Helper()
	base := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		base.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), base.Recorder(),
	)
	require.NoError(t, err)
	var reqProfile []OfficialCodexReqProfileTransportResource
	if resource, ok := upstream.(OfficialCodexReqProfileTransportResource); ok {
		reqProfile = append(reqProfile, resource)
	}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard, upstream, officialCodexExecutorID, officialegress.ReleaseMode(mode), reqProfile...,
	)
	require.NoError(t, err, "构造 %s 槽位的官方出站 runtime 失败", mode)
	return runtimeState
}

// ----------------------------------------------------------------------------
// 本地 TLS 终端
// ----------------------------------------------------------------------------

// codexGateClientHello 是本地终端在 TLS 握手时观测到的 ClientHello 事实。
type codexGateClientHello struct {
	connection       int
	serverName       string
	signatureSchemes []uint16
	extensions       []uint16
}

// codexGateWireFrame 是 WS 连接上客户端发出的一帧（已去掩码）。
type codexGateWireFrame struct {
	opcode  byte
	payload []byte
}

// codexGateWireRequest 是本地终端从解密后的 HTTP/1.1 字节中读到的一次请求。
type codexGateWireRequest struct {
	connection int
	sequence   int
	method     string
	target     string
	path       string
	// headerNames 保留 wire 上的原样名称（含大小写）与出现顺序，是线序断言的唯一依据。
	headerNames []string
	header      http.Header
	rawBody     []byte
	// body 在 content-encoding: zstd 时为解压后的 JSON 字节，便于断言字段。
	body      []byte
	webSocket bool
	frames    []codexGateWireFrame
}

// lowerHeaderNames 返回小写化后的 wire 线序。
func (r codexGateWireRequest) lowerHeaderNames() []string {
	out := make([]string, 0, len(r.headerNames))
	for _, name := range r.headerNames {
		out = append(out, strings.ToLower(name))
	}
	return out
}

// values 返回指定 header 在 wire 上的全部取值（按出现顺序）。
func (r codexGateWireRequest) values(name string) []string {
	return r.header.Values(name)
}

// has 判断 wire 上是否出现过指定 header。
func (r codexGateWireRequest) has(name string) bool {
	return len(r.header.Values(name)) > 0
}

// codexGateWireResponse 是本地终端对一次请求的应答。
type codexGateWireResponse struct {
	status int
	header http.Header
	body   []byte
	// closeAfter 为 true 时写完应答即关闭连接（服务端主动断开 keep-alive）。
	closeAfter bool
	// dropWithoutResponse 为 true 时读完请求直接断开，不写任何应答（模拟断连）。
	dropWithoutResponse bool
}

// codexGateJSONResponse 构造 200 JSON 应答。
func codexGateJSONResponse(body string) codexGateWireResponse {
	return codexGateWireResponse{
		status: http.StatusOK,
		header: http.Header{"Content-Type": []string{"application/json"}},
		body:   []byte(body),
	}
}

// codexGateSSECompletedResponse 构造 Responses 的最小 SSE 完成事件应答。
func codexGateSSECompletedResponse(responseID string, header http.Header) codexGateWireResponse {
	body := strings.Join([]string{
		`data: {"type":"response.completed","response":{"id":"` + responseID +
			`","object":"response","model":"gpt-5.6-luna","status":"completed","output":[],` +
			`"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	out := http.Header{"Content-Type": []string{"text/event-stream"}}
	for name, values := range header {
		out[name] = append([]string(nil), values...)
	}
	return codexGateWireResponse{status: http.StatusOK, header: out, body: []byte(body)}
}

// codexGateDefaultResponse 是终端的默认应答：Responses 返回 SSE 完成事件，模型清单
// 返回与生产同源的清单，其余端点返回空 JSON 对象。
func codexGateDefaultResponse(request codexGateWireRequest) codexGateWireResponse {
	switch {
	case request.path == "/backend-api/codex/responses" && request.method == http.MethodPost:
		return codexGateSSECompletedResponse("resp_gate", nil)
	case request.path == "/backend-api/codex/models":
		return codexGateJSONResponse(codexModelsRecorderManifest)
	case request.path == "/oauth/token":
		return codexGateJSONResponse(`{"access_token":"access-gate","refresh_token":"refresh-gate","expires_in":3600}`)
	case request.path == "/backend-api/codex/alpha/search":
		return codexGateJSONResponse(`{"encrypted_output":"ciphertext","output":"search result"}`)
	case request.path == "/backend-api/codex/images/generations" || request.path == "/backend-api/codex/images/edits":
		return codexGateJSONResponse(`{"created":1710000000,"data":[{"b64_json":"aW1n"}],` +
			`"usage":{"input_tokens":1,"output_tokens":1,"output_tokens_details":{"image_tokens":1}}}`)
	default:
		return codexGateJSONResponse(`{}`)
	}
}

// codexGateDefaultWebSocketResponse 是终端对 WS 帧的默认应答：每个 response.create
// 都以 created、completed 两个事件完成，响应 ID 按连接与帧序号生成，便于续链。
func codexGateDefaultWebSocketResponse(request codexGateWireRequest, frame codexGateWireFrame) [][]byte {
	if codexGateWebSocketFrameType(frame) != "response.create" {
		return nil
	}
	return codexGateWebSocketCompleted(fmt.Sprintf("resp_gate_ws_%d_%d", request.connection, len(request.frames)))
}

// codexGateWireServer 是晋升后门禁的本地 TLS 终端。它用测试根证书签发官方主机名
// 证书，记录每次握手的 ClientHello 与每个请求的原始 HTTP/1.1 字节；支持 keep-alive
// （同一连接上的多个请求共享连接序号）与 WS 升级（记录握手后客户端发出的帧）。
type codexGateWireServer struct {
	listener    net.Listener
	roots       *x509.CertPool
	certificate tls.Certificate

	mu        sync.Mutex
	respond   func(codexGateWireRequest) codexGateWireResponse
	webSocket func(request codexGateWireRequest, frame codexGateWireFrame) [][]byte
	// upgradeHeader 是 101 应答额外携带的 header（例如 Set-Cookie，验证握手写回 jar）。
	upgradeHeader http.Header
	hellos        []codexGateClientHello
	requests      []codexGateWireRequest
	connections   int
	open          map[net.Conn]struct{}
	handshakes    []error
	wg            sync.WaitGroup
}

// startCodexGateWireServer 启动本地 TLS 终端；respond 为 nil 时使用默认应答。
func startCodexGateWireServer(
	t *testing.T,
	respond func(codexGateWireRequest) codexGateWireResponse,
) *codexGateWireServer {
	t.Helper()
	certificate, roots := newCodexGateTestCertificate(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	if respond == nil {
		respond = codexGateDefaultResponse
	}
	server := &codexGateWireServer{
		listener: listener, roots: roots, certificate: certificate,
		respond: respond, open: make(map[net.Conn]struct{}),
		webSocket: codexGateDefaultWebSocketResponse,
	}
	server.wg.Add(1)
	go server.acceptLoop()
	t.Cleanup(server.close)
	return server
}

func (s *codexGateWireServer) acceptLoop() {
	defer s.wg.Done()
	for {
		connection, err := s.listener.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		s.connections++
		index := s.connections
		s.open[connection] = struct{}{}
		s.mu.Unlock()
		s.wg.Add(1)
		go s.serve(connection, index)
	}
}

func (s *codexGateWireServer) close() {
	_ = s.listener.Close()
	s.mu.Lock()
	for connection := range s.open {
		_ = connection.Close()
	}
	s.mu.Unlock()
	s.wg.Wait()
}

// setWebSocketResponder 设置 WS 帧应答：返回值中的每个元素作为一帧文本回写客户端。
func (s *codexGateWireServer) setWebSocketResponder(
	respond func(request codexGateWireRequest, frame codexGateWireFrame) [][]byte,
) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.webSocket = respond
}

// setUpgradeHeader 设置 WS 101 应答额外携带的 header。
func (s *codexGateWireServer) setUpgradeHeader(header http.Header) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.upgradeHeader = header.Clone()
}

// clientHellos 返回已观测到的 ClientHello 副本（按连接序号）。
func (s *codexGateWireServer) clientHellos() []codexGateClientHello {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]codexGateClientHello(nil), s.hellos...)
}

// wireRequests 返回已观测到的请求副本（按到达顺序）。
func (s *codexGateWireServer) wireRequests() []codexGateWireRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]codexGateWireRequest, len(s.requests))
	for index, request := range s.requests {
		out[index] = request
		out[index].frames = append([]codexGateWireFrame(nil), request.frames...)
	}
	return out
}

// requestsForPath 返回指定路径的请求副本。
func (s *codexGateWireServer) requestsForPath(path string) []codexGateWireRequest {
	var out []codexGateWireRequest
	for _, request := range s.wireRequests() {
		if request.path == path {
			out = append(out, request)
		}
	}
	return out
}

// httpRequestsForPath 返回指定路径上的普通 HTTP 请求（排除同路径的 WS 握手）。
func (s *codexGateWireServer) httpRequestsForPath(path string) []codexGateWireRequest {
	var out []codexGateWireRequest
	for _, request := range s.requestsForPath(path) {
		if !request.webSocket {
			out = append(out, request)
		}
	}
	return out
}

// handshakeErrors 返回握手失败记录，便于在断言失败时给出原因。
func (s *codexGateWireServer) handshakeErrors() []error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]error(nil), s.handshakes...)
}

func (s *codexGateWireServer) serve(connection net.Conn, index int) {
	defer s.wg.Done()
	defer func() {
		_ = connection.Close()
		s.mu.Lock()
		delete(s.open, connection)
		s.mu.Unlock()
	}()
	var hello codexGateClientHello
	tlsConnection := tls.Server(connection, &tls.Config{
		Certificates: []tls.Certificate{s.certificate},
		MinVersion:   tls.VersionTLS12,
		MaxVersion:   tls.VersionTLS13,
		NextProtos:   []string{"http/1.1"},
		GetConfigForClient: func(info *tls.ClientHelloInfo) (*tls.Config, error) {
			if info.HelloRetryRequest {
				return nil, nil
			}
			hello = codexGateClientHello{
				connection: index,
				serverName: info.ServerName,
				extensions: append([]uint16(nil), info.Extensions...),
			}
			for _, scheme := range info.SignatureSchemes {
				hello.signatureSchemes = append(hello.signatureSchemes, uint16(scheme))
			}
			return nil, nil
		},
	})
	_ = tlsConnection.SetDeadline(time.Now().Add(30 * time.Second))
	if err := tlsConnection.Handshake(); err != nil {
		s.mu.Lock()
		s.handshakes = append(s.handshakes, err)
		s.mu.Unlock()
		return
	}
	s.mu.Lock()
	s.hellos = append(s.hellos, hello)
	s.mu.Unlock()
	reader := bufio.NewReader(tlsConnection)
	for sequence := 1; ; sequence++ {
		request, err := readCodexGateWireRequest(reader)
		if err != nil {
			return
		}
		request.connection, request.sequence = index, sequence
		if request.webSocket {
			s.serveWebSocket(tlsConnection, reader, request)
			return
		}
		s.mu.Lock()
		respond := s.respond
		s.mu.Unlock()
		response := respond(request)
		s.mu.Lock()
		s.requests = append(s.requests, request)
		s.mu.Unlock()
		if response.dropWithoutResponse {
			return
		}
		if err := writeCodexGateWireResponse(tlsConnection, response); err != nil {
			return
		}
		if response.closeAfter || strings.EqualFold(request.header.Get("Connection"), "close") {
			return
		}
	}
}

// serveWebSocket 完成 RFC 6455 握手（不接受压缩扩展，客户端据此发送未压缩帧），
// 随后记录客户端发出的每一帧；应答帧由 setWebSocketResponder 决定。
func (s *codexGateWireServer) serveWebSocket(
	connection net.Conn,
	reader *bufio.Reader,
	request codexGateWireRequest,
) {
	s.mu.Lock()
	s.requests = append(s.requests, request)
	requestIndex := len(s.requests) - 1
	s.mu.Unlock()
	key := strings.TrimSpace(request.header.Get("Sec-WebSocket-Key"))
	digest := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")) //nolint:gosec // RFC 6455 固定算法。
	accept := base64.StdEncoding.EncodeToString(digest[:])
	var extra strings.Builder
	s.mu.Lock()
	for name, values := range s.upgradeHeader {
		for _, value := range values {
			_, _ = fmt.Fprintf(&extra, "%s: %s\r\n", name, value)
		}
	}
	s.mu.Unlock()
	handshake := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + accept + "\r\n" + extra.String() + "\r\n"
	if _, err := io.WriteString(connection, handshake); err != nil {
		return
	}
	for {
		frame, err := readCodexGateWebSocketFrame(reader)
		if err != nil {
			return
		}
		s.mu.Lock()
		s.requests[requestIndex].frames = append(s.requests[requestIndex].frames, frame)
		current := s.requests[requestIndex]
		respond := s.webSocket
		s.mu.Unlock()
		switch frame.opcode {
		case 0x8:
			_ = writeCodexGateWebSocketFrame(connection, 0x8, frame.payload)
			return
		case 0x9:
			_ = writeCodexGateWebSocketFrame(connection, 0xA, frame.payload)
			continue
		}
		if respond == nil {
			continue
		}
		for _, payload := range respond(current, frame) {
			if err := writeCodexGateWebSocketFrame(connection, 0x1, payload); err != nil {
				return
			}
		}
	}
}

// readCodexGateWireRequest 读取一个 HTTP/1.1 请求，保留 header 的原样名称与顺序。
func readCodexGateWireRequest(reader *bufio.Reader) (codexGateWireRequest, error) {
	line, err := reader.ReadString('\n')
	if err != nil {
		return codexGateWireRequest{}, err
	}
	parts := strings.Fields(strings.TrimRight(line, "\r\n"))
	if len(parts) != 3 {
		return codexGateWireRequest{}, fmt.Errorf("非法请求行：%q", line)
	}
	request := codexGateWireRequest{method: parts[0], target: parts[1], header: make(http.Header)}
	request.path, _, _ = strings.Cut(parts[1], "?")
	for {
		headerLine, readErr := reader.ReadString('\n')
		if readErr != nil {
			return codexGateWireRequest{}, readErr
		}
		headerLine = strings.TrimRight(headerLine, "\r\n")
		if headerLine == "" {
			break
		}
		name, value, ok := strings.Cut(headerLine, ":")
		if !ok {
			return codexGateWireRequest{}, fmt.Errorf("非法 header 行：%q", headerLine)
		}
		request.headerNames = append(request.headerNames, name)
		request.header.Add(textproto.CanonicalMIMEHeaderKey(strings.TrimSpace(name)), strings.TrimSpace(value))
	}
	if strings.EqualFold(request.header.Get("Upgrade"), "websocket") {
		request.webSocket = true
		return request, nil
	}
	if length := request.header.Get("Content-Length"); length != "" {
		size, parseErr := strconv.Atoi(length)
		if parseErr != nil || size < 0 {
			return codexGateWireRequest{}, fmt.Errorf("非法 content-length：%q", length)
		}
		request.rawBody = make([]byte, size)
		if _, readErr := io.ReadFull(reader, request.rawBody); readErr != nil {
			return codexGateWireRequest{}, readErr
		}
	}
	request.body = request.rawBody
	if strings.EqualFold(request.header.Get("Content-Encoding"), "zstd") && len(request.rawBody) > 0 {
		decoder, decodeErr := zstd.NewReader(nil)
		if decodeErr != nil {
			return codexGateWireRequest{}, decodeErr
		}
		decoded, decodeErr := decoder.DecodeAll(request.rawBody, nil)
		decoder.Close()
		if decodeErr != nil {
			return codexGateWireRequest{}, fmt.Errorf("zstd 请求体解压失败：%w", decodeErr)
		}
		request.body = decoded
	}
	return request, nil
}

func writeCodexGateWireResponse(connection net.Conn, response codexGateWireResponse) error {
	status := response.status
	if status == 0 {
		status = http.StatusOK
	}
	var builder strings.Builder
	_, _ = fmt.Fprintf(&builder, "HTTP/1.1 %d %s\r\n", status, http.StatusText(status))
	for name, values := range response.header {
		for _, value := range values {
			_, _ = fmt.Fprintf(&builder, "%s: %s\r\n", name, value)
		}
	}
	_, _ = fmt.Fprintf(&builder, "Content-Length: %d\r\n", len(response.body))
	if response.closeAfter {
		_, _ = builder.WriteString("Connection: close\r\n")
	}
	_, _ = builder.WriteString("\r\n")
	if _, err := io.WriteString(connection, builder.String()); err != nil {
		return err
	}
	_, err := connection.Write(response.body)
	return err
}

// readCodexGateWebSocketFrame 读取一帧客户端 WS 帧并去掩码；分片帧按 continuation 拼接。
func readCodexGateWebSocketFrame(reader *bufio.Reader) (codexGateWireFrame, error) {
	var frame codexGateWireFrame
	for {
		head := make([]byte, 2)
		if _, err := io.ReadFull(reader, head); err != nil {
			return codexGateWireFrame{}, err
		}
		final := head[0]&0x80 != 0
		opcode := head[0] & 0x0F
		masked := head[1]&0x80 != 0
		length := uint64(head[1] & 0x7F)
		switch length {
		case 126:
			extended := make([]byte, 2)
			if _, err := io.ReadFull(reader, extended); err != nil {
				return codexGateWireFrame{}, err
			}
			length = uint64(binary.BigEndian.Uint16(extended))
		case 127:
			extended := make([]byte, 8)
			if _, err := io.ReadFull(reader, extended); err != nil {
				return codexGateWireFrame{}, err
			}
			length = binary.BigEndian.Uint64(extended)
		}
		var mask [4]byte
		if masked {
			if _, err := io.ReadFull(reader, mask[:]); err != nil {
				return codexGateWireFrame{}, err
			}
		}
		if length > 64<<20 {
			return codexGateWireFrame{}, errors.New("WS 帧过大")
		}
		payload := make([]byte, length)
		if _, err := io.ReadFull(reader, payload); err != nil {
			return codexGateWireFrame{}, err
		}
		if masked {
			for index := range payload {
				payload[index] ^= mask[index%4]
			}
		}
		if opcode != 0x0 {
			frame.opcode = opcode
		}
		frame.payload = append(frame.payload, payload...)
		if final {
			return frame, nil
		}
	}
}

func writeCodexGateWebSocketFrame(connection net.Conn, opcode byte, payload []byte) error {
	var header []byte
	header = append(header, 0x80|opcode)
	switch {
	case len(payload) < 126:
		header = append(header, byte(len(payload)))
	case len(payload) <= 0xFFFF:
		header = append(header, 126, byte(len(payload)>>8), byte(len(payload)))
	default:
		header = append(header, 127)
		size := make([]byte, 8)
		binary.BigEndian.PutUint64(size, uint64(len(payload)))
		header = append(header, size...)
	}
	if _, err := connection.Write(header); err != nil {
		return err
	}
	_, err := connection.Write(payload)
	return err
}

// newCodexGateTestCertificate 签发覆盖官方主机名的测试证书链；根证书只进入捕获
// 上游的画像副本，不进入系统信任链。
func newCodexGateTestCertificate(t *testing.T) (tls.Certificate, *x509.CertPool) {
	t.Helper()
	now := time.Now()
	rootKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	rootTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Codex 晋升后门禁测试根证书"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	rootDER, err := x509.CreateCertificate(rand.Reader, rootTemplate, rootTemplate, &rootKey.PublicKey, rootKey)
	require.NoError(t, err)
	rootCertificate, err := x509.ParseCertificate(rootDER)
	require.NoError(t, err)
	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	leafTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "chatgpt.com"},
		DNSNames: []string{
			"chatgpt.com", "auth.openai.com", "api.openai.com", "*.oaiusercontent.com",
		},
		NotBefore:   now.Add(-time.Hour),
		NotAfter:    now.Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, rootCertificate, &leafKey.PublicKey, rootKey)
	require.NoError(t, err)
	leafKeyDER, err := x509.MarshalPKCS8PrivateKey(leafKey)
	require.NoError(t, err)
	certificate, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: leafKeyDER}),
	)
	require.NoError(t, err)
	roots := x509.NewCertPool()
	roots.AddCert(rootCertificate)
	return certificate, roots
}

// ----------------------------------------------------------------------------
// 捕获上游（HTTPUpstream 实现）
// ----------------------------------------------------------------------------

// codexGateWireUpstream 实现 HTTPUpstream：收到生产端口交来的已定型请求与 TLS 画像后，
// 按生产 HTTPUpstream 的同一组连接池身份（画像身份、账号、官方连接池键）复用
// http.Client，用 tlsfingerprint 真实拨号到本地终端。它只把目标地址改为本地终端，
// 画像的 TLS 参数、H1 线序规则与小写化选项全部原样生效；根证书换成测试根证书。
type codexGateWireUpstream struct {
	server *codexGateWireServer
	// guard 是签发 FinalizationToken 的同一个 runtime Guard。生产上进程级 Guard 同时
	// 服务 Executor 与各传输的终端校验（GuardedRoundTripper），这里显式传入以保持同构。
	guard *officialegress.Guard

	mu       sync.Mutex
	clients  map[string]*http.Client
	profiles []*tlsfingerprint.Profile
	failures []error
	// attempts 按到达顺序记录每个请求携带的 Executor attempt 身份（端点、序号、原因、
	// Bundle 与连接池摘要），用来区分“同一 invocation 的重试”与“新的调用”。
	attempts []officialegress.AttemptIdentity
	// egressInvocations 按到达顺序记录 service 出站上下文层（入口）的调用 ID。
	egressInvocations []string
}

func newCodexGateWireUpstream(t *testing.T, server *codexGateWireServer) *codexGateWireUpstream {
	t.Helper()
	upstream := &codexGateWireUpstream{server: server, clients: make(map[string]*http.Client)}
	t.Cleanup(upstream.closeIdle)
	return upstream
}

func (u *codexGateWireUpstream) closeIdle() {
	u.mu.Lock()
	defer u.mu.Unlock()
	for _, client := range u.clients {
		client.CloseIdleConnections()
	}
}

// Do 拒绝没有 TLS 画像的请求：官方出站必须经生产端口带着已定型画像到达这里。
func (u *codexGateWireUpstream) Do(request *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	path := ""
	if request != nil && request.URL != nil {
		path = request.URL.Path
	}
	err := fmt.Errorf("晋升后门禁的官方出站缺少已定型 TLS 画像：%s", path)
	u.mu.Lock()
	u.failures = append(u.failures, err)
	u.mu.Unlock()
	return nil, err
}

func (u *codexGateWireUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	accountID int64,
	_ int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	return u.send(request, accountID, profile, officialegress.BackendHTTPUpstream)
}

func (u *codexGateWireUpstream) send(
	request *http.Request,
	accountID int64,
	profile *tlsfingerprint.Profile,
	backend officialegress.BackendKind,
) (*http.Response, error) {
	if request == nil || request.URL == nil || profile == nil {
		return nil, errors.New("捕获上游缺少请求或 TLS 画像")
	}
	poolID, _ := OfficialEgressConnectionPoolIDFromContext(request.Context())
	transportIdentity, err := json.Marshal(profile.Transport)
	if err != nil {
		return nil, err
	}
	key := fmt.Sprintf("%s|%s|%s|account:%d|official:%s", backend, profile.Name, transportIdentity, accountID, poolID)
	u.mu.Lock()
	u.profiles = append(u.profiles, profile)
	if identity, ok := officialegress.AttemptIdentityFromContext(request.Context()); ok {
		u.attempts = append(u.attempts, identity)
	}
	egressInvocation := ""
	if egressContext, ok := OfficialEgressContextFromContext(request.Context()); ok {
		egressInvocation = egressContext.InvocationID()
	}
	u.egressInvocations = append(u.egressInvocations, egressInvocation)
	client, ok := u.clients[key]
	if !ok {
		client = u.newClient(profile, backend)
		u.clients[key] = client
	}
	u.mu.Unlock()
	if jar := HTTPUpstreamCookieJarFromContext(request.Context()); jar != nil {
		clone := *client
		clone.Jar = &codexGateResponseOnlyCookieJar{delegate: jar}
		client = &clone
	}
	return client.Do(request)
}

// SendOfficialCodexReqProfile 实现 req 画像传输资源（OAuth refresh 等 BackendReqProfile
// 端点）：与生产资源一样由已定型 TransportSpec 转出 tlsfingerprint 画像，再按同一方式
// 真实拨号到本地终端。生产资源使用 req 客户端，但 H1 线序同样由 tlsfingerprint 的
// wire 重写决定，本地终端观测到的字节与生产一致。
func (u *codexGateWireUpstream) SendOfficialCodexReqProfile(
	_ context.Context,
	request *http.Request,
	transport officialegress.TransportSpec,
	_ string,
) (*http.Response, error) {
	profile, err := tlsFingerprintProfileFromTransportSpec(transport)
	if err != nil {
		return nil, err
	}
	return u.send(request, 0, profile, officialegress.BackendReqProfile)
}

func (u *codexGateWireUpstream) newClient(
	profile *tlsfingerprint.Profile,
	backend officialegress.BackendKind,
) *http.Client {
	cloned := *profile
	cloned.RootCAs = u.server.roots
	address := u.server.listener.Addr().String()
	dialer := tlsfingerprint.NewDialer(&cloned, func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, address)
	})
	transport := &http.Transport{
		DialTLSContext:     dialer.DialTLSContext,
		ForceAttemptHTTP2:  false,
		DisableCompression: cloned.Transport.DisableCompression,
		MaxIdleConns:       16,
		IdleConnTimeout:    30 * time.Second,
	}
	// 与生产 HTTPUpstream 的组装顺序一致：Transport → 终端 Guard → 小写化。
	var roundTripper http.RoundTripper = transport
	if u.guard != nil {
		roundTripper = officialegress.NewGuardedRoundTripper(
			roundTripper, u.guard, backend, officialegress.WireProtocolHTTP,
		)
	}
	if cloned.Transport.LowercaseHeaders {
		roundTripper = tlsfingerprint.NewLowercaseHeaderRoundTripper(roundTripper, cloned.Transport.PreserveHeaderCase)
	}
	return &http.Client{
		Transport:     roundTripper,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// failureList 返回捕获上游拒绝过的请求（缺少 TLS 画像等）副本，用于失败信息。
func (u *codexGateWireUpstream) failureList() []error {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]error(nil), u.failures...)
}

// egressInvocationIDs 返回已记录的入口层调用 ID 副本。
func (u *codexGateWireUpstream) egressInvocationIDs() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]string(nil), u.egressInvocations...)
}

// profileNames 返回已记录的 TLS 画像名称副本。
func (u *codexGateWireUpstream) profileNames() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	names := make([]string, 0, len(u.profiles))
	for _, profile := range u.profiles {
		names = append(names, profile.Name)
	}
	return names
}

// attemptIdentities 返回已记录的 attempt 身份副本。
func (u *codexGateWireUpstream) attemptIdentities() []officialegress.AttemptIdentity {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]officialegress.AttemptIdentity(nil), u.attempts...)
}

// codexGateResponseOnlyCookieJar 与生产 HTTPUpstream 一致：只把上游 Set-Cookie 写回
// 账号级 jar，不在已签名请求上再补 Cookie（Cookie 已由 Compiler 在签名前固化）。
type codexGateResponseOnlyCookieJar struct {
	delegate http.CookieJar
}

func (j *codexGateResponseOnlyCookieJar) SetCookies(target *url.URL, cookies []*http.Cookie) {
	if j != nil && j.delegate != nil {
		j.delegate.SetCookies(target, cookies)
	}
}

func (*codexGateResponseOnlyCookieJar) Cookies(*url.URL) []*http.Cookie { return nil }

// ----------------------------------------------------------------------------
// 断言辅助
// ----------------------------------------------------------------------------

// codexGateRequireOrderedSubset 断言 names 是 allowed 的有序子序列，且包含全部 required。
// 语义与批准断言的 all_ordered_subset_of 相同。
func codexGateRequireOrderedSubset(
	t *testing.T,
	names []string,
	allowed []string,
	required []string,
	message string,
) {
	t.Helper()
	position := 0
	for _, name := range names {
		index := slices.Index(allowed[position:], name)
		require.GreaterOrEqual(t, index, 0,
			"%s：%q 不在允许线序中或违反允许的相对顺序，实际线序 %v，允许线序 %v", message, name, names, allowed)
		position += index + 1
	}
	for _, name := range required {
		require.Contains(t, names, name, "%s：缺少必需的 %q，实际线序 %v", message, name, names)
	}
}

// codexGateJSONFieldOrder 返回 JSON 对象顶层字段的原始顺序。
func codexGateJSONFieldOrder(t *testing.T, raw []byte) []string {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	require.NoError(t, err)
	require.Equal(t, json.Delim('{'), token, "JSON 载荷不是对象：%s", raw)
	var names []string
	for decoder.More() {
		key, keyErr := decoder.Token()
		require.NoError(t, keyErr)
		name, ok := key.(string)
		require.True(t, ok)
		names = append(names, name)
		var skip json.RawMessage
		require.NoError(t, decoder.Decode(&skip))
	}
	return names
}

// ----------------------------------------------------------------------------
// 生产调用驱动
// ----------------------------------------------------------------------------

// codexGateForwardAccountID 与 newOfficialOpenAIHTTPTestService 预置模型清单的账号一致，
// 使 Lite 判定与生产同源（gpt-5.6-luna 走 Lite）。
const codexGateForwardAccountID = 94

// codexGateService 是指向某个发布槽位的生产网关服务：OpenAIGatewayService 的业务
// 入口、官方出站 runtime（正式 Compiler/Executor）与捕获上游三者绑在同一个上游实例上。
type codexGateService struct {
	service  *OpenAIGatewayService
	upstream *codexGateWireUpstream
	account  *Account
}

// newCodexGateService 构造指向 mode 槽位的生产网关服务与 OAuth 测试账号。
func newCodexGateService(t *testing.T, mode string, server *codexGateWireServer) *codexGateService {
	t.Helper()
	upstream := newCodexGateWireUpstream(t, server)
	service := newOfficialOpenAIHTTPTestService(nil)
	service.httpUpstream = upstream
	service.officialEgress = newCodexGateRuntime(t, upstream, mode)
	// 生产上 service 的发布指针配置与 Executor runtime 的 release mode 同源
	// （officialEgressReleaseModeFromConfig）：HTTP/WS 出站上下文挂载、Cookie 名单等
	// 读配置，Compiler/Executor 读 runtime。两者必须指向同一槽位，否则 service 层会
	// 按另一份画像派生身份事实。
	service.cfg.Gateway.OfficialClientProfiles.Mode = mode
	upstream.guard = service.officialEgress.Guard
	return &codexGateService{
		service: service, upstream: upstream,
		account: newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID),
	}
}

// ingress 模拟生产路由入口：在 body 解压前冻结官方进程快照（压缩、子代理、
// guardian 等 wire 事实只认这份快照），返回应交给业务入口的请求上下文。
func (g *codexGateService) ingress(c *gin.Context) context.Context {
	c.Request = c.Request.WithContext(WithOfficialCodexIngressRuntime(c.Request.Context(), c))
	return c.Request.Context()
}

// seedCookie 在账号级 Cookie jar 中预置一条 chatgpt.com 的 Cookie（模拟 jar 已建立）。
func (g *codexGateService) seedCookie(name string, value string) {
	g.service.openAICookieJar(g.account).SetCookies(
		&url.URL{Scheme: "https", Host: "chatgpt.com", Path: "/"},
		[]*http.Cookie{{Name: name, Value: value, Path: "/"}},
	)
}

// forward 经生产 Responses 入口（与 handler 相同：入站传输登记为 HTTP 并冻结快照）。
func (g *codexGateService) forward(c *gin.Context, body []byte) (*OpenAIForwardResult, error) {
	return g.forwardAs(c, body, g.account)
}

// forwardAs 与 forward 相同，但由指定账号承接（模拟调度把同一会话交给另一账号）。
func (g *codexGateService) forwardAs(c *gin.Context, body []byte, account *Account) (*OpenAIForwardResult, error) {
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	return g.service.Forward(g.ingress(c), c, account, body)
}

// codexGateAuxiliaryIngress 构造官方 Codex 客户端形态的辅助端点入站请求。
func codexGateAuxiliaryIngress(method string, path string, body []byte, contentType string) *gin.Context {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(method, path, bytes.NewReader(body))
	if contentType != "" {
		c.Request.Header.Set("Content-Type", contentType)
	}
	c.Request.Header.Set("User-Agent", officialOpenAIHTTPUserAgent)
	c.Request.Header.Set("originator", "codex_exec")
	c.Set("api_key", &APIKey{ID: 42})
	return c
}

// alphaSearch 经生产 ForwardAlphaSearch 入口发出一次 alpha-search。
func (g *codexGateService) alphaSearch(body []byte) (*OpenAIForwardResult, error) {
	c := codexGateAuxiliaryIngress(http.MethodPost, "/v1/alpha/search", body, "application/json")
	return g.service.ForwardAlphaSearch(g.ingress(c), c, g.account, body)
}

// images 经生产 ParseOpenAIImagesRequest 与 ForwardImages 入口发出一次图像请求。
func (g *codexGateService) images(t *testing.T, path string, body []byte, contentType string) (*OpenAIForwardResult, error) {
	t.Helper()
	c := codexGateAuxiliaryIngress(http.MethodPost, path, body, contentType)
	ctx := g.ingress(c)
	parsed, err := g.service.ParseOpenAIImagesRequest(c, body)
	require.NoError(t, err, "解析图像入站请求失败")
	return g.service.ForwardImages(ctx, c, g.account, body, parsed, "")
}

// oauthRefresh 经生产 OpenAIOAuthService 的刷新执行入口发出一次 OAuth refresh。
func (g *codexGateService) oauthRefresh() error {
	oauth := NewOpenAIOAuthService(nil, nil)
	oauth.SetOfficialEgressRuntime(g.service.officialEgress)
	response, err := oauth.executeOAuthRefresh(context.Background(), "promotion-gate-refresh-token", "", "")
	if response != nil && response.Body != nil {
		_ = response.Body.Close()
	}
	return err
}

// models 经生产 FetchCodexModelsManifest 入口拉取一次模型清单。
func (g *codexGateService) models() error {
	c := codexGateAuxiliaryIngress(http.MethodGet, "/backend-api/codex/models", nil, "")
	_, err := g.service.FetchCodexModelsManifest(g.ingress(c), g.account, "", "", c)
	return err
}

// codexGateForwardResponses 经生产 OpenAIGatewayService.Forward 把一次入站请求发往
// mode 槽位：入站快照、身份派生、请求体定型、Compiler/Executor 与 HTTPUpstream 端口
// 都是生产实现。返回 Forward 的结果与错误，由调用方决定期望。
func codexGateForwardResponses(
	t *testing.T,
	mode string,
	server *codexGateWireServer,
	c *gin.Context,
	body []byte,
	configure func(service *OpenAIGatewayService, account *Account),
) (*OpenAIForwardResult, *codexGateWireUpstream, error) {
	t.Helper()
	gate := newCodexGateService(t, mode, server)
	if configure != nil {
		configure(gate.service, gate.account)
	}
	result, err := gate.forward(c, body)
	return result, gate.upstream, err
}

// codexGateForwardResponsesWire 与 codexGateForwardResponses 相同，但要求 Forward 成功，
// 并返回本次调用在本地终端上新增的唯一一条 Responses 请求。
func codexGateForwardResponsesWire(
	t *testing.T,
	mode string,
	server *codexGateWireServer,
	c *gin.Context,
	body []byte,
	configure func(service *OpenAIGatewayService, account *Account),
) codexGateWireRequest {
	t.Helper()
	before := len(server.httpRequestsForPath(codexGateResponsesPath))
	_, upstream, err := codexGateForwardResponses(t, mode, server, c, body, configure)
	require.NoError(t, err, "%s 槽位的 Responses Forward 失败（握手错误 %v，上游错误 %v）",
		mode, server.handshakeErrors(), upstream.failureList())
	requests := server.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, requests, before+1, "一次 Forward 应恰好产生一条 Responses 请求")
	return requests[len(requests)-1]
}

const (
	codexGateResponsesPath      = "/backend-api/codex/responses"
	codexGateLegacyCompactPath  = "/backend-api/codex/responses/compact"
	codexGateResponsesHTTPS     = "https://chatgpt.com" + codexGateResponsesPath
	codexGateLegacyCompactHTTPS = "https://chatgpt.com" + codexGateLegacyCompactPath
)

// codexGateLegacyCompactRemoved 是“legacy compact 端点已删除”这一结构事实：画像中
// 既没有 responses_compact 端点，也没有任何端点占用 compact 路径。
func codexGateLegacyCompactRemoved(profile profilecontract.ExecutableProfile) bool {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == officialCodexEndpointResponsesCompact || endpoint.Path == codexGateLegacyCompactPath {
			return false
		}
	}
	return true
}

// ----------------------------------------------------------------------------
// WebSocket：真实握手与帧
// ----------------------------------------------------------------------------

// codexGateWebSocketDial 记录一次 WS 拨号时 Executor 交下来的调用身份。
type codexGateWebSocketDial struct {
	// egressInvocationID 是 service 出站上下文层（入口）的调用 ID；未挂载上下文时为空。
	egressInvocationID string
	attempt            officialegress.AttemptIdentity
	// err 是生产拨号器返回的握手错误（握手成功时为 nil）。
	err error
}

// codexGateWebSocketDialer 实现 openAIWSClientDialer：委托生产 coderOpenAIWSClientDialer
// 完成握手，只把它为本次已编译传输准备的 http.Client 换成“目标地址重定向到本地终端、
// 根证书换成测试根证书”的同构副本。副本与生产 compiledOfficialEgressHTTPClient 的构造
// 一致：同一份 TLS 画像（tlsFingerprintProfileFromTransportSpec）、同一个
// buildOpenAIOfficialEgressWSTransport、同一层 Guard 与 officialEgressWebSocketRoundTripper，
// 并按生产缓存键预先放入生产拨号器的缓存。于是握手头由 coder/websocket 真实生成、由
// tlsfingerprint 真实按 swap_remove 重写，压缩提议、Set-Cookie 写回也都是生产代码。
type codexGateWebSocketDialer struct {
	server *codexGateWireServer
	inner  *coderOpenAIWSClientDialer
	// guard 与 codexGateWireUpstream.guard 相同：签发 token 的 runtime Guard。
	guard *officialegress.Guard

	mu    sync.Mutex
	dials []codexGateWebSocketDial
}

func newCodexGateWebSocketDialer(
	server *codexGateWireServer,
	guard *officialegress.Guard,
) *codexGateWebSocketDialer {
	inner, _ := newDefaultOpenAIWSClientDialer().(*coderOpenAIWSClientDialer)
	return &codexGateWebSocketDialer{server: server, inner: inner, guard: guard}
}

func (d *codexGateWebSocketDialer) Dial(
	ctx context.Context,
	wsURL string,
	headers http.Header,
	proxyURL string,
) (openAIWSClientConn, int, http.Header, error) {
	compiled, ok := ctx.Value(officialCompiledWSTransportContextKey{}).(officialCompiledWSTransport)
	if !ok {
		return nil, 0, nil, errors.New("晋升后门禁的 WS 出站必须携带 Executor 已编译的传输")
	}
	profile, err := tlsFingerprintProfileFromTransportSpec(compiled.transport)
	if err != nil {
		return nil, 0, nil, err
	}
	profile.RootCAs = d.server.roots
	transport, err := buildOpenAIOfficialEgressWSTransport(profile, proxyURL)
	if err != nil {
		return nil, 0, nil, err
	}
	address := d.server.listener.Addr().String()
	transport.DialTLSContext = tlsfingerprint.NewDialer(profile, func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, address)
	}).DialTLSContext
	guarded := officialegress.NewGuardedRoundTripper(
		transport, d.guard, officialegress.BackendWebSocket, officialegress.WireProtocolWebSocket,
	)
	client := &http.Client{
		Transport:     &officialEgressWebSocketRoundTripper{base: guarded},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	cacheKey := "official:" + compiled.poolDigest +
		"|proxy_state=" + officialEgressProxyStateKey(strings.TrimSpace(proxyURL))
	d.inner.proxyMu.Lock()
	d.inner.proxyClients[cacheKey] = &openAIWSProxyClientEntry{client: client, lastUsedUnixNano: time.Now().UnixNano()}
	d.inner.proxyMu.Unlock()

	record := codexGateWebSocketDial{}
	if egressContext, exists := OfficialEgressContextFromContext(ctx); exists {
		record.egressInvocationID = egressContext.InvocationID()
	}
	if attempt, exists := officialegress.AttemptIdentityFromContext(compiled.ctx); exists {
		record.attempt = attempt
	}
	connection, status, responseHeaders, dialErr := d.inner.Dial(ctx, wsURL, headers, proxyURL)
	record.err = dialErr
	d.mu.Lock()
	d.dials = append(d.dials, record)
	d.mu.Unlock()
	return connection, status, responseHeaders, dialErr
}

func (d *codexGateWebSocketDialer) snapshot() []codexGateWebSocketDial {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]codexGateWebSocketDial(nil), d.dials...)
}

// codexGateWebSocketCompleted 是本地终端对 response.create 的默认应答：created 与
// completed 两个事件，响应 ID 由调用方给出。
func codexGateWebSocketCompleted(responseID string) [][]byte {
	return [][]byte{
		[]byte(`{"type":"response.created","response":{"id":"` + responseID + `","object":"response","status":"in_progress"}}`),
		[]byte(`{"type":"response.completed","response":{"id":"` + responseID +
			`","object":"response","status":"completed","output":[],` +
			`"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}`),
	}
}

// codexGateWebSocketFrameType 返回 WS 文本帧的事件类型。
func codexGateWebSocketFrameType(frame codexGateWireFrame) string {
	var envelope struct {
		Type string `json:"type"`
	}
	_ = json.Unmarshal(frame.payload, &envelope)
	return envelope.Type
}

// enableWebSocket 让服务按生产 WS v2 路径把 HTTP 入站的 Responses 发往上游 WS：
// 连接池的拨号器换成真实 wire 的 codexGateWebSocketDialer；超时与退避压到最小，只为
// 缩短测试时间，不改变重试与降级的判定。
func (g *codexGateService) enableWebSocket(t *testing.T, server *codexGateWireServer) *codexGateWebSocketDialer {
	t.Helper()
	cfg := g.service.cfg
	cfg.Gateway.OpenAIWS.Enabled = true
	cfg.Gateway.OpenAIWS.OAuthEnabled = true
	cfg.Gateway.OpenAIWS.ResponsesWebsocketsV2 = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.DialTimeoutSeconds = 5
	cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 5
	cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 5
	cfg.Gateway.OpenAIWS.RetryBackoffInitialMS = 1
	cfg.Gateway.OpenAIWS.RetryBackoffMaxMS = 2
	cfg.Gateway.OpenAIWS.RetryJitterRatio = 0
	dialer := g.webSocketDialer(server)
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(dialer)
	t.Cleanup(pool.Close)
	g.service.cache = &stubGatewayCache{}
	g.service.openaiWSResolver = NewOpenAIWSProtocolResolver(cfg)
	g.service.toolCorrector = NewCodexToolCorrector()
	g.service.openaiWSPool = pool
	return dialer
}

// webSocketDialer 返回绑定到本服务 runtime Guard 的真实 wire WS 拨号器。
func (g *codexGateService) webSocketDialer(server *codexGateWireServer) *codexGateWebSocketDialer {
	return newCodexGateWebSocketDialer(server, g.service.officialEgress.Guard)
}

// codexGateResponsesWSURL 是 Responses WS 端点的官方目标。
const codexGateResponsesWSURL = "wss://chatgpt.com" + codexGateResponsesPath

// dialResponsesWebSocket 按生产 WS 入口的装配方式建立一次 Responses WS 握手：先由
// attachOfficialEgressWebSocketContext 冻结出站上下文（入站快照、身份派生、运行态条件），
// 再经 newOfficialCodexWebSocketInvocation 与 DialDirect 交给正式 Compiler/Executor，
// 最后由真实 wire 拨号器完成握手。firstPayload 是首个 response.create 语义帧。
func (g *codexGateService) dialResponsesWebSocket(
	t *testing.T,
	c *gin.Context,
	firstPayload []byte,
	dialer openAIWSClientDialer,
) (*executorWebSocketFrameSession, context.Context, error) {
	t.Helper()
	ctx := g.ingress(c)
	ctx, err := attachOfficialEgressWebSocketContext(
		ctx, c, g.account, codexGateResponsesWSURL, firstPayload, g.service.cfg,
	)
	if err != nil {
		return nil, nil, err
	}
	invocation, err := newOfficialCodexWebSocketInvocation(ctx, officialCodexWebSocketInvocationInput{
		Runtime: g.service.officialEgress, Account: g.account,
		SinkID: officialegress.SinkCodexResponsesWS, PolicyID: "promotion-gate.responses.ws",
		PolicySource: "promotion-gate", AttemptBudget: 1,
	})
	if err != nil {
		return nil, nil, err
	}
	routingHint, err := officialegress.ParseOfficialCodexRoutingHintFacts(officialCodexEndpointResponsesWS, firstPayload)
	if err != nil {
		return nil, nil, err
	}
	session, _, _, err := invocation.DialDirect(ctx, dialer, openAIWSAcquireRequest{
		Account: g.account, WSURL: codexGateResponsesWSURL,
		Headers:     http.Header{"Authorization": []string{"Bearer oauth-test-token"}},
		RoutingHint: routingHint,
	}, officialCodexEndpointResponsesWS)
	return session, ctx, err
}

// codexGateThirdPartyUserAgent 是非官方客户端的入站 UA：画像默认让这类 HTTP 入站的
// Responses 走上游 WS（官方客户端 UA 会强制 HTTP）。
const codexGateThirdPartyUserAgent = "promotion-gate-third-party/1.0"

// forwardViaWebSocket 以第三方客户端形态经生产 Forward 发出一次 Responses 调用，返回本次
// 调用在终端上新增的 WS 握手请求（含其后客户端发出的全部帧）。调用前须 enableWebSocket。
func (g *codexGateService) forwardViaWebSocket(
	t *testing.T,
	server *codexGateWireServer,
	body []byte,
	header http.Header,
) (codexGateWireRequest, error) {
	t.Helper()
	before := 0
	for _, request := range server.wireRequests() {
		if request.webSocket {
			before++
		}
	}
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/openai/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("User-Agent", codexGateThirdPartyUserAgent)
	c.Request.Header.Set("Content-Type", "application/json")
	for name, values := range header {
		for _, value := range values {
			c.Request.Header.Add(name, value)
		}
	}
	_, err := g.service.Forward(g.ingress(c), c, g.account, body)
	var handshakes []codexGateWireRequest
	for _, request := range server.wireRequests() {
		if request.webSocket {
			handshakes = append(handshakes, request)
		}
	}
	if len(handshakes) <= before {
		return codexGateWireRequest{}, fmt.Errorf("Forward 没有产生新的 WS 握手（错误 %v）", err)
	}
	return handshakes[len(handshakes)-1], err
}

// codexGateResponseCreateFrames 返回握手之后客户端发出的 response.create 帧。
func codexGateResponseCreateFrames(request codexGateWireRequest) [][]byte {
	var out [][]byte
	for _, frame := range request.frames {
		if frame.opcode == 0x1 && codexGateWebSocketFrameType(frame) == "response.create" {
			out = append(out, frame.payload)
		}
	}
	return out
}

// codexGateRemainingHandshakeNames 返回 WS 握手中固定前五项之后的 header 名（原样）。
func codexGateRemainingHandshakeNames(request codexGateWireRequest) []string {
	if len(request.headerNames) <= 5 {
		return nil
	}
	return append([]string(nil), request.headerNames[5:]...)
}

// codexGateWebSocketBody 构造第三方客户端的 Responses 入站请求体（走 WS 的派生帧）。
func codexGateWebSocketBody(model string, extra map[string]any) []byte {
	payload := map[string]any{
		"model": model, "stream": false,
		"reasoning": map[string]any{"effort": "high"},
		"input": []any{
			map[string]any{"type": "message", "role": "developer", "content": "developer context"},
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}
	for key, value := range extra {
		payload[key] = value
	}
	raw, _ := json.Marshal(payload)
	return raw
}

// ----------------------------------------------------------------------------
// realtime（Live）驱动
// ----------------------------------------------------------------------------

const (
	codexGateRealtimeCallsPath = "/backend-api/codex/realtime/calls"
	codexGateRealtimeCallID    = "rtc_promotion_gate"
)

// codexGateRealtimeResponse 在默认应答之上补 realtime 第一跳：201 + Location（call_id）。
func codexGateRealtimeResponse(request codexGateWireRequest) codexGateWireResponse {
	if request.path == codexGateRealtimeCallsPath {
		return codexGateWireResponse{
			status: http.StatusCreated,
			header: http.Header{
				"Content-Type": []string{"application/sdp"},
				"Location":     []string{"/v1/realtime/calls/" + codexGateRealtimeCallID},
			},
			body: []byte("v=0\r\n"),
		}
	}
	return codexGateDefaultResponse(request)
}

// realtimeCall 按生产 CreateLiveCall 的装配发出 realtime 第一跳：入站快照冻结后，由
// bindOfficialCodexRuntimeStateFromCapturedIngress 绑定运行态，再调用
// createUpstreamLiveCall（本身经 newOfficialCodexHTTPInvocation 进入 Executor）。账号选择、
// 并发租约与证明签发属于调度与计费边界，不影响出站形态，这里不重放。
func (g *codexGateService) realtimeCall(
	t *testing.T,
	mode string,
) (*LiveCallCreated, *LiveCallRecord, error) {
	t.Helper()
	c := codexGateAuxiliaryIngress(http.MethodPost, "/v1/realtime/calls", nil, "application/json")
	ctx := g.ingress(c)
	attemptContext, runtimeState, err := bindOfficialCodexRuntimeStateFromCapturedIngress(
		ctx, g.account, mode, codexEndpointID(officialCodexEndpointRealtimeCalls),
	)
	require.NoError(t, err)
	leaseID := "promotion-gate-lease"
	created, err := g.service.createUpstreamLiveCall(
		attemptContext, g.account,
		&LiveCallRequest{SDP: "v=0\r\n", Session: json.RawMessage(`{"type":"realtime"}`)},
		"", liveRealtimeSessionID(leaseID),
	)
	record := &LiveCallRecord{
		AccountID: g.account.ID, LeaseID: leaseID,
		CodexRuntimeState: liveCodexRuntimeStateFromOfficial(runtimeState),
		CreatedAt:         time.Now(), ExpiresAt: time.Now().Add(time.Minute),
	}
	if created != nil {
		record.CallID = created.CallID
		record.CallHash = hashLiveCallID(created.CallID)
	}
	return created, record, err
}

// realtimeSideband 按生产 dialLiveSideband 建立 realtime 第二跳（api.openai.com 的 WS）：
// 账号仓库、证明密文与 passthrough 拨号器按 Live 生命周期测试的方式装配，拨号器换成
// 真实 wire 的 codexGateWebSocketDialer。
func (g *codexGateService) realtimeSideband(
	t *testing.T,
	server *codexGateWireServer,
	record *LiveCallRecord,
) error {
	t.Helper()
	cipher := newLiveAttestationCipher(&config.Config{JWT: config.JWTConfig{Secret: "promotion-gate-live-secret"}})
	ciphertext, err := cipher.Encrypt(`{"v":1,"s":0,"t":"v1.sideband"}`)
	require.NoError(t, err)
	record.AttestationCiphertext = ciphertext
	g.service.accountRepo = &liveTestAccountRepo{account: g.account}
	g.service.liveAttestationCipher = cipher
	g.service.openaiWSPassthroughDialer = g.webSocketDialer(server)
	conn, err := g.service.dialLiveSideband(context.Background(), record)
	if conn != nil {
		_ = conn.Close()
	}
	return err
}

// ----------------------------------------------------------------------------
// WS 入站会话（官方客户端 WS 模式）
// ----------------------------------------------------------------------------

// codexGateWebSocketIngress 是一条下游 WS 入站会话：本地明文 httptest 服务端按生产 WS
// handler 的方式（冻结入站快照、构造 gin 上下文）调用 ProxyResponsesWebSocketFromClient；
// 上游经连接池与真实 wire 拨号器到本地 TLS 终端。
type codexGateWebSocketIngress struct {
	client *coderws.Conn
	done   chan error
}

// startWebSocketIngress 建立一条下游 WS 入站会话，first 是客户端首帧。调用前须 enableWebSocket。
func (g *codexGateService) startWebSocketIngress(
	t *testing.T,
	header http.Header,
	apiKey *APIKey,
	first []byte,
) *codexGateWebSocketIngress {
	t.Helper()
	ingress := &codexGateWebSocketIngress{done: make(chan error, 1)}
	wsServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		conn, err := coderws.Accept(writer, request, &coderws.AcceptOptions{
			CompressionMode: coderws.CompressionContextTakeover,
		})
		if err != nil {
			ingress.done <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		ginContext, _ := gin.CreateTestContext(httptest.NewRecorder())
		cloned := request.Clone(request.Context())
		cloned.Header = request.Header.Clone()
		for name, values := range header {
			cloned.Header.Del(name)
			for _, value := range values {
				cloned.Header.Add(name, value)
			}
		}
		cloned.URL.Path = "/v1/responses"
		ginContext.Request = cloned
		ginContext.Set("api_key", apiKey)
		ctx := g.ingress(ginContext)
		readCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, firstMessage, readErr := conn.Read(readCtx)
		cancel()
		if readErr != nil {
			ingress.done <- readErr
			return
		}
		ingress.done <- g.service.ProxyResponsesWebSocketFromClient(
			ctx, ginContext, conn, g.account, "oauth-test-token", firstMessage, nil,
		)
	}))
	t.Cleanup(wsServer.Close)
	dialCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(dialCtx, "ws"+strings.TrimPrefix(wsServer.URL, "http"), nil)
	require.NoError(t, err)
	t.Cleanup(func() { _ = client.CloseNow() })
	ingress.client = client
	ingress.send(t, first)
	return ingress
}

// send 由下游客户端发出一帧文本。
func (w *codexGateWebSocketIngress) send(t *testing.T, payload []byte) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	require.NoError(t, w.client.Write(ctx, coderws.MessageText, payload))
}

// readUntilCompleted 读取下游事件直到 response.completed，返回该事件的响应 ID。
func (w *codexGateWebSocketIngress) readUntilCompleted(t *testing.T) string {
	t.Helper()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, message, err := w.client.Read(ctx)
		cancel()
		if err != nil {
			select {
			case serverErr := <-w.done:
				t.Fatalf("WS 入站会话提前结束：读 %v，服务端 %v", err, serverErr)
			default:
			}
			t.Fatalf("读取下游事件失败：%v", err)
		}
		var event struct {
			Type     string `json:"type"`
			Response struct {
				ID string `json:"id"`
			} `json:"response"`
		}
		_ = json.Unmarshal(message, &event)
		if event.Type == "error" {
			t.Fatalf("下游收到错误事件：%s", message)
		}
		if event.Type == "response.completed" {
			return event.Response.ID
		}
	}
}

// close 关闭下游连接并等待服务端会话结束。
func (w *codexGateWebSocketIngress) close(t *testing.T) {
	t.Helper()
	_ = w.client.Close(coderws.StatusNormalClosure, "")
	select {
	case <-w.done:
	case <-time.After(5 * time.Second):
		t.Fatal("等待 WS 入站会话结束超时")
	}
}
