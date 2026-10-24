package service

// codex_third_party_routes.go 解析一个公开模型名在分组内的真实路由，并区分「原生 OpenAI GPT」与「第三方」。
//
// 路径与真实请求一致：公开名 → Composite 目标（仅 Composite 分组）→ 渠道映射 → 账号映射。
// 只读复用现有解析函数，不复制健康、sticky 或故障转移算法：候选账号取持久启用的账号，
// 暂时限流或 quota 受限的账号也参与能力检查，不会因瞬时不可调度而放宽能力交集。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/domain"
)

// codexTPAccountRoute 是一个承接账号对某个公开名的最终路由结果。
type codexTPAccountRoute struct {
	Account  *Account
	Final    string
	Protocol string
	// NativeCapable 表示该账号对此模型的承接目标确实是原生 OpenAI GPT。
	NativeCapable bool
}

// codexTPRoute 是一个公开名在分组内的路由分析结果。
type codexTPRoute struct {
	Slug           string
	TargetPlatform string
	// RouteModel 是 Composite 路由改写后的模型名（非 Composite 分组等于 Slug）。
	RouteModel string
	// MappedModel 是渠道映射后的模型名。
	MappedModel string
	Accounts    []codexTPAccountRoute

	// Native 为 true 表示全部承接账号都确认指向原生 OpenAI GPT 目标。
	Native bool
	// Conflict 表示归属冲突（多平台声明、原生与第三方账号同名并存等），停止发布。
	Conflict bool
	// Unavailable 表示没有可确认的路由或承接账号。
	Unavailable bool
	Reasons     []string

	// 以下字段仅第三方有意义。
	// UpstreamModel 是展示用的最终上游型号：只有一个时就是它，有多个时按字典序用逗号连接。
	UpstreamModel string
	// UpstreamModels 是各承接账号的最终上游型号（去重、按字典序），能力档案按它逐个匹配。
	UpstreamModels []string
	Protocols      []string
	AccountIDs     []int64
	EndpointDigest string

	// Primary 与 Alternates 仅在 Composite 分组按账号级跨平台候选池承接时有值：
	// Primary 是首选账号（决定 TargetPlatform），Alternates 是同一承接分组里的其余账号。
	Primary    *CodexThirdPartyRouteAccount
	Alternates []CodexThirdPartyRouteAccount
}

// codexTPSession 缓存一次目录编排里的账号列表与路由分析结果。
type codexTPSession struct {
	svc      *CodexThirdPartyCatalogService
	group    *Group
	accounts []Account
	memo     map[string]*codexTPRoute

	// 跨平台承接分组用的全平台账号缓存（见 allPlatformAccounts）：只在遇到跨平台歧义时才加载。
	allLoaded   bool
	allAccounts []*Account
	allErr      error

	// 分组默认模型名单的会话内缓存（见 groupModelNames）。
	listingLoaded bool
	listingNames  []string
	listingSet    map[string]struct{}
	listingErr    error
}

func (s *CodexThirdPartyCatalogService) newSession(ctx context.Context, group *Group) (*codexTPSession, error) {
	accounts, err := s.loadGroupAccounts(ctx, group)
	if err != nil {
		return nil, err
	}
	return &codexTPSession{svc: s, group: group, accounts: accounts, memo: map[string]*codexTPRoute{}}, nil
}

// loadGroupAccounts 读取分组内持久启用的、经 OpenAI 网关转发的账号。
// 查询失败时返回错误，调用方据此降级，不得当作「没有账号」继续。
func (s *CodexThirdPartyCatalogService) loadGroupAccounts(ctx context.Context, group *Group) ([]Account, error) {
	if s == nil || s.accountRepo == nil || group == nil {
		return nil, fmt.Errorf("codex third party: account repository unavailable")
	}
	groupID := group.ID
	platforms := domain.PlatformIDsWhere(func(spec domain.PlatformSpec) bool {
		return spec.Gateway == domain.PlatformGatewayOpenAI
	})
	accounts, err := s.accountRepo.ListModelAvailabilityCandidates(ctx, &groupID, platforms, false)
	if err != nil {
		return nil, fmt.Errorf("list group accounts: %w", err)
	}
	return accounts, nil
}

// hasThirdPartySource 判断分组内是否存在可能承接第三方模型的账号。
func (sess *codexTPSession) hasThirdPartySource() bool {
	for i := range sess.accounts {
		account := &sess.accounts[i]
		if account.Platform != PlatformOpenAI {
			return true
		}
		if account.IsOpenAIOAuthLike() || isOfficialOpenAICodexAccount(account) {
			continue
		}
		return true
	}
	return false
}

// officialOnlyOpenAIGroup 判断目录策略是否可以作为空操作：分组平台为 OpenAI 或 Composite，
// 且分组内账号全部是官方 OpenAI 账号（ChatGPT 订阅或官方 API 地址，含账号为空的 OpenAI 分组）。
// 此时目录不可能出现第三方条目，原有目录逐字节不变。
// 其它平台的分组（DeepSeek、MiniMax 等）即使没有账号也不能走捷径：通用目录 handler 会用
// 平台回落清单生成条目，必须经过统一过滤，保证第三方默认关闭。
func (sess *codexTPSession) officialOnlyOpenAIGroup() bool {
	if sess.group.Platform != PlatformOpenAI && sess.group.Platform != PlatformComposite {
		return false
	}
	return !sess.hasThirdPartySource()
}

// route 返回公开名的路由分析，结果在一次编排内缓存。返回 error 表示路由查询失败（不是「没有路由」）。
func (sess *codexTPSession) route(ctx context.Context, slug string) (*codexTPRoute, error) {
	slug = strings.TrimSpace(slug)
	if cached, ok := sess.memo[slug]; ok {
		return cached, nil
	}
	route, err := sess.analyze(ctx, slug)
	if err != nil {
		return nil, err
	}
	sess.memo[slug] = route
	return route, nil
}

func (sess *codexTPSession) analyze(ctx context.Context, slug string) (*codexTPRoute, error) {
	route := &codexTPRoute{Slug: slug, RouteModel: slug}
	group := sess.group

	// 第一步：目标平台。Composite 走真实解析器，其它分组固定为分组平台。
	// receivers 非空表示按账号级跨平台候选池承接：同一模型名被多个平台的账号声明时，
	// 真实请求由候选池在这些账号里选号，目录必须对其中全部账号成立。
	var receivers []*Account
	if group.Platform == PlatformComposite {
		resolver := sess.svc.resolver
		if resolver == nil {
			resolver = NewCompositeRouteResolver(nil)
		}
		decision, err := resolver.Resolve(ctx, group.ID, slug, CompositeRouteEndpointResponses)
		if err != nil {
			return nil, fmt.Errorf("resolve composite route for %s: %w", slug, err)
		}
		ambiguous := decision.OAuthAmbiguous() || decision.Reason == CompositeRouteReasonAmbiguous
		if ambiguous {
			// 旧归属判定遇到多平台同时声明只会报歧义；真实请求走账号级候选池，这里复用同一套规则。
			var perr error
			if receivers, perr = sess.compositeReceivers(ctx, slug); perr != nil {
				return nil, fmt.Errorf("resolve composite receivers for %s: %w", slug, perr)
			}
		}
		switch {
		case ambiguous && len(receivers) == 0:
			route.Conflict = true
			route.Reasons = append(route.Reasons, "该模型名在多个平台的账号上同时声明，需先配置显式 Composite 路由")
			return route, nil
		case ambiguous:
			route.TargetPlatform = receivers[0].Platform
		case !decision.Matched:
			route.Unavailable = true
			route.Reasons = append(route.Reasons, "Composite 路由无法确认目标平台："+codexTPTrimReason(decision.Reason))
			return route, nil
		default:
			route.TargetPlatform = decision.TargetPlatform
			if upstream := strings.TrimSpace(decision.UpstreamModel); upstream != "" {
				route.RouteModel = upstream
			}
		}
	} else {
		route.TargetPlatform = group.Platform
	}
	if len(receivers) > 0 {
		// 承接集合里只要有一个账号不经 Responses 网关，整组就无法提供 Codex 目录。
		for _, account := range receivers {
			if !domain.UsesOpenAIGateway(account.Platform) {
				route.Unavailable = true
				route.Reasons = append(route.Reasons, fmt.Sprintf("目标平台 %s 不经 Responses 网关，无法提供 Codex 目录", account.Platform))
				return route, nil
			}
		}
	} else if !domain.UsesOpenAIGateway(route.TargetPlatform) {
		route.Unavailable = true
		route.Reasons = append(route.Reasons, fmt.Sprintf("目标平台 %s 不经 Responses 网关，无法提供 Codex 目录", route.TargetPlatform))
		return route, nil
	}

	// 第二步：渠道映射。查询失败必须报错，不能当作「没有渠道改写」。
	// 渠道映射按账号所在平台分别查（跨平台承接时各账号平台不同）；MappedModel 记录首选账号平台的结果。
	route.MappedModel = route.RouteModel
	mappedByPlatform := map[string]string{}
	channelMapped := func(platform string) (string, error) {
		if mapped, ok := mappedByPlatform[platform]; ok {
			return mapped, nil
		}
		mapped := route.RouteModel
		if sess.svc.channels != nil {
			lookupCtx := WithResolvedTargetPlatform(ctx, platform)
			lk, err := sess.svc.channels.lookupGroupChannel(lookupCtx, group.ID)
			if err != nil {
				return "", fmt.Errorf("lookup channel mapping for %s: %w", slug, err)
			}
			if lk != nil {
				if rewritten := strings.TrimSpace(resolveMapping(lk, group.ID, route.RouteModel).MappedModel); rewritten != "" {
					mapped = rewritten
				}
			}
		}
		mappedByPlatform[platform] = mapped
		return mapped, nil
	}
	var err error
	if route.MappedModel, err = channelMapped(route.TargetPlatform); err != nil {
		return nil, err
	}

	// 第三步：账号映射与协议。承接账号按各自平台的渠道映射结果逐个计算最终上游型号。
	type receiverAccount struct {
		account *Account
		mapped  string
	}
	var takers []receiverAccount
	if len(receivers) > 0 {
		for _, account := range receivers {
			mapped, merr := channelMapped(account.Platform)
			if merr != nil {
				return nil, merr
			}
			takers = append(takers, receiverAccount{account: account, mapped: mapped})
		}
	} else {
		for i := range sess.accounts {
			takers = append(takers, receiverAccount{account: &sess.accounts[i], mapped: route.MappedModel})
		}
	}
	for _, taker := range takers {
		account, mappedModel := taker.account, taker.mapped
		if len(receivers) == 0 && account.Platform != route.TargetPlatform {
			continue
		}
		if !account.IsModelSupported(mappedModel) {
			continue
		}
		final := strings.TrimSpace(ResolveOpenAIAccountUpstreamModelForRequest(account, mappedModel, false))
		if final == "" {
			continue
		}
		// 官方 OpenAI 账号（ChatGPT 订阅、官方 API 地址）只可能承接官方 GPT/Codex 目录范围内的模型，
		// 或管理员显式映射过的名称；对未知别名，空映射账号虽被调度过滤放行，但上游并不能真正服务，
		// 不能把它算作该别名的承接账号（否则同组第三方站点上的别名会被误判为归属冲突）。
		if account.IsOpenAIOAuthLike() || isOfficialOpenAICodexAccount(account) {
			if _, mapped := account.ResolveMappedModel(mappedModel); !mapped && !isOfficialOpenAICodexCatalogModel(final) && !sess.upstreamListsModel(account, final) {
				continue
			}
		}
		route.Accounts = append(route.Accounts, codexTPAccountRoute{
			Account:       account,
			Final:         final,
			Protocol:      sess.svc.accountProtocol(account, slug),
			NativeCapable: codexTPNativeCapable(account, final),
		})
	}
	if len(receivers) > 0 && len(route.Accounts) > 0 {
		// 跨平台承接：以实际进入集合的首个账号（排序后的首选）为准写出目标平台、首选与备选。
		route.TargetPlatform = route.Accounts[0].Account.Platform
		route.Primary = codexTPRouteAccountRef(route.Accounts[0].Account)
		for _, item := range route.Accounts[1:] {
			route.Alternates = append(route.Alternates, *codexTPRouteAccountRef(item.Account))
		}
	}
	if len(route.Accounts) == 0 {
		route.Unavailable = true
		route.Reasons = append(route.Reasons, "分组内没有账号承接该模型")
		return route, nil
	}

	native, third := 0, 0
	finals := map[string]struct{}{}
	protocols := map[string]struct{}{}
	for _, item := range route.Accounts {
		if item.NativeCapable {
			native++
		} else {
			third++
		}
		route.AccountIDs = append(route.AccountIDs, item.Account.ID)
		finals[item.Final] = struct{}{}
		if item.Protocol != "" {
			protocols[item.Protocol] = struct{}{}
		}
	}
	sort.Slice(route.AccountIDs, func(i, j int) bool { return route.AccountIDs[i] < route.AccountIDs[j] })
	switch {
	case native > 0 && third > 0:
		route.Conflict = true
		route.Reasons = append(route.Reasons, "同名模型同时被原生 OpenAI GPT 与第三方账号承接，归属冲突")
		return route, nil
	case third == 0:
		route.Native = true
		return route, nil
	}

	// 第三方：收集最终上游型号、协议与端点身份。多个账号映射到不同的原始上游名是允许的，
	// 能否套用单一能力档案由发布判定按「去厂商前缀、忽略大小写」后的结果决定。
	for final := range finals {
		route.UpstreamModels = append(route.UpstreamModels, final)
	}
	sort.Strings(route.UpstreamModels)
	route.UpstreamModel = strings.Join(route.UpstreamModels, ", ")
	for protocol := range protocols {
		route.Protocols = append(route.Protocols, protocol)
	}
	sort.Strings(route.Protocols)
	missingProtocol := false
	for _, item := range route.Accounts {
		if item.Protocol == "" {
			missingProtocol = true
		}
	}
	if missingProtocol {
		route.Protocols = nil
		route.Reasons = append(route.Reasons, "部分承接账号按模型分流上游协议，无法静态确认 Responses 通路")
	}
	route.EndpointDigest = codexTPEndpointDigest(route.Accounts)
	return route, nil
}

// codexTPRouteAccountRef 生成路由结构里的账号展示信息（不含凭据）。
func codexTPRouteAccountRef(account *Account) *CodexThirdPartyRouteAccount {
	return &CodexThirdPartyRouteAccount{ID: account.ID, Name: account.Name, Platform: account.Platform}
}

// allPlatformAccounts 读取分组内持久启用的全部具体平台账号（含不经 OpenAI 网关的平台），
// 与 Composite 账号归属解析读取同一批账号；结果在一次编排内缓存，仅跨平台歧义时才会触发。
func (sess *codexTPSession) allPlatformAccounts(ctx context.Context) ([]*Account, error) {
	if sess.allLoaded {
		return sess.allAccounts, sess.allErr
	}
	sess.allLoaded = true
	repo := sess.svc.accountRepo
	if repo == nil || sess.group == nil {
		sess.allErr = fmt.Errorf("codex third party: account repository unavailable")
		return nil, sess.allErr
	}
	groupID := sess.group.ID
	accounts, err := repo.ListModelAvailabilityCandidates(ctx, &groupID, domain.ConcretePlatformIDs(), false)
	if err != nil {
		sess.allErr = fmt.Errorf("list group accounts of all platforms: %w", err)
		return nil, sess.allErr
	}
	sess.allAccounts = make([]*Account, 0, len(accounts))
	for i := range accounts {
		sess.allAccounts = append(sess.allAccounts, &accounts[i])
	}
	return sess.allAccounts, nil
}

// compositeReceivers 按账号级候选池的同一套规则（声明强弱分层、零转换优先、Priority 与账号 ID 排序）
// 求 Responses 请求可能落到的全部账号，首项是首选账号。只看配置态，不看冷却、限流等瞬态。
func (sess *codexTPSession) compositeReceivers(ctx context.Context, slug string) ([]*Account, error) {
	accounts, err := sess.allPlatformAccounts(ctx)
	if err != nil {
		return nil, err
	}
	set := compositeConfigReceivers(accounts, slug, func(account *Account) bool {
		return sess.svc.responsesZeroConversion(account, slug)
	})
	return set.Accounts, nil
}

// routingModelAndCatalog 返回账号参与协议分流的模型名，以及按模型分流账号在上游模型目录缓存里
// 该模型支持的协议（只读缓存，页面读取不发上游请求；没有缓存、不是按模型分流账号或 Grok 时 catalog 为空）。
// 零转换判定与协议判定共用这一份输入，两者永远基于同一份缓存目录。
func (s *CodexThirdPartyCatalogService) routingModelAndCatalog(account *Account, slug string) (routingModel string, catalog []string) {
	if account == nil || !domain.UsesOpenAIGateway(account.Platform) || account.IsGrok() {
		return "", nil
	}
	body, _ := json.Marshal(map[string]string{"model": slug})
	routingModel = upstreamRoutingModel(account, body, "")
	if lister, ok := s.fetcher.(codexThirdPartyProtocolCataloger); ok {
		catalog = lister.CachedModelProtocols(account, routingModel)
	}
	return routingModel, catalog
}

// responsesZeroConversion 判定账号对 Responses 入站是否零转换：规则与候选池同一个函数，
// 按模型分流的账号带上游模型目录的缓存结果。
func (s *CodexThirdPartyCatalogService) responsesZeroConversion(account *Account, slug string) bool {
	routingModel, catalog := s.routingModelAndCatalog(account, slug)
	return compositeAccountZeroConversion(account, APIProtocolResponses, routingModel, catalog)
}

// accountProtocol 判定账号对 Responses 请求的实际通路，无法静态确认时返回空串。
// 普通账号看账号自身配置（codexTPAccountProtocol）；按模型分流的账号要看上游模型目录：
// 目录缓存里该模型支持 Responses 就是原生通路，只支持 Chat Completions 就是转换通路；
// 目录没有缓存、该模型不在目录里或目录给出的是其它协议时维持「无法静态确认」。
func (s *CodexThirdPartyCatalogService) accountProtocol(account *Account, slug string) string {
	if account == nil || !account.routesByModel() {
		return codexTPAccountProtocol(account)
	}
	routingModel, catalog := s.routingModelAndCatalog(account, slug)
	if len(catalog) == 0 {
		return ""
	}
	switch resolveUpstreamProtocol(account, APIProtocolResponses, routingModel, catalog) {
	case APIProtocolResponses:
		return CodexThirdPartyProtocolNative
	case APIProtocolChatCompletions:
		return CodexThirdPartyProtocolViaChat
	}
	return ""
}

// upstreamListsModel 判断官方账号最近一次拉到的上游目录是否列出了该型号（正向证据）。
// 上游以后出现名称不在官方前缀范围内的原生模型时，靠它保留；快照缺失、过期或未列出时返回 false，
// 此时仍由名称范围规则兜底。
func (sess *codexTPSession) upstreamListsModel(account *Account, model string) bool {
	lister, ok := sess.svc.fetcher.(codexThirdPartyUpstreamLister)
	return ok && lister.UpstreamListsModel(account, model)
}

// codexTPAccountProtocol 判定账号对 Responses 请求的实际通路；无法静态确认时返回空串。
func codexTPAccountProtocol(account *Account) string {
	if account == nil {
		return ""
	}
	if account.routesByModel() {
		return ""
	}
	if shouldForwardOpenAIResponsesViaRawChatCompletions(account) {
		return CodexThirdPartyProtocolViaChat
	}
	return CodexThirdPartyProtocolNative
}

// codexTPNativeCapable 判断账号对最终上游型号 final 的承接是否属于原生 OpenAI GPT：
//   - 必须是 openai 平台账号；
//   - 最终型号不能是其它厂商的模型（按现有平台识别函数判断，例如映射到 kimi-k3、deepseek-*）；
//   - ChatGPT 订阅账号与官方 API 地址的账号，其目录条目都来自 OpenAI 自身，视为原生；
//   - 自定义地址的 API Key 账号，只有最终型号本身属于官方 GPT/Codex 目录范围才视为原生。
func codexTPNativeCapable(account *Account, final string) bool {
	if account == nil || account.Platform != PlatformOpenAI {
		return false
	}
	if platform, ok := DetectModelPlatform(final); ok && platform != PlatformOpenAI {
		return false
	}
	if account.IsOpenAIOAuthLike() || isOfficialOpenAICodexAccount(account) {
		return true
	}
	return isOfficialOpenAICodexCatalogModel(final)
}

// codexTPEndpointDigest 计算承接账号的端点身份摘要：平台、类型、上游地址、协议与接入模式。
// 不含任何凭据；换站点（地址变化）摘要必然变化，旧的「已验证」状态随之失效。
func codexTPEndpointDigest(accounts []codexTPAccountRoute) string {
	parts := make([]string, 0, len(accounts))
	for _, item := range accounts {
		account := item.Account
		parts = append(parts, strings.Join([]string{
			fmt.Sprint(account.ID),
			account.Platform,
			account.Type,
			strings.TrimRight(strings.ToLower(strings.TrimSpace(account.GetOpenAIBaseURL())), "/"),
			strings.TrimSpace(account.GetCredential("api_protocol")),
			strings.TrimSpace(account.GetCredential("account_mode")),
			item.Final,
			item.Protocol,
		}, "|"))
	}
	sort.Strings(parts)
	sum := sha256.Sum256([]byte(strings.Join(parts, "\n")))
	return hex.EncodeToString(sum[:])
}

func codexTPTrimReason(reason string) string {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return "无显式路由且内置识别无匹配"
	}
	return reason
}
