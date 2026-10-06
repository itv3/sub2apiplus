package handler

import (
	"bytes"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestOpenAIWSTurnPayloadRetiresCompletedBodyAndBudget(t *testing.T) {
	var reserved atomic.Int64
	memory := service.NewOpenAIWSRequestMemory(1<<20, 0, 1, func(weight int64) bool {
		reserved.Store(weight)
		return true
	})
	defer memory.Close()
	payload := &openAIWSTurnPayload{memory: memory}
	first := bytes.Repeat([]byte("x"), 64<<10)
	require.NoError(t, payload.setCurrent(1, first, "gpt-5.1"))
	require.NoError(t, payload.prepare(first))
	require.NoError(t, payload.beginAttempt())
	require.Equal(t, first, payload.takePrepared())
	require.Nil(t, payload.prepared, "服务调用期间 handler 不应另持首包")

	// 失败时原文仍可重试；服务撤销本轮加工权重后，handler 仍持有唯一原文。
	require.NoError(t, memory.FinishTurn("service"))
	current, model, turn := payload.snapshot()
	require.Equal(t, "gpt-5.1", model)
	require.Equal(t, 1, turn)
	next, ok := openAIWSNextAttemptMessage(current, nil, false)
	require.True(t, ok)
	require.Equal(t, first, next)
	require.Equal(t, int64(cap(first)), reserved.Load())

	payload.retire(1)
	current, model, turn = payload.snapshot()
	require.Nil(t, current, "已成功的首轮不得在后续轮次被误重发")
	require.Equal(t, "gpt-5.1", model, "释放正文后仍须保留省略模型时的会话信息")
	require.Equal(t, 1, turn)
	require.Zero(t, reserved.Load(), "完成的正文必须同时退出准入持有快照")
	next, ok = openAIWSNextAttemptMessage(current, nil, false)
	require.False(t, ok)
	require.Nil(t, next)
}

func TestOpenAIWSTurnPayloadLaterFailureUsesCurrentOriginalBody(t *testing.T) {
	payload := &openAIWSTurnPayload{}
	first := []byte(`{"type":"response.create","model":"gpt-5.1","input":"first"}`)
	second := []byte(`{"type":"response.create","client_metadata":{"session_id":"client-session"},"input":"second"}`)
	replay := []byte(`{"type":"response.create","input":["first","second"]}`)
	require.NoError(t, payload.setCurrent(1, first, "gpt-5.1"))
	payload.retire(1)
	require.NoError(t, payload.setCurrent(2, second, "gpt-5.2"))
	payload.retire(1)
	current, model, turn := payload.snapshot()
	require.Equal(t, second, current, "首轮迟到的完成通知不能清除第二轮原文")
	require.Equal(t, "gpt-5.2", model)
	require.Equal(t, 2, turn)

	next, ok := openAIWSNextAttemptMessage(current, nil, false)
	require.True(t, ok)
	require.Equal(t, second, next, "裸换号错误须使用隔离前的当前轮，不能回退首轮")
	next[0] = 'x'
	require.Equal(t, byte('{'), second[0], "重试副本不能改写审计共享的客户端原文")

	next, ok = openAIWSNextAttemptMessage(current, replay, true)
	require.True(t, ok)
	require.Equal(t, replay, next, "服务提供的完整重放正文仍须优先")
	payload.retire(2)
	current, _, _ = payload.snapshot()
	require.Nil(t, current)
}

func TestOpenAIWSTurnPayloadFailedAdmissionKeepsPreviousSnapshot(t *testing.T) {
	memory := service.NewOpenAIWSRequestMemory(1<<20, 0, 1, func(weight int64) bool { return weight <= 32 })
	defer memory.Close()
	payload := &openAIWSTurnPayload{memory: memory}
	first := make([]byte, 16)
	require.NoError(t, payload.setCurrent(1, first, "gpt-5.1"))
	require.Error(t, payload.setCurrent(2, make([]byte, 64), "gpt-5.2"))
	current, model, turn := payload.snapshot()
	require.Equal(t, first, current)
	require.Equal(t, "gpt-5.1", model)
	require.Equal(t, 1, turn)
}

func TestOpenAIWSTurnPayloadConcurrentCompletionDoesNotRetireNextTurn(t *testing.T) {
	payload := &openAIWSTurnPayload{}
	first := []byte(`{"input":"first"}`)
	second := []byte(`{"input":"second"}`)
	require.NoError(t, payload.setCurrent(1, first, "gpt-5.1"))
	var workers sync.WaitGroup
	workers.Add(3)
	go func() {
		defer workers.Done()
		for i := 0; i < 100; i++ {
			_ = payload.setCurrent(2, second, "gpt-5.2")
		}
	}()
	go func() {
		defer workers.Done()
		for i := 0; i < 100; i++ {
			payload.retire(1)
		}
	}()
	go func() {
		defer workers.Done()
		for i := 0; i < 100; i++ {
			payload.snapshot()
		}
	}()
	workers.Wait()
	current, model, turn := payload.snapshot()
	require.Equal(t, second, current)
	require.Equal(t, "gpt-5.2", model)
	require.Equal(t, 2, turn)
}
