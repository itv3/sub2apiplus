package service

import (
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// resolveAnthropicTLSProfileForRequest 按请求级 mimic 结果解析 TLS profile。
// 管理员显式选择的固定或随机 profile 始终优先于内置 Claude mimic profile。
func resolveAnthropicTLSProfileForRequest(
	account *Account,
	mimicAPIKeyClaudeCode bool,
	tlsFPProfileService *TLSFingerprintProfileService,
	configs ...*config.Config,
) *tlsfingerprint.Profile {
	if account == nil {
		return nil
	}

	if account.Platform == PlatformAnthropic && account.Type == AccountTypeAPIKey {
		if !mimicAPIKeyClaudeCode || !account.IsTLSFingerprintEnabled() {
			return nil
		}

		if account.GetTLSFingerprintProfileID() == 0 {
			mode := officialClientProfileModeActive
			if len(configs) > 0 {
				mode = officialClientProfileModeFromConfig(configs[0])
			}
			if mode == officialClientProfileModeActive {
				return newAnthropicOfficialEgressTLSProfile()
			}
			// 旧 Desktop 画像没有可复用的当前 Linux CLI 实抓 TLS，保持原行为。
			return nil
		}
	}

	if tlsFPProfileService == nil {
		return nil
	}
	return tlsFPProfileService.ResolveTLSProfile(account)
}
