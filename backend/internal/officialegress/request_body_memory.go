package officialegress

import (
	"errors"
	"io"
	"sync"
)

// RequestBodyMemoryBuffer 不借出底层切片，使取消释放与正在进行的复制可由实现串行化。
// 分配器由组合层注入；officialegress 不反向依赖入站 HTTP 工具包。
type RequestBodyMemoryBuffer interface {
	io.ReaderAt
	io.WriterAt
	io.Closer
}

// RequestBodyMemoryAllocator 必须返回可显式释放并完整计入请求峰值的系统内存。
// 实现不得捕获正文或业务 context；不可用时返回错误，由输出 writer 转存普通文件。
type RequestBodyMemoryAllocator func(int) (RequestBodyMemoryBuffer, error)

// 每块完整占用 1MiB 匿名系统内存；额度按容量而非有效字节扣减。
// 进程总额度限制早响应上传及并发请求叠加，独立于线上请求准入参数。
const requestBodyMemoryBlockSize = 1 << 20
const requestBodyMemoryProcessLimit = 64 << 20

var processRequestBodyMemoryBudget = requestBodyMemoryBudget{limit: requestBodyMemoryProcessLimit}
var errRequestBodyMemoryBudget = errors.New("正文系统内存额度不足")

type requestBodyMemoryBudget struct {
	mu          sync.Mutex
	limit, used int64
}

func (b *requestBodyMemoryBudget) acquire(size int64) bool {
	if b == nil {
		return false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if size > b.limit-b.used {
		return false
	}
	b.used += size
	return true
}

func (b *requestBodyMemoryBudget) release(size int64) {
	b.mu.Lock()
	b.used -= size
	b.mu.Unlock()
}

// requestBodyMemory 保存不可扩容的系统内存块。外层资源锁保护写入、读取与关闭，
// 不借出任何映射切片。重放只复制当前读取片段，不物化整份正文。
type requestBodyMemory struct {
	blocks   []RequestBodyMemoryBuffer
	budget   *requestBodyMemoryBudget
	allocate RequestBodyMemoryAllocator
	length   int64
}

func (m *requestBodyMemory) write(p []byte) (int, error) {
	written := 0
	for len(p) > 0 {
		index, offset := int(m.length/requestBodyMemoryBlockSize), m.length%requestBodyMemoryBlockSize
		if index == len(m.blocks) {
			if !m.budget.acquire(requestBodyMemoryBlockSize) {
				return written, errRequestBodyMemoryBudget
			}
			if !processRequestBodyMemoryBudget.acquire(requestBodyMemoryBlockSize) {
				m.budget.release(requestBodyMemoryBlockSize)
				return written, errRequestBodyMemoryBudget
			}
			block, err := m.allocate(requestBodyMemoryBlockSize)
			if err != nil {
				m.budget.release(requestBodyMemoryBlockSize)
				processRequestBodyMemoryBudget.release(requestBodyMemoryBlockSize)
				return written, err
			}
			m.blocks = append(m.blocks, block)
		}
		size := min(len(p), requestBodyMemoryBlockSize-int(offset))
		n, err := m.blocks[index].WriteAt(p[:size], offset)
		written += n
		m.length += int64(n)
		if err != nil {
			return written, err
		}
		p = p[n:]
	}
	return written, nil
}

func (m *requestBodyMemory) readAt(p []byte, offset int64) (int, error) {
	written := 0
	for len(p) > 0 {
		if offset >= m.length {
			return written, io.EOF
		}
		index, start := int(offset/requestBodyMemoryBlockSize), offset%requestBodyMemoryBlockSize
		size := min(len(p), requestBodyMemoryBlockSize-int(start), int(m.length-offset))
		n, err := m.blocks[index].ReadAt(p[:size], start)
		written += n
		offset += int64(n)
		if err != nil {
			return written, err
		}
		p = p[n:]
	}
	return written, nil
}

func (m *requestBodyMemory) close() error {
	var result error
	for i, block := range m.blocks {
		if block == nil {
			continue
		}
		if err := block.Close(); err != nil {
			result = errors.Join(result, err)
			continue
		}
		m.blocks[i] = nil
		m.budget.release(requestBodyMemoryBlockSize)
		processRequestBodyMemoryBudget.release(requestBodyMemoryBlockSize)
	}
	return result
}
