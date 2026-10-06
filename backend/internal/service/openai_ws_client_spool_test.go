package service

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

func TestOpenAIWSAdmittedFrameSpoolPreservesLimitAndBudget(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	body := bytes.Repeat([]byte("帧"), 1<<20)
	for _, test := range []struct {
		name     string
		limit    int64
		budget   int64
		tooLarge bool
	}{
		{name: "大帧完整读取", limit: 4 << 20, budget: 10 << 20},
		{name: "暂存期间额度不足", limit: 4 << 20, budget: 3 << 20},
		{name: "暂存后超出单帧上限", limit: 2 << 20, budget: 10 << 20, tooLarge: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var used int64
			memory := NewOpenAIWSRequestMemory(test.limit, 8, 2, func(weight int64) bool {
				if weight > test.budget {
					return false
				}
				used = weight
				return true
			})
			got, err := readOpenAIWSAdmittedFrame(bytes.NewReader(body), memory)
			if test.name == "大帧完整读取" {
				require.NoError(t, err)
				require.Equal(t, body, got)
				require.Equal(t, len(got), cap(got))
				require.EqualValues(t, len(body)*2+8, used)
			} else {
				var memoryErr *OpenAIWSRequestMemoryError
				require.ErrorAs(t, err, &memoryErr)
				require.Equal(t, test.tooLarge, memoryErr.TooLarge)
				require.Nil(t, got)
				require.Zero(t, used)
			}
			memory.Close()
			require.Zero(t, used)
			entries, err := os.ReadDir(directory)
			require.NoError(t, err)
			require.Empty(t, entries)
		})
	}
}

type wsSpoolCancellingReader struct {
	reader io.Reader
	cancel context.CancelFunc
	read   int
}

func (r *wsSpoolCancellingReader) Read(p []byte) (int, error) {
	n, err := r.reader.Read(p)
	r.read += n
	if r.read > (1<<20)+(2<<15) {
		r.cancel()
	}
	return n, err
}

func TestOpenAIWSAdmittedFrameSpoolCancellationReleasesReservation(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	var used int64
	memory := NewOpenAIWSRequestMemory(4<<20, 8, 2, func(weight int64) bool { used = weight; return true })
	reader := &wsSpoolCancellingReader{reader: bytes.NewReader(make([]byte, 3<<20)), cancel: cancel}
	got, err := readOpenAIWSAdmittedFrameContext(ctx, reader, memory)
	require.ErrorIs(t, err, context.Canceled)
	require.Nil(t, got)
	require.Zero(t, used)
	entries, err := os.ReadDir(directory)
	require.NoError(t, err)
	require.Empty(t, entries)
}

func TestReadOpenAIWSClientMessageStorageFailureCloses1013(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	require.NoError(t, os.Chmod(directory, 0500))
	t.Cleanup(func() { _ = os.Chmod(directory, 0700) })
	file, err := os.CreateTemp(directory, "permission-probe-*")
	if err == nil {
		_ = file.Close()
		_ = os.Remove(file.Name())
		t.Skip("当前用户可绕过目录写权限，无法构造实际暂存失败")
	}
	require.True(t, os.IsPermission(err))
	var used atomic.Int64
	result := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		conn, err := coderws.Accept(w, req, nil)
		if err != nil {
			result <- err
			return
		}
		defer conn.CloseNow()
		conn.SetReadLimit(4 << 20)
		memory := NewOpenAIWSRequestMemory(4<<20, 8, 2, func(weight int64) bool { used.Store(weight); return true })
		ctx := WithOpenAIWSRequestMemory(req.Context(), memory)
		_, _, err = ReadOpenAIWSClientMessage(ctx, conn, 5*time.Second, coderws.StatusNormalClosure, "测试读取超时")
		result <- err
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), nil)
	require.NoError(t, err)
	defer client.CloseNow()
	// 服务器可在上传尚未完成时拒绝；Write 与 Read 都可能先收到相同关闭帧。
	writeErr := client.Write(ctx, coderws.MessageText, make([]byte, 2<<20))
	_, _, readErr := client.Read(ctx)
	var closeErr coderws.CloseError
	if !errors.As(readErr, &closeErr) {
		require.ErrorAs(t, writeErr, &closeErr)
	}
	require.Equal(t, coderws.StatusTryAgainLater, closeErr.Code)
	require.NotContains(t, closeErr.Reason, directory)
	select {
	case err := <-result:
		var storageErr *pkghttputil.RequestBodyStorageError
		require.ErrorAs(t, err, &storageErr)
		require.Zero(t, used.Load())
	case <-ctx.Done():
		t.Fatal("磁盘失败后服务器没有退出读帧")
	}
}
