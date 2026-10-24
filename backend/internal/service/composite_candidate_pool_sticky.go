package service

// 候选池对会话粘性与 Responses 续链归属的只读查询。
//
// 粘性有两套互不相通的契约，候选池不能只按请求路径选一套，而要按"候选账号所属的网关族"
// 分别计算会话哈希、分别查缓存键：
//   - OpenAI 网关族（openai、grok 与各多协议 API Key 供应商）：会话哈希由
//     OpenAIGatewayService.GenerateSessionHash 计算，缓存键带 "openai:" 前缀，
//     并可能带旧哈希兜底读取；Messages 入口还会叠加 Claude Code 会话头与 metadata.user_id 的派生；
//   - Anthropic 网关族（anthropic、typesafe、gemini、antigravity）：会话哈希由
//     GatewayService.GenerateSessionHash 在预解析请求上计算，缓存键为裸哈希；
//     Gemini 账号在 Messages 与 Chat Completions 入口会再加 "gemini:" 前缀，
//     Responses 与 count_tokens 入口不加，Antigravity 任何入口都不加。
//
// 写入仍由各网关照旧执行，候选池不写粘性、不写续链绑定。

import (
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

// 粘性查询的变体：同一变体内所有账号共用同一个会话哈希与缓存键。
const (
	compositeStickyVariantOpenAI    = "openai"
	compositeStickyVariantGrok      = "openai_grok"
	compositeStickyVariantAnthropic = "anthropic"
	compositeStickyVariantGemini    = "anthropic_gemini_prefixed"
)

// stickyVariant 返回账号所属的粘性查询变体。
func (r *compositeCandidateRun) stickyVariant(account *Account) string {
	if domain.UsesOpenAIGateway(account.Platform) {
		// Grok 的粘性种子按模型加了命名空间，与其它 OpenAI 网关族平台的哈希不同。
		if account.IsGrok() {
			return compositeStickyVariantGrok
		}
		return compositeStickyVariantOpenAI
	}
	if account.Platform == PlatformGemini &&
		(r.endpoint == CompositeRouteEndpointMessages || r.endpoint == CompositeRouteEndpointChatCompletions) {
		return compositeStickyVariantGemini
	}
	return compositeStickyVariantAnthropic
}

// boundAccountID 返回账号所在粘性变体下，当前会话绑定的账号 ID（没有绑定或无法计算时为 0）。
// 同一变体只查一次。
func (r *compositeCandidateRun) boundAccountID(account *Account) int64 {
	variant := r.stickyVariant(account)
	if id, ok := r.bound[variant]; ok {
		return id
	}
	var id int64
	switch variant {
	case compositeStickyVariantOpenAI, compositeStickyVariantGrok:
		id = r.lookupOpenAIFamilyBinding(account.Platform)
	default:
		id = r.lookupAnthropicFamilyBinding(variant)
	}
	r.bound[variant] = id
	return id
}

// stickyCandidate 在声明账号里找出"当前会话绑定且仍合格"的账号。
// 绑定账号按粘性语义重新评估资格（Anthropic 族里窗口费用、RPM 的"仅限粘性"状态对它放行）。
// 两套网关族可能各自绑定了一个账号，此时按候选排序取最优的一个。
func (r *compositeCandidateRun) stickyCandidate(candidates []*compositeCandidate) *compositeCandidate {
	var best *compositeCandidate
	for _, candidate := range candidates {
		if r.boundAccountID(candidate.account) != candidate.account.ID {
			continue
		}
		if !r.evaluate(candidate.account, true).eligible {
			continue
		}
		candidate.zeroConversion = r.isZeroConversion(candidate.account)
		if best == nil || compositeCandidateLess(candidate, best) {
			best = candidate
		}
	}
	return best
}

// continuationOwner 返回 Responses 续链归属的账号。成立条件：
//  1. 入站是 Responses 且请求带 previous_response_id；
//  2. 响应归属表里查得到该响应 ID 的账号；
//  3. 该账号是 OpenAI 平台账号（国产与自定义平台的 Responses 一律无状态，不存在可续的链），
//     仍属于本分组且对该模型有声明（任意强度：续链已落在它身上，不能因声明强度层级变化而断链）；
//  4. 该账号当前通过资格过滤（基于实时读取的账号）。
//
// 条件不满足时返回 nil，请求按普通请求处理（与现有调度器"续链账号不可用则落回普通调度"一致）。
func (r *compositeCandidateRun) continuationOwner(groupCfg *compositeCandidateGroupConfig) *Account {
	previousResponseID := r.previousResponseID()
	if previousResponseID == "" || r.pool.openAI == nil {
		return nil
	}
	store := r.pool.openAI.getOpenAIWSStateStore()
	if store == nil {
		return nil
	}
	ownerID, err := store.GetResponseAccount(r.ctx, r.groupID, previousResponseID)
	if err != nil || ownerID <= 0 {
		return nil
	}
	cfg := groupCfg.byID[ownerID]
	if cfg == nil || !cfg.account.IsOpenAI() || cfg.strength(r.model) == CompositeClaimNone {
		return nil
	}
	account := r.freshAccount(cfg)
	if _, dropped := r.dropped[account.ID]; dropped || !r.evaluate(account, false).eligible {
		return nil
	}
	return account
}

// lookupOpenAIFamilyBinding 按 OpenAI 网关族的契约计算会话哈希并查询绑定。
func (r *compositeCandidateRun) lookupOpenAIFamilyBinding(platform string) int64 {
	svc := r.pool.openAI
	if svc == nil {
		return 0
	}
	c := r.syntheticGinContext(platform)
	hash := r.openAIFamilySessionHash(svc, c)
	if hash == "" {
		return 0
	}
	// 用 GenerateSessionHash 回挂了旧哈希的请求上下文查询，旧哈希兜底读取才会生效。
	id, err := svc.getStickySessionAccountID(c.Request.Context(), &r.groupID, hash)
	if err != nil || id <= 0 {
		return 0
	}
	return id
}

// openAIFamilySessionHash 复刻 OpenAI 网关各文本入口计算会话哈希的方式。
// Messages 入口在 GenerateSessionHash 之上还有 Claude Code 会话头与 metadata.user_id 的派生，
// 对应处理器里的 resolveOpenAIMessagesMetadataSession（处理器包内私有，无法直接调用，
// 这里按同一规则复刻；处理器若调整该规则，需同步修改此处）。
func (r *compositeCandidateRun) openAIFamilySessionHash(svc *OpenAIGatewayService, c *gin.Context) string {
	body := r.entry.Body
	sessionHash := svc.GenerateSessionHash(c, body)
	if r.endpoint != CompositeRouteEndpointMessages {
		return sessionHash
	}
	if promptCacheKey := svc.ExtractSessionID(c, body); promptCacheKey == "" {
		if claudeSessionID := ClaudeCodeSessionIDFromHeader(c); claudeSessionID != "" {
			return DeriveSessionHashFromSeed(claudeSessionID)
		}
	}
	if sessionHash != "" {
		return sessionHash
	}
	if userID := strings.TrimSpace(gjson.GetBytes(body, "metadata.user_id").String()); userID != "" {
		return DeriveSessionHashFromSeed(r.model + "-" + userID)
	}
	return ""
}

// syntheticGinContext 构造一个只带请求头和请求上下文的最小 gin 上下文，
// 供 OpenAI 网关的会话哈希函数读取请求头。请求上下文里写入候选账号的平台，
// 这样 Grok 的粘性种子按真实调度时的方式派生。
func (r *compositeCandidateRun) syntheticGinContext(platform string) *gin.Context {
	header := r.entry.Header
	if header == nil {
		header = http.Header{}
	}
	req := (&http.Request{Method: http.MethodPost, URL: &url.URL{}, Header: header}).
		WithContext(WithResolvedTargetPlatform(r.ctx, platform))
	return &gin.Context{Request: req}
}

// lookupAnthropicFamilyBinding 按 Anthropic 网关族的契约计算会话哈希并查询绑定。
func (r *compositeCandidateRun) lookupAnthropicFamilyBinding(variant string) int64 {
	gw := r.pool.gateway
	if gw == nil {
		return 0
	}
	parsed := r.parsedGatewayRequest()
	if parsed == nil {
		return 0
	}
	hash := gw.GenerateSessionHash(parsed)
	if hash == "" {
		return 0
	}
	key := hash
	if variant == compositeStickyVariantGemini {
		key = "gemini:" + hash
	}
	id, err := gw.GetCachedSessionAccountID(r.ctx, &r.groupID, key)
	if err != nil || id <= 0 {
		return 0
	}
	return id
}

// parsedGatewayRequest 复刻 Anthropic 网关各入口的请求预解析与 SessionContext 装配。
// 一次解析内只做一次。Messages 与 count_tokens 解析失败时处理器会直接 400，这里返回 nil
// 表示放弃粘性查询；Responses 与 Chat Completions 入口解析失败时处理器退回一个只带请求体的
// 空解析结果，这里同样处理。
func (r *compositeCandidateRun) parsedGatewayRequest() *ParsedRequest {
	if r.parsedReq != nil || r.parsedErr {
		return r.parsedReq
	}
	protocol := domain.PlatformAnthropic
	switch r.endpoint {
	case CompositeRouteEndpointResponses:
		protocol = "responses"
	case CompositeRouteEndpointChatCompletions:
		protocol = "chat_completions"
	}
	ref := NewRequestBodyRef(r.entry.Body)
	parsed, _ := ParseGatewayRequest(ref, protocol)
	if parsed == nil {
		if protocol == domain.PlatformAnthropic {
			r.parsedErr = true
			return nil
		}
		parsed = &ParsedRequest{Model: r.model, Body: ref}
	}
	userAgent := r.entry.UserAgent
	if userAgent == "" {
		userAgent = r.entry.Header.Get("User-Agent")
	}
	parsed.SessionContext = &SessionContext{
		ClientIP:  r.entry.ClientIP,
		UserAgent: userAgent,
		APIKeyID:  r.entry.APIKeyID,
	}
	r.parsedReq = parsed
	return parsed
}
