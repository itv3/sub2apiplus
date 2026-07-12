package service

import (
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
)

// claudeCLI21207TLSProfile 复刻 2026-07-12 在 anyrouter.top 抓到的
// claude-cli/2.1.207（Node.js 26）ClientHello。除扩展序列外，其余参数沿用
// 已与官方样本一致的内置 Node 默认值。
func claudeCLI21207TLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name:              "Built-in Claude CLI 2.1.207 / Node.js 26 (captured anyrouter.top 2026-07-12)",
		ALPNProtocols:     []string{"http/1.1"},
		SupportedVersions: []uint16{utls.VersionTLS13, utls.VersionTLS12},
		Extensions:        []uint16{0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21},
	}
}

// resolveAnthropicTLSProfileForRequest 按请求级 mimic 结果解析 TLS profile。
// 管理员显式选择的固定或随机 profile 始终优先于内置 Claude mimic profile。
func resolveAnthropicTLSProfileForRequest(
	account *Account,
	mimicAPIKeyClaudeCode bool,
	tlsFPProfileService *TLSFingerprintProfileService,
) *tlsfingerprint.Profile {
	if account == nil {
		return nil
	}

	if account.Platform == PlatformAnthropic && account.Type == AccountTypeAPIKey {
		if !mimicAPIKeyClaudeCode || !account.IsTLSFingerprintEnabled() {
			return nil
		}

		if account.GetTLSFingerprintProfileID() == 0 {
			return claudeCLI21207TLSProfile()
		}
	}

	if tlsFPProfileService == nil {
		return nil
	}
	return tlsFPProfileService.ResolveTLSProfile(account)
}
