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

// PersonaReleasePair 是同一 Persona 的 production active/rollback 对。
type PersonaReleasePair struct {
	Active   ResolvedPersonaRelease
	Rollback ResolvedPersonaRelease
}

// PersonaReleaseCatalog 在启动期预解析所有已登记 Persona 的 production active/rollback。
// 请求期只做固定槽位查表，自动发现版本和入站自报版本均没有写入口。
type PersonaReleaseCatalog struct {
	personas PersonaRegistry
	resolved map[Persona]map[ProductionReleaseRole]ResolvedPersonaRelease
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
		resolved: make(map[Persona]map[ProductionReleaseRole]ResolvedPersonaRelease, len(byPersona)),
	}
	for persona, source := range byPersona {
		roles := make(map[ProductionReleaseRole]ResolvedPersonaRelease, 2)
		for _, role := range []ProductionReleaseRole{ProductionReleaseActive, ProductionReleaseRollback} {
			release, err := source.ResolvePersonaRelease(role)
			if err != nil {
				return PersonaReleaseCatalog{}, fmt.Errorf(
					"解析 Persona %s/%s 发布坐标: %w", persona, role, err,
				)
			}
			if err := release.validate(); err != nil {
				return PersonaReleaseCatalog{}, err
			}
			if release.persona != persona || release.role != role {
				return PersonaReleaseCatalog{}, errors.New("发布源返回了错误 Persona/production role")
			}
			roles[role] = release
		}
		if roles[ProductionReleaseActive].releaseDigest == roles[ProductionReleaseRollback].releaseDigest {
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
		Persona       Persona               `json:"persona"`
		Role          ProductionReleaseRole `json:"role"`
		Version       string                `json:"version"`
		ReleaseDigest string                `json:"release_digest"`
		ProfileDigest string                `json:"profile_digest"`
	}
	payload := make([]coordinate, 0, len(personas)*2)
	for _, value := range personas {
		persona := Persona(value)
		for _, role := range []ProductionReleaseRole{ProductionReleaseActive, ProductionReleaseRollback} {
			release := catalog.resolved[persona][role]
			payload = append(payload, coordinate{
				release.persona, release.role, release.version,
				release.releaseDigest, release.profileDigest,
			})
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
	if !role.Valid() {
		return ResolvedPersonaRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			errors.New("ProductionReleaseRole 非法"),
		)
	}
	if _, ok := c.personas.Resolve(persona); !ok {
		return ResolvedPersonaRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			fmt.Errorf("Persona 未登记: %s", persona),
		)
	}
	release, ok := c.resolved[persona][role]
	if !ok {
		return ResolvedPersonaRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"persona_catalog.resolve",
			fmt.Errorf("Persona %s 缺少 %s 发布坐标", persona, role),
		)
	}
	return release, nil
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
