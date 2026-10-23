//go:build integration

package repository

// 展示配置原子存储在真实 PostgreSQL 上的集成测试：验证 FOR UPDATE 行锁下的并发行为。
// 需要 Docker（由 integration_harness_test.go 起容器）；本机没有 Docker 时整个集成测试包会被跳过。

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

// 集成测试专用的分组 ID，避开真实数据；settings 表按字符串键存放，与 groups 表没有外键。
const codexVisibilityIntegrationGroupBase int64 = 9_100_000

func newCodexVisibilityIntegrationStore(t *testing.T, offset int64) (service.CodexModelVisibilityStore, int64) {
	t.Helper()
	groupID := codexVisibilityIntegrationGroupBase + offset
	store := NewCodexModelVisibilityStore(testEntClient(t))
	ctx := context.Background()
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, groupID))
	t.Cleanup(func() { _ = store.DeleteCodexModelVisibility(context.Background(), groupID) })
	return store, groupID
}

func TestCodexModelVisibilityStorePostgresCreateUpdateConflict(t *testing.T) {
	store, groupID := newCodexVisibilityIntegrationStore(t, 1)
	ctx := context.Background()

	first, err := store.CompareAndSwapCodexModelVisibility(ctx, groupID, 0, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
		"kimi-k3": {Enabled: true},
	}))
	require.NoError(t, err)
	require.EqualValues(t, 1, first.Revision)

	_, err = store.CompareAndSwapCodexModelVisibility(ctx, groupID, 0, codexVisibilityConfig(nil))
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityConflict, "行已存在时期望 0 即冲突")

	second, err := store.CompareAndSwapCodexModelVisibility(ctx, groupID, 1, codexVisibilityConfig(nil))
	require.NoError(t, err)
	require.EqualValues(t, 2, second.Revision)

	_, err = store.CompareAndSwapCodexModelVisibility(ctx, groupID, 1, codexVisibilityConfig(nil))
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityConflict)

	require.NoError(t, store.DeleteCodexModelVisibility(ctx, groupID))
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, groupID), "删除不存在的行视为成功")
}

func TestCodexModelVisibilityStorePostgresConcurrentFirstCreateHasOneWinner(t *testing.T) {
	store, groupID := newCodexVisibilityIntegrationStore(t, 2)
	ctx := context.Background()

	const workers = 8
	var wg sync.WaitGroup
	results := make([]error, workers)
	start := make(chan struct{})
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, results[i] = store.CompareAndSwapCodexModelVisibility(ctx, groupID, 0, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
				"kimi-k3": {Enabled: true},
			}))
		}(i)
	}
	close(start)
	wg.Wait()

	assertExactlyOneWinner(t, results)
}

func TestCodexModelVisibilityStorePostgresConcurrentUpdateHasOneWinner(t *testing.T) {
	store, groupID := newCodexVisibilityIntegrationStore(t, 3)
	ctx := context.Background()
	_, err := store.CompareAndSwapCodexModelVisibility(ctx, groupID, 0, codexVisibilityConfig(nil))
	require.NoError(t, err)

	const workers = 8
	var wg sync.WaitGroup
	results := make([]error, workers)
	start := make(chan struct{})
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, results[i] = store.CompareAndSwapCodexModelVisibility(ctx, groupID, 1, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
				"glm-5.3": {Enabled: true},
			}))
		}(i)
	}
	close(start)
	wg.Wait()

	assertExactlyOneWinner(t, results)
}

// assertExactlyOneWinner 要求并发写入中恰好一个成功，其余全部是展示配置冲突，不允许出现其它错误。
func assertExactlyOneWinner(t *testing.T, results []error) {
	t.Helper()
	winners := 0
	for _, err := range results {
		switch {
		case err == nil:
			winners++
		case errors.Is(err, service.ErrCodexModelVisibilityConflict):
		default:
			t.Fatalf("并发写入出现了冲突以外的错误：%v", err)
		}
	}
	require.Equal(t, 1, winners, "并发写入必须恰好一个成功")
}
