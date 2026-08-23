// Package gatewayendpoint 提供网关入口路径的纯规范化逻辑。
//
// 该包不依赖 handler 或 service，避免官方出站画像为了复用入口规范化而形成循环依赖。
package gatewayendpoint

import "strings"

const (
	Messages             = "/v1/messages"
	MessagesCountTokens  = "/v1/messages/count_tokens"
	ChatCompletions      = "/v1/chat/completions"
	Embeddings           = "/v1/embeddings"
	AlphaSearch          = "/v1/alpha/search"
	Responses            = "/v1/responses"
	ResponsesCompact     = "/v1/responses/compact"
	ResponsesInputTokens = "/v1/responses/input_tokens"
	ImagesGenerations    = "/v1/images/generations"
	ImagesEdits          = "/v1/images/edits"
	ImageTasks           = "/v1/images/tasks"
	VideosGenerations    = "/v1/videos/generations"
	VideosEdits          = "/v1/videos/edits"
	VideosExtensions     = "/v1/videos/extensions"
	Videos               = "/v1/videos"
	GeminiModels         = "/v1beta/models"
)

// NormalizeInboundEndpoint 将带平台前缀、裸路径或 Codex 直连别名的入口路径
// 统一为网关内部使用的规范端点。compact 与 input_tokens 必须先于 Responses 根路径判断。
func NormalizeInboundEndpoint(path string) string {
	path = strings.TrimSpace(path)
	switch {
	case strings.Contains(path, Embeddings):
		return Embeddings
	case strings.Contains(path, AlphaSearch) || isBareOrSubpathOf(strings.TrimRight(path, "/"), "/alpha/search") || isBareOrSubpathOf(strings.TrimRight(path, "/"), "/backend-api/codex/alpha/search"):
		return AlphaSearch
	case strings.Contains(path, ChatCompletions) || isBareOrSubpathOf(strings.TrimRight(path, "/"), "/chat/completions"):
		return ChatCompletions
	case strings.Contains(path, MessagesCountTokens):
		return MessagesCountTokens
	case strings.Contains(path, Messages):
		return Messages
	case strings.Contains(path, ImagesGenerations) || strings.Contains(path, "/images/generations"):
		return ImagesGenerations
	case strings.Contains(path, ImagesEdits) || strings.Contains(path, "/images/edits"):
		return ImagesEdits
	case strings.Contains(path, ImageTasks) || strings.Contains(path, "/images/tasks/"):
		return ImageTasks
	case strings.Contains(path, VideosGenerations) || strings.Contains(path, "/videos/generations"):
		return VideosGenerations
	case strings.Contains(path, VideosEdits) || strings.Contains(path, "/videos/edits"):
		return VideosEdits
	case strings.Contains(path, VideosExtensions) || strings.Contains(path, "/videos/extensions"):
		return VideosExtensions
	case strings.Contains(path, Videos) || strings.Contains(path, "/videos/"):
		return Videos
	case strings.Contains(path, ResponsesCompact) || isResponsesCompactAliasPath(path):
		return ResponsesCompact
	case strings.Contains(path, ResponsesInputTokens) || isResponsesInputTokensAliasPath(path):
		return ResponsesInputTokens
	case strings.Contains(path, Responses) || isResponsesRootAliasPath(path):
		return Responses
	case strings.Contains(path, GeminiModels):
		return GeminiModels
	default:
		return path
	}
}

func isResponsesInputTokensAliasPath(path string) bool {
	trimmed := strings.TrimRight(strings.TrimSpace(path), "/")
	if trimmed == "" {
		return false
	}
	return isBareOrSubpathOf(trimmed, "/responses/input_tokens") ||
		isBareOrSubpathOf(trimmed, "/backend-api/codex/responses/input_tokens")
}

func isResponsesCompactAliasPath(path string) bool {
	trimmed := strings.TrimRight(strings.TrimSpace(path), "/")
	if trimmed == "" {
		return false
	}
	return isBareOrSubpathOf(trimmed, "/responses/compact") ||
		isBareOrSubpathOf(trimmed, "/backend-api/codex/responses/compact")
}

func isResponsesRootAliasPath(path string) bool {
	trimmed := strings.TrimRight(strings.TrimSpace(path), "/")
	if trimmed == "" {
		return false
	}
	return isBareOrSubpathOf(trimmed, "/responses") ||
		isBareOrSubpathOf(trimmed, "/backend-api/codex/responses")
}

func isBareOrSubpathOf(path, root string) bool {
	return path == root || strings.HasPrefix(path, root+"/")
}
