package handler

// Composite 分组在线目录的接点：基础目录只从可调度的官方账号拉取，拉不到改为本地生成，不再因为调度到
// 中转账号而返回 500。

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// cxtpCompositeRelayAccount 是 Composite 分组里的中转 API Key 账号，显式映射一个 GPT 名称。
func cxtpCompositeRelayAccount() service.Account {
	return service.Account{
		ID: 1, Name: "relay", Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: 0,
		Credentials: map[string]any{
			"api_key": "sk-relay", "base_url": "https://relay.example/v1",
			"model_mapping": map[string]any{"gpt-6.1-sol": "gpt-6.1-sol"},
		},
	}
}

// cxtpCompositeOfficialAccount 是官方地址的 API Key 账号：属于官方账号，又能在单测里经上游桩拉取目录
// （OAuth 账号需要完整的官方出站运行时，单测里没有）。
func cxtpCompositeOfficialAccount() service.Account {
	return service.Account{
		ID: 2, Name: "official", Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true, Concurrency: 1, Priority: 1,
		Credentials: map[string]any{"api_key": "sk-official", "base_url": "https://api.openai.com/v1"},
	}
}

// cxtpCompositeHandler 装配 Composite 分组的 handler：真实的目录服务加假的账号仓库与上游。
func cxtpCompositeHandler(t *testing.T, upstream *codexModelsFailoverHTTPUpstream, enabled []string, accounts ...service.Account) *OpenAIGatewayHandler {
	t.Helper()
	repo := &codexModelsFailoverAccountRepo{accounts: accounts}
	gatewayService := service.NewOpenAIGatewayService(
		repo,
		nil, nil, nil, nil, nil, nil, &config.Config{RunMode: config.RunModeSimple}, nil, nil, nil, nil, nil,
		upstream,
		nil, nil, nil, nil, nil, nil, nil, nil,
		nil,
	)
	h := &OpenAIGatewayHandler{gatewayService: gatewayService}
	catalog := cxtpService(t, enabled, accounts...)
	h.SetCodexThirdPartyCatalog(catalog)
	return h
}

func TestCodexCompositeCatalogWithOnlyRelayAccountsNeverFetchesAndReturns200(t *testing.T) {
	upstream := &codexModelsFailoverHTTPUpstream{}
	relay := cxtpCompositeRelayAccount()
	kimi := cxtpKimiAccount()
	h := cxtpCompositeHandler(t, upstream, []string{"kimi-k3"}, relay, kimi)
	group := &service.Group{ID: 8, Platform: service.PlatformComposite}

	rec := performCodexModelsRequestForGroup(t, h, group, "")
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	require.Equal(t, []string{"gpt-6.1-sol", "kimi-k3"}, codexHandlerManifestSlugs(t, rec), "本地生成的 GPT 条目加已开启的三方条目")
	require.Empty(t, upstream.calls(), "不得向任何账号（尤其是中转站）拉取目录")
	etag := rec.Header().Get("ETag")
	require.NotEmpty(t, etag)

	// 相同输入的第二次请求带着 ETag 得到 304。
	rec = performCodexModelsRequestForGroup(t, h, group, etag)
	require.Equal(t, http.StatusNotModified, rec.Code)
}

func TestCodexCompositeCatalogUsesSchedulableOfficialAccount(t *testing.T) {
	upstream := &codexModelsFailoverHTTPUpstream{}
	h := cxtpCompositeHandler(t, upstream, nil, cxtpCompositeRelayAccount(), cxtpCompositeOfficialAccount())
	group := &service.Group{ID: 8, Platform: service.PlatformComposite}

	rec := performCodexModelsRequestForGroup(t, h, group, "")
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	// 官方上游的目录（桩返回 gpt-5.6-sol）原样作为基础，本地映射里的 gpt-6.1-sol 不混入。
	require.Equal(t, []string{"gpt-5.6-sol"}, codexHandlerManifestSlugs(t, rec))
	require.Equal(t, []int64{2}, upstream.calls(), "只拉官方账号，中转账号 1 不拉")
}

func TestCodexCompositeCatalogFallsBackToLocalWhenOfficialFetchFails(t *testing.T) {
	upstream := &codexModelsFailoverHTTPUpstream{statuses: map[int64]int{2: http.StatusBadGateway}}
	h := cxtpCompositeHandler(t, upstream, nil, cxtpCompositeRelayAccount(), cxtpCompositeOfficialAccount())
	group := &service.Group{ID: 8, Platform: service.PlatformComposite}

	rec := performCodexModelsRequestForGroup(t, h, group, "")
	require.Equal(t, http.StatusOK, rec.Code, "官方账号失败不再是 500：%s", rec.Body.String())
	require.Contains(t, codexHandlerManifestSlugs(t, rec), "gpt-6.1-sol", "改为按账号映射本地生成")
	require.Equal(t, []int64{2}, upstream.calls())
}
