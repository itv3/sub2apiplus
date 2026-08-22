package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/google/uuid"
)

const (
	claudeOfficialIngressCatalogPath   = "catalogdata/claude/official-ingress-catalog.json"
	claudeOfficialIngressCatalogSHA256 = "3641036e6272c7e3be6a952269b4e58aebd88028f760a939fcc22e4a2fd73dd4"
)

var claudeOfficialIngressBodyOrder = []string{
	"model", "messages", "system", "tools", "tool_choice", "metadata",
	"max_tokens", "thinking", "context_management", "fallbacks",
	"output_config", "temperature", "stream",
}

type claudeOfficialIngressCatalogDocument struct {
	SchemaVersion       string `json:"schema_version"`
	Persona             string `json:"persona"`
	TargetReleaseSHA256 string `json:"target_release_sha256"`
	EvidencePolicy      string `json:"evidence_policy"`
	Entries             []struct {
		ClientID                       string            `json:"client_id"`
		Product                        string            `json:"product"`
		Version                        string            `json:"version"`
		Platform                       string            `json:"platform"`
		BinarySHA256                   string            `json:"binary_sha256"`
		UserAgents                     []string          `json:"user_agents"`
		AllowRegisteredReleaseFeatures bool              `json:"allow_registered_release_features"`
		TargetEntrypoint               string            `json:"target_entrypoint"`
		FixedHeaders                   map[string]string `json:"fixed_headers"`
		SystemAnchorSHA256             string            `json:"system_anchor_sha256"`
		SystemTailSHA256               []string          `json:"system_tail_sha256"`
		ToolCatalogSHA256              []string          `json:"tool_catalog_sha256"`
	} `json:"entries"`
}

type claudeOfficialIngressEntry struct {
	clientID                       string
	product                        string
	version                        string
	platform                       string
	binaryDigest                   string
	userAgents                     []string
	allowRegisteredReleaseFeatures bool
	targetEntrypoint               string
	fixedHeaders                   map[string]string
	systemAnchorDigest             string
	systemTailDigests              map[string]struct{}
	toolCatalogDigests             map[string]struct{}
}

type claudeOfficialIngressCatalog struct {
	targetReleaseDigest string
	evidencePolicy      string
	entries             []claudeOfficialIngressEntry
	digest              string
}

type claudeOfficialIngressMatch struct {
	entry                claudeOfficialIngressEntry
	sourceEntrypoint     string
	nativeTargetScenario bool
	toolCatalogDigest    string
}

func loadClaudeOfficialIngressCatalogFromFS(catalogFS fs.FS) (
	claudeOfficialIngressCatalog,
	error,
) {
	raw, err := fs.ReadFile(catalogFS, claudeOfficialIngressCatalogPath)
	if err != nil {
		return claudeOfficialIngressCatalog{}, err
	}
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	if digest != claudeOfficialIngressCatalogSHA256 {
		return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog 摘要漂移")
	}
	var document claudeOfficialIngressCatalogDocument
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return claudeOfficialIngressCatalog{}, fmt.Errorf("解析 Claude 官方入口 Catalog：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog 尾部存在额外 JSON")
	}
	if document.SchemaVersion != "official-egress-claude-official-ingress-catalog/v1" ||
		document.Persona != string(PersonaClaudeCode) ||
		!receiptSHA256(document.TargetReleaseSHA256) ||
		strings.TrimSpace(document.EvidencePolicy) == "" || len(document.Entries) == 0 {
		return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog 顶层身份非法")
	}
	catalog := claudeOfficialIngressCatalog{
		targetReleaseDigest: document.TargetReleaseSHA256,
		evidencePolicy:      document.EvidencePolicy,
		entries:             make([]claudeOfficialIngressEntry, 0, len(document.Entries)),
		digest:              digest,
	}
	seenIDs := make(map[string]struct{}, len(document.Entries))
	seenUserAgents := make(map[string]struct{})
	for _, source := range document.Entries {
		entry := claudeOfficialIngressEntry{
			clientID: strings.TrimSpace(source.ClientID), product: strings.TrimSpace(source.Product),
			version: strings.TrimSpace(source.Version), platform: strings.TrimSpace(source.Platform),
			binaryDigest:                   source.BinarySHA256,
			userAgents:                     append([]string(nil), source.UserAgents...),
			allowRegisteredReleaseFeatures: source.AllowRegisteredReleaseFeatures,
			targetEntrypoint:               strings.TrimSpace(source.TargetEntrypoint),
			fixedHeaders:                   make(map[string]string, len(source.FixedHeaders)),
			systemAnchorDigest:             source.SystemAnchorSHA256,
			systemTailDigests:              make(map[string]struct{}, len(source.SystemTailSHA256)),
			toolCatalogDigests:             make(map[string]struct{}, len(source.ToolCatalogSHA256)),
		}
		if entry.clientID == "" || entry.product == "" || entry.version == "" ||
			entry.platform == "" || !receiptSHA256(entry.binaryDigest) ||
			!receiptSHA256(entry.systemAnchorDigest) ||
			(entry.targetEntrypoint != ClaudeEntrypointSDKCLI &&
				entry.targetEntrypoint != ClaudeEntrypointCLI) || len(entry.userAgents) == 0 ||
			len(source.SystemTailSHA256) == 0 || len(source.ToolCatalogSHA256) == 0 {
			return claudeOfficialIngressCatalog{}, fmt.Errorf(
				"Claude 官方入口 Catalog 条目不完整：%s", entry.clientID,
			)
		}
		if _, duplicate := seenIDs[entry.clientID]; duplicate {
			return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog client_id 重复")
		}
		seenIDs[entry.clientID] = struct{}{}
		for _, userAgent := range entry.userAgents {
			if strings.TrimSpace(userAgent) != userAgent || userAgent == "" {
				return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog UA 非法")
			}
			if _, duplicate := seenUserAgents[userAgent]; duplicate {
				return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 Catalog UA 重复")
			}
			seenUserAgents[userAgent] = struct{}{}
		}
		for name, value := range source.FixedHeaders {
			lower := strings.ToLower(strings.TrimSpace(name))
			if lower != name || lower == "" || strings.TrimSpace(value) == "" {
				return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口固定 Header 非法")
			}
			entry.fixedHeaders[lower] = strings.TrimSpace(value)
		}
		for _, value := range source.SystemTailSHA256 {
			if !receiptSHA256(value) {
				return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口 System 摘要非法")
			}
			entry.systemTailDigests[value] = struct{}{}
		}
		for _, value := range source.ToolCatalogSHA256 {
			if !receiptSHA256(value) {
				return claudeOfficialIngressCatalog{}, errors.New("Claude 官方入口工具摘要非法")
			}
			entry.toolCatalogDigests[value] = struct{}{}
		}
		catalog.entries = append(catalog.entries, entry)
	}
	return catalog, nil
}

var loadDefaultClaudeOfficialIngressCatalog = sync.OnceValues(func() (
	claudeOfficialIngressCatalog,
	error,
) {
	return loadClaudeOfficialIngressCatalogFromFS(claudeReleaseCatalogFS)
})

func defaultClaudeOfficialIngressCatalog() claudeOfficialIngressCatalog {
	catalog, err := loadDefaultClaudeOfficialIngressCatalog()
	if err != nil {
		panic(err)
	}
	return catalog
}

// ClaudeOfficialIngressCatalogDigest 返回当前进程冻结的官方入站目录摘要，供验收收据绑定。
func ClaudeOfficialIngressCatalogDigest() string {
	return defaultClaudeOfficialIngressCatalog().digest
}

func (c claudeOfficialIngressCatalog) matchMessages(
	body []byte,
	headers http.Header,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (claudeOfficialIngressMatch, error) {
	if c.targetReleaseDigest != DefaultClaudeReleaseCatalog().ProductionActive().Release().ReleaseDigest() {
		return claudeOfficialIngressMatch{}, errors.New("Claude 官方入口 Catalog 未绑定 production active Release")
	}
	entry, sourceEntrypoint, nativeHeaders, err := c.matchHeaders(headers, profile, artifact)
	if err != nil {
		return claudeOfficialIngressMatch{}, err
	}
	nativeBody, toolDigest, err := validateClaudeOfficialCatalogBody(body, entry, artifact)
	if err != nil {
		return claudeOfficialIngressMatch{}, err
	}
	return claudeOfficialIngressMatch{
		entry:                entry,
		sourceEntrypoint:     sourceEntrypoint,
		nativeTargetScenario: nativeHeaders && nativeBody,
		toolCatalogDigest:    toolDigest,
	}, nil
}

func (c claudeOfficialIngressCatalog) matchHeaders(
	headers http.Header,
	profile claudeFWGProfile,
	artifact claudeWireArtifact,
) (claudeOfficialIngressEntry, string, bool, error) {
	userAgent := strings.TrimSpace(headers.Get("User-Agent"))
	for _, entry := range c.entries {
		sourceEntrypoint, exact := claudeCatalogExactUserAgent(entry, userAgent)
		registeredFeature := false
		if !exact && entry.allowRegisteredReleaseFeatures && entry.version == artifact.Identity.Version {
			var features map[string]string
			var err error
			sourceEntrypoint, features, err = parseClaudeOfficialUserAgent(userAgent, entry.version)
			registeredFeature = err == nil && validClaudeCatalogUAFeatures(features)
		}
		if !exact && !registeredFeature {
			continue
		}
		catalogErr := validateClaudeCatalogRequiredHeaders(headers, entry.fixedHeaders)
		if catalogErr == nil {
			if !entry.allowRegisteredReleaseFeatures && strings.TrimSpace(headers.Get("x-client-request-id")) != "" {
				return claudeOfficialIngressEntry{}, "", false, errors.New(
					"Claude 官方入口出现未登记的 x-client-request-id",
				)
			}
			// 与目标 Release 同版本的官方 Claude Code 即使运行在不同取证平台，
			// 仍属于原生目标场景；平台 Header 会由 Persona 重新生成，但其已登记
			// entrypoint、agent 和条件特性必须保留。Desktop／VS Code 则只作为
			// 已登记的官方来源进入规范化边界，不能继承目标 Release 的条件状态。
			return entry, sourceEntrypoint,
				entry.allowRegisteredReleaseFeatures && entry.version == artifact.Identity.Version,
				nil
		}
		if entry.allowRegisteredReleaseFeatures && entry.version == artifact.Identity.Version {
			targetErr := validateClaudeTargetReleaseHeaders(headers, profile)
			if targetErr == nil {
				return entry, sourceEntrypoint, true, nil
			}
			return claudeOfficialIngressEntry{}, "", false, fmt.Errorf(
				"Claude 官方入口固定 Header 不在 Catalog：catalog=%v；target=%v",
				catalogErr, targetErr,
			)
		}
		return claudeOfficialIngressEntry{}, "", false, fmt.Errorf(
			"Claude 官方入口固定 Header 不在 Catalog：%w", catalogErr,
		)
	}
	return claudeOfficialIngressEntry{}, "", false, errors.New("Claude 入站客户端未登记")
}

func claudeCatalogExactUserAgent(
	entry claudeOfficialIngressEntry,
	userAgent string,
) (string, bool) {
	for _, candidate := range entry.userAgents {
		if userAgent != candidate {
			continue
		}
		parsed, _, err := parseClaudeOfficialUserAgent(userAgent, entry.version)
		return parsed, err == nil
	}
	return "", false
}

func validClaudeCatalogUAFeatures(features map[string]string) bool {
	for name, value := range features {
		if value == "" {
			return false
		}
		switch name {
		case "agent-sdk", "client-app", "workload":
		default:
			return false
		}
	}
	return true
}

func validateClaudeCatalogRequiredHeaders(headers http.Header, expected map[string]string) error {
	for name, value := range expected {
		if strings.TrimSpace(headers.Get(name)) != value {
			return fmt.Errorf("Claude 官方入口固定 Header 不一致：%s", name)
		}
	}
	if strings.TrimSpace(headers.Get("anthropic-beta")) == "" {
		return errors.New("Claude 官方入口缺少 anthropic-beta")
	}
	return nil
}

func validateClaudeTargetReleaseHeaders(headers http.Header, profile claudeFWGProfile) error {
	endpoint, err := profile.endpoint("messages-inference")
	if err != nil {
		return err
	}
	facts := claudeHeaderFacts(endpoint.headers)
	for _, name := range []string{
		"accept", "accept-encoding", "anthropic-dangerous-direct-browser-access",
		"anthropic-version", "content-type", "x-stainless-arch", "x-stainless-lang",
		"x-stainless-os", "x-stainless-package-version", "x-stainless-retry-count",
		"x-stainless-runtime", "x-stainless-runtime-version",
	} {
		if strings.TrimSpace(headers.Get(name)) != facts[name].Value {
			return fmt.Errorf("Claude target Release Header 不一致：%s", name)
		}
	}
	if strings.TrimSpace(headers.Get("anthropic-beta")) == "" {
		return errors.New("Claude target Release 缺少 anthropic-beta")
	}
	timeout, err := strconv.Atoi(strings.TrimSpace(headers.Get("x-stainless-timeout")))
	if err != nil || timeout <= 0 || timeout > 3600 {
		return errors.New("Claude target Release x-stainless-timeout 非法")
	}
	return nil
}

func validateClaudeOfficialCatalogBody(
	body []byte,
	entry claudeOfficialIngressEntry,
	artifact claudeWireArtifact,
) (bool, string, error) {
	fields, err := decodeClaudeOrderedObject(body)
	if err != nil {
		return false, "", errors.New("Claude 官方入口 Body 不是唯一对象")
	}
	order := make([]string, 0, len(fields))
	document := make(map[string]json.RawMessage, len(fields))
	for _, field := range fields {
		order = append(order, field.name)
		document[field.name] = field.raw
	}
	if !claudeOfficialBodyFieldsAllowed(order) {
		return false, "", errors.New("Claude 官方入口 Body 顶层字段未登记")
	}
	systemNative, err := validateClaudeOfficialCatalogSystem(document["system"], entry, artifact)
	if err != nil {
		return false, "", err
	}
	toolDigest := digestClaudeRaw(document["tools"])
	_, toolRegistered := entry.toolCatalogDigests[toolDigest]
	toolNative := entry.version == artifact.Identity.Version &&
		claudeToolsMatchFrozenPolicy(document["tools"], document["tool_choice"], artifact)
	if !toolRegistered && !toolNative {
		return false, "", errors.New("Claude 官方入口工具目录摘要未登记")
	}
	if _, err := extractClaudeOfficialMetadata(body); err != nil {
		return false, "", err
	}
	native := systemNative && toolNative
	if !native {
		if !claudeOfficialBodyOrderAllowed(order) {
			return false, "", errors.New("Claude 新版官方入口 Body 顶层字段顺序未登记")
		}
		for _, required := range []string{
			"model", "messages", "system", "tools", "metadata", "max_tokens", "output_config", "stream",
		} {
			if len(bytes.TrimSpace(document[required])) == 0 {
				return false, "", fmt.Errorf("Claude 新版官方入口 Body 缺少字段：%s", required)
			}
		}
	}
	return native, toolDigest, nil
}

func claudeOfficialBodyFieldsAllowed(actual []string) bool {
	allowed := make(map[string]struct{}, len(claudeOfficialIngressBodyOrder))
	for _, name := range claudeOfficialIngressBodyOrder {
		allowed[name] = struct{}{}
	}
	for _, name := range actual {
		if _, ok := allowed[name]; !ok {
			return false
		}
	}
	return true
}

func claudeOfficialBodyOrderAllowed(actual []string) bool {
	positions := make(map[string]int, len(claudeOfficialIngressBodyOrder))
	for index, name := range claudeOfficialIngressBodyOrder {
		positions[name] = index
	}
	previous := -1
	for _, name := range actual {
		position, ok := positions[name]
		if !ok || position <= previous {
			return false
		}
		previous = position
	}
	return true
}

func validateClaudeOfficialCatalogSystem(
	raw json.RawMessage,
	entry claudeOfficialIngressEntry,
	artifact claudeWireArtifact,
) (bool, error) {
	if len(bytes.TrimSpace(raw)) == 0 && entry.version == artifact.Identity.Version {
		return true, nil
	}
	var blocks []claudeWireSystemBlock
	if json.Unmarshal(raw, &blocks) != nil {
		return false, errors.New("Claude 官方入口 system 不是非空文本块数组")
	}
	if len(blocks) == 0 {
		if entry.version == artifact.Identity.Version &&
			bytes.Equal(bytes.TrimSpace(raw), []byte("[]")) {
			return true, nil
		}
		return false, errors.New("Claude 官方入口 system 不是非空文本块数组")
	}
	filtered := make([]claudeWireSystemBlock, 0, len(blocks))
	for index, block := range blocks {
		if block.Type != "text" || block.Text == "" {
			return false, errors.New("Claude 官方入口 system 含非文本块")
		}
		if strings.HasPrefix(strings.TrimSpace(block.Text), claudeBillingPrefix) {
			if index != 0 {
				return false, errors.New("Claude 官方入口 attribution 位置非法")
			}
			continue
		}
		filtered = append(filtered, block)
	}
	if len(filtered) == 0 && entry.version == artifact.Identity.Version {
		return true, nil
	}
	if entry.version == artifact.Identity.Version {
		if recognized, _ := classifyClaudeOfficialSystem(filtered, artifact); recognized {
			return true, nil
		}
	}
	if len(filtered) < 2 || digestClaudeText(filtered[0].Text) != entry.systemAnchorDigest {
		return false, errors.New("Claude 官方入口 system anchor 未登记")
	}
	for _, block := range filtered[1:] {
		if _, ok := entry.systemTailDigests[digestClaudeText(block.Text)]; !ok {
			return false, errors.New("Claude 官方入口 system 尾块摘要未登记")
		}
	}
	return false, nil
}

func claudeToolsMatchFrozenPolicy(
	toolsRaw json.RawMessage,
	toolChoiceRaw json.RawMessage,
	artifact claudeWireArtifact,
) bool {
	toolsRaw = bytes.TrimSpace(toolsRaw)
	toolChoiceRaw = normalizeClaudeOptionalRaw(toolChoiceRaw)
	if len(toolsRaw) == 0 {
		return len(toolChoiceRaw) == 0
	}
	if bytes.Equal(toolsRaw, []byte("[]")) {
		return len(toolChoiceRaw) == 0
	}
	policy := artifact.ImplementationPolicy.ToolPolicy
	for _, catalog := range []claudeWireToolCatalog{
		policy.Agent, policy.Bash, policy.MCPDeferred, policy.Advisor,
		policy.Background, policy.WebSearchOuter, policy.WebSearchServer,
	} {
		if claudeJSONEqual(toolsRaw, catalog.Tools) &&
			claudeOptionalJSONEqual(toolChoiceRaw, catalog.ToolChoice) {
			return true
		}
	}
	for _, capability := range artifact.ImplementationPolicy.ModelCatalog.Models {
		for _, scenario := range claudeNamedModelScenarios(capability) {
			if scenario.scenario.ToolsPresent && claudeJSONEqual(toolsRaw, scenario.scenario.Tools) {
				return true
			}
		}
	}
	return false
}

func digestClaudeRaw(raw json.RawMessage) string {
	sum := sha256.Sum256(bytes.TrimSpace(raw))
	return hex.EncodeToString(sum[:])
}

func digestClaudeText(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func validateClaudeOfficialMetadataSession(
	body []byte,
	headers http.Header,
) (claudeOfficialMetadata, error) {
	metadata, err := extractClaudeOfficialMetadata(body)
	if err != nil {
		return claudeOfficialMetadata{}, err
	}
	sessionID := strings.TrimSpace(headers.Get("X-Claude-Code-Session-Id"))
	if _, err := uuid.Parse(sessionID); err != nil || metadata.sessionID != sessionID {
		return claudeOfficialMetadata{}, errors.New("Claude 官方 Session Header 与 metadata 缺失或冲突")
	}
	return metadata, nil
}

func cloneClaudeOfficialIngressEntry(entry claudeOfficialIngressEntry) claudeOfficialIngressEntry {
	entry.userAgents = append([]string(nil), entry.userAgents...)
	entry.fixedHeaders = cloneClaudeStringMap(entry.fixedHeaders)
	entry.systemTailDigests = cloneClaudeStringSet(entry.systemTailDigests)
	entry.toolCatalogDigests = cloneClaudeStringSet(entry.toolCatalogDigests)
	return entry
}

func cloneClaudeStringMap(source map[string]string) map[string]string {
	out := make(map[string]string, len(source))
	for key, value := range source {
		out[key] = value
	}
	return out
}

func cloneClaudeStringSet(source map[string]struct{}) map[string]struct{} {
	out := make(map[string]struct{}, len(source))
	for key := range source {
		out[key] = struct{}{}
	}
	return out
}

func claudeCatalogHasExactUserAgent(entry claudeOfficialIngressEntry, value string) bool {
	return slices.Contains(entry.userAgents, value)
}
