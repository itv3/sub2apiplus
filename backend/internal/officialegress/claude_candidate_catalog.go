package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

const (
	claudeFWGCandidateChangeset  = "claude-code-2.1.226-fw-g-candidate"
	claudeFWHProductionChangeset = "claude-code-2.1.226-fw-h-production"
)

const claudeFWGEgressDispositionInventoryDigest = "2a13ef7d301cd845d501fb21152bbeab3baedaa4c46dbf7ec1343b9bbe867373"

var claudeFWGManagedPolicies = map[SinkID]ManagedEgressPolicy{
	SinkClaudeLegacyAccountTest: {
		ID: "egress-claude-account-test", Authentication: "claude.ai-oauth",
		Endpoint: "POST https://api.anthropic.com/v1/messages", Client: "http_upstream",
	},
	SinkClaudeLegacyCookieAuthorize: {
		ID: "egress-claude-cookie-authorize", Authentication: "claude.ai-session-cookie",
		Endpoint: "POST https://claude.ai/v1/oauth/{organization_uuid}/authorize", Client: "req_profile",
	},
	SinkClaudeLegacyCookieOrganizations: {
		ID: "egress-claude-cookie-organizations", Authentication: "claude.ai-session-cookie",
		Endpoint: "GET https://claude.ai/api/organizations", Client: "req_profile",
	},
	SinkClaudeLegacyMessagesInference: {
		ID: "egress-claude-messages-inference", Authentication: "claude.ai-oauth",
		Endpoint: "POST https://api.anthropic.com/v1/messages", Client: "http_upstream",
	},
	SinkClaudeLegacyOAuthExchange: {
		ID: "egress-claude-oauth-exchange", Authentication: "claude.ai-oauth",
		Endpoint: "POST https://platform.claude.com/v1/oauth/token", Client: "req_profile",
	},
	SinkClaudeLegacyOAuthRefresh: {
		ID: "egress-claude-oauth-refresh", Authentication: "claude.ai-oauth",
		Endpoint: "POST https://platform.claude.com/v1/oauth/token", Client: "req_profile",
	},
	SinkClaudeLegacyTokenCount: {
		ID: "egress-claude-token-count", Authentication: "claude.ai-oauth",
		Endpoint: "POST https://api.anthropic.com/v1/messages/count_tokens", Client: "http_upstream",
	},
	SinkClaudeLegacyUpstreamModels: {
		ID: "egress-claude-upstream-models", Authentication: "claude.ai-oauth",
		Endpoint: "GET https://api.anthropic.com/v1/models", Client: "http_upstream",
	},
	SinkClaudeLegacyUsage: {
		ID: "egress-claude-usage", Authentication: "claude.ai-oauth",
		Endpoint: "GET https://api.anthropic.com/api/oauth/usage", Client: "http_upstream",
	},
}

type claudeCatalogRole struct {
	changeset     string
	owner         string
	expiry        string
	receiptSchema string
}

var (
	claudeCandidateCatalogRole = claudeCatalogRole{
		changeset:     claudeFWGCandidateChangeset,
		owner:         "official-client-fw-g",
		expiry:        "FW-G candidate 被作废、晋升或进入 FW-H 后由后继事实替代",
		receiptSchema: "claude-fw-g-candidate-migration/v1",
	}
	claudeProductionCatalogRole = claudeCatalogRole{
		changeset:     claudeFWHProductionChangeset,
		owner:         "official-client-fw-h",
		expiry:        "正式 Release 被后继生产部署替代或回滚到冻结遗留部署",
		receiptSchema: "claude-fw-h-production-migration/v1",
	}
)

func completeClaudeFWGManagedPolicy(policy ManagedEgressPolicy) ManagedEgressPolicy {
	policy.Source = "egress-disposition-inventory:" + claudeFWGEgressDispositionInventoryDigest
	policy.TimeoutPolicy = "legacy-code-defined"
	policy.RetryPolicy = "legacy-code-defined"
	policy.SecretPolicy = "redacted"
	policy.AuditPolicy = "metadata-only"
	return policy
}

// ClaudeFWGCandidateSinkCatalog 在既有进程 Catalog 上追加隔离候选闭集。
// 调用方只有在显式 candidate 开关开启时才能使用该快照。
func ClaudeFWGCandidateSinkCatalog(base SinkCatalog) (SinkCatalog, error) {
	return buildClaudeSinkCatalog(base, claudeCandidateCatalogRole)
}

// ClaudeProductionSinkCatalog 只为已显式选择 active 的 Claude production Runtime
// 安装正式闭集。它与 ValidationCandidate 使用不同 changeset 和迁移收据。
func ClaudeProductionSinkCatalog(base SinkCatalog) (SinkCatalog, error) {
	return buildClaudeSinkCatalog(base, claudeProductionCatalogRole)
}

func buildClaudeSinkCatalog(base SinkCatalog, role claudeCatalogRole) (SinkCatalog, error) {
	if len(base.bindings) == 0 {
		return SinkCatalog{}, fmt.Errorf("Claude Catalog 缺少基础 SinkCatalog")
	}
	if strings.TrimSpace(role.changeset) == "" || strings.TrimSpace(role.owner) == "" ||
		strings.TrimSpace(role.expiry) == "" || strings.TrimSpace(role.receiptSchema) == "" {
		return SinkCatalog{}, fmt.Errorf("Claude Catalog role 不完整")
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		return SinkCatalog{}, err
	}
	out := SinkCatalog{bindings: make(map[SinkID]SinkBinding, len(base.bindings)+len(profile.endpoints))}
	for id, binding := range base.bindings {
		out.bindings[id] = binding
	}
	for sinkID, rawPolicy := range claudeFWGManagedPolicies {
		legacy, ok := out.bindings[sinkID]
		if !ok || legacy.EnforcementState() != SinkStateLegacyObserve ||
			legacy.Persona() != PersonaUnclassified {
			return SinkCatalog{}, fmt.Errorf("Claude managed Sink 基线不一致：%s", sinkID)
		}
		policy := completeClaudeFWGManagedPolicy(rawPolicy)
		input := SinkBindingInput{
			ID: legacy.ID(), Purpose: legacy.Purpose(), Persona: legacy.Persona(),
			EndpointEvidence: legacy.EndpointEvidence(), Routes: legacy.Routes(),
			TargetBackend: legacy.TargetBackend(), LegacyBackends: legacy.LegacyBackends(),
			EnforcementState: SinkStateEnforced, Owner: role.owner,
			MigrationChangeset: role.changeset,
			ExpiryCondition:    role.expiry,
			RuntimeBindable:    true, ManagedPolicy: &policy,
		}
		binding, err := newSinkBinding(input)
		if err != nil {
			return SinkCatalog{}, err
		}
		out.bindings[sinkID] = binding
	}
	for _, kind := range claudeStrictEndpointKinds() {
		endpoint, err := profile.endpoint(kind)
		if err != nil {
			return SinkCatalog{}, err
		}
		if _, exists := out.bindings[endpoint.sinkID]; exists {
			return SinkCatalog{}, fmt.Errorf("Claude candidate SinkID 已存在：%s", endpoint.sinkID)
		}
		input := SinkBindingInput{
			ID: endpoint.sinkID, Purpose: endpoint.purpose, Persona: PersonaClaudeCode,
			EndpointEvidence:   EndpointEvidenceClaudeProfile,
			Routes:             []CatalogRoute{endpoint.route},
			TargetBackend:      BackendHTTPUpstream,
			LegacyBackends:     []BackendKind{BackendHTTPUpstream},
			EnforcementState:   SinkStateEnforced,
			Owner:              role.owner + "/claude-code",
			MigrationChangeset: role.changeset,
			ExpiryCondition:    role.expiry,
			RuntimeBindable:    true,
		}
		receipt, err := buildClaudeMigrationReceipt(input, endpoint, role.receiptSchema)
		if err != nil {
			return SinkCatalog{}, err
		}
		input.migrationReceipt = &receipt
		binding, err := newSinkBinding(input)
		if err != nil {
			return SinkCatalog{}, err
		}
		out.bindings[binding.ID()] = binding
	}
	return out, nil
}

func buildClaudeMigrationReceipt(
	input SinkBindingInput,
	endpoint claudeEndpointProfile,
	schemaVersion string,
) (MigrationReceipt, error) {
	if strings.TrimSpace(schemaVersion) == "" {
		return MigrationReceipt{}, fmt.Errorf("Claude MigrationReceipt schema 为空")
	}
	bindingDigest, err := sinkBindingIdentityDigest(input)
	if err != nil {
		return MigrationReceipt{}, err
	}
	claim := migrationRouteClaim{
		route: endpoint.route, evidenceKind: "claude_endpoint", evidenceID: endpoint.id,
		backend: BackendHTTPUpstream, adapterID: AdapterHTTPUpstream,
		transportID: endpoint.transportID,
		transportIDsByRelease: map[string]string{
			ClaudeFWGReleaseDigest: endpoint.transportID,
		},
	}
	digestPayload := struct {
		SchemaVersion string                        `json:"schema_version"`
		SinkID        SinkID                        `json:"sink_id"`
		State         SinkEnforcementState          `json:"state"`
		Binding       string                        `json:"binding_digest"`
		Authority     receiptcontract.AuthorityKind `json:"authority_kind"`
		AuthorityID   ExecutorID                    `json:"authority_id"`
		IssuerID      ExecutorID                    `json:"token_issuer_id"`
		EndpointID    string                        `json:"endpoint_id"`
		TransportID   string                        `json:"transport_id"`
	}{
		SchemaVersion: schemaVersion,
		SinkID:        input.ID, State: input.EnforcementState, Binding: bindingDigest,
		Authority:   receiptcontract.AuthorityClaudeExecutor,
		AuthorityID: ClaudeExecutorAuthorityID, IssuerID: ClaudeTokenIssuerID,
		EndpointID: endpoint.id, TransportID: endpoint.transportID,
	}
	raw, err := json.Marshal(digestPayload)
	if err != nil {
		return MigrationReceipt{}, err
	}
	sum := sha256.Sum256(raw)
	return MigrationReceipt{
		digest: hex.EncodeToString(sum[:]), sinkID: input.ID,
		approvedState: input.EnforcementState, bindingDigest: bindingDigest,
		authorityKind: receiptcontract.AuthorityClaudeExecutor,
		authorityID:   ClaudeExecutorAuthorityID, tokenIssuerID: ClaudeTokenIssuerID,
		routeClaims: []migrationRouteClaim{claim},
	}, nil
}

func claudePersonaDescriptorInput() PersonaDescriptorInput {
	return PersonaDescriptorInput{
		Persona: PersonaClaudeCode,
		Identity: PersonaIdentity{
			Provider: "anthropic", OfficialProduct: "claude-code", AuthFamily: "oauth",
			UpstreamRouteFamily: "anthropic-api",
		},
		AuthorityKind:           receiptcontract.AuthorityClaudeExecutor,
		AllowedEndpointEvidence: []EndpointEvidence{EndpointEvidenceClaudeProfile},
		Exclusions: []PersonaExclusion{
			{
				Dimension: PersonaExclusionAuthFamily, Value: "api-key",
				Reason: "API Key 不属于 Claude Code OAuth Persona",
			},
			{
				Dimension: PersonaExclusionAuthFamily, Value: "setup-token",
				Reason: "Setup Token 尚未进入已批准 SupportEnvelope",
			},
			{
				Dimension: PersonaExclusionOfficialProduct, Value: "anthropic-sdk",
				Reason: "通用 Anthropic SDK 不是 Claude Code 官方产品身份",
			},
		},
	}
}

func newClaudePersonaRegistry(sinks SinkCatalog) (PersonaRegistry, error) {
	return NewPersonaRegistry([]PersonaDescriptorInput{claudePersonaDescriptorInput()}, sinks)
}

func claudeEndpointForRoute(
	profile claudeFWGProfile,
	sink SinkBinding,
	route CatalogRoute,
) (claudeEndpointProfile, bool) {
	kinds := append([]string(nil), claudeStrictEndpointKinds()...)
	sort.Strings(kinds)
	for _, kind := range kinds {
		endpoint := profile.endpoints[kind]
		if endpoint.sinkID == sink.ID() && endpoint.route == route {
			return endpoint, true
		}
	}
	return claudeEndpointProfile{}, false
}

func claudeCatalogDigest(catalog SinkCatalog) string {
	parts := make([]string, 0, len(catalog.bindings))
	for _, binding := range catalog.Bindings() {
		parts = append(parts, strings.Join([]string{
			string(binding.ID()), string(binding.Persona()), binding.EnforcementIdentityDigest(),
		}, "\x00"))
	}
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}
