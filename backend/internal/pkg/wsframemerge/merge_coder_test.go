package wsframemerge

import (
	"bytes"
	"context"
	"crypto/rand"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

// 本文件用真实的 coder/websocket 客户端与服务端做端到端验证：
// 服务端的监听连接记录客户端发来的全部原始字节，测试再把原始字节解析成帧，
// 断言每条消息在线上的帧数、FIN 位与 RSV1 位，并核对对端解压后的消息内容。

const officialOffer = "permessage-deflate; client_max_window_bits"

type recordingListener struct {
	net.Listener
	mu    sync.Mutex
	conns []*recordingConn
}

func (l *recordingListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	rc := &recordingConn{Conn: conn}
	l.mu.Lock()
	l.conns = append(l.conns, rc)
	l.mu.Unlock()
	return rc, nil
}

type recordingConn struct {
	net.Conn
	mu  sync.Mutex
	buf bytes.Buffer
}

func (c *recordingConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if n > 0 {
		c.mu.Lock()
		_, _ = c.buf.Write(p[:n])
		c.mu.Unlock()
	}
	return n, err
}

// wireBytes 返回 HTTP 升级请求之后的全部 WebSocket 字节。
func (c *recordingConn) wireBytes() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	raw := c.buf.Bytes()
	idx := bytes.Index(raw, []byte("\r\n\r\n"))
	if idx < 0 {
		return nil
	}
	return append([]byte(nil), raw[idx+4:]...)
}

// offerRewriteTransport 复刻 service 包里官方出站握手的压缩提议改写，并可选地包上合并层。
type offerRewriteTransport struct {
	base http.RoundTripper
	wrap bool
}

func (t *offerRewriteTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	cloned := req.Clone(req.Context())
	cloned.Header = req.Header.Clone()
	if cloned.Header.Get("Sec-WebSocket-Extensions") == "permessage-deflate" {
		cloned.Header.Set("Sec-WebSocket-Extensions", officialOffer)
	}
	resp, err := t.base.RoundTrip(cloned)
	if err != nil || resp == nil {
		return resp, err
	}
	if t.wrap && resp.StatusCode == http.StatusSwitchingProtocols {
		if rwc, ok := resp.Body.(io.ReadWriteCloser); ok {
			resp.Body = Wrap(rwc)
		}
	}
	return resp, nil
}

type echoServer struct {
	url      string
	listener *recordingListener
	received chan []byte
}

func startEchoServer(t *testing.T) *echoServer {
	t.Helper()
	received := make(chan []byte, 256)
	ts := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{CompressionMode: websocket.CompressionContextTakeover})
		if err != nil {
			return
		}
		defer func() { _ = conn.CloseNow() }()
		conn.SetReadLimit(64 << 20)
		for {
			_, msg, err := conn.Read(r.Context())
			if err != nil {
				return
			}
			received <- msg
			// 回一个确认，让客户端知道这条消息已被对端完整解压读取。
			if err := conn.Write(r.Context(), websocket.MessageText, []byte("ack")); err != nil {
				return
			}
		}
	}))
	listener := &recordingListener{Listener: ts.Listener}
	ts.Listener = listener
	ts.Start()
	t.Cleanup(ts.Close)
	return &echoServer{url: "ws" + strings.TrimPrefix(ts.URL, "http"), listener: listener, received: received}
}

// sendAll 用 coder 客户端依次发送消息，每条等服务端确认后再发下一条。
// 返回服务端收到的消息与线上记录到的客户端帧。
func sendAll(t *testing.T, wrap bool, threshold int, messages [][]byte) ([][]byte, []frame) {
	t.Helper()
	server := startEchoServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	client, _, err := websocket.Dial(ctx, server.url, &websocket.DialOptions{
		HTTPClient:           &http.Client{Transport: &offerRewriteTransport{base: http.DefaultTransport, wrap: wrap}},
		CompressionMode:      websocket.CompressionContextTakeover,
		CompressionThreshold: threshold,
	})
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()
	client.SetReadLimit(1 << 20)
	var got [][]byte
	for i, message := range messages {
		require.NoError(t, client.Write(ctx, websocket.MessageText, message), "message %d", i)
		got = append(got, <-server.received)
		_, ack, readErr := client.Read(ctx)
		require.NoError(t, readErr)
		require.Equal(t, "ack", string(ack))
	}
	require.NoError(t, client.Close(websocket.StatusNormalClosure, ""))
	// 等服务端连接关闭，确保记录的字节完整。
	time.Sleep(50 * time.Millisecond)
	server.listener.mu.Lock()
	defer server.listener.mu.Unlock()
	require.Len(t, server.listener.conns, 1)
	frames := parseFrames(t, server.listener.conns[0].wireBytes())
	return got, frames
}

func testMessages(t *testing.T) [][]byte {
	t.Helper()
	random := func(n int) []byte {
		b := make([]byte, n)
		_, err := rand.Read(b)
		require.NoError(t, err)
		// 保持是合法 JSON 文本也不重要，这里只验证帧层；用 base16 保持 UTF-8。
		return []byte(fmt.Sprintf("%x", b))[:n]
	}
	repeated := func(n int) []byte { return bytes.Repeat([]byte(`{"type":"response.create","k":"v"}`), n/33+1)[:n] }
	var out [][]byte
	for _, size := range []int{1, 20, 100, 127, 128, 200, 1000, 4096, 65535, 65536 + 1, 300000} {
		out = append(out, repeated(size))
	}
	// 不可压缩的大消息会让 flate 吐出大量块，是分片最多的情形。
	out = append(out, random(1<<20), random(3<<20))
	return out
}

func TestCoderWithoutMergeFragmentsMessages(t *testing.T) {
	// 对照组：不包合并层时，coder 压缩写路径把消息拆成多帧。这条测试证明下面的断言有区分度。
	messages := testMessages(t)
	got, frames := sendAll(t, false, 1, messages)
	for i := range messages {
		require.True(t, bytes.Equal(messages[i], got[i]))
	}
	dataFrames := 0
	for _, f := range frames {
		if f.opcode <= 2 {
			dataFrames++
		}
	}
	t.Logf("未合并：%d 条消息在线上是 %d 个数据帧（其中 RSV1 首帧 %d 个）", len(messages), dataFrames, countRSV1(frames))
	require.Greater(t, dataFrames, len(messages), "未合并时帧数应多于消息数")
}

func TestCoderWithMergeWritesOneFramePerMessage(t *testing.T) {
	messages := testMessages(t)
	got, frames := sendAll(t, true, 1, messages)
	require.Len(t, got, len(messages))
	for i := range messages {
		require.True(t, bytes.Equal(messages[i], got[i]), "message %d 对端解压后内容必须一致", i)
	}
	var data []frame
	for _, f := range frames {
		if f.opcode <= 2 {
			data = append(data, f)
		}
	}
	require.Len(t, data, len(messages), "每条消息恰好一个数据帧")
	for i, f := range data {
		require.True(t, f.fin, "message %d FIN=1", i)
		require.Equal(t, byte(1), f.opcode, "message %d 为文本帧", i)
		require.Equal(t, byte(0x40), f.rsv, "message %d 必须置 RSV1（压缩）", i)
		require.True(t, f.masked, "message %d 客户端帧必须带掩码", i)
	}
}

func TestCoderThresholdOneCompressesSmallMessagesButDefaultDoesNot(t *testing.T) {
	// 官方客户端不论大小都压缩（RSV1=1）。coder 默认只压缩不小于 128 字节的消息。
	small := [][]byte{[]byte(`{"type":"response.cancel"}`), []byte("x")}
	_, defaultFrames := sendAll(t, true, 0, small)
	var defaultData []frame
	for _, f := range defaultFrames {
		if f.opcode <= 2 {
			defaultData = append(defaultData, f)
		}
	}
	require.Len(t, defaultData, 2)
	require.Equal(t, byte(0), defaultData[0].rsv, "默认阈值下小消息不压缩")

	_, forced := sendAll(t, true, 1, small)
	var forcedData []frame
	for _, f := range forced {
		if f.opcode <= 2 {
			forcedData = append(forcedData, f)
		}
	}
	require.Len(t, forcedData, 2)
	for _, f := range forcedData {
		require.True(t, f.fin)
		require.Equal(t, byte(0x40), f.rsv)
	}
}

func TestCoderMergeKeepsPingPongWorkingDuringWrites(t *testing.T) {
	server := startEchoServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	client, _, err := websocket.Dial(ctx, server.url, &websocket.DialOptions{
		HTTPClient:           &http.Client{Transport: &offerRewriteTransport{base: http.DefaultTransport, wrap: true}},
		CompressionMode:      websocket.CompressionContextTakeover,
		CompressionThreshold: 1,
	})
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()

	// 后台读循环：处理服务端的 pong 和 ack。
	acks := make(chan struct{}, 64)
	go func() {
		for {
			if _, _, readErr := client.Read(ctx); readErr != nil {
				return
			}
			acks <- struct{}{}
		}
	}()
	payload := bytes.Repeat([]byte(`{"type":"response.create"}`), 40000)
	const rounds = 8
	pingErr := make(chan error, rounds)
	for i := 0; i < rounds; i++ {
		go func() { pingErr <- client.Ping(ctx) }()
		require.NoError(t, client.Write(ctx, websocket.MessageText, payload))
		require.True(t, bytes.Equal(payload, <-server.received))
		<-acks
	}
	for i := 0; i < rounds; i++ {
		require.NoError(t, <-pingErr)
	}
	require.NoError(t, client.Close(websocket.StatusNormalClosure, ""))
	time.Sleep(50 * time.Millisecond)
	server.listener.mu.Lock()
	defer server.listener.mu.Unlock()
	frames := parseFrames(t, server.listener.conns[0].wireBytes())
	data := 0
	for _, f := range frames {
		if f.opcode <= 2 {
			data++
			require.True(t, f.fin)
			require.Equal(t, byte(0x40), f.rsv)
		}
	}
	require.Equal(t, rounds, data)
}

func countRSV1(frames []frame) int {
	n := 0
	for _, f := range frames {
		if f.opcode <= 2 && f.rsv == 0x40 {
			n++
		}
	}
	return n
}
