package officialegress

import (
	"io"
	"sync"
	"weak"

	"github.com/klauspost/compress/zstd"
)

// requestBodyZstdEncoderCache 最多保存一个空闲的等级 3 编码器弱引用。借用时在锁内
// 提升为强引用并清空槽，确保并发请求各自独占；其他等级保持逐次新建。
// 空闲编码器允许被 GC 回收，不抬高常驻存活基线，也不依赖可能失真的池容量计数。
// 历史窗口、块大小及并发度固定，状态占用不会随请求正文总长度增加。
type requestBodyZstdEncoderCache struct {
	mu   sync.Mutex
	idle weak.Pointer[zstd.Encoder]
}

var sharedRequestBodyZstdEncoderCache = newRequestBodyZstdEncoderCache()

func newRequestBodyZstdEncoderCache() *requestBodyZstdEncoderCache {
	return &requestBodyZstdEncoderCache{}
}

func (c *requestBodyZstdEncoderCache) take(level int) (*zstd.Encoder, error) {
	if c != nil && level == 3 {
		c.mu.Lock()
		encoder := c.idle.Value()
		c.idle = weak.Pointer[zstd.Encoder]{}
		c.mu.Unlock()
		if encoder != nil {
			return encoder, nil
		}
	}
	return zstd.NewWriter(nil,
		zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(level)),
		zstd.WithEncoderConcurrency(1),
		zstd.WithLowerEncoderMem(true),
		zstd.WithWindowSize(requestBodyZstdWindowSize),
	)
}

func (c *requestBodyZstdEncoderCache) put(level int, encoder *zstd.Encoder) {
	if c == nil || level != 3 {
		return
	}
	// Reset 让下一帧重新开始，并断开对旧分段输出 writer 的引用。不能只 Close 后
	// 缓存，否则编码器仍会持有上一请求的全部压缩结果，形成跨轮正文残留。
	encoder.Reset(io.Discard)
	c.mu.Lock()
	if c.idle.Value() == nil {
		c.idle = weak.Make(encoder)
	}
	c.mu.Unlock()
	// 空闲槽已有编码器时直接释放本次引用，不扩大缓存，也不等待另一个请求归还。
}
