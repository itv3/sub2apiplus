package service

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// 模型清单缓存的合并刷新必须在领头者内部复核缓存：
// 调用方“先看缓存未命中、再进入 singleflight”之间没有锁，上一轮合并调用若恰好在这两步
// 之间完成，后来者不能再把自己当成新一轮领头者重复请求上游。

func newOpenAIModelsRefreshProbe() (*OpenAIGatewayService, openAIModelsRequest, *atomic.Int32, func(context.Context, string) (*OpenAIModelsResponse, error)) {
	var calls atomic.Int32
	fetch := func(context.Context, string) (*OpenAIModelsResponse, error) {
		calls.Add(1)
		return &OpenAIModelsResponse{Body: []byte(`{"object":"list","data":[{"id":"shared"}]}`)}, nil
	}
	return &OpenAIGatewayService{}, openAIModelsRequest{url: "https://models.example/v1/models", accountID: 7, standardModelsList: true}, &calls, fetch
}

// 确定性复现：模拟“后来者在缓存未命中时的判断已作废，领头者此刻才被调度”的时序，
// 即第二次直接进入合并刷新，而缓存里已有第一轮写入的新鲜结果。
func TestRefreshCachedOpenAIModelsReusesFreshEntryWrittenByPreviousFlight(t *testing.T) {
	s, request, calls, fetch := newOpenAIModelsRefreshProbe()
	key := buildOpenAIModelsCacheKey(request)

	first := <-s.refreshCachedOpenAIModels(key, request, fetch)
	require.NoError(t, first.Err)
	require.EqualValues(t, 1, calls.Load())

	late := <-s.refreshCachedOpenAIModels(key, request, fetch)
	require.NoError(t, late.Err)
	require.EqualValues(t, 1, calls.Load(), "缓存已新鲜时，迟到的领头者不得重复请求上游")
	lateResponse, ok := late.Val.(*OpenAIModelsResponse)
	require.True(t, ok)
	require.JSONEq(t, `{"object":"list","data":[{"id":"shared"}]}`, string(lateResponse.Body))
}

// 过期（stale）与过期失效的条目仍然必须刷新，复核不能把正常刷新也吞掉。
func TestRefreshCachedOpenAIModelsStillRefreshesStaleAndExpiredEntries(t *testing.T) {
	s, request, calls, fetch := newOpenAIModelsRefreshProbe()
	key := buildOpenAIModelsCacheKey(request)

	require.NoError(t, (<-s.refreshCachedOpenAIModels(key, request, fetch)).Err)
	require.EqualValues(t, 1, calls.Load())

	expireCodexModelsManifestCache(s, 2*time.Minute) // 已过新鲜期、仍在可用期内
	require.NoError(t, (<-s.refreshCachedOpenAIModels(key, request, fetch)).Err)
	require.EqualValues(t, 2, calls.Load(), "stale 条目必须刷新")

	expireCodexModelsManifestCache(s, 6*time.Minute) // 已超过可用期
	require.NoError(t, (<-s.refreshCachedOpenAIModels(key, request, fetch)).Err)
	require.EqualValues(t, 3, calls.Load(), "过期失效的条目必须刷新")
}

// 并发冷启动：每一轮都用全新的缓存，多个调用方同时冷读同一个 key，上游只能被请求一次。
// 旧实现下该用例是概率性失败（时间窗很窄），确定性复现见上面的第一个用例。
func TestFetchCachedOpenAIModelsColdConcurrentReadersHitUpstreamOnce(t *testing.T) {
	for round := 0; round < 300; round++ {
		s, request, calls, fetch := newOpenAIModelsRefreshProbe()
		var wait sync.WaitGroup
		start := make(chan struct{})
		errs := make([]error, 6)
		for i := range errs {
			wait.Add(1)
			go func() {
				defer wait.Done()
				<-start
				_, errs[i] = s.fetchCachedOpenAIModels(context.Background(), request, fetch, "")
			}()
		}
		close(start)
		wait.Wait()
		for _, err := range errs {
			require.NoError(t, err)
		}
		require.EqualValues(t, 1, calls.Load(), "第 %d 轮冷读重复请求了上游", round)
	}
}
