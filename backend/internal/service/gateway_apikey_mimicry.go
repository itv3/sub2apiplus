package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

func shouldMimicAnthropicAPIKeyClaudeCode(account *Account, tokenType string, c *gin.Context, body []byte) bool {
	return account != nil &&
		tokenType == "apikey" &&
		account.IsAnthropicAPIKeyClaudeCodeMimicEnabled() &&
		!isInboundAnthropicOfficialClient(c, body)
}

func isInboundAnthropicOfficialClient(c *gin.Context, body []byte) bool {
	if c == nil || c.Request == nil {
		return false
	}
	if IsClaudeCodeClient(c.Request.Context()) {
		return true
	}
	userAgent := c.GetHeader("User-Agent")
	if isClaudeCodeClient(userAgent, gjson.GetBytes(body, "metadata.user_id").String()) {
		return true
	}
	return isClaudeDesktopOfficialClientUserAgent(userAgent)
}

func isClaudeDesktopOfficialClientUserAgent(userAgent string) bool {
	ua := strings.ToLower(strings.TrimSpace(userAgent))
	return strings.HasPrefix(ua, "claude desktop/") ||
		strings.HasPrefix(ua, "claude-desktop/") ||
		strings.HasPrefix(ua, "claude_desktop/") ||
		strings.HasPrefix(ua, "claude_app/")
}

func (s *GatewayService) resolveAnthropicTLSProfileForRequest(account *Account, mimicAPIKeyClaudeCode bool) *tlsfingerprint.Profile {
	if s == nil {
		return nil
	}
	return resolveAnthropicTLSProfileForRequest(account, mimicAPIKeyClaudeCode, s.tlsFPProfileService, s.cfg)
}

func defaultAPIKeyCountTokensMimicBetaHeader(body []byte) string {
	// 当前没有官方 count_tokens 对照样本，保持既有固定尾部，不把 /v1/messages
	// 的 structured-outputs 条件规则无证据扩展到该接口。
	profile, err := resolveOfficialClientProfile(officialClientPurposeAnthropicAPIKeyCountTokensCompat, officialClientProfileModeActive)
	if err != nil {
		return ""
	}
	beta := buildDefaultAPIKeyMimicBetaHeaderForProfile(body, false, profile)
	return mergeAnthropicBeta([]string{beta, "token-counting-2024-11-01"}, "")
}

// defaultAPIKeyMimicBetaHeader 返回 Anthropic API Key mimic Claude Code 时的 anthropic-beta，
// 对齐当前发布指针指向的官方客户端抓包 beta 列表（仅 mimic 路径使用）。
func defaultAPIKeyMimicBetaHeader(body []byte) string {
	return buildDefaultAPIKeyMimicBetaHeader(body, true)
}

func buildDefaultAPIKeyMimicBetaHeader(body []byte, selectStructuredOutputs bool) string {
	profile, err := resolveOfficialClientProfile(officialClientPurposeAnthropicAPIKeyMessagesHTTP, officialClientProfileModeActive)
	if err != nil {
		return ""
	}
	return buildDefaultAPIKeyMimicBetaHeaderForProfile(body, selectStructuredOutputs, profile)
}

func buildDefaultAPIKeyMimicBetaHeaderForProfile(body []byte, selectStructuredOutputs bool, profile officialClientResolvedProfile) string {
	modelID := gjson.GetBytes(body, "model").String()
	betas := splitAnthropicBetaTokens(profile.Wire.BetaHeader)
	if requiresContext1MBetaForAPIKeyMimic(modelID) {
		betas = appendAnthropicBetaToken(betas, claude.BetaContext1M)
	}
	if selectStructuredOutputs && apiKeyMimicBodyRequiresStructuredOutputs(body) {
		replacedLegacyFallback := false
		for i, beta := range betas {
			if beta == AnthropicAPIKeyBetaFallbackCredit {
				betas[i] = AnthropicAPIKeyBetaStructuredOutputs
				replacedLegacyFallback = true
				break
			}
		}
		if !replacedLegacyFallback {
			betas = appendAnthropicBetaToken(betas, AnthropicAPIKeyBetaStructuredOutputs)
		}
	}
	return strings.Join(betas, ",")
}

func splitAnthropicBetaTokens(value string) []string {
	parts := strings.Split(value, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if token := strings.TrimSpace(part); token != "" {
			out = append(out, token)
		}
	}
	return out
}

func appendAnthropicBetaToken(tokens []string, token string) []string {
	for _, existing := range tokens {
		if existing == token {
			return tokens
		}
	}
	return append(tokens, token)
}

// apiKeyMimicBodyRequiresStructuredOutputs 判断最终出站 body 是否启用了 JSON Schema
// 结构化输出。afk-mode 属于客户端运行状态，无法从 body 可靠推导，因此不在此猜测。
func apiKeyMimicBodyRequiresStructuredOutputs(body []byte) bool {
	return gjson.GetBytes(body, "output_config.format.type").String() == "json_schema"
}

func anthropicAPIKeyMimicExtraBetas(modelID string) []string {
	if requiresContext1MBetaForAPIKeyMimic(modelID) {
		return []string{claude.BetaContext1M}
	}
	return nil
}

func requiresContext1MBetaForAPIKeyMimic(modelID string) bool {
	modelID = strings.ToLower(strings.TrimSpace(modelID))
	return strings.HasPrefix(modelID, "claude-opus-4-6") ||
		strings.HasPrefix(modelID, "claude-opus-4-7") ||
		strings.HasPrefix(modelID, "claude-opus-4-8") ||
		strings.HasPrefix(modelID, "claude-fable-5")
}

func (s *GatewayService) buildAnthropicAPIKeyCLIMimicRequest(
	ctx context.Context,
	account *Account,
	body []byte,
	token string,
	targetURL string,
	reqStream bool,
	c *gin.Context,
	effectiveDropSet map[string]struct{},
) (*http.Request, []byte, error) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyMessagesHTTP,
		officialClientProfileModeFromConfig(s.cfg),
	)
	if err != nil {
		return nil, nil, err
	}
	body = s.applyAnthropicAPIKeyClaudeCodeMimicryToBody(ctx, c, account, body, profile)
	body = enforceCacheControlLimit(body)
	modelID := gjson.GetBytes(body, "model").String()
	// 官方客户端基线 beta 是身份形态：先算默认列表，再把这些
	// token 从 drop set 中移除。不能让全局 BetaPolicy 把 context-1m 等身份 beta 剥掉，
	// 否则会偏离实抓画像，且多数中转站会直接 400 要求启用 1m。
	// 注意：不要把这些 token 再以 required 方式重复合并，否则会破坏官方顺序。
	defaultBetaHeader := buildDefaultAPIKeyMimicBetaHeaderForProfile(body, true, profile)
	// 只保护静态身份基线；structured-outputs 等按 body 动态替换的 beta
	// 仍必须受 BetaPolicy 控制。
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, splitAnthropicBetaTokens(profile.Wire.BetaHeader)...)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, anthropicAPIKeyMimicExtraBetas(modelID)...)
	finalBetaHeader := stripBetaTokensWithSet(defaultBetaHeader, effectiveDropSet)
	if apiKeyMimicBodyRequiresStructuredOutputs(body) &&
		!strings.Contains(strings.ToLower(modelID), "haiku") &&
		!anthropicBetaTokensContains(finalBetaHeader, AnthropicAPIKeyBetaStructuredOutputs) {
		return nil, nil, &BetaBlockedError{Message: "结构化输出请求需要 beta feature " + AnthropicAPIKeyBetaStructuredOutputs + "，但该 beta 已被过滤策略禁用"}
	}
	if blockErr := s.checkBetaPolicyBlockForHeader(ctx, finalBetaHeader, account, modelID); blockErr != nil {
		return nil, nil, blockErr
	}
	if sanitized, changed := sanitizeAnthropicBodyForBetaTokens(body, finalBetaHeader); changed {
		body = sanitized
	}
	if rw := buildClaudeCodeOAuthToolNameRewriteFromBody(body); rw != nil {
		body = applyToolNameRewriteToBody(body, rw)
		if c != nil {
			c.Set(toolNameRewriteKey, rw)
		}
	} else {
		body = applyToolsLastCacheBreakpoint(body)
	}
	if profile.Build.ID == officialClientBuildAnthropicCLI21220 {
		body = normalizeAnthropicAPIKeyCLICacheProfile(body)
	}
	body = enforceCacheControlLimit(body)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	setHeaderRaw(req.Header, "x-api-key", token)
	deleteHeaderAllForms(req.Header, "Authorization")
	// Header 画像只在 API Key mimic 的 /v1/messages 构造链局部应用，不改变 OAuth、
	// count_tokens 或其他平台的共享 Header 规则。
	applyAnthropicAPIKeyProfileHeaders(req.Header, profile)
	// API Key 官方客户端样本不发送这两个 OAuth mimic 历史头。只在本构造路径
	// 局部删除，避免改变共享 helper 的 OAuth 和 count_tokens 行为。
	deleteHeaderAllForms(req.Header, "x-client-request-id")
	deleteHeaderAllForms(req.Header, "x-stainless-helper-method")
	if sessionID := apiKeyMimicSessionIDFromBody(body); sessionID != "" {
		setHeaderRaw(req.Header, "X-Claude-Code-Session-Id", sessionID)
	}
	deleteHeaderAllForms(req.Header, "anthropic-beta")
	if finalBetaHeader != "" {
		setHeaderRaw(req.Header, "anthropic-beta", finalBetaHeader)
	}
	account.ApplyHeaderOverridesForAPIKeyMimic(req.Header)
	return req, body, nil
}

func removeTokensFromSetCopy(in map[string]struct{}, tokens ...string) map[string]struct{} {
	if len(in) == 0 || len(tokens) == 0 {
		return in
	}
	out := make(map[string]struct{}, len(in))
	for k, v := range in {
		out[k] = v
	}
	for _, token := range tokens {
		delete(out, token)
	}
	return out
}

func (s *GatewayService) applyAnthropicAPIKeyClaudeCodeMimicryToBody(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	body []byte,
	profiles ...officialClientResolvedProfile,
) []byte {
	if account == nil || len(body) == 0 {
		return body
	}
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyMessagesHTTP,
		officialClientProfileModeFromConfig(s.cfg),
	)
	if len(profiles) > 0 {
		profile = profiles[0]
		err = nil
	}
	if err != nil {
		return body
	}

	model := gjson.GetBytes(body, "model").String()
	systemPromptInjectionEnabled, systemPrompt, systemPromptBlocks := s.claudeOAuthSystemPromptInjectionSettings(ctx)
	systemRewritten := false
	if !strings.Contains(strings.ToLower(model), "haiku") {
		// API Key 官方客户端画像的三段 system 是固定身份契约，不能受 OAuth 系统提示词
		// 注入开关影响。过滤真实 CLI 已带的固定身份块，只把 CWD 等项目上下文移入消息。
		if !systemPromptInjectionEnabled {
			systemPrompt = ""
			systemPromptBlocks = ""
		}
		customSystem := filterAnthropicAPIKeyMimicCustomSystem(rawJSONValue(body, "system"))
		body = rewriteSystemForNonClaudeCodeWithPromptBlocks(body, customSystem, systemPrompt, systemPromptBlocks)
		systemRewritten = true
	}

	metadataUserID := buildAPIKeyMimicMetadataUserID(account, body, safeClientHeaders(c), safeClientIP(c), getAPIKeyIDFromContext(c), profile.Build.Version)
	// 已有但不可解析的第三方 metadata.user_id 无法与官方 Session-Id header 保持一致。
	// 仅在这种情况下覆盖为规范 mimic metadata；合法 metadata 继续原样保留。
	if metadataUserID != "" {
		existing := gjson.GetBytes(body, "metadata.user_id").String()
		if existing != "" && ParseMetadataUserID(existing) == nil {
			if next, ok := setJSONValueBytes(body, "metadata.user_id", metadataUserID); ok {
				body = next
			}
		}
	}
	body, _ = normalizeClaudeOAuthRequestBody(body, model, claudeOAuthNormalizeOptions{
		stripSystemCacheControl:    !systemRewritten,
		injectMetadata:             metadataUserID != "",
		metadataUserID:             metadataUserID,
		preserveMissingTemperature: true,
		dropPlainAutoToolChoice:    true,
	})
	body = applyAnthropicAPIKeySDKCLIIdentity(body, profile)
	body = syncBillingHeaderVersion(body, profile.Build.UserAgent)

	body = s.rewriteMessageCacheControlIfEnabled(ctx, body)

	return body
}

const (
	claudeSDKCLIEntrypoint     = "cc_entrypoint=sdk-cli;"
	claudeDesktopEntrypoint    = "cc_entrypoint=claude-desktop-3p;"
	claudeSDKCLIIdentityPrompt = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
)

// filterAnthropicAPIKeyMimicCustomSystem 从真实 CLI system 中剔除 billing、身份和
// 标准 expansion，仅保留 CWD、项目约束等业务上下文，供重写器移入 messages。
func filterAnthropicAPIKeyMimicCustomSystem(system any) any {
	system = normalizeSystemParam(system)
	if system == nil {
		return nil
	}
	if text, ok := system.(string); ok {
		if isAnthropicAPIKeyMimicFixedSystemText(text) {
			return nil
		}
		return text
	}
	items, ok := system.([]any)
	if !ok {
		return system
	}
	custom := make([]any, 0, len(items))
	for _, item := range items {
		block, ok := item.(map[string]any)
		if !ok {
			custom = append(custom, item)
			continue
		}
		text, _ := block["text"].(string)
		if isAnthropicAPIKeyMimicFixedSystemText(text) {
			continue
		}
		custom = append(custom, item)
	}
	if len(custom) == 0 {
		return nil
	}
	return custom
}

func isAnthropicAPIKeyMimicFixedSystemText(text string) bool {
	trimmed := strings.TrimSpace(text)
	return strings.HasPrefix(trimmed, "x-anthropic-billing-header:") ||
		hasClaudeCodePrefix(trimmed) ||
		trimmed == strings.TrimSpace(claudeCodeSystemPromptExpansion)
}

// applyAnthropicAPIKeySDKCLIIdentity 只修正 API Key mimic 已生成的固定身份块。
// 使用精确匹配避免替换用户自定义 system 文本，也不会影响共用该构造链的 OAuth 请求。
func applyAnthropicAPIKeySDKCLIIdentity(body []byte, profiles ...officialClientResolvedProfile) []byte {
	entrypoint := claudeSDKCLIEntrypoint
	if len(profiles) > 0 && profiles[0].Build.Surface == "desktop" {
		entrypoint = claudeDesktopEntrypoint
	}
	system := gjson.GetBytes(body, "system")
	if !system.IsArray() {
		return body
	}
	out := body
	system.ForEach(func(index, item gjson.Result) bool {
		if item.Get("type").String() != "text" {
			return true
		}
		path := "system." + index.String() + ".text"
		text := item.Get("text").String()
		nextText := text
		if strings.HasPrefix(text, "x-anthropic-billing-header:") {
			nextText = strings.Replace(text, "cc_entrypoint=sdk-cli;", entrypoint, 1)
			nextText = strings.Replace(nextText, "cc_entrypoint=cli;", entrypoint, 1)
			nextText = strings.Replace(nextText, "cc_entrypoint=claude-desktop-3p;", entrypoint, 1)
		} else if strings.TrimSpace(text) == strings.TrimSpace(claudeCodeSystemPrompt) {
			nextText = claudeSDKCLIIdentityPrompt
		}
		if nextText != text {
			if next, ok := setJSONValueBytes(out, path, nextText); ok {
				out = next
			}
		}
		return true
	})
	return out
}

func buildAPIKeyMimicMetadataUserID(account *Account, body []byte, clientHeaders http.Header, clientIP string, apiKeyID int64, versions ...string) string {
	if account == nil {
		return ""
	}
	if existing := gjson.GetBytes(body, "metadata.user_id").String(); existing != "" && ParseMetadataUserID(existing) != nil {
		return ""
	}

	userAgent := ""
	if clientHeaders != nil {
		userAgent = clientHeaders.Get("User-Agent")
	}
	normalizedUserAgent := NormalizeSessionUserAgent(userAgent)
	clientDiscriminator := ""
	if strings.TrimSpace(clientIP) != "" || normalizedUserAgent != "" || apiKeyID > 0 {
		clientDiscriminator = sessionContextDiscriminator(&SessionContext{
			ClientIP:  strings.TrimSpace(clientIP),
			UserAgent: userAgent,
			APIKeyID:  apiKeyID,
		})
	}
	if clientDiscriminator == "" {
		clientDiscriminator = normalizedUserAgent
	}
	if clientDiscriminator == "" {
		clientDiscriminator = strconv.FormatInt(account.ID, 10)
	}
	deviceSeed := buildStableSessionSeed(account.ID, clientDiscriminator, "apikey-mimic-device")
	deviceHash := sha256.Sum256([]byte(deviceSeed))
	deviceID := hex.EncodeToString(deviceHash[:])

	sessionSeed := buildStableSessionSeed(account.ID, clientDiscriminator, extractFirstUserText(body))
	sessionID := generateSessionUUID(sessionSeed)
	accountUUID := strings.TrimSpace(account.GetExtraString("account_uuid"))
	uaVersion := officialAnthropicCLIVersion
	if len(versions) > 0 && strings.TrimSpace(versions[0]) != "" {
		uaVersion = strings.TrimSpace(versions[0])
	}
	return FormatMetadataUserID(deviceID, accountUUID, sessionID, uaVersion)
}

// apiKeyMimicSessionIDFromBody 从最终出站 body 中提取 Claude Code session ID。
// header 与 metadata 必须使用同一来源，避免无状态代理重复计算产生不一致。
func apiKeyMimicSessionIDFromBody(body []byte) string {
	metadataUserID := gjson.GetBytes(body, "metadata.user_id").String()
	parsed := ParseMetadataUserID(metadataUserID)
	if parsed == nil {
		return ""
	}
	return strings.TrimSpace(parsed.SessionID)
}

func rawJSONValue(body []byte, path string) any {
	result := gjson.GetBytes(body, path)
	if !result.Exists() {
		return nil
	}
	var value any
	if err := json.Unmarshal([]byte(result.Raw), &value); err != nil {
		return nil
	}
	return value
}

func (s *GatewayService) buildAnthropicAPIKeyCLICountTokensMimicRequest(
	ctx context.Context,
	account *Account,
	body []byte,
	token string,
	targetURL string,
	effectiveDropSet map[string]struct{},
) (*http.Request, []byte, error) {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeAnthropicAPIKeyCountTokensCompat,
		officialClientProfileModeFromConfig(s.cfg),
	)
	if err != nil {
		return nil, nil, err
	}
	body = sanitizeCountTokensRequestBody(body)
	modelID := gjson.GetBytes(body, "model").String()
	// 与 /v1/messages mimic 一致：保护当前客户端基线 beta，避免全局策略剥掉身份特征。
	defaultBetaHeader := buildDefaultAPIKeyMimicBetaHeaderForProfile(body, false, profile)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, splitAnthropicBetaTokens(profile.Wire.BetaHeader)...)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, anthropicAPIKeyMimicExtraBetas(modelID)...)
	finalBetaHeader := stripBetaTokensWithSet(defaultBetaHeader, effectiveDropSet)
	if blockErr := s.checkBetaPolicyBlockForHeader(ctx, finalBetaHeader, account, modelID); blockErr != nil {
		return nil, nil, blockErr
	}
	if sanitized, changed := sanitizeAnthropicBodyForBetaTokens(body, finalBetaHeader); changed {
		body = sanitized
	}
	if rw := buildClaudeCodeOAuthToolNameRewriteFromBody(body); rw != nil {
		body = applyToolNameRewriteNamesToBody(body, rw)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	setHeaderRaw(req.Header, "x-api-key", token)
	deleteHeaderAllForms(req.Header, "Authorization")
	applyAnthropicAPIKeyProfileHeaders(req.Header, profile)
	deleteHeaderAllForms(req.Header, "anthropic-beta")
	if finalBetaHeader != "" {
		setHeaderRaw(req.Header, "anthropic-beta", finalBetaHeader)
	}
	account.ApplyHeaderOverridesForAPIKeyMimic(req.Header)
	return req, body, nil
}

func safeClientHeaders(c *gin.Context) http.Header {
	if c == nil || c.Request == nil {
		return http.Header{}
	}
	return c.Request.Header
}

func safeClientIP(c *gin.Context) string {
	if c == nil {
		return ""
	}
	return strings.TrimSpace(c.ClientIP())
}

// applyAnthropicAPIKeyProfileHeaders 仅把已解析画像写入当前 API Key 请求，
// 不再修改 claude 包的共享常量，避免 OAuth、模型同步和连通性检查被连带污染。
func applyAnthropicAPIKeyProfileHeaders(header http.Header, profile officialClientResolvedProfile) {
	if header == nil {
		return
	}
	for _, item := range profile.Wire.StaticHeaders {
		setHeaderRaw(header, item.Name, item.Value)
	}
	for _, item := range profile.Build.RuntimeHeaders {
		setHeaderRaw(header, item.Name, item.Value)
	}
	setHeaderRaw(header, "User-Agent", profile.Build.UserAgent)
}

// normalizeAnthropicAPIKeyCLICacheProfile 把第三方客户端携带的断点收敛为
// Claude Code 2.1.220 API Key 实抓画像：system.1、system.2 和最后一个
// 可缓存消息块各保留一个 ephemeral，全部不携带 ttl，tools 不携带断点。
func normalizeAnthropicAPIKeyCLICacheProfile(body []byte) []byte {
	invalidThinking, messagePaths, toolPaths, systemPaths := collectCacheControlPaths(body)
	out := body
	for _, item := range invalidThinking {
		if next, ok := deleteJSONPathBytes(out, item.path); ok {
			out = next
		}
	}
	for _, paths := range [][]string{messagePaths, toolPaths, systemPaths} {
		for _, path := range paths {
			if next, ok := deleteJSONPathBytes(out, path); ok {
				out = next
			}
		}
	}
	if gjson.GetBytes(out, "cache_control").Exists() {
		if next, ok := deleteJSONPathBytes(out, "cache_control"); ok {
			out = next
		}
	}

	ephemeral := []byte(`{"type":"ephemeral"}`)
	if system := gjson.GetBytes(out, "system"); system.IsArray() && len(system.Array()) >= 3 {
		for _, path := range []string{"system.1.cache_control", "system.2.cache_control"} {
			if next, ok := setJSONRawBytes(out, path, ephemeral); ok {
				out = next
			}
		}
	}

	blockPath, stringPath, stringValue := lastOfficialAnthropicCacheableMessageContent(out)
	switch {
	case blockPath != "":
		if next, ok := setJSONRawBytes(out, blockPath+".cache_control", ephemeral); ok {
			out = next
		}
	case stringPath != "":
		rawBlock, err := json.Marshal([]map[string]any{{
			"type":          "text",
			"text":          stringValue,
			"cache_control": map[string]any{"type": "ephemeral"},
		}})
		if err == nil {
			if next, ok := setJSONRawBytes(out, stringPath, rawBlock); ok {
				out = next
			}
		}
	}
	return out
}
