package officialegress

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// personaReleaseBundle 是共享 Executor 唯一消费的发布能力。共享层只能取得
// executorBundleControl 中的厂商无关投影，不能读取 Persona 自有 ProfileSchema、
// Policy、fallback 闭集或部署画像。
type personaReleaseBundle interface {
	executorControl() executorBundleControl
}

// executorAttemptControl 是 Persona 方言投影给共享 Executor 的最小 attempt／lease
// 执行结果。两个上限分别保留既有调用预算和 transport 预算语义，但共享层不知道
// 它们来自何种厂商 Policy。
type executorAttemptControl struct {
	policyID               string
	invocationAttemptLimit int
	transportAttemptLimit  int
	replayable             bool
	minimumInterval        time.Duration
	concurrencyLimit       int
}

func (c executorAttemptControl) validate() error {
	if strings.TrimSpace(c.policyID) == "" || c.invocationAttemptLimit <= 0 ||
		c.transportAttemptLimit <= 0 || c.minimumInterval < 0 || c.concurrencyLimit < 0 {
		return errors.New("Executor attempt control 字段非法")
	}
	return nil
}

// executorBundleControl 只保存 Release 身份和共享控制内核确实需要执行的标量。
// 所有字段均由 Persona Bundle 在进入共享层前一次性投影。
type executorBundleControl struct {
	persona       Persona
	mode          ReleaseMode
	profileDigest string
	releaseDigest string
	bundleDigest  string
	primarySinkID SinkID
	attempt       executorAttemptControl
}

func (c executorBundleControl) validate() error {
	if !c.persona.Valid() || !c.mode.Valid() || strings.TrimSpace(c.profileDigest) == "" ||
		strings.TrimSpace(c.releaseDigest) == "" || strings.TrimSpace(c.bundleDigest) == "" ||
		strings.TrimSpace(string(c.primarySinkID)) == "" {
		return errors.New("Executor bundle control 坐标不完整")
	}
	return c.attempt.validate()
}

// executorPlanControl 是 TypedEgressPlan 投影到共享 Executor 的最小终态元数据。
// Header、Body、身份字段及其厂商 Policy 名称只存在于各 Persona 专属 Plan 中；
// 共享层只校验不可逆 attestation 和 Body 可重放性。
type executorPlanControl struct {
	persona                     Persona
	sinkID                      SinkID
	purpose                     Purpose
	endpointID                  string
	mode                        ReleaseMode
	protocol                    WireProtocol
	method                      string
	target                      *url.URL
	invocationID                string
	bodyReplayable              bool
	identityAttestationDigest   string
	dialectAttestationDigest    string
	invocationAttestationDigest string
}

func (c executorPlanControl) clone() executorPlanControl {
	out := c
	if c.target != nil {
		cloned := *c.target
		out.target = &cloned
	}
	return out
}

func (c executorPlanControl) targetIdentity() invocationAttemptTarget {
	protocol := c.protocol
	if !protocol.Valid() {
		protocol = WireProtocolHTTP
	}
	return invocationAttemptTarget{
		sinkID: c.sinkID, purpose: c.purpose,
		endpointID: strings.TrimSpace(c.endpointID), protocol: protocol,
	}
}

// typedEgressPlan 是闭集登记的 Persona 专属 Plan 能力。未登记业务包无法实现
// 私有方法，也就不能把联合 map/any Plan 送入共享 Executor。
type typedEgressPlan interface {
	persona() Persona
	control() executorPlanControl
}

type codexDialectPlan struct {
	plan    CodexEgressPlan
	dynamic EndpointDynamicInputs
}

func newCodexDialectPlan(plan CodexEgressPlan, dynamic EndpointDynamicInputs) codexDialectPlan {
	return codexDialectPlan{plan: plan.clone(), dynamic: dynamic.clone()}
}

func (codexDialectPlan) persona() Persona { return PersonaCodexCLI }

func (p codexDialectPlan) control() executorPlanControl {
	plan := p.plan
	protocol := plan.Protocol
	if !protocol.Valid() {
		// 保留既有 Codex Executor 语义：未显式声明的单次 HTTP Plan 归一为 HTTP；
		// WS 正式调用始终显式携带 websocket。
		protocol = WireProtocolHTTP
	}
	return executorPlanControl{
		persona: PersonaCodexCLI, sinkID: plan.SinkID, purpose: plan.Purpose,
		endpointID: plan.EndpointID, mode: plan.Mode, protocol: protocol,
		method: plan.Method, target: plan.URL, invocationID: plan.InvocationID,
		bodyReplayable: plan.Body.Mode() == RequestBodyReplayable,
		identityAttestationDigest: codexAttestationDigest(
			"identity", string(plan.IdentityMode), plan.IdentityFacts.Digest(),
		),
		dialectAttestationDigest: codexAttestationDigest(
			"dialect", plan.HeaderPolicy.Digest(), plan.BodyPolicy.Digest(),
			plan.BehaviorPolicy.ID, plan.RoutingHint.Digest(),
		),
		invocationAttestationDigest: codexAttestationDigest(
			"invocation", string(plan.IdentityMode), plan.IdentityFacts.invocationDigest(),
			plan.HeaderPolicy.Digest(), plan.BodyPolicy.Digest(), plan.BehaviorPolicy.ID,
		),
	}.clone()
}

// codexAttestationDigest 在 Codex 方言边界内把专属字段折叠为共享层不可解释的摘要。
// 域标签防止不同用途的同一字段集合被交叉复用。
func codexAttestationDigest(domain string, values ...string) string {
	parts := append([]string{strings.TrimSpace(domain)}, values...)
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}

type compiledExecutionControl struct {
	persona                     Persona
	sinkID                      SinkID
	purpose                     Purpose
	endpointID                  string
	route                       RouteKey
	protocol                    WireProtocol
	bodyReplayable              bool
	identityAttestationDigest   string
	dialectAttestationDigest    string
	invocationAttestationDigest string
}

type preparedDialectState interface {
	persona() Persona
}

type codexPreparedState struct {
	bundle   ReleaseBundle
	identity CodexIdentityFacts
}

func (codexPreparedState) persona() Persona { return PersonaCodexCLI }

// DialectCompiler 是闭集 Persona 编译器。compile 使用私有 TypedEgressPlan，
// 保证实现只能位于 officialegress 包内并接受代码审核。
type DialectCompiler interface {
	Persona() Persona
	compile(
		ctx context.Context,
		bundle personaReleaseBundle,
		plan typedEgressPlan,
	) (CompiledExecution, error)
}

type codexDialectCompiler struct {
	compiler RequestCompiler
}

func newCodexDialectCompiler(compiler RequestCompiler) (DialectCompiler, error) {
	if compiler == nil {
		return nil, errors.New("Codex DialectCompiler 为空")
	}
	return codexDialectCompiler{compiler: compiler}, nil
}

func (codexDialectCompiler) Persona() Persona { return PersonaCodexCLI }

func (c codexDialectCompiler) compile(
	ctx context.Context,
	bundle personaReleaseBundle,
	input typedEgressPlan,
) (CompiledExecution, error) {
	codexBundle, bundleOK := bundle.(ReleaseBundle)
	plan, planOK := input.(codexDialectPlan)
	if !bundleOK || !planOK || bundle.executorControl().persona != PersonaCodexCLI ||
		input.persona() != PersonaCodexCLI {
		return CompiledExecution{}, errors.New("Codex DialectCompiler 收到其他 Persona 的 Bundle/Plan")
	}
	compiled, err := c.compiler.Compile(ctx, codexBundle, plan.plan, plan.dynamic)
	if err != nil {
		return CompiledExecution{}, err
	}
	compiled.profileDigest = codexBundle.ProfileDigest()
	planControl := plan.control()
	compiled.control = compiledExecutionControl{
		persona: PersonaCodexCLI,
		sinkID:  compiled.endpointPlan.SinkID(), purpose: compiled.endpointPlan.Purpose(),
		endpointID:                  compiled.endpointPlan.EndpointID(),
		route:                       compiled.endpointPlan.template.route.Key,
		protocol:                    compiled.endpointPlan.Protocol(),
		bodyReplayable:              planControl.bodyReplayable,
		identityAttestationDigest:   planControl.identityAttestationDigest,
		dialectAttestationDigest:    planControl.dialectAttestationDigest,
		invocationAttestationDigest: planControl.invocationAttestationDigest,
	}
	compiled.dialectState = codexPreparedState{
		bundle: codexBundle, identity: plan.plan.IdentityFacts,
	}
	return compiled, nil
}

// DialectCompilerRegistry 在构造后不可变；每个已登记 Persona 必须恰有一个编译器。
type DialectCompilerRegistry struct {
	personas  PersonaRegistry
	compilers map[Persona]DialectCompiler
}

func NewDialectCompilerRegistry(
	personas PersonaRegistry,
	compilers []DialectCompiler,
) (DialectCompilerRegistry, error) {
	if personas.Digest() == "" || len(compilers) == 0 {
		return DialectCompilerRegistry{}, errors.New("DialectCompilerRegistry 输入为空")
	}
	registry := DialectCompilerRegistry{
		personas: personas, compilers: make(map[Persona]DialectCompiler, len(compilers)),
	}
	for _, compiler := range compilers {
		if compiler == nil {
			return DialectCompilerRegistry{}, errors.New("DialectCompilerRegistry 存在空编译器")
		}
		persona := compiler.Persona()
		if _, ok := personas.Resolve(persona); !ok {
			return DialectCompilerRegistry{}, fmt.Errorf("DialectCompiler Persona 未登记: %s", persona)
		}
		if _, duplicate := registry.compilers[persona]; duplicate {
			return DialectCompilerRegistry{}, fmt.Errorf("DialectCompiler 重复: %s", persona)
		}
		registry.compilers[persona] = compiler
	}
	for persona := range personas.byPersona {
		if registry.compilers[persona] == nil {
			return DialectCompilerRegistry{}, fmt.Errorf("Persona 缺少 DialectCompiler: %s", persona)
		}
	}
	return registry, nil
}

func (r DialectCompilerRegistry) resolve(persona Persona) (DialectCompiler, error) {
	if _, ok := r.personas.Resolve(persona); !ok {
		return nil, fmt.Errorf("Persona 未登记: %s", persona)
	}
	compiler := r.compilers[persona]
	if compiler == nil {
		return nil, fmt.Errorf("Persona %s 缺少 DialectCompiler", persona)
	}
	return compiler, nil
}

func newCodexDialectCompilerRegistry(
	personas PersonaRegistry,
	compiler RequestCompiler,
) (DialectCompilerRegistry, error) {
	codex, err := newCodexDialectCompiler(compiler)
	if err != nil {
		return DialectCompilerRegistry{}, err
	}
	return NewDialectCompilerRegistry(personas, []DialectCompiler{codex})
}
