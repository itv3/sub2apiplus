package officialegress

import (
	"context"
	"reflect"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

func TestProvisionalSharedContractsExcludePersonaPolicyFields(t *testing.T) {
	forbidden := []string{
		"identitymode", "headerpolicy", "bodypolicy", "behaviorpolicy",
		"deploymentsupportpolicy", "executionpolicy", "fallbacknode",
	}
	contracts := []reflect.Type{
		reflect.TypeOf(executorBundleControl{}),
		reflect.TypeOf(executorAttemptControl{}),
		reflect.TypeOf(executorPlanControl{}),
		reflect.TypeOf(compiledExecutionControl{}),
		reflect.TypeOf(tokenPayload{}),
	}
	for _, contract := range contracts {
		for index := 0; index < contract.NumField(); index++ {
			field := contract.Field(index)
			projection := strings.ToLower(field.Name + " " + field.Type.String())
			for _, token := range forbidden {
				if strings.Contains(projection, token) {
					t.Fatalf("共享合同 %s.%s 泄漏 Persona Policy 类型：%s",
						contract.Name(), field.Name, field.Type)
				}
			}
		}
	}

	bundleContract := reflect.TypeOf((*personaReleaseBundle)(nil)).Elem()
	if bundleContract.NumMethod() != 1 || bundleContract.Method(0).Name != "executorControl" {
		t.Fatalf("共享 Bundle 合同必须只暴露 executorControl：%s", bundleContract)
	}
	var _ personaReleaseBundle = ReleaseBundle{}
}

func TestDefaultPersonaRegistryFreezesCodexIdentityAndRouteClosure(t *testing.T) {
	registry := DefaultPersonaRegistry()
	descriptor, ok := registry.ResolveIdentity(PersonaIdentity{
		Provider: "OpenAI", OfficialProduct: "Codex", AuthFamily: "OAuth",
		UpstreamRouteFamily: "openai-oauth",
	})
	if !ok || descriptor.Persona() != PersonaCodexCLI || descriptor.Digest() == "" ||
		descriptor.AuthorityKind() != receiptcontract.AuthorityCodexExecutor ||
		registry.Digest() == "" {
		t.Fatalf("Codex PersonaDescriptor 不完整：%+v", descriptor)
	}
	if _, ok := registry.ResolveIdentity(PersonaIdentity{
		Provider: "openai", OfficialProduct: "codex", AuthFamily: "api-key",
		UpstreamRouteFamily: "openai-oauth",
	}); ok {
		t.Fatal("API Key 自报身份错误命中 Codex OAuth Persona")
	}

	allowedRoutes := 0
	for _, sink := range DefaultSinkCatalog().Bindings() {
		if !sink.RuntimeBindable() || sink.Persona() != PersonaCodexCLI {
			continue
		}
		for _, route := range sink.Routes() {
			allowed := registry.AuthorizeRoute(
				PersonaCodexCLI, sink.ID(), sink.Purpose(), route.Key, route.Protocol,
			)
			if sink.EndpointEvidence() == EndpointEvidenceCodexProfile {
				if !allowed {
					t.Fatalf("Codex 画像 route 未登记：%s %s", sink.ID(), route.Key)
				}
				allowedRoutes++
			} else if allowed {
				t.Fatalf("非画像 Codex route 被错误授权：%s", sink.ID())
			}
		}
	}
	if allowedRoutes == 0 || len(descriptor.RouteBindings()) != allowedRoutes {
		t.Fatalf("Persona route 闭集不完整：descriptor=%d catalog=%d",
			len(descriptor.RouteBindings()), allowedRoutes)
	}
	if !descriptor.excludes(PersonaExclusionSinkID, string(SinkCodexOAuthExchange)) {
		t.Fatal("Codex OAuth exchange transport-only 路径未明确排除")
	}
}

func TestPersonaRegistryRejectsUnclassifiedRuntimeSink(t *testing.T) {
	sinks, err := NewSinkCatalog([]SinkBindingInput{{
		ID: "codex.synthetic.transport-only", Purpose: "synthetic.transport",
		Persona: PersonaCodexCLI, EndpointEvidence: EndpointEvidenceTransportOnly,
		Routes: []CatalogRoute{{
			Key: RouteKey{
				Method: "POST", Host: "example.test", Path: "/oauth/token",
				Purpose: "synthetic.transport",
			},
			Protocol: WireProtocolHTTP,
		}},
		TargetBackend: BackendReqProfile, LegacyBackends: []BackendKind{BackendReqProfile},
		EnforcementState: SinkStateLegacyObserve, Owner: "test",
		MigrationChangeset: "test", ExpiryCondition: "test", RuntimeBindable: true,
	}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = NewPersonaRegistry([]PersonaDescriptorInput{{
		Persona: PersonaCodexCLI,
		Identity: PersonaIdentity{
			Provider: "openai", OfficialProduct: "codex", AuthFamily: "oauth",
			UpstreamRouteFamily: "openai-oauth",
		},
		AuthorityKind:           receiptcontract.AuthorityCodexExecutor,
		AllowedEndpointEvidence: []EndpointEvidence{EndpointEvidenceCodexProfile},
		Exclusions: []PersonaExclusion{{
			Dimension: PersonaExclusionAuthFamily, Value: "api-key", Reason: "test",
		}},
	}}, sinks)
	if err == nil || !strings.Contains(err.Error(), "未被允许或明确排除") {
		t.Fatalf("未分类运行时 Sink 未 fail-close：%v", err)
	}
}

func TestPersonaReleaseCatalogMatchesCodexAndKeepsRollbackPair(t *testing.T) {
	catalog := DefaultPersonaReleaseCatalog()
	pair, err := catalog.RollbackPair(PersonaCodexCLI)
	if err != nil {
		t.Fatal(err)
	}
	for _, release := range []ResolvedPersonaRelease{pair.Active, pair.Rollback} {
		mode, modeErr := codexModeForProductionRole(release.Role())
		if modeErr != nil {
			t.Fatal(modeErr)
		}
		codex, resolveErr := DefaultReleaseCatalog().Resolve(mode)
		if resolveErr != nil {
			t.Fatal(resolveErr)
		}
		if err := validateCodexPersonaReleaseCoordinate(release, codex); err != nil {
			t.Fatal(err)
		}
	}
	if pair.Active.ReleaseDigest() == pair.Rollback.ReleaseDigest() || catalog.Digest() == "" ||
		catalog.PersonaRegistryDigest() != DefaultPersonaRegistry().Digest() ||
		pair.Active.Role() != ProductionReleaseActive ||
		pair.Rollback.Role() != ProductionReleaseRollback {
		t.Fatalf("Persona production active/rollback 对非法：%+v", pair)
	}
	compatRollback, err := catalog.ResolveCodexMode(PersonaCodexCLI, ReleaseModePrevious)
	if err != nil || compatRollback.ReleaseDigest() != pair.Rollback.ReleaseDigest() {
		t.Fatalf("Codex previous facade 未严格映射 production rollback：%+v %v", compatRollback, err)
	}
	if _, err := catalog.Resolve(PersonaChatGPTWeb, ProductionReleaseActive); err == nil {
		t.Fatal("未登记 Persona 错误取得生产发布坐标")
	}
}

type foldedPersonaReleaseSource struct{}

func (foldedPersonaReleaseSource) Persona() Persona { return PersonaCodexCLI }

func (foldedPersonaReleaseSource) ResolvePersonaRelease(
	role ProductionReleaseRole,
) (ResolvedPersonaRelease, error) {
	return ResolvedPersonaRelease{
		persona: PersonaCodexCLI, role: role, version: "test",
		releaseDigest: strings.Repeat("a", 64), profileDigest: strings.Repeat("b", 64),
	}, nil
}

func TestPersonaReleaseCatalogRejectsFoldedActiveRollback(t *testing.T) {
	_, err := NewPersonaReleaseCatalog(
		DefaultPersonaRegistry(), []PersonaReleaseSource{foldedPersonaReleaseSource{}},
	)
	if err == nil || !strings.Contains(err.Error(), "active/rollback 被折叠") {
		t.Fatalf("折叠发布对未 fail-close：%v", err)
	}
}

type wrongPersonaDialectCompiler struct{}

func (wrongPersonaDialectCompiler) Persona() Persona { return PersonaChatGPTWeb }

func (wrongPersonaDialectCompiler) compile(
	context.Context,
	personaReleaseBundle,
	typedEgressPlan,
) (CompiledExecution, error) {
	return CompiledExecution{}, nil
}

func TestDialectCompilerRegistryRejectsMissingAndCrossPersonaCompiler(t *testing.T) {
	personas := DefaultPersonaRegistry()
	if _, err := NewDialectCompilerRegistry(personas, nil); err == nil {
		t.Fatal("缺少 DialectCompiler 未 fail-close")
	}
	if _, err := NewDialectCompilerRegistry(
		personas, []DialectCompiler{wrongPersonaDialectCompiler{}},
	); err == nil || !strings.Contains(err.Error(), "Persona 未登记") {
		t.Fatalf("跨 Persona DialectCompiler 未 fail-close：%v", err)
	}
	registry, err := newCodexDialectCompilerRegistry(personas, NewCompiler())
	if err != nil {
		t.Fatal(err)
	}
	compiler, err := registry.resolve(PersonaCodexCLI)
	if err != nil || compiler.Persona() != PersonaCodexCLI {
		t.Fatalf("Codex DialectCompiler 未正确登记：%v", err)
	}
}

func TestExecutorTokenUsesRegisteredPersonaAuthority(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 1, 1)
	if _, err := newPersonaExecutor(
		"wrong-persona-executor", PersonaChatGPTWeb, executor.personas,
		executor.dialects, executor.registry, DefaultGuard(),
	); err == nil {
		t.Fatal("未登记 Persona 错误取得共享 Executor")
	}
	request = freshExecutorInvocationRequest(t, request, 1)
	invocation, err := executor.BeginInvocation(
		context.Background(), bundle, request.Plan.InvocationID,
	)
	if err != nil {
		t.Fatal(err)
	}
	prepared, err := invocation.PrepareAttempt(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	descriptor, ok := executor.personas.Resolve(PersonaCodexCLI)
	if !ok {
		t.Fatal("Executor 缺少 Codex PersonaDescriptor")
	}
	payload := prepared.Token().payload
	if payload.Persona != descriptor.Persona() ||
		payload.AuthorityKind != descriptor.AuthorityKind() ||
		executor.issuer.persona != descriptor.Persona() {
		t.Fatalf("FinalizationToken 未绑定 Persona Registry：%+v", payload)
	}
}

func TestPersonaExecutorRejectsMismatchedRegistry(t *testing.T) {
	executor, _, _ := newExecutorInvocationTestFixture(t, 1, 1)
	input := codexPersonaDescriptorInput()
	input.Identity.UpstreamRouteFamily = "different-openai-oauth"
	personas, err := NewPersonaRegistry(
		[]PersonaDescriptorInput{input}, DefaultSinkCatalog(),
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newPersonaExecutor(
		"mismatched-registry-executor", PersonaCodexCLI, personas,
		executor.dialects, executor.registry, DefaultGuard(),
	); err == nil || !strings.Contains(err.Error(), "PersonaRegistry 不一致") {
		t.Fatalf("不一致的 PersonaRegistry 未 fail-close：%v", err)
	}
}

func TestSharedExecutorControlRejectsCrossLayerFactMismatch(t *testing.T) {
	executor, bundle, request := newExecutorInvocationTestFixture(t, 1, 1)
	request = freshExecutorInvocationRequest(t, request, 1)
	plan := newCodexDialectPlan(request.Plan, request.DynamicInputs)
	control := plan.control()
	bundleControl := bundle.executorControl()
	if err := validateExecutorInput(control, bundleControl); err != nil {
		t.Fatalf("有效 TypedEgressPlan 被拒绝：%v", err)
	}

	missingAttestation := control
	missingAttestation.dialectAttestationDigest = ""
	if err := validateExecutorInput(missingAttestation, bundleControl); err == nil ||
		!strings.Contains(err.Error(), "字段不完整") {
		t.Fatalf("缺失 Persona attestation 未 fail-close：%v", err)
	}

	dialect, err := executor.dialects.resolve(PersonaCodexCLI)
	if err != nil {
		t.Fatal(err)
	}
	compiled, err := dialect.compile(context.Background(), bundle, plan)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateCompiledExecutionForPlan(compiled, bundleControl, control); err != nil {
		t.Fatalf("有效 CompiledExecution 被拒绝：%v", err)
	}

	wrongEndpoint := compiled
	wrongEndpoint.control.endpointID = "other-endpoint"
	if err := validateCompiledExecutionForPlan(wrongEndpoint, bundleControl, control); err == nil {
		t.Fatal("跨层 EndpointID 漂移未 fail-close")
	}

	wrongAttestation := compiled
	wrongAttestation.control.dialectAttestationDigest = strings.Repeat("a", 64)
	if err := validateCompiledExecutionForPlan(wrongAttestation, bundleControl, control); err == nil {
		t.Fatal("DialectCompiler attestation 漂移未 fail-close")
	}

	wrongProtocol := compiled
	if wrongProtocol.control.protocol == WireProtocolHTTP {
		wrongProtocol.control.protocol = WireProtocolWebSocket
	} else {
		wrongProtocol.control.protocol = WireProtocolHTTP
	}
	if err := validateCompiledExecutionForPlan(wrongProtocol, bundleControl, control); err == nil {
		t.Fatal("跨层 Protocol 漂移未 fail-close")
	}

	wrongProfile := compiled
	wrongProfile.profileDigest = strings.Repeat("a", 64)
	if err := validateCompiledExecutionForPlan(wrongProfile, bundleControl, control); err == nil {
		t.Fatal("DialectCompiler 返回错误 ProfileDigest 未 fail-close")
	}

	wrongRoute := compiled
	wrongRoute.control.route.Path = "/unregistered-path"
	if err := validateCompiledExecutionForPlan(wrongRoute, bundleControl, control); err == nil ||
		!strings.Contains(err.Error(), "route 与 final request 不一致") {
		t.Fatalf("route/final request 漂移未 fail-close：%v", err)
	}
}
