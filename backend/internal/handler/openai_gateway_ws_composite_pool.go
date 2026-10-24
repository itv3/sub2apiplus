package handler

import (
	"context"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ip"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// compositeCandidatePoolContextForResponsesWS 为 Responses WebSocket 的首帧构造带候选池入口标记的上下文，
// 只用于 Composite 路由解析（Resolve），不写回请求，避免把首帧引用带进后续处理链。
//
// 为什么要在这里补标记：HTTP 入口的路由中间件对 GET 请求直接放行，WebSocket 握手本身就是 GET，
// 所以 WS 首帧的 Composite 解析一直由处理器自己做，且从未带过入口标记，候选池因此不处理它，
// 退回按「声明模型的平台数」判歧义的旧流程：同一个公开模型被多个平台声明时一律判歧义、
// 不命中，随后被平台白名单以 1008 关闭，Codex 把这种握手后失败当作网络错误反复重连。
//
// 首帧 response.create 的结构与 HTTP Responses 请求体同形（顶层 model、input、previous_response_id、
// prompt_cache_key 等字段平铺，只多一个 type 字段），候选池只按字段名读取，直接把首帧作为 Body 即可：
//   - 会话哈希：握手请求头里的 session_id 等，或首帧的 prompt_cache_key，或首帧内容摘要，
//     与调度器随后对同一帧、同一握手头计算的哈希一致，粘性查询才能对上；
//   - 按模型分流账号的上游协议：读首帧的 model；
//   - 续链：读首帧的 previous_response_id，规则与 HTTP 入口一致（只对 OpenAI 平台账号锁定归属）。
//
// 用户、分组、订阅的取法与 HTTP 入口的 withCompositeCandidatePoolEntry 一致。
func compositeCandidatePoolContextForResponsesWS(
	c *gin.Context,
	apiKey *service.APIKey,
	subject middleware2.AuthSubject,
	firstMessage []byte,
) context.Context {
	ctx := c.Request.Context()
	if apiKey == nil {
		return ctx
	}
	entry := service.CompositeCandidatePoolEntry{
		Endpoint:  service.CompositeRouteEndpointResponses,
		Body:      firstMessage,
		Header:    c.Request.Header,
		ClientIP:  ip.GetClientIP(c),
		UserAgent: c.GetHeader("User-Agent"),
		APIKeyID:  apiKey.ID,
		UserID:    apiKey.UserID,
		Group:     apiKey.Group,
	}
	if subject.UserID > 0 {
		entry.UserID = subject.UserID
	}
	if entry.UserID <= 0 && apiKey.User != nil {
		entry.UserID = apiKey.User.ID
	}
	if subscription, found := middleware2.GetSubscriptionFromContext(c); found {
		entry.Subscription = subscription
	}
	return service.WithCompositeCandidatePoolEntry(ctx, entry)
}
