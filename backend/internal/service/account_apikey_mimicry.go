package service

// IsAnthropicAPIKeyClaudeCodeMimicEnabled 返回 Anthropic API Key 账号是否开启 Claude Code header mimic。
func (a *Account) IsAnthropicAPIKeyClaudeCodeMimicEnabled() bool {
	return a != nil &&
		a.Platform == PlatformAnthropic &&
		a.Type == AccountTypeAPIKey &&
		a.getExtraBool("anthropic_apikey_mimic_claude_code")
}

// IsOpenAIAPIKeyCodexMimicEnabled 返回 OpenAI API Key 账号是否开启 Codex CLI header mimic。
func (a *Account) IsOpenAIAPIKeyCodexMimicEnabled() bool {
	return a != nil &&
		a.IsOpenAI() &&
		a.Type == AccountTypeAPIKey &&
		a.getExtraBool("openai_apikey_mimic_codex_cli")
}

// ShouldUseOpenAITLSFingerprint 返回当前账号是否应启用 OpenAI API Key mimic 的 TLS 指纹路径。
func (a *Account) ShouldUseOpenAITLSFingerprint() bool {
	return a != nil &&
		a.IsOpenAIAPIKeyCodexMimicEnabled() &&
		a.getExtraBool("enable_tls_fingerprint")
}

func shouldUseAnthropicAPIKeyPassthroughRuntime(account *Account, mimicClaudeCode bool) bool {
	return account != nil &&
		account.IsAnthropicAPIKeyPassthroughEnabled() &&
		!mimicClaudeCode
}
