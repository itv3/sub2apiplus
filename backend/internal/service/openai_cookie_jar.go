package service

import (
	"context"
	"fmt"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
)

type chatGPTCloudflareCookieJar struct {
	jar http.CookieJar
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
	filtered := make([]*http.Cookie, 0, len(cookies))
	for _, cookie := range cookies {
		if cookie != nil && isAllowedCloudflareCookieName(cookie.Name) {
			filtered = append(filtered, cookie)
		}
	}
	if len(filtered) > 0 {
		j.jar.SetCookies(target, filtered)
	}
}

func (j *chatGPTCloudflareCookieJar) Cookies(target *url.URL) []*http.Cookie {
	if j == nil || j.jar == nil || !isAllowedChatGPTCookieURL(target) {
		return nil
	}
	cookies := j.jar.Cookies(target)
	filtered := make([]*http.Cookie, 0, len(cookies))
	for _, cookie := range cookies {
		if cookie != nil && isAllowedCloudflareCookieName(cookie.Name) {
			filtered = append(filtered, cookie)
		}
	}
	return filtered
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

func isAllowedCloudflareCookieName(name string) bool {
	switch name {
	case "__cf_bm", "__cflb", "__cfruid", "__cfseq", "__cfwaitingroom",
		"_cfuvid", "cf_clearance", "cf_ob_info", "cf_use_ob":
		return true
	default:
		return strings.HasPrefix(name, "cf_chl_")
	}
}
