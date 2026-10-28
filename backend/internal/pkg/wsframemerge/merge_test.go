package wsframemerge

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"

	"github.com/stretchr/testify/require"
)

// frame 是测试里解析出的一个 WebSocket 帧（载荷已去掩码）。
type frame struct {
	fin     bool
	rsv     byte
	opcode  byte
	masked  bool
	payload []byte
}

// encodeFrame 按 RFC 6455 编码一个客户端帧。key 为空表示不加掩码。
func encodeFrame(fin bool, rsv, opcode byte, key *[4]byte, payload []byte) []byte {
	var out bytes.Buffer
	b0 := rsv | opcode
	if fin {
		b0 |= 0x80
	}
	_ = out.WriteByte(b0)
	maskBit := byte(0)
	if key != nil {
		maskBit = 0x80
	}
	switch {
	case len(payload) > 65535:
		_ = out.WriteByte(maskBit | 127)
		var l [8]byte
		binary.BigEndian.PutUint64(l[:], uint64(len(payload)))
		_, _ = out.Write(l[:])
	case len(payload) > 125:
		_ = out.WriteByte(maskBit | 126)
		var l [2]byte
		binary.BigEndian.PutUint16(l[:], uint16(len(payload)))
		_, _ = out.Write(l[:])
	default:
		_ = out.WriteByte(maskBit | byte(len(payload)))
	}
	body := append([]byte(nil), payload...)
	if key != nil {
		_, _ = out.Write(key[:])
		for i := range body {
			body[i] ^= key[i&3]
		}
	}
	_, _ = out.Write(body)
	return out.Bytes()
}

// ParseFrames 把字节流解析为帧序列，供测试断言线上形态。
func parseFrames(t *testing.T, raw []byte) []frame {
	t.Helper()
	var frames []frame
	for len(raw) > 0 {
		require.GreaterOrEqual(t, len(raw), 2, "帧头不完整")
		f := frame{fin: raw[0]&0x80 != 0, rsv: raw[0] & 0x70, opcode: raw[0] & 0x0f, masked: raw[1]&0x80 != 0}
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
		if f.masked {
			copy(key[:], raw[off:off+4])
			off += 4
		}
		require.GreaterOrEqual(t, len(raw), off+length, "帧载荷不完整")
		f.payload = append([]byte(nil), raw[off:off+length]...)
		if f.masked {
			for i := range f.payload {
				f.payload[i] ^= key[i&3]
			}
		}
		frames = append(frames, f)
		raw = raw[off+length:]
	}
	return frames
}

// sink 是记录写入内容的底层连接。
type sink struct {
	bytes.Buffer
	writes   int
	closed   bool
	failNext error
}

func (s *sink) Read(p []byte) (int, error) { return 0, io.EOF }
func (s *sink) Write(p []byte) (int, error) {
	if s.failNext != nil {
		return 0, s.failNext
	}
	s.writes++
	return s.Buffer.Write(p)
}
func (s *sink) Close() error { s.closed = true; return nil }

// feed 把字节流按给定块大小分次写入包装连接。
func feed(t *testing.T, c *Conn, stream []byte, chunk int) {
	t.Helper()
	for len(stream) > 0 {
		n := chunk
		if n > len(stream) {
			n = len(stream)
		}
		written, err := c.Write(stream[:n])
		require.NoError(t, err)
		require.Equal(t, n, written)
		stream = stream[n:]
	}
}

func TestMergeFragmentedMessageIntoSingleFrameForAnyChunking(t *testing.T) {
	key1 := [4]byte{1, 2, 3, 4}
	key2 := [4]byte{9, 8, 7, 6}
	key3 := [4]byte{0xde, 0xad, 0xbe, 0xef}
	payloadA := bytes.Repeat([]byte("abc"), 100)
	payloadB := []byte("0123456789")
	var stream []byte
	// 首帧：文本、RSV1（压缩）、非 FIN；续帧；空载荷 FIN 帧，与 coder/websocket 压缩写路径形态一致。
	stream = append(stream, encodeFrame(false, 0x40, 1, &key1, payloadA)...)
	stream = append(stream, encodeFrame(false, 0, 0, &key2, payloadB)...)
	stream = append(stream, encodeFrame(true, 0, 0, &key3, nil)...)

	want := append(append([]byte(nil), payloadA...), payloadB...)
	for _, chunk := range []int{1, 2, 3, 5, 7, 13, 64, 1000, len(stream)} {
		s := &sink{}
		feed(t, Wrap(s), stream, chunk)
		frames := parseFrames(t, s.Bytes())
		require.Len(t, frames, 1, "chunk=%d", chunk)
		require.True(t, frames[0].fin)
		require.Equal(t, byte(0x40), frames[0].rsv, "RSV1 必须保留在合并后的帧上")
		require.Equal(t, byte(1), frames[0].opcode)
		require.True(t, frames[0].masked)
		require.Equal(t, want, frames[0].payload)
		require.Equal(t, 1, s.writes, "合并帧应一次写出")
	}
}

func TestMergeLengthEncodings(t *testing.T) {
	for _, size := range []int{0, 1, 125, 126, 127, 65535, 65536, 70000} {
		key := [4]byte{5, 6, 7, 8}
		payload := bytes.Repeat([]byte{0xa5}, size)
		// 拆成两个非 FIN 帧再补空 FIN 帧。
		half := size / 2
		var stream []byte
		stream = append(stream, encodeFrame(false, 0x40, 2, &key, payload[:half])...)
		stream = append(stream, encodeFrame(false, 0, 0, &key, payload[half:])...)
		stream = append(stream, encodeFrame(true, 0, 0, &key, nil)...)
		s := &sink{}
		feed(t, Wrap(s), stream, 4096)
		frames := parseFrames(t, s.Bytes())
		require.Len(t, frames, 1, "size=%d", size)
		require.Equal(t, byte(2), frames[0].opcode)
		require.True(t, bytes.Equal(payload, frames[0].payload), "size=%d", size)
	}
}

func TestMergePassesSingleFrameMessageAndKeepsOrder(t *testing.T) {
	key := [4]byte{1, 1, 1, 1}
	var stream []byte
	stream = append(stream, encodeFrame(true, 0x40, 1, &key, []byte("first"))...)
	stream = append(stream, encodeFrame(true, 0, 1, &key, []byte("second"))...)
	s := &sink{}
	feed(t, Wrap(s), stream, 3)
	frames := parseFrames(t, s.Bytes())
	require.Len(t, frames, 2)
	require.Equal(t, []byte("first"), frames[0].payload)
	require.Equal(t, byte(0x40), frames[0].rsv)
	require.Equal(t, []byte("second"), frames[1].payload)
	require.Equal(t, byte(0), frames[1].rsv)
}

func TestMergeForwardsControlFramesImmediatelyInsideFragmentedMessage(t *testing.T) {
	key := [4]byte{3, 3, 3, 3}
	var stream []byte
	stream = append(stream, encodeFrame(false, 0x40, 1, &key, []byte("hello "))...)
	pingBytes := encodeFrame(true, 0, 9, &key, []byte("ping!"))
	stream = append(stream, pingBytes...)
	stream = append(stream, encodeFrame(false, 0, 0, &key, []byte("world"))...)
	stream = append(stream, encodeFrame(true, 0, 0, &key, nil)...)
	stream = append(stream, encodeFrame(true, 0, 8, &key, []byte{0x03, 0xe8})...) // close 帧也要透传

	s := &sink{}
	feed(t, Wrap(s), stream, 2)
	frames := parseFrames(t, s.Bytes())
	require.Len(t, frames, 3)
	require.Equal(t, byte(9), frames[0].opcode, "控制帧先于合并后的数据帧写出")
	require.Equal(t, []byte("ping!"), frames[0].payload)
	require.Equal(t, byte(1), frames[1].opcode)
	require.Equal(t, []byte("hello world"), frames[1].payload)
	require.True(t, frames[1].fin)
	require.Equal(t, byte(8), frames[2].opcode)
	// 控制帧整帧原样透传，连掩码键也不改。
	require.True(t, bytes.HasPrefix(s.Bytes(), pingBytes))
}

func TestMergeSupportsUnmaskedStream(t *testing.T) {
	var stream []byte
	stream = append(stream, encodeFrame(false, 0, 1, nil, []byte("ab"))...)
	stream = append(stream, encodeFrame(true, 0, 0, nil, []byte("cd"))...)
	s := &sink{}
	feed(t, Wrap(s), stream, 1)
	frames := parseFrames(t, s.Bytes())
	require.Len(t, frames, 1)
	require.False(t, frames[0].masked)
	require.Equal(t, []byte("abcd"), frames[0].payload)
}

func TestMergeUsesFreshMaskKeyPerMergedFrame(t *testing.T) {
	key := [4]byte{1, 2, 3, 4}
	stream := append(encodeFrame(false, 0x40, 1, &key, bytes.Repeat([]byte{'x'}, 64)),
		encodeFrame(true, 0, 0, &key, nil)...)
	seen := map[[4]byte]bool{}
	for i := 0; i < 16; i++ {
		s := &sink{}
		feed(t, Wrap(s), stream, 100)
		raw := s.Bytes()
		require.Equal(t, byte(0x80|64), raw[1])
		var got [4]byte
		copy(got[:], raw[2:6])
		seen[got] = true
	}
	require.Greater(t, len(seen), 1, "每个合并帧应重新随机掩码键")
}

func TestMergeRejectsMalformedStreams(t *testing.T) {
	key := [4]byte{1, 2, 3, 4}
	cases := map[string][]byte{
		"没有起始帧的续帧":    encodeFrame(true, 0, 0, &key, []byte("x")),
		"保留操作码":       encodeFrame(true, 0, 3, &key, []byte("x")),
		"消息未结束就来新数据帧": append(encodeFrame(false, 0, 1, &key, []byte("a")), encodeFrame(true, 0, 1, &key, []byte("b"))...),
		"非 FIN 控制帧":   encodeFrame(false, 0, 9, &key, nil),
		"超长控制帧":       encodeFrame(true, 0, 9, &key, bytes.Repeat([]byte{1}, 126)),
	}
	for name, stream := range cases {
		s := &sink{}
		c := Wrap(s)
		_, err := c.Write(stream)
		require.Error(t, err, name)
		require.ErrorIs(t, err, ErrProtocol, name)
		// 错误粘住：之后的写入不再前进。
		_, err2 := c.Write([]byte{0x81, 0x00})
		require.Error(t, err2, name)
	}
}

func TestMergeRejectsOversizedMessage(t *testing.T) {
	// 64 位长度直接声明超限，不需要真的构造这么大的载荷。
	hdr := []byte{0x01, 127, 0, 0, 0, 0, 0x20, 0, 0, 0}
	hdr[1] = 127
	binary.BigEndian.PutUint64(hdr[2:], uint64(MaxMessageBytes)+1)
	s := &sink{}
	_, err := Wrap(s).Write(hdr)
	require.ErrorIs(t, err, ErrProtocol)
}

func TestMergePropagatesInnerWriteError(t *testing.T) {
	key := [4]byte{1, 2, 3, 4}
	sentinel := errors.New("底层写失败")
	s := &sink{failNext: sentinel}
	c := Wrap(s)
	stream := append(encodeFrame(false, 0x40, 1, &key, []byte("a")), encodeFrame(true, 0, 0, &key, nil)...)
	_, err := c.Write(stream)
	require.ErrorIs(t, err, sentinel)
}

func TestWrapNilAndPassThroughReadClose(t *testing.T) {
	require.Nil(t, Wrap(nil))
	s := &sink{}
	c := Wrap(s)
	n, err := c.Read(make([]byte, 1))
	require.Equal(t, 0, n)
	require.ErrorIs(t, err, io.EOF)
	require.NoError(t, c.Close())
	require.True(t, s.closed)
	require.Same(t, s, c.Unwrap())
}
