package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

// Cookie jar 按画像名单读写与 WS 握手回写（VC-4 第 5 项）：
//   - 画像 CookieJar 节缺席时沿用旧名单，jar 的写入、读取与 WS 握手行为与改动前一致；
//   - 节存在时 jar 写入放宽到“旧名单 ∪ 主备画像名单”，官方出站在签名前按调用冻结的画像
//     名单读取（__oailb 只在声明它的画像下出站）；
//   - WS 握手：画像声明 cookie 槽位时绑定 jar 并在签名前固化 Cookie，声明写回时
//     101 与被拒绝升级的 Set-Cookie 写回同一 jar。

func cookieJarTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.CookieJar = syntheticServiceRawSection(t, profilecontract.CookieJarSection{
			AllowedNames: []string{
				"__cf_bm", "__cflb", "__cfruid", "__cfseq", "__cfwaitingroom", "__oailb",
				"_cfuvid", "cf_clearance", "cf_ob_info", "cf_use_ob",
			},
			AllowedPrefixes:             []string{"cf_chl_"},
			WebSocketHandshakeWriteBack: true,
		})
		endpoint := syntheticServiceSnapshotEndpoint(t, doc, officialCodexEndpointResponsesWS)
		endpoint.Headers = append(endpoint.Headers, profilecontract.SnapshotHeaderSlot{
			Slot: 195, Name: "cookie", WireName: "cookie",
			Source: string(profilecontract.SourceSession), Condition: string(profilecontract.ConditionCookiePresent),
		})
		endpoint.HeaderMapInsertionOrder = append(endpoint.HeaderMapInsertionOrder, "cookie")
	}
}

func cookieNames(cookies []*http.Cookie) []string {
	names := make([]string, 0, len(cookies))
	for _, cookie := range cookies {
		names = append(names, cookie.Name)
	}
	return names
}

func TestOfficialCodexCookiePolicyFollowsProfileSection(t *testing.T) {
	legacy := officialCodexCookiePolicyForMode(officialClientProfileModeActive)
	require.True(t, legacy.allows("__cf_bm"))
	require.True(t, legacy.allows("cf_chl_rc_i"))
	require.False(t, legacy.allows("__oailb"), "旧画像不允许 __oailb")

	withOfficialCodexSyntheticProfile(t, cookieJarTargetMutation(t))
	target := officialCodexCookiePolicyForMode(officialClientProfileModeActive)
	require.True(t, target.allows("__oailb"))
	require.True(t, target.allows("cf_chl_rc_i"))
	require.False(t, target.allows("codex_session"))
}

// cookieAllowedBySection 是测试侧独立实现的画像名单判定：有 CookieJar 节时按节的精确名与
// 前缀，无节时按旧名单。期望值由它从 ReleaseCatalog 的画像事实推导，不调用被测的写入／
// 读取名单函数。
func cookieAllowedBySection(section *profilecontract.CookieJarSection, name string) bool {
	if section == nil {
		return legacyOfficialCodexCookiePolicy().allows(name)
	}
	if slices.Contains(section.AllowedNames, name) {
		return true
	}
	for _, prefix := range section.AllowedPrefixes {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

// TestChatGPTCookieJarStoresUnionAndReadsByPolicy 证明 jar 写入放宽到“旧名单 ∪ 主备画像
// 名单”、读取按调用冻结的画像名单过滤。
//
// 正式目录部分改动前写死“主备画像都没有 CookieJar 节”；RuntimeCatalog 切换后 previous
// 槽位的目标画像声明了该节（含 __oailb），这一前提不再成立。现逐槽位按画像事实推导期望：
// 有 CookieJar 节的槽位按节的名单／前缀，无节的槽位按旧名单，jar 实际保存的集合是旧名单与
// 两个槽位名单之并。两个槽位都无节时，期望与改动前的写死值完全一致。
func TestChatGPTCookieJarStoresUnionAndReadsByPolicy(t *testing.T) {
	chatGPTURL, err := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	require.NoError(t, err)
	incoming := []*http.Cookie{
		{Name: "_cfuvid", Value: "visitor", Secure: true},
		{Name: "__oailb", Value: "lb", Secure: true},
		{Name: "codex_session", Value: "secret", Secure: true},
	}
	incomingNames := cookieNames(incoming)

	slots := []struct {
		mode    string
		release officialegress.ReleaseMode
	}{
		{mode: officialClientProfileModeActive, release: officialegress.ReleaseModeActive},
		{mode: officialClientProfileModePrevious, release: officialegress.ReleaseModePrevious},
	}
	// 各槽位画像的 CookieJar 节（nil 表示该槽位画像没有该节）。
	sections := make(map[string]*profilecontract.CookieJarSection, len(slots))
	for _, slot := range slots {
		release, resolveErr := officialegress.DefaultReleaseCatalog().Resolve(slot.release)
		require.NoError(t, resolveErr)
		sections[slot.mode] = release.ExecutableProfile().Optional().CookieJar
	}
	// jar 应保存的来信 Cookie：旧名单或任一槽位名单允许。
	stored := make([]string, 0, len(incomingNames))
	for _, name := range incomingNames {
		allowed := cookieAllowedBySection(nil, name)
		for _, slot := range slots {
			allowed = allowed || cookieAllowedBySection(sections[slot.mode], name)
		}
		if allowed {
			stored = append(stored, name)
		}
	}
	require.NotContains(t, stored, "codex_session", "不在任何名单里的 Cookie 不得进入 jar")

	legacyJar := (&OpenAIGatewayService{}).openAICookieJar(&Account{ID: 157, Platform: PlatformOpenAI, Type: AccountTypeOAuth})
	legacyJar.SetCookies(chatGPTURL, incoming)
	require.ElementsMatch(t, []string{"_cfuvid"}, cookieNames(legacyJar.Cookies(chatGPTURL)),
		"net/http 自动补 Cookie 的路径始终按旧名单读取，与画像是否声明 CookieJar 节无关")
	policyJar, ok := legacyJar.(officialCodexPolicyCookieJar)
	require.True(t, ok)
	// 用覆盖全部来信 Cookie 的名单读取，读出的就是 jar 实际保存的集合：写入名单恰为旧名单与
	// 两个槽位名单之并，未进入 jar 的 Cookie（codex_session，以及无节时的 __oailb）读不出。
	require.ElementsMatch(t, stored, cookieNames(policyJar.CookiesForPolicy(
		chatGPTURL, newOfficialCodexCookiePolicy(incomingNames, nil),
	)), "jar 写入名单必须是旧名单与主备画像名单之并，未进入 jar 的 Cookie 不会被任何名单读出")
	for _, slot := range slots {
		want := make([]string, 0, len(stored))
		for _, name := range stored {
			if cookieAllowedBySection(sections[slot.mode], name) {
				want = append(want, name)
			}
		}
		require.ElementsMatch(t, want, cookieNames(policyJar.CookiesForPolicy(
			chatGPTURL, officialCodexCookiePolicyForMode(slot.mode),
		)), "%s 槽位按该槽位画像名单读取（有 CookieJar 节按节，无节按旧名单）", slot.mode)
	}

	withOfficialCodexSyntheticProfile(t, cookieJarTargetMutation(t))
	jar := (&OpenAIGatewayService{}).openAICookieJar(&Account{ID: 158, Platform: PlatformOpenAI, Type: AccountTypeOAuth})
	jar.SetCookies(chatGPTURL, incoming)
	require.ElementsMatch(t, []string{"_cfuvid"}, cookieNames(jar.Cookies(chatGPTURL)),
		"net/http 自动补 Cookie 的路径始终按旧名单读取")
	targetJar, ok := jar.(officialCodexPolicyCookieJar)
	require.True(t, ok, "账号 Cookie jar 必须支持按名单读取")
	require.ElementsMatch(t, []string{"_cfuvid", "__oailb"}, cookieNames(targetJar.CookiesForPolicy(
		chatGPTURL, officialCodexCookiePolicyForMode(officialClientProfileModeActive),
	)))
	require.ElementsMatch(t, []string{"_cfuvid"}, cookieNames(targetJar.CookiesForPolicy(
		chatGPTURL, legacyOfficialCodexCookiePolicy(),
	)), "回退到旧画像时不会把 __oailb 带出去")
}

func TestMaterializeOfficialCodexCookieJarFollowsRequestProfile(t *testing.T) {
	materialize := func(t *testing.T, rawURL string, jar http.CookieJar) string {
		t.Helper()
		req := httptest.NewRequest(http.MethodGet, rawURL, nil)
		egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
			AccountID: 158, TargetPlatform: PlatformOpenAI, ProfileMode: officialClientProfileModeActive,
			ProfileVersion: officialCodexVersion0145, Transport: OfficialEgressTransportHTTP,
			UpstreamHost: "chatgpt.com",
		})
		ctx := WithOfficialEgressContext(WithHTTPUpstreamCookieJar(req.Context(), jar), egressContext)
		req = req.WithContext(ctx)
		materializeOfficialCodexCookieJar(req)
		return req.Header.Get("Cookie")
	}
	seed, err := url.Parse("https://chatgpt.com/")
	require.NoError(t, err)

	legacyJar := (&OpenAIGatewayService{}).openAICookieJar(&Account{ID: 160, Platform: PlatformOpenAI, Type: AccountTypeOAuth})
	legacyJar.SetCookies(seed, []*http.Cookie{{Name: "__cf_bm", Value: "bm", Path: "/"}, {Name: "__oailb", Value: "lb", Path: "/"}})
	require.Equal(t, "__cf_bm=bm", materialize(t, "https://chatgpt.com/backend-api/codex/responses", legacyJar))

	withOfficialCodexSyntheticProfile(t, cookieJarTargetMutation(t))
	jar := (&OpenAIGatewayService{}).openAICookieJar(&Account{ID: 161, Platform: PlatformOpenAI, Type: AccountTypeOAuth})
	jar.SetCookies(seed, []*http.Cookie{{Name: "__cf_bm", Value: "bm", Path: "/"}, {Name: "__oailb", Value: "lb", Path: "/"}})
	cookie := materialize(t, "https://chatgpt.com/backend-api/codex/responses", jar)
	require.Contains(t, cookie, "__cf_bm=bm")
	require.Contains(t, cookie, "__oailb=lb")
	wsCookie := materialize(t, "wss://chatgpt.com/backend-api/codex/responses", jar)
	require.Equal(t, cookie, wsCookie, "WS 握手按同站点 HTTPS URL 读取 jar")
}

func TestBindOfficialCodexWebSocketCookieJarGatedByProfile(t *testing.T) {
	service := &OpenAIGatewayService{}
	account := &Account{ID: 162, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	base := context.Background()

	legacy := service.bindOfficialCodexWebSocketCookieJar(base, account, officialClientProfileModeActive)
	require.Nil(t, HTTPUpstreamCookieJarFromContext(legacy), "旧画像 WS 握手不读 jar")
	require.Nil(t, legacy.Value(officialCodexWebSocketCookieWriteBackContextKey{}), "旧画像 WS 握手不写回")

	withOfficialCodexSyntheticProfile(t, cookieJarTargetMutation(t))
	bound := service.bindOfficialCodexWebSocketCookieJar(base, account, officialClientProfileModeActive)
	jar := HTTPUpstreamCookieJarFromContext(bound)
	require.NotNil(t, jar)
	require.Same(t, jar, service.openAICookieJar(account), "WS 与 HTTP 共用同一账号级 jar")
	require.Same(t, jar, bound.Value(officialCodexWebSocketCookieWriteBackContextKey{}))
}

type recordingCookieJar struct {
	mu      sync.Mutex
	targets []string
	names   []string
}

func (j *recordingCookieJar) SetCookies(target *url.URL, cookies []*http.Cookie) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.targets = append(j.targets, target.String())
	for _, cookie := range cookies {
		j.names = append(j.names, cookie.Name)
	}
}

func (j *recordingCookieJar) Cookies(*url.URL) []*http.Cookie { return nil }

func TestWriteBackOfficialCodexWebSocketHandshakeCookies(t *testing.T) {
	headers := http.Header{}
	headers.Add("Set-Cookie", "__oailb=lb; Path=/; Secure")
	headers.Add("Set-Cookie", "__cf_bm=bm; Path=/; Secure")

	jar := &recordingCookieJar{}
	ctx := withOfficialCodexWebSocketCookieWriteBack(context.Background(), jar)
	writeBackOfficialCodexWebSocketHandshakeCookies(ctx, "wss://chatgpt.com/backend-api/codex/responses", headers)
	require.Equal(t, []string{"https://chatgpt.com/backend-api/codex/responses"}, jar.targets)
	require.ElementsMatch(t, []string{"__oailb", "__cf_bm"}, jar.names)

	// 上下文未声明写回（旧版本画像）时为空操作，已绑定的 jar 不会被再次写入。
	writeBackOfficialCodexWebSocketHandshakeCookies(context.Background(), "wss://chatgpt.com/backend-api/codex/responses", headers)
	require.Len(t, jar.names, 2)
}

// 拨号器在 101 与被拒绝的升级两个出口都把 Set-Cookie 交给写回；上下文未声明写回时不触碰 jar。
func TestCoderOpenAIWSDialerWritesBackHandshakeCookies(t *testing.T) {
	upgraded := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Add("Set-Cookie", "__oailb=lb; Path=/")
		conn, err := coderws.Accept(w, r, nil)
		if err != nil {
			return
		}
		_ = conn.Close(coderws.StatusNormalClosure, "")
	}))
	defer upgraded.Close()
	rejected := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Add("Set-Cookie", "__cf_bm=bm; Path=/")
		w.WriteHeader(http.StatusForbidden)
	}))
	defer rejected.Close()

	dialer := newDefaultOpenAIWSClientDialer()
	wsURL := func(server *httptest.Server) string { return "ws" + strings.TrimPrefix(server.URL, "http") }

	jar := &recordingCookieJar{}
	ctx := withOfficialCodexWebSocketCookieWriteBack(context.Background(), jar)
	conn, _, _, err := dialer.Dial(ctx, wsURL(upgraded), nil, "")
	require.NoError(t, err)
	_ = conn.Close()
	_, status, _, err := dialer.Dial(ctx, wsURL(rejected), nil, "")
	require.Error(t, err)
	require.Equal(t, http.StatusForbidden, status)
	require.ElementsMatch(t, []string{"__oailb", "__cf_bm"}, jar.names)

	// 未声明写回的上下文照常握手，不触碰任何 jar。
	conn, _, _, err = dialer.Dial(context.Background(), wsURL(upgraded), nil, "")
	require.NoError(t, err)
	_ = conn.Close()
	require.Len(t, jar.names, 2)
}
