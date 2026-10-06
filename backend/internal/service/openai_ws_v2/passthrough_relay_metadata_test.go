package openai_ws_v2

import (
	"context"
	"sync"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

func TestRelayFirstMessageMetadataMatchesOriginalFrame(t *testing.T) {
	for _, messageType := range []coderws.MessageType{0, coderws.MessageText, coderws.MessageBinary} {
		t.Run(relayMessageTypeString(messageType), func(t *testing.T) {
			first := []byte(`{"type":"response.create","model":"gpt-5.1","input":[{"role":"user","content":"hello"}]}`)
			createdAt := time.Date(2026, time.August, 17, 9, 59, 59, 0, time.UTC)
			relayAt := createdAt.Add(time.Second)
			type observation struct {
				result RelayResult
				turn   RelayTurnResult
				traces []RelayTraceEvent
				writes []passthroughTestFrame
			}
			run := func(withMetadata bool) observation {
				clientConn := newPassthroughTestFrameConn(nil, false)
				upstreamConn := newPassthroughTestFrameConn([]passthroughTestFrame{{
					msgType: coderws.MessageText,
					payload: []byte(`{"type":"response.completed","response":{"id":"resp_metadata","model":"gpt-5.1","usage":{"input_tokens":4,"output_tokens":2}}}`),
				}}, true)
				var observed observation
				var traceMu sync.Mutex
				options := RelayOptions{
					FirstMessageType: messageType, FirstMessageSent: true,
					FirstTurnStartedAt: createdAt,
					Now:                func() time.Time { return relayAt },
					OnTurnComplete:     func(turn RelayTurnResult) { observed.turn = turn },
					OnTrace: func(event RelayTraceEvent) {
						if event.Stage == "relay_start" || event.Stage == "write_first_message_skipped" {
							traceMu.Lock()
							observed.traces = append(observed.traces, event)
							traceMu.Unlock()
						}
					},
				}
				body := first
				if withMetadata {
					body = nil
					options.FirstMessageMetadata = &RelayFirstMessageMetadata{
						RequestModel: "gpt-5.1", ResponseCreate: true, PayloadBytes: len(first),
					}
				}
				var exit *RelayExit
				observed.result, exit = Relay(context.Background(), clientConn, upstreamConn, body, options)
				require.Nil(t, exit)
				require.Empty(t, upstreamConn.Writes(), "调用方已发出的首帧不能重复发送")
				observed.writes = clientConn.Writes()
				return observed
			}
			original := run(false)
			metadata := run(true)
			require.Equal(t, original, metadata, "小元数据须保持计费时间、模型、用量和首帧 trace 一致")
			require.Equal(t, createdAt, metadata.turn.StartedAt, "默认消息类型同样按文本 response.create 处理")
			require.Equal(t, "gpt-5.1", metadata.result.RequestModel)
			require.Equal(t, 4, metadata.result.Usage.InputTokens)
			require.Len(t, metadata.traces, 2)
			require.Equal(t, len(first), metadata.traces[0].PayloadBytes)
		})
	}
}

func TestRelayFirstMessageMetadataOnlyAppliesAfterSynchronousSend(t *testing.T) {
	first := []byte(`{"type":"response.create","model":"gpt-5.1","input":[]}`)
	clientConn := newPassthroughTestFrameConn(nil, false)
	upstreamConn := newPassthroughTestFrameConn([]passthroughTestFrame{{
		msgType: coderws.MessageText,
		payload: []byte(`{"type":"response.completed","response":{"id":"resp_body","usage":{"input_tokens":1,"output_tokens":1}}}`),
	}}, true)
	result, exit := Relay(context.Background(), clientConn, upstreamConn, first, RelayOptions{
		FirstMessageMetadata: &RelayFirstMessageMetadata{RequestModel: "ignored-model", PayloadBytes: 1},
	})
	require.Nil(t, exit)
	require.Equal(t, "gpt-5.1", result.RequestModel)
	writes := upstreamConn.Writes()
	require.Len(t, writes, 1)
	require.Equal(t, first, writes[0].payload)
}
