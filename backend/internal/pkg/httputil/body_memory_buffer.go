package httputil

import (
	"errors"
	"io"
)

// ErrRequestBodyMemoryUnavailable 表示本平台不能提供可显式释放的正文内存。
// 调用方应尝试普通磁盘，不能把 Go 堆伪装成已计量的匿名映射。
var ErrRequestBodyMemoryUnavailable = errors.New("正文系统内存不可用")

// RequestBodyMemoryBuffer 是不向外借出切片的定长正文缓冲。写入阶段结束后才能交给
// 重放读者；ReadAt、WriteAt 与 Close 串行化，取消释放不会与正在复制的内存发生竞争。
// 完整映射容量计入 ActiveOwnedRequestBodyBytes，Close 成功后同步扣减。
type RequestBodyMemoryBuffer struct{ owner *OwnedRequestBody }

func NewRequestBodyMemoryBuffer(size int) (*RequestBodyMemoryBuffer, error) {
	owner, err := newOwnedRequestBody(size)
	if err != nil {
		return nil, err
	}
	if !owner.IsMapped() {
		_ = owner.Close()
		return nil, ErrRequestBodyMemoryUnavailable
	}
	return &RequestBodyMemoryBuffer{owner: owner}, nil
}

func (b *RequestBodyMemoryBuffer) ReadAt(p []byte, offset int64) (int, error) {
	b.owner.mu.Lock()
	defer b.owner.mu.Unlock()
	if b.owner.storage == nil {
		return 0, ErrOwnedRequestBodyClosed
	}
	if offset < 0 {
		return 0, errors.New("正文读取偏移为负数")
	}
	if offset >= int64(len(b.owner.body)) {
		return 0, io.EOF
	}
	n := copy(p, b.owner.body[offset:])
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func (b *RequestBodyMemoryBuffer) WriteAt(p []byte, offset int64) (int, error) {
	b.owner.mu.Lock()
	defer b.owner.mu.Unlock()
	if b.owner.storage == nil {
		return 0, ErrOwnedRequestBodyClosed
	}
	if offset < 0 || offset > int64(len(b.owner.body)) {
		return 0, io.ErrShortWrite
	}
	n := copy(b.owner.body[offset:], p)
	if n < len(p) {
		return n, io.ErrShortWrite
	}
	return n, nil
}

func (b *RequestBodyMemoryBuffer) Close() error { return b.owner.Close() }
