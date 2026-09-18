package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
)

// GET /wham/usage 的 Luna Reserve 条件只由网关自身的配额请求声明：非 FedRAMP 账号
// 携带，FedRAMP 账号不带；其余 WHAM 请求头不受影响。
func TestCodexQuotaUsageHeadersDeclareLunaReserveForNonFedRAMP(t *testing.T) {
	base := http.Header{"Authorization": []string{"Bearer token"}}

	withReserve := codexQuotaUsageHeaders(base, false)
	require.Equal(t, "1", withReserve.Get("x-openai-codex-luna-reserve"))
	require.Equal(t, "Bearer token", withReserve.Get("Authorization"))
	require.Empty(t, base.Get("x-openai-codex-luna-reserve"), "不得修改传入的公共头")

	fedRAMP := codexQuotaUsageHeaders(base, true)
	require.Empty(t, fedRAMP.Get("x-openai-codex-luna-reserve"))

	require.NotNil(t, codexQuotaUsageHeaders(nil, false))
}

// 画像没有 wham_usage 的 Luna Reserve 槽位时（当前 active 画像），该条件头只作为事实
// 进入 compiler，不得以普通 Header 身份泄漏到 wire；周期入口 QueryUsageOnly 与管理端
// QueryUsage 都遵守画像闭集。
func TestCodexWhamUsageLunaReserveDoesNotLeakWithoutProfileSlot(t *testing.T) {
	account := &Account{
		ID:       711,
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Status:   StatusActive,
		Credentials: map[string]any{
			"chatgpt_account_id": "acct-luna-reserve",
		},
	}
	repo := &stubQuotaAccountRepo{accounts: map[int64]*Account{account.ID: account}}
	tokenProvider := NewOpenAITokenProvider(repo, &stubQuotaTokenCache{tokens: map[string]string{
		OpenAITokenCacheKey(account): "token-luna-reserve",
	}}, nil)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer server.Close()

	upstream := newQuotaRedirectingUpstream(server)
	service := NewOpenAIQuotaService(repo, nil, tokenProvider, upstream)
	runtimeState := defaultOfficialCodexRuntimeState()
	runtimeState.ProfileMode = officialClientProfileModeActive
	runtimeState.SurfaceID = officialCodexSurfaceTUI
	runtimeState.Originator = "codex-tui"
	runtimeState.TerminalToken = "xterm-256color"
	runtimeState.UserAgentSuffixEnabled = false
	runtimeContext, err := withOfficialCodexRuntimeState(context.Background(), runtimeState)
	require.NoError(t, err)

	_, err = service.QueryUsageOnly(runtimeContext, account.ID)
	require.NoError(t, err)
	_, err = service.QueryUsage(runtimeContext, account.ID)
	require.NoError(t, err)

	usageSeen := 0
	for _, request := range upstream.requests {
		if request.URL.Path == "/backend-api/wham/usage" {
			usageSeen++
		}
		require.Empty(t, request.Header.Get("x-openai-codex-luna-reserve"),
			"%s：active 画像没有 Luna Reserve 槽位，条件头不得进入 wire", request.URL.Path)
	}
	require.Equal(t, 2, usageSeen, "QueryUsageOnly 与 QueryUsage 各发一次 /wham/usage")
}
