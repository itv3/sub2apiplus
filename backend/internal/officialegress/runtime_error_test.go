package officialegress

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"testing"
	"testing/fstest"
	"time"
)

func TestRuntimeErrorPreservesGuardErrorChain(t *testing.T) {
	rejection := &GuardRejectionError{
		Reason: ReasonWrongExecutor,
		SinkID: SinkCodexResponsesForward,
	}
	err := WrapRuntimeError(RuntimeErrorCodeGuardRejected, "guard.round_trip", rejection)
	if !errors.Is(err, ErrGuardRejected) {
		t.Fatal("RuntimeError 丢失 ErrGuardRejected 错误链")
	}
	var rejectionError *GuardRejectionError
	if !errors.As(err, &rejectionError) || rejectionError.Reason != ReasonWrongExecutor {
		t.Fatalf("RuntimeError 丢失 GuardRejectionError：%v", err)
	}
	code, ok := RuntimeErrorCodeOf(fmt.Errorf("外层：%w", err))
	if !ok || code != RuntimeErrorCodeGuardRejected {
		t.Fatalf("无法从包装链提取稳定错误码：%s %v", code, ok)
	}
}

func TestRuntimeErrorPreservesBudgetAndContextChains(t *testing.T) {
	budget := WrapRuntimeError(
		RuntimeErrorCodeBehaviorAttemptBudgetExceeded,
		"executor.reserve_attempt",
		ErrBehaviorAttemptBudgetExceeded,
	)
	if !errors.Is(budget, ErrBehaviorAttemptBudgetExceeded) {
		t.Fatal("RuntimeError 丢失 Behavior attempt budget 错误链")
	}
	deadline := WrapRuntimeError(
		RuntimeErrorCodeExecutionPolicyRejected,
		"executor.acquire_policy",
		context.DeadlineExceeded,
	)
	if !errors.Is(deadline, context.DeadlineExceeded) {
		t.Fatal("RuntimeError 丢失 context deadline 错误链")
	}
}

func TestWrapRuntimeErrorKeepsInnerSpecificCode(t *testing.T) {
	inner := WrapRuntimeError(RuntimeErrorCodeGuardRejected, "guard.round_trip", ErrGuardRejected)
	outer := WrapRuntimeError(RuntimeErrorCodeTransportFailed, "executor.execute_transport", inner)
	code, ok := RuntimeErrorCodeOf(outer)
	if !ok || code != RuntimeErrorCodeGuardRejected {
		t.Fatalf("外层包装覆盖了具体错误码：%s %v", code, ok)
	}
	if _, ok := RuntimeErrorCodeOf(errors.New("普通错误")); ok {
		t.Fatal("普通错误被误识别为 RuntimeError")
	}
}

func TestExecutorExecutionPolicyErrorsUseStableRuntimeCodeAtRealBoundary(t *testing.T) {
	t.Run("最短间隔", func(t *testing.T) {
		executor, bundle, request := newRuntimeErrorExecutionPolicyFixture(t, ExecutionPolicy{
			ID: "runtime-error-minimum-interval", Source: "test", MaxAttempts: 1,
			Replayable: true, MinimumInterval: time.Hour, ConcurrencyLimit: 1,
		})
		request.ExecutionScopeKey = "account:minimum-interval"
		if _, err := executeSingleExecutorTestAttempt(
			context.Background(), executor, freshExecutorInvocationRequest(t, request),
		); err != nil {
			t.Fatal(err)
		}
		request.Bundle = bundle
		_, err := executeSingleExecutorTestAttempt(
			context.Background(), executor, freshExecutorInvocationRequest(t, request),
		)
		assertExecutionPolicyRuntimeError(t, err)
		if !errors.Is(err, ErrExecutionPolicyMinimumInterval) {
			t.Fatalf("RuntimeError 丢失最短间隔 sentinel：%v", err)
		}
		var intervalError *ExecutionPolicyMinimumIntervalError
		if !errors.As(err, &intervalError) || intervalError.PolicyID != bundle.Execution().ID {
			t.Fatalf("RuntimeError 丢失最短间隔具体错误：%v", err)
		}
	})

	t.Run("并发等待 deadline", func(t *testing.T) {
		executor, bundle, request := newRuntimeErrorExecutionPolicyFixture(t, ExecutionPolicy{
			ID: "runtime-error-concurrency", Source: "test", MaxAttempts: 1,
			Replayable: true, ConcurrencyLimit: 1,
		})
		request.ExecutionScopeKey = "account:concurrency"
		release, err := executor.executionPolicies.acquire(
			context.Background(), request.ExecutionScopeKey, request.Plan.SinkID, bundle.Execution(),
		)
		if err != nil {
			t.Fatal(err)
		}
		defer release()
		deadlineContext, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		_, err = executeSingleExecutorTestAttempt(deadlineContext, executor, freshExecutorInvocationRequest(t, request))
		assertExecutionPolicyRuntimeError(t, err)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("RuntimeError 丢失 context deadline：%v", err)
		}
	})
}

func TestExecutorBeginInvocationFailuresAreNotExecutionPolicyRejections(t *testing.T) {
	t.Run("未初始化 Executor", func(t *testing.T) {
		var executor *Executor
		_, err := executeSingleExecutorTestAttempt(context.Background(), executor, ExecutorRequest{})
		if err == nil {
			t.Fatal("未初始化 Executor 错误地执行成功")
		}
		if code, ok := RuntimeErrorCodeOf(err); ok || code != "" {
			t.Fatalf("未初始化 Executor 被误标为 execution policy：%s %v", code, err)
		}
		var runtimeError *RuntimeError
		if errors.As(err, &runtimeError) {
			t.Fatalf("未初始化 Executor 错误地产生 RuntimeError：%v", err)
		}
	})

	t.Run("不完整 Bundle", func(t *testing.T) {
		executor, _, request := newExecutorInvocationTestFixture(t, 1, 1)
		request.Bundle = ReleaseBundle{}
		_, err := executeSingleExecutorTestAttempt(context.Background(), executor, request)
		if err == nil {
			t.Fatal("不完整 Bundle 错误地执行成功")
		}
		if code, ok := RuntimeErrorCodeOf(err); ok || code != "" {
			t.Fatalf("不完整 Bundle 被误标为 execution policy：%s %v", code, err)
		}
		var runtimeError *RuntimeError
		if errors.As(err, &runtimeError) {
			t.Fatalf("不完整 Bundle 错误地产生 RuntimeError：%v", err)
		}
	})
}

func TestCatalogLoadAndResolveErrorsUseStableRuntimeCodes(t *testing.T) {
	t.Run("真实 fs.FS 加载入口", func(t *testing.T) {
		catalogFS := fstest.MapFS{
			runtimeReleaseCatalogManifestPath: &fstest.MapFile{
				Data: []byte(`{"schema_version":]}`),
			},
		}
		_, err := loadReleaseCatalogFromFS(catalogFS)
		assertRuntimeErrorCodeAndOp(
			t, err, RuntimeErrorCodeCatalogLoadFailed, "catalog.load",
		)
		var syntaxError *json.SyntaxError
		if !errors.As(err, &syntaxError) {
			t.Fatalf("Catalog load RuntimeError 丢失 JSON syntax 错误链：%v", err)
		}
	})

	t.Run("缺失 manifest 保留 PathError", func(t *testing.T) {
		_, err := loadReleaseCatalogFromFS(fstest.MapFS{})
		assertRuntimeErrorCodeAndOp(
			t, err, RuntimeErrorCodeCatalogLoadFailed, "catalog.load",
		)
		var pathError *fs.PathError
		if !errors.As(err, &pathError) {
			t.Fatalf("Catalog load RuntimeError 丢失 fs.PathError：%v", err)
		}
	})

	t.Run("非法 mode 的 Resolve 失败出口", func(t *testing.T) {
		_, err := DefaultReleaseCatalog().Resolve(ReleaseMode("invalid"))
		assertRuntimeErrorCodeAndOp(
			t, err, RuntimeErrorCodeCatalogResolveFailed, "catalog.resolve",
		)
	})

	t.Run("未预编译 Catalog 的 Resolve 失败出口", func(t *testing.T) {
		_, err := (ReleaseCatalog{}).Resolve(ReleaseModeActive)
		assertRuntimeErrorCodeAndOp(
			t, err, RuntimeErrorCodeCatalogResolveFailed, "catalog.resolve",
		)
	})
}

func newRuntimeErrorExecutionPolicyFixture(
	t *testing.T,
	execution ExecutionPolicy,
) (*Executor, ReleaseBundle, ExecutorRequest) {
	t.Helper()
	executor, baseBundle, request := newExecutorInvocationTestFixture(t, 1, 1)
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := resolver.Resolve(BundleResolveRequest{
		SinkID: request.Plan.SinkID, Mode: ReleaseModeActive,
		Execution: execution, Deployment: baseBundle.Deployment(), Behavior: baseBundle.Behavior(),
	})
	if err != nil {
		t.Fatal(err)
	}
	request.Bundle = bundle
	request.Plan.BehaviorPolicy = bundle.Behavior()
	return executor, bundle, request
}

func assertExecutionPolicyRuntimeError(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("执行策略拒绝没有返回错误")
	}
	code, ok := RuntimeErrorCodeOf(err)
	if !ok || code != RuntimeErrorCodeExecutionPolicyRejected {
		t.Fatalf("执行策略拒绝稳定 code 错误：code=%s err=%v", code, err)
	}
	var runtimeError *RuntimeError
	if !errors.As(err, &runtimeError) || runtimeError.Op != "executor.acquire_policy" {
		t.Fatalf("执行策略拒绝 RuntimeError/op 错误：%v", err)
	}
}

func assertRuntimeErrorCodeAndOp(
	t *testing.T,
	err error,
	wantCode RuntimeErrorCode,
	wantOp string,
) {
	t.Helper()
	if err == nil {
		t.Fatalf("期望 RuntimeError：code=%s op=%s", wantCode, wantOp)
	}
	code, ok := RuntimeErrorCodeOf(err)
	if !ok || code != wantCode {
		t.Fatalf("RuntimeError code 错误：got=%s want=%s err=%v", code, wantCode, err)
	}
	var runtimeError *RuntimeError
	if !errors.As(err, &runtimeError) || runtimeError.Op != wantOp {
		t.Fatalf("RuntimeError op 错误：got=%+v want=%s", runtimeError, wantOp)
	}
}
