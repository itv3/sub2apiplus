package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strings"
)

const (
	ClaudeExecutorAuthorityID ExecutorID = "claude-persona-executor"
	ClaudeTokenIssuerID       ExecutorID = "claude-finalization-token"
)

// 以下历史名称只保留现有收据和测试的源码兼容性。它们从 Catalog 的
// validation_candidate 派生，不再拥有生产 selector 的选择权。
var (
	ClaudeFWGVersion       = DefaultClaudeReleaseCatalog().ValidationCandidate().Release().Version()
	ClaudeFWGProfileDigest = DefaultClaudeReleaseCatalog().ValidationCandidate().Release().ProfileDigest()
	ClaudeFWGReleaseDigest = DefaultClaudeReleaseCatalog().ValidationCandidate().Release().ReleaseDigest()
	ClaudeFWGBundleDigest  = DefaultClaudeReleaseCatalog().ValidationCandidate().Release().BundleDigest()
)

type claudeProfileEndpointDocument struct {
	Scheme        string `json:"scheme"`
	Host          string `json:"host"`
	Port          int    `json:"port"`
	Method        string `json:"method"`
	RequestTarget string `json:"request_target"`
	HTTPVersion   string `json:"http_version"`
}

type claudeHeaderFact struct {
	Name           string   `json:"name"`
	Classification string   `json:"classification"`
	Value          string   `json:"value"`
	ValuePolicy    string   `json:"value_policy"`
	Source         string   `json:"source"`
	PresentIn      []string `json:"present_in"`
}

type claudeHeadersDocument struct {
	Facts           []claudeHeaderFact  `json:"facts"`
	OrderByScenario map[string][]string `json:"order_by_scenario"`
}

type claudeBodyDocument struct {
	BodyPolicy    string              `json:"body_policy"`
	FieldTypes    map[string][]string `json:"field_types"`
	Required      []string            `json:"required_fields"`
	Optional      []string            `json:"optional_fields"`
	SecretFields  []string            `json:"secret_fields"`
	TopLevel      []string            `json:"top_level_order"`
	TopByScenario map[string][]string `json:"top_level_order_by_scenario"`
}

type claudeEndpointSection struct {
	EndpointID string                        `json:"endpoint_id"`
	Purpose    string                        `json:"purpose"`
	Endpoint   claudeProfileEndpointDocument `json:"endpoint"`
	Headers    claudeHeadersDocument         `json:"headers"`
	Body       claudeBodyDocument            `json:"body"`
	Transport  map[string]any                `json:"transport"`
}

type claudeStrictEndpointDocument struct {
	EndpointKind string                        `json:"endpoint_kind"`
	EndpointID   string                        `json:"endpoint_id"`
	EgressID     string                        `json:"egress_id"`
	RouteID      string                        `json:"route_id"`
	BindingID    string                        `json:"binding_id"`
	BodyPolicy   string                        `json:"body_policy"`
	Endpoint     claudeProfileEndpointDocument `json:"endpoint"`
	SpecIDs      []string                      `json:"spec_ids"`
}

type claudeRuleDocument struct {
	SpecID             string   `json:"spec_id"`
	Title              string   `json:"title"`
	AtomicAssertionIDs []string `json:"atomic_assertion_ids"`
	AssertionResult    string   `json:"assertion_result"`
	EvidenceLevel      string   `json:"evidence_level"`
	ProductionEligible string   `json:"production_eligibility"`
}

type claudeProfileDocument struct {
	SchemaVersion string `json:"schema_version"`
	Identity      struct {
		Version                      string   `json:"version"`
		Platform                     string   `json:"platform"`
		Entrypoints                  []string `json:"entrypoints"`
		SupportedModels              []string `json:"supported_models"`
		ModelAliasPolicy             string   `json:"model_alias_policy"`
		UnknownModelPolicy           string   `json:"unknown_model_policy"`
		ModelCapabilityCatalogSHA256 string   `json:"model_capability_catalog_sha256"`
	} `json:"identity"`
	Endpoint        claudeProfileEndpointDocument    `json:"endpoint"`
	Headers         claudeHeadersDocument            `json:"headers"`
	Body            claudeBodyDocument               `json:"body"`
	Lifecycle       claudeEndpointSection            `json:"lifecycle"`
	Auxiliary       map[string]claudeEndpointSection `json:"auxiliary"`
	StrictEndpoints []claudeStrictEndpointDocument   `json:"strict_endpoints"`
	Rules           []claudeRuleDocument             `json:"rules"`
}

type claudeEndpointProfile struct {
	kind        string
	id          string
	egressID    string
	routeID     string
	bindingID   string
	sinkID      SinkID
	purpose     Purpose
	method      string
	target      *url.URL
	route       CatalogRoute
	headers     claudeHeadersDocument
	body        claudeBodyDocument
	transportID string
	withALPN    bool
}

type claudeFWGProfile struct {
	document  claudeProfileDocument
	endpoints map[string]claudeEndpointProfile
	rules     map[string]claudeRuleDocument
}

func loadClaudeFWGProfile() (claudeFWGProfile, error) {
	return loadClaudeProfile(
		DefaultClaudeReleaseCatalog().ValidationCandidate().Release(),
	)
}

func loadClaudeProfile(release ResolvedClaudeRelease) (claudeFWGProfile, error) {
	if err := release.validate(); err != nil {
		return claudeFWGProfile{}, err
	}
	sum := sha256.Sum256(release.profileRaw)
	if hex.EncodeToString(sum[:]) != release.ProfileDigest() {
		return claudeFWGProfile{}, errors.New("Claude FW-G 画像原文字节摘要不匹配")
	}
	var document claudeProfileDocument
	if err := json.Unmarshal(release.profileRaw, &document); err != nil {
		return claudeFWGProfile{}, fmt.Errorf("解析 Claude FW-G 画像：%w", err)
	}
	if document.SchemaVersion != "claude-code-fw-f-target-profile/v6" ||
		document.Identity.Version != release.Version() ||
		document.Identity.Platform != release.Platform() {
		return claudeFWGProfile{}, errors.New("Claude FW-G 画像身份不一致")
	}
	if !slices.Equal(document.Identity.SupportedModels, release.Models()) ||
		document.Identity.ModelAliasPolicy != "explicit-only" ||
		document.Identity.UnknownModelPolicy != "deny" ||
		len(document.Identity.ModelCapabilityCatalogSHA256) != sha256.Size*2 ||
		!slices.Equal(document.Body.FieldTypes["fallbacks"], []string{"array"}) ||
		!slices.Contains(document.Body.Optional, "fallbacks") {
		return claudeFWGProfile{}, errors.New("Claude FW-G 模型能力画像声明不完整")
	}
	if len(document.Rules) != release.RequiredRuleCount() ||
		len(document.StrictEndpoints) != release.StrictEndpointCount() {
		return claudeFWGProfile{}, errors.New("Claude FW-G 画像规则或 strict endpoint 数量与 Release 不一致")
	}
	profile := claudeFWGProfile{
		document:  document,
		endpoints: make(map[string]claudeEndpointProfile, len(document.StrictEndpoints)),
		rules:     make(map[string]claudeRuleDocument, len(document.Rules)),
	}
	atomicAssertions := make(map[string]string, release.ProfileAtomicAssertionCount())
	for _, rule := range document.Rules {
		if strings.TrimSpace(rule.SpecID) == "" || rule.AssertionResult != "passed" ||
			rule.EvidenceLevel != "observed" || rule.ProductionEligible != "validation_only" ||
			len(rule.AtomicAssertionIDs) == 0 {
			return claudeFWGProfile{}, fmt.Errorf("Claude RequiredRule 状态非法：%s", rule.SpecID)
		}
		if _, duplicate := profile.rules[rule.SpecID]; duplicate {
			return claudeFWGProfile{}, fmt.Errorf("Claude RequiredRule 重复：%s", rule.SpecID)
		}
		for _, assertionID := range rule.AtomicAssertionIDs {
			if strings.TrimSpace(assertionID) != assertionID || !strings.HasPrefix(assertionID, "SPEC-") {
				return claudeFWGProfile{}, fmt.Errorf(
					"Claude RequiredRule %s 含非法原子断言：%s", rule.SpecID, assertionID,
				)
			}
			if owner, duplicate := atomicAssertions[assertionID]; duplicate {
				return claudeFWGProfile{}, fmt.Errorf(
					"Claude 原子断言重复归属：%s（%s/%s）", assertionID, owner, rule.SpecID,
				)
			}
			atomicAssertions[assertionID] = rule.SpecID
		}
		profile.rules[rule.SpecID] = rule
	}
	if len(atomicAssertions) != release.ProfileAtomicAssertionCount() {
		return claudeFWGProfile{}, fmt.Errorf(
			"Claude 画像原子断言数量与 Release 不一致：%d", len(atomicAssertions),
		)
	}
	endpointRuleUnion := make(map[string]struct{}, len(profile.rules))
	for _, strict := range document.StrictEndpoints {
		if len(strict.SpecIDs) == 0 {
			return claudeFWGProfile{}, fmt.Errorf("Claude strict endpoint 没有规则：%s", strict.EndpointKind)
		}
		endpointRules := make(map[string]struct{}, len(strict.SpecIDs))
		for _, specID := range strict.SpecIDs {
			if _, approved := profile.rules[specID]; !approved {
				return claudeFWGProfile{}, fmt.Errorf(
					"Claude strict endpoint %s 引用未知规则：%s", strict.EndpointKind, specID,
				)
			}
			if _, duplicate := endpointRules[specID]; duplicate {
				return claudeFWGProfile{}, fmt.Errorf(
					"Claude strict endpoint %s 重复引用规则：%s", strict.EndpointKind, specID,
				)
			}
			endpointRules[specID] = struct{}{}
			endpointRuleUnion[specID] = struct{}{}
		}
		endpoint, err := buildClaudeEndpointProfile(document, strict)
		if err != nil {
			return claudeFWGProfile{}, err
		}
		if _, duplicate := profile.endpoints[endpoint.kind]; duplicate {
			return claudeFWGProfile{}, fmt.Errorf("Claude strict endpoint 重复：%s", endpoint.kind)
		}
		profile.endpoints[endpoint.kind] = endpoint
	}
	if len(endpointRuleUnion) != len(profile.rules) {
		missing := make([]string, 0)
		for specID := range profile.rules {
			if _, covered := endpointRuleUnion[specID]; !covered {
				missing = append(missing, specID)
			}
		}
		sort.Strings(missing)
		return claudeFWGProfile{}, fmt.Errorf(
			"Claude strict endpoint 规则并集未覆盖全部 RequiredRules：%v", missing,
		)
	}
	return profile, nil
}

func buildClaudeEndpointProfile(
	document claudeProfileDocument,
	strict claudeStrictEndpointDocument,
) (claudeEndpointProfile, error) {
	section, sinkID, purpose, withALPN, err := claudeEndpointSectionForKind(document, strict.EndpointKind)
	if err != nil {
		return claudeEndpointProfile{}, err
	}
	if section.EndpointID != strict.EndpointID || section.Endpoint != strict.Endpoint {
		return claudeEndpointProfile{}, fmt.Errorf("Claude strict endpoint 与画像 section 不一致：%s", strict.EndpointKind)
	}
	if strict.Endpoint.Scheme != "https" || strict.Endpoint.Port != 443 ||
		strict.Endpoint.HTTPVersion != "HTTP/1.1" ||
		strings.ToUpper(strict.Endpoint.Method) != strict.Endpoint.Method {
		return claudeEndpointProfile{}, fmt.Errorf("Claude endpoint 坐标非法：%s", strict.EndpointKind)
	}
	target, err := url.Parse(strict.Endpoint.Scheme + "://" + strict.Endpoint.Host + strict.Endpoint.RequestTarget)
	if err != nil || target.Hostname() == "" {
		return claudeEndpointProfile{}, fmt.Errorf("Claude endpoint URL 非法：%s", strict.EndpointKind)
	}
	path := target.EscapedPath()
	route := CatalogRoute{Key: RouteKey{
		Method: strict.Endpoint.Method, Host: strict.Endpoint.Host, Path: path, Purpose: purpose,
	}, Protocol: WireProtocolHTTP}
	if err := route.Validate(); err != nil {
		return claudeEndpointProfile{}, err
	}
	transportID := "claude-code-node-http1-no-alpn-v1"
	if withALPN {
		transportID = "claude-code-node-http1-alpn-v1"
	}
	return claudeEndpointProfile{
		kind: strict.EndpointKind, id: strict.EndpointID, egressID: strict.EgressID,
		routeID: strict.RouteID, bindingID: strict.BindingID, sinkID: sinkID,
		purpose: purpose, method: strict.Endpoint.Method, target: target, route: route,
		headers: section.Headers, body: section.Body, transportID: transportID,
		withALPN: withALPN,
	}, nil
}

func claudeEndpointSectionForKind(
	document claudeProfileDocument,
	kind string,
) (claudeEndpointSection, SinkID, Purpose, bool, error) {
	switch kind {
	case "messages-inference":
		return claudeEndpointSection{
			EndpointID: "claude-messages-inference-v1", Purpose: "official-client-inference",
			Endpoint: document.Endpoint, Headers: document.Headers, Body: document.Body,
		}, SinkClaudeMessagesInference, Purpose("user_request.messages"), true, nil
	case "lifecycle-hello":
		return document.Lifecycle, SinkClaudeLifecycleHello, Purpose("lifecycle.hello"), true, nil
	case "policy-limits":
		return document.Auxiliary["policy-limits"], SinkClaudePolicyLimits, Purpose("account.policy_limits"), false, nil
	case "remote-settings":
		return document.Auxiliary["remote-settings"], SinkClaudeRemoteSettings, Purpose("account.remote_settings"), false, nil
	case "oauth-profile":
		return document.Auxiliary["oauth-profile"], SinkClaudeOAuthProfile, Purpose("account.oauth_profile"), false, nil
	case "count-tokens":
		return document.Auxiliary["count-tokens"], SinkClaudeCountTokens, Purpose("user_request.count_tokens"), true, nil
	case "oauth-token-refresh":
		return document.Auxiliary["oauth-token-refresh"], SinkClaudeOAuthTokenRefresh, Purpose("oauth.refresh"), false, nil
	case "mcp-servers":
		return document.Auxiliary["mcp-servers"], SinkClaudeMCPServers, Purpose("catalog.mcp_servers"), false, nil
	default:
		return claudeEndpointSection{}, "", "", false, fmt.Errorf("未知 Claude strict endpoint：%s", kind)
	}
}

func (p claudeFWGProfile) endpoint(kind string) (claudeEndpointProfile, error) {
	endpoint, ok := p.endpoints[kind]
	if !ok {
		return claudeEndpointProfile{}, fmt.Errorf("Claude endpoint 未批准：%s", kind)
	}
	return endpoint, nil
}

func (p claudeFWGProfile) ruleIDs() []string {
	ids := make([]string, 0, len(p.rules))
	for id := range p.rules {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func claudeHeaderOrder(headers claudeHeadersDocument, agent bool) ([]string, error) {
	preferred := []string{"target-rm", "a1-01", "s1", "a1"}
	if agent {
		preferred = []string{"a1-02", "target-rm", "a1-01", "s1", "a1"}
	}
	for _, scenario := range preferred {
		if order := headers.OrderByScenario[scenario]; len(order) != 0 {
			return append([]string(nil), order...), nil
		}
	}
	if len(headers.OrderByScenario) != 1 {
		return nil, errors.New("Claude Header 顺序没有唯一可执行场景")
	}
	for _, order := range headers.OrderByScenario {
		return append([]string(nil), order...), nil
	}
	return nil, errors.New("Claude Header 顺序为空")
}

func claudeHeaderFacts(headers claudeHeadersDocument) map[string]claudeHeaderFact {
	out := make(map[string]claudeHeaderFact, len(headers.Facts))
	for _, fact := range headers.Facts {
		out[strings.ToLower(fact.Name)] = fact
	}
	return out
}

func claudeStrictEndpointKinds() []string {
	return []string{
		"count-tokens", "lifecycle-hello", "mcp-servers", "messages-inference",
		"oauth-profile", "oauth-token-refresh", "policy-limits", "remote-settings",
	}
}

func claudeEndpointQueryMatches(endpoint claudeEndpointProfile, target *url.URL) bool {
	return target != nil && target.Scheme == endpoint.target.Scheme &&
		target.Host == endpoint.target.Host && target.EscapedPath() == endpoint.target.EscapedPath() &&
		target.RawQuery == endpoint.target.RawQuery && target.Fragment == "" && target.User == nil
}

func claudeHTTPMethodAllowsBody(method string) bool {
	return method == http.MethodPost
}
