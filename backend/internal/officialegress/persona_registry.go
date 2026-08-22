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

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

// PersonaIdentity 是官方 OAuth 客户端的稳定四元组。它只接受受信账号路由事实，
// 不读取入站 User-Agent、客户端自报版本或任意同名 Header。
type PersonaIdentity struct {
	Provider            string `json:"provider"`
	OfficialProduct     string `json:"official_product"`
	AuthFamily          string `json:"auth_family"`
	UpstreamRouteFamily string `json:"upstream_route_family"`
}

func (i PersonaIdentity) normalize() PersonaIdentity {
	return PersonaIdentity{
		Provider:            strings.ToLower(strings.TrimSpace(i.Provider)),
		OfficialProduct:     strings.ToLower(strings.TrimSpace(i.OfficialProduct)),
		AuthFamily:          strings.ToLower(strings.TrimSpace(i.AuthFamily)),
		UpstreamRouteFamily: strings.ToLower(strings.TrimSpace(i.UpstreamRouteFamily)),
	}
}

func (i PersonaIdentity) validate() error {
	i = i.normalize()
	if i.Provider == "" || i.OfficialProduct == "" || i.AuthFamily == "" ||
		i.UpstreamRouteFamily == "" {
		return errors.New("PersonaIdentity 字段不完整")
	}
	return nil
}

func (i PersonaIdentity) key() string {
	i = i.normalize()
	return strings.Join([]string{
		i.Provider, i.OfficialProduct, i.AuthFamily, i.UpstreamRouteFamily,
	}, "\x00")
}

type PersonaExclusionDimension string

const (
	PersonaExclusionAuthFamily      PersonaExclusionDimension = "auth_family"
	PersonaExclusionOfficialProduct PersonaExclusionDimension = "official_product"
	PersonaExclusionSinkID          PersonaExclusionDimension = "sink_id"
)

// PersonaExclusion 记录明确不属于该 Persona 执行链的相邻路径。
type PersonaExclusion struct {
	Dimension PersonaExclusionDimension `json:"dimension"`
	Value     string                    `json:"value"`
	Reason    string                    `json:"reason"`
}

func (e PersonaExclusion) normalize() PersonaExclusion {
	e.Value = strings.ToLower(strings.TrimSpace(e.Value))
	e.Reason = strings.TrimSpace(e.Reason)
	return e
}

func (e PersonaExclusion) validate() error {
	e = e.normalize()
	switch e.Dimension {
	case PersonaExclusionAuthFamily, PersonaExclusionOfficialProduct, PersonaExclusionSinkID:
	default:
		return errors.New("PersonaExclusion dimension 非法")
	}
	if e.Value == "" || e.Reason == "" {
		return errors.New("PersonaExclusion 字段不完整")
	}
	return nil
}

// PersonaRouteBinding 是 Persona Registry 冻结的 Sink/route 闭集。
type PersonaRouteBinding struct {
	SinkID   SinkID           `json:"sink_id"`
	Purpose  Purpose          `json:"purpose"`
	Route    RouteKey         `json:"route"`
	Protocol WireProtocol     `json:"protocol"`
	Backend  BackendKind      `json:"backend"`
	Evidence EndpointEvidence `json:"endpoint_evidence"`
}

func (b PersonaRouteBinding) key() string {
	return strings.Join([]string{
		string(b.SinkID), string(b.Purpose), b.Route.String(), string(b.Protocol),
		string(b.Backend), string(b.Evidence),
	}, "\x00")
}

// PersonaDescriptorInput 只描述经审核的 Persona 身份和允许使用的证据类别。
// 具体 Sink/route 闭集始终从同一份 SinkCatalog 生成，避免形成第二个路由事实源。
type PersonaDescriptorInput struct {
	Persona                 Persona
	Identity                PersonaIdentity
	AuthorityKind           receiptcontract.AuthorityKind
	AllowedEndpointEvidence []EndpointEvidence
	Exclusions              []PersonaExclusion
}

// PersonaDescriptor 是构造后不可变的 Persona 登记项。
type PersonaDescriptor struct {
	persona         Persona
	identity        PersonaIdentity
	authorityKind   receiptcontract.AuthorityKind
	allowedEvidence []EndpointEvidence
	exclusions      []PersonaExclusion
	routes          []PersonaRouteBinding
	digest          string
}

func (d PersonaDescriptor) Persona() Persona                             { return d.persona }
func (d PersonaDescriptor) Identity() PersonaIdentity                    { return d.identity }
func (d PersonaDescriptor) AuthorityKind() receiptcontract.AuthorityKind { return d.authorityKind }
func (d PersonaDescriptor) Digest() string                               { return d.digest }

func (d PersonaDescriptor) AllowedEndpointEvidence() []EndpointEvidence {
	return append([]EndpointEvidence(nil), d.allowedEvidence...)
}

func (d PersonaDescriptor) Exclusions() []PersonaExclusion {
	return append([]PersonaExclusion(nil), d.exclusions...)
}

func (d PersonaDescriptor) RouteBindings() []PersonaRouteBinding {
	return append([]PersonaRouteBinding(nil), d.routes...)
}

func (d PersonaDescriptor) authorize(
	sinkID SinkID,
	purpose Purpose,
	route RouteKey,
	protocol WireProtocol,
) bool {
	for _, binding := range d.routes {
		if binding.SinkID == sinkID && binding.Purpose == purpose && binding.Route == route &&
			binding.Protocol == protocol {
			return true
		}
	}
	return false
}

func (d PersonaDescriptor) excludes(dimension PersonaExclusionDimension, value string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	for _, exclusion := range d.exclusions {
		if exclusion.Dimension == dimension && exclusion.Value == value {
			return true
		}
	}
	return false
}

// PersonaRegistry 是进程级不可变 Persona 闭集。
type PersonaRegistry struct {
	byPersona  map[Persona]PersonaDescriptor
	byIdentity map[string]Persona
	digest     string
}

func NewPersonaRegistry(
	inputs []PersonaDescriptorInput,
	sinks SinkCatalog,
) (PersonaRegistry, error) {
	if len(inputs) == 0 {
		return PersonaRegistry{}, errors.New("PersonaRegistry 为空")
	}
	registry := PersonaRegistry{
		byPersona:  make(map[Persona]PersonaDescriptor, len(inputs)),
		byIdentity: make(map[string]Persona, len(inputs)),
	}
	for _, input := range inputs {
		descriptor, err := buildPersonaDescriptor(input, sinks)
		if err != nil {
			return PersonaRegistry{}, err
		}
		if _, duplicate := registry.byPersona[descriptor.persona]; duplicate {
			return PersonaRegistry{}, fmt.Errorf("Persona 重复登记: %s", descriptor.persona)
		}
		identityKey := descriptor.identity.key()
		if existing, duplicate := registry.byIdentity[identityKey]; duplicate {
			return PersonaRegistry{}, fmt.Errorf(
				"PersonaIdentity 重复登记: %s 与 %s", existing, descriptor.persona,
			)
		}
		registry.byPersona[descriptor.persona] = descriptor
		registry.byIdentity[identityKey] = descriptor.persona
	}
	digest, err := digestPersonaRegistry(registry)
	if err != nil {
		return PersonaRegistry{}, err
	}
	registry.digest = digest
	return registry, nil
}

func buildPersonaDescriptor(
	input PersonaDescriptorInput,
	sinks SinkCatalog,
) (PersonaDescriptor, error) {
	if !input.Persona.Valid() || input.Persona == PersonaUnclassified ||
		input.Persona == PersonaDeadCode {
		return PersonaDescriptor{}, errors.New("PersonaDescriptor persona 非法")
	}
	identity := input.Identity.normalize()
	if err := identity.validate(); err != nil {
		return PersonaDescriptor{}, err
	}
	if !input.AuthorityKind.Valid() {
		return PersonaDescriptor{}, errors.New("PersonaDescriptor authority kind 非法")
	}
	if len(input.AllowedEndpointEvidence) == 0 {
		return PersonaDescriptor{}, errors.New("PersonaDescriptor 缺少允许的 EndpointEvidence")
	}
	allowed := make([]EndpointEvidence, 0, len(input.AllowedEndpointEvidence))
	allowedSet := make(map[EndpointEvidence]bool, len(input.AllowedEndpointEvidence))
	for _, evidence := range input.AllowedEndpointEvidence {
		if !evidence.Valid() || allowedSet[evidence] {
			return PersonaDescriptor{}, errors.New("PersonaDescriptor EndpointEvidence 非法或重复")
		}
		allowedSet[evidence] = true
		allowed = append(allowed, evidence)
	}
	sort.Slice(allowed, func(i, j int) bool { return allowed[i] < allowed[j] })

	if len(input.Exclusions) == 0 {
		return PersonaDescriptor{}, errors.New("PersonaDescriptor 缺少明确排除项")
	}
	exclusions := make([]PersonaExclusion, 0, len(input.Exclusions))
	exclusionSet := make(map[string]bool, len(input.Exclusions))
	for _, raw := range input.Exclusions {
		exclusion := raw.normalize()
		if err := exclusion.validate(); err != nil {
			return PersonaDescriptor{}, err
		}
		key := string(exclusion.Dimension) + "\x00" + exclusion.Value
		if exclusionSet[key] {
			return PersonaDescriptor{}, errors.New("PersonaDescriptor 排除项重复")
		}
		if (exclusion.Dimension == PersonaExclusionAuthFamily &&
			exclusion.Value == identity.AuthFamily) ||
			(exclusion.Dimension == PersonaExclusionOfficialProduct &&
				exclusion.Value == identity.OfficialProduct) {
			return PersonaDescriptor{}, errors.New("PersonaDescriptor 排除项与正向身份冲突")
		}
		exclusionSet[key] = true
		exclusions = append(exclusions, exclusion)
	}
	sort.Slice(exclusions, func(i, j int) bool {
		left := string(exclusions[i].Dimension) + "\x00" + exclusions[i].Value
		right := string(exclusions[j].Dimension) + "\x00" + exclusions[j].Value
		return left < right
	})

	routes := make([]PersonaRouteBinding, 0)
	for _, sink := range sinks.Bindings() {
		if !sink.RuntimeBindable() || sink.Persona() != input.Persona {
			continue
		}
		if !allowedSet[sink.EndpointEvidence()] {
			if !exclusionSet[string(PersonaExclusionSinkID)+"\x00"+
				strings.ToLower(string(sink.ID()))] {
				return PersonaDescriptor{}, fmt.Errorf(
					"Persona %s 的运行时 Sink %s 未被允许或明确排除",
					input.Persona, sink.ID(),
				)
			}
			continue
		}
		if exclusionSet[string(PersonaExclusionSinkID)+"\x00"+
			strings.ToLower(string(sink.ID()))] {
			return PersonaDescriptor{}, fmt.Errorf(
				"Persona %s 的 Sink %s 同时被允许和排除", input.Persona, sink.ID(),
			)
		}
		for _, route := range sink.Routes() {
			routes = append(routes, PersonaRouteBinding{
				SinkID: sink.ID(), Purpose: sink.Purpose(), Route: route.Key,
				Protocol: route.Protocol, Backend: sink.TargetBackend(),
				Evidence: sink.EndpointEvidence(),
			})
		}
	}
	if len(routes) == 0 {
		return PersonaDescriptor{}, errors.New("PersonaDescriptor 没有可执行 route")
	}
	sort.Slice(routes, func(i, j int) bool { return routes[i].key() < routes[j].key() })
	for index := 1; index < len(routes); index++ {
		if routes[index-1].key() == routes[index].key() {
			return PersonaDescriptor{}, errors.New("PersonaDescriptor route 重复")
		}
	}
	descriptor := PersonaDescriptor{
		persona: input.Persona, identity: identity, authorityKind: input.AuthorityKind,
		allowedEvidence: allowed, exclusions: exclusions, routes: routes,
	}
	raw, err := json.Marshal(struct {
		Persona         Persona                       `json:"persona"`
		Identity        PersonaIdentity               `json:"identity"`
		AuthorityKind   receiptcontract.AuthorityKind `json:"authority_kind"`
		AllowedEvidence []EndpointEvidence            `json:"allowed_endpoint_evidence"`
		Exclusions      []PersonaExclusion            `json:"exclusions"`
		Routes          []PersonaRouteBinding         `json:"routes"`
	}{
		descriptor.persona, descriptor.identity, descriptor.authorityKind,
		descriptor.allowedEvidence, descriptor.exclusions, descriptor.routes,
	})
	if err != nil {
		return PersonaDescriptor{}, err
	}
	sum := sha256.Sum256(raw)
	descriptor.digest = hex.EncodeToString(sum[:])
	return descriptor, nil
}

func digestPersonaRegistry(registry PersonaRegistry) (string, error) {
	personas := make([]string, 0, len(registry.byPersona))
	for persona := range registry.byPersona {
		personas = append(personas, string(persona))
	}
	sort.Strings(personas)
	payload := make([]struct {
		Persona Persona `json:"persona"`
		Digest  string  `json:"digest"`
	}, 0, len(personas))
	for _, value := range personas {
		descriptor := registry.byPersona[Persona(value)]
		payload = append(payload, struct {
			Persona Persona `json:"persona"`
			Digest  string  `json:"digest"`
		}{descriptor.persona, descriptor.digest})
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func (r PersonaRegistry) Resolve(persona Persona) (PersonaDescriptor, bool) {
	descriptor, ok := r.byPersona[persona]
	return descriptor, ok
}

func (r PersonaRegistry) ResolveIdentity(identity PersonaIdentity) (PersonaDescriptor, bool) {
	persona, ok := r.byIdentity[identity.key()]
	if !ok {
		return PersonaDescriptor{}, false
	}
	return r.Resolve(persona)
}

func (r PersonaRegistry) AuthorizeRoute(
	persona Persona,
	sinkID SinkID,
	purpose Purpose,
	route RouteKey,
	protocol WireProtocol,
) bool {
	descriptor, ok := r.Resolve(persona)
	return ok && descriptor.authorize(sinkID, purpose, route, protocol)
}

func (r PersonaRegistry) Digest() string { return r.digest }

func NewCodexPersonaRegistry(sinks SinkCatalog) (PersonaRegistry, error) {
	return NewPersonaRegistry([]PersonaDescriptorInput{codexPersonaDescriptorInput()}, sinks)
}

// NewProductionPersonaRegistry 为共享 production selector 冻结 Codex 与 Claude
// 两个已登记 Persona。DefaultPersonaRegistry 继续保持 Codex-only，避免改写 FW-E 基线事实。
func NewProductionPersonaRegistry(sinks SinkCatalog) (PersonaRegistry, error) {
	return NewPersonaRegistry([]PersonaDescriptorInput{
		codexPersonaDescriptorInput(),
		claudePersonaDescriptorInput(),
	}, sinks)
}

func codexPersonaDescriptorInput() PersonaDescriptorInput {
	return PersonaDescriptorInput{
		Persona: PersonaCodexCLI,
		Identity: PersonaIdentity{
			Provider: "openai", OfficialProduct: "codex", AuthFamily: "oauth",
			UpstreamRouteFamily: "openai-oauth",
		},
		AuthorityKind:           receiptcontract.AuthorityCodexExecutor,
		AllowedEndpointEvidence: []EndpointEvidence{EndpointEvidenceCodexProfile},
		Exclusions: []PersonaExclusion{
			{
				Dimension: PersonaExclusionAuthFamily, Value: "api-key",
				Reason: "API Key mimic 不属于官方 OAuth Persona",
			},
			{
				Dimension: PersonaExclusionOfficialProduct, Value: "chatgpt-web",
				Reason: "ChatGPT Web 具有独立客户端身份与执行 authority",
			},
			{
				Dimension: PersonaExclusionSinkID, Value: string(SinkCodexOAuthExchange),
				Reason: "授权码交换当前只有 transport 证据，尚无 Codex 画像绑定",
			},
		},
	}
}

var loadDefaultPersonaRegistry = sync.OnceValues(func() (PersonaRegistry, error) {
	return NewCodexPersonaRegistry(DefaultSinkCatalog())
})

func DefaultPersonaRegistry() PersonaRegistry {
	registry, err := loadDefaultPersonaRegistry()
	if err != nil {
		panic(err)
	}
	return registry
}
