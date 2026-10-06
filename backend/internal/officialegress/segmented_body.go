package officialegress

import (
	"io"
	"sync"
)

// 每个块单独分配，不随正文增长搬移旧字节；尾块的未使用空间至多为一个块。
const segmentedBodyBlockSize = 64 * 1024

// segmentedBodyWriter 仅在编译期间使用，finish 后移交全部块的只读所有权。
// 压缩器会复用自己的输出缓冲，因此 Write 必须复制入参，不能只保存切片。
type segmentedBodyWriter struct {
	segments [][]byte
	length   int64
	finished *segmentedBodyContent
}

func newSegmentedBodyWriter() *segmentedBodyWriter {
	return &segmentedBodyWriter{}
}

func (w *segmentedBodyWriter) Write(p []byte) (int, error) {
	if w.finished != nil {
		return 0, io.ErrClosedPipe
	}
	written := len(p)
	for len(p) > 0 {
		if len(w.segments) == 0 || len(w.segments[len(w.segments)-1]) == segmentedBodyBlockSize {
			w.segments = append(w.segments, make([]byte, 0, segmentedBodyBlockSize))
		}
		index := len(w.segments) - 1
		segment := w.segments[index]
		n := min(len(p), cap(segment)-len(segment))
		w.segments[index] = append(segment, p[:n]...)
		p = p[n:]
	}
	w.length += int64(written)
	return written, nil
}

func (w *segmentedBodyWriter) finish() RequestBody {
	if w.finished == nil {
		for i, segment := range w.segments {
			w.segments[i] = segment[:len(segment):len(segment)]
		}
		w.finished = &segmentedBodyContent{segments: w.segments, length: w.length}
		w.segments = nil
	}
	return RequestBody{state: &requestBodyState{
		mode: RequestBodyReplayable, segmented: w.finished, length: w.finished.length,
	}}
}

// 分段字节构造后永久只读。连续视图只为显式要求 []byte 的兼容读取方缓存，
// 正常发送、重放和摘要不经过 materialize。
type segmentedBodyContent struct {
	segments [][]byte
	length   int64
	once     sync.Once
	bytes    []byte
}

func (c *segmentedBodyContent) copyBytes() []byte {
	if c.length == 0 {
		return nil
	}
	result := make([]byte, c.length)
	offset := 0
	for _, segment := range c.segments {
		offset += copy(result[offset:], segment)
	}
	return result
}

func (c *segmentedBodyContent) materialize() []byte {
	c.once.Do(func() { c.bytes = c.copyBytes() })
	return c.bytes
}

// 每个 reader 独占游标，不修改共享的块描述或字节。锁只保护短暂的内存读取，
// 使 HTTP transport 并发调用 Read/Close 时安全，并让 Close 释放本 reader 的引用。
type segmentedBodyReader struct {
	mu       sync.Mutex
	segments [][]byte
	index    int
	offset   int
	closed   bool
}

func (r *segmentedBodyReader) Read(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return 0, io.ErrClosedPipe
	}
	if len(p) == 0 {
		return 0, nil
	}
	if len(r.segments) == 0 {
		return 0, io.EOF
	}
	written := 0
	for written < len(p) {
		segment := r.segments[r.index]
		n := copy(p[written:], segment[r.offset:])
		written += n
		r.offset += n
		if r.offset == len(segment) {
			r.index++
			r.offset = 0
			if r.index == len(r.segments) {
				r.segments = nil
				break
			}
		}
	}
	return written, nil
}

func (r *segmentedBodyReader) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed = true
	r.segments = nil
	return nil
}
