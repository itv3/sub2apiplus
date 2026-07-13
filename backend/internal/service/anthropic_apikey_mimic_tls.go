package service

import (
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

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
			return nil
		}
	}

	if tlsFPProfileService == nil {
		return nil
	}
	return tlsFPProfileService.ResolveTLSProfile(account)
}
