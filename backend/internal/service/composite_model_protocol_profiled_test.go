//go:build unit

package service

// 多协议 profile 平台（Command Code、Cline、OpenCode Go、自定义）进入 Composite 模型列表的判定单测：
// 按 profile 与账号凭据判定，不依赖运行时才有的上游模型目录缓存；既有平台的结果保持不变。

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

func profiledAccount(platform string, credentials map[string]any) *Account {
	creds := map[string]any{"api_key": "sk-profiled"}
	for key, value := range credentials {
		creds[key] = value
	}
	return &Account{ID: 901, Name: platform, Platform: platform, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true, Credentials: creds}
}

func TestProfiledPlatformsEnterCompositeModelList(t *testing.T) {
	threeURLs := map[string]any{
		APIProtocolChatCompletions: "http://cc.example/provider/v1",
		APIProtocolResponses:       "http://cc.example/provider/v1",
		APIProtocolAnthropic:       "http://cc.example/provider",
	}
	cases := []struct {
		name          string
		account       *Account
		wantOpenAI    bool
		wantAnthropic bool
	}{
		{"command_code 三地址（缺省 adaptive）", profiledAccount(PlatformCommandCode, map[string]any{"api_base_urls": threeURLs}), true, true},
		{"command_code 只用内置地址", profiledAccount(PlatformCommandCode, nil), true, true},
		{"command_code 固定 Chat Completions", profiledAccount(PlatformCommandCode, map[string]any{"api_protocol": APIProtocolChatCompletions}), true, false},
		{"command_code 固定 Anthropic", profiledAccount(PlatformCommandCode, map[string]any{"api_protocol": APIProtocolAnthropic}), false, true},
		{"cline 只有 Chat Completions", profiledAccount(PlatformCline, nil), true, false},
		{"cline 设成 adaptive 仍只进 openai 列表", profiledAccount(PlatformCline, map[string]any{"api_protocol": APIProtocolAdaptive}), true, false},
		{"opencode_go 默认 Go 模式", profiledAccount(PlatformOpenCodeGo, nil), true, true},
		{"opencode_go Zen 模式", profiledAccount(PlatformOpenCodeGo, map[string]any{"account_mode": AccountModeZen}), true, true},
		// 自定义平台沿用原判定：看凭据里填了哪些地址。
		{"custom 三地址", customTestAccount(customFullURLs()), true, true},
		{"custom 只填 Chat Completions", customTestAccount(map[string]any{APIProtocolChatCompletions: "http://c.example/v1"}), true, false},
		{"custom 没有任何地址", customTestAccount(nil), false, false},
		// 不是多协议 profile 的平台仍不进入。
		{"gemini", &Account{Platform: PlatformGemini}, false, false},
		{"antigravity", &Account{Platform: PlatformAntigravity}, false, false},
		{"空平台", &Account{}, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.wantOpenAI, accountSupportsCompositeModelListProtocol(tc.account, CompositeModelListProtocolOpenAI), "openai 列表")
			require.Equal(t, tc.wantAnthropic, accountSupportsCompositeModelListProtocol(tc.account, CompositeModelListProtocolAnthropic), "anthropic 列表")
		})
	}
}

func TestProfiledPlatformDecisionDoesNotNeedUpstreamCatalogCache(t *testing.T) {
	// 按模型分流的平台按 profile 判定，不看运行时才有的上游模型目录缓存：缓存里什么都没有也进列表。
	account := profiledAccount(PlatformCommandCode, nil)
	require.True(t, account.routesByModel())
	_, key, ok := accountModelCatalogEndpoint(account)
	require.True(t, ok)
	loaded, _ := catalogState(&upstreamModelProtocols, key)
	require.False(t, loaded)
	require.True(t, accountSupportsCompositeModelListProtocol(account, CompositeModelListProtocolOpenAI))
}

func TestExistingPlatformsKeepCompositeModelListDecision(t *testing.T) {
	// 既有平台的结果保持不变（与 composite_model_protocol_test.go 的全表一致，这里补边角）。
	for _, platform := range []string{PlatformKimi, PlatformZhipu, PlatformDeepseek, PlatformMiniMax} {
		account := &Account{Platform: platform}
		require.True(t, accountSupportsCompositeModelListProtocol(account, CompositeModelListProtocolOpenAI), platform)
		require.False(t, accountSupportsCompositeModelListProtocol(account, CompositeModelListProtocolAnthropic), platform)
	}
	require.True(t, accountSupportsCompositeModelListProtocol(&Account{Platform: PlatformAnthropic}, CompositeModelListProtocolAnthropic))
	require.False(t, accountSupportsCompositeModelListProtocol(&Account{Platform: PlatformAnthropic}, CompositeModelListProtocolOpenAI))
	require.True(t, accountSupportsCompositeModelListProtocol(&Account{Platform: PlatformOpenAI}, CompositeModelListProtocolOpenAI))
	require.True(t, accountSupportsCompositeModelListProtocol(&Account{Platform: PlatformGrok}, CompositeModelListProtocolOpenAI))
}

// ARM64 测试分组的形态：只有一个 command_code 账号（映射 deepseek-flash）加一个停用的 deepseek 账号。
// 修复前分组模型列表为空；现在 command_code 声明的模型进入 openai 与 anthropic 两类来源。
func TestCompositeProtocolModelSourcesIncludeCommandCodeAccount(t *testing.T) {
	groupID := int64(96)
	commandCode := profiledAccount(PlatformCommandCode, map[string]any{"model_mapping": map[string]any{"deepseek-flash": "deepseek/deepseek-v4-flash"}})
	// 停用的 deepseek 账号不会出现在可调度账号列表里，所以分组里只有 command_code（与 Cline）账号。
	cline := profiledAccount(PlatformCline, map[string]any{"model_mapping": map[string]any{"cline-only": "x"}})
	cline.ID = 902
	svc := &GatewayService{accountRepo: &modelsListAccountRepoStub{byGroup: map[int64][]Account{groupID: {*commandCode, *cline}}}}

	openaiSources := svc.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolOpenAI)
	require.Equal(t, []string{"deepseek-flash"}, openaiSources[PlatformCommandCode])
	require.Equal(t, []string{"cline-only"}, openaiSources[PlatformCline])

	anthropicSources := svc.GetCompositeProtocolModelSources(context.Background(), &groupID, CompositeModelListProtocolAnthropic)
	require.Equal(t, []string{"deepseek-flash"}, anthropicSources[PlatformCommandCode])
	require.NotContains(t, anthropicSources, PlatformCline, "Cline 只有 Chat Completions，不进 anthropic 列表")
}
