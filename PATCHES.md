# PATCHES

本文档记录 `sub2api-apikey客户端伪装.md` 阶段一落地时的热点接线点、修改理由与后续升级复核建议，便于后续合并 upstream 时快速重放与复核。

## Anthropic

### `backend/internal/service/account.go`

- 锚点：`IsTLSFingerprintEnabled()`
- 修改理由：放开 `PlatformAnthropic + AccountTypeAPIKey + anthropic_apikey_mimic_claude_code=true` 的 TLS 指纹启用条件。

### `backend/internal/service/gateway_service.go`

- 锚点：`Forward()`
- 修改理由：`anthropic_passthrough` 与 `anthropic_apikey_mimic_claude_code` 互斥，mimic 优先。

- 锚点：`ForwardCountTokens()`
- 修改理由：`count_tokens` 入口与主路径保持同样的 passthrough 互斥语义。

- 锚点：`buildUpstreamRequest()`
- 修改理由：API Key mimic 通过顶部 early-return 分流到独立 builder，避免把大量逻辑塞进主干。

- 锚点：`buildCountTokensRequest()`
- 修改理由：`count_tokens` 走独立 mimic builder，避免客户端 header 污染并补齐 beta/header 语义。

## OpenAI

### `backend/internal/service/account.go`

- 锚点：新增 API Key mimic helper
- 修改理由：阶段一使用 `account.Extra` 承载 `openai_apikey_mimic_codex_cli`，保持 API-only、低入侵。

### `backend/internal/service/openai_gateway_service.go`

- 锚点：`Forward()`
- 修改理由：通过 `openAIAPIKeyCodexMimicProfile` 解析账号级 API Key mimic，让 mimic 参与 `isCodexCLI` 和 Responses 路由判定，保留 Codex body 语义，避免 Kilo 这类第三方请求被当作普通客户端清理。

- 锚点：`buildUpstreamRequest()`
- 修改理由：通过 `openAIUpstreamRequestPlan` 显式传入 `IsCodexCLI` / `PromptCacheKey` / API Key mimic profile，避免继续增加散落布尔参数。

- 锚点：主 HTTP `/v1/responses` 发送路径
- 修改理由：经 `doOpenAIHTTPUpstream()` 统一选择 `Do` / `DoWithTLS`，让 API Key mimic 绑定的 TLS profile 在 `/v1/responses` 与 `/v1/chat/completions` 转换路径保持一致。OpenAI Codex mimic 不使用内置 Node.js 默认 profile；只有显式绑定 TLS profile ID 且解析成功时才走 `DoWithTLS`。

- 锚点：`forwardOpenAIPassthrough()`
- 修改理由：`openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 互斥，保持阶段一语义清晰。

### `backend/internal/service/openai_apikey_mimic_profile.go`

- 锚点：`openAIAPIKeyCodexMimicProfile`
- 修改理由：统一承载 API Key Codex mimic 的 body rewrite、header rewrite、Responses 路由判断和探测/自测接线，避免 `/v1/responses`、`/v1/chat/completions`、probe、自测四处重复拼装。默认 profile 为实测的 `desktop_0_142`（`Codex Desktop/0.142.0`），保留 `cli_rs_0_125` 作为显式兼容/回滚 profile。

### `backend/internal/service/openai_apikey_mimicry.go`

- 锚点：`applyOpenAIAPIKeyCodexMimicHeaders()`
- 修改理由：OpenAI APIKey mimic 的出站 header 改为 profile 驱动。默认 Desktop profile 注入 `originator: Codex Desktop`、`Codex Desktop/0.142.0 ...` UA、`x-codex-*` / `session-id` / `thread-id`，并移除旧 `version` / `OpenAI-Beta` / 下划线 session header；显式 `cli_rs_0_125` profile 保留旧 `codex_cli_rs` header 形态。

### `backend/internal/service/apikey_mimic_identity.go`

- 锚点：`ensureOpenAIAPIKeyCodexPromptCacheKey()` / `ensureOpenAIAPIKeyCodexDesktopClientMetadata()`
- 修改理由：默认 Desktop profile 的 `prompt_cache_key` 改为稳定 UUID 形态，并补齐与 header 同源的 `client_metadata`，贴近实测 Codex Desktop 请求；显式 CLI profile 继续使用旧 `codex-mimic-*` 不透明 key。

### `backend/internal/service/openai_codex_transform.go`

- 锚点：`applyCodexResponsesNormalization()`
- 修改理由：抽出通用 Codex Responses 规范化层，API Key mimic 复用这一层处理裸 `role/content`、`system -> developer`、`stream` / `store` / `include` / `instructions` 默认值，避免与 OAuth Codex transform 长期平行漂移。

### `backend/internal/service/openai_apikey_responses_probe.go`

- 锚点：`ProbeOpenAIAPIKeyResponsesSupport()`
- 修改理由：Responses capability probe 对 mimic / 非 mimic 形态分键持久化，避免 `openai_apikey_mimic_codex_cli` 打开的探测结果污染普通 API Key 路由判定；阶段一 mimic 固定走 Responses，该分键当前作为观测/诊断数据。

### `backend/internal/pkg/openai/constants.go`

- 锚点：`CodexBaseInstructionsForModel()`
- 修改理由：保留 `gpt-5.5` / `gpt-5.2` 专项映射，同时明确其余 `gpt-5*` 非 codex 型号统一回落到 GPT-5.1 prompt；这是有意的宽匹配策略，不再收窄为仅 `gpt-5` 精确命中。

## 升级复核清单

- Anthropic：
  - `applyClaudeCodeMimicHeaders()` 的默认 header 集合是否变化。
  - `claude.APIKeyBetaHeader` / `BetaTokenCounting` / 相关 beta 常量是否变化。
  - `buildUpstreamRequest()` / `buildCountTokensRequest()` 顶部是否新增必须逻辑需要同步进独立 builder。

- OpenAI：
  - `codexDesktopUserAgent` / `codexDesktopOriginator` / `codexDesktopBetaFeatures` 是否变化；若重新抓包发现新版 Codex Desktop header 变更，需要同步 `desktop_0_142` 或新增新版 profile。
  - `codexCLIUserAgent` / `codexCLIVersion` 是否变化；它们当前仅作为 `cli_rs_0_125` 兼容 profile 与部分 OAuth 兜底使用，不应再误当成 APIKey mimic 的默认身份。
  - `openAIAPIKeyCodexMimicProfile` 的 body/header/route 行为是否仍被 `/v1/responses`、`/v1/chat/completions`、probe、自测共同复用。
  - `buildUpstreamRequest()` 的 `openAIUpstreamRequestPlan` 是否仍准确区分 `ForceCodexCLI`、官方入站 Codex、账号级 API Key mimic。
  - `doOpenAIHTTPUpstream()` 的 `DoWithTLS` 路径与 profile 解析语义是否变化；OpenAI Codex mimic 未绑定显式 TLS profile 时不要回落到内置 Node.js profile。
  - 账号级 mimic 是否仍参与 `isCodexCLI`；不要误回退成只改上游 header，否则 Kilo 请求可能再次失败。
  - `openai_responses_supported` 与 `openai_responses_supported_mimic_codex_cli` 的分键语义是否仍与观测/诊断用途一致；不要把 mimic 探测结果写回普通 capability 键，也不要误把该观测键接入阶段一 mimic 路由。
  - 默认 Desktop profile 的 `prompt_cache_key` 是否仍是稳定 UUID 形态且与 `session-id` / `thread-id` / `client_metadata.session_id` 同源；显式 CLI profile 的 `codex-mimic-*` 兼容行为是否仍可回退。
  - `CodexBaseInstructionsForModel()` 是否仍保持“其余 `gpt-5*` 非 codex -> GPT-5.1 prompt”的宽匹配；若上游模型族再细分，需要同步更新测试与文档。

- 阶段二 body identity：
  - OpenAI/Codex 路径可只净化 `instructions` / `input` 中的客户端身份词，保持 `tools[*].name` / `tool_choice.name` 原样。
  - Claude API Key mimic 路径保持第一阶段 body 形态，不替换第三方客户端 system 文本，不新增工具名改写。
  - 对比测试以 BWG 入站为准：官方客户端入站作为基准，ARM64 入站代表 ARM64 sub2api 出站行为，并用 `client_ip` 分组。
