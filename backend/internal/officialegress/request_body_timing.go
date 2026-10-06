package officialegress

import (
	"context"
	"time"
)

type requestBodyTimingContextKey struct{}

// WithRequestBodyTiming 为定向测量提供阶段耗时，不记录正文或业务字段。
// observer 可能并发执行；生产调用不设置观察器时不读时钟、不产生指标分配。
// 阶段可能包含子阶段，因此各项不能相加当作整场耗时。
func WithRequestBodyTiming(ctx context.Context, observer func(string, time.Duration)) context.Context {
	return context.WithValue(ctx, requestBodyTimingContextKey{}, observer)
}

func startRequestBodyTiming(ctx context.Context, stage string) func() {
	observer, _ := ctx.Value(requestBodyTimingContextKey{}).(func(string, time.Duration))
	if observer == nil {
		return func() {}
	}
	start := time.Now()
	return func() { observer(stage, time.Since(start)) }
}
