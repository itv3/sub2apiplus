package httputil

import (
	"bytes"
	"errors"
	"io"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestRequestBodyMemoryBufferBoundariesAndReleaseFailure(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	buffer, err := NewRequestBodyMemoryBuffer(ownedRequestBodyMapThreshold)
	if errors.Is(err, ErrRequestBodyMemoryUnavailable) {
		t.Skip("本平台不支持匿名正文映射")
	}
	require.NoError(t, err)
	defer buffer.Close()
	want := []byte("边界正文")
	offset := int64(ownedRequestBodyMapThreshold - len(want))
	n, err := buffer.WriteAt(append(bytes.Clone(want), 'x'), offset)
	require.Equal(t, len(want), n)
	require.ErrorIs(t, err, io.ErrShortWrite)
	got := make([]byte, len(want)+1)
	n, err = buffer.ReadAt(got, offset)
	require.Equal(t, len(want), n)
	require.ErrorIs(t, err, io.EOF)
	require.Equal(t, want, got[:n])
	_, err = buffer.WriteAt(want, -1)
	require.Error(t, err)
	_, err = buffer.ReadAt(got, -1)
	require.Error(t, err)
	// 释放失败不能扣减计数；恢复系统调用后同一个缓冲仍可重试 Close。
	unmap := buffer.owner.storage.unmap
	buffer.owner.storage.unmap = func([]byte) error { return errors.New("模拟释放失败") }
	require.Error(t, buffer.Close())
	require.Equal(t, baseline+ownedRequestBodyMapThreshold, ActiveOwnedRequestBodyBytes())
	buffer.owner.storage.unmap = unmap
	require.NoError(t, buffer.Close())
	require.NoError(t, buffer.Close())
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
	_, err = buffer.ReadAt(got, 0)
	require.ErrorIs(t, err, ErrOwnedRequestBodyClosed)
	_, err = buffer.WriteAt(want, 0)
	require.ErrorIs(t, err, ErrOwnedRequestBodyClosed)
}

func TestRequestBodyMemoryBufferDoesNotDisguiseHeapAsMappedMemory(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	buffer, err := NewRequestBodyMemoryBuffer(32)
	require.Nil(t, buffer)
	require.ErrorIs(t, err, ErrRequestBodyMemoryUnavailable)
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
}
