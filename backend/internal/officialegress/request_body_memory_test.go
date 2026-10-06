package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"path/filepath"
	"runtime"
	"sync"
	"testing"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/stretchr/testify/require"
)

func requestBodyMemoryTestContext(t *testing.T, limit int64) (context.Context, func()) {
	t.Helper()
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("本平台不支持匿名正文映射")
	}
	requestBodySpoolTestDirectory(t)
	ctx, release := WithRequestBodyStorageMemoryLimit(context.Background(), limit, func(size int) (RequestBodyMemoryBuffer, error) {
		return pkghttputil.NewRequestBodyMemoryBuffer(size)
	})
	t.Cleanup(release)
	return ctx, release
}

func TestRequestBodyMemoryReplayLeasesAndAccounting(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, 4*requestBodyMemoryBlockSize)
	want := bytes.Repeat([]byte("正文重放0123"), 100000)
	body := newRequestBodySpoolTestBody(t, ctx, want)
	resource := body.state.spooled.resource
	require.Nil(t, resource.file)
	require.NotNil(t, resource.memory)
	require.Equal(t, baseline+2*requestBodyMemoryBlockSize, pkghttputil.ActiveOwnedRequestBodyBytes())
	digest := sha256.Sum256(want)
	require.Equal(t, hex.EncodeToString(digest[:]), body.state.bodyDigest)
	request := newSegmentedTestRequest(t, body)
	copyBody, ok := body.ReplayableBytes()
	require.True(t, ok)
	require.Equal(t, want, copyBody)
	clear(copyBody)
	var group sync.WaitGroup
	for range 8 {
		group.Go(func() {
			reader, err := request.GetBody()
			if err != nil {
				t.Error(err)
				return
			}
			defer func() {
				if closeErr := reader.Close(); closeErr != nil {
					t.Error(closeErr)
				}
			}()
			hash := sha256.New()
			_, err = io.CopyBuffer(hash, reader, make([]byte, 7919))
			if err != nil || !bytes.Equal(hash.Sum(nil), digest[:]) {
				t.Errorf("重放内容或独立游标错误：%v", err)
			}
		})
	}
	group.Wait()
	// EOF 仍保留租约；scope 结束后的早响应上传可继续 GetBody，最后 Close 同步释放。
	_, err := io.Copy(io.Discard, request.Body)
	require.NoError(t, err)
	release()
	late, err := request.GetBody()
	require.NoError(t, err)
	require.NoError(t, request.Body.Close())
	require.False(t, resource.closed.Load())
	assertReaderBytes(t, late, want)
	require.True(t, resource.closed.Load())
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
	_, err = request.GetBody()
	require.ErrorIs(t, err, context.Canceled)
}

func TestRequestBodyMemoryBudgetSpillPreservesPrefixAndDigest(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, requestBodyMemoryBlockSize)
	want := bytes.Repeat([]byte("x"), 2*requestBodyMemoryBlockSize+37)
	writer := newRequestBodyOutputWriter(ctx)
	defer writer.abort()
	_, err := writer.Write(want[:requestBodyMemoryBlockSize-17])
	require.NoError(t, err)
	require.Equal(t, baseline+requestBodyMemoryBlockSize, pkghttputil.ActiveOwnedRequestBodyBytes())
	_, err = writer.Write(want[requestBodyMemoryBlockSize-17:])
	require.NoError(t, err)
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	body, err := writer.finish()
	require.NoError(t, err)
	require.NotNil(t, body.state.spooled.resource.file)
	require.Nil(t, body.state.spooled.resource.memory)
	digest := sha256.Sum256(want)
	require.Equal(t, hex.EncodeToString(digest[:]), body.state.bodyDigest)
	reader, _, err := body.openReplayable()
	require.NoError(t, err)
	assertReaderBytes(t, reader, want)
	release()
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
}

func TestRequestBodyMemoryProcessLimitFallsBackWithoutHeapEscape(t *testing.T) {
	ctx, release := requestBodyMemoryTestContext(t, 2*requestBodyMemoryBlockSize)
	defer release()
	require.True(t, processRequestBodyMemoryBudget.acquire(requestBodyMemoryProcessLimit))
	defer processRequestBodyMemoryBudget.release(requestBodyMemoryProcessLimit)
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("p"), requestBodyMemoryBlockSize+3))
	require.NotNil(t, body.state.spooled.resource.file)
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
}

func TestRequestBodyMemoryAllocationFailureReturnsQuotaAndUsesDisk(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, 2*requestBodyMemoryBlockSize)
	defer release()
	requestBodyStorageScopeFromContext(ctx).allocateMemory = func(int) (RequestBodyMemoryBuffer, error) {
		return nil, errors.New("模拟平台映射不可用")
	}
	want := bytes.Repeat([]byte("f"), requestBodyMemoryBlockSize+19)
	body := newRequestBodySpoolTestBody(t, ctx, want)
	require.NotNil(t, body.state.spooled.resource.file)
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
	reader, _, err := body.openReplayable()
	require.NoError(t, err)
	assertReaderBytes(t, reader, want)
}

func TestRequestBodyMemoryUnavailableStorageFailsWithoutUnboundedFallback(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, requestBodyMemoryBlockSize)
	defer release()
	// 无法确认的目录不能当普通磁盘，也不能在额度耗尽后退回无上限 Go 堆。
	t.Setenv("TMPDIR", filepath.Join(t.TempDir(), "不存在的暂存目录"))
	writer := newRequestBodyOutputWriter(ctx)
	_, err := writer.Write(bytes.Repeat([]byte("s"), requestBodyMemoryBlockSize+1))
	var storageError *RequestBodyStorageError
	require.ErrorAs(t, err, &storageError)
	writer.abort()
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
}

func TestRequestBodyMemoryCancelAndCloseDuringConcurrentReads(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, 4*requestBodyMemoryBlockSize)
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("c"), requestBodyMemoryBlockSize+3))
	var group sync.WaitGroup
	for range 8 {
		reader, _, err := body.openReplayable()
		require.NoError(t, err)
		group.Go(func() {
			defer func() {
				if closeErr := reader.Close(); closeErr != nil {
					t.Error(closeErr)
				}
			}()
			_, err := io.Copy(io.Discard, reader)
			if err != nil && !errors.Is(err, context.Canceled) {
				t.Error(err)
			}
		})
	}
	for range 4 {
		group.Go(func() { body.closeOwnedReplayableStorage(); release() })
	}
	group.Wait()
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
}

func TestRequestBodyMemoryAbortReleasesUnfinishedOutput(t *testing.T) {
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	ctx, release := requestBodyMemoryTestContext(t, 2*requestBodyMemoryBlockSize)
	defer release()
	writer := newRequestBodyOutputWriter(ctx)
	_, err := writer.Write([]byte("未完成的压缩输出"))
	require.NoError(t, err)
	require.Equal(t, baseline+requestBodyMemoryBlockSize, pkghttputil.ActiveOwnedRequestBodyBytes())
	writer.abort()
	writer.abort()
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, requestBodyStorageScopeFromContext(ctx).memoryBudget.used)
}

func TestRequestBodyMemoryGuardReadsActualBytesDespiteCachedCompilerDigest(t *testing.T) {
	ctx, release := requestBodyMemoryTestContext(t, 3*requestBodyMemoryBlockSize)
	defer release()
	want := bytes.Repeat([]byte("g"), requestBodyMemoryBlockSize+1)
	body := newRequestBodySpoolTestBody(t, ctx, want)
	request := newSegmentedTestRequest(t, body)
	defer func() {
		if closeErr := request.Body.Close(); closeErr != nil {
			t.Error(closeErr)
		}
	}()
	normalization := WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}
	digest, err := requestDigest(request, normalization, WireProtocolHTTP)
	require.NoError(t, err)
	metadata := attemptMetadata{Token: &FinalizationToken{payload: tokenPayload{
		RequestDigest: digest, Normalization: normalization,
	}}}
	guard := &Guard{}
	reasons, _, err := guard.finalizationWireReasons(request, metadata, WireProtocolHTTP)
	require.NoError(t, err)
	require.Empty(t, reasons)
	// 故意在包内破坏不可变约束：缓存摘要仍不变，Guard 必须读取实际字节并拒绝。
	_, err = body.state.spooled.resource.memory.blocks[0].WriteAt([]byte("X"), 0)
	require.NoError(t, err)
	reasons, _, _ = guard.finalizationWireReasons(request, metadata, WireProtocolHTTP)
	require.Contains(t, reasons, ReasonRequestModifiedAfterFinalize)
	require.Nil(t, body.state.spooled.bytes)
}
