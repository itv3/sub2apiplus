package service

import "net/http"

const (
	// Codex API Key mimic 抓包基线：codex_exec 0.144.1，2026-07（anyrouter.top 直连抓包）。
	codexDesktopOriginator   = "codex_exec"
	codexDesktopUserAgent    = "codex_exec/0.144.1 (Debian 12.0.0; aarch64) unknown (codex_exec; 0.144.1)"
	codexDesktopBetaFeatures = "remote_compaction_v2"
)

func applyOpenAIAPIKeyCodexMimicHeaders(req *http.Request, isStream bool, scopes ...openAIAPIKeyCodexMimicScope) {
	if req == nil {
		return
	}
	var scope openAIAPIKeyCodexMimicScope
	if len(scopes) > 0 {
		scope = scopes[0]
	}
	client := resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope)
	req.Header.Set("user-agent", client.UserAgent)
	req.Header.Set("originator", client.Originator)
	deleteHeaderAllForms(req.Header, "session_id")
	deleteHeaderAllForms(req.Header, "conversation_id")
	deleteHeaderAllForms(req.Header, "x-codex-turn-state")
	deleteHeaderAllForms(req.Header, "x-codex-turn-metadata")
	if client.IsDesktop {
		deleteHeaderAllForms(req.Header, "OpenAI-Beta")
		deleteHeaderAllForms(req.Header, "version")
		metadata := buildOpenAIAPIKeyCodexDesktopMetadata(scope)
		req.Header.Set("x-codex-beta-features", client.BetaFeatures)
		req.Header.Set("x-openai-internal-codex-responses-lite", "true")
		req.Header.Set("x-client-request-id", metadata.SessionID)
		req.Header.Set("session-id", metadata.SessionID)
		req.Header.Set("thread-id", metadata.ThreadID)
		req.Header.Set("x-codex-window-id", metadata.WindowID)
		req.Header.Set("x-codex-turn-metadata", metadata.TurnMetadata)
	} else {
		req.Header.Set("OpenAI-Beta", client.OpenAIBeta)
		req.Header.Set("version", client.Version)
	}
	if isStream {
		if req.Header.Get("accept") == "" {
			req.Header.Set("accept", "text/event-stream")
		}
	} else {
		if req.Header.Get("accept") == "" {
			req.Header.Set("accept", "application/json")
		}
	}
}
