package service

import (
	"bytes"
	"compress/flate"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

// 官方出站 WebSocket 的发送形态：一条消息只写一个 FIN=1、RSV1=1 的数据帧（官方客户端是整条压缩后写单帧）。
// 这些测试用真实的 coder/websocket 客户端经 officialEgressWebSocketRoundTripper 拨号，
// 服务端监听连接记录原始字节，再解析成帧来断言线上形态。

type singleFrameRecordingListener struct {
	net.Listener
	mu    sync.Mutex
	conns []*singleFrameRecordingConn
}

func (l *singleFrameRecordingListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	recording := &singleFrameRecordingConn{Conn: conn}
	l.mu.Lock()
	l.conns = append(l.conns, recording)
	l.mu.Unlock()
	return recording, nil
}

type singleFrameRecordingConn struct {
	net.Conn
	mu  sync.Mutex
	buf bytes.Buffer
}

func (c *singleFrameRecordingConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if n > 0 {
		c.mu.Lock()
		_, _ = c.buf.Write(p[:n])
		c.mu.Unlock()
	}
	return n, err
}

// singleFrameWire 是线上解析出的一个客户端帧。
type singleFrameWire struct {
	fin     bool
	rsv     byte
	opcode  byte
	payload []byte
}

func parseSingleFrameWire(t *testing.T, conn *singleFrameRecordingConn) []singleFrameWire {
	t.Helper()
	conn.mu.Lock()
	raw := append([]byte(nil), conn.buf.Bytes()...)
	conn.mu.Unlock()
	idx := bytes.Index(raw, []byte("\r\n\r\n"))
	require.GreaterOrEqual(t, idx, 0)
	raw = raw[idx+4:]
	var frames []singleFrameWire
	for len(raw) > 0 {
		frame := singleFrameWire{fin: raw[0]&0x80 != 0, rsv: raw[0] & 0x70, opcode: raw[0] & 0x0f}
		masked := raw[1]&0x80 != 0
		length := int(raw[1] & 0x7f)
		off := 2
		switch length {
		case 126:
			length = int(binary.BigEndian.Uint16(raw[off:]))
			off += 2
		case 127:
			length = int(binary.BigEndian.Uint64(raw[off:]))
			off += 8
		}
		var key [4]byte
		if masked {
			copy(key[:], raw[off:off+4])
			off += 4
		}
		frame.payload = append([]byte(nil), raw[off:off+length]...)
		for i := range frame.payload {
			if masked {
				frame.payload[i] ^= key[i&3]
			}
		}
		frames = append(frames, frame)
		raw = raw[off+length:]
	}
	return frames
}

func dialOfficialSingleFrameServer(t *testing.T, messages [][]byte) ([][]byte, []singleFrameWire) {
	t.Helper()
	received := make(chan []byte, len(messages))
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			return
		}
		defer func() { _ = conn.CloseNow() }()
		conn.SetReadLimit(64 << 20)
		for {
			_, message, readErr := conn.Read(r.Context())
			if readErr != nil {
				return
			}
			received <- message
			if conn.Write(r.Context(), coderws.MessageText, []byte("ack")) != nil {
				return
			}
		}
	}))
	listener := &singleFrameRecordingListener{Listener: server.Listener}
	server.Listener = listener
	server.Start()
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{
		HTTPClient:           &http.Client{Transport: &officialEgressWebSocketRoundTripper{base: http.DefaultTransport}},
		CompressionMode:      coderws.CompressionContextTakeover,
		CompressionThreshold: officialOpenAIWSCompressionThresholdBytes,
	})
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()
	var got [][]byte
	for _, message := range messages {
		require.NoError(t, client.Write(ctx, coderws.MessageText, message))
		got = append(got, <-received)
		_, ack, readErr := client.Read(ctx)
		require.NoError(t, readErr)
		require.Equal(t, "ack", string(ack))
	}
	require.NoError(t, client.Close(coderws.StatusNormalClosure, ""))
	time.Sleep(50 * time.Millisecond)
	listener.mu.Lock()
	defer listener.mu.Unlock()
	require.Len(t, listener.conns, 1)
	return got, parseSingleFrameWire(t, listener.conns[0])
}

func TestOfficialEgressWebSocketWritesOneCompressedFramePerMessage(t *testing.T) {
	var messages [][]byte
	for _, size := range []int{1, 25, 127, 128, 500, 4096, 70000, 400000} {
		messages = append(messages, bytes.Repeat([]byte(`{"type":"response.create"}`), size/26+1)[:size])
	}
	// 不可压缩内容让 flate 吐出大量块，是原先分片最多的情形。
	noisy := make([]byte, 0, 1<<20)
	for i := 0; len(noisy) < 1<<20; i++ {
		noisy = append(noisy, []byte(strings.Repeat(string(rune('a'+i%26)), 1+i%7))...)
		noisy = append(noisy, byte('0'+(i*7919)%10))
	}
	messages = append(messages, noisy[:1<<20])

	got, frames := dialOfficialSingleFrameServer(t, messages)
	for i := range messages {
		require.True(t, bytes.Equal(messages[i], got[i]), "message %d 对端解压后内容必须一致", i)
	}
	var data []singleFrameWire
	for _, frame := range frames {
		if frame.opcode <= 2 {
			data = append(data, frame)
		}
	}
	require.Len(t, data, len(messages), "每条消息在线上恰好一个数据帧")
	for i, frame := range data {
		require.True(t, frame.fin, "message %d FIN=1", i)
		require.Equal(t, byte(1), frame.opcode, "message %d 为文本帧", i)
		require.Equal(t, byte(0x40), frame.rsv, "message %d 必须置 RSV1，小消息也压缩", i)
	}
}

// 官方客户端用上下文接管：同一连接上每条消息共用 LZ77 窗口（每条连接的第一条消息能被全新解压器单独解开，之后的不能；
// 官方实采 124 条消息的结论）。握手提议是 `permessage-deflate; client_max_window_bits`，没有 no_context_takeover。
// 候选侧同样启用：线上第二条重复内容的消息单独解压必然失败，接续同一解压上下文则成功。
func TestOfficialEgressWebSocketKeepsCompressionContextTakeover(t *testing.T) {
	text := bytes.Repeat([]byte(`{"type":"response.create","input":[{"role":"user","text":"context takeover probe"}]}`), 30)
	got, frames := dialOfficialSingleFrameServer(t, [][]byte{text, text, text})
	for _, message := range got {
		require.True(t, bytes.Equal(text, message))
	}
	var data []singleFrameWire
	for _, frame := range frames {
		if frame.opcode <= 2 {
			data = append(data, frame)
		}
	}
	require.Len(t, data, 3)
	tail := []byte{0x00, 0x00, 0xff, 0xff}
	inflate := func(segments ...[]byte) ([]byte, error) {
		var stream []byte
		for _, segment := range segments {
			stream = append(append(stream, segment...), tail...)
		}
		out, readErr := io.ReadAll(flate.NewReader(bytes.NewReader(stream)))
		if errors.Is(readErr, io.ErrUnexpectedEOF) {
			readErr = nil // 流以同步标记结尾、没有最终块：读到末尾报这个错属正常，已读出的内容有效
		}
		return out, readErr
	}
	first, err := inflate(data[0].payload)
	require.NoError(t, err, "第一条消息可单独解开")
	require.True(t, bytes.Equal(text, first))
	alone, aloneErr := inflate(data[1].payload)
	require.True(t, aloneErr != nil || !bytes.Equal(text, alone), "第二条消息单独解压不能得到原文：它引用了前一条消息的窗口")
	chained, err := inflate(data[0].payload, data[1].payload, data[2].payload)
	require.NoError(t, err)
	require.True(t, bytes.Equal(bytes.Repeat(text, 3), chained), "接续同一解压上下文可依次还原")
	require.Less(t, len(data[1].payload), len(data[0].payload), "重复内容的后续消息应比首条更小（引用了窗口）")
}

func TestOfficialEgressWebSocketRoundTripperLeavesNon101ResponsesUntouched(t *testing.T) {
	body := &wsSingleFrameBody{Reader: strings.NewReader("denied")}
	response := &http.Response{StatusCode: http.StatusForbidden, Body: body}
	roundTripper := &officialEgressWebSocketRoundTripper{base: wsSingleFrameStubRoundTripper{response: response}}
	request, err := http.NewRequest(http.MethodGet, "https://example.com/ws", nil)
	require.NoError(t, err)
	got, err := roundTripper.RoundTrip(request)
	require.NoError(t, err)
	require.Same(t, body, got.Body, "非 101 响应的 Body 不能被包装")
}

type wsSingleFrameBody struct{ io.Reader }

func (*wsSingleFrameBody) Close() error { return nil }

type wsSingleFrameStubRoundTripper struct {
	response *http.Response
}

func (s wsSingleFrameStubRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	return s.response, nil
}
