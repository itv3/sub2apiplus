package service

import (
	"context"
	"errors"
	"io"
	"time"

	coderws "github.com/coder/websocket"
)

type openAIWSClientReadResult struct {
	messageType coderws.MessageType
	payload     []byte
	err         error
}

// ReadOpenAIWSClientMessage keeps one reader alive while control events send
// their close frame, then closes the transport and joins that reader.
func ReadOpenAIWSClientMessage(
	controlCtx context.Context,
	conn *coderws.Conn,
	timeout time.Duration,
	timeoutStatus coderws.StatusCode,
	timeoutReason string,
) (coderws.MessageType, []byte, error) {
	return readOpenAIWSClientMessageWithTimeoutStart(
		controlCtx,
		conn,
		timeout,
		timeoutStatus,
		timeoutReason,
		nil,
		nil,
	)
}

// readOpenAIWSClientMessageWithTimeoutStart supports readers whose timeout
// starts after a state transition, such as a completed passthrough turn. When
// timeoutActive is nil, a positive timeout starts immediately.
func readOpenAIWSClientMessageWithTimeoutStart(
	controlCtx context.Context,
	conn *coderws.Conn,
	timeout time.Duration,
	timeoutStatus coderws.StatusCode,
	timeoutReason string,
	timeoutStart <-chan struct{},
	timeoutActive func() bool,
) (coderws.MessageType, []byte, error) {
	if conn == nil {
		return 0, nil, errors.New("openai websocket client connection is nil")
	}
	if controlCtx == nil {
		controlCtx = context.Background()
	}

	memory := openAIWSRequestMemoryFromContext(controlCtx)
	readDone := make(chan openAIWSClientReadResult, 1)
	go func() {
		var messageType coderws.MessageType
		var payload []byte
		var err error
		if memory == nil {
			messageType, payload, err = conn.Read(context.Background())
		} else {
			var reader io.Reader
			messageType, reader, err = conn.Reader(context.Background())
			if err == nil {
				payload, err = readOpenAIWSAdmittedFrame(reader, memory)
			}
		}
		readDone <- openAIWSClientReadResult{messageType: messageType, payload: payload, err: err}
	}()

	var timer *time.Timer
	var timeoutCh <-chan time.Time
	startTimeout := func() {
		if timeout <= 0 || (timeoutActive != nil && !timeoutActive()) {
			return
		}
		if timer == nil {
			timer = time.NewTimer(timeout)
		} else {
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			timer.Reset(timeout)
		}
		timeoutCh = timer.C
	}
	if timeoutActive == nil || timeoutActive() {
		startTimeout()
	}
	defer func() {
		if timer != nil {
			timer.Stop()
		}
	}()

	closeAndJoin := func(status coderws.StatusCode, reason string, cause error) (coderws.MessageType, []byte, error) {
		_ = conn.Close(status, reason)
		_ = conn.CloseNow()
		<-readDone
		memory.abortRead()
		return 0, nil, NewOpenAIWSClientCloseError(status, reason, cause)
	}

	for {
		select {
		case result := <-readDone:
			// 兼容调用方为底层连接设置更低的帧限额，同样属于本地正文拒绝。
			if memory != nil && errors.Is(result.err, coderws.ErrMessageTooBig) {
				result.err = (&OpenAIWSRequestMemoryError{TooLarge: true}).closeError()
			}
			var memoryErr *OpenAIWSRequestMemoryError
			if errors.As(result.err, &memoryErr) {
				closeErr := memoryErr.closeError()
				// reader 已退出；仍先发关闭帧，再关闭底层连接，保持控制事件的顺序。
				_ = conn.Close(closeErr.StatusCode(), closeErr.Reason())
				_ = conn.CloseNow()
				return 0, nil, closeErr
			}
			return result.messageType, result.payload, result.err
		case <-timeoutStart:
			startTimeout()
		case <-timeoutCh:
			return closeAndJoin(timeoutStatus, timeoutReason, context.DeadlineExceeded)
		case <-controlCtx.Done():
			cause := context.Cause(controlCtx)
			if errors.Is(cause, ErrOpenAIWSIngressLeaseLost) {
				return closeAndJoin(
					coderws.StatusTryAgainLater,
					"websocket ingress capacity lease lost; please reconnect",
					cause,
				)
			}
			return closeAndJoin(coderws.StatusGoingAway, "websocket request canceled", cause)
		}
	}
}

// readOpenAIWSAdmittedFrame 在每块分配前申请预算，最终拼接也计入同时存活的临时副本。
// Reader 已经过 websocket 解压器，因此限额针对实际 JSON 字节而非压缩帧大小。
func readOpenAIWSAdmittedFrame(reader io.Reader, memory *OpenAIWSRequestMemory) (result []byte, returnErr error) {
	defer func() {
		if returnErr != nil {
			memory.abortRead()
		}
	}()
	if err := memory.beginRead(); err != nil {
		return nil, err
	}
	const chunkSize = 32 * 1024
	var chunks [][]byte
	var allocated, total int64
	for {
		capacity := int64(chunkSize)
		if memory.maxBytes > 0 {
			capacity = min(capacity, memory.maxBytes-total+1)
		}
		if err := memory.growRead(allocated+capacity, 0); err != nil {
			return nil, err
		}
		chunk := make([]byte, int(capacity))
		allocated += capacity
		n, err := io.ReadFull(reader, chunk)
		total += int64(n)
		if memory.maxBytes > 0 && total > memory.maxBytes {
			return nil, (&OpenAIWSRequestMemoryError{TooLarge: true}).closeError()
		}
		if n > 0 {
			chunks = append(chunks, chunk[:n])
		}
		if err == nil {
			continue
		}
		if err != io.EOF && err != io.ErrUnexpectedEOF {
			return nil, err
		}
		if err := memory.growRead(allocated, total); err != nil {
			return nil, err
		}
		payload := make([]byte, int(total))
		offset := 0
		for _, chunk := range chunks {
			offset += copy(payload[offset:], chunk)
		}
		chunks = nil
		chunk = nil
		if err := memory.commitRead(payload); err != nil {
			return nil, err
		}
		return payload, nil
	}
}
