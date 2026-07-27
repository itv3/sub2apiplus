package service

import (
	"context"
	"errors"
	"testing"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

// fakeOpenAIWSFrameConn 按脚本回放上游事件，并记录写入的帧，
// 用于在不建立真实 WebSocket 的前提下验证预热往返。
type fakeOpenAIWSFrameConn struct {
	written  [][]byte
	inbound  [][]byte
	readIdx  int
	readErr  error
	writeErr error
}

func (f *fakeOpenAIWSFrameConn) ReadFrame(context.Context) (coderws.MessageType, []byte, error) {
	if f.readErr != nil {
		return coderws.MessageText, nil, f.readErr
	}
	if f.readIdx >= len(f.inbound) {
		return coderws.MessageText, nil, errors.New("no more inbound frames")
	}
	payload := f.inbound[f.readIdx]
	f.readIdx++
	return coderws.MessageText, payload, nil
}

func (f *fakeOpenAIWSFrameConn) WriteFrame(_ context.Context, _ coderws.MessageType, payload []byte) error {
	if f.writeErr != nil {
		return f.writeErr
	}
	f.written = append(f.written, append([]byte(nil), payload...))
	return nil
}

func (f *fakeOpenAIWSFrameConn) Close() error { return nil }

// 预热往返读到终止事件为止，只取 response id；中间事件全部丢弃，不回传客户端。
func TestPassthroughOfficialPrewarmReturnsResponseID(t *testing.T) {
	conn := &fakeOpenAIWSFrameConn{
		inbound: [][]byte{
			[]byte(`{"type":"response.created","response":{"id":"resp_prewarm_1"}}`),
			[]byte(`{"type":"response.output_item.added"}`),
			[]byte(`{"type":"response.completed","response":{"id":"resp_prewarm_1"}}`),
		},
	}
	prewarm := []byte(`{"type":"response.create","generate":false}`)

	responseID, err := (&OpenAIGatewayService{}).runOpenAIWSPassthroughOfficialPrewarm(
		context.Background(),
		conn,
		prewarm,
	)

	require.NoError(t, err)
	require.Equal(t, "resp_prewarm_1", responseID)
	require.Len(t, conn.written, 1, "预热只写一帧")
	require.JSONEq(t, string(prewarm), string(conn.written[0]))
	require.Equal(t, 3, conn.readIdx, "必须读到终止事件才结束")
}

// 上游在预热阶段返回 error 事件时必须失败，不能带着无效链路继续发业务帧。
func TestPassthroughOfficialPrewarmFailsOnErrorEvent(t *testing.T) {
	conn := &fakeOpenAIWSFrameConn{
		inbound: [][]byte{
			[]byte(`{"type":"error","error":{"message":"prewarm rejected"}}`),
		},
	}

	_, err := (&OpenAIGatewayService{}).runOpenAIWSPassthroughOfficialPrewarm(
		context.Background(),
		conn,
		[]byte(`{"type":"response.create","generate":false}`),
	)

	require.Error(t, err)
}

// 终止事件到达但全程没有 response id 时同样失败：没有 id 就无法把业务帧挂上去。
func TestPassthroughOfficialPrewarmFailsWithoutResponseID(t *testing.T) {
	conn := &fakeOpenAIWSFrameConn{
		inbound: [][]byte{
			[]byte(`{"type":"response.completed"}`),
		},
	}

	_, err := (&OpenAIGatewayService{}).runOpenAIWSPassthroughOfficialPrewarm(
		context.Background(),
		conn,
		[]byte(`{"type":"response.create","generate":false}`),
	)

	require.Error(t, err)
}

func TestPassthroughOfficialPrewarmPropagatesWriteError(t *testing.T) {
	conn := &fakeOpenAIWSFrameConn{writeErr: errors.New("upstream closed")}

	_, err := (&OpenAIGatewayService{}).runOpenAIWSPassthroughOfficialPrewarm(
		context.Background(),
		conn,
		[]byte(`{"type":"response.create","generate":false}`),
	)

	require.Error(t, err)
	require.Empty(t, conn.written)
}
