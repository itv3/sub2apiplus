package service

import "net/http"

func applyOpenAIAPIKeyCodexMimicHeaders(req *http.Request, isStream bool) {
	if req == nil {
		return
	}
	req.Header.Set("user-agent", codexCLIUserAgent)
	req.Header.Set("originator", "codex_cli_rs")
	req.Header.Set("OpenAI-Beta", "responses=experimental")
	req.Header.Set("version", codexCLIVersion)
	req.Header.Del("session_id")
	req.Header.Del("conversation_id")
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
