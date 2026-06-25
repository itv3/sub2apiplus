package service

import (
	"encoding/json"

	"github.com/Wei-Shaw/sub2api/internal/util/logredact"
)

func redactGatewayDebugBodyForLog(body []byte) string {
	if len(body) == 0 {
		return ""
	}
	extraKeys := []string{
		"prompt_cache_key",
		"session_id",
		"conversation_id",
		"metadata",
		"user_id",
		"instructions",
		"system",
		"input",
		"content",
		"text",
		"delta",
		"description",
		"arguments",
	}
	if json.Valid(body) {
		return logredact.RedactJSON(body, extraKeys...)
	}
	return logredact.RedactText(string(body), extraKeys...)
}
