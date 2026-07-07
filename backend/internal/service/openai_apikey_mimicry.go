package service

import "net/http"

const (
	codexDesktopOriginator   = "Codex Desktop"
	codexDesktopUserAgent    = "Codex Desktop/0.142.0 (Mac OS 26.5.1; arm64) dumb (codex_exec; 0.142.0)"
	codexDesktopBetaFeatures = "memories,remote_compaction_v2"
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
