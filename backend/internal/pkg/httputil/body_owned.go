package httputil

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"sync/atomic"
)

const ownedRequestBodyMapThreshold = 1 << 20

// ErrOwnedRequestBodyClosed 表示租约已经关闭，不能再借用或增加持有者。
var ErrOwnedRequestBodyClosed = errors.New("owned request body is closed")

var activeOwnedRequestBodyBytes atomic.Int64

// ActiveOwnedRequestBodyBytes 返回所有未释放匿名映射的完整页对齐长度。
// 同一映射的多个租约只计一次，尚未触页的容量也完整计数；此数不包含 Go 堆正文。
func ActiveOwnedRequestBodyBytes() int64 { return activeOwnedRequestBodyBytes.Load() }

type ownedRequestBodyStorage struct {
	mu          sync.Mutex
	buffer      []byte
	mappedBytes int64
	references  int
	unmap       func([]byte) error
}

// OwnedRequestBody 是不可变正文的一个独立租约，不得按值复制。
// Bytes 与 RetainedBuffers 只在本租约 Close 前有效；派生的切片、字符串视图也受同一限制。
// 调用方必须协调实际读者结束后再 Close，不能将“context 已取消”等同于没有读者。
// Retain 为明确共享的消费者创建独立租约；最后一个租约同步释放映射，不使用 finalizer。
type OwnedRequestBody struct {
	mu      sync.Mutex
	storage *ownedRequestBodyStorage
	body    []byte
}

// Bytes 借用只读正文。调用方不得写入、扩容后覆盖，或在 Close 后继续使用该视图。
func (b *OwnedRequestBody) Bytes() []byte {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.body
}

// RetainedBuffers 返回实际持有的完整底层区间，供内存准入器按数组范围去重。
// 映射区间包含页对齐尾部，不能仅凭 Bytes 的 cap 估算系统内存。
func (b *OwnedRequestBody) RetainedBuffers() [][]byte {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.storage == nil || len(b.storage.buffer) == 0 {
		return nil
	}
	return [][]byte{b.storage.buffer}
}

// MappedBytes 返回本租约所持映射的页对齐长度；多个租约的值不能直接相加。
func (b *OwnedRequestBody) MappedBytes() int64 {
	if b == nil {
		return 0
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.storage == nil {
		return 0
	}
	return b.storage.mappedBytes
}

func (b *OwnedRequestBody) IsMapped() bool { return b.MappedBytes() > 0 }

// Retain 返回具有独立 Close 生命周期的租约，不复制正文，也不重复增加系统内存计数。
func (b *OwnedRequestBody) Retain() (*OwnedRequestBody, error) {
	if b == nil {
		return nil, ErrOwnedRequestBodyClosed
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.storage == nil {
		return nil, ErrOwnedRequestBodyClosed
	}
	b.storage.mu.Lock()
	b.storage.references++
	b.storage.mu.Unlock()
	return &OwnedRequestBody{storage: b.storage, body: b.body}, nil
}

// Close 幂等地结束当前租约。最后一个持有者同步 munmap 成功后才扣减计数。
// 系统释放失败时保留本租约，调用方可重试 Close；不会把尚未释放的内存登记为零。
func (b *OwnedRequestBody) Close() error {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	storage := b.storage
	if storage == nil {
		return nil
	}
	storage.mu.Lock()
	defer storage.mu.Unlock()
	if storage.references == 1 {
		if storage.mappedBytes > 0 {
			if err := storage.unmap(storage.buffer); err != nil {
				return &RequestBodyStorageError{Err: fmt.Errorf("release request body memory: %w", err)}
			}
			activeOwnedRequestBodyBytes.Add(-storage.mappedBytes)
		}
		storage.buffer = nil
	}
	storage.references--
	b.storage, b.body = nil, nil
	return nil
}

func newOwnedRequestBody(size int) (*OwnedRequestBody, error) {
	return newOwnedRequestBodyWithMapping(size, mapOwnedRequestBody, unmapOwnedRequestBody)
}

func newOwnedRequestBodyWithMapping(size int, mapBody func(int) ([]byte, bool, error), unmap func([]byte) error) (*OwnedRequestBody, error) {
	if size < 0 {
		return nil, &RequestBodyStorageError{Err: errors.New("request body allocation size is negative")}
	}
	buffer, mapped, err := mapBody(size)
	if err != nil {
		return nil, &RequestBodyStorageError{Err: fmt.Errorf("allocate request body memory: %w", err)}
	}
	storage := &ownedRequestBodyStorage{buffer: buffer, references: 1, unmap: unmap}
	if mapped {
		storage.mappedBytes = int64(len(buffer))
		activeOwnedRequestBodyBytes.Add(storage.mappedBytes)
	}
	return &OwnedRequestBody{storage: storage, body: buffer[:size:size]}, nil
}

type requestBodyAllocator func(int) ([]byte, error)

func allocateRequestBodyHeap(size int) ([]byte, error) { return make([]byte, size), nil }

func allocateRequestBodySize(size int64, allocate requestBodyAllocator) ([]byte, error) {
	if size < 0 || size > int64(int(^uint(0)>>1)) {
		return nil, &RequestBodyStorageError{Err: errors.New("request body allocation size overflows")}
	}
	return allocate(int(size))
}

// ReadOwnedAdmittedLenientJSONRequestBodyWithReservation 读取已准入的 HTTP JSON 正文。
// Linux/Darwin 上至少 1MiB 的最终正文使用匿名系统内存，调用方必须在全部读者结束后 Close。
// 小正文与其他平台使用 Go 堆；旧的裸切片读取 API 始终使用 Go 堆，不借出隐式生命周期的映射。
func ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(req *http.Request, maxNormalizedBytes int64, beforeNormalize func(int) error) (*OwnedRequestBody, error) {
	return readOwnedAdmittedLenientJSONRequestBody(req, maxNormalizedBytes, beforeNormalize, newOwnedRequestBody)
}

func readOwnedAdmittedLenientJSONRequestBody(req *http.Request, maxNormalizedBytes int64, beforeNormalize func(int) error, allocate func(int) (*OwnedRequestBody, error)) (*OwnedRequestBody, error) {
	owner, err := collectOwnedRequestBody(func(allocateBody requestBodyAllocator) ([]byte, error) {
		body, err := readAdmittedLenientJSONRequestBodyAllocated(req, maxNormalizedBytes, beforeNormalize, allocateBody)
		if err == nil && req != nil {
			err = req.Context().Err()
		}
		return body, err
	}, func(size int) (*OwnedRequestBody, error) {
		if req != nil {
			if err := req.Context().Err(); err != nil {
				return nil, err
			}
		}
		return allocate(size)
	})
	// 预读正文未触发规范化时在收口器中才复制；该复制也必须服从请求取消。
	if err == nil && req != nil {
		if cancelErr := req.Context().Err(); cancelErr != nil {
			return nil, errors.Join(cancelErr, owner.Close())
		}
	}
	return owner, err
}

// ReadOwnedAdmittedBodyStream 为 HTTP/WS 未知长度读取返回独立正文租约。
// 暂存、限额与取消语义和裸切片 API 相同；匿名映射完整计入 ActiveOwnedRequestBodyBytes。
func ReadOwnedAdmittedBodyStream(ctx context.Context, reader io.Reader, maxBytes int64, hooks AdmittedBodyReadHooks) (*OwnedRequestBody, error) {
	return readOwnedAdmittedBodyStream(ctx, reader, maxBytes, hooks, newRequestBodySpool, newOwnedRequestBody)
}

func readOwnedAdmittedBodyStream(ctx context.Context, reader io.Reader, maxBytes int64, hooks AdmittedBodyReadHooks, createSpool func() (*requestBodySpool, error), allocate func(int) (*OwnedRequestBody, error)) (*OwnedRequestBody, error) {
	return collectOwnedRequestBody(func(allocateBody requestBodyAllocator) ([]byte, error) {
		body, err := readAdmittedBodyStreamAllocated(ctx, reader, maxBytes, hooks, createSpool, allocateBody)
		if err == nil {
			err = ctx.Err()
		}
		return body, err
	}, allocate)
}

// collectOwnedRequestBody 把读取与规范化的分配收口为一个返回租约。中间版本在成功时
// 关闭，任何错误关闭所有已分配版本；预读正文属于调用方，未生成新版本时必须复制。
func collectOwnedRequestBody(read func(requestBodyAllocator) ([]byte, error), allocate func(int) (*OwnedRequestBody, error)) (*OwnedRequestBody, error) {
	var allocated []*OwnedRequestBody
	body, err := read(func(size int) ([]byte, error) {
		owner, err := allocate(size)
		if err != nil {
			return nil, err
		}
		allocated = append(allocated, owner)
		return owner.Bytes(), nil
	})
	if err == nil && len(allocated) == 0 {
		var owner *OwnedRequestBody
		owner, err = allocate(len(body))
		if err == nil {
			copy(owner.Bytes(), body)
			allocated = append(allocated, owner)
			body = owner.Bytes()
		}
	}
	var result *OwnedRequestBody
	if err == nil {
		result = allocated[len(allocated)-1]
		// 读取入口可能移除了 BOM；底层 storage 仍持有完整原始分配区间。
		result.body = body[:len(body):len(body)]
		allocated = allocated[:len(allocated)-1]
	}
	for _, owner := range allocated {
		if closeErr := owner.Close(); closeErr != nil {
			err = errors.Join(err, closeErr)
		}
	}
	if err != nil && result != nil {
		err = errors.Join(err, result.Close())
		result = nil
	}
	return result, err
}
