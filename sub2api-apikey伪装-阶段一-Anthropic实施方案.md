# 阶段一复盘：Anthropic API Key 官方客户端伪装

更新时间：2026-06-21

> 本文件原本是 Anthropic 阶段一实施方案。当前阶段一已经完成编码、测试、部署和 Kilo 验证，因此本文改为复盘文档。
>
> 配套主文档：`sub2api-apikey客户端伪装.md`

---

## 1. 阶段一结论

Anthropic API Key mimic 已完成，不再是早期设计里的“只做 header”。

当前实际能力：

- API Key 账号可开启 `anthropic_apikey_mimic_claude_code`。
- passthrough 与 mimic 互斥，mimic 优先。
- `/v1/messages` 和 `/v1/messages/count_tokens` 都覆盖。
- 出站认证仍为 `x-api-key`，不伪装 OAuth。
- 出站 header 采用 Claude Code CLI 形态。
- 出站 beta 不包含 `oauth-2025-04-20`。
- `count_tokens` 带 `token-counting-2024-11-01`。
- `claude-opus-4-6` / `claude-opus-4-7` / `claude-opus-4-8` 保留 `context-1m-2025-08-07`。
- API Key mimic 可使用 TLS fingerprint profile。
- 第三方客户端 body 会被改成 Claude Code body 形态。

已部署到 ARM64 测试服，并通过 Kilo 调用 `claude-opus-4-8` 流式请求。

---

## 2. 账号开关

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "enable_tls_fingerprint": true
}
```

规则：

- 只对 `PlatformAnthropic + AccountTypeAPIKey` 生效。
- 只接受 JSON boolean。
- 与 `anthropic_passthrough` 互斥。
- 不影响 OAuth / SetupToken / Bedrock / Vertex 账号。

---

## 3. 实际代码路径

新增文件：

- `backend/internal/service/account_apikey_mimicry.go`
- `backend/internal/service/gateway_apikey_mimicry.go`

既有文件接线：

- `backend/internal/service/account.go`
- `backend/internal/service/gateway_service.go`

关键函数：

- `IsAnthropicAPIKeyClaudeCodeMimicEnabled()`
- `shouldMimicAnthropicAPIKeyClaudeCode()`
- `buildAnthropicAPIKeyCLIMimicRequest()`
- `buildAnthropicAPIKeyCLICountTokensMimicRequest()`
- `applyAnthropicAPIKeyClaudeCodeMimicryToBody()`
- `buildAPIKeyMimicMetadataUserID()`

发送路径：

- 主请求继续复用 `Forward()` 的发送循环。
- `count_tokens` 继续复用 `ForwardCountTokens()` 的发送循环。
- 实际发送走 `DoWithTLS(... ResolveTLSProfile(account))`。

---

## 4. Body mimic 实际行为

阶段一已经做了 body 级模拟：

- 将第三方客户端的 `system` 改写为：
  - billing block
  - Claude Code system prompt
  - Claude Code expansion block
- 原客户端 `system` 被迁移到 messages 前置 user/assistant 对。
- 注入 `metadata.user_id`。
- 标准化 Claude Code 请求体。
- 重写 message/tool cache control。
- 对 tool name 做必要改写。
- 补齐 `temperature` 和 `max_tokens`。
- 对 billing block 做 CCH 签名。

这样做的原因是：Kilo 等第三方客户端如果只改 header，仍会被上游或模型侧识别为非官方客户端，且容易触发请求体差异。

---

## 5. 当前暴露的新问题

用户在 Kilo 与官方客户端里问同一个问题“你是什么模型”，回答明显不同：

- 官方客户端回答自己在 Claude Code / 官方环境里。
- Kilo 回答自己是 Kilo。

这说明当前仍有 body identity 泄漏。

可能来源：

- Kilo 注入的 system/instructions 中含有“我是 Kilo”或类似身份声明。
- 工具名、工具描述或 model alias 带有 Kilo 痕迹。
- 原客户端 system 虽已从 system 层迁移到 messages 层，但模型仍可能把它当作上下文遵循。
- Claude Desktop、Claude Code、agent-sdk 的真实 prompt/profile 并不完全一样。

结论：

> 要让模型“认为自己就在官方客户端中”，必须进入阶段二，对官方客户端真实 body 做抓包对照，并增加身份净化和官方身份注入能力。

---

## 6. 阶段二 Anthropic 修正方向

建议新增可控开关，而不是无条件删除第三方客户端 system：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "anthropic_apikey_mimic_official_identity": true
}
```

处理流程：

1. 抓 Claude 官方客户端 / Claude Code 的真实请求 body。
2. 抓 Kilo 经 ARM64 伪装后到 BWG 的请求 body。
3. 对比 `system`、`metadata`、`tools`、`tool_choice`、`messages`、`model`、`anthropic-beta`。
4. 识别并删除 Kilo/Cline/Roo/Cursor 等第三方客户端身份声明。
5. 保留项目规则、用户规则和必要工具规则。
6. 注入真实官方客户端 system prompt。
7. 如果工具名泄漏第三方客户端身份，建立双向工具名映射。
8. 用“你是什么模型 / 你在哪个客户端中”作为回归测试。

风险：

- 删除过多 system 内容会破坏 Kilo 的工具使用规则。
- 工具名映射需要回写，否则客户端可能无法执行 tool_use。
- Claude Desktop 与 Claude Code 可能需要不同 profile，不能混用。

---

## 7. 已有测试覆盖

相关测试位于：

- `backend/internal/service/gateway_anthropic_apikey_passthrough_test.go`
- `backend/internal/service/account_anthropic_passthrough_test.go`

覆盖重点：

- API Key mimic 丢弃客户端 header。
- API Key mimic 不带 OAuth beta。
- 第三方 body 被改写为 Claude Code 形态。
- `metadata.user_id` 被注入。
- system block 包含 billing block、Claude Code prompt、expansion block。
- tools cache control 被补齐。
- `temperature` 和 `max_tokens` 被补齐。
- `claude-opus-4-6/4-7/4-8` 保留 `context-1m-2025-08-07`。
- `count_tokens` 使用 token-counting beta。

---

## 8. 升级维护

升级 upstream 后重点复核：

- `applyClaudeCodeMimicHeaders()` 是否变化。
- `claude.DefaultHeaders` 是否变化。
- `claude.APIKeyBetaHeader` 和相关 beta 常量是否变化。
- `normalizeClaudeOAuthRequestBody()` 语义是否变化。
- `buildUpstreamRequest()` 顶部是否新增必须同步进 mimic builder 的逻辑。
- `buildCountTokensRequest()` 顶部是否新增必须同步进 mimic builder 的逻辑。
- `PATCHES.md` 中记录的接线点是否仍存在。

不要按旧方案回退到“header only”。阶段一的 Kilo 成功依赖 body mimic。
