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
	return resolveAnthropicTLSProfileForRequest(account, mimicAPIKeyClaudeCode, s.tlsFPProfileService)
}

func defaultAPIKeyCountTokensMimicBetaHeader(body []byte) string {
	// 当前没有官方 count_tokens 对照样本，保持既有固定尾部，不把 /v1/messages
	// 的 structured-outputs 条件规则无证据扩展到该接口。
	beta := buildDefaultAPIKeyMimicBetaHeader(body, false)
	return mergeAnthropicBeta([]string{beta, "token-counting-2024-11-01"}, "")
}

// defaultAPIKeyMimicBetaHeader 返回 Anthropic API Key mimic Claude Code 时的 anthropic-beta，
// 对齐官方 Claude Desktop 2.1.209 抓包的完整 beta 列表（仅 mimic 路径使用）。
// haiku 模型上游不做第三方判定，沿用精简 haiku header。
func defaultAPIKeyMimicBetaHeader(body []byte) string {
	return buildDefaultAPIKeyMimicBetaHeader(body, true)
}

func buildDefaultAPIKeyMimicBetaHeader(body []byte, selectStructuredOutputs bool) string {
	modelID := gjson.GetBytes(body, "model").String()
	if strings.Contains(strings.ToLower(modelID), "haiku") {
		return claude.APIKeyHaikuBetaHeader
	}
	betas := claude.APIKeyMimicBetas()
	// Desktop 抓包中的稳定身份列表已经包含 context-1m；保留分支以兼容现有调用契约。
	if requiresContext1MBetaForAPIKeyMimic(modelID) {
		betas = claude.APIKeyMimicBetasWithContext1M()
	}
	// 官方桌面客户端会根据最终出站 body 选择末尾 beta：结构化输出请求使用
	// structured-outputs，普通请求使用 fallback-credit。这里在 body 规范化之后调用，
	// 因而不会基于客户端尚未重写的原始 body 作出错误判断。
	if selectStructuredOutputs && apiKeyMimicBodyRequiresStructuredOutputs(body) {
		for i, beta := range betas {
			if beta == claude.BetaFallbackCredit {
				betas[i] = claude.BetaStructuredOutputs
				break
			}
		}
	}
	return strings.Join(betas, ",")
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
	body = s.applyAnthropicAPIKeyClaudeCodeMimicryToBody(ctx, c, account, body)
	body = enforceCacheControlLimit(body)
	modelID := gjson.GetBytes(body, "model").String()
	// Desktop 基线 beta（含 context-1m）是官方客户端身份形态：先算默认列表，再把这些
	// token 从 drop set 中移除。不能让全局 BetaPolicy 把 context-1m 等身份 beta 剥掉，
	// 否则会偏离 Desktop 抓包，且多数中转站会直接 400 要求启用 1m。
	// 注意：不要把这些 token 再以 required 方式重复合并，否则会破坏官方顺序。
	defaultBetaHeader := defaultAPIKeyMimicBetaHeader(body)
	// 只保护静态 Desktop 身份基线；structured-outputs 等按 body 动态替换的 beta
	// 仍必须受 BetaPolicy 控制。
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, claude.APIKeyMimicBetas()...)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, anthropicAPIKeyMimicExtraBetas(modelID)...)
	finalBetaHeader := stripBetaTokensWithSet(defaultBetaHeader, effectiveDropSet)
	if apiKeyMimicBodyRequiresStructuredOutputs(body) &&
		!strings.Contains(strings.ToLower(modelID), "haiku") &&
		!anthropicBetaTokensContains(finalBetaHeader, claude.BetaStructuredOutputs) {
		return nil, nil, &BetaBlockedError{Message: "结构化输出请求需要 beta feature " + claude.BetaStructuredOutputs + "，但该 beta 已被过滤策略禁用"}
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
	body = enforceCacheControlLimit(body)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	setHeaderRaw(req.Header, "x-api-key", token)
	setHeaderRaw(req.Header, "Authorization", "Bearer "+token)
	// 官方 Claude Desktop 2.1.209 直连 HTTP/1.1 中转站时使用 Node 风格的 Header
	// 文本形态。这里只调整 API Key mimic 的 /v1/messages 构造链，不改变 OAuth、
	// count_tokens 或其他平台的共享 Header 规则。
	setHeaderRaw(req.Header, "Content-Type", "application/json")
	setHeaderRaw(req.Header, "Accept-Encoding", "gzip, deflate, br, zstd")
	// HTTP/1.1 默认支持长连接；显式写入仅用于尽量贴近官方 wire 形态。
	// 最终是否保留仍由 Go Transport 按实际协议安全处理。
	setHeaderRaw(req.Header, "Connection", "keep-alive")
	setHeaderRaw(req.Header, "anthropic-version", "2023-06-01")
	applyClaudeCodeMimicHeaders(req, reqStream)
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
) []byte {
	if account == nil || len(body) == 0 {
		return body
	}

	model := gjson.GetBytes(body, "model").String()
	systemPromptInjectionEnabled, systemPrompt, systemPromptBlocks := s.claudeOAuthSystemPromptInjectionSettings(ctx)
	systemRewritten := false
	if systemPromptInjectionEnabled && !strings.Contains(strings.ToLower(model), "haiku") {
		body = rewriteSystemForNonClaudeCodeWithPromptBlocks(body, rawJSONValue(body, "system"), systemPrompt, systemPromptBlocks)
		systemRewritten = true
	}

	metadataUserID := buildAPIKeyMimicMetadataUserID(account, body, safeClientHeaders(c), safeClientIP(c), getAPIKeyIDFromContext(c))
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
	body = applyAnthropicAPIKeySDKCLIIdentity(body)

	body = s.rewriteMessageCacheControlIfEnabled(ctx, body)

	return body
}

const (
	claudeSDKCLIEntrypoint     = "cc_entrypoint=claude-desktop-3p;"
	claudeSDKCLIIdentityPrompt = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
)

// applyAnthropicAPIKeySDKCLIIdentity 只修正 API Key mimic 已生成的固定身份块。
// 使用精确匹配避免替换用户自定义 system 文本，也不会影响共用该构造链的 OAuth 请求。
func applyAnthropicAPIKeySDKCLIIdentity(body []byte) []byte {
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
			nextText = strings.Replace(text, "cc_entrypoint=cli;", claudeSDKCLIEntrypoint, 1)
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

func buildAPIKeyMimicMetadataUserID(account *Account, body []byte, clientHeaders http.Header, clientIP string, apiKeyID int64) string {
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
	uaVersion := ExtractCLIVersion(claude.DefaultHeaders["User-Agent"])
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
	body = sanitizeCountTokensRequestBody(body)
	modelID := gjson.GetBytes(body, "model").String()
	// 与 /v1/messages mimic 一致：保护 Desktop 基线 beta，避免全局策略剥掉 context-1m。
	defaultBetaHeader := defaultAPIKeyCountTokensMimicBetaHeader(body)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, claude.APIKeyMimicBetas()...)
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
	setHeaderRaw(req.Header, "content-type", "application/json")
	setHeaderRaw(req.Header, "anthropic-version", "2023-06-01")
	applyClaudeCodeMimicHeaders(req, false)
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
