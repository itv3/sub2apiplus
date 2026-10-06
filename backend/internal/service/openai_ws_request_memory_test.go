package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

func TestOpenAIWSReplayWorkingBytesBoundsSerializedBody(t *testing.T) {
	tests := []struct {
		name    string
		payload []byte
		items   []json.RawMessage
	}{
		{
			name:    "HTML字符",
			payload: []byte(`{"type":"response.create","input":[]}`),
			items:   []json.RawMessage{json.RawMessage(`{"role":"user","content":"` + strings.Repeat("<&>", 128) + `"}`)},
		},
		{
			name:    "Unicode分隔符与已转义内容",
			payload: []byte(`{"type":"response.create","input":"替换旧值"}`),
			items:   []json.RawMessage{json.RawMessage(`{"content":"` + strings.Repeat("\u2028\u2029", 128) + `\u003c\u2028"}`)},
		},
		{
			name:    "缺少input字段",
			payload: []byte(`{"type":"response.create"}`),
			items:   []json.RawMessage{json.RawMessage(`"x"`)},
		},
		{
			name:    "原始空白与多元素",
			payload: []byte(`{"input":[]}`),
			items:   []json.RawMessage{json.RawMessage(` { "content": "<&>" } `), json.RawMessage(`"y"`)},
		},
		{
			name:    "空数组语义",
			payload: []byte(`{}`),
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			state := newOpenAIWSReplayInputState(tt.items, true)
			rebuilt, err := setOpenAIWSPayloadInputSequence(tt.payload, tt.items, true)
			require.NoError(t, err)
			require.GreaterOrEqual(t, openAIWSReplayWorkingBytes(tt.payload, state), int64(len(rebuilt)), "分配前估算必须覆盖实际重建结果")
			require.Zero(t, testing.AllocsPerRun(10, func() { _ = openAIWSReplayWorkingBytes(tt.payload, state) }), "估算不得提前生成另一份正文")
		})
	}
}

func TestOpenAIWSReplayWorkingBytesRejectsEscapedGrowth(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(64<<10, 8, 2, func(weight int64) bool {
		if weight > 1024 {
			return false
		}
		used = weight
		return true
	})
	payload := []byte(`{"type":"response.create","input":[]}`)
	require.NoError(t, memory.BeginAttempt(payload))
	previousWeight := used
	items := []json.RawMessage{json.RawMessage(`{"content":"` + strings.Repeat("<", 384) + `"}`)}
	state := newOpenAIWSReplayInputState(items, true)
	err := memory.EnsureWorkingBytes(openAIWSReplayWorkingBytes(payload, state))
	var memoryErr *OpenAIWSRequestMemoryError
	require.ErrorAs(t, err, &memoryErr)
	require.False(t, memoryErr.TooLarge, "这是共享预算拒绝，不改变入站帧上限语义")
	require.Equal(t, previousWeight, used, "拒绝重建后仍须持有原帧额度")
	memory.Close()
	require.Zero(t, used)
}

func TestOpenAIWSRequestMemoryTransfersSharedRootsAndRetiresOldTurns(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(1024, 8, 2, func(weight int64) bool { used = weight; return true })
	require.Zero(t, used, "空连接不能提前预留最大帧")
	first := make([]byte, 256)
	require.NoError(t, memory.commitRead(first))
	require.EqualValues(t, 520, used)
	require.NoError(t, memory.Retain("handler", first))
	require.EqualValues(t, 520, used, "首帧与 handler 引用必须去重")
	require.NoError(t, memory.FinishTurn("service", first[64:80:80], first[128:160]))
	require.NoError(t, memory.Retain("handler"))
	require.EqualValues(t, 264, used, "子切片仍保活完整源数组，包括不可见前缀")

	second := make([]byte, 64)
	require.NoError(t, memory.commitRead(second))
	require.EqualValues(t, 392, used, "新帧工作区必须与旧重放缓存同时计费")
	require.NoError(t, memory.FinishTurn("service", second[7:19:19]))
	require.EqualValues(t, 72, used, "替换快照必须淘汰上一轮的源数组")
	require.Len(t, memory.roots, 1)
	require.Len(t, memory.owners, 1)
	require.NoError(t, memory.Retain("service"))
	require.Zero(t, used)
	require.Empty(t, memory.roots)
	require.Empty(t, memory.owners)
	memory.Close()
	memory.Close()
	require.Zero(t, used)
	require.Error(t, memory.Retain("late", second), "关闭后不能重新获得预算")
}

func TestOpenAIWSRequestMemoryRejectedHandoffKeepsPreviousReservation(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(1024, 8, 2, func(weight int64) bool {
		if weight > 300 {
			return false
		}
		used = weight
		return true
	})
	first := make([]byte, 100)
	require.NoError(t, memory.commitRead(first))
	require.EqualValues(t, 208, used)
	err := memory.FinishTurn("service", make([]byte, 400))
	var memoryErr *OpenAIWSRequestMemoryError
	require.ErrorAs(t, err, &memoryErr)
	require.False(t, memoryErr.TooLarge)
	require.EqualValues(t, 208, used, "失败不能先归还旧预算")
	require.Contains(t, memory.owners, "frame")
	require.NotContains(t, memory.owners, "service")
	require.NoError(t, memory.FinishTurn("service", first))
	require.EqualValues(t, 108, used)
	memory.Close()
	require.Zero(t, used)
}

func TestOpenAIWSRequestMemoryReadGrowthReusesRetainedSnapshot(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(1<<20, 8, 2, func(weight int64) bool { used = weight; return true })
	owner := make([][]byte, 512)
	for i := range owner {
		owner[i] = make([]byte, 7)
	}
	require.NoError(t, memory.Retain("replay", owner...))
	require.NoError(t, memory.beginRead())
	version := memory.snapshotVersion
	retained := used
	for block := int64(1); block <= 8; block++ {
		require.NoError(t, memory.growRead(block*32768, 0))
		require.Equal(t, version, memory.snapshotVersion, "同一帧的增量读取不得重扫 owner")
		require.Equal(t, retained+block*32768*2, used)
	}
	require.NoError(t, memory.EnsureWorkingBytes(123))
	require.Equal(t, version, memory.snapshotVersion)
	memory.Close()
}

func TestOpenAIWSRequestMemoryAttemptHandoffKeepsConnectionContext(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(1024, 8, 2, func(weight int64) bool { used = weight; return true })
	first := make([]byte, 100)
	require.NoError(t, memory.BeginAttempt(first))
	require.NoError(t, memory.Retain("context", make([]byte, 64)))
	require.NoError(t, memory.FinishTurn("service", first))
	require.EqualValues(t, 172, used)
	require.NoError(t, memory.BeginAttempt(make([]byte, 20)))
	require.EqualValues(t, 112, used, "换号移除旧服务缓存，但 Gin 上下文里的工具状态仍须计费")
	require.NotContains(t, memory.owners, "service")
	require.Contains(t, memory.owners, "context")
	memory.Close()
	require.Zero(t, used)
}

type memoryCountingReader struct {
	reader io.Reader
	read   int
	before func()
}

func (r *memoryCountingReader) Read(p []byte) (int, error) {
	if r.before != nil {
		r.before()
	}
	n, err := r.reader.Read(p)
	r.read += n
	return n, err
}

func TestOpenAIWSAdmittedFrameRejectsBeforeReadingWholeMessage(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(128<<10, 8, 2, func(weight int64) bool {
		if weight > 100<<10 {
			return false
		}
		used = weight
		return true
	})
	reader := &memoryCountingReader{reader: bytes.NewReader(bytes.Repeat([]byte("x"), 80<<10))}
	reader.before = func() { require.Positive(t, used, "必须先占预算再读入字节") }
	_, err := readOpenAIWSAdmittedFrame(reader, memory)
	var memoryErr *OpenAIWSRequestMemoryError
	require.ErrorAs(t, err, &memoryErr)
	require.False(t, memoryErr.TooLarge)
	require.Equal(t, 32<<10, reader.read, "第二块分配前应拒绝，不能收到完整大帧后才准入")
	require.Zero(t, used, "失败的读帧临时分配必须释放")
	require.Empty(t, memory.owners)
}

func TestOpenAIWSAdmittedFramePreservesBytesAndChecksHardLimit(t *testing.T) {
	for _, size := range []int{0, 1, 32767, 32768, 32769, 65536} {
		var used int64
		memory := NewOpenAIWSRequestMemory(65536, 8, 2, func(weight int64) bool { used = weight; return true })
		want := bytes.Repeat([]byte("p"), size)
		got, err := readOpenAIWSAdmittedFrame(bytes.NewReader(want), memory)
		require.NoError(t, err)
		require.Equal(t, want, got)
		if size > 0 {
			require.EqualValues(t, size*2+8, used)
		}
		memory.Close()
		require.Zero(t, used)
	}
	var used int64
	memory := NewOpenAIWSRequestMemory(16, 8, 2, func(weight int64) bool { used = weight; return true })
	_, err := readOpenAIWSAdmittedFrame(bytes.NewReader(bytes.Repeat([]byte("x"), 17)), memory)
	var memoryErr *OpenAIWSRequestMemoryError
	require.ErrorAs(t, err, &memoryErr)
	require.True(t, memoryErr.TooLarge)
	require.Zero(t, used)
}

func TestReadOpenAIWSClientMessageMemoryFailureSendsStableClose(t *testing.T) {
	for _, test := range []struct {
		name      string
		maxBytes  int64
		readLimit int64
		budget    int64
		bodyBytes int
		status    coderws.StatusCode
		tooLarge  bool
	}{
		{"预算不足", 128 << 10, 1 << 20, 100 << 10, 80 << 10, coderws.StatusTryAgainLater, false},
		{"解压后超限", 16 << 10, 1 << 20, 1 << 20, 20 << 10, coderws.StatusMessageTooBig, true},
		{"底层帧上限更低", 128 << 10, 16 << 10, 1 << 20, 20 << 10, coderws.StatusMessageTooBig, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var used atomic.Int64
			result := make(chan error, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
				if err != nil {
					result <- err
					return
				}
				defer func() { _ = conn.CloseNow() }()
				conn.SetReadLimit(test.readLimit)
				memory := NewOpenAIWSRequestMemory(test.maxBytes, 8, 2, func(weight int64) bool {
					if weight > test.budget {
						return false
					}
					used.Store(weight)
					return true
				})
				ctx := WithOpenAIWSRequestMemory(r.Context(), memory)
				_, _, err = ReadOpenAIWSClientMessage(ctx, conn, 0, 0, "")
				memory.Close()
				result <- err
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			client, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{CompressionMode: coderws.CompressionContextTakeover})
			require.NoError(t, err)
			defer func() { _ = client.CloseNow() }()
			_ = client.Write(ctx, coderws.MessageText, bytes.Repeat([]byte("a"), test.bodyBytes))
			_, _, err = client.Read(ctx)
			var closeErr coderws.CloseError
			require.ErrorAs(t, err, &closeErr)
			require.Equal(t, test.status, closeErr.Code)
			if test.readLimit >= test.maxBytes {
				require.Equal(t, (&OpenAIWSRequestMemoryError{TooLarge: test.tooLarge}).Error(), closeErr.Reason)
			}
			select {
			case err := <-result:
				var memoryErr *OpenAIWSRequestMemoryError
				require.ErrorAs(t, err, &memoryErr)
				require.Equal(t, test.tooLarge, memoryErr.TooLarge)
				require.Zero(t, used.Load())
			case <-ctx.Done():
				t.Fatal("准入拒绝后的 WS reader 未完成关闭与回收")
			}
		})
	}
}

func TestOpenAIWSAdmittedFrameReadFailureKeepsOnlyRetainedCache(t *testing.T) {
	var used int64
	memory := NewOpenAIWSRequestMemory(128<<10, 8, 2, func(weight int64) bool { used = weight; return true })
	require.NoError(t, memory.Retain("service", make([]byte, 100)))
	wantErr := errors.New("读取中断")
	_, err := readOpenAIWSAdmittedFrame(&memoryFailingReader{err: wantErr}, memory)
	require.ErrorIs(t, err, wantErr)
	require.EqualValues(t, 108, used)
	require.Len(t, memory.owners, 1)
	memory.Close()
	require.Zero(t, used)
}

func TestReadOpenAIWSClientMessageMemoryCancellationReleasesPartialFrame(t *testing.T) {
	controlCtx, cancelControl := context.WithCancelCause(context.Background())
	defer cancelControl(context.Canceled)
	var used atomic.Int64
	admitted := make(chan struct{}, 1)
	result := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, nil)
		if err != nil {
			result <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		conn.SetReadLimit(1 << 20)
		memory := NewOpenAIWSRequestMemory(1<<20, 8, 2, func(weight int64) bool {
			used.Store(weight)
			if weight > 0 {
				select {
				case admitted <- struct{}{}:
				default:
				}
			}
			return true
		})
		_, _, err = ReadOpenAIWSClientMessage(WithOpenAIWSRequestMemory(controlCtx, memory), conn, 0, 0, "")
		memory.Close()
		result <- err
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	client, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), nil)
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()
	writer, err := client.Writer(ctx, coderws.MessageText)
	require.NoError(t, err)
	defer func() { _ = writer.Close() }()
	_, err = writer.Write(bytes.Repeat([]byte("x"), 64<<10))
	require.NoError(t, err)
	select {
	case <-admitted:
	case <-ctx.Done():
		t.Fatal("分片正文没有在读取期间占用预算")
	}
	cancelControl(context.Canceled)
	_, _, err = client.Read(ctx)
	var closeErr coderws.CloseError
	require.ErrorAs(t, err, &closeErr)
	require.Equal(t, coderws.StatusGoingAway, closeErr.Code)
	select {
	case err := <-result:
		require.Error(t, err)
		require.Zero(t, used.Load(), "取消后必须先 join reader，再归还预算")
	case <-ctx.Done():
		t.Fatal("部分正文取消后 reader 没有退出")
	}
}

type memoryFailingReader struct{ err error }

func (r *memoryFailingReader) Read([]byte) (int, error) { return 0, r.err }

// 既有端到端多轮用例复用这一入口，在不改变请求内容与 failover 预期的条件下
// 覆盖真实服务的非空准入句柄，核对原始首帧保持不可变及结束后的预算回收。
func openAIWSMemoryTestContext(t *testing.T, ctx context.Context, firstMessage []byte) (context.Context, func()) {
	t.Helper()
	initialDigest := sha256.Sum256(firstMessage)
	var used atomic.Int64
	memory := NewOpenAIWSRequestMemory(1<<20, 8, 3, func(weight int64) bool {
		if weight > 2<<20 {
			return false
		}
		used.Store(weight)
		return true
	})
	require.NoError(t, memory.BeginAttempt(firstMessage))
	return WithOpenAIWSRequestMemory(ctx, memory), func() {
		require.Equal(t, initialDigest, sha256.Sum256(firstMessage), "服务规范化、重试和跨轮处理不得改写 handler 共享的原始首帧")
		memory.Close()
		require.Zero(t, used.Load())
	}
}
