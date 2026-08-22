package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
)

// ProductionReleaseRole 是共享 Runtime Selector 唯一允许表达的可变生产角色。
// Validation candidate 使用独立不可变引用，不能占用本类型的 rollback 角色。
type ProductionReleaseRole string

const (
	ProductionReleaseActive   ProductionReleaseRole = "production_active"
	ProductionReleaseRollback ProductionReleaseRole = "production_rollback"
)

func (r ProductionReleaseRole) Valid() bool {
	return r == ProductionReleaseActive || r == ProductionReleaseRollback
}

func codexModeForProductionRole(role ProductionReleaseRole) (ReleaseMode, error) {
	switch role {
	case ProductionReleaseActive:
		return ReleaseModeActive, nil
	case ProductionReleaseRollback:
		return ReleaseModePrevious, nil
	default:
		return "", errors.New("ProductionReleaseRole 非法")
	}
}

func productionRoleForCodexMode(mode ReleaseMode) (ProductionReleaseRole, error) {
	switch mode {
	case ReleaseModeActive:
		return ProductionReleaseActive, nil
	case ReleaseModePrevious:
		return ProductionReleaseRollback, nil
	default:
		return "", errors.New("Codex ReleaseMode 非法")
	}
}

// ResolvedPersonaRelease 是共享控制层可见的最小发布坐标。
// 具体 ProfileSchema、节点和编译产物仍由各 Persona 的发布源持有。
type ResolvedPersonaRelease struct {
	persona       Persona
	role          ProductionReleaseRole
	version       string
	releaseDigest string
	profileDigest string
}

type ProductionSelectionKind string

const (
	ProductionSelectionRelease               ProductionSelectionKind = "release"
	ProductionSelectionOperationalDeployment ProductionSelectionKind = "operational_deployment"
)

func (k ProductionSelectionKind) Valid() bool {
	return k == ProductionSelectionRelease || k == ProductionSelectionOperationalDeployment
}

// ResolvedPersonaOperationalDeployment 表示只能通过部署系统执行的回退点。
// 它不含 Profile／Release 摘要，因此不能进入 final wire 编译链。
type ResolvedPersonaOperationalDeployment struct {
	persona       Persona
	role          ProductionReleaseRole
	revision      string
	imageDigest   string
	receiptPath   string
	receiptDigest string
}

func (r ResolvedPersonaOperationalDeployment) Persona() Persona            { return r.persona }
func (r ResolvedPersonaOperationalDeployment) Role() ProductionReleaseRole { return r.role }
func (r ResolvedPersonaOperationalDeployment) Revision() string            { return r.revision }
func (r ResolvedPersonaOperationalDeployment) ImageDigest() string         { return r.imageDigest }
func (r ResolvedPersonaOperationalDeployment) ReceiptPath() string         { return r.receiptPath }
func (r ResolvedPersonaOperationalDeployment) ReceiptDigest() string       { return r.receiptDigest }

func (r ResolvedPersonaOperationalDeployment) validate() error {
	if !r.persona.Valid() || r.role != ProductionReleaseRollback ||
		strings.TrimSpace(r.revision) == "" || strings.TrimSpace(r.imageDigest) == "" ||
		strings.TrimSpace(r.receiptPath) == "" || !receiptSHA256(r.receiptDigest) {
		return errors.New("ResolvedPersonaOperationalDeployment 坐标不完整")
	}
	return nil
}

// ResolvedPersonaSelection 是共享 selector 的带类型结果。
// Release 与 operational deployment 两种载荷严格互斥。
type ResolvedPersonaSelection struct {
	persona    Persona
	role       ProductionReleaseRole
	kind       ProductionSelectionKind
	release    ResolvedPersonaRelease
	deployment ResolvedPersonaOperationalDeployment
}

func (s ResolvedPersonaSelection) Persona() Persona              { return s.persona }
func (s ResolvedPersonaSelection) Role() ProductionReleaseRole   { return s.role }
func (s ResolvedPersonaSelection) Kind() ProductionSelectionKind { return s.kind }
func (s ResolvedPersonaSelection) Release() (ResolvedPersonaRelease, bool) {
	return s.release, s.kind == ProductionSelectionRelease
}
func (s ResolvedPersonaSelection) OperationalDeployment() (
	ResolvedPersonaOperationalDeployment,
	bool,
) {
	return s.deployment, s.kind == ProductionSelectionOperationalDeployment
}

func (s ResolvedPersonaSelection) validate() error {
	if !s.persona.Valid() || !s.role.Valid() || !s.kind.Valid() {
		return errors.New("ResolvedPersonaSelection 身份非法")
	}
	switch s.kind {
	case ProductionSelectionRelease:
		if err := s.release.validate(); err != nil || s.release.persona != s.persona ||
			s.release.role != s.role {
			return errors.New("ResolvedPersonaSelection Release 载荷非法")
		}
	case ProductionSelectionOperationalDeployment:
		if err := s.deployment.validate(); err != nil || s.deployment.persona != s.persona ||
			s.deployment.role != s.role {
			return errors.New("ResolvedPersonaSelection deployment 载荷非法")
		}
	}
	return nil
}

func (r ResolvedPersonaRelease) Persona() Persona            { return r.persona }
func (r ResolvedPersonaRelease) Role() ProductionReleaseRole { return r.role }
func (r ResolvedPersonaRelease) Version() string             { return r.version }
func (r ResolvedPersonaRelease) ReleaseDigest() string       { return r.releaseDigest }
func (r ResolvedPersonaRelease) ProfileDigest() string       { return r.profileDigest }

func (r ResolvedPersonaRelease) validate() error {
	if !r.persona.Valid() || !r.role.Valid() || strings.TrimSpace(r.version) == "" ||
		!receiptSHA256(r.releaseDigest) || !receiptSHA256(r.profileDigest) {
		return errors.New("ResolvedPersonaRelease 坐标不完整")
	}
	return nil
}

// PersonaReleaseSource 只把 Persona 自有发布目录投影为共享坐标。
// 它不能把 ProfileSchema 或 wire 字段暴露给其他 Persona。
type PersonaReleaseSource interface {
	Persona() Persona
	ResolvePersonaRelease(role ProductionReleaseRole) (ResolvedPersonaRelease, error)
}

// PersonaOperationalRollbackSource 只用于尚无第二个严格 Release、但已有真实
// 回退部署的 Persona。实现该接口后，rollback 槽位不得再伪造 Release。
type PersonaOperationalRollbackSource interface {
	ResolvePersonaOperationalRollback() (ResolvedPersonaOperationalDeployment, error)
}

type codexPersonaReleaseSource struct {
	catalog ReleaseCatalog
}

func NewCodexPersonaReleaseSource(catalog ReleaseCatalog) PersonaReleaseSource {
	return codexPersonaReleaseSource{catalog: catalog}
}

func (codexPersonaReleaseSource) Persona() Persona { return PersonaCodexCLI }

func (s codexPersonaReleaseSource) ResolvePersonaRelease(
	role ProductionReleaseRole,
) (ResolvedPersonaRelease, error) {
	mode, err := codexModeForProductionRole(role)
	if err != nil {
		return ResolvedPersonaRelease{}, err
	}
	release, err := s.catalog.Resolve(mode)
	if err != nil {
		return ResolvedPersonaRelease{}, err
	}
	return ResolvedPersonaRelease{
		persona: PersonaCodexCLI, role: role, version: release.Version(),
		releaseDigest: release.ReleaseDigest(), profileDigest: release.ProfileDigest(),
	}, nil
}

type claudePersonaReleaseSource struct {
	catalog ClaudeReleaseCatalog
}

func NewClaudePersonaReleaseSource(catalog ClaudeReleaseCatalog) PersonaReleaseSource {
	return claudePersonaReleaseSource{catalog: catalog}
}

func (claudePersonaReleaseSource) Persona() Persona { return PersonaClaudeCode }

func (s claudePersonaReleaseSource) ResolvePersonaRelease(
	role ProductionReleaseRole,
) (ResolvedPersonaRelease, error) {
	if role != ProductionReleaseActive {
		return ResolvedPersonaRelease{}, errors.New("Claude rollback 是操作部署，不是 strict Release")
	}
	release := s.catalog.ProductionActive().Release()
	return ResolvedPersonaRelease{
		persona: PersonaClaudeCode, role: role, version: release.Version(),
		releaseDigest: release.ReleaseDigest(), profileDigest: release.ProfileDigest(),
	}, nil
}

func (s claudePersonaReleaseSource) ResolvePersonaOperationalRollback() (
	ResolvedPersonaOperationalDeployment,
	error,
) {
	rollback := s.catalog.ProductionRollback()
	return ResolvedPersonaOperationalDeployment{
		persona: PersonaClaudeCode, role: ProductionReleaseRollback,
		revision: rollback.Commit(), imageDigest: rollback.ImageDigest(),
		receiptPath: rollback.ReceiptPath(), receiptDigest: rollback.ReceiptDigest(),
	}, nil
}

// PersonaReleasePair 是同一 Persona 的 production active/rollback 对。
type PersonaReleasePair struct {
	Active   ResolvedPersonaRelease
	Rollback ResolvedPersonaRelease
}

// PersonaReleaseCatalog 在启动期预解析所有已登记 Persona 的 production active/rollback。
// 请求期只做固定槽位查表，自动发现版本和入站自报版本均没有写入口。
type PersonaReleaseCatalog struct {
	personas PersonaRegistry
	resolved map[Persona]map[ProductionReleaseRole]ResolvedPersonaSelection
	digest   string
}

func NewPersonaReleaseCatalog(
	personas PersonaRegistry,
	sources []PersonaReleaseSource,
) (PersonaReleaseCatalog, error) {
	if personas.Digest() == "" || len(sources) == 0 {
		return PersonaReleaseCatalog{}, errors.New("PersonaReleaseCatalog 输入为空")
	}
	byPersona := make(map[Persona]PersonaReleaseSource, len(sources))
	for _, source := range sources {
		if source == nil {
			return PersonaReleaseCatalog{}, errors.New("PersonaReleaseCatalog 存在空发布源")
		}
		persona := source.Persona()
		if _, ok := personas.Resolve(persona); !ok {
			return PersonaReleaseCatalog{}, fmt.Errorf("发布源 Persona 未登记: %s", persona)
		}
		if _, duplicate := byPersona[persona]; duplicate {
			return PersonaReleaseCatalog{}, fmt.Errorf("Persona 发布源重复: %s", persona)
		}
		byPersona[persona] = source
	}
	for persona := range personas.byPersona {
		if byPersona[persona] == nil {
			return PersonaReleaseCatalog{}, fmt.Errorf("Persona 缺少发布源: %s", persona)
		}
	}

	catalog := PersonaReleaseCatalog{
		personas: personas,
		resolved: make(map[Persona]map[ProductionReleaseRole]ResolvedPersonaSelection, len(byPersona)),
	}
	for persona, source := range byPersona {
		roles := make(map[ProductionReleaseRole]ResolvedPersonaSelection, 2)
		active, err := source.ResolvePersonaRelease(ProductionReleaseActive)
		if err != nil {
			return PersonaReleaseCatalog{}, fmt.Errorf(
				"解析 Persona %s/%s 发布坐标: %w", persona, ProductionReleaseActive, err,
			)
		}
		roles[ProductionReleaseActive] = ResolvedPersonaSelection{
			persona: persona, role: ProductionReleaseActive,
			kind: ProductionSelectionRelease, release: active,
		}
		if operationalSource, ok := source.(PersonaOperationalRollbackSource); ok {
			deployment, resolveErr := operationalSource.ResolvePersonaOperationalRollback()
			if resolveErr != nil {
				return PersonaReleaseCatalog{}, fmt.Errorf(
					"解析 Persona %s/%s 操作回退: %w",
					persona, ProductionReleaseRollback, resolveErr,
				)
			}
			roles[ProductionReleaseRollback] = ResolvedPersonaSelection{
				persona: persona, role: ProductionReleaseRollback,
				kind: ProductionSelectionOperationalDeployment, deployment: deployment,
			}
		} else {
			rollback, resolveErr := source.ResolvePersonaRelease(ProductionReleaseRollback)
			if resolveErr != nil {
				return PersonaReleaseCatalog{}, fmt.Errorf(
					"解析 Persona %s/%s 发布坐标: %w",
					persona, ProductionReleaseRollback, resolveErr,
				)
			}
			roles[ProductionReleaseRollback] = ResolvedPersonaSelection{
				persona: persona, role: ProductionReleaseRollback,
				kind: ProductionSelectionRelease, release: rollback,
			}
		}
		for _, role := range []ProductionReleaseRole{ProductionReleaseActive, ProductionReleaseRollback} {
			if err := roles[role].validate(); err != nil {
				return PersonaReleaseCatalog{}, err
			}
		}
		activeRelease, _ := roles[ProductionReleaseActive].Release()
		rollbackRelease, hasRollbackRelease := roles[ProductionReleaseRollback].Release()
		if hasRollbackRelease && activeRelease.releaseDigest == rollbackRelease.releaseDigest {
			return PersonaReleaseCatalog{}, fmt.Errorf("Persona %s 的 production active/rollback 被折叠", persona)
		}
		catalog.resolved[persona] = roles
	}
	digest, err := digestPersonaReleaseCatalog(catalog)
	if err != nil {
		return PersonaReleaseCatalog{}, err
	}
	catalog.digest = digest
	return catalog, nil
}

func digestPersonaReleaseCatalog(catalog PersonaReleaseCatalog) (string, error) {
	personas := make([]string, 0, len(catalog.resolved))
	for persona := range catalog.resolved {
		personas = append(personas, string(persona))
	}
	sort.Strings(personas)
	type coordinate struct {
		Persona       Persona                 `json:"persona"`
		Role          ProductionReleaseRole   `json:"role"`
		Kind          ProductionSelectionKind `json:"kind"`
		Version       string                  `json:"version"`
		ReleaseDigest string                  `json:"release_digest"`
		ProfileDigest string                  `json:"profile_digest"`
		Revision      string                  `json:"revision"`
		ImageDigest   string                  `json:"image_digest"`
		ReceiptPath   string                  `json:"receipt_path"`
		ReceiptDigest string                  `json:"receipt_digest"`
	}
	payload := make([]coordinate, 0, len(personas)*2)
	for _, value := range personas {
		persona := Persona(value)
		for _, role := range []ProductionReleaseRole{ProductionReleaseActive, ProductionReleaseRollback} {
			selection := catalog.resolved[persona][role]
			item := coordinate{Persona: persona, Role: role, Kind: selection.kind}
			if release, ok := selection.Release(); ok {
				item.Version = release.version
				item.ReleaseDigest = release.releaseDigest
				item.ProfileDigest = release.profileDigest
			} else if deployment, ok := selection.OperationalDeployment(); ok {
				item.Revision = deployment.revision
				item.ImageDigest = deployment.imageDigest
				item.ReceiptPath = deployment.receiptPath
				item.ReceiptDigest = deployment.receiptDigest
			}
			payload = append(payload, item)
		}
	}
	raw, err := json.Marshal(struct {
		PersonaRegistryDigest string       `json:"persona_registry_digest"`
		Coordinates           []coordinate `json:"coordinates"`
	}{catalog.personas.Digest(), payload})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func (c PersonaReleaseCatalog) Resolve(
	persona Persona,
	role ProductionReleaseRole,
) (ResolvedPersonaRelease, error) {
	selection, err := c.ResolveSelection(persona, role)
	if err != nil {
		return ResolvedPersonaRelease{}, err
	}
	release, ok := selection.Release()
	if !ok {
		return ResolvedPersonaRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			fmt.Errorf("Persona %s/%s 是操作回退，不是 Release", persona, role),
		)
	}
	return release, nil
}

func (c PersonaReleaseCatalog) ResolveSelection(
	persona Persona,
	role ProductionReleaseRole,
) (ResolvedPersonaSelection, error) {
	if !role.Valid() {
		return ResolvedPersonaSelection{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			errors.New("ProductionReleaseRole 非法"),
		)
	}
	if _, ok := c.personas.Resolve(persona); !ok {
		return ResolvedPersonaSelection{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			fmt.Errorf("Persona 未登记: %s", persona),
		)
	}
	selection, ok := c.resolved[persona][role]
	if !ok {
		return ResolvedPersonaSelection{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			fmt.Errorf("Persona %s 缺少 %s 发布坐标", persona, role),
		)
	}
	return selection, nil
}

func (c PersonaReleaseCatalog) OperationalRollback(
	persona Persona,
) (ResolvedPersonaOperationalDeployment, error) {
	selection, err := c.ResolveSelection(persona, ProductionReleaseRollback)
	if err != nil {
		return ResolvedPersonaOperationalDeployment{}, err
	}
	rollback, ok := selection.OperationalDeployment()
	if !ok {
		return ResolvedPersonaOperationalDeployment{}, errors.New("Persona rollback 是 Release，不是操作部署")
	}
	return rollback, nil
}

func (c PersonaReleaseCatalog) RollbackPair(persona Persona) (PersonaReleasePair, error) {
	active, err := c.Resolve(persona, ProductionReleaseActive)
	if err != nil {
		return PersonaReleasePair{}, err
	}
	rollback, err := c.Resolve(persona, ProductionReleaseRollback)
	if err != nil {
		return PersonaReleasePair{}, err
	}
	if active.ReleaseDigest() == rollback.ReleaseDigest() {
		return PersonaReleasePair{}, errors.New("production active/rollback 回滚对被折叠")
	}
	return PersonaReleasePair{Active: active, Rollback: rollback}, nil
}

// ResolveCodexMode 只为现有 Codex active/previous facade 提供兼容映射。隔离候选
// Catalog 可以继续用 previous 承载 candidate，但该角色不会进入共享生产合同。
func (c PersonaReleaseCatalog) ResolveCodexMode(
	persona Persona,
	mode ReleaseMode,
) (ResolvedPersonaRelease, error) {
	role, err := productionRoleForCodexMode(mode)
	if err != nil {
		return ResolvedPersonaRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed, "persona_catalog.resolve_codex_mode", err,
		)
	}
	return c.Resolve(persona, role)
}

func (c PersonaReleaseCatalog) PersonaRegistryDigest() string {
	return c.personas.Digest()
}

func (c PersonaReleaseCatalog) Digest() string { return c.digest }

var loadDefaultPersonaReleaseCatalog = sync.OnceValues(func() (PersonaReleaseCatalog, error) {
	return NewPersonaReleaseCatalog(
		DefaultPersonaRegistry(),
		[]PersonaReleaseSource{NewCodexPersonaReleaseSource(DefaultReleaseCatalog())},
	)
})

func DefaultPersonaReleaseCatalog() PersonaReleaseCatalog {
	catalog, err := loadDefaultPersonaReleaseCatalog()
	if err != nil {
		panic(err)
	}
	return catalog
}

// NewProductionPersonaReleaseCatalog 把 Claude production active 与真实操作回退
// 接入和 Codex 相同的共享 Runtime Selector 合同。
func NewProductionPersonaReleaseCatalog(sinks SinkCatalog) (PersonaReleaseCatalog, error) {
	personas, err := NewProductionPersonaRegistry(sinks)
	if err != nil {
		return PersonaReleaseCatalog{}, err
	}
	return NewPersonaReleaseCatalog(personas, []PersonaReleaseSource{
		NewCodexPersonaReleaseSource(DefaultReleaseCatalog()),
		NewClaudePersonaReleaseSource(DefaultClaudeReleaseCatalog()),
	})
}
