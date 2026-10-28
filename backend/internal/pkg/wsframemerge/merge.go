// Package wsframemerge 把 WebSocket 客户端写出的“分片消息”在线路上合并成单个帧。
//
// 背景：官方 Codex 客户端（tungstenite 的 OpenAI 分支，开 permessage-deflate）每条应用层消息只写
// 一个数据帧：整条消息先压缩成一个缓冲区，再作为 FIN=1、RSV1=1 的单帧写出，不分片。网关向上游
// 拨号用的 github.com/coder/websocket 在压缩写路径上则是“每吐出一块 flate 输出就写一个非 FIN 帧，
// 最后再补一个空载荷的 FIN 帧”，所以一条消息在线路上是若干数据帧加一个空 FIN 帧。
// coder/websocket 的公开接口无法把一条压缩消息写成单帧，本包因此不改依赖源码，而是在
// 它的底层读写连接（握手 101 响应的 Body）上做一层“写方向的帧合并”：
//
//   - 数据帧（文本、二进制及其续帧）：按 RFC 6455 解析帧头，去掩码后累积载荷，直到遇到 FIN=1
//     的帧，再用“首帧的操作码与 RSV 位、FIN=1、新的随机掩码键”一次性写出合并后的单帧。
//     permessage-deflate 的 RSV1 只出现在消息的首帧，合并后保持不变，压缩载荷字节按序拼接，
//     对端解压得到的消息内容不变。
//   - 控制帧（ping、pong、close）：整帧原样立即透传。RFC 6455 允许控制帧插在分片消息中间，
//     透传不会破坏尚在累积的数据消息。
//
// 读方向（对端发来的帧）不做任何处理。本包只做字节搬运，不发起网络连接。
//
// 升级 coder/websocket 时的维护：本包不依赖该库的任何内部实现，只依赖两个外部事实——
// 握手成功后 http.Response.Body 是 io.ReadWriteCloser（库自己也要求这一点，否则拨号失败），以及库写出的
// 是符合 RFC 6455 的客户端帧。升级后跑 merge_coder_test.go（真实 coder 客户端与服务端的端到端用例）和
// service 包的 TestOfficialEgressWebSocketWritesOneCompressedFramePerMessage：库若改成原生单帧，
// 合并层对单帧消息是原样重组，行为不变；库若改了帧的写法，用例会立刻报出。
package wsframemerge

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"sync"
)

const (
	// MaxMessageBytes 是单条合并消息累积载荷的上限。超限视为协议错误并让写入失败（失败关闭），
	// 避免异常调用方让合并缓冲无限增长。官方请求体通常在 KB 到数 MB 量级，该值远高于此。
	MaxMessageBytes = 256 << 20

	// headroom 是合并缓冲区头部为帧头预留的字节数：2 字节基础头 + 8 字节扩展长度 + 4 字节掩码键。
	headroom = 14

	// retainBufferBytes 是合并缓冲区在消息结束后继续持有的容量上限，超过则释放，
	// 避免一次超大消息让连接长期占用大块内存。
	retainBufferBytes = 1 << 20
)

// 解析状态。
const (
	stateHeader  = iota // 正在收集帧头
	statePayload        // 正在收集帧载荷
)

// ErrProtocol 表示写入的字节流不是合法的客户端 WebSocket 帧序列。
var ErrProtocol = errors.New("wsframemerge: 写入的字节流不是合法的 WebSocket 帧序列")

// ErrMessageTooLarge 表示单条消息累积载荷超过 MaxMessageBytes。
var ErrMessageTooLarge = errors.New("wsframemerge: 单条消息超过合并缓冲上限")

// Conn 包裹一个已升级的 WebSocket 底层连接，写方向把分片消息合并为单帧，读方向与关闭原样透传。
// 写方向由 coder/websocket 在持有自己写锁时调用；这里仍加互斥保护，使并发误用也不会破坏解析状态。
type Conn struct {
	inner io.ReadWriteCloser

	mu  sync.Mutex
	err error // 一旦出错即粘住：后续写入全部返回同一个错误

	state int
	// 帧头收集
	hdr     [headroom]byte
	hdrHave int
	// 当前帧
	fin       bool
	rsv       byte // 帧头第一个字节的 RSV1~3 位（0x70 掩码后）
	opcode    byte
	masked    bool
	maskKey   [4]byte
	maskPos   int   // 已处理的载荷字节数，用于选择掩码键字节
	remaining int64 // 当前帧尚未收到的载荷字节数
	control   bool

	// 控制帧：连帧头一起原样缓存，收齐后一次透传
	ctrl []byte

	// 数据消息：msg[headroom:] 是已去掩码的累积载荷，msg[:headroom] 留给合并帧头
	inMessage bool
	msgOpcode byte
	msgRSV    byte
	msgMasked bool
	msg       []byte
}

// Wrap 返回合并写方向分片的连接包装。inner 为空时返回 nil。
func Wrap(inner io.ReadWriteCloser) *Conn {
	if inner == nil {
		return nil
	}
	return &Conn{inner: inner}
}

// Read 原样透传。
func (c *Conn) Read(p []byte) (int, error) { return c.inner.Read(p) }

// Close 原样透传。
func (c *Conn) Close() error { return c.inner.Close() }

// Unwrap 返回被包裹的底层连接，供需要探测底层能力的调用方使用。
func (c *Conn) Unwrap() io.ReadWriteCloser { return c.inner }

// Write 接收 coder/websocket 刷出的字节（可能在帧中间切断），按帧合并后写入底层连接。
// 成功时恒返回 len(p)：数据帧的字节可能仍留在合并缓冲里，等该消息的 FIN 帧到达时才真正写出。
func (c *Conn) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return 0, c.err
	}
	consumed := 0
	for consumed < len(p) {
		switch c.state {
		case stateHeader:
			n, err := c.collectHeader(p[consumed:])
			consumed += n
			if err != nil {
				return consumed, c.fail(err)
			}
		case statePayload:
			n, err := c.collectPayload(p[consumed:])
			consumed += n
			if err != nil {
				return consumed, c.fail(err)
			}
		}
		// 零载荷的帧在帧头收齐后没有后续字节，需要立刻结算，否则会等到下一次 Write 才处理。
		if c.state == statePayload && c.remaining == 0 {
			if err := c.finishFrame(); err != nil {
				return consumed, c.fail(err)
			}
		}
	}
	return consumed, nil
}

func (c *Conn) fail(err error) error {
	c.err = err
	return err
}

// headerLen 根据已收到的前两个字节算出完整帧头长度。
func headerLen(b0, b1 byte) int {
	n := 2
	switch b1 & 0x7f {
	case 126:
		n += 2
	case 127:
		n += 8
	}
	if b1&0x80 != 0 {
		n += 4
	}
	return n
}

// collectHeader 从 p 中收集帧头字节，收齐后解析并切换到载荷状态，返回消耗的字节数。
func (c *Conn) collectHeader(p []byte) (int, error) {
	consumed := 0
	// 先凑够 2 字节才能知道帧头总长。
	for c.hdrHave < 2 && consumed < len(p) {
		c.hdr[c.hdrHave] = p[consumed]
		c.hdrHave++
		consumed++
	}
	if c.hdrHave < 2 {
		return consumed, nil
	}
	need := headerLen(c.hdr[0], c.hdr[1])
	for c.hdrHave < need && consumed < len(p) {
		c.hdr[c.hdrHave] = p[consumed]
		c.hdrHave++
		consumed++
	}
	if c.hdrHave < need {
		return consumed, nil
	}
	if err := c.parseHeader(need); err != nil {
		return consumed, err
	}
	return consumed, nil
}

// parseHeader 解析已收齐的帧头并准备接收载荷。
func (c *Conn) parseHeader(need int) error {
	b0, b1 := c.hdr[0], c.hdr[1]
	c.fin = b0&0x80 != 0
	c.rsv = b0 & 0x70
	c.opcode = b0 & 0x0f
	c.masked = b1&0x80 != 0
	length := int64(b1 & 0x7f)
	off := 2
	switch length {
	case 126:
		length = int64(binary.BigEndian.Uint16(c.hdr[off:]))
		off += 2
	case 127:
		raw := binary.BigEndian.Uint64(c.hdr[off:])
		if raw>>63 != 0 || raw > MaxMessageBytes {
			return fmt.Errorf("%w: 帧载荷长度 %d 不合法或超限", ErrProtocol, raw)
		}
		length = int64(raw)
		off += 8
	}
	if c.masked {
		copy(c.maskKey[:], c.hdr[off:off+4])
	}
	c.maskPos = 0
	c.remaining = length
	c.control = c.opcode&0x08 != 0

	switch {
	case c.control:
		// 控制帧必须 FIN=1 且载荷不超过 125 字节。
		if !c.fin || length > 125 {
			return fmt.Errorf("%w: 控制帧必须 FIN=1 且载荷不超过 125 字节", ErrProtocol)
		}
		c.ctrl = append(c.ctrl[:0], c.hdr[:need]...)
	case c.opcode == 0:
		if !c.inMessage {
			return fmt.Errorf("%w: 出现没有起始帧的续帧", ErrProtocol)
		}
	case c.opcode == 1 || c.opcode == 2:
		if c.inMessage {
			return fmt.Errorf("%w: 上一条消息尚未结束就出现新的数据帧", ErrProtocol)
		}
		c.beginMessage()
	default:
		return fmt.Errorf("%w: 保留操作码 %d", ErrProtocol, c.opcode)
	}
	if !c.control && int64(len(c.msg)-headroom)+length > MaxMessageBytes {
		return ErrMessageTooLarge
	}
	c.hdrHave = 0
	c.state = statePayload
	return nil
}

// beginMessage 以当前帧作为首帧开始累积一条新消息。
func (c *Conn) beginMessage() {
	c.inMessage = true
	c.msgOpcode = c.opcode
	c.msgRSV = c.rsv
	c.msgMasked = c.masked
	if cap(c.msg) < headroom {
		c.msg = make([]byte, headroom, headroom+512)
	}
	c.msg = c.msg[:headroom]
}

// collectPayload 消耗当前帧的载荷字节。载荷收齐时结算该帧，返回消耗的字节数。
func (c *Conn) collectPayload(p []byte) (int, error) {
	take := int64(len(p))
	if take > c.remaining {
		take = c.remaining
	}
	chunk := p[:take]
	if c.control {
		c.ctrl = append(c.ctrl, chunk...)
	} else {
		start := len(c.msg)
		c.msg = append(c.msg, chunk...)
		if c.masked {
			dst := c.msg[start:]
			for i := range dst {
				dst[i] ^= c.maskKey[(c.maskPos+i)&3]
			}
			c.maskPos += len(dst)
		}
	}
	c.remaining -= take
	if c.remaining == 0 {
		if err := c.finishFrame(); err != nil {
			return int(take), err
		}
	}
	return int(take), nil
}

// finishFrame 在一个帧的载荷收齐后结算：控制帧立即透传，数据帧遇到 FIN 时写出合并帧。
func (c *Conn) finishFrame() error {
	c.state = stateHeader
	if c.control {
		err := c.writeAll(c.ctrl)
		c.ctrl = c.ctrl[:0]
		return err
	}
	if !c.fin {
		return nil
	}
	return c.emitMessage()
}

// emitMessage 把累积的消息写成单个 FIN=1 的帧。
func (c *Conn) emitMessage() error {
	payload := c.msg[headroom:]
	length := len(payload)

	// 合并帧头从后往前写进预留区，使帧头与载荷在 c.msg 里连续，一次 Write 发出。
	hl := 2
	switch {
	case length > 65535:
		hl += 8
	case length > 125:
		hl += 2
	}
	if c.msgMasked {
		hl += 4
	}
	start := headroom - hl
	h := c.msg[start:headroom]
	h[0] = 0x80 | c.msgRSV | c.msgOpcode
	maskBit := byte(0)
	if c.msgMasked {
		maskBit = 0x80
	}
	off := 2
	switch {
	case length > 65535:
		h[1] = maskBit | 127
		binary.BigEndian.PutUint64(h[off:], uint64(length))
		off += 8
	case length > 125:
		h[1] = maskBit | 126
		binary.BigEndian.PutUint16(h[off:], uint16(length))
		off += 2
	default:
		h[1] = maskBit | byte(length)
	}
	if c.msgMasked {
		var key [4]byte
		if _, err := io.ReadFull(rand.Reader, key[:]); err != nil {
			return fmt.Errorf("wsframemerge: 生成掩码键失败: %w", err)
		}
		copy(h[off:], key[:])
		for i := range payload {
			payload[i] ^= key[i&3]
		}
	}
	err := c.writeAll(c.msg[start:])

	c.inMessage = false
	if cap(c.msg) > retainBufferBytes {
		c.msg = nil
	} else {
		c.msg = c.msg[:headroom]
	}
	return err
}

func (c *Conn) writeAll(b []byte) error {
	n, err := c.inner.Write(b)
	if err != nil {
		return err
	}
	if n != len(b) {
		return io.ErrShortWrite
	}
	return nil
}
