package service

import (
	"errors"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// OfficialEgressTransitionRuntime 保存进程级不可变 Catalog、Bundle 解析器与执行器。
// 名称暂为兼容既有 service 字段；1A 的临时 provider/compiler 适配器已删除。
type OfficialEgressTransitionRuntime struct {
	BundleResolver   *officialegress.BundleResolver
	RequestCompiler  *officialegress.Compiler
	LegacyDispatcher *officialegress.LegacyCompiledDispatcher
	Guard            *officialegress.Guard
	CodexExecutor    *officialegress.Executor
	webSocketPort    *officialCodexWebSocketPort
	CodexReleaseMode officialegress.ReleaseMode
	ProcessSinks     officialegress.SinkCatalog
}

func (r *OfficialEgressTransitionRuntime) BindCodexWebSocketAcquirer(
	acquirer OfficialCodexWebSocketAcquirer,
) error {
	if r == nil || r.webSocketPort == nil {
		return errors.New("Codex Executor WebSocket port 未配置")
	}
	return r.webSocketPort.Bind(acquirer)
}

func NewOfficialEgressTransitionRuntime(
	resolver *officialegress.BundleResolver,
	compiler *officialegress.Compiler,
	dispatcher *officialegress.LegacyCompiledDispatcher,
	guard *officialegress.Guard,
	modes ...officialegress.ReleaseMode,
) *OfficialEgressTransitionRuntime {
	mode := officialegress.ReleaseModeActive
	if len(modes) > 0 && modes[0].Valid() {
		mode = modes[0]
	}
	return &OfficialEgressTransitionRuntime{
		BundleResolver: resolver, RequestCompiler: compiler,
		LegacyDispatcher: dispatcher, Guard: guard, CodexReleaseMode: mode,
		ProcessSinks: guard.ProcessSinkCatalog(),
	}
}
