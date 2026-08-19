package officialegress

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

// BootstrapCommit 是变更集 0 发送面快照的不可变历史锚点。
const BootstrapCommit = "38a9929eac35a39c86de2f27de8f7a805d7dae52"

//go:embed bindingcontract/testdata/release-bindings.json
var embeddedReleaseBindings []byte

// CatalogRoute 在业务 purpose 维度上补齐 wire protocol。
type CatalogRoute struct {
	Key      RouteKey
	Protocol WireProtocol
}

func (r CatalogRoute) Validate() error {
	if !r.Key.Valid() || !r.Protocol.Valid() {
		return errors.New("CatalogRoute 非法")
	}
	return nil
}

// SinkBindingInput 是构建不可变 SinkCatalog 的显式输入。
// RuntimeBindable=false 用于共享 facade 和已确认死代码，二者都不能成为运行时 binding key。
type SinkBindingInput struct {
	ID                 SinkID
	Purpose            Purpose
	Persona            Persona
	EndpointEvidence   EndpointEvidence
	Routes             []CatalogRoute
	TargetBackend      BackendKind
	LegacyBackends     []BackendKind
	EnforcementState   SinkEnforcementState
	Owner              string
	MigrationChangeset string
	ExpiryCondition    string
	RuntimeBindable    bool
	Override           *SinkGuardOverride
	ManagedPolicy      *ManagedEgressPolicy
	migrationReceipt   *MigrationReceipt
}

// SinkBinding 对外只暴露值和深拷贝，避免请求期修改 Catalog。
type SinkBinding struct {
	id                 SinkID
	purpose            Purpose
	persona            Persona
	endpointEvidence   EndpointEvidence
	routes             []CatalogRoute
	targetBackend      BackendKind
	legacyBackends     []BackendKind
	enforcementState   SinkEnforcementState
	owner              string
	migrationChangeset string
	expiryCondition    string
	runtimeBindable    bool
	migrationReceipt   *MigrationReceipt
	override           *SinkGuardOverride
	managedPolicy      *ManagedEgressPolicy
}

func newSinkBinding(input SinkBindingInput) (SinkBinding, error) {
	if strings.TrimSpace(string(input.ID)) == "" || strings.TrimSpace(string(input.Purpose)) == "" ||
		!input.Persona.Valid() || !input.EnforcementState.Valid() || strings.TrimSpace(input.Owner) == "" ||
		strings.TrimSpace(input.MigrationChangeset) == "" || strings.TrimSpace(input.ExpiryCondition) == "" {
		return SinkBinding{}, errors.New("SinkBinding 身份字段不完整")
	}
	if !input.EndpointEvidence.Valid() {
		return SinkBinding{}, fmt.Errorf("Sink %s 的 EndpointEvidence 非法", input.ID)
	}
	if input.Override != nil {
		if err := input.Override.Validate(); err != nil {
			return SinkBinding{}, fmt.Errorf("Sink %s: %w", input.ID, err)
		}
	}

	isFacade := input.Purpose == Purpose("facade")
	isDead := input.Persona == PersonaDeadCode || input.EnforcementState == SinkStatePendingRemoval
	if (isFacade || isDead) && input.RuntimeBindable {
		return SinkBinding{}, fmt.Errorf("facade/dead Sink %s 禁止成为运行时 binding key", input.ID)
	}
	if input.RuntimeBindable && len(input.Routes) == 0 {
		return SinkBinding{}, fmt.Errorf("运行时 Sink %s 没有 route", input.ID)
	}
	if input.EnforcementState == SinkStatePendingRemoval && input.Persona != PersonaDeadCode {
		return SinkBinding{}, fmt.Errorf("非 dead-code Sink %s 不得进入 pending_removal", input.ID)
	}
	managedPolicy := input.ManagedPolicy != nil
	if managedPolicy {
		if err := input.ManagedPolicy.validate(); err != nil {
			return SinkBinding{}, fmt.Errorf("Sink %s: %w", input.ID, err)
		}
		if input.Persona != PersonaUnclassified ||
			input.EndpointEvidence != EndpointEvidenceExternalPersona ||
			!input.RuntimeBindable || input.migrationReceipt != nil ||
			(input.EnforcementState != SinkStateCanaryEnforce &&
				input.EnforcementState != SinkStateEnforced) {
			return SinkBinding{}, fmt.Errorf("Sink %s 的受管第三态组合非法", input.ID)
		}
	}
	if !isDead && input.migrationReceipt == nil && !managedPolicy &&
		input.EnforcementState != SinkStateLegacyObserve {
		return SinkBinding{}, fmt.Errorf("无 MigrationReceipt 的 Sink %s 必须为 legacy_observe", input.ID)
	}
	if input.EnforcementState == SinkStateCanaryEnforce || input.EnforcementState == SinkStateEnforced {
		migrationValid := input.migrationReceipt != nil && input.migrationReceipt.validFor(input)
		if (!migrationValid && !managedPolicy) || !input.RuntimeBindable {
			return SinkBinding{}, fmt.Errorf("Sink %s 未满足 canary/enforced 定型前置条件", input.ID)
		}
	}

	routes := make([]CatalogRoute, len(input.Routes))
	copy(routes, input.Routes)
	seenRoutes := make(map[string]struct{}, len(routes))
	for _, route := range routes {
		if err := route.Validate(); err != nil {
			return SinkBinding{}, fmt.Errorf("Sink %s: %w", input.ID, err)
		}
		if route.Key.Purpose != input.Purpose {
			return SinkBinding{}, fmt.Errorf("Sink %s 的 route purpose 不一致", input.ID)
		}
		key := route.Key.String() + "\x00" + string(route.Protocol)
		if _, duplicate := seenRoutes[key]; duplicate {
			return SinkBinding{}, fmt.Errorf("Sink %s 的 route 重复", input.ID)
		}
		seenRoutes[key] = struct{}{}
	}

	legacyBackends := append([]BackendKind(nil), input.LegacyBackends...)
	seenBackends := make(map[BackendKind]struct{}, len(legacyBackends))
	for _, backend := range legacyBackends {
		if !backend.Valid() {
			return SinkBinding{}, fmt.Errorf("Sink %s 的 legacy backend 非法: %s", input.ID, backend)
		}
		if _, duplicate := seenBackends[backend]; duplicate {
			return SinkBinding{}, fmt.Errorf("Sink %s 的 legacy backend 重复: %s", input.ID, backend)
		}
		seenBackends[backend] = struct{}{}
	}
	if !isDead && !input.TargetBackend.Valid() {
		return SinkBinding{}, fmt.Errorf("Sink %s 的目标 backend 非法: %s", input.ID, input.TargetBackend)
	}

	binding := SinkBinding{
		id:                 input.ID,
		purpose:            input.Purpose,
		persona:            input.Persona,
		endpointEvidence:   input.EndpointEvidence,
		routes:             routes,
		targetBackend:      input.TargetBackend,
		legacyBackends:     legacyBackends,
		enforcementState:   input.EnforcementState,
		owner:              strings.TrimSpace(input.Owner),
		migrationChangeset: strings.TrimSpace(input.MigrationChangeset),
		expiryCondition:    strings.TrimSpace(input.ExpiryCondition),
		runtimeBindable:    input.RuntimeBindable,
	}
	if input.migrationReceipt != nil {
		receipt := *input.migrationReceipt
		binding.migrationReceipt = &receipt
	}
	if input.Override != nil {
		override := *input.Override
		binding.override = &override
	}
	if input.ManagedPolicy != nil {
		policy := *input.ManagedPolicy
		binding.managedPolicy = &policy
	}
	return binding, nil
}

func (b SinkBinding) ID() SinkID                             { return b.id }
func (b SinkBinding) Purpose() Purpose                       { return b.purpose }
func (b SinkBinding) Persona() Persona                       { return b.persona }
func (b SinkBinding) EndpointEvidence() EndpointEvidence     { return b.endpointEvidence }
func (b SinkBinding) TargetBackend() BackendKind             { return b.targetBackend }
func (b SinkBinding) EnforcementState() SinkEnforcementState { return b.enforcementState }
func (b SinkBinding) Owner() string                          { return b.owner }
func (b SinkBinding) MigrationChangeset() string             { return b.migrationChangeset }
func (b SinkBinding) ExpiryCondition() string                { return b.expiryCondition }
func (b SinkBinding) RuntimeBindable() bool                  { return b.runtimeBindable }
func (b SinkBinding) MigrationReceiptDigest() string {
	if b.migrationReceipt == nil {
		return ""
	}
	return b.migrationReceipt.Digest()
}

func (b SinkBinding) ManagedPolicy() (ManagedEgressPolicy, bool) {
	if b.managedPolicy == nil {
		return ManagedEgressPolicy{}, false
	}
	return *b.managedPolicy, true
}

// EnforcementIdentityDigest 用于长连接复用隔离。状态、binding 或迁移收据任一变化
// 都会产生新 key，旧连接不能跨越 enforcement 边界继续复用。
func (b SinkBinding) EnforcementIdentityDigest() string {
	routes := b.Routes()
	sort.Slice(routes, func(i, j int) bool {
		return catalogRouteIdentity(routes[i]) < catalogRouteIdentity(routes[j])
	})
	parts := []string{
		string(b.id), string(b.purpose), string(b.persona), string(b.targetBackend),
		string(b.enforcementState), b.MigrationReceiptDigest(),
	}
	if policy, ok := b.ManagedPolicy(); ok {
		parts = append(parts, policy.Digest())
	}
	if override, ok := b.Override(); ok {
		parts = append(parts, override.ObserveUntil.UTC().Format(time.RFC3339Nano), override.Owner, override.ReasonCode)
	}
	for _, route := range routes {
		parts = append(parts, catalogRouteIdentity(route))
	}
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}
func (b SinkBinding) Routes() []CatalogRoute {
	return append([]CatalogRoute(nil), b.routes...)
}
func (b SinkBinding) LegacyBackends() []BackendKind {
	return append([]BackendKind(nil), b.legacyBackends...)
}
func (b SinkBinding) Override() (SinkGuardOverride, bool) {
	if b.override == nil {
		return SinkGuardOverride{}, false
	}
	return *b.override, true
}

func (b SinkBinding) backendAllowed(actual BackendKind) bool {
	if b.enforcementState == SinkStateLegacyObserve {
		for _, backend := range b.legacyBackends {
			if backend == actual {
				return true
			}
		}
	}
	return b.targetBackend == actual
}

// SinkCatalog 是全部受控 SinkID 的不可变登记表。
type SinkCatalog struct {
	bindings map[SinkID]SinkBinding
}

func NewSinkCatalog(inputs []SinkBindingInput) (SinkCatalog, error) {
	if len(inputs) == 0 {
		return SinkCatalog{}, errors.New("SinkCatalog 为空")
	}
	catalog := SinkCatalog{bindings: make(map[SinkID]SinkBinding, len(inputs))}
	for _, input := range inputs {
		binding, err := newSinkBinding(input)
		if err != nil {
			return SinkCatalog{}, err
		}
		if _, duplicate := catalog.bindings[binding.id]; duplicate {
			return SinkCatalog{}, fmt.Errorf("SinkID 重复: %s", binding.id)
		}
		catalog.bindings[binding.id] = binding
	}
	return catalog, nil
}

func (c SinkCatalog) Resolve(id SinkID) (SinkBinding, bool) {
	binding, ok := c.bindings[id]
	return binding, ok
}

func (c SinkCatalog) Bindings() []SinkBinding {
	ids := make([]string, 0, len(c.bindings))
	for id := range c.bindings {
		ids = append(ids, string(id))
	}
	sort.Strings(ids)
	out := make([]SinkBinding, 0, len(ids))
	for _, id := range ids {
		out = append(out, c.bindings[SinkID(id)])
	}
	return out
}

// StartAttemptContext 在明确的业务调用边界开始一个新的出站 attempt。
// 它可以替换父流程留下的业务 binding；共享 facade 不得绑定或覆盖业务身份。
func (c SinkCatalog) StartAttemptContext(ctx context.Context, id SinkID) (context.Context, error) {
	binding, ok := c.Resolve(id)
	if !ok {
		return nil, fmt.Errorf("未登记 SinkID: %s", id)
	}
	if !binding.runtimeBindable {
		return nil, fmt.Errorf("SinkID %s 是非运行时证据，禁止作为 binding key", id)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, attemptMetadataContextKey{}, attemptMetadata{
		SinkID:          binding.id,
		Purpose:         binding.purpose,
		DeclaredPersona: binding.persona,
		ManagedPolicyDigest: func() string {
			if policy, ok := binding.ManagedPolicy(); ok {
				return policy.Digest()
			}
			return ""
		}(),
	}), nil
}

// PreserveAttemptContext 用于连接池等延迟发送边界：只有携带 FinalizationToken 的
// 同一 Sink Executor attempt 才能继续使用；共享层不得补造最小 binding，也不得
// 覆盖不同 Sink 的父 attempt。
func (c SinkCatalog) PreserveAttemptContext(ctx context.Context, id SinkID) (context.Context, error) {
	binding, ok := c.Resolve(id)
	if !ok || !binding.runtimeBindable {
		return nil, fmt.Errorf("SinkID %s 不能作为运行时 binding key", id)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	metadata, exists := attemptMetadataFromContext(ctx)
	if !exists {
		return nil, fmt.Errorf("SinkID %s 缺少当前 Executor attempt", id)
	}
	if metadata.SinkID != binding.id || metadata.Purpose != binding.purpose ||
		metadata.DeclaredPersona != binding.persona {
		return nil, fmt.Errorf("已有 attempt 与 SinkID %s 不一致", id)
	}
	if metadata.Token == nil {
		return nil, fmt.Errorf("SinkID %s 缺少当前 Executor attempt token", id)
	}
	return ctx, nil
}

// LoadEmbeddedSinkCatalog 从变更集 0 的提交生成物构建 1A provisional Catalog。
func LoadEmbeddedSinkCatalog() (SinkCatalog, error) {
	doc, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		return SinkCatalog{}, err
	}
	evidence, err := bindingcontract.NewBindingCatalog(doc)
	if err != nil {
		return SinkCatalog{}, err
	}
	if evidence.Source().BootstrapCommit != BootstrapCommit {
		return SinkCatalog{}, fmt.Errorf("bootstrap_commit 不一致: %s", evidence.Source().BootstrapCommit)
	}

	inputs := make([]SinkBindingInput, 0, len(evidence.Bindings()))
	for _, item := range evidence.Bindings() {
		input, err := sinkBindingInputFromEvidence(item)
		if err != nil {
			return SinkCatalog{}, err
		}
		inputs = append(inputs, input)
	}
	inputs, err = applyCatalogAmendments(evidence, inputs)
	if err != nil {
		return SinkCatalog{}, err
	}
	var evidenceBySink map[string]bindingcontract.ReleaseBindingDoc
	inputs, evidenceBySink, err = applyPreBootstrapSupplements(evidence, inputs)
	if err != nil {
		return SinkCatalog{}, err
	}
	inputs, evidenceBySink, err = applyCatalogRetirements(evidenceBySink, inputs)
	if err != nil {
		return SinkCatalog{}, err
	}
	inputs, err = applyMigrationReceipts(evidenceBySink, inputs)
	if err != nil {
		return SinkCatalog{}, err
	}
	inputs, err = applyLegacyObservationSinks(inputs)
	if err != nil {
		return SinkCatalog{}, err
	}
	return NewSinkCatalog(inputs)
}

func sinkBindingInputFromEvidence(item bindingcontract.ReleaseBindingDoc) (SinkBindingInput, error) {
	persona := Persona(item.Persona)
	if !persona.Valid() {
		return SinkBindingInput{}, fmt.Errorf("Sink %s 的 persona 非法", item.SinkID)
	}
	state := SinkEnforcementState(item.EnforcementState)
	if !state.Valid() {
		return SinkBindingInput{}, fmt.Errorf("Sink %s 的状态非法", item.SinkID)
	}
	targetBackend, err := parseEvidenceBackend(item.TargetBackend)
	if err != nil {
		return SinkBindingInput{}, fmt.Errorf("Sink %s: %w", item.SinkID, err)
	}

	routes := make([]CatalogRoute, 0, len(item.Routes))
	for _, route := range item.Routes {
		protocol := WireProtocol(route.Transport)
		if !protocol.Valid() {
			return SinkBindingInput{}, fmt.Errorf("Sink %s 的 route protocol 非法", item.SinkID)
		}
		routes = append(routes, CatalogRoute{
			Key: RouteKey{
				Method:  route.Method,
				Host:    strings.ToLower(route.Host),
				Path:    route.Path,
				Purpose: Purpose(item.Purpose),
			},
			Protocol: protocol,
		})
	}

	legacyBackends := make([]BackendKind, 0, len(item.Candidates))
	seenBackends := make(map[BackendKind]struct{})
	for _, candidate := range item.Candidates {
		backend, parseErr := parseEvidenceBackend(candidate.ActualBackend)
		if parseErr != nil {
			return SinkBindingInput{}, fmt.Errorf("Sink %s: %w", item.SinkID, parseErr)
		}
		if backend == BackendNone {
			continue
		}
		if _, exists := seenBackends[backend]; exists {
			continue
		}
		seenBackends[backend] = struct{}{}
		legacyBackends = append(legacyBackends, backend)
	}
	sort.Slice(legacyBackends, func(i, j int) bool { return legacyBackends[i] < legacyBackends[j] })

	runtimeBindable := item.Purpose != "facade" && persona != PersonaDeadCode &&
		state != SinkStatePendingRemoval
	return SinkBindingInput{
		ID:                 SinkID(item.SinkID),
		Purpose:            Purpose(item.Purpose),
		Persona:            persona,
		EndpointEvidence:   EndpointEvidence(item.EndpointEvidence),
		Routes:             routes,
		TargetBackend:      targetBackend,
		LegacyBackends:     legacyBackends,
		EnforcementState:   state,
		Owner:              item.Owner,
		MigrationChangeset: item.MigrationChangeset,
		ExpiryCondition:    item.ExpiryCondition,
		RuntimeBindable:    runtimeBindable,
	}, nil
}

func parseEvidenceBackend(raw string) (BackendKind, error) {
	if raw == "-" {
		return BackendNone, nil
	}
	backend := BackendKind(raw)
	if !backend.Valid() {
		return BackendNone, fmt.Errorf("backend 非法: %s", raw)
	}
	return backend, nil
}

var defaultSinkCatalog = mustLoadEmbeddedSinkCatalog()

func mustLoadEmbeddedSinkCatalog() SinkCatalog {
	catalog, err := LoadEmbeddedSinkCatalog()
	if err != nil {
		panic(fmt.Sprintf("加载 official egress SinkCatalog: %v", err))
	}
	return catalog
}

func DefaultSinkCatalog() SinkCatalog {
	return defaultSinkCatalog
}

func DefaultSinkEnforcementIdentity(id SinkID) (string, error) {
	// 生产 wiring 可能安装只含 enforced→canary/限时覆盖的进程级快照。
	// 连接池必须读取 Guard 正在执行的同一快照，避免旧连接跨越回滚边界复用。
	catalog := defaultSinkCatalog
	if guard := DefaultGuard(); guard != nil {
		catalog = guard.sinks
	}
	binding, ok := catalog.Resolve(id)
	if !ok || !binding.RuntimeBindable() {
		return "", fmt.Errorf("SinkID %s 不能用于运行时 enforcement identity", id)
	}
	return binding.EnforcementIdentityDigest(), nil
}

// StartDefaultSinkAttempt 只供已登记业务调用点开启新的发送 attempt。
func StartDefaultSinkAttempt(ctx context.Context, id SinkID) (context.Context, error) {
	catalog := defaultSinkCatalog
	if guard := DefaultGuard(); guard != nil {
		catalog = guard.ProcessSinkCatalog()
	}
	return catalog.StartAttemptContext(ctx, id)
}

// PreserveDefaultSinkAttempt 供连接池/后台延迟发送保留当前 invocation 身份。
func PreserveDefaultSinkAttempt(ctx context.Context, id SinkID) (context.Context, error) {
	catalog := defaultSinkCatalog
	if guard := DefaultGuard(); guard != nil {
		catalog = guard.ProcessSinkCatalog()
	}
	return catalog.PreserveAttemptContext(ctx, id)
}
