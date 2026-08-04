package officialegress

import (
	"errors"
	"fmt"
	"strings"
)

// RuntimeErrorCode 是供程序消费的稳定错误分类；中文 Error 文本只面向人，禁止反向解析。
type RuntimeErrorCode string

const (
	RuntimeErrorCodeCatalogLoadFailed              RuntimeErrorCode = "official_egress.catalog.load_failed"
	RuntimeErrorCodeCatalogResolveFailed           RuntimeErrorCode = "official_egress.catalog.resolve_failed"
	RuntimeErrorCodeCompilerRejected               RuntimeErrorCode = "official_egress.compiler.rejected"
	RuntimeErrorCodeGuardRejected                  RuntimeErrorCode = "official_egress.guard.rejected"
	RuntimeErrorCodeBehaviorAttemptBudgetExceeded  RuntimeErrorCode = "official_egress.behavior.attempt_budget_exceeded"
	RuntimeErrorCodeTransportAttemptBudgetExceeded RuntimeErrorCode = "official_egress.transport.attempt_budget_exceeded"
	RuntimeErrorCodeExecutionPolicyRejected        RuntimeErrorCode = "official_egress.execution_policy.rejected"
	RuntimeErrorCodeTransportFailed                RuntimeErrorCode = "official_egress.transport.failed"
)

// RuntimeError 为 officialegress 运行期阶段增加稳定 code 与 op，同时完整保留原错误链。
type RuntimeError struct {
	Code RuntimeErrorCode
	Op   string
	Err  error
}

func (e *RuntimeError) Error() string {
	if e == nil {
		return "official egress 运行时错误"
	}
	if strings.TrimSpace(e.Op) == "" {
		return fmt.Sprintf("official egress 运行时错误：%v", e.Err)
	}
	return fmt.Sprintf("official egress %s 失败：%v", e.Op, e.Err)
}

func (e *RuntimeError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Err
}

// WrapRuntimeError 不覆盖已经存在的更具体 RuntimeError，避免外层 transport code
// 隐藏内层 Guard 或 attempt budget code。
func WrapRuntimeError(code RuntimeErrorCode, op string, err error) error {
	if err == nil {
		return nil
	}
	var existing *RuntimeError
	if errors.As(err, &existing) {
		return err
	}
	return &RuntimeError{Code: code, Op: strings.TrimSpace(op), Err: err}
}

// RuntimeErrorCodeOf 沿 unwrap 链提取稳定 code。
func RuntimeErrorCodeOf(err error) (RuntimeErrorCode, bool) {
	var runtimeError *RuntimeError
	if !errors.As(err, &runtimeError) || runtimeError == nil || runtimeError.Code == "" {
		return "", false
	}
	return runtimeError.Code, true
}
