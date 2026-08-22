package officialegress

// 这里集中导出允许作为运行时 binding key 的业务 SinkID。facade、pending_removal
// 与受审 scope-exclusion ID 有意不导出，防止共享层误用。
const (
	SinkCodexAdminTestCompact         SinkID = "codex.admin_test.compact"
	SinkCodexAdminTestResponses       SinkID = "codex.admin_test.responses"
	SinkCodexAlphaSearchDirect        SinkID = "codex.alpha_search.direct"
	SinkCodexAlphaSearchPATFallback   SinkID = "codex.alpha_search.pat_fallback"
	SinkCodexFilesBlobUpload          SinkID = "codex.files.blob_upload"
	SinkCodexFilesRegister            SinkID = "codex.files.register"
	SinkCodexImagesOAuthTest          SinkID = "codex.images.oauth_test"
	SinkCodexImagesResponses          SinkID = "codex.images.responses"
	SinkCodexModelsList               SinkID = "codex.models.list"
	SinkCodexOAuthExchange            SinkID = "codex.oauth.exchange"
	SinkCodexOAuthRefresh             SinkID = "codex.oauth.refresh"
	SinkCodexQuotaWHAM                SinkID = "codex.quota.wham"
	SinkCodexRealtimeCalls            SinkID = "codex.realtime.calls"
	SinkCodexRealtimeSideband         SinkID = "codex.realtime.sideband"
	SinkCodexResponsesAnthropicCompat SinkID = "codex.responses.anthropic_compat"
	SinkCodexResponsesChatCompletions SinkID = "codex.responses.chat_completions"
	SinkCodexResponsesForward         SinkID = "codex.responses.forward"
	SinkCodexResponsesPassthrough     SinkID = "codex.responses.passthrough"
	SinkCodexResponsesWS              SinkID = "codex.responses.ws"
	SinkCodexResponsesWSHTTPBridge    SinkID = "codex.responses.ws_http_bridge"
	SinkCodexResponsesWSV2Passthrough SinkID = "codex.responses.ws_v2_passthrough"
	SinkCodexUsageProbe               SinkID = "codex.usage.probe"
	SinkUnclassifiedAgentTaskRegister SinkID = "unclassified.agent.task_register"
	SinkUnclassifiedPATWhoAmI         SinkID = "unclassified.pat.whoami"
	SinkWebPrivacyAccountInfo         SinkID = "web.privacy.account_info"
	SinkWebPrivacyDisableTraining     SinkID = "web.privacy.disable_training"
	SinkWebPrivacySubscription        SinkID = "web.privacy.subscription"

	// Claude Code 2.1.226 FW-G candidate 的 persona_strict 闭集。它们只会进入
	// 显式构造的 Claude candidate Catalog，不会自动并入生产默认 Catalog。
	SinkClaudeMessagesInference SinkID = "claude.messages.inference"
	SinkClaudeLifecycleHello    SinkID = "claude.lifecycle.hello"
	SinkClaudePolicyLimits      SinkID = "claude.policy_limits"
	SinkClaudeRemoteSettings    SinkID = "claude.remote_settings"
	SinkClaudeOAuthProfile      SinkID = "claude.oauth.profile"
	SinkClaudeCountTokens       SinkID = "claude.messages.count_tokens"
	SinkClaudeOAuthTokenRefresh SinkID = "claude.oauth.token_refresh"
	SinkClaudeMCPServers        SinkID = "claude.mcp_servers"

	// Setup Token 明确排除在 Claude OAuth Persona 外，但推理与 token_count 仍是
	// 必须保留的产品能力，因此使用 non_persona_managed 的独立 Sink。
	SinkClaudeSetupTokenMessagesInference SinkID = "managed.claude.setup_token.messages_inference"
	SinkClaudeSetupTokenTokenCount        SinkID = "managed.claude.setup_token.token_count"

	// 以下 Sink 是 FW-E 的不可变历史基线。Claude active Catalog 会删除其中已退休的
	// Messages／TokenCount 两项；其余辅助出站按 non_persona_managed 继续受管。
	SinkClaudeLegacyAccountTest         SinkID = "unclassified.claude.account_test"
	SinkClaudeLegacyCookieAuthorize     SinkID = "unclassified.claude.cookie_authorize"
	SinkClaudeLegacyCookieOrganizations SinkID = "unclassified.claude.cookie_organizations"
	SinkClaudeLegacyMessagesInference   SinkID = "unclassified.claude.messages_inference"
	SinkClaudeLegacyOAuthExchange       SinkID = "unclassified.claude.oauth_exchange"
	SinkClaudeLegacyOAuthRefresh        SinkID = "unclassified.claude.oauth_refresh"
	SinkClaudeLegacyTokenCount          SinkID = "unclassified.claude.token_count"
	SinkClaudeLegacyUpstreamModels      SinkID = "unclassified.claude.upstream_models"
	SinkClaudeLegacyUsage               SinkID = "unclassified.claude.usage"
)
