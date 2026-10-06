package officialegress

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"runtime"
	"sync"
	"sync/atomic"
)

// 未指定系统内存额度时，大压缩结果先在堆中保留至多 1MiB，再移交普通文件。
// 正式 Forward 优先使用有额度的系统内存；额度耗尽或映射不可用时转存普通文件。
// 未启用额度的旧兼容入口在普通文件不可用时沿用分段正文。
const requestBodySpoolThreshold = 1 << 20

// RequestBodyStorageError 标识本机正文暂存错误。调用方必须把它归为服务端存储故障，
// 不能当作客户端 JSON 错误、账号异常或上游失败而惩罚账号。
type RequestBodyStorageError struct {
	Operation string
	Err       error
}

func (e *RequestBodyStorageError) Error() string {
	return fmt.Sprintf("官方请求正文暂存失败（%s）: %v", e.Operation, e.Err)
}

func (e *RequestBodyStorageError) Unwrap() error { return e.Err }

var errRequestBodySpoolClosed = errors.New("官方请求正文暂存已关闭")

// requestBodySpoolResource 不直接反向引用 content。正式 Forward 使用存储 scope 的纯
// 取消信号，资源、取消回调和 cleanup 都不回指业务工作区。未指定 scope 的裸调用保留
// 原 context 的精确取消错误；持有业务值的裸调用应显式使用 WithRequestBodyStorageScope。
type requestBodySpoolResource struct {
	mu            sync.Mutex
	dataMu        sync.RWMutex
	ctx           context.Context
	cancel        context.CancelFunc
	file          *os.File
	memory        *requestBodyMemory
	readers       int
	scopeReleased bool
	closed        atomic.Bool
	closeOnce     sync.Once
	closeErr      error
}

func (r *requestBodySpoolResource) failure(operation string, err error) error {
	if contextErr := r.ctx.Err(); contextErr != nil {
		return contextErr
	}
	if err == nil {
		return nil
	}
	return &RequestBodyStorageError{Operation: operation, Err: err}
}

func (r *requestBodySpoolResource) available() error {
	if err := r.ctx.Err(); err != nil {
		return err
	}
	if r.closed.Load() {
		return &RequestBodyStorageError{Operation: "read", Err: errRequestBodySpoolClosed}
	}
	return nil
}

func (r *requestBodySpoolResource) close() error {
	r.dataMu.Lock()
	defer r.dataMu.Unlock()
	r.closeOnce.Do(func() {
		if r.cancel != nil {
			r.cancel()
		}
		r.closed.Store(true)
		if r.file != nil {
			r.closeErr = r.file.Close()
		}
	})
	if r.memory != nil {
		// munmap 失败时保留资源与额度，后续 Close 可以重试，不能误记为已经释放。
		if err := r.memory.close(); err != nil {
			return errors.Join(r.closeErr, err)
		}
		r.memory = nil
	}
	return r.closeErr
}

func (r *requestBodySpoolResource) readAt(p []byte, offset int64) (int, error) {
	if r.file != nil {
		return r.file.ReadAt(p, offset)
	}
	r.dataMu.RLock()
	defer r.dataMu.RUnlock()
	if err := r.available(); err != nil {
		return 0, err
	}
	return r.memory.readAt(p, offset)
}

// 每个已创建的 reader 都有独立租约，包含尚未开始读取和已到 EOF 但未 Close 的读者。
// scope 正常结束后，只要仍有上传租约，GetBody 就能为同一未完成请求追加重放读者。
func (r *requestBodySpoolResource) acquireReader() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.available(); err != nil {
		return err
	}
	r.readers++
	return nil
}

func (r *requestBodySpoolResource) releaseReader() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.readers--
	if r.scopeReleased && r.readers == 0 {
		return r.close()
	}
	return nil
}

func (r *requestBodySpoolResource) releaseScope() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.scopeReleased = true
	if r.readers == 0 {
		return r.close()
	}
	return nil
}

type requestBodySpoolCleanup struct {
	resource *requestBodySpoolResource
	stop     func() bool
}

func (c requestBodySpoolCleanup) close() {
	c.stop()
	_ = c.resource.close()
}

// state、GetBody 工厂和每个活动 reader 都强持有 content；它们共享同一不可变存储，
// 各 reader 的 ReadAt 游标独立。正常 Execute 返回不关闭存储，允许早响应后仍在
// 进行的上传继续读取。正式 Forward 的 scope 和活动 reader 各自持有租约，最后一次释放
// 同步关闭；未指定 scope 时沿用 context 取消关闭，裸 Compile/Prepare 丢弃句柄时由 cleanup 兜底。
// 文件创建后立即 unlink，异常退出不会留下可见正文文件；映射关闭与读取复制互斥。
type requestBodySpoolContent struct {
	resource *requestBodySpoolResource
	length   int64
	cleanup  runtime.Cleanup
	stop     func() bool
	once     sync.Once
	bytes    []byte
	err      error
}

func newRequestBodySpoolContent(resource *requestBodySpoolResource, length int64, stop func() bool) *requestBodySpoolContent {
	content := &requestBodySpoolContent{resource: resource, length: length, stop: stop}
	content.cleanup = runtime.AddCleanup(content, func(cleanup requestBodySpoolCleanup) {
		cleanup.close()
	}, requestBodySpoolCleanup{resource: resource, stop: stop})
	return content
}

func (c *requestBodySpoolContent) close() {
	c.stop()
	_ = c.resource.close()
	c.cleanup.Stop()
}

func (c *requestBodySpoolContent) open() (io.ReadCloser, error) {
	if err := c.resource.acquireReader(); err != nil {
		return nil, err
	}
	return &requestBodySpoolReader{content: c}, nil
}

func (c *requestBodySpoolContent) copyBytes() ([]byte, error) {
	reader, err := c.open()
	if err != nil {
		return nil, err
	}
	defer func() { _ = reader.Close() }()
	result := make([]byte, c.length)
	if _, err := io.ReadFull(reader, result); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *requestBodySpoolContent) materialize() ([]byte, error) {
	if err := c.resource.available(); err != nil {
		return nil, err
	}
	c.once.Do(func() { c.bytes, c.err = c.copyBytes() })
	return c.bytes, c.err
}

// Read/Close 的短锁只保护该 reader 的游标与引用，不串行化不同 reader。文件 Close
// 与 ReadAt 的并发安全由 os.File 保证；取消后返回实际 context 错误，绝不默认为空正文。
type requestBodySpoolReader struct {
	mu      sync.Mutex
	content *requestBodySpoolContent
	offset  int64
	cached  int64
	closed  bool
}

func (r *requestBodySpoolReader) Read(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return 0, io.ErrClosedPipe
	}
	if len(p) == 0 {
		return 0, nil
	}
	if r.content == nil {
		return 0, io.EOF
	}
	content := r.content
	if err := content.resource.available(); err != nil {
		return 0, err
	}
	remaining := content.length - r.offset
	if remaining == 0 {
		return 0, io.EOF
	}
	if int64(len(p)) > remaining {
		p = p[:remaining]
	}
	n, err := content.resource.readAt(p, r.offset)
	r.offset += int64(n)
	if content.resource.file != nil && n > 0 && (r.offset-r.cached >= requestBodySpoolThreshold || r.offset == content.length) {
		// 文件内容已经同步到磁盘。释放已读范围的缓存仅影响性能，其他 reader 始终
		// 可以从同一不可变文件重读；平台实现负责确保原始描述符不会被并发关闭后复用。
		end := r.offset - r.offset%int64(os.Getpagesize())
		length := end - r.cached
		if r.offset == content.length {
			// 零长度提示释放从 cached 到文件末尾的范围，包含最后的不完整页。
			length = 0
		}
		if cacheErr := releaseRequestBodySpoolReadCache(content.resource.file, r.cached, length); cacheErr != nil && err == nil {
			err = cacheErr
		}
		r.cached = end
	}
	if err != nil && err != io.EOF {
		return n, content.resource.failure("read", err)
	}
	if err == io.EOF && r.offset < content.length {
		return n, content.resource.failure("read", io.ErrUnexpectedEOF)
	}
	return n, err
}

func (r *requestBodySpoolReader) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil
	}
	r.closed = true
	content := r.content
	r.content = nil
	if content != nil {
		return content.resource.releaseReader()
	}
	return nil
}

// requestBodyOutputWriter 的所有权在 finish 成功后才移交 RequestBody；此前每个错误
// 都由 abort 立即关闭存储。压缩器只写一次，切换存储不触发二次压缩或完整字节拼接。
type requestBodyOutputWriter struct {
	ctx         context.Context
	memory      *segmentedBodyWriter
	resource    *requestBodySpoolResource
	stop        func() bool
	length      int64
	digest      hash.Hash
	owned       *requestBodySpoolResource
	ownedStop   func() bool
	memoryTried bool
	// 文件写入按固定窗口同步并丢页；记录独立于逻辑 length，便于迁移已存在的内存前缀。
	spoolWritten  int64
	spoolReleased int64
	disabled      bool
	finished      bool
}

func newRequestBodyOutputWriter(ctx context.Context) *requestBodyOutputWriter {
	return &requestBodyOutputWriter{ctx: ctx, memory: newSegmentedBodyWriter(), digest: sha256.New()}
}

func (w *requestBodyOutputWriter) Write(p []byte) (int, error) {
	defer startRequestBodyTiming(w.ctx, "output_write")()
	n, err := w.write(p)
	_, _ = w.digest.Write(p[:n])
	return n, err
}

func (w *requestBodyOutputWriter) write(p []byte) (int, error) {
	if w.finished {
		return 0, io.ErrClosedPipe
	}
	if err := w.ctx.Err(); err != nil {
		return 0, err
	}
	if !w.memoryTried {
		w.memoryTried = true
		if scope := requestBodyStorageScopeFromContext(w.ctx); scope != nil && scope.memoryBudget.limit >= requestBodyMemoryBlockSize {
			ctx, cancel := context.WithCancel(context.Background())
			w.owned = &requestBodySpoolResource{ctx: ctx, cancel: cancel, memory: &requestBodyMemory{budget: scope.memoryBudget, allocate: scope.allocateMemory}}
			var err error
			w.ownedStop, err = scope.attach(w.owned)
			if err != nil {
				return 0, err
			}
		}
	}
	if w.owned != nil {
		w.owned.dataMu.Lock()
		err := w.owned.available()
		n := 0
		if err == nil {
			n, err = w.owned.memory.write(p)
		}
		w.owned.dataMu.Unlock()
		w.length += int64(n)
		if err == nil {
			return n, nil
		}
		if contextErr := w.ctx.Err(); contextErr != nil {
			return n, contextErr
		}
		// 已压缩的前缀只迁移一次；不重新压缩，也不申请完整连续副本。
		if spillErr := w.spill(); spillErr != nil {
			return n, spillErr
		}
		remaining, err := w.writeSpool(p[n:])
		w.length += int64(remaining)
		return n + remaining, err
	}
	if w.resource == nil && !w.disabled && w.length+int64(len(p)) > requestBodySpoolThreshold {
		if err := w.spill(); err != nil {
			return 0, err
		}
	}
	if w.resource == nil {
		n, err := w.memory.Write(p)
		w.length += int64(n)
		return n, err
	}
	n, err := w.writeSpool(p)
	w.length += int64(n)
	return n, err
}

func (w *requestBodyOutputWriter) writeSpool(p []byte) (int, error) {
	written := 0
	for len(p) > 0 {
		if err := w.resource.available(); err != nil {
			return written, err
		}
		remaining := int64(requestBodySpoolThreshold) - (w.spoolWritten - w.spoolReleased)
		size := min(len(p), int(remaining))
		n, err := w.resource.file.Write(p[:size])
		written += n
		w.spoolWritten += int64(n)
		if err == nil && n != size {
			err = io.ErrShortWrite
		}
		if err != nil {
			return written, w.resource.failure("write", err)
		}
		p = p[n:]
		if w.spoolWritten-w.spoolReleased == requestBodySpoolThreshold {
			// Linux 必须先同步脏页再提示丢页，避免大正文在 finish 之前占满 cgroup
			// 文件缓存；单次 Write 很大时也会在每个窗口边界停下同步。
			if err := flushRequestBodySpoolWriteCache(w.resource.file, w.spoolReleased, requestBodySpoolThreshold); err != nil {
				return written, w.resource.failure("sync", err)
			}
			w.spoolReleased = w.spoolWritten
		}
	}
	return written, nil
}

func (w *requestBodyOutputWriter) spill() error {
	directory := os.TempDir()
	if !requestBodySpoolAllowed(directory) {
		if w.owned != nil {
			return &RequestBodyStorageError{Operation: "spill", Err: errors.New("系统内存额度不足或不可用，且暂存目录不是普通磁盘")}
		}
		w.disabled = true
		return nil
	}
	file, err := os.CreateTemp(directory, "sub2api-egress-*")
	if err != nil {
		return &RequestBodyStorageError{Operation: "create", Err: err}
	}
	storageContext := w.ctx
	scope := requestBodyStorageScopeFromContext(w.ctx)
	var storageCancel context.CancelFunc
	if scope != nil {
		// 每个文件有独立纯取消信号，scope 结束不能取消仍有上传读者的其他文件。
		storageContext, storageCancel = context.WithCancel(context.Background())
	} else if storageContext.Done() == nil {
		// 上游可使用 WithoutCancel 保持独立发送；这种 context 的值可能包含整份
		// 原始正文。资源只需取消信号，cleanup 参数不能额外保活这些业务值。
		storageContext = context.Background()
	}
	resource := &requestBodySpoolResource{ctx: storageContext, cancel: storageCancel, file: file}
	if err := os.Remove(file.Name()); err != nil {
		_ = resource.close()
		_ = os.Remove(file.Name())
		return resource.failure("unlink", err)
	}
	w.resource = resource
	if scope != nil {
		w.stop, err = scope.attach(resource)
		if err != nil {
			return err
		}
	} else {
		w.stop = context.AfterFunc(storageContext, func() { _ = resource.close() })
	}
	if err := configureRequestBodySpool(file); err != nil {
		return resource.failure("configure", err)
	}
	if w.owned != nil {
		buffer := make([]byte, segmentedBodyBlockSize)
		for offset := int64(0); offset < w.length; {
			size := min(int64(len(buffer)), w.length-offset)
			n, err := w.owned.readAt(buffer[:size], offset)
			if err != nil {
				return err
			}
			if _, err := w.writeSpool(buffer[:n]); err != nil {
				return err
			}
			offset += int64(n)
		}
		if err := w.owned.close(); err != nil {
			return err
		}
		w.ownedStop()
		w.owned, w.ownedStop = nil, nil
	} else {
		for _, segment := range w.memory.segments {
			if _, err := w.writeSpool(segment); err != nil {
				return err
			}
		}
	}
	w.memory = nil
	return nil
}

func (w *requestBodyOutputWriter) finish() (RequestBody, error) {
	if w.finished {
		return RequestBody{}, io.ErrClosedPipe
	}
	if err := w.ctx.Err(); err != nil {
		return RequestBody{}, err
	}
	if w.owned != nil {
		if err := w.owned.available(); err != nil {
			return RequestBody{}, err
		}
		content := newRequestBodySpoolContent(w.owned, w.length, w.ownedStop)
		w.owned, w.ownedStop = nil, nil
		w.finished = true
		return RequestBody{state: &requestBodyState{mode: RequestBodyReplayable, spooled: content, length: w.length, bodyDigest: hex.EncodeToString(w.digest.Sum(nil))}}, nil
	}
	if w.resource == nil {
		w.finished = true
		body := w.memory.finish()
		body.state.bodyDigest = hex.EncodeToString(w.digest.Sum(nil))
		return body, nil
	}
	finishTiming := startRequestBodyTiming(w.ctx, "output_finish")
	err := finishRequestBodySpool(w.resource.file)
	finishTiming()
	if err != nil {
		return RequestBody{}, w.resource.failure("sync", err)
	}
	if err := w.ctx.Err(); err != nil {
		return RequestBody{}, err
	}
	content := newRequestBodySpoolContent(w.resource, w.length, w.stop)
	w.resource = nil
	w.stop = nil
	w.finished = true
	return RequestBody{state: &requestBodyState{
		mode: RequestBodyReplayable, spooled: content, length: w.length, bodyDigest: hex.EncodeToString(w.digest.Sum(nil)),
	}}, nil
}

func (w *requestBodyOutputWriter) abort() {
	if w.ownedStop != nil {
		w.ownedStop()
		w.ownedStop = nil
	}
	if w.owned != nil {
		_ = w.owned.close()
		w.owned = nil
	}
	if w.stop != nil {
		w.stop()
		w.stop = nil
	}
	if w.resource != nil {
		_ = w.resource.close()
		w.resource = nil
	}
	w.memory = nil
}
