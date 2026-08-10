package liveattestation

import (
	"context"
	"errors"
)

var (
	ErrUnsupportedPlatform = errors.New("live attestation is only supported when Sub2API runs on macOS; Windows support is not implemented yet")
	ErrChatGPTAppMissing   = errors.New("live attestation requires the official ChatGPT app on the Sub2API server")
)

// Provider 在发起 Live 请求前生成 ChatGPT DeviceCheck attestation。
type Provider interface {
	Check(ctx context.Context) error
	Generate(ctx context.Context) (string, error)
}

type candidateCaptureScopeContextKey struct{}

// CandidateCaptureScope 把一次 Live 调用绑定到专用验收身份和已配置代理的账号。
// 普通 provider 忽略它；candidatecapture provider 必须逐项匹配后才会生成合成值。
type CandidateCaptureScope struct {
	APIKeyID      int64
	GroupID       int64
	AccountID     int64
	ProxyID       int64
	ProxyName     string
	ProxyHost     string
	ProxyPort     int
	ProxyIsolated bool
}

// WithCandidateCaptureScope 只传递非敏感的调度身份，不携带 API Key 或代理凭据。
func WithCandidateCaptureScope(ctx context.Context, scope CandidateCaptureScope) context.Context {
	return context.WithValue(ctx, candidateCaptureScopeContextKey{}, scope)
}

// 读回调度身份的 candidateCaptureScopeFromContext 只有 candidatecapture 构建的 provider
// 消费，放在本文件会让默认构建把它算作未使用（golangci-lint 的 unused 会失败），因此
// 定义在 attestation_candidate_capture.go 内，与唯一的使用者同一构建标签。
