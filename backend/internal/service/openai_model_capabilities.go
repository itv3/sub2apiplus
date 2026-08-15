package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"golang.org/x/sync/singleflight"
)

const (
	openAIModelCapabilityHydrationTimeout = 8 * time.Second
	openAIModelCapabilityFreshTTL         = 5 * time.Minute
	openAIModelCapabilityFailureTTL       = 30 * time.Second
	// openAIModelVisibilityList 对应官方 ModelVisibility::List 的 serde 形态（lowercase）。
	openAIModelVisibilityList = "list"
)

// bundledOpenAIModelCapabilities 是随当前官方客户端画像发布的冷启动快照。
// 账号 manifest 对同名模型拥有更高优先级；缺失模型仍可使用与当前画像同版本的
// bundled 值，避免清单裁剪或暂时故障把已知模型静默当成非 Lite。
var bundledOpenAIModelCapabilities = map[string]openAIModelCapabilities{
	"codex-auto-review": {
		SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.3-codex-spark": {
		SupportsParallelToolCalls: true,
		DefaultReasoningSummary:   "auto", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.4": {
		SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.4-mini": {
		SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.5": {
		SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.6-sol": {
		UseResponsesLite: true, SupportsParallelToolCalls: true, DefaultReasoningLevel: "low",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.6-terra": {
		UseResponsesLite: true, SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
	"gpt-5.6-luna": {
		UseResponsesLite: true, SupportsParallelToolCalls: true, DefaultReasoningLevel: "medium",
		DefaultReasoningSummary: "none", SupportsReasoningSummaryParameter: true, ReasoningDefaultsKnown: true,
	},
}

type openAIModelCapabilities struct {
	UseResponsesLite                  bool
	SupportsParallelToolCalls         bool
	DefaultReasoningLevel             string
	DefaultReasoningSummary           string
	SupportsReasoningSummaryParameter bool
	ReasoningDefaultsKnown            bool
}

type openAIModelCapabilitySnapshot struct {
	mu       sync.RWMutex
	accounts map[int64]openAIModelCapabilityAccountSnapshot
	hydrate  singleflight.Group
}

type openAIModelCapabilityAccountSnapshot struct {
	models     map[string]openAIModelCapabilities
	expiresAt  time.Time
	retryAfter time.Time
	// remoteAuthoritative 表示本次账号清单满足官方 apply_remote_models 的接管条件
	// （清单非空且至少有一个 visibility=list 的模型）。此时远端清单是唯一真相源，
	// 清单未列出的 slug 必须走 fallback 能力位，不再回落 bundled 快照。
	remoteAuthoritative bool
}

// replaceFromManifest 用上游模型清单原子更新账号的 Responses Lite 能力快照。
//
// 合并语义对齐官方 models-manager 的 apply_remote_models：清单非空且至少含一个
// visibility=list 的模型时，远端整体接管，逐条按远端字段构建、不以 bundled 为基底
// （官方此时直接 `*self.remote_models = models` 丢弃内置清单）；不满足接管条件时才
// 退回 bundled 打底再按 slug 覆盖的旧行为。
func (s *openAIModelCapabilitySnapshot) replaceFromManifest(accountID int64, body []byte) {
	if s == nil || accountID <= 0 || len(body) == 0 {
		return
	}
	var envelope struct {
		Models []struct {
			Slug                              string  `json:"slug"`
			UseResponsesLite                  bool    `json:"use_responses_lite"`
			SupportsParallelToolCalls         *bool   `json:"supports_parallel_tool_calls"`
			DefaultReasoningLevel             *string `json:"default_reasoning_level"`
			DefaultReasoningSummary           *string `json:"default_reasoning_summary"`
			SupportsReasoningSummaryParameter *bool   `json:"supports_reasoning_summary_parameter"`
			Visibility                        string  `json:"visibility"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil || len(envelope.Models) == 0 {
		// 上游返回 200 但清单不可解析或为空时，快照不会被写入。这里必须同样进入
		// 失败退避：否则该账号永远处于“未加载”状态，refreshAllowed 恒为真，
		// 每一个未知模型的业务请求都会再同步拉取一次 /models。
		s.markLoadFailure(accountID, time.Now())
		return
	}
	remoteAuthoritative := false
	for _, model := range envelope.Models {
		if strings.EqualFold(strings.TrimSpace(model.Visibility), openAIModelVisibilityList) {
			remoteAuthoritative = true
			break
		}
	}
	capabilities := make(map[string]openAIModelCapabilities, len(envelope.Models))
	for _, model := range envelope.Models {
		slug := strings.ToLower(strings.TrimSpace(model.Slug))
		if slug == "" {
			continue
		}
		var capability openAIModelCapabilities
		if !remoteAuthoritative {
			capability = bundledOpenAIModelCapabilities[slug]
		} else {
			// ModelInfo 的 serde 默认值：summary=auto，且 summary 参数默认受支持。
			capability.DefaultReasoningSummary = "auto"
			capability.SupportsReasoningSummaryParameter = true
		}
		capability.ReasoningDefaultsKnown = true
		capability.UseResponsesLite = model.UseResponsesLite
		if model.SupportsParallelToolCalls != nil {
			capability.SupportsParallelToolCalls = *model.SupportsParallelToolCalls
		}
		if model.DefaultReasoningLevel != nil {
			capability.DefaultReasoningLevel = strings.ToLower(strings.TrimSpace(*model.DefaultReasoningLevel))
		}
		if model.DefaultReasoningSummary != nil {
			capability.DefaultReasoningSummary = strings.ToLower(strings.TrimSpace(*model.DefaultReasoningSummary))
		}
		if model.SupportsReasoningSummaryParameter != nil {
			capability.SupportsReasoningSummaryParameter = *model.SupportsReasoningSummaryParameter
		}
		capabilities[slug] = capability
	}
	if len(capabilities) == 0 {
		s.markLoadFailure(accountID, time.Now())
		return
	}
	s.mu.Lock()
	if s.accounts == nil {
		s.accounts = make(map[int64]openAIModelCapabilityAccountSnapshot)
	}
	s.accounts[accountID] = openAIModelCapabilityAccountSnapshot{
		models:              capabilities,
		expiresAt:           time.Now().Add(openAIModelCapabilityFreshTTL),
		remoteAuthoritative: remoteAuthoritative,
	}
	s.mu.Unlock()
}

func (s *openAIModelCapabilitySnapshot) responsesLite(accountID int64, model string) (bool, bool) {
	value, known, _ := s.responsesLiteState(accountID, model, time.Now())
	return value, known
}

// responsesLiteState 返回当前有效能力和是否应刷新。失败退避期内不会继续同步
// 请求 `/models`，从而避免上游故障时每个业务请求都放大一次辅助请求。
func (s *openAIModelCapabilitySnapshot) responsesLiteState(
	accountID int64,
	model string,
	now time.Time,
) (value bool, known bool, shouldRefresh bool) {
	capabilities, known, shouldRefresh := s.modelCapabilitiesState(accountID, model, now)
	return capabilities.UseResponsesLite, known, shouldRefresh
}

// modelCapabilitiesState 返回当前模型影响官方请求形态的完整能力集合。
// 账号 manifest 对 bundled 快照中的同名模型拥有更高优先级。
func (s *openAIModelCapabilitySnapshot) modelCapabilitiesState(
	accountID int64,
	model string,
	now time.Time,
) (value openAIModelCapabilities, known bool, shouldRefresh bool) {
	if s == nil || accountID <= 0 {
		return openAIModelCapabilities{}, false, false
	}
	model = strings.ToLower(strings.TrimSpace(model))
	if model == "" {
		return openAIModelCapabilities{}, false, false
	}
	s.mu.RLock()
	accountSnapshot, accountLoaded := s.accounts[accountID]
	value, exists := accountSnapshot.models[model]
	remoteAuthoritative := accountSnapshot.remoteAuthoritative && len(accountSnapshot.models) > 0
	s.mu.RUnlock()
	if !exists {
		if remoteAuthoritative {
			// 账号清单已按官方条件整体接管：清单未列出的 slug 等价于官方 fallback ModelInfo，
			// 两个能力位都是 false，且属于“已知不支持”而非“未知”。这里必须返回 known=true，
			// 否则调用方会把它当成未加载而反复同步拉取 /models，并让 bundled 旧值继续生效。
			value = openAIModelCapabilities{
				DefaultReasoningSummary:           "auto",
				SupportsReasoningSummaryParameter: true,
				ReasoningDefaultsKnown:            true,
			}
			exists = true
		} else {
			value, exists = bundledOpenAIModelCapabilities[model]
		}
	}
	refreshAllowed := !accountLoaded || !now.Before(accountSnapshot.expiresAt)
	if accountLoaded && now.Before(accountSnapshot.retryAfter) {
		refreshAllowed = false
	}
	return value, exists, refreshAllowed
}

func (s *openAIModelCapabilitySnapshot) modelCapabilities(
	accountID int64,
	model string,
) (openAIModelCapabilities, bool) {
	value, known, _ := s.modelCapabilitiesState(accountID, model, time.Now())
	return value, known
}

func (s *openAIModelCapabilitySnapshot) markLoadFailure(accountID int64, now time.Time) {
	if s == nil || accountID <= 0 {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.accounts == nil {
		s.accounts = make(map[int64]openAIModelCapabilityAccountSnapshot)
	}
	snapshot := s.accounts[accountID]
	snapshot.retryAfter = now.Add(openAIModelCapabilityFailureTTL)
	// 没有可用账号清单时也保留 bundled 能力；已有清单则继续作为 stale 值。
	if snapshot.expiresAt.IsZero() {
		snapshot.expiresAt = now
	}
	s.accounts[accountID] = snapshot
}

// resolveOpenAIResponsesLiteCapability 只接受模型清单中的能力位。
// 入站 Header 与 client_metadata 都可能由第三方伪造，不能反向决定模型画像。
func (s *OpenAIGatewayService) resolveOpenAIResponsesLiteCapability(account *Account, body []byte) bool {
	return s.resolveOpenAIModelCapabilities(account, body).UseResponsesLite
}

func (s *OpenAIGatewayService) resolveOpenAIModelCapabilities(
	account *Account,
	body []byte,
) openAIModelCapabilities {
	if s == nil || account == nil || !account.IsOpenAIOAuth() {
		return openAIModelCapabilities{}
	}
	model := openAIModelCapabilityKey(account, body)
	value, known := s.openaiModelCapabilities.modelCapabilities(account.ID, model)
	if !known {
		// 官方对未知 slug 使用 model_info_from_slug：effort=None、summary=auto，
		// 且 summary 参数受支持。拉取清单失败时也只能采用这一公开 fallback，
		// 不能发出缺失 reasoning 结构体的非官方形态。
		return openAIModelCapabilities{
			DefaultReasoningSummary:           "auto",
			SupportsReasoningSummaryParameter: true,
			ReasoningDefaultsKnown:            true,
		}
	}
	return value
}

// openAIModelCapabilityKey 先应用账号映射，再将 reasoning 后缀等客户端别名
// 归一到 models manifest 使用的真实模型 slug。
func openAIModelCapabilityKey(account *Account, body []byte) string {
	if account == nil {
		return ""
	}
	model := account.GetMappedModel(strings.TrimSpace(gjson.GetBytes(body, "model").String()))
	if strings.TrimSpace(model) == "" {
		return ""
	}
	canonical := canonicalizeOpenAIModelAliasSpelling(model)
	if canonical == "" {
		canonical = model
	}
	for _, suffix := range []string{"-none", "-minimal", "-low", "-medium", "-high", "-xhigh", "-max"} {
		if strings.HasSuffix(canonical, suffix) {
			canonical = strings.TrimSuffix(canonical, suffix)
			break
		}
	}
	// 能力查表键必须与实际出站 model 落到同一个 manifest slug。否则入站按别名
	// 查不到能力、当作非 Lite 把工具不可逆摊平，出站 model 已被改写成真实 slug
	// 又按 Lite 重新包装，最终产出官方客户端不会发送的自相矛盾形态。
	//
	// 这里只接受确定映射到已知 slug 的别名：normalizeOpenAIModelForUpstream 对
	// 陌生模型有默认兜底，用它会让未知模型冒名命中别人的能力位。
	if slug := strings.TrimSpace(normalizeKnownOpenAICodexModel(canonical)); slug != "" {
		return slug
	}
	return canonical
}

// ensureOpenAIModelCapability 在官方画像使用前刷新账号 models manifest。
// 已知 bundled 或 stale 能力立即服务并异步刷新；未知模型允许等待一次单飞加载。
// 加载失败进入有界退避并保持原请求语义，不把辅助 `/models` 故障升级为业务失败。
func (s *OpenAIGatewayService) ensureOpenAIModelCapability(
	ctx context.Context,
	account *Account,
	body []byte,
) error {
	if s == nil || account == nil || !account.IsOpenAIOAuth() {
		return nil
	}
	model := openAIModelCapabilityKey(account, body)
	if model == "" {
		return fmt.Errorf("OpenAI 官方出站画像缺少模型名称")
	}
	_, known, shouldRefresh := s.openaiModelCapabilities.responsesLiteState(
		account.ID,
		model,
		time.Now(),
	)
	if !shouldRefresh {
		return nil
	}

	key := fmt.Sprintf("%d", account.ID)
	load := func() <-chan singleflight.Result {
		return s.openaiModelCapabilities.hydrate.DoChan(key, func() (any, error) {
			loadCtx, cancel := context.WithTimeout(context.Background(), openAIModelCapabilityHydrationTimeout)
			defer cancel()
			_, err := s.fetchCodexModelsManifest(
				loadCtx,
				account,
				resolveVerifiedCodexClientVersion(),
				"",
				false,
			)
			if err != nil {
				s.openaiModelCapabilities.markLoadFailure(account.ID, time.Now())
			}
			return nil, err
		})
	}
	if known {
		// bundled 或 stale 值已经足以安全定型当前请求，刷新不占用业务延迟。
		go func() {
			result := <-load()
			if result.Err != nil {
				logger.LegacyPrintf(
					"service.openai_model_capabilities",
					"refresh failed: account_id=%d err=%v",
					account.ID,
					result.Err,
				)
			}
		}()
		return nil
	}

	loadResult := load()
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-ctx.Done():
		// 调用方取消不终止 singleflight 的后台加载，也不改变请求的既有语义。
		return nil
	case result := <-loadResult:
		if result.Err != nil {
			logger.LegacyPrintf(
				"service.openai_model_capabilities",
				"cold load failed; preserve request semantics: account_id=%d model=%s err=%v",
				account.ID,
				model,
				result.Err,
			)
			return nil
		}
	}
	return nil
}

type openAIModelCapabilitiesContextKey struct{}

func withOpenAIModelCapabilities(
	ctx context.Context,
	capabilities openAIModelCapabilities,
) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, openAIModelCapabilitiesContextKey{}, capabilities)
}

// withOpenAIResponsesLiteCapability 仅供聚焦 Lite Header 的既有测试构造上下文。
func withOpenAIResponsesLiteCapability(ctx context.Context, enabled bool) context.Context {
	return withOpenAIModelCapabilities(ctx, openAIModelCapabilities{UseResponsesLite: enabled})
}

func openAIResponsesLiteCapabilityFromContext(ctx context.Context) bool {
	return openAIModelCapabilitiesFromContext(ctx).UseResponsesLite
}

func openAIModelCapabilitiesFromContext(ctx context.Context) openAIModelCapabilities {
	if ctx == nil {
		return openAIModelCapabilities{}
	}
	capabilities, _ := ctx.Value(openAIModelCapabilitiesContextKey{}).(openAIModelCapabilities)
	return capabilities
}

func (s *OpenAIGatewayService) bindOpenAIResponsesLiteCapability(
	ctx context.Context,
	account *Account,
	body []byte,
) context.Context {
	return withOpenAIModelCapabilities(
		ctx,
		s.resolveOpenAIModelCapabilities(account, body),
	)
}

// normalizeOpenAIResponsesLiteIngressHeader 覆盖客户端自报的 Lite Header，
// 使权限、工具归一化和最终画像始终使用同一份模型能力判定。
func (s *OpenAIGatewayService) normalizeOpenAIResponsesLiteIngressHeader(
	c *gin.Context,
	account *Account,
	body []byte,
) bool {
	if account == nil || !account.IsOpenAIOAuth() {
		if c == nil || c.Request == nil {
			return false
		}
		return isOpenAIResponsesLiteHeader(c.GetHeader(responsesLiteHeader))
	}
	enabled := s.resolveOpenAIResponsesLiteCapability(account, body)
	if c == nil || c.Request == nil {
		return enabled
	}
	c.Request.Header.Del(responsesLiteHeader)
	if enabled {
		c.Request.Header.Set(responsesLiteHeader, "true")
	}
	return enabled
}
