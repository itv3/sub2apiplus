package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 采用当前部署的预算参数验证真实准入入口，而非只验证权重计算函数。
// 18 MiB 单请求权重为 134.98 MiB，256 MiB 预算只能同时放行一个；
// 被拒请求的正文由生成器提供，若入口先读后拒绝，会被读取计数立即发现。
func TestResponsesRequestMemoryAdmission18MiBConcurrentCancellation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const bodyBytes int64 = 18 << 20
	for _, concurrency := range []int{3, 10} {
		t.Run(fmt.Sprintf("%d路", concurrency), func(t *testing.T) {
			cfg := &config.Config{}
			cfg.Gateway.MaxBodySize = 64 << 20
			cfg.Gateway.RequestMemoryBudgetBytes = 256 << 20
			cfg.Gateway.RequestMemoryMaxRequestBytes = 37 << 20
			cfg.Gateway.RequestMemoryAmplification = 6.11
			cfg.Gateway.RequestMemoryFixedBytes = 25 << 20
			h := &OpenAIGatewayHandler{cfg: cfg}
			admission := h.responsesRequestMemoryAdmission()
			weight := int64(math.Ceil(float64(bodyBytes)*6.11 + float64(25<<20)))
			require.Greater(t, weight*2, cfg.Gateway.RequestMemoryBudgetBytes)

			runWave := func(count int) {
				ownedBefore := pkghttputil.ActiveOwnedRequestBodyBytes()
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				type outcome struct {
					index    int
					admitted bool
					err      error
				}
				ready := make(chan outcome, count)
				finished := make(chan int, count)
				readers := make([]*requestMemoryCountingReader, count)
				recorders := make([]*httptest.ResponseRecorder, count)
				start := make(chan struct{})
				for index := range count {
					readers[index] = newRequestMemoryJSONReader(bodyBytes)
					recorders[index] = httptest.NewRecorder()
					go func(index int) {
						defer func() { finished <- index }()
						<-start
						c, _ := gin.CreateTestContext(recorders[index])
						c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", readers[index]).WithContext(ctx)
						c.Request.ContentLength = bodyBytes
						c.Request.Header.Set("Content-Type", "application/json")
						release, acquired := h.acquireResponsesRequestMemory(c, nil)
						if !acquired {
							ready <- outcome{index: index}
							return
						}
						defer release()
						body, owner, err := readOwnedAdmittedResponsesJSONRequestBody(c.Request, cfg, nil)
						defer func() {
							if closeErr := owner.Close(); closeErr != nil {
								t.Error(closeErr)
							}
						}()
						if err == nil && (int64(len(body)) != bodyBytes || !json.Valid(body)) {
							err = fmt.Errorf("正文长度或 JSON 校验失败：收到 %d 字节", len(body))
						}
						ready <- outcome{index: index, admitted: true, err: err}
						// 持有正文直至取消，保证其他并发请求面对的是实际已占用的预算。
						<-ctx.Done()
						runtime.KeepAlive(body)
						c.Status(http.StatusNoContent)
						c.Writer.WriteHeaderNow()
					}(index)
				}
				close(start)
				outcomes := make([]outcome, count)
				accepted := 0
				for range count {
					select {
					case result := <-ready:
						require.NoError(t, result.err)
						outcomes[result.index] = result
						if result.admitted {
							accepted++
						}
					case <-time.After(10 * time.Second):
						t.Fatal("等待并发准入结果超时")
					}
				}
				require.Equal(t, 1, accepted)
				require.Equal(t, weight, admission.usedBytes.Load())
				require.LessOrEqual(t, admission.usedBytes.Load(), admission.capacityBytes)
				cancel()
				for range count {
					select {
					case <-finished:
					case <-time.After(5 * time.Second):
						t.Fatal("取消后请求未退出")
					}
				}
				require.Zero(t, admission.usedBytes.Load(), "取消后必须同步归还全部准入预算")
				require.Equal(t, ownedBefore, pkghttputil.ActiveOwnedRequestBodyBytes(), "额度归还时大正文系统内存也必须已经释放")
				for index, result := range outcomes {
					if result.admitted {
						require.Equal(t, bodyBytes, readers[index].readBytes.Load())
						require.Equal(t, http.StatusNoContent, recorders[index].Code)
					} else {
						require.Zero(t, readers[index].readBytes.Load(), "预算拒绝不得读取正文")
						require.Equal(t, http.StatusServiceUnavailable, recorders[index].Code)
						require.Equal(t, "2", recorders[index].Header().Get("Retry-After"))
						require.Contains(t, recorders[index].Body.String(), "Request memory budget is temporarily exhausted")
					}
				}
			}
			runWave(concurrency)
			// 同一个准入器恢复放行，避免仅观察归零、遗漏不可复用的取消状态。
			runWave(1)
			used, capacity, accepted, rejected, tooLarge := admission.stats()
			require.Zero(t, used)
			require.Equal(t, cfg.Gateway.RequestMemoryBudgetBytes, capacity)
			require.EqualValues(t, 2, accepted)
			require.EqualValues(t, concurrency-1, rejected)
			require.Zero(t, tooLarge)
		})
	}
}

type requestMemoryCountingReader struct {
	reader    io.Reader
	readBytes atomic.Int64
}

func (r *requestMemoryCountingReader) Read(p []byte) (int, error) {
	n, err := r.reader.Read(p)
	r.readBytes.Add(int64(n))
	return n, err
}

func newRequestMemoryJSONReader(size int64) *requestMemoryCountingReader {
	const prefix, suffix = `{"input":"`, `"}`
	return &requestMemoryCountingReader{reader: io.MultiReader(
		strings.NewReader(prefix),
		io.LimitReader(requestMemoryRepeatedReader{}, size-int64(len(prefix)+len(suffix))),
		strings.NewReader(suffix),
	)}
}

type requestMemoryRepeatedReader struct{}

func (requestMemoryRepeatedReader) Read(p []byte) (int, error) {
	for index := range p {
		p[index] = 'a'
	}
	return len(p), nil
}
