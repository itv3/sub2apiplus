package service

import (
	"errors"
	"reflect"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

var officialEgressTestRuntimes sync.Map

// legacyProfileConfigForTest 返回发布指针固定在旧画像槽位的配置（见 officialCodexLegacyBaseProfileMode）。
func legacyProfileConfigForTest(t *testing.T) *config.Config {
	t.Helper()
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = requireOfficialCodexLegacyBaseProfileMode(t)
	return cfg
}

// useOfficialCodexLegacyProfile 把整个用例固定在旧画像槽位上：服务的发布指针配置与它使用的 Codex runtime
// 同时指向旧画像（晋升前是 active，晋升后是 previous），二者必须一致，HTTP／WS 出站上下文读配置，
// Compiler／Executor 读 runtime。runtime 显式注入服务实例，不改任何包级变量，可用于 t.Parallel 的用例。
//
// 适用于断言「旧画像的最终线上形态」的用例，例如顶层 instructions 的位置、input 项的固定下标：
// 这些形态由画像决定，晋升后 Active 换成较新画像，形态随之改变，用例本身不是在检查画像切换。较新画像的
// 对应语义由晋升后门禁与较新画像的单元用例覆盖。
func useOfficialCodexLegacyProfile(t *testing.T, svc *OpenAIGatewayService) {
	t.Helper()
	mode := requireOfficialCodexLegacyBaseProfileMode(t)
	if svc.cfg == nil {
		svc.cfg = &config.Config{}
	}
	svc.cfg.Gateway.OfficialClientProfiles.Mode = mode
	require.NotNil(t, svc.httpUpstream, "固定旧画像 runtime 需要服务已配置 HTTPUpstream")
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, svc.httpUpstream, mode)
}

// olderSlotProfileConfigForTest 返回发布指针固定在 Active／Previous 里画像版本较旧的槽位的配置：晋升前是 Active（0.163），
// 晋升后是 Previous（0.163）。与 legacyProfileConfigForTest 的区别：后者找“尚未声明基础指令载体节”的 0.160 级旧画像，
// 正式目录晋升到 0.164 后该槽位已不可达（用例会跳过）；本助手面向“0.163 形态与 0.164 不同”的用例，两种目录形态下都有槽位可用。
func olderSlotProfileConfigForTest(t *testing.T) *config.Config {
	t.Helper()
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = codexGateOlderReleaseMode(t)
	return cfg
}

// useOfficialCodexOlderSlotProfile 把整个用例固定在较旧槽位，做法同 useOfficialCodexLegacyProfile（发布指针配置与
// Codex runtime 同时指向该槽位）。适用于断言 0.163 线上形态、而 0.164 已改变该形态的用例（例如 WS 预热帧的工具目录位置、
// 逐项 passthrough 的剥离）；0.164 对应语义由 0.164 的专项用例与晋升门禁覆盖。断言本身不放宽。
func useOfficialCodexOlderSlotProfile(t *testing.T, svc *OpenAIGatewayService) {
	t.Helper()
	mode := codexGateOlderReleaseMode(t)
	if svc.cfg == nil {
		svc.cfg = &config.Config{}
	}
	svc.cfg.Gateway.OfficialClientProfiles.Mode = mode
	require.NotNil(t, svc.httpUpstream, "固定较旧槽位 runtime 需要服务已配置 HTTPUpstream")
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, svc.httpUpstream, mode)
}

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
				runtimeState, valid := cached.(*OfficialEgressTransitionRuntime)
				if !valid {
					return nil, errors.New("测试 Codex Executor runtime 缓存类型非法")
				}
				return runtimeState, nil
			}
			runtimeState, err := newOfficialEgressTestRuntime(httpUpstream)
			if err != nil {
				return nil, err
			}
			actual, _ := officialEgressTestRuntimes.LoadOrStore(key, runtimeState)
			cachedRuntime, valid := actual.(*OfficialEgressTransitionRuntime)
			if !valid {
				return nil, errors.New("测试 Codex Executor runtime 缓存类型非法")
			}
			return cachedRuntime, nil
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
