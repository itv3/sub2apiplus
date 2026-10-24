package service

// 候选池的"承接分组"共享规则。
//
// 候选池（真实请求）与 Codex 三方模型目录（静态发布页）都要回答同一个问题：
// 某个公开模型名在某个入站协议下，Composite 分组里"可能承接它的是哪些账号、谁是首选"。
// 两边必须永远给出同一个答案，所以分层、零转换判定和排序都收敛在本文件与候选池已有的函数里：
//
//   - 声明收集与强弱分层：compositeClaimTier（composite_account_claims.go）；
//   - 零转换判定：compositeAccountZeroConversion（本文件，候选池的 isZeroConversion 也调用它）；
//   - 零转换组优先、组内按 Priority 与账号 ID 排序：compositeReceiverGroup（本文件，
//     候选池的 compositePickPrimary 也调用它）。
//
// 区别只在输入：候选池基于实时读取的账号并叠加资格过滤、粘性与续链；
// 目录页只看配置态（持久启用的账号和凭据里的 model_mapping），不看冷却、限流等瞬态，
// 否则静态发布的结论会随冷却抖动。

import (
	"sort"

	"github.com/Wei-Shaw/sub2api/internal/domain"
)

// compositeAccountPersistentlyEnabled 判断账号是否处于持久启用状态：状态为 active 且可调度开关打开。
// 持久停用的账号不声明任何模型；瞬态的限流、过载、临时不可调度不算停用。
func compositeAccountPersistentlyEnabled(account *Account) bool {
	return account != nil && account.Status == StatusActive && account.Schedulable
}

// compositeAccountZeroConversion 判定账号对入站协议 inbound 是否无需协议转换即可直通上游。
// 它是零转换判定的唯一实现：
//
//   - OpenAI 网关族账号：resolveUpstreamProtocol 返回的上游协议等于入站协议即零转换，
//     routingModel 与 catalog 仅对按模型分流的账号有意义（调用方负责取得）；
//   - Grok 在 Responses 与 Chat Completions 入口有专属直通链路（resolveUpstreamProtocol 的说明要求
//     调用前先分流），这两种入站对它视为零转换，Anthropic 入站需要转换；
//   - Anthropic 平台账号只对 Anthropic 入站（含 count_tokens）零转换；
//     其余 Anthropic 网关族平台（Gemini、Antigravity、TypeSafe）上游不是 Anthropic 原生协议，一律需转换。
func compositeAccountZeroConversion(account *Account, inbound, routingModel string, catalog []string) bool {
	if account == nil {
		return false
	}
	if domain.UsesOpenAIGateway(account.Platform) {
		if account.IsGrok() {
			return inbound == APIProtocolResponses || inbound == APIProtocolChatCompletions
		}
		return resolveUpstreamProtocol(account, inbound, routingModel, catalog) == inbound
	}
	return account.Platform == PlatformAnthropic && inbound == APIProtocolAnthropic
}

// compositeReceiverGroup 从候选里挑出承接分组并排好序：零转换组非空时只取零转换组，
// 否则取需转换组（组间不混用，不做请求内换组）；组内按 compositeCandidateLess 排序
// （Priority 升序、账号 ID 升序），首项即首选账号。没有候选时返回 nil。
// 返回的分组可以跨平台：候选池只截取其中与首选同平台的账号交给下游单平台调度器，
// 目录页则需要看到整个分组。
func compositeReceiverGroup(candidates []*compositeCandidate) []*compositeCandidate {
	var zero, convert []*compositeCandidate
	for _, candidate := range candidates {
		if candidate.zeroConversion {
			zero = append(zero, candidate)
		} else {
			convert = append(convert, candidate)
		}
	}
	group := zero
	if len(group) == 0 {
		group = convert
	}
	if len(group) == 0 {
		return nil
	}
	sorted := append([]*compositeCandidate(nil), group...)
	sort.SliceStable(sorted, func(i, j int) bool { return compositeCandidateLess(sorted[i], sorted[j]) })
	return sorted
}

// compositeConfigReceiverSet 是按配置态求出的承接分组。
type compositeConfigReceiverSet struct {
	// Tier 是实际采用的声明强度层级；没有任何声明账号时为 CompositeClaimNone。
	Tier CompositeClaimStrength
	// ZeroConversion 为 true 表示承接分组是零转换组，false 表示零转换组为空、退到了需转换组。
	ZeroConversion bool
	// Accounts 是承接分组的全部账号（可跨平台），已排序，首项是首选账号；没有承接账号时为空。
	Accounts []*Account
}

// Primary 返回首选账号；没有承接账号时返回 nil。
func (s compositeConfigReceiverSet) Primary() *Account {
	if len(s.Accounts) == 0 {
		return nil
	}
	return s.Accounts[0]
}

// compositeConfigReceivers 按"配置态"求某个公开模型名在某个入站协议下的承接分组：
//
//  1. 持久停用的账号视同无声明，先剔除；
//  2. 对其余账号按凭据 model_mapping 收集声明强度，再用 compositeClaimTier 分层
//     （有强声明只取强声明层，全无才取弱声明层；空映射不入池）；
//  3. zeroConversion 对每个声明账号判定入站协议是否零转换；
//  4. 用 compositeReceiverGroup 取承接分组并排序。
//
// 不读取限流、冷却、配额等瞬态状态，也不做资格过滤。
func compositeConfigReceivers(accounts []*Account, model string, zeroConversion func(*Account) bool) compositeConfigReceiverSet {
	enabled := make([]*Account, 0, len(accounts))
	for _, account := range accounts {
		if compositeAccountPersistentlyEnabled(account) {
			enabled = append(enabled, account)
		}
	}
	tier, claimed := compositeClaimTier(enabled, func(account *Account) CompositeClaimStrength {
		return CompositeAccountClaimStrength(account, model)
	})
	if len(claimed) == 0 {
		return compositeConfigReceiverSet{}
	}
	candidates := make([]*compositeCandidate, 0, len(claimed))
	for _, account := range claimed {
		candidates = append(candidates, &compositeCandidate{account: account, zeroConversion: zeroConversion(account)})
	}
	group := compositeReceiverGroup(candidates)
	set := compositeConfigReceiverSet{Tier: tier, Accounts: make([]*Account, 0, len(group))}
	for _, candidate := range group {
		set.Accounts = append(set.Accounts, candidate.account)
	}
	if len(group) > 0 {
		set.ZeroConversion = group[0].zeroConversion
	}
	return set
}
