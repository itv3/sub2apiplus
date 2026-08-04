package service

import (
	"errors"
	"reflect"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

var officialEgressTestRuntimes sync.Map

func init() {
	officialEgressTestRuntimeFactory = func(
		httpUpstream HTTPUpstream,
	) (*OfficialEgressTransitionRuntime, error) {
		if httpUpstream == nil {
			return nil, errors.New("测试 Codex Executor HTTPUpstream 未配置")
		}
		value := reflect.ValueOf(httpUpstream)
		if value.Kind() == reflect.Pointer && !value.IsNil() {
			// 直接以接口中的指针作为键并由 map 持有它，避免前一用例对象被回收后，
			// Go 分配器复用相同地址，导致新 recorder 错绑到旧 Runtime。
			key := httpUpstream
			if cached, exists := officialEgressTestRuntimes.Load(key); exists {
				return cached.(*OfficialEgressTransitionRuntime), nil
			}
			runtimeState, err := newOfficialEgressTestRuntime(httpUpstream)
			if err != nil {
				return nil, err
			}
			actual, _ := officialEgressTestRuntimes.LoadOrStore(key, runtimeState)
			return actual.(*OfficialEgressTransitionRuntime), nil
		}
		return newOfficialEgressTestRuntime(httpUpstream)
	}
}

func newOfficialEgressTestRuntime(
	httpUpstream HTTPUpstream,
) (*OfficialEgressTransitionRuntime, error) {
	base := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		base.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), base.Recorder(),
	)
	if err != nil {
		return nil, err
	}
	return newOfficialEgressTransitionRuntimeWithExecutor(
		guard, httpUpstream, officialCodexExecutorID, officialegress.ReleaseModeActive,
	)
}

// configureObserveGuardForLocalHTTPTest 仅供把受控 persona 重定向到 httptest 的用例使用。
// 生产默认值从 1C 起保持 enforce，测试不得再隐式依赖旧的进程级 observe 默认值。
func configureObserveGuardForLocalHTTPTest(t *testing.T) {
	t.Helper()
	previous := officialegress.DefaultGuard()
	_, err := officialegress.ConfigureDefaultGuard(officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyObserve),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyObserve),
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, restoreErr := officialegress.ConfigureDefaultGuard(previous.Config(), previous.Recorder()); restoreErr != nil {
			t.Errorf("恢复测试前 Guard 失败：%v", restoreErr)
		}
	})
}
