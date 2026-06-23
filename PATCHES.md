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
- 修改理由：拆分 `inboundIsCodexCLI` 与 `upstreamMimicCodexCLI`，同时让账号级 API Key mimic 参与 `isCodexCLI`，保留 Codex body 语义，避免 Kilo 这类第三方请求被当作普通客户端清理。

- 锚点：`buildUpstreamRequest()`
- 修改理由：API Key mimic 强制覆盖出站 `user-agent` / `originator` / `OpenAI-Beta` / `version`，并清理客户端 session 类 header。

- 锚点：主 HTTP `/v1/responses` 发送路径
- 修改理由：API Key mimic 走 `DoWithTLS`，让账号绑定的 TLS profile 真正生效。

- 锚点：`forwardOpenAIPassthrough()`
- 修改理由：`openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 互斥，保持阶段一语义清晰。

## 升级复核清单

- Anthropic：
  - `applyClaudeCodeMimicHeaders()` 的默认 header 集合是否变化。
  - `claude.APIKeyBetaHeader` / `BetaTokenCounting` / 相关 beta 常量是否变化。
  - `buildUpstreamRequest()` / `buildCountTokensRequest()` 顶部是否新增必须逻辑需要同步进独立 builder。

- OpenAI：
  - `codexCLIUserAgent` / `codexCLIVersion` 是否变化。
  - `buildUpstreamRequest()` 的 API Key 主路径是否新增必须 header 或 body 预处理。
  - `DoWithTLS` 路径与 profile 解析语义是否变化。
  - 账号级 mimic 是否仍参与 `isCodexCLI`；不要误回退成只改上游 header，否则 Kilo 请求可能再次失败。

- 阶段二 body identity：
  - OpenAI/Codex 路径可只净化 `instructions` / `input` 中的客户端身份词，保持 `tools[*].name` / `tool_choice.name` 原样。
  - Claude API Key mimic 路径保持第一阶段 body 形态，不替换第三方客户端 system 文本，不新增工具名改写。
  - 对比测试以 BWG 入站为准：官方客户端入站作为基准，ARM64 入站代表 ARM64 sub2api 出站行为，并用 `client_ip` 分组。
