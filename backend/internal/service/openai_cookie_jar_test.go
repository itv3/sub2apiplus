package service

import (
	"net/http"
	"net/url"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestChatGPTCloudflareCookieJarFiltersCookiesAndHosts(t *testing.T) {
	service := &OpenAIGatewayService{}
	account := &Account{ID: 94, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	jar := service.openAICookieJar(account)
	require.NotNil(t, jar)

	chatGPTURL, err := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	require.NoError(t, err)
	jar.SetCookies(chatGPTURL, []*http.Cookie{
		{Name: "_cfuvid", Value: "visitor", Secure: true},
		{Name: "codex_session", Value: "secret", Secure: true},
		{Name: "cf_chl_rc_i", Value: "challenge", Secure: true},
	})

	cookies := jar.Cookies(chatGPTURL)
	require.Len(t, cookies, 2)
	require.ElementsMatch(t, []string{"_cfuvid", "cf_chl_rc_i"}, []string{cookies[0].Name, cookies[1].Name})

	apiURL, err := url.Parse("https://api.openai.com/v1/responses")
	require.NoError(t, err)
	require.Empty(t, jar.Cookies(apiURL))

	httpURL, err := url.Parse("http://chatgpt.com/backend-api/codex/responses")
	require.NoError(t, err)
	require.Empty(t, jar.Cookies(httpURL))
}

func TestOpenAICookieJarIsolatedByProxy(t *testing.T) {
	service := &OpenAIGatewayService{}
	firstProxyID := int64(11)
	secondProxyID := int64(12)
	account := &Account{ID: 94, Platform: PlatformOpenAI, Type: AccountTypeOAuth, ProxyID: &firstProxyID}
	first := service.openAICookieJar(account)
	account.ProxyID = &secondProxyID
	second := service.openAICookieJar(account)
	require.NotSame(t, first, second)
}
