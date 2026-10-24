package routes

import (
	"context"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ip"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// compositeCandidatePoolEndpoints 是进入账号级候选池的文本入口：路径（含不带 /v1 前缀的
// 根路径别名）到入站端点的精确映射。
//
// 这里刻意不复用 compositeRouteEndpointForPath 的子串匹配：那个函数会把 /alpha/search、
// /realtime/calls、/live 也归到 responses 端点，而它们（以及 /responses 的子路径如 compact、
// input_tokens，Responses WebSocket、Gemini 原生、生图、embeddings）按设计不进池，
// 行为与引入候选池之前保持一致。
var compositeCandidatePoolEndpoints = map[string]string{
	"/v1/messages":              service.CompositeRouteEndpointMessages,
	"/messages":                 service.CompositeRouteEndpointMessages,
	"/v1/messages/count_tokens": service.CompositeRouteEndpointCountTokens,
	"/messages/count_tokens":    service.CompositeRouteEndpointCountTokens,
	"/v1/responses":             service.CompositeRouteEndpointResponses,
	"/responses":                service.CompositeRouteEndpointResponses,
	"/v1/chat/completions":      service.CompositeRouteEndpointChatCompletions,
	"/chat/completions":         service.CompositeRouteEndpointChatCompletions,
}

// withCompositeCandidatePoolEntry 在请求路径属于候选池文本入口时，返回带入口标记的上下文，
// 供 Resolve 使用；其余入口原样返回请求上下文。返回的上下文只用于 Resolve，
// 不会写回请求，避免把请求体引用带进后续处理链。
func withCompositeCandidatePoolEntry(c *gin.Context, apiKey *service.APIKey, body []byte, endpoint string) context.Context {
	ctx := c.Request.Context()
	poolEndpoint, ok := compositeCandidatePoolEndpoints[strings.TrimRight(c.Request.URL.Path, "/")]
	if !ok || poolEndpoint != endpoint || apiKey == nil {
		return ctx
	}
	entry := service.CompositeCandidatePoolEntry{
		Endpoint:  poolEndpoint,
		Body:      body,
		Header:    c.Request.Header,
		ClientIP:  ip.GetClientIP(c),
		UserAgent: c.GetHeader("User-Agent"),
		APIKeyID:  apiKey.ID,
		UserID:    apiKey.UserID,
		Group:     apiKey.Group,
	}
	if subject, found := middleware.GetAuthSubjectFromContext(c); found && subject.UserID > 0 {
		entry.UserID = subject.UserID
	}
	if entry.UserID <= 0 && apiKey.User != nil {
		entry.UserID = apiKey.User.ID
	}
	if subscription, found := middleware.GetSubscriptionFromContext(c); found {
		entry.Subscription = subscription
	}
	return service.WithCompositeCandidatePoolEntry(ctx, entry)
}
