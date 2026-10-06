package officialegress

import (
	"context"
	"sync"
)

type requestBodyStorageScopeContextKey struct{}

// requestBodyStorageScope 只保存取消通道、存储资源和额度，不保存入站 context 或业务工作区。
// 同一 Forward 内的重试共享它；正常结束撤销 scope 持有权，活动上传读者独立保留存储。
type requestBodyStorageScope struct {
	mu             sync.Mutex
	once           sync.Once
	sourceDone     <-chan struct{}
	closed         bool
	resources      map[*requestBodySpoolResource]struct{}
	memoryBudget   *requestBodyMemoryBudget
	allocateMemory RequestBodyMemoryAllocator
}

// WithRequestBodyStorageScope 为一次完整转发建立确定性的正文文件生命周期。返回的 ctx
// 保留原有业务值和取消行为，供调用链正常使用；文件只保存独立取消信号，绝不回指业务 ctx。
// release 必须在完整 HTTP Forward 或 WS bridge 单轮结束时调用，不能在收到响应头时调用。
// 正常结束后等待活动 Body/GetBody 租约关闭；取消退出仍立即关闭，不绕过上游取消策略。
func WithRequestBodyStorageScope(ctx context.Context) (context.Context, func()) {
	return WithRequestBodyStorageMemoryLimit(ctx, 0, nil)
}

// WithRequestBodyStorageMemoryLimit 为一次完整转发设置压缩输出的系统内存容量上限。
// 多个重试共享额度；正常结束后仍在上传的块继续占用进程额度，最后读者关闭才归还。
// 额度不足或映射不可用时自动转存普通文件，limit 为零时沿用原磁盘策略。
func WithRequestBodyStorageMemoryLimit(ctx context.Context, limit int64, allocate RequestBodyMemoryAllocator) (context.Context, func()) {
	if ctx == nil {
		ctx = context.Background()
	}
	if allocate == nil {
		limit = 0
	}
	scope := &requestBodyStorageScope{sourceDone: ctx.Done(), memoryBudget: &requestBodyMemoryBudget{limit: max(0, limit)}, allocateMemory: allocate}
	return context.WithValue(ctx, requestBodyStorageScopeContextKey{}, scope), scope.release
}

func (s *requestBodyStorageScope) attach(resource *requestBodySpoolResource) (func() bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		_ = resource.close()
		return nil, context.Canceled
	}
	if s.resources == nil {
		s.resources = make(map[*requestBodySpoolResource]struct{})
	}
	s.resources[resource] = struct{}{}
	return func() bool {
		s.mu.Lock()
		defer s.mu.Unlock()
		_, present := s.resources[resource]
		delete(s.resources, resource)
		return present
	}, nil
}

func (s *requestBodyStorageScope) release() {
	s.once.Do(func() {
		s.mu.Lock()
		s.closed = true
		resources := s.resources
		s.resources = nil
		s.mu.Unlock()
		canceled := false
		select {
		case <-s.sourceDone:
			canceled = true
		default:
		}
		// 没有活动读者的文件在此同步关闭；早响应后的上传由最后一个 reader.Close
		// 同步释放。取消与失败不采用延迟释放，仍阻止活动读者继续发送。
		for resource := range resources {
			if canceled {
				_ = resource.close()
			} else {
				_ = resource.releaseScope()
			}
		}
	})
}

func requestBodyStorageScopeFromContext(ctx context.Context) *requestBodyStorageScope {
	scope, _ := ctx.Value(requestBodyStorageScopeContextKey{}).(*requestBodyStorageScope)
	return scope
}
