package service

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	coderws "github.com/coder/websocket"
)

type openAIWSClientReadResult struct {
	messageType coderws.MessageType
	payload     []byte
	body        *pkghttputil.OwnedRequestBody
	err         error
}

// ReadOpenAIWSClientMessage 在控制事件发送关闭帧时保留唯一读者，随后关闭传输并等待读者退出。
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
	readCtx, cancelRead := context.WithCancel(context.Background())
	defer cancelRead()
	if err := memory.startReader(); err != nil {
		return 0, nil, err
	}
	readDone := make(chan openAIWSClientReadResult, 1)
	readerExited := make(chan struct{})
	go func() {
		defer close(readerExited)
		defer memory.finishReader()
		var messageType coderws.MessageType
		var payload []byte
		var body *pkghttputil.OwnedRequestBody
		var err error
		if memory == nil {
			messageType, payload, err = conn.Read(context.Background())
		} else {
			var reader io.Reader
			messageType, reader, err = conn.Reader(context.Background())
			if err == nil {
				payload, body, err = readOpenAIWSAdmittedFrameOwnerContext(readCtx, reader, memory)
			}
		}
		readDone <- openAIWSClientReadResult{messageType: messageType, payload: payload, body: body, err: err}
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
		// 传输关闭后再取消磁盘回读，随后 join；额度不得早于暂存文件和读协程释放。
		cancelRead()
		result := <-readDone
		<-readerExited
		// 取消与成功读取可能同时就绪。即使本次已登记成功，也没有交付消费者，
		// 因此必须关闭该新租约，不能等下一轮读取（取消后通常已不会再收帧）。
		if err := memory.discardRead(result.body); err != nil {
			cause = errors.Join(cause, err)
		}
		return 0, nil, NewOpenAIWSClientCloseError(status, reason, cause)
	}

	for {
		select {
		case result := <-readDone:
			<-readerExited
			// 兼容调用方为底层连接设置更低的帧限额，同样属于本地正文拒绝。
			if memory != nil && errors.Is(result.err, coderws.ErrMessageTooBig) {
				result.err = (&OpenAIWSRequestMemoryError{TooLarge: true}).closeError()
			}
			var memoryErr *OpenAIWSRequestMemoryError
			var storageErr *pkghttputil.RequestBodyStorageError
			var localClose *OpenAIWSClientCloseError
			if errors.As(result.err, &memoryErr) {
				localClose = memoryErr.closeError()
			} else if errors.As(result.err, &storageErr) {
				localClose = &OpenAIWSClientCloseError{statusCode: coderws.StatusTryAgainLater, reason: "websocket request body storage temporarily unavailable; retry later", err: result.err}
			}
			if localClose != nil {
				// reader 已退出；仍先发关闭帧，再关闭底层连接，保持控制事件的顺序。
				_ = conn.Close(localClose.StatusCode(), localClose.Reason())
				_ = conn.CloseNow()
				return 0, nil, localClose
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

// readOpenAIWSAdmittedFrame 在每块分配前申请预算，大帧复用 HTTP 普通磁盘暂存。
// Reader 已经过 websocket 解压器，因此限额针对实际 JSON 字节而非压缩帧大小。
func readOpenAIWSAdmittedFrame(reader io.Reader, memory *OpenAIWSRequestMemory) ([]byte, error) {
	return readOpenAIWSAdmittedFrameContext(context.Background(), reader, memory)
}

func readOpenAIWSAdmittedFrameContext(ctx context.Context, reader io.Reader, memory *OpenAIWSRequestMemory) ([]byte, error) {
	if err := memory.startReader(); err != nil {
		return nil, err
	}
	defer memory.finishReader()
	payload, _, err := readOpenAIWSAdmittedFrameOwnerContext(ctx, reader, memory)
	return payload, err
}

// 由外层登记实际读者；成功后 body 的唯一关闭责任移交 memory，裸切片仅在该会话内借用。
func readOpenAIWSAdmittedFrameOwnerContext(ctx context.Context, reader io.Reader, memory *OpenAIWSRequestMemory) (result []byte, owner *pkghttputil.OwnedRequestBody, returnErr error) {
	var body *pkghttputil.OwnedRequestBody
	defer func() {
		if returnErr != nil {
			returnErr = errors.Join(returnErr, body.Close())
			memory.abortRead()
		}
	}()
	if err := memory.beginRead(); err != nil {
		return nil, nil, err
	}
	var payload []byte
	var err error
	hooks := pkghttputil.AdmittedBodyReadHooks{Reserve: memory.growRead}
	if memory.ownedBodiesEnabled() {
		body, err = pkghttputil.ReadOwnedAdmittedBodyStream(ctx, reader, memory.maxBytes, hooks)
		if err == nil {
			payload = body.Bytes()
		}
	} else {
		payload, err = pkghttputil.ReadAdmittedBodyStream(ctx, reader, memory.maxBytes, hooks)
	}
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return nil, nil, (&OpenAIWSRequestMemoryError{TooLarge: true}).closeError()
		}
		return nil, nil, err
	}
	if body != nil {
		err = memory.commitOwnedRead(body)
	} else {
		err = memory.commitRead(payload)
	}
	if err != nil {
		return nil, nil, err
	}
	return payload, body, nil
}
