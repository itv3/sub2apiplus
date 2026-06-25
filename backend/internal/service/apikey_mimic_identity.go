package service

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/google/uuid"
)

const openAIAPIKeyCodexMimicProfileVersion = "codex_api_key_mimic_phase1_v2"

type openAIAPIKeyCodexMimicScope struct {
	AccountID           int64
	APIKeyID            int64
	UpstreamBaseURL     string
	ServerSalt          string
	ClientProfile       string
	TurnID              string
	TurnStartedAtUnixMS int64
}

func resolveOpenAIAPIKeyCodexMimicScope(account *Account, apiKeyID int64, cfgs ...*config.Config) openAIAPIKeyCodexMimicScope {
	scope := openAIAPIKeyCodexMimicScope{APIKeyID: apiKeyID}
	if len(cfgs) > 0 {
		scope.ServerSalt = openAIAPIKeyCodexPromptCacheKeyServerSalt(cfgs[0])
	}
	if account == nil {
		return scope
	}
	scope.AccountID = account.ID
	scope.UpstreamBaseURL = strings.TrimSpace(account.GetOpenAIBaseURL())
	if scope.UpstreamBaseURL == "" {
		scope.UpstreamBaseURL = "https://api.openai.com"
	}
	return scope
}

func applyOpenAIAPIKeyCodexMimicryToBody(body []byte, scopes ...openAIAPIKeyCodexMimicScope) []byte {
	if len(body) == 0 {
		return body
	}
	var reqBody map[string]any
	if err := json.Unmarshal(body, &reqBody); err != nil {
		return body
	}
	var scope openAIAPIKeyCodexMimicScope
	if len(scopes) > 0 {
		scope = scopes[0]
	}
	client := resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope)
	modified := applyCodexResponsesNormalization(reqBody, codexResponsesNormalizationOptions{
		NormalizeBareRoleContentMessages: true,
		ConvertSystemRoleToDeveloper:     true,
		EnsureDefaultInstructions:        true,
		EnsureStream:                     true,
		EnsureStoreFalse:                 true,
		EnsureReasoningEncryptedContent:  true,
	})
	if ensureOpenAIAPIKeyCodexPromptCacheKey(reqBody, scope) {
		modified = true
	}
	if client.IsDesktop && ensureOpenAIAPIKeyCodexDesktopClientMetadata(reqBody, scope) {
		modified = true
	}
	if !modified {
		return body
	}
	out, err := marshalOpenAIUpstreamJSON(reqBody)
	if err != nil {
		return body
	}
	return out
}

func ensureOpenAIAPIKeyCodexPromptCacheKey(reqBody map[string]any, scope openAIAPIKeyCodexMimicScope) bool {
	if resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope).IsDesktop {
		sessionID := buildOpenAIAPIKeyCodexDesktopMetadata(scope).SessionID
		if existing, ok := reqBody["prompt_cache_key"].(string); ok && strings.TrimSpace(existing) == sessionID {
			return false
		}
		reqBody["prompt_cache_key"] = sessionID
		return true
	}
	if existing, ok := reqBody["prompt_cache_key"].(string); ok && strings.TrimSpace(existing) != "" {
		return false
	}
	seed := buildOpenAIAPIKeyCodexPromptCacheKeySeed(reqBody, scope)
	if strings.TrimSpace(seed) == "" {
		seed = fmt.Sprintf("body_fields=%d", len(reqBody))
	}
	reqBody["prompt_cache_key"] = "codex-mimic-" + hashSensitiveValueForLog(seed)
	return true
}

func openAIAPIKeyCodexPromptCacheKeyServerSalt(cfg *config.Config) string {
	if cfg == nil {
		return ""
	}
	if secret := strings.TrimSpace(cfg.JWT.Secret); secret != "" {
		return hashSensitiveValueForLog("jwt_secret:" + secret)
	}
	return ""
}

func buildOpenAIAPIKeyCodexPromptCacheKeySeed(reqBody map[string]any, scope openAIAPIKeyCodexMimicScope) string {
	parts := make([]string, 0, 8)
	parts = append(parts, "profile="+openAIAPIKeyCodexMimicProfileVersion)
	if profileID := strings.TrimSpace(scope.ClientProfile); profileID != "" {
		parts = append(parts, "client_profile="+profileID)
	}
	if strings.TrimSpace(scope.ServerSalt) != "" {
		parts = append(parts, "server_salt="+scope.ServerSalt)
	}
	if scope.AccountID != 0 {
		parts = append(parts, fmt.Sprintf("account_id=%d", scope.AccountID))
	}
	if scope.APIKeyID != 0 {
		parts = append(parts, fmt.Sprintf("api_key_id=%d", scope.APIKeyID))
	}
	if upstreamBaseURL := strings.TrimSpace(scope.UpstreamBaseURL); upstreamBaseURL != "" {
		parts = append(parts, "upstream_base_url="+strings.ToLower(upstreamBaseURL))
	}
	if model, _ := reqBody["model"].(string); strings.TrimSpace(model) != "" {
		parts = append(parts, "model="+strings.TrimSpace(model))
	}
	return strings.Join(parts, "|")
}

type openAIAPIKeyCodexDesktopMetadata struct {
	InstallationID      string
	SessionID           string
	ThreadID            string
	TurnID              string
	WindowID            string
	TurnStartedAtUnixMS int64
	TurnMetadata        string
}

func buildOpenAIAPIKeyCodexDesktopMetadata(scope openAIAPIKeyCodexMimicScope) openAIAPIKeyCodexDesktopMetadata {
	sessionID := deterministicOpenAICodexUUID("session|" + buildOpenAIAPIKeyCodexDesktopSeed(scope))
	turnID := strings.TrimSpace(scope.TurnID)
	if turnID == "" {
		turnID = uuid.NewString()
	}
	installationID := deterministicOpenAICodexUUID("installation|" + buildOpenAIAPIKeyCodexDesktopSeed(scope))
	windowID := sessionID + ":0"
	turnStartedAtUnixMS := scope.TurnStartedAtUnixMS
	if turnStartedAtUnixMS <= 0 {
		turnStartedAtUnixMS = time.Now().UnixMilli()
	}
	turnMetadata := map[string]any{
		"installation_id":         installationID,
		"session_id":              sessionID,
		"thread_id":               sessionID,
		"turn_id":                 turnID,
		"window_id":               windowID,
		"request_kind":            "turn",
		"thread_source":           "user",
		"sandbox":                 "seatbelt",
		"turn_started_at_unix_ms": turnStartedAtUnixMS,
	}
	turnMetadataBytes, _ := json.Marshal(turnMetadata)
	return openAIAPIKeyCodexDesktopMetadata{
		InstallationID:      installationID,
		SessionID:           sessionID,
		ThreadID:            sessionID,
		TurnID:              turnID,
		WindowID:            windowID,
		TurnStartedAtUnixMS: turnStartedAtUnixMS,
		TurnMetadata:        string(turnMetadataBytes),
	}
}

func buildOpenAIAPIKeyCodexDesktopSeed(scope openAIAPIKeyCodexMimicScope) string {
	parts := make([]string, 0, 7)
	parts = append(parts, "client_profile="+openAIAPIKeyCodexMimicClientDesktop0142)
	if strings.TrimSpace(scope.ServerSalt) != "" {
		parts = append(parts, "server_salt="+scope.ServerSalt)
	}
	if scope.AccountID != 0 {
		parts = append(parts, fmt.Sprintf("account_id=%d", scope.AccountID))
	}
	if scope.APIKeyID != 0 {
		parts = append(parts, fmt.Sprintf("api_key_id=%d", scope.APIKeyID))
	}
	if upstreamBaseURL := strings.TrimSpace(scope.UpstreamBaseURL); upstreamBaseURL != "" {
		parts = append(parts, "upstream_base_url="+strings.ToLower(upstreamBaseURL))
	}
	if len(parts) == 1 {
		parts = append(parts, "anonymous_scope")
	}
	return strings.Join(parts, "|")
}

func deterministicOpenAICodexUUID(seed string) string {
	sum := sha256.Sum256([]byte(seed))
	b := sum[:16]
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func ensureOpenAIAPIKeyCodexDesktopClientMetadata(reqBody map[string]any, scope openAIAPIKeyCodexMimicScope) bool {
	metadata := buildOpenAIAPIKeyCodexDesktopMetadata(scope)
	values := map[string]string{
		"x-codex-installation-id": metadata.InstallationID,
		"session_id":              metadata.SessionID,
		"thread_id":               metadata.ThreadID,
		"turn_id":                 metadata.TurnID,
		"x-codex-window-id":       metadata.WindowID,
		"x-codex-turn-metadata":   metadata.TurnMetadata,
	}
	switch existing := reqBody["client_metadata"].(type) {
	case map[string]any:
		modified := false
		for k, v := range values {
			if existingStringValueIsEmpty(existing[k]) {
				existing[k] = v
				modified = true
			}
		}
		if modified {
			reqBody["client_metadata"] = existing
		}
		return modified
	case map[string]string:
		next := make(map[string]any, len(existing)+len(values))
		for k, v := range existing {
			next[k] = v
		}
		modified := false
		for k, v := range values {
			if strings.TrimSpace(existing[k]) == "" {
				next[k] = v
				modified = true
			}
		}
		if modified {
			reqBody["client_metadata"] = next
		}
		return modified
	case nil:
		next := make(map[string]any, len(values))
		for k, v := range values {
			next[k] = v
		}
		reqBody["client_metadata"] = next
		return true
	default:
		return false
	}
}

func existingStringValueIsEmpty(v any) bool {
	switch s := v.(type) {
	case nil:
		return true
	case string:
		return strings.TrimSpace(s) == ""
	default:
		return false
	}
}
