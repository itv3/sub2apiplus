package handler

// codex_third_party_catalog.go 是「Codex 三方模型启用」在两个目录入口（OpenAI 网关 handler 与通用网关 handler）上的接点。
// 编排逻辑全部在 service.CodexThirdPartyCatalogService，这里只做注入、调用与错误写出。
// 服务未注入（nil）时所有方法保持原行为：直接写出原 manifest，不改变任何响应。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// SetCodexThirdPartyCatalog 注入第三方模型目录服务（由依赖装配调用）。
func (h *OpenAIGatewayHandler) SetCodexThirdPartyCatalog(svc *service.CodexThirdPartyCatalogService) {
	if h != nil {
		h.codexThirdParty = svc
	}
}

// SetCodexThirdPartyCatalog 注入第三方模型目录服务（由依赖装配调用）。
// 通用网关 handler 同时负责把「分组默认启用的模型名单」生成函数交给目录服务：
// 名单必须与该分组 Key 调用普通模型列表接口的结果一致，生成逻辑只在本 handler 里有一份。
func (h *GatewayHandler) SetCodexThirdPartyCatalog(svc *service.CodexThirdPartyCatalogService) {
	if h != nil {
		h.codexThirdParty = svc
		svc.SetGroupModelLister(h.codexThirdPartyGroupModelIDs)
	}
}

// codexThirdPartyGroupModelIDs 返回分组默认启用的模型名单，是 Codex 三方模型候选的唯一来源。
//
//   - OpenAI 与 Composite 分组：直接运行普通模型列表接口（GatewayHandler.Models）并取其返回的 id，
//     因此与分组 Key 实际看到的名单逐项一致：只含可调度账号、遵守分组白名单、Composite 按 OpenAI 协议过滤、
//     遵守「隐藏已映射模型」规则；不复制其中任何一段逻辑。
//   - 其它兼容平台分组（DeepSeek、MiniMax 等）：普通列表在没有账号映射时会回落到面向 Claude 的静态清单，
//     对 Codex 没有意义，这类分组沿用 Codex 目录自己的名单（codexModelIDsForGroup）。
//   - 固定账号的 OpenAI 分组同理：它的普通列表要实时请求固定账号的上游，管理页读取不得发上游请求。
//
// 因此生成名单只查数据库，不发任何上游请求。
func (h *GatewayHandler) codexThirdPartyGroupModelIDs(ctx context.Context, group *service.Group) ([]string, error) {
	if h == nil || h.gatewayService == nil || group == nil {
		return nil, errors.New("gateway handler is not ready")
	}
	pinned := group.Platform == service.PlatformOpenAI && group.CodexModelsManifestConfig.Enabled
	if (group.Platform != service.PlatformOpenAI && group.Platform != service.PlatformComposite) || pinned {
		// 固定账号的 OpenAI 分组，普通模型列表是实时向固定账号上游拉取的，会产生网络请求，
		// 页面读取不允许；这类分组与其它兼容平台一样，名单取账号映射与默认清单（只查数据库）。
		return h.codexModelIDsForGroup(ctx, group, ""), nil
	}
	return h.plainModelListIDs(ctx, group)
}

// codexModelListWriter 是只收集响应的内存 http.ResponseWriter，用来把普通模型列表 handler 当函数调用。
type codexModelListWriter struct {
	header http.Header
	status int
	body   bytes.Buffer
}

func (w *codexModelListWriter) Header() http.Header { return w.header }

func (w *codexModelListWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	return w.body.Write(p)
}

func (w *codexModelListWriter) WriteHeader(status int) { w.status = status }

// plainModelListIDs 以该分组 Key 的身份（不带 Codex 的 client_version、不带 Anthropic 特征）运行普通模型列表接口，
// 返回其中的模型 id。非 200 或响应无法解析都按失败返回，调用方据此降级，不得当作空名单。
func (h *GatewayHandler) plainModelListIDs(ctx context.Context, group *service.Group) ([]string, error) {
	writer := &codexModelListWriter{header: http.Header{}}
	c, _ := gin.CreateTestContext(writer)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "/v1/models", nil)
	if err != nil {
		return nil, err
	}
	c.Request = req
	groupID := group.ID
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{GroupID: &groupID, Group: group})
	h.Models(c)
	if writer.status != http.StatusOK {
		return nil, fmt.Errorf("plain model list returned status %d", writer.status)
	}
	var parsed struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(writer.body.Bytes(), &parsed); err != nil {
		return nil, fmt.Errorf("decode plain model list: %w", err)
	}
	ids := make([]string, 0, len(parsed.Data))
	for _, item := range parsed.Data {
		ids = append(ids, item.ID)
	}
	return ids, nil
}

// codexBaseFetchDeadlineKey 是 gin 上下文里保存「本次请求基础目录获取截止时间」的键。
const codexBaseFetchDeadlineKey = "codex_third_party_base_fetch_deadline"

// codexBaseFetchContext 给 OpenAI 基础目录获取加时限：客户端对整次目录请求有 5 秒总超时，
// 超时即清空目录，所以基础目录获取最多占用其中一部分。
// 时限按整次请求计时：同一请求第一次调用确定截止时间并存入 gin 上下文，之后的调用（例如调度循环
// 换号重试）复用同一截止时间；截止时间已过时返回已到期的上下文，不再发起新的拉取。
// 未注入服务时原样返回请求上下文。计时器在请求结束或到期时释放。
func (h *OpenAIGatewayHandler) codexBaseFetchContext(c *gin.Context) context.Context {
	parent := c.Request.Context()
	if h == nil || h.codexThirdParty == nil {
		return parent
	}
	var deadline time.Time
	if stored, ok := c.Get(codexBaseFetchDeadlineKey); ok {
		deadline, _ = stored.(time.Time)
	}
	if deadline.IsZero() {
		deadline = time.Now().Add(service.CodexCatalogBaseFetchBudget)
		c.Set(codexBaseFetchDeadlineKey, deadline)
	}
	ctx, cancel := context.WithDeadline(parent, deadline)
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

// writeCodexCompositeCatalog 处理 Composite 分组的在线目录，返回 true 表示已写出响应。
//
// Composite 分组不进调度循环：调度器可能选中中转 API Key 账号，而中转站的 /models 是几千个各家模型的
// 普通列表，客户端 5 秒预算内转换不完，只会得到 500。这里基础目录只从可调度的官方 ChatGPT 账号拉取
// （共用整次请求的基础目录时限），没有官方账号或拉取失败就按账号映射本地生成，
// 之后与其它分组一样经过第三方策略（writeCodexModelsResponseWithPolicy）写出。
// 服务未注入或不是 Composite 分组时返回 false，保持原有流程。
func (h *OpenAIGatewayHandler) writeCodexCompositeCatalog(c *gin.Context, group *service.Group, clientETag string) bool {
	if h == nil || h.codexThirdParty == nil || group == nil || group.Platform != service.PlatformComposite {
		return false
	}
	clientVersion := c.Query("client_version")
	fetch := func(ctx context.Context, account *service.Account) (*service.OpenAIModelsResponse, error) {
		if h.gatewayService == nil {
			return nil, errors.New("openai gateway service unavailable")
		}
		// 与调度分支的成功路径同序：拉取、补全元数据、应用映射、合并分组配置的模型。
		manifest, err := h.gatewayService.FetchCodexModelsManifest(ctx, account, clientVersion, "", c)
		if err != nil {
			return nil, err
		}
		if err := h.gatewayService.CompleteAPIKeyCodexModelsManifestForClient(manifest, account); err != nil {
			return nil, err
		}
		if err := service.ApplyPinnedCodexModelsMapping(manifest, account, group); err != nil {
			return nil, err
		}
		if err := h.gatewayService.MergeGroupConfiguredCodexModels(ctx, group, manifest, ""); err != nil {
			return nil, err
		}
		return manifest, nil
	}
	manifest, account, err := h.codexThirdParty.BuildCompositeBaseManifest(c.Request.Context(), h.codexBaseFetchContext(c), group, fetch)
	if err != nil {
		if c.Request.Context().Err() != nil {
			return true
		}
		h.errorResponse(c, http.StatusServiceUnavailable, "upstream_error", infraerrors.Message(err))
		return true
	}
	if account != nil {
		setOpsSelectedAccount(c, account.ID, account.Platform)
	}
	h.writeCodexModelsResponseWithPolicy(c, group, manifest, clientETag)
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
