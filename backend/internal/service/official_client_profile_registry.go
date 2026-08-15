package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

const (
	officialClientProfileModeActive   = "active"
	officialClientProfileModePrevious = "previous"

	officialClientPurposeAnthropicOAuthMessagesHTTP       = "anthropic_oauth_messages_http"
	officialClientPurposeAnthropicAPIKeyMessagesHTTP      = "anthropic_apikey_messages_http"
	officialClientPurposeAnthropicAPIKeyCountTokensCompat = "anthropic_apikey_count_tokens_generic"
	officialClientPurposeOpenAIOAuthResponsesHTTP         = "openai_oauth_responses_http"
	officialClientPurposeOpenAIOAuthResponsesWS           = "openai_oauth_responses_ws"
	officialClientPurposeOpenAIAPIKeyResponsesHTTP        = "openai_apikey_responses_http"
	officialClientPurposeOpenAIAPIKeyResponsesWS          = "openai_apikey_responses_ws"

	officialClientBuildAnthropicCLI21220 = "anthropic_claude_code_2_1_220_linux_x64_node_26_3_0"
	officialClientBuildAnthropicCLI21218 = "anthropic_claude_code_2_1_218_linux_x64_node_26_3_0"
	officialClientBuildAnthropicDesktop  = "anthropic_claude_desktop_2_1_209_macos_arm64_node_26_3_0"

	// Anthropic 当前发布画像的传输边界，供连接池与兼容性测试使用。
	officialEgressTransportProfileAnthropicHTTP = "anthropic-http-claude-code-2.1.220-direct"
)

const (
	AnthropicAPIKeyBetaThinkingTokenCount    = "thinking-token-count-2026-05-13"
	AnthropicAPIKeyBetaMidConversationSystem = "mid-conversation-system-2026-04-07"
	AnthropicAPIKeyBetaEffort                = "effort-2025-11-24"
	AnthropicAPIKeyBetaStructuredOutputs     = "structured-outputs-2025-12-15"
	AnthropicAPIKeyBetaFallbackCredit        = "fallback-credit-2026-06-01"
)

type officialClientHeaderValue struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// officialClientBuild 保存同一客户端构建在不同认证和端点间可共享的静态身份。
type officialClientBuild struct {
	ID             string                      `json:"id"`
	Provider       string                      `json:"provider"`
	Product        string                      `json:"product"`
	Surface        string                      `json:"surface"`
	Version        string                      `json:"version"`
	UserAgent      string                      `json:"user_agent"`
	Originator     string                      `json:"originator,omitempty"`
	RuntimeHeaders []officialClientHeaderValue `json:"runtime_headers,omitempty"`
	Source         string                      `json:"source"`
}

// officialClientWireProfile 保存认证、端点、传输和网络形态专属的最终静态画像。
// Body 的动态语义仍由各自 Finalizer 负责，不能数据化后绕过终态校验。
type officialClientWireProfile struct {
	ID                 string                      `json:"id"`
	Purpose            string                      `json:"purpose"`
	BuildID            string                      `json:"build_id"`
	AuthMode           string                      `json:"auth_mode"`
	Endpoint           string                      `json:"endpoint"`
	Transport          string                      `json:"transport"`
	NetworkVariant     string                      `json:"network_variant"`
	StaticHeaders      []officialClientHeaderValue `json:"static_headers,omitempty"`
	BetaHeader         string                      `json:"beta_header,omitempty"`
	TransportProfileID string                      `json:"transport_profile_id"`
	Source             string                      `json:"source"`
	Digest             string                      `json:"digest"`
}

type officialClientResolvedProfile struct {
	Wire  officialClientWireProfile
	Build officialClientBuild
}

type officialClientReleasePointer struct {
	Active   string
	Previous string
}

type anthropicClientProfileCatalog struct {
	builds   map[string]officialClientBuild
	profiles map[string]officialClientWireProfile
	releases map[string]officialClientReleasePointer
}

var defaultAnthropicClientProfileCatalog = mustBuildAnthropicClientProfileCatalog()

func mustBuildAnthropicClientProfileCatalog() *anthropicClientProfileCatalog {
	registry, err := buildAnthropicClientProfileCatalog()
	if err != nil {
		panic(err)
	}
	return registry
}

func buildAnthropicClientProfileCatalog() (*anthropicClientProfileCatalog, error) {
	registry := &anthropicClientProfileCatalog{
		builds:   make(map[string]officialClientBuild),
		profiles: make(map[string]officialClientWireProfile),
		releases: make(map[string]officialClientReleasePointer),
	}

	for _, build := range officialClientBuildDefinitions() {
		if err := registry.addBuild(build); err != nil {
			return nil, err
		}
	}
	for _, profile := range officialClientWireProfileDefinitions() {
		if err := registry.addProfile(profile); err != nil {
			return nil, err
		}
	}
	for purpose, release := range officialClientReleaseDefinitions() {
		if err := registry.addRelease(purpose, release); err != nil {
			return nil, err
		}
	}
	return registry, nil
}

func (r *anthropicClientProfileCatalog) addBuild(build officialClientBuild) error {
	build.ID = strings.TrimSpace(build.ID)
	if build.ID == "" || strings.TrimSpace(build.Version) == "" || strings.TrimSpace(build.UserAgent) == "" {
		return errors.New("official client build is incomplete")
	}
	if _, exists := r.builds[build.ID]; exists {
		return fmt.Errorf("official client build is duplicated: %s", build.ID)
	}
	r.builds[build.ID] = cloneOfficialClientBuild(build)
	return nil
}

func (r *anthropicClientProfileCatalog) addProfile(profile officialClientWireProfile) error {
	profile.ID = strings.TrimSpace(profile.ID)
	profile.Purpose = strings.TrimSpace(profile.Purpose)
	profile.BuildID = strings.TrimSpace(profile.BuildID)
	if profile.ID == "" || profile.Purpose == "" || profile.BuildID == "" ||
		strings.TrimSpace(profile.AuthMode) == "" || strings.TrimSpace(profile.Endpoint) == "" ||
		strings.TrimSpace(profile.Transport) == "" || strings.TrimSpace(profile.NetworkVariant) == "" ||
		strings.TrimSpace(profile.TransportProfileID) == "" || strings.TrimSpace(profile.Source) == "" {
		return errors.New("official client wire profile is incomplete")
	}
	build, exists := r.builds[profile.BuildID]
	if !exists {
		return fmt.Errorf("official client wire profile references unknown build: %s", profile.BuildID)
	}
	if _, exists := r.profiles[profile.ID]; exists {
		return fmt.Errorf("official client wire profile is duplicated: %s", profile.ID)
	}
	profile.Digest = digestOfficialClientProfile(build, profile)
	r.profiles[profile.ID] = cloneOfficialClientWireProfile(profile)
	return nil
}

func (r *anthropicClientProfileCatalog) addRelease(purpose string, release officialClientReleasePointer) error {
	purpose = strings.TrimSpace(purpose)
	if purpose == "" || strings.TrimSpace(release.Active) == "" || strings.TrimSpace(release.Previous) == "" {
		return errors.New("official client release pointer is incomplete")
	}
	active, activeOK := r.profiles[release.Active]
	previous, previousOK := r.profiles[release.Previous]
	if !activeOK || !previousOK || active.Purpose != purpose || previous.Purpose != purpose {
		return fmt.Errorf("official client release pointer conflicts with purpose: %s", purpose)
	}
	r.releases[purpose] = release
	return nil
}

func (r *anthropicClientProfileCatalog) resolve(purpose, mode string) (officialClientResolvedProfile, error) {
	if r == nil {
		return officialClientResolvedProfile{}, errors.New("official client profile registry is nil")
	}
	release, exists := r.releases[strings.TrimSpace(purpose)]
	if !exists {
		return officialClientResolvedProfile{}, fmt.Errorf("unknown official client profile purpose: %s", purpose)
	}
	profileID := release.Active
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case "", officialClientProfileModeActive:
	case officialClientProfileModePrevious:
		profileID = release.Previous
	default:
		return officialClientResolvedProfile{}, fmt.Errorf("unknown official client profile mode: %s", mode)
	}
	return r.resolveByID(profileID)
}

func (r *anthropicClientProfileCatalog) resolveByID(profileID string) (officialClientResolvedProfile, error) {
	profile, exists := r.profiles[strings.TrimSpace(profileID)]
	if !exists {
		return officialClientResolvedProfile{}, fmt.Errorf("unknown official client profile: %s", profileID)
	}
	build, exists := r.builds[profile.BuildID]
	if !exists {
		return officialClientResolvedProfile{}, fmt.Errorf("official client profile build is unavailable: %s", profile.BuildID)
	}
	return officialClientResolvedProfile{
		Wire:  cloneOfficialClientWireProfile(profile),
		Build: cloneOfficialClientBuild(build),
	}, nil
}

func resolveOfficialClientProfile(purpose, mode string) (officialClientResolvedProfile, error) {
	switch purpose {
	case officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientPurposeOpenAIOAuthResponsesWS,
		officialClientPurposeOpenAIAPIKeyResponsesHTTP,
		officialClientPurposeOpenAIAPIKeyResponsesWS:
		return resolveFormalOpenAIClientProfile(purpose, mode)
	default:
		return defaultAnthropicClientProfileCatalog.resolve(purpose, mode)
	}
}

func resolveOfficialClientProfileByID(profileID string) (officialClientResolvedProfile, error) {
	return defaultAnthropicClientProfileCatalog.resolveByID(profileID)
}

// resolveFormalOpenAIClientProfile 只把正式 ReleaseCatalog 投影到仍在迁移期消费的
// service DTO。它不维护 release pointer，也不读取 service 侧 registry。
func resolveFormalOpenAIClientProfile(purpose, mode string) (officialClientResolvedProfile, error) {
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case "", officialClientProfileModeActive, officialClientProfileModePrevious:
	default:
		return officialClientResolvedProfile{}, fmt.Errorf("unknown official client profile mode: %s", mode)
	}
	releaseMode := officialegress.ReleaseMode(normalizeOfficialClientProfileMode(mode))
	release, err := officialegress.DefaultReleaseCatalog().Resolve(releaseMode)
	if err != nil {
		return officialClientResolvedProfile{}, err
	}
	releasePurpose := officialegress.RegistryPurposeOpenAIOAuthHTTP
	endpointID := officialCodexEndpointResponsesHTTP
	transport := "http"
	switch purpose {
	case officialClientPurposeOpenAIOAuthResponsesWS, officialClientPurposeOpenAIAPIKeyResponsesWS:
		releasePurpose = officialegress.RegistryPurposeOpenAIOAuthWS
		endpointID = officialCodexEndpointResponsesWS
		transport = "websocket"
	case officialClientPurposeOpenAIOAuthResponsesHTTP, officialClientPurposeOpenAIAPIKeyResponsesHTTP:
	default:
		return officialClientResolvedProfile{}, fmt.Errorf("unknown OpenAI client profile purpose: %s", purpose)
	}
	node, ok := release.Node(releasePurpose)
	if !ok {
		return officialClientResolvedProfile{}, fmt.Errorf("formal release lacks purpose: %s", releasePurpose)
	}
	build := officialClientBuild{
		ID: node.Build.ID, Provider: node.Build.Provider, Product: node.Build.Product,
		Surface: node.Build.Surface, Version: node.Build.Version,
		UserAgent: node.Build.UserAgent, Originator: node.Build.Originator,
		Source: node.Build.Source,
	}
	for _, header := range node.Build.RuntimeHeaders {
		build.RuntimeHeaders = append(build.RuntimeHeaders, officialClientHeaderValue{
			Name: header.Name, Value: header.Value,
		})
	}
	wire := officialClientWireProfile{
		ID: node.Wire.ID, Purpose: purpose, BuildID: node.Build.ID,
		AuthMode: node.Wire.AuthMode, Endpoint: node.Wire.Endpoint,
		Transport: transport, NetworkVariant: node.Wire.NetworkVariant,
		BetaHeader: node.Wire.BetaHeader, TransportProfileID: node.Wire.TransportProfileID,
		Source: node.Wire.Source, Digest: node.Wire.Digest,
	}
	for _, header := range node.Wire.StaticHeaders {
		wire.StaticHeaders = append(wire.StaticHeaders, officialClientHeaderValue{
			Name: header.Name, Value: header.Value,
		})
	}
	if strings.Contains(purpose, "apikey") {
		wire.ID += ".apikey_projection"
		wire.AuthMode = "apikey"
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if endpoint.ID != endpointID {
				continue
			}
			for _, header := range endpoint.Headers {
				switch strings.ToLower(header.Name) {
				case "x-codex-beta-features", "x-openai-internal-codex-responses-lite", "openai-beta":
					if header.Value != "" {
						wire.StaticHeaders = append(wire.StaticHeaders, officialClientHeaderValue{
							Name: header.WireName, Value: header.Value,
						})
					}
				}
			}
			break
		}
		wire.Digest = digestOfficialClientProfile(build, wire)
	}
	return officialClientResolvedProfile{Wire: wire, Build: build}, nil
}

func officialClientProfileModeFromConfig(cfg *config.Config) string {
	if cfg == nil {
		return officialClientProfileModeActive
	}
	return normalizeOfficialClientProfileMode(cfg.Gateway.OfficialClientProfiles.Mode)
}

func normalizeOfficialClientProfileMode(mode string) string {
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case officialClientProfileModePrevious:
		return officialClientProfileModePrevious
	default:
		return officialClientProfileModeActive
	}
}

func cloneOfficialClientBuild(build officialClientBuild) officialClientBuild {
	build.RuntimeHeaders = append([]officialClientHeaderValue(nil), build.RuntimeHeaders...)
	return build
}

func cloneOfficialClientWireProfile(profile officialClientWireProfile) officialClientWireProfile {
	profile.StaticHeaders = append([]officialClientHeaderValue(nil), profile.StaticHeaders...)
	return profile
}

func digestOfficialClientProfile(build officialClientBuild, profile officialClientWireProfile) string {
	profile.Digest = ""
	payload, err := json.Marshal(struct {
		Build   officialClientBuild       `json:"build"`
		Profile officialClientWireProfile `json:"profile"`
	}{Build: build, Profile: profile})
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func officialClientBuildDefinitions() []officialClientBuild {
	return []officialClientBuild{
		{
			ID:        officialClientBuildAnthropicCLI21220,
			Provider:  PlatformAnthropic,
			Product:   "claude_code",
			Surface:   "cli",
			Version:   "2.1.220",
			UserAgent: "claude-cli/2.1.220 (external, sdk-cli)",
			RuntimeHeaders: []officialClientHeaderValue{
				{Name: "X-Stainless-Arch", Value: "x64"},
				{Name: "X-Stainless-Lang", Value: "js"},
				{Name: "X-Stainless-OS", Value: "Linux"},
				{Name: "X-Stainless-Package-Version", Value: "0.94.0"},
				{Name: "X-Stainless-Retry-Count", Value: "0"},
				{Name: "X-Stainless-Runtime", Value: "node"},
				{Name: "X-Stainless-Runtime-Version", Value: "v26.3.0"},
				{Name: "X-Stainless-Timeout", Value: "600"},
			},
			Source: "capture:oauth-20260726T014021Z,api-20260726T014252Z",
		},
		{
			ID:        officialClientBuildAnthropicCLI21218,
			Provider:  PlatformAnthropic,
			Product:   "claude_code",
			Surface:   "cli",
			Version:   "2.1.218",
			UserAgent: "claude-cli/2.1.218 (external, sdk-cli)",
			RuntimeHeaders: []officialClientHeaderValue{
				{Name: "X-Stainless-Arch", Value: "x64"},
				{Name: "X-Stainless-Lang", Value: "js"},
				{Name: "X-Stainless-OS", Value: "Linux"},
				{Name: "X-Stainless-Package-Version", Value: "0.94.0"},
				{Name: "X-Stainless-Retry-Count", Value: "0"},
				{Name: "X-Stainless-Runtime", Value: "node"},
				{Name: "X-Stainless-Runtime-Version", Value: "v26.3.0"},
				{Name: "X-Stainless-Timeout", Value: "600"},
			},
			Source: "capture:phase0-20260724",
		},
		{
			ID:        officialClientBuildAnthropicDesktop,
			Provider:  PlatformAnthropic,
			Product:   "claude_desktop",
			Surface:   "desktop",
			Version:   "2.1.209",
			UserAgent: "claude-cli/2.1.209 (external, claude-desktop-3p, agent-sdk/0.3.209)",
			RuntimeHeaders: []officialClientHeaderValue{
				{Name: "X-Stainless-Arch", Value: "arm64"},
				{Name: "X-Stainless-Lang", Value: "js"},
				{Name: "X-Stainless-OS", Value: "MacOS"},
				{Name: "X-Stainless-Package-Version", Value: "0.94.0"},
				{Name: "X-Stainless-Retry-Count", Value: "0"},
				{Name: "X-Stainless-Runtime", Value: "node"},
				{Name: "X-Stainless-Runtime-Version", Value: "v26.3.0"},
				{Name: "X-Stainless-Timeout", Value: "900"},
			},
			Source: "capture:desktop-2.1.209-202607",
		},
	}
}

func officialClientWireProfileDefinitions() []officialClientWireProfile {
	const anthropicOAuthBeta = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,effort-2025-11-24,extended-cache-ttl-2025-04-11"
	const anthropicAPIKeyBeta = "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,effort-2025-11-24"
	const anthropicDesktopBeta = "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,mid-conversation-system-2026-04-07,effort-2025-11-24,fallback-credit-2026-06-01"

	profiles := []officialClientWireProfile{
		newAnthropicWireProfile("anthropic_claude_code_2_1_220_oauth_messages_http_direct", officialClientPurposeAnthropicOAuthMessagesHTTP, officialClientBuildAnthropicCLI21220, "oauth", "messages", anthropicOAuthBeta, "capture:oauth-20260726T014021Z"),
		newAnthropicWireProfile("anthropic_claude_code_2_1_218_oauth_messages_http_direct", officialClientPurposeAnthropicOAuthMessagesHTTP, officialClientBuildAnthropicCLI21218, "oauth", "messages", anthropicOAuthBeta, "capture:phase0-20260724"),
		newAnthropicWireProfile("anthropic_claude_code_2_1_220_apikey_messages_http_direct", officialClientPurposeAnthropicAPIKeyMessagesHTTP, officialClientBuildAnthropicCLI21220, "apikey", "messages", anthropicAPIKeyBeta, "capture:api-20260726T014252Z"),
		newAnthropicWireProfile("anthropic_claude_desktop_2_1_209_apikey_messages_http_previous", officialClientPurposeAnthropicAPIKeyMessagesHTTP, officialClientBuildAnthropicDesktop, "apikey", "messages", anthropicDesktopBeta, "capture:desktop-2.1.209-202607"),
		newAnthropicWireProfile("anthropic_claude_code_2_1_220_apikey_count_tokens_generic", officialClientPurposeAnthropicAPIKeyCountTokensCompat, officialClientBuildAnthropicCLI21220, "apikey", "count_tokens_generic", anthropicAPIKeyBeta+",token-counting-2024-11-01", "derived:messages-client-build-with-generic-count-tokens-contract"),
		newAnthropicWireProfile("anthropic_claude_desktop_2_1_209_apikey_count_tokens_generic_previous", officialClientPurposeAnthropicAPIKeyCountTokensCompat, officialClientBuildAnthropicDesktop, "apikey", "count_tokens_generic", anthropicDesktopBeta+",token-counting-2024-11-01", "derived:legacy-desktop-generic-count-tokens-contract"),
	}
	return profiles
}

func newAnthropicWireProfile(id, purpose, buildID, authMode, endpoint, betaHeader, source string) officialClientWireProfile {
	transportProfileID := officialEgressTransportProfileAnthropicHTTP
	switch buildID {
	case officialClientBuildAnthropicCLI21218:
		transportProfileID = "anthropic-http-claude-code-2.1.218-direct"
	case officialClientBuildAnthropicDesktop:
		transportProfileID = "anthropic-http-claude-desktop-2.1.209-legacy"
	}
	return officialClientWireProfile{
		ID:             id,
		Purpose:        purpose,
		BuildID:        buildID,
		AuthMode:       authMode,
		Endpoint:       endpoint,
		Transport:      "http",
		NetworkVariant: "direct",
		StaticHeaders: []officialClientHeaderValue{
			{Name: "Accept", Value: "application/json"},
			{Name: "Content-Type", Value: "application/json"},
			{Name: "anthropic-dangerous-direct-browser-access", Value: "true"},
			{Name: "anthropic-version", Value: "2023-06-01"},
			{Name: "x-app", Value: "cli"},
			{Name: "Accept-Encoding", Value: "gzip, deflate, br, zstd"},
			{Name: "Connection", Value: "keep-alive"},
		},
		BetaHeader:         betaHeader,
		TransportProfileID: transportProfileID,
		Source:             source,
	}
}

func officialClientReleaseDefinitions() map[string]officialClientReleasePointer {
	return map[string]officialClientReleasePointer{
		officialClientPurposeAnthropicOAuthMessagesHTTP: {
			Active:   "anthropic_claude_code_2_1_220_oauth_messages_http_direct",
			Previous: "anthropic_claude_code_2_1_218_oauth_messages_http_direct",
		},
		officialClientPurposeAnthropicAPIKeyMessagesHTTP: {
			Active:   "anthropic_claude_code_2_1_220_apikey_messages_http_direct",
			Previous: "anthropic_claude_desktop_2_1_209_apikey_messages_http_previous",
		},
		officialClientPurposeAnthropicAPIKeyCountTokensCompat: {
			Active:   "anthropic_claude_code_2_1_220_apikey_count_tokens_generic",
			Previous: "anthropic_claude_desktop_2_1_209_apikey_count_tokens_generic_previous",
		},
	}
}
