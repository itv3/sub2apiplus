package officialegress

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestExecutionPolicyControllerReservesMinimumIntervalAtomically(t *testing.T) {
	controller := newExecutionPolicyController()
	policy := ExecutionPolicy{
		ID: "usage-fallback", Source: "test", MaxAttempts: 1,
		Replayable: true, MinimumInterval: time.Hour, ConcurrencyLimit: 1,
	}
	release, err := controller.acquire(context.Background(), "account:1", "usage-probe", policy)
	if err != nil {
		t.Fatal(err)
	}
	release()

	_, err = controller.acquire(context.Background(), "account:1", "usage-probe", policy)
	if !errors.Is(err, ErrExecutionPolicyMinimumInterval) {
		t.Fatalf("同账号、Sink 和策略的第二次发送未被最短间隔阻止：%v", err)
	}

	otherRelease, err := controller.acquire(context.Background(), "account:2", "usage-probe", policy)
	if err != nil {
		t.Fatalf("不同账号不应共享最短间隔 lease：%v", err)
	}
	otherRelease()
}

func TestExecutionPolicyControllerAppliesConcurrencyLimit(t *testing.T) {
	controller := newExecutionPolicyController()
	policy := ExecutionPolicy{
		ID: "paid-probe", Source: "test", MaxAttempts: 1,
		Replayable: true, ConcurrencyLimit: 1,
	}
	firstRelease, err := controller.acquire(context.Background(), "account:1", "usage-probe", policy)
	if err != nil {
		t.Fatal(err)
	}

	waitContext, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err = controller.acquire(waitContext, "account:1", "usage-probe", policy)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("并发上限没有在 Executor 边界生效：%v", err)
	}

	firstRelease()
	secondRelease, err := controller.acquire(context.Background(), "account:1", "usage-probe", policy)
	if err != nil {
		t.Fatalf("前一个发送结束后应释放并发槽位：%v", err)
	}
	secondRelease()
}
