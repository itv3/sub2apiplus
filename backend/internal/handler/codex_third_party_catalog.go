package handler

// codex_third_party_catalog.go 是「Codex 三方模型启用」在两个目录入口（OpenAI 网关 handler 与通用网关 handler）上的接点。
// 编排逻辑全部在 service.CodexThirdPartyCatalogService，这里只做注入、调用与错误写出。
// 服务未注入（nil）时所有方法保持原行为：直接写出原 manifest，不改变任何响应。

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// SetCodexThirdPartyCatalog 注入第三方模型目录服务（由依赖装配调用）。
func (h *OpenAIGatewayHandler) SetCodexThirdPartyCatalog(svc *service.CodexThirdPartyCatalogService) {
	if h != nil {
		h.codexThirdParty = svc
	}
}

// SetCodexThirdPartyCatalog 注入第三方模型目录服务（由依赖装配调用）。
func (h *GatewayHandler) SetCodexThirdPartyCatalog(svc *service.CodexThirdPartyCatalogService) {
	if h != nil {
		h.codexThirdParty = svc
	}
}

// codexBaseFetchContext 给 OpenAI 基础目录获取加时限：客户端对整次目录请求有 5 秒总超时，
// 超时即清空目录，所以基础目录获取最多占用其中一部分。未注入服务时原样返回请求上下文。
// 计时器在请求结束或到期时释放。
func (h *OpenAIGatewayHandler) codexBaseFetchContext(c *gin.Context) context.Context {
	parent := c.Request.Context()
	if h == nil || h.codexThirdParty == nil {
		return parent
	}
	ctx, cancel := h.codexThirdParty.BaseFetchContext(parent)
	context.AfterFunc(parent, cancel)
	return ctx
}

// writeCodexModelsResponseWithPolicy 先应用第三方模型策略，再调用原有出口函数计算 ETag 并写出。
// 固定账号、配置提前返回、调度三条成功路径都经过这里，保证最终策略检查只有一个入口。
func (h *OpenAIGatewayHandler) writeCodexModelsResponseWithPolicy(c *gin.Context, group *service.Group, manifest *service.OpenAIModelsResponse, clientETag string) {
	if h != nil && h.codexThirdParty != nil && manifest != nil {
		if err := h.codexThirdParty.ApplyToManifest(c.Request.Context(), group, manifest); err != nil {
			if c.Request.Context().Err() != nil {
				return
			}
			h.errorResponse(c, http.StatusServiceUnavailable, "upstream_error", infraerrors.Message(err))
			return
		}
	}
	writeCodexModelsResponseForClient(c, manifest, clientETag)
}

// writeCodexThirdPartyOnlyCatalog 处理 Composite 分组没有任何 OpenAI 账号的情形：
// 以空基础加已开启的第三方模型生成目录。分组里仍有 OpenAI 账号（只是暂时不可调度）时返回 false，
// 保持原有的「暂无可用账号」错误，避免把原生 GPT 从客户端选择器里清空。
func (h *OpenAIGatewayHandler) writeCodexThirdPartyOnlyCatalog(c *gin.Context, group *service.Group, clientETag string) bool {
	if h == nil || h.codexThirdParty == nil || group == nil || group.Platform != service.PlatformComposite {
		return false
	}
	hasBase, err := h.codexThirdParty.HasOpenAIBaseSource(c.Request.Context(), group)
	if err != nil || hasBase {
		return false
	}
	body, err := h.codexThirdParty.BuildThirdPartyOnly(c.Request.Context(), group)
	if err != nil {
		h.errorResponse(c, http.StatusServiceUnavailable, "upstream_error", infraerrors.Message(err))
		return true
	}
	writeCodexModelsResponseForClient(c, &service.OpenAIModelsResponse{Body: body}, clientETag)
	return true
}

// applyCodexThirdParty 对通用网关 handler 生成的目录正文应用第三方模型策略。
// 返回 false 表示已写出错误响应，调用方应直接返回。
func (h *GatewayHandler) applyCodexThirdParty(c *gin.Context, group *service.Group, body []byte) ([]byte, bool) {
	if h == nil || h.codexThirdParty == nil {
		return body, true
	}
	applied, err := h.codexThirdParty.ApplyToBody(c.Request.Context(), group, body)
	if err != nil {
		if c.Request.Context().Err() != nil {
			return body, false
		}
		h.errorResponse(c, http.StatusServiceUnavailable, "api_error", infraerrors.Message(err))
		return body, false
	}
	return applied, true
}
