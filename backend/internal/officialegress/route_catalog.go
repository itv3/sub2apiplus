package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

const (
	RegistryPurposeOpenAIOAuthHTTP = "openai_oauth_responses_http"
	RegistryPurposeOpenAIOAuthWS   = "openai_oauth_responses_ws"
	ReleaseFamilyOpenAIOAuthHTTP   = "openai_oauth_http"
	ReleaseFamilyOpenAIOAuthWS     = "openai_oauth_ws"
)

type officialRouteEntry struct {
	sinkID      SinkID
	route       CatalogRoute
	physicalID  PhysicalRouteID
	persona     Persona
	evidence    EndpointEvidence
	claimsScope bool
	endpointID  string
	familyID    string
	registryKey string
}

// PhysicalRouteMatch 只表示 method/host/path/protocol 已落入受审物理路由闭集。
// 它有意不携带 purpose、persona 或发布选择，避免在校验 SinkBinding 之前信任请求
// context 中尚未验证的业务声明。
type PhysicalRouteMatch struct {
	ID           PhysicalRouteID
	Protocol     WireProtocol
	Method       string
	HostTemplate string
	PathTemplate string
	// ClaimsScope=false 表示该 route 只在调用点携带明确 SinkID 时进入 Guard。
	// 这避免 FW-E observation-only route 把同端点的 API Key 等其他产品流量
	// 意外纳入 Claude OAuth 管理域。
	ClaimsScope bool
}

// ResolvedOfficialRoute 是一次请求解析出的安全 route 视图。Path 只会返回模板。
type ResolvedOfficialRoute struct {
	SinkID          SinkID
	PhysicalRouteID PhysicalRouteID
	Key             RouteKey
	Protocol        WireProtocol
	Persona         Persona
	EndpointID      string
	FamilyID        string
	RegistryPurpose string
}

// OfficialRouteCatalog 按 method + host + path + purpose + protocol 解析受控 route。
// 同一物理 route 可以对应多个业务 purpose，但只连接到一个画像 endpoint。
type OfficialRouteCatalog struct {
	entries         []officialRouteEntry
	managedPurposes map[Purpose]struct{}
	scopeClaims     map[PhysicalRouteID]bool
	physical        PhysicalRouteCatalog
}

func NewOfficialRouteCatalog(sinks SinkCatalog) (OfficialRouteCatalog, error) {
	physical, err := NewPhysicalRouteCatalog(sinks)
	if err != nil {
		return OfficialRouteCatalog{}, err
	}
	endpointBindings := make([]EndpointBindingCatalog, 0, 2)
	var claudeProfile claudeFWGProfile
	if sinkCatalogHasClaudeProfile(sinks) {
		claudeProfile, err = loadClaudeFWGProfile()
		if err != nil {
			return OfficialRouteCatalog{}, err
		}
	}
	seenProfiles := make(map[string]bool)
	for _, role := range []ProductionReleaseRole{ProductionReleaseActive, ProductionReleaseRollback} {
		coordinate, coordinateErr := DefaultPersonaReleaseCatalog().Resolve(
			PersonaCodexCLI, role,
		)
		if coordinateErr != nil {
			return OfficialRouteCatalog{}, coordinateErr
		}
		mode, modeErr := codexModeForProductionRole(role)
		if modeErr != nil {
			return OfficialRouteCatalog{}, modeErr
		}
		release, resolveErr := DefaultReleaseCatalog().Resolve(mode)
		if resolveErr != nil {
			return OfficialRouteCatalog{}, resolveErr
		}
		if coordinateErr := validateCodexPersonaReleaseCoordinate(coordinate, release); coordinateErr != nil {
			return OfficialRouteCatalog{}, coordinateErr
		}
		if seenProfiles[release.ProfileDigest()] {
			continue
		}
		seenProfiles[release.ProfileDigest()] = true
		bindings, bindingErr := NewEndpointBindingCatalog(sinks, physical, release.ExecutableProfile())
		if bindingErr != nil {
			return OfficialRouteCatalog{}, bindingErr
		}
		endpointBindings = append(endpointBindings, bindings)
	}
	entries := make([]officialRouteEntry, 0)
	managedPurposes := make(map[Purpose]struct{})
	scopeClaims := make(map[PhysicalRouteID]bool)
	seen := make(map[string]struct{})
	for _, sink := range sinks.Bindings() {
		if !sink.RuntimeBindable() {
			continue
		}
		managedPurposes[sink.Purpose()] = struct{}{}
		for _, route := range sink.Routes() {
			physicalID, _, ok := physical.ResolveRoute(route)
			if !ok {
				return OfficialRouteCatalog{}, fmt.Errorf("Sink %s 缺少 PhysicalRoute", sink.ID())
			}
			entry := officialRouteEntry{
				sinkID: sink.ID(), route: route, physicalID: physicalID,
				persona: sink.Persona(), evidence: sink.EndpointEvidence(),
				claimsScope: !(sink.Persona() == PersonaUnclassified &&
					sink.EndpointEvidence() == EndpointEvidenceExternalPersona &&
					sink.EnforcementState() == SinkStateLegacyObserve),
			}
			if entry.claimsScope {
				scopeClaims[physicalID] = true
			}
			// 物理路由存在不等于具备 Codex Body/endpoint 画像。只有
			// codex_profile 证据才能连接 EndpointID 与 ReleaseSelection；
			// transport_only（例如 authorization-code exchange）只登记物理事实。
			if sink.Persona() == PersonaCodexCLI &&
				sink.EndpointEvidence() == EndpointEvidenceCodexProfile {
				var binding EndpointBinding
				found := false
				for _, catalog := range endpointBindings {
					candidate, ok := catalog.ResolveBindingRoute(sink, route, physical)
					if !ok {
						continue
					}
					if found && (candidate.EndpointID() != binding.EndpointID() ||
						candidate.ReleasePurpose() != binding.ReleasePurpose()) {
						return OfficialRouteCatalog{}, fmt.Errorf(
							"Codex Sink %s 的 route 在 Active/Previous 画像中连接到不同 EndpointBinding: %s",
							sink.ID(), route.Key,
						)
					}
					binding = candidate
					found = true
				}
				if !found {
					return OfficialRouteCatalog{}, fmt.Errorf(
						"Codex Sink %s 的 route 在 Active/Previous 画像中均没有 ReleaseSelection: %s",
						sink.ID(),
						route.Key,
					)
				}
				entry.endpointID = binding.EndpointID()
				entry.registryKey = binding.ReleasePurpose()
				entry.familyID = ReleaseFamilyOpenAIOAuthHTTP
				if route.Protocol == WireProtocolWebSocket {
					entry.familyID = ReleaseFamilyOpenAIOAuthWS
				}
			} else if sink.Persona() == PersonaClaudeCode &&
				sink.EndpointEvidence() == EndpointEvidenceClaudeProfile {
				endpoint, found := claudeEndpointForRoute(claudeProfile, sink, route)
				if !found {
					return OfficialRouteCatalog{}, fmt.Errorf(
						"Claude Sink %s 的 route 没有 FW-G endpoint 绑定：%s",
						sink.ID(), route.Key,
					)
				}
				entry.endpointID = endpoint.id
				entry.registryKey = endpoint.routeID
				entry.familyID = "anthropic-oauth-http"
			}
			key := string(sink.ID()) + "\x00" + catalogRouteIdentity(route)
			if _, duplicate := seen[key]; duplicate {
				return OfficialRouteCatalog{}, fmt.Errorf("Sink %s 的 route 重复登记", sink.ID())
			}
			seen[key] = struct{}{}
			entries = append(entries, entry)
		}
	}
	if len(entries) == 0 {
		return OfficialRouteCatalog{}, errors.New("OfficialRouteCatalog 为空")
	}
	sort.Slice(entries, func(i, j int) bool {
		return catalogRouteIdentity(entries[i].route) < catalogRouteIdentity(entries[j].route)
	})
	return OfficialRouteCatalog{
		entries: entries, managedPurposes: managedPurposes,
		scopeClaims: scopeClaims, physical: physical,
	}, nil
}

// sinkCatalogHasClaudeProfile 只从当前进程实际安装的 SinkCatalog 决定是否加载
// Claude candidate 制品。candidate 关闭时默认 Catalog 不含 Claude strict Sink，
// 因而 Codex-only 进程既不解析也不依赖 Claude 画像。
func sinkCatalogHasClaudeProfile(sinks SinkCatalog) bool {
	for _, binding := range sinks.Bindings() {
		if binding.Persona() == PersonaClaudeCode &&
			binding.EndpointEvidence() == EndpointEvidenceClaudeProfile {
			return true
		}
	}
	return false
}

// MatchPhysical 是 Guard 的第一阶段解析。它完全忽略 purpose/persona/SinkID，
// 只回答最终物理请求是否命中 Catalog 中至少一条 route。
func (c OfficialRouteCatalog) MatchPhysical(
	method string,
	target *url.URL,
	protocol WireProtocol,
) (PhysicalRouteMatch, bool) {
	id, key, ok := c.physical.Match(method, target, protocol)
	if !ok {
		return PhysicalRouteMatch{}, false
	}
	return PhysicalRouteMatch{
		ID: id, Protocol: key.Protocol, Method: key.Method,
		HostTemplate: key.Host, PathTemplate: key.Path,
		ClaimsScope: c.scopeClaims[id],
	}, true
}

// ResolveBinding 是第二阶段解析。purpose/persona 只取自已登记 SinkBinding；请求
// metadata 只能在 Guard 中作为待验证声明，不能参与这里的选择。
func (c OfficialRouteCatalog) ResolveBinding(
	method string,
	target *url.URL,
	protocol WireProtocol,
	binding SinkBinding,
) (ResolvedOfficialRoute, bool) {
	if target == nil || !protocol.Valid() || !binding.RuntimeBindable() {
		return ResolvedOfficialRoute{}, false
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	host := normalizeRouteHost(target.Hostname())
	path := target.EscapedPath()
	if path == "" {
		path = "/"
	}
	for i := range c.entries {
		entry := &c.entries[i]
		if entry.sinkID != binding.ID() || entry.route.Protocol != protocol || entry.route.Key.Method != method ||
			entry.route.Key.Purpose != binding.Purpose() || entry.persona != binding.Persona() ||
			!matchRouteHost(entry.route.Key.Host, host) || !matchRoutePath(entry.route.Key.Path, path) {
			continue
		}
		resolved := ResolvedOfficialRoute{
			SinkID: entry.sinkID, PhysicalRouteID: entry.physicalID,
			Key: entry.route.Key, Protocol: entry.route.Protocol, Persona: entry.persona,
			EndpointID: entry.endpointID, FamilyID: entry.familyID,
			RegistryPurpose: entry.registryKey,
		}
		if bindingHasRoute(binding, resolved, protocol) {
			return resolved, true
		}
	}
	return ResolvedOfficialRoute{}, false
}

func catalogRouteIdentity(route CatalogRoute) string {
	return route.Key.String() + "\x00" + string(route.Protocol)
}

func (c OfficialRouteCatalog) HasManagedPurpose(purpose Purpose) bool {
	_, ok := c.managedPurposes[purpose]
	return ok
}

func (c OfficialRouteCatalog) Resolve(
	method string,
	target *url.URL,
	protocol WireProtocol,
	purpose Purpose,
) (ResolvedOfficialRoute, bool) {
	if target == nil || !protocol.Valid() {
		return ResolvedOfficialRoute{}, false
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	host := normalizeRouteHost(target.Hostname())
	path := target.EscapedPath()
	if path == "" {
		path = "/"
	}

	var resolved *officialRouteEntry
	for i := range c.entries {
		entry := &c.entries[i]
		if entry.route.Protocol != protocol || entry.route.Key.Method != method ||
			!matchRouteHost(entry.route.Key.Host, host) || !matchRoutePath(entry.route.Key.Path, path) {
			continue
		}
		if purpose != "" && entry.route.Key.Purpose != purpose {
			continue
		}
		if resolved == nil {
			resolved = entry
			continue
		}
		// purpose 缺失时只把物理 route 识别为“已登记”；不同业务 purpose 仍由
		// SinkBinding 阶段判定，不能在这里任选一个 purpose 赋给请求。
		if purpose == "" && (resolved.persona != entry.persona ||
			resolved.endpointID != entry.endpointID || resolved.familyID != entry.familyID ||
			resolved.registryKey != entry.registryKey) {
			return ResolvedOfficialRoute{}, false
		}
	}
	if resolved == nil {
		return ResolvedOfficialRoute{}, false
	}
	key := resolved.route.Key
	if purpose == "" {
		key.Purpose = ""
	}
	return ResolvedOfficialRoute{
		SinkID: resolved.sinkID, PhysicalRouteID: resolved.physicalID,
		Key:             key,
		Protocol:        resolved.route.Protocol,
		Persona:         resolved.persona,
		EndpointID:      resolved.endpointID,
		FamilyID:        resolved.familyID,
		RegistryPurpose: resolved.registryKey,
	}, true
}

func (c OfficialRouteCatalog) Routes() []ResolvedOfficialRoute {
	out := make([]ResolvedOfficialRoute, 0, len(c.entries))
	for _, entry := range c.entries {
		out = append(out, ResolvedOfficialRoute{
			SinkID: entry.sinkID, PhysicalRouteID: entry.physicalID,
			Key:             entry.route.Key,
			Protocol:        entry.route.Protocol,
			Persona:         entry.persona,
			EndpointID:      entry.endpointID,
			FamilyID:        entry.familyID,
			RegistryPurpose: entry.registryKey,
		})
	}
	return out
}

func (c OfficialRouteCatalog) ReleaseSelection(
	route ResolvedOfficialRoute,
) (ReleaseSelectionPolicy, error) {
	if route.Persona != PersonaCodexCLI || route.EndpointID == "" || route.RegistryPurpose == "" ||
		route.FamilyID == "" || route.Key.Purpose == "" {
		return ReleaseSelectionPolicy{}, errors.New("route 没有完整 Codex ReleaseSelection")
	}
	selection := ReleaseSelectionPolicy{
		ID:              "selection:" + route.Key.String() + ":" + string(route.Protocol),
		Source:          "docs/egress/foundation/persona-catalog.md+release-graph.json",
		BusinessPurpose: route.Key.Purpose,
		FamilyID:        route.FamilyID,
		RegistryPurpose: route.RegistryPurpose,
		EndpointID:      route.EndpointID,
	}
	return selection, selection.Validate()
}

func normalizeRouteHost(host string) string {
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(host)), ".")
}

func matchRouteHost(template, actual string) bool {
	template = normalizeRouteHost(template)
	actual = normalizeRouteHost(actual)
	if strings.HasPrefix(template, "*.") {
		suffix := strings.TrimPrefix(template, "*")
		return strings.HasSuffix(actual, suffix) && len(actual) > len(suffix)
	}
	return template == actual
}

func matchRoutePath(template, actual string) bool {
	if template == "/{server_returned_path}" || template == "{server_returned_path}" {
		return strings.HasPrefix(actual, "/")
	}
	templateParts := strings.Split(strings.TrimPrefix(template, "/"), "/")
	actualParts := strings.Split(strings.TrimPrefix(actual, "/"), "/")
	if len(templateParts) != len(actualParts) {
		return false
	}
	for index, expected := range templateParts {
		if strings.HasPrefix(expected, "{") && strings.HasSuffix(expected, "}") {
			if actualParts[index] == "" {
				return false
			}
			continue
		}
		if expected != actualParts[index] {
			return false
		}
	}
	return true
}

type EgressScope string

const (
	EgressScopeManaged    EgressScope = "managed"
	EgressScopeOutOfScope EgressScope = "out_of_scope"
)

type ScopeReason string

const (
	ScopeReasonManagedNamespace  ScopeReason = "managed_namespace"
	ScopeReasonRegisteredRoute   ScopeReason = "registered_physical_route"
	ScopeReasonRegisteredSink    ScopeReason = "registered_sink"
	ScopeReasonRegisteredPurpose ScopeReason = "registered_purpose"
	ScopeReasonTrustedToken      ScopeReason = "trusted_finalization_token"
	ScopeReasonOutsideDomain     ScopeReason = "outside_managed_domain"
)

type ScopeDecision struct {
	Scope        EgressScope
	Reason       ScopeReason
	PathTemplate string
	// PathSHA256 仅用于 unknown route 的精确覆盖匹配，不写入 Guard 日志。
	// 摘要输入是最终 URL 的 EscapedPath，不包含 query。
	PathSHA256 string
}

// EgressScopeCatalog 是 Guard 的第 0 档。它不依赖 RouteCatalog 是否命中，避免
// “未知受控 route”与“第三方发送”被混成同一分支。
type EgressScopeCatalog struct {
	sinks  SinkCatalog
	routes OfficialRouteCatalog
}

func NewEgressScopeCatalog(sinks SinkCatalog, routes OfficialRouteCatalog) EgressScopeCatalog {
	return EgressScopeCatalog{sinks: sinks, routes: routes}
}

func (c EgressScopeCatalog) Classify(
	method string,
	target *url.URL,
	protocol WireProtocol,
	metadata attemptMetadata,
	trustedToken bool,
) ScopeDecision {
	if trustedToken {
		return ScopeDecision{Scope: EgressScopeManaged, Reason: ScopeReasonTrustedToken,
			PathTemplate: normalizedScopePath(target), PathSHA256: exactRoutePathSHA256(target)}
	}
	if physical, ok := c.routes.MatchPhysical(method, target, protocol); ok && physical.ClaimsScope {
		return ScopeDecision{Scope: EgressScopeManaged, Reason: ScopeReasonRegisteredRoute,
			PathTemplate: physical.PathTemplate, PathSHA256: exactRoutePathSHA256(target)}
	}
	// 任意非空 SinkID 都是调用点主动声明“这是受管发送”的证据；是否登记以及是否
	// runtime-bindable 留给 Guard 第 2 档判断。这样新 ID 不能借未登记 route 逃逸，
	// 同时也不需要信任可伪造的 purpose。
	if metadata.SinkID != "" {
		return ScopeDecision{Scope: EgressScopeManaged, Reason: ScopeReasonRegisteredSink,
			PathTemplate: normalizedScopePath(target), PathSHA256: exactRoutePathSHA256(target)}
	}
	if template, ok := managedNamespaceTemplate(method, target, protocol); ok {
		return ScopeDecision{Scope: EgressScopeManaged, Reason: ScopeReasonManagedNamespace,
			PathTemplate: template, PathSHA256: exactRoutePathSHA256(target)}
	}
	return ScopeDecision{Scope: EgressScopeOutOfScope, Reason: ScopeReasonOutsideDomain,
		PathTemplate: normalizedScopePath(target), PathSHA256: exactRoutePathSHA256(target)}
}

func managedNamespaceTemplate(method string, target *url.URL, protocol WireProtocol) (string, bool) {
	if target == nil {
		return "/", false
	}
	host := normalizeRouteHost(target.Hostname())
	path := target.EscapedPath()
	if path == "" {
		path = "/"
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	switch host {
	case "chatgpt.com":
		for _, prefix := range []string{
			"/backend-api/codex", "/backend-api/wham", "/backend-api/files",
			"/backend-api/settings", "/backend-api/accounts", "/backend-api/subscriptions",
		} {
			if path == prefix || strings.HasPrefix(path, prefix+"/") {
				return prefix + "/{managed_path}", true
			}
		}
	case "auth.openai.com":
		if path == "/oauth/token" {
			return "/oauth/token", true
		}
		if strings.HasPrefix(path, "/api/accounts/") {
			return "/api/accounts/{managed_path}", true
		}
	case "api.openai.com":
		// 该 host 同时承载第三方兼容层，只有这一个物理 WS route 可按 namespace 识别。
		if method == http.MethodGet && protocol == WireProtocolWebSocket && path == "/v1/realtime" {
			return "/v1/realtime", true
		}
	default:
		if method == http.MethodPut && matchRouteHost("*.oaiusercontent.com", host) {
			return "/{server_returned_path}", true
		}
	}
	return normalizedScopePath(target), false
}

func normalizedScopePath(target *url.URL) string {
	if target == nil {
		return "/"
	}
	path := target.EscapedPath()
	if path == "" {
		return "/"
	}
	// out-of-scope 事件不需要定位真实资源，最多保留首个静态段。
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		return "/"
	}
	return "/" + parts[0] + "/{path}"
}

// exactRoutePathSHA256 为运行时覆盖生成不泄漏原始路径的精确 route key。
// EscapedPath 保留最终线序中的路径转义；等价性不确定时宁可摘要不同并 fail-close。
func exactRoutePathSHA256(target *url.URL) string {
	path := "/"
	if target != nil && target.EscapedPath() != "" {
		path = target.EscapedPath()
	}
	sum := sha256.Sum256([]byte(path))
	return hex.EncodeToString(sum[:])
}

var defaultRouteCatalog = mustBuildDefaultRouteCatalog()

func mustBuildDefaultRouteCatalog() OfficialRouteCatalog {
	catalog, err := NewOfficialRouteCatalog(defaultSinkCatalog)
	if err != nil {
		panic(fmt.Sprintf("加载 OfficialRouteCatalog: %v", err))
	}
	return catalog
}

func DefaultOfficialRouteCatalog() OfficialRouteCatalog {
	return defaultRouteCatalog
}
