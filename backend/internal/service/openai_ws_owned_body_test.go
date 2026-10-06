package service

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/stretchr/testify/require"
)

func newOpenAIWSOwnedMemoryForTest(t *testing.T) (*OpenAIWSRequestMemory, *atomic.Int64) {
	t.Helper()
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("此用例核对匿名映射的显式释放，当前平台保留 Go 堆实现")
	}
	used := &atomic.Int64{}
	memory := NewOpenAIWSRequestMemory(4<<20, 8, 2, func(weight int64) bool {
		used.Store(weight)
		return true
	})
	t.Cleanup(memory.Close)
	return memory, used
}

func TestOpenAIWSOwnedBodiesExplicitEnableAndDisable(t *testing.T) {
	memory, _ := newOpenAIWSOwnedMemoryForTest(t)
	input := bytes.Repeat([]byte("a"), (1<<20)+17)
	body, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	require.Equal(t, input, body)
	require.Empty(t, memory.ownedBuffers, "旧裸切片调用不能被隐式切换为映射生命周期")

	memory.EnableOwnedBodies()
	body, err = readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	require.Len(t, memory.ownedBuffers, 1)
	owner := memory.ownedBuffers[0].body
	require.True(t, owner.IsMapped())
	require.NoError(t, memory.FinishTurn("service", body[12:32:32]))
	memory.DisableOwnedBodies()
	require.True(t, owner.IsMapped(), "关闭后续映射读取不能释放已经借出的原文")

	next, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	require.Equal(t, input, next)
	require.Len(t, memory.ownedBuffers, 1, "禁用后的读取使用普通 Go 堆，但上一轮 replay 仍持有映射")
	require.Equal(t, input[12:32], body[12:32])
	require.NoError(t, memory.FinishTurn("service"))
	require.True(t, owner.IsMapped())
	require.NoError(t, memory.beginRead())
	require.False(t, owner.IsMapped())
}

func TestOpenAIWSOwnedBodiesRetainAllRootsUntilNextRead(t *testing.T) {
	memory, used := newOpenAIWSOwnedMemoryForTest(t)
	memory.EnableOwnedBodies()
	input := bytes.Repeat([]byte("r"), (1<<20)+17)
	body, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	require.Len(t, memory.ownedBuffers, 1)
	owner := memory.ownedBuffers[0].body
	aligned := int64((len(input) + os.Getpagesize() - 1) / os.Getpagesize() * os.Getpagesize())
	require.Equal(t, aligned, owner.MappedBytes())
	require.Equal(t, aligned*2+8, used.Load(), "加工权重必须包含映射的页对齐容量")

	require.NoError(t, memory.Retain("handler", body))
	require.NoError(t, memory.Retain("context", body[12:32:32]))
	require.NoError(t, memory.FinishTurn("service", body[64:80:80]))
	require.NoError(t, memory.Retain("handler"))
	require.NoError(t, memory.beginRead())
	require.Equal(t, aligned+8, used.Load())
	require.True(t, owner.IsMapped())
	require.Equal(t, input[64:80], body[64:80])

	require.NoError(t, memory.Retain("service"))
	require.NoError(t, memory.BeginAttempt([]byte("retry")))
	require.NoError(t, memory.beginRead())
	require.True(t, owner.IsMapped(), "换号和 replay 淘汰不能释放 context 仍持有的映射")
	require.Equal(t, input[12:32], body[12:32])

	require.NoError(t, memory.Retain("context"))
	require.True(t, owner.IsMapped(), "撤销最后引用后也必须等到下一次收帧边界")
	require.Equal(t, aligned+8, used.Load(), "尚未释放的映射不能提前返还预算")
	// 同步交接允许先撤销旧名称再登记新名称；中间不得 unmap，且新子片仍计整块映射。
	require.NoError(t, memory.Retain("retry", body[32:48:48]))
	require.NoError(t, memory.beginRead())
	require.True(t, owner.IsMapped())
	require.Equal(t, aligned+8, used.Load())
	require.NoError(t, memory.Retain("retry"))
	require.NoError(t, memory.beginRead())
	require.False(t, owner.IsMapped())
	require.Empty(t, memory.ownedBuffers)
	require.Zero(t, used.Load())
}

func TestOpenAIWSOwnedBodiesDiscardCommittedReadKeepsReplay(t *testing.T) {
	memory, used := newOpenAIWSOwnedMemoryForTest(t)
	memory.EnableOwnedBodies()
	input := bytes.Repeat([]byte("d"), (1<<20)+17)
	first, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	oldOwner := memory.ownedBuffers[0].body
	require.NoError(t, memory.FinishTurn("service", first[32:48:48]))

	_, owner, err := readOpenAIWSAdmittedFrameOwnerContext(t.Context(), bytes.NewReader(input), memory)
	require.NoError(t, err)
	require.True(t, owner.IsMapped())
	require.Len(t, memory.ownedBuffers, 2)
	// 模拟取消与成功结果同时就绪：读者已退出，但结果没有交付给业务消费者。
	require.NoError(t, memory.discardRead(owner))
	require.False(t, owner.IsMapped())
	require.True(t, oldOwner.IsMapped())
	require.Equal(t, input[32:48], first[32:48])
	require.Equal(t, oldOwner.MappedBytes()+8, used.Load())
	memory.Close()
	require.False(t, oldOwner.IsMapped())
	require.Zero(t, used.Load())
}

func TestOpenAIWSOwnedBodiesRejectedCommitClosesNewLease(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("此用例核对匿名映射的显式释放")
	}
	baseline := pkghttputil.ActiveOwnedRequestBodyBytes()
	var memory *OpenAIWSRequestMemory
	var used int64
	// 分配和回读已成功，但最终 owner 交接仍可能被共享准入器拒绝。
	memory = NewOpenAIWSRequestMemory(4<<20, 8, 2, func(weight int64) bool {
		if len(memory.ownedBuffers) > 0 {
			return false
		}
		used = weight
		return true
	})
	t.Cleanup(memory.Close)
	memory.EnableOwnedBodies()
	body, err := readOpenAIWSAdmittedFrame(bytes.NewReader(bytes.Repeat([]byte("x"), (1<<20)+17)), memory)
	var memoryErr *OpenAIWSRequestMemoryError
	require.ErrorAs(t, err, &memoryErr)
	require.Nil(t, body)
	require.Empty(t, memory.ownedBuffers)
	require.Equal(t, baseline, pkghttputil.ActiveOwnedRequestBodyBytes())
	require.Zero(t, used)
}

func TestOpenAIWSOwnedBodiesReadFailureAndCancellationKeepOnlyReplay(t *testing.T) {
	for _, cancel := range []bool{false, true} {
		t.Run(map[bool]string{false: "读取失败", true: "取消读取"}[cancel], func(t *testing.T) {
			memory, used := newOpenAIWSOwnedMemoryForTest(t)
			memory.EnableOwnedBodies()
			input := bytes.Repeat([]byte("f"), (1<<20)+17)
			first, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
			require.NoError(t, err)
			owner := memory.ownedBuffers[0].body
			require.NoError(t, memory.FinishTurn("service", first[8:16:16]))
			var reader io.Reader
			ctx, cancelRead := context.WithCancel(t.Context())
			defer cancelRead()
			wantErr := errors.New("本次读取失败")
			if cancel {
				reader = &wsSpoolCancellingReader{reader: bytes.NewReader(make([]byte, 2<<20)), cancel: cancelRead}
				wantErr = context.Canceled
			} else {
				reader = io.MultiReader(bytes.NewReader(input), &memoryFailingReader{err: wantErr})
			}
			got, err := readOpenAIWSAdmittedFrameContext(ctx, reader, memory)
			require.ErrorIs(t, err, wantErr)
			require.Nil(t, got)
			require.Len(t, memory.ownedBuffers, 1)
			require.True(t, owner.IsMapped())
			require.Equal(t, input[8:16], first[8:16])
			require.Equal(t, owner.MappedBytes()+8, used.Load())
		})
	}
}

type openAIWSOwnedBlockingReader struct {
	started chan struct{}
	release chan struct{}
}

func (r *openAIWSOwnedBlockingReader) Read([]byte) (int, error) {
	close(r.started)
	<-r.release
	return 0, context.Canceled
}

func TestOpenAIWSOwnedBodiesCloseJoinsReaderBeforeReleasingReplay(t *testing.T) {
	memory, used := newOpenAIWSOwnedMemoryForTest(t)
	memory.EnableOwnedBodies()
	input := bytes.Repeat([]byte("j"), (1<<20)+17)
	body, err := readOpenAIWSAdmittedFrame(bytes.NewReader(input), memory)
	require.NoError(t, err)
	owner := memory.ownedBuffers[0].body
	require.NoError(t, memory.FinishTurn("service", body[8:16:16]))
	reader := &openAIWSOwnedBlockingReader{started: make(chan struct{}), release: make(chan struct{})}
	t.Cleanup(func() {
		select {
		case <-reader.release:
		default:
			close(reader.release)
		}
	})
	readDone := make(chan error, 1)
	go func() {
		_, readErr := readOpenAIWSAdmittedFrameContext(t.Context(), reader, memory)
		readDone <- readErr
	}()
	<-reader.started
	closeDone := make(chan struct{})
	go func() { memory.Close(); close(closeDone) }()
	require.Eventually(t, func() bool {
		memory.mu.Lock()
		defer memory.mu.Unlock()
		return memory.closed
	}, time.Second, time.Millisecond)
	select {
	case <-closeDone:
		t.Fatal("读协程仍在访问正文时 Close 提前完成")
	default:
	}
	require.True(t, owner.IsMapped())
	require.Equal(t, input[8:16], body[8:16])
	close(reader.release)
	require.ErrorIs(t, <-readDone, context.Canceled)
	select {
	case <-closeDone:
	case <-time.After(time.Second):
		t.Fatal("读协程退出后 Close 没有结束")
	}
	require.False(t, owner.IsMapped())
	require.Zero(t, used.Load())
	require.Error(t, memory.startReader(), "关闭后不能再登记读者")
}
