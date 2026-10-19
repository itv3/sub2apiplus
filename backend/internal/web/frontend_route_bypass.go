package web

import "strings"

// shouldBypassEmbeddedFrontend 判断请求是否属于后端入口。它必须独立于
// embed 构建标签存在，确保正式镜像中的前端中间件不会吞掉已注册的裸路径别名。
func shouldBypassEmbeddedFrontend(path string) bool {
	trimmed := strings.TrimSpace(path)
	return strings.HasPrefix(trimmed, "/api/") ||
		strings.HasPrefix(trimmed, "/v1/") ||
		strings.HasPrefix(trimmed, "/v1beta/") ||
		strings.HasPrefix(trimmed, "/backend-api/") ||
		strings.HasPrefix(trimmed, "/antigravity/") ||
		strings.HasPrefix(trimmed, "/setup/") ||
		trimmed == "/health" ||
		trimmed == "/models" ||
		strings.HasPrefix(trimmed, "/models/") ||
		trimmed == "/messages/count_tokens" ||
		trimmed == "/responses" ||
		strings.HasPrefix(trimmed, "/responses/") ||
		trimmed == "/chat/completions" ||
		trimmed == "/embeddings" ||
		trimmed == "/alpha/search" ||
		// 以下裸路径别名来自上游：与 routes/gateway.go 注册的根路径别名一一对应。
		trimmed == "/web_search" ||
		trimmed == "/x_search" ||
		trimmed == "/tts" ||
		trimmed == "/stt" ||
		trimmed == "/realtime" ||
		trimmed == "/custom-voices" ||
		strings.HasPrefix(trimmed, "/custom-voices/") ||
		trimmed == "/contents/generations/tasks" ||
		strings.HasPrefix(trimmed, "/contents/generations/tasks/") ||
		trimmed == "/v3/contents/generations/tasks" ||
		strings.HasPrefix(trimmed, "/v3/contents/generations/tasks/") ||
		strings.HasPrefix(trimmed, "/images/") ||
		trimmed == "/videos" ||
		strings.HasPrefix(trimmed, "/videos/")
}
