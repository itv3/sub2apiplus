package service

import "net/http"

const (
	// previous 回退画像：历史 Codex Desktop 抓包，2026-07。
	codexDesktopOriginator   = "Codex Desktop"
	codexDesktopUserAgent    = "Codex Desktop/0.144.0-alpha.4 (Mac OS 26.5.2; arm64) unknown (Codex Desktop; 26.707.51957)"
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
	deleteHeaderAllForms(req.Header, "x-codex-beta-features")
	deleteHeaderAllForms(req.Header, "x-openai-internal-codex-responses-lite")
	for _, item := range client.StaticHeaders {
		setHeaderRaw(req.Header, item.Name, item.Value)
	}
	if client.RequiresMetadata {
		deleteHeaderAllForms(req.Header, "OpenAI-Beta")
		deleteHeaderAllForms(req.Header, "version")
		metadata := buildOpenAIAPIKeyCodexClientMetadata(scope)
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
