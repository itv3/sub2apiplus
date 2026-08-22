package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	officialAnthropicCLIVersion = "2.1.220"
	officialAnthropicUserAgent  = "claude-cli/2.1.220 (external, sdk-cli)"
	officialAnthropicBetaHeader = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,effort-2025-11-24,extended-cache-ttl-2025-04-11"

	officialAnthropicSystemSplitMarker  = "# Text output (does not apply to tool calls)"
	officialAnthropicIngressContractKey = "official_anthropic_ingress_contract"
)

var (
	officialAnthropicGlobalCacheControl = []byte(`{"type":"ephemeral","ttl":"1h","scope":"global"}`)
	officialAnthropicLocalCacheControl  = []byte(`{"type":"ephemeral","ttl":"1h"}`)
)

type officialAnthropicIdentity struct {
	deviceID      string
	accountUUID   string
	sessionID     string
	deviceSource  OfficialEgressFieldSource
	sessionSource OfficialEgressFieldSource
}

type officialAnthropicIngressContract struct {
	metadataUserID                  string
	metadataUserIDSet               bool
	temperaturePresent              bool
	migratedSystemCacheControlFound bool
}

// finalizeAnthropicSetupTokenEgressRequest 在所有既有转换之后执行 Setup Token
// 产品语义的最小差异修正。Claude OAuth Persona 不得进入该兼容链。
// 未启用 Profile 时原样返回；启用后任何身份冲突或结构不满足都会明确失败。
func (s *GatewayService) finalizeAnthropicSetupTokenEgressRequest(
	req *http.Request,
	c *gin.Context,
	account *Account,
	body []byte,
) (*http.Request, []byte, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	if req == nil {
		return nil, nil, result, errors.New("anthropic official egress requires request")
	}
	egressContext, enabled := OfficialEgressContextFromContext(req.Context())
	if !enabled {
		return req, body, result, nil
	}
	if account == nil || account.Platform != PlatformAnthropic ||
		account.Type != AccountTypeSetupToken {
		return nil, nil, result, errors.New("Anthropic Setup Token finalizer 拒绝非 Setup Token 账号")
	}
	if c == nil || c.Request == nil {
		return nil, nil, result, errors.New("anthropic official egress requires ingress request")
	}

	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		egressContext.InboundEndpoint(),
	)
	if err != nil {
		return nil, nil, result, err
	}
	if profile.TargetPlatform != PlatformAnthropic {
		return nil, nil, result, errors.New("anthropic finalizer received non-Anthropic profile")
	}
	clientProfile, err := resolveOfficialClientProfileByID(profile.ID)
	if err != nil {
		return nil, nil, result, err
	}

	identity, err := resolveOfficialAnthropicIdentity(c, account, body)
	if err != nil {
		return nil, nil, result, err
	}
	if err := registerOfficialAnthropicIdentity(egressContext, identity); err != nil {
		return nil, nil, result, err
	}

	contract, _ := officialAnthropicIngressContractFromGin(c)
	finalBody, err := finalizeOfficialAnthropicBody(
		body,
		identity,
		"",
		clientProfile.Build.Version,
		contract.migratedSystemCacheControlFound,
	)
	if err != nil {
		return nil, nil, result, err
	}
	finalBody, err = finalizeOfficialAnthropicIngressDefaults(c, finalBody)
	if err != nil {
		return nil, nil, result, err
	}
	result.Modifications = append(result.Modifications,
		OfficialEgressModification{Kind: "body", Field: "metadata.user_id"},
		OfficialEgressModification{Kind: "body", Field: "system"},
	)

	clientRequestID := uuid.NewString()
	if err := egressContext.RegisterField(
		OfficialEgressFieldClientRequestID,
		clientRequestID,
		OfficialEgressFieldSourceDerived,
		OfficialEgressFieldLifecycleRequest,
	); err != nil {
		return nil, nil, result, err
	}
	if err := s.finalizeOfficialAnthropicHeaders(
		req,
		c,
		account,
		clientProfile,
		finalBody,
		identity.sessionID,
		clientRequestID,
	); err != nil {
		return nil, nil, result, err
	}
	result.Modifications = append(result.Modifications,
		OfficialEgressModification{Kind: "header", Field: "User-Agent"},
		OfficialEgressModification{Kind: "header", Field: "anthropic-beta"},
		OfficialEgressModification{Kind: "header", Field: "X-Claude-Code-Session-Id"},
		OfficialEgressModification{Kind: "header", Field: "x-client-request-id"},
	)

	if err := ValidateOfficialEgressFinalState(egressContext, profile); err != nil {
		return nil, nil, result, err
	}
	logOfficialEgressProfileResolved(egressContext, profile)
	resetOfficialEgressRequestBody(req, finalBody)
	return req, finalBody, result, nil
}

func resolveOfficialAnthropicIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
) (officialAnthropicIdentity, error) {
	if account == nil {
		return officialAnthropicIdentity{}, errors.New("anthropic official egress account is nil")
	}
	accountUUID := strings.TrimSpace(account.GetExtraString("account_uuid"))
	if _, err := uuid.Parse(accountUUID); err != nil {
		return officialAnthropicIdentity{}, errors.New("anthropic official egress requires account_uuid from account state")
	}

	rawUserID := strings.TrimSpace(gjson.GetBytes(body, "metadata.user_id").String())
	if contract, ok := officialAnthropicIngressContractFromGin(c); ok {
		if contract.metadataUserIDSet {
			rawUserID = strings.TrimSpace(contract.metadataUserID)
		} else {
			// 转换流程可能已经为第三方请求补过 metadata；入口未提供时不能把该值
			// 误记为显式官方身份，否则不同第三方会话可能共用旧派生 Session。
			rawUserID = ""
		}
	}
	parsed := ParseMetadataUserID(rawUserID)
	headerSessionID := strings.TrimSpace(c.GetHeader("X-Claude-Code-Session-Id"))
	if parsed != nil {
		if err := validateOfficialAnthropicParsedIdentity(parsed); err != nil {
			return officialAnthropicIdentity{}, err
		}
		if headerSessionID != "" && headerSessionID != parsed.SessionID {
			return officialAnthropicIdentity{}, errors.New("anthropic official egress session header conflicts with metadata")
		}
		return officialAnthropicIdentity{
			deviceID:      parsed.DeviceID,
			accountUUID:   accountUUID,
			sessionID:     parsed.SessionID,
			deviceSource:  OfficialEgressFieldSourceIngressExplicit,
			sessionSource: OfficialEgressFieldSourceIngressExplicit,
		}, nil
	}

	if headerSessionID != "" {
		if _, err := uuid.Parse(headerSessionID); err != nil {
			return officialAnthropicIdentity{}, errors.New("anthropic official egress session header must be UUID")
		}
	}

	deviceID, sessionID := deriveOfficialAnthropicIdentity(c, account, body, headerSessionID)
	sessionSource := OfficialEgressFieldSourceDerived
	if headerSessionID != "" {
		sessionID = headerSessionID
		sessionSource = OfficialEgressFieldSourceIngressExplicit
	}
	return officialAnthropicIdentity{
		deviceID:      deviceID,
		accountUUID:   accountUUID,
		sessionID:     sessionID,
		deviceSource:  OfficialEgressFieldSourceDerived,
		sessionSource: sessionSource,
	}, nil
}

func captureOfficialAnthropicIngressContract(c *gin.Context, body []byte) {
	if c == nil {
		return
	}
	metadataUserID := gjson.GetBytes(body, "metadata.user_id")
	system := gjson.GetBytes(body, "system")
	_, _, _, systemCachePaths := collectCacheControlPaths(body)
	c.Set(officialAnthropicIngressContractKey, officialAnthropicIngressContract{
		metadataUserID:                  metadataUserID.String(),
		metadataUserIDSet:               metadataUserID.Exists() && metadataUserID.Type == gjson.String,
		temperaturePresent:              gjson.GetBytes(body, "temperature").Exists(),
		migratedSystemCacheControlFound: len(systemCachePaths) > 0 && !isOfficialAnthropicSystemShape(system),
	})
}

func officialAnthropicIngressContractFromGin(
	c *gin.Context,
) (officialAnthropicIngressContract, bool) {
	if c == nil {
		return officialAnthropicIngressContract{}, false
	}
	value, exists := c.Get(officialAnthropicIngressContractKey)
	if !exists {
		return officialAnthropicIngressContract{}, false
	}
	contract, ok := value.(officialAnthropicIngressContract)
	return contract, ok
}

func finalizeOfficialAnthropicIngressDefaults(
	c *gin.Context,
	body []byte,
) ([]byte, error) {
	contract, ok := officialAnthropicIngressContractFromGin(c)
	removeTemperature := officialAnthropicThinkingEnabled(body) || (ok && !contract.temperaturePresent)
	fields := []string{"top_p", "top_k"}
	if removeTemperature {
		fields = append(fields, "temperature")
	}
	next := body
	for _, field := range fields {
		if !gjson.GetBytes(next, field).Exists() {
			continue
		}
		var err error
		next, err = sjson.DeleteBytes(next, field)
		if err != nil {
			return nil, fmt.Errorf("anthropic official egress failed to remove sampling field %s", field)
		}
	}
	return next, nil
}

func officialAnthropicThinkingEnabled(body []byte) bool {
	thinking := gjson.GetBytes(body, "thinking")
	if !thinking.Exists() {
		return false
	}
	thinkingType := strings.ToLower(strings.TrimSpace(thinking.Get("type").String()))
	return thinkingType == "enabled" || thinkingType == "adaptive"
}

func validateOfficialAnthropicParsedIdentity(parsed *ParsedUserID) error {
	if parsed == nil {
		return errors.New("anthropic official egress metadata identity is nil")
	}
	if len(parsed.DeviceID) != 64 {
		return errors.New("anthropic official egress requires a 64-character device_id")
	}
	if _, err := hex.DecodeString(parsed.DeviceID); err != nil {
		return errors.New("anthropic official egress device_id must be hexadecimal")
	}
	if _, err := uuid.Parse(parsed.SessionID); err != nil {
		return errors.New("anthropic official egress session_id must be UUID")
	}
	return nil
}

// deriveOfficialAnthropicIdentity 为没有 Claude Code 身份字段的第三方请求生成稳定身份。
// 设备身份按账号、API Key 与客户端产品稳定；会话身份优先使用客户端显式会话锚点，
// 没有锚点时退回首条用户消息，确保同一对话续轮稳定、不同对话相互隔离。
func deriveOfficialAnthropicIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
	headerSessionID string,
) (string, string) {
	userAgent := ""
	if c != nil {
		userAgent = c.GetHeader("User-Agent")
	}
	clientScope := fmt.Sprintf(
		"account=%d|api_key=%d|ua=%s",
		account.ID,
		getAPIKeyIDFromContext(c),
		NormalizeSessionUserAgent(userAgent),
	)
	deviceHash := sha256.Sum256([]byte("anthropic-official-egress-device|" + clientScope))
	deviceID := hex.EncodeToString(deviceHash[:])

	sessionAnchor := strings.TrimSpace(headerSessionID)
	if sessionAnchor == "" && c != nil {
		for _, name := range []string{
			"X-Session-Affinity",
			"session_id",
			"conversation_id",
			"X-Session-Id",
			"X-OpenCode-Session",
		} {
			if value := strings.TrimSpace(c.GetHeader(name)); value != "" {
				sessionAnchor = strings.ToLower(name) + "=" + value
				break
			}
		}
	}
	if sessionAnchor == "" {
		sessionAnchor = "first_user=" + extractOfficialAnthropicSessionAnchorText(body)
	}
	sessionID := generateSessionUUID(
		"anthropic-official-egress-session|" + clientScope + "|" + sessionAnchor,
	)
	return deviceID, sessionID
}

func extractOfficialAnthropicSessionAnchorText(body []byte) string {
	messages := gjson.GetBytes(body, "messages")
	if !messages.IsArray() {
		return ""
	}
	fallback := ""
	messages.ForEach(func(_, message gjson.Result) bool {
		if message.Get("role").String() != "user" {
			return true
		}
		content := message.Get("content")
		if content.Type == gjson.String {
			text := strings.TrimSpace(content.String())
			if fallback == "" {
				fallback = text
			}
			if isOfficialAnthropicSyntheticUserText(text) {
				return true
			}
			fallback = text
			return false
		}
		if !content.IsArray() {
			return true
		}
		found := false
		content.ForEach(func(_, block gjson.Result) bool {
			if block.Get("type").String() != "text" {
				return true
			}
			text := strings.TrimSpace(block.Get("text").String())
			if fallback == "" {
				fallback = text
			}
			if isOfficialAnthropicSyntheticUserText(text) {
				return true
			}
			fallback = text
			found = true
			return false
		})
		return !found
	})
	return fallback
}

func isOfficialAnthropicSyntheticUserText(text string) bool {
	return text == "" ||
		strings.HasPrefix(text, "[System Instructions]") ||
		strings.HasPrefix(text, "<system-reminder>")
}

func registerOfficialAnthropicIdentity(
	egressContext *OfficialEgressContext,
	identity officialAnthropicIdentity,
) error {
	fields := []struct {
		name      OfficialEgressFieldName
		value     string
		source    OfficialEgressFieldSource
		lifecycle OfficialEgressFieldLifecycle
	}{
		{
			name:      OfficialEgressFieldDeviceID,
			value:     identity.deviceID,
			source:    identity.deviceSource,
			lifecycle: OfficialEgressFieldLifecycleSession,
		},
		{
			name:      OfficialEgressFieldAccountUUID,
			value:     identity.accountUUID,
			source:    OfficialEgressFieldSourceAccountStatic,
			lifecycle: OfficialEgressFieldLifecycleSession,
		},
		{
			name:      OfficialEgressFieldSessionID,
			value:     identity.sessionID,
			source:    identity.sessionSource,
			lifecycle: OfficialEgressFieldLifecycleSession,
		},
	}
	for _, field := range fields {
		if err := egressContext.RegisterField(
			field.name,
			field.value,
			field.source,
			field.lifecycle,
		); err != nil {
			return err
		}
	}
	return nil
}

func finalizeOfficialAnthropicBody(
	body []byte,
	identity officialAnthropicIdentity,
	previousRequestID string,
	clientVersion string,
	preserveMigratedSystemCacheControl bool,
) ([]byte, error) {
	var err error
	body, err = normalizeOfficialAnthropicSystemShape(body)
	if err != nil {
		return nil, err
	}
	system := gjson.GetBytes(body, "system")
	if !system.IsArray() {
		return nil, errors.New("anthropic official egress requires system array")
	}
	rawBlocks := make([][]byte, 0, 4)
	system.ForEach(func(_, block gjson.Result) bool {
		rawBlocks = append(rawBlocks, []byte(block.Raw))
		return true
	})
	if len(rawBlocks) != 3 && len(rawBlocks) != 4 {
		return nil, fmt.Errorf("anthropic official egress requires three or four system blocks, got %d", len(rawBlocks))
	}
	for _, block := range rawBlocks {
		if gjson.GetBytes(block, "type").String() != "text" ||
			!gjson.GetBytes(block, "text").Exists() {
			return nil, errors.New("anthropic official egress requires text-only system blocks")
		}
	}

	billingText := buildOfficialAnthropicBillingText(body, previousRequestID, clientVersion)
	rawBlocks[0], err = setOfficialAnthropicSystemText(rawBlocks[0], billingText, nil)
	if err != nil {
		return nil, err
	}
	rawBlocks[1], err = setOfficialAnthropicSystemText(
		rawBlocks[1],
		claudeSDKCLIIdentityPrompt,
		nil,
	)
	if err != nil {
		return nil, err
	}

	if len(rawBlocks) == 3 {
		mergedText := gjson.GetBytes(rawBlocks[2], "text").String()
		boundary := "\n\n" + officialAnthropicSystemSplitMarker
		if strings.Count(mergedText, boundary) != 1 {
			return nil, errors.New("anthropic official egress system split marker is missing or ambiguous")
		}
		parts := strings.SplitN(mergedText, boundary, 2)
		rawBlocks[2], err = setOfficialAnthropicSystemText(
			rawBlocks[2],
			parts[0],
			officialAnthropicGlobalCacheControl,
		)
		if err != nil {
			return nil, err
		}
		fourthBlock, buildErr := setOfficialAnthropicSystemText(
			rawBlocks[2],
			officialAnthropicSystemSplitMarker+parts[1],
			officialAnthropicLocalCacheControl,
		)
		if buildErr != nil {
			return nil, buildErr
		}
		rawBlocks = append(rawBlocks, fourthBlock)
	} else {
		thirdText := gjson.GetBytes(rawBlocks[2], "text").String()
		fourthText := gjson.GetBytes(rawBlocks[3], "text").String()
		if strings.Contains(thirdText, officialAnthropicSystemSplitMarker) ||
			!strings.HasPrefix(fourthText, officialAnthropicSystemSplitMarker) {
			return nil, errors.New("anthropic official egress four-block system boundary is invalid")
		}
		rawBlocks[2], err = setOfficialAnthropicSystemText(
			rawBlocks[2],
			thirdText,
			officialAnthropicGlobalCacheControl,
		)
		if err != nil {
			return nil, err
		}
		rawBlocks[3], err = setOfficialAnthropicSystemText(
			rawBlocks[3],
			fourthText,
			officialAnthropicLocalCacheControl,
		)
		if err != nil {
			return nil, err
		}
	}

	nextBody, ok := setJSONRawBytes(body, "system", buildJSONArrayRaw(rawBlocks))
	if !ok {
		return nil, errors.New("anthropic official egress failed to replace system blocks")
	}
	nextBody, err = normalizeOfficialAnthropicCacheProfile(nextBody, preserveMigratedSystemCacheControl)
	if err != nil {
		return nil, err
	}
	metadataUserID := FormatMetadataUserID(
		identity.deviceID,
		identity.accountUUID,
		identity.sessionID,
		clientVersion,
	)
	nextBody, err = sjson.SetBytes(nextBody, "metadata.user_id", metadataUserID)
	if err != nil {
		return nil, errors.New("anthropic official egress failed to replace metadata.user_id")
	}
	orderedPayload, err := decodeOfficialJSONObjectUseNumber(nextBody)
	if err != nil {
		return nil, errors.New("anthropic official egress failed to decode final body")
	}
	orderedBody, err := marshalOfficialOrderedJSONObjectPreservingRaw(orderedPayload, []string{
		"model", "messages", "system", "tools", "tool_choice", "metadata",
		"max_tokens", "thinking", "temperature", "context_management",
		"stream", "output_config", "speed",
	}, nextBody)
	if err != nil {
		return nil, errors.New("anthropic official egress failed to encode ordered body")
	}
	return orderedBody, nil
}

// normalizeOfficialAnthropicCacheProfile 把第三方客户端携带的缓存断点收敛为
// Claude Code 2.1.220 当前实测画像：system 保留两个固定 1h 断点，messages
// 保留最后一个可缓存内容块；若原始 system 自带断点，则同时保留迁移到首条消息的
// 断点，确保稳定系统提示词可复用缓存。tools 不携带断点，总数最多为 4。
func normalizeOfficialAnthropicCacheProfile(body []byte, preserveMigratedSystemCacheControl bool) ([]byte, error) {
	invalidThinking, messagePaths, toolPaths, _ := collectCacheControlPaths(body)
	out := body
	preservedMessagePath := ""
	if preserveMigratedSystemCacheControl {
		const migratedPath = "messages.0.content.0.cache_control"
		for _, path := range messagePaths {
			if path == migratedPath {
				preservedMessagePath = path
				break
			}
		}
	}

	for _, item := range invalidThinking {
		next, ok := deleteJSONPathBytes(out, item.path)
		if !ok {
			return nil, errors.New("anthropic official egress failed to remove thinking cache_control")
		}
		out = next
	}
	for _, path := range toolPaths {
		next, ok := deleteJSONPathBytes(out, path)
		if !ok {
			return nil, errors.New("anthropic official egress failed to remove tool cache_control")
		}
		out = next
	}
	for _, path := range messagePaths {
		next, ok := deleteJSONPathBytes(out, path)
		if !ok {
			return nil, errors.New("anthropic official egress failed to remove message cache_control")
		}
		out = next
	}
	if gjson.GetBytes(out, "cache_control").Exists() {
		next, ok := deleteJSONPathBytes(out, "cache_control")
		if !ok {
			return nil, errors.New("anthropic official egress failed to remove top-level cache_control")
		}
		out = next
	}

	blockPath, stringPath, stringValue := lastOfficialAnthropicCacheableMessageContent(body)
	lastMessagePath := ""
	switch {
	case blockPath != "":
		next, err := sjson.SetRawBytes(
			out,
			blockPath+".cache_control",
			officialAnthropicLocalCacheControl,
		)
		if err != nil {
			return nil, errors.New("anthropic official egress failed to set message cache_control")
		}
		out = next
		lastMessagePath = blockPath + ".cache_control"
	case stringPath != "":
		rawBlock, err := json.Marshal([]map[string]any{{
			"type":          "text",
			"text":          stringValue,
			"cache_control": map[string]any{"type": "ephemeral", "ttl": "1h"},
		}})
		if err != nil {
			return nil, errors.New("anthropic official egress failed to build message cache block")
		}
		next, ok := setJSONRawBytes(out, stringPath, rawBlock)
		if !ok {
			return nil, errors.New("anthropic official egress failed to replace string message content")
		}
		out = next
		lastMessagePath = stringPath + ".0.cache_control"
	default:
		return nil, errors.New("anthropic official egress requires cacheable message content")
	}
	if preservedMessagePath != "" && preservedMessagePath != lastMessagePath {
		next, err := sjson.SetRawBytes(out, preservedMessagePath, officialAnthropicLocalCacheControl)
		if err != nil {
			return nil, errors.New("anthropic official egress failed to preserve migrated system cache_control")
		}
		out = next
	}

	_, finalMessagePaths, finalToolPaths, finalSystemPaths := collectCacheControlPaths(out)
	expectedMessagePathCount := 1
	if preservedMessagePath != "" && preservedMessagePath != lastMessagePath {
		expectedMessagePathCount = 2
	}
	if len(finalMessagePaths) != expectedMessagePathCount || len(finalToolPaths) != 0 || len(finalSystemPaths) != 2 {
		return nil, errors.New("anthropic official egress cache profile normalization failed")
	}
	return out, nil
}

// lastOfficialAnthropicCacheableMessageContent 返回最后一个可安全附加缓存断点的
// message 内容。thinking 块不能携带 cache_control，因此必须跳过。
func lastOfficialAnthropicCacheableMessageContent(body []byte) (
	blockPath string,
	stringPath string,
	stringValue string,
) {
	messages := gjson.GetBytes(body, "messages")
	if !messages.IsArray() {
		return "", "", ""
	}
	messageIndex := -1
	messages.ForEach(func(_, message gjson.Result) bool {
		messageIndex++
		content := message.Get("content")
		if content.Type == gjson.String {
			blockPath = ""
			stringPath = fmt.Sprintf("messages.%d.content", messageIndex)
			stringValue = content.String()
			return true
		}
		if !content.IsArray() {
			return true
		}
		contentIndex := -1
		content.ForEach(func(_, block gjson.Result) bool {
			contentIndex++
			blockType := block.Get("type").String()
			if blockType == "thinking" || blockType == "redacted_thinking" ||
				!strings.HasPrefix(strings.TrimSpace(block.Raw), "{") {
				return true
			}
			blockPath = fmt.Sprintf("messages.%d.content.%d", messageIndex, contentIndex)
			stringPath = ""
			stringValue = ""
			return true
		})
		return true
	})
	return blockPath, stringPath, stringValue
}

// normalizeOfficialAnthropicSystemShape 把标准 Anthropic system 输入归一化成当前
// Claude Code 画像的四段结构。第三方 system 指令会先迁移到 messages，避免为了改变
// 出站结构而丢失客户端语义；已经是官方结构的请求只补齐当前四段边界。
func normalizeOfficialAnthropicSystemShape(body []byte) ([]byte, error) {
	system := gjson.GetBytes(body, "system")
	if isOfficialAnthropicSystemShape(system) {
		rawBlocks := collectOfficialAnthropicSystemBlocks(system)
		if len(rawBlocks) == 3 {
			thirdText := gjson.GetBytes(rawBlocks[2], "text").String()
			boundary := "\n\n" + officialAnthropicSystemSplitMarker
			switch strings.Count(thirdText, boundary) {
			case 0:
				fourthBlock, err := marshalAnthropicSystemTextBlockWithCacheControl(
					officialAnthropicSystemSplitMarker,
					map[string]any{
						"type": "ephemeral",
						"ttl":  "1h",
					},
				)
				if err != nil {
					return nil, errors.New("anthropic official egress failed to build fourth system block")
				}
				rawBlocks = append(rawBlocks, fourthBlock)
				next, ok := setJSONRawBytes(body, "system", buildJSONArrayRaw(rawBlocks))
				if !ok {
					return nil, errors.New("anthropic official egress failed to append fourth system block")
				}
				return next, nil
			case 1:
				return body, nil
			default:
				return nil, errors.New("anthropic official egress system split marker is ambiguous")
			}
		}
		return body, nil
	}

	normalized := rewriteSystemForNonClaudeCodeWithPromptBlocks(
		body,
		rawJSONValue(body, "system"),
		"",
		"",
	)
	system = gjson.GetBytes(normalized, "system")
	if !isOfficialAnthropicSystemShape(system) {
		return nil, errors.New("anthropic official egress failed to normalize system structure")
	}
	return normalizeOfficialAnthropicSystemShape(normalized)
}

func isOfficialAnthropicSystemShape(system gjson.Result) bool {
	if !system.IsArray() {
		return false
	}
	rawBlocks := collectOfficialAnthropicSystemBlocks(system)
	if len(rawBlocks) != 3 && len(rawBlocks) != 4 {
		return false
	}
	for _, block := range rawBlocks {
		if gjson.GetBytes(block, "type").String() != "text" ||
			!gjson.GetBytes(block, "text").Exists() {
			return false
		}
	}
	firstText := strings.TrimSpace(gjson.GetBytes(rawBlocks[0], "text").String())
	secondText := strings.TrimSpace(gjson.GetBytes(rawBlocks[1], "text").String())
	return strings.HasPrefix(firstText, "x-anthropic-billing-header:") &&
		hasClaudeCodePrefix(secondText)
}

func collectOfficialAnthropicSystemBlocks(system gjson.Result) [][]byte {
	rawBlocks := make([][]byte, 0, 4)
	if !system.IsArray() {
		return rawBlocks
	}
	system.ForEach(func(_, block gjson.Result) bool {
		rawBlocks = append(rawBlocks, []byte(block.Raw))
		return true
	})
	return rawBlocks
}

func buildOfficialAnthropicBillingText(body []byte, _ string, versions ...string) string {
	clientVersion := officialAnthropicCLIVersion
	if len(versions) > 0 && strings.TrimSpace(versions[0]) != "" {
		clientVersion = strings.TrimSpace(versions[0])
	}
	fingerprint := computeOfficialAnthropicFingerprint(body, clientVersion)
	text := fmt.Sprintf(
		"x-anthropic-billing-header: cc_version=%s.%s; cc_entrypoint=sdk-cli;",
		clientVersion,
		fingerprint,
	)
	// 当前 cch 生成算法没有可验证来源，因此按方案约束省略，不能复制抓包值或恢复旧算法。
	return text
}

func computeOfficialAnthropicFingerprint(body []byte, version string) string {
	text := extractOfficialAnthropicFingerprintText(body)
	runes := []rune(text)
	chars := make([]rune, 0, 3)
	for _, index := range []int{4, 7, 20} {
		if index < len(runes) {
			chars = append(chars, runes[index])
		} else {
			chars = append(chars, '0')
		}
	}
	sum := sha256.Sum256([]byte(fingerprintSalt + string(chars) + version))
	return hex.EncodeToString(sum[:])[:3]
}

// extractOfficialAnthropicFingerprintText 跳过 Claude Code 自动插入的
// <system-reminder> 文本，取首个真实用户提示。阶段 0 五条请求均由该稳定首轮提示
// 生成 cc_version 后缀；续轮工具结果不得改变这个会话级指纹。
func extractOfficialAnthropicFingerprintText(body []byte) string {
	messages := gjson.GetBytes(body, "messages")
	if !messages.IsArray() {
		return ""
	}
	var firstText string
	messages.ForEach(func(_, message gjson.Result) bool {
		if message.Get("role").String() != "user" {
			return true
		}
		content := message.Get("content")
		if content.Type == gjson.String {
			text := content.String()
			if firstText == "" {
				firstText = text
			}
			if !strings.HasPrefix(strings.TrimSpace(text), "<system-reminder>") {
				firstText = text
				return false
			}
			return true
		}
		if !content.IsArray() {
			return true
		}
		found := false
		content.ForEach(func(_, block gjson.Result) bool {
			if block.Get("type").String() != "text" {
				return true
			}
			text := block.Get("text").String()
			if firstText == "" {
				firstText = text
			}
			if strings.HasPrefix(strings.TrimSpace(text), "<system-reminder>") {
				return true
			}
			firstText = text
			found = true
			return false
		})
		return !found
	})
	return firstText
}

func setOfficialAnthropicSystemText(block []byte, text string, cacheControl []byte) ([]byte, error) {
	next, err := sjson.SetBytes(block, "text", text)
	if err != nil {
		return nil, errors.New("anthropic official egress failed to update system text")
	}
	if len(cacheControl) == 0 {
		return deleteOfficialAnthropicCacheControl(next)
	}
	next, err = sjson.SetRawBytes(next, "cache_control", cacheControl)
	if err != nil {
		return nil, errors.New("anthropic official egress failed to update system cache_control")
	}
	return next, nil
}

func deleteOfficialAnthropicCacheControl(block []byte) ([]byte, error) {
	next, err := sjson.DeleteBytes(block, "cache_control")
	if err != nil {
		return nil, errors.New("anthropic official egress failed to remove system cache_control")
	}
	return next, nil
}

func (s *GatewayService) finalizeOfficialAnthropicHeaders(
	req *http.Request,
	c *gin.Context,
	account *Account,
	profile officialClientResolvedProfile,
	body []byte,
	sessionID string,
	clientRequestID string,
) error {
	header := req.Header
	for _, item := range profile.Wire.StaticHeaders {
		deleteHeaderAllForms(header, item.Name)
		setHeaderRaw(header, item.Name, item.Value)
	}
	for _, item := range profile.Build.RuntimeHeaders {
		deleteHeaderAllForms(header, item.Name)
		setHeaderRaw(header, item.Name, item.Value)
	}
	deleteHeaderAllForms(header, "User-Agent")
	setHeaderRaw(header, "User-Agent", profile.Build.UserAgent)
	stripOfficialEgressInboundHostHeaders(header)
	deleteHeaderAllForms(header, "anthropic-beta")
	betaHeader, err := s.resolveOfficialAnthropicBetaHeader(req, c, account, profile, body)
	if err != nil {
		return err
	}
	setHeaderRaw(header, "anthropic-beta", betaHeader)
	deleteHeaderAllForms(header, "X-Claude-Code-Session-Id")
	setHeaderRaw(header, "X-Claude-Code-Session-Id", sessionID)
	deleteHeaderAllForms(header, "x-client-request-id")
	setHeaderRaw(header, "x-client-request-id", clientRequestID)
	return nil
}

// resolveOfficialAnthropicBetaHeader 在生成官方动态 beta 后再次执行管理员策略。
// 静态客户端身份 beta 不能被 filter 静默剥离；动态能力 beta 可以被策略过滤，
// 但请求确实依赖该能力时必须显式失败，不能带着被削弱的请求继续访问上游。
func (s *GatewayService) resolveOfficialAnthropicBetaHeader(
	req *http.Request,
	c *gin.Context,
	account *Account,
	profile officialClientResolvedProfile,
	body []byte,
) (string, error) {
	modelID := strings.TrimSpace(gjson.GetBytes(body, "model").String())
	betas := splitAnthropicBetaTokens(buildOfficialAnthropicBetaHeader(profile, body))
	filterSet := s.getBetaPolicyFilterSet(req.Context(), c, account, modelID)
	filterSet = removeTokensFromSetCopy(filterSet, splitAnthropicBetaTokens(profile.Wire.BetaHeader)...)
	betas = filterBetaTokens(betas, filterSet)
	betaHeader := strings.Join(betas, ",")

	if apiKeyMimicBodyRequiresStructuredOutputs(body) &&
		anthropicModelSupportsStructuredOutputs(modelID) &&
		!containsBetaToken(betaHeader, AnthropicAPIKeyBetaStructuredOutputs) {
		return "", &BetaBlockedError{Message: "结构化输出请求需要 beta feature " + AnthropicAPIKeyBetaStructuredOutputs + "，但该 beta 已被过滤策略禁用"}
	}
	if officialAnthropicBodyRequiresAdvancedToolUse(body) &&
		!containsBetaToken(betaHeader, "advanced-tool-use-2025-11-20") {
		return "", &BetaBlockedError{Message: "延迟工具加载请求需要 beta feature advanced-tool-use-2025-11-20，但该 beta 已被过滤策略禁用"}
	}
	if blockErr := s.checkBetaPolicyBlockForTokens(req.Context(), betas, account, modelID); blockErr != nil {
		return "", blockErr
	}
	return betaHeader, nil
}

// buildOfficialAnthropicBetaHeader 构造 OAuth 官方出站的 beta 头。
// OAuth 链路与 API Key mimic 链路彼此独立：官方 CLI 默认流量不携带 context-1m，
// 因此这里不做 1M 自动补全（injectContext1M=false）；1M 补全仅属于开启了
// API Key 官方客户端兼容的 API 账号构造链。
func buildOfficialAnthropicBetaHeader(profile officialClientResolvedProfile, body []byte) string {
	betaHeader := buildDefaultAPIKeyMimicBetaHeaderForProfile(body, true, false, profile)
	betas := splitAnthropicBetaTokens(betaHeader)
	if officialAnthropicBodyRequiresAdvancedToolUse(body) {
		betas = appendAnthropicBetaToken(betas, "advanced-tool-use-2025-11-20")
	}
	return strings.Join(betas, ",")
}

func officialAnthropicBodyRequiresAdvancedToolUse(body []byte) bool {
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		return false
	}
	for _, tool := range tools.Array() {
		if isBedrockToolSearchType(tool.Get("type").String()) ||
			tool.Get("defer_loading").Bool() ||
			tool.Get("custom.defer_loading").Bool() ||
			hasCodeExecutionAllowedCallers(tool) ||
			hasInputExamples(tool) {
			return true
		}
	}
	return false
}
