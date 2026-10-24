//go:build unit

package service

// 本文件的部分用例改编自 Wei-Shaw/sub2api 的 PR #7114（来源提交
// bcb44606150881a8ce781ea2138d695b6e0d5c3e）中的 composite_account_claims_test.go，
// 并按本地"空映射账号一律无声明"的语义改写断言：
//   - 原文"空 mapping 走受控原生模型识别"的用例改为断言无声明；
//   - 原文 ollama_cloud 的 allowed_models 白名单声明用例改为断言无声明；
//   - 原文 Grok 默认映射算声明的用例改为断言无声明（只认管理员显式写的映射）；
//   - 原文 OpenAI OAuth 的"可服务模型"限制属于调度器终检，不在声明层，改为按映射断言。
// 同一批用例遵循 LGPL-3.0，与本仓库许可一致。

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func compositeClaimTestAccount(id int64, platform string, mapping map[string]any) Account {
	account := Account{ID: id, Platform: platform, Type: AccountTypeAPIKey}
	if mapping != nil {
		account.Credentials = map[string]any{"model_mapping": mapping}
	}
	return account
}

func TestCompositeAccountClaimsModel_NilAndEmptyInputs(t *testing.T) {
	require.False(t, CompositeAccountClaimsModel(nil, "gpt-5"))
	account := &Account{Platform: PlatformOpenAI}
	require.False(t, CompositeAccountClaimsModel(account, ""))
	require.False(t, CompositeAccountClaimsModel(account, "   "))
}

// 非空映射：精确键与通配键命中才算声明，未命中不回落到原生识别，空目标不构成声明。
func TestCompositeAccountClaimsModel_ExplicitMappingExactAndWildcard(t *testing.T) {
	account := compositeClaimTestAccount(1, PlatformOpenAI, map[string]any{
		"my-gpt":       "gpt-5",
		"team-*":       "gpt-5.1",
		"empty-target": "",
	})

	require.True(t, CompositeAccountClaimsModel(&account, "my-gpt"))
	require.True(t, CompositeAccountClaimsModel(&account, "team-alpha"))
	require.False(t, CompositeAccountClaimsModel(&account, "gpt-5"))
	require.False(t, CompositeAccountClaimsModel(&account, "unknown-alias"))
	require.False(t, CompositeAccountClaimsModel(&account, "empty-target"))
}

// 归一化回退：平台既有的归一化命中仍算声明。
func TestCompositeAccountClaimsModel_NormalizedLookupHit(t *testing.T) {
	account := compositeClaimTestAccount(1, PlatformGemini, map[string]any{
		"gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
	})
	require.True(t, CompositeAccountClaimsModel(&account, "gemini-3.1-pro-preview-customtools"))
	require.Equal(t, CompositeClaimExplicit, CompositeAccountClaimStrength(&account, "gemini-3.1-pro-preview-customtools"))
}

// 本地新语义：空映射账号对任何模型一律无声明，不论平台，不论模型名是否属于该平台。
func TestCompositeAccountClaimsModel_EmptyMappingNeverClaims(t *testing.T) {
	for _, platform := range []string{
		PlatformDeepseek, PlatformOpenAI, PlatformZhipu, PlatformKimi, PlatformAnthropic,
		PlatformGemini, PlatformOpenCodeGo, PlatformCommandCode, PlatformGrok, PlatformAntigravity,
	} {
		account := &Account{ID: 1, Platform: platform, Type: AccountTypeAPIKey}
		for _, model := range []string{"deepseek-flash", "gpt-5", "grok-4.3", "claude-sonnet-4", "unknown-alias"} {
			require.False(t, CompositeAccountClaimsModel(account, model), "platform=%s model=%s", platform, model)
		}
		// 凭据存在但没有 model_mapping，或映射是空对象，同样无声明。
		withCreds := &Account{ID: 2, Platform: platform, Type: AccountTypeAPIKey, Credentials: map[string]any{"api_key": "sk"}}
		require.False(t, CompositeAccountClaimsModel(withCreds, "deepseek-flash"), "platform=%s", platform)
		emptyObject := &Account{ID: 3, Platform: platform, Type: AccountTypeAPIKey,
			Credentials: map[string]any{"model_mapping": map[string]any{}}}
		require.False(t, CompositeAccountClaimsModel(emptyObject, "deepseek-flash"), "platform=%s", platform)
	}
}

// ollama_cloud 的 extra.allowed_models 出站白名单不再构成声明（空映射一律无声明）。
func TestCompositeAccountClaimsModel_OllamaAllowedModelsNoLongerClaims(t *testing.T) {
	account := &Account{
		ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Credentials: map[string]any{"base_url": "https://ollama.com/v1"},
		Extra:       map[string]any{"allowed_models": []any{"deepseek-flash"}},
	}
	require.False(t, CompositeAccountClaimsModel(account, "deepseek-flash"))
}

// 平台内置的默认映射（Grok、Antigravity、Gemini GoogleOne）不是管理员显式声明，不构成声明。
func TestCompositeAccountClaimsModel_PlatformDefaultMappingIsNotClaim(t *testing.T) {
	grok := &Account{ID: 1, Platform: PlatformGrok}
	require.NotEmpty(t, grok.GetModelMapping(), "前提：Grok 账号确有内置默认映射")
	for model := range grok.GetModelMapping() {
		require.False(t, CompositeAccountClaimsModel(grok, model), "grok default mapping key=%s", model)
		break
	}
	antigravity := &Account{ID: 2, Platform: PlatformAntigravity}
	require.NotEmpty(t, antigravity.GetModelMapping(), "前提：Antigravity 账号确有内置默认映射")
	for model := range antigravity.GetModelMapping() {
		require.False(t, CompositeAccountClaimsModel(antigravity, model), "antigravity default mapping key=%s", model)
		break
	}
}

// opencode_go 没有原生识别分支：仅显式映射命中才声明。
func TestCompositeAccountClaimsModel_OpenCodeGoRequiresExplicitMapping(t *testing.T) {
	empty := &Account{ID: 1, Platform: PlatformOpenCodeGo}
	require.False(t, CompositeAccountClaimsModel(empty, "deepseek-chat"))
	require.False(t, CompositeAccountClaimsModel(empty, "gpt-5"))

	mapped := compositeClaimTestAccount(2, PlatformOpenCodeGo, map[string]any{"deepseek-chat": "deepseek-chat"})
	require.True(t, CompositeAccountClaimsModel(&mapped, "deepseek-chat"))
	require.False(t, CompositeAccountClaimsModel(&mapped, "gpt-5"))
}

// OAuth 账号同样只看映射：声明属于配置态判定，"可服务模型"限制留给调度器终检。
func TestCompositeAccountClaimsModel_OAuthFollowsMappingOnly(t *testing.T) {
	oauth := &Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	require.False(t, CompositeAccountClaimsModel(oauth, "gpt-5"))
	require.False(t, CompositeAccountClaimsModel(oauth, "deepseek-v4-pro"))

	mapped := &Account{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"model_mapping": map[string]any{"deepseek-v4-pro": "deepseek-v4-pro"}}}
	require.True(t, CompositeAccountClaimsModel(mapped, "deepseek-v4-pro"))
}

// 声明强度分级：精确键为强声明，通配键为弱声明，空目标显式条目无声明且不回落通配。
func TestCompositeAccountClaimStrength(t *testing.T) {
	exact := compositeClaimTestAccount(1, PlatformDeepseek, map[string]any{"deepseek-chat": "deepseek-chat"})
	require.Equal(t, CompositeClaimExplicit, CompositeAccountClaimStrength(&exact, "deepseek-chat"))
	require.Equal(t, CompositeClaimNone, CompositeAccountClaimStrength(&exact, "deepseek-v4"))

	wildcard := compositeClaimTestAccount(2, PlatformOpenAI, map[string]any{"*": "gpt-5", "gpt-*": "gpt-5.1"})
	require.Equal(t, CompositeClaimWildcard, CompositeAccountClaimStrength(&wildcard, "any-alias"))

	// 同一账号同时有精确键与通配键时，精确键命中算强声明。
	withExact := compositeClaimTestAccount(3, PlatformOpenAI, map[string]any{"*": "gpt-5", "gpt-5": "gpt-5"})
	require.Equal(t, CompositeClaimExplicit, CompositeAccountClaimStrength(&withExact, "gpt-5"))

	// 空目标显式条目遮蔽通配：不构成声明，也不回落。
	emptyTarget := compositeClaimTestAccount(4, PlatformOpenAI, map[string]any{"empty-alias": "", "*": "gpt-5"})
	require.Equal(t, CompositeClaimNone, CompositeAccountClaimStrength(&emptyTarget, "empty-alias"))
	// 通配键映射目标为空同样不构成声明。
	wildcardEmptyTarget := compositeClaimTestAccount(5, PlatformOpenAI, map[string]any{"foo-*": ""})
	require.Equal(t, CompositeClaimNone, CompositeAccountClaimStrength(&wildcardEmptyTarget, "foo-x"))

	require.Equal(t, CompositeClaimNone, CompositeAccountClaimStrength(nil, "gpt-5"))
}

// 最长通配优先：同一模型命中多个通配键时按最长键的目标判定（目标为空则整体无声明）。
func TestCompositeAccountClaimStrength_LongestWildcardWins(t *testing.T) {
	account := compositeClaimTestAccount(1, PlatformOpenAI, map[string]any{"foo-*": "", "foo-bar-*": "target"})
	require.Equal(t, CompositeClaimWildcard, CompositeAccountClaimStrength(&account, "foo-bar-1"))
	require.Equal(t, CompositeClaimNone, CompositeAccountClaimStrength(&account, "foo-baz"))
}

// 通配一致性：同一通配映射在声明判定与既有 IsModelSupported 语义一致。
func TestCompositeAccountClaimsModel_WildcardConsistentWithSupport(t *testing.T) {
	account := compositeClaimTestAccount(1, PlatformDeepseek, map[string]any{"deepseek-v4*": "deepseek-v4"})
	for _, model := range []string{"deepseek-v4", "deepseek-v4-flash", "deepseek-v4-pro"} {
		require.True(t, CompositeAccountClaimsModel(&account, model), "model=%s", model)
		require.True(t, account.IsModelSupported(model), "model=%s", model)
	}
	require.False(t, CompositeAccountClaimsModel(&account, "deepseek-v3"))
}

// 瞬态字段不参与声明判定：限流、过载中的账号声明不变。
func TestCompositeAccountClaimsModel_IgnoresTransientState(t *testing.T) {
	now := time.Now()
	account := compositeClaimTestAccount(1, PlatformDeepseek, map[string]any{"deepseek-flash": "deepseek-flash"})
	account.RateLimitedAt = &now
	account.RateLimitResetAt = &now
	account.OverloadUntil = &now
	require.True(t, CompositeAccountClaimsModel(&account, "deepseek-flash"))
}

// 声明分层：有强声明账号时通配账号被压制；完全没有强声明才使用通配账号；都没有则为空。
func TestCompositeClaimTierAccounts(t *testing.T) {
	const model = "alias"
	strongA := compositeClaimTestAccount(1, PlatformDeepseek, map[string]any{model: "deepseek-chat"})
	strongB := compositeClaimTestAccount(2, PlatformOpenAI, map[string]any{model: "gpt-5"})
	weak := compositeClaimTestAccount(3, PlatformOpenAI, map[string]any{"*": "gpt-5"})
	none := compositeClaimTestAccount(4, PlatformOpenAI, map[string]any{"other": "gpt-5"})
	emptyMapping := Account{ID: 5, Platform: PlatformDeepseek, Type: AccountTypeAPIKey}

	tier, picked := compositeClaimTierOf([]Account{strongA, weak, none, emptyMapping, strongB}, model)
	require.Equal(t, CompositeClaimExplicit, tier)
	require.Equal(t, []int64{1, 2}, compositeTestAccountIDs(picked), "强声明压制弱声明，空映射与无关映射不入池")

	tier, picked = compositeClaimTierOf([]Account{weak, none, emptyMapping}, model)
	require.Equal(t, CompositeClaimWildcard, tier)
	require.Equal(t, []int64{3}, compositeTestAccountIDs(picked), "完全没有强声明时才使用弱声明账号")

	tier, picked = compositeClaimTierOf([]Account{none, emptyMapping}, model)
	require.Equal(t, CompositeClaimNone, tier)
	require.Empty(t, picked, "零声明时不生成集合")
}

// compositeClaimTierOf 对账号切片按声明强度分层，返回指向切片元素的指针。
func compositeClaimTierOf(accounts []Account, model string) (CompositeClaimStrength, []*Account) {
	pointers := make([]*Account, len(accounts))
	for i := range accounts {
		pointers[i] = &accounts[i]
	}
	return compositeClaimTier(pointers, func(account *Account) CompositeClaimStrength {
		return CompositeAccountClaimStrength(account, model)
	})
}

func compositeTestAccountIDs(accounts []*Account) []int64 {
	ids := make([]int64, 0, len(accounts))
	for _, account := range accounts {
		ids = append(ids, account.ID)
	}
	return ids
}
