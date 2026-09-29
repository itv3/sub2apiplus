package service

import (
	"errors"
	"slices"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// WS 可重试错误与降级预算（画像 WebSocketRetry 节）。
//
// 旧版本官方客户端把 503 + error.code=slow_down 视为不可重试（与 server_is_overloaded 同为
// ServerOverloaded），网关对应地把流内 slow_down 归入容量降载：同账号重试一次后返回 503，
// 不降级 HTTP。目标版本把 slow_down 改为可重试：握手拒绝体或流内 wrapped error 中出现该码时
// 计入 stream 重试预算，预算耗尽后改走 HTTP；server_is_overloaded 仍不可重试。
//
// 画像没有该节（或账号不是官方 OAuth 出口）时策略为 nil，重试循环完全保持旧逻辑。

// openAIWSUpstreamErrorCodeError 在 WS 失败链中保留上游 error.code。Error() 与被包装的错误
// 完全相同，日志、下游错误文本与错误分类不受影响。
type openAIWSUpstreamErrorCodeError struct {
	code string
	err  error
}

// withOpenAIWSUpstreamErrorCode 给 WS 失败附上上游 error.code；码为空时原样返回。
func withOpenAIWSUpstreamErrorCode(code string, err error) error {
	code = strings.ToLower(strings.TrimSpace(code))
	if code == "" || err == nil {
		return err
	}
	return &openAIWSUpstreamErrorCodeError{code: code, err: err}
}

func (e *openAIWSUpstreamErrorCodeError) Error() string {
	if e == nil || e.err == nil {
		return ""
	}
	return e.err.Error()
}

func (e *openAIWSUpstreamErrorCodeError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.err
}

// openAIWSUpstreamErrorCode 取出 WS 失败链中的上游 error.code：流内错误事件由转发器附上，
// 握手被拒时从拒绝体解析（{"error":{"code":...}}）。取不到时返回空串。
func openAIWSUpstreamErrorCode(err error) string {
	var coded *openAIWSUpstreamErrorCodeError
	if errors.As(err, &coded) && coded != nil {
		return coded.code
	}
	var dialErr *openAIWSDialError
	if errors.As(err, &dialErr) && dialErr != nil && len(dialErr.ResponseBody) > 0 {
		return openAIStreamFailedEventErrorCode(dialErr.ResponseBody)
	}
	return ""
}

// officialCodexWebSocketRetryPolicy 是 WebSocketRetry 节在一次上层调用内的执行投影。
type officialCodexWebSocketRetryPolicy struct {
	section *profilecontract.WebSocketRetrySection
	used    int
}

// newOfficialCodexWebSocketRetryPolicy 只对官方 OAuth 出口、且当前 release 画像声明了
// WebSocketRetry 节的调用返回策略；否则返回 nil（旧逻辑）。
func newOfficialCodexWebSocketRetryPolicy(account *Account, mode string) *officialCodexWebSocketRetryPolicy {
	if account == nil || !account.IsOpenAIOAuth() || strings.TrimSpace(mode) == "" {
		return nil
	}
	section := officialCodexOptionalSectionsForMode(mode).WebSocketRetry
	if section == nil {
		return nil
	}
	return &officialCodexWebSocketRetryPolicy{section: section}
}

// maxAttempts 在画像预算大于默认重试上限时放宽 WS 尝试次数上限（预算 + 首次尝试）。
func (p *officialCodexWebSocketRetryPolicy) maxAttempts(defaultAttempts int) int {
	if p == nil || p.section == nil || p.section.RetryBudget+1 <= defaultAttempts {
		return defaultAttempts
	}
	return p.section.RetryBudget + 1
}

// observe 判定一次 WS 失败是否属于画像声明的可重试错误码。
//
// handled 为 false 时调用方按旧逻辑处理；为 true 时：retry 表示仍在预算内应重试，
// 否则预算已耗尽，fallbackHTTP 表示按节改走 HTTP。
func (p *officialCodexWebSocketRetryPolicy) observe(err error) (handled bool, retry bool, fallbackHTTP bool) {
	if p == nil || p.section == nil {
		return false, false, false
	}
	code := openAIWSUpstreamErrorCode(err)
	if code == "" || !slices.Contains(p.section.RetryableErrorCodes, code) {
		return false, false, false
	}
	p.used++
	if p.used <= p.section.RetryBudget {
		return true, true, false
	}
	return true, false, p.section.FallbackTransport == "http"
}
