package httputil

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
)

// 未知长度正文先保留至多 1MiB；超过阈值后在非内存文件系统暂存，得到长度后
// 只分配一次最终正文。暂存同时限制页缓存，避免把第二份正文从堆移到容器页缓存。
const (
	admittedBodySpoolThreshold = 1 << 20
	admittedBodyReadChunk      = 32 << 10
	admittedBodyCacheBatch     = 1 << 20
)

// RequestBodyStorageError 表示本机暂存或正文内存管理失败，不能作为客户端 JSON 错误返回。
type RequestBodyStorageError struct{ Err error }

func (e *RequestBodyStorageError) Error() string {
	return fmt.Sprintf("request body storage: %v", e.Err)
}
func (e *RequestBodyStorageError) Unwrap() error { return e.Err }

type requestBodyContextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r requestBodyContextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(p)
}

func readAdmittedUnknownBody(ctx context.Context, reader io.Reader) ([]byte, error) {
	return ReadAdmittedBodyStream(ctx, reader, 0, AdmittedBodyReadHooks{})
}

// AdmittedBodyReadHooks 允许 HTTP/WS 共用暂存过程，不把入口的额度实现带入读取包。
type AdmittedBodyReadHooks struct {
	// Reserve 在每次读取前及最终正文分配前调用。readingBytes 包含累计正文及
	// 当前块容量，joiningBytes 是即将分配的结果；磁盘内容也保守计入累计额度。
	Reserve func(readingBytes, joiningBytes int64) error
	// ReadDone 在完整消费输入并解除 reader 引用后、结果分配前调用。
	// 压缩入口在这里关闭解码器、移除其他引用并检查剩余 wire 字节。
	ReadDone func() error
}

// ReadAdmittedBodyStream 分块准入未知长度正文，在普通磁盘暂存大正文并一次生成结果。
// maxBytes 限制 reader 提供的实际字节；零表示由调用方的受限 reader 负责限额。
func ReadAdmittedBodyStream(ctx context.Context, reader io.Reader, maxBytes int64, hooks AdmittedBodyReadHooks) ([]byte, error) {
	return readAdmittedBodyStream(ctx, reader, maxBytes, hooks, newRequestBodySpool)
}

type requestBodySpool struct {
	file       *os.File
	written    int64
	cacheStart int64
}

func newRequestBodySpool() (*requestBodySpool, error) {
	directory := os.TempDir()
	if !requestBodySpoolAllowed(directory) {
		// tmpfs 不会降低容器内存，未能确认文件系统类型时保留有界分块读取。
		return nil, nil
	}
	file, err := os.CreateTemp(directory, "sub2api-request-*")
	if err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	// 先解除目录链接，失败时也关闭并尝试清理；崩溃与取消不会留下正文文件。
	if err := os.Remove(file.Name()); err != nil {
		_ = file.Close()
		_ = os.Remove(file.Name())
		return nil, &RequestBodyStorageError{Err: err}
	}
	if err := requestBodySpoolPrepare(file); err != nil {
		_ = file.Close()
		return nil, &RequestBodyStorageError{Err: err}
	}
	return &requestBodySpool{file: file}, nil
}

func (s *requestBodySpool) write(p []byte) error {
	n, err := s.file.Write(p)
	s.written += int64(n)
	if err == nil && n != len(p) {
		err = io.ErrShortWrite
	}
	if err != nil {
		return &RequestBodyStorageError{Err: err}
	}
	if s.written-s.cacheStart >= admittedBodyCacheBatch {
		return s.flushCache()
	}
	return nil
}

func (s *requestBodySpool) flushCache() error {
	if s.written == s.cacheStart {
		return nil
	}
	if err := requestBodySpoolFlush(s.file, s.cacheStart, s.written-s.cacheStart); err != nil {
		return &RequestBodyStorageError{Err: err}
	}
	s.cacheStart = s.written
	return nil
}

func readAdmittedBodyStream(ctx context.Context, reader io.Reader, maxBytes int64, hooks AdmittedBodyReadHooks, createSpool func() (*requestBodySpool, error)) ([]byte, error) {
	return readAdmittedBodyStreamAllocated(ctx, reader, maxBytes, hooks, createSpool, allocateRequestBodyHeap)
}

func readAdmittedBodyStreamAllocated(ctx context.Context, reader io.Reader, maxBytes int64, hooks AdmittedBodyReadHooks, createSpool func() (*requestBodySpool, error), allocate requestBodyAllocator) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var chunks [][]byte
	var scratch []byte
	var spool *requestBodySpool
	var allocated, total int64
	spoolChecked := false
	defer func() {
		if spool != nil {
			_ = spool.file.Close()
		}
	}()
	for {
		capacity := int64(admittedBodyReadChunk)
		if maxBytes > 0 {
			capacity = min(capacity, maxBytes-total+1)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if hooks.Reserve != nil {
			if err := hooks.Reserve(max(allocated, total)+capacity, 0); err != nil {
				return nil, err
			}
		}
		var chunk []byte
		if spool == nil {
			chunk = make([]byte, int(capacity))
			allocated += capacity
		} else {
			chunk = scratch[:int(capacity)]
		}
		n, err := readAdmittedBodyChunk(ctx, reader, chunk)
		total += int64(n)
		if cancelErr := ctx.Err(); cancelErr != nil {
			return nil, cancelErr
		}
		if maxBytes > 0 && total > maxBytes {
			return nil, &http.MaxBytesError{Limit: maxBytes}
		}
		if err != nil && err != io.EOF {
			return nil, err
		}
		if n > 0 {
			if spool != nil {
				if writeErr := spool.write(chunk[:n]); writeErr != nil {
					return nil, writeErr
				}
			} else {
				chunks = append(chunks, chunk[:n])
				if total > admittedBodySpoolThreshold && !spoolChecked {
					spoolChecked = true
					var spoolErr error
					spool, spoolErr = createSpool()
					if spoolErr != nil {
						return nil, spoolErr
					}
					if spool != nil {
						for _, part := range chunks {
							if writeErr := spool.write(part); writeErr != nil {
								return nil, writeErr
							}
						}
						scratch = chunks[0][:cap(chunks[0])]
						chunks = nil
						allocated = int64(cap(scratch))
					}
				}
			}
		}
		if err == io.EOF {
			break
		}
	}
	// 解码器可能通过 reader 保活整个历史窗口，必须在完成回调及输出分配前解除引用。
	reader = nil
	scratch = nil
	if hooks.ReadDone != nil {
		if err := hooks.ReadDone(); err != nil {
			return nil, err
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if spool != nil {
		if err := spool.flushCache(); err != nil {
			return nil, err
		}
		if _, err := spool.file.Seek(0, io.SeekStart); err != nil {
			return nil, &RequestBodyStorageError{Err: err}
		}
	}
	if hooks.Reserve != nil {
		if err := hooks.Reserve(max(allocated, total), total); err != nil {
			return nil, err
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	body, err := allocateRequestBodySize(total, allocate)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if spool == nil {
		offset := 0
		for _, chunk := range chunks {
			offset += copy(body[offset:], chunk)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return body, nil
	}
	for offset := 0; offset < len(body); {
		end := min(offset+admittedBodyCacheBatch, len(body))
		if _, err := io.ReadFull(requestBodyContextReader{ctx: ctx, reader: spool.file}, body[offset:end]); err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return nil, &RequestBodyStorageError{Err: err}
		}
		if err := requestBodySpoolDiscardRead(spool.file, int64(offset), int64(end-offset)); err != nil {
			return nil, &RequestBodyStorageError{Err: err}
		}
		offset = end
	}
	if err := spool.file.Close(); err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	return body, nil
}

// 不把解码器的 UnexpectedEOF 当成正常终止，也不允许空读无限循环。
func readAdmittedBodyChunk(ctx context.Context, reader io.Reader, body []byte) (int, error) {
	used, emptyReads := 0, 0
	for used < len(body) {
		if err := ctx.Err(); err != nil {
			return used, err
		}
		n, err := reader.Read(body[used:])
		used += n
		if err != nil {
			return used, err
		}
		if n == 0 {
			emptyReads++
			if emptyReads >= 100 {
				return used, io.ErrNoProgress
			}
		} else {
			emptyReads = 0
		}
	}
	return used, nil
}
