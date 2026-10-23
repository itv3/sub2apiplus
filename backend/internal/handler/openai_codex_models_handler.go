package handler

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// CodexModels 为 Codex 客户端提供模型清单。
//
// Codex CLI 和 Codex 桌面应用通过 GET {base_url}/models?client_version=...
// （自定义供应商模式）或 GET /backend-api/codex/models（chatgpt_base_url 模式）
// 刷新模型选择器，这两条路由都会进入此处。模型清单会从选定账号的 ChatGPT 后端
// 原样代理；自定义 API Key 清单仅执行客户端兼容性规范化。API Key 清单使用
// 短期异步重新校验缓存，以容忍客户端取消请求。
func (h *OpenAIGatewayHandler) CodexModels(c *gin.Context) {
	if c.Request.Context().Err() != nil {
		return
	}
	apiKey, ok := middleware2.GetAPIKeyFromContext(c)
	if !ok || apiKey.Group == nil {
		h.errorResponse(c, http.StatusUnauthorized, "invalid_request_error", "API key group is required")
		return
	}
	if apiKey.Group.Platform != service.PlatformOpenAI &&
		apiKey.Group.Platform != service.PlatformComposite {
		h.errorResponse(c, http.StatusNotFound, "not_found_error", "Codex models manifest is only available for OpenAI and Composite groups")
		return
	}

	// 客户端 ETag 必须在完成分组本地过滤、映射和元数据补全后再比较。
	// 不能把它传入按账号共享的上游缓存，否则同一账号在白名单切换后会
	// 提前得到 304，导致客户端继续使用旧的未过滤目录。
	clientETag := c.GetHeader("If-None-Match")
	// 本功能接点：Composite 分组的基础目录只从官方 ChatGPT 账号拉取，拉不到则按账号映射本地生成，不进入调度循环。
	if h.writeCodexCompositeCatalog(c, apiKey.Group, clientETag) {
		return
	}
	// 固定账号分支：开启后只用选定账号拉取 manifest，不经过调度器；
	// 全部不可用/全部失败时按 FallbackToScheduler 决定回退调度器或返回错误。
	if apiKey.Group.Platform == service.PlatformOpenAI &&
		apiKey.Group.CodexModelsManifestConfig.Enabled {
		pinnedManifest, pinnedAccount, pinnedErr := h.gatewayService.FetchPinnedCodexModelsManifest(
			h.codexBaseFetchContext(c), // 本功能接点：基础目录获取限时
			apiKey.Group,
			c.Query("client_version"),
		)
		if pinnedErr != nil {
			if c.Request.Context().Err() != nil {
				return
			}
			if !apiKey.Group.CodexModelsManifestConfig.FallbackToScheduler {
				if errors.Is(pinnedErr, service.ErrNoPinnedCodexModelsAccounts) {
					h.errorResponse(c, http.StatusServiceUnavailable, "upstream_error", "No available pinned OpenAI accounts")
					return
				}
				h.errorResponse(c, infraerrors.Code(pinnedErr), "upstream_error", infraerrors.Message(pinnedErr))
				return
			}
			// 回退开启：跌入下方调度器循环。
		} else {
			// 让 ops 错误日志携带实际拉取成功的首个固定账号。
			setOpsSelectedAccount(c, pinnedAccount.ID, pinnedAccount.Platform)
			if err := h.gatewayService.MergeGroupConfiguredCodexModels(c.Request.Context(), apiKey.Group, pinnedManifest, ""); err != nil {
				h.errorResponse(c, http.StatusInternalServerError, "api_error", "Failed to build Codex models manifest")
				return
			}
			if c.Request.Context().Err() != nil {
				return
			}
			h.writeCodexModelsResponseWithPolicy(c, apiKey.Group, pinnedManifest, clientETag) // 本功能接点：先应用三方模型策略
			return
		}
	}

	if !apiKey.Group.CodexModelsManifestConfig.Enabled {
		configuredManifest, configured, err := h.gatewayService.BuildGroupConfiguredCodexModelsManifest(
			c.Request.Context(),
			apiKey.Group,
			"",
		)
		if err != nil {
			if c.Request.Context().Err() != nil {
				return
			}
			h.errorResponse(c, http.StatusInternalServerError, "api_error", "Failed to build Codex models manifest")
			return
		}
		if configured {
			h.writeCodexModelsResponseWithPolicy(c, apiKey.Group, configuredManifest, clientETag) // 本功能接点
			return
		}
	}

	maxAccountSwitches := h.maxAccountSwitches
	if maxAccountSwitches <= 0 {
		maxAccountSwitches = 3
	}
	failedAccountIDs := make(map[int64]struct{})
	switchCount := 0
	var lastUpstreamErr error

	for {
		account, err := h.gatewayService.SelectAccountForModelWithExclusions(c.Request.Context(), apiKey.GroupID, "", "", failedAccountIDs)
		if err != nil {
			if c.Request.Context().Err() != nil {
				return
			}
			if lastUpstreamErr != nil {
				h.errorResponse(c, infraerrors.Code(lastUpstreamErr), "upstream_error", infraerrors.Message(lastUpstreamErr))
				return
			}
			h.errorResponse(c, http.StatusServiceUnavailable, "upstream_error", "No available OpenAI accounts")
			return
		}
		// 让 ops 错误日志携带实际选中的上游账号，便于定位失效账号（#4544）。
		setOpsSelectedAccount(c, account.ID, account.Platform)

		manifest, err := h.gatewayService.FetchCodexModelsManifest(h.codexBaseFetchContext(c), account, c.Query("client_version"), "", c) // 本功能接点：基础目录获取限时
		if err != nil {
			if c.Request.Context().Err() != nil {
				return
			}
			if service.IsRetryableCodexModelsManifestError(err) && switchCount < maxAccountSwitches {
				failedAccountIDs[account.ID] = struct{}{}
				switchCount++
				lastUpstreamErr = err
				continue
			}
			h.errorResponse(c, infraerrors.Code(err), "upstream_error", infraerrors.Message(err))
			return
		}
		if err := h.gatewayService.CompleteAPIKeyCodexModelsManifestForClient(manifest, account); err != nil {
			h.errorResponse(c, http.StatusInternalServerError, "api_error", "Failed to complete Codex models manifest")
			return
		}
		if err := service.ApplyPinnedCodexModelsMapping(manifest, account, apiKey.Group); err != nil {
			h.errorResponse(c, http.StatusInternalServerError, "api_error", "Failed to apply model mappings")
			return
		}
		if err := h.gatewayService.MergeGroupConfiguredCodexModels(c.Request.Context(), apiKey.Group, manifest, ""); err != nil {
			h.errorResponse(c, http.StatusInternalServerError, "api_error", "Failed to build Codex models manifest")
			return
		}
		if c.Request.Context().Err() != nil {
			return
		}

		h.writeCodexModelsResponseWithPolicy(c, apiKey.Group, manifest, clientETag) // 本功能接点
		return
	}
}

// writeCodexModelsResponseForClient 在所有本地目录变换完成后计算并比较 ETag。
// ETag 绑定最终响应体，而不是共享上游缓存体或未过滤的上游响应。
func writeCodexModelsResponseForClient(c *gin.Context, manifest *service.OpenAIModelsResponse, clientETag string) {
	if manifest == nil {
		c.Status(http.StatusInternalServerError)
		return
	}
	if len(manifest.Body) > 0 {
		manifest.ETag = service.CodexModelsManifestETag(manifest.Body)
	}
	if service.CodexModelsManifestETagMatches(clientETag, manifest.ETag) {
		if manifest.ETag != "" {
			c.Header("ETag", manifest.ETag)
		}
		c.Status(http.StatusNotModified)
		c.Writer.WriteHeaderNow()
		return
	}
	writeOpenAIModelsResponse(c, manifest)
}
