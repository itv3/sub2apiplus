package httputil

import (
	"context"
	"fmt"
	"io"
	"os"
)

// 未知长度正文先保留至多 1MiB；超过阈值后在非内存文件系统暂存，得到长度后
// 只分配一次最终正文。文件在创建后立即解除目录链接，关闭时释放磁盘及页缓存。
const admittedBodySpoolThreshold = 1 << 20

// RequestBodyStorageError 表示本机暂存失败，不能把磁盘错误作为客户端 JSON 错误返回。
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

type requestBodySpoolWriter struct{ writer io.Writer }

func (w requestBodySpoolWriter) Write(p []byte) (int, error) {
	n, err := w.writer.Write(p)
	if err != nil {
		err = &RequestBodyStorageError{Err: err}
	}
	return n, err
}

func readAdmittedUnknownBody(ctx context.Context, reader io.Reader) ([]byte, error) {
	reader = requestBodyContextReader{ctx: ctx, reader: reader}
	directory := os.TempDir()
	if !requestBodySpoolAllowed(directory) {
		// tmpfs 不会降低容器内存，未能确认文件系统类型时也保留既有有界读取。
		return readRequestBodyChunks(reader, requestBodyReadInitCap, -1)
	}
	prefix := make([]byte, admittedBodySpoolThreshold)
	used, emptyReads := 0, 0
	for used < len(prefix) {
		n, err := reader.Read(prefix[used:])
		used += n
		if err != nil {
			if err == io.EOF {
				return prefix[:used:used], nil
			}
			return nil, err
		}
		if n == 0 {
			emptyReads++
			if emptyReads >= 100 {
				return nil, io.ErrNoProgress
			}
		} else {
			emptyReads = 0
		}
	}
	var next [1]byte
	n, err := io.ReadFull(reader, next[:])
	if err == io.EOF {
		return prefix, nil
	}
	if err != nil {
		return nil, err
	}
	file, err := os.CreateTemp(directory, "sub2api-request-*")
	if err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	defer file.Close()
	defer os.Remove(file.Name())
	// 此路径仅在支持解除打开文件链接的平台启用；崩溃、取消、写入失败均不留下正文文件。
	if err := os.Remove(file.Name()); err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	writer := requestBodySpoolWriter{writer: file}
	if _, err := writer.Write(prefix); err != nil {
		return nil, err
	}
	if _, err := writer.Write(next[:n]); err != nil {
		return nil, err
	}
	written, err := io.CopyBuffer(writer, reader, prefix[:32<<10])
	if err != nil {
		return nil, err
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	body := make([]byte, int64(used+n)+written)
	if _, err := io.ReadFull(requestBodyContextReader{ctx: ctx, reader: file}, body); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, &RequestBodyStorageError{Err: err}
	}
	if err := file.Close(); err != nil {
		return nil, &RequestBodyStorageError{Err: err}
	}
	return body, nil
}
