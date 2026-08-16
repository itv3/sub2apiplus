package officialegress

import "context"

// prepareSingleExecutorTestAttempt 让只验证单次编译／签发的测试显式经过
// invocation API；它不得进入生产源码或恢复已退休的一次性 Executor 方法。
func prepareSingleExecutorTestAttempt(
	ctx context.Context,
	executor *Executor,
	input ExecutorRequest,
) (PreparedRequest, error) {
	if input.ExpectedAttemptOrdinal == 0 {
		input.ExpectedAttemptOrdinal = 1
	}
	if !input.AttemptReason.Valid() {
		input.AttemptReason = AttemptReasonInitial
	}
	invocation, err := executor.BeginInvocation(ctx, input.Bundle, input.Plan.InvocationID)
	if err != nil {
		return PreparedRequest{}, err
	}
	return invocation.PrepareAttempt(ctx, input)
}

// executeSingleExecutorTestAttempt 与生产显式 invocation 链保持一致，供只关心
// 单次发送结果或错误分类的测试复用。
func executeSingleExecutorTestAttempt(
	ctx context.Context,
	executor *Executor,
	input ExecutorRequest,
) (TransportResult, error) {
	if input.ExpectedAttemptOrdinal == 0 {
		input.ExpectedAttemptOrdinal = 1
	}
	if !input.AttemptReason.Valid() {
		input.AttemptReason = AttemptReasonInitial
	}
	invocation, err := executor.BeginInvocation(ctx, input.Bundle, input.Plan.InvocationID)
	if err != nil {
		return TransportResult{}, err
	}
	return invocation.ExecuteAttempt(ctx, input)
}
