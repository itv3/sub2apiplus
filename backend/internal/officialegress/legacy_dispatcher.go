package officialegress

import (
	"context"
	"errors"
	"net/http"
	"sync"
)

// LegacyTransportResource 是 unsigned legacy 路径的最终资源端口。只有
// LegacyCompiledRequest.Dispatch 内部的私有 adapter 会调用它；业务层不能提前取得
// 请求或 TransportSpec，也不能把两次编译结果交叉配对。
type LegacyTransportResource interface {
	DispatchLegacy(
		context.Context,
		*http.Request,
		TransportSpec,
		string,
	) (any, error)
}

// LegacyCompiledRequest 是未迁移 sink 的不透明、单次消费发送能力。它不公开请求、
// TransportSpec、PoolDigest 或发布身份 getter；调用方只能把能力整体提交给 Dispatch。
type LegacyCompiledRequest struct {
	execution CompiledExecution
	metadata  AttemptMetadataInput
	mu        *sync.Mutex
	consumed  *bool
}

// Dispatch 由包内私有 adapter 原子完成能力消费、请求物化、TransportSpec 读取和
// 最终资源调用。请求与传输事实不会作为两个可长期持有的返回值离开该边界。
func (r LegacyCompiledRequest) Dispatch(
	ctx context.Context,
	resource LegacyTransportResource,
) (any, error) {
	if resource == nil {
		return nil, errors.New("legacy transport resource 未配置")
	}
	return dispatchLegacyCompiled(ctx, r, resource)
}

func dispatchLegacyCompiled(
	ctx context.Context,
	r LegacyCompiledRequest,
	resource LegacyTransportResource,
) (any, error) {
	if r.mu == nil || r.consumed == nil {
		return nil, errors.New("legacy compiled capability 未初始化")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if *r.consumed {
		return nil, errors.New("legacy compiled capability 已消费")
	}
	if current, ok := attemptMetadataFromContext(ctx); ok && current.Token != nil {
		return nil, errors.New("legacy dispatcher 禁止继承旧 FinalizationToken")
	}
	requestContext, err := WithAttemptMetadata(ctx, r.metadata)
	if err != nil {
		return nil, err
	}
	request, err := requestFromCompiled(requestContext, r.execution.request)
	if err != nil {
		return nil, err
	}
	*r.consumed = true
	return resource.DispatchLegacy(
		requestContext,
		request,
		r.execution.transport.Clone(),
		r.execution.poolDigest,
	)
}

// LegacyCompiledDispatcher 只允许 ProcessSinkCatalog 中仍为 legacy_observe 的 sink。
// observe_until 只影响 Guard，不会授予 canary/enforced sink 回到 unsigned 路径的权限。
type LegacyCompiledDispatcher struct {
	compiler *Compiler
	sinks    SinkCatalog
}

func NewLegacyCompiledDispatcher(
	compiler *Compiler,
	sinks SinkCatalog,
) (*LegacyCompiledDispatcher, error) {
	if compiler == nil || len(sinks.bindings) == 0 {
		return nil, errors.New("LegacyCompiledDispatcher 缺少 compiler 或 ProcessSinkCatalog")
	}
	return &LegacyCompiledDispatcher{compiler: compiler, sinks: sinks}, nil
}

func (d *LegacyCompiledDispatcher) Compile(
	ctx context.Context,
	bundle ReleaseBundle,
	plan CodexEgressPlan,
	dynamic EndpointDynamicInputs,
) (LegacyCompiledRequest, error) {
	if d == nil || d.compiler == nil {
		return LegacyCompiledRequest{}, errors.New("LegacyCompiledDispatcher 未初始化")
	}
	binding, ok := d.sinks.Resolve(plan.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.EnforcementState() != SinkStateLegacyObserve {
		return LegacyCompiledRequest{}, errors.New("legacy dispatcher 只接受 legacy_observe sink")
	}
	if binding.Purpose() != plan.Purpose || binding.Persona() != plan.DeclaredPersona {
		return LegacyCompiledRequest{}, errors.New("legacy dispatcher binding tuple 不一致")
	}
	execution, err := d.compiler.Compile(ctx, bundle, plan, dynamic)
	if err != nil {
		return LegacyCompiledRequest{}, err
	}
	consumed := false
	invocationID := plan.InvocationID
	if invocationID == "" {
		invocationID, err = newInvocationID()
		if err != nil {
			return LegacyCompiledRequest{}, err
		}
	}
	return LegacyCompiledRequest{
		execution: execution,
		metadata: AttemptMetadataInput{
			SinkID: plan.SinkID, Purpose: plan.Purpose,
			DeclaredPersona: plan.DeclaredPersona, EndpointID: execution.EndpointID(),
			InvocationID: invocationID,
			ReleaseMode:  bundle.Mode(), ReleaseDigest: bundle.ReleaseDigest(),
			BundleDigest: bundle.BundleDigest(), ProfileDigest: bundle.ProfileDigest(),
			ConnectionPoolDigest: execution.PoolDigest(),
		},
		mu: &sync.Mutex{}, consumed: &consumed,
	}, nil
}
