package handler

import (
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

// openAIWSTurnPayload 只保留正在处理的客户端原文。passthrough 的读帧与回包回调
// 分属不同 goroutine，turn 标记与锁保证迟到的完成通知不会清除下一轮正文。
// prepared 仅在一次上游调用前暂存首包，调用时立即移交，不跨整条 WS 连接保活。
type openAIWSTurnPayload struct {
	mu       sync.Mutex
	memory   *service.OpenAIWSRequestMemory
	turn     int
	model    string
	current  []byte
	prepared []byte
}

func (s *openAIWSTurnPayload) setCurrent(turn int, body []byte, model string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.memory.Retain("handler", body, s.prepared); err != nil {
		return err
	}
	s.turn, s.current = turn, body
	if model = strings.TrimSpace(model); model != "" {
		s.model = model
	}
	return nil
}

func (s *openAIWSTurnPayload) snapshot() ([]byte, string, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.current, s.model, s.turn
}

func (s *openAIWSTurnPayload) retire(turn int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.turn != turn {
		return
	}
	s.current = nil
	// 减少引用只会降低权重；连接关闭后的撤销同样无需恢复已完成的正文。
	_ = s.memory.Retain("handler", s.prepared)
}

func (s *openAIWSTurnPayload) prepare(body []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.memory.Retain("handler", s.current, body); err != nil {
		return err
	}
	s.prepared = body
	return nil
}

func (s *openAIWSTurnPayload) beginAttempt() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.memory.BeginAttempt(s.prepared)
}

func (s *openAIWSTurnPayload) takePrepared() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	body := s.prepared
	s.prepared = nil
	_ = s.memory.Retain("handler", s.current)
	return body
}
