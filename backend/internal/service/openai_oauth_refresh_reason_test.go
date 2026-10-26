//go:build unit

package service

import (
	"context"
	"io"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/stretchr/testify/require"
)

// 本文件覆盖 ChatGPT OAuth 令牌刷新请求体里的 refresh_reason（官方 0.164 起）：
//  1. 语义 Body 的字节与键序（四个取值），以及闭集校验、缺失失败关闭；
//  2. 画像是否声明该字段的判定（旧画像不声明、合成新画像声明）；
//  3. 旧画像下线上字节不带该字段（无论调用点传什么原因）；
//  4. 各调用点的原因映射。
//
// 新画像的最终线上字节（含 compiler 追加 refresh_token 后的键序）由 officialegress 包的
// 合成画像编译用例覆盖，两处共用同一组语义 Body 字面量。

var openAIOAuthRefreshAllReasons = []openAIOAuthRefreshReason{
	openAIOAuthRefreshReasonProactiveExpiry,
	openAIOAuthRefreshReasonScheduled,
	openAIOAuthRefreshReasonUnauthorizedRecovery,
	openAIOAuthRefreshReasonForced,
}

// 官方取值字面量单独写死一份，防止常量与官方字符串同时被改错而测试照样通过。
var openAIOAuthRefreshOfficialReasonLiterals = map[openAIOAuthRefreshReason]string{
	openAIOAuthRefreshReasonProactiveExpiry:      "proactive_expiry",
	openAIOAuthRefreshReasonScheduled:            "scheduled",
	openAIOAuthRefreshReasonUnauthorizedRecovery: "unauthorized_recovery",
	openAIOAuthRefreshReasonForced:               "forced",
}

func TestOpenAIOAuthRefreshReasonLiteralsMatchOfficial(t *testing.T) {
	require.Len(t, openAIOAuthRefreshAllReasons, len(openAIOAuthRefreshOfficialReasonLiterals))
	for _, reason := range openAIOAuthRefreshAllReasons {
		require.Equal(t, openAIOAuthRefreshOfficialReasonLiterals[reason], string(reason))
		require.True(t, reason.valid())
	}
	for _, bad := range []openAIOAuthRefreshReason{"", "Forced", "force", "unauthorized", "proactive", "other"} {
		require.False(t, bad.valid(), "非官方取值必须被闭集拒绝：%q", string(bad))
	}
}

func TestOpenAIOAuthRefreshSemanticBodyBytes(t *testing.T) {
	for _, reason := range openAIOAuthRefreshAllReasons {
		body, err := buildOpenAIOAuthRefreshSemanticBody("client-id", reason, true)
		require.NoError(t, err)
		// 语义 Body 不含 refresh_token（由 compiler 追加）；refresh_reason 紧跟 grant_type。
		require.Equal(t,
			`{"client_id":"client-id","grant_type":"refresh_token","refresh_reason":"`+
				openAIOAuthRefreshOfficialReasonLiterals[reason]+`"}`,
			string(body))
	}
}

func TestOpenAIOAuthRefreshSemanticBodyWithoutReasonIsLegacyBytes(t *testing.T) {
	// 画像未声明字段时，无论传什么原因（含空、非法）语义 Body 都与引入该字段前逐字节相同。
	for _, reason := range append(append([]openAIOAuthRefreshReason{}, openAIOAuthRefreshAllReasons...), "", "bogus") {
		body, err := buildOpenAIOAuthRefreshSemanticBody("client-id", reason, false)
		require.NoError(t, err)
		require.Equal(t, `{"client_id":"client-id","grant_type":"refresh_token"}`, string(body))
	}
}

func TestOpenAIOAuthRefreshSemanticBodyFailsClosedOnBadReason(t *testing.T) {
	for _, bad := range []openAIOAuthRefreshReason{"", "bogus", "FORCED"} {
		_, err := buildOpenAIOAuthRefreshSemanticBody("client-id", bad, true)
		require.Error(t, err, "画像要求 refresh_reason 时，缺失或非法取值必须失败关闭：%q", string(bad))
	}
}

// openAIOAuthRefreshSyntheticProfile 以正式目录 active 画像为底稿，改写 oauth_refresh 封闭 Body：
// withReason 为 true 时在 grant_type 与 refresh_token 之间插入 refresh_reason。
func openAIOAuthRefreshSyntheticProfile(t *testing.T, withReason bool) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	endpoint := syntheticServiceSnapshotEndpoint(t, &doc, officialCodexEndpointOAuthRefresh)
	fields := make([]profilecontract.SnapshotBodyField, 0, len(endpoint.Body.Fields)+1)
	for _, field := range endpoint.Body.Fields {
		if field.Name == openAIOAuthRefreshReasonFieldName {
			continue
		}
		if field.Name == "refresh_token" && withReason {
			fields = append(fields, profilecontract.SnapshotBodyField{
				Name: openAIOAuthRefreshReasonFieldName, Required: true,
			})
		}
		fields = append(fields, field)
	}
	endpoint.Body.Fields = fields
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	executable, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	return executable
}

func TestOpenAIOAuthRefreshProfileDeclaresReasonFollowsProfile(t *testing.T) {
	legacy := openAIOAuthRefreshSyntheticProfile(t, false)
	require.False(t, openAIOAuthRefreshProfileDeclaresReason(legacy))

	target := openAIOAuthRefreshSyntheticProfile(t, true)
	require.True(t, openAIOAuthRefreshProfileDeclaresReason(target))
	endpoint, found := target.Endpoint(officialCodexEndpointOAuthRefresh)
	require.True(t, found)
	names := make([]string, 0, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		names = append(names, field.Name)
	}
	// 官方 BTreeMap 编码：字典序 client_id, grant_type, refresh_reason, refresh_token。
	require.Equal(t, []string{"client_id", "grant_type", "refresh_reason", "refresh_token"}, names)

	// 没有 oauth_refresh 端点的画像按“未声明”处理，不 panic。
	require.False(t, openAIOAuthRefreshProfileDeclaresReason(profilecontract.ExecutableProfile{}))
}

// 正式目录当前 active 画像是否声明了 refresh_reason：晋升 0.164 之前为 false，之后为 true。
func officialActiveProfileDeclaresRefreshReason(t *testing.T) bool {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	return openAIOAuthRefreshProfileDeclaresReason(release.ExecutableProfile())
}

func TestOAuthRefreshWireFollowsActiveProfileForEveryReason(t *testing.T) {
	declares := officialActiveProfileDeclaresRefreshReason(t)
	for _, reason := range openAIOAuthRefreshAllReasons {
		resource := &oauthRefreshCaptureResource{}
		runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
			officialegress.DefaultGuard(), nil, officialegress.ExecutorID(t.Name()+"-"+string(reason)),
			officialegress.ReleaseModeActive, resource,
		)
		require.NoError(t, err)
		svc := NewOpenAIOAuthService(nil, nil)
		svc.SetOfficialEgressRuntime(runtimeState)

		response, err := svc.executeOAuthRefresh(
			context.Background(), "refresh-secret", "client-id", "", reason,
		)
		require.NoError(t, err)
		_ = response.Body.Close()
		body, err := io.ReadAll(resource.request.Body)
		require.NoError(t, err)
		if declares {
			// 画像已声明字段（0.164 之后的 active）：必须带，且键序为字典序。
			require.Equal(t,
				`{"client_id":"client-id","grant_type":"refresh_token","refresh_reason":"`+
					string(reason)+`","refresh_token":"refresh-secret"}`,
				string(body))
		} else {
			// 旧画像（0.163 已封）：一个字节都不多发，无论调用点传什么原因。
			require.Equal(t,
				`{"client_id":"client-id","grant_type":"refresh_token","refresh_token":"refresh-secret"}`,
				string(body))
			require.NotContains(t, string(body), "refresh_reason")
		}
	}
}

// ---------- 调用点原因映射 ----------

// recordOAuthRefreshReasons 安装观察者并返回读取已观察原因的函数；用例结束自动恢复。
func recordOAuthRefreshReasons(t *testing.T) func() []openAIOAuthRefreshReason {
	t.Helper()
	var observed []openAIOAuthRefreshReason
	previous := openAIOAuthRefreshReasonObserver
	openAIOAuthRefreshReasonObserver = func(reason openAIOAuthRefreshReason) {
		observed = append(observed, reason)
	}
	t.Cleanup(func() { openAIOAuthRefreshReasonObserver = previous })
	return func() []openAIOAuthRefreshReason { return append([]openAIOAuthRefreshReason(nil), observed...) }
}

func newOAuthRefreshMappingService(t *testing.T) (*OpenAIOAuthService, *openaiOAuthClientRefreshStub) {
	t.Helper()
	client := &openaiOAuthClientRefreshStub{refreshResponse: &openai.TokenResponse{
		AccessToken: "rotated-access-token", RefreshToken: "rotated-refresh-token", ExpiresIn: 3600,
	}}
	svc := NewOpenAIOAuthService(nil, client)
	t.Cleanup(svc.Stop)
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(), nil, officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive, &oauthRefreshCaptureResource{},
	)
	require.NoError(t, err)
	svc.SetOfficialEgressRuntime(runtimeState)
	return svc, client
}

func newOAuthRefreshMappingAccount() *Account {
	return &Account{
		ID: 901, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive,
		Credentials: map[string]any{
			"access_token":       "old-access-token",
			"refresh_token":      "old-refresh-token",
			"chatgpt_account_id": "acct-mapping",
			// 令牌即将过期，主动刷新路径会命中。
			"expires_at": time.Now().Add(time.Minute).UTC().Format(time.RFC3339),
		},
	}
}

func TestOpenAIOAuthRefreshReasonMapping_ManualAndSyncAreForced(t *testing.T) {
	observed := recordOAuthRefreshReasons(t)
	svc, _ := newOAuthRefreshMappingService(t)

	// 管理端手动刷新（account_handler、openai_oauth_handler 的 RefreshAccountToken）与
	// CRS 同步（crs_sync_service）都调用 RefreshAccountToken。
	_, err := svc.RefreshAccountToken(context.Background(), newOAuthRefreshMappingAccount())
	require.NoError(t, err)
	// 粘贴 refresh_token 校验（openai_oauth_handler.RefreshToken）与 RefreshToken。
	_, err = svc.RefreshTokenWithClientID(context.Background(), "pasted-refresh-token", "", "client-id")
	require.NoError(t, err)
	_, err = svc.RefreshToken(context.Background(), "pasted-refresh-token", "")
	require.NoError(t, err)

	require.Equal(t, []openAIOAuthRefreshReason{
		openAIOAuthRefreshReasonForced, openAIOAuthRefreshReasonForced, openAIOAuthRefreshReasonForced,
	}, observed())
}

func TestOpenAIOAuthRefreshReasonMapping_ExplicitReasonPassesThrough(t *testing.T) {
	observed := recordOAuthRefreshReasons(t)
	svc, _ := newOAuthRefreshMappingService(t)
	for _, reason := range openAIOAuthRefreshAllReasons {
		_, err := svc.RefreshAccountTokenWithReason(context.Background(), newOAuthRefreshMappingAccount(), reason)
		require.NoError(t, err)
	}
	require.Equal(t, openAIOAuthRefreshAllReasons, observed())
}

func TestOpenAIOAuthRefreshReasonMapping_BackgroundExecutorIsProactiveExpiry(t *testing.T) {
	observed := recordOAuthRefreshReasons(t)
	svc, _ := newOAuthRefreshMappingService(t)
	account := newOAuthRefreshMappingAccount()
	repo := &refreshAPIAccountRepo{account: account}
	executor := NewOpenAITokenRefresher(svc, repo)

	// 后台刷新服务（token_refresh_service）把同一个 OpenAITokenRefresher 当执行器交给
	// OAuthRefreshAPI.RefreshIfNeeded，这里走同一条链路。
	result, err := NewOAuthRefreshAPI(repo, nil).RefreshIfNeeded(
		context.Background(), account, executor, 5*time.Minute,
	)
	require.NoError(t, err)
	require.True(t, result.Refreshed)
	require.Equal(t, []openAIOAuthRefreshReason{openAIOAuthRefreshReasonProactiveExpiry}, observed())
}

func TestOpenAIOAuthRefreshReasonMapping_RequestPathIsProactiveExpiry(t *testing.T) {
	observed := recordOAuthRefreshReasons(t)
	svc, client := newOAuthRefreshMappingService(t)
	account := newOAuthRefreshMappingAccount()
	repo := &refreshAPIAccountRepo{account: account}
	executor := NewOpenAITokenRefresher(svc, repo)

	// 请求路径：OpenAITokenProvider.GetAccessToken 发现令牌临近过期，经 OAuthRefreshAPI 调
	// OpenAITokenRefresher.Refresh。
	provider := NewOpenAITokenProvider(repo, nil, svc)
	provider.SetRefreshAPI(NewOAuthRefreshAPI(repo, nil), executor)
	token, err := provider.GetAccessToken(context.Background(), account)
	require.NoError(t, err)
	require.Equal(t, "rotated-access-token", token)
	require.Equal(t, int32(1), atomic.LoadInt32(&client.refreshCalls))
	require.Equal(t, []openAIOAuthRefreshReason{openAIOAuthRefreshReasonProactiveExpiry}, observed())
}

func TestOpenAIOAuthRefreshReasonMapping_NoCallSiteProducesUnusedReasons(t *testing.T) {
	// 我方没有“401 后刷新再重试”路径，也没有按 8 天未刷新触发的路径：当前所有调用点都只会得到
	// forced 或 proactive_expiry。这里把“未使用”也钉住，将来新增调用点要显式改本表。
	require.NotEqual(t, openAIOAuthRefreshReasonScheduled, openAIOAuthProactiveRefreshReason())
	require.NotEqual(t, openAIOAuthRefreshReasonUnauthorizedRecovery, openAIOAuthProactiveRefreshReason())
	require.Equal(t, openAIOAuthRefreshReasonProactiveExpiry, openAIOAuthProactiveRefreshReason())
}
