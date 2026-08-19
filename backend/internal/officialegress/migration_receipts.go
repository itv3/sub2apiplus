package officialegress

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

// 整个 catalogdata 目录被嵌入，未来新增收据产物时不需要扩大 go:embed 声明；
// 收据仍只能引用自身清单中显式登记并通过摘要校验的文件。
//
//go:embed catalogdata
var migrationReceiptFS embed.FS

type migrationReceiptManifest = receiptcontract.Manifest
type migrationReceiptDoc = receiptcontract.Document
type migrationCandidateEvidence = receiptcontract.CandidateEvidence

type migrationRouteClaim struct {
	route                 CatalogRoute
	evidenceKind          string
	evidenceID            string
	backend               BackendKind
	adapterID             AdapterID
	transportID           string
	transportIDsByRelease map[string]string
}

func (c migrationRouteClaim) matchesTransport(releaseDigest, transportID string) bool {
	if len(c.transportIDsByRelease) == 0 {
		return c.transportID == transportID
	}
	return c.transportIDsByRelease[releaseDigest] == transportID
}

// MigrationReceipt 是一次 Sink 迁移验收的只读机器收据。其字段不导出，且只有在
// 实际 fixture/verification 可读取、全部 route 都被覆盖后才能构造。
type MigrationReceipt struct {
	digest        string
	sinkID        SinkID
	approvedState SinkEnforcementState
	bindingDigest string
	authorityKind receiptcontract.AuthorityKind
	authorityID   ExecutorID
	tokenIssuerID ExecutorID
	routeClaims   []migrationRouteClaim
}

func (r MigrationReceipt) Digest() string { return r.digest }

func loadMigrationReceiptManifest() (migrationReceiptManifest, error) {
	return loadMigrationReceiptManifestAt("catalogdata/migration-receipts.json")
}

// loadChangeset3MigrationReceiptManifest 单独加载变更集 3 的状态提升收据。
// 既有变更集 1B/1C 清单保持逐字节不变，避免新迁移覆盖已验收历史锚点。
func loadChangeset3MigrationReceiptManifest() (migrationReceiptManifest, error) {
	return loadMigrationReceiptManifestAt("catalogdata/changeset3-migration-receipts.json")
}

func loadMigrationReceiptManifestAt(path string) (migrationReceiptManifest, error) {
	raw, err := migrationReceiptFS.ReadFile(path)
	if err != nil {
		return migrationReceiptManifest{}, err
	}
	manifest, err := receiptcontract.ParseManifest(raw)
	if err != nil {
		return migrationReceiptManifest{}, err
	}
	if manifest.BootstrapCommit != BootstrapCommit {
		return migrationReceiptManifest{}, errors.New("MigrationReceipt bootstrap_commit 非法")
	}
	for _, document := range manifest.Receipts {
		if err := receiptcontract.VerifyArtifacts(migrationReceiptFS, document); err != nil {
			return migrationReceiptManifest{}, fmt.Errorf("MigrationReceipt %s 产物验证失败: %w", document.SinkID, err)
		}
	}
	return manifest, nil
}

func applyMigrationReceipts(
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, error) {
	versionRoutes, err := loadVersionRouteReceiptManifest()
	if err != nil {
		return nil, err
	}
	// 历史 MigrationReceipt 必须继续验证它当时的 binding tuple。版本新增 route
	// 必须尚未混入历史 binding；历史收据完成状态提升后，再由追加收据单调并入。
	inputs, err = prepareVersionRouteReceiptInputs(versionRoutes, evidenceBySink, inputs)
	if err != nil {
		return nil, err
	}
	manifest, err := loadMigrationReceiptManifest()
	if err != nil {
		return nil, err
	}
	inputs, err = applyMigrationReceiptManifestWithFS(
		manifest, migrationReceiptFS, evidenceBySink, inputs,
	)
	if err != nil {
		return nil, err
	}
	changeset3, err := loadChangeset3MigrationReceiptManifest()
	if err != nil {
		return nil, err
	}
	inputs, err = applyMigrationReceiptManifestWithFS(
		changeset3, migrationReceiptFS, evidenceBySink, inputs,
	)
	if err != nil {
		return nil, err
	}
	inputs, err = applyTransportReceiptTransitions(inputs)
	if err != nil {
		return nil, err
	}
	inputs, err = applyVersionRouteReceipts(versionRoutes, evidenceBySink, inputs)
	if err != nil {
		return nil, err
	}
	return bindMigrationReceiptTransports(inputs, DefaultReleaseCatalog())
}

// bindMigrationReceiptTransports 把历史收据中的 endpoint 证据重新绑定到每个
// ReleaseDigest 的可执行 transport。历史 transportID 继续原样保留作收据锚点；
// Guard 只接受 token 所属 ReleaseDigest 对应的 transport，禁止跨 Bundle 混用。
func bindMigrationReceiptTransports(
	inputs []SinkBindingInput,
	releaseCatalog ReleaseCatalog,
) ([]SinkBindingInput, error) {
	releases := make([]ResolvedCodexRelease, 0, 2)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := releaseCatalog.Resolve(mode)
		if err != nil {
			return nil, err
		}
		releases = append(releases, release)
	}
	for inputIndex := range inputs {
		input := &inputs[inputIndex]
		if input.Persona != PersonaCodexCLI || input.migrationReceipt == nil {
			continue
		}
		for claimIndex := range input.migrationReceipt.routeClaims {
			claim := &input.migrationReceipt.routeClaims[claimIndex]
			transportIDs := make(map[string]string, len(releases))
			for _, release := range releases {
				transportID := ""
				for _, endpoint := range release.ExecutableProfile().Endpoints() {
					if endpoint.ID == claim.evidenceID {
						transportID = endpoint.TransportID
						break
					}
				}
				if transportID == "" {
					// 版本新增端点只绑定实际包含它的画像；不存在该端点的 Release
					// 无法生成对应 Bundle/Token，因此不能伪造 transport 映射。
					continue
				}
				if existing := transportIDs[release.ReleaseDigest()]; existing != "" && existing != transportID {
					return nil, fmt.Errorf(
						"同一 ReleaseDigest 映射多个 transport：%s/%s",
						input.ID, claim.evidenceID,
					)
				}
				transportIDs[release.ReleaseDigest()] = transportID
			}
			if len(transportIDs) == 0 {
				return nil, fmt.Errorf(
					"MigrationReceipt endpoint 未进入任何 Active/Previous executable：%s/%s",
					input.ID, claim.evidenceID,
				)
			}
			claim.transportIDsByRelease = transportIDs
		}
	}
	return inputs, nil
}

func applyMigrationReceiptManifestWithFS(
	manifest migrationReceiptManifest,
	artifacts fs.FS,
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, error) {
	if err := receiptcontract.ValidateManifest(manifest); err != nil {
		return nil, err
	}
	if manifest.BootstrapCommit != BootstrapCommit {
		return nil, errors.New("MigrationReceipt bootstrap_commit 非法")
	}
	indexBySink := make(map[string]int, len(inputs))
	for index := range inputs {
		indexBySink[string(inputs[index].ID)] = index
	}
	for _, document := range manifest.Receipts {
		index, ok := indexBySink[document.SinkID]
		if !ok {
			return nil, fmt.Errorf("MigrationReceipt 引用未知 SinkID: %s", document.SinkID)
		}
		bindingDoc, ok := evidenceBySink[document.SinkID]
		if !ok {
			return nil, fmt.Errorf("MigrationReceipt 缺少绑定证据: %s", document.SinkID)
		}
		receipt, err := newMigrationReceipt(document, artifacts, inputs[index], bindingDoc)
		if err != nil {
			return nil, fmt.Errorf("MigrationReceipt %s: %w", document.SinkID, err)
		}
		// 状态升级只能由受审收据驱动。若同一 Sink 同时存在 canary 与 enforced
		// 历史，按清单顺序保留最终 enforced 收据，前序仍由共享契约验证。
		inputs[index].EnforcementState = receipt.approvedState
		inputs[index].migrationReceipt = &receipt
	}
	return inputs, nil
}

func newMigrationReceipt(
	document migrationReceiptDoc,
	artifacts fs.FS,
	input SinkBindingInput,
	bindingDoc bindingcontract.ReleaseBindingDoc,
) (MigrationReceipt, error) {
	state := SinkEnforcementState(document.ApprovedState)
	if SinkID(document.SinkID) != input.ID ||
		(state != SinkStateCanaryEnforce && state != SinkStateEnforced) {
		return MigrationReceipt{}, errors.New("SinkID 或状态不一致")
	}
	if err := validateMigrationAuthority(input, document.AuthorityKind); err != nil {
		return MigrationReceipt{}, err
	}
	if err := receiptcontract.VerifyArtifacts(artifacts, document); err != nil {
		return MigrationReceipt{}, err
	}
	bindingDigest, err := sinkBindingIdentityDigest(input)
	if err != nil || bindingDigest != document.BindingDigest {
		return MigrationReceipt{}, errors.New("binding digest 不匹配")
	}
	if !migrationCandidatesMatch(document.Candidates, bindingDoc.Candidates) {
		return MigrationReceipt{}, errors.New("源码候选证据不匹配")
	}

	expectedAdapter := map[BackendKind]AdapterID{
		BackendHTTPUpstream: AdapterHTTPUpstream,
		BackendReqProfile:   AdapterReqProfile,
		BackendWebSocket:    AdapterWebSocket,
	}[input.TargetBackend]
	if expectedAdapter == "" || len(document.Routes) != len(input.Routes) {
		return MigrationReceipt{}, errors.New("route proof 未覆盖完整 binding")
	}

	inputRoutes := make(map[string]CatalogRoute, len(input.Routes))
	for _, route := range input.Routes {
		inputRoutes[catalogRouteIdentity(route)] = route
	}
	claims := make([]migrationRouteClaim, 0, len(document.Routes))
	for _, proof := range document.Routes {
		route := CatalogRoute{Key: RouteKey{
			Method: proof.Route.Method, Host: proof.Route.Host,
			Path: proof.Route.Path, Purpose: Purpose(proof.Route.Purpose),
		}, Protocol: WireProtocol(proof.Route.Protocol)}
		registered, ok := inputRoutes[catalogRouteIdentity(route)]
		if !ok || registered != route || BackendKind(proof.Backend) != input.TargetBackend ||
			AdapterID(proof.AdapterID) != expectedAdapter {
			return MigrationReceipt{}, fmt.Errorf("route proof 与 binding/backend 不一致: %s", proof.Route.Identity())
		}
		if err := validateMigrationRouteEvidence(input, route, proof); err != nil {
			return MigrationReceipt{}, err
		}
		claims = append(claims, migrationRouteClaim{
			route: route, evidenceKind: proof.EvidenceKind, evidenceID: proof.EvidenceID,
			backend: BackendKind(proof.Backend), adapterID: AdapterID(proof.AdapterID),
			transportID: proof.TransportID,
		})
	}
	sort.Slice(claims, func(i, j int) bool {
		return catalogRouteIdentity(claims[i].route) < catalogRouteIdentity(claims[j].route)
	})
	digest, err := receiptcontract.DigestDocument(document)
	if err != nil {
		return MigrationReceipt{}, err
	}
	return MigrationReceipt{
		digest: digest, sinkID: input.ID, approvedState: state,
		bindingDigest: document.BindingDigest, authorityKind: document.AuthorityKind,
		authorityID: ExecutorID(document.AuthorityID), tokenIssuerID: ExecutorID(document.TokenIssuerID),
		routeClaims: claims,
	}, nil
}

func validateMigrationAuthority(input SinkBindingInput, authority receiptcontract.AuthorityKind) error {
	switch input.Persona {
	case PersonaCodexCLI:
		descriptor := codexPersonaDescriptorInput()
		if input.EndpointEvidence != EndpointEvidenceCodexProfile ||
			authority != descriptor.AuthorityKind {
			return errors.New("codex-cli 升级必须使用 codex_profile + Codex Executor 收据")
		}
	case PersonaClaudeCode:
		if input.EndpointEvidence != EndpointEvidenceClaudeProfile ||
			authority != receiptcontract.AuthorityClaudeExecutor {
			return errors.New("claude-code candidate 必须使用 claude_profile + Claude Executor 收据")
		}
	case PersonaChatGPTWeb:
		if input.EndpointEvidence != EndpointEvidenceExternalPersona ||
			authority != receiptcontract.AuthorityChatGPTWebClient {
			return errors.New("chatgpt-web 升级必须使用 external_persona + Browser Client 收据")
		}
	default:
		return errors.New("unclassified/dead persona 禁止升级")
	}
	return nil
}

func validateMigrationRouteEvidence(
	input SinkBindingInput,
	route CatalogRoute,
	proof receiptcontract.RouteProof,
) error {
	switch input.Persona {
	case PersonaCodexCLI:
		binding, ok := resolveReceiptEndpointBinding(input, route)
		if !ok || proof.EvidenceKind != "codex_endpoint" || proof.EvidenceID != binding.EndpointID() {
			return fmt.Errorf("Codex route 缺少匹配的 ProfileSpec endpoint: %s", route.Key)
		}
	case PersonaChatGPTWeb:
		if proof.EvidenceKind != "browser_profile" || strings.TrimSpace(proof.EvidenceID) == "" {
			return fmt.Errorf("chatgpt-web route 缺少 BrowserProfile 证据: %s", route.Key)
		}
	case PersonaClaudeCode:
		if proof.EvidenceKind != "claude_endpoint" || strings.TrimSpace(proof.EvidenceID) == "" {
			return fmt.Errorf("Claude route 缺少画像 endpoint 证据: %s", route.Key)
		}
	}
	return nil
}

func resolveReceiptEndpointBinding(
	input SinkBindingInput,
	route CatalogRoute,
) (EndpointBinding, bool) {
	// Receipt 验证发生在状态升级之前。用同一份静态 binding tuple 构造
	// legacy_observe 视图，避免 EndpointBinding 与 MigrationReceipt 形成构造循环。
	input.EnforcementState = SinkStateLegacyObserve
	input.migrationReceipt = nil
	input.Override = nil
	sinks, err := NewSinkCatalog([]SinkBindingInput{input})
	if err != nil {
		return EndpointBinding{}, false
	}
	physical, err := NewPhysicalRouteCatalog(sinks)
	if err != nil {
		return EndpointBinding{}, false
	}
	sink, ok := sinks.Resolve(input.ID)
	if !ok {
		return EndpointBinding{}, false
	}
	var resolved EndpointBinding
	found := false
	seenProfiles := make(map[string]bool)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil || seenProfiles[release.ProfileDigest()] {
			continue
		}
		seenProfiles[release.ProfileDigest()] = true
		bindings, err := NewEndpointBindingCatalog(sinks, physical, release.ExecutableProfile())
		if err != nil {
			return EndpointBinding{}, false
		}
		candidate, ok := bindings.ResolveBindingRoute(sink, route, physical)
		if !ok {
			continue
		}
		if found && (candidate.EndpointID() != resolved.EndpointID() ||
			candidate.ReleasePurpose() != resolved.ReleasePurpose()) {
			return EndpointBinding{}, false
		}
		resolved = candidate
		found = true
	}
	return resolved, found
}

func (r MigrationReceipt) validFor(input SinkBindingInput) bool {
	if r.digest == "" || r.sinkID != input.ID || r.approvedState != input.EnforcementState ||
		len(r.routeClaims) != len(input.Routes) || validateMigrationAuthority(input, r.authorityKind) != nil {
		return false
	}
	digest, err := sinkBindingIdentityDigest(input)
	if err != nil || digest != r.bindingDigest {
		return false
	}
	for _, route := range input.Routes {
		if _, ok := r.claimFor(route); !ok {
			return false
		}
	}
	return true
}

func (r MigrationReceipt) claimFor(route CatalogRoute) (migrationRouteClaim, bool) {
	identity := catalogRouteIdentity(route)
	for _, claim := range r.routeClaims {
		if catalogRouteIdentity(claim.route) == identity {
			return claim, true
		}
	}
	return migrationRouteClaim{}, false
}

func sinkBindingIdentityDigest(input SinkBindingInput) (string, error) {
	routes := append([]CatalogRoute(nil), input.Routes...)
	sort.Slice(routes, func(i, j int) bool {
		return catalogRouteIdentity(routes[i]) < catalogRouteIdentity(routes[j])
	})
	backends := append([]BackendKind(nil), input.LegacyBackends...)
	sort.Slice(backends, func(i, j int) bool { return backends[i] < backends[j] })
	payload := struct {
		ID                 SinkID
		Purpose            Purpose
		Persona            Persona
		EndpointEvidence   EndpointEvidence
		Routes             []CatalogRoute
		TargetBackend      BackendKind
		LegacyBackends     []BackendKind
		Owner              string
		MigrationChangeset string
		ExpiryCondition    string
		RuntimeBindable    bool
	}{
		ID: input.ID, Purpose: input.Purpose, Persona: input.Persona,
		EndpointEvidence: input.EndpointEvidence, Routes: routes,
		TargetBackend: input.TargetBackend, LegacyBackends: backends,
		Owner: strings.TrimSpace(input.Owner), MigrationChangeset: strings.TrimSpace(input.MigrationChangeset),
		ExpiryCondition: strings.TrimSpace(input.ExpiryCondition), RuntimeBindable: input.RuntimeBindable,
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func migrationCandidatesMatch(
	want []migrationCandidateEvidence,
	actual []bindingcontract.BindingCandidateDoc,
) bool {
	if len(want) != len(actual) {
		return false
	}
	actualByID := make(map[string]string, len(actual))
	for _, candidate := range actual {
		actualByID[candidate.ScanCandidateID] = candidate.ASTFingerprint
	}
	previous := ""
	for _, candidate := range want {
		if previous >= candidate.ScanCandidateID ||
			actualByID[candidate.ScanCandidateID] != candidate.ASTFingerprint {
			return false
		}
		previous = candidate.ScanCandidateID
	}
	return true
}
