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
	if account != nil && account.Platform == PlatformAnthropic && account.Type == AccountTypeAPIKey && !mimicAPIKeyClaudeCode {
		return nil
	}
	if s == nil || s.tlsFPProfileService == nil {
		return nil
	}
	return s.tlsFPProfileService.ResolveTLSProfile(account)
}

func defaultAPIKeyCountTokensMimicBetaHeader(body []byte) string {
	beta := defaultAPIKeyBetaHeader(body)
	return mergeAnthropicBeta([]string{beta, "token-counting-2024-11-01"}, "")
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
		strings.HasPrefix(modelID, "claude-opus-4-8")
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
	extraBetas := anthropicAPIKeyMimicExtraBetas(modelID)
	effectiveDropSet = removeTokensFromSetCopy(effectiveDropSet, extraBetas...)
	finalBetaHeader := stripBetaTokensWithSet(mergeAnthropicBeta(extraBetas, defaultAPIKeyBetaHeader(body)), effectiveDropSet)
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
	setHeaderRaw(req.Header, "content-type", "application/json")
	setHeaderRaw(req.Header, "anthropic-version", "2023-06-01")
	applyClaudeCodeMimicHeaders(req, reqStream)
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
	body, _ = normalizeClaudeOAuthRequestBody(body, model, claudeOAuthNormalizeOptions{
		stripSystemCacheControl: !systemRewritten,
		injectMetadata:          metadataUserID != "",
		metadataUserID:          metadataUserID,
	})

	body = s.rewriteMessageCacheControlIfEnabled(ctx, body)

	return body
}

func buildAPIKeyMimicMetadataUserID(account *Account, body []byte, clientHeaders http.Header, clientIP string, apiKeyID int64) string {
	if account == nil {
		return ""
	}
	if existing := gjson.GetBytes(body, "metadata.user_id").String(); existing != "" {
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
	finalBetaHeader := stripBetaTokensWithSet(defaultAPIKeyCountTokensMimicBetaHeader(body), effectiveDropSet)
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
