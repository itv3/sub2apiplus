package officialegress

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

const claudeFWGWireDigest = "a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d"

//go:embed catalogdata/claude/wire/2.1.226/a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d.json
var embeddedClaudeFWGWire []byte

type claudeWireTLSVector struct {
	CipherSuites        []uint16 `json:"cipher_suites"`
	SupportedGroups     []uint16 `json:"supported_groups"`
	SignatureAlgorithms []uint16 `json:"signature_algorithms"`
	ALPN                []string `json:"alpn"`
	Extensions          []uint16 `json:"extensions"`
	SupportedVersions   []uint16 `json:"supported_versions"`
	KeyShareGroups      []uint16 `json:"key_share_groups"`
	PSKModes            []uint16 `json:"psk_modes"`
	TLSMinVersion       uint16   `json:"tls_min_version"`
	TLSMaxVersion       uint16   `json:"tls_max_version"`
}

type claudeWireSystemBlock struct {
	Type         string          `json:"type"`
	Text         string          `json:"text"`
	CacheControl json.RawMessage `json:"cache_control,omitempty"`
}

type claudeWireScenario struct {
	Evidence struct {
		Path      string `json:"path"`
		RawSHA256 string `json:"raw_sha256"`
	} `json:"evidence"`
	Model                    string   `json:"model"`
	MaxTokensJSON            string   `json:"max_tokens_json"`
	ThinkingJSON             string   `json:"thinking_json"`
	ContextManagementJSON    string   `json:"context_management_json"`
	FallbacksJSON            string   `json:"fallbacks_json"`
	OutputConfigJSON         string   `json:"output_config_json"`
	TemperatureJSON          string   `json:"temperature_json"`
	StreamJSON               string   `json:"stream_json"`
	ToolsJSON                string   `json:"tools_json"`
	SystemBlocksJSON         string   `json:"system_blocks_json"`
	ThinkingPresent          bool     `json:"thinking_present"`
	ContextManagementPresent bool     `json:"context_management_present"`
	FallbacksPresent         bool     `json:"fallbacks_present"`
	OutputConfigPresent      bool     `json:"output_config_present"`
	TemperaturePresent       bool     `json:"temperature_present"`
	StreamPresent            bool     `json:"stream_present"`
	ToolsPresent             bool     `json:"tools_present"`
	SystemPresent            bool     `json:"system_present"`
	MetadataPresent          bool     `json:"metadata_present"`
	UserAgent                string   `json:"user_agent"`
	AnthropicBeta            string   `json:"anthropic_beta"`
	XApp                     string   `json:"x_app"`
	BodyOrder                []string `json:"body_order"`
	HeaderOrder              []string `json:"header_order"`
	FallbackLatchedByPresent bool     `json:"fallback_latched_by_present"`
	RefusalFallbackValue     string   `json:"refusal_fallback_value"`

	MaxTokens         json.RawMessage         `json:"-"`
	Thinking          json.RawMessage         `json:"-"`
	ContextManagement json.RawMessage         `json:"-"`
	Fallbacks         json.RawMessage         `json:"-"`
	OutputConfig      json.RawMessage         `json:"-"`
	Temperature       json.RawMessage         `json:"-"`
	Stream            json.RawMessage         `json:"-"`
	Tools             json.RawMessage         `json:"-"`
	SystemBlocks      []claudeWireSystemBlock `json:"-"`
}

type claudeWireScenarioSet struct {
	SDKCLI                claudeWireScenario   `json:"sdk_cli"`
	Agent                 claudeWireScenario   `json:"agent"`
	TUIMain               claudeWireScenario   `json:"tui_main"`
	TUITitle              claudeWireScenario   `json:"tui_title"`
	Fallback              claudeWireScenario   `json:"fallback"`
	Background            []claudeWireScenario `json:"background"`
	CustomSystem          claudeWireScenario   `json:"custom_system"`
	AppendSystem          claudeWireScenario   `json:"append_system"`
	ExcludeDynamic        claudeWireScenario   `json:"exclude_dynamic"`
	CustomAgent           claudeWireScenario   `json:"custom_agent"`
	SDKCLIBackgroundAgent claudeWireScenario   `json:"sdk_cli_background_agent"`
	AgentBackground       claudeWireScenario   `json:"agent_background"`
	WebSearchOuter        claudeWireScenario   `json:"web_search_outer"`
	WebSearchServer       claudeWireScenario   `json:"web_search_server"`
	ServerFallback        *claudeWireScenario  `json:"server_fallback"`
}

type claudeWireModelEvidence struct {
	Path      string `json:"path"`
	RawSHA256 string `json:"raw_sha256"`
}

type claudeWireModelCapability struct {
	CanonicalModel               string                             `json:"canonical_model"`
	Aliases                      []string                           `json:"aliases"`
	EffortValues                 []string                           `json:"effort_values"`
	EffortEvidence               map[string]claudeWireModelEvidence `json:"effort_evidence"`
	ThinkingDisableEvidence      claudeWireModelEvidence            `json:"thinking_disable_evidence"`
	CountTokensEvidence          claudeWireModelEvidence            `json:"count_tokens_evidence"`
	LegacyRetryFallbackSupported bool                               `json:"legacy_retry_fallback_supported"`
	Scenarios                    claudeWireScenarioSet              `json:"scenarios"`
}

type claudeWireModelCatalog struct {
	SchemaVersion      string `json:"schema_version"`
	RequiredRuleCount  int    `json:"required_rule_count"`
	UnknownModelPolicy string `json:"unknown_model_policy"`
	AliasPolicy        string `json:"alias_policy"`
	Evidence           struct {
		CampaignRoot               string `json:"campaign_root"`
		SuccessfulAttempts         int    `json:"successful_attempts"`
		HistoricalFailedAttempts   int    `json:"historical_failed_attempts"`
		CampaignSHA256             string `json:"campaign_sha256"`
		BehaviorTrafficDisposition string `json:"behavior_traffic_disposition"`
		ProductionDiff             struct {
			Path      string `json:"path"`
			SHA256    string `json:"sha256"`
			Identical bool   `json:"identical"`
		} `json:"production_diff"`
	} `json:"evidence"`
	Models []claudeWireModelCapability `json:"models"`
}

type claudeWireToolCatalog struct {
	Evidence struct {
		Path      string `json:"path"`
		RawSHA256 string `json:"raw_sha256"`
	} `json:"evidence"`
	ToolsJSON      string `json:"tools_json"`
	ToolChoiceJSON string `json:"tool_choice_json"`
	AnthropicBeta  string `json:"anthropic_beta"`

	Tools      json.RawMessage `json:"-"`
	ToolChoice json.RawMessage `json:"-"`
}

type claudeWireToolPolicy struct {
	SchemaVersion     string                `json:"schema_version"`
	UnknownToolPolicy string                `json:"unknown_tool_policy"`
	StructuredOutput  claudeWireToolCatalog `json:"structured_output"`
	Agent             claudeWireToolCatalog `json:"agent"`
	Bash              claudeWireToolCatalog `json:"bash"`
	MCPDeferred       claudeWireToolCatalog `json:"mcp_deferred"`
	Advisor           claudeWireToolCatalog `json:"advisor"`
	Background        claudeWireToolCatalog `json:"background"`
	WebSearchOuter    claudeWireToolCatalog `json:"web_search_outer"`
	WebSearchServer   claudeWireToolCatalog `json:"web_search_server"`
}

type claudeWireThinkingEvidence struct {
	Path      string `json:"path"`
	RawSHA256 string `json:"raw_sha256,omitempty"`
	SHA256    string `json:"sha256,omitempty"`
}

type claudeWireThinkingPolicy struct {
	SchemaVersion string                                `json:"schema_version"`
	Type          string                                `json:"type"`
	FieldOrder    []string                              `json:"field_order"`
	DisplayValues []string                              `json:"display_values"`
	Evidence      map[string]claudeWireThinkingEvidence `json:"evidence"`
}

type claudeWireImplementationPolicy struct {
	SchemaVersion  string `json:"schema_version"`
	EvidenceLedger struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"evidence_ledger"`
	Scenarios    claudeWireScenarioSet  `json:"scenarios"`
	ModelCatalog claudeWireModelCatalog `json:"model_catalog"`
	Identity     struct {
		DeviceIDAlgorithm string   `json:"device_id_algorithm"`
		SessionSources    []string `json:"session_sources"`
		AgentIDPattern    string   `json:"agent_id_pattern"`
		MetadataOrder     []string `json:"metadata_order"`
	} `json:"identity"`
	Headers struct {
		CustomInsertAfter string   `json:"custom_insert_after"`
		ProtectedNames    []string `json:"protected_names"`
		ConditionalOrder  []string `json:"conditional_order"`
	} `json:"headers"`
	Retry struct {
		RetryableStatuses            []int  `json:"retryable_statuses"`
		NonRetryableStatuses         []int  `json:"non_retryable_statuses"`
		DefaultBaseMS                []int  `json:"default_base_ms"`
		DefaultJitterMaxMS           int    `json:"default_jitter_max_ms"`
		RetryAfterSecondsJitterMaxMS int    `json:"retry_after_seconds_jitter_max_ms"`
		HTTPDatePolicy               string `json:"http_date_policy"`
		DefaultMaxRetries            int    `json:"default_max_retries"`
		RetryCountHeader             string `json:"retry_count_header"`
		StreamTimeoutSeconds         int    `json:"stream_timeout_seconds"`
		NonStreamTimeoutSeconds      int    `json:"non_stream_timeout_seconds"`
	} `json:"retry"`
	Thinking   claudeWireThinkingPolicy `json:"thinking"`
	ToolPolicy claudeWireToolPolicy     `json:"tool_policy"`
}

type claudeWireArtifact struct {
	SchemaVersion string `json:"schema_version"`
	Identity      struct {
		Version                      string `json:"version"`
		Platform                     string `json:"platform"`
		ProfileDigest                string `json:"profile_digest"`
		ModelCapabilityCatalogDigest string `json:"model_capability_catalog_digest"`
	} `json:"identity"`
	Attestation struct {
		VersionFingerprint struct {
			Algorithm      string `json:"algorithm"`
			Salt           string `json:"salt"`
			MessageIndexes []int  `json:"message_indexes"`
			HexLength      int    `json:"hex_length"`
		} `json:"version_fingerprint"`
		CCH struct {
			Algorithm string `json:"algorithm"`
			HexLength int    `json:"hex_length"`
			Lifecycle string `json:"lifecycle"`
		} `json:"cch"`
	} `json:"attestation"`
	Messages struct {
		DefaultBeta      string                  `json:"default_beta"`
		SystemBlocksJSON string                  `json:"system_blocks_json"`
		SystemBlocks     []claudeWireSystemBlock `json:"-"`
		SystemSHA256     []string                `json:"system_text_sha256"`
	} `json:"messages"`
	ImplementationPolicy claudeWireImplementationPolicy `json:"implementation_policy"`
	Transports           struct {
		WithALPN    claudeWireTLSVector `json:"http1_with_alpn"`
		WithoutALPN claudeWireTLSVector `json:"http1_without_alpn"`
	} `json:"transports"`
}

func loadClaudeFWGWire() (claudeWireArtifact, error) {
	sum := sha256.Sum256(embeddedClaudeFWGWire)
	if hex.EncodeToString(sum[:]) != claudeFWGWireDigest {
		return claudeWireArtifact{}, errors.New("Claude FW-G wire 制品原文字节摘要不匹配")
	}
	var artifact claudeWireArtifact
	if err := json.Unmarshal(embeddedClaudeFWGWire, &artifact); err != nil {
		return claudeWireArtifact{}, fmt.Errorf("解析 Claude FW-G wire 制品：%w", err)
	}
	var rawCatalog struct {
		ImplementationPolicy struct {
			ModelCatalog json.RawMessage `json:"model_catalog"`
		} `json:"implementation_policy"`
	}
	if err := json.Unmarshal(embeddedClaudeFWGWire, &rawCatalog); err != nil ||
		!json.Valid(rawCatalog.ImplementationPolicy.ModelCatalog) {
		return claudeWireArtifact{}, errors.New("Claude 模型能力目录原文缺失")
	}
	catalogRaw := append(append([]byte(nil), rawCatalog.ImplementationPolicy.ModelCatalog...), '\n')
	catalogSum := sha256.Sum256(catalogRaw)
	if artifact.SchemaVersion != "claude-code-fw-g-wire-artifact/v3" ||
		artifact.Identity.Version != ClaudeFWGVersion ||
		artifact.Identity.Platform != "linux/amd64" ||
		artifact.Identity.ProfileDigest != ClaudeFWGProfileDigest ||
		hex.EncodeToString(catalogSum[:]) != artifact.Identity.ModelCapabilityCatalogDigest {
		return claudeWireArtifact{}, errors.New("Claude FW-G wire 制品身份不一致")
	}
	if err := hydrateClaudeWireArtifact(&artifact); err != nil {
		return claudeWireArtifact{}, err
	}
	if artifact.Attestation.VersionFingerprint.Algorithm != "sha256-salt-message-index-v1" ||
		artifact.Attestation.VersionFingerprint.Salt == "" ||
		len(artifact.Attestation.VersionFingerprint.MessageIndexes) == 0 ||
		artifact.Attestation.VersionFingerprint.HexLength != 3 ||
		artifact.Attestation.CCH.Algorithm != "crypto-random-20-bit-nonce" ||
		artifact.Attestation.CCH.HexLength != 5 ||
		artifact.Attestation.CCH.Lifecycle != "per-logical-request-reused-by-transport-retry" {
		return claudeWireArtifact{}, errors.New("Claude FW-G attribution 派生合同不一致")
	}
	if strings.TrimSpace(artifact.Messages.DefaultBeta) == "" ||
		len(artifact.Messages.SystemBlocks) != 3 ||
		len(artifact.Messages.SystemSHA256) != len(artifact.Messages.SystemBlocks) {
		return claudeWireArtifact{}, errors.New("Claude FW-G messages 静态 wire 不完整")
	}
	if err := validateClaudeImplementationPolicy(artifact.ImplementationPolicy); err != nil {
		return claudeWireArtifact{}, err
	}
	for index, block := range artifact.Messages.SystemBlocks {
		textDigest := sha256.Sum256([]byte(block.Text))
		if block.Type != "text" || block.Text == "" ||
			hex.EncodeToString(textDigest[:]) != artifact.Messages.SystemSHA256[index] {
			return claudeWireArtifact{}, fmt.Errorf("Claude FW-G system[%d] 摘要不一致", index)
		}
	}
	if err := validateClaudeTLSVector(artifact.Transports.WithALPN, true); err != nil {
		return claudeWireArtifact{}, err
	}
	if err := validateClaudeTLSVector(artifact.Transports.WithoutALPN, false); err != nil {
		return claudeWireArtifact{}, err
	}
	return artifact, nil
}

func validateClaudeImplementationPolicy(policy claudeWireImplementationPolicy) error {
	if policy.SchemaVersion != "claude-code-fw-g-implementation-policy/v3" ||
		len(policy.EvidenceLedger.SHA256) != sha256.Size*2 ||
		policy.Identity.DeviceIDAlgorithm != "sha256-account-scope-ingress-binding-v1" ||
		policy.Identity.AgentIDPattern != "^[0-9a-f]{17}$" {
		return errors.New("Claude FW-G 实现策略身份不一致")
	}
	for name, scenario := range map[string]claudeWireScenario{
		"sdk-cli": policy.Scenarios.SDKCLI, "agent": policy.Scenarios.Agent,
		"tui-main": policy.Scenarios.TUIMain, "tui-title": policy.Scenarios.TUITitle,
		"fallback": policy.Scenarios.Fallback, "custom-system": policy.Scenarios.CustomSystem,
		"append-system":     policy.Scenarios.AppendSystem,
		"exclude-dynamic":   policy.Scenarios.ExcludeDynamic,
		"custom-agent":      policy.Scenarios.CustomAgent,
		"web-search-outer":  policy.Scenarios.WebSearchOuter,
		"web-search-server": policy.Scenarios.WebSearchServer,
	} {
		if scenario.Model == "" || scenario.UserAgent == "" || scenario.AnthropicBeta == "" ||
			len(scenario.Evidence.RawSHA256) != sha256.Size*2 {
			return fmt.Errorf("Claude FW-G 场景策略不完整：%s", name)
		}
	}
	if len(policy.Scenarios.Background) == 0 ||
		len(policy.Retry.RetryableStatuses) != 8 ||
		len(policy.Retry.NonRetryableStatuses) != 2 ||
		len(policy.Retry.DefaultBaseMS) != 2 ||
		policy.Retry.DefaultMaxRetries != 2 ||
		policy.Retry.RetryCountHeader != "0" ||
		policy.Retry.StreamTimeoutSeconds != 600 ||
		policy.Retry.NonStreamTimeoutSeconds != 300 {
		return errors.New("Claude FW-G 重试或 background 策略不完整")
	}
	if err := validateClaudeToolPolicy(policy.ToolPolicy); err != nil {
		return err
	}
	if err := validateClaudeThinkingPolicy(policy.Thinking); err != nil {
		return err
	}
	if err := validateClaudeModelCatalog(policy.ModelCatalog, policy.Scenarios); err != nil {
		return err
	}
	return nil
}

func validateClaudeModelCatalog(
	catalog claudeWireModelCatalog,
	base claudeWireScenarioSet,
) error {
	if catalog.SchemaVersion != "claude-code-model-capability-catalog/v1" ||
		catalog.RequiredRuleCount != 40 || catalog.UnknownModelPolicy != "deny" ||
		catalog.AliasPolicy != "explicit-only" || len(catalog.Models) != 3 ||
		catalog.Evidence.SuccessfulAttempts != 42 ||
		catalog.Evidence.HistoricalFailedAttempts != 3 ||
		len(catalog.Evidence.CampaignSHA256) != sha256.Size*2 ||
		catalog.Evidence.BehaviorTrafficDisposition !=
			"excluded-from-rules-and-difference-judgement" ||
		!catalog.Evidence.ProductionDiff.Identical ||
		len(catalog.Evidence.ProductionDiff.SHA256) != sha256.Size*2 {
		return errors.New("Claude 模型能力目录身份或证据摘要不完整")
	}
	wantModels := []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}
	aliases := make(map[string]string, len(wantModels))
	for index := range catalog.Models {
		capability := catalog.Models[index]
		if capability.CanonicalModel != wantModels[index] ||
			!slices.Equal(capability.Aliases, []string{capability.CanonicalModel}) ||
			!slices.Equal(capability.EffortValues, []string{"low", "medium", "high", "xhigh", "max"}) ||
			len(capability.EffortEvidence) != 5 ||
			!validClaudeModelEvidence(capability.ThinkingDisableEvidence) ||
			!validClaudeModelEvidence(capability.CountTokensEvidence) {
			return fmt.Errorf("Claude 模型能力不完整：%s", capability.CanonicalModel)
		}
		for _, effort := range capability.EffortValues {
			if !validClaudeModelEvidence(capability.EffortEvidence[effort]) {
				return fmt.Errorf("Claude 模型 effort 证据不完整：%s/%s", capability.CanonicalModel, effort)
			}
		}
		for _, alias := range capability.Aliases {
			if owner := aliases[alias]; owner != "" {
				return fmt.Errorf("Claude 模型别名重复：%s（%s/%s）", alias, owner, capability.CanonicalModel)
			}
			aliases[alias] = capability.CanonicalModel
		}
		if err := validateClaudeModelScenarioSet(capability); err != nil {
			return err
		}
		if (capability.CanonicalModel == "claude-sonnet-5") !=
			capability.LegacyRetryFallbackSupported {
			return fmt.Errorf("Claude 历史 Haiku fallback 能力归属非法：%s", capability.CanonicalModel)
		}
	}
	if catalog.Models[0].Scenarios.SDKCLI.Model != base.SDKCLI.Model ||
		catalog.Models[0].Scenarios.SDKCLI.Evidence.RawSHA256 != base.SDKCLI.Evidence.RawSHA256 {
		return errors.New("Claude Sonnet 模型能力未复用公共基线场景")
	}
	return nil
}

func validClaudeModelEvidence(evidence claudeWireModelEvidence) bool {
	return strings.TrimSpace(evidence.Path) != "" && len(evidence.RawSHA256) == sha256.Size*2
}

func validateClaudeModelScenarioSet(capability claudeWireModelCapability) error {
	scenarios := capability.Scenarios
	for name, scenario := range map[string]claudeWireScenario{
		"sdk-cli":           scenarios.SDKCLI,
		"agent":             scenarios.Agent,
		"tui-main":          scenarios.TUIMain,
		"custom-system":     scenarios.CustomSystem,
		"append-system":     scenarios.AppendSystem,
		"exclude-dynamic":   scenarios.ExcludeDynamic,
		"custom-agent":      scenarios.CustomAgent,
		"web-search-outer":  scenarios.WebSearchOuter,
		"web-search-server": scenarios.WebSearchServer,
	} {
		if scenario.Model != capability.CanonicalModel || !validClaudeWireScenario(scenario) {
			return fmt.Errorf("Claude 模型场景不完整：%s/%s", capability.CanonicalModel, name)
		}
	}
	if len(scenarios.Background) != 4 {
		return fmt.Errorf("Claude 模型 background 未冻结为四类：%s", capability.CanonicalModel)
	}
	primaryBackground := 0
	for _, scenario := range scenarios.Background {
		if !validClaudeWireScenario(scenario) {
			return fmt.Errorf("Claude 模型 background 场景不完整：%s", capability.CanonicalModel)
		}
		if scenario.Model == capability.CanonicalModel {
			primaryBackground++
		}
	}
	if primaryBackground != 1 {
		return fmt.Errorf("Claude 模型 background 主模型场景数量非法：%s", capability.CanonicalModel)
	}
	if capability.CanonicalModel == "claude-sonnet-5" {
		if capability.Scenarios.ServerFallback != nil {
			return errors.New("Claude Sonnet 不得声明 server fallback 锁存场景")
		}
		return nil
	}
	for name, scenario := range map[string]claudeWireScenario{
		"sdk-cli-background-agent": scenarios.SDKCLIBackgroundAgent,
		"agent-background":         scenarios.AgentBackground,
	} {
		if scenario.Model != capability.CanonicalModel || !validClaudeWireScenario(scenario) {
			return fmt.Errorf("Claude 模型场景不完整：%s/%s", capability.CanonicalModel, name)
		}
	}
	if capability.CanonicalModel == "claude-opus-5" {
		if scenarios.ServerFallback != nil {
			return errors.New("Claude Opus 不得声明 Fable server fallback 锁存场景")
		}
		return nil
	}
	if scenarios.ServerFallback == nil || scenarios.ServerFallback.Model != "claude-opus-4-8" ||
		!validClaudeWireScenario(*scenarios.ServerFallback) || scenarios.ServerFallback.FallbacksPresent ||
		!scenarios.ServerFallback.FallbackLatchedByPresent ||
		scenarios.ServerFallback.RefusalFallbackValue != "true" {
		return errors.New("Claude Fable server fallback 锁存场景不完整")
	}
	return nil
}

func validClaudeWireScenario(scenario claudeWireScenario) bool {
	return strings.TrimSpace(scenario.Model) != "" && strings.TrimSpace(scenario.UserAgent) != "" &&
		strings.TrimSpace(scenario.AnthropicBeta) != "" &&
		len(scenario.Evidence.RawSHA256) == sha256.Size*2 &&
		strings.TrimSpace(scenario.Evidence.Path) != "" &&
		len(scenario.BodyOrder) != 0 && len(scenario.HeaderOrder) != 0 &&
		slices.Contains(scenario.BodyOrder, "fallbacks") == scenario.FallbacksPresent
}

func claudeModelCapabilityForAlias(
	artifact claudeWireArtifact,
	model string,
) (claudeWireModelCapability, bool) {
	model = strings.TrimSpace(model)
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		if slices.Contains(capability.Aliases, model) {
			return capability, true
		}
	}
	return claudeWireModelCapability{}, false
}

func claudeModelCapabilityForServerFallback(
	artifact claudeWireArtifact,
	model string,
) (claudeWireModelCapability, bool) {
	model = strings.TrimSpace(model)
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		if capability.Scenarios.ServerFallback != nil &&
			capability.Scenarios.ServerFallback.Model == model {
			return capability, true
		}
	}
	return claudeWireModelCapability{}, false
}

func validateClaudeThinkingPolicy(policy claudeWireThinkingPolicy) error {
	if policy.SchemaVersion != "claude-code-thinking-display-policy/v1" ||
		policy.Type != "adaptive" ||
		!slices.Equal(policy.FieldOrder, []string{"type", "display"}) ||
		!slices.Equal(policy.DisplayValues, []string{"summarized", "omitted"}) ||
		len(policy.Evidence) != 4 {
		return errors.New("Claude thinking.display 画像策略不完整")
	}
	for _, name := range []string{
		"default", "summarized", "omitted", "production_change_receipt",
	} {
		evidence, ok := policy.Evidence[name]
		digest := evidence.RawSHA256
		if name == "production_change_receipt" {
			digest = evidence.SHA256
		}
		if !ok || strings.TrimSpace(evidence.Path) == "" || len(digest) != sha256.Size*2 {
			return fmt.Errorf("Claude thinking.display 证据不完整：%s", name)
		}
	}
	return nil
}

func hydrateClaudeWireArtifact(artifact *claudeWireArtifact) error {
	if artifact == nil {
		return errors.New("Claude FW-G wire 制品为空")
	}
	if err := decodeClaudeWireJSON(
		artifact.Messages.SystemBlocksJSON, &artifact.Messages.SystemBlocks,
	); err != nil {
		return fmt.Errorf("解析 Claude messages system wire：%w", err)
	}
	scenarios := []*claudeWireScenario{
		&artifact.ImplementationPolicy.Scenarios.SDKCLI,
		&artifact.ImplementationPolicy.Scenarios.Agent,
		&artifact.ImplementationPolicy.Scenarios.TUIMain,
		&artifact.ImplementationPolicy.Scenarios.TUITitle,
		&artifact.ImplementationPolicy.Scenarios.Fallback,
		&artifact.ImplementationPolicy.Scenarios.CustomSystem,
		&artifact.ImplementationPolicy.Scenarios.AppendSystem,
		&artifact.ImplementationPolicy.Scenarios.ExcludeDynamic,
		&artifact.ImplementationPolicy.Scenarios.CustomAgent,
		&artifact.ImplementationPolicy.Scenarios.WebSearchOuter,
		&artifact.ImplementationPolicy.Scenarios.WebSearchServer,
	}
	for index := range artifact.ImplementationPolicy.Scenarios.Background {
		scenarios = append(scenarios, &artifact.ImplementationPolicy.Scenarios.Background[index])
	}
	for modelIndex := range artifact.ImplementationPolicy.ModelCatalog.Models {
		set := &artifact.ImplementationPolicy.ModelCatalog.Models[modelIndex].Scenarios
		scenarios = append(scenarios,
			&set.SDKCLI, &set.Agent, &set.TUIMain, &set.TUITitle, &set.Fallback,
			&set.CustomSystem, &set.AppendSystem, &set.ExcludeDynamic, &set.CustomAgent,
			&set.SDKCLIBackgroundAgent, &set.AgentBackground,
			&set.WebSearchOuter, &set.WebSearchServer,
		)
		for index := range set.Background {
			scenarios = append(scenarios, &set.Background[index])
		}
		if set.ServerFallback != nil {
			scenarios = append(scenarios, set.ServerFallback)
		}
	}
	for _, scenario := range scenarios {
		if !validClaudeWireScenario(*scenario) {
			continue
		}
		if err := hydrateClaudeWireScenario(scenario); err != nil {
			return err
		}
	}
	policy := &artifact.ImplementationPolicy.ToolPolicy
	for _, catalog := range []*claudeWireToolCatalog{
		&policy.StructuredOutput, &policy.Agent, &policy.Bash, &policy.MCPDeferred,
		&policy.Advisor, &policy.Background, &policy.WebSearchOuter, &policy.WebSearchServer,
	} {
		if err := decodeClaudeWireRaw(catalog.ToolsJSON, &catalog.Tools); err != nil {
			return fmt.Errorf("解析 Claude ToolPolicy tools：%w", err)
		}
		if err := decodeClaudeWireRaw(catalog.ToolChoiceJSON, &catalog.ToolChoice); err != nil {
			return fmt.Errorf("解析 Claude ToolPolicy tool_choice：%w", err)
		}
	}
	return nil
}

func hydrateClaudeWireScenario(scenario *claudeWireScenario) error {
	if scenario == nil {
		return errors.New("Claude Wire 场景为空")
	}
	for _, item := range []struct {
		name string
		raw  string
		out  *json.RawMessage
	}{
		{"max_tokens", scenario.MaxTokensJSON, &scenario.MaxTokens},
		{"thinking", scenario.ThinkingJSON, &scenario.Thinking},
		{"context_management", scenario.ContextManagementJSON, &scenario.ContextManagement},
		{"fallbacks", scenario.FallbacksJSON, &scenario.Fallbacks},
		{"output_config", scenario.OutputConfigJSON, &scenario.OutputConfig},
		{"temperature", scenario.TemperatureJSON, &scenario.Temperature},
		{"stream", scenario.StreamJSON, &scenario.Stream},
		{"tools", scenario.ToolsJSON, &scenario.Tools},
	} {
		if err := decodeClaudeWireRaw(item.raw, item.out); err != nil {
			return fmt.Errorf("解析 Claude 场景 %s：%w", item.name, err)
		}
	}
	if err := decodeClaudeWireJSON(scenario.SystemBlocksJSON, &scenario.SystemBlocks); err != nil {
		return fmt.Errorf("解析 Claude 场景 system：%w", err)
	}
	return nil
}

func decodeClaudeWireRaw(source string, target *json.RawMessage) error {
	if target == nil || strings.TrimSpace(source) == "" || !json.Valid([]byte(source)) {
		return errors.New("wire JSON 字符串非法")
	}
	*target = append((*target)[:0], source...)
	return nil
}

func decodeClaudeWireJSON(source string, target any) error {
	if strings.TrimSpace(source) == "" || !json.Valid([]byte(source)) {
		return errors.New("wire JSON 字符串非法")
	}
	return json.Unmarshal([]byte(source), target)
}

func validateClaudeToolPolicy(policy claudeWireToolPolicy) error {
	if policy.SchemaVersion != "claude-code-fw-g-tool-policy/v1" ||
		policy.UnknownToolPolicy != "deny" {
		return errors.New("Claude ToolPolicy 身份或未知工具策略非法")
	}
	for name, catalog := range map[string]claudeWireToolCatalog{
		"structured-output": policy.StructuredOutput, "agent": policy.Agent,
		"bash": policy.Bash, "mcp-deferred": policy.MCPDeferred,
		"advisor": policy.Advisor, "background": policy.Background,
		"web-search-outer": policy.WebSearchOuter, "web-search-server": policy.WebSearchServer,
	} {
		if len(catalog.Evidence.RawSHA256) != sha256.Size*2 ||
			strings.TrimSpace(catalog.Evidence.Path) == "" || !rawJSONArray(catalog.Tools) ||
			strings.TrimSpace(catalog.AnthropicBeta) == "" {
			return fmt.Errorf("Claude ToolPolicy 目录不完整：%s", name)
		}
	}
	return nil
}

func validateClaudeTLSVector(vector claudeWireTLSVector, withALPN bool) error {
	if len(vector.CipherSuites) == 0 || len(vector.SupportedGroups) == 0 ||
		len(vector.SignatureAlgorithms) == 0 || len(vector.Extensions) == 0 ||
		len(vector.SupportedVersions) == 0 || len(vector.KeyShareGroups) == 0 ||
		len(vector.PSKModes) == 0 || vector.TLSMinVersion != 771 || vector.TLSMaxVersion != 772 {
		return errors.New("Claude FW-G TLS 向量不完整")
	}
	if withALPN {
		if len(vector.ALPN) != 1 || vector.ALPN[0] != "http/1.1" || !containsUint16(vector.Extensions, 16) {
			return errors.New("Claude FW-G ALPN TLS 向量不一致")
		}
	} else if len(vector.ALPN) != 0 || containsUint16(vector.Extensions, 16) {
		return errors.New("Claude FW-G no-ALPN TLS 向量不一致")
	}
	return nil
}

func containsUint16(values []uint16, target uint16) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func claudeTLSProfileSpec(
	artifact claudeWireArtifact,
	endpoint claudeEndpointProfile,
	headerOrder []string,
) TLSProfileSpec {
	vector := artifact.Transports.WithoutALPN
	if endpoint.withALPN {
		vector = artifact.Transports.WithALPN
	}
	return TLSProfileSpec{
		Stack:               "node-v26.3.0-openssl",
		CipherSuites:        append([]uint16(nil), vector.CipherSuites...),
		SupportedGroups:     append([]uint16(nil), vector.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), vector.SignatureAlgorithms...),
		ALPN:                append([]string(nil), vector.ALPN...),
		Extensions:          append([]uint16(nil), vector.Extensions...),
		SupportedVersions:   append([]uint16(nil), vector.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), vector.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), vector.PSKModes...),
		MinVersion:          vector.TLSMinVersion,
		MaxVersion:          vector.TLSMaxVersion,
		PreserveHeaderCase:  append([]string(nil), headerOrder...),
		H1HeaderOrders: []H1HeaderOrderRule{{
			Method: endpoint.method, Path: endpoint.target.EscapedPath(),
			Order: append([]string(nil), headerOrder...), RejectUnlisted: true,
		}},
		StrictH1Wire: true,
	}
}

func claudeResourceLifecycle() profilecontract.ResourceLifecyclePolicy {
	return profilecontract.ResourceLifecyclePolicy{
		Lifecycle:         profilecontract.LifecycleBackendClientLongLived,
		Scope:             profilecontract.ResourceScopeAccountTransport,
		RetryReusesClient: true,
	}
}
