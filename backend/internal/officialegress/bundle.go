package officialegress

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"sort"
	"strings"
	"sync/atomic"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

type BehaviorKind string

const (
	BehaviorUserRequest BehaviorKind = "user_request"
	BehaviorAdminTest   BehaviorKind = "admin_test"
	BehaviorQuotaQuery  BehaviorKind = "quota_query"
	BehaviorOAuth       BehaviorKind = "oauth"
	BehaviorProbe       BehaviorKind = "background_probe"
)

// BehaviorPolicy 是 service 在调用开始时冻结的行为事实，不从 ProfileSpec 推导。
type BehaviorPolicy struct {
	ID              string
	Source          string
	Kind            BehaviorKind
	FallbackSinkIDs []SinkID
	AttemptBudget   int
}

func (p BehaviorPolicy) Validate() error {
	if strings.TrimSpace(p.ID) == "" || strings.TrimSpace(p.Source) == "" ||
		strings.TrimSpace(string(p.Kind)) == "" || p.AttemptBudget <= 0 {
		return errors.New("BehaviorPolicy 字段不完整")
	}
	seen := map[SinkID]bool{}
	for _, id := range p.FallbackSinkIDs {
		if strings.TrimSpace(string(id)) == "" || seen[id] {
			return errors.New("BehaviorPolicy fallback SinkID 为空或重复")
		}
		seen[id] = true
	}
	return nil
}

func cloneBehaviorPolicy(in BehaviorPolicy) BehaviorPolicy {
	out := in
	out.FallbackSinkIDs = append([]SinkID(nil), in.FallbackSinkIDs...)
	return out
}

// CloneBehaviorPolicyForService 为 service 的不可变 InvocationPlan 提供深拷贝。
// 调用方仍不能修改 ReleaseBundle 内部保存的策略。
func CloneBehaviorPolicyForService(in BehaviorPolicy) BehaviorPolicy {
	return cloneBehaviorPolicy(in)
}

// EndpointPlanTemplate 只包含静态发布与端点事实，不读取当前机器代理、CA 或平台状态。
type EndpointPlanTemplate struct {
	binding         EndpointBinding
	route           CatalogRoute
	endpoint        profilecontract.ExecutableEndpointProfile
	transport       profilecontract.ExecutableTransportProfile
	tls             TLSProfileSpec
	backend         BackendKind
	adapter         AdapterID
	connectionGroup string
	dynamicTarget   bool
}

// ResolvedEndpointPlan 是 Bundle 内经部署能力验证的 endpoint plan。动态 target、最终
// ConnectionIdentity 和 PoolDigest 仍要等 compiler 取得 EndpointDynamicInputs 后冻结。
type ResolvedEndpointPlan struct {
	template EndpointPlanTemplate
}

func (p ResolvedEndpointPlan) SinkID() SinkID   { return p.template.binding.key.SinkID }
func (p ResolvedEndpointPlan) Purpose() Purpose { return p.template.binding.key.Purpose }
func (p ResolvedEndpointPlan) PhysicalRouteID() PhysicalRouteID {
	return p.template.binding.key.PhysicalRouteID
}
func (p ResolvedEndpointPlan) EndpointID() string     { return p.template.binding.endpointID }
func (p ResolvedEndpointPlan) Protocol() WireProtocol { return p.template.route.Protocol }
func (p ResolvedEndpointPlan) Backend() BackendKind   { return p.template.backend }
func (p ResolvedEndpointPlan) DynamicTarget() bool    { return p.template.dynamicTarget }

type FallbackNode struct {
	SinkID          SinkID
	Purpose         Purpose
	PhysicalRouteID PhysicalRouteID
	EndpointID      string
	Protocol        WireProtocol
}

// ReleaseBundle 是一次账号尝试或独立系统调用的冻结值。
type ReleaseBundle struct {
	release      ResolvedCodexRelease
	primarySink  SinkID
	plans        map[string]ResolvedEndpointPlan
	execution    ExecutionPolicy
	deployment   DeploymentSupportPolicy
	behavior     BehaviorPolicy
	fallback     []FallbackNode
	bundleDigest string
}

func (b ReleaseBundle) Mode() ReleaseMode          { return b.release.Mode() }
func (b ReleaseBundle) Persona() Persona           { return b.release.Persona() }
func (b ReleaseBundle) Version() string            { return b.release.Version() }
func (b ReleaseBundle) ProfileDigest() string      { return b.release.ProfileDigest() }
func (b ReleaseBundle) ReleaseDigest() string      { return b.release.ReleaseDigest() }
func (b ReleaseBundle) BundleDigest() string       { return b.bundleDigest }
func (b ReleaseBundle) PrimarySinkID() SinkID      { return b.primarySink }
func (b ReleaseBundle) Execution() ExecutionPolicy { return b.execution }
func (b ReleaseBundle) Deployment() DeploymentSupportPolicy {
	return cloneDeploymentPolicy(b.deployment)
}
func (b ReleaseBundle) Behavior() BehaviorPolicy      { return cloneBehaviorPolicy(b.behavior) }
func (b ReleaseBundle) Release() ResolvedCodexRelease { return b.release }

// executorControl 在 Codex Bundle 边界内完成共享执行事实投影。共享 Executor
// 只能看到这些厂商无关标量，不能反向取得 Codex Policy 或 fallback 闭集。
func (b ReleaseBundle) executorControl() executorBundleControl {
	return executorBundleControl{
		persona:       b.Persona(),
		mode:          b.Mode(),
		profileDigest: b.ProfileDigest(),
		releaseDigest: b.ReleaseDigest(),
		bundleDigest:  b.BundleDigest(),
		primarySinkID: b.PrimarySinkID(),
		attempt: executorAttemptControl{
			policyID:               b.execution.ID,
			invocationAttemptLimit: b.behavior.AttemptBudget,
			transportAttemptLimit:  b.execution.MaxAttempts,
			replayable:             b.execution.Replayable,
			minimumInterval:        b.execution.MinimumInterval,
			concurrencyLimit:       b.execution.ConcurrencyLimit,
		},
	}
}

func (b ReleaseBundle) EndpointPlans() []ResolvedEndpointPlan {
	keys := make([]string, 0, len(b.plans))
	for key := range b.plans {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]ResolvedEndpointPlan, 0, len(keys))
	for _, key := range keys {
		out = append(out, b.plans[key])
	}
	return out
}

func (b ReleaseBundle) FallbackNodes() []FallbackNode {
	return append([]FallbackNode(nil), b.fallback...)
}

// TransitionFallbackAttempt 原子地把同一调用的 attempt 切换到 Bundle 已冻结的
// fallback 节点。旧 Executor、Token、Transport 与连接池身份不会继承到目标节点。
func TransitionFallbackAttempt(
	ctx context.Context,
	bundle ReleaseBundle,
	target FallbackNode,
) (context.Context, error) {
	current, ok := attemptMetadataFromContext(ctx)
	if !ok || strings.TrimSpace(current.InvocationID) == "" {
		return nil, errors.New("fallback 缺少当前 InvocationID")
	}
	if current.BundleDigest != "" && current.BundleDigest != bundle.BundleDigest() {
		return nil, errors.New("fallback 当前 attempt 与 Bundle 不一致")
	}
	matched := false
	for _, candidate := range bundle.fallback {
		if candidate == target {
			matched = true
			break
		}
	}
	if !matched {
		return nil, errors.New("fallback target 不在 Bundle 冻结闭集中")
	}
	var targetPlan ResolvedEndpointPlan
	for _, plan := range bundle.plans {
		if plan.SinkID() == target.SinkID && plan.Purpose() == target.Purpose &&
			plan.PhysicalRouteID() == target.PhysicalRouteID &&
			plan.EndpointID() == target.EndpointID && plan.Protocol() == target.Protocol {
			targetPlan = plan
			break
		}
	}
	if targetPlan.EndpointID() == "" {
		return nil, errors.New("fallback target 缺少 ResolvedEndpointPlan")
	}
	metadata := attemptMetadata{
		SinkID: target.SinkID, Purpose: target.Purpose,
		DeclaredPersona: bundle.Persona(), EndpointID: target.EndpointID,
		InvocationID: current.InvocationID,
		ReleaseMode:  bundle.Mode(), ReleaseDigest: bundle.ReleaseDigest(),
		BundleDigest: bundle.BundleDigest(), ProfileDigest: bundle.ProfileDigest(),
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, attemptMetadataContextKey{}, metadata), nil
}

func (b ReleaseBundle) ResolveEndpointPlan(
	sinkID SinkID,
	method string,
	target *url.URL,
	protocol WireProtocol,
) (ResolvedEndpointPlan, error) {
	if target == nil {
		return ResolvedEndpointPlan{}, errors.New("endpoint target 为空")
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	for _, plan := range b.plans {
		if plan.SinkID() != sinkID || plan.Protocol() != protocol ||
			plan.template.route.Key.Method != method ||
			!matchRouteHost(plan.template.route.Key.Host, target.Hostname()) ||
			!matchRoutePath(plan.template.route.Key.Path, target.EscapedPath()) {
			continue
		}
		return plan, nil
	}
	return ResolvedEndpointPlan{}, fmt.Errorf("Bundle 不包含请求 endpoint plan: %s", sinkID)
}

// ValidatedTargetAuthority 返回 Bundle 已验证静态 target 的规范 authority。
// 它不读取入站 Host，也不接受动态上传 URL；动态 target 必须先通过 compiler 的
// EndpointDynamicInputs 验证后再生成同样格式的 authority。
func (b ReleaseBundle) ValidatedTargetAuthority(
	sinkID SinkID,
	method string,
	target *url.URL,
	protocol WireProtocol,
) (string, error) {
	plan, err := b.ResolveEndpointPlan(sinkID, method, target, protocol)
	if err != nil {
		return "", err
	}
	if plan.DynamicTarget() {
		return "", errors.New("动态 target 尚未经过 compiler 验证")
	}
	return normalizeValidatedAuthority(target)
}

// ValidatedRequestTargetAuthority 用于在具体 attempt 建立前冻结请求作用域。
// 它只接受已解析 Bundle 内任一静态 endpoint plan；因此 WS→HTTP fallback 尚未
// 切换 current sink 时，也能从同一冻结 Bundle 验证 HTTP authority。
func (b ReleaseBundle) ValidatedRequestTargetAuthority(
	method string,
	target *url.URL,
	protocol WireProtocol,
) (string, error) {
	if target == nil {
		return "", errors.New("endpoint target 为空")
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	matched := false
	for _, plan := range b.plans {
		if plan.Protocol() != protocol || plan.template.route.Key.Method != method ||
			!matchRouteHost(plan.template.route.Key.Host, target.Hostname()) ||
			!matchRoutePath(plan.template.route.Key.Path, target.EscapedPath()) {
			continue
		}
		if plan.DynamicTarget() {
			return "", errors.New("动态 target 尚未经过 compiler 验证")
		}
		matched = true
	}
	if !matched {
		return "", errors.New("Bundle 不包含请求 target")
	}
	return normalizeValidatedAuthority(target)
}

func normalizeValidatedAuthority(target *url.URL) (string, error) {
	if target == nil || target.User != nil {
		return "", errors.New("target authority 非法")
	}
	scheme := strings.ToLower(strings.TrimSpace(target.Scheme))
	hostname := strings.ToLower(strings.TrimSpace(target.Hostname()))
	if hostname == "" {
		return "", errors.New("target hostname 为空")
	}
	port := target.Port()
	switch scheme {
	case "https", "wss":
		if port == "" {
			port = "443"
		}
	case "http", "ws":
		if port == "" {
			port = "80"
		}
	default:
		return "", fmt.Errorf("target scheme 未受支持: %s", scheme)
	}
	return scheme + "://" + net.JoinHostPort(hostname, port), nil
}

type BundleResolveRequest struct {
	SinkID     SinkID
	Mode       ReleaseMode
	Execution  ExecutionPolicy
	Deployment DeploymentSupportPolicy
	Behavior   BehaviorPolicy
}

type releaseModeContextKey struct{}

// WithReleaseMode 把尚未具备 codex_profile EndpointBinding 的 transport-only
// 调用绑定到进程级发布模式。它只传递冻结选择，不在 repository 内查询 active。
func WithReleaseMode(ctx context.Context, mode ReleaseMode) (context.Context, error) {
	if !mode.Valid() {
		return nil, errors.New("ReleaseMode 非法")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, releaseModeContextKey{}, mode), nil
}

// ReleaseModeFromContext 返回 transport-only 调用已冻结的进程级发布模式。
func ReleaseModeFromContext(ctx context.Context) (ReleaseMode, bool) {
	if ctx == nil {
		return "", false
	}
	mode, ok := ctx.Value(releaseModeContextKey{}).(ReleaseMode)
	return mode, ok && mode.Valid()
}

// BundleResolver 的 Catalog 在构造后不可变。ResolveCount 只用于门禁和可观测性；
// retry、WS 重拨与 fallback 必须继续持有已解析 Bundle，而不是再次调用本对象。
type BundleResolver struct {
	releases          ReleaseCatalog
	personas          PersonaRegistry
	personaReleases   PersonaReleaseCatalog
	sinks             SinkCatalog
	physical          PhysicalRouteCatalog
	bindingsByProfile map[string]EndpointBindingCatalog
	resolveCount      atomic.Uint64
}

func NewBundleResolver(releases ReleaseCatalog, sinks SinkCatalog) (*BundleResolver, error) {
	personas, err := NewCodexPersonaRegistry(sinks)
	if err != nil {
		return nil, err
	}
	personaReleases, err := NewPersonaReleaseCatalog(
		personas,
		[]PersonaReleaseSource{NewCodexPersonaReleaseSource(releases)},
	)
	if err != nil {
		return nil, err
	}
	physical, err := NewPhysicalRouteCatalog(sinks)
	if err != nil {
		return nil, err
	}
	resolver := &BundleResolver{
		releases: releases, personas: personas, personaReleases: personaReleases,
		sinks: sinks, physical: physical,
		bindingsByProfile: make(map[string]EndpointBindingCatalog),
	}
	coveredRoutes := make(map[string]bool)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		coordinate, err := personaReleases.ResolveCodexMode(PersonaCodexCLI, mode)
		if err != nil {
			return nil, err
		}
		release, err := releases.Resolve(mode)
		if err != nil {
			return nil, err
		}
		if err := validateCodexPersonaReleaseCoordinate(coordinate, release); err != nil {
			return nil, err
		}
		if _, exists := resolver.bindingsByProfile[release.ProfileDigest()]; exists {
			continue
		}
		bindings, err := NewEndpointBindingCatalog(sinks, physical, release.ExecutableProfile())
		if err != nil {
			return nil, err
		}
		resolver.bindingsByProfile[release.ProfileDigest()] = bindings
		for _, binding := range bindings.Bindings() {
			coveredRoutes[binding.Key().identity()] = true
		}
	}
	for _, sink := range sinks.Bindings() {
		if !sink.RuntimeBindable() || sink.Persona() != PersonaCodexCLI ||
			sink.EndpointEvidence() != EndpointEvidenceCodexProfile {
			continue
		}
		for _, route := range sink.Routes() {
			physicalID, _, ok := physical.ResolveRoute(route)
			if !ok {
				return nil, fmt.Errorf("Sink %s 缺少物理路由", sink.ID())
			}
			key := EndpointBindingKey{
				SinkID: sink.ID(), Purpose: sink.Purpose(), PhysicalRouteID: physicalID,
				Protocol: route.Protocol,
			}
			if !coveredRoutes[key.identity()] {
				return nil, fmt.Errorf("Sink %s 的 route 在 Active/Previous 画像中均无 EndpointBinding", sink.ID())
			}
		}
	}
	return resolver, nil
}

func (r *BundleResolver) ResolveCount() uint64 {
	if r == nil {
		return 0
	}
	return r.resolveCount.Load()
}

func (r *BundleResolver) Resolve(request BundleResolveRequest) (ReleaseBundle, error) {
	if r == nil {
		return ReleaseBundle{}, errors.New("BundleResolver 为空")
	}
	r.resolveCount.Add(1)
	if strings.TrimSpace(request.Deployment.ProxyIdentityDigest) == "" &&
		strings.EqualFold(strings.TrimSpace(request.Deployment.ProxyMode), "direct") {
		// direct 是没有外部代理这一具体身份；旧测试/离线构造可由闭集值机械补齐。
		request.Deployment.ProxyIdentityDigest = "direct"
	}
	if err := request.Execution.Validate(); err != nil {
		return ReleaseBundle{}, err
	}
	if err := request.Deployment.Validate(); err != nil {
		return ReleaseBundle{}, err
	}
	if err := request.Behavior.Validate(); err != nil {
		return ReleaseBundle{}, err
	}
	coordinate, err := r.personaReleases.ResolveCodexMode(PersonaCodexCLI, request.Mode)
	if err != nil {
		return ReleaseBundle{}, err
	}
	release, err := r.releases.Resolve(request.Mode)
	if err != nil {
		return ReleaseBundle{}, err
	}
	if err := validateCodexPersonaReleaseCoordinate(coordinate, release); err != nil {
		return ReleaseBundle{}, err
	}
	endpointBindings := r.bindingsByProfile[release.ProfileDigest()]
	if len(endpointBindings.byIdentity) == 0 {
		return ReleaseBundle{}, errors.New("Release 缺少 EndpointBindingCatalog")
	}

	sinkIDs := []SinkID{request.SinkID}
	seenSink := map[SinkID]bool{request.SinkID: true}
	for _, fallbackID := range request.Behavior.FallbackSinkIDs {
		if !seenSink[fallbackID] {
			seenSink[fallbackID] = true
			sinkIDs = append(sinkIDs, fallbackID)
		}
	}
	plans := make(map[string]ResolvedEndpointPlan)
	fallbackNodes := make([]FallbackNode, 0)
	for sinkIndex, sinkID := range sinkIDs {
		sink, ok := r.sinks.Resolve(sinkID)
		if !ok || !sink.RuntimeBindable() || sink.Persona() != PersonaCodexCLI ||
			sink.EndpointEvidence() != EndpointEvidenceCodexProfile {
			return ReleaseBundle{}, fmt.Errorf("Sink %s 不能解析 Codex Bundle", sinkID)
		}
		if !containsBackend(request.Deployment.SupportedBackends, sink.TargetBackend()) {
			return ReleaseBundle{}, fmt.Errorf("Sink %s 的 backend 不受部署支持", sinkID)
		}
		sinkPlanCount := 0
		for _, route := range sink.Routes() {
			if !r.personas.AuthorizeRoute(
				PersonaCodexCLI, sink.ID(), sink.Purpose(), route.Key, route.Protocol,
			) {
				return ReleaseBundle{}, fmt.Errorf(
					"Sink %s 的 route 未获 Persona Registry 授权", sinkID,
				)
			}
			binding, ok := endpointBindings.ResolveBindingRoute(sink, route, r.physical)
			if !ok {
				// route 可能只属于另一版本画像；当前 mode 不生成 plan。构造器已经
				// 断言每条 route 至少在 Active/Previous 之一存在，因此这里不会
				// 把拼写错误或两侧同时缺失静默放行。
				continue
			}
			sinkPlanCount++
			endpoint, transport, err := resolveBundleProfileFacts(release.ExecutableProfile(), binding.EndpointID())
			if err != nil {
				return ReleaseBundle{}, err
			}
			tls, err := compileTLSProfileSpec(
				release.ExecutableProfile(), transport, endpoint.ID, endpoint.Path,
			)
			if err != nil {
				return ReleaseBundle{}, err
			}
			adapter := adapterForBackend(sink.TargetBackend())
			if adapter == "" {
				return ReleaseBundle{}, fmt.Errorf("Sink %s backend 没有 adapter", sinkID)
			}
			template := EndpointPlanTemplate{
				binding: binding, route: route, endpoint: endpoint, transport: transport, tls: tls,
				backend: sink.TargetBackend(), adapter: adapter,
				connectionGroup: endpointConnectionGroup(endpoint),
				dynamicTarget:   endpoint.HostFromResponse,
			}
			plan := ResolvedEndpointPlan{template: template}
			plans[binding.Key().identity()] = plan
			if sinkIndex > 0 {
				fallbackNodes = append(fallbackNodes, FallbackNode{
					SinkID: sink.ID(), Purpose: sink.Purpose(),
					PhysicalRouteID: binding.Key().PhysicalRouteID,
					EndpointID:      binding.EndpointID(), Protocol: route.Protocol,
				})
			}
		}
		if sinkPlanCount == 0 {
			return ReleaseBundle{}, fmt.Errorf("Sink %s 在当前 Release 中没有 EndpointBinding", sinkID)
		}
	}
	sort.Slice(fallbackNodes, func(i, j int) bool {
		if fallbackNodes[i].SinkID != fallbackNodes[j].SinkID {
			return fallbackNodes[i].SinkID < fallbackNodes[j].SinkID
		}
		return fallbackNodes[i].PhysicalRouteID < fallbackNodes[j].PhysicalRouteID
	})
	bundleDigest, err := digestReleaseBundle(
		release, request.SinkID, plans, fallbackNodes,
		request.Execution, request.Deployment, request.Behavior,
	)
	if err != nil {
		return ReleaseBundle{}, err
	}
	return ReleaseBundle{
		release: release, primarySink: request.SinkID, plans: plans, execution: request.Execution,
		deployment: cloneDeploymentPolicy(request.Deployment),
		behavior:   cloneBehaviorPolicy(request.Behavior), fallback: fallbackNodes,
		bundleDigest: bundleDigest,
	}, nil
}

func validateCodexPersonaReleaseCoordinate(
	coordinate ResolvedPersonaRelease,
	release ResolvedCodexRelease,
) error {
	wantRole, roleErr := productionRoleForCodexMode(release.Mode())
	if roleErr != nil {
		return roleErr
	}
	if coordinate.Persona() != release.Persona() || coordinate.Role() != wantRole ||
		coordinate.Version() != release.Version() ||
		coordinate.ReleaseDigest() != release.ReleaseDigest() ||
		coordinate.ProfileDigest() != release.ProfileDigest() {
		return errors.New("Persona ReleaseCatalog 与 Codex 发布源坐标不一致")
	}
	return nil
}

func resolveBundleProfileFacts(
	profile profilecontract.ExecutableProfile,
	endpointID string,
) (profilecontract.ExecutableEndpointProfile, profilecontract.ExecutableTransportProfile, error) {
	var endpoint profilecontract.ExecutableEndpointProfile
	foundEndpoint := false
	for _, candidate := range profile.Endpoints() {
		if candidate.ID == endpointID {
			endpoint = candidate
			foundEndpoint = true
			break
		}
	}
	if !foundEndpoint {
		return profilecontract.ExecutableEndpointProfile{}, profilecontract.ExecutableTransportProfile{},
			fmt.Errorf("ProfileSpec 缺少 endpoint: %s", endpointID)
	}
	for _, transport := range profile.Transports() {
		if transport.ID == endpoint.TransportID {
			return endpoint, transport, nil
		}
	}
	return profilecontract.ExecutableEndpointProfile{}, profilecontract.ExecutableTransportProfile{},
		fmt.Errorf("endpoint %s 缺少 transport: %s", endpointID, endpoint.TransportID)
}

func adapterForBackend(backend BackendKind) AdapterID {
	switch backend {
	case BackendHTTPUpstream:
		return AdapterHTTPUpstream
	case BackendReqProfile:
		return AdapterReqProfile
	case BackendWebSocket:
		return AdapterWebSocket
	default:
		return ""
	}
}

func endpointConnectionGroup(endpoint profilecontract.ExecutableEndpointProfile) string {
	if strings.HasPrefix(endpoint.ID, "wham_") {
		return "wham"
	}
	return endpoint.ID
}

func digestReleaseBundle(
	release ResolvedCodexRelease,
	primarySink SinkID,
	plans map[string]ResolvedEndpointPlan,
	fallback []FallbackNode,
	execution ExecutionPolicy,
	deployment DeploymentSupportPolicy,
	behavior BehaviorPolicy,
) (string, error) {
	type planDigestProjection struct {
		Key             EndpointBindingKey
		EndpointID      string
		EvidenceDigest  string
		TransportID     string
		Backend         BackendKind
		Adapter         AdapterID
		ConnectionGroup string
		DynamicTarget   bool
	}
	keys := make([]string, 0, len(plans))
	for key := range plans {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	projections := make([]planDigestProjection, 0, len(keys))
	for _, key := range keys {
		plan := plans[key]
		projections = append(projections, planDigestProjection{
			Key: plan.template.binding.Key(), EndpointID: plan.EndpointID(),
			EvidenceDigest: plan.template.binding.EvidenceDigest(),
			TransportID:    plan.template.transport.ID, Backend: plan.template.backend,
			Adapter: plan.template.adapter, ConnectionGroup: plan.template.connectionGroup,
			DynamicTarget: plan.template.dynamicTarget,
		})
	}
	raw, err := json.Marshal(struct {
		ReleaseDigest string
		PrimarySink   SinkID
		Plans         []planDigestProjection
		Fallback      []FallbackNode
		Execution     ExecutionPolicy
		Deployment    DeploymentSupportPolicy
		Behavior      BehaviorPolicy
	}{
		ReleaseDigest: release.ReleaseDigest(), PrimarySink: primarySink,
		Plans: projections, Fallback: fallback,
		Execution: execution, Deployment: cloneDeploymentPolicy(deployment),
		Behavior: cloneBehaviorPolicy(behavior),
	})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}
