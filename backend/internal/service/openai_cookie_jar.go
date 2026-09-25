package service

import (
	"context"
	"fmt"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

type chatGPTCloudflareCookieJar struct {
	jar http.CookieJar
}

// officialCodexCookiePolicy 是共享 Cookie jar 的名单策略：精确名 + 前缀。
//
// 画像 CookieJar 节存在时取节中的 AllowedNames／AllowedPrefixes；节缺席时取旧名单
// （九个 Cloudflare Cookie 名 + cf_chl_ 前缀），旧版本画像的出站 Cookie 与改动前一致。
type officialCodexCookiePolicy struct {
	names    map[string]struct{}
	prefixes []string
}

// legacyOfficialCodexCookieNames 是画像没有 CookieJar 节时沿用的旧名单。
var legacyOfficialCodexCookieNames = []string{
	"__cf_bm", "__cflb", "__cfruid", "__cfseq", "__cfwaitingroom",
	"_cfuvid", "cf_clearance", "cf_ob_info", "cf_use_ob",
}

var legacyOfficialCodexCookiePrefixes = []string{"cf_chl_"}

func newOfficialCodexCookiePolicy(names []string, prefixes []string) officialCodexCookiePolicy {
	policy := officialCodexCookiePolicy{names: make(map[string]struct{}, len(names))}
	for _, name := range names {
		policy.names[name] = struct{}{}
	}
	policy.prefixes = append([]string(nil), prefixes...)
	return policy
}

func legacyOfficialCodexCookiePolicy() officialCodexCookiePolicy {
	return newOfficialCodexCookiePolicy(legacyOfficialCodexCookieNames, legacyOfficialCodexCookiePrefixes)
}

// officialCodexCookiePolicyFromSection 把画像 CookieJar 节投影为名单策略；nil 取旧名单。
func officialCodexCookiePolicyFromSection(section *profilecontract.CookieJarSection) officialCodexCookiePolicy {
	if section == nil {
		return legacyOfficialCodexCookiePolicy()
	}
	return newOfficialCodexCookiePolicy(section.AllowedNames, section.AllowedPrefixes)
}

// officialCodexCookiePolicyForMode 返回 release mode 对应画像的读取名单。
func officialCodexCookiePolicyForMode(mode string) officialCodexCookiePolicy {
	return officialCodexCookiePolicyFromSection(officialCodexOptionalSectionsForMode(mode).CookieJar)
}

// officialCodexCookieStoragePolicy 是 jar 的写入名单：旧名单与 Active、Previous 两份画像
// CookieJar 节的并集。写入放宽到并集、读取按调用冻结的画像过滤，使 Cloudflare 状态在
// 主备画像之间共享，同时回退到旧画像时不会把只有新画像才允许的 Cookie（如 __oailb）
// 带出去。两份画像都没有该节时与旧名单完全相同。
func officialCodexCookieStoragePolicy() officialCodexCookiePolicy {
	names := append([]string(nil), legacyOfficialCodexCookieNames...)
	prefixes := append([]string(nil), legacyOfficialCodexCookiePrefixes...)
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		if section := officialCodexOptionalSectionsForMode(mode).CookieJar; section != nil {
			names = append(names, section.AllowedNames...)
			prefixes = append(prefixes, section.AllowedPrefixes...)
		}
	}
	return newOfficialCodexCookiePolicy(names, prefixes)
}

func (p officialCodexCookiePolicy) allows(name string) bool {
	if _, ok := p.names[name]; ok {
		return true
	}
	for _, prefix := range p.prefixes {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

// openAICookieJar 返回按账号和代理隔离的 Cloudflare Cookie jar。代理切换会得到
// 新实例，避免把与旧出口 IP 绑定的 Cloudflare 状态带到新出口。
func (s *OpenAIGatewayService) openAICookieJar(account *Account) http.CookieJar {
	if s == nil || account == nil || !account.IsOpenAIOAuth() || account.ID <= 0 {
		return nil
	}
	proxyID := int64(0)
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	key := fmt.Sprintf("%d:%d", account.ID, proxyID)
	if existing, ok := s.openaiCookieJars.Load(key); ok {
		jar, _ := existing.(http.CookieJar)
		return jar
	}
	baseJar, err := cookiejar.New(nil)
	if err != nil {
		return nil
	}
	jar := &chatGPTCloudflareCookieJar{jar: baseJar}
	actual, _ := s.openaiCookieJars.LoadOrStore(key, jar)
	resolved, _ := actual.(http.CookieJar)
	return resolved
}

func (s *OpenAIGatewayService) bindOpenAICookieJar(ctx context.Context, account *Account) context.Context {
	return WithHTTPUpstreamCookieJar(ctx, s.openAICookieJar(account))
}

func (j *chatGPTCloudflareCookieJar) SetCookies(target *url.URL, cookies []*http.Cookie) {
	if j == nil || j.jar == nil || !isAllowedChatGPTCookieURL(target) {
		return
	}
	policy := officialCodexCookieStoragePolicy()
	filtered := make([]*http.Cookie, 0, len(cookies))
	for _, cookie := range cookies {
		if cookie != nil && policy.allows(cookie.Name) {
			filtered = append(filtered, cookie)
		}
	}
	if len(filtered) > 0 {
		j.jar.SetCookies(target, filtered)
	}
}

// Cookies 供 net/http 自动补 Cookie 的非官方出站路径使用，始终按旧名单过滤。
// 官方出站在签名前经 CookiesForPolicy 按调用冻结的画像名单固化 Cookie。
func (j *chatGPTCloudflareCookieJar) Cookies(target *url.URL) []*http.Cookie {
	return j.CookiesForPolicy(target, legacyOfficialCodexCookiePolicy())
}

// CookiesForPolicy 按给定名单读取 Cookie。
func (j *chatGPTCloudflareCookieJar) CookiesForPolicy(
	target *url.URL,
	policy officialCodexCookiePolicy,
) []*http.Cookie {
	if j == nil || j.jar == nil || !isAllowedChatGPTCookieURL(target) {
		return nil
	}
	cookies := j.jar.Cookies(target)
	filtered := make([]*http.Cookie, 0, len(cookies))
	for _, cookie := range cookies {
		if cookie != nil && policy.allows(cookie.Name) {
			filtered = append(filtered, cookie)
		}
	}
	return filtered
}

// officialCodexPolicyCookieJar 是支持按画像名单读取的 jar。
type officialCodexPolicyCookieJar interface {
	CookiesForPolicy(target *url.URL, policy officialCodexCookiePolicy) []*http.Cookie
}

// officialCodexCookieJarURL 把 WebSocket 握手 URL 映射为 Cookie jar 查询用的 HTTP(S) URL：
// Cookie 按 host／path 作用于同一站点，wss 与 https 共享同一组 Cookie。
func officialCodexCookieJarURL(target *url.URL) *url.URL {
	if target == nil {
		return nil
	}
	mapped := *target
	switch strings.ToLower(mapped.Scheme) {
	case "wss":
		mapped.Scheme = "https"
	case "ws":
		mapped.Scheme = "http"
	}
	return &mapped
}

// officialCodexCookiePolicyForRequest 按请求上下文冻结的 release mode 选择读取名单；
// 没有冻结 mode 的请求使用旧名单。
func officialCodexCookiePolicyForRequest(ctx context.Context) officialCodexCookiePolicy {
	if egressContext, ok := OfficialEgressContextFromContext(ctx); ok && egressContext != nil {
		if mode := strings.TrimSpace(egressContext.ProfileMode()); mode != "" {
			return officialCodexCookiePolicyForMode(mode)
		}
	}
	if state, bound, err := officialCodexRuntimeStateFromContext(ctx); err == nil && bound {
		if mode := strings.TrimSpace(state.ProfileMode); mode != "" {
			return officialCodexCookiePolicyForMode(mode)
		}
	}
	return legacyOfficialCodexCookiePolicy()
}

type officialCodexWebSocketCookieWriteBackContextKey struct{}

// withOfficialCodexWebSocketCookieWriteBack 声明本次 WS 握手（101 与被拒绝的升级）的
// Set-Cookie 需要写回同一 jar。只在画像 CookieJar 节声明 WebSocketHandshakeWriteBack 时绑定。
func withOfficialCodexWebSocketCookieWriteBack(ctx context.Context, jar http.CookieJar) context.Context {
	if ctx == nil || jar == nil {
		return ctx
	}
	return context.WithValue(ctx, officialCodexWebSocketCookieWriteBackContextKey{}, jar)
}

// writeBackOfficialCodexWebSocketHandshakeCookies 把握手响应的 Set-Cookie 写回 jar；
// 上下文没有声明写回时为空操作。
func writeBackOfficialCodexWebSocketHandshakeCookies(ctx context.Context, target string, headers http.Header) {
	if ctx == nil || len(headers.Values("Set-Cookie")) == 0 {
		return
	}
	jar, ok := ctx.Value(officialCodexWebSocketCookieWriteBackContextKey{}).(http.CookieJar)
	if !ok || jar == nil {
		return
	}
	parsed, err := url.Parse(strings.TrimSpace(target))
	if err != nil {
		return
	}
	cookies := (&http.Response{Header: headers}).Cookies()
	if len(cookies) == 0 {
		return
	}
	jar.SetCookies(officialCodexCookieJarURL(parsed), cookies)
}

// bindOfficialCodexWebSocketCookieJar 按画像决定 WS 握手是否使用共享 Cookie jar：
// Responses WS 端点声明 cookie 槽位时，握手在签名前从 jar 固化 Cookie；CookieJar 节声明
// 握手写回时，101 与被拒绝升级的 Set-Cookie 写回同一 jar。两者都没有时（旧版本画像）
// 上下文原样返回，WS 握手不读写 jar，与改动前一致。
func (s *OpenAIGatewayService) bindOfficialCodexWebSocketCookieJar(
	ctx context.Context,
	account *Account,
	mode string,
) context.Context {
	if s == nil || account == nil || !account.IsOpenAIOAuth() {
		return ctx
	}
	profile, err := officialCodexExecutableProfileForMode(mode)
	if err != nil {
		return ctx
	}
	readCookies := false
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID != officialCodexEndpointResponsesWS {
			continue
		}
		for _, slot := range endpoint.Headers {
			if strings.EqualFold(strings.TrimSpace(slot.Name), "cookie") {
				readCookies = true
			}
		}
	}
	section := profile.Optional().CookieJar
	writeBack := section != nil && section.WebSocketHandshakeWriteBack
	if !readCookies && !writeBack {
		return ctx
	}
	jar := s.openAICookieJar(account)
	if jar == nil {
		return ctx
	}
	ctx = WithHTTPUpstreamCookieJar(ctx, jar)
	if writeBack {
		ctx = withOfficialCodexWebSocketCookieWriteBack(ctx, jar)
	}
	return ctx
}

func isAllowedChatGPTCookieURL(target *url.URL) bool {
	if target == nil || !strings.EqualFold(target.Scheme, "https") {
		return false
	}
	host := strings.ToLower(strings.TrimSuffix(target.Hostname(), "."))
	return host == "chatgpt.com" ||
		host == "chat.openai.com" ||
		host == "chatgpt-staging.com" ||
		strings.HasSuffix(host, ".chatgpt.com") ||
		strings.HasSuffix(host, ".chatgpt-staging.com")
}

// 编译期断言：官方 jar 支持按画像名单读取。
var _ officialCodexPolicyCookieJar = (*chatGPTCloudflareCookieJar)(nil)

// officialCodexWebSocketReleaseMode 返回 WS 握手所用的 release mode（与
// attachOfficialEgressWebSocketContext 的选择一致）。
func officialCodexWebSocketReleaseMode(s *OpenAIGatewayService) string {
	if s != nil && s.cfg != nil {
		return officialClientProfileModeFromConfig(s.cfg)
	}
	return officialClientProfileModeActive
}
