//go:build unit

package service

import (
	"context"
	"errors"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// customTestAccount 构造一个自定义多协议平台账号；urls 为 api_base_urls（协议 → 地址）。
func customTestAccount(urls map[string]any) *Account {
	credentials := map[string]any{
		"api_key":      "sk-custom-test",
		"api_protocol": APIProtocolAdaptive,
	}
	if urls != nil {
		credentials["api_base_urls"] = urls
	}
	return &Account{
		ID:          801,
		Name:        "custom-test",
		Platform:    PlatformCustom,
		Type:        AccountTypeAPIKey,
		Status:      StatusActive,
		Concurrency: 1,
		Credentials: credentials,
	}
}

// customFullURLs 是三个协议地址都填写的凭据：Responses 与 Chat Completions 填到版本号，
// Anthropic 填到域名。
func customFullURLs() map[string]any {
	return map[string]any{
		APIProtocolChatCompletions: "http://chat.example/v1",
		APIProtocolResponses:       "http://responses.example/v1",
		APIProtocolAnthropic:       "http://anthropic.example",
	}
}

// ---------- 平台登记与 profile ----------

func TestCustomPlatformRegistration(t *testing.T) {
	spec, ok := domain.LookupPlatform(PlatformCustom)
	require.True(t, ok)
	require.Equal(t, "custom", spec.ID)
	require.Equal(t, "自定义多协议", spec.DisplayName)
	require.Equal(t, domain.PlatformGatewayOpenAI, spec.Gateway)
	require.True(t, domain.UsesOpenAIGateway(PlatformCustom))
	require.True(t, domain.IsGroupPlatform(PlatformCustom))
	require.Contains(t, AllowedQuotaPlatforms, PlatformCustom)

	// 加入 Composite 回退顺序末尾。
	order := domain.CompositePrecedencePlatformIDs()
	require.Equal(t, PlatformCustom, order[len(order)-1])
	require.Contains(t, matchingPlatforms(PlatformComposite), PlatformCustom)
	require.True(t, isConcreteRequestPlatform(PlatformCustom))
}

func TestCustomPlatformProfileHasNoDefaultEndpoints(t *testing.T) {
	profile := LookupProviderProfile(PlatformCustom)
	require.NotNil(t, profile)
	require.True(t, profile.CredentialEndpoints)
	for mode, endpoints := range profile.Modes {
		require.Empty(t, endpoints.BaseURLs, mode)
	}
	for _, protocol := range []string{APIProtocolChatCompletions, APIProtocolResponses, APIProtocolAnthropic} {
		require.Empty(t, profile.DefaultBaseURL("", protocol), protocol)
		require.False(t, profile.SupportsProtocol("", protocol), protocol)
	}

	account := customTestAccount(customFullURLs())
	require.True(t, account.IsCustom())
	require.True(t, account.IsMultiProtocolAPIKey())
	require.True(t, account.RoutesProtocolByInbound())
	require.False(t, account.routesByModel())
	require.True(t, account.IsAdaptiveAPIProtocol())
	// 其它平台的 profile 不带该标志。
	for platform, other := range providerProfiles {
		if platform != PlatformCustom {
			require.False(t, other.CredentialEndpoints, platform)
		}
	}
}

// ---------- 协议支持判定 ----------

func TestCustomProviderSupportsProtocolFollowsCredentials(t *testing.T) {
	cases := []struct {
		name                       string
		urls                       map[string]any
		chat, responses, anthropic bool
	}{
		{name: "三个地址都填", urls: customFullURLs(), chat: true, responses: true, anthropic: true},
		{
			name: "只填 Chat Completions",
			urls: map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"},
			chat: true,
		},
		{
			name:      "Chat Completions 加 Responses",
			urls:      map[string]any{APIProtocolChatCompletions: "http://chat.example/v1", APIProtocolResponses: "http://r.example/v1"},
			chat:      true,
			responses: true,
		},
		{
			name:      "Chat Completions 加 Anthropic",
			urls:      map[string]any{APIProtocolChatCompletions: "http://chat.example/v1", APIProtocolAnthropic: "http://a.example"},
			chat:      true,
			anthropic: true,
		},
		{
			name: "空白地址视为未填",
			urls: map[string]any{APIProtocolChatCompletions: "http://chat.example/v1", APIProtocolResponses: "   ", APIProtocolAnthropic: ""},
			chat: true,
		},
		{name: "没有 api_base_urls", urls: nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			account := customTestAccount(tc.urls)
			require.Equal(t, tc.chat, account.providerSupportsProtocol(APIProtocolChatCompletions))
			require.Equal(t, tc.responses, account.providerSupportsProtocol(APIProtocolResponses))
			require.Equal(t, tc.anthropic, account.providerSupportsProtocol(APIProtocolAnthropic))
			// Responses 能力判定走同一入口。
			require.Equal(t, tc.responses, account.SupportsNativeCNResponses())
			require.Equal(t, tc.responses, account.UsesNativeCNResponses())
		})
	}

	// api_base_urls 以 map[string]string 形态出现时同样识别。
	account := customTestAccount(nil)
	account.Credentials["api_base_urls"] = map[string]string{APIProtocolChatCompletions: "http://chat.example/v1", APIProtocolAnthropic: "http://a.example"}
	require.True(t, account.providerSupportsProtocol(APIProtocolAnthropic))
	require.False(t, account.providerSupportsProtocol(APIProtocolResponses))
}

func TestOtherProvidersKeepProfileDefaultProtocolSupport(t *testing.T) {
	// 既有平台继续按 profile 默认地址判定，凭据里填什么都不改变支持范围。
	deepseek := &Account{Platform: PlatformDeepseek, Type: AccountTypeAPIKey, Credentials: map[string]any{
		"api_base_urls": map[string]any{APIProtocolChatCompletions: "http://x.example"},
	}}
	require.True(t, deepseek.providerSupportsProtocol(APIProtocolResponses))
	require.True(t, deepseek.providerSupportsProtocol(APIProtocolAnthropic))
	zhipu := &Account{Platform: PlatformZhipu, Type: AccountTypeAPIKey, Credentials: map[string]any{
		"api_base_urls": map[string]any{APIProtocolResponses: "http://x.example"},
	}}
	require.False(t, zhipu.providerSupportsProtocol(APIProtocolResponses))
	require.True(t, zhipu.providerSupportsProtocol(APIProtocolAnthropic))
}

func TestCustomResolveUpstreamProtocolAdaptive(t *testing.T) {
	cases := []struct {
		name    string
		urls    map[string]any
		inbound string
		want    string
	}{
		{"全齐 Chat 入站", customFullURLs(), APIProtocolChatCompletions, APIProtocolChatCompletions},
		{"全齐 Responses 入站", customFullURLs(), APIProtocolResponses, APIProtocolResponses},
		{"全齐 Anthropic 入站", customFullURLs(), APIProtocolAnthropic, APIProtocolAnthropic},
		{"只填 Chat：Responses 入站回落", map[string]any{APIProtocolChatCompletions: "http://c.example/v1"}, APIProtocolResponses, APIProtocolChatCompletions},
		{"只填 Chat：Anthropic 入站回落", map[string]any{APIProtocolChatCompletions: "http://c.example/v1"}, APIProtocolAnthropic, APIProtocolChatCompletions},
		{"只填 Chat：Chat 入站直通", map[string]any{APIProtocolChatCompletions: "http://c.example/v1"}, APIProtocolChatCompletions, APIProtocolChatCompletions},
		{
			"Responses 地址清空后 Responses 入站回落",
			map[string]any{APIProtocolChatCompletions: "http://c.example/v1", APIProtocolResponses: "", APIProtocolAnthropic: "http://a.example"},
			APIProtocolResponses, APIProtocolChatCompletions,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, resolveUpstreamProtocol(customTestAccount(tc.urls), tc.inbound, "any-model", nil))
		})
	}
}

// ---------- 地址拼接 ----------

func TestCustomEndpointURLAssembly(t *testing.T) {
	// Responses：版本感知追加；已是端点全路径时不重复追加。
	for base, want := range map[string]string{
		"https://ollama.com/v1":                 "https://ollama.com/v1/responses",
		"https://ollama.com":                    "https://ollama.com/v1/responses",
		"https://relay.example.com/api/v4":      "https://relay.example.com/api/v4/responses",
		"https://relay.example.com/x/responses": "https://relay.example.com/x/responses",
		"https://ollama.com/v1/responses":       "https://ollama.com/v1/responses",
	} {
		require.Equal(t, want, buildOpenAIResponsesURLForPlatform(PlatformCustom, base), base)
	}
	// Chat Completions：同样版本感知，端点全路径不重复。
	for base, want := range map[string]string{
		"https://ollama.com/v1":                  "https://ollama.com/v1/chat/completions",
		"https://ollama.com/v1/chat/completions": "https://ollama.com/v1/chat/completions",
		"https://relay.example.com/api/paas/v4":  "https://relay.example.com/api/paas/v4/chat/completions",
	} {
		require.Equal(t, want, buildOpenAIChatCompletionsURL(base), base)
	}
	// Anthropic：填到域名，追加 /v1/messages。
	account := customTestAccount(customFullURLs())
	require.Equal(t, "https://ollama.com/v1/messages", nativeAnthropicMessagesURL(account, "https://ollama.com"))
	require.Equal(t, "https://ollama.com/v1/messages", nativeAnthropicMessagesURL(account, "https://ollama.com/"))
}

func TestCustomAnthropicAuthHeaderFollowsExtraSwitchAndOllamaBearer(t *testing.T) {
	account := customTestAccount(customFullURLs())
	// 默认 x-api-key。
	header := map[string][]string{}
	setAnthropicAPIKeyAuthHeader(header, account, "tok", account.GetCNProtocolBaseURL(APIProtocolAnthropic))
	require.Equal(t, []string{"tok"}, header["X-Api-Key"])

	// 沿用账号 extra 已有开关：Bearer。
	account.Extra = map[string]any{anthropicAPIKeyAuthSchemeExtraKey: AnthropicAPIKeyAuthSchemeAuthorizationBearer}
	header = map[string][]string{}
	setAnthropicAPIKeyAuthHeader(header, account, "tok", account.GetCNProtocolBaseURL(APIProtocolAnthropic))
	require.Equal(t, []string{"Bearer tok"}, header["Authorization"])

	// Ollama Cloud Anthropic 端点强制 Bearer。
	ollama := customTestAccount(map[string]any{
		APIProtocolChatCompletions: "https://ollama.com/v1",
		APIProtocolAnthropic:       "https://ollama.com",
	})
	header = map[string][]string{}
	setAnthropicAPIKeyAuthHeader(header, ollama, "tok", ollama.GetCNProtocolBaseURL(APIProtocolAnthropic))
	require.Equal(t, []string{"Bearer tok"}, header["Authorization"])
}

// ---------- 连接测试 ----------

func TestCustomAccountConnectionTestHitsEveryFilledEndpoint(t *testing.T) {
	account := customTestAccount(customFullURLs())
	svc, upstream := adaptiveCNAccountTestService(
		account,
		adaptiveCNChatTestResponse(),
		adaptiveCNAnthropicTestResponse(),
		adaptiveCNResponsesTestResponse(),
	)
	c, _ := newTestContext()

	require.NoError(t, svc.TestAccountConnection(c, account.ID, "deepseek-flash", "hello", AccountTestModeDefault))

	require.Len(t, upstream.requests, 3)
	require.Equal(t, "http://chat.example/v1/chat/completions", upstream.requests[0].URL.String())
	require.Equal(t, "http://anthropic.example/v1/messages", upstream.requests[1].URL.String())
	require.Equal(t, "http://responses.example/v1/responses", upstream.requests[2].URL.String())
	require.Equal(t, "Bearer sk-custom-test", upstream.requests[0].Header.Get("Authorization"))
	require.Equal(t, "sk-custom-test", upstream.requests[1].Header.Get("x-api-key"))
	require.Equal(t, "Bearer sk-custom-test", upstream.requests[2].Header.Get("Authorization"))
	// 原生 Responses 探测同样是无状态。
	require.False(t, gjson.GetBytes(upstream.bodies[2], "store").Bool())
}

func TestCustomAccountConnectionTestSkipsUnfilledEndpoints(t *testing.T) {
	account := customTestAccount(map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"})
	svc, upstream := adaptiveCNAccountTestService(account, adaptiveCNChatTestResponse())
	c, _ := newTestContext()

	require.NoError(t, svc.TestAccountConnection(c, account.ID, "deepseek-flash", "hello", AccountTestModeDefault))

	require.Len(t, upstream.requests, 1)
	require.Equal(t, "http://chat.example/v1/chat/completions", upstream.requests[0].URL.String())
}

// ---------- 真实转发 ----------

func customForwardService() (*OpenAIGatewayService, *httpUpstreamRecorder) {
	upstream := &httpUpstreamRecorder{err: errors.New("stop after capture")}
	return &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}, upstream
}

func TestCustomForwardSameProtocolGoesToNativeEndpoint(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("Chat Completions 入站", func(t *testing.T) {
		svc, upstream := customForwardService()
		body := []byte(`{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}],"stream":false}`)
		_, err := svc.ForwardAsChatCompletions(context.Background(), adaptiveProtocolTestContext("/v1/chat/completions", body), customTestAccount(customFullURLs()), body, "", "")
		require.Error(t, err)
		require.Equal(t, "http://chat.example/v1/chat/completions", upstream.lastReq.URL.String())
		require.Equal(t, "Bearer sk-custom-test", upstream.lastReq.Header.Get("Authorization"))
		require.True(t, gjson.GetBytes(upstream.lastBody, "messages").IsArray())
	})

	t.Run("Anthropic 入站", func(t *testing.T) {
		svc, upstream := customForwardService()
		body := []byte(`{"model":"deepseek-flash","max_tokens":32,"messages":[{"role":"user","content":"hi"}],"stream":false}`)
		_, err := svc.ForwardAsAnthropic(context.Background(), adaptiveProtocolTestContext("/v1/messages", body), customTestAccount(customFullURLs()), body, "", "")
		require.Error(t, err)
		require.Equal(t, "http://anthropic.example/v1/messages", upstream.lastReq.URL.String())
		require.Equal(t, "sk-custom-test", upstream.lastReq.Header.Get("x-api-key"))
	})

	t.Run("Anthropic 入站且无 Anthropic 地址时回落 Chat Completions", func(t *testing.T) {
		svc, upstream := customForwardService()
		body := []byte(`{"model":"deepseek-flash","max_tokens":32,"messages":[{"role":"user","content":"hi"}],"stream":false}`)
		account := customTestAccount(map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"})
		_, err := svc.ForwardAsAnthropic(context.Background(), adaptiveProtocolTestContext("/v1/messages", body), account, body, "", "")
		require.Error(t, err)
		require.Equal(t, "http://chat.example/v1/chat/completions", upstream.lastReq.URL.String())
	})

	t.Run("Responses 入站且无 Responses 地址时回落 Chat Completions", func(t *testing.T) {
		svc, upstream := customForwardService()
		body := []byte(`{"model":"deepseek-flash","input":"hi","stream":false}`)
		account := customTestAccount(map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"})
		_, err := svc.Forward(context.Background(), adaptiveProtocolTestContext("/v1/responses", body), account, body)
		require.Error(t, err)
		require.Equal(t, "http://chat.example/v1/chat/completions", upstream.lastReq.URL.String())
	})
}

// 无状态归一化：custom 与国产原生 Responses 出站一律 store=false，并删除顶层
// previous_response_id 与 conversation；这里走真实 Forward，证明确实经过该归一化。
func TestStatelessResponsesNormalizationForCustomAndDomestic(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"model":"deepseek-flash","input":"hi","store":true,"previous_response_id":"resp_old","conversation":{"id":"conv_1"},"stream":false}`)

	cases := []struct {
		name    string
		account *Account
		wantURL string
	}{
		{"自定义多协议平台", customTestAccount(customFullURLs()), "http://responses.example/v1/responses"},
		{"国产平台 DeepSeek", adaptiveProtocolTestAccount(PlatformDeepseek, map[string]any{
			APIProtocolChatCompletions: "http://chat.example",
			APIProtocolResponses:       "http://responses.example",
		}), "http://responses.example/responses"},
		{"国产平台 Kimi", adaptiveProtocolTestAccount(PlatformKimi, map[string]any{
			APIProtocolChatCompletions: "http://chat.example",
			APIProtocolResponses:       "http://responses.example/v1",
		}), "http://responses.example/v1/responses"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc, upstream := customForwardService()
			_, err := svc.Forward(context.Background(), adaptiveProtocolTestContext("/v1/responses", body), tc.account, body)
			require.Error(t, err)
			require.Equal(t, tc.wantURL, upstream.lastReq.URL.String())
			require.True(t, gjson.GetBytes(upstream.lastBody, "store").Exists())
			require.False(t, gjson.GetBytes(upstream.lastBody, "store").Bool())
			require.False(t, gjson.GetBytes(upstream.lastBody, "previous_response_id").Exists())
			require.False(t, gjson.GetBytes(upstream.lastBody, "conversation").Exists())
			require.Equal(t, "hi", gjson.GetBytes(upstream.lastBody, "input").String())
		})
	}
}

func TestStatelessNormalizationLeavesNonNativeResponsesAlone(t *testing.T) {
	body := []byte(`{"model":"m","input":"hi","store":true,"previous_response_id":"resp_old","conversation":"conv_1"}`)

	// custom 没填 Responses 地址：不走原生 Responses，归一化不介入。
	noResponses := customTestAccount(map[string]any{APIProtocolChatCompletions: "http://chat.example/v1"})
	require.Equal(t, string(body), string(normalizeDeepSeekResponsesRequestBody(noResponses, body)))

	// OpenAI 平台 API Key 账号保持原样（有状态续链仍由 OpenAI 自己处理）。
	openaiAccount := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: map[string]any{"base_url": "https://api.openai.com"}}
	require.Equal(t, string(body), string(normalizeDeepSeekResponsesRequestBody(openaiAccount, body)))

	// 填了 Responses 地址的 custom 账号直接调用归一化函数同样生效。
	withResponses := customTestAccount(customFullURLs())
	out := normalizeDeepSeekResponsesRequestBody(withResponses, body)
	require.False(t, gjson.GetBytes(out, "store").Bool())
	require.False(t, gjson.GetBytes(out, "previous_response_id").Exists())
	require.False(t, gjson.GetBytes(out, "conversation").Exists())
}

// ---------- Composite 模型列表聚合 ----------

func TestCustomAccountSupportsCompositeModelListProtocol(t *testing.T) {
	cases := []struct {
		name          string
		account       *Account
		wantOpenAI    bool
		wantAnthropic bool
	}{
		{"三地址齐全", customTestAccount(customFullURLs()), true, true},
		{"只填 Chat Completions", customTestAccount(map[string]any{APIProtocolChatCompletions: "http://c.example/v1"}), true, false},
		{"Chat Completions 加 Anthropic", customTestAccount(map[string]any{
			APIProtocolChatCompletions: "http://c.example/v1", APIProtocolAnthropic: "http://a.example"}), true, true},
		{"没有任何地址", customTestAccount(nil), false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.wantOpenAI, accountSupportsCompositeModelListProtocol(tc.account, CompositeModelListProtocolOpenAI))
			require.Equal(t, tc.wantAnthropic, accountSupportsCompositeModelListProtocol(tc.account, CompositeModelListProtocolAnthropic))
		})
	}
	// Command Code / Cline 等其它多协议 profile 平台现在也按 profile 与凭据判定，
	// 详见 composite_model_protocol_profiled_test.go；不是多协议 profile 的平台仍不进入。
	require.False(t, accountSupportsCompositeModelListProtocol(&Account{Platform: PlatformGemini}, CompositeModelListProtocolOpenAI))
}

func TestCompositeProtocolModelSourcesIncludeCustomAccounts(t *testing.T) {
	groupID := int64(95)
	account := customTestAccount(customFullURLs())
	account.Credentials["model_mapping"] = map[string]any{"deepseek-flash": "deepseek-v3.1"}
	chatOnly := customTestAccount(map[string]any{APIProtocolChatCompletions: "http://c.example/v1"})
	chatOnly.ID = 802
	chatOnly.Credentials["model_mapping"] = map[string]any{"qwen-chat": "qwen3"}
	svc := &GatewayService{accountRepo: &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: {*account, *chatOnly}}}}

	openaiSources := svc.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolOpenAI)
	require.Equal(t, []string{"deepseek-flash", "qwen-chat"}, openaiSources[PlatformCustom])

	anthropicSources := svc.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolAnthropic)
	require.Equal(t, []string{"deepseek-flash"}, anthropicSources[PlatformCustom])
}

// ---------- Ollama 能力继承 ----------

func customOllamaAccount() *Account {
	account := customTestAccount(map[string]any{
		APIProtocolChatCompletions: "https://ollama.com/v1",
		APIProtocolAnthropic:       "https://ollama.com",
	})
	account.Credentials["base_url"] = "https://ollama.com/v1"
	return account
}

func TestCustomInheritsOllamaCloudUsageWhitelist(t *testing.T) {
	require.True(t, isOllamaCloudUsagePlatform(PlatformCustom))
	require.True(t, IsOllamaCloudUsageAccount(customOllamaAccount()))

	notOllama := customTestAccount(customFullURLs())
	notOllama.Credentials["base_url"] = "http://chat.example/v1"
	require.False(t, IsOllamaCloudUsageAccount(notOllama))

	// 既有白名单成员不变，未登记平台仍被排除。
	for _, platform := range []string{PlatformOpenAI, PlatformAnthropic, PlatformKimi, PlatformZhipu, PlatformDeepseek, PlatformMiniMax} {
		require.True(t, isOllamaCloudUsagePlatform(platform), platform)
	}
	for _, platform := range []string{PlatformGemini, PlatformGrok, PlatformCline, PlatformCommandCode} {
		require.False(t, isOllamaCloudUsagePlatform(platform), platform)
	}
}

func TestOllamaCloudHooksJudgeByOutboundAddress(t *testing.T) {
	// custom：出站地址是 Ollama Cloud 才生效，无需「强制 Chat Completions」开关。
	require.True(t, isOllamaCloudRawChatCompletionsAccount(customOllamaAccount()))
	notOllama := customTestAccount(customFullURLs())
	require.False(t, isOllamaCloudRawChatCompletionsAccount(notOllama))

	// reasoning 归一化：assistant 历史里的 reasoning_content 补齐为 reasoning。
	body := []byte(`{"model":"gpt-oss","messages":[{"role":"assistant","content":"x","reasoning_content":"think"}]}`)
	out := applyOllamaCloudRawChatCompletionsRequest(customOllamaAccount(), body)
	require.Equal(t, "think", gjson.GetBytes(out, "messages.0.reasoning").String())
	require.Equal(t, string(body), string(applyOllamaCloudRawChatCompletionsRequest(notOllama, body)))

	// max_tokens 夹紧：非 DeepSeek 模型在 custom 的 Ollama 出站上同样夹紧。
	big := []byte(`{"model":"gpt-oss:120b","max_tokens":200000}`)
	require.Equal(t, int64(ollamaCloudDefaultMaxTokensCap), gjson.GetBytes(clampOllamaCloudUpstreamMaxTokens(customOllamaAccount(), big), "max_tokens").Int())
	require.Equal(t, int64(200000), gjson.GetBytes(clampOllamaCloudUpstreamMaxTokens(notOllama, big), "max_tokens").Int())

	// 既有 OpenAI 平台挂 Ollama 的账号：必须强制 Chat Completions 才算，行为不变。
	openaiOllama := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: map[string]any{"base_url": "https://ollama.com/v1"}}
	require.False(t, isOllamaCloudRawChatCompletionsAccount(openaiOllama))
	require.Equal(t, int64(200000), gjson.GetBytes(clampOllamaCloudUpstreamMaxTokens(openaiOllama, big), "max_tokens").Int())
	openaiOllama.Extra = map[string]any{"openai_responses_mode": "force_chat_completions"}
	require.True(t, isOllamaCloudRawChatCompletionsAccount(openaiOllama))
	require.Equal(t, int64(ollamaCloudDefaultMaxTokensCap), gjson.GetBytes(clampOllamaCloudUpstreamMaxTokens(openaiOllama, big), "max_tokens").Int())

	// 其它平台挂 Ollama 的行为不变：DeepSeek 模型夹紧，非 DeepSeek 模型不夹紧。
	zhipu := &Account{Platform: PlatformZhipu, Type: AccountTypeAPIKey, Credentials: map[string]any{"base_url": "https://ollama.com/v1"}}
	require.False(t, isOllamaCloudRawChatCompletionsAccount(zhipu))
	require.Equal(t, int64(200000), gjson.GetBytes(clampOllamaCloudUpstreamMaxTokens(zhipu, big), "max_tokens").Int())
}

// ---------- 各类「允许平台」白名单 ----------

// custom 是 OpenAI 网关族、API Key 计费的普通上游平台，对齐 cline / command_code：
// 由平台清单派生的白名单自动包含它；依赖具体用量窗口实现的白名单不包含它。
func TestCustomPlatformAllowedPlatformLists(t *testing.T) {
	// 用户 × 平台配额、系统与认证来源默认配额共用同一份白名单。
	require.True(t, IsAllowedQuotaPlatform(PlatformCustom))
	require.NoError(t, validateDefaultPlatformQuotaMap(map[string]*DefaultPlatformQuotaSetting{
		PlatformCustom: {},
	}))
	// 错误透传规则平台、渠道定价（Composite 逐平台匹配）、调度快照都由平台清单派生。
	require.Contains(t, schedulerSnapshotPlatforms(), PlatformCustom)
	require.Contains(t, matchingPlatforms(PlatformComposite), PlatformCustom)
	require.Equal(t, []string{PlatformCustom}, matchingPlatforms(PlatformCustom))

	// 自动停调阈值只对有原生 / Coding Plan 用量窗口的平台开放，custom 没有这类窗口。
	require.NotContains(t, AllowedSchedulingThresholdPlatforms, PlatformCustom)
	_, err := validateAndNormalizeAccountSchedulingThresholds(map[string]int{PlatformCustom: 80})
	require.Error(t, err)
	// 与同类按量平台一致。
	require.NotContains(t, AllowedSchedulingThresholdPlatforms, PlatformCline)
}
